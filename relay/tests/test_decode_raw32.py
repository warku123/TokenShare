"""_decode_raw32 shape tests — dstack-sdk 0.5.x compatibility regression.

Root cause of the real-TEE boot crash: dstack-sdk 0.5.4's
``GetKeyResponse.decode_key()`` returns **bytes**, while the old decoder
only accepted ``str`` — every mock in this suite returned str, so local
runs were green while the real CVM failed closed. These tests use the
REAL installed dstack_sdk response type (no SDK mocking) plus direct
shape checks against ``relay.app.config._decode_raw32``.
"""

from __future__ import annotations

import types
from typing import Any

import pytest

dstack_sdk = pytest.importorskip("dstack_sdk")
from dstack_sdk import GetKeyResponse  # noqa: E402

from relay.app.config import ConfigError, _decode_raw32  # noqa: E402

PATH = "tokenshare/shared/signer/v1"

# Deterministic, valueless test material (never a real key).
HEX32 = "ab" * 32
RAW32 = bytes.fromhex(HEX32)


def _resp(hex_key: str) -> GetKeyResponse:
    """A REAL dstack_sdk GetKeyResponse (key field is a hex str)."""
    return GetKeyResponse(key=hex_key, signature_chain=[])


class _FakeResult:
    """Stands in for a decoded get_key result with a fixed payload."""

    def __init__(self, payload: Any) -> None:
        self._payload = payload

    def decode_key(self) -> Any:
        return self._payload


# --------------------------------------------------------------------------
# Real SDK shape: GetKeyResponse.decode_key() → bytes (the 0.5.x regression)
# --------------------------------------------------------------------------
def test_real_dstack_sdk_decode_key_returns_bytes_and_is_accepted() -> None:
    resp = _resp(HEX32)
    decoded = resp.decode_key()
    assert isinstance(decoded, bytes)  # SDK 0.5.x contract: bytes, not str
    assert _decode_raw32(resp, PATH) == RAW32


def test_real_dstack_sdk_bytes_result_via_decode_key_call_shape() -> None:
    """Same acceptance when _decode_raw32 receives the already-decoded
    bytes wrapped in a get_key-result-shaped object."""
    assert _decode_raw32(_FakeResult(_resp(HEX32).decode_key()), PATH) == RAW32


# --------------------------------------------------------------------------
# str hex paths (with / without 0x) — legacy mock shape, still supported
# --------------------------------------------------------------------------
def test_str_hex_with_0x_prefix() -> None:
    assert _decode_raw32(_FakeResult("0x" + HEX32), PATH) == RAW32


def test_str_hex_without_prefix() -> None:
    assert _decode_raw32(_FakeResult(HEX32), PATH) == RAW32


def test_str_hex_uppercase_and_whitespace() -> None:
    assert _decode_raw32(_FakeResult("  0X" + HEX32.upper() + "  "), PATH) == RAW32


def test_bytearray_len_32_accepted() -> None:
    assert _decode_raw32(_FakeResult(bytearray(RAW32)), PATH) == RAW32


# --------------------------------------------------------------------------
# Negative shapes — all fail closed with ConfigError
# --------------------------------------------------------------------------
@pytest.mark.parametrize(
    "payload",
    [
        RAW32 + b"\x00",  # bytes, 33
        RAW32[:-1],  # bytes, 31
        bytearray(RAW32 + b"\x00"),  # bytearray, 33
        b"",  # empty bytes
        "cc" * 31,  # 62 hex chars
        "cc" * 33,  # 66 hex chars
        "cc" * 63,  # odd length
        "not-a-key",  # non-hex chars
        "0x" + "zz" * 32,  # 0x-prefixed non-hex
        "",  # empty str
        "   ",  # whitespace-only str
        None,  # wrong type
        123,  # wrong type
        b"\x00" * 8,  # bytes, wrong length (8)
        ["aa" * 32],  # list
    ],
)
def test_bad_shapes_fail_closed(payload: Any) -> None:
    with pytest.raises(ConfigError):
        _decode_raw32(_FakeResult(payload), PATH)


def test_decode_key_raise_fails_closed() -> None:
    def boom() -> bytes:
        raise ValueError("bad hex from agent")

    with pytest.raises(ConfigError, match="decode failed"):
        _decode_raw32(types.SimpleNamespace(decode_key=boom), PATH)


# --------------------------------------------------------------------------
# No key material in messages — only type/length may appear
# --------------------------------------------------------------------------
def test_no_key_content_in_any_error_message() -> None:
    """For every failing shape, the message must not echo the material."""
    marker_hex = "deadbeef" * 8  # 64 chars, valueless marker
    secrets: dict[str, Any] = {
        "long_bytes": RAW32 + b"\x07",
        "short_hex": "ef" * 31,
        "non_hex": "hello-world-this-is-not-hex-material-xx",
        "prefixed_bad": "0x" + "cd" * 20,
        "odd_hex": marker_hex[:63],
        "none": None,
    }
    for payload in secrets.values():
        with pytest.raises(ConfigError) as exc:
            _decode_raw32(_FakeResult(payload), PATH)
        msg = str(exc.value)
        assert PATH in msg
        if isinstance(payload, bytes):
            assert payload.hex() not in msg
        elif isinstance(payload, str):
            stripped = payload.strip().lower().removeprefix("0x")
            assert stripped not in msg
            assert payload.strip() not in msg


def test_marker_hex_never_leaks_via_real_sdk_path() -> None:
    """A real GetKeyResponse whose hex is too short must not leak the hex."""
    marker = "beefcafe" * 8  # 64 chars
    with pytest.raises(ConfigError) as exc:
        _decode_raw32(_FakeResult(_resp(marker[:40]).decode_key()), PATH)
    msg = str(exc.value)
    assert "beefcafe" not in msg
    assert "20 raw bytes" in msg  # 40 hex chars → 20 bytes, length reported only
