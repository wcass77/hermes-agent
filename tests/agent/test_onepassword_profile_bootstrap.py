"""Concurrent cold profiles resolve explicit bootstraps without process-wide credentials."""
from concurrent.futures import ThreadPoolExecutor
import os
from pathlib import Path
import sys
from types import SimpleNamespace

import pytest

import yaml

from agent.secret_scope import build_profile_secret_scope
from hermes_cli.env_loader import hydrate_profile_secret_sources, reset_secret_source_cache


def test_simultaneous_profile_sources_and_missing_bootstrap(tmp_path, monkeypatch):
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    launch = tmp_path / "launch"
    launch.mkdir()
    monkeypatch.setenv("HERMES_HOME", str(launch))
    monkeypatch.setenv("OP_SERVICE_ACCOUNT_TOKEN", "synthetic-launch")
    monkeypatch.setenv("AGENTMAIL_API_KEY", "synthetic-launch-mail")
    binary = tmp_path / "op"
    binary.write_text(f"#!{sys.executable}\nimport os\n"
                      "assert 'OP_CONNECT_TOKEN' not in os.environ\n"
                      "assert not any(k.startswith('OP_SESSION_') for k in os.environ)\n"
                      "print('resolved-' + os.environ['OP_SERVICE_ACCOUNT_TOKEN'])\n")
    binary.chmod(0o700)
    homes = [tmp_path / name for name in ("pilot", "scorpius", "zhaan")]
    for home in homes:
        home.mkdir()
        (home / "bootstrap").write_text("synthetic-" + home.name)
        (home / "config.yaml").write_text(yaml.safe_dump({"secrets": {"onepassword": {
            "enabled": True, "binary_path": str(binary), "cache_ttl_seconds": 0,
            "service_account_token_file": "bootstrap",
            "env": {"AGENTMAIL_API_KEY": "op://Dedicated/Test/credential"},
        }}}))
    (homes[-1] / "bootstrap").unlink()
    before = dict(os.environ)
    reset_secret_source_cache()
    try:
        with ThreadPoolExecutor(max_workers=3) as executor:
            resolved = list(executor.map(hydrate_profile_secret_sources, homes))
        assert resolved == [{"AGENTMAIL_API_KEY": "resolved-synthetic-pilot"},
                            {"AGENTMAIL_API_KEY": "resolved-synthetic-scorpius"}, {}]
        assert [build_profile_secret_scope(home).get("AGENTMAIL_API_KEY") for home in homes] == [
            "resolved-synthetic-pilot", "resolved-synthetic-scorpius", None]
        assert os.environ == before
        # A corrected owning file recovers without resetting a sibling's scope.
        (homes[-1] / "bootstrap").write_text("synthetic-zhaan-fixed-host")
        assert hydrate_profile_secret_sources(homes[-1]) == {
            "AGENTMAIL_API_KEY": "resolved-synthetic-zhaan-fixed-host"}
        assert build_profile_secret_scope(homes[0])["AGENTMAIL_API_KEY"] == "resolved-synthetic-pilot"
    finally:
        reset_secret_source_cache()


@pytest.mark.parametrize("apply", [False, True])
def test_explicit_sync_uses_the_same_file_identity(tmp_path, monkeypatch, capsys, apply):
    from hermes_cli import onepassword_secrets_cli as cli
    from gateway.run import _profile_runtime_scope
    binary = tmp_path / "op"
    binary.write_text(f"#!{sys.executable}\nimport os\n"
                      "assert os.environ['OP_SERVICE_ACCOUNT_TOKEN']=='synthetic-own'\n"
                      "print('synthetic-business-key')\n")
    binary.chmod(0o700)
    config = {"enabled": True, "service_account_token_file": "bootstrap", "binary_path": str(binary),
              "env": {"SYNC_BUSINESS_KEY": "op://Dedicated/Test/credential"}}
    monkeypatch.setattr(cli, "_op_cfg", lambda: config)
    monkeypatch.setenv("OP_SERVICE_ACCOUNT_TOKEN", "synthetic-other")
    monkeypatch.delenv("SYNC_BUSINESS_KEY", raising=False)
    with _profile_runtime_scope(tmp_path, hydrate_secrets=False):
        assert cli.cmd_sync(SimpleNamespace(apply=apply)) == 1
        assert "SYNC_BUSINESS_KEY" not in os.environ
        (tmp_path / "bootstrap").write_text("synthetic-own")
        assert cli.cmd_sync(SimpleNamespace(apply=apply)) == 0
        assert os.environ.get("SYNC_BUSINESS_KEY") == ("synthetic-business-key" if apply else None)
    if apply:
        os.environ.pop("SYNC_BUSINESS_KEY")
    assert "synthetic-business-key" not in capsys.readouterr().out
