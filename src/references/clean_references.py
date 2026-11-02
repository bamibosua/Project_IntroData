"""
Xóa các file do crawler references tạo ra, để chạy thử lại từ đầu.

Chỉ xóa đúng:
  - <out>/<thư mục bài>/references.json  (và references.json.tmp còn sót nếu lần chạy trước bị ngắt)
  - với --meta: thêm các file đo đạc trong thư mục references_meta
    (crawl_log.jsonl, run_*.json, summary_references.json, papers_stats.csv)
KHÔNG xóa thư mục bài, v1/, v2/, mã nguồn LaTeX hay bất kỳ file nào khác. Chỉ tìm ở tầng ngay dưới <out>,
nên file trùng tên nằm sâu hơn (vd. <out>/<bài>/v1/references.json) không bị đụng tới.

Ví dụ (chạy từ thư mục gốc dự án):
    python src/clean_references.py --dry-run          # chỉ liệt kê, không xóa
    python src/clean_references.py                    # hỏi xác nhận rồi xóa references.json
    python src/clean_references.py --yes --meta       # xóa luôn cả log/số liệu đo, không hỏi
"""
import argparse
import glob
import os
import sys

from crawl_references import DEFAULT_OUT
from metrics import default_meta_dir

PAPER_FILES = ("references.json", "references.json.tmp")
META_FILES = ("crawl_log.jsonl", "summary_references.json", "papers_stats.csv")


def find_paper_files(out_dir):
    """Các file references.json (và .tmp) nằm ngay trong từng thư mục bài dưới out_dir."""
    found = []
    with os.scandir(out_dir) as it:
        for d in it:
            if d.is_dir():
                for name in PAPER_FILES:
                    path = os.path.join(d.path, name)
                    if os.path.isfile(path):
                        found.append(path)
    return sorted(found)


def find_meta_files(meta_dir):
    if not os.path.isdir(meta_dir):
        return []
    found = [os.path.join(meta_dir, n) for n in META_FILES if os.path.isfile(os.path.join(meta_dir, n))]
    found += glob.glob(os.path.join(meta_dir, "run_*.json"))
    return sorted(found)


def total_size(paths):
    return sum(os.path.getsize(p) for p in paths if os.path.exists(p))


def main(argv=None):
    ap = argparse.ArgumentParser(description="Xóa references.json do crawler tạo ra (để test lại nhiều lần).")
    ap.add_argument("--out", default=DEFAULT_OUT, help=f"thư mục chứa các thư mục bài (mặc định {DEFAULT_OUT})")
    ap.add_argument("--meta-dir", default=None, help="thư mục log/metrics (mặc định: references_meta ngang hàng với --out)")
    ap.add_argument("--meta", action="store_true", help="xóa cả các file log/số liệu đo trong thư mục meta")
    ap.add_argument("--dry-run", action="store_true", help="chỉ liệt kê, không xóa gì")
    ap.add_argument("--yes", "-y", action="store_true", help="không hỏi xác nhận")
    args = ap.parse_args(argv)

    if not os.path.isdir(args.out):
        print(f"Không tìm thấy thư mục: {os.path.abspath(args.out)} (kiểm tra lại --out).")
        return -1
    meta_dir = args.meta_dir or default_meta_dir(args.out)

    targets = find_paper_files(args.out)
    n_paper = len(targets)
    if args.meta:
        targets += find_meta_files(meta_dir)
    n_meta = len(targets) - n_paper

    print(f"Thư mục papers: {os.path.abspath(args.out)}")
    print(f"  references.json (và .tmp) sẽ xóa: {n_paper}")
    if args.meta:
        print(f"Thư mục meta: {os.path.abspath(meta_dir)}")
        print(f"  file đo đạc sẽ xóa: {n_meta}")
    if not targets:
        print("Không có gì để xóa.")
        return 0
    print(f"Tổng dung lượng: {total_size(targets) / 1e6:.2f} MB. Ví dụ: {[os.path.relpath(t) for t in targets[:3]]}")

    if args.dry_run:
        print("--dry-run: không xóa gì.")
        return 0
    if not args.yes:
        try:
            answer = input(f"Xóa {len(targets)} file này? (y/N): ").strip().lower()
        except EOFError:
            answer = ""
        if answer not in ("y", "yes"):
            print("Đã hủy, không xóa gì.")
            return 0

    deleted = failed = 0
    for path in targets:
        try:
            os.remove(path)
            deleted += 1
        except OSError as e:
            failed += 1
            print(f"  Không xóa được {path}: {e}")
    if args.meta and os.path.isdir(meta_dir) and not os.listdir(meta_dir):
        os.rmdir(meta_dir)                      # thư mục meta rỗng thì bỏ luôn
    print(f"Đã xóa {deleted} file" + (f", {failed} file lỗi." if failed else "."))
    return deleted


if __name__ == "__main__":
    sys.exit(0 if main() >= 0 else 1)