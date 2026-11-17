"""Small, dependency-free helpers."""

from __future__ import annotations

import re
from pathlib import Path, PurePosixPath


def peak_memory_mb() -> float:
    """Peak resident memory of this process in MB (real RSS, unlike tracemalloc which only sees Python heap)."""
    try:
        import resource  # POSIX (Linux/Colab, macOS)
        import sys
        peak = resource.getrusage(resource.RUSAGE_SELF).ru_maxrss
        return round(peak / (1024 * 1024 if sys.platform == "darwin" else 1024), 2)
    except ImportError:
        pass
    try:
        import psutil
        info = psutil.Process().memory_info()
        return round(getattr(info, "peak_wset", info.rss) / (1024 * 1024), 2)  # peak_wset: Windows only
    except Exception:
        return 0.0


_BIBITEM_RE = re.compile(rb"^(?:[^%\n\\]|\\.)*\\bibitem", re.M)


def has_inline_bibitem(data: bytes) -> bool:
    """True when the TeX source has a real \\bibitem, ignoring ones inside % comments."""
    return bool(_BIBITEM_RE.search(data))


def files_with_suffix(root: Path, suffix: str) -> list[Path]:
    """Files under `root` with this suffix, case-insensitively (a `main.TEX` counts as .tex)."""
    return [p for p in root.rglob("*") if p.is_file() and p.suffix.lower() == suffix]


def format_bytes(b: int | float) -> str:
    """Format bytes into human-readable string (KB, MB, GB, TB)."""
    for unit in ["B", "KB", "MB", "GB", "TB"]:
        if abs(b) < 1024.0:
            return f"{b:.2f} {unit}"
        b /= 1024.0
    return f"{b:.2f} PB"


def format_duration(seconds: float) -> str:
    """Format seconds into human-readable HH:MM:SS string."""
    sec = int(round(seconds))
    h, m, s = sec // 3600, (sec % 3600) // 60, sec % 60
    if h > 0:
        return f"{h}h {m:02d}m {s:02d}s ({seconds:.2f}s)"
    if m > 0:
        return f"{m}m {s:02d}s ({seconds:.2f}s)"
    return f"{seconds:.2f}s"


_FORBIDDEN_CHARS = '<>:"|?*'
_BINARY_PREFIXES = (b"%PDF-", b"%!PS", b"PK\x03\x04", b"\x89PNG", b"\xff\xd8\xff", b"GIF8")
_TEX_MARKERS = (
    b"\\documentclass", b"\\documentstyle", b"\\begin{", b"\\input", b"\\include", b"\\section",
    b"\\usepackage", b"\\newcommand", b"\\def", b"\\title", b"\\bye", b"\\chapter",
)
_BIB_MARKERS = (
    b"@article", b"@inproceedings", b"@book", b"@misc", b"@incollection", b"@techreport",
    b"@phdthesis", b"@proceedings", b"@inbook", b"@string", b"@unpublished",
)


def sanitize_relative_path(raw_name: str) -> Path | None:
    """Sanitize an archive member path: no traversal or macOS resource forks; forbidden chars become '_'."""
    cleaned = raw_name.replace("\\", "/").lstrip("/")
    raw_parts = PurePosixPath(cleaned).parts
    if not raw_parts or raw_parts[-1].startswith("._") or any(p.lower() == "__macosx" for p in raw_parts):
        return None
    parts = []
    for p in raw_parts:
        if p in ("", ".", ".."):
            continue
        p = "".join("_" if (c in _FORBIDDEN_CHARS or ord(c) < 32) else c for c in p).rstrip(" .")
        if p:
            parts.append(p)
    return Path(*parts) if parts else None


def is_usable_text_member(data: bytes) -> bool:
    """True for non-empty text members (rejects empty/whitespace-only files, null bytes, and common binary headers)."""
    if not data or not data.strip() or data.startswith(_BINARY_PREFIXES):
        return False
    return b"\x00" not in data[:8192]


def classify_plain_source(data: bytes) -> str | None:
    """Classify a single plain payload as 'tex', 'bib', or None (rejects PostScript, HTML, binary)."""
    if not is_usable_text_member(data):
        return None
    head = data[:65536]
    if head.lstrip().lower().startswith((b"<!doctype html", b"<html", b"<?xml")):
        return None
    if any(m in head for m in _TEX_MARKERS):
        return "tex"
    if any(m in head for m in _BIB_MARKERS):
        return "bib"
    return None


def looks_like_latex_or_bib(data: bytes) -> bool:
    """True only when the plain payload carries real TeX/BibTeX markers (rejects PostScript, HTML, binary)."""
    return classify_plain_source(data) is not None


def looks_like_html(data: bytes) -> bool:
    return data[:1024].lstrip().lower().startswith((b"<!doctype html", b"<html", b"<head", b"<?xml"))
