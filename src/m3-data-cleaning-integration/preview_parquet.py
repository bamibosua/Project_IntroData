"""Preview Parquet or stream all rows to CSV without loading the whole dataset."""
import argparse
import csv
import json
from pathlib import Path
import sys

import pyarrow.parquet as pq


def positive(value):
    number = int(value)
    if number < 1:
        raise argparse.ArgumentTypeError('rows must be positive')
    return number


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('file', type=Path)
    scope = parser.add_mutually_exclusive_group()
    scope.add_argument('--rows', type=positive, default=10)
    scope.add_argument('--all', action='store_true', help='Export all rows; requires --csv')
    parser.add_argument('--csv', type=Path, help='Save preview rows or --all rows as UTF-8 CSV for Excel')
    args = parser.parse_args()
    if args.all and not args.csv:
        parser.error('--all requires --csv')
    if hasattr(sys.stdout, 'reconfigure'):
        sys.stdout.reconfigure(encoding='utf-8')
    parquet = pq.ParquetFile(args.file)
    print(f'File: {args.file}\nTotal rows: {parquet.metadata.num_rows:,}')
    print(f'Schema:\n{parquet.schema_arrow}\nPreview:')
    batch = next(parquet.iter_batches(batch_size=args.rows), None)
    rows = batch.to_pylist() if batch is not None else []
    print(json.dumps(rows, ensure_ascii=False, indent=2))
    if args.csv:
        if args.csv.resolve() == args.file.resolve():
            parser.error('CSV output must differ from the input file')
        if args.csv.exists():
            parser.error('CSV output already exists; choose a new filename')
        with args.csv.open('x', encoding='utf-8-sig', newline='') as stream:
            writer = csv.DictWriter(stream, fieldnames=parquet.schema_arrow.names)
            writer.writeheader()
            batches = (batch.to_pylist() for batch in parquet.iter_batches(batch_size=10000)) if args.all else [rows]
            count = 0
            for chunk in batches:
                writer.writerows({k: json.dumps(v, ensure_ascii=False) if isinstance(v, list) else v
                                 for k, v in row.items()} for row in chunk)
                count += len(chunk)
        print(f'Saved {count:,} rows to {args.csv}')


if __name__ == '__main__':
    main()
