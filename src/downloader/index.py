"""Read the completed index exported by entry."""

import json
from pathlib import Path


def load_papers_index(index_path: Path) -> dict[str, list[str]]:
    """Load {arxiv_id: [v1, v2, ...]} without creating or modifying input."""
    index_path = Path(index_path)
    if not index_path.is_file():
        raise FileNotFoundError(f"Missing entry index: {index_path}. Run entry first.")
    return json.loads(index_path.read_text(encoding="utf-8"))
