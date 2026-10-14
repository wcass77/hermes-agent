"""Native browser pixels must carry their artifact reference to the model."""
from PIL import Image

from tools.browser_tool_vision import _native_vision_result


def test_native_and_fallback_text_expose_original_screenshot(tmp_path):
    screenshot = tmp_path / "browser_screenshot.png"
    Image.new("RGB", (16, 16), "white").save(screenshot)
    original = screenshot.read_bytes()
    result = _native_vision_result(screenshot, "Verify the displayed row", False, {}, None)

    native_text = "\n".join(part["text"] for part in result["content"] if part["type"] == "text")
    assert str(screenshot) in native_text
    assert str(screenshot) in result["text_summary"]
    assert result["meta"]["screenshot_path"] == str(screenshot)
    assert any(part["type"] == "image_url" for part in result["content"])
    assert screenshot.read_bytes() == original
