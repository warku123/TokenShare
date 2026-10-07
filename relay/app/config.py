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
from typing import Any, Final
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
        "opencode.ai",  # OpenCode Zen platform (OpenAI-compatible; base
        #                 https://opencode.ai/zen/v1 — SDK-style value, the
        #                 relay strips the trailing /v1 before gating).
        "token-plan.maas.qianwenaiapi.com",  # Qwen TokenPlan
        #                 (OpenAI-compatible; base
        #                 https://token-plan.maas.qianwenaiapi.com/
        #                 compatible-mode/v1 — SDK-style value, the relay
        #                 strips the trailing /v1 before gating; live-probed
        #                 2026-10-07: GET .../v1/models and POST
        #                 .../v1/chat/completions both 200).
    }
)

# ---------------------------------------------------------------------------
# TEE mode (M7-A, Phala Cloud / dstack CVM).
#
# When the relay runs inside a Phala Cloud Intel-TDX CVM the dstack guest
# agent exposes a unix socket at /var/run/dstack.sock; the seller key is then
# DERIVED INSIDE THE TEE via dstack get_key (deterministic, never lives in
# env vars or on disk outside the CVM). Without the socket (dev laptop /
# CI / plain docker) the relay keeps requiring RELAY_SELLER_KEY — zero
# behavior change outside TEE mode.
#
# Signing deliberately uses get_key + eth_account: the dstack Sign-RPC
# sign() returns a 64-byte signature WITHOUT the recovery id, which cannot
# back the EIP-191 request-auth or EIP-712 receipt signatures this relay
# needs.
# ---------------------------------------------------------------------------
TEE_KEY_PATH = "wallet/ethereum/tokenshare"
DEFAULT_DSTACK_SOCKET = "/var/run/dstack.sock"

# ---------------------------------------------------------------------------
# M15 R1 — SHARED custody mode (RELAY_MODE=shared, protocol v1.0-e1).
#
# In shared mode the relay hosts key custody for EXTERNAL sellers: each
# seller enrolls an upstream api key (encrypted at rest in the keystore,
# KEK-wrapped DEK per entry). The relay's OWN operational keys are:
#   * shared signer  — secp256k1 (EIP-191 request auth / receipts in R2),
#   * shared KEK     — AES-256 key unwrapping entry DEKs,
#   * shared upload  — NIST P-256 scalar for the ECDH envelope.
# With a dstack socket all three are DERIVED via the BARE legacy get_key
# paths below (raw 32 bytes; NEVER the unsupported secp256r1 dstack mode,
# NEVER a v1 KDF). Without a socket the three env keys are required — any
# derive/config error fails closed (ConfigError), there is NO fallback.
# Shared mode does NOT need OPENAI_API_KEY / RELAY_SELLER_KEY; single mode
# keeps the old TEE path and env unchanged.
# ---------------------------------------------------------------------------
ENV_RELAY_MODE = "RELAY_MODE"
ENV_PUBLIC_ORIGIN = "RELAY_PUBLIC_ORIGIN"
ENV_SHARED_KEYSTORE_PATH = "SHARED_KEYSTORE_PATH"
ENV_SHARED_SIGNER_KEY = "SHARED_SIGNER_KEY"
ENV_SHARED_KEK_HEX = "SHARED_KEK_HEX"
ENV_SHARED_UPLOAD_KEY = "SHARED_UPLOAD_KEY"
ENV_ALLOW_INSECURE_ENDPOINT = "ALLOW_INSECURE_ENDPOINT"
# M15 bootstrap gate (two-phase first boot, shared-only opt-in). Strictly
# parsed ONCE at startup and FROZEN for the process lifetime: there is no
# runtime/HTTP switch. Unset/empty/0/false → off; 1/true → on; anything else
# → ConfigError.
ENV_BOOTSTRAP_MODE = "RELAY_BOOTSTRAP_MODE"

MODE_SINGLE = "single"
MODE_SHARED = "shared"

DEFAULT_SHARED_KEYSTORE_PATH = "/data/tokenshare/keystore.json"

# BARE legacy dstack get_key paths (v1.0-e1) — raw 32-byte outputs.
SHARED_SIGNER_KEY_PATH = "tokenshare/shared/signer/v1"
SHARED_KEK_KEY_PATH = "tokenshare/shared/kek/v1"
SHARED_UPLOAD_KEY_PATH = "tokenshare/shared/upload/v1"

# NIST P-256 group order (secp256r1) — upload raw32 → scalar mapping.
P256_ORDER = 0xFFFFFFFF00000000FFFFFFFFFFFFFFFFBCE6FAADA7179E84F3B9CAC2FC632551

# host -> provider id (the provider identity used for model consistency).
OFFICIAL_HOST_PROVIDER: Final[dict[str, str]] = {
    "api.openai.com": "openai",
    "api.moonshot.cn": "moonshot",
    "api.moonshot.ai": "moonshot",
    "api.kimi.com": "moonshot",
    "opencode.ai": "zen",  # OpenCode Zen platform
    "token-plan.maas.qianwenaiapi.com": "qwen",  # Qwen TokenPlan
}

# Official catalog model prefixes per provider. UNKNOWN prefixes => the model
# is not an official-plan model (provider_for_model returns None). The relay
# 400s such models instead of forwarding them to an official upstream.
# "k3" covers the Kimi Coding plan model face: k3 / k3-256k
# (kimi-for-coding / kimi-for-coding-highspeed already match "kimi-").
MODEL_PROVIDER_PREFIXES: Final[dict[str, tuple[str, ...]]] = {
    "openai": ("gpt-", "o1", "o3", "o4", "chatgpt-"),
    "moonshot": ("kimi-", "moonshot-", "k3"),
    # OpenCode Zen platform (opencode.ai): minimal official-plan model face.
    "zen": ("deepseek-", "glm-"),
    # Qwen TokenPlan (token-plan.maas.qianwenaiapi.com): model IDs carry the
    # "qwen" face directly, e.g. qwen3.8-max / qwen-max / qwen-plus.
    # Like moonshot's "k3", the bare "qwen" prefix has no separator; it does
    # not collide with any other provider's prefix ("k3"-style faces are
    # disjoint, "qwen" is not a prefix of "kimi-"/"moonshot-"/"gpt-"/etc.).
    "qwen": ("qwen",),
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


def dstack_client(socket_path: str):
    """Construct a dstack SDK client (lazy import — the 'dstack-sdk'
    dependency is only required when the relay actually runs inside a
    dstack CVM; importing it on a dev laptop would be dead weight)."""
    from dstack_sdk import DstackClient

    return DstackClient(socket_path)


def _load_tee_seller_key(socket_path: str) -> str:
    """Derive the seller key inside the TEE: DstackClient.get_key(path) →
    hex key. Any failure raises ConfigError (fail-fast, no degraded mode);
    the key value itself is never logged."""
    try:
        result = dstack_client(socket_path).get_key(TEE_KEY_PATH)
        key = result.decode_key()
    except ImportError as exc:
        raise ConfigError(
            f"dstack socket found at {socket_path!r} (TEE mode) but the "
            "'dstack-sdk' package is not installed — add it to the relay "
            "image (requirements.txt)"
        ) from exc
    except Exception as exc:
        raise ConfigError(
            f"dstack get_key({TEE_KEY_PATH!r}) failed: {exc}"
        ) from exc
    if not isinstance(key, str) or not key.strip():
        raise ConfigError("dstack get_key returned no key material")
    key = key.strip()
    if not key.startswith("0x"):
        key = "0x" + key
    if len(key) != 66:
        raise ConfigError(
            "dstack get_key returned key material of unexpected length "
            "(expected a 0x-prefixed 32-byte private key)"
        )
    return key


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
    # Required (single mode) / shared-signer key (shared mode).
    seller_key: str
    rpc_url: str
    chain_id: int
    escrow_addr: str
    registry_addr: str
    usdc_addr: str
    # None in shared mode (per-seller upstream keys are custody-enrolled).
    openai_api_key: str | None

    # Optional with defaults.
    openai_base_url: str | None
    forward_margin_s: int
    port: int
    prompt_token_cap: int
    completion_token_cap: int
    verify_upstream_on_start: bool

    # TEE mode (M7-A): True when the seller key was derived inside a dstack
    # CVM (socket present). tee_socket_path is only set in TEE mode and is
    # reused by the /attestation route for get_quote.
    tee_mode: bool = False
    tee_socket_path: str | None = None

    # M15 R1 shared custody mode (RELAY_MODE=shared). All None in single.
    mode: str = MODE_SINGLE
    public_origin: str | None = None
    keystore_path: str | None = None
    shared_kek: bytes | None = None
    shared_upload_priv: int | None = None
    # M15 bootstrap gate (shared-only): frozen at startup. In bootstrap the
    # state carries NO Keystore/NonceStore, the gate denies every non-identity
    # route, and the keystore path MUST NOT exist yet (two-phase first boot).
    bootstrap_mode: bool = False

    @property
    def is_shared(self) -> bool:
        return self.mode == MODE_SHARED

    @property
    def seller_address(self) -> str:
        """Single mode: address derived from RELAY_SELLER_KEY. Shared mode:
        the SHARED SIGNER address (no RELAY_SELLER_KEY exists there)."""
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


def _is_invalid_tld_host(host: str) -> bool:
    """True for the reserved RFC 2606 ``.invalid`` TLD face: the bare
    ``invalid`` host or any ``*.invalid`` name. Label-exact — ``notinvalid.com``
    is a normal domain and never matches."""
    return host == "invalid" or host.endswith(".invalid")


def normalize_public_origin(raw: str, *, bootstrap_mode: bool = False) -> str:
    """Canonical RELAY_PUBLIC_ORIGIN (v1.0-e1): https://host[:port] —
    lowercase host, NO trailing slash, NO path/query/fragment/userinfo.
    ALLOW_INSECURE_ENDPOINT=1 (dev only) additionally admits http://. An
    explicit default port (443/80) is normalized away; non-default ports
    are kept.

    Bootstrap exception (RELAY_BOOTSTRAP_MODE=1, phase 1 only): a temporary
    placeholder origin under the reserved ``.invalid`` TLD is allowed so the
    operator can boot before the final gateway origin exists (https only).
    NORMAL shared mode rejects ``invalid``/``*.invalid`` hosts outright —
    the placeholder must never survive into the serving phase."""
    value = (raw or "").strip()
    if not value:
        raise ConfigError(f"{ENV_PUBLIC_ORIGIN!r} is required in shared mode")
    try:
        parsed = urlparse(value)
    except ValueError as exc:
        raise ConfigError(f"{ENV_PUBLIC_ORIGIN}={raw!r} is not a valid URL") from exc
    if parsed.username or parsed.password:
        raise ConfigError(f"{ENV_PUBLIC_ORIGIN}={raw!r} must not carry userinfo")
    if parsed.query or parsed.fragment or (parsed.path or "") not in ("",):
        raise ConfigError(
            f"{ENV_PUBLIC_ORIGIN}={raw!r} must be a bare origin "
            "(no path, query, fragment or trailing slash)"
        )
    allow_insecure = os.environ.get(ENV_ALLOW_INSECURE_ENDPOINT, "").strip() == "1"
    if parsed.scheme == "https":
        pass
    elif parsed.scheme == "http" and allow_insecure:
        logger.warning(
            "ALLOW_INSECURE_ENDPOINT=1 — http public origin accepted (DEV/TEST ONLY)"
        )
    else:
        raise ConfigError(
            f"{ENV_PUBLIC_ORIGIN}={raw!r} must be https (set ALLOW_INSECURE_ENDPOINT=1 "
            "for dev/test http only)"
        )
    if not parsed.hostname:
        raise ConfigError(f"{ENV_PUBLIC_ORIGIN}={raw!r} has no host")
    host = parsed.hostname.lower()
    if _is_invalid_tld_host(host):
        if not bootstrap_mode:
            raise ConfigError(
                f"{ENV_PUBLIC_ORIGIN}={raw!r}: the reserved .invalid TLD is only "
                "allowed as the temporary https placeholder during "
                "RELAY_BOOTSTRAP_MODE=1 phase 1"
            )
        if parsed.scheme != "https":
            raise ConfigError(
                f"{ENV_PUBLIC_ORIGIN}={raw!r}: the bootstrap .invalid placeholder "
                "must be https"
            )
    try:
        port = parsed.port
    except ValueError as exc:
        raise ConfigError(f"{ENV_PUBLIC_ORIGIN}={raw!r} has an invalid port") from exc
    default_port = (parsed.scheme == "https" and port == 443) or (
        parsed.scheme == "http" and port == 80
    )
    origin = f"{parsed.scheme}://{host}" + ("" if port is None or default_port else f":{port}")
    return origin


def _derive_shared_keys(socket_path: str) -> tuple[str, bytes, int]:
    """Derive the three shared keys INSIDE the TEE via the BARE legacy
    dstack get_key paths (v1.0-e1). raw32 outputs: signer = secp256k1
    private key, KEK = AES-256 key, upload = P-256 scalar via
    (int(raw) % (order-1)) + 1. NEVER touches the unsupported secp256r1
    dstack mode or any v1 KDF. Any failure → ConfigError (fail closed, no
    fallback); key material never appears in messages."""
    try:
        client = dstack_client(socket_path)
        signer_raw = _decode_raw32(client.get_key(SHARED_SIGNER_KEY_PATH), SHARED_SIGNER_KEY_PATH)
        kek_raw = _decode_raw32(client.get_key(SHARED_KEK_KEY_PATH), SHARED_KEK_KEY_PATH)
        upload_raw = _decode_raw32(client.get_key(SHARED_UPLOAD_KEY_PATH), SHARED_UPLOAD_KEY_PATH)
    except ImportError as exc:
        raise ConfigError(
            f"dstack socket found at {socket_path!r} (shared mode) but the "
            "'dstack-sdk' package is not installed — add it to the relay "
            "image (requirements.txt)"
        ) from exc
    except ConfigError:
        raise
    except Exception as exc:
        raise ConfigError(
            "dstack shared key derivation failed (fail closed, no fallback)"
        ) from exc

    signer_key = "0x" + signer_raw.hex()
    try:
        Account.from_key(signer_key)
    except Exception as exc:
        raise ConfigError("shared signer key is not a valid secp256k1 key") from exc

    from .custody import raw32_to_upload_scalar, validate_upload_scalar

    try:
        upload_scalar = raw32_to_upload_scalar(upload_raw)
        validate_upload_scalar(upload_scalar)
    except ValueError as exc:
        raise ConfigError("shared upload key derivation produced an invalid scalar") from exc
    return signer_key, kek_raw, upload_scalar


def _decode_raw32(value: Any, path: str) -> bytes:
    """dstack get_key result → exactly 32 raw bytes (fail closed).

    Accepts two shapes, both resolving to exactly 32 raw bytes:
    - bytes/bytearray: the native dstack-sdk 0.5.x ``decode_key()`` output;
    - str: 64 hex chars, optional 0x/0X prefix.
    Any other shape/length fails closed. Key material NEVER appears in
    messages — only its type/length.
    """
    try:
        key = value.decode_key()
    except Exception as exc:
        raise ConfigError(f"dstack get_key({path!r}) decode failed (fail closed)") from exc
    if isinstance(key, (bytes, bytearray)):
        if len(key) != 32:
            raise ConfigError(
                f"dstack get_key({path!r}) returned {len(key)} raw bytes (expected 32)"
            )
        return bytes(key)
    if not isinstance(key, str) or not key.strip():
        raise ConfigError(
            f"dstack get_key({path!r}) returned no usable key material "
            f"(got {type(key).__name__})"
        )
    key = key.strip()
    if key[:2].lower() == "0x":
        key = key[2:]
    if len(key) != 64:
        raise ConfigError(
            f"dstack get_key({path!r}) returned {len(key)} hex chars (expected 64)"
        )
    try:
        raw = bytes.fromhex(key)
    except ValueError as exc:
        raise ConfigError(f"dstack get_key({path!r}) returned non-hex material") from exc
    if len(raw) != 32:
        raise ConfigError(
            f"dstack get_key({path!r}) returned {len(raw)} bytes (expected 32)"
        )
    return raw


def _chain_id_from_env() -> int:
    try:
        return int(_require(ENV_CHAIN_ID))
    except ValueError as exc:
        raise ConfigError(f"{ENV_CHAIN_ID!r} must be an integer") from exc


def _parse_bootstrap_mode() -> bool:
    """Strict RELAY_BOOTSTRAP_MODE parse: unset/empty/0/false → False;
    1/true → True; ANY other value → ConfigError (fail closed). The result
    is frozen into the Config for the whole process lifetime."""
    raw = os.environ.get(ENV_BOOTSTRAP_MODE)
    if raw is None or not raw.strip():
        return False
    value = raw.strip().lower()
    if value in ("0", "false"):
        return False
    if value in ("1", "true"):
        return True
    raise ConfigError(
        f"{ENV_BOOTSTRAP_MODE!r} must be unset/empty/'0'/'false' or '1'/'true', "
        f"got {raw!r}"
    )


def _keystore_path_must_not_exist(keystore_path: str) -> None:
    """Bootstrap fail-closed gate: the keystore file must NOT exist yet —
    a fresh CVM volume has no store, and bootstrapping over an existing one
    (empty entries, corrupt JSON or wrong-KEK alike) is forbidden. Uses
    ``lexists`` so a DANGLING symlink also counts as existing; the file is
    never parsed, read, migrated, deleted, nor are its parent dirs created."""
    if os.path.lexists(keystore_path):
        raise ConfigError(
            f"RELAY_BOOTSTRAP_MODE=1: the keystore path {keystore_path!r} already "
            "exists — bootstrap only boots a FRESH keystore volume. Refusing to "
            "parse, read, migrate or delete it; unset RELAY_BOOTSTRAP_MODE to "
            "serve normally."
        )


def _load_shared_config(bootstrap_mode: bool) -> Config:
    """Shared-mode config assembly. Any derive/config error fails closed
    (ConfigError, no fallback). OPENAI_API_KEY / RELAY_SELLER_KEY are NOT
    needed here; the chain variables are (the custody messages bind them)."""
    origin = normalize_public_origin(_require(ENV_PUBLIC_ORIGIN), bootstrap_mode=bootstrap_mode)
    keystore_path = os.environ.get(ENV_SHARED_KEYSTORE_PATH, "").strip() or (
        DEFAULT_SHARED_KEYSTORE_PATH
    )
    # Bootstrap ordering guarantee: the existence gate fires BEFORE any dstack
    # key derivation — an existing store (any content) must abort the boot
    # before the TEE is even asked for keys.
    if bootstrap_mode:
        _keystore_path_must_not_exist(keystore_path)

    socket_path = DEFAULT_DSTACK_SOCKET
    tee_socket = os.path.exists(socket_path)
    if tee_socket:
        signer_key, kek, upload_priv = _derive_shared_keys(socket_path)
        if (os.environ.get(ENV_SHARED_SIGNER_KEY) or "").strip():
            logger.warning(
                "shared mode: SHARED_* key env vars are present but IGNORED — "
                "keys are derived inside the CVM via dstack get_key"
            )
    else:
        signer_hex = _require(ENV_SHARED_SIGNER_KEY)
        kek_hex = _require(ENV_SHARED_KEK_HEX)
        upload_hex = _require(ENV_SHARED_UPLOAD_KEY)
        for name, value in (
            (ENV_SHARED_SIGNER_KEY, signer_hex),
            (ENV_SHARED_KEK_HEX, kek_hex),
            (ENV_SHARED_UPLOAD_KEY, upload_hex),
        ):
            if not value.startswith("0x") or len(value) != 66:
                raise ConfigError(f"{name!r} must be a 0x-prefixed 32-byte hex key")
        try:
            Account.from_key(signer_hex)
        except Exception as exc:
            raise ConfigError(
                f"{ENV_SHARED_SIGNER_KEY!r} is not a valid secp256k1 private key"
            ) from exc
        kek = bytes.fromhex(kek_hex[2:])
        from .custody import validate_upload_scalar

        try:
            # The env value is ALREADY a valid P-256 scalar — no mapping.
            upload_priv = validate_upload_scalar(int(upload_hex[2:], 16))
        except ValueError as exc:
            raise ConfigError(
                f"{ENV_SHARED_UPLOAD_KEY!r} is not a valid P-256 scalar "
                f"(must be in [1, {P256_ORDER - 1}])"
            ) from exc
        signer_key = signer_hex

    return Config(
        seller_key=signer_key,
        rpc_url=_require(ENV_RPC_URL),
        chain_id=_chain_id_from_env(),
        escrow_addr=_require(ENV_ESCROW_ADDR),
        registry_addr=_require(ENV_REGISTRY_ADDR),
        usdc_addr=_require(ENV_USDC_ADDR),
        openai_api_key=None,
        openai_base_url=None,
        forward_margin_s=_optional_int(ENV_FORWARD_MARGIN_S, DEFAULT_FORWARD_MARGIN_S),
        port=_optional_int(ENV_PORT, DEFAULT_PORT, minimum=1),
        prompt_token_cap=_optional_int(ENV_PROMPT_TOKEN_CAP, DEFAULT_PROMPT_TOKEN_CAP),
        completion_token_cap=_optional_int(
            ENV_COMPLETION_TOKEN_CAP, DEFAULT_COMPLETION_TOKEN_CAP
        ),
        verify_upstream_on_start=False,
        tee_mode=tee_socket,
        tee_socket_path=socket_path if tee_socket else None,
        mode=MODE_SHARED,
        public_origin=origin,
        keystore_path=keystore_path,
        shared_kek=kek,
        shared_upload_priv=upload_priv,
        bootstrap_mode=bootstrap_mode,
    )


def load_config() -> Config:
    """Assemble configuration from the environment. Raise ConfigError on any
    missing required variable — callers must let the process exit non-zero."""
    bootstrap_mode = _parse_bootstrap_mode()
    mode = (os.environ.get(ENV_RELAY_MODE, "") or MODE_SINGLE).strip().lower() or MODE_SINGLE
    if mode not in (MODE_SINGLE, MODE_SHARED):
        raise ConfigError(
            f"{ENV_RELAY_MODE!r} must be {MODE_SINGLE!r} or {MODE_SHARED!r}, got {mode!r}"
        )
    if mode == MODE_SHARED:
        return _load_shared_config(bootstrap_mode)
    if bootstrap_mode:
        raise ConfigError(
            f"{ENV_BOOTSTRAP_MODE}=1 is a shared-mode-only opt-in; single mode "
            "has no bootstrap phase"
        )

    chain_id = _chain_id_from_env()

    # TEE branch: when the dstack socket exists (relay runs inside a Phala
    # Cloud CVM) the seller key is DERIVED INSIDE THE TEE and RELAY_SELLER_KEY
    # is ignored; without the socket the env key is required exactly as before
    # (zero behavior change outside TEE mode).
    socket_path = DEFAULT_DSTACK_SOCKET
    tee_mode = os.path.exists(socket_path)
    if tee_mode:
        seller_key = _load_tee_seller_key(socket_path)
        if (os.environ.get(ENV_SELLER_KEY) or "").strip():
            logger.warning(
                "TEE mode: RELAY_SELLER_KEY is present but IGNORED — the seller "
                "key is derived inside the CVM via dstack get_key(%r)",
                TEE_KEY_PATH,
            )
    else:
        seller_key = _require(ENV_SELLER_KEY)
        if not seller_key.startswith("0x") or len(seller_key) != 66:
            raise ConfigError(
                f"{ENV_SELLER_KEY!r} must be a 0x-prefixed 32-byte hex private key"
            )

    try:
        # Validate the key parses; never log or print it.
        Account.from_key(seller_key)
    except Exception as exc:
        source = "dstack TEE" if tee_mode else ENV_SELLER_KEY
        raise ConfigError(f"seller key from {source} is not a valid private key") from exc

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
        tee_mode=tee_mode,
        tee_socket_path=socket_path if tee_mode else None,
    )
