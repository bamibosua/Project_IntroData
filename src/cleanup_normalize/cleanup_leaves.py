"""Milestone 2, member 2: cleanup, math normalization, and smallest elements.

Input is the ACTUAL output of member 1, not a hypothetical nested tree:
  outline.json: {publication_key, version, nodes, body_segments, ...}
  merged.tex: original text; body_segments[*].span are CHARACTER offsets.

Output: same flat node schema (node_id, kind, parent_id, order, ...), with added
paragraph, list, item, sentence, block_formula, figure nodes. Tables are figures
with subtype='table'. Member 4 owns citation key replacement, dedup, and IDs.
"""
from __future__ import annotations

import argparse
from collections import Counter
import hashlib
import json
import os
from pathlib import Path
import re
import tempfile
import uuid
from typing import Any

MATH_ENVS = {"equation", "equation*", "displaymath", "math", "align", "align*",
             "aligned", "gather", "gather*", "gathered", "multline", "multline*",
             "split", "eqnarray", "eqnarray*", "flalign", "flalign*",
             "alignat", "alignat*", "alignedat", "IEEEeqnarray", "IEEEeqnarray*",
             "IEEEeqnarraybox", "IEEEeqnarraybox*"}
BLOCK_MATH_ENVS = MATH_ENVS - {"math"}
FLOAT_ENVS = {"figure", "figure*", "wrapfigure", "wrapfigure*", "table", "table*", "longtable", "tabular", "tabular*"}
ALGORITHM_ENVS = {'algorithm', 'algorithm*', 'algorithmic', 'algorithmicx'}
LIST_ENVS = {"itemize", "enumerate", "description"}
OPAQUE_ENVS = {"verbatim", "verbatim*", "lstlisting", "minted"}
STYLE_WRAPPERS = {"emph", "textbf", "textit", "textsc", "texttt", "textsf", "textrm",
                  "textnormal", "underline", "mbox", "textup", "textsl", "textmd"}
STYLE_STANDALONE = {"centering", "raggedright", "raggedleft", "noindent", "smallskip",
                    "medskip", "bigskip", "toprule", "midrule", "bottomrule", "hline",
                    "newpage", "clearpage", "pagebreak", "bfseries", "itshape", "slshape",
                    "scriptsize", "footnotesize", "small", "normalsize", "large", "Large", "LARGE"}
SKIP_MACROS = {"maketitle", "tableofcontents", "bibliography", "bibliographystyle", "pacs", "keywords", "appendix"}
ABBR_RE = re.compile(r"\b(?:i\.e|e\.g|et\s+al|Fig|Figs|Eq|Eqs|Sec|Secs|Ch|Dr|Mr|Mrs|Ms|Prof|vs|cf|No|Ref|Refs|Vol|approx)\.", re.I)
LATEX_ET_AL_RE = re.compile(r"\bet\s+al\.", re.I)
PHYS_REV_RE = re.compile(r"\bPhys\.\s+Rev\.")
PHYS_REV_SERIES_RE = re.compile(
    r"\s+[A-E]"
    r"(?P<dot>\.)?(?=\s*,?\s*\d)"
)
PHYS_REV_SUFFIX_RE = re.compile(
    r"\s+(?P<suffix>Lett\.|X|Applied|Fluids|Materials|Research|Phys\.\s+Educ\.\s+Res\.)(?![A-Za-z])"
)
JOURNAL_VOLUME_RE = re.compile(r"\s*,?\s*\d")
BOUNDARY_SPACE_RE = re.compile(
    r"\\(?:[ ,;:]|(?:space|nobreakspace|thinspace|enspace|quad|qquad)(?:\{\})?"
    r"|(?:it|itshape|rm|rmfamily|bf|bfseries|sl|slshape|em|upshape))"
)
INITIALISM_RE = re.compile(r"\b(?:[A-Z]\.){2,}")
URL_RE = re.compile(r"(?:https?://|www\.)[^\s<>]+", re.I)
ENV_RE = re.compile(r"\\(begin|end)\s*\{([A-Za-z0-9*@_-]+)\}")
CMD_RE = re.compile(r"\\([A-Za-z@]+)\*?")
VERB_RE = re.compile(r"\\verb\*?([^\w\s])")
MACRO_RE = re.compile(r"\\(?:[A-Za-z@]+\*?|.)")
ITEM_RE = re.compile(r'\\item\b')
PROSE_RUN_RE = re.compile(r"[^\\$]+")
TEXT_RUN_RE = re.compile(r"[^\\${}]+")
COMMENT_RUN_RE = re.compile(r"[^\\%]+")
LINE_END_RE = re.compile(r"\r\n|[\r\n]")
CONTROL_WORD_RE = re.compile(r"\\[A-Za-z@]+")
TAG_RE = re.compile(r"\ue000[0-9]+\ue001")
CONTROL_RE = re.compile(r'[\\%]')
FIGURE_DOT_RE = re.compile(r'\bFigures?\.(?=\s)', re.I)
REF_COMMAND_RE = re.compile(r'\\(?:ref|autoref|cref|Cref|pageref)\*?\s*\{')
DEFINITION_RE = re.compile(r'\\(newcommand|renewcommand|providecommand|def)\*?\s*')
BODY_DEFINITION_RE = re.compile(
    r'\\(?:newcommand|renewcommand|providecommand|DeclareRobustCommand|def|gdef|edef|xdef)\*?(?![A-Za-z@])\s*')
PROSE_ENVS = {'abstract', 'center', 'flushleft', 'flushright', 'document',
              'titlepage', 'keywords', 'IEEEkeywords', 'MSC', 'subequations',
              'widetext', 'theorem', 'lemma', 'proof', 'corollary', 'assumption',
              'notation', 'definition', 'remark', 'proposition', 'example', 'claim', 'appendices'}
LENGTH_COMMANDS = {'setlength', 'addtolength', 'setcounter', 'addtocounter'}
DOCUMENT_BEGIN_RE = re.compile(r'\\begin\s*\{document\}')
DOCUMENT_SCAN_RE = re.compile(r'[\\%{}]')
CITATION_RE = re.compile(
    r'\\(?:[Cc]ite[A-Za-z]*|parencite|textcite|autocite|footcite)\*?(?![A-Za-z@])')
INLINE_TEXT_ARITY = {**dict.fromkeys(STYLE_WRAPPERS, 1), 'href': 2, 'url': 1,
                     'path': 1, 'footnote': 1, 'textcolor': 2}
ROOT_METADATA_OPTIONS = {
    'title': 1, 'author': 1, 'date': 0, 'thanks': 0, 'address': 1,
    'affiliation': 1, 'institute': 0, 'email': 1,
    'documentclass': 1, 'usepackage': 1, 'RequirePackage': 1,
}
# Only commands with a known signature may consume leading square brackets.
# In particular, \in[-2L,0) and \left[... are mathematical intervals, not options.
OPTIONAL_MACRO_ARGS = {
    **ROOT_METADATA_OPTIONS, 'sqrt': 1, 'includegraphics': 1,
    'footnote': 1, 'footnotetext': 1, 'footnotemark': 1,
    'caption': 1, 'captionof': 1, 'item': 1,
    'section': 1, 'subsection': 1, 'subsubsection': 1,
    'chapter': 1, 'part': 1, 'paragraph': 1, 'subparagraph': 1,
    '\\': 1,
}



def definition_end(text: str, start: int) -> int | None:
    """Consume a full balanced macro declaration, including parameter tokens."""
    match = BODY_DEFINITION_RE.match(text, start)
    if not match: return None
    j = match.end()
    name = arg_at(text, j)
    if name:
        j = name[1]
    else:
        token = MACRO_RE.match(text, j)
        if not token: return None
        j = token.end()
    while j < len(text) and text[j].isspace(): j += 1
    if re.match(r'\\(?:def|gdef|edef|xdef)\b', text[start:]):
        while j < len(text) and text[j] != '{': j += 1
    else:
        while j < len(text) and text[j] == '[':
            option = arg_at(text, j, '[', ']')
            if option is None: return None
            j = option[1]
            while j < len(text) and text[j].isspace(): j += 1
    body = arg_at(text, j)
    return body[1] if body else None


def layout_command_end(text: str, start: int) -> int | None:
    """Recognize complete layout assignments without consuming following prose."""
    command = CMD_RE.match(text, start)
    if not command or command.group(1) not in LENGTH_COMMANDS: return None
    j = command.end()
    for index in range(2):
        while j < len(text) and text[j].isspace(): j += 1
        arg = arg_at(text, j)
        if arg:
            j = arg[1]
        elif index == 0 and (token := MACRO_RE.match(text, j)) is not None:
            j = token.end()
        else:
            return None
    return j


def math_aliases(tex: str, document_start: int | None = None) -> dict[str, str]:
    """Read zero-argument math delimiter aliases from the document preamble.

    This deliberately does not execute TeX or guess meanings from macro names.
    Parameterized/custom expressions and cyclic definitions remain untouched.
    """
    if document_start is None:
        boundary = document_boundary(tex)
        document_start = boundary[0] if boundary else len(tex)
    preamble = strip_comments(tex[:document_start])
    definitions: dict[str, str] = {}
    i = 0
    while i < len(preamble):
        end = verb_end(preamble, i) if preamble[i] == '\\' else None
        if end is not None:
            i = end; continue
        match = DEFINITION_RE.match(preamble, i)
        if not match:
            token = MACRO_RE.match(preamble, i)
            i = token.end() if token else i + 1
            continue
        j = match.end()
        name_arg = arg_at(preamble, j)
        if name_arg:
            name, j = name_arg
        else:
            name_match = re.match(r'\\[A-Za-z@]+', preamble[j:])
            if not name_match:
                i = j; continue
            name = name_match.group(); j += len(name)
        while j < len(preamble) and preamble[j].isspace(): j += 1
        parameterized = False
        while j < len(preamble) and preamble[j] == '[':
            option = arg_at(preamble, j, '[', ']')
            if not option: break
            parameterized = parameterized or option[0].strip() != '0'
            j = option[1]
            while j < len(preamble) and preamble[j].isspace(): j += 1
        if match.group(1) == 'def':
            brace = preamble.find('{', j)
            if brace >= 0:
                parameterized = parameterized or bool(preamble[j:brace].strip())
                j = brace
        body = arg_at(preamble, j)
        if not body:
            i = j; continue
        i = body[1]
        if not re.fullmatch(r'\\[A-Za-z@]+', name.strip()): continue
        name = name.strip()
        if match.group(1) == 'providecommand' and name in definitions: continue
        definitions[name] = '' if parameterized else body[0].strip()
    resolved: dict[str, str] = {}
    for name, value in definitions.items():
        seen = {name}
        while value in definitions and value not in seen:
            seen.add(value); value = definitions[value]
        env = ENV_RE.fullmatch(value)
        if value in {r'\[', r'\]', r'\(', r'\)'} or (env and env.group(2) in MATH_ENVS):
            resolved[name] = value
    return resolved


def declaration_aliases(tex: str) -> dict[str, str]:
    r"""Recognize explicit zero-argument aliases such as \newcommand{\nc}{\newcommand}.

    Only declaration commands are eligible. Conflicting declarations are left
    unresolved rather than assigning a guessed meaning to a custom command.
    """
    declarations = {'newcommand', 'renewcommand', 'providecommand', 'DeclareRobustCommand'}
    pattern = re.compile(
        r'\\(?:newcommand|renewcommand|providecommand|DeclareRobustCommand)\*?\s*'
        r'(?:\{\s*(\\[A-Za-z@]+)\s*\}|(\\[A-Za-z@]+))\s*'
        r'(?:\[0\]\s*)?\{\s*\\(' + '|'.join(sorted(declarations)) + r')\s*\}')
    aliases: dict[str, str] = {}
    conflicts: set[str] = set()
    text = strip_comments(tex)
    if not pattern.search(text): return {}
    i = 0
    while i < len(text):
        if text[i] != '\\':
            i += 1; continue
        if (end := verb_end(text, i)) is not None:
            i = end; continue
        match = pattern.match(text, i)
        if match:
            name = match.group(1) or match.group(2)
            target = '\\' + match.group(3)
            if name in aliases and aliases[name] != target:
                conflicts.add(name)
            aliases[name] = target
        declaration = definition_end(text, i)
        if declaration is not None:
            # A later ordinary redefinition invalidates an earlier alias too.
            name_match = re.match(
                r'\\[A-Za-z@]+\*?\s*(?:\{\s*(\\[A-Za-z@]+)\s*\}|(\\[A-Za-z@]+))',
                text[i:declaration])
            if not match and name_match:
                conflicts.add(name_match.group(1) or name_match.group(2))
            i = declaration; continue
        token = MACRO_RE.match(text, i)
        i = token.end() if token else i + 1
    return {name: target for name, target in aliases.items() if name not in conflicts}


def expand_declaration_aliases(text: str, aliases: dict[str, str]) -> str:
    """Expand only complete declarations; keep their bodies opaque to scanning."""
    if not aliases: return text
    out = []
    i = 0
    while i < len(text):
        if (run := PROSE_RUN_RE.match(text, i)) is not None:
            out.append(run.group()); i = run.end(); continue
        end = verb_end(text, i) if text[i] == '\\' else None
        if end is not None:
            out.append(text[i:end]); i = end; continue
        declaration = definition_end(text, i)
        if declaration is not None:
            out.append(text[i:declaration]); i = declaration; continue
        token = MACRO_RE.match(text, i)
        if token:
            replacement = aliases.get(token.group())
            if replacement:
                expanded = replacement + text[token.end():]
                end = definition_end(expanded, 0)
                if end is not None:
                    out.append(expanded[:end])
                    i = token.end() + end - len(replacement)
                    continue
            out.append(token.group()); i = token.end()
        else:
            out.append(text[i]); i += 1
    return ''.join(out)


def expand_math_aliases(text: str, aliases: dict[str, str]) -> str:
    """Expand verified delimiter aliases after slicing original source spans."""
    if not aliases: return text
    out = []
    i = 0
    while i < len(text):
        if (run := PROSE_RUN_RE.match(text, i)) is not None:
            out.append(run.group()); i = run.end(); continue
        end = verb_end(text, i) if text[i] == '\\' else None
        if end is not None:
            out.append(text[i:end]); i = end; continue
        declaration = definition_end(text, i)
        if declaration is not None:
            out.append(text[i:declaration]); i = declaration; continue
        token = MACRO_RE.match(text, i)
        if token:
            out.append(aliases.get(token.group(), token.group())); i = token.end()
        else:
            out.append(text[i]); i += 1
    return ''.join(out)


def extract_labels(text: str) -> tuple[str, list[str]]:
    """Remove label commands while retaining their keys as node metadata."""
    out = []
    labels = []
    i = 0
    while i < len(text):
        if (run := PROSE_RUN_RE.match(text, i)) is not None:
            out.append(run.group()); i = run.end(); continue
        end = verb_end(text, i) if text[i] == '\\' else None
        if end is not None:
            out.append(text[i:end]); i = end; continue
        token = MACRO_RE.match(text, i)
        if token:
            j = token.end()
            if token.group() == r'\label':
                while j < len(text) and text[j].isspace(): j += 1
                arg = arg_at(text, j)
                if arg:
                    if arg[0] not in labels: labels.append(arg[0])
                    i = arg[1]; continue
            out.append(token.group()); i = token.end()
        else:
            out.append(text[i]); i += 1
    return ''.join(out), labels


def escaped(text: str, i: int) -> bool:
    n = 0
    i -= 1
    while i >= 0 and text[i] == "\\":
        n += 1
        i -= 1
    return n % 2 != 0


def arg_at(text: str, start: int, open_ch: str = "{", close_ch: str = "}") -> tuple[str, int] | None:
    if start >= len(text) or text[start] != open_ch:
        return None
    depth = brace_depth = 0
    for i in range(start, len(text)):
        ch = text[i]
        if ch not in (open_ch, close_ch, '{', '}') or escaped(text, i):
            continue
        if open_ch == '[':
            # Braced content inside an optional argument may contain literal [ or ].
            if ch == '{':
                brace_depth += 1
                continue
            if ch == '}':
                if brace_depth == 0:
                    return None
                brace_depth -= 1
                continue
            if brace_depth:
                continue
        if ch == open_ch:
            depth += 1
        elif ch == close_ch:
            depth -= 1
            if depth == 0:
                return text[start+1:i], i+1
    return None


def find_env_end(text: str, start: int, env: str) -> tuple[int, int] | None:
    begin = ENV_RE.match(text, start)
    if not begin or begin.group(1) != "begin" or begin.group(2) != env:
        return None
    # Literal environments terminate at their actual closing marker. Their
    # contents must not recursively interpret TeX commands or nested begins.
    if env in OPAQUE_ENVS:
        ending = re.search(r'\\end\{' + re.escape(env) + r'\}', text[begin.end():])
        return ((begin.end() + ending.start(), begin.end() + ending.end())
                if ending else None)
    depth = 1
    i = begin.end()
    while i < len(text):
        control = CONTROL_RE.search(text, i)
        if control is None: break
        i = control.start()
        if text[i] == '%' and not escaped(text, i):
            newline = LINE_END_RE.search(text, i)
            i = newline.end() if newline else len(text)
            continue
        if text[i] != '\\':
            i += 1; continue
        literal = verb_end(text, i)
        if literal is not None:
            i = literal; continue
        token = ENV_RE.match(text, i)
        if token:
            if token.group(2) == env:
                depth += 1 if token.group(1) == 'begin' else -1
                if depth == 0: return i, token.end()
            i = token.end(); continue
        declaration = definition_end(text, i)
        i = declaration if declaration is not None else macro_end(text, i)
    return None


def macro_end(text: str, start: int) -> int:
    """Skip braced arguments and known leading options without swallowing math."""
    token = MACRO_RE.match(text, start)
    if not token: return start + 1
    end = token.end()
    command = token.group()[1:].rstrip('*')
    optional = 2 if CITATION_RE.fullmatch(token.group()) else OPTIONAL_MACRO_ARGS.get(command, 0)
    while end < len(text):
        gap = end
        while gap < len(text) and text[gap].isspace(): gap += 1
        if gap >= len(text) or text[gap] not in '{[': break
        opening = text[gap]
        if opening == '[' and not optional:
            break
        arg = arg_at(text, gap, opening, '}' if opening == '{' else ']')
        if not arg: break
        # An interval after a required argument is not another optional argument.
        optional = optional - 1 if opening == '[' else 0
        end = arg[1]
    return end


def command_args_end(text: str, start: int, *, optional: int, required: int) -> int | None:
    """Read a known command signature; never consume extra prose groups."""
    i = start
    for opening, closing, count in (('[', ']', optional), ('{', '}', required)):
        for _ in range(count):
            j = i
            while j < len(text) and text[j].isspace():
                j += 1
            arg = arg_at(text, j, opening, closing)
            if arg is None:
                if opening == '{':
                    return None
                break
            i = arg[1]
    return i


def document_boundary(text: str) -> tuple[int, int] | None:
    """Find a top-level document opener outside comments, literals and definitions."""
    if not DOCUMENT_BEGIN_RE.search(text):
        return None
    i = depth = 0
    while (control := DOCUMENT_SCAN_RE.search(text, i)) is not None:
        j = control.start()
        ch = text[j]
        i = j + 1
        if ch == '%':
            newline = LINE_END_RE.search(text, i)
            if newline is None:
                break
            i = newline.end()
        elif ch == '{':
            depth += 1
        elif ch == '}':
            depth = max(0, depth - 1)
        else:
            literal = verb_end(text, j)
            if literal is not None:
                i = literal
                continue
            declaration = definition_end(text, j)
            if declaration is not None:
                i = declaration
                continue
            if depth == 0 and (match := DOCUMENT_BEGIN_RE.match(text, j)):
                return j, match.end()
            token = MACRO_RE.match(text, j)
            if token:
                i = token.end()
    return None


def verb_end(text: str, i: int) -> int | None:
    m = VERB_RE.match(text, i)
    if m:
        end = text.find(m.group(1), m.end())
        return end+1 if end >= 0 else None
    m = ENV_RE.match(text, i)
    if m and m.group(1) == "begin" and m.group(2) in OPAQUE_ENVS:
        end = find_env_end(text, i, m.group(2))
        return end[1] if end else None
    return None


def strip_comments(tex: str) -> str:
    r"""Remove a TeX comment and its line ending, preserving \% and literals."""
    out = []
    i = 0
    while i < len(tex):
        if (run := COMMENT_RUN_RE.match(tex, i)) is not None:
            out.append(run.group()); i = run.end(); continue
        if tex[i] == "\\":
            if (end := verb_end(tex, i)) is not None:
                out.append(tex[i:end]); i = end; continue
            # Consume the control token as a unit: \% is not a comment,
            # whereas the % following a double backslash still starts one.
            if (token := MACRO_RE.match(tex, i)) is not None:
                out.append(token.group()); i = token.end(); continue
        if tex[i] == "%":
            newline = LINE_END_RE.search(tex, i)
            if newline is None:
                break
            i = newline.end()
            # TeX ignores indentation at the start of the continuation line.
            while i < len(tex) and tex[i] in ' \t':
                i += 1
            if i < len(tex) and out:
                if tex[i] in '\r\n' and not out[-1].rstrip(' \t').endswith(('\r', '\n')):
                    # A genuinely blank following line still ends a paragraph.
                    out.append(newline.group())
                elif CONTROL_WORD_RE.fullmatch(out[-1]) and re.match(r'[A-Za-z@]', tex[i]):
                    # Keep a control word from absorbing letters on the next line.
                    out.append(' ')
            continue
        out.append(tex[i]); i += 1
    return "".join(out)


def math_token(text: str, i: int) -> tuple[int, str, str] | None:
    """Return (end position, math form, raw body), never across literal verbatim."""
    if i >= len(text) or text[i] not in "\\$" or escaped(text, i):
        return None
    m = ENV_RE.match(text, i)
    if m and m.group(1) == "begin" and m.group(2) in MATH_ENVS:
        end = find_env_end(text, i, m.group(2))
        if end:
            return end[1], "inline" if m.group(2) == "math" else m.group(2), text[m.end():end[0]]
        raise ValueError(f'Unclosed math environment: {m.group(2)}')
    for start, close, form in ((r"\(", r"\)", "inline"), (r"\[", r"\]", "block"), ("$$", "$$", "block"), ("$", "$", "inline")):
        if text.startswith(start, i) and not escaped(text, i):
            pos = i + len(start)
            depth = 0
            while pos < len(text):
                # \( enters the same inline math mode as $. Either $ or \)
                # can close it. Groups can contain text with its own math.
                closers = (r'\)', '$') if form == 'inline' else (close,)
                if depth == 0:
                    for ending in closers:
                        if text.startswith(ending, pos):
                            # In $x$$y$, the first of the adjacent dollars closes
                            # x and the next opens y; it is not a display opener.
                            return pos + len(ending), form, text[i+len(start):pos]
                    if form == 'inline' and text.startswith(r'\(', pos):
                        raise ValueError('Unexpected inline math opener inside inline math')
                if text[pos] == '\\':
                    literal = verb_end(text, pos)
                    declaration = definition_end(text, pos)
                    if literal is not None or declaration is not None:
                        pos = literal if literal is not None else declaration
                        continue
                    token = MACRO_RE.match(text, pos)
                    if token:
                        pos = token.end(); continue
                if text[pos] == '{': depth += 1
                elif text[pos] == '}': depth = max(0, depth - 1)
                pos += 1
            raise ValueError(f'Unclosed math delimiter: {start}')
    return None


def canonical_ieee_math(form: str, body: str) -> str:
    """Use the nestable IEEE box form inside the canonical equation wrapper.

    IEEE column types/glue and multicolumn commands have their own semantics;
    retain them instead of guessing an AMS aligned/array column conversion.
    See IEEEtran HOWTO, Appendix F-G (IEEEeqnarraybox).
    """
    options = []
    i = 0
    while i < len(body) and body[i] == '[':
        option = arg_at(body, i, '[', ']')
        if option is None: raise ValueError(f'Malformed {form} optional argument')
        options.append(body[i:option[1]])
        i = option[1]
        while i < len(body) and body[i].isspace(): i += 1
    is_box = form.startswith('IEEEeqnarraybox')
    if len(options) > (3 if is_box else 1):
        raise ValueError(f'Too many optional arguments for {form}')
    columns = arg_at(body, i)
    if columns is None or not columns[0].strip():
        raise ValueError(f'{form} is missing its column specification')
    content = body[columns[1]:].strip()
    # Numbering and number-reservation space are layout, not equation content.
    numbering = {r'\IEEEnonumber', r'\IEEEyesnumber', r'\IEEEyessubnumber',
                 r'\IEEEnosubnumber', r'\IEEEeqnarraynumspace', r'\nonumber', r'\notag'}
    parts = []
    i = 0
    while i < len(content):
        end = verb_end(content, i) if content[i] == '\\' else None
        if end is not None:
            parts.append(content[i:end]); i = end; continue
        token = MACRO_RE.match(content, i)
        if token:
            if token.group() not in numbering: parts.append(token.group())
            i = token.end()
        else:
            parts.append(content[i]); i += 1
    # IEEEeqnarray* changes numbering; IEEEeqnarraybox* changes row spacing.
    # Preserve the latter's star only when it was explicitly present in source.
    box = 'IEEEeqnarraybox*' if form == 'IEEEeqnarraybox*' else 'IEEEeqnarraybox'
    return ('\\begin{' + box + '}' + ''.join(options) + '{' + columns[0] + '}\n'
            + ''.join(parts).strip() + '\n\\end{' + box + '}')


def canonical_math(form: str, body: str) -> tuple[str, str]:
    body = extract_labels(body)[0].strip()
    if form == "inline":
        return "inline", "$" + body + "$"
    # Math-mode wrapping preserves the syntax of aligned/gathered expressions.
    base = form.rstrip("*")
    if base in {'IEEEeqnarray', 'IEEEeqnarraybox'}:
        body = canonical_ieee_math(form, body)
    elif base in {'alignat', 'alignedat'}:
        columns = arg_at(body, 0)
        if columns is None and body and body[0] not in '{}[]':
            # TeX mandatory arguments may be one unbraced token, e.g. }1.
            token = MACRO_RE.match(body, 0)
            end = token.end() if token else 1
            columns = body[:end], end
        if columns is None:
            raise ValueError(f'{form} is missing its column-pair argument')
        body = ('\\begin{alignedat}{' + columns[0] + '}\n'
                + body[columns[1]:].strip() + '\n\\end{alignedat}')
    elif base in {"align", "aligned", "flalign", "split"}:
        body = "\\begin{aligned}\n" + body + "\n\\end{aligned}"
    elif base in {"gather", "gathered"}:
        body = "\\begin{gathered}\n" + body + "\n\\end{gathered}"
    elif base in {"multline", "eqnarray"}:
        body = "\\begin{aligned}\n" + body + "\n\\end{aligned}"
    return "block_formula", "\\begin{equation}\n" + body + "\n\\end{equation}"


def _clean_text_segment(text: str) -> str:
    """Conservative semantic cleanup without altering unknown/macro math syntax."""
    out = []
    table_layout = bool(re.search(r'\\begin\{(?:tabular\*?|longtable)\}', text))
    i = 0
    while i < len(text):
        if text[i] not in "\\${}":
            run = TEXT_RUN_RE.match(text, i)
            out.append(run.group()); i = run.end(); continue
        if (end := verb_end(text, i)) is not None:
            out.append(text[i:end]); i = end; continue
        if (math := math_token(text, i)) is not None:
            end, kind, body = math
            out.append(canonical_math(kind, body)[1]); i = end; continue
        removed = definition_end(text, i) or layout_command_end(text, i)
        if removed is not None:
            out.append(' '); i = removed; continue
        if text[i] in '{}':
            # Plain grouping braces affect scope, not prose semantics.
            i += 1; continue
        env = ENV_RE.match(text, i)
        if env:
            if env.group(2).rstrip('*') in PROSE_ENVS:
                out.append(' '); i = env.end(); continue
            out.append(env.group()); i = env.end()
            if env.group(1) == 'begin' and env.group(2) in FLOAT_ENVS | ALGORITHM_ENVS:
                option_start = i
                while option_start < len(text) and text[option_start].isspace():
                    option_start += 1
                option = arg_at(text, option_start, '[', ']')
                if option and re.fullmatch(r'[!Hhtpb\s]+', option[0]):
                    i = option[1]
            # Keep environment arguments (column specs, widths, list options).
            if env.group(1) == 'begin':
                while i < len(text):
                    gap = i
                    while gap < len(text) and text[gap].isspace(): gap += 1
                    if gap >= len(text) or text[gap] not in '{[': break
                    opening = text[gap]
                    arg = arg_at(text, gap, opening, '}' if opening == '{' else ']')
                    if not arg: break
                    out.append(text[i:arg[1]]); i = arg[1]
            continue
        m = CMD_RE.match(text, i)
        if not m:
            # A TeX line break is layout only in prose.
            if text.startswith("\\\\", i):
                out.append("\\\\" if table_layout else " "); i += 2; continue
            if text[i] == '\\' and i + 1 < len(text):
                out.append(text[i:i+2]); i += 2; continue
            out.append(text[i]); i += 1; continue
        cmd = m.group(1); j = m.end()
        if cmd in {'caption', 'footnote', 'title', 'author', 'thanks'}:
            pieces = [text[i:j]]
            while j < len(text):
                gap = j
                while gap < len(text) and text[gap].isspace(): gap += 1
                if gap >= len(text) or text[gap] not in '{[': break
                opening = text[gap]
                closing = '}' if opening == '{' else ']'
                arg = arg_at(text, gap, opening, closing)
                if not arg: break
                pieces.append(text[j:gap] + opening + _clean_text_segment(arg[0]) + closing)
                j = arg[1]
            out.extend(pieces); i = j; continue
        if cmd in STYLE_WRAPPERS | {'textcolor', 'label', 'index', 'vspace', 'hspace', 'cline'}:
            while j < len(text) and text[j].isspace(): j += 1
        if cmd in STYLE_STANDALONE:
            out.append(" " if cmd in {"noindent", "smallskip", "medskip", "bigskip", "newpage", "clearpage"} else "")
            i = j; continue
        if cmd in STYLE_WRAPPERS and j < len(text) and text[j] == "{":
            arg = arg_at(text, j)
            if arg:
                out.append(_clean_text_segment(arg[0])); i = arg[1]; continue
        if cmd == "textcolor" and j < len(text) and text[j] == "{":
            color = arg_at(text, j)
            if color:
                content = arg_at(text, color[1])
                if content:
                    out.append(_clean_text_segment(content[0])); i = content[1]; continue
        if cmd in {"label", "index"} and j < len(text) and text[j] == "{":
            arg = arg_at(text, j)
            if arg: i = arg[1]; continue
        if cmd in SKIP_MACROS:
            while j < len(text) and text[j] in ' \t': j += 1
            if j < len(text) and text[j] == "{":
                arg = arg_at(text, j)
                if arg: i = arg[1]; continue
            i = j; continue
        if cmd in {"vspace", "hspace", "cline"}:
            if j < len(text) and text[j] == "*": j += 1
            arg = arg_at(text, j)
            if arg: i = arg[1]; continue
        # Preserve content-bearing unknown LaTeX macros: \cite, \ref, \url, \S, etc.
        # Keep semantic/custom macro arguments and citation keys intact.
        while j < len(text):
            gap = j
            while gap < len(text) and text[gap] in ' \t': gap += 1
            if gap >= len(text) or text[gap] not in '{[': break
            opening = text[gap]
            arg = arg_at(text, gap, opening, '}' if opening == '{' else ']')
            if not arg: break
            j = arg[1]
        out.append(text[i:j]); i = j
    return "".join(out)


def _squash_prose(text: str) -> str:
    # This is only called on prose; math blocks were extracted separately.
    text = re.sub(r"\s+", " ", text)
    return text.strip()


def normalize_text(text: str) -> str:
    """Normalize prose spacing without collapsing math/verbatim payloads."""
    clean = _clean_text_segment(strip_comments(text))
    parts = []
    prose = []
    i = 0
    while i < len(clean):
        if (run := PROSE_RUN_RE.match(clean, i)) is not None:
            prose.append(run.group()); i = run.end(); continue
        end = verb_end(clean, i) if clean[i] == '\\' else None
        if end is None and (token := math_token(clean, i)) is not None:
            end = token[0]
        if end is not None:
            parts.append(re.sub(r'\s+', ' ', ''.join(prose)))
            prose.clear()
            parts.append(clean[i:end]); i = end
        else:
            prose.append(clean[i]); i += 1
    parts.append(re.sub(r'\s+', ' ', ''.join(prose)))
    return ''.join(parts).strip()


def _tex_abbreviation_dots(scan: str, subs: dict[str, str]) -> set[int]:
    """Match abbreviations in a same-length view; never rewrite the leaf text."""
    view = scan.translate(str.maketrans({"~": " ", "{": " ", "}": " "}))
    view = TAG_RE.sub(
        lambda m: " " * len(m.group())
        if BOUNDARY_SPACE_RE.fullmatch(subs.get(m.group(), "")) else m.group(),
        view,
    )
    dots = {m.end() - 1 for m in LATEX_ET_AL_RE.finditer(view)}
    for journal in PHYS_REV_RE.finditer(view):
        dots.add(journal.start() + len("Phys"))
        # "Rev." may itself end a sentence. Protect it before a recognized
        # journal suffix or a volume. A single letter alone is ambiguous.
        series = PHYS_REV_SERIES_RE.match(view, journal.end())
        suffix = PHYS_REV_SUFFIX_RE.match(view, journal.end())
        if suffix or series or JOURNAL_VOLUME_RE.match(view, journal.end()):
            dots.add(journal.end() - 1)
        if series and series.group("dot"):
            dots.add(series.start("dot"))
        if suffix:
            start, end = suffix.span("suffix")
            followed_by_volume = JOURNAL_VOLUME_RE.match(view, end)
            dots.update(i for i in range(start, end) if view[i] == "."
                        and (i != end - 1 or followed_by_volume))
    return dots


def sentence_split(text: str) -> list[str]:
    """Split on period/question/exclamation, protecting math, URLs, macros, decimals."""
    masked = []
    subs: dict[str, str] = {}
    i = 0
    while i < len(text):
        end = None
        if text[i] == "\\":
            end = verb_end(text, i)
        if end is None and (math := math_token(text, i)) is not None:
            end = math[0]
        if end is None and (url := URL_RE.match(text, i)) is not None:
            end = url.end()
            while end > i and text[end-1] in ".,;:!?": end -= 1
        if end is None and text[i] == "\\":
            macro = MACRO_RE.match(text, i)
            if macro:
                end = macro.end()
                while end < len(text):
                    gap = end
                    while gap < len(text) and text[gap] in " \t": gap += 1
                    if gap < len(text) and text[gap] in "{[":
                        delim = text[gap]
                        arg = arg_at(text, gap, delim, "}" if delim == "{" else "]")
                        if arg: end = arg[1]; continue
                    break
        if end is not None and end > i:
            tag = f"\ue000{len(subs)}\ue001"
            subs[tag] = text[i:end]
            masked.append(tag)
            i = end
        else:
            masked.append(text[i]); i += 1
    scan = "".join(masked)
    protected_dots = _tex_abbreviation_dots(scan, subs)
    for match in FIGURE_DOT_RE.finditer(scan):
        following = scan[match.end():].lstrip()
        tag = TAG_RE.match(following)
        reference = subs.get(tag.group(), '') if tag else ''
        if re.match(r'\d', following) or REF_COMMAND_RE.match(reference):
            protected_dots.add(match.end() - 1)
    for pat in (ABBR_RE, INITIALISM_RE):
        for m in pat.finditer(scan):
            protected_dots.update(k for k in range(m.start(), m.end()) if scan[k] == ".")
    result = []
    start = 0
    for k, ch in enumerate(scan):
        if ch not in ".!?" or k in protected_dots: continue
        if ch == "." and 0 < k < len(scan)-1 and scan[k-1].isdigit() and scan[k+1].isdigit():
            continue
        next_i = k+1
        while next_i < len(scan) and scan[next_i] in "\"'”’)]}": next_i += 1
        # A citation attached to punctuation starts a chain. Allow whitespace
        # between its members, but do not pull a citation-led new sentence back.
        cursor = next_i
        while (tag := TAG_RE.match(scan, cursor)) is not None:
            citation = subs.get(tag.group(), '')
            if not CITATION_RE.match(citation):
                break
            next_i = tag.end()
            while next_i < len(scan) and scan[next_i] in "\"'”’)]}":
                next_i += 1
            cursor = next_i
            while cursor < len(scan) and scan[cursor].isspace():
                cursor += 1
        if next_i < len(scan) and not scan[next_i].isspace(): continue
        part = scan[start:next_i].strip()
        if part:
            part = TAG_RE.sub(lambda m: subs.get(m.group(), m.group()), part)
            result.append(part)
        start = next_i
    rem = scan[start:].strip()
    if rem:
        rem = TAG_RE.sub(lambda m: subs.get(m.group(), m.group()), rem)
        result.append(rem)
    return result


class _Builder:
    def __init__(self, outline: dict[str, Any]):
        self.outline = outline
        self.root = outline['root_node_id']
        self.prefix = f"{outline['publication_key']}@v{outline['version']}"
        self.counts: Counter[str] = Counter()
        self.nodes: list[dict[str, Any]] = [dict(node) for node in outline['nodes']]
        self.temp_ids = {x['node_id'] for x in self.nodes}
        self.generated_index = 0
        self.diagnostics: list[str] = []
        self.macro_definitions: list[dict[str, Any]] = []

    def add(self, kind: str, parent: str, content: str = "", subtype: str | None = None,
            source_segment_span: list[int] | None = None,
            labels: list[str] | None = None) -> str:
        self.generated_index += 1
        nid = f"{self.prefix}#m2_{self.generated_index:06d}"
        while nid in self.temp_ids:
            # Input IDs may already use the m2_ prefix: never crash on assert.
            self.generated_index += 1
            nid = f"{self.prefix}#m2_{self.generated_index:06d}"
        self.temp_ids.add(nid)
        node: dict[str, Any] = {"node_id": nid, "kind": kind, "parent_id": parent,
                                "content": content, "order": 0}
        if subtype is not None: node['subtype'] = subtype
        if labels: node['labels'] = labels
        if source_segment_span is not None: node['source_segment_span'] = source_segment_span
        self.nodes.append(node)
        self.counts[kind if kind != "figure" else ('table' if subtype == 'table' else 'figure')] += 1
        return nid

    def make_paragraph(self, parent: str, content: str, origin: list[int]) -> None:
        clean = normalize_text(content)
        if not clean: return
        sentences = sentence_split(clean)
        if not sentences: return
        p = self.add('paragraph', parent, content=clean, source_segment_span=origin)
        for sentence in sentences:
            self.add('sentence', p, sentence, source_segment_span=origin)

    def consume_list(self, parent: str, env_text: str, env: str, origin: list[int]) -> None:
        r"""List is a branch, each \item another branch. Handle nested lists."""
        begin = ENV_RE.match(env_text)
        endpos = find_env_end(env_text, 0, env)
        if not begin or endpos is None:
            self.diagnostics.append(f'Unclosed list environment {env}'); return
        inside = env_text[begin.end():endpos[0]]
        list_id = self.add('list', parent, content=normalize_text(inside),
                           subtype=env, source_segment_span=origin)
        items: list[str] = []
        boundaries: list[int] = []
        i = 0
        while i < len(inside):
            literal_end = verb_end(inside, i) if inside[i] == '\\' else None
            math = math_token(inside, i)
            if literal_end is not None or math is not None:
                i = literal_end if literal_end is not None else math[0]
                continue
            tok = ENV_RE.match(inside, i)
            if tok and tok.group(1) == 'begin':
                end = find_env_end(inside, i, tok.group(2))
                if end: i = end[1]; continue
            m = ITEM_RE.match(inside, i)
            if m:
                boundaries.append(i)
                i = m.end()
                while i < len(inside) and inside[i].isspace(): i += 1
                label = arg_at(inside, i, '[', ']')
                if label: i = label[1]
                continue
            if inside[i] == '\\':
                i = definition_end(inside, i) or macro_end(inside, i)
                continue
            i += 1
        if not boundaries:
            self.consume(list_id, inside, origin)
            return
        for index, start in enumerate(boundaries):
            token = ITEM_RE.match(inside, start)
            assert token
            nextpos = boundaries[index+1] if index+1 < len(boundaries) else len(inside)
            body_start = token.end()
            while body_start < nextpos and inside[body_start].isspace(): body_start += 1
            label = arg_at(inside, body_start, '[', ']')
            if label: body_start = label[1]
            body = inside[body_start:nextpos]
            label_text = normalize_text(label[0]) if label else ''
            # A description label is semantic content, not just metadata; the
            # eventual elements table only guarantees a `content` column.
            semantic_body = body
            if env == 'description' and label_text:
                semantic_body = label_text + (': ' + body.lstrip() if body.strip() else '')
            item_id = self.add('item', list_id, content=normalize_text(semantic_body), source_segment_span=origin)
            if label:
                self.nodes[-1]['item_label'] = label_text
            self.consume(item_id, semantic_body, origin)

    def consume(self, parent: str, text: str, origin: list[int]) -> None:
        """Scan prose/paragraphs, math blocks, floats and lists in source order."""
        if not text or not text.strip(): return
        text = strip_comments(text)
        # Paragraphs require genuine blank lines (not physical LaTeX line wraps).
        chunks: list[str] = []

        def flush() -> None:
            if chunks:
                part = "".join(chunks)
                chunks.clear()
                for paragraph in re.split(r'\n\s*\n', part):
                    if paragraph.strip(): self.make_paragraph(parent, paragraph, origin)

        i = 0
        while i < len(text):
            if (run := PROSE_RUN_RE.match(text, i)) is not None:
                chunks.append(run.group()); i = run.end(); continue
            # Consume a line break as one token, never scan its second slash
            # as the start of an environment or another control word.
            if text.startswith('\\\\', i):
                chunks.append(text[i:i+2]); i += 2; continue
            opaque = ENV_RE.match(text, i)
            if opaque and opaque.group(1) == 'begin' and opaque.group(2) in OPAQUE_ENVS:
                ending = find_env_end(text, i, opaque.group(2))
                if ending:
                    flush()
                    # Code listings are an opaque non-prose object. We retain the
                    # source as one atomic node in the allowed 3-leaf taxonomy.
                    self.add('figure', parent, text[i:ending[1]], subtype='listing', source_segment_span=origin)
                    i = ending[1]; continue
            if text[i] == "\\" and (end := verb_end(text, i)) is not None:
                chunks.append(text[i:end]); i = end; continue
            declaration = definition_end(text, i)
            if declaration is not None:
                self.macro_definitions.append({'source': text[i:declaration],
                                               'source_segment_span': origin})
                chunks.append(' '); i = declaration; continue
            layout_end = layout_command_end(text, i)
            if layout_end is not None:
                chunks.append(' '); i = layout_end; continue
            if (math := math_token(text, i)) is not None:
                end, form, body = math
                if form != 'inline':
                    flush()
                    self.add('block_formula', parent, canonical_math(form, body)[1], source_segment_span=origin,
                             labels=extract_labels(body)[1])
                else:
                    chunks.append(canonical_math(form, body)[1])
                i = end; continue
            m = ENV_RE.match(text, i)
            if m and m.group(1) == 'begin':
                env = m.group(2)
                if env in MATH_ENVS:
                    raise ValueError(f'Unclosed math environment {env} in segment {origin}')
                if env == 'thebibliography':
                    flush()
                    end = find_env_end(text, i, env)
                    if end is None:
                        self.diagnostics.append('Unclosed thebibliography; remaining reference text excluded')
                        return
                    i = end[1]; continue
                if env in FLOAT_ENVS | LIST_ENVS | ALGORITHM_ENVS:
                    end = find_env_end(text, i, env)
                    if end:
                        flush()
                        if env in LIST_ENVS:
                            self.consume_list(parent, text[i:end[1]], env, origin)
                        else:
                            blob = text[i:end[1]]
                            # Placement option has no semantic value.
                            self.add('figure', parent, normalize_text(blob),
                                     subtype=('algorithm' if env in ALGORITHM_ENVS else
                                              'table' if env.startswith(('table', 'tabular', 'longtable')) else 'figure'),
                                     source_segment_span=origin, labels=extract_labels(blob)[1])
                        i = end[1]; continue
                    self.diagnostics.append(f'Unclosed {env} environment in segment {origin}')
                if env.rstrip('*') in PROSE_ENVS:
                    flush()
                    i = m.end()
                    # An optional theorem/proof title is semantic text.
                    j = i
                    while j < len(text) and text[j].isspace(): j += 1
                    title = arg_at(text, j, '[', ']')
                    if title:
                        chunks.append(title[0] + ': '); i = title[1]
                    continue
            if m and m.group(1) == 'end' and m.group(2).rstrip('*') in PROSE_ENVS:
                flush(); i = m.end(); continue
            cmd = CMD_RE.match(text, i)
            if cmd and parent == self.root and cmd.group(1) in ROOT_METADATA_OPTIONS:
                # Metadata has one required argument. Extra braced groups
                # belong to following prose and must remain in the input.
                end = command_args_end(text, cmd.end(),
                                       optional=ROOT_METADATA_OPTIONS[cmd.group(1)], required=1)
                if end is not None:
                    i = end
                    continue
            if cmd and cmd.group(1) == 'includegraphics':
                # graphicx has one optional group; graphics also accepts two
                # for the legacy bounding-box syntax. Both take one filename.
                end = command_args_end(text, cmd.end(), optional=2, required=1)
                if end is not None:
                    flush()
                    self.add('figure', parent, normalize_text(text[i:end]),
                             subtype='figure', source_segment_span=origin)
                    i = end
                    continue
            if cmd and cmd.group(1) in INLINE_TEXT_ARITY:
                name = cmd.group(1)
                end = command_args_end(text, cmd.end(),
                                       optional=int(name in {'footnote', 'textcolor'}),
                                       required=INLINE_TEXT_ARITY[name])
                if end is not None:
                    # Keep inline arguments with their command. An apparent
                    # graphics command inside e.g. texttt is not a standalone figure.
                    chunks.append(text[i:end])
                    i = end
                    continue
            if cmd and cmd.group(1) in SKIP_MACROS:
                i = cmd.end()
                while i < len(text) and text[i] in ' \t': i += 1
                if i < len(text) and text[i] == '{':
                    arg = arg_at(text, i)
                    if arg: i = arg[1]
                continue
            chunks.append(text[i]); i += 1
        flush()

    def finish(self, merged_length: int) -> dict[str, Any]:
        # Restore natural child order across headings and generated leaves.
        # The source span identifies the segment (coarse for generated leaves),
        # so within the same segment retain creation order.
        def source_key(node: dict[str, Any]) -> int:
            span = node.get('span') or node.get('source_segment_span')
            return int(span[0]) if span else 0
        self.nodes.sort(key=source_key)
        siblings: Counter[str] = Counter()
        for pos, node in enumerate(self.nodes):
            node['node_role'] = ('content' if node['kind'] in {'sentence', 'block_formula', 'figure'}
                                 else 'structure')
            node['content_role'] = ('atomic' if node['node_role'] == 'content' else
                                    'aggregate' if node['kind'] in {'paragraph', 'list', 'item'} else 'title')
            # The output table needs a string content even on non-leaf nodes.
            if node['kind'] == 'heading':
                node['content'] = normalize_text(node.get('title_raw', node.get('content', '')))
            elif node['kind'] == 'document':
                node['content'] = f"Version {self.outline['version']} root"
            if 'order' in node: node['outline_order'] = node['order']
            parent = node.get('parent_id')
            node['order'] = siblings[parent]
            siblings[parent] += 1
            node['m2_traversal_index'] = pos
        out = {k: v for k, v in self.outline.items() if k not in {'nodes', 'body_segments', 'source_map', 'source_hashes'}}
        out['stage'] = 'cleanup_leaves'
        out['source_stage'] = self.outline.get('stage', 'outline')
        out['nodes'] = self.nodes
        out['macro_definitions'] = self.macro_definitions
        out['source_merged_length'] = merged_length
        out['leaf_counts'] = {k: self.counts[k] for k in ('sentence', 'block_formula', 'figure', 'table')}
        out['total_leaves'] = sum(out['leaf_counts'].values())
        out['diagnostics'] = [*self.outline.get('diagnostics', []), *self.diagnostics]
        out['input_status'] = self.outline['status']
        out['cleanup_status'] = ('empty' if self.outline['status'] != 'ok' else
                                 'completed_with_warnings' if self.diagnostics else 'completed')
        return out


def validate_outline(outline: dict[str, Any], merged_length: int) -> None:
    """Require one connected tree and disjoint source slices before generating leaves."""
    nodes = outline['nodes']
    ids = [n['node_id'] for n in nodes]
    if any(not isinstance(nid, str) or not nid for nid in ids):
        raise ValueError('Node IDs must be nonempty strings')
    if len(set(ids)) != len(ids): raise ValueError('Duplicate node_id in outline')
    by_id = {n['node_id']: n for n in nodes}
    root = outline['root_node_id']
    if root not in by_id or by_id[root].get('kind') != 'document':
        raise ValueError('root_node_id must identify a document node')
    if by_id[root].get('parent_id') is not None:
        raise ValueError('Root must have no parent')
    connected = {root}
    for nid in ids:
        path = set()
        current = nid
        while current not in connected:
            if current in path: raise ValueError('Cycle in outline parents')
            path.add(current)
            parent = by_id[current].get('parent_id')
            if parent not in by_id: raise ValueError(f'Unknown parent_id {parent}')
            current = parent
        connected.update(path)
    spans = []
    for seg in outline['body_segments']:
        if seg['parent_id'] not in by_id:
            raise ValueError(f'Unknown parent_id {seg["parent_id"]}')
        span = seg['span']
        if (not isinstance(span, (list, tuple)) or len(span) != 2
                or any(type(x) is not int for x in span)
                or not 0 <= span[0] <= span[1] <= merged_length):
            raise ValueError(f'Invalid span {span}')
        if span[0] != span[1]: spans.append(tuple(span))
    previous_end = -1
    for a, b in sorted(spans):
        if a < previous_end: raise ValueError('Overlapping or duplicate body segment spans')
        previous_end = b


def transform(merged: str | None, outline: dict[str, Any]) -> dict[str, Any]:
    """Transform one publication version. No deduplication across versions."""
    for required in ('publication_key', 'version', 'status', 'root_node_id', 'nodes', 'body_segments'):
        if required not in outline: raise ValueError(f'Missing outline field: {required}')
    builder = _Builder(outline)
    if outline['status'] != 'ok':
        # Only explicit no-source statuses are empty. Parse/IO failures must
        # remain visible, rather than inflating PDF-only coverage statistics.
        if outline['status'] not in {'empty', 'empty_no_tex', 'no_tex', 'pdf_only',
                                     'no_source', 'missing_source'}:
            raise ValueError(f"Non-empty error status: {outline['status']}")
        builder.nodes = []
        return builder.finish(len(merged or ''))
    if merged is None:
        raise FileNotFoundError('Status is ok but merged.tex is missing')
    validate_outline(outline, len(merged))
    actual_hash = hashlib.sha256(merged.encode('utf-8')).hexdigest()
    if outline.get('merged_sha256') and actual_hash != outline['merged_sha256']:
        raise ValueError('merged.tex SHA256 differs from outline.json: mismatched input pair')
    # Reference headings and their descendants are excluded; starred appendix
    # and acknowledgement headings remain ordinary hierarchy components.
    excluded = {n['node_id'] for n in builder.nodes
                if n.get('kind') == 'heading' and
                normalize_text(n.get('title_raw', n.get('content', ''))).casefold().rstrip(' \t\n:.;!?：')
                in {'references', 'bibliography', 'references and notes', 'literature cited', 'works cited'}}
    children: dict[str, list[str]] = {}
    for node in builder.nodes:
        children.setdefault(node.get('parent_id'), []).append(node['node_id'])
    pending = list(excluded)
    while pending:
        for child in children.get(pending.pop(), []):
            if child not in excluded:
                excluded.add(child); pending.append(child)
    builder.nodes = [n for n in builder.nodes if n['node_id'] not in excluded]
    valid_ids = {n['node_id'] for n in outline['nodes']}
    boundary = document_boundary(merged)
    declarations = declaration_aliases(merged)
    if declarations:
        expanded = expand_declaration_aliases(merged, declarations)
        # Some sources declare macros after \begin{document}. Read the explicit
        # declarations too, without ever treating their bodies as live formulas.
        aliases = math_aliases(expanded, len(expanded))
    else:
        aliases = math_aliases(merged, boundary[0] if boundary else len(merged))
    for seg in outline['body_segments']:
        parent = seg['parent_id']
        if parent in excluded: continue
        a,b = seg['span']
        if parent not in valid_ids: raise ValueError(f'Unknown parent_id {parent}')
        if not (0 <= a <= b <= len(merged)): raise ValueError(f'Invalid span {seg["span"]}')
        # Use original source coordinates, never an opener-shaped string in a
        # normalized slice. Retain the upstream segment span as provenance.
        start = max(a, boundary[1]) if boundary and parent == builder.root else a
        if start < b:
            segment = expand_declaration_aliases(merged[start:b], declarations)
            builder.consume(parent, expand_math_aliases(segment, aliases), [a,b])
    return builder.finish(len(merged))


def summarize(results: list[dict[str, Any]]) -> dict[str, Any]:
    stats = Counter()
    papers = set()
    keys = set()
    per_version = []
    for result in results:
        paper = result['publication_key']; version = result['version']
        if (paper, version) in keys:
            raise ValueError(f'Duplicate paper version in input: {paper} v{version}')
        keys.add((paper, version)); papers.add(paper)
        c = result['leaf_counts']
        stats.update(c)
        per_version.append({'publication_key':paper,'version':version,'status':result['status'],
                            **c,'total_leaves':result['total_leaves'],
                            **{key: result[key] for key in ('run_id', 'cleanup_status', 'output_file', 'output_sha256')
                               if key in result}})
    return {'documents':len(results), 'publications':len(papers),
            'empty_versions':sum(x['status'] != 'ok' for x in results),
            'leaf_counts':{k:stats[k] for k in ('sentence','block_formula','figure','table')},
            'total_leaves':sum(stats.values()),'per_version':sorted(per_version,key=lambda x:(x['publication_key'],x['version'])),
            'count_stage':'before_content_deduplication'}


def discover(root: Path, *, allow_empty: bool = False) -> list[tuple[Path, Path | None]]:
    """Find every outline file below root, including v2/v3 and empty versions."""
    if not root.is_dir(): raise FileNotFoundError(f'Input directory does not exist: {root}')
    outlines = sorted(root.rglob('outline.json'))
    if not outlines and not allow_empty: raise FileNotFoundError(f'No outline.json found below {root}')
    return [(p, p.with_name('merged.tex') if p.with_name('merged.tex').exists() else None) for p in outlines]


def run_pairs(pairs: list[tuple[Path, Path | None]], output_dir: Path) -> dict[str, Any]:
    """Process a batch without misclassifying upstream failures as PDF-only.

    Successful/explicitly empty versions appear in per_version. Upstream parse
    failures and bad input pairs appear separately in failed_records and never
    contribute to the leaf counts or empty-version counts.
    """
    output_dir.mkdir(parents=True, exist_ok=True)
    results: list[dict[str, Any]] = []
    failures: list[dict[str, Any]] = []
    next_progress = 10
    run_id = uuid.uuid4().hex
    seen: dict[tuple[str, int], Path] = {}
    for outline_file, merged_file in pairs:
        outline: dict[str, Any] = {}
        out: Path | None = None
        try:
            outline = json.loads(outline_file.read_text(encoding='utf-8'))
            if not isinstance(outline, dict):
                outline = {}
                raise ValueError('Outline must be a JSON object')
            paper = outline.get('publication_key')
            version = outline.get('version')
            if (not isinstance(paper, str) or not paper or paper in {'.', '..'}
                    or any(ch in paper for ch in '/\\:') or type(version) is not int or version < 1):
                raise ValueError('Invalid publication_key or version in outline')
            out = output_dir / paper / f'v{version}' / 'cleaned_nodes.json'
            out.parent.mkdir(parents=True, exist_ok=True)
            identity = (paper, version)
            if identity in seen:
                # Invalidate the first result too: neither input is authoritative.
                prior = next((r for r in results if (r['publication_key'], r['version']) == identity), None)
                if prior is not None:
                    results.remove(prior)
                    # This was written by the current run, not the prior backup.
                    out.unlink(missing_ok=True)
                    failures.append({'outline': str(seen[identity]), 'publication_key': paper,
                                     'version': version, 'input_status': prior['status'],
                                     'reason': 'Duplicate publication/version in batch',
                                     'upstream_diagnostics': []})
                raise ValueError(f'Duplicate publication/version in batch: {paper} v{version}')
            seen[identity] = outline_file
            write_run_record(out, run_id, 'processing')
            # Preserve the last successful bytes outside the canonical path:
            # even readers that glob cleaned_nodes.json cannot pick them up.
            if out.exists():
                out.replace(out.with_name('cleaned_nodes.previous.json'))
            # Avoid trying to parse an outline whose gathering stage has failed.
            # This is not equivalent to a PDF-only (empty/no_tex) version.
            if outline.get('status') == 'failed':
                raise ValueError('upstream gathering/parsing status is failed')
            merged = merged_file.read_text(encoding='utf-8') if merged_file else None
            parsed = transform(merged, outline)
            parsed['run_id'] = run_id
            # Write silently, replacing a previous result for this paper/version.
            payload = json.dumps(parsed, ensure_ascii=False, indent=2).encode('utf-8')
            atomic_write(out, payload)
            digest = hashlib.sha256(payload).hexdigest()
            write_run_record(out, run_id, parsed['cleanup_status'], output_sha256=digest)
            # Summary needs only these fields; release each full tree after writing.
            results.append({key: parsed[key] for key in
                            ('publication_key', 'version', 'status', 'leaf_counts', 'total_leaves',
                             'cleanup_status', 'run_id')} | {'output_file': str(out.resolve()), 'output_sha256': digest})
        except (ValueError, TypeError, KeyError, FileNotFoundError, OSError) as error:
            record = {
                'outline': str(outline_file),
                'publication_key': outline.get('publication_key', outline_file.parent.parent.name),
                'version': outline.get('version', outline_file.parent.name),
                'input_status': outline.get('status', 'unknown'),
                'reason': str(error),
                'upstream_diagnostics': outline.get('diagnostics', []),
            }
            failures.append(record)
            if out is not None:
                try:
                    write_run_record(out, run_id, 'failed', reason=str(error))
                except OSError as status_error:
                    record['run_record_error'] = str(status_error)
        completed = len(results) + len(failures)
        while next_progress <= 100 and completed * 100 >= len(pairs) * next_progress:
            print(f"[{next_progress}%] {completed}/{len(pairs)} IDs/versions processed "
                  f"(successful/empty: {len(results)}, failed: {len(failures)})", flush=True)
            next_progress += 10
    summary = summarize(results)
    summary['attempted_versions'] = len(pairs)
    summary['failed_versions'] = len(failures)
    summary['failed_records'] = failures
    summary['successful_or_empty_versions'] = len(results)
    summary['run_id'] = run_id
    summary['run_status'] = 'failed' if failures else 'completed'
    (output_dir / 'summary.json').write_text(
        json.dumps(summary, ensure_ascii=False, indent=2), encoding='utf-8')
    return summary


def atomic_write(path: Path, payload: bytes) -> None:
    """Publish complete bytes by replacing a temp file on the same filesystem."""
    temp_path = None
    try:
        with tempfile.NamedTemporaryFile(dir=path.parent, prefix=f'.{path.name}.',
                                         suffix='.tmp', delete=False) as handle:
            temp_path = Path(handle.name)
            handle.write(payload)
        os.replace(temp_path, path)
    finally:
        if temp_path is not None:
            temp_path.unlink(missing_ok=True)


def write_run_record(out: Path, run_id: str, status: str, **extra: Any) -> None:
    """Record current validity; the previous snapshot is never a current result."""
    record = {'run_id': run_id, 'status': status,
              'valid_for_run': status in {'completed', 'completed_with_warnings', 'empty'},
              'output_file': str(out.resolve()), **extra}
    atomic_write(out.with_name('cleanup_run.json'),
                 json.dumps(record, ensure_ascii=False, indent=2).encode('utf-8'))


def _make_empty_outline(publication_key: str, version: int, status: str) -> dict[str, Any]:
    """Document-only skeleton for a version explicitly declared PDF-only."""
    root = f'{publication_key}@v{version}#document'
    return {'schema_version': 1, 'stage': 'outline',
            'publication_key': publication_key, 'version': version,
            'status': status, 'root_node_id': root,
            'nodes': [{'node_id': root, 'kind': 'document', 'parent_id': None,
                       'order': 0, 'span': [0, 0]}],
            'body_segments': [], 'diagnostics': ['no LaTeX source available']}


def add_manifest_empties(summary: dict[str, Any], output_dir: Path, manifest_file: Path) -> dict[str, Any]:
    """Include PDF-only versions missing outline.json; do not invent missing data.

    Manifest: [{"publication_key":"2511-11709","version":2,
                "status":"empty_no_tex"}, ...].
    Only explicitly non-ok records may be synthesized. All 'ok' versions must
    have been discovered from their genuine outline/merged.tex pair.
    """
    raw = json.loads(manifest_file.read_text(encoding='utf-8'))
    records = raw['versions'] if isinstance(raw, dict) and 'versions' in raw else raw
    if not isinstance(records, list): raise ValueError('Expected a list of manifest version records')
    existing_rows = {(x['publication_key'], x['version']): x for x in summary['per_version']}
    existing = set(existing_rows)
    publications = {x['publication_key'] for x in summary['per_version']}
    summary.setdefault('run_id', uuid.uuid4().hex)
    summary.setdefault('run_status', 'failed' if summary.get('failed_versions') else 'completed')
    failed = {(x['publication_key'], str(x['version'])) for x in summary.get('failed_records', [])}
    # Validate the complete inventory before creating any new empty outputs.
    # A bad later record must not leave partially updated coverage statistics.
    pending = []
    manifest_ids: set[tuple[str, int]] = set()
    for row in records:
        if not isinstance(row, dict): raise ValueError('Manifest records must be JSON objects')
        paper = row['publication_key']; version = row['version']
        if (not isinstance(paper, str) or not paper or paper in {'.', '..'}
                or any(ch in paper for ch in '/\\:') or type(version) is not int or version < 1):
            raise ValueError('Invalid publication_key or version in manifest')
        status = row['status']
        identity = (paper, version)
        if identity in manifest_ids:
            raise ValueError(f'Duplicate manifest version: {paper} v{version}')
        manifest_ids.add(identity)
        if identity in existing:
            previous = existing_rows[identity]
            if (status == 'ok') != (previous['status'] == 'ok'):
                raise ValueError(f'Manifest conflicts with processed input: {paper} v{version}')
            if status != 'ok': transform(None, _make_empty_outline(paper, version, status))
            continue
        if (paper, str(version)) in failed:
            raise ValueError(f'Manifest conflicts with failed input: {paper} v{version}')
        if status == 'ok': raise ValueError(f'Missing files for declared TeX version: {paper} v{version}')
        parsed = transform(None, _make_empty_outline(paper, version, status))
        pending.append((paper, version, status, parsed))
    for paper, version, status, parsed in pending:
        out = output_dir / paper / f'v{version}' / 'cleaned_nodes.json'
        out.parent.mkdir(parents=True, exist_ok=True)
        parsed['run_id'] = summary['run_id']
        payload = json.dumps(parsed, ensure_ascii=False, indent=2).encode('utf-8')
        atomic_write(out, payload)
        digest = hashlib.sha256(payload).hexdigest()
        write_run_record(out, summary['run_id'], 'empty', output_sha256=digest)
        summary['per_version'].append({'publication_key': paper, 'version': version, 'status': status,
                  **parsed['leaf_counts'], 'total_leaves': 0, 'cleanup_status': 'empty',
                  'run_id': summary['run_id'], 'output_file': str(out.resolve()), 'output_sha256': digest})
        summary['documents'] += 1
        summary['empty_versions'] += 1
        summary['attempted_versions'] = summary.get('attempted_versions', len(existing)) + 1
        summary['successful_or_empty_versions'] = len(summary['per_version'])
        publications.add(paper)
        summary['publications'] = len(publications)
        existing.add((paper, version))
    summary['successful_or_empty_versions'] = len(summary['per_version'])
    summary['publications'] = len({x['publication_key'] for x in summary['per_version']})
    summary['per_version'].sort(key=lambda x:(x['publication_key'],x['version']))
    (output_dir / 'summary.json').write_text(json.dumps(summary, ensure_ascii=False, indent=2), encoding='utf-8')
    return summary


def main() -> int:
    p=argparse.ArgumentParser(description='Member 2: parse actual merged.tex + outline.json')
    g=p.add_mutually_exclusive_group(required=True)
    g.add_argument('--input-root',type=Path,help='Folder containing paper/version/outline.json + merged.tex')
    g.add_argument('--outline',type=Path,help='Single outline.json file')
    p.add_argument('--merged',type=Path,help='Single merged.tex (not required for empty versions)')
    p.add_argument('--output-dir',type=Path,required=True)
    p.add_argument('--manifest',type=Path,help='Optional explicit version inventory including PDF-only records')
    opts=p.parse_args()
    if opts.outline:
        candidate=opts.merged or opts.outline.with_name('merged.tex')
        pairs=[(opts.outline,candidate if candidate.exists() else None)]
    else: pairs=discover(opts.input_root, allow_empty=opts.manifest is not None)
    summary=run_pairs(pairs, opts.output_dir)
    if opts.manifest:
        try:
            summary=add_manifest_empties(summary, opts.output_dir, opts.manifest)
        except (ValueError, KeyError, TypeError, OSError) as exc:
            summary['run_status'] = 'failed'
            summary['manifest_error'] = str(exc)
            (opts.output_dir / 'summary.json').write_text(json.dumps(summary, ensure_ascii=False, indent=2), encoding='utf-8')
            print(f'Manifest error: {exc}')
            return 1
    if summary.get('failed_versions', 0) > 0:
        print(f"{summary['failed_versions']} error version . Detail is saved at {(opts.output_dir / 'summary.json')}")
        return 1
    return 0

if __name__=='__main__': raise SystemExit(main())
