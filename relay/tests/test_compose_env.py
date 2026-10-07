"""Static compose-env validation (NB1): BOTH compose files must deliver every
required runtime setting to the container via explicit ``${VAR}`` compose
interpolation — because Phala Cloud ``phala deploy -e .env`` feeds the env to
SERVER-SIDE interpolation (not a container ``env_file``), and a setting that
is not ``${...}``-referenced never reaches the CVM container.

Evidence produced here (static only — NO daemon, NO build, NO real .env):
  * full synthetic render (fake values only) → the rendered container
    environment carries ALL required settings (chain pins, origin, CORS,
    single auth values, registrar inputs);
  * missing any mandatory setting (incl. explicit CORS) → render fails with
    a clear ``required variable <NAME>`` error (fail closed);
  * NB2: the default single/no-profile render with NO REGISTRAR_* inputs
    SUCCEEDS (compose interpolates even profile-gated services — the three
    registration-ONLY inputs default EMPTY, required only when the registrar
    actually runs; its own validation fail-fasts before any network/signing,
    evidenced against the real registrar.py);
  * literal pins (RELAY_MODE / PORT=8787 / shared keystore path) survive
    hostile interpolation inputs;
  * the local ``.env`` beside the compose file (compose default lookup) and
    ``--env-file`` both work as interpolation sources; ``env_file`` is NOT
    required to deliver values.

No real ``.env`` is ever read or printed: renders happen on COPIES of the
compose files inside a temp project dir with a CLEAN environment plus
synthetic placeholder values.
"""

from __future__ import annotations

import os
import shutil
import subprocess
from pathlib import Path

import pytest
import yaml  # host env dep for rendering assertions (static only)


_STRIP_TAGS = (
    "tag:yaml.org,2002:int", "tag:yaml.org,2002:float",
    "tag:yaml.org,2002:bool", "tag:yaml.org,2002:timestamp",
)


class _StrScalars(yaml.SafeLoader):
    """SafeLoader with int/float/bool/timestamp implicit resolution stripped —
    compose config output prints unquoted `0x…` addresses that YAML 1.1
    would coerce to huge ints; contract addresses must stay strings."""


_StrScalars.yaml_implicit_resolvers = {
    first: [(t, r) for (t, r) in entries if t not in _STRIP_TAGS]
    for first, entries in yaml.SafeLoader.yaml_implicit_resolvers.items()
}


def _yaml_load(text: str):
    return yaml.load(text, Loader=_StrScalars)

REPO_ROOT = Path(__file__).resolve().parents[2]
RELAY_DIR = REPO_ROOT / "relay"
SINGLE_COMPOSE = "docker-compose.yml"
SHARED_COMPOSE = "docker-compose.shared.yml"

# Managed/secret-ish variables that must NEVER leak from the inherited shell
# env into a render (dropped before adding synthetic values).
_DROPPED_PREFIXES = ("RELAY_", "SHARED_", "REGISTRAR_", "OPENAI_")
_DROPPED_NAMES = {
    "RPC_URL", "CHAIN_ID", "ESCROW_ADDR", "REGISTRY_ADDR", "USDC_ADDR",
    "PORT", "ALLOW_INSECURE_ENDPOINT", "ALLOW_CUSTOM_UPSTREAM",
    "FORWARD_MARGIN_S", "PROMPT_TOKEN_CAP", "COMPLETION_TOKEN_CAP",
    "VERIFY_UPSTREAM_ON_START", "DSTACK_SOCKET_PATH", "COVERAGE_PROCESS_START",
}

# Synthetic/fake values ONLY (no real secrets, no real keys, throwaway
# non-address-looking checksums where a format check is irrelevant).
SYNTH = {
    "RPC_URL": "https://rpc.example/nb1-fake",
    "CHAIN_ID": "10143",
    "ESCROW_ADDR": "0x1111111111111111111111111111111111111111",
    "REGISTRY_ADDR": "0x2222222222222222222222222222222222222222",
    "USDC_ADDR": "0x3333333333333333333333333333333333333333",
    "OPENAI_API_KEY": "nb1-fake-upstream-key-not-real",
    "OPENAI_BASE_URL": "https://api.kimi.com/coding/v1",
    "RELAY_CORS_ORIGINS": "https://front.example",
    "RELAY_PUBLIC_ORIGIN": "https://app-8787.gw.example",
    "REGISTRAR_ENDPOINT": "https://app-8787.gw.example",
    "REGISTRAR_MODELS": "glm-5.3-flash",
    "REGISTRAR_PRICES": "1000000:2000000:3000000",
}

# `${VAR:?...}`-required settings per compose service (fail-closed scope).
SHARED_REQUIRED = [
    "RELAY_PUBLIC_ORIGIN", "RPC_URL", "CHAIN_ID", "ESCROW_ADDR",
    "REGISTRY_ADDR", "USDC_ADDR", "RELAY_CORS_ORIGINS",
]
SINGLE_RELAY_REQUIRED = [
    "RPC_URL", "CHAIN_ID", "ESCROW_ADDR", "REGISTRY_ADDR", "USDC_ADDR",
    "OPENAI_API_KEY", "RELAY_CORS_ORIGINS",
]
# NB2 (verified against registrar.py load_env — not assumed): the
# REGISTRAR_*-prefixed fields are registration-ONLY; they default EMPTY in
# the compose and are validated fail-fast by registrar.py itself before any
# key derivation / network / signing. The chain trio is SHARED with the
# relay and stays render-fatal.
REGISTRAR_ONLY = ["REGISTRAR_ENDPOINT", "REGISTRAR_MODELS", "REGISTRAR_PRICES"]
REGISTRAR_SHARED = ["RPC_URL", "CHAIN_ID", "REGISTRY_ADDR"]
# Literal pins that must NOT be interpolation references.
LITERAL_PINS = {"RELAY_MODE", "PORT", "SHARED_KEYSTORE_PATH"}


def _clean_env(extra: dict[str, str]) -> dict[str, str]:
    env = {
        k: v
        for k, v in os.environ.items()
        if k not in _DROPPED_NAMES
        and not k.startswith(_DROPPED_PREFIXES)
    }
    env.update(extra)
    return env


def _project(tmp_path: Path, compose_name: str, with_dotenv: dict[str, str] | None = None) -> Path:
    """Copy ONE compose file into a fresh temp project dir (build context `..`
    resolves to the temp root — exists, never built). Optionally write a
    synthetic local `.env` beside it. No real file is ever read/written."""
    proj = tmp_path / compose_name.replace(".yml", "-proj")
    proj.mkdir(exist_ok=True)
    shutil.copy(RELAY_DIR / compose_name, proj / compose_name)
    if with_dotenv is not None:
        (proj / ".env").write_text(
            "\n".join(f"{k}={v}" for k, v in sorted(with_dotenv.items())) + "\n",
            encoding="utf-8",
        )
    return proj


def _render(
    tmp_path: Path,
    compose_name: str,
    *,
    env_extra: dict[str, str] | None = None,
    env_file: Path | None = None,
    profiles: tuple[str, ...] = (),
) -> subprocess.CompletedProcess:
    cmd = ["docker", "compose", "--project-directory", "."]
    for p in profiles:
        cmd += ["--profile", p]
    if env_file is not None:
        cmd += ["--env-file", str(env_file)]
    cmd += ["-f", compose_name, "config"]
    return subprocess.run(
        cmd,
        cwd=str(_project(tmp_path, compose_name)),
        capture_output=True,
        text=True,
        timeout=60,
        env=_clean_env(env_extra or {}),
    )


def _service_env(rendered: str, service: str) -> dict[str, str]:
    doc = _yaml_load(rendered)
    return {k: str(v) for k, v in doc["services"][service]["environment"].items()}


# ---------------------------------------------------------------------------
# Static source scan (no docker needed): interpolation wiring present, pins
# literal, no fake key/address defaults.
# ---------------------------------------------------------------------------


def test_static_scan_required_interpolation_refs_and_literal_pins():
    single = (RELAY_DIR / SINGLE_COMPOSE).read_text(encoding="utf-8")
    shared = (RELAY_DIR / SHARED_COMPOSE).read_text(encoding="utf-8")
    for name in SINGLE_RELAY_REQUIRED:
        assert f"${{{name}:?" in single, f"{SINGLE_COMPOSE} missing ${{{name}:?}}"
    # NB2: chain trio referenced by BOTH relay and registrar (render-fatal);
    # the three registration-ONLY inputs use EMPTY defaults (no `:?`).
    for name in REGISTRAR_SHARED:
        assert single.count(f"${{{name}:?") >= 2, f"registrar missing ${{{name}:?}}"
    for name in REGISTRAR_ONLY:
        assert f"${{{name}:-}}" in single, f"registrar missing empty default ${{{name}:-}}"
        assert f"${{{name}:?" not in single, f"{name} must NOT be render-fatal (NB2)"
    for name in SHARED_REQUIRED:
        assert f"${{{name}:?" in shared, f"{SHARED_COMPOSE} missing ${{{name}:?}}"
    # Dev-fallback pass-throughs (empty default, never a fake key value).
    assert "${RELAY_SELLER_KEY:-}" in single
    for dev in ("SHARED_SIGNER_KEY", "SHARED_KEK_HEX", "SHARED_UPLOAD_KEY"):
        assert f"${{{dev}:-}}" in shared
    assert "${OPENAI_BASE_URL:-https://api.openai.com}" in single
    # Pins are literal — no `environment:` line may interpolate them.
    for compose in (single, shared):
        for line in compose.splitlines():
            for pin in LITERAL_PINS:
                if line.strip().startswith(f"{pin}:"):
                    assert "${" not in line, f"{pin} must stay literal: {line!r}"
    # No placeholder key material / no fake 0x defaults inside interpolations.
    for blob in (single, shared):
        assert ":-0x" not in blob
        assert ":-sk-" not in blob


# ---------------------------------------------------------------------------
# Shared compose: full synthetic render delivers every required setting.
# ---------------------------------------------------------------------------


def test_shared_render_delivers_all_required_settings(tmp_path):
    proc = _render(tmp_path, SHARED_COMPOSE, env_extra=SYNTH)
    assert proc.returncode == 0, proc.stderr
    doc = _yaml_load(proc.stdout)
    assert list(doc["services"]) == ["relay-shared"]  # exactly one service
    env = _service_env(proc.stdout, "relay-shared")
    assert env["RELAY_MODE"] == "shared"
    assert env["PORT"] == "8787"
    assert env["SHARED_KEYSTORE_PATH"] == "/data/tokenshare/keystore.json"
    assert env["RELAY_PUBLIC_ORIGIN"] == SYNTH["RELAY_PUBLIC_ORIGIN"]
    assert env["RPC_URL"] == SYNTH["RPC_URL"]
    assert env["CHAIN_ID"] == SYNTH["CHAIN_ID"]
    assert env["ESCROW_ADDR"] == SYNTH["ESCROW_ADDR"]
    assert env["REGISTRY_ADDR"] == SYNTH["REGISTRY_ADDR"]
    assert env["USDC_ADDR"] == SYNTH["USDC_ADDR"]
    # CORS reaches the container EXPLICITLY pinned (demo * never silent).
    assert env["RELAY_CORS_ORIGINS"] == SYNTH["RELAY_CORS_ORIGINS"]
    assert env["RELAY_CORS_ORIGINS"] != "*"
    # Dev fallback keys: present, empty defaults (no fake key material).
    assert env["SHARED_SIGNER_KEY"] == ""
    assert env["SHARED_KEK_HEX"] == ""
    assert env["SHARED_UPLOAD_KEY"] == ""
    # Persistence + TEE socket wiring survive the NB1 change.
    svc = doc["services"]["relay-shared"]
    mounts = {(v.get("source"), v["target"]) for v in svc["volumes"]}
    assert ("/var/run/dstack.sock", "/var/run/dstack.sock") in mounts
    assert ("keystore-data", "/data/tokenshare") in mounts
    assert "keystore-data" in doc["volumes"]


@pytest.mark.parametrize("missing", SHARED_REQUIRED)
def test_shared_missing_mandatory_fails_render(tmp_path, missing):
    env = {k: v for k, v in SYNTH.items() if k != missing}
    proc = _render(tmp_path, SHARED_COMPOSE, env_extra=env)
    assert proc.returncode != 0, f"{missing} unexpectedly optional"
    assert f"required variable {missing}" in proc.stderr


def test_shared_render_via_cli_env_file_without_local_dotenv(tmp_path):
    """The Phala-like lane: interpolation inputs via --env-file, NO literal
    .env beside the compose copy — values still reach the container."""
    envf = tmp_path / "synthetic.env"
    envf.write_text(
        "\n".join(f"{k}={v}" for k, v in sorted(SYNTH.items())) + "\n",
        encoding="utf-8",
    )
    proj = _project(tmp_path, SHARED_COMPOSE)
    assert not (proj / ".env").exists()  # no literal .env beside the copy
    proc = subprocess.run(
        ["docker", "compose", "--project-directory", ".",
         "--env-file", str(envf), "-f", SHARED_COMPOSE, "config"],
        cwd=str(proj), capture_output=True, text=True, timeout=60,
        env=_clean_env({}),
    )
    assert proc.returncode == 0, proc.stderr
    env = _service_env(proc.stdout, "relay-shared")
    assert env["RELAY_PUBLIC_ORIGIN"] == SYNTH["RELAY_PUBLIC_ORIGIN"]
    assert env["RELAY_CORS_ORIGINS"] == SYNTH["RELAY_CORS_ORIGINS"]
    assert env["RPC_URL"] == SYNTH["RPC_URL"]


# ---------------------------------------------------------------------------
# Single compose + registrar compatibility.
# ---------------------------------------------------------------------------


def test_single_render_delivers_all_required_settings(tmp_path):
    proc = _render(tmp_path, SINGLE_COMPOSE, env_extra=SYNTH)
    assert proc.returncode == 0, proc.stderr
    doc = _yaml_load(proc.stdout)
    env = _service_env(proc.stdout, "relay")
    assert env["RELAY_MODE"] == "single"
    assert env["PORT"] == "8787"
    for name in ("RPC_URL", "CHAIN_ID", "ESCROW_ADDR", "REGISTRY_ADDR", "USDC_ADDR"):
        assert env[name] == SYNTH[name]
    # Single-mode auth values reach the container; base URL default mirrors
    # config.py; CORS explicitly pinned (never the silent demo *).
    assert env["OPENAI_API_KEY"] == SYNTH["OPENAI_API_KEY"]
    assert env["OPENAI_BASE_URL"] == SYNTH["OPENAI_BASE_URL"]
    assert env["RELAY_CORS_ORIGINS"] == SYNTH["RELAY_CORS_ORIGINS"]
    # Default relay has NO keystore volume (single mode needs none).
    assert all(v.get("source") != "keystore-data" for v in doc["services"]["relay"]["volumes"])
    assert "relay-shared" not in doc["services"]


def test_single_missing_mandatory_fails_render(tmp_path):
    for missing in SINGLE_RELAY_REQUIRED:
        env = {k: v for k, v in SYNTH.items() if k != missing}
        proc = _render(tmp_path, SINGLE_COMPOSE, env_extra=env)
        assert proc.returncode != 0, f"{missing} unexpectedly optional"
        assert f"required variable {missing}" in proc.stderr


def test_single_dev_fallback_seller_key(tmp_path):
    """RELAY_SELLER_KEY stays a controlled non-TEE fallback: absent → empty
    (TEE boots render; inside the CVM it is ignored); present → the local
    value wins (empty default never clobbers a valid local setting)."""
    proc = _render(tmp_path, SINGLE_COMPOSE, env_extra=SYNTH)
    assert _service_env(proc.stdout, "relay")["RELAY_SELLER_KEY"] == ""
    proc = _render(
        tmp_path, SINGLE_COMPOSE,
        env_extra={**SYNTH, "RELAY_SELLER_KEY": "0x" + "44" * 32},
    )
    assert _service_env(proc.stdout, "relay")["RELAY_SELLER_KEY"] == "0x" + "44" * 32


# ---------------------------------------------------------------------------
# NB2: default single render needs NO REGISTRAR_* inputs; registrar-only
# fields default EMPTY and are validated by registrar.py at runtime —
# fail-fast, no network, no signing (real-code evidence, not weakened).
# ---------------------------------------------------------------------------


def test_single_default_config_without_any_registrar_inputs(tmp_path):
    """The NB2 regression: plain `docker compose config` (no profile, none of
    the REGISTRAR_* inputs) with only the seven relay settings MUST succeed
    and contain NO registrar service — compose interpolates all services,
    so the registration-only inputs must not be render-fatal."""
    env = {k: v for k, v in SYNTH.items() if not k.startswith("REGISTRAR_")}
    proc = _render(tmp_path, SINGLE_COMPOSE, env_extra=env)
    assert proc.returncode == 0, proc.stderr
    doc = _yaml_load(proc.stdout)
    assert list(doc["services"]) == ["relay"]  # no registrar in default set
    envd = _service_env(proc.stdout, "relay")
    for name in SINGLE_RELAY_REQUIRED:
        assert envd[name] == SYNTH[name], name
    assert (envd["RELAY_MODE"], envd["PORT"]) == ("single", "8787")


def test_registrar_profile_with_full_inputs_delivers_all(tmp_path):
    proc = _render(tmp_path, SINGLE_COMPOSE, env_extra=SYNTH, profiles=("register",))
    assert proc.returncode == 0, proc.stderr
    doc = _yaml_load(proc.stdout)
    env = _service_env(proc.stdout, "registrar")
    assert env["RELAY_MODE"] == "single"
    for name in (*REGISTRAR_SHARED, *REGISTRAR_ONLY):
        assert env[name] == SYNTH[name], name
    # One-shot stays one-shot.
    assert doc["services"]["registrar"]["restart"] == "no"


def test_registrar_only_inputs_missing_render_ok_but_runtime_fail_fast(
    tmp_path, monkeypatch
):
    """Empty-default inputs ALLOW the render (profile-gated services are
    interpolated too — NB2), but the REAL registrar validation then fails
    fast with no network access and no signing. Two evidence layers:

    1. render: `--profile register` with NO REGISTRAR_* → config succeeds,
       the three inputs render empty, chain trio resolved;
    2. runtime: registrar.py (real code, in-process) with the same partial
       env → RegistrarError listing the missing var, BEFORE
       derive_seller_key / connect_web3 (patched to fail the test if ever
       called) — and a subprocess run exits 1 listing the var with no tx /
       address output. Reuses the guard-lane subprocess helpers.
    """
    env = {k: v for k, v in SYNTH.items() if not k.startswith("REGISTRAR_")}
    proc = _render(tmp_path, SINGLE_COMPOSE, env_extra=env, profiles=("register",))
    assert proc.returncode == 0, proc.stderr
    doc = _yaml_load(proc.stdout)
    envd = _service_env(proc.stdout, "registrar")
    for name in REGISTRAR_ONLY:
        assert envd[name] == "", f"{name} must default empty"
    for name in REGISTRAR_SHARED:
        assert envd[name] == SYNTH[name], name

    # In-process: real registrar.load_env fail-fast BEFORE derive/network.
    from relay import registrar

    def _must_not_run(*_a, **_k):  # pragma: no cover — would fail the test
        raise AssertionError("derive/network reached despite missing input")

    monkeypatch.setattr(registrar, "derive_seller_key", _must_not_run)
    monkeypatch.setattr(registrar, "connect_web3", _must_not_run)
    monkeypatch.delenv("RELAY_MODE", raising=False)
    for name, value in env.items():
        monkeypatch.setenv(name, value)
    for name in REGISTRAR_ONLY:
        monkeypatch.delenv(name, raising=False)
    with pytest.raises(registrar.RegistrarError) as exc:
        registrar.main()
    message = str(exc.value)
    assert "missing required environment variable(s)" in message
    assert "REGISTRAR_PRICES" in message
    assert "missing required environment variable(s)" in message

    # Subprocess (reuse guard-lane helpers): end-to-end exit != 0, and the
    # pre-derivation failure means no address printing, no tx sending.
    from relay.tests.test_registrar_shared_guard import _base_env, _run_registrar

    proc = _run_registrar(_base_env(env))
    assert proc.returncode != 0
    assert "missing required environment variable(s)" in proc.stderr
    assert "REGISTRAR_PRICES" in proc.stderr
    assert "tx sent" not in proc.stdout
    assert "TEE-derived seller address" not in proc.stdout


def test_registrar_shared_chain_inputs_stay_render_fatal(tmp_path):
    """The NB2 downgrade is scoped to the registration-ONLY fields only —
    a missing SHARED chain value still fails the render even with the
    register profile."""
    env = {k: v for k, v in SYNTH.items() if k != "RPC_URL"}
    proc = _render(tmp_path, SINGLE_COMPOSE, env_extra=env, profiles=("register",))
    assert proc.returncode != 0
    assert "required variable RPC_URL" in proc.stderr


def test_local_dotenv_beside_compose_case(tmp_path):
    """Existing local flow: `.env` beside the compose file (no --env-file, no
    shell vars) → compose default lookup feeds the interpolation AND the
    local env_file convenience; optional dev flags ride along locally."""
    local = {**SYNTH, "FORWARD_MARGIN_S": "90", "ALLOW_INSECURE_ENDPOINT": "1"}
    proj = _project(tmp_path, SHARED_COMPOSE, with_dotenv=local)
    proc = subprocess.run(
        ["docker", "compose", "--project-directory", ".", "-f", SHARED_COMPOSE, "config"],
        cwd=str(proj), capture_output=True, text=True, timeout=60,
        env=_clean_env({}),
    )
    assert proc.returncode == 0, proc.stderr
    env = _service_env(proc.stdout, "relay-shared")
    assert env["RELAY_PUBLIC_ORIGIN"] == SYNTH["RELAY_PUBLIC_ORIGIN"]
    assert env["RELAY_CORS_ORIGINS"] == SYNTH["RELAY_CORS_ORIGINS"]
    assert env["FORWARD_MARGIN_S"] == "90"  # optional tunable via local env_file
    assert env["ALLOW_INSECURE_ENDPOINT"] == "1"  # dev flag: local only


# ---------------------------------------------------------------------------
# Literal pins survive hostile interpolation inputs; default-scope flags.
# ---------------------------------------------------------------------------


def test_literal_pins_survive_hostile_env(tmp_path):
    hostile = {**SYNTH, "RELAY_MODE": "shared", "PORT": "9999",
               "SHARED_KEYSTORE_PATH": "/tmp/hostile"}
    proc = _render(tmp_path, SINGLE_COMPOSE, env_extra=hostile)
    env = _service_env(proc.stdout, "relay")
    assert (env["RELAY_MODE"], env["PORT"]) == ("single", "8787")

    hostile_shared = {**SYNTH, "RELAY_MODE": "single", "PORT": "9999",
                      "SHARED_KEYSTORE_PATH": "/tmp/hostile"}
    proc = _render(tmp_path, SHARED_COMPOSE, env_extra=hostile_shared)
    env = _service_env(proc.stdout, "relay-shared")
    assert (env["RELAY_MODE"], env["PORT"]) == ("shared", "8787")
    assert env["SHARED_KEYSTORE_PATH"] == "/data/tokenshare/keystore.json"


def test_allow_insecure_endpoint_not_interpolated_in_shared(tmp_path):
    """Dev flag must not ride into a cloud deploy via interpolation (local
    env_file only — see test_local_dotenv_beside_compose_case)."""
    shared = (RELAY_DIR / SHARED_COMPOSE).read_text(encoding="utf-8")
    assert "${ALLOW_INSECURE_ENDPOINT" not in shared
    env = {**SYNTH, "ALLOW_INSECURE_ENDPOINT": "1"}
    proc = _render(tmp_path, SHARED_COMPOSE, env_extra=env)
    assert proc.returncode == 0, proc.stderr
    assert "ALLOW_INSECURE_ENDPOINT" not in _service_env(proc.stdout, "relay-shared")


# ------------------------------------------------------- M15 bootstrap gate
def test_shared_compose_carries_bootstrap_interpolation_default_zero():
    """RELAY_BOOTSTRAP_MODE must reach the container via explicit
    interpolation with default 0 (normal serving) — NB1: an unreferenced var
    never reaches the CVM."""
    shared = (RELAY_DIR / SHARED_COMPOSE).read_text(encoding="utf-8")
    assert "RELAY_BOOTSTRAP_MODE: ${RELAY_BOOTSTRAP_MODE:-0}" in shared


def test_shared_compose_literal_pins_survive_bootstrap_addition():
    """The literal pins (RELAY_MODE=shared / PORT=8787 / keystore path) are
    untouched by the bootstrap interpolation, and the bootstrap var renders
    from the default when unset."""
    rendered = _yaml_load((RELAY_DIR / SHARED_COMPOSE).read_text(encoding="utf-8"))
    env = rendered["services"]["relay-shared"]["environment"]
    assert env["RELAY_MODE"] == "shared"
    assert env["PORT"] == "8787"
    assert env["SHARED_KEYSTORE_PATH"] == "/data/tokenshare/keystore.json"
    assert env["RELAY_BOOTSTRAP_MODE"] == "${RELAY_BOOTSTRAP_MODE:-0}"


def test_env_example_documents_bootstrap_gate():
    text = (RELAY_DIR / ".env.example").read_text(encoding="utf-8")
    assert "RELAY_BOOTSTRAP_MODE" in text
    assert "bootstrap" in text
