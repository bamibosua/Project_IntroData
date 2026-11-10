<div align="center">

  <!-- BADGES (CĂN GIỮA & PHÓNG TO) -->
  <p align="center">
    <img src="https://img.shields.io/badge/HCMUS-FIT%20%7C%2024KHDL1-1b5e20?style=for-the-badge&logo=google-classroom&logoColor=white" alt="HCMUS 24KHDL1" height="34" />
    <img src="https://img.shields.io/badge/Course-Nh%E1%BA%ADp%20m%C3%B4n%20KHDL-0056D2?style=for-the-badge&logo=bookstack&logoColor=white" alt="Nhập môn KHDL" height="34" />
  </p>

  <h1>Project IntroData</h1>
  <p>Thu thập và xử lý dữ liệu bài báo khoa học từ arXiv</p>
  <p>Trường Đại học Khoa học Tự nhiên, ĐHQG-HCM · Lớp 24KHDL1</p>

</div>

## Quy ước làm việc chung

- Mỗi thành viên làm việc trên nhánh riêng, đặt tên theo nội dung như `feat/entry-discovery`, `fix/download-retry` hoặc `docs/team-rules`.
- Đồng bộ nhánh trước khi bắt đầu; kiểm tra thay đổi của người khác trước khi sửa phần dùng chung.
- Mỗi commit tập trung vào một thay đổi, có mô tả rõ ràng. Dùng tiền tố `feat:`, `fix:`, `refactor:`, `docs:`, `test:` hoặc `chore:`.
- Kiểm tra phần bị ảnh hưởng trước khi gửi thay đổi. Khi tạo pull request, nêu nội dung sửa, cách kiểm tra và hạn chế còn lại.
- Thống nhất trước khi thay đổi schema, tên trường, ID bài báo, phiên bản hoặc đường dẫn bàn giao giữa các module.
- Dùng đường dẫn tương đối hoặc cấu hình; tránh ghi cố định đường dẫn máy cá nhân vào mã nguồn.
- Viết file văn bản bằng UTF-8; giữ phong cách mã nguồn nhất quán với module đang sửa.

## Những gì cần commit

| Loại file | Quy định |
| --- | --- |
| Mã nguồn, script và test | Commit các thay đổi liên quan đến chức năng của dự án. |
| `README.md`, `.gitignore` và tài liệu chung | Cập nhật cùng thay đổi có ảnh hưởng đến cả nhóm. |
| `requirements.txt` và cấu hình môi trường chung | Cập nhật khi thêm hoặc thay đổi thư viện. |
| Cấu hình phân công như `src/entry_discovery/team_ranges.json` | Commit để các thành viên dùng cùng phạm vi dữ liệu. |
| Notebook `.ipynb` | Commit phần mã và ghi chú; xóa output lớn, dữ liệu nhạy cảm và thông tin cá nhân trước khi commit. |
| Dữ liệu mẫu nhỏ phục vụ test | Chỉ commit dữ liệu cần thiết, không chứa secrets và đặt ngoài thư mục dữ liệu đã bị ignore. |
| `.env.example`, `.env.template` | Chỉ chứa tên biến và giá trị minh họa, không có API key hoặc thông tin xác thực thật. |

Không bỏ qua toàn bộ `*.json` hoặc thư mục `tests/`: cấu hình và dữ liệu mẫu hợp lệ vẫn cần được quản lý phiên bản.

## Những gì không commit

- File `.env`, `.env.*` chứa cấu hình riêng; API key, token, mật khẩu, private key và thông tin xác thực trong bất kỳ file nào.
- Môi trường ảo như `.venv/`, `venv/`, `env/`; thư viện đã cài và sản phẩm đóng gói như `build/`, `dist/`, `*.egg-info/`.
- Dữ liệu crawl, corpus, archive tải về, checkpoint và kết quả pipeline. Đặt chúng trong `data/`; quy tắc ignore áp dụng cả ở gốc và trong các module.
- Thư mục `parsed_structure/`, `cleaned_structure/`, `papers_references_meta/` và các báo cáo sinh tự động như `benchmark_results.json`, `benchmark_sessions.jsonl`.
- Bytecode, cache Python/test/lint, notebook checkpoint, log và file tạm.
- Cấu hình IDE cá nhân như `.vscode/`, `.idea/` và file hệ điều hành như `.DS_Store`, `Thumbs.db`.

`.gitignore` chỉ bỏ qua file chưa được Git theo dõi. Nếu một file đã được commit, thêm quy tắc ignore không tự loại nó khỏi lịch sử hoặc ngừng theo dõi file đó.

## Kiểm tra trước khi commit

1. Xem `git status` và `git diff` để kiểm tra đúng phạm vi thay đổi.
2. Chỉ stage các file cần thiết; xem lại `git diff --cached` trước khi commit.
3. Kiểm tra không có secrets, dữ liệu lớn, output notebook hoặc đường dẫn cá nhân trong phần đã stage.
4. Kiểm tra chức năng bị ảnh hưởng và cập nhật tài liệu, cấu hình liên quan.

Ví dụ commit message: `feat: add references crawler`, `fix: preserve discovery checkpoints`, `docs: update team conventions`.

## Bàn giao dữ liệu

- Chia sẻ dữ liệu lớn qua nơi lưu trữ chung của nhóm; ghi rõ vị trí, phạm vi ID, phiên bản và trạng thái xử lý.
- Bàn giao kèm index, cấu hình và báo cáo cần thiết để thành viên khác kiểm tra dữ liệu.
- Không tự ý đổi schema hoặc ghi đè dữ liệu/checkpoint của thành viên khác. Tránh nhiều tiến trình cùng ghi vào một thư mục output.
