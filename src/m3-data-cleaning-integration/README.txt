MILESTONE 3 — NGƯỜI 1: DATA CLEANING & DATASET INTEGRATION

PHẦN NÀY LÀM GÌ?
- Đọc metadata/references M1 và BibTeX đã parse từ M2.
- Chuẩn hóa arXiv ID, khoảng trắng, tác giả, năm và DOI; kiểm tra null.
- Gộp bản trùng tương thích; tách bản lỗi/xung đột vào quarantine.jsonl.
- Xuất Parquet cho papers, references, bibtex; chuyển elements/hierarchy M2
  sang Parquet và kiểm tra khóa liên kết.
- Ghi cleaning_report.json và publication_coverage.csv để theo dõi chất lượng.
- Chưa tạo nhãn, chia tập, huấn luyện model hoặc tích hợp predictions.

CÁCH CHẠY (từ thư mục gốc project)
python -m pip install -r src/m3-data-cleaning-integration/requirements.txt
python src/m3-data-cleaning-integration/cleaning.py
python src/m3-data-cleaning-integration/verify_outputs.py

Chỉnh inputs và output_dir trong config.json. Đường dẫn tương đối tính từ
thư mục chứa config. Input mặc định dùng các folder đã giải nén.
Output hiện tại: src/m3-data-cleaning-integration/data/m3-cleaning/
Đổi output_dir sang thư mục mới/rỗng trước khi chạy lại; không ghi đè kết quả.

BÀN GIAO CHO NGƯỜI 2
Các file dưới đây đều nằm trong thư mục output:

1. bibtex_clean.parquet — các entry cần gán nhãn.
   Cột: arxiv_id, citation_key, entry_type, title, authors, year, doi, journal, url.
   Mỗi entry được định danh bằng (arxiv_id, citation_key).

2. references_clean.parquet — reference candidates của từng publication.
   Cột: arxiv_id, ref_arxiv_id, ref_title, ref_authors, ref_submission_date, ref_s2_id.
   Người 2 đối chiếu mỗi entry với candidates có CÙNG arxiv_id, rồi ghi
   ref_arxiv_id vào groundtruth_arxiv_id hoặc xác nhận no_match sau khi kiểm tra.

3. publication_coverage.csv — owner, trạng thái nguồn và số dòng sạch mỗi paper.
   labeling_candidate=true là paper có dữ liệu hai phía, chưa bảo đảm parse đúng.
   Paper manual cần ít nhất 20 entry khác nhau sau đối chiếu key chuẩn M2.

4. cleaning_report.json — thống kê null, lỗi, ví dụ và các giới hạn dữ liệu.
5. quarantine.jsonl — chi tiết các bản bị loại; kiểm tra paper có reference bị
   loại do xung đột trước khi gán no_match, vì candidate pool có thể bị thiếu.
6. output_validation.json — kết quả kiểm tra schema, số dòng và khóa sau khi
   chạy verify_outputs.py.

papers_clean.parquet là metadata bổ sung nếu cần tra title/authors của paper.
Elements/hierarchy phục vụ tích hợp và feature engineering; chưa bắt buộc
cho labeling cơ bản. BibTeX vẫn cần M2 xác nhận canonical key và dedup.
Không dùng ô ground truth trống của entry chưa duyệt làm nhãn no_match.

CÁCH XEM FILE .PARQUET
Parquet là bảng nhị phân, không mở trực tiếp như CSV/text. Dùng script preview:

python src/m3-data-cleaning-integration/preview_parquet.py src/m3-data-cleaning-integration/data/m3-cleaning/bibtex_clean.parquet --rows 5

Lệnh hiển thị tổng số dòng, schema và 5 dòng đầu; không nạp toàn bộ bảng.
Đổi tên file thành references_clean.parquet để xem candidates.

Muốn xem bằng Excel, xuất một mẫu CSV (không xuất toàn bộ dataset):

python src/m3-data-cleaning-integration/preview_parquet.py src/m3-data-cleaning-integration/data/m3-cleaning/bibtex_clean.parquet --rows 100 --csv bibtex_preview.csv

Mở bibtex_preview.csv bằng Excel. Đây chỉ là mẫu 100 dòng; file bàn giao chính
vẫn là Parquet. Script từ chối ghi đè CSV đã tồn tại.

Xuất TẤT CẢ dòng để xem bằng Excel (đọc từng đợt, không nạp cả bảng):
python src/m3-data-cleaning-integration/preview_parquet.py src/m3-data-cleaning-integration/data/m3-cleaning/bibtex_clean.parquet --all --csv bibtex_all.csv
Đổi tên input/output tương ứng nếu muốn xuất references_clean.parquet.
