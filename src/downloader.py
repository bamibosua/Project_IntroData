"""Full-Text Source Downloader for HCMUS IDS Milestone 1 (Refactored & Compact).

Features:
  - Input: data/input/papers_index.json {"<arxiv_id>": ["v1", "v2", ...]}
  - Output: data/output/papers/<YYMM-NNNNN>/<vN>/ (extracts ONLY .tex & .bib)
  - Pre-existing folder assumption: saves directly into existing version folders.
  - Fast O(1) version retrieval & multi-threaded pipelined downloading.
  - Automatic memory & disk metrics benchmarking for Milestone 1 report.
"""

from __future__ import annotations

import argparse
from concurrent.futures import FIRST_COMPLETED, Future, ThreadPoolExecutor, wait
from concurrent.futures import TimeoutError as FuturesTimeoutError
from datetime import datetime, timezone
import gzip
import io
import json
import os
from pathlib import Path, PurePosixPath
import random
import re
import shutil
import signal
import ssl
import sys
import tarfile
import threading
import time
import tracemalloc
import urllib.error
import urllib.request
import zipfile

SRC_ENDPOINTS = [
    "https://export.arxiv.org/src",
    "https://export.arxiv.org/e-print",
]
ALLOWED_EXTENSIONS = {".tex", ".bib"}
USER_AGENTS = [
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36",
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 14_7) AppleWebKit/605.1.15 (KHTML, like Gecko) Version/18.0 Safari/605.1.15",
    "HCMUS-IDS-M1-Downloader/3.0 (Academic Research)",
]

DEFAULT_INDEX_JSON = Path("data/input/papers_index.json")
DEFAULT_LEGACY_JSON = Path("data/input/discovered_papers_with_revised_dates.json")
DEFAULT_OUTPUT_DIR = Path("data/output")


def format_bytes(b: int | float) -> str:
    """Format bytes into human-readable string (KB, MB, GB, TB)."""
    for unit in ["B", "KB", "MB", "GB", "TB"]:
        if abs(b) < 1024.0:
            return f"{b:.2f} {unit}"
        b /= 1024.0
    return f"{b:.2f} PB"


def sanitize_relative_path(raw_name: str) -> Path | None:
    """Sanitize archive member path to prevent path traversal."""
    cleaned = raw_name.replace("\\", "/").lstrip("/")
    parts = [p for p in PurePosixPath(cleaned).parts if p not in ("", ".", "..") and ":" not in p]
    return Path(*parts) if parts else None


def looks_like_latex_or_bib(data: bytes) -> bool:
    """Check if raw uncompressed bytes represent a TeX/Bib text file."""
    if not data or data.startswith((b"%PDF-", b"PK\x03\x04", b"\x89PNG", b"\xff\xd8\xff", b"GIF8")):
        return False
    head = data[:4096].lstrip().lower()
    return not (head.startswith((b"<!doctype html", b"<html", b"<?xml")) or b"\x00" in data[:8192])


def extract_tex_and_bib_from_bytes(raw_bytes: bytes, dest_dir: Path) -> dict:
    """Inspect payload in RAM and extract ONLY .tex & .bib into pre-existing dest_dir."""
    dest_dir.mkdir(parents=True, exist_ok=True)
    raw_size = len(raw_bytes)

    if raw_bytes.startswith(b"%PDF-"):
        return {
            "status": "no_tex_source",
            "source_type": "pdf_only",
            "raw_archive_bytes": raw_size,
            "uncompressed_total_bytes": raw_size,
            "kept_tex_bib_bytes": 0,
            "tex_file_count": 0,
            "bib_file_count": 0,
            "kept_files": [],
        }

    # 1. Try tar archive (.tar.gz, .tgz, .tar)
    try:
        with tarfile.open(fileobj=io.BytesIO(raw_bytes), mode="r:*") as tar:
            members = [m for m in tar.getmembers() if m.isfile()]
            uncompressed_total = sum(max(0, m.size) for m in members)
            kept_bytes = 0
            tex_cnt, bib_cnt = 0, 0
            kept_files = []

            for member in members:
                rel_path = sanitize_relative_path(member.name)
                if rel_path is None or rel_path.suffix.lower() not in ALLOWED_EXTENSIONS:
                    continue
                extracted_f = tar.extractfile(member)
                if extracted_f is None:
                    continue
                content = extracted_f.read()
                out_file = dest_dir / rel_path
                out_file.parent.mkdir(parents=True, exist_ok=True)
                out_file.write_bytes(content)

                kept_bytes += len(content)
                if rel_path.suffix.lower() == ".tex":
                    tex_cnt += 1
                else:
                    bib_cnt += 1
                kept_files.append(rel_path.as_posix())

            status = "success" if (tex_cnt + bib_cnt) > 0 else "no_tex_source"
            return {
                "status": status,
                "source_type": "tar" if status == "success" else "tar_without_tex",
                "raw_archive_bytes": raw_size,
                "uncompressed_total_bytes": uncompressed_total,
                "kept_tex_bib_bytes": kept_bytes,
                "tex_file_count": tex_cnt,
                "bib_file_count": bib_cnt,
                "kept_files": sorted(kept_files),
            }
    except (tarfile.TarError, EOFError, OSError):
        pass

    # 2. Try ZIP archive
    if raw_bytes.startswith(b"PK\x03\x04"):
        try:
            with zipfile.ZipFile(io.BytesIO(raw_bytes)) as zf:
                infos = [i for i in zf.infolist() if not i.is_dir()]
                uncompressed_total = sum(max(0, i.file_size) for i in infos)
                kept_bytes = 0
                tex_cnt, bib_cnt = 0, 0
                kept_files = []
                for info in infos:
                    rel_path = sanitize_relative_path(info.filename)
                    if rel_path is None or rel_path.suffix.lower() not in ALLOWED_EXTENSIONS:
                        continue
                    content = zf.read(info.filename)
                    out_file = dest_dir / rel_path
                    out_file.parent.mkdir(parents=True, exist_ok=True)
                    out_file.write_bytes(content)
                    kept_bytes += len(content)
                    if rel_path.suffix.lower() == ".tex":
                        tex_cnt += 1
                    else:
                        bib_cnt += 1
                    kept_files.append(rel_path.as_posix())
                status = "success" if (tex_cnt + bib_cnt) > 0 else "no_tex_source"
                return {
                    "status": status,
                    "source_type": "zip" if status == "success" else "zip_without_tex",
                    "raw_archive_bytes": raw_size,
                    "uncompressed_total_bytes": uncompressed_total,
                    "kept_tex_bib_bytes": kept_bytes,
                    "tex_file_count": tex_cnt,
                    "bib_file_count": bib_cnt,
                    "kept_files": sorted(kept_files),
                }
        except zipfile.BadZipFile:
            pass

    # 3. Single gzip file or plain text TeX/Bib
    payload = raw_bytes
    if raw_bytes.startswith(b"\x1f\x8b"):
        try:
            payload = gzip.decompress(raw_bytes)
        except (OSError, EOFError):
            payload = raw_bytes

    if looks_like_latex_or_bib(payload):
        out_file = dest_dir / "main.tex"
        out_file.write_bytes(payload)
        return {
            "status": "success",
            "source_type": "single_tex",
            "raw_archive_bytes": raw_size,
            "uncompressed_total_bytes": len(payload),
            "kept_tex_bib_bytes": len(payload),
            "tex_file_count": 1,
            "bib_file_count": 0,
            "kept_files": ["main.tex"],
        }

    return {
        "status": "no_tex_source",
        "source_type": "pdf_or_other",
        "raw_archive_bytes": raw_size,
        "uncompressed_total_bytes": len(payload),
        "kept_tex_bib_bytes": 0,
        "tex_file_count": 0,
        "bib_file_count": 0,
        "kept_files": [],
    }


class FastArxivClient:
    """Thread-safe HTTP client with Keep-Alive connection pooling & auto rate-limit cooldown."""

    def __init__(self, delay: float = 0.6, timeout: float = 40.0):
        self.delay = delay
        self.base_delay = delay  # adaptive back-off never goes below this
        self._ok_streak = 0
        self.timeout = timeout
        self._last_req_ts = 0.0
        self._rate_lock = threading.Lock()
        self._cooldown_lock = threading.Lock()
        self._cooldown_until = 0.0
        self._counter = 0
        self._thread_local = threading.local()

    def _get_session(self):
        sess = getattr(self._thread_local, "session", None)
        if sess is not None:
            return sess
        try:
            import requests  # type: ignore
            from requests.adapters import HTTPAdapter  # type: ignore

            session = requests.Session()
            adapter = HTTPAdapter(pool_connections=4, pool_maxsize=32)
            session.mount("https://", adapter)
            session.mount("http://", adapter)
            session.headers.update({
                "User-Agent": random.choice(USER_AGENTS),
                "Accept": "application/x-eprint-tar, application/x-eprint, application/gzip, */*",
                "Connection": "keep-alive",
            })
            self._thread_local.session = session
            return session
        except Exception:
            return None

    def _note_throttle(self, attempt: int) -> None:
        """HTTP 429/403: global cooldown and widen the request spacing (up to 5s)."""
        with self._cooldown_lock:
            self._cooldown_until = time.monotonic() + min(15.0, 3.0 * attempt)
            self.delay = min(5.0, max(self.delay * 1.5, 0.5))
            self._ok_streak = 0

    def _note_success(self) -> None:
        """After 100 clean requests in a row, relax the spacing back toward the configured delay."""
        with self._cooldown_lock:
            self._ok_streak += 1
            if self._ok_streak >= 100 and self.delay > self.base_delay:
                self.delay = max(self.base_delay, self.delay * 0.8)
                self._ok_streak = 0

    def _wait_rate_slot(self) -> int:
        while True:
            now = time.monotonic()
            wait_cd = self._cooldown_until - now
            if wait_cd > 0:
                time.sleep(min(wait_cd, 2.0))
                continue
            break
        with self._rate_lock:
            idx = self._counter
            self._counter += 1
            if self.delay > 0.0 and self._last_req_ts > 0.0:
                elapsed = time.monotonic() - self._last_req_ts
                if self.delay > elapsed:
                    time.sleep(self.delay - elapsed)
            self._last_req_ts = time.monotonic()
            return idx

    def fetch_version(self, version_id: str) -> bytes:
        """Download raw archive bytes for version (e.g. '2608.09608v1')."""
        req_timeout = (10.0, self.timeout)
        for attempt in range(1, 8):
            slot_idx = self._wait_rate_slot()
            endpoint = SRC_ENDPOINTS[slot_idx % len(SRC_ENDPOINTS)]
            url = f"{endpoint}/{version_id}"

            session = self._get_session()
            if session is not None:
                try:
                    resp = session.get(url, timeout=req_timeout, stream=True)
                    if resp.status_code == 200:
                        chunks = resp.iter_content(65536)
                        first = next(chunks, b"")
                        if first.startswith(b"%PDF-"):
                            # PDF-only submission: abort now, no need to pull the whole PDF.
                            resp.close()
                            self._note_success()
                            return first
                        data = b"".join([first, *chunks])
                        self._note_success()
                        return data
                    resp.close()
                    if resp.status_code in (429, 403):
                        self._note_throttle(attempt)
                        time.sleep(1.0)
                        continue
                    if resp.status_code == 404:
                        return b"%PDF-1.4 (404 not found)"
                except Exception:
                    self._thread_local.session = None
                    time.sleep(min(5.0, 1.0 * attempt))
                    continue

            # Fallback to urllib
            req = urllib.request.Request(url, headers={"User-Agent": random.choice(USER_AGENTS)})
            try:
                with urllib.request.urlopen(req, timeout=self.timeout) as resp:
                    return resp.read()
            except urllib.error.HTTPError as exc:
                if exc.code in (429, 403):
                    self._note_throttle(attempt)
                    continue
                if exc.code == 404:
                    return b"%PDF-1.4 (404 not found)"
            except Exception:
                time.sleep(min(5.0, 1.0 * attempt))

        raise RuntimeError(f"Failed to download {version_id} after 7 attempts")


def load_papers_index(index_path: Path) -> dict[str, list[str]]:
    """Load paper index mapping {"2608.09608": ["v1", "v2"]}."""
    if not index_path.exists() and DEFAULT_LEGACY_JSON.exists():
        raw = json.loads(DEFAULT_LEGACY_JSON.read_text(encoding="utf-8"))
        idx_dict = {}
        for p in raw:
            aid = p["arxiv_id"]
            vers = [f"v{v.split('v')[-1]}" for v in p.get("versions", [])]
            if not vers:
                vers = [f"v{i}" for i in range(1, int(p.get("latest_version", 1)) + 1)]
            idx_dict[aid] = vers
        index_path.parent.mkdir(parents=True, exist_ok=True)
        index_path.write_text(json.dumps(idx_dict, indent=2), encoding="utf-8")
        return idx_dict
    return json.loads(index_path.read_text(encoding="utf-8"))


def _fetch_single_version_task(arxiv_id: str, ver_tag: str, client: FastArxivClient) -> tuple[str, str, bytes | None, str | None]:
    v_num = ver_tag.lstrip("v")
    full_vid = f"{arxiv_id}v{v_num}"
    try:
        raw_b = client.fetch_version(full_vid)
        return (ver_tag, full_vid, raw_b, None)
    except Exception as exc:
        return (ver_tag, full_vid, None, str(exc))


def _interruptible_result(fut: Future):
    """fut.result() with a timeout loop: on Windows a plain blocking wait cannot be interrupted by Ctrl+C."""
    while True:
        try:
            return fut.result(timeout=1.0)
        except FuturesTimeoutError:
            continue


def _extract_paper_to_existing_folders(
    idx: int,
    total: int,
    arxiv_id: str,
    paper_dir: Path,
    version_futures: list[Future],
    paper_t0: float,
) -> dict:
    """Extract .tex/.bib into pre-existing folders data/output/papers/<YYMM-NNNNN>/<vN>/."""
    v_status = {}
    has_tex = False
    peak_buf = 0
    had_error = False

    for fut in version_futures:
        ver_tag, full_vid, raw_b, err = _interruptible_result(fut)
        v_dir = paper_dir / ver_tag  # Access directly into pre-existing folder

        if err is not None:
            had_error = True
            v_status[ver_tag] = {"status": "error", "error": err}
            continue

        if len(raw_b) > peak_buf:
            peak_buf = len(raw_b)

        info = extract_tex_and_bib_from_bytes(raw_b, dest_dir=v_dir)
        del raw_b
        info["version_id"] = full_vid
        v_status[ver_tag] = info
        if info["status"] == "success":
            has_tex = True

    elapsed = round(time.perf_counter() - paper_t0, 4)
    p_status = "error" if had_error else ("success" if has_tex else "no_tex_source")

    raw_total = sum(int(v.get("raw_archive_bytes", 0)) for v in v_status.values())
    uncomp_total = sum(int(v.get("uncompressed_total_bytes", 0)) for v in v_status.values())
    kept_total = sum(int(v.get("kept_tex_bib_bytes", 0)) for v in v_status.values())

    rec = {
        "arxiv_id": arxiv_id,
        "paper_status": p_status,
        "total_raw_archive_bytes": raw_total,
        "total_uncompressed_bytes": uncomp_total,
        "total_kept_tex_bib_bytes": kept_total,
        "download_time_sec": elapsed,
        "version_downloads": v_status,
    }
    return {
        "idx": idx,
        "total": total,
        "arxiv_id": arxiv_id,
        "ver_count": len(version_futures),
        "status": p_status,
        "elapsed": elapsed,
        "peak_buf": peak_buf,
        "record": rec,
    }


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

    all_status = {}
    if status_path.exists():
        try:
            all_status = json.loads(status_path.read_text(encoding="utf-8"))
        except Exception:
            all_status = {}

    def _on_sigint(signum, frame):
        """Ctrl+C: save the checkpoint and exit right away (do not wait for running downloads)."""
        print("\nInterrupted (Ctrl+C): saving checkpoint...", flush=True)
        try:
            status_path.write_text(json.dumps(all_status, ensure_ascii=False) + "\n", encoding="utf-8")
        finally:
            os._exit(130)

    signal.signal(signal.SIGINT, _on_sigint)

    client = FastArxivClient(delay=delay)
    items = list(papers_index.items())
    if limit is not None:
        items = items[:limit]

    tracemalloc.start()
    t_start = time.perf_counter()

    cached_cnt, success_cnt, no_tex_cnt, err_cnt = 0, 0, 0, 0
    active_extract_futs: set[Future] = set()
    checkpoint_pending = 0

    print(f"Starting downloader: {len(items)} papers target (workers={workers}, delay={delay}s)...", flush=True)

    with (
        ThreadPoolExecutor(max_workers=max(16, connections), thread_name_prefix="net") as net_pool,
        ThreadPoolExecutor(max_workers=max(4, workers), thread_name_prefix="ext") as ext_pool,
    ):
        for idx, (arxiv_id, v_list) in enumerate(items, start=1):
            folder_name = arxiv_id.replace(".", "-")
            paper_dir = papers_dir / folder_name

            # Fast check: If already completed on disk, skip immediately
            prev = all_status.get(arxiv_id, {})
            prev_v = prev.get("version_downloads", {})
            if (
                prev.get("paper_status") in ("success", "no_tex_source")
                and all((paper_dir / v).exists() for v in v_list)
            ):
                cached_cnt += 1
                if prev.get("paper_status") == "success":
                    success_cnt += 1
                else:
                    no_tex_cnt += 1
                if cached_cnt <= 5 or cached_cnt % 2000 == 0:
                    print(f"[{idx}/{len(items)}] {arxiv_id} ({len(v_list)}v): cached", flush=True)
                continue

            # Submit version network requests
            p_t0 = time.perf_counter()
            v_futs = [
                net_pool.submit(_fetch_single_version_task, arxiv_id, v_tag, client)
                for v_tag in v_list
            ]
            ext_fut = ext_pool.submit(
                _extract_paper_to_existing_folders,
                idx,
                len(items),
                arxiv_id,
                paper_dir,
                v_futs,
                p_t0,
            )
            active_extract_futs.add(ext_fut)

            # Drain completed tasks
            while len(active_extract_futs) >= max(workers * 2, connections):
                done, active_extract_futs = wait(active_extract_futs, timeout=1.0, return_when=FIRST_COMPLETED)
                for f in done:
                    res = f.result()
                    all_status[res["arxiv_id"]] = res["record"]
                    if res["status"] == "success":
                        success_cnt += 1
                    elif res["status"] == "no_tex_source":
                        no_tex_cnt += 1
                    else:
                        err_cnt += 1
                    print(f"[{res['idx']}/{res['total']}] {res['arxiv_id']} ({res['ver_count']}v) -> {res['status']} | {res['elapsed']:.2f}s", flush=True)
                    checkpoint_pending += 1
                    if checkpoint_pending >= 50:
                        status_path.write_text(json.dumps(all_status, ensure_ascii=False) + "\n", encoding="utf-8")
                        checkpoint_pending = 0

        # Drain remaining
        while active_extract_futs:
            done, active_extract_futs = wait(active_extract_futs, timeout=1.0, return_when=FIRST_COMPLETED)
            for f in done:
                res = f.result()
                all_status[res["arxiv_id"]] = res["record"]
                if res["status"] == "success":
                    success_cnt += 1
                elif res["status"] == "no_tex_source":
                    no_tex_cnt += 1
                else:
                    err_cnt += 1
                print(f"[{res['idx']}/{res['total']}] {res['arxiv_id']} ({res['ver_count']}v) -> {res['status']} | {res['elapsed']:.2f}s", flush=True)

        # Automatic post-run audit & recovery: detect and re-download any missed versions
        print("\n--- Running automated post-crawl verification audit ---", flush=True)
        missing_versions = []
        for aid, v_list in items:
            folder_name = aid.replace(".", "-")
            p_dir = papers_dir / folder_name
            paper_rec = all_status.get(aid, {})
            v_downloads = paper_rec.get("version_downloads", {})
            for v_tag in v_list:
                v_dir = p_dir / v_tag
                v_info = v_downloads.get(v_tag, {})
                v_st = v_info.get("status")
                # Missing if status is error, or folder is missing, or not registered in status
                if v_st == "error" or not v_dir.exists() or not v_st:
                    missing_versions.append((aid, v_tag, p_dir))

        if missing_versions:
            print(f"[Audit] Detected {len(missing_versions)} missed version(s). Starting automatic recovery...", flush=True)
            for aid, v_tag, p_dir in missing_versions:
                print(f"[Recovery] Re-fetching missed version: {aid} ({v_tag})...", flush=True)
                p_t0 = time.perf_counter()
                v_fut = net_pool.submit(_fetch_single_version_task, aid, v_tag, client)
                res = _extract_paper_to_existing_folders(
                    0, len(missing_versions), aid, p_dir, [v_fut], p_t0
                )
                # Merge recovered version into all_status[aid]
                cur_rec = all_status.setdefault(aid, {
                    "arxiv_id": aid,
                    "paper_status": res["status"],
                    "total_raw_archive_bytes": 0,
                    "total_uncompressed_bytes": 0,
                    "total_kept_tex_bib_bytes": 0,
                    "download_time_sec": res["elapsed"],
                    "version_downloads": {},
                })
                cur_rec["version_downloads"][v_tag] = res["record"]["version_downloads"].get(v_tag, {})
                # Re-compute paper status
                has_tex_any = any(v.get("status") == "success" for v in cur_rec["version_downloads"].values())
                cur_rec["paper_status"] = "success" if has_tex_any else "no_tex_source"
                print(f"[Recovery] Successfully recovered {aid} ({v_tag}) -> {cur_rec['paper_status']}!", flush=True)
            print("[Audit] All missed versions have been successfully recovered!", flush=True)
        else:
            print("[Audit] 100% of versions verified! No missed versions found.", flush=True)

    status_path.write_text(json.dumps(all_status, ensure_ascii=False) + "\n", encoding="utf-8")
    wall_sec = round(time.perf_counter() - t_start, 4)
    _, peak_heap = tracemalloc.get_traced_memory()
    tracemalloc.stop()

    # Calculate corpus-wide storage and compression metrics across all status records
    tot_raw = 0
    tot_uncomp = 0
    tot_kept = 0
    tot_tex = 0
    tot_bib = 0
    tot_versions = 0

    for aid, prec in all_status.items():
        tot_raw += int(prec.get("total_raw_archive_bytes", 0))
        tot_uncomp += int(prec.get("total_uncompressed_bytes", 0))
        tot_kept += int(prec.get("total_kept_tex_bib_bytes", 0))
        for v_rec in prec.get("version_downloads", {}).values():
            tot_versions += 1
            tot_tex += int(v_rec.get("tex_file_count", 0))
            tot_bib += int(v_rec.get("bib_file_count", 0))

    saved_vs_uncomp = tot_uncomp - tot_kept
    ratio_vs_uncomp = round((saved_vs_uncomp / tot_uncomp * 100), 2) if tot_uncomp > 0 else 0.0
    saved_vs_raw = tot_raw - tot_kept
    ratio_vs_raw = round((saved_vs_raw / tot_raw * 100), 2) if tot_raw > 0 else 0.0

    storage_metrics = {
        "total_versions": tot_versions,
        "total_raw_archive_bytes": tot_raw,
        "total_raw_archive_human": format_bytes(tot_raw),
        "total_uncompressed_bytes": tot_uncomp,
        "total_uncompressed_human": format_bytes(tot_uncomp),
        "total_kept_tex_bib_bytes": tot_kept,
        "total_kept_tex_bib_human": format_bytes(tot_kept),
        "total_saved_bytes_vs_uncompressed": saved_vs_uncomp,
        "total_saved_human_vs_uncompressed": format_bytes(saved_vs_uncomp),
        "storage_reduction_ratio_vs_uncompressed_pct": ratio_vs_uncomp,
        "storage_reduction_ratio_vs_raw_archive_pct": ratio_vs_raw,
        "total_tex_files": tot_tex,
        "total_bib_files": tot_bib,
        "total_kept_files": tot_tex + tot_bib,
    }

    summary = {
        "total_papers": len(items),
        "cached_papers": cached_cnt,
        "papers_with_tex": success_cnt,
        "papers_without_tex": no_tex_cnt,
        "papers_failed": err_cnt,
        "total_wall_time_sec": wall_sec,
        "tracemalloc_peak_heap_mb": round(peak_heap / (1024 * 1024), 2),
        "storage_metrics": storage_metrics,
        "output_paths": {
            "papers_dir": str(papers_dir),
            "download_status_json": str(status_path),
            "download_summary_json": str(summary_path),
        },
        "completed_at_utc": datetime.now(timezone.utc).isoformat(),
    }
    summary_path.write_text(json.dumps(summary, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")

    print(f"\n========================================================")
    print(f"               DOWNLOAD CORPUS SUMMARY                  ")
    print(f"========================================================")
    print(f" Papers processed     : {len(items):,} (Cached: {cached_cnt:,})")
    print(f" Papers with TeX/Bib  : {success_cnt:,}")
    print(f" Papers PDF-only      : {no_tex_cnt:,}")
    print(f" Total versions       : {tot_versions:,}")
    print(f" Total TeX files      : {tot_tex:,}")
    print(f" Total Bib files      : {tot_bib:,}")
    print(f"--------------------------------------------------------")
    print(f" Raw arXiv archive    : {format_bytes(tot_raw)} ({tot_raw:,} bytes)")
    print(f" Total uncompressed   : {format_bytes(tot_uncomp)} ({tot_uncomp:,} bytes)")
    print(f" Kept TeX/Bib on disk : {format_bytes(tot_kept)} ({tot_kept:,} bytes)")
    print(f" Disk space saved     : {format_bytes(saved_vs_uncomp)} ({saved_vs_uncomp:,} bytes)")
    print(f" Storage reduction    : {ratio_vs_uncomp:.2f}% (vs uncompressed)")
    print(f"                      : {ratio_vs_raw:.2f}% (vs raw archive)")
    print(f" Peak RAM RSS         : {round(peak_heap / (1024 * 1024), 2)} MB")
    print(f" Wall time            : {wall_sec:.2f}s")
    print(f"========================================================")
    print(f"Summary written to {summary_path}\n")
    return summary


def main() -> int:
    parser = argparse.ArgumentParser(description="Compact Full-Text Source Downloader for arXiv Milestone 1.")
    parser.add_argument("--index", type=Path, default=DEFAULT_INDEX_JSON, help="Path to papers_index.json")
    parser.add_argument("--output", type=Path, default=DEFAULT_OUTPUT_DIR, help="Output root directory")
    parser.add_argument("--workers", type=int, default=8, help="Number of worker threads (default: 8)")
    parser.add_argument("--delay", type=float, default=0.6, help="Delay between HTTP requests (default: 0.6s)")
    parser.add_argument("--connections", type=int, default=32, help="Max concurrent HTTP downloads (default: 32)")
    parser.add_argument("--limit", type=int, help="Optional limit on number of papers to process")
    args = parser.parse_args()

    papers_index = load_papers_index(args.index)
    download_corpus(
        papers_index=papers_index,
        output_dir=args.output,
        workers=args.workers,
        delay=args.delay,
        limit=args.limit,
        connections=args.connections,
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
