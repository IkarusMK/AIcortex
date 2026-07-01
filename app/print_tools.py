"""Generic IPP printing — printers as DATA, documents printed on the LAN.

Like services.py (HTTP) / mqtt_tools.py (MQTT), this lets a network printer be
added at RUNTIME as a config (its IP) so documents can be printed without new
code. It speaks IPP (Internet Printing Protocol, the basis of AirPrint /
IPP Everywhere) directly over HTTP on port 631 — no system print stack, no extra
dependency beyond httpx (already used).

``print_document`` sends a PDF (or image) either from a file under /data or as
inline base64 (so a document handed to the assistant in chat reaches the NAS as
a tool argument and prints). Only registered printers are reachable and every
target passes the SSRF egress guard, so the printer IP must sit inside
INTERNAL_ALLOW_CIDRS (e.g. your LAN range).
"""
import base64
import json

import cfgstore
import os
import re
import struct
from pathlib import Path

import httpx

import netguard

PRINT_DIR = Path(os.environ.get("PRINT_DIR", "/data/printers"))
DATA_ROOT = Path(os.environ.get("DATA_ROOT", "/data")).resolve()
# Max document size accepted for a single print job (default 20 MB). Guards
# against loading a huge payload into memory and printer-spool abuse.
_MAX_DOC = int(os.environ.get("PRINT_MAX_BYTES", str(20_000_000)))

# IPP value tags (RFC 8011 §5.1.x)
_TAG_CHARSET = 0x47
_TAG_NATURAL_LANG = 0x48
_TAG_URI = 0x45
_TAG_NAME = 0x42
_TAG_MIME = 0x49
_OP_PRINT_JOB = 0x0002

# Common IPP status codes → human meaning (RFC 8011 §13.1), for clearer errors.
_IPP_STATUS_NAMES = {
    0x0400: "bad-request", 0x0401: "forbidden", 0x0403: "not-authorized",
    0x0405: "not-possible", 0x0408: "request-value-too-long",
    0x040A: "document-format-not-supported",
    0x040C: "attributes-or-values-not-supported",
    0x0500: "internal-error", 0x0501: "operation-not-supported",
    0x0503: "service-unavailable", 0x0509: "busy",
}


def _slug(name: str) -> str:
    s = re.sub(r"[^a-z0-9]+", "-", (name or "").lower()).strip("-")
    return s[:60] or "printer"


def _cfg_path(name: str) -> Path:
    return PRINT_DIR / f"{_slug(name)}.json"


def _load(name: str):
    p = _cfg_path(name)
    if not p.exists():
        return None
    try:
        return json.loads(p.read_text(encoding="utf-8"))
    except Exception:
        return None


def _attr(tag: int, name: str, value: str) -> bytes:
    """Encode one IPP attribute: value-tag, name-len, name, value-len, value."""
    n = name.encode("utf-8")
    v = value.encode("utf-8")
    return bytes([tag]) + struct.pack(">H", len(n)) + n + struct.pack(">H", len(v)) + v


def _build_print_job(printer_uri: str, job_name: str, doc_format: str, data: bytes) -> bytes:
    """Build an IPP 2.0 Print-Job request (operation attributes + document data)."""
    out = bytearray()
    out += bytes([0x02, 0x00])             # version 2.0
    out += struct.pack(">H", _OP_PRINT_JOB)
    out += struct.pack(">I", 1)            # request-id
    out += bytes([0x01])                   # operation-attributes-tag
    # Order matters: charset, then natural-language, then the rest.
    out += _attr(_TAG_CHARSET, "attributes-charset", "utf-8")
    out += _attr(_TAG_NATURAL_LANG, "attributes-natural-language", "en")
    out += _attr(_TAG_URI, "printer-uri", printer_uri)
    out += _attr(_TAG_NAME, "requesting-user-name", "aicortex")
    if job_name:
        out += _attr(_TAG_NAME, "job-name", job_name)
    out += _attr(_TAG_MIME, "document-format", doc_format)
    out += bytes([0x03])                   # end-of-attributes-tag
    out += data
    return bytes(out)


def _ipp_status(resp: bytes):
    """The IPP status-code is bytes 2-3 of the response. Success ≤ 0x00FF."""
    if len(resp) < 8:
        return None
    return struct.unpack(">H", resp[2:4])[0]


def register(mcp):
    @mcp.tool
    def print_add(name: str, host: str, port: int = 631, path: str = "/ipp/print",
                  description: str = "") -> str:
        """Register/update a network printer as DATA (no redeploy). host = the
        printer's LAN IP. Most modern printers expose IPP Everywhere/AirPrint at
        port 631, path /ipp/print (some use /ipp or /). The IP must be inside
        INTERNAL_ALLOW_CIDRS or prints are blocked by the network policy."""
        try:
            PRINT_DIR.mkdir(parents=True, exist_ok=True)
            cfg = {"name": name, "host": host, "port": int(port),
                   "path": path or "/ipp/print", "description": description}
            cfgstore.write_merged(_cfg_path(name), cfg)
            return f"Registered printer '{_slug(name)}' ({host}:{port}{cfg['path']})."
        except Exception as exc:
            return f"Could not register printer: {exc}"

    @mcp.tool
    def print_list() -> str:
        """List registered printers (name — host:port — description)."""
        if not PRINT_DIR.exists():
            return "No printers configured yet. Use print_add."
        items = sorted(PRINT_DIR.glob("*.json"))
        if not items:
            return "No printers configured yet. Use print_add."
        out = []
        for p in items:
            try:
                c = json.loads(p.read_text(encoding="utf-8"))
                out.append(f"- {p.stem} — {c.get('host')}:{c.get('port')} — {c.get('description','')}")
            except Exception:
                out.append(f"- {p.stem} — (unreadable config)")
        return "\n".join(out)

    @mcp.tool
    def print_delete(name: str) -> str:
        """Remove a registered printer by name."""
        p = _cfg_path(name)
        if p.exists():
            p.unlink()
            return f"Deleted printer '{_slug(name)}'."
        return f"No printer '{name}'."

    @mcp.tool
    def print_document(printer: str, file: str = "", content_base64: str = "",
                       document_format: str = "application/pdf",
                       job_name: str = "") -> str:
        """Print a document on a registered printer via IPP. Provide EITHER
        file=<path under /data> OR content_base64=<the document bytes, base64>
        (use this to print something handed to you in chat). document_format
        defaults to application/pdf (use image/jpeg for photos). STATE-CHANGING /
        physical output — confirm with the user before printing."""
        cfg = _load(printer)
        if not cfg:
            return f"Unknown printer '{printer}'. Use print_list / print_add."
        host = cfg["host"]
        port = int(cfg.get("port", 631))
        path = cfg.get("path", "/ipp/print")

        ok, reason = netguard.check_host(host)
        if not ok:
            return f"Blocked by network policy — {reason} (add the printer's range to INTERNAL_ALLOW_CIDRS)."

        if content_base64:
            try:
                data = base64.b64decode(content_base64)
            except Exception:
                return "content_base64 is not valid base64."
            if len(data) > _MAX_DOC:
                return f"Refused: document is {len(data)} bytes, over the {_MAX_DOC}-byte print limit."
        elif file:
            p = Path(file)
            if not p.is_absolute():
                p = DATA_ROOT / file
            p = p.resolve()
            # Ancestry check, not a string prefix: startswith("/data") would also
            # match a sibling like /data-backup and let a path escape the sandbox.
            if p != DATA_ROOT and DATA_ROOT not in p.parents:
                return "File must be under /data."
            if not p.is_file():
                return f"No file at '{p}'."
            # Check size before reading so a huge file can't exhaust memory.
            if p.stat().st_size > _MAX_DOC:
                return f"Refused: '{p.name}' is {p.stat().st_size} bytes, over the {_MAX_DOC}-byte print limit."
            data = p.read_bytes()
            if not job_name:
                job_name = p.name
        else:
            return "Provide file=<path under /data> or content_base64=<document bytes>."

        if not data:
            return "Empty document — nothing to print."

        def _send(scheme: str, ipp_scheme: str, fmt: str):
            # The IPP printer-uri inside the body should match the transport.
            p_uri = f"{ipp_scheme}://{host}:{port}{path}"
            body = _build_print_job(p_uri, job_name or "aicortex-job", fmt, data)
            url = f"{scheme}://{host}:{port}{path}"
            # verify=False: printers use self-signed certs; this is a LAN device the
            # operator allow-listed, and IPP carries no credential here.
            # guard() re-applies the egress policy at connect (anti-rebinding).
            with netguard.guard(host):
                return httpx.post(url, content=body,
                                  headers={"Content-Type": "application/ipp"},
                                  timeout=60, verify=False)

        scheme, ipp_scheme = "http", "ipp"
        try:
            r = _send(scheme, ipp_scheme, document_format)
            # HTTP 426 "Upgrade Required" = the printer demands TLS (IPPS).
            # Many modern printers require it (common across vendors) — retry over HTTPS.
            if r.status_code == 426:
                scheme, ipp_scheme = "https", "ipps"
                r = _send(scheme, ipp_scheme, document_format)
            # IPP 0x040A = document-format-not-supported. Some printers reject an
            # explicit application/pdf but accept auto-sensed octet-stream — so
            # printing "just works" without the caller picking a format.
            if (_ipp_status(r.content) == 0x040A
                    and document_format != "application/octet-stream"):
                r = _send(scheme, ipp_scheme, "application/octet-stream")
        except Exception as exc:
            return (f"Print request failed ({type(exc).__name__}: {exc}). "
                    f"Check that the printer is powered on and reachable at "
                    f"{host}:{port}{path} — try port 631 and path /ipp/print (some use "
                    f"/ipp or /) — and that {host} is inside INTERNAL_ALLOW_CIDRS.")

        status = _ipp_status(r.content)
        if r.status_code == 200 and status is not None and status <= 0x00FF:
            return (f"Sent '{job_name or 'document'}' to printer '{printer}' "
                    f"(IPP status 0x{status:04x}) — it should print now.")
        shown = f"0x{status:04x}" if status is not None else "unknown"
        named = _IPP_STATUS_NAMES.get(status)
        meaning = f" ({named})" if named else ""
        extra = ""
        if named == "document-format-not-supported":
            extra = " The printer rejected the document format — try document_format=application/octet-stream."
        elif status is None:
            extra = " No IPP response — likely wrong path; try /ipp or /."
        return (f"Printer '{printer}' did not accept the job (HTTP {r.status_code}, "
                f"IPP status {shown}{meaning}).{extra} Check the IP/path (try /ipp or /) "
                f"and that the format ({document_format}) is supported.")
