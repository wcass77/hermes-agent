"""A pruned process retires its watcher even if profile routing cannot recover."""
import asyncio
from unittest.mock import Mock

import pytest
from tests.gateway.test_watcher_profile_progress import setup_watcher

pytest_plugins = ['tests.gateway.test_completion_profile_compatibility']


@pytest.mark.asyncio
@pytest.mark.parametrize('unserved', [False, True])
async def test_pruned_process_stops_watcher(fixture_runtime, monkeypatch, unserved):
    from tools import process_registry as pr_module
    from gateway.session_context import scoped_current_session_id

    runner, authorities, beta, watcher, _ = setup_watcher(fixture_runtime, monkeypatch, notify=True)
    registry = pr_module.ProcessRegistry()
    watcher['session_id'] = 'proc_fabricated_expired'
    session = pr_module.ProcessSession(id=watcher['session_id'], command='fabricated', started_at=1, exited=True)
    session._completion_event.set()
    registry._finished[session.id] = session
    registry._prune_if_needed()
    assert session.id not in registry._finished
    monkeypatch.setattr(pr_module, 'process_registry', registry)
    lookup = Mock(wraps=registry.get)
    monkeypatch.setattr(registry, 'get', lookup)
    if unserved:
        authorities.remove(beta)
    delays = []

    async def tick(delay):
        delays.append(delay)
        if len(delays) >= 3:
            raise AssertionError('a pruned process must not retry profile resolution indefinitely')

    monkeypatch.setattr(asyncio, 'sleep', tick)
    with scoped_current_session_id(''):
        assert registry.get(session.id) is None
        lookup.reset_mock()
        await runner._run_process_watcher(watcher)
    assert lookup.call_count == 1
    assert not unserved or not delays


@pytest.mark.asyncio
async def test_process_pruned_during_profile_backoff_stops_watcher(fixture_runtime, monkeypatch):
    from tools import process_registry as pr_module
    from gateway.session_context import scoped_current_session_id

    runner, authorities, beta, watcher, _ = setup_watcher(fixture_runtime, monkeypatch, notify=True)
    registry = pr_module.ProcessRegistry()
    watcher['session_id'] = 'proc_fabricated_pruned_later'
    session = pr_module.ProcessSession(id=watcher['session_id'], command='fabricated', started_at=1, exited=True)
    session._completion_event.set()
    registry._finished[session.id] = session
    monkeypatch.setattr(pr_module, 'process_registry', registry)
    authorities.remove(beta)
    delays = []

    async def prune_during_backoff(delay):
        delays.append(delay)
        registry._prune_if_needed()
        if len(delays) >= 3:
            raise AssertionError('pruning during backoff must retire the watcher')

    monkeypatch.setattr(asyncio, 'sleep', prune_during_backoff)
    with scoped_current_session_id(''):
        await runner._run_process_watcher(watcher)
    assert len(delays) == 1 and delays[0] >= 2
    assert registry.get(session.id) is None
