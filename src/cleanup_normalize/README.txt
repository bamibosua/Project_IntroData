CLEANUP LATEX – CÁC BẢN SỬA VÀ BÁO CÁO

Các cú pháp đã hỗ trợ thêm:
- Khoảng trắng sau begin/end, ví dụ \end {eqnarray}.
- Tham số alignat/alignedat là một token không có ngoặc, ví dụ \begin{alignat}1.
- Alias khai báo macro có định nghĩa rõ ràng, ví dụ \newcommand{\nc}{\newcommand}.
  Giữ toàn bộ thân khai báo opaque, không đọc \begin{equation} trong đó như công thức đang chạy.
- Công thức inline mở bằng \( và đóng bằng $, hoặc ngược lại.
  Giữ nguyên dollar escaped và công thức con nằm trong nhóm có ngoặc.

Không sửa file source gốc. Các body_segment span vẫn dùng tọa độ source gốc.
Cleanup không phải trình biên dịch TeX: trạng thái completed không thay thế kiểm tra PDF/log.
Macro có tham số, scope hoặc conditional phức tạp chưa được thực thi như TeX.

Kiểm thử từ thư mục gốc project:
  .\.venv\Scripts\python.exe -m unittest discover -s src/cleanup_normalize -p test_cleanup_leaves.py -v

Chạy lại các phiên bản đầu vào ok từng bị cleanup lỗi, xuất riêng kết quả phục hồi:
  .\.venv\Scripts\python.exe src/cleanup_normalize/audit_cleanup_failures.py --output-dir src/cleanup_normalize/data/cleanup_fix_review --export-recovered

Phạm vi audit lấy từ failed_records trong src/cleaned_structure/summary.json.
Không bao gồm các outline có trạng thái partial/failed từ bước structure.
Dataset cleaned_structure và summary cũ chưa được thay thế bằng kết quả audit.

Trong data/cleanup_fix_review:
- audit.json: trạng thái từng phiên bản, số lượng, context, hash code cleanup.
- versions.csv: toàn bộ phiên bản đã kiểm tra và kết quả.
- unresolved_versions.csv: từng phiên bản còn lỗi, note, bằng chứng, file source và dòng.
- unresolved_papers.csv: gom các phiên bản và nhóm lỗi theo paper.
- README.txt: danh sách còn lỗi chia nhóm, có giải thích để đọc nhanh.
- recovered/<paper>/vN/cleaned_nodes.json: cây đã chạy được trong đợt kiểm tra.

Đọc cause_confidence để biết nguyên nhân đã xác minh hay mới là triệu chứng.
source_line có thể trỏ đầu context hoặc đầu body_segment; xem location_precision.
Không diễn giải mọi lỗi ghép dấu toán thành source LaTeX bị hỏng.
Khi chạy lại cùng output, audit.json là danh sách kết quả có hiệu lực; không gom
mọi file bằng glob từ thư mục recovered của nhiều lần chạy khác nhau.
