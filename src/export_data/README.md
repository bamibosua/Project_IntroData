# Cấu hình export data

Chỉnh `export_config.json` trước khi chạy:

| Khóa | Ý nghĩa |
| --- | --- |
| `paper_roots` | Danh sách thư mục dữ liệu đầu vào. Mỗi thư mục chứa các thư mục paper, bên trong có `metadata.json` và `references.json`. |
| `ownership` | Đường dẫn file CSV phân công paper cho thành viên. |
| `output_dir` | Thư mục lưu Parquet và báo cáo export. |

Đường dẫn tương đối được tính từ **thư mục chứa file config**, không phụ thuộc thư mục đang chạy lệnh. Có thể dùng đường dẫn tuyệt đối; trên Windows nên dùng `/`, ví dụ `D:/datasets/papers`.

Ví dụ config cho dữ liệu ở ổ D:

```json
{
  "paper_roots": ["D:/datasets/papers"],
  "ownership": "../entry_discovery/data/input/id_ownership.csv",
  "output_dir": "D:/datasets/exported"
}
```

Chạy từ thư mục gốc project (cần cài `src/requirements.txt`):

```powershell
python src/export_data/export_m1_parquet.py
```

Để chạy thử với 100 paper đầu tiên:

```powershell
python src/export_data/export_m1_parquet.py --limit 100 --out "src/export_data/data/test_100"
```

`--limit` chọn tối đa N paper theo thứ tự trong CSV `ownership`, sau khi lọc `--owner`/`--id` nếu có. Giới hạn áp dụng cho tổng số paper của tất cả thành viên. Paper thiếu metadata hợp lệ sẽ được ghi trong báo cáo và có thể khiến số paper xuất thực tế nhỏ hơn N. Thư mục xuất riêng trong ví dụ giúp giữ kết quả chạy thử tách khỏi kết quả export đầy đủ.

Dùng config khác hoặc chỉ export một thành viên/paper:

```powershell
python src/export_data/export_m1_parquet.py --config "D:/datasets/export_config.json"
python src/export_data/export_m1_parquet.py --owner "ten_thanh_vien" --id 2301.00001
```

Các tham số `--paper-root`, `--ownership`, `--out` được ưu tiên hơn config. `--paper-root` có thể lặp lại để đọc nhiều nguồn; danh sách này thay thế toàn bộ `paper_roots` trong config. Đường dẫn trên dòng lệnh được tính từ thư mục đang chạy lệnh như trước.

```powershell
python src/export_data/export_m1_parquet.py --paper-root "D:/datasets/papers" --out "D:/datasets/exported"
```

Kết quả nằm trong `output_dir/<owner>/`:

- `data/papers/part-00000.parquet`
- `data/references/part-00000.parquet`
- `export_report.json`

Nếu trùng đường dẫn kết quả đã có, lần chạy sau sẽ ghi đè các file này.
