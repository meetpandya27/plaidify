"""Where the headless browser may go.

Two independent checks live here:

* **Host scoping.** A blueprint may navigate only over http(s), to its own
  domain (and that domain's subdomains) plus the domains it explicitly lists in
  ``allowed_domains``. Declared submission targets (login form, MFA form,
  logout) are matched with the same rules.
* **Address policy.** Requests to private, loopback, link-local, shared (CGNAT),
  multicast, reserved and cloud-metadata addresses are refused. The browser pool
  applies it to *every* request the page makes, resolving the hostname each time
  (with a short cache), so redirects, sub-resources and page-initiated fetches
  are covered, not just the first URL.

This module is pure policy: no Playwright, no blueprint imports.
"""

from __future__ import annotations

import asyncio
import fnmatch
import ipaddress
import socket
import time
from dataclasses import dataclass
from typing import Iterable, Optional, Sequence, Union
from urllib.parse import urlsplit

IPAddress = Union[ipaddress.IPv4Address, ipaddress.IPv6Address]

ALLOWED_SCHEMES = frozenset({"http", "https"})
_DEFAULT_PORTS = {"http": 80, "https": 443, "ws": 80, "wss": 443}

# Public addresses that still reach platform internals from inside a cloud VM.
_PLATFORM_SERVICE_NETWORKS = (
    ipaddress.ip_network("168.63.129.16/32"),  # Azure wireserver / DHCP / DNS
    ipaddress.ip_network("100.100.100.200/32"),  # Alibaba Cloud metadata
)
_SHARED_ADDRESS_SPACE = ipaddress.ip_network("100.64.0.0/10")  # RFC 6598 (CGNAT)
_NAT64_PREFIX = ipaddress.ip_network("64:ff9b::/96")


# ── Host scoping ──────────────────────────────────────────────────────────────


@dataclass(frozen=True)
class HostRule:
    """One host a blueprint may navigate to."""

    host: str
    port: Optional[int] = None
    include_subdomains: bool = True

    def allows(self, host: str, port: Optional[int]) -> bool:
        host = normalize_host(host)
        if self.port is not None and port != self.port:
            return False
        if host == self.host:
            return True
        return self.include_subdomains and host.endswith("." + self.host)


def normalize_host(host: str) -> str:
    """Lower-case, strip a trailing dot and brackets, and IDNA-encode a hostname."""
    value = (host or "").strip().strip("[]").rstrip(".").lower()
    if not value:
        return ""
    try:
        ipaddress.ip_address(value)
        return value
    except ValueError:
        pass
    try:
        return value.encode("idna").decode("ascii")
    except UnicodeError:
        return value


def _is_ip_literal(host: str) -> bool:
    try:
        ipaddress.ip_address(host)
        return True
    except ValueError:
        return False


def parse_domain_rule(domain: str) -> HostRule:
    """Turn a blueprint ``domain`` / ``allowed_domains`` entry into a :class:`HostRule`.

    ``www.bank.com`` and ``*.bank.com`` allow ``bank.com`` and all of its
    subdomains; an IP literal or a single-label name (``localhost``) matches
    exactly. An explicit ``:port`` must then match too.

    Raises ValueError for anything that is not ``host[:port]``.
    """
    raw = (domain or "").strip()
    if not raw:
        raise ValueError("domain must not be empty")
    if "://" in raw or "/" in raw or "@" in raw or "?" in raw or "#" in raw:
        raise ValueError(f"{domain!r} must be a bare host[:port], without scheme, path or credentials")

    wildcard = raw.startswith("*.")
    if wildcard:
        raw = raw[2:]

    try:
        parts = urlsplit(f"//{raw}")
        host = parts.hostname or ""
        port = parts.port
    except ValueError as exc:
        raise ValueError(f"{domain!r} is not a valid host[:port]") from exc

    host = normalize_host(host)
    if not host:
        raise ValueError(f"{domain!r} is not a valid host[:port]")

    if _is_ip_literal(host) or "." not in host:
        if wildcard:
            raise ValueError(f"{domain!r}: wildcards need a DNS name")
        return HostRule(host=host, port=port, include_subdomains=False)

    if not wildcard and host.startswith("www."):
        host = host[4:]
    return HostRule(host=host, port=port, include_subdomains=True)


def split_url(url: str) -> tuple[str, str, Optional[int], str, bool]:
    """Return ``(scheme, host, port, path, has_userinfo)`` with default ports filled in."""
    parts = urlsplit(url or "")
    scheme = (parts.scheme or "").lower()
    try:
        port = parts.port
    except ValueError:
        port = -1  # malformed; never matches a rule
    if port is None:
        port = _DEFAULT_PORTS.get(scheme)
    host = normalize_host(parts.hostname or "")
    has_userinfo = parts.username is not None or parts.password is not None
    return scheme, host, port, parts.path or "/", has_userinfo


def navigation_block_reason(url: str, rules: Sequence[HostRule]) -> Optional[str]:
    """Why a navigation to ``url`` is not allowed, or None when it is.

    Only http(s) is ever allowed (never file:, chrome:, data:, javascript: …),
    credentials in the URL are refused, and when ``rules`` is non-empty the host
    must match one of them.
    """
    scheme, host, port, _path, has_userinfo = split_url(url)
    if scheme not in ALLOWED_SCHEMES:
        return f"navigation to a {scheme or 'relative'}: URL is not allowed; only http(s) is"
    if not host:
        return "navigation URL has no host"
    if has_userinfo:
        return "navigation URLs may not carry credentials"
    if rules and not any(rule.allows(host, port) for rule in rules):
        return f"navigation to {host} is outside the blueprint's domains"
    return None


@dataclass(frozen=True)
class TargetPattern:
    """A declared submission target: a path glob, optionally pinned to one host."""

    path_glob: str
    host_rule: Optional[HostRule] = None

    def matches(self, url: str, rules: Sequence[HostRule]) -> bool:
        scheme, host, port, path, has_userinfo = split_url(url)
        if scheme not in ALLOWED_SCHEMES or not host or has_userinfo:
            return False
        if self.host_rule is not None:
            if not self.host_rule.allows(host, port):
                return False
        elif rules and not any(rule.allows(host, port) for rule in rules):
            return False
        return fnmatch.fnmatchcase(path, self.path_glob)


def parse_target(target: str) -> TargetPattern:
    """Parse a declared target: ``/path`` (on the blueprint's hosts) or an absolute http(s) URL.

    ``*`` in the path matches any run of characters. Query strings are ignored
    when matching.
    """
    raw = (target or "").strip()
    if not raw:
        raise ValueError("target must not be empty")
    if raw.startswith("/"):
        return TargetPattern(path_glob=raw.split("?", 1)[0].split("#", 1)[0] or "/")

    parts = urlsplit(raw)
    scheme = (parts.scheme or "").lower()
    if scheme not in ALLOWED_SCHEMES:
        raise ValueError(f"target {target!r} must be a /path or an http(s) URL")
    if parts.username is not None or parts.password is not None:
        raise ValueError(f"target {target!r} may not carry credentials")
    host = normalize_host(parts.hostname or "")
    if not host:
        raise ValueError(f"target {target!r} has no host")
    try:
        port = parts.port
    except ValueError as exc:
        raise ValueError(f"target {target!r} has an invalid port") from exc
    return TargetPattern(
        path_glob=parts.path or "/",
        host_rule=HostRule(host=host, port=port, include_subdomains=False),
    )


def url_matches_targets(url: str, targets: Iterable[TargetPattern], rules: Sequence[HostRule]) -> bool:
    return any(target.matches(url, rules) for target in targets)


# ── Address policy ────────────────────────────────────────────────────────────


def _embedded_ipv4(ip: IPAddress) -> Optional[ipaddress.IPv4Address]:
    """The IPv4 address hidden inside mapped / NAT64 / 6to4 / Teredo IPv6 forms."""
    if not isinstance(ip, ipaddress.IPv6Address):
        return None
    if ip.ipv4_mapped is not None:
        return ip.ipv4_mapped
    if ip in _NAT64_PREFIX:
        return ipaddress.IPv4Address(int(ip) & 0xFFFFFFFF)
    if ip.sixtofour is not None:
        return ip.sixtofour
    if ip.teredo is not None:
        return ip.teredo[1]
    return None


def address_block_reason(ip: IPAddress, *, allow_loopback: bool = False) -> Optional[str]:
    """Why the browser may not contact ``ip``, or None for an ordinary public address."""
    embedded = _embedded_ipv4(ip)
    if embedded is not None:
        reason = address_block_reason(embedded, allow_loopback=allow_loopback)
        if reason:
            return reason

    if ip.is_loopback:
        return None if allow_loopback else "loopback address"
    if any(ip in network for network in _PLATFORM_SERVICE_NETWORKS if network.version == ip.version):
        return "cloud platform service address"
    if ip.is_link_local:
        return "link-local address"
    if ip.version == 4 and ip in _SHARED_ADDRESS_SPACE:
        return "shared (CGNAT) address"
    if ip.is_private:
        return "private address"
    if ip.is_multicast or ip.is_unspecified or ip.is_reserved:
        return "reserved address"
    if not ip.is_global:
        return "non-public address"
    return None


def _loopback_name(host: str) -> bool:
    # Chromium resolves *.localhost to loopback internally, without asking DNS.
    return host == "localhost" or host.endswith(".localhost")


class AddressPolicy:
    """Refuses browser requests to non-public addresses.

    ``block_private=False`` disables the check (operator-trusted connectors that
    must reach an intranet). ``allow_loopback`` exempts loopback only, for the
    demo sandbox and tests.
    """

    def __init__(
        self,
        *,
        block_private: bool = True,
        allow_loopback: bool = False,
        cache_ttl: float = 30.0,
        max_cache_entries: int = 2048,
    ) -> None:
        self.block_private = block_private
        self.allow_loopback = allow_loopback
        self._cache_ttl = cache_ttl
        self._max_cache_entries = max_cache_entries
        self._cache: dict[str, tuple[float, Optional[str]]] = {}

    @property
    def active(self) -> bool:
        return self.block_private

    async def _resolve(self, host: str) -> list[IPAddress]:
        loop = asyncio.get_running_loop()
        infos = await loop.getaddrinfo(host, None, type=socket.SOCK_STREAM)
        addresses: list[IPAddress] = []
        for _family, _type, _proto, _canon, sockaddr in infos:
            try:
                addresses.append(ipaddress.ip_address(str(sockaddr[0]).split("%", 1)[0]))
            except ValueError:
                continue
        return addresses

    async def host_block_reason(self, host: str) -> Optional[str]:
        if not self.block_private:
            return None
        host = normalize_host(host)
        if not host:
            return None
        if _loopback_name(host):
            return None if self.allow_loopback else "loopback address"
        try:
            return address_block_reason(ipaddress.ip_address(host), allow_loopback=self.allow_loopback)
        except ValueError:
            pass

        now = time.monotonic()
        cached = self._cache.get(host)
        if cached is not None and cached[0] > now:
            return cached[1]

        try:
            addresses = await self._resolve(host)
        except (OSError, UnicodeError):
            # Unresolvable here means unresolvable for the browser too; let it
            # fail with its own DNS error rather than guessing.
            return None

        reason = None
        for address in addresses:
            reason = address_block_reason(address, allow_loopback=self.allow_loopback)
            if reason:
                reason = f"{host} resolves to a {reason}"
                break

        if len(self._cache) >= self._max_cache_entries:
            self._cache.clear()
        self._cache[host] = (now + self._cache_ttl, reason)
        return reason

    async def url_block_reason(self, url: str) -> Optional[str]:
        """Why a request to ``url`` must be refused, or None."""
        if not self.block_private:
            return None
        parts = urlsplit(url or "")
        if (parts.scheme or "").lower() not in {"http", "https", "ws", "wss"}:
            return None
        return await self.host_block_reason(parts.hostname or "")
