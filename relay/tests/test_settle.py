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
