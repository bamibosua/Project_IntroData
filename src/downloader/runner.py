"""Orchestration: download every paper of the index, checkpoint, audit, summarize."""

from __future__ import annotations

from concurrent.futures import FIRST_COMPLETED, Future, ThreadPoolExecutor, wait
import json
import logging
import os
from pathlib import Path
from queue import Empty
import signal
import time
import tracemalloc

from .audit import run_audit
from .client import FastArxivClient
from .config import CHECKPOINT_EVERY, DEFAULT_OUTPUT_DIR
from .pipeline import extract_paper_to_existing_folders, fetch_single_version_task
from .stats import compute_storage_metrics, print_report, write_summary

logger = logging.getLogger(__name__)


def _save_status(status_path: Path, all_status: dict) -> None:
    status_path.write_text(json.dumps(all_status, ensure_ascii=False) + "\n", encoding="utf-8")


def _load_status(status_path: Path) -> dict:
    if status_path.exists():
        try:
            return json.loads(status_path.read_text(encoding="utf-8"))
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


class _Progress:
    """Counters and checkpointing shared by the drain loops."""

    def __init__(self, status_path: Path, all_status: dict):
        self.status_path, self.all_status = status_path, all_status
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
            else:
                self.error += 1
            print(f"[{res['idx']}/{res['total']}] {res['arxiv_id']} ({res['ver_count']}v) -> "
                  f"{res['status']} | {res['elapsed']:.2f}s", flush=True)
            self._pending += 1
            if self._pending >= CHECKPOINT_EVERY:
                _save_status(self.status_path, self.all_status)
                self._pending = 0
        return running


def _is_cached(prev: dict, paper_dir: Path, v_list: list[str]) -> bool:
    return prev.get("paper_status") in ("success", "no_tex_source") and all((paper_dir / v).exists() for v in v_list)


def download_corpus(
    papers_index: dict[str, list[str]],
    output_dir: Path = DEFAULT_OUTPUT_DIR,
    workers: int = 8,
    delay: float = 0.6,
    limit: int | None = None,
    connections: int = 32,
) -> dict:
    papers_dir = output_dir / "papers"
    status_path = output_dir / "download_status.json"
    # A partial (--limit) run must not overwrite the corpus-wide summary.
    summary_path = output_dir / ("download_summary_limit.json" if limit is not None else "download_summary.json")

    all_status = _load_status(status_path)
    _install_sigint_handler(status_path, all_status)

    client = FastArxivClient(delay=delay)
    items = list(papers_index.items())[:limit]

    tracemalloc.start()
    t_start = time.perf_counter()
    progress = _Progress(status_path, all_status)
    running: set[Future] = set()
    window = max(workers * 2, connections)

    print(f"Starting downloader: {len(items)} papers target (workers={workers}, delay={delay}s)...", flush=True)

    with (
        ThreadPoolExecutor(max_workers=max(1, connections), thread_name_prefix="net") as net_pool,
        ThreadPoolExecutor(max_workers=max(4, workers), thread_name_prefix="ext") as ext_pool,
    ):
        for idx, (arxiv_id, v_list) in enumerate(items, start=1):
            paper_dir = papers_dir / arxiv_id.replace(".", "-")
            prev = all_status.get(arxiv_id, {})

            if _is_cached(prev, paper_dir, v_list):  # already done on disk: skip immediately
                progress.count_cached(prev["paper_status"])
                if progress.cached <= 5 or progress.cached % 2000 == 0:
                    print(f"[{idx}/{len(items)}] {arxiv_id} ({len(v_list)}v): cached", flush=True)
                continue

            t0 = time.perf_counter()
            v_futs = [net_pool.submit(fetch_single_version_task, arxiv_id, v, client) for v in v_list]
            running.add(ext_pool.submit(
                extract_paper_to_existing_folders, idx, len(items), arxiv_id, paper_dir, v_futs, t0))

            while len(running) >= window:
                running = progress.consume(running)

        while running:
            running = progress.consume(running)

        run_audit(items, papers_dir, all_status, client, net_pool)

    _save_status(status_path, all_status)
    wall_sec = round(time.perf_counter() - t_start, 4)
    _, peak_heap = tracemalloc.get_traced_memory()
    tracemalloc.stop()
    peak_heap_mb = round(peak_heap / (1024 * 1024), 2)

    counters = {
        "total_papers": len(items),
        "cached_papers": progress.cached,
        "papers_with_tex": progress.success,
        "papers_without_tex": progress.no_tex,
        "papers_failed": progress.error,
    }
    metrics = compute_storage_metrics(all_status)
    summary = write_summary(
        summary_path, counters, metrics, wall_sec, peak_heap_mb,
        {"papers_dir": papers_dir, "download_status_json": status_path, "download_summary_json": summary_path},
    )
    print_report(counters, metrics, wall_sec, peak_heap_mb, summary_path)
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


def download_stream(queue, entry_done, entry_status, *, output_dir=DEFAULT_OUTPUT_DIR,
                    workers=8, delay=0.6, connections=32, stop_event=None, client=None):
    """Receive individual validated jobs until entry finishes; empty queue means wait."""
    output_dir = Path(output_dir)
    output_dir.mkdir(parents=True, exist_ok=True)
    papers_dir = output_dir / 'papers'
    status_path = output_dir / 'download_status.json'
    summary_path = output_dir / 'download_summary.json'
    all_status = _load_status(status_path)
    client = client if client is not None else FastArxivClient(delay=delay)
    items, started, pending = {}, {}, {}
    cached = completed = 0
    next_wait_notice = 0.0
    window = max(workers * 2, connections)
    t_start = time.perf_counter()
    owns_trace = not tracemalloc.is_tracing()
    if owns_trace:
        tracemalloc.start()

    def stopped():
        return ((stop_event is not None and stop_event.is_set())
                or (entry_done.is_set() and entry_status.get('exit_code', 1) != 0))

    def collect(timeout=1.0):
        nonlocal completed
        if not pending:
            return
        done, _ = wait(pending, timeout=timeout, return_when=FIRST_COMPLETED)
        for future in done:
            aid = pending.pop(future)
            result = future.result()
            record = all_status[aid]
            record['version_downloads'].update(result['record']['version_downloads'])
            _refresh_stream_record(record, items[aid])
            record['download_time_sec'] = round(time.perf_counter() - started[aid], 4)
            if len(record['version_downloads']) == len(items[aid]):
                completed += 1
                if record['paper_status'] == 'error':
                    logger.warning('[DOWNLOAD] %s: incomplete; see download_status.json.', aid)
                if completed % 50 == 0:
                    logger.info('[DOWNLOAD] Processed: %d.', completed)
                if completed % CHECKPOINT_EVERY == 0:
                    _save_status(status_path, all_status)

    try:
        with (ThreadPoolExecutor(max_workers=max(1, connections), thread_name_prefix='net') as net_pool,
              ThreadPoolExecutor(max_workers=max(4, workers), thread_name_prefix='ext') as ext_pool):
            while not stopped():
                collect(0)
                try:
                    job = queue.get(timeout=1)
                except Empty:
                    if entry_done.is_set():
                        break
                    if not pending and time.monotonic() >= next_wait_notice:
                        logger.info('[DOWNLOAD] Waiting for entry.')
                        next_wait_notice = time.monotonic() + 60
                    continue
                next_wait_notice = 0.0
                aid = job['arxiv_id']
                if aid in items:
                    continue
                versions = list(job['versions'])
                items[aid] = versions
                paper_dir = papers_dir / aid.replace('.', '-')
                if _is_cached(all_status.get(aid, {}), paper_dir, versions):
                    cached += 1
                    completed += 1
                    if completed % 50 == 0:
                        logger.info('[DOWNLOAD] Processed: %d.', completed)
                    continue
                started[aid] = time.perf_counter()
                all_status[aid] = {'arxiv_id': aid, 'paper_status': 'error', 'version_downloads': {},
                                   'total_raw_archive_bytes': 0, 'total_uncompressed_bytes': 0,
                                   'total_kept_tex_bib_bytes': 0, 'download_time_sec': 0}
                for version in versions:
                    while len(pending) >= window and not stopped():
                        collect()
                    if stopped():
                        break
                    future = ext_pool.submit(
                        extract_paper_to_existing_folders, len(items), 0, aid, paper_dir,
                        [net_pool.submit(fetch_single_version_task, aid, version, client)], started[aid])
                    pending[future] = aid
            while pending:
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
