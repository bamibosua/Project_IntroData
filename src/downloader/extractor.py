"""In-memory inspection of a downloaded source archive; only .tex/.bib (and a conditional .bbl) reach the disk.

Status contract of `extract_tex_and_bib_from_bytes`:
  success        >= 1 .tex or .bib kept (a version with only .bib is still "success"); may carry
                 `partial: true` when oversize members were skipped
  no_tex_source  genuinely nothing usable (PDF-only, HTTP 404, archive without .tex/.bib, other formats)
  error          corrupt / truncated / encrypted archive -> retried by the audit (never a silent "no source")
"""

from __future__ import annotations

import io
from typing import Callable
from pathlib import Path, PurePosixPath
import tarfile
import zipfile
import zlib

from .client import _NOT_FOUND_PAYLOAD
from .config import (ALLOWED_EXTENSIONS, CONDITIONAL_EXTENSIONS, MAX_MEMBER_BYTES, MAX_TAR_TOTAL_BYTES,
                     MAX_UNCOMPRESSED_BYTES)
from .utils import looks_like_latex_or_bib, sanitize_relative_path


class ExtractWriteError(Exception):
    """Disk write failure; must NOT be mistaken for a corrupt/unsupported archive."""


def _result(status: str, source_type: str, raw: int, uncompressed: int, kept: int,
            tex: int, bib: int, files: list[str], *, skipped: list[str] | None = None,
            renamed: dict[str, str] | None = None, bbl: int = 0, error: str | None = None) -> dict:
    out = {
        "status": status,
        "source_type": source_type,
        "raw_archive_bytes": raw,
        "uncompressed_total_bytes": uncompressed,
        "kept_tex_bib_bytes": kept,
        "tex_file_count": tex,
        "bib_file_count": bib,
        "bbl_file_count": bbl,
        "kept_files": sorted(files),
    }
    if skipped:
        out["skipped_oversize_files"] = sorted(skipped)
        out["partial"] = True  # some members were dropped: this version is incomplete but still usable
    if renamed:
        out["renamed_files"] = renamed  # original archive path -> stored path (parser needs this map)
    if error:
        out["error"] = error
    return out


def _error(source_type: str, raw: int, message: str) -> dict:
    return _result("error", source_type, raw, 0, 0, 0, 0, [], error=message)


def _unique_rel(seen: set[str], rel: Path) -> Path:
    """Avoid silent overwrites when two members map to the same (case-insensitive) path."""
    key = rel.as_posix().lower()
    if key not in seen:
        seen.add(key)
        return rel
    n = 1
    while True:
        cand = rel.with_name(f"{rel.stem}__dup{n}{rel.suffix}")
        k2 = cand.as_posix().lower()
        if k2 not in seen:
            seen.add(k2)
            return cand
        n += 1


def _write_member(dest_dir: Path, rel_path: Path, content: bytes) -> Path:
    out_file = dest_dir / rel_path
    try:
        out_file.parent.mkdir(parents=True, exist_ok=True)
        out_file.write_bytes(content)
    except OSError as exc:
        raise ExtractWriteError(f"{out_file}: {exc}") from exc
    return out_file


def _cleanup(written: list[Path], dest_dir: Path) -> None:
    """Remove files of a failed extraction (and emptied sub-folders) so no half-written data stays."""
    for f in written:
        try:
            f.unlink()
        except OSError:
            pass
    for f in sorted({p.parent for p in written}, key=lambda p: len(p.parts), reverse=True):
        while f != dest_dir and dest_dir in f.parents:
            try:
                f.rmdir()
            except OSError:
                break
            f = f.parent


def _collect(entries, dest_dir: Path, raw_size: int, uncompressed: "int | Callable[[], int]", kind: str) -> dict:
    """Shared tar/zip logic. `entries` yields (member_name, size, read_callable)."""
    written: list[Path] = []
    seen: set[str] = set()
    skipped: list[str] = []
    renamed: dict[str, str] = {}
    files: list[str] = []
    pending_bbl: list[tuple[str, Path, bytes]] = []
    kept = tex = bib = bbl = 0
    has_bibitem = False
    try:
        for name, size, read in entries:
            rel = sanitize_relative_path(name)
            if rel is None:
                continue
            suffix = rel.suffix.lower()
            if suffix not in ALLOWED_EXTENSIONS and suffix not in CONDITIONAL_EXTENSIONS:
                continue
            if size > MAX_MEMBER_BYTES or kept + size > MAX_UNCOMPRESSED_BYTES:
                skipped.append(rel.as_posix())
                continue
            content = read()
            if content is None:
                continue
            if suffix in CONDITIONAL_EXTENSIONS:
                pending_bbl.append((name, rel, content))
                continue
            final = _unique_rel(seen, rel)
            original = PurePosixPath(name.replace("\\", "/").lstrip("/")).as_posix()  # "./a.tex" -> "a.tex" (not a rename)
            if final.as_posix() != original:
                renamed[original] = final.as_posix()
            written.append(_write_member(dest_dir, final, content))
            kept += len(content)
            if suffix == ".tex":
                tex += 1
                has_bibitem = has_bibitem or b"\\bibitem" in content
            else:
                bib += 1
            files.append(final.as_posix())
        # .bbl is the only bibliography when there is neither a .bib nor an inline \bibitem.
        if tex > 0 and bib == 0 and not has_bibitem:
            for name, rel, content in pending_bbl:
                final = _unique_rel(seen, rel)
                original = PurePosixPath(name.replace("\\", "/").lstrip("/")).as_posix()  # "./a.tex" -> "a.tex" (not a rename)
                if final.as_posix() != original:
                    renamed[original] = final.as_posix()
                written.append(_write_member(dest_dir, final, content))
                kept += len(content)
                bbl += 1
                files.append(final.as_posix())
    except BaseException:
        _cleanup(written, dest_dir)
        raise
    ok = tex + bib > 0
    return _result("success" if ok else "no_tex_source", kind if ok else f"{kind}_without_tex",
                   raw_size, uncompressed() if callable(uncompressed) else uncompressed,
                   kept, tex, bib, files, skipped=skipped, renamed=renamed, bbl=bbl)


def _extract_tar(payload: bytes, dest_dir: Path, raw_size: int) -> dict:
    with tarfile.open(fileobj=io.BytesIO(payload), mode="r:*") as tar:
        members = [m for m in tar.getmembers() if m.isfile()]
        uncompressed = sum(max(0, m.size) for m in members)

        def entries():
            for m in members:
                yield m.name, m.size, (lambda m=m: (lambda f: f.read() if f is not None else None)(tar.extractfile(m)))

        return _collect(entries(), dest_dir, raw_size, uncompressed, "tar")


def _extract_tar_stream(raw: bytes, dest_dir: Path) -> dict:
    """Fallback for gzip payloads above the in-RAM limit (figure-heavy sources): stream the tar so the whole
    decompressed archive is never held in memory. Members are read in order, so `_collect` must (and does)
    read each wanted member immediately."""
    total = [0]

    with tarfile.open(fileobj=io.BytesIO(raw), mode="r|gz") as tar:
        def entries():
            for m in tar:
                if not m.isfile():
                    continue
                total[0] += max(0, m.size)
                if total[0] > MAX_TAR_TOTAL_BYTES:
                    raise ValueError("tar content exceeds MAX_TAR_TOTAL_BYTES")
                yield m.name, m.size, (lambda m=m: (lambda f: f.read() if f is not None else None)(tar.extractfile(m)))

        return _collect(entries(), dest_dir, len(raw), lambda: total[0], "tar")


def _extract_big_gz(raw: bytes, dest_dir: Path) -> dict:
    try:
        return _extract_tar_stream(raw, dest_dir)
    except (ValueError, tarfile.TarError, EOFError, OSError, zlib.error) as exc:
        return _error("gzip_too_large", len(raw), f"{type(exc).__name__}: {exc}")


def _extract_zip(raw: bytes, dest_dir: Path) -> dict:
    with zipfile.ZipFile(io.BytesIO(raw)) as zf:
        infos = [i for i in zf.infolist() if not i.is_dir()]
        uncompressed = sum(max(0, i.file_size) for i in infos)

        def entries():
            for i in infos:
                yield i.filename, i.file_size, (lambda i=i: zf.read(i.filename))

        return _collect(entries(), dest_dir, len(raw), uncompressed, "zip")


def _bounded_gunzip(raw: bytes, limit: int = MAX_UNCOMPRESSED_BYTES) -> bytes:
    """gzip decompress with an output cap; a truncated stream raises EOFError."""
    d = zlib.decompressobj(wbits=31)
    out = d.decompress(raw, limit + 1)
    if len(out) > limit:
        raise ValueError("gzip payload exceeds decompression limit")
    if not d.eof:
        raise EOFError("truncated gzip stream")
    return out


def _looks_like_tar(payload: bytes) -> bool:
    return payload[257:262] == b"ustar"


def extract_tex_and_bib_from_bytes(raw_bytes: bytes, dest_dir: Path) -> dict:
    """Inspect payload in RAM and extract .tex/.bib (and a conditional .bbl) into dest_dir."""
    if not dest_dir.is_dir():
        raise FileNotFoundError(f"Missing prepared folder: {dest_dir}. Run entry first.")
    raw_size = len(raw_bytes)

    if raw_bytes == _NOT_FOUND_PAYLOAD:
        # HTTP 404 from arXiv: keep it distinguishable from a genuine PDF-only submission.
        return _result("no_tex_source", "not_found_404", 0, 0, 0, 0, 0, [])
    if raw_bytes.startswith(b"%PDF-"):
        return _result("no_tex_source", "pdf_only", raw_size, raw_size, 0, 0, 0, [])

    # ---- zip --------------------------------------------------------------------
    if raw_bytes.startswith(b"PK\x03\x04"):
        try:
            return _extract_zip(raw_bytes, dest_dir)
        except zipfile.BadZipFile as exc:
            return _error("corrupt_zip", raw_size, f"BadZipFile: {exc}")
        except (NotImplementedError, RuntimeError) as exc:  # encrypted / unsupported compression
            return _error("unsupported_zip", raw_size, f"{type(exc).__name__}: {exc}")
        except (zlib.error, EOFError) as exc:
            return _error("corrupt_zip", raw_size, f"{type(exc).__name__}: {exc}")

    # ---- gzip layer (verified complete BEFORE anything is written) --------------
    payload = raw_bytes
    if raw_bytes.startswith(b"\x1f\x8b"):
        try:
            payload = _bounded_gunzip(raw_bytes)
        except ValueError:  # decompressed size above the in-RAM cap: stream it instead of failing
            return _extract_big_gz(raw_bytes, dest_dir)
        except (OSError, EOFError, zlib.error) as exc:
            return _error("corrupt_gzip", raw_size, f"{type(exc).__name__}: {exc}")

    # ---- tar --------------------------------------------------------------------
    try:
        return _extract_tar(payload, dest_dir, raw_size)
    except (tarfile.TarError, EOFError, OSError, zlib.error) as exc:
        if _looks_like_tar(payload):
            return _error("corrupt_tar", raw_size, f"{type(exc).__name__}: {exc}")
        # not a tar: fall through to the single-file case

    # ---- single plain/gz TeX file -------------------------------------------------
    if looks_like_latex_or_bib(payload):
        if len(payload) > MAX_MEMBER_BYTES:
            return _result("no_tex_source", "single_tex_oversize", raw_size, len(payload), 0, 0, 0, [])
        _write_member(dest_dir, Path("main.tex"), payload)
        # `main.tex` is a name we invented (the single file had none): record it for the parser.
        return _result("success", "single_tex", raw_size, len(payload), len(payload), 1, 0, ["main.tex"],
                       renamed={"(single-file source)": "main.tex"})

    return _result("no_tex_source", "pdf_or_other", raw_size, len(payload), 0, 0, 0, [])
