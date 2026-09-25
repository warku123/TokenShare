#!/usr/bin/env python3
"""Re-point the caller's Registry listing ENDPOINT (deactivate → re-register).

Keeps EVERY listed model and its on-chain price: the current listing is read
via getListing, models/prices are backfilled from the chain (no manual
retyping), and the register itself is delegated to e2e/register_listing.py
(--model-price full suite: per-model price book, gas_for estimation, on-chain
post-verification). Typical use — after opening a demo tunnel:

    python3 scripts/re-register-endpoint.py --network monad_testnet \
        --endpoint https://<random>.trycloudflare.com

Env:
    SELLER_PRIVATE_KEY  operator key (required; never logged)
    RPC_URL / REGISTRY_ADDR  optional overrides — else the network default /
                        contracts/deployed.monad.json (then deployed.json)
"""

from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO / "e2e"))

from dotenv_loader import load_dotenv  # noqa: E402

load_dotenv()

from run import NETWORKS, REGISTRY_ABI, decode_price_tuple  # noqa: E402
from register_listing import gas_for, send_and_wait  # noqa: E402

# contracts/deployed.monad.json is authoritative (e2e/run.py semantics),
# falling back to the generic deployed.json.
_DEPLOYED_CANDIDATES = (
    REPO / "contracts" / "deployed.monad.json",
    REPO / "contracts" / "deployed.json",
)


def resolve_registry_addr() -> str:
    addr = (os.environ.get("REGISTRY_ADDR") or "").strip()
    if addr:
        return addr
    for path in _DEPLOYED_CANDIDATES:
        try:
            addr = json.loads(path.read_text(encoding="utf-8")).get("registry")
        except (OSError, json.JSONDecodeError):
            continue
        if addr:
            print(f"registry addr from {path.name}: {addr}")
            return str(addr)
    sys.exit("no REGISTRY_ADDR in env and no deployed artifact with 'registry' — deploy first")


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Swap a seller listing's endpoint (deactivate → re-register, "
        "models+prices preserved from the chain)"
    )
    parser.add_argument("--network", default="monad_testnet", choices=sorted(NETWORKS))
    parser.add_argument("--endpoint", required=True,
                        help="New relay URL buyers will POST to (e.g. a tunnel URL)")
    parser.add_argument("--relay", default=os.environ.get("RELAY_URL", "http://127.0.0.1:8787"),
                        help="Relay base for the register pre-check (default %(default)s)")
    parser.add_argument("--skip-verify", action="store_true",
                        help="Skip the /verify-upstream pre-check in register_listing")
    args = parser.parse_args()

    key = (os.environ.get("SELLER_PRIVATE_KEY") or "").strip()
    if not key:
        sys.exit("SELLER_PRIVATE_KEY missing in env/.env (never logged)")

    from eth_account import Account
    from web3 import Web3

    cfg = NETWORKS[args.network]
    rpc_url = os.environ.get("RPC_URL") or os.environ.get(cfg["rpc_env"]) or cfg["default_rpc"]
    w3 = Web3(Web3.HTTPProvider(rpc_url))
    if int(w3.eth.chain_id) != cfg["chain_id"]:
        sys.exit(f"RPC chainId {w3.eth.chain_id} != expected {cfg['chain_id']}")

    registry = w3.eth.contract(
        address=w3.to_checksum_address(resolve_registry_addr()), abi=REGISTRY_ABI
    )
    seller = Account.from_key(key)
    operator = w3.to_checksum_address(seller.address)

    # ---- read the CURRENT listing: models + prices to preserve -------------
    listing_op, old_endpoint, models, raw_prices, active = registry.functions.getListing(
        operator
    ).call()
    if str(listing_op) == "0x" + "0" * 40:
        sys.exit(
            "operator has NO listing on this Registry — nothing to re-point; "
            "use e2e/register_listing.py to register fresh"
        )
    prices = [decode_price_tuple(p) for p in raw_prices]
    print(f"current listing: endpoint={old_endpoint} active={active}")
    for model, price in zip(models, prices):
        print(f"  {model}: cached={price['cached']} input={price['input']} "
              f"output={price['output']}")

    bad = [m for m in models if ":" in str(m)]
    if bad:
        sys.exit(f"model names contain ':' (breaks the price-spec format): {bad}")

    # ---- deactivate (v2 AlreadyRegistered guard) when active ----------------
    if active:
        print("listing ACTIVE — deactivating first (v2 AlreadyRegistered guard)")
        tx = registry.functions.deactivate()
        send_and_wait(w3, seller, tx.build_transaction(
            {"from": seller.address,
             "nonce": w3.eth.get_transaction_count(seller.address),
             "gas": gas_for(w3, seller.address, tx),
             "chainId": cfg["chain_id"]}
        ))
        print("deactivated")
    else:
        print("listing inactive — register straight away")

    # ---- re-register via the e2e suite (price book + gas_for + verify) ------
    cmd = [
        sys.executable, str(REPO / "e2e" / "register_listing.py"),
        "--network", args.network,
        "--endpoint", args.endpoint,
        "--relay", args.relay,
    ]
    for model, price in zip(models, prices):
        cmd.append(f"--model-price={model}:{price['cached']}:{price['input']}:{price['output']}")
    if args.skip_verify:
        cmd.append("--skip-verify")

    env = dict(os.environ)
    env["REGISTRY_ADDR"] = registry.address  # authoritative for the child too
    print("register:", " ".join(cmd[:6]), f"... ({len(models)} --model-price flags)")
    result = subprocess.run(cmd, cwd=str(REPO), env=env)
    sys.exit(result.returncode)


if __name__ == "__main__":
    main()
