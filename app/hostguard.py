"""Protection against DNS rebinding.

DNS rebinding: a website points its own domain at the AICortex address, and the
visitor's browser then sends requests "from" that site — with the site's domain as
Host and Origin. Native clients (Claude Code, Hermes, SDKs, the Claude and ChatGPT
clouds) send no Origin and are never affected, whatever host name they use: LAN IP,
Docker name, VPN name.

- A request with an Origin (a browser) must come from a host that a rebinding page
  can't have: an IP address, localhost, a name without a dot, a private-use name
  (.local, .lan, .home.arpa, .internal), the BASE_URL host, or one listed in
  MCP_ALLOWED_ORIGINS / MCP_ALLOWED_HOSTS. Otherwise 403.
- The WebUI (/ui) is only ever used from a browser, so its Host must be such a host
  too — this also covers the reads a browser sends without an Origin. Otherwise 421.
- MCP_ALLOWED_HOSTS set: every request's Host must be listed (plus the BASE_URL
  host and loopback) — the strict mode for setups that want it. Otherwise 421.
  Entries may be "*.example.net" for every name under that domain.

Malformed or duplicated Host/Origin headers count as foreign. Unicode names
(bücher.example) are compared in the punycode form browsers send.
"""
import ipaddress
import os
import re

_PRIVATE_SUFFIXES = (".local", ".lan", ".home.arpa", ".internal", ".localhost")
_LOOPBACK = {"localhost", "127.0.0.1", "::1"}


def _csv(name: str) -> list[str]:
    return [v.strip() for v in os.environ.get(name, "").split(",") if v.strip()]


_HOST_PORT = re.compile(r"^(?P<host>\[[^\]]*\]|[^:\[\]]*)(?::\d{1,5})?$")
_NAME = re.compile(r"^[a-z0-9._-]+$")
_IPV6 = re.compile(r"^[0-9a-f:.]+$")


def hostname(value: str) -> str:
    """'Nas.LAN:8787' → 'nas.lan', '[::1]:80' → '::1', 'https://x.y:1' → 'x.y',
    'bücher.example' → 'xn--bcher-kva.example'. '' for anything malformed (userinfo,
    stray brackets, spaces …), which then counts as foreign."""
    value = (value or "").strip()
    if "://" in value:
        value = value.split("://", 1)[1].split("/", 1)[0]
    m = _HOST_PORT.match(value)
    if not m:
        return ""
    host = m.group("host").lower()
    if host.startswith("["):
        return host[1:-1] if _IPV6.match(host[1:-1]) else ""
    if not host.isascii():
        try:
            host = host.encode("idna").decode("ascii")
        except UnicodeError:
            return ""
    host = host.rstrip(".")
    return host if _NAME.match(host) else ""


def is_loopback_name(name: str) -> bool:
    return hostname(name) in _LOOPBACK


def _is_ip(name: str) -> bool:
    try:
        ipaddress.ip_address(name)
        return True
    except ValueError:
        return False


class _Names:
    """Exact host names plus "*.domain" entries (any name under that domain)."""

    def __init__(self, entries):
        self.exact, self.suffixes = set(), []
        for e in entries:
            if e.startswith("*."):
                if hostname(e[2:]):
                    self.suffixes.append("." + hostname(e[2:]))
            elif hostname(e):
                self.exact.add(hostname(e))

    def __bool__(self):
        return bool(self.exact or self.suffixes)

    def __contains__(self, name: str) -> bool:
        return bool(name) and (name in self.exact or name.endswith(tuple(self.suffixes)))


class Policy:
    """Which hosts count as ours; read from the environment once."""

    def __init__(self):
        own = [os.environ.get("BASE_URL", ""), *_LOOPBACK]
        strict = _csv("MCP_ALLOWED_HOSTS")
        self.strict = _Names(strict + own) if strict else _Names([])
        self.ours = _Names(strict + _csv("MCP_ALLOWED_ORIGINS") + own)

    def safe(self, name: str) -> bool:
        """A host a rebinding page can't stand behind."""
        name = hostname(name)
        return bool(name) and (name in self.ours or _is_ip(name) or "." not in name
                               or name.endswith(_PRIVATE_SUFFIXES))

    def refuse(self, path: str, host: str, origin: str | None) -> int:
        """HTTP status to refuse the request with, or 0 to let it through."""
        if self.strict and hostname(host) not in self.strict:
            return 421
        if origin is not None and (origin.strip().lower() == "null" or not self.safe(origin)):
            return 403
        if (path == "/ui" or path.startswith("/ui/")) and not self.safe(host):
            return 421
        return 0

    def refuse_headers(self, path: str, hosts: list[str], origins: list[str]) -> int:
        """Like ``refuse`` for the raw header lists; a repeated header is refused (a
        server reading the other copy than this check would act on an unchecked one)."""
        if len(origins) > 1:
            return 403
        if len(hosts) > 1:
            return 421
        return self.refuse(path, hosts[0] if hosts else "", origins[0] if origins else None)


class HostGuard:
    """ASGI middleware applying ``Policy`` to every HTTP request."""

    def __init__(self, app):
        self.app = app
        self.policy = Policy()

    async def __call__(self, scope, receive, send):
        if scope["type"] == "http":
            raw = [(k.decode("latin-1").lower(), v.decode("latin-1"))
                   for k, v in scope.get("headers") or []]
            status = self.policy.refuse_headers(
                scope.get("path", ""), [v for k, v in raw if k == "host"],
                [v for k, v in raw if k == "origin"])
            if status:
                body = b"Misdirected Request" if status == 421 else b"Forbidden Origin"
                await send({"type": "http.response.start", "status": status,
                            "headers": [(b"content-type", b"text/plain"),
                                        (b"content-length", str(len(body)).encode())]})
                await send({"type": "http.response.body", "body": body})
                return
        await self.app(scope, receive, send)
