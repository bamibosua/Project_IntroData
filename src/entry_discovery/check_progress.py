"""Offline inspection of existing Entry Discovery checkpoints; no Internet needed."""

import argparse
import hashlib
import json
import sys
from pathlib import Path


def main():
    parser = argparse.ArgumentParser(
        description="Count valid completed batches and unfinished work"
    )
    parser.add_argument("--ids-file", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--batch-size", type=int, default=20)
    args = parser.parse_args()
    if args.batch_size < 1:
        parser.error("--batch-size must be positive")
    ids = [
        x.strip()
        for x in args.ids_file.read_text(encoding="utf-8").splitlines()
        if x.strip() and not x.lstrip().startswith("#")
    ]
    expected = {
        "ids_sha256": hashlib.sha256(
            ("\n".join(ids) + "\n").encode("utf-8")
        ).hexdigest(),
        "total_ids": len(ids),
        "batch_size": args.batch_size,
    }
    manifest = args.output / "checkpoint_manifest.json"
    if (
        not manifest.exists()
        or json.loads(manifest.read_text(encoding="utf-8")) != expected
    ):
        parser.error(
            "Input list or batch size does not match this output checkpoint manifest. No files changed."
        )
    completed = 0
    incomplete = []
    corrupt = []
    found = 0
    not_found = 0
    count = (len(ids) + args.batch_size - 1) // args.batch_size
    for i in range(count):
        chunk = ids[i * args.batch_size : (i + 1) * args.batch_size]
        path = args.output / "batches" / f"batch_{i:05d}.json"
        if not path.exists():
            incomplete.append(i + 1)
            continue
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
            if (
                not isinstance(data, dict)
                or set(data) != set(chunk)
                or any(
                    not isinstance(r, dict)
                    or r.get("status") not in ("found", "not_found")
                    for r in data.values()
                )
            ):
                raise ValueError("invalid batch contents")
            completed += 1
            found += sum(r["status"] == "found" for r in data.values())
            not_found += sum(r["status"] == "not_found" for r in data.values())
        except (json.JSONDecodeError, ValueError, KeyError, TypeError):
            corrupt.append(i + 1)
    report = {
        "total_ids": len(ids),
        "total_batches": count,
        "completed_batches": completed,
        "remaining_batches": len(incomplete),
        "corrupt_batches": len(corrupt),
        "confirmed_found_ids": found,
        "confirmed_not_found_ids": not_found,
        "remaining_batch_numbers_1_based_preview": incomplete[:30],
        "corrupt_batch_numbers_1_based_preview": corrupt[:30],
    }
    print(json.dumps(report, ensure_ascii=False, indent=2))
    if corrupt:
        print(
            "Some checkpoint files are invalid. Back up data and investigate; do not delete all data.",
            file=sys.stderr,
        )
        return 2
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
