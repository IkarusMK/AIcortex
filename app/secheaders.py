"""Security headers on every HTTP response.

- ``Strict-Transport-Security`` when BASE_URL is https: browsers then refuse to talk
  to this server over plain HTTP for a year, so a downgraded first request can't
  expose the WebUI session or the OAuth pages. The default covers this host only:
  includeSubDomains would pin every name below it to HTTPS too, and some of those
  may still be plain HTTP. HSTS=<value> sets another value (e.g. "max-age=63072000;
  includeSubDomains; preload"), HSTS=off switches it off. Browsers ignore the header
  on plain-HTTP responses, so LAN access by IP keeps working.
- ``X-Content-Type-Options: nosniff``: no response is ever content-sniffed into
  something executable.

A header the app already set is left alone, so nothing is sent twice. The "server:
uvicorn" banner is switched off where server.py configures uvicorn (it is added
below the ASGI layer, out of reach of a middleware).
"""
import os
import re
import settings

DEFAULT_HSTS = "max-age=31536000"
_OFF = {"0", "off", "false", "no", "none"}
_HSTS_VALUE = re.compile(r"^max-age=\d{1,10}(\s*;\s*(includeSubDomains|preload))*$", re.I)


def hsts_value() -> str | None:
    """The Strict-Transport-Security value to send, or None."""
    if not os.environ.get("BASE_URL", "").strip().lower().startswith("https://"):
        return None
    setting = settings.knob("HSTS", None)
    if setting is None or not setting.strip():
        return DEFAULT_HSTS
    setting = setting.strip()
    if setting.lower() in _OFF:
        return None
    if not _HSTS_VALUE.match(setting):
        print(f"[AICortex] WARNING: HSTS={setting!r} is not a valid header value "
              f"(e.g. 'max-age=31536000; includeSubDomains' or 'off'); using the default")
        return DEFAULT_HSTS
    return setting


class SecurityHeaders:
    """ASGI middleware adding the headers above where the response lacks them."""

    def __init__(self, app):
        self.app = app
        hsts = hsts_value()
        self.headers = tuple(
            [(b"strict-transport-security", hsts.encode("ascii"))] if hsts else []
        ) + ((b"x-content-type-options", b"nosniff"),)

    async def __call__(self, scope, receive, send):
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return

        async def send_with_headers(message):
            if message["type"] == "http.response.start":
                present = {k.lower() for k, _ in message.get("headers") or []}
                missing = [(k, v) for k, v in self.headers if k not in present]
                message = {**message, "headers": [*(message.get("headers") or []), *missing]}
            await send(message)

        await self.app(scope, receive, send_with_headers)
