"""Server-side request forgery guard for outbound calls to company-provided URLs.

Companies point connections at their own systems, which makes us a proxy into whatever network
the API server can reach. Before any request we therefore: require HTTPS, forbid credentials in
the URL, resolve the host ourselves, and refuse any address that is not a public one
(loopback, private, link-local incl. cloud metadata, shared, multicast, reserved, unspecified).
The caller then connects to the *validated IP*, so DNS cannot change between check and use.
"""

import ipaddress
import socket
from dataclasses import dataclass
from urllib.parse import urlsplit

from app.core.config import get_settings


class BlockedDestination(Exception):
    """The URL is not allowed as an outbound target. The message is safe to show the admin."""


@dataclass
class Target:
    scheme: str
    host: str
    port: int
    ip: str  # the validated address to connect to
    path_and_query: str


def resolve_host(host: str) -> list[str]:
    """All addresses a hostname resolves to. A separate function so tests can substitute it."""
    try:
        infos = socket.getaddrinfo(host, None, type=socket.SOCK_STREAM)
    except socket.gaierror as exc:
        raise BlockedDestination(f"cannot resolve host '{host}'") from exc
    return list(dict.fromkeys(info[4][0] for info in infos))


def is_public(ip: str) -> bool:
    address = ipaddress.ip_address(ip)
    if isinstance(address, ipaddress.IPv6Address) and address.ipv4_mapped:
        address = address.ipv4_mapped  # ::ffff:127.0.0.1 is still loopback
    return address.is_global and not address.is_multicast


def validate_url(url: str) -> Target:
    settings = get_settings()
    allow_private = settings.allow_private_connections
    parts = urlsplit(url)
    scheme = parts.scheme.lower()
    if scheme not in ("https", "http"):
        raise BlockedDestination("only https URLs are allowed")
    if scheme == "http" and not allow_private:
        raise BlockedDestination("only https URLs are allowed")
    if parts.username or parts.password:
        raise BlockedDestination("credentials in the URL are not allowed")
    host = parts.hostname
    if not host:
        raise BlockedDestination("the URL has no host")
    try:
        port = parts.port or (443 if scheme == "https" else 80)
    except ValueError as exc:
        raise BlockedDestination("the URL has an invalid port") from exc
    allowed_ports = {int(p) for p in settings.connection_allowed_ports.split(",") if p.strip()}
    if not allow_private and port not in allowed_ports:
        raise BlockedDestination(f"port {port} is not allowed")

    try:
        ipaddress.ip_address(host)
        candidates = [host]
    except ValueError:
        candidates = resolve_host(host)
    if not candidates:
        raise BlockedDestination(f"cannot resolve host '{host}'")
    if not allow_private:
        for ip in candidates:
            if not is_public(ip):
                # Say nothing about *which* internal address, only that it is not permitted.
                raise BlockedDestination("that address is not publicly reachable and cannot be used")
    path = parts.path or "/"
    if parts.query:
        path += "?" + parts.query
    return Target(scheme=scheme, host=host, port=port, ip=candidates[0], path_and_query=path)
