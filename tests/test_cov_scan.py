"""eSCL scan tools (scan_tools): registration, transport selection, TLS options,
the SSRF guard, page draining, saving into the work dir and the Paperless push.

httpx.post / httpx.get / time.sleep and DNS are replaced by scripted fakes (no
network, no waiting). Proves: the scanner passes netguard before AND at connect
time (every request runs inside the guard); TLS is verified unless tls_insecure /
ca_bundle say otherwise and a certificate failure produces a re-register hint;
transports are tried in order; 503 warm-ups are retried within bounds; saved names
are sanitised and stay in the work dir; the Paperless token comes from the vault
by NAME and never appears in any result.
"""
import asyncio
import json
import socket
import ssl
import sys

import httpx
import pytest
from cryptography.fernet import Fernet
from fastmcp import Client

LAN = "192.0.2.0/24,198.51.100.0/24,203.0.113.0/24"


def _call(server, tool, args=None):
    async def go():
        async with Client(server.mcp) as client:
            res = await client.call_tool(tool, args or {}, raise_on_error=False)
            return res.content[0].text if res.content else ""
    return asyncio.run(go())


def _fake_dns(monkeypatch, table):
    def resolver(host, port=None, *args, **kwargs):
        if host not in table:
            raise socket.gaierror(-2, "Name or service not known")
        return [(socket.AF_INET, socket.SOCK_STREAM, 6, "", (ip, port or 0)) for ip in table[host]]
    monkeypatch.setattr(socket, "getaddrinfo", resolver)


class _Resp:
    def __init__(self, status, content=b"", headers=None):
        self.status_code, self.content, self.headers = status, content, headers or {}


class _World:
    def __init__(self):
        self.posts, self.gets, self.sleeps, self.guarded = [], [], [], []
        self.job_script = []   # per ScanJobs POST: _Resp or Exception (last one repeats)
        self.page_script = []  # per NextDocument GET: _Resp or Exception
        self.upload_script = []  # per Paperless POST
        # eSCL device queries, kept apart from the page GETs so page counts stay exact.
        # Default: the device answers neither (404), like a scanner without these pages.
        self.meta = []         # every ScannerCapabilities / ScannerStatus GET
        self.caps_script = [_Resp(404)]
        self.status_script = [_Resp(404)]

    def _next(self, script, index):
        item = script[min(index, len(script) - 1)]
        if isinstance(item, Exception):
            raise item
        return item


def _job(location="http://scan.example.com:80/eSCL/ScanJobs/1"):
    return _Resp(201, headers={"Location": location})


@pytest.fixture
def world(fresh_server, monkeypatch):
    w = _World()

    def post(url, **kw):
        w.guarded.append(socket.getaddrinfo.__name__ == "_filtered_getaddrinfo")
        w.posts.append({"url": url, **kw})
        if url.endswith("/ScanJobs"):
            return w._next(w.job_script, sum(p["url"].endswith("/ScanJobs") for p in w.posts) - 1)
        return w._next(w.upload_script, sum(not p["url"].endswith("/ScanJobs") for p in w.posts) - 1)

    def get(url, **kw):
        w.guarded.append(socket.getaddrinfo.__name__ == "_filtered_getaddrinfo")
        for suffix, script in (("/ScannerCapabilities", w.caps_script),
                               ("/ScannerStatus", w.status_script)):
            if url.endswith(suffix):
                w.meta.append({"url": url, **kw})
                return w._next(script, sum(m["url"].endswith(suffix) for m in w.meta) - 1)
        w.gets.append({"url": url, **kw})
        return w._next(w.page_script, len(w.gets) - 1)

    monkeypatch.setattr(httpx, "post", post)
    monkeypatch.setattr(httpx, "get", get)
    monkeypatch.setattr(sys.modules["scan_tools"].time, "sleep", lambda s: w.sleeps.append(s))
    w.job_script, w.page_script = [_job()], [_Resp(200, b"PAGE1"), _Resp(404)]
    return w


@pytest.fixture
def srv(fresh_server, monkeypatch, world):
    monkeypatch.setenv("STORAGE_ENCRYPTION_KEY", Fernet.generate_key().decode())
    monkeypatch.setenv("INTERNAL_ALLOW_CIDRS", LAN)
    _fake_dns(monkeypatch, {"scan.example.com": ["192.0.2.60"], "lan.example.net": ["10.0.0.60"],
                            "paperless.example.com": ["203.0.113.70"], "docs.example.net": ["10.0.0.70"]})
    return fresh_server


def _add(srv, name="flatbed", **extra):
    return _call(srv, "scan_add", {"name": name, "host": "scan.example.com", **extra})


def _scan(srv, **extra):
    return _call(srv, "scan_document", {"scanner": "flatbed", **extra})


def _service(tmp_path, base_url="https://paperless.example.com", token_env="PAPERLESS_TOKEN"):
    d = tmp_path / "services_dir"
    d.mkdir(exist_ok=True)
    (d / "paperless.json").write_text(json.dumps({"base_url": base_url, "token_env": token_env}))


def test_scan_add_reports_the_tls_mode_and_stores_options(srv, tmp_path):
    assert "(scan.example.com, base /eSCL, verified TLS)" in _add(srv)
    assert "verify OFF" in _add(srv, name="Self Signed", tls_insecure=True, port=443, tls="https")
    assert "CA bundle" in _add(srv, name="pinned", ca_bundle="/data/ca.pem", base="/scan")
    cfg = json.loads((tmp_path / "scan_dir" / "self-signed.json").read_text())
    assert cfg["tls_insecure"] is True and cfg["port"] == 443 and cfg["tls"] == "https"
    _add(srv, name="Self Signed", description="hall")  # update keeps unnamed fields
    cfg = json.loads((tmp_path / "scan_dir" / "self-signed.json").read_text())
    assert cfg["description"] == "hall" and cfg["tls_insecure"] is True


def test_scan_list_delete_and_storage_failure(srv, tmp_path, monkeypatch):
    assert "No scanners configured yet" in _call(srv, "scan_list")
    _add(srv, name="a", description="hall")
    (tmp_path / "scan_dir" / "broken.json").write_text("{nope")
    listing = _call(srv, "scan_list")
    assert "- a — scan.example.com — hall" in listing and "- broken — (unreadable config)" in listing
    assert "Deleted scanner 'a'" in _call(srv, "scan_delete", {"name": "a"})
    assert "No scanner 'a'" in _call(srv, "scan_delete", {"name": "a"})

    def boom(*a, **k):
        raise OSError("read-only")
    monkeypatch.setattr(sys.modules["scan_tools"].cfgstore, "write_merged", boom)
    assert "Could not register scanner: read-only" in _add(srv)


@pytest.mark.parametrize("cfg,expected", [
    ({}, [("https", 443, "/eSCL"), ("http", 80, "/eSCL")]),
    ({"port": 8443, "tls": "https"}, [("https", 8443, "/eSCL")]),
    ({"port": 8443, "tls": True}, [("https", 8443, "/eSCL")]),
    ({"port": 8080, "tls": "none", "base": "/scan/"}, [("http", 8080, "/scan")]),
    ({"port": 8080, "tls": False}, [("http", 8080, "/eSCL")]),
    ({"port": 631, "tls": "auto"}, [("https", 631, "/eSCL"), ("http", 631, "/eSCL")]),
])
def test_transport_candidates(srv, cfg, expected):
    assert sys.modules["scan_tools"]._candidates(cfg) == expected


def test_tls_error_detection_follows_the_cause_chain_and_survives_cycles(srv):
    looks = sys.modules["scan_tools"]._looks_like_tls_error
    assert looks(ssl.SSLCertVerificationError("bad"))
    try:
        try:
            raise ssl.SSLError("handshake")
        except ssl.SSLError as inner:
            raise RuntimeError("wrapped") from inner
    except RuntimeError as outer:
        assert looks(outer)
    a, b = RuntimeError("a"), RuntimeError("b")
    a.__cause__, b.__cause__ = b, a
    assert not looks(a) and not looks(ConnectionRefusedError("refused"))


def test_settings_xml_maps_friendly_values_and_falls_back_to_safe_defaults(srv, world):
    _add(srv)
    _scan(srv, color="GRAY", source="adf", format="jpeg", resolution=600)
    xml = world.posts[0]["content"].decode()
    for token in ("Grayscale8", "Feeder", "image/jpeg", "<scan:XResolution>600<", "<scan:YResolution>600<"):
        assert token in xml
    _scan(srv, color="sepia", source="tray", format="tiff")
    xml = world.posts[1]["content"].decode()
    assert "RGB24" in xml and "Platen" in xml and "application/pdf" in xml
    assert world.posts[0]["headers"] == {"Content-Type": "text/xml"}


def test_unknown_or_corrupt_scanner_and_private_address(srv, world, tmp_path, monkeypatch):
    assert "Unknown scanner 'flatbed'" in _scan(srv)
    (tmp_path / "scan_dir").mkdir()
    (tmp_path / "scan_dir" / "bad.json").write_text("{nope")
    assert "Unknown scanner 'bad'" in _call(srv, "scan_document", {"scanner": "bad"})
    _call(srv, "scan_add", {"name": "flatbed", "host": "lan.example.net"})
    out = _scan(srv)
    assert out.startswith("Blocked by network policy") and "10.0.0.60" in out and world.posts == []
    monkeypatch.setenv("INTERNAL_ALLOW_CIDRS", "10.0.0.0/24")
    assert _scan(srv).startswith("Scanned ")


def test_https_then_http_fallback_rebuilds_a_relative_job_location(srv, world, tmp_path):
    _add(srv)
    world.job_script = [httpx.ConnectError("connection refused"), _job("/eSCL/ScanJobs/9")]
    out = _scan(srv, filename="doc.pdf")
    assert [p["url"] for p in world.posts] == ["https://scan.example.com:443/eSCL/ScanJobs",
                                               "http://scan.example.com:80/eSCL/ScanJobs"]
    assert world.gets[0]["url"] == "http://scan.example.com:80/eSCL/ScanJobs/9/NextDocument"
    assert out == ("Scanned 5 bytes, 1 page(s) → /data/work/doc.pdf. Source: glass (chosen "
                   "automatically — the scanner did not report its capabilities).")
    assert (tmp_path / "work_dir" / "doc.pdf").read_bytes() == b"PAGE1"
    assert all(world.guarded)  # every request ran inside netguard.guard()


@pytest.mark.parametrize("extra,expected_verify", [({}, True), ({"tls_insecure": True}, False),
                                                   ({"ca_bundle": "/data/ca.pem"}, "/data/ca.pem")])
def test_tls_verification_follows_the_scanner_config(srv, world, extra, expected_verify):
    _add(srv, **extra)
    _scan(srv)
    assert world.posts[0]["verify"] == expected_verify and world.gets[0]["verify"] == expected_verify


def test_certificate_failure_hints_at_a_self_signed_scanner(srv, world):
    _add(srv)
    world.job_script = [httpx.ConnectError("[SSL: CERTIFICATE_VERIFY_FAILED] certificate verify failed"),
                        _Resp(503)]
    out = _scan(srv)
    assert "Tried: https:443 → TLS/certificate verification failed; http:80 → HTTP 503." in out
    assert "scan_add('flatbed', 'scan.example.com', tls_insecure=true)" in out


def test_other_failures_get_the_generic_hint_even_when_tls_is_already_relaxed(srv, world):
    _add(srv, tls_insecure=True)
    world.job_script = [httpx.ConnectError("[SSL: CERTIFICATE_VERIFY_FAILED] certificate verify failed"),
                        _Resp(200)]  # 200 without a Location header is not a job
    out = _scan(srv)
    assert "TLS/certificate verification failed; http:80 → HTTP 200" in out
    assert "Check the IP/base" in out and "tls_insecure=true)" not in out


def test_connect_time_guard_blocks_a_rebound_address(srv, world, monkeypatch):
    _add(srv, port=80, tls="http")
    answers = iter([["192.0.2.60"], ["10.0.0.9"]])
    monkeypatch.setattr(socket, "getaddrinfo", lambda host, port=None, *a, **k: [
        (socket.AF_INET, socket.SOCK_STREAM, 6, "", (ip, 0)) for ip in next(answers)])

    def post(url, **kw):  # the real connect resolves again inside the guard
        socket.getaddrinfo("scan.example.com", 80)
    monkeypatch.setattr(httpx, "post", post)
    out = _scan(srv)
    assert out.startswith("Scanner 'flatbed' did not start a job") and "netguard:" in out


def test_pages_are_drained_to_404_and_named_without_a_dot_extension(srv, world, tmp_path):
    _add(srv)
    world.page_script = [_Resp(200, b"A"), _Resp(200, b"BB"), _Resp(404)]
    out = _scan(srv, filename="my scan")
    work = tmp_path / "work_dir"
    assert out.startswith("Scanned 3 bytes, 2 page(s) → /data/work/my_scan, /data/work/my_scan-2. Source: ")
    assert (work / "my_scan").read_bytes() == b"A" and (work / "my_scan-2").read_bytes() == b"BB"
    assert len(world.gets) == 3


def test_default_name_uses_a_timestamp_and_the_format_extension(srv, world, tmp_path):
    _add(srv)
    _scan(srv, format="jpg")
    (name,) = [p.name for p in (tmp_path / "work_dir").iterdir()]
    assert name.startswith("scan-") and name.endswith(".jpg")


def test_filename_cannot_escape_the_work_dir(srv, world, tmp_path):
    _add(srv)
    _scan(srv, filename="../../etc/evil.pdf")
    assert [p.name for p in (tmp_path / "work_dir").iterdir()] == [".._.._etc_evil.pdf"]
    assert not (tmp_path / "etc").exists()


def test_warming_up_scanner_is_retried_with_short_pauses(srv, world):
    _add(srv)
    world.page_script = [_Resp(503), _Resp(200, b""), _Resp(200, b"OK"), _Resp(404)]
    assert _scan(srv).startswith("Scanned 2 bytes, 1 page(s)")
    assert world.sleeps == [2, 2]


def test_a_scanner_that_never_delivers_gives_up_after_bounded_retries(srv, world):
    _add(srv)
    world.page_script = [_Resp(503)]
    assert _scan(srv) == "Scan job created but no document came back (empty/timeout)."
    assert len(world.sleeps) == 15


def test_fetch_errors_fail_the_scan_only_when_no_page_arrived(srv, world, tmp_path):
    _add(srv)
    world.page_script = [httpx.ReadTimeout("timed out")]
    assert _scan(srv) == "Scan started but fetching the page failed: timed out"
    world.gets.clear()
    world.page_script = [_Resp(200, b"KEEP"), httpx.ReadTimeout("late")]
    assert _scan(srv, filename="k.pdf").startswith("Scanned 4 bytes, 1 page(s)")
    assert (tmp_path / "work_dir" / "k.pdf").read_bytes() == b"KEEP"


def test_paperless_push_uses_the_vault_token_and_never_returns_it(srv, world, tmp_path):
    sys.modules["secrets_store"].vault_set("PAPERLESS_TOKEN", "test-token")
    _service(tmp_path, base_url="https://paperless.example.com/")
    _add(srv)
    world.page_script = [_Resp(200, b"A"), _Resp(200, b"B"), _Resp(404)]
    world.upload_script = [_Resp(200)]
    out = _scan(srv, filename="d.pdf", paperless="paperless")
    assert out.endswith("Uploaded 2 page(s) to Paperless ('paperless').") and "test-token" not in out
    uploads = [p for p in world.posts if not p["url"].endswith("/ScanJobs")]
    assert [u["url"] for u in uploads] == ["https://paperless.example.com/api/documents/post_document/"] * 2
    assert uploads[0]["headers"] == {"Authorization": "Token test-token"}
    assert uploads[1]["files"]["document"] == ("d-2.pdf", b"B", "application/pdf")
    assert all(world.guarded)


def test_paperless_without_token_env_sends_no_authorization_header(srv, world, tmp_path):
    _service(tmp_path, token_env="")
    _add(srv)
    world.upload_script = [_Resp(201)]
    assert "Uploaded 1 page(s)" in _scan(srv, paperless="paperless")
    assert [p for p in world.posts if "post_document" in p["url"]][0]["headers"] == {}


@pytest.mark.parametrize("setup,expected", [
    ("missing", "(Paperless service 'paperless' not found — register it with service_add.)"),
    ("blocked", "(Paperless upload blocked by network policy — blocked: 10.0.0.70"),
    ("no-secret", "(Paperless needs secret 'PAPERLESS_TOKEN' — set it with secret_set.)"),
    ("http-error", "Paperless upload returned HTTP 500 for p.pdf."),
    ("exception", "Paperless upload failed: connection reset"),
])
def test_paperless_failures_keep_the_scan_and_explain_the_problem(srv, world, tmp_path, setup, expected):
    _add(srv)
    if setup not in ("missing", "no-secret"):
        sys.modules["secrets_store"].vault_set("PAPERLESS_TOKEN", "test-token")
    if setup != "missing":
        _service(tmp_path, base_url="https://docs.example.net" if setup == "blocked" else "https://paperless.example.com")
    world.upload_script = [_Resp(500)] if setup == "http-error" else [OSError("connection reset")]
    out = _scan(srv, filename="p.pdf", paperless="paperless")
    assert out.startswith("Scanned 5 bytes, 1 page(s) → /data/work/p.pdf.") and expected in out
    assert (tmp_path / "work_dir" / "p.pdf").read_bytes() == b"PAGE1"


# --- Choosing the source (glass / feeder) the vendor-neutral eSCL way -----------------
# Every eSCL device describes its sources in ScannerCapabilities (scan:Platen, scan:Adf
# with AdfSimplexInputCaps / AdfDuplexInputCaps) and reports the feeder in ScannerStatus
# (scan:AdfState). Prefixes differ between vendors, so the parser must go by local name.

_ESCL = "http://schemas.hp.com/imaging/escl/2011/05/03"
_PWG = "http://www.pwg.org/schemas/2010/12/sm"


def _caps(platen=True, adf=True, duplex=False, p="scan"):
    tag = (lambda name: f"{p}:{name}") if p else (lambda name: name)
    ns = f'xmlns:{p}="{_ESCL}"' if p else f'xmlns="{_ESCL}"'
    body = ""
    if platen:
        body += f"<{tag('Platen')}><{tag('PlatenInputCaps')}><{tag('MinWidth')}>16</{tag('MinWidth')}>" \
                f"</{tag('PlatenInputCaps')}></{tag('Platen')}>"
    if adf:
        body += f"<{tag('Adf')}><{tag('AdfSimplexInputCaps')}><{tag('MinWidth')}>16</{tag('MinWidth')}>" \
                f"</{tag('AdfSimplexInputCaps')}>"
        if duplex:
            body += f"<{tag('AdfDuplexInputCaps')}><{tag('MinWidth')}>16</{tag('MinWidth')}>" \
                    f"</{tag('AdfDuplexInputCaps')}>"
        body += f"</{tag('Adf')}>"
    return _Resp(200, (f'<?xml version="1.0" encoding="UTF-8"?><{tag("ScannerCapabilities")} {ns} '
                       f'xmlns:pwg="{_PWG}"><pwg:Version>2.63</pwg:Version>{body}'
                       f'</{tag("ScannerCapabilities")}>').encode())


def _status(adf_state=None, p="scan"):
    tag = (lambda name: f"{p}:{name}") if p else (lambda name: name)
    ns = f'xmlns:{p}="{_ESCL}"' if p else f'xmlns="{_ESCL}"'
    adf = f"<{tag('AdfState')}>{adf_state}</{tag('AdfState')}>" if adf_state else ""
    return _Resp(200, (f'<?xml version="1.0" encoding="UTF-8"?><{tag("ScannerStatus")} {ns} '
                       f'xmlns:pwg="{_PWG}"><pwg:Version>2.63</pwg:Version><pwg:State>Idle</pwg:State>'
                       f'{adf}</{tag("ScannerStatus")}>').encode())


def _source_sent(world):
    xml = [p for p in world.posts if p["url"].endswith("/ScanJobs")][0]["content"].decode()
    return xml


def test_auto_uses_the_feeder_when_paper_is_loaded(srv, world):
    _add(srv, ca_bundle="/data/ca.pem")
    world.caps_script, world.status_script = [_caps()], [_status("ScannerAdfLoaded")]
    world.page_script = [_Resp(200, b"P1"), _Resp(200, b"P2"), _Resp(404)]
    out = _scan(srv, filename="stack.pdf")
    assert "<pwg:InputSource>Feeder</pwg:InputSource>" in _source_sent(world)
    assert out.startswith("Scanned 4 bytes, 2 page(s) → /data/work/stack.pdf, /data/work/stack-2.pdf.")
    assert out.endswith("Source: feeder (chosen automatically — paper in the feeder).")
    assert [m["url"] for m in world.meta] == ["https://scan.example.com:443/eSCL/ScannerCapabilities",
                                              "https://scan.example.com:443/eSCL/ScannerStatus"]
    assert all(m["verify"] == "/data/ca.pem" for m in world.meta)  # same TLS rules as the scan
    assert all(world.guarded)                                      # and the same egress guard


def test_auto_uses_the_glass_when_the_feeder_is_empty(srv, world):
    _add(srv)
    world.caps_script, world.status_script = [_caps()], [_status("ScannerAdfEmpty")]
    out = _scan(srv)
    assert "<pwg:InputSource>Platen</pwg:InputSource>" in _source_sent(world)
    assert out.endswith("Source: glass (chosen automatically — the feeder is empty).")


def test_a_glass_only_scanner_never_asks_about_a_feeder(srv, world):
    _add(srv)
    world.caps_script = [_caps(adf=False)]
    out = _scan(srv)
    assert "<pwg:InputSource>Platen</pwg:InputSource>" in _source_sent(world)
    assert out.endswith("Source: glass (chosen automatically — the scanner has only a glass).")
    assert not any(m["url"].endswith("/ScannerStatus") for m in world.meta)


def test_a_feeder_only_scanner_always_uses_the_feeder(srv, world):
    _add(srv)
    world.caps_script = [_caps(platen=False)]
    out = _scan(srv)
    assert "<pwg:InputSource>Feeder</pwg:InputSource>" in _source_sent(world)
    assert out.endswith("Source: feeder (chosen automatically — the scanner has only a feeder).")


@pytest.mark.parametrize("state,expected", [
    ("ScannerAdfJam", "the document feeder reports a paper jam (ScannerAdfJam) — clear it and scan again."),
    ("ScannerAdfDoorOpen", "the document feeder's cover is open (ScannerAdfDoorOpen) — close it and scan again."),
    ("ScannerAdfProcessing", "the document feeder is still busy (ScannerAdfProcessing) — wait a moment and scan again."),
    ("ScannerAdfMispick", "the document feeder reports ScannerAdfMispick — check the feeder and scan again."),
])
def test_a_feeder_problem_stops_before_any_job_is_started(srv, world, state, expected):
    _add(srv)
    world.caps_script, world.status_script = [_caps()], [_status(state)]
    assert _scan(srv) == f"Scanner 'flatbed': {expected}"
    assert world.posts == [] and world.gets == []


@pytest.mark.parametrize("status,reason", [
    ([_Resp(404)], "the scanner did not report whether the feeder is loaded"),
    ([httpx.ReadTimeout("slow")], "the scanner did not report whether the feeder is loaded"),
    ([_status(None)], "the scanner did not report whether the feeder is loaded"),
    ([_Resp(200, b"<not xml")], "the scanner did not report whether the feeder is loaded"),
])
def test_an_unknown_feeder_state_falls_back_to_the_glass_and_says_so(srv, world, status, reason):
    _add(srv)
    world.caps_script, world.status_script = [_caps()], status
    out = _scan(srv)
    assert "<pwg:InputSource>Platen</pwg:InputSource>" in _source_sent(world)
    assert out.endswith(f"Source: glass (chosen automatically — {reason}).")


@pytest.mark.parametrize("caps", [[_Resp(404)], [httpx.ConnectError("refused")], [_Resp(200, b"garbage")]])
def test_unknown_capabilities_keep_the_previous_behaviour(srv, world, caps):
    _add(srv)
    world.caps_script = caps
    out = _scan(srv)
    assert "<pwg:InputSource>Platen</pwg:InputSource>" in _source_sent(world)
    assert out.endswith("Source: glass (chosen automatically — the scanner did not report its capabilities).")
    assert [p["url"] for p in world.posts][0] == "https://scan.example.com:443/eSCL/ScanJobs"


def test_capabilities_pick_the_transport_the_job_then_uses(srv, world):
    _add(srv)
    world.caps_script = [httpx.ConnectError("https refused"), _caps(adf=False)]
    world.job_script = [_job("/eSCL/ScanJobs/4")]
    _scan(srv)
    assert [m["url"] for m in world.meta] == ["https://scan.example.com:443/eSCL/ScannerCapabilities",
                                              "http://scan.example.com:80/eSCL/ScannerCapabilities"]
    assert world.posts[0]["url"] == "http://scan.example.com:80/eSCL/ScanJobs"  # no pointless https retry


@pytest.mark.parametrize("source,caps,expected", [
    ("adf", _caps(adf=False), "has no document feeder (only a glass) — scan with source=platen or source=auto."),
    ("platen", _caps(platen=False), "has no glass (only a document feeder) — put the pages in the feeder and "
                                    "scan with source=adf or source=auto."),
])
def test_an_explicit_source_the_scanner_lacks_is_refused(srv, world, source, caps, expected):
    _add(srv)
    world.caps_script = [caps]
    assert _scan(srv, source=source) == f"Scanner 'flatbed' {expected}"
    assert world.posts == []


def test_an_explicit_feeder_scan_with_an_empty_feeder_is_refused(srv, world):
    _add(srv)
    world.caps_script, world.status_script = [_caps()], [_status("ScannerAdfEmpty")]
    assert _scan(srv, source="adf") == ("Scanner 'flatbed': the document feeder is empty — put the pages in "
                                        "the feeder, or scan from the glass with source=platen.")
    assert world.posts == []


def test_explicit_sources_still_work_and_glass_skips_the_feeder_query(srv, world):
    _add(srv)
    world.caps_script = [_caps()]
    out = _scan(srv, source="glass")
    assert "<pwg:InputSource>Platen</pwg:InputSource>" in _source_sent(world) and out.endswith("Source: glass.")
    assert not any(m["url"].endswith("/ScannerStatus") for m in world.meta)
    world.posts.clear(); world.gets.clear(); world.status_script = [_status("ScannerAdfLoaded")]
    out = _scan(srv, source="feeder")
    assert "<pwg:InputSource>Feeder</pwg:InputSource>" in _source_sent(world) and out.endswith("Source: feeder.")


@pytest.mark.parametrize("prefix", ["escl", ""])
def test_namespace_prefixes_do_not_matter(srv, world, prefix):
    _add(srv)
    world.caps_script = [_caps(p=prefix)]
    world.status_script = [_status("ScannerAdfLoaded", p=prefix)]
    out = _scan(srv)
    assert "<pwg:InputSource>Feeder</pwg:InputSource>" in _source_sent(world)
    assert out.endswith("(chosen automatically — paper in the feeder).")


def test_duplex_is_sent_only_when_the_feeder_can_do_it(srv, world):
    _add(srv)
    world.caps_script, world.status_script = [_caps(duplex=True)], [_status("ScannerAdfLoaded")]
    out = _scan(srv, duplex=True)
    xml = _source_sent(world)
    assert "<pwg:InputSource>Feeder</pwg:InputSource>" in xml and "<scan:Duplex>true</scan:Duplex>" in xml
    assert out.endswith("Source: feeder, both sides (chosen automatically — paper in the feeder).")
    world.posts.clear(); world.gets.clear()
    _scan(srv)  # no duplex wish → no Duplex element at all
    assert "Duplex" not in _source_sent(world)


@pytest.mark.parametrize("caps,status,expected", [
    (_caps(duplex=False), _status("ScannerAdfLoaded"),
     "its document feeder cannot scan both sides — scan without duplex."),
    (_caps(adf=False), None, "two-sided scanning needs a document feeder, and this scanner has only a glass."),
    (_caps(), _status("ScannerAdfEmpty"), "two-sided scanning needs the document feeder, but it is empty — put "
                                          "the pages in the feeder and scan again."),
])
def test_duplex_that_cannot_work_is_refused_before_a_job(srv, world, caps, status, expected):
    _add(srv)
    world.caps_script = [caps]
    if status is not None:
        world.status_script = [status]
    assert _scan(srv, duplex=True) == f"Scanner 'flatbed': {expected}"
    assert world.posts == []


def test_an_explicit_feeder_scan_stops_on_a_feeder_problem(srv, world):
    _add(srv)
    world.caps_script, world.status_script = [_caps()], [_status("ScannerAdfJam")]
    assert _scan(srv, source="adf") == ("Scanner 'flatbed': the document feeder reports a paper jam "
                                        "(ScannerAdfJam) — clear it and scan again.")
    assert world.posts == []


def test_duplex_with_an_explicit_glass_scan_is_refused(srv, world):
    _add(srv)
    world.caps_script = [_caps(duplex=True)]
    assert _scan(srv, source="platen", duplex=True) == (
        "Scanner 'flatbed': two-sided scanning needs the document feeder — put the pages in the "
        "feeder and scan with source=adf.")
    assert world.posts == []
