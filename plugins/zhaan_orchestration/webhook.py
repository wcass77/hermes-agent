from __future__ import annotations

import json
from email.utils import parseaddr
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any

import yaml
from plugins.agentmail_common import verify_svix

from .store import Store


def allowed_senders(path: Path) -> set[str]:
    data = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    result: set[str] = set()
    for person in data.get("people", []):
        if not person.get("participant"):
            continue
        for address in person.get("channels", {}).get("agentmail_senders", []):
            result.add(address.casefold())
    return result


def sender_address(value: str) -> str:
    return parseaddr(value)[1].casefold()


class Handler(BaseHTTPRequestHandler):
    server_version = "ZhaanAgentMail/1"

    def _reply(self, status: int, body: dict[str, Any]) -> None:
        encoded = json.dumps(body, separators=(",", ":")).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(encoded)))
        self.end_headers()
        self.wfile.write(encoded)

    def do_POST(self):
        app = self.server.app
        if self.path != app.path:
            self._reply(404, {"error": "not found"})
            return
        try:
            length = int(self.headers.get("Content-Length", "0"))
        except ValueError:
            self._reply(400, {"error": "invalid content length"})
            return
        if length <= 0 or length > app.max_body:
            self._reply(413, {"error": "invalid body size"})
            return
        body = self.rfile.read(length)
        try:
            headers = {key.casefold(): value for key, value in self.headers.items()}
            webhook_id = verify_svix(body, headers, app.secret)
            payload = json.loads(body)
            if payload.get("event_type") != "message.received":
                raise ValueError("unexpected event type")
            message = payload["message"]
            sender = sender_address(str(message.get("from", "")))
            if not sender or sender not in allowed_senders(app.people_path):
                self._reply(202, {"status": "ignored"})
                return
            result = app.store.enqueue(webhook_id, payload, sender)
            self._reply(200, {"status": result})
        except (ValueError, KeyError, json.JSONDecodeError):
            self._reply(401, {"error": "invalid webhook"})

    def do_GET(self):
        self._reply(404, {"error": "not found"})

    def log_message(self, *_args):
        return


class Server(ThreadingHTTPServer):
    allow_reuse_address = True

    def __init__(self, host: str, port: int, *, path: str, secret: str, people_path: Path, store: Store, max_body: int = 1_048_576):
        if host not in {"127.0.0.1", "::1", "localhost"}:
            raise ValueError("AgentMail webhook must bind to loopback")
        self.app = type("App", (), dict(path=path, secret=secret, people_path=people_path, store=store, max_body=max_body))()
        super().__init__((host, port), Handler)
