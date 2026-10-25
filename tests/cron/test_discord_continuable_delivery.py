"""Discord flat scheduled deliveries keep the scheduling user's session."""
from unittest.mock import patch
from tests.cron.test_scheduler import TestCronContinuableSurfaceInChannel as _Fixtures


class TestDiscordContinuableDelivery:
    _slack_cfg = _Fixtures._slack_cfg
    _slack_adapter = _Fixtures._slack_adapter
    _run_inchannel_delivery = _Fixtures._run_inchannel_delivery

    def test_discord_flat_delivery_seeds_owner_and_no_synthetic_thread(self):
        from plugins.platforms.discord.adapter import DiscordAdapter
        assert DiscordAdapter.supports_inchannel_continuable is True
        from gateway.session import SessionSource, build_session_key
        from gateway.config import Platform
        adapter = self._slack_adapter()
        with patch("cron.scheduler_delivery._seed_cron_thread_session") as thread_seed:
            open_thread, mirror = self._run_inchannel_delivery(
                {"cron_continuable_surface": "in_channel"}, adapter, platform_name="discord")
        open_thread.assert_not_called()
        thread_seed.assert_not_called()
        source = adapter._session_store.get_or_create_session.call_args[0][0]
        inbound = SessionSource(platform=Platform.DISCORD, chat_id="C123", chat_type="group", user_id="U_HUMAN")
        assert build_session_key(source) == build_session_key(inbound)
        assert mirror.called

    def test_discord_missing_owner_does_not_seed_orphan_group(self):
        adapter = self._slack_adapter()
        with patch("cron.scheduler_delivery._seed_cron_channel_session") as seed:
            self._run_inchannel_delivery({"cron_continuable_surface": "in_channel"},
                adapter, platform_name="discord", origin={"platform": "discord", "chat_id": "C123"})
        seed.assert_not_called()
        adapter._session_store.get_or_create_session.assert_not_called()

    def test_discord_default_preserves_thread_behavior(self):
        adapter = self._slack_adapter()
        open_thread, _ = self._run_inchannel_delivery({}, adapter, platform_name="discord")
        open_thread.assert_called_once()

