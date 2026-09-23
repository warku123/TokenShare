"""Request signing (CLI -> relay), exactly per the interface-contract PIN.

PIN (m3-m5-e2e.md 「接口契约 PIN」, must not drift):
    msg = f"{METHOD}|{path}|{sha256(raw_body_bytes).hexdigest()}|{paymentId}"
    METHOD uppercase, path = /v1/chat/completions, hash is 64 lowercase hex
    with no 0x prefix, paymentId is a decimal string (no leading zeros).
    Sign with eth_account.encode_defunct(text=msg); send as
    X-Payment-Id (decimal) + X-Signature (0x + 65-byte hex).
"""

import hashlib
from typing import Any

from eth_account import Account
from eth_account.messages import SignableMessage, encode_defunct
from eth_account.messages import encode_typed_data  # noqa: F401  (re-exported for tests)
from hexbytes import HexBytes

RELAY_CHAT_PATH = "/v1/chat/completions"


def build_eip191_message(method: str, path: str, raw_body: bytes, payment_id: int) -> str:
    """Construct the exact PIN message string (byte-for-byte identical to relay)."""
    body_hash = hashlib.sha256(raw_body).hexdigest()  # 64 lowercase hex, no 0x
    payment_decimal = str(int(payment_id))  # decimal string, no leading zeros
    return f"{method.upper()}|{path}|{body_hash}|{payment_decimal}"


def sign_request(private_key: str, method: str, path: str, raw_body: bytes, payment_id: int) -> str:
    """Sign per PIN; returns the 0x-prefixed 65-byte hex signature (130 chars)."""
    message = build_eip191_message(method, path, raw_body, payment_id)
    account = Account.from_key(private_key)
    signable = encode_defunct(text=message)
    signature = account.sign_message(signable)
    sig = signature["signature"] if isinstance(signature, dict) else signature.signature
    if len(HexBytes(sig)) != 65:
        raise ValueError("EIP-191 signature is not 65 bytes")
    return _hex_str(sig)


def to_hex_str(value: Any) -> str:
    """0x-prefixed lowercase hex for bytes-like values."""
    raw = HexBytes(value)
    return "0x" + bytes(raw).hex()


_hex_str = to_hex_str


def recover_request_signer(method: str, path: str, raw_body: bytes, payment_id: int, signature: str) -> str:
    """Recover the signer of a payment-request signature (used by tests and
    available for debugging interop with the relay)."""
    message = build_eip191_message(method, path, raw_body, payment_id)
    return Account.recover_message(encode_defunct(text=message), signature=signature)


# ---------------------------------------------------------------------------
# EIP-712 (relay -> CLI receipts). eth_account renamed the public entry point
# across versions; support both so the exact PIN wording (`recover_typed_data`)
# keeps working on any eth_account the project pins.
# ---------------------------------------------------------------------------


def encode_typed_receipt(domain: dict, message: dict) -> SignableMessage:
    """Build the EIP-712 signable for a receipt (same bytes as the relay)."""
    types = receipt_types()
    return encode_typed_data(domain, types, message)


def recover_typed_data(signable: SignableMessage, signature: str) -> str:
    """Recover the signer address from an EIP-712 typed-data signature.

    Uses `Account.recover_message` on eth_account >= 0.13 (current pin) and
    falls back to the historical `eth_account.messages.recover_typed_data`
    for older versions.
    """
    try:
        return Account.recover_message(signable, signature=signature)
    except TypeError:
        from eth_account.messages import recover_typed_data as legacy_recover

        return legacy_recover(signable, signature=signature)


def receipt_types() -> dict:
    """Receipt type declaration, verbatim from the PIN."""
    return {
        "Receipt": [
            {"name": "paymentId", "type": "uint256"},
            {"name": "promptTokens", "type": "uint256"},
            {"name": "cachedTokens", "type": "uint256"},
            {"name": "completionTokens", "type": "uint256"},
            {"name": "actualAmount", "type": "uint256"},
            {"name": "seller", "type": "address"},
        ]
    }
