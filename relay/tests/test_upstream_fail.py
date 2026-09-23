"""502 on upstream failure (non-200 body, connect error, read timeout) —
never settles; buyer refunds after ttl."""

from __future__ import annotations

import httpx
import pytest

import relay.app.main as m

from .conftest import chat_body, post_chat, setup_relay_env


def test_502_upstream_non_200_no_settle(
    client: Any, fake_chain: Any, mock_openai: Any
) -> None:
    mock_openai.mode = "error_500"
    r = post_chat(client, chat_body())
    assert r.status_code == 502
    assert fake_chain.settle_calls == []


def test_502_upstream_missing_usage_no_settle(
    client: Any, fake_chain: Any, mock_openai: Any
) -> None:
    """200 from OpenAI but no usage object → 502, no settle."""
    mock_openai.mode = "ok_no_usage"
    r = post_chat(client, chat_body())
    assert r.status_code == 502
    assert fake_chain.settle_calls == []


def test_502_connect_error_no_settle(
    monkeypatch: pytest.MonkeyPatch, fake_chain: Any
) -> None:
    """Upstream unreachable (closed port) → 502 via the connect-error branch."""
    from fastapi.testclient import TestClient

    import relay.app.main as m

    # Port 1 is closed → immediate connection refused.
    setup_relay_env(monkeypatch, "http://127.0.0.1:1/v1")
    with TestClient(m.app) as client:
        r = post_chat(client, chat_body())
    assert r.status_code == 502
    assert "upstream error" in r.json()["detail"]
    assert fake_chain.settle_calls == []


def test_502_read_timeout_no_settle(
    client: Any, fake_chain: Any, mock_openai: Any,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Fix #2: non-stream read timeout must fire → 502, no settle."""
    mock_openai.mode = "slow_json"
    mock_openai.slow_seconds = 0.5
    monkeypatch.setattr(
        m, "UPSTREAM_TIMEOUT_NON_STREAM", httpx.Timeout(1.0, read=0.1)
    )
    r = post_chat(client, chat_body())
    assert r.status_code == 502
    assert fake_chain.settle_calls == []


def test_non_stream_timeout_is_bounded_for_stream(
    client: Any, fake_chain: Any, mock_openai: Any,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Streaming keeps the open client-level timeout: a slow SSE stream is not
    killed by the non-stream read timeout (regression guard for fix #2)."""
    mock_openai.mode = "stream_slow"
    mock_openai.slow_seconds = 0.3
    monkeypatch.setattr(
        m, "UPSTREAM_TIMEOUT_NON_STREAM", httpx.Timeout(1.0, read=0.05)
    )
    body = chat_body(extra={"stream": True})
    r = post_chat(client, body)
    assert r.status_code == 200
    assert "data: [DONE]" in r.text
