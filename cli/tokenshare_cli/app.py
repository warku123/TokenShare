"""TokenShare buyer CLI — Typer app.

Commands (BUILD_SPEC §3 M4): deposit / lock / call / balance / refund,
plus `disputes` for reviewing failed-receipt records.

Env (required, PIN): BUYER_PRIVATE_KEY / RPC_URL / CHAIN_ID / ESCROW_ADDR /
REGISTRY_ADDR / USDC_ADDR. Optional: SELLER_ADDR (default seller for `call`),
PROMPT_TOKEN_CAP / COMPLETION_TOKEN_CAP (default lock sizing, mirrors relay
defaults 200000 / 32000), TX_TIMEOUT_S.

The default relay endpoint comes from Registry getListing(seller).endpoint;
`--relay` overrides it. Amount inputs are human USDC ("5" = 5 USDC =
5000000 native 6dp units); outputs always show native units + humanized.
"""

from dataclasses import dataclass
import json
import os
import time
from typing import Optional

import typer

from . import chain as chain_mod
from . import disputes as dispute_mod
from . import signing
from .config import load_config, load_seller_override
from .errors import TokenshareError
from .receipt import Receipt, ReceiptDecodeError, decode_receipt, verify_receipt
from .relay_client import post_chat_json, open_chat_stream
from .signing import RELAY_CHAT_PATH
from .streaming import parse_sse_lines
from .units import display_usdc, parse_usdc_amount

_EXIT_ERR = 2

PROMPT_TOKEN_CAP_DEFAULT = 200_000
COMPLETION_TOKEN_CAP_DEFAULT = 32_000

_APP_HELP = """TokenShare buyer CLI — rent sellers' OpenAI API quota, paid in USDC via on-chain Escrow.

Env vars required by every chain-touching command (no values are ever hardcoded): BUYER_PRIVATE_KEY (buyer EVM key, 0x-hex; never logged or echoed), RPC_URL (JSON-RPC endpoint), CHAIN_ID (must match RPC_URL), ESCROW_ADDR, REGISTRY_ADDR, USDC_ADDR (6-decimal USDC ERC-20).

Optional env: SELLER_ADDR (default seller for call), PROMPT_TOKEN_CAP / COMPLETION_TOKEN_CAP (default lock sizing, mirrors relay defaults 200000 / 32000), TX_TIMEOUT_S (tx wait timeout).

Commands: deposit (approve + deposit USDC into Escrow); lock (lock(seller, maxAmount, ttl=600) -> prints paymentId); call (pick seller -> lock NEW paymentId, or reuse via --payment-id -> read Registry listing.endpoint -> POST /v1/chat/completions with EIP-191 X-Payment-Id + X-Signature -> prints reply, X-Settle-Status, and verifies the EIP-712 X-Receipt against the Registry listing operator); balance (wallet USDC + withdrawable Escrow); refund (withdraw an expired lock after its TTL); disputes (list locally recorded receipt-verification disputes).

Receipt verification (BUILD_SPEC §6.3): a failed X-Receipt check prints a warning and records the paymentId in the dispute ledger (default ~/.tokenshare/disputes.json; override with --disputes-file, review via the `disputes` command).

Amounts are displayed as USDC 6dp native integers with a humanized conversion (1e6 native units = 1 USDC); amount inputs accept human USDC ("5" = 5000000 native).
"""

app = typer.Typer(
    name="tokenshare",
    help=_APP_HELP,
    no_args_is_help=True,
    context_settings={"help_option_names": ["-h", "--help"]},
)


def _echo_err(message: str) -> None:
    typer.secho(message, fg=typer.colors.RED, err=True)


def _fail(message: str) -> None:
    _echo_err(f"error: {message}")
    raise typer.Exit(code=_EXIT_ERR)


def _run(fn):
    """Execute a command body, mapping TokenshareError -> clean exit."""
    try:
        return fn()
    except TokenshareError as exc:
        _fail(str(exc))


def _hdr(headers: dict, name: str) -> str | None:
    lowered = name.lower()
    for key, value in headers.items():
        if key.lower() == lowered:
            return value
    return None


def _state_of(payment: dict) -> str:
    return payment["state_name"]


# ---------------------------------------------------------------------------
# global options
# ---------------------------------------------------------------------------


@dataclass
class Globals:
    disputes_file: Optional[str] = None


globals_opts = Globals()


@app.callback()
def main_callback(
    disputes_file: Optional[str] = typer.Option(
        None,
        "--disputes-file",
        help="Path of the local dispute ledger (default: ~/.tokenshare/disputes.json).",
    ),
) -> None:
    """TokenShare buyer CLI (see --help for env vars and receipt-verification notes)."""
    globals_opts.disputes_file = disputes_file


def _disputes_path() -> str | None:
    return globals_opts.disputes_file


# ---------------------------------------------------------------------------
# deposit
# ---------------------------------------------------------------------------


@app.command("deposit")
def deposit_cmd(
    amount: str = typer.Option(..., "--amount", help="USDC amount to deposit, e.g. 10.5 (= 10500000 native)."),
) -> None:
    """Approve USDC (when needed) and deposit it into the Escrow."""
    def body() -> None:
        cfg = load_config()
        units = parse_usdc_amount(amount, "--amount")
        if units <= 0:
            _fail("--amount must be greater than zero")
        ctx = chain_mod.open_chain(cfg)
        typer.echo(f"buyer: {ctx.address}  chain_id: {cfg.chain_id}")
        result = chain_mod.deposit(ctx, units)
        for step in result["steps"]:
            typer.echo(step)
        typer.echo(f"deposited: {display_usdc(result['amount'])}")
        typer.echo(f"Escrow balance: {display_usdc(result['escrow_balance'])}")

    _run(body)


# ---------------------------------------------------------------------------
# lock
# ---------------------------------------------------------------------------


@app.command("lock")
def lock_cmd(
    seller: str = typer.Option(..., "--seller", help="Seller (relay operator) address the payment locks to."),
    max_amount: str = typer.Option(..., "--max", help="Max settle amount in USDC (human), e.g. 5 = 5000000 native."),
    ttl: int = typer.Option(600, "--ttl", help="Lock TTL seconds (contract default 600)."),
) -> None:
    """Lock Escrow balance for a seller; prints the new paymentId."""
    def body() -> None:
        cfg = load_config()
        units = parse_usdc_amount(max_amount, "--max")
        if units <= 0:
            _fail("--max must be greater than zero")
        from eth_utils import to_checksum_address

        seller_addr = to_checksum_address(seller)
        ctx = chain_mod.open_chain(cfg)
        result = chain_mod.lock(ctx, seller_addr, units, ttl)
        _print_lock(result)

    _run(body)


def _print_lock(result: dict) -> None:
    typer.echo(f"paymentId: {result['payment_id']}")
    typer.echo(f"buyer: {result['buyer']}")
    typer.echo(f"seller: {result['seller']}")
    typer.echo(f"maxAmount: {display_usdc(result['max_amount'])}")
    typer.echo(f"expiresAt: unix {result['expires_at']}")
    typer.echo(f"tx: {result['tx_hash']}")


# ---------------------------------------------------------------------------
# balance
# ---------------------------------------------------------------------------


@app.command("balance")
def balance_cmd() -> None:
    """Show wallet USDC balance and withdrawable Escrow balance."""
    def body() -> None:
        cfg = load_config()
        ctx = chain_mod.open_chain(cfg)
        wallet = chain_mod.usdc_balance(ctx)
        escrow = chain_mod.escrow_balance(ctx)
        next_id = chain_mod.escrow_next_payment_id(ctx)
        typer.echo(f"buyer: {ctx.address}")
        typer.echo(f"USDC wallet balance: {display_usdc(wallet)}")
        typer.echo(f"Escrow balance (withdrawable): {display_usdc(escrow)}")
        typer.echo(f"Escrow nextPaymentId: {next_id}")

    _run(body)


# ---------------------------------------------------------------------------
# refund
# ---------------------------------------------------------------------------


@app.command("refund")
def refund_cmd(
    payment_id: int = typer.Option(..., "--payment-id", help="paymentId to refund (after its TTL elapsed)."),
) -> None:
    """Refund an expired lock back into the buyer's Escrow balance."""
    def body() -> None:
        cfg = load_config()
        ctx = chain_mod.open_chain(cfg)
        result = chain_mod.refund(ctx, payment_id)
        typer.echo(f"paymentId: {result['payment_id']} refunded")
        if result["amount"] is not None:
            typer.echo(f"refunded amount: {display_usdc(result['amount'])}")
        typer.echo(f"Escrow balance: {display_usdc(result['escrow_balance'])}")
        typer.echo(f"tx: {result['tx_hash']}")

    _run(body)


# ---------------------------------------------------------------------------
# disputes
# ---------------------------------------------------------------------------


@app.command("disputes")
def disputes_cmd() -> None:
    """List locally recorded receipt-verification disputes."""
    path = dispute_mod.resolve_path(_disputes_path())
    disputes = dispute_mod.load_disputes(path)
    typer.echo(f"disputes file: {path}")
    typer.echo(dispute_mod.format_disputes(disputes))


# ---------------------------------------------------------------------------
# call
# ---------------------------------------------------------------------------


@app.command("call")
def call_cmd(
    prompt: str = typer.Argument(..., help="User prompt sent to the seller relay."),
    seller: Optional[str] = typer.Option(None, "--seller", help="Seller (relay operator) address; defaults to env SELLER_ADDR."),
    model: Optional[str] = typer.Option(None, "--model", help="Model name; defaults to the first model of the Registry listing."),
    max_amount: Optional[str] = typer.Option(
        None,
        "--max",
        help=(
            "Lock maxAmount in USDC (human). Default is estimated from the CLI token caps "
            "(PROMPT_TOKEN_CAP/COMPLETION_TOKEN_CAP = 200000/32000); these may be out of sync "
            "with the relay-side caps — on HTTP 402 pass --max explicitly."
        ),
    ),
    ttl: int = typer.Option(600, "--ttl", help="TTL for the auto-lock (seconds)."),
    relay: Optional[str] = typer.Option(None, "--relay", help="Override the relay endpoint (default: Registry listing endpoint)."),
    payment_id: Optional[int] = typer.Option(None, "--payment-id", help="Reuse an existing Locked payment instead of locking a new one (not PIN-defined; when absent a NEW paymentId is always locked)."),
    stream: bool = typer.Option(False, "--stream", help="Pass stream=true and render SSE deltas (bonus feature)."),
    timeout: float = typer.Option(120.0, "--timeout", help="HTTP timeout seconds; also caps the receipt GET-poll deadline (max 30s)."),
) -> None:
    """One-shot paid call: pick seller -> lock -> POST /v1/chat/completions -> verify receipt.

    Flow: reads the Registry listing for the seller (endpoint + prices), locks
    a NEW paymentId (unless --payment-id reuses one), signs the request with
    EIP-191 (X-Payment-Id + X-Signature), prints the model reply, the
    X-Settle-Status header and the EIP-712 receipt verification result.
    """
    def body() -> None:
        cfg = load_config()
        seller_addr = seller or load_seller_override()
        if not seller_addr:
            _fail(
                "no seller specified: pass --seller 0x… (or set env SELLER_ADDR); "
                "the relay endpoint comes from the Registry listing of that seller"
            )
        from eth_utils import to_checksum_address

        seller_addr = to_checksum_address(seller_addr)

        ctx = chain_mod.open_chain(cfg)
        listing = chain_mod.get_listing(ctx, seller_addr)
        _require_listing(listing, relay)

        endpoint = relay or listing["endpoint"]
        if relay is not None:
            _warn_if_plain_http(relay)
        model_name = model or next((m for m in (listing["models"] or []) if str(m).strip()), None)
        if not model_name:
            _fail("listing has no models and --model not given")

        expected_seller = str(listing["operator"] or "")

        active_pid = payment_id
        if active_pid is not None:
            existing = chain_mod.get_payment(ctx, active_pid)
            if _state_of(existing) != "Locked":
                _fail(
                    f"payment {active_pid} is not Locked (state={_state_of(existing)}); "
                    "cannot reuse it"
                )
            payment_buyer = str(existing.get("buyer") or "")
            if payment_buyer and not _same_addr(payment_buyer, ctx.address):
                _fail(
                    f"payment {active_pid} was locked by {payment_buyer}, not by this "
                    f"buyer ({ctx.address}); refusing to reuse it"
                )
            typer.echo(f"paymentId: {active_pid} (reused lock)")
        else:
            units = (
                parse_usdc_amount(max_amount, "--max")
                if max_amount is not None
                else _default_lock_amount(listing)
            )
            lock_result = chain_mod.lock(ctx, seller_addr, units, ttl)
            _print_lock(lock_result)
            active_pid = lock_result["payment_id"]

        body_bytes = _chat_body(prompt, model_name, stream)
        signature = signing.sign_request(cfg.private_key, "POST", RELAY_CHAT_PATH, body_bytes, active_pid)

        if stream:
            _run_stream(endpoint, active_pid, signature, body_bytes, expected_seller, timeout, cfg.chain_id)
        else:
            _run_json(endpoint, active_pid, signature, body_bytes, expected_seller, timeout, cfg.chain_id)

    _run(body)


def _require_listing(listing: dict, relay_override: str | None) -> None:
    operator = str(listing["operator"] or "")
    if operator in ("", "0x" + "0" * 40):
        _fail("no listing registered for this seller on the Registry")
    if not listing["active"]:
        _fail("seller listing is inactive (deactivated); refusing to call")
    if not relay_override and not listing["endpoint"]:
        _fail("listing endpoint is empty; pass --relay to override")


def _default_lock_amount(listing: dict) -> int:
    """Size the default lock maxAmount from Registry prices with the same
    token caps the relay uses by default (PROMPT_TOKEN_CAP / COMPLETION_TOKEN_CAP).

    Relay minAmount = (priceInput*P + priceOutput*C) // 1e6  (floor to whole
    USDC-native units, i.e. the USDC cost of the estimate).
    The CLI locks that same estimate rounded UP to whole USDC (with a 1 USDC
    floor) so maxAmount >= relay minAmount always holds.
    """
    prompt_cap = int(os.environ.get("PROMPT_TOKEN_CAP", PROMPT_TOKEN_CAP_DEFAULT))
    completion_cap = int(os.environ.get("COMPLETION_TOKEN_CAP", COMPLETION_TOKEN_CAP_DEFAULT))
    total_native = (
        listing["price_input"] * prompt_cap + listing["price_output"] * completion_cap
    )
    estimate = total_native // 1_000_000  # = relay minAmount (USDC-native)
    amount = max(((estimate + 999_999) // 1_000_000) * 1_000_000, 1_000_000)
    return amount


def _chat_body(prompt: str, model: str, stream: bool) -> bytes:
    payload = {
        "model": model,
        "messages": [{"role": "user", "content": prompt}],
        "stream": stream,
    }
    return json.dumps(payload, separators=(",", ":")).encode("utf-8")


def _run_json(endpoint: str, payment_id: int, signature: str, body_bytes: bytes, expected_seller: str, timeout: float, chain_id: int) -> None:
    typer.echo(f"POST {endpoint.rstrip('/')}{RELAY_CHAT_PATH}")
    response = post_chat_json(endpoint, payment_id, signature, body_bytes, timeout)
    typer.echo(f"HTTP status: {response.status_code}")
    settle = _hdr(response.headers, "X-Settle-Status")
    typer.echo(f"Settle status: {settle or '(header missing)'}")

    try:
        payload = response.json()
    except Exception:
        payload = {}
    reply = ""
    choices = (payload or {}).get("choices") or []
    if choices:
        reply = ((choices[0].get("message") or {}).get("content")) or ""
    typer.echo("Reply:")
    typer.echo(reply if reply else response.body_text)

    usage = (payload or {}).get("usage") or {}
    if usage:
        cached = ((usage.get("prompt_tokens_details") or {}).get("cached_tokens")) or 0
        typer.echo(
            f"Usage: prompt={usage.get('prompt_tokens')} cached={cached} "
            f"completion={usage.get('completion_tokens')}"
        )

    _verify_and_report(endpoint, payment_id, response.headers, expected_seller, timeout, chain_id)


def _run_stream(endpoint: str, payment_id: int, signature: str, body_bytes: bytes, expected_seller: str, timeout: float, chain_id: int) -> None:
    handle = open_chat_stream(endpoint, payment_id, signature, body_bytes, timeout)
    typer.echo(f"HTTP status: {handle.status_code} (stream)")
    settle = _hdr(handle.headers, "X-Settle-Status")
    try:
        text, usage = parse_sse_lines(handle.lines)
    finally:
        _close_stream(handle)
    typer.echo("Reply:")
    typer.echo(text)
    if usage:
        cached = ((usage.get("prompt_tokens_details") or {}).get("cached_tokens")) or 0
        typer.echo(
            f"Usage: prompt={usage.get('prompt_tokens')} cached={cached} "
            f"completion={usage.get('completion_tokens')}"
        )
    typer.echo(f"Settle status: {settle or '(header missing)'}")
    _verify_and_report(endpoint, payment_id, handle.headers, expected_seller, timeout, chain_id)


def _close_stream(handle) -> None:
    """n2: parse_sse_lines breaks on `data: [DONE]` for parsing, but the httpx
    Client + stream response must not leak. We close (aclose) them right after
    parsing — the relay settles server-side shortly after [DONE] passthrough,
    so holding the connection is not needed."""
    close = getattr(handle, "close", None)
    if close is None:
        return
    try:
        close()
    except Exception:
        pass


def _verify_and_report(
    endpoint: str,
    payment_id: int,
    headers: dict,
    expected_seller: str,
    timeout: float = 30.0,
    chain_id: int | None = None,
) -> None:
    """Verify X-Receipt == Registry listing operator; warn + dispute on failure.

    The receipt EIP-712 domain is additionally asserted against the PIN
    {name:"TokenShare Relay", version:"1", chainId:<cfg.chain_id>} (m1).
    """
    disputes_path = dispute_mod.resolve_path(_disputes_path())
    raw = _hdr(headers, "X-Receipt")
    receipt: Receipt | None = None
    if not raw:
        raw = _fetch_receipt_via_get(endpoint, payment_id, timeout)
    if not raw:
        _echo_err(
            f"warning: no receipt available for payment {payment_id} "
            f"(polled GET /receipt/{payment_id} for up to {min(max(timeout, 0.0), 30.0):g}s; "
            "settle-failed? you may refund after the TTL)"
        )
        return
    try:
        receipt = decode_receipt(raw)
    except ReceiptDecodeError as exc:
        _echo_err(f"warning: receipt could not be decoded for payment {payment_id}: {exc}")
        dispute_mod.record_dispute(
            disputes_path, payment_id, dispute_mod.REASON_DECODE_FAILED,
            expected_seller, None, None, str(exc),
        )
        typer.echo(f"Dispute recorded: payment {payment_id} -> {disputes_path}")
        return

    check = verify_receipt(receipt, expected_seller, payment_id, chain_id)
    if check.ok and check.reason is None:
        typer.echo(f"Receipt verification: OK (recovered={check.recovered} == seller/operator)")
        typer.echo(
            f"Receipt: paymentId={receipt.payment_id} prompt={receipt.prompt_tokens} "
            f"cached={receipt.cached_tokens} completion={receipt.completion_tokens} "
            f"actual={display_usdc(receipt.actual_amount)} seller={receipt.seller}"
        )
        return

    _echo_err(
        f"warning: receipt verification FAILED for payment {payment_id}: "
        f"reason={check.reason} recovered={check.recovered} expected_seller={expected_seller}"
    )
    reason_map = {
        "recover-mismatch": dispute_mod.REASON_RECOVER_MISMATCH,
        "seller-mismatch": dispute_mod.REASON_SELLER_MISMATCH,
        "paymentid-mismatch": dispute_mod.REASON_PAYMENT_ID_MISMATCH,
        "domain-mismatch": dispute_mod.REASON_DOMAIN_MISMATCH,
    }
    dispute_mod.record_dispute(
        disputes_path, payment_id,
        reason_map.get(check.reason or "", dispute_mod.REASON_DECODE_FAILED),
        expected_seller, check.recovered, receipt.seller,
    )
    typer.echo(f"Dispute recorded: payment {payment_id} -> {disputes_path}")


def _fetch_receipt_via_get(endpoint: str, payment_id: int, timeout: float) -> str | None:
    """GET {endpoint}/receipt/{paymentId} fallback, polled (M1).

    The relay settles 0.3-2s AFTER the response/[DONE] passthrough lands on
    the wire, so a single GET is almost always a 404 and used to produce a
    misleading "settle-failed" warning. We poll every 0.5s until
    deadline = min(--timeout, 30s) expires; only then do we give up and let
    the caller print the settle-failed warning. The caller has already
    closed/drained the stream response before we poll.
    """
    import httpx as _httpx
    from urllib.parse import quote

    base = endpoint.rstrip("/")
    url = f"{base}/receipt/{quote(str(int(payment_id)), safe='')}"
    request_timeout = min(max(timeout, 0.1), 30.0)
    deadline = time.monotonic() + min(max(timeout, 0.0), 30.0)
    poll_interval = 0.5
    while True:
        try:
            resp = _httpx.get(url, timeout=request_timeout)
        except Exception:
            resp = None
        if resp is not None and resp.status_code == 200:
            text = resp.text.strip()
            if not text:
                return None
            if text.startswith("{"):
                try:
                    return _b64_of(json.loads(text))
                except Exception:
                    return None
            return text
        if time.monotonic() >= deadline:
            return None
        time.sleep(min(poll_interval, max(0.0, deadline - time.monotonic())))


def _warn_if_plain_http(endpoint: str) -> None:
    """m2: warn when --relay is neither https nor a loopback target."""
    from urllib.parse import urlparse

    parsed = urlparse(endpoint)
    scheme = (parsed.scheme or "").lower()
    host = (parsed.hostname or "").lower()
    if scheme == "https" or host in ("localhost", "127.0.0.1", "::1"):
        return
    _echo_err(
        f"warning: --relay {endpoint!r} uses '{scheme or 'no'}' scheme (not https); "
        "payment headers (X-Payment-Id/X-Signature) would travel in cleartext"
    )


def _same_addr(a: str, b: str) -> bool:
    from eth_utils import to_checksum_address

    try:
        return to_checksum_address(a) == to_checksum_address(b)
    except Exception:
        return False


def _b64_of(obj: dict) -> str:
    import base64

    raw = json.dumps(obj, separators=(",", ":")).encode("utf-8")
    return base64.urlsafe_b64encode(raw).decode("ascii").rstrip("=")
