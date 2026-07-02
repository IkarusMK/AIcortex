"""CalDAV — calendars as DATA (Nextcloud, Radicale, iCloud-style servers).

The calendar counterpart of webdav_tools: register a CalDAV endpoint (its URL + an
app-password secret) once, then discover calendars, list events in a time range, and
add an event. Speaks CalDAV over HTTP (PROPFIND / REPORT calendar-query / PUT of an
iCalendar object) with httpx — no extra dependency.

A Nextcloud calendar home looks like:
    https://<host>/remote.php/dav/calendars/<user>/
and a single calendar like:
    https://<host>/remote.php/dav/calendars/<user>/personal/
Use a Nextcloud **app password**, not the login password. Only registered endpoints
are reachable and the host passes the SSRF guard.
"""
import json
import re
import uuid
import xml.etree.ElementTree as ET
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import unquote, urlparse

import cfgstore
import os
import httpx

import netguard
import secrets_store

CALDAV_DIR = Path(os.environ.get("CALDAV_DIR", "/data/caldav"))
_NS = {"d": "DAV:", "c": "urn:ietf:params:xml:ns:caldav"}


def _slug(name: str) -> str:
    s = re.sub(r"[^a-z0-9]+", "-", (name or "").lower()).strip("-")
    return s[:60] or "caldav"


def _cfg_path(name: str) -> Path:
    return CALDAV_DIR / f"{_slug(name)}.json"


def _load(name: str):
    p = _cfg_path(name)
    if not p.exists():
        return None
    try:
        return json.loads(p.read_text(encoding="utf-8"))
    except Exception:
        return None


def _client(cfg: dict):
    """httpx client for the endpoint (basic auth from the vault), after the SSRF
    check. Returns (client, base_url) or (None, error_string)."""
    base = cfg.get("base_url", "").rstrip("/") + "/"
    ok, reason = netguard.check_url(base)
    if not ok:
        return None, f"Blocked by network policy — {reason}"
    auth = None
    user = cfg.get("username")
    if user:
        pw_env = cfg.get("password_env")
        pw = secrets_store.get_secret(pw_env) if pw_env else ""
        if pw_env and not pw:
            return None, f"Endpoint needs secret '{pw_env}'. Store it with secret_set."
        auth = (user, pw or "")
    verify = netguard.tls_verify(cfg)
    return httpx.Client(auth=auth, verify=verify, timeout=60, follow_redirects=True), base


def _to_ical(dt: str) -> str:
    """ISO-8601 → iCalendar UTC stamp (YYYYMMDDTHHMMSSZ). Naive input is treated as
    UTC. Accepts a trailing 'Z'."""
    s = (dt or "").strip().replace("Z", "+00:00")
    d = datetime.fromisoformat(s)
    if d.tzinfo is None:
        d = d.replace(tzinfo=timezone.utc)
    return d.astimezone(timezone.utc).strftime("%Y%m%dT%H%M%SZ")


def _ics_field(ics: str, name: str) -> str:
    """First value of an iCal property (ignoring any ;params), unfolded."""
    ics = re.sub(r"\r\n[ \t]", "", ics)  # unfold long lines
    m = re.search(rf"(?im)^{name}(?:;[^:\r\n]*)?:(.+)$", ics)
    return m.group(1).strip() if m else ""


def register(mcp):
    @mcp.tool
    def caldav_add(name: str, base_url: str, username: str = "",
                   password_env: str = "", tls_insecure: bool = False,
                   ca_bundle: str = "", description: str = "") -> str:
        """Register/update a CalDAV endpoint as DATA (no redeploy). base_url = the
        calendar home (…/calendars/<user>/) or a single calendar (…/<user>/personal/).
        password_env = NAME of the secret holding the app password (store it with
        secret_set). TLS is verified by default; for a self-signed server set
        ca_bundle or tls_insecure=true (admin-only)."""
        try:
            CALDAV_DIR.mkdir(parents=True, exist_ok=True)
            cfg = {"name": name, "base_url": base_url.rstrip("/") + "/",
                   "username": username, "password_env": password_env,
                   "tls_insecure": bool(tls_insecure), "ca_bundle": ca_bundle,
                   "description": description}
            cfgstore.write_merged(_cfg_path(name), cfg)
            note = ""
            if password_env and not secrets_store.get_secret(password_env):
                note = f" — set the app password: secret_set('{password_env}', <value>)"
            return f"Registered CalDAV endpoint '{_slug(name)}'.{note}"
        except Exception as exc:
            return f"Could not register endpoint: {exc}"

    @mcp.tool
    def caldav_list_endpoints() -> str:
        """List registered CalDAV endpoints (name — base_url — description)."""
        if not CALDAV_DIR.exists() or not any(CALDAV_DIR.glob("*.json")):
            return "No CalDAV endpoints yet. Use caldav_add."
        out = []
        for p in sorted(CALDAV_DIR.glob("*.json")):
            try:
                c = json.loads(p.read_text(encoding="utf-8"))
                out.append(f"- {p.stem} — {c.get('base_url')} — {c.get('description', '')}")
            except Exception:
                out.append(f"- {p.stem} — (unreadable)")
        return "\n".join(out)

    @mcp.tool
    def caldav_delete_endpoint(name: str) -> str:
        """Remove a registered CalDAV endpoint by name (does NOT touch the server)."""
        p = _cfg_path(name)
        if p.exists():
            p.unlink()
            return f"Deleted CalDAV endpoint '{_slug(name)}'."
        return f"No CalDAV endpoint '{name}'."

    @mcp.tool
    def caldav_list_calendars(endpoint: str) -> str:
        """Discover the calendars under a registered endpoint (name · href). Use one of
        the hrefs as the `calendar` argument for events."""
        cfg = _load(endpoint)
        if not cfg:
            return f"Unknown endpoint '{endpoint}'. Use caldav_list_endpoints / caldav_add."
        client, base = _client(cfg)
        if client is None:
            return base
        body = ('<d:propfind xmlns:d="DAV:" xmlns:c="urn:ietf:params:xml:ns:caldav">'
                '<d:prop><d:displayname/><d:resourcetype/></d:prop></d:propfind>')
        try:
            with netguard.guard(urlparse(base).hostname or ""), client:
                r = client.request("PROPFIND", base, content=body,
                                   headers={"Depth": "1", "Content-Type": "application/xml"})
        except Exception as exc:
            return f"List failed: {exc}"
        if r.status_code not in (207, 200):
            return f"List returned HTTP {r.status_code}. Check base_url / credentials."
        out = []
        try:
            root = ET.fromstring(r.content)
            for resp in root.findall("d:response", _NS):
                is_cal = resp.find(".//c:calendar", _NS) is not None
                if not is_cal:
                    continue
                href = unquote(resp.findtext("d:href", default="", namespaces=_NS))
                dn = resp.findtext(".//d:displayname", default="", namespaces=_NS)
                out.append(f"- {dn or href.rstrip('/').split('/')[-1]} · {href}")
        except Exception as exc:
            return f"Could not parse calendars: {exc}"
        return "\n".join(out) if out else "(no calendars found under this endpoint)"

    @mcp.tool
    def caldav_list_events(endpoint: str, calendar: str = "", start: str = "",
                           end: str = "") -> str:
        """List events in a time range (summary · start · end · uid). `calendar` = a
        calendar href from caldav_list_calendars, or empty to use base_url directly.
        start/end = ISO-8601 (default: now → +30 days)."""
        cfg = _load(endpoint)
        if not cfg:
            return f"Unknown endpoint '{endpoint}'."
        client, base = _client(cfg)
        if client is None:
            return base
        url = calendar or base
        if not urlparse(url).scheme:  # a relative href from list_calendars
            url = f"{urlparse(base).scheme}://{urlparse(base).netloc}{url}"
        try:
            s = _to_ical(start) if start else datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
            e = _to_ical(end) if end else (datetime.now(timezone.utc).replace(microsecond=0)
                                           ).strftime("%Y%m%dT%H%M%SZ")
            if not end:
                from datetime import timedelta
                e = (datetime.now(timezone.utc) + timedelta(days=30)).strftime("%Y%m%dT%H%M%SZ")
        except Exception as exc:
            return f"Bad start/end (use ISO-8601): {exc}"
        body = ('<c:calendar-query xmlns:d="DAV:" xmlns:c="urn:ietf:params:xml:ns:caldav">'
                '<d:prop><c:calendar-data/></d:prop><c:filter>'
                '<c:comp-filter name="VCALENDAR"><c:comp-filter name="VEVENT">'
                f'<c:time-range start="{s}" end="{e}"/>'
                '</c:comp-filter></c:comp-filter></c:filter></c:calendar-query>')
        try:
            with netguard.guard(urlparse(url).hostname or ""), client:
                r = client.request("REPORT", url, content=body,
                                   headers={"Depth": "1", "Content-Type": "application/xml"})
        except Exception as exc:
            return f"Query failed: {exc}"
        if r.status_code not in (207, 200):
            return f"Query returned HTTP {r.status_code}. Check the calendar href."
        out = []
        try:
            root = ET.fromstring(r.content)
            for cd in root.findall(".//c:calendar-data", _NS):
                ics = cd.text or ""
                summary = _ics_field(ics, "SUMMARY") or "(no title)"
                out.append(f"- {summary} · {_ics_field(ics, 'DTSTART')} → "
                           f"{_ics_field(ics, 'DTEND')} · {_ics_field(ics, 'UID')}")
        except Exception as exc:
            return f"Could not parse events: {exc}"
        return "\n".join(out) if out else "(no events in that range)"

    @mcp.tool
    def caldav_add_event(endpoint: str, calendar: str, summary: str, start: str,
                         end: str, description: str = "") -> str:
        """Create an event (PUTs an iCalendar object). `calendar` = a calendar href
        (from caldav_list_calendars). start/end = ISO-8601. STATE-CHANGING — confirm
        with the user first."""
        cfg = _load(endpoint)
        if not cfg:
            return f"Unknown endpoint '{endpoint}'."
        client, base = _client(cfg)
        if client is None:
            return base
        cal = calendar or base
        if not urlparse(cal).scheme:
            cal = f"{urlparse(base).scheme}://{urlparse(base).netloc}{cal}"
        try:
            dtstart, dtend = _to_ical(start), _to_ical(end)
        except Exception as exc:
            return f"Bad start/end (use ISO-8601): {exc}"
        uid = f"{uuid.uuid4()}@aicortex"
        stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
        desc = (description or "").replace("\n", "\\n").replace(",", "\\,")
        ics = ("BEGIN:VCALENDAR\r\nVERSION:2.0\r\nPRODID:-//AICortex//EN\r\n"
               f"BEGIN:VEVENT\r\nUID:{uid}\r\nDTSTAMP:{stamp}\r\n"
               f"DTSTART:{dtstart}\r\nDTEND:{dtend}\r\n"
               f"SUMMARY:{summary}\r\n" + (f"DESCRIPTION:{desc}\r\n" if desc else "") +
               "END:VEVENT\r\nEND:VCALENDAR\r\n")
        url = cal.rstrip("/") + f"/{uid}.ics"
        try:
            with netguard.guard(urlparse(url).hostname or ""), client:
                r = client.put(url, content=ics.encode("utf-8"),
                               headers={"Content-Type": "text/calendar; charset=utf-8"})
        except Exception as exc:
            return f"Create failed: {exc}"
        if r.status_code in (200, 201, 204):
            return f"Created event '{summary}' ({dtstart} → {dtend})."
        return f"Create returned HTTP {r.status_code} (check the calendar href / credentials)."
