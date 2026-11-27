"""Run Milestone 2 in parallel over the publications that already exist on disk.

Instead of finishing one stage for the whole corpus before starting the next
(parsing -> cleanup -> reference dedup -> full-text dedup), every publication
is sent as one job to a pool of worker processes. A worker runs all four stages
for its publication, so all CPU cores stay busy and a publication's data goes
through the stages while other publications are still being parsed.

Inputs : data/output/papers/<pub>/vN/...        (Milestone 1)
Outputs: data/output/parsed_structure/<pub>/vN/{outline.json,merged.tex}
         data/output/cleaned_structure/<pub>/vN/cleaned_nodes.json
         data/output/reference_output/<pub>/{references.bib,cite_map.json}, cite_key.json
         data/output/dedup_fulltext/{elements,hierarchy,coverage}.jsonl, dedup_summary.json
         data/output/benchmark/m2_benchmark_*.json

Usage: python run_m2.py [--workers N] [--limit N] [--paper-id ID]
"""

from __future__ import annotations

import argparse
from collections import deque
import contextlib
from concurrent.futures import ProcessPoolExecutor
import io
import multiprocessing
import json
import logging
import os
from pathlib import Path
import re
import shutil
import sys
import threading
import time

SRC = Path(__file__).resolve().parent
ROOT = SRC.parent
for _path in (SRC, SRC / "reference_dedup"):  # reference_dedup uses flat imports
    if str(_path) not in sys.path:
        sys.path.insert(0, str(_path))

import m2_benchmark as benchmark  # noqa: E402

logger = logging.getLogger("run_m2")
STAGE_NAMES = ("PARSE", "CLEANUP", "DEDUP_REF", "DEDUP_FULLTEXT")

DATA = ROOT / "data" / "output"
PAPERS = DATA / "papers"
PARSED = DATA / "parsed_structure"
CLEANED = DATA / "cleaned_structure"
REFERENCES = DATA / "reference_output"
DEDUP = DATA / "dedup_fulltext"
WORK = DATA / "_m2_work"
LOGS = DATA / "m2_logs"  # <stage>_errors.jsonl, one file per stage


def _version_number(path: Path) -> int:
    return int(path.name[1:])


def _publish(source: Path, destination: Path) -> None:
    """Replace destination with source (one publication directory)."""
    if destination.exists():
        shutil.rmtree(destination)
    destination.parent.mkdir(parents=True, exist_ok=True)
    shutil.move(str(source), str(destination))


_EVENTS = None  # multiprocessing queue shared with the parent, set in each worker


def _init_worker(events) -> None:
    global _EVENTS
    _EVENTS = events


def _emit(*event) -> None:
    if _EVENTS is not None:
        _EVENTS.put(event)


def _quiet(function, *args, **kwargs):
    """Call a stage that prints progress and discard its output (many workers run at once)."""
    with contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(io.StringIO()):
        return function(*args, **kwargs)


def process_publication(pid: str, papers: str, directories: dict, compact: bool = True) -> dict:
    """Worker: all four stages for one publication. Returns small picklable results."""
    from parsing.runner import run as parse_run
    from cleanup_normalize import cleanup_leaves
    from dedup_fulltext.engine import deduplicate_publication
    from reference_extractor import extract_bibitems
    from reference_parser import parse_reference
    from reference_normalizer import normalize_reference
    from reference_deduplicator import deduplicate_references, build_cite_map, save_cite_map
    from reference_exporter import export_bibtex
    from main import publication_id

    started = time.perf_counter()
    stages: dict[str, float] = {}
    outcome = {"publication": pid, "status": "ok", "versions": 0, "parse": {}, "cleanup": {},
               "references": {}, "result": None, "cite_map": {}, "error": None, "issues": [], "done": {}}
    stage = "PARSE"

    finished_stages: set[str] = set()

    def issue(stage_name, version, level, message):
        item = {"stage": stage_name, "publication": pid, "version": version,
                "level": level, "message": message}
        outcome["issues"].append(item)
        _emit("issue", item)  # shown immediately, not when the whole publication ends

    def stage_done(stage_name, nodes=None):
        if stage_name not in finished_stages:
            finished_stages.add(stage_name)
            _emit("done", stage_name, pid, nodes)

    work = Path(directories["work"]) / f"{pid}_{os.getpid()}"
    parsed, cleaned = work / "parsed", work / "cleaned"
    try:
        shutil.rmtree(work, ignore_errors=True)
        work.mkdir(parents=True)

        # 1. Multi-file gathering + hierarchy outline (parsing).
        stage = "PARSE"
        t = time.perf_counter()
        _quiet(parse_run, Path(papers), parsed, paper_id=pid, force=True)
        stages["parse"] = time.perf_counter() - t
        stage_done("PARSE")
        try:
            summary = json.loads((parsed / "summary.json").read_text(encoding="utf-8"))
            outcome["parse"] = summary.get("status_counts", {})
            outcome["versions"] = summary.get("versions_seen", 0)
        except (OSError, ValueError):
            pass
        pub_parsed = parsed / pid
        counts_text = " ".join(f"{k}={v}" for k, v in outcome["parse"].items() if v)
        outcome["done"]["PARSE"] = (f"{outcome['versions']} versions ({counts_text})"
                                    if outcome["versions"] else "no version directories")
        version_dirs = sorted((p for p in pub_parsed.glob("v*") if p.is_dir()), key=_version_number)
        for version_dir in version_dirs:
            try:
                record = json.loads((version_dir / "outline.json").read_text(encoding="utf-8"))
            except (OSError, ValueError) as exc:
                issue("PARSE", _version_number(version_dir), "error", f"outline.json unreadable: {exc}")
                continue
            if record.get("status") in ("partial", "failed"):
                codes = sorted({d.get("code", "?") for d in record.get("diagnostics", [])
                                if d.get("affects_completeness")}) or ["no completeness diagnostics"]
                issue("PARSE", _version_number(version_dir),
                      "error" if record["status"] == "failed" else "warning",
                      record["status"] + ": " + ", ".join(codes))

        # 2. Cleanup / normalization into leaf nodes.
        stage = "CLEANUP"
        t = time.perf_counter()
        pairs = cleanup_leaves.discover(parsed, allow_empty=True)
        if pairs:
            result = _quiet(cleanup_leaves.run_pairs, pairs, cleaned)
            outcome["cleanup"] = {"successful_or_empty": result.get("successful_or_empty_versions", 0),
                                  "failed": result.get("failed_versions", 0)}
            for record in result.get("failed_records", []):
                if record.get("input_status") == "failed":
                    continue  # already reported by the PARSE stage
                issue("CLEANUP", record.get("version"), "error", str(record.get("reason")))
        stages["cleanup"] = time.perf_counter() - t
        stage_done("CLEANUP")
        outcome["done"]["CLEANUP"] = (
            f"{outcome['cleanup'].get('successful_or_empty', 0)} versions cleaned, "
            f"{outcome['cleanup'].get('failed', 0)} rejected" if pairs else "nothing to clean")

        # 3. Reference extraction + cross-version reference deduplication.
        stage = "DEDUP_REF"
        t = time.perf_counter()
        version_refs = {}
        for version_dir in version_dirs:
            tex = version_dir / "merged.tex"
            try:
                raw = extract_bibitems(tex) if tex.exists() else []
                version_refs[version_dir.name] = [normalize_reference(parse_reference(r)) for r in raw]
            except Exception as exc:  # keep the other versions' references
                version_refs[version_dir.name] = []
                issue("DEDUP_REF", _version_number(version_dir), "error",
                      f"extraction failed: {type(exc).__name__}: {exc}")
        cite_map = {}
        if any(version_refs.values()):
            ref_out = work / "references"
            ref_out.mkdir()
            unique, mapping = deduplicate_references(version_refs)
            cite_map, ambiguous = build_cite_map(mapping)
            save_cite_map(cite_map, publication_id(pid), ref_out)
            if ambiguous:
                issue("DEDUP_REF", None, "warning",
                      "citation keys reused for different references across versions, not renamed: "
                      + "; ".join(k + " -> " + "/".join(v) for k, v in ambiguous.items()))
            for ref in unique:
                ref["source_arxiv_id"] = pid
            export_bibtex(unique, ref_out / "references.bib")
            outcome["references"] = {"raw": sum(map(len, version_refs.values())), "unique": len(unique)}
            _publish(ref_out, Path(directories["references"]) / pid)
        else:
            shutil.rmtree(Path(directories["references"]) / pid, ignore_errors=True)
        outcome["cite_map"] = cite_map
        stages["references"] = time.perf_counter() - t
        stage_done("DEDUP_REF")
        outcome["done"]["DEDUP_REF"] = (
            f"{outcome['references']['raw']} raw -> {outcome['references']['unique']} unique entries"
            if outcome["references"] else "no references found")

        # 4. Full-text deduplication across the versions of this publication.
        stage = "DEDUP_FULLTEXT"
        t = time.perf_counter()
        docs = []
        for node_file in sorted((cleaned / pid).glob("v*/cleaned_nodes.json"),
                                key=lambda p: _version_number(p.parent)):
            doc = json.loads(node_file.read_text(encoding="utf-8"))
            doc["version"] = _version_number(node_file.parent)
            docs.append(doc)
        nodes_pair = (0, 0)
        if docs:
            outcome["result"] = deduplicate_publication(
                pid, docs, cite_key_map=cite_map or None, compact_containers=compact)
            stats = outcome["result"]["stats"]
            nodes_pair = (stats["nodes_before"], stats["nodes_after"])
            for field, text in (("orphan_edges", "edges skipped (parent missing)"),
                                ("leaf_mismatch_versions", "versions whose leaf count differs from cleanup"),
                                ("duplicate_versions", "duplicate version documents skipped")):
                if stats.get(field):
                    issue("DEDUP_FULLTEXT", None, "warning", f"{stats[field]} {text}")
        stages["dedup"] = time.perf_counter() - t
        stage_done("DEDUP_FULLTEXT", nodes_pair)
        outcome["done"]["DEDUP_FULLTEXT"] = (
            f"{nodes_pair[0]} -> {nodes_pair[1]} elements" if docs
            else "skipped (no cleaned versions)")

        # Publish per-publication artifacts only after every stage succeeded.
        if pub_parsed.exists():
            _publish(pub_parsed, Path(directories["parsed"]) / pid)
        if (cleaned / pid).exists():
            _publish(cleaned / pid, Path(directories["cleaned"]) / pid)
        if not docs:
            outcome["status"] = "empty"
    except Exception as exc:  # one broken publication must not stop the corpus
        outcome["status"] = "failed"
        outcome["error"] = f"{type(exc).__name__}: {exc}"
        issue(stage, None, "error", "stage aborted: " + outcome["error"])
    finally:
        shutil.rmtree(work, ignore_errors=True)
        for name in STAGE_NAMES:  # an aborted publication still counts as handled by every stage
            stage_done(name)
    outcome["stages"] = stages
    outcome["elapsed"] = time.perf_counter() - started
    return outcome


PROGRESS_EVERY = 50


def _watch(events, total, started, issue_counts, issue_publications, issue_files) -> None:
    """Parent thread: every stage counts the publications it has finished on its own, reports every
    PROGRESS_EVERY of them, and prints errors/warnings of that stage the moment they happen."""
    finished = dict.fromkeys(STAGE_NAMES, 0)
    dedup_nodes = [0, 0]  # [before, after] cumulative for DEDUP_FULLTEXT
    while True:
        event = events.get()
        if event is None:
            return
        if event[0] == "issue":
            item = event[1]
            issue_counts[item["stage"]][item["level"]] += 1
            issue_publications[item["stage"]].add(item["publication"])
            issue_files[item["stage"]].write(json.dumps(item, ensure_ascii=False) + "\n")
            issue_files[item["stage"]].flush()
            where = item["publication"] + (f" v{item['version']}" if item["version"] else "")
            (logger.error if item["level"] == "error" else logger.warning)(
                "[%s] %s %s: %s", item["stage"], item["level"].upper(), where, item["message"])
            continue
        name = event[1]
        nodes = event[3] if len(event) > 3 else None
        if name == "DEDUP_FULLTEXT" and nodes is not None:
            dedup_nodes[0] += nodes[0]
            dedup_nodes[1] += nodes[1]
        finished[name] += 1
        count = finished[name]
        if count % PROGRESS_EVERY == 0 or count == total:
            elapsed = time.perf_counter() - started
            rate = count / elapsed if elapsed else 0.0
            extra = f" | {dedup_nodes[0]:,} -> {dedup_nodes[1]:,} nodes" if name == "DEDUP_FULLTEXT" else ""
            logger.info("[%s] %d/%d publications%s | %d errors, %d warnings | %.1f pub/s, ETA %.0fs",
                        name, count, total, extra, issue_counts[name]["error"], issue_counts[name]["warning"],
                        rate, (total - count) / rate if rate else 0.0)


def _select(args) -> list[str]:
    if not PAPERS.is_dir():
        raise SystemExit(f"Input directory does not exist: {PAPERS}")
    names = sorted(p.name for p in PAPERS.iterdir() if p.is_dir())
    if args.paper_id:
        names = [n for n in names if n == args.paper_id]
    return names[: args.limit] if args.limit else names


def parse_args(argv=None):
    parser = argparse.ArgumentParser(description="Run Milestone 2 in parallel per publication.")
    parser.add_argument("--workers", type=int, default=os.cpu_count() or 1,
                        help="worker processes (default: CPU count)")
    parser.add_argument("--limit", type=int, help="only the first N publications")
    parser.add_argument("--paper-id", help="only this publication directory name")
    parser.add_argument("--keep-container-text", action="store_true",
                        help="store full text in paragraphs/lists/items as well")
    return parser.parse_args(argv)


def main(argv=None) -> int:
    args = parse_args(argv)
    from dedup_fulltext.runner import _ShardWriter, _concat, TABLES, _write_json_atomic

    names = _select(args)
    if not names:
        logger.error("[M2] No publications found in %s.", PAPERS)
        return 1
    workers = max(1, min(args.workers, len(names)))
    directories = {k: str(v) for k, v in (("work", WORK), ("parsed", PARSED), ("cleaned", CLEANED),
                                           ("references", REFERENCES))}
    shutil.rmtree(WORK, ignore_errors=True)
    DEDUP.mkdir(parents=True, exist_ok=True)
    shard_dir = DEDUP / "_shards"
    shutil.rmtree(shard_dir, ignore_errors=True)
    shard_dir.mkdir(parents=True)

    measurement = benchmark.Benchmark(ROOT, workers, [PARSED, CLEANED, REFERENCES, DEDUP])
    measurement.start()
    writer = _ShardWriter(shard_dir, 1)
    all_cite_maps, counts, failures = {}, {"ok": 0, "empty": 0, "failed": 0}, []
    issue_counts = {name: {"warning": 0, "error": 0} for name in STAGE_NAMES}
    issue_publications = {name: set() for name in STAGE_NAMES}
    DEDUP.mkdir(parents=True, exist_ok=True)
    LOGS.mkdir(parents=True, exist_ok=True)
    issue_files = {name: (LOGS / f"{name.lower()}_errors.jsonl").open("w", encoding="utf-8")
                   for name in STAGE_NAMES}
    started = time.perf_counter()
    logger.info("[M2] Starting parse, cleanup, references and dedup: %d publications, %d workers.",
                len(names), workers)
    code = 0
    events = multiprocessing.Queue()
    watcher = threading.Thread(
        target=_watch, name="m2-progress", daemon=True,
        args=(events, len(names), started, issue_counts, issue_publications, issue_files))
    watcher.start()
    try:
        with ProcessPoolExecutor(max_workers=workers, initializer=_init_worker,
                                 initargs=(events,)) as pool:
            pending, iterator, done = deque(), iter(names), 0
            window = workers * 4  # bounded backlog keeps memory flat; results stay in input order

            def submit():
                name = next(iterator, None)
                if name is not None:
                    pending.append(pool.submit(process_publication, name, str(PAPERS), directories,
                                               not args.keep_container_text))
                return name is not None

            for _ in range(window):
                if not submit():
                    break
            while pending:
                outcome = pending.popleft().result()
                submit()
                done += 1
                counts[outcome["status"]] += 1
                if outcome["status"] == "failed":
                    failures.append({"publication": outcome["publication"], "error": outcome["error"]})
                if outcome["result"] is not None:
                    writer.add(outcome["result"])
                if outcome["cite_map"]:
                    all_cite_maps[re.sub(r"^(\d{4})-", r"\1.", outcome["publication"])] = outcome["cite_map"]
                measurement.record(outcome["publication"], outcome["elapsed"], outcome["stages"],
                                   outcome["status"], outcome["versions"])
        events.put(None)  # every worker has exited: flush the remaining progress lines
        watcher.join()
        writer.commit(time.perf_counter() - started)
        for table in TABLES:
            _concat(shard_dir, ["shard_00001"], table, DEDUP / f"{table}.jsonl")
        shutil.rmtree(shard_dir, ignore_errors=True)
        REFERENCES.mkdir(parents=True, exist_ok=True)
        (REFERENCES / "cite_key.json").write_text(
            json.dumps(dict(sorted(all_cite_maps.items())), ensure_ascii=False, indent=2), encoding="utf-8")
        summary = {
            "stage": "m2_parallel_pipeline",
            "workers": workers,
            "publications": len(names),
            "publication_status": counts,
            "failures": failures,
            "coverage": {"versions": writer.versions, "versions_with_elements": writer.ok_versions,
                         "empty_versions": writer.empty_versions},
            "all_publications": writer.everyone.report(),
            "multi_version_publications": writer.multi.report(),
        }
        _write_json_atomic(DEDUP / "dedup_summary.json", summary)
        code = 1 if counts["failed"] else 0
        summary["issues_by_stage"] = {
            name: {**issue_counts[name], "publications": len(issue_publications[name])}
            for name in STAGE_NAMES}
        _write_json_atomic(DEDUP / "dedup_summary.json", summary)
        logger.info("[M2] Issues by stage (details in %s):", LOGS)
        for name in STAGE_NAMES:
            logger.info("[%s] %d errors, %d warnings in %d publications.", name,
                        issue_counts[name]["error"], issue_counts[name]["warning"],
                        len(issue_publications[name]))
    except KeyboardInterrupt:
        writer.abort()
        logger.info("[M2] Interrupted.")
        code = 130
    finally:
        if watcher.is_alive():
            events.put(None)
            watcher.join(5)
        for handle in issue_files.values():
            handle.close()
        shutil.rmtree(WORK, ignore_errors=True)
        try:
            measurement.finish({"publication_status": counts, "exit_code": code})
        except Exception as exc:
            logger.warning("[BENCHMARK] Save failed: %s.", type(exc).__name__)
    logger.info("[M2] Finished in %.1fs%s.", time.perf_counter() - started,
                " with errors" if code else "")
    return code


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO, format="%(message)s", force=True)
    raise SystemExit(main())
