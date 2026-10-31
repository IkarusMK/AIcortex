"""WebUI views of a person's own area — notes now; jobs and playbook runs follow —
plus the admin's registry editor.

Every route takes the SAME policy path as a REST or MCP call: ``authz.enforce_rest``
(role decision, memory-scope confinement, device areas, audit) and
``authz.rest_identity`` while the tool runs. The browser therefore gets exactly the
rights the person's AI has — a viewer reads, a user works in their own area, an
admin manages — and the logic lives once, in the tools.
"""
import re

import authz
import memory

_SAVED = re.compile(r"memory '([^']+)'.*\(version (\d+)\)")


def _text(result) -> str:
    return "\n".join(getattr(c, "text", "") for c in (getattr(result, "content", None) or []))


def register(mcp, h):
    """`h` carries the WebUI's helpers: json, require_session, require_admin, body,
    gate (CSRF/Origin), audit."""
    from starlette.responses import JSONResponse

    def who(sess):
        return str(sess.get("sub", "")), str(sess.get("role", "user"))

    def confine(sess, tool: str, args: dict):
        """Run the policy gate for `tool`; `args` comes back confined (e.g. scope)."""
        sub, role = who(sess)
        return authz.enforce_rest(sub, role, tool, args)

    async def run_tool(sess, name: str, args: dict):
        """(ok, text) — the tool's own result, or the policy's refusal reason."""
        ok, reason = confine(sess, name, args)
        if not ok:
            return False, reason
        sub, role = who(sess)
        with authz.rest_identity(sub, role):
            tool = await mcp.get_tool(name)
            if tool is None:
                return False, f"no such tool: {name}"
            return True, _text(await tool.run(args))

    async def session_and_body(request, admin=False):
        sess = (h.require_admin if admin else h.require_session)(request)
        if isinstance(sess, JSONResponse):
            return sess, None
        denied = h.gate(request, sess)
        if denied:
            return denied, None
        body = await h.body(request)
        if body is None:
            return h.json({"error": "invalid body"}, 400), None
        return sess, body

    # ── notes ────────────────────────────────────────────────────────────────
    @mcp.custom_route("/ui/api/memory", methods=["GET"])
    async def _memory_list(request):
        sess = h.require_session(request)
        if isinstance(sess, JSONResponse):
            return sess
        q = request.query_params
        args = {"scope": q.get("scope", "shared"), "query": q.get("q", "")}
        ok, reason = confine(sess, "memory_search", args)
        if not ok:
            return h.json({"error": "forbidden", "detail": reason}, 403)
        sub, role = who(sess)
        with authz.rest_identity(sub, role):
            rows = memory.search_entries(args["scope"], args["query"],
                                         project=q.get("project", ""),
                                         type_=q.get("type", ""), tag=q.get("tag", ""),
                                         limit=500)
        return h.json({"notes": rows, "scope": args["scope"]})

    @mcp.custom_route("/ui/api/memory/get", methods=["GET"])
    async def _memory_get(request):
        sess = h.require_session(request)
        if isinstance(sess, JSONResponse):
            return sess
        args = {"name": request.query_params.get("name", ""),
                "scope": request.query_params.get("scope", "shared")}
        ok, reason = confine(sess, "memory_read", args)
        if not ok:
            return h.json({"error": "forbidden", "detail": reason}, 403)
        path = memory._note_path(memory._scope_dir(args["scope"]), args["name"])
        if not args["name"] or not path.exists():
            return h.json({"error": "not found"}, 404)
        meta, body = memory.read_meta(path)
        return h.json({"name": path.stem, "scope": args["scope"], "meta": meta, "body": body})

    @mcp.custom_route("/ui/api/memory", methods=["POST"])
    async def _memory_write(request):
        sess, body = await session_and_body(request)
        if body is None:
            return sess
        args = {k: str(body.get(k, "") or "") for k in
                ("title", "content", "type", "project", "tags", "review", "scope")}
        args["scope"] = args["scope"] or "shared"
        if body.get("expected_version") not in (None, ""):
            try:
                args["expected_version"] = int(body["expected_version"])
            except (TypeError, ValueError):
                return h.json({"error": "expected_version must be a number"}, 400)
        ok, text = await run_tool(sess, "memory_write", args)
        if not ok:
            return h.json({"error": "forbidden", "detail": text}, 403)
        saved = _SAVED.search(text)
        if text.startswith("Not saved"):
            return h.json({"ok": False, "message": text}, 409)
        if not saved:
            return h.json({"ok": False, "message": text}, 400)
        h.audit(sess, "ui:memory_write", "allow", f"name={saved.group(1)}")
        return h.json({"ok": True, "message": text.splitlines()[0],
                       "name": saved.group(1), "version": int(saved.group(2))})

    @mcp.custom_route("/ui/api/memory/delete", methods=["POST"])
    async def _memory_delete(request):
        sess, body = await session_and_body(request)
        if body is None:
            return sess
        args = {"name": str(body.get("name", "")), "scope": str(body.get("scope", "shared"))}
        ok, text = await run_tool(sess, "memory_delete", args)
        if not ok:
            return h.json({"error": "forbidden", "detail": text}, 403)
        if not text.startswith("Deleted"):
            return h.json({"ok": False, "message": text}, 404)
        h.audit(sess, "ui:memory_delete", "allow", f"name={args['name']}")
        return h.json({"ok": True, "message": text})

    # Candidates are one shared queue (auto-captured from everyone's work), so
    # reviewing them is the admin's job.
    @mcp.custom_route("/ui/api/memory/candidates", methods=["GET"])
    async def _candidates(request):
        sess = h.require_admin(request)
        if isinstance(sess, JSONResponse):
            return sess
        return h.json({"candidates": memory.entries(memory.CANDIDATES_SCOPE)})

    for action in ("promote", "reject"):
        async def _decide(request, action=action):
            sess, body = await session_and_body(request, admin=True)
            if body is None:
                return sess
            args = {"name": str(body.get("name", ""))}
            if action == "promote":
                args["scope"] = str(body.get("scope", "shared"))
            ok, text = await run_tool(sess, f"memory_{action}", args)
            if not ok:
                return h.json({"error": "forbidden", "detail": text}, 403)
            done = text.startswith(("Promoted", "Rejected"))
            if done:
                h.audit(sess, f"ui:memory_{action}", "allow", f"name={args['name']}")
            return h.json({"ok": done, "message": text}, 200 if done else 404)
        mcp.custom_route(f"/ui/api/memory/candidates/{action}", methods=["POST"])(_decide)
