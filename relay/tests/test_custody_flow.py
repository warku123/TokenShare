"""M15 R1 custody flow tests — full POST/DELETE pipeline: shape, wallet
auth ordering, action cross-replay, domain binding, crypto tamper faces,
old-key preservation on every failure, and LAST-write-wins replacement."""

from __future__ import annotations

import json
from typing import Any

import pytest
from fastapi.testclient import TestClient

from . import custody_utils as cu
from .conftest import CHAIN_ID, mock_openai_url  # used by shared/S2 tests
from relay.app import custody


@pytest.fixture
def shared_client(monkeypatch: pytest.MonkeyPatch, tmp_path, mock_openai) -> TestClient:
    import relay.app.main as m

    cu.setup_shared_env(monkeypatch, str(tmp_path / "keystore.json"))
    with TestClient(m.app) as c:
        yield c


API_KEY_A = "sk-test-key-AAAAAAAA"
API_KEY_B = "sk-test-key-BBBBBBBB"
UPSTREAM = "http://127.0.0.1:9/v1"  # replaced per-test with the mock URL


def enroll(
    client: TestClient,
    api_key: str,
    upstream: str,
    *,
    seller: str = cu.WALLET,
    key: str = cu.WALLET_KEY,
    origin: str = cu.DEFAULT_ORIGIN,
    record: dict | None = None,
    upload_scalar: int | None = None,
    aad_override: str | None = None,
    msg_override: str | None = None,
) -> Any:
    rec = record or cu.fresh_record(client, seller)
    raw = cu.submit_payload(
        rec, upstream, api_key, seller=seller, upload_scalar=upload_scalar, aad_override=aad_override
    )
    message = msg_override or cu.submit_message_for(rec, raw, upstream, seller, origin=origin)
    headers = cu.wallet_headers(message, key=key, seller=seller)
    return client.post("/sellers/keys", content=raw, headers=headers)


def mock_upstream(mock_openai) -> str:
    return f"http://127.0.0.1:{mock_openai.server_port}"


# ------------------------------------------------------------------ happy path
def test_submit_happy_path(shared_client: TestClient, mock_openai) -> None:
    upstream = mock_upstream(mock_openai)
    resp = enroll(shared_client, API_KEY_A, upstream)
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["seller"] == cu.WALLET.lower()
    assert body["stored"] is True
    assert body["upstream_host"] == "127.0.0.1"
    assert body["official"] is False  # dev custom upstream
    assert body["key_fingerprint"] == custody.sha256_hex(API_KEY_A.encode())
    assert body["catalog"] == [{"model": "gpt-4o-mini", "servable": False}]
    assert body["delegate_authorized"] is False  # never claimed without a read
    # Status view reflects the enrollment.
    status = shared_client.get(f"/sellers/{cu.WALLET.lower()}/status").json()
    assert status["has_key"] is True
    assert status["key_fingerprint"] == body["key_fingerprint"]
    assert status["upstream_host"] == "127.0.0.1"
    assert status["updated_at"] is not None


def test_submit_last_write_wins_replaces(shared_client: TestClient, mock_openai) -> None:
    upstream = mock_upstream(mock_openai)
    first = enroll(shared_client, API_KEY_A, upstream)
    assert first.status_code == 200
    second = enroll(shared_client, API_KEY_B, upstream)
    assert second.status_code == 200
    status = shared_client.get(f"/sellers/{cu.WALLET.lower()}/status").json()
    assert status["key_fingerprint"] == custody.sha256_hex(API_KEY_B.encode())


# ------------------------------------------------------------------- auth
def test_submit_wrong_signer_401(shared_client: TestClient, mock_openai) -> None:
    upstream = mock_upstream(mock_openai)
    resp = enroll(shared_client, API_KEY_A, upstream, key=cu.WALLET2_KEY)
    assert resp.status_code == 401
    assert resp.json()["detail"] == "unauthorized"


def test_submit_missing_headers_401(shared_client: TestClient, mock_openai) -> None:
    upstream = mock_upstream(mock_openai)
    rec = cu.fresh_record(shared_client)
    raw = cu.submit_payload(rec, upstream, API_KEY_A)
    resp = shared_client.post("/sellers/keys", content=raw, headers={"Content-Type": "application/json"})
    assert resp.status_code == 401
    # Seller-only (signature missing) is also 401.
    resp2 = shared_client.post(
        "/sellers/keys",
        content=raw,
        headers={"X-Tokenshare-Seller": cu.WALLET.lower()},
    )
    assert resp2.status_code == 401


def test_submit_tampered_body_hash_401(shared_client: TestClient, mock_openai) -> None:
    """The signature is bound to the EXACT submitted bytes: a body built for
    upstream A signed over message-A but SENT with upstream B (different
    bytes) — body_sha256/normalized-domain mismatch → 401."""
    upstream = mock_upstream(mock_openai)
    rec = cu.fresh_record(shared_client)
    raw_signed = cu.submit_payload(rec, upstream, API_KEY_A)
    msg_for_a = cu.submit_message_for(rec, raw_signed, upstream, cu.WALLET)
    raw_sent = cu.submit_payload(rec, "http://other-host.invalid/v1", API_KEY_A)
    resp = shared_client.post(
        "/sellers/keys", content=raw_sent, headers=cu.wallet_headers(msg_for_a, seller=cu.WALLET)
    )
    assert resp.status_code == 401
    assert resp.json()["detail"] == "unauthorized"


def test_submit_wrong_upstream_domain_401(shared_client: TestClient, mock_openai) -> None:
    """Signed for upstream X, body carries upstream Y — the server-normalized
    domain never matches → 401."""
    upstream = mock_upstream(mock_openai)
    rec = cu.fresh_record(shared_client)
    raw = cu.submit_payload(rec, upstream, API_KEY_A)
    msg = cu.submit_message_for(rec, raw, "http://other-host.example/v1", cu.WALLET)
    headers = cu.wallet_headers(msg, seller=cu.WALLET)
    resp = shared_client.post("/sellers/keys", content=raw, headers=headers)
    assert resp.status_code == 401


def test_submit_wrong_origin_signing_401(shared_client: TestClient, mock_openai) -> None:
    upstream = mock_upstream(mock_openai)
    resp = enroll(shared_client, API_KEY_A, upstream, origin="https://evil.example")
    assert resp.status_code == 401


def test_origin_header_mismatch_409(shared_client: TestClient, mock_openai, monkeypatch) -> None:
    """Untrusted Origin → 409 — only when RELAY_CORS_ORIGINS is explicitly
    pinned (production posture); with the dev "*" default everything passes
    (see test_origin_gate_dev_default_allows_any)."""
    monkeypatch.setenv("RELAY_CORS_ORIGINS", "https://tokenshare-web.vercel.app")
    upstream = mock_upstream(mock_openai)
    rec = cu.fresh_record(shared_client)
    raw = cu.submit_payload(rec, upstream, API_KEY_A)
    msg = cu.submit_message_for(rec, raw, upstream, cu.WALLET)
    headers = {**cu.wallet_headers(msg, seller=cu.WALLET), "Origin": "https://attacker.example"}
    resp = shared_client.post("/sellers/keys", content=raw, headers=headers)
    assert resp.status_code == 409
    assert resp.json()["detail"] == "origin_mismatch"


# ---------------------------------------------------- ora20 origin-gate tests
CROSS_ORIGIN = "https://tokenshare-web.vercel.app"


def _cross_origin_post(client: TestClient, mock_openai, *, origin: str) -> Any:
    upstream = mock_upstream(mock_openai)
    rec = cu.fresh_record(client)
    raw = cu.submit_payload(rec, upstream, API_KEY_A)
    msg = cu.submit_message_for(rec, raw, upstream, cu.WALLET)
    headers = {**cu.wallet_headers(msg, seller=cu.WALLET), "Origin": origin}
    return client.post("/sellers/keys", content=raw, headers=headers)


def _cross_origin_delete(client: TestClient, mock_openai, *, origin: str, seller: str = cu.WALLET) -> Any:
    rec = cu.fresh_record(client, seller)
    raw = cu.revoke_payload(rec)
    msg = cu.revoke_message_for(rec, seller)
    headers = {**cu.wallet_headers(msg, key=cu.WALLET_KEY if seller == cu.WALLET else cu.WALLET2_KEY, seller=seller), "Origin": origin}
    return client.request("DELETE", "/sellers/keys", content=raw, headers=headers)


def test_origin_gate_post_cross_origin_frontend_allowed(
    shared_client: TestClient, mock_openai, monkeypatch
) -> None:
    """Vercel web → Phala relay: a RELAY_CORS_ORIGINS-pinned FRONTEND origin
    passes the custody origin gate (the old equality check wrongly rejected
    this exact case)."""
    monkeypatch.setenv("RELAY_CORS_ORIGINS", CROSS_ORIGIN)
    resp = _cross_origin_post(shared_client, mock_openai, origin=CROSS_ORIGIN)
    assert resp.status_code == 200, resp.text
    assert resp.json()["stored"] is True


def test_origin_gate_delete_cross_origin_frontend_allowed(
    shared_client: TestClient, mock_openai, monkeypatch
) -> None:
    monkeypatch.setenv("RELAY_CORS_ORIGINS", CROSS_ORIGIN)
    assert _cross_origin_post(shared_client, mock_openai, origin=CROSS_ORIGIN).status_code == 200
    resp = _cross_origin_delete(shared_client, mock_openai, origin=CROSS_ORIGIN)
    assert resp.status_code == 200, resp.text
    assert resp.json() == {"seller": cu.WALLET.lower(), "stored": False}


def test_origin_gate_own_relay_origin_allowed(
    shared_client: TestClient, mock_openai, monkeypatch
) -> None:
    """The relay's OWN canonical origin is always in the allowed set."""
    monkeypatch.setenv("RELAY_CORS_ORIGINS", CROSS_ORIGIN)
    resp = _cross_origin_post(shared_client, mock_openai, origin=cu.DEFAULT_ORIGIN)
    assert resp.status_code == 200, resp.text


def test_origin_gate_dev_default_allows_any(
    shared_client: TestClient, mock_openai, monkeypatch
) -> None:
    """Unset RELAY_CORS_ORIGINS keeps the EXISTING dev/demo "*" semantics
    (same face the CORSMiddleware already applies) — no new restriction, no
    new * beyond the existing default."""
    monkeypatch.delenv("RELAY_CORS_ORIGINS", raising=False)
    resp = _cross_origin_post(shared_client, mock_openai, origin="https://anything.example")
    assert resp.status_code == 200, resp.text


def test_origin_gate_delete_untrusted_409(
    shared_client: TestClient, mock_openai, monkeypatch
) -> None:
    monkeypatch.setenv("RELAY_CORS_ORIGINS", CROSS_ORIGIN)
    resp = _cross_origin_delete(shared_client, mock_openai, origin="https://attacker.example")
    assert resp.status_code == 409
    assert resp.json()["detail"] == "origin_mismatch"


def test_signing_relay_domain_still_server_authority(
    shared_client: TestClient, mock_openai, monkeypatch
) -> None:
    """The signed relay domain comes from CONFIG ONLY — a caller presenting
    an ALLOWED Origin header but signing a DIFFERENT relay value is 401;
    Origin/Host can never override or redirect the signed domain."""
    monkeypatch.setenv("RELAY_CORS_ORIGINS", CROSS_ORIGIN)
    upstream = mock_upstream(mock_openai)
    rec = cu.fresh_record(shared_client)
    raw = cu.submit_payload(rec, upstream, API_KEY_A)
    # Signed with the relay domain of ANOTHER relay instance (still an
    # allowed-origin header on the wire).
    msg = cu.submit_message_for(rec, raw, upstream, cu.WALLET, origin="https://other-relay.example")
    headers = {**cu.wallet_headers(msg, seller=cu.WALLET), "Origin": CROSS_ORIGIN}
    resp = shared_client.post("/sellers/keys", content=raw, headers=headers)
    assert resp.status_code == 401
    assert resp.json()["detail"] == "unauthorized"


def test_preflight_and_actual_sets_identical(
    shared_client: TestClient, mock_openai, monkeypatch
) -> None:
    """Preflight/actual consistency BY CONSTRUCTION: the CORSMiddleware's
    allow_origins (import-time build via _effective_cors_origins) and the
    custody handler's origin gate (custody_allowed_origins) are derived
    from the SAME parse_cors_origins(RELAY_CORS_ORIGINS) + the relay's own
    canonical origin — no preflight-OK/actual-409 split is possible."""
    from relay.app.custody import custody_allowed_origins
    from relay.app.main import _effective_cors_origins

    # Pinned production posture.
    monkeypatch.setenv("RELAY_CORS_ORIGINS", CROSS_ORIGIN)
    middleware = _effective_cors_origins()
    handler = custody_allowed_origins(cu.DEFAULT_ORIGIN)
    assert handler != "*"
    assert {o.lower() for o in middleware} == {o.lower().rstrip("/") for o in handler}
    assert CROSS_ORIGIN in middleware
    assert cu.DEFAULT_ORIGIN in middleware

    # Dev/demo default: BOTH faces are the existing "*" semantics.
    monkeypatch.delenv("RELAY_CORS_ORIGINS", raising=False)
    assert _effective_cors_origins() == ["*"]
    assert custody_allowed_origins(cu.DEFAULT_ORIGIN) == "*"

    # Already-pinned list containing the own origin: no duplication, no change.
    monkeypatch.setenv("RELAY_CORS_ORIGINS", f"{CROSS_ORIGIN},{cu.DEFAULT_ORIGIN}")
    assert _effective_cors_origins() == [CROSS_ORIGIN, cu.DEFAULT_ORIGIN]
    assert custody_allowed_origins(cu.DEFAULT_ORIGIN) == [CROSS_ORIGIN, cu.DEFAULT_ORIGIN]


def test_preflight_and_actual_consistent_dev_star(
    shared_client: TestClient, mock_openai, monkeypatch
) -> None:
    """Dev "*": preflight echoes * and the actual call accepts any origin —
    consistent both ways (runnable end-to-end: the middleware was built
    with the default ["*"])."""
    monkeypatch.delenv("RELAY_CORS_ORIGINS", raising=False)
    for method in ("POST", "DELETE"):
        pre = shared_client.options(
            "/sellers/keys",
            headers={
                "Origin": "https://arbitrary.example",
                "Access-Control-Request-Method": method,
            },
        )
        assert pre.status_code == 200, pre.text
        assert pre.headers.get("access-control-allow-origin") == "*"
    resp = _cross_origin_post(shared_client, mock_openai, origin="https://arbitrary.example")
    assert resp.status_code == 200, resp.text
    dele = _cross_origin_delete(shared_client, mock_openai, origin="https://arbitrary.example")
    assert dele.status_code in (200, 409)  # 409 only when no key/no nonce — not an origin face
    if dele.status_code == 409:
        assert dele.json()["detail"] != "origin_mismatch"


def test_action_cross_replay_401(shared_client: TestClient, mock_openai) -> None:
    """A DELETE (revoke) signature replayed on POST — and the reverse — is a
    401: the recovered signer over the other action's message differs."""
    upstream = mock_upstream(mock_openai)
    rec = cu.fresh_record(shared_client)
    raw_post = cu.submit_payload(rec, upstream, API_KEY_A)
    revoke_msg = cu.revoke_message_for(rec, cu.WALLET)
    post_with_revoke_sig = {
        "X-Tokenshare-Seller": cu.WALLET.lower(),
        "X-Tokenshare-Signature": cu.sign_eip191(revoke_msg, cu.WALLET_KEY),
        "Content-Type": "application/json",
    }
    resp = shared_client.post("/sellers/keys", content=raw_post, headers=post_with_revoke_sig)
    assert resp.status_code == 401

    raw_del = cu.revoke_payload(rec)
    submit_msg = cu.submit_message_for(rec, raw_post, upstream, cu.WALLET)
    del_with_submit_sig = {
        "X-Tokenshare-Seller": cu.WALLET.lower(),
        "X-Tokenshare-Signature": cu.sign_eip191(submit_msg, cu.WALLET_KEY),
        "Content-Type": "application/json",
    }
    resp2 = shared_client.request("DELETE", "/sellers/keys", content=raw_del, headers=del_with_submit_sig)
    assert resp2.status_code == 401


# -------------------------------------------------------------------- shape
def test_submit_non_compact_body_400(shared_client: TestClient, mock_openai) -> None:
    upstream = mock_upstream(mock_openai)
    rec = cu.fresh_record(shared_client)
    raw = cu.submit_payload(rec, upstream, API_KEY_A)
    spaced = json.dumps(json.loads(raw), separators=(", ", ": ")).encode()
    msg = cu.submit_message_for(rec, raw, upstream, cu.WALLET)
    resp = shared_client.post(
        "/sellers/keys", content=spaced, headers=cu.wallet_headers(msg, seller=cu.WALLET)
    )
    assert resp.status_code == 400
    assert resp.json()["detail"] == "bad_request"


def test_submit_wrong_key_order_400(shared_client: TestClient, mock_openai) -> None:
    upstream = mock_upstream(mock_openai)
    rec = cu.fresh_record(shared_client)
    raw = cu.submit_payload(rec, upstream, API_KEY_A)
    obj = json.loads(raw)
    reordered = {
        "issued_at": obj["issued_at"],
        "nonce": obj["nonce"],
        "expires_at": obj["expires_at"],
        "upstream_base_url": obj["upstream_base_url"],
        "envelope": obj["envelope"],
    }
    bad = json.dumps(reordered, separators=(",", ":")).encode()
    msg = cu.submit_message_for(rec, raw, upstream, cu.WALLET)
    resp = shared_client.post(
        "/sellers/keys", content=bad, headers=cu.wallet_headers(msg, seller=cu.WALLET)
    )
    assert resp.status_code == 400


def test_submit_bool_timestamp_400(shared_client: TestClient, mock_openai) -> None:
    upstream = mock_upstream(mock_openai)
    rec = cu.fresh_record(shared_client)
    raw = cu.submit_payload(rec, upstream, API_KEY_A)
    obj = json.loads(raw)
    obj["issued_at"] = True
    bad = json.dumps(obj, separators=(",", ":")).encode()
    msg = cu.submit_message_for(rec, raw, upstream, cu.WALLET)
    resp = shared_client.post(
        "/sellers/keys", content=bad, headers=cu.wallet_headers(msg, seller=cu.WALLET)
    )
    assert resp.status_code == 400


def test_submit_oversize_body_400(shared_client: TestClient, mock_openai) -> None:
    upstream = mock_upstream(mock_openai)
    rec = cu.fresh_record(shared_client)
    raw = cu.submit_payload(rec, upstream, "sk-" + "A" * (64 * 1024))
    msg = cu.submit_message_for(rec, raw, upstream, cu.WALLET)
    resp = shared_client.post(
        "/sellers/keys", content=raw, headers=cu.wallet_headers(msg, seller=cu.WALLET)
    )
    assert resp.status_code == 400


def test_submit_bad_nonce_format_400(shared_client: TestClient, mock_openai) -> None:
    upstream = mock_upstream(mock_openai)
    rec = {"nonce": "0x" + "ab" * 10, "issued_at": 1, "expires_at": 601}
    raw = cu.submit_payload(rec, upstream, API_KEY_A)
    msg = cu.submit_message_for(rec, raw, upstream, cu.WALLET)
    resp = shared_client.post(
        "/sellers/keys", content=raw, headers=cu.wallet_headers(msg, seller=cu.WALLET)
    )
    assert resp.status_code == 400


# ---------------------------------------------------- S2-C2 malformed ports
def test_normalize_custody_upstream_rejects_malformed_ports() -> None:
    """Out-of-range / non-numeric ports raise the CUSTODY 400 bad_request
    face (CustodyError) — never an unhandled ValueError (500)."""
    for bad in (
        "http://127.0.0.1:70000/v1",
        "http://127.0.0.1:notaport/v1",
        "https://api.kimi.com:70000/coding",
        "https://api.kimi.com:notaport",
    ):
        with pytest.raises(custody.CustodyError) as exc:
            custody.normalize_custody_upstream(bad)
        assert exc.value.status == 400
        assert exc.value.code == "bad_request"


def test_submit_malformed_port_upstream_400_preserves_key(
    shared_client: TestClient, mock_openai
) -> None:
    """A SIGNED encrypted custody submission whose upstream carries a
    malformed port fails 400 bad_request (NOT 500): the normalizer rejects
    it before auth/consume/decrypt; the previously stored valid key is
    preserved and the nonce is NOT consumed."""
    upstream = mock_upstream(mock_openai)
    assert enroll(shared_client, API_KEY_A, upstream).status_code == 200
    fp_before = shared_client.get(f"/sellers/{cu.WALLET.lower()}/status").json()["key_fingerprint"]

    rec = cu.fresh_record(shared_client)
    # aad_override → the helper skips its own normalization of the bad URL;
    # the message binds the RAW value (as a naive client would sign it).
    raw = cu.submit_payload(rec, "http://127.0.0.1:70000/v1", API_KEY_A, aad_override="unused")
    msg = custody.build_submit_message(
        seller=cu.WALLET.lower(),
        chain_id=CHAIN_ID,
        escrow_addr=cu.ESCROW_ADDR,
        registry_addr=cu.REGISTRY_ADDR,
        origin=cu.DEFAULT_ORIGIN,
        upstream_base_url="http://127.0.0.1:70000/v1",
        nonce=rec["nonce"],
        body_sha256=custody.sha256_hex(raw),
        issued=rec["issued_at"],
        expires=rec["expires_at"],
    )
    resp = shared_client.post(
        "/sellers/keys", content=raw, headers=cu.wallet_headers(msg, seller=cu.WALLET)
    )
    assert resp.status_code == 400, resp.text
    assert resp.json()["detail"] == "bad_request"
    # Old valid key preserved; nonce NOT consumed (still usable).
    status = shared_client.get(f"/sellers/{cu.WALLET.lower()}/status").json()
    assert status["has_key"] is True and status["key_fingerprint"] == fp_before
    raw_ok = cu.submit_payload(rec, upstream, API_KEY_B)
    msg_ok = cu.submit_message_for(rec, raw_ok, upstream, cu.WALLET)
    resp_ok = shared_client.post(
        "/sellers/keys", content=raw_ok, headers=cu.wallet_headers(msg_ok, seller=cu.WALLET)
    )
    assert resp_ok.status_code == 200, resp_ok.text
    assert resp_ok.json()["key_fingerprint"] == custody.sha256_hex(API_KEY_B.encode())


# ------------------------------------------------------------------- crypto
def test_submit_wrong_aad_envelope_invalid(shared_client: TestClient, mock_openai) -> None:
    """Envelope encrypted under a DIFFERENT AAD (the errata case: the upload
    AAD is independent — tampering it must fail authentication)."""
    upstream = mock_upstream(mock_openai)
    resp = enroll(
        shared_client, API_KEY_A, upstream, aad_override="TokenShare custody envelope|action=submit|tampered=1"
    )
    assert resp.status_code == 400
    assert resp.json()["detail"] == "envelope_invalid"


def test_submit_message_cannot_serve_as_aad(shared_client: TestClient, mock_openai) -> None:
    """The submit MESSAGE (with body hash) is NOT the envelope AAD — using
    it as the AAD fails (prefix/class separation)."""
    upstream = mock_upstream(mock_openai)
    rec = cu.fresh_record(shared_client)
    raw = cu.submit_payload(rec, upstream, API_KEY_A)
    wrong_aad = cu.submit_message_for(rec, raw, upstream, cu.WALLET)
    resp = enroll(shared_client, API_KEY_A, upstream, record=rec, aad_override=wrong_aad)
    assert resp.status_code == 400
    assert resp.json()["detail"] == "envelope_invalid"


def test_submit_wrong_upload_key_envelope_invalid(shared_client: TestClient, mock_openai) -> None:
    upstream = mock_upstream(mock_openai)
    resp = enroll(shared_client, API_KEY_A, upstream, upload_scalar="0x" + "9" * 64)
    assert resp.status_code == 400
    assert resp.json()["detail"] == "envelope_invalid"


def _resigned_post(client: TestClient, rec: dict, upstream: str, obj: dict, seller: str = cu.WALLET) -> Any:
    """Serialize a MODIFIED body and sign its ACTUAL bytes (the signature
    must cover what is sent — crypto-face tests re-sign)."""
    bad = json.dumps(obj, separators=(",", ":"), ensure_ascii=True).encode("ascii")
    msg = cu.submit_message_for(rec, bad, upstream, seller)
    return client.post("/sellers/keys", content=bad, headers=cu.wallet_headers(msg, seller=seller))


def test_submit_off_curve_epk_envelope_invalid(
    shared_client: TestClient, mock_openai, monkeypatch
) -> None:
    upstream = mock_upstream(mock_openai)
    rec = cu.fresh_record(shared_client)
    raw = cu.submit_payload(rec, upstream, API_KEY_A)
    obj = json.loads(raw)
    # A 65-byte 0x04-prefixed but off-curve point.
    bad_point = b"\x04" + bytes.fromhex("11" * 32 + "12" * 32)
    obj["envelope"]["epk"] = custody.b64url_encode(bad_point)
    resp = _resigned_post(shared_client, rec, upstream, obj)
    assert resp.status_code == 400
    assert resp.json()["detail"] == "envelope_invalid"


def test_submit_bad_iv_and_truncated_ct(
    shared_client: TestClient, mock_openai
) -> None:
    upstream = mock_upstream(mock_openai)
    rec = cu.fresh_record(shared_client)
    raw = cu.submit_payload(rec, upstream, API_KEY_A)
    obj = json.loads(raw)
    obj["envelope"]["iv"] = custody.b64url_encode(b"\x00" * 8)
    resp = _resigned_post(shared_client, rec, upstream, obj)
    assert resp.status_code == 400
    assert resp.json()["detail"] == "envelope_invalid"

    obj2 = json.loads(raw)
    rec2 = cu.fresh_record(shared_client)  # fresh nonce: case 1 consumed one
    raw2 = cu.submit_payload(rec2, upstream, API_KEY_A)
    obj2 = json.loads(raw2)
    obj2["envelope"]["ct"] = custody.b64url_encode(b"\x00" * 8)
    resp2 = _resigned_post(shared_client, rec2, upstream, obj2)
    assert resp2.status_code == 400
    assert resp2.json()["detail"] == "envelope_invalid"


def test_submit_wrong_alg_envelope_invalid(shared_client: TestClient, mock_openai) -> None:
    upstream = mock_upstream(mock_openai)
    rec = cu.fresh_record(shared_client)
    raw = cu.submit_payload(rec, upstream, API_KEY_A)
    obj = json.loads(raw)
    obj["envelope"]["alg"] = "ECDH-P256-HKDF-SHA256-A256GCM-v0"
    resp = _resigned_post(shared_client, rec, upstream, obj)
    assert resp.status_code == 400
    assert resp.json()["detail"] == "envelope_invalid"


def test_submit_plaintext_shape_enforced(shared_client: TestClient, mock_openai, monkeypatch) -> None:
    """A valid envelope whose plaintext is NOT {\"api_key\": \"…\"} compact
    ASCII → 400 envelope_invalid (shape gate after decrypt)."""
    from .conftest import CHAIN_ID as CID

    upstream = mock_upstream(mock_openai)
    rec = cu.fresh_record(shared_client)
    # Build an envelope with a WRONG plaintext shape but the CORRECT AAD.
    aad = custody.build_upload_aad(
        seller=cu.WALLET.lower(),
        chain_id=CID,
        escrow_addr=cu.ESCROW_ADDR,
        registry_addr=cu.REGISTRY_ADDR,
        origin=cu.DEFAULT_ORIGIN,
        upstream_base_url=custody.normalize_custody_upstream(upstream),
        nonce=rec["nonce"],
        issued=rec["issued_at"],
        expires=rec["expires_at"],
    )
    env = custody.encrypt_envelope(
        int(cu.SHARED_UPLOAD_KEY, 16), b'{"api_key":"x","extra":1}', aad
    )
    obj = {
        "nonce": rec["nonce"],
        "issued_at": rec["issued_at"],
        "expires_at": rec["expires_at"],
        "upstream_base_url": upstream,
        "envelope": env,
    }
    raw = json.dumps(obj, separators=(",", ":"), ensure_ascii=True).encode()
    msg = cu.submit_message_for(rec, raw, upstream, cu.WALLET)
    resp = shared_client.post("/sellers/keys", content=raw, headers=cu.wallet_headers(msg, seller=cu.WALLET))
    assert resp.status_code == 400
    assert resp.json()["detail"] == "envelope_invalid"


# ----------------------------------------------------- upstream probe faces
def test_submit_upstream_key_rejected_preserves_old(
    shared_client: TestClient, mock_openai
) -> None:
    upstream = mock_upstream(mock_openai)
    assert enroll(shared_client, API_KEY_A, upstream).status_code == 200
    fp_a = shared_client.get(f"/sellers/{cu.WALLET.lower()}/status").json()["key_fingerprint"]

    mock_openai.models_status = "401"
    resp = enroll(shared_client, API_KEY_B, upstream)
    assert resp.status_code == 400
    assert resp.json()["detail"] == "upstream_key_rejected"
    # Old valid key preserved everywhere.
    status = shared_client.get(f"/sellers/{cu.WALLET.lower()}/status").json()
    assert status["key_fingerprint"] == fp_a
    assert status["has_key"] is True


def test_submit_upstream_5xx_502(shared_client: TestClient, mock_openai) -> None:
    upstream = mock_upstream(mock_openai)
    rec = cu.fresh_record(shared_client)
    mock_openai.models_status = "500"
    resp = enroll(shared_client, API_KEY_A, upstream, record=rec)
    assert resp.status_code == 502
    assert resp.json()["detail"] == "upstream_unreachable"


def test_submit_upstream_redirect_502_ssrf(
    shared_client: TestClient, mock_openai, monkeypatch
) -> None:
    """A 3xx must NEVER be followed (SSRF hardening): 502
    upstream_unreachable, and no request reaches the redirect target."""
    import httpx

    seen: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(str(request.url))
        if request.url.path.endswith("/v1/models") and not seen.count(str(request.url)) > 1:
            return httpx.Response(302, headers={"location": "https://attacker.example/v1/models"})
        return httpx.Response(200, json={"data": [{"id": "gpt-4o-mini"}]})

    monkeypatch.setattr(custody, "_probe_transport", httpx.MockTransport(handler))
    resp = enroll(shared_client, API_KEY_A, "https://api.kimi.com/coding")
    assert resp.status_code == 502
    assert resp.json()["detail"] == "upstream_unreachable"
    # Exactly ONE request was made — nothing followed the redirect.
    assert len(seen) == 1


# ------------------------------------------------------------------- DELETE
def test_revoke_flow_idempotent(shared_client: TestClient, mock_openai) -> None:
    upstream = mock_upstream(mock_openai)
    assert enroll(shared_client, API_KEY_A, upstream).status_code == 200
    record = cu.fresh_record(shared_client)
    raw = cu.revoke_payload(record)
    headers = cu.wallet_headers(cu.revoke_message_for(record, cu.WALLET), seller=cu.WALLET)
    resp = shared_client.request("DELETE", "/sellers/keys", content=raw, headers=headers)
    assert resp.status_code == 200
    assert resp.json() == {"seller": cu.WALLET.lower(), "stored": False}
    status = shared_client.get(f"/sellers/{cu.WALLET.lower()}/status").json()
    assert status["has_key"] is False

    # Idempotent: revoking again (fresh nonce) is another clean 200.
    record2 = cu.fresh_record(shared_client)
    raw2 = cu.revoke_payload(record2)
    headers2 = cu.wallet_headers(cu.revoke_message_for(record2, cu.WALLET), seller=cu.WALLET)
    resp2 = shared_client.request("DELETE", "/sellers/keys", content=raw2, headers=headers2)
    assert resp2.status_code == 200
    assert resp2.json()["stored"] is False


def test_revoke_never_touches_other_sellers(
    shared_client: TestClient, mock_openai
) -> None:
    upstream = mock_upstream(mock_openai)
    assert enroll(shared_client, API_KEY_A, upstream).status_code == 200
    assert enroll(shared_client, API_KEY_B, upstream, seller=cu.WALLET2, key=cu.WALLET2_KEY).status_code == 200
    record = cu.fresh_record(shared_client, cu.WALLET)
    raw = cu.revoke_payload(record)
    headers = cu.wallet_headers(cu.revoke_message_for(record, cu.WALLET), seller=cu.WALLET)
    assert shared_client.request("DELETE", "/sellers/keys", content=raw, headers=headers).status_code == 200
    assert shared_client.get(f"/sellers/{cu.WALLET2.lower()}/status").json()["has_key"] is True
    assert shared_client.get(f"/sellers/{cu.WALLET.lower()}/status").json()["has_key"] is False


# --------------------------------------------------------- endpoint sanity
def test_custody_routes_exist_in_single_mode_too(monkeypatch: pytest.MonkeyPatch, mock_openai) -> None:
    """The routes are REGISTERED in single mode (they answer 503) — no
    silent removal of the custody API surface."""
    cu.setup_single_env(monkeypatch, mock_openai_url(mock_openai))
    import relay.app.main as m

    with TestClient(m.app):
        paths = {getattr(r, "path", "") for r in m.app.routes}
        assert "/sellers/keys" in paths
        assert "/sellers/nonce/{address}" in paths
        assert "/sellers/{address}/status" in paths
