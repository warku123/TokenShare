"""M15 R1 — shared-mode key custody primitives (protocol v1.0-e1, errata
merged; the old AAD loop is gone — the upload AAD is an INDEPENDENT string
that never contains the body hash).

This module holds the pure custody building blocks used by `main.py`:

  * nonce store (per-seller, one outstanding nonce, TTL 600 s, atomic
    consume-under-lock; memory only — safe on restart),
  * wallet header authentication (EIP-191 over the EXACT single-line
    custody messages),
  * upstream normalization (trim ASCII, reject userinfo/query/fragment/|,
    lowercase host, default-port normalize, strip trailing "/", then the
    terminal "/v1" once) + official-allowlist gate,
  * the ECDH-P256-HKDF-SHA256-A256GCM envelope (strict point/base64/size
    validation; shared Z = x-coord 32 B big-endian; salt =
    SHA256(ASCII "tokenshare-custody-salt-v1"), info = ASCII
    "tokenshare-custody-aesgcm-v1"),
  * catalog parsing (raw upstream ids, valid ASCII, <=128 chars; servable
    computed from the EXISTING provider_for_model/host_provider helpers —
    the model list is never hardcoded here),
  * the read-only chain binding snapshot (Registry listing + Escrow
    settleDelegateOf via a minimal ABI read; every failure degrades to
    false/empty — never claim true without a successful read).

KEY DISCIPLINE: private keys, KEKs and plaintext api keys never appear in
exceptions, log lines or HTTP responses.
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import json
import os
import re
import secrets
import threading
import time
from dataclasses import dataclass
from typing import Any
from urllib.parse import urlparse

from Crypto.Cipher import AES
from Crypto.PublicKey import ECC
from eth_account import Account
from eth_account.messages import encode_defunct

# Local imports are done lazily-free: config helpers are imported at module
# import time (config.py has no import cycle back into custody.py).
from .config import (
    ENV_ALLOW_CUSTOM_UPSTREAM,
    OFFICIAL_UPSTREAM_HOSTS,
    host_provider,
    provider_for_model,
)

# ---------------------------------------------------------------------------
# Protocol constants (v1.0-e1 — verbatim).
# ---------------------------------------------------------------------------

ALG_ID = "ECDH-P256-HKDF-SHA256-A256GCM"
CUSTODY_SALT_SEED = b"tokenshare-custody-salt-v1"
CUSTODY_ENVELOPE_INFO = b"tokenshare-custody-aesgcm-v1"

NONCE_TTL_S = 600
MAX_WINDOW_S = 600
MAX_BODY_BYTES = 64 * 1024
MAX_MODEL_ID_CHARS = 128
PROBE_TIMEOUT_S = 10.0

HEADER_SELLER = "X-Tokenshare-Seller"
HEADER_SIGNATURE = "X-Tokenshare-Signature"

_SUBMIT_MESSAGE_PREFIX = "TokenShare key custody|action=submit"
_REVOKE_MESSAGE_PREFIX = "TokenShare key custody|action=revoke"
_UPLOAD_AAD_PREFIX = "TokenShare custody envelope|action=submit"

_NONCE_RE = re.compile(r"^0x[0-9a-f]{64}$")
_ADDRESS_RE = re.compile(r"^0[xX][0-9a-fA-F]{40}$")
_B64URL_RE = re.compile(r"^[A-Za-z0-9_-]+$")

# NIST P-256 (secp256r1) domain parameters. The upload key is a P-256
# scalar; the ECDH peer point is an uncompressed P-256 point.
P256_P = 0xFFFFFFFF00000001000000000000000000000000FFFFFFFFFFFFFFFFFFFFFFFF
P256_A = P256_P - 3
P256_B = 0x5AC635D8AA3A93E7B3EBBD55769886BC651D06B0CC53B0F63BCE3C3E27D2604B
P256_N = 0xFFFFFFFF00000000FFFFFFFFFFFFFFFFBCE6FAADA7179E84F3B9CAC2FC632551
P256_GX = 0x6B17D1F2E12C4247F8BCE6E563A440F277037D812DEB33A0F4A13945D898C296
P256_GY = 0x4FE342E2FE1A7F9B8EE7EB4A7C0F9E162BCE33576B315ECECBB6406837BF51F5
P256_CURVE = "P-256"  # pycryptodome curve id for NIST P-256 (secp256r1)


class CustodyError(Exception):
    """A custody request failure. `status`/`code` map 1:1 onto the protocol
    error table; `message` is static text only — never key material."""

    def __init__(self, status: int, code: str, message: str = "") -> None:
        super().__init__(message or code)
        self.status = status
        self.code = code


def _now() -> int:
    """Protocol clock (int unix seconds). Module-level so tests can freeze."""
    return int(time.time())


# ---------------------------------------------------------------------------
# Small strict codecs
# ---------------------------------------------------------------------------


def normalize_address(raw: str, *, status: int = 400, code: str = "bad_request") -> str:
    """0x-prefixed 20-byte address → lowercase hex. Anything else is the
    caller's error face (400 bad_request on public paths, 401 on auth)."""
    value = (raw or "").strip()
    if not value or not _ADDRESS_RE.fullmatch(value):
        raise CustodyError(status, code, "invalid address")
    return value.lower()


def b64url_encode(raw: bytes) -> str:
    return base64.urlsafe_b64encode(raw).decode("ascii").rstrip("=")


def b64url_decode_strict(value: str, expected_len: int | None, code: str) -> bytes:
    """Strict unpadded base64url: alphabet-only, no '=', exact length."""
    if not isinstance(value, str) or not value or not _B64URL_RE.fullmatch(value):
        raise CustodyError(400, code, "invalid base64url")
    if len(value) % 4 == 1:  # impossible for an unpadded valid encoding
        raise CustodyError(400, code, "invalid base64url")
    try:
        raw = base64.b64decode(value + "=" * (-len(value) % 4), altchars=b"-_", validate=True)
    except Exception as exc:
        raise CustodyError(400, code, "invalid base64url") from exc
    if expected_len is not None and len(raw) != expected_len:
        raise CustodyError(400, code, "invalid base64url length")
    return raw


def sha256_hex(raw: bytes) -> str:
    return hashlib.sha256(raw).hexdigest()


# ---------------------------------------------------------------------------
# EXACT protocol messages / AAD (single line, no spaces beyond the literals)
# ---------------------------------------------------------------------------


def build_submit_message(
    *,
    seller: str,
    chain_id: int,
    escrow_addr: str,
    registry_addr: str,
    origin: str,
    upstream_base_url: str,
    nonce: str,
    body_sha256: str,
    issued: int,
    expires: int,
) -> str:
    """EXACT submit message — single line, NO extra spaces/newlines."""
    return (
        f"{_SUBMIT_MESSAGE_PREFIX}"
        f"|seller={seller.lower()}"
        f"|chain={int(chain_id)}"
        f"|escrow={escrow_addr.lower()}"
        f"|registry={registry_addr.lower()}"
        f"|relay={origin}"
        f"|upstream={upstream_base_url}"
        f"|nonce={nonce}"
        f"|body_sha256={body_sha256}"
        f"|issued={int(issued)}"
        f"|expires={int(expires)}"
    )


def build_upload_aad(
    *,
    seller: str,
    chain_id: int,
    escrow_addr: str,
    registry_addr: str,
    origin: str,
    upstream_base_url: str,
    nonce: str,
    issued: int,
    expires: int,
) -> str:
    """EXACT upload AAD — independent of the submit message: NO body hash.
    ("TokenShare custody envelope|…" ≠ "TokenShare key custody|…", so a
    submit signature can never be replayed as an envelope AAD or back.)"""
    return (
        f"{_UPLOAD_AAD_PREFIX}"
        f"|seller={seller.lower()}"
        f"|chain={int(chain_id)}"
        f"|escrow={escrow_addr.lower()}"
        f"|registry={registry_addr.lower()}"
        f"|relay={origin}"
        f"|upstream={upstream_base_url}"
        f"|nonce={nonce}"
        f"|issued={int(issued)}"
        f"|expires={int(expires)}"
    )


def build_revoke_message(
    *,
    seller: str,
    chain_id: int,
    escrow_addr: str,
    registry_addr: str,
    origin: str,
    nonce: str,
    issued: int,
    expires: int,
) -> str:
    """EXACT revoke message — no upstream, no body hash (DELETE body has
    neither)."""
    return (
        f"{_REVOKE_MESSAGE_PREFIX}"
        f"|seller={seller.lower()}"
        f"|chain={int(chain_id)}"
        f"|escrow={escrow_addr.lower()}"
        f"|registry={registry_addr.lower()}"
        f"|relay={origin}"
        f"|nonce={nonce}"
        f"|issued={int(issued)}"
        f"|expires={int(expires)}"
    )


def normalize_seller_header(request: Any) -> str:
    """Normalize X-Tokenshare-Seller → lowercase address; 401 unauthorized
    on missing/invalid (auth-face error, unlike the 400 public paths)."""
    seller_raw = request.headers.get(HEADER_SELLER)
    if not seller_raw:
        raise CustodyError(401, "unauthorized", "missing wallet headers")
    return normalize_address(seller_raw, status=401, code="unauthorized")


def recover_wallet_signature(expected_message: str, signature: str | None) -> str:
    """EIP-191 recover over the EXACT expected message → lowercase address.
    401 unauthorized on any failure. Runs BEFORE decrypt/upstream."""
    if not signature or not signature.strip():
        raise CustodyError(401, "unauthorized", "missing wallet headers")
    try:
        recovered = Account.recover_message(
            encode_defunct(text=expected_message), signature=signature.strip()
        )
    except Exception as exc:
        raise CustodyError(401, "unauthorized", "signature invalid") from exc
    return str(recovered).lower()


def verify_wallet_headers(request: Any, expected_message: str) -> str:
    """EIP-191 authenticate the wallet headers: recovered signer must equal
    the (normalized) X-Tokenshare-Seller address. 401 unauthorized on any
    missing/invalid input. Runs BEFORE decrypt/upstream per the protocol."""
    seller = normalize_seller_header(request)
    recovered = recover_wallet_signature(expected_message, request.headers.get(HEADER_SIGNATURE))
    if recovered != seller:
        raise CustodyError(401, "unauthorized", "signer is not the seller")
    return seller


def custody_allowed_origins(public_origin: str) -> Any:
    """ora20 narrow fix: the custody origin gate REUSES the EXISTING
    CORS configuration (parse_cors_origins over RELAY_CORS_ORIGINS) — a
    browser console on a configured frontend origin (e.g. the Vercel web
    calling the Phala relay cross-origin) is accepted alongside the relay's
    own canonical origin. The existing dev/demo `*` semantics are preserved
    verbatim (unset/blank RELAY_CORS_ORIGINS still yields ["*"] — the same
    face the CORSMiddleware has always applied to these endpoints); no NEW
    default-* is introduced anywhere, and production MUST pin the frontend
    explicitly (e.g. https://tokenshare-web.vercel.app) to get enforcement.

    Returns the string "*" (dev/demo: any origin) or the effective list."""
    from .config import ENV_CORS_ORIGINS, parse_cors_origins

    configured = parse_cors_origins(os.environ.get(ENV_CORS_ORIGINS))
    if "*" in configured:
        return "*"
    allowed = [entry for entry in configured if entry]
    if public_origin and public_origin not in allowed:
        allowed.append(public_origin)  # the relay's own canonical origin
    return allowed


def check_request_origin(request: Any, public_origin: str) -> None:
    """Transport-level anti-confusion for browser custody callers. A
    PRESENT Origin header must be an allowed one: the relay's canonical
    origin or a RELAY_CORS_ORIGINS-pinned frontend origin ("*" keeps the
    existing dev/demo semantics; server-to-server callers send no Origin
    header and are never blocked). The SIGNED message ALWAYS binds the
    CONFIG origin (server authority) — the Origin/Host headers can never
    override, rewrite or redirect the relay domain inside the signature."""
    origin_header = (request.headers.get("Origin") or "").strip().rstrip("/")
    if not origin_header:
        return
    allowed = custody_allowed_origins(public_origin)
    if allowed == "*":
        return
    if origin_header.lower() not in {entry.lower().rstrip("/") for entry in allowed}:
        raise CustodyError(
            409,
            "origin_mismatch",
            "request origin is not an allowed custody caller origin",
        )


# ---------------------------------------------------------------------------
# Body shaping (strict: raw bytes, compact ASCII, exact key order)
# ---------------------------------------------------------------------------


def _pairs_object(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    return dict(pairs)


def _compact_roundtrip(raw: bytes, obj: Any) -> None:
    """The body must be COMPACT ASCII JSON — the canonical re-serialization
    must reproduce the exact bytes (this also pins the key order)."""
    try:
        compact = json.dumps(obj, separators=(",", ":"), ensure_ascii=True).encode("ascii")
    except Exception as exc:
        raise CustodyError(400, "bad_request", "body not compact ASCII JSON") from exc
    if compact != raw:
        raise CustodyError(400, "bad_request", "body not compact ASCII JSON")


def _exact_keys(obj: Any, keys: list[str], code: str) -> None:
    if not isinstance(obj, dict) or list(obj.keys()) != keys:
        raise CustodyError(400, code, "unexpected key order/set")


def _int_field(obj: dict[str, Any], key: str) -> int:
    value = obj.get(key)
    # bools are ints in Python — reject them explicitly.
    if isinstance(value, bool) or not isinstance(value, int):
        raise CustodyError(400, "bad_request", f"{key} must be an integer")
    return value


def _str_field(obj: dict[str, Any], key: str) -> str:
    value = obj.get(key)
    if not isinstance(value, str) or not value.strip():
        raise CustodyError(400, "bad_request", f"{key} must be a non-empty string")
    return value


def parse_submit_body(raw: bytes) -> dict[str, Any]:
    """POST /sellers/keys raw body → {nonce, issued_at, expires_at,
    upstream_base_url, envelope:{alg,epk,iv,ct}}. Exact key order, compact
    ASCII, strict types. Only the ALG VALUE check is deferred (crypto-class
    error); everything structural fails here as 400 bad_request."""
    if len(raw) > MAX_BODY_BYTES:
        raise CustodyError(400, "bad_request", "body exceeds 64KiB")
    try:
        text = raw.decode("ascii")
    except UnicodeDecodeError as exc:
        raise CustodyError(400, "bad_request", "body must be ASCII") from exc
    try:
        obj = json.loads(text, object_pairs_hook=_pairs_object)
    except ValueError as exc:
        raise CustodyError(400, "bad_request", "invalid JSON body") from exc
    _compact_roundtrip(raw, obj)
    _exact_keys(
        obj,
        ["nonce", "issued_at", "expires_at", "upstream_base_url", "envelope"],
        "bad_request",
    )
    nonce = _str_field(obj, "nonce")
    if not _NONCE_RE.fullmatch(nonce):
        raise CustodyError(400, "bad_request", "nonce format invalid")
    issued_at = _int_field(obj, "issued_at")
    expires_at = _int_field(obj, "expires_at")
    upstream = _str_field(obj, "upstream_base_url")
    envelope = obj.get("envelope")
    _exact_keys(envelope, ["alg", "epk", "iv", "ct"], "bad_request")
    return {
        "nonce": nonce,
        "issued_at": issued_at,
        "expires_at": expires_at,
        "upstream_base_url": upstream,
        "envelope": envelope,
    }


def parse_revoke_body(raw: bytes) -> dict[str, Any]:
    """DELETE /sellers/keys raw body → {nonce, issued_at, expires_at} with
    the same strictness (non-interchangeable with POST via the message)."""
    if len(raw) > MAX_BODY_BYTES:
        raise CustodyError(400, "bad_request", "body exceeds 64KiB")
    try:
        text = raw.decode("ascii")
    except UnicodeDecodeError as exc:
        raise CustodyError(400, "bad_request", "body must be ASCII") from exc
    try:
        obj = json.loads(text, object_pairs_hook=_pairs_object)
    except ValueError as exc:
        raise CustodyError(400, "bad_request", "invalid JSON body") from exc
    _compact_roundtrip(raw, obj)
    _exact_keys(obj, ["nonce", "issued_at", "expires_at"], "bad_request")
    nonce = _str_field(obj, "nonce")
    if not _NONCE_RE.fullmatch(nonce):
        raise CustodyError(400, "bad_request", "nonce format invalid")
    return {
        "nonce": nonce,
        "issued_at": _int_field(obj, "issued_at"),
        "expires_at": _int_field(obj, "expires_at"),
    }


def check_timestamp_window(issued_at: int, expires_at: int) -> None:
    """SHAPE face (400): the declared window must be sane (non-negative,
    <= MAX_WINDOW_S). FRESHNESS is enforced against the SERVER nonce record
    at consume time (TTL expired / window echo mismatch → 409
    nonce_invalid): the record's window is server-minted, so a matching
    body is by construction the server's own window and only the record's
    TTL can make it stale."""
    if expires_at - issued_at > MAX_WINDOW_S or expires_at - issued_at < 0:
        raise CustodyError(400, "bad_request", "timestamp window too wide")


# ---------------------------------------------------------------------------
# Nonce store (memory only — safe on restart: after a restart every old
# nonce is simply unknown → consume 409s; nothing persists, nothing leaks).
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class NonceRecord:
    nonce: str
    issued_at: int
    expires_at: int


class NonceStore:
    """One outstanding random 32-byte nonce per seller address. A new issue
    atomically REPLACES the previous one. Consume happens under the lock —
    concurrent duplicates: exactly one wins."""

    def __init__(self, ttl_s: int = NONCE_TTL_S) -> None:
        self._ttl = ttl_s
        self._lock = threading.Lock()
        self._records: dict[str, NonceRecord] = {}

    def issue(self, seller: str) -> NonceRecord:
        now = _now()
        record = NonceRecord(
            nonce="0x" + secrets.token_bytes(32).hex(),
            issued_at=now,
            expires_at=now + self._ttl,
        )
        with self._lock:
            self._records[seller] = record
        return record

    def consume(self, seller: str, nonce: str, issued_at: int, expires_at: int) -> NonceRecord:
        """Atomic compare-and-delete. The client's issued/expires MUST match
        the server record (the message binds the RECORD's window, never a
        client echo). Unknown/expired/mismatched → 409 nonce_invalid."""
        with self._lock:
            record = self._records.get(seller)
            if record is None:
                raise CustodyError(409, "nonce_invalid", "no outstanding nonce")
            if record.nonce != nonce:
                raise CustodyError(409, "nonce_invalid", "nonce mismatch")
            if _now() >= record.expires_at:
                del self._records[seller]
                raise CustodyError(409, "nonce_invalid", "nonce expired")
            if record.issued_at != issued_at or record.expires_at != expires_at:
                raise CustodyError(409, "nonce_invalid", "nonce window mismatch")
            del self._records[seller]
            return record

    def outstanding(self, seller: str) -> bool:  # pragma: no cover - test aid
        with self._lock:
            return seller in self._records


# ---------------------------------------------------------------------------
# P-256 + HKDF + AES-GCM envelope
# ---------------------------------------------------------------------------


def validate_upload_scalar(scalar: int) -> int:
    if isinstance(scalar, bool) or not isinstance(scalar, int) or not (1 <= scalar < P256_N):
        raise ValueError("upload key is not a valid P-256 scalar")
    return scalar


def raw32_to_upload_scalar(raw: bytes) -> int:
    """dstack raw32 → P-256 scalar: (int(raw) % (order-1)) + 1."""
    if len(raw) != 32:
        raise ValueError("upload raw key must be 32 bytes")
    return validate_upload_scalar((int.from_bytes(raw, "big") % (P256_N - 1)) + 1)


def upload_public_bytes(scalar: int) -> bytes:
    """Uncompressed 65-byte public key 0x04||X||Y for the upload scalar."""
    point = ECC.construct(curve=P256_CURVE, d=scalar).pointQ
    return (
        b"\x04"
        + int(point.x).to_bytes(32, "big")
        + int(point.y).to_bytes(32, "big")
    )


def _point_from_uncompressed(raw: bytes) -> ECC.EccPoint:
    """Strict 65-byte uncompressed point decode: 0x04 prefix, coordinates in
    field range, point ON the curve. Any deviation → envelope_invalid."""
    if len(raw) != 65 or raw[0] != 0x04:
        raise CustodyError(400, "envelope_invalid", "epk encoding invalid")
    x = int.from_bytes(raw[1:33], "big")
    y = int.from_bytes(raw[33:65], "big")
    if not (0 < x < P256_P and 0 < y < P256_P):
        raise CustodyError(400, "envelope_invalid", "epk coordinates out of field")
    if (y * y - (x * x * x + P256_A * x + P256_B)) % P256_P != 0:
        raise CustodyError(400, "envelope_invalid", "epk point not on curve")
    return ECC.EccPoint(x, y, curve=P256_CURVE)


def _hkdf_sha256(ikm: bytes, salt: bytes, info: bytes, length: int = 32) -> bytes:
    """RFC 5869 HKDF with SHA-256 (extract+expand, explicit — no shortcuts)."""
    if len(salt) == 0:
        salt = b"\x00" * hashlib.sha256().digest_size
    prk = hmac.new(salt, ikm, hashlib.sha256).digest()
    okm = b""
    block = b""
    counter = 1
    while len(okm) < length:
        block = hmac.new(prk, block + info + bytes([counter]), hashlib.sha256).digest()
        okm += block
        counter += 1
    return okm[:length]


def derive_envelope_key(upload_scalar: int, epk_raw: bytes) -> bytes:
    """ECDH shared Z = x-coord of (d · EPK), 32 B big-endian → HKDF → 32 B
    AES-256 key. Rejects the (measure-zero) point at infinity."""
    peer = _point_from_uncompressed(epk_raw)
    shared = peer * upload_scalar
    if shared.is_point_at_infinity():
        raise CustodyError(400, "envelope_invalid", "ecdh produced infinity")
    z = int(shared.x).to_bytes(32, "big")
    return _hkdf_sha256(
        ikm=z,
        salt=hashlib.sha256(CUSTODY_SALT_SEED).digest(),
        info=CUSTODY_ENVELOPE_INFO,
        length=32,
    )


def _aesgcm_decrypt(key: bytes, iv: bytes, ct_with_tag: bytes, aad: str) -> bytes:
    if len(iv) != 12:
        raise CustodyError(400, "envelope_invalid", "iv must be 12 bytes")
    if len(ct_with_tag) < 16:
        raise CustodyError(400, "envelope_invalid", "ciphertext too short")
    cipher = AES.new(key, AES.MODE_GCM, nonce=iv, mac_len=16)
    cipher.update(aad.encode("utf-8"))
    try:
        return cipher.decrypt_and_verify(ct_with_tag[:-16], ct_with_tag[-16:])
    except ValueError as exc:
        raise CustodyError(400, "envelope_invalid", "authentication failed") from exc


def decrypt_envelope(upload_scalar: int, envelope: dict[str, Any], aad: str) -> bytes:
    """Decrypt the custody envelope → plaintext bytes. Strict alg/point/
    base64/size validation; any failure is 400 envelope_invalid."""
    if not isinstance(envelope, dict) or envelope.get("alg") != ALG_ID:
        raise CustodyError(400, "envelope_invalid", "unsupported alg")
    epk = b64url_decode_strict(envelope.get("epk"), 65, "envelope_invalid")
    iv = b64url_decode_strict(envelope.get("iv"), 12, "envelope_invalid")
    ct = b64url_decode_strict(envelope.get("ct"), None, "envelope_invalid")
    key = derive_envelope_key(upload_scalar, epk)
    return _aesgcm_decrypt(key, iv, ct, aad)


def encrypt_envelope(
    upload_scalar: int,
    plaintext: bytes,
    aad: str,
    *,
    eph_scalar: int | None = None,
    iv: bytes | None = None,
) -> dict[str, str]:
    """Inverse of decrypt_envelope (console/test mirror + vector fixture
    generation). Random eph scalar / 96-bit IV unless pinned."""
    eph = eph_scalar if eph_scalar is not None else secrets.randbelow(P256_N - 1) + 1
    validate_upload_scalar(eph)
    iv = iv if iv is not None else secrets.token_bytes(12)
    epk = upload_public_bytes(eph)
    key = derive_envelope_key(upload_scalar, epk)
    cipher = AES.new(key, AES.MODE_GCM, nonce=iv, mac_len=16)
    cipher.update(aad.encode("utf-8"))
    ct, _tag = cipher.encrypt_and_digest(plaintext)
    ct_with_tag = ct + _tag
    return {"alg": ALG_ID, "epk": b64url_encode(epk), "iv": b64url_encode(iv), "ct": b64url_encode(ct_with_tag)}


def parse_envelope_plaintext(plaintext: bytes) -> str:
    """Plaintext must be compact ASCII JSON exactly {"api_key": "..."}."""
    try:
        text = plaintext.decode("ascii")
    except UnicodeDecodeError as exc:
        raise CustodyError(400, "envelope_invalid", "plaintext not ASCII") from exc
    try:
        obj = json.loads(text, object_pairs_hook=_pairs_object)
    except ValueError as exc:
        raise CustodyError(400, "envelope_invalid", "plaintext not JSON") from exc
    _exact_keys(obj, ["api_key"], "envelope_invalid")
    api_key = obj.get("api_key")
    if not isinstance(api_key, str) or not api_key.strip():
        raise CustodyError(400, "envelope_invalid", "api_key invalid")
    _compact_roundtrip(plaintext, obj)
    return api_key


# ---------------------------------------------------------------------------
# Upstream normalization + official gate + probe
# ---------------------------------------------------------------------------


def normalize_custody_upstream(raw: str) -> str:
    """Custody upstream normalization (v1.0-e1): trim ASCII whitespace;
    reject userinfo/query/fragment/|; lowercase host; drop the default
    port; strip trailing "/"; then strip a terminal "/v1" ONCE. The result
    must be a canonical http(s) base (e.g. https://api.kimi.com/coding);
    the probe appends /v1/models."""
    value = (raw or "").strip()
    if not value:
        raise CustodyError(400, "bad_request", "upstream_base_url required")
    if "|" in value:
        raise CustodyError(400, "bad_request", "upstream_base_url contains |")
    try:
        parsed = urlparse(value)
    except ValueError as exc:
        raise CustodyError(400, "bad_request", "upstream_base_url unparseable") from exc
    if parsed.username or parsed.password:
        raise CustodyError(400, "bad_request", "upstream_base_url carries userinfo")
    if parsed.query or parsed.fragment:
        raise CustodyError(400, "bad_request", "upstream_base_url carries query/fragment")
    if parsed.scheme not in ("http", "https") or not parsed.hostname:
        raise CustodyError(400, "bad_request", "upstream_base_url must be an http(s) URL")
    host = parsed.hostname.lower()
    # S2-C2: the .port ACCESS (not just urlparse) raises ValueError for
    # malformed/out-of-range ports (":70000" / ":notaport") — it MUST be
    # inside a guarded block so a SIGNED custody POST (or a chain listing
    # endpoint read in the shared preflight) fails closed with the
    # documented 400 bad_request face instead of an unhandled 500, and a
    # malformed URL is never probed.
    try:
        port = parsed.port
    except ValueError as exc:
        raise CustodyError(400, "bad_request", "upstream_base_url has an invalid port") from exc
    default_port = (parsed.scheme == "https" and port == 443) or (
        parsed.scheme == "http" and port == 80
    )
    base = f"{parsed.scheme}://{host}" + ("" if port is None or default_port else f":{port}")
    path = parsed.path
    while path.endswith("/"):
        path = path[:-1]
    if path.endswith("/v1"):
        path = path[: -len("/v1")]
    return base + path


def upstream_is_official(normalized: str) -> bool:
    """Official = HTTPS + exact hostname on the EXISTING allowlist."""
    try:
        parsed = urlparse(normalized)
    except ValueError:
        return False
    return parsed.scheme == "https" and (parsed.hostname or "").lower() in OFFICIAL_UPSTREAM_HOSTS


def require_official_or_custom(normalized: str) -> None:
    """Official hosts pass; custom hosts only with the EXISTING
    ALLOW_CUSTOM_UPSTREAM=1 dev flag; anything else 400
    upstream_not_official."""
    if upstream_is_official(normalized):
        return
    if os.environ.get(ENV_ALLOW_CUSTOM_UPSTREAM, "").strip() == "1":
        return
    raise CustodyError(
        400,
        "upstream_not_official",
        f"upstream host not on the official allowlist {sorted(OFFICIAL_UPSTREAM_HOSTS)}",
    )


# Probe transport seam: None → real network; tests inject httpx.MockTransport.
_probe_transport: Any = None


@dataclass(frozen=True)
class ProbeResult:
    ok: bool
    status: int
    models: dict[str, Any] | None
    error: str | None


async def probe_upstream_key(normalized: str, api_key: str) -> ProbeResult:
    """GET {normalized}/v1/models with the seller key — 10 s timeout,
    certificate verification ON, redirects NEVER followed (a 3xx is an
    upstream failure, not a destination: SSRF/redirect hardening).

    Mapping (v1.0-e1): 200 → ok; 401/403 → 400 upstream_key_rejected;
    network/5xx/3xx/non-JSON-200 → 502 upstream_unreachable."""
    import httpx

    url = f"{normalized}/v1/models"
    try:
        async with httpx.AsyncClient(
            timeout=PROBE_TIMEOUT_S,
            verify=True,
            follow_redirects=False,
            transport=_probe_transport,
        ) as probe:
            resp = await probe.get(url, headers={"Authorization": f"Bearer {api_key}"})
    except httpx.HTTPError as exc:
        return ProbeResult(ok=False, status=0, models=None, error=f"unreachable: {type(exc).__name__}")
    if resp.status_code in (401, 403):
        # Static detail only — the upstream body could echo the key.
        return ProbeResult(ok=False, status=resp.status_code, models=None, error=None)
    if 300 <= resp.status_code < 400 or resp.status_code >= 500:
        return ProbeResult(ok=False, status=resp.status_code, models=None, error=None)
    if resp.status_code != 200:
        return ProbeResult(ok=False, status=resp.status_code, models=None, error=None)
    try:
        payload = resp.json()
    except ValueError:
        return ProbeResult(ok=False, status=200, models=None, error="non-JSON catalog")
    if not isinstance(payload, dict):
        return ProbeResult(ok=False, status=200, models=None, error="non-object catalog")
    return ProbeResult(ok=True, status=200, models=payload, error=None)


def catalog_from_payload(payload: dict[str, Any], normalized: str) -> list[dict[str, Any]]:
    """Raw upstream catalog → [{model, servable}]. Keep the upstream's ids
    (dedup, order-preserving) that are valid ASCII and <=128 chars; servable
    = EXISTING provider_for_model(id) == host_provider(normalized). No
    hardcoded model list anywhere."""
    data = payload.get("data") if isinstance(payload, dict) else None
    ids: list[str] = []
    if isinstance(data, list):
        for item in data:
            mid = item.get("id") if isinstance(item, dict) else None
            if (
                isinstance(mid, str)
                and mid
                and mid.isascii()
                and len(mid) <= MAX_MODEL_ID_CHARS
                and mid not in ids
            ):
                ids.append(mid)
    host_provider_id = host_provider(normalized)
    return [
        {"model": mid, "servable": provider_for_model(mid) == host_provider_id}
        for mid in ids
    ]


def upstream_host_of(normalized: str) -> str:
    try:
        return urlparse(normalized).hostname or ""
    except ValueError:
        return ""


# ---------------------------------------------------------------------------
# Read-only chain binding snapshot (status/submit responses)
# ---------------------------------------------------------------------------


def catalog_json_compact(catalog: list[dict[str, Any]]) -> str:
    """Compact ASCII JSON array with key order model,servable (keystore meta)."""
    return json.dumps(
        [{"model": e["model"], "servable": bool(e["servable"])} for e in catalog],
        separators=(",", ":"),
        ensure_ascii=True,
    )
