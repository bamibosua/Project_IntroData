"""Reference Crawler.

Coordinates batch crawling from Semantic Scholar API, checkpointing,
filtering ArXiv citations, and exporting the `references` table & summary.

Supports two crawl modes:
- **single**: One GET request per paper. Safest, most compatible.
- **batch**: One POST request per up to 500 papers. ~500x faster, same rate quota.
"""

from __future__ import annotations

import argparse
import csv
from datetime import datetime, timezone
import getpass
import json
import logging
import os
from pathlib import Path
import time
from typing import Any
from collections.abc import Iterable

from .client import SemanticScholarClient, BATCH_MAX_IDS, validate_references
from .extractor import extract_arxiv_references, normalize_arxiv_id
from .rate_limiter import SemanticScholarRateLimiter
from .storage import CheckpointStore

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Utility: atomic file writes
# ---------------------------------------------------------------------------

def write_json_atomic(path: Path, data: Any, *, _retries: int = 8, _delay: float = 0.15) -> None:
    """Writes data to a temporary file first then atomically replaces target.

    On Windows, the target file may be held open by an editor (e.g. VS Code),
    causing os.replace() to raise PermissionError (WinError 5). We retry up to
    ``_retries`` times with ``_delay`` second gaps to let the OS release the lock.
    If replacement remains blocked, preserve the previous file and raise.
    """
    path.parent.mkdir(parents=True, exist_ok=True)
    temp = path.with_suffix(path.suffix + ".tmp")
    with temp.open("w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)

    last_err: Exception | None = None
    for attempt in range(_retries):
        try:
            temp.replace(path)
            return  # success
        except PermissionError as exc:
            last_err = exc
            logger.debug(
                "write_json_atomic: PermissionError on attempt %d/%d for %s — "
                "file likely held open by editor. Retrying in %.2fs...",
                attempt + 1, _retries, path.name, _delay,
            )
            time.sleep(_delay)

    # Preserve the last valid checkpoint/export if replacement is blocked.
    temp.unlink(missing_ok=True)
    if last_err is not None:
        raise last_err
    raise ValueError("_retries must be positive")


def write_csv_atomic(path: Path, rows: Iterable[dict[str, Any]], fieldnames: list[str]) -> None:
    """Writes list of dicts to CSV atomically."""
    path.parent.mkdir(parents=True, exist_ok=True)
    temp = path.with_suffix(path.suffix + ".tmp")
    try:
        with temp.open("w", encoding="utf-8", newline="") as f:
            writer = csv.DictWriter(f, fieldnames=fieldnames)
            writer.writeheader()
            writer.writerows(rows)
        temp.replace(path)
    finally:
        temp.unlink(missing_ok=True)


def write_json_array_atomic(path: Path, rows: Iterable[dict[str, Any]]) -> None:
    """Stream a JSON array, retaining the previous export on failure."""
    path.parent.mkdir(parents=True, exist_ok=True)
    temp = path.with_suffix(path.suffix + ".tmp")
    try:
        with temp.open("w", encoding="utf-8") as stream:
            stream.write("[\n")
            separator = ""
            for row in rows:
                stream.write(separator)
                json.dump(row, stream, ensure_ascii=False)
                separator = ",\n"
            stream.write("\n]\n")
        temp.replace(path)
    finally:
        temp.unlink(missing_ok=True)


# ---------------------------------------------------------------------------
# Utility: load ArXiv IDs
# ---------------------------------------------------------------------------

def load_arxiv_ids_from_file(input_path: Path, limit: int | None = None) -> list[str]:
    """Loads ArXiv IDs from JSON, CSV, or plain text file.

    Args:
        input_path: Path to the file containing ArXiv IDs.
        limit: Optional cap on the number of IDs returned.

    Returns:
        Deduplicated list of ArXiv IDs.
    """
    if not input_path.exists():
        raise FileNotFoundError(f"Input file not found: {input_path}")

    ids: list[str] = []
    suffix = input_path.suffix.lower()

    if suffix == ".json":
        with input_path.open("r", encoding="utf-8") as f:
            data = json.load(f)
            if isinstance(data, list):
                for item in data:
                    if isinstance(item, dict) and "arxiv_id" in item:
                        ids.append(str(item["arxiv_id"]))
                    elif isinstance(item, str):
                        ids.append(item.strip())
            elif isinstance(data, dict):
                for k, v in data.items():
                    if isinstance(v, dict) and "arxiv_id" in v:
                        ids.append(str(v["arxiv_id"]))
                    else:
                        ids.append(str(k))
    elif suffix == ".csv":
        with input_path.open("r", encoding="utf-8") as f:
            reader = csv.DictReader(f)
            for row in reader:
                val = (
                    row.get("arxiv_id")
                    or row.get("paper_id")
                    or (list(row.values())[0] if row else None)
                )
                if val:
                    ids.append(str(val).strip())
    else:
        # Plain text: one ID per line, '#' comments ignored
        with input_path.open("r", encoding="utf-8") as f:
            for line in f:
                stripped = line.strip()
                if stripped and not stripped.startswith("#"):
                    ids.append(stripped)

    # Deduplicate while preserving order
    seen: set[str] = set()
    unique_ids: list[str] = []
    for aid in ids:
        aid = normalize_arxiv_id(aid)
        if aid and aid not in seen:
            seen.add(aid)
            unique_ids.append(aid)

    if limit is not None and limit > 0:
        unique_ids = unique_ids[:limit]

    return unique_ids


# ---------------------------------------------------------------------------
# API Key prompt helper
# ---------------------------------------------------------------------------

def prompt_api_key(*, env_var: str = "S2_API_KEY", allow_skip: bool = True) -> str | None:
    """Interactively prompts the user to enter a Semantic Scholar API key.

    Priority order:
    1. Environment variable ``S2_API_KEY`` (already set externally).
    2. User input at the prompt (typed securely, not echoed to terminal).
    3. Skip (press Enter with no input) — runs in public mode.

    Args:
        env_var: Name of the environment variable to check first.
        allow_skip: If True, pressing Enter skips key entry (public mode).

    Returns:
        The API key string, or None if skipped / not available.
    """
    existing = os.environ.get(env_var)
    if existing:
        print(f"🔑 Đã phát hiện API Key từ biến môi trường {env_var} (độ dài: {len(existing)}).")
        return existing

    print("\n" + "=" * 64)
    print("  SEMANTIC SCHOLAR API KEY")
    print("=" * 64)
    print("  Bạn có thể nhập API key để tăng tốc độ và tránh bị chặn.")
    print("  Để lấy key miễn phí: https://www.semanticscholar.org/product/api")
    print("  • Để trống và nhấn Enter → chạy chế độ công cộng (không cần key)")
    print("  • API key sẽ KHÔNG hiển thị trên màn hình khi bạn gõ")
    print("=" * 64)

    try:
        key = getpass.getpass("  Nhập API Key (hoặc Enter để bỏ qua): ").strip()
    except (EOFError, KeyboardInterrupt):
        print("\n  Bỏ qua nhập API Key.")
        key = ""

    if key:
        # Store in environment so child processes / subsequent checks also see it
        os.environ[env_var] = key
        print(f"  ✅ API Key đã được nhận (độ dài: {len(key)} ký tự).")
    else:
        if allow_skip:
            print("  ℹ️  Không có API Key. Chạy chế độ công cộng (1 req/s, tối đa 5,000 req/ngày).")
        key = None  # type: ignore[assignment]

    print("=" * 64 + "\n")
    return key or None


# ---------------------------------------------------------------------------
# Main ReferenceCrawler class
# ---------------------------------------------------------------------------

class ReferenceCrawler:
    """Crawls references from Semantic Scholar API and exports results.

    Supports two crawl modes:
    - ``single``: One request per paper (GET /paper/arXiv:{id}).
    - ``batch``: Up to ``batch_chunk_size`` papers per request
      (POST /paper/batch). Massively faster while respecting the same 1 req/s
      rate limit.

    Attributes:
        client: The :class:`SemanticScholarClient` used for API calls.
        output_dir: Directory where outputs and checkpoints are saved.
        batch_size: Deprecated compatibility setting; checkpoints use SQLite.
        mode: ``"single"`` or ``"batch"`` crawl mode.
        batch_chunk_size: Papers per POST request when mode is ``"batch"``
            (capped at BATCH_MAX_IDS = 500).
    """

    def __init__(
        self,
        client: SemanticScholarClient | None = None,
        output_dir: Path | str = Path("data/references"),
        batch_size: int = 50,
        mode: str = "batch",
        batch_chunk_size: int = 50,
        export_json: bool = False,
    ) -> None:
        self.client = client or SemanticScholarClient()
        self.output_dir = Path(output_dir)
        self.batch_size = max(1, batch_size)
        self.mode = mode if mode in ("single", "batch") else "batch"
        self.batch_chunk_size = max(1, min(int(batch_chunk_size), BATCH_MAX_IDS))
        self.export_json = export_json
        self._store: CheckpointStore | None = None
        self._service_wait_until = 0.0

    # ------------------------------------------------------------------
    # Export helpers
    # ------------------------------------------------------------------

    def _export_all_results(
        self,
        total_ids: int,
        paper_statuses: dict[str, dict[str, Any]],
        arxiv_ids: list[str],
        stopped_reason: str,
    ) -> dict[str, Any]:
        """Export CSV and summary, plus an optional JSON copy of references."""
        papers_found = sum(1 for p in paper_statuses.values() if p.get("status") == "found")
        papers_not_found = sum(1 for p in paper_statuses.values() if p.get("status") == "not_found")
        papers_error = sum(1 for p in paper_statuses.values() if p.get("status") == "error")
        total_raw_references = sum(p.get("raw_ref_count", 0) for p in paper_statuses.values())
        if self._store is None:
            raise RuntimeError("Checkpoint store is not initialized")
        counts = self._store.reference_counts(arxiv_ids)
        total_arxiv_references = counts["total_arxiv_references"]
        unique_cited_arxiv_ids = counts["unique_cited_arxiv_ids"]

        summary = {
            "total_papers_input": total_ids,
            "papers_processed": len(paper_statuses),
            "papers_pending": max(0, total_ids - papers_found - papers_not_found),
            "papers_found_in_s2": papers_found,
            "papers_not_found_in_s2": papers_not_found,
            "papers_error": papers_error,
            "total_raw_references": total_raw_references,
            "total_stored_references": counts["total_stored_references"],
            "total_arxiv_references": total_arxiv_references,
            "unique_cited_arxiv_ids": unique_cited_arxiv_ids,
            "average_arxiv_refs_per_found_paper": (
                round(total_arxiv_references / papers_found, 2) if papers_found > 0 else 0.0
            ),
            "stopped_reason": stopped_reason,
            "timestamp_utc": datetime.now(timezone.utc).isoformat(),
        }

        fieldnames = ["source_arxiv_id", "cited_arxiv_id", "cited_title", "cited_s2_id"]
        write_csv_atomic(self.output_dir / "references.csv", self._store.iter_arxiv_edges(arxiv_ids), fieldnames)
        if self.export_json:
            write_json_array_atomic(self.output_dir / "references.json", self._store.iter_arxiv_edges(arxiv_ids))
        write_json_atomic(self.output_dir / "references_summary.json", summary)

        return summary

    # ------------------------------------------------------------------
    # Checkpoint recovery
    # ------------------------------------------------------------------

    def _load_checkpoint(self, arxiv_ids: list[str]) -> tuple[dict, set[str]]:
        """Restore statuses and retry deadlines without loading reference payloads."""
        if self._store is None:
            raise RuntimeError("Checkpoint store is not initialized")
        return self._store.load_statuses(arxiv_ids)

    # ------------------------------------------------------------------
    # Crawl: single mode
    # ------------------------------------------------------------------

    def _crawl_single(
        self,
        remaining_ids: list[str],
        paper_statuses: dict[str, dict[str, Any]],
        reference_totals: dict[str, int],
        processed_ids: set[str],
        total_ids: int,
        arxiv_ids: list[str],
        pbar: Any,
    ) -> None:
        """Crawl loop: one GET request per paper."""
        for pid in remaining_ids:
            if self._service_wait_until > time.time():
                break
            res = self.client.fetch_paper_references(pid)
            self._process_result(
                pid, res, paper_statuses, reference_totals, processed_ids,
                total_ids, arxiv_ids, pbar,
            )

    # ------------------------------------------------------------------
    # Crawl: batch mode (with Batch-to-Single Fallback)
    # ------------------------------------------------------------------

    # Maximum number of consecutive batch-level failures before falling back
    # to single mode for a given chunk (default: 3 attempts = initial request + 2 retries).
    _BATCH_FALLBACK_THRESHOLD: int = 3

    def _is_chunk_all_errors(self, chunk: list[str], batch_results: Any) -> bool:
        """Returns True if every paper in the chunk got an error (or is missing/invalid)."""
        if not isinstance(batch_results, dict):
            return True
        for pid in chunk:
            res = batch_results.get(pid)
            if isinstance(res, dict) and res.get("status") in ("found", "not_found"):
                return False
        return True

    def _crawl_chunk_single_fallback(
        self,
        chunk: list[str],
        paper_statuses: dict[str, dict[str, Any]],
        reference_totals: dict[str, int],
        processed_ids: set[str],
        total_ids: int,
        arxiv_ids: list[str],
        pbar: Any,
    ) -> None:
        """Fallback: crawl each paper in the chunk one-by-one using single GET requests.

        Called automatically when a batch chunk has failed
        ``_BATCH_FALLBACK_THRESHOLD`` times in a row. After this method returns,
        the outer loop resumes batch mode for the next chunk.
        """
        msg_start = (
            f"\n🔀 [KÍCH HOẠT FALLBACK → SINGLE MODE] "
            f"Có {len(chunk)} bài chưa hoàn tất trong batch ({chunk[0]} ... {chunk[-1]}).\n"
            f"   → Tự động chuyển sang cào từng bài đơn lẻ (Single mode) với cùng cơ chế giới hạn tốc độ..."
        )
        print(msg_start, flush=True)

        logger.warning(
            "Batch-to-Single fallback activated for chunk of %d papers starting at %s.",
            len(chunk), chunk[0] if chunk else "?",
        )
        attempted = 0
        for pid in chunk:
            if self._service_wait_until > time.time():
                break
            res = self.client.fetch_paper_references(pid)
            self._process_result(
                pid, res, paper_statuses, reference_totals, processed_ids,
                total_ids, arxiv_ids, pbar,
            )
            attempted += 1

        msg_done = (
            f"\nℹ️ [FALLBACK KẾT THÚC] Đã thử {attempted}/{len(chunk)} bài bằng Single mode.\n"
            f"   → Bài chưa hoàn tất vẫn nằm trong hàng đợi; batch tiếp tục khi hết thời gian chờ.\n"
        )
        print(msg_done, flush=True)

        logger.info(
            "Batch-to-Single fallback attempted %d/%d papers; pending IDs remain queued.",
            attempted, len(chunk),
        )

    def _crawl_batch(
        self,
        remaining_ids: list[str],
        paper_statuses: dict[str, dict[str, Any]],
        reference_totals: dict[str, int],
        processed_ids: set[str],
        total_ids: int,
        arxiv_ids: list[str],
        pbar: Any,
    ) -> None:
        """Crawl loop: POST up to batch_chunk_size papers per request.

        Implements **Batch-to-Single Fallback**:

        Each chunk is attempted in batch mode up to ``_BATCH_FALLBACK_THRESHOLD`` (3)
        times. If every attempt returns all-error results (e.g. the chunk is too
        computationally heavy for the batch endpoint), the chunk is automatically
        re-processed paper-by-paper using single GET requests. After the fallback
        chunk is done, the main loop resumes batch mode for subsequent chunks.
        """
        chunk_size = self.batch_chunk_size
        threshold = self._BATCH_FALLBACK_THRESHOLD

        for chunk_start in range(0, len(remaining_ids), chunk_size):
            if self._service_wait_until > time.time():
                break
            chunk = remaining_ids[chunk_start: chunk_start + chunk_size]

            # ── Try this chunk in batch mode up to `threshold` times ─────────
            consecutive_failures = 0
            batch_results: dict[str, dict[str, Any]] = {}

            while consecutive_failures < threshold:
                attempt_no = consecutive_failures + 1
                retry_label = f"Retry {attempt_no - 1}/{threshold - 1}" if attempt_no > 1 else None

                batch_results = self.client.fetch_papers_batch(chunk, retry_info=retry_label)

                # Service-wide failures wait globally; bad batch requests isolate IDs.
                if isinstance(batch_results, dict) and any(
                    isinstance(r, dict) and r.get("http_status") in (400, 401, 403, 429)
                    for r in batch_results.values()
                ):
                    break

                if self._is_chunk_all_errors(chunk, batch_results):
                    consecutive_failures += 1
                    if consecutive_failures < threshold:
                        if isinstance(self.client, SemanticScholarClient):
                            time.sleep(self.client.retry_delay(consecutive_failures - 1))
                        continue
                    # Reached threshold — fall through to single-mode fallback
                else:
                    consecutive_failures = 0
                    break

            # Persist valid entries before handling failed entries.
            if not isinstance(batch_results, dict):
                batch_results = {}
            failed = []
            for pid in chunk:
                res = batch_results.get(pid, {"status": "error", "error": "Missing from batch response"})
                if isinstance(res, dict) and res.get("status") in ("found", "not_found"):
                    self._process_result(pid, res, paper_statuses, reference_totals,
                                         processed_ids, total_ids, arxiv_ids, pbar)
                else:
                    failed.append((pid, res))
            for pid, res in failed:
                code = res.get("http_status") if isinstance(res, dict) else None
                if code in (401, 403, 429):
                    self._process_result(pid, res, paper_statuses, reference_totals,
                                         processed_ids, total_ids, arxiv_ids, pbar)
            if self._service_wait_until > time.time():
                continue
            retry_ids = [pid for pid, res in failed]
            if retry_ids:
                self._crawl_chunk_single_fallback(
                    retry_ids, paper_statuses, reference_totals, processed_ids,
                    total_ids, arxiv_ids, pbar,
                )

    # ------------------------------------------------------------------
    # Shared result processor
    # ------------------------------------------------------------------

    def _process_result(
        self,
        pid: str,
        res: dict[str, Any],
        paper_statuses: dict[str, dict[str, Any]],
        reference_totals: dict[str, int],
        processed_ids: set[str],
        total_ids: int,
        arxiv_ids: list[str],
        pbar: Any,
    ) -> None:
        """Handles a single paper's API result: extracts refs, checkpoints, updates progress."""
        if not isinstance(res, dict):
            res = {"status": "error", "error": "Invalid result envelope"}
        status = res.get("status", "error")
        data = res.get("data")
        error = res.get("error")

        raw_ref_count = 0
        arxiv_ref_count = 0
        raw_refs = []

        if status not in ("found", "not_found", "error") or (status == "found" and not isinstance(data, dict)):
            status, data, error = "error", None, "Invalid paper response"
        if status == "found":
            try:
                validate_references(data.get("references"))
            except ValueError as exc:
                status, data, error = "error", None, str(exc)
        if status == "found" and data:
            raw_refs = data.get("references") or []
            raw_ref_count = len(raw_refs)
            arxiv_ref_count = len(extract_arxiv_references(pid, data))

        p_info = {
            "arxiv_id": pid,
            "status": status,
            "raw_ref_count": raw_ref_count,
            "arxiv_ref_count": arxiv_ref_count,
            "error": error,
            "source": "semantic_scholar",
            "updated_at_utc": datetime.now(timezone.utc).isoformat(),
            "source_s2_id": data.get("paperId") if isinstance(data, dict) else None,
        }
        if status == "error":
            previous = paper_statuses.get(pid, {})
            failures = previous.get("retry_count", 0) + 1
            code = res.get("http_status")
            delay = min(300.0, 5.0 * 2 ** min(failures - 1, 6))
            if code in (401, 403):
                delay = 300.0
            elif code == 429:
                delay = max(60.0, delay)
            retry_after = res.get("retry_after")
            if isinstance(retry_after, (int, float)) and 0 <= retry_after < float("inf"):
                delay = max(delay, retry_after)
            p_info.update(retry_count=failures, next_retry_at=time.time() + delay,
                          http_status=code, retry_after=retry_after)
            if code in (401, 403, 429):
                self._service_wait_until = max(self._service_wait_until, p_info["next_retry_at"])
            logger.warning("%s pending: %s; retry in %.0fs", pid, error, delay)
        if self._store is None:
            raise RuntimeError("Checkpoint store is not initialized")
        self._store.save(p_info, raw_refs)
        reference_totals["arxiv"] += arxiv_ref_count - paper_statuses.get(pid, {}).get("arxiv_ref_count", 0)
        paper_statuses[pid] = p_info
        if status in ("found", "not_found"):
            processed_ids.add(pid)

        n_done = len(processed_ids)

        if pbar:
            if status in ("found", "not_found"):
                pbar.update(1)
            pbar.set_postfix({"arxiv_refs": reference_totals['arxiv'], "last_status": status})
        else:
            print(
                f"[{n_done}/{total_ids}] {pid}: {status} "
                f"(raw refs: {raw_ref_count}, arxiv refs: {arxiv_ref_count})",
                flush=True,
            )

    # ------------------------------------------------------------------
    # Main crawl entry point
    # ------------------------------------------------------------------

    def crawl(
        self,
        arxiv_ids: list[str],
        resume: bool = True,
        show_progress: bool = True,
    ) -> dict[str, Any]:
        """Runs the reference crawl over a list of ArXiv IDs with instant checkpointing.

        Args:
            arxiv_ids: List of ArXiv IDs to crawl.
            resume: Whether to resume from existing checkpoints.
            show_progress: Whether to show a tqdm progress bar.

        Returns:
            Summary dictionary of crawling results and metrics.
        """
        self.output_dir.mkdir(parents=True, exist_ok=True)
        arxiv_ids = list(dict.fromkeys(normalize_arxiv_id(pid) for pid in arxiv_ids if pid.strip()))
        db_path = self.output_dir / "checkpoint.sqlite3"
        self._store = CheckpointStore(db_path)
        if not resume:
            self._store.reset()
        total_ids = len(arxiv_ids)

        # ── Checkpoint Recovery ──────────────────────────────────────────────
        paper_statuses: dict[str, dict[str, Any]] = {}
        reference_totals: dict[str, int] = {"arxiv": 0}
        processed_ids: set[str] = set()

        if resume:
            paper_statuses, processed_ids = self._load_checkpoint(arxiv_ids)
            reference_totals["arxiv"] = sum(p.get("arxiv_ref_count", 0) for p in paper_statuses.values())

        remaining_ids = [pid for pid in arxiv_ids if pid not in processed_ids]

        # ── Progress banner ───────────────────────────────────────────────────
        mode_desc = (
            f"BATCH (chunk={self.batch_chunk_size} bài | Attempts={self._BATCH_FALLBACK_THRESHOLD} lần → Fallback Single)"
            if self.mode == "batch"
            else "SINGLE"
        )
        if processed_ids:
            pct = (len(processed_ids) / total_ids * 100) if total_ids else 0.0
            print("\n" + "=" * 68, flush=True)
            print("🔁 [CHECKPOINT TỰ ĐỘNG KHÔI PHỤC TIẾN TRÌNH]", flush=True)
            print(f"   • Chế độ vận hành : {mode_desc}", flush=True)
            print(f"   • Đã hoàn thành   : {len(processed_ids)}/{total_ids} bài báo ({pct:.1f}%)", flush=True)
            print(f"   • Đã tích lũy     : {reference_totals['arxiv']} trích dẫn ArXiv", flush=True)
            print(f"   • Còn lại         : {len(remaining_ids)} bài", flush=True)
            if remaining_ids:
                print(f"   👉 Tiếp tục từ    : {remaining_ids[0]}", flush=True)
            else:
                print("   🎉 Toàn bộ danh sách đã được cào xong!", flush=True)
            print("=" * 68 + "\n", flush=True)
        else:
            print("\n" + "=" * 68, flush=True)
            print("🚀 [KHỞI ĐỘNG CRAWLER THAM KHẢO (SEMANTIC SCHOLAR)]", flush=True)
            print(f"   • Chế độ vận hành : {mode_desc}", flush=True)
            print(f"   • Tổng số bài     : {total_ids} bài", flush=True)
            limiter_obj = getattr(self.client, "limiter", None)
            interval_val = getattr(limiter_obj, "interval", None)
            interval_display = f"{interval_val:.2f}s" if isinstance(interval_val, (int, float)) else str(interval_val or "1.1s")
            print(f"   • Tốc độ an toàn  : interval = {interval_display}", flush=True)
            print("=" * 68 + "\n", flush=True)

        stopped_reason = "completed"

        pbar = None
        if show_progress:
            try:
                from tqdm import tqdm
                pbar = tqdm(
                    total=total_ids,
                    initial=len(processed_ids),
                    desc="Crawling References",
                    unit="paper",
                )
            except ImportError:
                pbar = None

        try:
            self._service_wait_until = max(
                (p.get("next_retry_at", 0.0) for p in paper_statuses.values()
                 if p.get("http_status") in (401, 403, 429)), default=0.0)
            while len(processed_ids) < total_ids:
                now = time.time()
                pending = [pid for pid in arxiv_ids if pid not in processed_ids]
                ready = [pid for pid in pending
                         if paper_statuses.get(pid, {}).get("next_retry_at", 0.0) <= now]
                if self._service_wait_until > now or not ready:
                    deadline = max(self._service_wait_until, min(
                        paper_statuses.get(pid, {}).get("next_retry_at", 0.0)
                        for pid in pending))
                    # Short sleeps keep the process responsive to interruption.
                    time.sleep(min(60.0, max(0.01, deadline - now)))
                    continue
                # Re-read the environment after authentication recovery; never fall back to public mode.
                if isinstance(self.client, SemanticScholarClient):
                    self.client.api_key = os.environ.get("S2_API_KEY") or self.client.api_key
                runner = self._crawl_batch if self.mode == "batch" else self._crawl_single
                runner(ready, paper_statuses, reference_totals, processed_ids,
                       total_ids, arxiv_ids, pbar)

        except Exception:
            stopped_reason = "failed"
            raise
        except KeyboardInterrupt:
            stopped_reason = "user_interrupted"
            if pbar:
                pbar.close()
                pbar = None
            print("\n" + "=" * 64, flush=True)
            print("⏹️  [DỪNG CHƯƠNG TRÌNH] Bạn đã bấm dừng (Ctrl+C).", flush=True)
            print(f"   • Đã hoàn thành : {len(processed_ids)}/{total_ids} bài báo", flush=True)
            print(f"   • Đã cào được   : {reference_totals['arxiv']} trích dẫn ArXiv", flush=True)
            print("👉 LẦN SAU CHẠY LẠI SẼ TỰ ĐỘNG TIẾP TỤC TỪ ĐÂY!", flush=True)
            print("=" * 64 + "\n", flush=True)

        finally:
            if pbar:
                pbar.close()
            if stopped_reason == "completed" and any(p.get("status") == "error" for p in paper_statuses.values()):
                stopped_reason = "completed_with_errors"
            summary = self._export_all_results(
                total_ids=total_ids,
                paper_statuses=paper_statuses,
                arxiv_ids=arxiv_ids,
                stopped_reason=stopped_reason,
            )
            if stopped_reason != "user_interrupted":
                print("\n========== REFERENCE CRAWLING SUMMARY ==========", flush=True)
                print(json.dumps(summary, indent=2, ensure_ascii=False), flush=True)
                print(f"References CSV  → {self.output_dir / 'references.csv'}", flush=True)
                if self.export_json:
                    print(f"References JSON → {self.output_dir / 'references.json'}", flush=True)
                print(f"Summary JSON    → {self.output_dir / 'references_summary.json'}", flush=True)

        return summary


# ---------------------------------------------------------------------------
# CLI entry point
# ---------------------------------------------------------------------------

def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description=(
            "Crawl reference citations from Semantic Scholar Graph API for ArXiv papers.\n\n"
            "Rate Limit: Semantic Scholar allows 1 request/second cumulative across all "
            "endpoints. This crawler enforces a default interval of 1.1s with a Global "
            "Circuit Breaker that activates on HTTP 429."
        ),
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument(
        "--input",
        type=Path,
        default=Path("src/discovered_papers_with_revised_dates.json"),
        help="Path to input papers file (JSON, CSV, or text). Default: %(default)s",
    )
    parser.add_argument(
        "--output-dir",
        type=Path,
        default=Path("data/references"),
        help="Directory to save output files and checkpoints. Default: %(default)s",
    )
    parser.add_argument(
        "--limit",
        type=int,
        default=None,
        help="Cap number of papers to crawl (default: all papers in input file).",
    )
    parser.add_argument(
        "--api-key",
        type=str,
        default=None,
        metavar="KEY",
        help=(
            "Semantic Scholar API key. If omitted, also checked from the S2_API_KEY "
            "environment variable. If neither is set, the crawler will prompt you "
            "interactively to enter a key (or skip for public mode)."
        ),
    )
    parser.add_argument(
        "--no-key-prompt",
        action="store_true",
        help="Disable the interactive API key prompt. Runs in public mode silently.",
    )
    parser.add_argument(
        "--interval",
        type=float,
        default=None,
        metavar="SECONDS",
        help=(
            "Minimum seconds between requests (default: 1.1). "
            "Must be >= 1.01. Increasing this adds a safety margin against 429s."
        ),
    )
    parser.add_argument(
        "--mode",
        choices=["single", "batch"],
        default="batch",
        help=(
            "'batch' (default): POST up to --batch-chunk-size papers per request (~500x faster). "
            "'single': one GET request per paper (more compatible, slower)."
        ),
    )
    parser.add_argument(
        "--batch-chunk-size",
        type=int,
        default=50,
        metavar="N",
        help=(
            "Papers per POST request in batch mode (1–500). Default: 50. "
            "Larger chunks are more efficient but require more memory."
        ),
    )
    parser.add_argument(
        "--batch-size",
        type=int,
        default=50,
        help="Deprecated compatibility option; SQLite checkpoints every paper.",
    )
    parser.add_argument(
        "--no-resume",
        action="store_true",
        help="Ignore existing checkpoints and start the crawl from scratch.",
    )
    parser.add_argument(
        "--verbose",
        action="store_true",
        help="Enable DEBUG-level logging output.",
    )

    parser.add_argument("--timeout", type=float, default=60.0, help="HTTP timeout in seconds (default: 60).")
    parser.add_argument("--export-json", action="store_true", help="Also export references.json (disabled by default).")
    args = parser.parse_args(argv)
    if not 0 < args.timeout < float("inf"):
        parser.error("--timeout must be a finite positive number")

    # ── Logging setup ────────────────────────────────────────────────────────
    log_level = logging.DEBUG if args.verbose else logging.INFO
    logging.basicConfig(
        level=log_level,
        format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
        datefmt="%H:%M:%S",
    )

    # ── API Key resolution ────────────────────────────────────────────────────
    # Priority: --api-key flag > S2_API_KEY env var > interactive prompt
    api_key: str | None = args.api_key or os.environ.get("S2_API_KEY")
    if not api_key and not args.no_key_prompt:
        api_key = prompt_api_key()

    has_api_key = bool(api_key)

    # ── Interval / Rate Limiter setup ─────────────────────────────────────────
    if args.interval is not None:
        interval = args.interval
        if interval < 1.01:
            parser.error(
                f"--interval {interval} is too low. Semantic Scholar requires >= 1 req/s. "
                "Please use a value >= 1.01."
            )
    else:
        # With an API key you still must respect 1 req/s; 1.1s is safe for both modes.
        interval = 1.1

    limiter = SemanticScholarRateLimiter(interval=interval)

    # ── Load input IDs ────────────────────────────────────────────────────────
    try:
        arxiv_ids = load_arxiv_ids_from_file(args.input, limit=args.limit)
    except Exception as exc:
        parser.error(f"Failed to load input IDs: {exc}")

    if not arxiv_ids:
        print("No ArXiv IDs found in input file.")
        return 0

    # ── Configuration banner ──────────────────────────────────────────────────
    masked_key = "Active" if api_key else "None (Public mode)"
    mode_str = (
        f"BATCH (chunk={args.batch_chunk_size} bài | Attempts={ReferenceCrawler._BATCH_FALLBACK_THRESHOLD} lần → Fallback Single)"
        if args.mode == "batch"
        else "SINGLE (Từng bài một)"
    )
    print("\n" + "=" * 68, flush=True)
    print("📋 [CẤU HÌNH HỆ THỐNG CRAWLER]", flush=True)
    print(f"   • Chế độ cào      : {mode_str}", flush=True)
    print(f"   • Xác thực        : {'🔑 API Key (' + masked_key + ')' if has_api_key else '🌐 Public (Không có key)'}", flush=True)
    print(f"   • Giới hạn tốc độ : Interval {interval:.2f}s | Cooldown 429: 60s", flush=True)
    print(f"   • Tệp đầu vào     : {args.input} ({len(arxiv_ids)} bài)", flush=True)
    print("=" * 68 + "\n", flush=True)

    # ── Construct client & crawler ────────────────────────────────────────────
    client = SemanticScholarClient(
        api_key=api_key,
        timeout=args.timeout,
        limiter=limiter,
    )
    crawler = ReferenceCrawler(
        client=client,
        output_dir=args.output_dir,
        batch_size=args.batch_size,
        mode=args.mode,
        batch_chunk_size=args.batch_chunk_size,
        export_json=args.export_json,
    )

    summary = crawler.crawl(arxiv_ids=arxiv_ids, resume=not args.no_resume)
    return 0 if summary["stopped_reason"] == "completed" else 1


if __name__ == "__main__":
    raise SystemExit(main())
