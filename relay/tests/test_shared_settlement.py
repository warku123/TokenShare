"""M15 R2 — shared-mode settlement semantics (LOCAL, fully mocked).

Frozen invariants under test:
  * settle/settlePartial txs are sent by the SHARED SIGNER account (never
    the seller) and credit the payment's seller via the chain (no
    redirect); capturedOf recovery + cumulative per-payment ledger +
    admission budget + receipt faces are untouched;
  * receipts keep the unchanged EIP-712 domain, carry the ACTUAL economic
    seller and are signed by the shared signer key;
  * budget/admission rejects happen BEFORE any upstream quota;
  * single mode NEVER reads the delegate method (v3.1 compatibility);
  * a settle failure keeps the LLM response (never swallowed).
"""

from __future__ import annotations

from typing import Any

import httpx
import pytest
from eth_account import Account
from eth_account.messages import encode_typed_data
from fastapi.testclient import TestClient

from . import custody_utils as cu
from .conftest import (
    chat_body,
    bearer_headers,
    mint_api_key,
    post_chat,
    signed_headers,
    FakeChain,
)
from .test_shared_routing import (
    CREDENTIAL_A,
    FakeSharedBuyerChain,
    MODEL_A,
    SEEN,
    SELLER_A,
    UPSTREAM_A,
    setup_two_seller_payments,
    upstream_handler,
)
import relay.app.main as m
from relay.app.receipt import RECEIPT_TYPES, decode_x_receipt

SIGNER = cu.SIGNER_ADDR

# Shared bearer path settle math (USAGE_A × PRICES_A): (200*25k + 800*50k +
# 1000*100k) // 1e6 = 145 native units per served call.
PER_CALL = 145


@pytest.fixture
def shared_buyer_client(monkeypatch: pytest.MonkeyPatch, tmp_path) -> TestClient:
    FakeSharedBuyerChain.reset()
    SEEN.clear()
    cu.setup_shared_env(monkeypatch, str(tmp_path / "keystore.json"))
    monkeypatch.setattr(m, "ChainClient", FakeSharedBuyerChain)
    monkeypatch.setattr(m, "_upstream_transport", httpx.MockTransport(upstream_handler))
    with TestClient(m.app) as client:
        m.state.keystore.set(
            SELLER_A.lower(),
            upstream_base_url=UPSTREAM_A,
            api_key=CREDENTIAL_A,
            catalog=[{"model": MODEL_A, "servable": True}],
        )
        yield client


# ---------------------------------------------------------------------------
# TX sender + receipt identity
# ---------------------------------------------------------------------------
def test_shared_signer_is_the_tx_sender_not_seller(
    shared_buyer_client: TestClient,
) -> None:
    """The settle tx goes out from the SHARED SIGNER account; the credited
    seller is the payment's seller via the chain (settle takes no seller
    redirect — the delegate authority is the mechanism)."""
    setup_two_seller_payments()
    raw = chat_body(model=MODEL_A)
    resp = shared_buyer_client.post("/v1/chat/completions", content=raw, headers=signed_headers(raw, "42"))
    assert resp.status_code == 200
    assert len(FakeSharedBuyerChain.settle_calls) == 1
    tx = FakeSharedBuyerChain.settle_calls[0]
    assert tx["sender"].lower() == SIGNER.lower()  # shared signer
    assert tx["sender"].lower() != SELLER_A.lower()  # never the seller
    assert tx["payment_id"] == 42 and tx["amount"] > 0


def _receipt_signer(receipt: dict[str, Any]) -> str:
    encoded = encode_typed_data(
        full_message={
            "types": RECEIPT_TYPES,
            "primaryType": "Receipt",
            "domain": receipt["domain"],
            "message": receipt["message"],
        }
    )
    return str(Account.recover_message(encoded, signature=receipt["signature"])).lower()


def test_receipt_signature_recovers_shared_signer(
    shared_buyer_client: TestClient,
) -> None:
    """EIP-712 domain unchanged; receipt.seller = the ACTUAL seller; the
    signature recovers the SHARED SIGNER (never the seller)."""
    setup_two_seller_payments()
    raw = chat_body(model=MODEL_A)
    resp = shared_buyer_client.post("/v1/chat/completions", content=raw, headers=signed_headers(raw, "42"))
    assert resp.status_code == 200
    stored = shared_buyer_client.get("/receipt/42").json()
    assert stored["domain"] == {"name": "TokenShare Relay", "version": "1", "chainId": 84532}
    assert stored["message"]["seller"] == SELLER_A  # actual economic seller
    assert _receipt_signer(stored) == SIGNER.lower()


def test_bearer_shared_receipt_identity(shared_buyer_client: TestClient) -> None:
    """Bearer + partial path: same receipt identity (actual seller, shared
    signer signature, unchanged domain)."""
    setup_two_seller_payments()
    api_key = mint_api_key(payment_id=42, max_amount=1_000_000)
    raw = chat_body(model=MODEL_A)
    resp = shared_buyer_client.post("/v1/chat/completions", content=raw, headers=bearer_headers(api_key))
    assert resp.status_code == 200
    stored = shared_buyer_client.get("/receipt/42").json()
    assert stored["message"]["seller"] == SELLER_A
    assert _receipt_signer(stored) == SIGNER.lower()


# ---------------------------------------------------------------------------
# Budget admission BEFORE quota; cumulative capture; recovery
# ---------------------------------------------------------------------------
def test_bearer_budget_reject_before_upstream_quota(
    shared_buyer_client: TestClient,
) -> None:
    """Payment maxAmount exhausted → 402 BEFORE any upstream request; the
    chain-budget half (①) governs over an inflated self-minted grant.m."""
    setup_two_seller_payments(max_amount=10)  # 10 native units on-chain
    api_key = mint_api_key(payment_id=42, max_amount=999_999_999)  # inflated m
    raw = chat_body(model=MODEL_A)
    resp = shared_buyer_client.post("/v1/chat/completions", content=raw, headers=bearer_headers(api_key))
    assert resp.status_code == 402
    assert SEEN == []  # no upstream quota consumed


def test_cumulative_capture_and_partial_flush_shared(
    shared_buyer_client: TestClient,
) -> None:
    """Cumulative per-payment ledger over repeated shared bearer calls;
    settlePartial recorded; captured accumulates; X-Receipt per call with
    the actual seller identity."""
    setup_two_seller_payments()
    api_key = mint_api_key(payment_id=42, max_amount=1_000_000)
    for _ in range(2):
        raw = chat_body(model=MODEL_A)
        resp = shared_buyer_client.post("/v1/chat/completions", content=raw, headers=bearer_headers(api_key))
        assert resp.status_code == 200, resp.text
        receipt = decode_x_receipt(resp.headers["X-Receipt"])
        assert receipt["message"]["seller"] == SELLER_A
    partials = FakeSharedBuyerChain.settle_partial_calls
    assert partials and all(c["sender"].lower() == SIGNER.lower() for c in partials)
    total_flushed = sum(c["amount"] for c in partials)
    assert FakeSharedBuyerChain.captured[42] == total_flushed
    assert total_flushed >= 2 * PER_CALL or total_flushed >= PER_CALL  # cumulative growth
    assert total_flushed == 2 * PER_CALL  # both calls captured fully


def test_captured_recovery_after_restart_shared(
    shared_buyer_client: TestClient,
) -> None:
    """Restart honesty (SEC1-3): a fresh relay process (empty ledger) seeds
    captured from the CHAIN (capturedOf) and refuses an over-budget call —
    no reset-to-0 over-admission, no upstream quota consumed."""
    setup_two_seller_payments()
    api_key = mint_api_key(payment_id=42, max_amount=1_000_000)
    raw = chat_body(model=MODEL_A)
    first = shared_buyer_client.post("/v1/chat/completions", content=raw, headers=bearer_headers(api_key))
    assert first.status_code == 200
    assert FakeSharedBuyerChain.captured.get(42, 0) == PER_CALL

    # Simulate a restart: fresh state (new empty ledger), SAME chain state
    # (now fully captured on-chain).
    with TestClient(m.app) as client2:
        FakeSharedBuyerChain.captured[42] = 1_000_000  # fully captured on-chain
        served_before = len(SEEN)
        resp = client2.post("/v1/chat/completions", content=raw, headers=bearer_headers(api_key))
        assert resp.status_code == 402  # real accumulated total → refuse
        assert len(SEEN) == served_before  # no upstream quota consumed


# ---------------------------------------------------------------------------
# Single-mode v3.1 compatibility
# ---------------------------------------------------------------------------
def test_single_mode_never_reads_delegate(client: TestClient, monkeypatch: pytest.MonkeyPatch) -> None:
    """Single-mode chat flow works on a v3.1 chain and NEVER invokes the
    delegate read; signature identity exactly old (receipt signed by the
    relay's OWN seller key)."""
    from .conftest import SELLER

    called: list[str] = []

    def _boom(self, seller: str) -> None:
        called.append(seller)
        raise AssertionError("single mode must never read settleDelegateOf")

    monkeypatch.setattr(FakeChain, "read_settle_delegate", _boom, raising=False)
    raw = chat_body()
    resp = post_chat(client, raw)
    assert resp.status_code == 200
    assert resp.headers["X-Settle-Status"] == "settled"
    assert called == []
    receipt = decode_x_receipt(resp.headers["X-Receipt"])
    assert receipt["message"]["seller"] == SELLER  # the relay's own seller
    assert _receipt_signer(receipt) == SELLER.lower()  # signed by the seller key


def test_settle_failure_keeps_response_shared(
    shared_buyer_client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A settle failure NEVER swallows the LLM response (X-Settle-Status:
    settle-failed; buyer can refund after ttl) — shared face identical to
    single."""

    def failing_settle(self, payment_id: int, actual_amount: int) -> dict[str, Any]:
        raise RuntimeError("rpc down")

    monkeypatch.setattr(FakeSharedBuyerChain, "settle", failing_settle)
    setup_two_seller_payments()
    raw = chat_body(model=MODEL_A)
    resp = shared_buyer_client.post("/v1/chat/completions", content=raw, headers=signed_headers(raw, "42"))
    assert resp.status_code == 200  # response preserved
    assert resp.headers["X-Settle-Status"] == "settle-failed"
    assert "X-Receipt" not in resp.headers
