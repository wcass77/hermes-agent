from __future__ import annotations

import logging
import threading
import time
from collections.abc import Callable
from typing import Any

from .store import Store

logger = logging.getLogger(__name__)


class Worker:
    """Durable single-event worker; processors own model/channel delivery."""

    def __init__(self, store: Store, processor: Callable[[dict[str, Any], str], None], *, poll_seconds: float = 1.0):
        self.store = store
        self.processor = processor
        self.poll_seconds = poll_seconds
        self.stop_event = threading.Event()

    def run_once(self) -> bool:
        item = self.store.claim_next(threading.current_thread().name)
        if item is None:
            return False
        session_id = self.store.session_for_email_thread(item["thread_id"])
        try:
            self.processor(item, session_id)
        except Exception as exc:
            status = self.store.fail(item["event_id"], str(exc))
            logger.exception("Zhaan AgentMail event %s moved to %s", item["event_id"], status)
        else:
            self.store.complete(item["event_id"])
        return True

    def run(self) -> None:
        self.store.recover_processing()
        while not self.stop_event.is_set():
            if not self.run_once():
                self.stop_event.wait(self.poll_seconds)

    def stop(self) -> None:
        self.stop_event.set()
