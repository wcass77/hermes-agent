"""Keep one process watcher's progress across transient profile routing refusals."""
import asyncio
from dataclasses import dataclass
from functools import wraps
import logging

from agent.retry_utils import jittered_backoff
from gateway.session_identity import IdentityUnresolved

logger = logging.getLogger('gateway.run')


@dataclass
class ProcessWatcherProgress:
    retry_interval: float
    output_length: int = 0
    completion_attempts: int = 0


def retry_process_watcher(method):
    """Retry this watcher's poll loop with the same cursor and completion budget."""
    @wraps(method)
    async def retry(runner, watcher):
        progress = ProcessWatcherProgress(retry_interval=watcher['check_interval'])
        attempt = 0
        while True:
            try:
                return await method(runner, watcher, progress)
            except IdentityUnresolved as exc:
                from gateway.run_notifications import _COMPLETION_INJECTION_RETRY_MAX_DELAY
                from tools.process_registry import process_registry
                # Profile recovery is irrelevant once the process no longer exists.
                if process_registry.get(watcher['session_id']) is None:
                    return
                attempt += 1
                logger.warning('Process watcher profile unresolved; retaining watcher for retry: %s', exc)
                await asyncio.sleep(jittered_backoff(
                    attempt, base_delay=max(watcher['check_interval'], 1) * 2,
                    max_delay=_COMPLETION_INJECTION_RETRY_MAX_DELAY, jitter_ratio=0.0))
    return retry
