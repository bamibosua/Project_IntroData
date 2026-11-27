"""Atomic references writes and metadata paths.

Serialize into a unique temporary file beside the destination, flush/fsync,
and replace atomically. Retry Windows lock errors 5/32/33 up to seven
attempts (six retries, 5.1 seconds total delay). Propagate other errors and
clean up temporary files in finally. Reads distinguish missing records from
I/O failures; record validation belongs to records.py."""

from __future__ import annotations

import json
import logging
import os
import time
import uuid
from typing import Callable

logger = logging.getLogger(__name__)

# Delays in seconds for retrying temporary Windows replace errors.
_REPLACE_DELAYS = [0.1, 0.2, 0.4, 0.8, 1.6, 2.0]

# Temporary Windows lock error codes.
_WIN_LOCKED_CODES = {5, 32, 33}


# ---------------------------------------------------------------------------
# Atomic write
# ---------------------------------------------------------------------------
def write_json_atomic(
    dest: str,
    data: object,
    *,
    sleep: Callable[[float], None] = time.sleep,
) -> int:
    """Serialize and atomically write JSON; propagate persistent I/O errors."""
    payload = json.dumps(data, ensure_ascii=False, indent=2, allow_nan=False).encode(
        "utf-8"
    )
    return write_bytes_atomic(dest, payload, sleep=sleep)


def write_bytes_atomic(dest: str, payload: bytes, *, sleep=time.sleep) -> int:
    """Use the same atomic-write mechanism for serialized JSON and CSV."""
    dest_dir = os.path.dirname(os.path.abspath(dest))
    os.makedirs(dest_dir, exist_ok=True)

    temporary_path = os.path.join(dest_dir, f".tmp_{uuid.uuid4().hex}")
    try:
        # Write and sync the temporary file before replacing the destination.
        with open(temporary_path, "xb") as stream:
            stream.write(payload)
            stream.flush()
            os.fsync(stream.fileno())

        # Replace atomically, retrying temporary Windows lock errors.
        _replace_with_retry(temporary_path, dest, sleep=sleep)
        temporary_path = (
            None  # Successful replacement leaves no temporary file to clean up.
        )
        return len(payload)
    finally:
        # Clean up the temporary file after failure or interruption.
        if temporary_path is not None:
            try:
                os.unlink(temporary_path)
            except OSError as e:
                logger.warning(
                    "Cannot remove temporary file (%s): %s",
                    type(e).__name__,
                    temporary_path,
                )


def _replace_with_retry(
    src: str,
    dest: str,
    *,
    sleep: Callable[[float], None] = time.sleep,
) -> None:
    """Retry os.replace for temporary Windows locks and propagate other errors."""
    last_exc: OSError | None = None
    for attempt, delay in enumerate([0.0] + _REPLACE_DELAYS):
        if delay > 0:
            sleep(delay)
        try:
            os.replace(src, dest)
            return
        except OSError as e:
            win_code = getattr(e, "winerror", None)
            if win_code in _WIN_LOCKED_CODES:
                last_exc = e
                logger.debug(
                    "os.replace attempt %d/%d failed (WinError %d): %s",
                    attempt + 1,
                    len(_REPLACE_DELAYS) + 1,
                    win_code,
                    dest,
                )
                continue
            raise  # Propagate unrelated errors immediately.

    # Retry budget exhausted.
    raise last_exc  # type: ignore[misc]


# ---------------------------------------------------------------------------
# Reading records for summaries and snapshot protection
# ---------------------------------------------------------------------------
def load_record(path: str) -> dict | None:
    """Read a JSON object, returning None only for a missing file.

    Raise OSError for permissions or I/O errors and ValueError for invalid JSON
    or a root value that is not a dictionary."""
    try:
        with open(path, encoding="utf-8") as fh:
            raw = fh.read()
    except FileNotFoundError:
        return None
    try:
        data = json.loads(raw)
    except json.JSONDecodeError as e:
        raise ValueError(f"Invalid JSON at {path}: {e}") from e
    if not isinstance(data, dict):
        raise ValueError(f"Record is not an object at {path}")
    return data


# ---------------------------------------------------------------------------
# Metadata paths
# ---------------------------------------------------------------------------
def default_meta_dir(out_path: str) -> str:
    """Return the adjacent metadata directory, such as papers_references_meta."""
    parent = os.path.dirname(os.path.abspath(out_path))
    name = os.path.basename(os.path.abspath(out_path))
    return os.path.join(parent, f"{name}_references_meta")


def record_path(out_dir: str, arxiv_id: str) -> str:
    """Return the references.json path using the existing publication folder convention."""
    if __package__:
        from .records import folder_name
    else:
        from records import folder_name
    return os.path.join(out_dir, folder_name(arxiv_id), "references.json")
