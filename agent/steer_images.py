"""Mid-turn steer rows that carry the user's images (a photo sent while the agent works).

The row is the same standalone ``role:user`` steer row a text steer gets, in the same slot (after the
newest tool result), so role alternation and the cached prefix behave exactly as for text. Its images
are native ``image_url`` parts built by ``image_routing.build_native_content_parts``, the builder a
normal image turn uses: the ``[Image attached at: <path>]`` hints sit inside the steer marker, so the
durable text projection (``[screenshot]`` per image) is the shape ``agent.image_replay`` rebuilds
byte-identically while the profile still retains those bytes.
"""

import logging
from typing import Any, Iterable

logger = logging.getLogger(__name__)


def steer_row(steer_text: str, images: Iterable[str] = ()) -> dict[str, Any]:
    """``prompt_builder.steer_user_row`` with *images* (local paths) attached natively."""
    from agent.prompt_builder import steer_user_row

    images = list(images or ())
    if not images:
        return steer_user_row(steer_text)
    from agent.image_routing import build_native_content_parts

    parts, skipped = build_native_content_parts(steer_text, images)
    if skipped:
        logger.warning("Steer image attachment: skipped %d unreadable path(s)", len(skipped))
    row = steer_user_row(parts[0]["text"] if parts else steer_text)
    if len(parts) > 1:
        row["content"] = [{"type": "text", "text": row["content"]}, *parts[1:]]
    return row


def codex_steer_text(steer_text: str, images: Iterable[str]) -> str:
    """The text of a Codex ``turn/steer`` carrying *images*: the caption plus the same hint per image a
    native row carries, so the projected transcript row (text only) still names each image."""
    from tools.credential_files import to_agent_visible_cache_path

    hints = [f"[Image attached at: {to_agent_visible_cache_path(str(path))}]" for path in images]
    return "\n\n".join([steer_text, "\n".join(hints)]) if hints else steer_text
