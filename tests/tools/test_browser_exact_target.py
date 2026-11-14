"""Pinned-target bridge contracts; all commands/supervisors are synthetic."""
from types import SimpleNamespace
from unittest.mock import Mock
import hashlib
import json
import pytest
from tools import browser_tool_target as target


def row(identity='own'):
    return {'targetId':identity,'active':True,'url':'https://same.test/','tabId':'t1'}


class Driver:
    def __init__(self, pre=None, post=None, action=None):
        self.rows=[pre or row(),post or pre or row()];self.calls=[]
        self.action=action or {'success':True,'data':{'snapshot':'synthetic'}}
    def __call__(self,command,args):
        self.calls.append((command,args))
        if (command,args)==('tab',['list']):
            value=self.rows.pop(0) if len(self.rows)>1 else self.rows[0]
            return {'success':True,'data':{'tabs':[value]}}
        return self.action


def sup():
    bound=[]
    def bind(identity):bound[:]=[identity];return {'ok':True}
    return SimpleNamespace(matches_command_target=lambda x:bound==[x],bind_command_target=bind)


def test_snapshot_carries_verified_id_and_clears_quarantine():
    info={'target_reconciliation_required':True};d=Driver();s=sup()
    r=target.dispatch('t',info,'snapshot',[],d,s)
    assert r['data']['targetId']=='own' and 'target_reconciliation_required' not in info
    assert s._secret_admission()


def test_same_origin_neighbor_never_receives_action():
    info={'command_target_id':'own'};d=Driver(pre=row('neighbor'))
    r=target.dispatch('t',info,'click',['@e1'],d,sup())
    assert r['code']=='target_changed';assert all(c=='tab' for c,_ in d.calls)


def test_missing_identity_refuses_before_action():
    d=Driver(pre={'active':True,'url':'https://same.test/'})
    r=target.dispatch('t',{},'click',['@e1'],d,sup())
    assert r['code']=='target_identity_unavailable';assert len(d.calls)==1


def test_post_action_change_is_uncertain_and_not_replayed():
    d=Driver(post=row('neighbor'));info={}
    r=target.dispatch('t',info,'click',['@e1'],d,sup())
    assert r['code']=='target_outcome_uncertain';assert info['target_reconciliation_required']
    assert len([c for c,_ in d.calls if c=='click'])==1


def test_failed_action_not_replayed_or_post_probed():
    d=Driver(action={'success':False,'error':'timeout'});r=target.dispatch('t',{},'screenshot',[],d,sup())
    assert not r['success'];assert len(d.calls)==2


def test_quarantine_allows_read_but_not_writes_or_secrets():
    info={'target_reconciliation_required':True};d=Driver();s=sup()
    r=target.dispatch('t',info,'fill',['@e1','synthetic'],d,s)
    assert r['code']=='target_reconciliation_required' and not s._command_admission()
    assert all(c=='tab' for c,_ in d.calls)


def test_fresh_snapshot_required_after_navigation():
    info={};d=Driver();s=sup();target.dispatch('t',info,'open',['https://same.test/'],d,s)
    assert info['fresh_snapshot_required'] and not s._secret_admission()
    denied=target.dispatch('t',info,'click',['@e1'],Driver(),s)
    assert denied['code']=='fresh_snapshot_required'
    target.dispatch('t',info,'snapshot',[],Driver(),s)
    assert s._secret_admission()


def test_cleanup_does_not_inspect_or_create_replacement():
    d=Driver();target.dispatch('t',{},'close',[],d,sup());assert d.calls==[('close',[])]


def test_binding_failure_does_not_send_action():
    s=sup();s.bind_command_target=lambda x:{'ok':False};d=Driver()
    r=target.dispatch('t',{},'click',['@e1'],d,s)
    assert r['code']=='target_binding_failed' and len(d.calls)==1


def test_explicit_tab_switch_updates_target_and_requires_snapshot():
    info={'command_target_id':'own'};d=Driver(post=row('other'))
    r=target.dispatch('t',info,'tab',['other'],d,sup())
    assert r['success'] and info['command_target_id']=='other' and info['fresh_snapshot_required']


def test_driver_checksum_failure_refuses_fallback(monkeypatch,tmp_path):
    from tools import browser_tool_install as install
    binary=tmp_path/'driver';binary.write_text('#!/bin/sh\nexit0\n');binary.chmod(0o700)
    values={'agent_browser_path':str(binary),'agent_browser_sha256':'wrong'}
    monkeypatch.setattr(install,'_origin',lambda:SimpleNamespace(_browser_cfg=lambda k,d,*a:values.get(k,d)))
    with pytest.raises(FileNotFoundError,match='checksum'):install._find_agent_browser(validate=False)
    values['agent_browser_sha256']=hashlib.sha256(binary.read_bytes()).hexdigest()
    assert install._find_agent_browser(validate=False)==str(binary)


def test_strict_origin_focus_does_not_search_neighbor_tabs():
    from tools.browser_supervisor import CDPSupervisor
    s=CDPSupervisor('t','ws://synthetic',command_binding_required=True)
    s._loop=SimpleNamespace(is_running=lambda:True);s._secret_admission=lambda:True
    s._cdp=Mock(side_effect=AssertionError('no target enumeration or selection'))
    s.evaluate_runtime=lambda expression:{'ok':True,'result':'https://wrong.test'}
    assert not s.focus_page('https://same.test')['ok']
    s.evaluate_runtime=lambda expression:{'ok':True,'result':'https://same.test' if expression=='location.origin' else True}
    assert s.focus_page('https://same.test',accept='synthetic form')['ok']
    s._cdp.assert_not_called()


def test_strict_evaluation_refuses_before_secret_dispatch():
    from tools.browser_supervisor import CDPSupervisor
    s=CDPSupervisor('t','ws://synthetic',command_binding_required=True)
    s._cdp=Mock(side_effect=AssertionError('must not send secret'))
    r=s.evaluate_runtime('synthetic-secret-expression')
    assert not r['ok'] and 'fresh snapshot' in r['error'];s._cdp.assert_not_called()
