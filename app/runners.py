"""Runners: who carries out a person's scheduled jobs.

A runner is one way to get a due job done:
- ``claude-cloud``: AICortex wakes the person's Claude Code cloud task (its API
  trigger) and sends the job along; Claude runs it on the person's own plan and
  reaches AICortex through their connector (runner_cloud.py);
- ``chatgpt``: AICortex asks a model on the ChatGPT plan it signed in with
  (chatgpt_signin.py — one per AICortex, set up by an admin);
- ``anthropic`` / ``openai`` / ``openrouter`` / ``gemini``: AICortex asks the model
  with an API key; ``compatible``: any OpenAI-compatible address, e.g. Ollama in the LAN.

Every person may have their own runners; a job is assigned to one (cron.py). The
standard runner — one of an admin's own, set by an admin — takes the jobs nobody
assigned, as the single runner did up to 2.2.

Keys stay in the vault. A record holds only the NAME of the entry and whether it is
shared: a person may pick only their own entries, an admin shared ones as well, and
the server's internal names never — otherwise a key could be sent to an address of
someone else's choosing. A key typed in is kept as ``RUNNER_KEY_<id>`` in the owner's
area; the ``RUNNER_`` prefix is internal, so no service can use it.
"""
import os
import re
import secrets
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import urlsplit

import store

CRON_DIR = Path(os.environ.get("CRON_DIR", "/data/cron"))
RUNNERS_FILE = CRON_DIR / "runners.json"
LEGACY_FILE = CRON_DIR / "runner_config.json"        # 2.2 — read once, left in place
LEGACY_KEY = "RUNNER_LLM_KEY"

EXTERNAL = "external"          # an own program that fetches its jobs (RUNNER_TOKEN)
CLOUD = "claude-cloud"
CHATGPT = "chatgpt"
KEY_KINDS = ("anthropic", "openai", "openrouter", "gemini", "compatible")
KINDS = (CLOUD, CHATGPT) + KEY_KINDS
BUILTIN_KINDS = (CHATGPT,) + KEY_KINDS               # AICortex asks the model itself
MODEL_REQUIRED = ("openai", "openrouter", "gemini", "compatible")
KEY_REQUIRED = (CLOUD, "anthropic", "openai", "openrouter", "gemini")
LABELS = {CLOUD: "Claude (cloud task)", CHATGPT: "ChatGPT plan", "anthropic": "Anthropic",
          "openai": "OpenAI", "openrouter": "OpenRouter", "gemini": "Google Gemini",
          "compatible": "OpenAI-compatible"}
# 2.2's backend names → kinds. claude-abo (a setup-token on the Messages API) is
# refused by Anthropic, so such a setup becomes a switched-off cloud runner.
LEGACY_KINDS = {"claude-abo": CLOUD, "chatgpt-abo": CHATGPT, "claude-api": "anthropic",
                "anthropic": "anthropic", "openai": "openai", "openrouter": "openrouter",
                "gemini": "gemini"}
SETUP_TOKEN_NOTE = (  # nosec B105 - a message, not a secret
    "Claude via `claude setup-token` no longer works — Anthropic refuses the token outside "
    "Claude Code. Set up a Claude cloud task instead (see the Runner tab), then delete the "
    "old token RUNNER_LLM_KEY in the vault.")
FIRE_URL = "https://api.anthropic.com/v1/claude_code/routines/{trigger}/fire"
TRIGGER = re.compile(r"trig_[A-Za-z0-9_-]{4,80}")
NAME_MAX, MODEL_MAX, URL_MAX = 80, 200, 300
KEY_MAX = 1024             # characters of a key or token typed in
MAX_PER_PERSON = 10        # runners a non-admin may have


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _empty() -> dict:
    return {"version": 1, "standard": "", "runners": [], "personal": {}}


def _new_id(runners: list) -> str:
    taken = {r.get("id") for r in runners}
    while True:
        rid = "r_" + secrets.token_hex(3)
        if rid not in taken:
            return rid


# ── the registry ─────────────────────────────────────────────────────────────
def _migrated(cfg: dict) -> dict:
    """The 2.2 setup as a registry with one runner, "Standard"."""
    kind = LEGACY_KINDS.get(str(cfg.get("backend") or "").strip().lower())
    if not kind:
        return _empty()
    owner, now = str(cfg.get("updated_by") or ""), _now()
    rec = {"id": _new_id([]), "name": "Standard", "kind": kind, "owner": owner,
           "enabled": bool(cfg.get("builtin")), "model": str(cfg.get("model") or ""),
           "base_url": "", "trigger": "", "note": "",
           "key": None if kind == CHATGPT else {"name": LEGACY_KEY, "shared": True},
           "created": now, "updated": now, "updated_by": owner}
    if kind == CLOUD:
        rec = dict(rec, name="Claude (setup-token)", enabled=False, model="", key=None,
                   note=SETUP_TOKEN_NOTE)
        return {"version": 1, "standard": "", "runners": [rec]}
    return {"version": 1, "standard": rec["id"], "runners": [rec]}


def _clean(data) -> dict:
    if not isinstance(data, dict):
        return _empty()
    runners = [r for r in data.get("runners") or [] if isinstance(r, dict) and r.get("id")]
    ids = {r["id"] for r in runners}
    personal = data.get("personal") if isinstance(data.get("personal"), dict) else {}
    return {"version": 1, "standard": str(data.get("standard") or ""), "runners": runners,
            "personal": {str(k): v for k, v in personal.items() if v in ids}}


def load() -> dict:
    """The registry; a 2.2 setup is taken over on the first read."""
    with store.locked(RUNNERS_FILE):
        data = store.read_json(RUNNERS_FILE, None)
        if data is None:
            legacy = store.read_json(LEGACY_FILE, None)
            if not isinstance(legacy, dict) or not legacy.get("backend"):
                return _empty()
            data = _migrated(legacy)
            store.write_json(RUNNERS_FILE, data)
        return _clean(data)


def get(rid: str):
    return next((r for r in load()["runners"] if r["id"] == rid), None)


def effective(job_runner: str, data: dict) -> str:
    """The runner a job goes to: its own (even when switched off — then it waits), else
    the standard runner when that is on, else an external runner (as in 2.2)."""
    if job_runner == EXTERNAL:
        return EXTERNAL
    if any(r.get("id") == job_runner for r in data.get("runners", [])):
        return job_runner
    std = next((r for r in data.get("runners", []) if r.get("id") == data.get("standard")), None)
    return std["id"] if std and std.get("enabled") else EXTERNAL


def personal_default(data: dict, person: str) -> str:
    """A person's own default runner: the one they chose, else their only runner."""
    mine = [r["id"] for r in data.get("runners", []) if person and r.get("owner") == person]
    chosen = data.get("personal", {}).get(person)
    if chosen in mine:
        return chosen
    return mine[0] if len(mine) == 1 else ""


def route(job: dict, data: dict) -> str:
    """The runner a job goes to: the one it names (switched off: the job waits); else
    its person's own default runner when that is on and may take it; else the standard
    runner when on; else an external one (as up to 2.2)."""
    named = job.get("runner", "")
    if named == EXTERNAL or any(r.get("id") == named for r in data.get("runners", [])):
        return effective(named, data)
    owner, by = job.get("owner", ""), job.get("updated_by", "")
    rid = personal_default(data, owner or by)
    rec = next((r for r in data.get("runners", []) if r["id"] == rid), None)
    if rec and rec.get("enabled") and may_run(rec, data.get("standard", ""), owner, by):
        return rid
    return effective("", data)


def may_run(rec: dict, standard_id: str, job_owner: str, job_by: str) -> bool:
    """May runner `rec` carry out a job owned by `job_owner`, last written by `job_by`?

    A cloud runner runs a job in its owner's own Claude account, with all their
    connectors — so only prompts that owner wrote, in their own area. The standard
    runner takes anyone's job; any other runner its owner's jobs (confined to the job
    owner's area through act-as, as before)."""
    if rec.get("kind") == CLOUD:
        return bool(job_by) and job_by == rec.get("owner") and job_owner in ("", rec.get("owner"))
    if rec.get("id") == standard_id:
        return True
    owner = rec.get("owner")
    return bool(owner) and (job_by == owner or (bool(job_owner) and job_owner == owner))


def visible(data: dict, sub: str, admin: bool) -> list:
    """The runners a person sees: an admin all, anyone else their own and the standard."""
    return [r for r in data["runners"]
            if admin or r.get("owner") == sub or r["id"] == data["standard"]]


def public(rec: dict) -> dict:
    """A runner as the WebUI shows it — the key only by name, never its value."""
    keys = ("id", "name", "kind", "owner", "enabled", "model", "base_url", "trigger", "note",
            "updated", "updated_by")
    ref = rec.get("key")
    return dict({k: rec.get(k, "") for k in keys},
                key={"name": ref["name"], "shared": bool(ref.get("shared"))} if ref else None)


def external_available() -> bool:
    """Can an own program fetch jobs — RUNNER_TOKEN set, or a runner key from 2.1?"""
    if os.environ.get("RUNNER_TOKEN"):
        return True
    try:
        import json

        import apikeys
        return any(json.loads(p.read_text(encoding="utf-8")).get("runner")
                   for p in apikeys.APIKEY_DIR.glob("*.json"))
    except Exception:
        return False


def fire_url(rec: dict) -> str:
    return FIRE_URL.format(trigger=rec["trigger"]) if TRIGGER.fullmatch(rec.get("trigger") or "") else ""


# ── keys ─────────────────────────────────────────────────────────────────────
def usable_key_names(sub: str, admin: bool) -> list:
    """Vault entries a person may pick: their own, and for an admin the shared ones too.
    Names only; the server's internal names are left out."""
    import secrets_store
    own = f"{secrets_store._owner_prefix(sub)}/" if sub else None
    out = []
    for key in sorted(secrets_store._read_all()):
        if key.startswith("users/"):
            if not own or not key.startswith(own) or "/" in key[len(own):]:
                continue
            name, shared = key[len(own):], False
        elif admin:
            name, shared = key, True
        else:
            continue
        if not secrets_store.is_internal_env(name):
            out.append({"name": name, "shared": shared})
    return out


def key_path(rec: dict) -> str:
    ref = rec.get("key")
    if not isinstance(ref, dict) or not ref.get("name") or "/" in str(ref["name"]):
        return ""
    if ref.get("shared"):
        return str(ref["name"])
    if not rec.get("owner"):
        return ""
    import secrets_store
    return f"{secrets_store._owner_prefix(rec['owner'])}/{ref['name']}"


def resolve_key(rec: dict) -> str:
    """The runner's key, read server-side from exactly the entry it names — trimmed; ""
    when it is missing or holds spaces or control characters (it would break the
    request header and could end up in an error message)."""
    import secrets_store
    path = key_path(rec)
    key = str(secrets_store._read_all().get(path) or "").strip() if path else ""
    return key if key.isprintable() and not any(c.isspace() for c in key) else ""


def _picked_key(ref, owner: str, admin: bool, current):
    """(ref, problem) for a vault entry picked by name."""
    import secrets_store
    if not isinstance(ref, dict) or not str(ref.get("name") or "").strip():
        return None, "Pick a vault entry by name."
    ref = {"name": str(ref["name"]).strip(), "shared": bool(ref.get("shared"))}
    if current and current.get("key") == ref:
        return ref, ""                          # unchanged — e.g. the migrated RUNNER_LLM_KEY
    if "/" in ref["name"] or secrets_store.is_internal_env(ref["name"]):
        return None, f"'{ref['name']}' belongs to AICortex itself and can't be a runner's key."
    if ref["shared"] and not admin:
        return None, "Only an admin may use a shared vault entry — pick one of your own."
    probe = {"key": ref, "owner": owner}
    if key_path(probe) not in secrets_store._read_all():
        return None, f"There is no vault entry '{ref['name']}' you may use."
    if not resolve_key(probe):
        return None, f"The vault entry '{ref['name']}' holds spaces or line breaks — fix it in the vault first."
    return ref, ""


# ── checks ───────────────────────────────────────────────────────────────────
def _url_problem(url: str) -> str:
    import netguard
    if len(url) > URL_MAX:
        return "The address is too long."
    try:
        parts = urlsplit(url)
    except ValueError:
        return "That is not an address."
    if parts.scheme not in ("http", "https") or not parts.hostname:
        return "The address must start with http:// or https://."
    if parts.username or parts.password:
        return "The address may not contain a user name or password."
    if parts.query or parts.fragment:
        return "The address may not contain a query or fragment."
    ok, reason = netguard.check_url(url)
    return "" if ok else f"AICortex may not reach that address — {reason}"


def _trigger(value: str) -> str:
    value = (value or "").strip()
    if TRIGGER.fullmatch(value):
        return value
    found = re.search(r"/routines/(trig_[A-Za-z0-9_-]{4,80})/fire/?$", value)
    return found.group(1) if found else ""


def _fields(fields: dict, current, kind: str, cloud_ok: bool):
    """(values, problem) for the editable fields, current values filling the gaps."""
    cur = current or {}

    def pick(name):
        return str(fields[name] if name in fields and fields[name] is not None else cur.get(name, "")).strip()

    if kind == CLOUD and not cloud_ok:
        return None, "A Claude cloud runner needs sign-in with your own account (OIDC)."
    model, base_url, trigger = pick("model"), pick("base_url").rstrip("/"), ""
    if kind in MODEL_REQUIRED and not model:
        return None, "This kind of runner needs a model name."
    if len(model) > MODEL_MAX:
        return None, "The model name is too long."
    if kind == "compatible":
        problem = _url_problem(base_url)
        if problem:
            return None, problem
    else:
        base_url = ""
    if kind == CLOUD:
        trigger, model = _trigger(pick("trigger")), ""
        if not trigger:
            return None, "Paste the cloud task's API address or its trig_… id."
    name = (pick("name") or LABELS[kind])[:NAME_MAX]
    enabled = bool(fields["enabled"]) if "enabled" in fields else bool(cur.get("enabled", True))
    return {"name": name, "model": model, "base_url": base_url, "trigger": trigger,
            "enabled": enabled}, ""


def _rights(data: dict, current, kind: str, by: str, admin: bool) -> str:
    if current and current.get("owner") != by and not (admin and not current.get("owner")):
        return ("Only the runner's owner can change it — an admin may switch it off or delete it."
                if admin else f"There is no runner '{current['id']}' you may change.")
    if kind not in KINDS:
        return f"Unknown kind '{kind}' — choose one of {', '.join(KINDS)}."
    if current and kind != current.get("kind"):
        return "A runner's kind can't change — make a new runner instead."
    if kind == CHATGPT and not admin:
        return "Only an admin can set up the ChatGPT runner."
    if kind == "compatible" and not admin:
        return "Only an admin can point a runner at an own address."
    if not current and not admin and sum(r.get("owner") == by for r in data["runners"]) >= MAX_PER_PERSON:
        return f"You have {MAX_PER_PERSON} runners already — delete one first."
    if kind == CHATGPT and not current and any(r.get("kind") == CHATGPT for r in data["runners"]):
        return "There is one ChatGPT runner at most — AICortex signs in with one ChatGPT account."
    return ""


# ── changes ──────────────────────────────────────────────────────────────────
def save(fields: dict, *, by: str, admin: bool, key: str = "", cloud_ok: bool = True):
    """Create or change a runner → (ok, message, record). `key` is a key typed in;
    `fields["key_ref"]` picks a vault entry instead; neither keeps the current one."""
    rid = str(fields.get("id") or "").strip()
    with store.locked(RUNNERS_FILE):
        data = load()
        current = next((r for r in data["runners"] if r["id"] == rid), None)
        if rid and current is None:
            return False, f"There is no runner '{rid}' you may change.", None
        kind = str(fields.get("kind") or (current or {}).get("kind") or "").strip()
        problem = _rights(data, current, kind, by, admin)
        values, problem = (None, problem) if problem else _fields(fields, current, kind, cloud_ok)
        if problem:
            return False, problem, None
        owner = (current or {}).get("owner") or by            # an admin takes on an ownerless one
        rid = rid or _new_id(data["runners"])
        typed = (key or "").strip()
        if len(typed) > KEY_MAX:
            return False, "The key is too long.", None
        ref = (current or {}).get("key")
        if typed:
            ref = {"name": f"RUNNER_KEY_{rid}", "shared": not owner}
        elif fields.get("key_ref") is not None:
            ref, problem = _picked_key(fields["key_ref"], owner, admin, current)
            if problem:
                return False, problem, None
        if kind in KEY_REQUIRED and not ref:
            return False, "Enter the key once or pick one from the vault — nothing is stored yet.", None
        if ref and ref.get("shared") and not admin:
            return False, "Only an admin may use a shared vault entry — pick one of your own.", None
        if typed:
            import secrets_store
            msg = secrets_store.vault_set(ref["name"], typed, owner=owner,
                                          description=f"Runner '{values['name']}'", by=by)
            if not msg.startswith("Stored"):
                return False, msg, None
        now = _now()
        rec = dict(values, id=rid, kind=kind, owner=owner, key=ref,
                   note=(current or {}).get("note", "") if current and not typed else "",
                   created=(current or {}).get("created") or now, updated=now, updated_by=by)
        runners = ([rec if r["id"] == rid else r for r in data["runners"]] if current
                   else data["runners"] + [rec])
        standard = data["standard"]
        if not standard and not current and admin and kind in BUILTIN_KINDS:
            standard = rid
        personal = dict(data["personal"])
        if not current and owner not in personal:          # the first runner is their default
            personal[owner] = next(r["id"] for r in runners if r.get("owner") == owner)
        store.write_json(RUNNERS_FILE, {"version": 1, "standard": standard, "runners": runners,
                                        "personal": personal})
        old = (current or {}).get("key") or {}
        if old.get("name") == f"RUNNER_KEY_{rid}" and ref != old:     # its own key, now unused
            import secrets_store
            secrets_store.vault_delete(old["name"], owner="" if old.get("shared") else owner)
        return True, "Runner saved.", rec


def _change(rid: str, by: str, admin: bool, fn):
    with store.locked(RUNNERS_FILE):
        data = load()
        rec = next((r for r in data["runners"] if r["id"] == rid), None)
        if rec is None or not (admin or rec.get("owner") == by):
            return False, f"There is no runner '{rid}' you may change."
        return fn(data, rec)


def set_enabled(rid: str, enabled: bool, *, by: str, admin: bool):
    def apply(data, rec):
        if enabled and rec.get("owner") not in ("", by):
            return False, "Only the runner's owner can switch it on — an admin may switch it off."
        runners = [dict(r, enabled=bool(enabled), updated=_now(), updated_by=by)
                   if r["id"] == rid else r for r in data["runners"]]
        store.write_json(RUNNERS_FILE, dict(data, runners=runners))
        return True, "Runner switched on." if enabled else "Runner switched off."
    return _change(rid, by, admin, apply)


def set_standard(rid: str, *, by: str, admin: bool):
    def apply(data, rec):
        if not admin or rec.get("owner") != by or rec.get("kind") not in BUILTIN_KINDS:
            return False, ("The standard runner is one of an admin's own runners that AICortex "
                           "runs itself — not a cloud task.")
        store.write_json(RUNNERS_FILE, dict(data, standard=rid))
        return True, "This is now the standard runner."
    return _change(rid, by, admin, apply)


def set_personal(rid: str, *, by: str):
    """Make one of a person's own runners their default for jobs that name none."""
    def apply(data, rec):
        if rec.get("owner") != by:
            return False, f"There is no runner '{rid}' you may change."
        store.write_json(RUNNERS_FILE, dict(data, personal=dict(data["personal"], **{by: rid})))
        return True, "This is now your default runner."
    return _change(rid, by, False, apply)


def delete(rid: str, *, by: str, admin: bool):
    def apply(data, rec):
        if rid == data["standard"]:
            return False, "The standard runner can't be deleted — make another one the standard first."
        personal = {k: v for k, v in data["personal"].items() if v != rid}
        store.write_json(RUNNERS_FILE, dict(data, personal=personal,
                                            runners=[r for r in data["runners"] if r["id"] != rid]))
        ref = rec.get("key") or {}
        if ref.get("name") == f"RUNNER_KEY_{rid}":                 # its own key goes with it
            import secrets_store
            secrets_store.vault_delete(ref["name"], owner="" if ref.get("shared") else rec.get("owner", ""))
        return True, "Runner deleted."
    return _change(rid, by, admin, apply)


# ── the tool ─────────────────────────────────────────────────────────────────
def _caller():
    try:
        import authz
        ident, role, _ = authz.real_caller()
        return ident, role
    except Exception:
        return "unknown", "user"


def register(mcp):
    @mcp.tool
    def runner_list() -> str:
        """List the runners you may give a scheduled job to (id — name — kind — on/off).
        Assign one with cron_add(runner=<id>); without one a job goes to its person's
        own default runner (marked "your default"), else to the standard runner.
        Runners are set up in the WebUI (tab "Runner")."""
        ident, role = _caller()
        admin = role == "admin"
        data = load()
        lines = []
        mine = personal_default(data, ident)
        for r in visible(data, ident, admin):
            tags = (" — standard" if r["id"] == data["standard"] else "") + (
                " — your default" if r["id"] == mine else "") + (
                f" — owner {r.get('owner') or '?'}" if admin else "")
            lines.append(f"- {r['id']} — {r.get('name', '')} — {r.get('kind', '')} — "
                         f"{'on' if r.get('enabled') else 'off'}{tags}")
        if admin and external_available():
            lines.append(f"- {EXTERNAL} — an own program that fetches its jobs (RUNNER_TOKEN)")
        return "\n".join(lines) if lines else "No runners yet — set one up in the WebUI (tab \"Runner\")."
