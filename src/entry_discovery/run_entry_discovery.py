#!/usr/bin/env python3
"""Run the full Entry Discovery pipeline with one command.

Keep this script alongside the other scripts in src/entry_discovery/.
From the repository root: python src/entry_discovery/run_entry_discovery.py
From another directory, use the absolute path to this script.

Stages:
  1. prepare_group.py -> data/input/{group_ids.txt, id_ownership.csv, group_summary.json}
  2. smart_discovery.py -> data/discovery/
  3. prepare_papers_handoff.py -> data/input/papers_index.json and data/output/paper/

Safeguards: resume existing metadata checkpoints; only start the handoff when
all input IDs have metadata and complete revision dates; do not overwrite changed
paper metadata unless explicitly authorized with --overwrite-metadata.
"""

from __future__ import annotations

import argparse
import json
import math
import subprocess
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent
INPUT = ROOT / "data" / "input"
DISCOVERY = ROOT / "data" / "discovery"
OUTPUT_PAPERS = ROOT / "data" / "output" / "paper"
INDEX = INPUT / "papers_index.json"


def read_json(path: Path):
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        raise RuntimeError(f"Cannot read valid JSON: {path}: {exc}") from exc


def call_stage(number: int, title: str, script: str, arguments: list[str]) -> None:
    path = ROOT / script
    if not path.is_file():
        raise RuntimeError(f"Missing {path}. Keep this runner alongside the pipeline scripts in entry_discovery/.")
    command = [sys.executable, "-u", str(path), *arguments]
    print(f"\n{'=' * 68}\n[STEP {number}/3] {title}\n{'=' * 68}", flush=True)
    print(f"Command: {' '.join(command)}", flush=True)
    started = time.perf_counter()
    try:
        subprocess.run(command, cwd=ROOT, check=True)
    except subprocess.CalledProcessError as exc:
        raise RuntimeError(
            f"STEP {number} failed (exit code {exc.returncode}). "
            "Previous checkpoint files are preserved. Fix the issue and rerun this same script."
        ) from exc
    print(f"[STEP {number}/3 DONE] {title} | {time.perf_counter() - started:.1f}s", flush=True)


def select_batch_size(requested: int | None) -> int:
    print("[CHECKPOINT] Checking saved metadata batch configuration...", flush=True)
    manifest = DISCOVERY / "checkpoint_manifest.json"
    if manifest.is_file():
        saved = read_json(manifest)
        existing = saved.get("batch_size")
        if type(existing) is not int or not 1 <= existing <= 50:
            raise RuntimeError(f"Invalid batch_size in existing checkpoint manifest: {manifest}")
        if requested is not None and requested != existing:
            raise RuntimeError(
                f"Existing checkpoint uses batch size {existing}, not {requested}. "
                "Rerun without --batch-size, or use a fresh discovery folder; "
                "do not mix incompatible checkpoint batches."
            )
        print(f"[INFO] Resuming existing metadata checkpoints with batch size {existing}.", flush=True)
        return existing
    return requested if requested is not None else 50


def verify_discovery(ids_file: Path) -> tuple[int, int]:
    print("[VERIFY] Reading input IDs and discovery outputs...", flush=True)
    ids = [line.strip() for line in ids_file.read_text(encoding="utf-8").splitlines()
           if line.strip() and not line.lstrip().startswith("#")]
    if not ids or len(ids) != len(set(ids)):
        raise RuntimeError("ID list is empty or contains duplicates.")

    summary = read_json(DISCOVERY / "smart_summary.json")
    papers = read_json(DISCOVERY / "discovered_papers_with_revised_dates.json")
    expected_count = len(ids)
    if not isinstance(papers, list):
        raise RuntimeError("Discovery output must be a JSON array of paper objects.")
    if (summary.get("total_ids") != expected_count
            or summary.get("pending_papers") != 0
            or summary.get("stopped_reason") is not None
            or summary.get("found") != expected_count):
        raise RuntimeError(
            "Discovery not fully completed; handoff will NOT run. "
            f"Expected {expected_count} found, got found={summary.get('found')}, "
            f"pending={summary.get('pending_papers')}, "
            f"stopped_reason={summary.get('stopped_reason')}. "
            "Rerun to resume existing checkpoints."
        )

    found_ids: set[str] = set()
    versions_total = 0
    print(f"[VERIFY] Checking metadata, version lists and revision dates for {expected_count} papers...", flush=True)
    for paper in papers:
        if not isinstance(paper, dict):
            raise RuntimeError("Paper entry must be a JSON object.")
        pid = paper.get("arxiv_id")
        latest = paper.get("latest_version")
        if not isinstance(pid, str) or pid in found_ids or type(latest) is not int or latest < 1:
            raise RuntimeError(f"Duplicate/invalid paper or version: {pid!r}")
        found_ids.add(pid)
        if paper.get("status") != "found":
            raise RuntimeError(f"Paper {pid} does not have status 'found'.")
        if paper.get("versions") != [f"{pid}v{i}" for i in range(1, latest + 1)]:
            raise RuntimeError(f"Inconsistent version list for {pid}.")
        dates = paper.get("revised_dates")
        if (not isinstance(dates, list) or len(dates) != latest - 1
                or any(not isinstance(d, str) or not d for d in dates)):
            raise RuntimeError(f"Missing or incomplete revised_dates for {pid}.")
        versions_total += latest
    if found_ids != set(ids):
        raise RuntimeError("Discovery paper IDs do not exactly match the input ID list.")
    print(f"[CHECK] OK: {expected_count} papers, {versions_total} versions, no pending paper.", flush=True)
    return expected_count, versions_total


def main() -> int:
    parser = argparse.ArgumentParser(description="One-command Entry Discovery pipeline")
    parser.add_argument("--batch-size", type=int, default=None,
                        help="Metadata batch size (default 50, auto-detect existing checkpoint)")
    parser.add_argument("--revision-batch-size", type=int, default=50)
    parser.add_argument("--delay", type=float, default=3.5)
    parser.add_argument("--cooldown", type=float, default=120,
                        help="Initial fallback backoff when Retry-After is absent (seconds)")
    parser.add_argument("--max-cooldown", type=float, default=1800)
    parser.add_argument("--max-delay", type=float, default=30)
    parser.add_argument("--max-consecutive-429", type=int, default=0,
                        help="0 retries 429 indefinitely; positive values stop after N consecutive 429s")
    parser.add_argument("--recovery-rounds", type=int, default=5,
                        help="Maximum consecutive rounds without progress on missing data")
    parser.add_argument("--pass-number", type=int, choices=(1, 2), default=1)
    parser.add_argument("--overwrite-metadata", action="store_true",
                        help="Allow updating existing metadata.json; never changes source or references")
    args = parser.parse_args()
    if args.batch_size is not None and not 1 <= args.batch_size <= 50:
        parser.error("--batch-size must be between 1 and 50")
    if not 1 <= args.revision_batch_size <= 50:
        parser.error("--revision-batch-size must be between 1 and 50")
    if not all(math.isfinite(x) for x in (args.delay, args.cooldown, args.max_cooldown, args.max_delay)):
        parser.error("Timing arguments must be finite")
    if args.delay < 3.0 or args.cooldown < 60.0:
        parser.error("--delay must be >= 3 and --cooldown must be >= 60")
    if args.max_cooldown < args.cooldown or args.max_delay < args.delay:
        parser.error("max-cooldown/max-delay must be >= cooldown/delay")
    if args.max_consecutive_429 < 0 or args.recovery_rounds < 1:
        parser.error("max-consecutive-429 must be nonnegative and recovery-rounds positive")

    try:
        print("[INIT] Checking pipeline configuration...", flush=True)
        config = ROOT / "team_ranges.json"
        if not config.is_file():
            raise RuntimeError(f"Missing config: {config}")
        print(f"[ROOT] {ROOT}", flush=True)
        print("[INFO] Using the current Python interpreter for all three stages.", flush=True)

        # 1. Build list and team ownership information.
        call_stage(1, "Prepare assigned arXiv IDs", "prepare_group.py", [
            "--config", str(config), "--output", str(INPUT)
        ])
        ids_file = INPUT / "group_ids.txt"
        print(f"[CHECK] Checking generated ID list: {ids_file}", flush=True)
        if not ids_file.is_file():
            raise RuntimeError(f"No ID list created at {ids_file}")

        # Reuse an existing checkpoint's batch size; never silently invalidate it.
        batch_size = select_batch_size(args.batch_size)
        print(f"[CONFIG] Metadata batch size: {batch_size} | Revision batch size: {args.revision_batch_size}", flush=True)

        # 2. Harvest metadata and revision dates using Global Revision Batching.
        call_stage(2, "Crawl metadata and revision dates", "smart_discovery.py", [
            "--ids-file", str(ids_file),
            "--output", str(DISCOVERY),
            "--batch-size", str(batch_size),
            "--revision-batch-size", str(args.revision_batch_size),
            "--delay", str(args.delay),
            "--cooldown", str(args.cooldown),
            "--max-cooldown", str(args.max_cooldown),
            "--max-delay", str(args.max_delay),
            "--max-consecutive-429", str(args.max_consecutive_429),
            "--recovery-rounds", str(args.recovery_rounds),
            "--pass-number", str(args.pass_number),
        ])

        count, versions_total = verify_discovery(ids_file)

        # 3. Generate paper folders and the index only after successful validation.
        handoff_args = [
            "--input", str(DISCOVERY / "discovered_papers_with_revised_dates.json"),
            "--papers-dir", str(OUTPUT_PAPERS),
            "--index-file", str(INDEX),
        ]
        if args.overwrite_metadata:
            handoff_args.append("--overwrite-metadata")
        call_stage(3, "Export per-paper metadata, version folders, and index", "prepare_papers_handoff.py",
                   handoff_args)

        print(f"[VERIFY] Checking exported papers index: {INDEX}", flush=True)
        generated_index = read_json(INDEX)
        if not isinstance(generated_index, dict) or len(generated_index) != count:
            raise RuntimeError(f"Index file incomplete: {INDEX}")
        print("\n" + "=" * 68, flush=True)
        print("[DONE] All three Entry Discovery steps finished successfully.", flush=True)
        print(f"Papers: {count} | Versions: {versions_total}", flush=True)
        print(f"IDs and ownership: {INPUT}", flush=True)
        print(f"Crawled metadata:  {DISCOVERY}", flush=True)
        print(f"Index file:        {INDEX}", flush=True)
        print(f"Paper folders:     {OUTPUT_PAPERS}", flush=True)
        return 0
    except (RuntimeError, OSError, ValueError) as exc:
        print(f"\n[ERROR] {exc}", file=sys.stderr, flush=True)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
