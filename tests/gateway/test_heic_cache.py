"""Original HEIC payloads survive the cache/MCP attachment path unchanged."""
import base64
import json
from pathlib import Path
from types import SimpleNamespace

import pytest

# A bounded HEIC-compatible ftyp box. Caching sniffs format; it never decodes pixels.
HEIC = b'\x00\x00\x00\x18ftypmif1\x00\x00\x00\x00heicmif1'


def test_heic_original_survives_mcp_cache(tmp_path, monkeypatch):
    monkeypatch.setenv('HERMES_HOME', str(tmp_path))
    from tools.mcp_tool_handlers import _render_call_tool_result
    block = SimpleNamespace(type='image', data=base64.b64encode(HEIC).decode(), mimeType='image/heic')
    result = SimpleNamespace(content=[SimpleNamespace(type='text', text='original.HEIC'), block])
    paths = []
    text = json.loads(_render_call_tool_result(result, 'apple-photos', image_paths=paths))['result']
    assert len(paths) == 1
    path = Path(paths[0])
    assert f'MEDIA:{path}' in text
    assert path.suffix == '.heic'
    assert path.read_bytes() == HEIC
    assert path.resolve().is_relative_to(tmp_path.resolve())


@pytest.mark.parametrize('payload', [
    b'<html>not an image</html>',
    b'\x00\x00\x00\x10ftypmp42\x00\x00\x00\x00heic',  # brand in following box
    b'\x00\x00\x00\x20ftypheic\x00\x00\x00\x00',       # truncated
    b'\x00\x00\x00\x00ftypheic\x00\x00\x00\x00',       # unbounded box
])
def test_non_image_and_malformed_containers_still_refused(payload, tmp_path, monkeypatch):
    monkeypatch.setenv('HERMES_HOME', str(tmp_path))
    from gateway.platforms.base import cache_image_from_bytes
    with pytest.raises(ValueError):
        cache_image_from_bytes(payload, ext='.heic')
