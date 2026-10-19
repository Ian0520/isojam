"""Restrict networking inside the dedicated, disposable Python downloader.

Install before importing yt-dlp/HTTP clients. Every hostname resolution and
socket connection is checked, including redirects. The HTTP client connects to
the checked numeric address; it cannot resolve the name a second time.
"""

import ipaddress
import socket

_HOSTS = {"youtube.com", "youtubei.googleapis.com", "jnn-pa.googleapis.com"}
_SUFFIXES = (".youtube.com", ".googlevideo.com")


def public_address(value: str) -> bool:
    try:
        address = ipaddress.ip_address(value)
    except ValueError:
        return False
    if "%" in value:
        return False
    if isinstance(address, ipaddress.IPv6Address) and address.ipv4_mapped is not None:
        address = address.ipv4_mapped
    return address.is_global and not address.is_multicast and not address.is_reserved


def install_network_guard() -> None:
    original_getaddrinfo = socket.getaddrinfo
    original_socket = socket.socket

    def getaddrinfo(host, port, *args, **kwargs):
        if not isinstance(host, str) or port not in {443, "443"}:
            raise OSError("YouTube network destination is not permitted")
        name = host.lower().rstrip(".")
        if name not in _HOSTS and not name.endswith(_SUFFIXES):
            raise OSError("YouTube network destination is not permitted")
        addresses = original_getaddrinfo(host, port, *args, **kwargs)
        if not addresses or any(not public_address(item[4][0]) for item in addresses):
            raise OSError("YouTube DNS returned a non-public destination")
        return addresses

    class PublicSocket(original_socket):
        def _check(self, address):
            if (
                self.family not in {socket.AF_INET, socket.AF_INET6}
                or not isinstance(address, tuple)
                or len(address) < 2
                or address[1] != 443
                or not public_address(address[0])
            ):
                raise OSError("YouTube network destination is not permitted")

        def connect(self, address):
            self._check(address)
            return super().connect(address)

        def connect_ex(self, address):
            self._check(address)
            return super().connect_ex(address)

    socket.getaddrinfo = getaddrinfo
    socket.socket = PublicSocket
