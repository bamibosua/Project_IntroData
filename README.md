# arXiv Project — Milestone 1: Full-Text Source Downloader

Repository mã nguồn cho phần tải toàn văn LaTeX / BibTeX nguồn của 20.000 bài báo arXiv thuộc môn **Introduction to Data Science**.

---

## 1. Cấu trúc Repository & Tổ chức Dữ liệu

Theo đúng yêu cầu của đề bài:
- Repository chỉ commit mã nguồn trong `src/` và hướng dẫn trong `README.md` (thư mục `data/` được loại trừ hoàn toàn trong `.gitignore`).
- Dữ liệu vào/ra được tổ chức rõ ràng trong `data/`:

```text
<group-repo>/
├── src/
│   ├── downloader.py               # Full-Text Source Downloader (.tex & .bib)
│   └── requirements.txt            # Thư viện phụ trợ tùy chọn
├── README.md                       # Hướng dẫn cài đặt và sử dụng
└── data/                           # (Ignored by Git) Chứa toàn bộ Input và Output
    ├── input/
    │   └── papers_index.json       # Metadata & danh sách version cần tải {"<id>": ["v1", ...]}
    └── output/
        ├── papers/                 # Thư mục chứa mã nguồn theo từng bài báo
        │   └── 2511-11709/         # Tên thư mục theo quy ước <YYMM-NNNNN>
        │       └── v1/             # Chỉ chứa các file .tex và .bib
        │           ├── main.tex
        │           └── referencearticle.bib
        ├── download_status.json    # Checkpoint & thống kê chi tiết từng version/bài
        ├── download_summary.json   # Thống kê tổng hợp toàn corpus (thời gian, RAM, dung lượng)
        └── download_summary_limit.json  # Thống kê khi chạy thử với cờ --limit
```

---

## 2. Cài đặt Môi trường

Yêu cầu **Python 3.10+**.

```powershell
# Tạo và kích hoạt môi trường ảo (khuyến nghị)
python -m venv .venv
.\.venv\Scripts\activate

# Cài đặt các thư viện phụ trợ (tùy chọn)
pip install -r src/requirements.txt
```

*(Lưu ý: `src/downloader.py` được thiết kế có thể chạy độc lập hoàn toàn chỉ bằng thư viện chuẩn của Python ngay cả khi chưa cài thư viện ngoài).*

---

## 3. Hướng dẫn Chạy `src/downloader.py`

Chương trình tải từng version qua `https://export.arxiv.org/src/{id}v{n}` (failover qua `/e-print/`). Mọi version của một bài được tải song song qua connection pool, giải nén và lọc trực tiếp trên RAM, **chỉ ghi file `.tex` và `.bib`** xuống thư mục tương ứng.

Các bài nộp dưới dạng PDF đơn thuần được phát hiện ngay qua magic bytes (`%PDF-`) và ngắt tải sớm để tiết kiệm băng thông. Khi gặp HTTP 429/403, downloader tự động kích hoạt cơ chế cooldown và tự điều chỉnh giãn nhịp request.

Cuối lượt tải, hệ thống tự động chạy **verification audit** rà soát toàn bộ các version đối chiếu với `papers_index.json` và tự động tải vét bất kỳ version nào bị sót.

### Bảng tham số dòng lệnh

| Tham số | Mặc định | Ý nghĩa |
|:---|:---|:---|
| `--index` | `data/input/papers_index.json` | Đường dẫn file index danh sách bài và version |
| `--output` | `data/output` | Thư mục output gốc |
| `--workers` | 8 | Số luồng xử lý giải nén và ghi đĩa |
| `--connections` | 32 | Số kết nối HTTP tải đồng thời |
| `--delay` | 0.6 | Độ trễ tối thiểu giữa hai request liên tiếp (giây) |
| `--limit` | — | Số lượng bài tải giới hạn (dùng để chạy thử) |

### Lệnh thực thi mẫu

```powershell
# 1. Chạy thử nghiệm 5 bài
python src/downloader.py --limit 5

# 2. Chạy toàn bộ 20.000 bài
python src/downloader.py --workers 8 --connections 32
```

---

## 4. Cơ chế Checkpoint & Ngắt tiến trình An toàn

- **Tự động lưu Checkpoint**: File `data/output/download_status.json` được cập nhật liên tục. Những bài đã tải thành công (`cached`) sẽ được tự động bỏ qua khi chạy lại lệnh, không bị tải trùng.
- **Hỗ trợ `Ctrl + C` an toàn**: Khi nhấn `Ctrl + C`, chương trình sẽ ngay lập tức flush toàn bộ trạng thái hiện tại xuống `download_status.json` rồi dừng an toàn, không làm hỏng dữ liệu và không bị treo.
