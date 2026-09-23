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