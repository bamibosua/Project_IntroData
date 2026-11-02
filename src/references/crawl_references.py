"""
Crawl references cho các bài trong discovered_papers_with_revised_dates.json qua Semantic Scholar.

  - Chạy theo batch (mặc định 50 bài/batch), chu kỳ mặc định 10 giây/request, 429 thì chờ 60 giây.
    Chỉnh thời gian trong src/config.py hoặc bằng --cycle / --wait-429 / --batch-size.
  - Mỗi bài ghi MỘT file thẳng vào thư mục có sẵn của bài:  <out>/<id>/references.json
    (mặc định <out> = data/output/papers, thư mục bài dạng 2411-15000; S2 không phân biệt version nên file dùng chung cho mọi version).
    Các file/thư mục khác trong thư mục bài (v1/, v2/, ...) không bị đụng tới.
  - Checkpoint: bài đã `ok` / `empty_refs` sẽ được bỏ qua khi chạy lại (dùng --force để làm lại).
  - Đo đạc đặt NGOÀI thư mục papers, mặc định data/output/references_meta/:
    crawl_log.jsonl (mỗi dòng một batch), run_*.json (mỗi lượt chạy), summary_references.json, papers_stats.csv.

Ví dụ (chạy từ thư mục gốc dự án):
    python src/crawl_references.py discovered_papers_with_revised_dates.json --limit 100
    python src/crawl_references.py discovered_papers_with_revised_dates.json
    python src/crawl_references.py discovered_papers_with_revised_dates.json --summarize
"""
import argparse
import datetime
import json
import os
import re
import sys

import config
from metrics import ResourceMonitor, RunMetrics, default_meta_dir, refs_size, summarize_output
from s2_client import TOO_LARGE, ReqStats, S2Client

ARXIV_DOI = re.compile(r"^10\.48550/arxiv\.(.+)$", re.IGNORECASE)
VERSION_SUFFIX = re.compile(r"v\d+$")
FAILED = object()                           # đánh dấu bài không lấy được trong batch
DEFAULT_OUT = os.path.join("data", "output", "papers")
DONE_STATUSES = ("ok", "empty_refs")        # bài ở các trạng thái này được bỏ qua khi chạy lại


# ----------------------------- ID & đường dẫn -----------------------------
def base_id(arxiv_id):
    """Bỏ hậu tố version (v1, v2...). ID kiểu cũ như hep-th/9901001 được giữ nguyên."""
    return VERSION_SUFFIX.sub("", arxiv_id.strip())


def folder_name(arxiv_id):
    """Tên thư mục bài: 2411.15000 -> 2411-15000 ; hep-th/9901001 -> hep-th_9901001."""
    return arxiv_id.replace("/", "_").replace(".", "-")


def record_path(out_dir, arxiv_id):
    return os.path.join(out_dir, folder_name(arxiv_id), "references.json")


def write_json_atomic(path, obj):
    """Ghi qua file tạm rồi đổi tên, để Ctrl+C giữa chừng không để lại file dở dang."""
    os.makedirs(os.path.dirname(path), exist_ok=True)
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(obj, f, ensure_ascii=False, indent=1)
    os.replace(tmp, path)
    return os.path.getsize(path)


# ----------------------------- đọc đầu vào -----------------------------
def load_papers(path):
    """Đọc discovered_papers_*.json. Chịu được file thiếu [] hoặc thừa dấu phẩy cuối. Khử trùng lặp theo base ID."""
    with open(path, encoding="utf-8") as f:
        text = f.read().strip()
    try:
        data = json.loads(text)
    except json.JSONDecodeError:
        data = json.loads("[" + text.rstrip().rstrip(",") + "]")
    if isinstance(data, (dict, str)):
        data = [data]
    papers, seen = [], set()
    for p in data:
        if isinstance(p, str):
            p = {"arxiv_id": p, "status": "found"}
        aid = base_id(str(p.get("arxiv_id", "")))
        if not aid or aid in seen:
            continue
        seen.add(aid)
        p["arxiv_id"] = aid
        papers.append(p)
    return papers


def is_done(out_dir, arxiv_id):
    try:
        with open(record_path(out_dir, arxiv_id), encoding="utf-8") as f:
            rec = json.load(f)
        return rec.get("status") in DONE_STATUSES
    except (OSError, ValueError):
        return False


# ----------------------------- chuẩn hóa reference -----------------------------
def extract_arxiv_id(cited):
    """Ưu tiên externalIds.ArXiv; nếu thiếu thì tách từ DOI dạng 10.48550/arXiv.xxxx. Trả về (id | None, nguồn | None)."""
    ext = cited.get("externalIds") or {}
    if ext.get("ArXiv"):
        return base_id(ext["ArXiv"]), "externalIds"
    m = ARXIV_DOI.match(ext.get("DOI") or "")
    if m:
        return base_id(m.group(1)), "doi"
    return None, None


def normalize_ref(cited):
    cited = cited or {}
    aid, source = extract_arxiv_id(cited)
    return {
        "ref_arxiv_id": aid,
        "arxiv_id_source": source,                 # "externalIds" | "doi" | None
        "ref_title": cited.get("title"),
        "ref_authors": [a.get("name") for a in (cited.get("authors") or []) if a and a.get("name")],
        "ref_submission_date": cited.get("publicationDate"),   # có thể None; có thể lệch với ngày nộp v1 trên arXiv
        "ref_year": cited.get("year"),
        "ref_venue": cited.get("venue"),
        "ref_s2_id": cited.get("paperId"),
        "ref_external_ids": cited.get("externalIds"),
    }


def build_record(paper, entry, raw_refs=None):
    """Dựng nội dung references.json cho một bài từ entry của batch (None nếu S2 không có bài)."""
    if entry is None:
        status, refs = "not_in_s2", []
    else:
        raw = raw_refs if raw_refs is not None else (entry.get("references") or [])
        refs = [normalize_ref(c) for c in raw]
        status = "ok" if refs else "empty_refs"
    return {
        "arxiv_id": paper["arxiv_id"],
        "s2_paper_id": (entry or {}).get("paperId"),
        "s2_reference_count": (entry or {}).get("referenceCount"),   # tổng số reference S2 ghi nhận (để đối chiếu)
        "status": status,
        # S2 không nhận ID có hậu tố version (đã thử), nên danh sách này dùng chung cho mọi version.
        "applies_to_versions": paper.get("versions") or [f"{paper['arxiv_id']}v1"],
        "n_refs": len(refs),
        "n_refs_with_arxiv": sum(1 for r in refs if r["ref_arxiv_id"]),
        "n_refs_without_s2_id": sum(1 for r in refs if not r["ref_s2_id"]),
        "references": refs,
    }


# ----------------------------- xử lý một batch -----------------------------
def _count_mismatch(entry, raw):
    """True nếu S2 báo số reference (referenceCount) nhiều hơn số reference thực nhận trong batch."""
    count = entry.get("referenceCount")
    return (config.VERIFY_ON_COUNT_MISMATCH and isinstance(count, int)
            and not isinstance(count, bool) and len(raw) < count)


def fetch_entries(client, papers):
    """Gửi batch; nếu phản hồi vượt 10 MB (HTTP 400) thì chia đôi và gửi lại, tới một bài thì chuyển sang /references.
    Trả về (entries, stats, số_lần_chia, lỗi). `entries` cùng thứ tự/độ dài với `papers`; mỗi phần tử là entry của S2,
    None (S2 không có bài) hoặc FAILED."""
    data, st, err = client.post_batch([p["arxiv_id"] for p in papers])
    if data is not None:
        return data, st, 0, None
    if err and err.startswith(TOO_LARGE):
        if len(papers) > 1:
            mid = len(papers) // 2
            print(f"    Phản hồi quá lớn, chia batch {len(papers)} thành {mid} + {len(papers) - mid}")
            left, st_l, sp_l, e_l = fetch_entries(client, papers[:mid])
            right, st_r, sp_r, e_r = fetch_entries(client, papers[mid:])
            st.add(st_l)
            st.add(st_r)
            return left + right, st, 1 + sp_l + sp_r, e_l or e_r
        full, st2, err2 = client.fetch_all_references(papers[0]["arxiv_id"])
        st.add(st2)
        if full is None:
            return [FAILED], st, 0, err2
        return [{"paperId": None, "references": full, "_via": "references_endpoint"}], st, 0, None
    return [FAILED] * len(papers), st, 0, err


def process_batch(client, papers, out_dir, truncation_min_refs=None):
    """Gửi một batch, ghi references.json cho từng bài, trả về số liệu đo của batch.
    Bài có referenceCount lớn hơn số reference nhận về (hoặc nhận về >= ngưỡng) được liệt kê rõ rồi đối chiếu với /references."""
    min_refs = config.TRUNCATION_CHECK_MIN_REFS if truncation_min_refs is None else truncation_min_refs
    t0 = client.clock()
    counts = {"ok": 0, "empty_refs": 0, "not_in_s2": 0, "error": 0}
    trunc = {"checked": 0, "fixed": 0, "unverified": 0, "count_mismatch": 0}
    refs_total = refs_arxiv = bytes_written = 0

    entries, stats, splits, err = fetch_entries(client, papers)
    if err:
        print(f"    Có bài lỗi, sẽ được thử lại ở lượt chạy sau: {err}")

    # --- liệt kê MỘT LẦN các bài cần đối chiếu, trước khi gửi bất kỳ request đối chiếu nào ---
    need = {}
    for paper, entry in zip(papers, entries):
        if entry is FAILED or entry is None or entry.get("_via"):
            continue
        raw = entry.get("references") or []
        reasons = []
        if _count_mismatch(entry, raw):
            reasons.append(f"referenceCount={entry['referenceCount']} > nhận về {len(raw)}")
        if len(raw) >= min_refs:
            reasons.append(f"nhận về {len(raw)} reference (>= {min_refs}, nghi cắt cụt)")
        if reasons:
            need[paper["arxiv_id"]] = reasons
    if need:
        print(f"    Phát hiện {len(need)} bài cần đối chiếu với /references:")
        for aid, reasons in need.items():
            print(f"      - {aid}: {'; '.join(reasons)}")
        print("    Gửi request để đối chiếu...")

    for paper, entry in zip(papers, entries):
        if entry is FAILED:
            counts["error"] += 1
            continue
        aid = paper["arxiv_id"]
        raw = (entry or {}).get("references") or []
        rec = build_record(paper, entry)
        if entry is not None and entry.get("_via"):
            rec["source"] = "references_endpoint"               # lấy qua phân trang vì batch quá lớn
        elif aid in need:                                       # chốt chặn cắt cụt
            mismatch = _count_mismatch(entry, raw)
            if mismatch:
                trunc["count_mismatch"] += 1
                rec["reference_count_mismatch"] = True
            trunc["checked"] += 1
            full, st2, _ = client.fetch_all_references(aid)
            stats.add(st2)
            if full is None:
                rec["truncation_unverified"] = True
                trunc["unverified"] += 1
                print(f"    !! {aid}: request đối chiếu thất bại, giữ {len(raw)} reference từ batch (đánh dấu chưa xác minh)")
            elif len(full) != len(raw):
                rec = build_record(paper, entry, raw_refs=full)
                if mismatch:
                    rec["reference_count_mismatch"] = True      # build_record tạo dict mới nên phải gắn lại cờ
                rec["truncation_fixed"] = True
                rec["n_refs_in_batch_response"] = len(raw)
                trunc["fixed"] += 1
                print(f"    OK {aid}: batch trả {len(raw)}, phân trang trả {len(full)} -> dùng bản đầy đủ")
            else:
                print(f"    OK {aid}: phân trang cũng trả {len(full)} (khớp batch) -> giữ nguyên")
        counts[rec["status"]] += 1
        refs_total += rec["n_refs"]
        refs_arxiv += rec["n_refs_with_arxiv"]
        bytes_written += write_json_atomic(record_path(out_dir, aid), rec)

    wall = client.clock() - t0
    work = wall - stats.wait_sec - stats.sleep_429_sec - stats.backoff_sec
    return {
        "ts": datetime.datetime.now().astimezone().isoformat(timespec="seconds"),
        "n_papers": len(papers), "counts": counts, "truncation": trunc, "splits": splits,
        "refs_total": refs_total, "refs_with_arxiv": refs_arxiv,
        "wall_sec": round(wall, 2), "work_sec": round(max(work, 0.0), 2),
        "bytes_written": bytes_written, "req": stats.as_dict(), "error": err,
    }


# ----------------------------- chương trình chính -----------------------------
def chunks(seq, n):
    for i in range(0, len(seq), n):
        yield seq[i:i + n]


def run(args, client=None):
    config.load_env()
    out_dir = args.out
    meta_dir = getattr(args, "meta_dir", None) or default_meta_dir(out_dir)
    os.makedirs(meta_dir, exist_ok=True)

    all_papers = load_papers(args.papers_file)
    found = [p for p in all_papers if p.get("status", "found") == "found"]

    if args.summarize:
        csv_path = os.path.join(meta_dir, "papers_stats.csv")
        s = summarize_output(out_dir, [p["arxiv_id"] for p in found], csv_path=csv_path, meta_dir=meta_dir)
        path = os.path.join(meta_dir, "summary_references.json")
        write_json_atomic(path, s)
        print(json.dumps(s, ensure_ascii=False, indent=2))
        print(f"\nĐã lưu: {os.path.abspath(path)}\nĐã lưu: {os.path.abspath(csv_path)}")
        return s

    if args.limit:
        found = found[: args.limit]
    todo = found if args.force else [p for p in found if not is_done(out_dir, p["arxiv_id"])]
    print(f"Đầu vào: {len(all_papers)} bài, {len(found)} bài status=found"
          f"{f' (giới hạn --limit {args.limit})' if args.limit else ''}; "
          f"cần crawl {len(todo)}, đã xong từ trước {len(found) - len(todo)}.")
    if not todo:
        print("Không còn bài nào cần crawl.")
        return None
    missing = [p["arxiv_id"] for p in todo if not os.path.isdir(os.path.join(out_dir, folder_name(p["arxiv_id"])))]
    if missing:
        note = " (thư mục output chưa tồn tại)" if not os.path.isdir(out_dir) else ""
        print(f"CẢNH BÁO: {len(missing)}/{len(todo)} bài chưa có thư mục trong {os.path.abspath(out_dir)}{note}; "
              f"crawler sẽ tạo mới. Ví dụ: {missing[:3]}. Nếu con số này lớn, hãy kiểm tra lại --out.")

    if client is None:
        key = config.get_api_key()
        print("Đã nạp API key." if key else "CẢNH BÁO: không có API key, rất dễ bị 429.")
        client = S2Client(api_key=key, cycle=args.cycle, wait_429=args.wait_429)
    batch_size = args.batch_size or config.BATCH_SIZE
    batches = list(chunks(todo, batch_size))
    print(f"{len(batches)} batch x tối đa {batch_size} bài, chu kỳ {client.cycle}s, nghỉ {client.wait_429}s khi 429. "
          f"Ước tính tối thiểu ~{len(batches) * client.cycle / 60:.1f} phút.\n")

    run_metrics = RunMetrics({"cycle_sec": client.cycle, "wait_429_sec": client.wait_429,
                              "batch_size": batch_size, "truncation_min_refs": config.TRUNCATION_CHECK_MIN_REFS})
    disk_before = refs_size(out_dir)
    log_path = os.path.join(meta_dir, "crawl_log.jsonl")
    interrupted = False
    with ResourceMonitor() as mon:
        try:
            for i, batch in enumerate(batches, 1):
                res = process_batch(client, batch, out_dir)
                run_metrics.add_batch(res)
                with open(log_path, "a", encoding="utf-8") as f:
                    f.write(json.dumps({"batch": i, **res}, ensure_ascii=False) + "\n")
                c = res["counts"]
                eta = (len(batches) - i) * client.cycle / 60
                print(f"[{i}/{len(batches)}] ok={c['ok']} rỗng={c['empty_refs']} không có trên S2={c['not_in_s2']} "
                      f"lỗi={c['error']} | {res['wall_sec']}s | còn ~{eta:.1f} phút")
        except KeyboardInterrupt:
            interrupted = True
            print("\nĐã dừng theo yêu cầu. Chạy lại cùng lệnh để tiếp tục từ chỗ dừng.")

    final = run_metrics.finalize(mon, disk_before, refs_size(out_dir), interrupted)
    stamp = datetime.datetime.now().strftime("%Y%m%d_%H%M%S")
    write_json_atomic(os.path.join(meta_dir, f"run_{stamp}.json"), final)

    p, t, m = final["papers"], final["time"], final["memory"]
    print("\n=== Tổng kết lượt chạy ===")
    print(f"Bài: thử {p['attempted']} | ok {p['ok']} | rỗng {p['empty_refs']} | "
          f"không có trên S2 {p['not_in_s2']} | lỗi {p['error']}")
    print(f"Thời gian: {t['total_wall_sec']}s tổng, {t['avg_wall_sec_per_paper']}s/bài "
          f"(thời gian làm việc thật {t['avg_work_sec_per_paper']}s/bài) | "
          f"429: {final['requests']['retries_429']} lần")
    print(f"RAM: đỉnh {m['peak_rss_mb']} MB, trung bình {m['avg_rss_mb']} MB | "
          f"Đĩa ghi thêm: {final['disk']['bytes_written_this_run'] / 1e6:.1f} MB")
    return final


def parse_args(argv=None):
    ap = argparse.ArgumentParser(description="Crawl references qua Semantic Scholar theo batch.")
    ap.add_argument("papers_file", help="discovered_papers_with_revised_dates.json")
    ap.add_argument("--out", default=DEFAULT_OUT,
                    help=f"thư mục chứa các thư mục bài, references.json ghi thẳng vào đó (mặc định {DEFAULT_OUT})")
    ap.add_argument("--meta-dir", default=None,
                    help="nơi ghi log/metrics (mặc định: thư mục references_meta ngang hàng với --out)")
    ap.add_argument("--limit", type=int, default=0, help="chỉ lấy N bài đầu (0 = tất cả)")
    ap.add_argument("--force", action="store_true", help="crawl lại cả bài đã xong")
    ap.add_argument("--summarize", action="store_true", help="chỉ tổng hợp thống kê từ output hiện có, không crawl")
    ap.add_argument("--batch-size", type=int, default=0, help=f"số bài mỗi batch (mặc định {config.BATCH_SIZE})")
    ap.add_argument("--cycle", type=float, default=None, help=f"giây giữa hai request (mặc định {config.CYCLE_SECONDS})")
    ap.add_argument("--wait-429", type=float, default=None,
                    help=f"giây chờ khi bị 429 (mặc định {config.WAIT_429_SECONDS})")
    return ap.parse_args(argv)


if __name__ == "__main__":
    run(parse_args())