from __future__ import annotations

import hashlib
import json
import os
import re
from pathlib import Path
from typing import Any

from hermes_state import SessionDB
import requests


RETURN_ARCHIVED_DOCUMENT_SCHEMA = {
    "name": "return_archived_document",
    "description": "Return an archived family document through AgentMail or the Family Assistant Discord main channel. Prefer this for document retrieval; provide only its catalog document ID.",
    "parameters": {
        "type": "object", "additionalProperties": False,
        "properties": {"document_id": {"type": "string", "pattern": "^doc-[a-z0-9-]+$"}},
        "required": ["document_id"],
    },
}


def resolve_document(workspace: Path, document_id: str) -> tuple[Path, str]:
    if not re.fullmatch(r"doc-[a-z0-9-]+", document_id or ""):
        raise ValueError("invalid document ID")
    catalog = (workspace / "documents" / "CATALOG.md").read_text(encoding="utf-8")
    match = re.search(rf"^## {re.escape(document_id)}(?: — .*?)?\n(?P<body>.*?)(?=^## |\Z)", catalog, re.M | re.S)
    if not match:
        raise ValueError(f"document not found: {document_id}")
    body = match.group("body")
    path_match = re.search(r"^- Archive path: `([^`]+)`$", body, re.M)
    hash_match = re.search(r"^- SHA-256: `([0-9a-f]{64})`$", body, re.M)
    if not path_match or not hash_match:
        raise ValueError("catalog entry lacks archive path or SHA-256")
    archive_root = (workspace / "documents" / "archive").resolve()
    path = (workspace / path_match.group(1)).resolve(strict=True)
    if archive_root not in path.parents:
        raise ValueError("catalog path is outside the document archive")
    digest = hashlib.sha256(path.read_bytes()).hexdigest()
    if digest != hash_match.group(1):
        raise ValueError("archived document failed integrity verification")
    return path, digest


def return_archived_document_tool(args: dict[str, Any], *, session_id: str = "", **_kwargs) -> str:
    try:
        workspace = Path(os.environ.get("ZHAAN_WORKSPACE", "/home/hermes/.hermes/profiles/zhaan/workspace"))
        document_id = str(args.get("document_id") or "")
        path, digest = resolve_document(workspace, document_id)
        manifest = os.environ.get("ZHAAN_AGENTMAIL_ATTACHMENT_MANIFEST")
        email_session = os.environ.get("ZHAAN_AGENTMAIL_SESSION_ID")
        if manifest and session_id == email_session:
            target = Path(manifest)
            target.parent.mkdir(parents=True, exist_ok=True)
            temporary = target.with_suffix(".tmp")
            temporary.write_text(json.dumps({"document_id": document_id, "path": str(path), "sha256": digest}), encoding="utf-8")
            temporary.replace(target)
            return json.dumps({"success": True, "channel": "agentmail", "attachment_prepared": True, "document_id": document_id})

        db = SessionDB()
        session = db.get_session(session_id)
        if not session or str(session.get("source") or "") != "discord":
            raise ValueError("document return is supported only in the current Discord or AgentMail conversation")
        from .shared_update import SharedUpdateService, configured_service
        destination = configured_service().channel_id
        try:
            token = SharedUpdateService._token()
        except RuntimeError:
            token_file = Path(os.environ.get(
                "ZHAAN_DISCORD_TOKEN_FILE",
                "/home/hermes/.hermes/profiles/zhaan/secrets/discord-bot-token",
            ))
            token = token_file.read_text(encoding="utf-8").strip()
            if not token:
                raise RuntimeError("Zhaan Discord token is unavailable")
        with path.open("rb") as attachment:
            response = requests.post(
                f"https://discord.com/api/v10/channels/{destination}/messages",
                headers={"Authorization": f"Bot {token}"},
                data={"payload_json": json.dumps({"content": f"Archived document {document_id}"})},
                files={"files[0]": (path.name, attachment)}, timeout=60,
            )
        if response.status_code not in {200, 201}:
            raise RuntimeError(f"Discord attachment delivery failed ({response.status_code})")
        delivered = response.json()
        return json.dumps({"success": True, "channel": "discord", "document_id": document_id, "message_id": delivered.get("id")})
    except Exception as exc:
        return json.dumps({"success": False, "error": str(exc)})
