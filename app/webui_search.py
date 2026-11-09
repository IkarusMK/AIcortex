"""WebUI tab "Search" (admin): one field that finds skills, tools and secrets.

Admin-only, like the vault and the skills library it searches — so it shows nobody
more than they could already see. Secrets are matched by NAME and description only:
``secrets_store.vault_entries()`` never carries a value, so none can leak, not even
as a match. Tools are matched with the vocabulary of integration_search
(``profiles._terms``), so German words ("drucken", "scannen") find English tools.
"""
import profiles
import secrets_store

MIN_QUERY = 2
DEFAULT_LIMIT, MAX_LIMIT = 20, 50


def parse_limit(raw) -> int:
    """Results per group: 1…MAX_LIMIT, DEFAULT_LIMIT for anything unusable."""
    try:
        n = int(raw)
    except (TypeError, ValueError):
        return DEFAULT_LIMIT
    return max(1, min(n, MAX_LIMIT))


def _score(terms: dict, raw: str, name: str, text: str) -> int:
    """Name hits weigh more than hits in the description; the whole query inside the
    name ranks first (typing part of a name is the common case)."""
    name_l, text_l = name.lower(), (text or "").lower()
    score = sum(w * (3 * (t in name_l) + (t in text_l)) for t, w in terms.items())
    if raw in name_l:
        score += 10
    elif raw in text_l:
        score += 2
    return score


def _top(scored, limit: int) -> list:
    """Rows with a score, best first (ties by name), at most `limit`."""
    hits = sorted((s for s in scored if s[0] > 0), key=lambda s: (-s[0], s[1]))
    return [row for _, _, row in hits[:limit]]


def _first_sentence(text: str) -> str:
    flat = " ".join(str(text or "").split())
    return flat[:200]


def search(query, skill_rows, tools, secret_rows, limit: int = DEFAULT_LIMIT) -> dict:
    """Pure ranking over the three sources; the route supplies them."""
    q = " ".join(str(query or "").split())
    out = {"query": q, "skills": [], "tools": [], "secrets": []}
    if len(q) < MIN_QUERY:
        return out
    raw, terms = q.lower(), profiles._terms(q)
    out["skills"] = _top((
        (_score(terms, raw, f"{r['name']} {r['title']}",
                f"{r['description']} {r['category']} {r['tags']}"),
         r["name"],
         {"name": r["name"], "title": r["title"], "category": r["category"],
          "summary": _first_sentence(r["description"])})
        for r in skill_rows), limit)
    out["tools"] = _top((
        (_score(terms, raw, f"{t.name} {t.name.replace('_', ' ')}", t.description or ""),
         t.name,
         {"name": t.name, "signature": profiles._signature(t), "summary": profiles._summary(t)})
        for t in tools), limit)
    out["secrets"] = _top((
        (_score(terms, raw, s["name"], s.get("description", "")),
         f"{s['name']}/{s.get('owner', '')}",
         # Constructed fields only — never the entry dict itself.
         {"name": s["name"], "owner_label": s.get("owner_label", ""),
          "shared": not s.get("owner"), "description": s.get("description", "")})
        for s in secret_rows), limit)
    return out


def register(mcp, h, skill_rows):
    """`h` carries the WebUI helpers (json, require_admin, …); `skill_rows` lists the
    skills library the way the Skills tab does."""
    from starlette.responses import JSONResponse

    @mcp.custom_route("/ui/api/search", methods=["GET"])
    async def _search(request):
        sess = h.require_admin(request)
        if isinstance(sess, JSONResponse):
            return sess
        params = request.query_params
        tools = await mcp.list_tools(run_middleware=False)
        return h.json(search(params.get("q", ""), skill_rows(), tools,
                             secrets_store.vault_entries(), parse_limit(params.get("limit"))))
