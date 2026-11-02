"""WebUI routes for the runner (admin): status, model access, on/off.

Registered from webui.register with the same helpers as the other views; the
key itself is written to the vault and never sent back to the browser.
"""
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

