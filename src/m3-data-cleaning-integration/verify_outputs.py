"""Verify actual Parquet schemas, null counts and matching-table keys after cleaning."""
import argparse
from collections import Counter
import json
from pathlib import Path

import pyarrow.parquet as pq
from cleaning import SCHEMAS, Source, arxiv_id


def verify(root):
    report = json.loads((root / 'cleaning_report.json').read_text(encoding='utf-8'))
    errors, tables, keysets = [], {}, {}
    if report['run_status'] == 'failed':
        errors.append('Cleaning run failed; partial output must not be handed off.')
    # Empty versions have no hierarchy rows; retain their original coverage separately.
    if 'elements' in report['table_rows'] and report['run_status'] != 'failed':
        source = Source(Path(report['resolved_inputs']['fulltext_source']))
        coverage_files = source.find('coverage.jsonl')
        if len(coverage_files) == 1:
            import csv
            with (root / 'publication_coverage.csv').open(encoding='utf-8-sig', newline='') as f:
                scope = {r['arxiv_id'] for r in csv.DictReader(f)}
            coverage_counts = Counter()
            with source.open(coverage_files[0]) as src, (root / 'fulltext_version_coverage.jsonl').open('w', encoding='utf-8') as out:
                for line in src:
                    if not line.strip(): continue
                    row = json.loads(line)
                    if arxiv_id(row.get('arxiv_id')) not in scope: continue
                    out.write(json.dumps(row, ensure_ascii=False) + '\n')
                    coverage_counts['publications'] += 1
                    coverage_counts['versions'] += row.get('version_count', 0)
                    coverage_counts['empty_versions'] += row.get('empty_versions', 0)
            report['fulltext_version_coverage'] = dict(coverage_counts)
            (root / 'cleaning_report.json').write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding='utf-8')
    for name, expected in report['table_rows'].items():
        files = sorted((root / 'data' / name).glob('*.parquet'))
        counts, nulls, keys, duplicates = 0, Counter(), set(), 0
        for file in files:
            parquet = pq.ParquetFile(file)
            if not parquet.schema_arrow.equals(SCHEMAS[name], check_metadata=False):
                errors.append(f'{file}: schema mismatch')
            counts += parquet.metadata.num_rows
            # Arrow batches preserve null-list versus empty-list distinctions.
            columns = list(SCHEMAS[name].names) if name in {'papers', 'references', 'bibtex'} else None
            if columns:
                for batch in parquet.iter_batches(batch_size=20000, columns=columns):
                    for i, field in enumerate(batch.schema):
                        nulls[field.name] += batch.column(i).null_count
                    ids = batch.column(batch.schema.get_field_index('arxiv_id')).to_pylist()
                    second = 'ref_arxiv_id' if name == 'references' else 'citation_key' if name == 'bibtex' else None
                    other = batch.column(batch.schema.get_field_index(second)).to_pylist() if second else [None] * len(ids)
                    for aid, key in zip(ids, other):
                        identity = (aid, key) if second else aid
                        if identity in keys: duplicates += 1
                        keys.add(identity)
            else:
                for group in range(parquet.metadata.num_row_groups):
                    for i, field in enumerate(SCHEMAS[name]):
                        stat = parquet.metadata.row_group(group).column(i).statistics
                        if stat and stat.has_null_count: nulls[field.name] += stat.null_count
        if counts != expected: errors.append(f'{name}: expected {expected} rows, found {counts}')
        if expected and not files: errors.append(f'{name}: missing files')
        if duplicates: errors.append(f'{name}: {duplicates} duplicate primary keys')
        for field in SCHEMAS[name]:
            if not field.nullable and nulls[field.name]: errors.append(f'{name}.{field.name}: null key')
        tables[name] = {'rows': counts, 'parquet_files': len(files), 'null_counts': dict(nulls),
                        'duplicate_primary_keys': duplicates if name in {'papers', 'references', 'bibtex'} else 'checked_during_cleaning'}
        keysets[name] = keys
    papers = keysets.get('papers', set())
    for name in ('references', 'bibtex'):
        orphans = {key[0] for key in keysets.get(name, set())} - papers
        if orphans: errors.append(f'{name}: publication keys absent from papers: {sorted(orphans)[:5]}')
    result = {'valid': not errors, 'errors': errors, 'tables': tables,
              'scope': 'Schemas, row counts, nulls, matching-table PK/FK; fulltext PK/FK checked by cleaning.py.',
              'semantic_quality': 'M2 extraction and canonical citation keys still require review.'}
    (root / 'output_validation.json').write_text(json.dumps(result, indent=2), encoding='utf-8')
    print(json.dumps({'valid': result['valid'], 'errors': errors,
                      'rows': {k: v['rows'] for k, v in tables.items()}}, indent=2))
    return 0 if result['valid'] else 1


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--config', type=Path, default=Path(__file__).with_name('config.json'))
    args = parser.parse_args()
    config_path = args.config.resolve()
    config = json.loads(config_path.read_text(encoding='utf-8-sig'))
    raise SystemExit(verify((config_path.parent / config['output_dir']).resolve()))
