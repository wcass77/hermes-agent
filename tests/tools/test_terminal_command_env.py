"""Command-scoped plugin values travel outside argv and never persist in shell state."""
from unittest.mock import patch

import pytest

from tools.environments.command_env import terminal_command_env
from tools.environments.docker import DockerEnvironment


def test_current_hook_values_recomputed_and_invalid_values_refused():
    with patch('hermes_cli.plugins.invoke_hook', side_effect=[[{'CONTEXT': 'first'}], [{'CONTEXT': ''}]]):
        assert terminal_command_env() == {'CONTEXT': 'first'}
        assert terminal_command_env() == {'CONTEXT': ''}
    for value in [{'BAD-NAME': 'x'}, {'VALID': 3}, {'VALID': 'x\0y'}, 'bad']:
        with patch('hermes_cli.plugins.invoke_hook', return_value=[value]), pytest.raises(ValueError):
            terminal_command_env()


def test_values_not_in_docker_argv_and_excluded_without_multiplex():
    env = object.__new__(DockerEnvironment)
    env._container_id, env._docker_exe = 'container', 'docker'
    env._profile_scoped_passthrough = False
    env._docker_client_env = lambda values: values
    with patch('hermes_cli.plugins.invoke_hook', return_value=[{'CONTEXT': 'ephemeral-private-value'}]):
        assert 'CONTEXT' in env._snapshot_excluded_passthrough_names()
        with patch('tools.environments.docker._popen_bash') as spawn:
            env._run_bash('echo hello')
        argv = spawn.call_args.args[0]
        assert 'ephemeral-private-value' not in argv
        assert ['-e', 'CONTEXT'] == argv[2:4]
        assert spawn.call_args.kwargs['env']['CONTEXT'] == 'ephemeral-private-value'
