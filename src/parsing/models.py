"""Intermediate outline records; offsets are Unicode characters in LF text."""
from __future__ import annotations

from dataclasses import asdict, dataclass, field
from typing import Any

SCHEMA_VERSION = 1
STAGE = "outline"


@dataclass
class Diagnostic:
    code: str
    message: str
    severity: str = "warning"
    source_file: str | None = None
    source_span: list[int] | None = None
    line: int | None = None
    column: int | None = None
    affects_completeness: bool = True


@dataclass
class GatherResult:
    publication_key: str
    version: int
    root_file: str | None = None
    merged_text: str = ""
    compilation_path: list[dict[str, Any]] = field(default_factory=list)
    source_map: list[dict[str, Any]] = field(default_factory=list)
    diagnostics: list[Diagnostic] = field(default_factory=list)
    status: str = "ok"
    dependencies: list[str] = field(default_factory=list)
    source_texts: dict[str, str] = field(default_factory=dict, repr=False)
    source_hashes: dict[str, str] = field(default_factory=dict, repr=False)
    metrics: dict[str, Any] = field(default_factory=dict)


@dataclass
class OutlineResult:
    gathered: GatherResult
    nodes: list[dict[str, Any]] = field(default_factory=list)
    body_segments: list[dict[str, Any]] = field(default_factory=list)
    bibliography_regions: list[dict[str, Any]] = field(default_factory=list)
    diagnostics: list[Diagnostic] = field(default_factory=list)
    status: str = "ok"
    metrics: dict[str, Any] = field(default_factory=dict)

    def to_record(self) -> dict[str, Any]:
        g = self.gathered
        return {
            "schema_version": SCHEMA_VERSION,
            "stage": STAGE,
            "publication_key": g.publication_key,
            "version": g.version,
            "status": self.status,
            "root_file": g.root_file,
            "root_node_id": self.nodes[0]["node_id"] if self.nodes else None,
            "compilation_path": g.compilation_path,
            "nodes": self.nodes,
            "body_segments": self.body_segments,
            "source_map": g.source_map,
            "bibliography_regions": self.bibliography_regions,
            "diagnostics": [asdict(d) for d in self.diagnostics],
            "dependencies": sorted(set(g.dependencies)),
            "source_hashes": dict(sorted(g.source_hashes.items())),
            "metrics": {**g.metrics, **self.metrics},
        }


def source_diagnostic(
    gathered: GatherResult, code: str, message: str, start: int, end: int,
    *, severity: str = "warning", affects_completeness: bool = True,
) -> Diagnostic:
    """Locate a merged offset without reopening any source file."""
    for item in gathered.source_map:
        lo, hi = item["merged_span"]
        if lo <= start < hi:
            origin = item["source_span"][0] + start - lo
            origin_end = min(origin + end - start, item["source_span"][1])
            text = gathered.source_texts.get(item["source_file"], "")
            return Diagnostic(
                code, message, severity, item["source_file"], [origin, origin_end],
                text.count("\n", 0, origin) + 1,
                origin - text.rfind("\n", 0, origin), affects_completeness,
            )
    return Diagnostic(code, message, severity, affects_completeness=affects_completeness)
