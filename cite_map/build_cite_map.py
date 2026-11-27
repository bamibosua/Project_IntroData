#!/usr/bin/env python3
"""
Tạo cite_map.json: liệt kê các cite key bị dedup theo key gộp NHẦM.

Cấu trúc thư mục mong đợi:
    reference_output/
      2511-11709/references.bib      (tên file bib tự nhận; hoặc đặt bằng --bib-name)
      2511-11742/references.bib
      ...

Kết quả mặc định: 1 file  reference_output/cite_map.json  (không tạo thư mục mới), chỉ chứa data:
    {
      "2511.11742": {"Smolin1992GNewtonZero": "Smolin1992"},
      "2510.01234": {"keyA": "keyB"}
    }
  - key ngoài cùng : id publication (2511-11742 -> 2511.11742)
  - "A": "B"       : key A sẽ bị dedup theo key gộp vào B, nhưng A và B là 2 tài liệu khác nhau
                     (hoặc chưa chắc giống nhau) -> cần fulltext dedup
  - publication không có va chạm thì không xuất hiện.

Dùng (chạy trong thư mục chứa detect_key_collisions.py):
    python build_cite_map.py reference_output
    python build_cite_map.py reference_output --per-folder   # ghi thêm cite_map.json cạnh file .bib
    python build_cite_map.py reference_output --id-style folder   # giữ nguyên tên thư mục (2511-11742)
"""
import argparse
import json
import os
import re
import sys
from pathlib import Path

for _stream in (sys.stdout, sys.stderr):  # tránh lỗi mã hóa tiếng Việt trên console Windows
    try:
        _stream.reconfigure(encoding="utf-8")
    except Exception:
        pass

# Dòng này luôn in ra khi file được chạy. Nếu KHÔNG thấy dòng này trên terminal
# thì file build_cite_map.py trên máy đang rỗng / chưa lưu / sai nội dung.
print(f"[build_cite_map] BẮT ĐẦU - file: {Path(__file__).resolve()}")

try:
    from detect_key_collisions import analyze, load_refs
except Exception as _e:
    print(f"THẤT BẠI: không import được detect_key_collisions.py ({_e!r}). "
          f"Hãy để build_cite_map.py và detect_key_collisions.py cùng một thư mục, và đã lưu (Ctrl+S) cả hai file.")
    sys.exit(1)


def rel_to_cwd(p: Path) -> str:
    try:
        return Path(os.path.relpath(p, Path.cwd())).as_posix()
    except ValueError:  # khác ổ đĩa trên Windows
        return p.resolve().as_posix()


def publication_id(folder_name: str, style: str) -> str:
    """2511-11742 -> 2511.11742 (style 'dot'); style 'folder' giữ nguyên tên thư mục."""
    if style == "dot":
        return re.sub(r"^(\d{4})-(\d{4,5})(.*)$", r"\1.\2\3", folder_name)
    return folder_name


def find_bibs(root: Path, bib_name):
    """Mỗi thư mục publication chọn đúng 1 file .bib."""
    if bib_name:
        return sorted(root.rglob(bib_name))
    by_dir = {}
    for p in root.rglob("*.bib"):
        by_dir.setdefault(p.parent, []).append(p)
    picked = []
    for d, files in sorted(by_dir.items()):
        files = sorted(files)
        choice = next((f for pref in ("references.bib", "reference.bib")
                       for f in files if f.name == pref), files[0])
        if len(files) > 1:
            print(f"[CẢNH BÁO] {rel_to_cwd(d)} có {len(files)} file .bib, dùng '{choice.name}' "
                  f"(đổi bằng --bib-name)")
        picked.append(choice)
    return picked


def problem_map(analysis):
    """
    {key bị gộp nhầm: key hấp thụ nó}.
    Trong mỗi nhóm va chạm, key ngắn nhất là key "hấp thụ" (dedup theo prefix/author+year
    sẽ gộp mọi key khác về nó). Chỉ liệt kê key mà cặp với key hấp thụ KHÔNG chắc chắn
    là cùng một công trình (different_work / uncertain). Cặp same_work là gộp hợp lệ nên bỏ qua.
    """
    out = {}
    for g in analysis["collision_groups"]:
        members = g["keys"]
        absorbing = min(members, key=lambda k: (len(k), k))
        verdict, bad_partner = {}, set()
        for p in g["pairs"]:
            verdict[frozenset((p["a"], p["b"]))] = p["verdict"]
            if p["verdict"] != "same_work":
                bad_partner.update((p["a"], p["b"]))
        for k in members:
            if k == absorbing:
                continue
            v = verdict.get(frozenset((absorbing, k)))
            flagged = (v != "same_work") if v is not None else (k in bad_partner)
            if flagged:
                out[k] = absorbing
    return dict(sorted(out.items()))


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("root", nargs="?", default="milestone1/reference_output", help="thư mục gốc (mặc định: reference_output)")
    ap.add_argument("--bib-name", default=None,
                    help="tên file bib. Mặc định tự nhận: references.bib / reference.bib / file .bib duy nhất trong thư mục")
    ap.add_argument("-o", "--output", default=None,
                    help="đường dẫn file kết quả (mặc định: <root>/cite_map.json)")
    ap.add_argument("--per-folder", action="store_true",
                    help="ghi thêm cite_map.json cạnh file .bib của từng publication có va chạm")
    ap.add_argument("--id-style", choices=["dot", "folder"], default="dot",
                    help="dot: 2511-11742 -> 2511.11742 (mặc định); folder: giữ nguyên tên thư mục")
    args = ap.parse_args()

    root = Path(args.root)
    if not root.is_dir():
        print(f"THẤT BẠI: không thấy thư mục '{root}' (đang đứng ở {Path.cwd()}). "
              f"Hãy chạy lệnh từ thư mục chứa '{root}' hoặc truyền đường dẫn đầy đủ.")
        return 1

    bibs = find_bibs(root, args.bib_name)
    if not bibs:
        names = sorted({p.name for p in root.rglob("*.bib")})[:10]
        hint = f" Các file .bib tìm thấy: {names}" if names else " Không có file .bib nào."
        print(f"THẤT BẠI: không thấy file bib '{args.bib_name or '*.bib'}' trong {root.resolve()}.{hint}")
        return 1

    print(f"Root: {root.resolve()}")
    cite_map, errors, n_folder_files = {}, [], 0
    for bib in bibs:
        pub = bib.parent
        pid = publication_id(pub.name, args.id_style)
        try:
            analysis = analyze(load_refs(bib))
            pm = problem_map(analysis)
        except Exception as e:  # một publication lỗi không làm dừng cả batch
            errors.append(pid)
            print(f"[LỖI] {rel_to_cwd(pub)}: {e!r}")
            continue

        if pm:
            cite_map[pid] = pm
            if args.per_folder:
                (pub / "cite_map.json").write_text(
                    json.dumps({pid: pm}, ensure_ascii=False, indent=2), encoding="utf-8")
                n_folder_files += 1
        flag = f"{len(pm)} key bị gộp nhầm" if pm else "ok"
        print(f"  {pid:<14} refs={analysis['total_refs']:<4} {flag:<20} {rel_to_cwd(bib)}")

    out = Path(args.output) if args.output else root / "cite_map.json"
    out.write_text(json.dumps(dict(sorted(cite_map.items())), ensure_ascii=False, indent=2), encoding="utf-8")

    print(f"Tổng: {len(bibs)} publication, {len(cite_map)} có key bị gộp nhầm, {len(errors)} lỗi")
    print(f"File kết quả: {rel_to_cwd(out)}")
    if args.per_folder:
        print(f"Đã ghi thêm {n_folder_files} file cite_map.json cạnh file .bib")

    if errors and len(errors) == len(bibs):
        print("THẤT BẠI: tất cả publication đều lỗi.")
        return 1
    if errors:
        print(f"XONG NHƯNG CÓ LỖI: {len(errors)} publication bị lỗi (xem dòng [LỖI] phía trên): {errors}")
        return 2
    print(f"THÀNH CÔNG: {len(cite_map)} publication có key bị gộp, đã ghi {rel_to_cwd(out)}.")
    return 0


if __name__ == "__main__":
    sys.exit(main())