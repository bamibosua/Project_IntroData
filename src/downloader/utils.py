"""Small, dependency-free helpers."""

from __future__ import annotations

from pathlib import Path, PurePosixPath


def format_bytes(b: int | float) -> str:
    """Format bytes into human-readable string (KB, MB, GB, TB)."""
    for unit in ["B", "KB", "MB", "GB", "TB"]:
        if abs(b) < 1024.0:
            return f"{b:.2f} {unit}"
        b /= 1024.0
    return f"{b:.2f} PB"


def sanitize_relative_path(raw_name: str) -> Path | None:
    """Sanitize archive member path to prevent path traversal."""
    cleaned = raw_name.replace("\\", "/").lstrip("/")
    parts = [p for p in PurePosixPath(cleaned).parts if p not in ("", ".", "..") and ":" not in p]
    return Path(*parts) if parts else None


def looks_like_latex_or_bib(data: bytes) -> bool:
    """Check if raw uncompressed bytes represent a TeX/Bib text file."""
    if not data or data.startswith((b"%PDF-", b"PK\x03\x04", b"\x89PNG", b"\xff\xd8\xff", b"GIF8")):
        return False
    head = data[:4096].lstrip().lower()
    return not (head.startswith((b"<!doctype html", b"<html", b"<?xml")) or b"\x00" in data[:8192])
