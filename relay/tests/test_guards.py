"""409 TTL-margin boundary and 400 listing/model guards."""

from __future__ import annotations

from typing import Any

from .conftest import chat_body, post_chat


def test_409_below_margin_rejected(client: Any, fake_chain: Any) -> None:
    """expiresAt - now == margin - 1 → 409 (settle window too tight)."""
    fake_chain.ttl_delta = 119  # FORWARD_MARGIN_S=120 → 119 < 120
    body = chat_body()
    r = post_chat(client, body)
    assert r.status_code == 409
    assert "forward margin" in r.json()["detail"]


def test_409_at_margin_passes(client: Any, fake_chain: Any, mock_openai: Any) -> None:
    """expiresAt - now == margin exactly → not rejected (strictly <)."""
    fake_chain.ttl_delta = 120
    r = post_chat(client, chat_body())
    assert r.status_code == 200
    assert r.headers["X-Settle-Status"] == "settled"


# ------------------------------------------------------------------- 400 (x4)


def test_400_listing_inactive(client: Any, fake_chain: Any) -> None:
    fake_chain.listing_active = False
    r = post_chat(client, chat_body())
    assert r.status_code == 400
    assert "inactive" in r.json()["detail"]


def test_400_seller_never_registered(client: Any, fake_chain: Any) -> None:
    fake_chain.listing_registered = False
    r = post_chat(client, chat_body())
    assert r.status_code == 400
    assert "unregistered" in r.json()["detail"]


def test_400_model_not_in_listing(client: Any, fake_chain: Any) -> None:
    r = post_chat(client, chat_body(model="gpt-9-turbo"))
    assert r.status_code == 400
    assert "model not in listing.models" in r.json()["detail"]


def test_400_empty_strings_filtered_but_real_model_matches(
    client: Any, fake_chain: Any
) -> None:
    """M1/M2 deferred item: models contains an empty string; filtering keeps
    the real model usable while blank entries never match."""
    fake_chain.models = ["gpt-4o-mini", "", "  "]
    r = post_chat(client, chat_body(model="gpt-4o-mini"))
    assert r.status_code == 200

    blank = post_chat(client, chat_body(model=""))
    assert blank.status_code == 400

    blank_ws = post_chat(client, chat_body(model="  "))
    assert blank_ws.status_code == 400
