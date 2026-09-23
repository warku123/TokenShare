"""Typed errors for the TokenShare buyer CLI.

The CLI turns these into clean, actionable stderr messages; they never carry
secrets (private keys are never echoed back in any message).
"""


class TokenshareError(Exception):
    """Base class for all expected CLI errors."""


class EnvError(TokenshareError):
    """Required environment variables are missing or malformed."""


class ConfigError(TokenshareError):
    """Configuration value is malformed (bad address, bad chain id, ...)."""


class RelayError(TokenshareError):
    """The relay returned a non-2xx response."""

    def __init__(self, status_code: int, body: str) -> None:
        self.status_code = status_code
        self.body = body
        super().__init__(f"relay HTTP {status_code}: {body}")


class ReceiptError(TokenshareError):
    """The X-Receipt payload could not be decoded or failed verification."""
