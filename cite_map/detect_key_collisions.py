#!/usr/bin/env python3
"""
Phát hiện các cite key "dễ bị gộp nhầm" khi dedup theo key.

Ví dụ: Smolin1992 và Smolin1992GNewtonZero là 2 tài liệu khác nhau, nhưng
bước dedup (so khớp prefix / author+year) gộp thành 1 key Smolin1992.

Script:
  1. Đọc danh sách ref (.bib hoặc .json), tùy chọn đọc thêm main.tex để đếm \\cite.
  2. Gom các key có nguy cơ va chạm (prefix, cùng author+year, trùng sau chuẩn hóa).
  3. So sánh metadata (DOI/eprint/title/volume/pages) để phán định từng cặp:
        same_work | different_work | uncertain
  4. Xuất JSON dùng cho bước fulltext dedup.

Chỉ dùng thư viện chuẩn, không cần cài gói.

Dùng:
  python detect_key_collisions.py refs.bib -o key_collisions.json
  python detect_key_collisions.py refs.bib --tex main.tex -o key_collisions.json
  python detect_key_collisions.py refs.json -o key_collisions.json
    (refs.json: list các dict {"key","title","author","year","journal","doi",...}
     hoặc dict {key: {...fields}})
"""
import argparse
import difflib
import itertools
import json
import re
import sys
from pathlib import Path

# ----------------------------------------------------------------------------
# Đọc dữ liệu
# ----------------------------------------------------------------------------

FIELD_NAME_RE = re.compile(r"\s*,?\s*([A-Za-z][\w\-]*)\s*=\s*")


def _read_braced(text, i):
    """text[i] == '{' -> trả (nội dung bên trong, vị trí sau '}')."""
    depth, j = 0, i
    while j < len(text):
        c = text[j]
        if c == "\\":
            j += 2
            continue
        if c == "{":
            depth += 1
        elif c == "}":
            depth -= 1
            if depth == 0:
                return text[i + 1:j], j + 1
        j += 1
    return text[i + 1:], len(text)


def _parse_fields(body):
    fields, pos = {}, 0
    while True:
        m = FIELD_NAME_RE.match(body, pos)
        if not m:
            break
        name = m.group(1).lower()
        pos = m.end()
        if pos >= len(body):
            break
        if body[pos] == "{":
            value, pos = _read_braced(body, pos)
        elif body[pos] == '"':
            j = pos + 1
            while j < len(body) and not (body[j] == '"' and body[j - 1] != "\\"):
                j += 1
            value, pos = body[pos + 1:j], j + 1
        else:
            j = pos
            while j < len(body) and body[j] not in ",\n":
                j += 1
            value, pos = body[pos:j].strip(), j
        fields[name] = re.sub(r"\s+", " ", value).strip()
    return fields


def parse_bib(text):
    entries = {}
    at_re = re.compile(r"@(\w+)\s*\{")
    i = 0
    while True:
        m = at_re.search(text, i)
        if not m:
            break
        etype = m.group(1).lower()
        inner, i = _read_braced(text, m.end() - 1)
        if etype in ("comment", "string", "preamble"):
            continue
        key, _, rest = inner.partition(",")
        key = key.strip()
        if key:
            fields = _parse_fields("," + rest)
            fields["_type"] = etype
            entries[key] = fields
    return entries


def load_refs(path):
    p = Path(path)
    text = p.read_text(encoding="utf-8", errors="replace")
    if p.suffix.lower() == ".json":
        data = json.loads(text)
        if isinstance(data, dict):
            return {k: {kk.lower(): vv for kk, vv in v.items()} for k, v in data.items()}
        out = {}
        for item in data:
            item = {k.lower(): v for k, v in item.items()}
            out[item.pop("key")] = item
        return out
    return parse_bib(text)


def cite_counts(tex_path):
    text = Path(tex_path).read_text(encoding="utf-8", errors="replace")
    counts = {}
    for m in re.finditer(r"\\[A-Za-z]*cite[A-Za-z]*\*?(?:\[[^\]]*\])*\{([^}]*)\}", text):
        for k in m.group(1).split(","):
            k = k.strip()
            if k:
                counts[k] = counts.get(k, 0) + 1
    return counts


# ----------------------------------------------------------------------------
# Chuẩn hóa / so sánh
# ----------------------------------------------------------------------------

def norm_text(s):
    s = re.sub(r"\\[A-Za-z]+", " ", s or "")
    s = re.sub(r"[{}$\\]", "", s)
    return re.sub(r"[^a-z0-9]+", " ", s.lower()).strip()


def norm_doi(s):
    s = (s or "").lower().strip()
    s = re.sub(r"^https?://(dx\.)?doi\.org/", "", s)
    return s


def norm_eprint(s):
    return re.sub(r"v\d+$", "", (s or "").lower().strip())


def norm_key(k):
    return re.sub(r"[^a-z0-9]", "", k.lower())


def first_surname(author):
    if not author:
        return ""
    first = re.split(r"\s+and\s+", author)[0]
    if "," in first:
        return norm_text(first.split(",")[0])
    return norm_text(first.split()[-1]) if first.split() else ""


YEAR_RE = re.compile(r"(?<!\d)((?:19|20)\d{2})(?!\d)")


def key_stem(key):
    """(author_part, year, suffix) nếu key có dạng Author<year>suffix."""
    m = YEAR_RE.search(key)
    if not m:
        return None
    return key[:m.start()].lower().rstrip(":_-"), m.group(1), key[m.end():]


# ----------------------------------------------------------------------------
# Gom nhóm các key có nguy cơ va chạm
# ----------------------------------------------------------------------------

class UnionFind:
    def __init__(self, items):
        self.p = {i: i for i in items}

    def find(self, x):
        while self.p[x] != x:
            self.p[x] = self.p[self.p[x]]
            x = self.p[x]
        return x

    def union(self, a, b):
        self.p[self.find(a)] = self.find(b)


def collision_reasons(a, b, min_prefix=4):
    """Lý do 2 key có thể bị dedup theo key gộp nhầm."""
    reasons = []
    la, lb = a.lower(), b.lower()
    if norm_key(a) == norm_key(b):
        reasons.append("normalized_equal")
    elif min(len(la), len(lb)) >= min_prefix and (la.startswith(lb) or lb.startswith(la)):
        reasons.append("prefix")
    sa, sb = key_stem(a), key_stem(b)
    if sa and sb and sa[0] == sb[0] and sa[1] == sb[1]:
        reasons.append("same_author_year")
    return reasons


def judge_pair(ea, eb):
    sig = {}
    da, db = norm_doi(ea.get("doi")), norm_doi(eb.get("doi"))
    xa = norm_eprint(ea.get("eprint") or ea.get("arxiv"))
    xb = norm_eprint(eb.get("eprint") or eb.get("arxiv"))
    ta, tb = norm_text(ea.get("title")), norm_text(eb.get("title"))
    sim = difflib.SequenceMatcher(None, ta, tb).ratio() if ta and tb else None

    sig["title_similarity"] = None if sim is None else round(sim, 3)
    sig["same_doi"] = (da == db) if da and db else None
    sig["same_eprint"] = (xa == xb) if xa and xb else None
    sig["same_year"] = (ea.get("year") == eb.get("year")) if ea.get("year") and eb.get("year") else None
    sig["same_first_author"] = (
        first_surname(ea.get("author")) == first_surname(eb.get("author"))
        if ea.get("author") and eb.get("author") else None
    )
    va, vb = (ea.get("volume"), ea.get("pages")), (eb.get("volume"), eb.get("pages"))
    sig["same_volume_pages"] = (va == vb) if all(va) and all(vb) else None

    if sig["same_doi"] or sig["same_eprint"]:
        verdict = "same_work"
    elif sig["same_doi"] is False or sig["same_eprint"] is False:
        verdict = "different_work"
    elif sig["same_volume_pages"] is False and sig["same_year"] is not False and (
        norm_text(ea.get("journal")) == norm_text(eb.get("journal"))
        or not ea.get("journal") or not eb.get("journal")
    ) and (sim is None or sim < 0.9):
        # cùng tạp chí/năm nhưng khác volume+pages -> hai bài khác nhau
        verdict = "different_work"
    elif sim is not None and sim >= 0.9:
        verdict = "same_work"
    elif sim is not None and sim < 0.6:
        verdict = "different_work"
    else:
        verdict = "uncertain"
    return verdict, sig


def summarize(entry, key, counts):
    return {
        "title": entry.get("title"),
        "author": entry.get("author"),
        "year": entry.get("year"),
        "journal": entry.get("journal") or entry.get("booktitle"),
        "volume": entry.get("volume"),
        "pages": entry.get("pages"),
        "doi": entry.get("doi"),
        "eprint": entry.get("eprint") or entry.get("arxiv"),
        "cited_in_tex": counts.get(key) if counts is not None else None,
    }


def analyze(refs, counts=None):
    keys = sorted(refs)
    uf = UnionFind(keys)
    pair_reasons = {}
    for a, b in itertools.combinations(keys, 2):
        r = collision_reasons(a, b)
        if r:
            pair_reasons[(a, b)] = r
            uf.union(a, b)

    groups = {}
    for k in keys:
        groups.setdefault(uf.find(k), []).append(k)

    out_groups, do_not_merge, fulltext, safe_merge = [], [], [], []
    for gi, members in enumerate(sorted((sorted(g) for g in groups.values() if len(g) > 1)), 1):
        pairs = []
        for a, b in itertools.combinations(members, 2):
            if (a, b) not in pair_reasons:
                continue
            verdict, sig = judge_pair(refs[a], refs[b])
            pairs.append({"a": a, "b": b, "collision_reasons": pair_reasons[(a, b)],
                          "verdict": verdict, "signals": sig})
            if verdict == "same_work":
                safe_merge.append([a, b])
            else:
                do_not_merge.append([a, b])
                if verdict == "uncertain":
                    fulltext.append([a, b])
        # key ngắn nhất thường là key bị "nuốt" các key dài hơn khi dedup theo prefix
        absorbing = min(members, key=len)
        out_groups.append({
            "group_id": f"G{gi:03d}",
            "keys": members,
            "likely_collapsed_into": absorbing,
            "entries": {k: summarize(refs[k], k, counts) for k in members},
            "pairs": pairs,
            "needs_fulltext_dedup": any(p["verdict"] != "same_work" for p in pairs),
        })

    return {
        "schema_version": 1,
        "total_refs": len(refs),
        "collision_group_count": len(out_groups),
        "collision_groups": out_groups,
        # Các cặp tuyệt đối KHÔNG được gộp chỉ dựa trên key/prefix
        "do_not_merge_by_key": do_not_merge,
        # Các cặp metadata chưa đủ kết luận -> đưa vào so sánh fulltext
        "fulltext_dedup_candidates": fulltext,
        # Các cặp xác nhận cùng một công trình -> được phép gộp
        "safe_merge_pairs": safe_merge,
        # Key nào phải giữ nguyên, không remap về key khác
        "protected_keys": sorted({k for pair in do_not_merge for k in pair}),
    }


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("refs", help="file .bib hoặc .json chứa danh sách reference")
    ap.add_argument("--tex", help="main.tex để đếm số lần \\cite mỗi key (tùy chọn)")
    ap.add_argument("-o", "--output", default="key_collisions.json")
    args = ap.parse_args()

    refs = load_refs(args.refs)
    counts = cite_counts(args.tex) if args.tex else None
    result = analyze(refs, counts)
    if counts is not None:
        result["cited_keys_missing_in_refs"] = sorted(k for k in counts if k not in refs)

    Path(args.output).write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"refs={result['total_refs']} groups={result['collision_group_count']} "
          f"do_not_merge={len(result['do_not_merge_by_key'])} "
          f"fulltext={len(result['fulltext_dedup_candidates'])} -> {args.output}",
          file=sys.stderr)


if __name__ == "__main__":
    main()