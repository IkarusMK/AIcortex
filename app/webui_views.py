"""WebUI views of a person's own area — notes now; jobs and playbook runs follow —
plus the admin's registry editor.

Every route takes the SAME policy path as a REST or MCP call: ``authz.enforce_rest``
(role decision, memory-scope confinement, device areas, audit) and
``authz.rest_identity`` while the tool runs. The browser therefore gets exactly the
rights the person's AI has — a viewer reads, a user works in their own area, an
admin manages — and the logic lives once, in the tools.
"""
import json
import os
import re
from pathlib import Path

import authz
import cfgstore
import cron
import memory

_SAVED = re.compile(r"memory '([^']+)'.*\(version (\d+)\)")
_REGISTERED = re.compile(r"'([^']+)'")
_STEM = re.compile(r"^[a-z0-9][a-z0-9._-]{0,100}$")

# Registry (as in webui._DEVICE_DIRS) → its upsert tool and its delete tool.
REGISTRIES = {
    "services": ("service_add", "service_delete"),
    "mqtt": ("mqtt_add", "mqtt_delete"),
    "ftp": ("ftp_add", "ftp_delete"),
    "printers": ("print_add", "print_delete"),
    "scanners": ("scan_add", "scan_delete"),
    "webdav": ("webdav_add", "webdav_delete_endpoint"),
    "caldav": ("caldav_add", "caldav_delete_endpoint"),
    "ssh": ("ssh_add", "ssh_delete_endpoint"),
    "mail": ("mail_add", "mail_delete_account"),
    "imap": ("imap_add", "imap_delete_account"),
    "mcp": ("mcp_add", "mcp_delete"),
    "webhooks": ("webhook_add", "webhook_delete"),
}


def _text(result) -> str:
    return "\n".join(getattr(c, "text", "") for c in (getattr(result, "content", None) or []))


def _fields(tool) -> list[dict]:
    """Form fields from a tool's input schema: name, type, required, default, enum,
    and whether it names a vault secret (``*_env``)."""
    schema = getattr(tool, "parameters", None) or {}
    required = set(schema.get("required") or [])
    out = []
    for name, prop in (schema.get("properties") or {}).items():
        types = [o.get("type") for o in prop.get("anyOf", []) if o.get("type") != "null"]
        out.append({"name": name, "type": prop.get("type") or (types[0] if types else "string"),
                    "required": name in required, "default": prop.get("default"),
                    "enum": prop.get("enum"), "secret_ref": name.endswith("_env")})
    return out


def _first_sentence(text) -> str:
    flat = " ".join(str(text or "").split())
    return re.split(r"(?<=[.!?])\s", flat, maxsplit=1)[0][:200]


def _coerce(field: dict, value):
    """A form value as the type the tool expects; ValueError names the field."""
    kind = field["type"]
    try:
        if value is None:
            return None
        if kind == "boolean":
            return value if isinstance(value, bool) else str(value).lower() in ("1", "true", "yes", "on")
        if kind == "integer":
            return int(value)
        if kind == "number":
            return float(value)
        if kind in ("object", "array"):
            return json.loads(value) if isinstance(value, str) else value
        return str(value)
    except (TypeError, ValueError) as exc:
        raise ValueError(f"{field['name']}: expected {kind}") from exc


def _registry_dir(kind: str) -> Path:
    import webui
    env, default = webui._DEVICE_DIRS[kind]
    return Path(os.environ.get(env, default))


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

    # ── services and devices (admin): forms built from the *_add tools ─────────
    async def kind_fields(kind: str) -> list[dict]:
        tool = await mcp.get_tool(REGISTRIES[kind][0])
        return _fields(tool) if tool else []

    @mcp.custom_route("/ui/api/registry/schema", methods=["GET"])
    async def _registry_schema(request):
        sess = h.require_admin(request)
        if isinstance(sess, JSONResponse):
            return sess
        kinds = []
        for kind, (add, _) in REGISTRIES.items():
            tool = await mcp.get_tool(add)
            if tool:
                kinds.append({"kind": kind, "tool": add, "fields": _fields(tool),
                              "help": _first_sentence(tool.description)})
        return h.json({"kinds": kinds})

    @mcp.custom_route("/ui/api/registry/{kind}/{name}", methods=["GET"])
    async def _registry_get(request):
        sess = h.require_admin(request)
        if isinstance(sess, JSONResponse):
            return sess
        kind, name = request.path_params.get("kind", ""), request.path_params.get("name", "")
        if kind not in REGISTRIES or not _STEM.match(name):
            return h.json({"error": "not found"}, 404)
        path = _registry_dir(kind) / f"{name}.json"
        try:
            stored = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return h.json({"error": "not found"}, 404)
        # Only fields the form knows leave the server — never the raw config.
        known = {f["name"] for f in await kind_fields(kind)}
        return h.json({"kind": kind, "name": name,
                       "config": {k: v for k, v in stored.items() if k in known}})

    @mcp.custom_route("/ui/api/registry/{kind}", methods=["POST"])
    async def _registry_save(request):
        sess, body = await session_and_body(request, admin=True)
        if body is None:
            return sess
        kind = request.path_params.get("kind", "")
        if kind not in REGISTRIES:
            return h.json({"error": "not found"}, 404)
        fields = {f["name"]: f for f in await kind_fields(kind)}
        try:
            args = {k: _coerce(fields[k], v) for k, v in body.items() if k in fields}
        except ValueError as exc:
            return h.json({"ok": False, "message": str(exc)}, 400)
        missing = [n for n, f in fields.items() if f["required"] and args.get(n) in (None, "")]
        if missing:
            return h.json({"ok": False, "message": f"required: {', '.join(missing)}"}, 400)
        try:
            with cfgstore.given(args):
                ok, text = await run_tool(sess, REGISTRIES[kind][0], args)
        except Exception as exc:  # pydantic validation and the like
            return h.json({"ok": False, "message": str(exc).splitlines()[0]}, 400)
        if not ok:
            return h.json({"error": "forbidden", "detail": text}, 403)
        if not text.startswith("Registered"):
            return h.json({"ok": False, "message": text}, 400)
        saved = _REGISTERED.search(text)
        h.audit(sess, f"ui:{REGISTRIES[kind][0]}", "allow", f"name={args.get('name', '')}")
        return h.json({"ok": True, "message": text, "name": saved.group(1) if saved else ""})

    @mcp.custom_route("/ui/api/registry/{kind}/delete", methods=["POST"])
    async def _registry_delete(request):
        sess, body = await session_and_body(request, admin=True)
        if body is None:
            return sess
        kind = request.path_params.get("kind", "")
        if kind not in REGISTRIES:
            return h.json({"error": "not found"}, 404)
        args = {"name": str(body.get("name", ""))}
        ok, text = await run_tool(sess, REGISTRIES[kind][1], args)
        if not ok:
            return h.json({"error": "forbidden", "detail": text}, 403)
        if not text.startswith("Deleted"):
            return h.json({"ok": False, "message": text}, 404)
        h.audit(sess, f"ui:{REGISTRIES[kind][1]}", "allow", f"name={args['name']}")
        return h.json({"ok": True, "message": text})

    # ── scheduled jobs (own jobs; admins see all) ─────────────────────────────
    @mcp.custom_route("/ui/api/cron", methods=["GET"])
    async def _cron_list(request):
        sess = h.require_session(request)
        if isinstance(sess, JSONResponse):
            return sess
        ok, reason = confine(sess, "cron_list", {})
        if not ok:
            return h.json({"error": "forbidden", "detail": reason}, 403)
        sub, role = who(sess)
        with authz.rest_identity(sub, role):
            rows = cron.job_rows()
        return h.json({"jobs": rows})

    @mcp.custom_route("/ui/api/cron/preview", methods=["GET"])
    async def _cron_preview(request):
        sess = h.require_session(request)
        if isinstance(sess, JSONResponse):
            return sess
        expr = request.query_params.get("schedule", "")
        problem = cron.schedule_problem(expr)
        if problem:
            return h.json({"ok": False, "message": problem})
        runs, at = [], None
        for _ in range(3):
            at = cron.next_run(expr, at)
            if at is None:
                break
            runs.append(at.strftime("%Y-%m-%dT%H:%M"))
        return h.json({"ok": True, "next": runs})

    @mcp.custom_route("/ui/api/cron", methods=["POST"])
    async def _cron_save(request):
        sess, body = await session_and_body(request)
        if body is None:
            return sess
        args = {"name": str(body.get("name", "")).strip(),
                "schedule": " ".join(str(body.get("schedule", "")).split()),
                "prompt": str(body.get("prompt", "")),
                "notify": str(body.get("notify", "") or "user"),
                "enabled": body.get("enabled", True) is not False}
        if str(body.get("owner", "")).strip():
            args["owner"] = str(body["owner"]).strip()
        problem = cron.schedule_problem(args["schedule"])
        if not args["name"] or problem:
            return h.json({"ok": False, "message": problem or "name is required"}, 400)
        ok, text = await run_tool(sess, "cron_add", args)
        if not ok:
            return h.json({"error": "forbidden", "detail": text}, 403)
        if not text.startswith("Scheduled"):
            return h.json({"ok": False, "message": text}, 400)
        h.audit(sess, "ui:cron_add", "allow", f"name={args['name']}")
        return h.json({"ok": True, "message": text})

    @mcp.custom_route("/ui/api/cron/delete", methods=["POST"])
    async def _cron_delete(request):
        sess, body = await session_and_body(request)
        if body is None:
            return sess
        args = {"name": str(body.get("name", ""))}
        ok, text = await run_tool(sess, "cron_delete", args)
        if not ok:
            return h.json({"error": "forbidden", "detail": text}, 403)
        if not text.startswith("Deleted"):
            return h.json({"ok": False, "message": text}, 404)
        h.audit(sess, "ui:cron_delete", "allow", f"name={args['name']}")
        return h.json({"ok": True, "message": text})

    # ── playbooks (library + a person's runs) ─────────────────────────────────
    import types

    import webui_playbooks
    webui_playbooks.register(mcp, h, types.SimpleNamespace(
        who=who, confine=confine, run_tool=run_tool, session_and_body=session_and_body))
