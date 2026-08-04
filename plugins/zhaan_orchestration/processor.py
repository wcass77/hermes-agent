from __future__ import annotations

import json
import os
import re
import subprocess
import urllib.request
from pathlib import Path
from typing import Any

from hermes_state import SessionDB

from .agentmail import Client


def child_environment() -> dict[str, str]:
    """Return an agent-child environment without gateway process identity."""
    environment = os.environ.copy()
    environment.pop("_HERMES_GATEWAY", None)
    return environment


def safe_name(value: str) -> str:
    return re.sub(r"[^A-Za-z0-9._-]+", "_", value).strip("._") or "attachment"


class Processor:
    def __init__(self, client: Client, workspace: Path, hermes_command: str = "/home/hermes/.local/bin/hermes"):
        self.client = client
        self.workspace = workspace
        self.hermes_command = hermes_command

    def _download_attachments(self, message: dict[str, Any]) -> list[str]:
        destination = self.workspace / "inbox" / "agentmail" / safe_name(str(message["message_id"]))
        paths = []
        for attachment in message.get("attachments", []):
            destination.mkdir(parents=True, exist_ok=True)
            path = destination / safe_name(str(attachment.get("filename") or attachment["attachment_id"]))
            url = self.client.attachment_url(message["inbox_id"], message["message_id"], attachment["attachment_id"])
            with urllib.request.urlopen(url, timeout=60) as response, path.open("wb") as output:
                output.write(response.read())
            paths.append(str(path.relative_to(self.workspace)))
        return paths

    def __call__(self, item: dict[str, Any], session_id: str) -> None:
        message = self.client.get_message(item["inbox_id"], item["message_id"])
        attachments = self._download_attachments(message)
        db = SessionDB()
        if not db.resolve_session_id(session_id):
            db.create_session(session_id, "agentmail", cwd=str(self.workspace))
        prompt = (
            "A trusted Participant sent this email to Family Assistant. Process it according to AGENTS.md. "
            "Act when clear and reversible; otherwise ask one focused clarification. Archive every attachment "
            "with receipt provenance before using it. If today's shared understanding changes, use "
            "post_shared_update with only the concise participant-facing update. Do not use cron for this and "
            "do not claim the update was shared unless the tool reports both discord_sent and context_mirrored. "
            "Return only the participant-facing email reply.\n\n"
            + json.dumps({
                "inbox_id": message.get("inbox_id"), "thread_id": message.get("thread_id"),
                "message_id": message.get("message_id"), "from": message.get("from"),
                "subject": message.get("subject"), "text": message.get("text") or message.get("extracted_text"),
                "html": message.get("html") if not (message.get("text") or message.get("extracted_text")) else None,
                "attachment_paths": attachments,
            }, ensure_ascii=False)
        )
        result = subprocess.run(
            [self.hermes_command, "--profile", "zhaan", "chat", "--resume", session_id, "-q", prompt, "--quiet"],
            cwd=self.workspace, env=child_environment(), text=True, capture_output=True, timeout=1800,
        )
        if result.returncode != 0:
            raise RuntimeError(result.stderr[-2000:] or f"Hermes exited {result.returncode}")
        reply = result.stdout.strip()
        if not reply:
            raise RuntimeError("Hermes returned an empty email reply")
        self.client.reply(item["inbox_id"], item["message_id"], reply)

    def failure_reply(self, item: dict[str, Any]) -> None:
        self.client.reply(
            item["inbox_id"], item["message_id"],
            "I couldn't process this message after several attempts, so I did not make any changes. Please retry or handle it manually.",
        )
