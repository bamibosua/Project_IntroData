"""Heuristic parser for free-form LaTeX \\bibitem contents.

There is no universal grammar for the text inside \\bibitem. This module
extracts high-confidence fields when possible and always preserves raw_content.
Uncertain fields are left as None rather than fabricated.
"""
from __future__ import annotations

import re
from typing import Any

from reference_classify import classify_reference


YEAR_RE = re.compile(r"\b(?:19|20)\d{2}\b")
DOI_RE = re.compile(r"\b10\.\d{4,9}/[-._;()/:A-Z0-9]+\b", re.IGNORECASE)
ARXIV_RE = re.compile(r"\b(?:arXiv\s*:\s*)?(\d{4}\.\d{4,5})(?:v\d+)?\b", re.IGNORECASE)
URL_RE = re.compile(r"https?://[^\s<>{}\]]+", re.IGNORECASE)

# \emph{X}, \textit{X}, {\em X}, {\it X}
VENUE_RE = re.compile(
    r"\\(?:emph|textit)\s*\{([^{}]+)\}|\{\s*\\(?:em|it)\s+([^{}]+)\}"
)

_PUBLISHERS = {
    r"american institute of physics": "American Institute of Physics",
    r"aip publishing": "AIP Publishing",
    r"american physical society": "American Physical Society",
    r"iop publishing": "IOP Publishing",
    r"institute of physics": "Institute of Physics",
    r"ieee": "IEEE",
    r"acm": "ACM",
    r"springer(?:[- ]verlag)?": "Springer",
    r"elsevier": "Elsevier",
    r"wiley(?:[- ]vch)?": "Wiley",
    r"taylor\s*(?:&|and)\s*francis": "Taylor & Francis",
    r"cambridge university press": "Cambridge University Press",
    r"oxford university press": "Oxford University Press",
    r"mit press": "MIT Press",
    r"nature publishing group": "Nature Publishing Group",
    r"academic press": "Academic Press",
    r"crc press": "CRC Press",
    r"siam": "SIAM",
    r"optica publishing group": "Optica Publishing Group",
    r"optical society of america": "Optical Society of America",
}


def _extract_address(text: str) -> str | None:
    """Trích thành phố xuất bản từ bibitem kiểu plain."""
    m = re.search(
        r"\b(New York|Cambridge|London|Berlin|Heidelberg|Boston|Amsterdam|"
        r"Boca Raton|Upper Saddle River|Hoboken|Oxford|Paris|Tokyo|"
        r"San Francisco|Washington|Vienna|Singapore|Seoul)\b",
        text, re.I
    )
    return m.group(0).title() if m else None


def _extract_edition(text: str) -> str | None:
    """Trích số edition từ bibitem kiểu plain. Trả về chuỗi số (vd '7', '2')."""
    m = re.search(r"\b(\d+)(?:st|nd|rd|th)\s+ed(?:ition)?\.?", text, re.I)
    if m:
        return m.group(1)
    m = re.search(r"\bedition\s+(\d+)\b", text, re.I)
    if m:
        return m.group(1)
    # "7th edition" / "edition 7" đã bắt; thêm "ed. 7" / "7 ed."
    m = re.search(r"\bed\.\s+(\d+)\b", text, re.I)
    return m.group(1) if m else None


def _extract_publisher(text: str, entry_type: str | None = None) -> str | None:
    # 1. "published by X": nguồn ghi rõ nên luôn tin
    m = re.search(r"published by\s+([^.,;()]+)", text, re.I)
    if m:
        return m.group(1).strip()
    # 2. Đoán từ danh sách nhà xuất bản: chỉ áp dụng cho sách/báo cáo/manual
    #    để tránh nhầm "IEEE Transactions..." thành publisher của bài báo
    if entry_type in {"book", "techreport", "manual"}:
        for pattern, canonical in _PUBLISHERS.items():
            if re.search(rf"\b{pattern}\b", text, re.I):
                return canonical
    return None


# ---------------------------------------------------------------------------
# Helpers đọc nhóm {...} cân bằng ngoặc (cho REVTeX / APS)
# ---------------------------------------------------------------------------
def _read_group(s: str, i: int):
    """Đọc nhóm {...} cân bằng ngoặc bắt đầu từ vị trí i (bỏ qua khoảng trắng).
    Trả về (nội dung, vị trí sau dấu }) hoặc None."""
    n = len(s)
    while i < n and s[i].isspace():
        i += 1
    if i >= n or s[i] != "{":
        return None
    depth, j, start = 0, i, i + 1
    while j < n:
        c = s[j]
        if c == "\\":
            j += 2
            continue
        if c == "{":
            depth += 1
        elif c == "}":
            depth -= 1
            if depth == 0:
                return s[start:j], j + 1
        j += 1
    return None


def _find_cmd_args(s: str, cmd: str) -> list[str]:
    """Trả về đối số {…} đầu tiên của mọi lần xuất hiện \\cmd{…}."""
    out = []
    for m in re.finditer(rf"\\{cmd}\s*(?=\{{)", s):
        g = _read_group(s, m.end())
        if g:
            out.append(g[0])
    return out


def _parse_bibinfo(raw: str) -> dict[str, list[str]]:
    """\\bibinfo {name} {value} -> {name: [value, ...]}."""
    fields: dict[str, list[str]] = {}
    for m in re.finditer(r"\\bibinfo\s*(?=\{)", raw):
        g1 = _read_group(raw, m.end())
        if not g1:
            continue
        name, pos = g1
        g2 = _read_group(raw, pos)
        if g2:
            fields.setdefault(name.strip(), []).append(g2[0])
    return fields


def _strip_latex_markup(text: str) -> str:
    text = re.sub(r"\\newblock\b", " ", text)
    text = re.sub(r"\{\s*\\(?:em|it)\s+([^{}]*)\}", r"\1", text)
    text = re.sub(r"\\(?:emph|textit|textbf|textrm|texttt)\s*\{([^{}]*)\}", r"\1", text)
    text = re.sub(r"\\url\s*\{([^{}]*)\}", r"\1", text)
    text = re.sub(r"\\href\s*\{([^{}]*)\}\s*\{([^{}]*)\}", r"\2 (\1)", text)
    text = re.sub(r"\\[a-zA-Z]+\*?(?:\[[^\]]*\])?", " ", text)
    text = text.replace("{", "").replace("}", "")
    text = re.sub(r"~", " ", text)
    text = re.sub(r"\s+", " ", text)
    return text.strip()


def _format_revtex_author(value: str) -> str:
    """\\bibfnamefont {A.~A.}\\ \\bibnamefont {Starobinsky} -> 'Starobinsky, A. A.'"""
    first = " ".join(_strip_latex_markup(x) for x in _find_cmd_args(value, "bibfnamefont"))
    last = " ".join(_strip_latex_markup(x) for x in _find_cmd_args(value, "bibnamefont"))
    if last:
        return f"{last}, {first}".strip(", ")
    return _strip_latex_markup(value)  # collaboration / tổ chức


def _parse_revtex(raw: str) -> dict[str, Any] | None:
    if r"\bibinfo" not in raw:
        return None
    f = _parse_bibinfo(raw)

    def one(name):
        vals = f.get(name)
        return _strip_latex_markup(vals[0]).strip(" .,:;") if vals else None

    authors = [_format_revtex_author(v) for v in f.get("author", [])]
    year_raw = one("year")
    year_m = YEAR_RE.search(year_raw or "")
    return {
        "authors": [a for a in authors if a],
        "title": one("title"),
        "journal": one("journal"),
        "booktitle": one("booktitle"),
        "publisher": one("publisher"),
        "volume": one("volume"),
        "number": one("number"),
        "pages": one("pages"),
        "year": int(year_m.group(0)) if year_m else None,
        "eprint": one("eprint"),
    }


# ---------------------------------------------------------------------------
# Helpers cho bibitem kiểu plain / viết tay
# ---------------------------------------------------------------------------
def _parse_physics_astronomy_ref(raw: str) -> dict[str, Any] | None:
    """
    Nhận diện định dạng trích dẫn phong cách Astronomy / Physics / Economics:
    - Authors, Journal {\\bf Vol}, Pages (Year).
    - Authors. (Year). Title. {\\it Journal}, {\\bf Vol(Num)}, Pages.
    """
    clean_raw = re.sub(r"(?<!\\)%.*$", "", raw).strip()
    m_vol = re.search(r"\\(?:bf|textbf)\s*\{?(\d+)(?:\((\d+)\))?\}?", clean_raw)
    if not m_vol:
        return None

    vol = m_vol.group(1)
    num = m_vol.group(2)
    before_vol = clean_raw[:m_vol.start()].strip(" ,;:{")
    after_vol = clean_raw[m_vol.end():].strip(" ,;:}")

    m_year = re.search(r"\((\d{4})\)", clean_raw) or re.search(r"\b((?:19|20)\d{2})\b", clean_raw)
    year = int(m_year.group(1)) if m_year else None

    pages = None
    m_pages = re.search(r"([A-Za-z]?\d+(?:\s*(?:\\?[,;]?[-–—]+\\?[,;]?)\s*[A-Za-z]?\d+)?)\b", after_vol)
    if m_pages:
        pages = re.sub(r"\\?[,;\s]*[-–—]+\\?[,;\s]*", "--", m_pages.group(1))

    m_it = re.search(r"\\(?:it|textit)\s*\{?([^}]+)\}?", before_vol)
    journal = None
    title = None
    authors = None

    if m_it:
        journal = m_it.group(1).strip(" .,:;{}")
        pre_it = before_vol[:m_it.start()].strip(" .,:;{}")
        m_y_head = re.search(r"^\s*\((\d{4})\)\.?\s*", pre_it)
        if m_y_head:
            title = pre_it[m_y_head.end():].strip(" .,:;{}")
        else:
            dot_split = [p.strip() for p in pre_it.split(".") if len(p.strip()) > 3]
            if len(dot_split) >= 2:
                title = dot_split[-1].strip(" .,:;{}")
                authors = ".".join(dot_split[:-1]).strip(" .,:;{}")
            else:
                authors = pre_it
    else:
        parts = [p.strip(" .") for p in before_vol.split(",") if p.strip(" .")]
        if len(parts) >= 2:
            journal = parts[-1].strip(" {}\\\\")
            authors = ", ".join(parts[:-1]).strip(" {}\\\\")
        else:
            authors = before_vol

    entry_type = "article"
    booktitle = None
    if journal and re.search(r"\b(proc\.|proceedings|conference|workshop|symposium)\b", journal, re.I):
        entry_type = "inproceedings"
        booktitle = journal
        journal = None

    author_list = [a.strip(" ,.;:([") for a in re.split(r"\s+and\s+", authors, flags=re.I)] if authors else []
    author_list = [a for a in author_list if a]

    return {
        "authors": author_list,
        "author_text": authors,
        "title": title,
        "journal": journal,
        "booktitle": booktitle,
        "volume": vol,
        "number": num,
        "pages": pages,
        "year": year,
        "entry_type": entry_type,
    }


def _clean_doi(value: str | None) -> str | None:
    if not value:
        return None
    value = re.sub(r"^https?://(?:dx\.)?doi\.org/", "", value, flags=re.I)
    value = re.sub(r"^doi\s*:\s*", "", value, flags=re.I)
    return value.rstrip(".,; )]}")


def _extract_venue(raw: str) -> str | None:
    """Phần in nghiêng thường là tên journal / hội nghị / sách."""
    m = VENUE_RE.search(raw)
    if not m:
        return None
    venue = next((g for g in m.groups() if g), None)
    return _strip_latex_markup(venue).strip(" .,:;") if venue else None


def _extract_title(raw: str) -> str | None:
    # 1. Title trong dấu nháy (IEEE style)
    quoted = re.search(r'["“](.+?)["”]|``(.+?)\'\'', raw)
    if quoted:
        value = next(g for g in quoted.groups() if g)
        return _strip_latex_markup(value).strip(" .,:;") or None

    # 2. Style plain/abbrv: block0 = authors, block1 = title, block2 = venue
    blocks = [b.strip() for b in re.split(r"\\newblock\b", raw) if b.strip()]
    if len(blocks) >= 2:
        title = _strip_latex_markup(blocks[1]).strip(" .,:;")
        if title and len(title.split()) >= 2:
            return title

    # 3. Fallback: chọn đoạn "giống câu" sau phần tác giả
    text = _strip_latex_markup(raw)
    segments = [s.strip() for s in re.split(r"\.\s+", text) if s.strip()]
    candidates = []
    for segment in segments:
        if YEAR_RE.search(segment) or DOI_RE.search(segment) or URL_RE.search(segment):
            continue
        if len(segment.split()) >= 4 and not re.match(
            r"^(in|proceedings|journal|vol\.?|pp\.?)\b", segment, re.I
        ):
            candidates.append(segment.strip(" .,:;"))
    if candidates:
        return candidates[1] if len(candidates) > 1 else candidates[0]

    # 4. Cuối cùng mới dùng phần in nghiêng (có thể là venue, độ tin cậy thấp)
    return _extract_venue(raw)


def _extract_authors(text: str, title: str | None) -> tuple[list[str], str | None]:
    prefix = text
    if title:
        pos = prefix.find(title)
        if pos > 0:
            prefix = prefix[:pos]
    prefix = re.sub(r"\\newblock\b", " ", prefix)
    prefix = re.sub(r"\s+", " ", prefix).strip(" ,.;:([")
    prefix = re.sub(r"[\(\[]?\s*\b(?:19|20)\d{2}\b.*$", "", prefix).strip(" ,.;:([")

    if not prefix or len(prefix.split()) > 45:
        return [], None

    if re.search(r"\s+and\s+", prefix, flags=re.I):
        authors = [a.strip(" ,.;:([") for a in re.split(r"\s+and\s+", prefix, flags=re.I)]
        cleaned_authors = [a for a in authors if a]
        return cleaned_authors, " and ".join(cleaned_authors)

    clean_prefix = prefix.strip(" ,.;:([")
    return [clean_prefix], clean_prefix


def _extract_vol_pages(text: str) -> dict[str, str]:
    """Trích volume / number / pages từ text đã strip LaTeX."""
    out: dict[str, str] = {}
    # 12(3):45--67  hoặc  12(3), 45-67
    m = re.search(
        r"\b(\d{1,4})\s*\((\d{1,3})\)\s*[:,]\s*(\d+(?:\s*[-–—]+\s*\d+)?)", text
    )
    if m:
        out["volume"], out["number"], out["pages"] = m.group(1), m.group(2), m.group(3)
        return out
    m = re.search(r"\bvol(?:ume)?\.?\s*(\d+)", text, re.I)
    if m:
        out["volume"] = m.group(1)
    m = re.search(r"\bno\.?\s*(\d+)", text, re.I)
    if m:
        out["number"] = m.group(1)
    m = re.search(r"\bpp?\.?\s*(\d+(?:\s*[-–—]+\s*\d+)?)", text, re.I)
    if m:
        out["pages"] = m.group(1)
    return out


# ---------------------------------------------------------------------------
# Hàm chính
# ---------------------------------------------------------------------------
def parse_reference(raw_reference: dict[str, Any]) -> dict[str, Any]:
    raw = raw_reference.get("raw_content", "")
    text = _strip_latex_markup(raw)

    doi_match = DOI_RE.search(raw) or DOI_RE.search(text)   # DOI nằm trong \href{...}
    url_match = URL_RE.search(text)
    arxiv_match = ARXIV_RE.search(text)

    # Nếu raw_reference đã có sẵn metadata từ amsrefs hoặc embedded BibTeX
    pre_authors = raw_reference.get("authors")
    pre_author_text = raw_reference.get("author_text")
    pre_title = raw_reference.get("title")
    pre_year = raw_reference.get("year")
    pre_entry_type = raw_reference.get("entry_type")
    author_hint = raw_reference.get("author_hint")

    notes: list[str] = []

    # Thử parse style Astronomy / Physics / Economics nếu có {\bf vol}
    phys_info = _parse_physics_astronomy_ref(raw)

    if pre_title or pre_authors or pre_entry_type:
        title = pre_title or _extract_title(raw)
        authors = pre_authors or []
        author_text = pre_author_text or (" and ".join(authors) if authors else None)
        year_match = YEAR_RE.search(text) if not pre_year else None
        year = pre_year or (int(year_match.group(0)) if year_match else None)
        venue = raw_reference.get("journal") or raw_reference.get("booktitle") or _extract_venue(raw)
        extra = {k: raw_reference[k] for k in (
            "journal", "booktitle", "publisher", "volume", "number", "pages", "address", "edition"
        ) if raw_reference.get(k)}
        meta = {"title": title, "authors": authors, "year": year, "venue": venue}
        notes.append("parsed_from_structured_source")
    elif phys_info:
        title = phys_info.get("title")
        if author_hint:
            author_text = author_hint
            authors = [a.strip(" ,.;:([") for a in re.split(r"\s+and\s+", author_hint, flags=re.I)]
            authors = [a for a in authors if a]
        else:
            authors = phys_info.get("authors") or []
            author_text = phys_info.get("author_text")
        year = phys_info.get("year")
        venue = phys_info.get("journal") or phys_info.get("booktitle")
        pre_entry_type = phys_info.get("entry_type")
        extra = {k: phys_info[k] for k in (
            "journal", "booktitle", "volume", "number", "pages"
        ) if phys_info.get(k)}
        meta = {"title": title, "authors": authors, "year": year, "venue": venue}
        notes.append("parsed_as_physics_astronomy")
    else:
        rx = _parse_revtex(raw)          # None nếu không phải REVTeX/APS style

        if rx:
            title = rx["title"]
            authors = rx["authors"]
            author_text = " and ".join(authors) if authors else None
            year = rx["year"]
            venue = rx["journal"] or rx["booktitle"]
            meta = {
                "title": title, "authors": authors, "year": year, "venue": venue,
                "journal": rx["journal"], "booktitle": rx["booktitle"],
            }
            extra = {k: rx[k] for k in ("journal", "booktitle", "publisher",
                                        "volume", "number", "pages")}
            notes.append("parsed_as_revtex_bibinfo")
        else:
            year_match = YEAR_RE.search(text)
            title = _extract_title(raw)
            venue = _extract_venue(raw)
            if author_hint:
                author_text = author_hint
                authors = [a.strip(" ,.;:([") for a in re.split(r"\s+and\s+", author_hint, flags=re.I)]
                authors = [a for a in authors if a]
            else:
                authors, author_text = _extract_authors(text, title)
            year = int(year_match.group(0)) if year_match else None
            meta = {"title": title, "authors": authors, "year": year, "venue": venue}
            extra = _extract_vol_pages(text)
            if venue:
                extra["journal"] = venue   # exporter chỉ dùng nếu entry_type == article

    doi = raw_reference.get("doi") or (_clean_doi(doi_match.group(0)) if doi_match else None)
    url = raw_reference.get("url") or (url_match.group(0).rstrip(".,;") if url_match else None)
    arxiv_id = raw_reference.get("arxiv_id") or (arxiv_match.group(1) if arxiv_match else None)

    if not title:
        notes.append("title_not_found")
    if not authors:
        notes.append("authors_not_confidently_parsed")

    meta.update({"doi": doi, "url": url})
    if pre_entry_type and pre_entry_type.lower() in {
        "article", "book", "inproceedings", "phdthesis", "mastersthesis", "techreport", "manual", "misc"
    }:
        entry_type = pre_entry_type.lower()
        reason = "extracted from structured source (amsrefs/bibtex)"
    else:
        entry_type, reason = classify_reference(text, meta)

    # Publisher
    publisher = extra.get("publisher") or _extract_publisher(text, entry_type)
    if publisher:
        extra["publisher"] = publisher

    # Address và edition
    address = extra.get("address") or _extract_address(text)
    edition = extra.get("edition") or _extract_edition(text)
    if address:
        extra["address"] = address
    if edition:
        extra["edition"] = edition

    extra = {k: v for k, v in extra.items() if v}

    return {
        "citation_key": raw_reference.get("citation_key"),
        "entry_type": entry_type,
        "entry_type_reason": reason,
        "title": title,
        "venue": venue,
        "authors": authors,
        "author_text": author_text,
        "year": year,
        "doi": doi,
        "arxiv_id": arxiv_id,
        "url": url,
        "address": address,
        "edition": edition,
        "raw_content": raw,
        "source_file": raw_reference.get("source_file"),
        "parse_notes": notes,
        **extra,
    }