"""
Cấu hình của crawler references. Muốn chỉnh THỜI GIAN thì sửa ngay phần đầu file này
(hoặc ghi đè tạm thời bằng tham số dòng lệnh --cycle / --wait-429 / --batch-size).
"""
import os

# =====================================================================
#  THỜI GIAN  <-- chỉnh ở đây
# =====================================================================
CYCLE_SECONDS = 10       # khoảng cách tối thiểu giữa hai request liên tiếp (tính từ lúc GỬI request trước)
WAIT_429_SECONDS = 60    # bị 429 thì chờ bấy nhiêu giây rồi gửi lại đúng request đó

# =====================================================================
#  BATCH
# =====================================================================
BATCH_SIZE = 200         # số bài mỗi batch (endpoint cho phép tối đa 500)

# =====================================================================
#  ĐỘ BỀN
# =====================================================================
MAX_429_RETRIES = 8      # số lần thử lại tối đa khi gặp 429 (cho mỗi request)
MAX_OTHER_RETRIES = 3    # số lần thử lại tối đa khi lỗi mạng / HTTP 5xx
REQUEST_TIMEOUT = 120    # giây

# =====================================================================
#  CHỐT CHẶN CẮT CỤT
#  Bài có từ ngần này reference trở lên sẽ được đối chiếu với endpoint /references (có phân trang).
#  500 là con số chọn để an toàn, không phải giới hạn đã biết của Semantic Scholar.
# =====================================================================
TRUNCATION_CHECK_MIN_REFS = 500

#  Batch trả thêm `referenceCount` (tổng số reference S2 ghi nhận). Nếu số reference nhận về ít hơn con số này
#  thì nghi bị cắt (hoặc S2 chỉ đếm khác) -> đối chiếu với /references. Mỗi lần đối chiếu tốn thêm >= 1 request
#  (>= CYCLE_SECONDS giây), nên nếu lượt chạy thử cho thấy nhiều bài lệch "vô hại" thì đặt False.
VERIFY_ON_COUNT_MISMATCH = True

# =====================================================================
#  ĐO ĐẠC
# =====================================================================
RAM_SAMPLE_INTERVAL = 0.5   # giây giữa hai lần lấy mẫu RAM

# =====================================================================
#  API KEY (đọc từ .env cạnh file này, hoặc từ biến môi trường)
# =====================================================================
KEY_NAMES = ("S2_API_KEY", "SEMANTIC_SCHOLAR_API_KEY", "SEMANTIC_SCHOLAR_KEY")


def load_env():
    """Đọc .env (cạnh file này hoặc ở thư mục đang chạy) vào os.environ, không ghi đè biến đã có."""
    here = os.path.dirname(os.path.abspath(__file__))
    for path in (os.path.join(here, ".env"), os.path.join(os.getcwd(), ".env")):
        if not os.path.isfile(path):
            continue
        with open(path, encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if not line or line.startswith("#") or "=" not in line:
                    continue
                key, _, val = line.partition("=")
                key = key.removeprefix("export ").strip()
                os.environ.setdefault(key, val.strip().strip('"').strip("'"))


def get_api_key():
    for name in KEY_NAMES:
        if os.environ.get(name):
            return os.environ[name]
    return None