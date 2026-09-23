"""On-chain access layer (web3). All values come from EnvConfig — the code
contains zero hardcoded addresses, chain ids, or RPC endpoints.

Every public function opens its own chain handle from a config, which keeps
the Typer commands thin and the tests patchable without a running node.
"""

from dataclasses import dataclass
import os
from typing import Any

from eth_account.signers.local import LocalAccount
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
    if confirm_wait is None:
        try:
            confirm_wait = float(os.environ.get("TX_TIMEOUT_S", _TX_TIMEOUT_S_DEFAULT))
        except ValueError:
            confirm_wait = _TX_TIMEOUT_S_DEFAULT
    w3 = ctx.w3
    address = ctx.address
    try:
        nonce = w3.eth.get_transaction_count(address)
        tx = fn_call.build_transaction(
            {
                "from": address,
                "nonce": nonce,
                "gasPrice": w3.eth.gas_price,
                "chainId": int(ctx.cfg.chain_id),
            }
        )
        est = w3.eth.estimate_gas(tx)
        tx["gas"] = max(int(est * _GAS_BUFFER), int(tx.get("gas", est * _GAS_BUFFER)))
        signed = ctx.account.sign_transaction(tx)
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
    (
        operator,
        endpoint,
        models,
        price_cached_in,
        price_input,
        price_output,
        active,
    ) = ctx.registry.functions.getListing(seller).call()
    return {
        "operator": operator,
        "endpoint": endpoint,
        "models": list(models),
        "price_cached_in": int(price_cached_in),
        "price_input": int(price_input),
        "price_output": int(price_output),
        "active": bool(active),
    }


# Registry.Registered event — canonical Solidity signature string (topic0 =
# keccak256 of it). Must stay in sync with contracts/src/Registry.sol EVENTS;
# tests/test_listings.py cross-checks the two against each other.
REGISTERED_EVENT_SIG = "Registered(address,string,string[],uint256,uint256,uint256)"


def registered_operators(ctx: ChainContext, from_block: int) -> list[str]:
    """Operators that ever emitted Registry.Registered — event-order dedup.

    The Registry has NO on-chain enumeration (only getListing(address)), so
    eth_getLogs over the Registered event is the discovery mechanism for
    `listings`. Re-registers emit the event again — duplicates removed here;
    each operator's CURRENT state is read separately via getListing (the
    latest registration overwrites the stored struct on-chain).

    from_block: 0 = whole chain; callers pass LISTINGS_FROM_BLOCK so users
    can skip a slow full-chain scan on long chains.
    """
    from eth_utils import keccak

    topic0 = "0x" + keccak(text=REGISTERED_EVENT_SIG).hex()
    try:
        logs = ctx.w3.eth.get_logs(
            {
                "fromBlock": int(from_block),
                "toBlock": "latest",
                "address": ctx.registry.address,
                "topics": [topic0],
            }
        )
        decoded = [ctx.registry.events.Registered().process_log(log) for log in logs]
    except TokenshareError:
        raise
    except Exception as exc:
        raise TokenshareError(
            f"cannot scan Registered events (eth_getLogs from block {from_block}): "
            f"{_clean_exc(exc)}"
        ) from exc
    operators: list[str] = []
    seen: set[str] = set()
    for event in decoded:
        operator = str(event["args"]["operator"])
        if operator not in seen:
            seen.add(operator)
            operators.append(operator)
    return operators


def usdc_allowance(ctx: ChainContext) -> int:
    return int(ctx.usdc.functions.allowance(ctx.address, ctx.cfg.escrow_addr).call())


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
