"""Profile MCP restrictions survive the canonical ACP gateway and scoped reconnects."""
import json
from pathlib import Path
import sys
from types import SimpleNamespace

import pytest

from hermes_cli.config import atomic_config_write
from tests.acp_adapter.test_gateway_sessions import editor
from tests.acp_adapter.test_gateway_editor_mcp import initialize, _policies


@pytest.fixture
def filtered_spec(tmp_path):
    script = tmp_path / 'filtered_peer.py'
    script.write_text('''import json, sys
for line in sys.stdin:
 r=json.loads(line); method=r.get('method'); ident=r.get('id')
 if ident is None: continue
 if method=='initialize': result={'protocolVersion':r['params']['protocolVersion'],'capabilities':{'tools':{}},'serverInfo':{'name':'filtered-peer','version':'1'}}
 elif method=='tools/list': result={'tools':[{'name':n,'description':n,'inputSchema':{'type':'object','properties':{}}} for n in ['read_note','write_note']]}
 elif method=='tools/call': result={'content':[{'type':'text','text':'CALLED_'+r['params']['name']}]}
 else: result={}
 print(json.dumps({'jsonrpc':'2.0','id':ident,'result':result}),flush=True)
''')
    return {'name': 't3-code', 'command': sys.executable, 'args': [str(script)], 'env': []}


@pytest.mark.asyncio
@pytest.mark.platforms('linux')
@pytest.mark.parametrize('daemon', [{'acp': {'mcp_tool_filters': {'t3-code': {'include': ['read_note']}}}}], indirect=True)
async def test_default_acp_gateway_new_and_resume_filter_manifest(daemon, filtered_spec, tmp_path):
    # Real entry.main/default HermesACPAgent, not the legacy SessionManager injection seam.
    async with editor(daemon, tmp_path) as acp:
        await initialize(acp)
        created = await acp.rpc('session/new', cwd=str(tmp_path), mcpServers=[filtered_spec])
        assert 'result' in created, created
        sid = created['result']['sessionId']
        (manifest,) = _policies(daemon[0], sid)
        assert {name.rsplit('__', 1)[-1] for name in manifest['schemas']} == {'read_note'}
    # A fresh peer advertises changed descriptions; the next admission must refreeze
    # only the approved schemas, rather than widening back to the client's whole list.
    script = Path(filtered_spec['args'][0])
    script.write_text(script.read_text().replace("'description':n", "'description':n+' updated'"))
    async with editor(daemon, tmp_path) as acp:
        await initialize(acp)
        resumed = await acp.rpc('session/load', sessionId=sid, cwd=str(tmp_path), mcpServers=[filtered_spec])
        assert 'result' in resumed, resumed
        reply = await acp.rpc('session/prompt', sessionId=sid, prompt=[{'type': 'text', 'text': 'Hello'}])
        assert 'result' in reply, reply
        (manifest,) = _policies(daemon[0], sid)
        assert {name.rsplit('__', 1)[-1] for name in manifest['schemas']} == {'read_note'}
        assert all('updated' in schema['description'] for schema in manifest['schemas'].values())


@pytest.mark.parametrize('first,second', [(['read_note'], ['write_note']), ([], ['read_note'])])
def test_scoped_gateway_rebind_and_reconnect_enforce_profile_filters(tmp_path, monkeypatch, filtered_spec, first, second):
    from gateway.session_local_mcp import _borrowed, _server_names, session_scope
    from model_tools import _dispatch_bridge_tool, get_tool_definitions
    from tools.mcp_tool_lifecycle import shutdown_mcp_servers

    monkeypatch.setenv('HERMES_HOME', str(tmp_path / 'ambient'))
    for profile, allowed in [('pilot', first), ('zhaan', second)]:
        home = tmp_path / profile
        home.mkdir()
        atomic_config_write(home / 'config.yaml', {'acp': {'mcp_tool_filters': {'t3-code': {'include': allowed}}}})
        owner = SimpleNamespace(profile_id=str(home), db=SimpleNamespace(db_path=str(home / 'state.db')), sessions={})
        scope = session_scope(owner, 'session')
        names = _server_names(scope, [filtered_spec])
        toolsets = [f'mcp-{name}' for name in names.values()]
        try:
            for _ in range(2):  # no viewer: closes and reconnects with the same policy
                with _borrowed(owner, scope, [filtered_spec], names, str(tmp_path)) as (schemas, connected):
                    expected = {f'mcp__{names[0]}__{name}' for name in allowed}
                    assert connected == [0]
                    assert set(schemas) == expected
                    definitions = get_tool_definitions(enabled_toolsets=toolsets, quiet_mode=True, skip_tool_search_assembly=True)
                    assert {t['function']['name'] for t in definitions} == expected
                    found, _ = _dispatch_bridge_tool('tool_search', {'queries': ['note'], 'limit': 50}, toolsets, [])
                    assert set(json.loads(found)['tools']) == expected
                    for forbidden in {'read_note', 'write_note'} - set(allowed):
                        result, target = _dispatch_bridge_tool('tool_call', {'name': f'mcp__{names[0]}__{forbidden}', 'arguments': {}}, toolsets, [])
                        assert result is not None and target is None
        finally:
            shutdown_mcp_servers(scope=scope)


@pytest.mark.parametrize('filters', [[], {'t3-code': None}, {'t3-code': {'include': 3}}, {'t3-code': {'incldue': []}}])
def test_malformed_gateway_policy_fails_before_registration(tmp_path, monkeypatch, filtered_spec, filters):
    from gateway.session_local_mcp import _borrowed, _server_names, session_scope
    monkeypatch.setenv('HERMES_HOME', str(tmp_path))
    atomic_config_write(tmp_path / 'config.yaml', {'acp': {'mcp_tool_filters': filters}})
    owner = SimpleNamespace(profile_id=str(tmp_path), db=SimpleNamespace(db_path=str(tmp_path / 'state.db')), sessions={})
    scope = session_scope(owner, 'session')
    calls = []
    monkeypatch.setattr('tools.mcp_tool_discovery.register_mcp_servers', lambda configs: calls.append(configs))
    with pytest.raises(ValueError):
        with _borrowed(owner, scope, [filtered_spec], _server_names(scope, [filtered_spec]), str(tmp_path)):
            pytest.fail('Malformed policy exposed a registry scope')
    assert calls == []


def test_policy_narrowing_replaces_a_viewer_retained_connection(tmp_path, monkeypatch, filtered_spec):
    from gateway.session_local_mcp import _borrowed, _server_names, session_scope, release_editor_mcp
    from tools.mcp_tool import _servers
    from tools.mcp_tool_scope import _server_key
    from model_tools import _dispatch_bridge_tool

    monkeypatch.setenv('HERMES_HOME', str(tmp_path))
    config = tmp_path / 'config.yaml'
    atomic_config_write(config, {'acp': {'mcp_tool_filters': {'t3-code': {'include': ['read_note', 'write_note']}}}})
    viewer = SimpleNamespace(subscribers={'editor': object()})
    owner = SimpleNamespace(profile_id=str(tmp_path), db=SimpleNamespace(db_path=str(tmp_path / 'state.db')),
                            sessions={'session': viewer})
    scope = session_scope(owner, 'session')
    names = _server_names(scope, [filtered_spec])
    key = _server_key(names[0], scope)
    try:
        with _borrowed(owner, scope, [filtered_spec], names, str(tmp_path)) as (schemas, _):
            assert len(schemas) == 2
        retained = _servers[key]
        assert retained.session is not None
        atomic_config_write(config, {'acp': {'mcp_tool_filters': {'t3-code': {'include': []}}}})
        with _borrowed(owner, scope, [filtered_spec], names, str(tmp_path)) as (schemas, connected):
            assert schemas == {} and connected == [0]
            assert _servers[key] is not retained
            assert retained.session is None
            result, target = _dispatch_bridge_tool('tool_call', {
                'name': f'mcp__{names[0]}__write_note', 'arguments': {}}, [f'mcp-{names[0]}'], [])
            assert result is not None and target is None
    finally:
        viewer.subscribers.clear()
        release_editor_mcp(owner, ['session'])


@pytest.mark.platforms('posix')
@pytest.mark.parametrize('failure', ['syntax', 'unreadable'])
@pytest.mark.parametrize('warm', [False, True])
def test_unreadable_or_invalid_yaml_never_registers_unrestricted_tools(tmp_path, monkeypatch, filtered_spec, failure, warm):
    from acp_adapter.mcp_config import client_mcp_tool_filters
    from gateway.session_local_mcp import _borrowed, _server_names, session_scope

    monkeypatch.setenv('HERMES_HOME', str(tmp_path))
    config = tmp_path / 'config.yaml'
    atomic_config_write(config, {'acp': {'mcp_tool_filters': {'t3-code': {'include': ['read_note']}}}})
    if warm:
        assert client_mcp_tool_filters(tmp_path)['t3-code']['include'] == ['read_note']
    if failure == 'syntax':
        config.write_text('acp: [unterminated')
    else:
        config.chmod(0)
    owner = SimpleNamespace(profile_id=str(tmp_path), db=SimpleNamespace(db_path=str(tmp_path / 'state.db')), sessions={})
    scope = session_scope(owner, 'session')
    calls = []
    monkeypatch.setattr('tools.mcp_tool_discovery.register_mcp_servers', lambda configs: calls.append(configs))
    try:
        with pytest.raises(ValueError, match='Cannot load ACP MCP policy'):
            with _borrowed(owner, scope, [filtered_spec], _server_names(scope, [filtered_spec]), str(tmp_path)):
                pytest.fail('Failed config read exposed an MCP scope')
        assert calls == []
    finally:
        config.chmod(0o600)
