# Per-command Docker environment

A plugin may register `terminal_command_env`, returning an environment-name to
string mapping for the current terminal command. It runs under the calling
profile/turn scope. Values travel in the Docker client's environment with
name-only `docker exec -e NAME` flags, and the names are excluded from shell
snapshots even in a standalone gateway. Values are recomputed, never stored in
container configuration; an empty string revokes previous context. Invalid or
conflicting entries refuse command execution. Return `None` when not applicable.

This hook serves the profile-local AgentMail CLI broker: it signs the native
session/message identity for one command without exposing mailbox credentials.
It applies to the Docker terminal backend. It does not isolate mutually
untrusted processes running under the same user in a shared profile container.
