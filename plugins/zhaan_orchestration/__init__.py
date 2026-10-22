"""Zhaan-only orchestration hooks and the narrow Shared Update tool."""

from __future__ import annotations

import logging
import os
import sys
from pathlib import Path

import yaml
from agent.secret_scope import get_secret_str
from hermes_constants import get_hermes_home

from .store import Store
from .shared_update import POST_SHARED_UPDATE_SCHEMA, post_shared_update_tool
from .document_return import RETURN_ARCHIVED_DOCUMENT_SCHEMA, return_archived_document_tool
from .webhook import Server
from .worker import Worker

logger = logging.getLogger(__name__)
_server = None
_worker = None


def _is_gateway_process(argv: list[str] | None = None) -> bool:
    """Detect gateway startup before gateway.run sets its private marker."""
    if os.environ.get("_HERMES_GATEWAY") == "1":
        return True
    args = list(sys.argv if argv is None else argv)
    return any(args[index:index + 2] == ["gateway", "run"] for index in range(len(args) - 1))


def _participant_discord_ids(path: Path) -> set[str]:
    data = yaml.safe_load(path.read_text(encoding="utf-8-sig")) or {}
    return {
        str(person.get("channels", {}).get("discord_user_id"))
        for person in data.get("people", [])
        if person.get("participant") and person.get("channels", {}).get("discord_user_id")
    }


def _configure_discord_participant_allowlist(ctx) -> None:
    """Bind the canonical People Registry only to this profile's Discord adapter."""
    from .discord import register_discord
    people = Path(get_secret_str("ZHAAN_PEOPLE_REGISTRY", str(get_hermes_home() / "workspace/config/people.yaml")))
    participant_ids = _participant_discord_ids(people)
    if not participant_ids:
        raise RuntimeError(f"Zhaan has no Discord Participants in {people}")
    register_discord(ctx, participant_ids)


def _coordination_channel_id() -> str:
    configured = get_secret_str("ZHAAN_DISCORD_COORDINATION_CHANNEL_ID").strip()
    if configured:
        return configured
    from hermes_cli.config import load_config

    config = load_config() or {}
    plugins = config.get("plugins") or {}
    entry = (plugins.get("entries") or {}).get("zhaan_orchestration") or {}
    return str(entry.get("coordination_channel_id") or "").strip()


def _plugin_entry() -> dict:
    from hermes_cli.config import load_config

    config = load_config() or {}
    plugins = config.get("plugins") or {}
    return (plugins.get("entries") or {}).get("zhaan_orchestration") or {}


def pre_gateway_dispatch(event, **_kwargs):
    source = event.source
    if getattr(source.platform, "value", source.platform) != "discord":
        return None
    people = Path(get_secret_str("ZHAAN_PEOPLE_REGISTRY", str(get_hermes_home() / "workspace/config/people.yaml")))
    try:
        allowed = _participant_discord_ids(people)
    except (OSError, ValueError, TypeError, yaml.YAMLError):
        return {"action": "skip", "reason": "participant-registry-unavailable"}
    if str(source.user_id or "") not in allowed:
        return {"action": "skip", "reason": "unknown-discord-participant"}
    if (
        str(getattr(source, "chat_type", "") or "") != "group"
        or str(getattr(source, "chat_id", "") or "") != _coordination_channel_id()
    ):
        return {"action": "skip", "reason": "discord-main-channel-only"}
    return {"action": "allow"}


def _start_server() -> None:
    global _server, _worker
    secret_file = Path(get_secret_str("ZHAAN_AGENTMAIL_WEBHOOK_SECRET_FILE", str(get_hermes_home() / "secrets/agentmail-webhook-signing-secret")))
    if not secret_file.is_file():
        logger.warning("Zhaan orchestration enabled without AgentMail webhook secret; listener disabled")
        return
    state = Path(get_secret_str("ZHAAN_ORCHESTRATION_STATE", str(get_hermes_home() / "state/orchestration.sqlite")))
    people = Path(get_secret_str("ZHAAN_PEOPLE_REGISTRY", str(get_hermes_home() / "workspace/config/people.yaml")))
    store = Store(state)
    _server = Server(
        "127.0.0.1", int(get_secret_str("ZHAAN_AGENTMAIL_WEBHOOK_PORT", "8787")),
        path=get_secret_str("ZHAAN_AGENTMAIL_WEBHOOK_PATH", "/webhooks/agentmail"),
        secret=secret_file.read_text(encoding="utf-8-sig").strip(), people_path=people, store=store,
    )
    from agent.memory_provider import spawn_context_thread
    spawn_context_thread(target=_server.serve_forever, name="zhaan-agentmail-webhook", daemon=True).start()
    api_key_file = Path(get_secret_str("ZHAAN_AGENTMAIL_API_KEY_FILE", str(get_hermes_home() / "secrets/agentmail-api-key")))
    if api_key_file.is_file():
        from .agentmail import Client
        from .processor import Processor
        workspace = Path(get_secret_str("ZHAAN_WORKSPACE", str(get_hermes_home() / "workspace")))
        repository_url = str(
            _plugin_entry().get("email_rule_repository_url")
            or "https://github.com/wcass77/family-logistics"
        ).strip()
        processor = Processor(Client(api_key_file), workspace, rule_repository_url=repository_url)
        _worker = Worker(store, processor, failure_notifier=processor.failure_reply)
        spawn_context_thread(target=_worker.run, name="zhaan-agentmail-worker", daemon=True).start()
    else:
        logger.warning("Zhaan AgentMail API key missing; verified messages will queue without processing")


def register(ctx) -> None:
    _configure_discord_participant_allowlist(ctx)
    ctx.register_hook("pre_gateway_dispatch", pre_gateway_dispatch)
    ctx.register_tool(
        name="post_shared_update", toolset="zhaan",
        schema=POST_SHARED_UPDATE_SCHEMA, handler=post_shared_update_tool,
        description="Post and persist a concise update in the family Discord main channel.",
        emoji="📣",
    )
    ctx.register_tool(
        name="return_archived_document", toolset="zhaan",
        schema=RETURN_ARCHIVED_DOCUMENT_SCHEMA, handler=return_archived_document_tool,
        description="Return a cataloged family document through the current conversation.", emoji="📎",
    )
    if _is_gateway_process() and not _plugin_entry().get("native_agentmail"):
        _start_server()
