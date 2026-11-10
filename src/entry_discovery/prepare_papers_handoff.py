#!/usr/bin/env python3
"""Build a per-paper handoff folder tree and fast ID-to-versions index.

Reads Entry Discovery's discovered_papers_with_revised_dates.json.
Only creates metadata.json and empty version directories; it never writes TeX,
BibTeX or reference data. Safe to rerun: existing downstream files survive.
Python 3.10+, standard library only.
"""
from __future__ import annotations

import argparse
import json
import os
import re
import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[2]
PAPER_ID = re.compile(r"\d{4}\.\d{4,5}\Z")
VERSION_ID = re.compile(r"v([1-9]\d*)\Z")


def read_papers(path: Path) -> list[dict]:
    if not path.is_file():
        raise ValueError(f"Cannot find Entry Discovery JSON: {path}")
    with path.open('r', encoding='utf-8') as file:
        data = json.load(file)
    if isinstance(data, dict) and isinstance(data.get('papers'), list):
        data = data['papers']
    elif isinstance(data, dict):
        # Optional dictionary format {arxiv_id: metadata}.
        data = [dict(value, arxiv_id=key) for key, value in data.items()
                if isinstance(value, dict)]
    if not isinstance(data, list):
        raise ValueError('Expected a JSON list of paper objects (or {"papers": [...]})')
    return data


def normalized_versions(paper: dict) -> tuple[str, list[str]]:
    pid = paper.get('arxiv_id')
    if not isinstance(pid, str) or not PAPER_ID.fullmatch(pid):
        raise ValueError(f"Invalid arxiv_id: {pid!r}")
    latest = paper.get('latest_version')
    if type(latest) is not int or latest < 1:
        raise ValueError(f'{pid}: invalid latest_version {latest!r}')
    expected = [f'v{i}' for i in range(1, latest + 1)]
    supplied = paper.get('versions')
    if supplied is not None:
        if not isinstance(supplied, list) or supplied != [f'{pid}{v}' for v in expected]:
            raise ValueError(f'{pid}: versions mismatch latest_version')
    return pid, expected


def atomic_write_json(path: Path, value: object) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    # Each output path gets a temporary file in the same directory.
    temp = path.with_name(path.name + f'.{os.getpid()}.tmp')
    try:
        with temp.open('w', encoding='utf-8', newline='\n') as file:
            json.dump(value, file, ensure_ascii=False, indent=2)
            file.write('\n')
        os.replace(temp, path)
    finally:
        if temp.exists():
            temp.unlink()


def update_index(index_file: Path, updates: dict[str, list[str]]) -> None:
    """Merge a validated batch into the index; entry is the only writer."""
    index = json.loads(index_file.read_text(encoding='utf-8')) if index_file.exists() else {}
    if not isinstance(index, dict):
        raise ValueError(f'Expected an index JSON object: {index_file}')
    index.update(updates)
    atomic_write_json(index_file, index)


def same_json(path: Path, value: object) -> bool:
    if not path.exists():
        return False
    try:
        with path.open('r', encoding='utf-8') as file:
            return json.load(file) == value
    except (json.JSONDecodeError, UnicodeDecodeError):
        return False


def build_handoff(papers: list[dict], papers_dir: Path, index_file: Path,
                  *, overwrite_metadata: bool = False, limit: int | None = None,
                  dry_run: bool = False) -> dict:
    selected = papers[:limit] if limit is not None else papers
    index: dict[str, list[str]] = {}
    validated: list[tuple[str, list[str], dict]] = []
    incomplete_revisions = 0
    for paper in selected:
        if not isinstance(paper, dict):
            raise ValueError('Every paper record must be a JSON object')
        if paper.get('status', 'found') != 'found':
            raise ValueError(f"Paper not found/invalid: {paper.get('arxiv_id')}")
        pid, versions = normalized_versions(paper)
        if pid in index:
            raise ValueError(f'Duplicate paper ID: {pid}')
        index[pid] = versions
        validated.append((pid, versions, paper))
        revised = paper.get('revised_dates')
        if not isinstance(revised, list) or len(revised) != len(versions) - 1 or any(not d for d in revised):
            incomplete_revisions += 1

    if papers_dir.resolve() == index_file.resolve():
        raise ValueError('Index file cannot be the same path as papers folder')
    if index_file.parent.resolve() == papers_dir.resolve():
        raise ValueError('Keep index JSON next to papers/, not inside it')
    if len(validated) == 0:
        raise ValueError('No valid papers to export')

    # Preflight: don't discover a metadata conflict after creating thousands of dirs.
    if not dry_run and not overwrite_metadata:
        for pid, _, paper in validated:
            target = papers_dir / pid.replace('.', '-') / 'metadata.json'
            if target.exists() and not same_json(target, paper):
                raise ValueError(f'Existing metadata differs: {target}. '
                                 'Run with --overwrite-metadata ONLY if you want to refresh metadata; '
                                 'TeX and references are never overwritten.')

    metadata_new, metadata_same, metadata_updated, dirs_created = 0, 0, 0, 0
    for number, (pid, versions, paper) in enumerate(validated, 1):
        paper_dir = papers_dir / pid.replace('.', '-')
        meta = paper_dir / 'metadata.json'
        if not dry_run:
            if not paper_dir.exists():
                dirs_created += 1
            paper_dir.mkdir(parents=True, exist_ok=True)
            for version in versions:
                (paper_dir / version).mkdir(exist_ok=True)
            if meta.exists():
                if same_json(meta, paper):
                    metadata_same += 1
                else:
                    atomic_write_json(meta, paper)
                    metadata_updated += 1
            else:
                atomic_write_json(meta, paper)
                metadata_new += 1
        if number % 1000 == 0 or number == len(validated):
            print(f'[HANDOFF] {number}/{len(validated)} papers', flush=True)

    if not dry_run:
        atomic_write_json(index_file, index)

    result = {
        'total_papers': len(validated),
        'total_versions': sum(len(vs) for vs in index.values()),
        'papers_missing_complete_revised_dates': incomplete_revisions,
        'new_paper_directories': dirs_created,
        'new_metadata_files': metadata_new,
        'unchanged_metadata_files': metadata_same,
        'refreshed_metadata_files': metadata_updated,
        'papers_dir': str(papers_dir),
        'index_file': str(index_file),
        'dry_run': dry_run,
    }
    print(json.dumps(result, indent=2, ensure_ascii=False))
    if incomplete_revisions:
        print(f'WARNING: {incomplete_revisions} paper(s) have incomplete revised_dates; '
              'index/folders still reflect their known latest_version.', file=sys.stderr)
    return result


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(description='Create per-paper folders + ID/versions JSON index for teammates')
    p.add_argument('--input', type=Path, required=True,
                   help='discovered_papers_with_revised_dates.json from Entry Discovery')
    p.add_argument('--papers-dir', type=Path, default=PROJECT_ROOT / 'data' / 'output' / 'papers')
    p.add_argument('--index-file', type=Path, default=PROJECT_ROOT / 'data' / 'input' / 'papers_index.json')
    p.add_argument('--limit', type=int, help='Only first N papers, for a small trial')
    p.add_argument('--dry-run', action='store_true', help='Validate and count without writing files')
    p.add_argument('--overwrite-metadata', action='store_true',
                   help='Refresh existing metadata.json; NEVER touches .tex/.bib/references.json')
    args = p.parse_args(argv)
    if args.limit is not None and args.limit <= 0:
        p.error('--limit must be positive')
    try:
        papers = read_papers(args.input)
        build_handoff(papers, args.papers_dir, args.index_file,
                      overwrite_metadata=args.overwrite_metadata,
                      limit=args.limit, dry_run=args.dry_run)
    except (ValueError, OSError, json.JSONDecodeError) as exc:
        p.error(str(exc))
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
