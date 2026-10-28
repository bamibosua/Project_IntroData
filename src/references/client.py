"""Semantic Scholar API Client.

Handles HTTP requests to the Semantic Scholar Academic Graph API with:
- Centralized _send_request() ensuring every call passes through the global
  rate limiter (enforcing "1 req/s cumulative across all endpoints").
- Exponential backoff with Global Cooldown on HTTP 429.
- Optional API key support (via constructor or S2_API_KEY env var).
- Single-paper endpoint: GET /graph/v1/paper/arXiv:{id}
- Batch endpoint:        POST /graph/v1/paper/batch  (up to 500 papers/request)
"""

from __future__ import annotations

import json
import logging
import math
import random
from datetime import timezone
from email.utils import parsedate_to_datetime
import os
import time
import urllib.error
import urllib.parse
import urllib.request
from typing import Any

from .rate_limiter import SemanticScholarRateLimiter, default_rate_limiter

logger = logging.getLogger(__name__)

GRAPH_API_BASE = "https://api.semanticscholar.org/graph/v1/paper"

# Fields requested for both single-paper and batch endpoints
_PAPER_REFERENCE_FIELDS = "paperId,title,authors,publicationDate,externalIds"
_REFERENCE_FIELDS = "referenceCount," + ",".join(
    "references." + field for field in _PAPER_REFERENCE_FIELDS.split(",")
)


# Maximum IDs per batch request (Semantic Scholar hard limit: 500)
BATCH_MAX_IDS = 500


class RateLimitException(Exception):
    """Raised when rate limit is exceeded and all retries are exhausted."""
    pass


def _parse_retry_after(headers: Any) -> float | None:
    """Extracts the Retry-After value from an HTTP response header object.

    Args:
        headers: The header object from an HTTPError or urlopen response.

    Returns:
        The retry delay in seconds, or None if the header is absent or invalid.
    """
    if headers is None:
        return None
    retry_after = headers.get("Retry-After")
    if retry_after is None:
        return None
    try:
        delay = float(retry_after)
    except (ValueError, TypeError):
        try:
            date = parsedate_to_datetime(retry_after)
            if date.tzinfo is None:
                date = date.replace(tzinfo=timezone.utc)
            delay = max(0.0, date.timestamp() - time.time())
        except (ValueError, TypeError, OverflowError):
            return None
    return delay if math.isfinite(delay) and delay >= 0 else None


def validate_references(refs: Any) -> None:
    """Allow unavailable metadata (null), reject malformed payloads."""
    if not isinstance(refs, list):
        raise ValueError("references must be an array")
    for ref in refs:
        if ref is None:
            continue
        if not isinstance(ref, dict):
            raise ValueError("reference must be an object or null")
        for field in ("paperId", "title", "publicationDate"):
            if ref.get(field) is not None and not isinstance(ref[field], str):
                raise ValueError(f"Invalid reference {field}")
        ext = ref.get("externalIds")
        if ext is not None and not isinstance(ext, dict):
            raise ValueError("Invalid externalIds")
        if isinstance(ext, dict) and any(
            key.lower() == "arxiv" and value is not None and not isinstance(value, str)
            for key, value in ext.items()
        ):
            raise ValueError("Invalid ArXiv identifier")
        authors = ref.get("authors")
        if authors is not None and (not isinstance(authors, list) or any(
            not isinstance(author, dict) or
            (author.get("name") is not None and not isinstance(author["name"], str))
            for author in authors
        )):
            raise ValueError("Invalid authors")



class SemanticScholarClient:
    """Client for querying the Semantic Scholar Graph API.

    Supports both single-paper and batch (up to 500) requests, with all HTTP
    calls routed through a shared ``SemanticScholarRateLimiter`` to comply
    with the "1 request per second cumulative across all endpoints" constraint.

    Attributes:
        api_key: Optional Semantic Scholar API key. When provided, requests
            include the ``x-api-key`` header. Key can also be supplied via the
            ``S2_API_KEY`` environment variable.
        max_retries: Maximum number of retry attempts on HTTP 429 or 5xx errors.
        timeout: HTTP request timeout in seconds.
        user_agent: User-Agent string sent with every request.
        limiter: The :class:`SemanticScholarRateLimiter` instance controlling
            request pacing. Defaults to the module-level singleton so that all
            client instances share the same rate-limit window — enforcing the
            "cumulative across all endpoints" policy.
    """

    def __init__(
        self,
        api_key: str | None = None,
        max_retries: int = 2,
        timeout: float = 60.0,
        user_agent: str | None = None,
        limiter: SemanticScholarRateLimiter | None = None,
        delay: float | None = None,
        base_backoff: float | None = None,
        **kwargs: Any,
    ) -> None:
        self.api_key: str | None = api_key or os.environ.get("S2_API_KEY")
        self.max_retries: int = max(0, int(max_retries))
        self.timeout: float = float(timeout)
        self.base_backoff: float = float(base_backoff) if base_backoff is not None else 2.0
        self.user_agent: str = (
            user_agent
            or "HCMUS-IntroData-ReferencesCrawler/2.0 "
               "(academic research; contact: student@hcmus.edu.vn)"
        )
        if limiter is not None:
            self.limiter = limiter
        elif delay is not None:
            self.limiter = SemanticScholarRateLimiter(interval=delay)
        else:
            self.limiter = default_rate_limiter
        # Running counter of HTTP attempts made (across all endpoints).
        self._request_count: int = 0

        if self.api_key:
            logger.info(
                "SemanticScholarClient: API key loaded.",
            )
        else:
            logger.info(
                "SemanticScholarClient: Running in public mode (no API key). "
                "Rate limit enforced at %.2fs interval.",
                self.limiter.interval,
            )

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    @staticmethod
    def _mask_key(key: str) -> str:
        """Returns a masked version of the API key for safe terminal display.

        Shows the first 4 and last 4 characters separated by '***', e.g.:
            ``s2k-***Lec`` — enough to verify *which* key is active without
            exposing the full secret.
        """
        if len(key) <= 8:
            return "***"
        return f"{key[:4]}***{key[-4:]}"

    def retry_delay(self, attempt: int) -> float:
        """Bounded exponential backoff with additive jitter."""
        base = min(60.0, self.base_backoff * 2 ** min(attempt, 10))
        return min(60.0, base + random.uniform(0.0, min(1.0, base * 0.25)))

    def _build_headers(self) -> dict[str, str]:
        """Constructs HTTP headers common to every request."""
        headers: dict[str, str] = {
            "User-Agent": self.user_agent,
            "Accept": "application/json",
        }
        if self.api_key:
            headers["x-api-key"] = self.api_key
        return headers

    def _send_request(self, *args: Any, **kwargs: Any) -> dict[str, Any] | list[Any]:
        # Shared by clients using the same limiter; includes response-end updates.
        with self.limiter.request_lock:
            return self._send_request_locked(*args, **kwargs)

    def _send_request_locked(
        self,
        url: str,
        *,
        method: str = "GET",
        body: bytes | None = None,
        extra_headers: dict[str, str] | None = None,
        max_retries: int | None = None,
        retry_info: str | None = None,
    ) -> dict[str, Any] | list[Any]:
        """Sends an HTTP request to Semantic Scholar with rate limiting and retry logic.

        This is the single HTTP gateway for ALL Semantic Scholar calls.
        It ensures:
        1. ``limiter.acquire()`` is called before every attempt (enforcing 1 req/s).
        2. On HTTP 429, ``limiter.notify_rate_limit()`` broadcasts a global cooldown.
        3. Exponential backoff on transient 5xx errors.

        Args:
            url: Full request URL.
            method: HTTP method ("GET" or "POST").
            body: Optional encoded request body bytes (for POST requests).
            extra_headers: Optional extra headers to merge in.
            max_retries: Optional override for retry count. If None, uses ``self.max_retries``.
            retry_info: Optional custom retry label (e.g. "Retry 1/2").

        Returns:
            Parsed JSON response as a dict or list.

        Raises:
            RateLimitException: If rate limit is still exceeded after all retries.
            urllib.error.HTTPError: For non-retriable HTTP errors (4xx other than 429).
            urllib.error.URLError: For network-level errors after all retries.
        """
        headers = self._build_headers()
        if extra_headers:
            headers.update(extra_headers)

        effective_retries = self.max_retries if max_retries is None else max(0, int(max_retries))
        last_exc: Exception | None = None

        for attempt in range(effective_retries + 1):
            # ─── RATE LIMIT: Must acquire permission before every request ───
            _t_before_acquire = time.monotonic()
            self.limiter.acquire()
            _pacing_waited = time.monotonic() - _t_before_acquire

            self._request_count += 1
            # ─── TRANSPARENT REQUEST LOG ──────────────────────────────────
            # Shows request number, method, authentication status (no key content),
            # and how long the rate limiter actually paused before firing.
            _key_status = (
                "Key: Active"
                if self.api_key
                else "Key: None (Public mode)"
            )
            if retry_info:
                _retry_display = f" | {retry_info}"
            elif attempt > 0:
                _retry_display = f" | Retry {attempt}/{effective_retries}"
            else:
                _retry_display = ""

            logger.info(
                "[Request #%d] %s %s | %s | Paced: %.2fs%s",
                self._request_count,
                method,
                url.split("?")[0],   # URL without query params (cleaner)
                _key_status,
                _pacing_waited,
                _retry_display,
            )

            req = urllib.request.Request(url, data=body, headers=headers, method=method)

            try:
                try:
                    with urllib.request.urlopen(req, timeout=self.timeout) as resp:
                        raw = resp.read()
                        result = json.loads(raw.decode("utf-8"))
                        self.limiter.notify_success()
                        return result

                except urllib.error.HTTPError as exc:
                    last_exc = exc
                    retry_after = _parse_retry_after(exc.headers)
                    if retry_after is not None and exc.code != 429:
                        self.limiter.defer(retry_after)

                    # ── HTTP 404: Paper not indexed, non-retriable ──────────────
                    if exc.code == 404:
                        raise

                    # ── HTTP 429: Rate limit exceeded ───────────────────────────
                    if exc.code == 429:
                        # Activate GLOBAL cooldown — blocks all further requests
                        self.limiter.notify_rate_limit(retry_after)
                        wait = max(30.0, retry_after) if retry_after is not None else 60.0
                        print(f"⚠️  [HTTP 429] Cooldown {wait:.0f}s", flush=True)

                        logger.debug(
                            "HTTP 429 for %s (attempt %d/%d). Global cooldown %.0fs.",
                            url, attempt + 1, effective_retries + 1, wait,
                        )
                        if attempt >= effective_retries:
                            raise RateLimitException(
                                f"HTTP 429: Rate limit not resolved after {effective_retries + 1} attempts."
                            ) from exc
                        continue  # limiter.acquire() will sleep through cooldown on next loop

                    # ── HTTP 5xx: Transient server error ───────────────────────
                    if 500 <= exc.code < 600:
                        sleep_seconds = self.retry_delay(attempt)
                        print(f"⚠️  [HTTP {exc.code}] Cooldown {sleep_seconds:.0f}s", flush=True)

                        logger.debug(
                            "HTTP %d server error (attempt %d/%d). Retrying in %.1fs...",
                            exc.code, attempt + 1, effective_retries + 1, sleep_seconds,
                        )
                        if attempt < effective_retries:
                            time.sleep(sleep_seconds)
                            continue
                        raise

                    # ── Other 4xx: Non-retriable ────────────────────────────────
                    raise

                except urllib.error.HTTPError:
                    raise
                except (urllib.error.URLError, TimeoutError, OSError, ValueError) as exc:
                    last_exc = exc
                    sleep_seconds = self.retry_delay(attempt)
                    logger.debug(
                        "Network error (%s) on attempt %d/%d. Retrying in %.1fs...",
                        exc, attempt + 1, effective_retries + 1, sleep_seconds,
                    )
                    if attempt < effective_retries:
                        time.sleep(sleep_seconds)
                        continue
                    raise

            finally:
                # Always record response-end time so the rate limiter enforces
                # an End-to-Start gap from when the response *finished*, not
                # from when the request was sent.
                self.limiter.notify_request_end()

        # Should not normally reach here; last_exc re-raised for clarity
        if last_exc:
            raise last_exc
        raise RuntimeError("Unexpected exit from _send_request retry loop.")

    # ------------------------------------------------------------------
    # Public endpoints
    # ------------------------------------------------------------------

    def _complete_references(self, arxiv_id: str, paper: Any,
                             max_retries: int | None = None) -> dict[str, Any]:
        if not isinstance(paper, dict):
            raise ValueError("Paper must be an object")
        validate_references(paper.get("references"))
        count = paper.get("referenceCount")
        if count is not None and (type(count) is not int or count < 0):
            raise ValueError("Invalid referenceCount")
        refs = paper["references"]
        if count is not None and count == len(refs):
            return paper
        # Missing count or a mismatch cannot establish embedded-list completeness.
        refs = []
        offset = 0
        while True:
            query = urllib.parse.urlencode({"fields": _PAPER_REFERENCE_FIELDS,
                                            "limit": 1000, "offset": offset})
            encoded_id = urllib.parse.quote("arXiv:" + arxiv_id, safe=":")
            url = f"{GRAPH_API_BASE}/{encoded_id}/references?{query}"
            try:
                page = self._send_request(url, max_retries=max_retries)
            except urllib.error.HTTPError as exc:
                if exc.code == 404:
                    raise ValueError("References endpoint unavailable for an existing paper") from exc
                raise
            if not isinstance(page, dict) or not isinstance(page.get("data"), list):
                raise ValueError("Invalid references page")
            if type(page.get("offset")) is not int or page["offset"] != offset:
                raise ValueError("Unexpected references page offset")
            rows = page["data"]
            if any(not isinstance(row, dict) or "citedPaper" not in row for row in rows):
                raise ValueError("Invalid reference page entry")
            cited = [row["citedPaper"] for row in rows]
            validate_references(cited)
            refs.extend(cited)
            next_offset = page.get("next")
            if next_offset is None:
                break
            if type(next_offset) is not int or next_offset != offset + len(rows) or not rows:
                raise ValueError("Invalid references pagination cursor")
            offset = next_offset
        if count is not None and len(refs) != count:
            raise ValueError("Reference count changed or pagination is incomplete")
        return dict(paper, references=refs)

    def fetch_paper_references(self, arxiv_id: str) -> dict[str, Any]:
        """Fetches metadata and references for a single ArXiv paper.

        Uses: GET /graph/v1/paper/arXiv:{id}?fields=references.*

        Args:
            arxiv_id: The ArXiv identifier (e.g. '2106.09685' or '1706.03762').

        Returns:
            A result envelope dict::

                {
                    "status": "found" | "not_found" | "error",
                    "arxiv_id": str,
                    "data": dict | None,
                    "error": str | None,
                }
        """
        clean_id = arxiv_id.strip()
        query = urllib.parse.urlencode({"fields": _REFERENCE_FIELDS})
        encoded_id = urllib.parse.quote("arXiv:" + clean_id, safe=":")
        url = f"{GRAPH_API_BASE}/{encoded_id}?{query}"

        try:
            data = self._send_request(url, method="GET")
            data = self._complete_references(clean_id, data)
            return {"status": "found", "arxiv_id": clean_id, "data": data, "error": None}

        except urllib.error.HTTPError as exc:
            if exc.code == 404:
                return {
                    "status": "not_found",
                    "arxiv_id": clean_id,
                    "data": None,
                    "error": "HTTP 404: Paper not found in Semantic Scholar",
                }
            return {
                "status": "error",
                "arxiv_id": clean_id,
                "data": None,
                "error": f"HTTP {exc.code}: {exc.reason}", "http_status": exc.code,
                "retry_after": _parse_retry_after(exc.headers),
            }

        except RateLimitException as exc:
            return {"status": "error", "arxiv_id": clean_id, "data": None,
                    "error": str(exc), "http_status": 429,
                    "retry_after": _parse_retry_after(getattr(exc.__cause__, "headers", None))}

        except (urllib.error.URLError, TimeoutError, OSError, ValueError) as exc:
            return {
                "status": "error",
                "arxiv_id": clean_id,
                "data": None,
                "error": f"Invalid response: {exc}" if isinstance(exc, ValueError) else f"Network error: {exc}",
            }

    def fetch_papers_batch(
        self,
        arxiv_ids: list[str],
        retry_info: str | None = None,
    ) -> dict[str, dict[str, Any]]:
        """Fetches references for up to 500 ArXiv papers in a single POST request.

        Uses: POST /graph/v1/paper/batch?fields=references.*

        This is dramatically more efficient than calling :meth:`fetch_paper_references`
        in a loop — 500 papers cost only 1 API request (1 second of rate-limit quota)
        instead of 500.

        Args:
            arxiv_ids: List of ArXiv IDs. Maximum 500 per call; larger inputs raise ValueError.
                Use ReferenceCrawler for larger lists.
            retry_info: Optional custom retry label (e.g. "Retry 1/2").

        Returns:
            A dict keyed by arxiv_id with the same envelope format as
            :meth:`fetch_paper_references`::

                {
                    "2106.09685": {"status": "found", "data": {...}, "error": None},
                    "9999.99999": {"status": "not_found", "data": None, "error": "..."},
                    ...
                }
        """
        if not arxiv_ids:
            return {}

        if len(arxiv_ids) > BATCH_MAX_IDS:
            raise ValueError(f"At most {BATCH_MAX_IDS} IDs per batch")
        chunk = arxiv_ids
        query = urllib.parse.urlencode({"fields": _REFERENCE_FIELDS})
        url = f"{GRAPH_API_BASE}/batch?{query}"
        payload = json.dumps({"ids": [f"arXiv:{aid.strip()}" for aid in chunk]}).encode("utf-8")

        results: dict[str, dict[str, Any]] = {}

        try:
            raw_list = self._send_request(
                url,
                method="POST",
                body=payload,
                extra_headers={"Content-Type": "application/json"},
                max_retries=0,
                retry_info=retry_info,
            )
            # The batch endpoint returns a JSON array in the same order as input ids.
            if not isinstance(raw_list, list) or len(raw_list) != len(chunk):
                raise ValueError("Batch response must be an array with one entry per requested ID")
            for aid, paper_data in zip(chunk, raw_list):
                clean_aid = aid.strip()
                if paper_data is None:
                    results[clean_aid] = {
                        "status": "not_found",
                        "arxiv_id": clean_aid,
                        "data": None,
                        "error": "HTTP 404: Paper not found in Semantic Scholar",
                    }
                else:
                    try:
                        complete = self._complete_references(clean_aid, paper_data, max_retries=0)
                        results[clean_aid] = {"status": "found", "arxiv_id": clean_aid,
                                              "data": complete, "error": None}
                    except (urllib.error.URLError, TimeoutError, OSError, ValueError, RateLimitException) as exc:
                        code = 429 if isinstance(exc, RateLimitException) else getattr(exc, "code", None)
                        results[clean_aid] = {"status": "error", "arxiv_id": clean_aid,
                            "data": None, "error": str(exc), "http_status": code,
                            "retry_after": _parse_retry_after(getattr(
                                exc.__cause__ if isinstance(exc, RateLimitException) else exc,
                                "headers", None))}
                        if code in (401, 403, 429):
                            # Keep subsequent complete embedded responses even if page fetching pauses.
                            for index in range(len(results), len(chunk)):
                                remaining = chunk[index].strip()
                                candidate = raw_list[index]
                                if candidate is None:
                                    results[remaining] = {"status": "not_found", "arxiv_id": remaining,
                                                          "data": None, "error": "Paper not found"}
                                    continue
                                try:
                                    if not isinstance(candidate, dict):
                                        raise ValueError("Invalid paper")
                                    validate_references(candidate.get("references"))
                                    count = candidate.get("referenceCount")
                                    if type(count) is not int or count != len(candidate["references"]):
                                        raise ValueError("Requires pagination")
                                    results[remaining] = {"status": "found", "arxiv_id": remaining,
                                                          "data": candidate, "error": None}
                                except ValueError:
                                    results[remaining] = dict(results[clean_aid], arxiv_id=remaining)
                            break

        except RateLimitException as exc:
            for aid in chunk:
                results[aid.strip()] = {
                    "status": "error", "arxiv_id": aid.strip(),
                    "data": None, "error": str(exc), "http_status": 429,
                    "retry_after": _parse_retry_after(getattr(exc.__cause__, "headers", None)),
                }

        except (urllib.error.HTTPError, urllib.error.URLError, TimeoutError, OSError, ValueError) as exc:
            err_msg = (f"HTTP {exc.code}: {exc.reason}" if hasattr(exc, "code") else
                       f"Invalid response: {exc}" if isinstance(exc, ValueError) else f"Network error: {exc}")
            for aid in chunk:
                results[aid.strip()] = {
                    "status": "error", "arxiv_id": aid.strip(),
                    "data": None, "error": err_msg, "http_status": getattr(exc, "code", None),
                    "retry_after": _parse_retry_after(getattr(exc, "headers", None)),
                }

        return results
