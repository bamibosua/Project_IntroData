"""Orchestration: download every paper of the index, checkpoint, audit, summarize."""

from __future__ import annotations

from concurrent.futures import FIRST_COMPLETED, Future, ThreadPoolExecutor, wait
import json
import logging
import os
from pathlib import Path
import signal
import time
import tracemalloc

from .audit import run_audit
from .client import FastArxivClient
from .config import CHECKPOINT_EVERY, DEFAULT_OUTPUT_DIR, DOWNLOAD_META_DIR_NAME
from .pipeline import extract_paper_to_existing_folders, fetch_single_version_task
from .stats import compute_storage_metrics, print_report, write_summary

try:
    if __package__ and __package__.startswith('src.'):
        from .. import m1_benchmark as benchmark
    else:
        import m1_benchmark as benchmark
except ImportError:
    benchmark = None

logger = logging.getLogger(__name__)


def _save_status(status_path: Path, all_status: dict) -> None:
    status_path.parent.mkdir(parents=True, exist_ok=True)
    status_path.write_text(json.dumps(all_status, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")


def _load_status(status_path: Path, legacy_dir: Path | None = None) -> dict:
    for candidate in (status_path, (legacy_dir / status_path.name) if legacy_dir else None):
        if candidate is not None and candidate.exists():
            try:
                return json.loads(candidate.read_text(encoding="utf-8"))
            except Exception:
                pass
    return {}


def _install_sigint_handler(status_path: Path, all_status: dict) -> None:
    def handler(signum, frame):
        """Ctrl+C: save the checkpoint and exit right away (do not wait for running downloads)."""
        print("\nInterrupted (Ctrl+C): saving checkpoint...", flush=True)
        try:
            _save_status(status_path, all_status)
        finally:
            os._exit(130)

    signal.signal(signal.SIGINT, handler)


from datetime import datetime, timezone
from typing import Callable


class _Progress:
    """Counters and checkpointing shared by the drain loops."""

    def __init__(self, all_status: dict, on_checkpoint: Callable[[], None]):
        self.all_status = all_status
        self.on_checkpoint = on_checkpoint
        self.cached = self.success = self.no_tex = self.error = 0
        self._pending = 0

    def count_cached(self, status: str) -> None:
        self.cached += 1
        self.success += status == "success"
        self.no_tex += status != "success"

    def consume(self, futures: set[Future], timeout: float = 1.0) -> set[Future]:
        """Wait for some extraction tasks, record their results, return the still-running ones."""
        done, running = wait(futures, timeout=timeout, return_when=FIRST_COMPLETED)
        for f in done:
            res = f.result()
            self.all_status[res["arxiv_id"]] = res["record"]
            if res["status"] == "success":
                self.success += 1
            elif res["status"] == "no_tex_source":
                self.no_tex += 1
                logger.info("[DOWNLOAD] %s (%dv): no TeX source / PDF-only", res["arxiv_id"], res.get("ver_count", 1))
            else:
                self.error += 1
            print(f"[{res['idx']}/{res['total']}] {res['arxiv_id']} ({res['ver_count']}v) -> "
                  f"{res['status']} | {res['elapsed']:.2f}s", flush=True)
            self._pending += 1
            if self._pending >= CHECKPOINT_EVERY:
                self.on_checkpoint()
                self._pending = 0
        return running


def _is_cached(prev: dict, paper_dir: Path, v_list: list[str]) -> bool:
    return prev.get("paper_status") in ("success", "no_tex_source") and all((paper_dir / v).exists() for v in v_list)


def _require_prepared_folder(path: Path) -> None:
    if not path.is_dir():
        message = f'Missing prepared folder: {path}. Run entry first.'
        logger.error('[DOWNLOAD] %s', message)
        raise FileNotFoundError(message)


def download_corpus(
    papers_index: dict[str, list[str]],
    output_dir: Path = DEFAULT_OUTPUT_DIR,
    workers: int = 8,
    delay: float = 0.6,
    limit: int | None = None,
    connections: int = 32,
) -> dict:
    output_dir = Path(output_dir)
    _require_prepared_folder(output_dir)
    papers_dir = output_dir / "papers"
    meta_dir = output_dir / DOWNLOAD_META_DIR_NAME
    meta_dir.mkdir(parents=True, exist_ok=True)
    status_path = meta_dir / "download_status.json"
    summary_path = meta_dir / ("download_summary_limit.json" if limit is not None else "download_summary.json")

    all_status = _load_status(status_path, legacy_dir=output_dir)
    prev_summary = _load_status(summary_path, legacy_dir=output_dir)

    tracemalloc.start()
    dt_start = datetime.now()
    t_start = time.perf_counter()

    prev_wall_sec = float(prev_summary.get("total_wall_time_sec", 0.0))
    first_started_local = prev_summary.get("first_started_at_local") or prev_summary.get("started_at_local")
    first_started_utc = prev_summary.get("first_started_at_utc") or prev_summary.get("started_at_utc")
    first_started_local = first_started_local or dt_start.strftime("%Y-%m-%d %H:%M:%S")
    first_started_utc = first_started_utc or dt_start.astimezone(timezone.utc).isoformat()
    prev_peak_mb = float(prev_summary.get("tracemalloc_peak_heap_mb", 0.0))

    items = list(papers_index.items())[:limit]
    item_keys = {k for k, _ in items}
    paths_dict = {
        "papers_dir": papers_dir,
        "download_status_json": status_path,
        "download_summary_json": summary_path,
    }

    def flush_checkpoint_and_summary(run_state: str, show_report: bool = False) -> dict:
        _save_status(status_path, all_status)
        dt_now = datetime.now()
        session_wall_sec = round(time.perf_counter() - t_start, 4)
        total_wall_sec = round(prev_wall_sec + session_wall_sec, 4)
        _, peak_heap = tracemalloc.get_traced_memory()
        cur_peak_mb = max(prev_peak_mb, round(peak_heap / (1024 * 1024), 2))

        active_status = {k: r for k, r in all_status.items() if k in item_keys}
        n_tex = sum(1 for r in active_status.values() if r.get("paper_status") == "success")
        n_notex = sum(1 for r in active_status.values() if r.get("paper_status") == "no_tex_source")
        n_fail = len(active_status) - n_tex - n_notex

        counters = {
            "total_papers": len(items),
            "processed_papers": len(active_status),
            "cached_papers": progress.cached,
            "papers_with_tex": n_tex,
            "papers_without_tex": n_notex,
            "papers_failed": n_fail,
        }
        metrics = compute_storage_metrics(active_status)
        summary = write_summary(
            summary_path, counters, metrics, total_wall_sec, cur_peak_mb, paths_dict,
            prev_wall_sec=prev_wall_sec, session_wall_sec=session_wall_sec,
            first_started_local=first_started_local, first_started_utc=first_started_utc,
            dt_start=dt_start, dt_end=dt_now, run_state=run_state,
        )
        if show_report:
            print_report(
                counters, metrics, total_wall_sec, cur_peak_mb, summary_path,
                prev_wall_sec=prev_wall_sec, session_wall_sec=session_wall_sec,
                first_started_local=first_started_local, dt_start=dt_start,
                dt_end=dt_now, run_state=run_state,
            )
        return summary

    progress = _Progress(all_status, on_checkpoint=lambda: flush_checkpoint_and_summary("in_progress", False))

    def _on_sigint(signum, frame):
        print("\nInterrupted: saving checkpoint and summary...", flush=True)
        try:
            flush_checkpoint_and_summary("interrupted", show_report=True)
        finally:
            os._exit(130)

    signal.signal(signal.SIGINT, _on_sigint)
    if hasattr(signal, "SIGTERM"):
        signal.signal(signal.SIGTERM, _on_sigint)

    flush_checkpoint_and_summary("in_progress", show_report=False)

    client = FastArxivClient(delay=delay)
    running: set[Future] = set()
    window = max(workers * 2, connections)

    print(f"Starting downloader: {len(items)} papers target (workers={workers}, delay={delay}s, prev_time={prev_wall_sec:.2f}s)...", flush=True)

    with (
        ThreadPoolExecutor(max_workers=max(1, connections), thread_name_prefix="net") as net_pool,
        ThreadPoolExecutor(max_workers=max(4, workers), thread_name_prefix="ext") as ext_pool,
    ):
        for idx, (arxiv_id, v_list) in enumerate(items, start=1):
            paper_dir = papers_dir / arxiv_id.replace(".", "-")
            prev = all_status.get(arxiv_id, {})

            if _is_cached(prev, paper_dir, v_list):
                progress.count_cached(prev["paper_status"])
                if progress.cached <= 5 or progress.cached % 2000 == 0:
                    print(f"[{idx}/{len(items)}] {arxiv_id} ({len(v_list)}v): cached", flush=True)
                continue

            for version in v_list:
                _require_prepared_folder(paper_dir / version)
            t0 = time.perf_counter()
            v_futs = [net_pool.submit(fetch_single_version_task, arxiv_id, v, client) for v in v_list]
            running.add(ext_pool.submit(
                extract_paper_to_existing_folders, idx, len(items), arxiv_id, paper_dir, v_futs, t0))

            while len(running) >= window:
                running = progress.consume(running)

        while running:
            running = progress.consume(running)

        run_audit(items, papers_dir, all_status, client, net_pool)

    summary = flush_checkpoint_and_summary("completed", show_report=True)
    tracemalloc.stop()
    return summary


def _refresh_stream_record(record, versions):
    statuses = [record['version_downloads'].get(version, {}).get('status') for version in versions]
    if any(status not in ('success', 'no_tex_source') for status in statuses):
        record['paper_status'] = 'error'
    else:
        record['paper_status'] = 'success' if 'success' in statuses else 'no_tex_source'
    for total, field in (('total_raw_archive_bytes', 'raw_archive_bytes'),
                         ('total_uncompressed_bytes', 'uncompressed_total_bytes'),
                         ('total_kept_tex_bib_bytes', 'kept_tex_bib_bytes')):
        record[total] = sum(int(version.get(field, 0)) for version in record['version_downloads'].values())


def download_stream(papers_index, *, output_dir=DEFAULT_OUTPUT_DIR,
                    workers=8, delay=0.6, connections=32, stop_event=None, client=None):
    """Download an index or (ID, versions) stream; None means temporarily idle."""
    output_dir = Path(output_dir)
    _require_prepared_folder(output_dir)
    papers_dir = output_dir / 'papers'
    meta_dir = output_dir / DOWNLOAD_META_DIR_NAME
    meta_dir.mkdir(parents=True, exist_ok=True)
    status_path = meta_dir / 'download_status.json'
    summary_path = meta_dir / 'download_summary.json'
    all_status = _load_status(status_path, legacy_dir=output_dir)
    client = client if client is not None else FastArxivClient(delay=delay)
    streaming = not isinstance(papers_index, dict)
    source = papers_index if streaming else papers_index.items()
    total = 0 if streaming else len(papers_index)
    items, running = {}, set()
    cached = completed = 0
    window = max(workers * 2, connections)
    t_start = time.perf_counter()
    owns_trace = not tracemalloc.is_tracing()
    if owns_trace:
        tracemalloc.start()

    def stopped():
        return stop_event is not None and stop_event.is_set()

    def collect(timeout=1.0):
        nonlocal completed, running
        if not running:
            return
        done, running = wait(running, timeout=timeout, return_when=FIRST_COMPLETED)
        for future in done:
            result = future.result()
            aid = result['arxiv_id']
            all_status[aid] = result['record']
            completed += 1
            if result['status'] == 'error':
                logger.warning('[DOWNLOAD] %s: incomplete; see download_status.json.', aid)
            elif result['status'] == 'no_tex_source':
                logger.info('[DOWNLOAD] %s (%dv): no TeX source / PDF-only', aid, result.get('ver_count', 1))
            if completed % 50 == 0:
                logger.info('[DOWNLOAD] Processed: %d.', completed)
            if completed % CHECKPOINT_EVERY == 0:
                _save_status(status_path, all_status)

    try:
        with (ThreadPoolExecutor(max_workers=max(1, connections), thread_name_prefix='net') as net_pool,
              ThreadPoolExecutor(max_workers=max(4, workers), thread_name_prefix='ext') as ext_pool):
            for job in source:
                if stopped():
                    break
                if job is None:
                    collect(0)
                    continue
                aid, version_list = job
                if aid in items:
                    continue
                idx = len(items) + 1
                versions = list(version_list)
                paper_dir = papers_dir / aid.replace('.', '-')
                for version in versions:
                    _require_prepared_folder(paper_dir / version)
                items[aid] = versions
                if _is_cached(all_status.get(aid, {}), paper_dir, versions):
                    if benchmark is not None:
                        benchmark.event(aid, 'cached_download')
                    if benchmark is not None:
                        benchmark.event(aid, 'download', all_status[aid].get('version_downloads', {}))
                    cached += 1
                    completed += 1
                    if completed % 50 == 0:
                        logger.info('[DOWNLOAD] Processed: %d.', completed)
                    continue
                t0 = time.perf_counter()
                all_status[aid] = {'arxiv_id': aid, 'paper_status': 'error', 'version_downloads': {},
                                   'total_raw_archive_bytes': 0, 'total_uncompressed_bytes': 0,
                                   'total_kept_tex_bib_bytes': 0, 'download_time_sec': 0}
                version_futures = [net_pool.submit(fetch_single_version_task, aid, version, client)
                                   for version in versions]
                running.add(ext_pool.submit(
                    extract_paper_to_existing_folders, idx, total, aid, paper_dir,
                    version_futures, t0))
                while len(running) >= window and not stopped():
                    collect()
            while running:
                collect()
            if not stopped():
                run_audit(list(items.items()), papers_dir, all_status, client, net_pool)
    finally:
        try:
            for aid, versions in items.items():
                _refresh_stream_record(all_status[aid], versions)
            _save_status(status_path, all_status)
        finally:
            _, peak_heap = tracemalloc.get_traced_memory()
            if owns_trace:
                tracemalloc.stop()

    selected = {aid: all_status[aid] for aid in items}
    counters = {'total_papers': len(items), 'cached_papers': cached,
                'papers_with_tex': sum(r['paper_status'] == 'success' for r in selected.values()),
                'papers_without_tex': sum(r['paper_status'] == 'no_tex_source' for r in selected.values()),
                'papers_failed': sum(r['paper_status'] == 'error' for r in selected.values())}
    exit_code = 1 if stopped() or counters['papers_failed'] else 0
    reason = 'stopped' if stopped() else 'publications_incomplete' if exit_code else None
    wall_sec = round(time.perf_counter() - t_start, 4)
    metrics = compute_storage_metrics(selected)
    summary = write_summary(summary_path, counters, metrics, wall_sec, round(peak_heap / 1024**2, 2),
                            {'papers_dir': papers_dir, 'download_status_json': status_path,
                             'download_summary_json': summary_path}, exit_code=exit_code, reason=reason)
    label = 'Stopped' if stopped() else 'Finished with errors' if exit_code else 'Done'
    logger.info('[DOWNLOAD] %s: ok=%d no_source=%d error=%d.',
                label, counters['papers_with_tex'], counters['papers_without_tex'], counters['papers_failed'])
    return summary
