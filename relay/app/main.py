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

M13-R api-key revocation: the buyer kills the bearer key(s) minted for a
payment via POST /payment/{id}/revoke — an EIP-191-signed
`TokenShare API key revoke|paymentId={p}|expiry={e}` message, verified
against the CHAIN's getPayment (path-id + on-chain expiresAt binding,
recovered signer == payment.buyer) and recorded in an in-memory revoked
set. The bearer call path checks that set right after _verify_bearer_grant
→ 401 "revoked" (ahead of every 402 gate). The set is process-local BY
DESIGN: it is lost on restart; exposure stays bounded because every grant
hard-expires at payment.expiresAt (TTL backstop) and spend is capped by the
on-chain maxAmount (economic backstop) — no persistence layer.
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
from typing import Any, AsyncIterator, Final

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
    SHARED_SIGNER_KEY_PATH,
    TEE_KEY_PATH,
    Config,
    ConfigError,
    ENV_ALLOW_CUSTOM_UPSTREAM,
    ENV_CORS_ORIGINS,
    ENV_PUBLIC_ORIGIN,
    _normalize_openai_base_url,
    dstack_client,
    host_provider,
    load_config,
    parse_cors_origins,
    provider_for_model,
)
from .custody import (
    HEADER_SIGNATURE,
    MAX_BODY_BYTES,
    CustodyError,
    NonceStore,
    b64url_encode,
    build_revoke_message,
    build_submit_message,
    build_upload_aad,
    catalog_from_payload,
    check_request_origin,
    check_timestamp_window,
    decrypt_envelope,
    normalize_address,
    normalize_custody_upstream,
    normalize_seller_header,
    parse_envelope_plaintext,
    parse_revoke_body,
    parse_submit_body,
    probe_upstream_key,
    recover_wallet_signature,
    require_official_or_custom,
    sha256_hex,
    upload_public_bytes,
    upstream_host_of,
    upstream_is_official,
)
from .keystore import Keystore
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

# Test seam for upstream forwarding (httpx transport). None → real network.
# Kept module-level like custody._probe_transport; production code never
# touches it.
_upstream_transport: Any = None

# ---------------------------------------------------------------------------
# M15 bootstrap gate (RELAY_BOOTSTRAP_MODE=1, shared-only, two-phase boot).
#
# While the flag is active the middleware below is deny-by-default: ONLY
# GET/HEAD on /health, /info, /attestation (canonical path + single trailing
# slash) reach the app — every other method/path (business routes, docs,
# unknown paths, plain OPTIONS) gets an exact 503 {"detail":"bootstrap_mode"}
# WITHOUT touching routing or handlers. Non-HTTP scopes (e.g. a future
# websocket mount) are closed, never passed to business logic.
#
# The flag is FROZEN at startup (set in lifespan from the parsed config,
# cleared on shutdown): there is deliberately no runtime/HTTP switch.
#
# Registration order matters: Starlette's add_middleware makes the LAST-added
# middleware OUTERMOST. This gate is added BEFORE CORSMiddleware, so CORS
# wraps the gate — compliant preflights short-circuit in CORS without ever
# seeing the gate, while plain OPTIONS / business requests fall through CORS
# into the gate's deny.
# ---------------------------------------------------------------------------
BOOTSTRAP_ALLOWED_PATHS: Final[frozenset[str]] = frozenset(
    {"/health", "/info", "/attestation"}
)
BOOTSTRAP_ALLOWED_METHODS: Final[frozenset[str]] = frozenset({"GET", "HEAD"})
_BOOTSTRAP_BODY = b'{"detail":"bootstrap_mode"}'

# Frozen in lifespan from the parsed config; never mutated by any request.
_bootstrap_gate_active: bool = False


class BootstrapGateMiddleware:
    """Pure-ASGI deny-by-default gate (bootstrap phase 1). Reads NOTHING
    from the request body and keeps no per-request state."""

    def __init__(self, app: Any) -> None:
        self.app = app

    async def __call__(self, scope: Any, receive: Any, send: Any) -> None:
        if not _bootstrap_gate_active:
            await self.app(scope, receive, send)
            return
        if scope["type"] != "http":
            # A future websocket (or any non-HTTP scope) must not reach
            # business logic during bootstrap: close it instead of passing.
            if scope["type"] == "websocket":
                await send({"type": "websocket.close", "code": 1011})
            return
        method = (scope.get("method") or "").upper()
        path = scope.get("path") or "/"
        canonical = path[:-1] if len(path) > 1 and path.endswith("/") else path
        if method in BOOTSTRAP_ALLOWED_METHODS and canonical in BOOTSTRAP_ALLOWED_PATHS:
            await self.app(scope, receive, send)
            return
        await send(
            {
                "type": "http.response.start",
                "status": 503,
                "headers": [(b"content-type", b"application/json")],
            }
        )
        await send({"type": "http.response.body", "body": _BOOTSTRAP_BODY})


class RelayState:
    """Process-wide singletons assembled once at startup (fail-fast)."""

    def __init__(self, config: Config) -> None:
        self.config = config
        self.receipts = ReceiptStore()
        # M13-D in-memory partial-settle ledger {paymentId: entry}. Resets on
        # restart — see PartialSettleLedger for the conservative semantics.
        self.ledger = PartialSettleLedger()
        # M13-R in-memory bearer-key revocations {paymentId}. HONEST RESTART
        # SEMANTICS: this set is process-local and LOST on restart — there is
        # deliberately no persistence. The exposure after a restart stays
        # bounded by two natural backstops: (a) TTL — every grant hard-expires
        # at payment.expiresAt regardless of revocation state, so a forgotten
        # revocation dies with the grant; (b) economics — the SEC1-1 budget
        # admission gates cap total spend at the payment's on-chain maxAmount,
        # so a resurrected key can never spend beyond what the buyer escrowed.
        self.revoked_api_keys: set[int] = set()
        if config.is_shared:
            # M15 R1 shared custody mode: NO global upstream credential (the
            # R2 buyer flow sends the SELECTED seller's key per-request) and
            # NO base_url (the upstream is the seller's stored canonical
            # base, addressed absolutely). The chain client is constructed
            # lazily once (read-only binding snapshot + R2 settlement on the
            # shared signer account); a failed construction/read degrades
            # the binding fields to false/empty and buyer flows 503 — it
            # never blocks custody. The keystore load here is the
            # fail-closed gate: a corrupted file / totally-unwrappable
            # store raises KeystoreConfigError and kills startup.
            self.http = httpx.AsyncClient(
                timeout=httpx.Timeout(None, connect=10.0),
                transport=_upstream_transport,
            )
            self._chain = None
            self._chain_failed = False
            self.signer_address = Account.from_key(config.seller_key).address
            self.upload_pub = upload_public_bytes(config.shared_upload_priv)  # 65 B
            if config.bootstrap_mode:
                # BOOTSTRAP phase 1: identity only. No Keystore (no file
                # read/write/migration), no NonceStore, no persistence; the
                # chain client stays UNCONSTRUCTED (no RPC); the http client
                # exists but the gate denies every business route, so there
                # is no upstream probe / forward / settle either.
                self.nonces = None
                self.keystore = None
                self._chain_obj = None
                return
            self.nonces = NonceStore()
            self.keystore = Keystore(
                config.keystore_path,
                config.shared_kek,
                chain_id=config.chain_id,
                escrow_addr=config.escrow_addr,
                registry_addr=config.registry_addr,
                origin=config.public_origin,
            )
            self._chain_obj = None
            return
        self._chain_obj = ChainClient(
            rpc_url=config.rpc_url,
            escrow_addr=config.escrow_addr,
            registry_addr=config.registry_addr,
            seller_key=config.seller_key,
            chain_id=config.chain_id,
        )
        # httpx client for OpenAI forwarding. Client-level timeout stays open
        # (streaming responses are long-lived); non-stream requests apply a
        # bounded per-request timeout (UPSTREAM_TIMEOUT_NON_STREAM).
        self.http = httpx.AsyncClient(
            base_url=config.openai_base_url,
            headers={"Authorization": f"Bearer {config.openai_api_key}"},
            timeout=httpx.Timeout(None, connect=10.0),
        )

    @property
    def chain(self) -> ChainClient:
        """Buyer-facing chain access (legacy + shared buyer flows).

        Single mode: the eager startup client (fail-fast boot contract).
        Shared mode: the lazily constructed client — when it could not be
        built (RPC down / chain mismatch) this raises 503: fail closed,
        the buyer flow refuses before ANY upstream quota. The custody
        binding snapshot keeps its own graceful accessor
        (`chain_for_binding`) — status/degradation semantics are R1
        behavior and stay untouched."""
        if not self.config.is_shared:
            return self._chain_obj
        chain = self.chain_for_binding()
        if chain is None:
            raise HTTPException(
                status_code=503,
                detail="chain unavailable — buyer flows refuse (fail closed)",
            )
        return chain

    # -- shared-mode lazy chain (read-only binding snapshot) ----------------
    def chain_for_binding(self):
        """Construct the ChainClient on first use in shared mode; a failed
        construction is cached (binding snapshot degrades to false)."""
        if self._chain is None and not self._chain_failed:
            try:
                self._chain = ChainClient(
                    rpc_url=self.config.rpc_url,
                    escrow_addr=self.config.escrow_addr,
                    registry_addr=self.config.registry_addr,
                    seller_key=self.config.seller_key,
                    chain_id=self.config.chain_id,
                )
            except Exception as exc:
                logger.warning("shared mode: chain client unavailable (%s)", exc)
                self._chain_failed = True
        return self._chain


state: RelayState | None = None


@asynccontextmanager
async def lifespan(_: FastAPI) -> AsyncIterator[None]:
    global state, _bootstrap_gate_active
    config = load_config()  # ConfigError → startup fails, process exits non-zero
    _bootstrap_gate_active = config.bootstrap_mode  # FROZEN for this run
    state = RelayState(config)  # shared mode: keystore load (fail-closed gate)
    if config.is_shared:
        if config.bootstrap_mode:
            # Bootstrap log: keystore is deliberately NOT constructed here —
            # never call len() on it (it is None by design).
            logger.info(
                "relay up (shared BOOTSTRAP phase 1): chainId=%s signer=%s "
                "origin=%s (temporary) keystorePath=%s (must not exist) "
                "gate=GET/HEAD /health,/info,/attestation only",
                config.chain_id,
                state.signer_address,
                config.public_origin,
                config.keystore_path,
            )
        else:
            logger.info(
                "relay up (shared custody): chainId=%s signer=%s origin=%s "
                "keystoreEntries=%s keystorePath set=%s",
                config.chain_id,
                state.signer_address,
                config.public_origin,
                len(state.keystore),
                config.keystore_path is not None,
            )
    else:
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
    # Shared custody mode has NO configured upstream key — no probe.
    if (
        not config.is_shared
        and config.verify_upstream_on_start
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
        if state is not None and state.http is not None:
            await state.http.aclose()
        state = None  # prevent bare app calls from reusing stale config
        _bootstrap_gate_active = False  # the gate is startup-frozen, never sticky


def _effective_cors_origins() -> list[str]:
    """parse_cors_origins(RELAY_CORS_ORIGINS) + the relay's own canonical
    origin when RELAY_CORS_ORIGINS is explicitly pinned. "*" (dev/demo
    default) passes through untouched."""
    configured = parse_cors_origins(os.environ.get(ENV_CORS_ORIGINS))
    if "*" in configured:
        return configured
    own = (os.environ.get(ENV_PUBLIC_ORIGIN) or "").strip().rstrip("/")
    if own and own not in configured:
        configured.append(own)
    return configured


app = FastAPI(title="TokenShare Seller Relay", version="1", lifespan=lifespan)

# M15 bootstrap gate — added FIRST. Starlette's add_middleware makes the
# LAST-added middleware OUTERMOST, so the registration order below yields
# CORS(outer) → gate(inner): a compliant preflight OPTIONS short-circuits in
# CORSMiddleware before the gate sees it, while every other request falls
# through CORS into the gate (plain OPTIONS / business paths → 503
# {"detail":"bootstrap_mode"} during bootstrap).
app.add_middleware(BootstrapGateMiddleware)

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
    # ora20 narrow fix: keep the parse_cors_origins face; when RELAY_CORS_ORIGINS
    # is explicitly pinned (production), the relay's own canonical origin is
    # added so the CORS preflight set matches the custody handler's origin
    # gate exactly (no preflight-OK/actual-409 split). Unset/blank keeps the
    # existing dev/demo ["*"] verbatim — no new default-* anywhere.
    allow_origins=_effective_cors_origins(),
    allow_methods=["GET", "POST", "DELETE", "OPTIONS"],
    allow_headers=[
        "Authorization",
        "Content-Type",
        "X-Payment-Id",
        "X-Signature",
        # M15 R1 custody wallet-auth headers (browser console enrollment).
        "X-Tokenshare-Seller",
        "X-Tokenshare-Signature",
    ],
    expose_headers=["X-Receipt", "X-Settle-Status"],
)


def _get_state() -> RelayState:
    if state is None:
        raise HTTPException(status_code=503, detail="relay not initialized")
    return state


def _require_shared(st: RelayState) -> None:
    """Custody endpoints exist ONLY in shared mode — single mode answers the
    documented 503 not_shared_mode (protocol v1.0-e1)."""
    if not st.config.is_shared:
        raise HTTPException(status_code=503, detail="not_shared_mode")


def _require_single(st: RelayState, *, what: str) -> None:
    """Legacy payment/forwarding endpoints need the single-mode wiring
    (settle-capable chain client + configured upstream). Shared mode hosts
    custody instead and answers these with an honest 503 — single-mode
    behavior is untouched."""
    if st.config.is_shared:
        raise HTTPException(
            status_code=503,
            detail=f"{what} is not available in shared custody mode "
            "(no upstream API key / settle wiring; custody-only deployment)",
        )


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
    _require_single(st, what="/verify-upstream")
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
    st = _get_state()  # probe is served only by a fully-initialized relay
    _require_single(st, what="/preview-models")
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


# M15 bootstrap whitelist: HEAD is an explicitly supported identity method.
# This FastAPI/Starlette stack does NOT auto-derive HEAD from GET (observed
# 405), so the three whitelisted identity routes register GET+HEAD directly.
# Read-only handlers — single/shared bodies are unchanged by this.
@app.api_route("/health", methods=["GET", "HEAD"])
def health() -> dict[str, Any]:
    st = _get_state()
    out: dict[str, Any] = {
        "status": "ok",
        "chainId": st.config.chain_id,
        "seller": st.signer_address if st.config.is_shared else st.chain.seller_address,
        "mode": st.config.mode,
    }
    if st.config.is_shared:
        out["signer"] = st.signer_address
        if st.config.bootstrap_mode:
            # Bootstrap phase 1: no keystore object exists — report the phase
            # and OMIT keystoreEntries entirely (never len(None)).
            out["bootstrap"] = True
        else:
            out["bootstrap"] = False
            out["keystoreEntries"] = len(st.keystore)
    return out


# ------------------------------------------------------------------- TEE M7-A


@app.api_route("/attestation", methods=["GET", "HEAD"])
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

    if st.config.is_shared:
        # M15 R1 shared branch: report-data binds the SHARED SIGNER address
        # (20 B) + 12 zero bytes + keccak256(65-byte upload pubkey) = 64 B.
        derived = st.signer_address
        report_data = (
            bytes.fromhex(derived[2:]) + b"\x00" * 12 + keccak(st.upload_pub)
        )
        key_path = SHARED_SIGNER_KEY_PATH
    else:
        derived = st.chain.seller_address
        report_data = b"\x00" * 44 + bytes.fromhex(derived[2:])
        key_path = TEE_KEY_PATH
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
        # appId is reported ONLY when the dstack client actually exposes it;
        # never fabricated. quote ≠ DCAP proof (R2 adds receipts/verify).
        "appId": app_id,
        "keyPath": key_path,
        "derivedAddress": derived,
        "reportData": "0x" + report_data.hex(),
        "quote": "0x" + quote_hex if quote_hex else None,
        "quoteDigest": quote_digest,
    }


@app.api_route("/info", methods=["GET", "HEAD"])
def info() -> dict[str, Any]:
    """Capability / policy descriptor: TEE state, seller address, and a
    summary of the official-endpoint upstream policy (the anti-poisoning
    guarantee whose enforcement is attested by /attestation)."""
    st = _get_state()
    cfg = st.config
    if cfg.is_shared:
        # M15 R1: legacy fields retained (no global upstream exists → the
        # upstream block reports the per-seller custody posture) plus
        # mode + shared{origin, signer, uploadPubkey, uploadPubkeySha256,
        # keystoreEntries}.
        return {
            "name": "TokenShare Seller Relay",
            "version": "1",
            "chainId": cfg.chain_id,
            "seller": st.signer_address,
            "mode": "shared",
            "tee": {
                "enabled": cfg.tee_mode,
                "socketPath": cfg.tee_socket_path,
                "keyPath": SHARED_SIGNER_KEY_PATH if cfg.tee_mode else None,
            },
            "upstream": {
                "host": None,
                "official": False,
                "policy": "per-seller custody upstreams (shared mode; official "
                "allowlist enforced per enrollment)",
            },
            "officialUpstreamHosts": sorted(OFFICIAL_UPSTREAM_HOSTS),
            "modelProviderPrefixes": {
                provider: list(prefixes)
                for provider, prefixes in MODEL_PROVIDER_PREFIXES.items()
            },
            "shared": {
                "origin": cfg.public_origin,
                "signer": st.signer_address,
                "uploadPubkey": b64url_encode(st.upload_pub),
                "uploadPubkeySha256": sha256_hex(st.upload_pub),
                # Bootstrap phase 1 reports the flag and omits the entry
                # count (no keystore is constructed); normal shared mode
                # reports bootstrap:false explicitly so a completed phase-2
                # switch is externally verifiable.
                "bootstrap": cfg.bootstrap_mode,
                **(
                    {}
                    if cfg.bootstrap_mode
                    else {"keystoreEntries": len(st.keystore)}
                ),
            },
        }
    official = host_provider(cfg.openai_base_url) is not None
    return {
        "name": "TokenShare Seller Relay",
        "version": "1",
        "chainId": cfg.chain_id,
        "seller": st.chain.seller_address,
        "mode": "single",
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


# ============================================================ M15 R1 custody
#
# Shared-mode key custody (protocol v1.0-e1, errata merged). Endpoints:
#
#   GET    /sellers/nonce/{address}   issue/replace the seller's nonce
#   POST   /sellers/keys              enroll/replace an upstream api key
#   DELETE /sellers/keys              revoke the stored key (idempotent)
#   GET    /sellers/{address}/status  PUBLIC unauthenticated status view
#
# Pipeline (POST): shape → static upstream gate → wallet EIP-191 auth
# (BEFORE decrypt/upstream) → timestamp window → atomic nonce consume →
# envelope decrypt → upstream probe → keystore store (persist-then-commit).
# Any failure preserves the previously stored valid key. In SINGLE mode all
# four endpoints answer 503 not_shared_mode; chat/payments keep the legacy
# wiring untouched.


def _custody_http_error(exc: CustodyError) -> HTTPException:
    # detail is the protocol CODE — never key material.
    return HTTPException(status_code=exc.status, detail=exc.code)


def _chain_binding_snapshot(st: RelayState, seller: str) -> dict[str, Any]:
    """Read-only chain binding for status/submit responses. Registry
    getListing → listing_bound/listing_active/listing_models; Escrow
    settleDelegateOf (minimal ABI read) → delegate_authorized. EVERY read
    failure degrades to false/empty — we never claim true without a
    successful read (and a chain without the v3.1 delegate function
    reports authorized=false cleanly)."""
    out: dict[str, Any] = {
        "listing_bound": False,
        "listing_active": False,
        "listing_models": [],
        "delegate_authorized": False,
    }
    chain = st.chain_for_binding()
    if chain is None:
        return out
    try:
        listing = chain.get_listing(seller)
    except Exception as exc:
        logger.warning("binding snapshot: listing read failed (%s)", exc)
        listing = None
    if listing is not None:
        out["listing_bound"] = True
        out["listing_active"] = bool(listing.get("active"))
        out["listing_models"] = [
            m for m in (listing.get("models") or []) if isinstance(m, str) and m.strip()
        ]
    try:
        # Minimal ABI delegate read (Escrow v3.1 settleDelegateOf); a chain
        # without the function surfaces as a call error → false, cleanly.
        delegate = chain.escrow.functions.settleDelegateOf(seller).call()
        if delegate is not None and str(delegate).lower() != "0x0000000000000000000000000000000000000000":
            out["delegate_authorized"] = str(delegate).lower() == st.signer_address.lower()
    except Exception as exc:
        logger.warning("binding snapshot: delegate read failed (%s)", exc)
    return out


@app.get("/sellers/nonce/{address}")
def custody_nonce(address: str) -> dict[str, Any]:
    st = _get_state()
    _require_shared(st)
    try:
        seller = normalize_address(address)
    except CustodyError as exc:
        raise _custody_http_error(exc)
    record = st.nonces.issue(seller)
    cfg = st.config
    return {
        "nonce": record.nonce,
        "issued_at": record.issued_at,
        "expires_at": record.expires_at,
        "origin": cfg.public_origin,
        "chain_id": cfg.chain_id,
        "escrow_addr": cfg.escrow_addr.lower(),
        "registry_addr": cfg.registry_addr.lower(),
        "mode": "shared",
    }


@app.post("/sellers/keys")
async def custody_submit(request: Request) -> JSONResponse:
    st = _get_state()
    _require_shared(st)
    cfg = st.config
    try:
        check_request_origin(request, cfg.public_origin)
        raw = await request.body()
        if len(raw) > MAX_BODY_BYTES:
            raise CustodyError(400, "bad_request", "body exceeds 64KiB")
        body = parse_submit_body(raw)
        # Static upstream gate: normalization + official/custom allowlist.
        normalized = normalize_custody_upstream(body["upstream_base_url"])
        require_official_or_custom(normalized)
        # Authenticate BEFORE decrypt/upstream. The message binds the
        # SERVER-normalized upstream, the CONFIG domain (server authority)
        # and the client's nonce/window — the window itself is verified
        # against the SERVER record below, never a client echo.
        seller = normalize_seller_header(request)
        message = build_submit_message(
            seller=seller,
            chain_id=cfg.chain_id,
            escrow_addr=cfg.escrow_addr,
            registry_addr=cfg.registry_addr,
            origin=cfg.public_origin,
            upstream_base_url=normalized,
            nonce=body["nonce"],
            body_sha256=sha256_hex(raw),
            issued=body["issued_at"],
            expires=body["expires_at"],
        )
    except CustodyError as exc:
        raise _custody_http_error(exc)
    try:
        recovered = recover_wallet_signature(
            message, request.headers.get(HEADER_SIGNATURE)
        )
    except CustodyError as exc:
        raise _custody_http_error(exc)
    if recovered != seller:
        raise HTTPException(status_code=401, detail="unauthorized")
    try:
        check_timestamp_window(body["issued_at"], body["expires_at"])
        record = st.nonces.consume(seller, body["nonce"], body["issued_at"], body["expires_at"])
    except CustodyError as exc:
        raise _custody_http_error(exc)
    try:
        aad = build_upload_aad(
            seller=seller,
            chain_id=cfg.chain_id,
            escrow_addr=cfg.escrow_addr,
            registry_addr=cfg.registry_addr,
            origin=cfg.public_origin,
            upstream_base_url=normalized,
            nonce=record.nonce,
            issued=record.issued_at,
            expires=record.expires_at,
        )
        api_key = parse_envelope_plaintext(
            decrypt_envelope(cfg.shared_upload_priv, body["envelope"], aad)
        )
    except CustodyError as exc:
        raise _custody_http_error(exc)

    probe = await probe_upstream_key(normalized, api_key)
    if not probe.ok:
        if probe.status in (401, 403):
            raise HTTPException(status_code=400, detail="upstream_key_rejected")
        raise HTTPException(status_code=502, detail="upstream_unreachable")
    catalog = catalog_from_payload(probe.models, normalized)
    fingerprint = sha256_hex(api_key.encode("ascii"))

    try:
        st.keystore.set(
            seller,
            upstream_base_url=normalized,
            api_key=api_key,
            catalog=catalog,
        )
    except Exception as exc:
        # Persist failed → memory rolled back → the OLD valid key survives.
        logger.exception("keystore persist failed (old key preserved)")
        raise HTTPException(status_code=503, detail="keystore_unavailable") from exc

    binding = _chain_binding_snapshot(st, seller)
    return JSONResponse(
        status_code=200,
        content={
            "seller": seller,
            "stored": True,
            "upstream_host": upstream_host_of(normalized),
            "official": upstream_is_official(normalized),
            "key_fingerprint": fingerprint,
            "catalog": catalog,
            "listing_bound": binding["listing_bound"],
            "delegate_authorized": binding["delegate_authorized"],
        },
    )


@app.delete("/sellers/keys")
async def custody_revoke(request: Request) -> JSONResponse:
    st = _get_state()
    _require_shared(st)
    cfg = st.config
    try:
        check_request_origin(request, cfg.public_origin)
        raw = await request.body()
        if len(raw) > MAX_BODY_BYTES:
            raise CustodyError(400, "bad_request", "body exceeds 64KiB")
        body = parse_revoke_body(raw)
        seller = normalize_seller_header(request)
        # DELETE messages carry NO upstream/body-hash segments — a POST
        # signature can never be replayed here (action cross-replay 401).
        message = build_revoke_message(
            seller=seller,
            chain_id=cfg.chain_id,
            escrow_addr=cfg.escrow_addr,
            registry_addr=cfg.registry_addr,
            origin=cfg.public_origin,
            nonce=body["nonce"],
            issued=body["issued_at"],
            expires=body["expires_at"],
        )
    except CustodyError as exc:
        raise _custody_http_error(exc)
    try:
        recovered = recover_wallet_signature(
            message, request.headers.get(HEADER_SIGNATURE)
        )
    except CustodyError as exc:
        raise _custody_http_error(exc)
    if recovered != seller:
        raise HTTPException(status_code=401, detail="unauthorized")
    try:
        check_timestamp_window(body["issued_at"], body["expires_at"])
        st.nonces.consume(seller, body["nonce"], body["issued_at"], body["expires_at"])
    except CustodyError as exc:
        raise _custody_http_error(exc)

    try:
        st.keystore.remove(seller)  # idempotent; absent → no write
    except Exception as exc:
        logger.exception("keystore persist failed (old key preserved)")
        raise HTTPException(status_code=503, detail="keystore_unavailable") from exc
    return JSONResponse(status_code=200, content={"seller": seller, "stored": False})


@app.get("/sellers/{address}/status")
def custody_status(address: str) -> dict[str, Any]:
    st = _get_state()
    _require_shared(st)
    try:
        seller = normalize_address(address)
    except CustodyError as exc:
        raise _custody_http_error(exc)
    entry = st.keystore.get(seller)
    if entry is None:
        catalog: list[dict[str, Any]] = []
        fingerprint: str | None = None
        upstream_host: str | None = None
        upstream_official = False
        updated_at: int | None = None
    else:
        catalog = entry.catalog
        fingerprint = entry.key_fingerprint
        upstream_host = upstream_host_of(entry.upstream_base_url)
        upstream_official = upstream_is_official(entry.upstream_base_url)
        updated_at = entry.updated
    binding = _chain_binding_snapshot(st, seller)
    return {
        "mode": "shared",
        "has_key": entry is not None,
        "key_fingerprint": fingerprint,
        "upstream_host": upstream_host,
        "upstream_official": upstream_official,
        "catalog": catalog,
        "listing_bound": binding["listing_bound"],
        "listing_active": binding["listing_active"],
        "delegate_authorized": binding["delegate_authorized"],
        "listing_models": binding["listing_models"],
        "updated_at": updated_at,
    }


# ------------------------------------------------------- M13 bearer + partial

# PIN key format: `tsk1.<b64url(payload_json)>.<b64url(sig_hex)>`,
# payload={"p":paymentId,"e":expiry,"m":maxAmount,"b":buyer}.
BEARER_SCHEME = "bearer "
BEARER_KEY_PREFIX = "tsk1."
# PIN mint message (EIP-191 text, C-lane console signs exactly this):
# `TokenShare API key grant|paymentId={p}|expiry={e}|maxAmount={m}`.
BEARER_GRANT_MESSAGE = "TokenShare API key grant"
# M13-R revoke message (EIP-191 text, buyer signs exactly this to kill the
# key(s) of a payment): `TokenShare API key revoke|paymentId={p}|expiry={e}`.
# Deliberately a DIFFERENT literal prefix from the mint message — a revoke
# signature can never be replayed as an api-key mint signature (or minted
# keys' signatures as revocations): the recovered signer differs.
BEARER_REVOKE_MESSAGE = "TokenShare API key revoke"

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

    def __init__(self, max_amount: int | None) -> None:
        self.captured = 0
        self.pending = 0
        # None until the first plan_capture re-bases it (chain-seeded entries
        # are created before any budget is known — the usage endpoint then
        # falls back to getPayment).
        self.max_amount = max_amount


class PartialSettleLedger:
    """In-memory capture ledger {paymentId: entry} for the bearer partial
    path (M13-B/D, SEC1 revisions).

    RESTART SEMANTICS (SEC1-3, rev-6 L2): the ledger is memory-only, and the
    first request that touches a paymentId after a restart seeds `captured`
    from the chain via Escrow v2 capturedOf (seed_and_get). A FAILED chain
    read creates NO entry and returns None — fabricating a 0 entry would
    under-count captured forever, over-admit through the budget gate and
    send doomed flush txs (OverMax reverts) until TTL; instead the request
    takes the no-accumulator / short-circuit path and the NEXT request
    retries the seeding. Gates and the usage view therefore work against the
    REAL accrued total, and a legacy fold-settle can never under-shoot the
    on-chain captured floor.

    BUDGET REFRESH (SEC1-1): entry.max_amount is NOT a first-request cache —
    plan_capture re-bases it from the CURRENT request's effective budget
    (min(on-chain maxAmount, grant.m)) on every call."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._entries: dict[int, _LedgerEntry] = {}

    def seed_and_get(self, payment_id: int, read_captured: Any) -> int | None:
        """Return the current captured total, creating the entry seeded from
        the chain (read_captured() → Escrow v2 capturedOf) on first sight.
        The chain read happens OUTSIDE the lock; a concurrent creator wins
        and its seeding is kept.

        rev-6 L2: a FAILED read_captured() returns None and leaves NO entry
        (the caller either short-circuits or takes the no-accumulator path);
        the next request retries the seeding. Sync — run via
        asyncio.to_thread."""
        with self._lock:
            entry = self._entries.get(payment_id)
            if entry is not None:
                return entry.captured
        try:
            seeded = int(read_captured())
        except Exception as exc:
            # rev-6 L2: never fabricate a 0 entry — it would permanently
            # under-count captured (budget gate over-admits, flushes revert
            # OverMax until TTL). No entry; next request re-seeds.
            logger.warning(
                "capturedOf seed failed paymentId=%s — no ledger entry "
                "created (%s); next request retries the seeding",
                payment_id,
                exc,
            )
            return None
        with self._lock:
            entry = self._entries.get(payment_id)
            if entry is None:
                entry = _LedgerEntry(None)  # budget re-based by plan_capture
                entry.captured = seeded
                self._entries[payment_id] = entry
            return entry.captured

    def has_entry(self, payment_id: int) -> bool:
        """True when a ledger entry exists for the paymentId (observability /
        tests: rev-6 L2/L4 pin that failed seeds and usage reads never
        create one)."""
        with self._lock:
            return payment_id in self._entries

    def plan_capture(self, payment_id: int, actual: int, max_amount: int) -> int:
        """PIN clamp: capture = min(actual, maxAmount - captured); below 1
        native unit nothing is recorded (dust absorbed). `max_amount` is the
        CURRENT request's effective budget and is re-based onto the entry
        every call (SEC1-1: never a stale first-request cache). Returns the
        captured amount (0 = nothing recorded)."""
        with self._lock:
            entry = self._entries.get(payment_id)
            if entry is None:
                entry = _LedgerEntry(max_amount)
                self._entries[payment_id] = entry
            else:
                entry.max_amount = max_amount
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

    def clear(self, payment_id: int) -> None:
        """Drop the entry — after a terminal settle (SEC1-2: settle() ends the
        payment, no further captures/flushes may accrue against it)."""
        with self._lock:
            self._entries.pop(payment_id, None)

    def snapshot(self, payment_id: int) -> tuple[int, int | None]:
        """(captured, max_amount | None) — max_amount None when this process
        never saw the payment (usage endpoint falls back to getPayment)."""
        with self._lock:
            entry = self._entries.get(payment_id)
            if entry is None:
                return 0, None
            return entry.captured, entry.max_amount


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
    anything else rejected → 401, aligned with the X-Payment-Id face).
    SEC1 顺手项: values beyond uint256 are rejected HERE as 401 — letting
    them through would surface later as an ABI to_bytes OverflowError (500)."""
    value = payload.get(field)
    if isinstance(value, bool):
        raise HTTPException(status_code=401, detail=f"api key payload {field!r} invalid")
    if isinstance(value, int) and 0 <= value < (1 << 256):
        return value
    if isinstance(value, str) and value.isascii() and value.isdigit():
        parsed = int(value)
        if parsed < (1 << 256):
            return parsed
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


# rev-6 L2: the former _read_chain_captured (capturedOf with a conservative
# 0 fallback) is GONE — callers now pass `st.chain.captured_of` straight into
# seed_and_get, which leaves no entry on failure (see PartialSettleLedger).


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
    *,
    receipt_seller: str | None = None,
    upstream_host: str | None = None,
) -> tuple[str, dict[str, Any]]:
    """M13 partial settle orchestration for ONE served call.

    capture = min(actual, maxAmount - captured), dust (<1 native) absorbed;
    X-Receipt ALWAYS issued with the call's own (unclamped) actualAmount;
    flush policy (PIN):
      captured >= maxAmount×0.9  OR  ttl below FORWARD_MARGIN_S → immediate
      synchronous settlePartial; otherwise fire-and-forget thread.
    R2: receipt_seller/upstream_host select the receipt identity (shared
    buyer flow: the ACTUAL seller + the seller's validated host); None
    keeps the single-mode face.
    Returns (X-Settle-Status value, receipt) — the caller stamps the response
    headers (streaming has no headers left to stamp; the store holds it)."""
    actual = compute_actual(usage, prices)
    st.ledger.plan_capture(payment_id, actual, max_amount)

    receipt = _build_receipt(
        st, payment_id, usage, actual, model,
        receipt_seller=receipt_seller, upstream_host=upstream_host,
    )
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
    """Accrued-usage view (M13): {paymentId, captured, maxAmount, remaining,
    revoked}.

    rev-6 L4 — READ/WRITE SEPARATION: this endpoint NEVER creates a ledger
    entry (the previous seed_and_get let an unauthenticated GET mint durable
    entries, one RPC read each, for arbitrary paymentIds). Instead:
      - entry exists (this process served the payment): report the ledger's
        captured with NO chain read (it may lead the on-chain counter while
        a background flush is in flight);
      - no entry: ONE capturedOf read, nothing written;
      - that read fails: `captured`/`remaining` degrade to null plus
        `capturedUnavailable: true` (a fake 0 would under-report spend);
        `maxAmount` still falls back to Escrow.getPayment, `revoked` still
        reports the in-memory revocation set (false after a restart even
        for a revoked key — see RelayState).
    Unauthenticated by design (same posture as GET /receipt/{id})."""
    st = _get_state()
    captured: int | None
    entry_max: int | None
    if st.ledger.has_entry(payment_id):
        captured = st.ledger.captured(payment_id)
        _, entry_max = st.ledger.snapshot(payment_id)
    else:
        entry_max = None
        try:
            captured = await asyncio.to_thread(st.chain.captured_of, payment_id)
        except Exception as exc:
            logger.warning(
                "capturedOf read failed paymentId=%s — usage view degrades "
                "captured to unavailable (%s)",
                payment_id,
                exc,
            )
            captured = None
    max_amount = entry_max
    if max_amount is None:
        payment = await asyncio.to_thread(st.chain.get_payment, payment_id)
        max_amount = int(payment["maxAmount"])
    if captured is None:
        return {
            "paymentId": payment_id,
            "captured": None,
            "maxAmount": max_amount,
            "remaining": None,
            "capturedUnavailable": True,
            "revoked": payment_id in st.revoked_api_keys,
        }
    return {
        "paymentId": payment_id,
        "captured": captured,
        "maxAmount": max_amount,
        "remaining": max(0, max_amount - captured),
        "revoked": payment_id in st.revoked_api_keys,
    }


@app.post("/payment/{payment_id}/revoke")
async def payment_revoke(payment_id: int, request: Request) -> dict[str, Any]:
    """M13-R: revoke the bearer API key(s) minted for a payment.

    body: {"message": "TokenShare API key revoke|paymentId={p}|expiry={e}",
           "signature": "0x…"}  (EIP-191 text via encode_defunct).

    The message is parsed into THREE pipe segments and each is bound to
    authoritative state BEFORE the signer is trusted:
      1. prefix == "TokenShare API key revoke" verbatim (a grant/mint message
         or any other text → 401 — signature classes never mix);
      2. paymentId segment == the PATH id (a revoke signed for another
         payment cannot be replayed against this one → 401);
      3. expiry segment == the on-chain payment.expiresAt — the SAME value
         the mint grant carried — so a stale revoke for a previous grant
         round of the same paymentId is rejected (→ 401).
    The buyer comes from the CHAIN's getPayment, never from body parameters;
    recovered signer != payment.buyer → 401. On success the paymentId joins
    the in-memory revoked_api_keys set and the bearer call path refuses with
    401 "revoked" ahead of every 402 budget gate.

    IDEMPOTENT: revoking an already-revoked payment returns 200 again (set
    add). RESTART HONESTY: the set is memory-only and lost on restart — see
    RelayState.revoked_api_keys for the TTL + maxAmount backstops that keep
    post-restart exposure bounded. No persistence, by design."""
    st = _get_state()
    try:
        body = await request.json()
    except ValueError as exc:
        raise HTTPException(status_code=400, detail="invalid JSON body") from exc
    if not isinstance(body, dict):
        raise HTTPException(status_code=400, detail="body must be a JSON object")
    message = body.get("message")
    signature = body.get("signature")
    if not isinstance(message, str) or not message.strip():
        raise HTTPException(status_code=400, detail="missing message")
    if not isinstance(signature, str) or not signature.strip():
        raise HTTPException(status_code=400, detail="missing signature")

    # Segment parse — verbatim prefix + decimal paymentId/expiry. Any
    # structural deviation is a 401 (signature-verification failure class),
    # decided with pure CPU BEFORE any chain read.
    parts = message.split("|")
    if (
        len(parts) != 3
        or parts[0] != BEARER_REVOKE_MESSAGE
        or not parts[1].startswith("paymentId=")
        or not parts[2].startswith("expiry=")
    ):
        raise HTTPException(status_code=401, detail="revoke message malformed")
    pid_raw = parts[1][len("paymentId="):]
    exp_raw = parts[2][len("expiry="):]
    if not (pid_raw.isascii() and pid_raw.isdigit()) or not (
        exp_raw.isascii() and exp_raw.isdigit()
    ):
        raise HTTPException(status_code=401, detail="revoke message malformed")
    # Misplaced-signature replay guard: the signed paymentId must be the one
    # in the path (decimal per PIN).
    if int(pid_raw) != payment_id:
        raise HTTPException(
            status_code=401, detail="revoke message paymentId does not match path"
        )

    # On-chain binding: expiry must equal the CURRENT grant's expiresAt (the
    # same value mint carried), and the signer must be the payment's on-chain
    # buyer — getPayment is authoritative, body parameters are never trusted.
    payment = await asyncio.to_thread(st.chain.get_payment, payment_id)
    if int(exp_raw) != int(payment["expiresAt"]):
        raise HTTPException(
            status_code=401, detail="revoke message expiry does not match payment"
        )
    try:
        recovered = Account.recover_message(
            encode_defunct(text=message), signature=signature
        )
    except Exception as exc:
        raise HTTPException(status_code=401, detail="revoke signature invalid") from exc
    if str(recovered).lower() != str(payment["buyer"]).lower():
        raise HTTPException(
            status_code=401, detail="revoke signer is not the payment buyer"
        )

    already = payment_id in st.revoked_api_keys
    st.revoked_api_keys.add(payment_id)  # set add → naturally idempotent
    logger.info(
        "bearer api key revoked paymentId=%s (alreadyRevoked=%s)", payment_id, already
    )
    return {"paymentId": payment_id, "revoked": True, "alreadyRevoked": already}


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

def _check_model_provider_consistency(
    body: dict[str, Any], upstream_base_url: str | None = None
) -> None:
    """Anti-poisoning per-request gate: the buyer-requested model must belong
    to the OFFICIAL provider the upstream serves.

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

    R2: `upstream_base_url` selects the upstream — None keeps the single-mode
    global configured upstream; the shared buyer flow passes the SELLER'S
    validated custody endpoint host (never the global OPENAI config).

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
    upstream_base = (
        upstream_base_url
        if upstream_base_url is not None
        else _get_state().config.openai_base_url
    )
    upstream_provider = host_provider(upstream_base)
    if upstream_provider is not None and upstream_provider != provider:
        raise HTTPException(
            status_code=400,
            detail=(
                f"model {model!r} (provider {provider}) does not match the official "
                f"upstream provider {upstream_provider}; refusing to forward"
            ),
        )


def _build_receipt(
    st: RelayState,
    payment_id: int,
    usage: Usage,
    actual: int,
    model: str,
    *,
    receipt_seller: str | None = None,
    upstream_host: str | None = None,
) -> dict[str, Any]:
    """R2: receipt_seller/upstream_host select the receipt identity —
    None keeps the single-mode face (relay's own seller/configured host).
    Shared buyer flow: seller = the ACTUAL economic seller (payment.seller /
    listing operator); the signature key is ALWAYS the relay's own account
    key (shared signer in shared mode — single identity unchanged)."""
    return build_receipt(
        chain_id=st.config.chain_id,
        payment_id=payment_id,
        prompt_tokens=usage.prompt_tokens,
        cached_tokens=usage.cached_tokens,
        completion_tokens=usage.completion_tokens,
        actual_amount=actual,
        seller=receipt_seller or st.chain.seller_address,
        seller_key=st.config.seller_key,
        upstream_host=(
            upstream_host
            if upstream_host is not None
            else _upstream_host(st.config.openai_base_url)
        ),
        model=model,
    )


# ------------------------------------------------------------- settle helpers


async def _try_settle(
    st: RelayState, payment_id: int, usage: Usage, prices: Prices, max_amount: int
) -> tuple[bool, int]:
    """Price + settle. Returns (settled, actual). Never raises — a settle
    failure must not swallow the LLM response (PIN semantics).

    SEC1-2 (CHOSEN FIX ① — unified settlement semantics): Escrow v2 settle
    requires actual ≥ the on-chain captured total, so after bearer partial
    captures a plain clamp(actual) would revert BelowCaptured FOREVER (→ free
    service within ttl). The ledger's accrued total — including un-flushed
    pending, atomically CLAIMED here so an already-scheduled background flush
    no-ops instead of double-sending — is folded into ONE final settle:
        total = min(captured + clamp(actual), maxAmount)
    On success the entry is cleared (Settled is terminal). On failure the
    claimed pending is NOT restored to `pending`: it stays folded into
    `captured` and the next settle retry carries it — a later bearer flush
    can therefore never double-count it (the buyer cannot be overcharged).
    Acceptable narrow edge (documented): a background flush already IN FLIGHT
    (claimed+mid-send) cannot be recalled; both txs serialize on _SETTLE_LOCK
    and the straggler settlePartial reverts on the settled/over-captured
    state — mixed-mode concurrency inside one flush window only."""
    actual = compute_actual(usage, prices)
    settle_amount = clamp_settle_amount(actual, max_amount)
    # Seed captured from the chain when this process has no entry (SEC1-3:
    # e.g. restart after bearer captures) — no-op once the entry exists.
    # rev-6 L2: on a FAILED seed there is NO entry and the fold proceeds
    # without it (response already served — short-circuiting here would
    # swallow the LLM response, which PIN forbids). With no entry,
    # ledger.captured() reads 0 → total is the plain clamp; if the chain
    # really has captured > 0 the settle reverts BelowCaptured, which since
    # rev-6 L1 dies at estimate time (ContractLogicError) — gas-free.
    seeded = await asyncio.to_thread(
        st.ledger.seed_and_get,
        payment_id,
        lambda: st.chain.captured_of(payment_id),
    )
    if seeded is None:
        logger.info(
            "paymentId=%s settling without the ledger fold (capturedOf seed "
            "failed; no entry created — next request re-seeds)",
            payment_id,
        )
    st.ledger.take_pending(payment_id)  # claim un-flushed: bg flush no-ops
    total = min(st.ledger.captured(payment_id) + settle_amount, max_amount)
    try:
        await asyncio.to_thread(st.chain.settle, payment_id, total)
    except Exception:
        logger.exception("settle failed for paymentId=%s", payment_id)
        return False, actual
    st.ledger.clear(payment_id)
    return True, actual


async def _settle_and_stamp_headers(
    st: RelayState,
    payment_id: int,
    usage: Usage,
    prices: Prices,
    max_amount: int,
    response: JSONResponse,
    model: str,
    *,
    receipt_seller: str | None = None,
    upstream_host: str | None = None,
) -> JSONResponse:
    settled, actual = await _try_settle(st, payment_id, usage, prices, max_amount)
    if not settled:
        response.headers["X-Settle-Status"] = "settle-failed"
        return response  # no receipt; buyer can refund after ttl

    response.headers["X-Settle-Status"] = "settled"
    receipt = _build_receipt(
        st, payment_id, usage, actual, model,
        receipt_seller=receipt_seller, upstream_host=upstream_host,
    )
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
    upstream_url: str | None = None,
    extra_headers: dict[str, str] | None = None,
    receipt_seller: str | None = None,
    upstream_host: str | None = None,
) -> StreamingResponse:
    """Transparent SSE passthrough. Usage is taken from the final chunk (with
    injected stream_options); settle happens after the stream drains — for a
    streaming response the outcome is observable via GET /receipt/{paymentId}.

    partial_expires_at=None → legacy one-shot settle (unchanged); an int
    selects the M13 bearer partial path (capture + threshold/TTL flush).

    R2: upstream_url/extra_headers address the upstream — None keeps the
    single-mode face (client base_url + client-level Authorization). The
    shared buyer flow passes the seller's canonical absolute URL and the
    PER-REQUEST Authorization (the client-level headers are never mutated —
    concurrent A/B calls cannot leak credentials into each other's streams).
    receipt_seller/upstream_host select the drained-stream receipt identity."""
    request_body = _inject_stream_options(body)
    try:
        req = st.http.build_request(
            "POST",
            upstream_url or CHAT_COMPLETIONS_PATH,
            json=request_body,
            headers=extra_headers,
        )
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
                st.receipts.put(
                    payment_id,
                    _build_receipt(
                        st, payment_id, usage, actual, model,
                        receipt_seller=receipt_seller, upstream_host=upstream_host,
                    ),
                )
            else:
                logger.info(
                    "stream settle-failed paymentId=%s (buyer may refund after ttl)",
                    payment_id,
                )
        else:
            status, _receipt = await _partial_capture_and_maybe_flush(
                st, payment_id, usage, prices, max_amount, partial_expires_at, model,
                receipt_seller=receipt_seller, upstream_host=upstream_host,
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


# ----------------------------------------------------------- M15 R2 shared buyer flow
#
# Frozen R2 semantics (LOCAL implementation; Escrow v3.2 NOT deployed — the
# current-chain v3.1 lacks settleDelegateOf, so the delegate preflight read
# reverts and the shared buyer flow fails closed 424: this is HONEST
# behavior, never claimed live-working).
#
# RELAY_MODE=single defaults unchanged. In shared mode the buyer flow keeps
# the EXACT auth semantics of single (Bearer tsk1 or legacy X-Payment-Id +
# X-Signature, same faces/ordering), but the ECONOMIC SELLER is
# payment.seller — the upstream credential, endpoint, listing and price all
# resolve against THAT seller's custody entry, with three fail-closed
# preflight gates BEFORE any upstream quota:
#
#   424 SELLER_KEY_MISSING        no custody entry / unwrappable credential
#   424 LISTING_NOT_BOUND         no listing / inactive / listing endpoint
#                                 not canonical == cfg.public_origin
#   424 DELEGATE_NOT_AUTHORIZED   Escrow.settleDelegateOf(seller) != shared
#                                 signer (or the read itself fails — v3.1)
#
# Every 424 carries an X-Error-Code response header. Missing/deactivated/
# endpoint-mismatched/unapproved-delegate NEVER forward. Enrollment stays
# prelisting-allowed (the custody endpoints are unchanged).
# ============================================================ M15 R2 shared buyer flow

CODE_SELLER_KEY_MISSING = "SELLER_KEY_MISSING"
CODE_LISTING_NOT_BOUND = "LISTING_NOT_BOUND"
CODE_DELEGATE_NOT_AUTHORIZED = "DELEGATE_NOT_AUTHORIZED"


def _precondition(code: str) -> HTTPException:
    """Frozen R2 fail-closed face: 424 + X-Error-Code response header."""
    return HTTPException(status_code=424, detail=code, headers={"X-Error-Code": code})


@dataclass(frozen=True)
class SharedSellerContext:
    """Per-request seller execution context (R2 minimal helper — one place
    builds it, every security check path consumes it; no duplicated gate
    code, no architecture drift). `api_key` lives in memory for THIS
    request only and is never logged/reflected.

    S2-C1: `catalog` is the STORED-CATALOG SNAPSHOT captured atomically with
    the credential at resolve time (same keystore entry read). The buyer
    flow consumes THIS snapshot in its post-await pre-forward stage — never
    a fresh `st.keystore.get(...)` dereference, which a concurrent signed
    DELETE /sellers/keys can turn into None (AttributeError → 500) or a
    rotated entry (credential/catalog incoherence)."""

    seller: str  # lowercased economic seller (payment.seller)
    api_key: str  # plaintext upstream credential (memory only)
    catalog: list[dict[str, Any]]  # stored-catalog snapshot (same entry as api_key)
    upstream_base: str  # canonical stored base (no terminal /v1)
    upstream_url: str  # canonical base + /v1/chat/completions
    upstream_host: str  # host for receipts + provider checks
    listing: dict[str, Any]  # active Registry listing bound to this relay

    @property
    def forward_headers(self) -> dict[str, str]:
        """PER-REQUEST Authorization — the shared http client's own headers
        are never mutated (concurrent A/B isolation)."""
        return {"Authorization": f"Bearer {self.api_key}"}


async def _resolve_shared_seller_context(st: RelayState, seller_lower: str) -> SharedSellerContext:
    """Fail-closed preflight (frozen order) — runs BEFORE any upstream
    quota. Chain read failures fail closed under the matching gate face:
    a listing read that cannot prove the binding refuses with
    LISTING_NOT_BOUND, a delegate read that cannot prove approval (e.g.
    current-chain v3.1 without the function) refuses with
    DELEGATE_NOT_AUTHORIZED — clear reports, never fabricated success."""
    entry = st.keystore.get(seller_lower)
    if entry is None:
        raise _precondition(CODE_SELLER_KEY_MISSING)
    api_key = st.keystore.unwrap_api_key(seller_lower)
    if api_key is None:
        logger.warning(
            "shared flow: custody entry present but credential unwrap failed "
            "(seller=%s) — refusing (fail closed)",
            seller_lower,
        )
        raise _precondition(CODE_SELLER_KEY_MISSING)

    chain = st.chain_for_binding()
    if chain is None:
        raise HTTPException(
            status_code=503,
            detail="chain unavailable — buyer flows refuse (fail closed)",
        )

    try:
        listing = await asyncio.to_thread(chain.get_listing, seller_lower)
    except Exception as exc:
        logger.warning(
            "shared flow: listing read failed for seller=%s (%s)", seller_lower, exc
        )
        raise _precondition(CODE_LISTING_NOT_BOUND) from exc
    if listing is None or not listing.get("active"):
        raise _precondition(CODE_LISTING_NOT_BOUND)
    try:
        listing_endpoint = normalize_custody_upstream(listing.get("endpoint") or "")
    except CustodyError:
        raise _precondition(CODE_LISTING_NOT_BOUND)
    if listing_endpoint != st.config.public_origin:
        raise _precondition(CODE_LISTING_NOT_BOUND)

    try:
        delegate = await asyncio.to_thread(chain.read_settle_delegate, seller_lower)
    except Exception as exc:
        # Clear fail-closed report: e.g. current-chain Escrow v3.1 has no
        # settleDelegateOf — shared cannot be served yet, refuse cleanly.
        logger.warning(
            "shared flow: delegate read unavailable for seller=%s (%s) — "
            "refusing (fail closed)",
            seller_lower,
            exc,
        )
        raise _precondition(CODE_DELEGATE_NOT_AUTHORIZED) from exc
    if delegate is None or delegate.lower() != st.signer_address.lower():
        raise _precondition(CODE_DELEGATE_NOT_AUTHORIZED)

    try:
        upstream_base = normalize_custody_upstream(entry.upstream_base_url)
    except CustodyError:
        logger.warning(
            "shared flow: stored upstream unparseable (seller=%s)", seller_lower
        )
        raise _precondition(CODE_SELLER_KEY_MISSING) from None
    if upstream_base != entry.upstream_base_url:
        logger.warning(
            "shared flow: stored upstream not canonical (seller=%s)", seller_lower
        )
        raise _precondition(CODE_SELLER_KEY_MISSING)

    return SharedSellerContext(
        seller=seller_lower,
        api_key=api_key,
        catalog=entry.catalog,  # S2-C1: coherent key+catalog snapshot
        upstream_base=upstream_base,
        upstream_url=f"{upstream_base}/v1/chat/completions",
        upstream_host=_upstream_host(upstream_base),
        listing=listing,
    )


def _check_model_servable_in_catalog(
    catalog: list[dict[str, Any]] | None, model_name: str, upstream_base: str
) -> None:
    """R2: the upstream model must be in the SELLER'S STORED catalog with
    servable true, and the live provider check uses THAT seller's validated
    upstream host (never the global OPENAI config)."""
    stored = any(
        isinstance(e, dict) and e.get("model") == model_name and e.get("servable") is True
        for e in (catalog or [])
    )
    if not stored:
        raise HTTPException(
            status_code=400,
            detail=f"model {model_name!r} is not servable on the seller's upstream catalog",
        )
    if provider_for_model(model_name) != host_provider(upstream_base):
        raise HTTPException(
            status_code=400,
            detail=f"model {model_name!r} does not match the seller's validated upstream host",
        )


async def _chat_completions_shared(st: RelayState, request: Request, raw_body: bytes) -> Any:
    """R2 shared buyer flow — auth semantics byte-identical to single, then
    the fail-closed seller preflight, then the SAME bearer/legacy execution
    paths with the seller context threaded through."""
    auth = (request.headers.get("Authorization") or "").strip()
    if auth.lower().startswith(BEARER_SCHEME) and auth[len(BEARER_SCHEME):].strip().startswith(
        BEARER_KEY_PREFIX
    ):
        grant = _decode_bearer_key(request.headers.get("Authorization"))
        payment = await asyncio.to_thread(st.chain.get_payment, grant.payment_id)
        _verify_bearer_grant(grant, payment)
        # M13-R revocation gate — same precedence as single.
        if grant.payment_id in st.revoked_api_keys:
            raise HTTPException(status_code=401, detail="revoked")
        return await _bearer_rest(
            st, request, raw_body, grant, payment,
            shared=await _resolve_shared_seller_context(
                st, str(payment["seller"]).lower()
            ),
        )
    payment_id = _parse_payment_id(request.headers.get("X-Payment-Id"))
    payment_id_str = str(payment_id)
    recovered = _verify_request_signature(request, raw_body, payment_id_str)
    payment = await asyncio.to_thread(st.chain.get_payment, payment_id)
    if recovered.lower() != str(payment["buyer"]).lower():
        raise HTTPException(status_code=402, detail="signer is not payment buyer")
    return await _legacy_rest(
        st, request, raw_body, payment_id, payment,
        shared=await _resolve_shared_seller_context(
            st, str(payment["seller"]).lower()
        ),
    )


@app.post(CHAT_COMPLETIONS_PATH)
async def chat_completions(request: Request) -> Any:
    st = _get_state()
    raw_body = await request.body()
    if st.config.is_shared:
        return await _chat_completions_shared(st, request, raw_body)
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

    return await _legacy_rest(st, request, raw_body, payment_id, payment, shared=None)


async def _legacy_rest(
    st: RelayState,
    request: Request,
    raw_body: bytes,
    payment_id: int,
    payment: dict[str, Any],
    *,
    shared: SharedSellerContext | None = None,
) -> Any:
    """Post-auth execution for the legacy path (single: relay's own seller;
    R2 shared: the ACTUAL economic seller context)."""
    if shared is None:
        # 2. Listing must be active; model must be listed (400s) — chain read
        #    in a worker thread.
        listing = await _load_listing(st)
    else:
        listing = shared.listing  # pre-validated: active + bound to this relay
    try:
        body = json.loads(raw_body)
    except json.JSONDecodeError as exc:
        raise HTTPException(status_code=400, detail="invalid JSON body") from exc
    if not isinstance(body, dict):
        raise HTTPException(status_code=400, detail="body must be a JSON object")
    _check_model(listing, body)
    if shared is None:
        _check_model_provider_consistency(body)
    # R2 shared: the official-catalog gate is REPLACED by the seller's
    # stored-catalog servable check (below) — the provider check uses THAT
    # seller's validated upstream host, never the global OPENAI config.
    model_name = str(body["model"])  # validated as a listed string above

    # 2b. Registry v2 per-model price (PIN: the three unit prices follow the
    #     REQUESTED model, fetched from the chain via getPrice).
    try:
        price_operator = shared.seller if shared is not None else st.chain.seller_address
        prices = await asyncio.to_thread(st.chain.get_price, price_operator, model_name)
    except NotActive as exc:
        raise HTTPException(
            status_code=400, detail="listing inactive or unregistered"
        ) from exc
    except ModelNotFound as exc:
        raise HTTPException(
            status_code=400, detail="model not in listing.models"
        ) from exc
    if shared is not None:
        # S2-C1 post-await pre-forward guard: the enrollment can be
        # concurrently revoked (signed DELETE /sellers/keys) while the
        # get_price await was in flight — a FRESH keystore re-read here may
        # be None (AttributeError → 500) or a ROTATED entry (credential/
        # catalog incoherence). Fail closed 424 SELLER_KEY_MISSING when the
        # key is gone; otherwise consume the CONTEXT's coherent snapshot
        # (catalog captured atomically with the credential at resolve
        # time) — never a fresh partial re-read. No locks are held across
        # the upstream call.
        if st.keystore.get(shared.seller) is None:
            raise _precondition(CODE_SELLER_KEY_MISSING)
        # R2: the model must ALSO be servable per the SELLER'S stored catalog.
        _check_model_servable_in_catalog(shared.catalog, model_name, shared.upstream_base)

    # 3. TTL margin guard: seller needs FORWARD_MARGIN_S to settle (409).
    if int(payment["expiresAt"]) - int(time.time()) < st.config.forward_margin_s:
        raise HTTPException(status_code=409, detail="payment ttl below forward margin")

    # 4. On-chain validity with the per-request minAmount estimate (402).
    min_amount = estimate_min_amount(
        prices, st.config.prompt_token_cap, st.config.completion_token_cap
    )
    valid_seller = shared.seller if shared is not None else st.chain.seller_address
    valid = await asyncio.to_thread(st.chain.is_valid, payment_id, valid_seller, min_amount)
    if not valid:
        raise HTTPException(status_code=402, detail="payment invalid (unpaid/wrong seller/expired)")

    receipt_seller = shared.seller if shared is not None else None
    receipt_host = shared.upstream_host if shared is not None else None

    # 5-6. Forward, price, settle, receipt (never swallow the LLM response).
    # The buyer's body is forwarded to the upstream VERBATIM — zero parameter
    # injection here. Kimi compatibility: injecting sampling parameters
    # (temperature/top_p, any value including 0) would be rejected by the
    # upstream with HTTP 400, so the relay must never add them.
    if body.get("stream"):
        return await _forward_stream(
            st, body, payment_id, prices, int(payment["maxAmount"]), model_name,
            upstream_url=shared.upstream_url if shared is not None else None,
            extra_headers=shared.forward_headers if shared is not None else None,
            receipt_seller=receipt_seller,
            upstream_host=receipt_host,
        )

    try:
        upstream = await st.http.post(
            shared.upstream_url if shared is not None else CHAT_COMPLETIONS_PATH,
            json=body,
            timeout=UPSTREAM_TIMEOUT_NON_STREAM,
            headers=shared.forward_headers if shared is not None else None,
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
        st, payment_id, usage, prices, int(payment["maxAmount"]), response, model_name,
        receipt_seller=receipt_seller, upstream_host=receipt_host,
    )


async def _chat_completions_bearer(st: RelayState, request: Request, raw_body: bytes) -> Any:
    """M13 bearer API-key path (stateless key, partial settle).

    Pipeline: decode the tsk1 key envelope (401 before any RPC) → one
    getPayment read → EIP-191 recover vs payload buyer AND payment buyer +
    hard expiry (all 401) → M13-R revocation gate (401 "revoked", ahead of
    every later 400/402) → listing/model/provider/prices gates (400s, shared
    with legacy) → Escrow.isValid (402) — the legacy 409 FORWARD_MARGIN guard
    is intentionally skipped (PIN) → forward → capture into the ledger +
    threshold/TTL settlePartial flush → X-Receipt (per call) + X-Settle-Status.
    """
    grant = _decode_bearer_key(request.headers.get("Authorization"))
    payment = await asyncio.to_thread(st.chain.get_payment, grant.payment_id)
    _verify_bearer_grant(grant, payment)

    # M13-R revocation gate — FIRST among the post-grant gates: a revoked key
    # reports 401 "revoked" ahead of every 400/402 (listing/model/prices/
    # isValid/budget). Ordering note: the grant's own hard expiry lives inside
    # _verify_bearer_grant, so a key that is BOTH expired and revoked reports
    # "api key expired" (the contract pins the revoked check to this point).
    # Memory-only: lost on restart — TTL + maxAmount backstops (RelayState).
    if grant.payment_id in st.revoked_api_keys:
        raise HTTPException(status_code=401, detail="revoked")

    return await _bearer_rest(st, request, raw_body, grant, payment, shared=None)


async def _bearer_rest(
    st: RelayState,
    request: Request,
    raw_body: bytes,
    grant: BearerGrant,
    payment: dict[str, Any],
    *,
    shared: SharedSellerContext | None = None,
) -> Any:
    """Post-grant execution for the bearer path (single: relay's own seller;
    R2 shared: the ACTUAL economic seller context)."""
    if shared is None:
        listing = await _load_listing(st)
    else:
        listing = shared.listing
    try:
        body = json.loads(raw_body)
    except json.JSONDecodeError as exc:
        raise HTTPException(status_code=400, detail="invalid JSON body") from exc
    if not isinstance(body, dict):
        raise HTTPException(status_code=400, detail="body must be a JSON object")
    _check_model(listing, body)
    if shared is None:
        _check_model_provider_consistency(body)
    # R2 shared: the seller's stored-catalog servable check (below) replaces
    # the official-catalog gate — provider check vs the seller's host.
    model_name = str(body["model"])

    try:
        price_operator = shared.seller if shared is not None else st.chain.seller_address
        prices = await asyncio.to_thread(st.chain.get_price, price_operator, model_name)
    except NotActive as exc:
        raise HTTPException(
            status_code=400, detail="listing inactive or unregistered"
        ) from exc
    except ModelNotFound as exc:
        raise HTTPException(
            status_code=400, detail="model not in listing.models"
        ) from exc
    if shared is not None:
        # S2-C1 post-await pre-forward guard (bearer path — same contract as
        # the legacy path): a concurrent signed DELETE during the awaited
        # get_price must surface as 424 SELLER_KEY_MISSING with ZERO
        # upstream quota/settlement — never a 500; the servability check
        # consumes the CONTEXT's coherent snapshot, never a fresh re-read.
        if st.keystore.get(shared.seller) is None:
            raise _precondition(CODE_SELLER_KEY_MISSING)
        _check_model_servable_in_catalog(shared.catalog, model_name, shared.upstream_base)

    # NO 409 margin guard here — PIN: the bearer path replaces it with the
    # hard expiry check in _verify_bearer_grant.

    min_amount = estimate_min_amount(
        prices, st.config.prompt_token_cap, st.config.completion_token_cap
    )
    valid_seller = shared.seller if shared is not None else st.chain.seller_address
    valid = await asyncio.to_thread(st.chain.is_valid, grant.payment_id, valid_seller, min_amount)
    if not valid:
        raise HTTPException(
            status_code=402, detail="payment invalid (unpaid/wrong seller/expired)"
        )

    # SEC1-3: seed the ledger from the chain's capturedOf (no-op once the
    # entry exists) so the budget gate works against the REAL accrued total
    # after a relay restart, not a reset-to-0 guess.
    # rev-6 L2 decision — 503 SHORT-CIRCUIT: this path sits BEFORE forwarding
    # and its only purpose is an accurate `captured` for the admission gates
    # below; a failed seed would otherwise over-admit (fake 0) into doomed
    # OverMax flushes. 402 would lie ("payment problem") about what is a
    # transient relay-side condition — same class as the existing 503
    # "relay not initialized". No entry; the next request retries seeding.
    captured = await asyncio.to_thread(
        st.ledger.seed_and_get,
        grant.payment_id,
        lambda: st.chain.captured_of(grant.payment_id),
    )
    if captured is None:
        raise HTTPException(
            status_code=503,
            detail=(
                "capturedOf read failed — ledger not seeded, retry shortly"
            ),
        )

    # SEC1-1 budget admission gate — BEFORE forwarding. ① The CHAIN budget is
    # authoritative: a self-minted grant.m can never extend it, and once the
    # remaining headroom cannot cover a min-priced call the relay must refuse
    # service instead of serving for free until ttl.
    chain_budget = int(payment["maxAmount"])
    chain_remaining = chain_budget - captured
    if chain_remaining < min_amount:
        raise HTTPException(
            status_code=402,
            detail={
                "error": "payment budget exhausted",
                "remaining": max(chain_remaining, 0),
                "minAmount": min_amount,
            },
        )
    # ② grant.m is the buyer's own (possibly tighter) limit: respected —
    # refusal to serve once its headroom cannot cover a call either.
    grant_remaining = grant.max_amount - captured
    if grant_remaining < min_amount:
        raise HTTPException(
            status_code=402,
            detail={
                "error": "api key limit exhausted",
                "remaining": max(grant_remaining, 0),
                "minAmount": min_amount,
            },
        )

    # Capture budget: effective cap = min(on-chain, grant.m), passed to the
    # ledger so plan_capture RE-BASES the entry budget every request (SEC1-1:
    # never a stale first-request cache). An inflated m is already bounded by
    # ①; min() additionally keeps the clamp honest against a shrunken chain
    # maxAmount read.
    max_amount = min(chain_budget, grant.max_amount)
    expires_at = int(payment["expiresAt"])

    receipt_seller = shared.seller if shared is not None else None
    receipt_host = shared.upstream_host if shared is not None else None

    if body.get("stream"):
        return await _forward_stream(
            st,
            body,
            grant.payment_id,
            prices,
            max_amount,
            model_name,
            partial_expires_at=expires_at,
            upstream_url=shared.upstream_url if shared is not None else None,
            extra_headers=shared.forward_headers if shared is not None else None,
            receipt_seller=receipt_seller,
            upstream_host=receipt_host,
        )

    try:
        upstream = await st.http.post(
            shared.upstream_url if shared is not None else CHAT_COMPLETIONS_PATH,
            json=body,
            timeout=UPSTREAM_TIMEOUT_NON_STREAM,
            headers=shared.forward_headers if shared is not None else None,
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
        st, grant.payment_id, usage, prices, max_amount, expires_at, model_name,
        receipt_seller=receipt_seller, upstream_host=receipt_host,
    )
    response.headers["X-Settle-Status"] = status
    response.headers["X-Receipt"] = encode_x_receipt(receipt)
    return response
