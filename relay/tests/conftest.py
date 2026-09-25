"""Shared fixtures for the relay test suite — zero real chain, zero real
OpenAI. Chain layer is replaced by an in-process FakeChain; the OpenAI
upstream is a local threaded HTTP server whose behavior each test selects.
"""

from __future__ import annotations

import hashlib
import base64
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

from relay.app.chain import ModelNotFound, NotActive
from relay.app.pricing import Prices

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
    # M13: on-chain payment buyer (bearer cross-check knob) + settlePartial.
    payment_buyer: str = BUYER
    settle_partial_calls: list[tuple[int, int]] = []
    settle_partial_fails: bool = False
    listing_active: bool = True
    listing_registered: bool = True
    models: list[str] = ["gpt-4o-mini", ""]
    # M9 Registry v2 per-model pricing: get_price behavior knobs.
    price_reads: int = 0
    # None | "NotActive" | "ModelNotFound" → the mapped revert face to raise.
    price_error: str | None = None
    # Per-model price overrides {model: (cachedIn, input, output)}; models
    # without an entry price at the suite-wide defaults.
    model_prices: dict[str, tuple[int, int, int]] = {}

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
        cls.payment_buyer = BUYER
        cls.settle_partial_calls = []
        cls.settle_partial_fails = False
        cls.listing_active = True
        cls.listing_registered = True
        cls.models = ["gpt-4o-mini", ""]
        cls.price_reads = 0
        cls.price_error = None
        cls.model_prices = {}

    def get_payment(self, payment_id: int) -> dict[str, Any]:
        type(self).payment_reads += 1
        return {
            "buyer": type(self).payment_buyer,
            "seller": SELLER,
            "maxAmount": type(self).max_amount,
            "expiresAt": int(FROZEN) + type(self).ttl_delta,
            "state": 1,
        }

    def is_valid(self, payment_id: int, seller: str, min_amount: int) -> bool:
        type(self).valid_calls.append((payment_id, seller, min_amount))
        return type(self).valid

    def get_listing(self, operator: str) -> dict[str, Any] | None:
        """M9 Registry v2 shape: 5 fields — models[] with a PARALLEL prices[]
        array (one Price triple per model)."""
        type(self).listing_reads += 1
        if not type(self).listing_registered:
            return None
        models = list(type(self).models)
        return {
            "operator": operator,
            "endpoint": "http://relay.example",
            "models": models,
            "prices": [
                {
                    "cachedIn": PRICE_CACHED_IN,
                    "input": PRICE_INPUT,
                    "output": PRICE_OUTPUT,
                }
                for _ in models
            ],
            "active": type(self).listing_active,
        }

    def get_price(self, operator: str, model: str) -> Prices:
        """Same surface + error faces as ChainClient.get_price (Registry v2)."""
        type(self).price_reads += 1
        error = type(self).price_error
        if error == "NotActive":
            raise NotActive("NotActive()")
        if error == "ModelNotFound":
            raise ModelNotFound("ModelNotFound()")
        cached_in, price_input, price_output = type(self).model_prices.get(
            model, (PRICE_CACHED_IN, PRICE_INPUT, PRICE_OUTPUT)
        )
        return Prices(
            price_cached_in=cached_in,
            price_input=price_input,
            price_output=price_output,
        )

    def settle(self, payment_id: int, actual_amount: int) -> dict[str, Any]:
        if type(self).settle_fails:
            raise RuntimeError("rpc down")
        type(self).settle_calls.append((payment_id, actual_amount))
        return {"status": 1}

    def settle_partial(self, payment_id: int, amount: int) -> dict[str, Any]:
        """M13-D mirror of ChainClient.settle_partial."""
        if type(self).settle_partial_fails:
            raise RuntimeError("rpc down")
        type(self).settle_partial_calls.append((payment_id, amount))
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

    def do_GET(self) -> None:
        """GET /v1/models for the verify-upstream endpoint / startup probe.
        Behavior via server.attributes: `models_status` ("ok"|"401"|"403"|
        "500"|"net_err") + `models_ids` (ids returned under data[].id)."""
        server = self.server
        with server.behavior_lock:
            status = server.models_status
            ids = list(server.models_ids)
        if not self.path.split("?")[0].rstrip("/").endswith("/v1/models"):
            self.send_response(404)
            self.end_headers()
            return
        if status == "net_err":
            # Simulate a dropped connection: no response bytes at all.
            self.close_connection = True
            try:
                self.connection.close()
            except OSError:
                pass
            return
        payload = json.dumps(
            {"object": "list", "data": [{"id": i, "object": "model"} for i in ids]}
        ).encode()
        self.send_response(200 if status == "ok" else int(status))
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(payload)))
        self.end_headers()
        self.wfile.write(payload)

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
            server.received_paths.append(self.path)
            mode = server.mode

        usage = dict(server.usage)
        if server.usage_override is not None:
            usage = dict(server.usage_override)

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
        if mode == "stream_usage_then_null":
            # Kimi semantics (high confidence): usage appears BEFORE [DONE],
            # possibly in an empty-choices chunk, and a LATER chunk may carry
            # a null usage. The relay must keep the LAST NON-null usage.
            self.wfile.write(f"data: {json.dumps(chunk)}\n\n".encode())
            first = {"id": "c1", "choices": [], "usage": dict(usage)}
            self.wfile.write(f"data: {json.dumps(first)}\n\n".encode())
            self.wfile.write(b"data: {\"id\":\"c1\",\"choices\":[],\"usage\":null}\n\n")
            self.wfile.write(b"data: {\"id\":\"c1\",\"choices\":[],\"usage\":null,"
                             b"\"finish_reason\":\"stop\"}\n\n")
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
        self.received_paths: list[str] = []
        self.mode = "ok"
        self.usage: dict[str, Any] = dict(USAGE)
        # Non-None replaces the per-test usage object (fallback-chain tests).
        self.usage_override: dict[str, Any] | None = None
        self.slow_seconds = 0.5
        # GET /v1/models behavior (verify-upstream / startup probe tests).
        self.models_status = "ok"
        self.models_ids: list[str] = ["gpt-4o-mini"]


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
    mock_openai.received_paths.clear()
    mock_openai.usage = dict(USAGE)
    mock_openai.usage_override = None
    mock_openai.slow_seconds = 0.5
    mock_openai.models_status = "ok"
    mock_openai.models_ids = ["gpt-4o-mini"]


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
    # The mock upstream host is not an official endpoint: the relay's startup
    # anti-poisoning gate would refuse it, so every test boot opts into the
    # explicit dev/test-only escape hatch (the same one the e2e runner uses).
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
        "ALLOW_CUSTOM_UPSTREAM": "1",
        # Startup key probe: the mock upstream only answers /v1/models per the
        # models_status fixture, but tests must not depend on probe ordering —
        # OFF by default in suites, enabled explicitly in the probe tests.
        "VERIFY_UPSTREAM_ON_START": "0",
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


# --------------------------------------------------------------------------
# M13 bearer api-key helpers (console-side key assembly mirror)
# --------------------------------------------------------------------------
def b64url(raw: bytes) -> str:
    return base64.urlsafe_b64encode(raw).decode("ascii").rstrip("=")


def mint_api_key(
    payment_id: int = 42,
    expiry: int | None = None,
    max_amount: int = 1_000_000,
    buyer: str = BUYER,
    key: str = BUYER_KEY,
) -> str:
    """PIN (C-lane mirror): sign `TokenShare API key
    grant|paymentId={p}|expiry={e}|maxAmount={m}` (EIP-191 text) and wrap
    `tsk1.<b64url(payload_json)>.<b64url(sig_hex)>` with
    payload={"p","e","m","b"}."""
    e = int(FROZEN) + 600 if expiry is None else expiry
    payload = {"p": payment_id, "e": e, "m": max_amount, "b": buyer}
    msg = (
        f"TokenShare API key grant|paymentId={payload['p']}"
        f"|expiry={payload['e']}|maxAmount={payload['m']}"
    )
    sig = Account.from_key(key).sign_message(encode_defunct(text=msg)).signature.hex()
    if not sig.startswith("0x"):
        sig = "0x" + sig
    return (
        "tsk1."
        + b64url(json.dumps(payload, separators=(",", ":")).encode("utf-8"))
        + "."
        + b64url(sig.encode("ascii"))
    )


def bearer_headers(api_key: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {api_key}", "Content-Type": "application/json"}


def wait_until(condition: Any, timeout: float = 5.0) -> bool:
    """Poll a background-thread condition (fire-and-forget flush) with a
    deadline; returns the last condition value."""
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if condition():
            return True
        time.sleep(0.01)
    return bool(condition())
