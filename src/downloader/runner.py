"""Orchestration: download every paper of the index, checkpoint, audit, summarize."""

from __future__ import annotations

from concurrent.futures import FIRST_COMPLETED, Future, ThreadPoolExecutor, wait
import json
import os
from pathlib import Path
import signal
import time
import tracemalloc

from .audit import run_audit
from .client import FastArxivClient
from .config import CHECKPOINT_EVERY, DEFAULT_OUTPUT_DIR
from .pipeline import extract_paper_to_existing_folders, fetch_single_version_task
from .stats import compute_storage_metrics, print_report, write_summary


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
