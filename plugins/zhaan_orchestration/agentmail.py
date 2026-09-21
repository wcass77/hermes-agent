from __future__ import annotations

from pathlib import Path

from plugins.agentmail_common import AgentMailClient


class Client(AgentMailClient):
    def __init__(self, api_key_file: Path, base_url: str = "https://api.agentmail.to/v0"):
        self.api_key_file = api_key_file
        super().__init__(lambda: self.api_key, base_url=base_url)

    @property
    def api_key(self) -> str:
        return self.api_key_file.read_text().strip()
