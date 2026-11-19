"""Pure configuration data constructors, independent of the config loader."""

def auxiliary_defaults(timeout, *, reasoning_effort=True, **extra):
    """Standard auxiliary-task model block (see DEFAULT_CONFIG["auxiliary"]).

    reasoning_effort=False omits that key (MoA blocks configure depth per slot);
    ``extra`` keys are appended after the standard ones.
    """
    d = {"provider": "auto", "model": "", "base_url": "", "api_key": "", "timeout": timeout, "extra_body": {}}
    if reasoning_effort:
        d["reasoning_effort"] = ""
    d.update(extra)
    return d


def security_defaults():
    """Construct a fresh unchanged security-default block for every config import."""
    return {  # Security: URL/private-network guards, redaction and approval presentation.
        "allow_private_urls": False,  # private IPs for web/vision/media + cloud/CDP browsers; not local/sidecar Chromium
        # CIDR blocks a local TUN proxy answers DNS with (Mihomo/Clash fake-ip, Surge enhanced).
        # Answers inside these blocks are the proxy's sentinels, not internal hosts, so the guard
        # dials them instead of rejecting them as private. Empty = normal private-address verdict.
        "fake_ip_ranges": [],
        "redact_secrets": True,
        "redact_phone_numbers": True,  # false: E.164 numbers shown in replies/chat only, never other secrets
        # Ordinary terminal/file results only; credential guards and diagnostics remain separate.
        "ordinary_output": "redact",  # "fidelity" preserves ordinary content; restart to apply.
        # Persisted acknowledgement for unattended model overrides whose tier lets the vendor train
        # on prompts. The startup guard still warns every run; cost guards are unaffected.
        "allow_data_training_tiers_noninteractive": False,
        # Human-approval presentation transport. "builtin" = CLI/TUI/gateway/ACP surfaces; a plugin
        # transport is used only when named explicitly. Transport timeout/error/invalid response
        # DENIES unless transport_fallback is "builtin". Presentation only: plugins cannot detect,
        # suppress, or auto-approve commands outside a correlated human response.
        "approval": {"transport": "builtin", "transport_fallback": "deny"},
        # Writes to agent-instruction files (AGENTS.md/CLAUDE.md/SOUL.md/.cursorrules, project-local
        # .hermes config) always need human approval, even under yolo. Extra patterns are fnmatch
        # globs on the basename (e.g. "*.mdc").
        "protected_instruction_files": True,
        "protected_instruction_extra_patterns": [],
        "website_blocklist": {"enabled": False, "domains": [], "shared_files": []},
        # IDs of supply-chain advisories the user has read and acted on; acked ones stop the startup
        # banner. Add via `hermes doctor --ack <id>`; remove by editing the list. Catalog:
        # hermes_cli/security_advisories.py.
        "acked_advisories": [],
        # Lazy-install opt-in backend packages from PyPI when a backend that needs them is first
        # enabled (e.g. `elevenlabs`). False = require explicit pip install for everything beyond
        # the base set (restricted/audited/air-gapped environments).
        "allow_lazy_installs": True,
    }
