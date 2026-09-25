"""TokenShare seller relay — FastAPI application.

Request pipeline (BUILD_SPEC §6.2, PIN-verbatim semantics):

    1. headers X-Payment-Id + X-Signature → EIP-191 recover (pure CPU, no
       chain reads; 401 missing/bad signature); then one get_payment read and
       the signer-is-buyer check (402 not the buyer)
    2. Registry listing active check + model allow-list (400)
    3. TTL margin guard: expiresAt - now < FORWARD_MARGIN_S (409)
    4. Escrow.isValid(paymentId, seller=self, minAmount=estimate) (402) —
       unpaid requests are rejected at zero cost, never forwarded
    5. Forward to OpenAI; stream=true → transparent SSE passthrough with
       stream_options={"include_usage": true} injected; failure → 502, no settle
    6. Price from usage → settle(paymentId, min(actual, maxAmount))
       success → X-Settle-Status: settled + X-Receipt header
       tx failure → X-Settle-Status: settle-failed, response still returned
    7. Any OpenAI failure → never settle (buyer can refund after ttl)

M13 dual auth (PIN): the legacy path above is unchanged. A request carrying
`Authorization: Bearer tsk1.<payload>.<sig>` instead takes the stateless
API-key path (_chat_completions_bearer): EIP-191 recover against the mint
message, hard expiry check, NO 409 margin guard, per-call capture into an
in-memory ledger {paymentId: captured} with settlePartial flush at
maxAmount×0.9 / TTL-window (synchronous) or fire-and-forget (otherwise);
every call still gets an X-Receipt.

All amounts are native USDC units (6 dp). Zero hardcoded addresses/chainIds —
everything comes from env config; the seller key never appears in logs.
"""

from __future__ import annotations

import asyncio
import base64
import hashlib
import json
import logging
import os
import threading
import time
from contextlib import asynccontextmanager
from dataclasses import dataclass
from typing import Any, AsyncIterator

import httpx
from eth_account import Account
from eth_account.messages import encode_defunct
from eth_utils import keccak
from fastapi import FastAPI, HTTPException, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse, StreamingResponse

from .chain import ChainClient, ModelNotFound, NotActive
from .config import (
    MODEL_PROVIDER_PREFIXES,
    OFFICIAL_UPSTREAM_HOSTS,
    TEE_KEY_PATH,
    Config,
    ConfigError,
    ENV_ALLOW_CUSTOM_UPSTREAM,
    ENV_CORS_ORIGINS,
    _normalize_openai_base_url,
    dstack_client,
    host_provider,
    load_config,
    parse_cors_origins,
    provider_for_model,
)
from .pricing import Prices, Usage, clamp_settle_amount, compute_actual, estimate_min_amount
from .receipt import ReceiptStore, build_receipt, encode_x_receipt

logger = logging.getLogger("tokenshare.relay")

CHAT_COMPLETIONS_PATH = "/v1/chat/completions"
MODELS_PATH = "/v1/models"

# Per-request timeout for NON-STREAM forwarding only. Streaming SSE requests
# never set a short read timeout (long-lived responses).
UPSTREAM_TIMEOUT_NON_STREAM = httpx.Timeout(10.0, read=120.0)

# Bounded timeout for the /v1/models key probe (verify-upstream + startup).
VERIFY_UPSTREAM_TIMEOUT = httpx.Timeout(10.0)

# Escrow.State enum values (contracts/src/Escrow.sol).
STATE_LOCKED = 1


class RelayState:
    """Process-wide singletons assembled once at startup (fail-fast)."""

    def __init__(self, config: Config) -> None:
        self.config = config
        self.chain = ChainClient(
            rpc_url=config.rpc_url,
            escrow_addr=config.escrow_addr,
            registry_addr=config.registry_addr,
            seller_key=config.seller_key,
            chain_id=config.chain_id,
        )
        self.receipts = ReceiptStore()
        # M13-D in-memory partial-settle ledger {paymentId: entry}. Resets on
        # restart — see PartialSettleLedger for the conservative semantics.
        self.ledger = PartialSettleLedger()
        # httpx client for OpenAI forwarding. Client-level timeout stays open
        # (streaming responses are long-lived); non-stream requests apply a
        # bounded per-request timeout (UPSTREAM_TIMEOUT_NON_STREAM).
        self.http = httpx.AsyncClient(
            base_url=config.openai_base_url,
            headers={"Authorization": f"Bearer {config.openai_api_key}"},
            timeout=httpx.Timeout(None, connect=10.0),
        )


state: RelayState | None = None


@asynccontextmanager
async def lifespan(_: FastAPI) -> AsyncIterator[None]:
    global state
    config = load_config()  # ConfigError → startup fails, process exits non-zero
    state = RelayState(config)
    logger.info(
        "relay up: chainId=%s seller=%s escrow=%s registry=%s margin=%ss",
        config.chain_id,
        state.chain.seller_address,
        config.escrow_addr,
        config.registry_addr,
        config.forward_margin_s,
    )
    # Startup key probe (fail-fast, same philosophy as the missing-key check):
    # on an OFFICIAL upstream host with VERIFY_UPSTREAM_ON_START!=0, hit
    # GET /v1/models once; an auth rejection (401/403) means the configured
    # key cannot serve anything → ConfigError. Network errors / 5xx are
    # transient → WARNING and continue (never let official-API flakiness
    # kill the relay). Mock/custom upstreams (dev/test) skip the probe so
    # e2e runs and unit tests keep booting without any real network.
    if (
        config.verify_upstream_on_start
        and host_provider(config.openai_base_url) is not None
    ):
        key_valid, _models, probe_error = await _probe_upstream_key(state)
        if key_valid is False:
            await state.http.aclose()
            state = None
            raise ConfigError(
                "upstream API key rejected (probe /v1/models -> "
                f"{probe_error or '401/403'}); refusing to start (no degraded mode) "
                "— fix OPENAI_API_KEY / OPENAI_BASE_URL"
            )
        if probe_error:
            logger.warning(
                "startup upstream probe failed (transient, continuing): %s", probe_error
            )
    try:
        yield
    finally:
        if state is not None:
            await state.http.aclose()
        state = None  # prevent bare app calls from reusing stale config


app = FastAPI(title="TokenShare Seller Relay", version="1", lifespan=lifespan)

# CORS for the browser front-end, which calls the relay directly and must
# READ the response headers X-Receipt / X-Settle-Status from JS — without
# `expose_headers` browsers hide them from the fetch response. Origins come
# from RELAY_CORS_ORIGINS (comma-separated; default "*" — DEMO default, in
# production set the front-end's own domain list). Parsed here at IMPORT
# time: the middleware stack is built before startup, so it cannot wait for
# the lifespan-time config load. Preflight OPTIONS with Origin +
# Access-Control-Request-Method is short-circuited by this middleware before
# any routing (no 405 short-circuit against the GET/POST-only routes).
app.add_middleware(
    CORSMiddleware,
    allow_origins=parse_cors_origins(os.environ.get(ENV_CORS_ORIGINS)),
    allow_methods=["GET", "POST", "OPTIONS"],
    allow_headers=["Authorization", "Content-Type", "X-Payment-Id", "X-Signature"],
    expose_headers=["X-Receipt", "X-Settle-Status"],
)


def _get_state() -> RelayState:
    if state is None:
        raise HTTPException(status_code=503, detail="relay not initialized")
    return state


# ------------------------------------------------------------- upstream verify


async def _probe_upstream_key(
    st: RelayState,
) -> tuple[bool | None, list[str], str | None]:
    """GET /v1/models on the configured upstream with the configured key.

    Returns (key_valid, accessible_models, error):
      - 200            → (True, parsed data[].id list, None) — auth proven;
      - 401/403        → (False, [], detail) — key invalid for this host;
      - other status   → (None, [], "HTTP <n> ...") — transient (5xx etc.);
      - network error  → (None, [], "connect/timeout ...") — transient.

    key_valid=None distinguishes "couldn't tell" (transient) from a definite
    auth verdict, so the startup probe can warn-and-continue instead of
    killing the relay on flaky official API connectivity.
    """
    _probe = MODELS_PATH  # httpx base_url is the host root → /v1/models
    try:
        resp = await st.http.get(_probe, timeout=VERIFY_UPSTREAM_TIMEOUT)
    except httpx.HTTPError as exc:
        return None, [], f"upstream unreachable: {exc}"
    if resp.status_code in (401, 403):
        detail = (resp.text or "").strip()[:500]
        return False, [], detail or f"upstream rejected the key (HTTP {resp.status_code})"
    if resp.status_code == 200:
        try:
            payload = resp.json()
        except ValueError:
            return None, [], "upstream /v1/models returned non-JSON"
        data = payload.get("data") if isinstance(payload, dict) else None
        models: list[str] = []
        if isinstance(data, list):
            models = sorted(
                {
                    m.get("id")
                    for m in data
                    if isinstance(m, dict) and isinstance(m.get("id"), str) and m.get("id")
                }
            )
        return True, models, None
    return None, [], f"HTTP {resp.status_code}: {(resp.text or '')[:300]}"


def _filter_listed_models(listing: dict[str, Any] | None) -> list[str]:
    """listing.models with blanks filtered (same rule as _check_model)."""
    if not listing:
        return []
    return [
        m
        for m in (listing.get("models") or [])
        if isinstance(m, str) and m.strip()
    ]


@app.get("/verify-upstream")
async def verify_upstream() -> dict[str, Any]:
    """Seller pre-check: can the configured key actually serve the models the
    listing promises?

    Semantics live in the BODY (HTTP status stays 200 — the caller screens
    the JSON, not the status code):
      key_valid        key proved by upstream /v1/models (False on 401/403 or
                       unreachable; see `error`)
      accessible_models  ids the upstream reported for this key
      listing_ok       every listed model is accessible AND (official host
                       only) provider-matches the upstream host
      mismatches       listed models that failed a check (with reasons)

    Prices are intentionally NOT verified — pricing is the seller's freedom,
    not an upstream property.
    """
    st = _get_state()
    out: dict[str, Any] = {
        "key_valid": False,
        "upstream_host": _upstream_host(st.config.openai_base_url),
        "accessible_models": [],
        "listing_ok": False,
        "listed_models": [],
        "mismatches": [],
    }

    # 1. Probe the key against the official upstream /v1/models.
    key_valid, accessible, probe_error = await _probe_upstream_key(st)
    out["key_valid"] = bool(key_valid)
    out["accessible_models"] = accessible
    if probe_error:
        out["error"] = probe_error

    # 2. Cross-check the seller's own Registry listing.
    try:
        listing = await asyncio.to_thread(st.chain.get_listing, st.chain.seller_address)
    except Exception as exc:  # chain read failure → listing unverifiable
        out["listing_ok"] = False
        out.setdefault("error", "listing read failed")
        if not probe_error:
            out["error"] = f"listing read failed: {exc}"
        return out

    listed = _filter_listed_models(listing)
    out["listed_models"] = listed
    if listing is None:
        out.setdefault("error", "unregistered: no listing for the relay seller")
        return out
    if not listing.get("active"):
        out.setdefault("error", "listing inactive")
        return out

    accessible_set = set(accessible)
    # Custom upstream (ALLOW_CUSTOM_UPSTREAM=1, dev/test): the host has no
    # provider identity, mirroring the per-request gate — only the
    # accessible-superset half applies.
    custom_upstream = host_provider(st.config.openai_base_url) is None
    mismatches: list[dict[str, str]] = []
    if not accessible_set and listed:
        # Nothing provable about the key (invalid/unreachable/empty catalog):
        # no listed model can be confirmed → every listed model mismatches.
        reason = "key invalid or upstream unreachable — nothing confirmed accessible"
        mismatches = [{"model": model, "reason": reason} for model in listed]
    else:
        for model in listed:
            if model not in accessible_set:
                mismatches.append(
                    {"model": model, "reason": "not accessible with this key"}
                )
            elif not custom_upstream and provider_for_model(model) != host_provider(
                st.config.openai_base_url
            ):
                mismatches.append(
                    {
                        "model": model,
                        "reason": "provider does not match the upstream host",
                    }
                )
    out["mismatches"] = mismatches
    out["listing_ok"] = not mismatches
    return out


# ------------------------------------------------------------- preview-models


@app.post("/preview-models")
async def preview_models(request: Request) -> dict[str, Any]:
    """Seller register-flow probe (M9 PIN, verbatim semantics):

        body: {"upstream_base_url": "https://api.kimi.com/coding", "api_key": "sk-..."}
        → GET {normalized base}/v1/models, Authorization: Bearer <api_key>
          (normalization strips a trailing /v1 — the same rule as forwarding)
        → 200: {"upstream_host": "api.kimi.com", "official": true, "models": [...]}

    Errors: 400 missing body fields / 400 host not on the official allowlist
    (unless ALLOW_CUSTOM_UPSTREAM=1) / 401 upstream 401|403 (key rejected) /
    502 upstream network error or timeout. Timeout 10s.

    KEY DISCIPLINE: api_key is used for this ONE probe and is never stored,
    never logged, never reflected into any response (upstream error bodies
    are intentionally NOT forwarded — they could echo the Authorization
    header back)."""
    _get_state()  # probe is served only by a fully-initialized relay
    try:
        body = await request.json()
    except ValueError as exc:
        raise HTTPException(status_code=400, detail="invalid JSON body") from exc
    if not isinstance(body, dict):
        raise HTTPException(status_code=400, detail="body must be a JSON object")

    base_raw = body.get("upstream_base_url")
    api_key = body.get("api_key")
    if not isinstance(base_raw, str) or not base_raw.strip():
        raise HTTPException(status_code=400, detail="missing upstream_base_url")
    if not isinstance(api_key, str) or not api_key.strip():
        raise HTTPException(status_code=400, detail="missing api_key")

    normalized = _normalize_openai_base_url(base_raw)
    official = host_provider(normalized) is not None
    if not official and os.environ.get(ENV_ALLOW_CUSTOM_UPSTREAM, "").strip() != "1":
        raise HTTPException(
            status_code=400,
            detail=(
                "upstream host is not an official model-provider endpoint "
                f"(allowlist: {sorted(OFFICIAL_UPSTREAM_HOSTS)})"
            ),
        )

    host = _upstream_host(normalized)
    try:
        # One-off client on purpose: st.http is bound to the relay's own
        # configured upstream and key; this endpoint probes an ARBITRARY base
        # with the seller-supplied key. Timeout 10s per the PIN.
        async with httpx.AsyncClient(timeout=VERIFY_UPSTREAM_TIMEOUT) as probe:
            resp = await probe.get(
                f"{normalized}/v1/models",
                headers={"Authorization": f"Bearer {api_key}"},
            )
    except httpx.HTTPError as exc:
        raise HTTPException(
            status_code=502, detail=f"upstream unreachable: {exc}"
        ) from exc

    if resp.status_code in (401, 403):
        # Static detail: never echo the upstream's error body here.
        raise HTTPException(
            status_code=401,
            detail=f"upstream rejected the key (HTTP {resp.status_code})",
        )
    if resp.status_code != 200:
        raise HTTPException(
            status_code=502, detail=f"upstream HTTP {resp.status_code}"
        )
    try:
        payload = resp.json()
    except ValueError as exc:
        raise HTTPException(
            status_code=502, detail="upstream /v1/models returned non-JSON"
        ) from exc
    data = payload.get("data") if isinstance(payload, dict) else None
    models: list[str] = []
    if isinstance(data, list):
        models = sorted(
            {
                m.get("id")
                for m in data
                if isinstance(m, dict) and isinstance(m.get("id"), str) and m.get("id")
            }
        )
    return {"upstream_host": host, "official": official, "models": models}


# --------------------------------------------------------------------- health


@app.get("/health")
def health() -> dict[str, Any]:
    st = _get_state()
    return {
        "status": "ok",
        "chainId": st.config.chain_id,
        "seller": st.chain.seller_address,
    }


# ------------------------------------------------------------------- TEE M7-A


@app.get("/attestation")
async def attestation() -> dict[str, Any]:
    """TDX attestation quote of this relay instance (TEE mode only).

    The quote's report data is bound to the TEE-derived seller address:
    44 zero bytes + the 20-byte address (64 B total, the TDX report-data
    limit) — the same left-padding layout as Solidity
    `bytes32(uint256(uint160(addr)))`. A buyer can therefore check that the
    quote covers exactly the address it pays to.

    Response: {tee, appId, keyPath, derivedAddress, reportData, quote,
    quoteDigest}; quoteDigest = keccak256(raw quote bytes) — the value to
    anchor in AttestationAnchor.sol. Full cryptographic verification of the
    quote needs dstack-verifier (docker) or the Phala cloud-api verify
    endpoint (see relay/README-docker.md).
    """
    st = _get_state()
    if not st.config.tee_mode or not st.config.tee_socket_path:
        raise HTTPException(
            status_code=404,
            detail="relay is not running in TEE mode (no dstack socket detected)",
        )

    derived = st.chain.seller_address
    report_data = b"\x00" * 44 + bytes.fromhex(derived[2:])
    try:
        client = dstack_client(st.config.tee_socket_path)
        quote = client.get_quote(report_data)
        app_id = getattr(client.info(), "app_id", None)
    except Exception as exc:
        raise HTTPException(
            status_code=500, detail=f"dstack attestation failed: {exc}"
        ) from exc

    # dstack-sdk returns the quote as a hex string (GetQuoteResponse.quote);
    # tolerate a raw-bytes response for SDK-version safety.
    raw_quote = getattr(quote, "quote", None)
    if isinstance(raw_quote, (bytes, bytearray)):
        quote_hex = bytes(raw_quote).hex()
    else:
        quote_hex = str(raw_quote or "").strip()
    if quote_hex.startswith("0x"):
        quote_hex = quote_hex[2:]

    quote_digest: str | None = None
    if quote_hex:
        try:
            quote_digest = "0x" + keccak(bytes.fromhex(quote_hex)).hex()
        except ValueError:
            quote_digest = None

    return {
        "tee": True,
        "appId": app_id,
        "keyPath": TEE_KEY_PATH,
        "derivedAddress": derived,
        "reportData": "0x" + report_data.hex(),
        "quote": "0x" + quote_hex if quote_hex else None,
        "quoteDigest": quote_digest,
    }


@app.get("/info")
def info() -> dict[str, Any]:
    """Capability / policy descriptor: TEE state, seller address, and a
    summary of the official-endpoint upstream policy (the anti-poisoning
    guarantee whose enforcement is attested by /attestation)."""
    st = _get_state()
    cfg = st.config
    official = host_provider(cfg.openai_base_url) is not None
    return {
        "name": "TokenShare Seller Relay",
        "version": "1",
        "chainId": cfg.chain_id,
        "seller": st.chain.seller_address,
        "tee": {
            "enabled": cfg.tee_mode,
            "socketPath": cfg.tee_socket_path,
            "keyPath": TEE_KEY_PATH if cfg.tee_mode else None,
        },
        "upstream": {
            "host": _upstream_host(cfg.openai_base_url),
            "official": official,
            "policy": (
                "official-endpoint-only (OFFICIAL_UPSTREAM_HOSTS allowlist)"
                if official
                else "custom upstream (ALLOW_CUSTOM_UPSTREAM=1 — dev/test only, "
                "authenticity guarantee void)"
            ),
        },
        "officialUpstreamHosts": sorted(OFFICIAL_UPSTREAM_HOSTS),
        "modelProviderPrefixes": {
            provider: list(prefixes)
            for provider, prefixes in MODEL_PROVIDER_PREFIXES.items()
        },
    }


# ------------------------------------------------------------------- receipts


@app.get("/receipt/{payment_id}")
def get_receipt(payment_id: int) -> dict[str, Any]:
    """Latest signed receipt for a paymentId — same JSON structure
    {domain, message, signature} as the X-Receipt header payload."""
    st = _get_state()
    receipt = st.receipts.get(payment_id)
    if receipt is None:
        raise HTTPException(status_code=404, detail="no receipt for paymentId")
    return receipt


# ------------------------------------------------------- M13 bearer + partial

# PIN key format: `tsk1.<b64url(payload_json)>.<b64url(sig_hex)>`,
# payload={"p":paymentId,"e":expiry,"m":maxAmount,"b":buyer}.
BEARER_SCHEME = "bearer "
BEARER_KEY_PREFIX = "tsk1."
# PIN mint message (EIP-191 text, C-lane console signs exactly this):
# `TokenShare API key grant|paymentId={p}|expiry={e}|maxAmount={m}`.
BEARER_GRANT_MESSAGE = "TokenShare API key grant"

# Immediate flush threshold: captured >= maxAmount × 0.9 (PIN). Integer math.
_FLUSH_NUM = 9
_FLUSH_DEN = 10

# X-Settle-Status values of the bearer partial path (legacy values untouched):
#   partial-flush-settled  immediate flush tx confirmed
#   partial-flush-failed   immediate flush tx/send failed (response kept)
#   partial-flush-pending  fire-and-forget flush scheduled (post-response)
#   partial-flush-none     nothing flushable this call (dust / budget spent)


class _LedgerEntry:
    __slots__ = ("captured", "pending", "max_amount")

    def __init__(self, max_amount: int) -> None:
        self.captured = 0
        self.pending = 0
        self.max_amount = max_amount


class PartialSettleLedger:
    """In-memory capture ledger {paymentId: entry} for the bearer partial
    path (M13-B/D).

    RESTART SEMANTICS (decided, conservative): the ledger is memory-only; a
    relay restart resets every paymentId's captured to 0. Escrow v2 at PIN
    time exposes no captured getter (getPayment carries no captured field),
    so the relay cannot reseed from chain. Risk, per the M13 PIN: a replayed
    capture after restart can push settlePartial past the contract's own
    captured≤maxAmount cap — that tx reverts and surfaces as
    partial-flush-failed (never swallowing the LLM response; buyer refund
    intact). If the contracts lane later adds a getter, only `captured_of`
    below needs wiring — nothing else touches this class."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._entries: dict[int, _LedgerEntry] = {}

    def plan_capture(self, payment_id: int, actual: int, max_amount: int) -> int:
        """PIN clamp: capture = min(actual, maxAmount - captured); below 1
        native unit nothing is recorded (dust absorbed). Returns the captured
        amount (0 = nothing recorded)."""
        with self._lock:
            entry = self._entries.get(payment_id)
            if entry is None:
                entry = _LedgerEntry(max_amount)
                self._entries[payment_id] = entry
            capture = min(actual, entry.max_amount - entry.captured)
            if capture < 1:
                return 0
            entry.captured += capture
            entry.pending += capture
            return capture

    def take_pending(self, payment_id: int) -> int:
        """Atomically claim the un-flushed amount (0 when nothing pending)."""
        with self._lock:
            entry = self._entries.get(payment_id)
            if entry is None:
                return 0
            amount = entry.pending
            entry.pending = 0
            return amount

    def restore_pending(self, payment_id: int, amount: int) -> None:
        """Put a failed flush back so the next request retries it."""
        with self._lock:
            entry = self._entries.get(payment_id)
            if entry is not None:
                entry.pending += amount

    def captured(self, payment_id: int) -> int:
        with self._lock:
            entry = self._entries.get(payment_id)
            return entry.captured if entry is not None else 0

    def pending(self, payment_id: int) -> int:
        with self._lock:
            entry = self._entries.get(payment_id)
            return entry.pending if entry is not None else 0

    def snapshot(self, payment_id: int) -> tuple[int, int | None]:
        """(captured, max_amount | None) — max_amount None when this process
        never saw the payment (usage endpoint falls back to getPayment)."""
        with self._lock:
            entry = self._entries.get(payment_id)
            if entry is None:
                return 0, None
            return entry.captured, entry.max_amount

    def captured_of(self, payment_id: int) -> int:  # future chain-getter seam
        """Single wiring point if a chain captured getter lands later."""
        return self.captured(payment_id)


@dataclass(frozen=True)
class BearerGrant:
    payment_id: int
    expiry: int
    max_amount: int
    buyer: str
    signature: str


def _b64url_decode(part: str) -> bytes:
    """Strict base64url: non-alphabet characters (validate=True) and bad
    padding raise ValueError — any decode failure maps to 401 upstream."""
    padding = "=" * (-len(part) % 4)
    return base64.b64decode(part + padding, altchars=b"-_", validate=True)


def _grant_int(payload: dict[str, Any], field: str) -> int:
    """Grant payload integer field: JSON int or decimal string (bools and
    anything else rejected → 401, aligned with the X-Payment-Id face)."""
    value = payload.get(field)
    if isinstance(value, bool):
        raise HTTPException(status_code=401, detail=f"api key payload {field!r} invalid")
    if isinstance(value, int) and value >= 0:
        return value
    if isinstance(value, str) and value.isascii() and value.isdigit():
        return int(value)
    raise HTTPException(status_code=401, detail=f"api key payload {field!r} invalid")


def _decode_bearer_key(authorization: str | None) -> BearerGrant:
    """M13-B layer decode: `Authorization: Bearer tsk1.<payload>.<sig>`.

    Any of the three layers failing (scheme/prefix, payload JSON+fields,
    signature hex) → 401 BEFORE any RPC — same 401 semantics as the legacy
    X-Payment-Id path. This validates the ENVELOPE only; the EIP-191 recover,
    the buyer match and the expiry check run in _verify_bearer_grant."""
    header = (authorization or "").strip()
    if not header.lower().startswith(BEARER_SCHEME):
        raise HTTPException(status_code=401, detail="missing bearer api key")
    key = header[len(BEARER_SCHEME):].strip()
    parts = key.split(".")
    if len(parts) != 3 or not key.startswith(BEARER_KEY_PREFIX):
        raise HTTPException(status_code=401, detail="malformed api key")

    # Layer 2: payload JSON {p, e, m, b}.
    try:
        payload = json.loads(_b64url_decode(parts[1]).decode("utf-8"))
    except Exception as exc:
        raise HTTPException(status_code=401, detail="api key payload undecodable") from exc
    if not isinstance(payload, dict):
        raise HTTPException(status_code=401, detail="api key payload must be a JSON object")
    payment_id = _grant_int(payload, "p")
    expiry = _grant_int(payload, "e")
    max_amount = _grant_int(payload, "m")
    buyer = payload.get("b")
    if not isinstance(buyer, str) or not buyer.lower().startswith("0x"):
        raise HTTPException(status_code=401, detail="api key payload buyer invalid")

    # Layer 3: signature (65-byte hex string, 0x optional on the wire).
    try:
        signature = _b64url_decode(parts[2]).decode("ascii").strip()
    except Exception as exc:
        raise HTTPException(status_code=401, detail="api key signature undecodable") from exc
    if not signature.startswith("0x"):
        signature = "0x" + signature

    return BearerGrant(
        payment_id=payment_id,
        expiry=expiry,
        max_amount=max_amount,
        buyer=buyer,
        signature=signature,
    )


def _verify_bearer_grant(grant: BearerGrant, payment: dict[str, Any]) -> None:
    """EIP-191 recover over the PIN mint message; recovered must equal the
    payload buyer AND the on-chain payment buyer (getPayment cross-check).
    Hard expiry: now >= expiry → 401. The legacy 409 FORWARD_MARGIN guard is
    intentionally NOT applied on this path (PIN: the bearer partial mode must
    survive many calls as ttl approaches — expiry replaces the margin)."""
    if int(time.time()) >= grant.expiry:
        raise HTTPException(status_code=401, detail="api key expired")
    msg = (
        f"{BEARER_GRANT_MESSAGE}|paymentId={grant.payment_id}"
        f"|expiry={grant.expiry}|maxAmount={grant.max_amount}"
    )
    try:
        recovered = Account.recover_message(
            encode_defunct(text=msg), signature=grant.signature
        )
    except Exception as exc:
        raise HTTPException(status_code=401, detail="api key signature invalid") from exc
    if recovered.lower() != grant.buyer.lower():
        raise HTTPException(status_code=401, detail="api key signer is not the granted buyer")
    if recovered.lower() != str(payment["buyer"]).lower():
        raise HTTPException(status_code=401, detail="api key buyer does not match payment buyer")


async def _flush_pending_sync(st: RelayState, payment_id: int) -> bool:
    """Immediate flush: settlePartial(pending) on a worker thread, awaited
    BEFORE the response goes out. Failure keeps the pending amount (next
    request retries) and returns False — the response is never swallowed."""
    amount = st.ledger.take_pending(payment_id)
    if amount <= 0:
        return True
    try:
        await asyncio.to_thread(st.chain.settle_partial, payment_id, amount)
        return True
    except Exception:
        logger.exception(
            "partial settle flush failed paymentId=%s amount=%s", payment_id, amount
        )
        st.ledger.restore_pending(payment_id, amount)
        return False


def _flush_pending_bg(st: RelayState, payment_id: int) -> None:
    """Fire-and-forget flush body (its own thread; sync chain calls are fine
    off the event loop). Same failure contract: pending restored, logged."""
    amount = st.ledger.take_pending(payment_id)
    if amount <= 0:
        return
    try:
        st.chain.settle_partial(payment_id, amount)
    except Exception:
        logger.exception(
            "background partial flush failed paymentId=%s amount=%s",
            payment_id,
            amount,
        )
        st.ledger.restore_pending(payment_id, amount)


async def _partial_capture_and_maybe_flush(
    st: RelayState,
    payment_id: int,
    usage: Usage,
    prices: Prices,
    max_amount: int,
    expires_at: int,
    model: str,
) -> tuple[str, dict[str, Any]]:
    """M13 partial settle orchestration for ONE served call.

    capture = min(actual, maxAmount - captured), dust (<1 native) absorbed;
    X-Receipt ALWAYS issued with the call's own (unclamped) actualAmount;
    flush policy (PIN):
      captured >= maxAmount×0.9  OR  ttl below FORWARD_MARGIN_S → immediate
      synchronous settlePartial; otherwise fire-and-forget thread.
    Returns (X-Settle-Status value, receipt) — the caller stamps the response
    headers (streaming has no headers left to stamp; the store holds it)."""
    actual = compute_actual(usage, prices)
    st.ledger.plan_capture(payment_id, actual, max_amount)

    receipt = _build_receipt(st, payment_id, usage, actual, model)
    st.receipts.put(payment_id, receipt)

    if st.ledger.pending(payment_id) <= 0:
        return "partial-flush-none", receipt

    now = int(time.time())
    immediate = (
        st.ledger.captured(payment_id) * _FLUSH_DEN >= max_amount * _FLUSH_NUM
        or expires_at - now < st.config.forward_margin_s
    )
    if immediate:
        ok = await _flush_pending_sync(st, payment_id)
        status = "partial-flush-settled" if ok else "partial-flush-failed"
    else:
        threading.Thread(
            target=_flush_pending_bg, args=(st, payment_id), daemon=True
        ).start()
        status = "partial-flush-pending"
    return status, receipt


@app.get("/payment/{payment_id}/usage")
async def payment_usage(payment_id: int) -> dict[str, Any]:
    """Accrued-usage view (M13): {paymentId, captured, maxAmount, remaining}.

    `captured` is the relay's accrued total (may lead the on-chain captured
    counter while a background flush is in flight; resets to 0 on restart —
    see PartialSettleLedger). `maxAmount` comes from the ledger entry when
    this process has served the payment, otherwise from Escrow.getPayment.
    Unauthenticated by design (same posture as GET /receipt/{id})."""
    st = _get_state()
    captured, entry_max = st.ledger.snapshot(payment_id)
    max_amount = entry_max
    if max_amount is None:
        payment = await asyncio.to_thread(st.chain.get_payment, payment_id)
        max_amount = int(payment["maxAmount"])
    return {
        "paymentId": payment_id,
        "captured": captured,
        "maxAmount": max_amount,
        "remaining": max(0, max_amount - captured),
    }


# ------------------------------------------------------------ signature check


def _parse_payment_id(raw: str | None) -> int:
    """PIN: paymentId is a decimal string. isascii+isdigit rejects unicode
    digit lookalikes that int() would otherwise silently accept."""
    value = (raw or "").strip()
    if not value.isascii() or not value.isdigit():
        raise HTTPException(status_code=401, detail="missing/invalid X-Payment-Id")
    return int(value)


def _verify_request_signature(
    request: Request, raw_body: bytes, payment_id_str: str
) -> str:
    """PIN (verbatim): msg = f"{METHOD}|{path}|{sha256(raw_body_bytes).hexdigest()}
    |{paymentId}" with METHOD upper, path=/v1/chat/completions, 64-hex digest
    without 0x, decimal paymentId. EIP-191 via encode_defunct(text=msg).

    Pure CPU recover — no chain reads (401 is decided before any RPC).
    Returns the recovered signer; 401 on missing/invalid signature. The
    signer-is-buyer check happens after the single get_payment read."""
    signature = request.headers.get("X-Signature")
    if not signature:
        raise HTTPException(status_code=401, detail="missing X-Signature")

    msg = (
        f"{request.method.upper()}|{request.url.path}"
        f"|{hashlib.sha256(raw_body).hexdigest()}|{payment_id_str}"
    )
    try:
        return Account.recover_message(encode_defunct(text=msg), signature=signature)
    except Exception as exc:
        raise HTTPException(status_code=401, detail="invalid signature") from exc


# ------------------------------------------------------- upstream forwarding


def _extract_usage(usage: dict[str, Any] | None) -> Usage:
    """PIN: prompt_tokens / prompt_tokens_details.cached_tokens (default 0) /
    completion_tokens.

    Robustness for real upstreams (Kimi semantics, high confidence):
      - cached_tokens fallback chain:
        prompt_tokens_details.cached_tokens (standard position, primary)
        → top-level usage.cached_tokens (non-standard, Kimi-style last resort)
        → 0;
      - null values anywhere are treated as absent (`or 0` chains cover both
        explicit nulls and missing keys).
    """
    if not usage:
        raise ValueError("OpenAI response contained no usage object")
    prompt = int(usage.get("prompt_tokens") or 0)
    details = usage.get("prompt_tokens_details") or {}
    cached = int(
        details.get("cached_tokens")
        or usage.get("cached_tokens")
        or 0
    )
    completion = int(usage.get("completion_tokens") or 0)
    return Usage(prompt_tokens=prompt, cached_tokens=cached, completion_tokens=completion)


def _inject_stream_options(body: dict[str, Any]) -> dict[str, Any]:
    """Ensure stream_options={"include_usage": true} so the final SSE chunk
    carries usage. Returns a copy; never mutates the caller's dict."""
    body = dict(body)
    opts = dict(body.get("stream_options") or {})
    opts["include_usage"] = True
    body["stream_options"] = opts
    return body


async def _load_listing(st: RelayState) -> dict[str, Any]:
    """Seller's own Registry listing (chain read in a worker thread); 400 when
    unregistered or inactive."""
    listing = await asyncio.to_thread(st.chain.get_listing, st.chain.seller_address)
    if listing is None or not listing["active"]:
        raise HTTPException(status_code=400, detail="listing inactive or unregistered")
    return listing


def _check_model(listing: dict[str, Any], body: dict[str, Any]) -> None:
    """Model must be in listing.models; empty strings filtered before compare
    (M1/M2 deferred item folded into M3 per the deepwork plan)."""
    model = body.get("model")
    allowed = [m for m in (listing.get("models") or []) if isinstance(m, str) and m.strip()]
    if not isinstance(model, str) or model not in allowed:
        raise HTTPException(status_code=400, detail="model not in listing.models")


def _upstream_host(base_url: str) -> str:
    """Hostname of the configured upstream for the receipt `upstreamHost`
    field (e.g. "api.moonshot.cn"). Never empty — the config gate guarantees
    a parseable URL; fall back to the raw value for safety."""
    from urllib.parse import urlparse as _urlparse

    try:
        host = _urlparse(base_url).hostname or ""
    except ValueError:
        host = ""
    return host or base_url


def _check_model_provider_consistency(body: dict[str, Any]) -> None:
    """Anti-poisoning per-request gate: the buyer-requested model must belong
    to the OFFICIAL provider the relay's configured upstream serves.

    Two rejections:
      - `provider_for_model` None → the model name carries no official catalog
        prefix at all (a fake/invented model name — never forwarded);
      - the model's provider ≠ the upstream host's provider (e.g. a "kimi-…"
        name aimed at api.openai.com) — the only way such a request could be
        "served" is a lying upstream, which the allowlist already excludes;
        the check closes the mismatch at the request layer too.

    On an explicitly allowed CUSTOM upstream (ALLOW_CUSTOM_UPSTREAM=1, dev/test
    only) the host has no provider identity, so only the catalog-prefix half
    is enforced — the mismatch half needs a real official host to compare to.

    Complements the startup host allowlist and the listing.models check
    (errors are distinguishable; ordering with `_check_model` is free).
    """
    model = body.get("model")
    provider = provider_for_model(model)
    if provider is None:
        raise HTTPException(
            status_code=400,
            detail=(
                f"model {model!r} is not in the official model catalog "
                "(official-plan models only; refusing to forward)"
            ),
        )
    upstream_provider = host_provider(_get_state().config.openai_base_url)
    if upstream_provider is not None and upstream_provider != provider:
        raise HTTPException(
            status_code=400,
            detail=(
                f"model {model!r} (provider {provider}) does not match the official "
                f"upstream provider {upstream_provider}; refusing to forward"
            ),
        )


def _build_receipt(
    st: RelayState, payment_id: int, usage: Usage, actual: int, model: str
) -> dict[str, Any]:
    return build_receipt(
        chain_id=st.config.chain_id,
        payment_id=payment_id,
        prompt_tokens=usage.prompt_tokens,
        cached_tokens=usage.cached_tokens,
        completion_tokens=usage.completion_tokens,
        actual_amount=actual,
        seller=st.chain.seller_address,
        seller_key=st.config.seller_key,
        upstream_host=_upstream_host(st.config.openai_base_url),
        model=model,
    )


# ------------------------------------------------------------- settle helpers


async def _try_settle(
    st: RelayState, payment_id: int, usage: Usage, prices: Prices, max_amount: int
) -> tuple[bool, int]:
    """Price + settle. Returns (settled, actual). Never raises — a settle
    failure must not swallow the LLM response (PIN semantics)."""
    actual = compute_actual(usage, prices)
    settle_amount = clamp_settle_amount(actual, max_amount)
    try:
        await asyncio.to_thread(st.chain.settle, payment_id, settle_amount)
        return True, actual
    except Exception:
        logger.exception("settle failed for paymentId=%s", payment_id)
        return False, actual


async def _settle_and_stamp_headers(
    st: RelayState,
    payment_id: int,
    usage: Usage,
    prices: Prices,
    max_amount: int,
    response: JSONResponse,
    model: str,
) -> JSONResponse:
    settled, actual = await _try_settle(st, payment_id, usage, prices, max_amount)
    if not settled:
        response.headers["X-Settle-Status"] = "settle-failed"
        return response  # no receipt; buyer can refund after ttl

    response.headers["X-Settle-Status"] = "settled"
    receipt = _build_receipt(st, payment_id, usage, actual, model)
    st.receipts.put(payment_id, receipt)
    response.headers["X-Receipt"] = encode_x_receipt(receipt)
    return response


# ------------------------------------------------------------ streaming path


async def _forward_stream(
    st: RelayState,
    body: dict[str, Any],
    payment_id: int,
    prices: Prices,
    max_amount: int,
    model: str,
    *,
    partial_expires_at: int | None = None,
) -> StreamingResponse:
    """Transparent SSE passthrough. Usage is taken from the final chunk (with
    injected stream_options); settle happens after the stream drains — for a
    streaming response the outcome is observable via GET /receipt/{paymentId}.

    partial_expires_at=None → legacy one-shot settle (unchanged); an int
    selects the M13 bearer partial path (capture + threshold/TTL flush)."""
    request_body = _inject_stream_options(body)
    try:
        req = st.http.build_request("POST", CHAT_COMPLETIONS_PATH, json=request_body)
        resp = await st.http.send(req, stream=True)
    except httpx.HTTPError as exc:
        # Upstream connect error → 502, no settle.
        raise HTTPException(status_code=502, detail=f"upstream error: {exc}") from exc

    if resp.status_code != 200:
        raw = await resp.aread()
        await resp.aclose()
        raise HTTPException(status_code=502, detail=raw.decode("utf-8", "replace")[:2000])

    usage_holder: dict[str, Usage | None] = {"usage": None}

    async def sse_iterator() -> AsyncIterator[bytes]:
        buffer = b""
        try:
            async for chunk in resp.aiter_bytes():
                buffer += chunk
                while b"\n\n" in buffer:
                    event, buffer = buffer.split(b"\n\n", 1)
                    yield _relay_sse_event(event, usage_holder)
            if buffer.strip():  # flush a trailing event without final blank line
                yield _relay_sse_event(buffer, usage_holder)
        except (asyncio.CancelledError, GeneratorExit):
            # Client disconnected mid-stream → no settle (buyer can refund
            # after ttl). Re-raise so the server can clean up properly.
            logger.warning(
                "stream client disconnected mid-flight; not settling paymentId=%s",
                payment_id,
            )
            raise
        finally:
            await resp.aclose()

        # Stream fully drained → price from the last usage chunk and settle.
        # settle 仅在生成器正常完成路径可达（取消/断开在 except re-raise，不可达此处）
        usage = usage_holder["usage"]
        if usage is None:
            logger.warning(
                "stream drained without usage chunk; not settling paymentId=%s "
                "(buyer may refund after ttl)",
                payment_id,
            )
            return
        if partial_expires_at is None:
            settled, actual = await _try_settle(st, payment_id, usage, prices, max_amount)
            if settled:
                st.receipts.put(payment_id, _build_receipt(st, payment_id, usage, actual, model))
            else:
                logger.info(
                    "stream settle-failed paymentId=%s (buyer may refund after ttl)",
                    payment_id,
                )
        else:
            status, _receipt = await _partial_capture_and_maybe_flush(
                st, payment_id, usage, prices, max_amount, partial_expires_at, model
            )
            logger.info(
                "stream partial settle paymentId=%s status=%s", payment_id, status
            )

    return StreamingResponse(
        sse_iterator(),
        status_code=200,
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )


def _relay_sse_event(event: bytes, usage_holder: dict[str, Usage | None]) -> bytes:
    """Re-emit one raw SSE event verbatim; opportunistically capture usage.

    Usage may appear in an empty-choices chunk or a chunk carrying
    finish_reason, always BEFORE `data: [DONE]`; the LAST non-null usage in
    the stream is authoritative. A later chunk with a null/absent usage must
    not erase an earlier real one."""
    for line in event.decode("utf-8", "replace").splitlines():
        if not line.startswith("data:"):
            continue
        data = line[len("data:"):].strip()
        if data in ("", "[DONE]"):
            continue
        try:
            parsed = json.loads(data)
        except json.JSONDecodeError:
            continue
        if isinstance(parsed, dict) and parsed.get("usage"):
            try:
                usage_holder["usage"] = _extract_usage(parsed["usage"])
            except ValueError:
                pass
    return event + b"\n\n"


# --------------------------------------------------------------- main route


@app.post(CHAT_COMPLETIONS_PATH)
async def chat_completions(request: Request) -> Any:
    st = _get_state()
    raw_body = await request.body()
    # M13 dual auth, same priority: a Bearer tsk1.… header routes to the
    # stateless API-key path; anything else keeps the legacy path untouched.
    auth = (request.headers.get("Authorization") or "").strip()
    if auth.lower().startswith(BEARER_SCHEME) and auth[len(BEARER_SCHEME):].strip().startswith(
        BEARER_KEY_PREFIX
    ):
        return await _chat_completions_bearer(st, request, raw_body)
    return await _chat_completions_legacy(st, request, raw_body)


async def _chat_completions_legacy(st: RelayState, request: Request, raw_body: bytes) -> Any:
    """Legacy X-Payment-Id + X-Signature path — PIN semantics, byte-for-byte
    unchanged by M13."""
    # 1a. Payment id header (decimal string per PIN).
    payment_id = _parse_payment_id(request.headers.get("X-Payment-Id"))
    payment_id_str = str(payment_id)

    # 1b. EIP-191 signature recover — pure CPU, no chain read (401 first).
    recovered = _verify_request_signature(request, raw_body, payment_id_str)

    # Single get_payment read (one RPC roundtrip, in a worker thread).
    payment = await asyncio.to_thread(st.chain.get_payment, payment_id)

    # 1c. Recovered signer must be the payment's buyer (402, zero-cost reject).
    if recovered.lower() != str(payment["buyer"]).lower():
        raise HTTPException(status_code=402, detail="signer is not payment buyer")

    # 2. Listing must be active; model must be listed (400s) — chain read in
    #    a worker thread.
    listing = await _load_listing(st)
    try:
        body = json.loads(raw_body)
    except json.JSONDecodeError as exc:
        raise HTTPException(status_code=400, detail="invalid JSON body") from exc
    if not isinstance(body, dict):
        raise HTTPException(status_code=400, detail="body must be a JSON object")
    _check_model(listing, body)
    _check_model_provider_consistency(body)
    model_name = str(body["model"])  # validated as a listed string above

    # 2b. Registry v2 per-model price (PIN: the three unit prices follow the
    #     REQUESTED model, fetched from the chain via getPrice).
    try:
        prices = await asyncio.to_thread(
            st.chain.get_price, st.chain.seller_address, model_name
        )
    except NotActive as exc:
        raise HTTPException(
            status_code=400, detail="listing inactive or unregistered"
        ) from exc
    except ModelNotFound as exc:
        raise HTTPException(
            status_code=400, detail="model not in listing.models"
        ) from exc

    # 3. TTL margin guard: seller needs FORWARD_MARGIN_S to settle (409).
    if int(payment["expiresAt"]) - int(time.time()) < st.config.forward_margin_s:
        raise HTTPException(status_code=409, detail="payment ttl below forward margin")

    # 4. On-chain validity with the per-request minAmount estimate (402).
    min_amount = estimate_min_amount(
        prices, st.config.prompt_token_cap, st.config.completion_token_cap
    )
    valid = await asyncio.to_thread(
        st.chain.is_valid, payment_id, st.chain.seller_address, min_amount
    )
    if not valid:
        raise HTTPException(status_code=402, detail="payment invalid (unpaid/wrong seller/expired)")

    # 5-6. Forward, price, settle, receipt (never swallow the LLM response).
    # The buyer's body is forwarded to the upstream VERBATIM — zero parameter
    # injection here. Kimi compatibility: injecting sampling parameters
    # (temperature/top_p, any value including 0) would be rejected by the
    # upstream with HTTP 400, so the relay must never add them.
    if body.get("stream"):
        return await _forward_stream(
            st, body, payment_id, prices, int(payment["maxAmount"]), model_name
        )

    try:
        upstream = await st.http.post(
            CHAT_COMPLETIONS_PATH,
            json=body,
            timeout=UPSTREAM_TIMEOUT_NON_STREAM,
        )
    except httpx.HTTPError as exc:
        raise HTTPException(status_code=502, detail=f"upstream error: {exc}") from exc

    if upstream.status_code != 200:
        # OpenAI error → 502, no settle; buyer refunds after ttl.
        raise HTTPException(status_code=502, detail=upstream.text[:2000])

    try:
        payload = upstream.json()
        usage = _extract_usage(payload.get("usage"))
    except (ValueError, json.JSONDecodeError) as exc:
        raise HTTPException(status_code=502, detail=f"bad upstream payload: {exc}") from exc

    response = JSONResponse(status_code=200, content=payload)
    return await _settle_and_stamp_headers(
        st, payment_id, usage, prices, int(payment["maxAmount"]), response, model_name
    )


async def _chat_completions_bearer(st: RelayState, request: Request, raw_body: bytes) -> Any:
    """M13 bearer API-key path (stateless key, partial settle).

    Pipeline: decode the tsk1 key envelope (401 before any RPC) → one
    getPayment read → EIP-191 recover vs payload buyer AND payment buyer +
    hard expiry (all 401) → listing/model/provider/prices gates (400s, shared
    with legacy) → Escrow.isValid (402) — the legacy 409 FORWARD_MARGIN guard
    is intentionally skipped (PIN) → forward → capture into the ledger +
    threshold/TTL settlePartial flush → X-Receipt (per call) + X-Settle-Status.
    """
    grant = _decode_bearer_key(request.headers.get("Authorization"))
    payment = await asyncio.to_thread(st.chain.get_payment, grant.payment_id)
    _verify_bearer_grant(grant, payment)

    listing = await _load_listing(st)
    try:
        body = json.loads(raw_body)
    except json.JSONDecodeError as exc:
        raise HTTPException(status_code=400, detail="invalid JSON body") from exc
    if not isinstance(body, dict):
        raise HTTPException(status_code=400, detail="body must be a JSON object")
    _check_model(listing, body)
    _check_model_provider_consistency(body)
    model_name = str(body["model"])

    try:
        prices = await asyncio.to_thread(
            st.chain.get_price, st.chain.seller_address, model_name
        )
    except NotActive as exc:
        raise HTTPException(
            status_code=400, detail="listing inactive or unregistered"
        ) from exc
    except ModelNotFound as exc:
        raise HTTPException(
            status_code=400, detail="model not in listing.models"
        ) from exc

    # NO 409 margin guard here — PIN: the bearer path replaces it with the
    # hard expiry check in _verify_bearer_grant.

    min_amount = estimate_min_amount(
        prices, st.config.prompt_token_cap, st.config.completion_token_cap
    )
    valid = await asyncio.to_thread(
        st.chain.is_valid, grant.payment_id, st.chain.seller_address, min_amount
    )
    if not valid:
        raise HTTPException(
            status_code=402, detail="payment invalid (unpaid/wrong seller/expired)"
        )

    # Capture budget: the grant's m clamped by the on-chain maxAmount. An
    # inflated m (self-minted by the buyer) would otherwise turn every flush
    # into a doomed settlePartial revert — min() keeps the ledger bounded by
    # what the contract will actually accept.
    max_amount = min(int(payment["maxAmount"]), grant.max_amount)
    expires_at = int(payment["expiresAt"])

    if body.get("stream"):
        return await _forward_stream(
            st,
            body,
            grant.payment_id,
            prices,
            max_amount,
            model_name,
            partial_expires_at=expires_at,
        )

    try:
        upstream = await st.http.post(
            CHAT_COMPLETIONS_PATH,
            json=body,
            timeout=UPSTREAM_TIMEOUT_NON_STREAM,
        )
    except httpx.HTTPError as exc:
        raise HTTPException(status_code=502, detail=f"upstream error: {exc}") from exc

    if upstream.status_code != 200:
        raise HTTPException(status_code=502, detail=upstream.text[:2000])

    try:
        payload = upstream.json()
        usage = _extract_usage(payload.get("usage"))
    except (ValueError, json.JSONDecodeError) as exc:
        raise HTTPException(status_code=502, detail=f"bad upstream payload: {exc}") from exc

    response = JSONResponse(status_code=200, content=payload)
    status, receipt = await _partial_capture_and_maybe_flush(
        st, grant.payment_id, usage, prices, max_amount, expires_at, model_name
    )
    response.headers["X-Settle-Status"] = status
    response.headers["X-Receipt"] = encode_x_receipt(receipt)
    return response
