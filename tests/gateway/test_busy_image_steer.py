"""``busy_input_mode: steer`` and a photo, end to end through the real session authority.

The photo is admitted like any follow-up (its bytes retained under ``native-inputs``), then transferred
into the running turn: the agent holds caption + retained image paths, the user is told it steered,
and no second turn runs. When the turn ends before reading it, the claim's settlement puts the
admission back in its FIFO place and the photo runs as its own turn. Anything the model cannot take
natively, or any non-image attachment, keeps the old queue behaviour.
"""
import asyncio
import threading

import pytest

from agent.i18n import t
from agent.interrupt_control import InterruptControlMixin
from gateway.config import GatewayConfig, Platform, PlatformConfig
from gateway.platforms.base import SendResult
from gateway.platforms.event import MessageEvent, MessageType
from gateway.run import GatewayRunner
from gateway.run_turn_runner import TurnRunner

_PNG = bytes.fromhex(
    "89504e470d0a1a0a0000000d49484452000000010000000108020000009077"
    "53de0000000c4944415408d763f8cfc0f00f00050201ff2ecf3ca10000000049454e44ae426082")


class Agent(InterruptControlMixin):
    """The real steer seam (buffer, drains, tickets); the turn below drains it like the loop does."""
    api_mode, provider, model = "chat_completions", "custom", "fake-vision"

    def __init__(self):
        self._pending_steer, self._pending_steer_lock = None, threading.Lock()
        self._pending_redirect, self._pending_redirect_lock = None, threading.Lock()
        self._interrupt_requested, self._interrupt_message = False, None
        self._active_children, self._active_children_lock = [], threading.Lock()

    def clear_interrupt(self, *args, **kwargs):
        return True

    def interrupt(self, message=None, *args, **kwargs):
        self._interrupt_message = message

    def get_activity_summary(self):
        return {}


async def _photo_while_running(monkeypatch, tmp_path, event, *, drain=True, image_mode="native", probe=None):
    """Run one turn; deliver ``event`` while it is in progress. Returns (what the running turn's drain
    took, later turns' messages, chat sends, the agent)."""
    from gateway.session_authority import initialize_session_authority
    from plugins.platforms.discord.adapter import DiscordAdapter
    from tests.waiting import async_wait_until

    monkeypatch.setenv("DISCORD_ALLOWED_USERS", "42")
    monkeypatch.setattr(GatewayRunner, "_effective_busy_input_mode", lambda self, source: "steer")
    decided = []

    def decide(self, **kwargs):
        decided.append((kwargs.get("provider"), kwargs.get("model")))
        return image_mode
    monkeypatch.setattr(GatewayRunner, "_decide_image_input_mode", decide)

    async def transcribed(self, event, adapter, source, text, *, log_context, metadata=None):
        return (f"{text}\n\n[voice] heard this").strip(), ["heard this"]
    monkeypatch.setattr(GatewayRunner, "_transcribe_and_echo_pending_voice", transcribed)

    runner = GatewayRunner(GatewayConfig())
    adapter = DiscordAdapter(PlatformConfig(enabled=True, token="fixture-token", typing_indicator=False))
    sent = []

    async def send(chat_id, content, reply_to=None, metadata=None):
        sent.append(content)
        return SendResult(success=True, message_id="s1")
    monkeypatch.setattr(adapter, "send", send)
    adapter.gateway_runner = runner
    runner.adapters = {Platform.DISCORD: adapter}
    await initialize_session_authority(runner, profile_id="default", instance_id="owner")
    runner._wire_adapter_handlers(adapter)
    source = adapter.build_source(chat_id="42", chat_type="dm", user_id="42")
    event.source = source
    agent, release, ready, later, taken = Agent(), threading.Event(), threading.Event(), [], []

    def run_sync(self):
        if not ready.is_set():
            self._runner._session_state(self._ctx.session_key).turn.agent = agent
            authority, owner_id, generation = self._approval_owner
            authority.adopt_agent(owner_id, generation, agent)
            ready.set()
            release.wait(10)
            if drain:
                taken.append(agent._drain_pending_steer_content())  # the loop's next drain
            result = {"final_response": "first", "messages": [], "completed": True}
        else:
            later.append(self._native_image_run_message())  # what this turn hands the agent
            result = {"final_response": "second", "messages": [], "completed": True}
        self._ctx.result_holder[0] = result
        return result
    monkeypatch.setattr(TurnRunner, "run_sync", run_sync)

    opening = asyncio.create_task(adapter.handle_message(MessageEvent(text="A", source=source, message_id="A")))
    key = runner.session_store._generate_session_key(source)
    await async_wait_until(ready.is_set, what="the running agent")
    await adapter.handle_message(event)
    await async_wait_until(lambda: agent._pending_steer is not None or any(
        t("gateway.busy.queued_head") in s for s in sent), what="the photo to resolve")
    if probe is not None:
        probe(runner, agent)
    release.set()
    await opening
    await async_wait_until(lambda: not runner._is_session_running(key) and not adapter._background_tasks
                           and all(r[1] == "terminal" and r[2] != "steering" for r in _admissions(runner)),
                           what="every turn to finish")
    return taken, later, sent, agent, decided, runner


def _image(tmp_path, name="shot.png"):
    path = tmp_path / name
    path.write_bytes(_PNG + name.encode())
    return str(path)


def _photo(tmp_path, *paths, text="this is the bug", types=None):
    paths = list(paths) or [_image(tmp_path)]
    return MessageEvent(text=text, message_type=MessageType.PHOTO, message_id="P",
                        media_urls=paths, media_types=types or ["image/png"] * len(paths))


def _admissions(runner):
    from gateway.session_authorities import active_authority
    db = (active_authority(runner) or runner.session_authority).db
    with db._read_ctx() as conn:
        return conn.execute("SELECT request_id, status, outcome FROM session_admissions ORDER BY seq").fetchall()


@pytest.mark.asyncio
async def test_a_busy_photo_steers_its_caption_and_retained_image_into_the_running_turn(monkeypatch, tmp_path):
    from gateway.session_ingress_media import native_inputs_root
    taken, later, sent, _agent, decided, runner = await _photo_while_running(monkeypatch, tmp_path, _photo(tmp_path))

    (text, images), = taken
    assert "this is the bug" in text and "Gateway message origin" in text
    assert len(images) == 1
    from pathlib import Path
    assert Path(images[0]).parent.parent == native_inputs_root(), "the retained, replayable bytes, not the cache"
    assert later == [], "a steered photo never runs again as its own turn"
    assert any(t("gateway.busy.steered_head") in s for s in sent), sent
    assert not any(t("gateway.busy.queued_head") in s for s in sent), sent
    assert decided == [("custom", "fake-vision")], "the receiving agent's own runtime decides native input"
    assert [tuple(r)[1:] for r in _admissions(runner)] == [("terminal", "completed"), ("terminal", "steered")]


@pytest.mark.asyncio
async def test_several_images_and_a_voice_note_steer_together(monkeypatch, tmp_path):
    voice = tmp_path / "note.ogg"
    voice.write_bytes(b"OggS" + b"\x00" * 32)
    event = _photo(tmp_path, _image(tmp_path, "a.png"), _image(tmp_path, "b.png"), str(voice), text="",
                   types=["image/png", "image/png", "audio/ogg"])
    taken, later, sent, *_ = await _photo_while_running(monkeypatch, tmp_path, event)
    (text, images), = taken
    assert len(images) == 2 and "heard this" in text
    assert later == []
    assert any(t("gateway.busy.steered_head") in s for s in sent), sent


@pytest.mark.asyncio
async def test_a_caption_less_photo_gets_a_neutral_caption(monkeypatch, tmp_path):
    taken, *_ = await _photo_while_running(monkeypatch, tmp_path, _photo(tmp_path, text=""))
    (text, images), = taken
    assert "[The user sent this image while you were working.]" in text and len(images) == 1


@pytest.mark.asyncio
async def test_a_photo_the_turn_never_read_runs_next_with_its_image(monkeypatch, tmp_path):
    """The steer landed after the turn's last drain: settlement returns the admission to its FIFO place and
    the photo runs once as its own turn, image included (nothing lost, nothing doubled)."""
    taken, later, _sent, agent, _, runner = await _photo_while_running(
        monkeypatch, tmp_path, _photo(tmp_path), drain=False)
    assert taken == [] and agent._pending_steer is None
    assert len(later) == 1, later
    message = later[0]
    assert isinstance(message, list) and [p["type"] for p in message] == ["text", "image_url"], message
    assert "this is the bug" in message[0]["text"]
    assert [tuple(r)[1:] for r in _admissions(runner)] == [("terminal", "completed"), ("terminal", "completed")]


@pytest.mark.asyncio
async def test_a_model_without_native_image_input_keeps_the_photo_queued(monkeypatch, tmp_path):
    """Text-mode image routing (non-vision model): the established path pre-analyzes the image on its own
    turn, so the photo queues exactly as before instead of steering a caption without its image."""
    taken, later, sent, *_ = await _photo_while_running(
        monkeypatch, tmp_path, _photo(tmp_path), image_mode="text")
    assert taken == [(None, [])]
    assert len(later) == 1
    assert any(t("gateway.busy.queued_head") in s for s in sent), sent


@pytest.mark.asyncio
async def test_a_document_beside_the_photo_keeps_the_whole_message_queued(monkeypatch, tmp_path):
    doc = tmp_path / "notes.pdf"
    doc.write_bytes(b"%PDF-1.4\n%%EOF\n")
    event = _photo(tmp_path, _image(tmp_path), str(doc), types=["image/png", "application/pdf"])
    taken, later, sent, *_ = await _photo_while_running(monkeypatch, tmp_path, event)
    assert taken == [(None, [])]
    assert len(later) == 1
    assert any(t("gateway.busy.queued_head") in s for s in sent), sent


@pytest.mark.asyncio
async def test_a_photo_in_transfer_keeps_its_retained_bytes_held(monkeypatch, tmp_path):
    """``steering`` is terminal but not settled: a release pass in that window (another turn's settlement)
    must not collect the image the running turn is about to read, or that it may get back."""
    from pathlib import Path
    from gateway.session_authorities import active_authority
    from gateway.session_ingress_media import release_unheld_media
    held = []

    def probe(runner, agent):
        path = agent._pending_steer_parts[0][2][0]
        db = (active_authority(runner) or runner.session_authority).db
        release_unheld_media(db, [{"path": path, "sha256": Path(path).parent.name}], retain_history=False)
        held.append(Path(path).is_file())
    await _photo_while_running(monkeypatch, tmp_path, _photo(tmp_path), probe=probe)
    assert held == [True]
