import socket

import pytest

from app.youtube_network import install_network_guard, public_address


@pytest.mark.parametrize(
    "address",
    [
        "127.0.0.1",
        "10.1.2.3",
        "192.168.1.1",
        "169.254.169.254",
        "0.0.0.0",
        "224.0.0.1",
        "::1",
        "fc00::1",
        "fe80::1",
        "::ffff:127.0.0.1",
        "fe80::1%eth0",
        "not-an-address",
    ],
)
def test_rejects_non_public_connection_addresses(address):
    assert not public_address(address)


@pytest.fixture
def guard(monkeypatch):
    original_dns, original_socket = socket.getaddrinfo, socket.socket
    responses, connections = [], []

    class FakeSocket:
        family = socket.AF_INET

        def connect(self, address):
            connections.append(address)

        connect_ex = connect

    monkeypatch.setattr(socket, "getaddrinfo", lambda *args, **kwargs: responses)
    monkeypatch.setattr(socket, "socket", FakeSocket)
    install_network_guard()
    try:
        yield responses, connections
    finally:
        socket.getaddrinfo, socket.socket = original_dns, original_socket


@pytest.mark.parametrize(
    "host",
    [
        "localhost",
        "127.0.0.1",
        "youtube.com.evil.test",
        "evil.test",
        "metadata.google.internal",
    ],
)
def test_rejects_non_allowlisted_hosts_including_redirect_targets(guard, host):
    with pytest.raises(OSError, match="not permitted"):
        socket.getaddrinfo(host, 443)


def test_checks_every_dns_answer_not_just_first_and_rejects_rebinding(guard):
    responses, _ = guard
    public = (socket.AF_INET, socket.SOCK_STREAM, 6, "", ("142.250.1.1", 443))
    responses.append(public)
    assert socket.getaddrinfo("www.youtube.com", 443) == [public]
    responses.append((socket.AF_INET6, socket.SOCK_STREAM, 6, "", ("::1", 443, 0, 0)))
    with pytest.raises(OSError, match="non-public"):
        socket.getaddrinfo("www.youtube.com", 443)


def test_connections_use_checked_numeric_public_addresses_only(guard):
    _, connections = guard
    connection = socket.socket()
    connection.connect(("142.250.1.1", 443))
    assert connections == [("142.250.1.1", 443)]
    for address in [("127.0.0.1", 443), ("142.250.1.1", 80), ("www.youtube.com", 443)]:
        with pytest.raises(OSError, match="not permitted"):
            connection.connect(address)
        with pytest.raises(OSError, match="not permitted"):
            connection.connect_ex(address)
