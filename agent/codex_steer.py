"""Steer delivery for the Codex app-server runtime, which owns its own turn loop.

Nothing in that runtime drains ``AIAgent._pending_steer``, so a steer reaches the turn only through
native ``turn/steer``. A steer is buffered exactly like a native-loop steer (same parts, tickets and
fates) and a per-agent sender thread sends the buffer while a Codex turn is live: no caller ever waits
on the JSON-RPC (the gateway loop, the authority's stream lock, the turn's own thread).

- Before the turn exists (a steer latched by ``adopt_agent``, one sent while the turn starts), it
  waits in the buffer; ``CodexAppServerSession.on_turn_active`` marks the turn live and starts the
  sender. Insertion and activation share ``_pending_steer_lock``, so neither misses the other. The
  sender runs in the turn's own context (its profile scope), captured at activation.
- A definite refusal (the turn ended, codex rejected it) puts the steers back in front of the buffer
  and stops sending. A sent request without a reply (timeout, transport lost after the write) reads
  as delivered: it is never replayed.
- When the Codex run returns (``run_codex_app_server_turn`` here, what ``AIAgent`` forwards to), the
  sender is joined and the turn's epoch closes: a send still in flight then is uncertain and, like a
  transfer to a lost claim, never replayed; its late outcome cannot touch a later turn's buffer.
- Once the whole turn returned (``settle_codex_turn``, after any fallback to the generic loop has run),
  what is left is settled as the native finalizer does: ticketless text goes back to the surface as
  ``pending_steer`` (the next turn); a ticketed steer stays buffered for its claim's settlement
  (``withdraw_steer``) to requeue.
"""
import contextvars
import functools
import logging

logger = logging.getLogger(__name__)

_JOIN_TIMEOUT_S = 30.0


def _lock(agent):
    from agent.interrupt_control import _ic_lock
    return _ic_lock(agent, "_pending_steer_lock")


def codex_steer(agent, text: str, ticket, images: tuple) -> bool:
    """``AIAgent.steer`` under ``api_mode == "codex_app_server"``: buffered, sent by the sender."""
    from agent.interrupt_control import _ic_lock, _set_steer_parts, _steer_parts
    with _ic_lock(agent, "_pending_redirect_lock"):
        if agent._interrupt_requested:
            return False
    part = (ticket, text, images) if images else (ticket, text)
    with _lock(agent):
        _set_steer_parts(agent, _steer_parts(agent) + [part])
        if vars(agent).get("_codex_steer_live"):
            _start_sender_locked(agent)
    agent._turn_user_intervened = True
    return True


def codex_turn_active(agent) -> None:
    """``CodexAppServerSession.on_turn_active`` (the turn's thread): ``turn/steer`` is accepted from now."""
    with _lock(agent):
        agent._codex_steer_live = True
        agent._codex_steer_context = contextvars.copy_context()
        _start_sender_locked(agent)


def _start_sender_locked(agent) -> None:
    from agent.interrupt_control import _steer_parts
    from agent.memory_provider import spawn_context_thread
    if vars(agent).get("_codex_steer_sender") is not None or not _steer_parts(agent):
        return
    turn_context = agent._codex_steer_context.copy()
    sender = spawn_context_thread(
        functools.partial(turn_context.run, _send_buffer, agent, vars(agent).get("_codex_steer_epoch", 0)),
        name="codex-steer-send")
    agent._codex_steer_sender = sender
    sender.start()


def _send_buffer(agent, epoch: int) -> None:
    from agent.interrupt_control import _set_steer_parts, _steer_fates, _steer_part_images, _steer_parts
    while True:
        with _lock(agent):
            parts = _steer_parts(agent)
            if vars(agent).get("_codex_steer_epoch", 0) != epoch:
                return  # its turn ended while it was sending: a later turn's buffer is not its own
            if not parts or not vars(agent).get("_codex_steer_live"):
                agent._codex_steer_sender = None
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
            if vars(agent).get("_codex_steer_epoch", 0) != epoch:
                # Its turn already ended and settled these as in flight (uncertain, never replayed).
                logger.warning("codex turn/steer refused after its turn ended; %d steer(s) not replayed", len(parts))
                return
            for part in parts:
                _steer_fates(agent).pop(part[0], None)
            _set_steer_parts(agent, parts + _steer_parts(agent))
            agent._codex_steer_live = False
            agent._codex_steer_sender = None
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
    """``agent.codex_runtime.run_codex_app_server_turn`` (``AIAgent._run_codex_app_server_turn``): on
    every exit the sender is joined and the turn's steer epoch closes."""
    from agent.codex_runtime import run_codex_app_server_turn as run
    try:
        return run(agent, *args, **kwargs)
    finally:
        _end_turn(agent)


def _end_turn(agent) -> None:
    with _lock(agent):
        agent._codex_steer_live = False
        sender = vars(agent).get("_codex_steer_sender")
    if sender is not None:
        sender.join(_JOIN_TIMEOUT_S)
    with _lock(agent):
        agent._codex_steer_epoch = vars(agent).get("_codex_steer_epoch", 0) + 1
        if sender is not None and sender.is_alive():
            logger.warning("codex turn/steer still in flight after %.0fs; treated as delivered", _JOIN_TIMEOUT_S)
            agent._codex_steer_sender = None
        agent._codex_steer_turn_ended = True


def settle_codex_turn(agent, result):
    """``AIAgent.run_conversation``'s result, once a Codex run took part in the turn: ticketless leftovers
    become ``pending_steer`` (the next turn), as the native finalizer's do. This runs after the
    turn's fallback decision, so a turn the generic loop finished has already had its drains."""
    from agent.interrupt_control import _set_steer_parts, _steer_parts
    with _lock(agent):
        if not vars(agent).pop("_codex_steer_turn_ended", False) or not isinstance(result, dict):
            return result
        parts = _steer_parts(agent)
        _set_steer_parts(agent, [part for part in parts if part[0] is not None])
    leftover = "\n".join(part[1] for part in parts if part[0] is None)
    if leftover:
        result["pending_steer"] = "\n".join(filter(None, [result.get("pending_steer"), leftover]))
    return result
