"""Sol 6.2 Codex compaction uses route metadata rather than the API window."""

from types import SimpleNamespace

import pytest

from agent.auxiliary_client import _compression_threshold_for_model
from agent.model_metadata import (
    _resolve_codex_oauth_context_length_with_source,
    get_model_context_length,
    has_codex_context_variant,
)
from agent.native_compaction import native_compaction_context_management
from agent.reasoning_effort import clamp_effort, codex_supported_efforts

CODEX = "https://chatgpt.com/backend-api/codex"


def test_offline_sol62_window_threshold_and_request(monkeypatch):
    from agent import model_metadata as mm

    monkeypatch.setattr(mm, "_fetch_codex_oauth_context_lengths_with_source", lambda *a, **k: ({}, False))
    window, source = _resolve_codex_oauth_context_length_with_source("gpt-6.2-sol")
    assert (window, source) == (500_000, "fallback")
    assert get_model_context_length("gpt-6.2-sol", base_url=CODEX, provider="openai-codex") == window
    threshold = _compression_threshold_for_model("gpt-6.2-sol", "openai-codex")
    assert threshold == 0.85
    agent = SimpleNamespace(
        model="gpt-6.2-sol", provider="openai-codex", base_url=CODEX,
        codex_responses_native_compaction=True, compression_enabled=True,
        context_compressor=SimpleNamespace(threshold_tokens=int(window * threshold)),
    )
    assert native_compaction_context_management(agent, is_codex_backend=True) == [
        {"type": "compaction", "compact_threshold": 416_808},
    ]
    assert not has_codex_context_variant("gpt-6.2-sol")  # no unverified 900K alias
    assert _compression_threshold_for_model("gpt-6.2-sol", "openai") is None
    assert _compression_threshold_for_model("gpt-6.2-sol", "openai-codex", allow_codex_gpt55_autoraise=False) is None


@pytest.mark.parametrize("requested,expected", [("none", "low"), ("minimal", "low"), ("high", "high"), ("max", "max"), ("ultra", "max")])
def test_sol62_effort_uses_predecessor_wire_vocabulary(requested, expected):
    supported = codex_supported_efforts("openai/gpt-6.2-sol")
    assert supported == codex_supported_efforts("gpt-6.1-sol")
    assert clamp_effort(requested, supported) == expected
