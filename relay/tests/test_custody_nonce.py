"""M15 R1 custody nonce tests — issuance format, replace semantics, TTL,
atomic one-time consume (incl. the concurrency race), and the single-mode
503 not_shared_mode face for every custody endpoint."""

from __future__ import annotations

import threading

import pytest
from fastapi.testclient import TestClient

from . import custody_utils as cu
from .conftest import mock_openai_url


@pytest.fixture
def shared_client(monkeypatch: pytest.MonkeyPatch, tmp_path, mock_openai) -> TestClient:
    import relay.app.main as m

    cu.setup_shared_env(monkeypatch, str(tmp_path / "keystore.json"))
    with TestClient(m.app) as c:
        yield c


def _client(monkeypatch: pytest.MonkeyPatch, mock_openai) -> TestClient:
    cu.setup_single_env(monkeypatch, mock_openai_url(mock_openai))
    import relay.app.main as m

    return TestClient(m.app)


# ------------------------------------------------------------------ issuance
def test_nonce_response_shape(shared_client: TestClient) -> None:
    body = cu.get_nonce(shared_client).json() if hasattr(cu.get_nonce(shared_client), "json") else cu.get_nonce(shared_client)
    body = shared_client.get(f"/sellers/nonce/{cu.WALLET.lower()}").json()
    assert body["nonce"].startswith("0x") and len(body["nonce"]) == 66
    int(body["nonce"][2:], 16)
    assert body["expires_at"] - body["issued_at"] == 600
    assert body["origin"] == cu.DEFAULT_ORIGIN
    assert body["chain_id"] == 84532
    assert body["escrow_addr"] == cu.ESCROW_ADDR
    assert body["registry_addr"] == cu.REGISTRY_ADDR
    assert body["mode"] == "shared"


def test_nonce_reissue_replaces(shared_client: TestClient) -> None:
    first = cu.fresh_record(shared_client)
    second = cu.fresh_record(shared_client)
    assert first["nonce"] != second["nonce"]
    # The FIRST nonce no longer exists: consuming it is 409 (replaced).
    raw = cu.revoke_payload(first)
    headers = cu.wallet_headers(cu.revoke_message_for(first, cu.WALLET), seller=cu.WALLET)
    resp = shared_client.request("DELETE", "/sellers/keys", content=raw, headers=headers)
    assert resp.status_code == 409
    assert resp.json()["detail"] == "nonce_invalid"
    # The SECOND nonce is the outstanding one and still works.
    raw2 = cu.revoke_payload(second)
    headers2 = cu.wallet_headers(cu.revoke_message_for(second, cu.WALLET), seller=cu.WALLET)
    resp2 = shared_client.request("DELETE", "/sellers/keys", content=raw2, headers=headers2)
    assert resp2.status_code == 200


def test_nonce_per_address_isolated(shared_client: TestClient) -> None:
    a = cu.fresh_record(shared_client, cu.WALLET)
    b = cu.fresh_record(shared_client, cu.WALLET2)
    assert a["nonce"] != b["nonce"]
    # Wallet2's nonce cannot be consumed by wallet1's request.
    raw = cu.revoke_payload(b)
    headers = cu.wallet_headers(cu.revoke_message_for(b, cu.WALLET), seller=cu.WALLET)
    resp = shared_client.request("DELETE", "/sellers/keys", content=raw, headers=headers)
    assert resp.status_code == 409


def test_nonce_address_normalized_and_validated(shared_client: TestClient) -> None:
    resp = shared_client.get(f"/sellers/nonce/{cu.WALLET.upper()}")
    assert resp.status_code == 200
    bad = shared_client.get("/sellers/nonce/0x1234")
    assert bad.status_code == 400
    assert bad.json()["detail"] == "bad_request"


def test_nonce_ttl_expiry(monkeypatch: pytest.MonkeyPatch, shared_client: TestClient) -> None:
    from relay.app import custody

    record = cu.fresh_record(shared_client)
    # Advance the clock past the TTL.
    monkeypatch.setattr(custody, "_now", lambda: record["expires_at"] + 1)
    raw = cu.revoke_payload(record)
    headers = cu.wallet_headers(cu.revoke_message_for(record, cu.WALLET), seller=cu.WALLET)
    resp = shared_client.request("DELETE", "/sellers/keys", content=raw, headers=headers)
    assert resp.status_code == 409
    assert resp.json()["detail"] == "nonce_invalid"


# ------------------------------------------------------------------- consume
def test_consume_is_one_time(shared_client: TestClient) -> None:
    record = cu.fresh_record(shared_client)
    raw = cu.revoke_payload(record)
    headers = cu.wallet_headers(cu.revoke_message_for(record, cu.WALLET), seller=cu.WALLET)
    first = shared_client.request("DELETE", "/sellers/keys", content=raw, headers=headers)
    assert first.status_code == 200
    # Replay with an identical body: the nonce was consumed once already.
    second = shared_client.request("DELETE", "/sellers/keys", content=raw, headers=headers)
    assert second.status_code == 409
    assert second.json()["detail"] == "nonce_invalid"


def test_concurrent_duplicate_exactly_one_wins(shared_client: TestClient) -> None:
    import relay.app.main as m
    from relay.app import custody

    record = cu.fresh_record(shared_client)
    results: list[str] = []
    barrier = threading.Barrier(2)

    def racer(nonce: str) -> None:
        barrier.wait()
        try:
            m.state.nonces.consume(cu.WALLET.lower(), nonce, record["issued_at"], record["expires_at"])
            results.append("won")
        except custody.CustodyError as exc:
            results.append(exc.code)

    t1 = threading.Thread(target=racer, args=(record["nonce"],))
    t2 = threading.Thread(target=racer, args=(record["nonce"],))
    t1.start()
    t2.start()
    t1.join()
    t2.join()
    assert sorted(results) == ["nonce_invalid", "won"]


def test_nonce_window_echo_mismatch(shared_client: TestClient) -> None:
    record = cu.fresh_record(shared_client)
    # Same nonce, window SHIFTED back 1s (still ≤600 and time-valid) — must
    # fail the RECORD match (server authority), not the window check.
    forged = {
        "nonce": record["nonce"],
        "issued_at": record["issued_at"] - 1,
        "expires_at": record["expires_at"] - 1,
    }
    raw = cu.revoke_payload(forged)
    msg = cu.revoke_message_for(forged, cu.WALLET)
    headers = cu.wallet_headers(msg, seller=cu.WALLET)
    resp = shared_client.request("DELETE", "/sellers/keys", content=raw, headers=headers)
    assert resp.status_code == 409
    assert resp.json()["detail"] == "nonce_invalid"


def test_timestamp_window_too_wide_rejected(shared_client: TestClient) -> None:
    record = cu.fresh_record(shared_client)
    forged = {"nonce": record["nonce"], "issued_at": record["issued_at"], "expires_at": record["expires_at"] + 1}
    raw = cu.revoke_payload(forged)
    msg = cu.revoke_message_for(forged, cu.WALLET)
    headers = cu.wallet_headers(msg, seller=cu.WALLET)
    resp = shared_client.request("DELETE", "/sellers/keys", content=raw, headers=headers)
    assert resp.status_code == 400
    assert resp.json()["detail"] == "bad_request"


# ------------------------------------------------- single-mode custody faces
def test_single_mode_all_custody_endpoints_503(monkeypatch: pytest.MonkeyPatch, mock_openai) -> None:
    client = _client(monkeypatch, mock_openai)
    with client:
        seller = cu.WALLET.lower()
        nonce = client.get(f"/sellers/nonce/{seller}")
        assert nonce.status_code == 503
        assert nonce.json()["detail"] == "not_shared_mode"
        raw = cu.revoke_payload({"nonce": "0x" + "ab" * 32, "issued_at": 1, "expires_at": 2})
        headers = cu.wallet_headers("x", seller=cu.WALLET)
        post = client.post("/sellers/keys", content=raw, headers=headers)
        assert post.status_code == 503
        assert post.json()["detail"] == "not_shared_mode"
        dele = client.request("DELETE", "/sellers/keys", content=raw, headers=headers)
        assert dele.status_code == 503
        status = client.get(f"/sellers/{seller}/status")
        assert status.status_code == 503
        assert status.json()["detail"] == "not_shared_mode"


def test_shared_nonce_unknown_address_shape(shared_client: TestClient) -> None:
    resp = shared_client.get("/sellers/nonce/nothex")
    assert resp.status_code == 400
