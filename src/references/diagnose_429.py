"""
Chẩn đoán lỗi 429: gửi vài request có kiểm soát, KHÔNG tự thử lại, in rõ mã trạng thái, nội dung và header giới hạn.

Chạy (PowerShell, từ thư mục gốc dự án):
    python src/references/diagnose_429.py papers_index.json
    python src/references/diagnose_429.py papers_index.json --sizes 10,50,200 --gap 15

Thứ tự: (1) GET một bài  (2) POST batch lần lượt từng cỡ trong --sizes  (3) GET một bài lần nữa.
Cách đọc: nếu (1) cũng 429 -> vấn đề nằm ở khóa/tài khoản/nhịp chung, không phải kích thước batch.
          nếu (1) = 200 mà batch cỡ lớn 429 -> batch lớn bị hạn chế riêng, hãy dùng cỡ nhỏ hơn.
          nếu (1) = 403 -> khóa API bị từ chối.
"""
import argparse
import sys
import time

import requests

import config
from crawl_references import load_papers
from s2_client import BASE, BATCH_FIELDS

TIMEOUT = 120


def send(label, method, url, headers, gap, **kw):
    time.sleep(gap)
    t0 = time.time()
    try:
        r = requests.request(method, url, headers=headers, timeout=TIMEOUT, **kw)
    except requests.RequestException as e:
        print(f"{label:<28} LỖI MẠNG: {e}")
        return None
    dt = time.time() - t0
    hdrs = {k: v for k, v in r.headers.items() if k.lower() == "retry-after" or k.lower().startswith("x-ratelimit")}
    line = f"{label:<28} HTTP {r.status_code} | {dt:5.1f}s | {len(r.content) / 1e6:6.2f} MB"
    if r.status_code != 200:
        line += f" | nội dung={r.text[:150]!r}"
    if hdrs:
        line += f" | header={hdrs}"
    print(line)
    return r.status_code


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("papers_file")
    ap.add_argument("--sizes", default="10,50,200", help="các cỡ batch cần thử, cách nhau bằng dấu phẩy")
    ap.add_argument("--gap", type=float, default=10, help="giây nghỉ trước mỗi request (mặc định 10)")
    args = ap.parse_args()

    config.load_env()
    key = config.get_api_key()
    headers = {"User-Agent": "iDS-arxiv-project/0.1 (diagnose)"}
    if key:
        headers["x-api-key"] = key
        print(f"Khóa API: đã nạp (độ dài {len(key)} ký tự).")
    else:
        print("Khóa API: KHÔNG tìm thấy -> đang chạy không xác thực.")

    ids = [p["arxiv_id"] for p in load_papers(args.papers_file) if p.get("status", "found") == "found"]
    sizes = [int(x) for x in args.sizes.split(",") if x.strip()]
    if len(ids) < max(sizes):
        sys.exit(f"File chỉ có {len(ids)} bài, không đủ cho cỡ batch {max(sizes)}.")

    print(f"Nghỉ {args.gap}s trước mỗi request. Không có thử lại, mỗi dòng là một request.\n")
    first = ids[0]
    send("GET 1 bài", "GET", f"{BASE}/ARXIV:{first}", headers, 0, params={"fields": "title,referenceCount"})
    for n in sizes:
        send(f"POST batch {n} bài", "POST", f"{BASE}/batch", headers, args.gap,
             params={"fields": BATCH_FIELDS}, json={"ids": ["ARXIV:" + i for i in ids[:n]]})
    send("GET 1 bài (sau batch)", "GET", f"{BASE}/ARXIV:{first}", headers, args.gap,
         params={"fields": "title,referenceCount"})


if __name__ == "__main__":
    main()