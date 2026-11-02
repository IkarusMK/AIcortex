"""Small hardening of FTP and IMAP found while raising test coverage.

- FTP logged in with an EMPTY password when the vault secret named in password_env
  was missing (SSH, mail and IMAP refuse with "needs secret").
- IMAP built its TLS context outside the error handling, so an unreadable ca_bundle
  surfaced as a raw tool error instead of "IMAP connect/login failed".
- Saving IMAP attachments: one named "." or ".." made the whole fetch fail, and two
  attachments with the same name silently overwrote each other.
"""
from test_cov_ftp import _add as _ftp_add
from test_cov_ftp import srv as ftp_srv  # noqa: F401 - fixture
from test_cov_ftp import world as ftp_world  # noqa: F401 - fixture
from test_cov_ftp import _call as _ftp_call
from test_cov_imap import _add as _imap_add
from test_cov_imap import _call as _imap_call
from test_cov_imap import _full, _mail, _vault
from test_cov_imap import srv, world  # noqa: F401 - fixtures


def test_ftp_with_a_missing_password_secret_does_not_log_in(ftp_srv, ftp_world):
    _ftp_add(ftp_srv, password_env="FTP_PW")
    out = _ftp_call(ftp_srv, "ftp_list", {"server": "files"})
    assert "needs secret 'FTP_PW'" in out
    assert all(not s.logins for s in ftp_world.sessions)


def test_imap_with_an_unreadable_ca_bundle_reports_a_connect_failure(srv, world, tmp_path):
    _imap_add(srv, ca_bundle=str(tmp_path / "missing.pem"))
    out = _imap_call(srv, "imap_search", {"account": "inbox"})
    assert out.startswith("IMAP connect/login failed") and world.sessions == []


def test_odd_and_duplicate_attachment_names_are_all_saved(srv, world, tmp_path):
    _imap_add(srv, password_env="IMAP_PW")
    _vault()
    msg = _mail(**{"..": b"first"})
    msg.add_attachment(b"second", maintype="application", subtype="pdf", filename="report.pdf")
    msg.add_attachment(b"third", maintype="application", subtype="pdf", filename="report.pdf")
    world.fetches = {"7": _full(msg)}
    out = _imap_call(srv, "imap_fetch", {"account": "inbox", "uid": "7", "save_attachments": True})
    assert not out.startswith("Fetch failed"), out
    work = tmp_path / "work_dir"
    saved = sorted(p.read_bytes() for p in work.iterdir() if p.is_file())
    assert saved == [b"first", b"second", b"third"]
    assert not (tmp_path / "..").is_file()
