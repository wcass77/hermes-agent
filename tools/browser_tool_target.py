"""Exact-tab command bridge for explicitly configured, pinned CDP drivers.

The driver must support --pin-tab and tab-list targetId metadata (agent-browser
0.34). No origin/title inference, write retry, or neighbor-tab recovery.
"""
import threading

_LOCK_CREATION = threading.Lock()


def enabled(bt, info):
    return bool((info.get('features') or {}).get('cdp_override')
                and bt._browser_cfg('exact_target_binding', False, bool, 'browser.exact_target_binding'))


def _error(code, message):
    return {'success': False, 'code': code, 'error': message}


def dispatch(task_id, info, command, args, invoke, supervisor):
    with _LOCK_CREATION:
        lock = info.setdefault('_target_command_lock', threading.RLock())
    with lock:
        return _dispatch_locked(task_id, info, command, args, invoke, supervisor)


def _active(invoke):
    result = invoke('tab', ['list'])
    if not result.get('success'):
        return None
    tabs = (result.get('data') or {}).get('tabs')
    if not isinstance(tabs, list):
        return None
    active = [t for t in tabs if isinstance(t, dict) and t.get('active') is True]
    if len(active) != 1 or not isinstance(active[0].get('targetId'), str) or not active[0]['targetId']:
        return None
    return active[0]


def _dispatch_locked(task_id, info, command, args, invoke, supervisor):
    info['exact_target_binding'] = True
    if supervisor is not None:
        supervisor._command_admission = lambda: bool(
            not info.get('target_reconciliation_required')
            and supervisor.matches_command_target(info.get('command_target_id')))
        supervisor._secret_admission = lambda: bool(
            supervisor._command_admission() and not info.get('fresh_snapshot_required'))
    if command == 'close':
        # Cleanup must not start a new daemon merely to inspect the closed one.
        return invoke(command, args)
    explicit_select = command == 'tab' and args and args[0] not in ('list', 'close')
    before = _active(invoke)
    if before is None:
        info['target_reconciliation_required'] = True
        return _error('target_identity_unavailable', 'The pinned browser target could not be verified; no action was sent.')
    expected = info.get('command_target_id')
    if expected and expected != before['targetId'] and not explicit_select:
        info['target_reconciliation_required'] = True
        return _error('target_changed', 'The pinned browser target changed; select the intended tab explicitly.')
    if info.get('target_reconciliation_required') and command not in ('snapshot', 'get', 'tab'):
        return _error('target_reconciliation_required', 'Read a fresh snapshot of the verified target before sending another action.')
    if info.get('fresh_snapshot_required') and command not in ('snapshot', 'get', 'tab', 'open', 'back', 'forward', 'reload', 'console', 'errors'):
        return _error('fresh_snapshot_required', 'The selected page changed; obtain a fresh snapshot before using element references.')
    info['command_target_id'] = before['targetId']
    if supervisor is None:
        return _error('supervisor_unavailable', 'The pinned browser supervisor is unavailable; no action was sent.')
    if not supervisor.matches_command_target(before['targetId']):
        binding = supervisor.bind_command_target(before['targetId'])
        if not binding.get('ok'):
            return _error('target_binding_failed', 'Could not bind the exact browser target; no action was sent.')
    result = invoke(command, args)
    if not result.get('success'):
        return result  # Never replay the command, including a failed click/capture.
    after = _active(invoke)
    if after is None or (after['targetId'] != before['targetId'] and not explicit_select):
        info['target_reconciliation_required'] = True
        info['fresh_snapshot_required'] = True
        return _error('target_outcome_uncertain', 'The command finished but its target could not be confirmed. Inspect the outcome; do not repeat the action.')
    selected = after['targetId']
    if selected != before['targetId']:
        if not supervisor.bind_command_target(selected).get('ok'):
            info['target_reconciliation_required'] = True
            return _error('target_binding_failed', 'The tab selection finished but supervisor binding failed; inspect the selected tab.')
    info['command_target_id'] = selected
    if command in ('open', 'back', 'forward', 'reload') or selected != before['targetId']:
        info['fresh_snapshot_required'] = True
    if command == 'snapshot':
        info.pop('target_reconciliation_required', None)
        info.pop('fresh_snapshot_required', None)
    data = result.get('data')
    if isinstance(data, dict):
        data['targetId'] = selected
    return result
