"""TEE (Phala Cloud / dstack) tests — M7-A.

Everything is mocked: a fake socket file stands in for /var/run/dstack.sock
and a fake dstack SDK client is injected via monkeypatch. Zero real TEE,
zero dstack-sdk install, zero network.
"""

from __future__ import annotations

import sys
import types
from typing import Any

import pytest
from eth_account import Account
from fastapi.testclient import TestClient

from .conftest import SELLER, SELLER_KEY, mock_openai_url, setup_relay_env

# Deterministic valueless key standing in for the TEE-derived seller key.
TEE_KEY = "0x" + "44" * 32
TEE_ADDR = Account.from_key(TEE_KEY).address

TEE_KEY_PATH = "wallet/ethereum/tokenshare"


class FakeDstackClient:
    """Drop-in for dstack_sdk.DstackClient covering get_key/get_quote/info."""

    last_report_data: bytes | None = None
    fail_quote: bool = False
    instances: list["FakeDstackClient"] = []

    def __init__(self, endpoint: str | None = None) -> None:
        self.endpoint = endpoint
        type(self).instances.append(self)

    @classmethod
    def reset(cls) -> None:
        cls.last_report_data = None
        cls.fail_quote = False
        cls.instances = []

    def get_key(self, path: str) -> Any:
        assert path == TEE_KEY_PATH, f"unexpected dstack key path {path!r}"
        return types.SimpleNamespace(decode_key=lambda: TEE_KEY)

    def get_quote(self, report_data: bytes) -> Any:
        type(self).last_report_data = bytes(report_data)
        if type(self).fail_quote:
            raise RuntimeError("tdx agent down")
        return types.SimpleNamespace(quote="0x" + "ab" * 300, event_log="")

    def info(self) -> Any:
        return types.SimpleNamespace(app_id="0x" + "cd" * 8, vm_id="vm-1")


def _patch_socket(monkeypatch: pytest.MonkeyPatch, path: str) -> None:
    import relay.app.config as config

    monkeypatch.setattr(config, "DEFAULT_DSTACK_SOCKET", path)


def _patch_fake_client(monkeypatch: pytest.MonkeyPatch, factory: Any) -> None:
    """Patch the dstack client factory at both call sites (config + main)."""
    import relay.app.config as config
    import relay.app.main as main

    monkeypatch.setattr(config, "dstack_client", factory)
    monkeypatch.setattr(main, "dstack_client", factory)


@pytest.fixture
def fake_socket(monkeypatch: pytest.MonkeyPatch, tmp_path) -> str:
    """A fake dstack socket path (existence is all the config checks)."""
    socket = tmp_path / "dstack.sock"
    socket.write_bytes(b"")
    _patch_socket(monkeypatch, str(socket))
    return str(socket)


@pytest.fixture
def fake_dstack(monkeypatch: pytest.MonkeyPatch) -> type[FakeDstackClient]:
    FakeDstackClient.reset()
    _patch_fake_client(monkeypatch, FakeDstackClient)
    return FakeDstackClient


def _tee_env(monkeypatch: pytest.MonkeyPatch, mock_openai) -> None:
    """Full relay env for a TEE boot (no RELAY_SELLER_KEY — it is derived)."""
    setup_relay_env(monkeypatch, mock_openai_url(mock_openai))
    monkeypatch.delenv("RELAY_SELLER_KEY", raising=False)


# --------------------------------------------------------------------------
# config.py dstack branch
# --------------------------------------------------------------------------
def test_tee_mode_derives_seller_key_from_dstack(
    monkeypatch: pytest.MonkeyPatch, mock_openai, fake_socket, fake_dstack
) -> None:
    _tee_env(monkeypatch, mock_openai)

    import relay.app.config as config

    cfg = config.load_config()

    assert cfg.tee_mode is True
    assert cfg.tee_socket_path == fake_socket
    assert cfg.seller_key == TEE_KEY
    assert cfg.seller_address == TEE_ADDR
    # The client was constructed with the (fake) socket path.
    assert fake_dstack.instances and fake_dstack.instances[0].endpoint == fake_socket


def test_tee_mode_without_sdk_fails_fast(
    monkeypatch: pytest.MonkeyPatch, mock_openai, fake_socket
) -> None:
    """Socket present + dstack-sdk missing → ConfigError (real lazy import path)."""
    _tee_env(monkeypatch, mock_openai)
    # None in sys.modules makes `from dstack_sdk import ...` raise ImportError.
    monkeypatch.setitem(sys.modules, "dstack_sdk", None)

    import relay.app.config as config

    with pytest.raises(config.ConfigError) as exc:
        config.load_config()
    assert "dstack-sdk" in str(exc.value)


def test_tee_mode_with_bad_key_material_fails_fast(
    monkeypatch: pytest.MonkeyPatch, mock_openai, fake_socket, fake_dstack
) -> None:
    _tee_env(monkeypatch, mock_openai)

    class BadClient(FakeDstackClient):
        def get_key(self, path: str) -> Any:
            return types.SimpleNamespace(decode_key=lambda: "not-a-key")

    _patch_fake_client(monkeypatch, BadClient)

    import relay.app.config as config

    with pytest.raises(config.ConfigError) as exc:
        config.load_config()
    assert "unexpected length" in str(exc.value)


def test_no_socket_falls_back_to_env_key(
    monkeypatch: pytest.MonkeyPatch, mock_openai, tmp_path
) -> None:
    """No socket (dev/CI) → env key required, tee_mode off — unchanged behavior."""
    import relay.app.config as config

    missing = tmp_path / "absent.sock"
    _patch_socket(monkeypatch, str(missing))
    setup_relay_env(monkeypatch, mock_openai_url(mock_openai))

    cfg = config.load_config()

    assert cfg.tee_mode is False
    assert cfg.tee_socket_path is None
    assert cfg.seller_key == SELLER_KEY


def test_tee_mode_warns_when_seller_key_env_is_ignored(
    monkeypatch: pytest.MonkeyPatch, mock_openai, fake_socket, fake_dstack, caplog
) -> None:
    import logging

    import relay.app.config as config

    setup_relay_env(monkeypatch, mock_openai_url(mock_openai))  # sets RELAY_SELLER_KEY

    with caplog.at_level(logging.WARNING, logger="tokenshare.relay"):
        cfg = config.load_config()

    assert cfg.tee_mode is True
    assert any(
        "IGNORED" in rec.getMessage() and "dstack" in rec.getMessage()
        for rec in caplog.records
    )


# --------------------------------------------------------------------------
# /attestation + /info routes
# --------------------------------------------------------------------------
def _expected_report_data() -> bytes:
    return b"\x00" * 44 + bytes.fromhex(TEE_ADDR[2:])


def _keccak_hex(data: bytes) -> str:
    from eth_utils import keccak

    return "0x" + keccak(data).hex()


def test_attestation_route_tee(
    monkeypatch: pytest.MonkeyPatch, mock_openai, fake_socket, fake_dstack
) -> None:
    _tee_env(monkeypatch, mock_openai)
    import relay.app.main as main

    with TestClient(main.app) as client:
        resp = client.get("/attestation")

    assert resp.status_code == 200
    body = resp.json()
    assert body["tee"] is True
    assert body["derivedAddress"] == TEE_ADDR
    assert body["reportData"] == "0x" + _expected_report_data().hex()
    assert body["quote"] == "0x" + "ab" * 300
    assert body["appId"] == "0x" + "cd" * 8
    assert body["keyPath"] == TEE_KEY_PATH
    assert body["quoteDigest"] == _keccak_hex(bytes.fromhex("ab" * 300))
    # The quote's report data bound the TEE-derived seller address.
    assert fake_dstack.last_report_data == _expected_report_data()


@pytest.fixture
def host_client(monkeypatch: pytest.MonkeyPatch, mock_openai, tmp_path) -> TestClient:
    """TestClient with NO dstack socket (hermetic: default path points nowhere)."""
    missing = tmp_path / "no-dstack.sock"
    _patch_socket(monkeypatch, str(missing))
    setup_relay_env(monkeypatch, mock_openai_url(mock_openai))
    import relay.app.main as main

    with TestClient(main.app) as c:
        yield c


def test_attestation_route_non_tee_404(host_client: TestClient) -> None:
    resp = host_client.get("/attestation")
    assert resp.status_code == 404
    assert "TEE" in resp.json()["detail"]


def test_attestation_route_dstack_failure_is_500(
    monkeypatch: pytest.MonkeyPatch, mock_openai, fake_socket, fake_dstack
) -> None:
    _tee_env(monkeypatch, mock_openai)
    fake_dstack.fail_quote = True
    import relay.app.main as main

    with TestClient(main.app) as client:
        resp = client.get("/attestation")

    assert resp.status_code == 500
    assert "dstack" in resp.json()["detail"]


def test_info_route_non_tee(host_client: TestClient) -> None:
    resp = host_client.get("/info")

    assert resp.status_code == 200
    body = resp.json()
    assert body["tee"]["enabled"] is False
    assert body["tee"]["keyPath"] is None
    assert body["tee"]["socketPath"] is None
    assert body["seller"] == SELLER
    assert body["upstream"]["official"] is False  # mock upstream in tests
    assert "ALLOW_CUSTOM_UPSTREAM" in body["upstream"]["policy"]
    assert "api.kimi.com" in body["officialUpstreamHosts"]
    assert "kimi-" in body["modelProviderPrefixes"]["moonshot"]


def test_info_route_tee(
    monkeypatch: pytest.MonkeyPatch, mock_openai, fake_socket, fake_dstack
) -> None:
    _tee_env(monkeypatch, mock_openai)
    import relay.app.main as main

    with TestClient(main.app) as client:
        resp = client.get("/info")

    assert resp.status_code == 200
    body = resp.json()
    assert body["tee"]["enabled"] is True
    assert body["tee"]["socketPath"] == fake_socket
    assert body["tee"]["keyPath"] == TEE_KEY_PATH
    assert body["seller"] == TEE_ADDR


def test_info_route_official_upstream_policy(
    monkeypatch: pytest.MonkeyPatch, host_client: TestClient
) -> None:
    """official=true policy summary when the host resolves to a provider."""
    import relay.app.main as main

    monkeypatch.setattr(main, "host_provider", lambda _url: "moonshot")

    resp = host_client.get("/info")
    body = resp.json()
    assert body["upstream"]["official"] is True
    assert body["upstream"]["policy"].startswith("official-endpoint-only")
