"""M15 R1 custody upstream-gate tests — official HTTPS allowlist, custom
dev escape, SSRF/redirect hardening, catalog parsing (raw upstream ids,
servable from the EXISTING provider helpers, 128-char ASCII cap)."""

from __future__ import annotations

import httpx
import pytest
from fastapi.testclient import TestClient

from . import custody_utils as cu
from typing import Any

from relay.app import custody
from relay.app.config import OFFICIAL_UPSTREAM_HOSTS, provider_for_model


@pytest.fixture
def shared_client(monkeypatch: pytest.MonkeyPatch, tmp_path, mock_openai) -> TestClient:
    import relay.app.main as m

    cu.setup_shared_env(monkeypatch, str(tmp_path / "keystore.json"))
    with TestClient(m.app) as c:
        yield c


def mock_upstream(mock_openai) -> str:
    return f"http://127.0.0.1:{mock_openai.server_port}"


def enroll(client: TestClient, api_key: str, upstream: str) -> Any:
    rec = cu.fresh_record(client)
    raw = cu.submit_payload(rec, upstream, api_key)
    msg = cu.submit_message_for(rec, raw, upstream, cu.WALLET)
    return client.post("/sellers/keys", content=raw, headers=cu.wallet_headers(msg, seller=cu.WALLET))


# --------------------------------------------------------- normalize + gate
def test_official_host_accepted(shared_client: TestClient, monkeypatch) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.scheme == "https"
        assert str(request.url) == "https://api.kimi.com/coding/v1/models"
        return httpx.Response(200, json={"data": [{"id": "kimi-k2-instruct"}]})

    monkeypatch.setattr(custody, "_probe_transport", httpx.MockTransport(handler))
    resp = enroll(shared_client, "sk-official-key", "https://api.kimi.com/coding")
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["official"] is True
    assert body["upstream_host"] == "api.kimi.com"
    assert body["catalog"] == [{"model": "kimi-k2-instruct", "servable": True}]


def test_sdk_style_v1_url_normalized(shared_client: TestClient, monkeypatch) -> None:
    """The SDK-style base (.../coding/v1) normalizes to the same base and
    probes the canonical /v1/models."""
    seen: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(str(request.url))
        return httpx.Response(200, json={"data": []})

    monkeypatch.setattr(custody, "_probe_transport", httpx.MockTransport(handler))
    resp = enroll(shared_client, "sk-official-key", "https://api.kimi.com/coding/v1/")
    assert resp.status_code == 200
    assert seen == ["https://api.kimi.com/coding/v1/models"]


def test_official_host_over_http_rejected(
    shared_client: TestClient, monkeypatch
) -> None:
    """Official hosts REQUIRE https — http://api.kimi.com is not official
    (and without the custom flag it cannot pass as custom either)."""
    monkeypatch.setenv("ALLOW_CUSTOM_UPSTREAM", "0")
    resp = enroll(shared_client, "sk-x", "http://api.kimi.com/coding")
    assert resp.status_code == 400
    assert resp.json()["detail"] == "upstream_not_official"


def test_unknown_host_without_custom_flag_400(
    shared_client, monkeypatch
) -> None:
    monkeypatch.setenv("ALLOW_CUSTOM_UPSTREAM", "0")
    resp = enroll(shared_client, "sk-x", "http://127.0.0.1:9/v1")
    assert resp.status_code == 400
    assert resp.json()["detail"] == "upstream_not_official"


def test_custom_upstream_allowed_with_flag(shared_client: TestClient) -> None:
    resp = enroll(shared_client, "sk-x", "http://127.0.0.1:9/v1")
    assert resp.status_code in (400, 502, 200)  # network failure face, not gate
    if resp.status_code in (400, 502):
        assert resp.json()["detail"] != "upstream_not_official"


def test_normalize_rejects_userinfo_query_fragment_pipe(shared_client) -> None:
    """Structurally invalid upstreams are rejected server-side (the server
    normalizes BEFORE any auth/upstream work); the test signs the RAW value
    so the signature would be valid if normalization were skipped."""
    for bad in (
        "https://u@api.kimi.com/coding",
        "https://api.kimi.com/coding?x=1",
        "https://api.kimi.com/coding#f",
        "https://api.kimi.com/coding|",
        "ftp://api.kimi.com/coding",
        "   ",
    ):
        rec = cu.fresh_record(shared_client)
        raw = cu.submit_payload(rec, bad, "sk-x", aad_override="unused")
        msg = custody.build_submit_message(
            seller=cu.WALLET.lower(),
            chain_id=84532,
            escrow_addr=cu.ESCROW_ADDR,
            registry_addr=cu.REGISTRY_ADDR,
            origin=cu.DEFAULT_ORIGIN,
            upstream_base_url=bad,  # raw — client-signed candidate
            nonce=rec["nonce"],
            body_sha256=custody.sha256_hex(raw),
            issued=rec["issued_at"],
            expires=rec["expires_at"],
        )
        resp = shared_client.post(
            "/sellers/keys", content=raw, headers=cu.wallet_headers(msg, seller=cu.WALLET)
        )
        assert resp.status_code == 400, (bad, resp.status_code, resp.text)
        assert resp.json()["detail"] in ("bad_request", "upstream_not_official")


# ---------------------------------------------------------------- SSRF faces
def test_probe_never_follows_redirects(monkeypatch) -> None:
    calls: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(str(request.url))
        return httpx.Response(301, headers={"location": "http://attacker.example/v1/models"})

    monkeypatch.setattr(custody, "_probe_transport", httpx.MockTransport(handler))
    result = __import__("asyncio").run(custody.probe_upstream_key("https://api.kimi.com/coding", "sk-x"))
    assert not result.ok
    assert len(calls) == 1  # nothing followed


def test_probe_401_maps_key_rejected(monkeypatch) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(401, json={"error": "bad key"})

    monkeypatch.setattr(custody, "_probe_transport", httpx.MockTransport(handler))
    result = __import__("asyncio").run(custody.probe_upstream_key("https://api.kimi.com/coding", "sk-x"))
    assert not result.ok and result.status == 401


def test_probe_network_error_maps_unreachable(monkeypatch) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("refused")

    monkeypatch.setattr(custody, "_probe_transport", httpx.MockTransport(handler))
    result = __import__("asyncio").run(custody.probe_upstream_key("https://api.kimi.com/coding", "sk-x"))
    assert not result.ok and result.status == 0


def test_probe_non_json_200_maps_unreachable(monkeypatch) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, text="not json")

    monkeypatch.setattr(custody, "_probe_transport", httpx.MockTransport(handler))
    result = __import__("asyncio").run(custody.probe_upstream_key("https://api.kimi.com/coding", "sk-x"))
    assert not result.ok


# ----------------------------------------------------------------- catalog
def test_catalog_raw_upstream_ids_and_servable(monkeypatch) -> None:
    """servable comes from provider_for_model(model) == host_provider — the
    EXISTING config helpers; no model list is hardcoded in custody code."""
    payload = {
        "data": [
            {"id": "kimi-k2-turbo-preview"},      # moonshot prefix, kimi host → True
            {"id": "k3-256k"},                    # "k3" prefix → True
            {"id": "gpt-4o-mini"},                # openai prefix ≠ moonshot → False
            {"id": "x" * 129},                    # >128 chars → dropped
            {"id": "unicode-模型"},                # non-ASCII → dropped
            {"id": ""},                           # empty → dropped
            {"id": "kimi-k2-turbo-preview"},      # dup → deduped
            {"nope": 1},                          # malformed → skipped
        ]
    }
    catalog = custody.catalog_from_payload(payload, "https://api.kimi.com/coding")
    assert catalog == [
        {"model": "kimi-k2-turbo-preview", "servable": True},
        {"model": "k3-256k", "servable": True},
        {"model": "gpt-4o-mini", "servable": False},
    ]
    # Cross-check the servable flag against the config helpers directly.
    assert provider_for_model("kimi-k2-turbo-preview") == "moonshot"


def test_catalog_qwen_token_plan_servable(monkeypatch) -> None:
    """Qwen TokenPlan face: servable derives from provider_for_model ==
    host_provider with NO new code — a qwen* model is servable only on the
    token-plan.maas.qianwenaiapi.com upstream, foreign faces are not."""
    payload = {"data": [{"id": "qwen3.8-max"}, {"id": "qwen-plus"}, {"id": "kimi-k2-instruct"}]}
    catalog = custody.catalog_from_payload(
        payload,
        "https://token-plan.maas.qianwenaiapi.com/compatible-mode",
    )
    assert catalog == [
        {"model": "qwen3.8-max", "servable": True},
        {"model": "qwen-plus", "servable": True},
        {"model": "kimi-k2-instruct", "servable": False},
    ]


def test_catalog_custom_host_all_not_servable(monkeypatch) -> None:
    payload = {"data": [{"id": "gpt-4o-mini"}, {"id": "kimi-k2-instruct"}]}
    catalog = custody.catalog_from_payload(payload, "http://127.0.0.1:9299")
    assert all(e["servable"] is False for e in catalog)


def test_official_allowlist_matches_config() -> None:
    assert "api.kimi.com" in OFFICIAL_UPSTREAM_HOSTS
    assert custody.upstream_is_official("https://opencode.ai/zen")
    assert custody.upstream_is_official(
        "https://token-plan.maas.qianwenaiapi.com/compatible-mode"
    )
    assert not custody.upstream_is_official("https://evil.example/zen")
    assert not custody.upstream_is_official(
        "http://token-plan.maas.qianwenaiapi.com/compatible-mode"
    )
