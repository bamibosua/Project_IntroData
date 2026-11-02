"""
Đo đạc cho module crawl references (không đo toàn hệ thống, không đo discovery).

  - ResourceMonitor : RAM đỉnh / trung bình (lấy mẫu psutil định kỳ ở thread riêng)
  - dir_size        : dung lượng thư mục (tương đương `du`)
  - refs_size       : dung lượng riêng của các file references.json (thư mục output còn chứa dữ liệu của module khác)
  - RunMetrics      : thời gian, request, 429, số bài theo trạng thái của MỘT lượt chạy
  - summarize_output: thống kê dữ liệu trên toàn bộ output + gộp hiệu năng của mọi lượt chạy
"""
import csv
import datetime
import glob
import json
import os
import statistics
import sys
import threading
import time

import psutil

import config

try:                       # Windows không có module resource
    import resource
except ImportError:        # pragma: no cover
    resource = None

META_DIR_NAME = "references_meta"   # log và metrics đặt ngang hàng với thư mục papers, KHÔNG nằm trong đó


def default_meta_dir(out_dir):
    """Thư mục log/metrics mặc định: <cha của out_dir>/references_meta (vd. data/output/references_meta)."""
    return os.path.join(os.path.dirname(os.path.abspath(out_dir)), META_DIR_NAME)


# ----------------------------------------------------------------------
def dir_size(path):
    """Tổng dung lượng (byte) của mọi file dưới `path`."""
    total = 0
    for root, _, files in os.walk(path):
        for f in files:
            try:
                total += os.path.getsize(os.path.join(root, f))
            except OSError:
                pass
    return total


def refs_size(out_dir):
    """Tổng dung lượng các file <out_dir>/<thư mục bài>/references.json (chỉ phần do crawler này tạo ra)."""
    total = 0
    try:
        with os.scandir(out_dir) as it:
            for d in it:
                if d.is_dir() and not d.name.startswith("_"):
                    try:
                        total += os.path.getsize(os.path.join(d.path, "references.json"))
                    except OSError:
                        pass
    except OSError:
        pass
    return total


def _percentile(sorted_vals, q):
    if not sorted_vals:
        return None
    return sorted_vals[min(len(sorted_vals) - 1, int(round(q * (len(sorted_vals) - 1))))]


# ----------------------------------------------------------------------
class ResourceMonitor:
    """Lấy mẫu RSS (tiến trình hiện tại + tiến trình con) mỗi `interval` giây."""

    def __init__(self, interval=None):
        self.interval = config.RAM_SAMPLE_INTERVAL if interval is None else interval
        self._proc = psutil.Process()
        self._stop = threading.Event()
        self._thread = None
        self.n, self._sum, self.peak = 0, 0, 0

    def _rss(self):
        total = self._proc.memory_info().rss
        for c in self._proc.children(recursive=True):
            try:
                total += c.memory_info().rss
            except psutil.Error:
                pass
        return total

    def _sample(self):
        v = self._rss()
        self.n += 1
        self._sum += v
        self.peak = max(self.peak, v)

    def _loop(self):
        while not self._stop.wait(self.interval):
            self._sample()

    def __enter__(self):
        self._sample()
        self._thread = threading.Thread(target=self._loop, daemon=True)
        self._thread.start()
        return self

    def __exit__(self, *exc):
        self._stop.set()
        self._thread.join()
        self._sample()

    def as_dict(self):
        kernel_peak = None
        if resource is not None:
            r = resource.getrusage(resource.RUSAGE_SELF).ru_maxrss
            kernel_peak = round(r / (1e6 if sys.platform == "darwin" else 1e3), 1)  # Linux: KB, macOS: byte
        return {
            "peak_rss_mb": round(self.peak / 1e6, 1),
            "avg_rss_mb": round(self._sum / self.n / 1e6, 1) if self.n else None,
            "n_samples": self.n,
            "sample_interval_sec": self.interval,
            "peak_rss_kernel_mb": kernel_peak,      # đối chiếu độc lập với ru_maxrss của hệ điều hành
        }


# ----------------------------------------------------------------------
class RunMetrics:
    """Gom số liệu của một lượt chạy."""

    def __init__(self, cfg):
        self.cfg = cfg
        self.batches = []
        self.started_at = datetime.datetime.now().astimezone().isoformat(timespec="seconds")
        self._t0 = time.perf_counter()

    def add_batch(self, b):
        self.batches.append(b)

    def finalize(self, monitor, disk_before, disk_after, interrupted=False):
        wall = time.perf_counter() - self._t0
        B = self.batches
        n_papers = sum(b["n_papers"] for b in B)
        keys = ("ok", "empty_refs", "not_in_s2", "error")
        req = {k: sum(b["req"][k] for b in B) for k in
               ("requests", "retries_429", "sleep_429_sec", "retries_other", "backoff_sec", "wait_sec", "bytes")}
        work = sum(b["work_sec"] for b in B)
        return {
            "started_at": self.started_at,
            "finished_at": datetime.datetime.now().astimezone().isoformat(timespec="seconds"),
            "interrupted": interrupted,
            "config": self.cfg,
            "papers": {"attempted": n_papers, **{k: sum(b["counts"][k] for b in B) for k in keys}},
            "references": {"total": sum(b["refs_total"] for b in B),
                           "with_arxiv_id": sum(b["refs_with_arxiv"] for b in B)},
            "truncation_guard": {k: sum(b["truncation"][k] for b in B) for k in ("checked", "fixed", "unverified", "count_mismatch")},
            "batches": len(B),
            "batch_splits": sum(b.get("splits", 0) for b in B),
            "requests": {k: round(v, 2) if isinstance(v, float) else v for k, v in req.items()},
            "time": {
                "total_wall_sec": round(wall, 2),
                "work_sec": round(work, 2),    # thời gian làm việc thật (không tính nghỉ giữ chu kỳ / nghỉ 429)
                "avg_wall_sec_per_paper": round(wall / n_papers, 3) if n_papers else None,
                "avg_work_sec_per_paper": round(work / n_papers, 4) if n_papers else None,
                "avg_wall_sec_per_batch": round(wall / len(B), 2) if B else None,
            },
            "memory": monitor.as_dict(),
            "disk": {
                "output_bytes_before": disk_before,
                "output_bytes_after": disk_after,
                "bytes_written_this_run": max(0, disk_after - disk_before),
                "max_disk_note": "Crawler chỉ ghi thêm, không tạo file tạm lớn, nên dung lượng đỉnh = dung lượng cuối.",
            },
        }


# ----------------------------------------------------------------------
CSV_COLUMNS = ("arxiv_id", "status", "n_refs", "n_refs_with_arxiv", "n_refs_without_s2_id",
               "s2_reference_count", "truncation_fixed", "reference_count_mismatch", "source")


def summarize_output(out_dir, papers_file_ids=None, csv_path=None, meta_dir=None):
    """Thống kê dữ liệu trên mọi references.json trong out_dir, kèm hiệu năng gộp từ các lượt chạy.
    `papers_file_ids`: danh sách ID đầu vào (nếu có) để đếm số bài chưa có bản ghi (lỗi hoặc chưa chạy).
    `csv_path`: nếu có, ghi thêm một dòng cho mỗi bài (nhỏ, dùng để vẽ phân phối trong báo cáo).
    `meta_dir`: nơi chứa các file run_*.json (mặc định default_meta_dir(out_dir))."""
    meta_dir = meta_dir or default_meta_dir(out_dir)
    rows = []
    status = {"ok": 0, "empty_refs": 0, "not_in_s2": 0}
    refs_per_paper, refs_ok = [], []
    total_refs = with_arxiv = by_ext = by_doi = no_s2 = fixed = unverified = mismatch = empty_but_counted = 0
    for path in glob.glob(os.path.join(out_dir, "*", "references.json")):
        if os.path.basename(os.path.dirname(path)).startswith("_"):
            continue
        try:
            with open(path, encoding="utf-8") as f:
                rec = json.load(f)
        except (OSError, ValueError):
            continue
        status[rec.get("status", "error")] = status.get(rec.get("status", "error"), 0) + 1
        rows.append([rec.get(c) if c != "truncation_fixed" and c != "reference_count_mismatch"
                     else bool(rec.get(c)) for c in CSV_COLUMNS])
        n = rec.get("n_refs", 0)
        refs_per_paper.append(n)
        if rec.get("status") == "ok":
            refs_ok.append(n)
        total_refs += n
        with_arxiv += rec.get("n_refs_with_arxiv", 0)
        no_s2 += rec.get("n_refs_without_s2_id", 0)
        fixed += bool(rec.get("truncation_fixed"))
        unverified += bool(rec.get("truncation_unverified"))
        mismatch += bool(rec.get("reference_count_mismatch"))
        if rec.get("status") == "empty_refs" and (rec.get("s2_reference_count") or 0) > 0:
            empty_but_counted += 1
        for r in rec.get("references", []):
            if r.get("ref_arxiv_id"):
                by_ext += r.get("arxiv_id_source") == "externalIds"
                by_doi += r.get("arxiv_id_source") == "doi"

    if csv_path:
        rows.sort(key=lambda r: str(r[0]))
        with open(csv_path, "w", newline="", encoding="utf-8") as f:
            w = csv.writer(f)
            w.writerow(CSV_COLUMNS)
            w.writerows(rows)

    n_records = sum(status.values())
    srt = sorted(refs_per_paper)
    out = {
        "generated_at": datetime.datetime.now().astimezone().isoformat(timespec="seconds"),
        "definitions": {
            "paper_success_rate": "số bài có danh sách reference (status ok) / số bài đã có bản ghi",
            "paper_crawl_rate": "số bài đã có bản ghi (ok, empty_refs, not_in_s2) / số bài đầu vào (nếu cung cấp)",
            "ref_arxiv_rate": "reference có arXiv ID / tổng reference",
            "ref_s2_resolution_rate": "reference có Semantic Scholar ID / tổng reference",
        },
        "papers": {
            "records": n_records, **status,
            "paper_success_rate": round(status["ok"] / n_records, 4) if n_records else None,
        },
        "references": {
            "total": total_refs,
            "with_arxiv_id": with_arxiv,
            "arxiv_id_from_externalIds": by_ext,
            "arxiv_id_from_doi": by_doi,
            "without_s2_id": no_s2,
            "ref_arxiv_rate": round(with_arxiv / total_refs, 4) if total_refs else None,
            "ref_s2_resolution_rate": round(1 - no_s2 / total_refs, 4) if total_refs else None,
            "avg_per_paper_all_records": round(statistics.mean(refs_per_paper), 2) if refs_per_paper else None,
            "avg_per_paper_status_ok": round(statistics.mean(refs_ok), 2) if refs_ok else None,
            "median_per_paper": statistics.median(refs_per_paper) if refs_per_paper else None,
            "p90_per_paper": _percentile(srt, 0.9),
            "max_per_paper": srt[-1] if srt else None,
        },
        "truncation_guard": {"papers_fixed": fixed, "papers_unverified": unverified,
                             "papers_count_mismatch": mismatch,
                             "empty_refs_but_s2_count_positive": empty_but_counted},
        "storage": {"output_bytes": refs_size(out_dir), "meta_bytes": dir_size(meta_dir) if os.path.isdir(meta_dir) else 0},
    }
    if papers_file_ids is not None:
        n_in = len(papers_file_ids)
        out["papers"]["input_papers"] = n_in
        out["papers"]["without_record"] = max(0, n_in - n_records)
        out["papers"]["paper_crawl_rate"] = round(n_records / n_in, 4) if n_in else None

    # ---- hiệu năng gộp từ các lượt chạy ----
    runs = []
    for p in sorted(glob.glob(os.path.join(meta_dir, "run_*.json"))):
        try:
            with open(p, encoding="utf-8") as f:
                runs.append(json.load(f))
        except (OSError, ValueError):
            pass
    if runs:
        attempted = sum(r["papers"]["attempted"] for r in runs)
        wall = sum(r["time"]["total_wall_sec"] for r in runs)
        work = sum(r["time"]["work_sec"] for r in runs)
        samples = sum(r["memory"]["n_samples"] for r in runs) or 1
        out["performance"] = {
            "runs": len(runs),
            "papers_attempted_all_runs": attempted,
            "total_wall_sec": round(wall, 2),
            "avg_wall_sec_per_paper": round(wall / attempted, 3) if attempted else None,
            "avg_work_sec_per_paper": round(work / attempted, 4) if attempted else None,
            "requests": sum(r["requests"]["requests"] for r in runs),
            "retries_429": sum(r["requests"]["retries_429"] for r in runs),
            "sleep_429_sec": round(sum(r["requests"]["sleep_429_sec"] for r in runs), 1),
            "peak_rss_mb": max(r["memory"]["peak_rss_mb"] for r in runs),
            "avg_rss_mb": round(sum(r["memory"]["avg_rss_mb"] * r["memory"]["n_samples"] for r in runs) / samples, 1),
            "max_disk_bytes": max(r["disk"]["output_bytes_after"] for r in runs),
            "final_output_bytes": out["storage"]["output_bytes"],
        }
    return out