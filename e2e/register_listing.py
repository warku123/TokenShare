#!/usr/bin/env python3
"""Register (or re-register after deactivate) a seller listing on Registry.

Used for MANUAL demos (e2e/run.py does this automatically inside the e2e):

    python3 e2e/register_listing.py --network base_sepolia \
        --endpoint http://127.0.0.1:8787 \
        --price-cached-in 1000 --price-input 2000 --price-output 3000

Env:
    SELLER_PRIVATE_KEY  operator key (required; never logged)
    REGISTRY_ADDR       optional override; else contracts/deployed.json
    RPC_URL             optional override; else network RPC (NETWORKS map)

Prices are USDC native units per 1M tokens (6dp: 1 USDC = 1_000_000).
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from run import NETWORKS, REGISTRY_ABI  # noqa: E402


def main() -> None:
    parser = argparse.ArgumentParser(description="TokenShare Registry listing register")
    parser.add_argument("--network", required=True, choices=sorted(NETWORKS))
    parser.add_argument("--endpoint", required=True, help="Relay URL buyers will POST to.")
    parser.add_argument("--model", action="append", default=[],
                        help="Served model name (repeatable; default gpt-4o-mini-tokenshare).")
    parser.add_argument("--price-cached-in", type=int, required=True,
                        help="USDC native units per 1M cached input tokens (6dp).")
    parser.add_argument("--price-input", type=int, required=True,
                        help="USDC native units per 1M input tokens (6dp).")
    parser.add_argument("--price-output", type=int, required=True,
                        help="USDC native units per 1M output tokens (6dp).")
    args = parser.parse_args()

    from eth_account import Account
    from web3 import Web3

    seller_key = os.environ.get("SELLER_PRIVATE_KEY")
    if not seller_key:
        sys.exit("SELLER_PRIVATE_KEY missing in env (operator key; never logged)")
    seller = Account.from_key(seller_key)

    cfg = NETWORKS[args.network]
    rpc_url = os.environ.get("RPC_URL") or os.environ.get(cfg["rpc_env"]) or cfg["default_rpc"]
    w3 = Web3(Web3.HTTPProvider(rpc_url))
    if int(w3.eth.chain_id) != cfg["chain_id"]:
        sys.exit(f"RPC chainId {w3.eth.chain_id} != expected {cfg['chain_id']}")

    registry_addr = os.environ.get("REGISTRY_ADDR")
    if not registry_addr:
        artifact = Path(__file__).parents[1] / "contracts" / "deployed.json"
        try:
            registry_addr = json.loads(artifact.read_text()).get("registry")
        except (OSError, json.JSONDecodeError):
            registry_addr = None
    if not registry_addr:
        sys.exit("no REGISTRY_ADDR in env and contracts/deployed.json missing — deploy first")

    registry = w3.eth.contract(address=w3.to_checksum_address(registry_addr), abi=REGISTRY_ABI)
    models = args.model or ["gpt-4o-mini-tokenshare"]
    tx = registry.functions.register(
        args.endpoint, models, args.price_cached_in, args.price_input, args.price_output
    ).build_transaction(
        {
            "from": seller.address,
            "nonce": w3.eth.get_transaction_count(seller.address),
            "gas": 400_000,
            "chainId": cfg["chain_id"],
        }
    )
    signed = seller.sign_transaction(tx)
    raw = getattr(signed, "raw_transaction", None) or signed.rawTransaction
    tx_hash = w3.eth.send_raw_transaction(raw)
    receipt = w3.eth.wait_for_transaction_receipt(tx_hash, timeout=60.0, poll_latency=1.0)
    if receipt.get("status", 0) != 1:
        sys.exit(f"register tx reverted: {tx_hash.hex()}")
    listing = registry.functions.getListing(w3.to_checksum_address(seller.address)).call()
    print(f"registered: operator={seller.address} endpoint={args.endpoint} models={models}")
    print(f"listing active: {bool(listing[-1])} tx: {tx_hash.hex()}")


if __name__ == "__main__":
    main()
