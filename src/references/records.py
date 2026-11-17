"""Validate papers_index.json, normalize S2 responses, and build reference records.

The index maps each base arXiv ID to version strings, for example:
    {"2608.09608": ["v1", "v2"]}
Version numbers are positive integers without leading zeros."""

from __future__ import annotations

import json
import re
import os

# ---------------------------------------------------------------------------
# Regex
# ---------------------------------------------------------------------------
# Base arXiv IDs include modern IDs and old archive/YYMMNNN IDs.
_BASE_ID_RE = re.compile(
    r"(?:[a-z][a-z0-9.\-]*/[0-9]{7}|[0-9]{4}\.[0-9]{4,5})",
    re.IGNORECASE,
)
# Versions start at v1 and have no leading zeros.
_VERSION_RE = re.compile(r"^v([1-9]\d*)$")


# ---------------------------------------------------------------------------
# Input and response errors
# ---------------------------------------------------------------------------
class InputError(ValueError):
    """Missing input, invalid JSON, or invalid input schema; callers use exit code 2."""


class ResponseError(ValueError):
    """Invalid S2 response fields; callers retry or record the publication error."""


# ---------------------------------------------------------------------------
# Input index validation
# ---------------------------------------------------------------------------
def load_index(path: str) -> list[dict]:
    """Load and validate a publication-to-versions index.

    Return dictionaries containing arxiv_id and fully qualified versions. Keep
    publication order, deduplicate and numerically sort versions, and allow empty
    version lists. Raise InputError for unreadable input, invalid JSON, duplicate
    keys, invalid base IDs, or invalid version lists."""
    if not os.path.isfile(path):
        raise InputError(f"File not found: {path}")
    try:
        with open(path, encoding="utf-8") as f:
            text = f.read()
    except OSError as e:
        raise InputError(f"Cannot read file {path}: {e}") from e

    # Detect duplicate keys before json.loads collapses them.
    seen_keys: set[str] = set()
    duplicate_keys: list[str] = []

    def object_pairs_hook(pairs):
        d = {}
        for k, v in pairs:
            if k in seen_keys:
                duplicate_keys.append(k)
            seen_keys.add(k)
            d[k] = v
        return d

    try:
        data = json.loads(text, object_pairs_hook=object_pairs_hook)
    except json.JSONDecodeError as e:
        raise InputError(f"Invalid JSON in {path}: {e}") from e

    if not isinstance(data, dict):
        raise InputError(
            f"{path}: root must be an object (dict), got {type(data).__name__}"
        )
    if duplicate_keys:
        raise InputError(
            f"{path}: duplicate keys: {duplicate_keys[:5]}"
            + (" ..." if len(duplicate_keys) > 5 else "")
        )
    if not data:
        # An empty index is valid and requires no HTTP calls.
        return []

    result = []
    for raw_key, raw_versions in data.items():
        # Validate key
        if not isinstance(raw_key, str) or not _BASE_ID_RE.fullmatch(raw_key):
            raise InputError(
                f"{path}: invalid base arXiv ID: {raw_key!r}"
            )
        # Validate versions
        if not isinstance(raw_versions, list):
            raise InputError(
                f"{path}: value for key {raw_key!r} must be a list, got "
                f"{type(raw_versions).__name__}"
            )
        seen_v: set[int] = set()
        sorted_versions: list[str] = []
        for v in raw_versions:
            if not isinstance(v, str):
                raise InputError(
                    f"{path}: version for {raw_key!r} must be a string, got "
                    f"{type(v).__name__} ({v!r})"
                )
            m = _VERSION_RE.fullmatch(v)
            if not m:
                raise InputError(
                    f"{path}: version {v!r} for {raw_key!r} is invalid "
                    f"(expected v1, v2, ... without v0 or leading zeros)"
                )
            n = int(m.group(1))
            if n not in seen_v:
                seen_v.add(n)
                sorted_versions.append(v)
        # Sort versions numerically.
        sorted_versions.sort(key=lambda s: int(_VERSION_RE.match(s).group(1)))
        result.append(
            {
                "arxiv_id": raw_key,
                "versions": [f"{raw_key}{v}" for v in sorted_versions],
            }
        )
    return result


def folder_name(arxiv_id: str) -> str:
    """Keep the existing folder convention and reject IDs escaping the output root."""
    if not isinstance(arxiv_id, str) or not _BASE_ID_RE.fullmatch(arxiv_id):
        raise InputError(f"Invalid base arXiv ID: {arxiv_id!r}")
    return arxiv_id.replace("/", "_").replace(".", "-")


def validate_reference(ref, context: str) -> None:
    """Validate fields used by normalization while retaining optional nulls and missing IDs."""
    if ref is None:
        return
    if not isinstance(ref, dict):
        raise ResponseError(f"{context}: reference must be an object or null")
    for name in ("paperId", "title", "publicationDate", "venue"):
        if ref.get(name) is not None and not isinstance(ref[name], str):
            raise ResponseError(f"{context}.{name}: must be a string or null")
    year = ref.get("year")
    if year is not None and (isinstance(year, bool) or not isinstance(year, int)):
        raise ResponseError(f"{context}.year: must be an integer or null")
    ext = ref.get("externalIds")
    if ext is not None:
        if not isinstance(ext, dict):
            raise ResponseError(f"{context}.externalIds: must be an object or null")
        for name in ("ArXiv", "DOI"):
            if ext.get(name) is not None and not isinstance(ext[name], str):
                raise ResponseError(
                    f"{context}.externalIds.{name}: must be a string or null"
                )
    authors = ref.get("authors")
    if authors is not None:
        if not isinstance(authors, list):
            raise ResponseError(f"{context}.authors: must be a list or null")
        for i, author in enumerate(authors):
            if author is None:
                continue
            if not isinstance(author, dict):
                raise ResponseError(f"{context}.authors[{i}]: must be an object or null")
            if author.get("name") is not None and not isinstance(author["name"], str):
                raise ResponseError(
                    f"{context}.authors[{i}].name: must be a string or null"
                )


# ---------------------------------------------------------------------------
# Validate response batch
# ---------------------------------------------------------------------------
def validate_batch_entry(entry, arxiv_id: str):
    """Validate one S2 batch entry before normalization.

    None is a valid missing lookup. Raise ResponseError for invalid entry types
    or missing or malformed required fields."""
    if entry is None:
        # A missing S2 lookup is a valid result.
        return
    if not isinstance(entry, dict):
        raise ResponseError(
            f"{arxiv_id}: batch entry must be an object or null, "
            f"got {type(entry).__name__}"
        )
    # The required references field must be a list.
    if "references" not in entry:
        raise ResponseError(f"{arxiv_id}: batch entry is missing 'references'")
    refs = entry["references"]
    if refs is None or not isinstance(refs, list):
        raise ResponseError(
            f"{arxiv_id}: 'references' must be a list, "
            f"got {type(refs).__name__ if refs is not None else 'null'}"
        )
    # A present referenceCount must be a nonnegative integer, not bool.
    count = entry.get("referenceCount")
    if count is not None:
        if isinstance(count, bool) or not isinstance(count, int) or count < 0:
            raise ResponseError(
                f"{arxiv_id}: 'referenceCount' must be a nonnegative integer, "
                f"got {count!r}"
            )
    # Validate fields used during reference normalization.
    for i, ref in enumerate(refs):
        validate_reference(ref, f"{arxiv_id}.references[{i}]")


def validate_page_data(data, arxiv_id: str, offset: int, seen_offsets: set[int]):
    """Validate a references page and return its next offset, or None at the end.

    Reject missing data, invalid offsets, and pagination loops with ResponseError."""
    if not isinstance(data, dict):
        raise ResponseError(
            f"{arxiv_id} offset={offset}: page response must be an object, "
            f"got {type(data).__name__}"
        )
    if "data" not in data:
        raise ResponseError(f"{arxiv_id} offset={offset}: page is missing 'data'")
    page_data = data["data"]
    if not isinstance(page_data, list):
        raise ResponseError(
            f"{arxiv_id} offset={offset}: 'data' must be a list, "
            f"got {type(page_data).__name__}"
        )
    if len(page_data) == 0 and data.get("next") is not None:
        raise ResponseError(
            f"{arxiv_id} offset={offset}: empty page still has 'next'; "
            f"possible infinite pagination loop"
        )
    # Validate 'next'
    nxt = data.get("next")
    if nxt is not None and (isinstance(nxt, bool) or not isinstance(nxt, int)):
        raise ResponseError(
            f"{arxiv_id} offset={offset}: 'next' must be an integer or null, "
            f"got {nxt!r}"
        )
    if nxt is not None and nxt <= offset:
        raise ResponseError(
            f"{arxiv_id} offset={offset}: 'next'={nxt} does not advance; "
            f"possible pagination loop"
        )
    if nxt is not None and nxt in seen_offsets:
        raise ResponseError(
            f"{arxiv_id} offset={offset}: 'next'={nxt} was already used; "
            f"pagination loop"
        )
    if "offset" in data and (
        type(data["offset"]) is not int or data["offset"] != offset
    ):
        raise ResponseError(f"{arxiv_id}: returned offset does not match requested offset {offset}")
    for i, item in enumerate(page_data):
        if not isinstance(item, dict) or "citedPaper" not in item:
            raise ResponseError(
                f"{arxiv_id} offset={offset}: data[{i}] must be an object containing citedPaper"
            )
        validate_reference(
            item["citedPaper"], f"{arxiv_id} offset={offset}.data[{i}].citedPaper"
        )
    return nxt


# ---------------------------------------------------------------------------
# Reference-count checks
# ---------------------------------------------------------------------------
def count_matches(entry: dict, n_refs: int) -> bool:
    """Return whether a valid referenceCount matches the received list length."""
    count = entry.get("referenceCount")
    if (
        count is None
        or isinstance(count, bool)
        or not isinstance(count, int)
        or count < 0
    ):
        return False
    return count == n_refs


def count_is_valid(entry: dict) -> bool | None:
    """Return whether referenceCount is a nonnegative integer; return None if missing."""
    count = entry.get("referenceCount")
    if count is None:
        return None
    return not isinstance(count, bool) and isinstance(count, int) and count >= 0


# ---------------------------------------------------------------------------
# Reference normalization
# ---------------------------------------------------------------------------
_ARXIV_DOI = re.compile(r"^10\.48550/arxiv\.(.+)$", re.IGNORECASE)
_VERSION_SUFFIX = re.compile(r"v\d+$")


def _strip_version(arxiv_id: str) -> str:
    return _VERSION_SUFFIX.sub("", arxiv_id.strip())


def _extract_arxiv_id(cited: dict) -> tuple[str | None, str | None]:
    """Prefer externalIds.ArXiv, falling back to an arXiv DOI."""
    ext = cited.get("externalIds") or {}
    if ext.get("ArXiv"):
        return _strip_version(str(ext["ArXiv"])), "externalIds"
    match = _ARXIV_DOI.match(str(ext.get("DOI") or ""))
    if match:
        return _strip_version(match.group(1)), "doi"
    return None, None


def normalize_ref(cited: dict | None) -> dict:
    validate_reference(cited, "reference")
    cited = cited or {}
    aid, source = _extract_arxiv_id(cited)
    return {
        "ref_arxiv_id": aid,
        "arxiv_id_source": source,
        "ref_title": cited.get("title"),
        "ref_authors": [
            a.get("name") for a in (cited.get("authors") or []) if a and a.get("name")
        ],
        "ref_submission_date": cited.get("publicationDate"),
        "ref_year": cited.get("year"),
        "ref_venue": cited.get("venue"),
        "ref_s2_id": cited.get("paperId"),
        "ref_external_ids": cited.get("externalIds"),
    }


# ---------------------------------------------------------------------------
# Record construction
# ---------------------------------------------------------------------------
def build_record(
    publication: dict,
    entry: dict | None,
    *,
    raw_refs: list | None = None,
    method: str = "count_match",
    fetched_at: str | None = None,
) -> dict:
    """Build references.json after count verification or complete pagination.

    publication contains the base ID and qualified versions. entry is a validated
    S2 entry or None for not_in_s2. raw_refs overrides the batch references after
    pagination. method records count_match, paged_count_match,
    paged_count_mismatch, or not_found. fetched_at is an ISO timestamp."""
    arxiv_id = publication["arxiv_id"]
    if entry is None:
        # Save a completed not_in_s2 lookup record.
        return {
            "arxiv_id": arxiv_id,
            "s2_paper_id": None,
            "s2_reference_count": None,
            "status": "not_in_s2",
            "applies_to_versions": publication.get("versions") or [],
            "n_refs": 0,
            "n_refs_with_arxiv": 0,
            "n_refs_without_s2_id": 0,
            "references": [],
            "fetched_at": fetched_at,
            "verification": {"complete": True, "method": "not_found"},
        }

    validate_batch_entry(entry, arxiv_id)
    refs_raw = raw_refs if raw_refs is not None else entry["references"]
    complete = count_matches(entry, len(refs_raw))
    mismatch = method == "paged_count_mismatch"
    if mismatch and (raw_refs is None or count_is_valid(entry) is not True or complete):
        raise ResponseError(f"{arxiv_id}: invalid count mismatch record")
    if not complete and not mismatch:
        raise ResponseError(f"{arxiv_id}: references are not fully verified; cannot build record")
    if method not in ("count_match", "paged_count_match", "paged_count_mismatch"):
        raise ResponseError(f"{arxiv_id}: invalid verification method: {method}")
    refs = [normalize_ref(c) for c in refs_raw]
    status = "count_mismatch" if mismatch else ("ok" if refs else "empty_refs")
    return {
        "arxiv_id": arxiv_id,
        "s2_paper_id": entry.get("paperId"),
        "s2_reference_count": entry.get("referenceCount"),
        "status": status,
        "applies_to_versions": publication.get("versions") or [],
        "n_refs": len(refs),
        "n_refs_with_arxiv": sum(1 for r in refs if r["ref_arxiv_id"]),
        "n_refs_without_s2_id": sum(1 for r in refs if not r["ref_s2_id"]),
        "references": refs,
        "fetched_at": fetched_at,
        "verification": {"complete": complete, "method": method},
    }


# ---------------------------------------------------------------------------
# Saved-record validation for summaries and snapshot protection
# ---------------------------------------------------------------------------
def is_valid_record(
    rec: object, expected_id: str | None = None, *, allow_legacy: bool = False
) -> bool:
    """Validate a saved record, including explicitly warned count mismatches.

    Legacy records without verification require consistent schema and counts
    and must not carry an unresolved verification flag."""
    if not isinstance(rec, dict):
        return False
    aid = rec.get("arxiv_id")
    if not isinstance(aid, str) or not _BASE_ID_RE.fullmatch(aid):
        return False
    if expected_id is not None and aid != expected_id:
        return False
    for name in ("s2_paper_id", "fetched_at"):
        if rec.get(name) is not None and not isinstance(rec[name], str):
            return False
    if type(rec.get("refresh_pending", False)) is not bool or rec.get(
        "refresh_pending"
    ):
        return False
    if rec.get("status") not in ("ok", "empty_refs", "not_in_s2", "count_mismatch"):
        return False
    if not isinstance(rec.get("references"), list):
        return False
    refs = rec["references"]
    n = rec.get("n_refs")
    if type(n) is not int or n < 0 or n != len(refs):
        return False
    for ref in refs:
        if not isinstance(ref, dict):
            return False
        for key in (
            "ref_arxiv_id",
            "ref_s2_id",
            "ref_title",
            "ref_submission_date",
            "ref_venue",
            "arxiv_id_source",
        ):
            if ref.get(key) is not None and not isinstance(ref[key], str):
                return False
        authors = ref.get("ref_authors")
        if not isinstance(authors, list) or any(
            not isinstance(a, str) for a in authors
        ):
            return False
        ext = ref.get("ref_external_ids")
        if ext is not None and not isinstance(ext, dict):
            return False
        year = ref.get("ref_year")
        if year is not None and (type(year) is not int):
            return False
    for name, actual in (
        ("n_refs_with_arxiv", sum(bool(r.get("ref_arxiv_id")) for r in refs)),
        ("n_refs_without_s2_id", sum(not r.get("ref_s2_id") for r in refs)),
    ):
        if type(rec.get(name)) is not int or rec[name] != actual:
            return False
    versions = rec.get("applies_to_versions")
    if not isinstance(versions, list) or any(
        not isinstance(v, str)
        or not v.startswith(aid)
        or not _VERSION_RE.fullmatch(v[len(aid) :])
        for v in versions
    ):
        return False
    v = rec.get("verification")
    if rec["status"] == "count_mismatch":
        return (
            isinstance(v, dict)
            and v.get("complete") is False
            and v.get("method") == "paged_count_mismatch"
            and count_is_valid({"referenceCount": rec.get("s2_reference_count")})
            is True
            and rec["s2_reference_count"] != n
            and not rec.get("truncation_unverified")
        )
    if v is None:
        if not allow_legacy or rec.get("truncation_unverified"):
            return False
        if rec.get("reference_count_mismatch") and not rec.get("truncation_fixed"):
            return False
    elif not isinstance(v, dict) or v.get("complete") is not True:
        return False
    if rec["status"] == "not_in_s2":
        return (
            n == 0
            and rec.get("s2_reference_count") is None
            and rec.get("s2_paper_id") is None
            and isinstance(v, dict)
            and v.get("method") == "not_found"
        )
    if rec["status"] != ("ok" if n else "empty_refs"):
        return False
    if not count_matches({"referenceCount": rec.get("s2_reference_count")}, n):
        return False
    if v is not None and v.get("method") not in ("count_match", "paged_count_match"):
        return False
    if rec.get("truncation_unverified"):
        return False
    return True
