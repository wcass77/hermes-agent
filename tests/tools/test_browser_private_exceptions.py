"""Private app grants survive the browser_exec/CDP policy migration."""
import json
import socket
import pytest
from agent.secret_scope import set_secret_scope, reset_secret_scope
from hermes_constants import set_hermes_home_override, reset_hermes_home_override
from tools import browser_url_policy as policy
from tools.browser_cdp_tool import _browser_cdp_private_guard

HOST = 'workstation.example.ts.net'
ORIGIN = 'https://' + HOST
ADDRESS = '100.64.0.10'
RULE = {'cdp_url':'http://127.0.0.1:9222','host':HOST,'addresses':[ADDRESS],
        'paths':['/','/hours/','/timecard/','/files/']}

@pytest.fixture
def configured(tmp_path, monkeypatch):
    home = tmp_path / 'pilot'; home.mkdir()
    (home/'config.yaml').write_text(json.dumps({'browser':{'cdp_url':RULE['cdp_url'],
        'allow_private_urls':False,'private_url_exceptions':RULE}}))
    token = set_hermes_home_override(home)
    monkeypatch.delenv('BROWSER_CDP_URL', raising=False)
    monkeypatch.setattr(socket,'getaddrinfo',lambda *a,**k:[
        (socket.AF_INET,socket.SOCK_STREAM,6,'',(ADDRESS,443))])
    yield home
    reset_hermes_home_override(token)

@pytest.mark.parametrize('path',['/','/hours','/hours/','/hours/?period=2026-11-02',
    '/files/report%20one.pdf'])
def test_grant_applies_to_exec_literals_and_cdp_navigation(configured, path):
    url=ORIGIN+path
    assert policy.evaluate_url_safety(url) is None
    assert not policy.url_blocked(url)
    assert policy.expression_targets_private_url(f"fetch('{url}')") is None
    assert _browser_cdp_private_guard(method='Page.navigate',params={'url':url}) is None

@pytest.mark.parametrize('path',['/location/setup','/private-input/','/hours-other/',
    '/hours/../location/setup','/hours/%2e%2e/x','/hours/%252e%252e/x','/hours/%2f../x',
    '/hours//x','/hours/%5cx','/hours/%00x','/hours/%xx','/hours/?period=wrong',
    '/hours/?period=2026-02-30','/hours/?other=1','/hours/?period=2026-11-02&period=2026-11-03'])
def test_unapproved_and_ambiguous_paths_refused(configured,path):
    url=ORIGIN+path
    assert policy.evaluate_url_safety(url) is not None
    assert policy.url_blocked(url)
    assert _browser_cdp_private_guard(method='Page.navigate',params={'url':url}) is not None

@pytest.mark.parametrize('url',[f'http://{HOST}/hours/',f'https://{HOST}:8443/hours/',
    f'https://user@{HOST}/hours/','https://100.64.0.10/hours/',
    'http://127.0.0.1:8741/hours/','http://169.254.169.254/latest/meta-data/'])
def test_other_origins_and_metadata_refused(configured,url):
    assert policy.evaluate_url_safety(url) is not None

@pytest.mark.parametrize('answers',[[],['100.64.0.11'],[ADDRESS,'127.0.0.1'],['8.8.8.8']])
def test_dns_drift_refused_even_to_public_ip(configured,monkeypatch,answers):
    monkeypatch.setattr(socket,'getaddrinfo',lambda *a,**k:[
        (socket.AF_INET,socket.SOCK_STREAM,6,'',(ip,443)) for ip in answers])
    assert policy.evaluate_url_safety(ORIGIN+'/hours/') is not None

def test_profile_endpoint_and_website_denials(configured,monkeypatch,tmp_path):
    url=ORIGIN+'/hours/'
    other=tmp_path/'other';other.mkdir()
    (other/'config.yaml').write_text(json.dumps({'browser':{'cdp_url':RULE['cdp_url']}}))
    token=set_hermes_home_override(other)
    try: assert policy.evaluate_url_safety(url) is not None
    finally: reset_hermes_home_override(token)
    monkeypatch.setenv('BROWSER_CDP_URL','http://127.0.0.1:9333')
    assert policy.evaluate_url_safety(url) is None
    scope=set_secret_scope({'BROWSER_CDP_URL':'http://127.0.0.1:9333'},profile_home=str(configured))
    try: assert policy.evaluate_url_safety(url) is not None
    finally: reset_secret_scope(scope)
    monkeypatch.setattr(policy,'check_website_access',lambda url:{
        'message':'Denied by owner','host':HOST,'rule':HOST,'source':'test'})
    assert policy.evaluate_url_safety(url) is not None
    assert policy.url_blocked(url)

@pytest.mark.parametrize('page_path,blocked',[('/hours/',False),('/private-input/',True)])
def test_cdp_current_page_and_exec_preflight_share_grant(configured,monkeypatch,page_path,blocked):
    from tools import browser_cdp_tool, browser_use_cli
    url=ORIGIN+page_path
    async def targets(*args,**kwargs):
        return {'targetInfos':[{'targetId':'synthetic','type':'page','url':url}]}
    monkeypatch.setattr(browser_cdp_tool,'_cdp_call',targets)
    error=_browser_cdp_private_guard(method='DOM.getDocument',params={},
        endpoint='ws://synthetic.invalid/devtools/browser/fixture',target_id='synthetic')
    assert (error is not None) is blocked
    assert (browser_use_cli._code_url_verdict(f"page.goto('{url}')").blocked is not None) is blocked
