"""Read and verify the raw outline handoff, without interpreting or cleaning TeX."""
from __future__ import annotations

import hashlib
import json
from pathlib import Path, PurePosixPath
import re

from .models import SCHEMA_VERSION, STAGE, decode_source_bytes
from .outline import HEADING_LEVELS


def _relative(value) -> bool:
    return (isinstance(value, str) and bool(value) and "\\" not in value
            and not PurePosixPath(value).is_absolute() and ":" not in value
            and ".." not in PurePosixPath(value).parts)


def load_outline(directory: Path, *, source_dir: Path | None = None) -> tuple[dict, str]:
    """Load an artifact pair; raise ValueError/OSError for corrupt or inconsistent output.

    With source_dir, also verify every stored source hash and mapped substring.
    Partial/failed are valid quality statuses, not validation failures.
    """
    from .runner import STATUSES, _validate_record

    directory = Path(directory)
    record = json.loads((directory / "outline.json").read_text(encoding="utf-8"))
    raw = (directory / "merged.tex").read_bytes()
    text = raw.decode("utf-8")
    if not isinstance(record, dict):
        raise ValueError("outline must be a JSON object")
    if (record.get("schema_version") != SCHEMA_VERSION or record.get("stage") != STAGE
            or record.get("status") not in STATUSES
            or not isinstance(record.get("publication_key"), str)
            or type(record.get("version")) is not int or record["version"] <= 0):
        raise ValueError("invalid schema, stage, identity or status")
    if raw.startswith(b"\xef\xbb\xbf") or "\r" in text:
        raise ValueError("merged.tex must be UTF-8 without BOM, with LF newlines")
    if hashlib.sha256(raw).hexdigest() != record.get("merged_sha256"):
        raise ValueError("merged SHA-256 mismatch")
    if not re.fullmatch(r"[0-9a-f]{64}", record.get("input_fingerprint", "")):
        raise ValueError("missing/invalid input fingerprint")
    if not _validate_record(record, text):
        raise ValueError("invalid outline tree, spans, diagnostics or source map")
    dependencies = record.get("dependencies")
    hashes = record.get("source_hashes", {})
    if (not isinstance(dependencies, list) or not all(_relative(p) for p in dependencies)
            or not isinstance(hashes, dict)
            or not all(_relative(p) and isinstance(h, str) and re.fullmatch(r"[0-9a-f]{64}", h)
                       for p, h in hashes.items())):
        raise ValueError("invalid dependencies/source hashes")
    nodes = record["nodes"]
    if record["status"] in {"ok", "partial"} and not nodes:
        raise ValueError("ok/partial record must contain a document")
    if record["status"] in {"empty", "failed"} and (nodes or record["body_segments"] or record.get("root_node_id")):
        raise ValueError("empty/failed record must not contain a tree")
    prefix = f"{record['publication_key']}@v{record['version']}#"
    by_id = {n["node_id"]: n for n in nodes}
    if nodes:
        root = nodes[0]
        if (root.get("kind") != "document" or root.get("parent_id") is not None
                or root["node_id"] != prefix + "document"
                or record["root_node_id"] != root["node_id"]):
            raise ValueError("invalid document root")
        previous_end = root["span"][0]
        for order, node in enumerate(nodes[1:], 1):
            parent = by_id[node["parent_id"]]
            command = node.get("heading_command")
            if (node["node_id"] != prefix + f"h{order:06d}"
                    or command not in HEADING_LEVELS or node.get("level") != HEADING_LEVELS[command]
                    or not isinstance(node.get("title_raw"), str)
                    or (node.get("short_title_raw") is not None and not isinstance(node["short_title_raw"], str))
                    or type(node.get("starred")) is not bool or type(node.get("appendix")) is not bool
                    or node["span"][0] < previous_end or node["span"][1] > root["span"][1]
                    or (parent["kind"] == "heading" and parent["level"] >= node["level"])):
                raise ValueError("invalid heading identity, level or order")
            previous_end = node["span"][1]
        for segment in record["body_segments"]:
            start, end = segment["span"]
            parent = by_id[segment["parent_id"]]
            if (not root["span"][0] <= start < end <= root["span"][1]
                    or (parent["kind"] == "heading" and start < parent["span"][1])):
                raise ValueError("body segment outside document/parent")
    instances = {}
    for instance in record["compilation_path"]:
        identity = instance.get("file_instance_id")
        if (not isinstance(identity, str) or identity in instances
                or not _relative(instance.get("source_file"))
                or (instances and instance.get("parent_instance_id") not in instances)
                or (not instances and instance.get("parent_instance_id") is not None)):
            raise ValueError("invalid compilation path")
        instances[identity] = instance
    source_texts = {}
    if source_dir is not None:
        source_dir = Path(source_dir).resolve()
        for name, digest in hashes.items():
            path = (source_dir / name).resolve()
            if source_dir not in path.parents:
                raise ValueError("source path escapes version directory")
            data = path.read_bytes()
            if hashlib.sha256(data).hexdigest() != digest:
                raise ValueError(f"source hash mismatch: {name}")
            try:
                source_texts[name], _ = decode_source_bytes(data)
            except UnicodeDecodeError:
                # An unreadable source can be recorded with an explicit diagnostic.
                pass
    for mapping in record["source_map"]:
        instance = instances.get(mapping.get("file_instance_id"))
        name = mapping.get("source_file")
        if instance is None or instance["source_file"] != name:
            raise ValueError("source map refers to an unknown file instance")
        if source_dir is not None:
            if name not in source_texts:
                raise ValueError(f"mapped source not present in source_hashes: {name}")
            lo, hi = mapping["source_span"]
            start, end = mapping["merged_span"]
            if source_texts[name][lo:hi] != text[start:end]:
                raise ValueError(f"source map content mismatch: {name}")
    return record, text


def get_body(record: dict, merged: str, node_id: str) -> str:
    """Return direct raw body segments for one document/heading, in source order."""
    if not any(n["node_id"] == node_id for n in record["nodes"]):
        raise ValueError(f"Unknown node: {node_id}")
    return "".join(merged[s["span"][0]:s["span"][1]]
                   for s in record["body_segments"] if s["parent_id"] == node_id)


def output_directories(output_dir: Path):
    """Yield all version directories, including incomplete artifact pairs."""
    for publication in sorted(Path(output_dir).iterdir()):
        if publication.is_dir() and not publication.is_symlink():
            for directory in sorted(publication.iterdir()):
                if re.fullmatch(r"v[1-9][0-9]*", directory.name) and directory.is_dir() and not directory.is_symlink():
                    yield publication.name, int(directory.name[1:]), directory


def load_empty_publication(path: Path) -> dict:
    record = json.loads(Path(path).read_text(encoding="utf-8"))
    if (not isinstance(record, dict) or record.get("schema_version") != SCHEMA_VERSION
            or record.get("stage") != STAGE or record.get("scope") != "publication"
            or record.get("publication_key") != Path(path).parent.name
            or record.get("version") is not None or record.get("status") != "empty"
            or record.get("reason") != "no_version_directories" or record.get("versions") != []
            or record.get("nodes") != [] or record.get("body_segments") != []
            or record.get("root_node_id") is not None):
        raise ValueError("invalid empty publication record")
    return record
