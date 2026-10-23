"""Literal argument quoting must not expose ampersands to foreground guidance."""

import json
from unittest.mock import MagicMock, patch

import pytest

from tools.terminal_tool_guards import _foreground_background_guidance as guidance


@pytest.mark.parametrize("command", [
    '''family-logistics calendar create --title "Parents' dinner" --description 'Willy & Mady Cass' ''',
    '''printf '%s' 'Willy & Mady Cass' "Parents' dinner"''',
    '''printf '%s' "Parents' dinner" 'Willy & Mady Cass' ''',
    '''printf '%s' 'A "quoted" name' "Willy & Mady Cass"''',
    r'''printf '%s' "A \"quoted\" name" 'Willy & Mady Cass' ''',
    r'''printf '%s' 'Parents'\'' dinner' 'Willy & Mady Cass' ''',
    r"printf '%s' Willy\ \&\ Mady",
    "printf '%s' 'Willy & Mady Cass' && echo done",
    "printf '%s' 'Willy & Mady Cass' 2>&1",
    "printf '%s' 'Willy & Mady Cass' &>/tmp/guard-output",
    "printf '%s' 'literal $(sleep 10 &) and `nohup test`'",
])
def test_literal_arguments_are_allowed(command):
    assert guidance(command) is None


@pytest.mark.parametrize("command", [
    '''printf '%s' "Parents' dinner" 'Willy & Mady Cass' &''',
    '''printf '%s' "Parents' dinner" 'Willy & Mady Cass' & echo done''',
    '''printf '%s' "Parents' dinner"; nohup sleep 10''',
    '''printf '%s' "Parents' dinner"; setsid sleep 10''',
    '''printf '%s' "Parents' dinner"; disown''',
    '''printf '%s' "$(sleep 10 & echo ready)"''',
    '''printf '%s' "`sleep 10 & echo ready`"''',
    '''printf '%s' `sleep 10 & echo ready`''',
    '''printf '%s' "$(printf '%s' 'x & y'; sleep 10 & echo ready)"''',
    '''printf '%s' 'unterminated & shell''',
    r'''printf '%s' "unterminated \" & shell''',
    ": # a misleading quote ' \nsleep 10 &",
    "printf '%s' $'uncertain \\' & shell'",
])
def test_executable_or_ambiguous_backgrounding_stays_visible(command):
    assert guidance(command) is not None


@pytest.mark.parametrize("backgrounded", [False, True])
def test_terminal_preflight_executes_literal_once_and_blocks_real_operator(backgrounded):
    from tools.terminal_tool import terminal_tool

    command = '''printf '%s' "Parents' dinner" 'Willy & Mady Cass' '''
    if backgrounded:
        command += "&"
    config = {"env_type": "local", "timeout": 180, "cwd": "/tmp", "host_cwd": None}
    backend = MagicMock()
    backend.execute.return_value = {"output": "literal output", "returncode": 0}
    with (
        patch("tools.terminal_tool._get_env_config", return_value=config),
        patch("tools.terminal_tool._start_cleanup_thread"),
        patch("tools.terminal_tool._active_environments", {"default": backend}),
        patch("tools.terminal_tool._last_activity", {"default": 0}),
        patch("tools.terminal_tool._check_all_guards", return_value={"approved": True}),
    ):
        result = json.loads(terminal_tool(command=command))

    if backgrounded:
        assert "'&' backgrounding" in result["error"]
        backend.execute.assert_not_called()
    else:
        assert result.get("error") is None
        backend.execute.assert_called_once()
        assert backend.execute.call_args.args[0] == command
