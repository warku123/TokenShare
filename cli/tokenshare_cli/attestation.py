"""Best-effort OFF-CHAIN parsing for dstack/Phala TDX attestation quotes (M7-A).

Scope discipline: without the dstack-verifier docker image (or the Phala
cloud-api verify endpoint) the quote's ECDSA signature chain (PCK certs,
MRTD/RTMR collateral) CANNOT be verified cryptographically. This module does
structural parsing only:

- SGX ECDSA quote v4 header fields (version / att_key_type / qe_svn / pce_svn)
  — fixed 48-byte header layout, widely documented;
- the report-data address binding: the relay's /attestation binds the
  TEE-derived seller address as 44 zero bytes + 20 address bytes inside the
  64-byte report data; the CLI locates that pattern (offset-independent, so
  it works even if the exact TDX body layout differs between quote formats);
- the anchor digest: keccak256(raw quote bytes) — the SAME convention the
  relay reports in /attestation.quoteDigest and README-docker.md tells the
  seller to anchor via AttestationAnchor.anchor(bytes32).

Full verification is out of scope here and clearly labeled in the CLI output.
"""

from dataclasses import dataclass
import json

from eth_utils import keccak, to_checksum_address

from .errors import TokenshareError

# SGX ECDSA quote v4 header (48 bytes):
#   version u16 @0 | att_key_type u16 @2 | att_key_data_0 u32 @4 |
#   reserved u32 @8 | qe_svn u16 @12 | pce_svn u16 @14 |
#   vendor_id 16B @16 | user_data 16B @32
_HEADER_SIZE = 48
_TEE_KEY_PATH_HINT = "wallet/ethereum/tokenshare"

# Binding: 44 zero bytes followed by 20 NON-zero address bytes (64B total).
_BINDING_PREFIX = b"\x00" * 44


class AttestationError(TokenshareError):
    """Quote payload could not be decoded or parsed."""


@dataclass(frozen=True)
class QuoteSummary:
    length: int
    version: int | None
    att_key_type: int | None
    qe_svn: int | None
    pce_svn: int | None
    report_data: str | None       # "0x" + 128 hex chars (64 bytes), or None
    derived_address: str | None   # checksummed, or None when no binding found
    binding_offset: int | None
    digest: str                   # keccak256(raw quote bytes), "0x…"


def _strip_hex(text: str) -> bytes:
    value = text.strip()
    if value.startswith("0x") or value.startswith("0X"):
        value = value[2:]
    try:
        return bytes.fromhex(value)
    except ValueError as exc:
        raise AttestationError("quote is not valid hex (0x prefix optional)") from exc


def _binding_offsets(quote: bytes) -> list[int]:
    """Offsets where 44 zero bytes are followed by 20 nonzero-ish bytes — the
    report-data layout /attestation binds the seller address into.

    TDX reports have reserved zero-padding before report_data, so several
    overlapping windows can satisfy the prefix; callers rank candidates by
    how "address-like" the 20-byte suffix is (nonzero count) and take the
    last maximal one.
    """
    offsets: list[int] = []
    start = 0
    while True:
        idx = quote.find(_BINDING_PREFIX, start)
        if idx < 0 or idx + 64 > len(quote):
            break
        addr = quote[idx + 44 : idx + 64]
        if addr != b"\x00" * 20 and addr != b"\xff" * 20:
            offsets.append(idx)
        start = idx + 1
    return offsets


def _u16(quote: bytes, offset: int) -> int | None:
    if len(quote) < offset + 2:
        return None
    return int.from_bytes(quote[offset : offset + 2], "little")


def _address_from_binding(binding64: bytes) -> str | None:
    """Checksummed address from a 64-byte binding (44 zeros + 20 bytes), or
    None when the bytes do not match the binding layout."""
    if len(binding64) != 64 or binding64[:44] != _BINDING_PREFIX:
        return None
    addr = binding64[44:]
    if addr == b"\x00" * 20 or addr == b"\xff" * 20:
        return None
    return to_checksum_address(addr)


def parse_quote(raw: str) -> QuoteSummary:
    """Parse a quote payload. Accepts:
    - raw hex (0x optional) of the DCAP quote bytes;
    - the relay's /attestation JSON (fields: quote, reportData,
      derivedAddress, appId) — reportData is authoritative when present.

    Raises AttestationError on undecodable payloads.
    """
    text = (raw or "").strip()
    if not text:
        raise AttestationError("empty quote payload")

    json_report_data: bytes | None = None
    json_address: str | None = None
    if text.startswith("{"):
        try:
            payload = json.loads(text)
        except ValueError as exc:
            raise AttestationError("payload looks like JSON but is invalid") from exc
        if not isinstance(payload, dict) or not str(payload.get("quote") or "").strip():
            raise AttestationError("JSON payload has no 'quote' field (expected the /attestation response)")
        raw_report = str(payload.get("reportData") or "").strip()
        if raw_report:
            try:
                report = _strip_hex(raw_report)
            except AttestationError:
                report = b""
            if len(report) == 64:
                json_report_data = report
        raw_addr = str(payload.get("derivedAddress") or "").strip()
        if raw_addr.lower().startswith("0x") and len(raw_addr) == 42:
            try:
                json_address = to_checksum_address(raw_addr)
            except Exception:
                json_address = None
        quote_bytes = _strip_hex(str(payload["quote"]))
    else:
        quote_bytes = _strip_hex(text)

    if not quote_bytes:
        raise AttestationError("quote hex decodes to zero bytes")

    # ---- report-data binding --------------------------------------------
    derived_address: str | None = None
    binding_offset: int | None = None
    report_data: bytes | None = None

    if json_report_data is not None:
        report_data = json_report_data
        derived_address = _address_from_binding(json_report_data) or json_address
    else:
        matches = _binding_offsets(quote_bytes)
        if matches:
            # Rank candidates: the true binding window's "address" part is all
            # 20 nonzero bytes, while windows sliding over the zero padding
            # produce mostly-zero suffixes; the LAST maximal candidate wins
            # (reserved zero-padding precedes report_data).
            binding_offset = max(
                matches,
                key=lambda idx: (
                    sum(1 for b in quote_bytes[idx + 44 : idx + 64] if b),
                    idx,
                ),
            )
            report_data = quote_bytes[binding_offset : binding_offset + 64]
            derived_address = _address_from_binding(report_data)
    if derived_address is None and json_address is not None:
        derived_address = json_address  # JSON field without parseable reportData

    return QuoteSummary(
        length=len(quote_bytes),
        version=_u16(quote_bytes, 0),
        att_key_type=_u16(quote_bytes, 2),
        qe_svn=_u16(quote_bytes, 12),
        pce_svn=_u16(quote_bytes, 14),
        report_data="0x" + report_data.hex() if report_data is not None else None,
        derived_address=derived_address,
        binding_offset=binding_offset,
        digest="0x" + keccak(quote_bytes).hex(),
    )


def read_anchor_digest(rpc_url: str, anchor_addr: str, app_id: str) -> str:
    """Read AttestationAnchor.digests(appId) via a read-only RPC call."""
    from web3 import Web3

    from .abis import ANCHOR_ABI

    w3 = Web3(Web3.HTTPProvider(rpc_url))
    contract = w3.eth.contract(
        address=Web3.to_checksum_address(anchor_addr), abi=ANCHOR_ABI
    )
    digest = contract.functions.digests(Web3.to_checksum_address(app_id)).call()
    return "0x" + bytes(digest).hex()
