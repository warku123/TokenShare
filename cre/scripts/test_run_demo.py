#!/usr/bin/env python3
"""OFFLINE regression tests for cre/scripts/run_demo.py (ora37 findings F1-F6).

Hard rules honored by this suite:
  - ZERO network, ZERO transactions, ZERO keys. All "chain" interaction is
    fake stubs; the pinned secret-based paths are exercised only via fake
    signing objects. The pinned receipt fixture is public data and its
    EIP-712 recovery is pure offline cryptography.
  - Evidence writes are redirected to a per-test temp directory; the real
    cre-demo evidence dir is never touched.

Run: python -m unittest cre.scripts.test_run_demo -v  (or directly).
"""

from __future__ import annotations

import importlib.util
import inspect
import json
import tempfile
import types
import unittest
import urllib.error
from pathlib import Path

_MODULE_PATH = Path(__file__).resolve().parent / "run_demo.py"
_spec = importlib.util.spec_from_file_location("run_demo_under_test", _MODULE_PATH)
rd = importlib.util.module_from_spec(_spec)
assert _spec is not None and _spec.loader is not None
_spec.loader.exec_module(rd)

from eth_abi import encode  # noqa: E402  (offline, pure)
from web3 import Web3  # noqa: E402
from hexbytes import HexBytes  # noqa: E402

W = Web3()  # offline instance — keccak + contract event decoding only


def h32(i: int = 0, byte: int = 0x11) -> str:
    return "0x" + (bytes([byte]) * 32 + bytes([i & 0xFF])).hex()


def pad_uint(v: int) -> str:
    return "0x" + v.to_bytes(32, "big").hex()


def pad_addr(addr: str) -> str:
    return "0x" + (b"\x00" * 12 + bytes.fromhex(addr.removeprefix("0x"))).hex()


# ---------------------------------------------------------------- fixtures

REAL_ARTIFACT, REAL_ARTIFACT_SHA = rd.load_anchor_artifact()


def pinned_receipt_json() -> dict:
    """The exact pinned (public) receipt content the relay returns for p5."""
    return {
        "domain": {"name": "TokenShare Relay", "version": "1", "chainId": 10143},
        "message": {
            "paymentId": 5,
            "promptTokens": 93,
            "cachedTokens": 0,
            "completionTokens": 40,
            "actualAmount": 918,
            "seller": rd.EXPECTED_SELLER,
            "upstreamHost": rd.EXPECTED_HOST,
            "model": rd.EXPECTED_MODEL,
        },
        "signature": rd.EXPECTED_SIGNATURE,
    }


PINNED_RAW = json.dumps(pinned_receipt_json()).encode("utf-8")
# Pinned settle-tx trigger-block timestamp (from chain, block 69225090) — the
# preserved preflight evidence written_at (1791453374) is 48s earlier.
PINNED_SETTLE_BLOCK_TS = 1791453422


def valid_preflight_evidence(written_at: int = 1791453374, **over) -> dict:
    """Full provenance-valid preflight evidence for the explicit crash-
    recovery baseline (all fields the runner validates)."""
    doc = {
        "stage": "preflight",
        "written_at_unix": written_at,
        "network": {"rpc": rd.RPC_URL, "chain_id": rd.CHAIN_ID},
        "escrow": rd.ESCROW,
        "registry": rd.REGISTRY,
        "payment": {
            "escrow": rd.ESCROW,
            "payment_id": 5,
            "buyer": rd.EXPECTED_BUYER,
            "seller": rd.EXPECTED_SELLER,
            "max_amount": 1_500_000,
            "expires_at": 1_791_432_001,
            "state": 1,
            "captured": 918,
            "fee_bps": 0,
        },
        "receipt_http_status": 200,
        "receipt_snapshot": rd.receipt_snapshot(
            {"status": 200, "raw": PINNED_RAW, "json": pinned_receipt_json()}),
    }
    for path, val in over.items():
        # "payment.captured" style dotted override for negative tests
        parts = path.split(".")
        node = doc
        for part in parts[:-1]:
            node = node[part]
        node[parts[-1]] = val
    return doc


def settled_receipt(refund: int = 1_499_082, buyer: str = rd.EXPECTED_BUYER,
                    seller: str = rd.EXPECTED_SELLER, status: int = 1,
                    extra_settled: bool = False,
                    noise_prefix: bool = True,
                    include_settled: bool = True) -> dict:
    """Real-encoded Settled-event receipt (offline ABI encoding)."""
    topic0 = W.keccak(text="Settled(uint256,address,address,uint256,uint256)")
    settled_log = {
        "address": rd.ESCROW,
        "topics": [hex0x(topic0), pad_uint(5), pad_addr(buyer), pad_addr(seller)],
        "data": "0x" + encode(["uint256", "uint256"],
                              [918, refund]).hex(),
        "blockNumber": 99, "blockHash": h32(1), "transactionHash": h32(2),
        "transactionIndex": "0x1", "logIndex": "0x7",
    }
    logs = []
    if noise_prefix:
        logs.append({  # unrelated event — must NOT decode as Settled
            "address": "0x0000000000000000000000000000000000000001",
            "topics": ["0x" + "89" * 32], "data": "0x",
            "blockNumber": 99, "blockHash": h32(1), "transactionHash": h32(2),
            "transactionIndex": "0x1", "logIndex": "0x3",
        })
    if include_settled:
        logs.append(settled_log)
    if extra_settled:
        other = dict(settled_log)
        other["logIndex"] = "0x9"
        logs.append(other)
    # simulate a JSON-RPC-shaped receipt where quantities may arrive as
    # hex strings (exercises the hexint discipline too)
    return {"status": "0x%x" % status, "blockNumber": 99,
            "transactionHash": h32(2), "logs": logs,
            "transactionIndex": "0x1", "blockHash": h32(1)}


def hex0x(v) -> str:  # test-local alias to keep fixtures terse
    return bytes(v).hex() if isinstance(v, (bytes, bytearray)) else str(v)


def settled_poststate(**over) -> dict:
    payment = {
        "state": rd.EXPECTED_STATE_SETTLED, "captured": rd.EXPECTED_MAX,
        "buyer": rd.EXPECTED_BUYER, "seller": rd.EXPECTED_SELLER,
        "max_amount": rd.EXPECTED_MAX, "expires_at": 0, "fee_bps": 0,
        "escrow": rd.ESCROW, "payment_id": 5,
    }
    payment.update(over)
    return payment


def replay_saved_evidence(tmp: Path, snapshot: dict | None,
                          extra: dict | None = None) -> Path:
    saved = {
        "stage": "finalize", "mode": "broadcast", "tx_hash": "0x" + "ab" * 32,
        "gates": {"top_up": 0, "refund": rd.EXPECTED_REFUND, "fee": 0},
        "settled_logs": [{"log_index": 7, "evm_event_index": 1}],
        "trigger_evm_event_index": 1, "trigger_log_index": 7,
        "receipt_snapshot": snapshot,
        "payment_after": settled_poststate(),
        "receipt_refetch_raw_equal": True,
        "historical_prices": {"block": 99, "cached": 3_000_000,
                              "input": 6_000_000, "output": 9_000_000},
    }
    if extra:
        saved.update(extra)
    path = rd.EVIDENCE_FINALIZE
    tmp.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(saved), encoding="utf-8")
    return path


# ------------------------------------------------------------------ fakes

class FakeEthSend:
    def __init__(self, fail_wait: bool = True) -> None:
        self.sent: list[bytes] = []
        self.fail_wait = fail_wait

    def send_raw_transaction(self, raw: bytes) -> bytes:
        self.sent.append(bytes(raw))
        return b"\x11" * 32

    def wait_for_transaction_receipt(self, txh, timeout=None, poll_latency=None):
        if self.fail_wait:
            raise RuntimeError("simulated: tx never mined within timeout")
        return {"status": "0x1", "blockNumber": 5,
                "transactionHash": HexBytes(txh), "logs": []}


class FakeW3Send:
    def __init__(self, fail_wait: bool = True) -> None:
        self.eth = FakeEthSend(fail_wait)


class FakeSigned:
    raw_transaction = b"\x02\xf8"   # opaque blob; fake acct never signs real


class FakeAcct:
    def sign_transaction(self, built):  # noqa: ANN001
        return FakeSigned()


class FakeEthReconcile:
    def __init__(self, receipt: dict | None = None,
                 not_found: bool = False) -> None:
        self.receipt = receipt
        self.not_found = not_found

    def get_transaction_receipt(self, tx_hash):  # noqa: ANN001
        if self.not_found:
            raise RuntimeError("TransactionNotFound")
        if self.receipt is None:
            raise RuntimeError("TransactionNotFound")
        return self.receipt


class FakeW3Reconcile:
    def __init__(self, receipt: dict | None = None,
                 not_found: bool = False) -> None:
        self.eth = FakeEthReconcile(receipt, not_found)


class FakeEthCode:
    def __init__(self, code: bytes) -> None:
        self.code = code

    def get_code(self, address):  # noqa: ANN001
        return HexBytes(self.code)

    def get_block(self, block):  # noqa: ANN001 — replay-only fake
        return {"timestamp": PINNED_SETTLE_BLOCK_TS}


class FakeW3Code:
    def __init__(self, code: bytes) -> None:
        self.eth = FakeEthCode(code)
        self._checksum = lambda a: a

    def to_checksum_address(self, address):  # noqa: ANN001
        return address


class FakeW3ReconcileChecksum(FakeW3Reconcile):
    def to_checksum_address(self, address):  # noqa: ANN001
        return address


FakeW3Chain = FakeW3Code   # FakeEthCode now ALSO carries get_block (chain data)


def fake_w3_chain() -> FakeW3Code:
    return FakeW3Code(b"")   # offline chain fake: get_code + get_block


# ================================================================ test case


class EvidenceDirIsolated(unittest.TestCase):
    """Redirect module evidence-path constants to a per-test temp dir."""

    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        base = Path(self._tmp.name)
        rd.EVIDENCE_DIR = base
        rd.EVIDENCE_PREFLIGHT = base / "preflight.json"
        rd.EVIDENCE_DEPLOY = base / "deploy.json"
        rd.EVIDENCE_DEPLOY_PENDING = base / "deploy-pending.json"
        rd.EVIDENCE_FINALIZE = base / "finalize.json"
        rd.EVIDENCE_READBACK = base / "readback.json"

    def tearDown(self) -> None:
        self._tmp.cleanup()


# ------------------------------------------------------------- F4 canonical


class TestCanonicalHex(EvidenceDirIsolated):
    def test_jsonable_handles_mapping_not_dict(self) -> None:
        """Live-crash regression 2026-10-08: web3 AttributeDict is a Mapping,
        NOT a dict — jsonable must convert it (was: leaked to json.dumps)."""
        from web3.datastructures import AttributeDict
        leaky = AttributeDict(dict(status=1, logs=[
            AttributeDict(dict(logIndex=HexBytes(b"\x07") if False else 7,
                               topics=[b"\x89" * 32]))]))
        out = rd.jsonable(leaky)
        json.dumps(out)                       # must not raise
        self.assertEqual(out["logs"][0]["topics"], [rd.hex0x(b"\x89" * 32)])

    def test_hex0x_bytes_hexstring_all_canonical(self) -> None:
        b = bytes.fromhex("ABcdEF" + "00" * 29)
        self.assertEqual(rd.hex0x(b), "0x" + "abcdef" + "00" * 29)
        self.assertEqual(rd.hex0x(HexBytes(b)), "0x" + "abcdef" + "00" * 29)
        self.assertEqual(rd.hex0x("0xABCDEF"), "0xabcdef")
        self.assertEqual(rd.hex0x("ABCD"), "0xabcd")
        self.assertEqual(rd.hex0x("0x" + "ff" * 32), "0x" + "ff" * 32)

    def test_hex0x_rejects_nonhex_text(self) -> None:
        with self.assertRaises(rd.Abort):
            rd.hex0x("not-hex!")

    def test_hexint_quantities(self) -> None:
        self.assertEqual(rd.hexint("0x7"), 7)
        self.assertEqual(rd.hexint("0x0"), 0)
        self.assertEqual(rd.hexint(12), 12)
        self.assertEqual(rd.hexint("12"), 12)
        with self.assertRaises(rd.Abort):
            rd.hexint(None)

    def test_jsonable_all_bytes_canonical(self) -> None:
        out = rd.jsonable({"t": HexBytes(b"\x01\x02"), "l": [b"\x03"]})
        self.assertEqual(out, {"t": "0x0102", "l": ["0x03"]})

    def test_bytes32_topic_not_str_repr(self) -> None:
        """settled_log_json must canonicalize transactionHash (F4) — never a
        str(HexBytes) repr."""
        receipt = settled_receipt()
        fact = rd.settled_event_fact(W, receipt)
        th = fact["settled_log"]["transaction_hash"]
        self.assertTrue(th.startswith("0x"))
        self.assertEqual(th, h32(2))


# ------------------------------------------------- F1 exact runtime artifact


class TestRuntimeImmutables(EvidenceDirIsolated):
    def test_real_artifact_runtime_patched(self) -> None:
        runtime = rd.artifact_runtime_code(REAL_ARTIFACT)
        db = REAL_ARTIFACT["deployedBytecode"]["object"].removeprefix("0x").lower()
        self.assertEqual(len(runtime), 2 + len(db))  # patch preserves length
        self.assertNotEqual(runtime.removeprefix("0x"), db)
        regions = REAL_ARTIFACT["deployedBytecode"]["immutableReferences"]["40962"]
        body = runtime.removeprefix("0x")
        want = "00" * 12 + rd.FORWARDER.removeprefix("0x").lower()
        for r in regions:
            self.assertEqual(body[2 * r["start"]:2 * (r["start"] + r["length"])], want)

    def test_real_artifact_runtime_idempotent(self) -> None:
        once = rd.artifact_runtime_code(REAL_ARTIFACT)
        patched_artifact = {**REAL_ARTIFACT,
                            "deployedBytecode": {
                                **REAL_ARTIFACT["deployedBytecode"],
                                "object": once}}
        self.assertEqual(rd.artifact_runtime_code(patched_artifact), once)

    def test_missing_immutable_refs_aborts(self) -> None:
        artifact = {"deployedBytecode": {"object": "0x" + "00" * 72}}
        with self.assertRaises(rd.Abort) as ctx:
            rd.artifact_runtime_code(artifact)
        self.assertIn("immutableReferences missing", str(ctx.exception))

    def test_tampered_immutable_slot_aborts(self) -> None:
        obj = "ff" * 12 + "00" * 20 + "00" * 24   # garbage in first slot
        artifact = {"deployedBytecode": {
            "object": "0x" + obj,
            "immutableReferences": {"40962": [{"start": 0, "length": 32}]}}}
        with self.assertRaises(rd.Abort) as ctx:
            rd.artifact_runtime_code(artifact)
        self.assertIn("neither zero-filled nor pinned", str(ctx.exception))

    def test_truncated_bytecode_slot_out_of_bounds_aborts(self) -> None:
        artifact = {"deployedBytecode": {
            "object": "0x" + "00" * 16,   # shorter than the declared slot
            "immutableReferences": {"40962": [{"start": 0, "length": 32}]}}}
        with self.assertRaises(rd.Abort) as ctx:
            rd.artifact_runtime_code(artifact)
        self.assertIn("out of bounds", str(ctx.exception))

    def test_ambiguous_multi_identity_aborts(self) -> None:
        artifact = {"deployedBytecode": {
            "object": "0x" + "00" * 64,
            "immutableReferences": {"11111": [{"start": 0, "length": 32}],
                                    "22222": [{"start": 32, "length": 32}]}}}
        with self.assertRaises(rd.Abort):
            rd.artifact_runtime_code(artifact)


class TestExactCodeMatch(EvidenceDirIsolated):
    RUNTIME = rd.artifact_runtime_code(REAL_ARTIFACT)

    def _fake(self, code_hex: str) -> FakeW3Code:
        body = code_hex.removeprefix("0x")
        return FakeW3Code(bytes.fromhex(body))

    def test_exact_match(self) -> None:
        ok, how = rd.code_matches_onchain(self._fake(self.RUNTIME),
                                          "0x0000000000000000000000000000000000000001",
                                          self.RUNTIME)
        self.assertTrue(ok)
        self.assertEqual(how, "exact")

    def test_truncated_prefix_REJECTED(self) -> None:
        half_chars = (len(self.RUNTIME) - 2) // 2
        half_chars -= half_chars % 2                    # keep hex-pair aligned
        truncated = self.RUNTIME[: 2 + half_chars]
        ok, why = rd.code_matches_onchain(self._fake(truncated),
                                          "0x01", self.RUNTIME)
        self.assertFalse(ok)
        self.assertIn("NO prefix acceptance", why)

    def test_extended_code_REJECTED(self) -> None:
        extended = self.RUNTIME + "00" * 10
        ok, why = rd.code_matches_onchain(self._fake(extended),
                                          "0x01", self.RUNTIME)
        self.assertFalse(ok)

    def test_empty_code_REJECTED(self) -> None:
        ok, why = rd.code_matches_onchain(self._fake("0x"), "0x01", self.RUNTIME)
        self.assertFalse(ok)


class TestPendingDeployment(EvidenceDirIsolated):
    RUNTIME = rd.artifact_runtime_code(REAL_ARTIFACT)
    PENDING = {"stage": "deploy", "state": "pending",
               "tx_hash": h32(3), "sender": rd.EXPECTED_SELLER,
               "nonce": 4, "chain_id": 10143, "artifact_sha256": REAL_ARTIFACT_SHA}

    def test_pending_persisted_BEFORE_wait(self) -> None:
        """F1: persist_fn fires immediately after send, before wait — even
        when wait never returns (no silent duplicate deploy next run)."""
        writes: list[str] = []

        with self.assertRaises(rd.Abort):
            rd.send_signed_tx(FakeW3Send(fail_wait=True),
                              {"from": "0x0"}, FakeAcct(), "deploy",
                              persist_fn=lambda h: writes.append(h))
        self.assertEqual(writes, ["0x" + "11" * 32])

    def pending_path(self) -> dict:
        self.assertTrue(rd.EVIDENCE_DEPLOY_PENDING.exists())
        return json.loads(rd.EVIDENCE_DEPLOY_PENDING.read_text())

    def test_wait_failure_leaves_pending_record(self) -> None:
        rd.ensure_evidence_dir()
        with self.assertRaises(rd.Abort):
            try:
                rd.send_signed_tx(FakeW3Send(fail_wait=True), {"from": "0x0"},
                                  FakeAcct(), "deploy",
                                  persist_fn=lambda h: rd.write_evidence(
                                      rd.EVIDENCE_DEPLOY_PENDING,
                                      {"state": "pending", "tx_hash": h}))
            finally:
                pass  # pending write already committed inside persist_fn
        rec = self.pending_path()
        self.assertEqual(rec["state"], "pending")
        self.assertTrue(rec["tx_hash"].startswith("0x"))

    def test_reconcile_reverted_resolves_pending(self) -> None:
        rd.ensure_evidence_dir()
        rd.write_evidence(rd.EVIDENCE_DEPLOY_PENDING, {**self.PENDING})
        w3 = FakeW3Reconcile(receipt={"status": "0x0", "blockNumber": 1,
                                      "transactionHash": h32(3)})
        out = rd._deploy_pending_reconcile(w3, {**self.PENDING, "tx_hash": h32(3)},
                                           REAL_ARTIFACT, REAL_ARTIFACT_SHA,
                                           self.RUNTIME)
        self.assertEqual(out, "reverted")
        rec = self.pending_path()
        self.assertEqual(rec["state"], "resolved-reverted")

    def test_reconcile_unmined_aborts_no_duplicate(self) -> None:
        rd.ensure_evidence_dir()
        w3 = FakeW3Reconcile(not_found=True)
        with self.assertRaises(rd.Abort) as ctx:
            rd._deploy_pending_reconcile(w3, {**self.PENDING}, REAL_ARTIFACT,
                                         REAL_ARTIFACT_SHA, self.RUNTIME)
        self.assertIn("duplicate deploy", str(ctx.exception))

    def test_reconcile_stale_artifact_aborts(self) -> None:
        rd.ensure_evidence_dir()
        w3 = FakeW3Reconcile(not_found=True)
        with self.assertRaises(rd.Abort):
            rd._deploy_pending_reconcile(w3, {**self.PENDING}, REAL_ARTIFACT,
                                         "deadbeef" * 8, self.RUNTIME)

    def test_reconcile_malformed_aborts(self) -> None:
        rd.ensure_evidence_dir()
        w3 = FakeW3Reconcile(not_found=True)
        with self.assertRaises(rd.Abort):
            rd._deploy_pending_reconcile(w3, {"state": "pending",
                                              "chain_id": 10143,
                                              "artifact_sha256": REAL_ARTIFACT_SHA},
                                         REAL_ARTIFACT, REAL_ARTIFACT_SHA,
                                         self.RUNTIME)


# ------------------------------------------- F2 shared EXACT event validator


class TestSettledEventShared(EvidenceDirIsolated):
    def test_fact_exact_and_indices_distinct(self) -> None:
        fact = rd.settled_event_fact(W, settled_receipt())
        self.assertEqual(fact["evm_event_index"], 1)   # position in tx log array
        self.assertEqual(fact["log_index"], 7)         # block-global logIndex
        self.assertIn("evm_event_index", fact["settled_log"])
        self.assertEqual(fact["settled_log"]["args"]["refundedAmount"], 1_499_082)
        self.assertEqual(fact["settled_log"]["args"]["buyer"],
                         rd.EXPECTED_BUYER)
        self.assertEqual(fact["settled_log"]["args"]["seller"],
                         rd.EXPECTED_SELLER)

    def test_receipt_log_array_index_export(self) -> None:
        rh = settled_receipt()
        fact = rd.settled_event_fact(W, rh)
        lg = rh["logs"][fact["evm_event_index"]]
        self.assertEqual(rd.hexint(lg["logIndex"]), fact["log_index"])

    def test_mutation_refund_rejected(self) -> None:
        with self.assertRaises(rd.Abort) as ctx:
            rd.settled_event_fact(W, settled_receipt(refund=1_499_083))
        self.assertIn("refundedAmount", str(ctx.exception))

    def test_mutation_buyer_rejected(self) -> None:
        evil = "0x000000000000000000000000000000000000dead"
        with self.assertRaises(rd.Abort) as ctx:
            rd.settled_event_fact(W, settled_receipt(buyer=evil))
        self.assertIn("buyer", str(ctx.exception))

    def test_status0_rejected(self) -> None:
        with self.assertRaises(rd.Abort):
            rd.settled_event_fact(W, settled_receipt(status=0))

    def test_missing_event_rejected(self) -> None:
        with self.assertRaises(rd.Abort):
            rd.settled_event_fact(W, settled_receipt(noise_prefix=True,
                                                     include_settled=False))

    def test_double_settled_rejected(self) -> None:
        with self.assertRaises(rd.Abort):
            rd.settled_event_fact(W, settled_receipt(extra_settled=True))

    def test_hex_quantity_log_index(self) -> None:
        rpc = settled_receipt()
        for lg in rpc["logs"]:
            lg["logIndex"] = "0x7" if int(lg["logIndex"], 16) == 7 else lg["logIndex"]
        fact = rd.settled_event_fact(W, rpc)   # must not crash; same fact
        self.assertEqual(fact["log_index"], 7)

    def test_poststate_exact(self) -> None:
        rd._assert_settled_poststate(settled_poststate())   # no raise
        for bad in (settled_poststate(state=1),
                    settled_poststate(captured=918),
                    settled_poststate(buyer="0x0000000000000000000000000000000000000000"),
                    settled_poststate(seller="0x0000000000000000000000000000000000000000"),
                    settled_poststate(max_amount=1500001)):
            with self.assertRaises(rd.Abort):
                rd._assert_settled_poststate(bad)

    def test_fresh_and_saved_share_validator_source(self) -> None:
        """Structural (offline): both the broadcast path and the
        alreadySettled replay path go through settled_event_fact()."""
        self.assertIn("settled_event_fact(w3, receipt)",
                      inspect.getsource(rd.settle_tx_looks_exact))


# ----------------------------------------------------------- F6 gates/estimate


class TestExactPlanGates(EvidenceDirIsolated):
    def test_gates_exact(self) -> None:
        gates = rd.assert_payment_gates({"state": rd.EXPECTED_STATE_LOCKED,
                                         "buyer": rd.EXPECTED_BUYER,
                                         "seller": rd.EXPECTED_SELLER,
                                         "max_amount": 1_500_000,
                                         "captured": 918})
        self.assertEqual(gates, {"top_up": 0, "refund": 1_499_082, "fee": 0})

    def test_gates_drift_aborts(self) -> None:
        base = {"state": rd.EXPECTED_STATE_LOCKED, "buyer": rd.EXPECTED_BUYER,
                "seller": rd.EXPECTED_SELLER, "max_amount": 1_500_000,
                "captured": 918}
        for bad in ({**base, "captured": 919}, {**base, "state": 2},
                    {**base, "buyer": "0x0000000000000000000000000000000000000abc"},
                    {**base, "seller": "0x0000000000000000000000000000000000000abc"},
                    {**base, "max_amount": 1_499_999}):
            with self.assertRaises(rd.Abort):
                rd.assert_payment_gates(bad)

    def test_estimate_failure_no_fallback(self) -> None:
        class BoomEth:
            def estimate_gas(self, tx):  # noqa: ANN001
                raise RuntimeError("revert at estimate")

        class BoomW3:
            eth = BoomEth()

        with self.assertRaises(rd.Abort) as ctx:
            rd.estimate_gas(BoomW3(), {"from": "0x0", "nonce": 1, "data": "0x"})
        self.assertIn("no fallback", str(ctx.exception))

    def test_f6_recheck_after_estimate_before_send(self) -> None:
        """Structural: stage_finalize's broadcast path re-reads the exact
        payment gates AFTER estimate_gas and BEFORE send_signed_tx."""
        src = inspect.getsource(rd.stage_finalize)
        i_estimate = src.find('built["gas"] = estimate_gas(w3, built)')
        i_recheck = src.find("gates_pre_send = assert_payment_gates(read_payment(w3))")
        i_send = src.find("send_signed_tx(w3, built, acct")
        self.assertGreater(i_estimate, -1)
        self.assertGreater(i_recheck, i_estimate)
        self.assertGreater(i_send, i_recheck)


# ------------------------------------------------------- F5 strict HTTP gates


class TestReceiptRawPipeline(EvidenceDirIsolated):
    def test_duplicate_json_key_rejected(self) -> None:
        for raw in (b'{"a":1,"a":2}',
                    b'{"outer":{"k":1,"k":2},"a":1}',
                    b'{"T":2,"t":3,"t":4}'):
            with self.assertRaises(rd.Abort):
                rd.parse_strict_json(raw)

    def test_strict_json_ok(self) -> None:
        self.assertEqual(rd.parse_strict_json(b'{"a":1,"b":{"c":2}}'),
                         {"a": 1, "b": {"c": 2}})

    def test_404_stop_no_substitution(self) -> None:
        """The REAL workflow: fetch → GET → 404 HTTPError → production STOP
        branch (never a local-snapshot acceptance)."""
        err = urllib.error.HTTPError("u", 404, "nf", None, None)
        orig_urlopen = urllib.request.urlopen

        def boom(req, **kw):  # noqa: ANN001
            raise err

        urllib.request.urlopen = boom
        try:
            with self.assertRaises(rd.Abort) as ctx:
                rd.fetch_receipt_data(5)
            self.assertIn("404", str(ctx.exception))
        finally:
            urllib.request.urlopen = orig_urlopen

    def test_non200_stop(self) -> None:
        orig = rd._http_get_raw
        rd._http_get_raw = lambda url: (500, b"oops")
        try:
            with self.assertRaises(rd.Abort):
                rd.fetch_receipt_data(5)
        finally:
            rd._http_get_raw = orig

    def test_fetch_is_live_configured_get(self) -> None:
        seen: list[str] = []
        orig = rd._http_get_raw

        def rec(url: str):  # noqa: ANN001
            seen.append(url)
            return 200, PINNED_RAW

        rd._http_get_raw = rec
        try:
            data = rd.fetch_receipt_data(5)
            self.assertEqual(seen, [f"{rd.RELAY_URL}/receipt/5"])
            self.assertEqual(data["raw"], PINNED_RAW)
            self.assertEqual(data["status"], 200)
        finally:
            rd._http_get_raw = orig

    def test_snapshot_roundtrip(self) -> None:
        snap = rd.receipt_snapshot({"status": 200, "raw": PINNED_RAW,
                                    "json": pinned_receipt_json()})
        self.assertEqual(snap["raw_bytes"], len(PINNED_RAW))
        rd.require_receipt_equal(snap,
                                 {"raw": PINNED_RAW,
                                  "json": pinned_receipt_json()},
                                 "self-roundtrip")

    def test_raw_whitespaceDifference_body_rejected(self) -> None:
        snap = rd.receipt_snapshot({"status": 200, "raw": PINNED_RAW,
                                    "json": pinned_receipt_json()})
        with self.assertRaises(rd.Abort):
            rd.require_receipt_equal(snap, {"raw": PINNED_RAW + b" ",
                                            "json": pinned_receipt_json()},
                                     "whitespace tail")

    def test_raw_innerWhitespaceChange_rejected(self) -> None:
        snap = rd.receipt_snapshot({"status": 200, "raw": PINNED_RAW,
                                    "json": pinned_receipt_json()})
        spaced = json.dumps(pinned_receipt_json(), indent=1).encode()
        self.assertNotEqual(spaced, PINNED_RAW)
        with self.assertRaises(rd.Abort):
            rd.require_receipt_equal(snap, {"raw": spaced,
                                            "json": pinned_receipt_json()},
                                     "reformatted body")

    def test_changed_json_value_rejected(self) -> None:
        snap = rd.receipt_snapshot({"status": 200, "raw": PINNED_RAW,
                                    "json": pinned_receipt_json()})
        changed = pinned_receipt_json()
        changed["message"]["actualAmount"] = 919
        with self.assertRaises(rd.Abort):
            rd.require_receipt_equal(snap, {"raw": PINNED_RAW,
                                            "json": changed}, "json-diff")

    def test_missing_snapshot_blocked(self) -> None:
        with self.assertRaises(rd.Abort) as ctx:
            rd.require_receipt_equal(None, {"raw": PINNED_RAW,
                                            "json": pinned_receipt_json()},
                                     "gate")
        self.assertIn("BLOCKED", str(ctx.exception))

    def test_pinned_receipt_schema_signature_offline(self) -> None:
        checked = rd.verify_receipt_schema_and_signature(pinned_receipt_json())
        self.assertEqual(checked["recovered_signer"].lower(),
                         rd.TRUSTED_SIGNER.lower())


# --------------------------------------------------------- F3 replay rules


class TestAlreadySettledReplay(EvidenceDirIsolated):
    def _prep(self, snapshot_raw: bytes, snapshot_json: dict) -> None:
        rd.ensure_evidence_dir()
        replay_saved_evidence(
            rd.EVIDENCE_DIR,
            rd.receipt_snapshot({"status": 200, "raw": snapshot_raw,
                                 "json": snapshot_json}))

    def _patch(self, live_raw: bytes, price: tuple = rd.EXPECTED_PRICE_TRIPLE,
               tx_fake=None) -> None:
        self._orig_fetch = rd.fetch_receipt_data
        self._orig_price = rd.price_triple_at
        self._orig_verify = rd.settle_tx_looks_exact
        rd.fetch_receipt_data = lambda pid: {"url": "u", "status": 200,
                                             "raw": live_raw,
                                             "json": json.loads(live_raw)}
        rd.price_triple_at = lambda w3, block: price
        rd.settle_tx_looks_exact = tx_fake or self._fake_tx_ok

    def _fake_tx_ok(self, w3, txh):  # noqa: ANN001
        return {"tx_hash": txh, "receipt": {},
                "payment": settled_poststate(),
                "settled_log": {"address": rd.ESCROW, "block_number": 99,
                                "transaction_hash": h32(2),
                                "transaction_index": 1, "log_index": 7,
                                "evm_event_index": 1,
                                "args": {"paymentId": 5,
                                         "buyer": rd.EXPECTED_BUYER,
                                         "seller": rd.EXPECTED_SELLER,
                                         "actualAmount": 918,
                                         "refundedAmount": 1_499_082}}}

    def _unpatch(self) -> None:
        rd.fetch_receipt_data = self._orig_fetch
        rd.price_triple_at = self._orig_price
        rd.settle_tx_looks_exact = self._orig_verify

    def test_replay_happy_merges_and_preserves_prior_evidence(self) -> None:
        self._prep(PINNED_RAW, pinned_receipt_json())
        before = rd.EVIDENCE_FINALIZE.read_text()
        self._patch(PINNED_RAW)
        try:
            rd._finalize_already_settled(FakeW3Code(b""), None)
        finally:
            self._unpatch()
        after = json.loads(rd.EVIDENCE_FINALIZE.read_text())
        self.assertTrue(after["replay_verified"])
        self.assertTrue(after["replay"]["receipt_live_raw_equal_to_snapshot"])
        self.assertEqual(after["replay"]["last_receipt_arithmetic_actual"], 918)
        self.assertEqual(after["replay"]["trigger_block"], 99)
        # MERGE-ONLY: every prior key/value persists unchanged
        orig = json.loads(before)
        for key, val in orig.items():
            self.assertEqual(after[key], val, f"prior evidence key {key} clobbered")

    def test_replay_missing_evidence_blocked(self) -> None:
        # no finalize.json at all, saved settle tx given: DEFAULT is STOP
        # (fallback requires the explicit flag; "no tx at all" never passes
        # and is covered separately by test_replay_tx_missing_blocked).
        rd.ensure_evidence_dir()
        with self.assertRaises(rd.Abort) as ctx:
            rd._finalize_already_settled(FakeW3Code(b""), "0x" + "ab" * 32)
        self.assertIn("STOP", str(ctx.exception))

    def test_replay_missing_snapshot_blocked(self) -> None:
        rd.ensure_evidence_dir()
        replay_saved_evidence(rd.EVIDENCE_DIR, None)   # no receipt_snapshot
        with self.assertRaises(rd.Abort) as ctx:
            rd._finalize_already_settled(FakeW3Code(b""), None)
        self.assertIn("STOP", str(ctx.exception))

    def test_default_fallback_rejected_even_with_valid_preflight(self) -> None:
        """ora42 finding: the OLD universal fallback accepted any preflight
        snapshot by default. Now: missing finalize snapshot ⇒ STOP even when
        a FULLY VALID preflight.json is on disk — no flag, no recovery."""
        rd.ensure_evidence_dir()
        rd.EVIDENCE_PREFLIGHT.write_text(
            json.dumps(valid_preflight_evidence()), encoding="utf-8")
        with self.assertRaises(rd.Abort) as ctx:
            rd._finalize_already_settled(fake_w3_chain(), "0x" + "ab" * 32)
        self.assertIn("STOP", str(ctx.exception))
        self.assertIn("--reconcile-evidence-crash", str(ctx.exception))
        self.assertFalse(rd.EVIDENCE_FINALIZE.exists())

    def test_replay_tx_missing_blocked_never_fabricate(self) -> None:
        rd.ensure_evidence_dir()
        replay_saved_evidence(
            rd.EVIDENCE_DIR,
            rd.receipt_snapshot({"status": 200, "raw": PINNED_RAW,
                                 "json": pinned_receipt_json()}), extra={
                "tx_hash": ""})
        with self.assertRaises(rd.Abort) as ctx:
            rd._finalize_already_settled(FakeW3Code(b""), None)
        self.assertIn("never fabricate", str(ctx.exception))

    def test_replay_changed_live_raw_blocked(self) -> None:
        self._prep(PINNED_RAW, pinned_receipt_json())
        self._patch(PINNED_RAW + b" ")
        try:
            with self.assertRaises(rd.Abort):
                rd._finalize_already_settled(FakeW3Code(b""), None)
        finally:
            self._unpatch()
        after = rd.EVIDENCE_FINALIZE.read_text()
        self.assertNotIn("replay", json.loads(after))   # no partial write

    def test_replay_historical_price_mismatch_blocked(self) -> None:
        self._prep(PINNED_RAW, pinned_receipt_json())
        self._patch(PINNED_RAW, price=(2_000_000, 7_000_000, 9_000_000))
        try:
            with self.assertRaises(rd.Abort) as ctx:
                rd._finalize_already_settled(FakeW3Code(b""), None)
            self.assertIn("BLOCKED", str(ctx.exception))
        finally:
            self._unpatch()
        self.assertNotIn("replay", json.loads(rd.EVIDENCE_FINALIZE.read_text()))

    def test_replay_poststate_drift_blocked(self) -> None:
        self._prep(PINNED_RAW, pinned_receipt_json())

        def bad_tx(w3, txh):  # noqa: ANN001
            fake = self._fake_tx_ok(w3, txh)
            fake["payment"] = settled_poststate(captured=918)   # drift
            return fake

        self._patch(PINNED_RAW, tx_fake=bad_tx)
        try:
            with self.assertRaises(rd.Abort):
                rd._finalize_already_settled(FakeW3Code(b""), None)
        finally:
            self._unpatch()
        self.assertNotIn("replay", json.loads(rd.EVIDENCE_FINALIZE.read_text()))

    def test_replay_formula_uses_trigger_block_prices(self) -> None:
        self._prep(PINNED_RAW, pinned_receipt_json())
        self._patch(PINNED_RAW, price=(3_000_000, 6_000_000, 9_000_000))
        try:
            rd._finalize_already_settled(FakeW3Code(b""), None)
        finally:
            self._unpatch()
        r = json.loads(rd.EVIDENCE_FINALIZE.read_text())["replay"]
        self.assertEqual((r["historical_prices"]["cached"],
                          r["historical_prices"]["input"],
                          r["historical_prices"]["output"]),
                         rd.EXPECTED_PRICE_TRIPLE)
        self.assertEqual(r["last_receipt_arithmetic_actual"], 918)

    def test_replay_crash_reconcile_from_preflight(self) -> None:
        """Labeled recovery MUST be explicit: finalize.json ABSENT (crash at
        evidence write) + --reconcile-evidence-crash + full provenance-valid
        preflight baseline → accepted with the explicit label, full semantic
        + live raw re-verification; gates reconstructed as derived/posthoc
        (never a claim of reproduced presign checks)."""
        rd.ensure_evidence_dir()
        rd.EVIDENCE_PREFLIGHT.write_text(
            json.dumps(valid_preflight_evidence()), encoding="utf-8")
        self._patch(PINNED_RAW)
        try:
            rd._finalize_already_settled(fake_w3_chain(), "0x" + "ab" * 32,
                                        reconcile_crash=True)
        finally:
            self._unpatch()
        doc = json.loads(rd.EVIDENCE_FINALIZE.read_text())
        self.assertEqual(doc["mode"], "broadcast-reconciled-from-crash")
        self.assertIn("--reconcile-evidence-crash",
                      doc["replay"]["snapshot_source"])
        self.assertIn("preflight.json", doc["replay"]["snapshot_source"])
        self.assertTrue(doc["replay"]["receipt_live_raw_equal_to_snapshot"])
        # reconstructed gates are explicitly derived/posthoc:
        self.assertTrue(doc["gates"]["derived_posthoc"])
        self.assertFalse(doc["gates"]["presign_gate_recheck_reproduced"])
        self.assertFalse(doc["presign_gate_recheck_reproduced"])
        # tx receipt + trigger indices present for the CRE trigger export:
        self.assertEqual(doc["trigger_evm_event_index"],
                         doc["settled_logs"][0]["evm_event_index"])
        self.assertEqual(doc["trigger_log_index"],
                         doc["settled_logs"][0]["log_index"])

    def test_replay_crash_reconcile_refluses_when_baseline_absent(self) -> None:
        rd.ensure_evidence_dir()
        rd.EVIDENCE_PREFLIGHT.write_text(json.dumps({"stage": "preflight"}),
                                         encoding="utf-8")   # snapshot absent
        self._patch(PINNED_RAW)
        try:
            with self.assertRaises(rd.Abort) as ctx:
                rd._finalize_already_settled(fake_w3_chain(), "0x" + "ab" * 32,
                                             reconcile_crash=True)
            self.assertIn("REJECTED", str(ctx.exception))
        finally:
            self._unpatch()
        self.assertFalse(rd.EVIDENCE_FINALIZE.exists())   # nothing fabricated

    # ---- explicit-recovery provenance negatives (ora42) -------------------
    # All negatives _patch() the chain-side steps (offline) so the EXACT
    # provenance rejection is what fires.

    def _patched_run(self, preflight_doc: dict, expect_glob: str) -> None:
        rd.ensure_evidence_dir()
        rd.EVIDENCE_PREFLIGHT.write_text(json.dumps(preflight_doc),
                                         encoding="utf-8")
        self._patch(PINNED_RAW)
        try:
            with self.assertRaises(rd.Abort) as ctx:
                rd._finalize_already_settled(fake_w3_chain(),
                                             "0x" + "ab" * 32,
                                             reconcile_crash=True)
            self.assertIn(expect_glob, str(ctx.exception))
        finally:
            self._unpatch()
        self.assertFalse(rd.EVIDENCE_FINALIZE.exists())

    def test_crash_reconcile_wrong_network_rejected(self) -> None:
        self._patched_run(valid_preflight_evidence(
            network={"rpc": rd.RPC_URL, "chain_id": 84532}), "chain_id")

    def test_crash_reconcile_wrong_payment_rejected(self) -> None:
        for over in ("payment.captured:919", "payment.state:2",
                     "payment.max_amount:1_499_999",
                     "payment.buyer:0x0000000000000000000000000000000000000000"):
            path = over.rsplit(":", 1)[0]
            value = over.rsplit(":", 1)[1]
            kwargs = {path: int(value) if value.isdigit() else value}
            self._patched_run(valid_preflight_evidence(**kwargs),
                              path.split(".")[-1])

    def test_crash_reconcile_wrong_expires_rejected(self) -> None:
        self._patched_run(valid_preflight_evidence(
            **{"payment.expires_at": 1_791_432_002}), "expires_at")

    def test_crash_reconcile_wrong_stage_or_address_rejected(self) -> None:
        for path, value, glob in (
                ("stage", "not-preflight", "stage"),
                ("escrow", "0x" + "00" * 20, "escrow"),
                ("registry", "0x" + "11" * 20, "registry"),
                ("receipt_http_status", 404, "receipt_http_status"),
                ("network.rpc", "https://evil.example", "network.rpc"),
                ("receipt_snapshot.status", 201, "snapshot.status")):
            self._patched_run(valid_preflight_evidence(**{path: value}), glob)

    def test_crash_reconcile_written_at_gates(self) -> None:
        ts = PINNED_SETTLE_BLOCK_TS
        for over_label, written in (
                ("missing", None),
                ("equal-to-block-ts", ts),
                ("later-than-block-ts", ts + 1),
                ("non-int", float(ts - 1)),
                ("bool", True)):
            doc = valid_preflight_evidence()
            if written is None:
                doc.pop("written_at_unix")
            else:
                doc["written_at_unix"] = written
            self._patched_run(doc, "written_at_unix")

    def test_earlier_written_at_accepted_only_with_strict_provenance(self) -> None:
        """written_at 48s earlier than the settle-block ts (the ACTUAL
        preserved evidence) passes ONLY with every other field intact."""
        rd.ensure_evidence_dir()
        rd.EVIDENCE_PREFLIGHT.write_text(
            json.dumps(valid_preflight_evidence(written_at=1791453374)),
            encoding="utf-8")
        self._patch(PINNED_RAW)
        try:
            rd._finalize_already_settled(fake_w3_chain(), "0x" + "ab" * 32,
                                        reconcile_crash=True)
        finally:
            self._unpatch()
        r = json.loads(rd.EVIDENCE_FINALIZE.read_text())["replay"]
        self.assertEqual(r["last_receipt_arithmetic_actual"], 918)

    def test_reconcile_flag_refused_with_broadcast(self) -> None:
        """严禁broadcast: the recovery entry is never combinable with sends."""
        with self.assertRaises(rd.Abort):
            rd._assert_reconcile_no_broadcast(True, True)
        rd._assert_reconcile_no_broadcast(True, False)   # ok (read-only)
        rd._assert_reconcile_no_broadcast(False, True)   # ok (normal stages)


if __name__ == "__main__":
    unittest.main(verbosity=2)
