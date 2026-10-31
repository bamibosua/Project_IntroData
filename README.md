# arXiv Project — Milestone 1: Data Crawling

Repository mã nguồn cho đồ án môn **Introduction to Data Science** (Milestone 1: Entry Discovery, Full-Text LaTeX/BibTeX Source Download, và Reference Crawling).

---

## 1. Cấu trúc Repository & Tổ chức Dữ liệu (`Input` / `Output`)

Theo đúng yêu cầu của đề bài:
- Repository chỉ commit mã nguồn trong `src/` và hướng dẫn trong `README.md` (thư mục `data/` đã được loại trừ trong `.gitignore`).
- Toàn bộ dữ liệu đầu vào (`input`) và đầu ra (`output`) được tách bạch rõ ràng trong thư mục `data/`:

```text
<group-repo>/
├── src/
│   ├── discovery.py                # Phần 1: Entry Discovery (arXiv API)
│   ├── downloader.py               # Phần 2: Full-Text Source Download (.tex & .bib)
│   ├── references.py               # Phần 3: Reference Metadata Crawling (Semantic Scholar)
│   ├── pipeline_colab.ipynb        # Notebook tích hợp chạy trên Google Colab
│   └── requirements.txt            # Các thư viện Python cần thiết
├── README.md                       # Hướng dẫn cài đặt môi trường và chạy code
└── data/                           # (Ignored by Git) Chứa toàn bộ Input và Output
    ├── input/
    │   ├── discovered_papers_with_revised_dates.json   # Input: Metadata + danh sách version từ bước Discovery
    │   └── team_ranges.json                            # Cấu hình khoảng arXiv ID của 4 thành viên
    └── output/
        ├── sources/                                    # Corpus nguồn: mỗi bài báo 1 thư mục riêng
        │   ├── 2511-11709/                             # Tên thư mục theo quy ước <YYMM-NNNNN>
        │   │   ├── metadata.json                       # Metadata của bài báo & danh sách file từng version
        │   │   └── v1/                                 # Chỉ chứa các file .tex và .bib (đã loại bỏ hình ảnh)
        │   │       ├── main.tex
        │   │       └── referencearticle.bib
        │   └── 2511-11710/
        │       ├── metadata.json
        │       └── v1/
        ├── papers.json                                 # Bảng `papers` đúng schema đề bài (dùng cho Dataset)
        ├── download_status.json                        # Checkpoint & thống kê dung lượng chi tiết từng bài/version
        └── download_summary.json                       # Thống kê tổng hợp (Success rate, Size, Runtime, RAM, Disk)
```

---

## 2. Cài đặt Môi trường

Yêu cầu **Python 3.10+**.

```powershell
# Tạo môi trường ảo (khuyến nghị)
python -m venv .venv
.\.venv\Scripts\activate

# Cài đặt các gói phụ trợ
pip install -r src/requirements.txt
```

*(Lưu ý: `src/downloader.py` có thể chạy trực tiếp bằng thư viện chuẩn của Python ngay cả khi chưa cài các gói phụ trợ).*

---

## 3. Hướng dẫn Chạy Full-Text Source Downloader (`src/downloader.py` — Multi-threaded Pipelined HTTP)

Module `src/downloader.py` tải từng version qua `https://export.arxiv.org/src/{version_id}` (failover `/e-print/`), giữ nhịp tối thiểu 3 giây/request theo quy định của arXiv. Các tác vụ giải nén, lọc `.tex`/`.bib` và ghi đĩa chạy ở các luồng nền (`--workers`) chồng lấn với thời gian chờ mạng; `download_status.json` được ghi theo lô (`--checkpoint-interval`). Khi gặp HTTP 429/403 chương trình tự lưu checkpoint, nghỉ (cooldown), tạo lại session và request lại; các bài lỗi được vét lại cuối lượt (`--retry-passes`).

### 3.1. Chạy thử nghiệm với N bài đầu tiên (`--limit`)
```powershell
python src/downloader.py --limit 5
```

### 3.2. Chạy toàn bộ
```powershell
python src/downloader.py --workers 8
```

### 3.3. Chạy tải theo phân công từng thành viên (`--member`)
Bốn thành viên trong `data/input/team_ranges.json` gồm: `Yen_Vy`, `Vinh_Nguyen`, `Trong_Nhan`, `Minh_Hang`.
```powershell
# Tải thử 10 bài của thành viên Yen_Vy
python src/downloader.py --member Yen_Vy --limit 10

# Tải toàn bộ 5.000 bài của thành viên Yen_Vy
python src/downloader.py --member Yen_Vy
```

### 3.4. Chạy theo khoảng ID tùy chọn (`--start`, `--end`) hoặc danh sách ID cụ thể (`--ids`)
```powershell
python src/downloader.py --start 2511.11709 --end 2511.11750
python src/downloader.py --ids 2511.11709 2511.11710
```

### 3.5. Cơ chế Resume / Checkpoint tự động
- Khi đang tải mà bấm `Ctrl + C` hoặc dừng giữa chừng, toàn bộ các bài đã tải đều được lưu trạng thái trong `data/output/download_status.json` và `data/output/sources/`.
- Khi chạy lại cùng câu lệnh, chương trình sẽ bỏ qua các bài đã hoàn tất (`cached`) và chỉ tải tiếp những bài còn thiếu.
- Nếu muốn tải lại từ đầu (ghi đè cache), thêm cờ `--refresh`.
