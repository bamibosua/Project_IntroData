"""References package for extracting citation edges from Semantic Scholar API."""

from .client import SemanticScholarClient, RateLimitException
from .rate_limiter import SemanticScholarRateLimiter, default_rate_limiter
from .extractor import (
    extract_arxiv_references,
    get_arxiv_id_from_external_ids,
    normalize_arxiv_id,
)
from .crawler import ReferenceCrawler, load_arxiv_ids_from_file, prompt_api_key

__all__ = [
    # Client
    "SemanticScholarClient",
    "RateLimitException",
    # Rate Limiter
    "SemanticScholarRateLimiter",
    "default_rate_limiter",
    # Extractor
    "extract_arxiv_references",
    "get_arxiv_id_from_external_ids",
    "normalize_arxiv_id",
    # Crawler
    "ReferenceCrawler",
    "load_arxiv_ids_from_file",
    "prompt_api_key",
]
