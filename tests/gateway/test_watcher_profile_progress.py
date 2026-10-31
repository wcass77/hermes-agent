"""Fabricated, isolated read-only review diagnostics; no inference or transport sends."""
import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock
import pytest

pytest_plugins = ['tests.gateway.test_completion_profile_compatibility']


def setup_watcher(runtime, monkeypatch, *, notify=False):
    from gateway.config import Platform
    from gateway.run import _profile_runtime_scope
    from gateway.session import SessionSource
    from tools import process_registry as pr_module
    runner, _, registry, _, beta = runtime
    source = SessionSource(platform=Platform.DISCORD, chat_id='43', chat_type='dm', user_id='42', profile='beta')
    with _profile_runtime_scope(beta, prepared_secret_scope={}):
        entry = runner.session_store.get_or_create_session(source)
    watcher = dict(session_id='fabricated-process', check_interval=1, session_key=entry.session_key,
                   platform='discord', chat_id='43', notify_on_complete=notify)
    session = SimpleNamespace(output_buffer='unchanged output', exited=notify, exit_code=0 if notify else None,
                              command='fabricated', started_at=1, owner_task_id='', task_id='')
    monkeypatch.setattr(pr_module, 'process_registry', SimpleNamespace(
        get=lambda sid: session, is_completion_consumed=lambda sid: False))
    monkeypatch.setattr(runner, '_load_background_notifications_mode', lambda: 'all')
    return runner, registry, beta, watcher, session


@pytest.mark.asyncio
@pytest.mark.parametrize('withdraw', [False, True])
async def test_same_output_is_not_reposted_after_profile_recovery(fixture_runtime, monkeypatch, withdraw):
    from tools import process_registry as pr_module
    runner, registry, beta, watcher, session = setup_watcher(fixture_runtime, monkeypatch)
    sent, held = [], []
    gets = 0
    def next_session(sid):
        nonlocal gets
        gets += 1
        return session if gets <= 3 else None
    monkeypatch.setattr(pr_module.process_registry, 'get', next_session)
    async def send(platform, chat, thread, text, evt):
        sent.append(text)
        if withdraw and len(sent) == 1:
            held.append(registry.remove(beta))
    async def tick(delay):
        if delay >= 2 and held:
            registry.add(beta, held.pop(), name='beta')
    monkeypatch.setattr(runner, '_send_watcher_message', send)
    monkeypatch.setattr(asyncio, 'sleep', tick)
    await runner._run_process_watcher(watcher)
    assert len(sent) == 1, f'unchanged stdout posted {len(sent)} times: {sent!r}'


@pytest.mark.asyncio
async def test_completion_refusal_budget_survives_profile_refusal(fixture_runtime, monkeypatch):
    import gateway.run_notifications as rn
    runner, registry, beta, watcher, _ = setup_watcher(fixture_runtime, monkeypatch, notify=True)
    held, offers = [], []
    async def offer(text, evt):
        offers.append(evt)
        if len(offers) == 4:
            held.append(registry.remove(beta))
        return False
    async def tick(delay):
        # First next poll after offer #4 refuses; only its wrapper's delay restores the slot.
        if held and delay <= 2:
            registry.add(beta, held.pop(), name='beta')
    monkeypatch.setattr(runner, '_launching_turn_active', AsyncMock(return_value=False))
    monkeypatch.setattr(runner, '_enqueue_process_completion_notification', offer)
    monkeypatch.setattr(runner, '_send_watcher_message', AsyncMock())
    monkeypatch.setattr(asyncio, 'sleep', tick)
    await runner._run_process_watcher(watcher)
    assert len(offers) == rn._MAX_COMPLETION_INJECTION_ATTEMPTS, len(offers)


@pytest.mark.asyncio
async def test_refusal_backoff_is_cancellable_and_scope_restored(fixture_runtime, monkeypatch):
    from hermes_constants import get_hermes_home_override
    runner, registry, beta, watcher, _ = setup_watcher(fixture_runtime, monkeypatch)
    registry.remove(beta)
    sleeping = asyncio.Event()
    never = asyncio.Event()
    scopes = []
    async def pause(delay):
        scopes.append(get_hermes_home_override())
        sleeping.set()
        await never.wait()
    monkeypatch.setattr(asyncio, 'sleep', pause)
    task = asyncio.create_task(runner._run_process_watcher(watcher))
    await asyncio.wait_for(sleeping.wait(), timeout=10)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert scopes == [None]


@pytest.mark.asyncio
async def test_refused_progress_send_does_not_advance_cursor(fixture_runtime, monkeypatch):
    from gateway.session_identity import IdentityUnresolved
    from tools import process_registry as pr_module
    runner, registry, beta, watcher, session = setup_watcher(fixture_runtime, monkeypatch)
    sent, held = [], []
    gets = attempts = 0

    def next_session(sid):
        nonlocal gets
        gets += 1
        return session if gets <= 3 else None

    async def send(platform, chat, thread, text, evt):
        nonlocal attempts
        attempts += 1
        if attempts == 1:
            held.append(registry.remove(beta))
            raise IdentityUnresolved('fabricated profile withdrawal before send')
        sent.append(text)

    async def tick(delay):
        if delay >= 2 and held:
            registry.add(beta, held.pop(), name='beta')

    monkeypatch.setattr(pr_module.process_registry, 'get', next_session)
    monkeypatch.setattr(runner, '_send_watcher_message', send)
    monkeypatch.setattr(asyncio, 'sleep', tick)
    await runner._run_process_watcher(watcher)
    assert attempts == 2 and len(sent) == 1
