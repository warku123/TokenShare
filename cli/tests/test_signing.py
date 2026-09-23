"""PIN conformance tests: EIP-191 request signing (CLI -> relay).

PIN: msg = f"{METHOD}|{path}|{sha256(raw_body_bytes).hexdigest()}|{paymentId}"
     METHOD uppercase, path=/v1/chat/completions, hash 64 lowercase hex with
     no 0x, paymentId decimal string; encode_defunct(text=msg); X-Signature
     is 0x + 65-byte hex.
"""

import hashlib

from eth_account import Account
from eth_account.messages import encode_defunct

from tokenshare_cli.signing import (
    RELAY_CHAT_PATH,
    build_eip191_message,
    recover_request_signer,
    sign_request,
)
from tests.conftest import BUYER_KEY, addr_of

BODY = b'{"model":"gpt-4o-mini","messages":[{"role":"user","content":"hi"}],"stream":false}'


def _sign(payment_id: int) -> str:
    return sign_request(BUYER_KEY, "POST", RELAY_CHAT_PATH, BODY, payment_id)


def test_message_format_matches_pin_exactly():
    body_hash = hashlib.sha256(BODY).hexdigest()
    msg = build_eip191_message("POST", RELAY_CHAT_PATH, BODY, 42)
    assert msg == f"POST|/v1/chat/completions|{body_hash}|42"
    parts = msg.split("|")
    assert len(parts) == 4
    assert len(parts[2]) == 64          # 64 hex chars
    assert parts[2] == parts[2].lower()  # lowercase
    assert not parts[2].startswith("0x")  # no 0x prefix


def test_method_is_uppercased():
    msg = build_eip191_message("post", "/v1/chat/completions", b"{}", 1)
    assert msg.startswith("POST|")


def test_payment_id_is_plain_decimal_no_leading_zeros():
    assert build_eip191_message("GET", "/p", b"x", 7).endswith("|7")
    assert build_eip191_message("GET", "/p", b"x", 0).endswith("|0")
    assert build_eip191_message("GET", "/p", b"x", 123).endswith("|123")


def test_sign_and_recover_returns_buyer_address():
    payment_id = 17
    sig = _sign(payment_id)
    recovered = recover_request_signer("POST", RELAY_CHAT_PATH, BODY, payment_id, sig)
    assert recovered == addr_of(BUYER_KEY)


def test_signature_shape_0x_65_bytes():
    sig = _sign(1)
    assert sig.startswith("0x")
    assert len(sig) == 2 + 130  # 65 bytes hex-encoded
    assert int(sig, 16) > 0


def test_recovery_roundtrip_via_encode_defunct():
    """Independent recovery path: encode_defunct(text=msg) -> recover."""
    msg = build_eip191_message("POST", "/v1/chat/completions", BODY, 99)
    sig = _sign(99)
    recovered = Account.recover_message(encode_defunct(text=msg), signature=sig)
    assert recovered == addr_of(BUYER_KEY)


def test_body_hash_differs_for_different_bodies():
    a = build_eip191_message("POST", "/v1/chat/completions", b"{}", 1)
    b = build_eip191_message("POST", "/v1/chat/completions", b"[]", 1)
    assert a != b
    assert a.split("|")[2] == hashlib.sha256(b"{}").hexdigest()
