"""Private, value-free startup receipts for detached cron workers."""
import hashlib
import logging
import re
from datetime import UTC, datetime
from pathlib import Path

logger = logging.getLogger("cron.scheduler")


def collect_worker_credentials(home):
    """Keep the worker's existing hydrate/build order, retaining only safe source outcomes."""
    from agent.secret_scope import build_profile_secret_scope
    from hermes_cli.env_loader import hydrate_profile_secret_sources
    from hermes_cli.plugins import discover_plugins
    from hermes_cli.secret_source_diagnostics import hydration_observations

    discover_plugins()
    hydrate_profile_secret_sources(home)
    scope = build_profile_secret_scope(home)
    attempts = hydration_observations(home)
    receipt = {
        "schema_version": 1,
        "observed_at": datetime.now(UTC).isoformat(),
        "home": str(Path(home).resolve()),
        "attempts": attempts,
        "discord_supplied": bool(scope.get("DISCORD_BOT_TOKEN")),
    }
    return scope, receipt


def persist_worker_credentials(home, execution_id, receipt):
    """Diagnostics cannot change job admission; atomic private files survive stderr cleanup."""
    from hermes_constants import mkdir_under_hermes_home
    from utils import atomic_json_write

    name = hashlib.sha256(str(execution_id).encode()).hexdigest() + ".json"
    try:
        directory = mkdir_under_hermes_home(Path(home) / "cron" / "credential-diagnostics")
        atomic_json_write(directory / name, receipt, mode=0o600)
        receipts = sorted(
            (p for p in directory.iterdir() if re.fullmatch(r"[0-9a-f]{64}\.json", p.name)
             and p.is_file() and not p.is_symlink()), key=lambda p: p.stat().st_mtime_ns)
        for old in receipts[:-128]:
            old.unlink()
    except OSError:
        logger.warning("Cron credential diagnostic receipt unavailable")
