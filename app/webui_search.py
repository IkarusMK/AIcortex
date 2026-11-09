"""WebUI tab "Search" (admin): one field that finds everything the WebUI holds —
skills, tools, secrets, services & devices, cron jobs, runners, playbooks and their
runs, notes, connected clients and users.

Admin-only, like the management tabs it searches — so it shows nobody more than they
could already see. Every group is built from CONSTRUCTED fields, never a raw record:
secrets by name and description (``vault_entries()`` carries no value), runners and
clients without their keys, services with the secret's NAME only. Tools are matched
with the vocabulary of integration_search (``profiles._terms``), so German words
("drucken", "scannen") find English tools. The audit log is left out: the Logs tab
has its own filter.
"""
import time

import authz
import cron
import memory
import playbooks
import profiles
import runners
import secrets_store

MIN_QUERY = 2
DEFAULT_LIMIT, MAX_LIMIT = 20, 50
GROUPS = ("skills", "tools", "secrets", "services", "cron", "runners", "playbooks", "runs",
          "notes", "clients", "users")


def parse_limit(raw) -> int:
    """Results per group: 1…MAX_LIMIT, DEFAULT_LIMIT for anything unusable."""
    try:
        n = int(raw)
    except (TypeError, ValueError):
        return DEFAULT_LIMIT
    return max(1, min(n, MAX_LIMIT))


def _score(terms: dict, raw: str, name: str, text: str) -> int:
    """Name hits weigh more than hits in the rest; the whole query inside the name
    ranks first (typing part of a name is the common case)."""
    name_l, text_l = str(name or "").lower(), str(text or "").lower()
    score = sum(w * (3 * (t in name_l) + (t in text_l)) for t, w in terms.items())
    if raw in name_l:
        score += 10
    elif raw in text_l:
        score += 2
    return score


def _short(text, n: int = 200) -> str:
    return " ".join(str(text or "").split())[:n]


def _snippet(body: str, raw: str, terms: dict) -> str:
    """The note line that matched (the whole query first), else its first line."""
    lines = [ln.strip() for ln in str(body or "").splitlines() if ln.strip()]
    for wanted in ([raw], list(terms)):
        for ln in lines:
            if any(w and w in ln.lower() for w in wanted):
                return _short(ln, 160)
    return _short(lines[0], 160) if lines else ""


def _key_status(rec: dict) -> str:
    if rec.get("disabled") or rec.get("revoked"):
        return "disabled"
    exp = rec.get("expires") or 0
    return "expired" if exp and exp < time.time() else "active"


# group → (name to match, rest to match, the item the browser gets)
_SPECS = {
    "skills": (lambda r: f"{r['name']} {r['title']}",
               lambda r: f"{r['description']} {r['category']} {r['tags']}",
               lambda r, q: {"name": r["name"], "title": r["title"], "category": r["category"],
                             "summary": _short(r["description"])}),
    "tools": (lambda t: f"{t.name} {t.name.replace('_', ' ')}",
              lambda t: t.description or "",
              lambda t, q: {"name": t.name, "signature": profiles._signature(t),
                            "summary": profiles._summary(t)}),
    "secrets": (lambda s: s["name"],
                lambda s: s.get("description", ""),
                lambda s, q: {"name": s["name"], "owner_label": s.get("owner_label", ""),
                              "shared": not s.get("owner"), "description": s.get("description", "")}),
    "services": (lambda r: r["name"],
                 lambda r: f"{r['kind']} {r['target']} {r['description']} {r['secret']}",
                 lambda r, q: {k: r[k] for k in ("kind", "name", "target", "description", "secret")}),
    "cron": (lambda j: j.get("name", ""),
             lambda j: f"{j.get('prompt', '')} {j.get('schedule', '')} {j.get('owner', '')} {j.get('runner', '')}",
             lambda j, q: {"id": j.get("id", ""), "name": j.get("name", ""),
                           "schedule": j.get("schedule", ""), "owner": j.get("owner", ""),
                           "enabled": bool(j.get("enabled")), "summary": _short(j.get("prompt"), 160)}),
    "runners": (lambda r: r.get("name", ""),
                lambda r: f"{r.get('kind', '')} {r.get('owner', '')} {r.get('model', '')} {r.get('note', '')}",
                lambda r, q: {"id": r.get("id", ""), "name": r.get("name", ""), "kind": r.get("kind", ""),
                              "owner": r.get("owner", ""), "enabled": bool(r.get("enabled"))}),
    "playbooks": (lambda p: f"{p['name']} {p.get('title', '')}",
                  lambda p: f"{p.get('description', '')} {p.get('category', '')}",
                  lambda p, q: {"name": p["name"], "title": p.get("title", ""),
                                "category": p.get("category", ""), "summary": _short(p.get("description"))}),
    "runs": (lambda r: f"{r.get('title', '')} {r.get('playbook_title', '')}",
             lambda r: f"{r.get('project', '')} {r.get('person', '')} {r.get('status', '')} {r.get('playbook', '')}",
             lambda r, q: {k: r.get(k, "") for k in ("id", "title", "playbook_title", "project",
                                                     "person", "status")}),
    "notes": (lambda n: n["title"],
              lambda n: f"{n['tags']} {n['project']} {n['body']}",
              lambda n, q: {"name": n["name"], "title": n["title"], "type": n["type"],
                            "project": n["project"], "summary": _snippet(n["body"], *q)}),
    "clients": (lambda k: k.get("name", "") or k.get("keyid", ""),
                lambda k: f"{k.get('sub', '')} {k.get('keyid', '')} {k.get('scopes', '')}",
                lambda k, q: {"keyid": k.get("keyid", ""), "name": k.get("name", ""),
                              "owner": k.get("sub", ""), "status": _key_status(k)}),
    "users": (lambda u: u["identity"],
              lambda u: f"{u.get('role', '')} {u.get('note', '')}",
              lambda u, q: {"identity": u["identity"], "role": u.get("role", ""),
                            "note": u.get("note", "")}),
}


def _rank(rows, spec, raw: str, terms: dict, limit: int) -> list:
    name_of, text_of, item_of = spec
    scored = []
    for i, r in enumerate(rows or ()):
        s = _score(terms, raw, name_of(r), text_of(r))
        if s > 0:
            scored.append((-s, str(name_of(r)).lower(), i, r))
    scored.sort(key=lambda x: x[:3])
    return [item_of(r, (raw, terms)) for *_, r in scored[:limit]]


def search(query, sources: dict, limit: int = DEFAULT_LIMIT) -> dict:
    """Pure ranking: `sources` maps a group to its rows (missing group = none)."""
    q = " ".join(str(query or "").split())
    out = {"query": q, **{g: [] for g in GROUPS}}
    if len(q) < MIN_QUERY:
        return out
    raw, terms = q.lower(), profiles._terms(q)
    for group in GROUPS:
        out[group] = _rank(sources.get(group), _SPECS[group], raw, terms, limit)
    return out


def _note_rows() -> list:
    """The shared notes — the scope an admin's Notes tab shows."""
    rows = []
    for p in sorted(memory.scope_path("shared").glob("*.md")):
        try:
            meta, body = memory.read_meta(p)
        except Exception:
            continue
        rows.append({"name": p.stem, "title": str(meta.get("title", p.stem)),
                     "type": memory.canon_type(meta.get("type", "")),
                     "project": str(meta.get("project", "")),
                     "tags": " ".join(str(t) for t in (meta.get("tags") or [])), "body": body})
    return rows


def register(mcp, h, rows):
    """`h` carries the WebUI helpers (json, require_admin, …); `rows` the WebUI's own
    row builders (skills, services, users) so each group matches what its tab lists."""
    from starlette.responses import JSONResponse

    import apikeys
    import webui_runner

    async def _sources(sess) -> dict:
        sub, role = str(sess.get("sub", "")), str(sess.get("role", "admin"))
        with authz.rest_identity(sub, role):
            jobs = cron.job_rows()
            runs = playbooks.runs_for(status="all")
            library = playbooks.listing()
        data = runners.load()
        return {
            "skills": rows.skills(),
            "tools": await mcp.list_tools(run_middleware=False),
            "secrets": secrets_store.vault_entries(),
            "services": rows.services(),
            "cron": jobs,
            "runners": [webui_runner._row(r, data, sub, True) for r in runners.visible(data, sub, True)],
            "playbooks": library,
            "runs": runs,
            "notes": _note_rows(),
            "clients": apikeys.list_keys(),
            "users": rows.users(),
        }

    @mcp.custom_route("/ui/api/search", methods=["GET"])
    async def _search(request):
        sess = h.require_admin(request)
        if isinstance(sess, JSONResponse):
            return sess
        params = request.query_params
        query, limit = params.get("q", ""), parse_limit(params.get("limit"))
        if len(" ".join(query.split())) < MIN_QUERY:          # nothing to rank, nothing to load
            return h.json(search(query, {}, limit))
        return h.json(search(query, await _sources(sess), limit))
