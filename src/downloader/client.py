"""Thread-safe HTTP client: Keep-Alive pooling, global rate slot, adaptive cooldown."""

from __future__ import annotations

import random
import math
from datetime import datetime, timezone
from email.utils import parsedate_to_datetime
import threading
import time
import urllib.error
import urllib.request

from .config import MAX_FETCH_ATTEMPTS, SRC_ENDPOINTS, USER_AGENTS

_NOT_FOUND_PAYLOAD = b"%PDF-1.4 (404 not found)"


def _retry_after_seconds(value):
    if not value:
        return 0.0
    try:
        if value.strip().isdecimal():
            seconds = float(value)
            return seconds if math.isfinite(seconds) else 0.0
        date = parsedate_to_datetime(value)
        if date.tzinfo is None:
            date = date.replace(tzinfo=timezone.utc)
        return max(0.0, (date - datetime.now(timezone.utc)).total_seconds())
    except (ValueError, TypeError, OverflowError):
        return 0.0


class FastArxivClient:
    """Download raw source archives of arXiv versions."""

    def __init__(self, delay: float = 0.6, timeout: float = 40.0, *, shared_cooldown=None):
        self.delay = delay
        self.base_delay = delay  # adaptive back-off never goes below this
        self.timeout = timeout
        self.shared_cooldown = shared_cooldown
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
        try:
            import requests  # type: ignore
            from requests.adapters import HTTPAdapter  # type: ignore

            session = requests.Session()
            adapter = HTTPAdapter(pool_connections=4, pool_maxsize=32)
            session.mount("https://", adapter)
            session.mount("http://", adapter)
            session.headers.update({
                "User-Agent": random.choice(USER_AGENTS),
                "Accept": "application/x-eprint-tar, application/x-eprint, application/gzip, */*",
                "Connection": "keep-alive",
            })
            self._thread_local.session = session
            return session
        except Exception:
            return None

    # ---- rate limiting ----------------------------------------------------------
    def _note_throttle(self, attempt: int, retry_after=None, *, status=429) -> None:
        """Keep the client cooldown; publish HTTP 429 to the shared deadline."""
        with self._cooldown_lock:
            duration = max(min(15.0, 3.0 * attempt),
                           _retry_after_seconds(retry_after) if status == 429 else 0.0)
            self._cooldown_until = max(self._cooldown_until, time.monotonic() + duration)
            self.delay = min(5.0, max(self.delay * 1.5, 0.5))
            self._ok_streak = 0
        if status == 429 and self.shared_cooldown is not None:
            self.shared_cooldown.defer(duration)

    def _note_success(self) -> None:
        """After 100 clean requests in a row, relax the spacing back toward the configured delay."""
        with self._cooldown_lock:
            self._ok_streak += 1
            if self._ok_streak >= 100 and self.delay > self.base_delay:
                self.delay = max(self.base_delay, self.delay * 0.8)
                self._ok_streak = 0

    def _wait_rate_slot(self) -> int:
        while True:
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
            time.sleep(min(wait, 2.0))

    # ---- download ---------------------------------------------------------------
    def fetch_version(self, version_id: str) -> bytes:
        """Download raw archive bytes for a version (e.g. '2608.09608v1')."""
        req_timeout = (10.0, self.timeout)
        for attempt in range(1, MAX_FETCH_ATTEMPTS + 1):
            slot_idx = self._wait_rate_slot()
            url = f"{SRC_ENDPOINTS[slot_idx % len(SRC_ENDPOINTS)]}/{version_id}"

            session = self._get_session()
            if session is not None:
                try:
                    resp = session.get(url, timeout=req_timeout, stream=True)
                    if resp.status_code == 200:
                        chunks = resp.iter_content(65536)
                        first = next(chunks, b"")
                        if first.startswith(b"%PDF-"):
                            # PDF-only submission: abort now, no need to pull the whole PDF.
                            resp.close()
                            self._note_success()
                            return first
                        data = b"".join([first, *chunks])
                        self._note_success()
                        return data
                    resp.close()
                    if resp.status_code in (429, 403):
                        self._note_throttle(attempt, resp.headers.get('Retry-After'), status=resp.status_code)
                        time.sleep(1.0)
                        continue
                    if resp.status_code == 404:
                        return _NOT_FOUND_PAYLOAD
                except Exception:
                    self._thread_local.session = None
                    time.sleep(min(5.0, 1.0 * attempt))
                    continue

            # A fallback is another HTTP request and needs its own rate slot.
            if session is not None:
                slot_idx = self._wait_rate_slot()
                url = f"{SRC_ENDPOINTS[slot_idx % len(SRC_ENDPOINTS)]}/{version_id}"
            # Fallback to urllib when `requests` is unavailable
            req = urllib.request.Request(url, headers={"User-Agent": random.choice(USER_AGENTS)})
            try:
                with urllib.request.urlopen(req, timeout=self.timeout) as resp:
                    return resp.read()
            except urllib.error.HTTPError as exc:
                if exc.code in (429, 403):
                    self._note_throttle(attempt, exc.headers.get('Retry-After') if exc.headers else None,
                                        status=exc.code)
                    continue
                if exc.code == 404:
                    return _NOT_FOUND_PAYLOAD
            except Exception:
                time.sleep(min(5.0, 1.0 * attempt))

        raise RuntimeError(f"Failed to download {version_id} after {MAX_FETCH_ATTEMPTS} attempts")
