"""M15 bootstrap gate tests (RELAY_BOOTSTRAP_MODE, shared-only, phase 1).

Everything here is synthetic and local: tmp_path-only files, the custody
utils' throwaway dev keys, a non-existent dstack socket, and NO network.
The gate is deny-by-default with an explicit GET/HEAD whitelist on
/health, /info, /attestation (canonical + single trailing slash); every
other method/path answers an exact 503 {"detail":"bootstrap_mode"}.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import os
from typing import Any

import pytest
from starlette.testclient import TestClient

from . import custody_utils as cu

BOOTSTRAP_BODY = b'{"detail":"bootstrap_mode"}'
PLACEHOLDER_ORIGIN = "https://placeholder.invalid"


# --------------------------------------------------------------------------
# env helpers (local — custody_utils is intentionally NOT modified)
# --------------------------------------------------------------------------
def _shared_base_env(monkeypatch: pytest.MonkeyPatch, tmp_path, *, origin: str) -> str:
    """Shared-mode env with synthetic dev keys and a keystore path inside
    tmp_path that does NOT exist. Returns the keystore path string."""
    import relay.app.config as config

    keystore_path = str(tmp_path / "keystore.json")
    monkeypatch.setenv("RELAY_MODE", "shared")
    monkeypatch.setenv("RELAY_PUBLIC_ORIGIN", origin)
    monkeypatch.setenv("SHARED_KEYSTORE_PATH", keystore_path)
    monkeypatch.setenv("SHARED_SIGNER_KEY", cu.SHARED_SIGNER_KEY)
    monkeypatch.setenv("SHARED_KEK_HEX", cu.SHARED_KEK_HEX)
    monkeypatch.setenv("SHARED_UPLOAD_KEY", cu.SHARED_UPLOAD_KEY)
    monkeypatch.setenv("RPC_URL", "http://unused.invalid")
    monkeypatch.setenv("CHAIN_ID", str(cu.CHAIN_ID))
    monkeypatch.setenv("ESCROW_ADDR", cu.ESCROW_ADDR)
    monkeypatch.setenv("REGISTRY_ADDR", cu.REGISTRY_ADDR)
    monkeypatch.setenv("USDC_ADDR", "0x3333333333333333333333333333333333333333")
    monkeypatch.delenv("RELAY_SELLER_KEY", raising=False)
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)
    monkeypatch.delenv("ALLOW_INSECURE_ENDPOINT", raising=False)
    monkeypatch.delenv("RELAY_CORS_ORIGINS", raising=False)
    # Determinism: the test machine must look socket-less to the config.
    monkeypatch.setattr(config, "DEFAULT_DSTACK_SOCKET", "/nonexistent/dstack.sock")
    return keystore_path


def _bootstrap_env(
    monkeypatch: pytest.MonkeyPatch, tmp_path, *, origin: str = PLACEHOLDER_ORIGIN, mode: str = "1"
) -> str:
    monkeypatch.setenv("RELAY_BOOTSTRAP_MODE", mode)
    return _shared_base_env(monkeypatch, tmp_path, origin=origin)


def _config():
    import relay.app.config as config

    return config.load_config()


def _snapshot_tree(root) -> set[str]:
    return {str(p.relative_to(root)) for p in root.rglob("*")}


# --------------------------------------------------------------------------
# strict RELAY_BOOTSTRAP_MODE parsing (frozen-at-startup contract)
# --------------------------------------------------------------------------
@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        (None, False),
        ("", False),
        ("   ", False),
        ("0", False),
        (" false ", False),
        ("FALSE", False),
        ("1", True),
        ("true", True),
        ("  True ", True),
    ],
)
def test_bootstrap_bool_strict_boundaries(
    monkeypatch: pytest.MonkeyPatch, tmp_path, raw: str | None, expected: bool
) -> None:
    keystore = _shared_base_env(monkeypatch, tmp_path, origin="https://relay.example")
    assert not os.path.lexists(keystore)  # sanity: fresh path must not pre-exist
    if raw is None:
        monkeypatch.delenv("RELAY_BOOTSTRAP_MODE", raising=False)
    else:
        monkeypatch.setenv("RELAY_BOOTSTRAP_MODE", raw)
    assert _config().bootstrap_mode is expected


@pytest.mark.parametrize("raw", ["2", "yes", "on", "enabled", "01", "true123", "no"])
def test_bootstrap_bool_garbage_rejected(
    monkeypatch: pytest.MonkeyPatch, tmp_path, raw: str
) -> None:
    import relay.app.config as config

    _shared_base_env(monkeypatch, tmp_path, origin="https://relay.example")
    monkeypatch.setenv("RELAY_BOOTSTRAP_MODE", raw)
    with pytest.raises(config.ConfigError) as exc:
        _config()
    assert "RELAY_BOOTSTRAP_MODE" in str(exc.value)


def test_bootstrap_is_shared_only(monkeypatch: pytest.MonkeyPatch, tmp_path) -> None:
    import relay.app.config as config

    cu.setup_single_env(monkeypatch, "https://api.openai.com")
    monkeypatch.setenv("RELAY_BOOTSTRAP_MODE", "1")
    with pytest.raises(config.ConfigError) as exc:
        _config()
    assert "RELAY_BOOTSTRAP_MODE" in str(exc.value)


# --------------------------------------------------------------------------
# keystore-path existence gate (BEFORE any dstack derive; lexists semantics)
# --------------------------------------------------------------------------
def _install_derive_spy(monkeypatch: pytest.MonkeyPatch) -> list[Any]:
    import relay.app.config as config

    calls: list[Any] = []

    def _boom(socket_path):  # pragma: no cover - executed only on failure
        calls.append(socket_path)
        raise AssertionError("dstack derive must not run before the bootstrap gate")

    monkeypatch.setattr(config, "_derive_shared_keys", _boom)
    return calls


@pytest.mark.parametrize(
    "content",
    [b'{"entries": {}}', b"not json at all", b'{"v": 9, "entries": {"x": "wrong-kek"}}'],
)
def test_bootstrap_rejects_any_existing_keystore_before_derive(
    monkeypatch: pytest.MonkeyPatch, tmp_path, content: bytes
) -> None:
    import relay.app.config as config

    _bootstrap_env(monkeypatch, tmp_path)
    # A fake EXISTING socket proves ordering: the gate must fire before the
    # (spied, must-not-run) derive ever sees the socket.
    fake_socket = tmp_path / "dstack.sock"
    fake_socket.write_bytes(b"")
    monkeypatch.setattr(config, "DEFAULT_DSTACK_SOCKET", str(fake_socket))
    calls = _install_derive_spy(monkeypatch)

    (tmp_path / "keystore.json").write_bytes(content)
    before = (tmp_path / "keystore.json").read_bytes()
    with pytest.raises(config.ConfigError) as exc:
        _config()
    assert "already exists" in str(exc.value)
    assert calls == []
    assert (tmp_path / "keystore.json").read_bytes() == before  # untouched


def test_bootstrap_rejects_dangling_symlink_keystore(
    monkeypatch: pytest.MonkeyPatch, tmp_path
) -> None:
    import relay.app.config as config

    _bootstrap_env(monkeypatch, tmp_path)
    calls = _install_derive_spy(monkeypatch)
    (tmp_path / "keystore.json").symlink_to(tmp_path / "missing-target.json")
    with pytest.raises(config.ConfigError) as exc:
        _config()
    assert "already exists" in str(exc.value)
    assert calls == []
    assert (tmp_path / "keystore.json").is_symlink()  # untouched


def test_bootstrap_rejects_symlink_to_existing_file(
    monkeypatch: pytest.MonkeyPatch, tmp_path
) -> None:
    import relay.app.config as config

    _bootstrap_env(monkeypatch, tmp_path)
    target = tmp_path / "real-store.json"
    target.write_bytes(b'{"entries": {"seller": "wrapped"}}')
    (tmp_path / "keystore.json").symlink_to(target)
    with pytest.raises(config.ConfigError) as exc:
        _config()
    assert "already exists" in str(exc.value)
    assert target.read_bytes() == b'{"entries": {"seller": "wrapped"}}'


# --------------------------------------------------------------------------
# RelayState in bootstrap: identity without stores/persistence/network
# --------------------------------------------------------------------------
def test_bootstrap_state_has_no_store_nonce_and_creates_nothing(
    monkeypatch: pytest.MonkeyPatch, tmp_path
) -> None:
    import relay.app.main as m

    _bootstrap_env(monkeypatch, tmp_path)
    before = _snapshot_tree(tmp_path)
    state = m.RelayState(_config())
    try:
        assert state.keystore is None
        assert state.nonces is None
        assert state._chain is None and state._chain_failed is False
        assert not (tmp_path / "keystore.json").exists()  # nothing created
        assert _snapshot_tree(tmp_path) == before  # zero files/dirs created
    finally:
        asyncio.run(state.http.aclose())


def test_bootstrap_and_normal_share_identity_from_same_devkeys(
    monkeypatch: pytest.MonkeyPatch, tmp_path
) -> None:
    import relay.app.main as m

    _bootstrap_env(monkeypatch, tmp_path)
    st_boot = m.RelayState(_config())
    try:
        monkeypatch.setenv("RELAY_BOOTSTRAP_MODE", "0")
        monkeypatch.setenv("RELAY_PUBLIC_ORIGIN", "https://relay.example")
        monkeypatch.setenv("SHARED_KEYSTORE_PATH", str(tmp_path / "normal-keystore.json"))
        st_norm = m.RelayState(_config())
        try:
            assert st_boot.signer_address == st_norm.signer_address
            assert st_boot.upload_pub == st_norm.upload_pub
            assert (
                hashlib.sha256(st_boot.upload_pub).hexdigest()
                == hashlib.sha256(st_norm.upload_pub).hexdigest()
            )
            assert st_norm.keystore is not None and st_boot.keystore is None
        finally:
            asyncio.run(st_norm.http.aclose())
    finally:
        asyncio.run(st_boot.http.aclose())


# --------------------------------------------------------------------------
# pure-ASGI gate units (no HTTP server involved)
# --------------------------------------------------------------------------
def _run_gate(monkeypatch: pytest.MonkeyPatch, scope: dict[str, Any]):
    import relay.app.main as m

    monkeypatch.setattr(m, "_bootstrap_gate_active", True)
    sent: list[Any] = []
    passed: list[Any] = []

    async def app(scope, receive, send):  # pragma: no cover - spy
        passed.append(scope)

    async def receive():  # pragma: no cover - never read (no body reads)
        raise AssertionError("gate must not read the request body")

    async def send(message):
        sent.append(message)

    mw = m.BootstrapGateMiddleware(app)
    asyncio.run(mw(scope, receive, send))
    return sent, passed


def test_gate_denies_post_health_exact_frames(monkeypatch: pytest.MonkeyPatch) -> None:
    sent, passed = _run_gate(
        monkeypatch, {"type": "http", "method": "POST", "path": "/health"}
    )
    assert passed == []
    assert sent == [
        {
            "type": "http.response.start",
            "status": 503,
            "headers": [(b"content-type", b"application/json")],
        },
        {"type": "http.response.body", "body": BOOTSTRAP_BODY},
    ]


def test_gate_closes_websocket_scope(monkeypatch: pytest.MonkeyPatch) -> None:
    sent, passed = _run_gate(
        monkeypatch, {"type": "websocket", "path": "/v1/chat/completions"}
    )
    assert passed == []
    assert sent == [{"type": "websocket.close", "code": 1011}]


def test_gate_allows_whitelisted_get(monkeypatch: pytest.MonkeyPatch) -> None:
    sent, passed = _run_gate(monkeypatch, {"type": "http", "method": "GET", "path": "/health"})
    assert len(passed) == 1 and sent == []


# --------------------------------------------------------------------------
# HTTP surface (TestClient + lifespan; still 100% synthetic/local)
# --------------------------------------------------------------------------
def _bootstrap_client(monkeypatch: pytest.MonkeyPatch, tmp_path) -> TestClient:
    import relay.app.main as m

    _bootstrap_env(monkeypatch, tmp_path)
    return TestClient(m.app)


BUSINESS_DENY_PATHS = [
    ("GET", "/sellers/nonce/0x38c26E1782b7E6F656Aa35EbF473e2ff0D718Dd6"),
    ("GET", "/sellers/0x38c26E1782b7E6F656Aa35EbF473e2ff0D718Dd6/status"),
    ("POST", "/sellers/keys"),
    ("DELETE", "/sellers/keys"),
    ("GET", "/receipt/1"),
    ("GET", "/payment/1/usage"),
    ("POST", "/payment/1/revoke"),
    ("GET", "/verify-upstream"),
    ("POST", "/preview-models"),
    ("GET", "/v1/chat/completions"),
    ("POST", "/v1/chat/completions"),
    ("GET", "/docs"),
    ("GET", "/openapi.json"),
    ("GET", "/redoc"),
    ("GET", "/whatever-unknown"),
    ("POST", "/health"),
    ("POST", "/health/"),
    ("GET", "/"),
    ("GET", "/healthz"),
    ("GET", "/Health"),
    ("OPTIONS", "/info"),
    ("GET", "/sellers/keys/"),
]


@pytest.mark.parametrize(("method", "path"), BUSINESS_DENY_PATHS)
def test_business_and_unknown_paths_denied_exact(
    monkeypatch: pytest.MonkeyPatch, tmp_path, method: str, path: str
) -> None:
    before = _snapshot_tree(tmp_path)
    with _bootstrap_client(monkeypatch, tmp_path) as client:
        r = client.request(method, path)
    assert r.status_code == 503, (method, path, r.status_code)
    assert r.content == BOOTSTRAP_BODY
    assert r.headers["content-type"].startswith("application/json")
    assert "location" not in {k.lower() for k in r.headers}  # no 307 detour
    assert _snapshot_tree(tmp_path) == before  # zero writes while gated


def test_gate_denies_before_handlers_run(
    monkeypatch: pytest.MonkeyPatch, tmp_path
) -> None:
    import relay.app.main as m

    calls = {"n": 0}
    original = m._get_state

    def _spy():
        calls["n"] += 1
        return original()

    monkeypatch.setattr(m, "_get_state", _spy)
    with _bootstrap_client(monkeypatch, tmp_path) as client:
        for path in ("/sellers/keys", "/receipt/1", "/docs", "/v1/chat/completions"):
            r = client.get(path) if path != "/sellers/keys" else client.post(path)
            assert r.status_code == 503 and r.content == BOOTSTRAP_BODY
    assert calls["n"] == 0  # no business handler was ever reached


def test_whitelist_identity_surface(monkeypatch: pytest.MonkeyPatch, tmp_path) -> None:
    with _bootstrap_client(monkeypatch, tmp_path) as client:
        h = client.get("/health")
        assert h.status_code == 200
        hb = h.json()
        assert hb["bootstrap"] is True and hb["mode"] == "shared"
        assert "keystoreEntries" not in hb

        i = client.get("/info")
        assert i.status_code == 200
        ib = i.json()
        assert ib["mode"] == "shared"
        assert ib["shared"]["bootstrap"] is True
        assert "keystoreEntries" not in ib["shared"]

        # HEAD mirrors GET on the whitelist (Starlette auto-HEAD on GET routes;
        # the gate whitelists HEAD explicitly regardless of framework behavior).
        assert client.head("/health").status_code == 200
        assert client.head("/info").status_code == 200

        # Trailing slash (single) stays whitelisted; TestClient follows the
        # canonicalizing redirect, and the destination is still whitelisted.
        assert client.get("/health/").status_code == 200
        assert client.get("/info/").status_code == 200
        assert client.head("/health/").status_code == 200

        # /attestation stays reachable (404 here: synthetic dev host has no
        # dstack socket) and its identity body is unchanged — no bootstrap key.
        a = client.get("/attestation")
        assert a.status_code == 404
        assert "bootstrap" not in a.json()
        assert client.get("/attestation/").status_code == 404


def test_normal_shared_reports_bootstrap_false(
    monkeypatch: pytest.MonkeyPatch, tmp_path
) -> None:
    import relay.app.main as m

    _shared_base_env(monkeypatch, tmp_path, origin="https://relay.example")
    monkeypatch.setenv("RELAY_BOOTSTRAP_MODE", "0")
    with TestClient(m.app) as client:
        h = client.get("/health").json()
        assert h["bootstrap"] is False and "keystoreEntries" in h
        ib = client.get("/info").json()
        assert ib["shared"]["bootstrap"] is False and "keystoreEntries" in ib["shared"]


def test_single_mode_bodies_unchanged_by_gate(
    monkeypatch: pytest.MonkeyPatch, tmp_path
) -> None:
    from .conftest import setup_relay_env

    import relay.app.main as m

    class _FakeChain:
        seller_address = "0x9999999999999999999999999999999999999999"

        def __init__(self, **kwargs: Any) -> None:  # absorb ctor kwargs
            pass

    setup_relay_env(monkeypatch, "http://127.0.0.1:9/v1")
    monkeypatch.setattr(m, "ChainClient", _FakeChain)
    with TestClient(m.app) as client:
        h = client.get("/health").json()
        assert "bootstrap" not in h and "keystoreEntries" not in h
        i = client.get("/info").json()
        assert i["mode"] == "single" and "bootstrap" not in i


def test_flag_frozen_no_runtime_switch(monkeypatch: pytest.MonkeyPatch, tmp_path) -> None:
    with _bootstrap_client(monkeypatch, tmp_path) as client:
        # Flip the env after startup: the frozen gate must not care.
        monkeypatch.setenv("RELAY_BOOTSTRAP_MODE", "0")
        r = client.post("/sellers/keys")
        assert r.status_code == 503 and r.content == BOOTSTRAP_BODY
        # And there is no enable API: unknown paths stay denied too.
        for path in ("/bootstrap", "/bootstrap/enable", "/admin/enable"):
            r = client.post(path)
            assert r.status_code == 503 and r.content == BOOTSTRAP_BODY


def test_cors_preflight_outer_plain_options_inner(
    monkeypatch: pytest.MonkeyPatch, tmp_path
) -> None:
    with _bootstrap_client(monkeypatch, tmp_path) as client:
        # Compliant preflight: handled by the OUTER CORS layer, never the gate.
        pre = client.options(
            "/sellers/keys",
            headers={
                "Origin": "https://front.example",
                "Access-Control-Request-Method": "POST",
            },
        )
        assert pre.status_code == 200
        assert pre.content != BOOTSTRAP_BODY
        # Plain OPTIONS (no preflight headers): falls through CORS into the gate.
        plain = client.options("/info")
        assert plain.status_code == 503 and plain.content == BOOTSTRAP_BODY
        # Requests without an Origin are not CORS requests: business is denied.
        no_origin = client.get("/sellers/keys")
        assert no_origin.status_code == 503 and no_origin.content == BOOTSTRAP_BODY
        # Whitelisted GET passes both layers.
        assert client.get("/health").status_code == 200


def test_json_payload_is_exact_canonical_form() -> None:
    # The gate emits the exact compact bytes — no FastAPI default spacing.
    assert json.dumps({"detail": "bootstrap_mode"}, separators=(",", ":")).encode() == (
        BOOTSTRAP_BODY
    )
