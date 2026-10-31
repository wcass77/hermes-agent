"""Recover legacy completion profile labels from a trusted canonical route.

The namespace selects execution state; it never selects the receiving bot.
"""
from dataclasses import replace

from gateway.session_identity import IdentityUnresolved, identity_of, replace_source


def completion_source(runner, source, session_key):
    """Return a corrected copy, refusing explicit profile contradictions before any DB read.

    Standalone gateways intentionally use agent:main even for named launch profiles.
    Raw API keys have their own ownership proof and are left to that dispatcher.
    """
    from gateway.run import _parse_session_key
    from hermes_cli.profiles import get_profile_dir, profile_exists

    route = _parse_session_key(session_key)
    if source is None or not route or not getattr(getattr(runner, 'config', None), 'multiplex_profiles', False):
        return source
    profile = route.get('profile') or 'default'
    explicit = source.profile
    identity = identity_of(source)
    if explicit and explicit != profile:
        raise IdentityUnresolved('Completion source profile contradicts its canonical route')
    if explicit and identity is not None and identity.runtime_profile != profile:
        raise IdentityUnresolved('Completion identity contradicts its canonical route')
    if not profile_exists(profile):
        raise IdentityUnresolved('Completion route profile is unavailable')
    # An omitted default label is part of native admission's durable principal and
    # relay seals. Recover named runtimes without changing that historical spelling.
    corrected = replace_source(source, profile=explicit or (profile if profile != 'default' else None))
    # Never use the live source resolver's missing-profile fallback for a pinned completion.
    home = get_profile_dir(profile)
    registry = getattr(runner, 'session_authorities', None)
    if registry is not None and registry.for_home(home) is None:
        raise IdentityUnresolved('Completion route profile is not served')
    if identity is not None:
        corrected._identity = replace(identity, runtime_profile=profile, runtime_home=home)
    return corrected
