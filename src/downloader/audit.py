"""Post-crawl verification: find versions that were missed and download them again."""

from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
import time

from .client import FastArxivClient
from .pipeline import extract_paper_to_existing_folders, fetch_single_version_task


def find_missing_versions(items, papers_dir: Path, all_status: dict) -> list[tuple[str, str, Path]]:
    """A version is missing if its folder is absent, it has no status record, or its status is `error`."""
    missing = []
    for aid, v_list in items:
        p_dir = papers_dir / aid.replace(".", "-")
        v_downloads = all_status.get(aid, {}).get("version_downloads", {})
        for v_tag in v_list:
            st = v_downloads.get(v_tag, {}).get("status")
            if st == "error" or not st or not (p_dir / v_tag).exists():
                missing.append((aid, v_tag, p_dir))
    return missing


def recover_missing_versions(missing, all_status: dict, client: FastArxivClient, net_pool: ThreadPoolExecutor) -> None:
    """Re-fetch each missing version and merge the result into `all_status`."""
    for aid, v_tag, p_dir in missing:
        print(f"[Recovery] Re-fetching missed version: {aid} ({v_tag})...", flush=True)
        t0 = time.perf_counter()
        fut = net_pool.submit(fetch_single_version_task, aid, v_tag, client)
        res = extract_paper_to_existing_folders(0, len(missing), aid, p_dir, [fut], t0)

        rec = all_status.setdefault(aid, {
            "arxiv_id": aid,
            "paper_status": res["status"],
            "total_raw_archive_bytes": 0,
            "total_uncompressed_bytes": 0,
            "total_kept_tex_bib_bytes": 0,
            "download_time_sec": res["elapsed"],
            "version_downloads": {},
        })
        rec["version_downloads"][v_tag] = res["record"]["version_downloads"].get(v_tag, {})
        has_tex = any(v.get("status") == "success" for v in rec["version_downloads"].values())
        rec["paper_status"] = "success" if has_tex else "no_tex_source"
        print(f"[Recovery] Recovered {aid} ({v_tag}) -> {rec['paper_status']}", flush=True)


def run_audit(items, papers_dir: Path, all_status: dict, client: FastArxivClient, net_pool: ThreadPoolExecutor) -> None:
    print("\n--- Running automated post-crawl verification audit ---", flush=True)
    missing = find_missing_versions(items, papers_dir, all_status)
    if not missing:
        print("[Audit] 100% of versions verified! No missed versions found.", flush=True)
        return
    print(f"[Audit] Detected {len(missing)} missed version(s). Starting automatic recovery...", flush=True)
    recover_missing_versions(missing, all_status, client, net_pool)
    print("[Audit] All missed versions have been processed.", flush=True)
