from __future__ import annotations

import json
import urllib.parse
import urllib.request
from pathlib import Path
from typing import Any


class Client:
    def __init__(self, api_key_file: Path, base_url: str = "https://api.agentmail.to/v0"):
        self.api_key_file = api_key_file
        self.base_url = base_url.rstrip("/")

    @property
    def api_key(self) -> str:
        return self.api_key_file.read_text().strip()

    def request(self, method: str, path: str, body: dict[str, Any] | None = None) -> dict[str, Any]:
        data = json.dumps(body).encode() if body is not None else None
        request = urllib.request.Request(
            self.base_url + path,
            data=data,
            method=method,
            headers={"Authorization": f"Bearer {self.api_key}", "Content-Type": "application/json"},
        )
        with urllib.request.urlopen(request, timeout=30) as response:
            return json.loads(response.read())

    def get_message(self, inbox_id: str, message_id: str) -> dict[str, Any]:
        inbox = urllib.parse.quote(inbox_id, safe="")
        message = urllib.parse.quote(message_id, safe="")
        return self.request("GET", f"/inboxes/{inbox}/messages/{message}")

    def reply(self, inbox_id: str, message_id: str, text: str) -> dict[str, Any]:
        inbox = urllib.parse.quote(inbox_id, safe="")
        message = urllib.parse.quote(message_id, safe="")
        return self.request("POST", f"/inboxes/{inbox}/messages/{message}/reply", {"text": text})

    def attachment_url(self, inbox_id: str, message_id: str, attachment_id: str) -> str:
        parts = [urllib.parse.quote(value, safe="") for value in (inbox_id, message_id, attachment_id)]
        result = self.request("GET", f"/inboxes/{parts[0]}/messages/{parts[1]}/attachments/{parts[2]}")
        return str(result["download_url"])
