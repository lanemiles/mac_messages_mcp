# Copyright (c) 2023 Carter Lasalle
"""Decode message text out of Apple typedstream blobs (``attributedBody``)."""

from __future__ import annotations


def extract_body_from_attributed(attributed_body: bytes | None) -> str | None:
    """Extract message content from attributedBody binary data, or None.

    The typedstream layout the decoder walks is documented on
    ``_decode_attributed_body``.
    """
    try:
        return _decode_attributed_body(attributed_body)
    except (AttributeError, IndexError, TypeError, ValueError):
        return None


def _decode_attributed_body(attributed_body: bytes | None) -> str | None:
    r"""Decode the UTF-8 text out of an Apple typedstream blob.

    The attributedBody column contains an Apple typedstream blob
    (NSArchiver serialization of NSMutableAttributedString).  The string
    content is stored after the first ``NSString`` class marker followed
    by a 5-byte header (``\x01 <byte> \x84 \x01 +``) and a
    variable-length integer encoding the byte length of the UTF-8 text.

    Length encoding (first byte after the header):
        < 0x80  — the byte *is* the length.
        0x81    — next 2 bytes (little-endian) are the length.
        0x82    — next 3 bytes (little-endian) are the length.
        0x83    — next 4 bytes (little-endian) are the length.
    """
    if attributed_body is None:
        return None

    # Locate the first NSString class reference in the blob.
    marker = b"NSString"
    idx = attributed_body.find(marker)
    if idx < 0:
        return None

    # Skip past: NSString (8) + \x01 + <byte> + \x84 + \x01 + '+' = 5 bytes
    pos = idx + len(marker) + 5

    if pos >= len(attributed_body):
        return None

    # Read the variable-length integer for the text byte count.
    length_byte = attributed_body[pos]
    pos += 1

    if length_byte < 0x80:
        text_length = length_byte
    elif length_byte == 0x81:
        if pos + 2 > len(attributed_body):
            return None
        text_length = attributed_body[pos] | (attributed_body[pos + 1] << 8)
        pos += 2
    elif length_byte == 0x82:
        if pos + 3 > len(attributed_body):
            return None
        text_length = (
            attributed_body[pos]
            | (attributed_body[pos + 1] << 8)
            | (attributed_body[pos + 2] << 16)
        )
        pos += 3
    elif length_byte == 0x83:
        if pos + 4 > len(attributed_body):
            return None
        text_length = (
            attributed_body[pos]
            | (attributed_body[pos + 1] << 8)
            | (attributed_body[pos + 2] << 16)
            | (attributed_body[pos + 3] << 24)
        )
        pos += 4
    else:
        return None

    if pos + text_length > len(attributed_body):
        return None

    return attributed_body[pos : pos + text_length].decode(
        "utf-8",
        errors="replace",
    )
