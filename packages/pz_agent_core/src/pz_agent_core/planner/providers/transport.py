"""One HTTP request, over the standard library, bounded on every axis.

A provider that talks to a model needs an HTTP client. This package may not
acquire one: ``pyproject.toml`` declares no runtime dependency for the core
package and the shipped product has no ``pip``, so the client is
:mod:`http.client` and the seam is :class:`HttpTransport` — a Protocol with one
method, which is what lets every provider test run with no socket in sight.

Four bounds, and the reason each is not left to the caller:

* **A connect timeout and a read timeout, separately.** They answer different
  questions. Failing to reach a port is a mistyped address and should be over in
  seconds; a local 7B model thinking about a plan legitimately takes a minute.
  One number for both either aborts working requests or hangs on a dead one.
* **A byte ceiling on the response.** The peer decides how much it sends, and a
  model that loops can emit megabytes. The body is read with an explicit limit
  and one byte over it is :class:`ResponseTooLarge`, so a runaway generation
  costs a refusal instead of the process's memory.
* **Bounded retries, on a failure to connect and on nothing else.** A refused
  connection or an unresolvable host means the request was never delivered, so
  re-sending it is the same request. Everything else is not retried, and the
  distinction is deliberate: a 4xx rejects again for the identical reason, and
  a read timeout or a reset mid-exchange means the server may already be acting
  on what it received — a retry there duplicates work the caller cannot see.
* **A credential that is never written down.** :func:`key_from_env` reads a key
  from an environment variable *named* by configuration. The name lives in the
  config file; the key never does, which is what keeps it out of the config
  dump in a support bundle and out of a file a user pastes into a bug report.

What this module deliberately does not do: follow redirects (a 3xx is returned
as a status, because following one would re-send the Authorization header to
whatever host the redirect names), and weaken TLS (an ``https`` endpoint gets
:mod:`ssl`'s default verification, and a certificate that does not verify is a
refusal rather than a warning).
"""

from __future__ import annotations

import http.client
import os
import re
import ssl
import time
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from types import MappingProxyType
from typing import Final, Protocol
from urllib.parse import urlsplit

__all__ = [
    "DEFAULT_BACKOFF_S",
    "DEFAULT_CONNECT_TIMEOUT_S",
    "DEFAULT_MAX_ATTEMPTS",
    "DEFAULT_MAX_RESPONSE_BYTES",
    "DEFAULT_READ_TIMEOUT_S",
    "DEFAULT_TRANSPORT_CONFIG",
    "MAX_ATTEMPTS",
    "MAX_BACKOFF_S",
    "MAX_RESPONSE_BYTES",
    "MAX_TIMEOUT_S",
    "ConnectFailed",
    "CredentialUnavailable",
    "Endpoint",
    "ExchangeFailed",
    "HttpRequest",
    "HttpResponse",
    "HttpTransport",
    "InvalidEndpoint",
    "ReadTimedOut",
    "ResponseTooLarge",
    "StdlibHttpTransport",
    "TlsFailed",
    "TransportConfig",
    "TransportError",
    "ensure_env_name",
    "key_from_env",
    "parse_endpoint",
]

#: Long enough for a busy loopback, short enough that a mistyped port is a
#: five-second answer rather than a minute of silence.
DEFAULT_CONNECT_TIMEOUT_S: Final = 5.0

#: A local model generating a plan is the slow case this covers.
DEFAULT_READ_TIMEOUT_S: Final = 60.0

MAX_TIMEOUT_S: Final = 600.0

#: A plan document is a few kilobytes. A megabyte is room for a verbose model
#: and an error page; past that the peer is not answering the question asked.
DEFAULT_MAX_RESPONSE_BYTES: Final = 1_000_000
MAX_RESPONSE_BYTES: Final = 8_000_000

DEFAULT_MAX_ATTEMPTS: Final = 3
MAX_ATTEMPTS: Final = 5

DEFAULT_BACKOFF_S: Final = 0.25
MAX_BACKOFF_S: Final = 10.0

#: RFC 9110 token characters. A header name outside this set cannot be sent, so
#: a name assembled from configuration cannot terminate the field and start
#: another one.
_HEADER_NAME: Final = re.compile(r"[A-Za-z0-9!#$%&'*+.^_`|~-]+")

#: Printable ASCII with spaces and tabs, which is what a header value may hold.
#: The characters this excludes are CR and LF, and excluding them is the whole
#: point: they are how a value smuggles a second header in.
_HEADER_VALUE: Final = re.compile(r"[\x20-\x7e\t]*")

#: Visible ASCII, no whitespace: what a bearer credential looks like. A value
#: failing this is not treated as a key, because it would either be truncated by
#: the header encoder or split the request.
_CREDENTIAL: Final = re.compile(r"[\x21-\x7e]+")

#: Environment variable names this module will read. Uppercase by convention,
#: and the convention is load-bearing here: it is a shape no API key format this
#: project has seen fits, so a user who pastes the key itself into the
#: ``api_key_env`` setting is told they have named nothing.
_ENV_NAME: Final = re.compile(r"[A-Z][A-Z0-9_]{0,63}")

_METHODS: Final = frozenset({"GET", "POST"})


class TransportError(RuntimeError):
    """A request did not produce a response. Never raised for an HTTP status."""


class ConnectFailed(TransportError):
    """The connection was never established, after every attempt allowed."""

    def __init__(self, target: str, *, attempts: int, cause: str) -> None:
        super().__init__(
            f"could not connect to {target} in {attempts} attempt(s): {cause}. "
            "Check that the endpoint is running and that the address is right."
        )
        self.target = target
        self.attempts = attempts


class TlsFailed(TransportError):
    """The TLS handshake failed. Not retried: a bad certificate stays bad."""

    def __init__(self, target: str, *, cause: str) -> None:
        super().__init__(f"the TLS handshake with {target} failed: {cause}")
        self.target = target


class ReadTimedOut(TransportError):
    """The request was delivered and no complete response arrived in time.

    Not retried. The peer may be part-way through the work already, and this
    transport has no way to observe whether it is, so a second request would be
    a second job rather than a second chance.
    """

    def __init__(self, target: str, *, read_timeout_s: float) -> None:
        super().__init__(
            f"{target} accepted the request but sent no complete response within "
            f"{read_timeout_s:g}s"
        )
        self.target = target
        self.read_timeout_s = read_timeout_s


class ExchangeFailed(TransportError):
    """The connection broke after it was established. Not retried, as above."""

    def __init__(self, target: str, *, cause: str) -> None:
        super().__init__(f"the exchange with {target} failed after connecting: {cause}")
        self.target = target


class ResponseTooLarge(TransportError):
    """The peer sent, or announced, more than the ceiling allows."""

    def __init__(self, target: str, *, limit: int, announced: int | None = None) -> None:
        seen = f"announced {announced} bytes" if announced is not None else "sent more"
        super().__init__(f"{target} {seen} than the {limit} byte ceiling this client reads")
        self.target = target
        self.limit = limit
        self.announced = announced


class CredentialUnavailable(TransportError):
    """The named environment variable holds no usable credential."""

    def __init__(self, variable: str, *, detail: str = "") -> None:
        because = detail or "it is unset or empty"
        super().__init__(
            f"no API key in the environment variable {variable}: {because}. "
            f"Set {variable} in the environment you start pz-agent from — the key is "
            "deliberately not read from the configuration file."
        )
        self.variable = variable


class InvalidEndpoint(ValueError):
    """A URL this transport will not send to. Raised at configuration time."""


@dataclass(frozen=True, slots=True)
class Endpoint:
    """A URL, split into what :mod:`http.client` needs and nothing more."""

    tls: bool
    host: str
    port: int
    path: str

    def describe(self) -> str:
        """The endpoint as a string safe to put in an error a user will read.

        The port is shown only when it is not the scheme's default, so what
        appears in a message is what the user wrote in the config file.
        """
        scheme = "https" if self.tls else "http"
        default = 443 if self.tls else 80
        authority = self.host if self.port == default else f"{self.host}:{self.port}"
        return f"{scheme}://{authority}{self.path}"

    def join(self, path: str) -> str:
        """This endpoint's URL with *path* appended to it."""
        return f"{self.describe().rstrip('/')}/{path.lstrip('/')}"


def parse_endpoint(url: str) -> Endpoint:
    """Split *url*, refusing anything this transport must not send.

    Raises:
        InvalidEndpoint: for a scheme other than http or https, a missing host,
            a port that is not a number, or a URL carrying userinfo, a query or
            a fragment. Userinfo is refused for the same reason the key is read
            from the environment: a credential in a URL ends up in logs.
    """
    parts = urlsplit(url.strip())
    if parts.scheme not in ("http", "https"):
        raise InvalidEndpoint(f"{url!r} must be an http:// or https:// URL")
    if not parts.hostname:
        raise InvalidEndpoint(f"{url!r} names no host")
    if parts.username or parts.password:
        raise InvalidEndpoint(
            f"{url!r} carries credentials in the URL; put the key in the "
            "environment variable named by api_key_env instead"
        )
    if parts.query or parts.fragment:
        raise InvalidEndpoint(f"{url!r} must be a plain endpoint with no query or fragment")
    try:
        port = parts.port
    except ValueError as exc:
        raise InvalidEndpoint(f"{url!r} has no usable port: {exc}") from exc
    tls = parts.scheme == "https"
    return Endpoint(
        tls=tls,
        host=parts.hostname,
        port=port if port is not None else (443 if tls else 80),
        path=parts.path or "/",
    )


@dataclass(frozen=True, slots=True)
class HttpRequest:
    """One request, validated at construction so ``send`` cannot be handed one that
    would forge a header."""

    url: str
    method: str = "POST"
    body: bytes = b""
    headers: Mapping[str, str] = field(default_factory=dict)

    def __post_init__(self) -> None:
        if self.method not in _METHODS:
            raise ValueError(f"method must be one of {sorted(_METHODS)}, got {self.method!r}")
        parse_endpoint(self.url)
        for name, value in self.headers.items():
            if not _HEADER_NAME.fullmatch(name):
                raise ValueError(f"{name!r} is not a header name")
            if not _HEADER_VALUE.fullmatch(value):
                raise ValueError(f"the value of the {name} header holds a control character")
        object.__setattr__(self, "headers", MappingProxyType(dict(self.headers)))

    @property
    def endpoint(self) -> Endpoint:
        return parse_endpoint(self.url)


@dataclass(frozen=True, slots=True)
class HttpResponse:
    """A status and a body. A non-2xx is data here, not an exception.

    Whether a 404 is fatal, worth reporting or worth a different request is the
    caller's decision, and a transport that raised would be making it.
    """

    status: int
    body: bytes

    @property
    def ok(self) -> bool:
        return 200 <= self.status < 300

    @property
    def client_error(self) -> bool:
        """A 4xx: the request was understood and rejected. Never worth resending."""
        return 400 <= self.status < 500


class HttpTransport(Protocol):
    """Sends one request and returns what came back.

    Implementations raise :class:`TransportError` when there is no response at
    all, and return an :class:`HttpResponse` for every status the peer sends.
    """

    def send(self, request: HttpRequest) -> HttpResponse:
        """Deliver *request*, or raise :class:`TransportError` explaining why not."""
        ...


@dataclass(frozen=True, slots=True)
class TransportConfig:
    """The bounds one transport runs under."""

    connect_timeout_s: float = DEFAULT_CONNECT_TIMEOUT_S
    read_timeout_s: float = DEFAULT_READ_TIMEOUT_S
    max_response_bytes: int = DEFAULT_MAX_RESPONSE_BYTES
    max_attempts: int = DEFAULT_MAX_ATTEMPTS
    backoff_s: float = DEFAULT_BACKOFF_S

    def __post_init__(self) -> None:
        for name, value in (
            ("connect_timeout_s", self.connect_timeout_s),
            ("read_timeout_s", self.read_timeout_s),
        ):
            if not 0.0 < value <= MAX_TIMEOUT_S:
                raise ValueError(f"{name} must be within 0..{MAX_TIMEOUT_S}, got {value}")
        if not 1 <= self.max_response_bytes <= MAX_RESPONSE_BYTES:
            raise ValueError(
                f"max_response_bytes must be within 1..{MAX_RESPONSE_BYTES}, "
                f"got {self.max_response_bytes}"
            )
        if not 1 <= self.max_attempts <= MAX_ATTEMPTS:
            raise ValueError(
                f"max_attempts must be within 1..{MAX_ATTEMPTS}, got {self.max_attempts}"
            )
        if not 0.0 <= self.backoff_s <= MAX_BACKOFF_S:
            raise ValueError(f"backoff_s must be within 0..{MAX_BACKOFF_S}, got {self.backoff_s}")


DEFAULT_TRANSPORT_CONFIG: Final = TransportConfig()


class StdlibHttpTransport:
    """:class:`HttpTransport` over :mod:`http.client`.

    The connection is opened, used and closed per request. Keeping one alive
    would save a handshake against a server on loopback that is about to spend
    thirty seconds thinking, and would cost a pool of half-dead sockets to
    reason about.
    """

    def __init__(
        self,
        config: TransportConfig = DEFAULT_TRANSPORT_CONFIG,
        *,
        sleep: Callable[[float], None] = time.sleep,
    ) -> None:
        self._config = config
        self._sleep = sleep

    def send(self, request: HttpRequest) -> HttpResponse:
        """Send *request*, retrying only a connection that was never established."""
        endpoint = request.endpoint
        target = endpoint.describe()
        attempts = self._config.max_attempts
        cause = ""
        for attempt in range(1, attempts + 1):
            try:
                return self._send_once(request, endpoint)
            except ConnectFailed as exc:
                cause = str(exc.__cause__ or exc)
                if attempt == attempts:
                    break
                self._sleep(min(self._config.backoff_s * 2 ** (attempt - 1), MAX_BACKOFF_S))
        raise ConnectFailed(target, attempts=attempts, cause=cause)

    # -- one attempt -------------------------------------------------------

    def _send_once(self, request: HttpRequest, endpoint: Endpoint) -> HttpResponse:
        connection = self._connect(endpoint)
        try:
            return self._exchange(connection, request, endpoint)
        finally:
            connection.close()

    def _connect(self, endpoint: Endpoint) -> http.client.HTTPConnection:
        """Open the socket under the connect timeout, then arm the read timeout.

        Two timeouts out of one API: :class:`http.client.HTTPConnection` applies
        its ``timeout`` to whatever it does next, so it is created with the
        connect budget and the socket is re-armed with the read budget once the
        connection exists.
        """
        connection: http.client.HTTPConnection
        if endpoint.tls:
            connection = http.client.HTTPSConnection(
                endpoint.host, endpoint.port, timeout=self._config.connect_timeout_s
            )
        else:
            connection = http.client.HTTPConnection(
                endpoint.host, endpoint.port, timeout=self._config.connect_timeout_s
            )
        try:
            connection.connect()
        except ssl.SSLError as exc:
            connection.close()
            raise TlsFailed(endpoint.describe(), cause=str(exc)) from exc
        except OSError as exc:
            connection.close()
            raise ConnectFailed(endpoint.describe(), attempts=1, cause=str(exc)) from exc
        connection.sock.settimeout(self._config.read_timeout_s)
        return connection

    def _exchange(
        self,
        connection: http.client.HTTPConnection,
        request: HttpRequest,
        endpoint: Endpoint,
    ) -> HttpResponse:
        limit = self._config.max_response_bytes
        target = endpoint.describe()
        response: http.client.HTTPResponse | None = None
        try:
            connection.request(
                request.method, endpoint.path, body=request.body, headers=dict(request.headers)
            )
            response = connection.getresponse()
            announced = _announced_length(response)
            if announced is not None and announced > limit:
                raise ResponseTooLarge(target, limit=limit, announced=announced)
            # One byte past the ceiling is enough to know it was exceeded, and
            # stopping there is what keeps a runaway generation off the heap.
            body = response.read(limit + 1)
            if len(body) > limit:
                raise ResponseTooLarge(target, limit=limit)
            return HttpResponse(status=response.status, body=body)
        except TimeoutError as exc:
            raise ReadTimedOut(target, read_timeout_s=self._config.read_timeout_s) from exc
        except (OSError, http.client.HTTPException) as exc:
            raise ExchangeFailed(target, cause=str(exc)) from exc
        finally:
            # Refusing an over-large body means walking away from a response
            # mid-read, and a response abandoned that way keeps the socket's
            # file object alive after the connection below it is closed. The
            # refusal is the case this exists for, so it is the case that must
            # not leak a file descriptor per attempt.
            if response is not None:
                response.close()


def _announced_length(response: http.client.HTTPResponse) -> int | None:
    """``Content-Length`` when the peer sent a usable one.

    A missing or unparseable header is not an error: a chunked response has
    none, and the read below is bounded either way.
    """
    raw = response.getheader("Content-Length")
    if raw is None or not raw.strip().isdigit():
        return None
    return int(raw.strip())


def ensure_env_name(variable: str) -> str:
    """*variable*, once it is confirmed to be an environment variable name.

    Split out so a configuration file can be checked while the user is still
    looking at the terminal, without needing the key to be set yet.

    Raises:
        ValueError: when it is not a name — which is what pasting the key
            itself into ``api_key_env`` looks like from here.
    """
    if not _ENV_NAME.fullmatch(variable):
        raise ValueError(
            f"{variable!r} is not an environment variable name (expected upper case, "
            "letters, digits and underscores) — this setting names the variable holding "
            "the key, it is not the key"
        )
    return variable


def key_from_env(variable: str, *, environ: Mapping[str, str] | None = None) -> str:
    """The API key held in the environment variable *variable*.

    Raises:
        ValueError: when *variable* is not an environment variable name.
        CredentialUnavailable: when it is unset, empty, or holds something that
            cannot be sent as a header value. The message names the variable,
            because "401 Unauthorized" leaves the user guessing which of the two
            providers, and which of their shells, is the one missing it.
    """
    ensure_env_name(variable)
    source = os.environ if environ is None else environ
    key = source.get(variable, "").strip()
    if not key:
        raise CredentialUnavailable(variable)
    if not _CREDENTIAL.fullmatch(key):
        raise CredentialUnavailable(
            variable, detail="its value holds a space or a control character"
        )
    return key
