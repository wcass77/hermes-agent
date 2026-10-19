"""Ordinary fidelity has one result across live tools, private replay and recovery."""

import json
import logging
import shlex
import stat
import sys

import pytest


FIXTURE = "sk-proj-" + "SyntheticFixture123" * 4
VAULT = "synthetic-vault-password-only"


@pytest.fixture
def fidelity_home(tmp_path, monkeypatch):
    from hermes_constants import set_hermes_home_override, reset_hermes_home_override
    home = tmp_path / "profile"
    home.mkdir(mode=0o700)
    (home / "config.yaml").write_text(
        "security:\n  redact_secrets: true\n  ordinary_output: fidelity\n"
        "terminal:\n  backend: local\n  cwd: " + str(tmp_path) + "\n")
    monkeypatch.setenv("HERMES_HOME", str(home))
    monkeypatch.setenv("TERMINAL_ENV", "local")
    monkeypatch.setenv("TERMINAL_CWD", str(tmp_path))
    token = set_hermes_home_override(home)
    try:
        yield home
    finally:
        from agent.redact import clear_vault_redaction_values
        clear_vault_redaction_values()
        reset_hermes_home_override(token)


def test_ordinary_fixture_survives_file_terminal_spill_and_private_session(fidelity_home, tmp_path):
    from agent.redact import RedactingFormatter, register_vault_redaction_value
    from hermes_state import SessionDB
    from tools import file_tools
    from tools.registry import registry
    from tools.terminal_tool_result import finalize_foreground_result

    register_vault_redaction_value(VAULT)
    payload = json.dumps({"fixture": FIXTURE, "vault": VAULT, "MAX_TOKENS": 100})
    source = tmp_path / "fixture.json"
    source.write_text(payload)
    # Real registry handlers, shell file backend and file I/O in the isolated home.
    read = json.loads(registry.get_entry("read_file").handler({"path": str(source)}, task_id="policy-read"))
    assert FIXTURE in read["content"]
    assert VAULT not in read["content"]
    search = json.loads(registry.get_entry("search_files").handler(
        {"path": str(source), "pattern": "fixture", "target": "content"}, task_id="policy-read"))
    assert FIXTURE in json.dumps(search)
    assert VAULT not in json.dumps(search)
    spill = tmp_path / "overflow.log"
    spill.write_text(payload + "\n" + "x" * 60_000)
    result = json.loads(finalize_foreground_result(
        command="python report.py", result={"output": payload, "returncode": 0,
            "full_output_path": str(spill), "output_total_chars": spill.stat().st_size},
        env=object(), env_type="local", effective_task_id="policy-read", task_id="policy-read",
        session_id=None, session_key="policy-read", workdir=None, command_cwd=None, approval_note=None))
    assert FIXTURE in result["output"] and VAULT not in result["output"]
    capture = spill.read_text()
    assert FIXTURE in capture and VAULT not in capture
    assert stat.S_IMODE(spill.stat().st_mode) == 0o600
    db = SessionDB()
    try:
        db.create_session("policy-session", "cli")
        db.append_message("policy-session", "tool", result["output"])
        assert db.get_messages("policy-session")[0]["content"] == result["output"]
    finally:
        db.close()
    record = logging.LogRecord("policy", logging.INFO, __file__, 1, payload, (), None)
    diagnostic = RedactingFormatter().format(record)
    assert FIXTURE not in diagnostic and VAULT not in diagnostic


def test_background_recovery_preserves_guarded_output_and_sanitizes_diagnostics(fidelity_home, tmp_path, monkeypatch):
    from agent.redact import register_vault_redaction_value
    from tools.process_registry import ProcessRegistry, _redact_process_result
    from tools.process_registry_results import load_completed_results

    register_vault_redaction_value(VAULT)
    monkeypatch.setenv("HERMES_SESSION_ID", "policy-owner")
    registry = ProcessRegistry()
    command = shlex.join([sys.executable, "-c", f"print({(FIXTURE + ' ' + VAULT)!r})"])
    process = registry.spawn_local(command, cwd=str(tmp_path), task_id="policy-owner")
    assert process._completion_event.wait(20)
    live = _redact_process_result(registry.read_log(process.id))
    assert FIXTURE in live["output"] and VAULT not in live["output"]
    recovered = load_completed_results(process.id)[process.id]
    assert FIXTURE in recovered.output_buffer and VAULT not in recovered.output_buffer
    assert recovered.exited and recovered._completion_event.is_set()
    diagnostic = fidelity_home / "logs" / "process-results" / f"{process.id}.json"
    assert FIXTURE not in diagnostic.read_text() and VAULT not in diagnostic.read_text()
    private = fidelity_home / "state" / "process-results" / f"{process.id}.json"
    assert stat.S_IMODE(private.stat().st_mode) == 0o600
    assert stat.S_IMODE(private.parent.stat().st_mode) == 0o700
    assert FIXTURE in private.read_text() and VAULT not in private.read_text()
    monkeypatch.setenv("HERMES_SESSION_ID", "another-owner")
    assert load_completed_results(process.id) == {}


def test_recovery_does_not_transform_twice_or_use_diagnostic_fallback(fidelity_home, monkeypatch):
    from tools import process_registry as processes
    from tools.process_registry_results import save_completed_result, load_completed_results

    monkeypatch.setenv("HERMES_SESSION_ID", "policy-owner")
    session = processes.ProcessSession(id="proc_policy", command="python report.py", task_id="policy-owner",
        parent_session_id="policy-owner", exited=True, exit_code=0, output_buffer=FIXTURE)
    monkeypatch.setattr(processes, "transform_process_output", lambda text, **kwargs: "transformed:" + text)
    save_completed_result(session)
    recovered = load_completed_results(session.id)[session.id]
    monkeypatch.setattr(processes.process_registry, "get", lambda sid: recovered)
    result = processes._redact_process_result({"session_id": session.id, "command": session.command,
                                               "output": recovered.output_buffer, "exit_code": 0})
    assert result["output"] == "transformed:" + FIXTURE
    (fidelity_home / "state" / "process-results" / f"{session.id}.json").unlink()
    assert load_completed_results(session.id) == {}


def test_gateway_model_notifications_preserve_fixtures_and_human_delivery_sanitizes(fidelity_home):
    from gateway.run import GatewayRunner
    from tools.process_registry import ProcessSession
    from agent.redact import register_vault_redaction_value

    register_vault_redaction_value(VAULT)
    session = ProcessSession(id="proc_policy", command="python report.py", exited=True, exit_code=0,
                             output_buffer=FIXTURE + " " + VAULT)
    event = GatewayRunner._build_process_completion_event({}, session, session.id)
    assert FIXTURE in event["output"] and VAULT not in event["output"]
    coalesced = GatewayRunner._format_coalesced_process_completions([("", event, None)])
    assert FIXTURE in coalesced and VAULT not in coalesced
    direct = GatewayRunner._redacted_output_tail(session, 1000)
    assert FIXTURE not in direct and VAULT not in direct
