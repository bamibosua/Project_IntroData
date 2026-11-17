"""Command-line interface:  python -m src.downloader [options]"""

from __future__ import annotations

import argparse
from pathlib import Path
import sys

from .config import DEFAULT_CONNECTIONS, DEFAULT_DELAY, DEFAULT_INDEX_JSON, DEFAULT_OUTPUT_DIR, USER_AGENT
from .index import load_papers_index
from .runner import download_corpus


def _positive_int(text: str) -> int:
    value = int(text)
    if value < 1:
        raise argparse.ArgumentTypeError("must be >= 1")
    return value


def _positive_float(text: str) -> float:
    value = float(text)
    if not value > 0:
        raise argparse.ArgumentTypeError("must be > 0")
    return value


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(description="Full-Text Source Downloader for arXiv Milestone 1.")
    p.add_argument("--index", type=Path, default=DEFAULT_INDEX_JSON, help="Path to papers_index.json")
    p.add_argument("--output", type=Path, default=DEFAULT_OUTPUT_DIR, help="Output root directory")
    p.add_argument("--workers", type=_positive_int, default=8, help="Extraction/disk worker threads (default: 8)")
    p.add_argument("--delay", type=_positive_float, default=DEFAULT_DELAY,
                   help=f"Min seconds between HTTP requests, global across all threads (default: {DEFAULT_DELAY:g}; "
                        "the brief says 3, lowered with the instructor's permission)")
    p.add_argument("--connections", type=_positive_int, default=DEFAULT_CONNECTIONS,
                   help=f"Downloads in flight at once (default: {DEFAULT_CONNECTIONS}); the request rate is "
                        "capped by --delay, so more connections only hide network latency")
    p.add_argument("--limit", type=_positive_int, help="Only process the first N papers")
    p.add_argument("--contact-email", help="Added to the User-Agent so arXiv can reach you if needed")
    p.add_argument("--skip-audit", action="store_true", help="Skip the post-crawl recovery of missing versions")
    return p


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    if not args.contact_email and "@" not in USER_AGENT:
        print("WARNING: no --contact-email; the User-Agent will not tell arXiv how to reach you.", file=sys.stderr)
    summary = download_corpus(
        papers_index=load_papers_index(args.index),
        output_dir=args.output,
        workers=args.workers,
        delay=args.delay,
        limit=args.limit,
        connections=args.connections,
        contact_email=args.contact_email,
        skip_audit=args.skip_audit,
    )
    return summary.get("exit_code") or 0