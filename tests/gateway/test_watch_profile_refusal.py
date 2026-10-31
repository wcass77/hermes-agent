"""A profile routing refusal must retain its event without losing healthy detached siblings."""
from queue import Queue
import time

import pytest
pytest_plugins = ['tests.gateway.test_completion_profile_compatibility']


@pytest.mark.asyncio
@pytest.mark.parametrize('refusal', ['removed', 'conflict'])
async def test_one_refused_profile_does_not_lose_other_watch_events(fixture_runtime, monkeypatch, refusal):
    from gateway.config import Platform
    from gateway.run import _profile_runtime_scope
    from gateway.session import SessionSource
    runner, _, registry, home, beta = fixture_runtime
    bad = SessionSource(platform=Platform.DISCORD, chat_id='43', chat_type='dm', user_id='42', profile='beta')
    good = SessionSource(platform=Platform.DISCORD, chat_id='42', chat_type='dm', user_id='42')
    with _profile_runtime_scope(beta, prepared_secret_scope={}):
        bad_entry = runner.session_store.get_or_create_session(bad)
    with _profile_runtime_scope(home, prepared_secret_scope={}):
        good_entry = runner.session_store.get_or_create_session(good)
    if refusal == 'removed':
        registry.remove(beta)
    else:
        bad_entry.origin.profile = 'default'
    first = dict(type='watch_match', event_id='fabricated-bad', session_id='proc-bad',
                 session_key=bad_entry.session_key, pattern='READY', output='READY')
    second = dict(type='watch_match', event_id='fabricated-good', session_id='proc-good',
                  session_key=good_entry.session_key, pattern='READY', output='READY')
    pending = Queue()
    pending.put(first)
    pending.put(second)
    observed = []
    async def record(text, evt):
        observed.append(evt['event_id'])
        return False if evt['event_id'] == 'fabricated-bad' else True
    monkeypatch.setattr(runner, '_inject_watch_notification', record)
    monkeypatch.setattr(runner, '_load_background_notifications_mode', lambda: 'all')
    await runner._drain_watch_notifications(pending)
    assert observed == ['fabricated-good']
    assert pending.qsize() == 1 and pending.get_nowait() is first
    assert first['_watch_retry_step'] == 1
    assert first['_watch_next_retry_at'] > time.monotonic()
    pending.put(first)
    await runner._drain_watch_notifications(pending)
    assert observed == ['fabricated-good']  # backoff does not retry or spend a permanent attempt
    assert pending.get_nowait() is first and first['_watch_retry_step'] == 1
    assert not first.get('_watch_injection_attempts')


@pytest.mark.asyncio
async def test_process_watcher_waits_for_profile_recovery(fixture_runtime, monkeypatch):
    import asyncio
    from types import SimpleNamespace
    from unittest.mock import AsyncMock
    from gateway.config import Platform
    from gateway.run import _profile_runtime_scope
    from gateway.session import SessionSource
    from tools import process_registry as pr_module

    runner, _, registry, _, beta = fixture_runtime
    source = SessionSource(platform=Platform.DISCORD, chat_id='43', chat_type='dm', user_id='42', profile='beta')
    with _profile_runtime_scope(beta, prepared_secret_scope={}):
        entry = runner.session_store.get_or_create_session(source)
    authority = registry.remove(beta)
    session = SimpleNamespace(output_buffer='done', exited=True, exit_code=0, command='fabricated',
                              started_at=1, owner_task_id='', task_id='')
    monkeypatch.setattr(pr_module, 'process_registry', SimpleNamespace(
        get=lambda sid: session, is_completion_consumed=lambda sid: False))
    monkeypatch.setattr(runner, '_load_background_notifications_mode', lambda: 'result')
    monkeypatch.setattr(runner, '_launching_turn_active', AsyncMock(return_value=False))
    admitted = AsyncMock(return_value=True)
    monkeypatch.setattr(runner, '_enqueue_process_completion_notification', admitted)
    delays = []

    async def resume_profile(delay):
        delays.append(delay)
        if registry.for_home(beta) is None:
            registry.add(beta, authority, name='beta')

    monkeypatch.setattr(asyncio, 'sleep', resume_profile)
    await runner._run_process_watcher({'session_id': 'fabricated-process', 'check_interval': 0,
        'session_key': entry.session_key, 'platform': 'discord', 'chat_id': '43', 'notify_on_complete': True})
    assert delays[0] >= 2
    admitted.assert_awaited_once()
