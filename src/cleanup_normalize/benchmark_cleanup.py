"""Reproducible CPU benchmark; outputs JSON suitable for report generation.

Run from src: python -m cleanup_normalize.benchmark_cleanup --help
An optional baseline may be supplied for comparison with an older implementation.
"""
from __future__ import annotations

import argparse
from collections import Counter
import gc
import hashlib
import importlib.util
import json
from pathlib import Path
import platform
import statistics
import subprocess
import sys
import tempfile
import time
import tracemalloc

from . import cleanup_leaves as current


def peak_rss_bytes():
    if sys.platform == 'win32':
        import ctypes
        from ctypes import wintypes

        class MemoryCounters(ctypes.Structure):
            _fields_ = [('cb', wintypes.DWORD), ('PageFaultCount', wintypes.DWORD),
                        *[(name, ctypes.c_size_t) for name in (
                            'PeakWorkingSetSize', 'WorkingSetSize', 'QuotaPeakPagedPoolUsage',
                            'QuotaPagedPoolUsage', 'QuotaPeakNonPagedPoolUsage',
                            'QuotaNonPagedPoolUsage', 'PagefileUsage', 'PeakPagefileUsage')]]

        counters = MemoryCounters()
        counters.cb = ctypes.sizeof(counters)
        kernel = ctypes.WinDLL('kernel32', use_last_error=True)
        psapi = ctypes.WinDLL('psapi', use_last_error=True)
        kernel.GetCurrentProcess.restype = wintypes.HANDLE
        psapi.GetProcessMemoryInfo.argtypes = [wintypes.HANDLE, ctypes.POINTER(MemoryCounters), wintypes.DWORD]
        if not psapi.GetProcessMemoryInfo(kernel.GetCurrentProcess(), ctypes.byref(counters), counters.cb):
            raise ctypes.WinError(ctypes.get_last_error())
        return counters.PeakWorkingSetSize
    import resource
    value = resource.getrusage(resource.RUSAGE_SELF).ru_maxrss
    return value if sys.platform == 'darwin' else value * 1024


def load_module(path):
    spec = importlib.util.spec_from_file_location('cleanup_baseline', path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def main():
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument('--input-root', type=Path, default=Path('parsed_structure'))
    p.add_argument('--baseline', type=Path, help='Optional older implementation for comparison')
    p.add_argument('--require-equivalent', action='store_true',
                   help='Fail on semantic differences (only for performance-only changes)')
    p.add_argument('--sample', type=int, default=100)
    p.add_argument('--repeats', type=int, default=3)
    p.add_argument('--report', type=Path, default=Path('cleanup_normalize/benchmark_results.json'))
    p.add_argument('--memory-worker', choices=['baseline', 'optimized'], help=argparse.SUPPRESS)
    args = p.parse_args()
    if args.sample < 1 or args.repeats < 1:
        p.error('sample and repeats must be positive')
    baseline = load_module(args.baseline) if args.baseline else current
    all_pairs = current.discover(args.input_root)
    n = min(args.sample, len(all_pairs))
    pairs = [all_pairs[i * len(all_pairs) // n] for i in range(n)]
    if args.memory_worker:
        module = baseline if args.memory_worker == 'baseline' else current
        with tempfile.TemporaryDirectory(prefix='cleanup_rss_') as tmp:
            module.run_pairs(pairs, Path(tmp))
        print(peak_rss_bytes())
        return
    data = [(m.read_text(encoding='utf-8') if m else None,
             json.loads(o.read_text(encoding='utf-8'))) for o, m in pairs]
    statuses = Counter(o['status'] for _, o in data)
    errors = []
    eligible = []
    leaves = 0
    digest = hashlib.sha256()
    equivalent_versions = 0
    changed_versions = []
    for (o_path, _), (merged, outline) in zip(pairs, data):
        values = []
        for module in (baseline, current):
            try:
                values.append(module.transform(merged, outline))
            except (ValueError, KeyError, TypeError, OSError) as e:
                values.append((type(e).__name__, str(e)))
        if values[0] != values[1]:
            changed_versions.append(str(o_path))
            if args.require_equivalent:
                raise AssertionError(f'Output mismatch: {o_path}')
        else:
            equivalent_versions += 1
        if all(isinstance(value, dict) for value in values):
            eligible.append((merged, outline))
            leaves += values[1]['total_leaves']
            digest.update(json.dumps(values[1], sort_keys=True).encode())
        else:
            errors.append({'outline': str(o_path), 'error': values[0]})
    metrics = {}
    for name in ('baseline', 'optimized'):
        metrics[name] = {'transform_seconds': [], 'batch_seconds': []}
    # Alternate order to reduce warm-cache and thermal bias. Trace separately:
    # tracemalloc changes CPU timing substantially.
    for repeat in range(args.repeats):
        order = [('baseline', baseline), ('optimized', current)]
        if repeat % 2:
            order.reverse()
        for name, module in order:
            gc.collect()
            start = time.perf_counter()
            for merged, outline in eligible:
                module.transform(merged, outline)
            metrics[name]['transform_seconds'].append(time.perf_counter() - start)
            with tempfile.TemporaryDirectory(prefix='cleanup_bench_') as tmp:
                start = time.perf_counter()
                summary = module.run_pairs(pairs, Path(tmp))
                metrics[name]['batch_seconds'].append(time.perf_counter() - start)
                metrics[name]['output_bytes'] = sum(f.stat().st_size for f in Path(tmp).rglob('*') if f.is_file())
                metrics[name]['failed_versions'] = summary['failed_versions']
                metrics[name]['publications'] = summary['publications']
            print(f'{name} repeat {repeat + 1} complete', flush=True)
    for name, module in [('baseline', baseline), ('optimized', current)]:
        gc.collect()
        with tempfile.TemporaryDirectory(prefix='cleanup_memory_') as tmp:
            tracemalloc.start()
            module.run_pairs(pairs, Path(tmp))
            _, peak = tracemalloc.get_traced_memory()
            tracemalloc.stop()
        metrics[name]['peak_python_allocated_bytes'] = peak
        worker = subprocess.run([
            sys.executable, '-m', 'cleanup_normalize.benchmark_cleanup',
            '--input-root', str(args.input_root),
            *(['--baseline', str(args.baseline)] if args.baseline else []),
            '--sample', str(args.sample), '--memory-worker', name],
            check=True, text=True, capture_output=True)
        metrics[name]['peak_process_rss_bytes'] = int(worker.stdout.strip().splitlines()[-1])
        for key in ('transform', 'batch'):
            metrics[name][key + '_median_seconds'] = statistics.median(metrics[name][key + '_seconds'])
        metrics[name]['batch_seconds_per_publication'] = (metrics[name]['batch_median_seconds'] / metrics[name]['publications']
                                                        if metrics[name]['publications'] else None)
    report = {'python': platform.python_version(), 'platform': platform.platform(),
              'processor': platform.processor(), 'corpus_versions': len(all_pairs),
              'sample_versions': n, 'sample_statuses': dict(statuses),
              'successful_or_empty': len(eligible), 'leaves': leaves,
              'baseline_provided': args.baseline is not None,
              'equivalent_versions': equivalent_versions, 'changed_versions': changed_versions,
              'output_sha256': digest.hexdigest(),
              'rejected_inputs': errors, 'metrics': metrics,
              'transform_speedup': metrics['baseline']['transform_median_seconds'] / metrics['optimized']['transform_median_seconds'],
              'batch_speedup': metrics['baseline']['batch_median_seconds'] / metrics['optimized']['batch_median_seconds'],
              'notes': ['Deterministic evenly spaced sample in sorted input paths.',
                        'Batch includes read, transform, JSON serialization and write; excludes discovery.',
                        'Python allocations traced separately; peak process RSS measured in fresh subprocesses.',
                        'Local CPU benchmark; final milestone benchmark requires Colab CPU.',
                        'Input corpus is unchanged; temporary output directories are removed.']}
    args.report.parent.mkdir(parents=True, exist_ok=True)
    args.report.write_text(json.dumps(report, indent=2), encoding='utf-8')
    print(json.dumps(report, indent=2))


if __name__ == '__main__':
    main()