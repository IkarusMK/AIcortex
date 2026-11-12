"""Who may use a person's sign-in to an OAuth MCP server — the person decides.

A sign-in (mcp_oauth) belongs to one person. Up to 2.6 every call acting as that person
used it: their interactive apps, each of their agent keys with mcp_call in scope, and
any of their scheduled jobs on any runner (act-as) — a runner asking a third-party
model included. Now each person decides, per server, which of their channels may:

- ``webui`` — the person in the AICortex WebUI: always (it is their own login);
- ``app`` — an interactive app (``allow_apps``), known by the redirect host of the OAuth
  client it registered here (pocketid_proxy.APP_CLAIM): claude.ai, chatgpt.com,
  "localhost" for desktop/CLI apps. "(local)" stands for an unauthenticated connection
  to a server without sign-in — a marker no host name can be (until 2.7.0's review it
  was "local", which a real redirect host can be; stored settings are read with the
  new marker, see ``settings``);
- ``runner`` — a runner carrying out one of their jobs (``allow_runners``: runner ids,
  "external" for an own program with RUNNER_TOKEN), known by the token it calls with;
- ``key`` — one of their agent keys (``allow_keys``: key ids).

Everything else — and every call that can't be told apart — is refused, and every
decision is in the audit log with its channel. ``approve_writes`` (default on): a tool
the server does not mark readOnlyHint does not run on such a call; it waits for the
person's approval in the WebUI (mcp_approvals).

The settings live in the server's config, under ``access`` → person. A person with
nothing stored there gets the strict defaults (WebUI only, approvals on) — that is
also what every server from before has. Only the person in the WebUI may widen their
settings; a tool call (``mcp_access``) may only narrow them.
"""
import os
import re
from datetime import datetime, timezone

import store

DEFAULTS = {"allow_apps": [], "allow_runners": [], "allow_keys": [], "approve_writes": True}
LISTS = ("allow_apps", "allow_runners", "allow_keys")
LOCAL = "(local)"
_OLD_LOCAL = "local"           # the marker before: read as LOCAL, never an app any more
_ENTRY = re.compile(r"[A-Za-z0-9][A-Za-z0-9._:-]{0,199}")
_MAX_ENTRIES = 50
_SEEN_MAX = 100
_KIND_OF = {"app": "allow_apps", "local": "allow_apps", "runner": "allow_runners",
            "key": "allow_keys"}


class Invalid(ValueError):
    """A setting that can't be stored (wrong type or a malformed entry)."""


# ── the settings ─────────────────────────────────────────────────────────────
def _apps(names: list) -> list:
    """Apps with the old marker of a connection without sign-in as the new one."""
    return list(dict.fromkeys(LOCAL if n == _OLD_LOCAL else n for n in names))


def settings(cfg, person: str) -> dict:
    """`person`'s settings for this server (a copy), the strict defaults filled in —
    and an app "local" stored before as the connection without sign-in it meant."""
    own = (((cfg or {}).get("access") or {}).get(person) if isinstance(cfg, dict) else None)
    own = own if isinstance(own, dict) else {}
    out = {k: [str(v) for v in own.get(k) or [] if isinstance(v, str)] for k in LISTS}
    out["allow_apps"] = _apps(out["allow_apps"])
    out["approve_writes"] = own.get("approve_writes") is not False
    return out


def _clean(changes: dict) -> dict:
    out = {}
    for k in LISTS:
        if changes.get(k) is None:
            continue
        v = changes[k]
        if not isinstance(v, list) or len(v) > _MAX_ENTRIES:
            raise Invalid(f"{k} must be a list of at most {_MAX_ENTRIES} names.")
        names = [str(x).strip() for x in v]
        if k == "allow_apps":
            names = _apps(names)
        bad = [x for x in names if not (_ENTRY.fullmatch(x) or (k == "allow_apps" and x == LOCAL))]
        if bad:
            raise Invalid(f"{k}: '{bad[0][:40]}' is not a host, runner id or key id.")
        out[k] = list(dict.fromkeys(names))
    if changes.get("approve_writes") is not None:
        if not isinstance(changes["approve_writes"], bool):
            raise Invalid("approve_writes must be true or false.")
        out["approve_writes"] = changes["approve_writes"]
    return out


def widens(old: dict, new: dict) -> list:
    """What `new` would allow beyond `old` (empty when it only narrows)."""
    more = [f"{k}: {', '.join(sorted(set(new[k]) - set(old[k])))}"
            for k in LISTS if set(new[k]) - set(old[k])]
    if old["approve_writes"] and not new["approve_writes"]:
        more.append("approve_writes: off")
    return more


def update(server: str, person: str, changes: dict, by_person: bool) -> tuple[bool, str, dict]:
    """Apply `changes` to `person`'s settings for `server`: (ok, message, settings).
    Without `by_person` (the person in the WebUI) only narrowing is accepted."""
    import mcp_gateway
    if not person:
        return False, "Refused: cannot tell whose settings these are.", {}
    clean = _clean(changes)              # raises Invalid
    path = mcp_gateway._cfg_path(server)
    with store.locked(path):
        data = store.read_json(path, None)
        if not isinstance(data, dict):
            return False, f"Unknown MCP server '{server}'.", {}
        old = settings(data, person)
        new = {**old, **clean}
        more = widens(old, new)
        if more and not by_person:
            return False, ("Refused: only you, in the AICortex WebUI (Services → "
                           f"{mcp_gateway._slug(server)} → Access), can allow more "
                           f"({'; '.join(more)}). A tool call can only take permissions "
                           "away."), old
        access = data.get("access") if isinstance(data.get("access"), dict) else {}
        stamp = datetime.now(timezone.utc).isoformat(timespec="seconds")
        store.write_json(path, {**data, "access": {**access, person: {**new, "updated": stamp}}})
    return True, "Saved.", new


# ── the channel a call comes through ─────────────────────────────────────────
def _oidc() -> bool:
    return bool(os.environ.get("OIDC_CONFIG_URL") and os.environ.get("OIDC_CLIENT_ID"))


def _token() -> str:
    try:
        from fastmcp.server.dependencies import get_access_token
        return str(getattr(get_access_token(), "token", "") or "")
    except Exception:
        return ""


def channel() -> dict:
    """{"kind", "id"} of the current call: webui, app, runner, key, local — or
    unknown (id "") when it can't be told."""
    import authz
    if authz.from_webui():
        return {"kind": "webui", "id": "webui"}
    if authz._REST_IDENTITY.get():                    # REST: an agent key
        key = authz.rest_key()
        return {"kind": "key", "id": key} if key else {"kind": "unknown", "id": ""}
    ident, is_runner, claims = authz._identity()
    claims = claims if isinstance(claims, dict) else {}
    import actas
    acting = actas.current(authz.binding_key(ident))
    if is_runner or acting:
        # A job run as its owner: the runner is the token it calls with — an act-as
        # run started by anyone else (an admin's app) has no runner, so no access.
        import agent_auth
        rid = agent_auth.runner_of(_token()) if is_runner else ""
        return {"kind": "runner", "id": rid}
    if claims.get("aicortex_key"):
        return {"kind": "key", "id": str(claims["aicortex_key"])}
    import pocketid_proxy
    app = claims.get(pocketid_proxy.APP_CLAIM)
    if isinstance(app, dict) and app.get("host"):
        if not pocketid_proxy.is_app_host(app["host"]):
            return {"kind": "unknown", "id": ""}
        return {"kind": "app", "id": str(app["host"]), "name": str(app.get("name") or "")}
    if ident == "unknown" and not claims and not _token() and not _oidc():
        return {"kind": "local", "id": LOCAL}
    return {"kind": "unknown", "id": ""}


def label(ch: dict) -> str:
    """The channel in words, for a refusal."""
    if not ch.get("id"):
        return "a caller AICortex cannot identify"
    return {"webui": "the WebUI", "app": f"the app {ch['id']}", "runner": f"runner {ch['id']}",
            "key": f"agent key {ch['id']}", "local": "a connection without sign-in"}[ch["kind"]]


def tag(ch: dict) -> str:
    """The channel as the audit log and an approval show it, e.g. app:claude.ai."""
    return f"{ch['kind']}:{ch.get('id') or '?'}"


# ── the check ────────────────────────────────────────────────────────────────
def check(server: str, cfg: dict, person: str, tool: str) -> tuple[bool, dict, str]:
    """May the current call use `person`'s sign-in to `server`? (ok, channel, refusal)."""
    import authz
    import mcp_gateway
    slug = mcp_gateway._slug(server)
    ch = channel()
    if ch["kind"] == "app":
        note_app(ch["id"], ch.get("name", ""))
    allowed = ch["kind"] == "webui" or (
        bool(ch["id"]) and ch["kind"] in _KIND_OF
        and ch["id"] in settings(cfg, person)[_KIND_OF[ch["kind"]]])
    authz.audit(person or "unknown", authz.effective_identity()[1], tool,
                "allow" if allowed else "deny",
                f"oauth sign-in of '{slug}' via {tag(ch)}"
                + ("" if allowed else " — not allowed by the person"))
    if allowed:
        return True, ch, ""
    return False, ch, (f"Refused: {person or 'the person'} has not allowed {label(ch)} to use "
                       f"their sign-in to '{slug}'. Only they can, in the AICortex WebUI → "
                       f"Services → {slug} → Access.")


# ── apps seen so far (for the WebUI's choices) ───────────────────────────────
def _seen_file():
    import mcp_gateway
    return mcp_gateway.MCP_DIR / "_access" / "apps.json"


def note_app(host: str, name: str = "") -> None:
    """Remember an app that asked for a sign-in, so the person can pick it."""
    path = _seen_file()
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        with store.locked(path):
            seen = store.read_json(path, {})
            seen = seen if isinstance(seen, dict) else {}
            if seen.get(host, {}).get("name") == name:
                return
            if host not in seen and len(seen) >= _SEEN_MAX:
                return
            store.write_json(path, {**seen, host: {"name": name[:80]}})
    except OSError:
        pass                          # only a convenience for the WebUI


def apps_seen() -> dict:
    seen = store.read_json(_seen_file(), {})
    return seen if isinstance(seen, dict) else {}


def show(server: str, cfg: dict, person: str) -> str:
    """The settings as text for a tool answer."""
    s = settings(cfg, person)
    names = lambda k: ", ".join(s[k]) or "none"        # noqa: E731
    return (f"Access to {person}'s sign-in to '{server}': apps: {names('allow_apps')}; "
            f"runners: {names('allow_runners')}; agent keys: {names('allow_keys')}; "
            f"changes wait for approval: {'yes' if s['approve_writes'] else 'no'}. "
            f"This call comes from {label(channel())}. Only the person can allow more, in "
            f"the AICortex WebUI → Services → {server} → Access.")


def register(mcp):
    @mcp.tool
    def mcp_access(server: str, allow_apps: list[str] | None = None,
                   allow_runners: list[str] | None = None, allow_keys: list[str] | None = None,
                   approve_writes: bool | None = None) -> str:
        """Show or NARROW who may use your sign-in to an OAuth MCP server: apps (by host,
        e.g. claude.ai; "(local)" = without sign-in), runners (ids), agent keys (ids), and
        whether tools that change data wait for your approval in the WebUI. Pass a
        list to keep only those entries, approve_writes=true to switch approvals on.
        Allowing more is only possible for the person in the AICortex WebUI."""
        import mcp_gateway
        import mcp_oauth
        cfg = mcp_gateway._load(server)
        if not mcp_oauth.is_oauth(cfg):
            return f"'{server}' is no OAuth MCP server (mcp_list)."
        person = mcp_oauth.caller()
        changes = {"allow_apps": allow_apps, "allow_runners": allow_runners,
                   "allow_keys": allow_keys, "approve_writes": approve_writes}
        slug = mcp_gateway._slug(server)
        if all(v is None for v in changes.values()):
            return show(slug, cfg, person)
        import authz
        try:
            ok, msg, _ = update(slug, person, changes, by_person=authz.from_webui())
        except Invalid as exc:
            return f"Refused: {exc}"
        authz.audit(person or "unknown", authz.effective_identity()[1], "mcp_access",
                    "allow" if ok else "deny",
                    f"server={slug} via {tag(channel())}: {msg[:160]}")
        return show(slug, mcp_gateway._load(server), person) if ok else msg
