"""Checks that a URL a website user typed (a webhook for their alerts) is safe for the server to POST to.

A webhook URL makes the server send requests where the user says, so without these rules a user could point it at
the server itself or at the private network behind it (server-side request forgery): the Fly.io machine's own ports,
other apps on the organisation's private network (fdaa::/16), a cloud metadata service (169.254.169.254). The rules:

- https only, and no user name or password in the URL;
- the host must resolve, and every address it resolves to must be public: loopback, private (10/8, 172.16/12,
  192.168/16), link-local (169.254/16, fe80::/10), carrier-grade NAT (100.64/10), unique local IPv6 (fc00::/7,
  which includes Fly's fdaa::/16), multicast, reserved and documentation ranges are refused, and so are IPv6
  addresses that wrap an IPv4 one (6to4, NAT64; an IPv4-mapped address is judged by the IPv4 address inside);
- host names that only exist inside a private network (localhost, *.internal, *.flycast, *.local) are refused before
  they are looked up.

The website checks a URL when the user saves it and again before every send, and the webhook notifier doesn't follow
redirects. Checking the name isn't enough by itself: a name can answer a public address to the check and a private
one to the connection a moment later (DNS rebinding). So members' webhooks are sent through public_https_session(),
whose connections look the host up once, refuse unless every address is public, and connect to the address that was
checked; TLS still checks the certificate against the host name. The resolver is injectable, so tests never use the
network.
"""

from __future__ import annotations

import ipaddress
import socket
from collections.abc import Callable, Iterable
from typing import Any
from urllib.parse import urlsplit

import requests
from requests.adapters import HTTPAdapter
from urllib3.connection import HTTPConnection, HTTPSConnection
from urllib3.connectionpool import HTTPConnectionPool, HTTPSConnectionPool
from urllib3.exceptions import ConnectTimeoutError, NameResolutionError, NewConnectionError

MAX_URL_LENGTH = 2048
PRIVATE_TARGET = "The address must be on the public internet: its host points to a private, local or reserved network."
# (host, port) -> the IP addresses it resolves to, as text.
Resolver = Callable[[str, int], Iterable[str]]

_PRIVATE_NAMES = ("localhost", ".localhost", ".internal", ".flycast", ".local", ".localdomain", ".home.arpa")
_BLOCKED_NETWORKS = tuple(
    ipaddress.ip_network(network)
    for network in (
        "0.0.0.0/8",
        "10.0.0.0/8",
        "100.64.0.0/10",  # carrier-grade NAT (shared address space)
        "127.0.0.0/8",
        "169.254.0.0/16",  # link-local, cloud metadata services
        "172.16.0.0/12",
        "192.0.0.0/24",
        "192.0.2.0/24",
        "192.88.99.0/24",
        "192.168.0.0/16",
        "198.18.0.0/15",
        "198.51.100.0/24",
        "203.0.113.0/24",
        "224.0.0.0/4",
        "240.0.0.0/4",
        "::/128",
        "::1/128",
        "64:ff9b::/96",  # NAT64: wraps an IPv4 address
        "64:ff9b:1::/48",
        "100::/64",
        "2001::/23",  # IETF protocol assignments, Teredo included
        "2001:db8::/32",
        "2002::/16",  # 6to4: wraps an IPv4 address
        "fc00::/7",  # unique local, Fly.io's private network fdaa::/16 included
        "fdaa::/16",
        "fe80::/10",
        "fec0::/10",
        "ff00::/8",
    )
)


class UnsafeURLError(ValueError):
    """The URL isn't allowed; the message says why, in words for the person who typed it."""


def system_resolver(host: str, port: int) -> list[str]:
    """The addresses the operating system resolves host to (IPv4 and IPv6)."""
    infos = socket.getaddrinfo(host, port, proto=socket.IPPROTO_TCP)
    return [str(info[4][0]) for info in infos]


def is_public_address(address: str) -> bool:
    """Whether an IP address (text; an IPv6 zone like %eth0 is ignored) is a public internet address."""
    try:
        ip = ipaddress.ip_address(address.split("%", 1)[0])
    except ValueError:
        return False
    if isinstance(ip, ipaddress.IPv6Address) and ip.ipv4_mapped is not None:
        ip = ip.ipv4_mapped
    if any(ip in network for network in _BLOCKED_NETWORKS if network.version == ip.version):
        return False
    return ip.is_global and not (ip.is_multicast or ip.is_reserved or ip.is_link_local or ip.is_loopback)


def check_public_url(url: object, *, resolver: Resolver | None = None) -> str:
    """The URL, stripped, when the server may POST to it (see the module docstring); UnsafeURLError otherwise.

    resolver defaults to the system's DNS (system_resolver); a name that doesn't resolve is refused too.
    """
    if not isinstance(url, str) or not url.strip():
        raise UnsafeURLError("Enter the webhook's address, starting with https://.")
    url = url.strip()
    if len(url) > MAX_URL_LENGTH:
        raise UnsafeURLError(f"That address is too long (over {MAX_URL_LENGTH} characters).")
    if any(char.isspace() or ord(char) < 32 or ord(char) == 127 or char == "\\" for char in url):
        raise UnsafeURLError("The address can't contain spaces, backslashes or control characters.")
    try:
        parts = urlsplit(url)
        port = parts.port
    except ValueError:
        raise UnsafeURLError("That isn't a valid web address.") from None
    if parts.scheme.lower() != "https":
        raise UnsafeURLError("The address must start with https:// (the alerts are sent encrypted).")
    if "@" in parts.netloc or parts.username is not None or parts.password is not None:
        raise UnsafeURLError("The address can't contain a user name or password before the host.")
    host = (parts.hostname or "").rstrip(".")
    if not host:
        raise UnsafeURLError("The address has no host name.")
    try:
        addresses = [str(ipaddress.ip_address(host))]  # an address written as such is judged as it is
    except ValueError:
        try:
            ascii_host = host.encode("idna").decode("ascii").lower()
        except UnicodeError:
            raise UnsafeURLError("The host name isn't valid.") from None
        if ascii_host == "localhost" or ascii_host.endswith(_PRIVATE_NAMES):
            raise UnsafeURLError(
                "The address must be on the public internet, not a local or private network."
            ) from None
        try:
            addresses = list(dict.fromkeys((resolver or system_resolver)(ascii_host, port or 443)))
        except (OSError, UnicodeError, ValueError):
            addresses = []
    if not addresses:
        raise UnsafeURLError(f"The host {host} can't be found; check the address.")
    if not all(is_public_address(address) for address in addresses):
        raise UnsafeURLError(PRIVATE_TARGET)
    return url


# --- connections pinned to the checked address -----------------------------------------------------------------------


class UnsafeAddressError(NewConnectionError):
    """A connection refused because its host doesn't resolve to public addresses only (raised by the connections of
    public_https_session; requests wraps it in a ConnectionError)."""


class _PublicOnly:
    """Mixin for urllib3 connections: look the host up once with _resolve, refuse unless every address is public, and
    connect to the checked address itself (never a second lookup of the name). TLS (HTTPSConnection.connect) still
    sends the host name as SNI and checks the certificate against it, since it uses self.host, not the address."""

    _resolve: Resolver = staticmethod(lambda host, port: system_resolver(host, port))
    # Set by urllib3's HTTPConnection:
    _dns_host: str
    host: str
    port: int
    timeout: Any
    socket_options: Any

    def _new_conn(self) -> socket.socket:
        host = self._dns_host.strip("[]")
        try:
            addresses = list(dict.fromkeys(self._resolve(host, self.port)))
        except (OSError, UnicodeError, ValueError) as exc:
            raise NameResolutionError(self.host, self, exc) from exc  # type: ignore[arg-type]
        if not addresses or not all(is_public_address(address) for address in addresses):
            raise UnsafeAddressError(self, PRIVATE_TARGET)  # type: ignore[arg-type]
        timeout = self.timeout if isinstance(self.timeout, int | float) else None
        failure: Exception | None = None
        for address in addresses:
            family = socket.AF_INET6 if ":" in address else socket.AF_INET
            sock = socket.socket(family, socket.SOCK_STREAM)
            try:
                for option in self.socket_options or ():
                    sock.setsockopt(*option)
                sock.settimeout(timeout)
                sock.connect((address, self.port))
            except TimeoutError as exc:
                sock.close()
                failure = ConnectTimeoutError(self, f"Connection to {self.host} timed out.")
                failure.__cause__ = exc
            except OSError as exc:
                sock.close()
                failure = NewConnectionError(self, f"Failed to establish a new connection: {exc}")  # type: ignore[arg-type]
                failure.__cause__ = exc
            else:
                return sock
        assert failure is not None
        raise failure


def public_https_session(resolver: Resolver | None = None) -> requests.Session:
    """A requests session for URLs website users typed (their webhooks): every connection resolves the host once with
    resolver (default: the system's DNS), is refused (UnsafeAddressError inside a requests.ConnectionError) unless
    every address is public, and connects to the address it checked, so a name can't be rebound to a private address
    between the check and the connection. Proxies from the environment are ignored (a proxy would look the name up
    itself)."""
    lookup: Resolver = resolver or system_resolver

    class Connection(_PublicOnly, HTTPConnection):
        _resolve = staticmethod(lookup)

    class SecureConnection(_PublicOnly, HTTPSConnection):
        _resolve = staticmethod(lookup)

    class Pool(HTTPConnectionPool):
        ConnectionCls = Connection

    class SecurePool(HTTPSConnectionPool):
        ConnectionCls = SecureConnection

    class Adapter(HTTPAdapter):
        def init_poolmanager(self, *args: Any, **kwargs: Any) -> None:
            super().init_poolmanager(*args, **kwargs)
            self.poolmanager.pool_classes_by_scheme = {"http": Pool, "https": SecurePool}

    session = requests.Session()
    session.trust_env = False
    adapter = Adapter()
    session.mount("https://", adapter)
    session.mount("http://", adapter)
    return session


def refused_by_guard(exc: BaseException) -> bool:
    """Whether an exception (from requests) comes from public_https_session refusing a private address."""
    seen: set[int] = set()
    pending: list[object] = [exc]
    while pending:
        item = pending.pop()
        if not isinstance(item, BaseException) or id(item) in seen:
            continue
        seen.add(id(item))
        if isinstance(item, UnsafeAddressError):
            return True
        pending.extend([item.__cause__, item.__context__, getattr(item, "reason", None), *item.args])
    return False
