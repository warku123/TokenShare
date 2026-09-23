"""Shared fixtures for the TokenShare buyer CLI tests.

No real chain and no real relay: chain access and HTTP are monkeypatched;
signing / receipt verification run against eth_account directly (pure).
"""

import base64
import json
import pathlib
import sys

# Make `cli/` (parent of the tokenshare_cli package) importable regardless of
# the invocation cwd — e.g. `python3 -m pytest cli/tests -q` from the repo root.
_HERE = pathlib.Path(__file__).resolve().parent
_CLI_ROOT = _HERE.parent
for _p in (str(_CLI_ROOT),):
    if _p not in sys.path:
        sys.path.insert(0, _p)

import pytest  # noqa: E402
from eth_account import Account  # noqa: E402
from typer.testing import CliRunner  # noqa: E402

from tokenshare_cli.signing import receipt_types  # noqa: E402

BUYER_KEY = "0x" + "11" * 32
SELLER_KEY = "0x" + "22" * 32
OTHER_KEY = "0x" + "33" * 32
CHAIN_ID = 31337

ESCROW_ADDR = "0x" + "aa" * 20
REGISTRY_ADDR = "0x" + "bb" * 20
USDC_ADDR = "0x" + "cc" * 20

REQUIRED_ENV_VARS = ("BUYER_PRIVATE_KEY", "RPC_URL", "CHAIN_ID", "ESCROW_ADDR", "REGISTRY_ADDR", "USDC_ADDR")


def addr_of(key: str) -> str:
    return Account.from_key(key).address


@pytest.fixture()
def buyer_env(monkeypatch):
    """Set the full PIN env for a buyer."""
    monkeypatch.setenv("BUYER_PRIVATE_KEY", BUYER_KEY)
    monkeypatch.setattr(
        "tokenshare_cli.config.load_seller_override", lambda: Account.from_key(SELLER_KEY).address
    )
    yield
    monkeypatch.delenv("BUYER_PRIVATE_KEY", raising=False)


def all_output(result) -> str:
    """stdout + stderr of a CliRunner result (click 8.x keeps them apart)."""
    out = result.output or ""
    try:
        err = result.stderr or ""
    except Exception:
        err = ""
    return out + err


def make_receipt(signer_key: str, payment_id: int, seller_addr: str, chain_id: int = CHAIN_ID, **overrides) -> dict:
    """Build a relay-shaped receipt {domain, message, signature}."""
    domain = {"name": "TokenShare Relay", "version": "1", "chainId": chain_id}
    message = {
        "paymentId": int(payment_id),
        "promptTokens": 10,
        "cachedTokens": 0,
        "completionTokens": 5,
        "actualAmount": 7,
        "seller": seller_addr,
        "upstreamHost": "api.moonshot.cn",
        "model": "kimi-k2.6",
    }
    message.update(overrides)
    types = receipt_types()
    acct = Account.from_key(signer_key)
    signed = acct.sign_typed_data(domain, types, message)
    return {"domain": domain, "message": message, "signature": signed.signature.hex()}


def receipt_header(payload: dict) -> str:
    """Base64url JSON of a receipt payload (unpadded), as the relay sends it."""
    raw = json.dumps(payload, separators=(",", ":")).encode("utf-8")
    return base64.urlsafe_b64encode(raw).decode("ascii").rstrip("=")
