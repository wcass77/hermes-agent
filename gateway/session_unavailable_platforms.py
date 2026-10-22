"""Retain dormant plugin routes without admitting them as active conversations."""
from __future__ import annotations

import json


def unavailable_platform_entry(entry_json: str):
    """Validated routing data whose origin platform has not registered yet, or None."""
    from gateway.config import Platform
    from gateway.session import SessionEntry

    try:
        data = json.loads(entry_json)
        origin = data.get("origin") if isinstance(data, dict) else None
        name = origin.get("platform") if isinstance(origin, dict) else None
        if not isinstance(name, str) or not name.strip():
            return None
        try:
            Platform(name)
        except ValueError:
            # Still reject malformed session IDs/keys and timestamps. Only platform
            # availability is deferred; preserved data never enters the runtime map.
            SessionEntry.from_dict({**data, "origin": None, "platform": None})
            return data
    except (ValueError, KeyError, TypeError):
        return None
    return None


def recover_available_entries(store) -> None:
    """Plugin discovery can finish after the routing index's first read."""
    from gateway.config import Platform

    pending = getattr(store, "_unavailable_platform_entries", {})
    for key, data in list(pending.items()):
        try:
            Platform(data["origin"]["platform"])
        except ValueError:
            continue
        entry = store._routing_entry_from_json(key, json.dumps(data))
        if entry is not None:
            store._entries.setdefault(key, entry)
            del pending[key]


def rekey_unavailable_entries(store, old: str, new: str) -> int:
    """Dormant routes follow the same collision and namespace rules as active routes."""
    import copy

    pending = getattr(store, "_unavailable_platform_entries", {})
    old_ns, new_ns = f"agent:{old}:", f"agent:{new}:"
    all_keys = pending.keys() | store._entries.keys()
    moving = [key for key in all_keys if key.startswith(old_ns)]
    if any(new_ns + key[len(old_ns):] in all_keys for key in moving):
        raise ValueError("profile routing collision while renaming dormant routes")
    count = 0
    for key in moving:
        if key not in pending:
            continue
        data = copy.deepcopy(pending.pop(key))
        new_key = new_ns + key[len(old_ns):]
        data["session_key"] = new_key
        if data["origin"].get("profile") == old:
            data["origin"]["profile"] = new
        pending[new_key] = data
        count += 1
    return count


def remove_unavailable_entries(store, predicate) -> int:
    pending = getattr(store, "_unavailable_platform_entries", {})
    keys = [key for key, data in pending.items() if predicate(key, data)]
    for key in keys:
        del pending[key]
    return len(keys)
