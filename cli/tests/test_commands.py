"""Command-level tests: argument assembly, env validation, and the full
`call` flow with a mocked relay response (including receipt verification
positive + tampered-negative cases and dispute recording).

No real chain: chain helpers are monkeypatched. No real relay: the HTTP
client functions are monkeypatched.
"""

import json

import httpx

from typer.testing import CliRunner

from tests.conftest import (
    BUYER_KEY,
    CHAIN_ID,
    ESCROW_ADDR,
    REGISTRY_ADDR,
    SELLER_KEY,
    USDC_ADDR,
    addr_of,
    all_output,
    make_receipt,
    receipt_header,
)

RPC_URL = "http://127.0.0.1:8545"
REQUIRED_ENV_VARS = ("BUYER_PRIVATE_KEY", "RPC_URL", "CHAIN_ID", "ESCROW_ADDR", "REGISTRY_ADDR", "USDC_ADDR")

# quick smoke import; real imports happen inside tests to patch cleanly
import tokenshare_cli.app as app_mod  # noqa: E402
import tokenshare_cli.chain as chain_mod  # noqa: E402
from tokenshare_cli.app import app  # noqa: E402
from tokenshare_cli.errors import RelayError  # noqa: E402
from tokenshare_cli.relay_client import RelayResponse  # noqa: E402

runner = CliRunner()

SELLER = addr_of(SELLER_KEY)


def _set_full_env(monkeypatch) -> None:
    monkeypatch.setenv("BUYER_PRIVATE_KEY", BUYER_KEY)
    monkeypatch.setenv("RPC_URL", RPC_URL)
    monkeypatch.setenv("CHAIN_ID", str(CHAIN_ID))
    monkeypatch.setenv("ESCROW_ADDR", ESCROW_ADDR)
    monkeypatch.setenv("REGISTRY_ADDR", REGISTRY_ADDR)
    monkeypatch.setenv("USDC_ADDR", USDC_ADDR)


class FakeChain:
    """ChainContext stand-in; records invoked chain calls."""

    def __init__(self, monkeypatch, *, listing=None, escrow_balance=10**9, payment=None):
        self.calls = []
        self.listing = listing or {
            "operator": SELLER,
            "endpoint": "http://127.0.0.1:8787",
            "models": ["gpt-4o-mini"],
            "price_cached_in": 100,
            "price_input": 1000,
            "price_output": 1000,
            "active": True,
        }
        self.escrow_balance = escrow_balance
        self.payment = payment

        ctx = self
        monkeypatch.setattr(chain_mod, "open_chain", lambda cfg: ctx)
        monkeypatch.setattr(chain_mod, "escrow_balance", lambda c: ctx.escrow_balance)
        monkeypatch.setattr(chain_mod, "usdc_balance", lambda c: 100_000_000)
        monkeypatch.setattr(chain_mod, "escrow_next_payment_id", lambda c: 5)
        monkeypatch.setattr(chain_mod, "get_listing", lambda c, seller: ctx.listing)
        monkeypatch.setattr(chain_mod, "get_payment", lambda c, pid: ctx.payment)
        monkeypatch.setattr(chain_mod, "deposit", ctx._deposit)
        monkeypatch.setattr(chain_mod, "lock", ctx._lock)
        monkeypatch.setattr(chain_mod, "refund", ctx._refund)
        monkeypatch.setattr(chain_mod, "usdc_allowance", lambda c: 0)

    @property
    def address(self):
        return addr_of(BUYER_KEY)

    def _deposit(self, c, amount):
        self.calls.append(("deposit", amount))
        return {"steps": [f"approve tx 0xdead", f"deposit tx 0xbeef"], "amount": amount, "escrow_balance": self.escrow_balance + amount}

    def _lock(self, c, seller, max_amount, ttl):
        self.calls.append(("lock", seller, max_amount, ttl))
        return {
            "payment_id": 42,
            "buyer": self.address,
            "seller": seller,
            "max_amount": max_amount,
            "expires_at": 1234,
            "tx_hash": "0xlock",
        }

    def _refund(self, c, payment_id):
        self.calls.append(("refund", payment_id))
        return {"payment_id": payment_id, "amount": 1_000_000, "tx_hash": "0xref", "escrow_balance": 9_000_000}


# ---------------------------------------------------------------------------
# env errors
# ---------------------------------------------------------------------------


def test_missing_env_names_all_missing_vars(monkeypatch):
    for var in REQUIRED_ENV_VARS:
        monkeypatch.delenv(var, raising=False)
    result = runner.invoke(app, ["balance"])
    assert result.exit_code == 2
    out = all_output(result)
    for var in REQUIRED_ENV_VARS:
        assert var in out


def test_bad_private_key_is_rejected(monkeypatch):
    _set_full_env(monkeypatch)
    monkeypatch.setenv("BUYER_PRIVATE_KEY", "0xzz-nope")
    FakeChain(monkeypatch)
    result = runner.invoke(app, ["balance"])
    assert result.exit_code != 0
    assert "BUYER_PRIVATE_KEY" in all_output(result)


# ---------------------------------------------------------------------------
# balance / deposit / lock
# ---------------------------------------------------------------------------


def test_balance_shows_wallet_and_escrow(monkeypatch):
    _set_full_env(monkeypatch)
    FakeChain(monkeypatch)
    result = runner.invoke(app, ["balance"])
    assert result.exit_code == 0
    out = all_output(result)
    assert "USDC wallet balance: 100000000 native (= 100 USDC)" in out
    assert "Escrow balance (withdrawable): 1000000000 native (= 1000 USDC)" in out


def test_deposit_parses_human_amount(monkeypatch):
    _set_full_env(monkeypatch)
    fake = FakeChain(monkeypatch)
    result = runner.invoke(app, ["deposit", "--amount", "10.5"])
    assert result.exit_code == 0
    deposit_call = [c for c in fake.calls if c[0] == "deposit"][0]
    assert deposit_call[1] == 10_500_000  # 10.5 USDC in native units
    assert "10.5 USDC" in all_output(result)


def test_deposit_rejects_more_than_6dp(monkeypatch):
    _set_full_env(monkeypatch)
    FakeChain(monkeypatch)
    result = runner.invoke(app, ["deposit", "--amount", "1.0000001"])
    assert result.exit_code == 2
    assert "decimal" in all_output(result)


def test_lock_prints_payment_id(monkeypatch):
    _set_full_env(monkeypatch)
    fake = FakeChain(monkeypatch)
    result = runner.invoke(app, ["lock", "--seller", SELLER, "--max", "2"])
    assert result.exit_code == 0
    assert "paymentId: 42" in all_output(result)
    lock_call = [c for c in fake.calls if c[0] == "lock"][0]
    assert lock_call[1] == SELLER
    assert lock_call[2] == 2_000_000
    assert lock_call[3] == 600  # default TTL per contract DEFAULT_TTL


def test_lock_rejects_zero_max(monkeypatch):
    _set_full_env(monkeypatch)
    FakeChain(monkeypatch)
    result = runner.invoke(app, ["lock", "--seller", SELLER, "--max", "0"])
    assert result.exit_code == 2


# ---------------------------------------------------------------------------
# call — full flow with a mocked relay response
# ---------------------------------------------------------------------------


def _mock_http(monkeypatch, receipt_header_str: str | None, settle: str = "settled"):
    body = json.dumps(
        {
            "choices": [{"message": {"content": "Hello from the seller relay!"}}],
            "usage": {
                "prompt_tokens": 10,
                "completion_tokens": 5,
                "prompt_tokens_details": {"cached_tokens": 0},
            },
        }
    )
    headers = {"Content-Type": "application/json", "X-Settle-Status": settle}
    if receipt_header_str is not None:
        headers["X-Receipt"] = receipt_header_str

    seen = {}

    def fake_post(endpoint, payment_id, signature, body_bytes, timeout=120.0):
        seen["endpoint"] = endpoint
        seen["payment_id"] = payment_id
        seen["signature"] = signature
        seen["body"] = body_bytes
        return RelayResponse(status_code=200, headers=headers, body_text=body)

    monkeypatch.setattr(app_mod, "post_chat_json", fake_post)
    return seen


def test_call_success_flow(monkeypatch, tmp_path):
    _set_full_env(monkeypatch)
    fake = FakeChain(monkeypatch)
    receipt = make_receipt(SELLER_KEY, 42, seller_addr=SELLER)
    seen = _mock_http(monkeypatch, receipt_header(receipt))

    disputes = tmp_path / "disputes.json"
    result = runner.invoke(
        app,
        ["--disputes-file", str(disputes), "call", "hello there", "--seller", SELLER],
    )
    assert result.exit_code == 0, all_output(result)
    out = all_output(result)

    # lock happened with pricing-derived default maxAmount
    lock_call = [c for c in fake.calls if c[0] == "lock"][0]
    assert lock_call[1] == SELLER
    # default max = ceil((1000*200000 + 1000*32000)/1e6) = 232 (232_000_000)
    assert lock_call[2] == 232_000_000
    assert lock_call[3] == 600

    # paymentId printed and passed to HTTP
    assert "paymentId: 42" in out
    assert seen["payment_id"] == 42
    assert seen["endpoint"] == "http://127.0.0.1:8787"  # raw endpoint; URL join tested separately
    assert seen["body"].startswith(b'{"model":"gpt-4o-mini"')

    # output includes reply, settle status, receipt verification result
    assert "Hello from the seller relay!" in out
    assert "Settle status: settled" in out
    assert "Receipt verification: OK" in out
    assert "actual=7 native" in out

    # no dispute was recorded
    assert not disputes.exists()


def test_call_tampered_receipt_records_dispute(monkeypatch, tmp_path):
    _set_full_env(monkeypatch)
    FakeChain(monkeypatch)
    receipt = make_receipt(SELLER_KEY, 42, seller_addr=SELLER)
    receipt["message"]["actualAmount"] = 777  # tamper
    _mock_http(monkeypatch, receipt_header(receipt))

    disputes = tmp_path / "disputes.json"
    result = runner.invoke(
        app,
        ["--disputes-file", str(disputes), "call", "hello there", "--seller", SELLER],
    )
    assert result.exit_code == 0
    out = all_output(result)
    assert "verification FAILED" in out
    ledger = json.loads(disputes.read_text())
    assert "42" in ledger
    assert ledger["42"][0]["reason"] == "receipt-recover-mismatch"
    assert ledger["42"][0]["expected_seller"] == SELLER


def _patch_receipt_polling(monkeypatch, responses):
    """Patch the GET /receipt polling transport + the app clock so the M1
    polling loop runs deterministically without real sleeping.

    `responses` is a list-like of (status_code, text) returned per call.
    Returns the list of requested URLs.
    """
    urls = []

    class FakeResp:
        def __init__(self, status_code, text=""):
            self.status_code = status_code
            self.text = text

    def fake_get(url, timeout=None):
        urls.append(url)
        status_code, text = responses[min(len(urls) - 1, len(responses) - 1)]
        return FakeResp(status_code, text)

    monkeypatch.setattr(httpx, "get", fake_get)
    clock = {"now": 0.0}
    monkeypatch.setattr(app_mod.time, "monotonic", lambda: clock["now"])

    def fake_sleep(seconds):
        clock["now"] += seconds

    monkeypatch.setattr(app_mod.time, "sleep", fake_sleep)
    return urls


def test_call_settled_without_receipt_warns(monkeypatch, tmp_path):
    _set_full_env(monkeypatch)
    FakeChain(monkeypatch)
    _mock_http(monkeypatch, None)  # settled but no receipt -> poll, then warning (not dispute per PIN)
    _patch_receipt_polling(monkeypatch, [(404, "")])
    result = runner.invoke(app, ["call", "hi", "--seller", SELLER, "--timeout", "1"])
    assert result.exit_code == 0
    out = all_output(result)
    assert "no receipt available" in out
    assert "polled GET /receipt/42" in out  # M1: polling exhausted, not a single-shot GET


def test_call_reuses_payment_id(monkeypatch, tmp_path):
    _set_full_env(monkeypatch)
    fake = FakeChain(
        monkeypatch,
        payment={
            "payment_id": 7,
            "buyer": addr_of(BUYER_KEY),
            "seller": SELLER,
            "max_amount": 1_000_000,
            "expires_at": 9999,
            "state": 1,
            "state_name": "Locked",
        },
    )
    receipt = make_receipt(SELLER_KEY, 7, seller_addr=SELLER)
    seen = _mock_http(monkeypatch, receipt_header(receipt))
    result = runner.invoke(app, ["call", "hi", "--seller", SELLER, "--payment-id", "7"])
    assert result.exit_code == 0
    out = all_output(result)
    assert "(reused lock)" in out
    assert not any(c[0] == "lock" for c in fake.calls)
    assert seen["payment_id"] == 7


def test_call_rejects_non_locked_reuse(monkeypatch):
    _set_full_env(monkeypatch)
    FakeChain(
        monkeypatch,
        payment={
            "payment_id": 7,
            "buyer": addr_of(BUYER_KEY),
            "seller": SELLER,
            "max_amount": 1_000_000,
            "expires_at": 9999,
            "state": 2,
            "state_name": "Settled",
        },
    )
    result = runner.invoke(app, ["call", "hi", "--seller", SELLER, "--payment-id", "7"])
    assert result.exit_code == 2
    assert "not Locked" in all_output(result)


def test_call_requires_seller(monkeypatch):
    _set_full_env(monkeypatch)
    monkeypatch.delenv("SELLER_ADDR", raising=False)
    import tokenshare_cli.config as config_mod

    monkeypatch.setattr(config_mod, "load_seller_override", lambda: None)
    FakeChain(monkeypatch)
    result = runner.invoke(app, ["call", "hi"])
    assert result.exit_code == 2
    assert "--seller" in all_output(result)


def test_call_inactive_listing(monkeypatch):
    _set_full_env(monkeypatch)
    listing = {
        "operator": SELLER,
        "endpoint": "http://127.0.0.1:8787",
        "models": ["gpt-4o-mini"],
        "price_cached_in": 100,
        "price_input": 1000,
        "price_output": 1000,
        "active": False,
    }
    FakeChain(monkeypatch, listing=listing)
    result = runner.invoke(app, ["call", "hi", "--seller", SELLER])
    assert result.exit_code == 2
    assert "inactive" in all_output(result)


def test_call_stream_flow(monkeypatch, tmp_path):
    _set_full_env(monkeypatch)
    FakeChain(monkeypatch)
    receipt = make_receipt(SELLER_KEY, 42, seller_addr=SELLER)
    headers = {"X-Settle-Status": "settled", "X-Receipt": receipt_header(receipt)}
    seen = {}
    closed = {"n": 0}

    def fake_stream(endpoint, payment_id, signature, body_bytes, timeout=120.0):
        seen["payment_id"] = payment_id
        assert json.loads(body_bytes)["stream"] is True
        lines = [
            "data: " + json.dumps({"choices": [{"delta": {"content": "Hi"}}]}),
            "",
            "data: " + json.dumps({"choices": [], "usage": {"prompt_tokens": 3, "completion_tokens": 1}}),
            "",
            "data: [DONE]",
        ]
        from tokenshare_cli.relay_client import RelayStreamHandle

        return RelayStreamHandle(
            status_code=200, headers=headers, lines=iter(lines),
            close=lambda: closed.__setitem__("n", closed["n"] + 1),  # n2: closed after [DONE]
        )

    monkeypatch.setattr(app_mod, "open_chat_stream", fake_stream)
    result = runner.invoke(app, ["call", "hi", "--seller", SELLER, "--stream"])
    assert result.exit_code == 0, all_output(result)
    out = all_output(result)
    assert "Hi" in out
    assert "Settle status: settled" in out
    assert "Receipt verification: OK" in out
    assert seen["payment_id"] == 42
    assert closed["n"] == 1  # n2: stream handle closed exactly once after [DONE]


def test_call_relay_override(monkeypatch):
    _set_full_env(monkeypatch)
    FakeChain(monkeypatch)
    receipt = make_receipt(SELLER_KEY, 42, seller_addr=SELLER)
    seen = _mock_http(monkeypatch, receipt_header(receipt))
    result = runner.invoke(app, ["call", "hi", "--seller", SELLER, "--relay", "http://other:9999/"])
    assert result.exit_code == 0, all_output(result)
    assert seen["endpoint"] == "http://other:9999/"  # --relay override honored
    assert "POST http://other:9999/v1/chat/completions" in all_output(result)  # PIN path joined


def test_relay_url_joins_chat_path():
    from tokenshare_cli.relay_client import _url

    assert _url("http://host:8787") == "http://host:8787" + "/v1/chat/completions"
    assert _url("http://host:8787/") == "http://host:8787" + "/v1/chat/completions"


# ---------------------------------------------------------------------------
# refund
# ---------------------------------------------------------------------------


def test_refund_flow(monkeypatch):
    _set_full_env(monkeypatch)
    fake = FakeChain(monkeypatch)
    result = runner.invoke(app, ["refund", "--payment-id", "42"])
    assert result.exit_code == 0
    assert ("refund", 42) in fake.calls
    assert "refunded amount: 1000000 native (= 1 USDC)" in all_output(result)


# ---------------------------------------------------------------------------
# disputes view
# ---------------------------------------------------------------------------


def test_disputes_command_lists_records(monkeypatch, tmp_path):
    from tokenshare_cli import disputes as dispute_mod

    path = tmp_path / "d.json"
    dispute_mod.record_dispute(path, 42, dispute_mod.REASON_RECOVER_MISMATCH, "0xexpected", "0xrecovered")
    result = runner.invoke(app, ["--disputes-file", str(path), "disputes"])
    assert result.exit_code == 0
    out = all_output(result)
    assert "paymentId 42" in out
    assert "receipt-recover-mismatch" in out


# ---------------------------------------------------------------------------
# M1: GET /receipt polling fallback (404 until the relay settles, then 200)
# ---------------------------------------------------------------------------


def test_call_receipt_polled_via_get_fallback(monkeypatch, tmp_path):
    """M1: no X-Receipt header -> CLI polls GET /receipt/{id}; relay settles
    0.3-2s AFTER the response, so the first GETs 404 and a later one 200."""
    _set_full_env(monkeypatch)
    FakeChain(monkeypatch)
    receipt = make_receipt(SELLER_KEY, 42, seller_addr=SELLER)
    body = json.dumps(
        {
            "choices": [{"message": {"content": "Hello from the seller relay!"}}],
            "usage": {"prompt_tokens": 10, "completion_tokens": 5},
        }
    )
    headers = {"Content-Type": "application/json", "X-Settle-Status": "settled"}  # no X-Receipt
    monkeypatch.setattr(app_mod, "post_chat_json", lambda *a, **k: RelayResponse(200, headers, body))

    # The GET body is the receipt JSON object (app re-encodes to base64url).
    get_text = json.dumps(json.loads(json.dumps(receipt)))
    urls = _patch_receipt_polling(monkeypatch, [(404, ""), (404, ""), (200, get_text)])

    disputes = tmp_path / "disputes.json"
    result = runner.invoke(
        app,
        ["--disputes-file", str(disputes), "call", "hi", "--seller", SELLER, "--timeout", "1"],
    )
    assert result.exit_code == 0, all_output(result)
    out = all_output(result)
    assert "Receipt verification: OK" in out  # found via polling, NOT a settle-failed warning
    assert "no receipt available" not in out
    assert len(urls) == 3  # 404 -> 404 -> 200
    assert urls[0].endswith("/receipt/42")
    assert not disputes.exists()


def test_call_receipt_polling_gives_up_after_deadline(monkeypatch, tmp_path):
    """M1: 404s all the way to the deadline -> settle-failed warning, no crash."""
    _set_full_env(monkeypatch)
    FakeChain(monkeypatch)
    _mock_http(monkeypatch, None)
    _patch_receipt_polling(monkeypatch, [(404, "")])
    result = runner.invoke(app, ["call", "hi", "--seller", SELLER, "--timeout", "1"])
    assert result.exit_code == 0
    assert "no receipt available" in all_output(result)


# ---------------------------------------------------------------------------
# m1: domain verification negative (chainId=999 receipt must NOT verify)
# ---------------------------------------------------------------------------


def test_call_receipt_wrong_chain_id_records_dispute(monkeypatch, tmp_path):
    _set_full_env(monkeypatch)
    FakeChain(monkeypatch)
    receipt = make_receipt(SELLER_KEY, 42, seller_addr=SELLER, chain_id=999)  # wrong chainId
    _mock_http(monkeypatch, receipt_header(receipt))

    disputes = tmp_path / "disputes.json"
    result = runner.invoke(
        app,
        ["--disputes-file", str(disputes), "call", "hi", "--seller", SELLER],
    )
    assert result.exit_code == 0
    out = all_output(result)
    assert "verification FAILED" in out
    assert "domain-mismatch" in out
    ledger = json.loads(disputes.read_text())
    assert "42" in ledger
    assert ledger["42"][0]["reason"] == "receipt-domain-mismatch"


# ---------------------------------------------------------------------------
# RelayError (401/402) command-level output
# ---------------------------------------------------------------------------


def test_call_relay_402_error_message(monkeypatch):
    """Relay 402 (lock below relay minimum) -> clean exit, actionable stderr."""
    _set_full_env(monkeypatch)
    FakeChain(monkeypatch)

    def fake_post(*a, **k):
        raise RelayError(402, '{"error":"maxAmount below relay minimum"}')

    monkeypatch.setattr(app_mod, "post_chat_json", fake_post)
    result = runner.invoke(app, ["call", "hi", "--seller", SELLER])
    assert result.exit_code == 2
    out = all_output(result)
    assert "relay HTTP 402" in out
    assert "maxAmount below relay minimum" in out


def test_call_relay_401_error_message(monkeypatch):
    """Relay 401 (bad signature/payment) -> clean exit, actionable stderr."""
    _set_full_env(monkeypatch)
    FakeChain(monkeypatch)

    def fake_post(*a, **k):
        raise RelayError(401, '{"error":"invalid signature"}')

    monkeypatch.setattr(app_mod, "post_chat_json", fake_post)
    result = runner.invoke(app, ["call", "hi", "--seller", SELLER])
    assert result.exit_code == 2
    out = all_output(result)
    assert "relay HTTP 401" in out
    assert "invalid signature" in out


# ---------------------------------------------------------------------------
# m2: --relay plain-http warning
# ---------------------------------------------------------------------------


def test_call_relay_plain_http_warns(monkeypatch):
    _set_full_env(monkeypatch)
    FakeChain(monkeypatch)
    receipt = make_receipt(SELLER_KEY, 42, seller_addr=SELLER)
    seen = _mock_http(monkeypatch, receipt_header(receipt))
    result = runner.invoke(app, ["call", "hi", "--seller", SELLER, "--relay", "http://other:9999/"])
    assert result.exit_code == 0, all_output(result)
    out = all_output(result)
    assert "warning: --relay" in out
    assert "not https" in out
    assert seen["endpoint"] == "http://other:9999/"


def test_call_relay_localhost_no_warning(monkeypatch):
    _set_full_env(monkeypatch)
    FakeChain(monkeypatch)
    receipt = make_receipt(SELLER_KEY, 42, seller_addr=SELLER)
    _mock_http(monkeypatch, receipt_header(receipt))
    result = runner.invoke(app, ["call", "hi", "--seller", SELLER, "--relay", "http://127.0.0.1:8787"])
    assert result.exit_code == 0, all_output(result)
    assert "warning: --relay" not in all_output(result)
