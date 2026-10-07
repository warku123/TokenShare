"""M15 R1 shared-mode surface tests — dstack-derived shared keys, the
/info /health /attestation shared branches (report_data = signer20 + 12
zero bytes + keccak256(upload pubkey 65B)), and the legacy-endpoint guards
in shared mode. Single-mode TEE behavior stays covered by test_tee.py."""

from __future__ import annotations

import types
from typing import Any

import pytest
from eth_account import Account
from eth_utils import keccak
from fastapi.testclient import TestClient

from . import custody_utils as cu
from relay.app.config import SHARED_SIGNER_KEY_PATH

# The dstack-derived shared keys (deterministic, valueless).
TEE_SIGNER_KEY = "0x" + "aa" * 32
TEE_SIGNER_ADDR = Account.from_key(TEE_SIGNER_KEY).address
TEE_KEK = bytes.fromhex("bb" * 32)
TEE_UPLOAD_RAW = "cc" * 32

SHARED_PATHS = (
    "tokenshare/shared/signer/v1",
    "tokenshare/shared/kek/v1",
    "tokenshare/shared/upload/v1",
)


class SharedDstackClient:
    """Fake dstack client for the THREE bare legacy get_key paths."""

    instances: list["SharedDstackClient"] = []
    last_report_data: bytes | None = None

    def __init__(self, endpoint: str | None = None) -> None:
        self.endpoint = endpoint
        type(self).instances.append(self)

    @classmethod
    def reset(cls) -> None:
        cls.instances = []
        cls.last_report_data = None

    def get_key(self, path: str) -> Any:
        material = {
            SHARED_PATHS[0]: TEE_SIGNER_KEY,
            SHARED_PATHS[1]: "0x" + TEE_KEK.hex(),
            SHARED_PATHS[2]: "0x" + TEE_UPLOAD_RAW,
        }[path]
        return types.SimpleNamespace(decode_key=lambda: material)

    def get_quote(self, report_data: bytes) -> Any:
        type(self).last_report_data = bytes(report_data)
        return types.SimpleNamespace(quote="0x" + "ef" * 300, event_log="")

    def info(self) -> Any:
        return types.SimpleNamespace(app_id="0x" + "cd" * 8, vm_id="vm-1")


@pytest.fixture
def shared_tee_client(monkeypatch: pytest.MonkeyPatch, tmp_path, mock_openai) -> TestClient:
    import relay.app.config as config
    import relay.app.main as main

    cu.setup_shared_env(monkeypatch, str(tmp_path / "keystore.json"))
    monkeypatch.delenv("SHARED_SIGNER_KEY", raising=False)
    monkeypatch.delenv("SHARED_KEK_HEX", raising=False)
    monkeypatch.delenv("SHARED_UPLOAD_KEY", raising=False)
    socket = tmp_path / "dstack.sock"
    socket.write_bytes(b"")
    monkeypatch.setattr(config, "DEFAULT_DSTACK_SOCKET", str(socket))
    SharedDstackClient.reset()
    monkeypatch.setattr(config, "dstack_client", SharedDstackClient)
    monkeypatch.setattr(main, "dstack_client", SharedDstackClient)
    with TestClient(main.app) as c:
        yield c


def test_shared_dstack_boot_health_and_info(
    monkeypatch: pytest.MonkeyPatch, tmp_path, mock_openai
) -> None:
    import relay.app.config as config
    import relay.app.main as main

    cu.setup_shared_env(monkeypatch, str(tmp_path / "keystore.json"))
    for name in ("SHARED_SIGNER_KEY", "SHARED_KEK_HEX", "SHARED_UPLOAD_KEY"):
        monkeypatch.delenv(name, raising=False)
    socket = tmp_path / "dstack.sock"
    socket.write_bytes(b"")
    monkeypatch.setattr(config, "DEFAULT_DSTACK_SOCKET", str(socket))
    SharedDstackClient.reset()
    monkeypatch.setattr(config, "dstack_client", SharedDstackClient)
    monkeypatch.setattr(main, "dstack_client", SharedDstackClient)
    with TestClient(main.app) as client:
        health = client.get("/health").json()
        assert health["status"] == "ok"
        assert health["mode"] == "shared"
        assert health["signer"] == TEE_SIGNER_ADDR.lower() or health["signer"] == TEE_SIGNER_ADDR
        assert health["keystoreEntries"] == 0

        info = client.get("/info").json()
        assert info["mode"] == "shared"
        assert info["shared"]["origin"] == cu.DEFAULT_ORIGIN
        assert info["shared"]["signer"] == TEE_SIGNER_ADDR.lower() or info["shared"]["signer"] == TEE_SIGNER_ADDR
        assert info["shared"]["keystoreEntries"] == 0
        # uploadPubkey: base64url(65 B uncompressed) + sha256 hex(64).
        from relay.app import custody

        raw_pub = custody.b64url_decode_strict(info["shared"]["uploadPubkey"], 65, "t")
        assert raw_pub[0] == 0x04
        assert info["shared"]["uploadPubkeySha256"] == custody.sha256_hex(raw_pub)
        # The upload scalar is the raw32 MAPPING (not the direct value).
        from relay.app.config import P256_ORDER

        expected = (int.from_bytes(bytes.fromhex(TEE_UPLOAD_RAW), "big") % (P256_ORDER - 1)) + 1
        assert custody.upload_public_bytes(expected) == raw_pub
        # Legacy fields retained.
        assert info["chainId"] == 84532
        assert info["officialUpstreamHosts"]


def test_shared_attestation_report_data(shared_tee_client: TestClient) -> None:
    resp = shared_tee_client.get("/attestation")
    assert resp.status_code == 200
    body = resp.json()
    assert body["tee"] is True
    assert body["keyPath"] == SHARED_SIGNER_KEY_PATH
    assert body["derivedAddress"].lower() == TEE_SIGNER_ADDR.lower()
    # 64 B = signer(20) + zero(12) + keccak256(upload pub 65).
    from relay.app import custody
    from relay.app.config import P256_ORDER

    report = bytes.fromhex(body["reportData"][2:])
    assert len(report) == 64
    assert report[:20] == bytes.fromhex(TEE_SIGNER_ADDR.lower()[2:])
    assert report[20:32] == b"\x00" * 12
    # keccak part matches the derived upload pubkey.
    upload_scalar = (int.from_bytes(bytes.fromhex(TEE_UPLOAD_RAW), "big") % (P256_ORDER - 1)) + 1
    pub = custody.upload_public_bytes(upload_scalar)
    assert report[32:] == keccak(pub)
    # The quote request carried EXACTLY this report data.
    assert SharedDstackClient.last_report_data == report
    assert body["quote"] == "0x" + "ef" * 300
    assert body["quoteDigest"] == "0x" + keccak(bytes.fromhex("ef" * 300)).hex()


def test_shared_attestation_without_socket_404(
    monkeypatch: pytest.MonkeyPatch, tmp_path, mock_openai
) -> None:
    import relay.app.main as main

    cu.setup_shared_env(monkeypatch, str(tmp_path / "keystore.json"))
    with TestClient(main.app) as client:
        resp = client.get("/attestation")
        assert resp.status_code == 404


def test_shared_legacy_endpoints_guarded(
    monkeypatch: pytest.MonkeyPatch, tmp_path, mock_openai
) -> None:
    """R2 revision of the R1 guard face: chat now SERVES the shared buyer
    flow (it reaches the chain/buyer gates — here: payment 1 unknown on the
    fake chain face) and usage/revoke WORK in shared mode via the lazy
    chain; the single-only seller tools (/verify-upstream, /preview-models)
    stay 503-guarded. Custody endpoints are unaffected."""
    cu.setup_shared_env(monkeypatch, str(tmp_path / "keystore.json"))
    import relay.app.main as main

    with TestClient(main.app) as client:
        # Single-only seller tools remain 503.
        assert client.get("/verify-upstream").status_code == 503
        assert client.post("/preview-models", content=b"{}").status_code == 503
        # Buyer-facing endpoints now serve the shared flow (not 503): the
        # payment read happens on the (fake) chain — an unknown payment is a
        # chain-domain error face, NOT a mode guard.
        chat = client.post(
            "/v1/chat/completions",
            content=b"{}",
            headers={"Content-Type": "application/json"},
        )
        assert chat.status_code != 503
        usage = client.get("/payment/1/usage")
        assert usage.status_code != 503
        revoke = client.post("/payment/1/revoke", content=b"{}")
        assert revoke.status_code != 503


def test_shared_keystore_entries_reflected(
    shared_tee_client: TestClient, mock_openai
) -> None:
    """Enrollments surface as keystoreEntries in /health and /info."""
    from relay.app.config import P256_ORDER

    # The TEE-derived upload scalar = raw32 mapping of "cc"*32.
    upload_scalar = (int.from_bytes(bytes.fromhex(TEE_UPLOAD_RAW), "big") % (P256_ORDER - 1)) + 1
    upstream = f"http://127.0.0.1:{mock_openai.server_port}"
    rec = cu.fresh_record(shared_tee_client)
    raw = cu.submit_payload(
        rec, upstream, "sk-A", upload_scalar="0x" + format(upload_scalar, "064x")
    )
    msg = cu.submit_message_for(rec, raw, upstream, cu.WALLET)
    resp = shared_tee_client.post(
        "/sellers/keys", content=raw, headers=cu.wallet_headers(msg, seller=cu.WALLET)
    )
    assert resp.status_code == 200, resp.text
    assert shared_tee_client.get("/health").json()["keystoreEntries"] == 1
    assert shared_tee_client.get("/info").json()["shared"]["keystoreEntries"] == 1
