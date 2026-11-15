"""Orchestration: download every paper of the index, checkpoint, audit, summarize.

`download_corpus` (CLI / batch) and `download_stream` (used by run_m1 while entry is still producing papers)
are thin wrappers around the single `_engine`, so resume, checkpoint, audit and summary logic exist once.
"""

from __future__ import annotations

from concurrent.futures import FIRST_COMPLETED, ThreadPoolExecutor, wait
from datetime import datetime, timezone
import json
import logging
import os
from pathlib import Path
import signal
import threading
import time

from .audit import run_audit
from .client import FastArxivClient
from .config import (CHECKPOINT_EVERY, DEFAULT_CONNECTIONS, DEFAULT_DELAY, DEFAULT_OUTPUT_DIR,
                     DOWNLOAD_META_DIR_NAME, MAX_BLOCKED_STREAK)
from .pipeline import (OK_STATUSES, extract_paper_to_existing_folders, fetch_single_version_task, is_retryable,
                       merge_version_records, refresh_record)
from .stats import compute_storage_metrics, print_report, write_summary
from .utils import peak_memory_mb

logger = logging.getLogger(__name__)


def _save_status(status_path: Path, all_status: dict) -> None:
    """Atomic + compact write: a crash mid-write can no longer wipe the checkpoint."""
    status_path.parent.mkdir(parents=True, exist_ok=True)
    tmp = status_path.with_name(status_path.name + ".tmp")
    tmp.write_text(json.dumps(all_status, ensure_ascii=False, separators=(",", ":")) + "\n", encoding="utf-8")
    os.replace(tmp, status_path)


def _load_status(status_path: Path, legacy_dir: Path | None = None) -> dict:
    """Load a checkpoint. A corrupt file is moved aside (never silently treated as empty)."""
    for candidate in (status_path, (legacy_dir / status_path.name) if legacy_dir else None):
        if candidate is None or not candidate.exists():
            continue
        try:
            return json.loads(candidate.read_text(encoding="utf-8"))
        except Exception as exc:
            aside = candidate.with_name(candidate.name + f".corrupt-{int(time.time())}")
            logger.error('[DOWNLOAD] Unreadable checkpoint %s (%s: %s); moved to %s.',
                         candidate, type(exc).__name__, exc, aside.name)
            try:
                os.replace(candidate, aside)
            except OSError:
                pass
    return {}


def _require_prepared_folder(path: Path) -> None:
    if not path.is_dir():
        message = f'Missing prepared folder: {path}. Run entry first.'
        logger.error('[DOWNLOAD] %s', message)
        raise FileNotFoundError(message)


def _may_need_bbl(version_dir: Path) -> bool:
    """Disk heuristic for records written before .bbl support: TeX present, but no .bib, no .bbl and no
    \\bibitem anywhere, i.e. the bibliography may have been left behind in the archive."""
    tex = list(version_dir.rglob("*.tex"))
    if not tex or any(version_dir.rglob("*.bib")) or any(version_dir.rglob("*.bbl")):
        return False
    return not any(b"\\bibitem" in p.read_bytes() for p in tex)


def _todo_versions(prev: dict | None, versions: list[str], paper_dir: Path | None = None) -> list[str]:
    """Versions that still need work (per-version resume): not done and not out of attempts, or a legacy
    success record (no `bbl_file_count`) that may have lost its only bibliography. A legacy record that does
    not need the .bbl is stamped `bbl_file_count = 0` so it is not scanned again."""
    done = (prev or {}).get("version_downloads", {})
    todo = []
    for v in versions:
        rec = done.get(v, {})
        if is_retryable(rec):
            todo.append(v)
        elif rec.get("status") == "success" and "bbl_file_count" not in rec and paper_dir is not None:
            if _may_need_bbl(paper_dir / v):
                todo.append(v)
            else:
                rec["bbl_file_count"] = 0
    return todo


class _Interrupt(KeyboardInterrupt):
    """Raised by the SIGTERM handler so that the same cleanup path as Ctrl+C runs."""


def _engine(source, *, mode: str, total: int, output_dir, workers: int, delay: float, connections: int,
            stop_event, client, summary_name: str, skip_audit: bool, contact_email, show_report: bool) -> dict:
    output_dir = Path(output_dir)
    _require_prepared_folder(output_dir)
    papers_dir = output_dir / "papers"
    meta_dir = output_dir / DOWNLOAD_META_DIR_NAME
    meta_dir.mkdir(parents=True, exist_ok=True)
    status_path = meta_dir / "download_status.json"
    summary_path = meta_dir / summary_name

    all_status = _load_status(status_path, legacy_dir=output_dir)
    prev_summary = _load_status(summary_path, legacy_dir=output_dir)

    dt_start = datetime.now().astimezone()
    t_start = time.perf_counter()
    prev_wall_sec = float(prev_summary.get("total_wall_time_sec", 0.0))
    first_started_local = (prev_summary.get("first_started_at_local") or prev_summary.get("started_at_local")
                           or dt_start.strftime("%Y-%m-%d %H:%M:%S"))
    first_started_utc = (prev_summary.get("first_started_at_utc") or prev_summary.get("started_at_utc")
                         or dt_start.astimezone(timezone.utc).isoformat())
    prev_peak_mb = float(prev_summary.get("peak_rss_mb", 0.0))

    stop = stop_event if stop_event is not None else threading.Event()
    if client is None:
        client = FastArxivClient(delay=delay, stop_event=stop, contact_email=contact_email)

    items: dict[str, list[str]] = {}
    done_ids: set[str] = set()
    running: set = set()
    cached = completed = 0
    blocked_streak = 0
    blocked = False
    fatal: BaseException | None = None
    window = max(workers * 2, connections)
    paths_dict = {"papers_dir": papers_dir, "download_status_json": status_path, "download_summary_json": summary_path}

    def stopped() -> bool:
        return stop.is_set()

    def flush(run_state: str, report: bool = False, exit_code=None, reason=None) -> dict:
        _save_status(status_path, all_status)
        selected = {aid: all_status[aid] for aid in done_ids if aid in all_status}
        n_tex = sum(r.get("paper_status") == "success" for r in selected.values())
        n_notex = sum(r.get("paper_status") == "no_tex_source" for r in selected.values())
        counters = {
            "total_papers": total or len(items),
            "processed_papers": len(selected),
            "cached_papers": cached,
            "papers_with_tex": n_tex,
            "papers_without_tex": n_notex,
            "papers_failed": len(selected) - n_tex - n_notex,
        }
        metrics = compute_storage_metrics(selected)
        dt_now = datetime.now().astimezone()
        session_sec = round(time.perf_counter() - t_start, 4)
        total_sec = round(prev_wall_sec + session_sec, 4)
        peak = max(prev_peak_mb, peak_memory_mb())
        summary = write_summary(
            summary_path, counters, metrics, total_sec, peak, paths_dict,
            prev_wall_sec=prev_wall_sec, session_wall_sec=session_sec,
            first_started_local=first_started_local, first_started_utc=first_started_utc,
            dt_start=dt_start, dt_end=dt_now, run_state=run_state, exit_code=exit_code, reason=reason)
        if report:
            print_report(counters, metrics, total_sec, peak, summary_path, prev_wall_sec=prev_wall_sec,
                         session_wall_sec=session_sec, first_started_local=first_started_local,
                         dt_start=dt_start, dt_end=dt_now, run_state=run_state)
        return summary

    def finish_paper(aid: str, status: str, res: dict | None = None) -> None:
        nonlocal completed
        done_ids.add(aid)
        completed += 1
        if res is not None:
            n = res["ver_count"]
            if mode == "corpus":
                print(f"[{res['idx']}/{res['total']}] {aid} ({n}v) -> {status} | {res['elapsed']:.2f}s", flush=True)
            elif status == "error":
                logger.warning("[DOWNLOAD] %s: incomplete; see download_status.json.", aid)
        elif mode == "corpus" and (cached <= 5 or cached % 2000 == 0):
            print(f"[{len(items)}/{total}] {aid} ({len(items[aid])}v): cached", flush=True)
        if mode == "stream" and completed % 50 == 0:
            logger.info("[DOWNLOAD] Processed: %d.", completed)
        if completed % CHECKPOINT_EVERY == 0:
            flush("in_progress")

    def collect(timeout: float = 1.0) -> None:
        nonlocal running, blocked_streak, blocked
        if not running:
            return
        done, running = wait(running, timeout=timeout, return_when=FIRST_COMPLETED)
        for fut in done:
            try:
                res = fut.result()
            except Exception as exc:  # e.g. cancelled during shutdown; the paper stays pending for the next run
                logger.warning("[DOWNLOAD] Task dropped: %s: %s", type(exc).__name__, exc)
                continue
            aid = res["arxiv_id"]
            rec = all_status[aid]
            new_versions = res["record"]["version_downloads"]
            merge_version_records(rec, new_versions)  # keeps earlier good versions, counts failed attempts
            rec["download_time_sec"] = round(float(rec.get("download_time_sec", 0.0)) + res["elapsed"], 4)
            rec["versions_expected"] = items[aid]
            refresh_record(rec)
            # Circuit breaker: repeated HTTP 403 means the server is blocking us; stop instead of hammering it.
            reached = any(v.get("status") in OK_STATUSES for v in new_versions.values())
            if reached:
                blocked_streak = 0
            elif any("BlockedError" in str(v.get("error", "")) for v in new_versions.values()):
                blocked_streak += 1
                if blocked_streak >= MAX_BLOCKED_STREAK and not blocked:
                    blocked = True
                    logger.error("[DOWNLOAD] %d papers in a row got HTTP 403: stopping (blocked?). Rerun later.",
                                 blocked_streak)
                    stop.set()
            finish_paper(aid, rec["paper_status"], res)

    # Ctrl+C / SIGTERM -> checkpoint + summary through the normal cleanup path (corpus mode owns the signals).
    old_handlers: dict = {}
    if mode == "corpus" and threading.current_thread() is threading.main_thread():
        def on_term(signum, frame):
            raise _Interrupt()
        for sig in (signal.SIGINT, getattr(signal, "SIGTERM", None)):
            if sig is not None:
                old_handlers[sig] = signal.signal(sig, on_term if sig != signal.SIGINT else signal.default_int_handler)

    interrupted = False
    if mode == "corpus":
        print(f"Starting downloader: {total} papers target (workers={workers}, delay={delay}s, "
              f"prev_time={prev_wall_sec:.2f}s)...", flush=True)
    flush("in_progress")
    try:
        with (ThreadPoolExecutor(max_workers=max(1, connections), thread_name_prefix="net") as net_pool,
              ThreadPoolExecutor(max_workers=max(4, workers), thread_name_prefix="ext") as ext_pool):
            try:
                for job in source:
                    if stopped():
                        break
                    if job is None:  # idle slot of the live stream: let finished papers be collected
                        collect(0)
                        continue
                    aid, version_list = job
                    if aid in items:
                        continue
                    versions = list(version_list)
                    paper_dir = papers_dir / aid.replace(".", "-")
                    for version in versions:
                        _require_prepared_folder(paper_dir / version)
                    items[aid] = versions
                    prev = all_status.get(aid)
                    todo = _todo_versions(prev, versions, paper_dir)
                    if not todo:
                        cached += 1
                        prev["versions_expected"] = versions
                        refresh_record(prev)  # normalise legacy records (schema/status/totals)
                        finish_paper(aid, prev["paper_status"])
                        continue
                    if prev is None:
                        all_status[aid] = {"arxiv_id": aid, "paper_status": "error", "version_downloads": {},
                                           "total_raw_archive_bytes": 0, "total_uncompressed_bytes": 0,
                                           "total_kept_tex_bib_bytes": 0, "download_time_sec": 0.0}
                    futs = [net_pool.submit(fetch_single_version_task, aid, v, client) for v in todo]
                    running.add(ext_pool.submit(extract_paper_to_existing_folders, len(items), total, aid,
                                                paper_dir, futs))
                    while len(running) >= window and not stopped():
                        collect()
                while running:
                    collect()
                if not stopped() and not skip_audit:
                    run_audit(list(items.items()), papers_dir, all_status, client, net_pool,
                              stop_event=stop, checkpoint=lambda: flush("in_progress"))
            except BaseException as exc:  # Ctrl+C / SIGTERM or any unexpected failure: never lose finished work
                stop.set()  # in-flight downloads abort quickly instead of finishing their retries
                interrupted = isinstance(exc, KeyboardInterrupt)
                if interrupted:
                    print("\nInterrupted: saving checkpoint and summary...", flush=True)
                else:
                    fatal = exc
                try:
                    while running:
                        collect()
                except BaseException as drain_exc:  # a second Ctrl+C, disk full, ...: save what we have
                    logger.warning("[DOWNLOAD] Draining stopped early: %s: %s", type(drain_exc).__name__, drain_exc)
    finally:
        for sig, handler in old_handlers.items():
            signal.signal(sig, handler)

    if fatal is not None:
        flush("failed", report=show_report, exit_code=1, reason=f"{type(fatal).__name__}: {fatal}")
        raise fatal

    selected_failed = sum(all_status[a].get("paper_status") == "error" for a in done_ids if a in all_status)
    if interrupted:
        run_state, exit_code, reason = "interrupted", 130, "interrupted"
    elif blocked:
        run_state, exit_code, reason = "stopped", 3, "blocked_by_server"
    elif stopped():
        run_state, exit_code, reason = "stopped", 1, "stopped"
    elif selected_failed:
        run_state, exit_code, reason = "completed", 1, "publications_incomplete"
    else:
        run_state, exit_code, reason = "completed", 0, None
    summary = flush(run_state, report=show_report, exit_code=exit_code, reason=reason)
    if not show_report:
        label = {"stopped": "Stopped", "interrupted": "Interrupted"}.get(
            run_state, "Finished with errors" if exit_code else "Done")
        logger.info("[DOWNLOAD] %s: ok=%d no_source=%d error=%d.", label, summary["papers_with_tex"],
                    summary["papers_without_tex"], summary["papers_failed"])
    return summary


def download_corpus(
    papers_index: dict[str, list[str]],
    output_dir: Path = DEFAULT_OUTPUT_DIR,
    workers: int = 8,
    delay: float = DEFAULT_DELAY,
    limit: int | None = None,
    connections: int = DEFAULT_CONNECTIONS,
    *,
    contact_email: str | None = None,
    skip_audit: bool = False,
) -> dict:
    """Download a whole index (batch / CLI). A `--limit` run writes its own summary file."""
    items = list(papers_index.items())[:limit]
    return _engine(items, mode="corpus", total=len(items), output_dir=output_dir, workers=workers, delay=delay,
                   connections=connections, stop_event=None, client=None,
                   summary_name="download_summary_limit.json" if limit is not None else "download_summary.json",
                   skip_audit=skip_audit, contact_email=contact_email, show_report=True)


def download_stream(papers_index, *, output_dir=DEFAULT_OUTPUT_DIR, workers=8, delay=DEFAULT_DELAY,
                    connections=DEFAULT_CONNECTIONS, stop_event=None, client=None, skip_audit=False) -> dict:
    """Download an index or an (ID, versions) stream; a None item means "idle, keep collecting"."""
    streaming = not isinstance(papers_index, dict)
    source = papers_index if streaming else papers_index.items()
    return _engine(source, mode="stream", total=0 if streaming else len(papers_index), output_dir=output_dir,
                   workers=workers, delay=delay, connections=connections, stop_event=stop_event, client=client,
                   summary_name="download_summary.json", skip_audit=skip_audit, contact_email=None,
                   show_report=False)