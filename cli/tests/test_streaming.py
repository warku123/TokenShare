"""Stream passthrough parser tests (PIN: stream 透传 SSE, terminal usage
chunk carries the usage object)."""

import json

from tokenshare_cli.streaming import parse_sse_lines, stream_body


def _sse(*chunks):
    lines = []
    for chunk in chunks:
        lines.append("data: " + json.dumps(chunk, separators=(",", ":")))
        lines.append("")
    lines.append("data: [DONE]")
    lines.append("")
    return lines


def test_parses_deltas_and_final_usage():
    lines = _sse(
        {"choices": [{"delta": {"content": "Hel"}}]},
        {"choices": [{"delta": {"content": "lo"}}]},
        {"choices": [], "usage": {"prompt_tokens": 10, "completion_tokens": 2,
                                  "prompt_tokens_details": {"cached_tokens": 0}}},
    )
    text, usage = parse_sse_lines(lines)
    assert text == "Hello"
    assert usage["prompt_tokens"] == 10


def test_ignores_malformed_and_non_data_lines():
    lines = [
        ": keep-alive comment",
        "",
        "data: not-json",
        "data: {\"choices\":[{\"delta\":{\"content\":\"ok\"}}]}",
        "",
        "data: [DONE]",
    ]
    text, usage = parse_sse_lines(lines)
    assert text == "ok"
    assert usage is None


def test_stream_body_has_stream_true():
    body = stream_body("hi", "m1")
    obj = json.loads(body.decode())
    assert obj == {"model": "m1", "messages": [{"role": "user", "content": "hi"}], "stream": True}
