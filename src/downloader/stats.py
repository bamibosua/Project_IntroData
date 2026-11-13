"""Corpus-wide storage metrics, summary file and console report."""

from __future__ import annotations

from datetime import datetime, timezone
import json
from pathlib import Path

from .utils import format_bytes, format_duration


def compute_storage_metrics(all_status: dict) -> dict:
    """Aggregate sizes and file counts over every status record."""
    raw = uncomp = kept = tex = bib = versions = 0
    for rec in all_status.values():
        raw += int(rec.get("total_raw_archive_bytes", 0))
        uncomp += int(rec.get("total_uncompressed_bytes", 0))
        kept += int(rec.get("total_kept_tex_bib_bytes", 0))
        for v in rec.get("version_downloads", {}).values():
            versions += 1
            tex += int(v.get("tex_file_count", 0))
            bib += int(v.get("bib_file_count", 0))

    saved = uncomp - kept
    return {
        "total_versions": versions,
        "total_raw_archive_bytes": raw,
        "total_raw_archive_human": format_bytes(raw),
        "total_uncompressed_bytes": uncomp,
        "total_uncompressed_human": format_bytes(uncomp),
        "total_kept_tex_bib_bytes": kept,
        "total_kept_tex_bib_human": format_bytes(kept),
        "total_saved_bytes_vs_uncompressed": saved,
        "total_saved_human_vs_uncompressed": format_bytes(saved),
        "storage_reduction_ratio_vs_uncompressed_pct": round(saved / uncomp * 100, 2) if uncomp > 0 else 0.0,
        "storage_reduction_ratio_vs_raw_archive_pct": round((raw - kept) / raw * 100, 2) if raw > 0 else 0.0,
        "total_tex_files": tex,
        "total_bib_files": bib,
        "total_kept_files": tex + bib,
    }


def write_summary(
    summary_path: Path,
    counters: dict,
    metrics: dict,
    wall_sec: float,
    peak_heap_mb: float,
    paths: dict,
    *,
    prev_wall_sec: float = 0.0,
    session_wall_sec: float | None = None,
    first_started_local: str | None = None,
    first_started_utc: str | None = None,
    dt_start: datetime | None = None,
    dt_end: datetime | None = None,
    run_state: str = "completed",
    exit_code=None,
    reason=None,
) -> dict:
    dt_now = dt_end or datetime.now()
    dt_beg = dt_start or dt_now
    sess_sec = wall_sec if session_wall_sec is None else session_wall_sec
    summary = {
        "run_state": run_state,
        **counters,
        "first_started_at_local": first_started_local or dt_beg.strftime("%Y-%m-%d %H:%M:%S"),
        "session_started_at_local": dt_beg.strftime("%Y-%m-%d %H:%M:%S"),
        "completed_at_local": dt_now.strftime("%Y-%m-%d %H:%M:%S"),
        "first_started_at_utc": first_started_utc or dt_beg.astimezone(timezone.utc).isoformat(),
        "started_at_utc": dt_beg.astimezone(timezone.utc).isoformat(),
        "completed_at_utc": dt_now.astimezone(timezone.utc).isoformat(),
        "previous_wall_time_sec": prev_wall_sec,
        "session_wall_time_sec": sess_sec,
        "total_wall_time_sec": wall_sec,
        "total_wall_time_human": format_duration(wall_sec),
        "tracemalloc_peak_heap_mb": peak_heap_mb,
        "storage_metrics": metrics,
        "output_paths": {k: str(v) for k, v in paths.items()},
    }
    if exit_code is not None:
        summary.update(exit_code=exit_code, reason=reason)
    summary_path.parent.mkdir(parents=True, exist_ok=True)
    summary_path.write_text(json.dumps(summary, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
    return summary


def print_report(
    counters: dict,
    m: dict,
    wall_sec: float,
    peak_heap_mb: float,
    summary_path: Path,
    *,
    prev_wall_sec: float = 0.0,
    session_wall_sec: float | None = None,
    first_started_local: str | None = None,
    dt_start: datetime | None = None,
    dt_end: datetime | None = None,
    run_state: str = "completed",
) -> None:
    dt_now = dt_end or datetime.now()
    dt_beg = dt_start or dt_now
    sess_sec = wall_sec if session_wall_sec is None else session_wall_sec
    bar = "=" * 56
    title = "DOWNLOAD CORPUS SUMMARY" if run_state == "completed" else f"DOWNLOAD SUMMARY ({run_state.upper()})"
    print(f"\n{bar}\n{title.center(56)}\n{bar}")
    print(f" Status               : {run_state.upper()}")
    print(f" First started at     : {first_started_local or dt_beg.strftime('%Y-%m-%d %H:%M:%S')}")
    print(f" Session started at   : {dt_beg.strftime('%Y-%m-%d %H:%M:%S')}")
    print(f" Saved / Ended at     : {dt_now.strftime('%Y-%m-%d %H:%M:%S')}")
    print(f" Previous runtime     : {format_duration(prev_wall_sec)}")
    print(f" Session runtime      : {format_duration(sess_sec)}")
    print(f" Total runtime (cum.) : {format_duration(wall_sec)}")
    print("-" * 56)
    print(f" Papers processed     : {counters.get('processed_papers', counters['total_papers']):,} / {counters['total_papers']:,} (Cached: {counters['cached_papers']:,})")
    print(f" Papers with TeX/Bib  : {counters['papers_with_tex']:,}")
    print(f" Papers PDF-only      : {counters['papers_without_tex']:,}")
    print(f" Papers failed        : {counters['papers_failed']:,}")
    print(f" Total versions       : {m['total_versions']:,}")
    print(f" Total TeX files      : {m['total_tex_files']:,}")
    print(f" Total Bib files      : {m['total_bib_files']:,}")
    print("-" * 56)
    print(f" Raw arXiv archive    : {m['total_raw_archive_human']} ({m['total_raw_archive_bytes']:,} bytes)")
    print(f" Total uncompressed   : {m['total_uncompressed_human']} ({m['total_uncompressed_bytes']:,} bytes)")
    print(f" Kept TeX/Bib on disk : {m['total_kept_tex_bib_human']} ({m['total_kept_tex_bib_bytes']:,} bytes)")
    print(f" Disk space saved     : {m['total_saved_human_vs_uncompressed']} ({m['total_saved_bytes_vs_uncompressed']:,} bytes)")
    print(f" Storage reduction    : {m['storage_reduction_ratio_vs_uncompressed_pct']:.2f}% (vs uncompressed)")
    print(f"                      : {m['storage_reduction_ratio_vs_raw_archive_pct']:.2f}% (vs raw archive)")
    print(f" Peak heap            : {peak_heap_mb} MB")
    print(f"{bar}\nSummary written to {summary_path}\n", flush=True)
