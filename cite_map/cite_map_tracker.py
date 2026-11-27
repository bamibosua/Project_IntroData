"""
Ghi lại các lần gộp key trong lúc dedup và xuất cite_map.json.

Cách dùng trong reference_deduplicator.py (mỗi publication):

    from cite_map_tracker import CiteMapTracker

    tracker = CiteMapTracker("2511.11742")
    ...
    # mỗi khi bạn gộp ref `old_key` vào ref `kept_key`:
    tracker.record(old_key, kept_key)
    ...
    # sau khi dedup xong, ghi file CẠNH reference.bib của publication đó:
    tracker.save(output_folder)          # -> <output_folder>/cite_map.json

cite_map.json chỉ chứa data, ví dụ:
    {"2511.11742": {"Smolin1992GNewtonZero": "Smolin1992"}}

- Không có lần gộp nào (hoặc chỉ gộp key với chính nó) -> không ghi file,
  và xóa cite_map.json cũ nếu có (tránh dữ liệu lỗi thời).
- Gộp nối tiếp A->B rồi B->C sẽ tự thu gọn thành A->C.
"""
import json
from pathlib import Path


class CiteMapTracker:
    def __init__(self, pub_id: str, filename: str = "cite_map.json"):
        self.pub_id = pub_id
        self.filename = filename
        self._map = {}

    def record(self, old_key: str, kept_key: str) -> None:
        """Ghi nhận: ref có cite key `old_key` bị thay bằng `kept_key`."""
        if old_key and kept_key and old_key != kept_key:
            self._map[old_key] = kept_key

    def resolve(self) -> dict:
        """Thu gọn chuỗi (A->B, B->C => A->C); bỏ vòng lặp bất thường."""
        out = {}
        for old in self._map:
            seen, cur = {old}, self._map[old]
            while cur in self._map and cur not in seen:
                seen.add(cur)
                cur = self._map[cur]
            if cur != old:
                out[old] = cur
        return dict(sorted(out.items()))

    def to_dict(self) -> dict:
        m = self.resolve()
        return {self.pub_id: m} if m else {}

    def save(self, folder) -> Path | None:
        """Ghi <folder>/cite_map.json nếu có gộp; trả về đường dẫn hoặc None."""
        path = Path(folder) / self.filename
        data = self.to_dict()
        if not data:
            if path.exists():
                path.unlink()
            return None
        path.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
        return path