"""Unloaded platform plugins cannot erase their persisted conversation routes."""
import json
from datetime import datetime, UTC

import pytest

from gateway.config import GatewayConfig, Platform
from gateway.platform_registry import PlatformEntry, platform_registry
from gateway.session import SessionEntry, SessionStore


@pytest.mark.parametrize('legacy_only', [False, True])
def test_unavailable_route_survives_save_and_recovers_after_registration(tmp_path, monkeypatch, legacy_only):
    import hermes_constants
    monkeypatch.setattr(hermes_constants, 'get_hermes_home', lambda: tmp_path)
    name = 'unavailable-mail-' + ('legacy' if legacy_only else 'durable')
    key = 'agent:pilot:' + name + ':dm:owner'
    now = datetime.now(UTC)
    entry = SessionEntry(session_key=key, session_id='retained-session', created_at=now, updated_at=now).to_dict()
    entry.update(platform=name, transport_profile='pilot', origin={
        'platform': name, 'chat_id': 'thread', 'chat_type': 'dm', 'user_id': 'owner', 'profile': 'pilot'})
    store = SessionStore(tmp_path / 'sessions', GatewayConfig())
    if legacy_only:
        store.sessions_dir.mkdir()
        (store.sessions_dir / 'sessions.json').write_text(json.dumps({key: entry}))
    else:
        store._routing_db.save_gateway_routing_entry(key, json.dumps(entry), scope=store._routing_scope())
    assert store.list_sessions() == []
    with store._lock:
        store._save()
    assert json.loads(store._routing_db.load_gateway_routing_entries(scope=store._routing_scope())[key]) == entry
    assert json.loads((store.sessions_dir / 'sessions.json').read_text())[key] == entry
    platform_registry.register(PlatformEntry(name=name, label='Synthetic mail', adapter_factory=lambda cfg: None,
                                             check_fn=lambda: True, source='plugin'))
    try:
        restored = store.list_sessions()
        assert len(restored) == 1
        assert restored[0].session_id == 'retained-session'
        assert restored[0].origin.platform == Platform(name)
        assert restored[0].transport_profile == 'pilot'
    finally:
        platform_registry.unregister(name)
        store._routing_db.close()


@pytest.mark.parametrize('operation', ['rename', 'delete_profile', 'delete_session'])
def test_dormant_routes_follow_profile_and_session_lifecycle(tmp_path, monkeypatch, operation):
    import hermes_constants
    monkeypatch.setattr(hermes_constants, 'get_hermes_home', lambda: tmp_path)
    name = 'dormant-' + operation
    key = 'agent:pilot:' + name + ':dm:owner'
    now = datetime.now(UTC)
    data = SessionEntry(session_key=key, session_id='retained', created_at=now, updated_at=now).to_dict()
    data['origin'] = {'platform': name, 'chat_id': 'thread', 'profile': 'pilot'}
    store = SessionStore(tmp_path / 'sessions', GatewayConfig())
    store._routing_db.save_gateway_routing_entry(key, json.dumps(data), scope=store._routing_scope())
    assert store.list_sessions() == []
    if operation == 'rename':
        assert store.rekey_profile_routing('pilot', 'new') == 1
        rows = store._routing_db.load_gateway_routing_entries(scope=store._routing_scope())
        new_key = key.replace('agent:pilot:', 'agent:new:')
        assert key not in rows
        assert json.loads(rows[new_key])['origin']['profile'] == 'new'
    elif operation == 'delete_profile':
        assert store.purge_profile_routing('pilot') == 1
        assert store._routing_db.load_gateway_routing_entries(scope=store._routing_scope()) == {}
    else:
        assert store.remove_by_session_id('retained') == 1
        assert store._routing_db.load_gateway_routing_entries(scope=store._routing_scope()) == {}
    store._routing_db.close()
