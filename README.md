# Project_IntroData

Crawler thu thập toàn bộ references và metadata từ Semantic Scholar, đồng thời xuất các cạnh trích dẫn ArXiv. Yêu cầu Python 3.10+.

## Cài đặt và chạy

Chạy các lệnh từ thư mục gốc dự án:

```powershell
python -m pip install -r src/requirements.txt
python -m src.references --limit 10 --no-key-prompt
```

Có thể đặt API key qua biến môi trường `S2_API_KEY`, hoặc nhập qua lời nhắc khi bỏ `--no-key-prompt`. Không lưu key vào mã nguồn.

```powershell
python -m src.references --input papers.csv --output-dir data/references --mode batch
```

Đầu vào hỗ trợ JSON, CSV và text. CSV dùng cột `arxiv_id` hoặc `paper_id`; text chứa một ID mỗi dòng. ID được bỏ tiền tố `arXiv:`, khoảng trắng và loại trùng trước khi crawl. Hậu tố version được giữ nguyên.

## Kết quả và checkpoint

Mặc định ghi vào `data/references/`, tách khỏi mã nguồn:

| File | Mục đích |
|---|---|
| `checkpoint.sqlite3` | Trạng thái, lịch retry và toàn bộ references của mỗi bài, lưu cùng một transaction |
| `references.csv` | Các cạnh trích dẫn thuộc tập đầu vào hiện tại |
| `references_summary.json` | Thống kê và lý do kết thúc |

Không tạo thêm JSON checkpoint hoặc thư mục batch. Thêm `--export-json` nếu cần `references.json`. File JSON đã có từ các lần chạy trước không tự bị xóa; khi không bật cờ này, bản JSON cũ không được cập nhật.

Resume được bật mặc định. Bài `found` và `not_found` được bỏ qua; bài `error` tự được thử lại trong cùng tiến trình. Khi chạy lại, chương trình khôi phục cả thời điểm retry. Kết quả xuất chỉ chứa các ID của lần chạy hiện tại, dù database có checkpoint của một tập lớn hơn.

`--no-resume` xóa các bản ghi trong database checkpoint của output directory được chọn và bắt đầu lại. Nó không xóa JSON cũ. Không chạy nhiều crawler đồng thời vào cùng một output directory.

JSON checkpoint và các batch JSON cũ không còn được đọc hay chuyển đổi. SQLite từ giai đoạn 1–2 được nâng schema trong transaction: các bài `found` cũ được đưa về hàng đợi crawl lại vì dữ liệu cũ thiếu references không có ArXiv ID và metadata. Bài `not_found` và lịch retry của bài lỗi được giữ. Các cạnh lọc cũ được bỏ ngay khi nâng schema; snapshot đầy đủ được lưu sau khi crawl lại thành công.

`--batch-chunk-size` điều khiển số ID mỗi API request (1–500, mặc định 50). `--batch-size` chỉ còn để tương thích lệnh cũ, không tạo file batch.

Batch defaults to 50 IDs. Transient failures get one initial attempt plus two retries, then single fallback for failed IDs. HTTP 429 and authentication failures wait and retry automatically in the same process. All requests share the limiter when using the same limiter instance.

Mã thoát CLI là `0` khi hoàn thành không có lỗi, `1` khi còn bài lỗi hoặc bị ngắt. Exception bất ngờ được truyền ra, với summary mang `stopped_reason=failed` nếu xuất summary thành công.

## Kiểm thử

```powershell
python -m pip install pytest
python -m pytest tests -q
```

Mặc định không gọi API thật. Để chạy thêm integration tests có mạng:

```powershell
$env:RUN_LIVE_API_TESTS = '1'
python -m pytest tests/test_references.py -q -k IntegrationSamplePapers
```

Các test hồi quy kiểm tra batch thành công/rỗng/thiếu, retry lỗi mạng, resume với tập con, khởi động lại từ đầu, nâng schema SQLite cũ, rollback transaction và trạng thái khi có exception.

## References request policy (stage 1)

- Batch chunk: 50 IDs (`--batch-chunk-size`); interval: 1.1 seconds (`--interval`).
- HTTP timeout: 60 seconds (`--timeout`, finite and positive).
- Single requests: client owns two retries; batch requests: crawler owns two retries (three attempts total in either case).
- Transient batch failures fall back to single for failed IDs only. Successful entries are checkpointed first.
- Failed single requests stay pending and retry automatically in this process, after 5, 10, 20, ... seconds, capped at 300 seconds.
- HTTP 429 pauses requests globally for at least 60 seconds before retrying; no single fallback for throttling.
- HTTP 401/403 pauses requests globally for 300 seconds before probing recovery. The process retains its key and re-reads S2_API_KEY in its own environment; no automatic public-mode fallback. Changing a parent terminal environment does not update an already-running process.
- HTTP 400 on a batch isolates IDs through single fallback. An unresolved single 400 stays pending with backoff; invalid request configuration requires correction and cannot be cured by retry alone.
- HTTP 404 / null batch entry is terminal `not_found`, not a successful metadata retrieval.
- Pending rows persist retry_count, next_retry_at (Unix timestamp), http_status and the last error in SQLite. Resume honors these deadlines.
- Only found/not_found entries advance progress. The run finishes when all input IDs have a terminal result; API failures have no global retry limit.
- Ctrl+C still interrupts safely. Unexpected programming or storage failures are reported rather than silently ignored.
- A permanently invalid key, bad request or unavailable service can keep the process waiting indefinitely. This does not imply data collection is progressing.

Stage 1 keeps continuous retry scheduling. Stage 2 extends client metadata, pagination and retry handling. Stage 3 persists complete reference snapshots; the final assignment export schema is a separate next stage.

Offline tests: `python -m unittest discover -s tests -q`.

## References client (stage 2)

- Requests reference paperId, title, authors, publicationDate and externalIds, plus the source referenceCount.
- Validates each batch member independently; malformed entries do not discard valid papers. Nullable metadata and null reference entries remain valid API values.
- Uses embedded references when their count matches referenceCount. Missing/mismatched counts trigger GET /paper/{id}/references, following offset/next in pages of up to 1000 records. Non-advancing cursors, bad pages and inconsistent counts become pending errors, never partial successes.
- All page requests use the same request gateway and limiter. A failed page retries the publication through the existing queue; page-level checkpoints are not introduced in this stage.
- Backoff for transient failures includes bounded additive jitter. Batch retries remain owned by the crawler; pagination within batch makes one HTTP attempt per page.
- Retry-After accepts finite seconds and HTTP dates. Server deadlines are shared through the limiter and persisted in error retry metadata; delays above 300 seconds are not shortened. Long cooldowns sleep in intervals of at most 60 seconds.
- API key content is omitted from authentication logs and the CLI banner.
- publicationDate is the Semantic Scholar publication date, not an ArXiv submission date.

The client returns richer metadata, including references without ArXiv IDs. Stage 3 stores these complete snapshots and automatically re-crawls found rows from the older filtered checkpoint schema.

API contract: https://api.semanticscholar.org/api-docs/graphs

## Lưu toàn bộ references (giai đoạn 3)

- SQLite tách bảng `papers` (trạng thái, nguồn, thời điểm cập nhật, lịch retry) và `reference_rows` (publication nguồn, vị trí reference, ArXiv ID nếu có, payload JSON gốc).
- Payload giữ nguyên tác giả, ngày xuất bản, external IDs, S2 ID và mọi trường API trả về. Reference thiếu ID, reference trùng và mục `null` vẫn được lưu theo vị trí, không gộp nhầm các reference không có ID.
- Ghi lại một publication thay thế snapshot cũ và trạng thái trong cùng transaction. Nếu lỗi giữa chừng, snapshot trước vẫn nguyên vẹn. `--no-resume` xóa cả trạng thái và references trong database được chọn.
- Resume chỉ đọc trạng thái cho các ID đầu vào theo nhóm tối đa 400 ID. Crawler chỉ giữ trạng thái và bộ đếm trong RAM; CSV/JSON đọc từng dòng từ SQLite. Bộ nhớ vẫn phụ thuộc kích thước một response API/batch và danh sách publication đầu vào.
- `total_stored_references` đếm mọi mục lưu cục bộ, gồm cả bản trùng và `null`. `total_arxiv_references` đếm cạnh ArXiv duy nhất theo cặp publication nguồn và ArXiv ID đích. `unique_cited_arxiv_ids` được tính trên tập đầu vào hiện tại.
- CSV và JSON tùy chọn vẫn dùng bốn cột cũ: `source_arxiv_id`, `cited_arxiv_id`, `cited_title`, `cited_s2_id`. Chỉ bước export loại trùng cạnh ArXiv; metadata đầy đủ vẫn nằm trong SQLite. Chuẩn hóa sáu cột theo đề sẽ làm ở giai đoạn export.
- `publicationDate` được giữ riêng là ngày xuất bản, không xem là ngày nộp ArXiv. Dữ liệu vẫn ở cấp publication, không giả định khác biệt giữa các version.

Đọc snapshot đầy đủ bằng Python:

```python
from pathlib import Path
from src.references.storage import CheckpointStore

store = CheckpointStore(Path("data/references/checkpoint.sqlite3"))
for record in store.iter_reference_records(["2511.11709"]):
    print(record["cited_authors"], record["cited_publication_date"])
    print(record["raw_reference"])  # dict gốc hoặc None nếu API trả null
```
