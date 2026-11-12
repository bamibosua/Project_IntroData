"""Constants and default paths of the downloader."""

from __future__ import annotations

from pathlib import Path

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

CHECKPOINT_EVERY = 20  # papers between two writes of download_status.json and download_summary.json
MAX_FETCH_ATTEMPTS = 7
