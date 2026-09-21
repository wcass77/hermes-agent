"""Shared AgentMail transport, webhook, and attachment safety primitives."""
from __future__ import annotations

import base64
import datetime as dt
import hashlib
import hmac
import json
import os
import re
import urllib.error
import urllib.parse
import urllib.request
from collections.abc import Callable, Iterable, Mapping
from email.utils import getaddresses
from pathlib import Path
from typing import Any


API = "https://api.agentmail.to/v0"
MAX_ATTACHMENT = 10 * 1024 * 1024
MAX_PROVIDER_RESPONSE = 4 * 1024 * 1024


class AgentMailError(Exception):
    """A bounded error safe to expose without leaking credentials or content."""


def canonical(value: Any) -> str:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False)


def addresses(values: str | list[str]) -> list[str]:
    if isinstance(values, str):
        values = [values]
    if not isinstance(values, list) or not values or len(values) > 20:
        raise AgentMailError("Expected 1–20 email addresses.")
    result = []
    for value in values:
        if not isinstance(value, str) or "\n" in value or "\r" in value:
            raise AgentMailError("Invalid email address.")
        parsed = getaddresses([value])
        if len(parsed) != 1 or not re.fullmatch(r"[^\s<>@,;]+@[^\s<>@,;]+\.[^\s<>@,;]+", parsed[0][1]):
            raise AgentMailError("Invalid email address.")
        result.append(parsed[0][1].casefold())
    return list(dict.fromkeys(result))


def segment(value: str) -> str:
    if not isinstance(value, str) or not 0 < len(value) <= 1000 or any(ord(char) < 32 for char in value):
        raise AgentMailError("Invalid message/attachment ID.")
    return urllib.parse.quote(value, safe="")


class NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, *_args, **_kwargs):
        raise AgentMailError("Unexpected redirect refused.")


def safe_attachment_name(value: str | None, *, fallback: str = "attachment", limit: int = 150) -> str:
    leaf = str(value or fallback).replace("\\", "/").rsplit("/", 1)[-1]
    name = re.sub(r"[^A-Za-z0-9._-]+", "_", leaf).strip("._")
    return name[:limit] or fallback


def unique_attachment_names(attachments: Iterable[Mapping[str, Any]]) -> list[str]:
    """Return stable, path-safe names without allowing duplicate-name overwrites."""
    used: set[str] = set()
    result = []
    for attachment in attachments:
        name = safe_attachment_name(attachment.get("filename") or attachment.get("attachment_id"))
        stem, suffix = os.path.splitext(name)
        candidate = name
        number = 2
        while candidate in used:
            candidate = f"{stem}-{number}{suffix}"
            number += 1
        used.add(candidate)
        result.append(candidate)
    return result


def attachment_bucket(message_id: str, attachment_id: str) -> str:
    return hashlib.sha256((message_id + attachment_id).encode()).hexdigest()[:24]


def secret_bytes(value: str) -> bytes:
    value = value.strip()
    if value.startswith("whsec_"):
        try:
            return base64.b64decode(value[6:], validate=True)
        except ValueError as exc:
            raise ValueError("invalid Svix secret") from exc
    return value.encode()


def verify_svix(
    body: bytes,
    headers: Mapping[str, str],
    secret: str,
    *,
    now: dt.datetime | float | None = None,
    tolerance: int = 300,
) -> str:
    webhook_id = headers.get("svix-id", "")
    timestamp = headers.get("svix-timestamp", "")
    signatures = headers.get("svix-signature", "")
    if not webhook_id or len(webhook_id) > 200 or not timestamp or not signatures:
        raise ValueError("missing or invalid Svix headers")
    try:
        sent = dt.datetime.fromtimestamp(int(timestamp), dt.timezone.utc)
    except (ValueError, OverflowError) as exc:
        raise ValueError("invalid Svix timestamp") from exc
    if isinstance(now, (int, float)):
        current = dt.datetime.fromtimestamp(now, dt.timezone.utc)
    else:
        current = now or dt.datetime.now(dt.timezone.utc)
    if abs((current - sent).total_seconds()) > tolerance:
        raise ValueError("stale Svix timestamp")
    signed = webhook_id.encode() + b"." + timestamp.encode() + b"." + body
    expected = base64.b64encode(hmac.new(secret_bytes(secret), signed, hashlib.sha256).digest()).decode()
    candidates = [part.split(",", 1)[1] for part in signatures.split() if part.startswith("v1,")]
    if not any(hmac.compare_digest(expected, candidate) for candidate in candidates):
        raise ValueError("invalid Svix signature")
    return webhook_id


def download_signed_attachment(url: str, *, timeout: int = 30, limit: int | None = MAX_ATTACHMENT) -> bytes:
    parsed = urllib.parse.urlsplit(url)
    if parsed.scheme != "https" or not parsed.hostname or parsed.username or parsed.password:
        raise AgentMailError("Invalid attachment download URL.")
    try:
        with urllib.request.build_opener(NoRedirect()).open(url, timeout=timeout) as response:
            content = response.read(limit + 1 if limit is not None else -1)
    except (OSError, ValueError):
        raise AgentMailError("Attachment download failed.") from None
    if limit is not None and len(content) > limit:
        raise AgentMailError("Attachment exceeds size limit.")
    return content


class AgentMailClient:
    def __init__(
        self,
        api_key: Callable[[], str],
        *,
        base_url: str = API,
        response_limit: int = MAX_PROVIDER_RESPONSE,
    ):
        self._api_key = api_key
        self.base_url = base_url.rstrip("/")
        self.response_limit = response_limit

    def request(self, method: str, path: str, body: dict[str, Any] | None = None, key: str | None = None) -> dict[str, Any]:
        headers = {"Authorization": "Bearer " + self._api_key(), "Content-Type": "application/json"}
        if key:
            headers["Idempotency-Key"] = key
        request = urllib.request.Request(
            self.base_url + path,
            data=canonical(body).encode() if body is not None else None,
            headers=headers,
            method=method,
        )
        try:
            with urllib.request.build_opener(NoRedirect()).open(request, timeout=30) as response:
                data = response.read(self.response_limit + 1)
                if len(data) > self.response_limit:
                    raise AgentMailError("Provider response exceeds read limit.")
                return json.loads(data)
        except urllib.error.HTTPError as exc:
            raise AgentMailError(f"AgentMail HTTP {exc.code}; no response body or credentials exposed.") from None
        except AgentMailError:
            raise
        except (OSError, ValueError):
            raise AgentMailError("AgentMail transport failed; delivery may be uncertain for a send.") from None

    def get_message(self, inbox_id: str, message_id: str) -> dict[str, Any]:
        return self.request("GET", f"/inboxes/{segment(inbox_id)}/messages/{segment(message_id)}")

    def reply(
        self,
        inbox_id: str,
        message_id: str,
        text: str,
        attachments: list[dict[str, str]] | None = None,
    ) -> dict[str, Any]:
        body: dict[str, Any] = {"text": text}
        if attachments:
            body["attachments"] = attachments
        return self.request("POST", f"/inboxes/{segment(inbox_id)}/messages/{segment(message_id)}/reply", body)

    def attachment_url(self, inbox_id: str, message_id: str, attachment_id: str) -> str:
        result = self.request(
            "GET",
            f"/inboxes/{segment(inbox_id)}/messages/{segment(message_id)}/attachments/{segment(attachment_id)}",
        )
        return str(result["download_url"])
