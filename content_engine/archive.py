"""SQLite article history. Approval is distinct from publication."""

import json
import sqlite3
from datetime import datetime, timezone
from pathlib import Path


class ArticleArchive:
    def __init__(self, path: Path):
        path.parent.mkdir(parents=True, exist_ok=True)
        self.path = path
        with self.connect() as db:
            db.execute("""CREATE TABLE IF NOT EXISTS articles (
                id TEXT PRIMARY KEY, thread_id TEXT NOT NULL, version TEXT NOT NULL,
                approved_at TEXT NOT NULL, status TEXT NOT NULL DEFAULT 'approved',
                brief TEXT NOT NULL, package TEXT NOT NULL, export_path TEXT NOT NULL,
                UNIQUE(thread_id, version))""")

    def connect(self):
        return sqlite3.connect(self.path)

    def briefs(self, exclude_thread: str = "") -> list[dict]:
        with self.connect() as db:
            rows = db.execute(
                "SELECT id, brief FROM articles WHERE thread_id != ? ORDER BY approved_at DESC",
                (exclude_thread,),
            ).fetchall()
        return [{"id": key, **json.loads(brief)} for key, brief in rows]

    def articles(self):
        with self.connect() as db:
            rows = db.execute("SELECT id, thread_id, approved_at, status, brief FROM articles ORDER BY approved_at DESC").fetchall()
        return [{"id": key, "thread_id": thread, "recorded_at": date, "status": status, "brief": json.loads(brief)} for key, thread, date, status, brief in rows]

    def article(self, key):
        with self.connect() as db:
            row = db.execute("SELECT brief, package, status FROM articles WHERE id = ?", (key,)).fetchone()
        if row is None:
            raise ValueError("Article not found")
        return {"brief": json.loads(row[0]), "package": json.loads(row[1]), "status": row[2]}

    def save(self, thread_id: str, version: str, brief: dict, package: dict, export_path: str, *, status="approved"):
        with self.connect() as db:
            db.execute(
                "INSERT OR IGNORE INTO articles (id, thread_id, version, approved_at, brief, package, export_path, status) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                (f"{thread_id}-{version[:12]}", thread_id, version, datetime.now(timezone.utc).isoformat(),
                 json.dumps(brief), json.dumps(package), export_path, status),
            )
