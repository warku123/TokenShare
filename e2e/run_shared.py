#!/usr/bin/env python3
"""TokenShare LOCAL shared-custody integration runner (M15 R2, E lane).

    python3 e2e/run_shared.py        # pure-local anvil, ZERO external RPC

Everything on this machine:
  1. anvil PURE LOCAL (no --fork-url, chainId 31337) — no external RPC
     dependency at all. Public default accounts (well-known test mnemonic,
     valueless; private keys are NEVER printed):
       0 deployer | 1 buyer | 2 sellerA | 3 sellerB | 4 sharedSigner | 5 probeP | 6 probeQ
  2. forge script Deploy.s.sol --sig run(local_shared) deploys the CURRENT
     tracked sources (Escrow with the settle-delegation face + Registry v4)
     + MockUSDC (fee-free demo: FEE_BPS=0). It writes the IGNORED
     contracts/deployed.json — a pre-run snapshot is restored afterwards and
     the TRACKED contracts/deployed.monad.json is hash-proven unchanged.
  3. Direct web3 with MINIMAL ABI fragments (no CLI ABI expansion):
     MockUSDC.mint(buyer) + approveSettleDelegate(sharedSigner) from both
     sellers (the frozen R2 delegation mechanism).
  4. TWO mock upstreams (e2e/mock_openai.py): per-seller catalog faces
     (mock-model-a / mock-model-b, plus an official-prefixed id whose
     servable flag must stay FALSE under the existing provider rule — no
     production exemption is invented), /v1/* BEARER ALLOWLIST enforcement
     and per-key request counters (GET /counters).
  5. relay REAL HTTP process (uvicorn, RELAY_MODE=shared):
     RELAY_PUBLIC_ORIGIN = http://127.0.0.1:<port> + ALLOW_INSECURE_ENDPOINT=1
     (dev-only http origin), SHARED_KEYSTORE_PATH (encrypted keystore),
     SHARED_SIGNER_KEY / SHARED_KEK_HEX / SHARED_UPLOAD_KEY — synthetic keys
     generated at runtime, never printed or logged. ALLOW_CUSTOM_UPSTREAM=1
     (mock upstream dev flag only).
  6. Frozen R2 scenario segments:
     a. wallet-authenticated ENCRYPTED custody enrollment for both sellers
        BEFORE any listing exists (prelisting allowed) — R1 ceremony.
     b. listings via direct web3: SAME trusted relay endpoint, different
        keys/models/prices.
     c. buyer: CLI deposit -> lock(payment->A)/lock(payment->B) -> Bearer
        calls with DIFFERENT prices -> on-chain capture + per-seller Escrow
        balance deltas prove per-seller pricing end to end.
     d. receipt: economic seller != pinned shared signer — cli
        verify_receipt with the explicit pin verifies OK; WITHOUT a pin the
        verification is REJECTED (recover-mismatch). One full CLI `call
        --expected-signer` black-box segment.
     e. legacy path (X-Payment-Id + X-Signature) and stream path exercised.
     f. 424 gates: unenrolled seller / wrong-endpoint probe listing /
        revoked delegate — ZERO upstream quota (counter deltas).
     g. buyer revoke endpoint -> the ORIGINAL bearer -> 401 "revoked".
     h. refund: TTL warp -> on-chain buyer refund tx returns EXACTLY
        maxAmount - captured (gross; refunds are never charged).
     i. restart: kill relay, start a NEW process reusing the encrypted
        keystore + same signer/KEK/upload env -> the ORIGINAL bearer works
        with NO re-enrollment; capturedOf recovery admits only within the
        remaining budget.
     j. cross-seller non-contamination evidence: counters — mock1 never saw
        seller B's credential, mock2 never saw seller A's.
  7. Prints E2E SHARED PASSED (verbatim) on success; E2E SHARED FAILED with
     a non-zero exit code otherwise.

  HONEST LIMITS: the restart proves RELAY-SIDE encrypted-keystore continuity
  (operator-held KEK in env) only. It is NOT dstack KMS continuity and NOT a
  real TEE; CVM key derivation and /attestation quote verification are out
  of scope here.
"""

from __future__ import annotations

import hashlib
import json
import subprocess
import sys
import time
from pathlib import Path
from typing import Any, NoReturn

REPO_ROOT = Path(__file__).resolve().parents[1]
E2E_DIR = REPO_ROOT / "e2e"
sys.path.insert(0, str(E2E_DIR))

import run as single_harness  # noqa: E402  (helper reuse; no harness semantic change)

# repo-root import path for the relay library (decode/encode helpers)
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))
from relay.app.receipt import decode_x_receipt  # noqa: E402
from relay.app.chain import ZERO_ADDRESS  # noqa: E402

# ---------------------------------------------------------------------------
# Local constants (fee-free demo deploy; concrete but computed via the
# imported pin formula — no hand math in the asserts).
# ---------------------------------------------------------------------------
CHAIN_ID_LOCAL = 31337  # anvil pure-local default (no fork, no external RPC)
FEE_BPS_LOCAL = 0  # Deploy.s.sol demo default: fee-free

DEPOSIT_USDC = "2000"  # whole USDC minted/deposited (human)
LOCK_USDC_A = "510"   # call1 captures 310000000 native (= 310 USDC); call2 serves
                      # the 200000000-native (= 200 USDC) remainder; call3 402s
LOCK_USDC_B = "800"
LOCK_TTL_S = 600

USAGE_A = {"prompt_tokens": 5000, "cached_tokens": 1000, "completion_tokens": 800}
USAGE_B = {"prompt_tokens": 3000, "cached_tokens": 0, "completion_tokens": 500}

# USDC-native per-1M-token triples (cached, input, output) — per SELLER.
# Scaled so ONE served call costs 310000000 native (= 310 USDC) for A and
# 80000000 native (= 80 USDC) for B — the budget-gate math must be exact and
# the per-seller deltas visibly different.
PRICES_A = (30_000_000_000, 50_000_000_000, 100_000_000_000)
PRICES_B = (10_000_000_000, 20_000_000_000, 40_000_000_000)

# Catalog faces: seller A's upstream mock, seller B's upstream mock. The
# official-prefixed id is INCLUDED deliberately: the existing provider rule
# must mark it servable=FALSE on this custom (non-official) host.
CATALOG_A = ["mock-model-a", "gpt-4o-mini"]
CATALOG_B = ["mock-model-b"]
MODEL_A = "mock-model-a"
MODEL_B = "mock-model-b"


def step(msg: str) -> None:
    print(f"\n=== {msg}", flush=True)


def shared_fail(reason: str) -> NoReturn:
    single_harness.cleanup()
    print(f"E2E SHARED FAILED: {reason}", file=sys.stderr, flush=True)
    raise SystemExit(1)


def shared_fail_all(reason: str) -> NoReturn:
    single_harness.cleanup()
    shared_fail(reason)


def generate_shared_keys() -> dict[str, str]:
    """Runtime-generated synthetic keys (never printed, never committed).
    The upload value must be a VALID P-256 scalar in [1, order-1] — the env
    value is used DIRECTLY (no raw32 mapping).

    Fixture hygiene (E-lane audit fix): the encrypted keystore lives in a
    SYSTEM temp directory (mkdtemp), NOT this repo — no git tree sees the
    synthetic key file, and the directory is removed at cleanup (rmtree via
    atexit), covering both success and failure exits."""
    import atexit
    import secrets
    import shutil
    import tempfile

    from relay.app.config import P256_ORDER

    tmp_dir = tempfile.mkdtemp(prefix="tokenshare_shared_run_")
    atexit.register(shutil.rmtree, tmp_dir, ignore_errors=True)
    upload = secrets.randbelow(P256_ORDER - 1) + 1
    return {
        "keystore_path": str(Path(tmp_dir) / "keystore.json"),
        "signer_key": "0x" + secrets.token_bytes(32).hex(),
        "kek_hex": "0x" + secrets.token_bytes(32).hex(),
        "upload_hex": "0x" + format(upload, "064x"),
    }


def start_relay_shared(base_env: dict[str, str], deployed: dict[str, Any],
                       rpc_url: str, chain_id: int, relay_port: int,
                       keys: dict[str, str]) -> None:
    """One REAL relay process in RELAY_MODE=shared (fresh or restarted)."""
    relay_env = dict(base_env)
    relay_env.update(
        {
            "RELAY_MODE": "shared",
            "RELAY_PUBLIC_ORIGIN": f"http://127.0.0.1:{relay_port}",
            "ALLOW_INSECURE_ENDPOINT": "1",  # dev/test-only http origin
            "SHARED_KEYSTORE_PATH": keys["keystore_path"],
            "SHARED_SIGNER_KEY": keys["signer_key"],
            "SHARED_KEK_HEX": keys["kek_hex"],
            "SHARED_UPLOAD_KEY": keys["upload_hex"],
            "RPC_URL": rpc_url,
            "CHAIN_ID": str(chain_id),
            "ESCROW_ADDR": deployed["escrow"],
            "REGISTRY_ADDR": deployed["registry"],
            "USDC_ADDR": deployed["usdc"],
            "PORT": str(relay_port),
            "FORWARD_MARGIN_S": "120",
            "ALLOW_CUSTOM_UPSTREAM": "1",  # mock upstream — dev/test flag
            "VERIFY_UPSTREAM_ON_START": "0",
            # The relay-side estimate caps: with the SCALED e2e prices the
            # minAmount estimate must stay BELOW the e2e lock sizes so the
            # per-payment budget (LOCK_USDC_A = 510 USDC) can genuinely be
            # exhausted through served calls.
            "PROMPT_TOKEN_CAP": "1000",
            "COMPLETION_TOKEN_CAP": "320",
        }
    )
    # Single-tenant env MUST NOT leak into a shared boot.
    for name in ("RELAY_SELLER_KEY", "OPENAI_API_KEY", "OPENAI_BASE_URL"):
        relay_env.pop(name, None)
    single_harness.start(
        "relay-shared",
        [sys.executable, "-m", "uvicorn", "relay.app.main:app",
         "--host", "127.0.0.1", "--port", str(relay_port), "--log-level", "info"],
        cwd=REPO_ROOT, env=relay_env, log_path=E2E_DIR / ".relay_shared.log",
    )
    single_harness.wait_http(f"http://127.0.0.1:{relay_port}/health", timeout=60)
    import httpx

    health = httpx.get(f"http://127.0.0.1:{relay_port}/health", timeout=10).json()
    if str(health.get("mode", "")) != "shared":
        shared_fail_all(f"relay /health mode={health.get('mode')} != shared")
    from eth_account import Account

    expect_signer = Account.from_key(keys["signer_key"]).address
    if str(health.get("signer", "")).lower() != expect_signer.lower():
        shared_fail_all(f"relay /health signer={health.get('signer')} != shared signer")
    print(f"relay(shared) up: port={relay_port} signer={health['signer']} "
          f"keystoreEntries={health.get('keystoreEntries')}")


# ---------------------------------------------------------------------------
# minimal ABI fragments (e2e-only; relay/CLI keep their own ABIs)
# ---------------------------------------------------------------------------
DELEGATE_ABI: list[dict[str, Any]] = [
    {
        "type": "function",
        "name": "approveSettleDelegate",
        "stateMutability": "nonpayable",
        "inputs": [{"name": "delegate", "type": "address"}],
        "outputs": [],
    },
]


def approve_settle_delegate(w3: Any, escrow_addr: str, seller_key: str,
                            delegate_addr: str) -> str:
    """Frozen R2 delegation: seller -> approveSettleDelegate(sharedSigner)."""
    escrow = w3.eth.contract(
        address=w3.to_checksum_address(escrow_addr), abi=DELEGATE_ABI
    )
    fn = escrow.functions.approveSettleDelegate(w3.to_checksum_address(delegate_addr))
    return single_harness.send_tx(w3, fn, seller_key)


# ---------------------------------------------------------------------------
# custody enrollment ceremony (R1 verbatim, driven against the REAL relay)
# ---------------------------------------------------------------------------
def fund_eth(w3: Any, from_key: str, to_addr: str, amount_eth: float) -> str:
    """E2E-only: pre-fund a synthetic address with gas ETH (anvil local)."""
    from eth_account import Account

    acct = Account.from_key(from_key)
    tx = {
        "to": w3.to_checksum_address(to_addr),
        "value": w3.to_wei(amount_eth, "ether"),
        "nonce": w3.eth.get_transaction_count(acct.address),
        "gas": 21_000,
        "gasPrice": w3.eth.gas_price,
        "chainId": w3.eth.chain_id,
    }
    signed = acct.sign_transaction(tx)
    raw = getattr(signed, "raw_transaction", None) or signed.rawTransaction
    h = w3.eth.send_raw_transaction(raw)
    receipt = w3.eth.wait_for_transaction_receipt(h, timeout=60.0, poll_latency=1.0)
    if receipt.get("status", 0) != 1:
        shared_fail_all(f"ETH funding reverted: {h.hex()}")
    return h.hex()


def wait_captured(rpc_url: str, escrow_addr: str, payment_id: int,
                  expected: int, timeout_s: float = 30.0) -> int:
    """Wait until the on-chain cumulative captured reaches `expected`
    (fire-and-forget settlePartial flushes land AFTER the HTTP response)."""
    deadline = time.monotonic() + timeout_s
    last = -1
    while time.monotonic() < deadline:
        last = captured_of_read(rpc_url, escrow_addr, payment_id)
        if last >= expected:
            return last
        time.sleep(0.5)
    shared_fail_all(
        f"on-chain capture did not reach {expected} in {timeout_s}s "
        f"(last={last}, payment {payment_id})")


def start_mock_instance(usage: dict[str, int], model_ids: str, bearer_key: str) -> int:
    """One REAL e2e/mock_openai.py process: catalog face (comma-separated
    ids) + /v1/* bearer allowlist + per-key counters."""
    mock_port = single_harness.pick_free_port()
    single_harness.start(
        "mock_openai",
        [sys.executable, str(E2E_DIR / "mock_openai.py"),
         "--port", str(mock_port),
         "--prompt-tokens", str(usage["prompt_tokens"]),
         "--cached-tokens", str(usage["cached_tokens"]),
         "--completion-tokens", str(usage["completion_tokens"]),
         "--model-id", model_ids,
         "--require-bearer", bearer_key],
        cwd=REPO_ROOT, env=dict(single_harness.os.environ),
        log_path=E2E_DIR / ".mock_openai_shared.log",
    )
    single_harness.wait_http(f"http://127.0.0.1:{mock_port}/health", timeout=30)
    return mock_port


def enroll_seller(relay_base: str, seller_addr: str, seller_key: str,
                  upstream_base_url: str, catalog_ids: list[str],
                  chain_id: int, escrow_addr: str, registry_addr: str,
                  relay_upload_hex: str, *, credential: str) -> dict[str, Any]:
    """Wallet-authenticated ENCRYPTED custody enrollment (R1 ceremony): the
    seller signs the EXACT submit message (relay domain = the nonce
    response's origin — server authority) over the exact canonical body; the
    upstream api key travels encrypted (ECDH-P256 envelope against the
    relay's upload key). Returns the 200 response."""
    import secrets

    import httpx
    from eth_account import Account
    from eth_account.messages import encode_defunct
    from relay.app import custody
    from relay.app.config import P256_ORDER

    nonce_resp = httpx.get(f"{relay_base}/sellers/nonce/{seller_addr.lower()}", timeout=10.0)
    if nonce_resp.status_code != 200:
        shared_fail_all(f"nonce issuance failed: HTTP {nonce_resp.status_code} {nonce_resp.text}")
    nonce = nonce_resp.json()
    if str(nonce.get("mode")) != "shared":
        shared_fail_all(f"nonce mode={nonce.get('mode')} != shared")
    if str(nonce.get("origin")) != relay_base:  # server authority, not Host echo
        shared_fail_all(f"nonce origin {nonce.get('origin')} != relay origin {relay_base}")
    record = {"nonce": nonce["nonce"], "issued_at": int(nonce["issued_at"]),
              "expires_at": int(nonce["expires_at"])}

    # The relay's upload PUBLIC key must match the runtime-generated scalar.
    expect_pub = custody.upload_public_bytes(int(relay_upload_hex, 16))
    info = httpx.get(f"{relay_base}/info", timeout=10.0).json()
    import base64 as _b64

    reported = _b64.urlsafe_b64decode(
        info["shared"]["uploadPubkey"] + "=" * (-len(info["shared"]["uploadPubkey"]) % 4)
    )
    if reported != expect_pub:
        shared_fail_all("/info uploadPubkey does not match the configured upload key")

    # Ephemeral P-256 sender key + encrypted payload (compact ASCII). The
    # upload AAD is the independent envelope string (NO body hash).
    eph = secrets.randbelow(P256_ORDER - 1) + 1
    plaintext = json.dumps({"api_key": credential}, separators=(",", ":")).encode("ascii")
    aad = custody.build_upload_aad(
        seller=seller_addr.lower(), chain_id=chain_id,
        escrow_addr=escrow_addr.lower(), registry_addr=registry_addr.lower(),
        origin=str(nonce["origin"]), upstream_base_url=upstream_base_url,
        nonce=record["nonce"], issued=record["issued_at"], expires=record["expires_at"],
    )
    envelope = custody.encrypt_envelope(int(relay_upload_hex, 16), plaintext, aad, eph_scalar=eph)

    body_obj = {
        "nonce": record["nonce"],
        "issued_at": record["issued_at"],
        "expires_at": record["expires_at"],
        "upstream_base_url": upstream_base_url,
        "envelope": envelope,
    }
    raw = json.dumps(body_obj, separators=(",", ":"), ensure_ascii=True).encode("ascii")
    message = custody.build_submit_message(
        seller=seller_addr.lower(), chain_id=chain_id,
        escrow_addr=escrow_addr.lower(), registry_addr=registry_addr.lower(),
        origin=str(nonce["origin"]), upstream_base_url=upstream_base_url,
        nonce=record["nonce"], body_sha256=custody.sha256_hex(raw),
        issued=record["issued_at"], expires=record["expires_at"],
    )
    sig = Account.from_key(seller_key).sign_message(encode_defunct(text=message)).signature.hex()
    headers = {
        "X-Tokenshare-Seller": seller_addr.lower(),
        "X-Tokenshare-Signature": sig if sig.startswith("0x") else "0x" + sig,
        "Origin": str(nonce["origin"]),  # own-relay origin — allowed face
    }
    resp = httpx.post(f"{relay_base}/sellers/keys", content=raw, headers=headers, timeout=30.0)
    if resp.status_code != 200:
        shared_fail_all(f"custody submit failed: HTTP {resp.status_code} {resp.text[:500]}")
    body = resp.json()
    if body.get("stored") is not True or str(body.get("seller", "")).lower() != seller_addr.lower():
        shared_fail_all(f"custody submit unexpected body: {body}")
    return body


def custody_counters(relay_base: str, mock1: int, mock2: int) -> tuple[dict, dict]:
    import httpx

    s1 = httpx.get(f"http://127.0.0.1:{mock1}/counters", timeout=10.0).json()
    s2 = httpx.get(f"http://127.0.0.1:{mock2}/counters", timeout=10.0).json()
    return s1, s2


def bearer_chat(relay_base: str, api_key: str, model: str, *, stream: bool = False):
    import httpx

    payload = {"model": model, "messages": [{"role": "user", "content": "ping"}], "stream": stream}
    return httpx.post(
        f"{relay_base}/v1/chat/completions",
        headers={"Authorization": f"Bearer {api_key}"},
        json=payload,
        timeout=60.0,
    )


def legacy_chat(relay_base: str, buyer_key: str, payment_id: int, model: str):
    import hashlib

    import httpx
    from eth_account import Account
    from eth_account.messages import encode_defunct

    raw = json.dumps(
        {"model": model, "messages": [{"role": "user", "content": "legacy-hello"}]}
    ).encode()
    msg = f"POST|/v1/chat/completions|{hashlib.sha256(raw).hexdigest()}|{payment_id}"
    sig = Account.from_key(buyer_key).sign_message(encode_defunct(text=msg)).signature.hex()
    return httpx.post(
        f"{relay_base}/v1/chat/completions",
        headers={"X-Payment-Id": str(payment_id), "X-Signature": sig},
        content=raw,
        timeout=60.0,
    )


def buyer_revoke(relay_base: str, buyer_key: str, payment_id: int, expires_at: int):
    import httpx
    from eth_account import Account
    from eth_account.messages import encode_defunct

    message = f"TokenShare API key revoke|paymentId={payment_id}|expiry={expires_at}"
    sig = Account.from_key(buyer_key).sign_message(encode_defunct(text=message)).signature.hex()
    return httpx.post(
        f"{relay_base}/payment/{payment_id}/revoke",
        json={"message": message, "signature": sig},
        timeout=30.0,
    )


def captured_of_read(rpc_url: str, escrow_addr: str, payment_id: int) -> int:
    """On-chain cumulative captured via the REAL relay chain client
    (ABI-free PIN selector); a fresh synthetic valueless account key is
    generated per call (never printed)."""
    import secrets

    from relay.app.chain import ChainClient

    cc = ChainClient(
        rpc_url=rpc_url,
        escrow_addr=escrow_addr,
        registry_addr=ZERO_ADDRESS,  # no registry call made
        seller_key="0x" + secrets.token_bytes(32).hex(),
        chain_id=CHAIN_ID_LOCAL,
    )
    return cc.captured_of(payment_id)


# ---------------------------------------------------------------------------
# forge deploy (current tracked sources; fee-free demo; artifact snapshot)
# ---------------------------------------------------------------------------
_ARTIFACT_BACKUP: str | None = None


def forge_deploy_shared(rpc_url: str, deployer_addr: str, deployer_key: str) -> dict[str, Any]:
    global _ARTIFACT_BACKUP
    forge = single_harness.resolve_tool("forge")
    env = single_harness.foundry_env(dict(single_harness.os.environ))
    env.pop("USDC_ADDR", None)  # empty -> fresh MockUSDC (usdcIsMock=true)
    env["FEE_BPS"] = str(FEE_BPS_LOCAL)  # fee-free demo credit asserts
    artifact_path = single_harness.CONTRACTS_DIR / "deployed.json"
    try:
        _ARTIFACT_BACKUP = artifact_path.read_text()
    except OSError:
        _ARTIFACT_BACKUP = None
    cmd = [
        forge, "script", "script/Deploy.s.sol",
        "--sig", "run(string)", "local_shared",
        "--rpc-url", rpc_url,
        "--broadcast",
        "--private-key", deployer_key,
        "--sender", deployer_addr,
        "--force",
    ]
    try:
        result = subprocess.run(cmd, cwd=str(single_harness.CONTRACTS_DIR), env=env,
                                capture_output=True, text=True, timeout=300)
    except subprocess.TimeoutExpired as exc:
        shared_fail_all(f"forge script deploy timed out: {exc}")
    (E2E_DIR / ".forge_deploy_shared.log").write_text(
        (result.stdout or "") + "\n" + (result.stderr or ""), errors="replace"
    )
    if result.returncode != 0:
        print((result.stdout or "")[-3000:] or (result.stderr or "")[-3000:])
        shared_fail_all(f"forge script deploy exited {result.returncode} (log: e2e/.forge_deploy_shared.log)")
    deadline = time.monotonic() + 120
    while time.monotonic() < deadline:
        try:
            fresh_text = artifact_path.read_text()
            artifact = json.loads(fresh_text)
            if artifact.get("escrow") and fresh_text != (_ARTIFACT_BACKUP or ""):
                print(f"deployed.json updated: escrow={artifact['escrow']} "
                      f"registry={artifact['registry']} usdc={artifact['usdc']}")
                return artifact
        except (OSError, json.JSONDecodeError):
            pass
        time.sleep(0.5)
    shared_fail_all("contracts/deployed.json not written / invalid after deploy")


def recover_artifact() -> None:
    """Safe recovery of MY OWN deploy side effect (ignored deployed.json):
    restore the pre-run snapshot, or remove the file when none existed.
    Unrelated resources are never touched."""
    path = single_harness.CONTRACTS_DIR / "deployed.json"
    try:
        if _ARTIFACT_BACKUP is None:
            if path.exists():
                path.unlink()
        else:
            path.write_text(_ARTIFACT_BACKUP)
    except OSError:
        pass


single_harness.atexit.register(recover_artifact)


# ---------------------------------------------------------------------------
# the frozen R2 scenario
# ---------------------------------------------------------------------------
def run_shared_scenario() -> None:
    import os

    import httpx

    step("[1/7] anvil pure-local (chainId 31337 — no fork, no external RPC)")
    anvil = single_harness.resolve_tool("anvil")
    anvil_port = single_harness.pick_free_port(int(os.environ.get("ANVIL_PORT", "8545")))
    single_harness.start(
        "anvil-local", [anvil, "--port", str(anvil_port)],
        cwd=REPO_ROOT, env=single_harness.foundry_env(dict(os.environ)),
        log_path=E2E_DIR / ".anvil_shared.log",
    )
    rpc_url = f"http://127.0.0.1:{anvil_port}"
    deadline = time.monotonic() + 90
    while time.monotonic() < deadline:
        try:
            if single_harness.rpc_chain_id(rpc_url) == CHAIN_ID_LOCAL:
                break
        except Exception:
            pass
        time.sleep(1.0)
    else:
        shared_fail_all("anvil pure-local did not become ready in 90s")
    print(f"anvil up: {rpc_url} (chainId {CHAIN_ID_LOCAL})")

    # Roles (anvil public default accounts — valueless; keys never printed).
    deployer_addr, deployer_key = single_harness.anvil_account(0)
    buyer_addr, buyer_key = single_harness.anvil_account(1)
    seller_a_addr, seller_a_key = single_harness.anvil_account(2)
    seller_b_addr, seller_b_key = single_harness.anvil_account(3)
    shared_signer_addr, _shared_signer_key_unused = single_harness.anvil_account(4)
    probe_p_addr, probe_p_key = single_harness.anvil_account(5)
    probe_q_addr, _probe_q_key_unused = single_harness.anvil_account(6)
    print("anvil public test accounts (NO real assets): "
          f"deployer={deployer_addr} buyer={buyer_addr} sellerA={seller_a_addr} "
          f"sellerB={seller_b_addr} sharedSigner={shared_signer_addr}")

    step("[2/7] forge deploy: current sources (Escrow settle-delegation face + Registry v4 + MockUSDC, fee-free)")
    deployed = forge_deploy_shared(rpc_url, deployer_addr, deployer_key)
    escrow_addr = deployed["escrow"]

    step("[3/7] direct web3: mint buyer USDC + approveSettleDelegate(relay shared signer) x2")
    # The relay's shared signer key is generated HERE (before the approvals —
    # the sellers must approve exactly the signer this relay will use).
    keys = generate_shared_keys()
    from eth_account import Account as _A

    relay_signer = _A.from_key(keys["signer_key"]).address
    w3 = single_harness.w3_at(rpc_url)
    mint_fn = w3.eth.contract(
        address=w3.to_checksum_address(deployed["usdc"]),
        abi=[{"type": "function", "name": "mint", "stateMutability": "nonpayable",
              "inputs": [{"name": "to", "type": "address"}, {"name": "amount", "type": "uint256"}],
              "outputs": []}],
    ).functions.mint(w3.to_checksum_address(buyer_addr), int(DEPOSIT_USDC) * 10**6)
    single_harness.send_tx(w3, mint_fn, deployer_key)
    fund_eth(w3, deployer_key, relay_signer, 10.0)  # gas for the delegate settle txs
    approve_settle_delegate(w3, escrow_addr, seller_a_key, relay_signer)
    approve_settle_delegate(w3, escrow_addr, seller_b_key, relay_signer)
    print(f"minted {DEPOSIT_USDC} USDC -> buyer; both sellers approved the relay's "
          f"shared signer {relay_signer}")

    step("[4/7] two mock upstreams (per-seller catalog faces + /v1/* bearer allowlists + counters)")
    import secrets

    key_a = f"sk-shared-e2e-a-{secrets.token_hex(6)}"
    key_b = f"sk-shared-e2e-b-{secrets.token_hex(6)}"
    mock1_port = start_mock_instance(USAGE_A, ",".join(CATALOG_A), key_a)
    mock2_port = start_mock_instance(USAGE_B, ",".join(CATALOG_B), key_b)
    upstream_a = f"http://127.0.0.1:{mock1_port}"
    upstream_b = f"http://127.0.0.1:{mock2_port}"

    step("[5/7] relay (REAL HTTP process, RELAY_MODE=shared) + restart twin")
    relay_port = single_harness.pick_free_port()
    relay_base = f"http://127.0.0.1:{relay_port}"
    start_relay_shared(dict(os.environ), deployed, rpc_url, CHAIN_ID_LOCAL, relay_port, keys)

    # ---- segment a: ENCRYPTED custody enrollment BEFORE listings ----------
    step("[6/7] segment a: custody enrollment A + B (prelisting; encrypted envelopes)")
    resp_a = enroll_seller(relay_base, seller_a_addr, seller_a_key, upstream_a, CATALOG_A,
                           CHAIN_ID_LOCAL, escrow_addr, deployed["registry"], keys["upload_hex"],
                           credential=key_a)
    assert resp_a["listing_bound"] is False and resp_a["delegate_authorized"] is False, resp_a
    resp_b = enroll_seller(relay_base, seller_b_addr, seller_b_key, upstream_b, CATALOG_B,
                           CHAIN_ID_LOCAL, escrow_addr, deployed["registry"], keys["upload_hex"],
                           credential=key_b)
    assert resp_b["listing_bound"] is False and resp_b["delegate_authorized"] is False, resp_b
    print("both sellers enrolled (encrypted at rest; prelisting face proven)")

    # ---- segment b: listings (same trusted relay endpoint, different prices)
    register_listing_fn_a = single_harness.registry_register(
        w3, deployed["registry"], relay_base, [MODEL_A],
        [{"cached": PRICES_A[0], "input": PRICES_A[1], "output": PRICES_A[2]}],
    )
    single_harness.send_tx(w3, register_listing_fn_a, seller_a_key)
    register_listing_fn_b = single_harness.registry_register(
        w3, deployed["registry"], relay_base, [MODEL_B],
        [{"cached": PRICES_B[0], "input": PRICES_B[1], "output": PRICES_B[2]}],
    )
    single_harness.send_tx(w3, register_listing_fn_b, seller_b_key)
    print(f"listings registered: A={MODEL_A}@{PRICES_A} B={MODEL_B}@{PRICES_B} "
          f"(endpoint {relay_base} for both)")

    # ---- segment c: buyer deposit + locks + Bearer calls (per-seller price)
    step("[7/7] buyer segments: deposit -> locks -> Bearer/legacy/stream -> 424 gates -> revoke -> refund -> restart")
    buyer_env = single_harness.cli_env(dict(os.environ), deployed, rpc_url, CHAIN_ID_LOCAL,
                                        buyer_key, seller_a_addr)
    single_harness.run_cli(["deposit", "--amount", DEPOSIT_USDC], buyer_env)
    lock_out = single_harness.run_cli(["lock", "--seller", seller_a_addr,
                                        "--max", LOCK_USDC_A, "--ttl", str(LOCK_TTL_S)], buyer_env)
    pid_a = int(single_harness.parse_cli_value(lock_out, "paymentId"))
    lock_out = single_harness.run_cli(["lock", "--seller", seller_b_addr,
                                        "--max", LOCK_USDC_B, "--ttl", str(LOCK_TTL_S)], buyer_env)
    pid_b = int(single_harness.parse_cli_value(lock_out, "paymentId"))
    mint_out = single_harness.run_cli(["mint-key", "--payment-id", str(pid_b), "--json"], buyer_env)
    api_key_b = str(json.loads(mint_out.strip())["apiKey"])

    actual_a = single_harness.pin_actual(
        {"cached": PRICES_A[0], "input": PRICES_A[1], "output": PRICES_A[2]},
        USAGE_A["prompt_tokens"], USAGE_A["cached_tokens"], USAGE_A["completion_tokens"],
    )
    actual_b = single_harness.pin_actual(
        {"cached": PRICES_B[0], "input": PRICES_B[1], "output": PRICES_B[2]},
        USAGE_B["prompt_tokens"], USAGE_B["cached_tokens"], USAGE_B["completion_tokens"],
    )
    print(f"pin settle amounts: A={actual_a} (pid {pid_a}) B={actual_b} (pid {pid_b})")

    def seller_bal(addr: str) -> int:
        return single_harness.escrow_balances(rpc_url, deployed, buyer_addr, addr)["seller_escrow"]

    bal_b0 = seller_bal(seller_b_addr)
    resp = bearer_chat(relay_base, api_key_b, MODEL_B)
    assert resp.status_code == 200, resp.text
    assert resp.headers["X-Settle-Status"].startswith("partial-flush"), resp.headers
    receipt = decode_x_receipt(resp.headers["X-Receipt"])
    assert receipt["message"]["seller"] == seller_b_addr
    assert receipt["message"]["paymentId"] == pid_b
    assert receipt["message"]["actualAmount"] == actual_b

    # seller B's escrow balance delta == actual_b (fee-free full credit).
    wait_captured(rpc_url, escrow_addr, pid_b, actual_b)
    assert seller_bal(seller_b_addr) - bal_b0 == actual_b
    print(f"chain capture verified: sellerB balance +{actual_b} (gross, fee-free)")

    # ---- seller A: lock + Bearer call at A's OWN price face ---------------
    mint_out = single_harness.run_cli(["mint-key", "--payment-id", str(pid_a), "--json"], buyer_env)
    api_key_a = str(json.loads(mint_out.strip())["apiKey"])
    bal_a0 = seller_bal(seller_a_addr)
    resp = bearer_chat(relay_base, api_key_a, MODEL_A)
    assert resp.status_code == 200, resp.text
    receipt = decode_x_receipt(resp.headers["X-Receipt"])
    assert receipt["message"]["seller"] == seller_a_addr
    assert receipt["message"]["paymentId"] == pid_a
    assert receipt["message"]["actualAmount"] == actual_a
    wait_captured(rpc_url, escrow_addr, pid_a, actual_a)
    assert seller_bal(seller_a_addr) - bal_a0 == actual_a
    print(f"chain capture verified: sellerA balance +{actual_a} (A's own price face)")

    # Cross-seller price non-contamination: A's delta face (actual_a) != B's
    # delta face (actual_b) — pricing followed each seller's Registry triple.
    assert actual_a != actual_b

    # ---- BUDGET GATE (relay's own admission 402, BEFORE any upstream
    # quota — separate from the seller-preflight 424 X-Error-Code gates) --
    # pid_a (510 USDC): call2 serves the CLAMPED remainder (capture
    # 200000000 native = 200 USDC; cumulative 510000000 = 510 USDC); call3
    # must 402 "payment budget exhausted" (chain_remaining == 0 < minAmount)
    # with ZERO upstream quota consumed.
    resp = bearer_chat(relay_base, api_key_a, MODEL_A)
    assert resp.status_code == 200, resp.text  # clamped remainder call
    receipt = decode_x_receipt(resp.headers["X-Receipt"])
    assert receipt["message"]["actualAmount"] == actual_a  # unclamped receipt face
    wait_captured(rpc_url, escrow_addr, pid_a, int(LOCK_USDC_A) * 10**6)  # cum == cap
    counters_before_gate = custody_counters(relay_base, mock1_port, mock2_port)
    resp = bearer_chat(relay_base, api_key_a, MODEL_A)
    assert resp.status_code == 402, resp.text
    detail = resp.json().get("detail", {})
    assert detail.get("error") == "payment budget exhausted", detail
    assert detail.get("remaining") == 0, detail
    counters_after_gate = custody_counters(relay_base, mock1_port, mock2_port)
    assert counters_after_gate == counters_before_gate, (counters_before_gate, counters_after_gate)
    print(f"budget gate proven: 402 payment-budget-exhausted remaining={detail['remaining']}, "
          f"ZERO upstream quota")

    # ---- segment d: receipt pin (economic seller != shared signer) --------
    stored = json.loads(httpx.get(f"{relay_base}/receipt/{pid_a}", timeout=10.0).text)
    # E-lane READ-ONLY use of the CLI receipt lib: PYTHONPATH + the relay's
    # own encoder round-trip the stored receipt into the X-Receipt face.
    sys.path.insert(0, str(REPO_ROOT / "cli"))
    from relay.app.receipt import encode_x_receipt
    from cli.tokenshare_cli.receipt import decode_receipt, verify_receipt

    cli_receipt = decode_receipt(encode_x_receipt(stored))
    ok = verify_receipt(cli_receipt, expected_seller=seller_a_addr,
                        expected_payment_id=pid_a, expected_chain_id=CHAIN_ID_LOCAL,
                        expected_signer=relay_signer)  # pin the RELAY's signer
    assert ok.ok, f"pinned verification failed: {ok.reason}"
    # WITHOUT a pin the CLI verifies against the listing operator — the
    # receipt signer is the INDEPENDENT shared signer → REJECTED.
    ok_unpinned = verify_receipt(cli_receipt, expected_seller=seller_a_addr,
                                  expected_payment_id=pid_a, expected_chain_id=CHAIN_ID_LOCAL)
    assert not ok_unpinned.ok and ok_unpinned.reason == "recover-mismatch", ok_unpinned
    print(f"receipt pin proven: recover={ok.recovered} (shared signer) "
          f"!= economic seller {seller_a_addr}; unpinned verification rejected")

    # ---- segment e: legacy path + stream path -----------------------------
    lock_out = single_harness.run_cli(["lock", "--seller", seller_a_addr,
                                        "--max", "320", "--ttl", str(LOCK_TTL_S)], buyer_env)
    pid_f = int(single_harness.parse_cli_value(lock_out, "paymentId"))
    resp = legacy_chat(relay_base, buyer_key, pid_f, MODEL_A)
    assert resp.status_code == 200, resp.text
    assert resp.headers["X-Settle-Status"] == "settled"  # legacy one-shot
    receipt = decode_x_receipt(resp.headers["X-Receipt"])
    assert receipt["message"]["seller"] == seller_a_addr  # ACTUAL seller
    with httpx.stream(
        "POST", f"{relay_base}/v1/chat/completions",
        headers={"Authorization": f"Bearer {api_key_b}"},
        json={"model": MODEL_B, "messages": [{"role": "user", "content": "stream-hello"}],
              "stream": True},
        timeout=60.0,
    ) as stream_resp:
        assert stream_resp.status_code == 200, (
            stream_resp.status_code, stream_resp.read()[:300])
        sse = b"".join(stream_resp.iter_bytes())
    assert b"[DONE]" in sse
    receipt = json.loads(httpx.get(f"{relay_base}/receipt/{pid_b}", timeout=10.0).text)
    assert receipt["message"]["seller"] == seller_b_addr
    print("legacy (settled, actual seller) + stream (SSE drained, actual seller) exercised")

    # ---- segment f: 424 gates with ZERO upstream quota --------------------
    s1_before, s2_before = custody_counters(relay_base, mock1_port, mock2_port)

    # f1: payment bound to an UNENROLLED seller (probe Q — no custody entry).
    mint_fn_q = w3.eth.contract(
        address=w3.to_checksum_address(deployed["usdc"]),
        abi=[{"type": "function", "name": "mint", "stateMutability": "nonpayable",
              "inputs": [{"name": "to", "type": "address"}, {"name": "amount", "type": "uint256"}],
              "outputs": []}],
    ).functions.mint(w3.to_checksum_address(buyer_addr), 3000 * 10**6)
    single_harness.send_tx(w3, mint_fn_q, deployer_key)
    # The gate locks spend the ESCROW (deposit) balance, not the raw token
    # balance — deposit the freshly minted USDC via the CLI (approve+deposit).
    single_harness.run_cli(["deposit", "--amount", "3000"], buyer_env)
    lock_out = single_harness.run_cli(["lock", "--seller", probe_q_addr,
                                        "--max", "500", "--ttl", str(LOCK_TTL_S)], buyer_env)
    pid_q = int(single_harness.parse_cli_value(lock_out, "paymentId"))
    resp = legacy_chat(relay_base, buyer_key, pid_q, MODEL_A)
    assert resp.status_code == 424, resp.text
    assert resp.headers["X-Error-Code"] == "SELLER_KEY_MISSING"
    assert resp.json()["detail"] == "SELLER_KEY_MISSING"

    # f2: enrolled seller whose LISTING points at a WRONG relay endpoint
    # (probe P: custody entry enrolled, listing endpoint ≠ public_origin).
    resp = enroll_seller(relay_base, probe_p_addr, probe_p_key, upstream_a, CATALOG_A,
                         CHAIN_ID_LOCAL, escrow_addr, deployed["registry"], keys["upload_hex"],
                         credential=key_a)
    lock_out = single_harness.run_cli(["lock", "--seller", probe_p_addr,
                                        "--max", "500", "--ttl", str(LOCK_TTL_S)], buyer_env)
    pid_p = int(single_harness.parse_cli_value(lock_out, "paymentId"))
    register_wrong = single_harness.registry_register(
        w3, deployed["registry"], "http://127.0.0.1:9", [MODEL_A],
        [{"cached": PRICES_A[0], "input": PRICES_A[1], "output": PRICES_A[2]}],
    )
    single_harness.send_tx(w3, register_wrong, probe_p_key)
    resp = legacy_chat(relay_base, buyer_key, pid_p, MODEL_A)
    assert resp.status_code == 424, resp.text
    assert resp.headers["X-Error-Code"] == "LISTING_NOT_BOUND"

    # f3: delegate REVOKED (sellerB approves the zero address) → refused.
    approve_settle_delegate(w3, escrow_addr, seller_b_key, ZERO_ADDRESS)
    lock_out = single_harness.run_cli(["lock", "--seller", seller_b_addr,
                                        "--max", "500", "--ttl", str(LOCK_TTL_S)], buyer_env)
    pid_d = int(single_harness.parse_cli_value(lock_out, "paymentId"))
    resp = legacy_chat(relay_base, buyer_key, pid_d, MODEL_B)
    assert resp.status_code == 424, resp.text
    assert resp.headers["X-Error-Code"] == "DELEGATE_NOT_AUTHORIZED"
    # Restore B's delegation for the later segments (approve the RELAY's
    # shared signer — the delegate the relay actually settles with).
    approve_settle_delegate(w3, escrow_addr, seller_b_key, relay_signer)

    s1_after, s2_after = custody_counters(relay_base, mock1_port, mock2_port)
    assert s1_after["total"] == s1_before["total"] and s2_after["total"] == s2_before["total"], (
        s1_before, s1_after, s2_before, s2_after)
    print("424 gates proven: unenrolled / wrong-endpoint / revoked-delegate all refused "
          "with ZERO upstream quota")

    # ---- segment g: buyer revoke BEFORE any served call on its own pid ----
    counters_pre_revoke = custody_counters(relay_base, mock1_port, mock2_port)
    lock_out = single_harness.run_cli(["lock", "--seller", seller_b_addr,
                                        "--max", "320", "--ttl", str(LOCK_TTL_S)], buyer_env)
    pid_d = int(single_harness.parse_cli_value(lock_out, "paymentId"))
    mint_out = single_harness.run_cli(["mint-key", "--payment-id", str(pid_d), "--json"], buyer_env)
    api_key_d = str(json.loads(mint_out.strip())["apiKey"])
    resp = buyer_revoke(relay_base, buyer_key, pid_d, int(
        single_harness.escrow_payment(rpc_url, deployed, pid_d)["expiresAt"]))
    assert resp.status_code == 200 and resp.json()["revoked"] is True, resp.text
    resp = bearer_chat(relay_base, api_key_d, MODEL_B)
    assert resp.status_code == 401 and resp.json()["detail"] == "revoked", resp.text
    counters_post_revoke = custody_counters(relay_base, mock1_port, mock2_port)
    assert counters_post_revoke == counters_pre_revoke, counters_post_revoke
    print("bearer authority revoked: original key now 401 'revoked' (zero upstream)")

    # Stream face (pid_b, seller B): SSE drained, usage from the final
    # chunk, cumulative capture — then it is the refundable remainder face.
    import httpx as _hx

    with _hx.stream(
        "POST", f"{relay_base}/v1/chat/completions",
        headers={"Authorization": f"Bearer {api_key_b}"},
        json={"model": MODEL_B, "messages": [{"role": "user", "content": "stream-hi"}],
              "stream": True},
        timeout=60.0,
    ) as stream_resp:
        assert stream_resp.status_code == 200, (
            stream_resp.status_code, stream_resp.read()[:300])
        sse = b"".join(stream_resp.iter_bytes())
    assert b"[DONE]" in sse
    receipt = json.loads(httpx.get(f"{relay_base}/receipt/{pid_b}", timeout=10.0).text)
    assert receipt["message"]["seller"] == seller_b_addr
    wait_captured(rpc_url, escrow_addr, pid_b, actual_b * 2)  # cumulative 2x served
    print(f"stream face exercised on pid_b (cumulative captured {actual_b * 2})")

    # ---- segment h: refund the UNCONSUMED remainder -----------------------
    # (pid_a was SETTLED by the legacy one-shot face — terminal on-chain;
    #  the refund must target a STILL-LOCKED, partially captured payment:
    #  pid_b — captured but revoked; refund is a buyer chain action.
    #  Validated amounts: captured 240000000 native (= 240 USDC) across the
    #  three settlePartial flushes; remainder 560000000 native
    #  (= 560 USDC) credited to the buyer in full — refunds never charged.)
    pay_b = single_harness.escrow_payment(rpc_url, deployed, pid_b)
    expires_b = int(pay_b["expiresAt"])
    warp_hard(rpc_url, expires_b)  # persistent clock verify (strictly > target)
    remainder = int(LOCK_USDC_B) * 10**6 - captured_gross_a(rpc_url, escrow_addr, pid_b)
    buyer_before = single_harness.escrow_balances(rpc_url, deployed, buyer_addr, seller_b_addr)["buyer_escrow"]
    single_harness.send_tx(
        w3,
        w3.eth.contract(
            address=w3.to_checksum_address(escrow_addr),
            abi=[{"type": "function", "name": "refund", "stateMutability": "nonpayable",
                  "inputs": [{"name": "paymentId", "type": "uint256"}], "outputs": []}],
        ).functions.refund(pid_b),
        buyer_key,
    )
    buyer_after = single_harness.escrow_balances(rpc_url, deployed, buyer_addr, seller_b_addr)["buyer_escrow"]
    assert buyer_after - buyer_before == remainder, (buyer_before, buyer_after, remainder)
    state = single_harness.escrow_payment(rpc_url, deployed, pid_b)["state"]
    assert int(state) == 3, f"payment {pid_b} not Refunded (state={state})"
    print(f"refund verified (on-chain): buyer credited EXACTLY maxAmount - captured "
          f"({remainder}); state=Refunded")

    # ---- segment i: RESTART continuity (encrypted keystore + signer) ------
    step("restart segment: kill relay -> NEW process reusing the encrypted keystore")
    for p in reversed(single_harness.procs):
        if p.name == "relay-shared":
            p.terminate()
            single_harness.procs.remove(p)
    start_relay_shared(dict(os.environ), deployed, rpc_url, CHAIN_ID_LOCAL, relay_port, keys)
    status = json.loads(httpx.get(f"{relay_base}/sellers/{seller_a_addr.lower()}/status", timeout=10.0).text)
    assert status["has_key"] is True and status["mode"] == "shared", status  # NO re-enrollment
    # capturedOf recovery + ORIGINAL bearer: a fresh lock bound to A, then
    # exhaust it fully across the restart boundary.
    lock_out = single_harness.run_cli(["lock", "--seller", seller_a_addr,
                                        "--max", "620", "--ttl", str(LOCK_TTL_S)], buyer_env)
    pid_c = int(single_harness.parse_cli_value(lock_out, "paymentId"))
    mint_out = single_harness.run_cli(["mint-key", "--payment-id", str(pid_c), "--json"], buyer_env)
    api_key_c = str(json.loads(mint_out.strip())["apiKey"])
    resp = bearer_chat(relay_base, api_key_c, MODEL_A)
    assert resp.status_code == 200, resp.text  # pre-restart served call
    captured_pre = captured_gross_a(rpc_url, escrow_addr, pid_c)
    assert captured_pre == actual_a

    for p in reversed(single_harness.procs):
        if p.name == "relay-shared":
            p.terminate()
            single_harness.procs.remove(p)
    start_relay_shared(dict(os.environ), deployed, rpc_url, CHAIN_ID_LOCAL, relay_port, keys)
    # POST-RESTART: the ORIGINAL bearer works with NO key re-transmission.
    # The relay's capturedOf seed recovers the pre-restart capture
    # (310000000 native = 310 USDC); the call captures EXACTLY the remaining
    # budget (never over budget): cumulative 620000000 native = 620 USDC
    # against the pid_c lock of 620 USDC.
    resp = bearer_chat(relay_base, api_key_c, MODEL_A)
    assert resp.status_code == 200, resp.text  # capturedOf seeded from the chain
    captured_post = captured_gross_a(rpc_url, escrow_addr, pid_c)
    assert captured_post == int(620) * 10**6, captured_post  # fully exhausted to the cap
    print("restart continuity verified: original bearer + encrypted keystore + "
          "capturedOf recovery captured the EXACT remaining budget "
          f"(cumulative {captured_post}); no re-enrollment, no over-spend")

    # ---- segment j: cross-seller credential non-contamination (counters) --
    s1, s2 = custody_counters(relay_base, mock1_port, mock2_port)
    assert all(k != key_b for k in s1["perKey"]), s1  # mock1 never saw B's key
    assert all(k != key_a for k in s2["perKey"]), s2  # mock2 never saw A's key
    assert s1["perKey"].get(key_a, 0) >= 2, s1  # mock1 served A's calls
    assert s2["perKey"].get(key_b, 0) >= 1, s2  # mock2 served B's calls
    print(f"contamination evidence: {s1['perKey']} | {s2['perKey']}")

    # ---- tracked deployment snapshot unchanged ---------------------------
    monad_path = single_harness.CONTRACTS_DIR / "deployed.monad.json"
    h_after = hashlib.sha256(monad_path.read_bytes()).hexdigest()
    if h_after != _MONAD_HASH_BEFORE:
        shared_fail_all("tracked contracts/deployed.monad.json CHANGED — E lane must never touch it")
    print(f"tracked deployed.monad.json unchanged (sha256 {h_after[:16]}…)")


def warp_hard(rpc_url: str, target_ts: int) -> None:
    """Pure-local anvil variant of the fork warp: the chain's latest block
    timestamp only advances when a tx is mined, so a single evm_increaseTime
    + evm_mine can leave the clock up to ~1s SHORT of the refund target.
    Increase once, then mine blocks in a loop until the chain clock is
    strictly PAST the target."""
    import httpx

    def rpc(method: str, params: list[Any]) -> Any:
        resp = httpx.post(
            rpc_url, json={"jsonrpc": "2.0", "method": method, "params": params, "id": 1},
            timeout=10.0,
        )
        body = resp.json()
        if "error" in body:
            shared_fail_all(f"anvil RPC {method} failed: {body['error']}")
        return body.get("result")

    latest = rpc("eth_getBlockByNumber", ["latest", False]) or {}
    chain_now = int(latest.get("timestamp", "0x0"), 16)
    delta = target_ts + 2 - chain_now
    if delta > 0:
        rpc("evm_increaseTime", [delta])
    for _ in range(10):
        rpc("evm_mine", [])
        latest = rpc("eth_getBlockByNumber", ["latest", False]) or {}
        chain_now = int(latest.get("timestamp", "0x0"), 16)
        if chain_now > target_ts:
            print(f"anvil clock warped to {chain_now} (target {target_ts})")
            return
    shared_fail_all(f"anvil clock never passed {target_ts} (last {chain_now})")


def captured_gross_a(rpc_url: str, escrow_addr: str, payment_id: int) -> int:
    return captured_of_read(rpc_url, escrow_addr, payment_id)


def _monad_hash_before() -> str:
    monad_path = single_harness.CONTRACTS_DIR / "deployed.monad.json"
    return hashlib.sha256(monad_path.read_bytes()).hexdigest()


_MONAD_HASH_BEFORE = _monad_hash_before()


def main() -> None:
    step("TokenShare LOCAL SHARED integration (M15 R2) — pure-local anvil")
    try:
        run_shared_scenario()
    except SystemExit:
        raise
    except KeyboardInterrupt:
        single_harness.cleanup()
        shared_fail("interrupted")
    except Exception as exc:  # noqa: BLE001
        import traceback

        traceback.print_exc()
        single_harness.cleanup()
        shared_fail(f"unexpected error: {exc}")
    single_harness.cleanup()
    print("\nE2E SHARED PASSED", flush=True)


if __name__ == "__main__":
    main()



