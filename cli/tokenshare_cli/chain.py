"""On-chain access layer (web3). All values come from EnvConfig — the code
contains zero hardcoded addresses, chain ids, or RPC endpoints.

Every public function opens its own chain handle from a config, which keeps
the Typer commands thin and the tests patchable without a running node.
"""

from dataclasses import dataclass
import os
from typing import Any

from eth_account.signers.local import LocalAccount
from eth_utils import to_checksum_address
from web3 import Web3
from web3.contract import Contract

from .abis import ERC20_ABI, ESCROW_ABI, REGISTRY_ABI
from .config import EnvConfig
from .errors import ConfigError, EnvError, TokenshareError

STATE_NAMES = {0: "None", 1: "Locked", 2: "Settled", 3: "Refunded"}

_TX_TIMEOUT_S_DEFAULT = 180.0


@dataclass
class ChainContext:
    cfg: EnvConfig
    w3: Web3
    account: LocalAccount
    escrow: Contract
    registry: Contract
    usdc: Contract

    @property
    def address(self) -> str:
        return self.account.address


def open_chain(cfg: EnvConfig) -> ChainContext:
    """Connect to the RPC and validate chain id + contracts."""
    w3 = Web3(Web3.HTTPProvider(cfg.rpc_url))
    try:
        connected = w3.is_connected()
    except Exception as exc:
        raise ConfigError(f"cannot reach RPC_URL {cfg.rpc_url!r}: {exc}") from exc
    if not connected:
        raise ConfigError(f"RPC_URL {cfg.rpc_url!r} is not reachable")

    onchain_chain_id = w3.eth.chain_id
    if int(cfg.chain_id) != int(onchain_chain_id):
        raise EnvError(
            f"env CHAIN_ID={cfg.chain_id} does not match the RPC chain id "
            f"({onchain_chain_id}); check RPC_URL and CHAIN_ID"
        )

    from eth_account import Account as EthAccount

    account = EthAccount.from_key(cfg.private_key)

    escrow = w3.eth.contract(address=cfg.escrow_addr, abi=ESCROW_ABI)
    registry = w3.eth.contract(address=cfg.registry_addr, abi=REGISTRY_ABI)
    usdc = w3.eth.contract(address=cfg.usdc_addr, abi=ERC20_ABI)
    return ChainContext(cfg=cfg, w3=w3, account=account, escrow=escrow, registry=registry, usdc=usdc)


# ---------------------------------------------------------------------------
# low-level tx plumbing
# ---------------------------------------------------------------------------

_GAS_BUFFER = 1.25


def _send(ctx: ChainContext, fn_call: Any, confirm_wait: float | None = None) -> Any:
    """Sign, send and await a transaction; raises on revert."""
    return _submit_and_confirm(
        w3=ctx.w3,
        chain_id=int(ctx.cfg.chain_id),
        address=ctx.address,
        account=ctx.account,
        fn_call=fn_call,
        confirm_wait=confirm_wait,
    )


def _submit_and_confirm(
    *,
    w3: Web3,
    chain_id: int,
    address: str,
    account: LocalAccount,
    fn_call: Any,
    confirm_wait: float | None = None,
) -> Any:
    """Shared tx plumbing: nonce -> build -> gas estimate (+25% buffer) ->
    sign -> send -> receipt; raises TokenshareError on revert/failure.

    `_send` is the ChainContext (buyer-key) entry point; `remove_model` calls
    this directly because it signs with an operator key that is not part of
    any EnvConfig (M12).
    """
    if confirm_wait is None:
        try:
            confirm_wait = float(os.environ.get("TX_TIMEOUT_S", _TX_TIMEOUT_S_DEFAULT))
        except ValueError:
            confirm_wait = _TX_TIMEOUT_S_DEFAULT
    try:
        nonce = w3.eth.get_transaction_count(address)
        tx = fn_call.build_transaction(
            {
                "from": address,
                "nonce": nonce,
                "gasPrice": w3.eth.gas_price,
                "chainId": int(chain_id),
            }
        )
        est = w3.eth.estimate_gas(tx)
        tx["gas"] = max(int(est * _GAS_BUFFER), int(tx.get("gas", est * _GAS_BUFFER)))
        signed = account.sign_transaction(tx)
        raw = getattr(signed, "raw_transaction", None)
        if raw is None:  # eth_account < 0.13 attribute name
            raw = signed.rawTransaction
        tx_hash = w3.eth.send_raw_transaction(raw)
        receipt = w3.eth.wait_for_transaction_receipt(tx_hash, timeout=confirm_wait, poll_latency=1.0)
    except TokenshareError:
        raise
    except Exception as exc:
        # NOTE: exceptions here never contain the private key (it is not part
        # of the tx payload); surface them verbatim minus any secrets.
        raise TokenshareError(f"transaction failed: {_clean_exc(exc)}") from exc
    if receipt.get("status", 0) != 1:
        raise TokenshareError(
            f"transaction reverted on-chain: {receipt.get('transactionHash', b'').hex()}"
        )
    return receipt


def _clean_exc(exc: Exception) -> str:
    text = str(exc)
    for secret in ("private_key", "BUYER_PRIVATE_KEY"):
        if secret in text:
            return "transaction failed (details withheld)"
    return text


# ---------------------------------------------------------------------------
# views
# ---------------------------------------------------------------------------


def usdc_balance(ctx: ChainContext) -> int:
    return int(ctx.usdc.functions.balanceOf(ctx.address).call())


def escrow_balance(ctx: ChainContext) -> int:
    return int(ctx.escrow.functions.balances(ctx.address).call())


def escrow_next_payment_id(ctx: ChainContext) -> int:
    return int(ctx.escrow.functions.nextPaymentId().call())


def get_payment(ctx: ChainContext, payment_id: int) -> dict:
    buyer, seller, max_amount, expires_at, state = ctx.escrow.functions.getPayment(
        int(payment_id)
    ).call()
    return {
        "payment_id": int(payment_id),
        "buyer": buyer,
        "seller": seller,
        "max_amount": int(max_amount),
        "expires_at": int(expires_at),
        "state": int(state),
        "state_name": STATE_NAMES.get(int(state), str(state)),
    }


def get_listing(ctx: ChainContext, seller: str) -> dict:
    """Registry v2 getListing: (operator, endpoint, models, prices[], active).

    `prices` is parallel to `models` (M9 ABI PIN) — one Price
    {cached_in, input, output} per model entry, all USDC 6dp native.
    """
    (
        operator,
        endpoint,
        models,
        prices,
        active,
    ) = ctx.registry.functions.getListing(seller).call()
    return {
        "operator": operator,
        "endpoint": endpoint,
        "models": [str(m) for m in (models or [])],
        "prices": [_decode_price(p) for p in (prices or [])],
        "active": bool(active),
    }


def _decode_price(price: Any) -> dict:
    """(cachedIn, input, output) tuple -> {"cached_in", "input", "output"}."""
    cached_in, price_input, price_output = price
    return {
        "cached_in": int(cached_in),
        "input": int(price_input),
        "output": int(price_output),
    }


def get_price(ctx: ChainContext, seller: str, model: str) -> dict:
    """Registry v2 getPrice(operator, model) -> {"cached_in","input","output"}.

    Reverts on-chain when the listing is inactive (NotActive) or the model is
    unknown (ModelNotFound) — surfaced as TokenshareError by _send-free call()
    wrapping (web3 raises ContractLogicError).
    """
    return _decode_price(ctx.registry.functions.getPrice(seller, model).call())


# M10 Registry v3 on-chain enumeration (ABI PIN 「M10」): Uniswap-V2-Factory
# style O(1) discovery. The contract CLAMPS getSellers count to 500 per call
# (公开常量便于测) and truncates at the end of the list, so this helper clamps
# its page size too and advances by the RETURNED batch length — clamped
# responses may be shorter than the requested count.
GET_SELLERS_MAX_PAGE = 500


def get_sellers(w3: Web3, registry_addr: str, page: int = 100) -> list[str]:
    """All registered sellers via Registry v3 sellerCount()/getSellers()
    pagination — replaces the eth_getLogs Registered-event scan (and the
    LISTINGS_FROM_BLOCK env, removed as breaking in M10).

    Order is the on-chain append order (deactivate -> re-register never
    re-appends). Defensive guards: a sellerCount() of 0 short-circuits to []
    without calling getSellers; an empty batch breaks the loop (no hang on a
    misbehaving node); duplicates are removed preserving first-seen order.
    """
    registry = w3.eth.contract(address=registry_addr, abi=REGISTRY_ABI)
    page = int(page)
    if page < 1:
        raise TokenshareError(f"get_sellers page must be >= 1, got {page}")
    page = min(page, GET_SELLERS_MAX_PAGE)  # the contract clamps count to 500

    try:
        total = int(registry.functions.sellerCount().call())
    except TokenshareError:
        raise
    except Exception as exc:
        raise TokenshareError(
            f"Registry sellerCount() failed at {registry_addr}: {_clean_exc(exc)}"
        ) from exc

    sellers: list[str] = []
    seen: set[str] = set()
    start = 0
    while start < total:
        try:
            batch = registry.functions.getSellers(start, page).call()
        except TokenshareError:
            raise
        except Exception as exc:
            raise TokenshareError(
                f"Registry getSellers({start}, {page}) failed at {registry_addr}: "
                f"{_clean_exc(exc)}"
            ) from exc
        if not batch:  # edge/clamp defense: never loop forever
            break
        for addr in batch:
            normalized = to_checksum_address(str(addr))
            if normalized not in seen:
                seen.add(normalized)
                sellers.append(normalized)
        start += len(batch)  # advance by RETURNED length (clamp-safe)
    return sellers


def usdc_allowance(ctx: ChainContext) -> int:
    return int(ctx.usdc.functions.allowance(ctx.address, ctx.cfg.escrow_addr).call())


# ---------------------------------------------------------------------------
# operator-side mutations (M12: signed by the listing OPERATOR, not the buyer)
# ---------------------------------------------------------------------------


def remove_model(
    w3: Web3,
    registry_addr: str,
    sender_key: "str | LocalAccount",
    model: str,
    confirm_wait: float | None = None,
) -> dict:
    """Registry v4 (M12 ABI PIN) removeModel(model) — remove ONE model, and
    its parallel prices[] entry (same index, swap-and-pop), from the CALLER'S
    OWN listing. The caller must be the listing operator; inactive listings
    are allowed (no active requirement).

    On-chain guards: ModelNotFound(model) for an unlisted model (v2 error,
    reused); RemoveLastModel() when `model` is the last remaining one —
    deactivate() the whole listing instead. After removal
    getPrice(operator, model) reverts, so in-flight payments settle-fail and
    buyers refund after their TTL.

    Sender: a LocalAccount, or a raw private-key hex string (derived here,
    never logged). Gas estimation / signing / sending / receipt-status
    assertion go through the shared _submit_and_confirm plumbing (same 1.25x
    gas buffer + TX_TIMEOUT_S as every other mutation). The ModelRemoved
    event is decoded from the receipt; a missing event falls back to the
    caller-derived identity (defensive, mirrors lock/refund's lenient decode).
    """
    registry = w3.eth.contract(address=registry_addr, abi=REGISTRY_ABI)
    if isinstance(sender_key, str):
        from eth_account import Account as EthAccount

        account = EthAccount.from_key(sender_key)
    else:
        account = sender_key

    receipt = _submit_and_confirm(
        w3=w3,
        chain_id=w3.eth.chain_id,
        address=account.address,
        account=account,
        fn_call=registry.functions.removeModel(str(model)),
        confirm_wait=confirm_wait,
    )
    events = registry.events.ModelRemoved().process_receipt(receipt)
    args = events[0]["args"] if events else {}
    return {
        "operator": str(args.get("operator", account.address)),
        "model": str(args.get("model", model)),
        "tx_hash": receipt["transactionHash"].hex(),
    }


# ---------------------------------------------------------------------------
# mutations
# ---------------------------------------------------------------------------


def deposit(ctx: ChainContext, amount: int) -> dict:
    """USDC approve (when needed) + Escrow.deposit(amount)."""
    steps: list[str] = []
    allowance = usdc_allowance(ctx)
    if int(allowance) < int(amount):
        receipt = _send(ctx, ctx.usdc.functions.approve(ctx.cfg.escrow_addr, int(amount)))
        steps.append(f"approve tx {receipt['transactionHash'].hex()}")
    receipt = _send(ctx, ctx.escrow.functions.deposit(int(amount)))
    steps.append(f"deposit tx {receipt['transactionHash'].hex()}")
    return {"steps": steps, "amount": int(amount), "escrow_balance": escrow_balance(ctx)}


def lock(ctx: ChainContext, seller: str, max_amount: int, ttl: int) -> dict:
    """Escrow.lock(seller, maxAmount, ttl); decodes the Locked event."""
    if int(escrow_balance(ctx)) < int(max_amount):
        raise TokenshareError(
            f"Escrow balance {escrow_balance(ctx)} is below the lock maxAmount {max_amount}; "
            "run `deposit` first"
        )
    receipt = _send(ctx, ctx.escrow.functions.lock(seller, int(max_amount), int(ttl)))
    events = ctx.escrow.events.Locked().process_receipt(receipt)
    if not events:
        raise TokenshareError("Locked event missing from lock transaction receipt")
    ev = events[0]["args"]
    return {
        "payment_id": int(ev["paymentId"]),
        "buyer": ev["buyer"],
        "seller": ev["seller"],
        "max_amount": int(ev["maxAmount"]),
        "expires_at": int(ev["expiresAt"]),
        "tx_hash": receipt["transactionHash"].hex(),
    }


def refund(ctx: ChainContext, payment_id: int) -> dict:
    """Refund an expired lock (Escrow.refund)."""
    payment = get_payment(ctx, payment_id)
    if payment["state"] != 1:
        raise TokenshareError(
            f"payment {payment_id} is not Locked (state={payment['state_name']}); "
            "nothing to refund"
        )
    if payment["expires_at"] > _now():
        raise TokenshareError(
            f"TTL not elapsed for payment {payment_id}: refund becomes available at "
            f"unix {payment['expires_at']}"
        )
    receipt = _send(ctx, ctx.escrow.functions.refund(int(payment_id)))
    events = ctx.escrow.events.Refunded().process_receipt(receipt)
    amount = int(events[0]["args"]["amount"]) if events else None
    return {
        "payment_id": int(payment_id),
        "amount": amount,
        "tx_hash": receipt["transactionHash"].hex(),
        "escrow_balance": escrow_balance(ctx),
    }


def _now() -> int:
    import time

    return int(time.time())
