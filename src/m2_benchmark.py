"""M2 wall time, per-publication time, sampled RSS (workers included) and disk usage."""

import datetime
import json
import logging
import os
from pathlib import Path
import threading
import time
import uuid

import psutil


def tree_bytes(path):
    """Total size of all files below path (0 when it does not exist)."""
    total = 0
    path = Path(path)
    if not path.exists():
        return 0
    for directory, _, files in os.walk(path):
        for name in files:
            try:
                total += (Path(directory) / name).stat().st_size
            except OSError:
                pass
    return total


class Benchmark:
    """Samples RSS of this process plus every worker process while M2 runs."""

    def __init__(self, root, workers, output_dirs):
        self.root = Path(root)
        self.workers = workers
        self.output_dirs = [Path(p) for p in output_dirs]
        self.started = datetime.datetime.now(datetime.timezone.utc).isoformat()
        self.t0 = time.perf_counter()
        self.papers, self.errors = {}, set()
        self.rss, self.disk_peak = [], None
        self.lock, self.stop = threading.Lock(), threading.Event()
        self.thread = threading.Thread(target=self.sample, name="m2-benchmark", daemon=True)
        self.disk_thread = threading.Thread(target=self.sample_disk, name="m2-disk", daemon=True)

    def start(self):
        self.thread.start()
        self.disk_thread.start()

    def record(self, publication, elapsed, stages, status, versions=0):
        """Called by the parent when a publication has finished all stages."""
        with self.lock:
            self.papers[publication] = {
                "time_sec": elapsed,
                "stages_sec": stages,
                "status": status,
                "versions": versions,
                "finished_offset_sec": time.perf_counter() - self.t0,
            }

    def sample(self):
        process = psutil.Process()
        while True:
            now = time.perf_counter()
            try:
                total = 0
                for item in [process, *process.children(recursive=True)]:
                    try:
                        total += item.memory_info().rss
                    except psutil.Error:
                        pass
                self.rss.append((now, total))
            except (psutil.Error, OSError):
                self.errors.add("rss_sampling_incomplete")
            if self.stop.is_set():
                return
            self.stop.wait(0.5)

    def sample_disk(self):
        """Separate thread: a full scan of data/ is slow and must not delay RSS sampling."""
        while not self.stop.wait(60):
            self.measure_disk()

    def measure_disk(self):
        try:
            size = tree_bytes(self.root / "data")
            self.disk_peak = max(self.disk_peak or 0, size)
        except OSError:
            self.errors.add("disk_sampling_incomplete")

    def finish(self, extra=None):
        finished = time.perf_counter()
        self.stop.set()
        self.thread.join()
        self.disk_thread.join()
        self.measure_disk()  # final size of data/, taken after the timed run
        durations = [row["time_sec"] for row in self.papers.values()]
        samples = self.rss
        span = samples[-1][0] - samples[0][0] if len(samples) > 1 else 0
        mean_rss = (
            sum(
                (b[0] - a[0]) * (a[1] + b[1]) / 2 for a, b in zip(samples, samples[1:])
            )
            / span
            if span
            else samples[0][1] if samples else None
        )
        try:
            output_bytes = sum(tree_bytes(p) for p in self.output_dirs)
        except OSError:
            output_bytes = None
            self.errors.add("output_size_incomplete")
        stage_totals = {}
        for row in self.papers.values():
            for name, value in row["stages_sec"].items():
                stage_totals[name] = stage_totals.get(name, 0.0) + value
        wall = finished - self.t0
        report = {
            "started_at_utc": self.started,
            "workers": self.workers,
            "pipeline_wall_sec": wall,
            "average_paper_time_sec": sum(durations) / len(durations) if durations else None,
            "wall_per_paper_sec": wall / len(durations) if durations else None,
            "papers_observed": len(durations),
            "cpu_seconds_per_stage": stage_totals,
            "sampled_peak_rss_bytes": max((v for _, v in samples), default=None),
            "mean_rss_bytes": mean_rss,
            "sampled_peak_disk_bytes": self.disk_peak,
            "final_dataset_bytes": output_bytes,
            "papers": [{"publication": k, **v} for k, v in sorted(self.papers.items())],
            "warnings": sorted(self.errors),
            "measurement": {
                "ram_interval_sec": 0.5,
                "disk_interval_sec": 60,
                "ram_scope": "parent process plus all worker processes (summed sampled RSS)",
                "disk_scope": str(self.root / "data"),
                "paper_time": "latency of one publication through parse, cleanup, reference dedup and full-text dedup inside its worker",
                "wall_per_paper": "end-to-end wall time divided by publications (reflects parallel speedup)",
                "peaks": "largest sampled observation, not a guaranteed absolute peak",
            },
            **(extra or {}),
        }
        directory = self.root / "data/output/benchmark"
        directory.mkdir(parents=True, exist_ok=True)
        path = directory / (
            "m2_benchmark_"
            + datetime.datetime.now(datetime.timezone.utc).strftime("%Y-%m-%d_%H-%M-%S_utc_")
            + uuid.uuid4().hex[:8]
            + ".json"
        )
        path.write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        logging.getLogger(__name__).info("[BENCHMARK] Saved: %s", path)
        return path
