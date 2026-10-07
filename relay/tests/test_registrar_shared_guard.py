"""Focused tests: the registrar refuses RELAY_MODE=shared EARLY — before any
key derivation (dstack get_key), any network access and any registration —
with a clear, secret-free error; and it stays fully compatible with the
single/default behavior (M15 R1 guard, B3).

Two layers:
  * in-process (monkeypatched derive_seller_key / connect_web3 that FAIL the
    test if ever called — proves the guard fires first);
  * real subprocess (`python relay/registrar.py`) with all REGISTRAR_* env
    vars AND a nonexistent dstack socket path — proves the refusal wins over
    both the config path and the derivation path, end to end, and that
    single/default mode keeps its old behavior.
"""

from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

import pytest

from relay import registrar

REGISTRAR_PATH = Path(registrar.__file__).resolve()

# Complete required env (so a config failure CANNOT mask the guard).
REQUIRED_ENV = {
    "RPC_URL": "https://testnet-rpc.example/v3",
    "CHAIN_ID": "10143",
    "REGISTRY_ADDR": "0x00000000000000000000000000000000000000aa",
    "REGISTRAR_ENDPOINT": "https://app-8787.gateway.example",
    "REGISTRAR_MODELS": "glm-5.3-flash",
    "REGISTRAR_PRICES": "1000000:2000000:3000000",
}


def _base_env(extra: dict[str, str]) -> dict[str, str]:
    """Deterministic subprocess env: never inherit the outer RELAY_MODE."""
    env = {
        k: v
        for k, v in os.environ.items()
        if k not in ("RELAY_MODE", "PYTHONPATH", "COVERAGE_PROCESS_START")
    }
    env.update(extra)
    return env


# ---------------------------------------------------------------------------
# In-process: the guard fires BEFORE derive/network and matches config.py
# semantics (strip + lowercase).
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("raw_mode", ["shared", "SHARED", "  shared  ", "Shared"])
def test_shared_mode_refused_before_derive_and_network(monkeypatch, raw_mode):
    """Guard error, and neither key derivation nor RPC wiring is touched."""
    calls: list[str] = []

    def _no_derive(socket_path):  # pragma: no cover — must never run
        calls.append("derive")
        raise AssertionError("derive_seller_key called despite shared guard")

    def _no_web3(rpc_url, expected_chain_id):  # pragma: no cover — never run
        calls.append("network")
        raise AssertionError("connect_web3 called despite shared guard")

    monkeypatch.setattr(registrar, "derive_seller_key", _no_derive)
    monkeypatch.setattr(registrar, "connect_web3", _no_web3)
    monkeypatch.setenv("RELAY_MODE", raw_mode)
    for name, value in REQUIRED_ENV.items():
        monkeypatch.setenv(name, value)
    monkeypatch.setenv("DSTACK_SOCKET_PATH", "/nonexistent/dstack.sock")

    with pytest.raises(registrar.RegistrarError) as exc:
        registrar.main()

    message = str(exc.value)
    assert calls == [], "guard must fire before derive/network"
    assert "RELAY_MODE=shared" in message
    assert "single-mode ONLY" in message
    assert "seller's own external EOA" in message
    assert "approveSettleDelegate" in message
    # Safe: no key material / socket path / endpoint value echoed.
    assert "0x" not in message
    assert "dstack.sock" not in message


def test_single_mode_still_reaches_derivation(monkeypatch):
    """Default compatibility: no RELAY_MODE (or 'single') → the old flow runs;
    the guard does not trip and the failure is the (sentinel) derivation one,
    not the shared refusal."""
    calls: list[str] = []

    def _sentinel_derive(socket_path):
        calls.append("derive")
        raise registrar.RegistrarError("SOCKET_SENTINEL_not_shared_refusal")

    monkeypatch.setattr(registrar, "derive_seller_key", _sentinel_derive)
    monkeypatch.delenv("RELAY_MODE", raising=False)
    for name, value in REQUIRED_ENV.items():
        monkeypatch.setenv(name, value)
    with pytest.raises(registrar.RegistrarError) as exc:
        registrar.main()
    assert "SOCKET_SENTINEL" in str(exc.value)
    assert calls == ["derive"]

    monkeypatch.setenv("RELAY_MODE", "single")
    with pytest.raises(registrar.RegistrarError) as exc:
        registrar.main()
    assert "SOCKET_SENTINEL" in str(exc.value)
    assert calls == ["derive", "derive"]


def test_guard_is_first_action_in_main(monkeypatch):
    """Even load_env must not run before the guard (a shared .env is not
    guaranteed to carry REGISTRAR_* vars — the shared error must win)."""
    monkeypatch.setattr(
        registrar,
        "load_env",
        lambda: (_ for _ in ()).throw(AssertionError("load_env before guard")),
    )
    monkeypatch.setenv("RELAY_MODE", "shared")
    with pytest.raises(registrar.RegistrarError) as exc:
        registrar.main()
    assert "RELAY_MODE=shared" in str(exc.value)


# ---------------------------------------------------------------------------
# Subprocess: end-to-end early exit through the real __main__ entrypoint.
# ---------------------------------------------------------------------------


def _run_registrar(env: dict[str, str]) -> subprocess.CompletedProcess:
    return subprocess.run(
        [sys.executable, str(REGISTRAR_PATH)],
        capture_output=True,
        text=True,
        timeout=60,
        env=env,
    )


def test_subprocess_shared_refusal_wins_over_config_and_socket(tmp_path):
    """RELAY_MODE=shared + FULL required env + nonexistent socket → the guard
    message (not the missing-vars or socket error), non-zero exit, and no
    address/network output."""
    missing_sock = tmp_path / "absent.sock"
    env = _base_env({**REQUIRED_ENV, "RELAY_MODE": "shared",
                     "DSTACK_SOCKET_PATH": str(missing_sock)})
    proc = _run_registrar(env)

    assert proc.returncode != 0
    assert "RELAY_MODE=shared" in proc.stderr
    assert "single-mode ONLY" in proc.stderr
    # Guard fired BEFORE the config path (all required vars were present) and
    # BEFORE derivation (the socket path does not exist — a derivation failure
    # would instead say "dstack socket not found").
    assert "missing required environment" not in proc.stderr
    assert "dstack socket not found" not in proc.stderr
    assert "TEE-derived seller address" not in proc.stdout
    assert "tx sent" not in proc.stdout


def test_subprocess_shared_refusal_with_empty_config(tmp_path):
    """RELAY_MODE=shared with NO other env at all → still the shared refusal,
    proving the guard precedes load_env entirely."""
    env = _base_env({"RELAY_MODE": "shared",
                     "DSTACK_SOCKET_PATH": str(tmp_path / "absent.sock")})
    proc = _run_registrar(env)
    assert proc.returncode != 0
    assert "RELAY_MODE=shared" in proc.stderr
    assert "missing required environment" not in proc.stderr


def test_subprocess_default_mode_keeps_old_behavior(tmp_path):
    """RELAY_MODE unset, env otherwise empty → the OLD pre-guard behavior:
    the missing-required-vars error, exit 1 — not the shared refusal."""
    env = _base_env({"DSTACK_SOCKET_PATH": str(tmp_path / "absent.sock")})
    proc = _run_registrar(env)
    assert proc.returncode == 1
    assert "missing required environment variable(s)" in proc.stderr
    assert "RELAY_MODE=shared" not in proc.stderr
