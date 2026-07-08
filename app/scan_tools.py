"""Network scanning via eSCL (AirScan / Mopria) — scanners as DATA.

The scan-side counterpart of print_tools.py. eSCL is the open, vendor-neutral
scan protocol behind Apple AirScan and Mopria, supported by virtually all modern
multifunction devices (Epson, HP, Canon, Brother …). A scan job is:

  1. POST ScanSettings XML to {base}/ScanJobs  -> 201 + Location header (job URL)
  2. GET  {Location}/NextDocument               -> the scanned bytes (PDF/JPEG)
     (repeat for multi-page ADF until 404)

Only registered scanners are reachable and the host passes the SSRF egress guard
(same LAN device as the printer, so it's already allow-listed). The scan is saved
under /data/work and can optionally be pushed straight into Paperless-ngx.
"""
import json

import cfgstore
import os
import re
import time
from datetime import datetime, timezone
from pathlib import Path

import httpx

import netguard
import secrets_store
import services as services_mod

SCAN_DIR = Path(os.environ.get("SCAN_DIR", "/data/scanners"))
WORK_DIR = Path(os.environ.get("WORK_DIR", "/data/work"))

# eSCL value maps (friendly -> protocol)
_COLOR = {"color": "RGB24", "gray": "Grayscale8", "grayscale": "Grayscale8",
          "bw": "BlackAndWhite1", "mono": "BlackAndWhite1"}
_SOURCE = {"platen": "Platen", "flatbed": "Platen", "glass": "Platen",
           "adf": "Feeder", "feeder": "Feeder"}
_FORMAT = {"pdf": "application/pdf", "jpeg": "image/jpeg", "jpg": "image/jpeg"}
_EXT = {"application/pdf": "pdf", "image/jpeg": "jpg"}
# A4 at 1/300-inch units (≈ 210 × 297 mm).
_A4_W, _A4_H = 2480, 3508


def _slug(name: str) -> str:
    s = re.sub(r"[^a-z0-9]+", "-", (name or "").lower()).strip("-")
    return s[:60] or "scanner"


def _cfg_path(name: str) -> Path:
    return SCAN_DIR / f"{_slug(name)}.json"


def _load(name: str):
    p = _cfg_path(name)
    if not p.exists():
        return None
    try:
        return json.loads(p.read_text(encoding="utf-8"))
    except Exception:
        return None


def _settings_xml(color: str, source: str, fmt: str, resolution: int) -> str:
    return (
        '<?xml version="1.0" encoding="UTF-8"?>'
        '<scan:ScanSettings xmlns:scan="http://schemas.hp.com/imaging/escl/2011/05/03"'
        ' xmlns:pwg="http://www.pwg.org/schemas/2010/12/sm">'
        '<pwg:Version>2.0</pwg:Version>'
        '<scan:Intent>Document</scan:Intent>'
        '<pwg:ScanRegions><pwg:ScanRegion>'
        f'<pwg:Height>{_A4_H}</pwg:Height><pwg:Width>{_A4_W}</pwg:Width>'
        '<pwg:XOffset>0</pwg:XOffset><pwg:YOffset>0</pwg:YOffset>'
        '<pwg:ContentRegionUnits>escl:ThreeHundredthsOfInches</pwg:ContentRegionUnits>'
        '</pwg:ScanRegion></pwg:ScanRegions>'
        f'<scan:InputSource>{source}</scan:InputSource>'
        f'<scan:ColorMode>{color}</scan:ColorMode>'
        f'<scan:DocumentFormatExt>{fmt}</scan:DocumentFormatExt>'
        f'<scan:XResolution>{resolution}</scan:XResolution>'
        f'<scan:YResolution>{resolution}</scan:YResolution>'
        '</scan:ScanSettings>'
    )


def _candidates(cfg: dict):
    """(scheme, port) transports to try. eSCL devices use http:80 or https:443;
    if the config pins port/tls, use only that."""
    base = cfg.get("base", "/eSCL").rstrip("/")
    port = int(cfg.get("port", 0))
    tls = cfg.get("tls", "auto")
    if port and tls in (True, "true", "https"):
        return [("https", port, base)]
    if port and tls in (False, "false", "http", "none"):
        return [("http", port, base)]
    if port:  # port set, tls auto → try https then http on that port
        return [("https", port, base), ("http", port, base)]
    # nothing pinned: the common eSCL endpoints
    return [("https", 443, base), ("http", 80, base)]


def _looks_like_tls_error(exc) -> bool:
    """True if an exception (walking its cause/context chain) is a TLS/certificate
    verification failure — so we can hint at a self-signed device."""
    seen = set()
    e = exc
    while e is not None and id(e) not in seen:
        seen.add(id(e))
        s = f"{type(e).__name__} {e}".lower()
        if any(k in s for k in ("certificate", "ssl", "verify failed")):
            return True
        e = getattr(e, "__cause__", None) or getattr(e, "__context__", None)
    return False


def register(mcp):
    @mcp.tool
    def scan_add(name: str, host: str, port: int = 0, base: str = "/eSCL",
                 tls: str = "auto", ca_bundle: str = "", tls_insecure: bool = False,
                 description: str = "") -> str:
        """Register/update a network scanner as DATA (no redeploy). host = the
        device's LAN IP (same as the printer for a multifunction). Leave port=0 /
        tls="auto" to auto-detect (tries https:443 then http:80, eSCL base /eSCL).
        The IP must be inside INTERNAL_ALLOW_CIDRS.

        TLS is VERIFIED by default. For a self-signed device, either point
        `ca_bundle` at its CA/cert file (the safe way), or set `tls_insecure=true`
        to skip verification (this admin-only choice is stored on the scanner; a
        self-signed https-only device needs one of these or it falls back to http)."""
        try:
            SCAN_DIR.mkdir(parents=True, exist_ok=True)
            cfg = {"name": name, "host": host, "port": int(port),
                   "base": base or "/eSCL", "tls": tls,
                   "ca_bundle": ca_bundle, "tls_insecure": bool(tls_insecure),
                   "description": description}
            cfgstore.write_merged(_cfg_path(name), cfg)
            mode = ("CA bundle" if ca_bundle else
                    ("verify OFF" if tls_insecure else "verified TLS"))
            return f"Registered scanner '{_slug(name)}' ({host}, base {cfg['base']}, {mode})."
        except Exception as exc:
            return f"Could not register scanner: {exc}"

    @mcp.tool
    def scan_list() -> str:
        """List registered scanners (name — host — description)."""
        if not SCAN_DIR.exists() or not any(SCAN_DIR.glob("*.json")):
            return "No scanners configured yet. Use scan_add."
        out = []
        for p in sorted(SCAN_DIR.glob("*.json")):
            try:
                c = json.loads(p.read_text(encoding="utf-8"))
                out.append(f"- {p.stem} — {c.get('host')} — {c.get('description', '')}")
            except Exception:
                out.append(f"- {p.stem} — (unreadable config)")
        return "\n".join(out)

    @mcp.tool
    def scan_delete(name: str) -> str:
        """Remove a registered scanner by name."""
        p = _cfg_path(name)
        if p.exists():
            p.unlink()
            return f"Deleted scanner '{_slug(name)}'."
        return f"No scanner '{name}'."

    @mcp.tool
    def scan_document(scanner: str, resolution: int = 300, color: str = "color",
                      source: str = "platen", format: str = "pdf",
                      filename: str = "", paperless: str = "") -> str:
        """Scan a document via eSCL and save it under /data/work. color: color|gray|bw.
        source: platen (flatbed) | adf (feeder). format: pdf | jpeg. Optionally push
        the result straight into Paperless: pass paperless=<a service name registered
        with service_add> (base_url of your Paperless, token_env -> API token in the
        vault); the scan is uploaded to its /api/documents/post_document/.

        To SEE the scan afterwards, call fs_view('<the saved filename>') — it renders the
        page as an image the assistant reads with vision (don't push private scans through
        Paperless just to OCR them)."""
        cfg = _load(scanner)
        if not cfg:
            return f"Unknown scanner '{scanner}'. Use scan_list / scan_add."
        host = cfg["host"]
        ok, reason = netguard.check_host(host)
        if not ok:
            return f"Blocked by network policy — {reason}"

        cmode = _COLOR.get((color or "color").lower(), "RGB24")
        src = _SOURCE.get((source or "platen").lower(), "Platen")
        fmt = _FORMAT.get((format or "pdf").lower(), "application/pdf")
        xml = _settings_xml(cmode, src, fmt, int(resolution))

        # 1) Create the scan job (try the candidate transports until one accepts).
        job_url = None
        attempts = []          # every transport we tried + why it failed
        tls_failed = False     # an https attempt failed cert verification
        for scheme, port, base in _candidates(cfg):
            url = f"{scheme}://{host}:{port}{base}/ScanJobs"
            try:
                # guard() re-applies the egress policy at connect (anti-rebinding).
                with netguard.guard(host):
                    r = httpx.post(url, content=xml.encode("utf-8"),
                                   headers={"Content-Type": "text/xml"},
                                   timeout=120, verify=netguard.tls_verify(cfg))
            except Exception as exc:
                if scheme == "https" and _looks_like_tls_error(exc):
                    tls_failed = True
                    attempts.append(f"{scheme}:{port} → TLS/certificate verification failed")
                else:
                    attempts.append(f"{scheme}:{port} → {type(exc).__name__}: {exc}")
                continue
            if r.status_code in (200, 201) and r.headers.get("Location"):
                job_url = r.headers["Location"]
                # Some devices return a relative/incorrect host — rebuild on our base.
                if job_url.startswith("/"):
                    job_url = f"{scheme}://{host}:{port}{job_url}"
                transport = (scheme, port)
                break
            attempts.append(f"{scheme}:{port} → HTTP {r.status_code}")
        if not job_url:
            detail = "; ".join(attempts) or "no transports available"
            hint = ""
            if tls_failed and not cfg.get("tls_insecure") and not cfg.get("ca_bundle"):
                hint = (" The HTTPS attempt failed certificate verification — this is "
                        "almost always a self-signed scanner: re-register it with "
                        f"scan_add('{scanner}', '{host}', tls_insecure=true) (or point "
                        "ca_bundle at its cert).")
            else:
                hint = (" Check the IP/base (eSCL path is often /eSCL, port 80 or 443) "
                        "and that network scanning (eSCL/AirScan) is enabled on the device.")
            return f"Scanner '{scanner}' did not start a job. Tried: {detail}.{hint}"

        # 2) Pull the scanned document (first page; ADF could yield more).
        doc = None
        for _ in range(3):
            try:
                with netguard.guard(host):
                    rd = httpx.get(f"{job_url.rstrip('/')}/NextDocument", timeout=180,
                                   verify=netguard.tls_verify(cfg))
            except Exception as exc:
                return f"Scan started but fetching the page failed: {exc}"
            if rd.status_code == 200 and rd.content:
                doc = rd.content
                break
            if rd.status_code == 404:
                break
            time.sleep(2)  # device may still be warming up / scanning
        if not doc:
            return "Scan job created but no document came back (empty/timeout)."

        # 3) Save under /data/work.
        WORK_DIR.mkdir(parents=True, exist_ok=True)
        ext = _EXT.get(fmt, "pdf")
        ts = datetime.now(timezone.utc).strftime("%Y%m%d-%H%M%S")
        name = filename or f"scan-{ts}.{ext}"
        name = re.sub(r"[^A-Za-z0-9._-]+", "_", name)
        out_path = WORK_DIR / name
        out_path.write_bytes(doc)
        result = f"Scanned {len(doc)} bytes → {out_path}."

        # 4) Optional: push into Paperless (multipart upload, token from the vault).
        if paperless:
            svc = services_mod._load(paperless)
            if not svc:
                return result + f" (Paperless service '{paperless}' not found — register it with service_add.)"
            base_url = svc.get("base_url", "").rstrip("/")
            up_url = f"{base_url}/api/documents/post_document/"
            ok2, reason2 = netguard.check_url(up_url)
            if not ok2:
                return result + f" (Paperless upload blocked by network policy — {reason2}.)"
            headers = {}
            tok_env = svc.get("token_env")
            if tok_env:
                token = secrets_store.get_secret(tok_env)
                if not token:
                    return result + f" (Paperless needs secret '{tok_env}' — set it with secret_set.)"
                headers["Authorization"] = f"Token {token}"
            try:
                from urllib.parse import urlparse as _urlparse
                with netguard.guard(_urlparse(up_url).hostname or ""):
                    ru = httpx.post(up_url, headers=headers,
                                    files={"document": (name, doc, fmt)}, timeout=180)
                if ru.status_code in (200, 201):
                    return result + f" Uploaded to Paperless ('{paperless}')."
                return result + f" Paperless upload returned HTTP {ru.status_code}."
            except Exception as exc:
                return result + f" Paperless upload failed: {exc}"
        return result
