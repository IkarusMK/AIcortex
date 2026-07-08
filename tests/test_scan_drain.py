"""Regression test for the scan_document eSCL drain fix.

The bug: scan_document grabbed the FIRST page and stopped, never reading NextDocument
until 404 — so the scanner never got told the job was done, stayed "busy", and the next
scan came back HTTP 503 (the operator had to cancel at the device). The fix drains
NextDocument to 404. These tests mock the eSCL HTTP exchange (no real device) and assert
the client keeps going until the 404 that releases the scanner.
"""
import contextlib
import importlib
import tempfile
from pathlib import Path


class FakeMCP:
    def __init__(self):
        self.tools = {}

    def tool(self, fn):
        self.tools[fn.__name__] = fn
        return fn


class _Resp:
    def __init__(self, status, content=b"", headers=None):
        self.status_code = status
        self.content = content
        self.headers = headers or {}


def _scan_tool(monkeypatch, get_sequence):
    """Wire scan_document up against a scripted eSCL exchange; return (tool, counters)."""
    work = Path(tempfile.mkdtemp()) / "work"
    monkeypatch.setenv("WORK_DIR", str(work))
    monkeypatch.setenv("SCAN_DIR", str(Path(tempfile.mkdtemp()) / "scanners"))
    import scan_tools
    importlib.reload(scan_tools)

    monkeypatch.setattr(scan_tools, "_load",
                        lambda name: {"host": "10.0.0.9", "base": "/eSCL", "port": 0, "tls": "auto"})
    monkeypatch.setattr(scan_tools.netguard, "check_host", lambda h: (True, ""))
    monkeypatch.setattr(scan_tools.netguard, "tls_verify", lambda cfg: True)
    monkeypatch.setattr(scan_tools.netguard, "guard", lambda h: contextlib.nullcontext())

    counters = {"post": 0, "get": 0}

    def fake_post(url, **kw):
        counters["post"] += 1
        return _Resp(201, headers={"Location": "http://10.0.0.9:80/eSCL/ScanJobs/1"})

    def fake_get(url, **kw):
        i = counters["get"]
        counters["get"] += 1
        return get_sequence[i]

    monkeypatch.setattr(scan_tools.httpx, "post", fake_post)
    monkeypatch.setattr(scan_tools.httpx, "get", fake_get)

    mcp = FakeMCP()
    scan_tools.register(mcp)
    return mcp.tools["scan_document"], counters, work


def test_scan_drains_multipage_until_404(monkeypatch):
    seq = [_Resp(200, b"PAGE1"), _Resp(200, b"PAGE2"), _Resp(404)]
    scan_document, counters, work = _scan_tool(monkeypatch, seq)
    out = scan_document("epson", format="jpeg", filename="t.jpg")

    assert counters["get"] == 3, out          # 2 pages + the 404 that releases the device
    assert "2 page(s)" in out
    assert (work / "t.jpg").read_bytes() == b"PAGE1"
    assert (work / "t-2.jpg").read_bytes() == b"PAGE2"


def test_scan_single_page_still_reads_the_releasing_404(monkeypatch):
    # The whole point: even a 1-page platen scan must issue the follow-up GET that
    # returns 404, or the device stays busy. So one page => exactly two GETs.
    seq = [_Resp(200, b"ONLYPAGE"), _Resp(404)]
    scan_document, counters, work = _scan_tool(monkeypatch, seq)
    out = scan_document("epson", format="jpeg", filename="single.jpg")

    assert counters["get"] == 2, out          # page + releasing 404
    assert "1 page(s)" in out
    assert (work / "single.jpg").read_bytes() == b"ONLYPAGE"


def test_scan_rides_out_503_warmup(monkeypatch):
    # A 503 before the first page = device still warming up; the loop should wait and
    # retry, then still drain to 404.
    seq = [_Resp(503), _Resp(200, b"P"), _Resp(404)]
    scan_document, counters, _work = _scan_tool(monkeypatch, seq)
    monkeypatch.setattr("time.sleep", lambda *_a, **_k: None)   # don't actually sleep
    out = scan_document("epson", format="jpeg", filename="warm.jpg")

    assert counters["get"] == 3, out
    assert "1 page(s)" in out
