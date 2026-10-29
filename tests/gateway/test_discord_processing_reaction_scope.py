"""Per-profile reaction settings must not borrow another profile's environment."""
from gateway.config import PlatformConfig
from plugins.platforms.discord.adapter import DiscordAdapter


def test_processing_reactions_use_each_adapters_scoped_config(monkeypatch):
    monkeypatch.setenv("DISCORD_PROCESSING_START_REACTION", "false")
    from agent.secret_scope import set_secret_scope, reset_secret_scope, set_multiplex_active
    # Explicit adapter settings remain separate when the launch environment disagrees.
    adapters = []
    for enabled in (True, False):
        adapter = object.__new__(DiscordAdapter)
        adapter.config = PlatformConfig(enabled=True, extra={"reactions": True, "processing_start_reaction": enabled})
        adapters.append(adapter)
    set_multiplex_active(True)
    token = set_secret_scope({})
    try:
        assert adapters[0]._processing_start_reaction_enabled() is True
        assert adapters[1]._processing_start_reaction_enabled() is False
    finally:
        reset_secret_scope(token)
        set_multiplex_active(False)
