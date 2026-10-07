"""M15 R1 — encrypted keystore for shared-mode custody (protocol v1.0-e1).

File layout (JSON):
    {"schema": 1, "entries": {"<seller_lower>": {
        "v": 1, "created": <int>, "updated": <int>,
        "upstream_base_url": "...", "key_fingerprint": "<sha256-hex-64>",
        "catalog_snapshot": [{"model": "...", "servable": true}, ...],
        "wrap": {"iv": "<b64url-12B>", "ct": "<b64url>"},
        "enc":  {"iv": "<b64url-12B>", "ct": "<b64url>"}}}}

Crypto: a fresh random 32-byte DEK per entry encrypts the plaintext api key
(AES-256-GCM, 96-bit IV); the KEK wraps the DEK (AES-256-GCM, 96-bit IV).
Both ciphertexts are bound by EXACT canonical AAD strings built from the
meta line — seller/url/catalog/chain/escrow/registry/origin/fingerprint and
timestamps — so at-rest copy/swap between sellers, upstreams, catalogs or
domains fails authentication.

Canonical meta (EXACT):
  seller={seller_lower}|schema={schema}|v={v}|chain={chain_id}|
  escrow={escrow}|registry={registry}|origin={RELAY_PUBLIC_ORIGIN}|
  upstream={upstream_base_url}|fingerprint={key_fingerprint}|
  catalog={catalog_json}|created={created}|updated={updated}
(one line, no spaces; catalog_json = compact ASCII array, key order
model,servable).

Persistence: same-directory 0600 temp file + fsync + os.replace. The
in-memory map commits ONLY after the file write succeeds (rollback on
failure → the previous valid key set survives every crash/failure).
Startup load: corrupted JSON or ALL entries failing unwrap → ConfigError
with the file PRESERVED (never overwritten); partial failures exclude the
bad entries (warn, no values) and the next successful set() persists the
valid set. A nonce-style empty overwrite on a wrong KEK can never happen:
load refuses before any write path exists.
"""

from __future__ import annotations

import hashlib
import json
import logging
import os
import secrets
import tempfile
import time
from dataclasses import dataclass
from typing import Any

from Crypto.Cipher import AES

from .custody import b64url_decode_strict, b64url_encode, catalog_json_compact

logger = logging.getLogger("tokenshare.relay")

SCHEMA_VERSION = 1
ENTRY_VERSION = 1

WRAP_AAD_PREFIX = "tokenshare-keystore-wrap-v1|"
ENC_AAD_PREFIX = "tokenshare-keystore-payload-v1|"


class KeystoreConfigError(RuntimeError):
    """Fatal keystore state at startup (corrupted file / total unwrap
    failure). Raised as a ConfigError-class failure — startup aborts, the
    file on disk is preserved untouched."""


@dataclass
class _Entry:
    created: int
    updated: int
    upstream_base_url: str
    key_fingerprint: str
    catalog: list[dict[str, Any]]
    wrap_iv: bytes
    wrap_ct: bytes
    enc_iv: bytes
    enc_ct: bytes
    dek: bytes | None = None  # cached in memory only; never persisted/logged


def build_keystore_meta(
    *,
    seller: str,
    schema: int,
    v: int,
    chain_id: int,
    escrow_addr: str,
    registry_addr: str,
    origin: str,
    upstream_base_url: str,
    key_fingerprint: str,
    catalog: list[dict[str, Any]],
    created: int,
    updated: int,
) -> str:
    """Canonical meta line — EXACT field order per v1.0-e1."""
    return (
        f"seller={seller.lower()}"
        f"|schema={int(schema)}"
        f"|v={int(v)}"
        f"|chain={int(chain_id)}"
        f"|escrow={escrow_addr.lower()}"
        f"|registry={registry_addr.lower()}"
        f"|origin={origin}"
        f"|upstream={upstream_base_url}"
        f"|fingerprint={key_fingerprint}"
        f"|catalog={catalog_json_compact(catalog)}"
        f"|created={int(created)}"
        f"|updated={int(updated)}"
    )


def _wrap_aad(meta: str) -> bytes:
    return (WRAP_AAD_PREFIX + meta).encode("utf-8")


def _enc_aad(meta: str) -> bytes:
    return (ENC_AAD_PREFIX + meta).encode("utf-8")


def _gcm_encrypt(key: bytes, iv: bytes, plaintext: bytes, aad: bytes) -> bytes:
    cipher = AES.new(key, AES.MODE_GCM, nonce=iv, mac_len=16)
    cipher.update(aad)
    ct, _tag = cipher.encrypt_and_digest(plaintext)
    return ct + _tag  # ct includes the 16-byte tag


def _gcm_decrypt(key: bytes, iv: bytes, ct_with_tag: bytes, aad: bytes) -> bytes:
    if len(iv) != 12 or len(ct_with_tag) < 16:
        raise ValueError("keystore blob malformed")
    cipher = AES.new(key, AES.MODE_GCM, nonce=iv, mac_len=16)
    cipher.update(aad)
    return cipher.decrypt_and_verify(ct_with_tag[:-16], ct_with_tag[-16:])


class Keystore:
    """Encrypted at-rest store for enrolled seller api keys (shared mode)."""

    def __init__(
        self,
        path: str,
        kek: bytes,
        *,
        chain_id: int,
        escrow_addr: str,
        registry_addr: str,
        origin: str,
    ) -> None:
        if len(kek) != 32:
            raise KeystoreConfigError("KEK must be 32 bytes")
        self._path = path
        self._kek = kek
        self._chain_id = chain_id
        self._escrow = escrow_addr.lower()
        self._registry = registry_addr.lower()
        self._origin = origin
        self._entries: dict[str, _Entry] = {}
        self.load()

    # ------------------------------------------------------------------ meta
    def _meta(self, seller: str, entry: _Entry) -> str:
        return build_keystore_meta(
            seller=seller,
            schema=SCHEMA_VERSION,
            v=ENTRY_VERSION,
            chain_id=self._chain_id,
            escrow_addr=self._escrow,
            registry_addr=self._registry,
            origin=self._origin,
            upstream_base_url=entry.upstream_base_url,
            key_fingerprint=entry.key_fingerprint,
            catalog=entry.catalog,
            created=entry.created,
            updated=entry.updated,
        )

    # ------------------------------------------------------------------ load
    def load(self) -> None:
        """Load + unwrap every entry. Corrupted JSON / schema mismatch /
        ALL-unwrap-failure → KeystoreConfigError (file preserved, nothing
        written). Partial failures are excluded with a warning (no values
        in the log); the next successful set() persists the valid set."""
        if not os.path.exists(self._path):
            self._entries = {}
            return
        try:
            with open(self._path, "rb") as fh:
                doc = json.loads(fh.read().decode("utf-8"))
        except (OSError, ValueError) as exc:
            raise KeystoreConfigError(
                f"keystore file {self._path!r} is unreadable/corrupted — refusing to "
                "start (file preserved; fix or remove it manually)"
            ) from exc
        if not isinstance(doc, dict) or doc.get("schema") != SCHEMA_VERSION:
            raise KeystoreConfigError(
                f"keystore file {self._path!r} has an unsupported schema — refusing "
                "to start (file preserved)"
            )
        entries_doc = doc.get("entries")
        if not isinstance(entries_doc, dict):
            raise KeystoreConfigError(f"keystore file {self._path!r} has no entries object")
        loaded: dict[str, _Entry] = {}
        failed = 0
        for seller, raw in entries_doc.items():
            entry = self._unwrap_entry(seller, raw)
            if entry is None:
                failed += 1
                continue
            loaded[seller] = entry
        if entries_doc and failed == len(entries_doc):
            # ALL unwrap failures (wrong KEK / tampered file) → fail closed;
            # NEVER start with an empty store that could later overwrite it.
            raise KeystoreConfigError(
                f"keystore file {self._path!r}: all {failed} entries failed to "
                "unwrap (wrong KEK or tampered file) — refusing to start "
                "(file preserved)"
            )
        if failed:
            logger.warning(
                "keystore: %d/%d entries failed to unwrap and are EXCLUDED "
                "(no values available); the next successful store persists "
                "the valid set",
                failed,
                len(entries_doc),
            )
        self._entries = loaded

    def _unwrap_entry(self, seller: str, raw: Any) -> _Entry | None:
        """Best-effort unwrap of one file entry; None on ANY failure (no
        key material ever surfaces)."""
        try:
            if not isinstance(raw, dict) or raw.get("v") != ENTRY_VERSION:
                return None
            upstream = raw["upstream_base_url"]
            fingerprint = raw["key_fingerprint"]
            catalog = raw["catalog_snapshot"]
            created = int(raw["created"])
            updated = int(raw["updated"])
            wrap_iv = b64url_decode_strict(raw["wrap"]["iv"], 12, "keystore")
            wrap_ct = b64url_decode_strict(raw["wrap"]["ct"], None, "keystore")
            enc_iv = b64url_decode_strict(raw["enc"]["iv"], 12, "keystore")
            enc_ct = b64url_decode_strict(raw["enc"]["ct"], None, "keystore")
            if (
                not isinstance(upstream, str)
                or not upstream
                or not isinstance(fingerprint, str)
                or len(fingerprint) != 64
                or not isinstance(catalog, list)
                or not all(
                    isinstance(e, dict)
                    and isinstance(e.get("model"), str)
                    and isinstance(e.get("servable"), bool)
                    for e in catalog
                )
            ):
                return None
            meta = self._meta(
                seller,
                _Entry(
                    created=created,
                    updated=updated,
                    upstream_base_url=upstream,
                    key_fingerprint=fingerprint,
                    catalog=catalog,
                    wrap_iv=b"",
                    wrap_ct=b"",
                    enc_iv=b"",
                    enc_ct=b"",
                ),
            )
            dek = _gcm_decrypt(self._kek, wrap_iv, wrap_ct, _wrap_aad(meta))
            api_key = _gcm_decrypt(dek, enc_iv, enc_ct, _enc_aad(meta)).decode("ascii")
            # Defense in depth: the fingerprint is inside the AAD already;
            # verify the payload actually matches it.
            if hashlib.sha256(api_key.encode("ascii")).hexdigest() != fingerprint:
                return None
            return _Entry(
                created=created,
                updated=updated,
                upstream_base_url=upstream,
                key_fingerprint=fingerprint,
                catalog=catalog,
                wrap_iv=wrap_iv,
                wrap_ct=wrap_ct,
                enc_iv=enc_iv,
                enc_ct=enc_ct,
                dek=dek,
            )
        except Exception:
            return None

    # ------------------------------------------------------------------ read
    def get(self, seller: str) -> _Entry | None:
        return self._entries.get(seller.lower())

    def __len__(self) -> int:
        return len(self._entries)

    def unwrap_api_key(self, seller: str) -> str | None:
        """Plaintext api key for an entry (memory-only use; never logged)."""
        entry = self.get(seller)
        if entry is None:
            return None
        if entry.dek is not None:
            dek = entry.dek
        else:
            dek = _gcm_decrypt(
                self._kek, entry.wrap_iv, entry.wrap_ct, _wrap_aad(self._meta(seller, entry))
            )
            entry.dek = dek
        return _gcm_decrypt(dek, entry.enc_iv, entry.enc_ct, _enc_aad(self._meta(seller, entry))).decode(
            "ascii"
        )

    # ----------------------------------------------------------------- write
    def _serialize(self) -> dict[str, Any]:
        entries: dict[str, Any] = {}
        for seller, e in self._entries.items():
            entries[seller] = {
                "v": ENTRY_VERSION,
                "created": e.created,
                "updated": e.updated,
                "upstream_base_url": e.upstream_base_url,
                "key_fingerprint": e.key_fingerprint,
                "catalog_snapshot": e.catalog,
                "wrap": {"iv": b64url_encode(e.wrap_iv), "ct": b64url_encode(e.wrap_ct)},
                "enc": {"iv": b64url_encode(e.enc_iv), "ct": b64url_encode(e.enc_ct)},
            }
        return {"schema": SCHEMA_VERSION, "entries": entries}

    def _persist(self) -> None:
        """Atomic same-directory replace: 0600 temp + fsync + os.replace.
        Raises on ANY failure — the caller then leaves memory untouched."""
        directory = os.path.dirname(os.path.abspath(self._path)) or "."
        os.makedirs(directory, exist_ok=True)
        payload = json.dumps(self._serialize(), separators=(",", ":"), ensure_ascii=True).encode(
            "utf-8"
        )
        fd, tmp_path = tempfile.mkstemp(prefix=".keystore-", suffix=".tmp", dir=directory)
        try:
            os.fchmod(fd, 0o600)
            with os.fdopen(fd, "wb") as fh:
                fh.write(payload)
                fh.flush()
                os.fsync(fh.fileno())
            os.replace(tmp_path, self._path)
        except BaseException:
            try:
                os.close(fd)
            except OSError:
                pass
            try:
                os.unlink(tmp_path)
            except OSError:
                pass
            raise
        try:  # best-effort directory fsync (durable rename)
            dir_fd = os.open(directory, os.O_RDONLY)
            try:
                os.fsync(dir_fd)
            finally:
                os.close(dir_fd)
        except OSError:
            pass

    def set(
        self,
        seller: str,
        *,
        upstream_base_url: str,
        api_key: str,
        catalog: list[dict[str, Any]],
    ) -> _Entry:
        """Store/replace the seller's key (LAST write wins). A new random
        DEK + IVs are minted every call. The file is persisted FIRST; the
        memory map commits only on success — any failure preserves the old
        valid key everywhere."""
        seller = seller.lower()
        now = int(time.time())
        existing = self._entries.get(seller)
        created = existing.created if existing else now
        entry = _Entry(
            created=created,
            updated=now,
            upstream_base_url=upstream_base_url,
            key_fingerprint=hashlib.sha256(api_key.encode("ascii")).hexdigest(),
            catalog=catalog,
            wrap_iv=secrets.token_bytes(12),
            wrap_ct=b"",
            enc_iv=secrets.token_bytes(12),
            enc_ct=b"",
        )
        dek = secrets.token_bytes(32)
        wrap_ct = _gcm_encrypt(self._kek, entry.wrap_iv, dek, _wrap_aad(self._meta(seller, entry)))
        enc_ct = _gcm_encrypt(dek, entry.enc_iv, api_key.encode("ascii"), _enc_aad(self._meta(seller, entry)))
        new_entry = _Entry(
            created=created,
            updated=now,
            upstream_base_url=upstream_base_url,
            key_fingerprint=entry.key_fingerprint,
            catalog=catalog,
            wrap_iv=entry.wrap_iv,
            wrap_ct=wrap_ct,
            enc_iv=entry.enc_iv,
            enc_ct=enc_ct,
            dek=dek,
        )
        previous = self._entries.get(seller)
        self._entries[seller] = new_entry  # staged for serialization
        try:
            self._persist()
        except BaseException:
            if previous is None:
                self._entries.pop(seller, None)  # rollback
            else:
                self._entries[seller] = previous  # rollback
            raise
        return new_entry

    def remove(self, seller: str) -> bool:
        """Idempotent revoke: True (and a persisted rewrite) only when an
        entry existed; absent → False, file untouched."""
        seller = seller.lower()
        if seller not in self._entries:
            return False
        previous = self._entries.pop(seller)
        try:
            self._persist()
        except BaseException:
            self._entries[seller] = previous  # rollback
            raise
        return True
