"""Profile-local tool filters for MCP servers supplied by an ACP client."""

from acp.schema import McpServerHttp, McpServerSse, McpServerStdio


def client_mcp_server_configs(servers: list[McpServerStdio | McpServerHttp | McpServerSse]) -> dict[str, dict]:
    """Preserve client transports; pass only local include/exclude rules to MCP registration."""
    from hermes_cli.config import load_config

    filters = (load_config().get("acp") or {}).get("mcp_tool_filters", {})
    if not isinstance(filters, dict):
        raise ValueError("acp.mcp_tool_filters must map client server names to tool filters")
    configs = {}
    for server in servers:
        if isinstance(server, McpServerStdio):
            config = {"command": server.command, "args": list(server.args),
                      "env": {item.name: item.value for item in server.env}}
        else:
            config = {"url": server.url, "headers": {item.name: item.value for item in server.headers}}
        if server.name in filters:
            config["tools"] = _validated_tool_filter(filters[server.name])
        configs[server.name] = config
    return configs


def _validated_tool_filter(rules: dict) -> dict:
    """Malformed local policy must fail registration rather than accidentally expose all tools."""
    if not isinstance(rules, dict):
        raise ValueError("ACP MCP tool filters must be mappings")
    for key, names in rules.items():
        if key in {"resources", "prompts"}:
            if not isinstance(names, bool):
                raise ValueError(f"ACP MCP tools.{key} must be a boolean")
            continue
        if key not in {"include", "exclude"}:
            raise ValueError(f"Unknown ACP MCP tool filter key: {key}")
        if not (isinstance(names, str) or isinstance(names, list) and all(isinstance(n, str) for n in names)):
            raise ValueError(f"ACP MCP tools.{key} must be a name or a list of names")
    return dict(rules)
