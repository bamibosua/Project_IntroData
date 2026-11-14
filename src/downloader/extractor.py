"""In-memory inspection of a downloaded source archive; only .tex and .bib reach the disk."""

from __future__ import annotations

import gzip
import io
from pathlib import Path
import tarfile
import zipfile

from .config import ALLOWED_EXTENSIONS
from .utils import looks_like_latex_or_bib, sanitize_relative_path


def _result(
    status: str,
    source_type: str,
    raw: int,
    uncompressed: int,
    kept: int,
    tex: int,
    bib: int,
    files: list[str],
) -> dict:
    return {
        "status": status,
        "source_type": source_type,
        "raw_archive_bytes": raw,
        "uncompressed_total_bytes": uncompressed,
        "kept_tex_bib_bytes": kept,
        "tex_file_count": tex,
        "bib_file_count": bib,
        "kept_files": sorted(files),
    }


def _write_member(dest_dir: Path, rel_path: Path, content: bytes) -> None:
    out_file = dest_dir / rel_path
    out_file.parent.mkdir(parents=True, exist_ok=True)
    out_file.write_bytes(content)


def _extract_tar(raw_bytes: bytes, dest_dir: Path) -> dict:
    with tarfile.open(fileobj=io.BytesIO(raw_bytes), mode="r:*") as tar:
        members = [m for m in tar.getmembers() if m.isfile()]
        uncompressed = sum(max(0, m.size) for m in members)
        kept, tex, bib, files = 0, 0, 0, []
        for member in members:
            relative_path = sanitize_relative_path(member.name)
            if (
                relative_path is None
                or relative_path.suffix.lower() not in ALLOWED_EXTENSIONS
            ):
                continue
            member_file = tar.extractfile(member)
            if member_file is None:
                continue
            content = member_file.read()
            _write_member(dest_dir, relative_path, content)
            kept += len(content)
            tex, bib = (
                (tex + 1, bib)
                if relative_path.suffix.lower() == ".tex"
                else (tex, bib + 1)
            )
            files.append(relative_path.as_posix())
    has_source_files = tex + bib > 0
    return _result(
        "success" if has_source_files else "no_tex_source",
        "tar" if has_source_files else "tar_without_tex",
        len(raw_bytes),
        uncompressed,
        kept,
        tex,
        bib,
        files,
    )


def _extract_zip(raw_bytes: bytes, dest_dir: Path) -> dict:
    with zipfile.ZipFile(io.BytesIO(raw_bytes)) as archive:
        infos = [i for i in archive.infolist() if not i.is_dir()]
        uncompressed = sum(max(0, i.file_size) for i in infos)
        kept, tex, bib, files = 0, 0, 0, []
        for info in infos:
            relative_path = sanitize_relative_path(info.filename)
            if (
                relative_path is None
                or relative_path.suffix.lower() not in ALLOWED_EXTENSIONS
            ):
                continue
            content = archive.read(info.filename)
            _write_member(dest_dir, relative_path, content)
            kept += len(content)
            tex, bib = (
                (tex + 1, bib)
                if relative_path.suffix.lower() == ".tex"
                else (tex, bib + 1)
            )
            files.append(relative_path.as_posix())
    has_source_files = tex + bib > 0
    return _result(
        "success" if has_source_files else "no_tex_source",
        "zip" if has_source_files else "zip_without_tex",
        len(raw_bytes),
        uncompressed,
        kept,
        tex,
        bib,
        files,
    )


def extract_tex_and_bib_from_bytes(raw_bytes: bytes, dest_dir: Path) -> dict:
    """Inspect payload in RAM and extract ONLY .tex & .bib into dest_dir."""
    if not dest_dir.is_dir():
        raise FileNotFoundError(
            f"Missing prepared folder: {dest_dir}. Run entry first."
        )
    raw_size = len(raw_bytes)

    if raw_bytes.startswith(b"%PDF-"):
        return _result("no_tex_source", "pdf_only", raw_size, raw_size, 0, 0, 0, [])

    # 1. tar (.tar.gz, .tgz, .tar)
    try:
        return _extract_tar(raw_bytes, dest_dir)
    except (tarfile.TarError, EOFError, OSError):
        pass

    # 2. zip
    if raw_bytes.startswith(b"PK\x03\x04"):
        try:
            return _extract_zip(raw_bytes, dest_dir)
        except zipfile.BadZipFile:
            pass

    # 3. single gzip file or plain-text TeX
    payload = raw_bytes
    if raw_bytes.startswith(b"\x1f\x8b"):
        try:
            payload = gzip.decompress(raw_bytes)
        except (OSError, EOFError):
            payload = raw_bytes
    if looks_like_latex_or_bib(payload):
        (dest_dir / "main.tex").write_bytes(payload)
        return _result(
            "success",
            "single_tex",
            raw_size,
            len(payload),
            len(payload),
            1,
            0,
            ["main.tex"],
        )

    return _result("no_tex_source", "pdf_or_other", raw_size, len(payload), 0, 0, 0, [])
