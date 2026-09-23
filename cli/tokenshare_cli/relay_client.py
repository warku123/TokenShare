"""HTTP client for the seller relay.

Endpoint source (PIN): default is Registry getListing(operator).endpoint;
the `--relay` option overrides it. The CLI always talks to
`<endpoint>/v1/chat/completions` with the EIP-191 payment headers.

The response object is a plain dataclass so tests can feed fakes without
spinning up HTTP servers.
"""

import json as _json
from dataclasses import dataclass, field
from typing import Iterator

import httpx

from .errors import ConfigError, RelayError
from .signing import RELAY_CHAT_PATH


@dataclass
class RelayResponse:
    status_code: int
    headers: dict = field(default_factory=dict)
    body_text: str = ""

    def json(self):
        return _json.loads(self.body_text)


@dataclass
class RelayStreamHandle:
    status_code: int
    headers: dict
    lines: Iterator[str]


def _headers(payment_id: int, signature: str, stream: bool) -> dict:
    return {
        "Content-Type": "application/json",
        "Accept": "text/event-stream" if stream else "application/json",
        "X-Payment-Id": str(int(payment_id)),  # decimal string per PIN
        "X-Signature": signature,  # 0x + 65-byte hex per PIN
    }


def _url(base_url: str) -> str:
    if not base_url:
        raise ConfigError("relay endpoint is empty; check the Registry listing or pass --relay")
    return base_url.rstrip("/") + RELAY_CHAT_PATH


def post_chat_json(
    base_url: str,
    payment_id: int,
    signature: str,
    body_bytes: bytes,
    timeout: float = 120.0,
) -> RelayResponse:
    """POST /v1/chat/completions (non-stream); raises RelayError on non-2xx."""
    with httpx.Client(timeout=timeout) as client:
        resp = client.post(
            _url(base_url), content=body_bytes, headers=_headers(payment_id, signature, stream=False)
        )
    response = RelayResponse(
        status_code=resp.status_code,
        headers={k: v for k, v in resp.headers.items()},
        body_text=resp.text,
    )
    if response.status_code >= 400:
        raise RelayError(response.status_code, response.body_text)
    return response


def open_chat_stream(
    base_url: str,
    payment_id: int,
    signature: str,
    body_bytes: bytes,
    timeout: float = 120.0,
) -> RelayStreamHandle:
    """POST /v1/chat/completions with stream=true; returns a handle whose
    `iter_lines` yields raw SSE lines. Headers (X-Settle-Status / X-Receipt)
    are captured up front; relay may also expose the receipt via
    GET /receipt/{paymentId} when header timing is impossible for streams."""
    client = httpx.Client(timeout=timeout)
    request = client.build_request(
        "POST",
        _url(base_url),
        content=body_bytes,
        headers=_headers(payment_id, signature, stream=True),
    )
    response = client.send(request, stream=True)
    if response.status_code >= 400:
        try:
            body = response.read().decode("utf-8", "replace")
        finally:
            response.close()
            client.close()
        raise RelayError(response.status_code, body)
    return RelayStreamHandle(
        status_code=response.status_code,
        headers={k.lower(): v for k, v in response.headers.items()},
        lines=iter(response.iter_lines()),
    )
