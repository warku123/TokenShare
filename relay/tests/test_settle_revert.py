"""fix-34: settle/settlePartial gas must be estimate-first (Escrow v3 settle
with the protocol fee measured 132,608 gas — the old flat 120k out-of-gas-
reverted on-chain; settlePartial 104,042 was borderline), and _sign_send_wait
must check receipt.status — a MINED-but-reverted tx (status 0) raised as
SettleError must flow into the existing settle-failed semantics instead of a
fake X-Settle-Status: settled.

Two layers:
  1. ChainClient against a local stub JSON-RPC (asserts the gas VALUE put on
     the signed tx — decode via legacy-RLP — and the SettleError path).
  2. App-level FakeChain revert injection → caller settle-failed branches.
"""

from __future__ import annotations

import json
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any

import pytest
from eth_account._utils.legacy_transactions import Transaction
from fastapi.testclient import TestClient
from rlp import decode as rlp_decode

from relay.app.chain import (
    ChainClient,
    SettleError,
    _encode_settle_partial_calldata,
    _SETTLE_GAS_BUFFER,
    _SETTLE_GAS_FALLBACK,
    _SETTLE_GAS_HEADROOM,
)

from .conftest import (
    CHAIN_ID,
    SELLER_KEY,
    bearer_headers,
    chat_body,
    mint_api_key,
    mock_openai_url,
    post_chat,
    setup_relay_env,
)

# Escrow v3 settle with the protocol fee — measured on-chain (fix-34 e2e).
REAL_SETTLE_GAS = 132_608

# Raw gas estimate the stub reports for eth_estimateGas (≈ the e2e figure).
STUB_ESTIMATE = 100_000
# gas_for philosophy: ×1.3 + 20k headroom → 150_000 for a 100k estimate.
EXPECTED_SCALED = int(STUB_ESTIMATE * _SETTLE_GAS_HEADROOM) + _SETTLE_GAS_BUFFER


# ---------------------------------------------------------------------------
# Stub JSON-RPC: full settle flow (nonce/estimate/block/send/receipt).
# The latest block carries NO baseFeePerGas → _sign_send_wait takes the
# legacy gasPrice branch → legacy RLP tx → gas field decodable deterministically.
# ---------------------------------------------------------------------------
class SettleStubServer(ThreadingHTTPServer):
    daemon_threads = True

    def __init__(self, address: Any) -> None:
        super().__init__(address, SettleStub)
        # Per-test knobs (reset by the fixture):
        self.estimate_result: int = STUB_ESTIMATE
        self.estimate_error: str | None = None  # when set → JSON-RPC error
        self.receipt_status: str = "0x1"  # "0x0" → mined-but-reverted
        self.sent_raw: list[bytes] = []
        self.estimate_probes: list[dict[str, Any]] = []
        self.send_count: int = 0

    def reset(self) -> None:
        self.estimate_result = STUB_ESTIMATE
        self.estimate_error = None
        self.receipt_status = "0x1"
        self.sent_raw = []
        self.estimate_probes = []
        self.send_count = 0


class SettleStub(BaseHTTPRequestHandler):
    def log_message(self, *args: Any) -> None:
        pass

    def _reply(self, body: dict[str, Any]) -> None:
        payload = json.dumps(body).encode()
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(payload)))
        self.end_headers()
        self.wfile.write(payload)

    def do_POST(self) -> None:
        length = int(self.headers.get("Content-Length", "0"))
        body = json.loads(self.rfile.read(length) if length else b"{}")
        method = body.get("method", "")
        req_id = body.get("id", 1)
        server: SettleStubServer = self.server  # type: ignore[assignment]
        params = body.get("params") or []

        if method == "web3_clientVersion":
            result: Any = "FakeRPC/1.0"
        elif method == "eth_chainId":
            result = hex(CHAIN_ID)
        elif method == "eth_getTransactionCount":
            result = "0x7"
        elif method == "eth_gasPrice":
            result = hex(1_000_000_000)
        elif method == "eth_estimateGas":
            server.estimate_probes.append(params[0] if params else {})
            if server.estimate_error is not None:
                self._reply({
                    "jsonrpc": "2.0",
                    "id": req_id,
                    "error": {"code": -32000, "message": server.estimate_error},
                })
                return
            result = hex(server.estimate_result)
        elif method == "eth_getBlockByNumber":
            # No baseFeePerGas → legacy gasPrice tx path in _sign_send_wait.
            result = {
                "number": "0x1",
                "hash": "0x" + "31" * 32,
                "parentHash": "0x" + "0" * 64,
                "nonce": "0x" + "0" * 16,
                "sha3Uncles": "0x" + "0" * 64,
                "logsBloom": "0x" + "0" * 256,
                "transactionsRoot": "0x" + "0" * 64,
                "stateRoot": "0x" + "0" * 64,
                "receiptsRoot": "0x" + "0" * 64,
                "miner": "0x" + "0" * 40,
                "difficulty": "0x0",
                "totalDifficulty": "0x0",
                "extraData": "0x",
                "size": "0x0",
                "gasLimit": "0x47e7c4",
                "gasUsed": "0x0",
                "timestamp": "0x0",
                "transactions": [],
                "uncles": [],
            }
        elif method == "eth_sendRawTransaction":
            raw_hex = params[0]
            server.sent_raw.append(bytes.fromhex(raw_hex[2:]))
            server.send_count += 1
            result = "0x" + "ab" * 32
        elif method == "eth_getTransactionReceipt":
            result = {
                "transactionHash": "0x" + "ab" * 32,
                "transactionIndex": "0x0",
                "blockNumber": "0x1",
                "blockHash": "0x" + "32" * 32,
                "cumulativeGasUsed": hex(REAL_SETTLE_GAS),
                "gasUsed": hex(REAL_SETTLE_GAS),
                "effectiveGasPrice": hex(1_000_000_000),
                "from": "0x" + "0" * 40,
                "to": "0x" + "11" * 20,
                "contractAddress": None,
                "logs": [],
                "logsBloom": "0x" + "0" * 256,
                "status": server.receipt_status,
                "type": "0x0",
            }
        else:
            self._reply({
                "jsonrpc": "2.0",
                "id": req_id,
                "error": {"code": -32601, "message": f"unhandled {method}"},
            })
            return
        self._reply({"jsonrpc": "2.0", "id": req_id, "result": result})


@pytest.fixture()
def settle_rpc() -> SettleStubServer:
    srv = SettleStubServer(("127.0.0.1", 0))
    thread = threading.Thread(target=srv.serve_forever, daemon=True)
    thread.start()
    yield srv
    srv.shutdown()
    srv.server_close()


@pytest.fixture(autouse=True)
def fresh_stub(settle_rpc: SettleStubServer) -> None:
    settle_rpc.reset()


def _client(settle_rpc: SettleStubServer) -> ChainClient:
    return ChainClient(
        rpc_url=f"http://127.0.0.1:{settle_rpc.server_port}",
        escrow_addr="0x1111111111111111111111111111111111111111",
        registry_addr="0x2222222222222222222222222222222222222222",
        seller_key=SELLER_KEY,
        chain_id=CHAIN_ID,
    )


def _sent_gas(settle_rpc: SettleStubServer) -> int:
    """gas field of the (legacy) signed tx that hit eth_sendRawTransaction."""
    assert len(settle_rpc.sent_raw) == 1
    signed = rlp_decode(settle_rpc.sent_raw[0], Transaction)
    return int(signed.gas)


# ------------------------------------------------- ChainClient: estimate gas

def test_settle_gas_estimate_scaled_and_sent(
    settle_rpc: SettleStubServer,
) -> None:
    """Estimate-first: tx carries estimate ×1.3 + 20k (100k → 150k), NOT the
    old flat 120k that out-of-gas-reverted Escrow v3 settles."""
    receipt = _client(settle_rpc).settle(42, 315)
    assert int(receipt["status"]) == 1
    assert _sent_gas(settle_rpc) == EXPECTED_SCALED
    assert EXPECTED_SCALED > REAL_SETTLE_GAS  # the v3-measured cost fits


def test_settle_estimate_probe_has_no_gas_field(
    settle_rpc: SettleStubServer,
) -> None:
    """The eth_estimateGas probe is sent WITHOUT a gas field (the value being
    asked for) and targets the Escrow address."""
    _client(settle_rpc).settle(42, 315)
    assert len(settle_rpc.estimate_probes) == 1
    probe = settle_rpc.estimate_probes[0]
    assert "gas" not in probe
    assert probe["to"] == "0x1111111111111111111111111111111111111111"
    assert probe["from"]  # seller sender present


def test_settle_gas_estimate_failure_falls_back_to_300k(
    settle_rpc: SettleStubServer,
) -> None:
    """Estimate failure (RPC hiccup / revert-at-estimate) → WARNING fallback
    cap 300k — still ≥ the v3-measured 132,608, never the old flat 120k."""
    settle_rpc.estimate_error = "execution reverted"
    receipt = _client(settle_rpc).settle(42, 315)
    assert int(receipt["status"]) == 1
    assert _sent_gas(settle_rpc) == _SETTLE_GAS_FALLBACK
    assert _sent_gas(settle_rpc) > REAL_SETTLE_GAS


def test_settle_partial_gas_estimate_scaled_and_sent(
    settle_rpc: SettleStubServer,
) -> None:
    """settlePartial (pre-encoded calldata path) — same estimate-first gas;
    the calldata on the wire is the PIN-encoded settlePartial."""
    receipt = _client(settle_rpc).settle_partial(42, 315)
    assert int(receipt["status"]) == 1
    assert _sent_gas(settle_rpc) == EXPECTED_SCALED
    # Calldata round-trip: decode the signed legacy tx's data field.
    signed = rlp_decode(settle_rpc.sent_raw[0], Transaction)
    assert bytes(signed.data).hex() == _encode_settle_partial_calldata(42, 315)[2:]


# -------------------------------------------- ChainClient: receipt status 0

def test_settle_reverted_receipt_raises_settle_error(
    settle_rpc: SettleStubServer,
) -> None:
    """Mined but reverted (status 0) → SettleError naming the tx hash and
    status — never a fake-success receipt return."""
    settle_rpc.receipt_status = "0x0"
    with pytest.raises(SettleError, match="receipt.status=0"):
        _client(settle_rpc).settle(42, 315)


def test_settle_partial_reverted_receipt_raises_settle_error(
    settle_rpc: SettleStubServer,
) -> None:
    settle_rpc.receipt_status = "0x0"
    with pytest.raises(SettleError, match="receipt.status=0"):
        _client(settle_rpc).settle_partial(42, 315)


def test_settle_revert_is_not_retried_as_nonce_conflict(
    settle_rpc: SettleStubServer,
) -> None:
    """A reverted receipt is NOT a same-nonce rejection: exactly ONE tx is
    sent — the settle()-level nonce-conflict retry must not mask it."""
    settle_rpc.receipt_status = "0x0"
    with pytest.raises(SettleError):
        _client(settle_rpc).settle(42, 315)
    assert settle_rpc.send_count == 1


# ------------------------------------- App level: revert → settle-failed face

def test_legacy_settle_reverted_is_settle_failed(
    client: TestClient, fake_chain: Any
) -> None:
    """On-chain revert (status 0) on the legacy path → LLM response intact +
    X-Settle-Status: settle-failed + no X-Receipt (buyer can refund)."""
    fake_chain.settle_receipt_status = 0
    r = post_chat(client, chat_body())
    assert r.status_code == 200
    assert r.json()["choices"][0]["message"]["content"] == "hi"
    assert r.headers["X-Settle-Status"] == "settle-failed"
    assert "X-Receipt" not in r.headers
    assert fake_chain.settle_calls == [(42, 315)]  # tx WAS sent and mined
    assert client.get("/receipt/42").status_code == 404


def test_bearer_flush_reverted_is_partial_flush_failed(
    client: TestClient, fake_chain: Any
) -> None:
    """Immediate synchronous flush with a reverted settlePartial receipt →
    partial-flush-failed, response + X-Receipt kept (flush retries later)."""
    fake_chain.ttl_delta = 60  # ttl below FORWARD_MARGIN_S → immediate flush
    fake_chain.settle_partial_receipt_status = 0
    r = client.post(
        "/v1/chat/completions",
        content=chat_body(),
        headers=bearer_headers(mint_api_key()),
    )
    assert r.status_code == 200
    assert r.json()["choices"][0]["message"]["content"] == "hi"
    assert r.headers["X-Settle-Status"] == "partial-flush-failed"
    assert "X-Receipt" in r.headers  # partial path always issues the receipt
    assert fake_chain.settle_partial_calls == [(42, 315)]
    assert fake_chain.settle_calls == []  # legacy settle untouched
