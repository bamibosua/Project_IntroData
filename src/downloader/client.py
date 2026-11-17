"""Thread-safe HTTP client: Keep-Alive pooling, global rate slot, adaptive cooldown."""

from __future__ import annotations

import math
import logging
from datetime import datetime, timezone
from email.utils import parsedate_to_datetime
import threading
import time

import requests
from requests.adapters import HTTPAdapter

from .config import (DEFAULT_DELAY, MAX_403_ATTEMPTS, MAX_ADAPTIVE_DELAY, MAX_ARCHIVE_BYTES, MAX_FETCH_ATTEMPTS,
                     MAX_RETRY_AFTER_SEC, SRC_ENDPOINTS, USER_AGENT)
from .utils import looks_like_html

_NOT_FOUND_PAYLOAD = b"%PDF-1.4 (404 not found)"
logger = logging.getLogger(__name__)


class ArchiveTooLarge(Exception):
    """Raw archive exceeds MAX_ARCHIVE_BYTES; not retried."""


class BlockedError(RuntimeError):
    """HTTP 403 persisted: the server is refusing us (blocked?). Not retried; the runner counts these."""


class _BadPayload(Exception):
    """HTTP 200 with an unusable body (empty / HTML error page / truncated); retried."""


def _retry_after_seconds(value):
    if not value:
        return 0.0
    try:
        if value.strip().isdecimal():
            seconds = float(value)
            return min(seconds, MAX_RETRY_AFTER_SEC) if math.isfinite(seconds) else 0.0
        date = parsedate_to_datetime(value)
        if date.tzinfo is None:
            date = date.replace(tzinfo=timezone.utc)
        return min(MAX_RETRY_AFTER_SEC, max(0.0, (date - datetime.now(timezone.utc)).total_seconds()))
    except (ValueError, TypeError, OverflowError):
        return 0.0


class FastArxivClient:
    """Download raw source archives of arXiv versions."""

    def __init__(self, delay: float = DEFAULT_DELAY, timeout: float = 40.0, *, shared_cooldown=None, stop_event=None,
                 contact_email: str | None = None):
        self.user_agent = f"{USER_AGENT} mailto:{contact_email}" if contact_email else USER_AGENT
        self.delay = delay
        self.base_delay = delay  # adaptive back-off never goes below this
        self.timeout = timeout
        self.shared_cooldown = shared_cooldown
        self.stop_event = stop_event
        self._ok_streak = 0
        self._last_req_ts = None
        self._rate_lock = threading.Lock()
        self._cooldown_lock = threading.Lock()
        self._cooldown_until = 0.0
        self._counter = 0
        self._thread_local = threading.local()

    # ---- session handling -------------------------------------------------------
    def _get_session(self):
        sess = getattr(self._thread_local, "session", None)
        if sess is not None:
            return sess
        session = requests.Session()
        adapter = HTTPAdapter(pool_connections=2, pool_maxsize=2)  # one connection per thread is enough
        session.mount("https://", adapter)
        session.mount("http://", adapter)
        session.headers.update({
            "User-Agent": self.user_agent,
            "Accept": "application/x-eprint-tar, application/x-eprint, application/gzip, */*",
            "Connection": "keep-alive",
        })
        self._thread_local.session = session
        return session

    # ---- rate limiting ----------------------------------------------------------
    def _sleep(self, seconds):
        if self.stop_event is None:
            time.sleep(seconds)
        elif self.stop_event.wait(seconds):
            raise InterruptedError('Pipeline stopped.')

    def _note_throttle(self, attempt: int, retry_after=None, *, status=429) -> float:
        """Keep the client cooldown without cascading delay; publish HTTP 429 to the shared deadline."""
        with self._cooldown_lock:
            now = time.monotonic()
            already_cooling = now < self._cooldown_until
            duration = max(min(25.0, 10.0 + 5.0 * attempt),
                           _retry_after_seconds(retry_after) if status == 429 else 0.0)
            self._cooldown_until = max(self._cooldown_until, now + duration)
            if not already_cooling:
                # Raise the spacing once per burst; it never drops below the configured base delay.
                ceiling = max(MAX_ADAPTIVE_DELAY, self.base_delay * 2)
                self.delay = min(ceiling, max(self.delay * 1.25, self.base_delay))
            self._ok_streak = 0
        if status == 429 and self.shared_cooldown is not None:
            self.shared_cooldown.defer(duration)
        return duration

    def _note_success(self) -> None:
        """After 25 clean requests in a row, relax the spacing back toward the configured delay."""
        with self._cooldown_lock:
            self._ok_streak += 1
            if self._ok_streak >= 25 and self.delay > self.base_delay:
                self.delay = max(self.base_delay, self.delay * 0.8)
                self._ok_streak = 0

    def _wait_rate_slot(self) -> int:
        while True:
            if self.stop_event is not None and self.stop_event.is_set():
                raise InterruptedError('Pipeline stopped.')
            with self._rate_lock:
                now = time.monotonic()
                with self._cooldown_lock:
                    pacing = self.delay - (now - self._last_req_ts) if self._last_req_ts is not None else 0.0
                    wait = max(0.0, self._cooldown_until - now, pacing)
                if self.shared_cooldown is not None:
                    wait = max(wait, self.shared_cooldown.remaining())
                if wait <= 0:
                    idx = self._counter
                    self._counter += 1
                    self._last_req_ts = now
                    return idx
            self._sleep(min(wait, 2.0))

    # ---- download ---------------------------------------------------------------
    def _read_body(self, resp) -> bytes:
        """Read a 200 response; validate it is a usable payload (not empty/HTML/truncated)."""
        declared = resp.headers.get("Content-Length")
        declared = int(declared) if declared and declared.isdigit() else None
        if declared is not None and declared > MAX_ARCHIVE_BYTES:
            raise ArchiveTooLarge(f"Content-Length {declared} > {MAX_ARCHIVE_BYTES}")
        chunks = resp.iter_content(65536)
        first = next(chunks, b"")
        if first.startswith(b"%PDF-"):
            # PDF-only submission: abort now, no need to pull the whole PDF.
            return first
        data, size = [first], len(first)
        for chunk in chunks:
            size += len(chunk)
            if size > MAX_ARCHIVE_BYTES:
                raise ArchiveTooLarge(f"archive exceeds {MAX_ARCHIVE_BYTES} bytes")
            data.append(chunk)
        body = b"".join(data)
        if not body:
            raise _BadPayload("empty body")
        if looks_like_html(body):
            raise _BadPayload("HTML error page instead of source")
        # Compare only when the body is not content-encoded (requests decodes gzip/deflate transparently).
        if declared is not None and not resp.headers.get("Content-Encoding") and len(body) != declared:
            raise _BadPayload(f"truncated download ({len(body)}/{declared} bytes)")
        return body

    def _drop_session(self) -> None:
        sess = getattr(self._thread_local, "session", None)
        self._thread_local.session = None
        if sess is not None:
            try:
                sess.close()
            except Exception:
                pass

    def fetch_version(self, version_id: str) -> bytes:
        """Download raw archive bytes for a version (e.g. '2608.09608v1')."""
        req_timeout = (10.0, self.timeout)
        for attempt in range(1, MAX_FETCH_ATTEMPTS + 1):
            self._wait_rate_slot()
            # Primary endpoint first; the alternate one is only a fallback on retries (it shares the same host
            # and limit, so alternating does not add capacity).
            url = f"{SRC_ENDPOINTS[(attempt - 1) % len(SRC_ENDPOINTS)]}/{version_id}"
            session = self._get_session()
            try:
                resp = session.get(url, timeout=req_timeout, stream=True)
                try:
                    if resp.status_code == 200:
                        data = self._read_body(resp)
                        self._note_success()
                        return data
                    status = resp.status_code
                    retry_after = resp.headers.get('Retry-After')
                finally:
                    resp.close()
                if status in (429, 403):
                    if status == 403 and attempt >= MAX_403_ATTEMPTS:
                        raise BlockedError(f"{version_id}: HTTP 403 Forbidden (possibly blocked); giving up")
                    duration = self._note_throttle(attempt, retry_after, status=status)
                    logger.warning('[DOWNLOAD] %s: HTTP %d; wait %.0fs, delay=%.2fs (attempt %d/%d).',
                                   version_id, status, duration, self.delay, attempt, MAX_FETCH_ATTEMPTS)
                    self._sleep(1.0)
                    continue
                if status == 404:
                    logger.warning('[DOWNLOAD] %s: HTTP 404 (Not Found).', version_id)
                    return _NOT_FOUND_PAYLOAD
                wait_s = min(5.0, 1.0 * attempt)
                logger.warning('[DOWNLOAD] %s: HTTP %d; retrying in %.0fs (attempt %d/%d).',
                               version_id, status, wait_s, attempt, MAX_FETCH_ATTEMPTS)
                self._sleep(wait_s)
            except (ArchiveTooLarge, InterruptedError):
                raise
            except _BadPayload as exc:
                # Bad body but healthy connection: keep the TLS session, just retry.
                wait_s = min(5.0, 1.0 * attempt)
                logger.warning('[DOWNLOAD] %s: bad payload (%s); retrying in %.0fs (attempt %d/%d).',
                               version_id, exc, wait_s, attempt, MAX_FETCH_ATTEMPTS)
                self._sleep(wait_s)
            except BlockedError:
                raise
            except Exception as exc:
                if self.stop_event is not None and self.stop_event.is_set():
                    raise
                wait_s = min(5.0, 1.0 * attempt)
                logger.warning('[DOWNLOAD] %s: %s (%s); retrying in %.0fs (attempt %d/%d).',
                               version_id, type(exc).__name__, exc, wait_s, attempt, MAX_FETCH_ATTEMPTS)
                self._drop_session()
                self._sleep(wait_s)

        raise RuntimeError(f"Failed to download {version_id} after {MAX_FETCH_ATTEMPTS} attempts")
