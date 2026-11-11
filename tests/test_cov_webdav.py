"""WebDAV tools: registration, vault-resolved credentials, SSRF guard, transfers.

Proves, with no real network or DNS (httpx.Client and socket.getaddrinfo are
faked; netguard still checks the fake resolution): registration stores only the
secret NAME; the app password comes from the vault, reaches the client as basic
auth and never shows in output or on disk; private targets are refused before a
client exists and the connect-time guard rejects DNS rebinding; PROPFIND / PUT /
GET / MKCOL / DELETE hit the right URLs; uploads cannot leave the /data sandbox
(absolute, `..`, sibling dir, symlink) and downloads cannot escape the work dir.
One known defect is pinned as strict xfail (see the BUG reason).
"""
import asyncio
import collections
import re
import socket
import sys
from types import SimpleNamespace
from urllib.parse import urlparse

import pytest
from cryptography.fernet import Fernet
from fastmcp import Client

PASSWORD = "test-password"
HOST = "dav.example.com"
BASE = f"https://{HOST}/remote.php/dav/files/alice/"
ROOT = "/remote.php/dav/files/alice"
Call = collections.namedtuple("Call", "method url headers body")


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
    """Stands in for httpx and is its own Client/response: records and replays."""

    def __init__(self):
        self.clients, self.calls, self.error, self.closed = [], [], None, 0
        self.status, self.content = 207, b""

    @property
    def status_code(self):
        return self.status

    def reply(self, status, body=b""):
        self.status, self.content = status, body.encode() if isinstance(body, str) else body

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
        return self

    def put(self, url, content=None, headers=None):
        return self.request("PUT", url, content.read() if hasattr(content, "read") else content, headers)

    def stream(self, method, url):
        return self.request(method, url)

    def iter_bytes(self, chunk_size=None):
        yield self.content[:4]
        yield self.content[4:]


class _Env:
    def __init__(self, server, dns, http, mod, root):
        self.server, self.dns, self.http, self.mod, self.root = server, dns, http, mod, root

    def call(self, tool, **args):
        async def go():
            async with Client(self.server.mcp) as client:
                res = await client.call_tool(tool, args, raise_on_error=False)
                return res.content[0].text if res.content else ""
        return asyncio.run(go())

    def add(self, name="home", **over):
        args = {"name": name, "base_url": BASE, "username": "alice", "password_env": "DAV_PW"}
        return self.call("webdav_add", **{**args, **over})


@pytest.fixture
def env(fresh_server, monkeypatch, tmp_path):
    monkeypatch.setenv("STORAGE_ENCRYPTION_KEY", Fernet.generate_key().decode())
    monkeypatch.setenv("INTERNAL_ALLOW_CIDRS", "203.0.113.0/24")
    dns, hub, mod = _Resolver(), _Hub(), sys.modules["webdav_tools"]
    dns.set(HOST, "203.0.113.10")
    monkeypatch.setattr(socket, "getaddrinfo", dns)
    monkeypatch.setattr(mod, "httpx", SimpleNamespace(Client=hub.build))
    root = tmp_path / "sandbox"  # stands in for /data
    (root / "work").mkdir(parents=True)
    (root / "f.txt").write_bytes(b"test-data")
    monkeypatch.setattr(mod, "DATA_ROOT", root.resolve())
    monkeypatch.setattr(mod, "WORK_DIR", root / "work")
    # the hosts these tests register — bound by a person, as only a person binds a secret (2.6.0)
    sys.modules["secrets_store"].vault_set("DAV_PW", PASSWORD, hosts=[HOST, "lan.example.net", "odd.example.net", "rebind.example.net"])
    return _Env(fresh_server, dns, hub, mod, root)


@pytest.fixture
def home(env):
    env.add()
    return env


def _ms(*items):
    return f'<?xml version="1.0"?><d:multistatus xmlns:d="DAV:">{"".join(items)}</d:multistatus>'


def _item(href, folder=False, size=None):
    rt = "<d:collection/>" if folder else ""
    sz = f"<d:getcontentlength>{size}</d:getcontentlength>" if size is not None else ""
    return (f"<d:response><d:href>{href}</d:href><d:propstat><d:prop>"
            f"<d:resourcetype>{rt}</d:resourcetype>{sz}</d:prop></d:propstat></d:response>")


ALL_TOOLS = [("webdav_list", {}), ("webdav_upload", {"source": "f.txt", "dest": "f.txt"}),
             ("webdav_download", {"path": "f.txt"}), ("webdav_mkdir", {"path": "d"}),
             ("webdav_delete", {"path": "f.txt"})]


# --- registration ---------------------------------------------------------
def test_add_normalises_slug_and_url_and_stores_only_the_secret_name(env):
    out = env.add("Cloud Drive!", base_url="https://dav.example.com/dav/alice")
    assert out == "Registered WebDAV endpoint 'cloud-drive'."  # secret exists: no hint
    assert env.add("!!!", password_env="UNSET_PW").startswith("Registered WebDAV endpoint 'webdav'.")
    assert "secret_set('UNSET_PW', <value>, hosts='dav.example.com')" in env.add(password_env="UNSET_PW")  # secret missing: store it with its host
    text = (env.mod.WEBDAV_DIR / "cloud-drive.json").read_text()
    assert '"base_url": "https://dav.example.com/dav/alice/"' in text
    assert '"password_env": "DAV_PW"' in text and PASSWORD not in text


def test_add_reports_an_unusable_store_instead_of_raising(env, tmp_path, monkeypatch):
    blocker = tmp_path / "not-a-dir"
    blocker.write_text("x")
    monkeypatch.setattr(env.mod, "WEBDAV_DIR", blocker)
    assert env.add().startswith("Could not register endpoint:")


def test_list_and_delete_endpoints_incl_unreadable_config(env):
    assert env.call("webdav_list_endpoints") == "No WebDAV endpoints yet. Use webdav_add."
    env.add(description="mine")
    (env.mod.WEBDAV_DIR / "broken.json").write_text("{not json")
    assert env.call("webdav_list_endpoints").splitlines() == [
        "- broken — (unreadable)", f"- home — {BASE} — mine"]
    assert env.call("webdav_list", endpoint="broken").startswith("Unknown endpoint")
    assert env.call("webdav_delete_endpoint", name="Home") == "Deleted WebDAV endpoint 'home'."
    assert env.call("webdav_delete_endpoint", name="home") == "No WebDAV endpoint 'home'."
    assert env.http.calls == []  # remote files are never touched


@pytest.mark.parametrize("tool,extra", ALL_TOOLS)
def test_unknown_endpoint_is_refused_for_every_tool(env, tool, extra):
    out = env.call(tool, endpoint="nope", **extra)
    assert out.startswith("Unknown endpoint 'nope'") and env.http.clients == []


# --- credentials and TLS ---------------------------------------------------
def test_password_is_read_from_vault_for_basic_auth_and_never_echoed(home):
    home.http.reply(207, _ms(_item(ROOT + "/", True)))
    out = home.call("webdav_list", endpoint="home")
    kw = home.http.clients[0]
    assert kw["auth"] == ("alice", PASSWORD) and kw["verify"] is True and kw["follow_redirects"]
    assert PASSWORD not in out + home.call("webdav_list_endpoints")


def test_missing_secret_stops_before_any_client_and_anonymous_endpoints_send_no_auth(env):
    env.add(password_env="UNSET_PW")
    assert env.call("webdav_list", endpoint="home") == "Endpoint needs secret 'UNSET_PW'. Store it with secret_set."
    assert env.http.clients == []
    env.add(username="", password_env="")
    env.call("webdav_list", endpoint="home")
    assert env.http.clients[0]["auth"] is None


@pytest.mark.parametrize("over,expected", [
    ({"tls_insecure": True}, False),
    ({"tls_insecure": True, "ca_bundle": "/certs/lan-ca.pem"}, "/certs/lan-ca.pem"),
])
def test_tls_options_reach_the_client(env, over, expected):
    env.add(**over)
    env.call("webdav_list", endpoint="home")
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
    out = env.call("webdav_list", endpoint="home")
    assert out.startswith("Blocked by network policy") and fragment in out


@pytest.mark.parametrize("tool,extra,prefix", [
    ("webdav_list", {}, "List failed:"), ("webdav_upload", {"source": "f.txt", "dest": "x"}, "Upload failed:"),
    ("webdav_download", {"path": "x"}, "Download failed:"), ("webdav_mkdir", {"path": "d"}, "mkdir failed:"),
    ("webdav_delete", {"path": "x"}, "Delete failed:")])
def test_connect_time_guard_rejects_a_dns_rebinding_answer(env, tool, extra, prefix):
    env.dns.set("rebind.example.net", "203.0.113.20", "10.0.0.9")  # check, then connect
    env.add(base_url="https://rebind.example.net/dav/")
    out = env.call(tool, endpoint="home", **extra)
    assert out.startswith(prefix) and "internal/blocked at connect" in out and env.http.calls == []


# --- list ------------------------------------------------------------------
def test_list_sends_propfind_and_formats_names_sizes_and_folders(home):
    home.http.reply(207, _ms(
        _item(f"{ROOT}/Docs/", True),  # the folder itself: skipped
        _item(f"{ROOT}/Docs/report%20q1.pdf", size=2048),
        _item(f"{ROOT}/Docs/Photos/", True),
        _item(f"https://{HOST}{ROOT}/Docs/notes.txt", size=12)))  # absolute href
    out = home.call("webdav_list", endpoint="home", path="/Docs/")
    call = home.http.calls[0]
    assert (call.method, call.url, call.headers["Depth"]) == ("PROPFIND", BASE + "Docs/", "1")
    assert out.splitlines() == ["- report q1.pdf · 2048 B", "- Photos/", "- notes.txt · 12 B"]
    assert home.http.closed == 1


def test_list_root_skips_the_root_and_reports_an_empty_folder(home):
    home.http.reply(207, _ms(_item(ROOT + "/", True)))
    assert home.call("webdav_list", endpoint="home") == "(empty)"
    assert home.http.calls[0].url == BASE


@pytest.mark.parametrize("status,body,expected", [
    (404, "", "List returned HTTP 404. Check path / credentials."),
    (200, "<d:multistatus xmlns:d='DAV:'><oops>", "Could not parse listing:"),
])
def test_list_error_and_malformed_answers(home, status, body, expected):
    home.http.reply(status, body)
    assert home.call("webdav_list", endpoint="home").startswith(expected)


def test_transport_errors_are_returned_not_raised(home):
    home.http.error = ConnectionError("connection refused")
    why = "ConnectionError (could not connect; details in the server log)"   # never the text
    assert home.call("webdav_list", endpoint="home") == f"List failed: {why}"
    assert home.call("webdav_mkdir", endpoint="home", path="d") == f"mkdir failed: {why}"
    assert home.call("webdav_delete", endpoint="home", path="d") == f"Delete failed: {why}"
    assert home.call("webdav_download", endpoint="home", path="d") == f"Download failed: {why}"
    assert home.call("webdav_upload", endpoint="home", source="f.txt", dest="d") == (
        f"Upload failed: {why}")


# --- upload ----------------------------------------------------------------
def test_upload_puts_the_file_body_to_base_url_plus_dest(home):
    home.http.reply(201)
    out = home.call("webdav_upload", endpoint="home", source="f.txt", dest="/Docs/scan.pdf")
    call = home.http.calls[0]
    assert (call.method, call.url, call.body) == ("PUT", BASE + "Docs/scan.pdf", b"test-data")
    assert out == "Uploaded f.txt → /Docs/scan.pdf (9 B)."


def test_upload_accepts_an_absolute_path_inside_the_sandbox_and_hints_on_conflict(home):
    home.http.reply(409)
    out = home.call("webdav_upload", endpoint="home", source=str(home.root / "f.txt"), dest="Missing/f.txt")
    assert out == "Upload returned HTTP 409 (check the dest folder exists — webdav_mkdir)."


def test_upload_refuses_paths_outside_the_data_sandbox_before_connecting(home, tmp_path):
    (home.root.parent / "sandbox-backup").mkdir()  # sibling sharing the string prefix
    (home.root.parent / "sandbox-backup" / "x.txt").write_text("x")
    (tmp_path / "elsewhere.txt").write_text("secret")
    (home.root / "link.txt").symlink_to(tmp_path / "elsewhere.txt")
    for source in (str(tmp_path / "elsewhere.txt"), "../elsewhere.txt", "../sandbox-backup/x.txt", "link.txt",
                   "/etc/hosts"):
        assert home.call("webdav_upload", endpoint="home", source=source, dest="x") == "Source must be under /data."
    assert home.http.clients == []


def test_upload_reports_a_missing_source_file(home):
    out = home.call("webdav_upload", endpoint="home", source="nope.pdf", dest="x")
    assert out == f"No file at '{home.root.resolve() / 'nope.pdf'}'." and home.http.clients == []


# --- download --------------------------------------------------------------
def test_download_streams_into_the_work_dir_under_the_remote_name(home):
    home.http.reply(200, b"hello world")
    out = home.call("webdav_download", endpoint="home", path="/Docs/report.pdf")
    target = home.root / "work" / "report.pdf"
    call = home.http.calls[0]
    assert (call.method, call.url) == ("GET", BASE + "Docs/report.pdf")
    assert target.read_bytes() == b"hello world"  # chunks reassembled
    assert out == f"Downloaded /Docs/report.pdf → {target} (11 B)."


@pytest.mark.parametrize("path,dest,name", [
    ("Docs/a.txt", "../../evil name.txt", ".._.._evil_name.txt"),
    ("Docs/", "", "Docs"), ("", "", "download")])
def test_download_sanitises_the_local_file_name(home, tmp_path, path, dest, name):
    home.http.reply(200, b"abcdefgh")
    home.call("webdav_download", endpoint="home", path=path, dest=dest)
    assert sorted(p.name for p in (home.root / "work").iterdir()) == [name]
    assert not (home.root / "evil name.txt").exists() and not (tmp_path / "evil_name.txt").exists()


def test_download_refuses_a_dest_that_resolves_to_a_directory(home):
    home.http.reply(200, b"abcdefgh")
    assert home.call("webdav_download", endpoint="home", path="a.txt", dest="..").startswith("Download failed:")
    assert [p.name for p in home.root.iterdir() if p.is_file()] == ["f.txt"]  # nothing written in the data root


def test_download_error_status_writes_no_file(home):
    home.http.reply(404)
    assert home.call("webdav_download", endpoint="home", path="gone.txt") == (
        "Download returned HTTP 404. Check the path.")
    assert list((home.root / "work").iterdir()) == []


# --- mkdir / delete ----------------------------------------------------------
def test_mkdir_uses_mkcol_with_a_trailing_slash_and_maps_statuses(home):
    home.http.reply(201)
    assert home.call("webdav_mkdir", endpoint="home", path="/Docs/2026/") == "Folder '/Docs/2026/' ready."
    assert (home.http.calls[0].method, home.http.calls[0].url) == ("MKCOL", BASE + "Docs/2026/")
    home.http.reply(405)  # already exists
    assert home.call("webdav_mkdir", endpoint="home", path="Docs") == "Folder 'Docs' ready."
    home.http.reply(409)
    assert home.call("webdav_mkdir", endpoint="home", path="a/b") == "mkdir returned HTTP 409."


def test_delete_maps_statuses_and_targets_base_url_plus_path(home):
    home.http.reply(204)
    assert home.call("webdav_delete", endpoint="home", path="/Docs/old.txt") == "Deleted '/Docs/old.txt'."
    assert (home.http.calls[0].method, home.http.calls[0].url) == ("DELETE", BASE + "Docs/old.txt")
    home.http.reply(404)  # already gone counts as deleted
    assert home.call("webdav_delete", endpoint="home", path="gone") == "Deleted 'gone'."
    home.http.reply(403)
    assert home.call("webdav_delete", endpoint="home", path="locked") == "Delete returned HTTP 403."


# --- known defect (strict xfail: flips to a failure once fixed) ------------------
def test_list_skips_the_folder_itself_when_the_base_url_is_percent_encoded(env):
    encoded_root = "/remote.php/dav/files/alice%40example.com"
    env.add(base_url=f"https://{HOST}{encoded_root}/")
    env.http.reply(207, _ms(_item(encoded_root + "/", True), _item(encoded_root + "/a.txt", size=5)))
    assert env.call("webdav_list", endpoint="home") == "- a.txt · 5 B"
