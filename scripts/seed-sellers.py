#!/usr/bin/env python3
"""Seed N seller listings for a lively market page (#7, demo observation).

Each seller key registers its own listing with a DIFFERENT model subset and
DIFFERENT prices (deterministic rotation — same keys always seed the same
shape). Keys come from --keys (repeatable) or the SEED_SELLER_KEYS env var
(space/comma/newline separated). Private keys are NEVER printed — only the
derived addresses.

⚠️  Every key needs MON for register gas (faucet.monad.xyz) — keys with a
zero balance are skipped with a warning, the rest proceed. In a real setup
each seller would also run its OWN relay process serving its listed models
at its registered endpoint; for the demo all listings may point at one relay
URL (--endpoint), which is fine for the market view.

    python3 scripts/seed-sellers.py \
        --network monad_testnet --endpoint http://127.0.0.1:8787 \
        --keys 0xaaa... 0xbbb...
    SEED_SELLER_KEYS="0xaaa... 0xbbb..." python3 scripts/seed-sellers.py --dry-run

Prices are USDC native 6dp per 1M tokens; the per-seller multiplier is
1 + 0.25×i (seller 0 cheapest, seller N-1 most expensive).
"""

from __future__ import annotations

import argparse
import os
import re
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO / "e2e"))

from dotenv_loader import load_dotenv  # noqa: E402

load_dotenv()

from run import NETWORKS, REGISTRY_ABI, decode_price_tuple, price_tuple  # noqa: E402
from register_listing import gas_for, send_and_wait  # noqa: E402

# Demo catalog — per-model listing names. Adjust to what your upstream plan
# can actually serve (the relay 400s models its key cannot serve).
DEFAULT_CATALOG = (
    "kimi-k2.6",
    "kimi-k2-turbo-preview",
    "kimi-latest",
    "moonshot-v1-8k",
)
# Deterministic per-seller subsets (indices into the catalog).
SUBSETS = ((0, 1), (1, 2), (0, 2, 3), (1, 3), (0, 3), (2, 3))
# Base price triple (cached, input, output), USDC native 6dp / 1M tokens.
BASE_PRICES = (500_000, 1_000_000, 1_500_000)

_KEY_RE = re.compile(r"^0x[0-9a-fA-F]{64}$")


def collect_keys(args: argparse.Namespace) -> list[str]:
    keys: list[str] = list(args.keys)
    env_keys = (os.environ.get("SEED_SELLER_KEYS") or "").strip()
    if env_keys:
        keys.extend(k for k in re.split(r"[\s,]+", env_keys) if k)
    deduped: list[str] = []
    for k in keys:
        if not _KEY_RE.match(k):
            sys.exit(f"bad private key format (want 0x + 64 hex): 0x…{k[-6:]}" if k.startswith("0x")
                     else "bad private key format (want 0x + 64 hex)")
        if k not in deduped:
            deduped.append(k)
    if not deduped:
        sys.exit(
            "no seller keys given — pass --keys 0x… (repeatable) or set "
            "SEED_SELLER_KEYS (space/comma separated)"
        )
    return deduped


def plan_for(index: int, catalog: list[str]) -> tuple[list[str], list[dict[str, int]]]:
    """Deterministic variety: subset rotation × price multiplier 1 + 0.25i."""
    subset = SUBSETS[index % len(SUBSETS)]
    models = [catalog[i % len(catalog)] for i in subset]
    mult = 1 + 0.25 * index
    prices = [
        {"cached": int(BASE_PRICES[0] * mult),
         "input": int(BASE_PRICES[1] * mult),
         "output": int(BASE_PRICES[2] * mult)}
        for _ in models
    ]
    return models, prices


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Seed N seller listings (different model subsets + prices)"
    )
    parser.add_argument("--network", default="monad_testnet", choices=sorted(NETWORKS))
    parser.add_argument("--endpoint", default=os.environ.get("RELAY_URL", "http://127.0.0.1:8787"),
                        help="Relay URL written into every listing (default %(default)s)")
    parser.add_argument("--keys", action="append", default=[],
                        help="Seller private key 0x… (repeatable; or env SEED_SELLER_KEYS)")
    parser.add_argument("--catalog", nargs="*", default=list(DEFAULT_CATALOG),
                        help="Model-name pool (default: Kimi-plan names — adjust to your upstream)")
    parser.add_argument("--dry-run", action="store_true",
                        help="Print the planned listings without sending any tx")
    args = parser.parse_args()

    keys = collect_keys(args)
    catalog = [m for m in args.catalog if m] or list(DEFAULT_CATALOG)

    cfg = NETWORKS[args.network]
    rpc_url = os.environ.get("RPC_URL") or os.environ.get(cfg["rpc_env"]) or cfg["default_rpc"]

    registry_addr = (os.environ.get("REGISTRY_ADDR") or "").strip()
    if not registry_addr:
        for candidate in (REPO / "contracts" / "deployed.monad.json",
                          REPO / "contracts" / "deployed.json"):
            try:
                import json
                registry_addr = json.loads(candidate.read_text(encoding="utf-8")).get("registry", "")
            except (OSError, ValueError):
                registry_addr = ""
            if registry_addr:
                print(f"registry addr from {candidate.name}: {registry_addr}")
                break
    if not registry_addr:
        sys.exit("no REGISTRY_ADDR and no deployed artifact — deploy first")

    # web3 only needed past arg validation (keeps --help dependency-free).
    from web3 import Web3

    w3 = Web3(Web3.HTTPProvider(rpc_url))
    if int(w3.eth.chain_id) != cfg["chain_id"]:
        sys.exit(f"RPC chainId {w3.eth.chain_id} != expected {cfg['chain_id']}")
    registry = w3.eth.contract(address=w3.to_checksum_address(registry_addr), abi=REGISTRY_ABI)

    zero = "0x" + "0" * 40
    seeded, skipped = [], []
    for index, key in enumerate(keys):
        from eth_account import Account

        seller = Account.from_key(key)
        operator = w3.to_checksum_address(seller.address)
        models, prices = plan_for(index, catalog)

        print(f"\n── seller #{index} {operator}")
        for model, price in zip(models, prices):
            print(f"   {model}: cached={price['cached']} input={price['input']} "
                  f"output={price['output']} (native/1M)")

        balance = w3.from_wei(w3.eth.get_balance(operator), "ether")
        if balance == 0:
            print("   SKIP: zero MON balance — fund this key at faucet.monad.xyz first")
            skipped.append(operator)
            continue

        listing_op, endpoint, _models, _prices, active = registry.functions.getListing(
            operator
        ).call()
        if str(listing_op) != zero and active:
            print(f"   SKIP: already has an ACTIVE listing (endpoint={endpoint}) "
                  "— not overwritten (use scripts/re-register-endpoint.py to change it)")
            seeded.append(operator)
            continue

        if args.dry_run:
            print(f"   DRY-RUN: would register endpoint={args.endpoint}")
            seeded.append(operator)
            continue

        reg_tx = registry.functions.register(
            args.endpoint, models, [price_tuple(p) for p in prices]
        )
        tx_hash = send_and_wait(w3, seller, reg_tx.build_transaction(
            {"from": seller.address,
             "nonce": w3.eth.get_transaction_count(seller.address),
             "gas": gas_for(w3, seller.address, reg_tx),
             "chainId": cfg["chain_id"]}
        ))
        got = [decode_price_tuple(p) for p in
               registry.functions.getListing(operator).call()[3]]
        if got != prices:
            sys.exit(f"on-chain prices {got} != planned {prices}")
        print(f"   registered: endpoint={args.endpoint} tx: {tx_hash}")
        seeded.append(operator)

    print(f"\ndone: {len(seeded)} seeded, {len(skipped)} skipped "
          f"(zero-balance: {skipped or 'none'})")
    print("view the market: python3 -m http.server 8080 -d web  → http://localhost:8080/market.html")


if __name__ == "__main__":
    main()
