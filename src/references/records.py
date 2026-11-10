"""
Đọc và validate papers_index.json; normalize + validate response S2; dựng references.json record.

Schema papers_index.json:
  {
    "2608.09608": ["v1", "v2"],
    "2608.09609": ["v1"]
  }
Key là arXiv base ID; value là list version string dạng v<N> (N >= 1, không có số 0 đầu).
"""
from __future__ import annotations

import json
import re
import os

# ---------------------------------------------------------------------------
# Regex
# ---------------------------------------------------------------------------
# Base arXiv ID: YYMM.NNNNN hoặc dạng cũ hep-th/YYMMNNN v.v.
_BASE_ID_RE = re.compile(
    r"(?:[a-z][a-z0-9.\-]*/[0-9]{7}|[0-9]{4}\.[0-9]{4,5})",
    re.IGNORECASE,
)
# Version string hợp lệ: v1, v2, v10, ... (không có số 0 đầu, không v0)
_VERSION_RE = re.compile(r"^v([1-9]\d*)$")


# ---------------------------------------------------------------------------
# Lỗi input / schema
# ---------------------------------------------------------------------------
class InputError(ValueError):
    """Input file thiếu, JSON sai hoặc schema không hợp lệ. Caller nên exit 2."""


class ResponseError(ValueError):
    """Response HTTP từ S2 sai schema / kiểu dữ liệu. Caller nên retry hoặc ghi lỗi."""


# ---------------------------------------------------------------------------
# Đọc và validate index
# ---------------------------------------------------------------------------
def load_index(path: str) -> list[dict]:
    """Đọc papers_index.json.

    Trả về list[dict] dạng:
        {"arxiv_id": "2608.09608", "versions": ["2608.09608v1", "2608.09608v2"]}
    Giữ thứ tự key; version trùng bị loại và sắp theo số; version list rỗng được giữ.

    Raise InputError với thông báo rõ nếu:
      - File không đọc được / không tồn tại
      - Không phải JSON hợp lệ
      - Root không phải object
      - Key không phải arXiv base ID hợp lệ
      - Value không phải list thuần string dạng v<N>
      - Có key trùng (json phát hiện; Python dict đã dedup nhưng kiểm tra thêm)
    """
    if not os.path.isfile(path):
        raise InputError(f"File không tồn tại: {path}")
    try:
        with open(path, encoding="utf-8") as f:
            text = f.read()
    except OSError as e:
        raise InputError(f"Không đọc được file {path}: {e}") from e

    # Phát hiện key trùng sớm (json.loads tự dedup nên cần hook)
    seen_keys: set[str] = set()
    duplicate_keys: list[str] = []

    def object_pairs_hook(pairs):
        d = {}
        for k, v in pairs:
            if k in seen_keys:
                duplicate_keys.append(k)
            seen_keys.add(k)
            d[k] = v
        return d

    try:
        data = json.loads(text, object_pairs_hook=object_pairs_hook)
    except json.JSONDecodeError as e:
        raise InputError(f"JSON không hợp lệ trong {path}: {e}") from e

    if not isinstance(data, dict):
        raise InputError(
            f"{path}: root phải là object (dict), nhận được {type(data).__name__}"
        )
    if duplicate_keys:
        raise InputError(
            f"{path}: key trùng lặp: {duplicate_keys[:5]}"
            + (" ..." if len(duplicate_keys) > 5 else "")
        )
    if not data:
        # {} hợp lệ → không có bài, không gửi HTTP
        return []

    result = []
    for raw_key, raw_versions in data.items():
        # Validate key
        if not isinstance(raw_key, str) or not _BASE_ID_RE.fullmatch(raw_key):
            raise InputError(
                f"{path}: key không phải arXiv base ID hợp lệ: {raw_key!r}"
            )
        # Validate versions
        if not isinstance(raw_versions, list):
            raise InputError(
                f"{path}: value của key {raw_key!r} phải là list, nhận được "
                f"{type(raw_versions).__name__}"
            )
        seen_v: set[int] = set()
        sorted_versions: list[str] = []
        for v in raw_versions:
            if not isinstance(v, str):
                raise InputError(
                    f"{path}: version của {raw_key!r} phải là string, nhận được "
                    f"{type(v).__name__} ({v!r})"
                )
            m = _VERSION_RE.fullmatch(v)
            if not m:
                raise InputError(
                    f"{path}: version {v!r} của {raw_key!r} không hợp lệ "
                    f"(phải dạng v1, v2, ..., không v0 hay số 0 đầu)"
                )
            n = int(m.group(1))
            if n not in seen_v:
                seen_v.add(n)
                sorted_versions.append(v)
        # Sắp lại theo số
        sorted_versions.sort(key=lambda s: int(_VERSION_RE.match(s).group(1)))
        result.append({
            "arxiv_id": raw_key,
            "versions": [f"{raw_key}{v}" for v in sorted_versions],
        })
    return result


def folder_name(arxiv_id: str) -> str:
    """Giữ publication folder của pipeline cũ; ID không được thoát output root."""
    if not isinstance(arxiv_id, str) or not _BASE_ID_RE.fullmatch(arxiv_id):
        raise InputError(f"arXiv base ID không hợp lệ: {arxiv_id!r}")
    return arxiv_id.replace("/", "_").replace(".", "-")


def validate_reference(ref, context: str) -> None:
    """Kiểm tra các trường normalize thực sự dùng, giữ optional null/thiếu ID."""
    if ref is None:
        return
    if not isinstance(ref, dict):
        raise ResponseError(f"{context}: reference phải là object hoặc null")
    for name in ("paperId", "title", "publicationDate", "venue"):
        if ref.get(name) is not None and not isinstance(ref[name], str):
            raise ResponseError(f"{context}.{name}: phải là string hoặc null")
    year = ref.get("year")
    if year is not None and (isinstance(year, bool) or not isinstance(year, int)):
        raise ResponseError(f"{context}.year: phải là integer hoặc null")
    ext = ref.get("externalIds")
    if ext is not None:
        if not isinstance(ext, dict):
            raise ResponseError(f"{context}.externalIds: phải là object hoặc null")
        for name in ("ArXiv", "DOI"):
            if ext.get(name) is not None and not isinstance(ext[name], str):
                raise ResponseError(f"{context}.externalIds.{name}: phải là string hoặc null")
    authors = ref.get("authors")
    if authors is not None:
        if not isinstance(authors, list):
            raise ResponseError(f"{context}.authors: phải là list hoặc null")
        for i, author in enumerate(authors):
            if author is None:
                continue
            if not isinstance(author, dict):
                raise ResponseError(f"{context}.authors[{i}]: phải là object hoặc null")
            if author.get("name") is not None and not isinstance(author["name"], str):
                raise ResponseError(f"{context}.authors[{i}].name: phải là string hoặc null")


# ---------------------------------------------------------------------------
# Validate response batch
# ---------------------------------------------------------------------------
def validate_batch_entry(entry, arxiv_id: str):
    """Kiểm tra một entry trong phản hồi batch trước khi normalize.

    entry: giá trị từ list phản hồi (có thể là None theo hợp đồng S2 lookup).
    Raise ResponseError nếu entry không phải None / dict, hoặc thiếu / sai kiểu trường bắt buộc.
    """
    if entry is None:
        # Hợp lệ: S2 không tìm thấy bài
        return
    if not isinstance(entry, dict):
        raise ResponseError(
            f"{arxiv_id}: entry batch phải là object hoặc null, "
            f"nhận được {type(entry).__name__}"
        )
    # Trường bắt buộc: references phải là list
    if "references" not in entry:
        raise ResponseError(
            f"{arxiv_id}: entry batch thiếu trường 'references'"
        )
    refs = entry["references"]
    if refs is None or not isinstance(refs, list):
        raise ResponseError(
            f"{arxiv_id}: 'references' phải là list, "
            f"nhận được {type(refs).__name__ if refs is not None else 'null'}"
        )
    # referenceCount: nếu có phải là int không âm, không phải bool
    count = entry.get("referenceCount")
    if count is not None:
        if isinstance(count, bool) or not isinstance(count, int) or count < 0:
            raise ResponseError(
                f"{arxiv_id}: 'referenceCount' phải là số nguyên không âm, "
                f"nhận được {count!r}"
            )
    # Kiểm tra kiểu từng ref sẽ được normalize
    for i, ref in enumerate(refs):
        validate_reference(ref, f"{arxiv_id}.references[{i}]")


def validate_page_data(data, arxiv_id: str, offset: int, seen_offsets: set[int]):
    """Kiểm tra một trang từ /references trước khi dùng.

    Raise ResponseError nếu page thiếu 'data', 'next' không hợp lệ hoặc phát hiện vòng lặp.
    Trả về next_offset hoặc None nếu hết trang.
    """
    if not isinstance(data, dict):
        raise ResponseError(
            f"{arxiv_id} offset={offset}: page response phải là object, "
            f"nhận được {type(data).__name__}"
        )
    if "data" not in data:
        raise ResponseError(
            f"{arxiv_id} offset={offset}: page thiếu trường 'data'"
        )
    page_data = data["data"]
    if not isinstance(page_data, list):
        raise ResponseError(
            f"{arxiv_id} offset={offset}: 'data' phải là list, "
            f"nhận được {type(page_data).__name__}"
        )
    if len(page_data) == 0 and data.get("next") is not None:
        raise ResponseError(
            f"{arxiv_id} offset={offset}: page rỗng nhưng vẫn có 'next' — "
            f"nghi vòng lặp vô hạn"
        )
    # Validate 'next'
    nxt = data.get("next")
    if nxt is not None and (isinstance(nxt, bool) or not isinstance(nxt, int)):
        raise ResponseError(
            f"{arxiv_id} offset={offset}: 'next' phải là integer hoặc null, "
            f"nhận được {nxt!r}"
        )
    if nxt is not None and nxt <= offset:
        raise ResponseError(
            f"{arxiv_id} offset={offset}: 'next'={nxt} không tiến lên — "
            f"nghi vòng lặp"
        )
    if nxt is not None and nxt in seen_offsets:
        raise ResponseError(
            f"{arxiv_id} offset={offset}: 'next'={nxt} đã từng dùng — "
            f"vòng lặp phân trang"
        )
    if "offset" in data and (type(data["offset"]) is not int or data["offset"] != offset):
        raise ResponseError(f"{arxiv_id}: offset trả về không khớp request {offset}")
    for i, item in enumerate(page_data):
        if not isinstance(item, dict) or "citedPaper" not in item:
            raise ResponseError(f"{arxiv_id} offset={offset}: data[{i}] thiếu citedPaper/object")
        validate_reference(item["citedPaper"], f"{arxiv_id} offset={offset}.data[{i}].citedPaper")
    return nxt


# ---------------------------------------------------------------------------
# Kiểm tra count khớp
# ---------------------------------------------------------------------------
def count_matches(entry: dict, n_refs: int) -> bool:
    """Trả về True nếu referenceCount hợp lệ và khớp với số reference nhận về."""
    count = entry.get("referenceCount")
    if count is None or isinstance(count, bool) or not isinstance(count, int) or count < 0:
        return False
    return count == n_refs


def count_is_valid(entry: dict) -> bool | None:
    """Trả về True nếu referenceCount hợp lệ (int >= 0, không bool), None nếu thiếu."""
    count = entry.get("referenceCount")
    if count is None:
        return None
    return not isinstance(count, bool) and isinstance(count, int) and count >= 0


# ---------------------------------------------------------------------------
# Normalize một reference
# ---------------------------------------------------------------------------
_ARXIV_DOI = re.compile(r"^10\.48550/arxiv\.(.+)$", re.IGNORECASE)
_VERSION_SUFFIX = re.compile(r"v\d+$")


def _strip_version(arxiv_id: str) -> str:
    return _VERSION_SUFFIX.sub("", arxiv_id.strip())


def _extract_arxiv_id(cited: dict) -> tuple[str | None, str | None]:
    """Ưu tiên externalIds.ArXiv; nếu thiếu tách từ DOI dạng 10.48550/arXiv.xxxx."""
    ext = cited.get("externalIds") or {}
    if ext.get("ArXiv"):
        return _strip_version(str(ext["ArXiv"])), "externalIds"
    m = _ARXIV_DOI.match(str(ext.get("DOI") or ""))
    if m:
        return _strip_version(m.group(1)), "doi"
    return None, None


def normalize_ref(cited: dict | None) -> dict:
    validate_reference(cited, "reference")
    cited = cited or {}
    aid, source = _extract_arxiv_id(cited)
    return {
        "ref_arxiv_id": aid,
        "arxiv_id_source": source,
        "ref_title": cited.get("title"),
        "ref_authors": [
            a.get("name")
            for a in (cited.get("authors") or [])
            if a and a.get("name")
        ],
        "ref_submission_date": cited.get("publicationDate"),
        "ref_year": cited.get("year"),
        "ref_venue": cited.get("venue"),
        "ref_s2_id": cited.get("paperId"),
        "ref_external_ids": cited.get("externalIds"),
    }


# ---------------------------------------------------------------------------
# Dựng record
# ---------------------------------------------------------------------------
def build_record(
    publication: dict,
    entry: dict | None,
    *,
    raw_refs: list | None = None,
    method: str = "count_match",
    fetched_at: str | None = None,
) -> dict:
    """Dựng references.json; count mismatch chỉ được giữ sau khi lấy hết trang.

    publication: {"arxiv_id": "2608.09608", "versions": ["2608.09608v1", ...]}
    entry: S2 entry đã validate, hoặc None nếu not_in_s2.
    raw_refs: danh sách citedPaper nếu dùng phân trang (override entry["references"]).
    method: count_match, paged_count_match, paged_count_mismatch hoặc not_found.
    fetched_at: ISO timestamp của lúc fetch.
    """
    arxiv_id = publication["arxiv_id"]
    if entry is None:
        # S2 không tìm thấy bài — đã xác minh là not_in_s2
        return {
            "arxiv_id": arxiv_id,
            "s2_paper_id": None,
            "s2_reference_count": None,
            "status": "not_in_s2",
            "applies_to_versions": publication.get("versions") or [],
            "n_refs": 0,
            "n_refs_with_arxiv": 0,
            "n_refs_without_s2_id": 0,
            "references": [],
            "fetched_at": fetched_at,
            "verification": {"complete": True, "method": "not_found"},
        }

    validate_batch_entry(entry, arxiv_id)
    refs_raw = raw_refs if raw_refs is not None else entry["references"]
    complete = count_matches(entry, len(refs_raw))
    mismatch = method == "paged_count_mismatch"
    if mismatch and (raw_refs is None or count_is_valid(entry) is not True or complete):
        raise ResponseError(f"{arxiv_id}: count mismatch record không hợp lệ")
    if not complete and not mismatch:
        raise ResponseError(f"{arxiv_id}: chưa xác minh đủ references để dựng record")
    if method not in ("count_match", "paged_count_match", "paged_count_mismatch"):
        raise ResponseError(f"{arxiv_id}: verification method không hợp lệ: {method}")
    refs = [normalize_ref(c) for c in refs_raw]
    status = "count_mismatch" if mismatch else ("ok" if refs else "empty_refs")
    return {
        "arxiv_id": arxiv_id,
        "s2_paper_id": entry.get("paperId"),
        "s2_reference_count": entry.get("referenceCount"),
        "status": status,
        "applies_to_versions": publication.get("versions") or [],
        "n_refs": len(refs),
        "n_refs_with_arxiv": sum(1 for r in refs if r["ref_arxiv_id"]),
        "n_refs_without_s2_id": sum(1 for r in refs if not r["ref_s2_id"]),
        "references": refs,
        "fetched_at": fetched_at,
        "verification": {"complete": complete, "method": method},
    }


# ---------------------------------------------------------------------------
# Validate record đã lưu (thống kê và bảo vệ snapshot cũ)
# ---------------------------------------------------------------------------
def is_valid_record(rec: object, expected_id: str | None = None, *, allow_legacy: bool = False) -> bool:
    """Kiểm tra record lưu được, bao gồm record có cảnh báo count mismatch.

    Bản cũ không có verification chỉ hợp lệ khi đủ schema/count và không có cờ chưa xác minh.
    """
    if not isinstance(rec, dict):
        return False
    aid = rec.get("arxiv_id")
    if not isinstance(aid, str) or not _BASE_ID_RE.fullmatch(aid):
        return False
    if expected_id is not None and aid != expected_id:
        return False
    for name in ("s2_paper_id", "fetched_at"):
        if rec.get(name) is not None and not isinstance(rec[name], str):
            return False
    if type(rec.get("refresh_pending", False)) is not bool or rec.get("refresh_pending"):
        return False
    if rec.get("status") not in ("ok", "empty_refs", "not_in_s2", "count_mismatch"):
        return False
    if not isinstance(rec.get("references"), list):
        return False
    refs = rec["references"]
    n = rec.get("n_refs")
    if type(n) is not int or n < 0 or n != len(refs):
        return False
    for ref in refs:
        if not isinstance(ref, dict):
            return False
        for key in ("ref_arxiv_id", "ref_s2_id", "ref_title", "ref_submission_date", "ref_venue", "arxiv_id_source"):
            if ref.get(key) is not None and not isinstance(ref[key], str):
                return False
        authors = ref.get("ref_authors")
        if not isinstance(authors, list) or any(not isinstance(a, str) for a in authors):
            return False
        ext = ref.get("ref_external_ids")
        if ext is not None and not isinstance(ext, dict):
            return False
        year = ref.get("ref_year")
        if year is not None and (type(year) is not int):
            return False
    for name, actual in (
        ("n_refs_with_arxiv", sum(bool(r.get("ref_arxiv_id")) for r in refs)),
        ("n_refs_without_s2_id", sum(not r.get("ref_s2_id") for r in refs)),
    ):
        if type(rec.get(name)) is not int or rec[name] != actual:
            return False
    versions = rec.get("applies_to_versions")
    if not isinstance(versions, list) or any(
        not isinstance(v, str) or not v.startswith(aid) or not _VERSION_RE.fullmatch(v[len(aid):])
        for v in versions
    ):
        return False
    v = rec.get("verification")
    if rec["status"] == "count_mismatch":
        return (isinstance(v, dict) and v.get("complete") is False
                and v.get("method") == "paged_count_mismatch"
                and count_is_valid({"referenceCount": rec.get("s2_reference_count")}) is True
                and rec["s2_reference_count"] != n and not rec.get("truncation_unverified"))
    if v is None:
        if not allow_legacy or rec.get("truncation_unverified"):
            return False
        if rec.get("reference_count_mismatch") and not rec.get("truncation_fixed"):
            return False
    elif not isinstance(v, dict) or v.get("complete") is not True:
        return False
    if rec["status"] == "not_in_s2":
        return (n == 0 and rec.get("s2_reference_count") is None and rec.get("s2_paper_id") is None
                and isinstance(v, dict) and v.get("method") == "not_found")
    if rec["status"] != ("ok" if n else "empty_refs"):
        return False
    if not count_matches({"referenceCount": rec.get("s2_reference_count")}, n):
        return False
    if v is not None and v.get("method") not in ("count_match", "paged_count_match"):
        return False
    if rec.get("truncation_unverified"):
        return False
    return True