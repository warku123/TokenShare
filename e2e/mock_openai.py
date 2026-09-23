#!/usr/bin/env python3
"""Local deterministic mock OpenAI (M5a design decision 2).

Serves the endpoint the relay forwards to. Path-agnostic on POST on purpose:
the relay joins OPENAI_BASE_URL + its own fixed path, so depending on the
base URL the effective path may be /v1/chat/completions or /v1/v1/…
(joined by httpx). The mock serves any POST path — only the relay's
passthrough behavior matters here.

Two modes driven by the request body's "stream" flag:

* non-stream  -> standard OpenAI JSON completion + `usage`
* stream=true -> SSE passthrough shape: content delta chunks, a final usage
                 chunk (choices == []), then `data: [DONE]`

`stream_options.include_usage` is IGNORED — the final usage chunk is always
sent, which is exactly what the relay needs (it injects include_usage anyway
and reads usage from the last chunk).

Usage counts (prompt/cached/completion) are CONFIGURABLE via CLI flags so the
e2e runner can pair them with Registry tiered prices to produce a non-zero
settled amount. Zero external dependencies (http.server only).

The reply content embeds the model name and a fixed marker so the e2e runner
can assert the round trip verbatim.
"""

from __future__ import annotations

import argparse
import json
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

CHAT_PATH = "/v1/chat/completions"
REPLY_MARKER = "TokenShare mock LLM online"


def _usage(prompt: int, cached: int, completion: int) -> dict:
    return {
        "prompt_tokens": prompt,
        "completion_tokens": completion,
        "total_tokens": prompt + completion,
        "prompt_tokens_details": {"cached_tokens": cached},
        "completion_tokens_details": {"reasoning_tokens": 0},
    }


class Handler(BaseHTTPRequestHandler):
    server_version = "TokenShareMockOpenAI/1"
    # Configurable counts, set on the server object by run().
    prompt_tokens = 0
    cached_tokens = 0
    completion_tokens = 0

    def log_message(self, *args):  # keep stdout quiet for the e2e runner
        pass

    # ------------------------------------------------------------- helpers
    def _chat_body(self) -> dict:
        length = int(self.headers.get("Content-Length", "0"))
        raw = self.rfile.read(length) if length else b"{}"
        try:
            body = json.loads(raw)
        except json.JSONDecodeError:
            body = {}
        return body if isinstance(body, dict) else {}

    def _reply_text(self, model: str, prompt: str) -> str:
        return f"[{model}] {REPLY_MARKER} — echo: {prompt}"

    def _first_user_prompt(self, body: dict) -> str:
        messages = body.get("messages") or []
        for msg in reversed(messages):
            if isinstance(msg, dict) and msg.get("role") == "user":
                content = msg.get("content")
                return content if isinstance(content, str) else json.dumps(content)
        return ""

    def _model_name(self, body: dict) -> str:
        model = body.get("model")
        return model if isinstance(model, str) and model else "mock-model"

    # ------------------------------------------------------------- health
    def do_GET(self) -> None:
        if self.path in ("/health", "/healthz"):
            payload = json.dumps({"status": "ok", "server": "tokenshare-mock-openai"}).encode()
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(payload)))
            self.end_headers()
            self.wfile.write(payload)
            return
        self.send_response(404)
        self.end_headers()

    # ------------------------------------------------------------- chat
    def do_POST(self) -> None:
        # Path-agnostic (see module docstring): serve any POST as chat.
        body = self._chat_body()
        model = self._model_name(body)
        prompt = self._first_user_prompt(body)
        reply = self._reply_text(model, prompt)
        usage = _usage(self.prompt_tokens, self.cached_tokens, self.completion_tokens)
        created = int(time.time())

        if body.get("stream"):
            self._serve_stream(model, created, reply, usage)
        else:
            self._serve_json(model, created, reply, usage)

    def _serve_json(self, model: str, created: int, reply: str, usage: dict) -> None:
        payload = {
            "id": "chatcmpl-tokenshare-mock",
            "object": "chat.completion",
            "created": created,
            "model": model,
            "choices": [
                {
                    "index": 0,
                    "message": {"role": "assistant", "content": reply},
                    "logprobs": None,
                    "finish_reason": "stop",
                }
            ],
            "usage": usage,
            "object_type": "chat.completion",
        }
        raw = json.dumps(payload).encode()
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(raw)))
        self.end_headers()
        self.wfile.write(raw)

    def _serve_stream(self, model: str, created: int, reply: str, usage: dict) -> None:
        self.send_response(200)
        self.send_header("Content-Type", "text/event-stream")
        self.send_header("Cache-Control", "no-cache")
        self.end_headers()

        def chunk(delta: dict, finish: str | None = None) -> bytes:
            event = {
                "id": "chatcmpl-tokenshare-mock",
                "object": "chat.completion.chunk",
                "created": created,
                "model": model,
                "choices": [
                    {"index": 0, "delta": delta, "logprobs": None, "finish_reason": finish}
                ],
            }
            return f"data: {json.dumps(event)}\n\n".encode()

        self.wfile.write(chunk({"role": "assistant", "content": ""}))
        # Split the reply across a few content deltas (deterministic split).
        piece = max(1, len(reply) // 3)
        parts = [reply[i : i + piece] for i in range(0, len(reply), piece)]
        for part in parts:
            self.wfile.write(chunk({"content": part}))
        self.wfile.write(chunk({}, "stop"))
        # Final usage chunk (choices empty) — always emitted, so
        # stream_options.include_usage need not be honored.
        final = {
            "id": "chatcmpl-tokenshare-mock",
            "object": "chat.completion.chunk",
            "created": created,
            "model": model,
            "choices": [],
            "usage": usage,
        }
        self.wfile.write(f"data: {json.dumps(final)}\n\n".encode())
        self.wfile.write(b"data: [DONE]\n\n")
        self.wfile.flush()


def main() -> None:
    parser = argparse.ArgumentParser(description="TokenShare local mock OpenAI")
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=0, help="0 = pick a random free port")
    parser.add_argument("--prompt-tokens", type=int, default=5000)
    parser.add_argument("--cached-tokens", type=int, default=1000)
    parser.add_argument("--completion-tokens", type=int, default=800)
    args = parser.parse_args()

    Handler.prompt_tokens = args.prompt_tokens
    Handler.cached_tokens = args.cached_tokens
    Handler.completion_tokens = args.completion_tokens

    server = ThreadingHTTPServer((args.host, args.port), Handler)
    server.daemon_threads = True
    print(f"MOCK_OPENAI_READY {server.server_address[1]}", flush=True)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()


if __name__ == "__main__":
    main()
