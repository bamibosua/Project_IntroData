# Entry Discovery — Group-wide crawler (4 member ranges)

This extends the earlier `Entry_Discovery` project to follow the instructor's recommendation: **merge all four assigned ID sets, deduplicate, then run ONE shared crawler** for the whole group. Each ID still retains ownership to allow filtering by member at the end of Milestone 3. Python 3.10+, no third-party libraries.

## Folder structure

- `team_ranges.json` — input config for four members (only member_01's range is known; fill other three)
- `src/prepare_group.py` — merge ranges and create `group_ids.txt`, `id_ownership.csv`, `group_summary.json`
- `src/discovery.py` — retrieve all paper metadata and versions using the single group worklist
- `tests/` — offline unit tests
- `data/` — generated outputs, ignored by Git (make backups separately)

## Windows VS Code / PowerShell

Open this folder in VS Code, select Terminal -> New Terminal:

```powershell
py -m venv .venv
.\.venv\Scripts\python.exe -m unittest discover -s tests -v
```

Edit `team_ranges.json` and enter the REAL ranges for member_02, member_03, member_04 (do not copy the sample range). If one person's assignment crosses into another month, break it into separate ranges for each month. **Do not guess the last arXiv sequence number of a month.**

First, verify the worklist using the known one member's range only (optional):

```powershell
.\.venv\Scripts\python.exe src\prepare_group.py --allow-incomplete --output data\trial_input
```

Once all four members' ranges are configured, merge them:

```powershell
.\.venv\Scripts\python.exe src\prepare_group.py --output data\group\input
```

Look at `data/group/input/group_summary.json` and `data/group/input/id_ownership.csv` before crawling. `group_ids.txt` has unique IDs only, one per line; `id_ownership.csv` stores every member assigned to each ID (some IDs may overlap).

Set a real contact email for the User-Agent:

```powershell
$env:ARXIV_USER_AGENT = "HCMUS-IDS-M1/1.0 (contact: your-real-email@example.com)"
```

Test only the first 5 IDs:

```powershell
.\.venv\Scripts\python.exe src\discovery.py --ids-file data\group\input\group_ids.txt --limit 5 --output data\trial_discovery
```

Run the entire group worklist:

```powershell
.\.venv\Scripts\python.exe src\discovery.py --ids-file data\group\input\group_ids.txt --batch-size 20 --output data\group\discovery
```

Outputs:

```text
data/group/
  input/
    group_ids.txt            # unique union of member ranges (same list for all 3 milestones)
    id_ownership.csv         # arxiv_id -> member(s); keep until Milestone 3 export
    group_summary.json       # ranges / duplicates summary
  discovery/
    batches/                 # per-batch resume checkpoints
    checkpoint_manifest.json # prevents mixing different ID sets or batch sizes
    discovered_papers.json  # found papers + latest version and all version IDs
    discovery_status.json   # status for every ID
    summary.json            # discovery stats
```

Pass the shared `discovered_papers.json` (or its parsed Python objects) to the Source Downloader and References Crawler; write all source and reference data to one **group corpus organized by arxiv_id**, not four copies.

At the end of Milestone 3, filter the finalized dataset with `id_ownership.csv` to obtain member-specific submissions. If ranges overlap, the same ID can belong to multiple members even though it was crawled only once.

**Warning:** The file `team_ranges.json` contains only one real range. The other ranges are placeholders and MUST be supplied before the strict full-group command will work. The program is tested offline; real-world network/API behavior is not verified in this environment. `revised_dates` is still `null` until you implement version-history date retrieval.

API etiquette: at least ~3 seconds between requests, sequential connections (built into the HTTP client). Do not commit `data/` to the code GitHub repository. Keep a safe shared backup, for example on the group's Drive.
