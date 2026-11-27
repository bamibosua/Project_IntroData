"""Pass 2 resolver for ambiguous_root versions using in-RAM arXiv 00README inspection.

Scans Pass 1 M2 parsing outputs for failed `ambiguous_root` versions, fetches their
raw archives via the M1 downloader client (`FastArxivClient`), parses `00README.XXX`
or `00README.json` directly in RAM (never mutating `data/output/papers/`), and
re-parses resolved versions with `forced_root`.
"""
from __future__ import annotations

import argparse
import io
import json
from pathlib import Path, PurePosixPath
import posixpath
import re
import sys
import tarfile
from typing import Any, Callable
import zipfile
import zlib

from src.downloader.client import ArchiveTooLarge, BlockedError, FastArxivClient, _NOT_FOUND_PAYLOAD
from src.downloader.config import DEFAULT_DELAY, MAX_TAR_TOTAL_BYTES, MAX_UNCOMPRESSED_BYTES

from .models import status_reasons_from_diagnostics
from .runner import (
    _code_signature,
    _json_bytes,
    _utc_now,
    _write_atomic,
    configure_console,
    process_and_commit_version,
)

_README_NAMES = frozenset({"00readme.xxx", "00readme.json"})
_AMBIGUOUS_PREFIX = "Multiple documentclass roots: "
_RESOLUTION_REPORT_NAME = "ambiguous_readme_resolution.json"


def paper_dir_to_arxiv_id(paper_dir_name: str) -> str:
    """Convert M1 paper directory name (e.g. '2511-11713') back to arXiv ID ('2511.11713')."""
    if re.fullmatch(r"\d{4}-\d{4,5}", paper_dir_name):
        return paper_dir_name.replace("-", ".", 1)
    if "_" in paper_dir_name and re.fullmatch(r"[a-z][a-z0-9.-]*_\d{7}", paper_dir_name, re.IGNORECASE):
        return paper_dir_name.replace("_", "/", 1)
    return paper_dir_name


def find_ambiguous_versions(output_dir: Path) -> list[dict[str, Any]]:
    """Collect all Pass 1 versions that failed with `ambiguous_root`."""
    output_dir = Path(output_dir).resolve()
    items: list[dict[str, Any]] = []
    if not output_dir.is_dir():
        return items
    for pub_dir in sorted(output_dir.iterdir()):
        if not pub_dir.is_dir():
            continue
        for ver_dir in sorted(pub_dir.iterdir()):
            match = re.fullmatch(r"v([1-9]\d*)", ver_dir.name)
            if not match or not ver_dir.is_dir():
                continue
            outline_path = ver_dir / "outline.json"
            if not outline_path.is_file():
                continue
            try:
                record = json.loads(outline_path.read_text(encoding="utf-8"))
            except (OSError, ValueError, UnicodeError):
                continue
            if not isinstance(record, dict) or record.get("status") != "failed":
                continue
            ambiguous_diag = next(
                (d for d in record.get("diagnostics", []) if d.get("code") == "ambiguous_root"),
                None,
            )
            if ambiguous_diag is None:
                continue
            message = str(ambiguous_diag.get("message") or "")
            candidates: list[str] = []
            if message.startswith(_AMBIGUOUS_PREFIX):
                raw_list = message[len(_AMBIGUOUS_PREFIX):]
                candidates = [c.strip() for c in raw_list.split(",") if c.strip()]
            items.append({
                "publication_key": pub_dir.name,
                "version": int(match.group(1)),
                "version_id": f"{paper_dir_to_arxiv_id(pub_dir.name)}v{int(match.group(1))}",
                "candidate_roots": candidates,
                "outline_path": outline_path,
            })
    return items


def _bounded_gunzip(raw: bytes, limit: int = MAX_UNCOMPRESSED_BYTES) -> bytes:
    decompressor = zlib.decompressobj(wbits=31)
    out = decompressor.decompress(raw, limit + 1)
    if len(out) > limit:
        raise ValueError("gzip payload exceeds decompression limit")
    if not decompressor.eof:
        raise EOFError("truncated gzip stream")
    return out


def _pick_best_readme(found: list[tuple[str, bytes]]) -> tuple[str, bytes] | None:
    if not found:
        return None
    # Prefer shallower paths and modern 00README.json over 00README.XXX at equal depth.
    found.sort(key=lambda item: (len(PurePosixPath(item[0]).parts), 0 if item[0].lower().endswith(".json") else 1, item[0]))
    return found[0]


def extract_readme_from_archive_bytes(raw_bytes: bytes) -> tuple[str, str] | None:
    """Inspect a raw arXiv archive in RAM and return (member_name, text) for 00README.* if present."""
    if not raw_bytes or raw_bytes == _NOT_FOUND_PAYLOAD or raw_bytes.startswith(b"%PDF-"):
        return None

    def collect_from_tar(tar_obj: tarfile.TarFile, *, streamed: bool = False) -> tuple[str, bytes] | None:
        found: list[tuple[str, bytes]] = []
        total = 0
        for member in tar_obj:
            if not member.isfile():
                continue
            if streamed:
                total += max(0, member.size)
                if total > MAX_TAR_TOTAL_BYTES:
                    break
            cleaned = member.name.replace("\\", "/").lstrip("/")
            base = PurePosixPath(cleaned).name.lower()
            if base in _README_NAMES and 0 < member.size <= 1024 * 1024:
                extracted = tar_obj.extractfile(member)
                if extracted is not None:
                    found.append((cleaned, extracted.read()))
        return _pick_best_readme(found)

    picked: tuple[str, bytes] | None = None
    if raw_bytes.startswith(b"PK\x03\x04"):
        try:
            with zipfile.ZipFile(io.BytesIO(raw_bytes)) as zf:
                found: list[tuple[str, bytes]] = []
                for info in zf.infolist():
                    if info.is_dir():
                        continue
                    cleaned = info.filename.replace("\\", "/").lstrip("/")
                    base = PurePosixPath(cleaned).name.lower()
                    if base in _README_NAMES and 0 < info.file_size <= 1024 * 1024:
                        found.append((cleaned, zf.read(info.filename)))
                picked = _pick_best_readme(found)
        except (zipfile.BadZipFile, OSError, zlib.error, EOFError):
            return None
    else:
        payload = raw_bytes
        streamed_fallback = False
        if raw_bytes.startswith(b"\x1f\x8b"):
            try:
                payload = _bounded_gunzip(raw_bytes)
            except ValueError:
                streamed_fallback = True
            except (OSError, EOFError, zlib.error):
                return None
        if streamed_fallback:
            try:
                with tarfile.open(fileobj=io.BytesIO(raw_bytes), mode="r|gz") as tar:
                    picked = collect_from_tar(tar, streamed=True)
            except (tarfile.TarError, OSError, EOFError, zlib.error):
                return None
        else:
            try:
                with tarfile.open(fileobj=io.BytesIO(payload), mode="r:*") as tar:
                    picked = collect_from_tar(tar)
            except (tarfile.TarError, OSError, EOFError, zlib.error):
                return None

    if picked is None:
        return None
    name, data = picked
    try:
        text = data.decode("utf-8")
    except UnicodeDecodeError:
        text = data.decode("latin-1", errors="replace")
    return name, text


def _normalize_readme_path(raw_name: str) -> str:
    cleaned = raw_name.strip().strip('"').strip("'").replace("\\", "/").lstrip("/")
    norm = posixpath.normpath(cleaned)
    return "" if norm in ("", ".") else norm


def parse_readme_directives(member_name: str, text: str) -> tuple[set[str], set[str]]:
    """Return (toplevel_files, ignored_files) parsed from 00README.XXX or 00README.json."""
    toplevel: set[str] = set()
    ignored: set[str] = set()
    lower_name = PurePosixPath(member_name).name.lower()

    if lower_name == "00readme.json":
        try:
            payload = json.loads(text)
        except ValueError:
            return toplevel, ignored
        if isinstance(payload, dict):
            sources = payload.get("sources")
            if isinstance(sources, list):
                for entry in sources:
                    if not isinstance(entry, dict):
                        continue
                    fname = _normalize_readme_path(str(entry.get("filename") or ""))
                    if not fname:
                        continue
                    usage = str(entry.get("usage") or "").strip().lower()
                    if usage in {"toplevel", "toplevelfile"} or entry.get("toplevel") is True:
                        toplevel.add(fname)
                    if usage == "ignore" or entry.get("ignore") is True:
                        ignored.add(fname)
        return toplevel, ignored

    # Classic 00README.XXX format: "<filename> <directive>" (comments start with '#').
    for line in text.splitlines():
        cleaned = line.split("#", 1)[0].strip()
        if not cleaned:
            continue
        parts = cleaned.split()
        if len(parts) < 2:
            continue
        first_lower = parts[0].lower()
        rest_lower = {tok.lower() for tok in parts[1:]}
        if first_lower in {"toplevelfile", "toplevel"}:
            fname = _normalize_readme_path(parts[1])
            if fname:
                toplevel.add(fname)
            continue
        if first_lower == "ignore":
            fname = _normalize_readme_path(parts[1])
            if fname:
                ignored.add(fname)
            continue
        fname = _normalize_readme_path(parts[0])
        if not fname:
            continue
        if "toplevelfile" in rest_lower or "toplevel" in rest_lower:
            toplevel.add(fname)
        if "ignore" in rest_lower:
            ignored.add(fname)
    return toplevel, ignored


def select_root_from_readme(
    candidate_roots: list[str],
    toplevel_files: set[str],
    ignored_files: set[str],
) -> str | None:
    """Resolve a unique candidate root from parsed 00README directives."""
    if not candidate_roots:
        return None
    norm_candidates = [(cand, _normalize_readme_path(cand)) for cand in candidate_roots]

    def match_set(directives: set[str]) -> set[str]:
        matched: set[str] = set()
        dir_exact = {_normalize_readme_path(d) for d in directives if d}
        dir_lower = {d.casefold() for d in dir_exact}
        dir_basenames = {PurePosixPath(d).name.casefold() for d in dir_exact if "/" not in d}
        for orig, norm in norm_candidates:
            if norm in dir_exact or norm.casefold() in dir_lower:
                matched.add(orig)
        if not matched and dir_basenames:
            by_base = [
                orig for orig, norm in norm_candidates
                if PurePosixPath(norm).name.casefold() in dir_basenames
            ]
            if len(by_base) == 1:
                matched.add(by_base[0])
        return matched

    ignored_matched = match_set(ignored_files)
    remaining = [cand for cand in candidate_roots if cand not in ignored_matched]
    if not remaining:
        return None

    toplevel_matched = [cand for cand in remaining if cand in match_set(toplevel_files)]
    if len(toplevel_matched) == 1:
        return toplevel_matched[0]
    if len(remaining) == 1:
        return remaining[0]
    return None


def _apply_summary_delta(
    summary: dict[str, Any],
    old_record: dict[str, Any],
    new_record: dict[str, Any],
    old_bytes: int,
    new_bytes: int,
) -> None:
    """Update Pass 1 summary.json counters in-place when one version is re-parsed."""
    old_status = old_record.get("status")
    new_status = new_record.get("status")
    status_counts = summary.get("status_counts")
    if isinstance(status_counts, dict):
        if old_status in status_counts and status_counts[old_status] > 0:
            status_counts[old_status] -= 1
        if new_status in status_counts:
            status_counts[new_status] += 1

    reason_counts = summary.get("status_reason_counts")
    if isinstance(reason_counts, dict):
        old_reasons = old_record.get("status_reasons")
        if not isinstance(old_reasons, list):
            old_reasons = status_reasons_from_diagnostics(str(old_status or ""), old_record.get("diagnostics", []))
        new_reasons = new_record.get("status_reasons")
        if not isinstance(new_reasons, list):
            new_reasons = status_reasons_from_diagnostics(str(new_status or ""), new_record.get("diagnostics", []))
        if old_status in reason_counts and isinstance(reason_counts[old_status], dict):
            for reason in old_reasons:
                if reason in reason_counts[old_status]:
                    reason_counts[old_status][reason] -= 1
                    if reason_counts[old_status][reason] <= 0:
                        del reason_counts[old_status][reason]
        if new_status in reason_counts and isinstance(reason_counts[new_status], dict):
            for reason in new_reasons:
                reason_counts[new_status][reason] = reason_counts[new_status].get(reason, 0) + 1

    diag_counts = summary.get("diagnostic_counts")
    diag_ver_counts = summary.get("diagnostic_version_counts")
    if isinstance(diag_counts, dict) and isinstance(diag_ver_counts, dict):
        for diag in old_record.get("diagnostics", []):
            code = diag.get("code")
            if code in diag_counts:
                diag_counts[code] -= 1
                if diag_counts[code] <= 0:
                    del diag_counts[code]
        for code in {d.get("code") for d in old_record.get("diagnostics", []) if d.get("code")}:
            if code in diag_ver_counts:
                diag_ver_counts[code] -= 1
                if diag_ver_counts[code] <= 0:
                    del diag_ver_counts[code]
        for diag in new_record.get("diagnostics", []):
            code = diag.get("code")
            if code:
                diag_counts[code] = diag_counts.get(code, 0) + 1
        for code in {d.get("code") for d in new_record.get("diagnostics", []) if d.get("code")}:
            diag_ver_counts[code] = diag_ver_counts.get(code, 0) + 1

    def record_metrics(rec: dict[str, Any]) -> tuple[int, int, int, int, int]:
        nodes = rec.get("nodes", [])
        smap = rec.get("source_map", [])
        return (
            len(nodes),
            sum(n.get("kind") == "heading" for n in nodes),
            len(rec.get("body_segments", [])),
            len(rec.get("bibliography_regions", [])),
            smap[-1]["merged_span"][1] if smap else 0,
        )

    totals = summary.get("totals")
    if isinstance(totals, dict):
        old_m = record_metrics(old_record)
        new_m = record_metrics(new_record)
        for key, old_val, new_val in zip(
            ("nodes", "headings", "body_segments", "bibliography_regions", "merged_characters"),
            old_m,
            new_m,
        ):
            totals[key] = max(0, int(totals.get(key, 0)) - old_val + new_val)
        totals["artifact_bytes"] = max(0, int(totals.get("artifact_bytes", 0)) - old_bytes + new_bytes)

    heading_counts = summary.get("heading_counts")
    if isinstance(heading_counts, dict):
        for node in old_record.get("nodes", []):
            if node.get("kind") == "heading":
                cmd = node.get("heading_command")
                if cmd in heading_counts:
                    heading_counts[cmd] -= 1
                    if heading_counts[cmd] <= 0:
                        del heading_counts[cmd]
        for node in new_record.get("nodes", []):
            if node.get("kind") == "heading":
                cmd = node.get("heading_command")
                if cmd:
                    heading_counts[cmd] = heading_counts.get(cmd, 0) + 1

    total_seen = summary.get("versions_seen", 0)
    if total_seen and isinstance(status_counts, dict):
        summary["status_rates"] = {k: v / total_seen for k, v in status_counts.items()}
    summary["finished_at"] = _utc_now()


def resolve_ambiguous_versions(
    input_dir: Path,
    output_dir: Path,
    *,
    delay: float = DEFAULT_DELAY,
    retry_checked: bool = False,
    limit: int | None = None,
    fetch_fn: Callable[[str], bytes] | None = None,
) -> dict[str, Any]:
    """Run Pass 2 00README resolution for all failed `ambiguous_root` versions in output_dir."""
    input_dir = Path(input_dir).resolve()
    output_dir = Path(output_dir).resolve()
    report_path = output_dir / _RESOLUTION_REPORT_NAME

    previous_records: dict[str, dict[str, Any]] = {}
    if report_path.is_file():
        try:
            prev_payload = json.loads(report_path.read_text(encoding="utf-8"))
            if isinstance(prev_payload, dict) and isinstance(prev_payload.get("versions"), dict):
                previous_records = prev_payload["versions"]
        except (OSError, ValueError, UnicodeError):
            previous_records = {}

    ambiguous_items = find_ambiguous_versions(output_dir)
    if limit is not None:
        ambiguous_items = ambiguous_items[:limit]

    client = FastArxivClient(delay=delay) if fetch_fn is None else None
    downloader = fetch_fn if fetch_fn is not None else client.fetch_version
    code = _code_signature()

    summary_path = output_dir / "summary.json"
    summary_data: dict[str, Any] | None = None
    if summary_path.is_file():
        try:
            loaded = json.loads(summary_path.read_text(encoding="utf-8"))
            if isinstance(loaded, dict):
                summary_data = loaded
        except (OSError, ValueError, UnicodeError):
            summary_data = None

    counts = {
        "scanned": len(ambiguous_items),
        "skipped_previously_checked": 0,
        "resolved": 0,
        "no_readme_in_archive": 0,
        "still_ambiguous": 0,
        "error": 0,
    }
    summary_dirty = False

    for item in ambiguous_items:
        pub = item["publication_key"]
        ver = item["version"]
        ver_key = f"{pub}/v{ver}"
        prev = previous_records.get(ver_key)
        if (
            not retry_checked
            and prev is not None
            and prev.get("outcome") in {"no_readme_in_archive", "still_ambiguous", "resolved"}
        ):
            counts["skipped_previously_checked"] += 1
            continue

        version_id = item["version_id"]
        candidates = item["candidate_roots"]
        source_dir = input_dir / pub / f"v{ver}"
        dest_dir = output_dir / pub / f"v{ver}"

        try:
            raw_bytes = downloader(version_id)
        except (BlockedError, ArchiveTooLarge, RuntimeError, OSError) as exc:
            counts["error"] += 1
            previous_records[ver_key] = {
                "version_id": version_id,
                "outcome": "error",
                "error": f"{type(exc).__name__}: {exc}",
                "checked_at": _utc_now(),
            }
            if isinstance(exc, BlockedError):
                break
            continue

        extracted = extract_readme_from_archive_bytes(raw_bytes)
        if extracted is None:
            counts["no_readme_in_archive"] += 1
            previous_records[ver_key] = {
                "version_id": version_id,
                "outcome": "no_readme_in_archive",
                "candidate_roots": candidates,
                "checked_at": _utc_now(),
            }
            continue

        member_name, readme_text = extracted
        toplevel, ignored = parse_readme_directives(member_name, readme_text)
        chosen = select_root_from_readme(candidates, toplevel, ignored)
        if chosen is None:
            counts["still_ambiguous"] += 1
            previous_records[ver_key] = {
                "version_id": version_id,
                "outcome": "still_ambiguous",
                "readme_member": member_name,
                "toplevel_directives": sorted(toplevel),
                "ignore_directives": sorted(ignored),
                "candidate_roots": candidates,
                "checked_at": _utc_now(),
            }
            continue

        old_record = json.loads((dest_dir / "outline.json").read_text(encoding="utf-8"))
        old_bytes = sum(
            (dest_dir / name).stat().st_size
            for name in ("outline.json", "merged.tex")
            if (dest_dir / name).is_file()
        )
        new_record = process_and_commit_version(
            source_dir,
            dest_dir,
            code,
            forced_root=chosen,
            forced_root_reason="resolved_root_by_readme",
        )
        new_bytes = sum(
            (dest_dir / name).stat().st_size
            for name in ("outline.json", "merged.tex")
            if (dest_dir / name).is_file()
        )
        if summary_data is not None:
            _apply_summary_delta(summary_data, old_record, new_record, old_bytes, new_bytes)
            summary_dirty = True

        counts["resolved"] += 1
        previous_records[ver_key] = {
            "version_id": version_id,
            "outcome": "resolved",
            "readme_member": member_name,
            "resolved_root": chosen,
            "new_status": new_record["status"],
            "checked_at": _utc_now(),
        }

    if summary_dirty and summary_data is not None:
        _write_atomic(summary_path, _json_bytes(summary_data))

    report = {
        "updated_at": _utc_now(),
        "input_dir": str(input_dir),
        "output_dir": str(output_dir),
        "counts": counts,
        "versions": dict(sorted(previous_records.items())),
    }
    _write_atomic(report_path, _json_bytes(report))
    return report


def main(argv: list[str] | None = None) -> int:
    configure_console()
    parser = argparse.ArgumentParser(
        description="Pass 2: resolve remaining ambiguous_root versions using in-RAM arXiv 00README."
    )
    parser.add_argument("--input-dir", type=Path, default=Path("data/output/papers"))
    parser.add_argument("--output-dir", type=Path, default=Path("data/output/parsed_structure"))
    parser.add_argument("--delay", type=float, default=DEFAULT_DELAY)
    parser.add_argument("--retry-checked", action="store_true")
    parser.add_argument("--limit", type=int, default=None)
    args = parser.parse_args(argv)

    report = resolve_ambiguous_versions(
        args.input_dir,
        args.output_dir,
        delay=args.delay,
        retry_checked=args.retry_checked,
        limit=args.limit,
    )
    c = report["counts"]
    print(
        f"[AMBIGUOUS-PASS2] scanned={c['scanned']} resolved={c['resolved']} "
        f"no_readme={c['no_readme_in_archive']} still_ambiguous={c['still_ambiguous']} "
        f"skipped={c['skipped_previously_checked']} error={c['error']}"
    )
    return 0 if c["error"] == 0 else 1


if __name__ == "__main__":
    raise SystemExit(main())
