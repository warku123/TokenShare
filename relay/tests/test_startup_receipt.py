"""Startup failure on missing required env (no degraded mode) and receipt
cryptography: EIP-712 recover == seller with the verbatim domain."""

from __future__ import annotations

from typing import Any

import pytest
from eth_account import Account
from eth_account.messages import encode_typed_data

from relay.app.config import ConfigError
from relay.app.receipt import RECEIPT_TYPES, decode_x_receipt

from .conftest import (
    ACTUAL,
    CHAIN_ID,
    SELLER,
    chat_body,
    post_chat,
    setup_relay_env,
)


def test_startup_fails_without_openai_api_key(monkeypatch: pytest.MonkeyPatch) -> None:
    """No degraded mode: lifespan must raise before any request is served."""
    from fastapi.testclient import TestClient

    import relay.app.main as m

    setup_relay_env(monkeypatch, "http://127.0.0.1:1/v1")
    monkeypatch.delenv("OPENAI_API_KEY")
    with pytest.raises(ConfigError, match="OPENAI_API_KEY"):
        with TestClient(m.app):
            pass


def test_startup_fails_when_seller_key_missing(monkeypatch: pytest.MonkeyPatch) -> None:
    from fastapi.testclient import TestClient

    import relay.app.main as m

    setup_relay_env(monkeypatch, "http://127.0.0.1:1/v1")
    monkeypatch.delenv("RELAY_SELLER_KEY")
    with pytest.raises(ConfigError, match="RELAY_SELLER_KEY"):
        with TestClient(m.app):
            pass


def test_receipt_recover_equals_seller_and_domain_verbatim(
    client: Any, fake_chain: Any
) -> None:
    """End-to-end: settle → X-Receipt → rebuild typed data from JSON →
    recover == seller; domain must match the PIN verbatim."""
    r = post_chat(client, chat_body())
    assert r.status_code == 200
    receipt = decode_x_receipt(r.headers["X-Receipt"])

    # Domain verbatim per PIN.
    assert receipt["domain"] == {"name": "TokenShare Relay", "version": "1",
                                 "chainId": CHAIN_ID}

    # Rebuild the exact typed structure from the plain JSON and recover.
    encoded = encode_typed_data(
        full_message={
            "types": RECEIPT_TYPES,
            "primaryType": "Receipt",
            "domain": receipt["domain"],
            "message": receipt["message"],
        }
    )
    recovered = Account.recover_message(encoded, signature=receipt["signature"])
    assert recovered == SELLER


def test_receipt_message_fields_exact(client: Any, fake_chain: Any) -> None:
    r = post_chat(client, chat_body())
    receipt = decode_x_receipt(r.headers["X-Receipt"])
    msg = receipt["message"]
    assert msg["paymentId"] == 42
    assert msg["promptTokens"] == 1500
    assert msg["cachedTokens"] == 400
    assert msg["completionTokens"] == 2500
    assert msg["actualAmount"] == ACTUAL
    assert msg["seller"] == SELLER
    assert msg["upstreamHost"] == "127.0.0.1"
    assert msg["model"] == "gpt-4o-mini"
    assert list(msg.keys()) == ["paymentId", "promptTokens", "cachedTokens",
                                "completionTokens", "actualAmount", "seller",
                                "upstreamHost", "model"]


def test_receipt_rejects_tampered_message(client: Any, fake_chain: Any) -> None:
    """Signature must not verify over a modified payload (CLI dispute path)."""
    r = post_chat(client, chat_body())
    receipt = decode_x_receipt(r.headers["X-Receipt"])
    tampered = dict(receipt["message"],
                    actualAmount=receipt["message"]["actualAmount"] + 1)
    encoded = encode_typed_data(
        full_message={
            "types": RECEIPT_TYPES,
            "primaryType": "Receipt",
            "domain": receipt["domain"],
            "message": tampered,
        }
    )
    recovered = Account.recover_message(encoded, signature=receipt["signature"])
    assert recovered != SELLER
