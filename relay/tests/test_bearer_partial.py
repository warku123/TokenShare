"""M13 bearer api-key auth (B) + partial settle orchestration (D).

Covers: bearer decode positive/negative (bad b64 / bad signature / expired /
non-buyer), dual-path coexistence (legacy untouched, bearer wins when both
present), accumulator clamping (min(actual, maxAmount-captured), dust
absorbed), flush threshold ×0.9 + TTL-window (synchronous) vs fire-and-forget,
the settlePartial call shape, and the GET /payment/{id}/usage view.
"""

from __future__ import annotations

import base64
import json
from typing import Any

from relay.app.chain import _encode_settle_partial_calldata

from .conftest import (
    ACTUAL,
    BUYER,
    BUYER_KEY,
    FROZEN,
    SELLER,
    SELLER_KEY,
    bearer_headers,
    chat_body,
    mint_api_key,
    post_chat,
    signed_headers,
    wait_until,
)


def _post_bearer(client: Any, raw_body: bytes, api_key: str) -> Any:
    return client.post("/v1/chat/completions", content=raw_body, headers=bearer_headers(api_key))


# ------------------------------------------------------- bearer decode (B)


def test_bearer_happy_path_background_flush(client: Any, fake_chain: Any) -> None:
    """Default budget (1_000_000): 315 is far below ×0.9 → fire-and-forget
    flush; X-Receipt issued with this call's actualAmount; legacy settle is
    never touched."""
    r = _post_bearer(client, chat_body(), mint_api_key())
    assert r.status_code == 200
    assert r.json()["choices"][0]["message"]["content"] == "hi"
    assert r.headers["X-Settle-Status"] == "partial-flush-pending"

    receipt = json.loads(
        base64.urlsafe_b64decode(r.headers["X-Receipt"] + "=" * (-len(r.headers["X-Receipt"]) % 4))
    )
    assert receipt["message"]["actualAmount"] == ACTUAL

    assert wait_until(lambda: fake_chain.settle_partial_calls == [(42, ACTUAL)])
    assert fake_chain.settle_calls == []  # legacy settle path untouched


def test_bearer_bad_base64_payload_401(client: Any, fake_chain: Any) -> None:
    for bad in ("tsk1.!!!.QUFB", "tsk1.eyJ9.!!!", "not-a-key", ""):
        r = _post_bearer(client, chat_body(), bad)
        assert r.status_code == 401, bad
        assert fake_chain.payment_reads == 0  # 401 decided pre-chain


def test_bearer_bad_signature_401(client: Any, fake_chain: Any) -> None:
    key = mint_api_key()
    parts = key.split(".")
    padding = "=" * (-len(parts[2]) % 4)
    sig = base64.urlsafe_b64decode(parts[2] + padding).decode("ascii")
    tampered = ("0x1" + sig[3:]) if sig[2] == "0" else ("0x0" + sig[3:])
    broken = "tsk1." + parts[1] + "." + base64.urlsafe_b64encode(tampered.encode()).decode().rstrip("=")
    r = _post_bearer(client, chat_body(), broken)
    assert r.status_code == 401
    assert fake_chain.settle_partial_calls == []


def test_bearer_expired_401(client: Any, fake_chain: Any) -> None:
    """Hard expiry: now >= expiry → 401 (both strictly-past and boundary)."""
    for expiry in (int(FROZEN) - 1, int(FROZEN)):
        r = _post_bearer(client, chat_body(), mint_api_key(expiry=expiry))
        assert r.status_code == 401, expiry
        assert r.json()["detail"] == "api key expired"
    # One tick of headroom passes the expiry gate (reaches the forward).
    r = _post_bearer(client, chat_body(), mint_api_key(expiry=int(FROZEN) + 1))
    assert r.status_code == 200


def test_bearer_payload_buyer_mismatch_401(client: Any, fake_chain: Any) -> None:
    """Key granted to (and signed by) someone who is NOT the payment's
    on-chain buyer → 401, zero-cost reject."""
    fake_chain.payment_buyer = BUYER
    r = _post_bearer(
        client, chat_body(), mint_api_key(buyer=SELLER, key=SELLER_KEY)
    )
    assert r.status_code == 401
    assert r.json()["detail"] == "api key buyer does not match payment buyer"


def test_bearer_wrong_signer_401(client: Any, fake_chain: Any) -> None:
    """payload.b claims the buyer but the signature is someone else's →
    recover ≠ payload.b → 401."""
    r = _post_bearer(client, chat_body(), mint_api_key(key=SELLER_KEY))
    assert r.status_code == 401
    assert r.json()["detail"] == "api key signer is not the granted buyer"


def test_bearer_malformed_envelope_401(client: Any, fake_chain: Any) -> None:
    body = chat_body()
    # Wrong prefix / wrong part count / missing Authorization entirely.
    good = mint_api_key()
    parts = good.split(".")
    for bad_key in (
        "tsk2." + ".".join(parts[1:]),
        good + ".extra",  # four parts
        "tsk1." + parts[1],
        parts[1] + "." + parts[2],
    ):
        r = _post_bearer(client, body, bad_key)
        assert r.status_code == 401, bad_key
    r = client.post(
        "/v1/chat/completions", content=body, headers={"Content-Type": "application/json"}
    )
    assert r.status_code == 401
    # Non-decimal p in the payload → 401.
    payload = {"p": "abc", "e": int(FROZEN) + 600, "m": 1_000_000, "b": BUYER}
    raw = json.dumps(payload, separators=(",", ":")).encode()
    sig = base64.urlsafe_b64encode(b"0x" + b"00" * 65).decode().rstrip("=")
    bad_p = "tsk1." + base64.urlsafe_b64encode(raw).decode().rstrip("=") + "." + sig
    assert _post_bearer(client, body, bad_p).status_code == 401
    assert fake_chain.payment_reads == 0


# ------------------------------------------------------- dual auth coexistence


def test_dual_path_coexistence(client: Any, fake_chain: Any) -> None:
    """Legacy X-Payment-Id + X-Signature path unchanged; a request carrying
    BOTH legacy and bearer headers routes to the bearer path."""
    # Legacy one-shot settle still works exactly as before.
    r_legacy = post_chat(client, chat_body())
    assert r_legacy.status_code == 200
    assert r_legacy.headers["X-Settle-Status"] == "settled"
    assert fake_chain.settle_calls == [(42, ACTUAL)]
    assert fake_chain.settle_partial_calls == []

    # Both headers → bearer wins (same priority, bearer presence decides).
    body = chat_body()
    both = signed_headers(body, "43")
    both["Authorization"] = f"Bearer {mint_api_key(payment_id=43)}"
    r_both = client.post("/v1/chat/completions", content=body, headers=both)
    assert r_both.status_code == 200
    assert r_both.headers["X-Settle-Status"] == "partial-flush-pending"
    assert wait_until(lambda: fake_chain.settle_partial_calls == [(43, ACTUAL)])
    assert fake_chain.settle_calls == [(42, ACTUAL)]  # legacy only from req #1


def test_bearer_skips_ttl_margin_guard(client: Any, fake_chain: Any) -> None:
    """expiresAt - now (60s) < FORWARD_MARGIN_S (120s): legacy path would 409;
    the bearer path must NOT be margin-blocked (PIN). The TTL-window rule
    then flushes synchronously → settled."""
    fake_chain.ttl_delta = 60
    r = _post_bearer(client, chat_body(), mint_api_key())
    assert r.status_code == 200  # would be 409 on the legacy path
    assert r.headers["X-Settle-Status"] == "partial-flush-settled"
    assert fake_chain.settle_partial_calls == [(42, ACTUAL)]  # synchronous flush


# ------------------------------------------------------- accumulator + flush


def test_bearer_threshold_immediate_flush(client: Any, fake_chain: Any) -> None:
    """captured(315) >= maxAmount(350)×0.9 → immediate synchronous flush."""
    fake_chain.max_amount = 350
    r = _post_bearer(client, chat_body(), mint_api_key(max_amount=350))
    assert r.status_code == 200
    assert r.headers["X-Settle-Status"] == "partial-flush-settled"
    assert fake_chain.settle_partial_calls == [(42, ACTUAL)]


def test_bearer_accumulator_clamps_across_calls(client: Any, fake_chain: Any) -> None:
    """capture = min(actual, maxAmount - captured): 400 budget absorbs 315
    then the 85 remainder; budget exhausted → dust absorbed (no capture)."""
    fake_chain.max_amount = 400
    key = mint_api_key(max_amount=400)

    r1 = _post_bearer(client, chat_body(), key)
    assert r1.headers["X-Settle-Status"] == "partial-flush-pending"  # 315 < 360
    assert wait_until(lambda: fake_chain.settle_partial_calls == [(42, ACTUAL)])

    r2 = _post_bearer(client, chat_body(), key)
    assert r2.headers["X-Settle-Status"] == "partial-flush-settled"  # 400 ≥ 360
    assert fake_chain.settle_partial_calls == [(42, ACTUAL), (42, 85)]
    assert wait_until(lambda: fake_chain.settle_partial_calls[-1] == (42, 85))

    view = client.get("/payment/42/usage")
    assert view.status_code == 200
    assert view.json() == {
        "paymentId": 42,
        "captured": 400,
        "maxAmount": 400,
        "remaining": 0,
    }

    # Third call: budget spent → capture 0 → nothing flushable, receipt still
    # issued with the call's own actual (PIN).
    r3 = _post_bearer(client, chat_body(), key)
    assert r3.status_code == 200
    assert r3.headers["X-Settle-Status"] == "partial-flush-none"
    assert "X-Receipt" in r3.headers
    assert fake_chain.settle_partial_calls == [(42, ACTUAL), (42, 85)]


def test_bearer_maxed_budget_flushes_remainder(client: Any, fake_chain: Any) -> None:
    """maxAmount 330: call1 immediate (315 ≥ 297), call2 captures the 15
    remainder, call3 absorbs dust."""
    fake_chain.max_amount = 330
    key = mint_api_key(max_amount=330)
    r1 = _post_bearer(client, chat_body(), key)
    assert r1.headers["X-Settle-Status"] == "partial-flush-settled"
    r2 = _post_bearer(client, chat_body(), key)
    assert r2.headers["X-Settle-Status"] == "partial-flush-settled"
    assert fake_chain.settle_partial_calls == [(42, 315), (42, 15)]
    r3 = _post_bearer(client, chat_body(), key)
    assert r3.headers["X-Settle-Status"] == "partial-flush-none"
    assert fake_chain.settle_partial_calls == [(42, 315), (42, 15)]


def test_bearer_zero_usage_dust_absorbed(
    client: Any, fake_chain: Any, mock_openai: Any
) -> None:
    """Proper dust test: actual == 0 → capture 0 → partial-flush-none."""
    mock_openai.usage_override = {
        "prompt_tokens": 0,
        "prompt_tokens_details": {"cached_tokens": 0},
        "completion_tokens": 0,
    }
    r = _post_bearer(client, chat_body(), mint_api_key())
    assert r.status_code == 200
    assert r.headers["X-Settle-Status"] == "partial-flush-none"
    assert "X-Receipt" in r.headers  # receipt always issued on partial path
    receipt = json.loads(
        base64.urlsafe_b64decode(r.headers["X-Receipt"] + "=" * (-len(r.headers["X-Receipt"]) % 4))
    )
    assert receipt["message"]["actualAmount"] == 0
    assert fake_chain.settle_partial_calls == []


def test_bearer_flush_failed_keeps_response_and_receipt(
    client: Any, fake_chain: Any
) -> None:
    """Immediate flush tx failure: LLM response intact + partial-flush-failed
    + receipt STILL issued (PIN: every partial call gets one)."""
    fake_chain.max_amount = 350
    fake_chain.settle_partial_fails = True
    r = _post_bearer(client, chat_body(), mint_api_key(max_amount=350))
    assert r.status_code == 200
    assert r.json()["choices"][0]["message"]["content"] == "hi"
    assert r.headers["X-Settle-Status"] == "partial-flush-failed"
    assert "X-Receipt" in r.headers
    # Pending restored → next request retries the flush (old pending 315 +
    # new capture 35 = 350 in one settlePartial).
    fake_chain.settle_partial_fails = False
    r2 = _post_bearer(client, chat_body(), mint_api_key(max_amount=350))
    assert r2.status_code == 200
    assert fake_chain.settle_partial_calls[-1] == (42, 350)


def test_bearer_grant_max_smaller_than_chain(client: Any, fake_chain: Any) -> None:
    """Grant m < chain maxAmount: the ledger budget uses min(m, chain) so a
    flush can never exceed what settlePartial will accept."""
    fake_chain.max_amount = 1_000_000
    r = _post_bearer(client, chat_body(), mint_api_key(max_amount=200))
    assert r.status_code == 200
    assert r.headers["X-Settle-Status"] == "partial-flush-settled"  # 200 ≥ 180
    assert fake_chain.settle_partial_calls == [(42, 200)]
    assert client.get("/payment/42/usage").json() == {
        "paymentId": 42,
        "captured": 200,
        "maxAmount": 200,
        "remaining": 0,
    }


# ------------------------------------------------------- usage endpoint (4)


def test_usage_endpoint_views(client: Any, fake_chain: Any) -> None:
    """Fresh payment: chain fallback for maxAmount, captured 0. After calls:
    ledger values with remaining floored at 0."""
    fresh = client.get("/payment/42/usage")
    assert fresh.status_code == 200
    assert fresh.json() == {
        "paymentId": 42,
        "captured": 0,
        "maxAmount": 1_000_000,
        "remaining": 1_000_000,
    }
    unknown = client.get("/payment/7/usage")  # never served → chain fallback
    assert unknown.status_code == 200
    assert unknown.json()["paymentId"] == 7

    fake_chain.max_amount = 350
    _post_bearer(client, chat_body(), mint_api_key(max_amount=350))
    after = client.get("/payment/42/usage")
    assert after.json() == {
        "paymentId": 42,
        "captured": 315,
        "maxAmount": 350,
        "remaining": 35,
    }


# ------------------------------------------------------- stream partial (5)


def test_bearer_stream_partial_receipt(
    client: Any, fake_chain: Any, mock_openai: Any
) -> None:
    """Streaming bearer call: SSE passthrough, receipt in the store after the
    stream drains, threshold flush fired synchronously in the drain."""
    fake_chain.max_amount = 350
    mock_openai.mode = "stream_ok"
    r = _post_bearer(client, chat_body(extra={"stream": True}), mint_api_key(max_amount=350))
    assert r.status_code == 200
    assert "text/event-stream" in r.headers["content-type"]

    stored = client.get("/receipt/42")
    assert stored.status_code == 200
    assert stored.json()["message"]["actualAmount"] == ACTUAL
    assert fake_chain.settle_partial_calls == [(42, ACTUAL)]
    assert fake_chain.settle_calls == []


# ------------------------------------------------------- settlePartial shape (6)


def test_settle_partial_calldata_shape() -> None:
    """PIN selector `settlePartial(uint256,uint256)` = 0xc97ac54c; calldata =
    selector + two 32-byte big-endian uint256 words."""
    data = _encode_settle_partial_calldata(42, 315)
    assert data == "0xc97ac54c" + "0" * 62 + "2a" + "0" * 61 + "13b"
    assert len(data) == 2 + 8 + 64 + 64
    # Large values round-trip via big-endian words.
    data2 = _encode_settle_partial_calldata(2**100, 2**170)
    assert data2.startswith("0xc97ac54c")
    assert int(data2[10:74], 16) == 2**100
    assert int(data2[74:138], 16) == 2**170
