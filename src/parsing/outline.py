"""Build document/heading outlines while keeping raw body spans unchanged.

This module consumes a gathered stream. File-control commands have already been
handled per source file by gather; in particular, an ``endinput`` retained in the
stream must not terminate processing of the remainder of its parent file.
"""
from __future__ import annotations

import re
import time
from typing import Any

from .models import GatherResult, OutlineResult, source_diagnostic
from .scanner import Command, iter_commands, scan_errors


HEADING_LEVELS = {
    "part": 0,
    "chapter": 1,
    "section": 2,
    "subsection": 3,
    "subsubsection": 4,
    "paragraph": 5,
    "subparagraph": 6,
}
REFERENCE_TITLES = {"references", "bibliography"}
POST_REFERENCE_TITLES = {
    "acknowledgements", "acknowledgments", "appendix", "appendices",
}
BIBLIOGRAPHY_COMMANDS = {"bibliography", "printbibliography", "bibliographystyle"}


def _literal_title(value: str | None) -> str:
    """Recognize only the literal headings promised by the outline contract."""
    return " ".join((value or "").split()).casefold()


def _conditional_affects_outline(
    raw: str, source_boundaries: list[int] | None = None,
) -> bool:
    """Inspect an opaque conditional without selecting one of its branches."""
    boundaries = source_boundaries or []
    first_seam = min(boundaries, default=len(raw))
    opening = re.match(r"\\[A-Za-z@]+", raw[:first_seam])
    if opening is None:
        return True
    closing = re.search(r"\\fi\s*$", raw)
    inner_start = opening.end()
    inner_end = closing.start() if closing is not None else len(raw)
    inner = raw[inner_start:inner_end]
    inner_boundaries = [
        offset - inner_start for offset in boundaries
        if inner_start < offset < inner_end
    ]
    for command in iter_commands(inner, source_boundaries=inner_boundaries):
        if command.name in HEADING_LEVELS or command.name == "appendix":
            return True
        if command.name in BIBLIOGRAPHY_COMMANDS:
            return True
        if command.name in {"begin", "end"} and command.arg in {
            "document", "thebibliography",
        }:
            return True
        if command.name == "__conditional__" and _conditional_affects_outline(
            command.arg or inner[command.start:command.end],
            [offset - command.start for offset in inner_boundaries
             if command.start < offset < command.end],
        ):
            return True
    return False


def _declared_bib_files(command: Command) -> list[str]:
    if command.name != "bibliography" or not command.arg:
        return []
    # Dynamic declarations remain available verbatim in merged.tex. Resolving or
    # opening their targets belongs to the reference-processing stage.
    return [part.strip() for part in command.arg.split(",") if part.strip()]


def _regions(
    commands: list[Command], body_start: int, body_end: int,
    add_diagnostic: Any,
) -> list[dict[str, Any]]:
    """Find and union bibliography intervals; never remove their raw source."""
    candidates: list[dict[str, Any]] = []
    for index, command in enumerate(commands):
        if command.start >= body_end:
            continue
        if command.name in BIBLIOGRAPHY_COMMANDS:
            candidates.append({
                "kind": "command",
                "span": [command.start, min(command.end, body_end)],
                "declared_bib_files": _declared_bib_files(command),
            })
            continue
        if command.start < body_start:
            continue
        if command.name == "begin" and command.arg == "thebibliography":
            depth = 1
            end = body_end
            for later in commands[index + 1:]:
                if later.start >= body_end:
                    break
                if later.arg == "thebibliography":
                    if later.name == "begin":
                        depth += 1
                    elif later.name == "end":
                        depth -= 1
                        if depth == 0:
                            end = min(later.end, body_end)
                            break
            if depth:
                add_diagnostic(
                    "ambiguous_bibliography_boundary",
                    "The bibliography environment has no matching end; its raw "
                    "source is retained through the end of the available body.",
                    command.start, command.end,
                )
            candidates.append({
                "kind": "environment", "span": [command.start, end],
                "declared_bib_files": [],
            })
        elif command.name in HEADING_LEVELS and _literal_title(
            command.arg
        ) in REFERENCE_TITLES:
            level = HEADING_LEVELS[command.name]
            end = body_end
            for later in commands[index + 1:]:
                if later.start >= body_end:
                    break
                if later.name == "appendix":
                    end = later.start
                    break
                if later.name in HEADING_LEVELS and (
                    HEADING_LEVELS[later.name] <= level
                    or _literal_title(later.arg) in POST_REFERENCE_TITLES
                ):
                    end = later.start
                    break
            candidates.append({
                "kind": "heading", "span": [command.start, end],
                "declared_bib_files": [],
            })

    union: list[dict[str, Any]] = []
    for item in sorted(candidates, key=lambda row: (row["span"][0], -row["span"][1])):
        if union and item["span"][0] <= union[-1]["span"][1]:
            previous = union[-1]
            previous["span"][1] = max(previous["span"][1], item["span"][1])
            for name in item["declared_bib_files"]:
                if name not in previous["declared_bib_files"]:
                    previous["declared_bib_files"].append(name)
        else:
            union.append({
                "kind": item["kind"], "span": list(item["span"]),
                "declared_bib_files": list(item["declared_bib_files"]),
            })
    return union


def _check_structure(result: OutlineResult) -> None:
    """Catch implementation errors without repairing source or raw offsets."""
    if not result.nodes:
        return
    size = len(result.gathered.merged_text)
    seen: set[str] = set()
    for order, node in enumerate(result.nodes):
        if node["node_id"] in seen or node["order"] != order:
            raise ValueError("Outline occurrence IDs/order are inconsistent")
        if order and node["parent_id"] not in seen:
            raise ValueError("Outline parent must precede its child")
        if not 0 <= node["span"][0] <= node["span"][1] <= size:
            raise ValueError("Outline node span is outside merged text")
        seen.add(node["node_id"])
    last_end = 0
    for segment in result.body_segments:
        start, end = segment["span"]
        if segment["parent_id"] not in seen or not last_end <= start < end <= size:
            raise ValueError("Outline body spans are inconsistent")
        last_end = end


def build_outline(gathered: GatherResult) -> OutlineResult:
    """Return a raw document/heading tree, with bibliography stored separately.

    Node IDs identify occurrences inside a version. No sentence splitting,
    cleanup, math normalization, citation rewriting or content deduplication is
    performed here. Body/source offsets always refer to the unchanged gathered
    stream and must not be reused as offsets into a subsequently cleaned string.
    """
    started = time.perf_counter()
    result = OutlineResult(
        gathered=gathered, diagnostics=list(gathered.diagnostics),
        status=gathered.status,
    )

    def finish() -> OutlineResult:
        if result.status not in {"empty", "failed"}:
            result.status = "partial" if (
                gathered.status == "partial"
                or any(item.affects_completeness for item in result.diagnostics)
            ) else "ok"
        result.metrics = {
            "outline_wall_seconds": time.perf_counter() - started,
            "heading_count": sum(node["kind"] == "heading" for node in result.nodes),
            "body_segment_count": len(result.body_segments),
        }
        _check_structure(result)
        return result

    def add_diagnostic(code: str, message: str, start: int, end: int) -> None:
        diagnostic = source_diagnostic(gathered, code, message, start, end)
        key = (diagnostic.code, diagnostic.source_file, diagnostic.source_span)
        if not any(
            (item.code, item.source_file, item.source_span) == key
            for item in result.diagnostics
        ):
            result.diagnostics.append(diagnostic)

    if gathered.status in {"empty", "failed"}:
        return finish()

    text = gathered.merged_text
    # Inclusion seams are lexical file boundaries, even when raw text has no
    # separating whitespace. In particular, a child EOF ends its comment and
    # must not join its control word with text in the returning parent stream.
    source_boundaries = sorted({
        offset
        for item in gathered.source_map
        for offset in item["merged_span"]
        if 0 < offset < len(text)
    })
    commands = list(iter_commands(text, source_boundaries=source_boundaries))
    for start, end, message in scan_errors(text, source_boundaries=source_boundaries):
        add_diagnostic("malformed_latex", message, start, end)
    for command in commands:
        if command.name == "__conditional__" and _conditional_affects_outline(
            command.arg or text[command.start:command.end],
            [offset - command.start for offset in source_boundaries
             if command.start < offset < command.end],
        ):
            add_diagnostic(
                "unresolved_conditional",
                "An unresolved conditional can affect document headings or "
                "bibliography; its source is retained without choosing a branch.",
                command.start, command.end,
            )

    begin = next((
        command for command in commands
        if command.name == "begin" and command.arg == "document"
    ), None)
    if begin is None:
        add_diagnostic(
            "malformed_latex", "No active begin{document} boundary was found.",
            0, min(1, len(text)),
        )
        result.status = "failed"
        return finish()
    body_start = begin.end
    end = next((
        command for command in commands
        if command.start >= body_start
        and command.name == "end" and command.arg == "document"
    ), None)
    body_end = end.start if end is not None else len(text)
    if end is None:
        add_diagnostic(
            "malformed_latex",
            "No matching end{document} boundary was found; the available body "
            "is retained through the end of the merged source.",
            begin.start, begin.end,
        )
    for command in commands:
        if command.start >= body_start and command.start < body_end and (
            command.name == "begin" and command.arg == "document"
        ):
            add_diagnostic(
                "malformed_latex", "Nested begin{document} is not supported.",
                command.start, command.end,
            )

    regions = _regions(commands, body_start, body_end, add_diagnostic)
    result.bibliography_regions = regions
    root_id = f"{gathered.publication_key}@v{gathered.version}#document"
    root = {
        "node_id": root_id, "kind": "document", "parent_id": None,
        "order": 0, "span": [body_start, body_end],
    }
    result.nodes.append(root)
    stack = [root]
    appendix = False
    cursor = body_start

    def emit_body(parent_id: str, start: int, stop: int) -> None:
        position = start
        for region in regions:
            left, right = region["span"]
            if right <= position:
                continue
            if left >= stop:
                break
            if position < left:
                result.body_segments.append({
                    "parent_id": parent_id, "span": [position, min(left, stop)],
                })
            position = max(position, right)
            if position >= stop:
                return
        if position < stop:
            result.body_segments.append({"parent_id": parent_id, "span": [position, stop]})

    for command in commands:
        if command.start < body_start or command.start >= body_end:
            continue
        if command.name not in HEADING_LEVELS and command.name != "appendix":
            continue
        region = next((
            item for item in regions
            if item["span"][0] <= command.start < item["span"][1]
        ), None)
        if region is not None:
            # A references heading closes the previous section even though the
            # heading itself is not a node in the exported outline.
            if command.name in HEADING_LEVELS and (
                _literal_title(command.arg) in REFERENCE_TITLES
            ):
                emit_body(stack[-1]["node_id"], cursor, command.start)
                level = HEADING_LEVELS[command.name]
                while len(stack) > 1 and stack[-1]["level"] >= level:
                    stack.pop()
                cursor = command.end
            continue

        emit_body(stack[-1]["node_id"], cursor, command.start)
        if command.name == "appendix":
            stack = [root]
            appendix = True
            # Keep the appendix command as raw body under document.
            cursor = command.start
            continue
        if command.arg is None:
            add_diagnostic(
                "malformed_latex", "A heading command has no required title.",
                command.start, command.end,
            )
            cursor = command.start
            continue
        level = HEADING_LEVELS[command.name]
        while len(stack) > 1 and stack[-1]["level"] >= level:
            stack.pop()
        order = len(result.nodes)
        node = {
            "node_id": f"{gathered.publication_key}@v{gathered.version}#h{order:06d}",
            "kind": "heading", "parent_id": stack[-1]["node_id"],
            "order": order, "heading_command": command.name, "level": level,
            "title_raw": command.arg, "short_title_raw": command.optional,
            "starred": command.starred, "appendix": appendix,
            "span": [command.start, command.end],
        }
        result.nodes.append(node)
        stack.append(node)
        cursor = command.end
    emit_body(stack[-1]["node_id"], cursor, body_end)
    return finish()
