"""Pure, IO-free Intel TDX quote parsing (P1 TEE proof completion).

No sockets, no files, no clocks — the functions below only slice bytes so
they can run inside the /attestation handler without any failure mode that
would turn into a 500. Every entry point returns None instead of raising:
a quote we cannot parse is reported as null fields, never as an error page.

TDX quote v4 layout (Intel SGX/TDX QE quote format, attestation-key type
ECDSA_P256):

    header  (48 B, quote offset 0)
        0:  uint16  version          (4 for v4)
        2:  uint16  att_key_type     (2 = ECDSA_P256)
        4:  uint16  tee_type         (0x81 = TDX)
        6:  uint16  qe_svn
        8:  uint16  pce_svn
        12: 16 B    qe_vendor
        28: 20 B    user_data
    body    (584 B, TDREPORT, quote offset 48)
        48+0:    16 B   tee_tcb_svn
        48+16:   48 B   mrseam
        48+64:   48 B   mrsignerseam
        48+112:   8 B   seamattributes
        48+120:   8 B   tdattributes
        48+128:   8 B   xfam
        48+136:  48 B   mrtd
        48+184:  48 B   mr_config_id
        48+232:  48 B   mr_owner
        48+280:  48 B   mr_owner_config
        48+328:  48 B   rtmr0
        48+376:  48 B   rtmr1
        48+424:  48 B   rtmr2
        48+472:  48 B   rtmr3
        48+520:  64 B   reportdata

The offsets used here (mrseam 48+16, rtmr0-3 48+328/376/424/472, reportdata
48+520) were cross-checked field-by-field against a live Phala cloud
verifier report (quote from /attestation, values from the verifier's
quote.body): rtmr0-3, mrseam and reportdata matched byte-for-byte.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

# TDX quote v4 constants.
QUOTE_HEADER_LEN = 48
TDX_BODY_LEN = 584
#: The fixed-length prefix a v4 parse needs (header + body). The tail after
#: it (signature + cert data) is variable-length and never touched here.
TDX_QUOTE_V4_LEN = QUOTE_HEADER_LEN + TDX_BODY_LEN  # 632

QUOTE_VERSION_OFF = 0  # uint16 LE
V4_VERSION = 4

# Field offsets relative to the START of the quote (header included).
MRSEAM_OFF = QUOTE_HEADER_LEN + 16  # 64
RTMR0_OFF = QUOTE_HEADER_LEN + 328  # 376
RTMR1_OFF = QUOTE_HEADER_LEN + 376  # 424
RTMR2_OFF = QUOTE_HEADER_LEN + 424  # 472
RTMR3_OFF = QUOTE_HEADER_LEN + 472  # 520
REPORTDATA_OFF = QUOTE_HEADER_LEN + 520  # 568

MRSEAM_LEN = 48
RTMR_LEN = 48
REPORTDATA_LEN = 64

# The exact field set exposed by both /attestation ("quoteBody") and
# /attestation/verify ("quote_body") — one shape, two sources.
QUOTE_BODY_FIELDS: tuple[str, ...] = (
    "reportdata",
    "rtmr0",
    "rtmr1",
    "rtmr2",
    "rtmr3",
    "mrseam",
)


@dataclass(frozen=True)
class TdxQuoteFields:
    """Parsed fields of a TDX v4 quote (all values 0x-prefixed lowercase
    hex, the face the verifier reports and the front-end compares)."""

    version: int
    reportdata: str
    rtmr0: str
    rtmr1: str
    rtmr2: str
    rtmr3: str
    mrseam: str

    def as_body(self) -> dict[str, str]:
        """The shared quoteBody/quote_body JSON shape."""
        return {field: getattr(self, field) for field in QUOTE_BODY_FIELDS}


def _to_bytes(quote: Any) -> bytes | None:
    """Accept bytes/bytearray or a hex string (0x-prefixed or not).
    Anything else → None (never raises)."""
    if isinstance(quote, (bytes, bytearray)):
        return bytes(quote)
    if isinstance(quote, str):
        text = quote.strip()
        if text[:2].lower() == "0x":
            text = text[2:]
        try:
            return bytes.fromhex(text)
        except ValueError:
            return None
    return None


def _hex_field(quote: bytes, off: int, length: int) -> str:
    return "0x" + quote[off : off + length].hex()


def parse_tdx_quote(quote: Any) -> TdxQuoteFields | None:
    """Parse reportdata/rtmr0-3/mrseam out of a TDX quote v4.

    Returns None for anything that is not a v4 quote of at least the fixed
    632 B header+body prefix: short/empty input, non-hex strings, wrong
    version. Callers map None to null fields — no exception ever escapes.
    """
    raw = _to_bytes(quote)
    if raw is None or len(raw) < TDX_QUOTE_V4_LEN:
        return None
    version = int.from_bytes(raw[QUOTE_VERSION_OFF : QUOTE_VERSION_OFF + 2], "little")
    if version != V4_VERSION:
        return None
    try:
        return TdxQuoteFields(
            version=version,
            reportdata=_hex_field(raw, REPORTDATA_OFF, REPORTDATA_LEN),
            rtmr0=_hex_field(raw, RTMR0_OFF, RTMR_LEN),
            rtmr1=_hex_field(raw, RTMR1_OFF, RTMR_LEN),
            rtmr2=_hex_field(raw, RTMR2_OFF, RTMR_LEN),
            rtmr3=_hex_field(raw, RTMR3_OFF, RTMR_LEN),
            mrseam=_hex_field(raw, MRSEAM_OFF, MRSEAM_LEN),
        )
    except (ValueError, IndexError):  # defensive: never raise out of a route
        return None


def parse_quote_body(quote: Any) -> dict[str, str] | None:
    """The shared quoteBody shape, or None when the quote is unparseable."""
    parsed = parse_tdx_quote(quote)
    return parsed.as_body() if parsed is not None else None


def parse_quote_version(quote: Any) -> int | None:
    """The quote's version (4 for TDX v4), or None when unparseable."""
    parsed = parse_tdx_quote(quote)
    return parsed.version if parsed is not None else None
