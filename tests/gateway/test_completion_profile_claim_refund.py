"""Late routing uncertainty refunds claims in the proven owner home, without spending attempts."""
from unittest.mock import AsyncMock
import pytest
pytest_plugins = ['tests.gateway.test_completion_profile_compatibility']

async def seed_route(runtime):
    from gateway.platforms.event import MessageEvent
    from gateway.session_ingress_context import native_callback
    from gateway.profile_routing import ProfileRoute
    from gateway.run import _profile_runtime_scope
    runner, adapter, registry, home, beta = runtime
    runner.config.profile_routes = [ProfileRoute(name='fabricated', platform='discord', chat_id='42', profile='beta')]
    source = adapter.build_source(chat_id='42', chat_type='dm', user_id='42')
    source.profile = 'beta'
    with _profile_runtime_scope(beta, prepared_secret_scope={}):
        with native_callback(runner, event := MessageEvent(text='fabricated', source=source, message_id='fabricated-human'), home):
            receipt = await registry.for_home(beta).admit_native(event)
    async def accept_only(event):
        event._gateway_accepted = True
    adapter.handle_message = AsyncMock(side_effect=accept_only)
    adapter.send = AsyncMock()
    return runner, registry, beta, runner.session_store._generate_session_key(source), receipt.ref.session_id


def seed_event(key, owner, ident):
    from tools import async_delegation as ad
    evt = dict(type='async_delegation', delegation_id=ident, session_key=key,
               parent_session_id=owner, dispatched_at=1, status='completed', summary='fabricated result')
    ad._persist_dispatch(evt)
    ad._persist_completion(evt, dict(status='completed', summary='fabricated result'))
    return evt


@pytest.mark.asyncio
async def test_late_refusal_does_not_spend_primary_attempt(fixture_runtime, monkeypatch):
    from gateway.run import _profile_runtime_scope
    from gateway.session_identity import IdentityUnresolved
    from tools import async_delegation as ad
    runner, registry, beta, key, owner = await seed_route(fixture_runtime)
    with _profile_runtime_scope(beta, prepared_secret_scope={}):
        evt = seed_event(key, owner, 'fabricated-primary-control')
    preflight = runner._preflight_completion_delivery
    held = []
    async def withdraw_after_claim(event):
        claim = await preflight(event)
        assert claim.claim_id and claim.proceed
        held.append(registry.remove(beta))
        return claim
    monkeypatch.setattr(runner, '_preflight_completion_delivery', withdraw_after_claim)
    outcomes, states = [], []
    for _ in range(ad._MAX_DELIVERY_ATTEMPTS):
        if held:
            registry.add(beta, held.pop(), name='beta')
        try:
            outcomes.append(await runner._deliver_completion_notification('fabricated result', evt))
        except IdentityUnresolved:
            outcomes.append('IdentityUnresolved')
        with _profile_runtime_scope(beta, prepared_secret_scope={}):
            row = ad.get_durable_delegation(evt['delegation_id'])
        states.append((row['delivery_state'], row['delivery_attempts']))
    assert outcomes == [False] * ad._MAX_DELIVERY_ATTEMPTS
    assert states == [('pending', 0)] * ad._MAX_DELIVERY_ATTEMPTS
    registry.add(beta, held.pop(), name='beta')
    monkeypatch.setattr(runner, '_preflight_completion_delivery', preflight)
    assert await runner._deliver_completion_notification('fabricated result', evt) is True
    with _profile_runtime_scope(beta, prepared_secret_scope={}):
        row = ad.get_durable_delegation(evt['delegation_id'])
    assert (row['delivery_state'], row['delivery_attempts']) == ('delivered', 1)
    assert await runner._deliver_completion_notification('fabricated result', evt) is None



@pytest.mark.asyncio
@pytest.mark.parametrize('refusal', ['withdrawn', 'suppressed'])
async def test_late_refusal_does_not_leave_sibling_claim(fixture_runtime, monkeypatch, refusal):
    import sqlite3
    from gateway.run import _profile_runtime_scope
    from gateway.session_identity import IdentityUnresolved
    from tools import async_delegation as ad
    runner, registry, beta, key, owner = await seed_route(fixture_runtime)
    with _profile_runtime_scope(beta, prepared_secret_scope={}):
        group = [seed_event(key, owner, 'fabricated-sibling-control-1'), seed_event(key, owner, 'fabricated-sibling-control-2')]
    from hermes_constants import get_hermes_home
    from tools import process_registry_notifications as notices
    surface = notices.should_surface_notification
    claim = ad.claim_event_delivery
    held, settlements = [], []
    settle = runner._settle_durable_claim
    def record_settlement(operation, delegation_id, claim_id):
        settlements.append((operation, get_hermes_home()))
        return settle(operation, delegation_id, claim_id)
    monkeypatch.setattr(runner, '_settle_durable_claim', record_settlement)
    def withdraw_after_claim(event, consumer):
        result = claim(event, consumer)
        assert result
        if refusal == 'withdrawn':
            held.append(registry.remove(beta))
        else:
            monkeypatch.setattr(notices, 'should_surface_notification', lambda evt: False)
        return result
    monkeypatch.setattr(ad, 'claim_event_delivery', withdraw_after_claim)
    try:
        await runner._deliver_async_delegation_group(group)
    except IdentityUnresolved:
        pass
    with _profile_runtime_scope(beta, prepared_secret_scope={}):
        with sqlite3.connect(ad._db_path()) as conn:
            row = conn.execute('SELECT delivery_state,delivery_attempts,delivery_claim IS NOT NULL FROM async_delegations WHERE delegation_id=?', ('fabricated-sibling-control-2',)).fetchone()
    assert row == ('pending', 0, 0), row
    assert settlements and all(op == 'defer' and home == beta for op, home in settlements)
    if held:
        registry.add(beta, held.pop(), name='beta')
    monkeypatch.setattr(ad, 'claim_event_delivery', claim)
    monkeypatch.setattr(notices, 'should_surface_notification', surface)
    assert await runner._deliver_async_delegation_group(group) is True
    with _profile_runtime_scope(beta, prepared_secret_scope={}):
        for event in group:
            row = ad.get_durable_delegation(event['delegation_id'])
            assert (row['delivery_state'], row['delivery_attempts']) == ('delivered', 1)
