"""Participant authorization belongs to Zhaan's adapter, never the process env."""
from dataclasses import replace
from types import SimpleNamespace

from plugins.platforms.discord.adapter import DiscordAdapter


class ParticipantDiscordAdapter(DiscordAdapter):
    def _get_allowed_users(self) -> set:
        return set(self.config.extra["allow_from"])

    def authorization_user_ids(self) -> set[str]:
        """The People Registry is authoritative at both gateway authorization gates."""
        return self._get_allowed_users()

    def _is_sender_authorized(self, user_id, chat_type=None, chat_id=None, **kwargs):
        if str(user_id or "") not in self._get_allowed_users():
            return False
        return super()._is_sender_authorized(user_id, chat_type, chat_id, **kwargs)


def register_discord(ctx, participant_ids):
    from plugins.platforms.discord.adapter import register
    allowed = sorted(participant_ids)

    def factory(config):
        scoped = replace(config, extra={**config.extra, "allow_from": allowed, "allowed_users": allowed})
        return ParticipantDiscordAdapter(scoped)

    def platform(**options):
        options["adapter_factory"] = factory
        return ctx.register_platform(**options)

    register(SimpleNamespace(register_platform=platform))
