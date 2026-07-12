"""Admin WebUI — manage the vault, skills, and users from a browser.

Served by the SAME process alongside ``/mcp`` (like ``/hooks`` and ``/api``):
``GET /ui`` is a self-contained static app (no build step, no CDNs) talking to a
small JSON API under ``/ui/api/*``. Nothing here duplicates business logic — the
endpoints call the same module-level functions the MCP tools use
(``secrets_store.vault_*``, ``skills._*`` helpers, ``tenancy._write_policy``), so
UI and assistant can never drift apart.

AUTH — the browser flow mirrors the server's own model:
- OIDC configured (OIDC_CONFIG_URL + OIDC_CLIENT_ID): a standard authorization-
  code flow WITH PKCE against the same IdP (e.g. Pocket ID). The IdP needs ONE
  extra redirect URI: ``<BASE_URL>/ui/callback``. The resulting session is a
  signed, HttpOnly cookie; the role comes from ``authz.role_for`` (policy.json /
  group claim / default) and every management endpoint requires **admin**.
- No OIDC (local testing): the server already binds to 127.0.0.1 in that mode;
  the UI is open as a local admin. Mutations still demand the custom CSRF header,
  which forces a CORS preflight for any cross-origin page → a malicious website
  cannot drive the localhost UI blind.

SECRETS: names only, ever. The write endpoint accepts a value and hands it to
``secrets_store.vault_set`` — it is never echoed, logged, or audited.
"""
import base64
import hashlib
import hmac
import json
import os
import secrets as pysecrets
import time
from pathlib import Path
from urllib.parse import urlencode, urlparse

import authz
import secrets_store
import skills
import tenancy
import version

UI_DIR = Path(__file__).parent / "webui_static"
SESSION_COOKIE = "aicortex_ui"
OAUTH_COOKIE = "aicortex_ui_oauth"
SESSION_TTL = int(os.environ.get("UI_SESSION_TTL", str(12 * 3600)))
_OAUTH_TTL = 600  # seconds a login attempt may take
_MAX_BODY = int(os.environ.get("UI_MAX_BODY_BYTES", str(1_000_000)))

_ASSETS = {  # allow-list: only these files are ever served from UI_DIR
    "app.js": "application/javascript; charset=utf-8",
    "style.css": "text/css; charset=utf-8",
    "logo.svg": "image/svg+xml",
}
_CSP = ("default-src 'none'; script-src 'self'; style-src 'self'; "
        "img-src 'self' data:; connect-src 'self'; base-uri 'none'; "
        "form-action 'self'; frame-ancestors 'none'")

# Per-boot fallback signing key: sessions then die on restart, which is an
# acceptable degradation — never a weaker signature.
_BOOT_KEY = pysecrets.token_bytes(32)
_OIDC_CACHE: dict = {"exp": 0.0, "doc": None}

# Device/endpoint registries counted on the overview page (dir of *.json each).
_DEVICE_DIRS = {
    "services": ("SERVICES_DIR", "/data/services"),
    "mqtt": ("MQTT_DIR", "/data/mqtt"),
    "ftp": ("FTP_DIR", "/data/ftp"),
    "printers": ("PRINT_DIR", "/data/printers"),
    "scanners": ("SCAN_DIR", "/data/scanners"),
    "webdav": ("WEBDAV_DIR", "/data/webdav"),
    "caldav": ("CALDAV_DIR", "/data/caldav"),
    "ssh": ("SSH_DIR", "/data/ssh"),
    "mail": ("MAIL_DIR", "/data/mail"),
    "imap": ("IMAP_DIR", "/data/imap"),
    "mcp": ("MCP_DIR", "/data/mcp"),
    "webhooks": ("WEBHOOK_DIR", "/data/webhooks"),
}

_ROLES = ("admin", "user", "viewer")


def _enabled() -> bool:
    return os.environ.get("UI_ENABLED", "1").strip().lower() not in (
        "0", "false", "no", "off")


def _oidc_configured() -> bool:
    return bool(os.environ.get("OIDC_CONFIG_URL") and os.environ.get("OIDC_CLIENT_ID"))


def _base_url(request) -> str:
    base = os.environ.get("BASE_URL", "").rstrip("/")
    return base or str(request.base_url).rstrip("/")


def _secure_cookies(request) -> bool:
    return _base_url(request).lower().startswith("https://")


def _ui_key() -> bytes:
    for env in ("JWT_SIGNING_KEY", "STORAGE_ENCRYPTION_KEY"):
        v = os.environ.get(env)
        if v:
            return hashlib.sha256(f"aicortex-ui:{v}".encode()).digest()
    return _BOOT_KEY


def _b64(raw: bytes) -> str:
    return base64.urlsafe_b64encode(raw).decode().rstrip("=")


def _unb64(s: str) -> bytes:
    return base64.urlsafe_b64decode(s + "=" * (-len(s) % 4))


def _sign(payload: dict) -> str:
    body = _b64(json.dumps(payload, separators=(",", ":")).encode())
    mac = _b64(hmac.new(_ui_key(), body.encode(), hashlib.sha256).digest())
    return f"{body}.{mac}"


def _verify(token: str):
    """Signed-payload check + expiry. Returns the payload dict or None."""
    try:
        body, mac = token.split(".", 1)
        want = _b64(hmac.new(_ui_key(), body.encode(), hashlib.sha256).digest())
        if not hmac.compare_digest(mac, want):
            return None
        payload = json.loads(_unb64(body))
        if float(payload.get("exp", 0)) < time.time():
            return None
        return payload
    except Exception:
        return None


def _session(request):
    """The caller's UI session, or None. Open mode (no OIDC): the server binds
    localhost-only, so the local operator IS the admin — synthetic session."""
    if not _oidc_configured():
        return {"sub": "local-operator", "name": "Local operator",
                "role": "admin", "csrf": "local", "mode": "open"}
    tok = request.cookies.get(SESSION_COOKIE, "")
    return _verify(tok) if tok else None


def _mutation_ok(request, sess) -> bool:
    """CSRF gate for state-changing calls: the custom header must echo the
    session's token (its mere presence already forces a CORS preflight, which
    this server never approves), and a present Origin must match our own host."""
    if request.headers.get("x-csrf", "") != sess.get("csrf", ""):
        return False
    origin = request.headers.get("origin", "")
    if origin:
        try:
            o, b = urlparse(origin), urlparse(_base_url(request))
            if o.netloc and o.netloc not in (b.netloc, request.headers.get("host", "")):
                return False
        except Exception:
            return False
    return True


def _discovery() -> dict:
    """The IdP's OIDC discovery document (cached 1h)."""
    if _OIDC_CACHE["doc"] and _OIDC_CACHE["exp"] > time.time():
        return _OIDC_CACHE["doc"]
    import httpx
    r = httpx.get(os.environ["OIDC_CONFIG_URL"], timeout=15)
    r.raise_for_status()
    doc = r.json()
    _OIDC_CACHE.update(doc=doc, exp=time.time() + 3600)
    return doc


def _display_name(claims: dict) -> str:
    for k in ("name", "preferred_username", "email", "sub"):
        v = claims.get(k)
        if v:
            return str(v)
    return "?"


def _audit(sess, action: str, decision: str, detail: str) -> None:
    try:
        authz.audit(sess.get("sub", "?") if sess else "?",
                    (sess or {}).get("role", "?"), f"ui:{action}", decision, detail)
    except Exception:
        pass


def _count_json(env: str, default: str) -> int:
    d = Path(os.environ.get(env, default))
    return len(list(d.glob("*.json"))) if d.exists() else 0


def _skill_rows() -> list:
    rows = []
    for sk in sorted(skills.SKILLS_DIR.glob("*/SKILL.md")):
        try:
            meta, _ = skills._parse(sk.read_text(encoding="utf-8"))
        except Exception:
            meta = {}
        rows.append({
            "name": sk.parent.name,
            "title": str(meta.get("name", sk.parent.name)),
            "category": skills._category(meta),
            "description": str(meta.get("description", "")),
            "tags": str(meta.get("tags", "")),
        })
    return rows


def _service_rows() -> list:
    """One row per registered integration/device across ALL registries. Only
    CONSTRUCTED fields leave this function (kind, name, target, description,
    secret ref-NAME) — never the raw config dict, so a future config field can't
    accidentally leak through the UI."""
    rows = []
    for kind, (env, default) in _DEVICE_DIRS.items():
        d = Path(os.environ.get(env, default))
        if not d.exists():
            continue
        for p in sorted(d.glob("*.json")):
            try:
                c = json.loads(p.read_text(encoding="utf-8"))
            except Exception:
                rows.append({"kind": kind, "name": p.stem, "target": "(unreadable)",
                             "description": "", "secret": ""})
                continue
            host = str(c.get("host") or "")
            port = c.get("port")
            target = str(c.get("base_url") or c.get("url") or c.get("uri")
                         or (f"{host}:{port}" if host and port else host))
            secret = str(c.get("token_env") or c.get("password_env")
                         or c.get("secret_env") or "")
            rows.append({"kind": kind, "name": p.stem, "target": target,
                         "description": str(c.get("description") or ""),
                         "secret": secret})
    return rows


def _audit_tail(limit: int, q: str) -> list:
    """Newest-first slice of the authz audit log (JSONL). Reads at most the last
    1 MB so a years-old log can't balloon a request."""
    path = authz.AUDIT_FILE
    if not path.exists():
        return []
    size = path.stat().st_size
    with open(path, "rb") as f:
        if size > 1_000_000:
            f.seek(size - 1_000_000)
        raw = f.read().decode("utf-8", "replace")
    lines = raw.splitlines()
    if size > 1_000_000 and lines:
        lines = lines[1:]  # drop the possibly-cut first line
    q = (q or "").lower()
    out = []
    for ln in reversed(lines):
        if q and q not in ln.lower():
            continue
        try:
            out.append(json.loads(ln))
        except Exception:
            continue
        if len(out) >= limit:
            break
    return out


def _user_rows() -> dict:
    pol = tenancy._policy()
    roles = pol.get("roles", {}) if isinstance(pol.get("roles"), dict) else {}
    areas = pol.get("users", {}) if isinstance(pol.get("users"), dict) else {}
    identities = sorted(set(roles) | set(areas))
    out = []
    for ident in identities:
        cfg = areas.get(ident, {}) if isinstance(areas.get(ident), dict) else {}
        devices = {k: tenancy._fmt_access(cfg[k])
                   for k in tenancy._DEVICE_KINDS if k in cfg}
        out.append({
            "identity": ident,
            "role": roles.get(ident, ""),
            "memory": str(cfg.get("memory", "own")),
            "vault": str(cfg.get("vault", "own")),
            "services": tenancy._fmt_access(cfg.get("services", "all")),
            "skills": tenancy._fmt_access(cfg.get("skills", "all")),
            "devices": devices,
            "note": str(cfg.get("note", "")),
        })
    return {"users": out, "enforce": authz.enabled(),
            "default_role": os.environ.get("OIDC_DEFAULT_ROLE", "admin"),
            "device_kinds": list(tenancy._DEVICE_KINDS)}


def register(mcp):
    from starlette.responses import JSONResponse, RedirectResponse, Response

    def _json(obj, status: int = 200, headers: dict = None):
        return JSONResponse(obj, status_code=status, headers=headers or {})

    def _cookie(resp, name: str, value: str, request, max_age: int, path: str = "/ui"):
        resp.set_cookie(name, value, max_age=max_age, path=path, httponly=True,
                        samesite="lax", secure=_secure_cookies(request))

    def _require_admin(request):
        """(session) or a JSONResponse short-circuit. Admin only — the UI is the
        management plane; viewers/users have no business here (yet)."""
        if not _enabled():
            return _json({"error": "ui disabled"}, 404)
        sess = _session(request)
        if not sess:
            return _json({"error": "unauthorized"}, 401)
        if sess.get("role") != "admin":
            _audit(sess, "access", "deny", "non-admin on management API")
            return _json({"error": "forbidden", "detail": "admin role required"}, 403)
        return sess

    async def _body(request):
        raw = await request.body()
        if len(raw) > _MAX_BODY:
            return None
        try:
            obj = json.loads(raw) if raw.strip() else {}
        except Exception:
            return None
        return obj if isinstance(obj, dict) else None

    def _gate_mutation(request, sess):
        if not _mutation_ok(request, sess):
            _audit(sess, "csrf", "deny", "missing/invalid CSRF header or origin")
            return _json({"error": "forbidden", "detail": "CSRF check failed"}, 403)
        return None

    # ── static app ───────────────────────────────────────────────────────────
    @mcp.custom_route("/ui", methods=["GET"])
    async def _index(request):
        if not _enabled():
            return _json({"error": "ui disabled"}, 404)
        page = UI_DIR / "index.html"
        if not page.exists():
            return _json({"error": "ui assets missing from image"}, 500)
        # {{v}} cache-buster: asset URLs change with every release, so browsers
        # can cache aggressively yet always pick up a new image's UI.
        html = page.read_text(encoding="utf-8").replace("{{v}}", version.__version__)
        return Response(html, media_type="text/html; charset=utf-8",
                        headers={"Cache-Control": "no-store",
                                 "Content-Security-Policy": _CSP,
                                 "X-Content-Type-Options": "nosniff",
                                 "Referrer-Policy": "same-origin"})

    @mcp.custom_route("/ui/assets/{name}", methods=["GET"])
    async def _asset(request):
        name = request.path_params.get("name", "")
        ctype = _ASSETS.get(name)  # allow-list — no path traversal surface
        f = UI_DIR / name
        if not _enabled() or not ctype or not f.is_file():
            return _json({"error": "not found"}, 404)
        return Response(f.read_bytes(), media_type=ctype,
                        headers={"Cache-Control": "no-cache",
                                 "X-Content-Type-Options": "nosniff"})

    # ── login / logout (OIDC authorization code + PKCE) ──────────────────────
    @mcp.custom_route("/ui/login", methods=["GET"])
    async def _login(request):
        if not _enabled() or not _oidc_configured():
            return RedirectResponse("/ui", status_code=302)
        try:
            doc = _discovery()
        except Exception as exc:
            return _json({"error": f"IdP discovery failed: {exc}"}, 502)
        state = pysecrets.token_urlsafe(24)
        verifier = pysecrets.token_urlsafe(48)
        challenge = _b64(hashlib.sha256(verifier.encode()).digest())
        params = {
            "response_type": "code",
            "client_id": os.environ["OIDC_CLIENT_ID"],
            "redirect_uri": f"{_base_url(request)}/ui/callback",
            "scope": os.environ.get("OIDC_SCOPE", "openid profile email"),
            "state": state,
            "code_challenge": challenge,
            "code_challenge_method": "S256",
        }
        resp = RedirectResponse(
            f"{doc['authorization_endpoint']}?{urlencode(params)}", status_code=302)
        _cookie(resp, OAUTH_COOKIE,
                _sign({"state": state, "verifier": verifier,
                       "exp": time.time() + _OAUTH_TTL}),
                request, _OAUTH_TTL)
        return resp

    @mcp.custom_route("/ui/callback", methods=["GET"])
    async def _callback(request):
        if not _enabled() or not _oidc_configured():
            return RedirectResponse("/ui", status_code=302)

        def _fail(msg: str):
            return RedirectResponse(f"/ui?login_error={msg}", status_code=302)

        pending = _verify(request.cookies.get(OAUTH_COOKIE, ""))
        state = request.query_params.get("state", "")
        code = request.query_params.get("code", "")
        if not pending or not code or not hmac.compare_digest(
                state, pending.get("state", "")):
            return _fail("state")
        try:
            doc = _discovery()
            import httpx
            data = {
                "grant_type": "authorization_code",
                "code": code,
                "redirect_uri": f"{_base_url(request)}/ui/callback",
                "client_id": os.environ["OIDC_CLIENT_ID"],
                "code_verifier": pending["verifier"],
            }
            secret = os.environ.get("OIDC_CLIENT_SECRET")
            if secret:
                data["client_secret"] = secret
            tr = httpx.post(doc["token_endpoint"], data=data, timeout=20)
            tr.raise_for_status()
            access = tr.json().get("access_token", "")
            ur = httpx.get(doc["userinfo_endpoint"],
                           headers={"Authorization": f"Bearer {access}"}, timeout=20)
            ur.raise_for_status()
            claims = ur.json()
        except Exception:
            return _fail("exchange")
        sub = str(claims.get("sub", "")).strip()
        if not sub:
            return _fail("claims")
        role = authz.role_for(sub, is_runner=False, claims=claims)
        sess = {"sub": sub, "name": _display_name(claims), "role": role,
                "csrf": pysecrets.token_urlsafe(16),
                "exp": time.time() + SESSION_TTL}
        _audit(sess, "login", "allow" if role == "admin" else "deny",
               f"browser login, role={role}")
        resp = RedirectResponse("/ui", status_code=302)
        _cookie(resp, SESSION_COOKIE, _sign(sess), request, SESSION_TTL)
        _cookie(resp, OAUTH_COOKIE, "", request, 0)  # one-shot
        return resp

    @mcp.custom_route("/ui/logout", methods=["POST"])
    async def _logout(request):
        resp = _json({"ok": True})
        _cookie(resp, SESSION_COOKIE, "", request, 0)
        return resp

    # ── session / overview ────────────────────────────────────────────────────
    @mcp.custom_route("/ui/api/me", methods=["GET"])
    async def _me(request):
        if not _enabled():
            return _json({"error": "ui disabled"}, 404)
        sess = _session(request)
        if not sess:
            return _json({"authenticated": False, "oidc": _oidc_configured(),
                          "version": version.__version__})
        return _json({"authenticated": True, "sub": sess.get("sub"),
                      "name": sess.get("name"), "role": sess.get("role"),
                      "csrf": sess.get("csrf"), "mode": sess.get("mode", "oidc"),
                      "version": version.__version__})

    @mcp.custom_route("/ui/api/overview", methods=["GET"])
    async def _overview(request):
        sess = _require_admin(request)
        if isinstance(sess, JSONResponse):
            return sess
        skill_rows = _skill_rows()
        devices = sum(_count_json(env, d) for k, (env, d) in _DEVICE_DIRS.items()
                      if k != "services")
        return _json({
            "version": version.__version__,
            "enforce": authz.enabled(),
            "skills": len(skill_rows),
            "categories": len({r["category"] for r in skill_rows}),
            "secrets": len(secrets_store.vault_entries()),
            "services": _count_json(*_DEVICE_DIRS["services"]),
            "devices": devices,
            "users": len(_user_rows()["users"]),
        })

    # ── vault (names only; values are write-only) ────────────────────────────
    @mcp.custom_route("/ui/api/secrets", methods=["GET"])
    async def _secrets_list(request):
        sess = _require_admin(request)
        if isinstance(sess, JSONResponse):
            return sess
        return _json({"secrets": secrets_store.vault_entries()})

    @mcp.custom_route("/ui/api/secrets", methods=["POST"])
    async def _secrets_set(request):
        sess = _require_admin(request)
        if isinstance(sess, JSONResponse):
            return sess
        denied = _gate_mutation(request, sess)
        if denied:
            return denied
        body = await _body(request)
        if body is None:
            return _json({"error": "invalid body"}, 400)
        name = str(body.get("name", "")).strip()
        value = str(body.get("value", ""))
        owner = str(body.get("owner", "")).strip()
        if not name or not value:
            return _json({"error": "name and value are required"}, 400)
        msg = secrets_store.vault_set(name, value, owner)
        ok = msg.startswith("Stored")
        _audit(sess, "secret_set", "allow" if ok else "deny",
               f"name={name} owner={owner or 'shared'}")  # NEVER the value
        return _json({"ok": ok, "message": msg}, 200 if ok else 409)

    @mcp.custom_route("/ui/api/secrets/delete", methods=["POST"])
    async def _secrets_delete(request):
        sess = _require_admin(request)
        if isinstance(sess, JSONResponse):
            return sess
        denied = _gate_mutation(request, sess)
        if denied:
            return denied
        body = await _body(request)
        if body is None:
            return _json({"error": "invalid body"}, 400)
        name = str(body.get("name", "")).strip()
        owner = str(body.get("owner", "")).strip()
        msg = secrets_store.vault_delete(name, owner)
        ok = msg.startswith("Deleted")
        _audit(sess, "secret_delete", "allow" if ok else "deny",
               f"name={name} owner={owner or 'shared'}")
        return _json({"ok": ok, "message": msg}, 200 if ok else 404)

    # ── services & devices (read-only) + audit log ───────────────────────────
    @mcp.custom_route("/ui/api/services", methods=["GET"])
    async def _services_list(request):
        sess = _require_admin(request)
        if isinstance(sess, JSONResponse):
            return sess
        return _json({"services": _service_rows()})

    @mcp.custom_route("/ui/api/audit", methods=["GET"])
    async def _audit_view(request):
        sess = _require_admin(request)
        if isinstance(sess, JSONResponse):
            return sess
        try:
            limit = max(1, min(int(request.query_params.get("limit", "200")), 1000))
        except ValueError:
            limit = 200
        return _json({"entries": _audit_tail(limit, request.query_params.get("q", ""))})

    # ── skills ────────────────────────────────────────────────────────────────
    @mcp.custom_route("/ui/api/skills", methods=["GET"])
    async def _skills_list(request):
        sess = _require_admin(request)
        if isinstance(sess, JSONResponse):
            return sess
        return _json({"skills": _skill_rows()})

    @mcp.custom_route("/ui/api/skills/get", methods=["GET"])
    async def _skills_get(request):
        sess = _require_admin(request)
        if isinstance(sess, JSONResponse):
            return sess
        name = request.query_params.get("name", "")
        path = skills.SKILLS_DIR / skills._slug(name) / "SKILL.md"
        if not path.exists():
            return _json({"error": f"no skill named '{name}'"}, 404)
        meta, instructions = skills._parse(path.read_text(encoding="utf-8"))
        return _json({"name": path.parent.name,
                      "title": str(meta.get("name", path.parent.name)),
                      "category": skills._category(meta),
                      "description": str(meta.get("description", "")),
                      "tags": str(meta.get("tags", "")),
                      "instructions": instructions})

    @mcp.custom_route("/ui/api/skills", methods=["POST"])
    async def _skills_save(request):
        sess = _require_admin(request)
        if isinstance(sess, JSONResponse):
            return sess
        denied = _gate_mutation(request, sess)
        if denied:
            return denied
        body = await _body(request)
        if body is None:
            return _json({"error": "invalid body"}, 400)
        name = str(body.get("name", "")).strip()
        category = skills._canonical_category(str(body.get("category", "")))
        if not name:
            return _json({"error": "name is required"}, 400)
        if not category:  # same house rule as skill_write
            return _json({"error": "category is required (house rule)",
                          "categories": skills._existing_categories()}, 400)
        folder = skills.SKILLS_DIR / skills._slug(name)
        folder.mkdir(parents=True, exist_ok=True)
        fm = skills._frontmatter(name, str(body.get("description", "")),
                                 category, str(body.get("tags", "")))
        (folder / "SKILL.md").write_text(
            fm + str(body.get("instructions", "")).rstrip() + "\n", encoding="utf-8")
        _audit(sess, "skill_save", "allow", f"skill={folder.name} [{category}]")
        return _json({"ok": True,
                      "message": f"Saved skill '{folder.name}' [{category}]."})

    @mcp.custom_route("/ui/api/skills/delete", methods=["POST"])
    async def _skills_delete(request):
        sess = _require_admin(request)
        if isinstance(sess, JSONResponse):
            return sess
        denied = _gate_mutation(request, sess)
        if denied:
            return denied
        body = await _body(request)
        if body is None:
            return _json({"error": "invalid body"}, 400)
        name = str(body.get("name", "")).strip()
        folder = skills.SKILLS_DIR / skills._slug(name)
        if not (folder.exists() and folder.is_dir()):
            return _json({"ok": False, "message": f"No skill named '{name}'."}, 404)
        import shutil
        shutil.rmtree(folder)
        _audit(sess, "skill_delete", "allow", f"skill={folder.name}")
        return _json({"ok": True, "message": f"Deleted skill '{folder.name}'."})

    # ── users (roles + per-user areas, both live in policy.json) ─────────────
    @mcp.custom_route("/ui/api/users", methods=["GET"])
    async def _users_list(request):
        sess = _require_admin(request)
        if isinstance(sess, JSONResponse):
            return sess
        return _json(_user_rows())

    @mcp.custom_route("/ui/api/users", methods=["POST"])
    async def _users_set(request):
        sess = _require_admin(request)
        if isinstance(sess, JSONResponse):
            return sess
        denied = _gate_mutation(request, sess)
        if denied:
            return denied
        body = await _body(request)
        if body is None:
            return _json({"error": "invalid body"}, 400)
        identity = str(body.get("identity", "")).strip()
        if not identity:
            return _json({"error": "identity is required"}, 400)
        role = str(body.get("role", "")).strip().lower()
        if role and role not in _ROLES + ("default",):
            return _json({"error": f"role must be one of {_ROLES} or 'default'"}, 400)
        for label in ("memory", "vault"):
            v = str(body.get(label, "")).strip().lower()
            if v and v not in tenancy._AREA_VALUES:
                return _json({"error": f"{label} must be one of "
                                       f"{tenancy._AREA_VALUES}"}, 400)
        pol = tenancy._policy()
        roles = pol.get("roles") if isinstance(pol.get("roles"), dict) else {}
        users = pol.get("users") if isinstance(pol.get("users"), dict) else {}
        if role == "default":
            roles.pop(identity, None)
        elif role:
            roles[identity] = role
        entry = dict(users.get(identity, {}))
        for label in ("memory", "vault"):
            v = str(body.get(label, "")).strip().lower()
            if v:
                entry[label] = v
        for label in ("services", "skills"):
            v = str(body.get(label, "")).strip()
            if v:
                entry[label] = tenancy._norm_access(v)
        devices = body.get("devices")
        if isinstance(devices, dict):
            for kind, spec in devices.items():
                kind = str(kind).strip().lower()
                if kind not in tenancy._DEVICE_KINDS:
                    return _json({"error": f"unknown device kind '{kind}'"}, 400)
                spec = str(spec).strip()
                if spec == "unset":
                    entry.pop(kind, None)
                elif spec:
                    entry[kind] = tenancy._norm_access(spec)
        note = str(body.get("note", "")).strip()
        if note:
            entry["note"] = note
        entry.setdefault("memory", "own")
        users[identity] = entry
        pol["roles"], pol["users"] = roles, users
        tenancy._write_policy(pol)
        _audit(sess, "user_set", "allow", f"identity={identity} role={role or '-'}")
        return _json({"ok": True, "message": f"Saved '{identity}'."})

    @mcp.custom_route("/ui/api/users/delete", methods=["POST"])
    async def _users_delete(request):
        sess = _require_admin(request)
        if isinstance(sess, JSONResponse):
            return sess
        denied = _gate_mutation(request, sess)
        if denied:
            return denied
        body = await _body(request)
        if body is None:
            return _json({"error": "invalid body"}, 400)
        identity = str(body.get("identity", "")).strip()
        pol = tenancy._policy()
        found = False
        for key in ("roles", "users"):
            section = pol.get(key)
            if isinstance(section, dict) and identity in section:
                del section[identity]
                found = True
        if not found:
            return _json({"ok": False,
                          "message": f"No entry for '{identity}'."}, 404)
        tenancy._write_policy(pol)
        _audit(sess, "user_delete", "allow", f"identity={identity}")
        return _json({"ok": True, "message": f"Removed '{identity}'."})
