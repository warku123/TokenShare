"""Tests for `listings` (Registry v3 on-chain enumeration discovery, M10).

open_chain / get_sellers / get_listing are monkeypatched at the chain-module
level; the sellerCount/getSellers ABI entries are cross-checked against the
M10 ABI PIN (.slim/deepwork/m3-m5-e2e.md 「M10」 — the authoritative spec)
and, once the v3 source lands, against contracts/src/Registry.sol — so
drift fails loudly.
"""

from __future__ import annotations

import json
import pathlib
import re
import types

import pytest
from eth_utils import to_checksum_address
from typer.testing import CliRunner

from tests.conftest import all_output
from tokenshare_cli import config as config_mod
from tokenshare_cli import chain as chain_mod
from tokenshare_cli.app import app

REPO_ROOT = pathlib.Path(__file__).resolve().parents[2]
PIN_FILE = REPO_ROOT / ".slim" / "deepwork" / "m3-m5-e2e.md"

SELLER_A = to_checksum_address("0x" + "aa" * 20)
SELLER_B = to_checksum_address("0x" + "bb" * 20)
SELLER_C = to_checksum_address("0x" + "cc" * 20)
REGISTRY_ADDR = "0x" + "99" * 20

# Per-model tiered prices (native USDC per 1M tokens) — M9 Registry v2:
#   A: (50_000*200_000 + 100_000*32_000)//1e6 = 13_200  -> est "0.0132 USDC"
#   B: (100_000*200_000 + 200_000*32_000)//1e6 = 26_400 -> est "0.0264 USDC"
#   cheap: (10_000*200_000 + 20_000*32_000)//1e6 = 2_640 -> est "0.00264 USDC"
PRICE_A = {"cached_in": 25_000, "input": 50_000, "output": 100_000}
PRICE_B = {"cached_in": 25_000, "input": 100_000, "output": 200_000}
PRICE_CHEAP = {"cached_in": 0, "input": 10_000, "output": 20_000}


def make_listing(operator: str, *, active: bool = True, models=None, prices=None) -> dict:
    """Registry v2 listing: models + PARALLEL per-model prices array."""
    if models is None:
        models = ["kimi-k2.6"]
    if prices is None:
        prices = [dict(PRICE_A) for _ in models]
    return {
        "operator": operator,
        "endpoint": f"http://127.0.0.1:878{'7' if operator == SELLER_A else '8'}",
        "models": list(models),
        "prices": [dict(p) for p in prices],
        "active": active,
    }


@pytest.fixture()
def runner():
    return CliRunner()


@pytest.fixture()
def listings_env(monkeypatch):
    """Full PIN env + scrubbed estimate vars (deterministic caps)."""
    monkeypatch.setenv("BUYER_PRIVATE_KEY", "0x" + "11" * 32)
    monkeypatch.setenv("RPC_URL", "http://127.0.0.1:8545")
    monkeypatch.setenv("CHAIN_ID", "10143")
    monkeypatch.setenv("ESCROW_ADDR", "0x" + "aa" * 20)
    monkeypatch.setenv("REGISTRY_ADDR", REGISTRY_ADDR)
    monkeypatch.setenv("USDC_ADDR", "0x" + "cc" * 20)
    monkeypatch.delenv("PROMPT_TOKEN_CAP", raising=False)
    monkeypatch.delenv("COMPLETION_TOKEN_CAP", raising=False)


class FakeListingsChain:
    """ChainModule stand-in: canned enumeration order + per-operator listings."""

    def __init__(self, monkeypatch, *, enumerated: list[str], listings: dict):
        self.enumerated = enumerated
        self.listings = listings
        self.w3 = types.SimpleNamespace()  # app passes ctx.w3 to get_sellers
        self.get_sellers_calls: list[tuple[str, int]] = []
        self.get_listing_calls: list[str] = []
        ctx = self
        monkeypatch.setattr(chain_mod, "open_chain", lambda cfg: ctx)
        monkeypatch.setattr(
            chain_mod,
            "get_sellers",
            lambda w3, addr, page=100: ctx._sellers(w3, addr, page),
        )
        monkeypatch.setattr(chain_mod, "get_listing", lambda c, op: ctx._listing(op))

    def _sellers(self, w3, addr, page=100) -> list[str]:
        self.get_sellers_calls.append((str(addr), page))
        return list(self.enumerated)

    def _listing(self, operator: str) -> dict:
        self.get_listing_calls.append(operator)
        return self.listings[operator]


# --------------------------------------------------------------------------
# ABI / signature correspondence with the M10 ABI PIN (authoritative) and the
# v3 Registry.sol source once fix-23 lands it
# --------------------------------------------------------------------------
def _canonical_sig(name: str, params: str) -> str:
    param_types = []
    for param in params.split(","):
        param = param.strip()
        if not param:
            continue
        tokens = param.split()
        if tokens[0] == "indexed":  # indexed-ness never enters the signature
            tokens = tokens[1:]
        param_types.append(tokens[0])
    return f"{name}(" + ",".join(param_types) + ")"


def test_registry_abi_has_v3_enumeration():
    """REGISTRY_ABI carries the M10 enumeration surface + the full M9 v2
    surface (v3 keeps the v2 face 100% — only additions, no changes), plus
    the M12 v4 additions (removeModel / ModelRemoved)."""
    from tokenshare_cli.abis import REGISTRY_ABI

    by_name = {(e["type"], e["name"]): e for e in REGISTRY_ABI}
    assert {
        ("function", "sellerCount"),
        ("function", "getSellers"),
        ("function", "register"),
        ("function", "updateModelPrice"),
        ("function", "deactivate"),
        ("function", "getListing"),
        ("function", "getPrice"),
        ("function", "removeModel"),
        ("event", "PriceUpdated"),
        ("event", "Deactivated"),
        ("event", "ModelRemoved"),
    } <= set(by_name)

    seller_count = by_name[("function", "sellerCount")]
    assert seller_count["inputs"] == []
    assert [o["type"] for o in seller_count["outputs"]] == ["uint256"]
    assert seller_count["stateMutability"] == "view"

    get_sellers = by_name[("function", "getSellers")]
    assert [i["type"] for i in get_sellers["inputs"]] == ["uint256", "uint256"]
    assert [o["type"] for o in get_sellers["outputs"]] == ["address[]"]
    assert get_sellers["stateMutability"] == "view"

    # M12 v4 shape: removeModel(string) nonpayable; ModelRemoved(address
    # indexed operator, string model).
    remove_model_fn = by_name[("function", "removeModel")]
    assert [i["type"] for i in remove_model_fn["inputs"]] == ["string"]
    assert remove_model_fn["outputs"] == []
    assert remove_model_fn["stateMutability"] == "nonpayable"

    model_removed = by_name[("event", "ModelRemoved")]
    assert model_removed["anonymous"] is False
    assert [
        (i["type"], i["indexed"]) for i in model_removed["inputs"]
    ] == [("address", True), ("string", False)]


def test_enumeration_signatures_match_m10_pin():
    """The M10 ABI PIN (.slim/deepwork 「M10」) is the authoritative spec
    while contracts/src/Registry.sol may still be v2 (fix-23 lane lands v3
    in parallel)."""
    pin_text = PIN_FILE.read_text()
    match_count = re.search(r"function\s+sellerCount\(\)\s+external\s+view", pin_text)
    assert match_count, "sellerCount() not found in the M10 ABI PIN"

    match_page = re.search(
        r"function\s+getSellers\(([^)]*)\)\s+external\s+view", pin_text
    )
    assert match_page, "getSellers(...) not found in the M10 ABI PIN"
    canonical = _canonical_sig("getSellers", match_page.group(1))

    from tokenshare_cli.abis import REGISTRY_ABI

    by_name = {(e["type"], e["name"]): e for e in REGISTRY_ABI}
    get_sellers = by_name[("function", "getSellers")]
    on_chain = "getSellers(" + ",".join(i["type"] for i in get_sellers["inputs"]) + ")"
    assert canonical == on_chain == "getSellers(uint256,uint256)"


def test_enumeration_signatures_match_v3_source_once_landed():
    """Auto-tightens when fix-23 lands: if Registry.sol contains the v3
    enumeration surface, its signatures must match ours verbatim."""
    source = (REPO_ROOT / "contracts" / "src" / "Registry.sol").read_text()
    if "sellerCount" not in source:
        pytest.skip(
            "contracts/src/Registry.sol is still v2 — fix-23 has not landed "
            "the v3 enumeration source yet"
        )
    match_page = re.search(
        r"function\s+getSellers\(([^)]*)\)\s+external\s+view", source, re.S
    )
    assert match_page, "getSellers(...) not found in Registry.sol"
    canonical = _canonical_sig("getSellers", match_page.group(1))
    assert canonical == "getSellers(uint256,uint256)"


def test_remove_model_signature_matches_m12_pin():
    """The M12 ABI PIN (.slim/deepwork 「M12」) is authoritative while the
    contracts lane lands Registry v4 in parallel: removeModel(string) and
    ModelRemoved(address indexed, string) must match it exactly."""
    pin_text = PIN_FILE.read_text()
    match_fn = re.search(r"function\s+removeModel\(([^)]*)\)\s+external", pin_text)
    assert match_fn, "removeModel(...) not found in the M12 ABI PIN"
    canonical = _canonical_sig("removeModel", match_fn.group(1))

    from tokenshare_cli.abis import REGISTRY_ABI

    by_name = {(e["type"], e["name"]): e for e in REGISTRY_ABI}
    remove_model_fn = by_name[("function", "removeModel")]
    on_chain = (
        "removeModel(" + ",".join(i["type"] for i in remove_model_fn["inputs"]) + ")"
    )
    assert canonical == on_chain == "removeModel(string)"

    match_ev = re.search(r"ModelRemoved\(([^)]*)\)", pin_text)
    assert match_ev, "event ModelRemoved(...) not found in the M12 ABI PIN"
    ev_canonical = _canonical_sig("ModelRemoved", match_ev.group(1))
    model_removed = by_name[("event", "ModelRemoved")]
    ev_on_chain = (
        "ModelRemoved(" + ",".join(i["type"] for i in model_removed["inputs"]) + ")"
    )
    assert ev_canonical == ev_on_chain == "ModelRemoved(address,string)"


def test_remove_model_signature_matches_v4_source_once_landed():
    """Auto-tightens when the contracts lane lands Registry v4: if
    Registry.sol contains removeModel, its signature must match ours."""
    source = (REPO_ROOT / "contracts" / "src" / "Registry.sol").read_text()
    if "removeModel" not in source:
        pytest.skip(
            "contracts/src/Registry.sol has no removeModel yet — the v4 "
            "contracts lane has not landed"
        )
    match_fn = re.search(r"function\s+removeModel\(([^)]*)\)\s+external", source, re.S)
    assert match_fn, "removeModel(...) not found in Registry.sol"
    canonical = _canonical_sig("removeModel", match_fn.group(1))
    assert canonical == "removeModel(string)"


# --------------------------------------------------------------------------
# chain.get_sellers unit tests (fake w3/registry with clamp semantics)
# --------------------------------------------------------------------------
class FakeRegistryV3:
    """sellerCount/getSellers over an append-only list.

    Clamp semantics per the M10 PIN: start >= len -> []; start+count > len
    -> truncated to the end. `lie_total` simulates a sellerCount bigger than
    what getSellers serves (empty batches); `dup_pages` simulates a
    misbehaving node echoing the first batch element again.
    """

    def __init__(self, sellers, *, lie_total=None, dup_pages=False):
        self.sellers = list(sellers)
        self.lie_total = lie_total
        self.dup_pages = dup_pages
        self.count_calls = 0
        self.page_calls: list[tuple[int, int]] = []

        registry = self

        def sellerCount():
            registry.count_calls += 1
            return types.SimpleNamespace(call=lambda: registry._total())

        def getSellers(start, count):
            registry.page_calls.append((int(start), int(count)))
            batch = list(registry.sellers[int(start) : int(start) + int(count)])
            if registry.dup_pages and batch:
                batch = batch + batch[:1]
            return types.SimpleNamespace(call=lambda: batch)

        self.functions = types.SimpleNamespace(
            sellerCount=sellerCount, getSellers=getSellers
        )

    def _total(self) -> int:
        return len(self.sellers) if self.lie_total is None else self.lie_total


def _fake_w3(registry: FakeRegistryV3):
    return types.SimpleNamespace(
        eth=types.SimpleNamespace(contract=lambda address, abi: registry)
    )


def test_get_sellers_paginates_full_set():
    sellers = [to_checksum_address("0x" + f"{i:02x}" * 20) for i in range(250)]
    registry = FakeRegistryV3(sellers)

    result = chain_mod.get_sellers(_fake_w3(registry), REGISTRY_ADDR)

    assert result == sellers  # on-chain order preserved
    assert registry.count_calls == 1
    assert registry.page_calls == [(0, 100), (100, 100), (200, 100)]


def test_get_sellers_zero_count_short_circuits():
    registry = FakeRegistryV3([])

    assert chain_mod.get_sellers(_fake_w3(registry), REGISTRY_ADDR) == []
    assert registry.count_calls == 1
    assert registry.page_calls == []  # getSellers never called


def test_get_sellers_handles_clamped_tail():
    """start+count > len -> the contract truncates; the loop must advance by
    the RETURNED batch length and still collect every seller exactly once."""
    sellers = [SELLER_A, SELLER_B, SELLER_C]
    registry = FakeRegistryV3(sellers)

    result = chain_mod.get_sellers(_fake_w3(registry), REGISTRY_ADDR, page=2)

    assert result == sellers
    assert registry.page_calls == [(0, 2), (2, 2)]  # (2,2) clamps to 1 item


def test_get_sellers_empty_batch_breaks():
    """sellerCount lies (total > 0) but getSellers serves nothing — the
    helper must return promptly instead of looping forever."""
    registry = FakeRegistryV3([], lie_total=10)

    assert chain_mod.get_sellers(_fake_w3(registry), REGISTRY_ADDR) == []
    assert registry.page_calls == [(0, 100)]  # one attempt, then break


def test_get_sellers_dedups_preserving_order():
    registry = FakeRegistryV3([SELLER_A, SELLER_B, SELLER_C], dup_pages=True)

    result = chain_mod.get_sellers(_fake_w3(registry), REGISTRY_ADDR)

    assert result == [SELLER_A, SELLER_B, SELLER_C]  # echoes removed


def test_get_sellers_clamps_page_to_500():
    sellers = [SELLER_A, SELLER_B]
    registry = FakeRegistryV3(sellers)

    result = chain_mod.get_sellers(_fake_w3(registry), REGISTRY_ADDR, page=1000)

    assert result == sellers
    assert registry.page_calls == [(0, 500)]  # contract cap applied client-side


def test_get_sellers_rejects_nonpositive_page():
    registry = FakeRegistryV3([SELLER_A])
    with pytest.raises(chain_mod.TokenshareError, match="page"):
        chain_mod.get_sellers(_fake_w3(registry), REGISTRY_ADDR, page=0)
    assert registry.count_calls == 0


def test_get_sellers_wraps_rpc_failure():
    registry = FakeRegistryV3([SELLER_A])

    def boom_count():
        raise ValueError("node says no")

    registry.functions = types.SimpleNamespace(
        sellerCount=lambda: types.SimpleNamespace(call=boom_count)
    )
    with pytest.raises(chain_mod.TokenshareError, match="sellerCount"):
        chain_mod.get_sellers(_fake_w3(registry), REGISTRY_ADDR)

    registry2 = FakeRegistryV3([SELLER_A])

    def boom_page(start, count):
        raise ValueError("node says no")

    registry2.functions = types.SimpleNamespace(
        sellerCount=lambda: types.SimpleNamespace(call=lambda: 1),
        getSellers=lambda start, count: types.SimpleNamespace(call=boom_page(start, count)),
    )
    with pytest.raises(chain_mod.TokenshareError, match="getSellers"):
        chain_mod.get_sellers(_fake_w3(registry2), REGISTRY_ADDR)


# --------------------------------------------------------------------------
# chain.get_listing / get_price decode (fake registry)
# --------------------------------------------------------------------------
def test_get_listing_decodes_v2_parallel_prices():
    prices = [(25_000, 50_000, 100_000), (1, 2, 3)]
    models = ["kimi-k2.6", "k3"]
    registry = types.SimpleNamespace(
        functions=types.SimpleNamespace(
            getListing=lambda seller: types.SimpleNamespace(
                call=lambda: (SELLER_A, "http://ep", models, prices, True)
            )
        )
    )
    ctx = types.SimpleNamespace(registry=registry)

    listing = chain_mod.get_listing(ctx, SELLER_A)

    assert listing == {
        "operator": SELLER_A,
        "endpoint": "http://ep",
        "models": ["kimi-k2.6", "k3"],
        "prices": [
            {"cached_in": 25_000, "input": 50_000, "output": 100_000},
            {"cached_in": 1, "input": 2, "output": 3},
        ],
        "active": True,
    }


def test_get_price_decodes_three_tiers():
    registry = types.SimpleNamespace(
        functions=types.SimpleNamespace(
            getPrice=lambda seller, model: types.SimpleNamespace(
                call=lambda: (7, 8, 9)
            )
        )
    )
    ctx = types.SimpleNamespace(registry=registry)

    assert chain_mod.get_price(ctx, SELLER_A, "k3") == {
        "cached_in": 7, "input": 8, "output": 9,
    }


# --------------------------------------------------------------------------
# config: LISTINGS_FROM_BLOCK removed (M10 breaking)
# --------------------------------------------------------------------------
def test_listings_from_block_env_removed():
    """Breaking-change guard: the scan-start knob and its loader are gone."""
    assert not hasattr(config_mod, "LISTINGS_FROM_BLOCK_VAR")
    assert not hasattr(config_mod, "load_listings_from_block")


def test_stale_listings_from_block_env_is_ignored(runner, listings_env, monkeypatch):
    """The removed env must not break (or alter) the enumeration path."""
    monkeypatch.setenv("LISTINGS_FROM_BLOCK", "not-a-number-anymore")
    FakeListingsChain(
        monkeypatch, enumerated=[SELLER_A], listings={SELLER_A: make_listing(SELLER_A)}
    )

    result = runner.invoke(app, ["listings"])
    out = all_output(result)
    assert result.exit_code == 0, out
    assert "Active listings (1 model price rows / 1 seller(s), chainId 10143" in out


# --------------------------------------------------------------------------
# command: table output (per-model rows)
# --------------------------------------------------------------------------
def test_listings_table_sorted_cheapest_first(runner, listings_env, monkeypatch):
    ctx = FakeListingsChain(
        monkeypatch,
        enumerated=[SELLER_B, SELLER_A],  # B enumerated first, A is cheaper
        listings={SELLER_A: make_listing(SELLER_A), SELLER_B: make_listing(SELLER_B, prices=[PRICE_B])},
    )

    result = runner.invoke(app, ["listings"])
    out = all_output(result)
    assert result.exit_code == 0, out

    assert "Active listings (2 model price rows / 2 seller(s), chainId 10143" in out
    assert "sorted by estimated per-call cost" in out
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
    # discovery went through the v3 enumeration helper at the configured registry
    assert ctx.get_sellers_calls == [(REGISTRY_ADDR, 100)]


def _short(address: str) -> str:
    return f"{address[:6]}…{address[-4:]}"


def test_listings_one_row_per_model(runner, listings_env, monkeypatch):
    """M9: each (operator, model) pair gets its own priced row."""
    FakeListingsChain(
        monkeypatch,
        enumerated=[SELLER_A],
        listings={
            SELLER_A: make_listing(
                SELLER_A,
                models=["kimi-k2.6", "k3"],
                prices=[PRICE_A, PRICE_CHEAP],
            )
        },
    )

    result = runner.invoke(app, ["listings"])
    out = all_output(result)
    assert result.exit_code == 0, out
    assert "Active listings (2 model price rows / 1 seller(s)" in out
    assert "kimi-k2.6" in out and "k3" in out
    # cheapest model first within the seller (PRICE_CHEAP est 2640 < PRICE_A est 13200)
    assert out.index("k3") < out.index("kimi-k2.6")
    assert "2640 native (= 0.00264 USDC)" in out


def test_listings_blank_models_skipped(runner, listings_env, monkeypatch):
    """M9: blank model entries (relay-side filter rule) never become rows."""
    FakeListingsChain(
        monkeypatch,
        enumerated=[SELLER_A],
        listings={
            SELLER_A: make_listing(
                SELLER_A,
                models=["kimi-k2.6", "", "kimi-k2.7-code"],
                prices=[PRICE_A, PRICE_A, PRICE_A],
            )
        },
    )

    result = runner.invoke(app, ["listings"])
    out = all_output(result)
    assert result.exit_code == 0, out
    assert "2 model price rows" in out
    assert "kimi-k2.6" in out and "kimi-k2.7-code" in out
    assert ",," not in out  # blank entries never join


def test_listings_model_filter(runner, listings_env, monkeypatch):
    """M9: --model restricts the comparison to that model across sellers."""
    FakeListingsChain(
        monkeypatch,
        enumerated=[SELLER_A, SELLER_B],
        listings={
            SELLER_A: make_listing(SELLER_A, models=["kimi-k2.6", "k3"], prices=[PRICE_A, PRICE_CHEAP]),
            SELLER_B: make_listing(SELLER_B, models=["k3"], prices=[PRICE_B]),
        },
    )

    result = runner.invoke(app, ["listings", "--model", "k3"])
    out = all_output(result)
    assert result.exit_code == 0, out
    assert "model 'k3'" in out
    assert "2 model price rows" in out
    assert "kimi-k2.6" not in out  # filtered out
    # B's k3 (26400) is pricier than A's k3 (2640) -> A's row first
    assert out.index(_short(SELLER_A)) < out.index(_short(SELLER_B))
    assert "26400 native (= 0.0264 USDC)" in out


def test_listings_model_filter_no_match_message(runner, listings_env, monkeypatch):
    FakeListingsChain(
        monkeypatch,
        enumerated=[SELLER_A],
        listings={SELLER_A: make_listing(SELLER_A, models=["kimi-k2.6"])},
    )

    result = runner.invoke(app, ["listings", "--model", "gpt-99"])
    out = all_output(result)
    assert result.exit_code == 0, out
    assert "no ACTIVE listing serves model 'gpt-99'" in out
    assert "1 registered operator(s) scanned" in out


def test_listings_duplicate_enumeration_calls_getlisting_once_per_operator(
    runner, listings_env, monkeypatch
):
    """Command-level dedup guard: even a discovery layer returning raw
    duplicates renders each seller once (defense in depth)."""
    ctx = FakeListingsChain(
        monkeypatch,
        enumerated=[SELLER_A, SELLER_B, SELLER_A, SELLER_A],
        listings={SELLER_A: make_listing(SELLER_A), SELLER_B: make_listing(SELLER_B)},
    )

    result = runner.invoke(app, ["listings"])
    out = all_output(result)
    assert result.exit_code == 0, out
    assert ctx.get_listing_calls == [SELLER_A, SELLER_B]
    assert "Active listings (2 model price rows / 2 seller(s)" in out


def test_listings_inactive_filtered(runner, listings_env, monkeypatch):
    FakeListingsChain(
        monkeypatch,
        enumerated=[SELLER_A, SELLER_B],
        listings={SELLER_A: make_listing(SELLER_A, active=False), SELLER_B: make_listing(SELLER_B)},
    )

    result = runner.invoke(app, ["listings"])
    out = all_output(result)
    assert result.exit_code == 0, out
    assert "Active listings (1 model price rows / 2 seller(s)" in out
    assert _short(SELLER_B) in out
    assert _short(SELLER_A) not in out  # inactive seller never displayed


def test_listings_all_inactive_message(runner, listings_env, monkeypatch):
    FakeListingsChain(
        monkeypatch,
        enumerated=[SELLER_A],
        listings={SELLER_A: make_listing(SELLER_A, active=False)},
    )

    result = runner.invoke(app, ["listings"])
    out = all_output(result)
    assert result.exit_code == 0, out
    assert "1 registered operator(s), none ACTIVE" in out


def test_listings_empty_registry_message(runner, listings_env, monkeypatch):
    FakeListingsChain(monkeypatch, enumerated=[], listings={})

    result = runner.invoke(app, ["listings"])
    out = all_output(result)
    assert result.exit_code == 0, out
    assert "sellerCount() == 0" in out
    assert "nobody has registered a listing yet" in out


def test_listings_env_override_caps_changes_estimate(runner, listings_env, monkeypatch):
    monkeypatch.setenv("PROMPT_TOKEN_CAP", "100000")
    monkeypatch.setenv("COMPLETION_TOKEN_CAP", "0")
    FakeListingsChain(
        monkeypatch, enumerated=[SELLER_A], listings={SELLER_A: make_listing(SELLER_A)}
    )

    result = runner.invoke(app, ["listings"])
    out = all_output(result)
    assert result.exit_code == 0, out
    # (50_000*100_000 + 100_000*0)//1e6 = 5_000
    assert "5000 native (= 0.005 USDC)" in out


def test_listings_getlisting_failure_is_clean_error(runner, listings_env, monkeypatch):
    FakeListingsChain(monkeypatch, enumerated=[SELLER_A], listings={})

    def boom(c, op):
        raise ValueError("rpc dropped")

    monkeypatch.setattr(chain_mod, "get_listing", boom)

    result = runner.invoke(app, ["listings"])
    out = all_output(result)
    assert result.exit_code == 2
    assert "getListing" in out and "rpc dropped" in out


def test_listings_short_prices_tail_skipped(runner, listings_env, monkeypatch):
    """Malformed listing (prices shorter than models) degrades gracefully:
    priced rows render, the unpriced tail is skipped without crashing."""
    FakeListingsChain(
        monkeypatch,
        enumerated=[SELLER_A],
        listings={
            SELLER_A: make_listing(
                SELLER_A, models=["m1", "m2", "m3"], prices=[PRICE_A]
            )
        },
    )

    result = runner.invoke(app, ["listings"])
    out = all_output(result)
    assert result.exit_code == 0, out
    assert "1 model price rows" in out
    assert "m1" in out


# --------------------------------------------------------------------------
# command: --json
# --------------------------------------------------------------------------
def test_listings_json_schema(runner, listings_env, monkeypatch):
    FakeListingsChain(
        monkeypatch,
        enumerated=[SELLER_B, SELLER_A],
        listings={
            SELLER_A: make_listing(SELLER_A, models=["kimi-k2.6"]),
            SELLER_B: make_listing(SELLER_B, models=["kimi-k2.6"], prices=[PRICE_B]),
        },
    )

    result = runner.invoke(app, ["listings", "--json"])
    out = all_output(result)
    assert result.exit_code == 0, out

    payload = json.loads(out)
    assert set(payload) == {
        "chainId", "registry", "sellerCount", "activeOnly", "model", "count", "listings"
    }
    assert payload["chainId"] == 10143
    assert payload["registry"] == REGISTRY_ADDR
    assert payload["sellerCount"] == 2  # chain enumeration size (pre-filter)
    assert payload["activeOnly"] is True
    assert payload["model"] is None
    assert payload["count"] == 2
    first = payload["listings"][0]
    assert set(first) == {
        "operator", "endpoint", "model",
        "priceCachedIn", "priceInput", "priceOutput", "estimatedCallCost",
    }
    assert first["operator"] == SELLER_A  # cheapest first
    assert first["model"] == "kimi-k2.6"
    assert first["priceInput"] == 50_000
    assert first["estimatedCallCost"] == 13_200  # (50k*200k + 100k*32k)//1e6
    assert payload["listings"][1]["estimatedCallCost"] == 26_400


def test_listings_json_model_filter(runner, listings_env, monkeypatch):
    FakeListingsChain(
        monkeypatch,
        enumerated=[SELLER_A],
        listings={
            SELLER_A: make_listing(SELLER_A, models=["kimi-k2.6", "k3"], prices=[PRICE_A, PRICE_CHEAP])
        },
    )

    result = runner.invoke(app, ["listings", "--json", "--model", "k3"])
    payload = json.loads(all_output(result))
    assert result.exit_code == 0
    assert payload["model"] == "k3"
    assert payload["count"] == 1
    assert payload["listings"][0]["model"] == "k3"
    assert payload["listings"][0]["estimatedCallCost"] == 2_640


def test_listings_json_empty(runner, listings_env, monkeypatch):
    FakeListingsChain(monkeypatch, enumerated=[], listings={})

    result = runner.invoke(app, ["listings", "--json"])
    payload = json.loads(all_output(result))
    assert result.exit_code == 0
    assert payload["sellerCount"] == 0
    assert payload["count"] == 0
    assert payload["listings"] == []


# --------------------------------------------------------------------------
# discoverability
# --------------------------------------------------------------------------
def test_help_lists_listings(runner):
    result = runner.invoke(app, ["--help"])
    out = all_output(result)
    assert result.exit_code == 0
    assert "listings" in out
