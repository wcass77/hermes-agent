"""Zhaan-only orchestration hooks. This plugin intentionally registers no tools."""

from __future__ import annotations

import logging
import os
import threading
from pathlib import Path

import yaml

from .store import Store
from .webhook import Server
from .worker import Worker

logger = logging.getLogger(__name__)
_server = None
_worker = None


def _participant_discord_ids(path: Path) -> set[str]:
    data = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    return {
        str(person.get("channels", {}).get("discord_user_id"))
        for person in data.get("people", [])
        if person.get("participant") and person.get("channels", {}).get("discord_user_id")
    }


def _configure_discord_participant_allowlist() -> None:
    """Bridge the canonical People Registry into Hermes's core auth gate."""
    people = Path(os.environ.get("ZHAAN_PEOPLE_REGISTRY", "/home/hermes/.hermes/profiles/zhaan/workspace/config/people.yaml"))
    participant_ids = _participant_discord_ids(people)
    if not participant_ids:
        raise RuntimeError(f"Zhaan has no Discord Participants in {people}")
    os.environ["DISCORD_ALLOWED_USERS"] = ",".join(sorted(participant_ids))


def pre_gateway_dispatch(event, **_kwargs):
    source = event.source
    if getattr(source.platform, "value", source.platform) != "discord":
        return None
    people = Path(os.environ.get("ZHAAN_PEOPLE_REGISTRY", "/home/hermes/.hermes/profiles/zhaan/workspace/config/people.yaml"))
    if str(source.user_id or "") not in _participant_discord_ids(people):
        return {"action": "skip", "reason": "unknown-discord-participant"}
    return {"action": "allow"}


def _start_server() -> None:
    global _server, _worker
    secret_file = Path(os.environ.get("ZHAAN_AGENTMAIL_WEBHOOK_SECRET_FILE", "/home/hermes/.hermes/profiles/zhaan/secrets/agentmail-webhook-signing-secret"))
    if not secret_file.is_file():
        logger.warning("Zhaan orchestration enabled without AgentMail webhook secret; listener disabled")
        return
    state = Path(os.environ.get("ZHAAN_ORCHESTRATION_STATE", "/home/hermes/.hermes/profiles/zhaan/state/orchestration.sqlite"))
    people = Path(os.environ.get("ZHAAN_PEOPLE_REGISTRY", "/home/hermes/.hermes/profiles/zhaan/workspace/config/people.yaml"))
    store = Store(state)
    _server = Server(
        "127.0.0.1", int(os.environ.get("ZHAAN_AGENTMAIL_WEBHOOK_PORT", "8787")),
        path=os.environ.get("ZHAAN_AGENTMAIL_WEBHOOK_PATH", "/webhooks/agentmail"),
        secret=secret_file.read_text().strip(), people_path=people, store=store,
    )
    threading.Thread(target=_server.serve_forever, name="zhaan-agentmail-webhook", daemon=True).start()
    api_key_file = Path(os.environ.get("ZHAAN_AGENTMAIL_API_KEY_FILE", "/home/hermes/.hermes/profiles/zhaan/secrets/agentmail-api-key"))
    if api_key_file.is_file():
        from .agentmail import Client
        from .processor import Processor
        workspace = Path(os.environ.get("ZHAAN_WORKSPACE", "/home/hermes/.hermes/profiles/zhaan/workspace"))
        processor = Processor(Client(api_key_file), workspace)
        _worker = Worker(store, processor, failure_notifier=processor.failure_reply)
        threading.Thread(target=_worker.run, name="zhaan-agentmail-worker", daemon=True).start()
    else:
        logger.warning("Zhaan AgentMail API key missing; verified messages will queue without processing")


def register(ctx) -> None:
    _configure_discord_participant_allowlist()
    ctx.register_hook("pre_gateway_dispatch", pre_gateway_dispatch)
    _start_server()
