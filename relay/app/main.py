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

All amounts are native USDC units (6 dp). Zero hardcoded addresses/chainIds —
everything comes from env config; the seller key never appears in logs.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import os
import time
from contextlib import asynccontextmanager
from typing import Any, AsyncIterator

import httpx
from eth_account import Account
from eth_account.messages import encode_defunct
from eth_utils import keccak
from fastapi import FastAPI, HTTPException, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse, StreamingResponse

from .chain import ChainClient
from .config import (
    MODEL_PROVIDER_PREFIXES,
    OFFICIAL_UPSTREAM_HOSTS,
    TEE_KEY_PATH,
    Config,
    ConfigError,
    ENV_CORS_ORIGINS,
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
) -> StreamingResponse:
    """Transparent SSE passthrough. Usage is taken from the final chunk (with
    injected stream_options); settle happens after the stream drains — for a
    streaming response the outcome is observable via GET /receipt/{paymentId}."""
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
        settled, actual = await _try_settle(st, payment_id, usage, prices, max_amount)
        if settled:
            st.receipts.put(payment_id, _build_receipt(st, payment_id, usage, actual, model))
        else:
            logger.info(
                "stream settle-failed paymentId=%s (buyer may refund after ttl)",
                payment_id,
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
    prices = Prices(
        price_cached_in=listing["priceCachedIn"],
        price_input=listing["priceInput"],
        price_output=listing["priceOutput"],
    )
    try:
        body = json.loads(raw_body)
    except json.JSONDecodeError as exc:
        raise HTTPException(status_code=400, detail="invalid JSON body") from exc
    if not isinstance(body, dict):
        raise HTTPException(status_code=400, detail="body must be a JSON object")
    _check_model(listing, body)
    _check_model_provider_consistency(body)
    model_name = str(body["model"])  # validated as a listed string above

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
