"""A small, non-executing LaTeX scanner for compilation paths and outlines.

Only literal conditionals are selected.  Macro definitions and command
arguments are opaque; this deliberately does not emulate a TeX interpreter.
"""
from __future__ import annotations

from dataclasses import dataclass
from collections.abc import Iterator
from collections.abc import Iterable
from bisect import bisect_right
import re


@dataclass(frozen=True)
class Command:
    name: str
    start: int
    end: int
    arg: str | None = None
    optional: str | None = None
    starred: bool = False
    defined_name: str | None = None
    definition_body: str | None = None
    second_arg: str | None = None


RAW_ENVIRONMENTS = frozenset({"verbatim", "verbatim*", "lstlisting", "minted", "comment"})
MATH_ENVIRONMENTS = frozenset({
    "math", "displaymath", "equation", "equation*", "align", "align*",
    "alignat", "alignat*", "gather", "gather*", "multline", "multline*",
    "eqnarray", "eqnarray*", "flalign", "flalign*", "split",
})
DEFINITION_COMMANDS = frozenset({
    "newcommand", "renewcommand", "providecommand", "DeclareRobustCommand",
    "def", "gdef", "edef", "xdef", "let",
})
ENV_DEFINITION_COMMANDS = frozenset({"newenvironment", "renewenvironment"})
HEADING_COMMANDS = frozenset({"part", "chapter", "section", "subsection", "subsubsection", "paragraph", "subparagraph"})
_TWO_ARG_COMMANDS = frozenset({"import", "subimport", "includefrom", "subincludefrom"})
_REQUIRES_ARG = HEADING_COMMANDS | {
    "documentclass", "input", "include", "includeonly", "subfile", "subfileinclude",
    "begin", "end", "bibliography", "addbibresource", "bibliographystyle",
}
_DECLARATION_SWITCHES = frozenset({
    "tiny", "scriptsize", "footnotesize", "small", "normalsize",
    "large", "Large", "LARGE", "huge", "Huge",
    "bf", "bfseries", "it", "itshape", "em", "sc", "scshape",
    "rm", "rmfamily", "sf", "sffamily", "tt", "ttfamily",
    "sl", "slshape", "upshape", "mdseries", "normalfont",
    "centering", "raggedright", "raggedleft", "noindent", "indent", "par",
    "newpage", "clearpage", "cleardoublepage",
    "medskip", "smallskip", "bigskip", "hfill", "vfill", "onecolumn",
})
_ARGLESS = frozenset({
    "appendix", "endinput", "else", "fi", "relax", "makeatletter", "makeatother", "iff",
}) | _DECLARATION_SWITCHES
_NON_CONDITIONAL_IF = frozenset({
    "iff",
    "ifthenelse", "ifdef", "ifcsdef", "ifstrequal", "ifblank", "ifboolexpr",
    "ifboolexpe", "ifstrempty", "ifundef", "ifcsundef", "ifcsvoid", "ifbool",
    "iftoggle", "ifdefempty", "ifdefvoid", "ifdefequal", "ifdefstring",
    "ifdefstrequal", "ifdefmacro", "ifdefparam", "ifdefprefix",
    "ifdefprotected", "ifcsstring", "ifcsstrequal", "ifcsempty", "ifnumodd",
    "ifnumcomp", "ifnumequal", "ifnumgreater", "ifnumless",
    "ifdimcomp", "ifdimequal", "ifdimgreater", "ifdimless",
})


class _SourceText(str):
    """An immutable text view carrying original-file lexical seams explicitly."""

    def __new__(cls, text: str, boundaries: Iterable[int] | None = None):
        value = super().__new__(cls, text)
        value.source_boundaries = tuple(sorted({p for p in boundaries or () if 0 < p < len(text)}))
        return value


def _lexical_limit(text: str, pos: int, limit: int) -> int:
    boundaries = getattr(text, "source_boundaries", ())
    index = bisect_right(boundaries, pos)
    return min(limit, boundaries[index]) if index < len(boundaries) else limit


def _comment_end(text: str, pos: int, limit: int) -> int:
    limit = _lexical_limit(text, pos, limit)
    newline = text.find("\n", pos, limit)
    return limit if newline < 0 else newline + 1


def _control(text: str, pos: int, limit: int) -> tuple[str, int]:
    """Read one control word or escaped single character at a backslash."""
    limit = _lexical_limit(text, pos, limit)
    end = pos + 1
    if end >= limit:
        return "", end
    if text[end].isalpha() or text[end] == "@":
        end += 1
        while end < limit and (text[end].isalpha() or text[end] == "@"):
            end += 1
        return text[pos + 1:end], end
    return text[end], end + 1


def _space(text: str, pos: int, limit: int) -> int:
    while pos < limit:
        if text[pos].isspace():
            pos += 1
        elif text[pos] == "%":
            pos = _comment_end(text, pos, limit)
        else:
            break
    return pos


def _balanced(text: str, pos: int, limit: int, errors: list[tuple[int, int, str]]) -> tuple[str, int]:
    opener = text[pos]
    closer = "}" if opener == "{" else "]"
    depth = 1
    cursor = pos + 1
    while cursor < limit:
        char = text[cursor]
        if char == "\\":
            _, cursor = _control(text, cursor, limit)
        elif char == "%":
            cursor = _comment_end(text, cursor, limit)
        elif opener == "[" and char == "{":
            # A ] within a braced option is not the option's terminator.
            _, cursor = _balanced(text, cursor, limit, errors)
        elif char == opener:
            depth += 1
            cursor += 1
        elif char == closer:
            depth -= 1
            cursor += 1
            if depth == 0:
                return text[pos + 1:cursor - 1], cursor
        else:
            cursor += 1
    errors.append((pos, limit, f"Unclosed {opener}{closer} argument."))
    return text[pos + 1:limit], limit


def _definition(text: str, start: int, name: str, pos: int, limit: int,
                errors: list[tuple[int, int, str]]) -> Command:
    cursor = _space(text, pos, limit)
    starred = cursor < limit and text[cursor] == "*"
    if starred:
        cursor = _space(text, cursor + 1, limit)
    target: str | None = None
    body: str | None = None
    if cursor < limit and text[cursor] == "{":
        target_raw, cursor = _balanced(text, cursor, limit, errors)
        match = re.fullmatch(r"\s*\\([A-Za-z@]+|.)\s*", target_raw, re.S)
        if match:
            target = match.group(1)
    elif cursor < limit and text[cursor] == "\\":
        target, cursor = _control(text, cursor, limit)
    if not target:
        errors.append((start, max(cursor, pos), f"Malformed \\{name} target."))
    cursor = _space(text, cursor, limit)
    if name == "let":
        if cursor < limit and text[cursor] == "=":
            cursor = _space(text, cursor + 1, limit)
        if cursor < limit and text[cursor] == "\\":
            alias_start = cursor
            _, cursor = _control(text, cursor, limit)
            body = text[alias_start:cursor]
        elif cursor < limit:
            body = text[cursor]
            cursor += 1
        else:
            errors.append((start, cursor, "Missing \\let replacement."))
    else:
        if name in {"def", "gdef", "edef", "xdef"}:
            # Parameter tokens are not executed. The first body group ends the definition.
            while cursor < limit and text[cursor] != "{":
                if text[cursor] == "%":
                    cursor = _space(text, cursor, limit)
                elif text[cursor] == "\\":
                    _, cursor = _control(text, cursor, limit)
                else:
                    cursor += 1
        else:
            for _ in range(2):
                if cursor < limit and text[cursor] == "[":
                    _, cursor = _balanced(text, cursor, limit, errors)
                    cursor = _space(text, cursor, limit)
        if cursor < limit and text[cursor] == "{":
            body, cursor = _balanced(text, cursor, limit, errors)
        else:
            errors.append((start, cursor, f"Missing \\{name} definition body."))
    return Command(name, start, cursor, starred=starred,
                   defined_name=target, definition_body=body)


def _env_definition(text: str, start: int, name: str, pos: int, limit: int,
                    errors: list[tuple[int, int, str]]) -> Command:
    cursor = _space(text, pos, limit)
    starred = cursor < limit and text[cursor] == "*"
    if starred:
        cursor = _space(text, cursor + 1, limit)
    target: str | None = None
    if cursor < limit and text[cursor] == "{":
        target_raw, cursor = _balanced(text, cursor, limit, errors)
        stripped = target_raw.strip()
        if stripped:
            target = stripped
    if not target:
        errors.append((start, max(cursor, pos), f"Malformed \\{name} target."))
    cursor = _space(text, cursor, limit)
    for _ in range(2):
        if cursor < limit and text[cursor] == "[":
            _, cursor = _balanced(text, cursor, limit, errors)
            cursor = _space(text, cursor, limit)
    bodies: list[str] = []
    for part_name in ("begin", "end"):
        if cursor < limit and text[cursor] == "{":
            body_part, cursor = _balanced(text, cursor, limit, errors)
            bodies.append(body_part)
            cursor = _space(text, cursor, limit)
        else:
            errors.append((start, cursor, f"Missing \\{name} {part_name} body."))
    combined_body = "\n".join(bodies) if bodies else None
    return Command(name, start, cursor, starred=starred,
                   defined_name=target, definition_body=combined_body)


def _newif(text: str, start: int, pos: int, limit: int,
           errors: list[tuple[int, int, str]]) -> Command:
    cursor = _space(text, pos, limit)
    target: str | None = None
    if cursor < limit and text[cursor] == "{":
        target_raw, cursor = _balanced(text, cursor, limit, errors)
        match = re.fullmatch(r"\s*\\([A-Za-z@]+)\s*", target_raw, re.S)
        if match:
            target = match.group(1)
    elif cursor < limit and text[cursor] == "\\":
        candidate, cursor = _control(text, cursor, limit)
        if candidate:
            target = candidate
    if not target:
        errors.append((start, max(cursor, pos), "Malformed \\newif target."))
    return Command("newif", start, cursor, defined_name=target)


def _command(text: str, start: int, limit: int,
             errors: list[tuple[int, int, str]]) -> Command:
    name, end = _control(text, start, limit)
    if name in DEFINITION_COMMANDS:
        return _definition(text, start, name, end, limit, errors)
    if name in ENV_DEFINITION_COMMANDS:
        return _env_definition(text, start, name, end, limit, errors)
    if name == "newif":
        return _newif(text, start, end, limit, errors)
    if name in _ARGLESS or _is_conditional(name):
        return Command(name, start, end)
    cursor = _space(text, end, limit)
    starred = cursor < limit and text[cursor] == "*"
    if starred:
        end = cursor + 1
        cursor = _space(text, end, limit)
    arg: str | None = None
    optional: str | None = None
    second_arg: str | None = None
    # Escaped symbols have no LaTeX arguments.
    if len(name) == 1 and not name.isalpha():
        return Command(name, start, end, starred=starred)
    while cursor < limit and text[cursor] in "[{":
        opener = text[cursor]
        value, next_pos = _balanced(text, cursor, limit, errors)
        if opener == "{" and arg is None:
            arg = value
        elif opener == "{" and name in _TWO_ARG_COMMANDS and second_arg is None:
            second_arg = value
        elif opener == "[" and optional is None:
            optional = value
        end = next_pos
        # begin/end consume their environment name, but not following body groups.
        if name in _REQUIRES_ARG and arg is not None:
            break
        if name in _TWO_ARG_COMMANDS and arg is not None and second_arg is not None:
            break
        cursor = _space(text, next_pos, limit)
    if name == "input" and arg is None:
        cursor = _space(text, end, limit)
        path_start = cursor
        while cursor < limit and not text[cursor].isspace() and text[cursor] not in "%{}":
            cursor += 1
        if cursor > path_start:
            arg = text[path_start:cursor]
            end = cursor
    if (name in _REQUIRES_ARG and arg is None) or (
        name in _TWO_ARG_COMMANDS and (arg is None or second_arg is None)
    ):
        errors.append((start, max(end, start + 1), f"Missing argument for \\{name}."))
    return Command(name, start, end, arg, optional, starred, second_arg=second_arg)


def _is_conditional(name: str) -> bool:
    # Math \iff and braced macros like \ifthenelse are not \if...\fi primitives.
    return name.startswith("if") and name not in _NON_CONDITIONAL_IF


def _delimited(text: str, start: int, end: int, delimiter: str,
               errors: list[tuple[int, int, str]], description: str) -> int:
    cursor = end
    while cursor < len(text):
        if (text.startswith(delimiter, cursor)
                and (not delimiter.startswith("\\") or cursor + len(delimiter) <= _lexical_limit(text, cursor, len(text)))):
            return cursor + len(delimiter)
        if text[cursor] == "\\":
            _, cursor = _control(text, cursor, len(text))
        else:
            cursor += 1
    errors.append((start, len(text), f"Unclosed {description}."))
    return len(text)


def _raw_end(text: str, cmd: Command, limit: int,
              errors: list[tuple[int, int, str]]) -> tuple[int, int]:
    env = cmd.arg or ""
    matches = re.finditer(r"\\end\s*\{" + re.escape(env) + r"\}", text[cmd.end:limit])
    for match in matches:
        start = cmd.end + match.start()
        if _control(text, start, limit)[0] == "end":
            return start, cmd.end + match.end()
    else:
        errors.append((cmd.start, limit, f"Unclosed {env} environment."))
        return limit, limit


def _verb_end(text: str, start: int, pos: int, limit: int,
               errors: list[tuple[int, int, str]]) -> int:
    if pos < limit and text[pos] == "*":
        pos += 1
    if pos >= limit or text[pos].isspace():
        errors.append((start, min(pos + 1, limit), "Missing \\verb delimiter."))
        return pos
    delimiter = text[pos]
    close = text.find(delimiter, pos + 1, limit)
    newline = text.find("\n", pos + 1, limit)
    if close < 0 or (newline >= 0 and newline < close):
        stop = newline if newline >= 0 else limit
        errors.append((start, stop, "Unclosed \\verb."))
        return stop
    return close + 1


def _conditional_bounds(text: str, cmd: Command, limit: int,
                        errors: list[tuple[int, int, str]],
                        include_math: bool = False) -> tuple[int | None, int | None, int, int]:
    cursor = cmd.end
    depth = 1
    else_start: int | None = None
    else_end: int | None = None
    while cursor < limit:
        char = text[cursor]
        if char == "%":
            cursor = _comment_end(text, cursor, limit)
            continue
        if char == "$" and not include_math:
            delimiter = "$$" if text.startswith("$$", cursor) else "$"
            cursor = min(_delimited(text, cursor, cursor + len(delimiter), delimiter, errors, "math delimiter"), limit)
            continue
        if char != "\\":
            cursor += 1
            continue
        name, control_end = _control(text, cursor, limit)
        if name == "verb":
            cursor = _verb_end(text, cursor, control_end, limit, errors)
            continue
        if name in {"(", "["} and not include_math:
            cursor = min(_delimited(text, cursor, control_end, "\\)" if name == "(" else "\\]", errors, "math delimiter"), limit)
            continue
        token = _command(text, cursor, limit, errors)
        if name == "begin" and (token.arg in RAW_ENVIRONMENTS or (not include_math and token.arg in MATH_ENVIRONMENTS)):
            _, cursor = _raw_end(text, token, limit, errors)
            continue
        if _is_conditional(name):
            depth += 1
        elif name == "fi":
            depth -= 1
            if depth == 0:
                return else_start, else_end, token.start, token.end
        elif name == "else" and depth == 1:
            if else_start is not None:
                errors.append((token.start, token.end, "Repeated \\else in conditional."))
            else_start, else_end = token.start, token.end
        cursor = max(token.end, control_end)
    errors.append((cmd.start, limit, f"Unclosed \\{cmd.name} conditional."))
    return else_start, else_end, limit, limit


def _scan(text: str, start: int, limit: int,
          errors: list[tuple[int, int, str]],
          include_math: bool = False) -> Iterator[Command]:
    cursor = start
    while cursor < limit:
        char = text[cursor]
        if char == "%":
            cursor = _comment_end(text, cursor, limit)
            continue
        if char == "$" and not include_math:
            delimiter = "$$" if text.startswith("$$", cursor) else "$"
            cursor = min(_delimited(text, cursor, cursor + len(delimiter), delimiter, errors, "math delimiter"), limit)
            continue
        if char != "\\":
            cursor += 1
            continue
        name, control_end = _control(text, cursor, limit)
        if name == "verb":
            cursor = _verb_end(text, cursor, control_end, limit, errors)
            continue
        if name in {"(", "["} and not include_math:
            cursor = min(_delimited(text, cursor, control_end, "\\)" if name == "(" else "\\]", errors, "math delimiter"), limit)
            continue
        token = _command(text, cursor, limit, errors)
        if _is_conditional(name):
            else_start, else_end, fi_start, fi_end = _conditional_bounds(text, token, limit, errors, include_math)
            if name == "iftrue":
                yield from _scan(text, token.end, else_start if else_start is not None else fi_start, errors, include_math)
            elif name == "iffalse":
                if else_end is not None:
                    yield from _scan(text, else_end, fi_start, errors, include_math)
            else:
                yield Command("__conditional__", cursor, fi_end, text[cursor:fi_end])
            cursor = fi_end
            continue
        if name in {"else", "fi"}:
            errors.append((token.start, token.end, f"Unmatched \\{name}."))
        yield token
        cursor = max(token.end, control_end)
        if name == "begin" and (token.arg in RAW_ENVIRONMENTS or (not include_math and token.arg in MATH_ENVIRONMENTS)):
            end_start, end_end = _raw_end(text, token, limit, errors)
            if end_start < limit:
                yield Command("end", end_start, end_end, token.arg)
            cursor = end_end


def iter_commands(text: str, *, source_boundaries: Iterable[int] | None = None,
                  include_math: bool = False) -> Iterator[Command]:
    """Yield active commands with exact offsets; never execute arbitrary macros."""
    if source_boundaries is not None:
        text = _SourceText(text, source_boundaries)
    yield from _scan(text, 0, len(text), [], include_math)


def scan_errors(text: str, *, source_boundaries: Iterable[int] | None = None) -> list[tuple[int, int, str]]:
    """Return malformed argument/raw/math/conditional spans without raising."""
    errors: list[tuple[int, int, str]] = []
    if source_boundaries is not None:
        text = _SourceText(text, source_boundaries)
    for _ in _scan(text, 0, len(text), errors):
        pass
    return list(dict.fromkeys(errors))