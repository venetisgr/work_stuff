"""netguard: which webhook addresses the server may POST to (no network: the resolver is a fake)."""

from __future__ import annotations

import socket
import threading

import pytest
import requests

from dip_scanner import netguard
from dip_scanner.netguard import (
    UnsafeURLError,
    check_public_url,
    is_public_address,
    public_https_session,
    refused_by_guard,
    system_resolver,
)
from dip_scanner.notify import NotifyError, WebhookNotifier


def resolver(*addresses: str, calls: list | None = None):
    """A fake DNS that answers every name with these addresses (and records what it was asked)."""

    def resolve(host: str, port: int) -> list[str]:
        if calls is not None:
            calls.append((host, port))
        return list(addresses)

    return resolve


@pytest.mark.parametrize(
    "address",
    [
        "8.8.8.8",
        "1.1.1.1",
        "34.120.1.2",
        "2606:4700:4700::1111",  # Cloudflare's public IPv6
        "::ffff:8.8.8.8",  # an IPv4-mapped public address is judged by the IPv4 address inside
    ],
)
def test_public_addresses_are_allowed(address):
    assert is_public_address(address)


@pytest.mark.parametrize(
    "address",
    [
        "127.0.0.1",
        "127.8.9.10",
        "10.0.0.5",
        "172.16.3.4",
        "192.168.1.1",
        "169.254.169.254",  # cloud metadata services
        "100.64.0.1",  # carrier-grade NAT
        "0.0.0.0",
        "255.255.255.255",
        "224.0.0.1",
        "192.0.2.10",
        "198.18.0.1",
        "::1",
        "::",
        "fe80::1",
        "fe80::1%eth0",
        "fc00::1",
        "fd12:3456::1",
        "fdaa::3",  # Fly.io's private network
        "fdaa:0:1234:a7b:5c:1::2",
        "::ffff:10.0.0.1",  # IPv4-mapped private
        "::ffff:169.254.169.254",
        "2002:a00:1::1",  # 6to4 wrapping 10.0.0.1
        "64:ff9b::a00:1",  # NAT64 wrapping 10.0.0.1
        "2001:db8::1",
        "ff02::1",
        "not an address",
    ],
)
def test_private_local_and_reserved_addresses_are_refused(address):
    assert not is_public_address(address)


def test_a_public_https_url_is_returned_stripped_and_its_host_looked_up_with_the_port():
    calls: list = []
    url = "  https://hooks.slack.com/services/T000/B000/XXXX  "
    assert check_public_url(url, resolver=resolver("34.120.1.2", calls=calls)) == url.strip()
    assert calls == [("hooks.slack.com", 443)]
    check_public_url("https://n8n.example.com:8443/webhook/x", resolver=resolver("93.184.215.14", calls=calls))
    assert calls[-1] == ("n8n.example.com", 8443)


@pytest.mark.parametrize(
    ("url", "message"),
    [
        ("", "Enter the webhook's address"),
        (None, "Enter the webhook's address"),
        ("http://hooks.slack.com/services/x", "must start with https://"),
        ("ftp://example.com/x", "must start with https://"),
        ("javascript:alert(1)", "must start with https://"),
        ("https://user:secret@hooks.slack.com/x", "user name or password"),
        ("https://user@hooks.slack.com/x", "user name or password"),
        ("https://hooks.slack.com\\@evil.example/x", "backslashes"),
        ("https://hooks.slack.com/x y", "spaces"),
        ("https://hooks.slack.com/\x00", "control characters"),
        ("https:///no-host", "no host name"),
        ("https://hooks.slack.com:99999/x", "valid web address"),
        ("https://localhost/x", "public internet"),
        ("https://LocalHost./x", "public internet"),
        ("https://my-app.internal/x", "public internet"),
        ("https://my-app.flycast/x", "public internet"),
        ("https://printer.local/x", "public internet"),
        ("https://127.0.0.1/x", "private, local or reserved"),
        ("https://[::1]/x", "private, local or reserved"),
        ("https://[fdaa::2]:8080/x", "private, local or reserved"),
        ("https://169.254.169.254/latest/meta-data", "private, local or reserved"),
        ("https://" + "a" * 2050 + ".com/", "too long"),
    ],
)
def test_unsafe_urls_are_refused_with_a_reason(url, message):
    with pytest.raises(UnsafeURLError, match=message):
        check_public_url(url, resolver=resolver("8.8.8.8"))


def test_a_name_that_resolves_to_a_private_address_is_refused():
    """DNS rebinding: the name looks public but points inside; every address it resolves to must be public."""
    with pytest.raises(UnsafeURLError, match="private, local or reserved"):
        check_public_url("https://rebind.example.com/hook", resolver=resolver("10.0.0.7"))
    with pytest.raises(UnsafeURLError, match="private, local or reserved"):
        check_public_url("https://mixed.example.com/hook", resolver=resolver("8.8.8.8", "fdaa::5"))


def test_a_name_that_does_not_resolve_is_refused():
    def failing(host, port):
        raise socket.gaierror("Name or service not known")

    with pytest.raises(UnsafeURLError, match="can't be found"):
        check_public_url("https://nosuch.example.com/x", resolver=failing)
    with pytest.raises(UnsafeURLError, match="can't be found"):
        check_public_url("https://empty.example.com/x", resolver=resolver())


def test_an_ip_literal_is_judged_without_a_lookup():
    calls: list = []
    assert check_public_url("https://8.8.8.8/x", resolver=resolver("10.0.0.1", calls=calls)) == "https://8.8.8.8/x"
    assert calls == []


def test_international_host_names_are_looked_up_in_their_ascii_form():
    calls: list = []
    check_public_url("https://bücher.example/hook", resolver=resolver("8.8.8.8", calls=calls))
    assert calls == [("xn--bcher-kva.example", 443)]


def test_the_system_resolver_answers_an_ip_literal_without_the_network():
    assert system_resolver("127.0.0.1", 443) == ["127.0.0.1"]


# --- connections pinned to the checked address ------------------------------------------------------------------


def listener():
    """A loopback server that counts the connections it accepts and keeps what the first one sent."""
    server = socket.socket()
    server.bind(("127.0.0.1", 0))
    server.listen(5)
    accepted: list[bytes] = []

    def run():
        while True:
            try:
                conn, _ = server.accept()
            except OSError:
                return
            with conn:
                conn.settimeout(2)
                try:
                    accepted.append(conn.recv(4096))
                except OSError:
                    accepted.append(b"")

    threading.Thread(target=run, daemon=True).start()
    return server, server.getsockname()[1], accepted


def test_a_name_rebound_to_loopback_after_the_check_is_never_connected_to():
    """DNS rebinding: public for the check before the send, 127.0.0.1 for the connection a moment later."""
    answers = iter([["93.184.216.34"], ["127.0.0.1"], ["127.0.0.1"]])

    def flip_flop(host, port):
        return next(answers)

    server, port, accepted = listener()
    try:
        hook = WebhookNotifier(
            f"https://rebind.attacker.example:{port}/hook",
            "slack",
            session=public_https_session(flip_flop),
            check_url=lambda url: check_public_url(url, resolver=flip_flop),
            follow_redirects=False,
            show_replies=False,
            timeout=2,
        )
        with pytest.raises(NotifyError, match="isn't allowed: The address must be on the public internet"):
            hook.send("Subject", "text", "html")
    finally:
        server.close()
    assert accepted == []


def test_a_pinned_connection_goes_to_the_checked_address_and_keeps_the_host_name_for_tls(monkeypatch):
    lookups: list[tuple[str, int]] = []
    server, port, accepted = listener()
    monkeypatch.setattr(netguard, "is_public_address", lambda address: True)  # let 127.0.0.1 stand in for the VPS
    targets = []
    real_connect = socket.socket.connect

    def connect(sock, address):
        targets.append(address)
        return real_connect(sock, address)

    monkeypatch.setattr(socket.socket, "connect", connect)
    try:
        session = public_https_session(resolver("127.0.0.1", calls=lookups))
        with pytest.raises(requests.RequestException):  # the listener doesn't speak TLS
            session.post(f"https://hooks.example.com:{port}/hook", json={}, timeout=2)
    finally:
        server.close()
    assert lookups == [("hooks.example.com", port)]  # one lookup, through the given resolver
    assert targets == [("127.0.0.1", port)]  # the checked address, not the name
    assert b"hooks.example.com" in accepted[0]  # the TLS ClientHello names the host (SNI): certificates still count


def test_a_pinned_session_refuses_private_addresses_and_says_so():
    session = public_https_session(resolver("10.0.0.8"))
    with pytest.raises(requests.ConnectionError) as error:
        session.post("https://hooks.example.com/hook", json={}, timeout=2)
    assert refused_by_guard(error.value)
    assert not refused_by_guard(requests.ConnectionError("Connection refused"))
    assert session.trust_env is False  # a proxy from the environment would look the name up itself
