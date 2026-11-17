"""Measure one references run and summarize only the selected publications."""

from __future__ import annotations

import csv
import datetime
import io
import logging
import os
import threading
import time

import psutil

if __package__:
    from . import config
    from .records import is_valid_record
    from .storage import default_meta_dir, load_record, record_path, write_bytes_atomic
else:
    import config
    from records import is_valid_record
    from storage import default_meta_dir, load_record, record_path, write_bytes_atomic

logger = logging.getLogger(__name__)


def timestamp() -> str:
    return datetime.datetime.now(datetime.timezone.utc).isoformat(timespec="seconds")


def refs_size(out_dir, publications) -> int | None:
    """Count selected references.json bytes; return None when measurement fails."""
    total = 0
    for publication in publications:
        try:
            total += os.path.getsize(record_path(out_dir, publication["arxiv_id"]))
        except FileNotFoundError:
            continue
        except OSError as exc:
            logger.warning(
                "Cannot measure reference output size (%s).", type(exc).__name__
            )
            return None
    return total


class ResourceMonitor:
    """Sample current-process RSS; the sample mean is not time-weighted."""

    def __init__(self, interval=None):
        self.interval = config.RAM_SAMPLE_INTERVAL if interval is None else interval
        self._stop = threading.Event()
        self._lock = threading.Lock()
        self._thread = None
        self._proc = None
        self.n, self._sum, self.peak = 0, 0, 0
        self.error = None

    def _warn(self, exc):
        if self.error is None:
            logger.warning("RAM monitoring is incomplete; see the run report.")
        self.error = str(exc)

    def _sample(self):
        try:
            value = self._proc.memory_info().rss
        except (psutil.Error, OSError) as exc:
            self._warn(exc)
            return
        with self._lock:
            self.n += 1
            self._sum += value
            self.peak = max(self.peak, value)

    def _loop(self):
        while not self._stop.wait(self.interval):
            self._sample()

    def __enter__(self):
        try:
            self._proc = psutil.Process()
            self._sample()
            self._thread = threading.Thread(target=self._loop, daemon=True)
            self._thread.start()
        except (psutil.Error, OSError, RuntimeError) as exc:
            self._warn(exc)
        return self

    def __exit__(self, *exc):
        self._stop.set()
        if self._thread is not None and self._thread.ident is not None:
            self._thread.join(timeout=2.0)
            if self._thread.is_alive():
                self._warn("sampler did not stop within 2 seconds")
                return
        if self._proc is not None:
            self._sample()

    def as_dict(self):
        with self._lock:
            return {
                "scope": "current_process_rss",
                "sampled_peak_rss_mb": round(self.peak / 1e6, 3) if self.n else None,
                "sample_mean_rss_mb": (
                    round(self._sum / self.n / 1e6, 3) if self.n else None
                ),
                "n_samples": self.n,
                "sample_interval_sec": self.interval,
                "status": (
                    "partial"
                    if self.error and self.n
                    else "unavailable" if not self.n else "ok"
                ),
                "error": self.error,
            }


class RunMetrics:
    """Track committed records immediately, including interrupted batches."""

    def __init__(self, cfg, publications, input_path, output_path):
        self.cfg, self.publications = cfg, publications
        self.input_path, self.output_path = os.path.abspath(
            input_path
        ), os.path.abspath(output_path)
        self.started_at, self._t0 = timestamp(), time.perf_counter()
        self.papers = dict(
            selected=len(publications),
            attempted=0,
            newly_completed=0,
            ok=0,
            empty_refs=0,
            not_in_s2=0,
            count_mismatch=0,
            errors=0,
        )
        self.references = {"total": 0, "with_arxiv_id": 0}
        self.payload_bytes = 0
        self.batches = self.splits = 0
        self.errors = []
        self.warnings = []

    def add_record(self, record, *, payload_bytes=0):
        self.papers["newly_completed"] += 1
        self.papers[record["status"]] += 1
        self.references["total"] += record["n_refs"]
        self.references["with_arxiv_id"] += record["n_refs_with_arxiv"]
        self.payload_bytes += payload_bytes

    def add_error(self, arxiv_id, error):
        self.papers["errors"] += 1
        self.errors.append(
            {"arxiv_id": arxiv_id, "code": error.code, "message": str(error)}
        )

    def finalize(
        self, monitor, disk_before, disk_after, requests, exit_code, reason=None
    ):
        wall = time.perf_counter() - self._t0
        papers = dict(self.papers)
        # Saved records may carry a count warning; completed counts verified records only.
        papers["saved"] = papers["newly_completed"]
        papers["completed"] = papers["saved"] - papers["count_mismatch"]
        papers["pending"] = papers["selected"] - papers["saved"]
        memory = monitor.as_dict()
        if memory["error"]:
            self.warnings.append("ram_monitor_unavailable_or_partial")
        return {
            "scope": "references_module",
            "started_at": self.started_at,
            "finished_at": timestamp(),
            "input_path": self.input_path,
            "output_path": self.output_path,
            "config": self.cfg,
            "status": (
                "interrupted"
                if exit_code == 130
                else "completed" if exit_code == 0 else "failed"
            ),
            "exit_code": exit_code,
            "reason": reason,
            "papers": papers,
            "selected_versions": sum(len(p["versions"]) for p in self.publications),
            "references": dict(self.references),
            "requests": requests,
            "batches_started": self.batches,
            "batch_splits": self.splits,
            "time": {
                "total_wall_sec": round(wall, 3),
                "avg_wall_sec_per_attempted_publication": (
                    round(wall / papers["attempted"], 3)
                    if papers["attempted"]
                    else None
                ),
                "note": "includes crawl/waits; excludes final telemetry write; not CPU time",
            },
            "memory": memory,
            "disk": {
                "selected_output_bytes_before": disk_before,
                "selected_output_bytes_after": disk_after,
                "committed_reference_payload_bytes": self.payload_bytes,
                "net_reference_output_change_bytes": (
                    disk_after - disk_before
                    if disk_after is not None and disk_before is not None
                    else None
                ),
                "peak_disk_bytes": None,
                "peak_disk_reason": "not_sampled",
            },
            "errors": self.errors,
            "warnings": list(self.warnings),
        }


CSV_COLUMNS = (
    "arxiv_id",
    "status",
    "complete",
    "n_refs",
    "n_refs_with_arxiv",
    "n_refs_without_s2_id",
)


def summarize_output(out_dir, publications, csv_path=None, meta_dir=None):
    """Summarize the selected IDs offline without changing records or merging runs."""
    counts = dict(
        selected=len(publications),
        saved=0,
        completed=0,
        pending=0,
        missing=0,
        invalid=0,
        unreadable=0,
        refresh_pending=0,
        ok=0,
        empty_refs=0,
        not_in_s2=0,
        count_mismatch=0,
        version_metadata_outdated=0,
    )
    refs = dict(total=0, with_arxiv_id=0, without_s2_id=0)
    missing_ids, diagnostics, rows, warnings = [], [], [], []
    for publication in publications:
        arxiv_id, record, problem = publication["arxiv_id"], None, None
        try:
            record = load_record(record_path(out_dir, arxiv_id))
        except ValueError as exc:
            problem = "invalid"
            diagnostics.append(
                {"arxiv_id": arxiv_id, "code": problem, "message": str(exc)}
            )
        except OSError as exc:
            problem = "unreadable"
            diagnostics.append(
                {"arxiv_id": arxiv_id, "code": problem, "message": str(exc)}
            )
        if problem is None:
            if record is None:
                problem = "missing"
            elif record.get("refresh_pending") is True:
                problem = "refresh_pending"
            elif not is_valid_record(record, arxiv_id, allow_legacy=True):
                problem = "invalid"
        if problem:
            counts[problem] += 1
            missing_ids.append(arxiv_id)
            rows.append([arxiv_id, problem, False, None, None, None])
            continue
        complete = record["status"] != "count_mismatch"
        counts["saved"] += 1
        counts["completed"] += complete
        counts[record["status"]] += 1
        counts["version_metadata_outdated"] += (
            record.get("applies_to_versions") != publication["versions"]
        )
        refs["total"] += record["n_refs"]
        refs["with_arxiv_id"] += record["n_refs_with_arxiv"]
        refs["without_s2_id"] += record["n_refs_without_s2_id"]
        rows.append(
            [
                arxiv_id,
                record["status"],
                complete,
                record["n_refs"],
                record["n_refs_with_arxiv"],
                record["n_refs_without_s2_id"],
            ]
        )

    counts["pending"] = len(missing_ids)
    counts["paper_crawl_rate"] = (
        counts["saved"] / counts["selected"] if counts["selected"] else None
    )
    counts["references_success_rate"] = (
        (counts["ok"] + counts["empty_refs"]) / counts["selected"]
        if counts["selected"]
        else None
    )
    refs["ref_arxiv_rate"] = (
        refs["with_arxiv_id"] / refs["total"] if refs["total"] else None
    )
    csv_saved = None
    if csv_path:
        buffer = io.StringIO(newline="")
        writer = csv.writer(buffer)
        writer.writerow(CSV_COLUMNS)
        writer.writerows(rows)
        try:
            write_bytes_atomic(csv_path, buffer.getvalue().encode("utf-8"))
            csv_saved = os.path.abspath(csv_path)
        except OSError as exc:
            warnings.append(f"csv_persistence_error: {exc}")
            logger.warning("Cannot save the statistics CSV (%s).", type(exc).__name__)
    return {
        "generated_at": timestamp(),
        "scope": "selected_papers_index",
        "output_path": os.path.abspath(out_dir),
        "papers": counts,
        "references": refs,
        "missing_ids": missing_ids,
        "diagnostics": diagnostics,
        "warnings": warnings,
        "storage": {"selected_output_bytes": refs_size(out_dir, publications)},
        "csv_saved": csv_saved,
        "definitions": {
            "paper_crawl_rate": "saved records / selected publications; includes not_in_s2 and count_mismatch",
            "completed": "verified records; excludes count_mismatch",
            "pending": "publications without a usable saved record",
            "references_success_rate": "(ok + empty_refs) / selected publications",
            "reference_counts": "all saved entries including count_mismatch, not deduplicated citations",
        },
    }
