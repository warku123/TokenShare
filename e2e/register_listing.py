#!/usr/bin/env python3
"""Register (or re-register after deactivate) a seller listing on Registry.

Registry v2 (M9): prices are PER MODEL — register takes a Price[] array
parallel to models[], and an EXISTING model's price is changed with
updateModelPrice(model, price). Used for MANUAL demos (e2e/run.py does this
automatically inside the e2e):

    python3 e2e/register_listing.py --network base_sepolia \
        --endpoint http://127.0.0.1:8787 \
        --price-cached-in 1000000 --price-input 2000000 --price-output 3000000

    # multi-model with per-model prices:
    python3 e2e/register_listing.py --network base_sepolia \
        --endpoint http://127.0.0.1:8787 \
        --model kimi-k2.6 --model-price kimi-k2.0:500000:700000:900000

    # update ONE listed model's price on the caller's ACTIVE listing:
    python3 e2e/register_listing.py --network base_sepolia \
        --update-price kimi-k2.6:1200000:2200000:3200000

Env:
    SELLER_PRIVATE_KEY  operator key (required; never logged)
    REGISTRY_ADDR       optional override; else contracts/deployed.json
    RPC_URL             optional override; else network RPC (NETWORKS map)

Prices are USDC native units per 1M tokens (6dp: 1 USDC = 1_000_000).
Price assignment (v2): the --price-* triple prices the FIRST model; every
further model needs an explicit --model-price (no silent pricing).
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parent))
from run import (  # noqa: E402
    NETWORKS,
    REGISTRY_ABI,
    decode_price_tuple,
    price_tuple,
)
from dotenv_loader import load_dotenv  # noqa: E402

load_dotenv()

DEFAULT_RELAY_URL = "http://127.0.0.1:8787"
DEFAULT_MODEL = "gpt-4o-mini-tokenshare"


def _require_env(name: str) -> str:
    value = os.environ.get(name)
    if not value or not value.strip():
        sys.exit(f"{name} missing in env (.env or shell; never logged)")
    return value.strip()


def parse_price_spec(spec: str) -> tuple[str, dict[str, int]]:
    """'MODEL:CACHED:INPUT:OUTPUT' → (model, {"cached","input","output"}).

    Prices are USDC native 6dp integers per 1M tokens; exits with a readable
    message on any malformed spec."""
    parts = spec.split(":")
    if len(parts) != 4:
        sys.exit(f"price spec must be MODEL:CACHED:INPUT:OUTPUT, got {spec!r}")
    name, cached, price_in, price_out = (p.strip() for p in parts)
    if not name:
        sys.exit(f"empty model name in price spec {spec!r}")
    try:
        values = {"cached": int(cached), "input": int(price_in), "output": int(price_out)}
    except ValueError:
        sys.exit(f"prices must be integers (USDC native 6dp) in {spec!r}")
    if any(v < 0 for v in values.values()):
        sys.exit(f"prices must be >= 0 in {spec!r}")
    return name, values


def build_price_book(
    model_flags: list[str],
    model_price_flags: list[str],
    first_prices: dict[str, int] | None,
) -> tuple[list[str], list[dict[str, int]]]:
    """Registry v2 register inputs: (models, prices) parallel arrays.

    - model order = first-seen across --model and --model-price flags
      (the DEFAULT_MODEL when neither flag is used);
    - the FIRST model gets `first_prices` (the --price-* triple) unless an
      explicit --model-price for it exists (explicit wins);
    - every further model needs an explicit --model-price (no silent pricing)."""
    price_book: dict[str, dict[str, int] | None] = {}
    models: list[str] = []

    def add(name: str) -> None:
        if name not in price_book:
            price_book[name] = None
        if name not in models:
            models.append(name)

    for name in model_flags:
        add(name)
    for name, prices in (parse_price_spec(spec) for spec in model_price_flags):
        add(name)
        price_book[name] = prices
    if not models:
        models = [DEFAULT_MODEL]
    if first_prices is not None and price_book.get(models[0]) is None:
        price_book[models[0]] = dict(first_prices)
    missing = [m for m in models if price_book.get(m) is None]
    if missing:
        sys.exit(
            "Registry v2: every model needs a price — no --model-price given "
            f"for: {missing} (--price-* only prices the FIRST model "
            f"{models[0]!r})"
        )
    return models, [dict(price_book[m]) for m in models]  # type: ignore[arg-type]


def verify_upstream_precheck(relay_url: str, models: list[str]) -> None:
    """Seller-side pre-check before paying register-gas: ask the relay's
    GET /verify-upstream whether the configured key can actually serve the
    models being listed. Aborts with a readable reason on refusal; a
    --skip-verify flag or unreachable relay supports the old flow."""
    if os.environ.get("SKIP_VERIFY", "").strip() == "1":
        return
    import httpx

    url = relay_url.rstrip("/") + "/verify-upstream"
    try:
        resp = httpx.get(url, timeout=15.0)
    except Exception as exc:  # noqa: BLE001
        print(f"WARNING: relay not reachable at {relay_url} ({exc}) — "
              "skipping upstream pre-check (或重跑并 --skip-verify 显式跳过)")
        return
    if resp.status_code != 200:
        print(f"WARNING: relay /verify-upstream HTTP {resp.status_code} — "
              "skipping pre-check")
        return
    body = resp.json()
    if not body.get("key_valid"):
        sys.exit(
            "refusing to register: the relay's upstream key is NOT valid.\n"
            f"  relay said: key_valid=false upstream_host={body.get('upstream_host')}\n"
            f"  error: {body.get('error')}\n"
            "Fix OPENAI_API_KEY / OPENAI_BASE_URL in .env first."
        )
    accessible = set(body.get("accessible_models") or [])
    missing = [m for m in models if m not in accessible]
    if missing:
        sys.exit(
            "refusing to register: models not accessible with the relay's key:\n"
            f"  missing: {missing}\n"
            f"  accessible: {sorted(accessible)}\n"
            "List only models the upstream key can actually serve."
        )
    if not body.get("listing_ok") and body.get("mismatches") not in (None, []):
        # Extra cross-check failures beyond key/model (e.g. provider mismatch
        # on an official host) — surface them.
        sys.exit(
            "refusing to register: relay reports listing-model mismatches:\n"
            f"  {body.get('mismatches')}"
        )
    print(f"pre-check OK: key serves {len(accessible)} models, "
          f"all listed models confirmed accessible ({', '.join(models)})")


def gas_for(w3: Any, frm: str, fn_call: Any, fallback: int = 1_500_000) -> int:
    """Estimate gas with a 30% + 30k buffer; fall back rather than fail
    (estimation can be blocked by RPC quirks while broadcast succeeds).
    Hardcoded caps previously caused out-of-gas reverts on multi-model
    Registry v2 registers (4 models + Price structs > 400k)."""
    try:
        est = fn_call.estimate_gas({"from": frm})
        return int(est * 1.3) + 30_000
    except Exception:  # noqa: BLE001
        return fallback


def send_and_wait(w3: Any, seller: Any, tx: dict) -> str:
    """Sign + send + wait; returns the tx hash or exits on revert."""
    signed = seller.sign_transaction(tx)
    raw = getattr(signed, "raw_transaction", None) or signed.rawTransaction
    tx_hash = w3.eth.send_raw_transaction(raw)
    receipt = w3.eth.wait_for_transaction_receipt(tx_hash, timeout=60.0, poll_latency=1.0)
    if receipt.get("status", 0) != 1:
        sys.exit(f"tx reverted: {tx_hash.hex()}")
    return tx_hash.hex()


def resolve_registry_addr() -> str:
    registry_addr = os.environ.get("REGISTRY_ADDR")
    if not registry_addr:
        artifact = Path(__file__).parents[1] / "contracts" / "deployed.json"
        try:
            registry_addr = json.loads(artifact.read_text()).get("registry")
        except (OSError, json.JSONDecodeError):
            registry_addr = None
    if not registry_addr:
        sys.exit("no REGISTRY_ADDR in env and contracts/deployed.json missing — deploy first")
    return registry_addr


def main() -> None:
    parser = argparse.ArgumentParser(
        description="TokenShare Registry listing register (v2 per-model prices)"
    )
    parser.add_argument("--network", required=True, choices=sorted(NETWORKS))
    parser.add_argument("--endpoint", help="Relay URL buyers will POST to (register mode).")
    parser.add_argument("--model", action="append", default=[],
                        help="Served model name (repeatable; default "
                             f"{DEFAULT_MODEL}). The FIRST model is priced by "
                             "the --price-* triple.")
    parser.add_argument("--model-price", action="append", default=[],
                        metavar="MODEL:CACHED:INPUT:OUTPUT",
                        help="Per-model tiered price (6dp native per 1M tokens). "
                             "Repeatable; appends the model if new, overrides "
                             "if already listed.")
    parser.add_argument("--update-price", action="append", default=[],
                        metavar="MODEL:CACHED:INPUT:OUTPUT",
                        help="Instead of register: updateModelPrice(model, price) "
                             "on the caller's ACTIVE listing (Registry v2 "
                             "per-model price change). Repeatable.")
    parser.add_argument("--price-cached-in", type=int, default=None,
                        help="USDC native units per 1M cached input tokens (6dp); "
                             "prices the FIRST model.")
    parser.add_argument("--price-input", type=int, default=None,
                        help="USDC native units per 1M input tokens (6dp); "
                             "prices the FIRST model.")
    parser.add_argument("--price-output", type=int, default=None,
                        help="USDC native units per 1M output tokens (6dp); "
                             "prices the FIRST model.")
    parser.add_argument("--relay", default=os.environ.get("RELAY_URL", DEFAULT_RELAY_URL),
                        help=f"Relay base URL for the pre-check (default {DEFAULT_RELAY_URL} / env RELAY_URL).")
    parser.add_argument("--skip-verify", action="store_true",
                        help="Skip the /verify-upstream pre-check entirely.")
    args = parser.parse_args()

    triple = (args.price_cached_in, args.price_input, args.price_output)
    if all(p is None for p in triple):
        first_prices = None
    elif any(p is None for p in triple):
        sys.exit("--price-cached-in/--price-input/--price-output must be given together")
    else:
        first_prices = {"cached": triple[0], "input": triple[1], "output": triple[2]}

    from eth_account import Account
    from web3 import Web3

    seller_key = _require_env("SELLER_PRIVATE_KEY")
    seller = Account.from_key(seller_key)

    cfg = NETWORKS[args.network]
    rpc_url = os.environ.get("RPC_URL") or os.environ.get(cfg["rpc_env"]) or cfg["default_rpc"]
    w3 = Web3(Web3.HTTPProvider(rpc_url))
    if int(w3.eth.chain_id) != cfg["chain_id"]:
        sys.exit(f"RPC chainId {w3.eth.chain_id} != expected {cfg['chain_id']}")

    registry = w3.eth.contract(
        address=w3.to_checksum_address(resolve_registry_addr()), abi=REGISTRY_ABI
    )
    operator = w3.to_checksum_address(seller.address)

    # ---- update-only mode: Registry v2 per-model price change ----------------
    if args.update_price:
        for spec in args.update_price:
            model, prices = parse_price_spec(spec)
            tx = registry.functions.updateModelPrice(model, price_tuple(prices))
            tx_hash = send_and_wait(w3, seller, tx.build_transaction(
                {"from": seller.address, "nonce": w3.eth.get_transaction_count(seller.address),
                 "gas": gas_for(w3, seller.address, tx), "chainId": cfg["chain_id"]}
            ))
            got = decode_price_tuple(registry.functions.getPrice(operator, model).call())
            if got != prices:
                sys.exit(f"getPrice({model!r}) = {got} != updated {prices}")
            print(f"updated: {model}: cached={prices['cached']} "
                  f"input={prices['input']} output={prices['output']} tx: {tx_hash}")
        return

    # ---- register path -------------------------------------------------------
    models, prices = build_price_book(args.model, args.model_price, first_prices)
    if not args.endpoint:
        sys.exit("--endpoint is required to register a listing")
    if not args.skip_verify:
        verify_upstream_precheck(args.relay, models)

    reg_tx = registry.functions.register(
        args.endpoint, models, [price_tuple(p) for p in prices]
    )
    tx_hash = send_and_wait(w3, seller, reg_tx.build_transaction(
        {"from": seller.address, "nonce": w3.eth.get_transaction_count(seller.address),
         "gas": gas_for(w3, seller.address, reg_tx), "chainId": cfg["chain_id"]}
    ))

    listing_operator, _endpoint, got_models, got_prices, active = registry.functions.getListing(
        operator
    ).call()
    if not active:
        sys.exit("register tx mined but listing not active")
    if list(got_models) != models:
        sys.exit(f"listing models {list(got_models)} != requested {models}")
    got_prices_decoded = [decode_price_tuple(p) for p in got_prices]
    if got_prices_decoded != prices:
        sys.exit(f"listing per-model prices {got_prices_decoded} != requested {prices}")

    print(f"registered: operator={seller.address} endpoint={args.endpoint} tx: {tx_hash}")
    for model, price in zip(models, prices):
        onchain = decode_price_tuple(registry.functions.getPrice(operator, model).call())
        if onchain != price:
            sys.exit(f"getPrice({model!r}) = {onchain} != requested {price}")
        print(f"  {model}: cached={price['cached']} input={price['input']} "
              f"output={price['output']} (native/1M tokens)")
    print(f"listing active: True ({len(models)} model price rows, "
          "v2 getPrice verified)")


if __name__ == "__main__":
    main()
