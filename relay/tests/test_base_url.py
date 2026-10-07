"""R3 regression: OPENAI_BASE_URL must be host-root after normalization.

main.py forwards with the absolute path `/v1/chat/completions`. If the base
url keeps a `/v1` suffix (SDK-style paste or the old default), httpx merges
paths into `.../v1/v1/chat/completions` → real OpenAI 404. These tests are
path-sensitive: the mock upstream records each request path and every case
asserts the received path is exactly `/v1/chat/completions`.

Cases:
  ① env unset        → new default (host root) — no double /v1
  ② env `.../v1`     → suffix stripped
  ③ env `.../v1/`    → trailing slash + suffix stripped
"""

from __future__ import annotations

from typing import Any

import pytest
from fastapi.testclient import TestClient

import relay.app.config as cfg
from relay.app.config import (
    DEFAULT_OPENAI_BASE_URL,
    _normalize_openai_base_url,
)

from .conftest import chat_body, post_chat, setup_relay_env

EXPECTED_UPSTREAM_PATH = "/v1/chat/completions"


def _post_and_assert_path(
    monkeypatch: pytest.MonkeyPatch, base_url_env: str | None
) -> None:
    """Boot the relay against the mock upstream, forward one chat call, and
    assert the upstream received exactly the canonical path."""
    import relay.app.main as m

    body = chat_body()
    with TestClient(m.app) as client:
        r = post_chat(client, body)
    assert r.status_code == 200, r.text
    from .conftest import SELLER

    # The mock server records paths in reset_upstream/conftest handler.
    srv = _last_mock
    assert srv.received_paths, "mock upstream received no requests"
    assert srv.received_paths[0] == EXPECTED_UPSTREAM_PATH, (
        f"upstream got path {srv.received_paths[0]!r}, expected "
        f"{EXPECTED_UPSTREAM_PATH!r} (base_url={base_url_env!r})"
    )


_last_mock: Any = None


@pytest.fixture(autouse=True)
def _capture_mock(mock_openai: Any) -> None:
    global _last_mock
    _last_mock = mock_openai


def test_default_constant_is_host_root() -> None:
    """New default is the host root: no trailing slash, no /v1 suffix."""
    assert DEFAULT_OPENAI_BASE_URL == "https://api.openai.com"
    assert _normalize_openai_base_url(DEFAULT_OPENAI_BASE_URL) == (
        "https://api.openai.com"
    )


def test_normalize_variants() -> None:
    """Normalization: SDK-style /v1 and trailing slashes collapse to host root."""
    assert _normalize_openai_base_url("https://x.invalid/v1") == "https://x.invalid"
    assert _normalize_openai_base_url("https://x.invalid/v1/") == "https://x.invalid"
    assert _normalize_openai_base_url("https://x.invalid/") == "https://x.invalid"
    assert _normalize_openai_base_url("https://x.invalid") == "https://x.invalid"
    assert _normalize_openai_base_url("  ") == DEFAULT_OPENAI_BASE_URL
    # Deeper SDK-style paths keep their own prefix, only /v1 tail is stripped.
    assert _normalize_openai_base_url(
        "https://proxy.invalid/openai/v1"
    ) == "https://proxy.invalid/openai"
    # Kimi Coding plan: /coding/v1 -> /coding (host gate then sees api.kimi.com).
    assert _normalize_openai_base_url(
        "https://api.kimi.com/coding/v1"
    ) == "https://api.kimi.com/coding"
    # OpenCode Zen: /zen/v1 -> /zen (host gate then sees opencode.ai).
    assert _normalize_openai_base_url(
        "https://opencode.ai/zen/v1"
    ) == "https://opencode.ai/zen"
    # Qwen TokenPlan: /compatible-mode/v1 -> /compatible-mode
    # (host gate then sees token-plan.maas.qianwenaiapi.com).
    assert _normalize_openai_base_url(
        "https://token-plan.maas.qianwenaiapi.com/compatible-mode/v1"
    ) == "https://token-plan.maas.qianwenaiapi.com/compatible-mode"
    assert _normalize_openai_base_url(
        "https://token-plan.maas.qianwenaiapi.com/compatible-mode/v1/"
    ) == "https://token-plan.maas.qianwenaiapi.com/compatible-mode"


def test_zen_base_composes_official_forward_url() -> None:
    """OpenCode Zen (opencode.ai) normalization + composition pin, following
    the Kimi /coding precedent: the SDK-style base https://opencode.ai/zen/v1
    strips to https://opencode.ai/zen, and forwarding with the absolute path
    /v1/chat/completions composes exactly https://opencode.ai/zen/v1/chat/
    completions (no /zen/v1/v1 doubling)."""
    from relay.app.main import CHAT_COMPLETIONS_PATH

    normalized = _normalize_openai_base_url("https://opencode.ai/zen/v1")
    assert normalized == "https://opencode.ai/zen"
    assert (
        normalized + CHAT_COMPLETIONS_PATH
        == "https://opencode.ai/zen/v1/chat/completions"
    )
    # Same composition for the models path (startup / verify-upstream probe).
    assert (
        normalized + "/v1/models" == "https://opencode.ai/zen/v1/models"
    )


def test_qwen_token_plan_base_composes_official_forward_url() -> None:
    """Qwen TokenPlan (token-plan.maas.qianwenaiapi.com) normalization +
    composition pin, following the Kimi /coding and Zen /zen precedents: the
    SDK-style base https://token-plan.maas.qianwenaiapi.com/compatible-mode/v1
    strips to https://token-plan.maas.qianwenaiapi.com/compatible-mode, and
    forwarding with the absolute path /v1/chat/completions composes exactly
    https://token-plan.maas.qianwenaiapi.com/compatible-mode/v1/chat/
    completions (no /compatible-mode/v1/v1 doubling — matches the live
    OpenAI-compatible endpoint probed 2026-10-07)."""
    from relay.app.main import CHAT_COMPLETIONS_PATH

    normalized = _normalize_openai_base_url(
        "https://token-plan.maas.qianwenaiapi.com/compatible-mode/v1"
    )
    assert normalized == (
        "https://token-plan.maas.qianwenaiapi.com/compatible-mode"
    )
    assert (
        normalized + CHAT_COMPLETIONS_PATH
        == "https://token-plan.maas.qianwenaiapi.com"
        "/compatible-mode/v1/chat/completions"
    )
    # Same composition for the models path (startup / verify-upstream probe).
    assert (
        normalized + "/v1/models"
        == "https://token-plan.maas.qianwenaiapi.com"
        "/compatible-mode/v1/models"
    )


def test_forward_no_double_v1_env_unset(
    monkeypatch: pytest.MonkeyPatch, fake_chain: Any
) -> None:
    """① env unset → default base url (host root) → forward path /v1/chat/completions.

    DEFAULT_OPENAI_BASE_URL is patched to the mock host root (same value shape
    as the real default) so the request lands on the local mock instead of
    api.openai.com; load_config's default-branch and normalization run for real.
    """
    srv = _last_mock
    monkeypatch.setattr(
        cfg, "DEFAULT_OPENAI_BASE_URL", f"http://127.0.0.1:{srv.server_port}"
    )
    setup_relay_env(monkeypatch, f"http://127.0.0.1:{srv.server_port}/v1")
    monkeypatch.delenv("OPENAI_BASE_URL", raising=False)
    _post_and_assert_path(monkeypatch, None)


def test_forward_no_double_v1_env_sdk_style(
    monkeypatch: pytest.MonkeyPatch, fake_chain: Any
) -> None:
    """② env set to SDK-style `.../v1` → normalized to host root → canonical path."""
    srv = _last_mock
    setup_relay_env(monkeypatch, f"http://127.0.0.1:{srv.server_port}/v1")
    _post_and_assert_path(monkeypatch, f"http://127.0.0.1:{srv.server_port}/v1")


def test_forward_no_double_v1_env_trailing_slash(
    monkeypatch: pytest.MonkeyPatch, fake_chain: Any
) -> None:
    """③ env set to `.../v1/` (trailing slash) → normalized to host root."""
    srv = _last_mock
    setup_relay_env(monkeypatch, f"http://127.0.0.1:{srv.server_port}/v1/")
    _post_and_assert_path(monkeypatch, f"http://127.0.0.1:{srv.server_port}/v1/")
