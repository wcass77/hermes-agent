"""Participant adapters and copied weekly histories retain their owning profile."""
import datetime as dt
import json
import os
from pathlib import Path
import sqlite3
from types import SimpleNamespace

import pytest
import yaml

from agent.secret_scope import set_multiplex_active
from gateway.config import GatewayConfig, Platform, PlatformConfig
from gateway.platform_registry import PlatformEntry, platform_registry
from gateway.run import GatewayRunner, _profile_runtime_scope
from hermes_constants import hermes_home_key
from hermes_state import SessionDB
from plugins import zhaan_orchestration
from plugins.platforms.discord.adapter import register as register_discord
from plugins.zhaan_orchestration.shared_update import SharedUpdateService
from plugins.zhaan_orchestration.store import Store


def test_three_discord_owners_keep_conflicting_allowlists(tmp_path, monkeypatch):
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    root = tmp_path / ".hermes"
    root.mkdir()
    monkeypatch.setenv("HERMES_HOME", str(root))
    monkeypatch.setenv("DISCORD_ALLOWED_USERS", "ambient-other-owner")
    runner = object.__new__(GatewayRunner)
    runner.config = GatewayConfig(multiplex_profiles=True)
    runner._primary_profile_name = "default"
    runner.adapters, runner._profile_adapters = {}, {}
    runner.pairing_store, runner.pairing_stores = None, {}
    ids = {"pilot": "11", "scorpius": "22", "zhaan": "33"}
    for name, user in ids.items():
        home = root / "profiles" / name
        home.mkdir(parents=True)
        (home / "config.yaml").write_text("{}\n")
        (home / ".env").write_text(f"DISCORD_ALLOWED_USERS={user}\n")
        if name == "zhaan":
            people = home / "workspace/config/people.yaml"
            people.parent.mkdir(parents=True)
            people.write_text(yaml.safe_dump({"people": [
                {"participant": True, "channels": {"discord_user_id": user}}]}))
    before = dict(os.environ)
    set_multiplex_active(True)
    try:
        for name, user in ids.items():
            home = root / "profiles" / name
            with _profile_runtime_scope(home):
                context = SimpleNamespace(
                    register_hook=lambda *a, **k: None, register_tool=lambda **k: None,
                    register_platform=lambda **kwargs: platform_registry.register(
                        PlatformEntry(**kwargs), scope=hermes_home_key(home)))
                (zhaan_orchestration.register if name == "zhaan" else register_discord)(context)
                entry = platform_registry.get("discord")
                adapter = entry.adapter_factory(PlatformConfig(enabled=True, token="synthetic-" + name))
                adapter.gateway_runner = runner
                adapter.set_owner_profile(name)
                runner._profile_adapters[name] = {Platform.DISCORD: adapter}
                adapter.set_authorization_check(runner._make_adapter_auth_check(Platform.DISCORD, name))
                assert adapter._get_allowed_users() == {user}
        # Callbacks run with no owning turn on the stack: each must rebind its home.
        for name, adapters in runner._profile_adapters.items():
            adapter = adapters[Platform.DISCORD]
            for owner, user in ids.items():
                assert adapter._is_sender_authorized(user, "group", "shared-channel") is (owner == name)
        assert os.environ == before
    finally:
        set_multiplex_active(False)


def test_weekly_copy_adopts_legacy_identity_without_losing_archive_or_receipts(tmp_path):
    original = tmp_path / "original.db"
    db = SessionDB(original)
    legacy_key = "agent:main:discord:group:family"
    db.create_session("old-compressed", "discord", session_key=legacy_key, chat_id="family", chat_type="group")
    db.append_message("old-compressed", "user", "Synthetic prior conversation")
    db.end_session("old-compressed", "compression")
    db.set_session_archived("old-compressed", True)
    db.create_session("active-tip", "discord", parent_session_id="old-compressed", session_key=legacy_key,
                      chat_id="family", chat_type="group")
    db.append_message("active-tip", "assistant", "Synthetic retained receipt")
    db.close()
    copy = tmp_path / "copy.db"
    with sqlite3.connect(f"file:{original}?mode=ro", uri=True) as source, sqlite3.connect(copy) as target:
        source.backup(target)
    sent = []
    service = SharedUpdateService(Store(tmp_path / "orchestration.sqlite"), tmp_path, channel_id="family",
        profile="zhaan", session_db_factory=lambda: SessionDB(copy),
        send_message=lambda *args: sent.append(args) or {"success": True, "message_id": "synthetic-delivery"})
    receipt = service.post("Synthetic shared update", now=dt.datetime.now(dt.UTC))
    assert receipt["success"] and receipt["session_id"] == "active-tip"
    assert len(sent) == 1
    restored = SessionDB(copy)
    try:
        for session in ("old-compressed", "active-tip"):
            row = restored.get_session(session)
            assert row["session_key"] == "agent:zhaan:discord:group:family"
            assert row["transport_profile"] == "zhaan"
            assert json.loads(row["origin_json"])["profile"] == "zhaan"
        assert restored.get_session("old-compressed")["archived"] == 1
        assert [m["content"] for m in restored.get_messages("active-tip")] == [
            "Synthetic retained receipt", "Synthetic shared update"]
        assert restored.get_messages("old-compressed")[0]["content"] == "Synthetic prior conversation"
    finally:
        restored.close()


def test_competing_weekly_histories_stop_before_delivery(tmp_path):
    path = tmp_path / "state.db"
    db = SessionDB(path)
    for name, namespace in (("legacy", "main"), ("current", "zhaan")):
        db.create_session(name, "discord", session_key=f"agent:{namespace}:discord:group:family",
                          chat_id="family", chat_type="group")
    db.close()
    sent = []
    service = SharedUpdateService(Store(tmp_path / "orchestration.sqlite"), tmp_path, channel_id="family",
        profile="zhaan", session_db_factory=lambda: SessionDB(path), send_message=lambda *a: sent.append(a))
    with pytest.raises(RuntimeError, match="two active histories"):
        service.post("Must not be sent")
    assert sent == []


def test_plugin_loader_preserves_scoped_discord_override(tmp_path, monkeypatch):
    from hermes_cli.plugins import discover_plugins
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    root = tmp_path / ".hermes"
    root.mkdir()
    monkeypatch.setenv("HERMES_HOME", str(root))
    homes = {name: root / "profiles" / name for name in ("pilot", "zhaan")}
    for name, home in homes.items():
        home.mkdir(parents=True)
        (home / ".env").write_text("DISCORD_ALLOWED_USERS=11\n")
        enabled = ["platforms/discord"] + (["zhaan_orchestration"] if name == "zhaan" else [])
        (home / "config.yaml").write_text(yaml.safe_dump({"plugins": {"enabled": enabled}}))
        people = home / "workspace/config/people.yaml"
        people.parent.mkdir(parents=True)
        people.write_text(yaml.safe_dump({"people": [{"participant": True, "channels": {"discord_user_id": "33"}}]}))
    set_multiplex_active(True)
    try:
        with _profile_runtime_scope(homes["zhaan"]):
            discover_plugins()
            adapter = platform_registry.get("discord").adapter_factory(PlatformConfig(enabled=True, token="synthetic"))
            assert adapter._get_allowed_users() == {"33"}
        with _profile_runtime_scope(homes["pilot"]):
            discover_plugins()
            adapter = platform_registry.get("discord").adapter_factory(PlatformConfig(enabled=True, token="synthetic"))
            assert adapter._get_allowed_users() == {"11"}
    finally:
        set_multiplex_active(False)
