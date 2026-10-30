"""Legacy origins must not select the launch DB (#114609, missing-field half of #143447)."""
from pathlib import Path
from types import SimpleNamespace

import pytest

from gateway.config import GatewayConfig, Platform, PlatformConfig
from gateway.run import GatewayRunner, _profile_runtime_scope
from gateway.session import SessionSource
from gateway.session_identity import IdentityUnresolved, identity_of


@pytest.fixture
def homes(tmp_path, monkeypatch):
    monkeypatch.setattr(Path, 'home', lambda: tmp_path)
    launch = tmp_path / '.hermes'
    beta = launch / 'profiles' / 'beta'
    beta.mkdir(parents=True)
    for home in (launch, beta):
        (home / 'config.yaml').write_text('{}')
    monkeypatch.setenv('HERMES_HOME', str(launch))
    import hermes_state
    monkeypatch.setattr(hermes_state, 'DEFAULT_DB_PATH', hermes_state._IMPORT_DEFAULT_DB_PATH)
    return launch, beta


@pytest.mark.parametrize('cached', [False, True])
def test_legacy_source_uses_namespace_without_changing_transport(homes, cached):
    runner = GatewayRunner(GatewayConfig(multiplex_profiles=True))
    key = 'agent:beta:discord:dm:42'
    source = SessionSource(platform=Platform.DISCORD, chat_type='dm', chat_id='42')
    entry = SimpleNamespace(origin=source, transport_profile='default')
    runner.session_store._ensure_loaded = lambda: None
    runner.session_store._entries = {} if cached else {key: entry}
    runner._get_cached_session_source = lambda key: runner._restored_source(entry)
    try:
        resolved = runner._build_process_event_source({'session_key': key})
        assert resolved.profile == 'beta'
        assert identity_of(resolved).runtime_home == homes[1]
        assert identity_of(resolved).transport_profile == 'default'
        assert source.profile is None
        source.profile = 'default'
        with pytest.raises(IdentityUnresolved):
            runner._build_process_event_source({'session_key': key})
    finally:
        runner.close_all_session_db_handles()
        runner.session_store.close_all_db_handles()


@pytest.mark.asyncio
@pytest.mark.parametrize('profile', ['default', 'beta'])
@pytest.mark.parametrize('legacy_envelope', [False, True])
async def test_dispatch_legacy_completion_admits_once_in_owner_profile(homes, monkeypatch, profile, legacy_envelope):
    from gateway.platforms.event import MessageEvent
    from gateway.session_authorities import SessionAuthorities
    from gateway.session_authority import SessionAuthority, initialize_session_authority
    from hermes_state_runtime import list_session_admissions
    from plugins.platforms.discord.adapter import DiscordAdapter
    from gateway.session_ingress_context import native_callback
    from concurrent.futures import ThreadPoolExecutor
    from queue import Queue
    import asyncio
    from tools.process_registry import process_registry
    from tools import async_delegation as delegation

    launch, beta = homes
    home = beta if profile == 'beta' else launch
    monkeypatch.setattr(SessionAuthority, '_schedule', lambda self, ref: None)
    monkeypatch.setenv('DISCORD_ALLOWED_USERS', '42')
    runner = GatewayRunner(GatewayConfig(multiplex_profiles=True))
    adapter = DiscordAdapter(PlatformConfig(enabled=True, token='fixture', typing_indicator=False))
    runner.adapters = {Platform.DISCORD: adapter}
    runner.config._runtime_profile_homes = [('default', launch), ('beta', beta)]
    from gateway.profile_routing import ProfileRoute
    runner.config.profile_routes = [ProfileRoute(name='fixture', platform='discord', chat_id='42', profile=profile)]
    runner._wire_adapter_handlers(adapter)
    queue = Queue()
    monkeypatch.setattr(process_registry, 'completion_queue', queue)
    executor = ThreadPoolExecutor(max_workers=1)
    monkeypatch.setattr(delegation, '_get_executor', lambda size: executor)
    registry = SessionAuthorities(launch, multiplexed=True)
    runner.session_authorities = registry
    try:
        for name, path in [('default', launch), ('beta', beta)]:
            with _profile_runtime_scope(path, prepared_secret_scope={}):
                authority = await initialize_session_authority(runner, profile_id=str(path),
                                                               instance_id=name, register=name == 'default')
                registry.add(path, authority, name=name)
        authority = registry.for_home(home)
        source = adapter.build_source(chat_id='42', chat_type='dm', user_id='42')
        source.profile = profile
        with _profile_runtime_scope(home, prepared_secret_scope={}):
            human = MessageEvent(text='fabricated task', source=source, message_id='human')
            with native_callback(runner, human, launch):
                receipt = await authority.admit_native(human)
            if legacy_envelope:
                import json
                from hermes_state_keys import admission_fingerprint
                human_row = list_session_admissions(authority.db, session_id=receipt.ref.session_id)[0]
                human_row['payload']['native_text_v1']['source'].pop('profile', None)
                encoded = json.dumps(human_row['payload'], sort_keys=True, separators=(',', ':'))
                digest = admission_fingerprint(canonical_target=receipt.ref.session_id,
                    payload={'input': human_row['payload'], 'intent': human_row['intent']})
                authority.db._write_sql('UPDATE session_admissions SET payload_json=?,payload_digest=? WHERE admission_id=?',
                                        (encoded, digest, human_row['admission_id']))
            key = runner.session_store._generate_session_key(source)
            handle = delegation.dispatch_async_delegation(
                goal='fabricated task', context=None, toolsets=None, role='test', model=None,
                session_key=key, parent_session_id=receipt.ref.session_id,
                runner=lambda: {'status': 'completed', 'summary': 'legacy-completion'})
            assert handle['status'] == 'dispatched'
            event = await asyncio.to_thread(queue.get, True, 5)
            assert delegation.get_durable_delegation(handle['delegation_id'])['delivery_state'] == 'pending'
            entry = runner.session_store.lookup_by_session_key(key)
            entry.origin.profile = None
            # Persist the legacy shape, as if loaded after a gateway restart.
            runner.session_store._save()
        assert await runner._deliver_async_delegation_group([event]) is True
        assert await runner._deliver_async_delegation_group([event]) is None
        rows = list_session_admissions(authority.db, session_id=receipt.ref.session_id, pending_only=False)
        assert len(rows) == 2 and 'legacy-completion' in rows[-1]['payload']['text']
        with _profile_runtime_scope(home, prepared_secret_scope={}):
            row = delegation.get_durable_delegation(handle['delegation_id'])
            assert (row['delivery_state'], row['delivery_attempts']) == ('delivered', 1)
        if profile == 'beta':
            assert registry.launch.db.get_session(receipt.ref.session_id) is None
        from copy import deepcopy
        from gateway.session_automation import check_automation_route
        from gateway.session_ingress import execute_admission
        from hermes_constants import get_hermes_home
        payload = deepcopy(rows[-1]['payload'])
        executed = []

        async def turn(event):
            executed.append((event.source.profile, get_hermes_home()))
            return 'fabricated reply'

        monkeypatch.setattr(runner, '_handle_message', turn)
        with _profile_runtime_scope(home, prepared_secret_scope={}):
            restored, route = check_automation_route(runner, payload, receipt.ref.session_id, source, adapter)
            assert restored.profile == profile and route == key
            await execute_admission(authority, receipt.ref, rows[-1])
        assert executed == [(profile, home)]
        assert payload == rows[-1]['payload']  # restore never changes the retry fingerprint
        assert not adapter._background_tasks  # no inference or network send
    finally:
        executor.shutdown(wait=True)
        runner.close_all_session_db_handles()
        runner.session_store.close_all_db_handles()


@pytest.mark.parametrize('namespace,explicit', [('main', 'beta'), ('beta', 'default'), ('main~', 'beta')])
def test_explicit_profile_conflicts_fail_before_claim(homes, namespace, explicit):
    from gateway.completion_profile import completion_source
    runner = GatewayRunner(GatewayConfig(multiplex_profiles=True))
    source = SessionSource(platform=Platform.DISCORD, chat_type='dm', chat_id='42', profile=explicit)
    try:
        with pytest.raises(IdentityUnresolved):
            completion_source(runner, source, f'agent:{namespace}:discord:dm:42')
    finally:
        runner.close_all_session_db_handles()
        runner.session_store.close_all_db_handles()


def test_unavailable_profile_and_standalone_namespace(homes):
    from gateway.completion_profile import completion_source
    runner = GatewayRunner(GatewayConfig(multiplex_profiles=True))
    source = SessionSource(platform=Platform.DISCORD, chat_type='dm', chat_id='42')
    try:
        with pytest.raises(IdentityUnresolved):
            completion_source(runner, source, 'agent:missing:discord:dm:42')
        runner.config.multiplex_profiles = False
        source.profile = 'beta'
        assert completion_source(runner, source, 'agent:main:discord:dm:42') is source
        assert completion_source(runner, source, 'opaque-api-session') is source
    finally:
        runner.close_all_session_db_handles()
        runner.session_store.close_all_db_handles()
