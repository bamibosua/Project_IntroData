"""Extract raw references in all LaTeX formats (bibitem, amsrefs, reference, sections, bibtex, list environments)."""
from __future__ import annotations

import re
from pathlib import Path
from typing import Any

# 1. Regex cho \bibitem, \reference{key}, \refitem{key}
EXPLICIT_KEY_RE = re.compile(
    r"\\(?:bibitem|reference|refitem)\s*(?:\[(?P<label>[^\]]*)\]\s*)?\{(?P<key>[^{}]+)\}",
    flags=re.MULTILINE,
)

# 2. Regex cho \harvarditem[abbrv]{full}{year}{key}
HARVARD_RE = re.compile(
    r"\\harvarditem\s*(?:\[[^\]]*\]\s*)?\{[^{}]*\}\s*\{[^{}]*\}\s*\{(?P<key>[^{}]+)\}",
    flags=re.MULTILINE,
)

# 3. Regex cho amsrefs: \bib{key}{type}{
AMSREFS_RE = re.compile(
    r"\\bib\s*\{(?P<key>[^{}]+)\}\s*\{(?P<type>[^{}]+)\}\s*\{",
    flags=re.MULTILINE,
)

# 4. Regex cho embedded BibTeX: @article{key, ...}
BIBTEX_ENTRY_RE = re.compile(
    r"@(?P<type>article|book|inproceedings|misc|phdthesis|mastersthesis|techreport|manual)\s*\{\s*(?P<key>[a-zA-Z0-9_:.\-]+)\s*,",
    flags=re.IGNORECASE | re.MULTILINE,
)

# 5. Regex nhận diện tiêu đề phần Reference / Bibliography (mọi biến thể)
REF_HEADING_RE = re.compile(
    r"(?:"
    r"\\(?:section|chapter|paragraph|subsection)\*?\s*\{[^}]*(?:refer|bibliograph)[^}]*\}|"
    r"\\references?\b(?!\s*\{)|"
    r"\\bibliographytitle\b|"
    r"(?:\\bigskip|\\medskip|\\smallskip|\s)*\{(?:\s*\\bf|\s*\\large|\s*\\bfseries)?\s*(?:REFERENCES?|BIBLIOGRAPHY)\s*\}|"
    r"\\(?:centerline|textbf|noindent|textsc|large|huge)\s*\{[^}]*(?:references?|bibliography)[^}]*\}"
    r")",
    flags=re.IGNORECASE,
)

# 6. Regex nhận diện môi trường danh sách (list environments)
LIST_ENV_RE = re.compile(
    r"\\begin\s*\{(enumerate|description|itemize|list|thebibliography|references|biblist)\*?\}(.*?)\\end\s*\{\1\*?\}",
    flags=re.DOTALL | re.IGNORECASE,
)

ITEM_RE = re.compile(r"\\item(?:\s*\[(?P<label>[^\]]*)\])?", flags=re.MULTILINE)


def remove_latex_comments(text: str) -> str:
    """Remove unescaped % comments while preserving escaped \\%."""
    cleaned_lines = []
    for line in text.splitlines():
        match = re.search(r"(?<!\\)(?:\\\\)*%", line)
        if match:
            line = line[:match.start()]
        cleaned_lines.append(line)
    return "\n".join(cleaned_lines)


def read_balanced_group(s: str, start_pos: int) -> tuple[str | None, int]:
    """Đọc khối ngoặc {...} cân bằng bắt đầu từ start_pos."""
    depth = 0
    start = -1
    for i in range(start_pos, len(s)):
        if s[i] == "\\":
            continue
        if s[i] == "{":
            if depth == 0:
                start = i + 1
            depth += 1
        elif s[i] == "}":
            depth -= 1
            if depth == 0:
                return s[start:i], i + 1
    return None, len(s)


def parse_amsrefs_fields(body: str) -> dict[str, Any]:
    """Trích xuất các trường từ khối nội dung amsrefs."""
    fields: dict[str, Any] = {}
    authors: list[str] = []
    pos = 0
    while pos < len(body):
        m = re.search(r"([a-zA-Z_]+)\s*=\s*\{", body[pos:])
        if not m:
            break
        key = m.group(1).lower()
        brace_start = pos + m.end() - 1
        val, next_pos = read_balanced_group(body, brace_start)
        if val is not None:
            val_clean = val.strip()
            if key == "author":
                authors.append(val_clean)
            elif key == "date":
                fields["date"] = val_clean
                m_year = re.search(r"\b(19|20)\d{2}\b", val_clean)
                if m_year:
                    fields["year"] = int(m_year.group(0))
            elif key == "place":
                fields["address"] = val_clean
            elif key == "book":
                fields["booktitle"] = val_clean
            elif key in {"eprint", "arxiv"}:
                fields["arxiv_id"] = val_clean
            else:
                fields[key] = val_clean
            pos = next_pos
        else:
            break
    if authors:
        fields["authors"] = authors
        fields["author_text"] = " and ".join(authors)
    return fields


def parse_bibtex_fields(body: str) -> dict[str, Any]:
    """Trích xuất các trường từ khối BibTeX thuần."""
    fields: dict[str, Any] = {}
    for m in re.finditer(r'([a-zA-Z_]+)\s*=\s*(?:\{([^{}]*)\}|"([^"]*)"|([0-9]+))', body):
        k = m.group(1).lower()
        v = (m.group(2) or m.group(3) or m.group(4) or "").strip()
        if k == "author":
            fields["author_text"] = v
            fields["authors"] = [a.strip() for a in re.split(r"\s+and\s+", v, flags=re.I) if a.strip()]
        elif k == "year":
            m_year = re.search(r"\b(19|20)\d{2}\b", v)
            if m_year:
                fields["year"] = int(m_year.group(0))
        else:
            fields[k] = v
    return fields


def parse_bib_file(bib_path: str | Path) -> list[dict[str, Any]]:
    """Đọc và parse toàn bộ entries từ một file .bib có sẵn."""
    text = Path(bib_path).read_text(encoding="utf-8", errors="replace")
    entries = []
    entry_start_re = re.compile(r"@([a-zA-Z]+)\s*\{\s*([^{},\s]+)\s*,", re.MULTILINE)

    for m in entry_start_re.finditer(text):
        entry_type = m.group(1).lower()
        key = m.group(2).strip()
        brace_pos = text.find("{", m.start())
        if brace_pos == -1:
            continue
        body, _ = read_balanced_group(text, brace_pos)
        if body is None:
            continue

        fields = parse_bibtex_fields(body)
        fields["citation_key"] = key
        fields["entry_type"] = entry_type
        fields["raw_content"] = f"@{entry_type}{{{key},\n{body}\n}}"
        fields["source_file"] = str(bib_path)
        entries.append(fields)

    return entries


def _generate_key_from_text(author_part: str, content: str, fallback_idx: int) -> str:
    """Tự động sinh citation_key dạng authorYEAR từ text."""
    m_year = re.search(r"\b((?:19|20)\d{2})\b", content) or re.search(r"\b((?:19|20)\d{2})\b", author_part)
    year = m_year.group(1) if m_year else ""

    first_author = ""
    # Tìm họ đầu tiên
    clean_author = re.sub(r"[{}\\]", "", author_part)
    words = clean_author.replace(",", " ").split()
    for w in words:
        w_clean = re.sub(r"[^a-zA-Z]", "", w)
        if len(w_clean) > 2 and w_clean.lower() not in {"the", "and", "vol", "pp", "eds", "collab", "gaia"}:
            first_author = w_clean.lower()
            break

    if not first_author:
        # Thử lấy từ content
        for w in re.sub(r"[{}\\]", "", content).replace(",", " ").split()[:6]:
            w_clean = re.sub(r"[^a-zA-Z]", "", w)
            if len(w_clean) > 2 and w_clean.lower() not in {"the", "and", "vol", "pp", "eds", "item"}:
                first_author = w_clean.lower()
                break

    if first_author and year:
        return f"{first_author}{year}"
    elif first_author:
        return f"{first_author}_{fallback_idx}"
    return f"ref_{fallback_idx}"


def _extract_explicit_macro(text: str, source_file: str) -> list[dict[str, Any]]:
    """Trích xuất các lệnh có citation key tường minh: \\bibitem, \\reference{key}, \\refitem, \\harvarditem."""
    matches = list(EXPLICIT_KEY_RE.finditer(text))
    harvard_matches = list(HARVARD_RE.finditer(text))

    all_matches = sorted(matches + harvard_matches, key=lambda m: m.start())
    if not all_matches:
        return []

    results: list[dict[str, Any]] = []
    for i, match in enumerate(all_matches):
        key = match.group("key").strip()
        end = all_matches[i + 1].start() if i + 1 < len(all_matches) else len(text)
        closing = text.find(r"\end{thebibliography}", match.end(), end)
        if closing != -1:
            end = closing
        raw = text[match.end():end].strip()
        results.append({
            "citation_key": key,
            "raw_content": raw,
            "source_file": source_file,
        })
    return results


def _extract_amsrefs(text: str, source_file: str) -> list[dict[str, Any]]:
    """Trích xuất \\bib{key}{type}{...} trong amsrefs."""
    results: list[dict[str, Any]] = []
    for m in AMSREFS_RE.finditer(text):
        key = m.group("key").strip()
        entry_type = m.group("type").strip().lower()
        body, _ = read_balanced_group(text, m.end() - 1)
        fields = parse_amsrefs_fields(body or "")
        fields["citation_key"] = key
        fields["entry_type"] = entry_type
        fields["raw_content"] = body or ""
        fields["source_file"] = source_file
        results.append(fields)
    return results


def _extract_embedded_bibtex(text: str, source_file: str) -> list[dict[str, Any]]:
    """Trích xuất khối BibTeX trực tiếp nếu tác giả nhúng thẳng vào TeX."""
    results: list[dict[str, Any]] = []
    for m in BIBTEX_ENTRY_RE.finditer(text):
        key = m.group("key").strip()
        entry_type = m.group("type").strip().lower()
        body, _ = read_balanced_group(text, m.end() - 1)
        fields = parse_bibtex_fields(body or "")
        fields["citation_key"] = key
        fields["entry_type"] = entry_type
        fields["raw_content"] = body or ""
        fields["source_file"] = source_file
        results.append(fields)
    return results


def _extract_list_environments(text: str, source_file: str) -> list[dict[str, Any]]:
    """
    Trích xuất từ các môi trường danh sách:
    \\begin{enumerate}, \\begin{description}, \\begin{itemize}, \\begin{list}
    khi chúng chứa references (sau heading hoặc ở cuối tài liệu).
    """
    # Tìm vùng sau Reference heading
    m_head = REF_HEADING_RE.search(text)
    search_zone = text[m_head.end():] if m_head else text

    # Tìm các môi trường list
    for m_list in LIST_ENV_RE.finditer(search_zone):
        body = m_list.group(2)
        matches = list(ITEM_RE.finditer(body))
        if not matches:
            continue

        # Kiểm tra xem danh sách này có phải là danh sách tài liệu tham khảo không:
        # Ít nhất 2 mục, có chứa các năm 19xx hoặc 20xx hoặc từ viết tắt journal
        has_years = len(re.findall(r"\b(?:19|20)\d{2}\b", body))
        if has_years < min(2, len(matches)):
            # Không phải references list (có thể là danh sách bình thường trong bài)
            if not m_head:
                continue

        results: list[dict[str, Any]] = []
        for i, match in enumerate(matches):
            label = (match.group("label") or "").strip()
            end = matches[i + 1].start() if i + 1 < len(matches) else len(body)
            raw = body[match.end():end].strip()
            if not raw and not label:
                continue

            full_content = f"{label} {raw}".strip() if label else raw

            # Sinh key
            key = _generate_key_from_text(label, raw, i + 1)

            entry_dict: dict[str, Any] = {
                "citation_key": key,
                "raw_content": full_content,
                "source_file": source_file,
            }

            # Nếu label là tác giả (dài > 3 ký tự, không phải số đơn thuần)
            if label and not re.match(r"^\d+$|^\[\d+\]$", label):
                entry_dict["author_hint"] = label

            results.append(entry_dict)

        if results:
            return results

    return []


def _extract_aas_references(text: str, source_file: str) -> list[dict[str, Any]]:
    """Trích xuất các lệnh \\reference ... không có ngoặc nhọn (kiểu AAS)."""
    env_m = re.search(r"\\begin\s*\{(?:references|thebibliography)\}(.*?)\\end\s*\{(?:references|thebibliography)\}", text, re.DOTALL | re.I)
    target_text = env_m.group(1) if env_m else ""
    if not target_text or r"\reference" not in target_text:
        return []

    parts = re.split(r"\\reference\b", target_text)
    results = []
    for idx, p in enumerate(parts[1:], 1):
        content = p.strip()
        if len(content) < 15:
            continue
        key = _generate_key_from_text("", content, idx)
        results.append({
            "citation_key": key,
            "raw_content": content,
            "source_file": source_file,
        })
    return results


def _extract_section_references(text: str, source_file: str) -> list[dict[str, Any]]:
    """Trích xuất danh sách tài liệu từ \\section*{References} hoặc các Heading tương đương khi không dùng list env."""
    m_sec = REF_HEADING_RE.search(text)
    if not m_sec:
        return []

    sec_content = text[m_sec.end():]
    m_stop = re.search(r"\\(?:section|chapter|appendix)\b|\\end\{document\}", sec_content, re.I)
    if m_stop:
        sec_content = sec_content[:m_stop.start()]

    cleaned = re.sub(r"\\[a-zA-Z]+\*?(?:\[[^\]]*\])?(?:\{[^{}]*\})?", " ", sec_content)
    cleaned = cleaned.strip()

    entries: list[str] = []

    # Cách 1: Danh sách đánh số [1], [2] hoặc 1., 2.
    numbered_splits = re.split(r"(?:^|\n)\s*(?:\[\s*\d+\s*\]|\d+\.|\(\s*\d+\s*\))\s+", cleaned)
    if len(numbered_splits) >= 3:
        entries = [e.strip() for e in numbered_splits if len(e.strip()) > 15]

    # Cách 2: Phân tách bằng \\ hoặc dòng trống đôi
    if not entries:
        raw_splits = [p.strip() for p in re.split(r"\\{2,}|\n\s*\n+", sec_content) if len(p.strip()) > 20]
        cand_entries = [p for p in raw_splits if re.search(r"\b(19|20)\d{2}\b", p)]
        if len(cand_entries) >= 2:
            entries = cand_entries

    results = []
    for idx, e in enumerate(entries, 1):
        key = _generate_key_from_text("", e, idx)
        results.append({
            "citation_key": key,
            "raw_content": e,
            "source_file": source_file,
        })
    return results


def extract_bibitems(tex_path: str | Path) -> list[dict[str, Any]]:
    """
    Trích xuất toàn bộ references từ file merged.tex hỗ trợ mọi định dạng:
    1. \\bibitem, \\reference{key}, \\refitem, \\harvarditem
    2. amsrefs: \\bib{key}{type}{...}
    3. Embedded BibTeX: @article{key, ...}
    4. List environments: \\begin{enumerate}, \\begin{description}, \\begin{itemize}
    5. AAS style: \\reference ...
    6. Fallback: Free-form text under Reference headings
    """
    tex_path = Path(tex_path)
    text = tex_path.read_text(encoding="utf-8", errors="replace")
    text = remove_latex_comments(text)

    # 1. Explicit macro
    explicit_refs = _extract_explicit_macro(text, str(tex_path))
    if explicit_refs:
        return explicit_refs

    # 2. amsrefs
    ams_refs = _extract_amsrefs(text, str(tex_path))
    if ams_refs:
        return ams_refs

    # 3. Embedded BibTeX
    bibtex_refs = _extract_embedded_bibtex(text, str(tex_path))
    if bibtex_refs:
        return bibtex_refs

    # 4. List environments (enumerate, description, itemize)
    list_refs = _extract_list_environments(text, str(tex_path))
    if list_refs:
        return list_refs

    # 5. AAS style
    aas_refs = _extract_aas_references(text, str(tex_path))
    if aas_refs:
        return aas_refs

    # 6. Free-form text under Reference headings
    sec_refs = _extract_section_references(text, str(tex_path))
    if sec_refs:
        return sec_refs

    return []
