"""WebUI routes for the runner (admin): status, model access, on/off, and the
sign-in with ChatGPT for a ChatGPT plan.

Registered from webui.register with the same helpers as the other views; the
key and the ChatGPT tokens are kept in the vault and never sent to the browser.
"""
import asyncio

import chatgpt_signin
import runner_setup


def register(mcp, h):
    """`h` carries the WebUI's helpers: json, require_admin, body, gate, audit."""
    from starlette.responses import JSONResponse

    @mcp.custom_route("/ui/api/runner", methods=["GET"])
    async def _runner_status(request):
        sess = h.require_admin(request)
        if isinstance(sess, JSONResponse):
            return sess
        return h.json(runner_setup.status())

    @mcp.custom_route("/ui/api/runner/config", methods=["POST"])
    async def _runner_save(request):
        sess = h.require_admin(request)
        if isinstance(sess, JSONResponse):
            return sess
        denied = h.gate(request, sess)
        if denied:
            return denied
        body = await h.body(request)
        if body is None:
            return h.json({"error": "invalid body"}, 400)
        backend = str(body.get("backend", ""))
        builtin = bool(body["builtin"]) if "builtin" in body else None
        ok, message = runner_setup.save_config(
            backend, str(body.get("model", "")), str(body.get("key", "")),
            by=str(sess.get("sub", "")), builtin=builtin)
        key_note = " key replaced" if str(body.get("key", "")).strip() else ""
        h.audit(sess, "runner_config", "allow" if ok else "deny", f"backend={backend}{key_note}")
        return h.json({"ok": ok, "message": message}, 200 if ok else 400)

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
