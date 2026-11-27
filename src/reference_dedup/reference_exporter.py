"""Export parsed references to a BibTeX file."""
from __future__ import annotations

import re
from pathlib import Path

VALID_TYPES = {
    "article", "book", "inproceedings", "phdthesis",
    "mastersthesis", "techreport", "manual", "misc",
}

# Thứ tự xuất field theo từng entry_type (khớp cấu trúc BIB mẫu)
FIELD_ORDER_BY_TYPE: dict[str, list[str]] = {
    "article": [
        "title", "author", "journal", "volume", "number",
        "pages", "year", "doi", "eprint", "archiveprefix",
        "url", "note", "publisher",
    ],
    "book": [
        "title", "author", "edition", "series", "volume",
        "publisher", "address", "year", "isbn", "doi", "url", "note",
    ],
    "inproceedings": [
        "title", "author", "booktitle", "series", "volume",
        "pages", "publisher", "year", "month", "doi", "organization", "note",
    ],
    "phdthesis": [
        "title", "author", "year", "school", "doi", "url", "note",
    ],
    "mastersthesis": [
        "title", "author", "year", "school", "doi", "url", "note",
    ],
    "techreport": [
        "title", "author", "institution", "number", "year", "doi", "url", "note",
    ],
    "manual": [
        "title", "author", "organization", "year", "url", "doi", "note",
    ],
    "misc": [
        "title", "author", "journal", "volume", "number",
        "pages", "year", "doi", "url", "note",
    ],
}

# Các trường không escape ký tự đặc biệt (URL/DOI có thể chứa & % _)
NO_ESCAPE_FIELDS = {"doi", "url", "eprint", "archiveprefix", "isbn"}


def _escape_special(text: str) -> str:
    """Escape & % # chưa được escape (tránh lỗi khi biên dịch LaTeX)."""
    return re.sub(r"(?<!\\)([&%#])", r"\\\1", text)


def _clean_value(value, escape: bool = True) -> str:
    """Bỏ khoảng trắng thừa, cân bằng ngoặc, escape ký tự đặc biệt.
    Không escape {} vì có thể dùng để bảo vệ chữ hoa (vd {NASA})."""
    text = re.sub(r"\s+", " ", str(value)).strip()
    depth = 0
    for ch in text:
        depth += 1 if ch == "{" else -1 if ch == "}" else 0
        if depth < 0:
            break
    if depth != 0:
        text = text.replace("{", "").replace("}", "")
    if escape:
        text = _escape_special(text)
    return text


def _format_pages(pages) -> str:
    """682 -> 682 ; 1801-1803 / 1801–1803 -> 1801--1803"""
    text = str(pages).strip()
    return re.sub(r"\s*[-–—]+\s*", "--", text)


def _make_key(ref: dict, index: int, seen: set) -> str:
    key = ref.get("citation_key") or f"ref{index}"
    key = re.sub(r"[\s,{}]", "", key)
    base, n = key, 1
    while key in seen:
        n += 1
        key = f"{base}_{n}"
    seen.add(key)
    return key


def _raw_to_note(raw: str, limit: int = 300) -> str:
    """Chuyển raw LaTeX thành văn bản thường để làm note."""
    text = re.sub(r"\\[a-zA-Z@]+\*?", " ", raw)
    text = text.replace("{", " ").replace("}", " ").replace("~", " ")
    text = re.sub(r"\s+", " ", text).strip()
    return text[:limit]


def _build_fields(ref: dict, entry_type: str) -> dict:
    fields: dict[str, str] = {}

    if ref.get("title"):
        fields["title"] = ref["title"]

    author = ref.get("author_text") or " and ".join(ref.get("authors") or [])
    if author:
        fields["author"] = author

    # journal / booktitle: ưu tiên trường riêng, sau đó mới dùng venue
    journal   = ref.get("journal")
    booktitle = ref.get("booktitle")
    venue     = ref.get("venue")

    if entry_type == "article":
        journal = journal or venue
    elif entry_type in {"inproceedings"}:
        booktitle = booktitle or venue

    if journal and entry_type in {"article", "misc"}:
        fields["journal"] = journal
    if booktitle and entry_type == "inproceedings":
        fields["booktitle"] = booktitle

    # Số học chung
    for key in ("volume", "number"):
        if ref.get(key):
            fields[key] = str(ref[key])
    if ref.get("pages"):
        fields["pages"] = _format_pages(ref["pages"])

    if ref.get("year"):
        fields["year"] = str(ref["year"])
    if ref.get("doi"):
        fields["doi"] = ref["doi"]

    # --- @book: edition, publisher, address, isbn, series ---
    if entry_type == "book":
        for key in ("edition", "series"):
            if ref.get(key):
                fields[key] = str(ref[key])
        if ref.get("publisher"):
            fields["publisher"] = ref["publisher"]
        if ref.get("address"):
            fields["address"] = ref["address"]
        if ref.get("isbn"):
            fields["isbn"] = ref["isbn"]
    else:
        if ref.get("publisher"):
            fields["publisher"] = ref["publisher"]

    # --- @inproceedings: series, organization, month ---
    if entry_type == "inproceedings":
        for key in ("series", "organization", "month"):
            if ref.get(key):
                fields[key] = str(ref[key])

    # --- @manual: organization ---
    if entry_type == "manual":
        if ref.get("organization"):
            fields["organization"] = ref["organization"]

    # arXiv
    if ref.get("arxiv_id"):
        fields["eprint"] = ref["arxiv_id"]
        fields["archiveprefix"] = "arXiv"
    if ref.get("url"):
        fields["url"] = ref["url"]

    # misc không có title: giữ lại nội dung gốc (đã bỏ lệnh LaTeX)
    if entry_type == "misc" and not fields.get("title"):
        raw = ref.get("raw_content", "")
        if raw:
            fields["note"] = _raw_to_note(raw)

    return fields


def export_bibtex(refs: list[dict], output_path) -> None:
    lines = []
    seen_keys: set[str] = set()

    for i, ref in enumerate(refs):
        entry_type = ref.get("entry_type") or "misc"
        if entry_type not in VALID_TYPES:
            entry_type = "misc"

        key = _make_key(ref, i, seen_keys)
        fields = _build_fields(ref, entry_type)

        order = FIELD_ORDER_BY_TYPE.get(entry_type, FIELD_ORDER_BY_TYPE["misc"])
        ordered = [k for k in order if k in fields]
        ordered += [k for k in fields if k not in order]   # trường dư xuất cuối

        width = max((len(k) for k in ordered), default=0)
        body = ",\n".join(
            f"  {k.ljust(width)} = "
            f"{{{_clean_value(fields[k], escape=k not in NO_ESCAPE_FIELDS)}}}"
            for k in ordered
        )
        lines.append(f"@{entry_type}{{{key},\n{body}\n}}\n")

    Path(output_path).write_text("\n".join(lines), encoding="utf-8")
