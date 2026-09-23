"""PIN error codes 401 (four types) and 402 (two types) — the unpaid path
must never reach the upstream (zero-cost rejection)."""

from __future__ import annotations

from typing import Any

import pytest

from .conftest import chat_body, post_chat, signed_headers


# ------------------------------------------------------------------ 401 (x4)


def test_401_missing_x_signature(client: Any) -> None:
    body = chat_body()
    r = client.post(
        "/v1/chat/completions",
        content=body,
        headers={"X-Payment-Id": "42", "Content-Type": "application/json"},
    )
    assert r.status_code == 401
    assert "X-Signature" in r.json()["detail"]


def test_401_malformed_signature(client: Any) -> None:
    body = chat_body()
    r = client.post(
        "/v1/chat/completions",
        content=body,
        headers={"X-Payment-Id": "42", "X-Signature": "0xdeadbeef",
                 "Content-Type": "application/json"},
    )
    assert r.status_code == 401
    assert r.json()["detail"] == "invalid signature"


def test_401_missing_x_payment_id(client: Any) -> None:
    body = chat_body()
    hdrs = signed_headers(body, "42")
    hdrs.pop("X-Payment-Id")
    r = client.post("/v1/chat/completions", content=body, headers=hdrs)
    assert r.status_code == 401
    assert "X-Payment-Id" in r.json()["detail"]


def test_401_non_decimal_payment_id(client: Any) -> None:
    body = chat_body()
    for bad in ("abc", "12a", "-1", "1.5", ""):  # non-decimal payment ids
        r = client.post(
            "/v1/chat/completions",
            content=body,
            headers={"X-Payment-Id": bad, "X-Signature": "0x" + "00" * 65},
        )
        assert r.status_code == 401, bad
        assert "X-Payment-Id" in r.json()["detail"]


def test_401_unicode_digit_payment_id_guard() -> None:
    """isascii guard unit test: int() would accept '٤٢' → 42; the relay must
    reject such values (httpx can't send non-ascii headers, so this is tested
    directly against the parser)."""
    import relay.app.main as m
    from fastapi import HTTPException

    assert m._parse_payment_id("42") == 42
    assert m._parse_payment_id(" 42 ") == 42
    for bad in ("٤٢", "²", "1²", "abc", "-1", "1.5", "", None):
        with pytest.raises(HTTPException) as exc:
            m._parse_payment_id(bad)
        assert exc.value.status_code == 401


# ------------------------------------------------------------------ 402 (x2)


def test_402_signer_is_not_buyer(client: Any, fake_chain: Any) -> None:
    """Seller tries to self-authorize with its own key → 402, no forward."""
    body = chat_body()
    r = post_chat(client, body, key="0xac0974bec39a17e36ba4a6b4d238ff944bacb478cbed5efcae784d7bf4f2ff80")
    assert r.status_code == 402
    assert "not payment buyer" in r.json()["detail"]


def test_402_is_valid_false(client: Any, fake_chain: Any) -> None:
    """Escrow.isValid false (unpaid/wrong seller/expired) → 402, no forward."""
    fake_chain.valid = False
    body = chat_body()
    r = post_chat(client, body)
    assert r.status_code == 402
    assert "payment invalid" in r.json()["detail"]


def test_402_never_forwards_upstream(client: Any, fake_chain: Any, mock_openai: Any) -> None:
    """Zero-cost rejection: neither 401 nor 402 may touch the upstream."""
    body = chat_body()
    r = post_chat(client, body, headers={"X-Payment-Id": "42"})
    fake_chain.valid = False
    assert post_chat(client, body).status_code == 402
    assert mock_openai.received == []
    assert fake_chain.settle_calls == []
