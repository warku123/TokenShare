"""TokenShare buyer CLI — Typer app.

Commands (BUILD_SPEC §3 M4): deposit / lock / call / balance / refund,
plus `disputes` for reviewing failed-receipt records, `listings` /
`remove-model` / `verify-attestation`, and the M13 consumer-side pair
`mint-key` (stateless bearer API key) + `usage` (cumulative capture view).

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
import shlex
import time
from typing import Optional

import typer

from . import attestation as attestation_mod
from . import chain as chain_mod
from . import disputes as dispute_mod
from . import signing
from .config import load_config, load_seller_override
from .errors import TokenshareError
from .receipt import Receipt, ReceiptDecodeError, decode_receipt, verify_receipt
from .relay_client import get_usage, post_chat_json, open_chat_stream
from .signing import RELAY_CHAT_PATH
from .streaming import parse_sse_lines
from .units import display_usdc, parse_usdc_amount

_EXIT_ERR = 2

PROMPT_TOKEN_CAP_DEFAULT = 200_000
COMPLETION_TOKEN_CAP_DEFAULT = 32_000

_APP_HELP = """TokenShare buyer CLI — rent sellers' OpenAI API quota, paid in USDC via on-chain Escrow.

Env vars required by every chain-touching command (no values are ever hardcoded): BUYER_PRIVATE_KEY (buyer EVM key, 0x-hex; never logged or echoed), RPC_URL (JSON-RPC endpoint), CHAIN_ID (must match RPC_URL), ESCROW_ADDR, REGISTRY_ADDR, USDC_ADDR (6-decimal USDC ERC-20).

Optional env: SELLER_ADDR (default seller for call), PROMPT_TOKEN_CAP / COMPLETION_TOKEN_CAP (default lock sizing, mirrors relay defaults 200000 / 32000), TX_TIMEOUT_S (tx wait timeout).

Commands: deposit (approve + deposit USDC into Escrow); lock (lock(seller, maxAmount, ttl=600) -> prints paymentId); call (pick seller -> lock NEW paymentId, or reuse via --payment-id -> read Registry listing.endpoint -> POST /v1/chat/completions with EIP-191 X-Payment-Id + X-Signature -> prints reply, X-Settle-Status, and verifies the EIP-712 X-Receipt against the Registry listing operator); balance (wallet USDC + withdrawable Escrow); refund (withdraw an expired lock after its TTL); disputes (list locally recorded receipt-verification disputes); verify-attestation (best-effort off-chain parse of a TEE attestation quote + optional on-chain digest comparison); listings (compare ACTIVE Registry listings — per-model tiered prices + estimated per-call cost, cheapest first; sellers discovered via the Registry v3 on-chain enumeration sellerCount/getSellers); remove-model (OPERATOR-side: remove ONE model + its parallel price row from your own listing via Registry v4 removeModel, signed with the listing-operator key — --key-env, default BUYER_PRIVATE_KEY for the demo single-account setup); mint-key (M13: sign a stateless bearer API key tsk1.… bound to an existing Locked payment — agents call the relay with Authorization: Bearer, per-call capture keeps the payment Locked until TTL); usage (M13: cumulative captured/maxAmount/remaining view via GET /payment/{id}/usage).

Receipt verification (BUILD_SPEC §6.3): a failed X-Receipt check prints a warning and records the paymentId in the dispute ledger (default ~/.tokenshare/disputes.json; override with --disputes-file, review via the `disputes` command).

TEE attestation (M7-A): `verify-attestation` does best-effort off-chain parsing of a dstack/Phala TDX quote (--quote accepts raw hex or the relay's /attestation JSON) — header fields, the report-data address binding, and the keccak256 digest that the seller anchors via the AttestationAnchor contract; --anchor <addr> compares against the on-chain digest. Full cryptographic verification needs dstack-verifier docker (the command says so).

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
# mint-key (M13: stateless signed API key for agent consumers)
# ---------------------------------------------------------------------------


@app.command("mint-key")
def mint_key_cmd(
    payment_id: int = typer.Option(..., "--payment-id", help="paymentId of an existing Locked payment to bind the key to."),
    ttl_override: Optional[int] = typer.Option(
        None,
        "--ttl-override",
        help="Shorten the key expiry to now + <seconds> (never extends past the payment's expiresAt).",
    ),
    relay: Optional[str] = typer.Option(None, "--relay", help="Override the relay base URL (default: Registry listing endpoint of the payment's seller)."),
    json_output: bool = typer.Option(False, "--json", help="Emit a single JSON object (apiKey/baseUrl/expiry/…) for scripts."),
) -> None:
    """Mint a stateless bearer API key (tsk1.…) bound to a Locked payment.

    Signs the EIP-191 message `TokenShare API key grant|paymentId={p}|expiry={e}|maxAmount={m}`
    with BUYER_PRIVATE_KEY and emits `tsk1.<b64url(payload)>.<b64url(sig)>`
    where payload = {p,e,m,buyer}. Agents use the key as
    `Authorization: Bearer …` against the relay base URL; the relay recovers
    the signer per request (must equal payment.buyer), captures per call and
    keeps the payment Locked until the TTL. Zero storage: the key cannot be
    revoked — wait out the TTL (max leak = maxAmount).
    """
    def body() -> None:
        cfg = load_config()
        ctx = chain_mod.open_chain(cfg)
        payment = chain_mod.get_payment(ctx, payment_id)
        if _state_of(payment) != "Locked":
            _fail(
                f"payment {payment_id} is not Locked (state={_state_of(payment)}); "
                "mint-key binds to an existing Locked payment (run `lock` first)"
            )
        payment_buyer = str(payment.get("buyer") or "")
        if not _same_addr(payment_buyer, ctx.address):
            _fail(
                f"payment {payment_id} was locked by {payment_buyer}, not by this "
                f"buyer ({ctx.address}); refusing to mint"
            )
        now = int(time.time())
        expires_at = int(payment["expires_at"])
        if expires_at <= now:
            _fail(
                f"payment {payment_id} already expired at unix {expires_at} — "
                "a minted key would be dead on arrival (refund it instead)"
            )
        expiry = expires_at
        if ttl_override is not None:
            if ttl_override <= 0:
                _fail("--ttl-override must be a positive number of seconds")
            expiry = min(expires_at, now + int(ttl_override))
            if expiry <= now:
                _fail("--ttl-override is shorter than the minting time — key would be already expired")
        seller = str(payment.get("seller") or "")
        listing = chain_mod.get_listing(ctx, seller)
        if str(listing.get("operator") or "") in ("", "0x" + "0" * 40):
            _fail("no listing registered for the payment's seller on the Registry")
        endpoint = relay or str(listing.get("endpoint") or "")
        if not endpoint:
            _fail("listing endpoint is empty; pass --relay to override")
        if relay is not None:
            _warn_if_plain_http(relay)
        from eth_utils import to_checksum_address

        buyer_cs = to_checksum_address(payment_buyer)
        api_key = signing.mint_api_key(cfg.private_key_hex, payment_id, expiry, int(payment["max_amount"]), buyer_cs)
        models = [str(m) for m in (listing.get("models") or []) if str(m).strip()]
        model_name = models[0] if models else "(model)"
        base = endpoint.rstrip("/")

        if json_output:
            typer.echo(
                json.dumps(
                    {
                        "paymentId": int(payment_id),
                        "buyer": buyer_cs,
                        "seller": seller,
                        "expiry": expiry,
                        "maxAmount": int(payment["max_amount"]),
                        "apiKey": api_key,
                        "baseUrl": base,
                        "model": model_name,
                    },
                    indent=2,
                )
            )
            return

        typer.echo(f"paymentId: {payment_id}")
        typer.echo(f"buyer: {buyer_cs}")
        typer.echo(f"seller: {seller}")
        if not listing.get("active"):
            _echo_err("warning: the seller's listing is currently INACTIVE — the key only works once it is re-activated")
        typer.echo(f"API key: {api_key}")
        typer.echo(f"expiry: unix {expiry}")
        typer.echo(f"maxAmount: {display_usdc(int(payment['max_amount']))}")
        typer.echo(f"Relay base URL: {base}")
        typer.echo(f"Model: {model_name}")
        curl_body = json.dumps(
            {"model": model_name, "messages": [{"role": "user", "content": "Hello from TokenShare"}]},
            separators=(",", ":"),
        )
        typer.echo("Example (agent usage, per-call partial capture):")
        # sec-2 C1: base URL and model come from the ON-CHAIN listing (any
        # seller-controlled bytes: quotes/;/`/$()/newlines) — every such
        # interpolation in this COPY-PASTE shell example must be shlex.quoted
        # or pasting the example would execute injected commands (with the
        # freshly minted bearer key in the headers). shlex.quote leaves
        # metachar-free strings untouched, so benign output is unchanged.
        typer.echo(
            f"  curl {shlex.quote(base + '/v1/chat/completions')} \\\n"
            f'    -H "Authorization: Bearer {api_key}" \\\n'
            f'    -H "Content-Type: application/json" \\\n'
            f"    -d {shlex.quote(curl_body)}"
        )

    _run(body)


# ---------------------------------------------------------------------------
# usage (M13: cumulative capture view for a payment)
# ---------------------------------------------------------------------------


@app.command("usage")
def usage_cmd(
    payment_id: int = typer.Option(..., "--payment-id", help="paymentId to query the cumulative capture view for."),
    relay: Optional[str] = typer.Option(None, "--relay", help="Override the relay base URL (default: Registry listing endpoint of the payment's seller)."),
) -> None:
    """Show captured / maxAmount / remaining for a payment (GET /payment/{id}/usage).

    The usage endpoint is a paymentId direct query (no signature needed per
    the relay PIN) — it reflects the relay's cumulative view across all
    bearer-key calls, matching the on-chain SettlePartial captures.
    """
    def body() -> None:
        cfg = load_config()
        ctx = chain_mod.open_chain(cfg)
        payment = chain_mod.get_payment(ctx, payment_id)
        seller = str(payment.get("seller") or "")
        endpoint = relay
        if endpoint is None:
            listing = chain_mod.get_listing(ctx, seller)
            endpoint = str(listing.get("endpoint") or "")
        response = get_usage(endpoint, payment_id)
        try:
            data = response.json()
        except Exception:
            _fail(f"relay usage endpoint returned non-JSON: {response.body_text[:200]}")
        captured = int(data.get("captured", 0))
        max_amount = int(data.get("maxAmount", payment["max_amount"]))
        remaining = data.get("remaining")
        remaining = int(remaining) if remaining is not None else max_amount - captured
        typer.echo(f"paymentId: {payment_id}")
        typer.echo(f"captured: {display_usdc(captured)}")
        typer.echo(f"maxAmount: {display_usdc(max_amount)}")
        typer.echo(f"remaining: {display_usdc(remaining)}")

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
# verify-attestation (M7-A TEE)
# ---------------------------------------------------------------------------


@app.command("verify-attestation")
def verify_attestation_cmd(
    quote: str = typer.Option(
        ...,
        "--quote",
        help="TDX attestation quote payload: raw hex (0x optional) or the relay's /attestation JSON response.",
    ),
    anchor: Optional[str] = typer.Option(
        None,
        "--anchor",
        help="AttestationAnchor contract address; compares the quote digest with the on-chain digest (requires the chain env vars).",
    ),
    app_id: Optional[str] = typer.Option(
        None,
        "--app-id",
        help="appId the on-chain digest is read for (default: the TEE-derived address extracted from the quote).",
    ),
) -> None:
    """Best-effort off-chain parse of a TEE attestation quote (+ anchor check).

    Parses SGX/TDX quote header fields, locates the seller-address binding in
    the 64-byte report data, and computes keccak256(raw quote bytes) — the
    digest anchored via AttestationAnchor.anchor(bytes32). With --anchor, the
    computed digest is compared to the on-chain digest (exit 1 on mismatch).
    Full cryptographic verification (signature chain, MRTD/RTMR collateral)
    requires dstack-verifier docker or the Phala cloud-api verify endpoint.
    """
    def body() -> None:
        try:
            summary = attestation_mod.parse_quote(quote)
        except attestation_mod.AttestationError as exc:
            _fail(str(exc))

        typer.echo(f"Quote length: {summary.length} bytes")
        typer.echo(f"Digest (keccak256, for AttestationAnchor.anchor): {summary.digest}")
        header_bits = []
        for label, value in (
            ("version", summary.version),
            ("att_key_type", summary.att_key_type),
            ("qe_svn", summary.qe_svn),
            ("pce_svn", summary.pce_svn),
        ):
            if value is not None:
                header_bits.append(f"{label}={value}")
        if header_bits:
            typer.echo(f"Quote header (SGX quote v4): {' '.join(header_bits)}")
        else:
            typer.echo("Quote header: too short to parse (SGX quote v4 header is 48 bytes)")

        if summary.report_data:
            typer.echo(f"Report data: {summary.report_data}")
        if summary.derived_address:
            typer.echo(
                f"Derived address (TEE key path wallet/ethereum/tokenshare): "
                f"{summary.derived_address}"
            )
            if summary.binding_offset is not None:
                typer.echo(f"Address binding found at quote offset {summary.binding_offset}")
        else:
            typer.echo(
                "no report-data address binding found — the relay's /attestation "
                "endpoint binds the TEE-derived seller address into reportData "
                "(dstack key path 'wallet/ethereum/tokenshare'); a quote fetched "
                "directly via dstack get_quote(\"\") carries none"
            )

        typer.echo(
            "NOTE: this is structural parsing only — full off-chain verification "
            "(signature chain, MRTD/RTMR collateral) needs the dstack-verifier "
            "docker image, or POST https://cloud-api.phala.com/api/v1/attestations/verify"
        )

        if anchor is None:
            return

        # On-chain comparison: the anchored digest for the quote's appId.
        # (lookup_id resolved BEFORE load_config, so a missing --app-id
        # fails without demanding chain env vars.)
        lookup_id = app_id or summary.derived_address
        if not lookup_id:
            _fail(
                "no appId to read the anchor for: pass --app-id (the mapping key "
                "is the seller address that ran anchor(bytes32))"
            )
        cfg = load_config()
        typer.echo(f"On-chain digest (appId {lookup_id}): reading {anchor} …")
        onchain = attestation_mod.read_anchor_digest(cfg.rpc_url, anchor, lookup_id)
        typer.echo(f"On-chain digest: {onchain}")
        if onchain == "0x" + "00" * 32:
            _echo_err(
                f"warning: no digest anchored for appId {lookup_id} — run "
                f"`cast send {anchor} 'anchor(bytes32)' {summary.digest}` "
                "from the relay first"
            )
            raise typer.Exit(code=1)
        if onchain == summary.digest:
            typer.secho(
                "Match: OK — quote digest == anchored digest (quote not swapped "
                "since anchoring)",
                fg=typer.colors.GREEN,
            )
        else:
            _echo_err(
                f"MISMATCH: computed {summary.digest} != anchored {onchain} — "
                "the quote is not the one anchored on-chain"
            )
            raise typer.Exit(code=1)

    _run(body)


# ---------------------------------------------------------------------------
# listings (multi-seller comparison; M10: Registry v3 enumeration discovery)
# ---------------------------------------------------------------------------


def _short_addr(address: str) -> str:
    """0xf39F…92266 — table-display truncation; the full (copyable) address
    is printed in the address block under the table and in --json."""
    return f"{address[:6]}…{address[-4:]}"


def _token_caps() -> tuple[int, int]:
    """(prompt, completion) caps — same env names and defaults the relay uses
    for its minAmount estimate (PROMPT_TOKEN_CAP / COMPLETION_TOKEN_CAP,
    defaults 200000 / 32000)."""
    return (
        int(os.environ.get("PROMPT_TOKEN_CAP", PROMPT_TOKEN_CAP_DEFAULT)),
        int(os.environ.get("COMPLETION_TOKEN_CAP", COMPLETION_TOKEN_CAP_DEFAULT)),
    )


def _estimate_call_cost(
    price_input: int, price_output: int, prompt_cap: int, completion_cap: int
) -> int:
    """Estimated per-call cost in native USDC: (priceInput*P + priceOutput*C)
    // 1e6 — mirrors the relay-side minAmount model with the default caps.
    Assumes ZERO cached tokens; the relay's three-tier formula bills cache
    hits at priceCachedIn, so the real cost is <= this estimate."""
    return (int(price_input) * prompt_cap + int(price_output) * completion_cap) // 1_000_000


@app.command("listings")
def listings_cmd(
    model: Optional[str] = typer.Option(
        None,
        "--model",
        help="Only show rows for this model (each seller is priced per model in Registry v2).",
    ),
    json_output: bool = typer.Option(
        False,
        "--json",
        help="Output structured JSON instead of a table (for scripts / the web console).",
    ),
) -> None:
    """List ACTIVE Registry listings with per-model tiered prices + est. per-call cost.

    Registry v2 (M9 ABI PIN): prices are PER MODEL — getListing returns
    `models` and a parallel `prices` array, so every row is one
    (operator, model) price pair. Discovery: Registry v3 on-chain
    enumeration (M10 ABI PIN) — sellerCount() + getSellers(start, count)
    pagination replaces the old eth_getLogs Registered-event scan; the
    LISTINGS_FROM_BLOCK env is gone (breaking). Rows are sorted by
    estimated per-call cost (cheapest first); with --model only that model's
    rows are compared. Pick a seller's full address from the list for
    `call --seller` / `lock --seller`.
    """
    def body() -> None:
        cfg = load_config()
        ctx = chain_mod.open_chain(cfg)
        prompt_cap, completion_cap = _token_caps()
        operators = chain_mod.get_sellers(ctx.w3, cfg.registry_addr)

        rows: list[dict] = []
        seen_operators: set[str] = set()
        for operator in operators:
            # Discovery already dedups; this guard keeps the table correct
            # even if a future discovery layer returns raw events.
            if operator in seen_operators:
                continue
            seen_operators.add(operator)
            try:
                listing = chain_mod.get_listing(ctx, operator)
            except TokenshareError:
                raise
            except Exception as exc:
                raise TokenshareError(
                    f"Registry getListing({operator}) failed: {exc}"
                ) from exc
            if not listing.get("active"):
                continue
            models = [str(m) for m in (listing.get("models") or []) if str(m).strip()]
            prices = list(listing.get("prices") or [])
            # models and prices are parallel arrays (M9 ABI PIN); a malformed
            # (short) prices tail is skipped instead of crashing the table.
            for i, model_name in enumerate(models):
                if i >= len(prices):
                    continue
                price = prices[i]
                if model is not None and model_name != model:
                    continue
                rows.append(
                    {
                        "operator": operator,
                        "endpoint": str(listing.get("endpoint") or ""),
                        "model": model_name,
                        "priceCachedIn": int(price["cached_in"]),
                        "priceInput": int(price["input"]),
                        "priceOutput": int(price["output"]),
                        "estimatedCallCost": _estimate_call_cost(
                            price["input"], price["output"],
                            prompt_cap, completion_cap,
                        ),
                    }
                )
        rows.sort(key=lambda r: (r["estimatedCallCost"], r["operator"].lower(), r["model"]))

        if json_output:
            typer.echo(
                json.dumps(
                    {
                        "chainId": cfg.chain_id,
                        "registry": cfg.registry_addr,
                        "sellerCount": len(operators),
                        "activeOnly": True,
                        "model": model,
                        "count": len(rows),
                        "listings": rows,
                    },
                    indent=2,
                )
            )
            return

        if not operators:
            typer.echo(
                "no registered sellers on-chain (Registry sellerCount() == 0) — "
                "nobody has registered a listing yet."
            )
            return
        if not rows:
            if model is not None:
                typer.echo(
                    f"no ACTIVE listing serves model {model!r} "
                    f"({len(seen_operators)} registered operator(s) scanned)."
                )
                return
            typer.echo(
                f"{len(seen_operators)} registered operator(s), none ACTIVE — all "
                "listings were deactivated after registration."
            )
            return

        suffix = f", model {model!r}" if model is not None else ""
        typer.echo(
            f"Active listings ({len(rows)} model price rows / {len(seen_operators)} "
            f"seller(s), chainId {cfg.chain_id}{suffix}), sorted by estimated "
            "per-call cost:"
        )
        typer.echo("")
        header = [
            "#", "operator", "endpoint", "model",
            "cached/1M", "input/1M", "output/1M", "est/call",
        ]
        table = [header]
        for i, row in enumerate(rows, 1):
            table.append(
                [
                    str(i),
                    _short_addr(row["operator"]),
                    row["endpoint"] or "(none)",
                    row["model"],
                    display_usdc(row["priceCachedIn"]),
                    display_usdc(row["priceInput"]),
                    display_usdc(row["priceOutput"]),
                    display_usdc(row["estimatedCallCost"]),
                ]
            )
        widths = [max(len(row[c]) for row in table) for c in range(len(header))]
        for row in table:
            typer.echo(
                "  "
                + "  ".join(
                    cell.ljust(widths[c]) for c, cell in enumerate(row)
                ).rstrip()
            )
        typer.echo("")
        typer.echo(
            f"est/call = (priceInput*{prompt_cap} + priceOutput*{completion_cap})//1e6 "
            "native USDC for THAT model, 0 cached tokens assumed (cache hits bill "
            "at cached/1M, so the real cost is lower)."
        )
        typer.echo("Full seller addresses (for --seller):")
        printed_operators: set[str] = set()
        for row in rows:
            if row["operator"] in printed_operators:
                continue
            printed_operators.add(row["operator"])
            typer.echo(f"  {row['operator']}  ({row['endpoint'] or '(no endpoint)'})")

    _run(body)


# ---------------------------------------------------------------------------
# remove-model (M12 Registry v4 — OPERATOR-side, not a buyer operation)
# ---------------------------------------------------------------------------

# removeModel(model) must be signed by the LISTING OPERATOR (caller ==
# operator). This CLI's identity model is buyer-centric (BUYER_PRIVATE_KEY is
# the only PIN key), and in the common demo that SAME account is the
# seller/operator (SELLER==BUYER), so it is the default signer. When the
# operator key lives elsewhere, --key-env names any other env var — no new
# required env, config.py untouched.
OPERATOR_KEY_ENV_DEFAULT = "BUYER_PRIVATE_KEY"


@app.command("remove-model")
def remove_model_cmd(
    model: str = typer.Argument(
        ...,
        help="Model name to remove from the CALLER'S OWN Registry listing (its parallel price row is removed too).",
    ),
    key_env: str = typer.Option(
        OPERATOR_KEY_ENV_DEFAULT,
        "--key-env",
        help="ENV VAR NAME holding the listing-OPERATOR private key (default BUYER_PRIVATE_KEY — the demo single-account setup where seller==buyer; pass e.g. SELLER_PRIVATE_KEY when they differ). The value is never echoed.",
    ),
) -> None:
    """Remove one model (and its price) from your own listing (Registry v4 removeModel).

    Seller/operator-side command: removeModel(model) is signed by the listing
    operator and removes the model + its parallel prices[] entry via
    swap-and-pop. On-chain guards: ModelNotFound (model not in the listing)
    and RemoveLastModel (it is the last remaining model — deactivate() the
    whole listing instead). Allowed while the listing is inactive. After
    removal getPrice(operator, model) reverts — in-flight payments
    settle-fail and buyers refund after the TTL.
    """
    def body() -> None:
        key = (os.environ.get(key_env) or "").strip()
        if not key:
            _fail(
                f"env {key_env} is not set — remove-model signs as the LISTING "
                "OPERATOR; put the operator private key in that env var "
                "(value never echoed)"
            )
        from eth_account import Account

        try:
            account = Account.from_key(key)
        except Exception:
            _fail(f"env {key_env} is not a valid private key (value withheld)")
        cfg = load_config()
        ctx = chain_mod.open_chain(cfg)
        typer.echo(
            f"operator: {account.address}  registry: {cfg.registry_addr}  "
            f"chain_id: {cfg.chain_id}"
        )
        try:
            result = chain_mod.remove_model(ctx.w3, cfg.registry_addr, account, model)
        except TokenshareError as exc:
            _explain_remove_model_revert(model, exc)
        typer.echo(f"model removed: {result['model']}  (operator {result['operator']})")
        typer.echo(f"tx: {result['tx_hash']}")

    _run(body)


# Registry guard errors are deliberately NOT in the CLI ABI fragment, so web3
# surfaces their reverts as the raw 4-byte custom-error SELECTOR hex (e.g.
# "custom error 0x2e1a7d4d") instead of a name — the friendly hints must match
# the selector too (same precedent as the e2e probes in e2e/run.py:928-970).
# (name, canonical sighash, hint text).
_EXPLAIN_REMOVE_MODEL_ERRORS: tuple[tuple[str, str, str], ...] = (
    (
        "RemoveLastModel",
        "RemoveLastModel()",
        "is the LAST model of the listing — removeModel refuses "
        "to empty it; deactivate() the whole listing instead",
    ),
    (
        "ModelNotFound",
        "ModelNotFound()",
        "is not in your listing — nothing to remove",
    ),
    (
        "NotActive",
        "NotActive()",
        "cannot be operated on: the Registry reports this "
        "listing as INACTIVE — register()/reactivate it first",
    ),
)


def _explain_remove_model_revert(model: str, exc: TokenshareError) -> None:
    """Translate the on-chain removeModel guards into actionable hints;
    any other transaction failure is re-raised for the generic path.

    rev-7 L1: with the guard error absent from the ABI, web3's revert message
    carries only the selector hex — decode it against the known-error table
    above and name the matched guard even when the chain sent just the hex."""
    text = f"{type(exc).__name__} {exc} {getattr(exc, 'data', '')}"
    bare = text.replace("0x", "")
    from web3 import Web3

    for name, sighash, hint in _EXPLAIN_REMOVE_MODEL_ERRORS:
        selector = Web3.keccak(text=sighash)[:4].hex()
        if name in text or selector in bare:
            _fail(
                f"{model!r} {hint} [{name}; chain said: {text}]"
            )
    raise exc


# ---------------------------------------------------------------------------
# call
# ---------------------------------------------------------------------------


@app.command("call")
def call_cmd(
    prompt: str = typer.Argument(..., help="User prompt sent to the seller relay."),
    seller: Optional[str] = typer.Option(None, "--seller", help="Seller (relay operator) address; defaults to env SELLER_ADDR."),
    model: Optional[str] = typer.Option(None, "--model", help="Model name; defaults to the first model of the Registry listing; must be one of the listing's models."),
    max_amount: Optional[str] = typer.Option(
        None,
        "--max",
        help=(
            "Lock maxAmount in USDC (human). Default is estimated from the SELECTED MODEL's Registry prices "
            "with the CLI token caps (PROMPT_TOKEN_CAP/COMPLETION_TOKEN_CAP = 200000/32000); these may be out "
            "of sync with the relay-side caps — on HTTP 402 pass --max explicitly."
        ),
    ),
    ttl: int = typer.Option(600, "--ttl", help="TTL for the auto-lock (seconds)."),
    relay: Optional[str] = typer.Option(None, "--relay", help="Override the relay endpoint (default: Registry listing endpoint)."),
    payment_id: Optional[int] = typer.Option(None, "--payment-id", help="Reuse an existing Locked payment instead of locking a new one (not PIN-defined; when absent a NEW paymentId is always locked)."),
    stream: bool = typer.Option(False, "--stream", help="Pass stream=true and render SSE deltas (bonus feature)."),
    timeout: float = typer.Option(120.0, "--timeout", help="HTTP timeout seconds; also caps the receipt GET-poll deadline (max 30s)."),
) -> None:
    """One-shot paid call: pick seller -> lock -> POST /v1/chat/completions -> verify receipt.

    Flow: reads the Registry listing for the seller (endpoint + per-model
    prices, Registry v2), locks a NEW paymentId (unless --payment-id reuses
    one) sized from the SELECTED model's prices, signs the request with
    EIP-191 (X-Payment-Id + X-Signature), prints the model reply, the
    X-Settle-Status header and the EIP-712 receipt verification result.
    --model must be one of the listing's models (fast failure otherwise).
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
        # Registry v2 (M9): models and prices are parallel arrays; the whole
        # call is priced by the SELECTED model's three-tier price.
        models = [str(m) for m in (listing["models"] or []) if str(m).strip()]
        prices = list(listing.get("prices") or [])
        model_name = model or (models[0] if models else None)
        if not model_name:
            _fail("listing has no models and --model not given")
        if model_name not in models:
            _fail(
                f"model {model_name!r} is not in the listing of seller {seller_addr} "
                f"(models: {', '.join(models) or '(none)'})"
            )
        price = prices[models.index(model_name)] if models.index(model_name) < len(prices) else None
        if price is not None:
            typer.echo(
                f"model: {model_name}  (cached/1M: {display_usdc(price['cached_in'])}, "
                f"input/1M: {display_usdc(price['input'])}, "
                f"output/1M: {display_usdc(price['output'])})"
            )
        else:
            typer.echo(f"model: {model_name}")

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
            if max_amount is not None:
                units = parse_usdc_amount(max_amount, "--max")
            elif price is not None:
                units = _default_lock_amount(price)
            else:
                _fail(
                    f"listing prices missing for model {model_name!r}; pass --max explicitly"
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


def _default_lock_amount(price: dict) -> int:
    """Size the default lock maxAmount from the SELECTED MODEL's Registry
    prices (M9 per-model pricing) with the same token caps the relay uses by
    default (PROMPT_TOKEN_CAP / COMPLETION_TOKEN_CAP).

    Relay minAmount = (price.input*P + price.output*C) // 1e6  (floor to whole
    USDC-native units, i.e. the USDC cost of the estimate — the //1e6 here is
    what makes the default sane; Gate G R2 regression guard).
    The CLI locks that same estimate rounded UP to whole USDC (with a 1 USDC
    floor) so maxAmount >= relay minAmount always holds.
    """
    prompt_cap = int(os.environ.get("PROMPT_TOKEN_CAP", PROMPT_TOKEN_CAP_DEFAULT))
    completion_cap = int(os.environ.get("COMPLETION_TOKEN_CAP", COMPLETION_TOKEN_CAP_DEFAULT))
    estimate = (
        int(price["input"]) * prompt_cap + int(price["output"]) * completion_cap
    ) // 1_000_000  # = relay minAmount for this model (USDC-native)
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
            f"actual={display_usdc(receipt.actual_amount)} seller={receipt.seller} "
            f"upstreamHost={receipt.upstream_host} model={receipt.model}"
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
