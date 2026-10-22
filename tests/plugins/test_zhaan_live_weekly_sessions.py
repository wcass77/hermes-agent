"""Weekly updates follow a verified owning gateway until its mode actually changes."""
import json
import os
from pathlib import Path

import pytest
import yaml

from gateway import status
from gateway.config import GatewayConfig, Platform
from gateway.run import _profile_runtime_scope
from gateway.session import SessionSource, SessionStore
from hermes_state import SessionDB
from plugins.zhaan_orchestration.shared_update import configured_service


@pytest.fixture
def owned_gateway(tmp_path, monkeypatch):
    import hermes_constants

    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    root = tmp_path / ".hermes"
    home = root / "profiles/zhaan"
    home.mkdir(parents=True)
    monkeypatch.setenv("HERMES_HOME", str(home))
    monkeypatch.setenv("HERMES_GATEWAY_LOCK_DIR", str(tmp_path / "locks"))
    monkeypatch.setattr(hermes_constants, "_default_hermes_root_memo", None)
    (root / "config.yaml").write_text("gateway: {multiplex_profiles: true}\n")
    config = {"gateway": {"standalone": True}, "plugins": {"entries": {
        "zhaan_orchestration": {"coordination_channel_id": "family"}}}}
    (home / "config.yaml").write_text(yaml.safe_dump(config))
    runtime = {"pid": os.getpid(), "hermes_home": str(home), "gateway_state": "running",
        "start_time": status._get_process_start_time(os.getpid()), "served_profiles": []}
    (home / "gateway_state.json").write_text(json.dumps(runtime))
    original_cmdline = status._read_process_cmdline
    monkeypatch.setattr(status, "_read_process_cmdline", lambda pid: (
        "python -m hermes_cli.main --profile zhaan gateway run"
        if pid == os.getpid() else original_cmdline(pid)))
    assert status.live_gateway_pid_for_home(home) == os.getpid()
    assert status.live_gateway_pid_for_home(root) is None
    return home, config, runtime


@pytest.mark.parametrize("live_multiplex", [False, True])
@pytest.mark.parametrize("edit_config", [False, True])
def test_owned_gateway_keeps_weekly_history_until_runtime_changes(
    tmp_path, owned_gateway, live_multiplex, edit_config,
):
    from hermes_cli.gateway_multiplex_mode import standalone_launcher_decision
    from hermes_cli.gateway_multiplex_served import recorded_served_profiles

    home, config, runtime = owned_gateway
    config["gateway"]["standalone"] = not live_multiplex
    (home / "config.yaml").write_text(yaml.safe_dump(config))
    runtime["served_profiles"] = ["zhaan", "pilot"] if live_multiplex else []
    (home / "gateway_state.json").write_text(json.dumps(runtime))
    assert recorded_served_profiles() is None
    with _profile_runtime_scope(home, hydrate_secrets=False):
        runtime_config = GatewayConfig(multiplex_profiles=live_multiplex)
        standalone_launcher_decision(runtime_config)
        store = object.__new__(SessionStore)
        store.config = runtime_config
        source = SessionSource(platform=Platform.DISCORD, chat_id="family", chat_type="group", profile="zhaan")
        active_key = store._generate_session_key(source)
        assert configured_service().profile == store._resolve_profile_for_key(source)
        if edit_config:
            config["gateway"]["standalone"] = live_multiplex
            (home / "config.yaml").write_text(yaml.safe_dump(config))
        assert status.live_gateway_pid_for_home(home) == os.getpid()
        path = tmp_path / "synthetic-state.db"
        db = SessionDB(path)
        db.create_session("live-weekly", "discord", session_key=active_key, chat_id="family", chat_type="group")
        db.append_message("live-weekly", "user", "Synthetic existing weekly context")
        db.close()
        service = configured_service()
        service._session_db_factory_override = lambda: SessionDB(path)
        service._send_message_override = lambda *a: {"success": True, "message_id": "synthetic-receipt"}
        receipt = service.post("Synthetic transition update")
        db = SessionDB(path)
        try:
            assert receipt["success"] and receipt["session_id"] == "live-weekly"
            assert db.get_session("live-weekly")["session_key"] == active_key
            assert [row["content"] for row in db.get_messages("live-weekly")] == [
                "Synthetic existing weekly context", "Synthetic transition update"]
        finally:
            db.close()


@pytest.mark.parametrize("stale_reason", ["stopped", "reused_pid"])
def test_stale_own_gateway_record_cannot_override_config(owned_gateway, stale_reason):
    home, config, runtime = owned_gateway
    config["gateway"]["standalone"] = False
    (home / "config.yaml").write_text(yaml.safe_dump(config))
    if stale_reason == "stopped":
        runtime["gateway_state"] = "stopped"
    else:
        runtime["start_time"] = float(runtime["start_time"]) + 1000
    (home / "gateway_state.json").write_text(json.dumps(runtime))
    assert status.live_gateway_pid_for_home(home) is None
    with _profile_runtime_scope(home, hydrate_secrets=False):
        assert configured_service().profile == "zhaan"


def test_live_own_gateway_with_unknown_mode_refuses_before_delivery(owned_gateway):
    home, _, runtime = owned_gateway
    del runtime["served_profiles"]
    (home / "gateway_state.json").write_text(json.dumps(runtime))
    assert status.live_gateway_pid_for_home(home) == os.getpid()
    with _profile_runtime_scope(home, hydrate_secrets=False):
        with pytest.raises(RuntimeError, match="live gateway.*session mode"):
            configured_service()
