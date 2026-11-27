"""Consolidate and verify team manual labels.csv files against Member 1 clean Parquet tables."""
from __future__ import annotations

import argparse
from collections import Counter
import csv
import json
from pathlib import Path
from typing import Any

import pyarrow.parquet as pq

if __package__:
    from .common import (
        DEFAULT_CLEANING_DIR,
        DEFAULT_MANUAL_ROOT,
        DEFAULT_MEMBER_DIRS,
        DEFAULT_OUTPUT_DIR,
        MANUAL_LABELS_SCHEMA,
        VALID_LABEL_STATUSES,
        normalize_arxiv_id,
        write_csv,
        write_json,
        write_parquet,
    )
else:
    from common import (
        DEFAULT_CLEANING_DIR,
        DEFAULT_MANUAL_ROOT,
        DEFAULT_MEMBER_DIRS,
        DEFAULT_OUTPUT_DIR,
        MANUAL_LABELS_SCHEMA,
        VALID_LABEL_STATUSES,
        normalize_arxiv_id,
        write_csv,
        write_json,
        write_parquet,
    )


def discover_manual_csv_paths(
    manual_root: Path, member_dirs: tuple[str, ...] = DEFAULT_MEMBER_DIRS
) -> list[Path]:
    """Return existing labels.csv files under the member directories in deterministic order."""
    paths: list[Path] = []
    for member_dir in member_dirs:
        candidate = manual_root / member_dir / "labels.csv"
        if candidate.is_file():
            paths.append(candidate)
    if not paths:
        raise FileNotFoundError(
            f"No member labels.csv files found under {manual_root} ({', '.join(member_dirs)})."
        )
    return paths


def consolidate_manual_labels(
    cleaning_dir: Path,
    manual_root: Path,
    output_dir: Path,
    *,
    csv_paths: list[Path] | None = None,
) -> dict[str, Any]:
    """Read, normalize, verify, and consolidate manual label CSVs without adding annotator column."""
    source_paths = csv_paths if csv_paths is not None else discover_manual_csv_paths(manual_root)

    bib_table = pq.read_table(
        cleaning_dir / "bibtex_clean.parquet", columns=["arxiv_id", "citation_key"]
    )
    ref_table = pq.read_table(
        cleaning_dir / "references_clean.parquet", columns=["arxiv_id", "ref_arxiv_id"]
    )

    bib_keys_by_pub: dict[str, set[str]] = {}
    for aid, key in zip(bib_table["arxiv_id"].to_pylist(), bib_table["citation_key"].to_pylist()):
        bib_keys_by_pub.setdefault(aid, set()).add(key)

    ref_keys_by_pub: dict[str, set[str]] = {}
    for aid, ref_id in zip(
        ref_table["arxiv_id"].to_pylist(), ref_table["ref_arxiv_id"].to_pylist()
    ):
        ref_keys_by_pub.setdefault(aid, set()).add(ref_id)

    clean_rows: list[dict[str, Any]] = []
    excluded_rows: list[dict[str, Any]] = []
    seen_keys: set[tuple[str, str]] = set()
    per_file_stats: dict[str, dict[str, Any]] = {}
    duplicate_refs_in_pub: list[dict[str, Any]] = []

    for csv_path in source_paths:
        file_label = csv_path.parent.name
        file_counter = Counter()
        file_pubs: set[str] = set()
        with csv_path.open("r", encoding="utf-8-sig", newline="") as stream:
            reader = csv.DictReader(stream)
            required_cols = {"arxiv_id", "citation_key", "groundtruth_arxiv_id", "label_status"}
            if not reader.fieldnames or not required_cols.issubset(set(reader.fieldnames)):
                raise ValueError(f"{csv_path}: missing required columns {sorted(required_cols)}")

            for line_num, raw in enumerate(reader, start=2):
                aid = normalize_arxiv_id(raw.get("arxiv_id"))
                citation_key = (raw.get("citation_key") or "").strip()
                status = (raw.get("label_status") or "").strip().lower()
                raw_gt = (raw.get("groundtruth_arxiv_id") or "").strip()
                gt_id = normalize_arxiv_id(raw_gt) if raw_gt else None
                note = (raw.get("note") or "").strip() or None

                if not aid or not citation_key:
                    raise ValueError(f"{csv_path}:{line_num}: missing arxiv_id or citation_key")
                if status not in VALID_LABEL_STATUSES:
                    raise ValueError(
                        f"{csv_path}:{line_num}: invalid label_status={status!r} "
                        "(unreviewed empty cells must not be treated as no_match)"
                    )
                if status == "matched" and not gt_id:
                    raise ValueError(
                        f"{csv_path}:{line_num}: matched entry has empty/invalid groundtruth_arxiv_id={raw_gt!r}"
                    )
                if status == "no_match" and raw_gt:
                    raise ValueError(
                        f"{csv_path}:{line_num}: no_match entry must have empty groundtruth_arxiv_id, got {raw_gt!r}"
                    )

                pair_key = (aid, citation_key)
                if pair_key in seen_keys:
                    raise ValueError(f"{csv_path}:{line_num}: duplicate (arxiv_id, citation_key)={pair_key}")
                seen_keys.add(pair_key)

                if citation_key not in bib_keys_by_pub.get(aid, set()):
                    excluded_rows.append({
                        "source_file": f"{file_label}/{csv_path.name}",
                        "line": line_num,
                        "arxiv_id": aid,
                        "citation_key": citation_key,
                        "groundtruth_arxiv_id": gt_id,
                        "label_status": status,
                        "note": note,
                        "reason": "missing_in_bibtex_clean",
                    })
                    file_counter["excluded_missing_in_bibtex_clean"] += 1
                    continue

                if status == "matched" and gt_id not in ref_keys_by_pub.get(aid, set()):
                    raise ValueError(
                        f"{csv_path}:{line_num}: groundtruth_arxiv_id={gt_id!r} "
                        f"not found in references_clean.parquet for publication {aid}"
                    )

                clean_rows.append({
                    "arxiv_id": aid,
                    "citation_key": citation_key,
                    "groundtruth_arxiv_id": gt_id,
                    "label_status": status,
                    "note": note,
                })
                file_pubs.add(aid)
                file_counter["kept"] += 1
                file_counter[status] += 1

        per_file_stats[file_label] = {
            "path": str(csv_path),
            "publications": sorted(file_pubs),
            "n_publications": len(file_pubs),
            **dict(file_counter),
        }

    # Sort deterministically by (arxiv_id, citation_key).
    clean_rows.sort(key=lambda r: (r["arxiv_id"], r["citation_key"]))

    # Check per-publication counts and duplicate ref targets within the same publication.
    pub_counts: dict[str, dict[str, Any]] = {}
    gt_seen_per_pub: dict[str, dict[str, list[str]]] = {}
    for row in clean_rows:
        aid = row["arxiv_id"]
        info = pub_counts.setdefault(
            aid,
            {
                "n_labeled": 0,
                "n_matched": 0,
                "n_no_match": 0,
                "n_bibtex_clean": len(bib_keys_by_pub.get(aid, set())),
                "n_ref_candidates": len(ref_keys_by_pub.get(aid, set())),
            },
        )
        info["n_labeled"] += 1
        if row["label_status"] == "matched":
            info["n_matched"] += 1
            gt_seen_per_pub.setdefault(aid, {}).setdefault(row["groundtruth_arxiv_id"], []).append(
                row["citation_key"]
            )
        else:
            info["n_no_match"] += 1

    for aid, ref_map in sorted(gt_seen_per_pub.items()):
        for ref_id, keys in sorted(ref_map.items()):
            if len(keys) > 1:
                duplicate_refs_in_pub.append({
                    "arxiv_id": aid,
                    "groundtruth_arxiv_id": ref_id,
                    "citation_keys": keys,
                })

    pubs_below_20 = {}
    for aid, info in sorted(pub_counts.items()):
        if info["n_labeled"] < 20:
            labeled_keys = {r["citation_key"] for r in clean_rows if r["arxiv_id"] == aid}
            available_extra = sorted(bib_keys_by_pub.get(aid, set()) - labeled_keys)
            pubs_below_20[aid] = {
                "n_labeled": info["n_labeled"],
                "missing_to_reach_20": 20 - info["n_labeled"],
                "available_unlabeled_keys_sample": available_extra[:5],
            }

    output_dir.mkdir(parents=True, exist_ok=True)
    write_parquet(output_dir / "manual_labels.parquet", clean_rows, MANUAL_LABELS_SCHEMA)
    write_csv(
        output_dir / "manual_labels.csv",
        clean_rows,
        ["arxiv_id", "citation_key", "groundtruth_arxiv_id", "label_status", "note"],
    )
    if excluded_rows:
        excluded_path = output_dir / "manual_excluded_keys.jsonl"
        with excluded_path.open("w", encoding="utf-8") as stream:
            for item in excluded_rows:
                stream.write(json.dumps(item, ensure_ascii=False) + "\n")

    summary = {
        "n_source_files": len(source_paths),
        "n_publications": len(pub_counts),
        "n_labeled_entries": len(clean_rows),
        "n_matched": sum(1 for r in clean_rows if r["label_status"] == "matched"),
        "n_no_match": sum(1 for r in clean_rows if r["label_status"] == "no_match"),
        "n_excluded_missing_in_bibtex_clean": len(excluded_rows),
        "excluded_entries": excluded_rows,
        "publications_below_20_clean_entries": pubs_below_20,
        "duplicate_groundtruth_within_publication": duplicate_refs_in_pub,
        "per_source_stats": per_file_stats,
        "per_publication_stats": dict(sorted(pub_counts.items())),
    }
    write_json(output_dir / "manual_summary.json", summary)
    return summary


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Consolidate manual labels.csv files for M3.")
    parser.add_argument("--cleaning-dir", type=Path, default=DEFAULT_CLEANING_DIR)
    parser.add_argument("--manual-root", type=Path, default=DEFAULT_MANUAL_ROOT)
    parser.add_argument("--output-dir", type=Path, default=DEFAULT_OUTPUT_DIR)
    args = parser.parse_args(argv)

    summary = consolidate_manual_labels(args.cleaning_dir, args.manual_root, args.output_dir)
    print(
        f"[MANUAL] Consolidated {summary['n_labeled_entries']} entries across "
        f"{summary['n_publications']} publications "
        f"(matched={summary['n_matched']}, no_match={summary['n_no_match']}, "
        f"excluded={summary['n_excluded_missing_in_bibtex_clean']})."
    )
    if summary["publications_below_20_clean_entries"]:
        print(
            "[MANUAL] Warning: publications below 20 clean entries after FK filtering: "
            + ", ".join(
                f"{aid}({info['n_labeled']})"
                for aid, info in summary["publications_below_20_clean_entries"].items()
            )
        )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
