"""Post-crawl verification: find versions that were missed and download them again."""

from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
import logging
from typing import Callable

from .client import FastArxivClient
from .config import AUDIT_MAX_CONSECUTIVE_FAILURES
from .pipeline import (extract_paper_to_existing_folders, fetch_single_version_task, is_retryable,
                       merge_version_records, refresh_record)


logger = logging.getLogger(__name__)
RECOVERY_BATCH = 16  # versions fetched in parallel before extraction (bounds RAM)


def find_missing_versions(items, papers_dir: Path, all_status: dict) -> list[tuple[str, str, Path]]:
    """Missing = folder absent, or no usable record and attempts not yet exhausted (see MAX_VERSION_ATTEMPTS)."""
    missing = []
    for aid, v_list in items:
        p_dir = papers_dir / aid.replace(".", "-")
        v_downloads = all_status.get(aid, {}).get("version_downloads", {})
        for v_tag in v_list:
            if is_retryable(v_downloads.get(v_tag, {})) or not (p_dir / v_tag).exists():
                missing.append((aid, v_tag, p_dir))
    return missing


def _recover_one(aid: str, v_tag: str, p_dir: Path, fut, total: int, all_status: dict) -> bool:
    """Process one re-fetched version; returns True when it ended in a usable (non-error) state."""
    try:
        (p_dir / v_tag).mkdir(parents=True, exist_ok=True)  # the folder is the very thing that may be missing
        res = extract_paper_to_existing_folders(0, total, aid, p_dir, [fut])
        version_record = res["record"]["version_downloads"].get(v_tag, {})
        elapsed = res["elapsed"]
    except Exception as exc:  # one failing item must never stop the whole audit
        logger.warning('[DOWNLOAD] Recovery failed for %s%s: %s: %s', aid, v_tag, type(exc).__name__, exc)
        version_record = {"status": "error", "error": f"{type(exc).__name__}: {exc}"}
        elapsed = 0.0

    rec = all_status.setdefault(aid, {
        "arxiv_id": aid,
        "paper_status": "error",
        "total_raw_archive_bytes": 0,
        "total_uncompressed_bytes": 0,
        "total_kept_tex_bib_bytes": 0,
        "download_time_sec": 0.0,
        "version_downloads": {},
    })
    merge_version_records(rec, {v_tag: version_record})
    rec["download_time_sec"] = round(float(rec.get("download_time_sec", 0.0)) + elapsed, 4)
    refresh_record(rec)  # same status rule + byte totals as the main pipeline
    ok = version_record.get("status") != "error"
    logger.info('[DOWNLOAD] Recovery processed: %s%s -> %s.', aid, v_tag, version_record.get("status"))
    return ok


def recover_missing_versions(missing, all_status: dict, client: FastArxivClient, net_pool: ThreadPoolExecutor,
                             *, stop_event=None, checkpoint: Callable[[], None] | None = None) -> bool:
    """Re-fetch missing versions in parallel batches. Returns False when stopped or the fuse tripped."""
    consecutive_failures = 0
    for start in range(0, len(missing), RECOVERY_BATCH):
        if stop_event is not None and stop_event.is_set():
            logger.info('[DOWNLOAD] Audit stopped.')
            return False
        batch = missing[start:start + RECOVERY_BATCH]
        futures = [(aid, v_tag, p_dir, net_pool.submit(fetch_single_version_task, aid, v_tag, client))
                   for aid, v_tag, p_dir in batch]
        for aid, v_tag, p_dir, fut in futures:
            logger.info('[DOWNLOAD] Recovering %s%s.', aid, v_tag)
            ok = _recover_one(aid, v_tag, p_dir, fut, len(missing), all_status)
            consecutive_failures = 0 if ok else consecutive_failures + 1
        if checkpoint is not None:
            checkpoint()  # keep recovered work if the process dies in a later batch
        if consecutive_failures >= AUDIT_MAX_CONSECUTIVE_FAILURES:
            logger.error('[DOWNLOAD] Audit aborted: %d recoveries failed in a row (blocked or offline?). '
                         'Rerun later to resume.', consecutive_failures)
            return False
    return True


def run_audit(items, papers_dir: Path, all_status: dict, client: FastArxivClient, net_pool: ThreadPoolExecutor,
              *, stop_event=None, checkpoint: Callable[[], None] | None = None) -> None:
    logger.info('[DOWNLOAD] Auditing versions.')
    missing = find_missing_versions(items, papers_dir, all_status)
    if not missing:
        logger.info('[DOWNLOAD] Audit: no missing versions.')
        return
    logger.info('[DOWNLOAD] Audit: recovering %d missing versions.', len(missing))
    finished = recover_missing_versions(missing, all_status, client, net_pool,
                                        stop_event=stop_event, checkpoint=checkpoint)
    logger.info('[DOWNLOAD] Audit done.' if finished else '[DOWNLOAD] Audit incomplete.')
