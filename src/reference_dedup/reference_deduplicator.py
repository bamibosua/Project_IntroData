"""Deduplicate references across versions of one arXiv paper."""
from __future__ import annotations

import json
from copy import deepcopy
from pathlib import Path
from typing import Any


def _same_reference(a: dict[str, Any], b: dict[str, Any]) -> bool:
    # 1. DOI: nếu cả hai đều có DOI thì phải giống nhau.
    doi_a = a.get("normalized_doi")
    doi_b = b.get("normalized_doi")

    if doi_a and doi_b:
        return doi_a == doi_b

    # 2. arXiv ID: nếu cả hai đều có ID thì phải giống nhau.
    arxiv_a = a.get("normalized_arxiv_id")
    arxiv_b = b.get("normalized_arxiv_id")

    if arxiv_a and arxiv_b:
        return arxiv_a == arxiv_b

    # 3. Exact normalized title + không xung đột year.
    title_a = a.get("normalized_title", "")
    title_b = b.get("normalized_title", "")

    if title_a and title_a == title_b:
        year_a = a.get("year")
        year_b = b.get("year")

        if year_a and year_b and year_a != year_b:
            return False

        return True

    return False


def _merge_records(
    target: dict[str, Any],
    source: dict[str, Any]
) -> None:
    """Merge metadata from duplicate references."""

    conflicts = target.setdefault("metadata_conflicts", [])

    bib_fields = (
        "title",
        "authors",
        "author_text",
        "year",
        "doi",
        "arxiv_id",
        "url",
        "journal",
        "booktitle",
        "volume",
        "number",
        "pages",
        "publisher",
        "organization",
        "month",
        "note",
        "entry_type",
    )

    for field in bib_fields:
        old = target.get(field)
        new = source.get(field)

        if old in (None, "", []):
            if new not in (None, "", []):
                target[field] = deepcopy(new)

        elif new not in (None, "", []) and old != new:
            conflict = {
                "field": field,
                "kept_value": old,
                "other_value": new,
                "source_file": source.get("source_file"),
            }

            if conflict not in conflicts:
                conflicts.append(conflict)

    # Merge source versions.
    target.setdefault("source_versions", [])

    for version in source.get("source_versions", []):
        if version not in target["source_versions"]:
            target["source_versions"].append(version)

    # Merge citation keys.
    target.setdefault("citation_keys", [])

    for key in source.get("citation_keys", []):
        if key and key not in target["citation_keys"]:
            target["citation_keys"].append(key)

    # Preserve raw variants.
    target.setdefault("raw_variants", [])

    raw = source.get("raw_content")

    if raw and raw not in target["raw_variants"]:
        target["raw_variants"].append(raw)


def build_cite_map(
    key_mapping: dict[str, dict[str, str]],
) -> tuple[dict[str, str], dict[str, list[str]]]:
    """
    Gom key_mapping của mọi version thành 1 map phẳng chỉ gồm các key bị THAY:
        {"Smolin1992GNewtonZero": "Smolin1992"}
    Key không đổi (key -> chính nó) không đưa vào.

    Trả về (cite_map, ambiguous):
      - ambiguous: key mà các version trỏ tới canonical khác nhau
        (vd v1: A -> B nhưng v2: A -> A). Các key này KHÔNG nằm trong cite_map
        vì không thể suy ra 1 đích duy nhất; cần xem lại thủ công.
    """
    targets: dict[str, set[str]] = {}
    for mapping in key_mapping.values():
        for old, new in mapping.items():
            targets.setdefault(old, set()).add(new)

    cite_map: dict[str, str] = {}
    ambiguous: dict[str, list[str]] = {}
    for old, news in targets.items():
        if len(news) > 1:
            ambiguous[old] = sorted(news)
        else:
            (new,) = news
            if new != old:
                cite_map[old] = new
    return dict(sorted(cite_map.items())), dict(sorted(ambiguous.items()))


def save_cite_map(
    cite_map: dict[str, str],
    pub_id: str,
    folder: str | Path,
    filename: str = "cite_map.json",
) -> Path | None:
    """
    Ghi <folder>/cite_map.json dạng {pub_id: {old_key: new_key}}.
    Không có key nào bị thay -> không ghi file (và xóa file cũ nếu có).
    """
    path = Path(folder) / filename
    if not cite_map:
        if path.exists():
            path.unlink()
        return None
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps({pub_id: cite_map}, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    return path

def save_global_cite_map(
    all_cite_maps: dict[str, dict[str, str]],
    output_dir: str | Path,
    filename: str = "cite_key.json",
) -> Path:
    """
    Save citation key mappings of all publications
    into one global JSON file.
    """

    output_dir = Path(output_dir)
    output_dir.mkdir(parents=True, exist_ok=True)

    path = output_dir / filename

    path.write_text(
        json.dumps(
            dict(sorted(all_cite_maps.items())),
            ensure_ascii=False,
            indent=2,
        ),
        encoding="utf-8",
    )

    return path

def deduplicate_references(
    version_references: dict[str, list[dict[str, Any]]],
    *,
    pub_id: str | None = None,
    cite_map_dir: str | Path | None = None,
) -> tuple[list[dict[str, Any]], dict[str, dict[str, str]]]:
    """
    Input: {"v1": [normalized refs], "v2": [...]}
    Output: unique references and citation-key mappings per version.
    Matching is intentionally conservative: DOI, arXiv ID, or exact title.

    Tham số thêm (tùy chọn, không đổi kiểu trả về):
      pub_id       : id publication, vd "2511.11981"
      cite_map_dir : thư mục ghi cite_map.json (thư mục chứa reference.bib)
    Cả hai được truyền thì cite_map.json được ghi sau khi dedup xong.
    """
    unique: list[dict[str, Any]] = []
    key_mapping: dict[str, dict[str, str]] = {}

    for version, references in version_references.items():
        key_mapping[version] = {}

        for ref in references:
            key = ref.get("citation_key") or ""
            candidate = None

            for existing in unique:
                if _same_reference(existing, ref):
                    candidate = existing
                    break

            if candidate is None:
                candidate = deepcopy(ref)
                candidate["canonical_key"] = key
                candidate["citation_keys"] = [key] if key else []
                candidate["source_versions"] = [version]
                candidate["raw_variants"] = (
                    [ref["raw_content"]] if ref.get("raw_content") else []
                )
                candidate["metadata_conflicts"] = []
                unique.append(candidate)
            else:
                _merge_records(candidate, {
                    **ref,
                    "source_versions": [version],
                    "citation_keys": [key] if key else [],
                })

            if key:
                key_mapping[version][key] = candidate["canonical_key"]

    if pub_id and cite_map_dir is not None:
        cite_map, ambiguous = build_cite_map(key_mapping)
        save_cite_map(cite_map, pub_id, cite_map_dir)
        if ambiguous:
            print(f"[CẢNH BÁO] {pub_id}: key trỏ tới canonical khác nhau giữa các version "
                  f"(không đưa vào cite_map.json): {ambiguous}")

    return unique, key_mapping