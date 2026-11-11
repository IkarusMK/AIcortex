"""What a tool tells the model when an outbound request fails.

A library's exception text can carry what the model — and whoever steers it with a
prompt injection — must not see: an internal address or port, a file path, a URL or
header with credentials, parts of a server's reply. The outbound tools therefore
answer with the exception's TYPE and a short, generic reason (``safe``); the full
text goes to the server log (logger "aicortex.outbound") for the operator.

Messages AICortex writes itself for the caller are passed on as they are: a
``Reportable`` exception, and the network policy's refusals (``netguard``).
"""
import logging
import socket

_log = logging.getLogger("aicortex.outbound")


class Reportable(Exception):
    """A failure whose message AICortex wrote for the caller — safe to show."""


# (fragments of an exception class's "module.qualname", reason) — checked along the
# class's MRO, the first match wins. Generic on purpose: what failed, not the details.
_REASONS = (
    (("timeout", "timedout"), "timed out"),
    (("badhostkey",), "the host key does not match the pinned one"),
    (("ssl", "certificate"), "TLS certificate verification or handshake failed"),
    (("gaierror",), "the host name did not resolve"),
    (("authentication", "auth"), "authentication failed"),
    (("connectionrefused",), "connection refused"),
    (("reset", "aborted", "brokenpipe", "remoteprotocol", "incompleteread"),
     "the connection was closed unexpectedly"),
    (("connect", "unreachable", "network"), "could not connect"),
    (("parseerror", "expat", "decode", "jsondecode"), "the server's answer could not be read"),
    (("imaplib",), "the IMAP server refused the request"),
    (("smtplib",), "the mail server refused the request"),
    (("ftplib",), "the FTP server refused the request"),
    (("paramiko", "sftp"), "the SSH server refused the request"),
    (("filenotfound",), "a file or program was not found"),
    (("permission",), "permission denied"),
)
_DEFAULT = "details in the server log"


def _reportable(exc: BaseException) -> bool:
    if isinstance(exc, Reportable):
        return True
    text = str(exc)
    try:
        import netguard
        if isinstance(exc, netguard.Blocked):
            return True
    except ImportError:
        pass
    # The connect-time filter (netguard._filtered_getaddrinfo) refuses with a gaierror
    # of its own wording; a resolver's own gaierror is a library text like any other.
    return isinstance(exc, socket.gaierror) and text.startswith("netguard:")


def _reason(exc: BaseException) -> str:
    names = [f"{c.__module__}.{c.__qualname__}".lower() for c in type(exc).__mro__]
    for fragments, reason in _REASONS:
        if any(f in name for name in names for f in fragments):
            return reason
    return _DEFAULT


def safe(exc: BaseException, action: str = "") -> str:
    """What to tell the caller about `exc`: its message when AICortex wrote it for the
    caller, else "<Type> (<generic reason>; details in the server log)". The full
    text is logged either way. `action` names the step, for the log line."""
    _log.warning("%s failed: %s: %s", action or "outbound request", type(exc).__name__, exc)
    if _reportable(exc):
        return str(exc)
    reason = _reason(exc)
    tail = "" if reason == _DEFAULT else "; details in the server log"
    return f"{type(exc).__qualname__} ({reason}{tail})"
