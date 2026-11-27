"""Validate consolidated manual and automatic labels before data splitting."""
from __future__ import annotations

import argparse
from collections import Counter
import math
from pathlib import Path
from typing import Any

import pyarrow.parquet as pq

if __package__:
    from .common import (
        AUTO_LABELS_SCHEMA,
        DEFAULT_CLEANING_DIR,
        DEFAULT_OUTPUT_DIR,
        MANUAL_LABELS_SCHEMA,
        VALID_LABEL_STATUSES,
        load_publication_coverage,
        load_quarantine_ref_conflicts,
        write_json,
    )
else:
    from common import (
        AUTO_LABELS_SCHEMA,
        DEFAULT_CLEANING_DIR,
        DEFAULT_OUTPUT_DIR,
        MANUAL_LABELS_SCHEMA,
        VALID_LABEL_STATUSES,
        load_publication_coverage,
        load_quarantine_ref_conflicts,
        write_json,
    )


def _validate_label_rows(
    rows: list[dict[str, Any]],
    source_name: str,
    bib_by_pub: dict[str, set[str]],
    ref_by_pub: dict[str, set[str]],
    errors: list[str],
    warnings: list[str],
) -> tuple[dict[str, Counter[str]], set[tuple[str, str]]]:
    """Validate PK, FK, status invariants, and per-publication counters for one label table."""
    seen: set[tuple[str, str]] = set()
    per_pub: dict[str, Counter[str]] = {}
    dup_gt_count = 0
    gt_seen: dict[str, dict[str, int]] = {}

    for idx, row in enumerate(rows):
        aid = row.get("arxiv_id")
        key = row.get("citation_key")
        status = row.get("label_status")
        gt = row.get("groundtruth_arxiv_id")

        if not aid or not key:
            errors.append(f"{source_name}[{idx}]: null or empty (arxiv_id, citation_key)")
            continue
        pair = (aid, key)
        if pair in seen:
            errors.append(f"{source_name}: duplicate primary key {pair}")
            continue
        seen.add(pair)

        if status not in VALID_LABEL_STATUSES:
            errors.append(f"{source_name} {pair}: invalid label_status={status!r}")
            continue
        if status == "matched" and not gt:
            errors.append(f"{source_name} {pair}: matched entry missing groundtruth_arxiv_id")
        if status == "no_match" and gt is not None:
            errors.append(f"{source_name} {pair}: no_match entry must have null groundtruth_arxiv_id")

        if key not in bib_by_pub.get(aid, set()):
            errors.append(f"{source_name} {pair}: missing from bibtex_clean.parquet")
        if status == "matched" and gt and gt not in ref_by_pub.get(aid, set()):
            errors.append(f"{source_name} {pair}: groundtruth_arxiv_id={gt} missing from references_clean.parquet")

        counter = per_pub.setdefault(aid, Counter())
        counter["total"] += 1
        counter[status] += 1
        if status == "matched" and gt:
            pub_gt = gt_seen.setdefault(aid, {})
            pub_gt[gt] = pub_gt.get(gt, 0) + 1

    for aid, gt_counts in gt_seen.items():
        for gt_id, count in gt_counts.items():
            if count > 1:
                dup_gt_count += 1
    if dup_gt_count:
        warnings.append(
            f"{source_name}: {dup_gt_count} (arxiv_id, groundtruth_arxiv_id) targets referenced by >1 citation_key "
            "(likely unmerged M2 citation aliases)"
        )
    return per_pub, seen


def validate_labels(
    cleaning_dir: Path,
    output_dir: Path,
    *,
    num_members: int = 4,
    min_entries_per_manual_pub: int = 20,
) -> dict[str, Any]:
    """Run structural, referential, quota, and quarantine checks on manual and automatic labels."""
    errors: list[str] = []
    warnings: list[str] = []

    manual_path = output_dir / "manual_labels.parquet"
    auto_path = output_dir / "auto_labels.parquet"
    manual_table = pq.read_table(manual_path)
    auto_table = pq.read_table(auto_path)

    if not manual_table.schema.equals(MANUAL_LABELS_SCHEMA, check_metadata=False):
        errors.append("manual_labels.parquet schema mismatch")
    if not auto_table.schema.equals(AUTO_LABELS_SCHEMA, check_metadata=False):
        errors.append("auto_labels.parquet schema mismatch")

    bib_table = pq.read_table(
        cleaning_dir / "bibtex_clean.parquet", columns=["arxiv_id", "citation_key"]
    )
    ref_table = pq.read_table(
        cleaning_dir / "references_clean.parquet", columns=["arxiv_id", "ref_arxiv_id"]
    )
    bib_by_pub: dict[str, set[str]] = {}
    for aid, key in zip(bib_table["arxiv_id"].to_pylist(), bib_table["citation_key"].to_pylist()):
        bib_by_pub.setdefault(aid, set()).add(key)
    ref_by_pub: dict[str, set[str]] = {}
    for aid, ref_id in zip(ref_table["arxiv_id"].to_pylist(), ref_table["ref_arxiv_id"].to_pylist()):
        ref_by_pub.setdefault(aid, set()).add(ref_id)

    manual_rows = manual_table.to_pylist()
    auto_rows = auto_table.to_pylist()
    manual_per_pub, manual_keys = _validate_label_rows(
        manual_rows, "manual", bib_by_pub, ref_by_pub, errors, warnings
    )
    auto_per_pub, auto_keys = _validate_label_rows(
        auto_rows, "auto", bib_by_pub, ref_by_pub, errors, warnings
    )

    manual_pubs = set(manual_per_pub)
    auto_pubs = set(auto_per_pub)
    overlap_pubs = sorted(manual_pubs & auto_pubs)
    if overlap_pubs:
        errors.append(f"manual and auto pools overlap on {len(overlap_pubs)} publications: {overlap_pubs[:5]}")
    overlap_keys = manual_keys & auto_keys
    if overlap_keys:
        errors.append(f"manual and auto pools share {len(overlap_keys)} duplicate entry keys")

    # Verify 100% BibTeX entry coverage for every publication in automatic pool.
    incomplete_auto_pubs = []
    for aid, counter in sorted(auto_per_pub.items()):
        expected = len(bib_by_pub.get(aid, set()))
        if counter["total"] != expected:
            incomplete_auto_pubs.append({"arxiv_id": aid, "labeled": counter["total"], "expected": expected})
    if incomplete_auto_pubs:
        errors.append(
            f"auto pool has {len(incomplete_auto_pubs)} publications without 100% BibTeX entry coverage: "
            f"{incomplete_auto_pubs[:5]}"
        )

    # Check Milestone 3 quotas for group size k = num_members.
    min_manual_pubs = 5 * num_members
    min_manual_entries = 100 * num_members
    min_auto_pubs_floor = 2 * num_members

    coverage = load_publication_coverage(cleaning_dir)
    remaining_candidates = max(
        0,
        sum(1 for info in coverage.values() if info["labeling_candidate"]) - len(manual_pubs),
    )
    remaining_assigned = max(0, len(coverage) - len(manual_pubs))
    min_auto_10pct_candidates = math.ceil(0.10 * remaining_candidates)
    min_auto_10pct_assigned = math.ceil(0.10 * remaining_assigned)

    if len(manual_pubs) < min_manual_pubs:
        errors.append(f"manual publications {len(manual_pubs)} < required {min_manual_pubs}")
    if len(manual_rows) < min_manual_entries:
        errors.append(f"manual entries {len(manual_rows)} < required {min_manual_entries}")

    manual_below_floor = {
        aid: counter["total"]
        for aid, counter in sorted(manual_per_pub.items())
        if counter["total"] < min_entries_per_manual_pub
    }
    if manual_below_floor:
        warnings.append(
            f"{len(manual_below_floor)} manual publications have < {min_entries_per_manual_pub} entries "
            f"after bibtex_clean FK filtering: {manual_below_floor}"
        )

    if len(auto_pubs) < max(min_auto_pubs_floor, min_auto_10pct_candidates):
        errors.append(
            f"auto publications {len(auto_pubs)} < required max({min_auto_pubs_floor}, "
            f"10% of remaining candidates = {min_auto_10pct_candidates})"
        )
    if len(auto_pubs) < min_auto_10pct_assigned:
        warnings.append(
            f"auto publications {len(auto_pubs)} < 10% of all {remaining_assigned} remaining assigned pubs "
            f"({min_auto_10pct_assigned})"
        )

    quarantined = load_quarantine_ref_conflicts(cleaning_dir)
    manual_quarantined = sorted(manual_pubs & quarantined)
    auto_quarantined = sorted(auto_pubs & quarantined)
    if manual_quarantined:
        warnings.append(f"manual publications with quarantine reference_field_conflict: {manual_quarantined}")
    if auto_quarantined:
        warnings.append(f"auto publications with quarantine reference_field_conflict: {len(auto_quarantined)}")

    report = {
        "valid": len(errors) == 0,
        "errors": errors,
        "warnings": warnings,
        "group_size_k": num_members,
        "manual": {
            "n_publications": len(manual_pubs),
            "n_entries": len(manual_rows),
            "n_matched": sum(c["matched"] for c in manual_per_pub.values()),
            "n_no_match": sum(c["no_match"] for c in manual_per_pub.values()),
            "min_entries_in_pub": min((c["total"] for c in manual_per_pub.values()), default=0),
            "pubs_meeting_floor_20": sum(
                1 for c in manual_per_pub.values() if c["total"] >= min_entries_per_manual_pub
            ),
            "pubs_below_floor_20": manual_below_floor,
            "required_publications": min_manual_pubs,
            "required_entries": min_manual_entries,
        },
        "auto": {
            "n_publications": len(auto_pubs),
            "n_entries": len(auto_rows),
            "n_matched": sum(c["matched"] for c in auto_per_pub.values()),
            "n_no_match": sum(c["no_match"] for c in auto_per_pub.values()),
            "all_pubs_100pct_entry_coverage": len(incomplete_auto_pubs) == 0,
            "required_min_publications_floor": min_auto_pubs_floor,
            "required_10pct_remaining_candidates": min_auto_10pct_candidates,
            "required_10pct_remaining_assigned": min_auto_10pct_assigned,
            "pct_of_remaining_candidates": round(
                100.0 * len(auto_pubs) / remaining_candidates, 2
            ) if remaining_candidates else 0.0,
            "pct_of_remaining_assigned": round(
                100.0 * len(auto_pubs) / remaining_assigned, 2
            ) if remaining_assigned else 0.0,
        },
        "overlap_publications": overlap_pubs,
        "quarantine_overlap": {
            "manual": manual_quarantined,
            "auto_count": len(auto_quarantined),
        },
    }
    write_json(output_dir / "label_validation_report.json", report)
    return report


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Validate M3 manual and automatic labels.")
    parser.add_argument("--cleaning-dir", type=Path, default=DEFAULT_CLEANING_DIR)
    parser.add_argument("--output-dir", type=Path, default=DEFAULT_OUTPUT_DIR)
    parser.add_argument("--num-members", type=int, default=4)
    args = parser.parse_args(argv)

    report = validate_labels(args.cleaning_dir, args.output_dir, num_members=args.num_members)
    print(
        f"[VALIDATE] valid={report['valid']} errors={len(report['errors'])} "
        f"warnings={len(report['warnings'])} | "
        f"manual={report['manual']['n_publications']} pubs ({report['manual']['n_entries']} entries) | "
        f"auto={report['auto']['n_publications']} pubs ({report['auto']['n_entries']:,} entries, "
        f"{report['auto']['pct_of_remaining_assigned']}% of remaining assigned)."
    )
    for err in report["errors"]:
        print(f"  ERROR: {err}")
    for warn in report["warnings"]:
        print(f"  WARN: {warn}")
    return 0 if report["valid"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
