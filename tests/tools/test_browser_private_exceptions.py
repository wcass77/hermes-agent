"""A profile may grant one loopback CDP browser access to named private app paths."""

import json
import socket

import pytest

from hermes_constants import reset_hermes_home_override, set_hermes_home_override
from tools import browser_tool as bt
from tools import browser_tool_session as session
from tools import browser_tool_eval_policy as policy
from tools.browser_cdp_tool import _browser_cdp_private_guard
from tools.registry import registry

HOST = "intel-air.tailb4105a.ts.net"
ORIGIN = f"https://{HOST}"
ADDRESS = "100.79.31.106"
RULE = {"cdp_url": "http://127.0.0.1:9222", "host": HOST,
        "addresses": [ADDRESS], "paths": ["/", "/_portal/", "/hours/", "/timecard/",
        "/sharebox/", "/surgery-docs/", "/vacation/", "/family/", "/call-schedule/"]}


@pytest.fixture
def configured(tmp_path, monkeypatch):
    """Real config reads and profile binding; only DNS/browser transport are synthetic."""
    home = tmp_path / "pilot"
    home.mkdir()
    (home / "config.yaml").write_text(json.dumps({"browser": {
        "cdp_url": RULE["cdp_url"], "allow_private_urls": False, "private_url_exceptions": RULE}}), encoding="utf-8")
    token = set_hermes_home_override(home)
    monkeypatch.delenv("BROWSER_CDP_URL", raising=False)
    monkeypatch.setattr(socket, "getaddrinfo", lambda *a, **k: [
        (socket.AF_INET, socket.SOCK_STREAM, 6, "", (ADDRESS, 443))])
    yield home
    reset_hermes_home_override(token)


def call(name, **args):
    return json.loads(registry.dispatch(name, args, task_id="private-exception-test"))


@pytest.mark.parametrize("path", ["/", "/_portal/app.css", "/hours/?period=2026-10-05",
    "/timecard/", "/sharebox/report%20one.html", "/surgery-docs/", "/vacation/",
    "/family/", "/call-schedule/"])
def test_approved_routes_pass_navigation_content_and_input_guards(configured, monkeypatch, path):
    url = ORIGIN + path
    commands = []

    def command(task, name, args, **kwargs):
        commands.append(name)
        return {"success": True, "data": {"url": url, "title": "Synthetic",
            "snapshot": "- textbox Name [ref=e1]", "refs": {"e1": {}}, "result": url}}

    monkeypatch.setattr(session, "_get_session_info", lambda *a: {"_first_nav": False})
    monkeypatch.setattr(session, "_run_browser_command", command)
    monkeypatch.setattr(bt._cdp, "_ensure_cdp_supervisor", lambda *a: None)
    assert call("browser_navigate", url=url)["success"] is True
    assert call("browser_snapshot")["success"] is True
    assert call("browser_click", ref="e1")["success"] is True
    assert call("browser_type", ref="e1", text="Synthetic only")["success"] is True
    assert call("browser_press", key="Tab")["success"] is True
    assert call("browser_back")["success"] is True
    assert _browser_cdp_private_guard(task_id="private-exception-test", method="DOM.getDocument", params={}) is None
    assert {"open", "snapshot", "click", "fill", "press", "back"} <= set(commands)


@pytest.mark.parametrize("suffix", ["/location/setup", "/private-input/r/test", "/hermes/",
    "/sunshine/", "/browser/", "/hours-other/", "/hours/../location/setup",
    "/hours/%2e%2e/location/setup", "/hours/%252e%252e/x", "/hours/%2f../x",
    "/hours//x", "/hours/%5cx", "/hours/%00x", "/hours/%xx", "/hours/?period=wrong", "/hours/?period=2026-02-30",
    "/hours/?other=1", "/hours/?period=2026-10-05&period=2026-10-19"])
def test_unapproved_or_ambiguous_paths_are_refused(configured, suffix):
    assert bt.evaluate_url_safety(ORIGIN + suffix) is not None


@pytest.mark.parametrize("url", [f"http://{HOST}/hours/", f"https://{HOST}:8443/hours/",
    f"https://user@{HOST}/hours/", "https://100.79.31.106/hours/",
    "http://127.0.0.1:8741/hours/", "http://169.254.169.254/latest/meta-data/",
    "https://other.tailb4105a.ts.net/hours/"])
def test_other_origins_and_metadata_remain_refused(configured, url):
    assert bt.evaluate_url_safety(url) is not None


def test_redirect_eval_and_cdp_use_the_same_boundary(configured, monkeypatch):
    denied = ORIGIN + "/location/setup"
    allowed = ORIGIN + "/hours/"
    commands = []
    monkeypatch.setattr(session, "_run_browser_command", lambda *a, **k:
        commands.append(a) or {"success": True, "data": {"result": denied}})
    assert bt._post_redirect_block("test", "https://example.com", allowed, False) is None
    assert bt._post_redirect_block("test", allowed, denied, False) is not None
    assert commands[-1][1:] == ("open", ["about:blank"])
    assert policy._expression_targets_private_url(f"fetch('{allowed}')") is None
    assert policy._expression_targets_private_url(f"fetch('{denied}')") == denied
    assert _browser_cdp_private_guard(task_id="test", method="Page.navigate", params={"url": denied}) is not None
    assert _browser_cdp_private_guard(task_id="test", method="Page.navigate", params={"url": allowed}) is None
    assert _browser_cdp_private_guard(task_id="test", method="DOM.getDocument", params={}) is not None


def test_dns_failure_mixed_answers_and_public_rebinding_fail_closed(configured, monkeypatch):
    for addresses in [[], ["100.79.31.107"], [ADDRESS, "127.0.0.1"], ["8.8.8.8"]]:
        monkeypatch.setattr(socket, "getaddrinfo", lambda *a, **k: [
            (socket.AF_INET, socket.SOCK_STREAM, 6, "", (ip, 443)) for ip in addresses])
        assert bt.evaluate_url_safety(ORIGIN + "/hours/") is not None
    def failure(*args, **kwargs):
        raise socket.gaierror("synthetic")
    monkeypatch.setattr(socket, "getaddrinfo", failure)
    assert bt.evaluate_url_safety(ORIGIN + "/hours/") is not None


def test_profile_endpoint_and_website_denials_are_preserved(configured, monkeypatch, tmp_path):
    url = ORIGIN + "/hours/"
    assert bt.evaluate_url_safety(url) is None
    other = tmp_path / "other"
    other.mkdir()
    (other / "config.yaml").write_text(json.dumps({"browser": {"cdp_url": RULE["cdp_url"]}}), encoding="utf-8")
    token = set_hermes_home_override(other)
    try:
        assert bt.evaluate_url_safety(url) is not None
    finally:
        reset_hermes_home_override(token)
    assert bt.evaluate_url_safety(url) is None
    monkeypatch.setenv("BROWSER_CDP_URL", "http://127.0.0.1:9333")
    assert bt.evaluate_url_safety(url) is not None
    monkeypatch.delenv("BROWSER_CDP_URL")
    monkeypatch.setattr(bt, "check_website_access", lambda url: {
        "message": "Denied by owner", "host": HOST, "rule": HOST, "source": "test"})
    assert bt.evaluate_url_safety(url) is not None
    assert policy._url_blocked(bt, url)
