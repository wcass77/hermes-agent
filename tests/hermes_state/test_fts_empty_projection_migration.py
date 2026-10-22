"""An empty pre-projection FTS index must migrate without losing its savepoint."""

import sqlite3

import pytest

from hermes_state import SessionDB


def _rewind_empty_index(db):
    if not db._fts_enabled:
        pytest.skip("SQLite FTS5 unavailable")
    db._conn.execute("DROP TABLE messages_fts")
    db._conn.execute(
        "CREATE VIRTUAL TABLE messages_fts USING fts5("
        "content, tool_name, tool_calls, content='messages', content_rowid='id')"
    )
    db._conn.commit()


@pytest.mark.parametrize("existing_session", [False, True])
def test_empty_old_index_opens_and_indexes_new_messages(tmp_path, existing_session):
    path = tmp_path / "state.db"
    first = SessionDB(path)
    try:
        if existing_session:
            first.create_session("retained", source="cli")
        _rewind_empty_index(first)
    finally:
        first.close()

    migrated = SessionDB(path)
    try:
        if existing_session:
            assert migrated.get_session("retained") is not None
        migrated.create_session("new", source="cli")
        migrated.append_message("new", role="user", content="empty migration searchable")
        assert migrated.search_messages("searchable")
        migrated._conn.execute(
            "INSERT INTO messages_fts(messages_fts, rank) VALUES('integrity-check', 1)"
        )
    finally:
        migrated.close()


def test_empty_alignment_failure_restores_original_index(tmp_path, monkeypatch):
    db = SessionDB(tmp_path / "state.db")
    try:
        _rewind_empty_index(db)
        original = db._conn.execute(
            "SELECT sql FROM sqlite_master WHERE name = 'messages_fts'"
        ).fetchone()[0]

        def fail_ddl(_cursor, _ddl):
            raise sqlite3.OperationalError("injected DDL failure")

        monkeypatch.setattr(db, "_execute_ddl_script_transactional", fail_ddl)
        with pytest.raises(sqlite3.OperationalError, match="injected DDL failure"):
            db._migrate_misaligned_fts_source(db._conn.cursor(), legacy=False)
        restored = db._conn.execute(
            "SELECT sql FROM sqlite_master WHERE name = 'messages_fts'"
        ).fetchone()[0]
        assert restored == original
        db._conn.execute(
            "INSERT INTO messages_fts(messages_fts, rank) VALUES('integrity-check', 1)"
        )
    finally:
        db.close()
