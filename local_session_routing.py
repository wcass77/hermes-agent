"""Fork-owned session-routing seam for local plugins.

Zhaan's Shared Update and the AgentMail interface (Personal OS
``hermes-plugins/agentmail-interface``) need a few session-routing operations
that upstream's storage interface (v0.23.3+) no longer carries on the
``hermes_state.SessionDB`` compatibility wrapper. Local plugins call these
functions instead of those SessionDB methods, so an upstream storage change is
ported here once, in the merge that brings it, and callers stay unchanged.

Only the non-carried operations live here. Carried SessionDB methods
(create/end/reopen/get/resolve/append/close; supported upstream through v0.26)
stay at their call sites.

The current backend is the fork's v0.23.2 ``SessionDB``. Against a SessionDB
without these methods every function raises ``SessionRoutingNotPorted`` naming
the operation, instead of an AttributeError deep in a caller or a silent
behaviour change. Porting notes for the storage interface are in the Personal OS
review ``projects/req-20261120-003-*/review-20261120.md``.
"""

from __future__ import annotations

from typing import Any, Optional

__all__ = [
    "SessionRoutingNotPorted",
    "compression_tip",
    "find_peer_session",
    "find_session_by_origin",
    "reset_session",
    "stamp_peer",
]


class SessionRoutingNotPorted(RuntimeError):
    """The session store lacks an operation this seam has not been ported to."""


def _legacy(db: Any, name: str):
    method = getattr(db, name, None)
    if not callable(method):
        raise SessionRoutingNotPorted(
            f"local_session_routing: session store {type(db).__name__} has no {name!r}; "
            "port local_session_routing.py to the current storage interface"
        )
    return method


def find_peer_session(
    db: Any, *, source: str, session_key: Optional[str], user_id: Optional[str] = None,
    chat_id: Optional[str] = None, chat_type: Optional[str] = None, thread_id: Optional[str] = None,
) -> Optional[dict[str, Any]]:
    """Latest recoverable session row for a routing peer (dict with ``id``, ``started_at``...), or None."""
    return _legacy(db, "find_latest_gateway_session_for_peer")(
        source=source, user_id=user_id, session_key=session_key,
        chat_id=chat_id, chat_type=chat_type, thread_id=thread_id,
    )


def stamp_peer(
    db: Any, session_id: str, *, source: str, session_key: Optional[str], user_id: Optional[str] = None,
    chat_id: Optional[str] = None, chat_type: Optional[str] = None, thread_id: Optional[str] = None,
    display_name: Optional[str] = None, origin_json: Optional[str] = None,
    include_compression_ancestors: bool = False, transport_profile: Optional[str] = None,
) -> None:
    """Record the gateway routing peer on a session row (optionally its compression ancestors)."""
    _legacy(db, "record_gateway_session_peer")(
        session_id, source=source, user_id=user_id, session_key=session_key,
        chat_id=chat_id, chat_type=chat_type, thread_id=thread_id,
        display_name=display_name, origin_json=origin_json,
        include_compression_ancestors=include_compression_ancestors,
        transport_profile=transport_profile,
    )


def compression_tip(db: Any, session_id: str) -> str:
    """Live tip of a compression chain; the input id when there is no continuation."""
    return _legacy(db, "get_compression_tip")(session_id) or session_id


def find_session_by_origin(
    db: Any, *, platform: str, chat_id: str, thread_id: Optional[str] = None, user_id: Optional[str] = None,
) -> Optional[str]:
    """Newest open keyed session id for a platform origin, or None."""
    return _legacy(db, "find_session_by_origin")(
        platform=platform, chat_id=chat_id, thread_id=thread_id, user_id=user_id,
    )


def reset_session(db: Any, session_id: str, reason: str) -> bool:
    """End a session as an intentional reset boundary (fences peer recovery behind it)."""
    return bool(_legacy(db, "promote_to_session_reset")(session_id, reason))
