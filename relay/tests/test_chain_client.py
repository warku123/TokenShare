"""Fix #3: ChainClient must refuse to start when the RPC node reports a
chainId different from the configured CHAIN_ID (receipt domain / settle chain
mismatch). Proven against a local stub JSON-RPC server — no real chain."""

from __future__ import annotations

import json
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any

import pytest

from relay.app.chain import ChainClient
from relay.app.config import ConfigError

from .conftest import CHAIN_ID, SELLER_KEY


class StubRPCServer(ThreadingHTTPServer):
    daemon_threads = True

    def __init__(self, address: Any, chain_id: int) -> None:
        super().__init__(address, StubRPC)
        self.chain_id = chain_id


class StubRPC(BaseHTTPRequestHandler):
    """Minimal JSON-RPC: web3_clientVersion (is_connected) + eth_chainId."""

    def log_message(self, *args: Any) -> None:
        pass

    def do_POST(self) -> None:
        length = int(self.headers.get("Content-Length", "0"))
        body = json.loads(self.rfile.read(length) if length else b"{}")
        method = body.get("method", "")
        result = (
            "FakeRPC/1.0" if method == "web3_clientVersion"
            else hex(self.server.chain_id)  # type: ignore[attr-defined]
        )
        payload = json.dumps(
            {"jsonrpc": "2.0", "id": body.get("id", 1), "result": result}
        ).encode()
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(payload)))
        self.end_headers()
        self.wfile.write(payload)


@pytest.fixture()
def stub_rpc() -> ThreadingHTTPServer:
    srv = StubRPCServer(("127.0.0.1", 0), CHAIN_ID)
    thread = threading.Thread(target=srv.serve_forever, daemon=True)
    thread.start()
    yield srv
    srv.shutdown()
    srv.server_close()


def _client_kwargs(url: str) -> dict[str, Any]:
    return {
        "rpc_url": url,
        "escrow_addr": "0x1111111111111111111111111111111111111111",
        "registry_addr": "0x2222222222222222222222222222222222222222",
        "seller_key": SELLER_KEY,
    }


def test_chain_id_match_starts_ok(stub_rpc: ThreadingHTTPServer) -> None:
    url = f"http://127.0.0.1:{stub_rpc.server_port}"
    cc = ChainClient(chain_id=CHAIN_ID, **_client_kwargs(url))
    assert cc.seller_address


def test_chain_id_mismatch_refuses(stub_rpc: ThreadingHTTPServer) -> None:
    url = f"http://127.0.0.1:{stub_rpc.server_port}"
    with pytest.raises(ConfigError, match="chainId"):
        ChainClient(chain_id=1, **_client_kwargs(url))
