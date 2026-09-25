"""web3.py wrappers for all on-chain interactions of the seller relay.

Loads the Foundry artifact ABIs directly from `contracts/out/` — no handwritten
ABI fragments, no hardcoded addresses or chainIds (BUILD_SPEC §2.5; every chain
parameter comes from env config).

Amounts are native USDC units (6 dp). The settle transaction is sent from the
RELAY_SELLER_KEY account (the seller designated in the payment).
"""

from __future__ import annotations

import json
import logging
import threading
from pathlib import Path
from typing import Any

from eth_account import Account
from eth_utils import keccak
from web3 import Web3
from web3.contract import Contract
from web3.exceptions import ContractLogicError
from web3.types import TxReceipt

from .config import ConfigError
from .pricing import Prices

_LOG = logging.getLogger(__name__)

# Foundry artifact locations relative to the repo root (relay/app/chain.py).
_REPO_ROOT: Path = Path(__file__).resolve().parents[2]
_ESCROW_ABI_PATH: Path = _REPO_ROOT / "contracts" / "out" / "Escrow.sol" / "Escrow.json"
_REGISTRY_ABI_PATH: Path = _REPO_ROOT / "contracts" / "out" / "Registry.sol" / "Registry.json"

ZERO_ADDRESS = "0x0000000000000000000000000000000000000000"


class NotActive(LookupError):
    """Registry.getPrice reverted NotActive — the operator's listing is not
    active (M9 Registry v2 error face)."""


class ModelNotFound(LookupError):
    """Registry.getPrice reverted ModelNotFound — the model is not in the
    operator's listing (M9 Registry v2 error face)."""


_PRICE_ERROR_FACES: tuple[tuple[str, type[LookupError]], ...] = (
    ("NotActive", NotActive),
    ("ModelNotFound", ModelNotFound),
)


def _map_price_revert(exc: Exception) -> LookupError | None:
    """Map a raw getPrice revert onto the contract's two error faces.

    web3 surfaces custom reverts either as decoded error names (ABI errors
    present) or as selector/text fragments inside ContractLogicError — match
    on the error NAME across message + data so both shapes map cleanly."""
    parts = (
        type(exc).__name__,
        str(exc),
        repr(getattr(exc, "data", None)),
    )
    text = " ".join(part for part in parts if part)
    for name, cls in _PRICE_ERROR_FACES:
        if name in text:
            return cls(text)
    return None


def _load_artifact(path: Path) -> dict[str, Any]:
    with open(path, "r", encoding="utf-8") as fh:
        artifact = json.load(fh)
    if "abi" not in artifact:
        raise RuntimeError(f"Foundry artifact {path} has no 'abi' field")
    return artifact


# ---------------------------------------------------------------------------
# Settle nonce serialization
#
# Settles run on asyncio.to_thread workers (relay/app/main.py), so two settle
# calls can race on the shared seller account: both fetch the same nonce, and
# the node rejects/replaces one tx — surfacing as a false settle-failed. A
# module-level lock serializes the fetch-nonce → build → sign → send →
# receipt-wait window. The receipt wait stays INSIDE the lock because
# eth_getTransactionCount is queried with the default 'latest' block: until
# the previous tx is mined, the next settle would read a stale nonce.
# Read-only paths (call/getPayment/...) are never taken under this lock.
# ---------------------------------------------------------------------------
_SETTLE_LOCK = threading.Lock()

# Text fragments identifying a same-nonce rejection across node clients and
# web3 versions (web3 v7 surfaces node errors as Web3RPCError; some versions
# raise TransactionAlready* exceptions client-side — matched by name below).
_NONCE_CONFLICT_MARKERS: tuple[str, ...] = (
    "nonce too low",
    "nonce has already been used",
    "already known",
    "replacement transaction underpriced",
    "transactionalready",  # TransactionAlreadySent/Pending/Replacement names
)


def _is_nonce_conflict(exc: BaseException) -> bool:
    """True when the exception is a same-nonce send rejection."""
    text = f"{type(exc).__name__} {exc}".lower()
    return any(marker in text for marker in _NONCE_CONFLICT_MARKERS)


# ---------------------------------------------------------------------------
# M13-D Escrow v2 partial settle (PIN-verbatim selector encoding)
#
# `settlePartial(uint256,uint256)` — Locked 态可多次调用，链上 captured 累计
# (≤maxAmount)，不改 payment 状态。The contracts lane lands in parallel, so
# the Foundry artifact may not yet carry settlePartial in its ABI: the
# calldata is therefore encoded straight from the PIN signature instead of
# `escrow.functions.settlePartial(...)` (byte-identical once the artifact
# refreshes; selector c97ac54c).
# ---------------------------------------------------------------------------
_SETTLE_PARTIAL_SIGNATURE = "settlePartial(uint256,uint256)"
# Hardcoded PIN selector, cross-checked against keccak at call time.
_SETTLE_PARTIAL_SELECTOR = "c97ac54c"

# SEC1-3: Escrow v2 captured getter (read-only eth_call, ABI-free for the
# same reason as settle_partial — the artifact may lag the deployed v2).
_CAPTURED_OF_SIGNATURE = "capturedOf(uint256)"
_CAPTURED_OF_SELECTOR = "d5d177a3"


def _check_selector(signature: str, expected: str) -> str:
    selector = keccak(signature.encode("ascii"))[:4].hex()
    if selector != expected:
        raise RuntimeError(
            f"selector drift: PIN says {expected}, keccak gives {selector} "
            f"for {signature}"
        )
    return selector


def _encode_settle_partial_calldata(payment_id: int, amount: int) -> str:
    """0x-prefixed calldata for settlePartial(uint256,uint256):
    4-byte selector + two 32-byte big-endian uint256 words."""
    selector = _check_selector(_SETTLE_PARTIAL_SIGNATURE, _SETTLE_PARTIAL_SELECTOR)
    return (
        "0x"
        + selector
        + payment_id.to_bytes(32, "big").hex()
        + amount.to_bytes(32, "big").hex()
    )


def _encode_captured_of_calldata(payment_id: int) -> str:
    """0x-prefixed calldata for capturedOf(uint256)."""
    selector = _check_selector(_CAPTURED_OF_SIGNATURE, _CAPTURED_OF_SELECTOR)
    return "0x" + selector + payment_id.to_bytes(32, "big").hex()


# ---------------------------------------------------------------------------
# Settle gas policy (fix-34): estimate-first, never a flat guess.
#
# Escrow v3 settle WITH the protocol fee measured 132,608 gas on-chain — the
# old flat 120_000 out-of-gas-reverted the tx; settlePartial (104,042) was
# borderline. Same philosophy as the e2e runner's gas_for: probe
# eth_estimateGas, then ×1.3 + 20k headroom. The flat fallback below is used
# ONLY when the estimate itself fails (RPC hiccup) — a genuine contract
# revert at estimate time is indistinguishable from an RPC hiccup there, so
# the fallback keeps ONE honest on-chain verdict via the receipt status check
# in _sign_send_wait.
# ---------------------------------------------------------------------------
_SETTLE_GAS_HEADROOM = 1.3
_SETTLE_GAS_BUFFER = 20_000
_SETTLE_GAS_FALLBACK = 300_000


class SettleError(RuntimeError):
    """A settle/settlePartial tx was MINED but reverted on-chain
    (receipt.status != 1 — out-of-gas or a contract revert such as
    OverMax/BelowCaptured). This is never a success: callers treat it like
    any settle failure (X-Settle-Status: settle-failed / partial-flush-failed)
    and must never swallow the LLM response."""


class ChainClient:
    """Reads Escrow/Registry state and sends settle transactions.

    Methods are synchronous (web3.py HTTP); async callers should run them via
    `asyncio.to_thread` to avoid blocking the event loop.
    """

    def __init__(
        self,
        *,
        rpc_url: str,
        escrow_addr: str,
        registry_addr: str,
        seller_key: str,
        chain_id: int,
    ) -> None:
        self._w3 = Web3(Web3.HTTPProvider(rpc_url))
        if not self._w3.is_connected():
            raise RuntimeError(f"RPC at {rpc_url!r} is not reachable")

        # Guard against a receipt-domain / settle-chain mismatch: the RPC node
        # must be the configured chain (receipt EIP-712 domain and settle tx
        # are only valid on the configured chainId).
        remote_chain_id = self._w3.eth.chain_id
        if remote_chain_id != chain_id:
            raise ConfigError(
                f"RPC reports chainId {remote_chain_id} but config says "
                f"{chain_id} — refusing to start (chain mismatch)"
            )

        self.escrow: Contract = self._w3.eth.contract(
            address=Web3.to_checksum_address(escrow_addr),
            abi=_load_artifact(_ESCROW_ABI_PATH)["abi"],
        )
        self.registry: Contract = self._w3.eth.contract(
            address=Web3.to_checksum_address(registry_addr),
            abi=_load_artifact(_REGISTRY_ABI_PATH)["abi"],
        )

        self._account = Account.from_key(seller_key)
        self.seller_address: str = self._account.address

    # ------------------------------------------------------------------ reads

    def is_valid(self, payment_id: int, seller: str, min_amount: int) -> bool:
        """Escrow.isValid(paymentId, seller, minAmount) — Locked, unexpired,
        designated seller, maxAmount >= minAmount."""
        return bool(
            self.escrow.functions.isValid(
                payment_id, Web3.to_checksum_address(seller), min_amount
            ).call()
        )

    def get_payment(self, payment_id: int) -> dict[str, Any]:
        """Escrow.getPayment(paymentId) → parsed record. State enum:
        0 None (id never issued), 1 Locked, 2 Settled, 3 Refunded."""
        buyer, seller, max_amount, expires_at, state = self.escrow.functions.getPayment(
            payment_id
        ).call()
        return {
            "buyer": buyer,
            "seller": seller,
            "maxAmount": int(max_amount),
            "expiresAt": int(expires_at),
            "state": int(state),
        }

    def captured_of(self, payment_id: int) -> int:
        """Escrow v2 capturedOf(paymentId) → the on-chain captured total
        (SEC1-3). ABI-free eth_call via the PIN selector (same rationale as
        settle_partial — the Foundry artifact may lag the deployed contract).
        Read-only: never taken under _SETTLE_LOCK. Raises on revert — callers
        (relay main) degrade conservatively to 0; an empty return decodes 0."""
        data = _encode_captured_of_calldata(payment_id)
        result = self._w3.eth.call({"to": self.escrow.address, "data": data})
        return int.from_bytes(result, "big") if result else 0

    def get_listing(self, operator: str) -> dict[str, Any] | None:
        """Registry.getListing(operator) → parsed listing, or None when the
        operator never registered (listingOperator == address(0)).
        Callers must still check `active` themselves.

        M9 Registry v2 structure (5 fields, operator still first):
        (address operator, string endpoint, string[] models,
         Price[] prices /* parallel to models */, bool active)."""
        (
            listing_operator,
            endpoint,
            models,
            prices,
            active,
        ) = self.registry.functions.getListing(Web3.to_checksum_address(operator)).call()

        if str(listing_operator) == ZERO_ADDRESS:
            return None

        return {
            "operator": listing_operator,
            "endpoint": endpoint,
            "models": [str(m) for m in models],
            # Parallel to `models` (same length): per-model Price triple,
            # USDC 6dp native units per 1M tokens.
            "prices": [
                {
                    "cachedIn": int(price[0]),
                    "input": int(price[1]),
                    "output": int(price[2]),
                }
                for price in prices
            ],
            "active": bool(active),
        }

    def get_price(self, operator: str, model: str) -> Prices:
        """Registry.getPrice(operator, model) → the per-model Price triple
        (M9 Registry v2): settle pricing and the minAmount estimate for a
        request MUST use the prices of the REQUESTED model.

        Reverts are mapped: NotActive → chain.NotActive, ModelNotFound →
        chain.ModelNotFound; any other revert propagates untouched."""
        try:
            cached_in, price_input, price_output = self.registry.functions.getPrice(
                Web3.to_checksum_address(operator), model
            ).call()
        except ContractLogicError as exc:
            mapped = _map_price_revert(exc)
            if mapped is not None:
                raise mapped from exc
            raise
        return Prices(
            price_cached_in=int(cached_in),
            price_input=int(price_input),
            price_output=int(price_output),
        )

    # ------------------------------------------------------------------ write

    def _estimate_settle_gas(self, tx: dict[str, Any]) -> int:
        """Estimate-first gas (gas_for philosophy): probe the tx without its
        'gas' field via eth_estimateGas, then ×1.3 + 20k headroom. On ANY
        estimate failure fall back to _SETTLE_GAS_FALLBACK with a WARNING —
        never a silent flat guess below the real cost (Escrow v3 settle with
        the protocol fee needs 132,608 gas; the old flat 120_000
        out-of-gas-reverted on-chain)."""
        probe = {key: value for key, value in tx.items() if key != "gas"}
        try:
            raw = int(self._w3.eth.estimate_gas(probe))
        except Exception as exc:  # noqa: BLE001 — any RPC/estimate failure
            _LOG.warning(
                "settle gas estimate failed (%s); falling back to %s gas",
                exc,
                _SETTLE_GAS_FALLBACK,
            )
            return _SETTLE_GAS_FALLBACK
        return int(raw * _SETTLE_GAS_HEADROOM) + _SETTLE_GAS_BUFFER

    def _build_sign_send_settle(self, payment_id: int, actual_amount: int) -> TxReceipt:
        """One fetch-nonce → build → sign → send → receipt-wait pass. Caller
        must hold _SETTLE_LOCK (see settle)."""
        chain_id = self._w3.eth.chain_id
        # "gas" present → build_transaction skips its own eth_estimateGas;
        # the estimate-first policy below fills the real value instead. The
        # placeholder gasPrice keeps build_transaction off the dynamic-fee
        # RPC probes (eth_maxPriorityFeePerGas/eth_feeHistory) — the REAL
        # fee fields are set afterwards in _sign_send_wait.
        tx: dict[str, Any] = self.escrow.functions.settle(
            payment_id, actual_amount
        ).build_transaction(
            {
                "from": self._account.address,
                "nonce": self._w3.eth.get_transaction_count(self._account.address),
                "chainId": chain_id,
                "gas": _SETTLE_GAS_FALLBACK,
                "gasPrice": 1_000_000_000,
            }
        )
        tx["gas"] = self._estimate_settle_gas(tx)
        return self._sign_send_wait(tx)

    def _sign_send_wait(self, tx: dict[str, Any]) -> TxReceipt:
        """Fee fields → sign → send → receipt-wait tail shared by settle and
        settle_partial. Caller must hold _SETTLE_LOCK (see settle)."""
        # EIP-1559 fee fields; fall back to legacy pricing for non-1559 nodes.
        latest = self._w3.eth.get_block("latest")
        if latest.get("baseFeePerGas") is not None:
            base = int(latest["baseFeePerGas"])
            tx["maxFeePerGas"] = base * 2 + 1_000_000_000
            tx["maxPriorityFeePerGas"] = 1_000_000_000
            tx.pop("gasPrice", None)
        else:
            tx["gasPrice"] = self._w3.eth.gas_price

        signed = self._account.sign_transaction(tx)
        raw = getattr(signed, "raw_transaction", None) or signed.rawTransaction
        tx_hash = self._w3.eth.send_raw_transaction(raw)
        receipt = self._w3.eth.wait_for_transaction_receipt(tx_hash)
        # A MINED tx can still have reverted on-chain (out-of-gas / contract
        # revert → status 0). Without this check the relay reported
        # X-Settle-Status: settled for on-chain failures (fix-34 e2e: the
        # buyer saw a fake success while Escrow never settled). Missing
        # status (pre-Byzantium chains) counts as success.
        status = int(receipt.get("status", 1))
        if status != 1:
            raise SettleError(
                f"settle tx {tx_hash.hex()} mined but reverted on-chain "
                f"(receipt.status={status}, gasUsed={int(receipt.get('gasUsed', 0))})"
            )
        return receipt

    def _send_escrow_tx(self, data: str) -> TxReceipt:
        """Escrow write with PRE-ENCODED calldata (M13 settlePartial — the
        artifact ABI may not carry the function yet). Same envelope as
        _build_sign_send_settle: seller `from`, estimate-first gas (see
        _estimate_settle_gas), configured chainId. Caller must hold
        _SETTLE_LOCK."""
        tx: dict[str, Any] = {
            "to": self.escrow.address,
            "from": self._account.address,
            "nonce": self._w3.eth.get_transaction_count(self._account.address),
            "chainId": self._w3.eth.chain_id,
            "gas": _SETTLE_GAS_FALLBACK,
            "data": data,
        }
        tx["gas"] = self._estimate_settle_gas(tx)
        return self._sign_send_wait(tx)

    def settle(self, payment_id: int, actual_amount: int) -> TxReceipt:
        """Send Escrow.settle(paymentId, actual) from the seller account and
        wait for the receipt. Raises on revert/send failure — callers treat
        ANY exception as settle-failed (never swallow the LLM response).

        Concurrent settles share the seller nonce: the module-level lock
        serializes the fetch-nonce → build → sign → send → receipt window so
        two settles can never grab the same nonce (concurrency fix C1). A
        node-side same-nonce rejection that still slips through (e.g. an
        out-of-band sender on the same account) triggers exactly ONE
        refetch-nonce → re-sign → resend attempt before failing.

        Gas is estimate-first (see _estimate_settle_gas) and a MINED-but-
        reverted receipt raises SettleError (see _sign_send_wait) — either
        way callers see settle-failed, never a fake success."""
        with _SETTLE_LOCK:
            try:
                return self._build_sign_send_settle(payment_id, actual_amount)
            except Exception as exc:
                if not _is_nonce_conflict(exc):
                    raise
            # Nonce conflicted anyway: retry once with a freshly fetched nonce.
            return self._build_sign_send_settle(payment_id, actual_amount)

    def settle_partial(self, payment_id: int, amount: int) -> TxReceipt:
        """Send Escrow v2 settlePartial(paymentId, amount) from the seller
        account and wait for the receipt (M13-D). Same failure contract as
        settle: raises on revert/send failure — callers treat ANY exception as
        partial-flush-failed (never swallow the LLM response). Shares the
        module-level nonce lock and the EIP-1559 build with settle; exactly
        ONE refetch-nonce retry on a same-nonce rejection.

        Calldata is PIN-selector-encoded (see _encode_settle_partial_calldata)
        so this works before/after the contracts lane refreshes the artifact.
        On-chain the call accumulates captured (≤maxAmount) without changing
        the payment state; a revert (e.g. OverMax after a relay restart lost
        the local ledger) surfaces as a normal exception to the caller — for
        a MINED-but-reverted receipt specifically as SettleError (same
        failure contract as settle)."""
        data = _encode_settle_partial_calldata(payment_id, amount)
        with _SETTLE_LOCK:
            try:
                return self._send_escrow_tx(data)
            except Exception as exc:
                if not _is_nonce_conflict(exc):
                    raise
            # Nonce conflicted anyway: retry once with a freshly fetched nonce.
            return self._send_escrow_tx(data)
