"""Fidelity is profile-scoped and cannot disable credential-source or diagnostic guards."""

import json

import pytest

from agent.redact import redact_terminal_output, register_vault_redaction_value, clear_vault_redaction_values
from hermes_constants import set_hermes_home_override, reset_hermes_home_override


FIXTURE = "ghp_" + "SyntheticFixture123" * 3


@pytest.mark.parametrize("mode,ordinary_preserved", [(None, False), ("redact", False), ("fidelity", True), ("invalid", False)])
def test_profile_policy_and_mandatory_guards(tmp_path, mode, ordinary_preserved):
    home = tmp_path / "profile"
    home.mkdir()
    security = {"redact_secrets": True}
    if mode is not None:
        security["ordinary_output"] = mode
    (home / "config.yaml").write_text(json.dumps({"security": security}))
    token = set_hermes_home_override(home)
    try:
        assert (FIXTURE in redact_terminal_output(FIXTURE, "python report.py")) is ordinary_preserved
        assert FIXTURE not in redact_terminal_output(FIXTURE, "python report.py", force=True)
        for command in ("printenv", "cat .env", "cat ~/.bashrc"):
            assert "opaque-example-value" not in redact_terminal_output("SERVICE_TOKEN=opaque-example-value", command)
        register_vault_redaction_value("synthetic-vault-value")
        assert "synthetic-vault-value" not in redact_terminal_output("synthetic-vault-value", "python report.py")
        other = tmp_path / "other"
        other.mkdir()
        (other / "config.yaml").write_text('security: {redact_secrets: true, ordinary_output: redact}\n')
        other_token = set_hermes_home_override(other)
        try:
            assert FIXTURE not in redact_terminal_output(FIXTURE, "python report.py")
            assert redact_terminal_output("synthetic-vault-value", "python report.py") == "synthetic-vault-value"
        finally:
            reset_hermes_home_override(other_token)
        assert (FIXTURE in redact_terminal_output(FIXTURE, "python report.py")) is ordinary_preserved
    finally:
        clear_vault_redaction_values()
        reset_hermes_home_override(token)
