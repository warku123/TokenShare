"""M13 bearer api-key auth (B) + partial settle orchestration (D) + SEC1 hardening.

Covers: bearer decode positive/negative (bad b64 / bad signature / expired /
non-buyer / uint256 overflow), dual-path coexistence (legacy untouched, bearer
wins when both present), accumulator clamping (min(actual, maxAmount-captured),
dust absorbed), flush threshold ×0.9 + TTL-window (synchronous) vs
fire-and-forget, the settlePartial/capturedOf call shapes, the
GET /payment/{id}/usage view — plus SEC1-1 budget admission gates (chain
budget + buyer's grant.m self-limit, 402 with remaining/minAmount), SEC1-2
legacy fold-settle (total ≥ on-chain captured; entry cleared on success) and
SEC1-3 capturedOf seeding (restart no longer resets accrued to 0).

`client_small_caps` boots with PROMPT/COMPLETION_TOKEN_CAP=1000 → minAmount
estimate 150, so small-budget clamp/threshold scenarios (330-470 range) can
legitimately pass the SEC1-1 admission gate.
"""

from __future__ import annotations

import base64
import json
from typing import Any

import pytest
from fastapi.testclient import TestClient

from relay.app.chain import (
    _CAPTURED_OF_SELECTOR,
    _encode_captured_of_calldata,
    _encode_settle_partial_calldata,
)

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
    mock_openai_url,
    post_chat,
    setup_relay_env,
    signed_headers,
    wait_until,
)

# minAmount estimate under the small-caps fixture:
# (50_000*1000 + 100_000*1000)//1e6 = 150
SMALL_MIN_AMOUNT = 150


@pytest.fixture
def client_small_caps(monkeypatch: Any, mock_openai: Any) -> Any:
    """Relay booted with tiny token caps → minAmount 150 (see module doc)."""
    import relay.app.main as m

    setup_relay_env(monkeypatch, mock_openai_url(mock_openai))
    monkeypatch.setenv("PROMPT_TOKEN_CAP", "1000")
    monkeypatch.setenv("COMPLETION_TOKEN_CAP", "1000")
    with TestClient(m.app) as c:
        yield c


def _post_bearer(client: Any, raw_body: bytes, api_key: str) -> Any:
    return client.post("/v1/chat/completions", content=raw_body, headers=bearer_headers(api_key))


def _key_with(payload: dict[str, Any]) -> str:
    """Envelope with an arbitrary payload (dummy 65-byte sig)."""
    raw = json.dumps(payload, separators=(",", ":")).encode("utf-8")
    sig = base64.urlsafe_b64encode(b"0x" + b"00" * 65).decode("ascii").rstrip("=")
    return "tsk1." + base64.urlsafe_b64encode(raw).decode("ascii").rstrip("=") + "." + sig


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
    assert _post_bearer(
        client, body, _key_with({"p": "abc", "e": int(FROZEN) + 600, "m": 1_000_000, "b": BUYER})
    ).status_code == 401
    assert fake_chain.payment_reads == 0


def test_grant_uint256_overflow_401_not_500(client: Any, fake_chain: Any) -> None:
    """SEC1 顺手项: p/e/m beyond uint256 → 401 at the decode layer (an ABI
    to_bytes overflow downstream would surface as a 500)."""
    base = {"p": 42, "e": int(FROZEN) + 600, "m": 1_000_000, "b": BUYER}
    for field in ("p", "e", "m"):
        for huge in (2**256, 2**300):
            payload = dict(base)
            payload[field] = huge
            r = _post_bearer(client, chat_body(), _key_with(payload))
            assert r.status_code == 401, (field, huge)
    # Negative expiry likewise.
    payload = dict(base)
    payload["e"] = -5
    assert _post_bearer(client, chat_body(), _key_with(payload)).status_code == 401
    assert fake_chain.payment_reads == 0


# ------------------------------------------------------- dual auth coexistence


def test_dual_path_coexistence(client: Any, fake_chain: Any) -> None:
    """Legacy X-Payment-Id + X-Signature path unchanged; a request carrying
    BOTH legacy and bearer headers routes to the bearer path."""
    # Legacy one-shot settle still works exactly as before (no bearer history
    # → fold is a no-op: total = 0 + clamp(315) = 315).
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


def test_bearer_threshold_immediate_flush(
    client_small_caps: Any, fake_chain: Any
) -> None:
    """captured(315) >= maxAmount(350)×0.9 → immediate synchronous flush."""
    fake_chain.max_amount = 350
    r = _post_bearer(client_small_caps, chat_body(), mint_api_key(max_amount=350))
    assert r.status_code == 200  # budget gate: 350 ≥ minAmount 150
    assert r.headers["X-Settle-Status"] == "partial-flush-settled"
    assert fake_chain.settle_partial_calls == [(42, ACTUAL)]


def test_bearer_accumulator_clamps_across_calls(
    client_small_caps: Any, fake_chain: Any
) -> None:
    """capture = min(actual, budget - captured): 470 budget absorbs 315 then
    the 155 remainder (≥ minAmount so the gate admits call 2); a further call
    is REFUSED by the SEC1-1 gate (remaining 0 < 150) — no more free service."""
    fake_chain.max_amount = 470
    key = mint_api_key(max_amount=470)

    r1 = _post_bearer(client_small_caps, chat_body(), key)
    assert r1.headers["X-Settle-Status"] == "partial-flush-pending"  # 315 < 423
    assert wait_until(lambda: fake_chain.settle_partial_calls == [(42, ACTUAL)])

    r2 = _post_bearer(client_small_caps, chat_body(), key)
    assert r2.headers["X-Settle-Status"] == "partial-flush-settled"  # 470 ≥ 423
    assert fake_chain.settle_partial_calls == [(42, ACTUAL), (42, 155)]

    view = client_small_caps.get("/payment/42/usage")
    assert view.status_code == 200
    assert view.json() == {
        "paymentId": 42,
        "captured": 470,
        "maxAmount": 470,
        "remaining": 0,
        "revoked": False,
    }

    # Third call: budget spent → SEC1-1 gate refuses with remaining/minAmount.
    r3 = _post_bearer(client_small_caps, chat_body(), key)
    assert r3.status_code == 402
    detail = r3.json()["detail"]
    assert detail["remaining"] == 0
    assert detail["minAmount"] == SMALL_MIN_AMOUNT
    assert fake_chain.settle_partial_calls == [(42, ACTUAL), (42, 155)]


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


def test_bearer_flush_failed_then_legacy_fold_carries_pending(
    client_small_caps: Any, fake_chain: Any
) -> None:
    """Immediate flush tx failure: LLM response intact + partial-flush-failed
    + receipt STILL issued. The claimed pending stays folded into the ledger,
    so the next LEGACY settle carries the whole accrued total in one tx."""
    fake_chain.max_amount = 350
    fake_chain.settle_partial_fails = True
    r = _post_bearer(client_small_caps, chat_body(), mint_api_key(max_amount=350))
    assert r.status_code == 200
    assert r.json()["choices"][0]["message"]["content"] == "hi"
    assert r.headers["X-Settle-Status"] == "partial-flush-failed"
    assert "X-Receipt" in r.headers

    # Legacy settle folds captured(315) + clamp(315) → min(630, 350) = 350.
    fake_chain.settle_partial_fails = False
    r2 = post_chat(client_small_caps, chat_body())
    assert r2.status_code == 200
    assert r2.headers["X-Settle-Status"] == "settled"
    assert fake_chain.settle_calls == [(42, 350)]
    # Terminal settle cleared the ledger entry (chain captured reads 0 here).
    assert client_small_caps.get("/payment/42/usage").json()["captured"] == 0


def test_bearer_grant_max_smaller_than_chain(
    client_small_caps: Any, fake_chain: Any
) -> None:
    """Grant m < chain maxAmount: the ledger budget uses min(m, chain) so a
    flush can never exceed what settlePartial will accept."""
    fake_chain.max_amount = 1_000_000
    r = _post_bearer(client_small_caps, chat_body(), mint_api_key(max_amount=200))
    assert r.status_code == 200
    assert r.headers["X-Settle-Status"] == "partial-flush-settled"  # 200 ≥ 180
    assert fake_chain.settle_partial_calls == [(42, 200)]
    assert client_small_caps.get("/payment/42/usage").json() == {
        "paymentId": 42,
        "captured": 200,
        "maxAmount": 200,
        "remaining": 0,
        "revoked": False,
    }


# ------------------------------------------------------- SEC1-1 budget gates


def test_budget_gate_402_when_chain_budget_below_min_amount(
    client_small_caps: Any, fake_chain: Any, mock_openai: Any
) -> None:
    """① Chain budget authoritative: remaining(100) < minAmount(150) → 402
    with remaining/minAmount in the body, zero-cost (never forwarded)."""
    fake_chain.max_amount = 100
    r = _post_bearer(client_small_caps, chat_body(), mint_api_key())
    assert r.status_code == 402
    detail = r.json()["detail"]
    assert detail == {
        "error": "payment budget exhausted",
        "remaining": 100,
        "minAmount": SMALL_MIN_AMOUNT,
    }
    assert mock_openai.received == []  # never forwarded
    assert fake_chain.settle_partial_calls == []
    assert fake_chain.settle_calls == []


def test_grant_self_limit_402_and_exact_min_boundary(
    client_small_caps: Any, fake_chain: Any
) -> None:
    """② grant.m is the buyer's own tighter limit — respected: remaining
    below minAmount refuses service; remaining == minAmount passes."""
    fake_chain.max_amount = 1_000_000
    r = _post_bearer(client_small_caps, chat_body(), mint_api_key(max_amount=100))
    assert r.status_code == 402
    assert r.json()["detail"] == {
        "error": "api key limit exhausted",
        "remaining": 100,
        "minAmount": SMALL_MIN_AMOUNT,
    }
    # Boundary: m == minAmount exactly → admitted (150 not < 150).
    r2 = _post_bearer(client_small_caps, chat_body(), mint_api_key(max_amount=150))
    assert r2.status_code == 200
    assert fake_chain.settle_partial_calls == [(42, 150)]


def test_entry_budget_refreshed_per_request(
    client_small_caps: Any, fake_chain: Any
) -> None:
    """SEC1-1③: entry.max_amount is re-based from the CURRENT request
    (chain∩grant), never a first-request cache: m=1_000_000 then m=600
    re-bases the capture budget to 600."""
    r1 = _post_bearer(client_small_caps, chat_body(), mint_api_key(max_amount=1_000_000))
    assert r1.status_code == 200  # capture 315 → pending background flush
    assert wait_until(lambda: fake_chain.settle_partial_calls == [(42, ACTUAL)])

    r2 = _post_bearer(client_small_caps, chat_body(), mint_api_key(max_amount=600))
    assert r2.status_code == 200  # gates: chain 1M-315 ✓, grant 600-315=285 ✓
    # Refreshed budget 600: capture min(315, 600-315)=285 → 600 ≥ 540 → flush.
    assert fake_chain.settle_partial_calls == [(42, ACTUAL), (42, 285)]
    assert client_small_caps.get("/payment/42/usage").json() == {
        "paymentId": 42,
        "captured": 600,
        "maxAmount": 600,
        "remaining": 0,
        "revoked": False,
    }


# ------------------------------------------------------- SEC1-2 legacy fold


def test_legacy_settle_folds_bearer_captures(
    client_small_caps: Any, fake_chain: Any
) -> None:
    """SEC1-2 fix ①: after bearer captures, a legacy settle sends
    total = min(captured + clamp(actual), maxAmount) — never a plain clamp
    that would revert BelowCaptured on Escrow v2 forever."""
    fake_chain.max_amount = 350
    r = _post_bearer(client_small_caps, chat_body(), mint_api_key(max_amount=350))
    assert r.status_code == 200
    assert fake_chain.settle_partial_calls == [(42, 315)]  # immediate flush

    r2 = post_chat(client_small_caps, chat_body())
    assert r2.status_code == 200
    assert r2.headers["X-Settle-Status"] == "settled"
    assert fake_chain.settle_calls == [(42, 350)]  # min(315+315, 350)
    # Terminal settle cleared the entry.
    assert client_small_caps.get("/payment/42/usage").json()["captured"] == 0


def test_legacy_settle_failure_keeps_fold_for_retry(
    client_small_caps: Any, fake_chain: Any
) -> None:
    """Fold failure: response kept + settle-failed (PIN); entry NOT cleared
    and the claimed pending is NOT handed back to flushes — the next settle
    retry carries the full total (buyer can never be overcharged)."""
    fake_chain.max_amount = 350
    assert _post_bearer(
        client_small_caps, chat_body(), mint_api_key(max_amount=350)
    ).headers["X-Settle-Status"] == "partial-flush-settled"

    fake_chain.settle_fails = True
    r = post_chat(client_small_caps, chat_body())
    assert r.status_code == 200
    assert r.headers["X-Settle-Status"] == "settle-failed"
    # Entry kept: captured 315 still visible (pending not re-flushed).
    assert client_small_caps.get("/payment/42/usage").json()["captured"] == 315
    assert fake_chain.settle_calls == []

    fake_chain.settle_fails = False
    r2 = post_chat(client_small_caps, chat_body())
    assert r2.headers["X-Settle-Status"] == "settled"
    assert fake_chain.settle_calls == [(42, 350)]  # full total on retry


def test_legacy_settle_without_bearer_history_unchanged(
    client: Any, fake_chain: Any
) -> None:
    """Fold no-op path: no ledger entry → total = clamp(actual), byte-for-byte
    the pre-M13 legacy settle."""
    r = post_chat(client, chat_body())
    assert r.status_code == 200
    assert fake_chain.settle_calls == [(42, ACTUAL)]


# ------------------------------------------------------- SEC1-3 capturedOf seed


def test_restart_seeds_captured_from_chain(client: Any, fake_chain: Any) -> None:
    """SEC1-3: first touch after a restart seeds captured from capturedOf —
    gate and ledger work against 500, not 0."""
    fake_chain.chain_captured = 500
    r = _post_bearer(client, chat_body(), mint_api_key())
    assert r.status_code == 200  # gate: 1_000_000-500 ≥ minAmount
    assert wait_until(lambda: fake_chain.settle_partial_calls == [(42, ACTUAL)])
    view = client.get("/payment/42/usage").json()
    assert view["captured"] == 815  # seeded 500 + this call's 315
    assert view["remaining"] == 1_000_000 - 815


def test_usage_endpoint_seeds_chain_captured(client: Any, fake_chain: Any) -> None:
    """Usage view of a payment this process never served: captured comes from
    the chain (capturedOf), maxAmount falls back to getPayment."""
    fake_chain.chain_captured = 500
    v = client.get("/payment/42/usage")
    assert v.status_code == 200
    assert v.json() == {
        "paymentId": 42,
        "captured": 500,
        "maxAmount": 1_000_000,
        "remaining": 999_500,
        "revoked": False,
    }


def test_captured_of_failure_falls_back_to_zero(
    client: Any, fake_chain: Any
) -> None:
    """Getter revert / RPC failure → conservative 0 seed (logged), service
    unimpaired — seeding is hardening, never a hard dependency."""
    fake_chain.captured_of_fails = True
    v = client.get("/payment/42/usage")
    assert v.json()["captured"] == 0
    r = _post_bearer(client, chat_body(), mint_api_key())
    assert r.status_code == 200
    assert wait_until(lambda: fake_chain.settle_partial_calls == [(42, ACTUAL)])


def test_captured_of_calldata_shape() -> None:
    """SEC1-3 selector `capturedOf(uint256)` = 0xd5d177a3; calldata =
    selector + one 32-byte big-endian uint256 word."""
    data = _encode_captured_of_calldata(42)
    assert data == "0x" + _CAPTURED_OF_SELECTOR + "0" * 62 + "2a"
    assert len(data) == 2 + 8 + 64
    assert _CAPTURED_OF_SELECTOR == "d5d177a3"


# ------------------------------------------------------- usage endpoint (4)


def test_usage_endpoint_views(client_small_caps: Any, fake_chain: Any) -> None:
    """Fresh payment: chain fallback for maxAmount, captured from capturedOf
    seed. After calls: ledger values with remaining floored at 0."""
    fresh = client_small_caps.get("/payment/42/usage")
    assert fresh.status_code == 200
    assert fresh.json() == {
        "paymentId": 42,
        "captured": 0,
        "maxAmount": 1_000_000,
        "remaining": 1_000_000,
        "revoked": False,
    }
    unknown = client_small_caps.get("/payment/7/usage")  # never served
    assert unknown.status_code == 200
    assert unknown.json()["paymentId"] == 7

    fake_chain.max_amount = 350
    _post_bearer(client_small_caps, chat_body(), mint_api_key(max_amount=350))
    after = client_small_caps.get("/payment/42/usage")
    assert after.json() == {
        "paymentId": 42,
        "captured": 315,
        "maxAmount": 350,
        "remaining": 35,
        "revoked": False,
    }


# ------------------------------------------------------- stream partial (5)


def test_bearer_stream_partial_receipt(
    client_small_caps: Any, fake_chain: Any, mock_openai: Any
) -> None:
    """Streaming bearer call: SSE passthrough, receipt in the store after the
    stream drains, threshold flush fired synchronously in the drain."""
    fake_chain.max_amount = 350
    mock_openai.mode = "stream_ok"
    r = _post_bearer(
        client_small_caps, chat_body(extra={"stream": True}), mint_api_key(max_amount=350)
    )
    assert r.status_code == 200
    assert "text/event-stream" in r.headers["content-type"]

    stored = client_small_caps.get("/receipt/42")
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
