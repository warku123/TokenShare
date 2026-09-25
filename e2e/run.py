#!/usr/bin/env python3
"""TokenShare end-to-end runner (M5a, BUILD_SPEC §5 / §7).

    python3 e2e/run.py --network base_sepolia     # full-local anvil-fork run
    python3 e2e/run.py --network monad_testnet    # same code, config switch (M5b)

base_sepolia local path = EVERYTHING on this machine:
  1. anvil --fork-url $BASE_SEPOLIA_RPC (public RPC default, env overridable).
     The public RPC URL is a documented network fact (spec §9), never a source
     chain binding. Anvil's PUBLIC default accounts (well-known test mnemonic)
     act as deployer/seller/buyer — valueless test keys, local path ONLY.
  2. forge script Deploy.s.sol --sig run(string) deploys Escrow + Registry
     (+ MockUSDC, since fork accounts hold no real testnet USDC) and writes
     contracts/deployed.json.
  3. Direct web3 (deepwork decision 4: "contract prep via direct web3"):
     deployer mints MockUSDC for the buyer; seller registers a Registry v2
     listing (M9 per-model pricing: Price[] parallel to models[]) with
     endpoint http://127.0.0.1:<relay_port>, the SERVED model FIRST with
     tiered prices paired with the mock OpenAI usage (exact non-zero
     settle) plus a second model whose price is moved via updateModelPrice;
     afterwards getPrice(operator, model) is asserted verbatim per model.
     The FORK path additionally registers two probe models so the Registry
     v4 (M12) removeModel verification can retire a NON-last entry
     (swap-and-pop, ModelRemoved log, getPrice revert for the removed model,
     seller enumeration untouched) and then restore the EXACT pre-probe
     listing via the M2 deactivate→re-register path — before the relay
     starts and the buyer flow reads the listing. The real-chain listing
     stays [served, alt].
     An ACTIVE listing is deactivated first (v2 AlreadyRegistered guard),
     so reruns on a reused deployment re-register cleanly.
  4. e2e/mock_openai.py serves deterministic non-stream JSON + SSE on a free
     port. NOTE: OPENAI_BASE_URL must be the bare host root WITHOUT /v1 — the
     relay (httpx base_url) appends /v1/chat/completions itself. The mock host
     is NOT an official endpoint, so the relay env gets the explicit dev/test
     flag ALLOW_CUSTOM_UPSTREAM=1 (the anti-poisoning gate in
     relay/app/config.py refuses non-official hosts otherwise).
  5. relay/app/main.py runs via uvicorn with the full relay env assembly
     (relay/app/config.py names); OPENAI_API_KEY=dummy passes the existence
     check; logs land in e2e/.relay.log.
  6. Buyer side is driven through the REAL CLI as a subprocess (black box):
     deposit -> lock (short TTL) -> call (auto-locks a NEW paymentId, --max
     explicit because the CLI's cap-based default can exceed the deposit) ->
     refund the short lock after its TTL expires. CLI output must contain the
     model reply, `Settle status: settled` and `Receipt verification: OK`.
     M13 (fork path only): a dedicated short lock is consumed TWICE via the
     CLI-minted stateless bearer API key (`mint-key`) — both calls must be
     200 with X-Receipts, the on-chain SettlePartial logs must accumulate
     exactly the two captures while the payment STAYS Locked, and the refund
     after the TTL must return exactly maxAmount - captured.
  7. On-chain asserts: Escrow payment Settled (state=2), settled amount equals
     the PIN pricing formula, escrow balances moved buyer->seller; M14 (Escrow
     v3 protocol fee): settle / settlePartial emit FeeTaken(paymentId, fee,
     sellerAmount) with fee == amount*FEE_BPS//10000 (the fork deploy injects
     FEE_BPS=100 explicitly), the seller ledger gets amount-fee and the
     feeRecipient (deployer) ledger gets fee; refund txs carry NO FeeTaken and
     credit the buyer IN FULL (zero-fee refund PIN); the M13 partial segment
     reconciles the CUMULATIVE fee across both captures. Refund path leaves
     the payment Refunded (state=3).
  8. All subprocesses are killed; the run prints E2E PASSED (verbatim) on
     success, or E2E FAILED: <reason> with a non-zero exit code otherwise.

monad_testnet path: same functions, no anvil, real RPC from the NETWORKS map.
Deployment + registration are IDENTICAL functions; the run needs real funded
keys (SELLER_PRIVATE_KEY / BUYER_PRIVATE_KEY) and is executed in M5b. Without
keys the script explains the M5b prerequisites and exits 0 (config-ready, not
a failure). Official USDC is used on real chains (Deploy.s.sol reads
USDC_ADDR), so no mint step happens there.

Real-chain upstream (authenticity architecture, 2026-09-23): the relay
defaults to the OFFICIAL upstream from env (OPENAI_BASE_URL + OPENAI_API_KEY,
e.g. Kimi at api.moonshot.cn) with a matching E2E_MODEL (default kimi-k2.6);
E2E_FORCE_MOCK_OPENAI=1 falls back to the local mock (with the relay's
ALLOW_CUSTOM_UPSTREAM=1 dev/test flag). The CLI's --model always uses the
model registered in the seller listing.

Polling per spec §9 (verified): 1s interval, 60s timeout on real chains.
"""

from __future__ import annotations

import argparse
import atexit
import json
import os
import re
import shutil
import socket
import subprocess
import sys
import time
from pathlib import Path
from typing import Any, NoReturn

REPO_ROOT = Path(__file__).resolve().parents[1]
CONTRACTS_DIR = REPO_ROOT / "contracts"
E2E_DIR = REPO_ROOT / "e2e"

# .env auto-load — BEFORE anything reads os.environ (module entry, earliest).
# Values only enter os.environ; nothing is printed (private-key discipline).
sys.path.insert(0, str(Path(__file__).resolve().parent))
from dotenv_loader import load_dotenv  # noqa: E402

load_dotenv()

# ---------------------------------------------------------------------------
# Network config map — the ONE allowed place (besides .env.example comments and
# deployed.json) where public RPC/USDC/chainId values appear. Values verified
# 2026-09-23 and recorded in TokenShare-BUILD_SPEC.md §4/§9 (librarian-1).
# ---------------------------------------------------------------------------
NETWORKS: dict[str, dict[str, Any]] = {
    "base_sepolia": {
        "chain_id": 84532,
        "default_rpc": "https://sepolia.base.org",
        "rpc_env": "BASE_SEPOLIA_RPC",
        "usdc_official": "0x036CbD53842c5426634e7929541eC2318f3dCF7e",
        "anvil": True,  # local fork path (M5a e2e)
    },
    "monad_testnet": {
        "chain_id": 10143,
        "default_rpc": "https://testnet-rpc.monad.xyz",
        "rpc_env": "MONAD_TESTNET_RPC",
        "usdc_official": "0x534b2f3A21130d7a60830c2Df862319e593943A3",
        "anvil": False,  # real testnet path (M5b)
    },
}

# Model served by the mock OpenAI AND registered FIRST in the seller listing.
MOCK_MODEL = "gpt-4o-mini-tokenshare"
# Usage the mock OpenAI always reports (paired with LISTING_PRICES below).
MOCK_USAGE = {"prompt_tokens": 5000, "cached_tokens": 1000, "completion_tokens": 800}
# SERVED model's listing prices (Registry v2, M9: prices are PER MODEL).
# USDC native units per 1M tokens, 6dp (1 USDC = 1e6). PIN formula (verbatim
# relay/app/pricing.py): actual = (cached*PC + (prompt-cached)*PI +
# completion*PO) // 1e6 = (1000*1e6 + 4000*2e6 + 800*3e6) // 1e6
# = 11,400 native = 0.0114 USDC.
LISTING_PRICES = {"cached": 1_000_000, "input": 2_000_000, "output": 3_000_000}
# Second listing model (Registry v2 per-model pricing proof): registered
# alongside the served model with a DIFFERENT triple, then moved via
# updateModelPrice — the buyer never calls it, so it costs only register gas.
ALT_MODEL_SUFFIX = "-alt"
ALT_MODEL_PRICES_INITIAL = {"cached": 500_000, "input": 700_000, "output": 900_000}
ALT_MODEL_PRICES_UPDATED = {"cached": 1_500_000, "input": 2_500_000, "output": 3_500_000}
# Registry v4 (M12) removeModel probe models — FORK PATH ONLY (the fork self-
# manages its deployment, so mutations are safe; the real-chain listing stays
# exactly [served, alt]). The two extra models exist so removeModel can retire
# a NON-last entry and exercise swap-and-pop; the buyer never requests them.
# The probe section removes the alt model (index 1 of 4), asserts the removed
# face + survivor prices + seller enumeration, then restores the EXACT
# pre-probe listing via the M2 deactivate→re-register path BEFORE any later
# flow step (relay start, buyer flow) consumes the listing.
PROBE_MODEL_SUFFIXES = ("-probe1", "-probe2")
PROBE_MODEL_PRICES = {"cached": 400_000, "input": 600_000, "output": 800_000}
# Relay minAmount estimate caps (match relay/CLI defaults; same formula):
# (2e6*200000 + 3e6*32000)//1e6 = 496,000 native = 0.496 USDC <= lock maxAmount.
PROMPT_TOKEN_CAP = 200_000
COMPLETION_TOKEN_CAP = 32_000
# Buyer deposits 500 USDC (mock mint on the fork; faucet-funded on real chains,
# where the amount can be lowered with the E2E_DEPOSIT_USDC env — whole USDC).
BUYER_DEPOSIT_USDC = "500"  # default; overridden by env E2E_DEPOSIT_USDC
# Explicit lock maxAmount for the CLI `call` (human USDC). The CLI's DEFAULT
# lock size is (listing price x token cap) rounded up to whole USDC, which with
# these prices is far above any sane deposit — so the e2e passes --max
# explicitly (CLI help documents exactly this for HTTP 402 / sizing control).
CALL_MAX_USDC = "5"
# Short lock for the refund path: lock 1 USDC with ttl=3s, then refund.
REFUND_MAX_USDC = "1"
REFUND_TTL_S = 3
# M13 partial-settle segment (fork path): a dedicated short lock consumed via
# the CLI-minted bearer API key — two relay calls capture per call while the
# payment STAYS Locked; after the TTL the refund returns maxAmount - captured.
PARTIAL_MAX_USDC = "1"
PARTIAL_TTL_S = 8
PARTIAL_PROMPT = "Explain stateless signed API keys in one sentence."
# The relay flushes captures on-chain asynchronously after each response
# (M13 PIN: per-response async flush / threshold / TTL-window) — poll up to
# this long for the SettlePartial logs before declaring failure.
SETTLE_PARTIAL_POLL_S = 25.0
# M14 (Escrow v3 protocol fee): the fork deploy injects FEE_BPS=<this> into
# the forge env — the contracts lane default is also 100, but it is passed
# EXPLICITLY so the fee asserts below never depend on the lane default.
# Single source of truth for every fee assert: fee == amount * E2E_FEE_BPS
# // 10_000, sellerAmount == amount - fee; refunds are ALWAYS zero-fee
# (buyer credited in full). FEE_RECIPIENT is the deployer (Deploy.s.sol
# default), i.e. anvil account 0 on the fork path.
E2E_FEE_BPS = int(os.environ.get("E2E_FEE_BPS", "100"))
def pin_actual(prices: dict[str, int], prompt_tokens: int, cached_tokens: int,
               completion_tokens: int) -> int:
    """PIN actual — verbatim relay/app/pricing.py compute_actual:
    (cached*priceCachedIn + (prompt-cached)*priceInput + completion*priceOutput)
    // 1e6; all values are USDC native 6dp integers, prices are the REQUESTED
    model's Registry v2 triple."""
    non_cached_prompt = max(0, prompt_tokens - cached_tokens)
    return (
        cached_tokens * prices["cached"]
        + non_cached_prompt * prices["input"]
        + completion_tokens * prices["output"]
    ) // 10**6


def pin_settle_amount(actual: int, max_amount: int) -> int:
    """PIN settle — verbatim relay/app/pricing.py clamp_settle_amount:
    min(actual, maxAmount)."""
    return min(actual, max_amount)


# settle amount expected from MOCK_USAGE x the SERVED model's Registry v2
# triple (LISTING_PRICES), PIN formula clamped by the explicit call lock cap.
EXPECTED_ACTUAL = pin_settle_amount(
    pin_actual(
        LISTING_PRICES,
        MOCK_USAGE["prompt_tokens"],
        MOCK_USAGE["cached_tokens"],
        MOCK_USAGE["completion_tokens"],
    ),
    int(CALL_MAX_USDC) * 10**6,
)

# ---------------------------------------------------------------------------
# anvil PUBLIC default accounts (mnemonic "test test ... junk"). Well-known
# valueless test keys used ONLY on the local fork path — documented in anvil's
# README. Index 0 = deployer; 1 = buyer; 2 = seller. Index 0's key/address are
# pinned below; 1/2 are derived from the mnemonic at runtime (anvil_account).
# Never valid for real networks; real chains take keys from env.
# ---------------------------------------------------------------------------
ANVIL_MNEMONIC = "test test test test test test test test test test test junk"


def step(msg: str) -> None:
    print(f"\n=== {msg}", flush=True)


def fail(reason: str) -> NoReturn:
    print(f"\nE2E FAILED: {reason}", flush=True)
    sys.exit(1)


def pick_free_port(preferred: int | None = None) -> int:
    if preferred is not None:
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
            try:
                s.bind(("127.0.0.1", preferred))
                return preferred
            except OSError:
                pass  # fall through to a random free port
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.bind(("127.0.0.1", 0))
        return int(s.getsockname()[1])


def wait_http(url: str, timeout: float = 60.0, interval: float = 1.0) -> None:
    import httpx

    deadline = time.monotonic() + timeout
    last = ""
    while time.monotonic() < deadline:
        try:
            r = httpx.get(url, timeout=5.0)
            if r.status_code < 500:
                return
            last = f"HTTP {r.status_code}"
        except Exception as exc:  # noqa: BLE001
            last = str(exc)
        time.sleep(interval)
    fail(f"endpoint not ready within {timeout}s: {url} ({last})")


def rpc_chain_id(rpc_url: str) -> int:
    import httpx

    resp = httpx.post(
        rpc_url, json={"jsonrpc": "2.0", "method": "eth_chainId", "params": [], "id": 1}, timeout=10.0
    )
    return int(resp.json()["result"], 16)


class Proc:
    """Tracked subprocess; output goes to a log file, printed on failure."""

    def __init__(self, name: str, cmd: list[str], *, cwd: Path, env: dict[str, str],
                 log_path: Path) -> None:
        self.name = name
        self.log_path = log_path
        self.cmd = cmd
        self.handle = subprocess.Popen(
            cmd,
            cwd=str(cwd),
            env=env,
            stdout=open(log_path, "w"),
            stderr=subprocess.STDOUT,
        )

    def tail(self, lines: int = 40) -> str:
        try:
            return "\n".join(self.log_path.read_text(errors="replace").splitlines()[-lines:])
        except OSError:
            return "(no log)"

    def terminate(self) -> None:
        if self.handle.poll() is None:
            self.handle.terminate()
            try:
                self.handle.wait(timeout=10)
            except subprocess.TimeoutExpired:
                self.handle.kill()
                self.handle.wait(timeout=5)


procs: list[Proc] = []


def start(name: str, cmd: list[str], *, cwd: Path, env: dict[str, str],
          log_path: Path) -> Proc:
    print(f"[start] {name}: {' '.join(cmd[:4])} … (log: {log_path})", flush=True)
    p = Proc(name, cmd, cwd=cwd, env=env, log_path=log_path)
    procs.append(p)
    return p


def cleanup() -> None:
    for p in reversed(procs):
        try:
            p.terminate()
        except Exception:
            pass


def fail_all(reason: str) -> NoReturn:
    cleanup()
    fail(reason)


atexit.register(cleanup)


# ---------------------------------------------------------------------------
# tool discovery (forge/anvil may live in ~/.foundry/bin, not on PATH)
# ---------------------------------------------------------------------------


def foundry_env(env: dict[str, str]) -> dict[str, str]:
    out = dict(env)
    foundry_bin = Path.home() / ".foundry" / "bin"
    if foundry_bin.is_dir():
        out["PATH"] = f"{foundry_bin}{os.pathsep}{out.get('PATH', os.defpath)}"
    return out


def resolve_tool(name: str) -> str:
    path = shutil.which(name) or shutil.which(name, path=str(Path.home() / ".foundry" / "bin"))
    if not path:
        fail(f"{name!r} not found on PATH (install Foundry: curl -L https://foundry.paradigm.xyz | bash)")
    return path


# ---------------------------------------------------------------------------
# anvil well-known account derivation (index via HD path)
# ---------------------------------------------------------------------------


def anvil_account(index: int) -> tuple[str, str]:
    """(address, private_key) for anvil's public default account `index`."""
    from eth_account import Account

    Account.enable_unaudited_hdwallet_features()
    acct = Account.from_mnemonic(ANVIL_MNEMONIC, account_path=f"m/44'/60'/0'/0/{index}")
    return acct.address, "0x" + acct.key.hex()


# ---------------------------------------------------------------------------
# Minimal ABI fragments for direct-web3 contract prep (deepwork decision 4).
# Relay/CLI use their own embedded ABIs; these cover e2e-only reads/writes.
# ---------------------------------------------------------------------------

MOCK_MINT_ABI = [
    {
        "type": "function",
        "name": "mint",
        "stateMutability": "nonpayable",
        "inputs": [{"name": "to", "type": "address"}, {"name": "amount", "type": "uint256"}],
        "outputs": [],
    },
    {
        "type": "function",
        "name": "balanceOf",
        "stateMutability": "view",
        "inputs": [{"name": "account", "type": "address"}],
        "outputs": [{"type": "uint256"}],
    },
]

# Price tuple components — Registry v2 struct Price { uint256 cachedIn;
# uint256 input; uint256 output; } (contracts/src/Registry.sol, M9).
_PRICE_COMPONENTS = [
    {"name": "cachedIn", "type": "uint256"},
    {"name": "input", "type": "uint256"},
    {"name": "output", "type": "uint256"},
]

# Registry v2 (M9 per-model pricing): register takes a Price[] array PARALLEL
# to models[]; per-model reads go through getPrice(operator, model); an
# existing model's price changes via updateModelPrice (operator = caller).
# Registry v3 (M10) adds the on-chain seller directory: sellerCount() +
# getSellers(start, count) (clamped page: start>=len -> empty, count>500 ->
# 500, tail-truncated; append-only, deduped across re-registrations).
# Registry v4 (M12) adds removeModel(model): ONE-model off-listing via
# swap-and-pop (models[]/prices[] same index), guarded by RemoveLastModel()
# when only one model remains (ModelNotFound is checked first), emitting
# ModelRemoved(operator, model). v3 faces and selectors are unchanged.
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
        "name": "updateModelPrice",
        "stateMutability": "nonpayable",
        "inputs": [
            {"name": "model", "type": "string"},
            {"name": "price", "type": "tuple", "components": _PRICE_COMPONENTS},
        ],
        "outputs": [],
    },
    {
        "type": "function",
        "name": "removeModel",
        "stateMutability": "nonpayable",
        "inputs": [{"name": "model", "type": "string"}],
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
        "name": "sellerCount",
        "stateMutability": "view",
        "inputs": [],
        "outputs": [{"name": "count", "type": "uint256"}],
    },
    {
        "type": "function",
        "name": "getSellers",
        "stateMutability": "view",
        "inputs": [
            {"name": "start", "type": "uint256"},
            {"name": "count", "type": "uint256"},
        ],
        "outputs": [{"name": "sellers", "type": "address[]"}],
    },
    {
        "type": "function",
        "name": "getListing",
        "stateMutability": "view",
        "inputs": [{"name": "operator", "type": "address"}],
        # v2 returns Listing memory = ONE struct → solc encodes the return as
        # a single (dynamic) outer tuple: outputs must be the wrapped tuple,
        # NOT the five fields flat (flat heads misread the outer offset).
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
    {
        "type": "function",
        "name": "getPrice",
        "stateMutability": "view",
        "inputs": [{"name": "operator", "type": "address"}, {"name": "model", "type": "string"}],
        "outputs": [{"name": "price", "type": "tuple", "components": _PRICE_COMPONENTS}],
    },
    # v4 removeModel faces: the guard error (one remaining model cannot be
    # removed) and the removal log — web3 decodes both from these entries.
    {
        "type": "error",
        "name": "RemoveLastModel",
        "inputs": [],
    },
    {
        "type": "event",
        "name": "ModelRemoved",
        "anonymous": False,
        "inputs": [
            {"name": "operator", "type": "address", "indexed": True},
            {"name": "model", "type": "string", "indexed": False},
        ],
    },
]

ESCROW_ABI = [
    {
        "type": "function",
        "name": "getPayment",
        "stateMutability": "view",
        "inputs": [{"name": "paymentId", "type": "uint256"}],
        "outputs": [
            {"name": "buyer", "type": "address"},
            {"name": "seller", "type": "address"},
            {"name": "maxAmount", "type": "uint256"},
            {"name": "expiresAt", "type": "uint64"},
            {"name": "state", "type": "uint8"},
        ],
    },
    {
        "type": "function",
        "name": "balances",
        "stateMutability": "view",
        "inputs": [{"name": "account", "type": "address"}],
        "outputs": [{"type": "uint256"}],
    },
    # Escrow v2 (M13 ABI PIN, append-only): partial settlement while Locked —
    # settlePartial accumulates `captured` (<= maxAmount) WITHOUT changing the
    # payment state; after the TTL refund() returns maxAmount - captured.
    {
        "type": "function",
        "name": "settlePartial",
        "stateMutability": "nonpayable",
        "inputs": [
            {"name": "paymentId", "type": "uint256"},
            {"name": "amount", "type": "uint256"},
        ],
        "outputs": [],
    },
    {
        "type": "event",
        "name": "SettlePartial",
        "anonymous": False,
        "inputs": [
            {"name": "paymentId", "type": "uint256", "indexed": True},
            {"name": "amount", "type": "uint256", "indexed": False},
            {"name": "captured", "type": "uint256", "indexed": False},
        ],
    },
    # Escrow v3 (M14 ABI PIN, append-only): every seller credit (the settle
    # top-up and each settlePartial capture) charges fee == amount *
    # FEE_BPS // 10_000 and emits this event; the seller ledger receives
    # sellerAmount and FEE_RECIPIENT receives fee. Refund NEVER emits it
    # (zero-fee refund). Existing event signatures are unchanged.
    {
        "type": "event",
        "name": "FeeTaken",
        "anonymous": False,
        "inputs": [
            {"name": "paymentId", "type": "uint256", "indexed": True},
            {"name": "fee", "type": "uint256", "indexed": False},
            {"name": "sellerAmount", "type": "uint256", "indexed": False},
        ],
    },
    {
        "type": "event",
        "name": "Refunded",
        "anonymous": False,
        "inputs": [
            {"name": "paymentId", "type": "uint256", "indexed": True},
            {"name": "buyer", "type": "address", "indexed": True},
            {"name": "amount", "type": "uint256", "indexed": False},
            {"name": "caller", "type": "address", "indexed": True},
        ],
    },
]


def w3_at(rpc_url: str) -> Any:
    from web3 import Web3

    w3 = Web3(Web3.HTTPProvider(rpc_url))
    if not w3.is_connected():
        fail(f"RPC {rpc_url} is not reachable")
    return w3


# Flat fallback when eth_estimateGas fails (Gate J M3: a multi-model
# Price[] register measured out-of-gas at this old flat default — estimates
# are now the primary path; this is only the WARNING fallback).
DEFAULT_TX_GAS = 400_000


def gas_for(w3: Any, fn: Any, sender: str, fallback: int = DEFAULT_TX_GAS) -> int:
    """Gate J M3: estimate gas for `fn` via web3 (build_transaction's
    eth_estimateGas), then headroom ×1.3 + 30k buffer. On any estimate
    failure (e.g. RPC hiccup) fall back to `fallback` with a WARNING —
    never a silent flat guess."""
    try:
        # build_transaction auto-fills nonce/chainId and estimates gas
        # (eth_estimateGas) when "gas" is absent — a revert at estimate
        # raises here and lands in the fallback below.
        probe = fn.build_transaction({"from": sender})
        estimate = int(probe["gas"])
    except Exception as exc:  # noqa: BLE001
        print(f"WARNING: gas estimate failed ({exc}); falling back to {fallback} gas")
        return fallback
    return int(estimate * 1.3) + 30_000


def send_tx(w3: Any, fn: Any, key: str, gas: int | None = None) -> str:
    """Sign+send+wait a simple function call (anvil auto-mines; real chains
    wait with a 1s poll / 60s timeout per spec §9). `gas=None` (default)
    estimates via gas_for() — the flat 400k default out-of-gas-reverted on a
    multi-model Price[] v2 register (Gate J M3)."""
    from eth_account import Account

    acct = Account.from_key(key)
    if gas is None:
        gas = gas_for(w3, fn, acct.address)
    tx = fn.build_transaction(
        {
            "from": acct.address,
            "nonce": w3.eth.get_transaction_count(acct.address),
            "gas": gas,
            "chainId": w3.eth.chain_id,
        }
    )
    latest = w3.eth.get_block("latest")
    if latest.get("baseFeePerGas") is not None:
        base = int(latest["baseFeePerGas"])
        tx["maxFeePerGas"] = base * 2 + 1_000_000_000
        tx["maxPriorityFeePerGas"] = 1_000_000_000
        tx.pop("gasPrice", None)
    else:
        tx["gasPrice"] = w3.eth.gas_price * 2
    signed = acct.sign_transaction(tx)
    raw = getattr(signed, "raw_transaction", None) or signed.rawTransaction
    tx_hash = w3.eth.send_raw_transaction(raw)
    receipt = w3.eth.wait_for_transaction_receipt(tx_hash, timeout=60.0, poll_latency=1.0)
    if receipt.get("status", 0) != 1:
        fail(f"tx reverted: {tx_hash.hex()}")
    return tx_hash.hex()


# ---------------------------------------------------------------------------
# step 2: forge deploy + deployed.json
# ---------------------------------------------------------------------------


# rev-3 C1: env pins vs reused deployment artifact — (artifact field, env name).
_ENV_ADDR_KEYS: tuple[tuple[str, str], ...] = (
    ("escrow", "ESCROW_ADDR"),
    ("registry", "REGISTRY_ADDR"),
    ("usdc", "USDC_ADDR"),
)


def assert_env_artifact_addresses_agree(env_pins: dict[str, str | None],
                                        artifact: dict[str, Any], network: str) -> None:
    """rev-3 C1: when env pins contract addresses, a same-network reused
    deployment artifact must name the SAME contracts (case-insensitive) —
    a mismatch would otherwise be silently overridden by the artifact and
    the run would retarget the wrong deployment yet print E2E PASSED.
    Unset env fields are skipped (no pin → artifact value wins)."""
    for key, env_name in _ENV_ADDR_KEYS:
        pin = (env_pins.get(key) or "").strip()
        in_artifact = str(artifact.get(key) or "").strip()
        if pin and in_artifact and pin.lower() != in_artifact.lower():
            fail_all(
                f"{network}: {env_name}={pin} disagrees with deployed.json "
                f"{key}={in_artifact} (same chainId) — refusing to silently "
                "retarget the run; fix .env or redeploy"
            )


def deploy_contracts(network: str, rpc_url: str, deployer_addr: str,
                     deployer_key: str, is_fork: bool) -> dict[str, Any]:
    step("[2/8] Deploying contracts via forge script "
         "(USDC_ADDR unset → MockUSDC)" if is_fork else
         "[2/8] Deploying contracts via forge script (official USDC from USDC_ADDR env)")
    forge = resolve_tool("forge")
    env = foundry_env(dict(os.environ))
    if is_fork:
        # Fork/local path always deploys MockUSDC: anvil accounts cannot hold
        # real testnet USDC. Real-chain deploys keep USDC_ADDR (Gate G m1) —
        # official Circle USDC is used there, never a mock with a mint.
        env.pop("USDC_ADDR", None)
        # M14: pin the Escrow v3 protocol fee for THIS run. The contracts lane
        # env default is also 100, but the value is injected explicitly so the
        # fee asserts never depend on the lane default (E2E_FEE_BPS is the
        # single shared constant: deploy env AND every assert).
        env["FEE_BPS"] = str(E2E_FEE_BPS)
    cmd = [
        forge, "script", "script/Deploy.s.sol",
        "--sig", "run(string)", network,
        "--rpc-url", rpc_url,
        "--broadcast",
        "--private-key", deployer_key,
        "--sender", deployer_addr,
        "--force",
    ]
    # Snapshot the pre-deploy artifact BEFORE running forge: Deploy.s.sol
    # writes deployed.json DURING the forge run, so the poll below must only
    # accept a write that differs from this pre-run snapshot (a content-blind
    # poll would return the STALE artifact of a previous run; a post-run
    # snapshot would equal the fresh write and never accept it).
    artifact_path = CONTRACTS_DIR / "deployed.json"
    try:
        pre_deploy_text = artifact_path.read_text()
    except OSError:
        pre_deploy_text = ""
    try:
        result = subprocess.run(cmd, cwd=str(CONTRACTS_DIR), env=env,
                                capture_output=True, text=True, timeout=300)
    except subprocess.TimeoutExpired as exc:
        fail_all(f"forge script deploy timed out: {exc}")
    (E2E_DIR / ".forge_deploy.log").write_text(
        (result.stdout or "") + "\n" + (result.stderr or ""), errors="replace"
    )
    if result.returncode != 0:
        print((result.stdout or "")[-3000:] or (result.stderr or "")[-3000:])
        fail_all(f"forge script deploy exited {result.returncode} (log: e2e/.forge_deploy.log)")

    deadline = time.monotonic() + 120  # real-chain broadcasts are slow; the
    # content-change guard above keeps this correct on the instant fork path.
    while time.monotonic() < deadline:
        try:
            fresh_text = artifact_path.read_text()
            artifact = json.loads(fresh_text)
            if artifact.get("escrow") and fresh_text != pre_deploy_text:
                print(
                    "deployed.json updated: "
                    f"escrow={artifact['escrow']} registry={artifact['registry']} "
                    f"usdc={artifact['usdc']} usdcIsMock={artifact.get('usdcIsMock')} "
                    f"deployer={artifact.get('deployer')}"
                )
                return artifact
        except (OSError, json.JSONDecodeError):
            pass
        time.sleep(0.5)
    fail_all("contracts/deployed.json not written / invalid after deploy")


# ---------------------------------------------------------------------------
# step 3: contract prep via direct web3 (mint + register listing)
# ---------------------------------------------------------------------------


def prepare_contracts(rpc_url: str, relay_port: int, deployed: dict[str, Any],
                      seller_addr: str, seller_key: str, buyer_addr: str,
                      mint_key: str | None, deposit_usdc: str,
                      served_model: str, is_fork: bool) -> None:
    step("[3/8] Contract prep via direct web3: mint mock USDC + register seller listing")
    w3 = w3_at(rpc_url)
    checksum = w3.to_checksum_address

    usdc_is_mock = bool(deployed.get("usdcIsMock"))

    # Fork/local path only: mint mock USDC for the buyer. Real chains use
    # faucet-funded official USDC (faucet.circle.com) — official USDC has no
    # permissionless mint, so minting is skipped when usdcIsMock is false.
    if usdc_is_mock:
        if mint_key is None:
            fail_all("usdcIsMock=true but no deployer key available for minting")
        usdc = w3.eth.contract(address=checksum(deployed["usdc"]), abi=MOCK_MINT_ABI)
        units_needed = int(deposit_usdc) * 10**6
        if int(usdc.functions.balanceOf(checksum(buyer_addr)).call()) < units_needed:
            amount = units_needed * 100  # 100x headroom for locks
            send_tx(w3, usdc.functions.mint(checksum(buyer_addr), amount), mint_key)
        print(f"minted MockUSDC for buyer {buyer_addr}")

    endpoint = os.environ.get("RELAY_PUBLIC_ENDPOINT") or f"http://127.0.0.1:{relay_port}"
    alt_model = served_model + ALT_MODEL_SUFFIX
    # Registry v4 removeModel probe models (fork path only): the real-chain
    # listing stays exactly [served, alt].
    probe_models = [served_model + s for s in PROBE_MODEL_SUFFIXES] if is_fork else []
    models_to_register = [served_model, alt_model] + probe_models
    prices_to_register = (
        [LISTING_PRICES, ALT_MODEL_PRICES_INITIAL] + [PROBE_MODEL_PRICES] * len(probe_models)
    )
    registry = registry_factory(w3, deployed["registry"])
    # Gate J M2: v2 register reverts AlreadyRegistered while a listing is
    # ACTIVE — reruns on a reused deployment (e.g. the monad_testnet artifact)
    # must deactivate first. getListing returns Listing memory (solc wraps
    # the single struct in an outer tuple; web3 unwraps it to 5 fields).
    _op, _ep, _models, _prices, listing_active = registry.functions.getListing(
        checksum(seller_addr)
    ).call()
    if listing_active:
        print("listing ACTIVE — deactivate first (v2 AlreadyRegistered guard)")
        send_tx(w3, registry.functions.deactivate(), seller_key)
    fn = registry_register(w3, deployed["registry"], endpoint,
                           models_to_register, prices_to_register)
    send_tx(w3, fn, seller_key)
    print(
        f"registered v2 listing: operator={seller_addr} endpoint={endpoint} "
        f"models={models_to_register} (Price[] parallel to models[])"
    )

    # Registry v2 per-model update: move ONLY the alt model's triple; the
    # served model's price must stay exactly LISTING_PRICES (M9 semantics).
    send_tx(
        w3,
        registry_update_model_price(w3, deployed["registry"], alt_model, ALT_MODEL_PRICES_UPDATED),
        seller_key,
    )

    # sanity: getListing shape, active flag, parallel per-model prices.
    # (web3 unwraps the single-tuple output — the call yields the 5 fields.)
    listing_operator, listing_endpoint, models, prices, active = registry.functions.getListing(
        checksum(seller_addr)
    ).call()
    expected_models = models_to_register
    expected_prices = (
        [LISTING_PRICES, ALT_MODEL_PRICES_UPDATED] + [PROBE_MODEL_PRICES] * len(probe_models)
    )
    if not active or listing_operator.lower() != checksum(seller_addr).lower():
        fail_all("seller listing not active / operator mismatch after register")
    if list(models) != expected_models:
        fail_all(f"listing models {list(models)} != registered {expected_models}")
    decoded_prices = [decode_price_tuple(p) for p in prices]
    if decoded_prices != expected_prices:
        fail_all(f"listing per-model prices {decoded_prices} != expected {expected_prices}")

    # getPrice(operator, model) must return each model's triple VERBATIM
    # (Registry v2 view; reverts ModelNotFound for an unlisted model).
    for model, expected in zip(expected_models, expected_prices):
        got = decode_price_tuple(registry.functions.getPrice(checksum(seller_addr), model).call())
        if got != expected:
            fail_all(f"getPrice({model!r}) = {got} != expected {expected}")

    # Registry v3 enumeration (M10): the seller directory must hold exactly
    # the seller — and STAY at one entry even when the M2 guard took the
    # deactivate→re-register path (append-only, deduped on re-registration).
    seller_count = int(registry.functions.sellerCount().call())
    first_page = [str(a) for a in registry.functions.getSellers(0, 1).call()]
    if seller_count != 1:
        fail_all(f"sellerCount() = {seller_count} != 1 after register")
    if not first_page or first_page[0].lower() != checksum(seller_addr).lower():
        fail_all(f"getSellers(0, 1) = {first_page} != [{seller_addr}]")
    print(f"listing verified: active endpoint={listing_endpoint_safe(listing_endpoint)} "
          f"getPrice verbatim for {len(expected_models)} models")
    print(f"seller directory verified: sellerCount=1 getSellers(0,1)=[{seller_addr}]")

    # Registry v4 (M12) removeModel verification — fork path only (the fork
    # self-manages its deployment). Restores the complete listing BEFORE the
    # relay starts and the buyer flow reads it.
    if is_fork:
        verify_remove_model(
            w3, registry, seller_addr, seller_key, endpoint,
            served_model, alt_model, probe_models,
        )


def verify_remove_model(w3: Any, registry: Any, seller_addr: str, seller_key: str,
                        endpoint: str, served_model: str, alt_model: str,
                        probe_models: list[str]) -> None:
    """Registry v4 (M12) removeModel verification (fork path only).

    Sequence: remove a NON-LAST model (alt, index 1 of the 4-model listing —
    removing the tail entry would only exercise a plain pop) → swap-and-pop
    must retire exactly that model. Asserts:
      1. the ModelRemoved log (operator + model decoded from the receipt),
      2. getPrice of the REMOVED model reverts (web3 ContractLogicError
         family — the raw ModelNotFound custom-error face),
      3. every SURVIVOR's getPrice stays verbatim,
      4. the v3 seller enumeration is untouched (sellerCount still 1).
    Then retires BOTH probe models (by name — the first swap-and-pop
    reordered the array) and asserts the LAST survivor's removal reverts with
    the RemoveLastModel guard (selector 0x9cf7b72b), before RESTORING the
    exact pre-probe listing via the M2 guard path
    (deactivate → re-register; alt returns with its UPDATED triple), so every
    later step (relay start, CLI call, refund) sees the complete listing.
    """
    step("[3/8] Registry v4 removeModel verification (fork-only): "
         "remove non-last model + restore full listing")
    checksum = w3.to_checksum_address
    all_models = [served_model, alt_model] + probe_models
    restored_prices = (
        [LISTING_PRICES, ALT_MODEL_PRICES_UPDATED] + [PROBE_MODEL_PRICES] * len(probe_models)
    )

    # --- removal: alt is non-last, so the tail model swap-and-pops into its
    # slot; exactly one ModelRemoved log must carry (seller, alt).
    tx_hash = send_tx(w3, registry.functions.removeModel(alt_model), seller_key)
    receipt = w3.eth.get_transaction_receipt(tx_hash)
    removed_logs = registry.events.ModelRemoved().process_receipt(receipt)
    if len(removed_logs) != 1:
        fail_all(f"ModelRemoved logs {len(removed_logs)} != 1 "
                 f"for removeModel({alt_model!r})")
    log_args = removed_logs[0]["args"]
    if str(log_args["operator"]).lower() != checksum(seller_addr).lower():
        fail_all(f"ModelRemoved operator {log_args['operator']} != seller {seller_addr}")
    if log_args["model"] != alt_model:
        fail_all(f"ModelRemoved model {log_args['model']!r} != {alt_model!r}")
    print(f"removeModel OK: ModelRemoved(operator={seller_addr}, model={alt_model}) "
          "decoded from receipt")

    # --- listing shape: exactly the survivors remain (order across the
    # swap-and-pop is an implementation detail — assert the SET, not indices;
    # prices[] must stay parallel to models[]).
    survivors = [served_model] + probe_models
    _op, _ep, models, prices, active = registry.functions.getListing(
        checksum(seller_addr)
    ).call()
    if not active:
        fail_all("listing inactive after removeModel (removeModel must not touch active)")
    if sorted(models) != sorted(survivors):
        fail_all(f"post-remove models {list(models)} != survivors {survivors}")
    if len(prices) != len(survivors):
        fail_all(f"post-remove prices len {len(prices)} != {len(survivors)}")

    # --- survivors keep their exact triples; the removed model is OFF the
    # price book (getPrice reverts with the ModelNotFound custom error, which
    # web3 surfaces as a ContractLogicError-family exception — ModelNotFound
    # is deliberately NOT in this e2e ABI fragment).
    for model in survivors:
        expected = PROBE_MODEL_PRICES if model in probe_models else LISTING_PRICES
        got = decode_price_tuple(registry.functions.getPrice(checksum(seller_addr), model).call())
        if got != expected:
            fail_all(f"survivor getPrice({model!r}) = {got} != expected {expected}")
    from web3.exceptions import ContractLogicError

    selector = w3.keccak(text="ModelNotFound()")[:4].hex()
    try:
        registry.functions.getPrice(checksum(seller_addr), alt_model).call()
    except ContractLogicError as exc:
        revert_text = f"{type(exc).__name__} {exc} {getattr(exc, 'data', '')}"
        if selector not in revert_text.replace("0x", "") and "ModelNotFound" not in revert_text:
            fail_all(f"getPrice({alt_model!r}) reverted with an unexpected face: "
                     f"{revert_text} (expected ModelNotFound selector {selector})")
    else:
        fail_all(f"getPrice({alt_model!r}) did NOT revert after removeModel")
    print(f"getPrice({alt_model!r}) reverts after removeModel (web3 exception, "
          "survivor getPrice verbatim)")

    # --- v3 seller enumeration untouched by a per-model removal.
    seller_count = int(registry.functions.sellerCount().call())
    first_page = [str(a) for a in registry.functions.getSellers(0, 1).call()]
    if seller_count != 1:
        fail_all(f"sellerCount() = {seller_count} != 1 after removeModel (enumeration moved)")
    if not first_page or first_page[0].lower() != checksum(seller_addr).lower():
        fail_all(f"getSellers(0, 1) = {first_page} != [{seller_addr}] after removeModel")
    print("seller enumeration untouched: sellerCount=1 getSellers(0,1)=["
          f"{seller_addr}]")

    # --- v4 RemoveLastModel guard: retire BOTH probe models (BY NAME — the
    # first swap-and-pop reordered the array), then removing the LAST
    # survivor (served) must revert with RemoveLastModel.
    for probe in reversed(probe_models):
        send_tx(w3, registry.functions.removeModel(probe), seller_key)
    print(f"removeModel OK: probes retired ({', '.join(reversed(probe_models))}); "
          f"1 model remains ({served_model})")
    remove_last_selector = w3.keccak(text="RemoveLastModel()")[:4].hex()
    try:
        registry.functions.removeModel(served_model).call({"from": checksum(seller_addr)})
    except ContractLogicError as exc:
        revert_text = f"{type(exc).__name__} {exc} {getattr(exc, 'data', '')}"
        if (remove_last_selector not in revert_text.replace("0x", "")
                and "RemoveLastModel" not in revert_text):
            fail_all(f"removeModel({served_model!r}) on the last survivor reverted with an "
                     f"unexpected face: {revert_text} (expected RemoveLastModel selector "
                     f"{remove_last_selector})")
    else:
        fail_all(f"removeModel({served_model!r}) on the last survivor did NOT revert "
                 "(RemoveLastModel guard missing)")
    print(f"RemoveLastModel guard OK: removeModel of the last survivor "
          f"({served_model!r}) reverts (selector {remove_last_selector})")

    # --- restore: M2 guard semantics (deactivate → re-register). The alt
    # model returns with its UPDATED triple (updateModelPrice ran before the
    # probe), so the restored listing is byte-identical to the pre-probe one.
    send_tx(w3, registry.functions.deactivate(), seller_key)
    send_tx(
        w3,
        registry_register(w3, registry.address, endpoint, all_models, restored_prices),
        seller_key,
    )
    _op, _ep, models, prices, active = registry.functions.getListing(
        checksum(seller_addr)
    ).call()
    if not active or list(models) != all_models:
        fail_all(f"restored listing models {list(models)} != {all_models} (or inactive)")
    decoded_prices = [decode_price_tuple(p) for p in prices]
    if decoded_prices != restored_prices:
        fail_all(f"restored listing prices {decoded_prices} != expected {restored_prices}")
    for model, expected in zip(all_models, restored_prices):
        got = decode_price_tuple(registry.functions.getPrice(checksum(seller_addr), model).call())
        if got != expected:
            fail_all(f"restored getPrice({model!r}) = {got} != expected {expected}")
    if int(registry.functions.sellerCount().call()) != 1:
        fail_all("sellerCount() != 1 after restore")
    print(f"listing restored: {len(all_models)} models, all getPrice verbatim, "
          "sellerCount=1 — downstream steps see the complete listing")


def listing_endpoint_safe(endpoint: str) -> str:
    return endpoint or "(empty)"


def registry_factory(w3: Any, addr: str) -> Any:
    return w3.eth.contract(address=w3.to_checksum_address(addr), abi=REGISTRY_ABI)


def price_tuple(prices: dict[str, int]) -> dict[str, int]:
    """Prices dict {"cached","input","output"} → Registry.Price web3 tuple
    (contract field names, 6dp native integers)."""
    return {
        "cachedIn": int(prices["cached"]),
        "input": int(prices["input"]),
        "output": int(prices["output"]),
    }


def decode_price_tuple(raw: Any) -> dict[str, int]:
    """Registry.Price tuple → {"cached","input","output"} native ints (6dp)."""
    cached_in, price_input, price_output = raw
    return {"cached": int(cached_in), "input": int(price_input), "output": int(price_output)}


def registry_register(w3: Any, addr: str, endpoint: str, models: list[str],
                      prices: list[dict[str, int]]) -> Any:
    """Registry v2 register: Price[] PARALLEL to models[] (M9 per-model
    pricing). The SERVED model is models[0] with LISTING_PRICES; the fork
    path additionally carries the v4 removeModel probe models."""
    return registry_factory(w3, addr).functions.register(
        endpoint,
        list(models),
        [price_tuple(p) for p in prices],
    )


def registry_update_model_price(w3: Any, addr: str, model: str,
                                prices: dict[str, int]) -> Any:
    """Registry v2 updateModelPrice: per-model triple change (operator=caller,
    model must already be listed, listing must be active)."""
    return registry_factory(w3, addr).functions.updateModelPrice(model, price_tuple(prices))


# ---------------------------------------------------------------------------
# buyer side via the CLI subprocess (black box; deepwork decision 4)
# ---------------------------------------------------------------------------


def cli_env(base: dict[str, str], deployed: dict[str, Any], rpc_url: str, chain_id: int,
            buyer_key: str, seller_addr: str) -> dict[str, str]:
    env = dict(base)
    cli_path = str(REPO_ROOT / "cli")
    env["PYTHONPATH"] = cli_path if not env.get("PYTHONPATH") else cli_path + os.pathsep + env["PYTHONPATH"]
    env.update(
        {
            "BUYER_PRIVATE_KEY": buyer_key,
            "RPC_URL": rpc_url,
            "CHAIN_ID": str(chain_id),
            "ESCROW_ADDR": deployed["escrow"],
            "REGISTRY_ADDR": deployed["registry"],
            "USDC_ADDR": deployed["usdc"],
            "SELLER_ADDR": seller_addr,
            "PROMPT_TOKEN_CAP": str(PROMPT_TOKEN_CAP),
            "COMPLETION_TOKEN_CAP": str(COMPLETION_TOKEN_CAP),
            "TX_TIMEOUT_S": "60",
        }
    )
    return env


def run_cli(cmd: list[str], env: dict[str, str], timeout: float = 180.0) -> str:
    result = subprocess.run(
        [sys.executable, "-m", "tokenshare_cli", *cmd],
        cwd=str(REPO_ROOT), env=env, capture_output=True, text=True, timeout=timeout,
    )
    if result.stdout:
        print(result.stdout, end="" if result.stdout.endswith("\n") else "\n")
    if result.stderr:
        print(result.stderr, file=sys.stderr, end="" if result.stderr.endswith("\n") else "\n")
    if result.returncode != 0:
        fail_all(f"CLI {' '.join(cmd)} exited {result.returncode}")
    return result.stdout


# ---------------------------------------------------------------------------
# on-chain reads / asserts
# ---------------------------------------------------------------------------


def warp_chain_past(rpc_url: str, target_ts: int) -> None:
    """anvil-only: advance the chain clock past `target_ts` (evm_increaseTime +
    evm_mine). Used on the local fork path because a fork's block timestamps
    lag the host clock while the refund TTL check is on-chain time."""
    import httpx

    def rpc(method: str, params: list[Any]) -> Any:
        resp = httpx.post(
            rpc_url,
            json={"jsonrpc": "2.0", "method": method, "params": params, "id": 1},
            timeout=10.0,
        )
        body = resp.json()
        if "error" in body:
            fail_all(f"anvil RPC {method} failed: {body['error']}")
        return body.get("result")

    latest = rpc("eth_getBlockByNumber", ["latest", False]) or {}
    chain_now = int(latest.get("timestamp", "0x0"), 16)
    delta = target_ts + 2 - chain_now
    if delta > 0:
        rpc("evm_increaseTime", [delta])
        rpc("evm_mine", [])  # materialize a block with the shifted timestamp
        print(f"anvil time warped +{delta}s (fork clock lags host clock)")


def escrow_balances(rpc_url: str, deployed: dict[str, Any], buyer: str, seller: str,
                    fee_recipient: str | None = None) -> dict[str, int]:
    """Withdrawable Escrow balances snapshot. `fee_recipient` (M14: the
    deployment's FEE_RECIPIENT — the deployer) is tracked only when given, so
    the v3 fee-split asserts can verify its ledger increment."""
    w3 = w3_at(rpc_url)
    escrow = w3.eth.contract(
        address=w3.to_checksum_address(deployed["escrow"]), abi=ESCROW_ABI
    )
    out = {
        "buyer_escrow": int(escrow.functions.balances(w3.to_checksum_address(buyer)).call()),
        "seller_escrow": int(escrow.functions.balances(w3.to_checksum_address(seller)).call()),
    }
    if fee_recipient:
        out["fee_recipient_escrow"] = int(
            escrow.functions.balances(w3.to_checksum_address(fee_recipient)).call()
        )
    return out


def escrow_payment(rpc_url: str, deployed: dict[str, Any], payment_id: int) -> dict[str, Any]:
    w3 = w3_at(rpc_url)
    escrow = w3.eth.contract(
        address=w3.to_checksum_address(deployed["escrow"]), abi=ESCROW_ABI
    )
    raw = escrow.functions.getPayment(int(payment_id)).call()
    # M13 Escrow v2 may add a trailing `captured` output; the v1 5-tuple stays
    # valid (captured=None → partial asserts fall back to SettlePartial logs).
    buyer, seller, max_amount, expires_at, state = raw[:5]
    return {
        "buyer": buyer, "seller": seller, "maxAmount": int(max_amount),
        "expiresAt": int(expires_at), "state": int(state),
        "captured": int(raw[5]) if len(raw) >= 6 else None,
    }


def escrow_supports_fee(rpc_url: str, escrow_addr: str) -> bool:
    """True when the DEPLOYED bytecode exposes the Escrow v3 `FEE_BPS()` public
    immutable getter — the deployment-generation gate for the M14 fee asserts.
    Events have no runtime-bytecode selectors, so the immutable getter is the
    reliable v3 probe (an Escrow v1/v2 deployment simply means the contracts
    lane has not landed for this run yet)."""
    w3 = w3_at(rpc_url)
    selector = w3.keccak(text="FEE_BPS()")[:4]
    code = w3.eth.get_code(w3.to_checksum_address(escrow_addr))
    return bytes(selector) in bytes(code)


def escrow_fee_taken_logs(rpc_url: str, deployed: dict[str, Any], payment_id: int,
                          from_block: int) -> list[dict[str, Any]]:
    """Decoded Escrow v3 FeeTaken logs for `payment_id` since `from_block`
    (paymentId is indexed → exact per-payment filter). Empty list = no fee
    events observed in the range."""
    w3 = w3_at(rpc_url)
    escrow = w3.eth.contract(
        address=w3.to_checksum_address(deployed["escrow"]), abi=ESCROW_ABI
    )
    events = escrow.events.FeeTaken().get_logs(
        argument_filters={"paymentId": int(payment_id)}, from_block=int(from_block)
    )
    return [
        {
            "fee": int(e["args"]["fee"]),
            "sellerAmount": int(e["args"]["sellerAmount"]),
            "tx": e["transactionHash"].hex(),
        }
        for e in events
    ]


def assert_refund_tx_has_no_fee(rpc_url: str, deployed: dict[str, Any],
                                tx_hash: str, label: str) -> None:
    """M14 refund PIN: refunds are ALWAYS zero-fee — the refund transaction
    receipt must carry ZERO FeeTaken logs. Safe against v1/v2 deployments too
    (they never emit the topic, so decoding yields nothing)."""
    w3 = w3_at(rpc_url)
    escrow = w3.eth.contract(
        address=w3.to_checksum_address(deployed["escrow"]), abi=ESCROW_ABI
    )
    receipt = w3.eth.get_transaction_receipt(tx_hash)
    fee_events = escrow.events.FeeTaken().process_receipt(receipt)
    if fee_events:
        fail_all(f"{label}: refund tx {tx_hash} emitted {len(fee_events)} FeeTaken "
                 "log(s) — refund must be zero-fee")


def parse_cli_value(cli_output: str, key: str) -> str:
    for line in cli_output.splitlines():
        if line.startswith(f"{key}: "):
            return line.split(":", 1)[1].strip()
    fail_all(f"no {key!r} line in CLI output:\n{cli_output}")


def parse_receipt_fields(call_out: str) -> dict[str, Any]:
    """Parse the CLI's `Receipt: …` line (printed after successful receipt
    verification; shape from cli/tokenshare_cli/app.py):

        Receipt: paymentId=<pid> prompt=<n> cached=<n> completion=<n>
                 actual=<n> native (= <h> USDC) seller=<addr>
                 upstreamHost=<host> model=<model>
    """
    for line in call_out.splitlines():
        if not line.startswith("Receipt: "):
            continue

        def field(name: str, line: str = line) -> str:
            m = re.search(rf"{name}=([^ ]+)", line)
            if not m:
                fail_all(f"Receipt line missing {name}=: {line}")
            return m.group(1)

        return {
            "payment_id": int(field("paymentId")),
            "prompt": int(field("prompt")),
            "cached": int(field("cached")),
            "completion": int(field("completion")),
            "actual": int(field("actual")),
            "model": field("model"),
        }
    fail_all(f"CLI call output has no Receipt: line:\n{call_out}")


def assert_receipt_amount(receipt: dict[str, Any], model_prices: dict[str, int],
                          max_amount: int, served_model: str) -> None:
    """Receipt actualAmount must equal the PIN settle amount recomputed from
    the receipt's OWN usage x the SERVED model's Registry v2 price triple
    (relay/app/pricing.py: compute_actual + clamp_settle_amount). Valid on the
    mock path AND on a real upstream — the formula is deterministic given the
    reported usage."""
    expected = pin_settle_amount(
        pin_actual(
            model_prices, receipt["prompt"], receipt["cached"], receipt["completion"]
        ),
        max_amount,
    )
    if receipt["actual"] != expected:
        fail_all(
            f"receipt actual {receipt['actual']} != PIN settle {expected} "
            f"(usage p={receipt['prompt']} c={receipt['cached']} "
            f"o={receipt['completion']} modelPrices={model_prices} "
            f"maxAmount={max_amount})"
        )
    if receipt["model"] != served_model:
        fail_all(f"receipt model {receipt['model']!r} != served model {served_model!r}")
    print(f"OK: receipt actualAmount {receipt['actual']} native == PIN settle "
          f"of model {served_model} prices (maxAmount {max_amount})")


def assert_call_output(out: str, prompt: str, expect_mock: bool = True) -> None:
    """CLI output must contain the model reply, settle status and a verified
    receipt (acceptance per BUILD_SPEC §7 M4/M5). The reply-content asserts
    are mock-upstream specific (a real official upstream answers freely)."""
    if expect_mock:
        if "TokenShare mock LLM online" not in out:
            fail_all(f"CLI call output missing mock model reply:\n{out}")
        if f"echo: {prompt}" not in out:
            fail_all(f"CLI call output missing prompt echo:\n{out}")
    if "Settle status: settled" not in out:
        fail_all(f'CLI call output missing "Settle status: settled":\n{out}')
    if "Receipt verification: OK" not in out:
        fail_all(f'CLI call output missing "Receipt verification: OK":\n{out}')


def onchain_settle_asserts(rpc_url: str, deployed: dict[str, Any], payment_id: int,
                           before: dict[str, int], after: dict[str, int],
                           buyer_addr: str, seller_addr: str,
                           fee_recipient_addr: str | None = None,
                           from_block: int = 0,
                           expected_actual: int | None = EXPECTED_ACTUAL) -> dict[str, Any]:
    step("[7/8] On-chain asserts: Escrow Settled + balance direction "
         f"(+ M14 fee split, {E2E_FEE_BPS} bps)")
    payment = escrow_payment(rpc_url, deployed, payment_id)
    if payment["state"] != 2:
        fail_all(f"payment {payment_id} state={payment['state']} (expected 2 = Settled)")
    if payment["buyer"].lower() != buyer_addr.lower():
        fail_all(f"payment buyer {payment['buyer']} != CLI buyer {buyer_addr}")
    if payment["seller"].lower() != seller_addr.lower():
        fail_all(f"payment seller {payment['seller']} != relay seller {seller_addr}")

    actual = before["buyer_escrow"] - after["buyer_escrow"]
    if actual <= 0:
        fail_all(f"settled delta {actual} <= 0 (before={before} after={after})")
    # Exact-amount equality only holds for the deterministic mock usage; on a
    # real official upstream the settled amount follows the real usage.
    if expected_actual is not None and actual != expected_actual:
        fail_all(f"settled delta {actual} != priced expectation {expected_actual} "
                 f"(before={before} after={after})")
    if actual > payment["maxAmount"]:
        fail_all(f"actual {actual} exceeds locked maxAmount {payment['maxAmount']}")

    delta_seller = after["seller_escrow"] - before["seller_escrow"]
    fee_recipient_is_seller = (
        fee_recipient_addr is not None
        and str(fee_recipient_addr).lower() == str(seller_addr).lower()
    )

    # M14 (Escrow v3): the settle charges fee == amount*FEE_BPS//10_000 on the
    # seller credit and emits FeeTaken(paymentId, fee, sellerAmount); the
    # balances ledger credits seller += amount-fee and FEE_RECIPIENT += fee.
    # A v1/v2 deployment (no FEE_BPS() face — contracts lane not landed for
    # this run) never emits the event and credits the seller in full — the
    # legacy full-credit asserts apply there instead.
    if escrow_supports_fee(rpc_url, deployed["escrow"]):
        fee_logs = escrow_fee_taken_logs(rpc_url, deployed, payment_id, from_block)
        if len(fee_logs) != 1:
            fail_all(f"FeeTaken logs {len(fee_logs)} != 1 for settled payment "
                     f"{payment_id} (a v3 settle must emit exactly one)")
        fee = fee_logs[0]["fee"]
        expected_fee = actual * E2E_FEE_BPS // 10_000
        if fee != expected_fee:
            fail_all(f"FeeTaken fee {fee} != amount*{E2E_FEE_BPS}//10000 = {expected_fee} "
                     f"(settled amount {actual})")
        if fee_logs[0]["sellerAmount"] != actual - fee:
            fail_all(f"FeeTaken sellerAmount {fee_logs[0]['sellerAmount']} != "
                     f"amount-fee {actual - fee} (amount {actual}, fee {fee})")
        if fee_recipient_is_seller:
            # FEE_RECIPIENT == seller (real-chain deployer = seller): both
            # credits merge into ONE ledger row — it must show the full amount.
            if delta_seller != actual:
                fail_all(f"seller(=feeRecipient) escrow delta {delta_seller} "
                         f"!= amount {actual}")
        else:
            if delta_seller != actual - fee:
                fail_all(f"seller escrow delta {delta_seller} != amount-fee "
                         f"{actual - fee} (amount {actual}, fee {fee})")
            if "fee_recipient_escrow" not in before or "fee_recipient_escrow" not in after:
                fail_all("FeeTaken present but the feeRecipient balance was not "
                         "tracked (escrow_balances must snapshot the deployer)")
            delta_fee = after["fee_recipient_escrow"] - before["fee_recipient_escrow"]
            if delta_fee != fee:
                fail_all(f"feeRecipient escrow delta {delta_fee} != FeeTaken fee {fee}")
        print(
            f"OK: payment {payment_id} Settled, actual={actual} native "
            f"({actual / 1e6:.6f} USDC) <= maxAmount {payment['maxAmount']}; "
            f"M14 fee split: fee={fee} ({E2E_FEE_BPS} bps), seller +{actual - fee}, "
            f"feeRecipient(={fee_recipient_addr}) +{fee}"
        )
    else:
        print("FEE ASSERTS SKIPPED: deployed Escrow has no FEE_BPS() face "
              "(v1/v2 — contracts lane not landed); asserting the legacy "
              "full seller credit")
        if delta_seller != actual:
            fail_all(
                f"seller escrow delta ({delta_seller}) "
                f"!= buyer delta ({actual})"
            )
        print(
            f"OK: payment {payment_id} Settled, actual={actual} native "
            f"({actual / 1e6:.6f} USDC) <= maxAmount {payment['maxAmount']}"
        )
    return payment


# ---------------------------------------------------------------------------
# M13 partial-settle segment (fork path): CLI mint-key -> bearer calls -> the
# payment stays Locked with cumulative captures -> TTL -> refund the remainder
# ---------------------------------------------------------------------------


def _b64url_pad(segment: str) -> str:
    return segment + "=" * (-len(segment) % 4)


def parse_receipt_header(raw: str) -> dict[str, Any]:
    """Decode an X-Receipt header value (unpadded base64url JSON
    {domain, message, signature}) into a dict."""
    import base64

    return json.loads(base64.urlsafe_b64decode(_b64url_pad(raw.strip())))


def escrow_supports_settle_partial(rpc_url: str, escrow_addr: str) -> bool:
    """True when the DEPLOYED bytecode contains the settlePartial selector —
    the gate for the M13 segment (an Escrow v1 deployment simply means the
    contracts lane has not landed for this run yet)."""
    w3 = w3_at(rpc_url)
    selector = w3.keccak(text="settlePartial(uint256,uint256)")[:4]
    code = w3.eth.get_code(w3.to_checksum_address(escrow_addr))
    return bytes(selector) in bytes(code)


def settle_partial_logs(rpc_url: str, deployed: dict[str, Any], payment_id: int,
                        from_block: int) -> list[Any]:
    """Decoded SettlePartial logs for `payment_id` since `from_block`."""
    w3 = w3_at(rpc_url)
    escrow = w3.eth.contract(
        address=w3.to_checksum_address(deployed["escrow"]), abi=ESCROW_ABI
    )
    events = escrow.events.SettlePartial().get_logs(
        argument_filters={"paymentId": int(payment_id)}, from_block=int(from_block)
    )
    return [
        {
            "amount": int(e["args"]["amount"]),
            "captured": int(e["args"]["captured"]),
            "tx": e["transactionHash"].hex(),
        }
        for e in events
    ]


def partial_settle_flow(deployed: dict[str, Any], rpc_url: str, chain_id: int,
                        base_env: dict[str, str], buyer_key: str, buyer_addr: str,
                        seller_addr: str, served_model: str,
                        fee_recipient_addr: str | None = None) -> None:
    """M13 consumer segment (fork path): one Locked payment consumed TWICE via
    a CLI-minted stateless bearer API key, then refunded for the remainder.

    1. CLI `lock` a short-TTL payment (dedicated to the bearer path).
    2. CLI `mint-key --json` -> tsk1 key + relay base URL (EIP-191 mint
       message: TokenShare API key grant|paymentId|expiry|maxAmount).
    3. httpx Bearer calls x2 against the relay (mock upstream): each must be
       200 with an X-Receipt whose paymentId/seller/actualAmount check out.
    4. On-chain + relay-view asserts: SettlePartial events accumulate exactly
       the two captured amounts while getPayment STILL reports Locked (v1
       getPayment has no captured getter — logs are the on-chain source); the
       relay GET /payment/{id}/usage view matches. M14 (Escrow v3): each
       capture also emits FeeTaken(fee == amount*FEE_BPS//10000) — the CUMULATIVE
       fee of both captures is reconciled against the ledger deltas
       (seller += captured - fees, feeRecipient += fees).
    5. TTL elapses (wall clock + fork warp) -> CLI `refund` returns exactly
       maxAmount - captured (Refunded event amount, payment state Refunded);
       the refund tx carries NO FeeTaken and the buyer is credited in full.

    Skips (clearly labeled, never masquerading as exercised) when the
    Escrow v2 settlePartial face is not deployed yet (contracts lane pending)
    or the relay does not serve the bearer/usage face yet (relay lane
    pending) — the rest of the run still gates E2E PASSED.
    """
    step("[6/8] M13 partial-settle segment: mint-key -> Bearer x2 -> partial captures -> refund remainder")
    if not escrow_supports_settle_partial(rpc_url, deployed["escrow"]):
        print(
            "PARTIAL SKIPPED: deployed Escrow has no settlePartial face "
            "(Escrow v2 / contracts lane not landed) — M13 segment not exercised"
        )
        return
    # M14 fee reconciliation gate: same deployment-generation probe as the
    # full-settle asserts (FEE_BPS() immutable getter). A v2 deployment skips
    # the fee reconciliation with a clear label instead of failing.
    fee_capable = escrow_supports_fee(rpc_url, deployed["escrow"])
    if not fee_capable:
        print("FEE RECONCILIATION SKIPPED: deployed Escrow has no FEE_BPS() face "
              "(v1/v2 — contracts lane not landed); M14 fee asserts not exercised")
    pre_balances = (
        escrow_balances(rpc_url, deployed, buyer_addr, seller_addr, fee_recipient_addr)
        if fee_capable else None
    )
    partial_start_block = int(w3_at(rpc_url).eth.block_number)

    buyer_env = cli_env(base_env, deployed, rpc_url, chain_id, buyer_key, seller_addr)
    disputes_file = E2E_DIR / ".disputes.json"

    lock_out = run_cli(["--disputes-file", str(disputes_file), "lock",
                        "--seller", seller_addr, "--max", PARTIAL_MAX_USDC,
                        "--ttl", str(PARTIAL_TTL_S)], buyer_env)
    partial_pid = int(parse_cli_value(lock_out, "paymentId"))
    expires_at = int(parse_cli_value(lock_out, "expiresAt").split("unix ")[1])
    print(f"locked paymentId {partial_pid} (ttl={PARTIAL_TTL_S}s) for the M13 bearer path")

    # Relay bearer-face probe: the M13 usage view must exist for this fresh
    # payment (captured=0) — a 404/missing field means the relay lane has not
    # landed; skip instead of failing against an old relay.
    mint_out = run_cli(["--disputes-file", str(disputes_file), "mint-key",
                        "--payment-id", str(partial_pid), "--json"], buyer_env)
    try:
        minted = json.loads(mint_out.strip())
    except json.JSONDecodeError:
        fail_all(f"mint-key --json output is not JSON:\n{mint_out}")
    api_key = str(minted.get("apiKey") or "")
    relay_base = str(minted.get("baseUrl") or "").rstrip("/")
    if not api_key.startswith("tsk1.") or not relay_base:
        fail_all(f"mint-key output missing apiKey/baseUrl:\n{mint_out}")
    if int(minted.get("paymentId", -1)) != partial_pid:
        fail_all(f"mint-key paymentId {minted.get('paymentId')} != {partial_pid}")
    if int(minted.get("maxAmount", -1)) != int(PARTIAL_MAX_USDC) * 10**6:
        fail_all(f"mint-key maxAmount {minted.get('maxAmount')} != lock maxAmount")
    print(f"minted API key for payment {partial_pid} (expiry unix {minted.get('expiry')}, "
          f"base {relay_base})")

    import base64 as _b64
    import httpx

    usage_probe = httpx.get(f"{relay_base}/payment/{partial_pid}/usage", timeout=10.0)
    probe_body = usage_probe.json() if "application/json" in usage_probe.headers.get("content-type", "") else {}
    if usage_probe.status_code != 200 or "captured" not in probe_body:
        print(
            f"PARTIAL SKIPPED: relay has no /payment/{{id}}/usage bearer face "
            f"(HTTP {usage_probe.status_code}) — relay lane not landed; M13 segment not exercised"
        )
        return

    # Two Bearer calls — each captures per-call actual while the payment
    # stays Locked; each response carries its own X-Receipt.
    actuals: list[int] = []
    for i in (1, 2):
        resp = httpx.post(
            f"{relay_base}/v1/chat/completions",
            headers={"Authorization": f"Bearer {api_key}"},
            json={"model": served_model,
                  "messages": [{"role": "user", "content": f"{PARTIAL_PROMPT} (call {i})"}]},
            timeout=60.0,
        )
        if resp.status_code != 200:
            fail_all(f"bearer call {i} returned HTTP {resp.status_code}: {resp.text[:300]}")
        receipt_raw = resp.headers.get("X-Receipt")
        if not receipt_raw:
            fail_all(f"bearer call {i} response has no X-Receipt header (headers: "
                     f"{list(resp.headers.keys())})")
        receipt = parse_receipt_header(receipt_raw)
        message = receipt.get("message") or {}
        if int(message.get("paymentId", -1)) != partial_pid:
            fail_all(f"bearer call {i} receipt paymentId {message.get('paymentId')} != {partial_pid}")
        if str(message.get("seller", "")).lower() != seller_addr.lower():
            fail_all(f"bearer call {i} receipt seller {message.get('seller')} != {seller_addr}")
        expected_actual = pin_actual(
            LISTING_PRICES,
            MOCK_USAGE["prompt_tokens"],
            MOCK_USAGE["cached_tokens"],
            MOCK_USAGE["completion_tokens"],
        )
        if int(message.get("actualAmount", -1)) != expected_actual:
            fail_all(f"bearer call {i} receipt actualAmount {message.get('actualAmount')} "
                     f"!= PIN pricing {expected_actual}")
        actuals.append(int(message["actualAmount"]))
        print(f"bearer call {i}: HTTP 200, X-Receipt verified (actualAmount={actuals[-1]} native)")
    captured_total = sum(actuals)

    # On-chain evidence: SettlePartial logs must accumulate exactly the two
    # captures, with the LAST event's captured == the running total — while
    # getPayment still reports Locked (partial settle never finalizes).
    start_block = max(int(w3_at(rpc_url).eth.block_number) - 2, 0)
    deadline = time.monotonic() + SETTLE_PARTIAL_POLL_S
    logs: list[dict[str, Any]] = []
    while time.monotonic() < deadline:
        logs = settle_partial_logs(rpc_url, deployed, partial_pid, start_block)
        if logs and logs[-1]["captured"] >= captured_total:
            break
        time.sleep(1.0)
    if len(logs) < 2:
        fail_all(f"SettlePartial logs for payment {partial_pid}: {len(logs)} < 2 "
                 f"(got {logs}) — relay did not flush the bearer captures on-chain")
    if sum(entry["amount"] for entry in logs) != captured_total:
        fail_all(f"SettlePartial amounts {[e['amount'] for e in logs]} != bearer captures {captured_total}")
    if logs[-1]["captured"] != captured_total:
        fail_all(f"final SettlePartial captured {logs[-1]['captured']} != {captured_total}")
    payment = escrow_payment(rpc_url, deployed, partial_pid)
    if payment["state"] != 1:
        fail_all(f"partial-settled payment {partial_pid} state={payment['state']} "
                 "(expected 1 = Locked — settlePartial must not finalize)")
    if payment["captured"] is not None and payment["captured"] != captured_total:
        fail_all(f"getPayment captured {payment['captured']} != {captured_total}")
    print(f"OK: payment {partial_pid} still Locked with on-chain captured "
          f"{captured_total} native ({len(logs)} SettlePartial events)")

    # M14 (Escrow v3): each capture charged fee == amount*FEE_BPS//10000 and
    # emitted FeeTaken — reconcile the CUMULATIVE fee: per-event exact split,
    # summed fees == ledger movements (seller += captured - fees, feeRecipient
    # += fees) across the whole segment.
    if fee_capable:
        fee_logs = escrow_fee_taken_logs(rpc_url, deployed, partial_pid,
                                         partial_start_block)
        if len(fee_logs) != len(actuals):
            fail_all(f"FeeTaken logs for payment {partial_pid}: {len(fee_logs)} != "
                     f"{len(actuals)} bearer captures (one per settlePartial)")
        for i, (fl, cap) in enumerate(zip(fee_logs, actuals), 1):
            expected_fee = cap * E2E_FEE_BPS // 10_000
            if fl["fee"] != expected_fee:
                fail_all(f"FeeTaken #{i} fee {fl['fee']} != "
                         f"amount*{E2E_FEE_BPS}//10000 = {expected_fee} (capture {cap})")
            if fl["sellerAmount"] != cap - fl["fee"]:
                fail_all(f"FeeTaken #{i} sellerAmount {fl['sellerAmount']} != "
                         f"capture-fee {cap - fl['fee']} (capture {cap})")
        fees = [fl["fee"] for fl in fee_logs]
        total_fee = sum(fees)
        post_balances = escrow_balances(rpc_url, deployed, buyer_addr, seller_addr,
                                        fee_recipient_addr)
        delta_seller = post_balances["seller_escrow"] - pre_balances["seller_escrow"]
        if delta_seller != captured_total - total_fee:
            fail_all(f"partial seller escrow delta {delta_seller} != "
                     f"captured-fee {captured_total - total_fee} "
                     f"(captured {captured_total}, fees {fees})")
        delta_fee = (post_balances["fee_recipient_escrow"]
                     - pre_balances["fee_recipient_escrow"])
        if delta_fee != total_fee:
            fail_all(f"partial feeRecipient escrow delta {delta_fee} != "
                     f"cumulative fee {total_fee} ({fees})")
        print(f"OK: M14 cumulative fee reconciliation: {len(actuals)} captures "
              f"{captured_total} native -> fees {fees} (total {total_fee}, "
              f"{E2E_FEE_BPS} bps each), seller +{captured_total - total_fee}, "
              f"feeRecipient +{total_fee}")

    # Relay-side cumulative view must agree with the chain.
    usage = httpx.get(f"{relay_base}/payment/{partial_pid}/usage", timeout=10.0).json()
    if int(usage.get("captured", -1)) != captured_total:
        fail_all(f"relay usage captured {usage.get('captured')} != {captured_total}")
    max_amount = int(minted.get("maxAmount"))
    if int(usage.get("remaining", -1)) != max_amount - captured_total:
        fail_all(f"relay usage remaining {usage.get('remaining')} != "
                 f"{max_amount - captured_total}")
    print(f"OK: relay usage view captured={captured_total} "
          f"remaining={max_amount - captured_total}")

    # TTL elapses -> refund returns maxAmount - captured exactly.
    wait_s = expires_at - int(time.time()) + 1.5
    if wait_s > 0:
        print(f"waiting {wait_s:.1f}s for lock {partial_pid} TTL to elapse …")
        time.sleep(wait_s)
    if rpc_url.startswith(("http://127.0.0.1", "http://localhost")):
        warp_chain_past(rpc_url, expires_at)
    before_refund = escrow_balances(rpc_url, deployed, buyer_addr, seller_addr,
                                    fee_recipient_addr)
    refund_out = run_cli(["--disputes-file", str(disputes_file), "refund",
                          "--payment-id", str(partial_pid)], buyer_env)
    if f"paymentId: {partial_pid} refunded" not in refund_out:
        fail_all(f"partial refund output missing confirmation:\n{refund_out}")
    refund_tx = parse_cli_value(refund_out, "tx")
    # M14: the refund must be zero-fee — no FeeTaken in its receipt.
    assert_refund_tx_has_no_fee(rpc_url, deployed, refund_tx,
                                f"partial payment {partial_pid}")
    w3 = w3_at(rpc_url)
    escrow = w3.eth.contract(
        address=w3.to_checksum_address(deployed["escrow"]), abi=ESCROW_ABI
    )
    refund_receipt = w3.eth.get_transaction_receipt(refund_tx)
    refunded_events = escrow.events.Refunded().process_receipt(refund_receipt)
    if len(refunded_events) != 1:
        fail_all(f"Refunded events {len(refunded_events)} != 1 for payment {partial_pid}")
    refunded_amount = int(refunded_events[0]["args"]["amount"])
    if refunded_amount != max_amount - captured_total:
        fail_all(f"refund returned {refunded_amount} native, expected "
                 f"maxAmount - captured = {max_amount - captured_total}")
    payment = escrow_payment(rpc_url, deployed, partial_pid)
    if payment["state"] != 3:
        fail_all(f"partial refund: payment {partial_pid} state={payment['state']} "
                 "(expected 3 = Refunded)")
    after_refund = escrow_balances(rpc_url, deployed, buyer_addr, seller_addr,
                                   fee_recipient_addr)
    buyer_credit = after_refund["buyer_escrow"] - before_refund["buyer_escrow"]
    if buyer_credit != refunded_amount:
        fail_all(f"partial refund buyer ledger credit {buyer_credit} != "
                 f"Refunded amount {refunded_amount} (zero-fee refund PIN)")
    print(f"OK: refund returned exactly maxAmount - captured "
          f"({max_amount} - {captured_total} = {refunded_amount} native), "
          f"no FeeTaken, buyer ledger +{buyer_credit}, payment Refunded")


# ---------------------------------------------------------------------------
# orchestration
# ---------------------------------------------------------------------------


def start_anvil(cfg: dict[str, Any], base_env: dict[str, str]) -> tuple[str, int]:
    step("[1/8] anvil fork (public RPC default from spec §9, env-overridable)")
    anvil = resolve_tool("anvil")
    anvil_port = pick_free_port(int(base_env.get("ANVIL_PORT", "8545")))
    fork_rpc = base_env.get(cfg["rpc_env"], cfg["default_rpc"])
    proc = start(
        "anvil", [anvil, "--fork-url", fork_rpc, "--port", str(anvil_port)],
        cwd=REPO_ROOT, env=foundry_env(base_env), log_path=E2E_DIR / ".anvil.log",
    )
    rpc_url = f"http://127.0.0.1:{anvil_port}"
    deadline = time.monotonic() + 90
    chain_id = None
    while time.monotonic() < deadline:
        try:
            chain_id = rpc_chain_id(rpc_url)
            break
        except Exception:
            time.sleep(1.0)
    if chain_id is None:
        print(proc.tail())
        fail_all("anvil did not become ready in 90s (is the fork RPC reachable?)")
    if chain_id != cfg["chain_id"]:
        fail_all(f"anvil fork chainId {chain_id} != {cfg['chain_id']}")
    print(f"anvil up: {rpc_url} (forking {fork_rpc}, chainId {chain_id})")
    return rpc_url, anvil_port


def start_mock(base_env: dict[str, str]) -> int:
    step(f"[4/8] mock OpenAI (deterministic usage {MOCK_USAGE})")
    mock_port = pick_free_port()
    proc = start(
        "mock_openai",
        [sys.executable, str(E2E_DIR / "mock_openai.py"),
         "--port", str(mock_port),
         "--prompt-tokens", str(MOCK_USAGE["prompt_tokens"]),
         "--cached-tokens", str(MOCK_USAGE["cached_tokens"]),
         "--completion-tokens", str(MOCK_USAGE["completion_tokens"])],
        cwd=REPO_ROOT, env=base_env, log_path=E2E_DIR / ".mock_openai.log",
    )
    wait_http(f"http://127.0.0.1:{mock_port}/health", timeout=30)
    print(f"mock OpenAI up: http://127.0.0.1:{mock_port}/v1/chat/completions")
    return mock_port


def start_relay(base_env: dict[str, str], deployed: dict[str, Any], rpc_url: str,
                chain_id: int, relay_port: int, seller_key: str,
                upstream_base_url: str) -> None:
    step(f"[5/8] relay (uvicorn) on port {relay_port}, OPENAI_BASE_URL -> {upstream_base_url}")
    relay_env = dict(base_env)
    # Full relay env assembly — names are verbatim relay/app/config.py ENV_*.
    relay_env.update(
        {
            "RELAY_SELLER_KEY": seller_key,
            "RPC_URL": rpc_url,
            "CHAIN_ID": str(chain_id),
            "ESCROW_ADDR": deployed["escrow"],
            "REGISTRY_ADDR": deployed["registry"],
            "USDC_ADDR": deployed["usdc"],
            "OPENAI_API_KEY": base_env.get("OPENAI_API_KEY") or "dummy-local-e2e-key",
            # httpx base_url appends /v1/chat/completions — the value must NOT
            # end in /v1 (relay default "https://api.openai.com/v1" would double
            # the path; reported as a relay finding for Gate G).
            "OPENAI_BASE_URL": upstream_base_url,
            "FORWARD_MARGIN_S": base_env.get("FORWARD_MARGIN_S", "120"),
            "PORT": str(relay_port),
            "PROMPT_TOKEN_CAP": str(PROMPT_TOKEN_CAP),
            "COMPLETION_TOKEN_CAP": str(COMPLETION_TOKEN_CAP),
        }
    )
    if _relay_upstream_is_mock(upstream_base_url):
        # A mock/localhost upstream is NOT an official endpoint: the relay's
        # startup anti-poisoning gate refuses non-official hosts, so every
        # mock-upstream run must pass the explicit dev/test-only flag.
        relay_env["ALLOW_CUSTOM_UPSTREAM"] = "1"
    start(
        "relay",
        [sys.executable, "-m", "uvicorn", "relay.app.main:app",
         "--host", "127.0.0.1", "--port", str(relay_port), "--log-level", "info"],
        cwd=REPO_ROOT, env=relay_env, log_path=E2E_DIR / ".relay.log",
    )
    wait_http(f"http://127.0.0.1:{relay_port}/health", timeout=60)
    import httpx

    health = httpx.get(f"http://127.0.0.1:{relay_port}/health", timeout=10).json()
    if str(health.get("seller", "")).lower() != seller_key_addr(seller_key).lower():
        fail_all(f"relay health seller={health.get('seller')} != registry seller")
    print(f"relay up: seller={health['seller']} chainId={health['chainId']} "
          f"upstream={upstream_base_url}")


def _relay_upstream_is_mock(upstream_base_url: str) -> bool:
    """True when the relay upstream is the local e2e mock (never an official
    provider host). Official hosts start with https:// (loopback mocks can't)."""
    return not upstream_base_url.startswith("https://")


def _normalize_base_url(raw: str) -> str:
    """Host-root form, mirroring relay/app/config.py normalization."""
    url = raw.strip().rstrip("/")
    if url.endswith("/v1"):
        url = url[: -len("/v1")]
    return url or raw.strip()


def seller_key_addr(seller_key: str) -> str:
    from eth_account import Account

    return Account.from_key(seller_key).address


def buyer_flow(deployed: dict[str, Any], rpc_url: str, chain_id: int,
               base_env: dict[str, str], buyer_key: str, buyer_addr: str,
               seller_addr: str, deposit_usdc: str, served_model: str,
               upstream_is_mock: bool, fee_recipient_addr: str | None = None) -> int:
    step("[6/8] Buyer flow via CLI subprocess: deposit -> lock -> call -> refund (expired)")
    buyer_env = cli_env(base_env, deployed, rpc_url, chain_id, buyer_key, seller_addr)
    disputes_file = E2E_DIR / ".disputes.json"

    deposit_out = run_cli(["--disputes-file", str(disputes_file), "deposit",
                           "--amount", deposit_usdc], buyer_env)
    if "deposited:" not in deposit_out:
        fail_all(f"CLI deposit output missing 'deposited:' line:\n{deposit_out}")

    # Separate short-TTL lock that the refund path will exercise (payment stays
    # Locked — the call below locks its own NEW paymentId per app.py default).
    lock_out = run_cli(["--disputes-file", str(disputes_file), "lock",
                        "--seller", seller_addr, "--max", REFUND_MAX_USDC,
                        "--ttl", str(REFUND_TTL_S)], buyer_env)
    refund_pid = int(parse_cli_value(lock_out, "paymentId"))
    expires_at = int(parse_cli_value(lock_out, "expiresAt").split("unix ")[1])
    print(f"locked paymentId {refund_pid} (ttl={REFUND_TTL_S}s) for the refund path")

    # Snapshot + block marker before the paid call: the settle must move exactly
    # EXPECTED_ACTUAL out of the buyer's escrow balance into the seller's
    # (minus the M14 protocol fee) + the feeRecipient's ledger. from_block is
    # the FeeTaken log-search boundary (the settle txs all mine after it).
    before = escrow_balances(rpc_url, deployed, buyer_addr, seller_addr,
                             fee_recipient_addr)
    pre_call_block = int(w3_at(rpc_url).eth.block_number)

    prompt = "Explain EIP-712 receipts in one sentence."
    call_out = run_cli(
        ["--disputes-file", str(disputes_file), "call", prompt,
         "--seller", seller_addr, "--model", served_model, "--max", CALL_MAX_USDC],
        buyer_env,
    )
    assert_call_output(call_out, prompt, expect_mock=upstream_is_mock)
    settle_pid = int(parse_cli_value(call_out, "paymentId"))
    print(f"call used paymentId {settle_pid} (auto-locked, settled)")
    receipt = parse_receipt_fields(call_out)

    # On-chain asserts for the settled payment. Exact EXPECTED_ACTUAL equality
    # only applies to the deterministic mock usage; a real official upstream
    # settles from its real usage (still asserted: Settled, buyer->seller flow).
    payment = onchain_settle_asserts(rpc_url, deployed, settle_pid, before,
                                     escrow_balances(rpc_url, deployed, buyer_addr,
                                                     seller_addr, fee_recipient_addr),
                                     buyer_addr, seller_addr,
                                     fee_recipient_addr=fee_recipient_addr,
                                     from_block=pre_call_block,
                                     expected_actual=EXPECTED_ACTUAL if upstream_is_mock else None)

    # Receipt actualAmount (M9 v2): recompute the PIN settle from the receipt's
    # own usage x the SERVED model's Registry v2 triple and demand equality.
    assert_receipt_amount(receipt, LISTING_PRICES, payment["maxAmount"], served_model)
    if upstream_is_mock:
        expected_usage = (MOCK_USAGE["prompt_tokens"], MOCK_USAGE["cached_tokens"],
                          MOCK_USAGE["completion_tokens"])
        got_usage = (receipt["prompt"], receipt["cached"], receipt["completion"])
        if got_usage != expected_usage:
            fail_all(f"mock receipt usage {got_usage} != deterministic {expected_usage}")

    # Refund path (M4): the CLI pre-check uses wall-clock time, the contract
    # uses block.timestamp — and an anvil FORK's chain clock lags the host
    # clock (it only advances when blocks are mined). So: wait out the wall
    # clock for the CLI, then warp the local chain past the expiry (fork only;
    # real chains simply wait).
    wait_s = expires_at - int(time.time()) + 1.5
    if wait_s > 0:
        print(f"waiting {wait_s:.1f}s for lock {refund_pid} TTL to elapse …")
        time.sleep(wait_s)
    if rpc_url.startswith(("http://127.0.0.1", "http://localhost")):
        warp_chain_past(rpc_url, expires_at)
    before_refund = escrow_balances(rpc_url, deployed, buyer_addr, seller_addr,
                                    fee_recipient_addr)
    refund_out = run_cli(["--disputes-file", str(disputes_file), "refund",
                          "--payment-id", str(refund_pid)], buyer_env)
    if f"paymentId: {refund_pid} refunded" not in refund_out:
        fail_all(f"CLI refund output missing confirmation:\n{refund_out}")
    refund_tx = parse_cli_value(refund_out, "tx")
    # M14: refund is ALWAYS zero-fee — no FeeTaken may appear in its receipt,
    # and the buyer's ledger must be credited the FULL refunded amount.
    assert_refund_tx_has_no_fee(rpc_url, deployed, refund_tx,
                                f"payment {refund_pid}")
    w3 = w3_at(rpc_url)
    escrow = w3.eth.contract(
        address=w3.to_checksum_address(deployed["escrow"]), abi=ESCROW_ABI
    )
    refund_receipt = w3.eth.get_transaction_receipt(refund_tx)
    refunded_events = escrow.events.Refunded().process_receipt(refund_receipt)
    if len(refunded_events) != 1:
        fail_all(f"Refunded events {len(refunded_events)} != 1 for payment {refund_pid}")
    refunded_amount = int(refunded_events[0]["args"]["amount"])
    after_refund = escrow_balances(rpc_url, deployed, buyer_addr, seller_addr,
                                   fee_recipient_addr)
    buyer_credit = after_refund["buyer_escrow"] - before_refund["buyer_escrow"]
    if buyer_credit != refunded_amount:
        fail_all(f"refund buyer ledger credit {buyer_credit} != Refunded amount "
                 f"{refunded_amount} (zero-fee refund PIN)")
    payment = escrow_payment(rpc_url, deployed, refund_pid)
    if payment["state"] != 3:
        fail_all(f"refund: payment {refund_pid} state={payment['state']} (expected 3 = Refunded)")
    print(f"OK: payment {refund_pid} Refunded — no FeeTaken, buyer ledger "
          f"+{buyer_credit} native (M4 refund path + M14 zero-fee refund verified on-chain)")
    return settle_pid


def run(network: str) -> str | None:
    """Run the full e2e pipeline. Returns None when the flow really completed;
    returns a short skip reason for config-not-ready cases (main() prints
    `E2E SKIPPED: <reason>` and exits 0 — NEVER `E2E PASSED`)."""
    cfg = NETWORKS[network]
    base_env = dict(os.environ)

    if cfg["anvil"]:
        # ----- local fork path ------------------------------------------------
        rpc_url, _anvil_port = start_anvil(cfg, base_env)
        # Index roles: 0 = deployer (forge script), 1 = buyer (CLI), 2 = seller
        # (relay + Registry listing). All derived from anvil's public default
        # mnemonic at runtime — zero private-key literals in source.
        deployer_addr, deployer_key = anvil_account(0)
        seller_addr, seller_key = anvil_account(2)
        buyer_addr, buyer_key = anvil_account(1)
        print("using anvil public test accounts (NO real assets): "
              f"deployer={deployer_addr} seller={seller_addr} buyer={buyer_addr}")
        deployed = deploy_contracts(network, rpc_url, deployer_addr, deployer_key, is_fork=True)
    else:
        # ----- real-chain path (M5b): keys + RPC from env ---------------------
        rpc_url = base_env.get(cfg["rpc_env"], cfg["default_rpc"])
        seller_key = base_env.get("SELLER_PRIVATE_KEY")
        buyer_key = base_env.get("BUYER_PRIVATE_KEY")
        if not seller_key or not buyer_key:
            print(
                f"[skip] {network}: real-chain e2e is ready but not runnable yet "
                "(M5b). Prerequisites:\n"
                "  1. put SELLER_PRIVATE_KEY / BUYER_PRIVATE_KEY in .env\n"
                "  2. fund both addresses: faucet.monad.xyz (MON gas) + "
                "faucet.circle.com (USDC)\n"
                "  3. deploy:\n"
                f"     USDC_ADDR={cfg['usdc_official']} forge script script/Deploy.s.sol "
                f"--sig run(string) {network} --rpc-url {rpc_url} --broadcast "
                "(from contracts/)\n"
                "  4. export RELAY_PUBLIC_ENDPOINT=<public URL of your relay> and rerun\n"
                "  5. optional: lower the buyer deposit to your funded balance with\n"
                "     export E2E_DEPOSIT_USDC=<whole USDC> (default 500)\n"
                "Config is ready — nothing else changes (same code path)."
            )
            return (f"{network} needs SELLER_PRIVATE_KEY + BUYER_PRIVATE_KEY in env "
                    "(M5b: fund via faucet.monad.xyz + faucet.circle.com, deploy, then rerun)")
        from eth_account import Account as _Account

        seller_addr = _Account.from_key(seller_key).address
        buyer_addr = _Account.from_key(buyer_key).address
        deployer_key = seller_key  # deployer on real chains = seller account
        deployed_path = CONTRACTS_DIR / "deployed.json"
        # rev-3 C1: explicit env pins must never be silently overridden by
        # the deployment artifact (neither by reuse below nor by a fresh
        # deploy) — every path that would supersede a pin fail-fasts instead.
        env_pins: dict[str, str | None] = {
            "escrow": base_env.get("ESCROW_ADDR"),
            "registry": base_env.get("REGISTRY_ADDR"),
            "usdc": base_env.get("USDC_ADDR"),
        }
        env_pinned = bool(env_pins["escrow"] and env_pins["registry"])
        if env_pinned:
            deployed = {
                "escrow": env_pins["escrow"],
                "registry": env_pins["registry"],
                "usdc": env_pins["usdc"] or cfg["usdc_official"],
                "usdcIsMock": False,
            }
        else:
            try:
                deployed = json.loads(deployed_path.read_text())
            except OSError:
                print(
                    f"[skip] {network}: no ESCROW_ADDR/REGISTRY_ADDR in env and "
                    "contracts/deployed.json is missing. Deploy first:\n"
                    f"  cd contracts && USDC_ADDR={cfg['usdc_official']} "
                    f"forge script script/Deploy.s.sol --sig run(string) {network} "
                    f"--rpc-url {rpc_url} --broadcast --private-key <SELLER key>"
                )
                return ("no ESCROW_ADDR/REGISTRY_ADDR in env and contracts/deployed.json "
                        f"is missing — deploy against {network} first")
            if deployed.get("network") != network:
                print(
                    f"[skip] {network}: contracts/deployed.json holds network="
                    f"{deployed.get('network')!r}. Re-run the deploy against {network}:\n"
                    f"  cd contracts && USDC_ADDR={cfg['usdc_official']} forge script "
                    f"script/Deploy.s.sol --sig run(string) {network} --rpc-url {rpc_url} "
                    "--broadcast --private-key <SELLER key>"
                )
                return ("contracts/deployed.json holds network="
                        f"{deployed.get('network')!r}, not {network} — re-run the deploy "
                        "against this network")
        # Deploy is skipped when an artifact for this network already exists
        # (M5b pre-deploy); otherwise deploy now with the seller key.
        try:
            existing = json.loads(deployed_path.read_text())
        except (OSError, json.JSONDecodeError):
            existing = {}
        if existing.get("network") == network and existing.get("escrow"):
            if env_pinned:
                # rev-3 C1: the same-chainId artifact is about to be reused —
                # it must name the SAME contracts the env pinned, else the
                # reuse below would silently retarget the run.
                assert_env_artifact_addresses_agree(env_pins, existing, network)
            if (
                not cfg["anvil"]
                and not env_pinned  # C1: env pins are never superseded by a redeploy
                and existing.get("usdcIsMock")
                and os.environ.get("USDC_ADDR")
            ):
                # A previous run left a MockUSDC deployment on a REAL chain
                # while the config now names official USDC — reusing it would
                # guarantee a mint failure (real chains have no mint key).
                # Redeploy against the configured official USDC instead.
                print(
                    "[2/8] redeploying: existing deployed.json has usdcIsMock=true "
                    f"for {network} but USDC_ADDR is set — stale mock artifact"
                )
                deployed = deploy_contracts(
                    network, rpc_url, seller_addr, deployer_key, is_fork=False
                )
            else:
                print(f"[2/8] contracts already deployed for {network}: {existing['escrow']}")
                deployed = existing
        else:
            if "USDC_ADDR" not in os.environ:
                print("WARNING: USDC_ADDR unset for a REAL network — Deploy.s.sol will "
                      "deploy a MockUSDC there. For M5b set USDC_ADDR="
                      f"{cfg['usdc_official']} (official Circle USDC) in env.")
            deployed = deploy_contracts(network, rpc_url, seller_addr, deployer_key, is_fork=False)

    # Deposit amount: M5b faucet-funded balances may be small — override with
    # E2E_DEPOSIT_USDC (whole USDC, default 500).
    deposit_usdc = base_env.get("E2E_DEPOSIT_USDC", BUYER_DEPOSIT_USDC)

    # Deployed chain must match the configured chain (fork or real).
    remote_chain = rpc_chain_id(rpc_url)
    expected_chain = int(deployed.get("chainId") or cfg["chain_id"])
    if remote_chain != expected_chain:
        fail_all(f"RPC chainId {remote_chain} != deployed chainId {expected_chain}")

    # Ports: relay prefers 8787 (PIN default), mock gets a random free port.
    relay_port = pick_free_port(int(base_env.get("RELAY_PORT", "8787")))
    # Upstream decision (authenticity architecture, 2026-09-23):
    #   - the anvil fork path ALWAYS uses the local mock (a fork run must
    #     never spend real quota); the mock host is not an official endpoint,
    #     so the relay gets the dev/test-only ALLOW_CUSTOM_UPSTREAM=1;
    #   - the real-chain path defaults to the OFFICIAL upstream from env
    #     (OPENAI_BASE_URL + OPENAI_API_KEY, e.g. Kimi at api.moonshot.cn) and
    #     only falls back to the mock when E2E_FORCE_MOCK_OPENAI=1 is set
    #     explicitly (same escape flag injected for the relay).
    force_mock = base_env.get("E2E_FORCE_MOCK_OPENAI", "").strip() == "1"
    use_mock = bool(cfg["anvil"]) or force_mock
    if use_mock:
        upstream_base_url = f"http://127.0.0.1:{start_mock(base_env)}"
        served_model = MOCK_MODEL
    else:
        upstream_base_url = _normalize_base_url(base_env.get("OPENAI_BASE_URL", ""))
        if not upstream_base_url:
            fail_all(
                "official-upstream e2e needs OPENAI_BASE_URL in env (e.g. "
                "https://api.moonshot.cn) — or set E2E_FORCE_MOCK_OPENAI=1 to run "
                "against the local mock"
            )
        if not (base_env.get("OPENAI_API_KEY") or "").strip():
            fail_all(
                "official-upstream e2e needs OPENAI_API_KEY in env — or set "
                "E2E_FORCE_MOCK_OPENAI=1 to run against the local mock"
            )
        # Must be an official-plan model matching the upstream provider; the
        # relay enforces both at startup and per request.
        served_model = (base_env.get("E2E_MODEL") or "kimi-k2.6").strip()
    print(f"upstream: {upstream_base_url} (mock={use_mock}) model={served_model}")

    # M14 fee recipient: the deployment's FEE_RECIPIENT is the DEPLOYER
    # (Deploy.s.sol default) — anvil account 0 on the fork path; on real chains
    # the deployer IS the seller account (deployer_key = seller_key), so the
    # fee credit merges into the seller ledger row (handled by the asserts).
    fee_recipient_addr = deployer_addr if cfg["anvil"] else seller_addr

    prepare_contracts(rpc_url, relay_port, deployed, seller_addr, seller_key,
                      buyer_addr, mint_key=deployer_key if cfg["anvil"] else None,
                      deposit_usdc=deposit_usdc, served_model=served_model,
                      is_fork=cfg["anvil"])
    start_relay(base_env, deployed, rpc_url, int(deployed.get("chainId") or cfg["chain_id"]),
                relay_port, seller_key, upstream_base_url)
    buyer_flow(deployed, rpc_url, int(deployed.get("chainId") or cfg["chain_id"]),
               base_env, buyer_key, buyer_addr, seller_addr, deposit_usdc,
               served_model, use_mock, fee_recipient_addr=fee_recipient_addr)
    if cfg["anvil"]:
        # M13 consumer segment (fork path ONLY): stateless bearer API key,
        # per-call partial captures on a still-Locked payment, then refund the
        # remainder after the TTL. Requires the Escrow v2 settlePartial face +
        # the relay bearer/usage face (skips with a clear label otherwise).
        partial_settle_flow(deployed, rpc_url,
                            int(deployed.get("chainId") or cfg["chain_id"]),
                            base_env, buyer_key, buyer_addr, seller_addr,
                            served_model, fee_recipient_addr=fee_recipient_addr)
    return None


def main() -> None:
    parser = argparse.ArgumentParser(description="TokenShare e2e runner")
    parser.add_argument("--network", required=True, choices=sorted(NETWORKS))
    args = parser.parse_args()

    step(f"TokenShare E2E — network={args.network} repo={REPO_ROOT}")
    try:
        skip_reason = run(args.network)
    except SystemExit:
        raise
    except KeyboardInterrupt:
        cleanup()
        fail("interrupted")
    except Exception as exc:  # noqa: BLE001
        import traceback

        traceback.print_exc()
        cleanup()
        fail(f"unexpected error: {exc}")
    cleanup()
    if skip_reason is not None:
        # Config-not-ready (e.g. real-chain keys missing) — a skip must NEVER
        # masquerade as a passed acceptance run (Gate G R1).
        print(f"\nE2E SKIPPED: {skip_reason}", flush=True)
        return
    print("\nE2E PASSED", flush=True)


if __name__ == "__main__":
    main()
