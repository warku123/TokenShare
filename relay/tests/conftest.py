"""Shared fixtures for the relay test suite — zero real chain, zero real
OpenAI. Chain layer is replaced by an in-process FakeChain; the OpenAI
upstream is a local threaded HTTP server whose behavior each test selects.
"""

from __future__ import annotations

import hashlib
import json
import sys
import threading
import time
import types
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any

import pytest
from eth_account import Account
from eth_account.messages import encode_defunct
from fastapi.testclient import TestClient

REPO_ROOT = Path(__file__).resolve().parents[2]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

# Deterministic test identities (anvil-style well-known keys, valueless).
BUYER_KEY = "0x59c6995e998f97a5a0044966f0945389dc9e86dae88c7a8412f4603b6b78690d"
SELLER_KEY = "0xac0974bec39a17e36ba4a6b4d238ff944bacb478cbed5efcae784d7bf4f2ff80"
BUYER = Account.from_key(BUYER_KEY).address
SELLER = Account.from_key(SELLER_KEY).address
CHAIN_ID = 84532

# Frozen clock for TTL-margin determinism.
FROZEN = 1_700_000_000.0

# Tiered listing prices used across the suite: per-1M-token USDC native units.
PRICE_CACHED_IN = 25_000
PRICE_INPUT = 50_000
PRICE_OUTPUT = 100_000

# Mixed usage: prompt 1500 (400 cached), completion 2500.
USAGE = {"prompt_tokens": 1500, "prompt_tokens_details": {"cached_tokens": 400},
         "completion_tokens": 2500}
# PIN: (400*25k + 1100*50k + 2500*100k) // 1e6 = 315
ACTUAL = 315


# --------------------------------------------------------------------------
# Fake on-chain layer (drop-in for relay.app.chain.ChainClient)
# --------------------------------------------------------------------------
class FakeChain:
    """Same constructor/method surface as the real ChainClient; per-test
    behavior via class attributes (reset by the autouse `fake_chain` fixture)."""

    settle_calls: list[tuple[int, int]] = []
    payment_reads: int = 0
    listing_reads: int = 0
    valid_calls: list[tuple[int, str, int]] = []
    settle_fails: bool = False
    valid: bool = True
    ttl_delta: int = 600
    max_amount: int = 1_000_000
    listing_active: bool = True
    listing_registered: bool = True
    models: list[str] = ["gpt-4o-mini", ""]

    def __init__(
        self, *, rpc_url: str, escrow_addr: str, registry_addr: str,
        seller_key: str, chain_id: int,
    ) -> None:
        self.seller_address = Account.from_key(seller_key).address

    @classmethod
    def reset(cls) -> None:
        cls.settle_calls = []
        cls.payment_reads = 0
        cls.listing_reads = 0
        cls.valid_calls = []
        cls.settle_fails = False
        cls.valid = True
        cls.ttl_delta = 600
        cls.max_amount = 1_000_000
        cls.listing_active = True
        cls.listing_registered = True
        cls.models = ["gpt-4o-mini", ""]

    def get_payment(self, payment_id: int) -> dict[str, Any]:
        type(self).payment_reads += 1
        return {
            "buyer": BUYER,
            "seller": SELLER,
            "maxAmount": type(self).max_amount,
            "expiresAt": int(FROZEN) + type(self).ttl_delta,
            "state": 1,
        }

    def is_valid(self, payment_id: int, seller: str, min_amount: int) -> bool:
        type(self).valid_calls.append((payment_id, seller, min_amount))
        return type(self).valid

    def get_listing(self, operator: str) -> dict[str, Any] | None:
        type(self).listing_reads += 1
        if not type(self).listing_registered:
            return None
        return {
            "operator": operator,
            "endpoint": "http://relay.example",
            "models": list(type(self).models),
            "priceCachedIn": PRICE_CACHED_IN,
            "priceInput": PRICE_INPUT,
            "priceOutput": PRICE_OUTPUT,
            "active": type(self).listing_active,
        }

    def settle(self, payment_id: int, actual_amount: int) -> dict[str, Any]:
        if type(self).settle_fails:
            raise RuntimeError("rpc down")
        type(self).settle_calls.append((payment_id, actual_amount))
        return {"status": 1}


# --------------------------------------------------------------------------
# Local mock OpenAI upstream (threaded HTTP server; no external calls)
# --------------------------------------------------------------------------
class MockOpenAIHandler(BaseHTTPRequestHandler):
    """Behavior driven by `server.behavior` dict:
    mode: ok | ok_no_usage | error_500 | slow_json | stream_ok |
          stream_no_usage | stream_slow
    """

    def log_message(self, *args: Any) -> None:  # silence test output
        pass

    def do_POST(self) -> None:
        server = self.server
        length = int(self.headers.get("Content-Length", "0"))
        raw = self.rfile.read(length) if length else b"{}"
        try:
            body = json.loads(raw)
        except json.JSONDecodeError:
            body = {}
        with server.behavior_lock:
            server.received.append(body)
            mode = server.mode

        usage = dict(server.usage)

        if mode in ("ok", "ok_no_usage", "error_500", "slow_json"):
            if mode == "slow_json":
                time.sleep(server.slow_seconds)
            if mode == "error_500":
                payload = json.dumps({"error": {"message": "upstream boom"}}).encode()
                self.send_response(500)
            elif mode == "ok_no_usage":
                payload = json.dumps({"id": "cmpl-1", "choices": [
                    {"message": {"role": "assistant", "content": "hi"}}]}).encode()
                self.send_response(200)
            else:
                payload = json.dumps({"id": "cmpl-1", "choices": [
                    {"message": {"role": "assistant", "content": "hi"}}],
                    "usage": usage}).encode()
                self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(payload)))
            self.end_headers()
            self.wfile.write(payload)
            return

        # ---- SSE modes -----------------------------------------------------
        self.send_response(200)
        self.send_header("Content-Type", "text/event-stream")
        self.end_headers()
        chunk = {"id": "c1", "choices": [{"delta": {"content": "hi"}}]}
        if mode == "stream_slow":
            self.wfile.write(b"data: {\"id\":\"c1\",\"choices\":[{\"delta\":{\"content\":\"a\"}}]}\n\n")
            self.wfile.flush()
            time.sleep(server.slow_seconds)
            self.wfile.write(b"data: [DONE]\n\n")
            return
        self.wfile.write(f"data: {json.dumps(chunk)}\n\n".encode())
        if mode == "stream_no_usage":
            self.wfile.write(b"data: {\"id\":\"c1\",\"choices\":[]}\n\n")
        else:
            final = {"id": "c1", "choices": [], "usage": usage}
            self.wfile.write(f"data: {json.dumps(final)}\n\n".encode())
        self.wfile.write(b"data: [DONE]\n\n")


class MockOpenAIServer(ThreadingHTTPServer):
    daemon_threads = True

    def __init__(self, *args: Any, **kwargs: Any) -> None:
        super().__init__(*args, **kwargs)
        self.behavior_lock = threading.Lock()
        self.received: list[dict[str, Any]] = []
        self.mode = "ok"
        self.usage: dict[str, Any] = dict(USAGE)
        self.slow_seconds = 0.5


import pytest  # noqa: E402  (kept after class defs for readability)

# --------------------------------------------------------------------------
# Fixtures
# --------------------------------------------------------------------------
@pytest.fixture(scope="session")
def mock_openai() -> MockOpenAIServer:
    srv = MockOpenAIServer(("127.0.0.1", 0), MockOpenAIHandler)
    thread = threading.Thread(target=srv.serve_forever, daemon=True)
    thread.start()
    yield srv
    srv.shutdown()
    srv.server_close()


@pytest.fixture(autouse=True)
def reset_upstream(mock_openai: MockOpenAIServer) -> None:
    """Fresh upstream behavior per test (mode/received/usage)."""
    mock_openai.mode = "ok"
    mock_openai.received.clear()
    mock_openai.usage = dict(USAGE)
    mock_openai.slow_seconds = 0.5


@pytest.fixture(autouse=True)
def fake_chain(monkeypatch: pytest.MonkeyPatch) -> type[FakeChain]:
    """Fresh FakeChain per test; patch relay.app.main.ChainClient and freeze
    the clock used by the TTL-margin check."""
    FakeChain.reset()
    import relay.app.main as m

    monkeypatch.setattr(m, "ChainClient", FakeChain)
    monkeypatch.setattr(m, "time", types.SimpleNamespace(time=lambda: FROZEN))
    return FakeChain


def setup_relay_env(monkeypatch: pytest.MonkeyPatch, base_url: str) -> None:
    env = {
        "RELAY_SELLER_KEY": SELLER_KEY,
        "RPC_URL": "http://unused.invalid",
        "CHAIN_ID": str(CHAIN_ID),
        "ESCROW_ADDR": "0x1111111111111111111111111111111111111111",
        "REGISTRY_ADDR": "0x2222222222222222222222222222222222222222",
        "USDC_ADDR": "0x3333333333333333333333333333333333333333",
        "OPENAI_API_KEY": "sk-test",
        "OPENAI_BASE_URL": base_url,
        "FORWARD_MARGIN_S": "120",
        "PORT": "8787",
        "PROMPT_TOKEN_CAP": "200000",
        "COMPLETION_TOKEN_CAP": "32000",
    }
    for key, value in env.items():
        monkeypatch.setenv(key, value)


@pytest.fixture
def client(monkeypatch: pytest.MonkeyPatch, mock_openai: MockOpenAI) -> TestClient:
    """Entered-lifespan TestClient wired to FakeChain + mock OpenAI."""
    import relay.app.main as m

    setup_relay_env(monkeypatch, mock_openai_url(mock_openai))
    with TestClient(m.app) as c:
        yield c


def mock_openai_url(server: MockOpenAI) -> str:
    return f"http://127.0.0.1:{server.server_port}/v1"


# --------------------------------------------------------------------------
# Request-building helpers
# --------------------------------------------------------------------------
def chat_body(
    model: str = "gpt-4o-mini",
    messages: list[dict[str, str]] | None = None,
    extra: dict[str, Any] | None = None,
) -> bytes:
    body: dict[str, Any] = {
        "model": model,
        "messages": messages or [{"role": "user", "content": "hi"}],
    }
    if extra:
        body.update(extra)
    return json.dumps(body).encode()


def signed_headers(
    raw_body: bytes,
    payment_id: str = "42",
    key: str = BUYER_KEY,
    path: str = "/v1/chat/completions",
    method: str = "POST",
) -> dict[str, str]:
    msg = f"{method}|{path}|{hashlib.sha256(raw_body).hexdigest()}|{payment_id}"
    signature = Account.from_key(key).sign_message(
        encode_defunct(text=msg)
    ).signature.hex()
    return {
        "X-Payment-Id": payment_id,
        "X-Signature": signature,
        "Content-Type": "application/json",
    }


def post_chat(
    client: TestClient,
    raw_body: bytes,
    payment_id: str = "42",
    key: str = BUYER_KEY,
    headers: dict[str, str] | None = None,
) -> Any:
    hdrs = signed_headers(raw_body, payment_id, key) if headers is None else headers
    return client.post("/v1/chat/completions", content=raw_body, headers=hdrs)
