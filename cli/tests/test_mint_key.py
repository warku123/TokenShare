"""M13 consumer-side tests: `mint-key` (stateless signed API key) + `usage`
(cumulative capture view) + the Escrow v2 partial-settle ABI faces.

No real chain: chain helpers are monkeypatched. Time is frozen where expiry
math matters. The mint message / key format follow the M13 PIN verbatim:

    message = "TokenShare API key grant|paymentId={p}|expiry={e}|maxAmount={m}"
    key     = "tsk1.<b64url(payload_json)>.<b64url(sig_65B_hex)>"
"""

import json
import time as _time

import pytest
from eth_account import Account
from typer.testing import CliRunner

from tests.conftest import (
    BUYER_KEY,
    CHAIN_ID,
    ESCROW_ADDR,
    REGISTRY_ADDR,
    SELLER_KEY,
    USDC_ADDR,
    addr_of,
    all_output,
)

import tokenshare_cli.chain as chain_mod  # noqa: E402
from tokenshare_cli import signing  # noqa: E402
from tokenshare_cli.app import app  # noqa: E402
from tokenshare_cli.relay_client import RelayResponse  # noqa: E402

runner = CliRunner()

RPC_URL = "http://127.0.0.1:8545"
SELLER = addr_of(SELLER_KEY)
BUYER = addr_of(BUYER_KEY)
LISTING_ENDPOINT = "http://127.0.0.1:8787"
PAYMENT_EXPIRES_AT = 1_000_000  # far future vs the frozen clock below
PAYMENT_MAX = 5_000_000
NOW = 500_000


def _set_full_env(monkeypatch) -> None:
    monkeypatch.setenv("BUYER_PRIVATE_KEY", BUYER_KEY)
    monkeypatch.setenv("RPC_URL", RPC_URL)
    monkeypatch.setenv("CHAIN_ID", str(CHAIN_ID))
    monkeypatch.setenv("ESCROW_ADDR", ESCROW_ADDR)
    monkeypatch.setenv("REGISTRY_ADDR", REGISTRY_ADDR)
    monkeypatch.setenv("USDC_ADDR", USDC_ADDR)


def _payment(state: int = 1, **overrides):
    """Escrow payment dict in chain_mod.get_payment's shape."""
    payment = {
        "payment_id": 42,
        "buyer": BUYER,
        "seller": SELLER,
        "max_amount": PAYMENT_MAX,
        "expires_at": PAYMENT_EXPIRES_AT,
        "state": state,
        "state_name": {0: "None", 1: "Locked", 2: "Settled", 3: "Refunded"}.get(state, str(state)),
    }
    payment.update(overrides)
    return payment


def _fake_chain(monkeypatch, payment, listing=None):
    ctx = type("Ctx", (), {})()
    ctx.address = BUYER
    ctx.w3 = None
    monkeypatch.setattr(chain_mod, "open_chain", lambda cfg: ctx)
    monkeypatch.setattr(chain_mod, "get_payment", lambda c, pid: payment)
    monkeypatch.setattr(
        chain_mod, "get_listing", lambda c, seller: listing or {
            "operator": SELLER,
            "endpoint": LISTING_ENDPOINT,
            "models": ["kimi-for-coding", ""],
            "prices": [{"cached_in": 1_000_000, "input": 2_000_000, "output": 3_000_000}],
            "active": True,
        }
    )
    return ctx


def _freeze_time(monkeypatch, now: int = NOW) -> None:
    monkeypatch.setattr(_time, "time", lambda: now)
    # app.py imported `time` as a module — patch through the same module object.
    import tokenshare_cli.app as app_mod

    monkeypatch.setattr(app_mod.time, "time", lambda: now, raising=False)


# ---------------------------------------------------------------------------
# signing helpers (pure)
# ---------------------------------------------------------------------------


def test_mint_message_verbatim():
    assert signing.build_mint_message(7, 1000, 5000000) == (
        "TokenShare API key grant|paymentId=7|expiry=1000|maxAmount=5000000"
    )


def test_mint_key_roundtrip_recovers_buyer():
    key = signing.mint_api_key(BUYER_KEY, 42, PAYMENT_EXPIRES_AT, PAYMENT_MAX, BUYER)
    assert key.startswith("tsk1.")
    assert key.count(".") == 2
    payload, recovered = signing.recover_api_key_signer(key)
    assert payload == {"p": 42, "e": PAYMENT_EXPIRES_AT, "m": PAYMENT_MAX, "b": BUYER}
    assert recovered.lower() == BUYER.lower()


def test_mint_key_signature_is_65_bytes_hex():
    decoded = signing.decode_api_key(
        signing.mint_api_key(BUYER_KEY, 42, PAYMENT_EXPIRES_AT, PAYMENT_MAX, BUYER)
    )
    assert len(decoded["signature_hex"]) == 130  # 65 bytes, no 0x
    assert not decoded["signature_hex"].startswith("0x")


def test_decode_api_key_rejects_garbage():
    with pytest.raises(ValueError):
        signing.decode_api_key("not-a-key")
    with pytest.raises(ValueError):
        signing.decode_api_key("tsk1.!!!.???")


# ---------------------------------------------------------------------------
# mint-key command
# ---------------------------------------------------------------------------


def test_mint_key_json_output_shape(monkeypatch):
    _set_full_env(monkeypatch)
    _freeze_time(monkeypatch)
    _fake_chain(monkeypatch, _payment())
    result = runner.invoke(app, ["mint-key", "--payment-id", "42", "--json"])
    assert result.exit_code == 0, all_output(result)
    data = json.loads(result.output)
    assert data["paymentId"] == 42
    assert data["buyer"].lower() == BUYER.lower()
    assert data["seller"].lower() == SELLER.lower()
    assert data["expiry"] == PAYMENT_EXPIRES_AT
    assert data["maxAmount"] == PAYMENT_MAX
    assert data["baseUrl"] == LISTING_ENDPOINT
    assert data["model"] == "kimi-for-coding"
    payload, recovered = signing.recover_api_key_signer(data["apiKey"])
    assert payload["p"] == 42 and payload["e"] == PAYMENT_EXPIRES_AT
    assert recovered.lower() == BUYER.lower()


def test_mint_key_text_output_has_key_base_url_and_curl(monkeypatch):
    _set_full_env(monkeypatch)
    _freeze_time(monkeypatch)
    _fake_chain(monkeypatch, _payment())
    result = runner.invoke(app, ["mint-key", "--payment-id", "42"])
    assert result.exit_code == 0, all_output(result)
    out = all_output(result)
    assert "API key: tsk1." in out
    assert f"Relay base URL: {LISTING_ENDPOINT}" in out
    assert "curl http://127.0.0.1:8787/v1/chat/completions" in out
    assert "Authorization: Bearer tsk1." in out
    assert f"expiry: unix {PAYMENT_EXPIRES_AT}" in out


def test_mint_key_rejects_non_locked_payment(monkeypatch):
    _set_full_env(monkeypatch)
    _fake_chain(monkeypatch, _payment(state=2))
    result = runner.invoke(app, ["mint-key", "--payment-id", "42"])
    assert result.exit_code != 0
    assert "not Locked" in all_output(result)


def test_mint_key_rejects_foreign_payment(monkeypatch):
    _set_full_env(monkeypatch)
    other = Account.from_key("0x" + "44" * 32).address
    _fake_chain(monkeypatch, _payment(buyer=other))
    result = runner.invoke(app, ["mint-key", "--payment-id", "42"])
    assert result.exit_code != 0
    assert "refusing to mint" in all_output(result)


def test_mint_key_rejects_expired_payment(monkeypatch):
    _set_full_env(monkeypatch)
    _freeze_time(monkeypatch)
    _fake_chain(monkeypatch, _payment(expires_at=NOW - 1))
    result = runner.invoke(app, ["mint-key", "--payment-id", "42"])
    assert result.exit_code != 0
    assert "already expired" in all_output(result)


def test_mint_key_ttl_override_shortens_expiry(monkeypatch):
    _set_full_env(monkeypatch)
    _freeze_time(monkeypatch)
    _fake_chain(monkeypatch, _payment())
    result = runner.invoke(
        app, ["mint-key", "--payment-id", "42", "--ttl-override", "60", "--json"]
    )
    assert result.exit_code == 0, all_output(result)
    assert json.loads(result.output)["expiry"] == NOW + 60


def test_mint_key_ttl_override_never_extends_past_payment(monkeypatch):
    _set_full_env(monkeypatch)
    _freeze_time(monkeypatch)
    _fake_chain(monkeypatch, _payment())
    result = runner.invoke(
        app, ["mint-key", "--payment-id", "42", "--ttl-override", "999999999", "--json"]
    )
    assert result.exit_code == 0, all_output(result)
    assert json.loads(result.output)["expiry"] == PAYMENT_EXPIRES_AT


def test_mint_key_rejects_nonpositive_ttl_override(monkeypatch):
    _set_full_env(monkeypatch)
    _freeze_time(monkeypatch)
    _fake_chain(monkeypatch, _payment())
    result = runner.invoke(app, ["mint-key", "--payment-id", "42", "--ttl-override", "0"])
    assert result.exit_code != 0
    assert "--ttl-override" in all_output(result)


def test_mint_key_requires_relay_when_listing_endpoint_empty(monkeypatch):
    _set_full_env(monkeypatch)
    _freeze_time(monkeypatch)
    _fake_chain(monkeypatch, _payment(), listing={
        "operator": SELLER, "endpoint": "", "models": ["m"], "prices": [], "active": True,
    })
    result = runner.invoke(app, ["mint-key", "--payment-id", "42"])
    assert result.exit_code != 0
    assert "--relay" in all_output(result)


# ---------------------------------------------------------------------------
# usage command
# ---------------------------------------------------------------------------


def test_usage_displays_captured_and_remaining(monkeypatch):
    _set_full_env(monkeypatch)
    _fake_chain(monkeypatch, _payment())
    import tokenshare_cli.app as app_mod

    payload = {"captured": 11400, "maxAmount": PAYMENT_MAX, "remaining": PAYMENT_MAX - 11400}
    monkeypatch.setattr(
        app_mod,
        "get_usage",
        lambda base_url, pid, timeout=30.0: RelayResponse(
            200, {}, json.dumps(payload)
        ),
    )
    result = runner.invoke(app, ["usage", "--payment-id", "42"])
    assert result.exit_code == 0, all_output(result)
    out = all_output(result)
    assert "captured: 11400 native (= 0.0114 USDC)" in out
    assert f"maxAmount: {PAYMENT_MAX} native (= 5 USDC)" in out
    assert f"remaining: {PAYMENT_MAX - 11400} native" in out


def test_usage_relay_error_surfaces(monkeypatch):
    _set_full_env(monkeypatch)
    _fake_chain(monkeypatch, _payment())
    import tokenshare_cli.app as app_mod
    from tokenshare_cli.errors import RelayError

    def boom(base_url, pid, timeout=30.0):
        raise RelayError(404, "no usage")

    monkeypatch.setattr(app_mod, "get_usage", boom)
    result = runner.invoke(app, ["usage", "--payment-id", "42"])
    assert result.exit_code != 0


# ---------------------------------------------------------------------------
# ABI faces (Escrow v2, M13)
# ---------------------------------------------------------------------------


def test_escrow_abi_has_partial_settle_faces():
    from tokenshare_cli.abis import ESCROW_ABI

    settle = next(
        e for e in ESCROW_ABI
        if e.get("type") == "function" and e.get("name") == "settlePartial"
    )
    assert [i["type"] for i in settle["inputs"]] == ["uint256", "uint256"]
    assert settle["stateMutability"] == "nonpayable"

    event = next(
        e for e in ESCROW_ABI
        if e.get("type") == "event" and e.get("name") == "SettlePartial"
    )
    assert [(i["type"], i.get("indexed", False)) for i in event["inputs"]] == [
        ("uint256", True),
        ("uint256", False),
        ("uint256", False),
    ]
