"""A steer that carries the user's photo (busy ``steer`` mode): the image reaches the running turn as
native ``image_url`` parts in the ordinary steer row, survives persistence and replay byte-identically,
and is never lost when no drain of the turn reads it."""
import json
import threading
from pathlib import Path

import pytest

from agent.interrupt_control import InterruptControlMixin
from agent.prompt_builder import STEER_DISPLAY_KIND, STEER_MARKER_CLOSE, STEER_MARKER_OPEN
from agent.session_persistence import _durable_content
from gateway.session_ingress_media import capture_native_media

_PNG = bytes.fromhex(
    "89504e470d0a1a0a0000000d49484452000000010000000108020000009077"
    "53de0000000c4944415408d763f8cfc0f00f00050201ff2ecf3ca10000000049454e44ae426082")


class _Agent(InterruptControlMixin):
    """The real steer seam; the drains are the real helpers the turn loop calls."""
    api_mode = "chat_completions"

    def __init__(self):
        self._pending_steer, self._pending_steer_lock = None, threading.Lock()
        self._pending_redirect, self._pending_redirect_lock = None, threading.Lock()
        self._interrupt_requested = False

    def _apply_pending_steer_to_tool_results(self, messages, num_tool_msgs):
        from agent.agent_runtime_helpers import apply_pending_steer_to_tool_results
        apply_pending_steer_to_tool_results(self, messages, num_tool_msgs)


def _config(mode="native"):
    from hermes_constants import get_hermes_home  # sandboxed per test by conftest
    (get_hermes_home() / "config.yaml").write_text(json.dumps({"agent": {"image_input_mode": mode}}), encoding="utf-8")


def _retained(tmp_path, name="photo.png", data=_PNG):
    """An image exactly as admission leaves it: content-addressed under this profile's native-inputs."""
    staged = tmp_path / name
    staged.write_bytes(data)
    return capture_native_media([staged])[0]["path"]


def _tool_batch():
    return [
        {"role": "user", "content": "fix the layout"},
        {"role": "assistant", "content": None, "tool_calls": [{"id": "a", "type": "function",
                                                             "function": {"name": "terminal", "arguments": "{}"}}]},
        {"role": "tool", "content": "done", "tool_call_id": "a"},
    ]


def test_an_image_steer_must_be_ticketed():
    """A ticketless leftover goes back to its surface as text alone, so only a ticketed (admitted) steer
    may carry an image: settlement returns an unread one to the admission that still holds it."""
    agent = _Agent()
    assert agent.steer("look at this", images=["/tmp/x.png"]) is False
    assert agent._pending_steer is None
    assert agent.steer("look at this", ticket="adm-1", images=["/tmp/x.png"]) is True


def test_the_post_tool_drain_appends_one_steer_row_with_the_native_images(tmp_path):
    first, second = _retained(tmp_path, "a.png"), _retained(tmp_path, "b.png", _PNG + b"\x00")
    agent = _Agent()
    agent.steer("this is the bug", ticket="adm-1", images=[first, second])
    messages = _tool_batch()
    agent._apply_pending_steer_to_tool_results(messages, num_tool_msgs=1)

    row = messages[-1]
    assert messages[2]["content"] == "done", "the persisted tool row is never rewritten"
    assert row["role"] == "user" and row["display_kind"] == STEER_DISPLAY_KIND
    text, *images = row["content"]
    assert text["type"] == "text"
    assert text["text"].startswith(STEER_MARKER_OPEN) and text["text"].endswith(STEER_MARKER_CLOSE)
    assert "this is the bug" in text["text"]
    # The handle each image's tools take sits inside the trusted marker, one line per image.
    assert f"[Image attached at: {first}]" in text["text"] and f"[Image attached at: {second}]" in text["text"]
    assert [part["type"] for part in images] == ["image_url", "image_url"]
    assert all(part["image_url"]["url"].startswith("data:image/png;base64,") for part in images)
    assert agent._pending_steer is None
    assert agent.withdraw_steer("adm-1") is False, "a drain of the turn consumed it: settles steered"


def test_a_text_steer_row_is_unchanged():
    agent = _Agent()
    agent.steer("plain words")
    messages = _tool_batch()
    agent._apply_pending_steer_to_tool_results(messages, num_tool_msgs=1)
    assert isinstance(messages[-1]["content"], str) and "plain words" in messages[-1]["content"]


def test_the_pre_api_drain_carries_the_images_too(tmp_path):
    from agent.turn_iteration_prep import _inject_steer_after_newest_tool_result
    path = _retained(tmp_path)
    agent = _Agent()
    agent.steer("see this", ticket="adm-1", images=[path])
    messages = _tool_batch()
    text, images = agent._drain_pending_steer_content()
    _inject_steer_after_newest_tool_result(agent, messages, text, images=images)
    assert messages[-1]["display_kind"] == STEER_DISPLAY_KIND
    assert [part["type"] for part in messages[-1]["content"]] == ["text", "image_url"]


def test_a_batch_with_no_tool_result_requeues_the_images_with_their_ticket(tmp_path):
    """Every tool of the batch was skipped (no tool row to follow): the drained steer goes back intact,
    images and ticket included, for the next drain or the claim's settlement."""
    path = _retained(tmp_path)
    agent = _Agent()
    agent.steer("see this", ticket="adm-1", images=[path])
    messages = [{"role": "assistant", "content": "skipped"}]
    agent._apply_pending_steer_to_tool_results(messages, num_tool_msgs=1)
    assert messages == [{"role": "assistant", "content": "skipped"}]
    assert agent.withdraw_steer("adm-1") is True, "requeued unread, ticket intact"


def test_an_unread_image_steer_is_never_a_text_leftover_and_withdraws_for_requeue(tmp_path):
    """The finalizer hands only ticketless text back; the ticketed image stays buffered, and the claim's
    settlement withdraws it (True), so its admission goes back to its queue place with the image."""
    path = _retained(tmp_path)
    agent = _Agent()
    agent.steer("typed text")
    agent.steer("see this", ticket="adm-1", images=[path])
    assert agent._drain_leftover_steer() == "typed text"
    assert agent.withdraw_steer("adm-1") is True
    assert agent._pending_steer is None


def test_a_hard_stop_drops_the_image_steer_unread_so_it_requeues(tmp_path):
    agent = _Agent()
    agent.steer("see this", ticket="adm-1", images=[_retained(tmp_path)])
    agent._execution_thread_id = None
    agent.clear_interrupt(hard_cancel=True)
    assert agent.withdraw_steer("adm-1") is True


def test_requeue_after_an_empty_tool_batch_keeps_the_images(tmp_path):
    from agent.agent_runtime_helpers import _requeue_pending_steer
    path = _retained(tmp_path)
    agent = _Agent()
    agent.steer("see this", ticket="adm-1", images=[path])
    text, _ = agent._drain_pending_steer_content()
    _requeue_pending_steer(agent, text)
    assert agent._drain_pending_steer_content() == ("see this", [path])
    assert agent.withdraw_steer("adm-1") is False


def test_the_persisted_row_replays_byte_identical_while_the_bytes_are_retained(tmp_path):
    """Cache contract: the steer row is a new appended row; a later turn rebuilt from the DB (restart,
    resume, API server) sends exactly the bytes the live request sent, so the cached prefix holds."""
    from agent.image_replay import replay_recent_images
    _config()
    path = _retained(tmp_path)
    agent = _Agent()
    agent.steer("this is the bug", ticket="adm-1", images=[path])
    messages = _tool_batch()
    agent._apply_pending_steer_to_tool_results(messages, num_tool_msgs=1)
    live = messages[-1]

    durable = _durable_content(live["content"])
    assert isinstance(durable, str) and durable.endswith("\n[screenshot]")
    rebuilt = [{"role": "user", "content": durable}]
    assert replay_recent_images(rebuilt, 3) == 1
    assert rebuilt[0]["content"] == live["content"]

    Path(path).unlink()  # swept: the rebuilt row keeps its text placeholder instead of a broken part
    rebuilt = [{"role": "user", "content": durable}]
    assert replay_recent_images(rebuilt, 3) == 0 and rebuilt[0]["content"] == durable


def test_a_non_vision_model_mid_turn_gets_the_existing_text_fallback(tmp_path, monkeypatch):
    """A fallback switch to a text-only model after the steer was accepted: the wire copy goes through the
    same non-vision preparation a normal image turn does (no image part reaches that provider)."""
    from agent.steer_images import steer_row
    from agent.vision_message_prep import VisionMessagePrepMixin

    class _TextOnly(VisionMessagePrepMixin):
        provider, model = "custom", "text-only"
        _anthropic_image_fallback_cache = {}

        def _model_supports_vision(self):
            return False

    monkeypatch.setattr(_TextOnly, "_describe_image_for_anthropic_fallback",
                        lambda self, url, role: "[The user attached an image. Here's what it contains:\nbug]")
    row = steer_row("see this", [_retained(tmp_path)])
    sent = _TextOnly()._prepare_messages_for_non_vision_model([row])
    assert not any(isinstance(p, dict) and p.get("type") == "image_url" for p in sent[0]["content"])
    assert row["content"][1]["type"] == "image_url", "the durable row itself is never rewritten"


class _CodexSession:
    def __init__(self, accept=True):
        self.calls, self.accept = [], accept

    def request_steer(self, text, *, images=()):
        self.calls.append((text, tuple(images)))
        return self.accept


@pytest.mark.parametrize("ticket", [None, "adm-1"])
def test_codex_app_server_steer_goes_through_native_turn_steer(ticket):
    """Codex owns its turn loop: nothing drains the buffer there, so a steer used to be accepted and
    then never delivered. It now rides ``turn/steer`` at once."""
    agent = _Agent()
    agent.api_mode, agent._codex_session = "codex_app_server", _CodexSession()
    assert agent.steer("use postgres", ticket=ticket) is True
    assert agent._codex_session.calls == [("use postgres", ())]
    assert agent._pending_steer is None
    if ticket:
        assert agent.withdraw_steer(ticket) is False, "the active Codex turn took it: settles steered"


def test_codex_app_server_image_steer_names_each_image_and_sends_it_natively():
    agent = _Agent()
    agent.api_mode, agent._codex_session = "codex_app_server", _CodexSession()
    assert agent.steer("this one", ticket="adm-1", images=["/x/a.png"]) is True
    (text, images), = agent._codex_session.calls
    assert images == ("/x/a.png",)
    assert text.startswith("this one") and "[Image attached at: /x/a.png]" in text


def test_codex_app_server_refused_steer_is_not_buffered():
    """No active Codex turn: refused, so the surface queues it as its own turn (nothing silently held)."""
    agent = _Agent()
    agent.api_mode, agent._codex_session = "codex_app_server", _CodexSession(accept=False)
    assert agent.steer("later", ticket="adm-1", images=["/x/a.png"]) is False
    assert agent._pending_steer is None
    assert agent.withdraw_steer("adm-1") is False


def test_codex_turn_steer_input_maps_images_to_local_image_inputs():
    """``TurnSteerParams.input`` is ``UserInput[]`` (codex 0.169 schema): text plus ``localImage``."""
    from tests.agent.transports.test_codex_app_server_session import FakeClient, make_session
    client = FakeClient()
    session = make_session(client)
    session.ensure_started()
    with session._active_turn_lock:
        session._active_turn_id = "turn-live-1"
    assert session.request_steer("see this", images=["/x/a.png", "/x/b.jpg"]) is True
    method, params = client.requests[-1]
    assert method == "turn/steer"
    assert params["input"] == [{"type": "text", "text": "see this"},
                               {"type": "localImage", "path": "/x/a.png"},
                               {"type": "localImage", "path": "/x/b.jpg"}]
    assert params["expectedTurnId"] == "turn-live-1"
