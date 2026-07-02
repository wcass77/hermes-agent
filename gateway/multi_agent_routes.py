"""Shared active-route state for multi-profile messaging gateways."""

from __future__ import annotations

import json
import os
import time
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Iterator, Optional

try:
    import fcntl
except Exception:  # pragma: no cover - non-Unix fallback
    fcntl = None  # type: ignore[assignment]


STATE_ENV = "HERMES_MULTI_AGENT_ROUTE_STATE"


def current_profile_name() -> str:
    """Return the active Hermes profile name for this gateway process."""
    try:
        from hermes_cli.profiles import get_active_profile_name

        return get_active_profile_name() or "default"
    except Exception:
        return os.getenv("HERMES_PROFILE", "default") or "default"


def state_path() -> Optional[Path]:
    raw = os.getenv(STATE_ENV, "").strip()
    if not raw:
        return None
    return Path(raw)


@contextmanager
def _locked_state(path: Path) -> Iterator[dict[str, Any]]:
    path.parent.mkdir(parents=True, exist_ok=True)
    lock_path = path.with_suffix(path.suffix + ".lock")
    with lock_path.open("a+") as lock_file:
        if fcntl is not None:
            fcntl.flock(lock_file.fileno(), fcntl.LOCK_EX)
        try:
            try:
                data = json.loads(path.read_text(encoding="utf-8"))
            except Exception:
                data = {}
            if not isinstance(data, dict):
                data = {}
            data.setdefault("routes", {})
            yield data
            tmp = path.with_suffix(path.suffix + ".tmp")
            tmp.write_text(json.dumps(data, sort_keys=True), encoding="utf-8")
            tmp.replace(path)
        finally:
            if fcntl is not None:
                fcntl.flock(lock_file.fileno(), fcntl.LOCK_UN)


def _read_routes(path: Path) -> dict[str, Any]:
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return {}
    if not isinstance(data, dict):
        return {}
    routes = data.get("routes")
    return routes if isinstance(routes, dict) else {}


def active_owner(route_key: str) -> Optional[str]:
    path = state_path()
    if not path or not route_key:
        return None
    entry = _read_routes(path).get(route_key)
    if not isinstance(entry, dict):
        return None
    owner = str(entry.get("profile") or "").strip()
    return owner or None


def claim_route(route_key: str, *, profile: Optional[str] = None) -> None:
    path = state_path()
    if not path or not route_key:
        return
    owner = profile or current_profile_name()
    with _locked_state(path) as data:
        routes = data.setdefault("routes", {})
        routes[route_key] = {"profile": owner, "updated_at": int(time.time())}


def route_allows_message(
    route_key: str,
    *,
    mentioned: bool,
    profile: Optional[str] = None,
) -> bool:
    """Return whether this profile should process an inbound route event.

    An explicit mention claims/transfers the route to the current profile. An
    unmentioned event is allowed unless another profile has already claimed the
    same route.
    """
    if not route_key:
        return True
    owner = profile or current_profile_name()
    if mentioned:
        claim_route(route_key, profile=owner)
        return True
    active = active_owner(route_key)
    return not active or active == owner


def discord_route_key(*, guild_id: Optional[str], channel_id: str) -> str:
    scope = str(guild_id or "dm")
    return f"discord:{scope}:{channel_id}"


def zulip_route_key(*, site_url: str, stream_id: Any, topic: str) -> str:
    site = str(site_url or "").rstrip("/").lower()
    return f"zulip:{site}:{stream_id}:{topic}"
