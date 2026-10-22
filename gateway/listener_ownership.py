"""Explicit plugin-owned per-profile listeners, shared by boot and migration preflight."""
from gateway.platform_registry import platform_registry


def profile_listener_claim(platform: str, config) -> tuple | None:
    entry = platform_registry.get(platform)
    if entry is None or entry.profile_listener_fn is None:
        return None
    endpoint = entry.profile_listener_fn(config)
    if endpoint is None:
        return None
    return listener_claim(*endpoint)


def listener_claim(host, port) -> tuple | None:
    """One TCP endpoint identity across plugin declarations and built-in sidecars."""
    if not isinstance(host, str) or not host.strip() or type(port) is not int or not 0 <= port <= 65535:
        raise ValueError("Invalid profile listener declaration")
    # Port zero is an isolated ephemeral listener, not a claim on a fixed endpoint.
    return ("listener", host.strip().lower(), port) if port else None
