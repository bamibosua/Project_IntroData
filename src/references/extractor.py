"""Reference Extractor.

Parses Semantic Scholar paper payloads, filters references to keep only
those with an ArXiv ID, and standardizes records for the `references` table.
"""

from __future__ import annotations

import re
from typing import Any

# Pattern matching modern and old ArXiv IDs
ARXIV_ID_PATTERN = re.compile(r"^\d{4}\.\d{4,5}(?:v\d+)?$|[a-zA-Z\-]+(?:\.[a-zA-Z\-]+)?/\d{7}(?:v\d+)?$")


def normalize_arxiv_id(arxiv_id: str) -> str:
    """Normalizes an ArXiv ID by stripping prefixes and whitespace."""
    clean = arxiv_id.strip()
    if clean.upper().startswith("ARXIV:"):
        clean = clean[6:].strip()
    return clean


def get_arxiv_id_from_external_ids(external_ids: dict[str, Any] | None) -> str | None:
    """Extracts ArXiv ID from externalIds dictionary if present.

    Semantic Scholar typically stores this under key 'ArXiv', but we also
    check case-insensitively for robustness.
    """
    if not isinstance(external_ids, dict):
        return None

    # Check exact key first
    if "ArXiv" in external_ids and external_ids["ArXiv"]:
        return normalize_arxiv_id(str(external_ids["ArXiv"]))

    # Case-insensitive fallback
    for key, val in external_ids.items():
        if key.lower() == "arxiv" and val:
            return normalize_arxiv_id(str(val))

    return None


def extract_arxiv_references(
    source_arxiv_id: str,
    paper_data: dict[str, Any] | None,
) -> list[dict[str, Any]]:
    """Extracts all references that have an ArXiv ID.

    Args:
        source_arxiv_id: The ArXiv ID of the citing paper.
        paper_data: The JSON payload returned by Semantic Scholar Graph API.

    Returns:
        List of reference dictionaries, each representing an edge in the
        `references` table:
        [
            {
                "source_arxiv_id": "2106.09685",
                "cited_arxiv_id": "2106.10199",
                "cited_title": "BitFit: ...",
                "cited_s2_id": "339b2b71...",
            },
            ...
        ]
    """
    if not paper_data or not isinstance(paper_data, dict):
        return []

    raw_refs = paper_data.get("references")
    if not isinstance(raw_refs, list):
        return []

    clean_source = normalize_arxiv_id(source_arxiv_id)
    arxiv_references: list[dict[str, Any]] = []
    seen_cited_ids: set[str] = set()

    for ref in raw_refs:
        if not isinstance(ref, dict):
            continue

        ext_ids = ref.get("externalIds")
        cited_arxiv_id = get_arxiv_id_from_external_ids(ext_ids)

        if cited_arxiv_id:
            # Avoid duplicate citations to the same paper within the same citing paper
            if cited_arxiv_id in seen_cited_ids:
                continue
            seen_cited_ids.add(cited_arxiv_id)

            arxiv_references.append({
                "source_arxiv_id": clean_source,
                "cited_arxiv_id": cited_arxiv_id,
                "cited_title": ref.get("title") or "",
                "cited_s2_id": ref.get("paperId") or "",
            })

    return arxiv_references
