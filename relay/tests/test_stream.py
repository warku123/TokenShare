"""Streaming behavior: caller stream_options preserved (injection must not
clobber existing keys), final-chunk usage drives pricing, missing usage or
client disconnect → no settle (both WARNING-logged)."""

from __future__ import annotations

import asyncio
import json
import logging
import types
from typing import Any

import httpx
import pytest

import relay.app.main as m
from relay.app.pricing import Prices

from .conftest import ACTUAL, chat_body, post_chat, signed_headers


def test_stream_preserves_caller_stream_options(
    client: Any, fake_chain: Any, mock_openai: Any
) -> None:
    """Injection must set include_usage=True while keeping caller keys."""
    body = chat_body(extra={"stream": True, "stream_options": {"user_key": 1}})
    r = post_chat(client, body)
    assert r.status_code == 200
    sent = mock_openai.received[0]
    assert sent["stream_options"]["user_key"] == 1
    assert sent["stream_options"]["include_usage"] is True


def test_stream_settles_from_final_chunk_usage(
    client: Any, fake_chain: Any, mock_openai: Any
) -> None:
    mock_openai.mode = "stream_ok"
    body = chat_body(extra={"stream": True})
    r = post_chat(client, body)
    assert r.status_code == 200
    assert r.headers["content-type"].startswith("text/event-stream")
    text = r.text
    assert "data: [DONE]" in text
    # passthrough fidelity: events re-emitted, including the usage chunk
    assert '"content"' in text
    assert fake_chain.settle_calls == [(42, ACTUAL)]

    # stream responses carry the receipt via the receipt endpoint
    r2 = client.get("/receipt/42")
    assert r2.status_code == 200
    assert r2.json()["message"]["actualAmount"] == ACTUAL


def test_stream_without_usage_never_settles(
    client: Any, fake_chain: Any, mock_openai: Any,
    caplog: pytest.LogCaptureFixture,
) -> None:
    mock_openai.mode = "stream_no_usage"
    body = chat_body(extra={"stream": True})
    with caplog.at_level(logging.WARNING, logger="tokenshare.relay"):
        r = post_chat(client, body)
    assert r.status_code == 200
    assert fake_chain.settle_calls == []
    assert any("without usage" in rec.message for rec in caplog.records)
    r2 = client.get("/receipt/42")
    assert r2.status_code == 404


def test_stream_client_disconnect_never_settles(
    client: Any, fake_chain: Any, mock_openai: Any,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """Consumer closes the stream after the first event → GeneratorExit inside
    the SSE iterator → WARNING + no settle, no receipt."""
    mock_openai.mode = "stream_slow"
    body = chat_body(extra={"stream": True})
    body_dict = json.loads(body)
    prices = Prices(price_cached_in=25000, price_input=50000, price_output=100000)
    base_url = f"http://127.0.0.1:{mock_openai.server_port}/v1"

    async def drive() -> None:
        st = m._get_state()
        # Isolated httpx client so the disconnect test never shares pooled
        # connections with the portal event loop of the TestClient.
        isolated = types.SimpleNamespace(
            chain=st.chain,
            receipts=st.receipts,
            config=st.config,
            http=httpx.AsyncClient(
                base_url=base_url,
                headers={"Authorization": "Bearer sk-test"},
                timeout=httpx.Timeout(None, connect=5.0),
            ),
        )
        try:
            gen_resp = await m._forward_stream(isolated, body_dict, 77, prices, 1_000_000)
            aiter = gen_resp.body_iterator
            first = await aiter.__anext__()  # consume event 1
            assert first.startswith(b"data:")
            await aiter.aclose()  # client walked away mid-stream
        finally:
            await isolated.http.aclose()

    asyncio.run(drive())
    assert fake_chain.settle_calls == []
    r2 = client.get("/receipt/77")
    assert r2.status_code == 404
    assert any("disconnected" in rec.message for rec in caplog.records)
