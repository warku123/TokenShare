"""Tests for `verify-attestation` (M7-A) — pure parsing + mocked chain read.

No network, no chain, no TEE: quotes are synthetic SGX-quote-v4-shaped
bytes and the on-chain anchor read is monkeypatched.
"""

from __future__ import annotations

import json

import pytest
from eth_utils import keccak, to_checksum_address
from typer.testing import CliRunner

from tokenshare_cli.app import app
from tokenshare_cli.attestation import parse_quote

from tests.conftest import all_output, buyer_env

TEE_ADDR_BYTES = bytes.fromhex("ab" * 20)
TEE_ADDR = to_checksum_address("0x" + "ab" * 20)

# Full chain env (read_anchor_digest is mocked in tests; load_config must pass).
FULL_ENV = {
    "BUYER_PRIVATE_KEY": "0x" + "11" * 32,
    "RPC_URL": "http://127.0.0.1:1",
    "CHAIN_ID": "10143",
    "ESCROW_ADDR": "0x" + "aa" * 20,
    "REGISTRY_ADDR": "0x" + "bb" * 20,
    "USDC_ADDR": "0x" + "cc" * 20,
}


@pytest.fixture()
def full_env(monkeypatch):
    for key, value in FULL_ENV.items():
        monkeypatch.setenv(key, value)


def make_quote_bytes(*, with_binding: bool = True) -> bytes:
    """Synthetic SGX ECDSA quote v4-shaped payload:
    48B header + 384B body (report_data best-effort at body+320) + tail.

    Non-binding regions are pseudorandom (like real signatures/DER certs) —
    they essentially never contain a 44-zero-byte run, so the binding search
    is not fooled (an all-zero body WOULD fake-match the nonzero tail).
    """
    def pseudo(n: int, seed: int) -> bytes:
        out = b""
        counter = 0
        while len(out) < n:
            out += keccak(b"tokenshare-test-seed" + bytes([seed, counter]))
            counter += 1
        return out[:n]

    header = bytearray(pseudo(48, 1))
    header[0:2] = (4).to_bytes(2, "little")   # quote version 4
    header[2:4] = (2).to_bytes(2, "little")   # att_key_type: ECDSA-P256
    header[12:14] = (7).to_bytes(2, "little")  # qe_svn
    header[14:16] = (5).to_bytes(2, "little")  # pce_svn
    body = bytearray(pseudo(384, 7))
    if with_binding:
        body[320:384] = b"\x00" * 44 + TEE_ADDR_BYTES
    return bytes(header) + bytes(body) + pseudo(96, 9)  # signature-ish tail


def quote_digest(quote: bytes) -> str:
    return "0x" + keccak(quote).hex()


@pytest.fixture()
def runner():
    return CliRunner()


def test_parse_quote_finds_binding_and_digest(runner, buyer_env):
    quote = make_quote_bytes()
    result = runner.invoke(
        app, ["verify-attestation", "--quote", "0x" + quote.hex()]
    )
    out = all_output(result)
    assert result.exit_code == 0, out
    assert "Quote length: 528 bytes" in out
    assert "version=4 att_key_type=2 qe_svn=7 pce_svn=5" in out
    assert f"Derived address (TEE key path wallet/ethereum/tokenshare): {TEE_ADDR}" in out
    assert "Address binding found at quote offset 368" in out
    assert quote_digest(quote) in out
    assert "dstack-verifier" in out  # the off-chain-verify disclaimer


def test_parse_quote_json_payload(runner, buyer_env):
    quote = make_quote_bytes()
    payload = {
        "tee": True,
        "appId": "0x1234",
        "derivedAddress": TEE_ADDR,
        "reportData": "0x" + (b"\x00" * 44 + TEE_ADDR_BYTES).hex(),
        "quote": "0x" + quote.hex(),
        "quoteDigest": quote_digest(quote),
    }
    result = runner.invoke(app, ["verify-attestation", "--quote", json.dumps(payload)])
    out = all_output(result)
    assert result.exit_code == 0, out
    assert f"Derived address (TEE key path wallet/ethereum/tokenshare): {TEE_ADDR}" in out
    assert quote_digest(quote) in out


def test_parse_quote_without_binding_still_digests(runner, buyer_env):
    quote = make_quote_bytes(with_binding=False)
    result = runner.invoke(app, ["verify-attestation", "--quote", quote.hex()])
    out = all_output(result)
    assert result.exit_code == 0, out
    assert "no report-data address binding found" in out
    assert quote_digest(quote) in out


def test_verify_attestation_invalid_hex_fails(runner, buyer_env):
    result = runner.invoke(app, ["verify-attestation", "--quote", "zz-not-hex"])
    out = all_output(result)
    assert result.exit_code == 2
    assert "not valid hex" in out


def test_verify_attestation_empty_json_fails(runner, buyer_env):
    result = runner.invoke(app, ["verify-attestation", "--quote", '{"appId":"x"}'])
    out = all_output(result)
    assert result.exit_code == 2
    assert "quote" in out


# --------------------------------------------------------------------------
# --anchor comparison (chain read monkeypatched)
# --------------------------------------------------------------------------
def test_anchor_match_exits_zero(runner, full_env, monkeypatch):
    quote = make_quote_bytes()
    import tokenshare_cli.attestation as att

    monkeypatch.setattr(
        att, "read_anchor_digest", lambda rpc, anchor, appid: quote_digest(quote)
    )
    result = runner.invoke(
        app,
        [
            "verify-attestation",
            "--quote",
            "0x" + quote.hex(),
            "--anchor",
            "0x" + "ee" * 20,
        ],
    )
    out = all_output(result)
    assert result.exit_code == 0, out
    assert "Match: OK" in out
    # app id defaults to the derived address from the quote.
    assert f"appId {TEE_ADDR}" in out


def test_anchor_mismatch_exits_one(runner, full_env, monkeypatch):
    quote = make_quote_bytes()
    import tokenshare_cli.attestation as att

    monkeypatch.setattr(
        att,
        "read_anchor_digest",
        lambda rpc, anchor, appid: "0x" + "11" * 32,
    )
    result = runner.invoke(
        app,
        [
            "verify-attestation",
            "--quote",
            "0x" + quote.hex(),
            "--anchor",
            "0x" + "ee" * 20,
        ],
    )
    out = all_output(result)
    assert result.exit_code == 1, out
    assert "MISMATCH" in out


def test_anchor_zero_digest_warns(runner, full_env, monkeypatch):
    quote = make_quote_bytes()
    import tokenshare_cli.attestation as att

    monkeypatch.setattr(
        att, "read_anchor_digest", lambda rpc, anchor, appid: "0x" + "00" * 32
    )
    result = runner.invoke(
        app,
        [
            "verify-attestation",
            "--quote",
            "0x" + quote.hex(),
            "--anchor",
            "0x" + "ee" * 20,
            "--app-id",
            "0x" + "99" * 20,
        ],
    )
    out = all_output(result)
    assert result.exit_code == 1, out
    assert "no digest anchored" in out
    assert "0x" + "99" * 20 in out  # --app-id override honored


def test_anchor_requires_lookup_id(runner, buyer_env):
    quote = make_quote_bytes(with_binding=False)  # no derived address in quote
    result = runner.invoke(
        app,
        [
            "verify-attestation",
            "--quote",
            "0x" + quote.hex(),
            "--anchor",
            "0x" + "ee" * 20,
        ],
    )
    out = all_output(result)
    assert result.exit_code == 2
    assert "--app-id" in out


# --------------------------------------------------------------------------
# parse_quote unit checks (no CLI)
# --------------------------------------------------------------------------
def test_parse_quote_rejects_empty_and_bad_json():
    with pytest.raises(Exception):
        parse_quote("")
    with pytest.raises(Exception):
        parse_quote("{not json")
    with pytest.raises(Exception):
        parse_quote('{"no_quote_field": 1}')
    with pytest.raises(Exception):
        parse_quote("zzzz")
