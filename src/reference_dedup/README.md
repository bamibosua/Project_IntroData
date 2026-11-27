# Reference extraction and deduplication pipeline

## Expected input layout

```text
parsed_structure/
├── 2511-11709/
│   ├── v1/merged.tex
│   └── v2/merged.tex
└── 2511-11710/
    └── v1/merged.tex
```

Each paper is processed independently. References from different versions of the
same paper are deduplicated together; references from different papers are not
merged.

## Run

Python 3.10+ is recommended. The current implementation uses only the Python
standard library.

From the project root:

```bash
python path/to/reference_pipeline/main.py \
  --root "Project_IntroData/parsed_structure/parsed_structure" \
  --output "reference_output"
```

On PowerShell, enter it on one line if line continuation is inconvenient:

```powershell
python path\to\reference_pipeline\main.py --root "Project_IntroData/parsed_structure/parsed_structure" --output "reference_output"
```

## Output

```text
reference_output/
├── 2511-11709/
│   ├── references.json
│   ├── references.csv
│   ├── references.bib
│   └── citation_key_mapping.json
├── all_references.json
├── all_references.csv
├── all_references.bib
└── all_citation_key_mappings.json
```

## Important limitations

- `\\bibitem` contents are free-form LaTeX text. The parser uses heuristics;
  fields it cannot identify confidently are left empty, and `raw_content` is kept.
- The parser currently labels generated BibTeX entries as `@misc`, because a
  bibliography text entry does not always reveal whether the source is an
  article, conference paper, book, etc. Improve `entry_type` inference only
  when there is reliable evidence.
- Automatic deduplication uses DOI, arXiv ID, or exact normalized title.
  Similar-but-not-identical titles are not automatically merged.
- Conflicting metadata is retained in `metadata_conflicts` for review.
- Before using the generated citation-key mapping to rewrite LaTeX citations,
  review the mapping and ensure that all citation commands used by the project
  are covered.
- Test on a small sample of papers and manually inspect the output before
  running the full dataset.

# Pipeline details

- First of all, the pipeline parse the references from the LateX source file by `reference_pipeline\reference_parser.py`. The output is a dictionary of references, where each reference is represented as a dictionary of its fields.
- Then, the pipeline normalizes the parsed references by `reference_pipeline\reference_normalization.py`. The normalization process includes:
  - Normalizing the title by removing punctuation, converting to lowercase, and removing stopwords.
  - Normalizing the author names by converting to lowercase and removing punctuation.
  - Normalizing the journal name by converting to lowercase and removing punctuation.
  - Normalizing the year by converting to an integer.
- After normalization, the system will be extracted the references from di
