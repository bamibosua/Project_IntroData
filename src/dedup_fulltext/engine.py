"""Cross-version full-text deduplication of the hierarchical elements of ONE publication.

Input  : the `cleaned_nodes.json` documents (already cleaned and normalized) of every version.
Output : `elements` rows (one per distinct element) and `hierarchy` rows (one node-parent edge per version).

Rules
- Two nodes of the same kind whose cleaned text matches (see `normalize.match_key`) share ONE `element_id`
  when they belong to different versions.
- Inside a single version every node keeps its own element: a sentence repeated twice in one version is
  still two nodes of that version's tree. The k-th occurrence of a text in a version is matched with the
  k-th occurrence in earlier versions, so each version tree is reproduced by its hierarchy rows.
- Nodes without text and the document root are never shared across versions.
- Paragraphs, lists and items (see `compact_containers`) are matched through their children, not their text.
- The hierarchy has no sibling-order column (the table is fixed by the specification): the rows of one version
  are written in document order and that ROW ORDER is what restores the order of siblings. Keep it when the
  rows are converted to Parquet (no shuffling, no re-sorting by element_id).
"""

from __future__ import annotations

from collections import Counter, defaultdict
from typing import Any

from .normalize import match_key, rewrite_citations, tidy_content

KIND_PREFIX = {
    "heading": "h",
    "paragraph": "p",
    "sentence": "s",
    "block_formula": "eq",
    "figure": "fig",
    "table": "tab",
    "list": "lst",
    "item": "itm",
}
LEAF_KINDS = ("sentence", "block_formula", "figure", "table")
ALL_KINDS = ("document", "heading", "paragraph", "sentence", "block_formula", "figure", "table", "list", "item")
# Containers whose text is just the concatenation of their children (the cleanup stage repeats it in `content`).
# With `compact_containers` their content is left empty and they are matched through their children instead.
CONTAINER_KINDS = frozenset({"paragraph", "list", "item"})
# LaTeX-heavy kinds: keep line breaks in the stored content and do not NFKC-fold the match key.
RAW_KINDS = frozenset({"block_formula", "figure", "table"})


def effective_kind(node: dict[str, Any]) -> str:
    """A table is stored by the cleanup as `figure` with subtype `table`; report it as its own kind."""
    kind = node.get("kind") or "unknown"
    if kind == "figure" and node.get("subtype") == "table":
        return "table"
    return kind


def _doc_version(doc: dict[str, Any]) -> int:
    raw = doc.get("version", 1)
    if isinstance(raw, str):
        raw = raw.strip().lstrip("vV")
    try:
        return int(raw)
    except (TypeError, ValueError):
        return 1


def deduplicate_publication(
    publication_key: str,
    version_docs: list[dict[str, Any]],
    cite_key_map: dict[str, str] | None = None,
    compact_containers: bool = True,
) -> dict[str, Any]:
    """Deduplicate the elements of all versions of one publication (versions are processed in order).

    compact_containers: a paragraph / list / item that has children is stored with an empty `content` and is
    shared across versions exactly when its ordered children are shared (its text is the children's text, so
    nothing is lost and the text is stored once instead of up to four times). A container without children
    keeps its own text and is matched by it.
    """
    arxiv_id = publication_key.replace("-", ".", 1)
    docs = sorted(version_docs, key=_doc_version)

    elements: list[dict[str, Any]] = []
    hierarchy: list[dict[str, Any]] = []
    versions: list[dict[str, Any]] = []
    pool: dict[tuple[str, str], list[str]] = {}  # (kind, text key) -> element ids, in creation order
    seq: Counter[str] = Counter()
    before: Counter[str] = Counter()
    after: Counter[str] = Counter()
    unknown: Counter[str] = Counter()
    seen_versions: set[int] = set()
    edges = orphans = ok_versions = empty_versions = leaf_mismatch = duplicates = 0

    def new_element(prefix: str, kind: str, content: str) -> str:
        seq[prefix] += 1
        element_id = f"{publication_key}#{prefix}{seq[prefix]}"
        elements.append({"element_id": element_id, "arxiv_id": arxiv_id, "kind": kind, "content": content})
        after[kind] += 1
        return element_id

    def new_root(version: int, content: str) -> str:
        element_id = f"{publication_key}#root{version}"
        elements.append({"element_id": element_id, "arxiv_id": arxiv_id, "kind": "document", "content": content})
        after["document"] += 1
        return element_id

    for doc in docs:
        version = _doc_version(doc)
        if version in seen_versions:
            # Two documents claiming the same version would collide on `#root<version>`: keep the first one.
            duplicates += 1
            versions.append({"version": version, "status": "duplicate_skipped", "nodes": 0})
            continue
        seen_versions.add(version)

        status = doc.get("status", "ok")
        nodes = doc.get("nodes") or []
        has_content = any(effective_kind(n) != "document" for n in nodes)
        if status != "ok" or not has_content:
            # No parsed elements: keep the version visible as an empty record (coverage statistics).
            empty_versions += 1
            versions.append({"version": version, "status": status if status != "ok" else "empty", "nodes": 0})
            continue
        ok_versions += 1

        nodes = sorted(nodes, key=lambda n: n.get("m2_traversal_index", 0))
        local: dict[str, str] = {}  # node_id of this version -> element_id
        children: dict[str, list[dict[str, Any]]] = defaultdict(list)
        for node in nodes:
            if node.get("parent_id") is not None:
                children[node["parent_id"]].append(node)
        occurrence: Counter[tuple[str, Any]] = Counter()
        version_kinds: Counter[str] = Counter()
        reused = 0

        def assign(key: tuple[str, Any], prefix: str, kind: str, content: str) -> str:
            """k-th occurrence of `key` in this version -> k-th element created for it in earlier versions."""
            nonlocal reused
            ids = pool.setdefault(key, [])
            index = occurrence[key]
            occurrence[key] += 1
            if index < len(ids):
                reused += 1
                return ids[index]
            element_id = new_element(prefix, kind, content)
            ids.append(element_id)
            return element_id

        def resolve(node: dict[str, Any]) -> str:
            node_id = node["node_id"]
            if node_id in local:
                return local[node_id]
            kind = effective_kind(node)
            if kind == "document":
                content = tidy_content(node.get("content") or "") or f"Version {version} root"
                local[node_id] = new_root(version, content)
                return local[node_id]

            prefix = KIND_PREFIX.get(kind, "el")
            kids = children.get(node_id)
            if compact_containers and kind in CONTAINER_KINDS and kids:
                child_ids = tuple(resolve(kid) for kid in kids)  # children first, then the container
                local[node_id] = assign((kind, child_ids), prefix, kind, "")
                return local[node_id]

            raw = rewrite_citations(node.get("content") or "", cite_key_map)
            content = tidy_content(raw, keep_layout=kind in RAW_KINDS)
            key_text = match_key(content, fold=kind not in RAW_KINDS)
            if not key_text:
                local[node_id] = new_element(prefix, kind, content)
            else:
                local[node_id] = assign((kind, key_text), prefix, kind, content)
            return local[node_id]

        for node in nodes:
            kind = effective_kind(node)
            before[kind] += 1
            version_kinds[kind] += 1
            if kind != "document" and kind not in KIND_PREFIX:
                unknown[kind] += 1
            resolve(node)

        version_edges = 0
        for node in nodes:
            parent = node.get("parent_id")
            if parent is None:
                if effective_kind(node) != "document":
                    orphans += 1  # a content node without parent is silently lost from the tree otherwise
                continue
            child_id, parent_id = local.get(node["node_id"]), local.get(parent)
            if child_id is None or parent_id is None:
                orphans += 1
                continue
            hierarchy.append({"arxiv_id": arxiv_id, "version": version, "element_id": child_id, "parent_id": parent_id})
            version_edges += 1
        edges += version_edges

        # Cross-check against the leaf total declared by the cleanup stage, when it is present.
        leaves = sum(version_kinds[k] for k in LEAF_KINDS)
        expected = doc.get("total_leaves")
        mismatch = isinstance(expected, int) and expected != leaves
        leaf_mismatch += int(mismatch)
        versions.append({"version": version, "status": "ok", "nodes": len(nodes), "edges": version_edges,
                         "reused_from_earlier_versions": reused, "leaves": leaves,
                         "leaves_expected": expected if isinstance(expected, int) else None,
                         "leaf_mismatch": mismatch})

    content_before = sum(c for k, c in before.items() if k != "document")
    content_after = sum(c for k, c in after.items() if k != "document")
    return {
        "publication_key": publication_key,
        "arxiv_id": arxiv_id,
        "version_count": len(docs),
        "ok_versions": ok_versions,
        "empty_versions": empty_versions,
        "versions": versions,
        "elements": elements,
        "hierarchy": hierarchy,
        "stats": {
            "nodes_before": content_before,
            "nodes_after": content_after,
            "leaves_before": sum(before[k] for k in LEAF_KINDS),
            "leaves_after": sum(after[k] for k in LEAF_KINDS),
            "edges": edges,
            "orphan_edges": orphans,
            "leaf_mismatch_versions": leaf_mismatch,
            "duplicate_versions": duplicates,
            "unknown_kinds": dict(unknown),
            "before_by_kind": dict(before),
            "after_by_kind": dict(after),
        },
    }
