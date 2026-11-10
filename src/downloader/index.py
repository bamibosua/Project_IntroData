"""Loading of the input index {"<arxiv_id>": ["v1", "v2", ...]}."""

from __future__ import annotations

import json
from pathlib import Path

from .config import DEFAULT_LEGACY_JSON


def load_papers_index(index_path: Path) -> dict[str, list[str]]:
    """Load paper index mapping {"2608.09608": ["v1", "v2"]}.

    If the index does not exist yet, it is built once from the legacy discovery file.
    """
    if not index_path.exists() and DEFAULT_LEGACY_JSON.exists():
        raw = json.loads(DEFAULT_LEGACY_JSON.read_text(encoding="utf-8"))
        idx_dict = {}
        for p in raw:
            vers = [f"v{v.split('v')[-1]}" for v in p.get("versions", [])]
            if not vers:
                vers = [f"v{i}" for i in range(1, int(p.get("latest_version", 1)) + 1)]
            idx_dict[p["arxiv_id"]] = vers
        index_path.parent.mkdir(parents=True, exist_ok=True)
        index_path.write_text(json.dumps(idx_dict, indent=2), encoding="utf-8")
        return idx_dict
    return json.loads(index_path.read_text(encoding="utf-8"))
