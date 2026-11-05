"""Fork allowlists compose with upstream portable-plugin ownership filtering."""
from types import SimpleNamespace

import pytest

from agent import skill_commands, skill_utils
from hermes_cli import plugins, plugins_discovery


@pytest.mark.parametrize('allowed, expected', [
    (None, {'/foo-bar:one', '/foo-bar:two'}),
    ([], set()),
    (['one'], {'/foo-bar:one'}),
    (['foo-bar:two'], {'/foo-bar:two'}),
])
def test_plugin_skill_allowlist_and_owner_disable(monkeypatch, allowed, expected):
    entries = {f'/foo-bar:{name}': {'name': f'foo-bar:{name}'} for name in ('one', 'two')}
    manager = SimpleNamespace(
        _plugin_skills={info['name']: {'plugin_key': 'portable-key', 'plugin_name': 'foo.bar'}
                        for info in entries.values()},
        list_plugin_skill_metadata=lambda: list(entries.values()),
    )
    disabled_plugins = set()
    monkeypatch.setattr(plugins, 'discover_plugins', lambda: None)
    monkeypatch.setattr(plugins, 'get_plugin_manager', lambda: manager)
    monkeypatch.setattr(skill_commands, '_scan_plugin_skill_commands', lambda *_: entries)
    monkeypatch.setattr(skill_utils, 'get_allowed_skill_names', lambda: allowed)
    monkeypatch.setattr(skill_utils, 'get_disabled_skill_names', set)
    monkeypatch.setattr(plugins_discovery, '_get_disabled_plugins', lambda: disabled_plugins)
    skill_commands.invalidate_plugin_skill_commands()
    assert set(skill_commands.get_plugin_skill_commands()) == expected
    # The manifest name differs from the slash namespace; disabling the actual
    # owner still wins over an explicit skill allowlist on the next call.
    disabled_plugins.add('foo.bar')
    assert skill_commands.get_plugin_skill_commands() == {}
