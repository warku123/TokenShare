"""EIP-712 receipt decoding and verification (relay -> CLI).

PIN (m3-m5-e2e.md 「接口契约 PIN」, must not drift):
    domain = {name: "TokenShare Relay", version: "1", chainId}
    Receipt(uint256 paymentId, uint256 promptTokens, uint256 cachedTokens,
            uint256 completionTokens, uint256 actualAmount, address seller)
    X-Receipt = base64url(JSON {domain, message, signature})
    Verification: recover_typed_data(...) == Registry listing operator (seller).

On failure the CLI prints a warning and records the paymentId as a dispute
(see disputes.py); it never crashes the already-served response.
"""

import base64
import binascii
import json
from dataclasses import dataclass

from eth_utils import to_checksum_address

from .errors import TokenshareError
from .signing import receipt_types, recover_typed_data

RECEIPT_DOMAIN_NAME = "TokenShare Relay"
RECEIPT_DOMAIN_VERSION = "1"


class ReceiptDecodeError(TokenshareError):
    """X-Receipt header is not valid base64url-JSON {domain,message,signature}."""


@dataclass(frozen=True)
class Receipt:
    domain: dict
    message: dict
    signature: str

    @property
    def payment_id(self) -> int:
        return int(self.message.get("paymentId", 0))

    @property
    def prompt_tokens(self) -> int:
        return int(self.message.get("promptTokens", 0))

    @property
    def cached_tokens(self) -> int:
        return int(self.message.get("cachedTokens", 0))

    @property
    def completion_tokens(self) -> int:
        return int(self.message.get("completionTokens", 0))

    @property
    def actual_amount(self) -> int:
        return int(self.message.get("actualAmount", 0))

    @property
    def seller(self) -> str:
        return str(self.message.get("seller", ""))


def decode_receipt(raw_b64: str) -> Receipt:
    """Decode the X-Receipt header value (base64url JSON).

    Accepts both padded and unpadded base64url (relay lanes differ on
    whether they append '=' padding; both round-trip identically).
    """
    if not raw_b64 or not raw_b64.strip():
        raise ReceiptDecodeError("X-Receipt header is empty")
    blob = raw_b64.strip().replace("\n", "").replace("\r", "")
    # tolerate missing padding
    padded = blob + "=" * (-len(blob) % 4)
    try:
        payload = base64.urlsafe_b64decode(padded.encode("ascii"))
    except (binascii.Error, ValueError) as exc:
        raise ReceiptDecodeError(f"X-Receipt is not valid base64url: {exc}") from exc
    try:
        obj = json.loads(payload.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise ReceiptDecodeError(f"X-Receipt is not valid JSON: {exc}") from exc
    if not isinstance(obj, dict):
        raise ReceiptDecodeError("X-Receipt JSON is not an object")
    for key in ("domain", "message", "signature"):
        if key not in obj:
            raise ReceiptDecodeError(f"X-Receipt JSON missing key {key!r}")
    return Receipt(domain=obj["domain"], message=obj["message"], signature=str(obj["signature"]))


def _same_address(a: str | None, b: str | None) -> bool:
    if not a or not b:
        return False
    try:
        return to_checksum_address(a) == to_checksum_address(b)
    except Exception:
        return False


@dataclass(frozen=True)
class ReceiptCheck:
    ok: bool
    recovered: str | None
    reason: str | None


def verify_receipt(
    receipt: Receipt,
    expected_seller: str,
    expected_payment_id: int | None = None,
) -> ReceiptCheck:
    """Verify the receipt signature against the Registry listing operator.

    Checks, in order:
      1. signature recovers to an address (recover over EIP-712);
      2. recovered address == expected seller (Registry listing operator);
      3. receipt.message.seller == expected seller;
      4. receipt.message.paymentId == the paymentId we used (optional).
    """
    try:
        recovered = recover_typed_data(
            _encode(receipt.domain, receipt.message), receipt.signature
        )
    except Exception as exc:
        return ReceiptCheck(False, None, f"recover-failed: {exc}")

    if not _same_address(recovered, expected_seller):
        return ReceiptCheck(False, recovered, "recover-mismatch")
    if not _same_address(receipt.message.get("seller"), expected_seller):
        return ReceiptCheck(False, recovered, "seller-mismatch")
    if expected_payment_id is not None and receipt.payment_id != expected_payment_id:
        return ReceiptCheck(False, recovered, "paymentid-mismatch")
    return ReceiptCheck(True, recovered, None)


def _encode(domain: dict, message: dict):
    from eth_account.messages import encode_typed_data

    types = receipt_types()
    return encode_typed_data(domain, types, message)
