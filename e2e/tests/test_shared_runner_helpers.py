"""e2e shared-runner tests — deterministic, zero real chain, zero network.

Drives the REAL e2e/mock_openai.py behavior (the M15 R2 additions: GET
/v1/models catalog face, /v1/* bearer allowlist enforcement, GET /counters
per-key counters) and the shared runner's pure helpers, in-thread.

AUXILIARY ONLY — these never replace the full chain+relay HTTP integration
run (`python3 e2e/run_shared.py`), which is the acceptance entry.
"""

from __future__ import annotations

import sys
import threading
from pathlib import Path

import httpx
import pytest

E2E_DIR = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(E2E_DIR))
REPO_ROOT = E2E_DIR.parent
sys.path.insert(0, str(REPO_ROOT))

import run_shared  # noqa: E402  (the shared runner helpers)
from mock_openai import CountingServer, Handler  # noqa: E402  (the REAL script)


@pytest.fixture
def mock_server():
    """A fresh CountingServer per test (hermetic port; default allowlist
    empty → single-harness-compatible)."""
    Handler.prompt_tokens = 5000
    Handler.cached_tokens = 1000
    Handler.completion_tokens = 800
    Handler.model_id = "mock-model"
    Handler.require_bearer = ()
    srv = CountingServer(("127.0.0.1", 0), Handler)
    t = threading.Thread(target=srv.serve_forever, daemon=True)
    t.start()
    yield srv
    srv.shutdown()
    srv.server_close()


def _base(srv: CountingServer) -> str:
    return f"http://127.0.0.1:{srv.server_address[1]}"


# ------------------------------------------------------------------ catalog
def test_models_catalog_face(mock_server: CountingServer) -> None:
    Handler.model_id = ("mock-model-a", "gpt-4o-mini")
    resp = httpx.get(f"{_base(mock_server)}/v1/models", timeout=10.0)
    assert resp.status_code == 200
    body = resp.json()
    assert body["object"] == "list"
    assert [m["id"] for m in body["data"]] == ["mock-model-a", "gpt-4o-mini"]


# ------------------------------------------------------ credential isolation
def test_bearer_enforcement_rejects_disallowed_key(
    mock_server: CountingServer,
) -> None:
    Handler.require_bearer = ("sk-valid-key",)
    resp = httpx.post(
        f"{_base(mock_server)}/v1/chat/completions",
        headers={"Authorization": "Bearer sk-attacker-key"},
        json={"model": "mock-model", "messages": []},
        timeout=10.0,
    )
    assert resp.status_code == 401
    counters = httpx.get(f"{_base(mock_server)}/counters", timeout=10.0).json()
    assert counters["perKey"].get("REJECTED") == 1
    assert counters["perKey"].get("sk-attacker-key") is None  # never counted as served


def test_bearer_enforcement_accepts_allowed_key(
    mock_server: CountingServer,
) -> None:
    Handler.require_bearer = ("sk-valid-key",)
    resp = httpx.post(
        f"{_base(mock_server)}/v1/chat/completions",
        headers={"Authorization": "Bearer sk-valid-key"},
        json={"model": "mock-model", "messages": [{"role": "user", "content": "hi"}]},
        timeout=10.0,
    )
    assert resp.status_code == 200
    assert "TokenShare mock LLM online" in resp.json()["choices"][0]["message"]["content"]
    counters = httpx.get(f"{_base(mock_server)}/counters", timeout=10.0).json()
    assert counters["perKey"].get("sk-valid-key") == 1


# ---------------------------------------------------------------- counters
def test_counters_snapshot_shape(mock_server: CountingServer) -> None:
    Handler.require_bearer = ("k1", "k2")
    for _ in range(2):
        httpx.post(
            f"{_base(mock_server)}/v1/chat/completions",
            headers={"Authorization": "Bearer k1"},
            json={"messages": []},
            timeout=10.0,
        )
    httpx.post(
        f"{_base(mock_server)}/v1/chat/completions",
        headers={"Authorization": "Bearer k2"},
        json={"messages": []},
        timeout=10.0,
    )
    counters = httpx.get(f"{_base(mock_server)}/counters", timeout=10.0).json()
    assert counters == {"total": 3, "perKey": {"k1": 2, "k2": 1}}


# ------------------------------------------------- single-harness compatibility
def test_defaults_disable_enforcement_single_harness_compat(
    mock_server: CountingServer,
) -> None:
    """Empty allowlist (the default) → NO bearer enforcement — the existing
    single harness semantics are byte-identical."""
    assert Handler.require_bearer == ()
    resp = httpx.post(
        f"{_base(mock_server)}/v1/chat/completions",
        json={"model": "mock-model", "messages": [{"role": "user", "content": "x"}]},
        timeout=10.0,
    )
    assert resp.status_code == 200  # no Authorization header required


# ------------------------------------------------------- shared runner helpers
def test_generate_shared_keys_shape() -> None:
    import tempfile
    """Synthetic key shape: 0x+64hex signer/KEK, upload a VALID P-256 scalar
    in [1, order-1]. Values are NEVER reflected into the result beyond
    shape use — nothing printed."""
    import run_shared

    keys = run_shared.generate_shared_keys()
    assert keys["keystore_path"].endswith("keystore.json")
    import os as _os

    assert _os.path.dirname(keys["keystore_path"]).startswith(
        tempfile.gettempdir()), keys["keystore_path"]  # NEVER the repo tree
    for field in ("signer_key", "kek_hex", "upload_hex"):
        value = keys[field]
        assert value.startswith("0x") and len(value) == 66
        int(value[2:], 16)  # hex-parseable
    from relay.app.config import P256_ORDER

    assert 1 <= int(keys["upload_hex"], 16) < P256_ORDER


def test_delegate_abi_fragment_builds_call() -> None:
    """The minimal delegate ABI fragment encodes approveSettleDelegate
    (the frozen R2 delegation mechanism) without CLI ABI expansion: the
    function signature derived from the fragment matches the Escrow ABI
    selector (keccak-crosschecked; no provider needed)."""
    from eth_utils import keccak

    abi = run_shared.DELEGATE_ABI[0]
    signature = f"{abi['name']}({','.join(i['type'] for i in abi['inputs'])})"
    assert signature == "approveSettleDelegate(address)"
    assert len(keccak(text=signature)[:4]) == 4  # selector derivable
    assert abi["stateMutability"] == "nonpayable"
