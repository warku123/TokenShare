"""GET /verify-upstream + the startup key probe (seller pre-check feature).

All upstream behavior comes from the in-conftest mock OpenAI server's
GET /v1/models handler (models_status / models_ids) — zero real network,
zero real chain (FakeChain supplies the listing).
"""

from __future__ import annotations

import logging
from typing import Any

import pytest
from fastapi.testclient import TestClient

import relay.app.config as cfg
import relay.app.main as m
from relay.app.config import ConfigError

from .conftest import SELLER, setup_relay_env

# ------------------------------------------------------------------ constants


def test_probe_parses_models_and_auth_rejection(
    monkeypatch: pytest.MonkeyPatch, mock_openai: Any, fake_chain: Any
) -> None:
    """Probe helper: 200 → key_valid=True + sorted unique ids; 401 → False."""
    _boot_state(monkeypatch, mock_openai)
    mock_openai.models_ids = ["b-model", "a-model", "a-model", "", None]  # dedup+filter
    key_valid, models, err = _run_probe()
    assert key_valid is True
    assert models == ["a-model", "b-model"]
    assert err is None

    mock_openai.models_status = "401"
    key_valid, models, err = _run_probe()
    assert key_valid is False
    assert models == []
    assert err  # carries the upstream detail


def _boot_state(monkeypatch: pytest.MonkeyPatch, mock_openai: Any) -> None:
    import relay.app.config as config_mod

    setup_relay_env(monkeypatch, f"http://127.0.0.1:{mock_openai.server_port}/v1")
    st = m.RelayState(config_mod.load_config())
    monkeypatch.setattr(m, "state", st)


def _run_probe() -> tuple[bool | None, list[str], str | None]:
    import asyncio

    st = m._get_state()
    return asyncio.run(m._probe_upstream_key(st))


def _verify(client: TestClient) -> dict[str, Any]:
    resp = client.get("/verify-upstream")
    assert resp.status_code == 200  # semantics live in the body, always 200
    return resp.json()


# ------------------------------------------------------ /verify-upstream body


def test_verify_ok_all_listed_models_pass(
    client: TestClient, fake_chain: Any, mock_openai: Any
) -> None:
    fake_chain.models = ["gpt-4o-mini", "gpt-4.1-mini", ""]
    mock_openai.models_ids = ["gpt-4o-mini", "gpt-4.1-mini", "gpt-4o"]
    body = _verify(client)
    assert body["key_valid"] is True
    assert body["upstream_host"] == "127.0.0.1"
    assert body["accessible_models"] == ["gpt-4.1-mini", "gpt-4o", "gpt-4o-mini"]
    assert body["listed_models"] == ["gpt-4o-mini", "gpt-4.1-mini"]
    assert body["mismatches"] == []
    assert body["listing_ok"] is True
    assert "error" not in body


def test_verify_401_key_invalid(
    client: TestClient, fake_chain: Any, mock_openai: Any
) -> None:
    mock_openai.models_status = "401"
    body = _verify(client)
    assert body["key_valid"] is False
    assert body["accessible_models"] == []
    assert body["listing_ok"] is False
    assert body["error"]
    # Every listed model is a mismatch: the key serves nothing.
    assert [m["model"] for m in body["mismatches"]] == ["gpt-4o-mini"]


def test_verify_listed_model_missing_from_accessible(
    client: TestClient, fake_chain: Any, mock_openai: Any
) -> None:
    fake_chain.models = ["gpt-4o-mini", "gpt-fake-dream"]
    mock_openai.models_ids = ["gpt-4o-mini"]
    body = _verify(client)
    assert body["key_valid"] is True
    assert body["listing_ok"] is False
    assert body["mismatches"] == [
        {"model": "gpt-fake-dream", "reason": "not accessible with this key"}
    ]


def test_verify_network_error_transient(
    client: TestClient, fake_chain: Any, mock_openai: Any
) -> None:
    """A dropped connection is reported with key_valid=False + error, HTTP 200."""
    mock_openai.models_status = "net_err"
    body = _verify(client)
    assert body["key_valid"] is False
    assert body["accessible_models"] == []
    assert body["error"]
    assert body["listing_ok"] is False


def test_verify_5xx_reported_as_error(
    client: TestClient, fake_chain: Any, mock_openai: Any
) -> None:
    mock_openai.models_status = "500"
    body = _verify(client)
    assert body["key_valid"] is False
    assert "HTTP 500" in body["error"]


def test_verify_provider_mismatch_listed(
    client: TestClient, fake_chain: Any, mock_openai: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A moonshot-prefixed model listed against an openai-prefixed accessible
    set on a mock host: the mock has no provider identity (custom upstream),
    so only the accessible half applies — provider is NOT compared."""
    fake_chain.models = ["kimi-k2.6"]
    mock_openai.models_ids = ["kimi-k2.6"]
    body = _verify(client)
    assert body["listing_ok"] is True

    # Same shape on a REAL official host: provider must match the host.
    import relay.app.config as config_mod

    setup_relay_env(monkeypatch, "https://api.openai.com")
    st = m.RelayState(config_mod.load_config())
    monkeypatch.setattr(m, "state", st)
    from .conftest import FakeChain as FC

    FC.models = ["kimi-k2.6"]
    # Point the probe at the mock server anyway (monkeypatch the http client's
    # target via base_url) — the mock serves the 200, the host identity is what
    # the check reads from config.
    st.http = __import__("httpx").AsyncClient(
        base_url=f"http://127.0.0.1:{mock_openai.server_port}",
        headers={"Authorization": "Bearer x"},
    )
    try:
        body = _verify(client)
        assert body["upstream_host"] == "api.openai.com"
        assert body["listing_ok"] is False
        assert body["mismatches"] == [
            {"model": "kimi-k2.6", "reason": "provider does not match the upstream host"}
        ]
    finally:
        import asyncio

        asyncio.run(st.http.aclose())
        FC.models = ["gpt-4o-mini", ""]


def test_verify_unregistered_listing(
    client: TestClient, fake_chain: Any, mock_openai: Any
) -> None:
    fake_chain.listing_registered = False
    body = _verify(client)
    assert body["listing_ok"] is False
    assert body["listed_models"] == []
    assert "unregistered" in body["error"]


def test_verify_inactive_listing(
    client: TestClient, fake_chain: Any, mock_openai: Any
) -> None:
    fake_chain.listing_active = False
    body = _verify(client)
    assert body["listing_ok"] is False
    assert "inactive" in body["error"]


# ----------------------------------------------------------- startup probe


def test_startup_official_host_bad_key_fails_fast(
    monkeypatch: pytest.MonkeyPatch, mock_openai: Any, fake_chain: Any
) -> None:
    """Official host + VERIFY_UPSTREAM_ON_START=1 + 401 probe → ConfigError.
    The mock's GET handler is reachable, so the probe gets a definite 401."""
    setup_relay_env(monkeypatch, "https://api.moonshot.cn")
    monkeypatch.delenv(cfg.ENV_ALLOW_CUSTOM_UPSTREAM, raising=False)
    monkeypatch.delenv(cfg.ENV_VERIFY_UPSTREAM_ON_START, raising=False)
    # Route the probe's httpx traffic to the mock server: RelayState builds
    # its AsyncClient with base_url from config, so patch the instance after
    # construction via a wrapper — simplest: patch httpx.AsyncClient to force
    # the base_url to the mock.
    import httpx

    real_init = httpx.AsyncClient.__init__

    def patched_init(self: Any, *a: Any, **kw: Any) -> None:
        if kw.get("base_url") == "https://api.moonshot.cn":
            kw["base_url"] = f"http://127.0.0.1:{mock_openai.server_port}"
        real_init(self, *a, **kw)

    monkeypatch.setattr(httpx.AsyncClient, "__init__", patched_init)
    mock_openai.models_status = "401"
    with pytest.raises(ConfigError, match="key rejected"):
        with TestClient(m.app):
            pass


def test_startup_mock_host_skips_probe(
    monkeypatch: pytest.MonkeyPatch, mock_openai: Any, fake_chain: Any
) -> None:
    """Custom/mock upstream: the startup probe never runs (even on 401)."""
    setup_relay_env(monkeypatch, f"http://127.0.0.1:{mock_openai.server_port}/v1")
    monkeypatch.delenv(cfg.ENV_VERIFY_UPSTREAM_ON_START, raising=False)
    mock_openai.models_status = "401"
    with TestClient(m.app) as c:
        assert c.get("/health").status_code == 200


def test_startup_probe_disabled_by_env(
    monkeypatch: pytest.MonkeyPatch, mock_openai: Any, fake_chain: Any
) -> None:
    """VERIFY_UPSTREAM_ON_START=0 skips the probe even on an official host."""
    setup_relay_env(monkeypatch, "https://api.moonshot.cn")
    monkeypatch.delenv(cfg.ENV_ALLOW_CUSTOM_UPSTREAM, raising=False)
    monkeypatch.setenv(cfg.ENV_VERIFY_UPSTREAM_ON_START, "0")
    with TestClient(m.app) as c:
        assert c.get("/health").status_code == 200


def test_startup_network_error_warns_and_continues(
    monkeypatch: pytest.MonkeyPatch,
    mock_openai: Any,
    fake_chain: Any,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """Probe transport failure (NOT 401) → WARNING, relay still boots."""
    setup_relay_env(monkeypatch, "https://api.moonshot.cn")
    monkeypatch.delenv(cfg.ENV_ALLOW_CUSTOM_UPSTREAM, raising=False)
    monkeypatch.delenv(cfg.ENV_VERIFY_UPSTREAM_ON_START, raising=False)
    import httpx

    real_init = httpx.AsyncClient.__init__

    def patched_init(self: Any, *a: Any, **kw: Any) -> None:
        if kw.get("base_url") == "https://api.moonshot.cn":
            kw["base_url"] = "http://127.0.0.1:9"  # nothing listens there
        real_init(self, *a, **kw)

    monkeypatch.setattr(httpx.AsyncClient, "__init__", patched_init)
    with caplog.at_level(logging.WARNING, logger="tokenshare.relay"):
        with TestClient(m.app) as c:
            assert c.get("/health").status_code == 200
    assert any("startup upstream probe failed" in rec.message for rec in caplog.records)