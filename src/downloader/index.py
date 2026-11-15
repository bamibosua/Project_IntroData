"""Read the completed index exported by entry."""

import json
import re
from pathlib import Path

_ID_RE = re.compile(r"\d{4}\.\d{4,5}")
_VERSION_RE = re.compile(r"v\d+")


def load_papers_index(index_path: Path) -> dict[str, list[str]]:
    """Load {arxiv_id: [v1, v2, ...]} without creating or modifying input; reject malformed entries.

    Old-style IDs (e.g. `hep-th/9901001`) are refused: `aid.replace('.', '-')` would turn them into nested
    paths. Entry only emits new-style IDs, so anything else signals a corrupt/foreign index.
    """
    index_path = Path(index_path)
    if not index_path.is_file():
        raise FileNotFoundError(f'Missing entry index: {index_path}. Run entry first.')
    index = json.loads(index_path.read_text(encoding="utf-8"))
    if not isinstance(index, dict):
        raise ValueError(f'Index must be a JSON object {{arxiv_id: [versions]}}: {index_path}')
    for aid, versions in index.items():
        if not isinstance(aid, str) or not _ID_RE.fullmatch(aid):
            raise ValueError(f'Invalid arXiv ID in index: {aid!r} (expected YYMM.NNNNN)')
        if (not isinstance(versions, list) or not versions
                or not all(isinstance(v, str) and _VERSION_RE.fullmatch(v) for v in versions)):
            raise ValueError(f'Invalid version list for {aid}: {versions!r} (expected ["v1", "v2", ...])')
    return index
