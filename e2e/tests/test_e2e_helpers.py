"""e2e helper tests — all mocked, zero real network / zero real chain.

  - dotenv_loader: load / no-overwrite / missing-file / quote-stripping;
  - register_listing pre-check: refusal paths driven by a mocked relay HTTP
    response (httpx is pointed at a local threaded stub).
"""

from __future__ import annotations

import json
import os
import sys
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any

import pytest

E2E_DIR = Path(__file__).resolve().parent
sys.path.insert(0, str(E2E_DIR))

from dotenv_loader import load_dotenv  # noqa: E402


# ------------------------------------------------------------- dotenv_loader


def test_loads_missing_values_into_environ(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    env_file = tmp_path / ".env"
    env_file.write_text(
        "# comment line\n"
        "\n"
        "FOO_BAR_TOKENSHARE=hello\n"
        'QUOTED_TOKENSHARE="double quoted"\n'
        "SINGLE_TOKENSHARE='single quoted'\n",
        encoding="utf-8",
    )
    monkeypatch.delenv("FOO_BAR_TOKENSHARE", raising=False)
    monkeypatch.delenv("QUOTED_TOKENSHARE", raising=False)
    monkeypatch.delenv("SINGLE_TOKENSHARE", raising=False)
    load_dotenv(env_file)
    assert os.environ["FOO_BAR_TOKENSHARE"] == "hello"
    assert os.environ["QUOTED_TOKENSHARE"] == "double quoted"
    assert os.environ["SINGLE_TOKENSHARE"] == "single quoted"


def test_does_not_overwrite_existing_environ(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    (tmp_path / ".env").write_text("KEEP_ME_TOKENSHARE=from-file\n", encoding="utf-8")
    monkeypatch.setenv("KEEP_ME_TOKENSHARE", "from-shell")
    load_dotenv(tmp_path / ".env")
    assert os.environ["KEEP_ME_TOKENSHARE"] == "from-shell"


def test_inline_comment_is_stripped(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    (tmp_path / ".env").write_text(
        'ADDR_TOKENSHARE=0xabc123  # auto from deployed.json\n'
        'HASHISH_TOKENSHARE="val#keep"  # trailing\n'
        'EMPTY_TOKENSHARE=  # comment-only value\n'
        "UNCLOSED_TOKENSHARE='no close\n",
        encoding="utf-8",
    )
    monkeypatch.delenv("ADDR_TOKENSHARE", raising=False)
    monkeypatch.delenv("HASHISH_TOKENSHARE", raising=False)
    monkeypatch.delenv("EMPTY_TOKENSHARE", raising=False)
    monkeypatch.delenv("UNCLOSED_TOKENSHARE", raising=False)
    load_dotenv(tmp_path / ".env")
    assert os.environ["ADDR_TOKENSHARE"] == "0xabc123"
    assert os.environ["HASHISH_TOKENSHARE"] == "val#keep"  # quoted value keeps #
    assert "EMPTY_TOKENSHARE" not in os.environ  # comment-only → skipped, no ""
    assert os.environ["UNCLOSED_TOKENSHARE"] == "no close"


def test_missing_file_is_silent(tmp_path: Path) -> None:
    load_dotenv(tmp_path / "nope.env")  # must not raise, must not print


def test_repo_dotenv_loads_seller_and_buyer_keys() -> None:
    """The real repo .env (present per the task baseline) feeds os.environ —
    values are asserted only by presence, never printed."""
    repo_env = E2E_DIR.parent / ".env"
    if not repo_env.exists():
        pytest.skip("repo .env not present in this checkout")
    load_dotenv()
    assert bool(os.environ.get("SELLER_PRIVATE_KEY"))
    assert bool(os.environ.get("BUYER_PRIVATE_KEY"))


# ------------------------------------------------ register_listing pre-check


class _RelayStub(BaseHTTPRequestHandler):
    """GET /verify-upstream → whatever body/next_status the test set."""

    body: dict[str, Any] = {}
    next_status: int = 200
    down: bool = False

    def log_message(self, *args: Any) -> None:
        pass

    def do_GET(self) -> None:
        if self.down:
            self.close_connection = True
            try:
                self.connection.close()
            except OSError:
                pass
            return
        payload = json.dumps(dict(_RelayStub.body)).encode()
        self.send_response(_RelayStub.next_status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(payload)))
        self.end_headers()
        self.wfile.write(payload)


@pytest.fixture()
def relay_stub() -> ThreadingHTTPServer:
    _RelayStub.body = {
        "key_valid": True,
        "upstream_host": "api.moonshot.cn",
        "accessible_models": ["kimi-k2.6", "kimi-k2.0"],
        "listing_ok": True,
        "listed_models": [],
        "mismatches": [],
    }
    _RelayStub.next_status = 200
    _RelayStub.down = False
    srv = ThreadingHTTPServer(("127.0.0.1", 0), _RelayStub)
    t = threading.Thread(target=srv.serve_forever, daemon=True)
    t.start()
    yield srv
    srv.shutdown()
    srv.server_close()


def _precheck(relay_url: str, models: list[str]) -> None:
    import register_listing as rl

    rl.verify_upstream_precheck(relay_url, models)


def test_precheck_passes_when_all_models_accessible(relay_stub: Any) -> None:
    _precheck(f"http://127.0.0.1:{relay_stub.server_port}", ["kimi-k2.6"])


def test_precheck_rejects_invalid_key(
    relay_stub: Any, capsys: pytest.CaptureFixture
) -> None:
    _RelayStub.body["key_valid"] = False
    _RelayStub.body["error"] = "invalid api key"
    with pytest.raises(SystemExit) as ei:
        _precheck(f"http://127.0.0.1:{relay_stub.server_port}", ["kimi-k2.6"])
    assert "NOT valid" in str(ei.value.code)


def test_precheck_rejects_missing_model(
    relay_stub: Any, capsys: pytest.CaptureFixture
) -> None:
    with pytest.raises(SystemExit) as ei:
        _precheck(
            f"http://127.0.0.1:{relay_stub.server_port}",
            ["kimi-k2.6", "gpt-fake-dream"],
        )
    assert "gpt-fake-dream" in str(ei.value.code)
    assert "accessible" in str(ei.value.code)


def test_precheck_unreachable_relay_warns_and_continues(
    capsys: pytest.CaptureFixture,
) -> None:
    # Nothing listens on port 9 — must NOT SystemExit, only print a warning.
    _precheck("http://127.0.0.1:9", ["kimi-k2.6"])
    assert "WARNING" in capsys.readouterr().out


def test_precheck_non_200_warns_and_continues(
    relay_stub: Any, capsys: pytest.CaptureFixture
) -> None:
    _RelayStub.next_status = 500
    _precheck(f"http://127.0.0.1:{relay_stub.server_port}", ["kimi-k2.6"])
    assert "WARNING" in capsys.readouterr().out


# --------------------------------------------- register_listing v2 price book
# Registry v2 (M9): register takes Price[] parallel to models[] — the parser
# below is the e2e-side helper that assembles those parallel arrays.


def _book(
    model_flags: list[str],
    model_price_flags: list[str],
    first_prices: dict[str, int] | None = None,
) -> tuple[list[str], list[dict[str, int]]]:
    import register_listing as rl

    return rl.build_price_book(model_flags, model_price_flags, first_prices)


def test_price_book_default_model_gets_first_triple() -> None:
    models, prices = _book([], [], {"cached": 1, "input": 2, "output": 3})
    assert models == ["gpt-4o-mini-tokenshare"]
    assert prices == [{"cached": 1, "input": 2, "output": 3}]


def test_price_book_per_model_prices_parallel() -> None:
    models, prices = _book(
        ["kimi-k2.6"], ["kimi-k2.0:5:6:7"], {"cached": 1, "input": 2, "output": 3}
    )
    assert models == ["kimi-k2.6", "kimi-k2.0"]  # first-seen order, --model first
    assert prices == [  # parallel to models (Registry v2 PIN)
        {"cached": 1, "input": 2, "output": 3},
        {"cached": 5, "input": 6, "output": 7},
    ]


def test_price_book_explicit_first_price_wins_over_triple() -> None:
    models, prices = _book(["m1"], ["m1:9:9:9"], {"cached": 1, "input": 2, "output": 3})
    assert models == ["m1"]
    assert prices == [{"cached": 9, "input": 9, "output": 9}]


def test_price_book_model_price_appends_new_model_in_order() -> None:
    models, prices = _book(
        [], ["a:1:1:1", "b:2:2:2", "a:3:3:3"], None
    )
    assert models == ["a", "b"]  # a appended once, override keeps position
    assert prices == [{"cached": 3, "input": 3, "output": 3}, {"cached": 2, "input": 2, "output": 2}]


def test_price_book_rejects_unpriced_model() -> None:
    with pytest.raises(SystemExit) as ei:
        _book(["m1", "m2"], [], {"cached": 1, "input": 2, "output": 3})
    assert "m2" in str(ei.value.code)


def test_price_book_rejects_bad_spec() -> None:
    with pytest.raises(SystemExit):
        _book([], ["nope"], None)  # wrong field count
    with pytest.raises(SystemExit):
        _book([], ["m:x:2:3"], None)  # non-integer price
    with pytest.raises(SystemExit):
        _book([], ["m:-1:2:3"], None)  # negative price


# --------------------------------------------- run.REGISTRY_ABI is Registry v2


def test_registry_abi_is_v2_per_model() -> None:
    """run.REGISTRY_ABI must match Registry v2 (M9): register(endpoint,
    string[], Price[]) with per-model parallel prices, updateModelPrice,
    getPrice(operator, model) → Price, getListing →
    (address, string, string[], Price[], bool)."""
    from run import REGISTRY_ABI

    def fn(name: str) -> dict[str, Any]:
        return next(
            f for f in REGISTRY_ABI if f.get("type") == "function" and f.get("name") == name
        )

    assert [i["type"] for i in fn("register")["inputs"]] == ["string", "string[]", "tuple[]"]
    assert fn("register")["inputs"][2]["components"] is not None

    update = fn("updateModelPrice")
    assert [i["type"] for i in update["inputs"]] == ["string", "tuple"]

    get_price = fn("getPrice")
    assert [i["type"] for i in get_price["inputs"]] == ["address", "string"]
    assert [o["type"] for o in get_price["outputs"]] == ["tuple"]

    # v2 getListing returns Listing memory = ONE struct → the ABI must wrap
    # the five fields in a single outer tuple (solc encodes a single-struct
    # return with an outer head-offset; flat outputs cannot decode it).
    listing = fn("getListing")
    assert [o["type"] for o in listing["outputs"]] == ["tuple"]
    assert [c["type"] for c in listing["outputs"][0]["components"]] == [
        "address", "string", "string[]", "tuple[]", "bool",
    ]

    # Registry v3 enumeration (M10): on-chain seller directory.
    count = fn("sellerCount")
    assert count["inputs"] == []
    assert [o["type"] for o in count["outputs"]] == ["uint256"]
    page = fn("getSellers")
    assert [i["type"] for i in page["inputs"]] == ["uint256", "uint256"]
    assert [o["type"] for o in page["outputs"]] == ["address[]"]


# ------------------------------- env pins vs reused artifact (rev-3 C1)


def test_env_artifact_addresses_agree_passes() -> None:
    """env==artifact (case-insensitive) must NOT exit — reuse proceeds."""
    from run import assert_env_artifact_addresses_agree

    assert_env_artifact_addresses_agree(
        {"escrow": "0xAAA", "registry": "0xBBB", "usdc": "0xCCC"},
        {"escrow": "0xaaa", "registry": "0xbbb", "usdc": "0xccc"},
        "monad_testnet",
    )


def test_env_artifact_address_mismatch_fails(
    capsys: pytest.CaptureFixture,
) -> None:
    """env!=artifact on the same chainId must fail_all with both addresses."""
    from run import assert_env_artifact_addresses_agree

    with pytest.raises(SystemExit) as ei:
        assert_env_artifact_addresses_agree(
            {"escrow": "0xAAA", "registry": "0xBBB", "usdc": "0xCCC"},
            {"escrow": "0xAAA", "registry": "0xFFF", "usdc": "0xCCC"},
            "monad_testnet",
        )
    assert ei.value.code == 1  # fail() prints the reason, exits 1
    msg = capsys.readouterr().out
    assert "E2E FAILED" in msg
    assert "REGISTRY_ADDR=0xBBB" in msg
    assert "registry=0xFFF" in msg
    assert "deployed.json" in msg


def test_unset_env_pin_defers_to_artifact() -> None:
    """No env pin for a field → no comparison; the artifact value is kept."""
    from run import assert_env_artifact_addresses_agree

    assert_env_artifact_addresses_agree(
        {"escrow": "0xAAA", "registry": None, "usdc": ""},
        {"escrow": "0xaaa", "registry": "0xDDD", "usdc": "0xCCC"},
        "monad_testnet",
    )