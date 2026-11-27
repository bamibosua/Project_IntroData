"""Normalize reference metadata for comparison and deduplication."""
from __future__ import annotations

import re
import unicodedata
from typing import Any


def normalize_text(value: str | None) -> str:
    if not value:
        return ""
    value = unicodedata.normalize("NFKC", value)
    value = value.casefold()
    value = re.sub(r"[^\w\s]", " ", value, flags=re.UNICODE)
    return re.sub(r"\s+", " ", value).strip()


def normalize_doi(value: str | None) -> str:
    if not value:
        return ""
    value = value.strip()
    value = re.sub(r"^https?://(?:dx\.)?doi\.org/", "", value, flags=re.I)
    value = re.sub(r"^doi\s*:\s*", "", value, flags=re.I)
    return value.rstrip(" .,:;").casefold()


def normalize_arxiv_id(value: str | None) -> str:
    if not value:
        return ""
    value = value.strip()
    value = re.sub(r"^https?://arxiv\.org/(abs|pdf)/", "", value, flags=re.I)
    value = re.sub(r"^arxiv\s*:\s*", "", value, flags=re.I)
    return re.sub(r"v\d+$", "", value, flags=re.I).casefold()


def normalize_reference(reference: dict[str, Any]) -> dict[str, Any]:
    result = dict(reference)
    result["normalized_title"] = normalize_text(reference.get("title"))
    result["normalized_doi"] = normalize_doi(reference.get("doi"))
    result["normalized_arxiv_id"] = normalize_arxiv_id(
        reference.get("arxiv_id")
    )
    result["normalized_authors"] = normalize_text(
        reference.get("author_text") or " ".join(reference.get("authors") or [])
    )
    return result
