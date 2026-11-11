"""Rate limiting for the auth endpoints and the public webhook receiver.

A token bucket per client IP and endpoint group, kept in memory: a group allows a
burst of N requests and then one more every period/N; past that the request is
answered with 429 and a Retry-After header. A refused request costs nothing, so a
client that keeps hammering is not locked out any longer than one that waits.

    group     paths                                                env (default)
    auth      /authorize /token /consent /revoke /auth/callback   RATE_LIMIT_AUTH (30/min)
    login     /ui/login /ui/callback /ui/mcp-oauth/callback       RATE_LIMIT_LOGIN (30/min)
    register  /register                                           RATE_LIMIT_REGISTER (10/hour)
    hooks     /hooks/<name>                                       RATE_LIMIT_HOOKS (60/min)

A limit is "N/second|minute|hour|day" (s, sec, m, min, h, hr, d also work) or a bare
N per minute; 0 switches the group off, RATE_LIMIT=0 switches off all of them.

The client IP is the ASGI scope's client, which uvicorn has already taken from
X-Forwarded-For when the proxy is trusted (FORWARDED_ALLOW_IPS). With "*" that is
the FIRST entry, which a client can forge when the proxy appends to the header —
naming the proxy's address there makes the count exact. IPv6 clients are
grouped by their /64, the block a single host usually gets. At most MAX_CLIENTS
addresses are tracked per group; the least recently seen is forgotten first, which
only ever resets that address to a full bucket.
"""
import ipaddress
import math
import os
import re
import time
from collections import OrderedDict

MAX_CLIENTS = 10_000
_EPS = 1e-9                     # float slack when a bucket has refilled to exactly 1

_PERIODS = {"s": 1.0, "sec": 1.0, "second": 1.0,
            "m": 60.0, "min": 60.0, "minute": 60.0,
            "h": 3600.0, "hr": 3600.0, "hour": 3600.0,
            "d": 86400.0, "day": 86400.0}
_LIMIT = re.compile(r"^(?P<n>\d+)(?:/(?P<period>[a-z]+?)s?)?$")

# group → (env var, default)
GROUPS = {
    "auth": ("RATE_LIMIT_AUTH", "30/min"),
    "login": ("RATE_LIMIT_LOGIN", "30/min"),
    "register": ("RATE_LIMIT_REGISTER", "10/hour"),
    "hooks": ("RATE_LIMIT_HOOKS", "60/min"),
}
_EXACT = {
    "/authorize": "auth", "/token": "auth", "/consent": "auth", "/revoke": "auth",
    "/auth/callback": "auth",
    "/ui/login": "login", "/ui/callback": "login", "/ui/mcp-oauth/callback": "login",
    "/register": "register",
}
_PREFIXES = (("/hooks/", "hooks"),)


def parse_limit(text: str) -> tuple[int, float] | None:
    """'30/min' → (30, 60.0); '0' → None (off). ValueError for anything else."""
    m = _LIMIT.match((text or "").strip().lower().replace(" ", ""))
    period = _PERIODS.get(m.group("period") or "min") if m else None
    if period is None:
        raise ValueError(f"invalid rate limit {text!r} (expected e.g. 30/min, 10/hour or 0)")
    count = int(m.group("n"))
    return (count, period) if count else None


def limits_from_env() -> dict[str, tuple[int, float]]:
    """The active groups and their (count, period); an invalid value keeps the default."""
    if os.environ.get("RATE_LIMIT", "").strip() == "0":
        return {}
    active = {}
    for group, (var, default) in GROUPS.items():
        try:
            limit = parse_limit(os.environ.get(var, default))
        except ValueError as exc:
            print(f"[AICortex] WARNING: {var}: {exc}; using the default {default}")
            limit = parse_limit(default)
        if limit:
            active[group] = limit
    return active


def describe(limits: dict[str, tuple[int, float]]) -> str:
    """Human-readable summary for the startup log: 'auth 30/min, register 10/hour'."""
    names = {1.0: "s", 60.0: "min", 3600.0: "hour", 86400.0: "day"}
    return ", ".join(f"{g} {n}/{names[p]}" for g, (n, p) in limits.items()) or "off"


def group_of(path: str) -> str | None:
    """The rate-limit group a request path belongs to, or None."""
    path = path.rstrip("/") or "/"
    if path in _EXACT:
        return _EXACT[path]
    return next((g for prefix, g in _PREFIXES if path.startswith(prefix)), None)


def client_key(scope) -> str:
    """The client address to count against: IPv4 as is, IPv6 by its /64."""
    host = (scope.get("client") or ("unknown",))[0] or "unknown"
    try:
        ip = ipaddress.ip_address(host)
    except ValueError:
        return host
    if ip.version == 6:
        if ip.ipv4_mapped:
            return str(ip.ipv4_mapped)
        return str(ipaddress.ip_network(f"{ip}/64", strict=False))
    return str(ip)


class Buckets:
    """Token buckets for one group: ``take`` returns 0 when the request may pass,
    else the seconds until the next token."""

    def __init__(self, limit: tuple[int, float], max_clients: int = MAX_CLIENTS):
        self.capacity, period = limit
        self.rate = self.capacity / period          # tokens per second
        self.max_clients = max_clients
        self._state: OrderedDict[str, tuple[float, float]] = OrderedDict()

    def __len__(self) -> int:
        return len(self._state)

    def take(self, key: str, now: float) -> float:
        old = self._state.pop(key, (float(self.capacity), now))
        tokens = min(float(self.capacity), old[0] + (now - old[1]) * self.rate)
        allowed = tokens >= 1 - _EPS
        # A refused request leaves the bucket as it was (re-inserted as most recent).
        self._state[key] = (tokens - 1, now) if allowed else old
        while len(self._state) > self.max_clients:
            self._state.popitem(last=False)
        return 0.0 if allowed else (1 - tokens) / self.rate


class RateLimit:
    """ASGI middleware applying the limits from the environment (read once)."""

    def __init__(self, app, clock=time.monotonic):
        self.app = app
        self.clock = clock
        self.buckets = {g: Buckets(limit) for g, limit in limits_from_env().items()}

    async def __call__(self, scope, receive, send):
        if scope["type"] == "http" and self.buckets and scope.get("method") != "OPTIONS":
            buckets = self.buckets.get(group_of(scope.get("path", "")) or "")
            wait = buckets.take(client_key(scope), self.clock()) if buckets is not None else 0
            if wait:
                body = b"Too Many Requests"
                await send({"type": "http.response.start", "status": 429,
                            "headers": [(b"content-type", b"text/plain"),
                                        (b"content-length", str(len(body)).encode()),
                                        (b"retry-after", str(math.ceil(wait)).encode())]})
                await send({"type": "http.response.body", "body": body})
                return
        await self.app(scope, receive, send)
