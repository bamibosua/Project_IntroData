"""Gather a single version's literal LaTeX compilation path without compiling."""
from __future__ import annotations

from pathlib import Path, PureWindowsPath
import hashlib
import re
import time

from .models import Diagnostic, GatherResult
from .scanner import Command, DEFINITION_COMMANDS, HEADING_COMMANDS, iter_commands, scan_errors


_PATH_COMMANDS = {"input", "include", "includeonly", "endinput"}
_STRUCTURAL_COMMANDS = _PATH_COMMANDS | set(HEADING_COMMANDS) | {"documentclass", "begin", "end", "appendix"}


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
        if command.name in DEFINITION_COMMANDS:
            # A nested definition does not execute its replacement yet.
            continue
        if command.name == "__conditional__":
            names.update(re.findall(r"\\([A-Za-z@]+)", command.arg or ""))
        elif command.arg and "\\" in command.arg:
            names.update(_references(command.arg))
    return names


class _Gatherer:
    def __init__(self, version_dir: Path):
        self.base = version_dir.resolve()
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

    def read(self, path: Path) -> str | None:
        if path in self.cache:
            return self.cache[path]
        if path in self.read_failures:
            return None
        try:
            raw = path.read_bytes()
            self.result.source_hashes[self.relative(path)] = hashlib.sha256(raw).hexdigest()
            text = raw.decode("utf-8-sig").replace("\r\n", "\n").replace("\r", "\n")
        except UnicodeDecodeError:
            self.read_failures.add(path)
            self.diagnostic("invalid_source_encoding", "Source is not valid UTF-8/UTF-8 BOM.", path)
            return None
        except OSError as exc:
            self.read_failures.add(path)
            self.diagnostic("source_read_error", f"Cannot read source: {exc}", path)
            return None
        self.cache[path] = text
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

    def resolve_include(self, command: Command, path: Path) -> Path | None:
        literal = _static(command.arg)
        if literal is None or not literal:
            self.diagnostic("unresolved_path_macro", f"\\{command.name} does not have a nonempty literal path.", path, command.start, command.end)
            return None
        requested = Path(literal)
        windows = PureWindowsPath(literal)
        if requested.is_absolute() or windows.is_absolute() or windows.drive:
            self.diagnostic("outside_version_path", f"Absolute include path is not permitted: {literal}", path, command.start, command.end)
            return None
        try:
            if not requested.suffix:
                requested = requested.with_suffix(".tex")
            lexical = self.root_directory / requested
            # Resolve '..' before recording a dependency; never expose an external path to the runner.
            resolved = lexical.resolve()
        except (OSError, ValueError, RuntimeError) as exc:
            self.diagnostic("malformed_latex", f"Invalid include path: {exc}", path, command.start, command.end)
            return None
        if not _within(resolved, self.base):
            self.diagnostic("outside_version_path", f"Include resolves outside the version: {literal}", path, command.start, command.end)
            return None
        self.result.dependencies.append(self.relative(resolved))
        # Also retain the logical symlink name so changes to its target invalidate resume.
        try:
            logical_name = lexical.absolute().relative_to(self.base).as_posix()
            if ".." not in Path(logical_name).parts:
                self.result.dependencies.append(logical_name)
        except ValueError:
            pass
        return resolved

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
        if command.name in DEFINITION_COMMANDS | _PATH_COMMANDS | {"begin", "end"}:
            return
        refs = _references(command.arg or "") | _references(command.optional or "")
        changes_path = bool(refs & _PATH_COMMANDS)
        if not changes_path:
            changes_path = any(self.effect(name)[0] for name in refs if name in self.definitions)
        if changes_path:
            self.diagnostic("unresolved_path_macro", f"File-changing command inside opaque arguments of \\{command.name}; retained raw.", path, command.start, command.end)

    def walk(self, path: Path, parent: str | None = None,
             include: Command | None = None) -> bool:
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
        cursor = 0
        examined_end = len(text)
        for command in iter_commands(text, include_math=True):
            if self.stopped:
                break
            if command.name in DEFINITION_COMMANDS:
                if command.defined_name:
                    self.definitions[command.defined_name] = _references(command.definition_body or "")
                continue
            if command.name == "__conditional__":
                self.conditional(command, path)
                continue
            self.opaque_arguments(command, path)
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
            if command.name not in {"input", "include"}:
                continue
            if command.name == "include" and self.includeonly is not None:
                literal = _static(command.arg)
                if literal is not None and self.include_key(literal) not in self.includeonly:
                    self.diagnostic("includeonly_excluded", f"Include excluded by includeonly: {literal}", path, command.start, command.end, severity="info", affects=False)
                    continue
            child = self.resolve_include(command, path)
            if child is None:
                continue
            if child in self.active:
                chain = " -> ".join(self.relative(p) for p in self.active + [child])
                self.diagnostic("include_cycle", f"Include cycle: {chain}", path, command.start, command.end)
                continue
            if not child.is_file():
                self.diagnostic("missing_include", f"Included file does not exist: {self.relative(child)}", path, command.start, command.end)
                continue
            # Read before replacing the command; a failed include stays raw.
            if self.read(child) is None:
                continue
            self.append(path, instance, cursor, command.start)
            self.walk(child, instance, command)
            cursor = command.end
            if self.stopped:
                examined_end = command.end
                break
        if not self.stopped and examined_end == len(text):
            self.append(path, instance, cursor, len(text))
        elif not self.stopped and cursor < examined_end:
            self.append(path, instance, cursor, examined_end)
        # Malformed inactive tails after endinput/enddocument do not affect this path.
        for start, end, message in scan_errors(text[:examined_end]):
            self.diagnostic("malformed_latex", message, path, start, end)
        self.active.pop()
        return True

    @staticmethod
    def include_key(literal: str) -> str:
        literal = literal.strip().replace("\\", "/")
        if literal.lower().endswith(".tex"):
            literal = literal[:-4]
        return str(Path(literal)).replace("\\", "/")

    def run(self) -> GatherResult:
        started = time.monotonic()
        if not self.base.is_dir():
            self.result.status = "failed"
            self.diagnostic("missing_version", "Version directory does not exist.", severity="error")
            return self.finish(started)
        candidates = sorted((p for p in self.base.rglob("*") if p.suffix.lower() == ".tex" and p.is_file()), key=lambda p: p.as_posix())
        self.result.dependencies.extend(self.relative(p) for p in candidates)
        if not candidates:
            self.result.status = "empty"
            return self.finish(started)
        roots: list[Path] = []
        root_seen: set[Path] = set()
        for original in candidates:
            path = original.resolve()
            if not _within(path, self.base):
                self.diagnostic("outside_version_path", f"TeX candidate resolves outside the version: {self.relative(original)}", severity="error")
                continue
            text = self.read(path)
            if text is None:
                continue
            for command in iter_commands(text):
                if command.name == "documentclass":
                    if path not in root_seen:
                        roots.append(path)
                        root_seen.add(path)
                    break
                if command.name == "endinput" or (command.name == "end" and command.arg == "document"):
                    break
        if not roots:
            self.result.status = "failed"
            self.diagnostic("missing_root", "No active documentclass root was found.", severity="error")
            return self.finish(started)
        if len(roots) != 1:
            self.result.status = "failed"
            self.diagnostic("ambiguous_root", "Multiple documentclass roots: " + ", ".join(self.relative(p) for p in roots), severity="error")
            return self.finish(started)
        self.root = roots[0]
        self.root_directory = self.root.parent
        self.result.root_file = self.relative(self.root)
        self.walk(self.root)
        self.result.merged_text = "".join(self.parts)
        self.result.status = "partial" if any(d.affects_completeness for d in self.result.diagnostics) else "ok"
        return self.finish(started)

    def finish(self, started: float) -> GatherResult:
        self.result.dependencies = sorted(set(self.result.dependencies))
        self.result.metrics.update({
            "gather_seconds": time.monotonic() - started,
            "source_files": len(self.result.source_texts),
            "file_instances": len(self.result.compilation_path),
        })
        return self.result


def gather_version(version_dir: Path) -> GatherResult:
    """Read only one version, return raw merged text and exact source provenance."""
    return _Gatherer(Path(version_dir)).run()
