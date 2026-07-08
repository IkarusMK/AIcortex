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
        if ip.is_global:
            continue
        if any(ip in net for net in allowed):
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
    return not bool(cfg.get("tls_insecure", False))


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
    elif cfg.get("tls_insecure", False):
        ctx.check_hostname = False
        ctx.verify_mode = ssl.CERT_NONE
    return ctx


def _ip_allowed(ip, nets) -> bool:
    """Egress policy for one IP: public is fine; private/link-local/etc only if
    inside an operator allow-listed CIDR."""
    return ip.is_global or any(ip in n for n in nets)


# ── DNS-rebinding guard (close the check→connect TOCTOU) ───────────────────
# check_host() validates at preflight, but the client libraries (httpx, ftplib,
# paramiko, paho, smtplib) resolve again at CONNECT time — a hostname that
# answered with a public IP during the check can answer with an internal IP at
# connect (DNS rebinding). guard(host) wraps the actual network call: while it is
# active EVERY socket.getaddrinfo is re-checked against the egress policy and any
# disallowed (internal/link-local, not allow-listed) address is dropped.
#
# Filtering ALL lookups while a guard is active — not just the named host — is
# deliberate: it also closes a cross-host redirect bypass (an already-registered
# endpoint that 30x-redirects the client to 169.254.169.254 or a LAN admin panel
# would otherwise resolve the NEW host unchecked). Public addresses still pass and
# operator allow-listed internal ranges still pass, so legitimate LAN devices keep
# working; only a disallowed target is blocked. TLS/SNI is unaffected (the
# hostname is unchanged), so certificate verification keeps working. Refcounted +
# thread-safe so concurrent/nested guarded calls coexist and getaddrinfo is
# restored only once the last one exits.
_guard_lock = threading.Lock()
_guarded: dict = {}
_orig_getaddrinfo = None


def _filtered_getaddrinfo(host, *args, **kwargs):
    res = _orig_getaddrinfo(host, *args, **kwargs)
    with _guard_lock:
        active = bool(_guarded)
    if not active:
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
        raise socket.gaierror(
            f"netguard: every address for '{host}' is internal/blocked at connect "
            f"time (possible DNS rebinding or redirect); none in INTERNAL_ALLOW_CIDRS")
    return allowed


@contextlib.contextmanager
def guard(host: str):
    """Enforce the egress IP policy at CONNECT time (anti-rebinding, anti-redirect).
    Wrap the network call: `with netguard.guard(host): client.request(...)`. While
    active, every outbound DNS resolution — including redirect targets — is checked
    against the policy, so `host` is only a label for refcounting/diagnostics.

    Coverage note: the guard filters ``socket.getaddrinfo``. Sync clients (httpx,
    smtplib, ftplib, paramiko, paho) and ASYNC httpx (mcp_gateway) on the default
    asyncio resolver all route through it (asyncio's ``loop.getaddrinfo`` calls
    ``socket.getaddrinfo`` in an executor). Do NOT install a C-ares resolver
    (``aiodns``): it would bypass this connect-time guard. The ``check_host`` /
    ``check_url`` preflight still applies regardless."""
    global _orig_getaddrinfo
    key = (host or "").strip().strip("[]").lower() or "-"
    with _guard_lock:
        _guarded[key] = _guarded.get(key, 0) + 1
        if _orig_getaddrinfo is None:
            _orig_getaddrinfo = socket.getaddrinfo
            socket.getaddrinfo = _filtered_getaddrinfo
    try:
        yield
    finally:
        with _guard_lock:
            n = _guarded.get(key, 0) - 1
            if n <= 0:
                _guarded.pop(key, None)
            else:
                _guarded[key] = n
            if not _guarded and _orig_getaddrinfo is not None:
                socket.getaddrinfo = _orig_getaddrinfo
                _orig_getaddrinfo = None
