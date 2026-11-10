"""Per-paper work units: fetch every version, then extract .tex/.bib into the version folders."""

from __future__ import annotations

from concurrent.futures import Future
from concurrent.futures import TimeoutError as FuturesTimeoutError
from pathlib import Path
import time

from .client import FastArxivClient
from .extractor import extract_tex_and_bib_from_bytes


def fetch_single_version_task(arxiv_id: str, ver_tag: str, client: FastArxivClient):
    """Network task. Returns (ver_tag, version_id, raw_bytes | None, error | None)."""
    full_vid = f"{arxiv_id}v{ver_tag.lstrip('v')}"
    try:
        return ver_tag, full_vid, client.fetch_version(full_vid), None
    except Exception as exc:
        return ver_tag, full_vid, None, str(exc)


def interruptible_result(fut: Future):
    """fut.result() with a timeout loop: on Windows a plain blocking wait cannot be interrupted by Ctrl+C."""
    while True:
        try:
            return fut.result(timeout=1.0)
        except FuturesTimeoutError:
            continue


def extract_paper_to_existing_folders(
    idx: int,
    total: int,
    arxiv_id: str,
    paper_dir: Path,
    version_futures: list[Future],
    paper_t0: float,
) -> dict:
    """Extract .tex/.bib into data/output/papers/<YYMM-NNNNN>/<vN>/ and build the paper record."""
    v_status: dict[str, dict] = {}
    has_tex = had_error = False
    peak_buf = 0

    for fut in version_futures:
        ver_tag, full_vid, raw_b, err = interruptible_result(fut)
        if err is not None:
            had_error = True
            v_status[ver_tag] = {"status": "error", "error": err}
            continue
        peak_buf = max(peak_buf, len(raw_b))
        info = extract_tex_and_bib_from_bytes(raw_b, dest_dir=paper_dir / ver_tag)
        del raw_b
        info["version_id"] = full_vid
        v_status[ver_tag] = info
        has_tex = has_tex or info["status"] == "success"

    elapsed = round(time.perf_counter() - paper_t0, 4)
    p_status = "error" if had_error else ("success" if has_tex else "no_tex_source")
    record = {
        "arxiv_id": arxiv_id,
        "paper_status": p_status,
        "total_raw_archive_bytes": sum(int(v.get("raw_archive_bytes", 0)) for v in v_status.values()),
        "total_uncompressed_bytes": sum(int(v.get("uncompressed_total_bytes", 0)) for v in v_status.values()),
        "total_kept_tex_bib_bytes": sum(int(v.get("kept_tex_bib_bytes", 0)) for v in v_status.values()),
        "download_time_sec": elapsed,
        "version_downloads": v_status,
    }
    return {
        "idx": idx,
        "total": total,
        "arxiv_id": arxiv_id,
        "ver_count": len(version_futures),
        "status": p_status,
        "elapsed": elapsed,
        "peak_buf": peak_buf,
        "record": record,
    }
