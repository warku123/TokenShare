"""Concurrency smoke: ≥2 simultaneous requests must all complete without the
event loop blocking on chain reads (fix #1 — chain reads off the loop)."""

from __future__ import annotations

import asyncio
import json
from typing import Any

import httpx
from fastapi.testclient import TestClient  # noqa: F401  (ensures env import)

import relay.app.main as m

from .conftest import BUYER_KEY, setup_relay_env, signed_headers, chat_body


def test_concurrent_requests_settle_independently(
    monkeypatch: Any, fake_chain: Any, mock_openai: Any
) -> None:
    setup_relay_env(monkeypatch, f"http://127.0.0.1:{mock_openai.server_port}/v1")

    async def run() -> list[tuple[int, str]]:
        async with m.lifespan(m.app):
            transport = httpx.ASGITransport(app=m.app)
            async with httpx.AsyncClient(
                transport=transport, base_url="http://relay.test"
            ) as ac:
                from eth_account import Account
                from eth_account.messages import encode_defunct

                async def one(payment_id: str) -> tuple[int, str]:
                    raw = chat_body().replace(b'"hi"', f'"q{payment_id}"'.encode())
                    msg = (f"POST|/v1/chat/completions|"
                           f"{__import__('hashlib').sha256(raw).hexdigest()}|{payment_id}")
                    sig = Account.from_key(BUYER_KEY).sign_message(
                        encode_defunct(text=msg)
                    ).signature.hex()
                    r = await ac.post(
                        "/v1/chat/completions",
                        content=raw,
                        headers={"X-Payment-Id": payment_id, "X-Signature": sig,
                                 "Content-Type": "application/json"},
                    )
                    return r.status_code, r.headers.get("X-Settle-Status", "")

                results = await asyncio.gather(one("61"), one("62"), one("63"))
                return results

    status_codes = asyncio.run(run())
    assert status_codes == [(200, "settled")] * 3
    settled_ids = sorted(pid for pid, _ in fake_chain.settle_calls)
    assert settled_ids == [61, 62, 63]
