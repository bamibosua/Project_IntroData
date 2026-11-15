"""Per-paper work units: fetch every version, then extract .tex/.bib into the version folders."""

from __future__ import annotations

from concurrent.futures import Future
from concurrent.futures import TimeoutError as FuturesTimeoutError
import logging
from pathlib import Path
import time

from .client import FastArxivClient
from .config import MAX_VERSION_ATTEMPTS, PERMANENT_ERROR_TYPES, SCHEMA_VERSION
from .extractor import extract_tex_and_bib_from_bytes

logger = logging.getLogger(__name__)

OK_STATUSES = ("success", "no_tex_source")


def is_exhausted(vrec: dict) -> bool:
    """A permanently broken archive (corrupt / encrypted / too large) that was already fetched often enough:
    counted as error, never fetched again. Transient errors (network, HTTP 5xx, 403 ...) never exhaust."""
    return (vrec.get("status") == "error" and vrec.get("source_type") in PERMANENT_ERROR_TYPES
            and int(vrec.get("attempts", 0)) >= MAX_VERSION_ATTEMPTS)


def is_retryable(version_record: dict) -> bool:
    """A version needs (another) download unless it is done or permanently exhausted."""
    return version_record.get("status") not in OK_STATUSES and not is_exhausted(version_record)


def merge_version_records(record: dict, new_versions: dict[str, dict]) -> None:
    """Merge fresh per-version records into a paper record and count failed attempts.

    Permanent failures (see PERMANENT_ERROR_TYPES) stop being retried after MAX_VERSION_ATTEMPTS; the counter
    must survive the merge, which is why it lives here and not only in the audit.
    An interrupted fetch (Ctrl+C / stop) is not the version's fault and does not count.
    """
    old = record["version_downloads"]
    for tag, info in new_versions.items():
        if info.get("status") == "error":
            previous = int(old.get(tag, {}).get("attempts", 0))
            interrupted = "InterruptedError" in str(info.get("error", ""))
            info["attempts"] = previous if interrupted else previous + 1
        old[tag] = info


def fetch_single_version_task(arxiv_id: str, ver_tag: str, client: FastArxivClient):
    """Network task. Returns (ver_tag, version_id, holder, error, fetch_sec).

    `holder` is a one-element list owning the raw bytes: the consumer `pop()`s it so the Future no longer
    pins the archive in RAM after extraction (a plain `del` of a local name would not release it).
    """
    full_vid = f"{arxiv_id}v{ver_tag.lstrip('v')}"
    t0 = time.perf_counter()
    try:
        data = client.fetch_version(full_vid)
        return ver_tag, full_vid, [data], None, round(time.perf_counter() - t0, 4)
    except Exception as exc:
        return ver_tag, full_vid, [], f"{type(exc).__name__}: {exc}", round(time.perf_counter() - t0, 4)


def interruptible_result(fut: Future):
    """fut.result() with a timeout loop: on Windows a plain blocking wait cannot be interrupted by Ctrl+C."""
    while True:
        try:
            return fut.result(timeout=1.0)
        except FuturesTimeoutError:
            continue


def compute_paper_status(version_downloads: dict[str, dict], expected: list[str] | None = None) -> str:
    """Single source of truth: a failed or missing expected version -> error; else success if any TeX."""
    if expected:
        version_downloads = {v: version_downloads.get(v, {}) for v in expected}
    statuses = [v.get("status") for v in version_downloads.values()]
    if not statuses or any(s not in OK_STATUSES for s in statuses):
        return "error"
    return "success" if "success" in statuses else "no_tex_source"


def refresh_record(record: dict) -> None:
    """Recompute paper_status and byte totals from the per-version records."""
    versions = record["version_downloads"].values()
    record["schema_version"] = SCHEMA_VERSION
    record["paper_status"] = compute_paper_status(record["version_downloads"], record.get("versions_expected"))
    for total, field in (("total_raw_archive_bytes", "raw_archive_bytes"),
                         ("total_uncompressed_bytes", "uncompressed_total_bytes"),
                         ("total_kept_tex_bib_bytes", "kept_tex_bib_bytes")):
        record[total] = sum(int(v.get(field, 0)) for v in versions)


def extract_paper_to_existing_folders(
    idx: int,
    total: int,
    arxiv_id: str,
    paper_dir: Path,
    version_futures: list[Future],
    paper_t0: float | None = None,  # kept for backward compatibility; timing now comes from the fetch tasks
) -> dict:
    """Extract .tex/.bib into data/output/papers/<YYMM-NNNNN>/<vN>/ and build the (partial) paper record.

    `download_time_sec` is the time spent inside the fetch tasks (network + rate-limit waits), without the
    time a task spent queued behind others.
    """
    v_status: dict[str, dict] = {}
    peak_buf = 0
    fetch_total = 0.0

    for fut in version_futures:
        ver_tag, full_vid, holder, err, fetch_sec = interruptible_result(fut)
        fetch_total += fetch_sec
        if err is not None:
            v_status[ver_tag] = {"status": "error", "error": err}
            continue
        raw_b = holder.pop()  # the Future's result no longer references the bytes
        peak_buf = max(peak_buf, len(raw_b))
        try:
            info = extract_tex_and_bib_from_bytes(raw_b, dest_dir=paper_dir / ver_tag)
        except Exception as exc:  # one bad version must not crash the whole run
            v_status[ver_tag] = {"status": "error", "error": f"{type(exc).__name__}: {exc}"}
            continue
        finally:
            del raw_b
        info["version_id"] = full_vid
        v_status[ver_tag] = info
        if info.get("status") == "no_tex_source" and info.get("source_type") != "not_found_404":
            st = info.get("source_type")
            if st == "pdf_only":
                v_detail = "PDF-only"
            elif st in ("tar_without_tex", "zip_without_tex"):
                v_detail = "archive without TeX"
            else:
                v_detail = "no TeX source"
            logger.info("[DOWNLOAD] %s: %s", full_vid, v_detail)

    elapsed = round(fetch_total, 4)
    record = {
        "arxiv_id": arxiv_id,
        "paper_status": "error",
        "total_raw_archive_bytes": 0,
        "total_uncompressed_bytes": 0,
        "total_kept_tex_bib_bytes": 0,
        "download_time_sec": elapsed,
        "version_downloads": v_status,
    }
    refresh_record(record)
    return {
        "idx": idx,
        "total": total,
        "arxiv_id": arxiv_id,
        "ver_count": len(version_futures),
        "status": record["paper_status"],
        "elapsed": elapsed,
        "peak_buf": peak_buf,
        "record": record,
    }