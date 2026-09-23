"""web3.py wrappers for all on-chain interactions of the seller relay.

Loads the Foundry artifact ABIs directly from `contracts/out/` — no handwritten
ABI fragments, no hardcoded addresses or chainIds (BUILD_SPEC §2.5; every chain
parameter comes from env config).

Amounts are native USDC units (6 dp). The settle transaction is sent from the
RELAY_SELLER_KEY account (the seller designated in the payment).
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from eth_account import Account
from web3 import Web3
from web3.contract import Contract
from web3.types import TxReceipt

from .config import ConfigError

# Foundry artifact locations relative to the repo root (relay/app/chain.py).
_REPO_ROOT: Path = Path(__file__).resolve().parents[2]
_ESCROW_ABI_PATH: Path = _REPO_ROOT / "contracts" / "out" / "Escrow.sol" / "Escrow.json"
_REGISTRY_ABI_PATH: Path = _REPO_ROOT / "contracts" / "out" / "Registry.sol" / "Registry.json"

ZERO_ADDRESS = "0x0000000000000000000000000000000000000000"


def _load_artifact(path: Path) -> dict[str, Any]:
    with open(path, "r", encoding="utf-8") as fh:
        artifact = json.load(fh)
    if "abi" not in artifact:
        raise RuntimeError(f"Foundry artifact {path} has no 'abi' field")
    return artifact


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

    def get_listing(self, operator: str) -> dict[str, Any] | None:
        """Registry.getListing(operator) → parsed listing, or None when the
        operator never registered (listingOperator == address(0)).
        Callers must still check `active` themselves."""
        (
            listing_operator,
            endpoint,
            models,
            price_cached_in,
            price_input,
            price_output,
            active,
        ) = self.registry.functions.getListing(Web3.to_checksum_address(operator)).call()

        if str(listing_operator) == ZERO_ADDRESS:
            return None

        return {
            "operator": listing_operator,
            "endpoint": endpoint,
            "models": [str(m) for m in models],
            "priceCachedIn": int(price_cached_in),
            "priceInput": int(price_input),
            "priceOutput": int(price_output),
            "active": bool(active),
        }

    # ------------------------------------------------------------------ write

    def settle(self, payment_id: int, actual_amount: int) -> TxReceipt:
        """Send Escrow.settle(paymentId, actual) from the seller account and
        wait for the receipt. Raises on revert/send failure — callers treat
        ANY exception as settle-failed (never swallow the LLM response)."""
        chain_id = self._w3.eth.chain_id
        tx: dict[str, Any] = self.escrow.functions.settle(
            payment_id, actual_amount
        ).build_transaction(
            {
                "from": self._account.address,
                "nonce": self._w3.eth.get_transaction_count(self._account.address),
                "chainId": chain_id,
                "gas": 120_000,
            }
        )
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
        return self._w3.eth.wait_for_transaction_receipt(tx_hash)
