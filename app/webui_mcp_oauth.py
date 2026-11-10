"""WebUI: sign in to (and out of) an upstream MCP server that uses OAuth.

Start (POST, CSRF-checked) → the provider's login page in the person's own browser →
back to ``/ui/mcp-oauth/callback``. What is in flight between the two lives in a
signed, HttpOnly cookie that expires after ten minutes and is bound to the signed-in
person: the callback refuses to finish someone else's sign-in. The tokens go straight
into that person's vault area (mcp_oauth) — no answer from here ever contains one.

Who may sign in to a server is the same rule as for calling it (``mcp_call`` through
``authz.enforce_rest``): the role and, for a non-admin, the MCP servers an admin
assigned them. Signing out is always possible — your own stored login is yours to drop.
Before the browser leaves for the provider, the WebUI shows where the sign-in happens
(the provider's host and the server it is for) and asks to confirm.
"""
import asyncio
import json
import time
from urllib.parse import urlencode, urlparse

import authz
import mcp_gateway
import mcp_oauth

COOKIE = "aicortex_ui_mcp_oauth"
_TTL = 600     # seconds a sign-in at the provider may take


def _servers() -> list:
    """(slug, config) of every registered OAuth server."""
    out = []
    for p in sorted(mcp_gateway.MCP_DIR.glob("*.json")) if mcp_gateway.MCP_DIR.exists() else []:
        try:
            cfg = json.loads(p.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            continue
        if mcp_oauth.is_oauth(cfg):
            out.append((p.stem, cfg))
    return out


def _may_use(sess, server: str, quiet: bool = False) -> bool:
    """The mcp_call policy for this person and server. `quiet`: the same decision
    without an audit entry (the status list asks on every page view)."""
    sub, role = str(sess.get("sub", "")), str(sess.get("role", "user"))
    if quiet:
        if not authz.enabled():
            return True
        import tenancy
        return authz.decide(role, "mcp_call")[0] and tenancy.endpoint_allowed(sub, role, "mcp", server)
    ok, _ = authz.enforce_rest(sub, role, "mcp_call", {"server": server})
    return ok


def register(mcp, h, ui):
    """`h`: the WebUI helpers (json, require_session, body, gate, audit). `ui`: sign,
    verify, base_url, cookie and session from webui.py."""
    from starlette.responses import JSONResponse, RedirectResponse

    async def _person_and_server(request, check_use: bool = True):
        """(session, slug, config) for a CSRF-checked POST, or (response, None, None)."""
        sess = h.require_session(request)
        if isinstance(sess, JSONResponse):
            return sess, None, None
        denied = h.gate(request, sess)
        if denied:
            return denied, None, None
        body = await h.body(request)
        if body is None:
            return h.json({"error": "invalid body"}, 400), None, None
        name = mcp_gateway._slug(str(body.get("server", "")))
        cfg = mcp_gateway._load(name)
        if not cfg:
            return h.json({"error": "not found"}, 404), None, None
        if not mcp_oauth.is_oauth(cfg):
            return h.json({"ok": False, "message": f"'{name}' does not use OAuth sign-in."}, 400), None, None
        if check_use and not _may_use(sess, name):
            return h.json({"error": "forbidden", "detail": f"not allowed to use '{name}'"}, 403), None, None
        return sess, name, cfg

    @mcp.custom_route("/ui/api/mcp-oauth", methods=["GET"])
    async def _status(request):
        sess = h.require_session(request)
        if isinstance(sess, JSONResponse):
            return sess
        sub = str(sess.get("sub", ""))
        rows = [{"name": name, **mcp_oauth.status(name, cfg, sub),
                 "read_only": mcp_gateway._flag(cfg.get("read_only"))}
                for name, cfg in _servers() if _may_use(sess, name, quiet=True)]
        return h.json({"servers": rows})

    @mcp.custom_route("/ui/api/mcp-oauth/start", methods=["POST"])
    async def _start(request):
        sess, name, cfg = await _person_and_server(request)
        if name is None:
            return sess
        redirect = f"{ui.base_url(request)}/ui/mcp-oauth/callback"
        try:
            url, pending = await asyncio.to_thread(
                mcp_oauth.begin, name, redirect, str(sess.get("sub", "")))
        except mcp_oauth.OAuthError as exc:
            h.audit(sess, "mcp_oauth_start", "deny", f"server={name} {exc.code}")
            return h.json({"ok": False, "message": str(exc)}, 502)
        except Exception as exc:          # never a 500 — and never details of the provider
            h.audit(sess, "mcp_oauth_start", "deny", f"server={name} {type(exc).__name__}")
            return h.json({"ok": False, "message": "The sign-in could not start."}, 502)
        resp = h.json({"ok": True, "url": url, "server_url": str(cfg.get("url", "")),
                       "login_host": urlparse(url).netloc})
        ui.cookie(resp, COOKIE, ui.sign({**pending, "exp": time.time() + _TTL}), request, _TTL)
        h.audit(sess, "mcp_oauth_start", "allow", f"server={name}")
        return resp

    @mcp.custom_route("/ui/mcp-oauth/callback", methods=["GET"])
    async def _callback(request):
        pending = ui.verify(request.cookies.get(COOKIE, "")) or {}
        server = str(pending.get("server", ""))

        def back(code: str):
            resp = RedirectResponse(f"/ui?{urlencode({'mcp_oauth': code, 'server': server})}#services",
                                    status_code=302)
            ui.cookie(resp, COOKIE, "", request, 0)      # one-shot
            return resp

        sess = ui.session(request)
        if not pending:
            return back("state")
        if not sess:
            return back("session")
        if str(sess.get("sub", "")) != str(pending.get("sub", "")):
            h.audit(sess, "mcp_oauth_callback", "deny", f"server={server} started by another person")
            return back("person")
        params = {k: request.query_params.get(k, "") for k in ("code", "state", "iss", "error")}
        try:
            await asyncio.to_thread(mcp_oauth.finish, pending, params)
        except mcp_oauth.OAuthError as exc:
            h.audit(sess, "mcp_oauth_callback", "deny", f"server={server} {exc.code}")
            return back(exc.code)
        except Exception as exc:
            h.audit(sess, "mcp_oauth_callback", "deny", f"server={server} {type(exc).__name__}")
            return back("error")
        h.audit(sess, "mcp_oauth_callback", "allow", f"server={server} signed in")
        return back("ok")

    @mcp.custom_route("/ui/api/mcp-oauth/logout", methods=["POST"])
    async def _logout(request):
        sess, name, _cfg = await _person_and_server(request, check_use=False)
        if name is None:
            return sess
        done = mcp_oauth.sign_out(name, str(sess.get("sub", "")))
        h.audit(sess, "mcp_oauth_logout", "allow" if done else "deny", f"server={name}")
        return h.json({"ok": done, "message": "Signed out." if done else "You were not signed in."},
                      200 if done else 404)
