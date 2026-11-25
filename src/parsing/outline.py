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
from .scanner import Command, DEFINITION_COMMANDS, iter_commands, scan_errors


HEADING_LEVELS = {
    "part": 0,
    "chapter": 1,
    "section": 2,
    "subsection": 3,
    "subsubsection": 4,
    "paragraph": 5,
    "subparagraph": 6,
}
REFERENCE_TITLES = {
    "references",
    "bibliography",
    "references and notes",
    "literature cited",
    "works cited",
    "reference list",
    "selected references",
    "bibliography and references",
}
POST_REFERENCE_TITLES = {
    "acknowledgements", "acknowledgments", "appendix", "appendices",
}
BIBLIOGRAPHY_COMMANDS = {
    "bibliography",
    "addbibresource",
    "printbibliography",
    "bibliographystyle",
}
BIBLIOGRAPHY_ENVIRONMENTS = {
    "thebibliography",
    "bibdiv",
    "biblist",
    "biblist*",
}
_TITLE_UNWRAP_MACROS = frozenset({
    "textbf", "textit", "emph", "textsc", "textrm", "textsf", "texttt",
    "textnormal", "textup", "textsl", "textmd", "underline", "mbox",
    "hbox", "uppercase", "MakeUppercase", "MakeTextUppercase",
    "lowercase", "MakeLowercase", "MakeTextLowercase",
})
_TITLE_SWITCH_MACROS = frozenset({
    "bf", "bfseries", "it", "itshape", "em", "sc", "scshape", "rm",
    "rmfamily", "sf", "sffamily", "tt", "ttfamily", "sl", "slshape",
    "upshape", "mdseries", "normalfont", "tiny", "scriptsize",
    "footnotesize", "small", "normalsize", "large", "Large", "LARGE",
    "huge", "Huge", "centering", "noindent", "protect",
})
_TITLE_DROP_ARG_MACROS = frozenset({"label"})
REFERENCE_TITLE_ALIASES = frozenset({"refname", "bibname"})
_DEFAULT_REFERENCE_ALIASES: dict[str, str | None] = {
    "refname": "references",
    "bibname": "bibliography",
}


def _read_braced(text: str, pos: int) -> tuple[str | None, int]:
    """Read one balanced `{...}` group after whitespace/comments, or return `(None, pos)`."""
    size = len(text)
    cursor = pos
    while cursor < size:
        if text[cursor].isspace():
            cursor += 1
        elif text[cursor] == "%":
            newline = text.find("\n", cursor)
            cursor = size if newline < 0 else newline + 1
        else:
            break
    if cursor >= size or text[cursor] != "{":
        return None, pos
    depth = 1
    inner_start = cursor + 1
    cursor = inner_start
    while cursor < size:
        char = text[cursor]
        if char == "\\":
            cursor += 2 if cursor + 1 < size else 1
        elif char == "%":
            newline = text.find("\n", cursor)
            cursor = size if newline < 0 else newline + 1
        elif char == "{":
            depth += 1
            cursor += 1
        elif char == "}":
            depth -= 1
            if depth == 0:
                return text[inner_start:cursor], cursor + 1
            cursor += 1
        else:
            cursor += 1
    return None, pos


def _strip_title_formatting(
    text: str, aliases: dict[str, str | None] | None = None,
) -> tuple[str, bool]:
    """Strip allowlisted LaTeX formatting macros/switches and `\\label{...}` for title matching."""
    out: list[str] = []
    unresolved = False
    size = len(text)
    cursor = 0
    while cursor < size:
        char = text[cursor]
        if char == "%":
            newline = text.find("\n", cursor)
            cursor = size if newline < 0 else newline + 1
            continue
        if char == "~":
            out.append(" ")
            cursor += 1
            continue
        if char == "{":
            inner, after = _read_braced(text, cursor)
            if inner is not None:
                stripped_inner, inner_unresolved = _strip_title_formatting(inner, aliases)
                out.append(stripped_inner)
                unresolved = unresolved or inner_unresolved
                cursor = after
            else:
                out.append("{")
                cursor += 1
            continue
        if char == "\\" and cursor + 1 < size:
            nxt = text[cursor + 1]
            if nxt.isalpha() or nxt == "@":
                cmd_end = cursor + 2
                while cmd_end < size and (text[cmd_end].isalpha() or text[cmd_end] == "@"):
                    cmd_end += 1
                cmd = text[cursor + 1:cmd_end]
                if aliases is not None and cmd in aliases:
                    replacement = aliases[cmd]
                    if replacement is not None:
                        out.append(replacement)
                    else:
                        out.append(text[cursor:cmd_end])
                        unresolved = True
                    cursor = cmd_end
                elif cmd in _TITLE_SWITCH_MACROS:
                    cursor = cmd_end
                elif cmd in _TITLE_UNWRAP_MACROS:
                    inner, after = _read_braced(text, cmd_end)
                    if inner is not None:
                        stripped_inner, inner_unresolved = _strip_title_formatting(inner, aliases)
                        out.append(stripped_inner)
                        unresolved = unresolved or inner_unresolved
                        cursor = after
                    else:
                        out.append(text[cursor:cmd_end])
                        cursor = cmd_end
                elif cmd in _TITLE_DROP_ARG_MACROS:
                    inner, after = _read_braced(text, cmd_end)
                    if inner is not None:
                        cursor = after
                    else:
                        out.append(text[cursor:cmd_end])
                        cursor = cmd_end
                else:
                    out.append(text[cursor:cmd_end])
                    cursor = cmd_end
                continue
            if nxt == " ":
                out.append(" ")
                cursor += 2
                continue
            out.append(text[cursor:cursor + 2])
            cursor += 2
            continue
        out.append(char)
        cursor += 1
    return "".join(out), unresolved


def _resolve_title(
    value: str | None, aliases: dict[str, str | None] | None = None,
) -> tuple[str, bool]:
    """Normalize a heading title and report whether a reference alias was unresolved."""
    if not value:
        return "", False
    stripped, unresolved = _strip_title_formatting(value, aliases)
    return " ".join(stripped.split()).casefold(), unresolved


def _normalize_title(
    value: str | None, aliases: dict[str, str | None] | None = None,
) -> str:
    """Normalize a heading title for reference/post-reference matching without mutating title_raw."""
    return _resolve_title(value, aliases)[0]


def _alias_redefinition(
    command: Command, current_aliases: dict[str, str | None],
) -> str | None:
    if command.definition_body is None:
        return None
    stripped, unresolved = _strip_title_formatting(command.definition_body, current_aliases)
    if unresolved or "\\" in stripped or "#" in stripped:
        return None
    return " ".join(stripped.split()).casefold()


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
        if command.name in DEFINITION_COMMANDS and command.defined_name in REFERENCE_TITLE_ALIASES:
            return True
        if command.name in {"begin", "end"} and command.arg in (
            {"document"} | BIBLIOGRAPHY_ENVIRONMENTS
        ):
            return True
        if command.name == "__conditional__" and _conditional_affects_outline(
            command.arg or inner[command.start:command.end],
            [offset - command.start for offset in inner_boundaries
             if command.start < offset < command.end],
        ):
            return True
    return False


def _declared_bib_files(command: Command) -> list[str]:
    if not command.arg:
        return []
    # Dynamic declarations remain available verbatim in merged.tex. Resolving or
    # opening their targets belongs to the reference-processing stage.
    if command.name == "bibliography":
        return [part.strip() for part in command.arg.split(",") if part.strip()]
    if command.name == "addbibresource":
        resource = command.arg.strip()
        return [resource] if resource else []
    return []


def _conditional_redefined_aliases(
    raw: str, source_boundaries: list[int] | None = None,
) -> set[str]:
    """Return reference-title aliases redefined inside an opaque conditional."""
    boundaries = source_boundaries or []
    first_seam = min(boundaries, default=len(raw))
    opening = re.match(r"\\[A-Za-z@]+", raw[:first_seam])
    if opening is None:
        return set(REFERENCE_TITLE_ALIASES)
    closing = re.search(r"\\fi\s*$", raw)
    inner_start = opening.end()
    inner_end = closing.start() if closing is not None else len(raw)
    inner = raw[inner_start:inner_end]
    inner_boundaries = [
        offset - inner_start for offset in boundaries
        if inner_start < offset < inner_end
    ]
    redefined: set[str] = set()
    for command in iter_commands(inner, source_boundaries=inner_boundaries):
        if command.name in DEFINITION_COMMANDS and command.defined_name in REFERENCE_TITLE_ALIASES:
            redefined.add(command.defined_name)
        elif command.name == "__conditional__":
            redefined.update(_conditional_redefined_aliases(
                command.arg or inner[command.start:command.end],
                [offset - command.start for offset in inner_boundaries
                 if command.start < offset < command.end],
            ))
    return redefined


def _resolve_heading_titles(
    commands: list[Command], body_start: int, body_end: int,
    source_boundaries: list[int],
) -> dict[int, tuple[str, bool]]:
    """Resolve heading titles in stream order while tracking literal/dynamic alias redefinitions."""
    aliases: dict[str, str | None] = dict(_DEFAULT_REFERENCE_ALIASES)
    resolved: dict[int, tuple[str, bool]] = {}
    for index, command in enumerate(commands):
        if command.start >= body_end:
            break
        if command.name in DEFINITION_COMMANDS and command.defined_name in REFERENCE_TITLE_ALIASES:
            aliases[command.defined_name] = _alias_redefinition(command, aliases)
        elif command.name == "__conditional__":
            for alias_name in _conditional_redefined_aliases(
                command.arg or "",
                [offset - command.start for offset in source_boundaries
                 if command.start < offset < command.end],
            ):
                aliases[alias_name] = None
        elif command.start >= body_start and command.name in HEADING_LEVELS:
            resolved[index] = _resolve_title(command.arg, aliases)
    return resolved


def _regions(
    commands: list[Command], body_start: int, body_end: int,
    add_diagnostic: Any,
    resolved_titles: dict[int, tuple[str, bool]],
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
        if command.name == "begin" and command.arg in BIBLIOGRAPHY_ENVIRONMENTS:
            env_name = command.arg
            depth = 1
            end = body_end
            for later in commands[index + 1:]:
                if later.start >= body_end:
                    break
                if later.arg == env_name:
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
        elif command.name in HEADING_LEVELS and (
            resolved_titles.get(index, ("", False))[0] in REFERENCE_TITLES
        ):
            level = HEADING_LEVELS[command.name]
            end = body_end
            for later_index, later in enumerate(commands[index + 1:], index + 1):
                if later.start >= body_end:
                    break
                if later.name == "appendix":
                    end = later.start
                    break
                if later.name in HEADING_LEVELS and (
                    HEADING_LEVELS[later.name] <= level
                    or resolved_titles.get(later_index, ("", False))[0] in POST_REFERENCE_TITLES
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

    resolved_titles = _resolve_heading_titles(commands, body_start, body_end, source_boundaries)
    regions = _regions(commands, body_start, body_end, add_diagnostic, resolved_titles)
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

    for index, command in enumerate(commands):
        if command.start < body_start or command.start >= body_end:
            continue
        if command.name not in HEADING_LEVELS and command.name != "appendix":
            continue
        title, unresolved = resolved_titles.get(index, ("", False))
        region = next((
            item for item in regions
            if item["span"][0] <= command.start < item["span"][1]
        ), None)
        if region is not None:
            # A references heading closes the previous section even though the
            # heading itself is not a node in the exported outline.
            if command.name in HEADING_LEVELS and title in REFERENCE_TITLES:
                emit_body(stack[-1]["node_id"], cursor, command.start)
                level = HEADING_LEVELS[command.name]
                while len(stack) > 1 and stack[-1]["level"] >= level:
                    stack.pop()
                cursor = command.end
            continue
        if command.name in HEADING_LEVELS and unresolved:
            add_diagnostic(
                "ambiguous_reference_heading",
                "Heading uses \\refname/\\bibname whose redefinition could not be resolved statically; retained as a normal heading.",
                command.start, command.end,
            )

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
