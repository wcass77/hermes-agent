"""``busy_input_mode: steer`` for a photo: its images reach the running turn, not the next one.

A text steer lands in the running agent before admission (``_hm_busy_preempt``). A photo cannot: a
caption-only steer would drop the image, and the image bytes only become durable (content-addressed
under ``native-inputs``, held, replayable by ``agent.image_replay``) when the message is admitted. So
the busy resolution defers it (``defer_image_steer``), the message is admitted to the FIFO like any
queued follow-up, and ``steer_admitted_images`` then TRANSFERS the committed row to the running claim
through the same fence ``prompt.steer_queued`` uses (``session_queued_controls``): queued -> terminal
``steering`` bound to the claim, handed to the agent ticketed by its admission id. The claim's
settlement decides it like any transfer: ``steered`` once a drain consumed it, or back to its own FIFO
place, image intact, when the turn ended before reading it.

Only images (and voice notes transcribed into the steer text) steer; any other attachment keeps the
whole message queued. The images steer only when the receiving agent's model takes them natively
(``_decide_image_input_mode``, the same decision a normal image turn makes); in text mode the queued
turn pre-analyzes them as before. The steer reaches the parent agent only, never its subagents.
"""
import asyncio
import logging
from dataclasses import replace
from functools import partial

from hermes_state_runtime import RuntimeStoreError, get_session_admission, revert_steer_input, steer_session_input

logger = logging.getLogger(__name__)

_NO_CAPTION = {1: '[The user sent this image while you were working.]',
               2: '[The user sent these images while you were working.]'}


def _image_steer_media(event):
    """True when every attachment is an image or a voice note and at least one is an image."""
    from gateway.run import _event_media_is_image, _event_media_is_stt_input
    media = getattr(event, 'media_urls', None) or []
    kinds = [_event_media_is_image(event, i) for i in range(len(media))]
    return any(kinds) and all(image or _event_media_is_stt_input(event, i) for i, image in enumerate(kinds))


def defer_image_steer(runner, event, running_agent) -> bool:
    """Steer mode, busy, a photo the session authority is about to admit: mark it so its busy ack waits
    for the transfer (``_busy_interrupt_and_ack``) and the caller admits it. False leaves the old path."""
    from gateway.session_authorities import active_authority
    from gateway.session_ingress import executing_admission
    if (active_authority(runner) is None or executing_admission.get() or getattr(event, '_fifo_only', False)
            or getattr(event, 'internal', False) or not getattr(event, 'allow_gateway_control', True)
            or not callable(getattr(running_agent, 'withdraw_steer', None)) or not _image_steer_media(event)):
        return False
    event._busy_image_steer = True
    return True


async def steer_admitted_images(authority, receipt, event) -> None:
    """After *event*'s admission committed: transfer it to the running claim, then send the busy ack
    its preempt deferred, naming what actually happened (steered, or queued)."""
    pending = event.__dict__.pop('_busy_image_steer', None)
    if not isinstance(pending, tuple):
        return
    session_key, adapter, busy_state, running_agent, outcome = pending
    delivered = await _transfer(authority, receipt, event, session_key)
    if delivered is None:
        return  # an uncertain write: the claim's settlement decides the row; claim neither outcome
    runner = authority.runner
    event._busy_steer_parent_only = True
    outcome = replace(outcome, effective_mode='steer' if delivered else 'queue', steered=delivered)
    await runner._busy_interrupt_and_ack(event, session_key, adapter, busy_state, running_agent, outcome)


def _native_images(runner, restored, session_key, agent):
    """The admitted row's retained image paths when the agent's model takes them natively and every one
    reads as an image the provider accepts; None keeps the message queued."""
    from agent.image_routing import build_native_content_parts
    from gateway.run import _event_media_is_image
    with runner._profile_scope_for_source(restored.source):
        mode = runner._decide_image_input_mode(
            source=restored.source, session_key=session_key,
            provider=getattr(agent, 'provider', None), model=getattr(agent, 'model', None))
    if mode != 'native':
        return None
    images = [path for i, path in enumerate(restored.media_urls) if _event_media_is_image(restored, i)]
    if not images or build_native_content_parts('', images)[1]:
        return None
    return images


async def _steer_text(runner, event, images):
    """Caption plus voice transcripts (the busy STT choke point), with the event's origin; None when a
    voice note did not transcribe, so the whole message stays queued and its own turn handles the clip
    instead of the steer silently carrying the images without it."""
    caption = (event.text or '').strip()
    audio = runner._pending_event_audio_paths(event)
    if audio:
        caption, transcripts = await runner._transcribe_and_echo_pending_voice(
            event, runner._delivery_adapter_for(event.source), event.source, caption, log_context='Busy-image-steer')
        if len(transcripts) < len(audio):
            return None
    return runner._steer_text_with_origin(caption.strip() or _NO_CAPTION[min(len(images), 2)], event)


async def _transfer(authority, receipt, event, session_key):
    """True once the running claim's agent holds the images; False when the row stays (or is again)
    queued; None after an uncertain transfer write. Mirrors ``session_queued_controls.steer_queued``."""
    from gateway.session_envelope import restore_native
    from gateway.session_queued_controls import _receiving_agent, _register, _unregister
    from gateway.session_runtime_workers import tracked_write
    runner = authority.runner
    if receipt.status != 'queued' or runner._draining:
        return False
    sid, ticket, ref = receipt.ref.session_id, receipt.admission_id, receipt.ref
    live = authority.sessions.get(sid)
    if live is None:
        return False
    with live.event_stream.lock:
        generation = authority.executing.get(sid)
        agent = _receiving_agent(authority, sid, generation)
    if agent is None or not callable(getattr(agent, 'withdraw_steer', None)):
        return False
    try:
        row = await asyncio.to_thread(get_session_admission, authority.db, admission_id=ticket)
        if row is None or row['status'] != 'queued' or 'native_text_v1' not in row['payload']:
            return False
        images = await asyncio.to_thread(
            lambda: _native_images(runner, restore_native(row['payload'], runner), session_key, agent))
    except Exception:
        logger.debug('Busy image steer preparation failed; the message stays queued', exc_info=True)
        return False
    if not images:
        return False
    text = await _steer_text(runner, event, images)
    if text is None:
        return False
    with live.event_stream.lock:
        if runner._draining or _receiving_agent(authority, sid, generation) is not agent:
            return False
        _register(authority, sid, generation, ticket)
    try:
        await tracked_write(authority, partial(
            steer_session_input, authority.db, epoch=authority.epoch, admission_id=ticket, generation=generation))
    except RuntimeStoreError:
        with live.event_stream.lock:
            _unregister(authority, sid, generation, ticket)
        return False
    except Exception:
        # A commit may precede the error: the fence stays and the claim's settlement decides the row.
        logger.warning('Busy image steer transfer write uncertain for %s', ticket, exc_info=True)
        return None
    with live.event_stream.lock:
        entry = authority.steer_transfers.get(sid)
        delivered = False
        if (entry is not None and entry[0] == generation and ticket in entry[1]
                and _receiving_agent(authority, sid, generation) is agent):
            if not entry[1][ticket]:
                try:
                    entry[1][ticket] = bool(agent.steer(text, ticket=ticket, images=images))
                except Exception:
                    logger.warning('Busy image steer refused by the running agent', exc_info=True)
            delivered = entry[1][ticket]
    if delivered:
        runner._fold_into_running_turn(agent, session_key, event)
        logger.info('Busy image steer: %d image(s) handed to the running turn of %s', len(images), session_key)
    else:
        # The claim ended (or its agent refused) first: back to its own FIFO place, image intact.
        try:
            await tracked_write(authority, partial(
                revert_steer_input, authority.db, epoch=authority.epoch, admission_id=ticket, generation=generation))
        except Exception:
            logger.warning('Busy image steer revert failed for %s; settlement decides it', ticket, exc_info=True)
            return None
        with live.event_stream.lock:
            _unregister(authority, sid, generation, ticket)
        authority._schedule(ref)
    authority._publish_pending(ref)
    return delivered
