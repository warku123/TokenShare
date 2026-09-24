"""Concurrency smoke: ≥2 simultaneous requests must all complete without the
event loop blocking on chain reads (fix #1 — chain reads off the loop)."""

from __future__ import annotations

import asyncio
import json
from typing import Any

import httpx
from fastapi.testclient import TestClient  # noqa: F401  (ensures env import)

import relay.app.main as m

from .conftest import BUYER_KEY, setup_relay_env, signed_headers, chat_body


def test_concurrent_requests_settle_independently(
    monkeypatch: Any, fake_chain: Any, mock_openai: Any
) -> None:
    setup_relay_env(monkeypatch, f"http://127.0.0.1:{mock_openai.server_port}/v1")

    async def run() -> list[tuple[int, str]]:
        async with m.lifespan(m.app):
            transport = httpx.ASGITransport(app=m.app)
            async with httpx.AsyncClient(
                transport=transport, base_url="http://relay.test"
            ) as ac:
                from eth_account import Account
                from eth_account.messages import encode_defunct

                async def one(payment_id: str) -> tuple[int, str]:
                    raw = chat_body().replace(b'"hi"', f'"q{payment_id}"'.encode())
                    msg = (f"POST|/v1/chat/completions|"
                           f"{__import__('hashlib').sha256(raw).hexdigest()}|{payment_id}")
                    sig = Account.from_key(BUYER_KEY).sign_message(
                        encode_defunct(text=msg)
                    ).signature.hex()
                    r = await ac.post(
                        "/v1/chat/completions",
                        content=raw,
                        headers={"X-Payment-Id": payment_id, "X-Signature": sig,
                                 "Content-Type": "application/json"},
                    )
                    return r.status_code, r.headers.get("X-Settle-Status", "")

                results = await asyncio.gather(one("61"), one("62"), one("63"))
                return results

    status_codes = asyncio.run(run())
    assert status_codes == [(200, "settled")] * 3
    settled_ids = sorted(pid for pid, _ in fake_chain.settle_calls)
    assert settled_ids == [61, 62, 63]


# --------------------------------------------------------------------------
# Real ChainClient settle race (fix C1): shared seller nonce. Two settles must
# never send the same nonce; a node-side nonce-conflict rejection must be
# retried once with a freshly fetched nonce. Node simulated in-process — no
# RPC, no chain.
# --------------------------------------------------------------------------
import threading  # noqa: E402
import time  # noqa: E402
from types import SimpleNamespace  # noqa: E402

from web3.exceptions import Web3RPCError  # noqa: E402

from relay.app.chain import ChainClient  # noqa: E402

from .conftest import CHAIN_ID, SELLER  # noqa: E402

_NONCE_TOO_LOW = Web3RPCError(
    {"jsonrpc": "2.0", "id": 1,
     "error": {"code": -32000, "message": "nonce too low"}}
)


class _FakeNodeEth:
    """eth_* surface with 'latest'-based getTransactionCount semantics (the
    web3 default): a tx only bumps the count once mined (synchronously here).
    Sends below the mined count are rejected like a real node."""

    def __init__(self) -> None:
        self.mined_count = 5
        self.sent_nonces: list[tuple[int, int]] = []  # (nonce, payment_id)
        self.rejected_nonces: list[int] = []
        self.receipts: dict[bytes, dict[str, Any]] = {}
        self._next_hash = 0
        # Test hook: force the NEXT count read to return this stale value
        # (simulates the pre-fix race artifact: a settle built off an old nonce).
        self.force_stale_once: int | None = None

    @property
    def chain_id(self) -> int:
        return CHAIN_ID

    def get_transaction_count(self, _addr: str) -> int:
        if self.force_stale_once is not None:
            stale = self.force_stale_once
            self.force_stale_once = None
            return stale
        return self.mined_count

    def get_block(self, _tag: str) -> dict[str, Any]:
        return {"baseFeePerGas": 1_000_000_000}  # EIP-1559 node

    @property
    def gas_price(self) -> int:
        return 1_000_000_000

    def send_raw_transaction(self, raw: bytes) -> bytes:
        tx = json.loads(bytes(raw).decode())
        nonce = int(tx["nonce"])
        if nonce < self.mined_count:
            self.rejected_nonces.append(nonce)
            raise _NONCE_TOO_LOW
        if nonce > self.mined_count:
            raise AssertionError(f"gap nonce {nonce} sent (expected {self.mined_count})")
        self.mined_count += 1
        self._next_hash += 1
        tx_hash = self._next_hash.to_bytes(1, "big")
        self.sent_nonces.append((nonce, int(tx["paymentId"])))
        self.receipts[tx_hash] = {"transactionHash": tx_hash, "status": 1}
        return tx_hash

    def wait_for_transaction_receipt(self, tx_hash: bytes) -> dict[str, Any]:
        return self.receipts[tx_hash]


class _FakeEscrow:
    """escrow.functions.settle(...).build_transaction(...); records attempts."""

    def __init__(self, node: _FakeNodeEth) -> None:
        self.node = node
        self.attempts: list[tuple[int, int, int]] = []  # (nonce, pid, amount)

    @property
    def functions(self) -> "_FakeEscrow":
        return self

    def settle(self, payment_id: int, actual_amount: int) -> "_FakeEscrow":
        self._pending = (payment_id, actual_amount)
        return self

    def build_transaction(self, params: dict[str, Any]) -> dict[str, Any]:
        pid, amount = self._pending
        self.attempts.append((int(params["nonce"]), pid, amount))
        return {"nonce": params["nonce"], "paymentId": pid, "data": "settle"}

    # signer needs paymentId in the raw tx → keep it in the built dict


class _FakeSigner:
    """Encodes the tx dict as 'raw' bytes so the fake node can read the nonce."""

    address = SELLER

    def __init__(self, node: _FakeNodeEth) -> None:
        self.node = node
        self.inside_sign = 0
        self.peak_sign = 0
        self._lock = threading.Lock()
        self.first_sign_delay_s = 0.0

    def sign_transaction(self, tx: dict[str, Any]) -> Any:
        with self._lock:
            self.inside_sign += 1
            self.peak_sign = max(self.peak_sign, self.inside_sign)
        try:
            if self.first_sign_delay_s:
                time.sleep(self.first_sign_delay_s)
                self.first_sign_delay_s = 0.0
            return SimpleNamespace(
                raw_transaction=json.dumps(
                    {"nonce": int(tx["nonce"]), "paymentId": int(tx["paymentId"])}
                ).encode()
            )
        finally:
            with self._lock:
                self.inside_sign -= 1


def _wired_client() -> tuple[ChainClient, _FakeNodeEth, _FakeEscrow]:
    """ChainClient instance with every network edge replaced by in-process
    fakes (construction is bypassed — the stub-RPC/chainId guard is covered
    by test_chain_client.py)."""
    node = _FakeNodeEth()
    escrow = _FakeEscrow(node)
    cc = ChainClient.__new__(ChainClient)
    cc._w3 = SimpleNamespace(eth=node)  # type: ignore[attr-defined]
    cc.escrow = escrow  # type: ignore[attr-defined]
    cc._account = _FakeSigner(node)  # type: ignore[attr-defined]
    cc.seller_address = SELLER  # type: ignore[attr-defined]
    return cc, node, escrow


def test_settle_nonce_conflict_retried_once_and_both_settled() -> None:
    """First settle succeeds; the second is forced to send with the OLD nonce
    (the pre-fix race artifact). The node rejects it ('nonce too low'), settle
    refetches + resigns + resends exactly once, and BOTH payments settle."""
    cc, node, escrow = _wired_client()

    r1 = cc.settle(61, 100)
    assert r1["status"] == 1
    assert node.sent_nonces == [(5, 61)]

    # Inject the race artifact: the next nonce read returns the stale value.
    node.force_stale_once = 5
    r2 = cc.settle(62, 100)
    assert r2["status"] == 1

    # attempt log: (5, 61), (5, 62 stale, rejected), (6, 62 retry)
    assert escrow.attempts == [(5, 61, 100), (5, 62, 100), (6, 62, 100)]
    assert node.sent_nonces == [(5, 61), (6, 62)]
    assert node.rejected_nonces == [5]  # exactly one rejection, one retry


def test_concurrent_settles_never_share_sign_window() -> None:
    """Two threads settling at once: the sign window is entered by one thread
    at a time (module-level lock), so the node never sees a duplicate nonce
    and no retry is even needed."""
    cc, node, escrow = _wired_client()
    signer = cc._account
    assert isinstance(signer, _FakeSigner)
    signer.first_sign_delay_s = 0.3  # widen window 1 so an unlocked impl fails

    results: list[tuple[int, dict[str, Any] | str]] = []

    def run(payment_id: int) -> None:
        try:
            results.append((payment_id, cc.settle(payment_id, 100)))
        except Exception as exc:  # noqa: BLE001 — surfaced via assertion below
            results.append((payment_id, f"{type(exc).__name__}: {exc}"))

    threads = [threading.Thread(target=run, args=(p,)) for p in (71, 72)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    assert len(results) == 2
    assert all(isinstance(r[1], dict) and r[1]["status"] == 1 for r in results)
    assert signer.peak_sign == 1  # sign windows strictly serialized
    assert node.rejected_nonces == []  # lock ⇒ no duplicate-nonce sends
    assert sorted(n for n, _ in node.sent_nonces) == [5, 6]
    # Every payment settled exactly once with the nonce it actually used.
    assert sorted((pid, nonce) for nonce, pid in node.sent_nonces) == sorted(
        (attempt[1], attempt[0]) for attempt in escrow.attempts
    )
