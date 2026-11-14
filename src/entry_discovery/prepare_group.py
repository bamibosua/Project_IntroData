import argparse
import csv
import hashlib
import json
from pathlib import Path

from discovery import generate_ids, write_json_atomic, ID_PATTERN

PROJECT_ROOT = Path(__file__).resolve().parents[2]


def arxiv_key(arxiv_id: str):
    m = ID_PATTERN.fullmatch(arxiv_id)
    if not m or not 1 <= int(m.group(1)[-2:]) <= 12:
        raise ValueError(f"Invalid modern arXiv ID: {arxiv_id}")
    return int(m.group(1)), int(m.group(2))


def make_worklist(config: dict, allow_incomplete: bool = False) -> dict:
    """Validate member ranges and retain ALL member memberships on shared IDs."""
    members = config.get("members")
    if not isinstance(members, list) or not members:
        raise ValueError("Config must contain a nonempty members list")
    membership: dict[str, set[str]] = {}
    member_names = set()
    member_stats = []
    for member in members:
        name = member.get("member")
        ranges = member.get("ranges")
        if not isinstance(name, str) or not name.strip() or name in member_names:
            raise ValueError(f"Duplicate or invalid member name: {name}")
        member_names.add(name)
        if not isinstance(ranges, list):
            raise ValueError(f"{name}: ranges must be a list")
        if not ranges and not allow_incomplete:
            raise ValueError(
                f"{name}: missing assigned range; fill in team_ranges.json"
            )
        owned = set()
        for r in ranges:
            start, end = r.get("start"), r.get("end")
            if not isinstance(start, str) or not isinstance(end, str):
                raise ValueError(f"{name}: every range needs start/end ID strings")
            arxiv_key(start)
            arxiv_key(end)
            # A range must be entirely in one month. Split a multi-month assignment
            # into one range per month; do not invent a monthly maximum ID.
            for arxiv_id in generate_ids(start, end):
                arxiv_key(arxiv_id)
                membership.setdefault(arxiv_id, set()).add(name)
                owned.add(arxiv_id)
        member_stats.append(
            {
                "member": name,
                "assigned_unique_ids": len(owned),
                "configured_ranges": len(ranges),
            }
        )
    if not membership:
        raise ValueError("No IDs generated. Fill in at least one assigned range.")
    ids = sorted(membership, key=arxiv_key)
    return {
        "ids": ids,
        "owners": {arxiv_id: sorted(membership[arxiv_id]) for arxiv_id in ids},
        "stats": {
            "members": member_stats,
            "total_unique_ids": len(ids),
            "total_member_assignments": sum(len(membership[id]) for id in ids),
            "shared_ids": sum(len(membership[id]) > 1 for id in ids),
            "incomplete_members": [
                s["member"] for s in member_stats if not s["configured_ranges"]
            ],
        },
    }


def save_worklist(result: dict, out: Path) -> None:
    out.mkdir(parents=True, exist_ok=True)
    (out / "group_ids.txt").write_text(
        "\n".join(result["ids"]) + "\n", encoding="utf-8"
    )
    with (out / "id_ownership.csv").open("w", encoding="utf-8", newline="") as f:
        w = csv.writer(f)
        w.writerow(["arxiv_id", "members"])
        for arxiv_id, owners in result["owners"].items():
            w.writerow([arxiv_id, ";".join(owners)])
    digest = hashlib.sha256(
        ("\n".join(result["ids"]) + "\n").encode("utf-8")
    ).hexdigest()
    write_json_atomic(
        out / "group_summary.json", {**result["stats"], "ids_sha256": digest}
    )


def main(argv=None):
    parser = argparse.ArgumentParser(
        description="Merge the 4 member ranges, crawl IDs only once"
    )
    parser.add_argument(
        "--config",
        type=Path,
        default=Path(__file__).resolve().with_name("team_ranges.json"),
    )
    parser.add_argument("--output", type=Path, default=PROJECT_ROOT / "data" / "input")
    parser.add_argument(
        "--allow-incomplete",
        action="store_true",
        help="For a local trial only, without all members configured",
    )
    args = parser.parse_args(argv)
    try:
        config = json.loads(args.config.read_text(encoding="utf-8"))
        result = make_worklist(config, allow_incomplete=args.allow_incomplete)
        save_worklist(result, args.output)
    except (ValueError, KeyError, OSError, json.JSONDecodeError) as exc:
        parser.error(str(exc))
    print(json.dumps(result["stats"], indent=2, ensure_ascii=False))
    print(f"Saved {len(result['ids'])} unique IDs to {args.output / 'group_ids.txt'}")
    print(f"ID-to-member mapping in {args.output / 'id_ownership.csv'}")


if __name__ == "__main__":
    main()
