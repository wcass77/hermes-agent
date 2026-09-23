"""Gateway vision pre-process prompt should stay concise."""

import json
from pathlib import Path
from unittest.mock import AsyncMock, patch

import pytest


@pytest.mark.asyncio
async def test_enrich_message_with_vision_uses_concise_prompt():
    from gateway.run import GatewayRunner

    runner = GatewayRunner.__new__(GatewayRunner)

    with patch(
        "tools.vision_tools.vision_analyze_tool",
        new_callable=AsyncMock,
        return_value=json.dumps({"success": True, "analysis": "A cat on a chair."}),
    ) as mock_vision:
        result = await runner._enrich_message_with_vision(
            user_text="What is happening here?",
            image_paths=["/tmp/cat.png"],
        )

    assert "A cat on a chair." in result
    assert "What is happening here?" in result
    assert (
        "Concisely describe this image in 2-4 sentences"
        in mock_vision.await_args.kwargs["user_prompt"]
    )
    assert "Skip decorative details." in mock_vision.await_args.kwargs["user_prompt"]
    # No output cap is forwarded: per the max-tokens-knob policy the aux
    # client decides token handling; conciseness comes from the prompt.
    assert "max_tokens" not in mock_vision.await_args.kwargs


@pytest.mark.asyncio
async def test_enrich_message_with_vision_exposes_backend_visible_cache_path(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
):
    from gateway.run import GatewayRunner

    hermes_home = tmp_path / ".hermes"
    image = hermes_home / "cache" / "images" / "participant.jpg"
    image.parent.mkdir(parents=True)
    image.write_bytes(b"image bytes")
    monkeypatch.setenv("HERMES_HOME", str(hermes_home))
    monkeypatch.setenv("TERMINAL_ENV", "docker")
    runner = GatewayRunner.__new__(GatewayRunner)

    with patch(
        "tools.vision_tools.vision_analyze_tool",
        new_callable=AsyncMock,
        return_value=json.dumps({"success": True, "analysis": "A school notice."}),
    ) as mock_vision:
        result = await runner._enrich_message_with_vision("Archive this.", [str(image)])

    assert mock_vision.await_args.kwargs["image_url"] == str(image)
    assert "image_url: /root/.hermes/cache/images/participant.jpg" in result
    assert str(image) not in result
