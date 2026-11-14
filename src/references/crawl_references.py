"""Crawl the selected publications with bounded retries and per-record commits."""

from __future__ import annotations

import argparse
import datetime
import json
import logging
import math
import os
import uuid
import time
from pathlib import Path
from queue import Empty

try:
    if __package__ and __package__.startswith("src."):
        from .. import m1_benchmark as benchmark
    else:
        import m1_benchmark as benchmark
except ImportError:
    benchmark = None

if __package__:
    from . import config
    from .metrics import (
        ResourceMonitor,
        RunMetrics,
        refs_size,
        summarize_output,
        timestamp,
    )
    from .records import (
        InputError,
        ResponseError,
        build_record,
        count_matches,
        is_valid_record,
        load_index,
    )
    from .s2_client import ClientError, FatalError, ReqStats, S2Client
    from .storage import default_meta_dir, load_record, record_path, write_json_atomic
else:
    import config
    from metrics import (
        ResourceMonitor,
        RunMetrics,
        refs_size,
        summarize_output,
        timestamp,
    )
    from records import (
        InputError,
        ResponseError,
        build_record,
        count_matches,
        is_valid_record,
        load_index,
    )
    from s2_client import ClientError, FatalError, ReqStats, S2Client
    from storage import default_meta_dir, load_record, record_path, write_json_atomic

logger = logging.getLogger(__name__)
PROJECT_ROOT = Path(__file__).resolve().parents[2]
DEFAULT_INPUT = str(PROJECT_ROOT / "data" / "input" / "papers_index.json")
DEFAULT_OUT = str(PROJECT_ROOT / "data" / "output" / "papers")
load_papers = load_index  # Keep the diagnostic script on the same index loader.


def _configuration(args):
    batch = args.batch_size or config.BATCH_SIZE
    if type(batch) is not int or not 1 <= batch <= 500:
        raise InputError("batch-size must be between 1 and 500 (0 uses the default)")
    if type(args.limit) is not int or args.limit < 0:
        raise InputError("limit must be >= 0 (0 selects all)")
    values = {
        "cycle_sec": config.CYCLE_SECONDS if args.cycle is None else args.cycle,
        "wait_429_sec": (
            config.WAIT_429_SECONDS if args.wait_429 is None else args.wait_429
        ),
        "timeout_sec": config.REQUEST_TIMEOUT,
        "ram_sample_interval_sec": config.RAM_SAMPLE_INTERVAL,
    }
    for name, value in values.items():
        if isinstance(value, bool) or not isinstance(value, (int, float)):
            raise InputError(f"{name}: invalid value")
        minimum_ok = (
            value > 0
            if name in ("timeout_sec", "ram_sample_interval_sec")
            else value >= 0
        )
        if not math.isfinite(value) or not minimum_ok:
            raise InputError(f"{name}: invalid value")
    for name in ("MAX_OTHER_RETRIES", "MAX_429_RETRIES", "TRUNCATION_CHECK_MIN_REFS"):
        value = getattr(config, name)
        if type(value) is not int or value < (
            1 if name == "TRUNCATION_CHECK_MIN_REFS" else 0
        ):
            raise InputError(f"{name}: invalid value")
    return {
        **values,
        "batch_size": batch,
        "truncation_min_refs": config.TRUNCATION_CHECK_MIN_REFS,
        "max_other_retries": config.MAX_OTHER_RETRIES,
        "max_429_retries": config.MAX_429_RETRIES,
    }


def _read_saved(out_dir, arxiv_id):
    try:
        return load_record(record_path(out_dir, arxiv_id))
    except ValueError:
        return None


def _verified_record(client, pub, entry, *, force_pages=False):
    arxiv_id = pub["arxiv_id"]
    if entry is None:
        return build_record(pub, None, fetched_at=timestamp())
    batch_references = entry["references"]
    if (
        not force_pages
        and count_matches(entry, len(batch_references))
        and len(batch_references) < config.TRUNCATION_CHECK_MIN_REFS
    ):
        return build_record(pub, entry, fetched_at=timestamp())
    logger.info(
        "[REFERENCES] Verifying %s: expected=%s batch=%d.",
        arxiv_id,
        entry.get("referenceCount"),
        len(batch_references),
    )
    full, _ = client.fetch_all_references(arxiv_id)
    if entry.get("referenceCount") is None:
        logger.info(f"[REFERENCES] Looking up reference count: {arxiv_id}.")
        metadata, _ = client.fetch_metadata(arxiv_id)
        entry = {
            **entry,
            "referenceCount": metadata["referenceCount"],
            "paperId": metadata.get("paperId") or entry.get("paperId"),
        }
    if not count_matches(entry, len(full)):
        return build_record(
            pub,
            entry,
            raw_refs=full,
            method="paged_count_mismatch",
            fetched_at=timestamp(),
        )
    logger.info(f"[REFERENCES] Verified {arxiv_id}: {len(full)} references.")
    return build_record(
        pub, entry, raw_refs=full, method="paged_count_match", fetched_at=timestamp()
    )


def _commit_record(pub, record, out_dir, metrics):
    if record["status"] == "count_mismatch":
        # Preserve the existing protection for verified snapshots during refresh.
        previous = _read_saved(out_dir, pub["arxiv_id"])
        if previous is not None:
            candidate = {**previous, "refresh_pending": False}
            if is_valid_record(
                candidate, pub["arxiv_id"], allow_legacy=True
            ) and candidate["status"] in ("ok", "empty_refs"):
                _record_error(
                    pub,
                    ClientError(
                        f"{pub['arxiv_id']}: count={record['s2_reference_count']}, pages={record['n_refs']}; completeness not verified",
                        code="count_mismatch",
                    ),
                    metrics,
                )
                logger.info(
                    "[REFERENCES] %s: mismatch %s/%s; verified data kept.",
                    pub["arxiv_id"],
                    record["n_refs"],
                    record["s2_reference_count"],
                )
                return
    size = write_json_atomic(record_path(out_dir, pub["arxiv_id"]), record)
    metrics.add_record(record, payload_bytes=size)
    if benchmark is not None:
        benchmark.event(pub["arxiv_id"], "references", record["status"])
    if record["status"] == "count_mismatch":
        logger.info(
            "[REFERENCES] %s: count mismatch %s/%s; saved with warning.",
            pub["arxiv_id"],
            record["n_refs"],
            record["s2_reference_count"],
        )


def _process_batch(client, publications, out_dir, metrics):
    try:
        entries, _ = client.post_batch(
            [batch_publication["arxiv_id"] for batch_publication in publications]
        )
    except ClientError as exc:
        if exc.code == "too_large" and len(publications) > 1:
            metrics.splits += 1
            middle = len(publications) // 2
            logger.info(
                f"[REFERENCES] Response too large; splitting {len(publications)} papers into {middle} + {len(publications) - middle}."
            )
            _process_batch(client, publications[:middle], out_dir, metrics)
            _process_batch(client, publications[middle:], out_dir, metrics)
            return
        if exc.code == "too_large":
            publication = publications[0]
            logger.info(
                f"[REFERENCES] Fetching references by page: {publication['arxiv_id']}."
            )
            try:
                metadata, _ = client.fetch_metadata(publication["arxiv_id"])
                record = _verified_record(
                    client,
                    publication,
                    {**metadata, "references": []},
                    force_pages=True,
                )
            except (ClientError, ResponseError) as error:
                logger.info(
                    f"[REFERENCES] {publication['arxiv_id']}: verification failed ({getattr(error, 'code', 'invalid_response').replace('_', ' ')}); remains pending."
                )
                _record_error(publication, error, metrics)
            else:
                _commit_record(publication, record, out_dir, metrics)
            return
        for publication in publications:
            _record_error(publication, exc, metrics)
        return
    for publication, entry in zip(publications, entries):
        try:
            record = _verified_record(client, publication, entry)
        except (ClientError, ResponseError) as exc:
            logger.info(
                f"[REFERENCES] {publication['arxiv_id']}: verification failed ({getattr(exc, 'code', 'invalid_response').replace('_', ' ')}); remains pending."
            )
            _record_error(publication, exc, metrics)
            continue
        _commit_record(publication, record, out_dir, metrics)


def _record_error(pub, error, metrics):
    if isinstance(error, ResponseError):
        error = ClientError(str(error), code="invalid_response")
    metrics.add_error(pub["arxiv_id"], error)
    if benchmark is not None:
        benchmark.event(pub["arxiv_id"], "references", "error")


def _save_report(path, report):
    try:
        write_json_atomic(path, report)
    except OSError as exc:
        report["warnings"].append(f"metrics_persistence_error: {exc}")
        logger.warning(
            "Cannot save the new report; the previous report may be outdated."
        )
        return False
    return True


def _append_log(path, metrics):
    if "log_persistence_error" in metrics.warnings:
        return
    try:
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, "a", encoding="utf-8") as stream:
            stream.write(
                json.dumps(
                    {
                        "at": timestamp(),
                        "batch": metrics.batches,
                        "papers": metrics.papers,
                    }
                )
                + "\n"
            )
    except OSError as exc:
        metrics.warnings.append("log_persistence_error")
        logger.warning("Cannot save the crawl log; crawling will continue.")


def run(args, client=None, *, source=None, stream_status=None):
    run_config = _configuration(args)
    streaming = source is not None
    publications = [] if streaming else load_index(args.papers_file)
    if args.limit and not streaming:
        publications = publications[: args.limit]
    meta_dir = args.meta_dir or default_meta_dir(args.out)
    if args.summarize:
        report = summarize_output(
            args.out, publications, csv_path=os.path.join(meta_dir, "papers_stats.csv")
        )
        report["exit_code"] = 1 if report["papers"]["pending"] else 0
        report_path = os.path.join(meta_dir, "summary_references.json")
        report_saved = _save_report(report_path, report)
        paper_counts = report["papers"]
        print(
            f"Summary: saved={paper_counts['saved']}/{paper_counts['selected']} | verified={paper_counts['completed']} | pending={paper_counts['pending']}"
        )
        print(
            f"Results: ok={paper_counts['ok']} empty={paper_counts['empty_refs']} not_in_s2={paper_counts['not_in_s2']} mismatch={paper_counts['count_mismatch']}"
        )
        if report_saved:
            print(f"Saved: {os.path.abspath(report_path)}")
        if report["csv_saved"]:
            print(f"Saved: {report['csv_saved']}")
        return report

    metrics = RunMetrics(run_config, publications, args.papers_file, args.out)
    disk_before = refs_size(args.out, publications)
    monitor = ResourceMonitor()
    exit_code, reason, owns_client = 0, None, False
    n_batches = 0
    stats_before = client.stats.as_dict() if client is not None else {}

    def batches():
        nonlocal disk_before
        if not streaming:
            for start in range(0, len(publications), run_config["batch_size"]):
                yield publications[start : start + run_config["batch_size"]]
            return
        pending, seen = [], set()
        for job in source:
            arxiv_id = job["arxiv_id"]
            if arxiv_id in seen:
                continue
            seen.add(arxiv_id)
            publication = {
                "arxiv_id": arxiv_id,
                "versions": [f"{arxiv_id}{version}" for version in job["versions"]],
            }
            before = refs_size(args.out, [publication])
            disk_before = (
                disk_before + before
                if disk_before is not None and before is not None
                else None
            )
            publications.append(publication)
            metrics.papers["selected"] = len(publications)
            pending.append(publication)
            stream_status["buffered"] = len(pending)
            if len(pending) == run_config["batch_size"]:
                stream_status["buffered"] = 0
                yield pending
                pending = []
        if pending and stream_status["exit_code"] == 0:
            yield pending

    try:
        with monitor:
            n_batches = (
                len(publications) + run_config["batch_size"] - 1
            ) // run_config["batch_size"]
            if streaming:
                logger.info("[REFERENCES] Batch size: %d.", run_config["batch_size"])
            else:
                print(f"Input: {len(publications):,} papers")
                print(
                    f"{n_batches} batch{'es' if n_batches != 1 else ''} x up to {run_config['batch_size']} papers | {run_config['cycle_sec']:g}s/request | 429 wait: {run_config['wait_429_sec']:g}s"
                )
            batches_started = client.clock() if client is not None else None
            for batch in batches():
                if client is None:
                    config.load_env()
                    client = S2Client(
                        api_key=config.get_api_key(),
                        cycle=run_config["cycle_sec"],
                        wait_429=run_config["wait_429_sec"],
                    )
                    print(
                        "API key loaded."
                        if client.headers.get("x-api-key")
                        else "No API key found; using unauthenticated requests."
                    )
                    owns_client = True
                if batches_started is None:
                    batches_started = client.clock()
                metrics.batches += 1
                if streaming:
                    logger.info(
                        "[REFERENCES] Batch %d: fetching %d papers.",
                        metrics.batches,
                        len(batch),
                    )
                metrics.papers["attempted"] += len(batch)
                batch_started = client.clock()
                batch_before = dict(metrics.papers)
                _process_batch(client, batch, args.out, metrics)
                _append_log(os.path.join(meta_dir, "crawl_log.jsonl"), metrics)
                elapsed = client.clock() - batch_started
                eta_minutes = (
                    (client.clock() - batches_started)
                    / metrics.batches
                    * (n_batches - metrics.batches)
                    / 60
                    if not streaming
                    else 0
                )
                counts = {
                    key: metrics.papers[key] - batch_before[key]
                    for key in (
                        "ok",
                        "empty_refs",
                        "not_in_s2",
                        "count_mismatch",
                        "errors",
                    )
                }
                label = (
                    f"[REFERENCES] Batch {metrics.batches}"
                    if streaming
                    else f"[{metrics.batches}/{n_batches}]"
                )
                timing = (
                    f"{elapsed:.1f}s"
                    if streaming
                    else f"{elapsed:.1f}s | ETA ~{eta_minutes:.1f}m"
                )
                emit = logger.info if streaming else print
                emit(
                    f"{label} ok={counts['ok']} empty={counts['empty_refs']} "
                    f"not_in_s2={counts['not_in_s2']} mismatch={counts['count_mismatch']} "
                    f"error={counts['errors']} | {timing}"
                )
                if counts["errors"]:
                    print(
                        f"  {counts['errors']} paper{'s' if counts['errors'] != 1 else ''} incomplete."
                    )
            if metrics.papers["errors"]:
                exit_code, reason = 1, "publications_incomplete"
            if streaming and stream_status["exit_code"] != 0:
                exit_code, reason = stream_status["exit_code"], stream_status["reason"]
    except KeyboardInterrupt:
        exit_code, reason = 130, "interrupted"
        print(
            f"Interrupted during batch {metrics.batches}/{n_batches}. Saved results are preserved."
            if metrics.batches
            else "Interrupted. Saved results are preserved."
        )
    except InterruptedError:
        exit_code, reason = 130, "interrupted"
        logger.info("[REFERENCES] Stopped; saved results kept.")
    except (FatalError, OSError) as exc:
        exit_code, reason = 1, str(exc)
        if isinstance(exc, FatalError):
            logger.error("[REFERENCES] Stopped: HTTP %s. Check API access.", exc.status)
        else:
            logger.error("[REFERENCES] Storage error: %s.", type(exc).__name__)
    except Exception:
        exit_code, reason = 1, "internal_error"
        logger.exception("Unexpected error; saved results are preserved.")
    finally:
        if owns_client:
            try:
                client.close()
            except KeyboardInterrupt:
                exit_code, reason = 130, "interrupted"
            except Exception:
                metrics.warnings.append("client_close_error")

    # Keep finalization bounded, including interruption during telemetry persistence.
    requests = (
        ReqStats().as_dict()
        if client is None
        else {
            k: value - stats_before.get(k, 0)
            for k, value in client.stats.as_dict().items()
        }
    )
    report_saved = False
    try:
        report = metrics.finalize(
            monitor,
            disk_before,
            refs_size(args.out, publications),
            requests,
            exit_code,
            reason,
        )
        run_timestamp = datetime.datetime.now(datetime.timezone.utc).strftime(
            "%Y-%m-%d_%H-%M-%S_%f_utc"
        )
        report_path = os.path.join(
            meta_dir, f"references_run_{run_timestamp}_{uuid.uuid4().hex[:8]}.json"
        )
        report_saved = _save_report(report_path, report)
    except KeyboardInterrupt:
        report = metrics.finalize(
            monitor, disk_before, None, requests, 130, "interrupted_during_finalization"
        )
        report["warnings"].append("final_report_not_saved")
    paper_counts = report["papers"]
    if streaming:
        logger.info(
            "[REFERENCES] %s: saved=%d verified=%d pending=%d.",
            "Done" if report["exit_code"] == 0 else "Stopped",
            paper_counts["saved"],
            paper_counts["completed"],
            paper_counts["pending"],
        )
        logger.info(
            "[REFERENCES] Results: mismatch=%d error=%d.",
            paper_counts["count_mismatch"],
            paper_counts["errors"],
        )
        for warning in dict.fromkeys(
            warning_entry.split(":", 1)[0] for warning_entry in report["warnings"]
        ):
            logger.warning("[REFERENCES] %s.", warning.replace("_", " "))
        return report
    memory, disk = report["memory"], report["disk"]
    peak = (
        f"{memory['sampled_peak_rss_mb']:g} MB"
        if memory["sampled_peak_rss_mb"] is not None
        else "N/A"
    )
    mean = (
        f"{memory['sample_mean_rss_mb']:g} MB"
        if memory["sample_mean_rss_mb"] is not None
        else "N/A"
    )
    sizes = {
        key: f"{disk[key] / 1e6:.1f} MB" if disk[key] is not None else "N/A"
        for key in (
            "selected_output_bytes_after",
            "net_reference_output_change_bytes",
            "committed_reference_payload_bytes",
        )
    }
    print("\n=== Run summary ===")
    print(
        f"Papers: {paper_counts['attempted']:,} attempted | ok={paper_counts['ok']:,} empty={paper_counts['empty_refs']:,} "
        f"not_in_s2={paper_counts['not_in_s2']:,} mismatch={paper_counts['count_mismatch']:,} error={paper_counts['errors']:,}"
    )
    print(
        f"Total: {paper_counts['saved']:,}/{paper_counts['selected']:,} saved | verified={paper_counts['completed']:,} "
        f"mismatch={paper_counts['count_mismatch']:,} pending={paper_counts['pending']:,}"
    )
    print(
        f"Time: {report['time']['total_wall_sec']:g}s wall | 429 retries: {requests['retries_429']}"
    )
    print(f"RAM: {peak} sampled peak | {mean} sample mean")
    print(
        f"Disk: {sizes['selected_output_bytes_after']} final | {sizes['net_reference_output_change_bytes']} net change | {sizes['committed_reference_payload_bytes']} committed payload"
    )
    print(f"Exit code: {report['exit_code']}")
    if report_saved:
        print(f"Saved: {os.path.abspath(report_path)}")
    for warning in dict.fromkeys(
        warning_entry.split(":", 1)[0] for warning_entry in report["warnings"]
    ):
        print(f"Warning: {warning.replace('_', ' ')}.")
    return report


def run_stream(args, queue, entry_done, entry_status, *, client=None, stop_event=None):
    """Receive validated jobs, retaining a partial batch until normal entry completion."""
    if args.limit or args.summarize:
        raise InputError("Streaming does not use --limit or --summarize.")
    state = {"exit_code": 0, "reason": None, "buffered": 0}

    def source():
        next_notice = 0.0
        while True:
            if stop_event is not None and stop_event.is_set():
                state.update(exit_code=130, reason="interrupted")
                return
            if entry_done.is_set() and entry_status.get("exit_code", 1) != 0:
                state.update(exit_code=1, reason="entry_incomplete")
                return
            try:
                job = queue.get(timeout=1)
                next_notice = 0.0
                yield job
            except Empty:
                if (
                    entry_done.is_set()
                    and entry_status.get("exit_code", 1) == 0
                    and not (stop_event is not None and stop_event.is_set())
                ):
                    return
                if time.monotonic() >= next_notice:
                    logger.info(
                        "[REFERENCES] Waiting for entry: %d/%d buffered.",
                        state["buffered"],
                        args.batch_size or config.BATCH_SIZE,
                    )
                    next_notice = time.monotonic() + 60

    return run(args, client=client, source=source(), stream_status=state)


def parse_args(argv=None):
    parser = argparse.ArgumentParser(
        description="Crawl references from data/input/papers_index.json, one job per publication."
    )
    parser.set_defaults(papers_file=DEFAULT_INPUT)
    parser.add_argument("--out", default=DEFAULT_OUT)
    parser.add_argument(
        "--meta-dir",
        default=None,
        help="default: <out_parent>/<out_name>_references_meta",
    )
    parser.add_argument(
        "--limit", type=int, default=0, help="first N publications; 0=all"
    )
    parser.add_argument(
        "--batch-size", type=int, default=0, help="0=use configured batch size"
    )
    parser.add_argument("--cycle", type=float, default=None)
    parser.add_argument("--wait-429", type=float, default=None)
    parser.add_argument(
        "--summarize",
        action="store_true",
        help="summarize the input scope without API requests",
    )
    return parser.parse_args(argv)


def main(argv=None, *, client=None) -> int:
    try:
        return run(parse_args(argv), client=client)["exit_code"]
    except InputError as exc:
        logger.error("Input error: check the input path, index format and CLI options.")
        return 2
    except KeyboardInterrupt:
        return 130
    except Exception:
        logger.exception("Cannot finish the references run.")
        return 1


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO, format="%(message)s")
    raise SystemExit(main())
