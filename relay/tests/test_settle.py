"""Settle = tiered pricing; settle amount clamped to maxAmount while the
receipt keeps the UNCLAMPED actualAmount (PIN literal, Gate-F-owned semantics);
settle tx failure keeps the LLM response and issues no receipt."""

from __future__ import annotations

import json
from typing import Any
from relay.app.receipt import decode_x_receipt

from .conftest import ACTUAL, SELLER, USAGE, chat_body, post_chat


def test_settle_equals_tiered_pricing(client: Any, fake_chain: Any) -> None:
    """Three-tier mixed usage: (400*25k + 1100*50k + 2500*100k)//1e6 = 315."""
    r = post_chat(client, chat_body())
    assert r.status_code == 200
    assert r.headers["X-Settle-Status"] == "settled"
    assert fake_chain.settle_calls == [(42, ACTUAL)]
    assert fake_chain.payment_reads == 1
    assert fake_chain.valid_calls == [(42, SELLER, 13_200)]

    # Receipt echoes the same tiered usage and the unclamped actual.
    receipt = decode_x_receipt(r.headers["X-Receipt"])
    assert receipt["message"]["promptTokens"] == USAGE["prompt_tokens"]
    assert receipt["message"]["cachedTokens"] == 400
    assert receipt["message"]["completionTokens"] == USAGE["completion_tokens"]
    assert receipt["message"]["actualAmount"] == ACTUAL


def test_settle_clamped_to_max_amount_but_receipt_unclamped(
    client: Any, fake_chain: Any
) -> None:
    """actual(315) > maxAmount(100) → settle(min)=100; receipt actualAmount
    stays the true actual (315) per PIN."""
    fake_chain.max_amount = 100
    r = post_chat(client, chat_body())
    assert r.status_code == 200
    assert r.headers["X-Settle-Status"] == "settled"
    assert fake_chain.settle_calls == [(42, 100)]
    receipt = decode_x_receipt(r.headers["X-Receipt"])
    assert receipt["message"]["actualAmount"] == ACTUAL
    assert receipt["message"]["actualAmount"] > 100


def test_settle_failure_keeps_llm_response_no_receipt(
    client: Any, fake_chain: Any
) -> None:
    """tx exception → LLM response returned intact + settle-failed, no
    X-Receipt, and GET /receipt/{id} → 404 (buyer refunds after ttl)."""
    fake_chain.settle_fails = True
    body = chat_body()
    r = post_chat(client, body)
    assert r.status_code == 200
    payload = r.json()
    assert payload["id"] == "cmpl-1"
    assert payload["choices"][0]["message"]["content"] == "hi"
    assert r.headers["X-Settle-Status"] == "settle-failed"
    assert "X-Receipt" not in r.headers

    r2 = client.get("/receipt/42")
    assert r2.status_code == 404


def test_receipt_endpoint_serves_same_structure(client: Any, fake_chain: Any) -> None:
    """GET /receipt/{paymentId} returns the same JSON structure as X-Receipt."""
    body = chat_body()
    r = post_chat(client, body)
    assert r.status_code == 200
    header_receipt = decode_x_receipt(r.headers["X-Receipt"])

    r2 = client.get("/receipt/42")
    assert r2.status_code == 200
    assert r2.json() == header_receipt
    assert json.loads(json.dumps(r2.json())) == header_receipt  # plain JSON round-trip


# ------------------------------------------------- M9 Registry v2 per-model


def test_pricing_and_min_amount_use_requested_model_price(
    client: Any, fake_chain: Any
) -> None:
    """M9 v2: the three unit prices follow the REQUESTED model (getPrice) —
    both the settle amount AND the minAmount estimate."""
    fake_chain.models = ["gpt-4o-mini", "gpt-4.1-mini", ""]
    fake_chain.model_prices = {
        "gpt-4o-mini": (12_500, 25_000, 50_000),
        "gpt-4.1-mini": (25_000, 50_000, 100_000),
    }

    r = post_chat(client, chat_body(model="gpt-4o-mini"))
    assert r.status_code == 200
    assert r.headers["X-Settle-Status"] == "settled"
    # (400*12.5k + 1100*25k + 2500*50k) // 1e6 = 157
    assert fake_chain.settle_calls == [(42, 157)]
    # minAmount = (25k*200000 + 50k*32000) // 1e6 = 6600 — same model's prices
    assert fake_chain.valid_calls == [(42, SELLER, 6_600)]
    receipt = decode_x_receipt(r.headers["X-Receipt"])
    assert receipt["message"]["model"] == "gpt-4o-mini"
    assert receipt["message"]["actualAmount"] == 157

    # The other listed model prices at the suite-wide defaults → 315 / 13_200.
    r2 = post_chat(client, chat_body(model="gpt-4.1-mini"), payment_id="43")
    assert r2.status_code == 200
    assert fake_chain.settle_calls[-1] == (43, ACTUAL)
    assert fake_chain.valid_calls[-1] == (43, SELLER, 13_200)
    assert fake_chain.price_reads == 2


def test_get_price_not_active_maps_to_400(client: Any, fake_chain: Any) -> None:
    """Race: listing deactivated between getListing and getPrice → the
    NotActive revert face maps to the listing-inactive 400."""
    fake_chain.price_error = "NotActive"
    r = post_chat(client, chat_body())
    assert r.status_code == 400
    assert "inactive" in r.json()["detail"]
    assert fake_chain.settle_calls == []


def test_get_price_model_not_found_maps_to_400(
    client: Any, fake_chain: Any
) -> None:
    """ModelNotFound revert face maps to the model-gate 400."""
    fake_chain.price_error = "ModelNotFound"
    r = post_chat(client, chat_body())
    assert r.status_code == 400
    assert "model not in listing.models" in r.json()["detail"]
    assert fake_chain.settle_calls == []
