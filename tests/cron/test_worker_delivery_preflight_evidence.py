"""A detached worker evaluates its own credentials, not the gateway's live adapter.

Use real profile plugin discovery, hydration, worker scope and delivery preflight,
with a synthetic secret source. No execution, model call or message send occurs.
"""
import json
from pathlib import Path

import pytest


@pytest.mark.parametrize("multiplex", [False, True])
@pytest.mark.parametrize("available", [False, True])
def test_detached_worker_preflight_uses_own_credential_snapshot(
    tmp_path, monkeypatch, multiplex, available,
):
    from agent.secret_sources import registry
    from cron import scheduler
    from cron.scheduler_preflight import _preflight_check_delivery
    from hermes_cli.env_loader import reset_secret_source_cache
    from hermes_cli.plugins import _reset_plugin_managers_for_tests

    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    launch = tmp_path / "launch"
    launch.mkdir()
    monkeypatch.setenv("HERMES_HOME", str(launch))
    monkeypatch.delenv("DISCORD_BOT_TOKEN", raising=False)
    profile = tmp_path / "profile"
    plugin = profile / "plugins" / "evidence-vault"
    plugin.mkdir(parents=True)
    values = {"DISCORD_BOT_TOKEN": "synthetic-discord-token-for-test"} if available else {}
    (plugin / "plugin.yaml").write_text("name: evidence-vault\ndescription: synthetic source\n")
    (plugin / "__init__.py").write_text(
        "from agent.secret_sources.base import SECRET_SOURCE_API_VERSION, FetchResult, SecretSource\n"
        "class EvidenceVault(SecretSource):\n"
        "    api_version = SECRET_SOURCE_API_VERSION\n"
        "    name = 'evidencevault'\n"
        "    label = 'Synthetic evidence vault'\n"
        "    shape = 'bulk'\n"
        "    def fetch(self, cfg, home_path):\n"
        f"        return FetchResult(secrets={values!r})\n"
        "def register(ctx):\n"
        "    ctx.register_secret_source(EvidenceVault())\n"
    )
    (profile / "config.yaml").write_text(
        "secrets:\n  sources: [evidencevault]\n  evidencevault:\n    enabled: true\n"
        "plugins:\n  enabled: [evidence-vault]\n"
        "platforms:\n  discord:\n    enabled: true\n"
    )
    payload = tmp_path / "payload.json"
    payload.write_text(json.dumps({
        "job": {"id": "synthetic", "execution_id": "synthetic-run", "deliver": "discord:12345"},
        "profile_home": str(profile), "multiplex_active": multiplex,
    }))
    monkeypatch.setattr("cron.executions.adopt_claimed_execution", lambda _: {"status": "running"})
    observed = []

    def inspect_only(job, *, adapters, loop, verbose):
        observed.append((adapters, _preflight_check_delivery(job)))
        return True

    monkeypatch.setattr(scheduler, "run_one_job", inspect_only)
    try:
        assert scheduler._run_external_worker_payload(payload, tmp_path / "ack")
        assert len(observed) == 1
        adapters, reason = observed[0]
        assert adapters is None
        if available:
            assert reason is None
        else:
            assert "no gateway credentials configured" in reason
    finally:
        registry._reset_registry_for_tests()
        reset_secret_source_cache()
        _reset_plugin_managers_for_tests()
