"""Shared paths, Arrow schemas, normalization helpers, and I/O utilities for M3 Member 2."""
from __future__ import annotations

import csv
import json
from pathlib import Path
import re
import unicodedata
from typing import Any

import pyarrow as pa
import pyarrow.parquet as pq

PROJECT_ROOT = Path(__file__).resolve().parents[2]
DEFAULT_CLEANING_DIR = PROJECT_ROOT / "data" / "output" / "m3-cleaning"
DEFAULT_MANUAL_ROOT = PROJECT_ROOT / "data" / "input"
DEFAULT_OUTPUT_DIR = PROJECT_ROOT / "data" / "output" / "m3-labeling"
DEFAULT_MEMBER_DIRS = ("Minh_Hang", "Trong_Nhan", "Vinh_Nguyen", "Yen_Vy")

VALID_LABEL_STATUSES = frozenset({"matched", "no_match"})
VALID_LABEL_SOURCES = frozenset({"manual", "auto"})
VALID_PARTITIONS = frozenset({"train", "valid", "test"})

STRING = pa.string()

MANUAL_LABELS_SCHEMA = pa.schema([
    pa.field("arxiv_id", STRING, False),
    pa.field("citation_key", STRING, False),
    pa.field("groundtruth_arxiv_id", STRING, True),
    pa.field("label_status", STRING, False),
    pa.field("note", STRING, True),
])

AUTO_LABELS_SCHEMA = pa.schema([
    pa.field("arxiv_id", STRING, False),
    pa.field("citation_key", STRING, False),
    pa.field("groundtruth_arxiv_id", STRING, True),
    pa.field("label_status", STRING, False),
    pa.field("match_rule", STRING, False),
    pa.field("match_score", pa.float32(), True),
])

LABELED_ENTRIES_SCHEMA = pa.schema([
    pa.field("arxiv_id", STRING, False),
    pa.field("citation_key", STRING, False),
    pa.field("partition", STRING, False),
    pa.field("label_source", STRING, False),
    pa.field("label_status", STRING, False),
    pa.field("groundtruth_arxiv_id", STRING, True),
    pa.field("match_rule", STRING, False),
])

PUBLICATION_SPLITS_SCHEMA = pa.schema([
    pa.field("arxiv_id", STRING, False),
    pa.field("partition", STRING, False),
    pa.field("label_source", STRING, False),
    pa.field("n_bibtex_entries", pa.int32(), False),
    pa.field("n_labeled_entries", pa.int32(), False),
    pa.field("n_matched", pa.int32(), False),
    pa.field("n_no_match", pa.int32(), False),
    pa.field("n_ref_candidates", pa.int32(), False),
])

_STOPWORDS = frozenset({
    "a", "an", "and", "are", "as", "at", "be", "by", "for", "from", "in",
    "into", "is", "of", "on", "or", "over", "that", "the", "their", "this",
    "to", "under", "using", "via", "with",
})
_NEW_ARXIV_RE = re.compile(r"(?<![0-9.])(\d{2})(0[1-9]|1[0-2])[.-](\d{4,5})(?:v[1-9]\d*)?(?!\d)")
_OLD_ARXIV_RE = re.compile(r"(?<![a-z0-9/.-])([a-z][a-z0-9.-]*/\d{7})(?:v[1-9]\d*)?(?!\d)", re.IGNORECASE)
_ARXIV_DOI_RE = re.compile(r"10\.48550/arxiv\.([^\s,;)}\]]+)", re.IGNORECASE)
_ARXIV_URL_RE = re.compile(r"arxiv\.org/(?:abs|pdf)/([^\s?#),;}\]]+)", re.IGNORECASE)
_ARXIV_PREFIX_RE = re.compile(r"\barxiv\s*[:\s]\s*([a-z][a-z0-9.-]*/\d{7}|\d{4}\.\d{4,5})(?:v\d+)?", re.IGNORECASE)
_LATEX_CMD_RE = re.compile(r"\\[a-zA-Z@]+\*?")
_NON_ALNUM_RE = re.compile(r"[^a-z0-9]+")
_YEAR_PREFIX_RE = re.compile(r"^(\d{4})")
_AUTHOR_NOISE = frozenset({"et", "al", "others", "jr", "sr", "ii", "iii", "iv", "de", "del", "van", "von", "der", "la", "le", "da", "di"})


def normalize_arxiv_id(value: str | None) -> str | None:
    """Normalize modern and legacy arXiv identifiers to canonical base ID without version."""
    if not isinstance(value, str):
        return None
    cleaned = value.strip()
    if not cleaned:
        return None
    cleaned = re.sub(r"^https?://(?:export\.)?arxiv\.org/(?:abs|pdf)/", "", cleaned, flags=re.IGNORECASE)
    cleaned = re.sub(r"^10\.48550/arxiv\.", "", cleaned, flags=re.IGNORECASE)
    cleaned = re.sub(r"^arxiv:\s*", "", cleaned, flags=re.IGNORECASE)
    cleaned = re.sub(r"\.pdf$", "", cleaned, flags=re.IGNORECASE)
    cleaned = re.sub(r"v[1-9]\d*$", "", cleaned, flags=re.IGNORECASE)
    match = re.fullmatch(r"(\d{2})(\d{2})[.-](\d{4,5})", cleaned)
    if match and 1 <= int(match.group(2)) <= 12:
        width = 5 if int(match.group(1)) >= 15 else 4
        return f"{match.group(1)}{match.group(2)}.{match.group(3).zfill(width)}"
    if re.fullmatch(r"[a-z][a-z0-9.-]*/\d{7}", cleaned, flags=re.IGNORECASE):
        return cleaned.lower()
    return None


def extract_arxiv_ids(text_value: str | None) -> list[str]:
    """Extract canonical arXiv IDs mentioned in URLs, DOIs, journals, or arXiv prefixes."""
    if not text_value:
        return []
    found: list[str] = []

    def add_candidate(raw_candidate: str) -> None:
        normalized = normalize_arxiv_id(raw_candidate.rstrip(".,;:)]}"))
        if normalized and normalized not in found:
            found.append(normalized)

    for pattern in (_ARXIV_DOI_RE, _ARXIV_URL_RE, _ARXIV_PREFIX_RE):
        for match in pattern.finditer(text_value):
            add_candidate(match.group(1))

    if "arxiv" in text_value.lower() or "preprint" in text_value.lower():
        for match in _NEW_ARXIV_RE.finditer(text_value):
            add_candidate(f"{match.group(1)}{match.group(2)}.{match.group(3)}")
        for match in _OLD_ARXIV_RE.finditer(text_value):
            add_candidate(match.group(1))
    return found


def strip_diacritics(value: str) -> str:
    """Fold Unicode characters to ASCII-friendly lowercase form."""
    decomposed = unicodedata.normalize("NFKD", value)
    return "".join(ch for ch in decomposed if not unicodedata.combining(ch))


def normalize_title(value: str | None) -> str:
    """Normalize a paper title for exact and fuzzy classic string matching."""
    if not value:
        return ""
    cleaned = strip_diacritics(value)
    cleaned = _LATEX_CMD_RE.sub(" ", cleaned)
    cleaned = cleaned.replace("{", " ").replace("}", " ").replace("$", " ")
    cleaned = _NON_ALNUM_RE.sub(" ", cleaned.lower())
    return " ".join(cleaned.split())


def title_tokens(norm_title: str, *, remove_stopwords: bool = True) -> set[str]:
    """Return normalized word tokens for a title."""
    if not norm_title:
        return set()
    tokens = {tok for tok in norm_title.split() if len(tok) >= 2}
    if remove_stopwords:
        filtered = tokens - _STOPWORDS
        if filtered:
            return filtered
    return tokens


_MULTI_SURNAME_INITIAL_RE = re.compile(
    r"([A-Za-z][A-Za-z'\-]{1,}(?:\s+[A-Za-z][A-Za-z'\-]{1,})?),\s*(?:[A-Z]\.?\s*){1,3}(?=[,&]|$)"
)


def _add_surname_tokens(raw_part: str, surnames: set[str], first_surname: str | None) -> str | None:
    tokens = [
        tok for tok in _NON_ALNUM_RE.sub(" ", raw_part.lower()).split()
        if len(tok) >= 2 and tok not in _AUTHOR_NOISE
    ]
    if not tokens:
        fallback = _NON_ALNUM_RE.sub("", raw_part.lower())
        if len(fallback) >= 2:
            tokens = [fallback]
    if tokens:
        if first_surname is None:
            first_surname = tokens[-1]
        surnames.update(tokens)
    return first_surname


def extract_author_surnames(author_list: list[str] | None) -> tuple[set[str], str | None]:
    """Extract normalized surname set and first-author surname from a list of author strings."""
    if not author_list:
        return set(), None
    surnames: set[str] = set()
    first_surname: str | None = None
    for raw_author in author_list:
        if not raw_author:
            continue
        cleaned = strip_diacritics(raw_author)
        cleaned = _LATEX_CMD_RE.sub(" ", cleaned)
        cleaned = cleaned.replace("{", " ").replace("}", " ").replace("\\'", "").strip()
        if not cleaned or cleaned.lower() in {"others", "et al", "et al."}:
            continue
        multi_hits = _MULTI_SURNAME_INITIAL_RE.findall(cleaned)
        if len(multi_hits) >= 2 or ("&" in cleaned and len(multi_hits) >= 1):
            for hit in multi_hits:
                first_surname = _add_surname_tokens(hit, surnames, first_surname)
            amp_match = re.search(r"&\s*([A-Za-z][A-Za-z'\-]{1,}),", cleaned)
            if amp_match:
                first_surname = _add_surname_tokens(amp_match.group(1), surnames, first_surname)
            continue
        if "," in cleaned:
            surname_part = cleaned.split(",", 1)[0]
        else:
            parts = [p for p in re.split(r"\s+", cleaned) if p]
            # Drop trailing suffixes like Jr. / III.
            while len(parts) > 1 and _NON_ALNUM_RE.sub("", parts[-1].lower()) in {"jr", "sr", "ii", "iii", "iv"}:
                parts.pop()
            surname_part = parts[-1] if parts else ""
        first_surname = _add_surname_tokens(surname_part, surnames, first_surname)
    return surnames, first_surname


def extract_ref_year(submission_date: str | None, ref_arxiv_id: str | None) -> int | None:
    """Derive reference year from submission date, falling back to the arXiv ID prefix."""
    if submission_date:
        match = _YEAR_PREFIX_RE.match(submission_date.strip())
        if match:
            year = int(match.group(1))
            if 1900 <= year <= 2100:
                return year
    if ref_arxiv_id:
        match = re.match(r"^(\d{2})(0[1-9]|1[0-2])\.\d{4,5}$", ref_arxiv_id)
        if match:
            return 2000 + int(match.group(1))
        old_match = re.match(r"^[a-z][a-z0-9.-]*/(\d{2})(0[1-9]|1[0-2])\d{3}$", ref_arxiv_id)
        if old_match:
            yy = int(old_match.group(1))
            return (1900 + yy) if yy >= 91 else (2000 + yy)
    return None


def load_quarantine_ref_conflicts(cleaning_dir: Path) -> set[str]:
    """Return publication arXiv IDs that had reference_field_conflict in M3 cleaning quarantine."""
    quarantine_path = cleaning_dir / "quarantine.jsonl"
    conflicted: set[str] = set()
    if not quarantine_path.is_file():
        return conflicted
    with quarantine_path.open("r", encoding="utf-8") as stream:
        for line in stream:
            if not line.strip():
                continue
            record = json.loads(line)
            if record.get("code") == "reference_field_conflict":
                aid = normalize_arxiv_id(record.get("arxiv_id"))
                if aid:
                    conflicted.add(aid)
    return conflicted


def load_publication_coverage(cleaning_dir: Path) -> dict[str, dict[str, Any]]:
    """Load Member 1 publication_coverage.csv indexed by normalized arxiv_id."""
    coverage_path = cleaning_dir / "publication_coverage.csv"
    coverage: dict[str, dict[str, Any]] = {}
    with coverage_path.open("r", encoding="utf-8-sig", newline="") as stream:
        for row in csv.DictReader(stream):
            aid = normalize_arxiv_id(row.get("arxiv_id"))
            if not aid:
                continue
            coverage[aid] = {
                "arxiv_id": aid,
                "member": (row.get("member") or "").strip(),
                "metadata_ok": row.get("metadata_ok") == "True",
                "reference_status": (row.get("reference_status") or "").strip(),
                "references_complete": row.get("references_complete") == "True",
                "references_clean": int(row.get("references_clean") or 0),
                "bibtex_clean": int(row.get("bibtex_clean") or 0),
                "labeling_candidate": row.get("labeling_candidate") == "True",
            }
    return coverage


def write_parquet(path: Path, rows: list[dict[str, Any]], schema: pa.Schema) -> None:
    """Write rows to a Parquet file with an explicit Arrow schema."""
    path.parent.mkdir(parents=True, exist_ok=True)
    table = pa.Table.from_pylist(rows, schema=schema)
    pq.write_table(table, path, compression="zstd")


def write_json(path: Path, payload: Any) -> None:
    """Write UTF-8 JSON report with deterministic formatting."""
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


def write_csv(path: Path, rows: list[dict[str, Any]], fieldnames: list[str]) -> None:
    """Write UTF-8 CSV without altering string IDs."""
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8", newline="") as stream:
        writer = csv.DictWriter(stream, fieldnames=fieldnames)
        writer.writeheader()
        for row in rows:
            writer.writerow({k: ("" if row.get(k) is None else row.get(k)) for k in fieldnames})
