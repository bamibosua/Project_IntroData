"""Command-line interface:  python -m src.downloader [options]"""

from __future__ import annotations

import argparse
from pathlib import Path

from .config import DEFAULT_INDEX_JSON, DEFAULT_OUTPUT_DIR
from .index import load_papers_index
from .runner import download_corpus


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(description="Full-Text Source Downloader for arXiv Milestone 1.")
    p.add_argument("--index", type=Path, default=DEFAULT_INDEX_JSON, help="Path to papers_index.json")
    p.add_argument("--output", type=Path, default=DEFAULT_OUTPUT_DIR, help="Output root directory")
    p.add_argument("--workers", type=int, default=8, help="Extraction/disk worker threads (default: 8)")
    p.add_argument("--delay", type=float, default=0.6, help="Delay between HTTP requests (default: 0.6s)")
    p.add_argument("--connections", type=int, default=32, help="Max concurrent HTTP downloads (default: 32)")
    p.add_argument("--limit", type=int, help="Only process the first N papers")
    return p


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    download_corpus(
        papers_index=load_papers_index(args.index),
        output_dir=args.output,
        workers=args.workers,
        delay=args.delay,
        limit=args.limit,
        connections=args.connections,
    )
    return 0
