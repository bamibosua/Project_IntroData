"""End-to-end runner for Milestone 3 Member 2 (Data Labelling & Data Splitting)."""
from __future__ import annotations

import argparse
from pathlib import Path

if __package__:
    from .auto_labeling import evaluate_on_manual, generate_automatic_labels
    from .common import DEFAULT_CLEANING_DIR, DEFAULT_MANUAL_ROOT, DEFAULT_OUTPUT_DIR
    from .data_split import build_splits
    from .label_validation import validate_labels
    from .manual_labeling import consolidate_manual_labels
else:
    from auto_labeling import evaluate_on_manual, generate_automatic_labels
    from common import DEFAULT_CLEANING_DIR, DEFAULT_MANUAL_ROOT, DEFAULT_OUTPUT_DIR
    from data_split import build_splits
    from label_validation import validate_labels
    from manual_labeling import consolidate_manual_labels


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Run full M3 Member 2 labeling and splitting pipeline.")
    parser.add_argument("--cleaning-dir", type=Path, default=DEFAULT_CLEANING_DIR)
    parser.add_argument("--manual-root", type=Path, default=DEFAULT_MANUAL_ROOT)
    parser.add_argument("--output-dir", type=Path, default=DEFAULT_OUTPUT_DIR)
    parser.add_argument("--num-members", type=int, default=4)
    parser.add_argument("--seed", type=int, default=42)
    args = parser.parse_args(argv)

    manual_summary = consolidate_manual_labels(args.cleaning_dir, args.manual_root, args.output_dir)
    print(
        f"[1/4 MANUAL] {manual_summary['n_labeled_entries']} entries across "
        f"{manual_summary['n_publications']} pubs (excluded={manual_summary['n_excluded_missing_in_bibtex_clean']})."
    )

    manual_parquet = args.output_dir / "manual_labels.parquet"
    eval_report = evaluate_on_manual(args.cleaning_dir, manual_parquet, args.output_dir)
    print(
        f"[2/4 AUTO-EVAL] Precision={eval_report['precision']:.4f} Recall={eval_report['recall']:.4f} "
        f"F1={eval_report['f1']:.4f} ExactAcc={eval_report['exact_accuracy']:.4f} "
        f"(WrongID={eval_report['wrong_id_matches']})."
    )

    auto_summary = generate_automatic_labels(args.cleaning_dir, manual_parquet, args.output_dir)
    print(
        f"[2/4 AUTO-GEN] {auto_summary['n_auto_entries']:,} entries across "
        f"{auto_summary['n_auto_publications']:,} pubs in {auto_summary['wall_seconds']}s "
        f"({auto_summary['coverage_vs_remaining_assigned_pct']}% of remaining assigned)."
    )

    val_report = validate_labels(args.cleaning_dir, args.output_dir, num_members=args.num_members)
    print(
        f"[3/4 VALIDATE] valid={val_report['valid']} errors={len(val_report['errors'])} "
        f"warnings={len(val_report['warnings'])}."
    )
    if not val_report["valid"]:
        for err in val_report["errors"]:
            print(f"  ERROR: {err}")
        return 1

    split_summary = build_splits(
        args.cleaning_dir, args.output_dir, num_members=args.num_members, seed=args.seed
    )
    for part in ("train", "valid", "test"):
        st = split_summary["partitions"][part]
        print(
            f"[4/4 SPLIT] {part:<5}: {st['n_publications']:>4} pubs "
            f"(M={st['n_manual_publications']}, A={st['n_auto_publications']}) | "
            f"{st['n_labeled_entries']:>6,} entries (match_rate={st['match_rate']:.3f})"
        )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
