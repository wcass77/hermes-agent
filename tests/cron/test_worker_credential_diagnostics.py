"""Persist worker lookup outcomes without retaining source payloads or errors."""
import hashlib
import json
from pathlib import Path

import pytest


@pytest.mark.parametrize("result,category,present", [
    ("success", "success", True),
    ("timeout", "timeout", False),
    ("auth_failed", "auth_failed", False),
    ("missing", None, False),
])
def test_worker_receipt_is_private_and_profile_scoped(tmp_path, monkeypatch, result, category, present):
    from agent.secret_sources import registry
    from cron import scheduler
    from cron.scheduler_preflight import _preflight_check_delivery
    from hermes_cli.env_loader import reset_secret_source_cache
    from hermes_cli.plugins import _reset_plugin_managers_for_tests

    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    launch = tmp_path / "launch"
    launch.mkdir()
    (launch / ".env").write_text("DISCORD_BOT_TOKEN=sibling-token-must-not-leak\n")
    monkeypatch.setenv("HERMES_HOME", str(launch))
    monkeypatch.setenv("DISCORD_BOT_TOKEN", "sibling-token-must-not-leak")
    profile = tmp_path / "profile"
    plugin = profile / "plugins" / "diagnostic-vault"
    plugin.mkdir(parents=True)
    expression = ('FetchResult(secrets={"DISCORD_BOT_TOKEN": "private-token-marker"})'
                  if present else f'FetchResult().fail("private-error-marker", ErrorKind.{result.upper()})')
    (plugin / "plugin.yaml").write_text("name: diagnostic-vault\ndescription: synthetic source\n")
    (plugin / "__init__.py").write_text(
        "from agent.secret_sources.base import SECRET_SOURCE_API_VERSION, FetchResult, SecretSource, ErrorKind\n"
        "class Vault(SecretSource):\n"
        "    api_version = SECRET_SOURCE_API_VERSION\n"
        "    name = 'diagnosticvault'\n"
        "    label = 'private-label-marker'\n"
        "    shape = 'bulk'\n"
        "    def fetch(self, cfg, home_path):\n"
        f"        return {expression}\n"
        "def register(ctx):\n"
        "    ctx.register_secret_source(Vault())\n"
    )
    source = "unregisteredsource" if result == "missing" else "diagnosticvault"
    (profile / "config.yaml").write_text(
        f"secrets:\n  sources: [{source}]\n  {source}:\n    enabled: true\n"
        "plugins:\n  enabled: [diagnostic-vault]\n"
        "platforms:\n  discord:\n    enabled: true\n"
    )
    payload = tmp_path / "payload.json"
    payload.write_text(json.dumps({"job": {"id": "safe-job", "execution_id": "safe-run", "deliver": "discord:123"},
                                   "profile_home": str(profile), "multiplex_active": True}))
    monkeypatch.setattr("cron.executions.adopt_claimed_execution", lambda _: {"status": "running"})
    verdicts = []

    def inspect_only(job, **kwargs):
        verdicts.append(_preflight_check_delivery(job))
        return True

    monkeypatch.setattr(scheduler, "run_one_job", inspect_only)
    try:
        assert scheduler._run_external_worker_payload(payload, tmp_path / "ack")
        path = profile / "cron/credential-diagnostics" / (hashlib.sha256(b"safe-run").hexdigest() + ".json")
        receipt = json.loads(path.read_text())
        assert receipt["home"] == str(profile.resolve())
        assert receipt["discord_supplied"] is present
        attempts = receipt["attempts"]
        assert attempts and all(event["home"] == str(profile.resolve()) for event in attempts)
        if category is None:
            assert any(event["outcome"] == "no_sources" for event in attempts)
        else:
            assert any({"source": "diagnosticvault", "category": category} in event["sources"]
                       for event in attempts)
        assert (verdicts[0] is None) is present
        assert path.stat().st_mode & 0o777 == 0o600
        assert not list((launch / "cron").glob("**/*.json"))
        for marker in ["private-token-marker", "private-error-marker", "private-label-marker", "sibling-token-must-not-leak"]:
            assert marker not in path.read_text()
    finally:
        registry._reset_registry_for_tests()
        reset_secret_source_cache()
        _reset_plugin_managers_for_tests()


def test_receipt_failure_does_not_change_job_admission(tmp_path, monkeypatch, caplog):
    from cron.worker_credentials import persist_worker_credentials

    def unavailable(*args, **kwargs):
        raise OSError("private-os-error-marker")

    monkeypatch.setattr("utils.atomic_json_write", unavailable)
    persist_worker_credentials(tmp_path, "safe-run", {"safe": True})
    assert "receipt unavailable" in caplog.text
    assert "private-os-error-marker" not in caplog.text


def test_receipt_retention_preserves_unrelated_files(tmp_path):
    from cron.worker_credentials import persist_worker_credentials

    directory = tmp_path / "cron/credential-diagnostics"
    directory.mkdir(parents=True)
    for i in range(130):
        (directory / (hashlib.sha256(str(i).encode()).hexdigest() + ".json")).write_text("{}")
    unrelated = directory / "user-note.json"
    unrelated.write_text("keep")
    persist_worker_credentials(tmp_path, "latest", {"safe": True})
    assert len(list(directory.glob("*.json"))) == 129  # 128 receipts plus unrelated note
    assert unrelated.read_text() == "keep"
    assert (directory / (hashlib.sha256(b"latest").hexdigest() + ".json")).exists()
