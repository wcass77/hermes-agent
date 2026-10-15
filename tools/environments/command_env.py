"""Ephemeral per-command plugin environment for Docker terminal execution."""
import re


def terminal_command_env():
    """Resolve under the calling turn scope; never cache values in a container config.

    Hooks return a dict of environment names and strings. Invalid payloads fail
    the command before spawning. Empty values explicitly revoke older context.
    """
    from hermes_cli.plugins import invoke_hook
    values = {}
    for result in invoke_hook("terminal_command_env"):
        if result is None:
            continue
        if not isinstance(result, dict):
            raise ValueError("Invalid terminal environment hook result")
        for name, value in result.items():
            if (not isinstance(name, str) or not re.fullmatch(r"[A-Z_][A-Z0-9_]*", name)
                    or not isinstance(value, str) or "\x00" in value):
                raise ValueError("Invalid terminal environment hook entry")
            if name in values and values[name] != value:
                raise ValueError("Conflicting terminal environment hook entries")
            values[name] = value
    return values
