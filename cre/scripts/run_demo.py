#!/usr/bin/env python3
"""TokenShare CRE demo runner — compact, safe, real-testnet (Monad 10143).

Stages (argparse --stage, default NONE — read-only unless --broadcast):
  preflight  READ-ONLY: Escrow p5 state + relay receipt verify + evidence notes
  deploy     ReceiptAnchor deployment (dry-read unless --broadcast)
  finalize   seller settle(5, 918) (dry-read unless --broadcast)
  readback   READ-ONLY: verify an actual anchor broadcast tx (--anchor/--tx)

Safety invariants (hard):
  - dry/read-only is the default; NO tx is ever sent without --broadcast.
  - SELLER_PRIVATE_KEY is loaded in-process only (e2e/dotenv_loader.py,
    pure stdlib, never printed/argv/logged); no subprocess ever sees it.
  - evidence JSON (public facts only) lands in the approved
    /private/var/folders/…/opencode/cre-demo/ dir — never secrets.
  - finalize is EXACT: settle(5, 918), zero topup/fee; any gate change →
    abort; estimate failure → abort; no fallback, no raised amount; the
    exact payment gates are re-read AFTER gas estimate, immediately before
    sign/send (ora37 F6).
  - alreadySettled is never fabricated: only a saved, re-verified on-chain
    settle tx is accepted, and replay must redo the historical trigger-block
    prices/formula (3e6/6e6/9e6 → 918), the post-state asserts AND the
    pre-settle receipt snapshot equality (raw bytes). A missing snapshot
    BLOCKS acceptance — never printed as FINALIZE PASSED (ora37 F3).
    Replay evidence never overwrites prior evidence gates (merge only).
  - deploy keeps an EXACT runtime-code match: the artifact's compiler
    immutableReferences slots are patched with the pinned forwarder and
    compared byte-for-byte with on-chain code — NO prefix acceptance
    (ora37 F1). The pending deployment tx hash is persisted BEFORE wait and
    reconciled before any new deploy (no duplicate deploy on failure).
  - bytes→hex is canonical via hex0x() everywhere (bytes32 hashes, topics,
    tx hashes) — str(HexBytes)/bytes.hex()-prefix mixing is usage error
    (ora37 F4).
  - relay receipt responses are kept as RAW response bytes; pre/post and
    replay equality is RAW-BYTE equality PLUS strict-JSON parsing
    (duplicate keys rejected) PLUS the full semantic/EIP-712 checks
    (ora37 F5). HTTP is always the live configured GET; 404/other → STOP,
    no substitution, no fabrication.

Network facts (public, pinned): RPC https://testnet-rpc.monad.xyz, chain
10143, Escrow 0xe4D5…514c, Registry 0xeD34…2254, mock forwarder 0xB9F7…192,
relay host c30652…phala.network, trustedSigner 0x49CA…fb2D.
"""

from __future__ import annotations

import argparse
import hashlib
import importlib.util
import json
import os
import re
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path
from typing import Any

# --------------------------------------------------------------------------
# Paths / constants (public values; pinned — no env overrides)
# --------------------------------------------------------------------------

REPO_ROOT = Path(__file__).resolve().parents[2]
CRE_DIR = REPO_ROOT / "cre"
E2E_DIR = REPO_ROOT / "e2e"
ARTIFACT_PATH = CRE_DIR / "contracts" / "out" / "ReceiptAnchor.sol" / "ReceiptAnchor.json"
RECEIPT_MODULE_PATH = REPO_ROOT / "relay" / "app" / "receipt.py"
DOTENV_LOADER_PATH = E2E_DIR / "dotenv_loader.py"

# Approved public-evidence directory (parent-approved lane; never secrets).
EVIDENCE_DIR = Path("/private/var/folders/xn/hs1mq79s29v0j1c1z5mmsf_40000gn/T/opencode/cre-demo")
EVIDENCE_PREFLIGHT = EVIDENCE_DIR / "preflight.json"
EVIDENCE_DEPLOY = EVIDENCE_DIR / "deploy.json"
EVIDENCE_DEPLOY_PENDING = EVIDENCE_DIR / "deploy-pending.json"
EVIDENCE_FINALIZE = EVIDENCE_DIR / "finalize.json"
EVIDENCE_READBACK = EVIDENCE_DIR / "readback.json"

RPC_URL = "https://testnet-rpc.monad.xyz"
CHAIN_ID = 10143
ESCROW = "0xe4D5Eb0dBDB6DB8063C07ECF7EDFcCdDB9Ad514c"
REGISTRY = "0xeD347cDc1761750E20C024459b38dedFb1462254"
FORWARDER = "0xB9F79d863261869B234c481D1f9A7af84AeAd192"
FORWARDER_BYTES = bytes.fromhex(FORWARDER[2:])
RELAY_URL = "https://c30652f4833465adda9bdc7ac557e8b45e8e66c6-8787.dstack-pha-prod5.phala.network"
TRUSTED_SIGNER = "0x49CA3eADB3b8F23623f735918A6F2b945F60fb2D"

PAYMENT_ID = 5
EXPECTED_BUYER = "0xA0ffF55bfa23AF58970f7C4b30DAba3e985eFef7"
EXPECTED_SELLER = "0x38c26E1782b7E6F656Aa35EbF473e2ff0D718Dd6"
EXPECTED_MAX = 1_500_000          # 1.5 USDC (6dp)
EXPECTED_CAPTURED = 918           # settlePartial advances → cumulative captured
EXPECTED_ACTUAL = 918             # receipt actualAmount == settle actual (EXACT)
EXPECTED_REFUND = EXPECTED_MAX - EXPECTED_ACTUAL   # 1_499_082
EXPECTED_STATE_LOCKED = 1
EXPECTED_STATE_SETTLED = 2
EXPECTED_MODEL = "k3-256k"
EXPECTED_HOST = "api.kimi.com"
EXPECTED_DOMAIN = {"name": "TokenShare Relay", "version": "1", "chainId": CHAIN_ID}
# Exact pinned receipt signature (public — an EIP-712 signature, not a secret).
EXPECTED_SIGNATURE = (
    "0x85dec4838f998992c394a1e1aaad1039b55e1a363d176c950933db19bb504fd"
    "a76282052ee38730419dc4e9549dd2c1bbc316e8f34a855a56a1afcfb46d370a51c"
)
# Registry per-model price triple (6dp) pinned at the finalize trigger block.
EXPECTED_PRICE_TRIPLE = (3_000_000, 6_000_000, 9_000_000)
# Payment 5 expiry (getPayment expires_at at preflight; pinned + provenance-
# checked against the preserved preflight evidence on crash recovery).
EXPECTED_EXPIRES_AT = 1_791_432_001

TX_WAIT_TIMEOUT_S = 90.0
TX_POLL_S = 1.0
HTTP_TIMEOUT_S = 30.0

SETTLE_ABI = [
    {"type": "function", "name": "getPayment", "stateMutability": "view",
     "inputs": [{"name": "paymentId", "type": "uint256"}],
     "outputs": [
         {"name": "buyer", "type": "address"},
         {"name": "seller", "type": "address"},
         {"name": "maxAmount", "type": "uint256"},
         {"name": "expiresAt", "type": "uint64"},
         {"name": "state", "type": "uint8"},
     ]},
    {"type": "function", "name": "capturedOf", "stateMutability": "view",
     "inputs": [{"name": "paymentId", "type": "uint256"}],
     "outputs": [{"name": "", "type": "uint256"}]},
    {"type": "function", "name": "FEE_BPS", "stateMutability": "view",
     "inputs": [], "outputs": [{"name": "", "type": "uint16"}]},
    {"type": "function", "name": "settle", "stateMutability": "nonpayable",
     "inputs": [{"name": "paymentId", "type": "uint256"},
                {"name": "amount", "type": "uint256"}],
     "outputs": []},
    {"type": "event", "name": "Settled", "anonymous": False,
     "inputs": [
         {"name": "paymentId", "type": "uint256", "indexed": True},
         {"name": "buyer", "type": "address", "indexed": True},
         {"name": "seller", "type": "address", "indexed": True},
         {"name": "actualAmount", "type": "uint256", "indexed": False},
         {"name": "refundedAmount", "type": "uint256", "indexed": False},
     ]},
]
PRICE_ABI = [
    {"type": "function", "name": "getPrice", "stateMutability": "view",
     "inputs": [{"name": "operator", "type": "address"},
                {"name": "model", "type": "string"}],
     "outputs": [{"name": "price", "type": "tuple", "components": [
         {"name": "cachedIn", "type": "uint256"},
         {"name": "input", "type": "uint256"},
         {"name": "output", "type": "uint256"},
     ]}]},
]


# --------------------------------------------------------------------------
# tiny run helpers (no log spam, no secrets)
# --------------------------------------------------------------------------


def step(msg: str) -> None:
    print(f"\n=== {msg}", flush=True)


def info(msg: str) -> None:
    print(f"    {msg}", flush=True)


class Abort(Exception):
    """Hard stop — no fallback, no alternate path, nothing sent."""


def die(msg: str) -> "Abort":
    raise Abort(msg)


# --------------------------------------------------------------------------
# module loading by exact file path (no broad sys.path, no collisions)
# --------------------------------------------------------------------------


def _load_module(name: str, path: Path) -> Any:
    spec = importlib.util.spec_from_file_location(name, path)
    if spec is None or spec.loader is None:
        die(f"cannot load module {name} from {path}")
    mod = importlib.util.module_from_spec(spec)
    sys.modules[name] = mod
    spec.loader.exec_module(mod)
    return mod


def load_dotenv() -> None:
    """Pure-stdlib .env loader (repo root .env); values only enter
    os.environ; nothing is ever printed (private-key discipline)."""
    mod = _load_module("ts_run_demo_dotenv_loader", DOTENV_LOADER_PATH)
    mod.load_dotenv()


def receipt_module() -> Any:
    """relay/app/receipt.py — exact EIP-712 domain/order pin (lines 30-49)."""
    return _load_module("ts_run_demo_relay_receipt", RECEIPT_MODULE_PATH)


def w3_connect() -> Any:
    from web3 import Web3

    w3 = Web3(Web3.HTTPProvider(RPC_URL))
    if not w3.is_connected():
        die(f"RPC {RPC_URL} not reachable")
    chain = int(w3.eth.chain_id)
    if chain != CHAIN_ID:
        die(f"chainId {chain} != {CHAIN_ID}")
    return w3


def hex0x(value: Any) -> str:
    """Canonical 0x-prefixed lowercase hex from bytes/HexBytes/memoryview or
    an existing hex string (ora37 F4: str(HexBytes) and bytes.hex() prefix
    mixing are usage errors — every hash/topic/bytes32/tx-hash conversion
    goes through this helper)."""
    if isinstance(value, (bytes, bytearray, memoryview)):
        return "0x" + bytes(value).hex()
    if isinstance(value, str):
        s = value.strip().lower()
        if s.startswith("0x"):
            s = s[2:]
        if not re.fullmatch(r"[0-9a-f]*", s):
            die(f"hex0x: non-hex text starting {s[:8]!r}")
        return "0x" + s
    die(f"hex0x: cannot canonicalize type {type(value).__name__}")


def bytes_of(value: Any) -> bytes:
    """Canonical bytes from bytes/HexBytes or a 0x-hex string."""
    return bytes.fromhex(hex0x(value).removeprefix("0x"))


def hexint(value: Any) -> int:
    """Quantity canonicalization: RPC may deliver hex quantities as '0x…'
    strings or ints — accept both, never mis-parse (F4 discipline)."""
    if isinstance(value, int):
        return value
    if isinstance(value, str):
        s = value.strip().lower()
        if s.startswith("0x"):
            return int(s, 16)
        return int(s, 10)
    die(f"hexint: cannot parse quantity {value!r}")


def jsonable(obj: Any) -> Any:
    """Convert web3 objects (AttributeDict **is a Mapping, NOT a dict** —
    crash found live 2026-10-08: settled evidence write failed on it —
    HexBytes / tuples) into plain JSON types; all bytes canonically via
    hex0x()."""
    if isinstance(obj, (bytes, bytearray, memoryview)):
        return hex0x(obj)
    if isinstance(obj, str) or isinstance(obj, (int, float, bool)) or obj is None:
        return obj
    if isinstance(obj, dict) or (hasattr(obj, "items") and hasattr(obj, "keys")):
        return {str(k): jsonable(v) for k, v in obj.items()}
    if isinstance(obj, (list, tuple)):
        return [jsonable(v) for v in obj]
    return str(obj)   # last resort: never leak non-serializable separators


def load_anchor_artifact() -> tuple[dict[str, Any], str]:
    """(artifact, sha256-of-artifact-file). Bytecode may be solc dict or str."""
    raw = ARTIFACT_PATH.read_bytes()
    sha = hashlib.sha256(raw).hexdigest()
    art = json.loads(raw.decode("utf-8"))
    return art, sha


def bytecode_str(bc: Any, key: str = "object") -> str:
    if isinstance(bc, str):
        return bc
    if isinstance(bc, dict) and isinstance(bc.get(key), str):
        return bc[key]
    die(f"artifact bytecode field {key!r} has unexpected shape")


# --------------------------------------------------------------------------
# artifact runtime code — EXACT equality, immutableReferences patched (F1)
# --------------------------------------------------------------------------


def artifact_runtime_code(artifact: dict[str, Any], forwarder: str = FORWARDER) -> str:
    """EXACT deployed runtime bytecode for on-chain comparison: patch the
    compiler's immutableReferences slots (zero-filled in this artifact) with
    the pinned constructor value. Returns 0x-hex lowercase. Raises when the
    artifact carries no usable immutableReferences — a bare (unpatched)
    deployedBytecode can NEVER equal on-chain runtime code."""
    deploy = artifact.get("deployedBytecode")
    obj = bytecode_str(deploy if isinstance(deploy, dict) else artifact)
    obj = obj.removeprefix("0x").lower()

    refs = deploy.get("immutableReferences") if isinstance(deploy, dict) else None
    if not isinstance(refs, dict) or not refs:
        die("artifact deployedBytecode.immutableReferences missing — exact "
            "runtime equality is impossible; regenerate the artifact")
    identities = [str(i) for i in refs]
    if len(refs) > 1 and "forwarder" not in identities:
        die(f"multiple immutable identities {sorted(refs)} without 'forwarder' "
            "— ambiguous artifact; refusing to guess")
    if len(refs) == 1 and "forwarder" not in identities:
        # Single numeric solc id (e.g. "40962") == the contract's single
        # immutable address (ReceiptAnchor.forwarder). Patch with the pinned
        # forwarder and say so.
        info(f"artifact immutable id {identities[0]!r} → pinned singleton "
             f"immutable forwarder {forwarder}")
    slots: list[dict[str, Any]] = []
    for _ident, regions in refs.items():
        slots += list(regions)

    runtime = bytearray(bytes.fromhex(obj))
    value = (b"\x00" * (32 - 20)) + FORWARDER_BYTES
    patched_bytes = 0
    for r in slots:
        try:
            start, length = int(r["start"]), int(r["length"])
        except Exception as exc:  # noqa: BLE001
            die(f"immutableReferences malformed entry {r!r}: {exc}")
        if length < 20:
            die(f"immutable slot {start}/{length} too short for an address")
        if start < 0 or start + length > len(runtime):
            die("immutable slot out of bounds (truncated artifact bytecode?)")
        region = bytes(runtime[start:start + length])
        if region != bytes(length) and region != bytes(value):
            die(f"immutable slot {start}/{length} is neither zero-filled nor "
                "pinned-value patched — artifact tampered or foreign")
        runtime[start:start + length] = value
        patched_bytes += length
    info(f"artifact runtime: patched {patched_bytes} immutable bytes "
         f"({len(slots)} slots) with forwarder {forwarder}")
    return hex0x(bytes(runtime))


def code_matches_onchain(w3: Any, address: str, runtime_hex: str) -> tuple[bool, str]:
    """EXACT runtime equality vs on-chain code — NO prefix acceptance
    (ora37 F1): truncated, extended or shifted code is a MISMATCH."""
    onchain = hex0x(w3.eth.get_code(w3.to_checksum_address(address)))
    want = runtime_hex.lower()
    if onchain in ("0x", ""):
        return False, "on-chain code EMPTY (no contract)"
    if onchain.lower() == want:
        return True, "exact"
    return (False,
            f"runtime mismatch (artifact {len(want) - 2} bytes vs chain "
            f"{len(onchain) - 2} bytes) — NO prefix acceptance")


# --------------------------------------------------------------------------
# relay receipt: RAW response bytes + strict JSON (F5)
# --------------------------------------------------------------------------


def parse_strict_json(raw: bytes) -> Any:
    """Strict JSON: duplicate keys REJECTED (a plain json.loads silently
    last-wins — a tamper surface for the replay comparator)."""
    def pairs(pairs_list: list[tuple[str, Any]]) -> dict[str, Any]:
        out: dict[str, Any] = {}
        for key, val in pairs_list:
            if key in out:
                die(f"receipt HTTP body has duplicate JSON key {key!r} — "
                    "rejecting (a plain json.loads would silently last-win)")
            out[key] = val
        return out

    try:
        return json.loads(raw.decode("utf-8"), object_pairs_hook=pairs)
    except UnicodeDecodeError as exc:
        die(f"receipt HTTP body is not UTF-8 JSON: {exc}")
    except json.JSONDecodeError as exc:
        die(f"receipt HTTP body is not valid JSON: {exc}")


def _http_get_raw(url: str) -> tuple[int, bytes]:
    """Live configured GET; returns (status, COMPLETE raw body bytes)."""
    req = urllib.request.Request(url, method="GET",
                                 headers={"Accept": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=HTTP_TIMEOUT_S) as resp:
            return int(resp.status), resp.read()
    except urllib.error.HTTPError as exc:
        if exc.code == 404:
            die(f"receipt GET 404 at {url} — HTTP404 STOP: no fabrication, "
                "no substitution, no local-snapshot acceptance")
        die(f"receipt GET HTTP {exc.code} != 200 at {url} — STOP")
    except OSError as exc:
        die(f"receipt GET transport error at {url}: {exc}")


def fetch_receipt_data(payment_id: int) -> dict[str, Any]:
    """{url, status, raw bytes, strict-parsed json}; 404/!200 → STOP."""
    url = f"{RELAY_URL}/receipt/{payment_id}"
    status, raw = _http_get_raw(url)
    if status != 200:
        die(f"receipt GET HTTP {status} != 200 at {url} — STOP")
    return {"url": url, "status": status, "raw": raw,
            "json": parse_strict_json(raw)}


def receipt_snapshot(data: dict[str, Any]) -> dict[str, Any]:
    """Canonical storable snapshot: COMPLETE raw bytes (canonical hex) +
    parsed JSON. This is the ONLY baseline for replay/fetch comparisons
    (F5: local snapshot comparator only)."""
    return {"status": int(data["status"]),
            "raw_bytes": len(bytes_of(data["raw"])),
            "raw_hex": hex0x(data["raw"]),
            "json": json.loads(json.dumps(data["json"], sort_keys=True))}


def require_receipt_equal(snapshot: dict[str, Any] | None,
                          live: dict[str, Any], label: str) -> None:
    """RAW-BYTE equality vs the saved snapshot, then parsed-JSON equality
    (F3/F5). MISSING snapshot → blocked (raise, never a pass)."""
    if not snapshot or not snapshot.get("raw_hex"):
        die(f"{label}: receipt snapshot MISSING from evidence — full "
            "acceptance BLOCKED (never accepted without the exact baseline)")
    if hex0x(live["raw"]) != hex0x(snapshot["raw_hex"]):
        die(f"{label}: live receipt RAW BYTES differ from snapshot (even "
            "where JSON-parsed values would be equal) — refusing")
    live_json = json.loads(json.dumps(live["json"], sort_keys=True))
    if live_json != snapshot.get("json"):
        die(f"{label}: live receipt JSON differs from snapshot")


def is_int(v: Any) -> bool:
    """True integer (bool is NOT an int for schema purposes)."""
    return isinstance(v, int) and not isinstance(v, bool)


def verify_receipt_schema_and_signature(receipt: dict[str, Any]) -> dict[str, Any]:
    """All schema integer checks BEFORE signature recovery; exact domain;
    recover signer == TRUSTED_SIGNER over the exact relay EIP-712 order."""
    if set(receipt.keys()) != {"domain", "message", "signature"}:
        die(f"receipt top-level keys {sorted(receipt.keys())} != "
            "[domain, message, signature]")
    if receipt["domain"] != EXPECTED_DOMAIN:
        die(f"receipt domain {receipt['domain']} != {EXPECTED_DOMAIN}")
    msg = receipt["message"]
    need_ints = ("paymentId", "promptTokens", "cachedTokens",
                 "completionTokens", "actualAmount")
    for key in need_ints:
        if key not in msg or not is_int(msg[key]):
            die(f"receipt message.{key} missing or not a plain integer")
    if set(msg.keys()) != set(need_ints) | {"seller", "upstreamHost", "model"}:
        die(f"receipt message keys {sorted(msg.keys())} unexpected")
    if not (isinstance(receipt["signature"], str)
            and re.fullmatch(r"0x[0-9a-fA-F]{130}", receipt["signature"])):
        die("receipt signature is not 0x + 130 hex chars")

    # Exact pinned content (task-vector).
    if msg["paymentId"] != PAYMENT_ID:
        die(f"receipt paymentId {msg['paymentId']} != {PAYMENT_ID}")
    if msg["actualAmount"] != EXPECTED_ACTUAL:
        die(f"receipt actualAmount {msg['actualAmount']} != {EXPECTED_ACTUAL}")
    if msg["promptTokens"] != 93:
        die(f"receipt promptTokens {msg['promptTokens']} != 93")
    if msg["cachedTokens"] != 0:
        die(f"receipt cachedTokens {msg['cachedTokens']} != 0")
    if msg["completionTokens"] != 40:
        die(f"receipt completionTokens {msg['completionTokens']} != 40")
    if msg["model"] != EXPECTED_MODEL:
        die(f"receipt model {msg['model']!r} != {EXPECTED_MODEL!r}")
    if msg["upstreamHost"] != EXPECTED_HOST:
        die(f"receipt upstreamHost {msg['upstreamHost']!r} != {EXPECTED_HOST!r}")
    if str(msg["seller"]).lower() != EXPECTED_SELLER.lower():
        die(f"receipt seller {msg['seller']} != {EXPECTED_SELLER}")
    if receipt["signature"] != EXPECTED_SIGNATURE:
        die("receipt signature != pinned expected signature")

    from eth_account import Account
    rm = receipt_module()
    encoded = rm.encode_typed_data(full_message={
        "types": rm.RECEIPT_TYPES,       # exact order: receipt.py lines 30-49
        "primaryType": "Receipt",
        "domain": receipt["domain"],
        "message": msg,
    })
    recovered = Account.recover_message(encoded, signature=receipt["signature"])
    signer = str(recovered)
    if signer.lower() != TRUSTED_SIGNER.lower():
        die(f"receipt EIP-712 signer {signer} != trustedSigner {TRUSTED_SIGNER}")
    return {
        "recovered_signer": signer,
        "message": dict(msg),
        "domain": dict(receipt["domain"]),
        "signature": receipt["signature"],
        "schema_integer_checks": "passed (bool≠int enforced, all fields typed)",
    }


def pin_formula(receipt_msg: dict[str, int], prices: tuple[int, int, int]) -> int:
    """PIN actual — verbatim relay/app/pricing.py compute_actual (via
    e2e/run.py pin_actual): (cached*PC + (prompt-cached)*PI + completion*PO)
    // 1e6, 6dp native USDC integers."""
    cached_in, p_in, p_out = prices
    non_cached = max(0, int(receipt_msg["promptTokens"]) - int(receipt_msg["cachedTokens"]))
    return (
        int(receipt_msg["cachedTokens"]) * cached_in
        + non_cached * p_in
        + int(receipt_msg["completionTokens"]) * p_out
    ) // 10**6


def current_price_triple(w3: Any) -> tuple[int, int, int]:
    reg = w3.eth.contract(address=w3.to_checksum_address(REGISTRY), abi=PRICE_ABI)
    raw = reg.functions.getPrice(
        w3.to_checksum_address(EXPECTED_SELLER), EXPECTED_MODEL).call()
    cached_in, p_in, p_out = raw
    return int(cached_in), int(p_in), int(p_out)


def price_triple_at(w3: Any, block_number: int) -> tuple[int, int, int]:
    """Historical getPrice at an exact block (trigger-block finality)."""
    reg = w3.eth.contract(address=w3.to_checksum_address(REGISTRY), abi=PRICE_ABI)
    raw = reg.functions.getPrice(
        w3.to_checksum_address(EXPECTED_SELLER), EXPECTED_MODEL).call(
        block_identifier=block_number)
    cached_in, p_in, p_out = raw
    return int(cached_in), int(p_in), int(p_out)


def check_formula(receipt_msg: dict[str, int], triple: tuple[int, int, int],
                  label: str) -> int:
    actual = pin_formula(receipt_msg, triple)
    if actual != EXPECTED_ACTUAL:
        die(f"{label}: PIN formula on last receipt = {actual} != {EXPECTED_ACTUAL}")
    return actual


# --------------------------------------------------------------------------
# evidence helpers (public facts only)
# --------------------------------------------------------------------------


def ensure_evidence_dir() -> None:
    EVIDENCE_DIR.mkdir(parents=True, exist_ok=True)


def write_evidence(path: Path, payload: dict[str, Any]) -> None:
    ensure_evidence_dir()
    payload = {"written_at_unix": int(time.time()), **payload}
    path.write_text(json.dumps(payload, indent=2, sort_keys=False) + "\n",
                    encoding="utf-8")
    info(f"evidence written: {path}")


def read_evidence(path: Path) -> dict[str, Any] | None:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None


# --------------------------------------------------------------------------
# tx helpers (web3, in-process signing; pattern from e2e/run.py send_tx/gas_for)
# --------------------------------------------------------------------------


def load_seller_account() -> tuple[str, Any]:
    """SELLER_PRIVATE_KEY from env ONLY (never argv/echo)."""
    key = os.environ.get("SELLER_PRIVATE_KEY")
    if not key:
        die("SELLER_PRIVATE_KEY missing (env/.env); refusing to proceed")
    from eth_account import Account

    acct = Account.from_key(key)
    if acct.address.lower() != EXPECTED_SELLER.lower():
        die("funded key address mismatch (public check only): signer address "
            "does not equal the expected seller — aborting without revealing "
            "values")
    return acct.address, acct


def estimate_gas(w3: Any, built: dict[str, Any]) -> int:
    """estimateGas-based gas with headroom; NO flat fallback (any stage:
    estimate failure ⇒ abort, never guess)."""
    try:
        estimate = int(w3.eth.estimate_gas(
            {k: v for k, v in built.items() if k != "gas"}))
    except Exception as exc:  # noqa: BLE001
        die(f"gas estimate failed ({exc}) — aborting, no fallback")
    return int(estimate * 1.3) + 30_000


def fee_fields(w3: Any) -> dict[str, int]:
    latest = w3.eth.get_block("latest")
    base = latest.get("baseFeePerGas")
    if base is None:
        return {}
    return {"maxFeePerGas": int(base) * 2 + 1_000_000_000,
            "maxPriorityFeePerGas": 1_000_000_000}


def send_signed_tx(w3: Any, built: dict[str, Any], acct: Any, label: str,
                   persist_fn: Any = None) -> dict[str, Any]:
    """Sign (in-process) → send → persist_fn FIRST (F1: pending deployment
    tx hash persisted BEFORE wait) → wait receipt → require status 1."""
    signed = acct.sign_transaction(built)
    raw = getattr(signed, "raw_transaction", None) or signed.rawTransaction
    tx_hash_hex = hex0x(w3.eth.send_raw_transaction(raw))
    if persist_fn is not None:
        persist_fn(tx_hash_hex)   # BEFORE any wait — rerun reconciles later
    print(f"    [{label}] broadcast {tx_hash_hex} — waiting "
          f"({TX_WAIT_TIMEOUT_S:.0f}s poll {TX_POLL_S:.0f}s)…", flush=True)
    try:
        receipt = w3.eth.wait_for_transaction_receipt(
            tx_hash_hex, timeout=TX_WAIT_TIMEOUT_S, poll_latency=TX_POLL_S)
    except Exception as exc:  # noqa: BLE001
        die(f"{label} tx {tx_hash_hex} not mined within "
            f"{TX_WAIT_TIMEOUT_S:.0f}s ({exc}) — pending record persisted "
            "BEFORE wait; rerun reconciles it (no duplicate send)")
    rdict = jsonable(receipt)
    if int(rdict["status"]) != 1:
        die(f"{label} tx {tx_hash_hex} status {rdict['status']} != 1 (reverted)")
    return rdict


# --------------------------------------------------------------------------
# shared on-chain reads + EXACT Settled-event validation (F2: shared by
# fresh broadcast AND alreadySettled replay)
# --------------------------------------------------------------------------


def read_payment(w3: Any) -> dict[str, Any]:
    escrow = w3.eth.contract(address=w3.to_checksum_address(ESCROW), abi=SETTLE_ABI)
    buyer, seller, max_amount, expires_at, state = escrow.functions.getPayment(
        PAYMENT_ID).call()
    captured = int(escrow.functions.capturedOf(PAYMENT_ID).call())
    fee_bps = int(escrow.functions.FEE_BPS().call())
    return {
        "escrow": ESCROW,
        "payment_id": PAYMENT_ID,
        "buyer": str(buyer),
        "seller": str(seller),
        "max_amount": int(max_amount),
        "expires_at": int(expires_at),
        "state": int(state),
        "captured": captured,
        "fee_bps": fee_bps,
    }


def assert_payment_gates(p: dict[str, Any]) -> dict[str, Any]:
    """Every finalize gate, exact — any drift aborts (no fallback)."""
    if p["state"] != EXPECTED_STATE_LOCKED:
        die(f"payment state {p['state']} != {EXPECTED_STATE_LOCKED} (Locked)")
    if p["buyer"].lower() != EXPECTED_BUYER.lower():
        die(f"payment buyer {p['buyer']} != {EXPECTED_BUYER}")
    if p["seller"].lower() != EXPECTED_SELLER.lower():
        die(f"payment seller {p['seller']} != {EXPECTED_SELLER}")
    if p["max_amount"] != EXPECTED_MAX:
        die(f"payment maxAmount {p['max_amount']} != {EXPECTED_MAX}")
    if p["captured"] != EXPECTED_CAPTURED:
        die(f"capturedOf({PAYMENT_ID}) {p['captured']} != {EXPECTED_CAPTURED}")
    top_up = EXPECTED_ACTUAL - p["captured"]
    if top_up != 0:
        die(f"seller top-up {top_up} != 0 (EXACT settle requires captured==actual)")
    refund = EXPECTED_MAX - EXPECTED_ACTUAL
    if refund != EXPECTED_REFUND:
        die(f"derived refund {refund} != {EXPECTED_REFUND}")
    return {"top_up": top_up, "refund": refund, "fee": 0}


def _assert_settled_poststate(payment: dict[str, Any]) -> None:
    """Post-settle state, EXACT (F3): buyer/seller/max 1500000/state 2/
    captured 1500000 (max marker, not actual)."""
    if payment["state"] != EXPECTED_STATE_SETTLED:
        die(f"post-state payment state {payment['state']} != {EXPECTED_STATE_SETTLED}")
    if payment["captured"] != EXPECTED_MAX:
        die(f"post-state capturedOf {payment['captured']} != {EXPECTED_MAX} "
            "(max marker, not actual)")
    if payment["buyer"].lower() != EXPECTED_BUYER.lower():
        die(f"post-state buyer {payment['buyer']} != {EXPECTED_BUYER}")
    if payment["seller"].lower() != EXPECTED_SELLER.lower():
        die(f"post-state seller {payment['seller']} != {EXPECTED_SELLER}")
    if payment["max_amount"] != EXPECTED_MAX:
        die(f"post-state maxAmount {payment['max_amount']} != {EXPECTED_MAX}")


class _AttrReceipt(dict):
    """web3 process_receipt needs attribute-style access on the receipt."""

    def __getattr__(self, item: str) -> Any:
        try:
            return self[item]
        except KeyError as exc:  # pragma: no cover
            raise AttributeError(item) from exc


def settled_event_fact(w3: Any, receipt: dict[str, Any]) -> dict[str, Any]:
    """ONE canonical validator for BOTH fresh post-broadcast receipts AND
    saved/settled replay receipts (ora37 F2): receipt status must be 1 and
    exactly ONE escrow Settled event may carry the pinned tuple
    (paymentId 5, buyer, seller, actual 918, refunded 1499082). Returns the
    decoded fact incl. BOTH exported indices: `evm_event_index` (position in
    this transaction's receipt log array — the CRE --evm-event-index input)
    and `log_index` (the block-global logIndex). Numerically distinct keys."
    """
    if hexint(receipt.get("status", 0)) != 1:
        die(f"settle tx receipt status {receipt.get('status')} != 1")
    escrow = w3.eth.contract(address=w3.to_checksum_address(ESCROW), abi=SETTLE_ABI)
    events = escrow.events.Settled().process_receipt(_AttrReceipt(receipt))
    logs = [e for e in events if str(e["address"]).lower() == ESCROW.lower()]
    if len(logs) != 1:
        die(f"escrow Settled logs in receipt: {len(logs)} != 1")
    e = logs[0]
    args = e["args"]
    if int(args["paymentId"]) != PAYMENT_ID:
        die(f"Settled.paymentId {args['paymentId']} != {PAYMENT_ID}")
    if str(args["buyer"]).lower() != EXPECTED_BUYER.lower():
        die(f"Settled.buyer {args['buyer']} != {EXPECTED_BUYER}")
    if str(args["seller"]).lower() != EXPECTED_SELLER.lower():
        die(f"Settled.seller {args['seller']} != {EXPECTED_SELLER}")
    if int(args["actualAmount"]) != EXPECTED_ACTUAL:
        die(f"Settled.actualAmount {args['actualAmount']} != {EXPECTED_ACTUAL}")
    if int(args["refundedAmount"]) != EXPECTED_REFUND:
        die(f"Settled.refundedAmount {args['refundedAmount']} != {EXPECTED_REFUND}")
    log_index = hexint(e["logIndex"])
    evm_event_index = None
    for i, lg in enumerate(receipt["logs"]):
        if hexint(lg["logIndex"]) == log_index:
            evm_event_index = i
            break
    if evm_event_index is None:
        die("Settled log absent from the receipt's log array (cross-check failed)")
    return {
        # decoded log entry carries BOTH distinct CRE trigger indices:
        "settled_log": {**settled_log_json(e),
                        "evm_event_index": evm_event_index,   # receipt-array position (--evm-event-index)
                        "log_index": log_index},              # block-global logIndex
        "evm_event_index": evm_event_index,
        "log_index": log_index,
    }


def settled_log_json(event: dict[str, Any]) -> dict[str, Any]:
    """Public JSON fact for one Settled event log — all bytes canonical."""
    return {
        "address": str(event["address"]),
        "block_number": hexint(event["blockNumber"]),
        "transaction_hash": hex0x(event["transactionHash"]),
        "transaction_index": hexint(event["transactionIndex"]),
        "log_index": hexint(event["logIndex"]),
        "args": {
            "paymentId": int(event["args"]["paymentId"]),
            "buyer": str(event["args"]["buyer"]),
            "seller": str(event["args"]["seller"]),
            "actualAmount": int(event["args"]["actualAmount"]),
            "refundedAmount": int(event["args"]["refundedAmount"]),
        },
    }


def settle_tx_looks_exact(w3: Any, tx_hash: str) -> dict[str, Any]:
    """Full re-verification of a saved settle tx (alreadySettled path) —
    uses the SAME shared validator as the fresh broadcast (F2). Never
    fabricates: every fact is re-read from the chain."""
    tx = w3.eth.get_transaction(tx_hash)
    if tx is None:
        die(f"saved settle tx {tx_hash} not found on chain")
    if str(tx["to"]).lower() != ESCROW.lower():
        die("saved settle tx 'to' != Escrow")
    sel = w3.keccak(text="settle(uint256,uint256)")[:4]
    data = bytes_of(tx["input"])
    if data[:4] != bytes(sel):
        die("saved settle tx input selector != settle(uint256,uint256)")
    from eth_abi import decode

    payment_id, actual = decode(["uint256", "uint256"], data[4:])
    if (int(payment_id), int(actual)) != (PAYMENT_ID, EXPECTED_ACTUAL):
        die(f"saved settle tx calldata ({int(payment_id)}, {int(actual)}) != "
            f"({PAYMENT_ID}, {EXPECTED_ACTUAL})")
    receipt = jsonable(w3.eth.get_transaction_receipt(tx_hash))
    fact = settled_event_fact(w3, receipt)          # shared exact validation
    log_entry = fact["settled_log"]                 # carries both indices (F2)
    payment = read_payment(w3)
    if payment["state"] != EXPECTED_STATE_SETTLED:
        die("saved settle tx verified but payment state != Settled")
    if payment["captured"] != EXPECTED_MAX:
        die("saved settle verified but capturedOf != maxAmount")
    return {"tx_hash": tx_hash, "receipt": receipt, "payment": payment,
            "settled_log": log_entry}


# --------------------------------------------------------------------------
# stage: preflight (READ-ONLY)
# --------------------------------------------------------------------------


def stage_preflight() -> None:
    step(f"[preflight] Escrow p{PAYMENT_ID} + relay receipt verify (READ-ONLY)")
    w3 = w3_connect()
    payment = read_payment(w3)

    if payment["buyer"].lower() != EXPECTED_BUYER.lower():
        die(f"payment buyer {payment['buyer']} != {EXPECTED_BUYER}")
    if payment["seller"].lower() != EXPECTED_SELLER.lower():
        die(f"payment seller {payment['seller']} != {EXPECTED_SELLER}")
    if payment["max_amount"] != EXPECTED_MAX:
        die(f"payment maxAmount {payment['max_amount']} != {EXPECTED_MAX}")
    if payment["state"] != EXPECTED_STATE_LOCKED:
        die(f"payment state {payment['state']} != {EXPECTED_STATE_LOCKED} (Locked)")
    if payment["captured"] != EXPECTED_CAPTURED:
        die(f"capturedOf({PAYMENT_ID}) {payment['captured']} != {EXPECTED_CAPTURED}")

    receipt_data = fetch_receipt_data(PAYMENT_ID)   # HTTP200 else STOP (F5)
    checked = verify_receipt_schema_and_signature(receipt_data["json"])

    triple = current_price_triple(w3)
    actual = check_formula(checked["message"], triple,
                           f"last-receipt arithmetic with CURRENT Registry "
                           f"getPrice (cached/input/output = "
                           f"{triple[0]}/{triple[1]}/{triple[2]})")

    write_evidence(EVIDENCE_PREFLIGHT, {
        "stage": "preflight",
        "network": {"rpc": RPC_URL, "chain_id": CHAIN_ID},
        "escrow": ESCROW, "registry": REGISTRY,
        "payment": payment,
        "receipt_http_status": int(receipt_data["status"]),
        "receipt_snapshot": receipt_snapshot(receipt_data),
        "receipt": receipt_data["json"],
        "eip712": {
            "domain_order_source": "relay/app/receipt.py lines 30-49 (RECEIPT_TYPES)",
            "recovered_signer": checked["recovered_signer"],
            "trusted_signer": TRUSTED_SIGNER,
            "schema_integer_checks": checked["schema_integer_checks"],
        },
        "last_receipt_arithmetic": {
            "prices_current_getPrice": {"cached": triple[0], "input": triple[1],
                                        "output": triple[2]},
            "formula": "(cached*cachedPrice + (prompt-cached)*inputPrice "
                       "+ completion*outputPrice) // 1e6",
            "actual": actual,
            "expected": EXPECTED_ACTUAL,
        },
        "proof_limits": {
            "onchain_captured_is_single_upstream_call_proof": False,
            "narration": "capturedOf(5)=918 accumulates settlePartial captures; "
                         "it is NOT a proof that exactly one upstream call served "
                         "the buyer. Verification here is LIMITED to the LAST "
                         "receipt arithmetic (token counts → PIN formula == 918).",
            "scope": "last-receipt arithmetic only",
        },
    })
    info(f"payment OK: state={payment['state']} max={payment['max_amount']} "
         f"captured={payment['captured']}")
    info(f"receipt OK: HTTP 200 raw {len(bytes_of(receipt_data['raw']))}B, "
         f"exact domain/content, signer recovered {checked['recovered_signer']}")
    info(f"last-receipt arithmetic: actual={actual} (== {EXPECTED_ACTUAL})")
    info("evidence note: on-chain captured is NOT single-upstream-call proof; "
         "check limited to last receipt arithmetic")
    print("\nPREFLIGHT PASSED (read-only)", flush=True)


# --------------------------------------------------------------------------
# stage: deploy (dry-read unless --broadcast) — F1 exact runtime + pending
# lifecycle: persist-before-wait, reconcile-before-new-deploy.
# --------------------------------------------------------------------------


def anchor_contract(w3: Any, address: str, artifact: dict[str, Any]) -> Any:
    return w3.eth.contract(address=w3.to_checksum_address(address),
                           abi=artifact["abi"])


def anchor_forwarder_matches(w3: Any, address: str,
                             artifact: dict[str, Any]) -> str:
    anchor = anchor_contract(w3, address, artifact)
    fwd = hex0x(str(anchor.functions.forwarder().call()))
    if fwd.lower() != FORWARDER.lower():
        die(f"anchor {address} forwarder() {fwd} != {FORWARDER}")
    return fwd


def verify_saved_deployment(w3: Any, saved: dict[str, Any], artifact_sha: str,
                            runtime_hex: str) -> str:
    if int(saved.get("chain_id", -1)) != CHAIN_ID:
        die(f"saved deployment chain_id {saved.get('chain_id')} != {CHAIN_ID}")
    if saved.get("artifact_sha256") != artifact_sha:
        die("saved deployment artifact_sha256 != current artifact sha256 — "
            "rejecting (never redeploy over a mismatch; re-point evidence dir "
            "or re-approve manually)")
    match, how = code_matches_onchain(w3, str(saved["address"]), runtime_hex)
    if not match:
        die(f"saved deployment {saved['address']} on-chain code mismatch "
            f"({how}) — rejecting")
    return str(saved["address"])


def _deploy_pending_reconcile(w3: Any, pending: dict[str, Any],
                              artifact: dict[str, Any], artifact_sha: str,
                              runtime_hex: str) -> str:
    """Reconcile a saved PENDING deployment BEFORE any new deploy (F1).
    Outcomes: 'promoted' (mined OK, promoted to deploy.json), 'reverted'
    (definitively dead — pending resolved, a new deploy may proceed),
    else aborts (unresolved pending ⇒ never a duplicate deploy)."""
    if int(pending.get("chain_id", -1)) != CHAIN_ID or \
            pending.get("artifact_sha256") != artifact_sha:
        die("pending deployment was built with a different artifact/chain — "
            "resolve manually (no duplicate deploy)")
    tx_hash = pending.get("tx_hash")
    if not tx_hash:
        die("pending deployment record malformed (no tx_hash) — resolve "
            "manually (no duplicate deploy)")
    try:
        rcpt = w3.eth.get_transaction_receipt(tx_hash)
    except Exception:  # noqa: BLE001 — TransactionNotFound family
        rcpt = None
    if rcpt is None:
        die(f"pending deployment tx {tx_hash} still UNMINED — refusing to "
            "start a duplicate deploy; rerun later or resolve manually")
    if hexint(rcpt.get("status", 0)) != 1:
        write_evidence(EVIDENCE_DEPLOY_PENDING,
                       {**pending, "state": "resolved-reverted"})
        info(f"pending deploy {tx_hash} REVERTED — pending resolved; a new "
             "deploy may now broadcast")
        return "reverted"
    # Mined with status 1 → verify deeply, then PROMOTE (never redeploy).
    tx = w3.eth.get_transaction(tx_hash)
    if tx is None:
        die(f"pending deploy {tx_hash}: receipt exists but tx not found")
    if tx.get("to"):
        die("pending deploy tx has a 'to' address (not a contract creation)")
    creation = hex0x(bytecode_str(artifact["bytecode"]))
    data_hex = hex0x(tx["input"])   # bytes/HexBytes/str all canonicalize (F4)
    if len(data_hex) < len(creation) or \
            data_hex[: len(creation)].lower() != creation.lower():
        die("pending deploy tx input does not start with the artifact "
            "creation bytecode — foreign deployment, refusing to promote")
    address = str(rcpt.get("contractAddress") or "")
    if not address:
        die("pending deploy receipt has no contractAddress")
    match, how = code_matches_onchain(w3, address, runtime_hex)
    if not match:
        die(f"pending deploy {tx_hash} produced wrong runtime code ({how})")
    anchor_forwarder_matches(w3, address, artifact)
    write_evidence(EVIDENCE_DEPLOY, {
        "stage": "deploy",
        "artifact_path": str(ARTIFACT_PATH.relative_to(REPO_ROOT)),
        "artifact_sha256": artifact_sha,
        "chain_id": CHAIN_ID, "rpc": RPC_URL,
        "address": address,
        "tx_hash": tx_hash,
        "block_number": hexint(rcpt["blockNumber"]),
        "deployer": str(pending.get("sender", "")),
        "ctor_forwarder": FORWARDER,
        "code_match": "exact",
        "status": 1,
        "reconciled_from_pending": True,
    })
    write_evidence(EVIDENCE_DEPLOY_PENDING, {**pending, "state": "resolved-promoted"})
    info(f"pending deploy {tx_hash} mined OK — promoted to verify_saved "
         f"deployment {address} (no new deploy)")
    return "promoted"


def stage_deploy(broadcast: bool) -> None:
    step("[deploy] ReceiptAnchor artifact check (dry-read unless --broadcast)")
    artifact, artifact_sha = load_anchor_artifact()
    runtime_hex = artifact_runtime_code(artifact)
    print(f"    artifact: {ARTIFACT_PATH.name} sha256={artifact_sha} "
          f"runtime={len(runtime_hex) // 2 - 1}B", flush=True)

    saved = read_evidence(EVIDENCE_DEPLOY)
    w3 = w3_connect()
    if saved is not None and saved.get("address"):
        address = verify_saved_deployment(w3, saved, artifact_sha, runtime_hex)
        fwd = anchor_forwarder_matches(w3, address, artifact)
        info(f"reuse verified deployment {address} (chain/artifact/runtime "
             f"OK, forwarder OK) — no redeploy")
        if not broadcast:
            print("\nDEPLOY DRY-READ PASSED (existing verified deployment)", flush=True)
            return
        print("\nDEPLOY PASSED (reuse; nothing broadcast)", flush=True)
        return

    # ---- F1: reconcile a saved PENDING deployment BEFORE any new deploy.
    pending = read_evidence(EVIDENCE_DEPLOY_PENDING)
    if pending is not None and pending.get("state") == "pending":
        outcome = _deploy_pending_reconcile(w3, pending, artifact,
                                            artifact_sha, runtime_hex)
        if outcome == "promoted":
            info("pending deployment reconciled & promoted — nothing new "
                 "to broadcast")
            print("\nDEPLOY PASSED (pending reconciled & promoted)", flush=True)
            return
        # 'reverted' → fall through to a fresh deploy decision below.

    # No saved record + no pending → deploy (only with --broadcast).
    if not broadcast:
        print("    no saved/pending deployment; broadcast would deploy "
              f"ReceiptAnchor with ctor forwarder {FORWARDER} "
              "(DRY-READ: nothing sent)")
        print("\nDEPLOY DRY-READ PASSED (not broadcasting)", flush=True)
        return

    load_dotenv()
    _, acct = load_seller_account()
    nonce = int(w3.eth.get_transaction_count(acct.address))
    balance = int(w3.eth.get_balance(acct.address))
    if balance <= 0:
        die("funded seller has zero native balance — cannot deploy")
    info(f"deployer {acct.address} nonce={nonce}")

    ctor = w3.eth.contract(abi=artifact["abi"],
                           bytecode=bytecode_str(artifact["bytecode"]))
    built = ctor.constructor(w3.to_checksum_address(FORWARDER)).build_transaction({
        "from": acct.address,
        "nonce": nonce,
        "chainId": CHAIN_ID,
        **fee_fields(w3),
    })
    built["gas"] = estimate_gas(w3, built)     # estimate failure → abort

    def _persist_pending(tx_hash_hex: str) -> None:
        """F1: pending deployment tx persisted BEFORE wait_for_receipt."""
        write_evidence(EVIDENCE_DEPLOY_PENDING, {
            "stage": "deploy", "state": "pending",
            "tx_hash": tx_hash_hex,
            "sender": acct.address, "nonce": nonce,
            "chain_id": CHAIN_ID, "artifact_sha256": artifact_sha,
        })

    receipt = send_signed_tx(w3, built, acct, "deploy",
                             persist_fn=_persist_pending)
    address = str(receipt.get("contractAddress") or "")
    if not address:
        die("deploy receipt has no contractAddress")
    if hexint(receipt.get("blockNumber", 0)) <= 0:
        die("deploy receipt missing blockNumber")

    # post-deploy on-chain verification (status 1 already checked)
    fwd = anchor_forwarder_matches(w3, address, artifact)
    match, how = code_matches_onchain(w3, address, runtime_hex)
    if not match:
        die(f"fresh deploy on-chain code mismatch ({how}) — rejecting")

    write_evidence(EVIDENCE_DEPLOY, {
        "stage": "deploy",
        "artifact_path": str(ARTIFACT_PATH.relative_to(REPO_ROOT)),
        "artifact_sha256": artifact_sha,
        "chain_id": CHAIN_ID, "rpc": RPC_URL,
        "address": address,
        "tx_hash": hex0x(receipt["transactionHash"]),
        "block_number": hexint(receipt["blockNumber"]),
        "deployer": acct.address,
        "ctor_forwarder": FORWARDER,
        "code_match": "exact",
        "status": 1,
    })
    write_evidence(EVIDENCE_DEPLOY_PENDING, {
        "stage": "deploy", "state": "resolved-promoted",
        "tx_hash": hex0x(receipt["transactionHash"]), "address": address,
    })
    info(f"deployed {address} at block {receipt['blockNumber']} "
         f"(status 1, code {how}, forwarder {fwd})")
    print("\nDEPLOY PASSED", flush=True)


# --------------------------------------------------------------------------
# stage: finalize (dry-read unless --broadcast) — F2 shared event validator,
# F3 replay rules, F5 raw snapshot, F6 gate recheck after estimate.
# --------------------------------------------------------------------------


def stage_finalize(broadcast: bool, saved_tx: str | None,
                   reconcile_crash: bool = False) -> None:
    step("[finalize] seller settle(5, 918) EXACT (dry-read unless --broadcast)")
    _assert_reconcile_no_broadcast(reconcile_crash, broadcast)
    w3 = w3_connect()
    payment = read_payment(w3)

    # ---- alreadySettled path: replay ONLY (F3) — saved tx + snapshot +
    # historical prices + post-state; missing snapshot ⇒ BLOCKED.
    if payment["state"] == EXPECTED_STATE_SETTLED:
        _finalize_already_settled(w3, saved_tx, reconcile_crash, broadcast)
        return
    if reconcile_crash:
        die("--reconcile-evidence-crash is only valid when the payment is "
            f"already Settled (state {payment['state']} != "
            f"{EXPECTED_STATE_SETTLED})")
    if payment["state"] != EXPECTED_STATE_LOCKED:
        die(f"payment state {payment['state']} not Locked(1)/Settled(2) — aborting")

    # ---- exact-plan gates (read-only)
    gates = assert_payment_gates(payment)
    if gates["top_up"] != 0 or gates["fee"] != 0:
        die("EXACT plan violated: top-up/fee must be zero")
    info(f"gates OK: state=1 buyer/seller/max/captured exact, top_up=0, "
         f"fee=0, refund={gates['refund']}")

    # pre-settle receipt SNAPSHOT (raw bytes preserved — F5)
    receipt_pre = fetch_receipt_data(PAYMENT_ID)
    checked_pre = verify_receipt_schema_and_signature(receipt_pre["json"])
    snapshot_pre = receipt_snapshot(receipt_pre)

    escrow = w3.eth.contract(address=w3.to_checksum_address(ESCROW), abi=SETTLE_ABI)
    call = escrow.functions.settle(PAYMENT_ID, EXPECTED_ACTUAL)

    if not broadcast:
        # read-only simulation: proves revert-free without state change
        try:
            call.call({"from": w3.to_checksum_address(EXPECTED_SELLER)})
        except Exception as exc:  # noqa: BLE001
            die(f"dry-run eth_call of settle({PAYMENT_ID}, {EXPECTED_ACTUAL}) "
                f"reverted: {exc}")
        print(f"    DRY-READ: eth_call settle({PAYMENT_ID}, {EXPECTED_ACTUAL}) "
              "OK; nothing broadcast")
        print("\nFINALIZE DRY-READ PASSED (no tx sent)", flush=True)
        return

    # ---- broadcast path: gates → eth_call → estimate → GATE-RECHECK (F6) →
    # sign → send → wait
    try:
        call.call({"from": w3.to_checksum_address(EXPECTED_SELLER)})
    except Exception as exc:  # noqa: BLE001
        die(f"eth_call settle({PAYMENT_ID}, {EXPECTED_ACTUAL}) reverted: {exc}")

    load_dotenv()
    _, acct = load_seller_account()                  # signer == expected seller
    built = call.build_transaction({
        "from": acct.address,
        "nonce": int(w3.eth.get_transaction_count(acct.address)),
        "chainId": CHAIN_ID,
        **fee_fields(w3),
    })
    built["gas"] = estimate_gas(w3, built)           # aborts on estimate failure
    # F6: repeat the EXACT payment gates AFTER the estimate, immediately
    # BEFORE signing/sending — stop on any change, no fallback, no raised
    # amount (still EXACT 918).
    gates_pre_send = assert_payment_gates(read_payment(w3))
    if gates_pre_send != gates:
        die("payment gates CHANGED between plan and pre-send recheck — "
            "stop, no fallback settlement")
    info("pre-send gate recheck OK (F6): exact plan still settle(5, 918)")
    receipt = send_signed_tx(w3, built, acct, f"settle({PAYMENT_ID}, {EXPECTED_ACTUAL})")

    # ---- post-tx exact asserts (shared F2 validator)
    fact = settled_event_fact(w3, receipt)
    log = fact["settled_log"]
    if log["block_number"] != hexint(receipt["blockNumber"]) or \
            log["transaction_hash"] != hex0x(receipt["transactionHash"]):
        die("Settled log block/tx mismatch vs settle receipt")
    payment_after = read_payment(w3)
    _assert_settled_poststate(payment_after)
    info(f"post-tx: state=2, captured={payment_after['captured']} (max marker), "
         f"Settled(actual={log['args']['actualAmount']}, "
         f"refund={log['args']['refundedAmount']}) "
         f"evm_event_index={fact['evm_event_index']} logIndex={fact['log_index']}")

    # receipt equality AFTER settle — RAW BYTES first (F5), then JSON.
    receipt_after = fetch_receipt_data(PAYMENT_ID)
    require_receipt_equal(snapshot_pre, receipt_after, "post-settle refetch")
    verify_receipt_schema_and_signature(receipt_after["json"])

    # Historical trigger-block prices + formula (no fallback, reject mismatch)
    block_number = hexint(receipt["blockNumber"])
    triple = price_triple_at(w3, block_number)
    if tuple(triple) != EXPECTED_PRICE_TRIPLE:
        die(f"historical getPrice at trigger block {block_number} = "
            f"{triple} != {EXPECTED_PRICE_TRIPLE} — rejecting (no fallback)")
    actual = check_formula(checked_pre["message"], triple,
                           f"trigger-block {block_number} formula")
    info(f"trigger block {block_number}: getPrice = "
         f"{triple[0]}/{triple[1]}/{triple[2]} → formula actual={actual}")

    write_evidence(EVIDENCE_FINALIZE, {
        "stage": "finalize", "mode": "broadcast",
        "gates": gates,
        "tx_hash": hex0x(receipt["transactionHash"]),
        "tx_receipt": receipt,
        # CRE trigger export — log ARRAY entry carries BOTH distinct indices
        # (receipt-array event index for CRE --evm-event-index + global logIndex):
        "settled_logs": [{**log,
                          "evm_event_index": fact["evm_event_index"],
                          "log_index": fact["log_index"]}],
        "trigger_evm_event_index": fact["evm_event_index"],
        "trigger_log_index": fact["log_index"],
        "trigger_block": block_number,
        "payment_after": payment_after,
        "receipt_snapshot": snapshot_pre,          # F3: baseline for replay
        "receipt_refetch_raw_equal": True,
        "historical_prices": {"block": block_number,
                              "cached": triple[0], "input": triple[1],
                              "output": triple[2]},
        "last_receipt_arithmetic_actual": actual,
    })
    print("\nFINALIZE PASSED", flush=True)


def _preflight_crash_baseline(w3: Any, settle_block: int) -> tuple[dict[str, Any], str]:
    """LABELED crash-reconcile baseline from preflight.json — reachable ONLY
    via the explicit --reconcile-evidence-crash entry (never the defaultpath,
    never with --broadcast). Full provenance validation (ora42 F: any drift
    ⇒ reject):
      stage=preflight; chain 10143 + exact configured RPC; escrow/registry
      addresses; payment 5 buyer/seller/max/captured 918/state 1/expires
      pinned; HTTP 200; snapshot raw+json complete; FULL receipt
      semantic/EIP-712 verification of the snapshot itself; written_at_unix
      present, integer and STRICTLY earlier than the settle tx block
      timestamp (missing/later/equal/non-int ⇒ reject)."""
    pre = read_evidence(EVIDENCE_PREFLIGHT) or {}

    def bad(msg: str) -> None:
        die(f"crash-reconcile preflight baseline REJECTED (explicit recovery "
            f"path): {msg}")

    if pre.get("stage") != "preflight":
        bad(f"stage {pre.get('stage')!r} != 'preflight'")
    net = pre.get("network") or {}
    if hexint(net.get("chain_id", -1)) != CHAIN_ID:
        bad(f"network.chain_id {net.get('chain_id')} != {CHAIN_ID}")
    if net.get("rpc") != RPC_URL:
        bad(f"network.rpc {net.get('rpc')!r} != configured RPC")
    if pre.get("escrow") != ESCROW:
        bad(f"escrow address {pre.get('escrow')} != pinned {ESCROW}")
    if pre.get("registry") != REGISTRY:
        bad(f"registry address {pre.get('registry')} != pinned {REGISTRY}")
    pay = pre.get("payment") or {}
    pinned_payment = {
        "payment_id": PAYMENT_ID,
        "buyer": EXPECTED_BUYER,
        "seller": EXPECTED_SELLER,
        "max_amount": EXPECTED_MAX,
        "expires_at": EXPECTED_EXPIRES_AT,
        "state": EXPECTED_STATE_LOCKED,
        "captured": EXPECTED_CAPTURED,
    }
    for key, want in pinned_payment.items():
        got = pay.get(key)
        if isinstance(want, str):
            ok = (isinstance(got, str)
                  and got.lower() == want.lower())
        elif isinstance(got, bool) or not isinstance(got, int):
            ok = False
        else:
            ok = got == want
        if not ok:
            bad(f"payment.{key} {got!r} != pinned {want!r}")
    if pre.get("receipt_http_status") not in (200, "200"):
        bad(f"receipt_http_status {pre.get('receipt_http_status')} != 200 (raw HTTP200 gate)")
    snapshot = pre.get("receipt_snapshot") or {}
    if not snapshot.get("raw_hex") or not snapshot.get("json"):
        bad("receipt_snapshot incomplete (raw_hex/json missing)")
    if hexint(snapshot.get("status", 0)) != 200:
        bad(f"snapshot.status {snapshot.get('status')} != 200")
    written = pre.get("written_at_unix")
    if isinstance(written, bool) or not isinstance(written, int):
        bad(f"written_at_unix {written!r} missing or not a plain integer")
    block = jsonable(w3.eth.get_block(settle_block))
    ts_raw = block.get("timestamp")
    if ts_raw is None:
        bad(f"settle block {settle_block} timestamp unreadable (missing)")
    try:
        block_ts = hexint(ts_raw)
    except Abort:
        bad(f"settle block {settle_block} timestamp {ts_raw!r} unparseable")
    if not isinstance(block_ts, int) or block_ts <= 0:
        bad(f"settle block {settle_block} timestamp unreadable")
    if not written < block_ts:
        bad(f"written_at_unix {written} is NOT strictly earlier than settle "
            f"block {settle_block} timestamp {block_ts} (must be strictly "
            "earlier; equal or later ⇒ reject)")
    verify_receipt_schema_and_signature(snapshot["json"])   # full receipt gate
    label = ("preflight.json — explicit --reconcile-evidence-crash recovery "
             "(crash at finalize evidence write); provenance validated: "
             f"stage/network/payment(expires {EXPECTED_EXPIRES_AT})/addresses"
             f"/raw snapshot; written_at {written} < settle-block "
             f"{settle_block} ts {block_ts}")
    return snapshot, label


def _assert_reconcile_no_broadcast(reconcile_crash: bool, broadcast: bool) -> None:
    """Pure guard: the evidence-crash recovery entry is READ-ONLY replay —
    never combinable with --broadcast (parent rule: 严禁broadcast)."""
    if reconcile_crash and broadcast:
        die("--reconcile-evidence-crash refuses --broadcast: the recovery path "
            "never sends transactions (read-only replay only)")


def _finalize_already_settled(w3: Any, saved_tx: str | None,
                             reconcile_crash: bool = False,
                             broadcast: bool = False) -> None:
    """F3 replay: NEVER fabricate/never print FINALIZE PASSED with less than
    (saved verified tx + post-state + historical trigger prices+formula +
    pre-settle snapshot equality). Default baseline: finalize.json's OWN
    snapshot. Missing/incomplete snapshot ⇒ STOP (silent fallback removed).
    The preflight.json comparator exists ONLY behind the explicit
    --reconcile-evidence-crash flag (strict provenance-gated, read-only).
    Evidence: MERGE ONLY — prior gates/info are never overwritten/lost;
    reconstructed gates are explicitly derived/posthoc (never a claim of
    reproduced presign checks)."""
    step("[finalize] payment already Settled — replay re-verification")
    _assert_reconcile_no_broadcast(reconcile_crash, broadcast)
    saved = read_evidence(EVIDENCE_FINALIZE)
    tx_hash = saved_tx or (saved or {}).get("tx_hash")
    if not tx_hash:
        die("alreadySettled replay BLOCKED: no saved settle tx available and "
            "none passed — never fabricate; pass --tx <saved settle tx>")

    # STOP BEFORE any chain work (ora42): the default path accepts ONLY
    # finalize.json's own pre-settle snapshot. The preflight.json comparator
    # exists EXCLUSIVELY behind --reconcile-evidence-crash (+ --tx; never
    # --broadcast) and is reserve-validated against the settle block later.
    own_bi_snapshot = (saved or {}).get("receipt_snapshot")
    if not own_bi_snapshot or not own_bi_snapshot.get("raw_hex"):
        if not reconcile_crash:
            die("alreadySettled replay STOP: finalize evidence is missing the "
                "pre-settle receipt snapshot — default accepts ONLY the "
                "finalize.json baseline; the preflight.json comparator is "
                "available exclusively via --reconcile-evidence-crash WITH "
                "--tx (read-only replay; never --broadcast)")

    print(f"    re-verifying saved tx {tx_hash}", flush=True)
    # 1. saved/settled tx re-verified with the SAME shared validator (F2).
    verified = settle_tx_looks_exact(w3, tx_hash)
    # 2. exact post-state (buyer/seller/max/state2/captured==max).
    _assert_settled_poststate(verified["payment"])
    # 3. historical trigger-block prices (no fallback).
    block = int(verified["settled_log"]["block_number"])
    triple = price_triple_at(w3, block)
    if tuple(triple) != EXPECTED_PRICE_TRIPLE:
        die(f"replay historical getPrice at block {block} = {triple} != "
            f"{EXPECTED_PRICE_TRIPLE} — BLOCKED (no fallback)")

    # 4. snapshot resolution: own finalize baseline, or the flagged+validated
    #    preflight recovery (provenance incl. written_at < settle-block ts).
    if own_bi_snapshot and own_bi_snapshot.get("raw_hex"):
        snapshot, snapshot_source = own_bi_snapshot, "finalize.json"
    else:
        snapshot, snapshot_source = _preflight_crash_baseline(w3, block)
        print("    LABELED crash-recovery baseline in use (explicit "
              "--reconcile-evidence-crash; strict provenance)", flush=True)

    # 5. receipt validation + RAW-equality against the saved snapshot.
    live = fetch_receipt_data(PAYMENT_ID)
    require_receipt_equal(snapshot, live, "alreadySettled replay")
    verify_receipt_schema_and_signature(live["json"])
    verify_receipt_schema_and_signature(snapshot["json"])   # baseline re-checked
    actual = check_formula(live["json"]["message"], triple,
                           f"replay trigger-block {block} formula")
    info(f"replay OK: poststate exact, snapshot raw-equal, block {block} "
         f"prices {triple[0]}/{triple[1]}/{triple[2]}, formula {actual}")

    # MERGE-ONLY update (F3): prior evidence keys preserved. When finalize
    # evidence never existed (crash-reconcile), the on-chain re-verified
    # facts build the missing baseline document explicitly; reconstructed
    # gates are DERIVED/POSTHOC — never a claim of reproduced presign checks.
    replay_block = {
        "verified_at_unix": int(time.time()),
        "tx_hash": tx_hash,
        "settled_log": verified["settled_log"],
        "trigger_evm_event_index_replay": verified["settled_log"]["evm_event_index"],
        "trigger_log_index_replay": verified["settled_log"]["log_index"],
        "poststate_asserts": "exact (state2/captured=max/buyer/seller/max)",
        "trigger_block": block,
        "historical_prices": {"cached": triple[0], "input": triple[1],
                              "output": triple[2]},
        "last_receipt_arithmetic_actual": actual,
        "receipt_live_raw_equal_to_snapshot": True,
        "snapshot_source": snapshot_source,
    }
    if saved is None:
        base_doc = {
            "stage": "finalize",
            "mode": "broadcast-reconciled-from-crash",
            "gates": {
                "top_up": 0, "refund": EXPECTED_REFUND, "fee": 0,
                "derived_posthoc": True,
                "presign_gate_recheck_reproduced": False,
                "note": ("gates reconstructed from pinned constants during "
                         "labeled crash-reconcile — NOT a reproduced pre-sign "
                         "gate check (the crashed broadcast run evidenced its "
                         "own passthrough only in stdout, never persisted)"),
            },
            "tx_hash": tx_hash,
            "tx_receipt": verified["receipt"],
            "settled_logs": [verified["settled_log"]],
            "trigger_evm_event_index": verified["settled_log"]["evm_event_index"],
            "trigger_log_index": verified["settled_log"]["log_index"],
            "trigger_block": block,
            "payment_after": verified["payment"],
            "receipt_snapshot": snapshot,
            "reconciled_from": ("on-chain settlement re-verified after the "
                                "broadcast-run evidence-write crash"),
            "presign_gate_recheck_reproduced": False,
        }
    else:
        base_doc = saved
    merged = {**base_doc, "replay": replay_block, "replay_verified": True}
    write_evidence(EVIDENCE_FINALIZE, merged)
    print("\nFINALIZE PASSED (alreadySettled; replay re-verified)", flush=True)


# --------------------------------------------------------------------------
# stage: readback (READ-ONLY; requires --anchor and --tx)
# --------------------------------------------------------------------------


def stage_readback(anchor: str, tx_hash: str) -> None:
    step("[readback] anchor contract + actual anchor broadcast tx (READ-ONLY)")
    if not anchor:
        die("readback requires --anchor <ReceiptAnchor address>")
    if not tx_hash:
        die("readback requires --tx <anchor broadcast tx hash>")
    artifact, artifact_sha = load_anchor_artifact()
    runtime_hex = artifact_runtime_code(artifact)
    w3 = w3_connect()
    anchor_addr = w3.to_checksum_address(anchor)

    match, how = code_matches_onchain(w3, anchor_addr, runtime_hex)
    if not match:
        die(f"anchor {anchor_addr} code mismatch ({how}) — rejecting")
    anchor_c = anchor_contract(w3, anchor_addr, artifact)

    fwd = hex0x(str(anchor_c.functions.forwarder().call()))
    if fwd.lower() != FORWARDER.lower():
        die(f"anchor forwarder() {fwd} != {FORWARDER}")

    rec = anchor_c.functions.records(PAYMENT_ID).call()

    def rget(key: str) -> Any:
        if isinstance(rec, dict):
            return rec[key]
        keys = ["paymentId", "settledAmount", "receiptAmount", "receiptHash",
                "upstreamHost", "model", "verdict", "anchoredAt"]
        return rec[keys.index(key)]

    record = {
        "paymentId": int(rget("paymentId")),
        "settledAmount": int(rget("settledAmount")),
        "receiptAmount": int(rget("receiptAmount")),
        "receiptHash": hex0x(bytes(rget("receiptHash"))),   # bytes32 canonical (F4)
        "upstreamHost": str(rget("upstreamHost")),
        "model": str(rget("model")),
        "verdict": int(rget("verdict")),
        "anchoredAt": int(rget("anchoredAt")),
    }
    if record["paymentId"] != PAYMENT_ID:
        die(f"records(5).paymentId {record['paymentId']} != {PAYMENT_ID}")
    if record["settledAmount"] != EXPECTED_ACTUAL:
        die(f"records(5).settledAmount {record['settledAmount']} != {EXPECTED_ACTUAL}")
    if record["receiptAmount"] != EXPECTED_ACTUAL:
        die(f"records(5).receiptAmount {record['receiptAmount']} != {EXPECTED_ACTUAL}")
    # receiptHash must equal keccak256(signature bytes) of the pinned receipt
    if record["receiptHash"] != hex0x(w3.keccak(bytes_of(EXPECTED_SIGNATURE))):
        die("records(5).receiptHash != keccak256(receipt signature)")
    if record["upstreamHost"] != EXPECTED_HOST:
        die(f"records(5).upstreamHost {record['upstreamHost']!r} != {EXPECTED_HOST!r}")
    if record["model"] != EXPECTED_MODEL:
        die(f"records(5).model {record['model']!r} != {EXPECTED_MODEL!r}")
    if record["verdict"] != 1:   # Verdict.Match
        die(f"records(5).verdict {record['verdict']} != 1 (Match)")

    is_verified = bool(anchor_c.functions.isVerified(PAYMENT_ID).call())
    if not is_verified:
        die("isVerified(5) is not True")
    total_anchored = int(anchor_c.functions.totalAnchored().call())
    if total_anchored < 1:
        die(f"totalAnchored {total_anchored} < 1")

    # ---- actual anchor broadcast tx: exact ReceiptAnchored event
    receipt = jsonable(w3.eth.get_transaction_receipt(tx_hash))
    if hexint(receipt["status"]) != 1:
        die(f"anchor tx {tx_hash} status != 1")
    topic0 = hex0x(w3.keccak(text=(
        "ReceiptAnchored(uint256,bytes32,uint256,uint256,uint8)")))
    matched = [lg for lg in receipt["logs"]
               if hex0x(lg["address"]).lower() == anchor_addr.lower()
               and hex0x(lg["topics"][0]).lower() == topic0]
    if len(matched) != 1:
        die(f"ReceiptAnchored logs for payment {PAYMENT_ID} in tx {tx_hash}: "
            f"{len(matched)} != 1")
    event = anchor_c.events.ReceiptAnchored().process_receipt(_AttrReceipt(receipt))
    ev = [e for e in event if str(e["address"]).lower() == anchor_addr.lower()
          and int(e["args"]["paymentId"]) == PAYMENT_ID]
    if len(ev) != 1:
        die("could not decode exactly one ReceiptAnchored(5) event from tx")
    e = ev[0]
    e_log_index = hexint(e["logIndex"])
    evm_event_index = None
    for i, lg in enumerate(receipt["logs"]):
        if hexint(lg["logIndex"]) == e_log_index:
            evm_event_index = i
    if evm_event_index is None:
        die("ReceiptAnchored event not found in the receipt log array")
    ev_json = {
        "address": str(e["address"]),
        "block_number": hexint(e["blockNumber"]),
        "transaction_hash": hex0x(e["transactionHash"]),   # canonical (F4)
        "log_index": e_log_index,
        # receipt-array index export (distinct from global logIndex):
        "evm_event_index": evm_event_index,
        "args": {
            "paymentId": int(e["args"]["paymentId"]),
            "receiptHash": hex0x(bytes(e["args"]["receiptHash"])),
            "settledAmount": int(e["args"]["settledAmount"]),
            "receiptAmount": int(e["args"]["receiptAmount"]),
            "verdict": int(e["args"]["verdict"]),
        },
    }
    if ev_json["args"]["receiptHash"] != record["receiptHash"]:
        die("event receiptHash != records(5).receiptHash")
    if ev_json["args"]["settledAmount"] != EXPECTED_ACTUAL:
        die("event settledAmount != 918")
    if ev_json["args"]["receiptAmount"] != EXPECTED_ACTUAL:
        die("event receiptAmount != 918")
    if ev_json["args"]["verdict"] != 1:
        die("event verdict != 1")
    if ev_json["block_number"] != hexint(receipt["blockNumber"]) or \
            ev_json["transaction_hash"] != hex0x(receipt["transactionHash"]):
        die("ReceiptAnchored event block/tx mismatch vs anchor tx receipt")

    write_evidence(EVIDENCE_READBACK, {
        "stage": "readback",
        "anchor": anchor_addr,
        "anchor_broadcast_tx": tx_hash,
        "artifact_sha256": artifact_sha,
        "code_match": how,
        "forwarder": fwd,
        "records": record,
        "is_verified": is_verified,
        "total_anchored": total_anchored,
        "receipt_anchored_event": ev_json,
    })
    info(f"forwarder OK, records(5) settled={record['settledAmount']} "
         f"receipt={record['receiptAmount']} verdict=Match, isVerified=True, "
         f"totalAnchored={total_anchored}")
    info(f"ReceiptAnchored event exact: block={ev_json['block_number']} "
         f"tx={ev_json['transaction_hash'][:18]}… "
         f"evm_event_index={ev_json['evm_event_index']} "
         f"logIndex={ev_json['log_index']}")
    print("\nREADBACK PASSED (read-only)", flush=True)


# --------------------------------------------------------------------------
# CLI
# --------------------------------------------------------------------------


def main() -> None:
    parser = argparse.ArgumentParser(
        description="TokenShare CRE demo runner (Monad testnet 10143) — "
                    "read-only by default; --broadcast required for any tx.")
    parser.add_argument("--stage", required=True,
                        choices=["preflight", "deploy", "finalize", "readback"])
    parser.add_argument("--anchor", help="readback: ReceiptAnchor address")
    parser.add_argument("--tx", help="readback: anchor broadcast tx hash "
                                     "(or finalize alreadySettled saved tx)")
    parser.add_argument("--broadcast", action="store_true",
                        help="ACTUALLY send transactions (deploy/finalize); "
                             "default is dry/read-only")
    parser.add_argument("--reconcile-evidence-crash", action="store_true",
                        help="finalize ONLY: allow the LABELED preflight.json "
                             "baseline recovery when finalize.json snapshot "
                             "is missing (strict provenance-validated, "
                             "read-only replay; refuses --broadcast)")
    args = parser.parse_args()

    try:
        if args.stage == "preflight":
            stage_preflight()
        elif args.stage == "deploy":
            stage_deploy(args.broadcast)
        elif args.stage == "finalize":
            stage_finalize(args.broadcast, args.tx,
                           reconcile_crash=args.reconcile_evidence_crash)
        elif args.stage == "readback":
            stage_readback(args.anchor, args.tx)
    except Abort as exc:
        print(f"\nRUN ABORTED: {exc}", flush=True)
        sys.exit(1)


if __name__ == "__main__":
    main()
