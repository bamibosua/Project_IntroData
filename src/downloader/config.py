"""Constants and default paths of the downloader."""

from __future__ import annotations

from pathlib import Path

SRC_ENDPOINTS = [
    "https://export.arxiv.org/src",
    "https://export.arxiv.org/e-print",
]
ALLOWED_EXTENSIONS = {".tex", ".bib"}
# Kept only when the archive has no .bib and no \bibitem inside the kept .tex (the .bbl is then the only
# bibliography available to the later reference-extraction step).
CONDITIONAL_EXTENSIONS = {".bbl"}
USER_AGENT = "HCMUS-IDS-M1-Downloader/3.0 (Academic Research) contact: huynhvy180706@gmail.com"  # honest, fixed identity (no rotation)
DEFAULT_DELAY = 0.6  # seconds between request starts, global (course rule: >= 3 s + descriptive User-Agent)
MAX_ADAPTIVE_DELAY = 1.8  # back-off ceiling for small delays; the effective ceiling is max(this, 2 x delay)
MAX_VERSION_ATTEMPTS = 3  # a version that failed this many times (across runs) is no longer retried
PERMANENT_ERROR_TYPES = {"corrupt_gzip", "corrupt_tar", "corrupt_zip", "unsupported_zip", "gzip_too_large"}
MAX_BLOCKED_STREAK = 5  # stop the whole run after this many papers in a row ended in HTTP 403
MAX_TAR_TOTAL_BYTES = 4 * 1024 ** 3  # streamed-tar fallback: ceiling on the summed size of all members
MAX_403_ATTEMPTS = 2  # 403 means blocked/forbidden rather than a transient rate limit
SCHEMA_VERSION = 2  # layout of a download_status.json record
DEFAULT_CONNECTIONS = 8  # in-flight downloads; throughput is bounded by the global request spacing anyway
AUDIT_MAX_CONSECUTIVE_FAILURES = 10  # circuit breaker: stop the audit if this many recoveries fail in a row

PROJECT_ROOT = Path(__file__).resolve().parents[2]
DEFAULT_INDEX_JSON = PROJECT_ROOT / "data" / "input" / "papers_index.json"
DEFAULT_OUTPUT_DIR = PROJECT_ROOT / "data" / "output"
DOWNLOAD_META_DIR_NAME = "papers_download_meta"

CHECKPOINT_EVERY = 20  # papers between two writes of download_status.json
MAX_FETCH_ATTEMPTS = 7
MAX_RETRY_AFTER_SEC = 300.0  # cap on a server supplied Retry-After
MAX_ARCHIVE_BYTES = 500 * 1024 * 1024  # refuse to hold a larger raw archive in RAM
MAX_MEMBER_BYTES = 100 * 1024 * 1024  # largest single .tex/.bib kept
MAX_UNCOMPRESSED_BYTES = 500 * 1024 * 1024  # total kept bytes / gzip output cap per version
