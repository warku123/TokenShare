"""Command-level tests: argument assembly, env validation, and the full
`call` flow with a mocked relay response (including receipt verification
positive + tampered-negative cases and dispute recording).

No real chain: chain helpers are monkeypatched. No real relay: the HTTP
client functions are monkeypatched.
"""

import json
import types

import httpx
import pytest

from eth_account import Account
from eth_utils import to_checksum_address
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
from tokenshare_cli.errors import RelayError, TokenshareError  # noqa: E402
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

    def __init__(
        self,
        monkeypatch,
        *,
        listing=None,
        escrow_balance=10**9,
        payment=None,
        remove_model_error: str | None = None,
    ):
        self.calls = []
        # Registry v2 shape (M9): models + parallel per-model prices array.
        self.listing = listing or {
            "operator": SELLER,
            "endpoint": "http://127.0.0.1:8787",
            "models": ["gpt-4o-mini"],
            "prices": [{"cached_in": 100, "input": 1000, "output": 1000}],
            "active": True,
        }
        self.escrow_balance = escrow_balance
        self.payment = payment
        self.remove_model_error = remove_model_error
        self.remove_model_calls: list[tuple[str, str, str]] = []
        self.w3 = types.SimpleNamespace()  # app passes ctx.w3 to remove_model

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
        monkeypatch.setattr(chain_mod, "remove_model", ctx._remove_model)

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

    def _remove_model(self, w3, registry_addr, account, model):
        """M12 removeModel stand-in. Three paths via remove_model_error:
        None -> success (ModelRemoved decoded); a string containing
        ModelNotFound / RemoveLastModel -> the corresponding on-chain guard,
        raised exactly as chain.remove_model surfaces reverts."""
        self.remove_model_calls.append(
            (str(registry_addr), getattr(account, "address", str(account)), str(model))
        )
        if self.remove_model_error is not None:
            raise TokenshareError(self.remove_model_error)
        return {
            "operator": getattr(account, "address", self.address),
            "model": str(model),
            "tx_hash": "0xremoved",
        }


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
# default lock sizing formula (R2, now per-model — M9 Registry v2)
# ---------------------------------------------------------------------------


def test_default_lock_amount_known_prices(monkeypatch):
    """Per-model price (M9): input=2e6, output=3e6, caps 200000/32000 ->
    estimate = (2e6*200000 + 3e6*32000)//1e6 = 496_000 (matches relay
    minAmount floor for THIS model) -> ceil to whole USDC = 1_000_000
    (also the 1 USDC floor)."""
    from tokenshare_cli.app import _default_lock_amount

    monkeypatch.delenv("PROMPT_TOKEN_CAP", raising=False)
    monkeypatch.delenv("COMPLETION_TOKEN_CAP", raising=False)
    price = {"cached_in": 0, "input": 2_000_000, "output": 3_000_000}
    assert _default_lock_amount(price) == 1_000_000


def test_default_lock_amount_small_prices_hits_usdc_floor(monkeypatch):
    """priceInput=1000, priceOutput=1000 (the FakeChain default listing) ->
    estimate = (1000*200000 + 1000*32000)//1e6 = 232 (USDC-native, matches
    relay minAmount) -> ceil to whole USDC = 1_000_000 (below the 1 USDC
    floor -> exactly 1 USDC)."""
    from tokenshare_cli.app import _default_lock_amount

    monkeypatch.delenv("PROMPT_TOKEN_CAP", raising=False)
    monkeypatch.delenv("COMPLETION_TOKEN_CAP", raising=False)
    price = {"cached_in": 100, "input": 1000, "output": 1000}
    assert _default_lock_amount(price) == 1_000_000


def test_default_lock_amount_large_prices_ceil_to_whole_usdc(monkeypatch):
    """Big-price scenario: estimate exceeds 1e6 -> rounded UP to whole USDC.
    priceInput=9_000_000, priceOutput=9_999_999, caps 200000/32000 ->
    estimate = (9e6*200000 + 9999999*32000)//1e6 = 2_119_999  (matches relay
    minAmount) -> ceil to whole USDC = 3_000_000."""
    from tokenshare_cli.app import _default_lock_amount

    monkeypatch.delenv("PROMPT_TOKEN_CAP", raising=False)
    monkeypatch.delenv("COMPLETION_TOKEN_CAP", raising=False)
    price = {"cached_in": 0, "input": 9_000_000, "output": 9_999_999}
    assert _default_lock_amount(price) == 3_000_000


def test_call_default_lock_uses_selected_model_price(monkeypatch, tmp_path):
    """M9: with two models priced differently, the auto-lock must use the
    price of the SELECTED model (--model wins over models[0])."""
    _set_full_env(monkeypatch)
    listing = {
        "operator": SELLER,
        "endpoint": "http://127.0.0.1:8787",
        "models": ["cheap-model", "fancy-model"],
        "prices": [
            {"cached_in": 0, "input": 1000, "output": 1000},      # est 232 -> 1 USDC floor
            {"cached_in": 0, "input": 10_000_000, "output": 10_000_000},  # est 2_320_000 -> ceil 3 USDC
        ],
        "active": True,
    }
    fake = FakeChain(monkeypatch, listing=listing)
    receipt = make_receipt(SELLER_KEY, 42, seller_addr=SELLER)
    _mock_http(monkeypatch, receipt_header(receipt))

    result = runner.invoke(
        app,
        ["call", "hi", "--seller", SELLER, "--model", "fancy-model"],
    )
    assert result.exit_code == 0, all_output(result)
    lock_call = [c for c in fake.calls if c[0] == "lock"][0]
    # (10_000_000*200_000 + 10_000_000*32_000)//1e6 = 2_320_000 -> ceil 3_000_000
    assert lock_call[2] == 3_000_000
    assert seen_model_price_line(all_output(result), "fancy-model")


def seen_model_price_line(out: str, model: str) -> bool:
    """The call output shows the selected model's three per-1M prices."""
    return f"model: {model}  (cached/1M:" in out


def test_call_unknown_model_fails_fast(monkeypatch):
    """M9: --model not in the listing -> exit 2 BEFORE locking or HTTP."""
    _set_full_env(monkeypatch)
    fake = FakeChain(monkeypatch)
    seen = _mock_http(monkeypatch, None)  # would succeed if ever reached

    result = runner.invoke(
        app, ["call", "hi", "--seller", SELLER, "--model", "no-such-model"]
    )
    assert result.exit_code == 2
    out = all_output(result)
    assert "no-such-model" in out
    assert "not in the listing" in out
    assert not any(c[0] == "lock" for c in fake.calls)  # no lock happened
    assert "payment_id" not in seen  # relay never contacted


def test_call_blank_model_fails_when_listing_only_has_blank_models(monkeypatch):
    """M9: listing whose model list is only blank strings -> clear error."""
    _set_full_env(monkeypatch)
    listing = {
        "operator": SELLER,
        "endpoint": "http://127.0.0.1:8787",
        "models": ["", "  "],
        "prices": [
            {"cached_in": 0, "input": 1, "output": 1},
            {"cached_in": 0, "input": 1, "output": 1},
        ],
        "active": True,
    }
    FakeChain(monkeypatch, listing=listing)
    result = runner.invoke(app, ["call", "hi", "--seller", SELLER])
    assert result.exit_code == 2
    assert "listing has no models" in all_output(result)


def test_call_shows_selected_model_prices(monkeypatch, tmp_path):
    """M9: call prints the selected model's three-tier per-1M prices."""
    _set_full_env(monkeypatch)
    FakeChain(monkeypatch)  # default listing: 100/1000/1000 for gpt-4o-mini
    receipt = make_receipt(SELLER_KEY, 42, seller_addr=SELLER)
    _mock_http(monkeypatch, receipt_header(receipt))

    result = runner.invoke(app, ["call", "hi", "--seller", SELLER])
    assert result.exit_code == 0, all_output(result)
    out = all_output(result)
    assert "model: gpt-4o-mini  (cached/1M: 100 native (= 0.0001 USDC)" in out
    assert "input/1M: 1000 native (= 0.001 USDC)" in out
    assert "output/1M: 1000 native (= 0.001 USDC)" in out


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
    # default max = ceil((1000*200000 + 1000*32000)/1e6 / 1e6) * 1e6 = 1 USDC floor
    assert lock_call[2] == 1_000_000
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
    # PIN v1.1 audit fields are displayed
    assert "upstreamHost=api.moonshot.cn" in out
    assert "model=kimi-k2.6" in out

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
        "prices": [{"cached_in": 100, "input": 1000, "output": 1000}],
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
# remove-model (M12 Registry v4 — operator-side)
# ---------------------------------------------------------------------------


def test_remove_model_success(monkeypatch):
    """Default identity: BUYER_PRIVATE_KEY signs (demo SELLER==BUYER); the
    helper receives (w3, REGISTRY_ADDR, operator account, model)."""
    _set_full_env(monkeypatch)
    fake = FakeChain(monkeypatch)
    result = runner.invoke(app, ["remove-model", "gpt-4o-mini"])
    assert result.exit_code == 0, all_output(result)
    out = all_output(result)
    assert "model removed: gpt-4o-mini" in out
    assert f"operator {addr_of(BUYER_KEY)}" in out
    assert "tx: 0xremoved" in out
    assert fake.remove_model_calls == [
        # load_config checksums REGISTRY_ADDR before it reaches the helper
        (to_checksum_address(REGISTRY_ADDR), addr_of(BUYER_KEY), "gpt-4o-mini")
    ]


def test_remove_model_key_env_override_signs_as_operator(monkeypatch):
    """--key-env points at any env var holding the real listing-operator key
    (seller != buyer setups)."""
    _set_full_env(monkeypatch)
    monkeypatch.setenv("SELLER_PRIVATE_KEY", SELLER_KEY)
    fake = FakeChain(monkeypatch)
    result = runner.invoke(
        app, ["remove-model", "gpt-4o-mini", "--key-env", "SELLER_PRIVATE_KEY"]
    )
    assert result.exit_code == 0, all_output(result)
    assert fake.remove_model_calls[0][1] == addr_of(SELLER_KEY)


def test_remove_model_model_not_found_hint(monkeypatch):
    _set_full_env(monkeypatch)
    FakeChain(
        monkeypatch,
        remove_model_error=(
            "transaction failed: execution reverted "
            'ModelNotFound("ghost-model")'
        ),
    )
    result = runner.invoke(app, ["remove-model", "ghost-model"])
    assert result.exit_code == 2
    out = all_output(result)
    assert "not in your listing" in out
    assert "ModelNotFound" in out
    assert "ghost-model" in out


def test_remove_model_last_model_hint(monkeypatch):
    _set_full_env(monkeypatch)
    FakeChain(
        monkeypatch,
        remove_model_error="transaction failed: execution reverted RemoveLastModel()",
    )
    result = runner.invoke(app, ["remove-model", "gpt-4o-mini"])
    assert result.exit_code == 2
    out = all_output(result)
    assert "LAST model" in out
    assert "deactivate()" in out  # points at the PIN-suggested alternative
    assert "RemoveLastModel" in out


def test_remove_model_generic_tx_failure_passes_through(monkeypatch):
    _set_full_env(monkeypatch)
    FakeChain(monkeypatch, remove_model_error="transaction failed: node unreachable")
    result = runner.invoke(app, ["remove-model", "gpt-4o-mini"])
    assert result.exit_code == 2
    assert "node unreachable" in all_output(result)


def test_remove_model_missing_key_env(monkeypatch):
    _set_full_env(monkeypatch)
    monkeypatch.delenv("SELLER_PRIVATE_KEY", raising=False)
    fake = FakeChain(monkeypatch)
    result = runner.invoke(
        app, ["remove-model", "m1", "--key-env", "SELLER_PRIVATE_KEY"]
    )
    assert result.exit_code == 2
    out = all_output(result)
    assert "SELLER_PRIVATE_KEY is not set" in out
    assert fake.remove_model_calls == []  # failed before any chain call


def test_remove_model_bad_key_value_withheld(monkeypatch):
    _set_full_env(monkeypatch)
    monkeypatch.setenv("SELLER_PRIVATE_KEY", "0xzz-nope")
    FakeChain(monkeypatch)
    result = runner.invoke(
        app, ["remove-model", "m1", "--key-env", "SELLER_PRIVATE_KEY"]
    )
    assert result.exit_code == 2
    out = all_output(result)
    assert "SELLER_PRIVATE_KEY" in out
    assert "valid private key" in out
    assert "0xzz-nope" not in out  # the key value is never echoed


class FakeRemoveRegistry:
    """Raw web3 Registry surface for remove_model helper tests."""

    def __init__(self, *, event_args=None):
        self.remove_calls: list[str] = []
        self.built: dict | None = None
        self.event_args = event_args or {}

    @property
    def functions(self):
        reg = self

        def removeModel(model):
            reg.remove_calls.append(str(model))

            class _Fn:
                def build_transaction(self, base):
                    reg.built = base  # same object web3 would return
                    return base

            return _Fn()

        return types.SimpleNamespace(removeModel=removeModel)

    @property
    def events(self):
        reg = self

        def ModelRemoved():
            def process_receipt(receipt):
                if reg.event_args is None:
                    return []
                return [{"args": dict(reg.event_args)}]

            return types.SimpleNamespace(process_receipt=process_receipt)

        return types.SimpleNamespace(ModelRemoved=ModelRemoved)


def _fake_remove_w3(
    registry: FakeRemoveRegistry,
    *,
    receipt_status: int = 1,
    estimate_error: Exception | None = None,
) -> types.SimpleNamespace:
    class FakeEth:
        chain_id = 31337
        gas_price = 1_000
        tx_hash = b"\x11" * 32

        def contract(self, address, abi):
            self.contract_addr = address
            self.contract_abi = abi
            return registry

        def get_transaction_count(self, address):
            return 7

        def estimate_gas(self, tx):
            if estimate_error is not None:
                raise estimate_error
            self.estimated_tx = dict(tx)
            return 60_000

        def send_raw_transaction(self, raw):
            self.sent_raw = raw
            return self.tx_hash

        def wait_for_transaction_receipt(self, tx_hash, timeout=None, poll_latency=None):
            return {"status": receipt_status, "transactionHash": tx_hash}

    return types.SimpleNamespace(eth=FakeEth())


def test_remove_model_helper_sends_operator_signed_tx():
    """Helper plumbing: gas estimate +1.25 buffer, operator as `from`, event
    decode wins over the fallback identity."""
    from tokenshare_cli.chain import remove_model

    operator_account = Account.from_key(BUYER_KEY)
    registry = FakeRemoveRegistry(
        event_args={"operator": operator_account.address, "model": "m1"}
    )
    w3 = _fake_remove_w3(registry)

    result = remove_model(w3, REGISTRY_ADDR, operator_account, "m1")

    assert registry.remove_calls == ["m1"]
    assert registry.built == {
        "from": operator_account.address,
        "nonce": 7,
        "gasPrice": 1_000,
        "chainId": 31337,
        "gas": 75_000,  # 60_000 * 1.25
    }
    assert result == {
        "operator": operator_account.address,
        "model": "m1",
        "tx_hash": (b"\x11" * 32).hex(),
    }
    assert w3.eth.contract_addr == REGISTRY_ADDR  # bound to the configured registry


def test_remove_model_helper_accepts_raw_key_string():
    from tokenshare_cli.chain import remove_model

    registry = FakeRemoveRegistry(event_args={"model": "m1"})
    w3 = _fake_remove_w3(registry)

    result = remove_model(w3, REGISTRY_ADDR, BUYER_KEY, "m1")

    assert result["operator"] == addr_of(BUYER_KEY)  # derived from the raw key


def test_remove_model_helper_reverted_receipt_raises():
    from tokenshare_cli.chain import remove_model

    registry = FakeRemoveRegistry(event_args={"model": "m1"})
    w3 = _fake_remove_w3(registry, receipt_status=0)

    with pytest.raises(TokenshareError, match="reverted on-chain"):
        remove_model(w3, REGISTRY_ADDR, BUYER_KEY, "m1")


def test_remove_model_helper_wraps_guard_revert():
    """A revert at eth_estimateGas (ModelNotFound/RemoveLastModel) surfaces
    as TokenshareError carrying the chain's message — the command layer maps
    the tokens to hints."""
    from tokenshare_cli.chain import remove_model

    registry = FakeRemoveRegistry(event_args={"model": "m1"})
    w3 = _fake_remove_w3(
        registry, estimate_error=ValueError('execution reverted ModelNotFound("m1")')
    )

    with pytest.raises(TokenshareError, match="ModelNotFound"):
        remove_model(w3, REGISTRY_ADDR, BUYER_KEY, "m1")


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
