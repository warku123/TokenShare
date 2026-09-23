"""Tests for `listings` (A-tier multi-seller comparison) — zero network.

open_chain / registered_operators / get_listing are monkeypatched at the
chain-module level; the Registered-event signature constant is additionally
cross-checked against contracts/src/Registry.sol so event drift fails loudly.
"""

from __future__ import annotations

import json
import pathlib
import re
import types

import pytest
from eth_utils import keccak, to_checksum_address
from typer.testing import CliRunner

from tests.conftest import all_output
from tokenshare_cli import chain as chain_mod
from tokenshare_cli.app import app
from tokenshare_cli.chain import REGISTERED_EVENT_SIG
from tokenshare_cli.config import load_listings_from_block

REPO_ROOT = pathlib.Path(__file__).resolve().parents[2]

SELLER_A = to_checksum_address("0x" + "aa" * 20)
SELLER_B = to_checksum_address("0x" + "bb" * 20)
REGISTRY_ADDR = "0x" + "99" * 20

# Suite-default tiered prices (native USDC per 1M tokens):
#   A: (50_000*200_000 + 100_000*32_000)//1e6 = 13_200  -> est "0.0132 USDC"
#   B: (100_000*200_000 + 200_000*32_000)//1e6 = 26_400 -> est "0.0264 USDC"
PRICE_A = dict(cached=25_000, input=50_000, output=100_000)
PRICE_B = dict(cached=25_000, input=100_000, output=200_000)


def make_listing(operator: str, *, active: bool = True, models=None, prices=None) -> dict:
    p = prices or PRICE_A
    return {
        "operator": operator,
        "endpoint": f"http://127.0.0.1:878{'7' if operator == SELLER_A else '8'}",
        "models": models if models is not None else ["kimi-k2.6"],
        "price_cached_in": p["cached"],
        "price_input": p["input"],
        "price_output": p["output"],
        "active": active,
    }


@pytest.fixture()
def runner():
    return CliRunner()


@pytest.fixture()
def listings_env(monkeypatch):
    """Full PIN env + scrubbed estimate/scan vars (deterministic caps)."""
    monkeypatch.setenv("BUYER_PRIVATE_KEY", "0x" + "11" * 32)
    monkeypatch.setenv("RPC_URL", "http://127.0.0.1:8545")
    monkeypatch.setenv("CHAIN_ID", "10143")
    monkeypatch.setenv("ESCROW_ADDR", "0x" + "aa" * 20)
    monkeypatch.setenv("REGISTRY_ADDR", REGISTRY_ADDR)
    monkeypatch.setenv("USDC_ADDR", "0x" + "cc" * 20)
    for var in ("LISTINGS_FROM_BLOCK", "PROMPT_TOKEN_CAP", "COMPLETION_TOKEN_CAP"):
        monkeypatch.delenv(var, raising=False)


class FakeListingsChain:
    """ChainModule stand-in: canned event order + per-operator listings."""

    def __init__(self, monkeypatch, *, events: list[str], listings: dict):
        self.events = events
        self.listings = listings
        self.registered_calls: list[int] = []
        self.get_listing_calls: list[str] = []
        ctx = self
        monkeypatch.setattr(chain_mod, "open_chain", lambda cfg: ctx)
        monkeypatch.setattr(
            chain_mod,
            "registered_operators",
            lambda c, from_block: ctx._registered(from_block),
        )
        monkeypatch.setattr(chain_mod, "get_listing", lambda c, op: ctx._listing(op))

    def _registered(self, from_block: int) -> list[str]:
        self.registered_calls.append(from_block)
        seen: list[str] = []
        for op in self.events:
            if op not in seen:
                seen.append(op)
        return seen

    def _listing(self, operator: str) -> dict:
        self.get_listing_calls.append(operator)
        return self.listings[operator]


# --------------------------------------------------------------------------
# ABI / signature correspondence with contracts/src/Registry.sol
# --------------------------------------------------------------------------
def test_registered_event_signature_matches_registry_source():
    source = (REPO_ROOT / "contracts" / "src" / "Registry.sol").read_text()
    match = re.search(r"event\s+Registered\(([^)]+)\)", source, re.S)
    assert match, "Registered event not found in Registry.sol"
    param_types = []
    for param in match.group(1).split(","):
        tokens = param.split()
        if tokens[0] == "indexed":  # indexed-ness never enters the signature
            tokens = tokens[1:]
        param_types.append(tokens[0])
    canonical = "Registered(" + ",".join(param_types) + ")"
    assert canonical == REGISTERED_EVENT_SIG


# --------------------------------------------------------------------------
# chain.registered_operators unit tests (fake w3/registry)
# --------------------------------------------------------------------------
def _fake_ctx(raw_logs: list[dict]):
    eth = types.SimpleNamespace(
        get_logs=lambda filter_dict: raw_logs  # logs pre-decoded by fake process_log
    )
    registered_event = types.SimpleNamespace(
        process_log=lambda log: log  # already {"args": {...}} shaped
    )
    registry = types.SimpleNamespace(
        address=REGISTRY_ADDR,
        events=types.SimpleNamespace(Registered=lambda: registered_event),
    )
    return types.SimpleNamespace(
        w3=types.SimpleNamespace(eth=eth), registry=registry,
        cfg=types.SimpleNamespace(rpc_url="http://127.0.0.1:8545"),
    )


def test_registered_operators_wiring_and_dedup(monkeypatch):
    captured: dict = {}

    def fake_get_logs(filter_dict):
        captured.update(filter_dict)
        return [
            {"args": {"operator": SELLER_A}},
            {"args": {"operator": SELLER_B}},
            {"args": {"operator": SELLER_A}},  # re-register -> dedup
        ]

    ctx = _fake_ctx([])
    ctx.w3.eth.get_logs = fake_get_logs

    operators = chain_mod.registered_operators(ctx, 42)

    assert operators == [SELLER_A, SELLER_B]  # event order, duplicates removed
    assert captured == {
        "fromBlock": 42,
        "toBlock": "latest",
        "address": REGISTRY_ADDR,
        "topics": ["0x" + keccak(text=REGISTERED_EVENT_SIG).hex()],
    }


def test_registered_operators_wraps_rpc_failure():
    def boom(filter_dict):
        raise ValueError("node says no")

    ctx = _fake_ctx([])
    ctx.w3.eth.get_logs = boom

    with pytest.raises(chain_mod.TokenshareError) as exc:
        chain_mod.registered_operators(ctx, 0)
    assert "eth_getLogs" in str(exc.value)


# --------------------------------------------------------------------------
# config: LISTINGS_FROM_BLOCK
# --------------------------------------------------------------------------
def test_load_listings_from_block_default_and_env(monkeypatch):
    monkeypatch.delenv("LISTINGS_FROM_BLOCK", raising=False)
    assert load_listings_from_block() == 0
    monkeypatch.setenv("LISTINGS_FROM_BLOCK", "123")
    assert load_listings_from_block() == 123


def test_load_listings_from_block_rejects_garbage(monkeypatch):
    monkeypatch.setenv("LISTINGS_FROM_BLOCK", "abc")
    with pytest.raises(Exception):
        load_listings_from_block()


# --------------------------------------------------------------------------
# command: table output
# --------------------------------------------------------------------------
def test_listings_table_sorted_cheapest_first(runner, listings_env, monkeypatch):
    ctx = FakeListingsChain(
        monkeypatch,
        events=[SELLER_B, SELLER_A],  # B discovered first, A is cheaper
        listings={SELLER_A: make_listing(SELLER_A), SELLER_B: make_listing(SELLER_B, prices=PRICE_B)},
    )

    result = runner.invoke(app, ["listings"])
    out = all_output(result)
    assert result.exit_code == 0, out

    assert "Active listings (2/2 sellers, chainId 10143" in out
    assert "from block 0" in out
    # sorted cheapest first: A before B
    assert out.index(_short(SELLER_A)) < out.index(_short(SELLER_B))
    # humanized tiered prices + estimates (units module formatting)
    assert "25000 native (= 0.025 USDC)" in out
    assert "13200 native (= 0.0132 USDC)" in out
    assert "26400 native (= 0.0264 USDC)" in out
    # estimate assumption note with the actual caps used
    assert "(priceInput*200000 + priceOutput*32000)//1e6" in out
    # full copyable addresses block
    assert "Full seller addresses (for --seller):" in out
    assert SELLER_A in out and SELLER_B in out


def _short(address: str) -> str:
    return f"{address[:6]}…{address[-4:]}"


def test_listings_reregister_calls_getlisting_once_per_operator(
    runner, listings_env, monkeypatch
):
    ctx = FakeListingsChain(
        monkeypatch,
        events=[SELLER_A, SELLER_B, SELLER_A, SELLER_A],  # 3 re-registers
        listings={SELLER_A: make_listing(SELLER_A), SELLER_B: make_listing(SELLER_B)},
    )

    result = runner.invoke(app, ["listings"])
    out = all_output(result)
    assert result.exit_code == 0, out
    assert ctx.get_listing_calls == [SELLER_A, SELLER_B]


def test_listings_inactive_filtered(runner, listings_env, monkeypatch):
    FakeListingsChain(
        monkeypatch,
        events=[SELLER_A, SELLER_B],
        listings={SELLER_A: make_listing(SELLER_A, active=False), SELLER_B: make_listing(SELLER_B)},
    )

    result = runner.invoke(app, ["listings"])
    out = all_output(result)
    assert result.exit_code == 0, out
    assert "Active listings (1/2 sellers" in out
    assert _short(SELLER_B) in out
    assert _short(SELLER_A) not in out  # inactive seller never displayed


def test_listings_all_inactive_message(runner, listings_env, monkeypatch):
    FakeListingsChain(
        monkeypatch,
        events=[SELLER_A],
        listings={SELLER_A: make_listing(SELLER_A, active=False)},
    )

    result = runner.invoke(app, ["listings"])
    out = all_output(result)
    assert result.exit_code == 0, out
    assert "1 registered operator(s), none ACTIVE" in out


def test_listings_empty_events_message(runner, listings_env, monkeypatch):
    FakeListingsChain(monkeypatch, events=[], listings={})

    result = runner.invoke(app, ["listings"])
    out = all_output(result)
    assert result.exit_code == 0, out
    assert "no Registered events found from block 0" in out
    assert "LISTINGS_FROM_BLOCK" in out


def test_listings_models_join_and_blank_filter(runner, listings_env, monkeypatch):
    FakeListingsChain(
        monkeypatch,
        events=[SELLER_A],
        listings={
            SELLER_A: make_listing(SELLER_A, models=["kimi-k2.6", "", "kimi-k2.7-code"])
        },
    )

    result = runner.invoke(app, ["listings"])
    out = all_output(result)
    assert result.exit_code == 0, out
    assert "kimi-k2.6,kimi-k2.7-code" in out
    assert ",," not in out  # blank entries never join


def test_listings_from_block_env_passthrough(runner, listings_env, monkeypatch):
    monkeypatch.setenv("LISTINGS_FROM_BLOCK", "777")
    ctx = FakeListingsChain(
        monkeypatch, events=[SELLER_A], listings={SELLER_A: make_listing(SELLER_A)}
    )

    result = runner.invoke(app, ["listings"])
    out = all_output(result)
    assert result.exit_code == 0, out
    assert ctx.registered_calls == [777]
    assert "from block 777" in out


def test_listings_env_override_caps_changes_estimate(runner, listings_env, monkeypatch):
    monkeypatch.setenv("PROMPT_TOKEN_CAP", "100000")
    monkeypatch.setenv("COMPLETION_TOKEN_CAP", "0")
    FakeListingsChain(
        monkeypatch, events=[SELLER_A], listings={SELLER_A: make_listing(SELLER_A)}
    )

    result = runner.invoke(app, ["listings"])
    out = all_output(result)
    assert result.exit_code == 0, out
    # (50_000*100_000 + 100_000*0)//1e6 = 5_000
    assert "5000 native (= 0.005 USDC)" in out


def test_listings_getlisting_failure_is_clean_error(runner, listings_env, monkeypatch):
    FakeListingsChain(monkeypatch, events=[SELLER_A], listings={})

    def boom(c, op):
        raise ValueError("rpc dropped")

    monkeypatch.setattr(chain_mod, "get_listing", boom)

    result = runner.invoke(app, ["listings"])
    out = all_output(result)
    assert result.exit_code == 2
    assert "getListing" in out and "rpc dropped" in out


def test_listings_command_guards_duplicate_operators(runner, listings_env, monkeypatch):
    """Command-level dedup guard: even a discovery layer returning raw
    duplicate events renders each seller once (defense in depth)."""
    ctx = types.SimpleNamespace()
    monkeypatch.setattr(chain_mod, "open_chain", lambda cfg: ctx)
    monkeypatch.setattr(
        chain_mod, "registered_operators", lambda c, fb: [SELLER_A, SELLER_A, SELLER_B]
    )
    monkeypatch.setattr(
        chain_mod, "get_listing", lambda c, op: make_listing(op)
    )

    result = runner.invoke(app, ["listings"])
    out = all_output(result)
    assert result.exit_code == 0, out
    assert "Active listings (2/2 sellers" in out
    assert out.count(_short(SELLER_A)) == 1


# --------------------------------------------------------------------------
# command: --json
# --------------------------------------------------------------------------
def test_listings_json_schema(runner, listings_env, monkeypatch):
    FakeListingsChain(
        monkeypatch,
        events=[SELLER_B, SELLER_A],
        listings={SELLER_A: make_listing(SELLER_A), SELLER_B: make_listing(SELLER_B, prices=PRICE_B)},
    )

    result = runner.invoke(app, ["listings", "--json"])
    out = all_output(result)
    assert result.exit_code == 0, out

    payload = json.loads(out)
    assert set(payload) == {
        "chainId", "registry", "fromBlock", "activeOnly", "count", "listings"
    }
    assert payload["chainId"] == 10143
    assert payload["registry"] == REGISTRY_ADDR
    assert payload["fromBlock"] == 0
    assert payload["activeOnly"] is True
    assert payload["count"] == 2
    first = payload["listings"][0]
    assert set(first) == {
        "operator", "endpoint", "models",
        "priceCachedIn", "priceInput", "priceOutput", "estimatedCallCost",
    }
    assert first["operator"] == SELLER_A  # cheapest first
    assert first["models"] == ["kimi-k2.6"]
    assert first["priceInput"] == 50_000
    assert first["estimatedCallCost"] == 13_200  # (50k*200k + 100k*32k)//1e6
    assert payload["listings"][1]["estimatedCallCost"] == 26_400


def test_listings_json_empty(runner, listings_env, monkeypatch):
    FakeListingsChain(monkeypatch, events=[], listings={})

    result = runner.invoke(app, ["listings", "--json"])
    payload = json.loads(all_output(result))
    assert result.exit_code == 0
    assert payload["count"] == 0
    assert payload["listings"] == []


def test_listings_bad_from_block_env_exits_two(runner, listings_env, monkeypatch):
    monkeypatch.setenv("LISTINGS_FROM_BLOCK", "not-a-number")
    FakeListingsChain(monkeypatch, events=[SELLER_A], listings={SELLER_A: make_listing(SELLER_A)})

    result = runner.invoke(app, ["listings"])
    out = all_output(result)
    assert result.exit_code == 2
    assert "LISTINGS_FROM_BLOCK" in out


# --------------------------------------------------------------------------
# discoverability
# --------------------------------------------------------------------------
def test_help_lists_listings(runner):
    result = runner.invoke(app, ["--help"])
    out = all_output(result)
    assert result.exit_code == 0
    assert "listings" in out
