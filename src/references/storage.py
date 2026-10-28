"""Transactional publication snapshots and bounded-memory SQLite readers."""

from __future__ import annotations

import json
import sqlite3
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Iterator

from .extractor import get_arxiv_id_from_external_ids

_SCHEMA_VERSION = "3"
_QUERY_CHUNK_SIZE = 400


def _chunks(ids: list[str]) -> Iterator[list[str]]:
    unique = list(dict.fromkeys(ids))
    for start in range(0, len(unique), _QUERY_CHUNK_SIZE):
        yield unique[start:start + _QUERY_CHUNK_SIZE]


class CheckpointStore:
    def __init__(self, path: Path) -> None:
        self.path = Path(path)
        with self._connect() as db:
            # DDL and legacy invalidation must commit together too.
            db.execute("BEGIN IMMEDIATE")
            has_metadata = db.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='metadata'").fetchone()
            if has_metadata:
                version = db.execute("SELECT value FROM metadata WHERE key='schema_version'").fetchone()
                if version:
                    try:
                        supported = int(version[0]) <= int(_SCHEMA_VERSION)
                    except (ValueError, TypeError):
                        supported = False
                    if not supported:
                        raise ValueError(f"Unsupported checkpoint schema version: {version[0]}")
            columns = [row[1] for row in db.execute("PRAGMA table_info(papers)")]
            if columns and set(columns) not in ({"id", "status"}, {"id", "status", "refs"}):
                raise ValueError("Unsupported checkpoint papers schema")
            legacy = "refs" in columns
            if legacy:
                db.execute("ALTER TABLE papers RENAME TO legacy_papers")
            db.execute("CREATE TABLE IF NOT EXISTS papers (id TEXT PRIMARY KEY, status TEXT NOT NULL)")
            db.execute("""CREATE TABLE IF NOT EXISTS reference_rows (
                source_id TEXT NOT NULL REFERENCES papers(id) ON DELETE CASCADE,
                ordinal INTEGER NOT NULL,
                cited_arxiv_id TEXT,
                payload TEXT NOT NULL,
                PRIMARY KEY (source_id, ordinal)
            )""")
            db.execute("CREATE INDEX IF NOT EXISTS reference_target ON reference_rows (source_id, cited_arxiv_id, ordinal)")
            db.execute("CREATE TABLE IF NOT EXISTS metadata (key TEXT PRIMARY KEY, value TEXT NOT NULL)")
            if legacy:
                for pid, status_text in db.execute("SELECT id, status FROM legacy_papers"):
                    status = json.loads(status_text)
                    status.setdefault("arxiv_id", pid)
                    if status.get("status") == "found":
                        status.update(status="error", raw_ref_count=0, arxiv_ref_count=0,
                                      next_retry_at=0.0, retry_count=0,
                                      error="Legacy checkpoint stored filtered edges; full references must be crawled again")
                        status.pop("http_status", None)
                        status.pop("retry_after", None)
                    db.execute("INSERT INTO papers (id, status) VALUES (?, ?)",
                               (pid, json.dumps(status, ensure_ascii=False)))
                db.execute("DROP TABLE legacy_papers")
            db.execute("INSERT OR REPLACE INTO metadata (key, value) VALUES ('schema_version', ?)",
                       (_SCHEMA_VERSION,))

    @contextmanager
    def _connect(self):
        db = sqlite3.connect(self.path, timeout=30)
        db.execute("PRAGMA foreign_keys = ON")
        try:
            with db:
                yield db
        finally:
            db.close()

    def reset(self) -> None:
        with self._connect() as db:
            db.execute("DELETE FROM reference_rows")
            db.execute("DELETE FROM papers")

    def save(self, paper: dict[str, Any], references: list[dict[str, Any] | None]) -> None:
        """Replace one complete snapshot and its status in the same transaction."""
        pid = paper["arxiv_id"]
        with self._connect() as db:
            # UPSERT avoids REPLACE's implicit delete and cascading side effects.
            db.execute("""INSERT INTO papers (id, status) VALUES (?, ?)
                ON CONFLICT(id) DO UPDATE SET status=excluded.status""",
                       (pid, json.dumps(paper, ensure_ascii=False)))
            db.execute("DELETE FROM reference_rows WHERE source_id = ?", (pid,))
            for ordinal, ref in enumerate(references):
                if ref is not None and not isinstance(ref, dict):
                    raise ValueError("Reference snapshot entries must be objects or null")
                arxiv_id = get_arxiv_id_from_external_ids(ref.get("externalIds")) if ref else None
                db.execute("""INSERT INTO reference_rows
                    (source_id, ordinal, cited_arxiv_id, payload) VALUES (?, ?, ?, ?)""",
                           (pid, ordinal, arxiv_id or None, json.dumps(ref, ensure_ascii=False)))

    def load_statuses(self, ids: list[str]) -> tuple[dict, set]:
        """Read only selected statuses, without reading or decoding references."""
        statuses, processed = {}, set()
        with self._connect() as db:
            for chunk in _chunks(ids):
                marks = ",".join("?" for _ in chunk)
                for pid, status_text in db.execute(f"SELECT id, status FROM papers WHERE id IN ({marks})", chunk):
                    status = json.loads(status_text)
                    statuses[pid] = status
                    if status.get("status") in ("found", "not_found"):
                        processed.add(pid)
        return statuses, processed

    def iter_reference_records(self, ids: list[str]) -> Iterator[dict[str, Any]]:
        """Yield every reference entry, preserving duplicate and null payloads."""
        with self._connect() as db:
            for chunk in _chunks(ids):
                marks = ",".join("?" for _ in chunk)
                rows = db.execute(f"""SELECT source_id, ordinal, cited_arxiv_id, payload
                    FROM reference_rows WHERE source_id IN ({marks})
                    ORDER BY source_id, ordinal""", chunk)
                for source, ordinal, arxiv_id, payload in rows:
                    raw = json.loads(payload)
                    ref = raw or {}
                    yield {
                        "source_arxiv_id": source,
                        "reference_index": ordinal,
                        "cited_arxiv_id": arxiv_id,
                        "cited_title": ref.get("title") or "",
                        "cited_authors": [a["name"] for a in ref.get("authors") or [] if a.get("name")],
                        "cited_publication_date": ref.get("publicationDate"),
                        "cited_s2_id": ref.get("paperId") or "",
                        "external_ids": ref.get("externalIds"),
                        "raw_reference": raw,
                    }

    def iter_arxiv_edges(self, ids: list[str]) -> Iterator[dict[str, Any]]:
        """Yield old export columns, deduplicating by source/target in SQL."""
        with self._connect() as db:
            for chunk in _chunks(ids):
                marks = ",".join("?" for _ in chunk)
                rows = db.execute(f"""SELECT r.source_id, r.cited_arxiv_id, r.payload
                    FROM reference_rows r
                    WHERE r.source_id IN ({marks}) AND r.cited_arxiv_id IS NOT NULL
                      AND r.ordinal = (SELECT MIN(s.ordinal) FROM reference_rows s
                                       WHERE s.source_id=r.source_id AND s.cited_arxiv_id=r.cited_arxiv_id)
                    ORDER BY r.source_id, r.ordinal""", chunk)
                for source, target, payload in rows:
                    ref = json.loads(payload)
                    yield {"source_arxiv_id": source, "cited_arxiv_id": target,
                           "cited_title": ref.get("title") or "", "cited_s2_id": ref.get("paperId") or ""}

    def reference_counts(self, ids: list[str]) -> dict[str, int]:
        """Aggregate in SQLite, including distinct targets across query chunks."""
        with self._connect() as db:
            db.execute("CREATE TEMP TABLE selected_ids (id TEXT PRIMARY KEY)")
            db.executemany("INSERT OR IGNORE INTO selected_ids VALUES (?)", ((pid,) for pid in ids))
            total = db.execute("""SELECT COUNT(*) FROM reference_rows r
                JOIN selected_ids s ON s.id=r.source_id""").fetchone()[0]
            arxiv = db.execute("""SELECT COUNT(*) FROM (
                SELECT r.source_id, r.cited_arxiv_id FROM reference_rows r
                JOIN selected_ids s ON s.id=r.source_id
                WHERE r.cited_arxiv_id IS NOT NULL GROUP BY r.source_id, r.cited_arxiv_id
                )""").fetchone()[0]
            unique = db.execute("""SELECT COUNT(DISTINCT r.cited_arxiv_id) FROM reference_rows r
                JOIN selected_ids s ON s.id=r.source_id""").fetchone()[0]
            return {"total_stored_references": total, "total_arxiv_references": arxiv,
                    "unique_cited_arxiv_ids": unique}

    def load(self, ids: list[str]) -> tuple[dict, list, set]:
        """Compatibility helper; production resume uses load_statuses instead."""
        statuses, processed = self.load_statuses(ids)
        return statuses, list(self.iter_arxiv_edges(ids)), processed
