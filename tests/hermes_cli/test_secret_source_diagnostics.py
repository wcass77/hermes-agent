"""Startup diagnostics retain categories, not secret payloads, and stay per-home."""
from types import SimpleNamespace

from agent.secret_sources.base import ErrorKind, FetchResult
from hermes_cli.secret_source_diagnostics import hydration_observations, observe_hydration


def test_early_failure_survives_cached_reads_and_stays_per_home(tmp_path):
    own, sibling = tmp_path / "own", tmp_path / "sibling"
    report = SimpleNamespace(sources=[SimpleNamespace(
        name="vault", result=FetchResult().fail("sensitive-error", ErrorKind.AUTH_FAILED))])
    observe_hydration(own, "fetched", report)
    for _ in range(100):
        observe_hydration(own, "cached")
    observe_hydration(sibling, "no_config")
    events = hydration_observations(own)
    assert len(events) == 2
    assert events[0]["sources"] == [{"source": "vault", "category": "auth_failed"}]
    assert all(event["home"] == str(own.resolve()) for event in events)
    assert "sensitive-error" not in str(events)
    events[0]["sources"].clear()
    assert hydration_observations(own)[0]["sources"]


def test_unknown_labels_and_categories_are_sanitized(tmp_path):
    result = FetchResult().fail("secret-error", "secret-kind")
    report = SimpleNamespace(sources=[SimpleNamespace(name="bad\nsecret-name", result=result)])
    observe_hydration(tmp_path, "secret-status", report)
    assert hydration_observations(tmp_path) == [{
        "home": str(tmp_path.resolve()), "outcome": "unknown",
        "sources": [{"source": "invalid", "category": "unknown"}],
    }]
