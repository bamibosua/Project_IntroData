"""Enrich EXISTING discovery results with per-version revision dates.

Run from the project root:
    python src/enrich_revised_dates.py --data-dir data/group/discovery --delay 8

Python 3.10+, stdlib only. Requires adjacent discovery.py from the 429-fix version.
Never modifies previously collected batches or discovered_papers.json.
"""

import argparse
import hashlib
import json
import os
import sys
import xml.etree.ElementTree as ET
from datetime import datetime
from pathlib import Path

from discovery import ATOM, VERSIONED, ArxivClient, RateLimitError, write_json_atomic


def read_papers(path: Path) -> list[dict]:
    papers = json.loads(path.read_text(encoding='utf-8'))
    if not isinstance(papers, list) or not papers:
        raise ValueError('Expected a nonempty list in discovered_papers.json')

    known_ids = set()
    for paper in papers:
        if not isinstance(paper, dict) or not isinstance(paper.get('arxiv_id'), str):
            raise ValueError('Invalid paper in discovered_papers.json')
        paper_id = paper['arxiv_id']
        if paper_id in known_ids:
            raise ValueError(f'Duplicate paper ID: {paper_id}')
        known_ids.add(paper_id)
        count = paper.get('latest_version')
        if type(count) is not int or count < 1:
            raise ValueError(f'Missing latest_version for {paper_id}')
        expected = [f'{paper_id}v{n}' for n in range(1, count + 1)]
        if paper.get('versions') != expected:
            raise ValueError(f'Invalid versions list for {paper_id}')
        if not paper.get('submission_date'):
            raise ValueError(f'Missing submission_date for {paper_id}')
    return papers


def version_tasks(papers: list[dict]) -> list[str]:
    """Only versions 2+ need remote calls; v1 date is submission_date.

    Group in layers (all v2, then all v3, etc.), permitting batches of
    distinct papers and avoiding multiple versions of one paper per request.
    """
    output = []
    max_version = max(p['latest_version'] for p in papers)
    for n in range(2, max_version + 1):
        output.extend(f"{p['arxiv_id']}v{n}" for p in papers if p['latest_version'] >= n)
    return output


def parse_version_updates(xml_bytes: bytes) -> dict[str, str]:
    """Retrieve EXACT version IDs and their Atom entry updated timestamp.

    Never use feed.updated, which is the feed update time (not paper history).
    """
    root = ET.fromstring(xml_bytes)
    result = {}
    for entry in root.findall(f'{ATOM}entry'):
        ident = (entry.findtext(f'{ATOM}id') or '').strip()
        match = VERSIONED.search(ident)
        if match is None:
            continue  # arXiv API may include an error placeholder entry
        version_id = f'{match.group(1)}v{int(match.group(2))}'
        updated = (entry.findtext(f'{ATOM}updated') or '').strip()
        if not updated:
            continue
        try:
            datetime.fromisoformat(updated.replace('Z', '+00:00'))
        except ValueError:
            continue
        if version_id in result and result[version_id] != updated:
            raise ValueError(f'Conflicting updated timestamps for {version_id}')
        result[version_id] = updated
    return result


def dataset_fingerprint(papers: list[dict]) -> str:
    identity = [[p['arxiv_id'], p['latest_version']] for p in papers]
    raw = json.dumps(identity, separators=(',', ':'), ensure_ascii=False)
    return hashlib.sha256(raw.encode('utf-8')).hexdigest()


def prepare_cache(data_dir: Path, papers: list[dict]) -> tuple[Path, dict[str, str]]:
    """Cache is independent of discovery's original batches and manifest."""
    data_dir.mkdir(parents=True, exist_ok=True)
    manifest_path = data_dir / 'revised_dates_manifest.json'
    cache_path = data_dir / 'revised_dates_cache.json'
    identity = {'fingerprint': dataset_fingerprint(papers), 'total_papers': len(papers)}
    if manifest_path.exists():
        saved = json.loads(manifest_path.read_text(encoding='utf-8'))
        if saved != identity:
            raise ValueError('Dataset changed since revisions cache was created. '
                             'Use a different --data-dir or back up and inspect the revision files.')
    elif cache_path.exists():
        raise ValueError('Revision cache exists without a manifest; refusing to assume it is compatible.')
    else:
        write_json_atomic(manifest_path, identity)
    cache = json.loads(cache_path.read_text(encoding='utf-8')) if cache_path.exists() else {}
    if not isinstance(cache, dict):
        raise ValueError('Invalid revision cache: expected JSON object')
    return cache_path, cache


def write_result(papers: list[dict], cache: dict[str, str], output: Path,
                 summary_path: Path, stopped_reason=None) -> dict:
    result = []
    completed = 0
    single_version = 0
    for paper in papers:
        p = dict(paper)  # keep all original metadata unchanged
        n = p['latest_version']
        if n == 1:
            p['revised_dates'] = []
            single_version += 1
            completed += 1
        else:
            versions = [f"{p['arxiv_id']}v{i}" for i in range(2, n + 1)]
            dates = [cache.get(vid) for vid in versions]
            if all(dates):
                p['revised_dates'] = dates  # ordered [v2, v3, ... vN]
                completed += 1
            else:
                p['revised_dates'] = None  # partial: never invent unknown dates
        result.append(p)

    summary = {
        'total_papers': len(papers),
        'single_version_papers': single_version,
        'completed_papers': completed,
        'pending_papers': len(papers) - completed,
        'version_dates_cached': len(cache),
        'stopped_reason': stopped_reason,
        'output_file': str(output),
    }
    write_json_atomic(output, result)
    write_json_atomic(summary_path, summary)
    return summary


def enrich(papers: list[dict], data_dir: Path, client: ArxivClient,
           batch_size: int = 20, max_batches: int | None = None,
           output: Path | None = None) -> dict:
    if not 1 <= batch_size <= 50:
        raise ValueError('batch_size must be between 1 and 50')
    if max_batches is not None and max_batches < 1:
        raise ValueError('max_batches must be positive')
    output = output or data_dir / 'discovered_papers_with_revised_dates.json'
    if output.resolve() == (data_dir / 'discovered_papers.json').resolve():
        raise ValueError('Refusing to overwrite original discovered_papers.json')

    cache_path, cache = prepare_cache(data_dir, papers)
    all_tasks = version_tasks(papers)
    need = [version_id for version_id in all_tasks if version_id not in cache]
    needed_by_version = {}
    for version_id in need:
        version_number = int(VERSIONED.search(version_id).group(2))
        needed_by_version.setdefault(version_number, []).append(version_id)

    # Never mix two versions of the same paper in one batch.
    batches = []
    for n in sorted(needed_by_version):
        ids = needed_by_version[n]
        batches.extend(ids[i:i + batch_size] for i in range(0, len(ids), batch_size))

    print(f'Papers: {len(papers)} | Revision versions needed: {len(all_tasks)} '
          f'| Cached: {len(all_tasks) - len(need)} | Requests planned: {len(batches)}', flush=True)

    stopped_reason = None
    calls = 0
    try:
        for i, batch in enumerate(batches, start=1):
            if max_batches is not None and calls >= max_batches:
                stopped_reason = 'max_batches_test_limit'
                break
            try:
                response = client.fetch(batch)
                returned = parse_version_updates(response)
                missing = [v for v in batch if v not in returned]
                for version_id in batch:
                    if version_id in returned:
                        cache[version_id] = returned[version_id]
                # Persist even a partial response before stopping.
                write_json_atomic(cache_path, cache)
                calls += 1
                print(f'[{i}/{len(batches)}] received {len(batch) - len(missing)}/{len(batch)} dates',
                      flush=True)
                if missing:
                    stopped_reason = 'missing_version_entries'
                    print(f'Incomplete API response: {missing[:5]} ... '
                          'Try again with --batch-size 1 after checking API behavior.',
                          file=sys.stderr, flush=True)
                    break
            except RateLimitError as exc:
                stopped_reason = 'http_429'
                print(f'Rate limited: {exc} STOPPING. Cached results are safe.',
                      file=sys.stderr, flush=True)
                break
            except (RuntimeError, ET.ParseError, ValueError) as exc:
                stopped_reason = 'request_or_parse_error'
                print(f'Error in batch {i}: {exc}. STOPPING without losing cache.',
                      file=sys.stderr, flush=True)
                break
    except KeyboardInterrupt:
        stopped_reason = 'user_interrupt'
        print('Interrupted by user; saved versions will be reused.', file=sys.stderr)
    finally:
        summary = write_result(papers, cache, output,
                               data_dir / 'revised_dates_summary.json', stopped_reason)
        print(json.dumps(summary, indent=2, ensure_ascii=False), flush=True)
    return summary


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description='Add complete revision dates to existing arXiv papers')
    parser.add_argument('--data-dir', type=Path, default=Path('data/group/discovery'))
    parser.add_argument('--batch-size', type=int, default=20)
    parser.add_argument('--delay', type=float, default=8.0)
    parser.add_argument('--max-batches', type=int,
                        help='Optional testing mode: make at most N HTTP requests')
    args = parser.parse_args(argv)
    if args.delay < 3:
        parser.error('Delay must be at least 3 seconds')

    try:
        papers = read_papers(args.data_dir / 'discovered_papers.json')
        client = ArxivClient(delay=args.delay, user_agent=os.environ.get('ARXIV_USER_AGENT'))
        summary = enrich(papers, args.data_dir, client, args.batch_size, args.max_batches)
        return 0 if summary['pending_papers'] == 0 else 1
    except (OSError, ValueError, json.JSONDecodeError) as exc:
        print(f'ERROR: {exc}', file=sys.stderr)
        return 2


if __name__ == '__main__':
    raise SystemExit(main())
