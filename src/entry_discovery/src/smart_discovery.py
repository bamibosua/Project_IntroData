"""Global Revision Batching, resumable arXiv metadata + revised dates crawler (Python 3.10+).

Uses existing discovery.py parsing, checkpoint formats, API client and rate limiting.
On 429, imposes a global cooldown: it never hammers the API to 'skip' errors.
Incomplete metadata batches and individual revision versions remain in the queue.
"""
import argparse
import json
import os
import time
import urllib.error
from datetime import datetime, timezone, timedelta
from pathlib import Path
from discovery import (ArxivClient, RateLimitError, ensure_checkpoint_identity,
                        valid_date,
                       load_ids_file, generate_ids, parse_metadata, parse_version_updates,
                       validate_cached_batch, load_revision_cache, write_json_atomic,
                       attach_revised_dates, retry_after_hint)


def utc():
    return datetime.now(timezone.utc).isoformat()


def append_jsonl(path, record):
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open('a', encoding='utf-8') as f:
        f.write(json.dumps(record, ensure_ascii=False) + '\n')


def load_batch(out, i, chunk):
    path = out / 'batches' / f'batch_{i:05d}.json'
    if not path.exists():
        return {}, path
    records = json.loads(path.read_text(encoding='utf-8'))
    # Existing complete checkpoints should remain valid and untouched.
    validate_cached_batch(records, chunk, path)
    return records, path


def create_status(ids, records, cache, attempts, errors):
    result = {}
    for pid in ids:
        p = records.get(pid)
        meta_state = p['status'] if p else 'pending'
        if meta_state == 'found':
            n = p['latest_version']
            missing = [f'{pid}v{v}' for v in range(2, n+1) if f'{pid}v{v}' not in cache]
            rev_state = 'completed' if not missing else 'pending'
        elif meta_state == 'not_found':
            missing = []
            rev_state = 'not_applicable'
        else:
            missing = []
            rev_state = 'pending'
        complete = meta_state == 'not_found' or (meta_state == 'found' and rev_state == 'completed')
        result[pid] = {'status': 'completed' if complete else 'pending',
                       'metadata': meta_state, 'revised_dates': rev_state,
                       'missing_versions': missing,
                       'retry_count': attempts.get(pid, 0),
                       'last_error': errors.get(pid)}
    return result


def main(argv=None, client=None):
    parser = argparse.ArgumentParser(description='Global Revision Batching crawler: metadata first, then pooled revisions')
    parser.add_argument('--ids-file', type=Path)
    parser.add_argument('--start', default='2609.12834')
    parser.add_argument('--end', default='2609.17833')
    parser.add_argument('--limit', type=int)
    parser.add_argument('--output', type=Path, default=Path('data/group/discovery'))
    parser.add_argument('--batch-size', type=int, default=20)
    parser.add_argument('--revision-batch-size', type=int, default=20)
    parser.add_argument('--delay', type=float, default=8)
    parser.add_argument('--pass-number', type=int, choices=[1, 2], default=1)
    parser.add_argument('--cooldown', type=float, default=900, help='429 minimum cooldown seconds (default 900)')
    parser.add_argument('--max-requests', type=int, help='Limit requests for a small test')
    parser.add_argument('--no-wait-cooldown', action='store_true',
                        help='Exit on cooldown/429 rather than waiting automatically')
    parser.add_argument('--max-consecutive-429', type=int, default=2,
                        help='Stop after this many consecutive 429 responses (default 2)')
    args = parser.parse_args(argv)
    if not 1 <= args.batch_size <= 50 or not 1 <= args.revision_batch_size <= 50:
        parser.error('Batch sizes must be 1..50')
    if args.delay < 3 or args.cooldown < 60:
        parser.error('Delay must be >=3s and cooldown >=60s')
    if args.max_consecutive_429 < 1:
        parser.error('--max-consecutive-429 must be positive')
    if args.max_requests is not None and args.max_requests < 1:
        parser.error('--max-requests must be positive')
    ids = load_ids_file(args.ids_file) if args.ids_file else generate_ids(args.start, args.end)
    if args.limit is not None:
        if args.limit < 1:
            parser.error('--limit must be positive')
        ids = ids[:args.limit]
    out = args.output
    ensure_checkpoint_identity(out, ids, args.batch_size)
    cache_path, cache = load_revision_cache(out)
    queue_path = out / 'crawl_queue.json'
    attempts_path = out / 'crawl_attempts.json'
    attempts = json.loads(attempts_path.read_text(encoding='utf-8')) if attempts_path.exists() else {}
    error_path = out / 'crawl_errors.json'
    errors = json.loads(error_path.read_text(encoding='utf-8')) if error_path.exists() else {}
    client = client or ArxivClient(delay=args.delay, retries=2, user_agent=os.environ.get('ARXIV_USER_AGENT'))
    began = time.perf_counter()
    records = {}
    metadata_success = 0
    revision_success = 0
    latest_dates_reused = 0
    revision_tasks_planned = 0
    skipped_batches = []
    api_failures = 0
    stopped_reason = None
    cooldown_path = out / 'rate_limit_until_utc.json'
    deadline = None
    if cooldown_path.exists():
        saved = json.loads(cooldown_path.read_text(encoding='utf-8'))
        deadline = datetime.fromisoformat(saved['retry_not_before'].replace('Z', '+00:00'))
    cooldown_until = time.monotonic() + max(0.0, (deadline - datetime.now(timezone.utc)).total_seconds()) if deadline else 0.0
    calls = 0
    cooldown_wait_seconds = 0.0
    consecutive_429 = 0
    def limited():
        return args.max_requests is not None and calls >= args.max_requests
    def fail(key, exc, *, rate_limited=False):
        nonlocal api_failures, cooldown_until
        api_failures += 1
        errors[key] = f'{type(exc).__name__}: {exc}'
        attempts[key] = attempts.get(key, 0) + 1
        append_jsonl(out / 'crawl_errors.jsonl', {'time_utc': utc(), 'task': key, 'error': errors[key]})
        if rate_limited:
            hint = getattr(exc, 'retry_after', None)
            seconds = hint if isinstance(hint, (int, float)) else args.cooldown
            duration = max(args.cooldown, seconds)
            cooldown_until = max(cooldown_until, time.monotonic() + duration)
            write_json_atomic(cooldown_path, {'retry_not_before': (datetime.now(timezone.utc) + timedelta(seconds=duration)).isoformat()})

    def request(ids_to_fetch, key):
        nonlocal calls, cooldown_wait_seconds, consecutive_429, stopped_reason
        while True:
            if limited():
                stopped_reason = 'request_limit'
                return None
            remaining = cooldown_until - time.monotonic()
            if remaining > 0:
                if args.no_wait_cooldown:
                    stopped_reason = 'cooldown_active'
                    return None
                print(f'Cooldown active: waiting {remaining:.0f}s before requesting {key}. '
                      'Press Ctrl+C to save progress and exit.', flush=True)
                # Print progress every 60 seconds while respecting the saved deadline.
                while True:
                    remaining = cooldown_until - time.monotonic()
                    if remaining <= 0:
                        break
                    start_wait = time.perf_counter()
                    time.sleep(min(60.0, remaining))
                    cooldown_wait_seconds += time.perf_counter() - start_wait
                    remaining = max(0.0, cooldown_until - time.monotonic())
                    if remaining > 0:
                        print(f'[COOLDOWN] Remaining: {remaining:.0f}s | next task: {key}', flush=True)
                print(f'[RESUME] Cooldown finished; requesting {key}', flush=True)
                continue
            if cooldown_path.exists():
                cooldown_path.unlink()
            calls += 1
            try:
                result = client.fetch(ids_to_fetch)
                consecutive_429 = 0
                return result
            except RateLimitError as exc:
                fail(key, exc, rate_limited=True)
                consecutive_429 += 1
                if args.no_wait_cooldown or consecutive_429 >= args.max_consecutive_429:
                    stopped_reason = 'http_429_cooldown'
                    return None
                # Wait the full persisted cooldown, then retry exactly this same task.
                # Do not switch to other IDs while arXiv is rate limiting us.
                print(f'HTTP 429: task {key} kept pending; will retry after cooldown.', flush=True)
            except (OSError, RuntimeError, ValueError) as exc:
                fail(key, exc)
                return None

    try:
        total_batches = (len(ids) + args.batch_size - 1) // args.batch_size
        for i in range(total_batches):
            chunk = ids[i*args.batch_size:(i+1)*args.batch_size]
            batch, path = load_batch(out, i, chunk)
            if not batch:
                partial = out / 'partial_batches' / f'batch_{i:05d}.json'
                previous = json.loads(partial.read_text(encoding='utf-8')) if partial.exists() else {}
                remaining = [pid for pid in chunk if pid not in previous]
                if remaining:
                    if limited():
                        stopped_reason = 'request_limit'
                        break
                    response = request(remaining, f'metadata_batch_{i:05d}')
                    if response is None:
                        records.update(previous)
                        skipped_batches.append(i)
                        if stopped_reason:
                            break
                        continue
                    try:
                        parsed = parse_metadata(response)
                        previous.update({pid: parsed[pid] for pid in remaining if pid in parsed})
                        write_json_atomic(partial, previous)
                        missing = [pid for pid in remaining if pid not in parsed]
                        for pid in missing:
                            errors[pid] = 'Missing from API response, not confirmed nonexistent (pending)'
                        metadata_success += len(parsed)
                    except (ValueError, RuntimeError) as exc:
                        fail(f'metadata_batch_{i:05d}', exc)
                        records.update(previous)
                        skipped_batches.append(i)
                        continue
                batch = previous
                if len(batch) == len(chunk):
                    write_json_atomic(path, batch)
                else:
                    skipped_batches.append(i)
            records.update(batch)
            # PHASE 1: metadata only.  Never fetch versions while walking metadata batches.
            if i % 25 == 0 or i == total_batches - 1:
                print(f'[METADATA] batch {i+1}/{total_batches}; collected={len(records)}', flush=True)
            if stopped_reason:
                break

        # PHASE 2: build one GLOBAL queue of all missing version IDs.
        # Preserve previously collected metadata/revisions and existing checkpoint format.
        if stopped_reason is None:
            # Earlier runs may have partial checkpoints; include valid partial records.
            for i in range(total_batches):
                chunk = ids[i * args.batch_size:(i + 1) * args.batch_size]
                batch, _ = load_batch(out, i, chunk)
                records.update(batch)
                partial = out / 'partial_batches' / f'batch_{i:05d}.json'
                if partial.exists():
                    records.update(json.loads(partial.read_text(encoding='utf-8')))

            # The unversioned Atom entry already provides the latest version's updated date.
            # Only populate missing keys and never overwrite existing verified cache dates.
            for rec in records.values():
                if rec.get('status') != 'found':
                    continue
                n = rec['latest_version']
                latest = rec.get('latest_updated')
                if n >= 2 and latest and valid_date(latest):
                    vid = f"{rec['arxiv_id']}v{n}"
                    if vid not in cache:
                        cache[vid] = latest
                        latest_dates_reused += 1
            if latest_dates_reused:
                write_json_atomic(cache_path, cache)

            # Global queue: group by version layer to avoid sending multiple versions
            # of the SAME paper in one request (API behavior can otherwise vary).
            # Unlike the old approach, grouping is across ALL metadata batches.
            layers = {}
            for pid in ids:
                rec = records.get(pid)
                if not rec or rec.get('status') != 'found':
                    continue
                for version in range(2, rec['latest_version'] + 1):
                    vid = f'{pid}v{version}'
                    if vid not in cache:
                        layers.setdefault(version, []).append(vid)
            revision_tasks_planned = sum(len(v) for v in layers.values())
            print(f'[GLOBAL] missing revisions={revision_tasks_planned}; '
                  f'reused latest dates={latest_dates_reused}; layers={len(layers)}', flush=True)
            for version in sorted(layers):
                vids = layers[version]
                for j in range(0, len(vids), args.revision_batch_size):
                    subset = vids[j:j + args.revision_batch_size]
                    response = request(subset, f'global_revisions_v{version}_{j}')
                    if response is None:
                        break
                    try:
                        updates = parse_version_updates(response)
                        for vid in subset:
                            if vid in updates:
                                if vid not in cache:
                                    cache[vid] = updates[vid]
                                    revision_success += 1
                            else:
                                errors[vid] = 'Missing exact version from API response (pending)'
                        write_json_atomic(cache_path, cache)
                    except (ValueError, RuntimeError) as exc:
                        fail(f'global_revisions_v{version}_{j}', exc)
                        stopped_reason = 'revision_parse_error'
                        break
                if stopped_reason:
                    break
    except KeyboardInterrupt:
        stopped_reason = 'keyboard_interrupt'
    finally:
        # Collect all historical valid cached batches, including those not visited after a test limit.
        for i in range((len(ids) + args.batch_size-1)//args.batch_size):
            chunk = ids[i*args.batch_size:(i+1)*args.batch_size]
            batch, _ = load_batch(out, i, chunk)
            records.update(batch)
            part = out / 'partial_batches' / f'batch_{i:05d}.json'
            if part.exists():
                records.update(json.loads(part.read_text(encoding='utf-8')))
        status = create_status(ids, records, cache, attempts, errors)
        papers = [attach_revised_dates(records[pid], cache) for pid in ids if pid in records and records[pid]['status']=='found']
        summary = {'total_ids': len(ids), 'found': len(papers),
                   'pending_papers': sum(x['status']=='pending' for x in status.values()),
                   'completed_papers': sum(x['status']=='completed' for x in status.values()),
                   'revision_dates_cached': len(cache), 'pass_number': args.pass_number,
                   'revision_strategy': 'global',
                   'http_429_count': client.metrics()['http_429_count'],
                   'stopped_reason': stopped_reason,
                   'generated_at_utc': utc()}
        write_json_atomic(out / 'discovered_papers_with_revised_dates.json', papers)
        write_json_atomic(out / 'crawl_queue.json', status)
        write_json_atomic(attempts_path, attempts)
        write_json_atomic(error_path, errors)
        write_json_atomic(out / 'smart_summary.json', summary)
        wall = time.perf_counter()-began
        benchmark = {'timestamp_utc': utc(), 'pass_number': args.pass_number,
                      'revision_strategy': 'global',
                     'wall_time_seconds': round(wall, 3), 'successful_metadata_records_new': metadata_success,
                     'successful_revision_dates_new': revision_success,
                      'latest_dates_reused': latest_dates_reused,
                      'global_revision_tasks_planned': revision_tasks_planned,
                     'failed_tasks': api_failures, 'pending_papers': summary['pending_papers'],
                     'total_ids': len(ids), 'stopped_reason': stopped_reason,
                     'cooldown_wait_seconds': round(cooldown_wait_seconds, 3), **client.metrics()}
        append_jsonl(out / 'benchmark_sessions.jsonl', benchmark)
        print(json.dumps({'summary': summary, 'benchmark': benchmark}, ensure_ascii=False, indent=2))
    return 0 if summary['pending_papers']==0 else 1

if __name__ == '__main__':
    raise SystemExit(main())
