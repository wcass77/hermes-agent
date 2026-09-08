from __future__ import annotations

import datetime as dt
import json
import sqlite3
import uuid
from pathlib import Path
from typing import Any


SCHEMA = """
CREATE TABLE IF NOT EXISTS ingress_events (
  event_id TEXT PRIMARY KEY, webhook_id TEXT NOT NULL UNIQUE,
  message_id TEXT NOT NULL UNIQUE, thread_id TEXT NOT NULL,
  inbox_id TEXT NOT NULL, sender TEXT NOT NULL, payload TEXT NOT NULL,
  status TEXT NOT NULL DEFAULT 'queued', attempts INTEGER NOT NULL DEFAULT 0,
  available_at TEXT NOT NULL, accepted_at TEXT NOT NULL,
  completed_at TEXT, last_error TEXT
);
CREATE TABLE IF NOT EXISTS email_sessions (
  thread_id TEXT PRIMARY KEY, session_id TEXT NOT NULL UNIQUE,
  created_at TEXT NOT NULL, updated_at TEXT NOT NULL
);
"""


def utcnow() -> str:
    return dt.datetime.now(dt.timezone.utc).isoformat()


class Store:
    def __init__(self, path: Path):
        self.path = path
        path.parent.mkdir(parents=True, exist_ok=True)
        with self.connect() as db:
            db.executescript(SCHEMA)
            columns = {row[1] for row in db.execute("PRAGMA table_info(ingress_events)")}
            if "content_fingerprint" not in columns:
                db.execute("ALTER TABLE ingress_events ADD COLUMN content_fingerprint TEXT")
            if "duplicate_of_event_id" not in columns:
                db.execute("ALTER TABLE ingress_events ADD COLUMN duplicate_of_event_id TEXT")
            db.execute(
                "CREATE UNIQUE INDEX IF NOT EXISTS ingress_events_content_fingerprint "
                "ON ingress_events(content_fingerprint) WHERE content_fingerprint IS NOT NULL"
            )

    def connect(self):
        return sqlite3.connect(self.path, timeout=30, isolation_level=None)

    def enqueue(self, webhook_id: str, payload: dict[str, Any], sender: str) -> str:
        message = payload["message"]
        event_id = str(payload["event_id"])
        with self.connect() as db:
            db.execute("BEGIN IMMEDIATE")
            try:
                db.execute(
                    """INSERT INTO ingress_events(event_id, webhook_id, message_id,
                    thread_id, inbox_id, sender, payload, available_at, accepted_at)
                    VALUES(?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                    (event_id, webhook_id, str(message["message_id"]),
                     str(message["thread_id"]), str(message["inbox_id"]), sender,
                     json.dumps(payload, separators=(",", ":")), utcnow(), utcnow()),
                )
                result = "queued"
            except sqlite3.IntegrityError:
                result = "duplicate"
            db.commit()
        return result

    def session_for_email_thread(self, thread_id: str) -> str:
        now = utcnow()
        with self.connect() as db:
            db.execute("BEGIN IMMEDIATE")
            row = db.execute("SELECT session_id FROM email_sessions WHERE thread_id=?", (thread_id,)).fetchone()
            if row:
                db.execute("UPDATE email_sessions SET updated_at=? WHERE thread_id=?", (now, thread_id))
                db.commit()
                return row[0]
            session_id = f"zhaan-email-{uuid.uuid4()}"
            db.execute("INSERT INTO email_sessions VALUES(?, ?, ?, ?)", (thread_id, session_id, now, now))
            db.commit()
            return session_id

    def counts(self) -> dict[str, int]:
        with self.connect() as db:
            return {
                status: count for status, count in db.execute(
                    "SELECT status, count(*) FROM ingress_events GROUP BY status"
                )
            }

    def claim_next(self, worker_id: str, now: dt.datetime | None = None) -> dict[str, Any] | None:
        now = now or dt.datetime.now(dt.timezone.utc)
        stamp = now.isoformat()
        with self.connect() as db:
            db.row_factory = sqlite3.Row
            db.execute("BEGIN IMMEDIATE")
            row = db.execute(
                """SELECT * FROM ingress_events
                WHERE status IN ('queued','retry') AND available_at <= ?
                ORDER BY accepted_at LIMIT 1""",
                (stamp,),
            ).fetchone()
            if not row:
                db.commit()
                return None
            db.execute(
                "UPDATE ingress_events SET status='processing', attempts=attempts+1, last_error=NULL WHERE event_id=?",
                (row["event_id"],),
            )
            db.commit()
            result = dict(row)
            result["attempts"] += 1
            result["worker_id"] = worker_id
            result["payload"] = json.loads(result["payload"])
            return result

    def recover_processing(self) -> int:
        with self.connect() as db:
            cursor = db.execute("UPDATE ingress_events SET status='retry' WHERE status='processing'")
            return cursor.rowcount

    def claim_content(self, event_id: str, fingerprint: str) -> tuple[str, str | None]:
        """Atomically elect one event to process a canonical original email."""
        with self.connect() as db:
            db.execute("BEGIN IMMEDIATE")
            current = db.execute(
                "SELECT content_fingerprint FROM ingress_events WHERE event_id=?", (event_id,)
            ).fetchone()
            if current is None:
                db.rollback()
                raise KeyError(event_id)
            if current[0] == fingerprint:
                db.commit()
                return "owner", event_id
            owner = db.execute(
                "SELECT event_id, status FROM ingress_events WHERE content_fingerprint=?",
                (fingerprint,),
            ).fetchone()
            if owner is None:
                db.execute(
                    "UPDATE ingress_events SET content_fingerprint=?, duplicate_of_event_id=NULL WHERE event_id=?",
                    (fingerprint, event_id),
                )
                db.commit()
                return "owner", event_id
            owner_id, owner_status = owner
            if owner_status == "failed":
                db.execute(
                    "UPDATE ingress_events SET content_fingerprint=NULL WHERE event_id=?", (owner_id,)
                )
                db.execute(
                    "UPDATE ingress_events SET content_fingerprint=?, duplicate_of_event_id=NULL WHERE event_id=?",
                    (fingerprint, event_id),
                )
                db.commit()
                return "owner", event_id
            if owner_status == "complete":
                db.execute(
                    "UPDATE ingress_events SET duplicate_of_event_id=? WHERE event_id=?",
                    (owner_id, event_id),
                )
                db.commit()
                return "duplicate", owner_id
            db.commit()
            return "wait", owner_id

    def defer(self, event_id: str, *, delay_seconds: int = 10) -> None:
        available = (dt.datetime.now(dt.timezone.utc) + dt.timedelta(seconds=delay_seconds)).isoformat()
        with self.connect() as db:
            db.execute(
                """UPDATE ingress_events
                SET status='retry', available_at=?,
                    attempts=CASE WHEN attempts > 0 THEN attempts - 1 ELSE 0 END
                WHERE event_id=?""",
                (available, event_id),
            )

    def complete(self, event_id: str) -> None:
        with self.connect() as db:
            db.execute(
                "UPDATE ingress_events SET status='complete', completed_at=? WHERE event_id=?",
                (utcnow(), event_id),
            )

    def fail(self, event_id: str, error: str, *, max_attempts: int = 5, delay_seconds: int = 60) -> str:
        with self.connect() as db:
            db.execute("BEGIN IMMEDIATE")
            row = db.execute("SELECT attempts FROM ingress_events WHERE event_id=?", (event_id,)).fetchone()
            if not row:
                db.rollback()
                raise KeyError(event_id)
            status = "failed" if row[0] >= max_attempts else "retry"
            available = (dt.datetime.now(dt.timezone.utc) + dt.timedelta(seconds=delay_seconds)).isoformat()
            db.execute(
                "UPDATE ingress_events SET status=?, available_at=?, last_error=? WHERE event_id=?",
                (status, available, error[:2000], event_id),
            )
            if status == "failed":
                db.execute(
                    "UPDATE ingress_events SET content_fingerprint=NULL WHERE event_id=?", (event_id,)
                )
            db.commit()
            return status
