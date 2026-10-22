"""Ordinary reasoning output policy, independent of diagnostic sanitization.

Each profile's choice is frozen on first use. Changing config takes effect in a
new process; it never rewrites an existing conversation's cached history.
"""

from functools import lru_cache
import logging

logger = logging.getLogger(__name__)


@lru_cache(maxsize=128)
def _profile_policy(home: str) -> str:
    from yaml import YAMLError
    from hermes_cli.config_effective import load_user_config_effective

    try:
        policy = (load_user_config_effective().get("security") or {}).get("ordinary_output", "redact")
        if policy in ("redact", "fidelity"):
            return policy
    except (OSError, ValueError, TypeError, AttributeError, YAMLError):
        pass
    logger.warning("Invalid ordinary-output policy; retaining redaction")
    return "redact"


def ordinary_output_policy() -> str:
    from hermes_constants import hermes_home_key

    return _profile_policy(hermes_home_key())


def protect_file_output(text: str, *, secret_file: bool = False) -> str:
    """Preserve ordinary fixtures in fidelity mode, never classified credential sources."""
    from agent.redact import redact_registered_vault_values, redact_sensitive_text

    fidelity = ordinary_output_policy() == "fidelity"
    if fidelity and not secret_file:
        return redact_registered_vault_values(text)
    return redact_sensitive_text(text, file_read=True, secret_file=secret_file, force=fidelity)


def protect_background_notification(text: str, command: str = "") -> str:
    """Model reinjection uses ordinary policy; direct human delivery uses egress protection."""
    from agent.redact import redact_for_egress, redact_terminal_output

    if ordinary_output_policy() == "fidelity":
        return redact_terminal_output(text, command)
    return redact_for_egress(text)
