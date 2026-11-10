import argparse
import hashlib
import json
import os
import re
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
import xml.etree.ElementTree as ET
from datetime import datetime, timezone
from email.utils import parsedate_to_datetime
from pathlib import Path

ATOM = '{http://www.w3.org/2005/Atom}'
ARXIV = '{http://arxiv.org/schemas/atom}'
ID_PATTERN = re.compile(r'^(\d{4})\.(\d{4,5})$')
VERSIONED = re.compile(r'(\d{4}\.\d{4,5})v(\d+)$')
API = 'https://export.arxiv.org/api/query'


def valid_date(value):
    if not isinstance(value, str) or not value:
        return False
    try:
        return datetime.fromisoformat(value.replace('Z', '+00:00')).tzinfo is not None
    except ValueError:
        return False


def generate_ids(start: str, end: str) -> list[str]:
    a = ID_PATTERN.fullmatch(start)
    b = ID_PATTERN.fullmatch(end)
    
    if not a or not b:
        raise ValueError('IDs must look like 2609.12834')
    if a.group(1) != b.group(1):
        raise ValueError('Split ranges at month boundaries in team_ranges.json')
    if not 1 <= int(a.group(1)[-2:]) <= 12:
        raise ValueError('Invalid month in arXiv ID')
    lo, hi = int(a.group(2)), int(b.group(2))
    if lo > hi:
        raise ValueError('Start ID must not exceed end ID')
    width = max(len(a.group(2)), len(b.group(2)))
    return [f'{a.group(1)}.{n:0{width}d}' for n in range(lo, hi + 1)]


def load_ids_file(path: Path) -> list[str]:
    ids = [x.strip() for x in path.read_text(encoding='utf-8').splitlines()
           if x.strip() and not x.lstrip().startswith('#')]
    if not ids:
        raise ValueError('IDs file is empty')
    if any(not ID_PATTERN.fullmatch(x) for x in ids):
        raise ValueError('Invalid arXiv ID in worklist')
    if len(ids) != len(set(ids)):
        raise ValueError('IDs file contains duplicates; run prepare_group.py first')
    return ids


def write_json_atomic(path: Path, data) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temp = path.with_suffix(path.suffix + '.tmp')
    temp.write_text(json.dumps(data, indent=2, ensure_ascii=False), encoding='utf-8')
    temp.replace(path)


def ensure_checkpoint_identity(out: Path, ids: list[str], batch_size: int) -> None:
    """Same manifest format as the original group crawler (important for reuse)."""
    out.mkdir(parents=True, exist_ok=True)
    path = out / 'checkpoint_manifest.json'
    digest = hashlib.sha256(('\n'.join(ids) + '\n').encode('utf-8')).hexdigest()
    expected = {'ids_sha256': digest, 'total_ids': len(ids), 'batch_size': batch_size}
    if path.exists():
        existing = json.loads(path.read_text(encoding='utf-8'))
        if existing != expected:
            raise ValueError('Existing output uses different IDs/order/batch-size. '
                             'Use the SAME --ids-file and --batch-size, or a NEW --output folder.')
    elif any((out / 'batches').glob('batch_*.json')):
        raise ValueError('Batches exist without a valid manifest. Back up and inspect first.')
    else:
        write_json_atomic(path, expected)


def clean_text(value):
    return ' '.join((value or '').split())


def parse_metadata(xml_bytes: bytes) -> dict[str, dict]:
    root = ET.fromstring(xml_bytes)
    results = {}
    errors = []
    for entry in root.findall(f'{ATOM}entry'):
        url = clean_text(entry.findtext(f'{ATOM}id'))
        match = VERSIONED.search(url)
        if not match:
            errors.append(clean_text(entry.findtext(f'{ATOM}title')))
            continue
        paper_id, n = match.group(1), int(match.group(2))
        results[paper_id] = {
            'arxiv_id': paper_id,
            'title': clean_text(entry.findtext(f'{ATOM}title')),
            'authors': [name for name in (
                clean_text(x.findtext(f'{ATOM}name')) for x in entry.findall(f'{ATOM}author')) if name],
            'submission_date': clean_text(entry.findtext(f'{ATOM}published')),
            'latest_updated': clean_text(entry.findtext(f'{ATOM}updated')),
            'revised_dates': None,
            'venue': clean_text(entry.findtext(f'{ARXIV}journal_ref')) or None,
            'latest_version': n,
            'versions': [f'{paper_id}v{k}' for k in range(1, n + 1)],
            'abstract': clean_text(entry.findtext(f'{ATOM}summary')),
            'status': 'found',
        }
    if errors and not results:
        raise ValueError(f'arXiv API error entry: {errors[:1]}')
    return results


def parse_version_updates(xml_bytes: bytes) -> dict[str, str]:
    """Read ENTRY updated (never feed updated) and retain exact version ID."""
    root = ET.fromstring(xml_bytes)
    results = {}
    for entry in root.findall(f'{ATOM}entry'):
        match = VERSIONED.search(clean_text(entry.findtext(f'{ATOM}id')))
        if not match:
            continue
        version_id = f'{match.group(1)}v{int(match.group(2))}'
        updated = clean_text(entry.findtext(f'{ATOM}updated'))
        if not valid_date(updated):
            continue
        if version_id in results and results[version_id] != updated:
            raise ValueError(f'Conflicting dates for {version_id}')
        results[version_id] = updated
    return results


class PermanentRequestError(RuntimeError):
    """An HTTP client error that retrying the same request cannot repair."""


class RateLimitError(RuntimeError):
    def __init__(self, retry_after=None):
        self.retry_after = retry_after
        super().__init__(f'HTTP 429 from arXiv; Retry-After: {retry_after!r}')


def retry_after_hint(value):
    if not value:
        return None
    value = value.strip()
    if value.isdecimal():
        return int(value)
    try:
        d = parsedate_to_datetime(value)
        if d.tzinfo is None:
            d = d.replace(tzinfo=timezone.utc)
        return max(0, int((d - datetime.now(timezone.utc)).total_seconds()))
    except (ValueError, TypeError, OverflowError):
        return value


class ArxivClient:
    """One HTTP connection at a time, including revision queries, with metrics."""

    def __init__(self, delay=8.0, timeout=40, retries=4, user_agent=None):
        if delay < 3.0:
            raise ValueError('arXiv delay must be >= 3 seconds')
        self.delay = delay
        self.timeout = timeout
        self.retries = retries
        self.user_agent = user_agent or 'HCMUS-IDS-M1/1.0 (contact: configure ARXIV_USER_AGENT)'
        self.last_start = None
        self.requests = 0
        self.retries_done = 0
        self.http_429 = 0
        self.pacing_wait_seconds = 0.0
        self.retry_wait_seconds = 0.0
        self.network_seconds = 0.0

    def _sleep(self, seconds, kind):
        if seconds <= 0:
            return
        before = time.perf_counter()
        time.sleep(seconds)
        elapsed = time.perf_counter() - before
        if kind == 'pacing':
            self.pacing_wait_seconds += elapsed
        else:
            self.retry_wait_seconds += elapsed

    def fetch(self, ids: list[str]) -> bytes:
        if not ids:
            raise ValueError('Cannot request an empty batch')
    
        url = API + '?' + urllib.parse.urlencode({
            'id_list': ','.join(ids),
            'max_results': len(ids)
        })
    
        for attempt in range(self.retries + 1):
        
            # Maintain minimum delay between requests
            if self.last_start is not None:
                self._sleep(
                    max(0.0, self.delay - (time.monotonic() - self.last_start)),
                    'pacing'
                )
    
            self.last_start = time.monotonic()
            self.requests += 1
    
            request = urllib.request.Request(
                url,
                headers={
                    'User-Agent': self.user_agent,
                    'Accept': 'application/atom+xml'
                }
            )
    
            began = time.perf_counter()
            retry_needed = False
    
            try:
                with urllib.request.urlopen(
                    request,
                    timeout=self.timeout
                ) as response:
                    return response.read()
    
            except urllib.error.HTTPError as exc:
            
                # Handle HTTP 429 - Too Many Requests
                if exc.code == 429:
                    self.http_429 += 1
    
                    # Read Retry-After header from arXiv
                    retry_after = (
                        exc.headers.get('Retry-After')
                        if exc.headers else None
                    )
    
                    print(
                        f'[HTTP 429] Retry-After: {retry_after!r}',
                        flush=True
                    )
    
                    # Convert Retry-After to seconds
                    retry_seconds = retry_after_hint(retry_after)
    
                    print(
                        f'[HTTP 429] Parsed Retry-After: {retry_seconds!r}',
                        flush=True
                    )
    
                    raise RateLimitError(retry_seconds) from exc
    
                # Other HTTP 4xx errors
                if 400 <= exc.code < 500:
                    raise PermanentRequestError(
                        f'HTTP {exc.code} (not retryable)'
                    ) from exc
    
                # HTTP 5xx errors
                if attempt == self.retries:
                    raise RuntimeError(
                        f'HTTP {exc.code} after retries'
                    ) from exc
    
                retry_needed = True
    
            except (urllib.error.URLError, TimeoutError) as exc:
            
                if attempt == self.retries:
                    raise RuntimeError(
                        f'Network request failed after retries: {exc}'
                    ) from exc
    
                retry_needed = True
    
            finally:
                self.network_seconds += time.perf_counter() - began
    
            # Retry network errors and HTTP 5xx
            if retry_needed:
                self.retries_done += 1
                self._sleep(
                    min(60, 2 ** (attempt + 1)),
                    'retry'
                )
    
        raise AssertionError('unreachable')
    def metrics(self):
        return {
            'api_requests_total': self.requests,
            'retry_count': self.retries_done,
            'http_429_count': self.http_429,
            'pacing_wait_seconds': round(self.pacing_wait_seconds, 3),
            'retry_wait_seconds': round(self.retry_wait_seconds, 3),
            'network_seconds': round(self.network_seconds, 3),
        }


def validate_cached_batch(records, chunk, path):
    if not isinstance(records, dict) or set(records) != set(chunk):
        raise ValueError(f'Invalid metadata checkpoint: {path}')
    for rec in records.values():
        if not isinstance(rec, dict) or rec.get('status') not in ('found', 'not_found'):
            raise ValueError(f'Invalid status in metadata checkpoint: {path}')
        if rec['status'] == 'found':
            paper_id = rec.get('arxiv_id')
            n = rec.get('latest_version')
            if type(n) is not int or n < 1 or rec.get('versions') != [f'{paper_id}v{k}' for k in range(1, n+1)]:
                raise ValueError(f'Invalid version list in {path}: {paper_id}')


def load_revision_cache(out: Path) -> tuple[Path, dict[str, str]]:
    """Can reuse the earlier enrich_revised_dates.py cache if present."""
    path = out / 'revised_dates_cache.json'
    raw = json.loads(path.read_text(encoding='utf-8')) if path.exists() else {}
    if not isinstance(raw, dict):
        raise ValueError('Revision cache must be an object')
    for vid, date in raw.items():
        if not VERSIONED.fullmatch(vid) or not valid_date(date):
            raise ValueError(f'Invalid revision date cache entry: {vid}')
    return path, raw


def enrich_batch(records, cache, cache_path, client, revision_batch_size):
    """For one metadata batch, fetch version-specific v2...vN immediately.

    Layers prevent asking for two versions of the same base paper at once.
    Cache is persisted after every successful network request.
    """
    tasks = {}
    for r in records.values():
        if r['status'] != 'found':
            continue
        n = r['latest_version']
        for version in range(2, n + 1):
            version_id = f"{r['arxiv_id']}v{version}"
            if version_id not in cache:
                tasks.setdefault(version, []).append(version_id)
    for ver in sorted(tasks):
        candidates = tasks[ver]
        for start in range(0, len(candidates), revision_batch_size):
            chunk = candidates[start:start+revision_batch_size]
            updates = parse_version_updates(client.fetch(chunk))
            missing = [vid for vid in chunk if vid not in updates]
            if missing:
                # Save returned entries before stopping; no invented dates.
                for vid in chunk:
                    if vid in updates:
                        cache[vid] = updates[vid]
                write_json_atomic(cache_path, cache)
                raise RuntimeError(f'Missing exact revision responses for {missing[:5]}; '
                                   'check API or retry with --revision-batch-size 1')
            cache.update({vid: updates[vid] for vid in chunk})
            write_json_atomic(cache_path, cache)
            print(f'    revision v{ver}: cached {len(chunk)} dates', flush=True)


def attach_revised_dates(record: dict, cache: dict[str, str]) -> dict:
    p = dict(record)
    if p.get('status') != 'found':
        return p
    n = p['latest_version']
    if n == 1:
        p['revised_dates'] = []
    else:
        keys = [f"{p['arxiv_id']}v{i}" for i in range(2, n+1)]
        dates = [cache.get(k) for k in keys]
        p['revised_dates'] = dates if all(dates) else None
    return p


def write_outputs(out: Path, ids: list[str], collected, cache, stopped_reason):
    status = [collected.get(pid, {'arxiv_id': pid, 'status': 'pending'}) for pid in ids]
    papers = [attach_revised_dates(rec, cache) for rec in status if rec['status'] == 'found']
    completed = sum(p['revised_dates'] is not None for p in papers)
    pending = len(papers) - completed
    summary = {
        'total_ids': len(ids),
        'found': len(papers),
        'not_found': sum(x['status'] == 'not_found' for x in status),
        'error': sum(x['status'] == 'error' for x in status),
        'pending': sum(x['status'] == 'pending' for x in status),
        'revision_completed_papers': completed,
        'revision_pending_papers': pending,
        'revision_dates_cached': len(cache),
        'stopped_reason': stopped_reason,
        'generated_at_utc': datetime.now(timezone.utc).isoformat(),
        'output_file': str(out / 'discovered_papers_with_revised_dates.json'),
    }
    # Do not overwrite discovery_status.json or discovered_papers.json from an old run.
    write_json_atomic(out / 'discovered_papers_with_revised_dates.json', papers)
    write_json_atomic(out / 'discovery_status_integrated.json', status)
    write_json_atomic(out / 'summary_integrated.json', summary)
    return summary


def discover_integrated(ids: list[str], out: Path, batch_size: int,
                        revision_batch_size: int, client: ArxivClient,
                        max_batches=None, report_session=True):
    ensure_checkpoint_identity(out, ids, batch_size)
    batches_dir = out / 'batches'
    batches_dir.mkdir(parents=True, exist_ok=True)
    cache_path, cache = load_revision_cache(out)
    collected = {}
    stopped_reason = None
    cached_metadata_batches = 0
    fetched_metadata_batches = 0
    start = time.perf_counter()
    total_batches = (len(ids) + batch_size - 1) // batch_size
    try:
        for i in range(total_batches):
            if max_batches is not None and i >= max_batches:
                stopped_reason = 'max_batches_test_limit'
                break
            chunk = ids[i*batch_size:(i+1)*batch_size]
            checkpoint = batches_dir / f'batch_{i:05d}.json'
            if checkpoint.exists():
                records = json.loads(checkpoint.read_text(encoding='utf-8'))
                validate_cached_batch(records, chunk, checkpoint)
                cached_metadata_batches += 1
                print(f'[{i+1}/{total_batches}] metadata: cached', flush=True)
            else:
                try:
                    parsed = parse_metadata(client.fetch(chunk))
                    records = {pid: parsed.get(pid, {'arxiv_id': pid, 'status': 'not_found'}) for pid in chunk}
                except RateLimitError:
                    raise
                except (RuntimeError, ValueError, ET.ParseError) as exc:
                    raise RuntimeError(f'Metadata batch {i+1}: {exc}') from exc
                write_json_atomic(checkpoint, records)
                fetched_metadata_batches += 1
                print(f'[{i+1}/{total_batches}] metadata: fetched {sum(r["status"]=="found" for r in records.values())}/{len(chunk)}', flush=True)
            collected.update(records)
            # Important: revision dates are gathered *before* the next metadata batch.
            try:
                enrich_batch(records, cache, cache_path, client, revision_batch_size)
            except RateLimitError:
                raise
            except (RuntimeError, ET.ParseError, ValueError) as exc:
                raise RuntimeError(f'Revision batch near metadata batch {i+1}: {exc}') from exc
    except RateLimitError as exc:
        stopped_reason = 'http_429'
        print(f'PAUSED: {exc}. No more requests. Keep your checkpoint/cache!',
              file=sys.stderr, flush=True)
    except KeyboardInterrupt:
        stopped_reason = 'user_interrupt'
        print('Interrupted. Completed metadata/revision checkpoints are intact.',
              file=sys.stderr, flush=True)
    except (RuntimeError, ET.ParseError, ValueError) as exc:
        stopped_reason = 'request_or_parse_error'
        print(f'ERROR: {exc}', file=sys.stderr, flush=True)
    finally:
        summary = write_outputs(out, ids, collected, cache, stopped_reason)
        runtime = time.perf_counter() - start
        session = {
            'timestamp_utc': datetime.now(timezone.utc).isoformat(),
            'runtime_seconds': round(runtime, 3),
            'cached_metadata_batches': cached_metadata_batches,
            'fetched_metadata_batches': fetched_metadata_batches,
            'revision_dates_cached_total': len(cache),
            'output_directory': str(out),
            'stopped_reason': stopped_reason,
            **client.metrics(),
        }
        if report_session:
            sessions = out / 'benchmark_sessions.jsonl'
            with sessions.open('a', encoding='utf-8') as f:
                f.write(json.dumps(session, ensure_ascii=False) + '\n')
        print(json.dumps(summary, indent=2, ensure_ascii=False), flush=True)
        print('Session metrics:', json.dumps(session, indent=2, ensure_ascii=False), flush=True)
    return summary


def main(argv=None):
    p = argparse.ArgumentParser(description='Integrated arXiv Entry Discovery: metadata + all revised_dates')
    p.add_argument('--ids-file', type=Path, help='group_ids.txt from prepare_group.py')
    p.add_argument('--start', default='2609.12834')
    p.add_argument('--end', default='2609.17833')
    p.add_argument('--limit', type=int, help='Test on first N IDs (use a fresh --output)')
    p.add_argument('--batch-size', type=int, default=20)
    p.add_argument('--revision-batch-size', type=int, default=20)
    p.add_argument('--max-batches', type=int, help='Test just first N metadata batches')
    p.add_argument('--output', type=Path, default=Path('data/group/discovery_integrated'))
    p.add_argument('--delay', type=float, default=8.0)
    args = p.parse_args(argv)
    if not 1 <= args.batch_size <= 50 or not 1 <= args.revision_batch_size <= 50:
        p.error('Both batch sizes must be between 1 and 50')
    if args.delay < 3:
        p.error('Delay must be >= 3 seconds')
    if args.limit is not None and args.limit < 1:
        p.error('--limit must be positive')
    if args.max_batches is not None and args.max_batches < 1:
        p.error('--max-batches must be positive')
    try:
        ids = load_ids_file(args.ids_file) if args.ids_file else generate_ids(args.start, args.end)
        if args.limit:
            ids = ids[:args.limit]
        client = ArxivClient(delay=args.delay, user_agent=os.environ.get('ARXIV_USER_AGENT'))
        summary = discover_integrated(ids, args.output, args.batch_size,
                                      args.revision_batch_size, client, args.max_batches)
        return 0 if not (summary['error'] or summary['pending'] or summary['revision_pending_papers']
                         or summary['stopped_reason']) else 1
    except (ValueError, OSError, json.JSONDecodeError) as exc:
        p.error(str(exc))


if __name__ == '__main__':
    raise SystemExit(main())
