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
from pathlib import Path
import re
from typing import Any

MATH_ENVS = {"equation", "equation*", "displaymath", "math", "align", "align*",
             "aligned", "gather", "gather*", "gathered", "multline", "multline*",
             "split", "eqnarray", "eqnarray*", "flalign", "flalign*"}
BLOCK_MATH_ENVS = MATH_ENVS - {"math"}
FLOAT_ENVS = {"figure", "figure*", "table", "table*", "longtable", "tabular", "tabular*"}
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
INITIALISM_RE = re.compile(r"\b(?:[A-Z]\.){2,}")
URL_RE = re.compile(r"(?:https?://|www\.)[^\s<>]+", re.I)
ENV_RE = re.compile(r"\\(begin|end)\{([A-Za-z0-9*@_-]+)\}")
CMD_RE = re.compile(r"\\([A-Za-z@]+)\*?")
VERB_RE = re.compile(r"\\verb\*?([^\w\s])")
MACRO_RE = re.compile(r"\\(?:[A-Za-z@]+\*?|.)")
ITEM_RE = re.compile(r'\\item\b(?:\[[^\]]*\])?')
PROSE_RUN_RE = re.compile(r"[^\\$]+")
TEXT_RUN_RE = re.compile(r"[^\\]+")
COMMENT_RUN_RE = re.compile(r"[^\\%]+")
TAG_RE = re.compile(r"\ue000[0-9]+\ue001")


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
    depth = 0
    for i in range(start, len(text)):
        if text[i] == open_ch and not escaped(text, i):
            depth += 1
        elif text[i] == close_ch and not escaped(text, i):
            depth -= 1
            if depth == 0:
                return text[start+1:i], i+1
    return None


def find_env_end(text: str, start: int, env: str) -> tuple[int, int] | None:
    begin = ENV_RE.match(text, start)
    if not begin or begin.group(1) != "begin" or begin.group(2) != env:
        return None
    depth = 1
    for token in ENV_RE.finditer(text, begin.end()):
        if token.group(2) != env:
            continue
        depth += 1 if token.group(1) == "begin" else -1
        if depth == 0:
            return token.start(), token.end()
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
    """Remove comments, leaving newlines; preserve escaped percent and verbatim."""
    out = []
    i = 0
    while i < len(tex):
        if (run := COMMENT_RUN_RE.match(tex, i)) is not None:
            out.append(run.group()); i = run.end(); continue
        if tex[i] == "\\" and (end := verb_end(tex, i)) is not None:
            out.append(tex[i:end]); i = end; continue
        if tex[i] == "%" and not escaped(tex, i):
            while i < len(tex) and tex[i] != "\n":
                i += 1
            continue
        out.append(tex[i]); i += 1
    return "".join(out)


def math_token(text: str, i: int) -> tuple[int, str, str] | None:
    """Return (end position, math form, raw body), never across literal verbatim."""
    if i >= len(text) or text[i] not in "\\$":
        return None
    m = ENV_RE.match(text, i)
    if m and m.group(1) == "begin" and m.group(2) in MATH_ENVS:
        end = find_env_end(text, i, m.group(2))
        if end:
            return end[1], "inline" if m.group(2) == "math" else m.group(2), text[m.end():end[0]]
    for start, close, form in ((r"\(", r"\)", "inline"), (r"\[", r"\]", "block"), ("$$", "$$", "block"), ("$", "$", "inline")):
        if text.startswith(start, i) and not escaped(text, i):
            pos = i + len(start)
            while pos < len(text):
                k = text.find(close, pos)
                if k < 0:
                    break
                if not escaped(text, k):
                    return k+len(close), form, text[i+len(start):k]
                pos = k+len(close)
    return None


def canonical_math(form: str, body: str) -> tuple[str, str]:
    body = body.strip()
    if form == "inline":
        return "inline", "$" + body + "$"
    # Math-mode wrapping preserves the syntax of aligned/gathered expressions.
    base = form.rstrip("*")
    if base in {"align", "aligned", "flalign", "split"}:
        body = "\\begin{aligned}\n" + body + "\n\\end{aligned}"
    elif base in {"gather", "gathered"}:
        body = "\\begin{gathered}\n" + body + "\n\\end{gathered}"
    elif base in {"multline", "eqnarray"}:
        body = "\\begin{aligned}\n" + body + "\n\\end{aligned}"
    return "block_formula", "\\begin{equation}\n" + body + "\n\\end{equation}"


def _clean_text_segment(text: str) -> str:
    """Conservative semantic cleanup without altering unknown/macro math syntax."""
    out = []
    i = 0
    while i < len(text):
        if text[i] != "\\":
            run = TEXT_RUN_RE.match(text, i)
            out.append(run.group()); i = run.end(); continue
        if (end := verb_end(text, i)) is not None:
            out.append(text[i:end]); i = end; continue
        if (math := math_token(text, i)) is not None:
            end, kind, body = math
            out.append(canonical_math(kind, body)[1]); i = end; continue
        m = CMD_RE.match(text, i)
        if not m:
            # A TeX line break is layout only in prose.
            if text.startswith("\\\\", i):
                out.append(" "); i += 2; continue
            out.append(text[i]); i += 1; continue
        cmd = m.group(1); j = m.end()
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
        out.append(text[i:j]); i = j
    return "".join(out)


def _squash_prose(text: str) -> str:
    # This is only called on prose; math blocks were extracted separately.
    text = re.sub(r"\s+", " ", text)
    return text.strip()


def normalize_text(text: str) -> str:
    """Clean a prose fragment; inline math is rewritten as $...$."""
    return _squash_prose(_clean_text_segment(strip_comments(text)))


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
    protected_dots: set[int] = set()
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

    def add(self, kind: str, parent: str, content: str = "", subtype: str | None = None,
            source_segment_span: list[int] | None = None) -> str:
        self.generated_index += 1
        nid = f"{self.prefix}#m2_{self.generated_index:06d}"
        assert nid not in self.temp_ids
        self.temp_ids.add(nid)
        node: dict[str, Any] = {"node_id": nid, "kind": kind, "parent_id": parent,
                                "content": content, "order": 0}
        if subtype is not None: node['subtype'] = subtype
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
            tok = ENV_RE.match(inside, i)
            if tok and tok.group(1) == 'begin':
                end = find_env_end(inside, i, tok.group(2))
                if end: i = end[1]; continue
            m = ITEM_RE.match(inside, i)
            if m:
                boundaries.append(i)
                i = m.end(); continue
            i += 1
        if not boundaries:
            self.consume(list_id, inside, origin)
            return
        for index, start in enumerate(boundaries):
            token = ITEM_RE.match(inside, start)
            assert token
            nextpos = boundaries[index+1] if index+1 < len(boundaries) else len(inside)
            body = inside[token.end():nextpos]
            item_id = self.add('item', list_id, content=normalize_text(body), source_segment_span=origin)
            self.consume(item_id, body, origin)

    def consume(self, parent: str, text: str, origin: list[int]) -> None:
        """Scan prose/paragraphs, math blocks, floats and lists in source order."""
        if not text or not text.strip(): return
        text = strip_comments(text)
        # First segment commonly contains title, authors and abstract. These
        # metadata are not sentences; the abstract, however, is useful prose.
        if parent == self.root and '\\begin{abstract}' in text:
            text = text[text.find('\\begin{abstract}'):]
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
            if (math := math_token(text, i)) is not None:
                end, form, body = math
                if form != 'inline':
                    flush()
                    self.add('block_formula', parent, canonical_math(form, body)[1], source_segment_span=origin)
                else:
                    chunks.append(canonical_math(form, body)[1])
                i = end; continue
            m = ENV_RE.match(text, i)
            if m and m.group(1) == 'begin':
                env = m.group(2)
                if env in FLOAT_ENVS | LIST_ENVS:
                    end = find_env_end(text, i, env)
                    if end:
                        flush()
                        if env in LIST_ENVS:
                            self.consume_list(parent, text[i:end[1]], env, origin)
                        else:
                            blob = text[i:end[1]]
                            # Placement option has no semantic value.
                            blob = re.sub(r'^(\\begin\{(?:figure\*?|table\*?)\})\s*\[[!Hhtbp]+\]',r'\1', blob)
                            self.add('figure', parent, _clean_text_segment(blob).strip(),
                                     subtype='table' if env.startswith(('table', 'tabular', 'longtable')) else 'figure',
                                     source_segment_span=origin)
                        i = end[1]; continue
                    self.diagnostics.append(f'Unclosed {env} environment in segment {origin}')
                if env in {'abstract', 'center', 'flushleft', 'flushright'}:
                    i = m.end(); continue
            if m and m.group(1) == 'end' and m.group(2) in {'abstract', 'center', 'flushleft', 'flushright', 'document'}:
                i = m.end(); continue
            cmd = CMD_RE.match(text, i)
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
            # The output table needs a string content even on non-leaf nodes.
            if node['kind'] == 'heading':
                node['content'] = normalize_text(node.get('title_raw', ''))
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
        out['source_merged_length'] = merged_length
        out['leaf_counts'] = {k: self.counts[k] for k in ('sentence', 'block_formula', 'figure', 'table')}
        out['total_leaves'] = sum(out['leaf_counts'].values())
        out['diagnostics'] = [*self.outline.get('diagnostics', []), *self.diagnostics]
        return out


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
        return builder.finish(len(merged or ''))
    if merged is None:
        raise FileNotFoundError('Status is ok but merged.tex is missing')
    actual_hash = hashlib.sha256(merged.encode('utf-8')).hexdigest()
    if outline.get('merged_sha256') and actual_hash != outline['merged_sha256']:
        raise ValueError('merged.tex SHA256 differs from outline.json: mismatched input pair')
    valid_ids = {n['node_id'] for n in outline['nodes']}
    for seg in outline['body_segments']:
        parent = seg['parent_id']
        a,b = seg['span']
        if parent not in valid_ids: raise ValueError(f'Unknown parent_id {parent}')
        if not (0 <= a <= b <= len(merged)): raise ValueError(f'Invalid span {seg["span"]}')
        builder.consume(parent, merged[a:b], [a,b])
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
                            **c,'total_leaves':result['total_leaves']})
    return {'documents':len(results), 'publications':len(papers),
            'empty_versions':sum(x['status'] != 'ok' for x in results),
            'leaf_counts':{k:stats[k] for k in ('sentence','block_formula','figure','table')},
            'total_leaves':sum(stats.values()),'per_version':sorted(per_version,key=lambda x:(x['publication_key'],x['version'])),
            'count_stage':'before_content_deduplication'}


def discover(root: Path) -> list[tuple[Path, Path | None]]:
    """Find every outline file below root, including v2/v3 and empty versions."""
    outlines = sorted(root.rglob('outline.json'))
    if not outlines: raise FileNotFoundError(f'No outline.json found below {root}')
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
    for outline_file, merged_file in pairs:
        outline: dict[str, Any] = {}
        try:
            outline = json.loads(outline_file.read_text(encoding='utf-8'))
            # Avoid trying to parse an outline whose gathering stage has failed.
            # This is not equivalent to a PDF-only (empty/no_tex) version.
            if outline.get('status') == 'failed':
                raise ValueError('upstream gathering/parsing status is failed')
            merged = merged_file.read_text(encoding='utf-8') if merged_file else None
            parsed = transform(merged, outline)
            out = output_dir / parsed['publication_key'] / f"v{parsed['version']}" / 'cleaned_nodes.json'
            out.parent.mkdir(parents=True, exist_ok=True)
            # Write silently, replacing a previous result for this paper/version.
            out.write_text(json.dumps(parsed, ensure_ascii=False, indent=2), encoding='utf-8')
            # Summary needs only these fields; release each full tree after writing.
            results.append({key: parsed[key] for key in
                            ('publication_key', 'version', 'status', 'leaf_counts', 'total_leaves')})
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
    (output_dir / 'summary.json').write_text(
        json.dumps(summary, ensure_ascii=False, indent=2), encoding='utf-8')
    return summary


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
    existing = {(x['publication_key'], x['version']) for x in summary['per_version']}
    for row in records:
        paper = row['publication_key']; version = int(row['version'])
        status = row['status']
        if (paper, version) in existing: continue
        if status == 'ok': raise ValueError(f'Missing files for declared TeX version: {paper} v{version}')
        parsed = transform(None, _make_empty_outline(paper, version, status))
        out = output_dir / paper / f'v{version}' / 'cleaned_nodes.json'
        out.parent.mkdir(parents=True, exist_ok=True)
        out.write_text(json.dumps(parsed, ensure_ascii=False, indent=2), encoding='utf-8')
        summary['per_version'].append({'publication_key': paper, 'version': version, 'status': status,
                  **parsed['leaf_counts'], 'total_leaves': 0})
        summary['documents'] += 1
        summary['empty_versions'] += 1
        existing.add((paper, version))
    summary['publications'] = len({x['publication_key'] for x in summary['per_version']})
    summary['per_version'].sort(key=lambda x:(x['publication_key'],x['version']))
    (output_dir / 'summary.json').write_text(json.dumps(summary, ensure_ascii=False, indent=2), encoding='utf-8')
    return summary


def main() -> None:
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
    else: pairs=discover(opts.input_root)
    summary=run_pairs(pairs, opts.output_dir)
    if opts.manifest:
        summary=add_manifest_empties(summary, opts.output_dir, opts.manifest)
    if summary.get('failed_versions', 0) > 0:
        print(f"{summary['failed_versions']} error version . Detail is saved at {(opts.output_dir / 'summary.json')}")

if __name__=='__main__': main()
