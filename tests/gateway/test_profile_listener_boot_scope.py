"""Secondary boot and detached retry claims retain the adapter's own plugin listener."""
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import pytest

from gateway.config import GatewayConfig, Platform, PlatformConfig
from gateway.platform_registry import PlatformEntry, platform_registry
from gateway.run import GatewayRunner, _profile_runtime_scope
from hermes_constants import hermes_home_key


@pytest.mark.asyncio
async def test_neutral_host_refuses_owning_listener_collision_before_connect(tmp_path, monkeypatch):
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    root = tmp_path / ".hermes"
    root.mkdir()
    monkeypatch.setenv("HERMES_HOME", str(root))
    runner = object.__new__(GatewayRunner)
    runner.config = GatewayConfig(multiplex_profiles=True)
    runner.adapters, runner._profile_adapters = {}, {}
    runner._configure_profile_adapter = Mock()
    runner._sync_voice_mode_state_to_adapter = Mock()
    runner._update_platform_runtime_status = Mock()
    runner._connect_initial_adapter_with_timeout = AsyncMock(return_value=True)
    profiles, configs = {}, {}
    for number, name in enumerate(("pilot", "scorpius", "zhaan")):
        home = root / "profiles" / name
        home.mkdir(parents=True)
        (home / "config.yaml").write_text("{}\n")
        profiles[name] = home
        platform_registry.register(PlatformEntry(name="own_listener", label="Own listener",
            check_fn=lambda: True, adapter_factory=lambda config: SimpleNamespace(config=config),
            profile_listener_fn=lambda config: ("127.0.0.1", config.extra["port"])), scope=hermes_home_key(home))
        with _profile_runtime_scope(home, hydrate_secrets=False):
            platform = Platform("own_listener")
            configs[name] = GatewayConfig(platforms={platform: PlatformConfig(enabled=True,
                token="synthetic-" + name, extra={"port": 20100 + (number if name != "zhaan" else 0)})})

    async def load(name, _home):
        return configs[name]
    runner._load_secondary_profile_config = load
    claimed = {}
    assert await runner._start_one_profile_adapters("pilot", profiles["pilot"], claimed) == 1
    assert await runner._start_one_profile_adapters("scorpius", profiles["scorpius"], claimed) == 1
    assert await runner._start_one_profile_adapters("zhaan", profiles["zhaan"], claimed) == 0
    assert runner._connect_initial_adapter_with_timeout.await_count == 2
    assert runner._update_platform_runtime_status.call_args.kwargs["error_code"] == "duplicate_listener"
    configs["zhaan"].platforms[platform].extra["port"] = 20102
    assert await runner._start_one_profile_adapters("zhaan", profiles["zhaan"], claimed) == 1
    for number, name in enumerate(profiles):
        adapter = runner._profile_adapters[name][platform]
        queued = runner._reconnect_queue_entry(platform, adapter, adapter.config, attempts=0, delay=1)
        assert queued["listener_claim"] == ("listener", "127.0.0.1", 20100 + number)


@pytest.mark.parametrize("first_photon", [False, True])
@pytest.mark.asyncio
async def test_plugin_and_photon_share_one_endpoint_claim(tmp_path, monkeypatch, first_photon):
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    root = tmp_path / ".hermes"
    root.mkdir()
    monkeypatch.setenv("HERMES_HOME", str(root))
    runner = object.__new__(GatewayRunner)
    runner.config = GatewayConfig(multiplex_profiles=True)
    runner.adapters, runner._profile_adapters = {}, {}
    runner._configure_profile_adapter = Mock()
    runner._sync_voice_mode_state_to_adapter = Mock()
    runner._update_platform_runtime_status = Mock()
    runner._connect_initial_adapter_with_timeout = AsyncMock(return_value=True)
    homes, configs, resources = {}, {}, []
    for name, photon in (("pilot", first_photon), ("scorpius", not first_photon)):
        home = root / "profiles" / name
        home.mkdir(parents=True)
        (home / "config.yaml").write_text("{}\n")
        homes[name] = home
        if photon:
            factory = lambda config: SimpleNamespace(config=config, _sidecar_bind="127.0.0.1",
                                                     _sidecar_port=config.extra["port"])
        else:
            factory = lambda config: SimpleNamespace(config=config)
        platform_registry.register(PlatformEntry(name="photon" if photon else "own_listener", label="Listener",
            check_fn=lambda: True, adapter_factory=factory,
            profile_listener_fn=None if photon else lambda config: ("127.0.0.1", config.extra["port"])),
            scope=hermes_home_key(home))
        with _profile_runtime_scope(home, hydrate_secrets=False):
            platform = Platform("photon" if photon else "own_listener")
            configs[name] = GatewayConfig(platforms={platform: PlatformConfig(enabled=True,
                token="synthetic-" + name, extra={"port": 20200})})
            adapter = runner._create_adapter(platform, configs[name].platforms[platform])
            resources.append(runner._adapter_listener_claim(platform, adapter))
    assert resources[0] == resources[1]
    async def load(name, _home):
        return configs[name]
    runner._load_secondary_profile_config = load
    claimed = {}
    assert await runner._start_one_profile_adapters("pilot", homes["pilot"], claimed) == 1
    assert await runner._start_one_profile_adapters("scorpius", homes["scorpius"], claimed) == 0
    assert runner._connect_initial_adapter_with_timeout.await_count == 1
    assert runner._update_platform_runtime_status.call_args.kwargs["error_code"] == "duplicate_listener"
    next(iter(configs["scorpius"].platforms.values())).extra["port"] = 20201
    assert await runner._start_one_profile_adapters("scorpius", homes["scorpius"], claimed) == 1
