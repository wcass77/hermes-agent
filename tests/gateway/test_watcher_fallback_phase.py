"""Profile recovery must resume fallback without reopening completion offers."""
import asyncio
from unittest.mock import AsyncMock

import pytest
from tests.gateway.test_watcher_profile_progress import setup_watcher

pytest_plugins = ['tests.gateway.test_completion_profile_compatibility']


@pytest.mark.asyncio
@pytest.mark.parametrize('withdrawals,outcome', [(0, 'notice'), (2, 'notice'), (1, 'consumed'), (1, 'admitted')])
async def test_fallback_retries_preserve_offer_ceiling(fixture_runtime, monkeypatch, withdrawals, outcome):
    import gateway.run_notifications as rn
    from gateway import session_automation
    from tools import process_registry as pr_module

    runner, registry, beta, watcher, _ = setup_watcher(fixture_runtime, monkeypatch, notify=True)
    held, offers, notices = [], [], []
    remaining = withdrawals
    resumed = False
    formatter = runner._format_process_final_message

    async def refuse_injection(text, evt, **kwargs):
        offers.append(evt)
        return False

    def format_notice(*args):
        nonlocal remaining
        if remaining:
            remaining -= 1
            held.append(registry.remove(beta))
        return formatter(*args)

    async def tick(delay):
        nonlocal resumed
        if held:
            registry.add(beta, held.pop(), name='beta')
            resumed = True

    async def notice(*args):
        notices.append(args)

    monkeypatch.setattr(runner, '_launching_turn_active', AsyncMock(return_value=False))
    monkeypatch.setattr(runner, '_inject_watch_notification', refuse_injection)
    monkeypatch.setattr(runner, '_format_process_final_message', format_notice)
    monkeypatch.setattr(runner, '_send_watcher_message', notice)
    monkeypatch.setattr(pr_module.process_registry, 'is_completion_consumed', lambda sid: resumed and outcome == 'consumed')
    monkeypatch.setattr(session_automation, 'completion_admission',
                        lambda runner, evt: {'admission_id': 'fabricated-lost-ack'} if resumed and outcome == 'admitted' else None)
    enqueue = AsyncMock(wraps=runner._enqueue_process_completion_notification)
    monkeypatch.setattr(runner, '_enqueue_process_completion_notification', enqueue)
    runner._completion_notification_batch_window = 0
    monkeypatch.setattr(asyncio, 'sleep', tick)
    try:
        await runner._run_process_watcher(watcher)
        assert enqueue.await_count == rn._MAX_COMPLETION_INJECTION_ATTEMPTS
        assert len(offers) == rn._MAX_COMPLETION_INJECTION_ATTEMPTS
        assert len(notices) == (1 if outcome == 'notice' else 0)
        assert not held and remaining == 0
    finally:
        await runner._cancel_process_completion_batch_tasks()
