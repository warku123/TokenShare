"""USDC unit helpers.

All on-chain amounts are native USDC units (USDC has 6 decimals, so
1 USDC == 1_000_000 native units). This module converts between human
input ("10.5") and native integers, and formats amounts for display.
"""

from decimal import Decimal, InvalidOperation

from .errors import ConfigError

USDC_DECIMALS = 6
_SCALE = Decimal(10**USDC_DECIMALS)


def parse_usdc_amount(text: str, what: str = "amount") -> int:
    """Parse a human decimal string (e.g. "10.5") into native 6dp units.

    Raises ConfigError on malformed input or more than 6 decimal places.
    """
    try:
        value = Decimal(text.strip())
    except InvalidOperation as exc:
        raise ConfigError(f"invalid {what} {text!r}: not a number") from exc
    if not value.is_finite() or value < 0:
        raise ConfigError(f"invalid {what} {text!r}: must be a finite, non-negative number")
    native = value * _SCALE
    units = int(native)
    if native != units:
        raise ConfigError(
            f"invalid {what} {text!r}: USDC only supports {USDC_DECIMALS} decimal places"
        )
    return units


def format_usdc(native: int) -> str:
    """Humanize native units back to a decimal string ("15000000" -> "15")."""
    d = Decimal(int(native)) / _SCALE
    text = format(d, "f")
    if "." in text:
        text = text.rstrip("0").rstrip(".")
    return text or "0"


def display_usdc(native: int) -> str:
    """Display string: native integer plus humanized conversion, per spec."""
    return f"{native} native (= {format_usdc(native)} USDC)"
