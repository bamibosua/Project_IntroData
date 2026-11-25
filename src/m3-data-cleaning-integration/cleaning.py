"""M3 member 1: auditable cleaning and staged dataset integration, without labels."""
from __future__ import annotations

import argparse
from collections import Counter, defaultdict
import csv
from datetime import datetime, timezone
import hashlib
import json
from pathlib import Path
import re
import time
import unicodedata
import zipfile

import pyarrow as pa
import pyarrow.parquet as pq

S = pa.string()
SCHEMAS = {
    'papers': pa.schema([pa.field('arxiv_id', S, False), ('title', S),
                        ('authors', pa.list_(S)), ('submission_date', S),
                        ('revised_dates', pa.list_(S)), ('venue', S)]),
    'references': pa.schema([pa.field('arxiv_id', S, False), pa.field('ref_arxiv_id', S, False),
                            ('ref_title', S), ('ref_authors', pa.list_(S)),
                            ('ref_submission_date', S), ('ref_s2_id', S)]),
    'bibtex': pa.schema([pa.field('arxiv_id', S, False), pa.field('citation_key', S, False),
                        ('entry_type', S), ('title', S), ('authors', pa.list_(S)),
                        ('year', pa.int32()), ('doi', S), ('journal', S), ('url', S)]),
    'elements': pa.schema([pa.field('element_id', S, False), pa.field('arxiv_id', S, False),
                          ('kind', S), ('content', S)]),
    'hierarchy': pa.schema([pa.field('arxiv_id', S, False), ('version', pa.int32()),
                           pa.field('element_id', S, False), pa.field('parent_id', S, False)]),
    'predictions': pa.schema([pa.field('arxiv_id', S, False), ('partition', S),
                             pa.field('citation_key', S, False), ('groundtruth_arxiv_id', S),
                             *[(f'cand_{i}', S) for i in range(1, 6)]])
}


def arxiv_id(value):
    if not isinstance(value, str):
        return None
    value = value.strip()
    value = re.sub(r'^https?://(?:export\.)?arxiv\.org/(?:abs|pdf)/', '', value, flags=re.I)
    value = re.sub(r'^arxiv:\s*', '', value, flags=re.I)
    value = re.sub(r'\.pdf$', '', value, flags=re.I)
    value = re.sub(r'v[1-9]\d*$', '', value, flags=re.I)
    match = re.fullmatch(r'(\d{2})(\d{2})[.-](\d{4,5})', value)
    if match and 1 <= int(match[2]) <= 12:
        width = 5 if int(match[1]) >= 15 else 4
        return match[1] + match[2] + '.' + match[3].zfill(width)
    if re.fullmatch(r'[a-z][a-z0-9.-]*/\d{7}', value, flags=re.I):
        return value.lower()
    return None


def text(value):
    if value is None:
        return None
    if not isinstance(value, (str, int)):
        raise ValueError('text must be string or integer')
    value = unicodedata.normalize('NFC', str(value))
    return ' '.join(value.split()) or None


def bibtext(value):
    value = text(value)
    if not value:
        return None
    value = re.sub(r'\\(?:emph|textit|textbf|textrm|textnormal|mbox|url|href)\s*', '', value)
    value = re.sub(r'\\([&%_#])', r'\1', value)
    # Retain unknown LaTeX commands: removing them can change names or mathematics.
    return text(value.replace('{', '').replace('}', ''))


def authors(value, bib=False):
    if value is None:
        return None
    if bib:
        value = re.split(r'\s+and\s+(?=(?:[^{}]*\{[^{}]*\})*[^{}]*$)', value)
    if not isinstance(value, list):
        raise ValueError('authors must be list (or BibTeX author string)')
    out = []
    for author in value:
        name = bibtext(author) if bib else text(author)
        if name and name not in out:
            out.append(name)
    return out or None


def split_top(value):
    depth, quote, escape, start = 0, False, False, 0
    for i, char in enumerate(value):
        if escape:
            escape = False
            continue
        if char == '\\':
            escape = True
        elif char == '"' and depth == 0:
            quote = not quote
        elif not quote:
            if char == '{': depth += 1
            elif char == '}': depth -= 1
            elif char == ',' and depth == 0:
                yield value[start:i].strip()
                start = i + 1
    if depth or quote:
        raise ValueError('unbalanced BibTeX field')
    yield value[start:].strip()


def parse_bib(source):
    """Read brace-delimited generated BibTeX. Reject unsupported expressions explicitly."""
    pattern = re.compile(r'@([a-zA-Z]+)\s*\{')
    pos = 0
    while match := pattern.search(source, pos):
        depth, quote, escape = 1, False, False
        i = match.end()
        next_header = re.search(r'(?m)^[ \t]*@[a-zA-Z]+\s*\{', source[i:])
        limit = i + next_header.start() if next_header else len(source)
        while i < limit and depth:
            char = source[i]
            if escape: escape = False
            elif char == '\\': escape = True
            elif char == '"' and depth == 1: quote = not quote
            elif not quote:
                if char == '{': depth += 1
                elif char == '}': depth -= 1
            i += 1
        if depth:
            key = source[match.end():limit].split(',', 1)[0].strip()
            yield {'ID': key}, 'unclosed BibTeX entry; recovered at next line-start entry'
            pos = limit
            continue
        body, pos = source[match.end():i - 1], i
        kind = match[1].lower()
        if kind in {'comment', 'preamble', 'string'}:
            continue
        key = body.split(',', 1)[0].strip()
        try:
            if ',' not in body or not key:
                raise ValueError('missing citation key or fields')
            fields = {}
            for part in split_top(body.split(',', 1)[1]):
                if not part: continue
                name, sep, val = part.partition('=')
                name, val = name.strip().lower(), val.strip()
                if not sep or not re.fullmatch(r'[a-z][\w-]*', name):
                    raise ValueError('invalid BibTeX field')
                if val.startswith('{') and val.endswith('}'):
                    level, escaped = 0, False
                    for j, char in enumerate(val):
                        if escaped: escaped = False; continue
                        if char == '\\': escaped = True; continue
                        if char == '{': level += 1
                        elif char == '}': level -= 1
                        if level == 0 and j != len(val) - 1:
                            raise ValueError('unresolved BibTeX macro/concatenation')
                    val = val[1:-1]
                elif val.startswith('"') and val.endswith('"'):
                    val = val[1:-1]
                elif not re.fullmatch(r'\d+', val):
                    raise ValueError('unresolved BibTeX macro/concatenation')
                if name in fields and fields[name] != val:
                    raise ValueError('conflicting repeated field')
                fields[name] = val
            yield {'ID': key, 'ENTRYTYPE': kind, **fields}, None
        except ValueError as exc:
            yield {'ID': key}, str(exc)


class Source:
    def __init__(self, path):
        self.path = path
        self.zip = zipfile.ZipFile(path) if path.is_file() and zipfile.is_zipfile(path) else None
        self.names = self.zip.namelist() if self.zip else None

    def find(self, basename):
        if self.zip:
            return sorted(n for n in self.names if n.rsplit('/', 1)[-1] == basename)
        return sorted(p.relative_to(self.path).as_posix() for p in self.path.rglob(basename))

    def open(self, name):
        return self.zip.open(name) if self.zip else (self.path / name).open('rb')

    def read(self, name):
        with self.open(name) as f:
            return f.read().decode('utf-8-sig')


class Sink:
    def __init__(self, root, name, opts):
        self.name, self.opts, self.root = name, opts, root
        self.rows, self.writer, self.total, self.part_rows, self.part = [], None, 0, 0, 0
        self.closed = False
        (root / 'data' / name).mkdir(parents=True, exist_ok=True)
        self.clean = name in {'papers', 'references', 'bibtex'}

    def add(self, row):
        self.rows.append(row)
        if len(self.rows) >= self.opts['batch_rows']:
            self.flush()

    def flush(self):
        if not self.rows: return
        if self.writer is None:
            path = self.root / (f'{self.name}_clean.parquet' if self.clean else
                                f'data/{self.name}/part-{self.part:05d}.parquet')
            self.writer = pq.ParquetWriter(path, SCHEMAS[self.name], compression=self.opts['compression'])
        table = pa.Table.from_pylist(self.rows, schema=SCHEMAS[self.name])
        if any(table[c.name].null_count for c in SCHEMAS[self.name] if not c.nullable):
            raise ValueError(f'{self.name}: nonnullable field contains nulls')
        self.writer.write_table(table)
        self.total += len(self.rows)
        self.part_rows += len(self.rows)
        self.rows.clear()
        if not self.clean and self.part_rows >= self.opts['shard_rows']:
            self.writer.close()
            self.writer, self.part_rows = None, 0
            self.part += 1

    def close(self):
        if self.closed: return
        self.flush()
        if self.writer: self.writer.close()
        if self.clean:
            path = self.root / f'{self.name}_clean.parquet'
            if not path.exists():
                pq.write_table(pa.Table.from_pylist([], schema=SCHEMAS[self.name]), path)
            # Publish one canonical file through a hard link when supported.
            target = self.root / 'data' / self.name / 'part-00000.parquet'
            try: target.hardlink_to(path)
            except OSError:
                import shutil
                shutil.copyfile(path, target)
        self.closed = True


class Pipeline:
    def __init__(self, config_path):
        self.config = json.loads(config_path.read_text(encoding='utf-8-sig'))
        self.paths = {k: (config_path.parent / v).resolve() if v else None
                      for k, v in self.config['inputs'].items()}
        self.out = (config_path.parent / self.config['output_dir']).resolve()
        print(f'Config: {config_path}\nOutput: {self.out}', flush=True)
        self.opts = self.config['options']
        for key in ('batch_rows', 'shard_rows'):
            if type(self.opts[key]) is not int or self.opts[key] <= 0:
                raise ValueError(f'{key} must be positive integer')
        limit = self.opts.get('limit_publications')
        if limit is not None and (type(limit) is not int or limit <= 0):
            raise ValueError('limit_publications must be null or positive integer')
        for key in ('papers_root', 'ownership_csv', 'bibtex_source'):
            if not self.paths[key].exists(): raise FileNotFoundError(self.paths[key])
        if self.opts['export_fulltext'] and not self.paths['fulltext_source'].exists():
            raise FileNotFoundError(self.paths['fulltext_source'])
        if self.out.exists() and any(self.out.iterdir()):
            raise ValueError(f'Output is not empty: {self.out}. Choose a fresh output_dir in config.')
        self.out.mkdir(parents=True, exist_ok=True)
        self.counts, self.issues, self.examples = Counter(), Counter(), defaultdict(list)
        self.coverage = {}
        with self.paths['ownership_csv'].open(encoding='utf-8-sig', newline='') as f:
            self.owners = {}
            for row in csv.DictReader(f):
                aid = arxiv_id(row['arxiv_id'])
                if not aid: raise ValueError('invalid assigned arXiv ID')
                self.owners[aid] = row.get('members', row.get('member', ''))
        if limit: self.owners = dict(list(self.owners.items())[:limit])
        self.sinks = {name: Sink(self.out, name, self.opts) for name in ('papers', 'references', 'bibtex')}
        self.rejects = (self.out / 'quarantine.jsonl').open('w', encoding='utf-8')

    def issue(self, code, aid, detail=None, reject=False):
        self.issues[code] += 1
        obj = {'code': code, 'arxiv_id': aid, 'detail': detail}
        if len(self.examples[code]) < self.opts['issue_examples_per_code']: self.examples[code].append(obj)
        if reject: self.rejects.write(json.dumps(obj, ensure_ascii=False) + '\n')

    def merge(self, rows, key, aid, code):
        result = None
        for row in rows:
            if result is None: result = dict(row); continue
            for field, value in row.items():
                if value is None: continue
                if result.get(field) is None: result[field] = value
                elif result[field] != value:
                    self.issue(code, aid, {'key': key, 'field': field, 'variants': rows}, True)
                    return None
        return result

    def m1(self):
        for aid, owner in self.owners.items():
            folder = self.paths['papers_root'] / aid.replace('.', '-')
            cov = self.coverage[aid] = {'arxiv_id': aid, 'member': owner, 'metadata_ok': False,
                'reference_status': 'missing', 'references_complete': False, 'references_clean': 0,
                'bibtex_clean': 0, 'bibtex_status': 'missing', 'fulltext_versions': 0}
            try:
                meta = json.loads((folder / 'metadata.json').read_text(encoding='utf-8-sig'))
                if arxiv_id(meta.get('arxiv_id')) != aid or meta.get('status', 'found') != 'found':
                    raise ValueError('metadata ID/status mismatch')
                dates = meta.get('revised_dates')
                if dates is not None and not isinstance(dates, list): raise ValueError('invalid revised_dates')
                self.sinks['papers'].add({'arxiv_id': aid, 'title': text(meta.get('title')),
                    'authors': authors(meta.get('authors')), 'submission_date': text(meta.get('submission_date')),
                    'revised_dates': [text(x) for x in dates if text(x)] if dates is not None else None,
                    'venue': text(meta.get('venue'))})
                cov['metadata_ok'] = True
            except (OSError, ValueError, TypeError) as exc:
                self.issue('invalid_or_missing_metadata', aid, str(exc), True)
            try:
                data = json.loads((folder / 'references.json').read_text(encoding='utf-8-sig'))
                if arxiv_id(data.get('arxiv_id')) != aid: raise ValueError('reference publication ID mismatch')
                status = data.get('status', 'unknown')
                self.counts[f'm1_status_{status}'] += 1
                complete = data.get('verification', {}).get('complete') is True
                cov.update(reference_status=status, references_complete=complete)
                if status not in {'ok', 'empty_refs', 'not_in_s2'} or not complete:
                    self.issue('incomplete_references', aid, status)
                    if self.opts['exclude_incomplete_references']: continue
                groups = defaultdict(list)
                for ref in data.get('references', []):
                    if not ref.get('ref_arxiv_id'):
                        self.counts['references_without_arxiv_filtered'] += 1; continue
                    rid = arxiv_id(ref['ref_arxiv_id'])
                    if not rid:
                        self.issue('invalid_reference_arxiv_id', aid, ref, True); continue
                    groups[rid].append({'arxiv_id': aid, 'ref_arxiv_id': rid,
                        'ref_title': text(ref.get('ref_title')), 'ref_authors': authors(ref.get('ref_authors')),
                        'ref_submission_date': text(ref.get('ref_submission_date')), 'ref_s2_id': text(ref.get('ref_s2_id'))})
                for rid, group in groups.items():
                    self.counts['reference_duplicate_rows'] += len(group) - 1
                    row = self.merge(group, rid, aid, 'reference_field_conflict')
                    if row:
                        self.sinks['references'].add(row); cov['references_clean'] += 1
            except (OSError, ValueError, TypeError) as exc:
                self.issue('invalid_or_missing_references', aid, str(exc), True)

    def bibtex(self):
        source = Source(self.paths['bibtex_source'])
        for name in source.find('cite_map.json'):
            data = json.loads(source.read(name))
            aid = arxiv_id(data.get('publication_key'))
            if aid not in self.owners: continue
            cov = self.coverage[aid]
            cov['bibtex_status'] = 'empty' if not data.get('total_refs') else 'provisional'
            groups = defaultdict(list)
            bibname = name.rsplit('/', 1)[0] + '/' + data.get('bib_file', 'references.bib')
            if data.get('has_collision'): self.issue('m2_collision_flag', aid)
            failed_keys = set()
            for entry, error in parse_bib(source.read(bibname)):
                if error:
                    self.issue('bibtex_parse_error', aid, {'entry': entry, 'reason': error}, True)
                    if entry: failed_keys.add(entry['ID'])
                    continue
                key = entry['ID']
                ep = entry.get('eprint')
                if ep and arxiv_id(ep) == aid:
                    self.issue('eprint_equals_citing_paper', aid, key)
                # eprint is neither in the publication schema nor a reliable label.
                if key.startswith('IEEEexample:') or key.startswith('IEEEhowto:'):
                    self.issue('template_bibtex_entry', aid, key, True); continue
                year = entry.get('year', '').strip()
                yr = int(year) if re.fullmatch(r'\d{4}', year) and 1000 <= int(year) <= 2100 else None
                if year and yr is None: self.issue('invalid_bibtex_year', aid, {'key': key, 'value': year})
                doi = text(entry.get('doi'))
                if doi:
                    doi = re.sub(r'^(?:https?://(?:dx\.)?doi\.org/|doi:\s*)', '', doi, flags=re.I).lower()
                    if not re.fullmatch(r'10\.\d{4,9}/\S+', doi):
                        self.issue('invalid_doi', aid, {'key': key, 'value': doi}); doi = None
                row = {'arxiv_id': aid, 'citation_key': key, 'entry_type': entry['ENTRYTYPE'],
                       'title': bibtext(entry.get('title')), 'authors': authors(entry.get('author'), bib=True),
                       'year': yr, 'doi': doi, 'journal': bibtext(entry.get('journal') or entry.get('booktitle')),
                       'url': text(entry.get('url'))}
                for field in ('title', 'authors', 'year'):
                    if row[field] is None: self.issue('bibtex_missing_' + field, aid, key)
                groups[key].append(row)
            for key, group in groups.items():
                if key in failed_keys: continue
                self.counts['bibtex_duplicate_key_rows'] += len(group) - 1
                row = self.merge(group, key, aid, 'bibtex_key_conflict')
                if row:
                    self.sinks['bibtex'].add(row); cov['bibtex_clean'] += 1
            for old, new in data.get('cite_map', {}).items():
                if old != new:
                    self.issue('citation_alias_requires_m2_review', aid, {'source': old, 'target': new})
            # Preserve source keys. Do not apply unverified aliases or merge by similarity.
            if cov['bibtex_clean'] == 0: self.issue('publication_without_clean_bibtex', aid)

    def fulltext(self):
        if not self.opts['export_fulltext']: return
        reuse = self.paths.get('reuse_validated_fulltext_dir')
        if reuse:
            previous = json.loads((reuse / 'cleaning_report.json').read_text(encoding='utf-8'))
            validation = json.loads((reuse / 'output_validation.json').read_text(encoding='utf-8'))
            if previous['run_status'] == 'failed' or not validation['valid']:
                raise ValueError('Cannot reuse failed/unvalidated fulltext output')
            if previous['resolved_inputs']['fulltext_source'] != str(self.paths['fulltext_source']):
                raise ValueError('Reused fulltext must have the same configured source')
            if set(self.owners) != set(previous_scope := {
                r['arxiv_id'] for r in csv.DictReader((reuse / 'publication_coverage.csv').open(encoding='utf-8-sig'))}):
                raise ValueError('Reused fulltext must have exactly the same publication scope')
            import shutil
            for table in ('elements', 'hierarchy'):
                sink = self.sinks[table] = Sink(self.out, table, self.opts)
                for file in sorted((reuse / 'data' / table).glob('*.parquet')):
                    if not pq.read_schema(file).equals(SCHEMAS[table], check_metadata=False):
                        raise ValueError('Reused fulltext schema mismatch')
                    target = self.out / 'data' / table / file.name
                    try: target.hardlink_to(file)
                    except OSError: shutil.copyfile(file, target)
                    sink.total += pq.read_metadata(file).num_rows
                if sink.total != previous['table_rows'][table]:
                    raise ValueError('Reused fulltext row count mismatch')
            with (reuse / 'publication_coverage.csv').open(encoding='utf-8-sig') as f:
                for row in csv.DictReader(f):
                    self.coverage[row['arxiv_id']]['fulltext_versions'] = int(row['fulltext_versions'])
            for key in ('hierarchy_missing_id', 'hierarchy_missing_parent'):
                self.counts[key] = previous['counts'][key]
            self.counts['reused_validated_fulltext'] = 1
            return
        source = Source(self.paths['fulltext_source'])
        self.sinks.update({n: Sink(self.out, n, self.opts) for n in ('elements', 'hierarchy')})
        import sqlite3
        dbpath = self.out / '_validation.sqlite'
        db = sqlite3.connect(dbpath)
        # Disposable validation index; source data and Parquet outputs are separate.
        db.execute('PRAGMA journal_mode=OFF')
        db.execute('PRAGMA synchronous=OFF')
        db.execute('PRAGMA cache_size=-65536')
        db.execute('CREATE TABLE elements (aid TEXT, id TEXT, digest TEXT, PRIMARY KEY(aid,id)) WITHOUT ROWID')
        db.execute('CREATE TABLE edges (aid TEXT, id TEXT, parent TEXT, version INT, PRIMARY KEY(aid,version,id,parent)) WITHOUT ROWID')
        try:
            for table in ('elements', 'hierarchy'):
                files = source.find(table + '.jsonl')
                if len(files) != 1: raise ValueError(f'Expected one {table}.jsonl, got {len(files)}')
                with source.open(files[0]) as f:
                    for line_no, line in enumerate(f, 1):
                        if not line.strip(): continue
                        row = json.loads(line)
                        aid = arxiv_id(row.get('arxiv_id'))
                        if aid not in self.owners: continue
                        row['arxiv_id'] = aid
                        if table == 'elements':
                            identity = row.get('element_id')
                            if not identity: raise ValueError('empty element ID')
                            digest = hashlib.sha256(json.dumps(row, sort_keys=True, ensure_ascii=False).encode()).hexdigest()
                            try: db.execute('INSERT INTO elements VALUES (?,?,?)', (aid, identity, digest))
                            except sqlite3.IntegrityError:
                                old = db.execute('SELECT digest FROM elements WHERE aid=? AND id=?', (aid, identity)).fetchone()[0]
                                if old != digest: raise ValueError(f'Conflicting element {identity}')
                                self.counts['duplicate_elements_skipped'] += 1; continue
                        else:
                            if type(row.get('version')) is not int or row['version'] < 1:
                                raise ValueError('invalid hierarchy version')
                            values = (aid, row.get('element_id'), row.get('parent_id'), row['version'])
                            if not all(values[:3]): raise ValueError('null edge ID/parent')
                            try: db.execute('INSERT INTO edges VALUES (?,?,?,?)', values)
                            except sqlite3.IntegrityError:
                                self.counts['duplicate_edges_skipped'] += 1; continue
                        self.sinks[table].add(row)
                        if line_no % 1000000 == 0:
                            db.commit(); print(f'{table}: {line_no:,} input rows', flush=True)
                db.commit()
            for column in ('id', 'parent'):
                count = db.execute(f'SELECT COUNT(*) FROM edges h LEFT JOIN elements e ON h.aid=e.aid AND h.{column}=e.id WHERE e.id IS NULL').fetchone()[0]
                self.counts[f'hierarchy_missing_{column}'] = count
                if count: raise ValueError(f'{count} orphan hierarchy {column} references')
            for aid, count in db.execute('SELECT aid, COUNT(DISTINCT version) FROM edges GROUP BY aid'):
                self.coverage[aid]['fulltext_versions'] = count
        finally:
            db.close()
            dbpath.unlink(missing_ok=True)

    def finish(self, started, error=None):
        for sink in self.sinks.values(): sink.close()
        self.rejects.close()
        rows = list(self.coverage.values())
        for r in rows:
            r['labeling_candidate'] = bool(r['metadata_ok'] and r['references_complete'] and
                                           r['reference_status'] == 'ok' and r['references_clean'] and r['bibtex_clean'])
        if rows:
            with (self.out / 'publication_coverage.csv').open('w', encoding='utf-8-sig', newline='') as f:
                writer = csv.DictWriter(f, fieldnames=list(rows[0])); writer.writeheader(); writer.writerows(rows)
        report = {'run_status': 'failed' if error else 'completed_with_quality_findings',
                  'error': error, 'created_at_utc': datetime.now(timezone.utc).isoformat(),
                  'wall_seconds': round(time.perf_counter() - started, 2),
                  'resolved_inputs': {k: str(v) if v else None for k, v in self.paths.items()},
                  'configuration': self.config,
                  'assigned_publications': len(self.owners),
                  'table_rows': {n: s.total for n, s in self.sinks.items()},
                  'counts': dict(self.counts), 'quality_issue_counts': dict(self.issues),
                  'issue_examples': dict(self.examples),
                  'labeling_candidate_publications': sum(r['labeling_candidate'] for r in rows),
                  'bibtex_missing_or_empty_publications': sum(not r['bibtex_clean'] for r in rows),
                  'readiness': {'m2_canonical_key_reconciliation': 'pending',
                                'human_labels': 'not_created', 'predictions': 'pending',
                                'final_publication_ready': False},
                  'limitations': ['Source bibliography extraction and cross-version deduplication remain M2 responsibilities.',
                                  'Missing title/authors and suspicious eprint are reported; no fields or labels are invented.',
                                  'labeling_candidate means nonempty usable inputs, not verified semantic correctness.',
                                  'Fulltext version counts derive from edges; empty versions remain in original coverage.jsonl.']}
        (self.out / 'cleaning_report.json').write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding='utf-8')
        (self.out / 'resolved_config.json').write_text(json.dumps(self.config, indent=2), encoding='utf-8')
        # Five table configs are draft integration, not a claim of final six-table completion.
        card = ['---', 'configs:']
        for name, sink in self.sinks.items():
            if sink.total:
                card.extend([f'- config_name: {name}', '  data_files:', '  - split: train', f'    path: data/{name}/*.parquet'])
        card.extend(['---', '', 'Draft M3 cleaned dataset. See cleaning_report.json for coverage and exclusions.',
                     'The dataset configuration split `train` is a storage split, not an ML partition.',
                     'Predictions and validated labels are not included. Not ready for final submission.'])
        (self.out / 'README.md').write_text('\n'.join(card) + '\n', encoding='utf-8')
        print(json.dumps({k: report[k] for k in ('run_status', 'wall_seconds', 'table_rows',
                                                'labeling_candidate_publications', 'bibtex_missing_or_empty_publications')}, indent=2))

    def run(self):
        started, error = time.perf_counter(), None
        try:
            print('Cleaning M1 metadata and references...', flush=True); self.m1()
            self.sinks['papers'].close()
            self.sinks['references'].close()
            print('Cleaning M2 bibliography...', flush=True); self.bibtex()
            self.sinks['bibtex'].close()
            print('Integrating fulltext tables...', flush=True); self.fulltext()
            if self.paths.get('predictions_parquet'):
                self.issue('predictions_integration_deferred', None,
                           'Predictions require final label and split validation; input was not imported.')
        except Exception as exc:
            error = f'{type(exc).__name__}: {exc}'
            raise
        finally:
            self.finish(started, error)


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--config', type=Path, default=Path(__file__).with_name('config.json'))
    args = parser.parse_args()
    Pipeline(args.config.resolve()).run()
