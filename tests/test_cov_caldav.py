"""CalDAV tools: registration, vault-resolved credentials, SSRF guard, payloads.

Proves, with no real network or DNS (httpx.Client and socket.getaddrinfo are
faked; netguard still checks the fake resolution): registration stores only the
secret NAME; the app password comes from the vault, reaches the client as basic
auth and never shows in output or on disk; private targets are refused before a
client exists and the connect-time guard rejects DNS rebinding; PROPFIND / REPORT
/ PUT / DELETE carry the right method, URL, headers and iCalendar body; foreign
hosts never receive the credentials, titles cannot inject iCalendar properties.
"""
import asyncio
import collections
import re
import socket
import sys
from datetime import datetime, timezone
from types import SimpleNamespace
from urllib.parse import urlparse

import pytest
from cryptography.fernet import Fernet
from fastmcp import Client

PASSWORD = "test-password"
HOST = "dav.example.com"
BASE = f"https://{HOST}/remote.php/dav/calendars/alice/"
NS = 'xmlns:d="DAV:" xmlns:c="urn:ietf:params:xml:ns:caldav"'
Call = collections.namedtuple("Call", "method url headers body")
Reply = collections.namedtuple("Reply", "status_code content")


class _Resolver:
    """Fake socket.getaddrinfo: host -> rounds of IPs (the last round repeats)."""

    def __init__(self):
        self.table = {}

    def set(self, host, *rounds):
        self.table[host] = [[r] if isinstance(r, str) else list(r) for r in rounds]

    def __call__(self, host, *args, **kwargs):
        if host not in self.table:
            raise socket.gaierror(f"fake DNS: no record for {host}")
        rounds = self.table[host]
        ips = rounds.pop(0) if len(rounds) > 1 else rounds[0]
        return [(socket.AF_INET, socket.SOCK_STREAM, 6, "", (ip, 0)) for ip in ips]


class _Hub:
    """Stands in for httpx and is its own Client: records kwargs, replays one answer."""

    def __init__(self):
        self.clients, self.calls, self.error, self.closed = [], [], None, 0
        self.answer = Reply(207, b"")

    def reply(self, status, body=b""):
        self.answer = Reply(status, body.encode() if isinstance(body, str) else body)

    def build(self, **kw):
        self.clients.append(kw)
        return self

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        self.closed += 1

    def request(self, method, url, content=None, headers=None):
        socket.getaddrinfo(urlparse(url).hostname, None)  # connect-time DNS (guarded)
        self.calls.append(Call(method, url, dict(headers or {}), content))
        if self.error:
            raise self.error
        return self.answer

    def put(self, url, content=None, headers=None):
        return self.request("PUT", url, content, headers)


class _Env:
    def __init__(self, server, dns, http, mod):
        self.server, self.dns, self.http, self.mod = server, dns, http, mod

    def call(self, tool, **args):
        async def go():
            async with Client(self.server.mcp) as client:
                res = await client.call_tool(tool, args, raise_on_error=False)
                return res.content[0].text if res.content else ""
        return asyncio.run(go())

    def add(self, name="home", **over):
        args = {"name": name, "base_url": BASE, "username": "alice", "password_env": "CAL_PW"}
        return self.call("caldav_add", **{**args, **over})


@pytest.fixture
def env(fresh_server, monkeypatch):
    monkeypatch.setenv("STORAGE_ENCRYPTION_KEY", Fernet.generate_key().decode())
    monkeypatch.setenv("INTERNAL_ALLOW_CIDRS", "203.0.113.0/24")
    dns, hub, mod = _Resolver(), _Hub(), sys.modules["caldav_tools"]
    dns.set(HOST, "203.0.113.10")
    monkeypatch.setattr(socket, "getaddrinfo", dns)
    monkeypatch.setattr(mod, "httpx", SimpleNamespace(Client=hub.build))
    # the hosts these tests register — bound by a person, as only a person binds a secret (2.6.0)
    sys.modules["secrets_store"].vault_set("CAL_PW", PASSWORD, hosts=[HOST, "lan.example.net", "odd.example.net", "rebind.example.net"])
    return _Env(fresh_server, dns, hub, mod)


@pytest.fixture
def home(env):
    env.add()
    return env


def _multistatus(*responses):
    return f'<?xml version="1.0"?><d:multistatus {NS}>{"".join(responses)}</d:multistatus>'


def _cal(href, name="", is_calendar=True):
    kind = "<d:collection/><c:calendar/>" if is_calendar else "<d:collection/>"
    dn = f"<d:displayname>{name}</d:displayname>" if name else "<d:displayname/>"
    return (f"<d:response><d:href>{href}</d:href><d:propstat><d:prop>{dn}"
            f"<d:resourcetype>{kind}</d:resourcetype></d:prop></d:propstat></d:response>")


def _event(href, ics):
    return (f"<d:response><d:href>{href}</d:href><d:propstat><d:prop>"
            f"<c:calendar-data>{ics}</c:calendar-data></d:prop></d:propstat></d:response>")


ICS = ("BEGIN:VCALENDAR\nBEGIN:VEVENT\nUID:1@x\n{}DTSTART:20260301T100000Z\n"
       "DTEND:20260301T110000Z\nEND:VEVENT\nEND:VCALENDAR")
WHEN = {"start": "2026-03-02T09:00:00Z", "end": "2026-03-02T10:00:00Z"}
ALL_TOOLS = [("caldav_list_calendars", {}), ("caldav_list_events", {}),
             ("caldav_add_event", {"calendar": "/c/", "summary": "s", **WHEN}),
             ("caldav_delete_event", {"event": "/c/e.ics"})]


# --- registration ---------------------------------------------------------
def test_add_normalises_slug_and_url_and_stores_only_the_secret_name(env):
    out = env.add("Family Cal!", base_url="https://dav.example.com/dav/alice")
    assert out == "Registered CalDAV endpoint 'family-cal'."  # secret exists: no hint
    assert env.add("!!!", password_env="UNSET_PW").startswith("Registered CalDAV endpoint 'caldav'.")
    assert "secret_set('UNSET_PW', <value>, hosts='dav.example.com')" in env.add(password_env="UNSET_PW")  # secret missing: store it with its host
    text = (env.mod.CALDAV_DIR / "family-cal.json").read_text()
    assert '"base_url": "https://dav.example.com/dav/alice/"' in text
    assert '"password_env": "CAL_PW"' in text and PASSWORD not in text


def test_add_reports_an_unusable_store_instead_of_raising(env, tmp_path, monkeypatch):
    blocker = tmp_path / "not-a-dir"
    blocker.write_text("x")
    monkeypatch.setattr(env.mod, "CALDAV_DIR", blocker)
    assert env.add().startswith("Could not register endpoint:")


def test_list_and_delete_endpoints_incl_unreadable_config(env):
    assert env.call("caldav_list_endpoints") == "No CalDAV endpoints yet. Use caldav_add."
    env.add(description="mine")
    (env.mod.CALDAV_DIR / "broken.json").write_text("{not json")
    assert env.call("caldav_list_endpoints").splitlines() == [
        "- broken — (unreadable)", f"- home — {BASE} — mine"]
    assert env.call("caldav_list_calendars", endpoint="broken").startswith("Unknown endpoint")
    assert env.call("caldav_delete_endpoint", name="Home") == "Deleted CalDAV endpoint 'home'."
    assert env.call("caldav_delete_endpoint", name="home") == "No CalDAV endpoint 'home'."
    assert env.http.calls == []  # the server itself is never touched


@pytest.mark.parametrize("tool,extra", ALL_TOOLS)
def test_unknown_endpoint_is_refused_for_every_tool(env, tool, extra):
    out = env.call(tool, endpoint="nope", **extra)
    assert out.startswith("Unknown endpoint 'nope'") and env.http.clients == []


# --- credentials and TLS ---------------------------------------------------
def test_password_is_read_from_vault_for_basic_auth_and_never_echoed(home):
    home.http.reply(207, _multistatus(_cal("/c/", "Personal")))
    out = home.call("caldav_list_calendars", endpoint="home")
    kw = home.http.clients[0]
    assert kw["auth"] == ("alice", PASSWORD) and kw["verify"] is True and kw["follow_redirects"]
    assert PASSWORD not in out + home.call("caldav_list_endpoints")


def test_missing_secret_stops_before_any_client_and_anonymous_endpoints_send_no_auth(env):
    env.add(password_env="UNSET_PW")
    assert env.call("caldav_list_calendars", endpoint="home") == (
        "Endpoint needs secret 'UNSET_PW'. Store it with secret_set.")
    assert env.http.clients == []
    env.add(username="", password_env="")
    env.call("caldav_list_calendars", endpoint="home")
    assert env.http.clients[0]["auth"] is None


@pytest.mark.parametrize("over,expected", [
    ({"tls_insecure": True}, False),
    ({"ca_bundle": "/certs/lan-ca.pem"}, "/certs/lan-ca.pem"),
    ({"tls_insecure": True, "ca_bundle": "/certs/lan-ca.pem"}, "/certs/lan-ca.pem"),
])
def test_tls_options_reach_the_client(env, over, expected):
    env.add(**over)
    env.call("caldav_list_calendars", endpoint="home")
    assert env.http.clients[0]["verify"] == expected


# --- SSRF guard ------------------------------------------------------------
@pytest.mark.parametrize("tool,extra", ALL_TOOLS)
def test_private_target_is_blocked_before_a_client_exists(env, tool, extra):
    env.dns.set("lan.example.net", "10.0.0.5")
    env.add(base_url="https://lan.example.net/dav/")
    out = env.call(tool, endpoint="home", **extra)
    assert out.startswith("Blocked by network policy — blocked: 10.0.0.5")
    assert env.http.clients == [] and env.http.calls == []


@pytest.mark.parametrize("ips,fragment", [("169.254.169.254", "169.254.169.254"), (None, "cannot resolve host")])
def test_metadata_and_unresolvable_hosts_stay_blocked(env, monkeypatch, ips, fragment):
    monkeypatch.setenv("INTERNAL_ALLOW_CIDRS", "10.0.0.0/8")  # a LAN allow-list must not open these
    ips and env.dns.set("odd.example.net", ips)
    env.add(base_url="https://odd.example.net/")
    out = env.call("caldav_list_calendars", endpoint="home")
    assert out.startswith("Blocked by network policy") and fragment in out


def test_connect_time_guard_rejects_a_dns_rebinding_answer(env):
    env.dns.set("rebind.example.net", "203.0.113.20", "10.0.0.9")  # check, then connect
    env.add(base_url="https://rebind.example.net/dav/")
    out = env.call("caldav_list_calendars", endpoint="home")
    assert out.startswith("List failed:") and "internal/blocked at connect" in out
    assert env.http.calls == []


# --- list calendars ---------------------------------------------------------
def test_list_calendars_sends_propfind_and_shows_only_calendar_collections(home):
    home.http.reply(207, _multistatus(
        _cal("/remote.php/dav/calendars/alice/", is_calendar=False),
        _cal("/remote.php/dav/calendars/alice/personal/", "Personal"),
        _cal("/remote.php/dav/calendars/alice/team%20plan/")))
    out = home.call("caldav_list_calendars", endpoint="home")
    call = home.http.calls[0]
    assert (call.method, call.url, call.headers["Depth"]) == ("PROPFIND", BASE, "1")
    assert "<d:resourcetype/>" in call.body and "displayname" in call.body
    assert out.splitlines() == ["- Personal · /remote.php/dav/calendars/alice/personal/",
                                "- team plan · /remote.php/dav/calendars/alice/team plan/"]
    assert home.http.closed == 1


@pytest.mark.parametrize("status,body,expected", [
    (500, "", "List returned HTTP 500. Check base_url / credentials."),
    (207, _multistatus(), "(no calendars found under this endpoint)"),
])
def test_list_calendars_error_and_empty_answers(home, status, body, expected):
    home.http.reply(status, body)
    assert home.call("caldav_list_calendars", endpoint="home") == expected


@pytest.mark.parametrize("body", [
    "<d:multistatus xmlns:d='DAV:'><oops>",
    '<?xml version="1.0"?><!DOCTYPE x [<!ENTITY a "aaaa">]><d:multistatus xmlns:d="DAV:">&a;</d:multistatus>',
])
def test_list_calendars_survives_malformed_or_entity_expansion_xml(home, body):
    home.http.reply(207, body)
    assert home.call("caldav_list_calendars", endpoint="home").startswith("Could not parse calendars:")


def test_transport_errors_are_returned_not_raised(home):
    home.http.error = ConnectionError("connection refused")
    why = "ConnectionError (could not connect; details in the server log)"   # never the text
    assert home.call("caldav_list_calendars", endpoint="home") == f"List failed: {why}"
    assert home.call("caldav_list_events", endpoint="home") == f"Query failed: {why}"
    assert home.call("caldav_delete_event", endpoint="home", event="/c/e.ics") == (
        f"Delete failed: {why}")


# --- list events -------------------------------------------------------------
def test_list_events_builds_a_time_range_report_and_formats_hits(home):
    home.http.reply(207, _multistatus(
        _event("/c/personal/a.ics", ICS.format("SUMMARY:Dentist\n")),
        _event("/c/personal/b%20c.ics", ICS.format("")),
        "<d:response><d:href>/c/personal/empty.ics</d:href></d:response>"))
    out = home.call("caldav_list_events", endpoint="home", calendar="/c/personal/",
                    start="2026-03-01T00:00:00Z", end="2026-03-31T23:59:59+02:00")
    call = home.http.calls[0]
    assert (call.method, call.url) == ("REPORT", "https://dav.example.com/c/personal/")
    assert 'start="20260301T000000Z" end="20260331T215959Z"' in call.body
    assert out.splitlines() == [
        "- Dentist · 20260301T100000Z → 20260301T110000Z · /c/personal/a.ics",
        "- (no title) · 20260301T100000Z → 20260301T110000Z · /c/personal/b c.ics"]


def test_list_events_defaults_to_the_next_thirty_days_on_base_url(home):
    home.call("caldav_list_events", endpoint="home")
    call = home.http.calls[0]
    start, end = (datetime.strptime(v, "%Y%m%dT%H%M%SZ").replace(tzinfo=timezone.utc)
                  for v in re.search(r'start="(\w+)" end="(\w+)"', call.body).groups())
    assert call.url == BASE and 29.9 < (end - start).total_seconds() / 86400 < 30.1
    assert abs((datetime.now(timezone.utc) - start).total_seconds()) < 60  # starts now


def test_list_events_reports_bad_dates_empty_ranges_and_http_errors(home):
    assert home.call("caldav_list_events", endpoint="home", start="soon").startswith("Bad start/end (use ISO-8601):")
    assert home.http.calls == []
    home.http.reply(207, _multistatus())
    assert home.call("caldav_list_events", endpoint="home") == "(no events in that range)"
    home.http.reply(404)
    assert home.call("caldav_list_events", endpoint="home") == "Query returned HTTP 404. Check the calendar href."
    home.http.reply(207, "<nope")
    assert home.call("caldav_list_events", endpoint="home").startswith("Could not parse events:")


def test_ical_helpers_convert_zones_and_read_parameterised_fields(env):
    to_ical, field = env.mod._to_ical, env.mod._ics_field
    assert to_ical("2026-03-01T10:00:00") == "20260301T100000Z"       # naive = UTC
    assert to_ical("2026-03-01T10:00:00+02:00") == "20260301T080000Z"  # offset -> UTC
    assert to_ical(" 2026-03-01T10:00:00Z ") == "20260301T100000Z"
    with pytest.raises(ValueError):
        to_ical("")
    ics = "DTSTART;TZID=Europe/Berlin:20260301T100000\r\nSUMMARY:Long\r\n  title\r\n"
    assert field(ics, "DTSTART") == "20260301T100000" and field(ics, "SUMMARY") == "Long title"
    assert field(ics, "LOCATION") == ""


# --- add / delete event --------------------------------------------------------
def test_add_event_puts_a_valid_icalendar_object(home):
    home.http.reply(201)
    out = home.call("caldav_add_event", endpoint="home", calendar="/remote.php/dav/calendars/alice/work/",
                    summary="Standup", start="2026-03-02T09:00:00+01:00", end="2026-03-02T09:15:00+01:00",
                    description="Bring notes, slides\nsecond line")
    call = home.http.calls[0]
    assert out == "Created event 'Standup' (20260302T080000Z → 20260302T081500Z)."
    assert call.method == "PUT"
    assert re.fullmatch(r"https://dav\.example\.com/remote\.php/dav/calendars/alice/work/[0-9a-f-]{36}@aicortex\.ics",
                        call.url)
    assert call.headers["Content-Type"] == "text/calendar; charset=utf-8"
    lines = call.body.decode().split("\r\n")
    assert {"BEGIN:VCALENDAR", "VERSION:2.0", "DTSTART:20260302T080000Z", "DTEND:20260302T081500Z", "SUMMARY:Standup",
            "DESCRIPTION:Bring notes\\, slides\\nsecond line"} <= set(lines)
    assert lines[-3:] == ["END:VEVENT", "END:VCALENDAR", ""]
    assert call.url.rsplit("/", 1)[1] == next(x for x in lines if x.startswith("UID:"))[4:] + ".ics"
    home.http.reply(204)  # no description: the property is omitted; no calendar: base_url is used
    home.call("caldav_add_event", endpoint="home", calendar="", summary="x", **WHEN)
    assert home.http.calls[1].url.startswith(BASE) and b"DESCRIPTION" not in home.http.calls[1].body


def test_add_event_refusal_and_failure_messages(home):
    args = dict(endpoint="home", calendar="/c/", summary="x", **WHEN)
    home.http.reply(403)
    assert home.call("caldav_add_event", **args) == "Create returned HTTP 403 (check the calendar href / credentials)."
    assert home.call("caldav_add_event", **{**args, "end": "tomorrow"}).startswith("Bad start/end")
    home.http.error = OSError("disk on fire")
    assert home.call("caldav_add_event", **args) == "Create failed: OSError (details in the server log)"


def test_delete_event_makes_hrefs_absolute_and_maps_statuses(home):
    assert home.call("caldav_delete_event", endpoint="home", event="  ") == (
        "Provide the event href (from caldav_list_events).")
    assert home.http.clients == []
    home.http.reply(204)
    out = home.call("caldav_delete_event", endpoint="home", event=" /c/personal/a%20b.ics ")
    assert (home.http.calls[0].method, home.http.calls[0].url) == (
        "DELETE", "https://dav.example.com/c/personal/a%20b.ics") and out == "Deleted event (204)."
    home.http.reply(404)  # already gone counts as deleted
    assert home.call("caldav_delete_event", endpoint="home", event="/c/e.ics") == "Deleted event (404)."
    home.http.reply(423)
    assert home.call("caldav_delete_event", endpoint="home", event="/c/e.ics") == (
        "Delete returned HTTP 423 (check the href / credentials).")


# --- regressions: credentials stay on the endpoint host, TEXT escaping, unfolding ---
OTHER = "https://collector.example.net/steal/"


@pytest.mark.parametrize("tool,extra", [
    ("caldav_list_events", {"calendar": OTHER}),
    ("caldav_add_event", {"calendar": OTHER, "summary": "x", **WHEN}),
    ("caldav_delete_event", {"event": OTHER + "e.ics"}),
])
def test_requests_never_leave_the_registered_host_with_credentials(home, tool, extra):
    home.dns.set("collector.example.net", "203.0.113.99")
    out = home.call(tool, endpoint="home", **extra)
    assert out.startswith("Refused: https://collector.example.net is not this endpoint's server")
    assert home.http.calls == [] and PASSWORD not in out


def test_summary_is_escaped_as_one_line_so_it_cannot_inject_properties(home):
    home.http.reply(201)
    home.call("caldav_add_event", endpoint="home", calendar="/c/", **WHEN,
              summary="Lunch\r\nATTENDEE:mailto:bob@example.com")
    lines = home.http.calls[0].body.decode().split("\r\n")
    assert not any(line.startswith("ATTENDEE") for line in lines)
    assert "SUMMARY:Lunch\\nATTENDEE:mailto:bob@example.com" in lines


def test_folded_summary_from_a_real_server_answer_is_shown_in_full(home):
    ics = "BEGIN:VEVENT\r\nSUMMARY:Quarterly planning \r\n review\r\nDTSTART:20260301T100000Z\r\nEND:VEVENT"
    home.http.reply(207, _multistatus(_event("/c/a.ics", ics)))
    assert "Quarterly planning review" in home.call("caldav_list_events", endpoint="home")
