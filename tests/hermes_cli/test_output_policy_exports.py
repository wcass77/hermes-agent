"""Export derivatives sanitize by default while their source remains faithful."""

import argparse
import asyncio
import copy
import json

import pytest

from hermes_cli.session_export import render_session_for_save
from hermes_state import SessionDB


FIXTURE = "am_" + "SyntheticFixture123" * 3
URL = "https://example.test/data?access_token=opaque-export-token"


def _session():
    return {"id": "policy-session", "title": FIXTURE, "system_prompt": URL,
            "messages": [{"role": "tool", "content": FIXTURE + " " + URL, "credential": {"password": "opaque-structured-secret"}},
                         {"role": "assistant", "content": "ordinary answer", "tool_calls": [
                             {"id": "call_a", "type": "function", "function": {
                                 "name": "terminal", "arguments": json.dumps({"command": "echo " + FIXTURE})}}]}]}


@pytest.mark.parametrize("fmt", ["json", "md", "html"])
def test_save_defaults_to_labeled_derivative_and_raw_requires_explicit_opt_out(fmt):
    source = _session()
    before = copy.deepcopy(source)
    exported = render_session_for_save(source, fmt)
    assert FIXTURE not in exported and "opaque-export-token" not in exported
    assert "sanitized" in exported
    assert "opaque-structured-secret" not in exported
    raw = render_session_for_save(source, fmt, redact=False)
    assert FIXTURE in raw and "raw" in raw
    assert source == before


@pytest.mark.parametrize("fmt", ["jsonl", "md", "qmd", "html"])
def test_cli_exports_sanitize_without_opt_in(tmp_path, fmt):
    from hermes_cli.sessions_cmd import _cmd_export
    db = SessionDB(tmp_path / "state.db")
    try:
        db.create_session("policy-session", "cli", system_prompt=URL)
        db.set_session_title("policy-session", FIXTURE)
        db.append_message("policy-session", "tool", FIXTURE + " " + URL)
        before = db.export_session("policy-session")
        output = tmp_path / ("export" if fmt in ("md", "qmd") else "export." + fmt)
        args = argparse.Namespace(session_id="policy-session", format=fmt, output=str(output),
            redact=False, no_redact=False, only=None, lineage="single", dry_run=False,
            delete_after_verified=False, force=False)
        _cmd_export(db, args)
        paths = list(output.glob("*." + fmt)) if output.is_dir() else [output]
        assert paths and all(path.exists() for path in paths)
        for path in paths:
            exported = path.read_text()
            assert FIXTURE not in exported and "opaque-export-token" not in exported
            assert "sanitized" in exported
        assert db.export_session("policy-session") == before
    finally:
        db.close()


def test_dashboard_stream_sanitizes_all_pages_and_preserves_source(tmp_path, monkeypatch):
    from hermes_cli.web_routers import sessions
    from hermes_cli import web_server_sessions
    from agent.redact import register_vault_redaction_value, clear_vault_redaction_values
    from hermes_constants import set_hermes_home_override, reset_hermes_home_override
    token = set_hermes_home_override(tmp_path)
    try:
        register_vault_redaction_value("opaque-secondary-vault")
    finally:
        reset_hermes_home_override(token)
    path = tmp_path / "state.db"
    db = SessionDB(path)
    db.create_session("policy-session", "cli", system_prompt=URL)
    db.set_session_title("policy-session", FIXTURE + " opaque-secondary-vault")
    db.append_messages_batch("policy-session", [{"role": "tool", "content": FIXTURE} for _ in range(503)])
    db.close()
    monkeypatch.setattr(web_server_sessions, "_open_session_db_for_profile",
                        lambda profile=None, *, read_only: SessionDB(path, read_only=read_only))

    monkeypatch.setattr(web_server_sessions, "_session_db_path_for_profile", lambda profile=None: path)

    async def export():
        response = await sessions.export_session_endpoint("policy-session")
        return "".join([chunk async for chunk in response.body_iterator])

    data = asyncio.run(export())
    assert FIXTURE not in data and "opaque-export-token" not in data
    assert "opaque-secondary-vault" not in data
    token = set_hermes_home_override(tmp_path)
    clear_vault_redaction_values()
    reset_hermes_home_override(token)
    result = json.loads(data)
    assert len(result["messages"]) == 503
    assert result["export_redaction"] == "sanitized"
    source = SessionDB(path, read_only=True)
    try:
        assert all(m["content"] == FIXTURE for m in source.get_messages("policy-session"))
    finally:
        source.close()


@pytest.mark.parametrize("raw", [False, True])
def test_prompt_only_cli_export_labels_its_explicit_policy(tmp_path, raw):
    from hermes_cli.sessions_cmd import _cmd_export
    with SessionDB(tmp_path / "state.db") as db:
        db.create_session("policy-session", "cli")
        db.append_message("policy-session", "user", FIXTURE)
        output = tmp_path / "prompts.jsonl"
        args = argparse.Namespace(session_id="policy-session", format="jsonl", output=str(output),
            redact=False, no_redact=raw, only="user-prompts", lineage="single", dry_run=False)
        _cmd_export(db, args)
        data = json.loads(output.read_text())
        assert data["export_redaction"] == ("raw" if raw else "sanitized")
        assert (FIXTURE in data["text"]) is raw
