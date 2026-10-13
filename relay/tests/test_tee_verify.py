"""P0 TEE proof completion — GET /attestation/verify (relay-side proxy of
the fixed Phala cloud verifier).

Everything is mocked: a fake dstack socket/client synthesizes a fixed-length
TDX v4 quote and an httpx.MockTransport stands in for the cloud verifier —
zero real TEE, zero network. The route always answers HTTP 200; the
verification semantics live in the body (front-end contract, field names
verbatim).
"""

from __future__ import annotations

import hashlib
import json
import types
from typing import Any

import httpx
import pytest
from eth_account import Account
from eth_utils import keccak
from fastapi.testclient import TestClient

from .conftest import mock_openai_url, setup_relay_env
from .test_tee import (
    SYNTH_MRSEAM,
    SYNTH_QUOTE_LEN,
    SYNTH_RTMR0,
    SYNTH_RTMR1,
    SYNTH_RTMR2,
    SYNTH_RTMR3,
    FakeDstackClient,
    TEE_ADDR,
    synth_v4_quote,
)

TEE_KEY = "0x" + "44" * 32

SHARED_SIGNER_PATH = "tokenshare/shared/signer/v1"
SHARED_KEK_PATH = "tokenshare/shared/kek/v1"
SHARED_UPLOAD_PATH = "tokenshare/shared/upload/v1"


class SharedFakeDstackClient(FakeDstackClient):
    """FakeDstackClient that also answers the three SHARED get_key paths
    (raw 32-byte outputs) so shared-mode boots inside a fake socket."""

    def get_key(self, path: str) -> Any:
        if path in (SHARED_SIGNER_PATH, SHARED_KEK_PATH, SHARED_UPLOAD_PATH):
            if path == SHARED_SIGNER_PATH:
                raw = bytes.fromhex(TEE_KEY[2:])
            elif path == SHARED_KEK_PATH:
                raw = bytes.fromhex("66" * 32)
            else:
                raw = bytes.fromhex("77" * 32)
            return types.SimpleNamespace(decode_key=lambda: raw)
        return super().get_key(path)


# The synthesized quote the relay will POST to the verifier (report data is
# the single-mode binding: 44 zero bytes + the TEE-derived address).
EXPECTED_REPORT_DATA = b"\x00" * 44 + bytes.fromhex(TEE_ADDR[2:])
LOCAL_QUOTE_HEX = synth_v4_quote(EXPECTED_REPORT_DATA).hex()

CHECKSUM = "56602bb34b60140b33c4cadb24ec36da39352452e1f02b725c1939aedb8bebc6"
VERIFIED_AT = "2026-10-12T10:57:14.635590+00:00"

# A verifier-shaped payload modeled on the live cloud-api response (the
# quote_collateral blobs are deliberately long PEM/CRL strings).
VERIFIER_HEADER = {
    "version": 4,
    "ak_type": "ECDSA_P256",
    "tee_type": "TEE_TDX",
    "qe_vendor": "0x939a7233f79c4ca9940a0db3957f0607",
    "user_data": "0x228b1b2533416561505b6d6ef4236c7900000000",
}


def _verifier_payload(*, reportdata: str | None = None) -> dict[str, Any]:
    return {
        "id": CHECKSUM,
        "success": True,
        "proof_of_cloud": True,
        "quote": {
            "header": dict(VERIFIER_HEADER),
            "cert_data": "-----BEGIN CERTIFICATE-----\nMIIE8jCC\n-----END CERTIFICATE-----\n",
            "body": {
                "tee_tcb_svn": "0b010500000000000000000000000000",
                "mrseam": SYNTH_MRSEAM,
                "mrsignerseam": "0x" + "00" * 48,
                "seamattributes": "0x0000000000000000",
                "tdattributes": "0x0000000100000000",
                "xfam": "0x0702000000000000",
                "mrtd": "0x" + "ab" * 48,
                "mr_config_id": "0x" + "cd" * 48,
                "mrowner": "0x" + "00" * 48,
                "mrownerconfig": "0x" + "00" * 48,
                "rtmr0": SYNTH_RTMR0,
                "rtmr1": SYNTH_RTMR1,
                "rtmr2": SYNTH_RTMR2,
                "rtmr3": SYNTH_RTMR3,
                "reportdata": reportdata or "0x" + EXPECTED_REPORT_DATA.hex(),
                "mrconfig": "0x" + "cd" * 48,
            },
            "verified": True,
        },
        "checksum": CHECKSUM,
        "verified_at": VERIFIED_AT,
        "quote_collateral": {
            "pck_crl_issuer_chain": "-----BEGIN CERTIFICATE-----\npck\n-----END CERTIFICATE-----\n",
            "root_ca_crl": "-----BEGIN X509 CRL-----\nroot\n-----END X509 CRL-----\n",
            "pck_crl": "-----BEGIN X509 CRL-----\npckcrl\n-----END X509 CRL-----\n",
            "tcb_info_issuer_chain": "-----BEGIN CERTIFICATE-----\ntcb\n-----END CERTIFICATE-----\n",
            "tcb_info": "-----BEGIN CERTIFICATE-----\ntcbinfo\n-----END CERTIFICATE-----\n",
            "tcb_info_signature": "0x" + "ef" * 64,
            "qe_identity_issuer_chain": "-----BEGIN CERTIFICATE-----\nqe\n-----END CERTIFICATE-----\n",
            "qe_identity": "-----BEGIN CERTIFICATE-----\nqeid\n-----END CERTIFICATE-----\n",
            "qe_identity_signature": "0x" + "fe" * 64,
        },
        "node_provider": {"proof_of_cloud": True, "provider": "phala", "ppid": "0xppid"},
    }


def _patch_socket(monkeypatch: pytest.MonkeyPatch, path: str) -> None:
    import relay.app.config as config

    monkeypatch.setattr(config, "DEFAULT_DSTACK_SOCKET", path)


def _patch_fake_client(monkeypatch: pytest.MonkeyPatch, factory: Any) -> None:
    import relay.app.config as config
    import relay.app.main as main

    monkeypatch.setattr(config, "dstack_client", factory)
    monkeypatch.setattr(main, "dstack_client", factory)


@pytest.fixture
def tee_client(monkeypatch: pytest.MonkeyPatch, mock_openai, tmp_path) -> Any:
    """A TEE-mode TestClient (fake socket + fake dstack client) with a fresh
    verifier cooldown/transport seam."""
    import relay.app.main as main

    socket = tmp_path / "dstack.sock"
    socket.write_bytes(b"")
    _patch_socket(monkeypatch, str(socket))
    setup_relay_env(monkeypatch, mock_openai_url(mock_openai))
    monkeypatch.delenv("RELAY_SELLER_KEY", raising=False)
    FakeDstackClient.reset()
    _patch_fake_client(monkeypatch, SharedFakeDstackClient)
    monkeypatch.setattr(main, "_verify_transport", None)
    monkeypatch.setattr(main, "_verify_last_call", None)
    with TestClient(main.app) as client:
        yield client


@pytest.fixture
def host_client(monkeypatch: pytest.MonkeyPatch, mock_openai, tmp_path) -> Any:
    """A NON-TEE TestClient (socket points nowhere)."""
    import relay.app.main as main

    missing = tmp_path / "no-dstack.sock"
    _patch_socket(monkeypatch, str(missing))
    setup_relay_env(monkeypatch, mock_openai_url(mock_openai))
    monkeypatch.setattr(main, "_verify_transport", None)
    monkeypatch.setattr(main, "_verify_last_call", None)
    with TestClient(main.app) as client:
        yield client


def _inject_verifier(
    monkeypatch: pytest.MonkeyPatch, handler: Any
) -> list[httpx.Request]:
    """Inject an httpx.MockTransport as the verifier transport seam; records
    every request the route made (URL, body, headers)."""
    import relay.app.main as main

    seen: list[httpx.Request] = []

    def _capture(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return handler(request)

    monkeypatch.setattr(main, "_verify_transport", httpx.MockTransport(_capture))
    return seen


def _ok(request: httpx.Request) -> httpx.Response:
    return httpx.Response(200, json=_verifier_payload())


# --------------------------------------------------------------------------
# success path: full contract mapping
# --------------------------------------------------------------------------
def test_verify_success_maps_contract(tee_client: Any, monkeypatch) -> None:
    seen = _inject_verifier(monkeypatch, _ok)

    resp = tee_client.get("/attestation/verify")

    assert resp.status_code == 200  # always 200, semantics in the body
    body = resp.json()
    assert set(body) == {
        "verified",
        "verifier_http_status",
        "checksum",
        "submittedQuoteSha256",
        "verified_at",
        "node_provider",
        "quote_header",
        "quote_body",
        "collateral",
        "error",
    }
    assert body["verified"] is True
    assert body["verifier_http_status"] == 200
    assert body["checksum"] == "0x" + CHECKSUM
    assert body["verified_at"] == VERIFIED_AT
    # node_provider maps the verifier's object down to the provider string.
    assert body["node_provider"] == "phala"
    assert body["quote_header"] == VERIFIER_HEADER
    assert body["quote_body"] == {
        "reportdata": "0x" + EXPECTED_REPORT_DATA.hex(),
        "rtmr0": SYNTH_RTMR0,
        "rtmr1": SYNTH_RTMR1,
        "rtmr2": SYNTH_RTMR2,
        "rtmr3": SYNTH_RTMR3,
        "mrseam": SYNTH_MRSEAM,
    }
    assert body["collateral"] == {
        "pck_chain": True,
        "tcb_info": True,
        "qe_identity": True,
        "crl": True,
    }
    assert body["error"] is None

    # The route POSTed the relay's OWN same-source quote to the FIXED
    # upstream with a browser-class User-Agent (Cloudflare 403s python UAs).
    assert len(seen) == 1
    request = seen[0]
    assert request.method == "POST"
    assert str(request.url) == "https://cloud-api.phala.com/api/v1/attestations/verify"
    assert json.loads(request.content) == {"hex": "0x" + LOCAL_QUOTE_HEX}
    assert "Mozilla/5.0" in (request.headers.get("user-agent") or "")

    # submittedQuoteSha256 hashes the EXACT bytes the verifier received
    # (decoded from the captured request body) — the same-round anchor the
    # front-end compares the verifier checksum against. A separately
    # fetched /attestation quote is a different signing and must NOT be
    # cross-compared (oracle MAJOR2).
    submitted_hex = json.loads(request.content)["hex"]
    assert submitted_hex == "0x" + LOCAL_QUOTE_HEX
    assert body["submittedQuoteSha256"] == "0x" + hashlib.sha256(
        bytes.fromhex(submitted_hex[2:])
    ).hexdigest()


def test_verify_does_not_leak_collateral_blobs(tee_client: Any, monkeypatch) -> None:
    """The raw quote_collateral PEM/CRL material must NEVER reach the body."""
    _inject_verifier(monkeypatch, _ok)

    body = tee_client.get("/attestation/verify").json()

    dumped = repr(body)
    assert "quote_collateral" not in body
    assert "cert_data" not in body
    assert "-----BEGIN CERTIFICATE-----" not in dumped
    assert "-----BEGIN X509 CRL-----" not in dumped


def test_verify_collateral_flags_reflect_missing_blobs(
    tee_client: Any, monkeypatch
) -> None:
    """Empty/absent collateral blobs → False flags (existence AND non-empty)."""

    def _sparse(request: httpx.Request) -> httpx.Response:
        payload = _verifier_payload()
        payload["quote_collateral"] = {
            "pck_crl_issuer_chain": "",
            "tcb_info": "   ",
            # qe_identity absent entirely
            "pck_crl": "-----BEGIN X509 CRL-----\nreal\n-----END X509 CRL-----\n",
        }
        return httpx.Response(200, json=payload)

    _inject_verifier(monkeypatch, _sparse)

    body = tee_client.get("/attestation/verify").json()

    assert body["verified"] is True
    assert body["collateral"] == {
        "pck_chain": False,
        "tcb_info": False,
        "qe_identity": False,
        "crl": True,
    }


def test_verify_unverified_quote_reports_false(tee_client: Any, monkeypatch) -> None:
    """HTTP 200 + success=false → verified=false, no 500. Strict council-F5
    mapping: the top-level success flag is the ONLY verdict gate —
    quote.verified is not consulted."""

    def _unverified(request: httpx.Request) -> httpx.Response:
        payload = _verifier_payload()
        payload["success"] = False
        return httpx.Response(200, json=payload)

    _inject_verifier(monkeypatch, _unverified)

    resp = tee_client.get("/attestation/verify")

    assert resp.status_code == 200
    body = resp.json()
    assert body["verified"] is False
    assert body["verifier_http_status"] == 200
    assert body["error"] is None
    assert body["checksum"] == "0x" + CHECKSUM


def test_verify_string_true_is_not_a_verdict(tee_client: Any, monkeypatch) -> None:
    """String "true" (top-level, quote-level, wherever) must never flip
    verified — only a JSON boolean true counts (strict F5 mapping)."""

    def _stringy(request: httpx.Request) -> httpx.Response:
        payload = _verifier_payload()
        payload["success"] = "true"
        payload["verified"] = "true"
        payload["quote"]["verified"] = "true"
        return httpx.Response(200, json=payload)

    _inject_verifier(monkeypatch, _stringy)

    body = tee_client.get("/attestation/verify").json()

    assert body["verified"] is False
    assert body["verifier_http_status"] == 200


def test_verify_non_200_with_success_true_is_not_verified(
    tee_client: Any, monkeypatch
) -> None:
    """A success:true body carried over a NON-200 verifier answer is never
    a verdict — verified stays false and the status is kept in error."""

    def _weird(request: httpx.Request) -> httpx.Response:
        return httpx.Response(502, json=_verifier_payload())

    _inject_verifier(monkeypatch, _weird)

    body = tee_client.get("/attestation/verify").json()

    assert body["verified"] is False
    assert body["verifier_http_status"] == 502
    assert body["error"] == "verifier HTTP 502"


def test_verify_malformed_200_body_reports_error(tee_client: Any, monkeypatch) -> None:
    """HTTP 200 with a malformed body (non-JSON HTML / non-object JSON) →
    verified=false and the raw error text kept, never swallowed."""

    def _html_ok(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, text="<html>not json</html>")

    _inject_verifier(monkeypatch, _html_ok)
    body = tee_client.get("/attestation/verify").json()
    assert body["verified"] is False
    assert body["verifier_http_status"] == 200
    assert body["error"] == "verifier returned non-JSON"


def test_verify_non_object_200_body_reports_error(
    tee_client: Any, monkeypatch
) -> None:
    def _list_ok(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json=[1, 2, 3])

    _inject_verifier(monkeypatch, _list_ok)
    body = tee_client.get("/attestation/verify").json()
    assert body["verified"] is False
    assert body["verifier_http_status"] == 200
    assert body["error"] == "verifier returned a non-object payload"


# --------------------------------------------------------------------------
# error paths
# --------------------------------------------------------------------------
def test_verify_upstream_400_passes_status_through(tee_client: Any, monkeypatch) -> None:
    def _bad_request(request: httpx.Request) -> httpx.Response:
        return httpx.Response(400, json={"detail": "invalid quote"})

    seen = _inject_verifier(monkeypatch, _bad_request)

    resp = tee_client.get("/attestation/verify")

    assert resp.status_code == 200
    body = resp.json()
    assert body["verified"] is False
    assert body["verifier_http_status"] == 400
    assert body["error"] == "verifier HTTP 400"
    assert body["checksum"] is None
    assert body["quote_body"] is None
    assert len(seen) == 1


def test_verify_upstream_403_cloudflare_challenge(tee_client: Any, monkeypatch) -> None:
    """Cloudflare intermittently 403s; the body must carry the status and an
    honest error string (never a 500, never a fabricated verdict)."""

    def _forbidden(request: httpx.Request) -> httpx.Response:
        return httpx.Response(403, json={"error": "rate limited"})

    _inject_verifier(monkeypatch, _forbidden)

    resp = tee_client.get("/attestation/verify")

    assert resp.status_code == 200
    body = resp.json()
    assert body["verified"] is False
    assert body["verifier_http_status"] == 403
    # the verifier's own error text is carried through verbatim (never swallowed)
    assert body["error"] == "verifier HTTP 403 — rate limited"
    assert body["checksum"] is None


def test_verify_upstream_403_non_json_body(tee_client: Any, monkeypatch) -> None:
    """A Cloudflare HTML challenge (403 + non-JSON) degrades to non-JSON."""

    def _challenge(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            403, text="<html><title>Attention Required</title></html>"
        )

    _inject_verifier(monkeypatch, _challenge)

    body = tee_client.get("/attestation/verify").json()

    assert body["verified"] is False
    assert body["verifier_http_status"] == 403
    assert body["error"] == "verifier returned non-JSON"


def test_verify_timeout_reports_error(tee_client: Any, monkeypatch) -> None:
    def _timeout(request: httpx.Request) -> httpx.Response:
        raise httpx.TimeoutException("verifier timed out", request=request)

    _inject_verifier(monkeypatch, _timeout)

    resp = tee_client.get("/attestation/verify")

    assert resp.status_code == 200
    body = resp.json()
    assert body["verified"] is False
    assert body["verifier_http_status"] == 0
    assert body["error"] == "verifier timeout: TimeoutException"


def test_verify_connect_error_reports_error(tee_client: Any, monkeypatch) -> None:
    def _unreachable(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("connection refused", request=request)

    _inject_verifier(monkeypatch, _unreachable)

    body = tee_client.get("/attestation/verify").json()

    assert body["verified"] is False
    assert body["verifier_http_status"] == 0
    assert body["error"] == "verifier unreachable: ConnectError"


# --------------------------------------------------------------------------
# gating: non-TEE 404 and in-process cooldown
# --------------------------------------------------------------------------
def test_verify_non_tee_404(host_client: Any) -> None:
    resp = host_client.get("/attestation/verify")
    assert resp.status_code == 404
    assert "TEE" in resp.json()["detail"]


def test_verify_cooldown_blocks_second_call(tee_client: Any, monkeypatch) -> None:
    seen = _inject_verifier(monkeypatch, _ok)

    first = tee_client.get("/attestation/verify").json()
    assert first["error"] is None and len(seen) == 1

    # Second call inside the 1 s window: cooldown, verifier untouched.
    second = tee_client.get("/attestation/verify").json()
    assert second["error"] == "cooldown"
    assert second["verified"] is False
    assert second["verifier_http_status"] == 0
    assert second["checksum"] is None
    assert second["submittedQuoteSha256"] is None  # no quote was submitted
    assert len(seen) == 1

    # After the cooldown expires the verifier is reachable again.
    import relay.app.main as main

    monkeypatch.setattr(main, "_verify_last_call", None)
    third = tee_client.get("/attestation/verify").json()
    assert third["error"] is None and len(seen) == 2


# --------------------------------------------------------------------------
# reportdata cross-check: local parse vs verifier report
# --------------------------------------------------------------------------
def test_verify_reportdata_cross_check(tee_client: Any, monkeypatch) -> None:
    """The locally PARSED reportdata (quoteBody) must equal the reportdata
    the verifier reports for the same quote — one quote, two sources."""
    _inject_verifier(monkeypatch, _ok)

    local = tee_client.get("/attestation").json()["quoteBody"]
    remote = tee_client.get("/attestation/verify").json()["quote_body"]

    assert local is not None and remote is not None
    assert remote["reportdata"] == local["reportdata"]
    assert remote["rtmr0"] == local["rtmr0"] == SYNTH_RTMR0
    assert remote["rtmr1"] == local["rtmr1"] == SYNTH_RTMR1
    assert remote["rtmr2"] == local["rtmr2"] == SYNTH_RTMR2
    assert remote["rtmr3"] == local["rtmr3"] == SYNTH_RTMR3
    assert remote["mrseam"] == local["mrseam"] == SYNTH_MRSEAM
    # And it binds the TEE-derived address exactly as /attestation promises.
    assert local["reportdata"] == "0x" + EXPECTED_REPORT_DATA.hex()


def test_verify_detects_reportdata_mismatch(tee_client: Any, monkeypatch) -> None:
    """A verifier reporting a DIFFERENT reportdata for the same quote is
    surfaced verbatim — the relay never overwrites the verdict fields."""

    def _mismatch(request: httpx.Request) -> httpx.Response:
        payload = _verifier_payload(reportdata="0x" + "99" * 64)
        return httpx.Response(200, json=payload)

    _inject_verifier(monkeypatch, _mismatch)

    local = tee_client.get("/attestation").json()["quoteBody"]
    remote = tee_client.get("/attestation/verify").json()["quote_body"]

    assert remote["reportdata"] == "0x" + "99" * 64
    assert remote["reportdata"] != local["reportdata"]


def test_verify_synthetic_quote_shape() -> None:
    """Guard the fake dstack quote itself: fixed-length v4 with the known
    field bytes exactly where the parser reads them."""
    assert len(LOCAL_QUOTE_HEX) // 2 == SYNTH_QUOTE_LEN
    raw = bytes.fromhex(LOCAL_QUOTE_HEX)
    assert int.from_bytes(raw[0:2], "little") == 4
    assert "0x" + raw[64:112].hex() == SYNTH_MRSEAM
    assert "0x" + raw[376:424].hex() == SYNTH_RTMR0
    assert "0x" + raw[424:472].hex() == SYNTH_RTMR1
    assert "0x" + raw[472:520].hex() == SYNTH_RTMR2
    assert "0x" + raw[520:568].hex() == SYNTH_RTMR3
    assert "0x" + raw[568:632].hex() == "0x" + EXPECTED_REPORT_DATA.hex()


# --------------------------------------------------------------------------
# bootstrap reachability + CORS + method surface
# --------------------------------------------------------------------------
def _bootstrap_tee_env(
    monkeypatch: pytest.MonkeyPatch, tmp_path, socket: str, *, origin: str
) -> None:
    """Shared-mode env pointing the socket at a fake dstack."""
    import relay.app.config as config

    monkeypatch.setenv("RELAY_MODE", "shared")
    monkeypatch.setenv("RELAY_PUBLIC_ORIGIN", origin)
    monkeypatch.setenv("SHARED_KEYSTORE_PATH", str(tmp_path / "keystore.json"))
    monkeypatch.setenv("SHARED_SIGNER_KEY", TEE_KEY)
    monkeypatch.setenv("SHARED_KEK_HEX", "0x" + "66" * 32)
    monkeypatch.setenv("SHARED_UPLOAD_KEY", "0x" + "77" * 32)
    monkeypatch.setenv("RPC_URL", "http://unused.invalid")
    monkeypatch.setenv("CHAIN_ID", "84532")
    monkeypatch.setenv("ESCROW_ADDR", "0x1111111111111111111111111111111111111111")
    monkeypatch.setenv("REGISTRY_ADDR", "0x2222222222222222222222222222222222222222")
    monkeypatch.setenv("USDC_ADDR", "0x3333333333333333333333333333333333333333")
    monkeypatch.delenv("RELAY_SELLER_KEY", raising=False)
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)
    monkeypatch.delenv("RELAY_CORS_ORIGINS", raising=False)
    monkeypatch.setattr(config, "DEFAULT_DSTACK_SOCKET", socket)


def test_verify_reachable_during_bootstrap(
    monkeypatch: pytest.MonkeyPatch, tmp_path
) -> None:
    """RELAY_BOOTSTRAP_MODE=1: /attestation/verify is whitelisted (the gate
    denies everything else) and still proxies the verifier."""
    import relay.app.main as main

    socket = tmp_path / "dstack.sock"
    socket.write_bytes(b"")
    _bootstrap_tee_env(
        monkeypatch,
        tmp_path,
        str(socket),
        origin="https://placeholder.invalid",
    )
    monkeypatch.setenv("RELAY_BOOTSTRAP_MODE", "1")
    FakeDstackClient.reset()
    _patch_fake_client(monkeypatch, SharedFakeDstackClient)
    monkeypatch.setattr(main, "_verify_transport", None)
    monkeypatch.setattr(main, "_verify_last_call", None)
    seen = _inject_verifier(monkeypatch, _ok)

    with TestClient(main.app) as client:
        health = client.get("/health")
        assert health.json()["bootstrap"] is True  # gate really is active
        # A non-whitelisted path still gets the exact bootstrap deny.
        denied = client.get("/verify-upstream")
        assert denied.status_code == 503
        assert denied.content == b'{"detail":"bootstrap_mode"}'

        resp = client.get("/attestation/verify")

    assert resp.status_code == 200
    body = resp.json()
    assert body["verified"] is True
    assert body["error"] is None
    assert len(seen) == 1
    assert str(seen[0].url) == "https://cloud-api.phala.com/api/v1/attestations/verify"


def test_verify_preflight_get_allowed(tee_client: Any) -> None:
    """CORS zero-change: a GET preflight for the verify route is allowed."""
    resp = tee_client.options(
        "/attestation/verify",
        headers={
            "Origin": "https://frontend.example",
            "Access-Control-Request-Method": "GET",
        },
    )
    assert resp.status_code == 200
    assert resp.headers.get("access-control-allow-origin") is not None


def test_verify_post_method_not_allowed(tee_client: Any) -> None:
    """GET-only surface (the relay picks its own fixed upstream; there is no
    caller-controlled URL to widen into POST)."""
    resp = tee_client.post("/attestation/verify")
    assert resp.status_code == 405


def test_verify_shared_mode_uses_signer_binding(
    monkeypatch: pytest.MonkeyPatch, tmp_path
) -> None:
    """Shared mode: the verified quote binds the shared signer + upload
    pubkey report data (the same rule as /attestation)."""
    import relay.app.main as main

    socket = tmp_path / "dstack.sock"
    socket.write_bytes(b"")
    _bootstrap_tee_env(
        monkeypatch, tmp_path, str(socket), origin="https://relay.example"
    )
    monkeypatch.delenv("RELAY_BOOTSTRAP_MODE", raising=False)
    FakeDstackClient.reset()
    _patch_fake_client(monkeypatch, SharedFakeDstackClient)
    monkeypatch.setattr(main, "_verify_transport", None)
    monkeypatch.setattr(main, "_verify_last_call", None)
    seen = _inject_verifier(monkeypatch, _ok)

    with TestClient(main.app) as client:
        attestation = client.get("/attestation").json()
        resp = client.get("/attestation/verify")
        upload_pub = main.state.upload_pub  # captured while state is alive

    assert resp.status_code == 200
    body = resp.json()
    assert body["verified"] is True
    # The posted quote is the shared-mode same-source quote.
    assert json.loads(seen[0].content) == {"hex": attestation["quote"]}
    signer = Account.from_key(TEE_KEY).address
    expected = bytes.fromhex(signer[2:]) + b"\x00" * 12 + keccak(upload_pub)
    assert attestation["quoteBody"]["reportdata"] == "0x" + expected.hex()
    assert attestation["reportData"] == "0x" + expected.hex()
