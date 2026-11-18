"""Exact-tab binding for browser_exec on a shared CDP browser (fork policy, carried from d05b5d48)."""
from types import ModuleType, SimpleNamespace
from unittest.mock import Mock
import json
import os

import pytest

from tools import browser_exec_target as target
from tools import browser_use_shot as shot


@pytest.fixture(autouse=True)
def _fresh_state():
    target._states.clear()
    target._tasks.clear()
    yield
    target._states.clear()
    target._tasks.clear()


class FakeSupervisor:
    def __init__(self, bind_ok=True):
        self.bound = None
        self.bind_ok = bind_ok
        self.binds = []

    def matches_command_target(self, target_id):
        return bool(target_id and target_id == self.bound)

    def bind_command_target(self, target_id):
        self.binds.append(target_id)
        if self.bind_ok:
            self.bound = target_id
            return {"ok": True}
        self.bound = None
        return {"ok": False}


def _write(path, report):
    with open(path, "w") as stream:
        json.dump(report, stream)


def _call(report, sup, task="t", route="r"):
    env = {}
    path = target.begin(env, task, route)
    if report is not None:
        _write(path, report)
    return env, target.finish(task, route, path, sup)


# ---------------------------------------------------------------- host reconciliation

def test_verified_call_binds_supervisor_and_admits_secrets():
    sup = FakeSupervisor()
    target.install_admission(sup, "t")
    assert not sup._secret_admission()  # nothing verified yet
    env, fields = _call({"pre": None, "post": "A", "checked": True}, sup)
    assert fields == {} and sup.binds == ["A"]
    assert sup._command_admission() and sup._secret_admission()
    env, _ = _call({"pre": "A", "post": "A", "checked": True}, sup)
    assert env[target.EXPECTED_ENV] == "A"
    assert sup.binds == ["A"]  # already bound: no rebind


def test_missing_report_is_uncertain_and_blocks_secrets_without_replay():
    sup = FakeSupervisor()
    target.install_admission(sup, "t")
    _call({"post": "A"}, sup)
    env, fields = _call(None, sup)  # timeout / killed harness
    assert fields["target_verified"] is False and "do not repeat" in fields["target_warning"]
    assert not sup._secret_admission()
    env, _ = _call({"post": "A"}, sup)
    assert target.EXPECTED_ENV not in env  # unknown tab: nothing to compare against
    assert sup._secret_admission()  # a later clean call reconciles


def test_changed_tab_refusal_keeps_guard_until_explicit_selection():
    sup = FakeSupervisor()
    target.install_admission(sup, "t")
    _call({"post": "A"}, sup)
    env, fields = _call({"pre": "B", "refused": "target_changed"}, sup)
    assert "No code ran" in fields["target_error"]
    assert not sup._secret_admission()
    env, fields = _call({"pre": None, "post": "B", "checked": True}, sup)  # code selected B explicitly
    assert env[target.EXPECTED_ENV] == "A" and fields == {}
    assert sup.bound == "B" and sup._secret_admission()


def test_binding_failure_blocks_secrets():
    sup = FakeSupervisor(bind_ok=False)
    target.install_admission(sup, "t")
    _, fields = _call({"post": "A"}, sup)
    assert fields["target_verified"] is False and not sup._secret_admission()


def test_secret_admission_closed_while_call_in_flight():
    sup = FakeSupervisor()
    target.install_admission(sup, "t")
    _call({"post": "A"}, sup)
    env = {}
    path = target.begin(env, "t", "r")
    assert not sup._secret_admission()
    _write(path, {"post": "A"})
    target.finish("t", "r", path, sup)
    assert sup._secret_admission()


def test_named_sessions_keep_separate_expected_tabs():
    sup = FakeSupervisor()
    target.install_admission(sup, "t")
    _call({"post": "A"}, sup, route="t::one")
    _call({"post": "B"}, sup, route="t::two")
    env = {}
    path = target.begin(env, "t", "t::one")
    assert env[target.EXPECTED_ENV] == "A"
    _write(path, {"post": "A"})
    target.finish("t", "t::one", path, sup)
    assert sup.bound == "A" and sup._secret_admission()


def test_report_file_is_private_and_removed(tmp_path):
    env = {}
    path = target.begin(env, "t", "r")
    assert oct(os.stat(os.path.dirname(path)).st_mode & 0o777) == "0o700"
    _write(path, {"post": "A"})
    target.finish("t", "r", path, FakeSupervisor())
    assert not os.path.exists(os.path.dirname(path))


# ---------------------------------------------------------------- harness-side guard

def _helpers(attached):
    mod = ModuleType("browser_harness.helpers")
    calls = []

    def _send(req):
        assert req == {"meta": "current_tab"}
        return {"targetId": attached[0]}

    def make(name):
        def fn(*a, **k):
            calls.append(name)
            if name == "switch_tab":
                attached[0] = a[0]
            return name
        fn.__module__ = shot._HARNESS_MODULE
        fn.__name__ = name
        return fn

    mod._send = _send
    for name in ("js", "goto_url", "click_at_xy", "list_tabs", "current_tab", "switch_tab", "new_tab", "cdp",
                 "capture_screenshot"):
        setattr(mod, name, make(name))
    return mod, calls


@pytest.fixture
def hook(monkeypatch, tmp_path):
    registered = []
    monkeypatch.setattr("atexit.register", lambda fn: registered.append(fn))
    report = tmp_path / "report.json"
    monkeypatch.setenv(shot.TARGET_REPORT_ENV, str(report))

    def run():
        for fn in registered:
            fn()
        return json.loads(report.read_text())
    return SimpleNamespace(run=run, report=report)


def test_guard_refuses_action_on_changed_tab(monkeypatch, hook):
    monkeypatch.setenv(shot.TARGET_EXPECTED_ENV, "A")
    attached = ["B"]
    helpers, calls = _helpers(attached)
    shot.install_target_report(helpers)
    with pytest.raises(SystemExit):
        helpers.click_at_xy(1, 2)
    assert calls == []  # the click never reached the harness
    assert hook.run() == {"pre": "B", "checked": True, "refused": "target_changed"}


def test_guard_allows_inspection_then_explicit_selection(monkeypatch, hook):
    monkeypatch.setenv(shot.TARGET_EXPECTED_ENV, "A")
    attached = ["B"]
    helpers, calls = _helpers(attached)
    shot.install_target_report(helpers)
    helpers.list_tabs()
    helpers.current_tab()
    helpers.switch_tab("C")
    helpers.click_at_xy(1, 2)
    assert calls == ["list_tabs", "current_tab", "switch_tab", "click_at_xy"]
    assert hook.run()["post"] == "C"


def test_guard_passes_matching_tab_and_reports_post(monkeypatch, hook):
    monkeypatch.setenv(shot.TARGET_EXPECTED_ENV, "A")
    attached = ["A"]
    helpers, calls = _helpers(attached)
    shot.install_target_report(helpers)
    helpers.js("1")
    helpers.goto_url("https://example.test")
    rep = hook.run()
    assert rep["pre"] == "A" and rep["post"] == "A" and "refused" not in rep


def test_guard_without_expected_only_reports(monkeypatch, hook):
    monkeypatch.delenv(shot.TARGET_EXPECTED_ENV, raising=False)
    attached = ["Z"]
    helpers, calls = _helpers(attached)
    shot.install_target_report(helpers)
    helpers.click_at_xy(0, 0)
    assert hook.run()["post"] == "Z"


def test_install_guards_replaced_screenshot_helper(monkeypatch, hook):
    monkeypatch.setenv(shot.TARGET_EXPECTED_ENV, "A")
    attached = ["B"]
    helpers, calls = _helpers(attached)
    shot.install(helpers)  # replaces capture_screenshot, then guards everything
    assert getattr(helpers.capture_screenshot, "__wrapped__", None) is not None
    with pytest.raises(SystemExit):
        helpers.capture_screenshot()


# ---------------------------------------------------------------- supervisor (carried from d05b5d48)

def test_strict_origin_focus_does_not_search_neighbor_tabs():
    from tools.browser_supervisor import CDPSupervisor
    s = CDPSupervisor('t', 'ws://synthetic', command_binding_required=True)
    s._loop = SimpleNamespace(is_running=lambda: True)
    s._secret_admission = lambda: True
    s._cdp = Mock(side_effect=AssertionError('no target enumeration or selection'))
    s.evaluate_runtime = lambda expression: {'ok': True, 'result': 'https://wrong.test'}
    assert not s.focus_page('https://same.test')['ok']
    s.evaluate_runtime = lambda expression: {'ok': True, 'result': 'https://same.test' if expression == 'location.origin' else True}
    assert s.focus_page('https://same.test', accept='synthetic form')['ok']
    s._cdp.assert_not_called()


def test_strict_evaluation_refuses_before_secret_dispatch():
    from tools.browser_supervisor import CDPSupervisor
    s = CDPSupervisor('t', 'ws://synthetic', command_binding_required=True)
    s._cdp = Mock(side_effect=AssertionError('must not send secret'))
    r = s.evaluate_runtime('synthetic-secret-expression')
    assert not r['ok'] and 'fresh snapshot' in r['error']
    s._cdp.assert_not_called()


def test_vault_supervisor_refuses_unbound_supervisor_on_shared_browser(monkeypatch):
    from tools import browser_vault_tool
    from tools.browser_supervisor import SUPERVISOR_REGISTRY
    monkeypatch.setattr(target, "enabled", lambda: True)
    monkeypatch.setattr(SUPERVISOR_REGISTRY, "get", lambda task_id: SimpleNamespace(_command_binding_required=False))
    assert browser_vault_tool._ensure_supervisor("t") is None
    monkeypatch.setattr(SUPERVISOR_REGISTRY, "get", lambda task_id: None)
    assert browser_vault_tool._ensure_supervisor("t") is None  # never starts an unbound one
