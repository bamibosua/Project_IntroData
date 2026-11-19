"""Text helpers for full-text matching (standard library only)."""

from __future__ import annotations

import re
import unicodedata

_WS_RE = re.compile(r"\s+")
_HSPACE_RE = re.compile(r"[ \t\f\v]+")
_COMMA_RE = re.compile(r"\s*,\s*")
_INVISIBLE = dict.fromkeys(map(ord, "​‌‍⁠﻿­"), None)

# \cite-like commands whose keys may have to be renamed to the unified citation key.
CITE_CMD_RE = re.compile(
    r"(\\(?:cite|citep|citet|citealp|citealt|citeauthor|citeyear|parencite|textcite|autocite|nocite|Cite[a-z]*)\*?"
    r"(?:\[[^\]]*\]){0,2}\{)([^{}]*)(\})"
)


def collapse_whitespace(text: str) -> str:
    """Collapse every whitespace run (tabs, newlines, NBSP...) to one space and trim the ends."""
    return _WS_RE.sub(" ", text.replace(" ", " ")).strip()


def tidy_content(text: str, keep_layout: bool = False) -> str:
    """Whitespace-clean text for storage.

    Prose is collapsed to single spaces. With `keep_layout` (equations, tables, figures) the line breaks are
    kept and only the spaces inside each line are collapsed, so the stored LaTeX stays as readable as the input.
    """
    if not keep_layout:
        return collapse_whitespace(text)
    lines = (_HSPACE_RE.sub(" ", line.replace(" ", " ")).strip() for line in text.strip().splitlines())
    return "\n".join(lines)


def _tidy_cite(match: re.Match[str]) -> str:
    return f"{match.group(1)}{_COMMA_RE.sub(',', match.group(2).strip())}{match.group(3)}"


def match_key(text: str, *, fold: bool = True) -> str:
    """Canonical form used ONLY to decide whether two cleaned texts are the same.

    Whitespace is collapsed (also around the commas of `\\cite{a, b}`), and with `fold` compatibility characters
    are folded (NFKC: ligatures, full-width forms) and invisible characters are dropped, so formatting leftovers
    cannot make identical texts look different. Pass `fold=False` for mathematics, tables and figures, where
    NFKC could merge genuinely different symbols (e.g. a superscript two and a digit two).
    The stored `content` keeps the original characters (see `tidy_content`).
    """
    if fold:
        text = unicodedata.normalize("NFKC", text).translate(_INVISIBLE)
    text = collapse_whitespace(text)
    if "cite" in text.lower() and "\\" in text:
        text = CITE_CMD_RE.sub(_tidy_cite, text)
    return text


def rewrite_citations(text: str, key_map: dict[str, str] | None) -> str:
    """Rename keys inside \\cite{...} commands (reference deduplication output) before text matching."""
    if not key_map or "\\" not in text or "cite" not in text.lower():
        return text

    def _sub(match: re.Match[str]) -> str:
        keys, seen = [], set()
        for raw in match.group(2).split(","):
            key = raw.strip()
            if not key:
                continue
            key = key_map.get(key, key)
            if key not in seen:
                seen.add(key)
                keys.append(key)
        return f"{match.group(1)}{','.join(keys)}{match.group(3)}"

    return CITE_CMD_RE.sub(_sub, text)
