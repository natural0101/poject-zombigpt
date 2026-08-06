"""The HTTP transport: its bounds, and the double the provider tests run on.

Two kinds of test live here. The first drive the real
:class:`StdlibHttpTransport` against a throwaway server bound to ``127.0.0.1:0``
in this process — no name resolution, no route off the machine, nothing to
configure — because the byte ceiling, the read timeout and "a 4xx is data, not
an exception" are only true if a real socket says so.

The second is :class:`FakeTransport`, which the two provider modules import.
It lives here rather than beside them because it stands in for the Protocol this
module is about, and :func:`test_the_fake_satisfies_the_transport_protocol`
holds it to that Protocol: a double that drifted would let the provider tests
pass against a client the shipped code could never use.
"""

from __future__ import annotations

import json
import socket
import threading
import time
from collections.abc import Iterator
from dataclasses import dataclass
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any, cast

import pytest

from pz_agent_core.planner.providers.transport import (
    MAX_ATTEMPTS,
    MAX_RESPONSE_BYTES,
    ConnectFailed,
    CredentialUnavailable,
    HttpRequest,
    HttpResponse,
    HttpTransport,
    InvalidEndpoint,
    ReadTimedOut,
    ResponseTooLarge,
    StdlibHttpTransport,
    TransportConfig,
    TransportError,
    ensure_env_name,
    key_from_env,
    parse_endpoint,
)

# ---------------------------------------------------------------------------
# the double the provider tests use
# ---------------------------------------------------------------------------


class FakeTransport:
    """Answers each request from a script, and remembers what it was asked.

    The last entry repeats, so a test about one exchange does not have to think
    about how many times a provider might call. A :class:`TransportError` in the
    script is raised rather than returned, which is how a caller sees "there was
    no response at all".
    """

    def __init__(self, *replies: HttpResponse | TransportError) -> None:
        if not replies:
            raise ValueError("a fake transport needs at least one reply")
        self._replies = list(replies)
        self.requests: list[HttpRequest] = []

    def send(self, request: HttpRequest) -> HttpResponse:
        self.requests.append(request)
        reply = self._replies[min(len(self.requests) - 1, len(self._replies) - 1)]
        if isinstance(reply, TransportError):
            raise reply
        return reply

    @property
    def calls(self) -> int:
        return len(self.requests)

    def sent_body(self, index: int = 0) -> dict[str, Any]:
        document: dict[str, Any] = json.loads(self.requests[index].body)
        return document


def json_response(payload: object, status: int = 200) -> HttpResponse:
    return HttpResponse(status=status, body=json.dumps(payload).encode("utf-8"))


def test_the_fake_satisfies_the_transport_protocol() -> None:
    """Static, not incidental: mypy checks this assignment on every run."""
    transport: HttpTransport = FakeTransport(json_response({}))

    assert transport.send(HttpRequest(url="http://127.0.0.1:1/v1/x")).ok


# ---------------------------------------------------------------------------
# a throwaway server on loopback
# ---------------------------------------------------------------------------


@dataclass
class Reply:
    """What the loopback server answers with."""

    status: int = 200
    body: bytes = b"{}"
    #: What to put in ``Content-Length``. None omits the header entirely, which
    #: is how a chunked or close-delimited body reaches the ceiling check.
    announce: int | None = -1
    delay_s: float = 0.0


@dataclass
class Recorded:
    method: str
    path: str
    body: bytes


class _Server(ThreadingHTTPServer):
    def __init__(self, reply: Reply) -> None:
        super().__init__(("127.0.0.1", 0), _Handler)
        self.reply = reply
        self.requests: list[Recorded] = []

    def handle_error(self, request: object, client_address: object) -> None:
        """A client that gave up mid-reply is the point of two of these tests."""
        return


class _Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.0"

    def do_GET(self) -> None:
        self._respond()

    def do_POST(self) -> None:
        self._respond()

    def _respond(self) -> None:
        server = cast(_Server, self.server)
        length = int(self.headers.get("Content-Length") or 0)
        server.requests.append(Recorded(self.command, self.path, self.rfile.read(length)))
        reply = server.reply
        if reply.delay_s:
            time.sleep(reply.delay_s)
        try:
            self.send_response(reply.status)
            announced = len(reply.body) if reply.announce == -1 else reply.announce
            if announced is not None:
                self.send_header("Content-Length", str(announced))
            self.end_headers()
            self.wfile.write(reply.body)
        except OSError:
            # The timeout test is a client that walked away mid-reply. Writing
            # into the closed socket is the expected end of this handler.
            self.close_connection = True

    def log_message(self, fmt: str, *args: Any) -> None:
        return


@pytest.fixture
def server() -> Iterator[_Server]:
    running = _Server(Reply())
    thread = threading.Thread(target=running.serve_forever, daemon=True)
    thread.start()
    try:
        yield running
    finally:
        running.shutdown()
        running.server_close()
        thread.join(timeout=5)


def url_of(running: _Server, path: str = "/v1/plan") -> str:
    port = int(running.server_address[1])
    return f"http://127.0.0.1:{port}{path}"


class Recorder:
    """Stands in for :func:`time.sleep` so backoff is asserted, not waited out."""

    def __init__(self) -> None:
        self.delays: list[float] = []

    def __call__(self, seconds: float) -> None:
        self.delays.append(seconds)


# ---------------------------------------------------------------------------
# the real client
# ---------------------------------------------------------------------------


def test_a_post_carries_its_body_and_returns_the_reply(server: _Server) -> None:
    server.reply = Reply(status=200, body=b'{"ok": true}')
    transport = StdlibHttpTransport()

    response = transport.send(
        HttpRequest(
            url=url_of(server),
            body=b'{"goal": 1}',
            headers={"content-type": "application/json"},
        )
    )

    assert (response.status, response.body) == (200, b'{"ok": true}')
    assert response.ok
    assert server.requests[0] == Recorded("POST", "/v1/plan", b'{"goal": 1}')


def test_a_client_error_is_returned_as_data_and_never_retried(server: _Server) -> None:
    """A rejected request rejects again; retrying it only burns the budget."""
    server.reply = Reply(status=404, body=b'{"error": "no such model"}')
    sleeps = Recorder()
    transport = StdlibHttpTransport(TransportConfig(max_attempts=3), sleep=sleeps)

    response = transport.send(HttpRequest(url=url_of(server)))

    assert response.status == 404
    assert response.client_error
    assert len(server.requests) == 1
    assert sleeps.delays == []


def test_a_server_error_is_also_returned_rather_than_retried(server: _Server) -> None:
    server.reply = Reply(status=503, body=b"busy")
    transport = StdlibHttpTransport(TransportConfig(max_attempts=3))

    assert transport.send(HttpRequest(url=url_of(server))).status == 503
    assert len(server.requests) == 1


def test_a_body_over_the_ceiling_is_refused(server: _Server) -> None:
    """No Content-Length, so the ceiling has to be enforced by the read itself."""
    server.reply = Reply(body=b"x" * 5_000, announce=None)
    transport = StdlibHttpTransport(TransportConfig(max_response_bytes=1_000))

    with pytest.raises(ResponseTooLarge) as caught:
        transport.send(HttpRequest(url=url_of(server)))

    assert caught.value.limit == 1_000
    assert caught.value.announced is None


def test_an_announced_length_over_the_ceiling_is_refused_before_reading(
    server: _Server,
) -> None:
    server.reply = Reply(body=b"x" * 40, announce=9_000_000)
    transport = StdlibHttpTransport(TransportConfig(max_response_bytes=1_000))

    with pytest.raises(ResponseTooLarge) as caught:
        transport.send(HttpRequest(url=url_of(server)))

    assert caught.value.announced == 9_000_000


def test_a_body_exactly_at_the_ceiling_is_kept(server: _Server) -> None:
    server.reply = Reply(body=b"x" * 1_000)
    transport = StdlibHttpTransport(TransportConfig(max_response_bytes=1_000))

    assert len(transport.send(HttpRequest(url=url_of(server))).body) == 1_000


def test_a_reply_that_never_comes_hits_the_read_timeout_and_is_not_retried() -> None:
    """A listening socket nobody accepts on: the connect succeeds, the read hangs.

    The two timeouts have to be separable for this to pass at all — one number
    for both would have aborted at the connect stage, where nothing is wrong.
    No retry, because the request was delivered and the peer may be acting on
    it; the empty backoff record is what proves none was attempted.
    """
    with socket.socket() as listener:
        listener.bind(("127.0.0.1", 0))
        listener.listen(1)
        sleeps = Recorder()
        transport = StdlibHttpTransport(
            TransportConfig(connect_timeout_s=2.0, read_timeout_s=0.1, max_attempts=3),
            sleep=sleeps,
        )

        with pytest.raises(ReadTimedOut) as caught:
            transport.send(HttpRequest(url=f"http://127.0.0.1:{listener.getsockname()[1]}/v1"))

    assert caught.value.read_timeout_s == 0.1
    assert sleeps.delays == []


def test_a_refused_connection_is_retried_up_to_the_bound_with_backoff() -> None:
    """Port 1 on loopback refuses immediately: a connection error, no network."""
    sleeps = Recorder()
    transport = StdlibHttpTransport(
        TransportConfig(connect_timeout_s=1.0, max_attempts=3, backoff_s=0.5), sleep=sleeps
    )

    with pytest.raises(ConnectFailed) as caught:
        transport.send(HttpRequest(url="http://127.0.0.1:1/v1/plan"))

    assert caught.value.attempts == 3
    # One sleep fewer than attempts: nothing is waited for after the last one.
    assert sleeps.delays == [0.5, 1.0]


def test_a_single_attempt_never_sleeps() -> None:
    sleeps = Recorder()
    transport = StdlibHttpTransport(TransportConfig(max_attempts=1), sleep=sleeps)

    with pytest.raises(ConnectFailed):
        transport.send(HttpRequest(url="http://127.0.0.1:1/v1/plan"))

    assert sleeps.delays == []


def test_the_failure_names_the_endpoint_without_the_key() -> None:
    transport = StdlibHttpTransport(TransportConfig(max_attempts=1))

    with pytest.raises(ConnectFailed) as caught:
        transport.send(
            HttpRequest(
                url="http://127.0.0.1:1/v1/plan", headers={"authorization": "Bearer sekrit"}
            )
        )

    assert "127.0.0.1:1" in str(caught.value)
    assert "sekrit" not in str(caught.value)


def test_every_transport_failure_shares_one_base_class() -> None:
    """Providers catch :class:`TransportError` and must not miss a subclass."""
    for failure in (ConnectFailed, ReadTimedOut, ResponseTooLarge, CredentialUnavailable):
        assert issubclass(failure, TransportError)


# ---------------------------------------------------------------------------
# what a request may be
# ---------------------------------------------------------------------------


def test_a_header_value_holding_a_newline_is_refused() -> None:
    with pytest.raises(ValueError, match="control character"):
        HttpRequest(url="http://127.0.0.1:8080/v1", headers={"x-note": "a\r\nx-admin: 1"})


def test_a_header_name_that_is_not_a_token_is_refused() -> None:
    with pytest.raises(ValueError, match="header name"):
        HttpRequest(url="http://127.0.0.1:8080/v1", headers={"x note": "1"})


def test_a_method_this_transport_does_not_send_is_refused() -> None:
    with pytest.raises(ValueError, match="method"):
        HttpRequest(url="http://127.0.0.1:8080/v1", method="DELETE")


@pytest.mark.parametrize(
    "url",
    [
        "ftp://example.invalid/v1",
        "file:///etc/passwd",
        "/v1/chat/completions",
        "http:///v1",
        "http://user:pass@127.0.0.1:8080/v1",
        "http://127.0.0.1:8080/v1?key=abc",
        "http://127.0.0.1:99999999/v1",
    ],
)
def test_a_url_this_transport_will_not_send_to_is_refused(url: str) -> None:
    with pytest.raises(InvalidEndpoint):
        parse_endpoint(url)


def test_an_endpoint_keeps_its_path_and_defaults_its_port() -> None:
    assert parse_endpoint("https://api.example.invalid/v1").port == 443
    assert parse_endpoint("http://api.example.invalid/v1").port == 80
    assert parse_endpoint("http://127.0.0.1:8080/base").path == "/base"


def test_a_described_endpoint_reads_back_as_the_user_wrote_it() -> None:
    """A default port is used and not printed; a chosen one is printed."""
    assert parse_endpoint("https://api.example.invalid/v1").describe() == (
        "https://api.example.invalid/v1"
    )
    assert parse_endpoint("http://127.0.0.1:8080/v1").describe() == "http://127.0.0.1:8080/v1"


def test_joining_a_path_survives_a_trailing_slash() -> None:
    for base in ("http://127.0.0.1:8080", "http://127.0.0.1:8080/"):
        assert (
            parse_endpoint(base).join("/v1/chat/completions")
            == "http://127.0.0.1:8080/v1/chat/completions"
        )


# ---------------------------------------------------------------------------
# bounds and credentials
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "overrides",
    [
        {"connect_timeout_s": 0.0},
        {"read_timeout_s": 6_000.0},
        {"max_response_bytes": MAX_RESPONSE_BYTES + 1},
        {"max_attempts": MAX_ATTEMPTS + 1},
        {"max_attempts": 0},
        {"backoff_s": -1.0},
    ],
)
def test_an_unbounded_transport_cannot_be_configured(overrides: dict[str, float]) -> None:
    with pytest.raises(ValueError, match="must be within"):
        TransportConfig(**overrides)  # type: ignore[arg-type]


def test_a_key_is_read_from_the_named_variable() -> None:
    assert key_from_env("PZ_TEST_KEY", environ={"PZ_TEST_KEY": " abc123 "}) == "abc123"


def test_a_missing_key_names_the_variable_rather_than_failing_at_the_server() -> None:
    with pytest.raises(CredentialUnavailable) as caught:
        key_from_env("PZ_TEST_KEY", environ={})

    assert caught.value.variable == "PZ_TEST_KEY"
    assert "PZ_TEST_KEY" in str(caught.value)


def test_an_empty_key_is_treated_as_missing() -> None:
    with pytest.raises(CredentialUnavailable):
        key_from_env("PZ_TEST_KEY", environ={"PZ_TEST_KEY": "   "})


def test_a_key_that_could_forge_a_header_is_refused() -> None:
    with pytest.raises(CredentialUnavailable, match="control character"):
        key_from_env("PZ_TEST_KEY", environ={"PZ_TEST_KEY": "abc\r\nx-admin: 1"})


def test_a_pasted_key_is_not_an_environment_variable_name() -> None:
    """The failure this catches is a secret typed into the config file."""
    with pytest.raises(ValueError, match="it is not the key"):
        ensure_env_name("sk-abcdef0123456789")


def test_a_variable_name_is_returned_unchanged() -> None:
    assert ensure_env_name("PZ_AGENT_OPENAI_API_KEY") == "PZ_AGENT_OPENAI_API_KEY"
