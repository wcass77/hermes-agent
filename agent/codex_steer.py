"""Steer delivery for the Codex app-server runtime, which owns its own turn loop.

Nothing in that runtime drains ``AIAgent._pending_steer``, so a steer reaches the turn only through
native ``turn/steer``. A steer is buffered exactly like a native-loop steer (same parts, tickets and
fates) and a per-agent flusher thread sends the buffer while a Codex turn is live: no caller ever
waits on the JSON-RPC (the gateway loop, the authority's stream lock, the turn's own thread).

- Before the turn exists (a steer latched by ``adopt_agent``, one sent while the turn starts), it
  waits in the buffer; ``CodexAppServerSession.on_turn_active`` marks the turn live and starts the
  flusher. Insertion and activation share ``_pending_steer_lock``, so neither misses the other.
- A definite refusal (the turn ended, codex rejected it) puts the steers back in front of the buffer
  and stops sending. A sent request without a reply (timeout, transport lost after the write) reads
  as delivered: it is never replayed.
- When the turn returns (``run_codex_app_server_turn`` here, what ``AIAgent`` forwards to), the flusher is joined, then what is left is settled
  as the native finalizer does: ticketless text goes back to the surface as ``pending_steer`` (the
  next turn); a ticketed steer stays buffered for its claim's settlement (``withdraw_steer``) to
  requeue. A failed turn that may fall back to the generic loop keeps its buffer for that loop.
"""
import logging
import threading

logger = logging.getLogger(__name__)

_JOIN_TIMEOUT_S = 30.0


def _lock(agent):
    from agent.interrupt_control import _ic_lock
    return _ic_lock(agent, "_pending_steer_lock")


def codex_steer(agent, text: str, ticket, images: tuple) -> bool:
    """``AIAgent.steer`` under ``api_mode == "codex_app_server"``: buffered, sent by the flusher."""
    from agent.interrupt_control import _ic_lock, _set_steer_parts, _steer_parts
    with _ic_lock(agent, "_pending_redirect_lock"):
        if agent._interrupt_requested:
            return False
    part = (ticket, text, images) if images else (ticket, text)
    with _lock(agent):
        _set_steer_parts(agent, _steer_parts(agent) + [part])
        if vars(agent).get("_codex_steer_live"):
            _start_flusher_locked(agent)
    agent._turn_user_intervened = True
    return True


def codex_turn_active(agent) -> None:
    """``CodexAppServerSession.on_turn_active``: the turn accepts ``turn/steer`` from now on."""
    with _lock(agent):
        agent._codex_steer_live = True
        _start_flusher_locked(agent)


def _start_flusher_locked(agent) -> None:
    from agent.interrupt_control import _steer_parts
    if vars(agent).get("_codex_steer_flusher") is not None or not _steer_parts(agent):
        return
    flusher = threading.Thread(target=_flush, args=(agent,), name="codex-steer-flush", daemon=True)
    agent._codex_steer_flusher = flusher
    flusher.start()


def _flush(agent) -> None:
    from agent.interrupt_control import _set_steer_parts, _steer_fates, _steer_part_images, _steer_parts
    while True:
        with _lock(agent):
            parts = _steer_parts(agent)
            if not parts or not vars(agent).get("_codex_steer_live"):
                agent._codex_steer_flusher = None
                return
            _set_steer_parts(agent, [])
            for part in parts:
                if part[0] is not None:
                    _steer_fates(agent)[part[0]] = True
        text = "\n".join(part[1] for part in parts)
        images = [path for part in parts for path in _steer_part_images(part)]
        if _send(getattr(agent, "_codex_session", None), text, images):
            continue
        with _lock(agent):
            for part in parts:
                _steer_fates(agent).pop(part[0], None)
            _set_steer_parts(agent, parts + _steer_parts(agent))
            agent._codex_steer_live = False
            agent._codex_steer_flusher = None
        return


def _send(session, text: str, images) -> bool:
    if session is None:
        return False
    try:
        if images:
            from agent.steer_images import codex_steer_text
            return bool(session.request_steer(codex_steer_text(text, images), images=images))
        return bool(session.request_steer(text))
    except Exception:
        logger.warning("Codex app-server turn/steer failed", exc_info=True)
        return False


def run_codex_app_server_turn(agent, *args, **kwargs):
    """``agent.codex_runtime.run_codex_app_server_turn`` (``AIAgent._run_codex_app_server_turn``), with the
    turn's steer buffer settled on every exit."""
    from agent.codex_runtime import run_codex_app_server_turn as run
    result = None
    try:
        result = run(agent, *args, **kwargs)
    finally:
        leftover = _end_turn(agent, result)
    if leftover and isinstance(result, dict):
        result["pending_steer"] = "\n".join(filter(None, [result.get("pending_steer"), leftover]))
    return result


def _end_turn(agent, result):
    from agent.interrupt_control import _set_steer_parts, _steer_parts
    with _lock(agent):
        agent._codex_steer_live = False
        flusher = vars(agent).get("_codex_steer_flusher")
    if flusher is not None:
        flusher.join(_JOIN_TIMEOUT_S)
        if flusher.is_alive():
            logger.warning("codex steer flusher still sending after %.0fs; its steers settle later", _JOIN_TIMEOUT_S)
    if (not isinstance(result, dict) or (result.get("error") and not result.get("interrupted")
                                         and getattr(agent, "_has_pending_fallback", lambda: False)())):
        return None  # a fallback may rerun this turn on the generic loop, whose drains take the buffer
    with _lock(agent):
        parts = _steer_parts(agent)
        _set_steer_parts(agent, [part for part in parts if part[0] is not None])
    return "\n".join(part[1] for part in parts if part[0] is None) or None
