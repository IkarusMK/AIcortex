"""WebUI routes for the Runner tab: every signed-in person manages their own runners;
admins see all of them, may switch them off or delete them, choose the standard
runner and sign in with ChatGPT for the ChatGPT runner.

Registered from webui.register with the same helpers as the other views. Keys are
written to the vault and never sent to the browser — only names of vault entries, and
a runner's key name and trigger only to its owner.
"""
import asyncio
import time

import chatgpt_signin
import cron
import runner_builtin
import runner_cloud
import runner_state
import runners

TEST_GAP = 60               # seconds between two "Test now" of one runner
_tested: dict = {}


def _row(rec: dict, data: dict, sub: str, admin: bool) -> dict:
    """A runner as one person sees it: the owner all of it (the key by name only), an
    admin all but its key and trigger, anyone else — the standard runner — only its
    name, kind and on/off: no address, no model, no other people's job names."""
    mine = rec.get("owner") == sub
    state = runner_state.get(rec["id"], bool(rec.get("enabled")))
    row = runners.public(rec)
    if not mine and admin:
        row = dict(row, key=None, trigger="")
    elif not mine:
        row = {k: row[k] if k in ("id", "name", "kind", "enabled") else ("" if k != "key" else None)
               for k in row}
        state = {"state": state["state"], "detail": "", "link": "", "at": state["at"]}
    return dict(row, mine=mine, standard=rec["id"] == data["standard"], state=state)


def register(mcp, h):
    """`h` carries the WebUI's helpers: json, require_session, require_admin, body, gate, audit."""
    from starlette.responses import JSONResponse

    def who(sess):
        return str(sess.get("sub", "")), sess.get("role") == "admin"

    async def change(request):
        """(session, body) for a CSRF-checked POST by a person who may change things,
        else (response, None)."""
        sess = h.require_session(request)
        if isinstance(sess, JSONResponse):
            return sess, None
        if sess.get("role") not in ("user", "admin"):
            return h.json({"error": "forbidden", "detail": "this role can't change runners"}, 403), None
        denied = h.gate(request, sess)
        if denied:
            return denied, None
        body = await h.body(request)
        if body is None:
            return h.json({"error": "invalid body"}, 400), None
        return sess, body

    def answer(sess, action, ok, message, rid, **extra):
        h.audit(sess, action, "allow" if ok else "deny", f"runner={rid}")
        return h.json(dict({"ok": ok, "message": message}, **extra), 200 if ok else 400)

    @mcp.custom_route("/ui/api/runners", methods=["GET"])
    async def _runners_list(request):
        sess = h.require_session(request)
        if isinstance(sess, JSONResponse):
            return sess
        sub, admin = who(sess)
        data = runners.load()
        body = {"runners": [_row(r, data, sub, admin) for r in runners.visible(data, sub, admin)],
                "standard": data["standard"], "kinds": list(runners.KINDS), "admin": admin,
                "external": runners.external_available(), "heartbeat": cron.runner_status(),
                "setup_sentence": runner_cloud.SETUP_SENTENCE,
                "cloud_ok": sess.get("mode", "oidc") != "open"}
        if admin:
            body["chatgpt"] = chatgpt_signin.status()
        return h.json(body)

    @mcp.custom_route("/ui/api/runners/secrets", methods=["GET"])
    async def _runners_secrets(request):
        sess = h.require_session(request)
        if isinstance(sess, JSONResponse):
            return sess
        sub, admin = who(sess)
        return h.json({"names": runners.usable_key_names(sub, admin)})

    @mcp.custom_route("/ui/api/runners", methods=["POST"])
    async def _runners_save(request):
        sess, body = await change(request)
        if body is None:
            return sess
        sub, admin = who(sess)
        fields = {k: body[k] for k in ("id", "name", "kind", "model", "base_url", "trigger",
                                        "enabled", "key_ref") if k in body}
        ok, message, rec = runners.save(fields, by=sub, admin=admin, key=str(body.get("key") or ""),
                                        cloud_ok=sess.get("mode", "oidc") != "open")
        extra = {"runner": runners.public(rec)} if rec else {}
        return answer(sess, "runner_save", ok, message, (rec or {}).get("id") or body.get("id", ""), **extra)

    async def _simple(request, action, fn):
        sess, body = await change(request)
        if body is None:
            return sess
        sub, admin = who(sess)
        rid = str(body.get("id") or "")
        ok, message = fn(rid, body, sub, admin)
        return answer(sess, action, ok, message, rid)

    @mcp.custom_route("/ui/api/runners/enabled", methods=["POST"])
    async def _runners_enabled(request):
        return await _simple(request, "runner_enabled", lambda rid, body, sub, admin: runners.set_enabled(
            rid, body.get("enabled") is not False, by=sub, admin=admin))

    @mcp.custom_route("/ui/api/runners/standard", methods=["POST"])
    async def _runners_standard(request):
        return await _simple(request, "runner_standard", lambda rid, body, sub, admin: runners.set_standard(
            rid, by=sub, admin=admin))

    @mcp.custom_route("/ui/api/runners/delete", methods=["POST"])
    async def _runners_delete(request):
        return await _simple(request, "runner_delete", lambda rid, body, sub, admin: runners.delete(
            rid, by=sub, admin=admin))

    @mcp.custom_route("/ui/api/runners/test", methods=["POST"])
    async def _runners_test(request):
        sess, body = await change(request)
        if body is None:
            return sess
        sub, _ = who(sess)
        rid = str(body.get("id") or "")
        rec = runners.get(rid)
        if rec is None or rec.get("owner") != sub:
            return answer(sess, "runner_test", False, "Only the runner's owner can test it.", rid)
        now = time.monotonic()
        if now - _tested.get(rid, -TEST_GAP) < TEST_GAP:
            return h.json({"ok": False, "message": "Tested less than a minute ago — try again shortly."}, 429)
        _tested[rid] = now
        ok, message = await runner_builtin.probe(rec)
        return answer(sess, "runner_test", ok, message, rid)

    # ── the ChatGPT sign-in (admins; the one ChatGPT runner) ──────────────────
    async def _admin_post(request):
        """(session, body) for an admin's CSRF-checked POST, else (response, None)."""
        sess = h.require_admin(request)
        if isinstance(sess, JSONResponse):
            return sess, None
        denied = h.gate(request, sess)
        if denied:
            return denied, None
        body = await h.body(request)
        if body is None:
            return h.json({"error": "invalid body"}, 400), None
        return sess, body

    @mcp.custom_route("/ui/api/runner/chatgpt/start", methods=["POST"])
    async def _chatgpt_start(request):
        sess, body = await _admin_post(request)
        if body is None:
            return sess
        url = await asyncio.to_thread(chatgpt_signin.start, str(sess.get("sub", "")))
        h.audit(sess, "runner_chatgpt_signin", "allow", "started")
        return h.json({"ok": True, "url": url})

    @mcp.custom_route("/ui/api/runner/chatgpt/finish", methods=["POST"])
    async def _chatgpt_finish(request):
        sess, body = await _admin_post(request)
        if body is None:
            return sess
        ok, message = await asyncio.to_thread(
            chatgpt_signin.finish, str(body.get("url", "")), str(sess.get("sub", "")))
        account = chatgpt_signin.status()["email"] if ok else ""
        h.audit(sess, "runner_chatgpt_signin", "allow" if ok else "deny",
                f"signed in as {account or 'an account without email'}" if ok else "not completed")
        return h.json({"ok": ok, "message": message}, 200 if ok else 400)

    @mcp.custom_route("/ui/api/runner/chatgpt/signout", methods=["POST"])
    async def _chatgpt_signout(request):
        sess, body = await _admin_post(request)
        if body is None:
            return sess
        ok, message = await asyncio.to_thread(chatgpt_signin.sign_out, str(sess.get("sub", "")))
        h.audit(sess, "runner_chatgpt_signout", "allow", "signed out")
        return h.json({"ok": ok, "message": message})

    @mcp.custom_route("/ui/api/runner/chatgpt/models", methods=["GET"])
    async def _chatgpt_models(request):
        sess = h.require_admin(request)
        if isinstance(sess, JSONResponse):
            return sess
        try:
            listed = await asyncio.to_thread(chatgpt_signin.models)
        except RuntimeError as exc:                    # includes SignInNeeded
            return h.json({"models": [], "message": str(exc)})
        return h.json({"models": listed})
