"""Diagnose HTTP 429 with controlled requests and no automatic retries.

From the repository root:
    python src/references/diagnose_429.py data/input/papers_index.json
    python src/references/diagnose_429.py data/input/papers_index.json --sizes 10,50,200 --gap 15

Send one GET, each configured batch size, then another GET. Inspect status,
response body, and rate-limit headers; HTTP 429 alone does not establish key validity.
"""

import argparse
import sys
import time

import requests

import config
from crawl_references import load_papers
from s2_client import BASE, BATCH_FIELDS

TIMEOUT = 120


def send(label, method, url, headers, gap, **kw):
    time.sleep(gap)
    started_at = time.time()
    try:
        response = requests.request(method, url, headers=headers, timeout=TIMEOUT, **kw)
    except requests.RequestException as e:
        print(f"{label:<28} NETWORK ERROR: {e}")
        return None
    elapsed_sec = time.time() - started_at
    rate_limit_headers = {
        k: v
        for k, v in response.headers.items()
        if k.lower() == "retry-after" or k.lower().startswith("x-ratelimit")
    }
    line = f"{label:<28} HTTP {response.status_code} | {elapsed_sec:5.1f}s | {len(response.content) / 1e6:6.2f} MB"
    if response.status_code != 200:
        line += f" | body={response.text[:150]!r}"
    if rate_limit_headers:
        line += f" | header={rate_limit_headers}"
    print(line)
    return response.status_code


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("papers_file")
    parser.add_argument(
        "--sizes",
        default="10,50,200",
        help="comma-separated batch sizes to test",
    )
    parser.add_argument(
        "--gap",
        type=float,
        default=10,
        help="seconds to wait before each request (default: 10)",
    )
    args = parser.parse_args()

    config.load_env()
    key = config.get_api_key()
    headers = {"User-Agent": "iDS-arxiv-project/0.1 (diagnose)"}
    if key:
        headers["x-api-key"] = key
        print(f"API key loaded ({len(key)} characters).")
    else:
        print("No API key found; using public access.")

    paper_ids = [
        p["arxiv_id"]
        for p in load_papers(args.papers_file)
        if p.get("status", "found") == "found"
    ]
    batch_sizes = [int(x) for x in args.sizes.split(",") if x.strip()]
    if len(paper_ids) < max(batch_sizes):
        sys.exit(
            f"Only {len(paper_ids)} papers available; need at least {max(batch_sizes)}."
        )

    print(
        f"Wait {args.gap}s before each request. No retries; one request per line.\n"
    )
    first_paper_id = paper_ids[0]
    send(
        "GET one paper",
        "GET",
        f"{BASE}/ARXIV:{first_paper_id}",
        headers,
        0,
        params={"fields": "title,referenceCount"},
    )
    for batch_size in batch_sizes:
        send(
            f"POST batch {batch_size} papers",
            "POST",
            f"{BASE}/batch",
            headers,
            args.gap,
            params={"fields": BATCH_FIELDS},
            json={"ids": ["ARXIV:" + i for i in paper_ids[:batch_size]]},
        )
    send(
        "GET one paper (after batch)",
        "GET",
        f"{BASE}/ARXIV:{first_paper_id}",
        headers,
        args.gap,
        params={"fields": "title,referenceCount"},
    )


if __name__ == "__main__":
    main()
