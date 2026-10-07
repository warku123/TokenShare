#!/usr/bin/env python3
"""One-shot Registry registrar for the TEE-derived seller key (M7-B).

WHY THIS EXISTS: in TEE mode the relay derives its seller private key
INSIDE the Phala Cloud / dstack CVM via ``dstack get_key`` — the key never
lives in env vars or on disk outside the enclave, so nobody (including the
operator) can sign Registry transactions from outside. But
``Registry.register`` / ``deactivate`` must be signed by the derived
address (listing operator == msg.sender) or buyers' signature checks
fail. This script runs as a ONE-SHOT sidecar container in the SAME CVM as
the relay (same image, same ``/var/run/dstack.sock``): it derives the same
key from inside the enclave, signs the listing transaction there, and
exits. It is idempotent: when the on-chain listing already matches the
desired one it prints ``already OK`` and exits 0 immediately — safe
across CVM restarts (the CVM re-runs it on boot).

ENVIRONMENT (all required unless a default is noted; every chain value is
injected via env — zero hardcoding, BUILD_SPEC §2.5):

  DSTACK_SOCKET_PATH  dstack guest-agent socket (default: /var/run/dstack.sock)
  RPC_URL             JSON-RPC endpoint (e.g. Monad testnet https URL)
  CHAIN_ID            expected chain id — asserted against the RPC
  REGISTRY_ADDR       Registry contract address (v4 ABI face)
  REGISTRAR_ENDPOINT  the PUBLIC https URL buyers hit (the relay gateway URL);
                      stored verbatim as the listing endpoint
  REGISTRAR_MODELS    csv of model names, e.g. "deepseek-v4.1-flash,glm-5.3-flash"
  REGISTRAR_PRICES    csv parallel to REGISTRAR_MODELS, one "cachedIn:input:output"
                      triple per model in USDC NATIVE units (6 decimal places,
                      1 USDC = 1_000_000) per 1M tokens, e.g.
                        "1000000:2000000:3000000,500000:1000000:1500000"
                      A SINGLE triple is broadcast to every model.

FLOW: derive key (dstack get_key path MUST equal relay/app/config.py
TEE_KEY_PATH = 'wallet/ethereum/tokenshare') -> print the derived ADDRESS
(the private key is NEVER printed or logged) -> connect to RPC and assert
chainId -> read getListing(derived address):
  * active and endpoint/models/prices all equal -> print "already OK", exit 0
  * active but different                        -> deactivate (wait receipt,
                                                   status==1) then register
  * inactive (or never registered)              -> register directly
Finally echo the terminal getListing.

MODE GUARD: this script is SINGLE-MODE ONLY. When RELAY_MODE=shared (M15
R1 shared custody) it fails fast BEFORE any key derivation, network access
or registration — the shared signer is not a seller EOA and must never be
registered (see guard_refuse_shared + tests/test_registrar_shared_guard.py).

All amounts are integers end to end — USDC native units, no floats.
"""

from __future__ import annotations

import os
import sys

# ---------------------------------------------------------------------------
# Mode guard (M15 R1): the registrar is SINGLE-MODE ONLY.
# ---------------------------------------------------------------------------


def guard_refuse_shared() -> None:
    """Fail-fast when RELAY_MODE=shared — BEFORE any key derivation, network
    access or registration. In shared custody (M15 R1) the Registry listing
    operator is each seller's OWN external EOA: the seller registers its
    listing and signs Escrow approveSettleDelegate itself via explicit
    web-frontend transactions. The TEE-derived shared signer is NOT a seller
    EOA and must never be registered as an operator — running this registrar
    in a shared CVM would register the shared signer's address and break
    buyer verification. The check reads ONLY the mode env var (no socket, no
    RPC, no key material) and the error carries no secrets."""
    mode = (
        os.environ.get(ENV_RELAY_MODE, "") or MODE_SINGLE
    ).strip().lower() or MODE_SINGLE
    if mode == MODE_SHARED:
        raise RegistrarError(
            "RELAY_MODE=shared: this registrar is single-mode ONLY and must "
            "NOT run in a shared-custody deployment. In shared mode the "
            "listing operator is each seller's own external EOA — the seller "
            "registers its listing and calls Escrow approveSettleDelegate "
            "itself via explicit web-frontend transactions; the TEE-derived "
            "shared signer is not a seller EOA and must never be registered. "
            "Refusing before any key derivation, network access or "
            "registration (no degraded mode)."
        )


# ---------------------------------------------------------------------------
# Config (env-driven, fail-fast)
# ---------------------------------------------------------------------------

# MUST equal relay/app/config.py TEE_KEY_PATH — the registrar derives the
# SAME seller key the relay serves with, otherwise the registered operator
# differs from the relay's signing address and buyer signature checks break.
TEE_KEY_PATH = "wallet/ethereum/tokenshare"

# RELAY_MODE value that marks the M15 R1 shared-custody deployment (same
# parsing semantics as relay/app/config.py: strip + lowercase). The registrar
# is SINGLE-MODE ONLY (see guard_refuse_shared below).
MODE_SINGLE = "single"
MODE_SHARED = "shared"
ENV_RELAY_MODE = "RELAY_MODE"

DEFAULT_DSTACK_SOCKET = "/var/run/dstack.sock"

# Monad gas rules (see README-docker.md "Notes"): generous EIP-1559 fees.
MAX_PRIORITY_FEE_GWEI = 2
MONAD_MAX_FEE_NUM = 3  # maxFeePerGas = baseFee * 3 + priority fee

# Gas: eth_estimateGas then headroom ×1.3 + 30k buffer (e2e/run.py gas_for).
# Flat fallback only if the estimate itself errors — a real revert then
# surfaces at the receipt status check, never silently.
FALLBACK_TX_GAS = 400_000

RECEIPT_TIMEOUT_S = 120.0
RECEIPT_POLL_S = 1.0


class RegistrarError(RuntimeError):
    """Fatal registrar failure — printed to stderr, exit != 0."""


def _require(name: str, missing: list[str]) -> str:
    value = os.environ.get(name)
    if value is None or not value.strip():
        missing.append(name)
        return ""
    return value.strip()


def load_env() -> dict[str, object]:
    """Collect every required variable up front, then parse. Missing env ->
    one clear error listing ALL of them, exit != 0."""
    missing: list[str] = []
    rpc_url = _require("RPC_URL", missing)
    registry_addr_raw = _require("REGISTRY_ADDR", missing)
    endpoint = _require("REGISTRAR_ENDPOINT", missing)
    models_raw = _require("REGISTRAR_MODELS", missing)
    prices_raw = _require("REGISTRAR_PRICES", missing)
    chain_id_raw = _require("CHAIN_ID", missing)
    socket_path = os.environ.get("DSTACK_SOCKET_PATH", "") or DEFAULT_DSTACK_SOCKET

    if missing:
        raise RegistrarError(
            "missing required environment variable(s): "
            + ", ".join(missing)
            + " — refusing to run (no degraded mode)"
        )

    try:
        chain_id = int(chain_id_raw)
    except ValueError as exc:
        raise RegistrarError(f"CHAIN_ID must be an integer, got {chain_id_raw!r}") from exc

    endpoint = endpoint.rstrip("/")
    if not endpoint.startswith("https://"):
        raise RegistrarError(
            f"REGISTRAR_ENDPOINT must be a public https URL, got {endpoint!r}"
        )

    models = [m.strip() for m in models_raw.split(",") if m.strip()]
    if not models:
        raise RegistrarError(
            f"REGISTRAR_MODELS must be a non-empty csv of model names, got {models_raw!r}"
        )

    prices = parse_prices(prices_raw)
    if len(prices) == 1 and len(models) > 1:
        prices = prices * len(models)
    if len(prices) != len(models):
        raise RegistrarError(
            f"REGISTRAR_PRICES ({len(prices)} triple(s)) must be parallel to "
            f"REGISTRAR_MODELS ({len(models)} model(s)) OR a single triple to "
            "broadcast to every model"
        )

    return {
        "rpc_url": rpc_url,
        "chain_id": chain_id,
        "registry_addr": registry_addr_raw,
        "endpoint": endpoint,
        "models": models,
        "prices": prices,
        "socket_path": socket_path,
    }


def parse_prices(raw: str) -> list[tuple[int, int, int]]:
    """Parse "cachedIn:input:output" triples (USDC 6dp native integers, zero
    floats). Rejects anything non-integral — "1.5" is an error, never float
    math."""
    triples: list[tuple[int, int, int]] = []
    for group in raw.split(","):
        parts = [p.strip() for p in group.strip().split(":")]
        if len(parts) != 3 or not all(p.isdigit() for p in parts):
            raise RegistrarError(
                f"REGISTRAR_PRICES entry {group!r} is not three non-negative "
                'integers "cachedIn:input:output" in USDC native units '
                "(6 decimal places, 1 USDC = 1000000; no decimals allowed)"
            )
        triples.append((int(parts[0]), int(parts[1]), int(parts[2])))
    return triples


# ---------------------------------------------------------------------------
# TEE key derivation (mirrors relay/app/config.py _load_tee_seller_key)
# ---------------------------------------------------------------------------


def derive_seller_key(socket_path: str) -> str:
    """Derive the seller key INSIDE the enclave via dstack get_key. The key
    value is never returned to loggers — only the account address derived
    from it is ever printed (in main())."""
    if not os.path.exists(socket_path):
        raise RegistrarError(
            f"dstack socket not found at {socket_path!r} — this registrar must "
            "run inside the dstack CVM (same container host as the relay). "
            "Running it on a laptop/dev box will not work (no TEE key source)."
        )
    try:
        from dstack_sdk import DstackClient  # noqa: PLC0415 (lazy, clearer error)
    except ImportError as exc:
        raise RegistrarError(
            "the 'dstack-sdk' package is not installed — it is part of "
            "relay/requirements.txt and must be present in the image"
        ) from exc

    try:
        result = DstackClient(socket_path).get_key(TEE_KEY_PATH)
        key = result.decode_key()
    except Exception as exc:
        raise RegistrarError(f"dstack get_key({TEE_KEY_PATH!r}) failed: {exc}") from exc

    if not isinstance(key, str) or not key.strip():
        raise RegistrarError("dstack get_key returned no key material")
    key = key.strip()
    if not key.startswith("0x"):
        key = "0x" + key
    if len(key) != 66:
        raise RegistrarError(
            "dstack get_key returned key material of unexpected length "
            "(expected a 0x-prefixed 32-byte private key)"
        )
    return key


# ---------------------------------------------------------------------------
# Registry ABI (v4 face) — minimal fragments, same shapes as
# e2e/run.py REGISTRY_ABI / cli/tokenshare_cli/abis.py: getListing returns
# ONE struct => solc encodes the return as a single wrapped outer tuple
# (flat field outputs misread the outer offset on real chain bytes).
# ---------------------------------------------------------------------------

_PRICE_COMPONENTS = [
    {"name": "cachedIn", "type": "uint256"},
    {"name": "input", "type": "uint256"},
    {"name": "output", "type": "uint256"},
]

REGISTRY_ABI = [
    {
        "type": "function",
        "name": "register",
        "stateMutability": "nonpayable",
        "inputs": [
            {"name": "endpoint", "type": "string"},
            {"name": "models", "type": "string[]"},
            {"name": "prices", "type": "tuple[]", "components": _PRICE_COMPONENTS},
        ],
        "outputs": [],
    },
    {
        "type": "function",
        "name": "deactivate",
        "stateMutability": "nonpayable",
        "inputs": [],
        "outputs": [],
    },
    {
        "type": "function",
        "name": "getListing",
        "stateMutability": "view",
        "inputs": [{"name": "operator", "type": "address"}],
        "outputs": [
            {
                "name": "listing",
                "type": "tuple",
                "components": [
                    {"name": "operator", "type": "address"},
                    {"name": "endpoint", "type": "string"},
                    {"name": "models", "type": "string[]"},
                    {"name": "prices", "type": "tuple[]", "components": _PRICE_COMPONENTS},
                    {"name": "active", "type": "bool"},
                ],
            }
        ],
    },
    # Error faces so a revert at estimate/receipt decodes with a real message.
    {"type": "error", "name": "AlreadyRegistered", "inputs": []},
    {"type": "error", "name": "NotActive", "inputs": []},
    {"type": "error", "name": "EmptyModels", "inputs": []},
    {"type": "error", "name": "LengthMismatch", "inputs": []},
]


# ---------------------------------------------------------------------------
# web3 plumbing
# ---------------------------------------------------------------------------


def connect_web3(rpc_url: str, expected_chain_id: int):
    """Connect to RPC_URL and hard-assert the chain id (AssertionError on
    mismatch — never register on the wrong network)."""
    from web3 import Web3

    w3 = Web3(Web3.HTTPProvider(rpc_url))
    if not w3.is_connected():
        raise RegistrarError(f"RPC {rpc_url!r} is not reachable")
    actual = w3.eth.chain_id
    if actual != expected_chain_id:
        raise RegistrarError(
            f"chainId mismatch: CHAIN_ID={expected_chain_id} but RPC reports {actual}"
        )
    return w3


def send_tx(w3, fn, account) -> str:
    """Sign + send + wait for one function call from `account`. Returns the
    tx hash hex; raises RegistrarError if the receipt status != 1."""
    try:
        # build_transaction fills nonce/chainId and estimates gas
        # (eth_estimateGas) when "gas" is absent; a revert at estimate
        # raises here and falls into the flat fallback below.
        probe = fn.build_transaction({"from": account.address})
        gas = int(probe["gas"]) * 13 // 10 + 30_000
    except Exception as exc:  # noqa: BLE001
        print(
            f"WARNING: gas estimate failed ({exc}); "
            f"falling back to {FALLBACK_TX_GAS} gas",
            flush=True,
        )
        gas = FALLBACK_TX_GAS

    tx = fn.build_transaction(
        {
            "from": account.address,
            "nonce": w3.eth.get_transaction_count(account.address),
            "gas": gas,
            "chainId": w3.eth.chain_id,
        }
    )
    latest = w3.eth.get_block("latest")
    base_fee = latest.get("baseFeePerGas")
    if base_fee is not None:
        # Monad EIP-1559: maxFeePerGas = baseFee*3 + 2 gwei,
        # maxPriorityFeePerGas = 2 gwei (see module docstring).
        tx["maxFeePerGas"] = int(base_fee) * MONAD_MAX_FEE_NUM + MAX_PRIORITY_FEE_GWEI * 10**9
        tx["maxPriorityFeePerGas"] = MAX_PRIORITY_FEE_GWEI * 10**9
        tx.pop("gasPrice", None)
    else:
        tx["gasPrice"] = int(w3.eth.gas_price) * 2

    signed = account.sign_transaction(tx)
    raw = getattr(signed, "raw_transaction", None) or signed.rawTransaction
    tx_hash = w3.eth.send_raw_transaction(raw)
    print(f"tx sent: {tx_hash.hex()} (waiting for receipt...)", flush=True)
    receipt = w3.eth.wait_for_transaction_receipt(
        tx_hash, timeout=RECEIPT_TIMEOUT_S, poll_latency=RECEIPT_POLL_S
    )
    if receipt.get("status", 0) != 1:
        raise RegistrarError(f"tx reverted: {tx_hash.hex()}")
    print(f"tx mined: {tx_hash.hex()} (status=1)")
    return tx_hash.hex()


def get_listing(w3, registry, operator: str) -> tuple:
    """getListing(operator) — web3 unwraps the single wrapped-tuple output,
    so the five-field unpack matches cli/e2e usage."""
    (operator_, endpoint, models, prices, active) = registry.functions.getListing(
        operator
    ).call()
    return operator_, endpoint, list(models), [(int(p[0]), int(p[1]), int(p[2])) for p in prices], active


def print_listing(header: str, listing: tuple) -> None:
    operator_, endpoint, models, prices, active = listing
    print(f"--- {header} ---")
    print(f"operator : {operator_}")
    print(f"active   : {active}")
    print(f"endpoint : {endpoint}")
    print(f"models   : {models}")
    print(f"prices   : {prices}  (cachedIn:input:output, USDC 6dp per 1M tokens)")


def listing_matches(listing: tuple, endpoint: str, models: list[str], prices: list[tuple[int, int, int]]) -> bool:
    _, chain_endpoint, chain_models, chain_prices, active = listing
    return (
        active
        and chain_endpoint == endpoint
        and chain_models == models
        and chain_prices == prices
    )


# ---------------------------------------------------------------------------
# main
# ---------------------------------------------------------------------------


def main() -> int:
    # Fail-fast BEFORE anything else (config parsing, key derivation, RPC):
    # the registrar must never run in a shared-custody CVM (M15 R1 guard).
    guard_refuse_shared()

    cfg = load_env()

    key = derive_seller_key(cfg["socket_path"])  # never printed below
    from eth_account import Account

    try:
        account = Account.from_key(key)
    except Exception as exc:
        raise RegistrarError("derived key material is not a valid private key") from exc
    operator = account.address
    del key  # drop the only reference; nothing else ever touches it

    from web3 import Web3

    print(f"registrar: TEE-derived seller address: {operator}")
    print(f"registrar: derive path {TEE_KEY_PATH!r} (== relay/app/config.py TEE_KEY_PATH)")

    w3 = connect_web3(cfg["rpc_url"], cfg["chain_id"])
    try:
        registry_addr = Web3.to_checksum_address(cfg["registry_addr"])
    except Exception as exc:
        raise RegistrarError(
            f"REGISTRY_ADDR is not a valid 0x address: {cfg['registry_addr']!r}"
        ) from exc
    registry = w3.eth.contract(address=registry_addr, abi=REGISTRY_ABI)
    print(
        f"registrar: RPC chainId={w3.eth.chain_id} registry={registry_addr}"
    )

    listing = get_listing(w3, registry, operator)
    print_listing("current on-chain listing", listing)
    desired = (cfg["endpoint"], cfg["models"], cfg["prices"])

    if listing_matches(listing, *desired):
        print("already OK — on-chain listing matches the desired one; nothing to send")
        return 0

    must_reregister = listing[4]  # active flag of the current listing
    if must_reregister:
        # register() reverts AlreadyRegistered while active — deactivate first.
        print("listing active but different (endpoint/models/prices) — deactivating first")
        send_tx(w3, registry.functions.deactivate(), account)

    print("registering listing ...")
    send_tx(
        w3,
        registry.functions.register(cfg["endpoint"], cfg["models"], cfg["prices"]),
        account,
    )

    final = get_listing(w3, registry, operator)
    print_listing("final on-chain listing", final)
    if not listing_matches(final, *desired):
        raise RegistrarError(
            "final getListing does not match the desired listing — investigate manually"
        )
    print("registrar: done (listing registered and verified)")
    return 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except RegistrarError as exc:
        print(f"registrar: FATAL: {exc}", file=sys.stderr)
        sys.exit(1)
