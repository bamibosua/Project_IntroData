"""Export Milestone 1 Parquet tables by citing-paper ownership."""
import argparse
import csv
import json
import re
import os
import tempfile
from collections import defaultdict
from pathlib import Path

import pyarrow as pa
import pyarrow.parquet as pq

TEXT = pa.string()
TEXT_LIST = pa.list_(TEXT)
PAPERS = pa.schema([
    pa.field('arxiv_id', TEXT, nullable=False),
    ('title', TEXT), ('authors', TEXT_LIST), ('submission_date', TEXT),
    ('revised_dates', TEXT_LIST), ('venue', TEXT),
])
REFERENCES = pa.schema([
    pa.field('arxiv_id', TEXT, nullable=False),
    pa.field('ref_arxiv_id', TEXT, nullable=False),
    ('ref_title', TEXT), ('ref_authors', TEXT_LIST),
    ('ref_submission_date', TEXT), ('ref_s2_id', TEXT),
])


def base_id(value):
    value = (value or '').strip()
    value = re.sub(r'^arxiv:\s*', '', value, flags=re.I)
    value = re.sub(r'v[0-9]+$', '', value, flags=re.I)
    # Convert only modern folder names; preserve old IDs like gr-qc/0207049.
    return re.sub(r'^(\d{4})-(\d{4,5})$', r'\1.\2', value)


def ownership(path):
    owners = {}
    with path.open(encoding='utf-8-sig', newline='') as f:
        for line, row in enumerate(csv.reader(f), 1):
            if not row or not any(cell.strip() for cell in row):
                continue
            if not owners and row[0].strip().lower() in {'arxiv_id', 'id', 'paper_id'}:
                continue
            if len(row) < 2:
                raise ValueError(f'{path}:{line}: expected ID and owner')
            aid, owner = base_id(row[0]), row[1].strip()
            if not aid or not owner or re.search(r'[<>:"/\\|?*]', owner) or owner in {'.', '..'}:
                raise ValueError(f'{path}:{line}: invalid ID or owner')
            if aid in owners and owners[aid] != owner:
                raise ValueError(f'{path}:{line}: {aid} has multiple owners')
            owners[aid] = owner
    return owners


def read_record(roots, aid, filename, report):
    folder = aid.replace('.', '-').replace('/', '-')
    paths = list(dict.fromkeys(
        (root / name / filename).resolve()
        for root in roots for name in dict.fromkeys((folder, aid))
        if (root / name / filename).is_file()
    ))
    if not paths:
        report['missing_' + filename.removesuffix('.json')].append(aid)
        return None
    selected = None
    selected_path = None
    for path in paths:
        try:
            obj = json.loads(path.read_text(encoding='utf-8-sig'))
            if not isinstance(obj, dict) or base_id(obj.get('arxiv_id')) != aid:
                raise ValueError('arxiv_id does not match the assigned ID')
            if filename == 'metadata.json' and (
                obj.get('status', 'found') != 'found' or not obj.get('title')
            ):
                raise ValueError('metadata not found or title missing')
            if filename == 'references.json' and not isinstance(obj.get('references'), list):
                raise ValueError('references must be a list')
        except (OSError, ValueError, TypeError, AttributeError) as exc:
            report['invalid_files'].append({'arxiv_id': aid, 'file': str(path), 'error': str(exc)})
            continue
        if selected is None:
            selected, selected_path = obj, path
        elif selected != obj:
            report['different_copies'].append({
                'arxiv_id': aid, 'used': str(selected_path), 'other': str(path),
            })
    return selected


def validate_row(row, schema):
    for field in schema:
        value = row.get(field.name)
        if value is None:
            if not field.nullable:
                raise ValueError(f'{field.name} must not be null')
            continue
        if pa.types.is_list(field.type):
            if not isinstance(value, list) or any(not isinstance(item, str) for item in value):
                raise ValueError(f'{field.name} must be a list of strings')
        elif not isinstance(value, str):
            raise ValueError(f'{field.name} must be a string')


def write_table(base, name, rows, schema):
    path = base / 'data' / name / 'part-00000.parquet'
    path.parent.mkdir(parents=True, exist_ok=True)
    pq.write_table(pa.Table.from_pylist(rows, schema=schema), path, compression='zstd')


def export_owner(owner, ids, roots, output):
    papers, refs = [], []
    report = {key: [] for key in (
        'missing_metadata', 'missing_references', 'invalid_files', 'different_copies',
        'reference_status_to_check', 'conflicting_reference_pairs', 'papers_not_exported',
    )}
    filtered = 0
    for aid in sorted(ids):
        metadata = read_record(roots, aid, 'metadata.json', report)
        source = read_record(roots, aid, 'references.json', report)
        if metadata is None:
            report['papers_not_exported'].append(aid)
            continue  # Do not publish orphan reference rows.
        paper = {**{k: metadata.get(k) for k in PAPERS.names}, 'arxiv_id': aid}
        try:
            validate_row(paper, PAPERS)
        except ValueError as exc:
            report['invalid_files'].append({'arxiv_id': aid, 'file': 'metadata.json', 'error': str(exc)})
            report['papers_not_exported'].append(aid)
            continue
        papers.append(paper)
        if source is None:
            continue
        status = source.get('status')
        verification = source.get('verification')
        if verification is not None and not isinstance(verification, dict):
            report['invalid_files'].append({'arxiv_id': aid, 'file': 'references.json',
                                            'error': 'verification must be an object'})
        if (status not in (None, 'ok') or
                (isinstance(verification, dict) and verification.get('complete') is False) or
                (verification is not None and not isinstance(verification, dict))):
            report['reference_status_to_check'].append({'arxiv_id': aid, 'status': status})
        seen = {}
        for index, item in enumerate(source['references']):
            if not isinstance(item, dict) or not isinstance(item.get('ref_arxiv_id'), (str, type(None))):
                report['invalid_files'].append({
                    'arxiv_id': aid, 'file': 'references.json',
                    'error': f'Invalid reference at index {index}',
                })
                continue
            ref_id = base_id(item.get('ref_arxiv_id'))
            if not ref_id:
                filtered += 1
                continue
            row = {k: item.get(k) for k in REFERENCES.names}
            row.update(arxiv_id=aid, ref_arxiv_id=ref_id)
            try:
                validate_row(row, REFERENCES)
            except ValueError as exc:
                report['invalid_files'].append({'arxiv_id': aid, 'file': 'references.json',
                                                'error': f'Reference at index {index}: {exc}'})
                continue
            if ref_id in seen:
                if seen[ref_id] != row:
                    report['conflicting_reference_pairs'].append({'arxiv_id': aid, 'ref_arxiv_id': ref_id})
                continue
            seen[ref_id] = row
            refs.append(row)

    base = output / owner
    counts = dict(ids_selected=len(ids), papers=len(papers), references=len(refs),
                  filtered_references_without_arxiv_id=filtered)
    # Prepare every file before replacing any existing result.
    output.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix='.export-', dir=output) as temporary:
        staged = Path(temporary)
        write_table(staged, 'papers', papers, PAPERS)
        write_table(staged, 'references', refs, REFERENCES)
        (staged / 'export_report.json').write_text(
            json.dumps({'counts': counts, **report}, ensure_ascii=False, indent=2), encoding='utf-8'
        )
        for relative in ('data/papers/part-00000.parquet',
                         'data/references/part-00000.parquet', 'export_report.json'):
            target = base / relative
            target.parent.mkdir(parents=True, exist_ok=True)
            os.replace(staged / relative, target)
    print(f'{owner}: papers={len(papers)}/{len(ids)}, references={len(refs)}, '
          f'filtered_without_arxiv_id={filtered}. Report: {base / "export_report.json"}')


def load_config(path):
    config = json.loads(path.read_text(encoding='utf-8-sig'))
    if not isinstance(config, dict):
        raise ValueError('Config must be a JSON object')
    unknown = config.keys() - {'paper_roots', 'ownership', 'output_dir'}
    if unknown:
        raise ValueError(f'Unknown config keys: {", ".join(sorted(unknown))}')
    roots = config.get('paper_roots')
    if not isinstance(roots, list) or not roots:
        raise ValueError('paper_roots must be a non-empty list of folder paths')

    def resolve(value, key):
        if not isinstance(value, str) or not value.strip():
            raise ValueError(f'{key} must be a non-empty path string')
        target = Path(value).expanduser()
        return (path.parent / target).resolve()

    return {
        'paper_root': [resolve(root, 'paper_roots') for root in roots],
        'ownership': resolve(config.get('ownership'), 'ownership'),
        'out': resolve(config.get('output_dir'), 'output_dir'),
    }


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--config', type=Path,
                        help='JSON config file (default: export_config.json next to this script).')
    parser.add_argument('--paper-root', type=Path, action='append',
                        help='Folder directly containing paper folders. Repeat for multiple sources.')
    parser.add_argument('--ownership', type=Path, help='CSV mapping paper IDs to owners.')
    parser.add_argument('--owner', help='Exact owner in the CSV. Omit to export all owners.')
    parser.add_argument('--id', help='Export a single assigned paper for testing.')
    parser.add_argument('--limit', type=int,
                        help='Select at most N papers in ownership CSV order, after owner/ID filters (total across owners).')
    parser.add_argument('--out', type=Path, help='Local output folder')
    args = parser.parse_args()
    if args.limit is not None and args.limit <= 0:
        parser.error('--limit must be a positive integer.')
    # Load configured ownership even when input/output are overridden on the CLI.
    config = {}
    if args.config is not None or args.paper_root is None or args.out is None or args.ownership is None:
        config_path = (args.config or Path(__file__).with_name('export_config.json')).resolve()
        try:
            config = load_config(config_path)
        except (OSError, ValueError) as exc:
            parser.error(f'Cannot load config {config_path}: {exc}')
    args.paper_root = args.paper_root or config['paper_root']
    args.out = args.out or config['out']
    args.ownership = args.ownership or config.get(
        'ownership', Path('src/entry_discovery/data/input/id_ownership.csv'))
    for root in args.paper_root:
        if not root.is_dir():
            parser.error(f'Paper root does not exist: {root}')
    if not args.ownership.is_file():
        parser.error(f'Ownership CSV does not exist: {args.ownership}')
    groups = defaultdict(list)
    selected = 0
    for aid, owner in ownership(args.ownership).items():
        if (not args.owner or owner == args.owner) and (not args.id or aid == base_id(args.id)):
            groups[owner].append(aid)
            selected += 1
            if args.limit is not None and selected >= args.limit:
                break
    if not groups:
        parser.error('No assigned IDs match the selected owner/ID.')
    for owner, ids in groups.items():
        export_owner(owner, ids, args.paper_root, args.out)


if __name__ == '__main__':
    main()
