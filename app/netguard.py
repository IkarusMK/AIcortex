"""Network egress guard — SSRF boundary for the model-driven dispatchers.

service_add / mqtt_add / ftp_add are themselves tools, so a prompt-injected
model could register an INTERNAL target (cloud metadata at 169.254.169.254, a
LAN admin panel, localhost services) and then call it. The list of registered
services is therefore NOT an SSRF boundary on its own.

This module is the boundary: every outbound host is resolved and each resolved
IP is checked. Private, loopback, link-local, reserved, multicast and
unspecified addresses are BLOCKED — unless the IP falls inside an
operator-configured allow-list (``INTERNAL_ALLOW_CIDRS``, env-only, NOT settable
by the model). Public addresses are allowed.

Set ``INTERNAL_ALLOW_CIDRS`` to the internal ranges you actually use, e.g. your
LAN and/or a VPN subnet:

    INTERNAL_ALLOW_CIDRS=<your-lan-cidr>,<your-vpn-cidr>
    e.g. 192.168.0.0/16,10.0.0.0/8,100.64.0.0/10
"""
import contextlib
import contextvars
import functools
import ipaddress
import os
import socket
import threading
from urllib.parse import urlparse


def _allowed_cidrs():
    raw = os.environ.get("INTERNAL_ALLOW_CIDRS", "")
    nets = []
    for part in raw.replace(";", ",").split(","):
        part = part.strip()
        if not part:
            continue
        try:
            nets.append(ipaddress.ip_network(part, strict=False))
        except ValueError:
            continue
    return nets


def _resolve_ips(host: str):
    ips = set()
    for info in socket.getaddrinfo(host, None):
        ips.add(info[4][0])
    return [ipaddress.ip_address(ip.split("%")[0]) for ip in ips]


def check_host(host: str):
    """Return (ok: bool, reason: str). ok=True ⇒ egress to host is permitted.

    Public IPs pass. Private/link-local/loopback/reserved IPs pass ONLY if they
    are inside an INTERNAL_ALLOW_CIDRS range (operator opt-in). DNS is resolved
    here so a public hostname that maps to an internal IP is still caught.
    """
    if not host:
        return False, "empty host"
    h = host.strip().strip("[]")
    allowed = _allowed_cidrs()
    try:
        ips = _resolve_ips(h)
    except Exception as exc:
        return False, f"cannot resolve host '{host}': {exc}"
    if not ips:
        return False, f"no addresses for host '{host}'"
    for ip in ips:
        if _ip_allowed(ip, allowed):
            continue
        return False, (
            f"blocked: {ip} is internal/link-local and not in INTERNAL_ALLOW_CIDRS. "
            "Add the range you trust (e.g. your LAN or VPN subnet) to that env var "
            "to permit it."
        )
    return True, "ok"


def check_url(url: str):
    """check_host for an http(s) URL — extracts and validates the hostname."""
    host = urlparse(url).hostname
    return check_host(host)


def tls_verify(cfg: dict):
    """Resolve the TLS ``verify`` value for an httpx client from a device/endpoint
    config. SECURE BY DEFAULT (#10): certificate verification is ON unless the
    operator explicitly opts out. Precedence:

    - ``ca_bundle`` set (a path) → verify against that CA bundle / pinned cert
      (the right way to trust a self-signed LAN device),
    - ``tls_insecure`` true → verification OFF — the operator's explicit choice
      for a self-signed device. These configs are written only by the admin-only
      registration tools (scan_add / webdav_add / service_add), so a
      normal/non-admin caller can't disable verification.
    - otherwise → ``True`` (verify).

    Returns a value suitable for httpx's ``verify=`` (bool or a path string).
    """
    if not isinstance(cfg, dict):
        return True
    ca = cfg.get("ca_bundle")
    if ca:
        return str(ca)
    return not _yes(cfg.get("tls_insecure", False))


def ssl_context(cfg: dict):
    """Build an ``ssl.SSLContext`` from the SAME knobs as ``tls_verify`` — for the
    socket clients that need a context object rather than httpx's ``verify=``
    (ftp / mqtt / imap / smtp). One shared TLS policy across every integration.
    Precedence: ``ca_bundle`` (trust a pinned CA/cert) > ``tls_insecure``
    (verification OFF) > default (verify). Secure by default; only the admin-only
    ``*_add`` tools ever set these keys.
    """
    import ssl
    ctx = ssl.create_default_context()
    cfg = cfg if isinstance(cfg, dict) else {}
    ca = (cfg.get("ca_bundle") or "").strip()
    if ca:
        ctx.load_verify_locations(ca)
    elif _yes(cfg.get("tls_insecure", False)):
        ctx.check_hostname = False
        ctx.verify_mode = ssl.CERT_NONE
    return ctx


# IPv6 forms ``is_global`` gets wrong for an egress guard. NAT64 (RFC 6052) carries an
# IPv4 address that a DNS64/NAT64 gateway connects to — 64:ff9b::7f00:1 is 127.0.0.1 —
# so it counts as that address. The others are deprecated and never public:
# IPv4-compatible ::a.b.c.d, site-local fec0::/10, and 6to4 2002::/16 (RFC 7526),
# which newer Pythons already treat as private and older ones as global.
_NAT64 = ipaddress.ip_network("64:ff9b::/96")
_NEVER_PUBLIC = tuple(ipaddress.ip_network(n) for n in ("::/96", "fec0::/10", "2002::/16"))


def _nat64_v4(ip):
    """The IPv4 address a NAT64 address stands for, else None."""
    if ip.version == 6 and ip in _NAT64:
        return ipaddress.IPv4Address(int(ip) & 0xFFFF_FFFF)
    return None


def _public(ip) -> bool:
    """A public address. is_global is True for some multicast ranges (224.0.0.1,
    ff02::1), which this guard promises to block — so multicast never counts; nor do
    the IPv6 forms above, and a NAT64 address is as public as the IPv4 it carries."""
    v4 = _nat64_v4(ip)
    if v4 is not None:
        return _public(v4)
    if ip.version == 6 and any(ip in n for n in _NEVER_PUBLIC):
        return False
    return ip.is_global and not ip.is_multicast


def _ip_allowed(ip, nets) -> bool:
    """Egress policy for one IP: public is fine; private/link-local/etc only if
    inside an operator allow-listed CIDR (for NAT64: the IPv4 address it carries)."""
    v4 = _nat64_v4(ip)
    return (_public(ip) or any(ip in n for n in nets)
            or (v4 is not None and any(v4 in n for n in nets)))


def _yes(value) -> bool:
    """An explicit yes: True, 1, "1", "true", "yes", "on". Anything else — notably
    the text "false" in a hand-edited config — is no."""
    if isinstance(value, str):
        return value.strip().lower() in ("1", "true", "yes", "on")
    return value is True or value == 1


def tls_insecure(cfg: dict) -> bool:
    """Whether a device/endpoint config explicitly switches TLS verification off."""
    return isinstance(cfg, dict) and _yes(cfg.get("tls_insecure", False))


# ── DNS-rebinding guard (close the check→connect TOCTOU) ───────────────────
# check_host() validates at preflight, but the client libraries (httpx, ftplib,
# paramiko, paho, smtplib) resolve again at CONNECT time — a hostname that
# answered with a public IP during the check can answer with an internal IP at
# connect (DNS rebinding). guard(host) wraps the actual network call: inside it,
# EVERY socket.getaddrinfo made by that call is re-checked against the egress
# policy and any disallowed (internal/link-local, not allow-listed) address is
# dropped.
#
# Filtering every lookup of the guarded call — not just the named host — is
# deliberate: it also closes a cross-host redirect bypass (an already-registered
# endpoint that 30x-redirects the client to 169.254.169.254 or a LAN admin panel
# would otherwise resolve the NEW host unchecked). Public addresses still pass and
# operator allow-listed internal ranges still pass, so legitimate LAN devices keep
# working; only a disallowed target is blocked. TLS/SNI is unaffected (the
# hostname is unchanged), so certificate verification keeps working.
#
# The filter is scoped to the CONTEXT that entered the guard (a contextvar): the
# guarded call and whatever it runs through asyncio.to_thread are filtered, other
# threads and other asyncio tasks are not — an unrelated lookup (OIDC discovery,
# another person's request) must not be filtered because some guard happens to be
# open. socket.getaddrinfo is still patched process-wide while any guard is open
# (refcounted + thread-safe) and restored once the last one exits.
#
# An async client must NOT rely on this filter: uvloop's resolver never calls
# socket.getaddrinfo, asyncio's executor threads don't carry the context, and an IP
# literal is connected without any lookup at all. Async callers use
# ``pinned_async_transport`` instead, which checks and pins every request itself.
_guard_lock = threading.Lock()
_guarded: dict = {}
_orig_getaddrinfo = None
_SYSTEM_GETADDRINFO = socket.getaddrinfo
_ACTIVE: contextvars.ContextVar = contextvars.ContextVar("netguard_active", default=0)


def _raw_getaddrinfo():
    """The resolver underneath the filter (the filter itself never resolves twice)."""
    orig = _orig_getaddrinfo
    if orig is not None:
        return orig
    current = socket.getaddrinfo
    return _SYSTEM_GETADDRINFO if current is _filtered_getaddrinfo else current


def _blocked_message(host) -> str:
    return (f"netguard: every address for '{host}' is internal/blocked at connect "
            f"time (possible DNS rebinding or redirect); none in INTERNAL_ALLOW_CIDRS")


def _filtered_getaddrinfo(host, *args, **kwargs):
    res = _raw_getaddrinfo()(host, *args, **kwargs)
    if _ACTIVE.get() <= 0:
        return res
    nets = _allowed_cidrs()
    allowed = []
    for entry in res:
        try:
            ip = ipaddress.ip_address(entry[4][0].split("%")[0])
        except (ValueError, IndexError):
            continue
        if _ip_allowed(ip, nets):
            allowed.append(entry)
    if not allowed:
        raise socket.gaierror(_blocked_message(host))
    return allowed


@contextlib.contextmanager
def guard(host: str):
    """Enforce the egress IP policy at CONNECT time (anti-rebinding, anti-redirect)
    for a SYNC network call: `with netguard.guard(host): client.request(...)`. While
    active, every DNS resolution made in this context — including redirect targets —
    is checked against the policy, so `host` is only a label for refcounting and
    diagnostics. Lookups of other threads and other asyncio tasks are left alone.

    Coverage note: the guard filters ``socket.getaddrinfo`` in the calling context.
    Sync clients (httpx, smtplib, ftplib, paramiko, paho) resolve there — even for
    an IP literal (``socket.create_connection`` always calls getaddrinfo). ASYNC
    httpx does not (uvloop, executor threads, IP literals); it must use
    ``pinned_async_transport``. The ``check_host`` / ``check_url`` preflight still
    applies regardless."""
    global _orig_getaddrinfo
    key = (host or "").strip().strip("[]").lower() or "-"
    with _guard_lock:
        _guarded[key] = _guarded.get(key, 0) + 1
        if _orig_getaddrinfo is None:
            _orig_getaddrinfo = socket.getaddrinfo
            socket.getaddrinfo = _filtered_getaddrinfo
    token = _ACTIVE.set(_ACTIVE.get() + 1)
    try:
        yield
    finally:
        try:
            _ACTIVE.reset(token)
        except ValueError:      # exited in another context than it was entered in
            _ACTIVE.set(max(0, _ACTIVE.get() - 1))
        with _guard_lock:
            n = _guarded.get(key, 0) - 1
            if n <= 0:
                _guarded.pop(key, None)
            else:
                _guarded[key] = n
            if not _guarded and _orig_getaddrinfo is not None:
                socket.getaddrinfo = _orig_getaddrinfo
                _orig_getaddrinfo = None


# ── Pinned async egress (every hop resolved once, checked, then connected to) ──
class Blocked(PermissionError):
    """Every address of a destination is outside the egress policy."""


_MAX_ADDRESSES = 3      # connect attempts per request before giving up


def allowed_addresses(host: str, port: int = 0) -> list:
    """Resolve `host` ONCE and return the addresses the egress policy allows, in the
    resolver's order — the caller connects to exactly these, so nothing can answer
    differently between the check and the connect. An IP literal is checked as is
    (it has no lookup that a filter could catch). Raises ``Blocked`` when none is
    allowed and ``socket.gaierror`` when the name does not resolve."""
    h = (host or "").strip().strip("[]")
    if not h:
        raise Blocked("netguard: empty host")
    try:
        ipaddress.ip_address(h.split("%")[0])
        candidates = [h]
    except ValueError:
        infos = _raw_getaddrinfo()(h, port or None, type=socket.SOCK_STREAM)
        candidates = []
        for info in infos:
            try:
                ip = str(info[4][0])
            except (IndexError, TypeError):
                continue
            if ip not in candidates:
                candidates.append(ip)
    nets = _allowed_cidrs()
    allowed = []
    for ip in candidates:
        try:
            parsed = ipaddress.ip_address(ip.split("%")[0])
        except ValueError:
            continue
        if "%" not in ip and _ip_allowed(parsed, nets):
            allowed.append(ip)
    if not allowed:
        raise Blocked(_blocked_message(h))
    return allowed


async def _allowed_async(host: str, port: int) -> list:
    import anyio
    return await anyio.to_thread.run_sync(allowed_addresses, host, port)


def _pinned_request(lib, request, ip: str):
    """`request` aimed at the checked address `ip`, with the original host name kept
    as the Host header and as the TLS server name (SNI + certificate check)."""
    host = request.url.host
    if ip == host:
        return request
    return lib.Request(request.method, request.url.copy_with(host=ip),
                       headers=request.headers, stream=request.stream,
                       extensions={**request.extensions, "sni_hostname": host})


@functools.lru_cache(maxsize=None)
def _pinned_class(lib):
    """The pinned transport for an httpx-compatible library (httpx, or httpx2 that the
    MCP SDK's clients are built on)."""

    class PinnedAsyncTransport(lib.AsyncBaseTransport):
        def __init__(self, transport_kwargs: dict):
            self._kwargs = dict(transport_kwargs)
            self._pools: dict = {}

        def _pool(self, url):
            # One connection pool per original host name, so a connection opened (and
            # TLS-verified) for one name is never reused for another on the same IP.
            key = (url.scheme, url.host, url.port)
            if key not in self._pools:
                self._pools[key] = lib.AsyncHTTPTransport(**self._kwargs)
            return self._pools[key]

        async def handle_async_request(self, request):
            url = request.url
            port = url.port or {"https": 443, "http": 80}.get(url.scheme, 0)
            addresses = await _allowed_async(url.host, port)
            pool = self._pool(url)
            failure = None
            for ip in addresses[:_MAX_ADDRESSES]:
                try:
                    return await pool.handle_async_request(_pinned_request(lib, request, ip))
                except lib.ConnectError as exc:     # nothing was sent yet: try the next one
                    failure = exc
            raise failure

        async def aclose(self) -> None:
            pools, self._pools = self._pools, {}
            for pool in pools.values():
                await pool.aclose()

    return PinnedAsyncTransport


def pinned_async_transport(lib=None, **transport_kwargs):
    """An async transport for an httpx(-compatible) client whose EVERY request —
    redirect hops included — is resolved once, checked against the egress policy and
    sent to a checked address, while the Host header and the TLS name stay the host
    name. Independent of the event loop's resolver (uvloop) and of IP literals, which
    the getaddrinfo filter of ``guard`` cannot see. `transport_kwargs` go to the
    library's ``AsyncHTTPTransport`` (e.g. ``verify``)."""
    if lib is None:
        import httpx as lib
    return _pinned_class(lib)(transport_kwargs)


def mcp_client_factory(headers=None, timeout=None, auth=None):
    """An ``httpx_client_factory`` for the MCP SDK's transports (fastmcp's SSE client):
    its httpx2 client connects through the pinned transport and follows no redirect
    itself (the SDK only follows same-origin ones, each through this transport)."""
    import httpx2
    kwargs = {"timeout": timeout or httpx2.Timeout(30.0, read=300.0),
              "transport": pinned_async_transport(httpx2), "follow_redirects": False}
    if headers is not None:
        kwargs["headers"] = headers
    if auth is not None:
        kwargs["auth"] = auth
    return httpx2.AsyncClient(**kwargs)
