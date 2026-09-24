"""Environment configuration for the buyer CLI.

Required env (PIN `m3-m5-e2e.md` — 「接口契约 PIN」):
  BUYER_PRIVATE_KEY / RPC_URL / CHAIN_ID / ESCROW_ADDR / REGISTRY_ADDR / USDC_ADDR

Source code contains ZERO hardcoded addresses or chain ids — everything is
injected through these variables. The private key is loaded but never logged.

BREAKING (M10): the optional LISTINGS_FROM_BLOCK env key was REMOVED —
`listings` now discovers sellers via the Registry v3 on-chain enumeration
(sellerCount/getSellers) instead of an eth_getLogs Registered-event scan,
so there is no block range to configure anymore. Setting the variable has
no effect.
"""

from dataclasses import dataclass
import os
import re

from eth_utils import to_checksum_address

from .errors import EnvError

REQUIRED_VARS = (
    "BUYER_PRIVATE_KEY",
    "RPC_URL",
    "CHAIN_ID",
    "ESCROW_ADDR",
    "REGISTRY_ADDR",
    "USDC_ADDR",
)

# Optional extra (not part of the PIN): default seller for `call`.
SELLER_ADDR_VAR = "SELLER_ADDR"

_ADDR_RE = re.compile(r"^0x[0-9a-fA-F]{40}$")


@dataclass(frozen=True)
class EnvConfig:
    private_key: str
    rpc_url: str
    chain_id: int
    escrow_addr: str
    registry_addr: str
    usdc_addr: str

    @property
    def private_key_hex(self) -> str:
        return self.private_key if self.private_key.startswith("0x") else "0x" + self.private_key


def _read(name: str) -> str | None:
    value = os.environ.get(name)
    if value is None:
        return None
    value = value.strip()
    return value or None


def _require_addr(name: str, value: str | None) -> str:
    if value is None:
        return ""
    try:
        return to_checksum_address(value)
    except Exception as exc:  # bad hex / wrong length
        raise EnvError(f"env {name}={value!r} is not a valid Ethereum address") from exc


def load_config() -> EnvConfig:
    """Read and validate the required env vars.

    Raises EnvError listing every missing variable in one message, so the
    command entry points give a single clear error.
    """
    missing = [name for name in REQUIRED_VARS if _read(name) is None]
    if missing:
        raise EnvError(
            "missing required environment variable(s): "
            + ", ".join(missing)
            + " — set BUYER_PRIVATE_KEY / RPC_URL / CHAIN_ID / ESCROW_ADDR / "
            "REGISTRY_ADDR / USDC_ADDR before running this command"
        )

    private_key = _read("BUYER_PRIVATE_KEY") or ""
    try:
        from eth_account import Account

        Account.from_key(private_key)
    except Exception:
        raise EnvError("env BUYER_PRIVATE_KEY is not a valid private key (value withheld)")
    chain_id_raw = _read("CHAIN_ID") or ""
    try:
        chain_id = int(chain_id_raw, 10)
        if chain_id < 0:
            raise ValueError
    except ValueError as exc:
        raise EnvError(f"env CHAIN_ID={chain_id_raw!r} is not a valid non-negative integer") from exc

    return EnvConfig(
        private_key=private_key,
        rpc_url=_read("RPC_URL") or "",
        chain_id=chain_id,
        escrow_addr=_require_addr("ESCROW_ADDR", _read("ESCROW_ADDR")),
        registry_addr=_require_addr("REGISTRY_ADDR", _read("REGISTRY_ADDR")),
        usdc_addr=_require_addr("USDC_ADDR", _read("USDC_ADDR")),
    )


def load_seller_override() -> str | None:
    """Optional SELLER_ADDR env (not required by the PIN)."""
    value = _read(SELLER_ADDR_VAR)
    if value is None:
        return None
    return _require_addr(SELLER_ADDR_VAR, value)
