"""EIP-712 signed receipts issued by the seller relay.

PIN contract (verbatim structure):
    domain  = {name: "TokenShare Relay", version: "1", chainId}
    Receipt = (uint256 paymentId, uint256 promptTokens, uint256 cachedTokens,
               uint256 completionTokens, uint256 actualAmount, address seller,
               string upstreamHost, string model)
    X-Receipt = base64url(JSON {domain, message, signature})

`upstreamHost` (e.g. "api.moonshot.cn") and `model` (the actually served
model name) make every receipt independently auditable against the
official-endpoint policy: the buyer can see WHICH official host and WHICH
model served the call.

GET /receipt/{paymentId} returns the same structure for the most recent
receipt of that payment (kept in an in-memory store).
"""

from __future__ import annotations

import base64
import json
import threading
from typing import Any, Final

from eth_account import Account
from eth_account.messages import encode_typed_data
from eth_utils import to_checksum_address

DOMAIN_NAME: Final[str] = "TokenShare Relay"
DOMAIN_VERSION: Final[str] = "1"

RECEIPT_TYPES: Final[dict[str, Any]] = {
    "EIP712Domain": [
        {"name": "name", "type": "string"},
        {"name": "version", "type": "string"},
        {"name": "chainId", "type": "uint256"},
    ],
    "Receipt": [
        {"name": "paymentId", "type": "uint256"},
        {"name": "promptTokens", "type": "uint256"},
        {"name": "cachedTokens", "type": "uint256"},
        {"name": "completionTokens", "type": "uint256"},
        {"name": "actualAmount", "type": "uint256"},
        {"name": "seller", "type": "address"},
        {"name": "upstreamHost", "type": "string"},
        {"name": "model", "type": "string"},
    ],
}


class ReceiptStore:
    """Thread-safe in-memory store of the latest receipt per paymentId."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._latest: dict[int, dict[str, Any]] = {}

    def put(self, payment_id: int, receipt: dict[str, Any]) -> None:
        with self._lock:
            self._latest[payment_id] = receipt

    def get(self, payment_id: int) -> dict[str, Any] | None:
        with self._lock:
            return self._latest.get(payment_id)


def build_receipt(
    *,
    chain_id: int,
    payment_id: int,
    prompt_tokens: int,
    cached_tokens: int,
    completion_tokens: int,
    actual_amount: int,
    seller: str,
    seller_key: str,
    upstream_host: str,
    model: str,
) -> dict[str, Any]:
    """Sign the receipt with the seller key and return the PIN JSON structure
    {domain, message, signature} (all values are plain JSON types)."""
    domain = {"name": DOMAIN_NAME, "version": DOMAIN_VERSION, "chainId": chain_id}
    message = {
        "paymentId": payment_id,
        "promptTokens": prompt_tokens,
        "cachedTokens": cached_tokens,
        "completionTokens": completion_tokens,
        "actualAmount": actual_amount,
        "seller": to_checksum_address(seller),
        "upstreamHost": upstream_host,
        "model": model,
    }

    encoded = encode_typed_data(
        full_message={
            "types": RECEIPT_TYPES,
            "primaryType": "Receipt",
            "domain": domain,
            "message": message,
        }
    )
    account = Account.from_key(seller_key)
    signed = account.sign_message(encoded)
    signature = signed.signature.hex()
    if not signature.startswith("0x"):
        signature = "0x" + signature

    return {"domain": domain, "message": message, "signature": signature}


def encode_x_receipt(receipt: dict[str, Any]) -> str:
    """X-Receipt header value: base64url(JSON {domain, message, signature})."""
    raw = json.dumps(receipt, separators=(",", ":"), sort_keys=False).encode("utf-8")
    return base64.urlsafe_b64encode(raw).decode("ascii").rstrip("=")


def decode_x_receipt(value: str) -> dict[str, Any]:
    """Inverse of encode_x_receipt (accepts stripped or padded base64url)."""
    padding = "=" * (-len(value) % 4)
    raw = base64.urlsafe_b64decode(value + padding)
    return json.loads(raw.decode("utf-8"))
