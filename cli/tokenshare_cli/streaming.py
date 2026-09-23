"""SSE passthrough helpers for `call --stream` (PIN: stream 透传 SSE; the
relay injects stream_options.include_usage, so the terminal chunk carries
the usage object the CLI reports)."""

import json
from typing import Iterable, Optional


def parse_sse_lines(lines: Iterable[str]) -> tuple[str, Optional[dict]]:
    """Consume SSE `data:` lines; return (assistant text, final usage).

    - `data: [DONE]` terminates.
    - Chat chunks: usage taken from any chunk's `usage` field (relay sends a
      final usage-only chunk when include_usage is injected).
    - Non-JSON / non-`data:` lines are ignored.
    """
    text_parts: list[str] = []
    usage: Optional[dict] = None
    for line in lines:
        line = line.strip()
        if not line or not line.startswith("data:"):
            continue
        payload = line[5:].strip()
        if payload == "[DONE]":
            break
        try:
            chunk = json.loads(payload)
        except json.JSONDecodeError:
            continue
        if not isinstance(chunk, dict):
            continue
        choices = chunk.get("choices") or []
        if choices and isinstance(choices[0], dict):
            delta = (choices[0].get("delta") or {}).get("content")
            if delta:
                text_parts.append(delta)
        if isinstance(chunk.get("usage"), dict):
            usage = chunk["usage"]
    return "".join(text_parts), usage


def stream_body(prompt: str, model: str) -> bytes:
    """Body for a streaming request (non-stream body lives in app.py)."""
    import json

    payload = {
        "model": model,
        "messages": [{"role": "user", "content": prompt}],
        "stream": True,
    }
    return json.dumps(payload, separators=(",", ":")).encode("utf-8")
