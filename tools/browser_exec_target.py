"""Exact-tab binding for ``browser_exec`` on an explicitly configured shared CDP browser.

Enabled only by ``browser.exact_target_binding: true`` with a CDP override (a browser that people or
other sessions may also use). The harness process reports the tab it was attached to before the model's
first helper call and when it exits (``tools/browser_use_shot.py`` target hook). Hermes then binds the
per-task CDP supervisor to that exact target, so vault/credential fills only ever touch the tab
``browser_exec`` actually drove.

Rules carried from the maintained fork fix (agent-browser era, d05b5d48):
- no origin/title inference and no neighbor-tab recovery;
- a tab that changed between calls is refused before the model's code acts, until the code selects a
  tab explicitly (``switch_tab``/``new_tab``) or only inspects tabs;
- an unconfirmed outcome (timeout, killed harness, missing report) is never replayed and blocks secret
  fills until a later call completes with a verified target;
- secret fills are refused while a ``browser_exec`` call for the task is in flight.
"""

from __future__ import annotations

import json
import logging
import os
import tempfile
import threading
from typing import Any, Optional

REPORT_ENV = "HERMES_BU_TARGET_REPORT"
EXPECTED_ENV = "HERMES_BU_EXPECTED_TARGET"

_lock = threading.Lock()
# Per route (task + named session: each named session has its own harness daemon and current tab).
_states: dict[str, dict[str, Any]] = {}
# Per task: the one CDP supervisor follows the route that last verified a target; calls in flight.
_tasks: dict[str, dict[str, Any]] = {}

UNCERTAIN_MESSAGE = ("The browser call did not finish cleanly, so its effect on the page is unknown. Inspect the "
                     "page (current_tab(), page_info()) before acting; do not repeat the action.")
CHANGED_MESSAGE = ("The browser tab changed since the last call (another session or a person switched it). No "
                   "code ran. Inspect with list_tabs()/current_tab(), then select the intended tab explicitly "
                   "with switch_tab(...) or new_tab(...).")


def enabled() -> bool:
    try:
        from tools.browser_session_cdp import get_cdp_override_raw
        from tools.browser_session_cloud import browser_cfg
        return bool(get_cdp_override_raw()) and browser_cfg("exact_target_binding", False, lambda v: v is True)
    except Exception:  # health: allow BLE001 -- unreadable browser config fails closed (strict binding stays on)
        logging.getLogger(__name__).warning("exact_target_binding unreadable; keeping strict binding", exc_info=True)
        return True


def _task(task_id: Optional[str]) -> dict[str, Any]:
    with _lock:
        return _tasks.setdefault(task_id or "default", {"route": None, "in_flight": 0,
                                                        "lock": threading.RLock()})


def state(route_key: str) -> dict[str, Any]:
    with _lock:
        return _states.setdefault(route_key, {"expected": None, "reconcile": False})


def task_lock(task_id: Optional[str]) -> threading.RLock:
    return _task(task_id)["lock"]


def install_admission(supervisor: Any, task_id: Optional[str]) -> None:
    """Point the supervisor's command/secret admission at the task's verified binding."""
    task = _task(task_id)

    def admitted() -> bool:
        route = task["route"]
        if route is None or task["in_flight"]:
            return False
        st = state(route)
        return bool(not st["reconcile"] and supervisor.matches_command_target(st["expected"]))
    supervisor._command_admission = admitted
    supervisor._secret_admission = admitted


def begin(env: dict, task_id: Optional[str], route_key: str) -> str:
    """Arm the harness hook for one call (caller holds ``task_lock``); returns the private report path."""
    task, st = _task(task_id), state(route_key)
    with _lock:
        task["in_flight"] += 1
    report_dir = tempfile.mkdtemp(prefix="hermes-bu-target-")
    path = os.path.join(report_dir, "report.json")
    env[REPORT_ENV] = path
    if st["expected"]:  # also after a refusal: only an explicit tab selection may proceed
        env[EXPECTED_ENV] = st["expected"]
    else:
        env.pop(EXPECTED_ENV, None)
    return path


def _read_report(path: str) -> Optional[dict]:
    try:
        with open(path, encoding="utf-8-sig") as stream:
            report = json.load(stream)
        return report if isinstance(report, dict) else None
    except (OSError, ValueError):
        return None
    finally:
        try:
            os.unlink(path)
            os.rmdir(os.path.dirname(path))
        except OSError:
            pass


def finish(task_id: Optional[str], route_key: str, path: str, supervisor: Any) -> dict[str, Any]:
    """Reconcile one call. Returns ``{}`` when verified, else fields to merge into the result
    (``target_error`` = refused before acting, ``target_warning`` = ran but unverified)."""
    task, st = _task(task_id), state(route_key)
    try:
        return _reconcile(st, task, route_key, _read_report(path), supervisor)
    finally:
        with _lock:
            task["in_flight"] -= 1


def _unverified(st: dict, task: dict, route_key: str, message: str) -> dict[str, Any]:
    st["reconcile"], st["expected"] = True, None
    task["route"] = route_key  # the supervisor's binding is no longer trustworthy for this task
    return {"target_warning": message, "target_verified": False}


def _reconcile(st, task, route_key, report, supervisor) -> dict[str, Any]:
    if report is None:
        return _unverified(st, task, route_key, UNCERTAIN_MESSAGE)
    if report.get("refused") == "target_changed":
        st["reconcile"] = "changed"
        task["route"] = route_key
        return {"target_error": CHANGED_MESSAGE}
    target = report.get("post")
    if not isinstance(target, str) or not target:
        return _unverified(st, task, route_key, UNCERTAIN_MESSAGE)
    if supervisor is None:
        return _unverified(st, task, route_key,
                           "The browser supervisor is unavailable; credential fills stay disabled.")
    if not supervisor.matches_command_target(target):
        if not supervisor.bind_command_target(target).get("ok"):
            return _unverified(st, task, route_key, "Could not bind the browser tab this call used; credential "
                                                    "fills stay disabled until a later call verifies it.")
    st["expected"], st["reconcile"] = target, False
    task["route"] = route_key
    return {}
