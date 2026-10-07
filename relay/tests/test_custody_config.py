"""M15 R1 shared-mode config tests — RELAY_MODE/origin/keys, fail-closed
derivation, and the single-mode invariance face."""

from __future__ import annotations

import pytest
from eth_account import Account

from . import custody_utils as cu

SHARED_KEY_ENVS = (
    "SHARED_SIGNER_KEY",
    "SHARED_KEK_HEX",
    "SHARED_UPLOAD_KEY",
)


def _base_shared_env(monkeypatch: pytest.MonkeyPatch, tmp_path) -> None:
    cu.setup_shared_env(monkeypatch, str(tmp_path / "keystore.json"))


def _config():
    import relay.app.config as config

    return config.load_config()


# ---------------------------------------------------------------- mode switch
def test_mode_defaults_to_single(monkeypatch: pytest.MonkeyPatch, tmp_path) -> None:
    cu.setup_single_env(monkeypatch, "https://api.openai.com")
    assert _config().mode == "single"


def test_mode_invalid_rejected(monkeypatch: pytest.MonkeyPatch, tmp_path) -> None:
    _base_shared_env(monkeypatch, tmp_path)
    import relay.app.config as config

    monkeypatch.setenv("RELAY_MODE", "hybrid")
    with pytest.raises(config.ConfigError) as exc:
        _config()
    assert "RELAY_MODE" in str(exc.value)


def test_mode_case_insensitive(monkeypatch: pytest.MonkeyPatch, tmp_path) -> None:
    _base_shared_env(monkeypatch, tmp_path)
    monkeypatch.setenv("RELAY_MODE", "SHARED")
    assert _config().mode == "shared"


# -------------------------------------------------------------- origin rules
def test_origin_trailing_slash_rejected(monkeypatch: pytest.MonkeyPatch, tmp_path) -> None:
    _base_shared_env(monkeypatch, tmp_path)
    import relay.app.config as config

    monkeypatch.setenv("RELAY_PUBLIC_ORIGIN", "https://relay.example/")
    with pytest.raises(config.ConfigError):
        _config()


def test_origin_path_query_fragment_userinfo_rejected(
    monkeypatch: pytest.MonkeyPatch, tmp_path
) -> None:
    _base_shared_env(monkeypatch, tmp_path)
    import relay.app.config as config

    for bad in (
        "https://relay.example/path",
        "https://relay.example?q=1",
        "https://relay.example#frag",
        "https://user@relay.example",
        "https://u:p@relay.example",
    ):
        monkeypatch.setenv("RELAY_PUBLIC_ORIGIN", bad)
        with pytest.raises(config.ConfigError):
            _config()


def test_origin_http_requires_insecure_flag(monkeypatch: pytest.MonkeyPatch, tmp_path) -> None:
    _base_shared_env(monkeypatch, tmp_path)
    import relay.app.config as config

    monkeypatch.setenv("RELAY_PUBLIC_ORIGIN", "http://relay.example")
    with pytest.raises(config.ConfigError):
        _config()
    monkeypatch.setenv("ALLOW_INSECURE_ENDPOINT", "1")
    assert _config().public_origin == "http://relay.example"


def test_origin_default_port_normalized(monkeypatch: pytest.MonkeyPatch, tmp_path) -> None:
    _base_shared_env(monkeypatch, tmp_path)
    monkeypatch.setenv("RELAY_PUBLIC_ORIGIN", "https://relay.example:443")
    assert _config().public_origin == "https://relay.example"
    monkeypatch.setenv("RELAY_PUBLIC_ORIGIN", "https://relay.example:8443")
    assert _config().public_origin == "https://relay.example:8443"


def test_origin_missing_rejected(monkeypatch: pytest.MonkeyPatch, tmp_path) -> None:
    _base_shared_env(monkeypatch, tmp_path)
    import relay.app.config as config

    monkeypatch.delenv("RELAY_PUBLIC_ORIGIN")
    with pytest.raises(config.ConfigError):
        _config()


# --------------------------------------------------------------- keystore path
def test_keystore_path_default(monkeypatch: pytest.MonkeyPatch, tmp_path) -> None:
    _base_shared_env(monkeypatch, tmp_path)

    monkeypatch.delenv("SHARED_KEYSTORE_PATH")
    assert _config().keystore_path == "/data/tokenshare/keystore.json"


def test_keystore_path_custom(monkeypatch: pytest.MonkeyPatch, tmp_path) -> None:
    _base_shared_env(monkeypatch, tmp_path)
    assert _config().keystore_path.endswith("keystore.json")


# ----------------------------------------------------------------- env keys
def test_shared_env_keys_used_directly(monkeypatch: pytest.MonkeyPatch, tmp_path) -> None:
    """The upload env value IS the scalar — no raw32 mapping applied."""
    _base_shared_env(monkeypatch, tmp_path)
    cfg = _config()
    assert cfg.seller_key == cu.SHARED_SIGNER_KEY
    assert cfg.seller_address == cu.SIGNER_ADDR
    assert cfg.shared_kek == cu.KEK
    assert cfg.shared_upload_priv == int(cu.SHARED_UPLOAD_KEY, 16)


def test_shared_env_key_missing_fails_closed(monkeypatch: pytest.MonkeyPatch, tmp_path) -> None:
    _base_shared_env(monkeypatch, tmp_path)
    import relay.app.config as config

    for name in SHARED_KEY_ENVS:
        env_backup = {n: __import__("os").environ.get(n) for n in SHARED_KEY_ENVS}
        monkeypatch.delenv(name)
        with pytest.raises(config.ConfigError):
            _config()
        for n, v in env_backup.items():
            monkeypatch.setenv(n, v)


def test_shared_upload_key_zero_rejected(monkeypatch: pytest.MonkeyPatch, tmp_path) -> None:
    _base_shared_env(monkeypatch, tmp_path)
    import relay.app.config as config

    monkeypatch.setenv("SHARED_UPLOAD_KEY", "0x" + "00" * 32)
    with pytest.raises(config.ConfigError) as exc:
        _config()
    assert "scalar" in str(exc.value)


def test_shared_upload_key_order_bound_rejected(monkeypatch: pytest.MonkeyPatch, tmp_path) -> None:
    _base_shared_env(monkeypatch, tmp_path)
    import relay.app.config as config
    from relay.app.config import P256_ORDER

    monkeypatch.setenv("SHARED_UPLOAD_KEY", "0x" + format(P256_ORDER, "064x"))
    with pytest.raises(config.ConfigError):
        _config()


def test_shared_keys_wrong_length_rejected(monkeypatch: pytest.MonkeyPatch, tmp_path) -> None:
    _base_shared_env(monkeypatch, tmp_path)
    import relay.app.config as config

    monkeypatch.setenv("SHARED_SIGNER_KEY", "0x" + "11" * 31)
    with pytest.raises(config.ConfigError):
        _config()


# ------------------------------------------------------------- dstack derive
class _FakeDstack:
    """Bare legacy get_key surface: raw32 hex per path."""

    keys: dict[str, str] = {}

    def __init__(self, endpoint: str | None = None) -> None:
        self.endpoint = endpoint

    def get_key(self, path: str):
        import types

        if path not in type(self).keys:
            raise RuntimeError(f"no key at {path}")
        return types.SimpleNamespace(decode_key=lambda: type(self).keys[path])


def _dstack_keys() -> dict[str, str]:
    return {
        "tokenshare/shared/signer/v1": "0x" + "44" * 32,
        "tokenshare/shared/kek/v1": "0x" + "55" * 32,
        "tokenshare/shared/upload/v1": "0x" + "66" * 32,
    }


def test_shared_dstack_derives_three_keys(
    monkeypatch: pytest.MonkeyPatch, tmp_path
) -> None:
    _base_shared_env(monkeypatch, tmp_path)
    import relay.app.config as config

    socket = tmp_path / "dstack.sock"
    socket.write_bytes(b"")
    monkeypatch.setattr(config, "DEFAULT_DSTACK_SOCKET", str(socket))
    _FakeDstack.keys = _dstack_keys()
    monkeypatch.setattr(config, "dstack_client", _FakeDstack)

    cfg = config.load_config()
    assert cfg.seller_key == "0x" + "44" * 32
    assert cfg.seller_address == Account.from_key("0x" + "44" * 32).address
    assert cfg.shared_kek == bytes.fromhex("55" * 32)
    # raw32 → scalar = (int(raw) % (order-1)) + 1
    expected = (int.from_bytes(bytes.fromhex("66" * 32), "big") % (config.P256_ORDER - 1)) + 1
    assert cfg.shared_upload_priv == expected
    assert cfg.mode == "shared"


def test_shared_dstack_requires_sdk(
    monkeypatch: pytest.MonkeyPatch, tmp_path
) -> None:
    _base_shared_env(monkeypatch, tmp_path)
    import sys

    import relay.app.config as config

    socket = tmp_path / "dstack.sock"
    socket.write_bytes(b"")
    monkeypatch.setattr(config, "DEFAULT_DSTACK_SOCKET", str(socket))
    monkeypatch.setitem(sys.modules, "dstack_sdk", None)
    with pytest.raises(config.ConfigError) as exc:
        _config()
    assert "dstack-sdk" in str(exc.value)


def test_shared_dstack_short_material_fails(
    monkeypatch: pytest.MonkeyPatch, tmp_path
) -> None:
    _base_shared_env(monkeypatch, tmp_path)
    import relay.app.config as config

    socket = tmp_path / "dstack.sock"
    socket.write_bytes(b"")
    monkeypatch.setattr(config, "DEFAULT_DSTACK_SOCKET", str(socket))
    keys = _dstack_keys()
    keys["tokenshare/shared/upload/v1"] = "0x" + "66" * 31
    _FakeDstack.keys = keys
    monkeypatch.setattr(config, "dstack_client", _FakeDstack)
    with pytest.raises(config.ConfigError):
        _config()


# -------------------------------------------------- shared does not need keys
def test_shared_does_not_require_openai_or_seller_key(
    monkeypatch: pytest.MonkeyPatch, tmp_path
) -> None:
    _base_shared_env(monkeypatch, tmp_path)
    import os

    assert not os.environ.get("OPENAI_API_KEY")
    assert not os.environ.get("RELAY_SELLER_KEY")
    cfg = _config()
    assert cfg.openai_api_key is None
    assert cfg.openai_base_url is None


def test_single_mode_still_requires_openai_key(monkeypatch: pytest.MonkeyPatch, tmp_path) -> None:
    cu.setup_single_env(monkeypatch, "https://api.openai.com")
    import relay.app.config as config

    monkeypatch.delenv("OPENAI_API_KEY")
    with pytest.raises(config.ConfigError):
        _config()


# ------------------------------------------------------- origin .invalid TLD
# M15 bootstrap gate: the reserved .invalid TLD is ONLY allowed as the
# temporary https placeholder during RELAY_BOOTSTRAP_MODE=1 phase 1; normal
# shared mode rejects it outright (label-exact: notinvalid.com is a normal
# domain and must never be mis-rejected).
def test_origin_invalid_tld_rejected_in_normal_shared(
    monkeypatch: pytest.MonkeyPatch, tmp_path
) -> None:
    _base_shared_env(monkeypatch, tmp_path)
    import relay.app.config as config

    for bad in ("https://invalid", "https://placeholder.invalid", "https://foo.invalid:8443"):
        monkeypatch.setenv("RELAY_PUBLIC_ORIGIN", bad)
        with pytest.raises(config.ConfigError):
            _config()


def test_origin_notinvalid_com_is_a_normal_domain(
    monkeypatch: pytest.MonkeyPatch, tmp_path
) -> None:
    _base_shared_env(monkeypatch, tmp_path)
    monkeypatch.setenv("RELAY_PUBLIC_ORIGIN", "https://notinvalid.com")
    assert _config().public_origin == "https://notinvalid.com"


def test_origin_invalid_placeholder_allowed_only_in_bootstrap(
    monkeypatch: pytest.MonkeyPatch, tmp_path
) -> None:
    _base_shared_env(monkeypatch, tmp_path)
    import relay.app.config as config

    monkeypatch.setenv("RELAY_BOOTSTRAP_MODE", "1")
    monkeypatch.setenv("RELAY_PUBLIC_ORIGIN", "https://placeholder.invalid")
    cfg = _config()
    assert cfg.bootstrap_mode is True
    assert cfg.public_origin == "https://placeholder.invalid"

    # https only — the bootstrap placeholder may never downgrade to http.
    monkeypatch.setenv("RELAY_PUBLIC_ORIGIN", "http://placeholder.invalid")
    with pytest.raises(config.ConfigError):
        _config()
