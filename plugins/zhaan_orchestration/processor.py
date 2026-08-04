from __future__ import annotations

import json
import base64
import hashlib
import mimetypes
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

    def _remove_archived_staging_attachments(self, attachments: list[str]) -> None:
        """Remove intake copies only after identical bytes exist in the archive."""
        archive = (self.workspace / "documents" / "archive" / "sha256").resolve()
        staging_root = (self.workspace / "inbox" / "agentmail").resolve()
        for relative in attachments:
            path = (self.workspace / relative).resolve(strict=True)
            if staging_root not in path.parents:
                raise RuntimeError("attachment staging path escaped the AgentMail inbox")
            digest = hashlib.sha256(path.read_bytes()).hexdigest()
            bucket = archive / digest[:2] / digest
            archived = any(
                candidate.is_file()
                and hashlib.sha256(candidate.read_bytes()).hexdigest() == digest
                for candidate in bucket.iterdir()
            ) if bucket.is_dir() else False
            if not archived:
                raise RuntimeError(f"attachment was not archived before processing completed: {relative}")
            path.unlink()
            parent = path.parent
            while parent != staging_root and staging_root in parent.parents:
                try:
                    parent.rmdir()
                except OSError:
                    break
                parent = parent.parent

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
        manifest = self.workspace.parent / "state" / "agentmail-attachments" / f"{safe_name(str(item['event_id']))}.json"
        manifest.unlink(missing_ok=True)
        environment = child_environment()
        environment["ZHAAN_AGENTMAIL_SESSION_ID"] = session_id
        environment["ZHAAN_AGENTMAIL_ATTACHMENT_MANIFEST"] = str(manifest)
        result = subprocess.run(
            [self.hermes_command, "--profile", "zhaan", "chat", "--resume", session_id, "-q", prompt, "--quiet"],
            cwd=self.workspace, env=environment, text=True, capture_output=True, timeout=1800,
        )
        if result.returncode != 0:
            raise RuntimeError(result.stderr[-2000:] or f"Hermes exited {result.returncode}")
        reply = result.stdout.strip()
        if not reply:
            raise RuntimeError("Hermes returned an empty email reply")
        outgoing = []
        if manifest.is_file():
            prepared = json.loads(manifest.read_text(encoding="utf-8"))
            path = Path(prepared["path"]).resolve(strict=True)
            archive = (self.workspace / "documents" / "archive").resolve()
            if archive not in path.parents or hashlib.sha256(path.read_bytes()).hexdigest() != prepared["sha256"]:
                raise RuntimeError("prepared email attachment failed safety verification")
            outgoing.append({
                "content": base64.b64encode(path.read_bytes()).decode(),
                "filename": path.name,
                "content_type": mimetypes.guess_type(path.name)[0] or "application/octet-stream",
            })
        try:
            self.client.reply(item["inbox_id"], item["message_id"], reply, attachments=outgoing)
            self._remove_archived_staging_attachments(attachments)
        finally:
            manifest.unlink(missing_ok=True)

    def failure_reply(self, item: dict[str, Any]) -> None:
        self.client.reply(
            item["inbox_id"], item["message_id"],
            "I couldn't process this message after several attempts, so I did not make any changes. Please retry or handle it manually.",
        )
