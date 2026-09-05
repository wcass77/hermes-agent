from __future__ import annotations

import contextlib
import datetime as dt
import fcntl
import json
import os
from pathlib import Path
from typing import Any, Callable
from zoneinfo import ZoneInfo

from .store import Store


POST_SHARED_UPDATE_SCHEMA = {
    "name": "post_shared_update",
    "description": (
        "Post a concise Shared Update to the Family Assistant Discord main "
        "channel and persist the same assistant message in the channel's shared "
        "weekly session. This is the preferred and only supported way to share "
        "information from AgentMail with the family. Use it for an explicit "
        "request to tell everyone or when today's shared understanding changes. "
        "Never use cron for an immediate Shared Update."
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
                    "private email conversation."
                ),
            }
        },
        "required": ["message"],
    },
}


def weekly_session_boundary(
    instant: dt.datetime, timezone: ZoneInfo, *, hour: int = 17, minute: int = 30
) -> tuple[dt.date, dt.datetime]:
    """Return the Sunday boundary governing *instant*.

    Sunday before 5:30 PM still belongs to the previous session. The Evening
    Preview at 5:30 PM is therefore the first publication in the new session.
    """
    local = instant.astimezone(timezone)
    days_since_sunday = (local.weekday() + 1) % 7
    sunday = local.date() - dt.timedelta(days=days_since_sunday)
    boundary = dt.datetime.combine(
        sunday, dt.time(hour=hour, minute=minute), tzinfo=timezone
    )
    if local < boundary:
        sunday -= dt.timedelta(days=7)
        boundary = dt.datetime.combine(
            sunday, dt.time(hour=hour, minute=minute), tzinfo=timezone
        )
    return sunday, boundary


class SharedUpdateService:
    def __init__(
        self,
        store: Store,
        workspace: Path,
        *,
        channel_id: str,
        timezone: str = "America/New_York",
        send_message: Callable | None = None,
        session_db_factory: Callable | None = None,
    ):
        self.store = store
        self.workspace = workspace
        self.channel_id = str(channel_id)
        self.timezone = ZoneInfo(timezone)
        self._send_message_override = send_message
        self._session_db_factory_override = session_db_factory

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

    def _send(self, message: str) -> dict[str, Any]:
        target = f"discord:{self.channel_id}"
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

    def _ensure_weekly_session(
        self, db, instant: dt.datetime
    ) -> tuple[str, dt.date]:
        week_start, boundary = weekly_session_boundary(instant, self.timezone)
        session_key = f"agent:main:discord:group:{self.channel_id}"
        current = db.find_latest_gateway_session_for_peer(
            source="discord",
            session_key=session_key,
            chat_id=self.channel_id,
            chat_type="group",
            thread_id=None,
        )
        current_started = None
        if current:
            try:
                current_started = dt.datetime.fromtimestamp(
                    float(current.get("started_at")), tz=dt.timezone.utc
                )
            except (TypeError, ValueError, OSError):
                current_started = None

        boundary_utc = boundary.astimezone(dt.timezone.utc)
        if current and current_started and current_started >= boundary_utc:
            session_id = str(current["id"])
        else:
            if current:
                promote = getattr(db, "promote_to_session_reset", None)
                if callable(promote):
                    promote(str(current["id"]), "weekly_reset")
                else:
                    db.end_session(str(current["id"]), "weekly_reset")
            session_id = f"zhaan-week-{week_start.isoformat()}"
            db.create_session(
                session_id,
                "discord",
                session_key=session_key,
                chat_id=self.channel_id,
                chat_type="group",
                cwd=str(self.workspace),
            )
            try:
                db.reopen_session(session_id)
            except Exception:
                pass

        origin = {
            "platform": "discord",
            "chat_id": self.channel_id,
            "chat_name": "Family Assistant / #family-logistics",
            "chat_type": "group",
        }
        db.record_gateway_session_peer(
            session_id,
            source="discord",
            session_key=session_key,
            chat_id=self.channel_id,
            chat_type="group",
            display_name=origin["chat_name"],
            origin_json=json.dumps(origin),
        )
        return session_id, week_start

    def post(self, message: str, *, now: dt.datetime | None = None) -> dict[str, Any]:
        message = str(message or "").strip()
        if not message:
            return {"success": False, "error": "message is required"}
        instant = now or dt.datetime.now(self.timezone)
        with self._lock():
            db = self._session_db()
            try:
                session_id, week_start = self._ensure_weekly_session(db, instant)
                result = self._send(message)
                if not result.get("success"):
                    return {
                        "success": False,
                        "discord_sent": False,
                        "context_mirrored": False,
                        "error": result.get("error") or "Discord delivery failed",
                    }
                mirrored = bool(result.get("mirrored"))
                if not mirrored:
                    try:
                        db.append_message(
                            session_id=session_id, role="assistant", content=message
                        )
                        mirrored = True
                    except Exception as exc:
                        return {
                            "success": False,
                            "discord_sent": True,
                            "context_mirrored": False,
                            "message_id": result.get("message_id"),
                            "error": (
                                "Discord sent, but weekly session persistence "
                                f"failed: {exc}"
                            ),
                        }
                return {
                    "success": True,
                    "discord_sent": True,
                    "context_mirrored": mirrored,
                    "week_start": week_start.isoformat(),
                    "channel_id": self.channel_id,
                    "session_id": session_id,
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
            "success": False,
            "discord_sent": False,
            "context_mirrored": False,
            "error": str(exc),
        })
