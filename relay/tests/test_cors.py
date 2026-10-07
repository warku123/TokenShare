"""CORS for the browser front-end (it calls the relay directly):

  - any request with an Origin gets access-control-allow-origin back;
  - a preflight OPTIONS carrying X-Payment-Id / X-Signature in
    Access-Control-Request-Headers is allowed (not short-circuited into the
    GET/POST-only route table as a 405);
  - access-control-expose-headers makes X-Receipt / X-Settle-Status readable
    by browser JS — without it the fetch response hides them.

Origins come from RELAY_CORS_ORIGINS read at import time; the suite never
sets that env var, so the default ["*"] applies (demo default).
"""

from __future__ import annotations

from typing import Any

import relay.app.main as m
from fastapi.testclient import TestClient

from .conftest import chat_body

ORIGIN = "https://tokenshare-demo.vercel.app"


def test_health_echoes_cors_origin(client: TestClient) -> None:
    r = client.get("/health", headers={"Origin": ORIGIN})
    assert r.status_code == 200
    assert r.headers.get("access-control-allow-origin") is not None


def test_preflight_allows_payment_headers(client: TestClient) -> None:
    """OPTIONS preflight for the signed chat route: must be allowed by the
    CORS middleware BEFORE routing (which has no OPTIONS handler) and must
    advertise the relay's custom payment headers."""
    r = client.options(
        "/v1/chat/completions",
        headers={
            "Origin": ORIGIN,
            "Access-Control-Request-Method": "POST",
            "Access-Control-Request-Headers": (
                "authorization, content-type, x-payment-id, x-signature"
            ),
        },
    )
    assert r.status_code == 200
    allow = (r.headers.get("access-control-allow-headers") or "").lower()
    assert "x-payment-id" in allow
    assert "x-signature" in allow
    assert "authorization" in allow
    assert r.headers.get("access-control-allow-origin") is not None


def test_expose_headers_make_receipt_readable_by_js(
    client: TestClient, fake_chain: Any, mock_openai: Any
) -> None:
    """The browser must be able to read X-Receipt / X-Settle-Status — the
    relay stamps them AFTER settlement, so the CORS middleware must expose
    them on the actual (non-preflight) response too. Browsers send Origin on
    every cross-origin fetch, so the request carries one here."""
    from .conftest import signed_headers

    body = chat_body()
    r = client.post(
        "/v1/chat/completions",
        content=body,
        headers={**signed_headers(body), "Origin": ORIGIN},
    )
    assert r.status_code == 200
    expose = (r.headers.get("access-control-expose-headers") or "").lower()
    assert "x-receipt" in expose
    assert "x-settle-status" in expose
    assert r.headers.get("access-control-allow-origin") is not None
    # Baseline behavior intact: the receipt is on the response itself.
    assert "X-Receipt" in r.headers
    assert r.headers["X-Settle-Status"] == "settled"


def test_middleware_registered_on_app() -> None:
    """Guard against silent config drift: the CORS middleware IS registered
    (types in starlette's stack), with the default demo origins ["*"]."""
    from fastapi.middleware.cors import CORSMiddleware

    cors = [
        mw
        for mw in m.app.user_middleware
        if getattr(mw, "cls", None) is CORSMiddleware
    ]
    assert cors, "CORSMiddleware missing from the app middleware stack"
    options = getattr(cors[0], "kwargs", {}) or {}
    assert options.get("allow_origins") == ["*"]
    # M15 R1: DELETE + the custody wallet headers were ADDED for the shared
    # custody endpoints (additive — every legacy entry stays).
    assert options.get("allow_methods") == ["GET", "POST", "DELETE", "OPTIONS"]
    assert "X-Payment-Id" in (options.get("allow_headers") or [])
    assert "X-Tokenshare-Seller" in (options.get("allow_headers") or [])
    assert "X-Tokenshare-Signature" in (options.get("allow_headers") or [])
    assert "X-Receipt" in (options.get("expose_headers") or [])
