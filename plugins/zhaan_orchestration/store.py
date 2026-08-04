from __future__ import annotations

import datetime as dt
import json
import sqlite3
import uuid
from pathlib import Path
from typing import Any, Callable, Iterable


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
CREATE TABLE IF NOT EXISTS reminder_sessions (
  participant_id TEXT NOT NULL, plan_date TEXT NOT NULL,
  session_id TEXT NOT NULL UNIQUE, main_session_id TEXT NOT NULL,
  synced_message_count INTEGER NOT NULL DEFAULT 0,
  created_at TEXT NOT NULL, updated_at TEXT NOT NULL,
  PRIMARY KEY(participant_id, plan_date)
);
CREATE TABLE IF NOT EXISTS daily_threads (
  plan_date TEXT PRIMARY KEY, channel_id TEXT NOT NULL,
  thread_id TEXT NOT NULL UNIQUE, session_id TEXT NOT NULL UNIQUE,
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

    def reminder_session(self, participant_id: str, plan_date: str, main_session_id: str) -> str:
        now = utcnow()
        with self.connect() as db:
            db.execute("BEGIN IMMEDIATE")
            row = db.execute(
                "SELECT session_id FROM reminder_sessions WHERE participant_id=? AND plan_date=?",
                (participant_id, plan_date),
            ).fetchone()
            if row:
                db.commit()
                return row[0]
            session_id = f"zhaan-reminder-{uuid.uuid4()}"
            db.execute(
                "INSERT INTO reminder_sessions VALUES(?, ?, ?, ?, 0, ?, ?)",
                (participant_id, plan_date, session_id, main_session_id, now, now),
            )
            db.commit()
            return session_id

    def daily_thread(self, plan_date: str) -> dict[str, str] | None:
        with self.connect() as db:
            db.row_factory = sqlite3.Row
            row = db.execute(
                "SELECT * FROM daily_threads WHERE plan_date=?", (plan_date,)
            ).fetchone()
        return dict(row) if row else None

    def save_daily_thread(
        self, plan_date: str, channel_id: str, thread_id: str, session_id: str
    ) -> None:
        now = utcnow()
        with self.connect() as db:
            db.execute(
                """INSERT INTO daily_threads
                   (plan_date,channel_id,thread_id,session_id,created_at,updated_at)
                   VALUES(?,?,?,?,?,?)
                   ON CONFLICT(plan_date) DO UPDATE SET
                     channel_id=excluded.channel_id,
                     thread_id=excluded.thread_id,
                     session_id=excluded.session_id,
                     updated_at=excluded.updated_at""",
                (plan_date, channel_id, thread_id, session_id, now, now),
            )

    def sync_reminder_context(
        self,
        participant_id: str,
        plan_date: str,
        main_messages: list[dict[str, Any]],
        append: Callable[[str, dict[str, Any]], None],
    ) -> int:
        with self.connect() as db:
            db.execute("BEGIN IMMEDIATE")
            row = db.execute(
                "SELECT session_id, synced_message_count FROM reminder_sessions WHERE participant_id=? AND plan_date=?",
                (participant_id, plan_date),
            ).fetchone()
            if not row:
                db.rollback()
                raise KeyError((participant_id, plan_date))
            session_id, offset = row
            delta = main_messages[offset:]
            for message in delta:
                copy = dict(message)
                copy["context_only"] = True
                copy["metadata"] = {**copy.get("metadata", {}), "zhaan_context_copy": True}
                append(session_id, copy)
            db.execute(
                "UPDATE reminder_sessions SET synced_message_count=?, updated_at=? WHERE participant_id=? AND plan_date=?",
                (len(main_messages), utcnow(), participant_id, plan_date),
            )
            db.commit()
            return len(delta)

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
            db.commit()
            return status
