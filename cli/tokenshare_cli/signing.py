"""Request signing (CLI -> relay), exactly per the interface-contract PIN.

PIN (m3-m5-e2e.md 「接口契约 PIN」, must not drift):
    msg = f"{METHOD}|{path}|{sha256(raw_body_bytes).hexdigest()}|{paymentId}"
    METHOD uppercase, path = /v1/chat/completions, hash is 64 lowercase hex
    with no 0x prefix, paymentId is a decimal string (no leading zeros).
    Sign with eth_account.encode_defunct(text=msg); send as
    X-Payment-Id (decimal) + X-Signature (0x + 65-byte hex).
"""

import base64
import binascii
import hashlib
import json
from typing import Any

from eth_account import Account
from eth_account.messages import SignableMessage, encode_defunct
from eth_account.messages import encode_typed_data  # noqa: F401  (re-exported for tests)
from hexbytes import HexBytes

RELAY_CHAT_PATH = "/v1/chat/completions"

# M13 buyer API key (PIN 「M13」 mint message C + key encoding B):
#   message = "TokenShare API key grant|paymentId={p}|expiry={e}|maxAmount={m}"
#   key     = "tsk1.<b64url(payload_json)>.<b64url(sig_65B_hex)>"
#   payload = {"p": paymentId, "e": expiry_unix, "m": maxAmount, "b": buyer}
# The signature is EIP-191 (encode_defunct text) — the relay recovers the
# signer per request and accepts only when it equals payment.buyer. The sig
# segment encodes the 130-char lowercase hex WITHOUT the 0x prefix (65 bytes).
MINT_MESSAGE_PREFIX = "TokenShare API key grant"
API_KEY_PREFIX = "tsk1"


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
# M13 buyer API key minting (stateless signed key, PIN 「M13」 B/C)
# ---------------------------------------------------------------------------


def build_mint_message(payment_id: int, expiry: int, max_amount: int) -> str:
    """Mint message, verbatim per PIN (four-segment '|' style, like request
    signing): expiry/maxAmount are plain decimal integers, paymentId decimal
    without leading zeros."""
    return (
        f"{MINT_MESSAGE_PREFIX}|paymentId={int(payment_id)}"
        f"|expiry={int(expiry)}|maxAmount={int(max_amount)}"
    )


def _b64url(raw: bytes) -> str:
    """Unpadded base64url (matches the relay's X-Receipt emit style)."""
    return base64.urlsafe_b64encode(raw).decode("ascii").rstrip("=")


def _b64url_decode(segment: str) -> bytes:
    """Padded/unpadded tolerant base64url decode (relay emits unpadded)."""
    padded = segment + "=" * (-len(segment) % 4)
    return base64.urlsafe_b64decode(padded.encode("ascii"))


def mint_api_key(private_key: str, payment_id: int, expiry: int, max_amount: int,
                 buyer: str) -> str:
    """Sign the mint message with the buyer key and assemble the tsk1 key.

    The signature segment is the base64url of the 130-char lowercase hex
    (65 bytes, no 0x prefix). Returns ONLY the key string — callers print it;
    the private key itself never appears in any output.
    """
    message = build_mint_message(payment_id, expiry, max_amount)
    account = Account.from_key(private_key)
    signed = account.sign_message(encode_defunct(text=message))
    sig = signed["signature"] if isinstance(signed, dict) else signed.signature
    sig_bytes = bytes(HexBytes(sig))
    if len(sig_bytes) != 65:
        raise ValueError("mint signature is not 65 bytes")
    sig_hex = sig_bytes.hex()  # 130 chars, no 0x
    payload = {
        "p": int(payment_id),
        "e": int(expiry),
        "m": int(max_amount),
        "b": buyer,
    }
    payload_json = json.dumps(payload, separators=(",", ":")).encode("utf-8")
    return f"{API_KEY_PREFIX}.{_b64url(payload_json)}.{_b64url(sig_hex.encode('ascii'))}"


def decode_api_key(api_key: str) -> dict[str, Any]:
    """Split + decode a tsk1 key into {payload, signature_hex} (testing /
    debugging aid — mirrors the relay's three-layer decode)."""
    parts = api_key.split(".")
    if len(parts) != 3 or parts[0] != API_KEY_PREFIX:
        raise ValueError("malformed tsk1 API key")
    try:
        payload = json.loads(_b64url_decode(parts[1]))
        signature_hex = _b64url_decode(parts[2]).decode("ascii")
    except (binascii.Error, UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise ValueError(f"malformed tsk1 API key: {exc}") from exc
    return {"payload": payload, "signature_hex": signature_hex}


def recover_api_key_signer(api_key: str) -> tuple[dict[str, Any], str]:
    """Decode a tsk1 key and recover the EIP-191 signer of its mint message.
    Returns (payload, recovered_address)."""
    decoded = decode_api_key(api_key)
    payload = decoded["payload"]
    message = build_mint_message(payload["p"], payload["e"], payload["m"])
    recovered = Account.recover_message(
        encode_defunct(text=message), signature="0x" + decoded["signature_hex"]
    )
    return payload, recovered


# ---------------------------------------------------------------------------
# EIP-712 (relay -> CLI receipts). eth_account renamed the public entry point
# across versions; `recover_typed_data` is the PIN wording. Current eth_account
# exposes this via Account.recover_message; the legacy
# eth_account.messages.recover_typed_data fallback was removed as dead code
# with an incorrect API usage (m3).
# ---------------------------------------------------------------------------


def encode_typed_receipt(domain: dict, message: dict) -> SignableMessage:
    """Build the EIP-712 signable for a receipt (same bytes as the relay)."""
    types = receipt_types()
    return encode_typed_data(domain, types, message)


def recover_typed_data(signable: SignableMessage, signature: str) -> str:
    """Recover the signer address from an EIP-712 typed-data signature."""
    return Account.recover_message(signable, signature=signature)


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
            {"name": "upstreamHost", "type": "string"},
            {"name": "model", "type": "string"},
        ]
    }
