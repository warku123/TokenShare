"""M15 R1 custody status/binding tests — public status view fields, honest
chain-binding snapshot (never claims true without a successful read),
pre-listing enrollment, and unknown-seller faces."""

from __future__ import annotations

import pytest
from typing import Any
from fastapi.testclient import TestClient

from . import custody_utils as cu
import relay.app.main as m


@pytest.fixture
def shared_client(monkeypatch: pytest.MonkeyPatch, tmp_path, mock_openai) -> TestClient:
    cu.setup_shared_env(monkeypatch, str(tmp_path / "keystore.json"))
    monkeypatch.setattr(m, "ChainClient", cu.FakeSharedChain)
    cu.FakeSharedChain.reset()
    with TestClient(m.app) as c:
        yield c


def enroll(client: TestClient, api_key: str, upstream: str, seller: str = cu.WALLET,
           key: str = cu.WALLET_KEY) -> Any:
    rec = cu.fresh_record(client, seller)
    raw = cu.submit_payload(rec, upstream, api_key, seller=seller)
    msg = cu.submit_message_for(rec, raw, upstream, seller)
    return client.post(
        "/sellers/keys", content=raw, headers=cu.wallet_headers(msg, key=key, seller=seller)
    )


def mock_upstream(mock_openai) -> str:
    return f"http://127.0.0.1:{mock_openai.server_port}"


# ------------------------------------------------------------------- unknown
def test_status_unknown_seller(shared_client: TestClient) -> None:
    resp = shared_client.get(f"/sellers/{cu.WALLET.lower()}/status")
    assert resp.status_code == 200
    body = resp.json()
    assert body["mode"] == "shared"
    assert body["has_key"] is False
    assert body["key_fingerprint"] is None
    assert body["upstream_host"] is None
    assert body["upstream_official"] is False
    assert body["catalog"] == []
    assert body["listing_bound"] is False
    assert body["listing_active"] is False
    assert body["delegate_authorized"] is False
    assert body["listing_models"] == []
    assert body["updated_at"] is None


def test_status_invalid_address_400(shared_client: TestClient) -> None:
    resp = shared_client.get("/sellers/notanaddress/status")
    assert resp.status_code == 400
    assert resp.json()["detail"] == "bad_request"


def test_status_not_authenticated_public(shared_client: TestClient) -> None:
    """No wallet headers required — the status view is public."""
    resp = shared_client.get(f"/sellers/{cu.WALLET.lower()}/status")
    assert resp.status_code == 200


# ---------------------------------------------------------------- enrollment
def test_status_after_enrollment(shared_client: TestClient, mock_openai) -> None:
    upstream = mock_upstream(mock_openai)
    resp = enroll(shared_client, "sk-A", upstream)
    assert resp.status_code == 200
    body = shared_client.get(f"/sellers/{cu.WALLET.lower()}/status").json()
    assert body["has_key"] is True
    assert body["upstream_host"] == "127.0.0.1"
    assert body["upstream_official"] is False
    assert body["catalog"] == [{"model": "gpt-4o-mini", "servable": False}]


def test_enrollment_works_before_listing(shared_client: TestClient, mock_openai) -> None:
    """No Registry listing yet → enrollment SUCCEEDS; status honestly shows
    the unbound/inactive listing (never claims bound without a read)."""
    upstream = mock_upstream(mock_openai)
    resp = enroll(shared_client, "sk-A", upstream)
    assert resp.status_code == 200
    body = shared_client.get(f"/sellers/{cu.WALLET.lower()}/status").json()
    assert body["has_key"] is True
    assert body["listing_bound"] is False
    assert body["listing_active"] is False
    assert body["listing_models"] == []
    assert body["delegate_authorized"] is False


def test_binding_snapshot_listing_and_delegate(shared_client: TestClient, mock_openai) -> None:
    upstream = mock_upstream(mock_openai)
    assert enroll(shared_client, "sk-A", upstream).status_code == 200
    # Nobody registered → binding false despite the default fake knobs.
    cu.FakeSharedChain.registered_operator = cu.WALLET.lower()
    body = shared_client.get(f"/sellers/{cu.WALLET.lower()}/status").json()
    assert body["listing_bound"] is True
    assert body["listing_active"] is True
    assert body["listing_models"] == ["gpt-4o-mini"]
    assert body["delegate_authorized"] is False

    # The relay signer IS the approved settle delegate → true (via read).
    cu.FakeSharedChain.settle_delegate = cu.SIGNER_ADDR.lower()
    body2 = shared_client.get(f"/sellers/{cu.WALLET.lower()}/status").json()
    assert body2["delegate_authorized"] is True


def test_delegate_true_only_after_read(shared_client: TestClient, mock_openai) -> None:
    """A chain WITHOUT the v3.1 delegate function → authorized=false
    cleanly (no error, no fabricated true)."""
    assert enroll(shared_client, "sk-A", mock_upstream(mock_openai)).status_code == 200
    cu.FakeSharedChain.settle_delegate = None  # function unavailable face
    body = shared_client.get(f"/sellers/{cu.WALLET.lower()}/status").json()
    assert body["delegate_authorized"] is False


def test_binding_read_failure_degrades_false(shared_client: TestClient, mock_openai) -> None:
    """Chain read failure → binding false/empty (never an error, never
    true)."""
    assert enroll(shared_client, "sk-A", mock_upstream(mock_openai)).status_code == 200
    cu.FakeSharedChain.listing_fails = True
    body = shared_client.get(f"/sellers/{cu.WALLET.lower()}/status").json()
    assert body["listing_bound"] is False
    assert body["delegate_authorized"] is False


def test_chain_never_reachable_binding_all_false(
    monkeypatch: pytest.MonkeyPatch, tmp_path, mock_openai
) -> None:
    """Unreachable RPC (the REAL ChainClient is constructed lazily and
    fails) → binding snapshot all false; enrollment itself still works."""
    from relay.app.chain import ChainClient as RealChainClient

    cu.setup_shared_env(monkeypatch, str(tmp_path / "keystore.json"))
    # Restore the REAL chain client (the autouse fake_chain fixture patched
    # it away) — lazy construction must hit the dead RPC.
    monkeypatch.setattr(m, "ChainClient", RealChainClient)
    with TestClient(m.app) as client:
        upstream = f"http://127.0.0.1:{mock_openai.server_port}"
        rec = cu.fresh_record(client)
        raw = cu.submit_payload(rec, upstream, "sk-A")
        msg = cu.submit_message_for(rec, raw, upstream, cu.WALLET)
        resp = client.post("/sellers/keys", content=raw, headers=cu.wallet_headers(msg, seller=cu.WALLET))
        assert resp.status_code == 200, resp.text
        body = client.get(f"/sellers/{cu.WALLET.lower()}/status").json()
        assert body["listing_bound"] is False
        assert body["listing_active"] is False
        assert body["delegate_authorized"] is False
