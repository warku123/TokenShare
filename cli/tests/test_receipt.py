"""EIP-712 receipt verification tests (relay -> CLI).

PIN: domain {name:"TokenShare Relay", version:"1", chainId};
     Receipt(uint256 paymentId, uint256 promptTokens, uint256 cachedTokens,
             uint256 completionTokens, uint256 actualAmount, address seller);
     X-Receipt = base64url(JSON {domain,message,signature});
     recover_typed_data(...) must equal the Registry listing operator.

Positive and negative cases, including a tampered message (recover must
fail / not match the expected seller).
"""

import pytest

from tokenshare_cli.receipt import (
    ReceiptDecodeError,
    decode_receipt,
    verify_receipt,
)
from tests.conftest import (
    BUYER_KEY,
    OTHER_KEY,
    SELLER_KEY,
    CHAIN_ID,
    addr_of,
    make_receipt,
    receipt_header,
)

SELLER = addr_of(SELLER_KEY)
OTHER = addr_of(OTHER_KEY)


def test_verify_ok_returns_recovered_seller():
    payload = make_receipt_from_key(SELLER_KEY, payment_id=42)
    receipt = decode_receipt(receipt_header(payload))
    check = verify_receipt(receipt, expected_seller=SELLER, expected_payment_id=42)
    assert check.ok is True
    assert check.reason is None
    assert check.recovered == SELLER


def test_tampered_message_fails_verification():
    """PIN negative case: bump actualAmount after signing -> recover mismatch."""
    payload = make_receipt_from_key(SELLER_KEY, payment_id=42)
    payload["message"]["actualAmount"] = 7_000_000  # tampered post-signing
    receipt = decode_receipt(receipt_header(payload))
    check = verify_receipt(receipt, expected_seller=SELLER, expected_payment_id=42)
    assert check.ok is False
    assert check.reason == "recover-mismatch"
    assert check.recovered != SELLER


def test_wrong_expected_operator_fails():
    payload = make_receipt_from_key(SELLER_KEY, payment_id=42)
    receipt = decode_receipt(receipt_header(payload))
    check = verify_receipt(receipt, expected_seller=OTHER, expected_payment_id=42)
    assert check.ok is False
    assert check.reason == "recover-mismatch"


def test_message_seller_field_mismatch_is_caught():
    payload = make_receipt_from_key(SELLER_KEY, payment_id=42, seller=SELLER)
    # operator (expected) differs from the seller inside the message:
    receipt = decode_receipt(receipt_header(payload))
    check = verify_receipt(receipt, expected_seller=OTHER, expected_payment_id=42)
    assert check.ok is False
    assert check.reason == "recover-mismatch"


def test_signature_by_other_key_fails():
    payload = make_receipt_from_key(BUYER_KEY, payment_id=42, seller=SELLER)
    receipt = decode_receipt(receipt_header(payload))
    check = verify_receipt(receipt, expected_seller=SELLER, expected_payment_id=42)
    assert check.ok is False
    assert check.reason == "recover-mismatch"


def test_paymentid_mismatch_flagged():
    payload = make_receipt_from_key(SELLER_KEY, payment_id=42)
    receipt = decode_receipt(receipt_header(payload))
    check = verify_receipt(receipt, expected_seller=SELLER, expected_payment_id=43)
    assert check.ok is False
    assert check.reason == "paymentid-mismatch"


def make_receipt_from_key(key: str, payment_id: int, **overrides) -> dict:
    from tests.conftest import make_receipt

    seller = overrides.pop("seller", SELLER)
    return make_receipt(key, payment_id, seller_addr=seller, chain_id=CHAIN_ID, **overrides)


# ---------------------------------------------------------------------------
# decoding
# ---------------------------------------------------------------------------


def test_decode_accepts_unpadded_and_padded_base64url():
    payload = make_receipt_from_key(SELLER_KEY, payment_id=1)
    raw = receipt_header(payload)  # unpadded
    receipt = decode_receipt(raw)
    assert receipt.payment_id == 1
    padded = raw + "=" * (-len(raw) % 4)
    assert decode_receipt(padded).payment_id == 1


def test_decode_receipt_fields():
    payload = make_receipt_from_key(SELLER_KEY, payment_id=9, promptTokens=100, cachedTokens=40,
                                    completionTokens=21, actualAmount=123)
    receipt = decode_receipt(receipt_header(payload))
    assert receipt.prompt_tokens == 100
    assert receipt.cached_tokens == 40
    assert receipt.completion_tokens == 21
    assert receipt.actual_amount == 123
    assert receipt.seller == SELLER


def test_decode_errors():
    with pytest.raises(ReceiptDecodeError):
        decode_receipt("")  # empty
    with pytest.raises(ReceiptDecodeError):
        decode_receipt("!!!not-base64!!!")
    bad_json = receipt_header({"domain": {}, "message": {}})  # missing signature
    with pytest.raises(ReceiptDecodeError):
        decode_receipt(bad_json)
