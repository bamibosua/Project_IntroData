"""Gather a single version's literal LaTeX compilation path without compiling."""
from __future__ import annotations

from pathlib import Path, PureWindowsPath
import difflib
import hashlib
import json
import posixpath
import re
import time
import unicodedata

from .models import Diagnostic, GatherResult, decode_source_bytes
from .scanner import (
    Command, DEFINITION_COMMANDS, ENV_DEFINITION_COMMANDS, HEADING_COMMANDS,
    iter_commands, scan_errors,
)


_SUBFILE_COMMANDS = frozenset({"subfile", "subfileinclude"})
_IMPORT_COMMANDS = frozenset({"import", "subimport", "includefrom", "subincludefrom"})
_INCLUDE_COMMANDS = frozenset({"input", "include"}) | _SUBFILE_COMMANDS | _IMPORT_COMMANDS
_PATH_COMMANDS = _INCLUDE_COMMANDS | {"includeonly", "endinput"}
_STRUCTURAL_COMMANDS = _PATH_COMMANDS | set(HEADING_COMMANDS) | {"documentclass", "begin", "end", "appendix"}
_AUXILIARY_DOCUMENT_CLASSES = frozenset({"standalone", "subfiles"})
_AUXILIARY_ROOT_TOKENS = frozenset({
    "rebuttal", "response", "author_response", "review_response",
    "reviewer_response", "cover_letter", "coverletter", "letter_to_reviewers",
})
_TITLE_COMMANDS = frozenset({"title", "icmltitle", "aistatstitle", "titletop", "jtitle"})
_TITLE_FOOTNOTE_COMMANDS = frozenset({"thanks", "footnote", "footnotemark"})
_SUPPLEMENT_TITLE_PREFIXES = (
    "supplementary material", "supplementary information", "supporting information",
    "supplemental material", "appendix", "online appendix", "technical appendix",
)
_METADATA_TITLE_MIN_SCORE = 0.85
_METADATA_TITLE_MIN_MARGIN = 0.15
_LATEX_COMMENT_RE = re.compile(r"(?<!\\)%[^\n]*")
_CONDITIONAL_DOC_CLASS_RE = re.compile(r"\\documentclass\b(?:\s*\[[^\]]*\])?(?:\s*\{([^}]*)\})?")
_CONDITIONAL_BEGIN_DOC_RE = re.compile(r"\\begin\s*\{document\}")
_TEXORPDFSTRING_RE = re.compile(r"\\texorpdfstring\s*\{[^{}]*\}\s*\{([^{}]*)\}")
_LATEX_CMD_RE = re.compile(r"\\[A-Za-z@]+\*?")
_NON_ALNUM_RE = re.compile(r"[^a-z0-9]+")


def _strip_braced_commands(text: str, names: frozenset[str]) -> str:
    """Remove commands like \\thanks{...} along with their balanced arguments."""
    out: list[str] = []
    i = 0
    n = len(text)
    while i < n:
        if text[i] == "\\":
            j = i + 1
            while j < n and (text[j].isalpha() or text[j] == "@"):
                j += 1
            if text[i + 1:j] in names:
                k = j
                while k < n and text[k].isspace():
                    k += 1
                if k < n and text[k] == "[":
                    depth = 1
                    k += 1
                    while k < n and depth > 0:
                        if text[k] == "\\":
                            k += 2
                            continue
                        if text[k] == "[":
                            depth += 1
                        elif text[k] == "]":
                            depth -= 1
                        k += 1
                    while k < n and text[k].isspace():
                        k += 1
                if k < n and text[k] == "{":
                    depth = 1
                    k += 1
                    while k < n and depth > 0:
                        if text[k] == "\\":
                            k += 2
                            continue
                        if text[k] == "{":
                            depth += 1
                        elif text[k] == "}":
                            depth -= 1
                        k += 1
                    i = k
                    continue
        out.append(text[i])
        i += 1
    return "".join(out)


def _normalize_title_text(raw: str | None) -> str:
    """Normalize a LaTeX or metadata title for deterministic lexical comparison."""
    if not isinstance(raw, str) or not raw.strip():
        return ""
    cleaned = _LATEX_COMMENT_RE.sub(" ", raw)
    cleaned = _strip_braced_commands(cleaned, _TITLE_FOOTNOTE_COMMANDS)
    cleaned = _TEXORPDFSTRING_RE.sub(r" \1 ", cleaned)
    cleaned = cleaned.replace("\\\\", " ")
    decomposed = unicodedata.normalize("NFKD", cleaned)
    cleaned = "".join(ch for ch in decomposed if not unicodedata.combining(ch))
    cleaned = _LATEX_CMD_RE.sub(" ", cleaned)
    cleaned = cleaned.replace("{", " ").replace("}", " ").replace("$", " ").replace("~", " ")
    cleaned = _NON_ALNUM_RE.sub(" ", cleaned.lower())
    return " ".join(cleaned.split())


def _title_similarity(cand_norm: str, meta_norm: str) -> float:
    """Blend SequenceMatcher ratio with token Jaccard similarity."""
    if not cand_norm or not meta_norm:
        return 0.0
    if cand_norm == meta_norm:
        return 1.0
    seq = difflib.SequenceMatcher(None, cand_norm, meta_norm).ratio()
    cand_toks = set(cand_norm.split())
    meta_toks = set(meta_norm.split())
    union = cand_toks | meta_toks
    jacc = (len(cand_toks & meta_toks) / len(union)) if union else 0.0
    return 0.55 * seq + 0.45 * jacc


def _conditional_documentclass(raw_conditional: str) -> str | None:
    """Return documentclass name if an active (uncommented) \\documentclass appears inside a conditional."""
    uncommented = _LATEX_COMMENT_RE.sub("", raw_conditional)
    match = _CONDITIONAL_DOC_CLASS_RE.search(uncommented)
    if not match:
        return None
    return (match.group(1) or "").strip().casefold()


def _conditional_has_begin_document(raw_conditional: str) -> bool:
    uncommented = _LATEX_COMMENT_RE.sub("", raw_conditional)
    return bool(_CONDITIONAL_BEGIN_DOC_RE.search(uncommented))


def _within(path: Path, base: Path) -> bool:
    try:
        path.relative_to(base)
        return True
    except ValueError:
        return False


def _static(value: str | None) -> str | None:
    """Return a literal path/list; do not expand macros, parameters, or TeX escapes."""
    if value is None:
        return None
    # Comments remove their terminating newline in TeX; escaped percent stays a macro.
    cleaned = re.sub(r"(?<!\\)%[^\n]*(?:\n|$)", "", value).strip()
    if any(char in cleaned for char in "\\#$^~{}"):
        return None
    if len(cleaned) >= 2 and cleaned.startswith('"') and cleaned.endswith('"'):
        cleaned = cleaned[1:-1]
    return cleaned


def _references(body: str) -> set[str]:
    """Collect macro dependencies, including commands nested in braced arguments."""
    names: set[str] = set()
    for command in iter_commands(body, include_math=True):
        names.add(command.name)
        if command.name in DEFINITION_COMMANDS or command.name in ENV_DEFINITION_COMMANDS:
            # A nested definition does not execute its replacement yet.
            continue
        if command.name == "begin" and command.arg:
            names.add(f"env:{command.arg.strip()}")
        if command.name == "__conditional__":
            names.update(re.findall(r"\\([A-Za-z@]+)", command.arg or ""))
        elif command.arg and "\\" in command.arg:
            names.update(_references(command.arg))
    return names


class _Gatherer:
    def __init__(
        self,
        version_dir: Path,
        *,
        forced_root: str | None = None,
        forced_root_reason: str | None = None,
    ):
        self.base = version_dir.resolve()
        self.forced_root = forced_root
        self.forced_root_reason = forced_root_reason
        match = re.fullmatch(r"v([1-9]\d*)", version_dir.name)
        version = int(match.group(1)) if match else 0
        self.result = GatherResult(version_dir.parent.name, version)
        self.parts: list[str] = []
        self.offset = 0
        self.active: list[Path] = []
        self.cache: dict[Path, str] = {}
        self.definitions: dict[str, set[str]] = {}
        self.includeonly: set[str] | None = None
        self.in_document = False
        self.stopped = False
        self.root: Path | None = None
        self.root_directory = self.base
        self.read_failures: set[Path] = set()
        self.read_failure_details: dict[Path, tuple[str, str]] = {}
        self.reported_read_failures: set[Path] = set()
        self.fallback_encodings: dict[Path, str] = {}
        self.reported_fallback_encodings: set[Path] = set()
        self._files_exact: dict[str, Path] = {}
        self._files_casefold: dict[str, list[Path]] = {}

    def relative(self, path: Path) -> str:
        return path.relative_to(self.base).as_posix()

    def diagnostic(self, code: str, message: str, path: Path | None = None,
                   start: int | None = None, end: int | None = None,
                   *, severity: str = "warning", affects: bool = True) -> None:
        source_file = self.relative(path) if path is not None and _within(path, self.base) else None
        text = self.cache.get(path, "") if path is not None else ""
        span = [start, end if end is not None else start] if start is not None else None
        line = text.count("\n", 0, start) + 1 if start is not None else None
        column = start - text.rfind("\n", 0, start) if start is not None else None
        self.result.diagnostics.append(Diagnostic(
            code, message, severity, source_file, span, line, column, affects,
        ))

    def read(self, path: Path, *, probe: bool = False) -> str | None:
        if path in self.cache:
            if not probe and path in self.fallback_encodings and path not in self.reported_fallback_encodings:
                encoding = self.fallback_encodings[path]
                self.diagnostic(
                    "fallback_source_encoding",
                    f"Decoded non-UTF-8 source using {encoding} fallback.",
                    path, severity="warning", affects=False,
                )
                self.reported_fallback_encodings.add(path)
            return self.cache[path]
        if path in self.read_failures:
            if not probe and path not in self.reported_read_failures:
                code, message = self.read_failure_details[path]
                self.diagnostic(code, message, path)
                self.reported_read_failures.add(path)
            return None
        try:
            raw = path.read_bytes()
            self.result.source_hashes[self.relative(path)] = hashlib.sha256(raw).hexdigest()
            text, encoding = decode_source_bytes(raw)
        except UnicodeDecodeError:
            self.read_failures.add(path)
            self.read_failure_details[path] = (
                "invalid_source_encoding",
                "Source is not valid UTF-8/UTF-8 BOM.",
            )
            if not probe:
                self.diagnostic("invalid_source_encoding", "Source is not valid UTF-8/UTF-8 BOM.", path)
                self.reported_read_failures.add(path)
            return None
        except OSError as exc:
            self.read_failures.add(path)
            message = f"Cannot read source: {exc}"
            self.read_failure_details[path] = ("source_read_error", message)
            if not probe:
                self.diagnostic("source_read_error", message, path)
                self.reported_read_failures.add(path)
            return None
        self.cache[path] = text
        if encoding != "utf-8":
            self.fallback_encodings[path] = encoding
            if not probe:
                self.diagnostic(
                    "fallback_source_encoding",
                    f"Decoded non-UTF-8 source using {encoding} fallback.",
                    path, severity="warning", affects=False,
                )
                self.reported_fallback_encodings.add(path)
        return text

    def append(self, path: Path, instance: str, start: int, end: int) -> None:
        if end <= start:
            return
        content = self.cache[path][start:end]
        self.parts.append(content)
        self.result.source_map.append({
            "merged_span": [self.offset, self.offset + len(content)],
            "source_file": self.relative(path), "file_instance_id": instance,
            "source_span": [start, end],
        })
        self.offset += len(content)

    def effect(self, name: str) -> tuple[bool, bool]:
        names: set[str] = set()
        pending = [name]
        while pending:
            current = pending.pop()
            if current in names:
                continue
            names.add(current)
            pending.extend(self.definitions.get(current, set()) - names)
        return bool(names & _PATH_COMMANDS), bool(names & (set(HEADING_COMMANDS) | {"appendix"}))

    def macro_use(self, command: Command, path: Path) -> None:
        if command.name not in self.definitions:
            return
        affects_path, affects_heading = self.effect(command.name)
        if affects_path:
            self.diagnostic("unresolved_path_macro", f"Macro \\{command.name} may change the compilation path; it was retained raw.", path, command.start, command.end)
        if affects_heading:
            self.diagnostic("unsupported_heading_macro", f"Macro \\{command.name} may create a heading; it was retained raw.", path, command.start, command.end)

    def _index_files(self) -> list[Path]:
        """Index all in-version files for exact and case-insensitive resolution."""
        self._files_exact.clear()
        self._files_casefold.clear()
        tex_candidates: list[Path] = []
        for original in sorted(self.base.rglob("*"), key=lambda p: p.as_posix()):
            if not original.is_file():
                continue
            resolved = original.resolve()
            if not _within(resolved, self.base):
                if original.suffix.lower() == ".tex":
                    self.diagnostic(
                        "outside_version_path",
                        f"TeX candidate resolves outside the version: {self.relative(original)}",
                        severity="error",
                    )
                continue
            rel = original.relative_to(self.base).as_posix()
            self._files_exact[rel] = resolved
            bucket = self._files_casefold.setdefault(rel.casefold(), [])
            if resolved not in bucket:
                bucket.append(resolved)
            if original.suffix.lower() == ".tex":
                tex_candidates.append(original)
        return tex_candidates

    def _record_dependency(self, resolved: Path, cand_rel: str) -> None:
        self.result.dependencies.append(self.relative(resolved))
        if ".." not in PureWindowsPath(cand_rel).parts and cand_rel != ".":
            self.result.dependencies.append(cand_rel)

    def _resolve_relative_target(
        self, literal: str, base_dir: Path, command: Command, path: Path,
        *, missing_ok: bool = False, record: bool = True,
    ) -> Path | None:
        requested = Path(literal)
        windows = PureWindowsPath(literal)
        if requested.is_absolute() or windows.is_absolute() or windows.drive:
            if record:
                self.diagnostic(
                    "outside_version_path",
                    f"Absolute include path is not permitted: {literal}",
                    path, command.start, command.end,
                )
            return None
        norm_literal = literal.replace("\\", "/")
        candidates = [norm_literal] if norm_literal.lower().endswith(".tex") else [norm_literal + ".tex", norm_literal]
        try:
            base_rel = base_dir.relative_to(self.base).as_posix()
        except ValueError:
            if record:
                self.diagnostic(
                    "outside_version_path",
                    f"Include resolves outside the version: {literal}",
                    path, command.start, command.end,
                )
            return None

        cand_rels: list[str] = []
        for cand in candidates:
            joined = cand if base_rel == "." else posixpath.join(base_rel, cand)
            cand_rel = posixpath.normpath(joined)
            if cand_rel == ".." or cand_rel.startswith("../"):
                if record:
                    self.diagnostic(
                        "outside_version_path",
                        f"Include resolves outside the version: {literal}",
                        path, command.start, command.end,
                    )
                return None
            cand_rels.append(cand_rel)

        for cand_rel in cand_rels:
            if cand_rel in self._files_exact:
                resolved = self._files_exact[cand_rel]
                if record:
                    self._record_dependency(resolved, cand_rel)
                return resolved

        for cand_rel in cand_rels:
            matches = self._files_casefold.get(cand_rel.casefold(), [])
            if len(matches) == 1:
                resolved = matches[0]
                if record:
                    self.diagnostic(
                        "case_insensitive_include",
                        f"Resolved \\{command.name}{{{literal}}} case-insensitively to {self.relative(resolved)}.",
                        path, command.start, command.end,
                        severity="info", affects=False,
                    )
                    self._record_dependency(resolved, cand_rel)
                return resolved
            if len(matches) > 1:
                if record:
                    self.diagnostic(
                        "ambiguous_include_case",
                        f"Include \\{command.name}{{{literal}}} matches multiple files differing only in case.",
                        path, command.start, command.end,
                    )
                return None

        if missing_ok:
            return None
        try:
            lexical = base_dir / candidates[0]
            resolved = lexical.resolve()
        except (OSError, ValueError, RuntimeError) as exc:
            if record:
                self.diagnostic("malformed_latex", f"Invalid include path: {exc}", path, command.start, command.end)
            return None
        if not _within(resolved, self.base):
            if record:
                self.diagnostic("outside_version_path", f"Include resolves outside the version: {literal}", path, command.start, command.end)
            return None
        if record:
            self._record_dependency(resolved, cand_rels[0])
        return resolved

    def resolve_include(
        self, command: Command, path: Path,
        *, import_dir: Path | None = None, record: bool = True,
    ) -> tuple[Path | None, Path | None]:
        if command.name in _IMPORT_COMMANDS:
            dir_lit = _static(command.arg)
            file_lit = _static(command.second_arg)
            if dir_lit is None or file_lit is None or not file_lit:
                if record:
                    self.diagnostic(
                        "unresolved_path_macro",
                        f"\\{command.name} does not have nonempty literal path arguments.",
                        path, command.start, command.end,
                    )
                return None, None
            dir_win = PureWindowsPath(dir_lit)
            file_win = PureWindowsPath(file_lit)
            if Path(dir_lit).is_absolute() or dir_win.is_absolute() or dir_win.drive or Path(file_lit).is_absolute() or file_win.is_absolute() or file_win.drive:
                if record:
                    self.diagnostic(
                        "outside_version_path",
                        f"Absolute include path is not permitted: {dir_lit}/{file_lit}",
                        path, command.start, command.end,
                    )
                return None, None
            clean_dir = dir_lit.replace("\\", "/").strip("/")
            clean_file = file_lit.replace("\\", "/").lstrip("/")
            combined = clean_file if clean_dir in ("", ".") else f"{clean_dir}/{clean_file}"
            base_dir = self.root_directory if command.name in {"import", "includefrom"} else (import_dir or path.parent)
            child = self._resolve_relative_target(combined, base_dir, command, path, record=record)
            return child, (child.parent if child is not None else None)

        literal = _static(command.arg)
        if literal is None or not literal:
            if record:
                self.diagnostic("unresolved_path_macro", f"\\{command.name} does not have a nonempty literal path.", path, command.start, command.end)
            return None, None
        if command.name in _SUBFILE_COMMANDS:
            child = None
            if path.parent != self.root_directory:
                child = self._resolve_relative_target(
                    literal, path.parent, command, path, missing_ok=True, record=record,
                )
                if child is None:
                    child = self._resolve_relative_target(literal, self.root_directory, command, path, record=record)
            else:
                child = self._resolve_relative_target(literal, self.root_directory, command, path, record=record)
            return child, (child.parent if child is not None else None)

        if import_dir is not None and import_dir != self.root_directory:
            child = self._resolve_relative_target(literal, import_dir, command, path, missing_ok=True, record=record)
            if child is not None:
                return child, import_dir
        child = self._resolve_relative_target(literal, self.root_directory, command, path, record=record)
        return child, import_dir

    def conditional(self, command: Command, path: Path) -> None:
        raw = command.arg or ""
        refs = set(re.findall(r"\\([A-Za-z@]+)", raw))
        affects = bool(refs & _STRUCTURAL_COMMANDS)
        if not affects:
            affects = any(any(self.effect(name)) for name in refs if name in self.definitions)
        if affects:
            self.diagnostic("unresolved_conditional", "Conditional cannot be selected safely and may change files or headings; retained raw.", path, command.start, command.end)

    def opaque_arguments(self, command: Command, path: Path) -> None:
        """Do not silently discard a file-changing command hidden in an opaque argument."""
        if command.name in DEFINITION_COMMANDS | ENV_DEFINITION_COMMANDS | _PATH_COMMANDS | {"begin", "end"}:
            return
        refs = (
            _references(command.arg or "")
            | _references(command.optional or "")
            | _references(command.second_arg or "")
        )
        changes_path = bool(refs & _PATH_COMMANDS)
        if not changes_path:
            changes_path = any(self.effect(name)[0] for name in refs if name in self.definitions)
        if changes_path:
            self.diagnostic("unresolved_path_macro", f"File-changing command inside opaque arguments of \\{command.name}; retained raw.", path, command.start, command.end)

    @staticmethod
    def _is_auxiliary_child_doc(text: str) -> bool:
        for command in iter_commands(text):
            if command.name == "documentclass":
                return (command.arg or "").strip().casefold() in _AUXILIARY_DOCUMENT_CLASSES
            if command.name == "endinput" or (command.name in {"begin", "end"} and command.arg == "document"):
                break
        return False

    def walk(self, path: Path, parent: str | None = None,
             include: Command | None = None, *,
             import_dir: Path | None = None,
             subfile_mode: bool = False) -> bool:
        """Append one instance. Return False only when reading the file failed."""
        if path in self.active:
            raise AssertionError("Cycles must be handled at the include call site.")
        text = self.read(path)
        if text is None:
            return False
        self.result.source_texts[self.relative(path)] = text
        instance = f"f{len(self.result.compilation_path) + 1:06d}"
        self.result.compilation_path.append({
            "file_instance_id": instance, "source_file": self.relative(path),
            "parent_instance_id": parent,
            "include_command": include.name if include else None,
            "include_source_span": [include.start, include.end] if include else None,
        })
        self.active.append(path)
        extract_child_body = subfile_mode or (parent is not None and self._is_auxiliary_child_doc(text))
        has_subfile_body = extract_child_body and any(
            cmd.name == "begin" and cmd.arg == "document"
            for cmd in iter_commands(text, include_math=True)
        )
        in_subfile_body = not has_subfile_body
        subfile_body_start = 0
        cursor = 0
        examined_end = len(text)
        for command in iter_commands(text, include_math=True):
            if self.stopped:
                break
            if has_subfile_body and not in_subfile_body:
                if command.name == "begin" and command.arg == "document":
                    in_subfile_body = True
                    cursor = command.end
                    subfile_body_start = command.end
                continue
            if command.name in DEFINITION_COMMANDS:
                if command.defined_name:
                    self.definitions[command.defined_name] = _references(command.definition_body or "")
                continue
            if command.name in ENV_DEFINITION_COMMANDS:
                if command.defined_name:
                    self.definitions[f"env:{command.defined_name}"] = _references(command.definition_body or "")
                continue
            if command.name == "__conditional__":
                self.conditional(command, path)
                continue
            self.opaque_arguments(command, path)
            if command.name == "begin" and command.arg:
                env_name = command.arg.strip()
                env_key = f"env:{env_name}"
                if env_key in self.definitions:
                    affects_path, affects_heading = self.effect(env_key)
                    if affects_path:
                        self.diagnostic(
                            "unresolved_path_macro",
                            f"Environment \\begin{{{env_name}}} may change the compilation path; it was retained raw.",
                            path, command.start, command.end,
                        )
                    if affects_heading:
                        self.diagnostic(
                            "unsupported_heading_macro",
                            f"Environment \\begin{{{env_name}}} may create a heading; it was retained raw.",
                            path, command.start, command.end,
                        )
            if command.name in self.definitions:
                self.macro_use(command, path)
                # Redefined structural primitives must not be interpreted as their old meaning.
                if command.name in _STRUCTURAL_COMMANDS:
                    if not any(self.effect(command.name)):
                        self.diagnostic("unresolved_path_macro", f"Structural command \\{command.name} was redefined; retained raw.", path, command.start, command.end)
                    continue
            if command.name == "begin" and command.arg == "document":
                self.in_document = True
            if command.name == "endinput":
                self.append(path, instance, cursor, command.end)
                cursor = command.end
                examined_end = command.end
                break
            if command.name == "end" and command.arg == "document":
                if has_subfile_body:
                    self.append(path, instance, cursor, command.start)
                    cursor = command.start
                    examined_end = command.start
                    break
                self.append(path, instance, cursor, command.end)
                cursor = command.end
                examined_end = command.end
                self.stopped = True
                break
            if command.name == "includeonly":
                literal = _static(command.arg)
                if literal is None or path != self.root or self.in_document:
                    self.diagnostic("unresolved_path_macro", "Only literal \\includeonly in the root preamble is supported.", path, command.start, command.end)
                else:
                    self.includeonly = {self.include_key(item.strip()) for item in literal.split(",") if item.strip()}
                continue
            if command.name not in _INCLUDE_COMMANDS:
                continue
            if command.name == "include" and self.includeonly is not None:
                literal = _static(command.arg)
                if literal is not None and self.include_key(literal) not in self.includeonly:
                    self.diagnostic("includeonly_excluded", f"Include excluded by includeonly: {literal}", path, command.start, command.end, severity="info", affects=False)
                    continue
            child, child_import_dir = self.resolve_include(command, path, import_dir=import_dir)
            if child is None:
                continue
            if child in self.active:
                chain = " -> ".join(self.relative(p) for p in self.active + [child])
                self.diagnostic("include_cycle", f"Include cycle: {chain}", path, command.start, command.end)
                continue
            if not child.is_file():
                self.diagnostic("missing_include", f"Included file does not exist: {self.relative(child)}", path, command.start, command.end)
                continue
            if child.suffix.lower() == ".bib":
                self.diagnostic(
                    "non_tex_include",
                    f"Skipped merging BibTeX file via \\{command.name}: {self.relative(child)}",
                    path, command.start, command.end,
                    severity="info", affects=False,
                )
                continue
            # Read before replacing the command; a failed include stays raw.
            if self.read(child) is None:
                continue
            self.append(path, instance, cursor, command.start)
            self.walk(
                child, instance, command,
                import_dir=child_import_dir,
                subfile_mode=(command.name in _SUBFILE_COMMANDS),
            )
            cursor = command.end
            if self.stopped:
                examined_end = command.end
                break
        if in_subfile_body:
            if not self.stopped and examined_end == len(text):
                self.append(path, instance, cursor, len(text))
            elif not self.stopped and cursor < examined_end:
                self.append(path, instance, cursor, examined_end)
        # Malformed inactive tails after endinput/enddocument do not affect this path.
        for start, end, message in scan_errors(text[subfile_body_start:examined_end]):
            self.diagnostic("malformed_latex", message, path, start + subfile_body_start, end + subfile_body_start)
        self.active.pop()
        return True

    @staticmethod
    def include_key(literal: str) -> str:
        literal = literal.strip().replace("\\", "/")
        if literal.lower().endswith(".tex"):
            literal = literal[:-4]
        return str(Path(literal)).replace("\\", "/")

    @staticmethod
    def _is_auxiliary_root_name(path: Path) -> bool:
        stem = path.stem.casefold().replace("-", "_")
        if stem in _AUXILIARY_ROOT_TOKENS:
            return True
        return any(
            token in stem
            for token in ("rebuttal", "cover_letter", "coverletter", "author_response", "review_response", "reviewer_response")
        )

    def _referenced_child_roots(self, root_path: Path, remaining: set[Path]) -> set[Path]:
        text = self.cache.get(root_path, "")
        referenced: set[Path] = set()
        saved_root_dir = self.root_directory
        self.root_directory = root_path.parent
        try:
            for command in iter_commands(text, include_math=True):
                if command.name == "endinput" or (command.name == "end" and command.arg == "document"):
                    break
                if command.name in _INCLUDE_COMMANDS:
                    child, _ = self.resolve_include(command, root_path, record=False)
                    if child is not None and child in remaining and child != root_path:
                        referenced.add(child)
        finally:
            self.root_directory = saved_root_dir
        return referenced

    def _resolve_forced_root(self, rel_path: str) -> Path | None:
        normalized = posixpath.normpath(rel_path.strip().replace("\\", "/").lstrip("/"))
        if normalized in ("", ".") or normalized == ".." or normalized.startswith("../"):
            return None
        resolved = self._files_exact.get(normalized)
        if resolved is None:
            matches = self._files_casefold.get(normalized.casefold(), [])
            if len(matches) == 1:
                resolved = matches[0]
        if resolved is None or not _within(resolved, self.base) or not resolved.is_file():
            return None
        return resolved

    def _load_metadata_title(self) -> str | None:
        meta_path = self.base.parent / "metadata.json"
        if not meta_path.is_file():
            return None
        try:
            payload = json.loads(meta_path.read_text(encoding="utf-8"))
        except (OSError, ValueError, UnicodeError):
            return None
        if not isinstance(payload, dict):
            return None
        norm = _normalize_title_text(payload.get("title"))
        return norm or None

    def _extract_title_from_file(
        self,
        path: Path,
        *,
        root_dir: Path,
        visited: set[Path],
        depth: int = 0,
    ) -> str | None:
        if path in visited or depth > 2:
            return None
        visited.add(path)
        text = self.read(path, probe=True)
        if text is None:
            return None
        saved_root_dir = self.root_directory
        self.root_directory = root_dir
        try:
            for command in iter_commands(text):
                if command.name in _TITLE_COMMANDS and command.arg:
                    norm = _normalize_title_text(command.arg)
                    if norm:
                        return norm
                if command.name == "twocolumn" and command.optional:
                    for nested in iter_commands(command.optional):
                        if nested.name in _TITLE_COMMANDS and nested.arg:
                            norm = _normalize_title_text(nested.arg)
                            if norm:
                                return norm
                if command.name in _INCLUDE_COMMANDS and depth < 2:
                    child, _ = self.resolve_include(command, path, record=False)
                    if (
                        child is not None
                        and child.suffix.lower() == ".tex"
                        and _within(child, self.base)
                        and child.is_file()
                    ):
                        found = self._extract_title_from_file(
                            child, root_dir=root_dir, visited=visited, depth=depth + 1
                        )
                        if found:
                            return found
                if (
                    command.name in {"endinput", "maketitle", "section", "chapter"}
                    or (command.name == "end" and command.arg == "document")
                ):
                    break
        finally:
            self.root_directory = saved_root_dir
        return None

    def _resolve_by_metadata(
        self, remaining: list[Path]
    ) -> tuple[Path, list[Path], float, float] | None:
        meta_norm = self._load_metadata_title()
        if not meta_norm:
            return None
        scored: list[tuple[float, str, Path]] = []
        for cand in remaining:
            cand_norm = self._extract_title_from_file(cand, root_dir=cand.parent, visited=set())
            if not cand_norm:
                # Never discard a candidate whose title could not be extracted cleanly.
                return None
            scored.append((_title_similarity(cand_norm, meta_norm), cand_norm, cand))
        scored.sort(key=lambda item: (item[0], self.relative(item[2])), reverse=True)
        top1_score, top1_norm, top1_path = scored[0]
        top2_score = scored[1][0]
        others_are_supplements = (
            top1_norm == meta_norm
            and not meta_norm.startswith(_SUPPLEMENT_TITLE_PREFIXES)
            and all(norm.startswith(_SUPPLEMENT_TITLE_PREFIXES) for _, norm, _ in scored[1:])
        )
        if (
            top1_score >= _METADATA_TITLE_MIN_SCORE
            and ((top1_score - top2_score) >= _METADATA_TITLE_MIN_MARGIN or others_are_supplements)
        ):
            skipped = [p for p in remaining if p != top1_path]
            return top1_path, skipped, top1_score, top2_score
        return None

    def _find_conditional_roots(
        self, candidates: list[Path]
    ) -> tuple[list[Path], dict[Path, str]]:
        cond_roots: list[Path] = []
        cond_classes: dict[Path, str] = {}
        seen: set[Path] = set()
        for original in candidates:
            path = original.resolve()
            if path in seen or not _within(path, self.base):
                continue
            text = self.read(path, probe=True)
            if text is None:
                continue
            found_class: str | None = None
            has_begin_doc = False
            for command in iter_commands(text):
                if command.name == "begin" and command.arg == "document":
                    has_begin_doc = True
                    break
                if command.name == "__conditional__":
                    raw = command.arg or ""
                    if found_class is None:
                        found_class = _conditional_documentclass(raw)
                    if _conditional_has_begin_document(raw):
                        has_begin_doc = True
                        break
                if command.name == "endinput" or (command.name == "end" and command.arg == "document"):
                    break
            if found_class is not None and has_begin_doc:
                cond_roots.append(path)
                cond_classes[path] = found_class
                seen.add(path)
        return cond_roots, cond_classes

    def run(self) -> GatherResult:
        started = time.monotonic()
        if not self.base.is_dir():
            self.result.status = "failed"
            self.diagnostic("missing_version", "Version directory does not exist.", severity="error")
            return self.finish(started)
        candidates = self._index_files()
        self.result.dependencies.extend(self.relative(p) for p in candidates)
        if not candidates:
            self.result.status = "empty"
            return self.finish(started)

        used_conditional_roots = False
        if self.forced_root is not None:
            resolved_forced = self._resolve_forced_root(self.forced_root)
            if resolved_forced is None:
                self._emit_unreported_read_failures(affects=True)
                self.result.status = "failed"
                self.diagnostic(
                    "missing_root",
                    f"Forced root file was not found in version: {self.forced_root}",
                    severity="error",
                )
                return self.finish(started)
            roots = [resolved_forced]
            if self.forced_root_reason:
                self.diagnostic(
                    self.forced_root_reason,
                    f"Selected root via external resolution: {self.relative(resolved_forced)}",
                    severity="info",
                    affects=False,
                )
        else:
            roots = []
            root_seen: set[Path] = set()
            root_classes: dict[Path, str] = {}
            for original in candidates:
                path = original.resolve()
                if not _within(path, self.base):
                    continue
                text = self.read(path, probe=True)
                if text is None:
                    continue
                for command in iter_commands(text):
                    if command.name == "documentclass":
                        if path not in root_seen:
                            roots.append(path)
                            root_seen.add(path)
                            root_classes[path] = (command.arg or "").strip().casefold()
                        break
                    if command.name == "endinput" or (command.name == "end" and command.arg == "document"):
                        break
            if not roots:
                cond_roots, cond_classes = self._find_conditional_roots(candidates)
                if cond_roots:
                    roots = cond_roots
                    root_classes = cond_classes
                    used_conditional_roots = True
            if not roots:
                self._emit_unreported_read_failures(affects=True)
                self.result.status = "failed"
                self.diagnostic("missing_root", "No active documentclass root was found.", severity="error")
                return self.finish(started)
            if len(roots) > 1:
                skipped: list[Path] = []
                remaining = list(roots)
                non_aux_class = [p for p in remaining if root_classes.get(p, "") not in _AUXILIARY_DOCUMENT_CLASSES]
                if non_aux_class and len(non_aux_class) < len(remaining):
                    skipped.extend(p for p in remaining if p not in non_aux_class)
                    remaining = non_aux_class
                if len(remaining) > 1:
                    remaining_set = set(remaining)
                    child_roots: set[Path] = set()
                    for cand in remaining:
                        child_roots.update(self._referenced_child_roots(cand, remaining_set))
                    parent_roots = [p for p in remaining if p not in child_roots]
                    if parent_roots and len(parent_roots) < len(remaining):
                        skipped.extend(p for p in remaining if p not in parent_roots)
                        remaining = parent_roots
                if len(remaining) > 1:
                    non_aux_name = [p for p in remaining if not self._is_auxiliary_root_name(p)]
                    if non_aux_name and len(non_aux_name) < len(remaining):
                        skipped.extend(p for p in remaining if p not in non_aux_name)
                        remaining = non_aux_name
                if len(remaining) == 1:
                    self.diagnostic(
                        "filtered_auxiliary_root",
                        "Filtered auxiliary documentclass roots: " + ", ".join(self.relative(p) for p in skipped),
                        severity="info", affects=False,
                    )
                elif len(remaining) > 1:
                    meta_pick = self._resolve_by_metadata(remaining)
                    if meta_pick is not None:
                        chosen, meta_skipped, top1_score, top2_score = meta_pick
                        self.diagnostic(
                            "resolved_root_by_metadata",
                            f"Selected root matching metadata.json title "
                            f"(score={top1_score:.2f} vs {top2_score:.2f}): {self.relative(chosen)} "
                            f"(skipped: {', '.join(self.relative(p) for p in meta_skipped)})",
                            severity="info",
                            affects=False,
                        )
                        remaining = [chosen]
                roots = remaining
            if len(roots) != 1:
                self._emit_unreported_read_failures(affects=True)
                self.result.status = "failed"
                self.diagnostic("ambiguous_root", "Multiple documentclass roots: " + ", ".join(self.relative(p) for p in roots), severity="error")
                return self.finish(started)
            if used_conditional_roots:
                self.diagnostic(
                    "conditional_documentclass",
                    f"Selected root with \\documentclass inside conditional block: {self.relative(roots[0])}",
                    roots[0],
                    severity="warning",
                    affects=False,
                )
        self.root = roots[0]
        self.root_directory = self.root.parent
        self.result.root_file = self.relative(self.root)
        self.walk(self.root)
        self._emit_unreported_read_failures(affects=False)
        self.result.merged_text = "".join(self.parts)
        self.result.status = "partial" if any(d.affects_completeness for d in self.result.diagnostics) else "ok"
        return self.finish(started)

    def _emit_unreported_read_failures(self, *, affects: bool) -> None:
        for failed_path, (code, message) in self.read_failure_details.items():
            if failed_path in self.reported_read_failures:
                continue
            detail = message if affects else f"{message} (unused candidate outside compilation path)"
            self.diagnostic(code, detail, failed_path, severity="warning", affects=affects)
            self.reported_read_failures.add(failed_path)

    def finish(self, started: float) -> GatherResult:
        self.result.dependencies = sorted(set(self.result.dependencies))
        self.result.metrics.update({
            "gather_seconds": time.monotonic() - started,
            "source_files": len(self.result.source_texts),
            "file_instances": len(self.result.compilation_path),
        })
        return self.result


def gather_version(
    version_dir: Path,
    *,
    forced_root: str | None = None,
    forced_root_reason: str | None = None,
) -> GatherResult:
    """Read only one version, return raw merged text and exact source provenance."""
    return _Gatherer(
        Path(version_dir),
        forced_root=forced_root,
        forced_root_reason=forced_root_reason,
    ).run()
