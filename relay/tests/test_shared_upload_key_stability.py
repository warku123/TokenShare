"""Shared-mode upload key boot-stability regression (v1.0-e1, fail-closed).

Real-machine evidence: shared-mode signer/kek derivations are stable across
boots, but the upload key was randomized every boot by a silent
``os.urandom`` fallback when the raw32 → P-256 scalar mapping "failed".
That fallback is FORBIDDEN by the frozen protocol (the upload key MUST be
dstack-domain-derived and boot-stable) and by the shared-mode discipline
(any derive failure → ConfigError, NO fallback — same as signer/kek).

The frozen mapping ``scalar = int(raw32) % (order-1) + 1`` yields a valid
scalar in [1, n-1] for EVERY 32-byte input, so there is no legitimate
failure path left: any get_key/decode/mapping failure must fail closed.

Everything is mocked: a fake dstack client is injected via monkeypatch.
Zero real TEE, zero dstack-sdk install, zero network.
"""

from __future__ import annotations

import os
import types
from typing import Any

import pytest
from eth_account import Account

import relay.app.config as config
from relay.app.custody import (
    P256_A,
    P256_B,
    P256_N,
    P256_P,
    raw32_to_upload_scalar,
    upload_public_bytes,
)

# Bare legacy dstack get_key paths (must stay in lockstep with config.py).
SIGNER_PATH = "tokenshare/shared/signer/v1"
KEK_PATH = "tokenshare/shared/kek/v1"
UPLOAD_PATH = "tokenshare/shared/upload/v1"


class _FakeDstack:
    """Per-path raw32 responses; a path may be mapped to an Exception to
    make get_key itself raise."""

    def __init__(self, keys: dict[str, Any]) -> None:
        self._keys = keys

    def get_key(self, path: str) -> Any:
        value = self._keys[path]
        if isinstance(value, Exception):
            raise value
        return types.SimpleNamespace(decode_key=lambda: value)


def _keys_for(upload_raw: bytes) -> dict[str, Any]:
    """Deterministic three-key material with the given upload raw32.
    Signer raw 0x44*32 and KEK raw 0x55*32 are valueless fixtures."""
    return {
        SIGNER_PATH: b"\x44" * 32,
        KEK_PATH: b"\x55" * 32,
        UPLOAD_PATH: upload_raw,
    }


def _derive(monkeypatch: pytest.MonkeyPatch, upload_raw: bytes) -> tuple[str, bytes, int]:
    """Run _derive_shared_keys against a fake dstack client (bypasses the
    socket-existence check — that lives in _load_shared_config)."""
    fake = _FakeDstack(_keys_for(upload_raw))
    monkeypatch.setattr(config, "dstack_client", lambda socket_path: fake)
    return config._derive_shared_keys("/fake/dstack.sock")


# --------------------------------------------------------------------------
# a) Same raw32 → same upload public key (boot-stability core regression)
# --------------------------------------------------------------------------
def test_same_raw32_derives_identical_upload_key_twice(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """THE regression: the same dstack raw32 must produce the SAME upload
    scalar/public key on every derivation (every boot). Any silent random
    fallback makes this test fail."""
    upload_raw = bytes.fromhex("ab" * 32)

    first = _derive(monkeypatch, upload_raw)
    second = _derive(monkeypatch, upload_raw)

    assert first == second
    _, _, scalar1 = first
    _, _, scalar2 = second
    assert scalar1 == scalar2
    assert upload_public_bytes(scalar1) == upload_public_bytes(scalar2)


def test_same_raw32_same_signer_address_same_report_data(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Boot stability end-to-end: identical raw32 → identical signer key →
    identical seller address → identical quote report_data binding
    (44 zero bytes + left-padded address, per the /attestation route)."""
    upload_raw = os.urandom(32)

    signer1, _, _ = _derive(monkeypatch, upload_raw)
    signer2, _, _ = _derive(monkeypatch, upload_raw)

    assert signer1 == signer2
    addr1 = Account.from_key(signer1).address
    addr2 = Account.from_key(signer2).address
    assert addr1 == addr2
    report_data = b"\x00" * 44 + bytes.fromhex(addr1[2:])
    assert report_data == b"\x00" * 44 + bytes.fromhex(addr2[2:])


def test_signer_and_kek_stable_while_upload_input_fixed(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """All three legs derive from the same fixed raw32 inputs → all three
    outputs identical across derivations (signer + kek + upload)."""
    upload_raw = os.urandom(32)

    run1 = _derive(monkeypatch, upload_raw)
    run2 = _derive(monkeypatch, upload_raw)

    assert run1[0] == run2[0]  # signer
    assert run1[1] == run2[1]  # kek
    assert run1[2] == run2[2]  # upload scalar


# --------------------------------------------------------------------------
# b) Edge + random raw32 → valid scalar, public key on P-256
# --------------------------------------------------------------------------
def _assert_on_p256(scalar: int) -> bytes:
    """The derived scalar must yield a 65-byte uncompressed public key whose
    (x, y) satisfies the P-256 curve equation (no failure path allowed)."""
    pub = upload_public_bytes(scalar)
    assert len(pub) == 65 and pub[0] == 0x04
    x = int.from_bytes(pub[1:33], "big")
    y = int.from_bytes(pub[33:65], "big")
    assert 0 < x < P256_P and 0 < y < P256_P
    assert (y * y - (x * x * x + P256_A * x + P256_B)) % P256_P == 0
    return pub


def test_upload_scalar_mapping_matches_frozen_protocol() -> None:
    """scalar = int(raw32) % (n-1) + 1 with n = P-256 order (frozen)."""
    assert P256_N == 0xFFFFFFFF00000000FFFFFFFFFFFFFFFFBCE6FAADA7179E84F3B9CAC2FC632551
    assert config.P256_ORDER == P256_N
    raw = os.urandom(32)
    expected = (int.from_bytes(raw, "big") % (P256_N - 1)) + 1
    assert raw32_to_upload_scalar(raw) == expected


@pytest.mark.parametrize(
    "raw",
    [
        b"\x00" * 32,  # all-zero → scalar 1 (lowest legal)
        b"\xff" * 32,  # all-ff → wraps into range
        os.urandom(32),
        os.urandom(32),
        os.urandom(32),
    ],
    ids=["zeros", "ffs", "random-1", "random-2", "random-3"],
)
def test_any_raw32_derives_valid_on_curve_upload_key(
    monkeypatch: pytest.MonkeyPatch, raw: bytes
) -> None:
    """The mapping has NO failure path: every 32-byte input yields a scalar
    in [1, n-1] and a public key ON the curve."""
    signer, kek, scalar = _derive(monkeypatch, raw)

    assert signer == "0x" + ("44" * 32)
    assert kek == b"\x55" * 32
    assert isinstance(scalar, int) and 1 <= scalar < P256_N
    _assert_on_p256(scalar)
    # Deterministic re-derivation of the same edge input.
    _, _, scalar_again = _derive(monkeypatch, raw)
    assert scalar_again == scalar


def test_all_zero_raw32_maps_to_scalar_one(monkeypatch: pytest.MonkeyPatch) -> None:
    """int(0) % (n-1) + 1 == 1 — the degenerate input is still legal."""
    _, _, scalar = _derive(monkeypatch, b"\x00" * 32)
    assert scalar == 1
    _assert_on_p256(scalar)


# --------------------------------------------------------------------------
# c) get_key/decode/mapping failures → ConfigError (fail closed, NO random)
# --------------------------------------------------------------------------
def test_upload_get_key_exception_fails_closed(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Upload get_key raising must ConfigError — never silently fall back to
    a random scalar (the exact real-machine bug this file pins)."""
    keys = _keys_for(b"\x66" * 32)
    keys[UPLOAD_PATH] = RuntimeError("tdx agent flapped")
    fake = _FakeDstack(keys)
    monkeypatch.setattr(config, "dstack_client", lambda socket_path: fake)

    with pytest.raises(config.ConfigError):
        config._derive_shared_keys("/fake/dstack.sock")


def test_upload_decode_failure_fails_closed(monkeypatch: pytest.MonkeyPatch) -> None:
    keys = _keys_for(b"\x66" * 32)
    # Wrong length raw32 (31 bytes) → _decode_raw32 fails closed.
    keys[UPLOAD_PATH] = b"\x66" * 31
    fake = _FakeDstack(keys)
    monkeypatch.setattr(config, "dstack_client", lambda socket_path: fake)

    with pytest.raises(config.ConfigError):
        config._derive_shared_keys("/fake/dstack.sock")


def test_upload_decode_key_raises_fails_closed(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    keys = _keys_for(b"\x66" * 32)

    def _boom() -> bytes:
        raise ValueError("decode failed")

    keys[UPLOAD_PATH] = types.SimpleNamespace(decode_key=_boom)
    fake = _FakeDstack(keys)
    monkeypatch.setattr(config, "dstack_client", lambda socket_path: fake)

    with pytest.raises(config.ConfigError):
        config._derive_shared_keys("/fake/dstack.sock")


@pytest.mark.parametrize("leg", [SIGNER_PATH, KEK_PATH, UPLOAD_PATH])
def test_any_leg_get_key_exception_fails_closed(
    monkeypatch: pytest.MonkeyPatch, leg: str
) -> None:
    """Same fail-closed discipline for all three legs (signer/kek/upload)."""
    keys = _keys_for(b"\x66" * 32)
    keys[leg] = RuntimeError(f"{leg} unavailable")
    fake = _FakeDstack(keys)
    monkeypatch.setattr(config, "dstack_client", lambda socket_path: fake)

    with pytest.raises(config.ConfigError):
        config._derive_shared_keys("/fake/dstack.sock")


def test_repeated_failures_stay_deterministic(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Failure is deterministic: two consecutive failing boots both raise —
    a random fallback would succeed on retry instead."""
    keys = _keys_for(b"\x66" * 32)
    keys[UPLOAD_PATH] = RuntimeError("still down")
    fake = _FakeDstack(keys)
    monkeypatch.setattr(config, "dstack_client", lambda socket_path: fake)

    with pytest.raises(config.ConfigError):
        config._derive_shared_keys("/fake/dstack.sock")
    with pytest.raises(config.ConfigError):
        config._derive_shared_keys("/fake/dstack.sock")
