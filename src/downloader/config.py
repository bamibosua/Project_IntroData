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

PROJECT_ROOT = Path(__file__).resolve().parents[2]
DEFAULT_INDEX_JSON = PROJECT_ROOT / "data" / "input" / "papers_index.json"
DEFAULT_LEGACY_JSON = PROJECT_ROOT / "data" / "discovery" / "discovered_papers_with_revised_dates.json"
DEFAULT_OUTPUT_DIR = PROJECT_ROOT / "data" / "output"

CHECKPOINT_EVERY = 50  # papers between two writes of download_status.json
MAX_FETCH_ATTEMPTS = 7
