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
    """``request_steer`` as the agent's flusher thread calls it; ``gate`` holds a reply open."""

    def __init__(self, accept=True, gate=None):
        self.calls, self.accept, self.gate, self.sent = [], accept, gate, threading.Event()

    def request_steer(self, text, *, images=()):
        self.calls.append((text, tuple(images)))
        self.sent.set()
        if self.gate is not None:
            self.gate.wait(10)
        return self.accept


def _codex_agent(**session):
    agent = _Agent()
    agent.api_mode, agent._codex_session = "codex_app_server", _CodexSession(**session)
    agent._has_pending_fallback = lambda: False
    return agent


def _settled(agent):
    """Wait for the agent's flusher thread to finish sending (the turn's end joins it the same way)."""
    flusher = vars(agent).get("_codex_steer_sender")
    if flusher is not None:
        flusher.join(10)


@pytest.mark.parametrize("ticket", [None, "adm-1"])
def test_codex_app_server_steer_goes_through_native_turn_steer(ticket):
    """Codex owns its turn loop: nothing drains the buffer there, so a steer used to be accepted and
    then never delivered. It now rides ``turn/steer`` while the turn is live."""
    from agent.codex_steer import codex_turn_active
    agent = _codex_agent()
    codex_turn_active(agent)
    assert agent.steer("use postgres", ticket=ticket) is True
    _settled(agent)
    assert agent._codex_session.calls == [("use postgres", ())]
    assert agent._pending_steer is None
    if ticket:
        assert agent.withdraw_steer(ticket) is False, "the active Codex turn took it: settles steered"


def test_codex_app_server_image_steer_names_each_image_and_sends_it_natively():
    from agent.codex_steer import codex_turn_active
    agent = _codex_agent()
    codex_turn_active(agent)
    assert agent.steer("this one", ticket="adm-1", images=["/x/a.png"]) is True
    _settled(agent)
    (text, images), = agent._codex_session.calls
    assert images == ("/x/a.png",)
    assert text.startswith("this one") and "[Image attached at: /x/a.png]" in text


def test_a_codex_steer_never_waits_on_the_json_rpc():
    """Round-2 review: the gateway hands a transfer to the agent on its event loop, under the session's
    stream lock. ``steer`` only buffers; the flusher thread waits on codex."""
    from agent.codex_steer import codex_turn_active
    gate = threading.Event()
    agent = _codex_agent(gate=gate)
    codex_turn_active(agent)
    assert agent.steer("photo", ticket="adm-1", images=["/x/a.png"]) is True  # returns while codex is silent
    assert agent._codex_session.sent.wait(10)
    gate.set()
    _settled(agent)
    assert agent.withdraw_steer("adm-1") is False


@pytest.mark.parametrize("order", ["steer-then-activate", "activate-then-steer"])
def test_codex_app_server_steer_before_or_after_turn_start_is_delivered_once(order):
    """Round-1/2 review: a steer latched before adoption, or sent while the turn starts, has no native turn
    yet. Insertion and activation share one lock, so whichever comes first the steer goes out once."""
    from agent.codex_steer import codex_turn_active
    agent = _codex_agent()
    if order == "activate-then-steer":
        codex_turn_active(agent)
    assert agent.steer("latched before adoption") is True
    assert agent.steer("photo", ticket="adm-1", images=["/x/a.png"]) is True
    if order == "steer-then-activate":
        assert agent._codex_session.calls == []
        codex_turn_active(agent)
    _settled(agent)
    sent = [(text, images) for text, images in agent._codex_session.calls]
    assert "".join(text for text, _ in sent).count("latched before adoption") == 1
    assert [img for _, imgs in sent for img in imgs] == ["/x/a.png"]
    assert agent._pending_steer is None and agent.withdraw_steer("adm-1") is False


def _turn(agent, monkeypatch, during, result=None, after_codex=None):
    """``AIAgent.run_conversation`` for one Codex turn: the forwarded Codex run (a stand-in runtime that
    runs *during* mid-turn), then *after_codex* (what the caller does next, e.g. a fallback's generic
    loop), then the turn-level settlement."""
    from agent import codex_runtime, codex_steer

    def run(agent, **kwargs):
        during()
        return dict(result or {"final_response": "done", "error": None, "interrupted": False})
    monkeypatch.setattr(codex_runtime, "run_codex_app_server_turn", run)
    turn_result = codex_steer.run_codex_app_server_turn(agent, user_message="hi")
    if after_codex is not None:
        turn_result = after_codex(turn_result)
    return codex_steer.settle_codex_turn(agent, turn_result)


def test_codex_steers_left_when_the_turn_ends_go_back_like_the_native_finalizers(monkeypatch):
    """Round-2 review: a steer that reached the agent after the native turn ended (during persistence)
    must not wait in the buffer for an unrelated later turn. Ticketless text becomes ``pending_steer``
    (the next turn); a ticketed photo stays for its claim's settlement to requeue."""
    from agent.codex_steer import codex_turn_active
    agent = _codex_agent(accept=False)  # the native turn already ended: codex refuses turn/steer

    def during():
        codex_turn_active(agent)
        agent.steer("typed after the answer")
        agent.steer("photo", ticket="adm-1", images=["/x/a.png"])
    result = _turn(agent, monkeypatch, during)
    assert result["pending_steer"] == "typed after the answer"
    assert agent.withdraw_steer("adm-1") is True, "unread: its admission goes back to its queue place"
    assert agent._pending_steer is None


def test_a_failed_codex_turn_that_does_not_fall_back_still_hands_its_steers_back(monkeypatch):
    """Round-3 review: a configured fallback that declines (a non rate-limit error) must not leave the
    steer buffered for a later turn. Settlement runs after the real fallback decision."""
    from agent.codex_steer import codex_turn_active
    agent = _codex_agent(accept=False)
    agent._has_pending_fallback = lambda: True

    def during():
        codex_turn_active(agent)
        agent.steer("typed mid-turn")
    result = _turn(agent, monkeypatch, during, {"final_response": "", "error": "invalid model", "interrupted": False})
    assert result["pending_steer"] == "typed mid-turn"
    assert agent._pending_steer is None


def test_a_codex_turn_that_falls_back_lets_the_generic_loop_take_its_steers(monkeypatch):
    from agent.codex_steer import codex_turn_active
    agent = _codex_agent(accept=False)
    taken = []

    def during():
        codex_turn_active(agent)
        agent.steer("typed mid-turn")

    def generic_loop(_codex_result):  # the fallback reran the turn; its drain took the steer
        agent.api_mode = "chat_completions"
        taken.append(agent._drain_pending_steer())
        return {"final_response": "from the fallback", "error": None, "interrupted": False}
    result = _turn(agent, monkeypatch, during, {"final_response": "", "error": "429", "interrupted": False},
                   after_codex=generic_loop)
    assert taken == ["typed mid-turn"]
    assert "pending_steer" not in result


def test_a_send_still_in_flight_when_the_turn_ends_is_never_replayed_into_a_later_turn(monkeypatch):
    """Round-3 review: a ``turn/steer`` write that outlives the turn's join is uncertain. Its claim settles
    it as delivered (never replayed); when the send later fails it must not re-enter a later turn."""
    from agent import codex_steer
    monkeypatch.setattr(codex_steer, "_JOIN_TIMEOUT_S", 0.05)
    gate = threading.Event()
    agent = _codex_agent(accept=False, gate=gate)

    def during():
        codex_steer.codex_turn_active(agent)
        agent.steer("photo", ticket="adm-1", images=["/x/a.png"])
        assert agent._codex_session.sent.wait(10)
    _turn(agent, monkeypatch, during)
    stalled = threading.enumerate()
    assert agent.withdraw_steer("adm-1") is False, "in flight at the turn's end: settled, not requeued"
    agent._codex_session.gate = None
    codex_steer.codex_turn_active(agent)  # the next turn
    agent.steer("next turn's own steer")
    _settled(agent)
    gate.set()  # the stalled write now fails definitely
    for thread in stalled:
        if thread.name == "codex-steer-send":
            thread.join(10)
    assert "photo" not in (agent._pending_steer or "")
    assert agent._codex_session.calls[-1] == ("next turn's own steer", ())


def test_the_codex_sender_runs_in_the_turns_profile_context():
    """Round-3 review: a bare thread loses the turn's ContextVars (profile home, terminal backend), so the
    image hint was built for the wrong environment. The sender runs in the activating turn's context."""
    import contextvars
    from agent.codex_steer import codex_turn_active
    marker = contextvars.ContextVar("steer_test_profile", default="launch profile")
    seen = []
    agent = _codex_agent()
    real = agent._codex_session.request_steer

    def request_steer(text, *, images=()):
        seen.append(marker.get())
        return real(text, images=images)
    agent._codex_session.request_steer = request_steer

    def turn_thread():
        marker.set("routed profile")
        codex_turn_active(agent)
    worker = threading.Thread(target=turn_thread)
    worker.start()
    worker.join()
    agent.steer("hello")  # from another thread (the gateway loop), in its own context
    _settled(agent)
    assert seen == ["routed profile"]


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


def test_codex_turn_steer_answered_late_is_delivered_not_replayed():
    """Round-1 review: the request was sent and codex may have taken it; a refusal here would requeue the
    photo and run it twice. A late answer reads as delivered."""
    _late_steer_reads(TimeoutError("codex app-server method 'turn/steer' timed out after 10s"), True)


@pytest.mark.parametrize("sent", [True, False])
def test_codex_turn_steer_transport_loss_is_a_refusal_only_before_the_write(sent):
    """Round-2 review: the transport dying after the request was written is as uncertain as a timeout
    (never replayed); a request that never reached the transport is a refusal (requeued)."""
    from agent.transports.codex_app_server import CodexAppServerTransportError
    exc = CodexAppServerTransportError(code=-32000, message="codex app-server transport lost")
    exc.request_sent = sent
    _late_steer_reads(exc, sent)


def _late_steer_reads(exc, expected):
    from tests.agent.transports.test_codex_app_server_session import FakeClient, make_session
    client = FakeClient()

    def fail(method, params):
        if method == "turn/steer":
            raise exc
        return {"thread": {"id": "thread-fake-001"}} if method == "thread/start" else {}
    session = make_session(client)
    session.ensure_started()
    client._request_handler = fail
    with session._active_turn_lock:
        session._active_turn_id = "turn-live-1"
    assert session.request_steer("see this", images=["/x/a.png"]) is expected


def test_the_client_marks_a_transport_loss_after_the_write_as_sent():
    """``CodexAppServerClient.request`` raises the same error type before and after the write; only the
    post-write one carries ``request_sent``."""
    from agent.transports.codex_app_server import CodexAppServerClient, CodexAppServerTransportError
    client = object.__new__(CodexAppServerClient)
    client._next_id, client._pending, client._pending_lock = 1, {}, threading.Lock()

    def write(obj):
        client._pending[obj["id"]].put_nowait({"error": {"code": -32000, "message": "lost"}, "transportLost": True})
    client._send = write
    with pytest.raises(CodexAppServerTransportError) as lost:
        client.request("turn/steer", {}, timeout=1)
    assert lost.value.request_sent is True
    client._send = lambda obj: (_ for _ in ()).throw(CodexAppServerTransportError(code=-32000, message="closed"))
    with pytest.raises(CodexAppServerTransportError) as unsent:
        client.request("turn/steer", {}, timeout=1)
    assert unsent.value.request_sent is False


def test_codex_runtime_delivers_a_pre_start_steer_on_the_turn_turn_start_opened(monkeypatch):
    """The runtime wiring: ``_ensure_codex_session`` installs the activation hook, so a steer the agent
    took before the native turn existed goes out as ``turn/steer`` on the turn ``turn/start`` opened."""
    from agent import codex_runtime
    from agent.transports import codex_app_server_session as sess_mod
    from tests.agent.transports.test_codex_app_server_session import FakeClient

    client = FakeClient()

    def answer(method, params):
        if method == "thread/start":
            return {"thread": {"id": "thread-fake-001"}}
        if method == "turn/start":
            return {"turn": {"id": "turn-fake-001"}}
        if method == "turn/steer":  # the turn completes only once the steer reached it
            client.queue_notification("turn/completed", threadId="t",
                                      turn={"id": "tu1", "status": "completed", "error": None})
            return {"turnId": params.get("expectedTurnId")}
        return {}
    client._request_handler = answer
    monkeypatch.setattr(sess_mod, "CodexAppServerClient", lambda **kw: client)
    agent = _Agent()
    agent.api_mode, agent._codex_session, agent.session_cwd = "codex_app_server", None, "/tmp"
    agent.tool_progress_callback, agent.ephemeral_system_prompt = None, None
    agent._cached_system_prompt = "SOUL"
    assert agent.steer("use postgres") is True  # e.g. latched before adoption: no session, no turn yet
    codex_runtime._ensure_codex_session(agent, [{"role": "user", "content": "migrate the db"}])
    agent._codex_session.run_turn("migrate the db", turn_timeout=5.0)
    steers = [params for method, params in client.requests if method == "turn/steer"]
    assert [p["input"] for p in steers] == [[{"type": "text", "text": "use postgres"}]]
    assert steers[0]["expectedTurnId"] == "turn-fake-001"
    assert agent._pending_steer is None
