"""Entry Discovery for HCMUS IDS Milestone 1. Python 3.10+, stdlib only."""
import argparse
import hashlib
import json
import re
import sys
import time
from email.utils import parsedate_to_datetime
import urllib.error
import urllib.parse
import urllib.request
import xml.etree.ElementTree as ET
from datetime import datetime, timezone
from pathlib import Path

ATOM = '{http://www.w3.org/2005/Atom}'
ARXIV = '{http://arxiv.org/schemas/atom}'
ID_PATTERN = re.compile(r'^(\d{4})\.(\d{4,5})$')
VERSIONED = re.compile(r'(\d{4}\.\d{4,5})v(\d+)$')
API = 'https://export.arxiv.org/api/query'


def generate_ids(start: str, end: str) -> list[str]:
    """Generate an inclusive ID range in a single YYMM; reject invalid bounds."""
    a, b = ID_PATTERN.fullmatch(start), ID_PATTERN.fullmatch(end)
    if not a or not b:
        raise ValueError('IDs must look like 2609.12834')
    if a.group(1) != b.group(1):
        raise ValueError('This version supports one YYMM per range only')
    lo, hi = int(a.group(2)), int(b.group(2))
    if lo > hi:
        raise ValueError('Start ID must not exceed end ID')
    width = max(len(a.group(2)), len(b.group(2)))
    return [f'{a.group(1)}.{n:0{width}d}' for n in range(lo, hi + 1)]


def load_ids_file(path: Path) -> list[str]:
    """Read a deduplicated one-ID-per-line worklist, never a numeric interval."""
    lines = path.read_text(encoding='utf-8').splitlines()
    ids = [s.strip() for s in lines if s.strip() and not s.lstrip().startswith('#')]
    if not ids:
        raise ValueError('IDs file is empty')
    bad = [arxiv_id for arxiv_id in ids if not ID_PATTERN.fullmatch(arxiv_id)]
    if bad:
        raise ValueError(f'Invalid arXiv ID in file: {bad[0]}')
    if len(ids) != len(set(ids)):
        raise ValueError('IDs file has duplicates; use prepare_group.py to deduplicate first')
    return ids


def ensure_checkpoint_identity(out: Path, ids: list[str], batch_size: int) -> None:
    """Prevent stale checkpoint files from being used for a different ID set."""
    out.mkdir(parents=True, exist_ok=True)
    manifest_path = out / 'checkpoint_manifest.json'
    digest = hashlib.sha256(('\n'.join(ids) + '\n').encode('utf-8')).hexdigest()
    expected = {'ids_sha256': digest, 'total_ids': len(ids), 'batch_size': batch_size}
    if manifest_path.exists():
        existing = json.loads(manifest_path.read_text(encoding='utf-8'))
        if existing != expected:
            raise ValueError('Output folder belongs to another ID set or batch size. '
                             'Use a different --output, or delete it only if you no longer need it.')
    elif (out / 'batches').exists() and any((out / 'batches').glob('batch_*.json')):
        raise ValueError('Old batches without a checkpoint manifest exist. Use a new --output.')
    else:
        write_json_atomic(manifest_path, expected)


def clean_text(value):
    return ' '.join((value or '').split())


def parse_atom(xml_bytes: bytes) -> dict[str, dict]:
    """Read returned entries, ignoring feed-level error messages."""
    root = ET.fromstring(xml_bytes)
    results = {}
    for entry in root.findall(f'{ATOM}entry'):
        url = clean_text(entry.findtext(f'{ATOM}id'))
        match = VERSIONED.search(url)
        if not match:
            continue  # e.g. arXiv API's 'Error' entry
        paper_id, latest = match.group(1), int(match.group(2))
        title = clean_text(entry.findtext(f'{ATOM}title'))
        authors = [clean_text(x.findtext(f'{ATOM}name')) for x in entry.findall(f'{ATOM}author')]
        authors = [x for x in authors if x]
        published = clean_text(entry.findtext(f'{ATOM}published'))
        updated = clean_text(entry.findtext(f'{ATOM}updated'))
        journal = clean_text(entry.findtext(f'{ARXIV}journal_ref'))
        results[paper_id] = {
            'arxiv_id': paper_id, 'title': title, 'authors': authors,
            'submission_date': published, 'latest_updated': updated,
            'revised_dates': None,  # latest-only lookup cannot establish complete revision history
            'venue': journal or None,
            'latest_version': latest,
            'versions': [f'{paper_id}v{i}' for i in range(1, latest+1)],
            'abstract': clean_text(entry.findtext(f'{ATOM}summary')),
            'status': 'found',
        }
    return results


class RateLimitError(RuntimeError):
    """arXiv returned HTTP 429. Caller must pause the crawl immediately."""

    def __init__(self, retry_after=None):
        self.retry_after = retry_after
        extra = f' Retry-After: {retry_after}.' if retry_after else ''
        super().__init__('arXiv returned HTTP 429 (Too Many Requests).' + extra)


def retry_after_hint(value):
    """Return server-provided Retry-After seconds, if possible; never guess a ban duration."""
    if not value:
        return None
    value = value.strip()
    if value.isdecimal():
        return int(value)
    try:
        server_time = parsedate_to_datetime(value)
        if server_time.tzinfo is None:
            server_time = server_time.replace(tzinfo=timezone.utc)
        return max(0, int((server_time - datetime.now(timezone.utc)).total_seconds()))
    except (ValueError, TypeError, OverflowError):
        return value


class ArxivClient:
    def __init__(self, delay=3.2, timeout=40, retries=4, user_agent=None):
        self.delay = delay
        self.timeout = timeout
        self.retries = retries
        self.user_agent = user_agent or 'HCMUS-IDS-EntryDiscovery/1.0 (academic research; contact: set ARXIV_USER_AGENT)'
        self._last_attempt = None

    def fetch(self, ids: list[str]) -> bytes:
        # One connection at a time. The delay is between *start times* of requests.
        url = API + '?' + urllib.parse.urlencode({'id_list': ','.join(ids), 'max_results': len(ids)})
        for attempt in range(self.retries + 1):
            if self._last_attempt is not None:
                time.sleep(max(0, self.delay - (time.monotonic() - self._last_attempt)))
            self._last_attempt = time.monotonic()
            request = urllib.request.Request(
                url, headers={'User-Agent': self.user_agent, 'Accept': 'application/atom+xml'}
            )
            try:
                with urllib.request.urlopen(request, timeout=self.timeout) as response:
                    return response.read()
            except urllib.error.HTTPError as exc:
                if exc.code == 429:
                    # Important: don't keep hammering arXiv, even during retries.
                    raise RateLimitError(retry_after_hint(exc.headers.get('Retry-After') if exc.headers else None)) from exc
                if 400 <= exc.code < 500:
                    raise RuntimeError(f'Non-retryable arXiv HTTP error {exc.code}') from exc
                if attempt == self.retries:
                    raise RuntimeError(f'arXiv HTTP {exc.code} after retries') from exc
                time.sleep(min(60, 2 ** (attempt + 1)))
            except (urllib.error.URLError, TimeoutError) as exc:
                if attempt == self.retries:
                    raise RuntimeError(f'arXiv request failed after retries: {exc}') from exc
                time.sleep(min(60, 2 ** (attempt + 1)))
        raise AssertionError('unreachable')


def write_json_atomic(path: Path, data):
    path.parent.mkdir(parents=True, exist_ok=True)
    temp = path.with_suffix(path.suffix + '.tmp')
    temp.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding='utf-8')
    temp.replace(path)


def discover(ids: list[str], out: Path, batch_size: int, client: ArxivClient, refresh=False):
    """Resume cached batches, stop safely on 429, and write a partial status summary."""
    ensure_checkpoint_identity(out, ids, batch_size)
    batches_dir = out / 'batches'
    batches_dir.mkdir(parents=True, exist_ok=True)
    all_results = {}
    batch_count = (len(ids) + batch_size - 1) // batch_size
    stopped_reason = None
    try:
        for batch_i in range(batch_count):
            chunk = ids[batch_i * batch_size:(batch_i + 1) * batch_size]
            checkpoint = batches_dir / f'batch_{batch_i:05d}.json'
            if checkpoint.exists() and not refresh:
                records = json.loads(checkpoint.read_text(encoding='utf-8'))
                if (not isinstance(records, dict) or set(records) != set(chunk)
                        or any(not isinstance(r, dict) or r.get('status') not in ('found', 'not_found')
                               for r in records.values())):
                    raise ValueError(f'Checkpoint inconsistent: {checkpoint}. Keep a backup before fixing it.')
                print(f'[{batch_i + 1}/{batch_count}] cached ({len(chunk)} IDs)', flush=True)
            else:
                try:
                    found = parse_atom(client.fetch(chunk))
                    records = {
                        paper_id: found.get(paper_id, {'arxiv_id': paper_id, 'status': 'not_found'})
                        for paper_id in chunk
                    }
                except RateLimitError as exc:
                    print(f'[{batch_i + 1}/{batch_count}] PAUSED: {exc}', file=sys.stderr, flush=True)
                    # Failed batch has NO checkpoint and will be retried on the next run.
                    all_results.update({
                        pid: {'arxiv_id': pid, 'status': 'error', 'error': 'HTTP 429: retry later'}
                        for pid in chunk
                    })
                    stopped_reason = 'http_429'
                    break
                except (RuntimeError, ET.ParseError) as exc:
                    print(f'[{batch_i + 1}/{batch_count}] ERROR: {exc}', file=sys.stderr, flush=True)
                    # No checkpoint: this batch will be retried on the next run.
                    all_results.update({
                        pid: {'arxiv_id': pid, 'status': 'error', 'error': str(exc)}
                        for pid in chunk
                    })
                    continue
                write_json_atomic(checkpoint, records)
                n_found = sum(r['status'] == 'found' for r in records.values())
                print(f'[{batch_i + 1}/{batch_count}] fetched: {n_found} found', flush=True)
            all_results.update(records)
    except KeyboardInterrupt:
        stopped_reason = 'user_interrupt'
        print('Interrupted by user. Preserving checkpoint files and exporting partial progress...',
              file=sys.stderr, flush=True)
    finally:
        # For never-visited IDs, use pending rather than claiming not_found or error.
        ordered = [all_results.get(pid, {'arxiv_id': pid, 'status': 'pending'}) for pid in ids]
        write_json_atomic(out / 'discovered_papers.json',
                          [r for r in ordered if r['status'] == 'found'])
        write_json_atomic(out / 'discovery_status.json', ordered)
        summary = {
            'start_id': ids[0], 'end_id': ids[-1], 'total_ids': len(ids),
            'found': sum(r['status'] == 'found' for r in ordered),
            'not_found': sum(r['status'] == 'not_found' for r in ordered),
            'error': sum(r['status'] == 'error' for r in ordered),
            'pending': sum(r['status'] == 'pending' for r in ordered),
            'stopped_reason': stopped_reason,
            'generated_at_utc': datetime.now(timezone.utc).isoformat(),
            'note': 'revised_dates is null: latest lookup alone does not provide every revision date',
        }
        write_json_atomic(out / 'summary.json', summary)
        print(json.dumps(summary, indent=2, ensure_ascii=False), flush=True)
    return summary


def main(argv=None):
    parser = argparse.ArgumentParser(description='arXiv Entry Discovery only (no source downloads)')
    parser.add_argument('--ids-file', type=Path, help='Combined group worklist (one ID per line)')
    parser.add_argument('--start', default='2609.12834')
    parser.add_argument('--end', default='2609.17833')
    parser.add_argument('--limit', type=int, help='Only first N IDs, for testing')
    parser.add_argument('--batch-size', type=int, default=20)
    parser.add_argument('--output', type=Path, default=Path('data/discovery'))
    parser.add_argument('--refresh', action='store_true', help='Re-fetch previously completed batches')
    parser.add_argument('--delay', type=float, default=3.2, help='Minimum seconds between API requests (>=3 recommended)')
    args = parser.parse_args(argv)
    if args.batch_size < 1 or args.batch_size > 50:
        parser.error('--batch-size must be between 1 and 50')
    if args.delay < 3.0:
        parser.error('--delay must be at least 3 seconds')
    if args.limit is not None and args.limit < 1:
        parser.error('--limit must be positive')
    try:
        ids = load_ids_file(args.ids_file) if args.ids_file else generate_ids(args.start, args.end)
    except ValueError as exc:
        parser.error(str(exc))
    if args.limit:
        ids = ids[:args.limit]
    print(f'Discovering {len(ids)} IDs: {ids[0]} -> {ids[-1]}', flush=True)
    import os
    client = ArxivClient(delay=args.delay, user_agent=os.environ.get('ARXIV_USER_AGENT'))
    try:
        summary = discover(ids, args.output, args.batch_size, client, refresh=args.refresh)
    except (ValueError, OSError) as exc:
        parser.error(str(exc))
    if summary['error'] or summary['pending']:
        return 1
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
