from __future__ import annotations

import base64
import datetime as dt
import hashlib
import hmac
import importlib
import json
import os
from pathlib import Path

import pytest
import yaml

from hermes_cli.plugins import PluginManager


plugin = importlib.import_module("plugins.zhaan_orchestration")
store_mod = importlib.import_module("plugins.zhaan_orchestration.store")
webhook = importlib.import_module("plugins.zhaan_orchestration.webhook")
shared_update = importlib.import_module("plugins.zhaan_orchestration.shared_update")


def test_disabled_by_default_registers_no_tools(tmp_path, monkeypatch):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    manager = PluginManager()
    manager.discover_and_load()
    loaded = manager._plugins["zhaan_orchestration"]
    assert not loaded.enabled
    assert loaded.tools_registered == []


def test_signature_timestamp_and_replay_queue(tmp_path):
    secret_raw = b"test secret"
    secret = "whsec_" + base64.b64encode(secret_raw).decode()
    now = dt.datetime.now(dt.timezone.utc)
    timestamp = str(int(now.timestamp()))
    body = json.dumps({"event_id": "evt", "message": {"message_id": "msg", "thread_id": "thr", "inbox_id": "in"}}).encode()
    webhook_id = "wh_1"
    digest = base64.b64encode(hmac.new(secret_raw, f"{webhook_id}.{timestamp}.".encode() + body, hashlib.sha256).digest()).decode()
    headers = {"svix-id": webhook_id, "svix-timestamp": timestamp, "svix-signature": f"v1,{digest}"}
    assert webhook.verify_svix(body, headers, secret, now=now) == webhook_id
    with pytest.raises(ValueError):
        webhook.verify_svix(body + b"x", headers, secret, now=now)
    with pytest.raises(ValueError):
        webhook.verify_svix(body, headers, secret, now=now + dt.timedelta(minutes=6))

    store = store_mod.Store(tmp_path / "queue.sqlite")
    payload = {"event_id": "evt", "message": {"message_id": "msg", "thread_id": "thr", "inbox_id": "in"}}
    assert store.enqueue(webhook_id, payload, "person@example.com") == "queued"
    assert store.enqueue(webhook_id, payload, "person@example.com") == "duplicate"
    assert store.counts() == {"queued": 1}


def test_stable_sessions_and_context_only_delta(tmp_path):
    store = store_mod.Store(tmp_path / "state.sqlite")
    assert store.session_for_email_thread("a") == store.session_for_email_thread("a")
    session = store.reminder_session("willy", "2026-08-04", "main")
    assert session == store.reminder_session("willy", "2026-08-04", "main")
    appended = []
    messages = [{"role": "user", "content": "one"}, {"role": "assistant", "content": "two"}]
    assert store.sync_reminder_context("willy", "2026-08-04", messages, lambda sid, msg: appended.append((sid, msg))) == 2
    assert all(item[1]["context_only"] for item in appended)
    assert store.sync_reminder_context("willy", "2026-08-04", messages, lambda *_: None) == 0


def test_queue_claim_retry_recovery_and_completion(tmp_path):
    store = store_mod.Store(tmp_path / "state.sqlite")
    payload = {"event_id": "evt", "message": {"message_id": "msg", "thread_id": "thr", "inbox_id": "in"}}
    store.enqueue("wh", payload, "person@example.com")
    item = store.claim_next("worker")
    assert item["event_id"] == "evt"
    assert store.claim_next("other") is None
    assert store.recover_processing() == 1
    item = store.claim_next("worker")
    assert item["attempts"] == 2
    assert store.fail("evt", "temporary", delay_seconds=0) == "retry"
    assert store.claim_next("worker") is not None
    store.complete("evt")
    assert store.counts() == {"complete": 1}


def test_discord_unknown_user_is_dropped(tmp_path, monkeypatch):
    people = tmp_path / "people.yaml"
    people.write_text(yaml.safe_dump({"people": [{"id": "willy", "participant": True, "channels": {"discord_user_id": "42"}}]}))
    monkeypatch.setenv("ZHAAN_PEOPLE_REGISTRY", str(people))
    source = type("Source", (), {"platform": "discord", "user_id": "99"})()
    event = type("Event", (), {"source": source})()
    assert plugin.pre_gateway_dispatch(event)["action"] == "skip"
    source.user_id = "42"
    assert plugin.pre_gateway_dispatch(event)["action"] == "allow"


def test_register_derives_core_discord_allowlist_from_people(tmp_path, monkeypatch):
    people = tmp_path / "people.yaml"
    people.write_text(yaml.safe_dump({"people": [
        {"id": "willy", "participant": True, "channels": {"discord_user_id": "42"}},
        {"id": "other", "participant": True, "channels": {"discord_user_id": "7"}},
        {"id": "child", "participant": False},
    ]}))
    monkeypatch.setenv("ZHAAN_PEOPLE_REGISTRY", str(people))
    monkeypatch.delenv("DISCORD_ALLOWED_USERS", raising=False)
    monkeypatch.setattr(plugin, "_start_server", lambda: None)
    hooks = []
    tools = []
    context = type("Context", (), {
        "register_hook": lambda self, name, hook: hooks.append((name, hook)),
        "register_tool": lambda self, **kwargs: tools.append(kwargs),
    })()

    plugin.register(context)

    assert os.environ["DISCORD_ALLOWED_USERS"] == "42,7"
    assert hooks == [("pre_gateway_dispatch", plugin.pre_gateway_dispatch)]
    assert [item["name"] for item in tools] == ["post_shared_update"]
    schema = tools[0]["schema"]["parameters"]
    assert schema["required"] == ["message"]
    assert set(schema["properties"]) == {"message"}


def test_register_starts_ingress_only_in_gateway(tmp_path, monkeypatch):
    people = tmp_path / "people.yaml"
    people.write_text(yaml.safe_dump({"people": [
        {"id": "willy", "participant": True, "channels": {"discord_user_id": "42"}},
    ]}))
    monkeypatch.setenv("ZHAAN_PEOPLE_REGISTRY", str(people))
    starts = []
    monkeypatch.setattr(plugin, "_start_server", lambda: starts.append(True))
    context = type("Context", (), {
        "register_hook": lambda self, *_: None,
        "register_tool": lambda self, **_: None,
    })()

    monkeypatch.delenv("_HERMES_GATEWAY", raising=False)
    monkeypatch.setattr(plugin.sys, "argv", ["hermes", "--profile", "zhaan", "chat", "-q", "hello"])
    plugin.register(context)
    assert starts == []

    monkeypatch.setattr(plugin.sys, "argv", ["hermes", "--profile", "zhaan", "gateway", "run"])
    plugin.register(context)
    assert starts == [True]

    starts.clear()
    monkeypatch.setattr(plugin.sys, "argv", ["hermes", "--profile", "zhaan", "chat", "-q", "hello"])
    monkeypatch.setenv("_HERMES_GATEWAY", "1")
    plugin.register(context)
    assert starts == [True]


class FakeSessionDB:
    def __init__(self, *, fail_append=False):
        self.sessions = []
        self.peers = []
        self.messages = []
        self.fail_append = fail_append

    def create_session(self, session_id, source, **kwargs):
        self.sessions.append((session_id, source, kwargs))

    def reopen_session(self, _session_id):
        return None

    def record_gateway_session_peer(self, session_id, **kwargs):
        self.peers.append((session_id, kwargs))

    def append_message(self, **kwargs):
        if self.fail_append:
            raise RuntimeError("state unavailable")
        self.messages.append(kwargs)

    def close(self):
        return None


def test_shared_update_creates_one_daily_thread_and_mirrors(tmp_path):
    store = store_mod.Store(tmp_path / "orchestration.sqlite")
    requests = []
    created = []
    sent = []
    db = FakeSessionDB()

    def request(method, path, _token, **kwargs):
        requests.append((method, path, kwargs))
        if path == "/channels/123":
            return {"guild_id": "guild"}
        if path == "/guilds/guild/threads/active":
            return {"threads": []}
        if path == "/channels/123/threads/archived/public":
            return {"threads": []}
        if path == "/channels/456":
            return {"id": "456", "parent_id": "123", "name": "Tuesday, August 4, 2026"}
        raise AssertionError((method, path))

    def create(_token, channel_id, name):
        created.append((channel_id, name))
        return {"success": True, "thread_id": "456", "name": name}

    def send(target, message):
        sent.append((target, message))
        return {"success": True, "message_id": "789"}

    service = shared_update.SharedUpdateService(
        store, tmp_path, channel_id="123", discord_request=request,
        create_thread=create, send_message=send, session_db_factory=lambda: db,
    )
    instant = dt.datetime(2026, 8, 4, 16, tzinfo=dt.timezone.utc)
    first = service.post("School closes at 2 PM.", now=instant)
    second = service.post("Pickup is at the west door.", now=instant)

    assert first["success"] and first["context_mirrored"]
    assert second["success"] and second["context_mirrored"]
    assert created == [("123", "Tuesday, August 4, 2026")]
    assert sent == [
        ("discord:456:456", "School closes at 2 PM."),
        ("discord:456:456", "Pickup is at the west door."),
    ]
    assert [item["content"] for item in db.messages] == [
        "School closes at 2 PM.", "Pickup is at the west door.",
    ]
    assert store.daily_thread("2026-08-04")["session_id"] == "zhaan-daily-2026-08-04"


def test_shared_update_reports_visible_but_unmirrored_partial_failure(tmp_path):
    store = store_mod.Store(tmp_path / "orchestration.sqlite")
    db = FakeSessionDB(fail_append=True)

    def request(_method, path, _token, **_kwargs):
        if path == "/channels/456":
            return {"id": "456", "parent_id": "123", "name": "Tuesday, August 4, 2026"}
        if path == "/channels/123":
            return {"guild_id": "guild"}
        return {"threads": [{"id": "456", "parent_id": "123", "name": "Tuesday, August 4, 2026"}]}

    service = shared_update.SharedUpdateService(
        store, tmp_path, channel_id="123", discord_request=request,
        send_message=lambda *_: {"success": True, "message_id": "789"},
        session_db_factory=lambda: db,
    )
    result = service.post(
        "Update", now=dt.datetime(2026, 8, 4, 16, tzinfo=dt.timezone.utc)
    )
    assert not result["success"]
    assert result["discord_sent"]
    assert not result["context_mirrored"]
    assert result["message_id"] == "789"
