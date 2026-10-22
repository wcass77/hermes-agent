"""Migration and boot agree on explicit plugin-owned per-profile listeners."""
from pathlib import Path
from types import SimpleNamespace

from gateway.config import GatewayConfig, Platform, PlatformConfig
from gateway.platform_registry import PlatformEntry, platform_registry
from gateway.run import GatewayRunner, _profile_runtime_scope
from hermes_cli.gateway_migrate import MigrationPlan, ProfileGateway
from hermes_cli.gateway_migrate_listeners import check_profile_listeners
from hermes_constants import hermes_home_key


def test_profile_listener_claims_match_boot_and_refuse_collision(tmp_path, monkeypatch):
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    root = tmp_path / ".hermes"
    root.mkdir()
    monkeypatch.setenv("HERMES_HOME", str(root))
    profiles, configs, adapters = [], {}, []
    runner = object.__new__(GatewayRunner)
    runner.config = GatewayConfig(multiplex_profiles=True)
    for number, name in enumerate(("pilot", "scorpius", "zhaan")):
        home = root / "profiles" / name
        home.mkdir(parents=True)
        (home / "config.yaml").write_text("{}\n")
        profiles.append(ProfileGateway(name=name, home=home))
        entry = PlatformEntry(name="profile_listener", label="Profile listener", adapter_factory=lambda c: SimpleNamespace(config=c),
            check_fn=lambda: True, profile_listener_fn=lambda config: ("127.0.0.1", config.extra["port"]))
        platform_registry.register(entry, scope=hermes_home_key(home))
        settings = PlatformConfig(enabled=True, extra={"port": 20000 + number})
        with _profile_runtime_scope(home, hydrate_secrets=False):
            platform = Platform("profile_listener")
            configs[name] = GatewayConfig(platforms={platform: settings})
            adapters.append(runner._create_adapter(platform, settings))
    claims = [runner._adapter_listener_claim(platform, adapter) for adapter in adapters]
    assert len(set(claims)) == 3
    plan = MigrationPlan(default_home=root, profiles=profiles, multiplex_flag_on=False, live_served=None)
    check_profile_listeners(plan, configs)
    assert plan.blockers == []
    configs["zhaan"].platforms[platform].extra["port"] = 20000
    check_profile_listeners(plan, configs)
    assert len(plan.blockers) == 1
    assert "pilot" in plan.blockers[0] and "zhaan" in plan.blockers[0]
