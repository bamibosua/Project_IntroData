"""
Client Semantic Scholar: batch POST, giãn cách theo chu kỳ, xử lý 429, fallback /references có phân trang.
Mọi request (kể cả các trang của fallback) đều đi qua cùng một bộ giãn cách (Pacer).
"""
import time
from dataclasses import dataclass, asdict

import requests

import config

BASE = "https://api.semanticscholar.org/graph/v1/paper"
# Không có khoảng trắng sau dấu phẩy.
BATCH_FIELDS = ("title,year,externalIds,referenceCount,references.paperId,references.title,references.authors,"
                "references.year,references.publicationDate,references.venue,references.externalIds")
REF_FIELDS = "paperId,title,authors,year,publicationDate,venue,externalIds"
PAGE_LIMIT = 1000
TOO_LARGE = "TOO_LARGE"   # tiền tố lỗi: phản hồi vượt 10 MB (HTTP 400 theo tài liệu S2)


def describe_429(resp):
    """Tóm tắt phản hồi 429 (nội dung + header liên quan tới giới hạn) để biết server từ chối vì lý do gì."""
    body = str(getattr(resp, "text", "") or "").replace("\n", " ")[:150]
    hdrs = {k: v for k, v in dict(getattr(resp, "headers", None) or {}).items()
            if k.lower() == "retry-after" or k.lower().startswith("x-ratelimit")}
    return f"nội dung={body!r} header={hdrs}"


class Pacer:
    """Đảm bảo hai request liên tiếp cách nhau tối thiểu `cycle` giây, tính từ lúc GỬI request trước."""

    def __init__(self, cycle, clock=time.monotonic, sleep=time.sleep):
        self.cycle, self.clock, self.sleep = cycle, clock, sleep
        self.last_start = None

    def wait(self):
        """Ngủ phần còn thiếu của chu kỳ. Trả về số giây đã ngủ."""
        slept = 0.0
        if self.last_start is not None:
            slept = max(0.0, self.cycle - (self.clock() - self.last_start))
            if slept > 0:
                self.sleep(slept)
        self.last_start = self.clock()
        return slept


@dataclass
class ReqStats:
    requests: int = 0            # số request đã gửi (kể cả thử lại)
    request_sec: float = 0.0     # tổng thời gian chờ phản hồi mạng
    bytes: int = 0               # tổng byte nhận về
    retries_429: int = 0
    sleep_429_sec: float = 0.0   # tổng thời gian nghỉ vì 429
    retries_other: int = 0
    backoff_sec: float = 0.0     # tổng thời gian nghỉ vì lỗi mạng/5xx
    wait_sec: float = 0.0        # tổng thời gian nghỉ để giữ chu kỳ

    def add(self, other):
        for k, v in asdict(other).items():
            setattr(self, k, getattr(self, k) + v)

    def as_dict(self):
        return asdict(self)


class S2Client:
    def __init__(self, api_key=None, cycle=None, wait_429=None, session=None,
                 clock=time.monotonic, sleep=time.sleep):
        self.cycle = config.CYCLE_SECONDS if cycle is None else cycle
        self.wait_429 = config.WAIT_429_SECONDS if wait_429 is None else wait_429
        self.clock, self.sleep = clock, sleep
        self.pacer = Pacer(self.cycle, clock, sleep)
        self.last_429 = None
        self.session = session or requests.Session()
        self.headers = {"User-Agent": "iDS-arxiv-project/0.1 (student crawler)"}
        if api_key:
            self.headers["x-api-key"] = api_key

    # ------------------------------------------------------------------
    def _request(self, method, url, **kw):
        """Gửi một request có giãn cách + thử lại. Trả về (json | None, ReqStats, lỗi | None)."""
        st = ReqStats()
        n429 = n_other = 0
        while True:
            st.wait_sec += self.pacer.wait()
            t0 = self.clock()
            try:
                r = self.session.request(method, url, headers=self.headers,
                                         timeout=config.REQUEST_TIMEOUT, **kw)
            except requests.RequestException as e:
                st.requests += 1
                n_other += 1
                if n_other > config.MAX_OTHER_RETRIES:
                    return None, st, f"lỗi mạng: {e}"
                self._backoff(n_other, st)
                continue
            dt = self.clock() - t0
            st.requests += 1
            st.request_sec += dt
            st.bytes += len(getattr(r, "content", b"") or b"")

            if r.status_code == 200:
                try:
                    return r.json(), st, None
                except ValueError as e:
                    n_other += 1
                    if n_other > config.MAX_OTHER_RETRIES:
                        return None, st, f"phản hồi không phải JSON: {e}"
                    self._backoff(n_other, st)
                    continue
            if r.status_code == 429:
                n429 += 1
                detail = describe_429(r)
                self.last_429 = detail
                if n429 > config.MAX_429_RETRIES:
                    return None, st, f"429 quá {config.MAX_429_RETRIES} lần thử ({detail})"
                wait = self.wait_429
                retry_after = str((getattr(r, "headers", None) or {}).get("Retry-After", "")).strip()
                if retry_after.isdigit():                    # server nói phải chờ bao lâu thì chờ ít nhất như vậy
                    wait = max(wait, int(retry_after))
                st.retries_429 += 1
                st.sleep_429_sec += wait
                print(f"    HTTP 429, chờ {wait}s rồi gửi lại ({n429}/{config.MAX_429_RETRIES}) | {detail}")
                self.sleep(wait)
                continue
            if r.status_code >= 500:
                n_other += 1
                if n_other > config.MAX_OTHER_RETRIES:
                    return None, st, f"HTTP {r.status_code} quá {config.MAX_OTHER_RETRIES} lần thử"
                self._backoff(n_other, st)
                continue
            body = str(getattr(r, "text", ""))
            if r.status_code == 400 and "maximum size" in body.lower():
                return None, st, f"{TOO_LARGE}: {body[:200]}"
            return None, st, f"HTTP {r.status_code}: {body[:200]}"

    def _backoff(self, n, st):
        wait = 2 ** n * 2
        st.retries_other += 1
        st.backoff_sec += wait
        self.sleep(wait)

    # ------------------------------------------------------------------
    def post_batch(self, arxiv_ids):
        """Gửi một batch. Trả về (list entry | None, ReqStats, lỗi | None).
        Phần tử `None` trong list nghĩa là S2 không có bài đó; list luôn cùng thứ tự và độ dài với đầu vào."""
        body = {"ids": ["ARXIV:" + i for i in arxiv_ids]}
        data, st, err = self._request("POST", f"{BASE}/batch", params={"fields": BATCH_FIELDS}, json=body)
        if data is not None and (not isinstance(data, list) or len(data) != len(arxiv_ids)):
            return None, st, "độ dài phản hồi batch không khớp với số ID đã gửi"
        return data, st, err

    def fetch_all_references(self, arxiv_id):
        """Lấy toàn bộ reference của một bài qua /references (phân trang). Trả về (list citedPaper | None, ReqStats, lỗi)."""
        total = ReqStats()
        out, offset = [], 0
        while True:
            data, st, err = self._request("GET", f"{BASE}/ARXIV:{arxiv_id}/references",
                                          params={"fields": REF_FIELDS, "limit": PAGE_LIMIT, "offset": offset})
            total.add(st)
            if data is None:
                return None, total, err
            out.extend((item or {}).get("citedPaper") or {} for item in data.get("data", []))
            if data.get("next") is None:
                return out, total, None
            offset = data["next"]