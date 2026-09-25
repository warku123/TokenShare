"""M13-R bearer api-key revocation.

Covers: POST /payment/{id}/revoke happy path (buyer-signed
`TokenShare API key revoke|paymentId={p}|expiry={e}`, EIP-191) → in-memory
revoked set → bearer calls 401 "revoked" ahead of every 402 gate; idempotent
second revoke (200 again); non-buyer 401 (recover vs CHAIN payment.buyer);
misplaced-signature replays 401 (message paymentId ≠ path id, message expiry
≠ on-chain expiresAt); wrong message prefix 401 (grant-format text never
verifies as a revoke); a revoke signature can NOT be reused as an api-key
mint signature (different prefix → different digest → recover mismatch);
restart semantics (fresh lifespan → fresh RelayState → empty revoked set);
GET /payment/{id}/usage exposes the `revoked` field.
"""

from __future__ import annotations

import json
from typing import Any

from eth_account import Account
from eth_account.messages import encode_defunct
from fastapi.testclient import TestClient

from .conftest import (
    BUYER,
    BUYER_KEY,
    FROZEN,
    bearer_headers,
    b64url,
    chat_body,
    mint_api_key,
    mock_openai_url,
    setup_relay_env,
)

# payment.expiresAt as FakeChain reports it (ttl_delta=600 default).
EXPIRY = int(FROZEN) + 600

# Second valueless well-known identity (anvil #3) for the non-buyer case.
OTHER_KEY = "0x5de4111afa1a4b94908f83103eb1f1706367c2e68ca870fc3fb9a804cdab365a"


def _revoke_body(
    payment_id: int = 42,
    expiry: int = EXPIRY,
    key: str = BUYER_KEY,
    message_override: str | None = None,
) -> dict[str, str]:
    msg = message_override or (
        f"TokenShare API key revoke|paymentId={payment_id}|expiry={expiry}"
    )
    sig = Account.from_key(key).sign_message(encode_defunct(text=msg)).signature.hex()
    if not sig.startswith("0x"):
        sig = "0x" + sig
    return {"message": msg, "signature": sig}


def _revoke(client: TestClient, **kwargs: Any) -> Any:
    return client.post("/payment/42/revoke", json=_revoke_body(**kwargs))


def _post_bearer(client: TestClient, api_key: str) -> Any:
    return client.post(
        "/v1/chat/completions", content=chat_body(), headers=bearer_headers(api_key)
    )


# ------------------------------------------------------------------ happy path


def test_revoke_then_bearer_401_revoked(client: Any, fake_chain: Any) -> None:
    r = _revoke(client)
    assert r.status_code == 200
    assert r.json() == {"paymentId": 42, "revoked": True, "alreadyRevoked": False}

    # The bearer key for paymentId 42 is dead: 401 detail "revoked", and the
    # refusal comes BEFORE the 402 budget gates / any settle side effects.
    r = _post_bearer(client, mint_api_key())
    assert r.status_code == 401
    assert r.json()["detail"] == "revoked"
    assert fake_chain.settle_partial_calls == []
    assert fake_chain.settle_calls == []


def test_revoke_idempotent_second_call_200(client: Any, fake_chain: Any) -> None:
    r1 = _revoke(client)
    r2 = _revoke(client)
    assert r1.status_code == r2.status_code == 200
    assert r1.json()["alreadyRevoked"] is False
    assert r2.json()["alreadyRevoked"] is True


def test_revoke_blocks_all_grants_of_payment(client: Any, fake_chain: Any) -> None:
    """Revocation is keyed by paymentId: EVERY bearer key minted for the
    payment dies, not just the exact envelope that was minted first."""
    assert _revoke(client).status_code == 200
    for key in (mint_api_key(), mint_api_key(max_amount=500_000)):
        r = _post_bearer(client, key)
        assert r.status_code == 401
        assert r.json()["detail"] == "revoked"


# ------------------------------------------------------------- negative cases


def test_revoke_non_buyer_401(client: Any, fake_chain: Any) -> None:
    """The recovered signer must equal the CHAIN's payment.buyer — body
    parameters are never trusted for identity."""
    r = _revoke(client, key=OTHER_KEY)
    assert r.status_code == 401
    assert fake_chain.payment_reads == 1  # chain read happened (authoritative)
    # Key untouched — a failed revoke never blocks service.
    assert _post_bearer(client, mint_api_key()).status_code == 200


def test_revoke_mismatched_payment_id_401(client: Any, fake_chain: Any) -> None:
    """Misplaced-signature replay: message signed for paymentId 43 cannot be
    replayed against the /payment/42/revoke path even by the real buyer."""
    r = _revoke(client, payment_id=43)
    assert r.status_code == 401
    assert _post_bearer(client, mint_api_key()).status_code == 200


def test_revoke_expiry_mismatch_401(client: Any, fake_chain: Any) -> None:
    """Message expiry must equal the on-chain payment.expiresAt (the mint
    grant's own value) — a stale revoke from a previous grant round of the
    same paymentId is rejected."""
    r = _revoke(client, expiry=EXPIRY - 1)
    assert r.status_code == 401


def test_revoke_wrong_prefix_401(client: Any, fake_chain: Any) -> None:
    """Prefix is checked VERBATIM: the grant-format message (even validly
    buyer-signed) is not a revoke message."""
    grant_text = (
        f"TokenShare API key grant|paymentId=42|expiry={EXPIRY}|maxAmount=1000000"
    )
    r = _revoke(client, message_override=grant_text)
    assert r.status_code == 401

    # Extra/missing pipe segments are structurally malformed → 401 too.
    extra = f"TokenShare API key revoke|paymentId=42|expiry={EXPIRY}|maxAmount=1"
    assert _revoke(client, message_override=extra).status_code == 401
    short = "TokenShare API key revoke|paymentId=42"
    assert _revoke(client, message_override=short).status_code == 401
    assert fake_chain.settle_partial_calls == []


def test_revoke_bad_signature_401(client: Any, fake_chain: Any) -> None:
    body = _revoke_body()
    body["signature"] = "0x" + "00" * 65
    r = client.post("/payment/42/revoke", json=body)
    assert r.status_code == 401


def test_revoke_missing_fields_400(client: Any, fake_chain: Any) -> None:
    for body in ({}, {"message": "x"}, {"signature": "0x" + "00" * 65}, [1, 2]):
        r = client.post("/payment/42/revoke", json=body)
        assert r.status_code == 400, body
    # No chain read for structurally invalid bodies.
    assert fake_chain.payment_reads == 0


def test_revoke_signature_not_reusable_as_mint_key(client: Any, fake_chain: Any) -> None:
    """A valid revoke signature wrapped into a tsk1 api-key envelope NEVER
    authenticates the bearer path: the mint message has a different literal
    prefix (grant vs revoke) → different digest → recover mismatch."""
    msg = f"TokenShare API key revoke|paymentId=42|expiry={EXPIRY}"
    sig = (
        "0x"
        + Account.from_key(BUYER_KEY).sign_message(encode_defunct(text=msg)).signature.hex()
    )
    payload = {"p": 42, "e": EXPIRY, "m": 1_000_000, "b": BUYER}
    forged_key = (
        "tsk1."
        + b64url(json.dumps(payload, separators=(",", ":")).encode("utf-8"))
        + "."
        + b64url(sig.encode("ascii"))
    )
    r = _post_bearer(client, forged_key)
    assert r.status_code == 401
    assert fake_chain.settle_partial_calls == []


# -------------------------------------------------------- state / observations


def test_restart_clears_revocations(
    monkeypatch: Any, mock_openai: Any, fake_chain: Any
) -> None:
    """HONEST restart semantics: the revoked set lives on RelayState — a new
    lifespan boots a fresh state with an EMPTY set (documented exposure,
    backstopped by grant TTL + maxAmount, never persisted)."""
    import relay.app.main as m

    setup_relay_env(monkeypatch, mock_openai_url(mock_openai))
    with TestClient(m.app) as c1:
        assert _revoke(c1).status_code == 200
        assert m.state is not None
        assert m.state.revoked_api_keys == {42}
        r = _post_bearer(c1, mint_api_key())
        assert r.status_code == 401
        assert r.json()["detail"] == "revoked"
    # "Restart": fresh lifespan → fresh RelayState → empty revoked set.
    with TestClient(m.app) as c2:
        assert m.state is not None
        assert m.state.revoked_api_keys == set()
        assert _post_bearer(c2, mint_api_key()).status_code == 200


def test_usage_reports_revoked_field(client: Any, fake_chain: Any) -> None:
    u1 = client.get("/payment/42/usage")
    assert u1.status_code == 200
    assert u1.json()["revoked"] is False

    assert _revoke(client).status_code == 200

    u2 = client.get("/payment/42/usage")
    body = u2.json()
    assert body["revoked"] is True
    assert body["paymentId"] == 42
    for field in ("captured", "maxAmount", "remaining"):
        assert field in body
