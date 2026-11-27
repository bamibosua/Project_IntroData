"""Publication-level Train / Validation / Test splitting with zero data leakage for M3 Member 2."""
from __future__ import annotations

import argparse
from collections import Counter, defaultdict
from pathlib import Path
import random
from typing import Any

import pyarrow.parquet as pq

if __package__:
    from .common import (
        DEFAULT_CLEANING_DIR,
        DEFAULT_OUTPUT_DIR,
        LABELED_ENTRIES_SCHEMA,
        PUBLICATION_SPLITS_SCHEMA,
        load_publication_coverage,
        write_csv,
        write_json,
        write_parquet,
    )
else:
    from common import (
        DEFAULT_CLEANING_DIR,
        DEFAULT_OUTPUT_DIR,
        LABELED_ENTRIES_SCHEMA,
        PUBLICATION_SPLITS_SCHEMA,
        load_publication_coverage,
        write_csv,
        write_json,
        write_parquet,
    )


def _select_manual_eval_pubs(
    manual_pub_stats: dict[str, dict[str, Any]],
    coverage: dict[str, dict[str, Any]],
    num_members: int,
    rng: random.Random,
) -> tuple[set[str], set[str]]:
    """Select k manual publications for test and k for valid, balanced across member ranges.

    Publications with fewer than 20 clean entries are kept in train so every test/valid
    manual publication satisfies Q >= 20.
    """
    eligible = [aid for aid, st in sorted(manual_pub_stats.items()) if st["n_labeled"] >= 20]
    if len(eligible) < 2 * num_members:
        raise ValueError(
            f"Need at least {2 * num_members} manual publications with >= 20 entries for test/valid, "
            f"found {len(eligible)}."
        )

    by_member: dict[str, list[str]] = defaultdict(list)
    for aid in eligible:
        member = coverage.get(aid, {}).get("member", "unknown") or "unknown"
        by_member[member].append(aid)

    test_pubs: set[str] = set()
    valid_pubs: set[str] = set()

    # First pick 1 test and 1 valid per member range (sorted by match_rate for balanced difficulty).
    for member in sorted(by_member):
        pool = sorted(
            by_member[member],
            key=lambda a: (manual_pub_stats[a]["match_rate"], a),
        )
        if len(pool) >= 2 and len(test_pubs) < num_members and len(valid_pubs) < num_members:
            # Pick middle-ish representatives so extremes don't skew test or valid.
            mid = len(pool) // 2
            pair = [pool[mid - 1], pool[mid]] if mid > 0 else [pool[0], pool[1]]
            rng.shuffle(pair)
            test_pubs.add(pair[0])
            valid_pubs.add(pair[1])

    remaining = [aid for aid in eligible if aid not in test_pubs and aid not in valid_pubs]
    rng.shuffle(remaining)
    while len(test_pubs) < num_members:
        test_pubs.add(remaining.pop())
    while len(valid_pubs) < num_members:
        valid_pubs.add(remaining.pop())

    return test_pubs, valid_pubs


def _select_auto_eval_pubs(
    auto_pub_stats: dict[str, dict[str, Any]],
    num_members: int,
    rng: random.Random,
) -> tuple[set[str], set[str]]:
    """Select k auto publications for test and k for valid, stratified by match rate."""
    # Prefer rich, balanced publications (>= 20 entries and both matched & no_match present).
    preferred = [
        aid
        for aid, st in sorted(auto_pub_stats.items())
        if st["n_labeled"] >= 20 and st["n_matched"] >= 3 and st["n_no_match"] >= 3
    ]
    pool = preferred if len(preferred) >= 2 * num_members else sorted(auto_pub_stats)
    if len(pool) < 2 * num_members:
        raise ValueError(
            f"Need at least {2 * num_members} automatic publications for test/valid, found {len(pool)}."
        )

    # Sort by match_rate and draw stratified buckets (one test + one valid per quantile).
    ordered = sorted(pool, key=lambda a: (auto_pub_stats[a]["match_rate"], auto_pub_stats[a]["n_labeled"], a))
    bucket_size = len(ordered) // num_members
    test_pubs: set[str] = set()
    valid_pubs: set[str] = set()

    for idx in range(num_members):
        start = idx * bucket_size
        end = len(ordered) if idx == num_members - 1 else (idx + 1) * bucket_size
        bucket = list(ordered[start:end])
        rng.shuffle(bucket)
        picks = [aid for aid in bucket if aid not in test_pubs and aid not in valid_pubs][:2]
        if len(picks) == 2:
            test_pubs.add(picks[0])
            valid_pubs.add(picks[1])

    fallback = [aid for aid in ordered if aid not in test_pubs and aid not in valid_pubs]
    rng.shuffle(fallback)
    while len(test_pubs) < num_members:
        test_pubs.add(fallback.pop())
    while len(valid_pubs) < num_members:
        valid_pubs.add(fallback.pop())

    return test_pubs, valid_pubs


def build_splits(
    cleaning_dir: Path,
    output_dir: Path,
    *,
    num_members: int = 4,
    seed: int = 42,
) -> dict[str, Any]:
    """Create disjoint publication-level train/valid/test splits and export Member 3 handoff tables."""
    rng = random.Random(seed)
    coverage = load_publication_coverage(cleaning_dir)

    manual_rows = pq.read_table(output_dir / "manual_labels.parquet").to_pylist()
    auto_rows = pq.read_table(output_dir / "auto_labels.parquet").to_pylist()

    def summarize_pubs(rows: list[dict[str, Any]]) -> dict[str, dict[str, Any]]:
        stats: dict[str, dict[str, Any]] = {}
        for row in rows:
            aid = row["arxiv_id"]
            st = stats.setdefault(aid, {"n_labeled": 0, "n_matched": 0, "n_no_match": 0})
            st["n_labeled"] += 1
            if row["label_status"] == "matched":
                st["n_matched"] += 1
            else:
                st["n_no_match"] += 1
        for st in stats.values():
            st["match_rate"] = st["n_matched"] / st["n_labeled"] if st["n_labeled"] else 0.0
        return stats

    manual_stats = summarize_pubs(manual_rows)
    auto_stats = summarize_pubs(auto_rows)

    manual_test, manual_valid = _select_manual_eval_pubs(manual_stats, coverage, num_members, rng)
    auto_test, auto_valid = _select_auto_eval_pubs(auto_stats, num_members, rng)

    manual_train = set(manual_stats) - manual_test - manual_valid
    auto_train = set(auto_stats) - auto_test - auto_valid

    train_pubs = manual_train | auto_train
    valid_pubs = manual_valid | auto_valid
    test_pubs = manual_test | auto_test

    # Strict zero-leakage assertions.
    assert not (train_pubs & valid_pubs), "Data leakage between train and valid"
    assert not (train_pubs & test_pubs), "Data leakage between train and test"
    assert not (valid_pubs & test_pubs), "Data leakage between valid and test"
    assert len(manual_test) == num_members and len(auto_test) == num_members
    assert len(manual_valid) == num_members and len(auto_valid) == num_members

    partition_map: dict[str, str] = {}
    for aid in train_pubs:
        partition_map[aid] = "train"
    for aid in valid_pubs:
        partition_map[aid] = "valid"
    for aid in test_pubs:
        partition_map[aid] = "test"

    labeled_entries: list[dict[str, Any]] = []
    for row in manual_rows:
        aid = row["arxiv_id"]
        labeled_entries.append({
            "arxiv_id": aid,
            "citation_key": row["citation_key"],
            "partition": partition_map[aid],
            "label_source": "manual",
            "label_status": row["label_status"],
            "groundtruth_arxiv_id": row["groundtruth_arxiv_id"],
            "match_rule": "manual",
        })
    for row in auto_rows:
        aid = row["arxiv_id"]
        labeled_entries.append({
            "arxiv_id": aid,
            "citation_key": row["citation_key"],
            "partition": partition_map[aid],
            "label_source": "auto",
            "label_status": row["label_status"],
            "groundtruth_arxiv_id": row["groundtruth_arxiv_id"],
            "match_rule": row["match_rule"],
        })
    labeled_entries.sort(key=lambda r: (r["partition"], r["arxiv_id"], r["citation_key"]))

    pub_split_rows: list[dict[str, Any]] = []
    for source_name, stats_map in (("manual", manual_stats), ("auto", auto_stats)):
        for aid, st in sorted(stats_map.items()):
            cov = coverage.get(aid, {})
            pub_split_rows.append({
                "arxiv_id": aid,
                "partition": partition_map[aid],
                "label_source": source_name,
                "n_bibtex_entries": int(cov.get("bibtex_clean", st["n_labeled"])),
                "n_labeled_entries": int(st["n_labeled"]),
                "n_matched": int(st["n_matched"]),
                "n_no_match": int(st["n_no_match"]),
                "n_ref_candidates": int(cov.get("references_clean", 0)),
            })
    pub_split_rows.sort(key=lambda r: (r["partition"], r["label_source"], r["arxiv_id"]))

    write_parquet(output_dir / "labeled_entries.parquet", labeled_entries, LABELED_ENTRIES_SCHEMA)
    write_parquet(output_dir / "publication_splits.parquet", pub_split_rows, PUBLICATION_SPLITS_SCHEMA)
    write_csv(
        output_dir / "publication_splits.csv",
        pub_split_rows,
        [
            "arxiv_id",
            "partition",
            "label_source",
            "n_bibtex_entries",
            "n_labeled_entries",
            "n_matched",
            "n_no_match",
            "n_ref_candidates",
        ],
    )

    partition_summary: dict[str, dict[str, Any]] = {}
    for part in ("train", "valid", "test"):
        part_pubs = [r for r in pub_split_rows if r["partition"] == part]
        part_entries = [r for r in labeled_entries if r["partition"] == part]
        n_matched = sum(1 for r in part_entries if r["label_status"] == "matched")
        partition_summary[part] = {
            "n_publications": len(part_pubs),
            "n_manual_publications": sum(1 for r in part_pubs if r["label_source"] == "manual"),
            "n_auto_publications": sum(1 for r in part_pubs if r["label_source"] == "auto"),
            "n_labeled_entries": len(part_entries),
            "n_matched": n_matched,
            "n_no_match": len(part_entries) - n_matched,
            "match_rate": round(n_matched / len(part_entries), 4) if part_entries else 0.0,
            "manual_arxiv_ids": sorted(r["arxiv_id"] for r in part_pubs if r["label_source"] == "manual"),
            "auto_arxiv_ids_sample": sorted(
                r["arxiv_id"] for r in part_pubs if r["label_source"] == "auto"
            )[:10],
        }

    summary = {
        "seed": seed,
        "group_size_k": num_members,
        "zero_publication_leakage_verified": True,
        "total_publications": len(pub_split_rows),
        "total_labeled_entries": len(labeled_entries),
        "partitions": partition_summary,
    }
    write_json(output_dir / "split_summary.json", summary)
    return summary


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Create publication-level train/valid/test splits for M3.")
    parser.add_argument("--cleaning-dir", type=Path, default=DEFAULT_CLEANING_DIR)
    parser.add_argument("--output-dir", type=Path, default=DEFAULT_OUTPUT_DIR)
    parser.add_argument("--num-members", type=int, default=4)
    parser.add_argument("--seed", type=int, default=42)
    args = parser.parse_args(argv)

    summary = build_splits(
        args.cleaning_dir,
        args.output_dir,
        num_members=args.num_members,
        seed=args.seed,
    )
    for part in ("train", "valid", "test"):
        st = summary["partitions"][part]
        print(
            f"[SPLIT] {part:<5}: {st['n_publications']:>4} pubs "
            f"(manual={st['n_manual_publications']}, auto={st['n_auto_publications']}) | "
            f"{st['n_labeled_entries']:>6,} entries "
            f"(matched={st['n_matched']:,}, no_match={st['n_no_match']:,}, rate={st['match_rate']:.3f})"
        )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
