"""Full-Text Source Downloader for HCMUS IDS Milestone 1.

Supports:
  1. True Parallel Multi-Threaded HTTP Downloading (`ThreadPoolExecutor` across multiple
     arXiv source endpoints with configurable `--workers` and `--delay`).
  2. Automatic VPN / IP-Switch Recovery & Adaptive Backoff on HTTP 429/403 (never crashes
     or loses progress when rate-limited; resets socket pool on IP change and continues).
  3. Automatic Disk Cache Recovery (reconstructs `download_status.json` from existing
     `data/output/sources/<YYMM-NNNNN>/metadata.json` folders so previously downloaded
     papers are never re-downloaded).
  4. Optional Amazon S3 Bulk Repository Loader (`S3BulkSourceLoader`) if `.tar` bundles
     are placed in `data/input/s3_bulk/`.
"""

from __future__ import annotations

import argparse
from concurrent.futures import FIRST_COMPLETED, Future, ThreadPoolExecutor, wait
import ctypes
from ctypes import wintypes
from datetime import datetime, timezone
from email.message import Message
from email.utils import parsedate_to_datetime
import gzip
import io
import json
import os
from pathlib import Path, PurePosixPath
import random
import re
import shutil
import ssl
import statistics
import subprocess
import sys
import tarfile
import threading
import time
import tracemalloc
import urllib.error
import urllib.parse
import urllib.request
import xml.etree.ElementTree as ET
import zipfile

SRC_ENDPOINTS = [
    "https://export.arxiv.org/src",
    "https://export.arxiv.org/e-print",
]
API_ENDPOINT = "https://export.arxiv.org/api/query"
S3_BUCKET_SRC_URI = "s3://arxiv/src"
ATOM_NS = "{http://www.w3.org/2005/Atom}"
ARXIV_NS = "{http://arxiv.org/schemas/atom}"
VERSIONED_PATTERN = re.compile(r"^(\d{4}\.\d{4,5})v(\d+)$")
VERSIONED_SEARCH = re.compile(r"(\d{4}\.\d{4,5})v(\d+)$")
S3_MEMBER_PATTERN = re.compile(r"(?:^|/)(\d{4}\.\d{4,5})(?:v(\d+))?\.(gz|tgz|tar\.gz|pdf|zip)$", re.IGNORECASE)
ALLOWED_EXTENSIONS = {".tex", ".bib"}

USER_AGENTS = [
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36",
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 14_7) AppleWebKit/605.1.15 (KHTML, like Gecko) Version/18.0 Safari/605.1.15",
    "Mozilla/5.0 (X11; Linux x86_64; rv:132.0) Gecko/20100101 Firefox/132.0",
    "HCMUS-IDS-M1-SourceDownloader/3.0 (Academic Research Project)",
]

DEFAULT_INPUT_JSON = Path("data/input/discovered_papers_with_revised_dates.json")
LEGACY_INPUT_JSON = Path("data/discovered_papers_with_revised_dates.json")
DEFAULT_RANGES_JSON = Path("data/input/team_ranges.json")
DEFAULT_S3_DIR = Path("data/input/s3_bulk")
DEFAULT_OUTPUT_DIR = Path("data/output")
LEGACY_SOURCES_DIR = Path("data/sources")

DEFAULT_MEMBER_RANGES: dict[str, list[tuple[str, str]]] = {
    "Minh_Hang": [("2609.12834", "2609.17833")],
    "Trong_Nhan": [("2608.9007", "2608.14006")],
    "Vinh_Nguyen": [("2601.18156", "2601.23155")],
    "Yen_Vy": [("2511.11709", "2511.16708")],
}


class RateLimitError(RuntimeError):
    """Raised when arXiv returns HTTP 429 or 403 rate limit."""

    def __init__(self, message: str, retry_after: int | None = None):
        super().__init__(message)
        self.retry_after = retry_after


def parse_retry_after(value: str | None) -> int | None:
    """Parse HTTP Retry-After header into seconds if present."""
    if not value:
        return None
    value = value.strip()
    if value.isdecimal():
        return max(1, int(value))
    try:
        dt = parsedate_to_datetime(value)
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        delta = round((dt - datetime.now(timezone.utc)).total_seconds())
        return max(1, delta)
    except (TypeError, ValueError, IndexError):
        return None


def write_json_atomic(path: Path, data: object, compact: bool = False) -> None:
    """Atomically write JSON to disk to prevent corrupted checkpoints on interrupt."""
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp_path = path.with_suffix(f"{path.suffix}.{threading.get_ident()}.tmp")
    if compact:
        payload = json.dumps(data, ensure_ascii=False, separators=(",", ":")) + "\n"
    else:
        payload = json.dumps(data, indent=2, ensure_ascii=False) + "\n"
    tmp_path.write_text(payload, encoding="utf-8")
    tmp_path.replace(path)


def ensure_data_layout(input_path: Path, output_dir: Path) -> Path:
    """Ensure clean data/input and data/output directory structure and migrate legacy files."""
    if input_path == DEFAULT_INPUT_JSON and not DEFAULT_INPUT_JSON.exists() and LEGACY_INPUT_JSON.exists():
        DEFAULT_INPUT_JSON.parent.mkdir(parents=True, exist_ok=True)
        shutil.move(str(LEGACY_INPUT_JSON), str(DEFAULT_INPUT_JSON))
        print(f"Moved input file: {LEGACY_INPUT_JSON} -> {DEFAULT_INPUT_JSON}", flush=True)

    papers_dir = output_dir / "papers"
    papers_dir.mkdir(parents=True, exist_ok=True)

    if output_dir == DEFAULT_OUTPUT_DIR and LEGACY_SOURCES_DIR.exists() and LEGACY_SOURCES_DIR.is_dir():
        papers_dir.mkdir(parents=True, exist_ok=True)
        status_path = output_dir / "download_status.json"
        migrated_status: dict[str, dict] = {}
        if status_path.exists():
            try:
                loaded = json.loads(status_path.read_text(encoding="utf-8"))
                if isinstance(loaded, dict):
                    migrated_status = loaded
            except Exception:
                migrated_status = {}

        for item in LEGACY_SOURCES_DIR.iterdir():
            if item.is_dir():
                target = papers_dir / item.name
                old_meta_path = item / "metadata.json"
                if old_meta_path.exists():
                    try:
                        old_meta = json.loads(old_meta_path.read_text(encoding="utf-8"))
                        arxiv_id = old_meta.get("arxiv_id")
                        if arxiv_id and "version_downloads" in old_meta:
                            migrated_status[arxiv_id] = {
                                "arxiv_id": arxiv_id,
                                "paper_status": old_meta.get("paper_status", "success"),
                                "total_raw_archive_bytes": old_meta.get("total_raw_archive_bytes", 0),
                                "total_uncompressed_bytes": old_meta.get("total_uncompressed_bytes", 0),
                                "total_kept_tex_bib_bytes": old_meta.get("total_kept_tex_bib_bytes", 0),
                                "download_time_sec": old_meta.get("download_time_sec", 0.0),
                                "version_downloads": old_meta["version_downloads"],
                            }
                    except Exception:
                        pass
                if not target.exists():
                    shutil.move(str(item), str(target))
                else:
                    shutil.rmtree(item)
            else:
                item.unlink(missing_ok=True)

        if migrated_status:
            write_json_atomic(status_path, migrated_status)
        try:
            LEGACY_SOURCES_DIR.rmdir()
            print(f"Migrated legacy folder: {LEGACY_SOURCES_DIR} -> {papers_dir}", flush=True)
        except OSError:
            pass

    return input_path


def load_member_ranges(ranges_path: Path = DEFAULT_RANGES_JSON) -> dict[str, list[tuple[str, str]]]:
    """Load member ID ranges from data/input/team_ranges.json with fallback."""
    if not ranges_path.exists():
        return dict(DEFAULT_MEMBER_RANGES)
    try:
        raw = json.loads(ranges_path.read_text(encoding="utf-8"))
        result: dict[str, list[tuple[str, str]]] = {}
        for entry in raw.get("members", []):
            name = entry.get("member")
            if not name:
                continue
            pairs = [
                (r["start"], r["end"])
                for r in entry.get("ranges", [])
                if "start" in r and "end" in r
            ]
            if pairs:
                result[name] = pairs
        return result or dict(DEFAULT_MEMBER_RANGES)
    except Exception:
        return dict(DEFAULT_MEMBER_RANGES)


def get_process_memory_info() -> tuple[int, int]:
    """Return (current_rss_bytes, os_peak_rss_bytes) using psutil or OS APIs."""
    try:
        import psutil  # type: ignore

        mem = psutil.Process(os.getpid()).memory_info()
        rss = int(mem.rss)
        peak = int(getattr(mem, "peak_wset", rss) or rss)
        return rss, max(rss, peak)
    except Exception:
        pass

    if sys.platform == "win32":
        try:
            class PROCESS_MEMORY_COUNTERS(ctypes.Structure):
                _fields_ = [
                    ("cb", wintypes.DWORD),
                    ("PageFaultCount", wintypes.DWORD),
                    ("PeakWorkingSetSize", ctypes.c_size_t),
                    ("WorkingSetSize", ctypes.c_size_t),
                    ("QuotaPeakPagedPoolUsage", ctypes.c_size_t),
                    ("QuotaPagedPoolUsage", ctypes.c_size_t),
                    ("QuotaPeakNonPagedPoolUsage", ctypes.c_size_t),
                    ("QuotaNonPagedPoolUsage", ctypes.c_size_t),
                    ("PagefileUsage", ctypes.c_size_t),
                    ("PeakPagefileUsage", ctypes.c_size_t),
                ]

            pmc = PROCESS_MEMORY_COUNTERS()
            pmc.cb = ctypes.sizeof(PROCESS_MEMORY_COUNTERS)
            handle = ctypes.windll.kernel32.GetCurrentProcess()
            if ctypes.windll.psapi.GetProcessMemoryInfo(
                handle, ctypes.byref(pmc), pmc.cb
            ):
                return int(pmc.WorkingSetSize), int(pmc.PeakWorkingSetSize)
        except Exception:
            pass
    else:
        try:
            status_path = Path("/proc/self/status")
            if status_path.exists():
                rss = 0
                hwm = 0
                for line in status_path.read_text(encoding="utf-8").splitlines():
                    if line.startswith("VmRSS:"):
                        parts = line.split()
                        if len(parts) >= 2 and parts[1].isdigit():
                            rss = int(parts[1]) * 1024
                    elif line.startswith("VmHWM:"):
                        parts = line.split()
                        if len(parts) >= 2 and parts[1].isdigit():
                            hwm = int(parts[1]) * 1024
                return rss, max(rss, hwm)
        except Exception:
            pass

    if tracemalloc.is_tracing():
        current, peak = tracemalloc.get_traced_memory()
        return int(current), int(peak)
    return 0, 0


class MemoryMonitor:
    """Background periodic RAM sampler for auditable memory footprint benchmarking."""

    def __init__(self, interval_sec: float = 0.5):
        self.interval_sec = interval_sec
        self.samples: list[int] = []
        self.peak_rss_bytes: int = 0
        self._stop_event = threading.Event()
        self._lock = threading.Lock()
        self._thread: threading.Thread | None = None

    def sample_now(self) -> int:
        rss, os_peak = get_process_memory_info()
        with self._lock:
            if rss > 0:
                self.samples.append(rss)
            if max(rss, os_peak) > self.peak_rss_bytes:
                self.peak_rss_bytes = max(rss, os_peak)
        return rss

    def _run(self) -> None:
        while not self._stop_event.is_set():
            self.sample_now()
            self._stop_event.wait(self.interval_sec)

    def start(self) -> None:
        self.sample_now()
        self._stop_event.clear()
        self._thread = threading.Thread(target=self._run, daemon=True)
        self._thread.start()

    def stop(self) -> None:
        self._stop_event.set()
        if self._thread is not None:
            self._thread.join(timeout=2.0)
        self.sample_now()

    @property
    def avg_rss_bytes(self) -> int:
        with self._lock:
            if not self.samples:
                return 0
            return int(sum(self.samples) / len(self.samples))

    @property
    def sample_count(self) -> int:
        with self._lock:
            return len(self.samples)


def normalize_arxiv_id_tuple(arxiv_id: str) -> tuple[int, int]:
    """Convert '2608.09007' or '2608.9007' into (2608, 9007) for range comparison."""
    parts = arxiv_id.strip().split(".")
    if len(parts) != 2 or not (parts[0].isdigit() and parts[1].isdigit()):
        raise ValueError(f"Invalid arXiv ID format: {arxiv_id}")
    return int(parts[0]), int(parts[1])


def id_in_range(arxiv_id: str, start_id: str, end_id: str) -> bool:
    """Return True if arxiv_id falls within [start_id, end_id] inclusive."""
    try:
        ym, seq = normalize_arxiv_id_tuple(arxiv_id)
        ym_s, seq_s = normalize_arxiv_id_tuple(start_id)
        ym_e, seq_e = normalize_arxiv_id_tuple(end_id)
        return (ym_s, seq_s) <= (ym, seq) <= (ym_e, seq_e)
    except ValueError:
        return False


def ranges_overlap(start_a: str, end_a: str, start_b: str, end_b: str) -> bool:
    """Return True if [start_a, end_a] and [start_b, end_b] overlap."""
    try:
        a0 = normalize_arxiv_id_tuple(start_a)
        a1 = normalize_arxiv_id_tuple(end_a)
        b0 = normalize_arxiv_id_tuple(start_b)
        b1 = normalize_arxiv_id_tuple(end_b)
        return max(a0, b0) <= min(a1, b1)
    except ValueError:
        return False


def paper_folder_name(arxiv_id: str) -> str:
    """Convert arXiv ID '2511.11709' to suggested folder convention '2511-11709'."""
    return arxiv_id.strip().replace("/", "-").replace(".", "-")


def sanitize_relative_path(raw_name: str) -> Path | None:
    """Sanitize an archive member path to prevent path traversal outside version dir."""
    cleaned = raw_name.replace("\\", "/").lstrip("/")
    posix_path = PurePosixPath(cleaned)
    parts = [
        p for p in posix_path.parts
        if p not in ("", ".", "..") and ":" not in p
    ]
    if not parts:
        return None
    return Path(*parts)


def parse_content_disposition_filename(header_val: str | None) -> str | None:
    """Extract filename from Content-Disposition header if available."""
    if not header_val:
        return None
    msg = Message()
    msg["content-disposition"] = header_val
    filename = msg.get_filename()
    if filename:
        return Path(filename.replace("\\", "/")).name
    return None


def looks_like_latex_or_bib(data: bytes) -> bool:
    """Heuristic check whether raw uncompressed bytes represent a TeX/Bib text file."""
    if not data:
        return False
    if data.startswith((
        b"%PDF-",
        b"PK\x03\x04",
        b"\x89PNG",
        b"\xff\xd8\xff",
        b"GIF8",
        b"%!PS-Adobe",
    )):
        return False
    head = data[:4096].lstrip().lower()
    if head.startswith((b"<!doctype html", b"<html", b"<?xml")):
        return False
    if b"\x00" in data[:8192]:
        return False
    return True


def extract_tex_and_bib_from_bytes(
    raw_bytes: bytes,
    dest_dir: Path,
    content_disposition: str | None = None,
    content_type: str | None = None,
) -> dict:
    """Inspect downloaded payload and extract ONLY .tex and .bib files into dest_dir."""
    if dest_dir.exists():
        shutil.rmtree(dest_dir)
    dest_dir.mkdir(parents=True, exist_ok=True)

    raw_size = len(raw_bytes)
    hint_filename = parse_content_disposition_filename(content_disposition)
    ctype = (content_type or "").lower()

    if raw_bytes.startswith(b"%PDF-") or "application/pdf" in ctype:
        return {
            "status": "no_tex_source",
            "source_type": "pdf_only",
            "raw_archive_bytes": raw_size,
            "uncompressed_total_bytes": raw_size,
            "kept_tex_bib_bytes": 0,
            "tex_file_count": 0,
            "bib_file_count": 0,
            "removed_file_count": 1,
            "kept_files": [],
        }

    # 1. Try tar archive (.tar.gz, .tgz, .tar.bz2, .tar)
    try:
        with tarfile.open(fileobj=io.BytesIO(raw_bytes), mode="r:*") as tar:
            members = [m for m in tar.getmembers() if m.isfile()]
            uncompressed_total = sum(max(0, m.size) for m in members)
            kept_bytes = 0
            tex_count = 0
            bib_count = 0
            removed_count = 0
            kept_files: list[str] = []

            for member in members:
                rel_path = sanitize_relative_path(member.name)
                if rel_path is None:
                    removed_count += 1
                    continue
                ext = rel_path.suffix.lower()
                if ext not in ALLOWED_EXTENSIONS:
                    removed_count += 1
                    continue
                extracted_f = tar.extractfile(member)
                if extracted_f is None:
                    removed_count += 1
                    continue
                content = extracted_f.read()
                out_file = dest_dir / rel_path
                out_file.parent.mkdir(parents=True, exist_ok=True)
                out_file.write_bytes(content)

                kept_bytes += len(content)
                if ext == ".tex":
                    tex_count += 1
                elif ext == ".bib":
                    bib_count += 1
                kept_files.append(rel_path.as_posix())

            status = "success" if (tex_count + bib_count) > 0 else "no_tex_source"
            return {
                "status": status,
                "source_type": "tar" if status == "success" else "tar_without_tex",
                "raw_archive_bytes": raw_size,
                "uncompressed_total_bytes": uncompressed_total,
                "kept_tex_bib_bytes": kept_bytes,
                "tex_file_count": tex_count,
                "bib_file_count": bib_count,
                "removed_file_count": removed_count,
                "kept_files": sorted(kept_files),
            }
    except (tarfile.TarError, EOFError, OSError):
        pass

    # 2. Try ZIP archive or DOCX
    if raw_bytes.startswith(b"PK\x03\x04"):
        if hint_filename and hint_filename.lower().endswith(".docx"):
            return {
                "status": "no_tex_source",
                "source_type": "docx_only",
                "raw_archive_bytes": raw_size,
                "uncompressed_total_bytes": raw_size,
                "kept_tex_bib_bytes": 0,
                "tex_file_count": 0,
                "bib_file_count": 0,
                "removed_file_count": 1,
                "kept_files": [],
            }
        try:
            with zipfile.ZipFile(io.BytesIO(raw_bytes)) as zf:
                infos = [info for info in zf.infolist() if not info.is_dir()]
                names = {info.filename for info in infos}
                if "[Content_Types].xml" in names and any(n.startswith("word/") for n in names):
                    return {
                        "status": "no_tex_source",
                        "source_type": "docx_only",
                        "raw_archive_bytes": raw_size,
                        "uncompressed_total_bytes": sum(i.file_size for i in infos),
                        "kept_tex_bib_bytes": 0,
                        "tex_file_count": 0,
                        "bib_file_count": 0,
                        "removed_file_count": len(infos),
                        "kept_files": [],
                    }
                uncompressed_total = sum(max(0, i.file_size) for i in infos)
                kept_bytes = 0
                tex_count = 0
                bib_count = 0
                removed_count = 0
                kept_files = []
                for info in infos:
                    rel_path = sanitize_relative_path(info.filename)
                    if rel_path is None or rel_path.suffix.lower() not in ALLOWED_EXTENSIONS:
                        removed_count += 1
                        continue
                    content = zf.read(info.filename)
                    out_file = dest_dir / rel_path
                    out_file.parent.mkdir(parents=True, exist_ok=True)
                    out_file.write_bytes(content)
                    kept_bytes += len(content)
                    if rel_path.suffix.lower() == ".tex":
                        tex_count += 1
                    else:
                        bib_count += 1
                    kept_files.append(rel_path.as_posix())
                status = "success" if (tex_count + bib_count) > 0 else "no_tex_source"
                return {
                    "status": status,
                    "source_type": "zip" if status == "success" else "zip_without_tex",
                    "raw_archive_bytes": raw_size,
                    "uncompressed_total_bytes": uncompressed_total,
                    "kept_tex_bib_bytes": kept_bytes,
                    "tex_file_count": tex_count,
                    "bib_file_count": bib_count,
                    "removed_file_count": removed_count,
                    "kept_files": sorted(kept_files),
                }
        except zipfile.BadZipFile:
            pass

    # 3. Single gzip-compressed file or plain file
    payload = raw_bytes
    was_gzipped = False
    if raw_bytes.startswith(b"\x1f\x8b"):
        try:
            payload = gzip.decompress(raw_bytes)
            was_gzipped = True
        except (OSError, EOFError):
            payload = raw_bytes

    uncompressed_total = len(payload)

    if payload.startswith(b"%PDF-"):
        return {
            "status": "no_tex_source",
            "source_type": "pdf_only",
            "raw_archive_bytes": raw_size,
            "uncompressed_total_bytes": uncompressed_total,
            "kept_tex_bib_bytes": 0,
            "tex_file_count": 0,
            "bib_file_count": 0,
            "removed_file_count": 1,
            "kept_files": [],
        }

    if looks_like_latex_or_bib(payload):
        out_name = "main.tex"
        if hint_filename:
            stem = hint_filename[:-3] if hint_filename.lower().endswith(".gz") else hint_filename
            clean_stem = sanitize_relative_path(stem)
            if clean_stem is not None:
                if clean_stem.suffix.lower() in ALLOWED_EXTENSIONS:
                    out_name = clean_stem.name
                elif not clean_stem.suffix:
                    out_name = f"{clean_stem.name}.tex"
        out_file = dest_dir / out_name
        out_file.write_bytes(payload)
        is_bib = out_name.lower().endswith(".bib")
        return {
            "status": "success",
            "source_type": "single_gz_tex" if was_gzipped else "single_plain_tex",
            "raw_archive_bytes": raw_size,
            "uncompressed_total_bytes": uncompressed_total,
            "kept_tex_bib_bytes": len(payload),
            "tex_file_count": 0 if is_bib else 1,
            "bib_file_count": 1 if is_bib else 0,
            "removed_file_count": 0,
            "kept_files": [out_name],
        }

    return {
        "status": "no_tex_source",
        "source_type": "non_tex_or_html",
        "raw_archive_bytes": raw_size,
        "uncompressed_total_bytes": uncompressed_total,
        "kept_tex_bib_bytes": 0,
        "tex_file_count": 0,
        "bib_file_count": 0,
        "removed_file_count": 1,
        "kept_files": [],
    }


class S3BulkSourceLoader:
    """Amazon S3 Bulk Repository (`s3://arxiv/src/`) loader and O(1) `.tar` byte-offset indexer."""

    def __init__(self, s3_dir: Path = DEFAULT_S3_DIR):
        self.s3_dir = s3_dir
        self.manifest_path = s3_dir / "arXiv_src_manifest.xml"
        self.index_cache_path = s3_dir / "s3_tar_index.json"
        self._index: dict[str, tuple[Path, str, int, int]] = {}

    @staticmethod
    def _detect_s3_cli() -> str | None:
        if shutil.which("aws"):
            return "aws"
        if shutil.which("s3cmd"):
            return "s3cmd"
        return None

    def _download_s3_uri(self, s3_uri: str, dest_path: Path, cli: str) -> bool:
        dest_path.parent.mkdir(parents=True, exist_ok=True)
        if cli == "aws":
            cmd = [
                "aws", "s3", "cp",
                s3_uri,
                str(dest_path),
                "--request-payer", "requester",
            ]
        else:
            cmd = [
                "s3cmd", "get",
                "--requester-pays",
                "--force",
                s3_uri,
                str(dest_path),
            ]
        print(f"[S3 Bulk] Running: {' '.join(cmd)}", flush=True)
        try:
            proc = subprocess.run(cmd, check=False)
            return proc.returncode == 0 and dest_path.exists()
        except Exception as exc:
            print(f"[S3 Bulk] Warning: CLI command failed ({exc})", file=sys.stderr, flush=True)
            return False

    def find_matching_tar_chunks_from_manifest(
        self, target_papers: list[dict]
    ) -> list[dict]:
        if not self.manifest_path.exists() or not target_papers:
            return []

        yymm_bounds: dict[str, tuple[str, str]] = {}
        for p in target_papers:
            aid = p["arxiv_id"]
            ym = aid.split(".")[0]
            if ym not in yymm_bounds:
                yymm_bounds[ym] = (aid, aid)
            else:
                cur_min, cur_max = yymm_bounds[ym]
                if normalize_arxiv_id_tuple(aid) < normalize_arxiv_id_tuple(cur_min):
                    cur_min = aid
                if normalize_arxiv_id_tuple(aid) > normalize_arxiv_id_tuple(cur_max):
                    cur_max = aid
                yymm_bounds[ym] = (cur_min, cur_max)

        matched_chunks: list[dict] = []
        try:
            for _, elem in ET.iterparse(str(self.manifest_path), events=("end",)):
                if elem.tag != "file":
                    continue
                filename = (elem.findtext("filename") or "").strip()
                first_item = (elem.findtext("first_item") or "").strip()
                last_item = (elem.findtext("last_item") or "").strip()
                yymm = (elem.findtext("yymm") or "").strip()
                size_str = (elem.findtext("size") or "0").strip()

                if filename and first_item and last_item:
                    for ym, (t_start, t_end) in yymm_bounds.items():
                        if (not yymm or yymm == ym) and ranges_overlap(
                            first_item, last_item, t_start, t_end
                        ):
                            matched_chunks.append({
                                "filename": filename,
                                "basename": Path(filename).name,
                                "first_item": first_item,
                                "last_item": last_item,
                                "yymm": yymm or ym,
                                "size": int(size_str) if size_str.isdigit() else 0,
                            })
                            break
                elem.clear()
        except Exception as exc:
            print(
                f"[S3 Bulk] Warning: Failed to parse {self.manifest_path}: {exc}",
                file=sys.stderr,
                flush=True,
            )
        return matched_chunks

    def sync_from_s3(self, target_papers: list[dict]) -> int:
        cli = self._detect_s3_cli()
        if cli is None:
            print(
                "[S3 Bulk] Neither `aws` nor `s3cmd` found on PATH. Falling back to HTTP.",
                file=sys.stderr,
                flush=True,
            )
            return 0

        self.s3_dir.mkdir(parents=True, exist_ok=True)
        if not self.manifest_path.exists():
            manifest_uri = f"{S3_BUCKET_SRC_URI}/arXiv_src_manifest.xml"
            if not self._download_s3_uri(manifest_uri, self.manifest_path, cli):
                return 0

        chunks = self.find_matching_tar_chunks_from_manifest(target_papers)
        if not chunks:
            return 0

        downloaded = 0
        for chunk in chunks:
            local_tar = self.s3_dir / chunk["basename"]
            if local_tar.exists() and (chunk["size"] <= 0 or local_tar.stat().st_size == chunk["size"]):
                downloaded += 1
                continue
            rel_key = chunk["filename"].lstrip("/")
            s3_uri = f"s3://arxiv/{rel_key}" if rel_key.startswith("src/") else f"{S3_BUCKET_SRC_URI}/{rel_key}"
            if self._download_s3_uri(s3_uri, local_tar, cli):
                downloaded += 1
        return downloaded

    def build_index(self, target_ids: set[str] | None = None) -> int:
        self._index.clear()
        if not self.s3_dir.exists():
            return 0

        tar_files = sorted(self.s3_dir.glob("*.tar"))
        cached_tar_meta: dict[str, dict] = {}
        if self.index_cache_path.exists():
            try:
                raw_cache = json.loads(self.index_cache_path.read_text(encoding="utf-8"))
                if isinstance(raw_cache, dict):
                    cached_tar_meta = raw_cache
            except Exception:
                cached_tar_meta = {}

        cache_updated = False
        for tar_path in tar_files:
            try:
                st = tar_path.stat()
            except OSError:
                continue
            cache_key = tar_path.name
            cached_entry = cached_tar_meta.get(cache_key)
            if (
                cached_entry
                and cached_entry.get("size") == st.st_size
                and cached_entry.get("mtime_ns") == st.st_mtime_ns
                and isinstance(cached_entry.get("members"), list)
            ):
                members_list = cached_entry["members"]
            else:
                print(f"[S3 Bulk] Indexing bundle {tar_path.name} ({st.st_size / (1024*1024):.1f} MB)...", flush=True)
                members_list = []
                try:
                    with tarfile.open(tar_path, mode="r:") as tf:
                        for m in tf:
                            if not m.isfile():
                                continue
                            match = S3_MEMBER_PATTERN.search(m.name.replace("\\", "/"))
                            if not match:
                                continue
                            base_id, v_num, _ = match.groups()
                            members_list.append([
                                base_id,
                                f"{base_id}v{v_num}" if v_num else "",
                                m.name,
                                int(m.offset_data),
                                int(m.size),
                            ])
                    cached_tar_meta[cache_key] = {
                        "size": st.st_size,
                        "mtime_ns": st.st_mtime_ns,
                        "members": members_list,
                    }
                    cache_updated = True
                except Exception as exc:
                    print(
                        f"[S3 Bulk] Warning: Could not index {tar_path.name}: {exc}",
                        file=sys.stderr,
                        flush=True,
                    )
                    continue

            for base_id, ver_id, m_name, offset_data, m_size in members_list:
                if target_ids is not None and base_id not in target_ids:
                    continue
                record = (tar_path, m_name, int(offset_data), int(m_size))
                self._index[base_id] = record
                if ver_id:
                    self._index[ver_id] = record

        if cache_updated:
            try:
                write_json_atomic(self.index_cache_path, cached_tar_meta, compact=True)
            except Exception:
                pass

        for fpath in self.s3_dir.rglob("*"):
            if not fpath.is_file() or fpath.suffix.lower() in (".tar", ".xml", ".json", ".tmp"):
                continue
            match = S3_MEMBER_PATTERN.search(fpath.name)
            if not match:
                continue
            base_id, v_num, _ = match.groups()
            if target_ids is not None and base_id not in target_ids:
                continue
            try:
                fsize = fpath.stat().st_size
            except OSError:
                continue
            record = (fpath, fpath.name, -1, int(fsize))
            if v_num:
                self._index[f"{base_id}v{v_num}"] = record
            else:
                self._index[base_id] = record

        return len(self._index)

    def has_version(self, arxiv_id: str, version_id: str, is_latest_version: bool) -> bool:
        if version_id in self._index:
            return True
        if is_latest_version and arxiv_id in self._index:
            return True
        return False

    def fetch_version_bytes(
        self, arxiv_id: str, version_id: str, is_latest_version: bool
    ) -> tuple[bytes, str, str] | None:
        entry = self._index.get(version_id)
        if entry is None and is_latest_version:
            entry = self._index.get(arxiv_id)
        if entry is None:
            return None

        src_path, member_name, offset_data, size = entry
        if offset_data < 0:
            raw_bytes = src_path.read_bytes()
        else:
            with src_path.open("rb") as f:
                f.seek(offset_data)
                raw_bytes = f.read(size)

        ext = Path(member_name).suffix.lower()
        content_type = "application/pdf" if ext == ".pdf" else "application/x-eprint"
        content_disp = f'attachment; filename="{Path(member_name).name}"'
        return raw_bytes, content_disp, content_type


class ArxivSourceClient:
    """Concurrent multi-threaded HTTP downloader with multi-endpoint rotation and auto VPN-switch recovery."""

    def __init__(
        self,
        delay: float = 0.6,
        timeout: float = 45.0,
        max_retries: int = 12,
        pool_maxsize: int = 32,
        user_agent: str | None = None,
    ):
        if delay < 0.0:
            raise ValueError("delay must be >= 0.0")
        self.base_delay = delay
        self.delay = delay
        self.timeout = timeout
        self.max_retries = max_retries
        self.pool_maxsize = max(4, pool_maxsize)
        self.user_agent = user_agent or os.environ.get("ARXIV_USER_AGENT")
        self._last_request_ts: float | None = None
        self._rate_lock = threading.Lock()
        self._cooldown_until: float = 0.0
        self._cooldown_lock = threading.Lock()
        self._consecutive_success: int = 0
        self._rr_counter = 0
        self._ssl_context = self._build_ssl_context()
        self._thread_local = threading.local()
        self._session_epoch = 0

    @staticmethod
    def _build_ssl_context() -> ssl.SSLContext:
        try:
            import certifi  # type: ignore

            return ssl.create_default_context(cafile=certifi.where())
        except Exception:
            return ssl.create_default_context()

    def _get_thread_session(self):
        """Return a thread-local Keep-Alive `requests.Session` recreated whenever IP/VPN resets."""
        try:
            import requests  # type: ignore
            from requests.adapters import HTTPAdapter  # type: ignore

            sess = getattr(self._thread_local, "session", None)
            sess_epoch = getattr(self._thread_local, "epoch", -1)
            if sess is not None and sess_epoch == self._session_epoch:
                return sess

            if sess is not None:
                try:
                    sess.close()
                except Exception:
                    pass

            session = requests.Session()
            adapter = HTTPAdapter(pool_connections=4, pool_maxsize=self.pool_maxsize)
            session.mount("https://", adapter)
            session.mount("http://", adapter)
            ua = self.user_agent or random.choice(USER_AGENTS)
            session.headers.update({
                "User-Agent": ua,
                "Accept": "application/x-eprint-tar, application/x-eprint, application/gzip, */*",
                "Connection": "keep-alive",
            })
            self._thread_local.session = session
            self._thread_local.epoch = self._session_epoch
            return session
        except Exception:
            return None

    def reset_connections(self) -> None:
        """Increment session epoch so all worker threads drop old sockets (e.g. after VPN IP switch)."""
        with self._cooldown_lock:
            self._session_epoch += 1

    def _acquire_rate_slot(self) -> int:
        """Wait for any global rate-limit cooldown and pace requests across worker threads."""
        while True:
            now = time.monotonic()
            wait_cooldown = self._cooldown_until - now
            if wait_cooldown > 0:
                time.sleep(min(wait_cooldown, 2.0))
                continue
            break

        with self._rate_lock:
            idx = self._rr_counter
            self._rr_counter += 1
            if self.delay > 0.0 and self._last_request_ts is not None:
                elapsed = time.monotonic() - self._last_request_ts
                remaining = self.delay - elapsed
                if remaining > 0:
                    time.sleep(remaining)
            self._last_request_ts = time.monotonic()
            return idx

    def _record_success(self) -> None:
        """Gradually relax adaptive delay back toward `base_delay` after sustained successes."""
        with self._rate_lock:
            self._consecutive_success += 1
            if self._consecutive_success >= 10 and self.delay > self.base_delay:
                self.delay = max(self.base_delay, round(self.delay * 0.85, 3))
                self._consecutive_success = 0

    def _trigger_rate_limit_cooldown(self, version_id: str, status_code: int, retry_after: int | None, attempt: int) -> None:
        """Coordinate a brief cooldown when export.arxiv.org signals rate limiting."""
        with self._cooldown_lock:
            now = time.monotonic()
            if self._cooldown_until > now + 1.0:
                return
            wait_sec = min(15, retry_after) if retry_after is not None else min(10, 3 * attempt)
            self._cooldown_until = now + wait_sec
            with self._rate_lock:
                self._consecutive_success = 0
                self.delay = min(1.5, max(0.8, round(self.delay * 1.25, 2)))
                new_delay = self.delay
            print(
                f"\n[AUTO-COOLDOWN HTTP {status_code} on {version_id}] "
                f"Tạm nghỉ {wait_sec}s (nhịp tải {new_delay}s)...",
                file=sys.stderr,
                flush=True,
            )

    def fetch_version_source(self, version_id: str) -> tuple[bytes, str | None, str | None]:
        """Download raw source bytes for a single versioned ID like '2511.11709v1'."""
        if not VERSIONED_PATTERN.fullmatch(version_id):
            raise ValueError(f"Expected versioned arXiv ID (e.g. 2511.11709v1), got: {version_id}")

        req_timeout = (10.0, self.timeout)
        for attempt in range(1, self.max_retries + 1):
            slot_idx = self._acquire_rate_slot()
            endpoint = SRC_ENDPOINTS[(slot_idx + attempt - 1) % len(SRC_ENDPOINTS)]
            url = f"{endpoint}/{version_id}"

            session = self._get_thread_session()
            if session is not None:
                try:
                    try:
                        resp = session.get(url, timeout=req_timeout)
                    except Exception as ssl_exc:
                        if "CERTIFICATE_VERIFY_FAILED" in str(ssl_exc) or "SSLError" in type(ssl_exc).__name__:
                            import urllib3  # type: ignore

                            urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)
                            session.verify = False
                            resp = session.get(url, timeout=req_timeout, verify=False)
                        else:
                            raise

                    if resp.status_code == 200:
                        self._record_success()
                        return (
                            resp.content,
                            resp.headers.get("Content-Disposition"),
                            resp.headers.get("Content-Type"),
                        )
                    retry_after = parse_retry_after(resp.headers.get("Retry-After"))
                    if resp.status_code == 429:
                        if attempt < self.max_retries:
                            self._trigger_rate_limit_cooldown(version_id, 429, retry_after, attempt)
                            continue
                        raise RateLimitError(
                            f"HTTP 429 Too Many Requests for {version_id}",
                            retry_after=retry_after,
                        )
                    if resp.status_code == 403:
                        body_preview = resp.text[:512].lower()
                        if "unavailable" in body_preview or "withdrawn" in body_preview or "no source" in body_preview:
                            return b"<html>source unavailable</html>", None, "text/html"
                        if attempt < self.max_retries:
                            self._trigger_rate_limit_cooldown(version_id, 403, retry_after, attempt)
                            continue
                        raise RateLimitError(
                            f"HTTP 403 Forbidden on {version_id} (possible rate limit block)",
                            retry_after=retry_after,
                        )
                    if resp.status_code == 404:
                        return b"<html>not found</html>", None, "text/html"
                    if resp.status_code in (500, 502, 503, 504) and attempt < self.max_retries:
                        time.sleep(max(1.0, (retry_after or 3) * attempt))
                        continue
                    raise RuntimeError(f"HTTP {resp.status_code} while downloading {version_id}")
                except RateLimitError:
                    raise
                except Exception as exc:
                    if attempt < self.max_retries:
                        self._thread_local.session = None
                        time.sleep(min(5.0, 1.0 * attempt))
                        continue
                    raise RuntimeError(f"Network error while downloading {version_id}: {exc}") from exc

            # Fallback to stdlib urllib.request
            req = urllib.request.Request(
                url,
                headers={
                    "User-Agent": self.user_agent or random.choice(USER_AGENTS),
                    "Accept": "application/x-eprint-tar, application/x-eprint, application/gzip, */*",
                },
            )
            try:
                with urllib.request.urlopen(
                    req, timeout=self.timeout, context=self._ssl_context
                ) as resp:
                    raw_bytes = resp.read()
                    content_disp = resp.headers.get("Content-Disposition")
                    content_type = resp.headers.get("Content-Type")
                    return raw_bytes, content_disp, content_type
            except urllib.error.HTTPError as exc:
                retry_after = parse_retry_after(exc.headers.get("Retry-After") if exc.headers else None)
                if exc.code in (429, 403):
                    if exc.code == 403:
                        body_preview = ""
                        try:
                            body_preview = exc.read(512).decode("utf-8", errors="ignore").lower()
                        except Exception:
                            pass
                        if "unavailable" in body_preview or "withdrawn" in body_preview or "no source" in body_preview:
                            return b"<html>source unavailable</html>", None, "text/html"
                    if attempt < self.max_retries:
                        self._trigger_rate_limit_cooldown(version_id, exc.code, retry_after, attempt)
                        continue
                    raise RateLimitError(
                        f"HTTP {exc.code} Rate Limit on {version_id}",
                        retry_after=retry_after,
                    ) from exc
                if exc.code == 404:
                    return b"<html>not found</html>", None, "text/html"
                if exc.code in (500, 502, 503, 504) and attempt < self.max_retries:
                    time.sleep(max(1.0, (retry_after or 3) * attempt))
                    continue
                raise RuntimeError(f"HTTP {exc.code} while downloading {version_id}: {exc.reason}") from exc
            except Exception as exc:
                if attempt < self.max_retries:
                    time.sleep(min(10.0, 1.5 * attempt))
                    continue
                raise RuntimeError(f"Network error while downloading {version_id}: {exc}") from exc

        raise RuntimeError(f"Failed to download {version_id} after {self.max_retries} attempts")


def compute_directory_size_bytes(root: Path) -> int:
    """Compute total disk size in bytes of all files under root using fast os.scandir."""
    if not root.exists():
        return 0
    total = 0
    stack = [root]
    while stack:
        current = stack.pop()
        try:
            with os.scandir(current) as it:
                for entry in it:
                    try:
                        if entry.is_dir(follow_symlinks=False):
                            stack.append(Path(entry.path))
                        elif entry.is_file(follow_symlinks=False):
                            total += entry.stat(follow_symlinks=False).st_size
                    except OSError:
                        pass
        except OSError:
            pass
    return total


def recover_status_from_disk_folder(paper_dir: Path, versions: list[str]) -> dict | None:
    """Recover paper download status from existing `papers/<YYMM-NNNNN>/metadata.json` and `v1..vN` dirs on disk."""
    meta_path = paper_dir / "metadata.json"
    if not meta_path.exists():
        return None
    try:
        meta = json.loads(meta_path.read_text(encoding="utf-8"))
    except Exception:
        return None

    arxiv_id = meta.get("arxiv_id")
    v_sources = meta.get("version_sources") or meta.get("version_downloads") or {}
    if not arxiv_id or not isinstance(v_sources, dict):
        return None

    recovered_versions: dict[str, dict] = {}
    total_kept = 0
    has_tex = False

    for vid in versions:
        v_key = f"v{vid.split('v')[-1]}"
        v_dir = paper_dir / v_key
        v_info = v_sources.get(v_key)
        if not v_info or not v_dir.exists():
            return None
        st = v_info.get("status")
        if st not in ("success", "no_tex_source"):
            return None

        kept_files = v_info.get("kept_files", [])
        tex_cnt = sum(1 for f in kept_files if f.lower().endswith(".tex"))
        bib_cnt = sum(1 for f in kept_files if f.lower().endswith(".bib"))
        kept_bytes = v_info.get("kept_tex_bib_bytes")
        if kept_bytes is None:
            kept_bytes = compute_directory_size_bytes(v_dir)

        raw_bytes = int(v_info.get("raw_archive_bytes") or kept_bytes)
        uncomp_bytes = int(v_info.get("uncompressed_total_bytes") or kept_bytes)
        total_kept += int(kept_bytes)
        if st == "success":
            has_tex = True

        recovered_versions[v_key] = {
            "version_id": vid,
            "status": st,
            "source_type": v_info.get("source_type", "tar" if st == "success" else "pdf_only"),
            "source_origin": v_info.get("source_origin", "http_export_arxiv"),
            "raw_archive_bytes": raw_bytes,
            "uncompressed_total_bytes": uncomp_bytes,
            "kept_tex_bib_bytes": int(kept_bytes),
            "tex_file_count": int(v_info.get("tex_file_count", tex_cnt)),
            "bib_file_count": int(v_info.get("bib_file_count", bib_cnt)),
            "removed_file_count": int(v_info.get("removed_file_count", 0)),
            "kept_files": kept_files,
            "download_time_sec": float(v_info.get("download_time_sec", 3.1)),
        }

    return {
        "arxiv_id": arxiv_id,
        "paper_status": "success" if has_tex else "no_tex_source",
        "total_raw_archive_bytes": sum(v["raw_archive_bytes"] for v in recovered_versions.values()),
        "total_uncompressed_bytes": sum(v["uncompressed_total_bytes"] for v in recovered_versions.values()),
        "total_kept_tex_bib_bytes": total_kept,
        "download_time_sec": round(sum(v["download_time_sec"] for v in recovered_versions.values()), 4),
        "version_downloads": recovered_versions,
    }


def build_clean_paper_metadata(paper: dict, version_downloads: dict[str, dict]) -> dict:
    """Build clean publication metadata stored inside `sources/<YYMM-NNNNN>/metadata.json`."""
    arxiv_id = paper["arxiv_id"]
    versions = paper.get("versions") or [
        f"{arxiv_id}v{i}" for i in range(1, int(paper.get("latest_version", 1)) + 1)
    ]
    version_files = {}
    for vid in versions:
        v_key = f"v{vid.split('v')[-1]}"
        v_info = version_downloads.get(v_key, {})
        version_files[v_key] = {
            "version_id": vid,
            "status": v_info.get("status", "pending"),
            "source_type": v_info.get("source_type"),
            "source_origin": v_info.get("source_origin", "http_export_arxiv"),
            "kept_files": v_info.get("kept_files", []),
        }
    return {
        "arxiv_id": arxiv_id,
        "title": paper.get("title"),
        "authors": paper.get("authors", []),
        "submission_date": paper.get("submission_date"),
        "revised_dates": paper.get("revised_dates", []),
        "venue": paper.get("venue"),
        "latest_version": paper.get("latest_version", len(versions)),
        "versions": versions,
        "version_sources": version_files,
    }


def build_papers_table_row(paper: dict) -> dict:
    """Build a row conforming strictly to the Milestone 1 `papers` dataset table schema."""
    return {
        "arxiv_id": paper["arxiv_id"],
        "title": paper.get("title") or "",
        "authors": list(paper.get("authors") or []),
        "submission_date": paper.get("submission_date") or "",
        "revised_dates": list(paper.get("revised_dates") or []),
        "venue": paper.get("venue"),
    }


def resolve_papers_via_arxiv_api(
    arxiv_ids: list[str],
    delay: float = 3.0,
) -> list[dict]:
    """Query the arXiv API (via `arxiv.py` Client or HTTP Atom endpoint) to resolve metadata and versions."""
    resolved: list[dict] = []
    if not arxiv_ids:
        return resolved

    base_ids = []
    for raw_id in arxiv_ids:
        m = VERSIONED_SEARCH.search(raw_id.strip())
        base_ids.append(m.group(1) if m else raw_id.strip())

    try:
        import arxiv  # type: ignore

        client = arxiv.Client(delay_seconds=max(3.0, delay), num_retries=3)
        search = arxiv.Search(id_list=base_ids)
        for paper in client.results(search):
            entry_id = getattr(paper, "entry_id", "") or getattr(paper, "id", "")
            m = VERSIONED_SEARCH.search(entry_id)
            if not m:
                continue
            pid, latest_v = m.group(1), int(m.group(2))
            published_dt = getattr(paper, "published", None)
            updated_dt = getattr(paper, "updated", None)
            pub_iso = published_dt.strftime("%Y-%m-%dT%H:%M:%SZ") if published_dt else ""
            upd_iso = updated_dt.strftime("%Y-%m-%dT%H:%M:%SZ") if updated_dt else pub_iso
            resolved.append({
                "arxiv_id": pid,
                "title": " ".join((paper.title or "").split()),
                "authors": [a.name for a in getattr(paper, "authors", []) if getattr(a, "name", None)],
                "submission_date": pub_iso,
                "latest_updated": upd_iso,
                "revised_dates": [] if latest_v == 1 else [upd_iso],
                "venue": getattr(paper, "journal_ref", None),
                "latest_version": latest_v,
                "versions": [f"{pid}v{i}" for i in range(1, latest_v + 1)],
                "abstract": " ".join((getattr(paper, "summary", "") or "").split()),
                "status": "found",
            })
        if resolved:
            return resolved
    except Exception:
        pass

    query = urllib.parse.urlencode({"id_list": ",".join(base_ids), "max_results": len(base_ids)})
    url = f"{API_ENDPOINT}?{query}"
    req = urllib.request.Request(
        url,
        headers={"User-Agent": "HCMUS-IDS-M1-SourceDownloader/3.0"},
    )
    ctx = ArxivSourceClient._build_ssl_context()
    try:
        with urllib.request.urlopen(req, timeout=45, context=ctx) as resp:
            xml_bytes = resp.read()
    except urllib.error.URLError:
        ctx = ssl._create_unverified_context()
        with urllib.request.urlopen(req, timeout=45, context=ctx) as resp:
            xml_bytes = resp.read()

    root = ET.fromstring(xml_bytes)
    for entry in root.findall(f"{ATOM_NS}entry"):
        entry_url = (entry.findtext(f"{ATOM_NS}id") or "").strip()
        m = VERSIONED_SEARCH.search(entry_url)
        if not m:
            continue
        pid, latest_v = m.group(1), int(m.group(2))
        title = " ".join((entry.findtext(f"{ATOM_NS}title") or "").split())
        authors = [
            " ".join((a.findtext(f"{ATOM_NS}name") or "").split())
            for a in entry.findall(f"{ATOM_NS}author")
        ]
        published = (entry.findtext(f"{ATOM_NS}published") or "").strip()
        updated = (entry.findtext(f"{ATOM_NS}updated") or "").strip()
        journal = (entry.findtext(f"{ARXIV_NS}journal_ref") or "").strip() or None
        resolved.append({
            "arxiv_id": pid,
            "title": title,
            "authors": [a for a in authors if a],
            "submission_date": published,
            "latest_updated": updated,
            "revised_dates": [] if latest_v == 1 else [updated],
            "venue": journal,
            "latest_version": latest_v,
            "versions": [f"{pid}v{i}" for i in range(1, latest_v + 1)],
            "abstract": " ".join((entry.findtext(f"{ATOM_NS}summary") or "").split()),
            "status": "found",
        })
    return resolved


def filter_papers(
    papers: list[dict],
    member_ranges: dict[str, list[tuple[str, str]]],
    member: str | None = None,
    start_id: str | None = None,
    end_id: str | None = None,
    only_ids: set[str] | None = None,
    limit: int | None = None,
) -> list[dict]:
    """Filter discovered papers list by member, start/end range, explicit IDs, and limit."""
    filtered = [p for p in papers if p.get("status", "found") == "found"]

    if member:
        if member not in member_ranges:
            valid = ", ".join(sorted(member_ranges))
            raise ValueError(f"Unknown member '{member}'. Valid members: {valid}")
        ranges = member_ranges[member]
        filtered = [
            p for p in filtered
            if any(id_in_range(p["arxiv_id"], s, e) for s, e in ranges)
        ]

    if start_id or end_id:
        s_bound = start_id or filtered[0]["arxiv_id"]
        e_bound = end_id or filtered[-1]["arxiv_id"]
        filtered = [
            p for p in filtered
            if id_in_range(p["arxiv_id"], s_bound, e_bound)
        ]

    if only_ids:
        filtered = [p for p in filtered if p["arxiv_id"] in only_ids]

    if limit is not None:
        filtered = filtered[:limit]

    return filtered


def _fetch_single_version_payload(
    arxiv_id: str,
    vid: str,
    v_key: str,
    is_latest: bool,
    client: ArxivSourceClient,
    s3_loader: S3BulkSourceLoader | None,
    use_s3: bool,
    use_http: bool,
    mem_monitor: MemoryMonitor,
) -> dict:
    """Dedicated Network Pool task: ONLY fetches raw bytes for 1 version without blocking on extraction/disk."""
    v_t0 = time.perf_counter()
    origin = "http_export_arxiv"
    try:
        if use_s3 and s3_loader is not None and s3_loader.has_version(arxiv_id, vid, is_latest):
            origin = "s3_bulk"
            s3_res = s3_loader.fetch_version_bytes(arxiv_id, vid, is_latest)
            if s3_res is None:
                raise RuntimeError(f"S3 Bulk entry missing for {vid}")
            raw_bytes, content_disp, content_type = s3_res
        elif use_http:
            origin = "http_export_arxiv"
            raw_bytes, content_disp, content_type = client.fetch_version_source(vid)
        else:
            raise RuntimeError(f"Version {vid} not found in S3 bulk directory and HTTP fallback is disabled")

        mem_monitor.sample_now()
        return {
            "v_key": v_key,
            "vid": vid,
            "origin": origin,
            "raw_bytes": raw_bytes,
            "content_disp": content_disp,
            "content_type": content_type,
            "net_elapsed": round(time.perf_counter() - v_t0, 4),
            "error": None,
            "rate_limited": False,
        }
    except RateLimitError as exc:
        return {
            "v_key": v_key,
            "vid": vid,
            "origin": origin,
            "raw_bytes": None,
            "net_elapsed": round(time.perf_counter() - v_t0, 4),
            "error": str(exc),
            "rate_limited": True,
        }
    except Exception as exc:
        return {
            "v_key": v_key,
            "vid": vid,
            "origin": origin,
            "raw_bytes": None,
            "net_elapsed": round(time.perf_counter() - v_t0, 4),
            "error": str(exc),
            "rate_limited": False,
        }


def _extract_only_paper_task(
    idx: int,
    total_papers: int,
    paper: dict,
    paper_dir: Path,
    versions: list[str],
    cached_versions: dict[str, dict],
    version_fetch_futures: list[Future],
    paper_t0: float,
    mem_monitor: MemoryMonitor,
) -> dict:
    """Dedicated Extraction Pool task: waits for version byte futures and extracts .tex/.bib to disk."""
    arxiv_id = paper["arxiv_id"]
    paper_dir.mkdir(parents=True, exist_ok=True)
    meta_path = paper_dir / "metadata.json"

    version_downloads: dict[str, dict] = dict(cached_versions)
    paper_had_error = False
    paper_has_tex = any(v.get("status") == "success" for v in version_downloads.values())
    peak_buffer = 0
    rate_limited = False

    for v_fut in version_fetch_futures:
        item = v_fut.result()
        v_key = item["v_key"]
        vid = item["vid"]
        origin = item["origin"]
        v_dir = paper_dir / v_key

        if item.get("error") is not None:
            paper_had_error = True
            if item.get("rate_limited"):
                rate_limited = True
            version_downloads[v_key] = {
                "version_id": vid,
                "status": "error",
                "source_origin": origin,
                "error": item["error"],
                "download_time_sec": item["net_elapsed"],
                "downloaded_at_utc": datetime.now(timezone.utc).isoformat(),
            }
            continue

        raw_bytes: bytes = item["raw_bytes"]
        if len(raw_bytes) > peak_buffer:
            peak_buffer = len(raw_bytes)

        ext_t0 = time.perf_counter()
        try:
            extract_info = extract_tex_and_bib_from_bytes(
                raw_bytes=raw_bytes,
                dest_dir=v_dir,
                content_disposition=item.get("content_disp"),
                content_type=item.get("content_type"),
            )
            del raw_bytes
            v_elapsed = round(item["net_elapsed"] + (time.perf_counter() - ext_t0), 4)

            extract_info["version_id"] = vid
            extract_info["source_origin"] = origin
            extract_info["download_time_sec"] = v_elapsed
            extract_info["downloaded_at_utc"] = datetime.now(timezone.utc).isoformat()
            version_downloads[v_key] = extract_info

            if extract_info["status"] == "success":
                paper_has_tex = True
        except Exception as exc:
            paper_had_error = True
            version_downloads[v_key] = {
                "version_id": vid,
                "status": "error",
                "source_origin": origin,
                "error": str(exc),
                "download_time_sec": round(item["net_elapsed"] + (time.perf_counter() - ext_t0), 4),
                "downloaded_at_utc": datetime.now(timezone.utc).isoformat(),
            }

    ordered_v_downloads: dict[str, dict] = {}
    for vid in versions:
        vk = f"v{vid.split('v')[-1]}"
        if vk in version_downloads:
            ordered_v_downloads[vk] = version_downloads[vk]

    paper_elapsed = round(time.perf_counter() - paper_t0, 4)
    current_rss = mem_monitor.sample_now()

    paper_raw_bytes = sum(
        int(v.get("raw_archive_bytes", 0)) for v in ordered_v_downloads.values()
    )
    paper_uncompressed_bytes = sum(
        int(v.get("uncompressed_total_bytes", 0)) for v in ordered_v_downloads.values()
    )
    paper_kept_bytes = sum(
        int(v.get("kept_tex_bib_bytes", 0)) for v in ordered_v_downloads.values()
    )
    p_status = (
        "error"
        if paper_had_error
        else ("success" if paper_has_tex else "no_tex_source")
    )

    write_json_atomic(meta_path, build_clean_paper_metadata(paper, ordered_v_downloads))

    status_record = {
        "arxiv_id": arxiv_id,
        "paper_status": p_status,
        "total_raw_archive_bytes": paper_raw_bytes,
        "total_uncompressed_bytes": paper_uncompressed_bytes,
        "total_kept_tex_bib_bytes": paper_kept_bytes,
        "download_time_sec": paper_elapsed,
        "ram_rss_bytes": current_rss,
        "version_downloads": ordered_v_downloads,
    }

    origins_used = sorted({
        v.get("source_origin", "http_export_arxiv")
        for v in ordered_v_downloads.values()
    })
    origin_tag = "+".join(origins_used)

    return {
        "idx": idx,
        "total_papers": total_papers,
        "arxiv_id": arxiv_id,
        "version_count": len(versions),
        "p_status": p_status,
        "paper_had_error": paper_had_error,
        "paper_has_tex": paper_has_tex,
        "rate_limited": rate_limited,
        "paper_elapsed": paper_elapsed,
        "peak_buffer": peak_buffer,
        "origin_tag": origin_tag,
        "status_record": status_record,
    }


def download_corpus(
    papers: list[dict],
    output_dir: Path,
    client: ArxivSourceClient,
    s3_loader: S3BulkSourceLoader | None = None,
    mode: str = "hybrid",
    workers: int = 8,
    checkpoint_interval: int = 50,
    refresh: bool = False,
    entry_discovery_time_sec: float | None = None,
) -> dict:
    """Download full-text `.tex` and `.bib` sources using Parallel Multi-Threaded HTTP + S3 Bulk."""
    papers_dir = output_dir / "papers"
    papers_dir.mkdir(parents=True, exist_ok=True)
    status_path = output_dir / "download_status.json"
    papers_table_path = output_dir / "papers.json"
    summary_path = output_dir / "download_summary.json"

    all_status: dict[str, dict] = {}
    if status_path.exists():
        try:
            loaded = json.loads(status_path.read_text(encoding="utf-8"))
            if isinstance(loaded, dict):
                all_status = loaded
        except Exception:
            all_status = {}

    prev_summary: dict = {}
    if not refresh and summary_path.exists():
        try:
            prev_summary = json.loads(summary_path.read_text(encoding="utf-8"))
        except Exception:
            prev_summary = {}

    tracemalloc.start()
    mem_monitor = MemoryMonitor(interval_sec=0.5)
    mem_monitor.start()

    start_wall = time.perf_counter()
    peak_temp_buffer_bytes = 0

    papers_succeeded = 0
    papers_no_tex = 0
    papers_failed = 0
    papers_cached = 0
    newly_downloaded_papers = 0

    versions_total = sum(len(p.get("versions", [])) for p in papers)
    versions_succeeded = 0
    versions_no_tex = 0
    versions_failed = 0
    versions_from_s3_bulk = 0
    versions_from_http = 0

    total_raw_archive_bytes = 0
    total_uncompressed_bytes = 0
    total_kept_tex_bib_bytes = 0
    total_tex_files = 0
    total_bib_files = 0
    total_removed_files = 0

    all_paper_times_sec: list[float] = []
    stopped_reason: str | None = None

    use_s3 = mode in ("hybrid", "s3") and s3_loader is not None
    use_http = mode in ("hybrid", "http")
    if use_s3 and s3_loader is not None:
        target_id_set = {p["arxiv_id"] for p in papers}
        indexed_count = s3_loader.build_index(target_ids=target_id_set)
        if indexed_count > 0:
            print(
                f"[Hybrid S3+HTTP] Indexed {indexed_count} matching entries in S3 bulk storage ({s3_loader.s3_dir}).",
                flush=True,
            )

    last_checkpoint_ts = time.monotonic()
    pending_since_checkpoint = 0
    recovered_from_disk_count = 0

    def _accumulate_completed_paper(res: dict) -> None:
        nonlocal papers_succeeded, papers_no_tex, papers_failed, newly_downloaded_papers
        nonlocal versions_succeeded, versions_no_tex, versions_failed
        nonlocal versions_from_s3_bulk, versions_from_http
        nonlocal total_raw_archive_bytes, total_uncompressed_bytes, total_kept_tex_bib_bytes
        nonlocal total_tex_files, total_bib_files, total_removed_files
        nonlocal peak_temp_buffer_bytes, pending_since_checkpoint, last_checkpoint_ts

        arxiv_id = res["arxiv_id"]
        status_rec = res["status_record"]
        all_status[arxiv_id] = status_rec

        if res["peak_buffer"] > peak_temp_buffer_bytes:
            peak_temp_buffer_bytes = res["peak_buffer"]

        newly_downloaded_papers += 1
        all_paper_times_sec.append(res["paper_elapsed"])

        for v_info in status_rec["version_downloads"].values():
            st = v_info.get("status")
            origin = v_info.get("source_origin", "http_export_arxiv")
            if origin == "s3_bulk":
                versions_from_s3_bulk += 1
            else:
                versions_from_http += 1

            if st == "success":
                versions_succeeded += 1
            elif st == "no_tex_source":
                versions_no_tex += 1
            else:
                versions_failed += 1

            raw_b = int(v_info.get("raw_archive_bytes", 0))
            if raw_b > peak_temp_buffer_bytes:
                peak_temp_buffer_bytes = raw_b
            total_raw_archive_bytes += raw_b
            total_uncompressed_bytes += int(v_info.get("uncompressed_total_bytes", 0))
            total_kept_tex_bib_bytes += int(v_info.get("kept_tex_bib_bytes", 0))
            total_tex_files += int(v_info.get("tex_file_count", 0))
            total_bib_files += int(v_info.get("bib_file_count", 0))
            total_removed_files += int(v_info.get("removed_file_count", 0))

        if res["paper_had_error"]:
            papers_failed += 1
        elif res["paper_has_tex"]:
            papers_succeeded += 1
        else:
            papers_no_tex += 1

        print(
            f"[{res['idx']}/{res['total_papers']}] {arxiv_id} ({res['version_count']}v, {res['origin_tag']}) -> "
            f"{res['p_status']} | "
            f"raw={status_rec['total_raw_archive_bytes'] / 1024:.1f}KB, "
            f"uncompressed={status_rec['total_uncompressed_bytes'] / 1024:.1f}KB, "
            f"kept_tex_bib={status_rec['total_kept_tex_bib_bytes'] / 1024:.1f}KB | "
            f"{res['paper_elapsed']:.2f}s",
            flush=True,
        )

        pending_since_checkpoint += 1
        now_ts = time.monotonic()
        if pending_since_checkpoint >= checkpoint_interval or (now_ts - last_checkpoint_ts) >= 30.0:
            write_json_atomic(status_path, all_status)
            pending_since_checkpoint = 0
            last_checkpoint_ts = now_ts

    active_futures: set[Future] = set()
    net_workers = max(16, workers * 2)
    ext_workers = max(4, workers)
    max_active_tasks = max(8, net_workers + ext_workers)

    try:
        with (
            ThreadPoolExecutor(max_workers=net_workers, thread_name_prefix="arxiv-net") as download_executor,
            ThreadPoolExecutor(max_workers=ext_workers, thread_name_prefix="arxiv-ext") as extract_executor,
        ):
            for idx, paper in enumerate(papers, start=1):
                # Harvest completed paper tasks when in-flight queue reaches capacity
                while len(active_futures) >= max_active_tasks:
                    done_set, active_futures = wait(active_futures, return_when=FIRST_COMPLETED)
                    for fut in done_set:
                        _accumulate_completed_paper(fut.result())

                arxiv_id = paper["arxiv_id"]
                versions: list[str] = paper.get("versions") or [
                    f"{arxiv_id}v{i}" for i in range(1, int(paper.get("latest_version", 1)) + 1)
                ]
                paper_dir = papers_dir / paper_folder_name(arxiv_id)
                meta_path = paper_dir / "metadata.json"

                prev_paper_status = all_status.get(arxiv_id, {})
                prev_v_downloads: dict[str, dict] = prev_paper_status.get("version_downloads", {})

                all_versions_ready = (
                    not refresh
                    and bool(prev_v_downloads)
                    and all(
                        prev_v_downloads.get(f"v{vid.split('v')[-1]}", {}).get("status")
                        in ("success", "no_tex_source")
                        and (paper_dir / f"v{vid.split('v')[-1]}").exists()
                        for vid in versions
                    )
                )

                # Auto-recover from disk `papers/<YYMM-NNNNN>/metadata.json` if missing in `download_status.json`
                if not all_versions_ready and not refresh and paper_dir.exists():
                    recovered = recover_status_from_disk_folder(paper_dir, versions)
                    if recovered is not None:
                        all_status[arxiv_id] = recovered
                        prev_paper_status = recovered
                        prev_v_downloads = recovered["version_downloads"]
                        all_versions_ready = True
                        recovered_from_disk_count += 1

                if all_versions_ready:
                    papers_cached += 1
                    has_tex = False
                    for vid in versions:
                        v_key = f"v{vid.split('v')[-1]}"
                        v_info = prev_v_downloads[v_key]
                        origin = v_info.get("source_origin", "http_export_arxiv")
                        if origin == "s3_bulk":
                            versions_from_s3_bulk += 1
                        else:
                            versions_from_http += 1
                        if v_info.get("status") == "success":
                            versions_succeeded += 1
                            has_tex = True
                        else:
                            versions_no_tex += 1
                        raw_b = int(v_info.get("raw_archive_bytes", 0))
                        if raw_b > peak_temp_buffer_bytes:
                            peak_temp_buffer_bytes = raw_b
                        total_raw_archive_bytes += raw_b
                        total_uncompressed_bytes += int(v_info.get("uncompressed_total_bytes", 0))
                        total_kept_tex_bib_bytes += int(v_info.get("kept_tex_bib_bytes", 0))
                        total_tex_files += int(v_info.get("tex_file_count", 0))
                        total_bib_files += int(v_info.get("bib_file_count", 0))
                        total_removed_files += int(v_info.get("removed_file_count", 0))

                    saved_time = float(prev_paper_status.get("download_time_sec", 0.0))
                    if saved_time <= 0.0:
                        saved_time = sum(
                            float(prev_v_downloads.get(f"v{vid.split('v')[-1]}", {}).get("download_time_sec", 0.0))
                            for vid in versions
                        )
                    if saved_time > 0.0:
                        all_paper_times_sec.append(round(saved_time, 4))

                    if not meta_path.exists():
                        write_json_atomic(meta_path, build_clean_paper_metadata(paper, prev_v_downloads))

                    if has_tex:
                        papers_succeeded += 1
                    else:
                        papers_no_tex += 1

                    if papers_cached <= 10 or papers_cached % 500 == 0:
                        print(
                            f"[{idx}/{len(papers)}] {arxiv_id} ({len(versions)}v): cached ({papers_cached} total cached)",
                            flush=True,
                        )
                    continue

                if recovered_from_disk_count > 0:
                    write_json_atomic(status_path, all_status)
                    print(
                        f"[Disk Cache Recovery] Đã khôi phục tự động {recovered_from_disk_count} bài có sẵn trong {papers_dir}!",
                        flush=True,
                    )
                    recovered_from_disk_count = 0

                paper_t0 = time.perf_counter()
                cached_versions: dict[str, dict] = {}
                version_fetch_futures: list[Future] = []

                for v_idx, vid in enumerate(versions):
                    v_num = vid.split("v")[-1]
                    v_key = f"v{v_num}"
                    v_dir = paper_dir / v_key
                    is_latest = v_idx == (len(versions) - 1)

                    prev_v = prev_v_downloads.get(v_key)
                    if (
                        not refresh
                        and prev_v
                        and prev_v.get("status") in ("success", "no_tex_source")
                        and v_dir.exists()
                    ):
                        cached_versions[v_key] = prev_v
                        continue

                    v_fut = download_executor.submit(
                        _fetch_single_version_payload,
                        arxiv_id,
                        vid,
                        v_key,
                        is_latest,
                        client,
                        s3_loader,
                        use_s3,
                        use_http,
                        mem_monitor,
                    )
                    version_fetch_futures.append(v_fut)

                fut = extract_executor.submit(
                    _extract_only_paper_task,
                    idx,
                    len(papers),
                    paper,
                    paper_dir,
                    versions,
                    cached_versions,
                    version_fetch_futures,
                    paper_t0,
                    mem_monitor,
                )
                active_futures.add(fut)

            # Drain all remaining active parallel tasks
            while active_futures:
                done_set, active_futures = wait(active_futures, return_when=FIRST_COMPLETED)
                for fut in done_set:
                    _accumulate_completed_paper(fut.result())

    except KeyboardInterrupt:
        stopped_reason = "user_interrupt"
        for fut in list(active_futures):
            if fut.done():
                try:
                    _accumulate_completed_paper(fut.result())
                except Exception:
                    pass
            else:
                fut.cancel()
        print(
            "\nInterrupted by user. Checkpoints saved.",
            file=sys.stderr,
            flush=True,
        )
    finally:
        mem_monitor.stop()
        _, tracemalloc_peak = tracemalloc.get_traced_memory()
        tracemalloc.stop()

    papers_table = [
        build_papers_table_row(p)
        for p in papers
        if all_status.get(p["arxiv_id"], {}).get("paper_status") in ("success", "no_tex_source")
    ]
    write_json_atomic(papers_table_path, papers_table)
    write_json_atomic(status_path, all_status)

    session_wall_sec = round(time.perf_counter() - start_wall, 4)
    sum_paper_times_sec = round(sum(all_paper_times_sec), 4)

    prev_rt = prev_summary.get("runtime_metrics_sec", {})
    prev_wall = float(prev_rt.get("total_wall_time_sec", 0.0))
    if newly_downloaded_papers == 0 and prev_wall > 0.0:
        cumulative_wall_sec = prev_wall
    elif papers_cached > 0 and newly_downloaded_papers > 0:
        cumulative_wall_sec = round(prev_wall + session_wall_sec, 4)
    else:
        cumulative_wall_sec = session_wall_sec

    processed_papers = papers_succeeded + papers_no_tex + papers_failed
    completed_papers = papers_succeeded + papers_no_tex
    processed_versions = versions_succeeded + versions_no_tex + versions_failed

    papers_disk_bytes = compute_directory_size_bytes(papers_dir)
    total_output_disk_bytes = compute_directory_size_bytes(output_dir)
    peak_disk_bytes = total_output_disk_bytes + peak_temp_buffer_bytes

    prev_mem = prev_summary.get("memory_and_disk_metrics", {})
    prev_peak_ram = int(prev_mem.get("peak_ram_rss_bytes", 0))
    prev_avg_ram = int(prev_mem.get("avg_ram_rss_bytes", 0))

    peak_ram_bytes = max(mem_monitor.peak_rss_bytes, prev_peak_ram if newly_downloaded_papers == 0 else 0)
    avg_ram_bytes = (
        prev_avg_ram
        if (newly_downloaded_papers == 0 and prev_avg_ram > 0)
        else mem_monitor.avg_rss_bytes
    )

    runtime_metrics: dict[str, float | int | None] = {
        "total_wall_time_sec": cumulative_wall_sec,
        "last_session_wall_time_sec": session_wall_sec,
        "sum_of_individual_paper_times_sec": sum_paper_times_sec,
        "avg_time_to_process_one_paper_sec": (
            round(sum_paper_times_sec / len(all_paper_times_sec), 4)
            if all_paper_times_sec
            else 0.0
        ),
        "median_time_to_process_one_paper_sec": (
            round(statistics.median(all_paper_times_sec), 4)
            if all_paper_times_sec
            else 0.0
        ),
        "max_time_to_process_one_paper_sec": (
            round(max(all_paper_times_sec), 4) if all_paper_times_sec else 0.0
        ),
        "min_time_to_process_one_paper_sec": (
            round(min(all_paper_times_sec), 4) if all_paper_times_sec else 0.0
        ),
        "entry_discovery_total_time_sec": entry_discovery_time_sec,
        "end_to_end_wall_time_with_discovery_sec": (
            round(cumulative_wall_sec + entry_discovery_time_sec, 4)
            if entry_discovery_time_sec is not None
            else cumulative_wall_sec
        ),
    }

    summary = {
        "download_mode": mode,
        "parallel_workers": workers,
        "request_delay_sec": client.delay,
        "total_target_papers": len(papers),
        "processed_papers": processed_papers,
        "cached_papers": papers_cached,
        "newly_downloaded_papers": newly_downloaded_papers,
        "papers_with_tex_source": papers_succeeded,
        "papers_without_tex_source_pdf_only": papers_no_tex,
        "papers_failed": papers_failed,
        "paper_crawl_success_rate": (
            round(completed_papers / len(papers), 4) if papers else 0.0
        ),
        "paper_tex_availability_rate": (
            round(papers_succeeded / completed_papers, 4) if completed_papers else 0.0
        ),
        "total_target_versions": versions_total,
        "processed_versions": processed_versions,
        "versions_with_tex_source": versions_succeeded,
        "versions_without_tex_source": versions_no_tex,
        "versions_failed": versions_failed,
        "hybrid_source_breakdown": {
            "versions_from_s3_bulk": versions_from_s3_bulk,
            "versions_from_http_export_arxiv": versions_from_http,
        },
        "size_metrics_bytes": {
            "total_raw_archive_bytes_before_filtering": total_raw_archive_bytes,
            "total_uncompressed_bytes_before_filtering": total_uncompressed_bytes,
            "total_kept_tex_bib_bytes_after_filtering": total_kept_tex_bib_bytes,
            "avg_paper_raw_archive_bytes_before_removing_figures": (
                round(total_raw_archive_bytes / completed_papers, 2)
                if completed_papers
                else 0.0
            ),
            "avg_paper_uncompressed_bytes_before_removing_figures": (
                round(total_uncompressed_bytes / completed_papers, 2)
                if completed_papers
                else 0.0
            ),
            "avg_paper_kept_tex_bib_bytes_after_removing_figures": (
                round(total_kept_tex_bib_bytes / completed_papers, 2)
                if completed_papers
                else 0.0
            ),
            "storage_reduction_ratio_vs_uncompressed": (
                round(1.0 - (total_kept_tex_bib_bytes / total_uncompressed_bytes), 4)
                if total_uncompressed_bytes > 0
                else 0.0
            ),
            "total_tex_files_kept": total_tex_files,
            "total_bib_files_kept": total_bib_files,
            "total_non_tex_bib_files_removed": total_removed_files,
        },
        "runtime_metrics_sec": runtime_metrics,
        "memory_and_disk_metrics": {
            "peak_ram_rss_bytes": peak_ram_bytes,
            "peak_ram_rss_mb": round(peak_ram_bytes / (1024 * 1024), 2),
            "avg_ram_rss_bytes": avg_ram_bytes,
            "avg_ram_rss_mb": round(avg_ram_bytes / (1024 * 1024), 2),
            "ram_periodic_sample_count": mem_monitor.sample_count,
            "tracemalloc_peak_python_heap_mb": round(
                tracemalloc_peak / (1024 * 1024), 2
            ),
            "max_disk_storage_required_bytes": peak_disk_bytes,
            "max_disk_storage_required_mb": round(peak_disk_bytes / (1024 * 1024), 2),
            "final_papers_storage_bytes": papers_disk_bytes,
            "final_papers_storage_mb": round(papers_disk_bytes / (1024 * 1024), 2),
            "final_output_storage_bytes": total_output_disk_bytes,
            "final_output_storage_mb": round(total_output_disk_bytes / (1024 * 1024), 2),
        },
        "output_paths": {
            "papers_dir": str(papers_dir),
            "papers_table_json": str(papers_table_path),
            "download_status_json": str(status_path),
            "download_summary_json": str(summary_path),
        },
        "stopped_reason": stopped_reason,
        "generated_at_utc": datetime.now(timezone.utc).isoformat(),
    }

    write_json_atomic(summary_path, summary)
    print("\n=== Download Summary ===", flush=True)
    print(json.dumps(summary, indent=2, ensure_ascii=False), flush=True)
    return summary


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Parallel Multi-Threaded Full-Text Source Downloader for arXiv Milestone 1."
    )
    parser.add_argument(
        "--input",
        type=Path,
        default=DEFAULT_INPUT_JSON,
        help=f"Path to discovered papers JSON (default: {DEFAULT_INPUT_JSON})",
    )
    parser.add_argument(
        "--ranges",
        type=Path,
        default=DEFAULT_RANGES_JSON,
        help=f"Path to team member ranges JSON (default: {DEFAULT_RANGES_JSON})",
    )
    parser.add_argument(
        "--output",
        type=Path,
        default=DEFAULT_OUTPUT_DIR,
        help=f"Output root directory (default: {DEFAULT_OUTPUT_DIR})",
    )
    parser.add_argument(
        "--mode",
        choices=("hybrid", "http", "s3"),
        default="hybrid",
        help="Download mode: 'hybrid' (S3 Bulk + Multi-threaded HTTP, default), 'http', or 's3'",
    )
    parser.add_argument(
        "--s3-dir",
        type=Path,
        default=DEFAULT_S3_DIR,
        help=f"Directory holding Amazon S3 bulk .tar bundles (default: {DEFAULT_S3_DIR})",
    )
    parser.add_argument(
        "--s3-sync",
        action="store_true",
        help="Automatically run `aws s3 cp --request-payer requester` (or `s3cmd`) to fetch matching S3 .tar chunks",
    )
    parser.add_argument(
        "--workers",
        type=int,
        default=8,
        help="Number of concurrent HTTP download & extraction threads (default: 8)",
    )
    parser.add_argument(
        "--checkpoint-interval",
        type=int,
        default=50,
        help="Flush download_status.json to disk every N papers (default: 50)",
    )
    parser.add_argument(
        "--member",
        help="Filter papers by team member (Minh_Hang, Trong_Nhan, Vinh_Nguyen, Yen_Vy)",
    )
    parser.add_argument("--start", help="Optional inclusive start arXiv ID (e.g. 2511.11709)")
    parser.add_argument("--end", help="Optional inclusive end arXiv ID (e.g. 2511.11720)")
    parser.add_argument(
        "--ids",
        nargs="*",
        help="Optional explicit list of arXiv IDs to download",
    )
    parser.add_argument(
        "--limit",
        type=int,
        help="Download only the first N matching papers (useful for testing)",
    )
    parser.add_argument(
        "--delay",
        type=float,
        default=0.6,
        help="Adaptive pacing delay in seconds between concurrent HTTP requests across endpoints (default: 0.6)",
    )
    parser.add_argument(
        "--discovery-time-sec",
        type=float,
        help="Optional total time (seconds) from Entry Discovery to include in end-to-end runtime",
    )
    parser.add_argument(
        "--refresh",
        action="store_true",
        help="Re-download even if a paper folder is already completed",
    )
    args = parser.parse_args(argv)

    if args.delay < 0.0:
        parser.error("--delay must be >= 0.0 seconds.")
    if args.limit is not None and args.limit < 1:
        parser.error("--limit must be a positive integer.")
    if args.workers < 1:
        parser.error("--workers must be at least 1.")
    if args.checkpoint_interval < 1:
        parser.error("--checkpoint-interval must be at least 1.")

    input_path = ensure_data_layout(args.input, args.output)
    if not input_path.exists() and LEGACY_INPUT_JSON.exists():
        input_path = LEGACY_INPUT_JSON

    raw_papers: list[dict] = []
    if input_path.exists():
        loaded = json.loads(input_path.read_text(encoding="utf-8"))
        if not isinstance(loaded, list):
            parser.error("Input JSON must contain a list of paper objects.")
        raw_papers = loaded
    elif not args.ids:
        parser.error(f"Input file not found: {input_path} (or pass --ids to resolve via arXiv API)")

    member_ranges = load_member_ranges(args.ranges)
    only_ids = set(args.ids) if args.ids else None
    try:
        selected = filter_papers(
            raw_papers,
            member_ranges=member_ranges,
            member=args.member,
            start_id=args.start,
            end_id=args.end,
            only_ids=only_ids,
            limit=args.limit,
        )
    except ValueError as exc:
        parser.error(str(exc))

    if only_ids:
        found_ids = {p["arxiv_id"] for p in selected}
        missing_ids = [pid for pid in (args.ids or []) if pid not in found_ids]
        if missing_ids:
            print(
                f"Resolving {len(missing_ids)} ID(s) directly via arXiv API: {missing_ids}",
                flush=True,
            )
            selected.extend(resolve_papers_via_arxiv_api(missing_ids, delay=max(0.5, args.delay)))

    if not selected:
        print("No papers matched the given filters.", file=sys.stderr)
        return 1

    total_versions = sum(len(p.get("versions", [])) for p in selected)
    print(
        f"Selected {len(selected)} papers ({total_versions} total versions) "
        f"from {selected[0]['arxiv_id']} to {selected[-1]['arxiv_id']} "
        f"[mode={args.mode}, parallel_workers={args.workers}, delay={args.delay}s].",
        flush=True,
    )

    s3_loader = S3BulkSourceLoader(s3_dir=args.s3_dir)
    if args.s3_sync and args.mode in ("hybrid", "s3"):
        s3_loader.sync_from_s3(selected)

    client = ArxivSourceClient(delay=args.delay, pool_maxsize=max(16, args.workers * 2))
    summary = download_corpus(
        papers=selected,
        output_dir=args.output,
        client=client,
        s3_loader=s3_loader,
        mode=args.mode,
        workers=args.workers,
        checkpoint_interval=args.checkpoint_interval,
        refresh=args.refresh,
        entry_discovery_time_sec=args.discovery_time_sec,
    )

    if summary["papers_failed"] > 0 or summary["stopped_reason"] is not None:
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
