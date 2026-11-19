"""Read `<publication_key>/v<N>/cleaned_nodes.json` from a directory or a .zip, one publication at a time."""

from __future__ import annotations

from collections import defaultdict
import json
from pathlib import Path
import re
from typing import Any, Callable, Iterable, Iterator
import zipfile

PUB_VER_RE = re.compile(r"(?:^|/)(\d{4}-\d{4,5})/v(\d+)/cleaned_nodes\.json$")

PublicationIter = Iterator[tuple[str, list[dict[str, Any]]]]


def _index(paths: Iterable[str], limit: int | None) -> tuple[list[str], dict[str, dict[int, str]]]:
    """File names -> sorted publication keys and {pub_key: {version: name}} (first copy in sorted order wins)."""
    by_pub: dict[str, dict[int, str]] = defaultdict(dict)
    for name in sorted(paths):
        match = PUB_VER_RE.search(name.replace("\\", "/"))
        if match:
            by_pub[match.group(1)].setdefault(int(match.group(2)), name)
    keys = sorted(by_pub)
    return (keys[:limit] if limit is not None else keys), by_pub


def _load_versions(read: Callable[[str], bytes], versions: list[tuple[int, str]]) -> list[dict[str, Any]]:
    """Parse the version documents. The version number of the PATH is authoritative (the JSON may lack it)."""
    docs = []
    for version, name in versions:
        doc = json.loads(read(name).decode("utf-8"))
        doc["version"] = version
        docs.append(doc)
    return docs


def _open_directory(root: Path, limit: int | None, skip: frozenset[str]) -> tuple[int, PublicationIter]:
    if not root.is_dir():
        raise FileNotFoundError(f"Input directory not found: {root}")
    keys, by_pub = _index((str(p.relative_to(root)) for p in root.rglob("cleaned_nodes.json")), limit)
    if not keys:
        raise FileNotFoundError(f"No <pub>/v<N>/cleaned_nodes.json found under {root}")

    def read(name: str) -> bytes:
        return (root / name).read_bytes()

    def generate() -> PublicationIter:
        for key in keys:
            if key not in skip:
                yield key, _load_versions(read, sorted(by_pub[key].items()))

    return len(keys), generate()


def _open_zip(path: Path, limit: int | None, skip: frozenset[str]) -> tuple[int, PublicationIter]:
    if not path.is_file():
        raise FileNotFoundError(f"Archive not found: {path}")
    with zipfile.ZipFile(path) as zf:
        keys, by_pub = _index(zf.namelist(), limit)
    if not keys:
        raise ValueError(f"No <pub>/v<N>/cleaned_nodes.json entries inside {path}")

    def generate() -> PublicationIter:
        with zipfile.ZipFile(path) as zf:
            for key in keys:
                if key not in skip:
                    yield key, _load_versions(zf.read, sorted(by_pub[key].items()))

    return len(keys), generate()


def open_publications(
    source: Path, limit: int | None = None, skip: Iterable[str] = ()
) -> tuple[int, PublicationIter]:
    """Return (publication count, iterator of (publication_key, [version documents sorted by version])).

    The count covers every publication; the keys in `skip` (already processed in an earlier, interrupted run)
    are not read again by the iterator.
    """
    opener: Callable[[Path, int | None, frozenset[str]], tuple[int, PublicationIter]]
    opener = _open_zip if source.is_file() and source.suffix.lower() == ".zip" else _open_directory
    return opener(source, limit, frozenset(skip))
