# Cleanup and normalization

Run from `src` using Python 3.12 (standard library only):

```powershell
python -m cleanup_normalize.cleanup_leaves --input-root parsed_structure --output-dir cleaned_structure
python -m unittest cleanup_normalize.test_cleanup_leaves -v
python -m cleanup_normalize.benchmark_cleanup --input-root parsed_structure --baseline cleanup_normalize/test/baseline_cleanup_leaves.py --sample 100 --repeats 3
```

The input directory is searched recursively, including the extra `parsed_structure/parsed_structure` nesting in the supplied corpus. Original inputs are never modified. Outputs are `publication_key/vN/cleaned_nodes.json` and a batch `summary.json`. Running cleanup again replaces existing outputs in the chosen output directory.

## Performance changes

- Consume ordinary prose and comment-free text in runs instead of appending one character at a time.
- Reject non-math characters before checking math delimiters and environments.
- Compile frequently used regular expressions once and match at an offset instead of copying the remaining text for each match.
- Restore protected sentence tokens in one substitution pass instead of scanning every sentence for every token.
- Retain only summary fields between documents in `run_pairs`; full trees are written and released. Memory no longer grows with all previously processed nodes.

The frozen original implementation is `test/baseline_cleanup_leaves.py`; it is only used for comparisons. The benchmark validates complete dictionary equality (or equal exception type/message) before timing. This is a performance refactor preserving existing cleanup behavior, not evidence that every source is semantically parsed correctly.

## Benchmark method

The default sample selects 100 equally spaced paths from the sorted corpus. Three repeats alternate implementation order. `benchmark_results.json` records individual measurements, medians, output sizes, input statuses, errors, and a digest of the successful output. Temporary output is removed automatically.

`transform_seconds` excludes file reads and output writes. `batch_seconds` includes reading, transformation, JSON serialization and writing, but excludes recursive discovery. Filesystem caches may be warm. The average per publication uses the number of successfully processed or explicitly empty publications in the sample. Python allocation peaks are measured separately using `tracemalloc`, whose overhead is excluded from runtime. Peak process RSS is measured in fresh subprocesses, using Windows PeakWorkingSetSize or Unix ru_maxrss, including interpreter and discovery overhead.

These are local CPU results for the cleanup stage, not end-to-end measurements of the full milestone pipeline. Repeat on **Google Colab CPU** for the final report, as required by the [milestone statement](https://hcmus-cs-ta.github.io/lab-book/intro-ds/statement/parsing/). Use the JSON measurements to generate report figures during Jupyter Book compilation. Use a larger sample or `--sample 27468` for the complete supplied corpus; full runs also validate every selected output against the baseline and can take considerably longer.

Measured on Windows 11, Python 3.12.10, local CPU, 100 sampled versions, three repeats:

| Measurement | Original | Optimized |
| --- | ---: | ---: |
| Transform median | 9.435 s | 2.959 s |
| Batch median, read + process + write | 9.907 s | 3.743 s |
| Batch time per successful/empty publication | 0.1208 s | 0.04565 s |
| Peak process RSS, separate process | 129.67 MiB | 97.19 MiB |
| Peak traced Python allocations | 66.23 MiB | 38.42 MiB |
| Output bytes | 29,364,222 | 29,364,222 |

Transform speedup: **3.19x**; batch speedup: **2.65x**. All 100 selected versions had equivalent results or equivalent errors. There were 72 successful TeX versions, 10 empty versions, 8 rejected partial versions and 10 failed upstream versions, with 38,768 generated leaves. Timings include error handling in batch mode. No full-corpus runtime is inferred from this sample.

## Review findings and scope

The supplied corpus contains 27,468 outlines: 20,499 `ok`, 3,231 `empty`, 2,371 `partial`, and 1,367 `failed`. Current behavior rejects `partial` and `failed`, and records them as failures rather than empty versions. Inspect upstream diagnostics and decide with the parsing owner whether partial trees are usable; do not silently include them as successful coverage. Full-corpus counts here are an inventory, not a completed full-corpus cleanup run.

The module handles formatting removal, math delimiters, sentences, lists and floats, and preserves citation/reference macros for downstream processing. Reference extraction, citation-key rewriting, field merging and cross-version element deduplication belong to later stages; this module alone does not satisfy those milestone requirements.

Existing semantic limitations remain: cleanup is conservative for unknown commands; generic grouping braces and custom style macros may survive. Math nested in float content does not receive the same extraction as body math, and dollar-delimited formulas passed directly through `normalize_text` do not have full opaque-math protection from the prose formatting scanner. Verbatim and math can still be affected by global whitespace collapsing in prose normalization. Complex or malformed LaTeX needs a dedicated semantic regression corpus before changing these behaviors. Output equivalence intentionally keeps these semantics stable in this optimization.

Input/output data, benchmark JSON, and generated bytecode should stay out of the source-only submission repository.
