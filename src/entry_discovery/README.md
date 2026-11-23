# Entry Discovery

Chỉnh tham số trong `discovery_config.json`, sau đó chạy từ thư mục gốc project:

```powershell
python src/entry_discovery/run_entry_discovery.py
```

Runner đọc cấu hình cùng thư mục với nó. Tham số truyền trên terminal (nếu có) được ưu tiên hơn file.

| Tham số | Ý nghĩa |
| --- | --- |
| `batch_size` | Số ID mỗi lượt lấy metadata (1–50). `null`: dùng batch size của checkpoint; nếu chưa có thì dùng 50. Tiếp tục checkpoint phải giữ cùng batch size. |
| `revision_batch_size` | Số revision mỗi lượt truy vấn (1–50). |
| `delay` | Nghỉ giữa các request, tính bằng giây, tối thiểu 3. |
| `cooldown` | Thời gian chờ ban đầu khi bị giới hạn request, tối thiểu 60 giây. |
| `max_cooldown` | Giới hạn thời gian chờ, phải >= `cooldown`. |
| `max_delay` | Giới hạn delay tự điều chỉnh, phải >= `delay`. |
| `max_consecutive_429` | Dừng sau số lần lỗi 429 liên tiếp này; `0` là thử lại không giới hạn. |
| `recovery_rounds` | Số vòng không có tiến triển tối đa trước khi dừng, tối thiểu 1. |
| `pass_number` | Lượt chạy: `1` hoặc `2`. |
| `overwrite_metadata` | `true` cho phép cập nhật metadata đã tồn tại; mặc định `false`. |

Khoảng ID của các thành viên vẫn chỉnh trong `team_ranges.json`.
JSON dùng `null`, `true`, `false` và không hỗ trợ comment.
