"""M1 wall time, publication completion, sampled RSS and disk usage."""

import datetime
import json
import logging
import os
from pathlib import Path
import tempfile
import threading
import time
import uuid

import psutil

active = None


def event(aid, stage, value=None):
    if active is not None:
        active.event(aid, stage, value)


def tree_bytes(path):
    total = 0
    try:
        path.stat()
    except FileNotFoundError:
        return 0

    def fail(error):
        raise error

    for directory, _, files in os.walk(path, onerror=fail):
        for name in files:
            try:
                total += (Path(directory) / name).stat().st_size
            except FileNotFoundError:
                pass
    return total


class Benchmark:
    def __init__(self, root):
        self.root = Path(root)
        self.started = datetime.datetime.now(datetime.timezone.utc).isoformat()
        self.t0 = time.perf_counter()
        self.papers, self.errors = {}, set()
        self.lock, self.stop = threading.Lock(), threading.Event()
        self.rss, self.disk_peak, self.discovery = [], None, None
        file_descriptor, self.metadata_events = tempfile.mkstemp(
            prefix="m1_metadata_", suffix=".jsonl"
        )
        os.close(file_descriptor)
        self.thread = threading.Thread(
            target=self.sample, name="m1-benchmark", daemon=True
        )

    def event(self, aid, stage, value=None):
        now = time.perf_counter()
        with self.lock:
            row = self.papers.setdefault(aid, {"stages": {}, "versions": {}})
            if stage == "start":
                row.setdefault("start", now)
            elif stage == "ready":
                if "start" not in row:
                    row.update(start=now, cached_entry=True)
                row["expected_versions"] = list(value)
            elif stage == "cached_download":
                row["cached_download"] = True
            elif stage == "download":
                row["versions"].update(
                    {
                        v: {"status": item["status"], "at": now}
                        for v, item in value.items()
                    }
                )
                expected = row.get("expected_versions", [])
                if expected and all(v in row["versions"] for v in expected):
                    row["stages"]["download"] = {
                        "at": now,
                        "status": (
                            "error"
                            if any(
                                row["versions"][v]["status"] == "error"
                                for v in expected
                            )
                            else "done"
                        ),
                    }
            else:
                row["stages"][stage] = {"at": now, "status": value}

    def sample(self):
        process = psutil.Process()
        next_disk = 0
        while True:
            now = time.perf_counter()
            try:
                processes = {
                    sampled_process.pid: sampled_process
                    for sampled_process in [process, *process.children(recursive=True)]
                }
                rss = 0
                for sampled_process in processes.values():
                    try:
                        rss += sampled_process.memory_info().rss
                    except psutil.NoSuchProcess:
                        pass
                self.rss.append((now, rss))
            except (psutil.Error, OSError):
                self.errors.add("rss_sampling_incomplete")
            if now >= next_disk or self.stop.is_set():
                try:
                    size = tree_bytes(self.root / "data")
                    self.disk_peak = max(self.disk_peak or 0, size)
                except OSError:
                    self.errors.add("disk_sampling_incomplete")
                next_disk = now + 60
            if self.stop.is_set():
                return
            self.stop.wait(0.5)

    def finish(self, codes):
        finished_at = time.perf_counter()
        self.stop.set()
        self.thread.join()
        try:
            for line in (
                Path(self.metadata_events).read_text(encoding="utf-8").splitlines()
            ):
                arxiv_id, recorded_at = json.loads(line)
                if arxiv_id in self.papers:
                    self.papers[arxiv_id]["stages"]["metadata"] = {
                        "at": recorded_at,
                        "status": "done",
                    }
        except (OSError, ValueError):
            self.errors.add("metadata_timestamps_incomplete")
        finally:
            Path(self.metadata_events).unlink(missing_ok=True)
        rows, durations = [], []
        output_size = 0
        for arxiv_id, row in self.papers.items():
            stages = row["stages"]
            complete = all(
                stage in stages for stage in ("metadata", "download", "references")
            )
            elapsed = (
                max(stage["at"] for stage in stages.values()) - row["start"]
                if complete
                else None
            )
            cached = row.get("cached_entry", False) or row.get("cached_download", False)
            if elapsed is not None and not cached:
                durations.append(elapsed)
            try:
                output_size += tree_bytes(
                    self.root / "data/output/papers" / arxiv_id.replace(".", "-")
                )
            except OSError:
                self.errors.add("output_size_incomplete")
            rows.append(
                {
                    "arxiv_id": arxiv_id,
                    "time_sec": elapsed,
                    "cached": cached,
                    "complete": complete,
                    "started_offset_sec": row["start"] - self.t0,
                    "stages": {
                        stage: {
                            "finished_offset_sec": value["at"] - self.t0,
                            "status": value["status"],
                        }
                        for stage, value in stages.items()
                    },
                }
            )
        samples = self.rss
        sample_span = samples[-1][0] - samples[0][0] if len(samples) > 1 else 0
        mean_rss_bytes = (
            sum(
                (next_sample[0] - previous_sample[0])
                * (previous_sample[1] + next_sample[1])
                / 2
                for previous_sample, next_sample in zip(samples, samples[1:])
            )
            / sample_span
            if sample_span
            else samples[0][1] if samples else None
        )
        try:
            required_paper_count = len(
                (self.root / "data/input/group_ids.txt")
                .read_text(encoding="utf-8")
                .splitlines()
            )
        except OSError:
            required_paper_count = None
        if any(not row["complete"] for row in rows):
            self.errors.add("incomplete_paper_timestamps")
        report = {
            "papers_required": required_paper_count,
            "started_at_utc": self.started,
            "module_exit_codes": codes,
            "pipeline_wall_sec": finished_at - self.t0,
            "discovery_wall_sec": self.discovery,
            "average_paper_time_sec": (
                sum(durations) / len(durations) if durations else None
            ),
            "average_paper_count": len(durations),
            "papers_observed": len(rows),
            "sampled_peak_rss_bytes": max(
                (value for _, value in samples), default=None
            ),
            "mean_rss_bytes": mean_rss_bytes,
            "sampled_peak_disk_bytes": self.disk_peak,
            "final_dataset_bytes": (
                output_size if "output_size_incomplete" not in self.errors else None
            ),
            "papers": rows,
            "warnings": sorted(self.errors),
            "measurement": {
                "ram_interval_sec": 0.5,
                "disk_interval_sec": 60,
                "ram_scope": "current process (including notebook kernel) plus descendants; sampled RSS",
                "disk_scope": str(self.root / "data"),
                "paper_time": "wall latency including waits/retries; cached or incomplete papers excluded from mean",
                "pipeline_time": "includes business output writes; excludes final benchmark persistence",
                "peaks": "largest sampled observation, not guaranteed absolute peaks",
            },
        }
        benchmark_dir = self.root / "data/output/benchmark"
        benchmark_dir.mkdir(parents=True, exist_ok=True)
        report_path = benchmark_dir / (
            "m1_benchmark_"
            + datetime.datetime.now(datetime.timezone.utc).strftime(
                "%Y-%m-%d_%H-%M-%S_utc_"
            )
            + uuid.uuid4().hex[:8]
            + ".json"
        )
        report_path.write_text(
            json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
        )
        logging.getLogger(__name__).info("[BENCHMARK] Saved: %s", report_path)
        return report_path
