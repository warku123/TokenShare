"""Environment configuration for the TokenShare seller relay.

Every chain-specific value (RPC, chainId, contract addresses) is injected via
environment variables — zero hardcoding in source (BUILD_SPEC §2.5). Missing
required configuration aborts startup immediately; there is NO degraded mode.

All amounts in this codebase are native USDC units (6 decimal places,
1 USDC = 1_000_000).
"""

from __future__ import annotations

import os
from dataclasses import dataclass

from eth_account import Account

# Env var names — the single source of truth (contract PIN §env).
ENV_SELLER_KEY = "RELAY_SELLER_KEY"
ENV_RPC_URL = "RPC_URL"
ENV_CHAIN_ID = "CHAIN_ID"
ENV_ESCROW_ADDR = "ESCROW_ADDR"
ENV_REGISTRY_ADDR = "REGISTRY_ADDR"
ENV_USDC_ADDR = "USDC_ADDR"
ENV_OPENAI_API_KEY = "OPENAI_API_KEY"
ENV_OPENAI_BASE_URL = "OPENAI_BASE_URL"
ENV_FORWARD_MARGIN_S = "FORWARD_MARGIN_S"
ENV_PORT = "PORT"
ENV_PROMPT_TOKEN_CAP = "PROMPT_TOKEN_CAP"
ENV_COMPLETION_TOKEN_CAP = "COMPLETION_TOKEN_CAP"

DEFAULT_OPENAI_BASE_URL = "https://api.openai.com"
DEFAULT_FORWARD_MARGIN_S = 120
DEFAULT_PORT = 8787
DEFAULT_PROMPT_TOKEN_CAP = 200_000
DEFAULT_COMPLETION_TOKEN_CAP = 32_000


class ConfigError(RuntimeError):
    """Raised when required configuration is missing or invalid."""


def _require(name: str) -> str:
    value = os.environ.get(name)
    if not value or not value.strip():
        raise ConfigError(
            f"Missing required environment variable {name!r}; refusing to start "
            f"(no degraded mode)."
        )
    return value.strip()


def _optional_int(name: str, default: int, minimum: int = 0) -> int:
    raw = os.environ.get(name)
    if raw is None or not raw.strip():
        return default
    try:
        value = int(raw.strip())
    except ValueError as exc:
        raise ConfigError(f"Invalid integer for {name!r}: {raw!r}") from exc
    if value < minimum:
        raise ConfigError(f"{name!r} must be >= {minimum}, got {value}")
    return value


@dataclass(frozen=True)
class Config:
    # Required.
    seller_key: str
    rpc_url: str
    chain_id: int
    escrow_addr: str
    registry_addr: str
    usdc_addr: str
    openai_api_key: str

    # Optional with defaults.
    openai_base_url: str
    forward_margin_s: int
    port: int
    prompt_token_cap: int
    completion_token_cap: int

    @property
    def seller_address(self) -> str:
        """Address derived from RELAY_SELLER_KEY (the on-chain seller)."""
        return Account.from_key(self.seller_key).address


def _normalize_openai_base_url(raw: str) -> str:
    """Normalize the OpenAI base URL to a host-root form.

    `main.py` forwards with absolute paths (`/v1/chat/completions`), so the
    base_url must be the host root — otherwise httpx merges the path and
    produces `.../v1/v1/chat/completions` (upstream 404). Accept SDK-style
    user input (`.../v1`, `.../v1/`) by stripping the trailing `/v1`.
    """
    url = raw.strip()
    if not url:
        return DEFAULT_OPENAI_BASE_URL
    url = url.rstrip("/")
    if url.endswith("/v1"):
        url = url[: -len("/v1")]
    return url or DEFAULT_OPENAI_BASE_URL


def load_config() -> Config:
    """Assemble configuration from the environment. Raise ConfigError on any
    missing required variable — callers must let the process exit non-zero."""
    try:
        chain_id = int(_require(ENV_CHAIN_ID))
    except ValueError as exc:
        raise ConfigError(f"{ENV_CHAIN_ID!r} must be an integer") from exc

    seller_key = _require(ENV_SELLER_KEY)
    if not seller_key.startswith("0x") or len(seller_key) != 66:
        raise ConfigError(
            f"{ENV_SELLER_KEY!r} must be a 0x-prefixed 32-byte hex private key"
        )
    try:
        # Validate the key parses; never log or print it.
        Account.from_key(seller_key)
    except Exception as exc:
        raise ConfigError(f"{ENV_SELLER_KEY!r} is not a valid private key") from exc

    for name in (ENV_ESCROW_ADDR, ENV_REGISTRY_ADDR, ENV_USDC_ADDR):
        addr = _require(name)
        if not addr.lower().startswith("0x") or len(addr) != 42:
            raise ConfigError(f"{name!r} must be a 0x-prefixed 20-byte address")

    return Config(
        seller_key=seller_key,
        rpc_url=_require(ENV_RPC_URL),
        chain_id=chain_id,
        escrow_addr=_require(ENV_ESCROW_ADDR),
        registry_addr=_require(ENV_REGISTRY_ADDR),
        usdc_addr=_require(ENV_USDC_ADDR),
        openai_api_key=_require(ENV_OPENAI_API_KEY),
        openai_base_url=_normalize_openai_base_url(
            os.environ.get(ENV_OPENAI_BASE_URL, DEFAULT_OPENAI_BASE_URL)
        ),
        forward_margin_s=_optional_int(ENV_FORWARD_MARGIN_S, DEFAULT_FORWARD_MARGIN_S),
        port=_optional_int(ENV_PORT, DEFAULT_PORT, minimum=1),
        prompt_token_cap=_optional_int(ENV_PROMPT_TOKEN_CAP, DEFAULT_PROMPT_TOKEN_CAP),
        completion_token_cap=_optional_int(
            ENV_COMPLETION_TOKEN_CAP, DEFAULT_COMPLETION_TOKEN_CAP
        ),
    )
