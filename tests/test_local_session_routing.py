"""Fork-owned session-routing seam: delegation on the fork SessionDB, loud refusal elsewhere."""

from pathlib import Path

import pytest

import local_session_routing as routing
from hermes_state import SessionDB


def test_seam_loads_from_this_checkout():
    assert Path(routing.__file__).resolve().parent == Path(__file__).resolve().parents[1]


class Recorder:
    def __init__(self):
        self.calls = []

    def find_latest_gateway_session_for_peer(self, **kw):
        self.calls.append(("find", kw))
        return {"id": "s1", "started_at": 1.0}

    def record_gateway_session_peer(self, session_id, **kw):
        self.calls.append(("stamp", session_id, kw))

    def get_compression_tip(self, session_id):
        return None

    def find_session_by_origin(self, **kw):
        self.calls.append(("origin", kw))
        return "s2"

    def promote_to_session_reset(self, session_id, reason):
        self.calls.append(("reset", session_id, reason))
        return True


def test_delegates_with_full_keyword_shape():
    db = Recorder()
    assert routing.find_peer_session(db, source="discord", session_key="k", chat_id="c", chat_type="group")["id"] == "s1"
    routing.stamp_peer(db, "s1", source="discord", session_key="k", chat_id="c", chat_type="group",
                       include_compression_ancestors=True, transport_profile="zhaan")
    assert routing.compression_tip(db, "s0") == "s0"  # no continuation -> input id
    assert routing.find_session_by_origin(db, platform="discord", chat_id="c", user_id="u") == "s2"
    assert routing.reset_session(db, "s1", "weekly_reset") is True
    assert db.calls[0] == ("find", {"source": "discord", "user_id": None, "session_key": "k",
                                    "chat_id": "c", "chat_type": "group", "thread_id": None})
    assert db.calls[1][2]["include_compression_ancestors"] is True
    assert db.calls[1][2]["transport_profile"] == "zhaan"
    assert db.calls[-1] == ("reset", "s1", "weekly_reset")


class CarriedOnly:
    """Shape of upstream v0.23.3's compatibility SessionDB: carried methods only."""

    def __getattr__(self, name):
        raise AttributeError(name)


@pytest.mark.parametrize("call", [
    lambda db: routing.find_peer_session(db, source="discord", session_key="k"),
    lambda db: routing.stamp_peer(db, "s", source="discord", session_key="k"),
    lambda db: routing.compression_tip(db, "s"),
    lambda db: routing.find_session_by_origin(db, platform="discord", chat_id="c"),
    lambda db: routing.reset_session(db, "s", "weekly_reset"),
])
def test_unported_store_refuses_loudly(call):
    with pytest.raises(routing.SessionRoutingNotPorted, match="port local_session_routing.py"):
        call(CarriedOnly())


def test_real_fork_sessiondb_round_trip(tmp_path):
    db = SessionDB(tmp_path / "state.db")
    try:
        db.create_session("zhaan-week-2026-11-16", "discord", session_key="agent:zhaan:discord:group:123",
                          chat_id="123", chat_type="group")
        routing.stamp_peer(db, "zhaan-week-2026-11-16", source="discord",
                           session_key="agent:zhaan:discord:group:123", chat_id="123", chat_type="group",
                           include_compression_ancestors=True, transport_profile="zhaan")
        found = routing.find_peer_session(db, source="discord", session_key="agent:zhaan:discord:group:123",
                                          chat_id="123", chat_type="group")
        assert found and found["id"] == "zhaan-week-2026-11-16"
        assert routing.compression_tip(db, "zhaan-week-2026-11-16") == "zhaan-week-2026-11-16"
        assert routing.reset_session(db, "zhaan-week-2026-11-16", "weekly_reset")
        assert db.get_session("zhaan-week-2026-11-16")["ended_at"] is not None
    finally:
        db.close()
