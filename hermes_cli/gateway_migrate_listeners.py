"""Preflight for plugin-declared listeners that remain owned by individual profiles."""


def check_profile_listeners(plan, configs):
    from gateway.listener_ownership import profile_listener_claim
    from gateway.run import _profile_runtime_scope
    from hermes_cli.profiles import profile_is_parked

    owners = {}
    for profile in plan.profiles:
        config = configs.get(profile.name)
        if config is None or profile_is_parked(profile.home):
            continue
        with _profile_runtime_scope(profile.home, hydrate_secrets=False):
            for platform, settings in config.platforms.items():
                if not settings.enabled:
                    continue
                claim = profile_listener_claim(platform.value, settings)
                if claim is None:
                    continue
                owner = owners.setdefault(claim, (profile.name, platform.value))
                if owner != (profile.name, platform.value):
                    plan.blockers.append(
                        f"Profiles '{owner[0]}' ({owner[1]}) and '{profile.name}' ({platform.value}) "
                        "declare the same listener; configure distinct per-profile listeners before migration.")
