from __future__ import annotations

import base64
import datetime as dt
import hashlib
import hmac

import pytest

from plugins.agentmail_common import (
    AgentMailError,
    addresses,
    attachment_bucket,
    safe_attachment_name,
    unique_attachment_names,
    verify_svix,
)


def test_attachment_names_are_safe_stable_and_unique():
    attachments = [
        {"attachment_id": "one", "filename": "../image.png"},
        {"attachment_id": "two", "filename": "../image.png"},
        {"attachment_id": "three", "filename": "../image.png"},
    ]

    assert unique_attachment_names(attachments) == ["image.png", "image-2.png", "image-3.png"]
    assert safe_attachment_name(None) == "attachment"
    assert attachment_bucket("message", "one") != attachment_bucket("message", "two")


def test_addresses_are_normalized_and_reject_injection():
    assert addresses(["Willy <WILLY@example.com>", "willy@example.com"]) == ["willy@example.com"]
    with pytest.raises(AgentMailError):
        addresses("willy@example.com\nBcc: outsider@example.com")


def test_svix_verification_is_shared_across_integrations():
    secret_raw = b"shared secret"
    secret = "whsec_" + base64.b64encode(secret_raw).decode()
    now = dt.datetime.now(dt.timezone.utc)
    timestamp = str(int(now.timestamp()))
    body = b'{"event_type":"message.received"}'
    signed = b"event." + timestamp.encode() + b"." + body
    signature = base64.b64encode(hmac.new(secret_raw, signed, hashlib.sha256).digest()).decode()
    headers = {
        "svix-id": "event",
        "svix-timestamp": timestamp,
        "svix-signature": "v1," + signature,
    }

    assert verify_svix(body, headers, secret, now=now) == "event"
    with pytest.raises(ValueError):
        verify_svix(body + b"changed", headers, secret, now=now)
