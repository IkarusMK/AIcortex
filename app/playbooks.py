"""Playbooks — multi-stage work where a person approves each hand-off.

A playbook (``PLAYBOOKS_DIR/<name>/PLAYBOOK.md``) is a shared, reusable sequence of
stages. Each stage has a goal, the artifact it produces (e.g. ``intent.md``), a
template for it and the approval it needs: ``webui`` (a person in the WebUI — the
default), ``chat`` (the AI may record an approval the person gave in the
conversation; marked as such) or ``none``. Optional ``plays`` list building blocks
and what they require. The text below the header is the guidance.

A run (``PLAYBOOKS_DIR/runs/<id>.json``) applies a playbook to a project. It keeps
every submitted version of each stage's artifact — text and/or a reference to a
repo file, commit or URL — and every decision. A run belongs to the person who
started it; admins see all. An AI can never approve its own work: chat approvals
come only from an interactive person, never an agent key, the runner or a
scheduled job.

The bundled template follows Anthropic's "The AI-native SDLC playbook"
(claude.com, 21 Aug 2026), written in our own words.
"""
import os
import re
import uuid
from datetime import datetime, timezone
from pathlib import Path

import yaml

import store

PLAYBOOKS_DIR = Path(os.environ.get("PLAYBOOKS_DIR", "/data/playbooks"))
RUNS_DIR = PLAYBOOKS_DIR / "runs"
TEMPLATES_DIR = Path(__file__).parent / "playbook_templates"
APPROVALS = ("webui", "chat", "none")
MAX_ARTIFACT = 100_000
MAX_VERSIONS = 20
MAX_STAGES = 20
_ID = re.compile(r"^[a-f0-9]{8,32}$")


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _slug(text: str) -> str:
    return re.sub(r"[^a-z0-9]+", "-", (text or "").lower()).strip("-")[:60]


def _one(value, limit: int = 300) -> str:
    return " ".join(str(value or "").split())[:limit]


# ── who is calling ───────────────────────────────────────────────────────────
def _caller() -> tuple[str, str]:
    """(person, role) of the current call — fails closed to an unknown user."""
    try:
        import authz
        return authz.effective_identity()
    except Exception:
        return "unknown", "user"


def _label() -> str:
    try:
        import authz
        return authz.caller_label()
    except Exception:
        return _caller()[0]


def _sees(person: str) -> bool:
    try:
        import authz
        return authz.sees(person)
    except Exception:
        return False


def _interactive_person() -> bool:
    """True only for a person in an interactive app (OAuth session) — never a REST or
    agent key, the runner, or a scheduled job acting for its owner."""
    try:
        import actas
        import authz
        if authz._REST_IDENTITY.get():
            return False
        ident, is_runner, claims = authz._identity()
        if is_runner or claims.get("aicortex_key") or ident in ("", "unknown"):
            return False
        return not actas.current(ident)
    except Exception:
        return False


# ── definitions ─────────────────────────────────────────────────────────────
def _clean_stages(stages) -> tuple[list, str]:
    """(stages, problem) — normalized stage dicts, or why they can't be used."""
    if isinstance(stages, str):
        try:
            stages = yaml.safe_load(stages)
        except yaml.YAMLError:
            return [], "stages must be a list (JSON or YAML)"
    if not isinstance(stages, list) or not stages:
        return [], "a playbook needs at least one stage"
    if len(stages) > MAX_STAGES:
        return [], f"at most {MAX_STAGES} stages"
    out, seen = [], set()
    for i, s in enumerate(stages, 1):
        if not isinstance(s, dict):
            return [], f"stage {i} is not an object"
        sid = _slug(s.get("id") or s.get("name") or f"stage-{i}")
        approval = str(s.get("approval") or "webui").strip().lower()
        if approval not in APPROVALS:
            return [], f"stage {i}: approval must be one of {', '.join(APPROVALS)}"
        if not sid or sid in seen:
            return [], f"stage {i}: id '{sid}' is missing or used twice"
        seen.add(sid)
        out.append({"id": sid, "name": _one(s.get("name") or sid, 80),
                    "goal": _one(s.get("goal"), 600), "artifact": _one(s.get("artifact"), 120),
                    "template": str(s.get("template") or "")[:20_000],
                    "approval": approval, "approver": _one(s.get("approver"), 120)})
    return out, ""


def _clean_plays(plays, stage_ids: set) -> tuple[list, str]:
    if isinstance(plays, str):
        try:
            plays = yaml.safe_load(plays) if plays.strip() else []
        except yaml.YAMLError:
            return [], "plays must be a list (JSON or YAML)"
    out, ids = [], set()
    for p in plays or []:
        if not isinstance(p, dict) or not p.get("id"):
            return [], "every play needs an id"
        pid = _slug(p["id"])
        ids.add(pid)
        out.append({"id": pid, "stage": _slug(p.get("stage", "")), "title": _one(p.get("title"), 120),
                    "summary": _one(p.get("summary"), 300),
                    "requires": [_slug(r) for r in p.get("requires") or []]})
    for p in out:
        if p["stage"] and p["stage"] not in stage_ids:
            return [], f"play '{p['id']}' names an unknown stage '{p['stage']}'"
        if not set(p["requires"]) <= ids:
            return [], f"play '{p['id']}' requires an unknown play"
    return out, ""


def _folder(name: str) -> Path:
    return PLAYBOOKS_DIR / _slug(name)


def _read_def(folder: Path) -> dict | None:
    f = folder / "PLAYBOOK.md"
    if not f.is_file():
        return None
    split = store.split_frontmatter(f.read_text(encoding="utf-8"))
    if not split:
        return None
    try:
        meta = yaml.safe_load(split[0]) or {}
    except yaml.YAMLError:
        return None
    if not isinstance(meta, dict):
        return None
    stages, _ = _clean_stages(meta.get("stages"))
    plays, _ = _clean_plays(meta.get("plays"), {s["id"] for s in stages})
    return {"name": folder.name, "title": str(meta.get("title") or folder.name),
            "description": str(meta.get("description") or ""),
            "category": str(meta.get("category") or "uncategorized"),
            "source": str(meta.get("source") or ""), "author": str(meta.get("author") or ""),
            "stages": stages, "plays": plays, "guidance": split[1].strip()}


def load(name: str) -> dict | None:
    return _read_def(_folder(name)) if _slug(name) and _slug(name) != "runs" else None


def listing() -> list[dict]:
    if not PLAYBOOKS_DIR.is_dir():
        return []
    out = []
    for folder in sorted(PLAYBOOKS_DIR.iterdir()):
        d = _read_def(folder) if folder.is_dir() and folder.name != "runs" else None
        if d:
            out.append({k: d[k] for k in ("name", "title", "description", "category", "author")}
                       | {"stages": len(d["stages"])})
    return out


def _may_change(name: str) -> str:
    """'' if the caller may overwrite/delete playbook `name`, else why not. Shared
    playbooks belong to everyone: only an admin or the author changes one."""
    existing = load(name)
    if not existing:
        return ""
    try:
        import authz
        if not authz.enabled():
            return ""
    except Exception:
        return "Refused: permissions could not be checked."
    person, role = _caller()
    if role == "admin" or existing["author"].split("/")[0] == person:
        return ""
    return (f"Refused: '{existing['name']}' is a shared playbook. Ask an admin to change it, "
            f"or save your variant under another name.")


def save(name, title, description, stages, category="", plays=None, guidance="", source="") -> str:
    slug = _slug(name)
    if not slug or slug == "runs":
        return "Refused: give the playbook a name (letters or digits; 'runs' is reserved)."
    clean, problem = _clean_stages(stages)
    if problem:
        return f"Refused: {problem}."
    clean_plays, problem = _clean_plays(plays, {s["id"] for s in clean})
    if problem:
        return f"Refused: {problem}."
    denied = _may_change(slug)
    if denied:
        return denied
    head = {"title": _one(title or name, 120), "description": _one(description, 600),
            "category": _one(category or "uncategorized", 60), "source": _one(source, 300),
            "author": _label(), "stages": clean, "plays": clean_plays}
    body = yaml.safe_dump(head, sort_keys=False, allow_unicode=True, width=100000)
    store.write_text_atomic(_folder(slug) / "PLAYBOOK.md",
                            f"---\n{body}---\n\n{str(guidance or '').strip()}\n")
    return f"Saved playbook '{slug}' ({len(clean)} stages)."


def delete(name: str) -> str:
    if not load(name):
        return f"No playbook '{name}'."
    denied = _may_change(name)
    if denied:
        return denied
    (_folder(name) / "PLAYBOOK.md").unlink()
    try:
        _folder(name).rmdir()
    except OSError:
        pass
    return f"Deleted playbook '{_slug(name)}'."


def install_example(name: str = "ai-native-sdlc") -> str:
    """Copy a bundled template into the library (overwrites only with change rights)."""
    src = TEMPLATES_DIR / _slug(name) / "PLAYBOOK.md"
    if not src.is_file():
        return f"No bundled template '{name}'."
    d = _read_def(src.parent)
    return save(d["name"], d["title"], d["description"], d["stages"], d["category"],
                d["plays"], d["guidance"], d["source"])


# ── runs ─────────────────────────────────────────────────────────────────────
def _run_path(run_id: str) -> Path:
    return RUNS_DIR / f"{run_id}.json"


def get_run(run_id: str) -> dict | None:
    if not _ID.match(run_id or ""):
        return None
    try:
        return store.read_json(_run_path(run_id), None)
    except store.StoreCorrupt:
        return None


def _visible_run(run_id: str) -> dict | None:
    run = get_run(run_id)
    return run if run and _sees(run.get("person", "")) else None


def runs_for(person: str = "", project: str = "", status: str = "active") -> list[dict]:
    """Runs the caller may see (optionally one person's), newest first."""
    if not RUNS_DIR.is_dir():
        return []
    out = []
    for f in RUNS_DIR.glob("*.json"):
        run = get_run(f.stem)
        if not run or not _sees(run.get("person", "")):
            continue
        if person and run.get("person") != person:
            continue
        if project and _slug(run.get("project", "")) != _slug(project):
            continue
        if status != "all" and run.get("status") != status:
            continue
        out.append(run)
    return sorted(out, key=lambda r: r.get("created", ""), reverse=True)


def start(playbook: str, project: str, title: str = "") -> tuple[dict | None, str]:
    d = load(playbook)
    if not d:
        return None, f"No playbook '{playbook}'. playbook_list shows the library."
    person, _ = _caller()
    run = {"id": uuid.uuid4().hex[:12], "playbook": d["name"], "playbook_title": d["title"],
           "title": _one(title or d["title"], 120), "project": _one(project, 80),
           "person": "" if person == "unknown" else person, "created_by": _label(),
           "created": _now(), "updated": _now(), "status": "active", "current": 0,
           "stages": [{"id": s["id"], "name": s["name"], "goal": s["goal"],
                       "artifact": s["artifact"], "template": s["template"],
                       "approval": s["approval"], "approver": s["approver"],
                       "status": "pending", "versions": [], "decision": None}
                      for s in d["stages"]]}
    store.write_json(_run_path(run["id"]), run)
    return run, ""


def _update(run_id: str, change) -> tuple[dict | None, str]:
    """Locked read-modify-write of a visible run; `change(run)` returns '' or a refusal."""
    path = _run_path(run_id) if _ID.match(run_id or "") else None
    if not path:
        return None, f"No run '{run_id}'."
    with store.locked(path):
        run = _visible_run(run_id)
        if not run:
            return None, f"No run '{run_id}'."
        problem = change(run)
        if problem:
            return run, problem
        run["updated"] = _now()
        store.write_json(path, run)
    return run, ""


def _advance(run: dict) -> None:
    if run["current"] + 1 >= len(run["stages"]):
        run["status"] = "done"
    else:
        run["current"] += 1


def submit(run_id, artifact="", ref_path="", ref_commit="", ref_url="", note="") -> tuple[dict | None, str]:
    if len(artifact or "") > MAX_ARTIFACT:
        return None, f"Refused: the artifact is too long (max {MAX_ARTIFACT:,} characters) — link it instead."
    if not (artifact or ref_path or ref_url):
        return None, "Refused: submit the artifact text or a reference (ref_path / ref_url)."

    def change(run):
        if run["status"] != "active":
            return f"Refused: the run is {run['status']}."
        stage = run["stages"][run["current"]]
        if stage["status"] == "approved":
            return "Refused: this stage is already approved."
        stage["versions"] = (stage["versions"] + [{
            "n": (stage["versions"][-1]["n"] + 1) if stage["versions"] else 1,
            "text": artifact or "", "note": _one(note, 500), "by": _label(), "at": _now(),
            "ref": {"path": _one(ref_path, 300), "commit": _one(ref_commit, 64),
                    "url": _one(ref_url, 500)}}])[-MAX_VERSIONS:]
        stage["status"] = "submitted"
        if stage["approval"] == "none":
            stage["status"] = "approved"
            stage["decision"] = {"decision": "approve", "by": "", "via": "none", "at": _now(), "note": ""}
            _advance(run)
        return ""
    return _update(run_id, change)


def decide(run_id, decision, by="", via="webui", note="") -> tuple[dict | None, str]:
    """Approve or reject the submitted artifact of the current stage. `via` is how
    the person decided: "webui" (only the WebUI passes it) or "chat"."""
    if decision not in ("approve", "reject"):
        return None, "Refused: decision must be 'approve' or 'reject'."

    def change(run):
        if run["status"] != "active":
            return f"Refused: the run is {run['status']}."
        stage = run["stages"][run["current"]]
        if stage["status"] != "submitted":
            return "Refused: nothing is waiting for a decision in this stage."
        if via == "chat" and stage["approval"] != "chat":
            return (f"Refused: '{stage['name']}' is approved by a person in the AICortex WebUI "
                    f"(Playbooks), not in the chat.")
        stage["decision"] = {"decision": decision, "by": by, "via": via, "at": _now(),
                             "note": _one(note, 1000)}
        if decision == "approve":
            stage["status"] = "approved"
            _advance(run)
        else:
            stage["status"] = "rejected"
        return ""
    return _update(run_id, change)


def cancel(run_id: str) -> tuple[dict | None, str]:
    def change(run):
        if run["status"] != "active":
            return f"Refused: the run is {run['status']}."
        run["status"] = "cancelled"
        return ""
    return _update(run_id, change)


def next_step(run: dict) -> str:
    """One line: what has to happen next in this run."""
    if run["status"] != "active":
        return run["status"]
    s = run["stages"][run["current"]]
    stage = f"stage {run['current'] + 1}/{len(run['stages'])} {s['name']}"
    if s["status"] == "submitted":
        where = "in the chat or the WebUI" if s["approval"] == "chat" else "in the WebUI"
        return f"{stage}: {s['artifact'] or 'result'} waits for approval {where}"
    if s["status"] == "rejected":
        note = (s.get("decision") or {}).get("note", "")
        return f"{stage}: rejected{' — ' + note if note else ''}; rework and playbook_submit again"
    return f"{stage}: write {s['artifact'] or 'the result'} and playbook_submit it"


def describe(run: dict, full: bool = True) -> str:
    lines = [f"Run {run['id']} — {run['title']} ({run['playbook']}, project {run['project'] or '–'})",
             f"Next: {next_step(run)}"]
    if run["status"] == "active" and full:
        s = run["stages"][run["current"]]
        lines += [f"Goal: {s['goal']}" if s["goal"] else "",
                  f"Approver: {s['approver']}" if s["approver"] else ""]
        if s["template"] and s["status"] in ("pending", "rejected"):
            lines += [f"Template for {s['artifact'] or 'the result'}:", s["template"].rstrip()]
    done = [f"  - {x['name']}: v{x['versions'][-1]['n']} {x['status']}"
            for x in run["stages"] if x["versions"]]
    if done:
        lines += ["Artifacts so far:"] + done
    return "\n".join(ln for ln in lines if ln)


# ── tools ────────────────────────────────────────────────────────────────────
def register(mcp):
    @mcp.tool
    def playbook_list(category: str = "") -> str:
        """List the playbook library: multi-stage workflows (e.g. an AI-native software
        change: Plan → Design → Build → Test → Deploy → Maintain) where a person
        approves each stage's artifact. Start one with playbook_start."""
        rows = [p for p in listing() if not category or p["category"].lower() == category.lower()]
        if not rows:
            return "No playbooks yet. An admin can add the AI-native SDLC template in the WebUI."
        return "\n".join(f"- {p['name']} — {p['title']} ({p['stages']} stages, {p['category']}): "
                         f"{p['description']}" for p in rows)

    @mcp.tool
    def playbook_search(query: str) -> str:
        """Find playbooks whose title, description, stages or guidance match the words
        of `query` (best first)."""
        words = [w for w in re.split(r"\W+", (query or "").lower()) if len(w) > 2]
        scored = []
        for p in listing():
            d = load(p["name"])
            head = f"{d['name']} {d['title']} {d['description']}".lower()
            rest = " ".join([s["name"] + " " + s["goal"] + " " + s["artifact"] for s in d["stages"]]
                            + [d["guidance"]]).lower()
            score = sum(3 * (w in head) + (w in rest) for w in words)
            if score:
                scored.append((-score, d["name"], d["title"], d["description"]))
        return "\n".join(f"- {n} — {t}: {desc}" for _, n, t, desc in sorted(scored)[:10]) \
            or f"No playbook matches '{query}'."

    @mcp.tool
    def playbook_load(name: str) -> str:
        """A playbook in full: every stage with goal, artifact, template and approval,
        the plays and the guidance."""
        d = load(name)
        if not d:
            return f"No playbook '{name}'."
        out = [f"# {d['title']} ({d['name']})", d["description"],
               f"Source: {d['source']}" if d["source"] else ""]
        for i, s in enumerate(d["stages"], 1):
            out += [f"\n## Stage {i}: {s['name']} → {s['artifact'] or '(no artifact)'}",
                    f"Goal: {s['goal']}", f"Approval: {s['approval']}"
                    + (f" by {s['approver']}" if s["approver"] else "")]
            if s["template"]:
                out += ["Template:", s["template"].rstrip()]
        if d["plays"]:
            out.append("\n## Plays")
            out += [f"- {p['id']} ({p['stage']}): {p['title']}"
                    + (f" — needs {', '.join(p['requires'])}" if p["requires"] else " — no prerequisites")
                    for p in d["plays"]]
        if d["guidance"]:
            out += ["\n## Guidance", d["guidance"]]
        return "\n".join(x for x in out if x)

    @mcp.tool
    def playbook_write(name: str, title: str, description: str, stages: list[dict] | str,
                       category: str = "", plays: list[dict] | str = "", guidance: str = "",
                       source: str = "") -> str:
        """Create or update a playbook. stages = list of {id, name, goal, artifact,
        template, approval: webui|chat|none, approver} in order; plays (optional) =
        list of {id, stage, title, requires: [ids]}. A shared playbook is changed only
        by an admin or its author."""
        return save(name, title, description, stages, category, plays or None, guidance, source)

    @mcp.tool
    def playbook_delete(name: str) -> str:
        """Delete a playbook from the library (admin or its author). Runs keep working."""
        return delete(name)

    @mcp.tool
    def playbook_start(playbook: str, project: str, title: str = "") -> str:
        """Start a run of `playbook` for `project`. Returns the first stage: its goal,
        the artifact to write and its template."""
        run, problem = start(playbook, project, title)
        return problem or "Started.\n" + describe(run)

    @mcp.tool
    def playbook_status(run_id: str = "", project: str = "") -> str:
        """Where a run stands and what to do next — one run by id, or the active runs
        of a project (or all of yours)."""
        if run_id:
            run = _visible_run(run_id)
            return describe(run) if run else f"No run '{run_id}'."
        runs = runs_for(project=project)
        return "\n\n".join(describe(r, full=False) for r in runs) or "No active playbook runs."

    @mcp.tool
    def playbook_runs(project: str = "", status: str = "active") -> str:
        """List playbook runs (status active, done, cancelled or all)."""
        runs = runs_for(project=project, status=status)
        return "\n".join(f"- {r['id']} — {r['title']} ({r['playbook']}, {r['project'] or '–'}): "
                         f"{next_step(r)}" for r in runs) or "No matching runs."

    @mcp.tool
    def playbook_submit(run_id: str, artifact: str = "", ref_path: str = "", ref_commit: str = "",
                        ref_url: str = "", note: str = "") -> str:
        """Submit the current stage's artifact (text, and/or a reference to the repo
        file and commit or a URL). A stage with approval 'none' moves on; otherwise it
        waits for the person's decision."""
        run, problem = submit(run_id, artifact, ref_path, ref_commit, ref_url, note)
        return problem or "Submitted.\n" + describe(run, full=False)

    @mcp.tool
    def playbook_record_approval(run_id: str, decision: str, note: str = "") -> str:
        """Record the person's own decision given in this conversation — only for a
        stage whose approval is 'chat'. Never decide on the person's behalf."""
        if not _interactive_person():
            return ("Refused: only the person themselves, in an interactive app, can approve; "
                    "agents and scheduled jobs cannot. Use the AICortex WebUI.")
        run, problem = decide(run_id, decision, by=_label(), via="chat", note=note)
        return problem or "Recorded.\n" + describe(run, full=False)

    @mcp.tool
    def playbook_cancel(run_id: str) -> str:
        """Cancel a run (its history is kept)."""
        run, problem = cancel(run_id)
        return problem or f"Cancelled run {run_id}."
