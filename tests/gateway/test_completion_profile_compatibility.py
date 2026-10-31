"""Fabricated review diagnostics only; never reads live Hermes data."""
import json
from pathlib import Path

import pytest
import pytest_asyncio



@pytest_asyncio.fixture
async def fixture_runtime(tmp_path, monkeypatch):
    monkeypatch.setattr(Path, 'home', lambda: tmp_path)
    home = tmp_path / '.hermes'
    beta = home / 'profiles' / 'beta'
    beta.mkdir(parents=True)
    for path in (home, beta):
        (path / 'config.yaml').write_text('{}', encoding='utf-8')
        (path / '.env').write_text('DISCORD_ALLOWED_USERS=42\n', encoding='utf-8')
    monkeypatch.setenv('HERMES_HOME', str(home))
    import hermes_state
    monkeypatch.setattr(hermes_state, 'DEFAULT_DB_PATH', hermes_state._IMPORT_DEFAULT_DB_PATH)
    monkeypatch.setenv('HERMES_RUNTIME_DIR', str(tmp_path / 'runtime'))
    monkeypatch.setenv('DISCORD_ALLOWED_USERS', '42')
    from gateway.config import GatewayConfig, Platform, PlatformConfig
    from gateway.run import GatewayRunner, _profile_runtime_scope
    from gateway.session_authorities import SessionAuthorities
    from gateway.session_authority import SessionAuthority, initialize_session_authority
    from plugins.platforms.discord.adapter import DiscordAdapter
    monkeypatch.setattr(SessionAuthority, '_schedule', lambda self, ref: None)
    runner = GatewayRunner(GatewayConfig(multiplex_profiles=True))
    runner.config._runtime_profile_homes = [('default', home), ('beta', beta)]
    adapter = DiscordAdapter(PlatformConfig(enabled=True, token='fabricated', typing_indicator=False))
    runner.adapters = {Platform.DISCORD: adapter}
    runner._wire_adapter_handlers(adapter)
    registry = SessionAuthorities(home, multiplexed=True)
    runner.session_authorities = registry
    for name, path in [('default', home), ('beta', beta)]:
        with _profile_runtime_scope(path, prepared_secret_scope={}):
            authority = await initialize_session_authority(runner, profile_id=str(path), instance_id=name,
                                                          register=name == 'default')
            registry.add(path, authority, name=name)
    try:
        yield runner, adapter, registry, home, beta
    finally:
        runner.close_all_session_db_handles()
        runner.session_store.close_all_db_handles()


@pytest.mark.asyncio
async def test_ordinary_default_retry_keeps_pre_upgrade_principal(fixture_runtime):
    from gateway.platforms.event import MessageEvent
    from gateway.session_ingress_context import native_callback
    from gateway.run import _profile_runtime_scope
    from hermes_state_runtime import list_session_admissions
    runner, adapter, registry, home, _ = fixture_runtime
    authority = registry.for_home(home)
    source = adapter.build_source(chat_id='42', chat_type='dm', user_id='42')
    assert source.profile is None  # ordinary default intake, no profile_routes override
    event = MessageEvent(text='fabricated human input', source=source, message_id='fabricated-msg-1')
    with _profile_runtime_scope(home, prepared_secret_scope={}):
        with native_callback(runner, event, home):
            first = await authority.admit_native(event)
        rows = list_session_admissions(authority.db, session_id=first.ref.session_id, pending_only=False)
        assert 'profile' not in rows[0]['payload']['native_text_v1']['source']
        legacy_principal = 'messaging:' + json.dumps([None, 'discord', '42', None, '42'], separators=(',', ':'))
        # Fabricate the exact principal that base _admit_native derived from this serialized source.
        authority.db._write_sql('UPDATE session_admissions SET principal_id=? WHERE admission_id=?',
                                (legacy_principal, first.admission_id))
        with native_callback(runner, event, home):
            retry = await authority.admit_native(event)
        rows = list_session_admissions(authority.db, session_id=first.ref.session_id, pending_only=False)
        assert retry.admission_id == first.admission_id
        assert len(rows) == 1


@pytest.mark.asyncio
async def test_native_recovery_contains_unserved_legacy_route(fixture_runtime):
    from gateway.platforms.event import MessageEvent
    from gateway.session_ingress_context import native_callback
    from gateway.run import _profile_runtime_scope
    from gateway.profile_routing import ProfileRoute
    from hermes_state_runtime import list_session_admissions
    runner, adapter, registry, home, beta = fixture_runtime
    runner.config.profile_routes = [ProfileRoute(name='fabricated', platform='discord', chat_id='42', profile='beta')]
    authority = registry.for_home(beta)
    source = adapter.build_source(chat_id='42', chat_type='dm', user_id='42')
    source.profile = 'beta'
    event = MessageEvent(text='fabricated queued input', source=source, message_id='fabricated-msg-2')
    with _profile_runtime_scope(beta, prepared_secret_scope={}):
        with native_callback(runner, event, home):
            first = await authority.admit_native(event)
        row = list_session_admissions(authority.db, session_id=first.ref.session_id, pending_only=False)[0]
        row['payload']['native_text_v1']['source'].pop('profile', None)
        authority.db._write_sql('UPDATE session_admissions SET payload_json=? WHERE admission_id=?',
                                (json.dumps(row['payload']), first.admission_id))
        # A late preflight can still hold this authority after its registry slot is withdrawn.
        registry.remove(beta)
        outcome = await authority.recover_native_sessions([(first.ref.session_id, source, adapter)])
        assert outcome[first.ref.session_id] == 'profile_mismatch'


@pytest.mark.asyncio
async def test_default_profile_plugin_wake_accepts_ordinary_origin(fixture_runtime):
    from gateway.platforms.event import MessageEvent
    from gateway.session_ingress_context import native_callback
    from gateway.run import _profile_runtime_scope
    from hermes_state_runtime import list_session_admissions
    runner, adapter, registry, home, _ = fixture_runtime
    authority = registry.for_home(home)
    source = adapter.build_source(chat_id='42', chat_type='dm', user_id='42')
    assert source.profile is None
    human = MessageEvent(text='fabricated human', source=source, message_id='fabricated-msg-3')
    with _profile_runtime_scope(home, prepared_secret_scope={}):
        with native_callback(runner, human, home):
            receipt = await authority.admit_native(human)
        key = runner.session_store._generate_session_key(source)
        entry = runner.session_store.lookup_by_session_key(key)
        # Ordinary pre-upgrade default origins have an absent profile, not an explicit "default".
        entry.origin.profile = None
        restored = runner._restored_source(entry)
        accepted = await runner._admit_plugin_injection(adapter, restored, session_key=key,
            session_id=receipt.ref.session_id, content='fabricated plugin wake', plugin_id='fabricated-review')
        rows = list_session_admissions(authority.db, session_id=receipt.ref.session_id, pending_only=False)
        assert accepted is True
        assert len(rows) == 2


@pytest.mark.asyncio
async def test_default_relay_completion_keeps_sealed_source_identity(fixture_runtime):
    from gateway.config import Platform, PlatformConfig
    from gateway.platforms.event import MessageEvent
    from gateway.relay.adapter import RelayAdapter
    from gateway.relay.descriptor import CapabilityDescriptor
    from gateway.relay.ws_transport import WebSocketRelayTransport
    from gateway.session import SessionSource
    from gateway.session_ingress_context import native_callback, relay_callback
    from gateway.run import _profile_runtime_scope
    from hermes_state_runtime import list_session_admissions
    runner, _, registry, home, _ = fixture_runtime
    authority = registry.for_home(home)
    descriptor = CapabilityDescriptor(1, 'telegram', 'Fabricated', 4096, False, True, False, 'plain', 'chars')
    transport = WebSocketRelayTransport('ws://127.0.0.1:1/not-connected', 'telegram', 'fabricated-bot',
        gateway_id='fabricated-gateway', upgrade_secret='fabricated-secret')
    transport._ws = object()  # no connection, send, or reader ever starts
    transport._descriptors_by_platform['telegram'] = descriptor
    adapter = RelayAdapter(PlatformConfig(enabled=True), descriptor, transport)
    runner.adapters = {Platform.RELAY: adapter}
    runner._wire_adapter_handlers(adapter)
    source = SessionSource(platform=Platform.TELEGRAM, chat_id='42', user_id='42', chat_type='dm',
                           delivered_via_upstream_relay=True)
    human = MessageEvent(text='fabricated relay input', source=source, message_id='fabricated-msg-4')
    with _profile_runtime_scope(home, prepared_secret_scope={}):
        with relay_callback(transport, human), native_callback(runner, human, home):
            first = await authority.admit_native(human)
        key = runner.session_store._generate_session_key(source)
        row = list_session_admissions(authority.db, session_id=first.ref.session_id, pending_only=False)[0]
        assert 'relay' in row['payload']['native_text_v1']['provenance']
        assert 'profile' not in row['payload']['native_text_v1']['source']
        completion_source = runner._build_process_event_source({'session_key': key})
        wake = MessageEvent(text='fabricated completion', source=completion_source, internal=True,
            metadata={'gateway_session_key': key, 'gateway_session_id': first.ref.session_id})
        receipt = await authority.admit_automation(adapter, wake, 'fabricated-relay-completion')
        assert receipt is not None
        assert not adapter._background_tasks
