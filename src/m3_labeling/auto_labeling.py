"""Classic deterministic automatic reference matcher (regex + exact + fuzzy) for M3 Member 2.

Uses only classic string/token heuristics (no LLMs or pretrained embeddings) so the
pipeline remains fast on CPU and strictly compliant with Milestone 3 grading rules.
"""
from __future__ import annotations

import argparse
from collections import Counter
from dataclasses import dataclass
import difflib
from pathlib import Path
import re
import time
from typing import Any

import pyarrow.parquet as pq

if __package__:
    from .common import (
        AUTO_LABELS_SCHEMA,
        DEFAULT_CLEANING_DIR,
        DEFAULT_OUTPUT_DIR,
        extract_arxiv_ids,
        extract_author_surnames,
        extract_ref_year,
        load_publication_coverage,
        load_quarantine_ref_conflicts,
        normalize_arxiv_id,
        normalize_title,
        title_tokens,
        write_json,
        write_parquet,
    )
else:
    from common import (
        AUTO_LABELS_SCHEMA,
        DEFAULT_CLEANING_DIR,
        DEFAULT_OUTPUT_DIR,
        extract_arxiv_ids,
        extract_author_surnames,
        extract_ref_year,
        load_publication_coverage,
        load_quarantine_ref_conflicts,
        normalize_arxiv_id,
        normalize_title,
        title_tokens,
        write_json,
        write_parquet,
    )

# Tuned classic thresholds (evaluated on the team manual gold standard).
FUZZY_SCORE_THRESHOLD = 0.72
FUZZY_MIN_TITLE_SIM = 0.62
FUZZY_MARGIN = 0.08
HIGH_TITLE_OVERRIDE = 0.90
TITLE_PREFIX_MIN_CHARS = 24
TITLE_PREFIX_MIN_RATIO = 0.68
AUTHOR_YEAR_MIN_CONTAINMENT = 0.80
AUTHOR_YEAR_MARGIN = 0.25
_PART_NUMERAL_RE = re.compile(r"(?:\bpart\s+)?\b(i|ii|iii|iv|v|vi|vii|viii|ix|x|[1-9])\b$")


def _trailing_part_marker(norm_title: str) -> str | None:
    """Detect trailing Roman or numeric series part markers (e.g. 'Paper I' vs 'Paper II')."""
    if not norm_title:
        return None
    match = _PART_NUMERAL_RE.search(norm_title)
    return match.group(1) if match else None


def _is_title_author_duplicate(bib_norm: str, bib_surnames: set[str], raw_authors: list[str] | None) -> bool:
    """Detect M2 parse artifact where the author string was duplicated into the title field."""
    if not bib_norm or not raw_authors:
        return False
    authors_norm = normalize_title(" ".join(a for a in raw_authors if a))
    if not authors_norm:
        return False
    if bib_norm == authors_norm or bib_norm in authors_norm or authors_norm in bib_norm:
        return True
    bib_toks = title_tokens(bib_norm, remove_stopwords=False)
    if bib_toks and bib_surnames and (len(bib_toks & bib_surnames) / len(bib_toks)) >= 0.50:
        return True
    return False


@dataclass(slots=True)
class PreparedCandidate:
    ref_arxiv_id: str
    norm_title: str
    tokens: set[str]
    surnames: set[str]
    first_surname: str | None
    year: int | None


def prepare_candidates(ref_rows: list[dict[str, Any]]) -> tuple[list[PreparedCandidate], dict[str, PreparedCandidate]]:
    """Precompute normalized fields for all candidate references of one publication."""
    prepared: list[PreparedCandidate] = []
    by_id: dict[str, PreparedCandidate] = {}
    for row in ref_rows:
        ref_id = row["ref_arxiv_id"]
        norm_title = normalize_title(row.get("ref_title"))
        tokens = title_tokens(norm_title)
        surnames, first_surname = extract_author_surnames(row.get("ref_authors"))
        year = extract_ref_year(row.get("ref_submission_date"), ref_id)
        candidate = PreparedCandidate(
            ref_arxiv_id=ref_id,
            norm_title=norm_title,
            tokens=tokens,
            surnames=surnames,
            first_surname=first_surname,
            year=year,
        )
        prepared.append(candidate)
        by_id[ref_id] = candidate
    return prepared, by_id


def _year_similarity(bib_year: int | None, ref_year: int | None) -> tuple[float, int | None]:
    """Score year compatibility; conference papers often publish 0-2 years after arXiv preprint."""
    if bib_year is None or ref_year is None:
        return 0.5, None
    diff = bib_year - ref_year
    if diff in (0, 1, 2):
        return 1.0, diff
    if diff in (-1, 3):
        return 0.75, diff
    if diff in (-2, 4):
        return 0.35, diff
    return 0.0, diff


def _author_similarity(
    bib_surnames: set[str],
    bib_first: str | None,
    cand: PreparedCandidate,
) -> tuple[float, float, bool]:
    """Return (containment_or_blended_sim, jaccard_sim, first_author_match)."""
    if not bib_surnames or not cand.surnames:
        return 0.0, 0.0, False
    overlap = len(bib_surnames & cand.surnames)
    if overlap == 0:
        return 0.0, 0.0, False
    min_len = min(len(bib_surnames), len(cand.surnames))
    union_len = len(bib_surnames | cand.surnames)
    containment = overlap / min_len
    jaccard = overlap / union_len
    first_match = bool(bib_first and cand.first_surname and bib_first == cand.first_surname)
    blended = 0.65 * containment + 0.35 * jaccard + (0.05 if first_match else 0.0)
    return min(1.0, blended), jaccard, first_match


def _title_similarity(
    bib_norm: str,
    bib_tokens: set[str],
    cand: PreparedCandidate,
    *,
    has_author_overlap: bool,
) -> float:
    """Compute classic lexical title similarity with cheap token gating before SequenceMatcher."""
    if not bib_norm or not cand.norm_title:
        return 0.0
    if bib_norm == cand.norm_title:
        return 1.0
    if not bib_tokens or not cand.tokens:
        return difflib.SequenceMatcher(None, bib_norm, cand.norm_title).ratio()

    overlap = len(bib_tokens & cand.tokens)
    union = len(bib_tokens | cand.tokens)
    min_tokens = min(len(bib_tokens), len(cand.tokens))
    jaccard = overlap / union if union else 0.0
    containment = overlap / min_tokens if min_tokens else 0.0

    # Fast reject clearly unrelated titles to keep 127k-entry matching under a few seconds.
    if jaccard < 0.22 and containment < 0.45 and not (has_author_overlap and overlap >= 1):
        return jaccard

    seq_ratio = difflib.SequenceMatcher(None, bib_norm, cand.norm_title).ratio()
    blended = 0.55 * seq_ratio + 0.45 * jaccard
    if min_tokens >= 3 and containment >= 0.80:
        # Handle subtitle omission (e.g., "Title" vs "Title: Extended Subtitle").
        blended = max(blended, 0.70 * seq_ratio + 0.30 * containment)
    return min(1.0, blended)


def _extract_entry_arxiv_ids(entry: dict[str, Any]) -> list[str]:
    """Collect candidate arXiv IDs embedded in BibTeX URL, DOI, journal, title, or citation_key."""
    ids: list[str] = []
    for field_name in ("url", "doi", "journal", "title"):
        for aid in extract_arxiv_ids(entry.get(field_name)):
            if aid not in ids:
                ids.append(aid)
    key_id = normalize_arxiv_id(entry.get("citation_key"))
    if key_id and key_id not in ids:
        ids.append(key_id)
    return ids


def match_bibtex_entry(
    entry: dict[str, Any],
    candidates: list[PreparedCandidate],
    candidates_by_id: dict[str, PreparedCandidate],
) -> tuple[str | None, str, str, float]:
    """Match one BibTeX entry against candidates of the same publication.

    Returns (groundtruth_arxiv_id, label_status, match_rule, match_score).
    """
    if not candidates:
        return None, "no_match", "no_candidates", 0.0

    # Tier 1: Deterministic arXiv ID extraction from URL / DOI / journal / citation_key.
    direct_hits = [aid for aid in _extract_entry_arxiv_ids(entry) if aid in candidates_by_id]
    if len(direct_hits) == 1:
        return direct_hits[0], "matched", "tier1_arxiv_id", 1.0

    bib_norm = normalize_title(entry.get("title"))
    bib_tokens = title_tokens(bib_norm)
    bib_surnames, bib_first = extract_author_surnames(entry.get("authors"))
    bib_year = entry.get("year")
    if isinstance(bib_year, str):
        bib_year = int(bib_year) if bib_year.isdigit() else None

    # Tier 2: Exact normalized title (or strong long prefix match with author confirmation).
    if bib_norm and (len(bib_norm) >= 12 or len(bib_tokens) >= 3):
        exact_matches: list[tuple[float, PreparedCandidate]] = []
        for cand in candidates:
            if not cand.norm_title:
                continue
            is_exact = bib_norm == cand.norm_title
            is_prefix = False
            if not is_exact:
                shorter, longer = (
                    (bib_norm, cand.norm_title)
                    if len(bib_norm) <= len(cand.norm_title)
                    else (cand.norm_title, bib_norm)
                )
                if (
                    len(shorter) >= TITLE_PREFIX_MIN_CHARS
                    and longer.startswith(shorter + " ")
                    and (len(shorter) / len(longer)) >= TITLE_PREFIX_MIN_RATIO
                ):
                    is_prefix = True
            if not (is_exact or is_prefix):
                continue
            author_sim, _, _ = _author_similarity(bib_surnames, bib_first, cand)
            year_sim, year_diff = _year_similarity(bib_year, cand.year)
            # Require author support for shorter exact titles or prefix matches.
            has_both_authors = bool(bib_surnames and cand.surnames)
            if is_prefix and has_both_authors and author_sim < 0.35:
                continue
            if is_exact and len(bib_norm) < 22 and has_both_authors and author_sim < 0.25:
                continue
            if year_diff is not None and abs(year_diff) >= 8 and author_sim < 0.50:
                continue
            rank_score = (1.0 if is_exact else 0.94) + 0.05 * author_sim + 0.01 * year_sim
            exact_matches.append((rank_score, cand))

        if exact_matches:
            exact_matches.sort(key=lambda pair: pair[0], reverse=True)
            best_score, best_cand = exact_matches[0]
            if len(exact_matches) == 1 or (best_score - exact_matches[1][0]) >= 0.03:
                return best_cand.ref_arxiv_id, "matched", "tier2_exact_title", round(min(1.0, best_score), 4)

    # Tier 3a: Fuzzy multi-signal matching when BibTeX entry has a genuine title.
    is_corrupt_author_title = _is_title_author_duplicate(bib_norm, bib_surnames, entry.get("authors"))
    if bib_norm and not is_corrupt_author_title:
        bib_part = _trailing_part_marker(bib_norm)
        scored: list[tuple[float, float, float, int | None, PreparedCandidate]] = []
        for cand in candidates:
            author_sim, author_jaccard, _ = _author_similarity(bib_surnames, bib_first, cand)
            t_sim = _title_similarity(
                bib_norm, bib_tokens, cand, has_author_overlap=(author_sim > 0.0)
            )
            year_sim, year_diff = _year_similarity(bib_year, cand.year)
            has_both_authors = bool(bib_surnames and cand.surnames)
            if has_both_authors:
                combined = 0.65 * t_sim + 0.25 * author_sim + 0.10 * year_sim
            else:
                combined = 0.85 * t_sim + 0.15 * year_sim
            # Penalize mismatched series part numbers (e.g. "Paper II" vs "Paper I").
            cand_part = _trailing_part_marker(cand.norm_title)
            if bib_part and cand_part and bib_part != cand_part:
                combined -= 0.30
                t_sim -= 0.25
            # Penalize severe author mismatch when both author lists are present and title is not near-exact.
            if has_both_authors and author_sim == 0.0 and t_sim < 0.90:
                combined -= 0.15
            if year_diff is not None and abs(year_diff) >= 6 and t_sim < 0.90:
                combined -= 0.15
            scored.append((combined, t_sim, author_sim, year_diff, cand))

        scored.sort(key=lambda item: (item[0], item[1], item[2]), reverse=True)
        top_score, top_title, top_author, top_year_diff, top_cand = scored[0]
        second_score = scored[1][0] if len(scored) > 1 else 0.0
        margin = top_score - second_score

        has_both_authors = bool(bib_surnames and top_cand.surnames)
        is_author_ok = (not has_both_authors) or (top_author >= 0.30) or (top_title >= HIGH_TITLE_OVERRIDE)
        is_year_ok = (top_year_diff is None) or (abs(top_year_diff) <= 5) or (top_title >= HIGH_TITLE_OVERRIDE and top_author >= 0.50)

        if (
            top_score >= FUZZY_SCORE_THRESHOLD
            and top_title >= FUZZY_MIN_TITLE_SIM
            and is_author_ok
            and is_year_ok
            and (margin >= FUZZY_MARGIN or top_title >= HIGH_TITLE_OVERRIDE)
        ):
            return top_cand.ref_arxiv_id, "matched", "tier3_fuzzy_multi_signal", round(top_score, 4)

        return None, "no_match", "no_match", round(max(0.0, top_score), 4)

    # Tier 3b: Strict author+year fallback when title is missing or corrupted into an author list.
    if bib_surnames and bib_year is not None:
        ay_scored: list[tuple[float, float, float, bool, int | None, PreparedCandidate]] = []
        for cand in candidates:
            if not cand.surnames:
                continue
            author_sim, author_jaccard, first_match = _author_similarity(bib_surnames, bib_first, cand)
            containment = len(bib_surnames & cand.surnames) / min(len(bib_surnames), len(cand.surnames))
            year_sim, year_diff = _year_similarity(bib_year, cand.year)
            if year_diff is None or year_diff < -1 or year_diff > 2:
                continue
            score = 0.55 * containment + 0.25 * author_jaccard + 0.15 * year_sim + (0.05 if first_match else 0.0)
            ay_scored.append((min(1.0, score), containment, author_jaccard, first_match, year_diff, cand))
        if ay_scored:
            ay_scored.sort(key=lambda item: (item[0], item[1], item[2]), reverse=True)
            best_score, best_cont, best_jacc, best_first, best_diff, best_cand = ay_scored[0]
            second_score = ay_scored[1][0] if len(ay_scored) > 1 else 0.0
            is_multi_author_ok = (
                len(bib_surnames) >= 2
                and best_cont >= AUTHOR_YEAR_MIN_CONTAINMENT
                and best_first
                and (best_score - second_score) >= AUTHOR_YEAR_MARGIN
            )
            is_single_author_ok = (
                len(bib_surnames) == 1
                and len(best_cand.surnames) == 1
                and best_first
                and best_diff in (0, 1)
                and (best_score - second_score) >= AUTHOR_YEAR_MARGIN
            )
            if is_multi_author_ok or is_single_author_ok:
                return best_cand.ref_arxiv_id, "matched", "tier3_author_year_fallback", round(best_score, 4)

    return None, "no_match", "no_match", 0.0


def _load_grouped_tables(
    cleaning_dir: Path, target_pubs: set[str] | None = None
) -> tuple[dict[str, list[dict[str, Any]]], dict[str, list[dict[str, Any]]]]:
    """Load bibtex_clean and references_clean grouped by arxiv_id."""
    bib_table = pq.read_table(cleaning_dir / "bibtex_clean.parquet")
    ref_table = pq.read_table(cleaning_dir / "references_clean.parquet")

    bib_by_pub: dict[str, list[dict[str, Any]]] = {}
    for row in bib_table.to_pylist():
        aid = row["arxiv_id"]
        if target_pubs is None or aid in target_pubs:
            bib_by_pub.setdefault(aid, []).append(row)

    ref_by_pub: dict[str, list[dict[str, Any]]] = {}
    for row in ref_table.to_pylist():
        aid = row["arxiv_id"]
        if target_pubs is None or aid in target_pubs:
            ref_by_pub.setdefault(aid, []).append(row)

    return bib_by_pub, ref_by_pub


def evaluate_on_manual(
    cleaning_dir: Path,
    manual_parquet_path: Path,
    output_dir: Path,
) -> dict[str, Any]:
    """Evaluate classic automatic matcher against consolidated manual gold standard."""
    manual_rows = pq.read_table(manual_parquet_path).to_pylist()
    manual_pubs = {row["arxiv_id"] for row in manual_rows}
    bib_by_pub, ref_by_pub = _load_grouped_tables(cleaning_dir, target_pubs=manual_pubs)

    bib_lookup: dict[tuple[str, str], dict[str, Any]] = {
        (row["arxiv_id"], row["citation_key"]): row
        for rows in bib_by_pub.values()
        for row in rows
    }
    prepared_cache = {
        aid: prepare_candidates(ref_by_pub.get(aid, []))
        for aid in manual_pubs
    }

    tp = fp = fn = tn = wrong_id = 0
    rule_counts: Counter[str] = Counter()
    error_examples: dict[str, list[dict[str, Any]]] = {
        "false_positive": [],
        "false_negative": [],
        "wrong_id": [],
    }

    for gt_row in manual_rows:
        aid = gt_row["arxiv_id"]
        key = gt_row["citation_key"]
        gt_status = gt_row["label_status"]
        gt_ref = gt_row["groundtruth_arxiv_id"]

        entry = bib_lookup[(aid, key)]
        candidates, by_id = prepared_cache[aid]
        pred_ref, pred_status, rule, score = match_bibtex_entry(entry, candidates, by_id)
        rule_counts[rule] += 1

        if gt_status == "matched" and pred_status == "matched":
            if pred_ref == gt_ref:
                tp += 1
            else:
                wrong_id += 1
                fp += 1
                fn += 1
                error_examples["wrong_id"].append({
                    "arxiv_id": aid,
                    "citation_key": key,
                    "title": entry.get("title"),
                    "gt_ref": gt_ref,
                    "pred_ref": pred_ref,
                    "rule": rule,
                    "score": score,
                })
        elif gt_status == "no_match" and pred_status == "matched":
            fp += 1
            error_examples["false_positive"].append({
                "arxiv_id": aid,
                "citation_key": key,
                "title": entry.get("title"),
                "pred_ref": pred_ref,
                "rule": rule,
                "score": score,
            })
        elif gt_status == "matched" and pred_status == "no_match":
            fn += 1
            error_examples["false_negative"].append({
                "arxiv_id": aid,
                "citation_key": key,
                "title": entry.get("title"),
                "authors": entry.get("authors"),
                "year": entry.get("year"),
                "gt_ref": gt_ref,
                "best_score": score,
            })
        else:
            tn += 1

    total = len(manual_rows)
    precision = tp / (tp + fp) if (tp + fp) else 0.0
    recall = tp / (tp + fn) if (tp + fn) else 0.0
    f1 = (2 * precision * recall / (precision + recall)) if (precision + recall) else 0.0
    exact_accuracy = (tp + tn) / total if total else 0.0

    report = {
        "n_manual_publications": len(manual_pubs),
        "n_manual_entries": total,
        "tp": tp,
        "tn": tn,
        "fp": fp,
        "fn": fn,
        "wrong_id_matches": wrong_id,
        "precision": round(precision, 4),
        "recall": round(recall, 4),
        "f1": round(f1, 4),
        "exact_accuracy": round(exact_accuracy, 4),
        "rule_counts": dict(rule_counts),
        "thresholds": {
            "fuzzy_score_threshold": FUZZY_SCORE_THRESHOLD,
            "fuzzy_min_title_sim": FUZZY_MIN_TITLE_SIM,
            "fuzzy_margin": FUZZY_MARGIN,
            "high_title_override": HIGH_TITLE_OVERRIDE,
            "author_year_min_containment": AUTHOR_YEAR_MIN_CONTAINMENT,
        },
        "error_examples": {k: v[:15] for k, v in error_examples.items()},
    }
    write_json(output_dir / "auto_eval_on_manual.json", report)
    return report


def generate_automatic_labels(
    cleaning_dir: Path,
    manual_parquet_path: Path,
    output_dir: Path,
    *,
    exclude_quarantined_ref_conflicts: bool = True,
    max_publications: int | None = None,
) -> dict[str, Any]:
    """Generate automatic labels for 100% of BibTeX entries in eligible non-manual publications."""
    started = time.perf_counter()
    manual_rows = pq.read_table(manual_parquet_path, columns=["arxiv_id"]).to_pylist()
    manual_pubs = {row["arxiv_id"] for row in manual_rows}

    coverage = load_publication_coverage(cleaning_dir)
    quarantined = load_quarantine_ref_conflicts(cleaning_dir) if exclude_quarantined_ref_conflicts else set()

    eligible_pubs = sorted(
        aid
        for aid, info in coverage.items()
        if info["labeling_candidate"]
        and aid not in manual_pubs
        and aid not in quarantined
    )
    if max_publications is not None:
        eligible_pubs = eligible_pubs[:max_publications]
    eligible_set = set(eligible_pubs)

    bib_by_pub, ref_by_pub = _load_grouped_tables(cleaning_dir, target_pubs=eligible_set)

    auto_rows: list[dict[str, Any]] = []
    rule_counts: Counter[str] = Counter()
    status_counts: Counter[str] = Counter()

    for aid in eligible_pubs:
        entries = sorted(bib_by_pub.get(aid, []), key=lambda r: r["citation_key"])
        candidates, by_id = prepare_candidates(ref_by_pub.get(aid, []))
        for entry in entries:
            pred_ref, pred_status, rule, score = match_bibtex_entry(entry, candidates, by_id)
            auto_rows.append({
                "arxiv_id": aid,
                "citation_key": entry["citation_key"],
                "groundtruth_arxiv_id": pred_ref,
                "label_status": pred_status,
                "match_rule": rule,
                "match_score": float(score),
            })
            rule_counts[rule] += 1
            status_counts[pred_status] += 1

    write_parquet(output_dir / "auto_labels.parquet", auto_rows, AUTO_LABELS_SCHEMA)
    elapsed = round(time.perf_counter() - started, 2)

    total_assigned = len(coverage)
    total_labeling_candidates = sum(1 for info in coverage.values() if info["labeling_candidate"])
    remaining_assigned = max(1, total_assigned - len(manual_pubs))
    remaining_candidates = max(1, total_labeling_candidates - len(manual_pubs))

    summary = {
        "n_auto_publications": len(eligible_pubs),
        "n_auto_entries": len(auto_rows),
        "n_matched": status_counts["matched"],
        "n_no_match": status_counts["no_match"],
        "match_rate": round(status_counts["matched"] / len(auto_rows), 4) if auto_rows else 0.0,
        "coverage_vs_remaining_assigned_pct": round(100.0 * len(eligible_pubs) / remaining_assigned, 2),
        "coverage_vs_remaining_labeling_candidates_pct": round(
            100.0 * len(eligible_pubs) / remaining_candidates, 2
        ),
        "excluded_quarantine_ref_conflict_pubs": len(
            {aid for aid in quarantined if aid in coverage and coverage[aid]["labeling_candidate"] and aid not in manual_pubs}
        ),
        "rule_counts": dict(rule_counts),
        "wall_seconds": elapsed,
    }
    write_json(output_dir / "auto_summary.json", summary)
    return summary


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Classic automatic reference matcher for M3.")
    parser.add_argument("--cleaning-dir", type=Path, default=DEFAULT_CLEANING_DIR)
    parser.add_argument("--output-dir", type=Path, default=DEFAULT_OUTPUT_DIR)
    parser.add_argument("--evaluate-only", action="store_true")
    parser.add_argument("--include-quarantined", action="store_true")
    parser.add_argument("--max-publications", type=int, default=None)
    args = parser.parse_args(argv)

    manual_parquet = args.output_dir / "manual_labels.parquet"
    eval_report = evaluate_on_manual(args.cleaning_dir, manual_parquet, args.output_dir)
    print(
        f"[AUTO-EVAL] Manual gold ({eval_report['n_manual_entries']} entries / "
        f"{eval_report['n_manual_publications']} pubs): "
        f"Precision={eval_report['precision']:.4f} Recall={eval_report['recall']:.4f} "
        f"F1={eval_report['f1']:.4f} ExactAcc={eval_report['exact_accuracy']:.4f} "
        f"(TP={eval_report['tp']} TN={eval_report['tn']} FP={eval_report['fp']} "
        f"FN={eval_report['fn']} WrongID={eval_report['wrong_id_matches']})."
    )
    if args.evaluate_only:
        return 0

    gen_summary = generate_automatic_labels(
        args.cleaning_dir,
        manual_parquet,
        args.output_dir,
        exclude_quarantined_ref_conflicts=not args.include_quarantined,
        max_publications=args.max_publications,
    )
    print(
        f"[AUTO-GEN] Labeled {gen_summary['n_auto_entries']:,} entries across "
        f"{gen_summary['n_auto_publications']:,} publications in {gen_summary['wall_seconds']}s "
        f"(matched={gen_summary['n_matched']:,}, no_match={gen_summary['n_no_match']:,}, "
        f"assigned_remaining={gen_summary['coverage_vs_remaining_assigned_pct']}%)."
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
