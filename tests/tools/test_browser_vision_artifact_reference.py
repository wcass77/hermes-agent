"""Native browser pixels must carry their artifact reference to the model (fork ec690c1dad, browser_exec)."""
from PIL import Image

from tools import browser_use_cli


def test_native_and_fallback_text_expose_original_screenshot(tmp_path, monkeypatch):
    from tools import vision_tools
    monkeypatch.setattr(vision_tools, "_should_use_native_vision_fast_path", lambda *a, **k: True)
    screenshot = tmp_path / "browser_screenshot.png"
    Image.new("RGB", (16, 16), "white").save(screenshot)
    original = screenshot.read_bytes()
    base = {"success": True, "output": "Verify the displayed row", "screenshot_path": str(screenshot)}
    result = browser_use_cli._native_screenshot_result(base, str(screenshot))

    native_text = "\n".join(part["text"] for part in result["content"] if part["type"] == "text")
    assert str(screenshot) in native_text
    assert str(screenshot) in result["text_summary"]
    assert result["meta"]["screenshot_path"] == str(screenshot)
    assert any(part["type"] == "image_url" for part in result["content"])
    assert screenshot.read_bytes() == original
