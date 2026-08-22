from __future__ import annotations

import contextlib
import datetime as dt
import fcntl
import json
import os
from pathlib import Path
from typing import Any, Callable
from zoneinfo import ZoneInfo

import yaml

from .store import Store


POST_SHARED_UPDATE_SCHEMA = {
    "name": "post_shared_update",
    "description": (
        "Post a concise Shared Update to today's authoritative Family Assistant "
        "Discord thread and persist the same assistant message in that thread's "
        "Calendar-Day Session. This is the preferred and only supported way to "
        "share information from AgentMail or a private Discord DM with the family. "
        "Use it for an explicit request to tell everyone or when today's shared "
        "understanding changes. Never use cron for an immediate Shared Update."
    ),
    "parameters": {
        "type": "object",
        "additionalProperties": False,
        "properties": {
            "message": {
                "type": "string",
                "minLength": 1,
                "description": (
                    "Concise participant-facing update only. Do not quote or forward "
                    "private email or DM conversation."
                ),
            }
        },
        "required": ["message"],
    },
}


def _thread_name(day: dt.date) -> str:
    return f"{day:%A, %B} {day.day}, {day:%Y}"


class SharedUpdateService:
    def __init__(
        self,
        store: Store,
        workspace: Path,
        *,
        channel_id: str,
        timezone: str = "America/New_York",
        discord_request: Callable | None = None,
        create_thread: Callable | None = None,
        send_message: Callable | None = None,
        session_db_factory: Callable | None = None,
        participant_ids: Callable[[], set[str]] | None = None,
    ):
        self.store = store
        self.workspace = workspace
        self.channel_id = str(channel_id)
        self.timezone = ZoneInfo(timezone)
        self._discord_request_override = discord_request
        self._create_thread_override = create_thread
        self._send_message_override = send_message
        self._session_db_factory_override = session_db_factory
        self._participant_ids_override = participant_ids

    @contextlib.contextmanager
    def _lock(self):
        path = self.store.path.with_suffix(self.store.path.suffix + ".shared-update.lock")
        path.parent.mkdir(parents=True, exist_ok=True)
        with path.open("w") as handle:
            path.chmod(0o600)
            fcntl.flock(handle, fcntl.LOCK_EX)
            yield

    @staticmethod
    def _token() -> str:
        from gateway.config import Platform, load_gateway_config

        config = load_gateway_config()
        platform = config.platforms.get(Platform.DISCORD)
        token = str(getattr(platform, "token", "") or "").strip()
        if not token:
            raise RuntimeError("Zhaan Discord token is unavailable")
        return token

    def _discord_request(self, method: str, path: str, token: str, **kwargs):
        if self._discord_request_override:
            return self._discord_request_override(method, path, token, **kwargs)
        from tools.discord_tool import _discord_request

        return _discord_request(method, path, token, **kwargs)

    def _create_thread(self, token: str, name: str) -> dict[str, Any]:
        if self._create_thread_override:
            return self._create_thread_override(token, self.channel_id, name)
        from tools.discord_tool import _create_thread

        return json.loads(_create_thread(token, self.channel_id, name))

    def _send(self, target: str, message: str) -> dict[str, Any]:
        if self._send_message_override:
            return self._send_message_override(target, message)
        from tools.send_message_tool import send_message_tool

        return json.loads(send_message_tool({
            "action": "send", "target": target, "message": message,
        }))

    def _session_db(self):
        if self._session_db_factory_override:
            return self._session_db_factory_override()
        from hermes_state import SessionDB

        return SessionDB()

    def _participant_ids(self) -> set[str]:
        if self._participant_ids_override:
            return self._participant_ids_override()
        people = Path(os.environ.get(
            "ZHAAN_PEOPLE_REGISTRY",
            "/home/hermes/.hermes/profiles/zhaan/workspace/config/people.yaml",
        ))
        data = yaml.safe_load(people.read_text(encoding="utf-8")) or {}
        return {
            str(person.get("channels", {}).get("discord_user_id"))
            for person in data.get("people", [])
            if person.get("participant")
            and person.get("channels", {}).get("discord_user_id")
        }

    def _add_participants(self, token: str, thread_id: str) -> None:
        participant_ids = self._participant_ids()
        if not participant_ids:
            raise RuntimeError("Zhaan has no Discord Participants")
        for user_id in sorted(participant_ids):
            self._discord_request(
                "PUT", f"/channels/{thread_id}/thread-members/{user_id}", token,
            )

    def _valid_thread(self, token: str, thread_id: str, name: str) -> bool:
        try:
            thread = self._discord_request("GET", f"/channels/{thread_id}", token)
        except Exception:
            return False
        return (
            str(thread.get("parent_id") or "") == self.channel_id
            and str(thread.get("name") or "") == name
        )

    def _find_thread(self, token: str, name: str) -> str | None:
        parent = self._discord_request("GET", f"/channels/{self.channel_id}", token)
        guild_id = str(parent.get("guild_id") or "")
        if not guild_id:
            raise RuntimeError("Coordination channel has no Discord guild")
        active = self._discord_request("GET", f"/guilds/{guild_id}/threads/active", token)
        candidates = list(active.get("threads", []))
        archived = self._discord_request(
            "GET", f"/channels/{self.channel_id}/threads/archived/public", token,
            params={"limit": "100"},
        )
        candidates.extend(archived.get("threads", []))
        matches = [
            item for item in candidates
            if str(item.get("parent_id") or "") == self.channel_id
            and str(item.get("name") or "") == name
        ]
        if not matches:
            return None
        thread = min(matches, key=lambda item: int(str(item["id"])))
        thread_id = str(thread["id"])
        if (thread.get("thread_metadata") or {}).get("archived"):
            self._discord_request(
                "PATCH", f"/channels/{thread_id}", token,
                body={"archived": False, "locked": False},
            )
        return thread_id

    def _ensure_thread(self, token: str, day: dt.date) -> tuple[str, str]:
        plan_date = day.isoformat()
        name = _thread_name(day)
        saved = self.store.daily_thread(plan_date)
        if saved and self._valid_thread(token, saved["thread_id"], name):
            return saved["thread_id"], saved["session_id"]
        thread_id = self._find_thread(token, name)
        if not thread_id:
            created = self._create_thread(token, name)
            if not created.get("success") or not created.get("thread_id"):
                raise RuntimeError(created.get("error") or "Discord did not create the daily thread")
            thread_id = str(created["thread_id"])
        self._add_participants(token, thread_id)
        session_id = f"zhaan-daily-{plan_date}"
        self.store.save_daily_thread(plan_date, self.channel_id, thread_id, session_id)
        return thread_id, session_id

    def _ensure_session(self, session_id: str, thread_id: str, name: str):
        db = self._session_db()
        session_key = f"agent:main:discord:thread:{thread_id}:{thread_id}"
        origin = {
            "platform": "discord", "chat_id": thread_id,
            "chat_name": f"Family Assistant / #{name}", "chat_type": "thread",
            "thread_id": thread_id, "parent_chat_id": self.channel_id,
        }
        db.create_session(
            session_id, "discord", session_key=session_key, chat_id=thread_id,
            chat_type="thread", thread_id=thread_id, cwd=str(self.workspace),
        )
        try:
            db.reopen_session(session_id)
        except Exception:
            pass
        db.record_gateway_session_peer(
            session_id, source="discord", session_key=session_key,
            chat_id=thread_id, chat_type="thread", thread_id=thread_id,
            display_name=origin["chat_name"], origin_json=json.dumps(origin),
        )
        return db

    def post(self, message: str, *, now: dt.datetime | None = None) -> dict[str, Any]:
        message = str(message or "").strip()
        if not message:
            return {"success": False, "error": "message is required"}
        instant = now or dt.datetime.now(self.timezone)
        day = instant.astimezone(self.timezone).date()
        with self._lock():
            token = "test-token" if self._discord_request_override else self._token()
            thread_id, session_id = self._ensure_thread(token, day)
            name = _thread_name(day)
            db = self._ensure_session(session_id, thread_id, name)
            try:
                result = self._send(f"discord:{thread_id}:{thread_id}", message)
                if not result.get("success"):
                    return {
                        "success": False, "discord_sent": False,
                        "context_mirrored": False,
                        "error": result.get("error") or "Discord delivery failed",
                    }
                mirrored = bool(result.get("mirrored"))
                if not mirrored:
                    try:
                        db.append_message(session_id=session_id, role="assistant", content=message)
                        mirrored = True
                    except Exception as exc:
                        return {
                            "success": False, "discord_sent": True,
                            "context_mirrored": False,
                            "message_id": result.get("message_id"),
                            "error": f"Discord sent, but Calendar-Day Session persistence failed: {exc}",
                        }
                return {
                    "success": True, "discord_sent": True,
                    "context_mirrored": mirrored, "plan_date": day.isoformat(),
                    "thread_id": thread_id, "session_id": session_id,
                    "message_id": result.get("message_id"),
                }
            finally:
                close = getattr(db, "close", None)
                if callable(close):
                    close()


def configured_service() -> SharedUpdateService:
    from hermes_cli.config import load_config

    config = load_config() or {}
    plugins = config.get("plugins") or {}
    entry = (plugins.get("entries") or {}).get("zhaan_orchestration") or {}
    channel_id = str(
        entry.get("coordination_channel_id")
        or os.environ.get("ZHAAN_DISCORD_COORDINATION_CHANNEL_ID", "")
    ).strip()
    if not channel_id:
        raise RuntimeError("Zhaan coordination_channel_id is not configured")
    state = Path(os.environ.get(
        "ZHAAN_ORCHESTRATION_STATE",
        "/home/hermes/.hermes/profiles/zhaan/state/orchestration.sqlite",
    ))
    workspace = Path(os.environ.get(
        "ZHAAN_WORKSPACE", "/home/hermes/.hermes/profiles/zhaan/workspace",
    ))
    return SharedUpdateService(
        Store(state), workspace, channel_id=channel_id,
        timezone=str(config.get("timezone") or "America/New_York"),
    )


def post_shared_update_tool(args: dict[str, Any], **_kwargs) -> str:
    try:
        return json.dumps(configured_service().post(args.get("message", "")))
    except Exception as exc:
        return json.dumps({
            "success": False, "discord_sent": False,
            "context_mirrored": False, "error": str(exc),
        })
