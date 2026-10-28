"""Semantic Scholar Global Rate Limiter.

Implements a thread-safe, singleton-capable strict rate limiter that enforces
the Semantic Scholar public API constraint:

    "1 request per second, cumulative across all endpoints."

Key design decisions:
- Uses a Safety Buffer of 1.1s (instead of exactly 1.0s) to absorb network
  jitter, ensuring the server never receives 2 requests in the same second.
- Thread-safe via threading.Lock(), preventing race conditions in multi-threaded
  crawling scenarios.
- Global Circuit Breaker: when any endpoint receives HTTP 429, the entire limiter
  enters a cooldown state, blocking ALL further requests until the cooldown expires.
  This prevents the common mistake of retrying aggressively during a rate-limit window.
- End-to-Start gap enforcement: ``interval`` is measured from the moment the
  *previous response finished* (not from when the request was *sent*), so a slow
  batch request that takes 15 s will not cause the next request to fire immediately.
  Call ``notify_request_end()`` in a ``finally`` block after every HTTP attempt.
"""

from __future__ import annotations

import math
import logging
import threading
import time

logger = logging.getLogger(__name__)

# Default interval between consecutive requests (seconds).
# 1.1s adds a ~100ms safety buffer above the required 1.0s minimum to absorb
# network jitter without sacrificing meaningful throughput.
DEFAULT_INTERVAL: float = 1.1

# Minimum interval we enforce even if the user sets an aggressive value.
HARD_MIN_INTERVAL: float = 1.01


class SemanticScholarRateLimiter:
    """Thread-safe, strict rate limiter for the Semantic Scholar API.

    All requests to Semantic Scholar (across all endpoints and all methods)
    must call ``acquire()`` before sending, and ``notify_request_end()`` in a
    ``finally`` block after each attempt finishes (success *or* error).
    On HTTP 429, also call ``notify_rate_limit(retry_after)`` before continuing.

    The ``interval`` is enforced as an **End-to-Start calm gap**: the next
    ``acquire()`` will sleep until at least ``interval`` seconds have passed
    *since the last response was received* (tracked by ``notify_request_end()``).
    This correctly handles slow batch requests that themselves take longer than
    ``interval`` to complete.

    Attributes:
        interval: Minimum seconds to wait *after* each response before the next
            request is sent. Must be >= HARD_MIN_INTERVAL (1.01s). Default 1.1s.
    """

    def __init__(self, interval: float = DEFAULT_INTERVAL) -> None:
        self.interval: float = max(HARD_MIN_INTERVAL, float(interval))
        self._lock: threading.Lock = threading.Lock()
        self.request_lock = threading.Lock()
        self._last_request_time: float = 0.0   # when acquire() last released
        self._last_response_time: float = 0.0  # when notify_request_end() last called
        self._cooldown_until: float = 0.0

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def acquire(self) -> None:
        """Blocks the calling thread until it is safe to send a request.

        Three scenarios are handled in order:
        1. Global Cooldown (triggered by HTTP 429): sleep until cooldown expires.
        2. End-to-Start gap: sleep until ``interval`` seconds have elapsed
           since the last *response* finished (``_last_response_time``).
        3. Start-to-Start gap: as a safety net, also enforce ``interval``
           since the last *request start* (``_last_request_time``).

        The state lock spaces grants. Clients additionally hold request_lock
        through each HTTP call to prevent overlapping requests.
        """
        # Recheck the deadline after sleeping: some clocks/schedulers wake early.
        # Release the state lock while waiting so a concurrent 429 can extend it.
        while True:
            with self._lock:
                now = time.monotonic()
                deadline = max(
                    self._cooldown_until,
                    self._last_request_time + self.interval if self._last_request_time else 0.0,
                    self._last_response_time + self.interval if self._last_response_time else 0.0,
                )
                remaining = deadline - now
                if remaining <= 0:
                    self._last_request_time = now
                    return
            time.sleep(min(60.0, remaining))

    def notify_request_end(self) -> None:
        """Records the moment the current HTTP attempt finished (success or error).

        **Must** be called in a ``finally`` block after every HTTP attempt so
        that the End-to-Start gap is measured from when the response was fully
        received, not from when the request was sent.

        Thread-safe — uses the internal lock.
        """
        with self._lock:
            self._last_response_time = time.monotonic()

    def notify_rate_limit(self, retry_after: float | None = None) -> None:
        """Activates a global cooldown after receiving HTTP 429.

        Called immediately when ANY endpoint returns HTTP 429. The cooldown
        blocks ALL subsequent ``acquire()`` calls until it expires, preventing
        the system from hammering Semantic Scholar during a rate-limit window.

        Args:
            retry_after: The number of seconds to wait, typically parsed from
                the ``Retry-After`` response header. If ``None`` or 0, a
                conservative default of 60 seconds is used.
        """
        with self._lock:
            wait_seconds = float(retry_after) if retry_after is not None else 60.0
            if not math.isfinite(wait_seconds) or wait_seconds < 0:
                wait_seconds = 60.0
            # Server deadlines must never be shortened by a local upper bound.
            wait_seconds = max(30.0, wait_seconds)
            self._cooldown_until = max(self._cooldown_until, time.monotonic() + wait_seconds)
            # Reset both timing markers so after cooldown we get a fresh start
            self._last_request_time = self._cooldown_until
            self._last_response_time = self._cooldown_until
            logger.debug(
                "RateLimiter: HTTP 429 received. Global cooldown set for %.0fs "
                "(until %.2f). All requests are now blocked.",
                wait_seconds,
                self._cooldown_until,
            )

    def defer(self, seconds: float) -> None:
        """Respect a server Retry-After deadline across all shared clients."""
        if math.isfinite(seconds) and seconds >= 0:
            with self._lock:
                self._cooldown_until = max(self._cooldown_until, time.monotonic() + seconds)

    def notify_success(self) -> None:
        """Signals a successful response. No-op; reserved for future adaptive logic."""
        pass

    def cooldown_remaining(self) -> float:
        """Returns remaining cooldown seconds (0.0 if not in cooldown)."""
        remaining = self._cooldown_until - time.monotonic()
        return max(0.0, remaining)

    def is_in_cooldown(self) -> bool:
        """Returns True if the limiter is currently in a global cooldown state."""
        return self.cooldown_remaining() > 0


# ---------------------------------------------------------------------------
# Module-level singleton
# ---------------------------------------------------------------------------

#: Default singleton instance, shared across all modules that import it.
#: Enforces "cumulative across all endpoints" by design — all callers
#: share the same object and therefore the same rate limit window.
default_rate_limiter = SemanticScholarRateLimiter()
