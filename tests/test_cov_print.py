"""IPP printing: request encoding, printer registry, retries, SSRF guard, sandbox.

Proves, with no real printer or DNS (httpx.post and socket.getaddrinfo are faked;
netguard still checks the fake resolution): the IPP Print-Job request is encoded
byte-exact (checked by an independent parser, not by the app's own helper);
http -> https (426) and pdf -> octet-stream (0x040A) fallbacks retry with the
right transport and format; error statuses map to readable messages; private
printers are refused before any request and the connect-time guard rejects DNS
rebinding; documents cannot come from outside /data and cannot exceed the size
cap. One known defect is pinned as strict xfail (see the BUG reason).
"""
import asyncio
import base64
import collections
import socket
import struct
import sys
from types import SimpleNamespace
from urllib.parse import urlparse

import pytest
from fastmcp import Client

HOST = "printer.example.net"
PDF = b"%PDF-1.4 test"
Resp = collections.namedtuple("Resp", "status_code content")


def ipp_resp(status):
    return bytes([2, 0]) + struct.pack(">HI", status, 1) + b"\x03"


def parse_ipp(body):
    """Independent IPP request parser: (version, operation, request-id, attrs, data)."""
    op, rid = struct.unpack(">HI", body[2:8])
    assert body[8] == 0x01  # operation-attributes-tag
    i, attrs = 9, []
    while body[i] != 0x03:  # until end-of-attributes-tag
        (nlen,) = struct.unpack(">H", body[i + 1:i + 3])
        name = body[i + 3:i + 3 + nlen].decode()
        j = i + 3 + nlen
        (vlen,) = struct.unpack(">H", body[j:j + 2])
        attrs.append((body[i], name, body[j + 2:j + 2 + vlen].decode()))
        i = j + 2 + vlen
    return body[:2], op, rid, attrs, body[i + 1:]


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
    """Stands in for httpx: records every POST, replays the queued answers."""

    def __init__(self):
        self.calls, self.queue, self.error = [], [Resp(200, ipp_resp(0))], None

    def post(self, url, content=None, headers=None, timeout=None, verify=None):
        socket.getaddrinfo(urlparse(url).hostname, None)  # connect-time DNS (guarded)
        self.calls.append(SimpleNamespace(url=url, body=content, headers=headers, timeout=timeout, verify=verify))
        if self.error:
            raise self.error
        return self.queue.pop(0) if len(self.queue) > 1 else self.queue[0]

    def job(self, index=-1):
        return parse_ipp(self.calls[index].body)


class _Env:
    def __init__(self, server, dns, http, mod, root):
        self.server, self.dns, self.http, self.mod, self.root = server, dns, http, mod, root

    def call(self, tool, **args):
        async def go():
            async with Client(self.server.mcp) as client:
                res = await client.call_tool(tool, args, raise_on_error=False)
                return res.content[0].text if res.content else ""
        return asyncio.run(go())

    def add(self, name="office", **over):
        return self.call("print_add", **{"name": name, "host": HOST, **over})

    def doc(self, **over):
        return self.call("print_document", **{"printer": "office", "file": "doc.pdf", **over})


@pytest.fixture
def env(fresh_server, monkeypatch, tmp_path):
    monkeypatch.setenv("INTERNAL_ALLOW_CIDRS", "192.168.0.0/16")
    dns, hub, mod = _Resolver(), _Hub(), sys.modules["print_tools"]
    dns.set(HOST, "192.168.1.50")
    monkeypatch.setattr(socket, "getaddrinfo", dns)
    monkeypatch.setattr(mod, "httpx", SimpleNamespace(post=hub.post))
    root = tmp_path / "sandbox"  # stands in for /data
    root.mkdir()
    (root / "doc.pdf").write_bytes(PDF)
    monkeypatch.setattr(mod, "DATA_ROOT", root.resolve())
    return _Env(fresh_server, dns, hub, mod, root)


@pytest.fixture
def office(env):
    env.add()
    return env


# --- IPP encoding ------------------------------------------------------------
def test_attribute_encoding_is_tag_name_length_name_value_length_value(env):
    assert env.mod._attr(0x47, "attributes-charset", "utf-8") == b"\x47\x00\x12attributes-charset\x00\x05utf-8"
    assert env.mod._attr(0x42, "job-name", "Zoë") == b"\x42\x00\x08job-name\x00\x04Zo\xc3\xab"  # utf-8 length


def test_print_job_request_has_version_operation_ordered_attributes_and_raw_data(env):
    data = b"\x03\x01binary\x03"  # data may contain the end-of-attributes byte
    version, op, rid, attrs, body = parse_ipp(
        env.mod._build_print_job("ipp://p.example.net:631/ipp/print", "Quarterly", "application/pdf", data))
    assert (version, op, rid) == (b"\x02\x00", 0x0002, 1)
    assert attrs == [(0x47, "attributes-charset", "utf-8"), (0x48, "attributes-natural-language", "en"),
                     (0x45, "printer-uri", "ipp://p.example.net:631/ipp/print"),
                     (0x42, "requesting-user-name", "aicortex"), (0x42, "job-name", "Quarterly"),
                     (0x49, "document-format", "application/pdf")]
    assert body == data
    names = [a[1] for a in parse_ipp(env.mod._build_print_job("ipp://p/", "", "image/jpeg", b"x"))[3]]
    assert "job-name" not in names  # empty job name is omitted


def test_status_is_read_from_bytes_two_and_three_and_short_answers_are_unknown(env):
    assert env.mod._ipp_status(ipp_resp(0x040A)) == 0x040A and env.mod._ipp_status(ipp_resp(0)) == 0
    assert env.mod._ipp_status(b"") is None and env.mod._ipp_status(b"\x02\x00\x00\x00\x00\x00\x00") is None


# --- registry ------------------------------------------------------------------
def test_add_normalises_slug_and_defaults_and_stores_them(env):
    assert env.add("Office Printer!") == "Registered printer 'office-printer' (printer.example.net:631/ipp/print)."
    assert env.add("!!!", path="", port=9100).startswith("Registered printer 'printer' (printer.example.net:9100/ipp/print)")
    cfg = (env.mod.PRINT_DIR / "office-printer.json").read_text()
    assert '"port": 631' in cfg and '"path": "/ipp/print"' in cfg


def test_add_reports_an_unusable_store_instead_of_raising(env, tmp_path, monkeypatch):
    blocker = tmp_path / "not-a-dir"
    blocker.write_text("x")
    monkeypatch.setattr(env.mod, "PRINT_DIR", blocker)
    assert env.add().startswith("Could not register printer:")


def test_list_and_delete_printers_incl_unreadable_config(env):
    assert env.call("print_list") == "No printers configured yet. Use print_add."  # no directory yet
    env.add(description="front desk")
    (env.mod.PRINT_DIR / "broken.json").write_text("{not json")
    assert env.call("print_list").splitlines() == ["- broken — (unreadable config)", f"- office — {HOST}:631 — front desk"]
    assert env.doc(printer="broken") == "Unknown printer 'broken'. Use print_list / print_add."
    assert env.call("print_delete", name="Office") == "Deleted printer 'office'."
    assert env.call("print_delete", name="office") == "No printer 'office'."
    env.call("print_delete", name="broken")
    assert env.call("print_list") == "No printers configured yet. Use print_add."
    assert env.http.calls == []


def test_unknown_printer_is_refused_without_a_request(env):
    assert env.doc(printer="nope") == "Unknown printer 'nope'. Use print_list / print_add."
    assert env.http.calls == []


# --- printing: success path and payload -----------------------------------------
def test_file_is_sent_as_an_ipp_print_job_with_its_name_and_format(office):
    out = office.doc()
    call = office.http.calls[0]
    assert out == "Sent 'doc.pdf' to printer 'office' (IPP status 0x0000) — it should print now."
    assert call.url == f"http://{HOST}:631/ipp/print" and call.headers == {"Content-Type": "application/ipp"}
    assert (call.timeout, call.verify) == (60, False)
    _, op, _, attrs, data = office.http.job()
    attrs = {name: value for _, name, value in attrs}
    assert op == 0x0002 and data == PDF
    assert attrs["printer-uri"] == f"ipp://{HOST}:631/ipp/print"
    assert (attrs["job-name"], attrs["document-format"]) == ("doc.pdf", "application/pdf")


def test_inline_base64_document_gets_a_default_job_name_and_custom_format(office):
    out = office.doc(file="", content_base64=base64.b64encode(b"\xff\xd8photo").decode(), document_format="image/jpeg")
    attrs = {name: value for _, name, value in office.http.job()[3]}
    assert out.startswith("Sent 'document' to printer 'office'")
    assert (attrs["job-name"], attrs["document-format"]) == ("aicortex-job", "image/jpeg")
    assert office.http.job()[4] == b"\xff\xd8photo"
    office.doc(file="", content_base64=base64.b64encode(PDF).decode(), job_name="Invoice 7")
    assert {n: v for _, n, v in office.http.job()[3]}["job-name"] == "Invoice 7"


def test_custom_port_and_path_are_used_for_the_url_and_printer_uri(env):
    env.add(port=8631, path="/ipp")
    env.doc()
    assert env.http.calls[0].url == f"http://{HOST}:8631/ipp"
    assert {n: v for _, n, v in env.http.job()[3]}["printer-uri"] == f"ipp://{HOST}:8631/ipp"


@pytest.mark.parametrize("status", [0x0000, 0x0001, 0x00FF])
def test_ipp_success_statuses_are_accepted(office, status):
    office.http.queue = [Resp(200, ipp_resp(status))]
    assert office.doc() == f"Sent 'doc.pdf' to printer 'office' (IPP status 0x{status:04x}) — it should print now."


# --- printing: fallbacks and error messages ---------------------------------------
def test_upgrade_required_retries_over_https_with_an_ipps_uri(office):
    office.http.queue = [Resp(426, b""), Resp(200, ipp_resp(0))]
    assert office.doc().startswith("Sent 'doc.pdf'")
    assert [c.url for c in office.http.calls] == [f"http://{HOST}:631/ipp/print", f"https://{HOST}:631/ipp/print"]
    assert {n: v for _, n, v in office.http.job()[3]}["printer-uri"] == f"ipps://{HOST}:631/ipp/print"


def test_unsupported_format_retries_as_octet_stream_keeping_the_https_transport(office):
    office.http.queue = [Resp(426, b""), Resp(200, ipp_resp(0x040A)), Resp(200, ipp_resp(0))]
    assert office.doc().startswith("Sent 'doc.pdf'")
    assert [c.url.split(":")[0] for c in office.http.calls] == ["http", "https", "https"]
    assert {n: v for _, n, v in office.http.job()[3]}["document-format"] == "application/octet-stream"
    assert office.http.job()[4] == PDF


def test_unsupported_octet_stream_is_not_retried_and_explains_the_rejection(office):
    office.http.queue = [Resp(200, ipp_resp(0x040A))]
    out = office.doc(document_format="application/octet-stream")
    assert len(office.http.calls) == 1
    assert out.startswith("Printer 'office' did not accept the job (HTTP 200, IPP status 0x040a (document-format-not-supported)).")
    assert "try document_format=application/octet-stream" in out and "(application/octet-stream) is supported" in out


@pytest.mark.parametrize("status,body,fragment", [
    (200, ipp_resp(0x0400), "HTTP 200, IPP status 0x0400 (bad-request))."),
    (200, ipp_resp(0x0509), "IPP status 0x0509 (busy))."),
    (200, ipp_resp(0x0777), "IPP status 0x0777)."),  # unnamed code: no name in parentheses
    (500, ipp_resp(0x0000), "HTTP 500, IPP status 0x0000)"),  # ok code but HTTP error: not accepted
    (404, b"nope", "HTTP 404, IPP status unknown). No IPP response — likely wrong path; try /ipp or /."),
])
def test_rejections_are_translated_into_readable_messages(office, status, body, fragment):
    office.http.queue = [Resp(status, body)]
    out = office.doc()
    assert out.startswith("Printer 'office' did not accept the job (HTTP ") and fragment in out
    assert "Sent" not in out and len(office.http.calls) == 1


def test_transport_errors_are_returned_with_a_connectivity_hint(office):
    office.http.error = ConnectionError("connection refused")
    out = office.doc()
    assert out.startswith("Print request failed (ConnectionError: connection refused).")
    assert f"reachable at {HOST}:631/ipp/print" in out and "INTERNAL_ALLOW_CIDRS" in out


# --- SSRF guard ----------------------------------------------------------------------
def test_private_printer_outside_the_allow_list_is_blocked_before_any_request(env):
    env.dns.set("far.example.net", "10.0.0.7")
    env.add(host="far.example.net")
    assert env.doc() == ("Blocked by network policy — blocked: 10.0.0.7 is internal/link-local and not in "
                         "INTERNAL_ALLOW_CIDRS. Add the range you trust (e.g. your LAN or VPN subnet) to that env var "
                         "to permit it. (add the printer's range to INTERNAL_ALLOW_CIDRS).")
    assert env.http.calls == []


@pytest.mark.parametrize("ips,fragment", [("169.254.169.254", "169.254.169.254"), (None, "cannot resolve host")])
def test_metadata_and_unresolvable_hosts_stay_blocked(env, ips, fragment):
    ips and env.dns.set("odd.example.net", ips)
    env.add(host="odd.example.net")
    out = env.doc()
    assert out.startswith("Blocked by network policy") and fragment in out and env.http.calls == []


def test_a_printer_given_as_ip_literal_passes_when_the_range_is_allow_listed(env):
    env.dns.set("192.168.1.77", "192.168.1.77")
    env.add(host="192.168.1.77")
    assert env.doc().startswith("Sent 'doc.pdf'")
    assert env.http.calls[0].url == "http://192.168.1.77:631/ipp/print"


def test_connect_time_guard_rejects_a_dns_rebinding_answer(env):
    env.dns.set("rebind.example.net", "192.168.1.60", "10.0.0.9")  # check, then connect
    env.add(host="rebind.example.net")
    out = env.doc()
    assert out.startswith("Print request failed (gaierror:") and "internal/blocked at connect" in out
    assert env.http.calls == []


# --- document validation -----------------------------------------------------------------
def test_invalid_base64_and_missing_input_and_empty_document_are_refused(office):
    assert office.doc(file="", content_base64="abcde") == "content_base64 is not valid base64."
    assert office.doc(file="") == "Provide file=<path under /data> or content_base64=<document bytes>."
    (office.root / "empty.pdf").write_bytes(b"")
    assert office.doc(file="empty.pdf") == "Empty document — nothing to print."
    assert office.http.calls == []


def test_size_cap_applies_to_inline_and_file_documents_and_allows_the_exact_limit(office, monkeypatch):
    monkeypatch.setattr(office.mod, "_MAX_DOC", 10)
    big = base64.b64encode(b"x" * 11).decode()
    assert office.doc(file="", content_base64=big) == "Refused: document is 11 bytes, over the 10-byte print limit."
    (office.root / "big.pdf").write_bytes(b"x" * 11)
    assert office.doc(file="big.pdf") == "Refused: 'big.pdf' is 11 bytes, over the 10-byte print limit."
    assert office.http.calls == []
    assert office.doc(file="", content_base64=base64.b64encode(b"x" * 10).decode()).startswith("Sent")


def test_files_outside_the_data_sandbox_are_refused_before_any_request(office, tmp_path):
    (tmp_path / "sandbox-backup").mkdir()  # sibling sharing the string prefix
    (tmp_path / "sandbox-backup" / "x.pdf").write_bytes(PDF)
    (tmp_path / "elsewhere.pdf").write_bytes(PDF)
    (office.root / "link.pdf").symlink_to(tmp_path / "elsewhere.pdf")
    for source in (str(tmp_path / "elsewhere.pdf"), "../elsewhere.pdf", "../sandbox-backup/x.pdf", "link.pdf", "/etc/hosts"):
        assert office.doc(file=source) == "File must be under /data."
    assert office.http.calls == []


def test_missing_files_and_directories_are_reported(office):
    (office.root / "folder").mkdir()
    assert office.doc(file="nope.pdf") == f"No file at '{office.root.resolve() / 'nope.pdf'}'."
    assert office.doc(file="folder").startswith("No file at ")
    assert office.http.calls == []


# --- known defect (strict xfail: flips to a failure once fixed) ------------------------------
def test_a_path_without_leading_slash_still_targets_the_printer_path(env):
    env.add(path="ipp/print")
    env.doc()
    assert urlparse(env.http.calls[0].url).path == "/ipp/print"


def test_an_html_error_page_is_not_mistaken_for_an_ipp_status(office):
    office.http.queue = [Resp(404, b"<html><body>Not Found</body></html>")]
    out = office.doc()
    assert "No IPP response" in out and "0x746d" not in out
