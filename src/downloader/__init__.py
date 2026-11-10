"""Full-Text Source Downloader (Milestone 1).

Modules:
  config     constants and default paths
  utils      small helpers (byte formatting, path sanitizing, TeX sniffing)
  extractor  in-memory archive inspection, writes only .tex / .bib
  client     HTTP client with Keep-Alive, rate limiting and cooldown
  index      loading of data/input/papers_index.json
  pipeline   per-paper work units (fetch versions, extract, build records)
  audit      post-crawl verification and recovery of missed versions
  stats      storage metrics, summary file and console report
  runner     orchestration (`download_corpus`)
  cli        argument parsing (`python -m src.downloader`)
"""

from .client import FastArxivClient
from .extractor import extract_tex_and_bib_from_bytes
from .index import load_papers_index
from .runner import download_corpus

__all__ = ["FastArxivClient", "extract_tex_and_bib_from_bytes", "load_papers_index", "download_corpus"]
