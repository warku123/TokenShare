"""POST /preview-models (M9 relay PIN, verbatim semantics): one-shot upstream
catalog probe for the seller register flow.

    body {"upstream_base_url", "api_key"} → GET {normalized base}/v1/models
    with Authorization Bearer → 200 {upstream_host, official, models[]}.

Errors: 400 missing body fields / 400 non-official host (unless
ALLOW_CUSTOM_UPSTREAM=1) / 401 upstream 401|403 / 502 network or timeout.

KEY DISCIPLINE ( asserted here): api_key is used for exactly ONE probe and
never appears in any response body/header or in any captured log record.
"""

from __future__ import annotations

import logging
from typing import Any

import pytest
from fastapi.testclient import TestClient

from .conftest import mock_openai_url

# A fake seller key that must NEVER leak (deliberately distinctive).
SECRET_KEY = "sk-preview-super-secret-123"


def _post(client: TestClient, base_url: str, api_key: str = SECRET_KEY) -> Any:
    return client.post(
        "/preview-models",
        json={"upstream_base_url": base_url, "api_key": api_key},
    )


def test_preview_models_happy(client: TestClient, mock_openai: Any) -> None:
    """200 → {upstream_host, official, models[]} with ids deduped/filtered;
    a trailing /v1 in the input base is stripped (same rule as forwarding)."""
    mock_openai.models_ids = ["b-model", "a-model", "a-model", "", None]
    r = _post(client, mock_openai_url(mock_openai))  # SDK-style trailing /v1
    assert r.status_code == 200
    assert r.json() == {
        "upstream_host": "127.0.0.1",
        "official": False,  # mock host is not on the official allowlist
        "models": ["a-model", "b-model"],
    }

    # Base WITHOUT the trailing /v1 reaches the same normalized /v1/models
    # path (any other path would 404 on the mock → non-200).
    mock_openai.models_ids = ["gpt-4o-mini"]
    r2 = _post(client, f"http://127.0.0.1:{mock_openai.server_port}")
    assert r2.status_code == 200
    assert r2.json()["models"] == ["gpt-4o-mini"]


def test_preview_models_official_host_flag(
    client: TestClient, mock_openai: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    """An allowlisted host reports official=true (the mock host is mapped as
    an official provider so no real network is touched)."""
    import relay.app.config as cfg

    monkeypatch.setitem(cfg.OFFICIAL_HOST_PROVIDER, "127.0.0.1", "openai")
    r = _post(client, mock_openai_url(mock_openai))
    assert r.status_code == 200
    body = r.json()
    assert body["official"] is True
    assert body["upstream_host"] == "127.0.0.1"
    assert body["models"] == ["gpt-4o-mini"]


def test_preview_models_unofficial_host_400(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Non-allowlist host → 400 unless ALLOW_CUSTOM_UPSTREAM=1 (the client
    fixture enables the flag, so it is removed here)."""
    monkeypatch.delenv("ALLOW_CUSTOM_UPSTREAM", raising=False)
    r = _post(client, "https://evil.example.com")
    assert r.status_code == 400
    assert "official" in r.json()["detail"]


def test_preview_models_upstream_auth_rejected_401(
    client: TestClient, mock_openai: Any
) -> None:
    """Upstream 401|403 → 401 with a STATIC detail (upstream error bodies are
    never forwarded — they could echo the Authorization header)."""
    mock_openai.models_status = "401"
    r = _post(client, mock_openai_url(mock_openai))
    assert r.status_code == 401
    assert r.json()["detail"] == "upstream rejected the key (HTTP 401)"

    mock_openai.models_status = "403"
    r2 = _post(client, mock_openai_url(mock_openai))
    assert r2.status_code == 401
    assert r2.json()["detail"] == "upstream rejected the key (HTTP 403)"


def test_preview_models_network_error_502(
    client: TestClient, mock_openai: Any
) -> None:
    """Dropped connection (mock net_err) and nothing-listening both → 502."""
    mock_openai.models_status = "net_err"
    r = _post(client, mock_openai_url(mock_openai))
    assert r.status_code == 502

    r2 = _post(client, "http://127.0.0.1:9")  # nothing listens there
    assert r2.status_code == 502


def test_preview_models_missing_fields_400(client: TestClient) -> None:
    """Body validation precedes any network I/O: missing/blank fields and
    non-JSON bodies → 400."""
    r = client.post(
        "/preview-models",
        json={"upstream_base_url": "https://api.kimi.com/coding"},
    )
    assert r.status_code == 400  # missing api_key

    r2 = client.post("/preview-models", json={"api_key": "sk-x"})
    assert r2.status_code == 400  # missing upstream_base_url

    r3 = client.post(
        "/preview-models",
        json={"upstream_base_url": "", "api_key": "sk-x"},
    )
    assert r3.status_code == 400  # blank base

    r4 = client.post(
        "/preview-models",
        content=b"not json",
        headers={"Content-Type": "application/json"},
    )
    assert r4.status_code == 400  # invalid JSON


def test_preview_models_key_never_in_responses_or_logs(
    client: TestClient,
    mock_openai: Any,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """KEY DISCIPLINE: the probe key appears in NO response (success, 401,
    502 paths) and in NO log record at ANY level from ANY logger."""
    with caplog.at_level(logging.DEBUG):  # capture every logger
        ok = _post(client, mock_openai_url(mock_openai))
        assert ok.status_code == 200
        assert SECRET_KEY not in ok.text
        assert SECRET_KEY not in str(ok.headers)

        mock_openai.models_status = "401"
        rejected = _post(client, mock_openai_url(mock_openai))
        assert rejected.status_code == 401
        assert SECRET_KEY not in rejected.text

        mock_openai.models_status = "net_err"
        broken = _post(client, mock_openai_url(mock_openai))
        assert broken.status_code == 502
        assert SECRET_KEY not in broken.text

    assert SECRET_KEY not in caplog.text
