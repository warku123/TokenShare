"""M15 R2 — shared buyer flow routing tests (LOCAL; all external effects
mocked: chain replaced by an in-process fake carrying the shared-signer
account, upstream forwarding intercepted by httpx.MockTransport).

Frozen semantics under test:
  * auth faces byte-identical to single (bearer tsk1 OR legacy
    X-Payment-Id + X-Signature), but the ECONOMIC SELLER is payment.seller;
  * three fail-closed preflight gates BEFORE any upstream quota —
    424 SELLER_KEY_MISSING / LISTING_NOT_BOUND / DELEGATE_NOT_AUTHORIZED,
    each carrying an X-Error-Code response header;
  * the upstream credential is the SELECTED seller's custody key, sent
    PER-REQUEST (client-level headers never mutated — concurrent A/B
    calls cannot leak A→B);
  * the model must be listed on-chain for the ACTUAL seller AND servable
    per that seller's stored catalog; pricing follows the actual seller.
"""

from __future__ import annotations

import json
import threading
from typing import Any

import httpx
import pytest
from eth_account import Account
from eth_account.messages import encode_defunct
from fastapi.testclient import TestClient

from . import custody_utils as cu
from .conftest import (
    BUYER,
    CHAIN_ID,
    FROZEN,
    chat_body,
    mint_api_key,
    bearer_headers,
    signed_headers,
)
import relay.app.main as main
from relay.app.pricing import Prices
from relay.app.receipt import decode_x_receipt

# ---------------------------------------------------------------------------
# Two synthetic custody sellers (different keys/hosts/models/prices).
# ---------------------------------------------------------------------------
SELLER_A_KEY = "0x" + "a1" * 32
SELLER_A = Account.from_key(SELLER_A_KEY).address
SELLER_B_KEY = "0x" + "b2" * 32
SELLER_B = Account.from_key(SELLER_B_KEY).address

UPSTREAM_A = "https://api.kimi.com/coding"  # official, provider moonshot
UPSTREAM_B = "https://opencode.ai/zen"  # official, provider zen
MODEL_A = "kimi-k2-instruct"
MODEL_B = "glm-4.7"

CREDENTIAL_A = "sk-custody-seller-A-0000000000"
CREDENTIAL_B = "sk-custody-seller-B-0000000000"

# Per-seller prices (deliberately DIFFERENT — pricing follows the actual
# seller, never a globally configured face).
PRICES_A = (25_000, 50_000, 100_000)
PRICES_B = (10_000, 20_000, 40_000)

USAGE_A = {"prompt_tokens": 1000, "prompt_tokens_details": {"cached_tokens": 200}, "completion_tokens": 1000}
USAGE_B = {"prompt_tokens": 3000, "prompt_tokens_details": {"cached_tokens": 0}, "completion_tokens": 500}

SIGNER = cu.SIGNER_ADDR  # the shared signer (relay account in shared mode)

# Recorded upstream requests: (url, Authorization) — cleared per test.
SEEN: list[tuple[str, str | None]] = []


def _respond_chat(request: httpx.Request, usage: dict[str, Any]) -> httpx.Response:
    if request.url.path.endswith("/v1/models"):
        return httpx.Response(200, json={"data": [{"id": MODEL_A}, {"id": MODEL_B}]})
    body = json.loads(request.read() or b"{}")
    if body.get("stream"):
        return httpx.Response(
            200,
            headers={"content-type": "text/event-stream"},
            content=(
                'data: {"id":"c1","choices":[{"delta":{"content":"hi"}}]}\n\n'
                f'data: {{"id":"c1","choices":[],"usage":{json.dumps(usage)}}}\n\n'
                "data: [DONE]\n\n"
            ).encode(),
        )
    return httpx.Response(
        200,
        json={"id": "cmpl-1", "choices": [{"message": {"role": "assistant", "content": "hi"}}], "usage": usage},
    )


def upstream_handler(request: httpx.Request) -> httpx.Response:
    """Hard host/credential binding: seller A's key is ONLY accepted on
    api.kimi.com, seller B's ONLY on opencode.ai — any leak (A key on B's
    host or vice versa) is an upstream 401 and gets recorded."""
    auth = request.headers.get("Authorization")
    SEEN.append((str(request.url), auth))
    if request.url.host == "api.kimi.com" and auth == f"Bearer {CREDENTIAL_A}":
        return _respond_chat(request, USAGE_A)
    if request.url.host == "opencode.ai" and auth == f"Bearer {CREDENTIAL_B}":
        return _respond_chat(request, USAGE_B)
    return httpx.Response(401, json={"error": "key/host mismatch"})


# ---------------------------------------------------------------------------
# In-process fake chain — full buyer-flow surface, account = the SHARED
# signer (constructed with config.seller_key by the relay).
# ---------------------------------------------------------------------------
class FakeSharedBuyerChain:
    payments: dict[int, dict[str, Any]] = {}
    listings: dict[str, dict[str, Any]] = {}  # lower operator → listing
    prices: dict[tuple[str, str], tuple[int, int, int]] = {}
    # lower seller → checksummed delegate | ZERO_ADDR (none) | "UNAVAILABLE"
    # (the deployed Escrow lacks the function — e.g. current-chain v3.1)
    delegates: dict[str, Any] = {}
    valid: bool = True
    settle_calls: list[dict[str, Any]] = []
    settle_partial_calls: list[dict[str, Any]] = []
    captured: dict[int, int] = {}
    captured_fails: bool = False

    def __init__(self, *, rpc_url: str, escrow_addr: str, registry_addr: str,
                 seller_key: str, chain_id: int) -> None:
        self._account = Account.from_key(seller_key)
        self.seller_address = self._account.address
        self.escrow = _EscrowStub(self)
        self.registry = None

    @classmethod
    def reset(cls) -> None:
        cls.payments = {}
        cls.listings = {}
        cls.prices = {}
        cls.delegates = {}
        cls.valid = True
        cls.settle_calls = []
        cls.settle_partial_calls = []
        cls.captured = {}
        cls.captured_fails = False
        cls.price_gate = None
        cls.price_entered = None

    def get_payment(self, payment_id: int) -> dict[str, Any]:
        rec = type(self).payments.get(payment_id)
        if rec is None:  # Escrow face: unknown id → zeroed record, state 0
            return {"buyer": cu_zero(), "seller": cu_zero(), "maxAmount": 0, "expiresAt": 0, "state": 0}
        return dict(rec)

    def is_valid(self, payment_id: int, seller: str, min_amount: int) -> bool:
        if not type(self).valid:
            return False
        rec = type(self).payments.get(payment_id)
        return rec is not None and str(rec["seller"]).lower() == seller.lower()

    def get_listing(self, operator: str) -> dict[str, Any] | None:
        return type(self).listings.get(operator.lower())

    # S2-C1 deterministic hook: when price_gate is set, get_price signals
    # price_entered and BLOCKS until released — the test can then land a
    # signed DELETE /sellers/keys inside the awaited get_price window.
    price_gate: threading.Event | None = None
    price_entered: threading.Event | None = None

    def get_price(self, operator: str, model: str) -> Prices:
        cls = type(self)
        if cls.price_gate is not None:
            if cls.price_entered is not None:
                cls.price_entered.set()
            cls.price_gate.wait(timeout=15.0)
        triple = cls.prices.get((operator.lower(), model))
        if triple is None:
            raise main.ModelNotFound("ModelNotFound()")
        cached_in, price_input, price_output = triple
        return Prices(price_cached_in=cached_in, price_input=price_input, price_output=price_output)

    def read_settle_delegate(self, seller: str) -> str | None:
        value = type(self).delegates.get(seller.lower(), "UNAVAILABLE")
        if value == "UNAVAILABLE":
            raise RuntimeError("settleDelegateOf unavailable (Escrow v3.1 face)")
        if value == "0x0000000000000000000000000000000000000000":
            return None
        return value

    def settle(self, payment_id: int, actual_amount: int) -> dict[str, Any]:
        type(self).settle_calls.append(
            {"payment_id": payment_id, "amount": actual_amount, "sender": self._account.address}
        )
        return {"status": 1}

    def settle_partial(self, payment_id: int, amount: int) -> dict[str, Any]:
        type(self).settle_partial_calls.append(
            {"payment_id": payment_id, "amount": amount, "sender": self._account.address}
        )
        type(self).captured[payment_id] = type(self).captured.get(payment_id, 0) + amount
        return {"status": 1}

    def captured_of(self, payment_id: int) -> int:
        if type(self).captured_fails:
            raise RuntimeError("getter unavailable")
        return type(self).captured.get(payment_id, 0)


class _EscrowStub:
    def __init__(self, chain: FakeSharedBuyerChain) -> None:
        self.address = "0x" + "11" * 20
        self.functions = _EscrowFunctions(chain)


class _EscrowFunctions:
    def __init__(self, chain: FakeSharedBuyerChain) -> None:
        self._chain = chain

    def settleDelegateOf(self, seller: str) -> Any:
        return _DelegateCall(self._chain, seller)


class _DelegateCall:
    def __init__(self, chain: FakeSharedBuyerChain, seller: str) -> None:
        self._chain = chain
        self._seller = seller

    def call(self) -> Any:
        return self._chain.read_settle_delegate(self._seller)


def cu_zero() -> str:
    return "0x0000000000000000000000000000000000000000"


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------
@pytest.fixture
def shared_buyer_client(monkeypatch: pytest.MonkeyPatch, tmp_path) -> TestClient:
    """Shared-mode relay: TWO custody sellers seeded into the REAL keystore
    (persisted file), chain = FakeSharedBuyerChain, upstream = MockTransport."""
    import relay.app.main as m

    FakeSharedBuyerChain.reset()
    SEEN.clear()
    cu.setup_shared_env(monkeypatch, str(tmp_path / "keystore.json"))
    monkeypatch.setattr(m, "ChainClient", FakeSharedBuyerChain)
    monkeypatch.setattr(m, "_upstream_transport", httpx.MockTransport(upstream_handler))
    with TestClient(m.app) as client:
        # Seed via the REAL keystore API (wrap/persist/commit all real).
        m.state.keystore.set(
            SELLER_A.lower(),
            upstream_base_url=UPSTREAM_A,
            api_key=CREDENTIAL_A,
            catalog=[
                {"model": MODEL_A, "servable": True},
                {"model": MODEL_B, "servable": False},  # cross-host model
            ],
        )
        m.state.keystore.set(
            SELLER_B.lower(),
            upstream_base_url=UPSTREAM_B,
            api_key=CREDENTIAL_B,
            catalog=[
                {"model": MODEL_B, "servable": True},
                {"model": MODEL_A, "servable": False},  # cross-host model
            ],
        )
        yield client


def setup_two_seller_payments(*, max_amount: int = 1_000_000, ttl: int = 600) -> None:
    """Payments 42→seller A, 43→seller B (both paid by BUYER, Locked)."""
    FakeSharedBuyerChain.payments = {
        42: {"buyer": BUYER, "seller": SELLER_A, "maxAmount": max_amount,
             "expiresAt": int(FROZEN) + ttl, "state": 1},
        43: {"buyer": BUYER, "seller": SELLER_B, "maxAmount": max_amount,
             "expiresAt": int(FROZEN) + ttl, "state": 1},
    }
    FakeSharedBuyerChain.listings = {
        SELLER_A.lower(): {"operator": SELLER_A, "endpoint": cu.DEFAULT_ORIGIN,
                           "models": [MODEL_A], "prices": [], "active": True},
        SELLER_B.lower(): {"operator": SELLER_B, "endpoint": cu.DEFAULT_ORIGIN,
                           "models": [MODEL_B], "prices": [], "active": True},
    }
    FakeSharedBuyerChain.prices = {
        (SELLER_A.lower(), MODEL_A): PRICES_A,
        (SELLER_B.lower(), MODEL_B): PRICES_B,
    }
    FakeSharedBuyerChain.delegates = {
        SELLER_A.lower(): SIGNER,
        SELLER_B.lower(): SIGNER,
    }


# ---------------------------------------------------------------------------
# Happy paths — receipt.seller = the ACTUAL seller (A), all four faces
# ---------------------------------------------------------------------------
def test_shared_legacy_nonstream_receipt_seller_a(shared_buyer_client: TestClient) -> None:
    setup_two_seller_payments()
    raw = chat_body(model=MODEL_A)
    resp = shared_buyer_client.post("/v1/chat/completions", content=raw, headers=signed_headers(raw, "42"))
    assert resp.status_code == 200, resp.text
    assert resp.headers["X-Settle-Status"] == "settled"
    receipt = decode_x_receipt(resp.headers["X-Receipt"])
    assert receipt["message"]["seller"] == SELLER_A  # checksummed actual seller
    assert receipt["message"]["upstreamHost"] == "api.kimi.com"  # SELLER'S host
    assert receipt["message"]["model"] == MODEL_A
    # Forwarded to the SELLER'S canonical URL with the SELLER'S credential.
    assert SEEN[0][0] == f"{UPSTREAM_A}/v1/chat/completions"
    assert SEEN[0][1] == f"Bearer {CREDENTIAL_A}"
    # settle tx sender = shared signer, credited seller via chain (no redirect).
    assert FakeSharedBuyerChain.settle_calls[0]["sender"].lower() == SIGNER.lower()
    assert FakeSharedBuyerChain.settle_calls[0]["sender"].lower() != SELLER_A.lower()


def test_shared_bearer_nonstream_receipt_seller_a(shared_buyer_client: TestClient) -> None:
    setup_two_seller_payments()
    api_key = mint_api_key(payment_id=42, max_amount=1_000_000)
    raw = chat_body(model=MODEL_A)
    resp = shared_buyer_client.post("/v1/chat/completions", content=raw, headers=bearer_headers(api_key))
    assert resp.status_code == 200, resp.text
    assert resp.headers["X-Settle-Status"].startswith("partial-flush")
    receipt = decode_x_receipt(resp.headers["X-Receipt"])
    assert receipt["message"]["seller"] == SELLER_A
    assert receipt["message"]["upstreamHost"] == "api.kimi.com"


def test_shared_bearer_stream_receipt_seller_a(shared_buyer_client: TestClient) -> None:
    setup_two_seller_payments()
    api_key = mint_api_key(payment_id=42, max_amount=1_000_000)
    raw = chat_body(model=MODEL_A, extra={"stream": True})
    with shared_buyer_client.stream("POST", "/v1/chat/completions", content=raw, headers=bearer_headers(api_key)) as resp:
        assert resp.status_code == 200
        body = b"".join(resp.iter_bytes())
    assert b"hi" in body and b"[DONE]" in body
    receipt = shared_buyer_client.get("/receipt/42").json()
    assert receipt["message"]["seller"] == SELLER_A  # actual seller
    assert receipt["message"]["upstreamHost"] == "api.kimi.com"
    assert any("stream" in url or "/v1/chat/completions" in url for url, _ in SEEN)


def test_shared_legacy_stream_one_shot_settle_seller_a(shared_buyer_client: TestClient) -> None:
    setup_two_seller_payments()
    raw = chat_body(model=MODEL_A, extra={"stream": True})
    with shared_buyer_client.stream("POST", "/v1/chat/completions", content=raw, headers=signed_headers(raw, "42")) as resp:
        assert resp.status_code == 200
        b"".join(resp.iter_bytes())
    receipt = shared_buyer_client.get("/receipt/42").json()
    assert receipt["message"]["seller"] == SELLER_A
    assert FakeSharedBuyerChain.settle_calls and FakeSharedBuyerChain.settle_calls[0]["sender"].lower() == SIGNER.lower()


# ---------------------------------------------------------------------------
# Preflight gates — 424 + X-Error-Code, ZERO upstream requests
# ---------------------------------------------------------------------------
def test_preflight_missing_key_424_no_upstream(shared_buyer_client: TestClient) -> None:
    setup_two_seller_payments()
    # A payment bound to an UNENROLLED seller.
    FakeSharedBuyerChain.payments[44] = {
        "buyer": BUYER, "seller": "0x" + "c3" * 20, "maxAmount": 1_000_000,
        "expiresAt": int(FROZEN) + 600, "state": 1,
    }
    raw = chat_body(model=MODEL_A)
    resp = shared_buyer_client.post("/v1/chat/completions", content=raw, headers=signed_headers(raw, "44"))
    assert resp.status_code == 424
    assert resp.headers["X-Error-Code"] == "SELLER_KEY_MISSING"
    assert SEEN == []  # no upstream quota consumed


def test_preflight_inactive_listing_424_no_upstream(shared_buyer_client: TestClient) -> None:
    setup_two_seller_payments()
    FakeSharedBuyerChain.listings[SELLER_A.lower()]["active"] = False
    raw = chat_body(model=MODEL_A)
    resp = shared_buyer_client.post("/v1/chat/completions", content=raw, headers=signed_headers(raw, "42"))
    assert resp.status_code == 424
    assert resp.headers["X-Error-Code"] == "LISTING_NOT_BOUND"
    assert SEEN == []


def test_preflight_listing_endpoint_mismatch_424_no_upstream(
    shared_buyer_client: TestClient,
) -> None:
    setup_two_seller_payments()
    FakeSharedBuyerChain.listings[SELLER_A.lower()]["endpoint"] = "https://other-relay.example/"
    raw = chat_body(model=MODEL_A)
    resp = shared_buyer_client.post("/v1/chat/completions", content=raw, headers=signed_headers(raw, "42"))
    assert resp.status_code == 424
    assert resp.headers["X-Error-Code"] == "LISTING_NOT_BOUND"
    assert SEEN == []


def test_preflight_delegate_zero_424_no_upstream(shared_buyer_client: TestClient) -> None:
    setup_two_seller_payments()
    FakeSharedBuyerChain.delegates[SELLER_A.lower()] = "0x0000000000000000000000000000000000000000"
    raw = chat_body(model=MODEL_A)
    resp = shared_buyer_client.post("/v1/chat/completions", content=raw, headers=signed_headers(raw, "42"))
    assert resp.status_code == 424
    assert resp.headers["X-Error-Code"] == "DELEGATE_NOT_AUTHORIZED"
    assert SEEN == []


def test_preflight_delegate_other_address_424_no_upstream(
    shared_buyer_client: TestClient,
) -> None:
    setup_two_seller_payments()
    FakeSharedBuyerChain.delegates[SELLER_A.lower()] = "0x" + "d4" * 20
    raw = chat_body(model=MODEL_A)
    resp = shared_buyer_client.post("/v1/chat/completions", content=raw, headers=signed_headers(raw, "42"))
    assert resp.status_code == 424
    assert resp.headers["X-Error-Code"] == "DELEGATE_NOT_AUTHORIZED"
    assert SEEN == []


def test_preflight_delegate_read_unavailable_424_v31(
    shared_buyer_client: TestClient,
) -> None:
    """Current-chain v3.1: no settleDelegateOf → the read raises → 424
    DELEGATE_NOT_AUTHORIZED with a CLEAR fail-closed face (shared cannot
    be served on v3.1; never claimed live-working)."""
    setup_two_seller_payments()
    FakeSharedBuyerChain.delegates[SELLER_A.lower()] = "UNAVAILABLE"
    raw = chat_body(model=MODEL_A)
    resp = shared_buyer_client.post("/v1/chat/completions", content=raw, headers=signed_headers(raw, "42"))
    assert resp.status_code == 424
    assert resp.headers["X-Error-Code"] == "DELEGATE_NOT_AUTHORIZED"
    assert SEEN == []


# ---------------------------------------------------------------------------
# Credential / host isolation
# ---------------------------------------------------------------------------
def test_payment_bound_a_never_touches_b_credential(
    shared_buyer_client: TestClient,
) -> None:
    setup_two_seller_payments()
    raw = chat_body(model=MODEL_A)
    resp = shared_buyer_client.post("/v1/chat/completions", content=raw, headers=signed_headers(raw, "42"))
    assert resp.status_code == 200
    urls = [url for url, _ in SEEN]
    assert urls and all(url.startswith(UPSTREAM_A) for url in urls)  # never B's host
    auths = [a for _, a in SEEN]
    assert auths and all(a == f"Bearer {CREDENTIAL_A}" for a in auths)  # never B's key


# ---------------------------------------------------------------------------
# S2-C2: malformed ports in the shared preflight — a chain listing endpoint
# (or a stored custody upstream) with an out-of-range/non-numeric port must
# fail CLOSED with the mapped 424 faces (NEVER 500, never probed).
# ---------------------------------------------------------------------------
def test_preflight_malformed_listing_endpoint_port_424(
    shared_buyer_client: TestClient,
) -> None:
    setup_two_seller_payments()
    FakeSharedBuyerChain.listings[SELLER_A.lower()]["endpoint"] = "https://api.kimi.com:notaport"
    raw = chat_body(model=MODEL_A)
    resp = shared_buyer_client.post("/v1/chat/completions", content=raw, headers=signed_headers(raw, "42"))
    assert resp.status_code == 424, resp.text
    assert resp.headers["X-Error-Code"] == "LISTING_NOT_BOUND"
    assert SEEN == []


def test_preflight_malformed_listing_endpoint_port_out_of_range_424(
    shared_buyer_client: TestClient,
) -> None:
    setup_two_seller_payments()
    FakeSharedBuyerChain.listings[SELLER_A.lower()]["endpoint"] = "https://api.kimi.com:70000"
    raw = chat_body(model=MODEL_A)
    resp = shared_buyer_client.post("/v1/chat/completions", content=raw, headers=signed_headers(raw, "42"))
    assert resp.status_code == 424, resp.text
    assert resp.headers["X-Error-Code"] == "LISTING_NOT_BOUND"
    assert SEEN == []


def test_preflight_malformed_stored_upstream_port_424_key_missing(
    shared_buyer_client: TestClient,
) -> None:
    """A stored custody upstream with a malformed port fails the preflight's
    stored-upstream re-validation → 424 SELLER_KEY_MISSING (the entry is
    unusable), never 500, never probed."""
    import relay.app.main as m

    setup_two_seller_payments()
    m.state.keystore.set(
        SELLER_A.lower(),
        upstream_base_url="https://api.kimi.com:70000",
        api_key=CREDENTIAL_A,
        catalog=[{"model": MODEL_A, "servable": True}],
    )
    raw = chat_body(model=MODEL_A)
    resp = shared_buyer_client.post("/v1/chat/completions", content=raw, headers=signed_headers(raw, "42"))
    assert resp.status_code == 424, resp.text
    assert resp.headers["X-Error-Code"] == "SELLER_KEY_MISSING"
    assert SEEN == []


def test_concurrent_a_b_credential_isolation(shared_buyer_client: TestClient) -> None:
    """Concurrent calls for payments bound to A and B: every upstream request
    carries EXACTLY its own seller's credential on its own host — no A→B
    leak through the shared client's headers."""
    setup_two_seller_payments()
    barrier = threading.Barrier(2)
    results: dict[str, int] = {}

    def call(payment_id: str, model: str, key: str) -> None:
        barrier.wait()
        raw = chat_body(model=model)
        if key == "legacy":
            headers = signed_headers(raw, payment_id)
        else:
            headers = bearer_headers(mint_api_key(payment_id=int(payment_id), max_amount=1_000_000))
        resp = shared_buyer_client.post("/v1/chat/completions", content=raw, headers=headers)
        results[payment_id] = resp.status_code

    t1 = threading.Thread(target=call, args=("42", MODEL_A, "legacy"))
    t2 = threading.Thread(target=call, args=("43", MODEL_B, "bearer"))
    t1.start()
    t2.start()
    t1.join()
    t2.join()
    assert results == {"42": 200, "43": 200}
    # Every recorded upstream hit is correctly bound: kimi↔A key, zen↔B key.
    for url, auth in SEEN:
        if url.startswith(UPSTREAM_A):
            assert auth == f"Bearer {CREDENTIAL_A}"
        elif url.startswith(UPSTREAM_B):
            assert auth == f"Bearer {CREDENTIAL_B}"
        else:  # pragma: no cover
            pytest.fail(f"unexpected upstream host: {url}")
    assert {url.split("/")[2] for url, _ in SEEN} == {"api.kimi.com", "opencode.ai"}


# ---------------------------------------------------------------------------
# Model gates + per-seller pricing
# ---------------------------------------------------------------------------
# ---------------------------------------------------------------------------
# S2-C1: revoke-during-await race — a signed DELETE /sellers/keys landing
# INSIDE the awaited get_price (between preflight and forward) must surface
# as 424 + X-Error-Code SELLER_KEY_MISSING with ZERO upstream calls and ZERO
# settlement — on BOTH the legacy and the bearer path. Deterministic via the
# fake chain's get_price gate (entered_price / price_gate events).
# ---------------------------------------------------------------------------


def _race_chat_in_thread(client: TestClient, raw: bytes, headers: dict[str, str],
                         results: dict[str, Any], key: str) -> threading.Thread:
    def call() -> None:
        results[key] = client.post("/v1/chat/completions", content=raw, headers=headers)
    thread = threading.Thread(target=call)
    thread.start()
    return thread


def _revoke_seller_entry(client: TestClient, seller: str, key: str) -> None:
    """Signed DELETE /sellers/keys for an enrolled custody seller."""
    record = cu.fresh_record(client, seller)
    raw_del = cu.revoke_payload(record)
    msg = cu.revoke_message_for(record, seller)
    headers = cu.wallet_headers(msg, key=key, seller=seller)
    resp = client.request("DELETE", "/sellers/keys", content=raw_del, headers=headers)
    assert resp.status_code == 200 and resp.json()["stored"] is False, resp.text


def _assert_race_424(results: dict[str, Any], key: str) -> None:
    resp = results[key]
    assert resp.status_code == 424, (resp.status_code, resp.text[:300])
    assert resp.headers["X-Error-Code"] == "SELLER_KEY_MISSING"
    assert resp.json()["detail"] == "SELLER_KEY_MISSING"
    assert SEEN == []  # ZERO upstream quota
    assert FakeSharedBuyerChain.settle_calls == []  # ZERO settlement
    assert FakeSharedBuyerChain.settle_partial_calls == []


def test_race_legacy_delete_during_get_price_424_no_upstream(
    shared_buyer_client: TestClient,
) -> None:
    setup_two_seller_payments()
    FakeSharedBuyerChain.price_gate = threading.Event()
    FakeSharedBuyerChain.price_entered = threading.Event()
    raw = chat_body(model=MODEL_A)
    results: dict[str, Any] = {}
    thread = _race_chat_in_thread(
        shared_buyer_client, raw, signed_headers(raw, "42"), results, "legacy")
    assert FakeSharedBuyerChain.price_entered.wait(timeout=10.0)
    # The signed revoke lands INSIDE the awaited get_price window.
    _revoke_seller_entry(shared_buyer_client, SELLER_A, SELLER_A_KEY)
    FakeSharedBuyerChain.price_gate.set()
    thread.join(timeout=30.0)
    assert not thread.is_alive()
    _assert_race_424(results, "legacy")


def test_race_bearer_delete_during_get_price_424_no_upstream(
    shared_buyer_client: TestClient,
) -> None:
    setup_two_seller_payments()
    FakeSharedBuyerChain.price_gate = threading.Event()
    FakeSharedBuyerChain.price_entered = threading.Event()
    api_key = mint_api_key(payment_id=42, max_amount=1_000_000)
    raw = chat_body(model=MODEL_A)
    results: dict[str, Any] = {}
    thread = _race_chat_in_thread(
        shared_buyer_client, raw, bearer_headers(api_key), results, "bearer")
    assert FakeSharedBuyerChain.price_entered.wait(timeout=10.0)
    _revoke_seller_entry(shared_buyer_client, SELLER_A, SELLER_A_KEY)
    FakeSharedBuyerChain.price_gate.set()
    thread.join(timeout=30.0)
    assert not thread.is_alive()
    _assert_race_424(results, "bearer")


def test_unlisted_model_refused(shared_buyer_client: TestClient) -> None:
    setup_two_seller_payments()
    raw = chat_body(model=MODEL_B)  # not in A's listing
    resp = shared_buyer_client.post("/v1/chat/completions", content=raw, headers=signed_headers(raw, "42"))
    assert resp.status_code == 400
    assert "not in listing.models" in resp.json()["detail"]
    assert SEEN == []


def test_unservable_model_refused(shared_buyer_client: TestClient) -> None:
    """LISTED and PRICED but NOT SERVABLE: MODEL_B is registered on A's
    Registry listing AND carries a chain price for seller A, yet A's STORED
    custody catalog marks it servable=False (cross-host model) — the relay
    must refuse with the distinct servability face BEFORE any upstream
    quota. (The distinct UNLISTED-model face is covered separately by
    test_unlisted_model_refused; an unpriced model is covered by the
    ModelNotFound mapping face in the same path.)"""
    setup_two_seller_payments()
    # A's Registry listing now carries the cross-host model...
    FakeSharedBuyerChain.listings[SELLER_A.lower()]["models"] = [MODEL_A, MODEL_B]
    # ...AND it is PRICED for seller A on-chain (otherwise the price lookup
    # would refuse first with the ModelNotFound "model not in listing.models"
    # mapping face — the pricing gate precedes the servability gate).
    FakeSharedBuyerChain.prices[(SELLER_A.lower(), MODEL_B)] = PRICES_A
    raw = chat_body(model=MODEL_B)
    resp = shared_buyer_client.post("/v1/chat/completions", content=raw, headers=signed_headers(raw, "42"))
    assert resp.status_code == 400
    detail = resp.json()["detail"]
    assert "not servable" in detail, detail  # the intended servability face
    assert SEEN == []


def test_price_follows_actual_seller(shared_buyer_client: TestClient) -> None:
    """B's payment serves glm-4.7 at B's OWN prices — settle math uses the
    actual seller's Registry price, never a globally configured one."""
    setup_two_seller_payments()
    raw = chat_body(model=MODEL_B)
    resp = shared_buyer_client.post("/v1/chat/completions", content=raw, headers=signed_headers(raw, "43"))
    assert resp.status_code == 200
    # B's price triple: (0*10k + 3000*20k + 500*40k)//1e6 = 80 — provably
    # not A's face ((0*25k + 3000*50k + 500*100k)//1e6 = 200).
    receipt = decode_x_receipt(resp.headers["X-Receipt"])
    assert receipt["message"]["actualAmount"] == 80
    assert receipt["message"]["seller"] == SELLER_B


# ---------------------------------------------------------------------------
# Custody ceremony → buyer flow (enrollment stays prelisting-allowed)
# ---------------------------------------------------------------------------
def test_enrollment_then_buyer_flow_e2e(
    monkeypatch: pytest.MonkeyPatch, tmp_path, mock_openai
) -> None:
    """Full R1 custody ceremony (envelope enrollment against the mocked
    official upstream) followed by a REAL shared buyer call — the enrolled
    catalog/credential serves the buyer."""
    import relay.app.custody as custody
    import relay.app.main as m

    FakeSharedBuyerChain.reset()
    SEEN.clear()
    cu.setup_shared_env(
        monkeypatch,
        str(tmp_path / "keystore.json"),
        cors_origins=cu.DEFAULT_ORIGIN,
    )
    monkeypatch.setenv("SHARED_UPLOAD_KEY", _vector_upload_key())
    monkeypatch.setattr(m, "ChainClient", FakeSharedBuyerChain)
    monkeypatch.setattr(m, "_upstream_transport", httpx.MockTransport(upstream_handler))
    monkeypatch.setattr(custody, "_probe_transport", httpx.MockTransport(upstream_handler))
    with TestClient(m.app) as client:
        # 1. custody nonce
        nonce = client.get(f"/sellers/nonce/{SELLER_A.lower()}").json()
        record = {"nonce": nonce["nonce"], "issued_at": nonce["issued_at"], "expires_at": nonce["expires_at"]}
        # 2. canonical body + envelope (encrypt with the relay's upload key)
        aad = custody.build_upload_aad(
            seller=SELLER_A.lower(), chain_id=CHAIN_ID,
            escrow_addr=cu.ESCROW_ADDR, registry_addr=cu.REGISTRY_ADDR,
            origin=cu.DEFAULT_ORIGIN, upstream_base_url=UPSTREAM_A,
            nonce=record["nonce"], issued=record["issued_at"], expires=record["expires_at"],
        )
        envelope = custody.encrypt_envelope(
            int(_vector_upload_key(), 16),
            json.dumps({"api_key": CREDENTIAL_A}, separators=(",", ":")).encode("ascii"),
            aad,
        )
        body_obj = {
            "nonce": record["nonce"],
            "issued_at": record["issued_at"],
            "expires_at": record["expires_at"],
            "upstream_base_url": UPSTREAM_A,
            "envelope": envelope,
        }
        raw = json.dumps(body_obj, separators=(",", ":"), ensure_ascii=True).encode("ascii")
        # 3. sign the EXACT submit message (relay domain from the NONCE
        #    response — the server's own origin)
        msg = custody.build_submit_message(
            seller=SELLER_A.lower(), chain_id=CHAIN_ID,
            escrow_addr=cu.ESCROW_ADDR, registry_addr=cu.REGISTRY_ADDR,
            origin=cu.DEFAULT_ORIGIN, upstream_base_url=UPSTREAM_A,
            nonce=record["nonce"], body_sha256=custody.sha256_hex(raw),
            issued=record["issued_at"], expires=record["expires_at"],
        )
        sig = Account.from_key(SELLER_A_KEY).sign_message(encode_defunct(text=msg)).signature.hex()
        headers = {
            "X-Tokenshare-Seller": SELLER_A.lower(),
            "X-Tokenshare-Signature": sig if sig.startswith("0x") else "0x" + sig,
            "Origin": cu.DEFAULT_ORIGIN,
        }
        enroll = client.post("/sellers/keys", content=raw, headers=headers)
        assert enroll.status_code == 200, enroll.text
        assert enroll.json()["catalog"] == [{"model": MODEL_A, "servable": True}, {"model": MODEL_B, "servable": False}]

        # 4. buyer flow — works WITHOUT any listing at enrollment time? No:
        #    the buyer flow REQUIRES the bound listing; register it now.
        setup_two_seller_payments()
        raw_chat = chat_body(model=MODEL_A)
        resp = client.post("/v1/chat/completions", content=raw_chat, headers=signed_headers(raw_chat, "42"))
        assert resp.status_code == 200, resp.text
        receipt = decode_x_receipt(resp.headers["X-Receipt"])
        assert receipt["message"]["seller"] == SELLER_A
        assert receipt["message"]["upstreamHost"] == "api.kimi.com"


def _vector_upload_key() -> str:
    import json as _json
    from pathlib import Path as _Path

    vec = _json.loads((_Path(__file__).resolve().parent / "vectors" / "custody_vector.json").read_text())
    return vec["upload_priv_hex"]
