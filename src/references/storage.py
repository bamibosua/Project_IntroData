"""
Ghi JSON nguyên tử (atomic write) và quản lý thư mục meta cho references crawler.

Quy trình ghi:
  1. Tạo file tạm tên duy nhất trong cùng thư mục đích.
  2. Ghi UTF-8, flush + fsync trước khi đóng handle.
  3. os.replace(tmp → dest):
     - Thành công: xong.
     - WinError 5/32/33: tối đa 7 attempts (6 retry), tổng nghỉ 5.1 giây.
     - Lỗi khác hoặc hết retry: raise lỗi gốc.
  4. Dọn file tạm trong finally; thất bại chỉ cảnh báo.

Đọc record và thư mục output:
  - load_record(path)        — đọc JSON từ path; None nếu không tồn tại; raise nếu lỗi I/O / ACL.
  - Validation record dùng is_valid_record() từ records.py.
"""
from __future__ import annotations

import json
import logging
import os
import time
import uuid
from typing import Callable

logger = logging.getLogger(__name__)

# Delays (giây) cho retry os.replace khi gặp WinError
_REPLACE_DELAYS = [0.1, 0.2, 0.4, 0.8, 1.6, 2.0]

# WinError codes bị lock tạm thời trên Windows
_WIN_LOCKED_CODES = {5, 32, 33}


# ---------------------------------------------------------------------------
# Atomic write
# ---------------------------------------------------------------------------
def write_json_atomic(
    dest: str,
    data: object,
    *,
    sleep: Callable[[float], None] = time.sleep,
) -> int:
    """Ghi data ra dest nguyên tử (serialize → tmp → replace).

    Raise:
      OSError / IOError nếu ghi tạm hoặc replace thất bại vĩnh viễn.
    """
    payload = json.dumps(data, ensure_ascii=False, indent=2, allow_nan=False).encode("utf-8")
    return write_bytes_atomic(dest, payload, sleep=sleep)


def write_bytes_atomic(dest: str, payload: bytes, *, sleep=time.sleep) -> int:
    """Cùng cơ chế atomic cho payload JSON và CSV."""
    dest_dir = os.path.dirname(os.path.abspath(dest))
    os.makedirs(dest_dir, exist_ok=True)

    tmp = os.path.join(dest_dir, f".tmp_{uuid.uuid4().hex}")
    try:
        # Bước 1: ghi ra file tạm, flush + fsync
        with open(tmp, "xb") as fh:
            fh.write(payload)
            fh.flush()
            os.fsync(fh.fileno())

        # Bước 2: atomic replace với retry cho WinError
        _replace_with_retry(tmp, dest, sleep=sleep)
        tmp = None  # replace thành công → không cần dọn
        return len(payload)
    finally:
        # Dọn file tạm nếu chưa được replace (lỗi hoặc interrupted)
        if tmp is not None:
            try:
                os.unlink(tmp)
            except OSError as e:
                logger.warning("Cannot remove temporary file (%s): %s", type(e).__name__, tmp)


def _replace_with_retry(
    src: str,
    dest: str,
    *,
    sleep: Callable[[float], None] = time.sleep,
) -> None:
    """Gọi os.replace(src, dest) với retry cho WinError 5/32/33.

    Hết retry hoặc lỗi không thuộc _WIN_LOCKED_CODES → raise lỗi gốc.
    """
    last_exc: OSError | None = None
    for attempt, delay in enumerate([0.0] + _REPLACE_DELAYS):
        if delay > 0:
            sleep(delay)
        try:
            os.replace(src, dest)
            return
        except OSError as e:
            win_code = getattr(e, "winerror", None)
            if win_code in _WIN_LOCKED_CODES:
                last_exc = e
                logger.debug(
                    "os.replace attempt %d/%d failed (WinError %d): %s",
                    attempt + 1, len(_REPLACE_DELAYS) + 1, win_code, dest,
                )
                continue
            raise  # lỗi khác: raise ngay

    # Hết retry
    raise last_exc  # type: ignore[misc]


# ---------------------------------------------------------------------------
# Đọc record (thống kê và bảo vệ snapshot cũ)
# ---------------------------------------------------------------------------
def load_record(path: str) -> dict | None:
    """Đọc references.json từ path.

    Trả về:
      dict     — nội dung hợp lệ
      None     — file không tồn tại (cần crawl)

    Raise:
      OSError  — không đọc được vì lỗi quyền / I/O (không giả thành missing)
      ValueError — JSON hỏng hoặc root không phải dict
    """
    try:
        with open(path, encoding="utf-8") as fh:
            raw = fh.read()
    except FileNotFoundError:
        return None
    try:
        data = json.loads(raw)
    except json.JSONDecodeError as e:
        raise ValueError(f"JSON hỏng tại {path}: {e}") from e
    if not isinstance(data, dict):
        raise ValueError(f"Record không phải object tại {path}")
    return data


# ---------------------------------------------------------------------------
# Thư mục meta
# ---------------------------------------------------------------------------
def default_meta_dir(out_path: str) -> str:
    """Tính đường dẫn thư mục meta mặc định từ --out.

    Ví dụ: data/output/papers → data/output/papers_references_meta
    """
    parent = os.path.dirname(os.path.abspath(out_path))
    name = os.path.basename(os.path.abspath(out_path))
    return os.path.join(parent, f"{name}_references_meta")


def record_path(out_dir: str, arxiv_id: str) -> str:
    """Tính đường dẫn references.json của một arxiv_id trong out_dir.

    Cấu trúc cũ: 2608.09608 -> 2608-09608; hep-th/9901001 -> hep-th_9901001.
    """
    if __package__:
        from .records import folder_name
    else:
        from records import folder_name
    return os.path.join(out_dir, folder_name(arxiv_id), "references.json")
