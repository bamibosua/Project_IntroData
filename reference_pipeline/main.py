"""Extract, parse, normalize and deduplicate references from TeX, BBL, and BIB files, saving one .bib per paper."""
from __future__ import annotations

import argparse
import re
from pathlib import Path

from reference_extractor import extract_bibitems, parse_bib_file
from reference_parser import parse_reference
from reference_normalizer import normalize_reference
from reference_deduplicator import (
    deduplicate_references,
    build_cite_map,
    save_global_cite_map,
)
from reference_exporter import export_bibtex


def publication_id(arxiv_id: str) -> str:
    """Tên thư mục 2511-11742 -> id 2511.11742 (dùng làm key ngoài cùng của cite_map.json)."""
    return re.sub(r"^(\d{4})-(\d{4,5})(.*)$", r"\1.\2\3", arxiv_id)


def extract_from_bbl_dir(bbl_paper_dir: Path, version_name: str) -> list[dict]:
    """Fallback tìm file .bib hoặc .bbl trong thư mục bbl/ khi merged.tex không có reference."""
    if not bbl_paper_dir.exists():
        return []

    # Tìm version dir tương ứng hoặc trong root của paper
    v_dir = bbl_paper_dir / version_name
    if not v_dir.exists():
        v_dir = bbl_paper_dir

    # 1. Ưu tiên file .bib có sẵn
    bib_files = sorted(v_dir.glob("*.bib"))
    if bib_files:
        refs = []
        for b in bib_files:
            refs.extend(parse_bib_file(b))
        return refs

    # 2. Convert từ file .bbl
    bbl_files = sorted(v_dir.glob("*.bbl"))
    if bbl_files:
        refs = []
        for bbl in bbl_files:
            raw_items = extract_bibitems(bbl)
            refs.extend([parse_reference(r) for r in raw_items])
        return refs

    return []


def process_dataset(root_dir: Path, output_dir: Path, bbl_dir: Path | None = None) -> None:
    root_dir = root_dir.resolve()
    output_dir.mkdir(parents=True, exist_ok=True)
    if bbl_dir:
        bbl_dir = bbl_dir.resolve()

    paper_dirs = sorted(p for p in root_dir.iterdir() if p.is_dir())

    all_cite_maps = {}
    all_cite_conflicts = {}

    for paper_dir in paper_dirs:
        arxiv_id = paper_dir.name
        version_references = {}

        for version_dir in sorted(p for p in paper_dir.iterdir() if p.is_dir()):
            tex_path = version_dir / "merged.tex"
            raw_refs = []

            if tex_path.exists():
                raw_refs = extract_bibitems(tex_path)

            # Fallback sang bbl_dir nếu không trích xuất được từ merged.tex
            if not raw_refs and bbl_dir:
                bbl_paper_dir = bbl_dir / arxiv_id
                raw_refs = extract_from_bbl_dir(bbl_paper_dir, version_dir.name)

            parsed_refs = [parse_reference(ref) for ref in raw_refs]
            normalized_refs = [normalize_reference(ref) for ref in parsed_refs]
            version_references[version_dir.name] = normalized_refs

            print(
                f"{arxiv_id}/{version_dir.name}: "
                f"{len(raw_refs)} references extracted"
            )

        # Thư mục output của paper (references.bib và cite_map.json cùng nằm ở đây)
        paper_output = output_dir / arxiv_id
        paper_output.mkdir(parents=True, exist_ok=True)

        # Deduplication giữa các version; ghi cite_map.json nếu có key bị thay
        unique_refs, key_mapping = deduplicate_references(
            version_references,
            pub_id=publication_id(arxiv_id),
            cite_map_dir=paper_output,
        )

        # Build mapping and ambiguous keys.
        cite_map, ambiguous = build_cite_map(key_mapping)

        # Collect resolved mappings for global JSON.
        if cite_map:
            all_cite_maps[publication_id(arxiv_id)] = cite_map

        # Collect ambiguous mappings for manual review.
        if ambiguous:
            all_cite_conflicts[publication_id(arxiv_id)] = ambiguous

        # Add publication ID to every exported reference
        for ref in unique_refs:
            ref["source_arxiv_id"] = arxiv_id

        # Xuất ra file references.bib duy nhất cho paper
        export_bibtex(unique_refs, paper_output / "references.bib")

        cite_map_path = paper_output / "cite_map.json"

        print(
            f"\n[DONE] {arxiv_id}: "
            f"{sum(map(len, version_references.values()))} raw -> "
            f"{len(unique_refs)} unique references"
        )

        if cite_map:
            print(
                f"[CITE MAP SUCCESS] {publication_id(arxiv_id)}: "
                f"{len(cite_map)} citation keys replaced"
            )
            print(f"[FILE SAVED] {cite_map_path}")

        elif ambiguous:
            print(
                f"[CITE MAP WARNING] {publication_id(arxiv_id)}: "
                f"{len(ambiguous)} ambiguous keys need manual review"
            )

        else:
            print(
                f"[CITE MAP INFO] {publication_id(arxiv_id)}: "
                "No citation key replacement required."
            )
    # Save global citation key mapping.
    global_map_path = save_global_cite_map(
        all_cite_maps,
        output_dir,
    )

    print("\n" + "=" * 60)
    print("[GLOBAL CITE MAP SUCCESS]")
    print(f"Publications with resolved mappings: {len(all_cite_maps)}")
    print(f"Total replaced citation keys: "
          f"{sum(len(m) for m in all_cite_maps.values())}")
    print(f"Global mapping saved: {global_map_path}")
    print("=" * 60)

    # Save ambiguous mappings for manual review.
    conflict_path = output_dir / "cite_key_conflicts.json"
    
    print(f"[CONFLICT REPORT SAVED] {conflict_path}")
    print(f"Publications requiring manual review: "
          f"{len(all_cite_conflicts)}")


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "--root",
        default="Project_IntroData/parsed_structure/parsed_structure",
        help="Folder containing one directory per arXiv paper",
    )
    parser.add_argument(
        "--output",
        default="reference_output",
        help="Where the per-paper references.bib files will be saved",
    )
    parser.add_argument(
        "--bbl-dir",
        default="Project_IntroData/bbl",
        help="Folder containing bbl/ and .bib files for fallback",
    )
    args = parser.parse_args()

    bbl_path = Path(args.bbl_dir) if args.bbl_dir else None
    process_dataset(Path(args.root), Path(args.output), bbl_path)