"""Command line: python -m dedup_fulltext [--input DIR|ZIP] [--output DIR] [--cite-map JSON] [--limit N]
[--batch-size N] [--restart] [--keep-shards]

An interrupted run is resumed by running the same command again (see runner.py); use --restart to start over.
"""

from __future__ import annotations

import argparse
from pathlib import Path

from .runner import DEFAULT_BATCH_SIZE, DEFAULT_OUTPUT_DIR, run_deduplication


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="dedup_fulltext", description="Milestone 2: cross-version full-text content deduplication.")
    parser.add_argument("--input", type=Path, default=None,
                        help="cleaned_structure directory or .zip (default: data/{output,input}/cleaned_structure[.zip])")
    parser.add_argument("--output", type=Path, default=DEFAULT_OUTPUT_DIR,
                        help="folder for elements.jsonl, hierarchy.jsonl, coverage.jsonl, dedup_summary.json "
                             f"(default: {DEFAULT_OUTPUT_DIR})")
    parser.add_argument("--cite-map", type=Path, default=None,
                        help="optional JSON {arxiv_id: {old_cite_key: unified_cite_key}} from reference deduplication")
    parser.add_argument("--limit", type=int, default=None,
                        help="only the first N publications (sorted by key); output goes to <output>/limit_N/")
    parser.add_argument("--batch-size", type=int, default=DEFAULT_BATCH_SIZE,
                        help=f"publications per checkpoint shard (default: {DEFAULT_BATCH_SIZE})")
    parser.add_argument("--restart", action="store_true", help="discard the checkpoint of an interrupted run")
    parser.add_argument("--keep-shards", action="store_true", help="keep the checkpoint shards after a finished run")
    parser.add_argument("--keep-container-text", action="store_true",
                        help="store the full text also in paragraphs/lists/items (about 2x larger `elements`)")
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    run_deduplication(args.input, args.output, limit=args.limit, cite_map_path=args.cite_map,
                      batch_size=args.batch_size, resume=not args.restart, keep_shards=args.keep_shards,
                      compact_containers=not args.keep_container_text)
    return 0
