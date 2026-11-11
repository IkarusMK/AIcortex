"""FTP/FTPS tools (ftp_tools): registration, ftplib connect, listing, upload, the
curl-based FTPS upload and the TLS-session-reusing implicit FTPS class.

ftplib, curl (subprocess.run) and DNS are replaced by recording fakes (no network).
Proves: the password is resolved from the vault by NAME, reaches curl only via the
stdin config (never argv) and is never returned; the host passes netguard before
AND at connect time (including the address pinned into curl); the TLS context
follows tls / tls_insecure / ca_bundle; uploads cannot come from outside the data
root; failures are readable and sessions closed. A strict xfail documents a real
curl-config injection bug.
"""
import asyncio
import ftplib
import json
import socket
import ssl
import subprocess
import sys
import types
from pathlib import Path
from unittest import mock

import pytest
from cryptography.fernet import Fernet
from fastmcp import Client

LAN = "192.0.2.0/24,198.51.100.0/24,203.0.113.0/24,2001:db8::/32"


def _call(server, tool, args=None):
    async def go():
        async with Client(server.mcp) as client:
            res = await client.call_tool(tool, args or {}, raise_on_error=False)
            return res.content[0].text if res.content else ""
    return asyncio.run(go())


def _fake_dns(monkeypatch, table):
    """host -> [ips] (static) or [[ips], [ips], ...] (successive answers, last repeats)."""
    seen = {}

    def resolver(host, port=None, *args, type=0, **kwargs):
        entry = table.get(host)
        if entry is None and host.replace(".", "").isdigit():
            entry = [host]
        if entry is None:
            raise socket.gaierror(-2, "Name or service not known")
        n = seen.get(host, 0)
        seen[host] = n + 1
        ips = entry[min(n, len(entry) - 1)] if isinstance(entry[0], (list, tuple)) else entry
        return [(socket.AF_INET6 if ":" in ip else socket.AF_INET, socket.SOCK_STREAM, 6, "", (ip, port or 0))
                for ip in ips]
    monkeypatch.setattr(socket, "getaddrinfo", resolver)


class _World:
    def __init__(self):
        self.sessions, self.connect_hook = [], None
        self.names, self.nlst_error, self.store_error, self.quit_error = [], None, None, None
        self.curl_calls, self.curl_result = [], types.SimpleNamespace(returncode=0, stderr=b"")


class _FTP:
    def __init__(self, world, kind, context=None):
        self.world, self.kind, self.context = world, kind, context
        self.connected, self.logins, self.stored = None, [], []
        self.prot_p_called = self.quit_called = self.closed = False
        world.sessions.append(self)

    def connect(self, host, port, timeout=None):
        if self.world.connect_hook:
            self.world.connect_hook(host, port)
        self.connected = (host, port, timeout)

    def login(self, user, password):
        self.logins.append((user, password))

    def prot_p(self):
        self.prot_p_called = True

    def nlst(self, path):
        if self.world.nlst_error:
            raise self.world.nlst_error
        return self.world.names

    def storbinary(self, cmd, fh):
        if self.world.store_error:
            raise self.world.store_error
        self.stored.append((cmd, fh.read()))

    def quit(self):
        self.quit_called = True
        if self.world.quit_error:
            raise self.world.quit_error

    def close(self):
        self.closed = True


@pytest.fixture
def world(fresh_server, monkeypatch):
    w = _World()
    tools = sys.modules["ftp_tools"]
    monkeypatch.setattr(ftplib, "FTP", lambda: _FTP(w, "plain"))
    monkeypatch.setattr(ftplib, "FTP_TLS", lambda context=None: _FTP(w, "explicit", context))
    monkeypatch.setattr(tools, "_ImplicitFTP_TLS", lambda context=None: _FTP(w, "implicit", context))
    monkeypatch.setattr(tools.shutil, "which", lambda name: None)  # default: no curl on the image

    def fake_run(argv, input=None, capture_output=None, timeout=None):
        w.curl_calls.append({"argv": argv, "input": input.decode(), "timeout": timeout})
        if isinstance(w.curl_result, Exception):
            raise w.curl_result
        return w.curl_result
    monkeypatch.setattr(subprocess, "run", fake_run)
    return w


@pytest.fixture
def srv(fresh_server, monkeypatch, world):
    monkeypatch.setenv("STORAGE_ENCRYPTION_KEY", Fernet.generate_key().decode())
    monkeypatch.setenv("INTERNAL_ALLOW_CIDRS", LAN)
    _fake_dns(monkeypatch, {"ftp.example.com": ["203.0.113.40"], "nas.example.net": ["10.0.0.40"],
                            "dual.example.com": ["2001:db8::40", "203.0.113.41"]})
    return fresh_server


def _add(srv, name="files", **extra):
    return _call(srv, "ftp_add", {"name": name, "host": "ftp.example.com", **extra})


def _vault(name="FTP_PW", value="test-password"):
    # Stored with its host, as a person does (only a person binds a secret, 2.6.0).
    sys.modules["secrets_store"].vault_set(name, value, hosts=["ftp.example.com", "dual.example.com", "nas.example.net", "printer.example.com", "rebind.example.com"])


def _file(tmp_path, name="m.bin", data=b"payload"):
    p = tmp_path / "work" / name
    p.parent.mkdir(exist_ok=True)
    p.write_bytes(data)
    return p


def test_ftp_add_normalises_options_stores_the_secret_name_only(srv, tmp_path):
    out = _add(srv, name="Printer SD", port=990, tls="IMPLICIT", ca_bundle=" /data/ca.pem ",
               username="bblp", password_env="FTP_PW")
    assert out.startswith("Registered FTP endpoint 'printer-sd'.") and "secret_set('FTP_PW'" in out
    path = tmp_path / "ftp_dir" / "printer-sd.json"
    cfg = json.loads(path.read_text())
    assert cfg["tls"] == "implicit" and cfg["ca_bundle"] == "/data/ca.pem" and cfg["port"] == 990
    _vault()
    assert "secret_set" not in _add(srv, name="Printer SD", password_env="FTP_PW")
    _add(srv, name="Printer SD", description="updated")  # unnamed fields are kept
    cfg = json.loads(path.read_text())
    assert cfg["description"] == "updated" and cfg["password_env"] == "FTP_PW" and "test-password" not in path.read_text()


def test_endpoint_listing_deletion_and_storage_failure(srv, tmp_path, monkeypatch):
    assert _call(srv, "ftp_list_endpoints") == "No FTP endpoints configured yet."
    (tmp_path / "ftp_dir").mkdir()
    assert "Use ftp_add" in _call(srv, "ftp_list_endpoints")
    _add(srv, name="a", port=2121, description="first")
    (tmp_path / "ftp_dir" / "broken.json").write_text("{nope")
    listing = _call(srv, "ftp_list_endpoints")
    assert "- a — ftp.example.com:2121 — first" in listing and "- broken — (unreadable config)" in listing
    assert "Deleted FTP endpoint 'a'" in _call(srv, "ftp_delete", {"name": "a"})
    assert "No FTP endpoint 'a'" in _call(srv, "ftp_delete", {"name": "a"})

    def boom(*a, **k):
        raise OSError("read-only")
    monkeypatch.setattr(sys.modules["ftp_tools"].cfgstore, "write_merged", boom)
    assert "Could not register endpoint: read-only" in _add(srv)


def test_unknown_or_corrupt_endpoint_is_refused(srv, world, tmp_path):
    (tmp_path / "ftp_dir").mkdir()
    (tmp_path / "ftp_dir" / "bad.json").write_text("{nope")
    for name in ("ghost", "bad"):
        assert f"Unknown FTP endpoint '{name}'" in _call(srv, "ftp_list", {"server": name})
        up = _call(srv, "ftp_upload", {"server": name, "nas_path": "x", "remote_path": "y"})
        assert f"Unknown FTP endpoint '{name}'" in up
    assert world.sessions == []


def test_plain_ftp_logs_in_anonymously_and_lists(srv, world):
    _add(srv)
    world.names = ["a.txt", "b.txt"]
    assert _call(srv, "ftp_list", {"server": "files", "path": "/pub"}) == "a.txt\nb.txt"
    (s,) = world.sessions
    assert s.kind == "plain" and s.connected == ("ftp.example.com", 21, 30)
    assert s.logins == [("anonymous", "")] and not s.prot_p_called and s.quit_called
    world.names = []
    assert _call(srv, "ftp_list", {"server": "files"}) == "(empty) /"


def test_vault_password_is_used_for_login_and_never_returned(srv, world):
    _vault()
    _add(srv, username="alice", password_env="FTP_PW")
    world.names = ["x"]
    out = _call(srv, "ftp_list", {"server": "files"})
    assert world.sessions[0].logins == [("alice", "test-password")] and "test-password" not in out


@pytest.mark.parametrize("tls,insecure,verify", [("explicit", False, ssl.CERT_REQUIRED),
                                                 ("implicit", False, ssl.CERT_REQUIRED),
                                                 ("implicit", True, ssl.CERT_NONE)])
def test_tls_modes_encrypt_the_data_channel_and_follow_the_verification_policy(srv, world, tls, insecure, verify):
    _add(srv, tls=tls, port=990, tls_insecure=insecure)
    _call(srv, "ftp_list", {"server": "files"})
    (s,) = world.sessions
    assert s.kind == tls and s.prot_p_called and s.connected[1] == 990
    assert s.context.verify_mode == verify and s.context.check_hostname is (not insecure)


def test_private_host_is_blocked_unless_allow_listed(srv, world, monkeypatch):
    _call(srv, "ftp_add", {"name": "lan", "host": "nas.example.net"})
    out = _call(srv, "ftp_list", {"server": "lan"})
    assert out.startswith("Connect failed: Blocked by network policy") and "10.0.0.40" in out
    assert world.sessions == []
    monkeypatch.setenv("INTERNAL_ALLOW_CIDRS", "10.0.0.0/24")
    assert _call(srv, "ftp_list", {"server": "lan"}) == "(empty) /"


def test_connect_time_guard_stops_dns_rebinding(srv, world, monkeypatch):
    _fake_dns(monkeypatch, {"ftp.example.com": [["203.0.113.40"], ["10.0.0.9"]]})
    world.connect_hook = lambda host, port: socket.getaddrinfo(host, port)
    _add(srv)
    monkeypatch.setenv("INTERNAL_ALLOW_CIDRS", "203.0.113.0/24")
    assert _call(srv, "ftp_list", {"server": "files"}).startswith("Connect failed: netguard:")


def test_listing_errors_and_failing_quit_still_release_the_connection(srv, world):
    _add(srv)
    world.nlst_error = ftplib.error_perm("550 no such directory")
    world.quit_error = OSError("broken pipe")
    assert _call(srv, "ftp_list", {"server": "files", "path": "/x"}) == "List failed: 550 no such directory"
    assert world.sessions[0].quit_called and world.sessions[0].closed


def test_upload_over_plain_ftp_stores_the_file_content(srv, world, tmp_path):
    _add(srv)
    _file(tmp_path)
    out = _call(srv, "ftp_upload", {"server": "files", "nas_path": "work/m.bin", "remote_path": "in/m.bin"})
    assert out == "Uploaded m.bin (7 bytes) → in/m.bin"
    assert world.sessions[0].stored == [("STOR in/m.bin", b"payload")] and world.sessions[0].quit_called


@pytest.mark.parametrize("nas_path", ["../outside.bin", "/etc/hosts", "SIBLING"])
def test_upload_refuses_sources_outside_the_data_root(srv, world, tmp_path, nas_path):
    _add(srv)
    if nas_path == "SIBLING":  # shares the string prefix of the data root
        nas_path = str(tmp_path) + "-backup/x.bin"
    out = _call(srv, "ftp_upload", {"server": "files", "nas_path": nas_path, "remote_path": "x"})
    assert out == "Source path must be under /data (the NAS workspace)." and world.sessions == []


def test_upload_reports_missing_file_connect_and_store_errors(srv, world, tmp_path):
    _add(srv)
    args = {"server": "files", "nas_path": "work/m.bin", "remote_path": "m.bin"}
    assert "No such file" in _call(srv, "ftp_upload", args)
    _file(tmp_path)
    world.store_error = ftplib.error_temp("452 disk full")
    world.quit_error = OSError("gone")
    assert _call(srv, "ftp_upload", args) == "Upload failed: 452 disk full"
    assert world.sessions[0].closed  # quit failed -> close() releases the socket
    _call(srv, "ftp_add", {"name": "files", "host": "nas.example.net"})  # now a private target
    assert _call(srv, "ftp_upload", args).startswith("Connect failed: Blocked by network policy")


def test_ftps_without_curl_falls_back_to_ftplib(srv, world, tmp_path):
    _add(srv, tls="explicit")
    _file(tmp_path)
    out = _call(srv, "ftp_upload", {"server": "files", "nas_path": "work/m.bin", "remote_path": "m.bin"})
    assert out.startswith("Uploaded m.bin") and world.sessions[0].prot_p_called and world.curl_calls == []


@pytest.fixture
def curl(world, monkeypatch):
    monkeypatch.setattr(sys.modules["ftp_tools"].shutil, "which", lambda name: "/usr/bin/curl")
    return world


def _curl_upload(srv, tmp_path, name="files", remote="m.bin"):
    _file(tmp_path)
    return _call(srv, "ftp_upload", {"server": name, "nas_path": "work/m.bin", "remote_path": remote})


def test_curl_receives_credentials_on_stdin_only_and_pins_the_vetted_ip(srv, curl, tmp_path):
    _vault()
    _call(srv, "ftp_add", {"name": "dual", "host": "dual.example.com", "port": 990, "tls": "implicit",
                           "username": "alice", "password_env": "FTP_PW"})
    out = _curl_upload(srv, tmp_path, "dual", "/sub dir/m.bin")
    (call,) = curl.curl_calls
    assert call["argv"] == ["curl", "--config", "-"] and call["timeout"] == 330
    assert 'user = "alice:test-password"' in call["input"]
    assert 'url = "ftps://dual.example.com:990/sub%20dir/m.bin"' in call["input"]
    assert 'resolve = "dual.example.com:990:203.0.113.41"' in call["input"]  # IPv4 preferred
    assert "test-password" not in " ".join(call["argv"]) and "test-password" not in out
    assert out == "Uploaded m.bin (7 bytes) → /sub dir/m.bin"


def test_curl_with_an_ip_literal_host_needs_no_pin(srv, curl, tmp_path):
    _call(srv, "ftp_add", {"name": "lit", "host": "192.0.2.50", "tls": "explicit", "ca_bundle": "/data/ca.pem"})
    _curl_upload(srv, tmp_path, "lit")
    cfg = curl.curl_calls[0]["input"]
    assert 'url = "ftp://192.0.2.50:21/m.bin"' in cfg and "ssl-reqd" in cfg
    assert "resolve" not in cfg and 'cacert = "/data/ca.pem"' in cfg and "insecure" not in cfg
    _call(srv, "ftp_add", {"name": "open", "host": "192.0.2.50", "tls": "implicit", "tls_insecure": True})
    _curl_upload(srv, tmp_path, "open")
    assert "\ninsecure\n" in curl.curl_calls[1]["input"]


def test_curl_path_applies_the_egress_policy_before_running(srv, curl, tmp_path):
    _call(srv, "ftp_add", {"name": "lan", "host": "nas.example.net", "tls": "implicit"})
    out = _curl_upload(srv, tmp_path, "lan")
    assert out.startswith("Connect failed: Blocked by network policy") and curl.curl_calls == []


def test_curl_pin_resolution_is_guarded_against_rebinding(srv, curl, tmp_path, monkeypatch):
    _call(srv, "ftp_add", {"name": "rb", "host": "rebind.example.com", "tls": "implicit"})
    _fake_dns(monkeypatch, {"rebind.example.com": [["203.0.113.40"], ["10.0.0.9"]]})
    monkeypatch.setenv("INTERNAL_ALLOW_CIDRS", "203.0.113.0/24")
    out = _curl_upload(srv, tmp_path, "rb")
    assert out.startswith("Connect failed: cannot resolve 'rebind.example.com': netguard:")
    assert curl.curl_calls == []


@pytest.mark.parametrize("result,expected", [
    (types.SimpleNamespace(returncode=67, stderr=b"login denied\n"), "Upload failed: curl exit 67: login denied"),
    (types.SimpleNamespace(returncode=28, stderr=b""), "Upload failed: curl exit 28: no error output"),
    (subprocess.TimeoutExpired("curl", 330), "Upload failed: curl did not finish within 330s."),
    (FileNotFoundError("curl"), "Upload failed: could not run curl: curl"),
])
def test_curl_failures_are_reported(srv, curl, tmp_path, result, expected):
    _call(srv, "ftp_add", {"name": "ok", "host": "192.0.2.50", "tls": "implicit"})
    curl.curl_result = result
    assert _curl_upload(srv, tmp_path, "ok") == expected


@pytest.mark.parametrize("host,expected", [("192.0.2.1", True), ("[2001:db8::1]", True),
                                           ("printer.example.com", False)])
def test_ip_literal_detection(srv, host, expected):
    assert sys.modules["ftp_tools"]._is_ip_literal(host) is expected


@pytest.mark.parametrize("field", ["src", "username", "password"])
def test_curl_config_values_cannot_inject_extra_options(srv, field):
    tools = sys.modules["ftp_tools"]
    evil = 'x"\noutput = "/data/pwned'
    cfg = {"host": "192.0.2.50", "port": 990, "tls": "implicit", "username": "u"}
    clean = tools._curl_config(cfg, "pw", Path("/data/work/a.bin"), "a.bin")
    dirty = tools._curl_config({**cfg, **({"username": evil} if field == "username" else {})},
                               evil if field == "password" else "pw",
                               Path("/data/work/" + evil) if field == "src" else Path("/data/work/a.bin"),
                               "a.bin")
    assert len(dirty.splitlines()) == len(clean.splitlines())


class _Ctx:
    def __init__(self):
        self.wrapped = []

    def wrap_socket(self, sock, server_hostname=None, session=None):
        self.wrapped.append((sock, server_hostname, session))
        return ("tls", sock)


def _implicit(fresh_server):
    ctx = _Ctx()
    ftp = sys.modules["ftp_tools"]._ImplicitFTP_TLS(context=ctx)
    ftp.host = "ftp.example.com"
    return ftp, ctx


def test_implicit_ftps_wraps_the_control_socket_in_tls_immediately(fresh_server):
    ftp, ctx = _implicit(fresh_server)
    assert ftp.sock is None
    ftp.sock = "raw-socket"
    assert ftp.sock == ("tls", "raw-socket") and ctx.wrapped == [("raw-socket", "ftp.example.com", None)]
    already = mock.MagicMock(spec=ssl.SSLSocket)
    ftp.sock = already
    assert ftp.sock is already and len(ctx.wrapped) == 1  # an SSL socket is never wrapped twice
    ftp.sock = None
    assert ftp.sock is None


@pytest.mark.parametrize("prot_p,session_reused", [(True, True), (False, False)])
def test_data_channel_resumes_the_control_session_only_when_protected(fresh_server, monkeypatch,
                                                                     prot_p, session_reused):
    ftp, ctx = _implicit(fresh_server)
    ftp._sock = types.SimpleNamespace(session="session-1")
    ftp._prot_p = prot_p
    monkeypatch.setattr(ftplib.FTP, "ntransfercmd", lambda self, cmd, rest=None: ("data-conn", 42))
    conn, size = ftp.ntransfercmd("STOR x")
    assert size == 42
    if session_reused:
        assert conn == ("tls", "data-conn") and ctx.wrapped == [("data-conn", "ftp.example.com", "session-1")]
    else:
        assert conn == "data-conn" and ctx.wrapped == []
