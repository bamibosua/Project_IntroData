# Milestone 3 — Người 2: Data Labelling & Data Splitting (`src/m3_labeling/`)

Module tổng hợp nhãn thủ công (Manual Ground Truth), tạo nhãn tự động bằng thuật toán cổ điển (Regex + Exact + Fuzzy, **không dùng LLM/pretrained**), kiểm định chất lượng nhãn và chia tập `train` / `valid` / `test` theo cấp `publication` (`arxiv_id`) chống rò rỉ dữ liệu.

## 1. Cài đặt & Cách chạy (từ thư mục gốc project)

```powershell
python -m pip install -r src/m3_labeling/requirements.txt

# Chạy trọn bộ 4 bước:
python -m src.m3_labeling.run_m3_labeling
```

Hoặc chạy từng bước riêng lẻ:

```powershell
python -m src.m3_labeling.manual_labeling
python -m src.m3_labeling.auto_labeling
python -m src.m3_labeling.label_validation
python -m src.m3_labeling.data_split
```

Đầu vào mặc định:
- Output Người 1: `data/output/m3-cleaning/` (`bibtex_clean.parquet`, `references_clean.parquet`, `publication_coverage.csv`, `quarantine.jsonl`)
- 4 file nhãn tay: `data/input/{Minh_Hang,Trong_Nhan,Vinh_Nguyen,Yen_Vy}/labels.csv`

Đầu ra mặc định: `data/output/m3-labeling/`

---

## 2. Hợp đồng bàn giao cho Người 3 (Candidate Generation & Feature Engineering) và Người 4 (Modeling & Evaluation)

Tất cả file bàn giao nằm trong `data/output/m3-labeling/`:

1. **`labeled_entries.parquet`** *(bảng nhãn hợp nhất — 1 dòng = 1 BibTeX entry đã có nhãn)*:
   - `arxiv_id` (`string`, non-null): khóa join với `bibtex_clean.parquet` và `references_clean.parquet`.
   - `citation_key` (`string`, non-null): cùng `arxiv_id` tạo khóa chính duy nhất.
   - `partition` (`string`, non-null): `train` | `valid` | `test` *(khớp trực tiếp cột `partition` của bảng `predictions` M3)*.
   - `label_source` (`string`, non-null): `manual` | `auto` *(giúp Người 4 phân tích riêng MRR@5 trên manual test vs auto test)*.
   - `label_status` (`string`, non-null): `matched` | `no_match`.
   - `groundtruth_arxiv_id` (`string`, nullable): `ref_arxiv_id` khi `matched`, `null` khi `no_match`.
   - `match_rule` (`string`, non-null): `manual` hoặc tên rule tự động (`tier1_arxiv_id`, `tier2_exact_title`, `tier3_fuzzy_multi_signal`, `tier3_author_year_fallback`, `no_match`). **LƯU Ý CHỐNG LEAKAGE: Người 3 tuyệt đối KHÔNG đưa `match_rule` hay `label_source` vào ma trận feature `X`!**

2. **`publication_splits.parquet` & `publication_splits.csv`** *(1 dòng = 1 publication thuộc `train` / `valid` / `test`)*:
   - Cột: `arxiv_id, partition, label_source, n_bibtex_entries, n_labeled_entries, n_matched, n_no_match, n_ref_candidates`.
   - Quy tắc sinh nhãn nhị phân `y` cho Người 3 khi tạo cặp ứng viên `(bibtex i, reference j)` có **cùng `arxiv_id`**:
     - Chỉ sinh hàng huấn luyện/đánh giá cho các entry xuất hiện trong `labeled_entries.parquet` (*"Unlabeled entries simply carry no rows"* cho các entry chưa gán trong bài manual).
     - `y = 1` nếu `label_status == "matched"` và `ref_arxiv_id == groundtruth_arxiv_id`.
     - `y = 0` trong mọi trường hợp còn lại (bao gồm toàn bộ cặp của entry `no_match`).

3. **Các file báo cáo & kiểm định (phục vụ kiểm tra và Poster M3):**
   - `manual_summary.json` & `manual_excluded_keys.jsonl` (nếu có key trong CSV không tồn tại trong `bibtex_clean.parquet`).
   - `auto_eval_on_manual.json` (Precision, Recall, F1, Exact Accuracy của `auto_labeling.py` đo trên tập manual gold standard).
   - `auto_summary.json` (thống kê số lượng và phân bố rule trên automatic pool).
   - `label_validation_report.json` (kiểm tra PK/FK, không overlap, đủ định mức `k = 4`, kiểm tra quarantine).
   - `split_summary.json` (thống kê số lượng publications/entries và `match_rate` trên từng tập `train` / `valid` / `test`).
