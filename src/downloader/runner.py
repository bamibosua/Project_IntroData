"""Orchestration: download every paper of the index, checkpoint, audit, summarize."""

from __future__ import annotations

from concurrent.futures import FIRST_COMPLETED, Future, ThreadPoolExecutor, wait
from datetime import datetime, timezone
import json
import os
from pathlib import Path
import signal
import time
import tracemalloc
from typing import Callable

from .audit import run_audit
from .client import FastArxivClient
from .config import CHECKPOINT_EVERY, DEFAULT_OUTPUT_DIR
from .pipeline import extract_paper_to_existing_folders, fetch_single_version_task
from .stats import compute_storage_metrics, print_report, write_summary


def _save_status(status_path: Path, all_status: dict) -> None:
    status_path.parent.mkdir(parents=True, exist_ok=True)
    status_path.write_text(json.dumps(all_status, ensure_ascii=False) + "\n", encoding="utf-8")


def _load_json(path: Path) -> dict:
    if path.exists():
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
            if isinstance(data, dict):
                return data
        except Exception:
            pass
    return {}


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


def download_corpus(
    papers_index: dict[str, list[str]],
    output_dir: Path = DEFAULT_OUTPUT_DIR,
    workers: int = 8,
    delay: float = 0.6,
    limit: int | None = None,
    connections: int = 32,
) -> dict:
    output_dir.mkdir(parents=True, exist_ok=True)
    papers_dir = output_dir / "papers"
    status_path = output_dir / "download_status.json"
    # A partial (--limit) run must not overwrite the corpus-wide summary.
    summary_path = output_dir / ("download_summary_limit.json" if limit is not None else "download_summary.json")

    all_status = _load_json(status_path)
    prev_summary = _load_json(summary_path)

    tracemalloc.start()
    dt_start = datetime.now()
    t_start = time.perf_counter()

    prev_wall_sec = float(prev_summary.get("total_wall_time_sec", 0.0))
    first_started_local = prev_summary.get("first_started_at_local") or prev_summary.get("started_at_local")
    first_started_utc = prev_summary.get("first_started_at_utc") or prev_summary.get("started_at_utc")

    # Recover elapsed time from status_path & papers_dir timestamps if summary didn't exist yet
    if prev_wall_sec == 0.0 and all_status and status_path.exists():
        try:
            ctime = os.path.getctime(papers_dir if papers_dir.exists() else output_dir)
            mtime = os.path.getmtime(status_path)
            if mtime > ctime:
                prev_wall_sec = round(mtime - ctime, 4)
            dt_first = datetime.fromtimestamp(ctime)
            first_started_local = first_started_local or dt_first.strftime("%Y-%m-%d %H:%M:%S")
            first_started_utc = first_started_utc or dt_first.astimezone(timezone.utc).isoformat()
        except Exception:
            pass

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
            summary_path, counters, metrics,
            prev_wall_sec, session_wall_sec, total_wall_sec, cur_peak_mb,
            paths_dict, first_started_local, first_started_utc, dt_start, dt_now,
            run_state=run_state,
        )
        if show_report:
            print_report(
                counters, metrics, prev_wall_sec, session_wall_sec, total_wall_sec,
                cur_peak_mb, summary_path, first_started_local, dt_start, dt_now,
                run_state=run_state,
            )
        return summary

    progress = _Progress(all_status, on_checkpoint=lambda: flush_checkpoint_and_summary("in_progress", False))

    def _on_sigint(signum, frame):
        """Ctrl+C / SIGTERM: save both download_status.json and download_summary.json, print report, and exit."""
        print("\nInterrupted: saving checkpoint and summary...", flush=True)
        try:
            flush_checkpoint_and_summary("interrupted", show_report=True)
        finally:
            os._exit(130)

    signal.signal(signal.SIGINT, _on_sigint)
    if hasattr(signal, "SIGTERM"):
        signal.signal(signal.SIGTERM, _on_sigint)

    # Immediately create/update summary_path at startup so it always exists even if disconnected abruptly
    flush_checkpoint_and_summary("in_progress", show_report=False)

    client = FastArxivClient(delay=delay)
    running: set[Future] = set()
    window = max(workers * 2, connections)

    print(
        f"Starting downloader at {dt_start.strftime('%Y-%m-%d %H:%M:%S')}: "
        f"{len(items)} papers target (workers={workers}, connections={connections}, delay={delay}s, "
        f"prev_time={prev_wall_sec:.2f}s)...",
        flush=True,
    )

    with (
        ThreadPoolExecutor(max_workers=max(16, connections), thread_name_prefix="net") as net_pool,
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

    summary = flush_checkpoint_and_summary("completed", show_report=True)
    tracemalloc.stop()
    return summary
