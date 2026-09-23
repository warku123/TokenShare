"""Environment configuration for the TokenShare seller relay.

Every chain-specific value (RPC, chainId, contract addresses) is injected via
environment variables — zero hardcoding in source (BUILD_SPEC §2.5). Missing
required configuration aborts startup immediately; there is NO degraded mode.

All amounts in this codebase are native USDC units (6 decimal places,
1 USDC = 1_000_000).
"""

from __future__ import annotations

import logging
import os
from dataclasses import dataclass
from typing import Final
from urllib.parse import urlparse

from eth_account import Account

logger = logging.getLogger("tokenshare.relay")

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
ENV_ALLOW_CUSTOM_UPSTREAM = "ALLOW_CUSTOM_UPSTREAM"
ENV_VERIFY_UPSTREAM_ON_START = "VERIFY_UPSTREAM_ON_START"
ENV_CORS_ORIGINS = "RELAY_CORS_ORIGINS"

DEFAULT_OPENAI_BASE_URL = "https://api.openai.com"
DEFAULT_FORWARD_MARGIN_S = 120
DEFAULT_PORT = 8787
DEFAULT_PROMPT_TOKEN_CAP = 200_000
DEFAULT_COMPLETION_TOKEN_CAP = 32_000
# Demo default: any origin may call the relay. Production MUST pin the
# front-end's own domain list via RELAY_CORS_ORIGINS instead.
DEFAULT_CORS_ORIGINS: Final[tuple[str, ...]] = ("*",)

# ---------------------------------------------------------------------------
# Official-endpoint-only upstream policy (anti-poisoning, 2026-09-23 ruling).
#
# Product positioning = renting out IDLE QUOTA OF OFFICIAL SUBSCRIPTION
# PLANS, so the relay's upstream must be an OFFICIAL model-provider endpoint
# and the served models must be official-plan models. A seller who points
# the relay at an attacker-controlled "upstream" gets fake models/fake usage
# — hence the startup allowlist below.
#
# P0 set — extendable: add new provider hosts HERE (host -> provider) plus
# the model prefixes in MODEL_PROVIDER_PREFIXES; no other code changes.
# ---------------------------------------------------------------------------
OFFICIAL_UPSTREAM_HOSTS: Final[frozenset[str]] = frozenset(
    {
        "api.openai.com",  # OpenAI
        "api.moonshot.cn",  # Kimi / Moonshot domestic
        "api.moonshot.ai",  # Kimi / Moonshot international (keys not interchangeable with .cn)
        "api.kimi.com",  # Kimi Coding plan (subscription coding quota; base
        #                   https://api.kimi.com/coding/v1 — SDK-style value,
        #                   the relay strips the trailing /v1 before gating).
    }
)

# host -> provider id (the provider identity used for model consistency).
OFFICIAL_HOST_PROVIDER: Final[dict[str, str]] = {
    "api.openai.com": "openai",
    "api.moonshot.cn": "moonshot",
    "api.moonshot.ai": "moonshot",
    "api.kimi.com": "moonshot",
}

# Official catalog model prefixes per provider. UNKNOWN prefixes => the model
# is not an official-plan model (provider_for_model returns None). The relay
# 400s such models instead of forwarding them to an official upstream.
# "k3" covers the Kimi Coding plan model face: k3 / k3-256k
# (kimi-for-coding / kimi-for-coding-highspeed already match "kimi-").
MODEL_PROVIDER_PREFIXES: Final[dict[str, tuple[str, ...]]] = {
    "openai": ("gpt-", "o1", "o3", "o4", "chatgpt-"),
    "moonshot": ("kimi-", "moonshot-", "k3"),
}


def provider_for_model(model_name: Any) -> str | None:
    """Provider identity of an official-catalog model name (prefix match on
    the single host-recognized prefixes). Unknown non-official prefixes (and
    non-strings) return None — the caller must reject, never guess."""
    if not isinstance(model_name, str):
        return None
    name = model_name.strip()
    if not name:
        return None
    for provider, prefixes in MODEL_PROVIDER_PREFIXES.items():
        for prefix in prefixes:
            if name.startswith(prefix):
                return provider
    return None


def host_provider(base_url: str) -> str | None:
    """Provider of a base URL's host, or None when the host is not official.

    Accepts the full (already normalized) base URL, e.g.
    "https://api.moonshot.cn" -> "moonshot"; "http://127.0.0.1:9"
    -> None (custom upstream — dev/test only)."""
    try:
        host = (urlparse(base_url).hostname or "").lower()
    except ValueError:
        return None
    return OFFICIAL_HOST_PROVIDER.get(host)


def parse_cors_origins(raw: str | None) -> list[str]:
    """Parse RELAY_CORS_ORIGINS: a comma-separated origin list, entries
    stripped, blank entries dropped. Unset/blank -> ["*"] — the DEMO default
    (any origin); production must pin the front-end's own domain list."""
    if raw is None or not raw.strip():
        return list(DEFAULT_CORS_ORIGINS)
    origins = [entry.strip() for entry in raw.split(",")]
    return [entry for entry in origins if entry]


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
    verify_upstream_on_start: bool

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

    base_url = _normalize_openai_base_url(
        os.environ.get(ENV_OPENAI_BASE_URL, DEFAULT_OPENAI_BASE_URL)
    )
    # Anti-poisoning startup gate: only OFFICIAL provider endpoints may serve
    # as the upstream. A custom host would let a seller (or a tampered config)
    # serve fake models and fake usage — the exact scenario this ruling
    # forbids. Custom upstreams remain reachable ONLY with an explicit
    # ALLOW_CUSTOM_UPSTREAM=1 (dev/test, e.g. the e2e mock).
    if host_provider(base_url) is None:
        if os.environ.get(ENV_ALLOW_CUSTOM_UPSTREAM, "").strip() == "1":

            logger.warning(
                "custom upstream enabled — dev/test only, authenticity guarantee void "
                "(OPENAI_BASE_URL host is not in OFFICIAL_UPSTREAM_HOSTS)"
            )
        else:
            raise ConfigError(
                f"{ENV_OPENAI_BASE_URL}={base_url!r} is not an official model-provider "
                "endpoint. TokenShare rents out idle quota of OFFICIAL subscription "
                f"plans, so the upstream host must be one of {sorted(OFFICIAL_UPSTREAM_HOSTS)} "
                "— this is the anti-poisoning guarantee that requests hit official "
                "models on official endpoints. For local development and tests only "
                "(e.g. the e2e mock upstream), set ALLOW_CUSTOM_UPSTREAM=1 explicitly; "
                "that voids the authenticity guarantee."
            )

    return Config(
        seller_key=seller_key,
        rpc_url=_require(ENV_RPC_URL),
        chain_id=chain_id,
        escrow_addr=_require(ENV_ESCROW_ADDR),
        registry_addr=_require(ENV_REGISTRY_ADDR),
        usdc_addr=_require(ENV_USDC_ADDR),
        openai_api_key=_require(ENV_OPENAI_API_KEY),
        openai_base_url=base_url,
        forward_margin_s=_optional_int(ENV_FORWARD_MARGIN_S, DEFAULT_FORWARD_MARGIN_S),
        port=_optional_int(ENV_PORT, DEFAULT_PORT, minimum=1),
        prompt_token_cap=_optional_int(ENV_PROMPT_TOKEN_CAP, DEFAULT_PROMPT_TOKEN_CAP),
        completion_token_cap=_optional_int(
            ENV_COMPLETION_TOKEN_CAP, DEFAULT_COMPLETION_TOKEN_CAP
        ),
        # Startup key probe: enabled unless explicitly disabled with "0"
        # (official-host boots only; custom/mock upstreams never probe).
        verify_upstream_on_start=(
            os.environ.get(ENV_VERIFY_UPSTREAM_ON_START, "1").strip() != "0"
        ),
    )
