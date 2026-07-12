"""Tests for the curl-based FTPS upload (ftp_tools).

The bug: Bambu Lab printers (and vsftpd with `require_ssl_reuse`) demand TLS
session reuse on the FTPS DATA channel; Python's ftplib doesn't deliver that
reliably, so STOR hangs → "read operation timed out". The fix routes FTPS
uploads through curl. These tests exercise the pure config builder (credentials
via stdin config, TLS policy knobs, URL encoding, IP pinning) and the routing
decision — no network, no real curl run.
"""
import importlib
from pathlib import Path


def _ftp(monkeypatch, tmp_path):
    monkeypatch.setenv("FTP_DIR", str(tmp_path / "ftp"))
    monkeypatch.setenv("DATA_ROOT", str(tmp_path))
    import ftp_tools
    importlib.reload(ftp_tools)
    return ftp_tools


def _base_cfg(**over):
    cfg = {"name": "p1s", "host": "192.168.178.77", "port": 990,
           "tls": "implicit", "tls_insecure": True, "username": "bblp",
           "password_env": "BAMBU_ACCESS_CODE"}
    cfg.update(over)
    return cfg


def test_config_implicit_ftps_url_and_stdin_credentials(monkeypatch, tmp_path):
    ftp_tools = _ftp(monkeypatch, tmp_path)
    src = tmp_path / "work" / "model.gcode.3mf"
    cfg = ftp_tools._curl_config(_base_cfg(), "s3cret", src, "model.gcode.3mf")
    assert 'url = "ftps://192.168.178.77:990/model.gcode.3mf"' in cfg
    assert 'user = "bblp:s3cret"' in cfg          # stdin config, never argv
    assert f'upload-file = "{src}"' in cfg
    assert "insecure" in cfg                       # tls_insecure honored
    assert "ssl-reqd" not in cfg                   # implicit = TLS from byte one


def test_config_explicit_ftps_requires_tls_upgrade(monkeypatch, tmp_path):
    ftp_tools = _ftp(monkeypatch, tmp_path)
    cfg = ftp_tools._curl_config(
        _base_cfg(tls="explicit", port=21, tls_insecure=False),
        "pw", tmp_path / "f.bin", "f.bin")
    assert 'url = "ftp://192.168.178.77:21/f.bin"' in cfg
    assert "ssl-reqd" in cfg                       # refuse to fall back to plain
    assert "insecure" not in cfg                   # verify by default


def test_config_ca_bundle_wins_over_insecure(monkeypatch, tmp_path):
    ftp_tools = _ftp(monkeypatch, tmp_path)
    cfg = ftp_tools._curl_config(
        _base_cfg(ca_bundle="/data/certs/printer.pem", tls_insecure=True),
        "pw", tmp_path / "f.bin", "f.bin")
    assert 'cacert = "/data/certs/printer.pem"' in cfg
    assert "\ninsecure" not in cfg                 # pin beats opt-out


def test_config_escapes_password_and_encodes_path(monkeypatch, tmp_path):
    ftp_tools = _ftp(monkeypatch, tmp_path)
    cfg = ftp_tools._curl_config(
        _base_cfg(), 'p"w\\d', tmp_path / "f.bin", "/sub dir/my file.3mf")
    assert 'user = "bblp:p\\"w\\\\d"' in cfg       # quotes/backslashes escaped
    # leading '/' stripped (FTP URLs are login-dir-relative), spaces encoded
    assert "/sub%20dir/my%20file.3mf" in cfg


def test_config_pins_vetted_ip_for_hostnames(monkeypatch, tmp_path):
    ftp_tools = _ftp(monkeypatch, tmp_path)
    cfg = ftp_tools._curl_config(
        _base_cfg(host="printer.lan"), "pw", tmp_path / "f.bin", "f.bin",
        pinned_ip="192.168.178.77")
    assert 'resolve = "printer.lan:990:192.168.178.77"' in cfg


def test_upload_routes_ftps_through_curl(monkeypatch, tmp_path):
    """ftp_upload must take the curl path for TLS endpoints (never ftplib)."""
    ftp_tools = _ftp(monkeypatch, tmp_path)
    src = tmp_path / "work" / "m.3mf"
    src.parent.mkdir(parents=True)
    src.write_bytes(b"x" * 8)

    calls = {}
    monkeypatch.setattr(ftp_tools, "_load", lambda name: _base_cfg())
    monkeypatch.setattr(ftp_tools.shutil, "which", lambda n: "/usr/bin/curl")
    def _fake_curl_upload(cfg, s, r):
        calls["args"] = (cfg, s, r)
        return "Uploaded m.3mf (8 bytes) → m.3mf"
    monkeypatch.setattr(ftp_tools, "_curl_upload", _fake_curl_upload)

    def _boom(cfg):  # ftplib path must not be touched
        raise AssertionError("ftplib path used for FTPS")
    monkeypatch.setattr(ftp_tools, "_connect", _boom)

    mcp = _FakeMCP()
    ftp_tools.register(mcp)
    out = mcp.tools["ftp_upload"]("p1s", "work/m.3mf", "m.3mf")
    assert out.startswith("Uploaded m.3mf")
    assert calls["args"][2] == "m.3mf"


def test_upload_plain_ftp_keeps_ftplib(monkeypatch, tmp_path):
    """No TLS → the original ftplib path (curl adds nothing there)."""
    ftp_tools = _ftp(monkeypatch, tmp_path)
    src = tmp_path / "work" / "m.bin"
    src.parent.mkdir(parents=True)
    src.write_bytes(b"y")

    monkeypatch.setattr(ftp_tools, "_load", lambda name: _base_cfg(tls="none"))
    monkeypatch.setattr(
        ftp_tools, "_curl_upload",
        lambda *a: (_ for _ in ()).throw(AssertionError("curl path used for plain FTP")))

    class _FakeFTP:
        def storbinary(self, cmd, f):
            _FakeFTP.stored = cmd
        def quit(self):
            pass
    monkeypatch.setattr(ftp_tools, "_connect", lambda cfg: _FakeFTP())

    mcp = _FakeMCP()
    ftp_tools.register(mcp)
    out = mcp.tools["ftp_upload"]("plain", "work/m.bin", "m.bin")
    assert out.startswith("Uploaded m.bin")
    assert _FakeFTP.stored == "STOR m.bin"


class _FakeMCP:
    def __init__(self):
        self.tools = {}

    def tool(self, fn):
        self.tools[fn.__name__] = fn
        return fn
