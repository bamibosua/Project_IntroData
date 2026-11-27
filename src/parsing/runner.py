"""Sequential outline processing with per-version commits and validated resume."""
from __future__ import annotations

import argparse
import hashlib
import json
import os
from pathlib import Path
import re
import sys
import tempfile
import time
from datetime import datetime, timezone
from uuid import uuid4

from .gather import gather_version
from .models import SCHEMA_VERSION, STAGE, status_reasons_from_diagnostics
from .outline import build_outline
from .telemetry import peak_rss_bytes

STATUSES = ("ok", "empty", "partial", "failed")
ATOMIC_REPLACE_RETRY_DELAYS = (0.1, 0.2, 0.4, 0.8, 1.6, 2.0)
SUMMARY_RETRY_INTERVAL_SECONDS = 30.0


def configure_console() -> None:
    """Make CLI paths/diagnostics printable even under legacy Windows code pages."""
    for stream in (sys.stdout, sys.stderr):
        if hasattr(stream, "reconfigure"):
            stream.reconfigure(encoding="utf-8", errors="backslashreplace")


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _within(path: Path, directory: Path) -> bool:
    return path == directory or directory in path.parents


def _hash_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _code_signature() -> str:
    digest = hashlib.sha256(str(SCHEMA_VERSION).encode("ascii"))
    for path in sorted(Path(__file__).parent.glob("*.py")):
        digest.update(path.name.encode("utf-8") + b"\0")
        digest.update(path.read_bytes())
    return digest.hexdigest()


def _tex_paths(version_dir: Path) -> set[str]:
    base = version_dir.resolve()
    paths = set()
    for directory, subdirs, files in os.walk(base, followlinks=False):
        subdirs[:] = sorted(d for d in subdirs if not Path(directory, d).is_symlink())
        for name in files:
            if Path(name).suffix.lower() == ".tex":
                paths.add(Path(directory, name).relative_to(base).as_posix())
    return paths


def _source_manifest(version_dir: Path, dependencies: list[str], *,
                     tex_paths: set[str] | None = None) -> list[list[str]]:
    """Hash source candidates and recorded dependencies, never dereference escapes."""
    base = version_dir.resolve()
    paths = set(dependencies) | (_tex_paths(version_dir) if tex_paths is None else tex_paths)
    manifest = []
    for relative in sorted(paths):
        candidate = base / relative
        if Path(relative).is_absolute() or not _within(candidate.resolve(), base):
            state = "outside_version"
        else:
            try:
                state = _hash_file(candidate)
            except FileNotFoundError:
                state = "missing"
            except OSError as exc:
                state = f"unreadable:{type(exc).__name__}:{exc.errno}"
        manifest.append([relative, state])
    return manifest


def _fingerprint(version_dir: Path, dependencies: list[str], code: str, *,
                 manifest: list[list[str]] | None = None) -> str:
    payload = {
        "schema_version": SCHEMA_VERSION,
        "stage": STAGE,
        "code": code,
        "path_base": "root_directory",
        "sources": _source_manifest(version_dir, dependencies) if manifest is None else manifest,
    }
    return hashlib.sha256(json.dumps(payload, sort_keys=True).encode("utf-8")).hexdigest()


def _replace_with_retry(source: str, destination: Path) -> None:
    """Retry Windows access/sharing failures without changing the destination."""
    for attempt in range(len(ATOMIC_REPLACE_RETRY_DELAYS) + 1):
        try:
            os.replace(source, destination)
            return
        except OSError as exc:
            if (getattr(exc, "winerror", None) not in {5, 32, 33}
                    or attempt == len(ATOMIC_REPLACE_RETRY_DELAYS)):
                raise
            time.sleep(ATOMIC_REPLACE_RETRY_DELAYS[attempt])


def _write_atomic(path: Path, payload: bytes) -> None:
    """Replace one artifact; temporary file lives on the destination filesystem."""
    path.parent.mkdir(parents=True, exist_ok=True)
    descriptor, temporary = tempfile.mkstemp(prefix=f".{path.name}.", suffix=".tmp", dir=path.parent)
    try:
        with os.fdopen(descriptor, "wb") as stream:
            stream.write(payload)
            stream.flush()
            os.fsync(stream.fileno())
        _replace_with_retry(temporary, path)
    finally:
        try:
            os.unlink(temporary)
        except FileNotFoundError:
            pass
        except OSError as exc:
            # Preserve the original write/replace error, including its traceback.
            print(f"Cannot remove temporary file {temporary}: {exc}", file=sys.stderr)


def _json_bytes(record: dict) -> bytes:
    return (json.dumps(record, ensure_ascii=False, indent=2, allow_nan=False) + "\n").encode("utf-8")


def _span(value, size: int) -> bool:
    return (
        isinstance(value, list) and len(value) == 2
        and all(type(x) is int for x in value)
        and 0 <= value[0] <= value[1] <= size
    )


def _validate_record(record: dict, text: str) -> bool:
    """Check saved structure before accepting a checkpoint as complete."""
    required_lists = ("nodes", "body_segments", "source_map", "bibliography_regions", "diagnostics", "compilation_path")
    if not all(isinstance(record.get(key), list) for key in required_lists):
        return False
    if not all(isinstance(item, dict) for key in required_lists for item in record[key]):
        return False
    if not isinstance(record.get("metrics"), dict):
        return False
    if record.get("root_file") is not None and not isinstance(record["root_file"], str):
        return False
    for diagnostic in record["diagnostics"]:
        if (not isinstance(diagnostic.get("code"), str)
                or not isinstance(diagnostic.get("message"), str)
                or type(diagnostic.get("affects_completeness")) is not bool):
            return False
    size = len(text)
    nodes = record["nodes"]
    if record["status"] == "empty":
        return not text and not nodes and all(not record[k] for k in ("body_segments", "source_map", "bibliography_regions")) and record.get("root_node_id") is None
    if record["status"] == "ok":
        if not nodes or nodes[0].get("kind") != "document" or nodes[0].get("parent_id") is not None:
            return False
        if record.get("root_node_id") != nodes[0].get("node_id"):
            return False
    seen = set()
    try:
        for order, node in enumerate(nodes):
            identity = node["node_id"]
            if not isinstance(identity, str) or identity in seen or node["order"] != order:
                return False
            if node["kind"] not in ("document", "heading") or not _span(node["span"], size):
                return False
            if order and (node["kind"] != "heading" or node["parent_id"] not in seen):
                return False
            seen.add(identity)
        last = 0
        for item in record["source_map"]:
            start, end = item["merged_span"]
            if not _span(item["merged_span"], size) or start != last or end <= start:
                return False
            origin = item["source_span"]
            if not _span(origin, sys.maxsize) or origin[1] - origin[0] != end - start:
                return False
            last = end
        if last != size:
            return False
        excluded = [n["span"] for n in nodes if n["kind"] == "heading"]
        previous_region_end = 0
        for region in record["bibliography_regions"]:
            region_span = region["span"]
            if not _span(region_span, size) or region_span[0] < previous_region_end:
                return False
            previous_region_end = region_span[1]
            excluded.append(region_span)
        last = 0
        for segment in record["body_segments"]:
            start, end = segment["span"]
            if not _span(segment["span"], size) or start < last or segment["parent_id"] not in seen:
                return False
            if any(start < hi and lo < end for lo, hi in excluded):
                return False
            last = end
        if record["status"] == "ok" and any(d.get("affects_completeness") for d in record["diagnostics"]):
            return False
    except (KeyError, TypeError, ValueError, AttributeError):
        return False
    return True


def _reuse(version_dir: Path, destination: Path, code: str, publication: str, version: int) -> dict | None:
    try:
        record = json.loads((destination / "outline.json").read_text(encoding="utf-8"))
    except (FileNotFoundError, ValueError, UnicodeError):
        return None
    if not isinstance(record, dict):
        return None
    if (record.get("schema_version") != SCHEMA_VERSION or record.get("stage") != STAGE
            or record.get("publication_key") != publication or record.get("version") != version
            or record.get("status") not in ("ok", "empty")):
        return None
    dependencies = record.get("dependencies")
    if not isinstance(dependencies, list) or not all(isinstance(d, str) for d in dependencies):
        return None
    if record.get("input_fingerprint") != _fingerprint(version_dir, dependencies, code):
        return None
    try:
        raw = (destination / "merged.tex").read_bytes()
        text = raw.decode("utf-8")
    except (FileNotFoundError, UnicodeError):
        return None
    if hashlib.sha256(raw).hexdigest() != record.get("merged_sha256") or "\r" in text:
        return None
    return record if _validate_record(record, text) else None


def _publications(input_dir: Path, paper_id: str | None):
    base = input_dir.resolve()
    if paper_id is not None and (Path(paper_id).name != paper_id or paper_id in (".", "..")):
        raise ValueError("--paper-id must be one publication directory name")
    publications = [base / paper_id] if paper_id is not None else sorted(base.iterdir())
    for publication in publications:
        if not publication.is_dir() or not _within(publication.resolve(), base):
            continue
        yield publication


def _versions(input_dir: Path, paper_id: str | None, version: int | None):
    for publication in _publications(input_dir, paper_id):
        versions = []
        for candidate in publication.iterdir():
            match = re.fullmatch(r"v([1-9][0-9]*)", candidate.name)
            if match and candidate.is_dir() and _within(candidate.resolve(), publication.resolve()):
                number = int(match.group(1))
                if version is None or version == number:
                    versions.append((number, candidate))
        for number, directory in sorted(versions):
            yield publication.name, number, directory


def process_and_commit_version(
    directory: Path,
    destination: Path,
    code: str,
    *,
    forced_root: str | None = None,
    forced_root_reason: str | None = None,
) -> dict:
    """Gather, outline, validate, and atomically commit one version's artifacts."""
    parse_start = time.perf_counter()
    initial_candidates = _tex_paths(directory)
    initial_manifest = dict(_source_manifest(directory, [], tex_paths=initial_candidates))
    gathered = gather_version(
        directory,
        forced_root=forced_root,
        forced_root_reason=forced_root_reason,
    )
    after_gather = time.perf_counter()
    outlined = build_outline(gathered)
    record = outlined.to_record()
    after_build = time.perf_counter()
    final_candidates = _tex_paths(directory)
    manifest = _source_manifest(directory, record["dependencies"], tex_paths=final_candidates)
    final_manifest = dict(manifest)
    changed = initial_candidates ^ final_candidates
    changed.update(name for name, digest in initial_manifest.items()
                   if final_manifest.get(name) != digest)
    changed.update(name for name, digest in gathered.source_hashes.items()
                   if final_manifest.get(name) != digest)
    if changed:
        record["status"] = "partial" if record["nodes"] else "failed"
        record["diagnostics"].append({
            "code": "source_changed_during_parse", "severity": "warning",
            "message": "Sources changed while parsing; reprocess this version: " + ", ".join(sorted(changed)),
            "source_file": None, "source_span": None,
            "line": None, "column": None, "affects_completeness": True,
        })
    record["status_reasons"] = status_reasons_from_diagnostics(record["status"], record["diagnostics"])
    record["metrics"].update({
        "gather_seconds": after_gather - parse_start,
        "build_seconds": after_build - after_gather,
        "parse_seconds": after_build - parse_start,
        "unique_source_files": len({item["source_file"] for item in gathered.compilation_path}),
        "file_instances": len(gathered.compilation_path),
        "heading_count": sum(node["kind"] == "heading" for node in outlined.nodes),
    })
    if record["status"] not in STATUSES or not _validate_record(record, gathered.merged_text):
        raise RuntimeError("Parser produced an inconsistent outline/source map")
    record["input_fingerprint"] = _fingerprint(directory, record["dependencies"], code, manifest=manifest)
    payload = gathered.merged_text.encode("utf-8")
    record["merged_sha256"] = hashlib.sha256(payload).hexdigest()
    save_start = time.perf_counter()
    _write_atomic(destination / "merged.tex", payload)
    record["metrics"]["merged_save_seconds"] = time.perf_counter() - save_start
    _write_atomic(destination / "outline.json", _json_bytes(record))
    return record


def run(input_dir: Path, output_dir: Path, *, paper_id: str | None = None,
        version: int | None = None, force: bool = False, progress_every: int = 100,
        resolve_ambiguous: bool = True, readme_delay: float | None = None,
        readme_fetch_fn=None) -> int:
    """Process existing vN directories and explicitly record publications without versions."""
    start = time.perf_counter()
    input_dir, output_dir = Path(input_dir).resolve(), Path(output_dir).resolve()
    if not input_dir.is_dir():
        raise ValueError(f"Input directory does not exist: {input_dir}")
    if _within(output_dir, input_dir) or _within(input_dir, output_dir):
        raise ValueError("Input and output directories must not contain each other")
    if version is not None and (not paper_id or type(version) is not int or version <= 0):
        raise ValueError("--version requires --paper-id and a positive integer")
    if type(progress_every) is not int or progress_every <= 0:
        raise ValueError("--progress-every must be a positive integer")
    publications = list(_publications(input_dir, paper_id))
    jobs = list(_versions(input_dir, paper_id, version))
    if not publications or (version is not None and not jobs):
        raise ValueError("No matching vN source directories")
    with_versions = {publication for publication, _, _ in jobs}
    without_versions = [p for p in publications if p.name not in with_versions]
    summary = {
        "schema_version": SCHEMA_VERSION,
        "scope": "outline_module",
        "run_id": uuid4().hex,
        "started_at": _utc_now(), "finished_at": None,
        "input_dir": str(input_dir), "output_dir": str(output_dir),
        "filters": {"paper_id": paper_id, "version": version, "force": force},
        "run_status": "running", "publications_seen": 0, "versions_seen": 0,
        "status_counts": dict.fromkeys(STATUSES, 0),
        "status_reason_counts": {"partial": {}, "failed": {}},
        "versions_reused": 0, "versions_processed": 0,
        "wall_seconds": 0.0, "diagnostic_counts": {}, "fatal_error": None,
        "summary_persistence_error": None, "summary_persistence_failures": 0,
        "publications_total": len(publications), "versions_total": len(jobs),
        "publications_without_versions": [],
        "publication_empty_count": 0,
        "totals": {"nodes": 0, "headings": 0, "body_segments": 0,
                   "bibliography_regions": 0, "merged_characters": 0, "artifact_bytes": 0},
        "heading_counts": {}, "diagnostic_version_counts": {},
        "peak_rss_bytes": None,
        "python_version": sys.version, "platform": sys.platform,
    }
    publications_seen = set()
    next_summary_attempt = 0.0
    summary_warning_active = False

    def save_summary(*, force: bool = False) -> bool:
        nonlocal next_summary_attempt, summary_warning_active
        summary["wall_seconds"] = time.perf_counter() - start
        summary["peak_rss_bytes"] = peak_rss_bytes()
        if not force and time.perf_counter() < next_summary_attempt:
            return False
        summary["summary_persistence_error"] = None
        try:
            _write_atomic(output_dir / "summary.json", _json_bytes(summary))
        except OSError as exc:
            summary["summary_persistence_error"] = f"{type(exc).__name__}: {exc}"
            summary["summary_persistence_failures"] += 1
            next_summary_attempt = time.perf_counter() + SUMMARY_RETRY_INTERVAL_SECONDS
            if not summary_warning_active:
                print(
                    f"Cannot update summary.json: {exc}. "
                    "Continuing with per-version output commits; the summary "
                    f"will be retried after {SUMMARY_RETRY_INTERVAL_SECONDS:g}s and at shutdown.",
                    file=sys.stderr,
                )
                summary_warning_active = True
            return False
        next_summary_attempt = 0.0
        if summary_warning_active:
            print("Summary persistence recovered; the latest counters were saved.", file=sys.stderr)
        summary_warning_active = False
        return True

    exit_code = 0
    try:
        code = _code_signature()
        save_summary()
        for publication in without_versions:
            destination = output_dir / publication.name / "publication.json"
            if not _within(destination.resolve(), output_dir):
                raise OSError("Output publication record resolves outside output root")
            _write_atomic(destination, _json_bytes({
                "schema_version": SCHEMA_VERSION, "stage": STAGE,
                "scope": "publication", "publication_key": publication.name,
                "version": None, "status": "empty", "reason": "no_version_directories",
                "versions": [], "nodes": [], "body_segments": [], "root_node_id": None,
            }))
            summary["publications_without_versions"].append(publication.name)
            summary["publication_empty_count"] += 1
            publications_seen.add(publication.name)
            summary["publications_seen"] = len(publications_seen)
            print(f"Empty publication: {publication.name} (no version directories)", flush=True)
        for publication, number, directory in jobs:
            destination = output_dir / publication / f"v{number}"
            if not _within(destination.resolve(), output_dir):
                raise OSError("Output version directory resolves outside output root")
            # An empty publication can acquire real versions on a later run.
            marker = destination.parent / "publication.json"
            if marker.exists():
                marker.unlink()
            record = None if force else _reuse(directory, destination, code, publication, number)
            reused = record is not None
            if record is None:
                record = process_and_commit_version(directory, destination, code)
            status = record["status"]
            reasons = record.get("status_reasons")
            if not isinstance(reasons, list):
                reasons = status_reasons_from_diagnostics(status, record.get("diagnostics", []))
            summary["status_counts"][status] += 1
            if status in ("partial", "failed"):
                bucket = summary["status_reason_counts"][status]
                for reason in reasons:
                    bucket[reason] = bucket.get(reason, 0) + 1
            summary["versions_seen"] += 1
            summary["versions_reused" if reused else "versions_processed"] += 1
            publications_seen.add(publication)
            summary["publications_seen"] = len(publications_seen)
            for diagnostic in record["diagnostics"]:
                key = diagnostic["code"]
                summary["diagnostic_counts"][key] = summary["diagnostic_counts"].get(key, 0) + 1
            for key in {d["code"] for d in record["diagnostics"]}:
                counts = summary["diagnostic_version_counts"]
                counts[key] = counts.get(key, 0) + 1
            totals = summary["totals"]
            totals["nodes"] += len(record["nodes"])
            totals["headings"] += sum(n["kind"] == "heading" for n in record["nodes"])
            totals["body_segments"] += len(record["body_segments"])
            totals["bibliography_regions"] += len(record["bibliography_regions"])
            totals["merged_characters"] += (record["source_map"][-1]["merged_span"][1]
                                              if record["source_map"] else 0)
            totals["artifact_bytes"] += sum((destination / name).stat().st_size
                                              for name in ("outline.json", "merged.tex"))
            for node in record["nodes"]:
                if node["kind"] == "heading":
                    key = node["heading_command"]
                    summary["heading_counts"][key] = summary["heading_counts"].get(key, 0) + 1
            if status in ("partial", "failed"):
                exit_code = 1
            counts = summary["status_counts"]
            done = summary["versions_seen"]
            if done == 1 or done % progress_every == 0 or done == len(jobs):
                elapsed = time.perf_counter() - start
                rate = done / elapsed if elapsed else 0.0
                eta = (len(jobs) - done) / rate if rate else 0.0
                status_label = f"{status} [{', '.join(reasons)}]" if reasons else status
                print(f"[{done}/{len(jobs)}] {publication}/v{number}: {status_label}"
                      f"{' (reused)' if reused else ''}; "
                      + " ".join(f"{key}={counts[key]}" for key in STATUSES)
                      + f"; elapsed={elapsed:.1f}s rate={rate:.2f}/s ETA={eta:.1f}s", flush=True)
                save_summary()
        summary["run_status"] = "completed"
    except KeyboardInterrupt:
        summary["run_status"] = "interrupted"
        exit_code = 130
    except Exception as exc:
        summary["run_status"] = "failed"
        summary["fatal_error"] = f"{type(exc).__name__}: {exc}"
        print(f"Outline processing failed: {summary['fatal_error']}", file=sys.stderr)
        exit_code = 1
    finally:
        summary["finished_at"] = _utc_now()
        total = summary["versions_seen"]
        summary["status_rates"] = {key: count / total if total else 0.0
                                   for key, count in summary["status_counts"].items()}
        try:
            final_summary_saved = save_summary(force=True)
        except KeyboardInterrupt:
            summary["run_status"] = "interrupted"
            summary["summary_persistence_error"] = "Final summary write was interrupted."
            final_summary_saved = False
            exit_code = 130
        if not final_summary_saved:
            print(
                "Final summary was not saved; an existing summary.json may be outdated. "
                "Committed per-version outputs remain available for resume.",
                file=sys.stderr,
            )
            if exit_code != 130:
                exit_code = 1

    ambiguous_remaining = summary["status_reason_counts"]["failed"].get("ambiguous_root", 0)
    if (
        resolve_ambiguous
        and summary["run_status"] == "completed"
        and final_summary_saved
        and ambiguous_remaining > 0
    ):
        from .resolve_ambiguous import resolve_ambiguous_versions

        print(
            f"[AMBIGUOUS-PASS2] Resolving {ambiguous_remaining} remaining ambiguous_root "
            "version(s) via arXiv 00README...",
            flush=True,
        )
        kwargs = {
            "only_versions": {(pub, num) for pub, num, _ in jobs},
            "fetch_fn": readme_fetch_fn,
        }
        if readme_delay is not None:
            kwargs["delay"] = readme_delay
        try:
            pass2_report = resolve_ambiguous_versions(input_dir, output_dir, **kwargs)
            c = pass2_report["counts"]
            print(
                f"[AMBIGUOUS-PASS2] scanned={c['scanned']} resolved={c['resolved']} "
                f"no_readme={c['no_readme_in_archive']} still_ambiguous={c['still_ambiguous']} "
                f"skipped={c['skipped_previously_checked']} error={c['error']}",
                flush=True,
            )
            updated_summary = json.loads((output_dir / "summary.json").read_text(encoding="utf-8"))
            if isinstance(updated_summary, dict):
                summary = updated_summary
            if (
                summary["status_counts"].get("partial", 0) == 0
                and summary["status_counts"].get("failed", 0) == 0
                and c["error"] == 0
            ):
                exit_code = 0
            else:
                exit_code = 1
        except KeyboardInterrupt:
            exit_code = 130
        except Exception as exc:
            print(f"[AMBIGUOUS-PASS2] Failed: {type(exc).__name__}: {exc}", file=sys.stderr)
            exit_code = 1

    for key_status, label in (("partial", "Partial"), ("failed", "Failed")):
        reason_map = summary["status_reason_counts"].get(key_status, {})
        if reason_map:
            ranked = sorted(reason_map.items(), key=lambda item: (-item[1], item[0]))
            print(f"{label} reasons: " + ", ".join(f"{code}={cnt}" for code, cnt in ranked), flush=True)
    if final_summary_saved:
        print(f"Summary: {output_dir / 'summary.json'}", flush=True)
    return exit_code


def main(argv: list[str] | None = None) -> int:
    configure_console()
    parser = argparse.ArgumentParser(description="Gather LaTeX and construct a raw document/heading outline.")
    parser.add_argument("--input-dir", type=Path, default=Path("data/output/papers"))
    parser.add_argument("--output-dir", type=Path, default=Path("data/output/parsed_structure"))
    parser.add_argument("--paper-id")
    parser.add_argument("--version", type=int)
    parser.add_argument("--force", action="store_true", help="Reprocess selected versions instead of reusing valid results")
    parser.add_argument("--progress-every", type=int, default=100, help="Print progress every N versions (default: 100)")
    parser.add_argument(
        "--no-resolve-ambiguous",
        action="store_true",
        help="Skip automatic Pass 2 arXiv 00README resolution for remaining ambiguous_root versions",
    )
    parser.add_argument("--readme-delay", type=float, default=None, help="Override request delay (seconds) for Pass 2 00README fetch")
    args = parser.parse_args(argv)
    try:
        return run(
            args.input_dir,
            args.output_dir,
            paper_id=args.paper_id,
            version=args.version,
            force=args.force,
            progress_every=args.progress_every,
            resolve_ambiguous=not args.no_resolve_ambiguous,
            readme_delay=args.readme_delay,
        )
    except ValueError as exc:
        print(f"Input error: {exc}", file=sys.stderr)
        return 2
    except OSError as exc:
        print(f"Cannot inspect input: {exc}", file=sys.stderr)
        return 1
