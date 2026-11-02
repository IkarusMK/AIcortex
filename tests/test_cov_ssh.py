"""SSH tools (ssh_tools): registration, vault-held credentials, host-key policy,
SSRF guard and the run / upload / download / list-dir paths.

paramiko.SSHClient is replaced by a recording fake and DNS by a scripted resolver
(no network). Proves: credentials are resolved from the vault by NAME and never
echoed or stored; every connection passes netguard (preflight and connect-time);
host keys are pinned via a persisted known_hosts (TOFU, or reject-unknown with
SSH_STRICT_HOST_KEYS=1); uploads cannot leave the data root; downloads stay in
the work dir; failures are readable messages and the client is always closed.
"""
import asyncio
import io
import json
import socket
import sys
import types
from pathlib import Path

import paramiko
import pytest
from cryptography.fernet import Fernet
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
from fastmcp import Client

LAN = "192.0.2.0/24,198.51.100.0/24,203.0.113.0/24"


def _call(server, tool, args=None):
    async def go():
        async with Client(server.mcp) as client:
            res = await client.call_tool(tool, args or {}, raise_on_error=False)
            return res.content[0].text if res.content else ""
    return asyncio.run(go())


def _fake_dns(monkeypatch, table):
    """host -> [ips] (static) or [[ips], [ips], ...] (successive answers, last repeats)."""
    seen = {}

    def resolver(host, port=None, *args, **kwargs):
        entry = table.get(host)
        if entry is None:
            raise socket.gaierror(-2, "Name or service not known")
        n = seen.get(host, 0)
        seen[host] = n + 1
        ips = entry[min(n, len(entry) - 1)] if isinstance(entry[0], (list, tuple)) else entry
        return [(socket.AF_INET, socket.SOCK_STREAM, 6, "", (ip, port or 0)) for ip in ips]
    monkeypatch.setattr(socket, "getaddrinfo", resolver)


class _World:
    """Everything the fake SSH layer records or is told to do."""

    def __init__(self):
        self.clients, self.sftp = [], _FakeSFTP()
        self.connect_error = self.connect_hook = self.host_keys_error = self.exec_error = None
        self.stdout, self.stderr, self.rc = b"", b"", 0


class _FakeSFTP:
    def __init__(self, listing=None, error=None, remote=None):
        self.listing, self.error, self.remote = listing or [], error, remote or {}
        self.puts, self.listed, self.closed = [], None, False

    def _fail(self):
        if self.error:
            raise self.error

    def put(self, local, remote):
        self._fail()
        self.puts.append((local, remote))

    def get(self, remote, local):
        self._fail()
        Path(local).write_bytes(self.remote.get(remote, b"remote-bytes"))

    def listdir_attr(self, path):
        self._fail()
        self.listed = path
        return self.listing

    def close(self):
        self.closed = True


def _stream(data, rc=0):
    return types.SimpleNamespace(
        read=lambda: data, channel=types.SimpleNamespace(recv_exit_status=lambda: rc))


@pytest.fixture
def world(monkeypatch):
    w = _World()

    class FakeClient:
        def __init__(self):
            self.kwargs, self.policy, self.known_hosts = None, None, None
            self.commands, self.closed = [], False
            w.clients.append(self)

        def load_host_keys(self, path):
            if w.host_keys_error:
                raise w.host_keys_error
            self.known_hosts = path

        def set_missing_host_key_policy(self, policy):
            self.policy = policy

        def connect(self, **kwargs):
            self.kwargs = kwargs
            if w.connect_hook:
                w.connect_hook(kwargs)
            if w.connect_error:
                raise w.connect_error

        def exec_command(self, command, timeout=None):
            if w.exec_error:
                raise w.exec_error
            self.commands.append((command, timeout))
            return None, _stream(w.stdout, w.rc), _stream(w.stderr)

        def open_sftp(self):
            return w.sftp

        def close(self):
            self.closed = True

    monkeypatch.setattr(paramiko, "SSHClient", FakeClient)
    return w


@pytest.fixture
def srv(fresh_server, monkeypatch, world):
    monkeypatch.setenv("STORAGE_ENCRYPTION_KEY", Fernet.generate_key().decode())
    monkeypatch.setenv("INTERNAL_ALLOW_CIDRS", LAN)
    monkeypatch.delenv("SSH_STRICT_HOST_KEYS", raising=False)
    _fake_dns(monkeypatch, {"box.example.com": ["203.0.113.10"], "lan.example.net": ["10.0.0.7"]})
    return fresh_server


def _add(srv, name="box", host="box.example.com", **extra):
    args = {"name": name, "host": host, "username": "alice", **extra}
    return _call(srv, "ssh_add", args)


def _vault(value="test-password", name="BOX_PW"):
    sys.modules["secrets_store"].vault_set(name, value)


def _ed25519_pem():
    return Ed25519PrivateKey.generate().private_bytes(
        serialization.Encoding.PEM, serialization.PrivateFormat.OpenSSH,
        serialization.NoEncryption()).decode()


def test_ssh_add_stores_secret_name_only_and_hints_until_the_vault_has_it(srv, tmp_path):
    out = _add(srv, name="Build Box", password_env="BOX_PW", port=2222)
    assert "Registered SSH host 'build-box' (alice@box.example.com:2222)" in out
    assert "secret_set('BOX_PW'" in out
    cfg = json.loads((tmp_path / "ssh_dir" / "build-box.json").read_text())
    assert cfg["password_env"] == "BOX_PW" and cfg["port"] == 2222
    assert "test-password" not in json.dumps(cfg)
    _vault()
    again = _add(srv, name="Build Box", password_env="BOX_PW")
    assert "secret_set" not in again and "test-password" not in again
    _add(srv, name="Build Box", description="second")  # update: unnamed fields are kept
    cfg = json.loads((tmp_path / "ssh_dir" / "build-box.json").read_text())
    assert cfg["description"] == "second" and cfg["password_env"] == "BOX_PW"


def test_ssh_add_reports_a_storage_failure(srv, monkeypatch):
    def boom(*a, **k):
        raise OSError("disk full")
    monkeypatch.setattr(sys.modules["ssh_tools"].cfgstore, "write_merged", boom)
    assert "Could not register host: disk full" in _add(srv)


def test_ssh_list_and_delete_roundtrip(srv, tmp_path):
    assert "No SSH hosts yet" in _call(srv, "ssh_list")
    _add(srv, name="a", description="first box")
    (tmp_path / "ssh_dir" / "broken.json").write_text("{not json")
    listing = _call(srv, "ssh_list")
    assert "- a — alice@box.example.com:22 — first box" in listing
    assert "- broken — (unreadable)" in listing
    assert "Deleted SSH host 'a'" in _call(srv, "ssh_delete_endpoint", {"name": "a"})
    assert "No SSH host 'a'" in _call(srv, "ssh_delete_endpoint", {"name": "a"})


def test_unknown_or_corrupt_host_is_refused_by_every_action(srv, world, tmp_path):
    (tmp_path / "ssh_dir").mkdir()
    (tmp_path / "ssh_dir" / "nope.json").write_text("{oops")  # unreadable config = unknown
    for host in ("nope", "ghost"):
        for tool, args in (("ssh_run", {"command": "id"}),
                           ("ssh_upload", {"source": "x", "dest": "/y"}),
                           ("ssh_download", {"path": "/y"}), ("ssh_list_dir", {})):
            assert f"Unknown SSH host '{host}'" in _call(srv, tool, {"host": host, **args})
    assert world.clients == []


def test_password_comes_from_the_vault_and_is_never_returned(srv, world):
    _vault()
    _add(srv, password_env="BOX_PW")
    world.stdout = b"ok\n"
    out = _call(srv, "ssh_run", {"host": "box", "command": "uptime"})
    kw = world.clients[0].kwargs
    assert kw["password"] == "test-password" and kw["username"] == "alice"
    assert kw["allow_agent"] is False and kw["look_for_keys"] is False
    assert "test-password" not in out


def _ecdsa_pem():
    buf = io.StringIO()
    paramiko.ECDSAKey.generate().write_private_key(buf)
    return buf.getvalue()


@pytest.mark.parametrize("pem,cls", [(_ed25519_pem, paramiko.Ed25519Key), (_ecdsa_pem, paramiko.ECDSAKey)])
def test_private_key_is_parsed_and_passed_as_pkey(srv, world, pem, cls):
    _vault(pem(), "BOX_KEY")
    _add(srv, key_env="BOX_KEY")
    _call(srv, "ssh_run", {"host": "box", "command": "id"})
    kw = world.clients[0].kwargs
    assert isinstance(kw["pkey"], cls) and "password" not in kw


def test_missing_or_unparseable_credentials_are_reported_by_secret_name(srv, world):
    run = lambda: _call(srv, "ssh_run", {"host": "box", "command": "id"})  # noqa: E731
    _add(srv, name="pw", password_env="BOX_PW")
    assert _call(srv, "ssh_run", {"host": "pw", "command": "id"}) == "Needs secret 'BOX_PW'. Use secret_set."
    assert world.clients[0].kwargs is None  # never even tried to connect
    _add(srv, key_env="BOX_KEY")
    assert "Needs secret 'BOX_KEY' (private key)" in run()
    _vault("this is not a key", "BOX_KEY")
    assert "Could not parse the private key" in run()


@pytest.mark.parametrize("strict,policy", [(None, paramiko.AutoAddPolicy), ("1", paramiko.RejectPolicy)])
def test_known_hosts_is_persisted_and_policy_follows_ssh_strict_host_keys(
        srv, world, tmp_path, monkeypatch, strict, policy):
    if strict:
        monkeypatch.setenv("SSH_STRICT_HOST_KEYS", strict)
    _add(srv)
    _call(srv, "ssh_list_dir", {"host": "box"})
    cli = world.clients[0]
    assert cli.known_hosts == str(tmp_path / "ssh_dir" / "known_hosts")
    assert (tmp_path / "ssh_dir" / "known_hosts").exists()
    assert isinstance(cli.policy, policy)  # TOFU by default, reject-unknown when strict


def test_known_hosts_load_failure_does_not_stop_the_connection(srv, world):
    world.host_keys_error = OSError("unreadable")
    _add(srv)
    _call(srv, "ssh_run", {"host": "box", "command": "id"})
    assert world.clients[0].kwargs["hostname"] == "box.example.com"


def test_changed_host_key_surfaces_as_a_connect_failure(srv, world):
    key = paramiko.ECDSAKey.generate()
    world.connect_error = paramiko.BadHostKeyException("box.example.com", key, key)
    _add(srv)
    out = _call(srv, "ssh_run", {"host": "box", "command": "id"})
    assert out.startswith("SSH connect failed: Host key for server 'box.example.com' does not match")


def test_private_target_is_blocked_unless_the_operator_allow_lists_it(srv, world, monkeypatch):
    _add(srv, name="lan", host="lan.example.net")
    out = _call(srv, "ssh_run", {"host": "lan", "command": "id"})
    assert out.startswith("Blocked by network policy") and "10.0.0.7" in out
    assert world.clients == []
    monkeypatch.setenv("INTERNAL_ALLOW_CIDRS", "10.0.0.0/24")
    assert _call(srv, "ssh_run", {"host": "lan", "command": "id"}).startswith("exit 0")


def test_connect_time_guard_stops_dns_rebinding(srv, world, monkeypatch):
    # Preflight sees a vetted address; the real connect would resolve to a private one.
    _fake_dns(monkeypatch, {"rebind.example.com": [["203.0.113.10"], ["10.0.0.9"]]})
    world.connect_hook = lambda kw: socket.getaddrinfo(kw["hostname"], kw["port"])
    _add(srv, name="rb", host="rebind.example.com")
    monkeypatch.setenv("INTERNAL_ALLOW_CIDRS", "203.0.113.0/24")
    out = _call(srv, "ssh_run", {"host": "rb", "command": "id"})
    assert out.startswith("SSH connect failed: netguard:")


def test_missing_paramiko_is_reported(srv, monkeypatch):
    _add(srv)
    monkeypatch.setitem(sys.modules, "paramiko", None)
    assert _call(srv, "ssh_run", {"host": "box", "command": "id"}) == "paramiko not installed in the image."


def test_run_returns_exit_code_stdout_and_stderr_then_closes(srv, world):
    _add(srv)
    world.stdout, world.stderr, world.rc = b"listing\n", b"warning: x\n", 3
    out = _call(srv, "ssh_run", {"host": "box", "command": "ls /srv", "timeout": 5})
    assert out == "exit 3\nlisting\n\n[stderr]\nwarning: x"
    assert world.clients[0].commands == [("ls /srv", 5)] and world.clients[0].closed


def test_run_truncates_huge_output(srv, world):
    _add(srv)
    world.stdout = b"a" * 9000
    out = _call(srv, "ssh_run", {"host": "box", "command": "cat big"})
    assert out.endswith("…(truncated)") and len(out) < 6100


def test_run_reports_command_failure_and_closes_the_client(srv, world):
    _add(srv)
    world.exec_error = RuntimeError("channel closed")
    out = _call(srv, "ssh_run", {"host": "box", "command": "id"})
    assert out == "Command failed: channel closed" and world.clients[0].closed


def test_upload_sends_a_file_from_the_data_root(srv, world, tmp_path):
    _add(srv)
    (tmp_path / "work").mkdir()
    (tmp_path / "work" / "report.txt").write_bytes(b"12345")
    out = _call(srv, "ssh_upload", {"host": "box", "source": "work/report.txt", "dest": "/srv/r.txt"})
    assert out == "Uploaded report.txt → /srv/r.txt (5 B)."
    assert world.sftp.puts == [(str(tmp_path / "work" / "report.txt"), "/srv/r.txt")]
    assert world.sftp.closed and world.clients[0].closed


@pytest.mark.parametrize("source", ["../outside.txt", "/etc/hosts", "SIBLING"])
def test_upload_refuses_sources_outside_the_data_root(srv, world, tmp_path, source):
    _add(srv)
    if source == "SIBLING":  # same string prefix as the data root, different directory
        source = str(tmp_path) + "-backup/secret.txt"
    out = _call(srv, "ssh_upload", {"host": "box", "source": source, "dest": "/x"})
    assert out == "Source must be under /data."
    assert world.clients == []


def test_upload_reports_a_missing_source_and_sftp_errors(srv, world, tmp_path):
    _add(srv)
    assert "No file at" in _call(srv, "ssh_upload", {"host": "box", "source": "nope.txt", "dest": "/x"})
    (tmp_path / "f.txt").write_bytes(b"x")
    world.sftp = _FakeSFTP(error=OSError("permission denied"))
    out = _call(srv, "ssh_upload", {"host": "box", "source": "f.txt", "dest": "/root/x"})
    assert out == "Upload failed: permission denied" and world.clients[0].closed


@pytest.mark.parametrize("tool,args", [
    ("ssh_run", {"host": "box", "command": "id"}),
    ("ssh_upload", {"host": "box", "source": "f.txt", "dest": "/x"}),
    ("ssh_download", {"host": "box", "path": "/x"}),
    ("ssh_list_dir", {"host": "box"}),
])
def test_every_action_reports_a_connect_failure(srv, world, tmp_path, tool, args):
    _add(srv)
    (tmp_path / "f.txt").write_bytes(b"x")
    world.connect_error = OSError("refused")
    assert _call(srv, tool, args) == "SSH connect failed: refused"


def test_download_lands_in_the_work_dir(srv, world, tmp_path):
    _add(srv)
    world.sftp = _FakeSFTP(remote={"/var/log/app.log": b"line1\nline2\n"})
    out = _call(srv, "ssh_download", {"host": "box", "path": "/var/log/app.log"})
    saved = tmp_path / "work_dir" / "app.log"
    assert saved.read_bytes() == b"line1\nline2\n"
    assert out == f"Downloaded /var/log/app.log → {saved} (12 B)."


def test_download_sanitizes_the_local_name(srv, world, tmp_path):
    _add(srv)
    _call(srv, "ssh_download", {"host": "box", "path": "/etc/x", "dest": "../../evil name.sh"})
    files = [p.name for p in (tmp_path / "work_dir").iterdir()]
    assert files == [".._.._evil_name.sh"]  # no separators survive: stays inside the work dir
    _call(srv, "ssh_download", {"host": "box", "path": "/"})
    assert "download" in [p.name for p in (tmp_path / "work_dir").iterdir()]


def _attr(name, size, mode):
    a = paramiko.SFTPAttributes()
    a.filename, a.st_size, a.st_mode = name, size, mode
    return a


def test_list_dir_sorts_and_marks_directories(srv, world):
    _add(srv)
    world.sftp = _FakeSFTP(listing=[_attr("b.txt", 10, 0o100644), _attr("a_dir", 4096, 0o040755),
                                    _attr("nomode", 1, None)])
    out = _call(srv, "ssh_list_dir", {"host": "box", "path": "/srv"})
    assert out == "- a_dir/ · 4096 B\n- b.txt · 10 B\n- nomode · 1 B"
    assert world.sftp.listed == "/srv"


def test_list_dir_empty_and_sftp_errors_for_listing_and_download(srv, world):
    _add(srv)
    assert _call(srv, "ssh_list_dir", {"host": "box"}) == "(empty)"
    world.sftp = _FakeSFTP(error=OSError("denied"))
    assert _call(srv, "ssh_list_dir", {"host": "box"}) == "List failed: denied"
    assert _call(srv, "ssh_download", {"host": "box", "path": "/x"}) == "Download failed: denied"
    assert all(c.closed for c in world.clients)
