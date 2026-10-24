"""Profile configuration filters client-injected MCP tools before progressive discovery."""

import json
from types import SimpleNamespace
from unittest.mock import patch

import pytest
from acp.schema import McpServerStdio

from acp_adapter.server import HermesACPAgent
from acp_adapter.session import SessionManager
from hermes_cli.config import atomic_config_write


@pytest.fixture(autouse=True)
def _isolated_editor_registry():
    from tools.registry import registry

    yield
    for name in ["mcp__editor__read_note", "mcp__editor__write_note"]:
        registry.deregister(name)


@pytest.mark.asyncio
@pytest.mark.parametrize("rules, expected", [
    ({"include": ["read_*"]}, {"read_note"}),
    ({"include": []}, set()),
    ({"exclude": ["write_*"]}, {"read_note"}),
    ({"include": ["read_note"], "resources": False, "prompts": False}, {"read_note"}),
    ({}, {"read_note", "write_note"}),
])
async def test_client_filter_scopes_registry_search_and_calls(tmp_path, monkeypatch, rules, expected):
    from model_tools import _dispatch_bridge_tool, get_tool_definitions
    from tools.mcp_tool_registration import _register_from_cache_sync

    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    atomic_config_write(tmp_path / "config.yaml", {
        "acp": {"mcp_tool_filters": {"editor": rules}},
    })
    native = SimpleNamespace(enabled_toolsets=[], disabled_toolsets=[], _memory_manager=None)
    manager = SessionManager(agent_factory=lambda: native)
    state = manager.create_session(cwd=str(tmp_path))
    server = HermesACPAgent(session_manager=manager)
    entry = {"tools": [
        {"name": name, "description": name, "inputSchema": {"type": "object", "properties": {}}}
        for name in ["read_note", "write_note"]
    ]}

    def register(configs):
        assert configs["editor"]["command"] == "editor-mcp"
        return _register_from_cache_sync("editor", configs["editor"], entry)

    with patch("tools.mcp_tool_discovery.register_mcp_servers", side_effect=register):
        await server._register_session_mcp_servers(state, [
            McpServerStdio(name="editor", command="editor-mcp", args=[], env=[]),
        ])

    definitions = get_tool_definitions(
        enabled_toolsets=native.enabled_toolsets, quiet_mode=True, skip_tool_search_assembly=True,
    )
    expected_names = {f"mcp__editor__{name}" for name in expected}
    assert {t["function"]["name"] for t in definitions} == expected_names
    result, _target = _dispatch_bridge_tool(
        "tool_search", {"queries": ["editor"], "limit": 50}, native.enabled_toolsets, [],
    )
    assert set(json.loads(result)["tools"]) == expected_names
    if "write_note" not in expected:
        result, target = _dispatch_bridge_tool(
            "tool_call", {"name": "mcp__editor__write_note", "arguments": {}}, native.enabled_toolsets, [],
        )
        assert result is not None and target is None


@pytest.mark.asyncio
async def test_profile_filters_do_not_cross_homes_or_override_transport(tmp_path, monkeypatch):
    from tools.mcp_tool_registration import _make_tool_filter

    editor = McpServerStdio(name="editor", command="client-command", args=["client-arg"], env=[])
    captured = []
    for profile, names in [("first", ["read_note"]), ("second", ["write_note"])]:
        home = tmp_path / profile
        home.mkdir()
        monkeypatch.setenv("HERMES_HOME", str(home))
        atomic_config_write(home / "config.yaml", {
            "acp": {"mcp_tool_filters": {"editor": {"include": names}}},
            "mcp_servers": {"editor": {"command": "unrelated-host-command", "enabled": False}},
        })
        native = SimpleNamespace(enabled_toolsets=[], disabled_toolsets=[], _memory_manager=None)
        manager = SessionManager(agent_factory=lambda: native)
        state = manager.create_session(cwd=str(home))
        with patch("tools.mcp_tool_discovery.register_mcp_servers", side_effect=lambda c: captured.append(c)), \
             patch("model_tools.get_tool_definitions", return_value=[]):
            await HermesACPAgent(session_manager=manager)._register_session_mcp_servers(state, [editor])
        config = captured[-1]["editor"]
        assert config["command"] == "client-command" and config["args"] == ["client-arg"]
        allowed = _make_tool_filter("editor", config)
        assert {n for n in ["read_note", "write_note"] if allowed(n)} == set(names)


@pytest.mark.asyncio
@pytest.mark.parametrize("filters", [
    [], {"editor": None}, {"editor": {"include": 3}},
    {"editor": {"incldue": ["read_note"]}}, {"editor": {"resources": "false"}},
])
async def test_malformed_client_policy_does_not_register_servers(tmp_path, monkeypatch, filters):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    atomic_config_write(tmp_path / "config.yaml", {"acp": {"mcp_tool_filters": filters}})
    native = SimpleNamespace(enabled_toolsets=[], disabled_toolsets=[], _memory_manager=None)
    manager = SessionManager(agent_factory=lambda: native)
    state = manager.create_session(cwd=str(tmp_path))
    with patch("tools.mcp_tool_discovery.register_mcp_servers") as register:
        await HermesACPAgent(session_manager=manager)._register_session_mcp_servers(state, [
            McpServerStdio(name="editor", command="editor-mcp", args=[], env=[]),
        ])
    register.assert_not_called()
    assert native.enabled_toolsets == []
