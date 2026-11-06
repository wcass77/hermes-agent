"""Lightweight raster format recognition for the private media cache."""

def looks_like_image(data: bytes) -> bool:
    """Recognize existing raster signatures and bounded HEIC still-image ftyp brands.

    This is a format sniff, not full pixel validation (as for JPEG/PNG).
    Preserve the original bytes; vision normalization remains separate.
    """
    if len(data) >= 4 and (data[:8] == b"\x89PNG\r\n\x1a\n" or data[:3] == b"\xff\xd8\xff"
               or data[:6] in {b"GIF87a", b"GIF89a"} or data[:2] == b"BM"
               or (data[:4] == b"RIFF" and len(data) >= 12 and data[8:12] == b"WEBP")):
        return True
    if len(data) < 16 or data[4:8] != b"ftyp":
        return False
    box_size = int.from_bytes(data[:4], "big")
    # No extended/zero/truncated boxes or scans into subsequent boxes.
    # Bound work even when the server supplies a huge compatible-brand table.
    if box_size < 16 or box_size > len(data) or box_size > 4096 or box_size % 4:
        return False
    brands = {data[8:12]} | {data[i:i + 4] for i in range(16, box_size, 4)}
    return bool(brands & {b"heic", b"heix"})
