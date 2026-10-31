"""WebUI routes for playbooks: the library and a person's runs.

Same policy path as every WebUI view (see webui_views): each route is gated like
the matching playbook_* tool. Approving a stage here is the human decision the
playbook asks for — it is recorded "via webui" with the signed-in person, and only
a person who may see the run (its owner or an admin) can make it.
"""
import re

import authz
import playbooks

_RUN_ID = re.compile(r"Run ([a-f0-9]+)")


def _status(text: str) -> int:
    return 404 if text.startswith("No ") else 400


def register(mcp, h, b):
    """`h`: the WebUI helpers; `b`: the policy bridge from webui_views."""
    from starlette.responses import JSONResponse

    def gated(request, tool, args=None):
        sess = h.require_session(request)
        if isinstance(sess, JSONResponse):
            return sess
        ok, reason = b.confine(sess, tool, args if args is not None else {})
        return sess if ok else h.json({"error": "forbidden", "detail": reason}, 403)

    def as_person(sess):
        return authz.rest_identity(*b.who(sess))

    # ── library ──────────────────────────────────────────────────────────────
    @mcp.custom_route("/ui/api/playbooks", methods=["GET"])
    async def _library(request):
        sess = gated(request, "playbook_list")
        if isinstance(sess, JSONResponse):
            return sess
        return h.json({"playbooks": playbooks.listing()})

    @mcp.custom_route("/ui/api/playbooks/get", methods=["GET"])
    async def _playbook(request):
        sess = gated(request, "playbook_load")
        if isinstance(sess, JSONResponse):
            return sess
        d = playbooks.load(request.query_params.get("name", ""))
        return h.json(d) if d else h.json({"error": "not found"}, 404)

    @mcp.custom_route("/ui/api/playbooks", methods=["POST"])
    async def _playbook_save(request):
        # Saving here is a person's decision, so it may also relax a stage's approval
        # (via_webui) — the playbook_write tool, used by AIs, may not.
        sess, body = await b.session_and_body(request)
        if body is None:
            return sess
        ok, reason = b.confine(sess, "playbook_write", {})
        if not ok:
            return h.json({"error": "forbidden", "detail": reason}, 403)
        fields = {k: str(body.get(k, "") or "") for k in ("name", "title", "description",
                                                          "category", "guidance", "source")}
        try:
            with as_person(sess):
                text = playbooks.save(fields["name"], fields["title"], fields["description"],
                                      body.get("stages"), fields["category"], body.get("plays"),
                                      fields["guidance"], fields["source"], via_webui=True)
        except (TypeError, ValueError, AttributeError) as exc:
            text = f"Refused: {type(exc).__name__}"
        saved = text.startswith("Saved")
        if saved:
            h.audit(sess, "playbook_write", "allow", f"name={fields['name']}")
        return h.json({"ok": saved, "message": text}, 200 if saved else 400)

    @mcp.custom_route("/ui/api/playbooks/delete", methods=["POST"])
    async def _playbook_delete(request):
        sess, body = await b.session_and_body(request)
        if body is None:
            return sess
        ok, text = await b.run_tool(sess, "playbook_delete", {"name": str(body.get("name", ""))})
        if not ok:
            return h.json({"error": "forbidden", "detail": text}, 403)
        done = text.startswith("Deleted")
        return h.json({"ok": done, "message": text}, 200 if done else _status(text))

    @mcp.custom_route("/ui/api/playbooks/example", methods=["POST"])
    async def _example(request):
        # The bundled templates fill the shared library for everyone: an admin's job,
        # and never over an existing (possibly edited) playbook unless forced.
        sess, body = await b.session_and_body(request, admin=True)
        if body is None:
            return sess
        with as_person(sess):
            text = playbooks.install_example(str(body.get("name", "") or "ai-native-sdlc"),
                                             force=body.get("force") is True)
        saved = text.startswith("Saved")
        if saved:
            h.audit(sess, "playbook_example", "allow", text)
        return h.json({"ok": saved, "message": text}, 200 if saved else (409 if "exists" in text else 400))

    # ── runs ─────────────────────────────────────────────────────────────────
    def summary(run: dict) -> dict:
        return {k: run.get(k) for k in ("id", "title", "playbook", "playbook_title", "project",
                                        "person", "status", "current", "created", "updated")} | {
            "next_step": playbooks.next_step(run),
            "stages": [{k: s[k] for k in ("id", "name", "artifact", "status", "approval")}
                       for s in run["stages"]]}

    @mcp.custom_route("/ui/api/playbooks/runs", methods=["GET"])
    async def _runs(request):
        sess = gated(request, "playbook_runs")
        if isinstance(sess, JSONResponse):
            return sess
        q = request.query_params
        with as_person(sess):
            rows = playbooks.runs_for(project=q.get("project", ""), status=q.get("status", "active"))
        return h.json({"runs": [summary(r) for r in rows]})

    @mcp.custom_route("/ui/api/playbooks/runs/get", methods=["GET"])
    async def _run(request):
        sess = gated(request, "playbook_status")
        if isinstance(sess, JSONResponse):
            return sess
        with as_person(sess):
            run = playbooks._visible_run(request.query_params.get("id", ""))
        if not run:
            return h.json({"error": "not found"}, 404)
        return h.json({"run": run, "next_step": playbooks.next_step(run)})

    @mcp.custom_route("/ui/api/playbooks/runs/start", methods=["POST"])
    async def _start(request):
        sess, body = await b.session_and_body(request)
        if body is None:
            return sess
        args = {k: str(body.get(k, "")) for k in ("playbook", "project", "title")}
        ok, text = await b.run_tool(sess, "playbook_start", args)
        if not ok:
            return h.json({"error": "forbidden", "detail": text}, 403)
        found = _RUN_ID.search(text)
        if not text.startswith("Started") or not found:
            return h.json({"ok": False, "message": text}, _status(text))
        return h.json({"ok": True, "id": found.group(1), "message": text})

    @mcp.custom_route("/ui/api/playbooks/runs/submit", methods=["POST"])
    async def _submit(request):
        sess, body = await b.session_and_body(request)
        if body is None:
            return sess
        args = {"run_id": str(body.get("id", "")), **{k: str(body.get(k, "") or "") for k in
                                                       ("artifact", "ref_path", "ref_commit",
                                                        "ref_url", "note")}}
        ok, text = await b.run_tool(sess, "playbook_submit", args)
        if not ok:
            return h.json({"error": "forbidden", "detail": text}, 403)
        done = text.startswith("Submitted")
        return h.json({"ok": done, "message": text}, 200 if done else _status(text))

    @mcp.custom_route("/ui/api/playbooks/runs/decide", methods=["POST"])
    async def _decide(request):
        sess, body = await b.session_and_body(request)
        if body is None:
            return sess
        run_id, decision = str(body.get("id", "")), str(body.get("decision", ""))
        ok, reason = b.confine(sess, "playbook_record_approval", {"run_id": run_id})
        if not ok:
            return h.json({"error": "forbidden", "detail": reason}, 403)
        with as_person(sess):
            run, problem = playbooks.decide(run_id, decision, by=str(sess.get("sub", "")),
                                            via="webui", note=str(body.get("note", "")))
        if problem:
            return h.json({"ok": False, "message": problem}, _status(problem))
        h.audit(sess, "playbook_decide", "allow", f"run={run_id} decision={decision}")
        return h.json({"ok": True, "message": playbooks.next_step(run)})

    @mcp.custom_route("/ui/api/playbooks/runs/cancel", methods=["POST"])
    async def _cancel(request):
        sess, body = await b.session_and_body(request)
        if body is None:
            return sess
        ok, text = await b.run_tool(sess, "playbook_cancel", {"run_id": str(body.get("id", ""))})
        if not ok:
            return h.json({"error": "forbidden", "detail": text}, 403)
        done = text.startswith("Cancelled")
        return h.json({"ok": done, "message": text}, 200 if done else _status(text))
