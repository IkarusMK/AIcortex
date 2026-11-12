"""WebUI: who may use your sign-in to an OAuth MCP server, and your waiting approvals.

GET  /ui/api/mcp-oauth/access?server=   your settings for that server and what you can
                                         pick: apps seen so far, your runners, your keys
POST /ui/api/mcp-oauth/access           change them (the only way to ALLOW more)
GET  /ui/api/mcp-oauth/approvals        your calls that wait for a decision
POST /ui/api/mcp-oauth/approvals/decide {id, approve}: approve → AICortex runs exactly
                                         that call once with your sign-in; reject → never

Everything here is about the signed-in person's OWN sign-ins and approvals — nobody,
an admin included, sees or decides someone else's. Changes are CSRF-checked and
audited; an approval can only be decided here, never by a tool call.

What an approval shows (tool, arguments, server, channel) comes from the call, i.e.
from the AI and whatever it copied. Characters that are invisible or change the
writing direction (U+202E, U+2066, U+200B, U+FEFF, …) are shown as escapes (\\u202e),
so the text the person approves can't read differently from the call that runs.
"""
import asyncio
import json
import os
import time
import unicodedata

import mcp_access
import mcp_approvals
import mcp_gateway
import mcp_oauth


def _choices(person: str, current: dict) -> dict:
    """What the person can pick from (and whatever is already allowed)."""
    def merged(found: list, key: str) -> list:
        known = {c["id"] for c in found}
        return found + [{"id": v, "label": v} for v in current[key] if v not in known]

    apps = [{"id": host, "label": f"{host} ({seen.get('name')})" if seen.get("name") else host}
            for host, seen in sorted(mcp_access.apps_seen().items())]
    if not (os.environ.get("OIDC_CONFIG_URL") and os.environ.get("OIDC_CLIENT_ID")):
        apps.insert(0, {"id": mcp_access.LOCAL, "label": mcp_access.LOCAL})
    import runners
    data = runners.load()
    runs = [{"id": r["id"], "label": f"{r.get('name') or r['id']} ({r.get('kind', '')})"}
            for r in runners.visible(data, person, admin=False)]
    runs.append({"id": runners.EXTERNAL, "label": runners.EXTERNAL})
    import apikeys
    keys = [{"id": k["keyid"], "label": f"{k.get('name') or k['keyid']} ({k['keyid']})"}
            for k in apikeys.list_keys() if k.get("sub") == person and not k.get("runner")]
    return {"apps": merged(apps, "allow_apps"), "runners": merged(runs, "allow_runners"),
            "keys": merged(keys, "allow_keys")}


# Invisible letters a format-character check misses (Hangul fillers and the like).
_BLANKS = frozenset("\u115f\u1160\u3164\uffa0")


def _hidden(c: str, keep: str) -> bool:
    """Does `c` not show as itself: a format character (bidi controls, zero-width ones,
    BOM, tags), a line/paragraph separator, a control character, a blank letter?"""
    return c not in keep and (unicodedata.category(c) in ("Cf", "Cc", "Zl", "Zp")
                              or c in _BLANKS)


def _escape(c: str) -> str:
    n = ord(c)
    if n <= 0xFFFF:
        return f"\\u{n:04x}"
    n -= 0x10000                     # a JSON-style surrogate pair, so the text stays JSON
    return f"\\u{0xD800 + (n >> 10):04x}\\u{0xDC00 + (n & 0x3FF):04x}"


def visible(text, keep: str = "") -> str:
    """`text` with every character that doesn't show as itself written as its escape."""
    return "".join(_escape(c) if _hidden(c, keep) else c for c in str(text))


def _row(r: dict) -> dict:
    args = json.dumps(r.get("args") or {}, ensure_ascii=False, indent=2)
    return {"id": r["id"], "server": visible(r.get("server", "")),
            "tool": visible(r.get("tool", "")),
            "args": visible(args, keep="\n"),     # json.dumps escapes every \n inside a string
            "requested_by": visible(r.get("channel", "")), "created": int(r.get("created", 0)),
            "expires": int(r.get("expires", 0))}


def register(mcp, h, oauth_server):
    """`h`: the WebUI helpers. `oauth_server(request, check_use)`: (session, slug, cfg)
    of a CSRF-checked POST naming an OAuth server, or (response, None, None)."""
    from starlette.responses import JSONResponse

    @mcp.custom_route("/ui/api/mcp-oauth/access", methods=["GET"])
    async def _access_get(request):
        sess = h.require_session(request)
        if isinstance(sess, JSONResponse):
            return sess
        name = mcp_gateway._slug(request.query_params.get("server", ""))
        cfg = mcp_gateway._load(name)
        if not mcp_oauth.is_oauth(cfg):
            return h.json({"error": "not found"}, 404)
        person = str(sess.get("sub", ""))
        current = mcp_access.settings(cfg, person)
        choices = await asyncio.to_thread(_choices, person, current)
        return h.json({"server": name, "settings": current, "choices": choices})

    @mcp.custom_route("/ui/api/mcp-oauth/access", methods=["POST"])
    async def _access_set(request):
        sess, name, _cfg = await oauth_server(request, False)
        if name is None:
            return sess
        body = await h.body(request) or {}
        person = str(sess.get("sub", ""))
        changes = {k: body.get(k) for k in (*mcp_access.LISTS, "approve_writes")}
        try:
            ok, msg, now = await asyncio.to_thread(
                mcp_access.update, name, person, changes, True)
        except mcp_access.Invalid as exc:
            return h.json({"ok": False, "message": str(exc)}, 400)
        h.audit(sess, "mcp_oauth_access", "allow" if ok else "deny",
                f"server={name} " + " ".join(f"{k}={','.join(now.get(k, [])) or '-'}"
                                             for k in mcp_access.LISTS)
                + f" approve_writes={now.get('approve_writes')}")
        return h.json({"ok": ok, "message": msg, "settings": now}, 200 if ok else 400)

    @mcp.custom_route("/ui/api/mcp-oauth/approvals", methods=["GET"])
    async def _approvals(request):
        sess = h.require_session(request)
        if isinstance(sess, JSONResponse):
            return sess
        try:
            rows = await asyncio.to_thread(mcp_approvals.waiting, str(sess.get("sub", "")))
        except mcp_approvals.Refused as exc:      # no key, or a file that can't be read
            return h.json({"approvals": [], "now": int(time.time()), "message": str(exc)})
        return h.json({"approvals": [_row(r) for r in rows], "now": int(time.time())})

    @mcp.custom_route("/ui/api/mcp-oauth/approvals/decide", methods=["POST"])
    async def _decide(request):
        sess = h.require_session(request)
        if isinstance(sess, JSONResponse):
            return sess
        denied = h.gate(request, sess)
        if denied:
            return denied
        body = await h.body(request)
        if body is None or not isinstance(body.get("approve"), bool):
            return h.json({"ok": False, "message": "Invalid request."}, 400)
        person, aid = str(sess.get("sub", "")), str(body.get("id", ""))[:64]
        role = str(sess.get("role", ""))
        try:
            if body["approve"]:
                # the person's policy NOW (role, assigned servers, read_only, access):
                # refused → not run, and the approval keeps waiting
                held = await asyncio.to_thread(mcp_approvals.get, person, aid)
                refused = await asyncio.to_thread(mcp_gateway.approval_refusal, held["server"],
                                                  person, role, held.get("channel", ""))
                if refused:
                    h.audit(sess, "mcp_oauth_approval", "deny", f"id={aid}: {refused}")
                    return h.json({"ok": False, "message": refused}, 403)
            row = await asyncio.to_thread(mcp_approvals.claim, person, aid, body["approve"])
        except mcp_approvals.Refused as exc:
            h.audit(sess, "mcp_oauth_approval", "deny", f"id={aid}: {exc}")
            return h.json({"ok": False, "message": str(exc)}, 409)
        where = f"id={aid} '{row['tool']}' on '{row['server']}' asked via {row['channel']}"
        if not body["approve"]:
            h.audit(sess, "mcp_oauth_approval", "allow", f"rejected {where}")
            return h.json({"ok": True, "state": "rejected", "message": "Rejected."})
        h.audit(sess, "mcp_oauth_approval", "allow", f"approved {where}")
        result = await mcp_gateway.run_approved(row["server"], row["tool"], row.get("args"),
                                                person, role=role, channel=row["channel"])
        try:
            await asyncio.to_thread(mcp_approvals.finish, person, aid, result)
        except mcp_approvals.Refused as exc:      # the file became unreadable meanwhile
            return h.json({"ok": True, "state": "done",
                           "message": f"Approved and run, but the answer could not be kept: {exc}"})
        return h.json({"ok": True, "state": "done",
                       "message": "Approved and run — the AI can fetch the answer now."})
