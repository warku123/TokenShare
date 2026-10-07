"""Shared-mode custody test helpers (M15 R1) — deterministic valueless
keys, env setup, request builders and a fake chain for binding snapshots.

All keys here are FIXED SYNTHETIC TEST KEYS (anvil-style, valueless). They
are never production material and never used against real networks.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path
from typing import Any

import pytest
from eth_account import Account
from eth_account.messages import encode_defunct

from relay.app import custody
from relay.app.config import P256_ORDER

REPO_ROOT = Path(__file__).resolve().parents[2]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from .conftest import CHAIN_ID  # noqa: E402

# ---------------------------------------------------------------------------
# Deterministic synthetic keys (valueless — NEVER production material).
# ---------------------------------------------------------------------------
SHARED_SIGNER_KEY = "0x" + "11" * 32
SHARED_KEK_HEX = "0x" + "22" * 32
# Fixed valid P-256 scalar (checked at import against the group order).
SHARED_UPLOAD_KEY = "0x" + "1f" * 32
assert 1 <= int(SHARED_UPLOAD_KEY, 16) < P256_ORDER

SIGNER_ADDR = Account.from_key(SHARED_SIGNER_KEY).address

# Custody customer wallets (external sellers enrolling keys).
WALLET_KEY = "0x" + "77" * 32
WALLET = Account.from_key(WALLET_KEY).address
WALLET2_KEY = "0x" + "88" * 32
WALLET2 = Account.from_key(WALLET2_KEY).address

DEFAULT_ORIGIN = "https://relay.example"
ESCROW_ADDR = "0x1111111111111111111111111111111111111111"
REGISTRY_ADDR = "0x2222222222222222222222222222222222222222"

KEK = bytes.fromhex(SHARED_KEK_HEX[2:])


# ---------------------------------------------------------------------------
# Env setup: shared mode without a dstack socket (env-key branch).
# ---------------------------------------------------------------------------
def setup_shared_env(
    monkeypatch: pytest.MonkeyPatch,
    keystore_path: str,
    *,
    origin: str = DEFAULT_ORIGIN,
    allow_custom_upstream: str = "1",
    cors_origins: str | None = None,
) -> None:
    import relay.app.config as config

    monkeypatch.setenv("RELAY_MODE", "shared")
    monkeypatch.setenv("RELAY_PUBLIC_ORIGIN", origin)
    monkeypatch.setenv("SHARED_KEYSTORE_PATH", keystore_path)
    monkeypatch.setenv("SHARED_SIGNER_KEY", SHARED_SIGNER_KEY)
    monkeypatch.setenv("SHARED_KEK_HEX", SHARED_KEK_HEX)
    monkeypatch.setenv("SHARED_UPLOAD_KEY", SHARED_UPLOAD_KEY)
    monkeypatch.setenv("RPC_URL", "http://unused.invalid")
    monkeypatch.setenv("CHAIN_ID", str(CHAIN_ID))
    monkeypatch.setenv("ESCROW_ADDR", ESCROW_ADDR)
    monkeypatch.setenv("REGISTRY_ADDR", REGISTRY_ADDR)
    monkeypatch.setenv("USDC_ADDR", "0x3333333333333333333333333333333333333333")
    monkeypatch.setenv("ALLOW_CUSTOM_UPSTREAM", allow_custom_upstream)
    # Custody origin gate (ora20 fix): None → unset (dev "*" face); a value
    # pins the allowed FRONTEND origins (production posture).
    if cors_origins is None:
        monkeypatch.delenv("RELAY_CORS_ORIGINS", raising=False)
    else:
        monkeypatch.setenv("RELAY_CORS_ORIGINS", cors_origins)
    # Shared mode must NOT depend on the single-mode seller/openai env.
    monkeypatch.delenv("RELAY_SELLER_KEY", raising=False)
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)
    monkeypatch.delenv("OPENAI_BASE_URL", raising=False)
    # No dstack socket → env-key branch (hermetic).
    monkeypatch.setattr(config, "DEFAULT_DSTACK_SOCKET", "/nonexistent/dstack.sock")


def setup_single_env(monkeypatch: pytest.MonkeyPatch, base_url: str) -> None:
    """Single-mode env WITHOUT custody vars (the legacy regression face)."""
    from .conftest import setup_relay_env

    import relay.app.config as config

    setup_relay_env(monkeypatch, base_url)
    monkeypatch.delenv("RELAY_MODE", raising=False)
    monkeypatch.delenv("RELAY_PUBLIC_ORIGIN", raising=False)
    monkeypatch.delenv("SHARED_KEYSTORE_PATH", raising=False)
    monkeypatch.delenv("SHARED_SIGNER_KEY", raising=False)
    monkeypatch.delenv("SHARED_KEK_HEX", raising=False)
    monkeypatch.delenv("SHARED_UPLOAD_KEY", raising=False)
    monkeypatch.setattr(config, "DEFAULT_DSTACK_SOCKET", "/nonexistent/dstack.sock")


# ---------------------------------------------------------------------------
# Fake chain for shared-mode binding snapshots (read-only surface only).
# ---------------------------------------------------------------------------
class _DelegateCall:
    def __init__(self, value: str | None) -> None:
        self._value = value

    def call(self) -> str:
        if self._value is None:
            raise RuntimeError("settleDelegateOf not available (chain v3.0)")
        return self._value


class _EscrowFunctions:
    def __init__(self, chain: "FakeSharedChain") -> None:
        self._chain = chain

    def settleDelegateOf(self, _addr: str) -> _DelegateCall:
        return _DelegateCall(type(self._chain).settle_delegate)


class _EscrowStub:
    def __init__(self, chain: "FakeSharedChain") -> None:
        self.functions = _EscrowFunctions(chain)


class FakeSharedChain:
    """Minimal ChainClient stand-in for the custody binding snapshot."""

    settle_delegate: str | None = "0x0000000000000000000000000000000000000000"
    # Lowercased operator that HAS a listing (None → nobody registered —
    # mirrors the real chain: getListing returns None for unregistered ops).
    registered_operator: str | None = None
    listing_active: bool = True
    models: list[str] = ["gpt-4o-mini"]
    listing_fails: bool = False
    delegate_fails: bool = False

    def __init__(self, *, rpc_url: str, escrow_addr: str, registry_addr: str,
                 seller_key: str, chain_id: int) -> None:
        self.seller_address = Account.from_key(seller_key).address
        self.escrow = _EscrowStub(self)

    @classmethod
    def reset(cls) -> None:
        cls.settle_delegate = "0x0000000000000000000000000000000000000000"
        cls.registered_operator = None
        cls.listing_active = True
        cls.models = ["gpt-4o-mini"]
        cls.listing_fails = False
        cls.delegate_fails = False

    def get_listing(self, operator: str) -> dict[str, Any] | None:
        if type(self).listing_fails:
            raise RuntimeError("rpc down")
        if (
            type(self).registered_operator is None
            or operator.lower() != type(self).registered_operator
        ):
            return None
        return {
            "operator": operator,
            "endpoint": "http://relay.example",
            "models": list(type(self).models),
            "prices": [],
            "active": type(self).listing_active,
        }


# ---------------------------------------------------------------------------
# Request builders (console-side mirror of the protocol)
# ---------------------------------------------------------------------------
def sign_eip191(message: str, key: str) -> str:
    sig = Account.from_key(key).sign_message(encode_defunct(text=message)).signature.hex()
    return sig if sig.startswith("0x") else "0x" + sig


def wallet_headers(message: str, key: str = WALLET_KEY, seller: str | None = None) -> dict[str, str]:
    addr = seller or Account.from_key(key).address
    return {
        "X-Tokenshare-Seller": addr.lower(),
        "X-Tokenshare-Signature": sign_eip191(message, key),
        "Content-Type": "application/json",
    }


def sha256_hex(raw: bytes) -> str:
    return custody.sha256_hex(raw)


def normalize_upstream(url: str) -> str:
    return custody.normalize_custody_upstream(url)


def submit_payload(
    record: dict[str, Any],
    upstream: str,
    api_key: str,
    *,
    seller: str = WALLET,
    upload_scalar: int | None = None,
    aad_override: str | None = None,
    eph_scalar: int | None = None,
) -> bytes:
    """Compact-ASCII POST body in the EXACT key order, envelope built with
    the server-mirror AAD for the given nonce record and seller."""
    from .conftest import CHAIN_ID as CID

    upload_scalar = SHARED_UPLOAD_KEY if upload_scalar is None else upload_scalar
    if aad_override is not None:
        normalized = upstream  # raw passthrough (tamper/invalid-URL tests)
    else:
        normalized = normalize_upstream(upstream)
    aad = aad_override if aad_override is not None else custody.build_upload_aad(
        seller=seller.lower(),
        chain_id=CID,
        escrow_addr=ESCROW_ADDR,
        registry_addr=REGISTRY_ADDR,
        origin=DEFAULT_ORIGIN,
        upstream_base_url=normalized,
        nonce=record["nonce"],
        issued=record["issued_at"],
        expires=record["expires_at"],
    )
    envelope = custody.encrypt_envelope(
        int(upload_scalar, 16),
        json.dumps({"api_key": api_key}, separators=(",", ":")).encode("ascii"),
        aad,
        eph_scalar=eph_scalar,
    )
    obj = {
        "nonce": record["nonce"],
        "issued_at": record["issued_at"],
        "expires_at": record["expires_at"],
        "upstream_base_url": upstream,
        "envelope": envelope,
    }
    return json.dumps(obj, separators=(",", ":"), ensure_ascii=True).encode("ascii")


def submit_message_for(
    record: dict[str, Any],
    raw: bytes,
    upstream: str,
    seller: str,
    *,
    origin: str = DEFAULT_ORIGIN,
) -> str:
    from .conftest import CHAIN_ID as CID

    return custody.build_submit_message(
        seller=seller.lower(),
        chain_id=CID,
        escrow_addr=ESCROW_ADDR,
        registry_addr=REGISTRY_ADDR,
        origin=origin,
        upstream_base_url=normalize_upstream(upstream),
        nonce=record["nonce"],
        body_sha256=sha256_hex(raw),
        issued=record["issued_at"],
        expires=record["expires_at"],
    )


def revoke_payload(record: dict[str, Any]) -> bytes:
    obj = {
        "nonce": record["nonce"],
        "issued_at": record["issued_at"],
        "expires_at": record["expires_at"],
    }
    return json.dumps(obj, separators=(",", ":"), ensure_ascii=True).encode("ascii")


def revoke_message_for(
    record: dict[str, Any], seller: str, *, origin: str = DEFAULT_ORIGIN
) -> str:
    from .conftest import CHAIN_ID as CID

    return custody.build_revoke_message(
        seller=seller.lower(),
        chain_id=CID,
        escrow_addr=ESCROW_ADDR,
        registry_addr=REGISTRY_ADDR,
        origin=origin,
        nonce=record["nonce"],
        issued=record["issued_at"],
        expires=record["expires_at"],
    )


def get_nonce(client: Any, seller: str = WALLET) -> dict[str, Any]:
    resp = client.get(f"/sellers/nonce/{seller.lower()}")
    assert resp.status_code == 200, resp.text
    return resp.json()


def fresh_record(client: Any, seller: str = WALLET) -> dict[str, Any]:
    record = get_nonce(client, seller)
    return {"nonce": record["nonce"], "issued_at": record["issued_at"], "expires_at": record["expires_at"]}
