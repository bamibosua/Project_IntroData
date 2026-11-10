"""Semantic Scholar client: một pacer, cooldown chung và retry hữu hạn."""
from __future__ import annotations

import datetime
import email.utils
import logging
import math
import time
from dataclasses import asdict, dataclass

import requests

if __package__:
    from . import config
    from .records import ResponseError, count_is_valid, validate_batch_entry, validate_page_data
else:
    import config
    from records import ResponseError, count_is_valid, validate_batch_entry, validate_page_data

logger = logging.getLogger(__name__)
BASE = "https://api.semanticscholar.org/graph/v1/paper"
BATCH_FIELDS = (
    "title,year,externalIds,referenceCount,references.paperId,references.title,"
    "references.authors,references.year,references.publicationDate,"
    "references.venue,references.externalIds"
)
REF_FIELDS = "paperId,title,authors,year,publicationDate,venue,externalIds"
PAGE_LIMIT = 1000
_BACKOFF = (4.0, 8.0, 16.0)
_WAIT_PRINT_INTERVAL = 60.0


class ClientError(Exception):
    def __init__(self, message: str, *, code: str, retryable: bool = False, status: int | None = None):
        super().__init__(message)
        self.code, self.retryable, self.status = code, retryable, status


class FatalError(Exception):
    """Request/xác thực sai: runner dừng và giữ mọi commit đã hoàn tất."""
    def __init__(self, message: str, *, status: int | None = None):
        super().__init__(message)
        self.status = status


@dataclass
class ReqStats:
    requests: int = 0
    request_sec: float = 0.0
    bytes: int = 0
    retries_429: int = 0
    sleep_429_sec: float = 0.0
    retries_other: int = 0
    backoff_sec: float = 0.0
    wait_sec: float = 0.0

    def add(self, other: ReqStats) -> None:
        for key, value in asdict(other).items():
            setattr(self, key, getattr(self, key) + value)

    def as_dict(self) -> dict:
        return asdict(self)


def parse_retry_after(header_value: str | None, *, warn_id: str = "") -> float | None:
    if not header_value:
        return None
    value = header_value.strip()
    try:
        seconds = float(value)
        if math.isfinite(seconds) and seconds >= 0:
            return seconds
    except ValueError:
        pass
    try:
        deadline = email.utils.parsedate_to_datetime(value)
        if deadline.tzinfo is not None:
            return max(0.0, (deadline - datetime.datetime.now(datetime.timezone.utc)).total_seconds())
    except (ValueError, TypeError, OverflowError):
        pass
    logger.warning("%s: invalid Retry-After header; using the configured wait.", warn_id)
    return None


def _wait_with_progress(seconds, reason, *, sleep=time.sleep, clock=time.monotonic,
                        print_interval=_WAIT_PRINT_INTERVAL) -> None:
    if seconds <= 0:
        return
    deadline = clock() + seconds
    logger.debug("Waiting %.1fs: %s", seconds, reason)
    while (remaining := deadline - clock()) > 0:
        sleep(min(remaining, print_interval))
        if deadline - clock() > 0:
            print(f"  Waiting: {reason}; {deadline - clock():.0f}s remaining.")


class Pacer:
    def __init__(self, cycle, clock=time.monotonic, sleep=time.sleep):
        self.cycle, self.clock, self.sleep = cycle, clock, sleep
        self.last_start = None

    def wait(self) -> float:
        started = self.clock()
        if self.last_start is not None:
            remaining = max(0.0, self.cycle - (started - self.last_start))
            _wait_with_progress(remaining, "request pacing", sleep=self.sleep, clock=self.clock)
        self.last_start = self.clock()
        return self.last_start - started


class S2Client:
    def __init__(self, api_key=None, cycle=None, wait_429=None, session=None,
                 clock=time.monotonic, sleep=time.sleep):
        self.cycle = config.CYCLE_SECONDS if cycle is None else cycle
        self.wait_429 = config.WAIT_429_SECONDS if wait_429 is None else wait_429
        self.clock, self.sleep = clock, sleep
        self.pacer = Pacer(self.cycle, clock, sleep)
        self._cooldown_until = 0.0
        self._cooldown_kind = "sleep_429_sec"
        self.stats = ReqStats()  # cộng ngay khi request/wait xảy ra, kể cả lỗi/Ctrl+C
        self.session = session if session is not None else requests.Session()
        self.headers = {"User-Agent": "iDS-arxiv-project/0.1 (student crawler)"}
        if api_key:
            self.headers["x-api-key"] = api_key

    def close(self) -> None:
        self.session.close()

    def _apply_cooldown(self, extra_seconds, reason="", *, kind="sleep_429_sec") -> float:
        deadline = self.clock() + extra_seconds
        if deadline >= self._cooldown_until:
            self._cooldown_until, self._cooldown_kind = deadline, kind
        return max(0.0, self._cooldown_until - self.clock())

    def _wait_cooldown(self, context="") -> float:
        remaining = max(0.0, self._cooldown_until - self.clock())
        if remaining:
            self._wait(remaining, self._cooldown_kind, f"cooldown {context}")
        return remaining

    def _wait(self, seconds, field, reason) -> None:
        started = self.clock()
        try:
            _wait_with_progress(seconds, reason, sleep=self.sleep, clock=self.clock)
        finally:
            setattr(self.stats, field, getattr(self.stats, field) + self.clock() - started)

    def _request(self, method, url, context="", *, validator=None, **kwargs):
        before = self.stats.as_dict()
        n429 = n_other = 0
        while True:
            self._wait_cooldown(context)
            started = self.clock()
            try:
                self.pacer.wait()
            finally:
                self.stats.wait_sec += self.clock() - started

            started = self.clock()
            self.stats.requests += 1
            try:
                try:
                    response = self.session.request(method, url, headers=self.headers,
                                                    timeout=config.REQUEST_TIMEOUT, **kwargs)
                finally:
                    self.stats.request_sec += self.clock() - started
            except requests.RequestException as exc:
                n_other += 1
                if n_other > config.MAX_OTHER_RETRIES:
                    logger.warning("  %s: network error; retries exhausted.", context)
                else:
                    logger.warning("  %s: network error (%s); wait %.0fs, retry %d/%d.",
                                   context, type(exc).__name__, _BACKOFF[min(n_other - 1, len(_BACKOFF) - 1)],
                                   n_other, config.MAX_OTHER_RETRIES)
                if n_other > config.MAX_OTHER_RETRIES:
                    raise ClientError(f"{context}: lỗi mạng sau {config.MAX_OTHER_RETRIES} retry",
                                      code="retryable") from exc
                self._backoff(n_other, context)
                continue

            self.stats.bytes += len(response.content or b"")
            status = response.status_code
            if status == 200:
                try:
                    data = response.json()
                    if validator is not None:
                        validator(data)
                except ValueError as exc:
                    n_other += 1
                    if n_other > config.MAX_OTHER_RETRIES:
                        logger.warning("  %s: invalid JSON/schema; retries exhausted.", context)
                    else:
                        logger.warning("  %s: invalid JSON/schema; wait %.0fs, retry %d/%d.",
                                       context, _BACKOFF[min(n_other - 1, len(_BACKOFF) - 1)],
                                       n_other, config.MAX_OTHER_RETRIES)
                    if n_other > config.MAX_OTHER_RETRIES:
                        raise ClientError(f"{context}: JSON/schema không hợp lệ: {exc}",
                                          code="invalid_response") from exc
                    self._backoff(n_other, context)
                    continue
                delta = ReqStats(**{k: v - before[k] for k, v in self.stats.as_dict().items()})
                return data, delta

            if status == 429:
                n429 += 1
                delay = max(self.wait_429, parse_retry_after(response.headers.get("Retry-After"), warn_id=context) or 0)
                self._apply_cooldown(delay, context)  # kể cả response cuối hết budget
                if n429 > config.MAX_429_RETRIES:
                    logger.warning("  %s: HTTP 429; retries exhausted.", context)
                    raise ClientError(f"{context}: HTTP 429 hết retry; cooldown còn {delay:.0f}s",
                                      code="retryable", status=429)
                self.stats.retries_429 += 1
                logger.warning("  %s: HTTP 429; wait %.0fs, retry %d/%d.", context, delay, n429, config.MAX_429_RETRIES)
                continue

            if status >= 500:
                n_other += 1
                delay = _BACKOFF[min(n_other - 1, len(_BACKOFF) - 1)]
                if status == 503:
                    delay = max(delay, parse_retry_after(response.headers.get("Retry-After"), warn_id=context) or 0)
                    self._apply_cooldown(delay, context, kind="backoff_sec")
                if n_other > config.MAX_OTHER_RETRIES:
                    logger.warning("  %s: HTTP %d; retries exhausted.", context, status)
                    raise ClientError(f"{context}: HTTP {status} hết retry", code="retryable", status=status)
                self.stats.retries_other += 1
                logger.warning("  %s: HTTP %d; wait %.0fs, retry %d/%d.",
                               context, status, delay, n_other, config.MAX_OTHER_RETRIES)
                if status != 503:
                    self._wait(delay, "backoff_sec", f"HTTP {status}: {context}")
                continue

            if status == 400 and "maximum size" in (response.text or "").lower():
                raise ClientError(f"{context}: HTTP 400 response quá lớn", code="too_large", status=400)
            if status in (401, 403):
                raise FatalError(f"{context}: HTTP {status} lỗi xác thực/quyền; kiểm tra API key", status=status)
            raise FatalError(f"{context}: HTTP {status}; kiểm tra request/cấu hình", status=status)

    def _backoff(self, number, context) -> None:
        self.stats.retries_other += 1
        self._wait(_BACKOFF[min(number - 1, len(_BACKOFF) - 1)], "backoff_sec", f"retry {number}: {context}")

    def post_batch(self, arxiv_ids):
        def validate(data):
            if not isinstance(data, list):
                raise ResponseError("phản hồi batch phải là list")
            if len(data) != len(arxiv_ids):
                raise ResponseError(f"độ dài batch {len(data)} khác {len(arxiv_ids)} ID đã gửi")
            for aid, entry in zip(arxiv_ids, data):
                validate_batch_entry(entry, aid)
        return self._request("POST", f"{BASE}/batch", context=f"batch[{len(arxiv_ids)}]",
                             validator=validate, params={"fields": BATCH_FIELDS},
                             json={"ids": [f"ARXIV:{aid}" for aid in arxiv_ids]})

    def fetch_metadata(self, arxiv_id):
        """Lookup nhỏ lấy count cho batch quá lớn hoặc thiếu referenceCount."""
        def validate(data):
            if not isinstance(data, dict) or count_is_valid(data) is not True:
                raise ResponseError(f"{arxiv_id}: metadata thiếu/sai referenceCount")
        return self._request("GET", f"{BASE}/ARXIV:{arxiv_id}", context=f"{arxiv_id}/metadata",
                             validator=validate, params={"fields": "paperId,referenceCount"})

    def fetch_all_references(self, arxiv_id):
        total, out, offset, seen = ReqStats(), [], 0, {0}
        while True:
            data, stats = self._request(
                "GET", f"{BASE}/ARXIV:{arxiv_id}/references",
                context=f"{arxiv_id}/references offset={offset}",
                validator=lambda page: validate_page_data(page, arxiv_id, offset, seen),
                params={"fields": REF_FIELDS, "limit": PAGE_LIMIT, "offset": offset})
            total.add(stats)
            out.extend(item["citedPaper"] or {} for item in data["data"])
            next_offset = data.get("next")
            if next_offset is None:
                return out, total
            seen.add(next_offset)
            offset = next_offset
