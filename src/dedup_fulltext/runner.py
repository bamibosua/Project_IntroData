"""Run the deduplication over a corpus, stream the output tables and write a statistics summary.

Crash safety
- The corpus is processed in batches ("shards") of `batch_size` publications. Every shard is written to
  `<output>/_shards/` through temporary files and is only valid once its `*.stats.json` exists (written last).
- Running the same command again resumes after the last complete shard; an interrupted shard is recomputed.
- When all shards are done they are concatenated (atomically) into `elements.jsonl`, `hierarchy.jsonl` and
  `coverage.jsonl`, the summary is written, and the shards are deleted (unless `keep_shards`).
- A run with `limit` writes into `<output>/limit_<N>/`, so a test run can never overwrite a full run.
"""

from __future__ import annotations

from collections import Counter
from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path
import shutil
import sys
import time
from typing import Any

from .engine import ALL_KINDS, LEAF_KINDS, deduplicate_publication
from .loader import open_publications

PROJECT_ROOT = Path(os.environ.get("DEDUP_PROJECT_ROOT") or Path(__file__).resolve().parents[2])
DEFAULT_OUTPUT_DIR = PROJECT_ROOT / "data" / "output" / "dedup_fulltext"
DEFAULT_BATCH_SIZE = 500
SHARD_DIRNAME = "_shards"
TABLES = ("elements", "hierarchy", "coverage")
_INPUT_CANDIDATES = (
    PROJECT_ROOT / "data" / "output" / "cleaned_structure",
    PROJECT_ROOT / "data" / "input" / "cleaned_structure",
    PROJECT_ROOT / "data" / "output" / "cleaned_structure.zip",
    PROJECT_ROOT / "data" / "input" / "cleaned_structure.zip",
)


def default_input() -> Path:
    for candidate in _INPUT_CANDIDATES:
        if candidate.is_file() or (candidate.is_dir() and any(candidate.iterdir())):
            return candidate
    return _INPUT_CANDIDATES[0]


def peak_memory_mb() -> float:
    """Peak resident memory of this process in MB (real RSS, not just the Python heap)."""
    try:
        import resource  # POSIX (Linux/Colab, macOS)
        peak = resource.getrusage(resource.RUSAGE_SELF).ru_maxrss
        return round(peak / (1024 * 1024 if sys.platform == "darwin" else 1024), 2)
    except ImportError:
        pass
    try:
        import psutil
        info = psutil.Process().memory_info()
        return round(getattr(info, "peak_wset", info.rss) / (1024 * 1024), 2)
    except Exception:
        return 0.0


def _format_bytes(value: float) -> str:
    for unit in ("B", "KB", "MB", "GB", "TB"):
        if abs(value) < 1024.0:
            return f"{value:.2f} {unit}"
        value /= 1024.0
    return f"{value:.2f} PB"


def _pct(before: int, after: int) -> float:
    return round((before - after) / before * 100.0, 2) if before else 0.0


def _write_json_atomic(path: Path, data: dict) -> None:
    tmp = path.with_suffix(path.suffix + ".tmp")
    with tmp.open("w", encoding="utf-8") as handle:
        handle.write(json.dumps(data, indent=2, ensure_ascii=False) + "\n")
        handle.flush()
        os.fsync(handle.fileno())
    os.replace(tmp, path)


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for block in iter(lambda: handle.read(1 << 20), b""):
            digest.update(block)
    return digest.hexdigest()


class _Totals:
    """Counters for one population of publications (all of them, or only those with >= 2 parsed versions)."""

    def __init__(self) -> None:
        self.publications = 0
        self.before: Counter[str] = Counter()
        self.after: Counter[str] = Counter()
        self.edges = 0

    def add(self, stats: dict[str, Any]) -> None:
        self.publications += 1
        self.before.update(stats["before_by_kind"])
        self.after.update(stats["after_by_kind"])
        self.edges += stats["edges"]

    def to_dict(self) -> dict[str, Any]:
        return {"publications": self.publications, "before": dict(self.before), "after": dict(self.after),
                "edges": self.edges}

    def merge(self, data: dict[str, Any]) -> None:
        self.publications += data["publications"]
        self.before.update(data["before"])
        self.after.update(data["after"])
        self.edges += data["edges"]

    def report(self) -> dict[str, Any]:
        nodes_b = sum(v for k, v in self.before.items() if k != "document")
        nodes_a = sum(v for k, v in self.after.items() if k != "document")
        leaves_b = sum(self.before[k] for k in LEAF_KINDS)
        leaves_a = sum(self.after[k] for k in LEAF_KINDS)
        kinds = [k for k in ALL_KINDS if k != "document"]
        kinds += sorted((set(self.before) | set(self.after)) - set(ALL_KINDS))  # unexpected kinds stay visible
        return {
            "publications": self.publications,
            "nodes_before": nodes_b,
            "nodes_after": nodes_a,
            "nodes_removed": nodes_b - nodes_a,
            "node_reduction_pct": _pct(nodes_b, nodes_a),
            "leaves_before": leaves_b,
            "leaves_after": leaves_a,
            "leaves_removed": leaves_b - leaves_a,
            "leaf_reduction_pct": _pct(leaves_b, leaves_a),
            "hierarchy_edges": self.edges,
            "by_kind": {
                k: {"before": self.before[k], "after": self.after[k], "reduction_pct": _pct(self.before[k], self.after[k])}
                for k in kinds if self.before[k] or self.after[k]
            },
        }


class _ShardWriter:
    """Streams the rows of one batch of publications into temporary files, committed atomically at the end."""

    def __init__(self, shard_dir: Path, index: int) -> None:
        self.dir, self.index = shard_dir, index
        self.base = f"shard_{index:05d}"
        self.files = {t: (shard_dir / f"{self.base}.{t}.jsonl.tmp").open("w", encoding="utf-8") for t in TABLES}
        self.keys: list[str] = []
        self.everyone, self.multi = _Totals(), _Totals()
        self.versions = self.ok_versions = self.empty_versions = 0
        self.orphans = self.leaf_mismatch = self.duplicates = 0
        self.unknown: Counter[str] = Counter()

    def add(self, result: dict[str, Any]) -> None:
        dump = json.dumps
        self.files["elements"].writelines(dump(row, ensure_ascii=False) + "\n" for row in result["elements"])
        self.files["hierarchy"].writelines(dump(row, ensure_ascii=False) + "\n" for row in result["hierarchy"])
        coverage = {k: result[k] for k in ("publication_key", "arxiv_id", "version_count", "ok_versions",
                                           "empty_versions", "versions")}
        self.files["coverage"].write(dump(coverage, ensure_ascii=False) + "\n")

        stats = result["stats"]
        self.keys.append(result["publication_key"])
        self.versions += result["version_count"]
        self.ok_versions += result["ok_versions"]
        self.empty_versions += result["empty_versions"]
        self.orphans += stats["orphan_edges"]
        self.leaf_mismatch += stats["leaf_mismatch_versions"]
        self.duplicates += stats["duplicate_versions"]
        self.unknown.update(stats["unknown_kinds"])
        self.everyone.add(stats)
        if result["ok_versions"] >= 2:  # only publications with something to deduplicate across versions
            self.multi.add(stats)

    def abort(self) -> None:
        for handle in self.files.values():
            handle.close()

    def commit(self, elapsed: float) -> None:
        for handle in self.files.values():
            handle.flush()
            os.fsync(handle.fileno())
            handle.close()
        for table in TABLES:
            final = self.dir / f"{self.base}.{table}.jsonl"
            os.replace(final.with_name(final.name + ".tmp"), final)
        _write_json_atomic(self.dir / f"{self.base}.stats.json", {
            "index": self.index, "keys": self.keys, "elapsed_sec": round(elapsed, 4),
            "versions": self.versions, "ok_versions": self.ok_versions, "empty_versions": self.empty_versions,
            "orphan_edges": self.orphans, "leaf_mismatch_versions": self.leaf_mismatch,
            "duplicate_versions": self.duplicates, "unknown_kinds": dict(self.unknown),
            "everyone": self.everyone.to_dict(), "multi": self.multi.to_dict(),
        })


def _load_progress(shard_dir: Path, meta: dict[str, Any], resume: bool) -> list[dict[str, Any]]:
    """Return the stats of the complete shards left by an earlier run, after discarding unusable leftovers."""
    meta_path = shard_dir / "run_meta.json"
    if shard_dir.exists():
        previous = json.loads(meta_path.read_text(encoding="utf-8")) if meta_path.exists() else None
        if resume and previous is not None and previous != meta:
            raise RuntimeError(
                f"Checkpoint in {shard_dir} was made with other settings ({previous}) than this run ({meta}). "
                "Re-run with --restart to discard it.")
        if not resume or previous is None:
            shutil.rmtree(shard_dir)
    shard_dir.mkdir(parents=True, exist_ok=True)
    if not meta_path.exists():
        _write_json_atomic(meta_path, meta)

    complete: list[dict[str, Any]] = []
    keep: set[str] = {meta_path.name}
    for stats_path in sorted(shard_dir.glob("shard_*.stats.json")):
        base = stats_path.name[: -len(".stats.json")]
        files = [shard_dir / f"{base}.{t}.jsonl" for t in TABLES]
        if all(f.exists() for f in files):
            complete.append(json.loads(stats_path.read_text(encoding="utf-8")))
            keep.update([stats_path.name, *(f.name for f in files)])
    for leftover in shard_dir.iterdir():  # temporary files and shards without a valid stats file
        if leftover.name not in keep:
            leftover.unlink()
    return complete


def _concat(shard_dir: Path, bases: list[str], table: str, final: Path) -> None:
    tmp = final.with_name(final.name + ".tmp")
    with tmp.open("wb") as out:
        for base in bases:
            with (shard_dir / f"{base}.{table}.jsonl").open("rb") as part:
                shutil.copyfileobj(part, out, 1 << 20)
        out.flush()
        os.fsync(out.fileno())
    os.replace(tmp, final)


def run_deduplication(
    input_path: Path | None = None,
    output_dir: Path = DEFAULT_OUTPUT_DIR,
    *,
    limit: int | None = None,
    cite_map_path: Path | None = None,
    batch_size: int = DEFAULT_BATCH_SIZE,
    resume: bool = True,
    keep_shards: bool = False,
    compact_containers: bool = True,
) -> dict[str, Any]:
    """Deduplicate every publication; write elements.jsonl, hierarchy.jsonl, coverage.jsonl, dedup_summary.json."""
    if batch_size < 1:
        raise ValueError("batch_size must be >= 1")
    t0 = time.perf_counter()  # end-to-end: the indexing of the input is part of the measured time
    started = datetime.now(timezone.utc)
    input_path = input_path or default_input()
    if limit is not None:
        output_dir = output_dir / f"limit_{limit}"  # a test run must never overwrite the full output
    output_dir.mkdir(parents=True, exist_ok=True)
    shard_dir = output_dir / SHARD_DIRNAME

    cite_maps: dict[str, dict[str, str]] = {}
    if cite_map_path is not None:
        cite_maps = json.loads(cite_map_path.read_text(encoding="utf-8"))
    meta = {"input": str(input_path.resolve()), "limit": limit,
            "cite_map_sha256": _sha256(cite_map_path) if cite_map_path is not None else None,
            "compact_containers": compact_containers}

    finished = _load_progress(shard_dir, meta, resume)
    done_keys = {key for stats in finished for key in stats["keys"]}
    next_index = max((s["index"] for s in finished), default=0) + 1

    total, publications = open_publications(input_path, limit, skip=done_keys)
    print(f"[DEDUP] {total:,} publications from {input_path}", flush=True)
    if done_keys:
        print(f"[DEDUP] resuming: {len(done_keys):,} publications already done in {len(finished)} shard(s)", flush=True)

    position = len(done_keys)
    writer: _ShardWriter | None = None
    batch_start = time.perf_counter()
    try:
        for key, docs in publications:
            if writer is None:
                writer = _ShardWriter(shard_dir, next_index)
            cite_map = cite_maps.get(key.replace("-", ".", 1)) or cite_maps.get(key)
            result = deduplicate_publication(key, docs, cite_key_map=cite_map, compact_containers=compact_containers)
            writer.add(result)
            position += 1
            stats = result["stats"]
            if position <= 3 or position % 2000 == 0 or position == total:
                print(f"[DEDUP] {position}/{total} {key} ({result['version_count']}v): "
                      f"{stats['nodes_before']} -> {stats['nodes_after']} elements", flush=True)
            if len(writer.keys) >= batch_size:
                writer.commit(time.perf_counter() - batch_start)
                writer, next_index, batch_start = None, next_index + 1, time.perf_counter()
        if writer is not None:
            writer.commit(time.perf_counter() - batch_start)
            writer = None
    except BaseException:
        if writer is not None:
            writer.abort()
        print(f"[DEDUP] interrupted at {position}/{total}: run the same command again to resume.", flush=True)
        raise

    # ---- assemble the final tables from every complete shard --------------------------------------------
    all_stats = [json.loads(p.read_text(encoding="utf-8")) for p in sorted(shard_dir.glob("shard_*.stats.json"))]
    bases = [f"shard_{s['index']:05d}" for s in all_stats]
    for table in TABLES:
        _concat(shard_dir, bases, table, output_dir / f"{table}.jsonl")

    everyone, multi = _Totals(), _Totals()
    unknown: Counter[str] = Counter()
    for stats in all_stats:
        everyone.merge(stats["everyone"])
        multi.merge(stats["multi"])
        unknown.update(stats["unknown_kinds"])
    processed = everyone.publications
    if processed != total:
        print(f"[DEDUP] WARNING: {processed} publications in the shards but {total} in the input", flush=True)
    cumulative = round(sum(s["elapsed_sec"] for s in all_stats), 4)

    elements_path, hierarchy_path = output_dir / "elements.jsonl", output_dir / "hierarchy.jsonl"
    coverage_path = output_dir / "coverage.jsonl"
    elements_bytes, hierarchy_bytes = elements_path.stat().st_size, hierarchy_path.stat().st_size
    if not keep_shards:
        shutil.rmtree(shard_dir)
    wall = round(time.perf_counter() - t0, 4)
    summary = {
        "stage": "fulltext_deduplication",
        "started_at_utc": started.isoformat(),
        "completed_at_utc": datetime.now(timezone.utc).isoformat(),
        "input": str(input_path),
        "limit": limit,
        "total_wall_time_sec": wall,  # this invocation, end to end (equals the whole run if it was not resumed)
        "processing_time_sec_cumulative": cumulative,  # dedup time summed over every shard, resumed or not
        "avg_time_per_publication_ms": round(cumulative * 1000.0 / processed, 3) if processed else 0.0,
        "peak_rss_mb": peak_memory_mb(),  # this invocation only
        "resumed_publications": len(done_keys),
        "coverage": {
            "publications": total,
            "multi_version_publications": multi.publications,  # >= 2 parsed versions
            "versions": sum(s["versions"] for s in all_stats),
            "versions_with_elements": sum(s["ok_versions"] for s in all_stats),
            "empty_versions": sum(s["empty_versions"] for s in all_stats),
            "duplicate_versions_skipped": sum(s["duplicate_versions"] for s in all_stats),
            "orphan_edges_skipped": sum(s["orphan_edges"] for s in all_stats),
            "leaf_count_mismatch_versions": sum(s["leaf_mismatch_versions"] for s in all_stats),
            "unknown_kinds": dict(unknown),
        },
        "all_publications": everyone.report(),
        "multi_version_publications": multi.report(),
        "storage": {
            "elements_jsonl_bytes": elements_bytes,
            "hierarchy_jsonl_bytes": hierarchy_bytes,
            "coverage_jsonl_bytes": coverage_path.stat().st_size,
            "total_output_bytes": elements_bytes + hierarchy_bytes,
            "total_output_human": _format_bytes(elements_bytes + hierarchy_bytes),
        },
    }
    summary_path = output_dir / "dedup_summary.json"
    _write_json_atomic(summary_path, summary)
    _print_report(summary, summary_path)
    return summary


def _print_report(summary: dict[str, Any], path: Path) -> None:
    cov, alls, mult = summary["coverage"], summary["all_publications"], summary["multi_version_publications"]
    print("\n" + "=" * 62)
    print(f" Runtime      : {summary['total_wall_time_sec']:.2f}s wall ({summary['avg_time_per_publication_ms']:.2f} ms/pub), "
          f"peak RSS {summary['peak_rss_mb']} MB")
    print(f" Publications : {cov['publications']:,} ({cov['multi_version_publications']:,} with >= 2 parsed versions)")
    print(f" Versions     : {cov['versions']:,} ({cov['versions_with_elements']:,} parsed, {cov['empty_versions']:,} empty)")
    print(f" Elements     : {alls['nodes_before']:,} -> {alls['nodes_after']:,} (-{alls['node_reduction_pct']:.2f}%)")
    print(f" Elements >=2v: {mult['nodes_before']:,} -> {mult['nodes_after']:,} (-{mult['node_reduction_pct']:.2f}%)")
    if cov["orphan_edges_skipped"] or cov["leaf_count_mismatch_versions"] or cov["unknown_kinds"]:
        print(f" CHECK        : orphan edges {cov['orphan_edges_skipped']}, leaf-count mismatches "
              f"{cov['leaf_count_mismatch_versions']}, unknown kinds {cov['unknown_kinds']}")
    print(f" Output       : {summary['storage']['total_output_human']}  |  summary: {path}")
    print("=" * 62)
