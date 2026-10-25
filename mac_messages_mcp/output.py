# Copyright (c) 2023 Carter Lasalle
"""Output-format primitives shared by the read tools: format validation and
JSON serialization of untrusted strings."""

from __future__ import annotations

import json
from typing import Any

from .untrusted import neutralize_untrusted_text

# Upper bound for one paginated read or listing. Bounds how much an agent can
# pull into context in a single call; callers page further with `offset`.
_MAX_MESSAGE_LIMIT = 1_000

OUTPUT_FORMATS = ("text", "json")


def _check_format(output_format: str) -> str | None:
    if output_format not in OUTPUT_FORMATS:
        return f"Error: format must be one of {', '.join(OUTPUT_FORMATS)}."
    return None


def _json_dumps(payload: dict[str, Any]) -> str:
    """One-line JSON. The encoder escapes control characters, so the fence
    around the result never carries a raw newline."""
    return json.dumps(payload, ensure_ascii=False)


def _json_text(value: Any, max_chars: int = 0) -> str:
    """Neutralize an untrusted string for a JSON field, keeping real newlines."""
    return neutralize_untrusted_text(value, max_chars=max_chars, keep_newlines=True)


def _participant_json(p: dict[str, Any]) -> dict[str, Any]:
    return {
        "name": _json_text(p["name"]),
        "handle_id": p["handle_id"],
        "address": _json_text(p["address"]) or None,
    }
