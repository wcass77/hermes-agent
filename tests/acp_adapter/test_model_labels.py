"""ACP choices must remain distinguishable when a narrow client truncates labels."""

import pytest

from acp_adapter.model_catalog import build_model_state
from hermes_cli import inventory, models


@pytest.mark.parametrize("in_inventory", [True, False])
def test_model_identity_precedes_long_provider_label(monkeypatch, in_inventory):
    provider = "openai-codex"
    provider_name = "ChatGPT or Codex Subscription"
    model = "example-model"
    monkeypatch.setattr(models, "provider_label", lambda _provider: provider_name)
    monkeypatch.setattr("acp_adapter.model_catalog._named_custom_provider_catalogs", list)
    monkeypatch.setattr(inventory, "build_models_payload", lambda *_args, **_kwargs: {
        "providers": [{"slug": provider, "name": provider_name, "models": [model]}]
        if in_inventory else [],
    })

    state = build_model_state(model, provider, "")

    assert state.current_model_id == f"{provider}:{model}"
    choice = next(row for row in state.available_models if row.model_id == state.current_model_id)
    assert choice.name.startswith(model)
    assert provider_name in choice.description
