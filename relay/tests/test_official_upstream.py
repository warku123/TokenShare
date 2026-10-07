"""Official-endpoint-only upstream policy (anti-poisoning, 2026-09-23):

  - startup: non-official OPENAI_BASE_URL host → ConfigError without the
    explicit ALLOW_CUSTOM_UPSTREAM=1 flag; with the flag → WARNING logged;
  - per-request: unknown model prefix → 400; provider/host mismatch → 400;
  - usage: LAST non-null streaming usage wins; cached_tokens fallback chain
    (prompt_tokens_details.cached_tokens → top-level usage.cached_tokens → 0);
  - receipt: upstreamHost + model fields present, sign/verify round-trip.
"""

from __future__ import annotations

import logging
from typing import Any

import pytest
from fastapi import HTTPException
from fastapi.testclient import TestClient

import relay.app.config as cfg
import relay.app.main as m
from relay.app.config import (
    ConfigError,
    OFFICIAL_HOST_PROVIDER,
    OFFICIAL_UPSTREAM_HOSTS,
    host_provider,
    provider_for_model,
)
from relay.app.receipt import decode_x_receipt

from .conftest import ACTUAL, CHAIN_ID, SELLER, chat_body, post_chat, setup_relay_env


# ----------------------------------------------------------------- constants


def test_official_allowlist_shape() -> None:
    assert OFFICIAL_UPSTREAM_HOSTS == frozenset(
        {
            "api.openai.com",
            "api.moonshot.cn",
            "api.moonshot.ai",
            "api.kimi.com",
            "opencode.ai",
            "token-plan.maas.qianwenaiapi.com",
        }
    )
    assert OFFICIAL_HOST_PROVIDER["api.openai.com"] == "openai"
    assert OFFICIAL_HOST_PROVIDER["api.moonshot.cn"] == "moonshot"
    assert OFFICIAL_HOST_PROVIDER["api.moonshot.ai"] == "moonshot"
    assert OFFICIAL_HOST_PROVIDER["api.kimi.com"] == "moonshot"
    assert OFFICIAL_HOST_PROVIDER["opencode.ai"] == "zen"
    assert OFFICIAL_HOST_PROVIDER["token-plan.maas.qianwenaiapi.com"] == "qwen"


def test_provider_for_model_prefixes() -> None:
    for name in ("gpt-4o-mini", "o3", "o1-mini", "o4-mini", "chatgpt-4o-latest"):
        assert provider_for_model(name) == "openai", name
    for name in (
        "kimi-k2.6",
        "moonshot-v1-8k",
        "kimi-latest",
        "kimi-for-coding",
        "kimi-for-coding-highspeed",
        "k3",
        "k3-256k",
    ):
        assert provider_for_model(name) == "moonshot", name
    for name in ("deepseek-v4.1-flash", "deepseek-chat", "glm-5.3-flash"):
        assert provider_for_model(name) == "zen", name
    # Qwen TokenPlan face: bare "qwen" prefix, no separator (like moonshot's
    # "k3") — qwen3.8-max etc. carry the provider face directly.
    for name in ("qwen3.8-max", "qwen-max", "qwen-plus", "qwen3-coder-plus"):
        assert provider_for_model(name) == "qwen", name
    # Unknown / non-official prefixes must NOT be guessed.
    assert provider_for_model("claude-3-sonnet") is None
    assert provider_for_model("my-fake-model") is None
    assert provider_for_model("gpt") is None  # prefix must match exactly
    assert provider_for_model("qwe") is None  # "qwen" prefix must match exactly
    assert provider_for_model("") is None
    assert provider_for_model(None) is None
    assert provider_for_model(42) is None


def test_host_provider_mapping() -> None:
    assert host_provider("https://api.openai.com") == "openai"
    assert host_provider("https://api.moonshot.cn") == "moonshot"
    assert host_provider("https://api.moonshot.ai") == "moonshot"
    # Kimi Coding plan base: the /coding path is irrelevant to the host gate;
    # host_provider parses the NORMALIZED (trailing /v1 stripped) URL.
    assert host_provider("https://api.kimi.com") == "moonshot"
    assert host_provider("https://api.kimi.com/coding") == "moonshot"
    assert host_provider("https://api.kimi.com/coding/") == "moonshot"
    # OpenCode Zen: the /zen path is irrelevant to the host gate;
    # host_provider parses the NORMALIZED (trailing /v1 stripped) URL.
    assert host_provider("https://opencode.ai") == "zen"
    assert host_provider("https://opencode.ai/zen") == "zen"
    assert host_provider("https://opencode.ai/zen/") == "zen"
    # Qwen TokenPlan: the /compatible-mode path is irrelevant to the host
    # gate; host_provider parses the NORMALIZED (trailing /v1 stripped) URL.
    assert (
        host_provider("https://token-plan.maas.qianwenaiapi.com") == "qwen"
    )
    assert (
        host_provider(
            "https://token-plan.maas.qianwenaiapi.com/compatible-mode"
        )
        == "qwen"
    )
    assert (
        host_provider(
            "https://token-plan.maas.qianwenaiapi.com/compatible-mode/"
        )
        == "qwen"
    )
    # Lookalike hosts of the Qwen endpoint must NOT pass.
    assert (
        host_provider("https://token-plan.maas.qianwenaiapi.com.evil.invalid")
        is None
    )
    # Scheme-relative case-insensitivity of hosts.
    assert host_provider("https://API.OPENAI.COM") == "openai"
    assert host_provider("http://127.0.0.1:9") is None
    assert host_provider("https://evil.example.com") is None


# ------------------------------------------------------- startup host gate


def test_startup_rejects_non_official_host_without_flag(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    setup_relay_env(monkeypatch, "http://127.0.0.1:1/v1")
    monkeypatch.delenv(cfg.ENV_ALLOW_CUSTOM_UPSTREAM, raising=False)
    with pytest.raises(ConfigError, match="official"):
        with TestClient(m.app):
            pass


def test_startup_rejects_lookalike_official_host(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A lookalike domain must not pass the allowlist."""
    setup_relay_env(monkeypatch, "https://api.openai.com.evil.invalid")
    monkeypatch.delenv(cfg.ENV_ALLOW_CUSTOM_UPSTREAM, raising=False)
    with pytest.raises(ConfigError, match="official"):
        with TestClient(m.app):
            pass


def test_startup_allows_official_host_without_flag(
    monkeypatch: pytest.MonkeyPatch, fake_chain: Any
) -> None:
    """An official host boots normally even without the flag. Only /health is
    called, so nothing is forwarded to the real network."""
    setup_relay_env(monkeypatch, "https://api.openai.com")
    monkeypatch.delenv(cfg.ENV_ALLOW_CUSTOM_UPSTREAM, raising=False)
    with TestClient(m.app) as client:
        assert client.get("/health").status_code == 200


def test_startup_accepts_kimi_coding_plan_base(
    monkeypatch: pytest.MonkeyPatch, fake_chain: Any
) -> None:
    """Kimi Coding plan base (SDK-style /coding/v1) passes the startup
    allowlist: normalization strips the trailing /v1 and host_provider must
    resolve the RESULTING URL's host (api.kimi.com) as official."""
    setup_relay_env(monkeypatch, "https://api.kimi.com/coding/v1")
    monkeypatch.delenv(cfg.ENV_ALLOW_CUSTOM_UPSTREAM, raising=False)
    with TestClient(m.app) as client:
        assert client.get("/health").status_code == 200


def test_startup_accepts_zen_base(
    monkeypatch: pytest.MonkeyPatch, fake_chain: Any
) -> None:
    """OpenCode Zen base (SDK-style /zen/v1) passes the startup allowlist:
    normalization strips the trailing /v1 and host_provider must resolve the
    RESULTING URL's host (opencode.ai) as official."""
    setup_relay_env(monkeypatch, "https://opencode.ai/zen/v1")
    monkeypatch.delenv(cfg.ENV_ALLOW_CUSTOM_UPSTREAM, raising=False)
    with TestClient(m.app) as client:
        assert client.get("/health").status_code == 200


def test_startup_accepts_qwen_token_plan_base(
    monkeypatch: pytest.MonkeyPatch, fake_chain: Any
) -> None:
    """Qwen TokenPlan base (SDK-style /compatible-mode/v1) passes the startup
    allowlist: normalization strips the trailing /v1 and host_provider must
    resolve the RESULTING URL's host (token-plan.maas.qianwenaiapi.com) as
    official."""
    setup_relay_env(
        monkeypatch,
        "https://token-plan.maas.qianwenaiapi.com/compatible-mode/v1",
    )
    monkeypatch.delenv(cfg.ENV_ALLOW_CUSTOM_UPSTREAM, raising=False)
    with TestClient(m.app) as client:
        assert client.get("/health").status_code == 200


def test_startup_rejects_qwen_lookalike_host(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A lookalike of the Qwen TokenPlan host must not pass the allowlist."""
    setup_relay_env(
        monkeypatch,
        "https://token-plan.maas.qianwenaiapi.com.evil.invalid/compatible-mode/v1",
    )
    monkeypatch.delenv(cfg.ENV_ALLOW_CUSTOM_UPSTREAM, raising=False)
    with pytest.raises(ConfigError, match="official"):
        with TestClient(m.app):
            pass


def test_startup_flag_allows_custom_host_with_warning(
    monkeypatch: pytest.MonkeyPatch,
    fake_chain: Any,
    caplog: pytest.LogCaptureFixture,
) -> None:
    setup_relay_env(monkeypatch, "http://127.0.0.1:1/v1")
    monkeypatch.setenv(cfg.ENV_ALLOW_CUSTOM_UPSTREAM, "1")
    with caplog.at_level(logging.WARNING, logger="tokenshare.relay"):
        with TestClient(m.app) as client:
            assert client.get("/health").status_code == 200
    assert any(
        "custom upstream enabled" in rec.message and "authenticity guarantee void" in rec.message
        for rec in caplog.records
    )


def test_startup_flag_other_values_do_not_enable(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Only '1' (after strip) enables; anything else does NOT (explicitness)."""
    for value in ("0", "true", "yes", "on", ""):
        setup_relay_env(monkeypatch, "http://127.0.0.1:1/v1")
        monkeypatch.setenv(cfg.ENV_ALLOW_CUSTOM_UPSTREAM, value)
        with pytest.raises(ConfigError, match="official"):
            with TestClient(m.app):
                pass


# ---------------------------------------------------- per-request gate (400)


def _state_with_base_url(monkeypatch: pytest.MonkeyPatch, base_url: str) -> None:
    """Boot a RelayState (FakeChain patched in) and install it as the module
    state, so the per-request gate can read the configured upstream."""
    import relay.app.config as config_mod

    setup_relay_env(monkeypatch, base_url)
    config = config_mod.load_config()
    st = m.RelayState(config)
    monkeypatch.setattr(m, "state", st)


def test_unknown_model_prefix_rejected_400(monkeypatch: pytest.MonkeyPatch) -> None:
    _state_with_base_url(monkeypatch, "https://api.moonshot.cn")
    with pytest.raises(HTTPException) as exc:
        m._check_model_provider_consistency({"model": "claude-3-5-sonnet"})
    assert exc.value.status_code == 400
    assert "official model catalog" in exc.value.detail

    with pytest.raises(HTTPException) as exc:
        m._check_model_provider_consistency({"model": "totally-fake-model"})
    assert exc.value.status_code == 400


def test_provider_mismatch_rejected_400(monkeypatch: pytest.MonkeyPatch) -> None:
    """A moonshot model name aimed at the OpenAI official host → 400."""
    _state_with_base_url(monkeypatch, "https://api.openai.com")
    with pytest.raises(HTTPException) as exc:
        m._check_model_provider_consistency({"model": "kimi-k2.6"})
    assert exc.value.status_code == 400
    assert "does not match" in exc.value.detail

    # And the reverse: an openai model aimed at the Kimi host.
    _state_with_base_url(monkeypatch, "https://api.moonshot.cn")
    with pytest.raises(HTTPException) as exc:
        m._check_model_provider_consistency({"model": "gpt-4o-mini"})
    assert exc.value.status_code == 400


def test_matching_provider_passes_gate(monkeypatch: pytest.MonkeyPatch) -> None:
    _state_with_base_url(monkeypatch, "https://api.moonshot.cn")
    m._check_model_provider_consistency({"model": "kimi-k2.6"})  # no raise
    _state_with_base_url(monkeypatch, "https://api.moonshot.ai")
    m._check_model_provider_consistency({"model": "moonshot-v1-8k"})  # no raise


def test_kimi_coding_plan_models_pass_gate(monkeypatch: pytest.MonkeyPatch) -> None:
    """Kimi Coding plan model face through the api.kimi.com upstream."""
    _state_with_base_url(monkeypatch, "https://api.kimi.com/coding/v1")
    m._check_model_provider_consistency({"model": "k3"})  # no raise
    m._check_model_provider_consistency({"model": "k3-256k"})  # no raise
    m._check_model_provider_consistency({"model": "kimi-for-coding"})  # no raise
    m._check_model_provider_consistency(
        {"model": "kimi-for-coding-highspeed"}
    )  # no raise


def test_zen_models_pass_gate(monkeypatch: pytest.MonkeyPatch) -> None:
    """OpenCode Zen model face through the opencode.ai upstream."""
    _state_with_base_url(monkeypatch, "https://opencode.ai/zen/v1")
    m._check_model_provider_consistency({"model": "deepseek-v4.1-flash"})  # no raise
    m._check_model_provider_consistency({"model": "glm-5.3-flash"})  # no raise


def test_qwen_models_pass_gate(monkeypatch: pytest.MonkeyPatch) -> None:
    """Qwen TokenPlan model face through the token-plan upstream."""
    _state_with_base_url(
        monkeypatch, "https://token-plan.maas.qianwenaiapi.com/compatible-mode/v1"
    )
    m._check_model_provider_consistency({"model": "qwen3.8-max"})  # no raise
    m._check_model_provider_consistency({"model": "qwen-max"})  # no raise
    m._check_model_provider_consistency({"model": "qwen-plus"})  # no raise


def test_qwen_host_rejects_foreign_provider_400(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A moonshot model name aimed at the token-plan host → 400 (kimi- prefix
    does not belong to the qwen provider)."""
    _state_with_base_url(
        monkeypatch, "https://token-plan.maas.qianwenaiapi.com/compatible-mode/v1"
    )
    with pytest.raises(HTTPException) as exc:
        m._check_model_provider_consistency({"model": "kimi-k2.6"})
    assert exc.value.status_code == 400
    assert "does not match" in exc.value.detail

    with pytest.raises(HTTPException) as exc:
        m._check_model_provider_consistency({"model": "glm-5.3-flash"})
    assert exc.value.status_code == 400
    assert "does not match" in exc.value.detail


def test_qwen_model_on_foreign_host_rejected_400(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A qwen model name aimed at other official hosts → 400."""
    _state_with_base_url(monkeypatch, "https://api.kimi.com/coding/v1")
    with pytest.raises(HTTPException) as exc:
        m._check_model_provider_consistency({"model": "qwen3.8-max"})
    assert exc.value.status_code == 400
    assert "does not match" in exc.value.detail

    _state_with_base_url(monkeypatch, "https://api.openai.com")
    with pytest.raises(HTTPException) as exc:
        m._check_model_provider_consistency({"model": "qwen3.8-max"})
    assert exc.value.status_code == 400


def test_zen_host_rejects_foreign_provider_400(monkeypatch: pytest.MonkeyPatch) -> None:
    """A moonshot model name aimed at opencode.ai → 400 (kimi- prefix does
    not belong to the zen provider)."""
    _state_with_base_url(monkeypatch, "https://opencode.ai/zen/v1")
    with pytest.raises(HTTPException) as exc:
        m._check_model_provider_consistency({"model": "kimi-for-coding"})
    assert exc.value.status_code == 400
    assert "does not match" in exc.value.detail


def test_kimi_host_rejects_zen_model_400(monkeypatch: pytest.MonkeyPatch) -> None:
    """A zen model name aimed at api.kimi.com → 400 (provider mismatch)."""
    _state_with_base_url(monkeypatch, "https://api.kimi.com/coding/v1")
    with pytest.raises(HTTPException) as exc:
        m._check_model_provider_consistency({"model": "deepseek-v4.1-flash"})
    assert exc.value.status_code == 400
    assert "does not match" in exc.value.detail


def test_k3_on_openai_host_rejected_400(monkeypatch: pytest.MonkeyPatch) -> None:
    """Provider mismatch still bites: a k3 model aimed at api.openai.com → 400."""
    _state_with_base_url(monkeypatch, "https://api.openai.com")
    with pytest.raises(HTTPException) as exc:
        m._check_model_provider_consistency({"model": "k3"})
    assert exc.value.status_code == 400
    assert "does not match" in exc.value.detail


def test_gate_on_custom_upstream_enforces_catalog_only(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """With the dev/test flag on a custom host, only the catalog-prefix half
    applies (a mock has no provider identity to compare against)."""
    _state_with_base_url(monkeypatch, "http://127.0.0.1:9")
    m._check_model_provider_consistency({"model": "kimi-k2.6"})  # no raise
    # zen prefixes are part of the official catalog now; on a custom upstream
    # the catalog half alone still applies (escape-hatch behavior unchanged).
    m._check_model_provider_consistency({"model": "deepseek-v4.1-flash"})  # no raise
    m._check_model_provider_consistency({"model": "glm-5.3-flash"})  # no raise
    with pytest.raises(HTTPException) as exc:
        m._check_model_provider_consistency({"model": "not-a-real-model"})
    assert exc.value.status_code == 400


def test_gate_runs_before_forwarding(
    client: Any, fake_chain: Any, mock_openai: Any
) -> None:
    """The full route 400s an unknown-prefix model and never touches the
    upstream (zero-cost rejection). The model is listed (so the listing check
    passes) but carries no official catalog prefix."""
    fake_chain.models = ["gpt-4o-mini", "poisoned-model-name"]
    r = post_chat(client, chat_body(model="poisoned-model-name"))
    assert r.status_code == 400
    assert "official model catalog" in r.json()["detail"]
    assert mock_openai.received == []
    assert fake_chain.settle_calls == []


# ------------------------------------------------------- usage robustness


def test_non_stream_cached_tokens_top_level_fallback(
    client: Any, fake_chain: Any, mock_openai: Any
) -> None:
    """Kimi-style top-level usage.cached_tokens (no prompt_tokens_details)
    feeds the tiered price; (7*25k + 93*50k + 20*100k)//1e6 = 6."""
    mock_openai.usage_override = {
        "prompt_tokens": 100,
        "completion_tokens": 20,
        "cached_tokens": 7,
    }
    r = post_chat(client, chat_body())
    assert r.status_code == 200
    assert fake_chain.settle_calls == [(42, 6)]
    receipt = decode_x_receipt(r.headers["X-Receipt"])
    assert receipt["message"]["cachedTokens"] == 7


def test_non_stream_null_fields_default_to_zero(
    client: Any, fake_chain: Any, mock_openai: Any
) -> None:
    """Explicit nulls (Kimi edge) behave like absent keys, never TypeError."""
    mock_openai.usage_override = {
        "prompt_tokens": 100,
        "completion_tokens": 20,
        "prompt_tokens_details": None,
        "cached_tokens": None,
    }
    r = post_chat(client, chat_body())
    assert r.status_code == 200
    # (100*50k + 20*100k) // 1e6 = 7 (no cached tier — cached defaults to 0)
    assert fake_chain.settle_calls == [(42, 7)]


def test_stream_last_non_null_usage_wins(
    client: Any, fake_chain: Any, mock_openai: Any
) -> None:
    """usage → null → null-with-finish_reason → [DONE]: the FIRST real usage
    (the only non-null one) must drive pricing; nulls must not erase it."""
    mock_openai.mode = "stream_usage_then_null"
    r = post_chat(client, chat_body(extra={"stream": True}))
    assert r.status_code == 200
    assert "data: [DONE]" in r.text
    assert fake_chain.settle_calls == [(42, ACTUAL)]


def test_extract_usage_unit_fallback_chain() -> None:
    assert m._extract_usage(
        {"prompt_tokens": 5, "completion_tokens": 2,
         "prompt_tokens_details": {"cached_tokens": 3}}
    ).cached_tokens == 3
    assert m._extract_usage(
        {"prompt_tokens": 5, "completion_tokens": 2, "cached_tokens": 4}
    ).cached_tokens == 4
    assert m._extract_usage(
        {"prompt_tokens": 5, "completion_tokens": 2,
         "prompt_tokens_details": {"cached_tokens": 3}, "cached_tokens": 9}
    ).cached_tokens == 3  # standard position wins
    assert m._extract_usage({"prompt_tokens": 1, "completion_tokens": 1}).cached_tokens == 0
    assert m._extract_usage(
        {"prompt_tokens": None, "completion_tokens": None}
    ) == m.Usage(prompt_tokens=0, cached_tokens=0, completion_tokens=0)


# ------------------------------------------------- receipt authenticity fields


def test_receipt_carries_upstream_host_and_model(
    client: Any, fake_chain: Any
) -> None:
    r = post_chat(client, chat_body())
    assert r.status_code == 200
    receipt = decode_x_receipt(r.headers["X-Receipt"])
    assert receipt["message"]["upstreamHost"] == "127.0.0.1"
    assert receipt["message"]["model"] == "gpt-4o-mini"
    # GET /receipt serves the same fields.
    served = client.get("/receipt/42").json()
    assert served["message"]["upstreamHost"] == "127.0.0.1"
    assert served["message"]["model"] == "gpt-4o-mini"


def test_upstream_host_extraction_official_hosts() -> None:
    """The receipt's upstreamHost derives from the configured base URL's
    host — for official hosts it is the official provider host itself."""
    from relay.app.main import _upstream_host

    assert _upstream_host("https://api.moonshot.cn") == "api.moonshot.cn"
    assert _upstream_host("https://api.moonshot.cn/v1") == "api.moonshot.cn"
    assert _upstream_host("https://api.openai.com") == "api.openai.com"
    assert _upstream_host("http://127.0.0.1:8787") == "127.0.0.1"


def test_build_receipt_tamper_covers_new_fields(
    client: Any, fake_chain: Any
) -> None:
    """Tampering the NEW fields must break verification like any other."""
    from eth_account import Account
    from eth_account.messages import encode_typed_data

    from relay.app.receipt import RECEIPT_TYPES

    r = post_chat(client, chat_body())
    receipt = decode_x_receipt(r.headers["X-Receipt"])
    for field, evil in (("upstreamHost", "evil.example.com"), ("model", "fake-model")):
        tampered = dict(receipt["message"], **{field: evil})
        encoded = encode_typed_data(
            full_message={
                "types": RECEIPT_TYPES,
                "primaryType": "Receipt",
                "domain": receipt["domain"],
                "message": tampered,
            }
        )
        recovered = Account.recover_message(encoded, signature=receipt["signature"])
        assert recovered != SELLER, field


def test_receipt_domain_unchanged_by_v1_1_fields(client: Any, fake_chain: Any) -> None:
    r = post_chat(client, chat_body())
    receipt = decode_x_receipt(r.headers["X-Receipt"])
    assert receipt["domain"] == {
        "name": "TokenShare Relay",
        "version": "1",
        "chainId": CHAIN_ID,
    }