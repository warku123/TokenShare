"""M15 R1 custody crypto-vector tests — the persisted SYNTHETIC fixture
(relay/tests/vectors/custody_vector.json, generated ONCE with random test
keys) must decrypt to the recorded plaintext and re-encrypt byte-exactly.
The message/AAD strings are also reproduced from the module builders."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from relay.app import custody

VECTOR_PATH = Path(__file__).resolve().parent / "vectors" / "custody_vector.json"


@pytest.fixture(scope="module")
def vector() -> dict:
    return json.loads(VECTOR_PATH.read_text())


def test_vector_is_synthetic_marker(vector: dict) -> None:
    assert "synthetic" in vector["note"]
    assert "never production" in vector["note"]
    assert vector["schema"] == 1
    assert vector["alg"] == custody.ALG_ID


def test_vector_decrypt_matches_plaintext(vector: dict) -> None:
    upload_priv = int(vector["upload_priv_hex"], 16)
    envelope = {
        "alg": vector["alg"],
        "epk": vector["expected"]["epk_b64url"],
        "iv": vector["expected"]["iv_b64url"],
        "ct": vector["expected"]["ct_b64url"],
    }
    plaintext = custody.decrypt_envelope(upload_priv, envelope, vector["aad_ascii"])
    assert plaintext.decode("ascii") == vector["plaintext_json"]
    assert custody.parse_envelope_plaintext(plaintext) == json.loads(vector["plaintext_json"])["api_key"]


def test_vector_deterministic_reencryption(vector: dict) -> None:
    """Same eph scalar + IV → byte-identical epk/iv/ct (reproducibility)."""
    upload_priv = int(vector["upload_priv_hex"], 16)
    eph_priv = int(vector["eph_priv_hex"], 16)
    env = custody.encrypt_envelope(
        upload_priv,
        vector["plaintext_json"].encode("ascii"),
        vector["aad_ascii"],
        eph_scalar=eph_priv,
        iv=custody.b64url_decode_strict(vector["iv_b64url"], 12, "vector"),
    )
    assert env["epk"] == vector["expected"]["epk_b64url"]
    assert env["iv"] == vector["expected"]["iv_b64url"]
    assert env["ct"] == vector["expected"]["ct_b64url"]


def test_vector_upload_pub_matches_private(vector: dict) -> None:
    upload_priv = int(vector["upload_priv_hex"], 16)
    assert custody.upload_public_bytes(upload_priv) == custody.b64url_decode_strict(
        vector["upload_pub_b64url"], 65, "vector"
    )


def test_vector_aad_rebuilt_from_fields(vector: dict) -> None:
    rebuilt = custody.build_upload_aad(**vector["aad_fields"])
    assert rebuilt == vector["aad_ascii"]
    # The AAD is an INDEPENDENT string: no body hash inside it.
    assert "body_sha256" not in vector["aad_ascii"]
    # And it is NOT a submit message (different class/prefix).
    assert not vector["aad_ascii"].startswith("TokenShare key custody")


def test_vector_raw_body_is_canonical_and_hash_is_independent(vector: dict) -> None:
    """ora19 BLOCK fix — the hash is verified INDEPENDENTLY: the canonical
    raw body is reconstructed from the fixture's own structural fields,
    re-serialized compact-ASCII, hashed, and compared against body_sha256 —
    the hash is NEVER extracted from the message as an input."""
    body = json.loads(vector["raw_body"])
    # Exact key order/set of the canonical POST body.
    assert list(body.keys()) == ["nonce", "issued_at", "expires_at", "upstream_base_url", "envelope"]
    assert list(body["envelope"].keys()) == ["alg", "epk", "iv", "ct"]
    # Structural fields agree with aad_fields (one coherent request).
    assert body["nonce"] == vector["aad_fields"]["nonce"]
    assert body["issued_at"] == vector["aad_fields"]["issued"]
    assert body["expires_at"] == vector["aad_fields"]["expires"]
    assert custody.normalize_custody_upstream(body["upstream_base_url"]) == vector["aad_fields"]["upstream_base_url"]
    assert body["envelope"]["alg"] == vector["alg"]
    assert body["envelope"]["epk"] == vector["expected"]["epk_b64url"]
    assert body["envelope"]["iv"] == vector["expected"]["iv_b64url"]
    assert body["envelope"]["ct"] == vector["expected"]["ct_b64url"]
    # Compact-ASCII round-trip reproduces the recorded raw bytes.
    rebuilt_raw = json.dumps(body, separators=(",", ":"), ensure_ascii=True).encode("ascii")
    assert rebuilt_raw.decode("ascii") == vector["raw_body"]
    # INDEPENDENT hash of the raw bytes == the fixture's body_sha256.
    assert custody.sha256_hex(rebuilt_raw) == vector["body_sha256"]
    # The message binds EXACTLY that hash (no self-extraction).
    assert f"|body_sha256={vector['body_sha256']}|" in vector["message"]


def test_vector_message_rebuilt_from_computed_hash(vector: dict) -> None:
    """The message is rebuilt from the INDEPENDENTLY computed hash (never
    parsed back out of the message string)."""
    raw = vector["raw_body"].encode("ascii")
    computed = custody.sha256_hex(raw)
    rebuilt = custody.build_submit_message(**vector["aad_fields"], body_sha256=computed)
    assert rebuilt == vector["message"]
    # Single line: no newlines/carriage returns, no padding spaces.
    assert "\n" not in vector["message"]
    assert "\r" not in vector["message"]
    assert vector["message"] == vector["message"].strip()
    assert "  " not in vector["message"]  # no doubled spaces
    # Field/value segments carry no stray spaces around '='.
    for segment in vector["message"].split("|")[1:]:
        key, _, value = segment.partition("=")
        assert value == value.strip() and key == key.strip()


def test_vector_eoa_signature_recovers_signer(vector: dict) -> None:
    """Synthetic EOA: the recorded signature recovers the recorded signer —
    which IS the custody seller bound in the AAD/message."""
    from eth_account import Account
    from eth_account.messages import encode_defunct

    signer = Account.from_key(vector["eoa_priv_hex"])
    assert signer.address == vector["signer"]  # EIP-55 checksummed, as ethers.verifyMessage returns
    assert vector["signer"].lower() == vector["aad_fields"]["seller"]
    recovered = Account.recover_message(
        encode_defunct(text=vector["message"]), signature=vector["signature"]
    )
    assert str(recovered).lower() == vector["signer"].lower()
    # The signature does NOT verify over a different message (tamper face).
    tampered = vector["message"].replace("action=submit", "action=revoke")
    other = Account.recover_message(encode_defunct(text=tampered), signature=vector["signature"])
    assert str(other).lower() != vector["signer"].lower()


def test_vector_no_production_material(vector: dict) -> None:
    """All secrets are random synthetic test data with the synthetic marker;
    nothing here is a real deployed/production key."""
    assert "synthetic" in vector["note"] and "never production" in vector["note"]
    for field in ("upload_priv_hex", "eph_priv_hex", "eoa_priv_hex"):
        raw = bytes.fromhex(vector[field][2:])
        assert len(raw) == 32 and raw != b"\x00" * 32


# ---------------------------------------------------------------------------
# End-to-end: the vector IS a real request — driven through the HTTP API
# (also proves the cross-origin + hash/signature semantics of the ora
# fixes together). Web/console interop reference.
# ---------------------------------------------------------------------------
import httpx  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402

from . import custody_utils as cu  # noqa: E402
import relay.app.main as m  # noqa: E402


def test_vector_drives_a_real_enrollment(
    monkeypatch: pytest.MonkeyPatch, tmp_path, mock_openai
) -> None:
    vector = json.loads(VECTOR_PATH.read_text())

    def handler(request: httpx.Request) -> httpx.Response:
        assert str(request.url) == "https://api.kimi.com/coding/v1/models"
        assert request.headers["Authorization"] == (
            "Bearer " + json.loads(vector["plaintext_json"])["api_key"]
        )
        return httpx.Response(200, json={"data": [{"id": "kimi-k2-instruct"}, {"id": "gpt-4o-mini"}]})

    monkeypatch.setattr(custody, "_probe_transport", httpx.MockTransport(handler))
    cu.setup_shared_env(
        monkeypatch,
        str(tmp_path / "keystore.json"),
        # ora20 fix: the configured FRONTEND origin is allowed cross-origin.
        cors_origins="https://tokenshare-web.vercel.app",
    )
    # The relay's upload key must be the VECTOR's upload key (the envelope
    # in raw_body was encrypted against it; KEK/signer stay env defaults).
    monkeypatch.setenv("SHARED_UPLOAD_KEY", vector["upload_priv_hex"])
    with TestClient(m.app) as client:
        # The vector's window is fixed in the past relative to the wall
        # clock — freeze the custody clock INSIDE it (deterministic TTL).
        monkeypatch.setattr(
            custody, "_now", lambda: vector["aad_fields"]["issued"] + 300
        )
        # Seed the vector's fixed nonce window as the outstanding record.
        from relay.app.custody import NonceRecord

        with m.state.nonces._lock:
            m.state.nonces._records[vector["aad_fields"]["seller"]] = NonceRecord(
                nonce=vector["aad_fields"]["nonce"],
                issued_at=vector["aad_fields"]["issued"],
                expires_at=vector["aad_fields"]["expires"],
            )

        resp = client.post(
            "/sellers/keys",
            content=vector["raw_body"].encode("ascii"),
            headers={
                "X-Tokenshare-Seller": vector["signer"],
                "X-Tokenshare-Signature": vector["signature"],
                "Origin": "https://tokenshare-web.vercel.app",  # cross-origin ✓
                "Content-Type": "application/json",
            },
        )
        assert resp.status_code == 200, resp.text
        body = resp.json()
        assert body["stored"] is True
        assert body["seller"] == vector["aad_fields"]["seller"]
        assert body["official"] is True
        assert body["key_fingerprint"] == custody.sha256_hex(
            json.loads(vector["plaintext_json"])["api_key"].encode("ascii")
        )
        assert body["catalog"] == [
            {"model": "kimi-k2-instruct", "servable": True},
            {"model": "gpt-4o-mini", "servable": False},
        ]
        # Key continuity: the stored entry unwraps to the vector plaintext.
        assert m.state.keystore.unwrap_api_key(vector["aad_fields"]["seller"]) == (
            json.loads(vector["plaintext_json"])["api_key"]
        )
