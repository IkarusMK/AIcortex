"""Inbox forwarding — a message for a person also reaches them.

The inbox is a file on the server; nobody looks at it. A forwarding RULE sends every
new message for one recipient (usually "user") through a REGISTERED service as a JSON
POST, so a cron job's report or a webhook event arrives as a push on the phone. The
rule is data, not code: which service, which path, and a JSON body template —
so any push service with an HTTP API works (ntfy, Gotify, a Home Assistant webhook …).

Placeholders in the template's strings: {title} {subject} {body} {from} {to} {id} {ts}.
Values are filled in literally (a message can't inject another placeholder), {body}
is cut to MAX_BODY characters, {title} falls back to the first line of the body and,
for a message that is not the rule owner's own (a webhook event), starts with the
sender in brackets.

Safety:
- Rules are admin-only (authz.ADMIN_TOOLS), and a rule written over MCP/REST is only
  PENDING: it sends nothing until the person approves it in the WebUI (Inbox tab).
  A prompt-injected admin session can't open a standing channel out on its own —
  the same rule as for secret host bindings (only a person makes the first one).
  The approval covers the TARGET too (the service's address, TLS and token settings):
  re-pointing the service afterwards revokes it. Changing where or what a rule sends
  needs the approval again; on/off does not. The owner must be an admin in AICortex's
  own policy (operators/roles), checked at set time and at every delivery.
- The call goes through services.post_json (network policy, the token's host
  binding, TLS settings). Delivery runs as the rule's owner — re-checked as an admin
  at every delivery — but marked as not-a-person, so it can never add a host to a
  secret the way the WebUI can.
- A message of another person is not forwarded to the rule owner.
- At most RATE_MAX deliveries per rule and minute, at most MAX_PENDING waiting ones,
  WORKERS threads; what doesn't fit is not sent, noted once per window.
- Forwarding stays off if the rules file could be written with the file tools
  (COORD_DIR inside WORK_DIR).
- A failing delivery never breaks posting; the log keeps the status code only, never
  the remote answer (it could echo a header).
"""
import hashlib
import json
import logging
import os
import threading
import time
from collections import deque
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
from pathlib import Path
from typing import Annotated
from urllib.parse import urlparse

from pydantic import Field

import coordination
import store

log = logging.getLogger("aicortex.inbox_forward")

RULES_FILE = coordination.COORD_DIR / "forward.json"
LOG_FILE = coordination.COORD_DIR / "forward_log.json"
LOCK = coordination.COORD_DIR / "forward"
WORK_DIR = Path(os.environ.get("WORK_DIR", "/data/work")).resolve()   # fs_tools' sandbox
TARGET_KEYS = ("base_url", "tls_insecure", "ca_bundle", "token_env", "auth_header",
               "auth_scheme", "username")
ASYNC = True              # tests switch delivery to inline
WORKERS = 2               # delivery threads
MAX_PENDING = 50          # deliveries waiting for a thread; more are dropped
RATE_MAX = 30             # deliveries per rule …
RATE_WINDOW = 60          # … within this many seconds
MAX_BODY = 3500           # ntfy turns longer messages into attachments (4096 bytes)
MAX_TITLE = 120
MAX_TEMPLATE = 4000       # characters of the JSON body template
MAX_RECIPIENT = 64
LOG_KEEP = 50
TEST_MESSAGE = {"id": "test", "from": "aicortex", "subject": "AICortex – test",
                "body": "Test message from AICortex: inbox forwarding works."}

_pool = None
_pending = 0
_pending_cv = threading.Condition()
_sent: dict = {}          # (recipient, owner) → deque of delivery times
_noted: dict = {}         # (recipient, owner, kind) → when the next "not sent" note may go
_rate_lock = threading.Lock()


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def rules() -> list:
    """All rules; raises store.StoreCorrupt when the file is unreadable."""
    return store.read_json(RULES_FILE, [])


def recent() -> list:
    try:
        return store.read_json(LOG_FILE, [])
    except store.StoreCorrupt:
        return []


def _note(msg_id: str, rule: dict, ok: bool, info: str) -> None:
    entry = {"ts": _now(), "id": msg_id, "recipient": rule.get("recipient", ""),
             "service": rule.get("service", ""), "owner": rule.get("owner", ""),
             "ok": bool(ok), "info": (info or "")[:200]}
    try:
        with store.locked(LOCK):
            store.write_json(LOG_FILE, (recent() + [entry])[-LOG_KEEP:])
    except Exception as exc:          # the log is a convenience, never a failure
        log.warning("inbox forward: could not write the delivery log (%s)", exc.__class__.__name__)


def host_of(rule: dict) -> str:
    """Where the rule sends to — shown to the person who approves it."""
    import services
    cfg = services._load(rule.get("service", "")) or {}
    return urlparse(cfg.get("base_url", "")).hostname or ""


def target(rule: dict) -> str:
    """Fingerprint of where the rule sends to: the service's address, TLS and token
    settings. An approval holds only while it stays the same."""
    import services
    cfg = services._load(rule.get("service", "")) or {}
    seen = json.dumps({k: cfg.get(k) for k in TARGET_KEYS}, sort_keys=True, default=str)
    return hashlib.sha256(seen.encode()).hexdigest()


# ── filling the template ─────────────────────────────────────────────────────
def _cut(text: str, limit: int) -> str:
    text = text or ""
    return text if len(text) <= limit else text[:limit].rstrip() + "…"


def _title(msg: dict, foreign: bool) -> str:
    subject = (msg.get("subject") or "").strip()
    first = next((ln.strip() for ln in (msg.get("body") or "").splitlines() if ln.strip()), "")
    title = _cut(subject or first, MAX_TITLE) or "AICortex"
    return f"[external · {_cut(msg.get('from') or '?', 40)}] {title}" if foreign else title


def values(msg: dict, owner: str = "") -> dict:
    """The placeholders' values. A message that isn't the owner's own (a webhook event,
    an unowned post) names its sender in the title, so it can't pose as AICortex."""
    foreign = msg.get("person", owner) != owner
    return {"title": _title(msg, foreign), "subject": msg.get("subject") or "",
            "body": _cut(msg.get("body") or "", MAX_BODY), "from": msg.get("from") or "",
            "to": msg.get("to") or "", "id": msg.get("id") or "", "ts": msg.get("ts") or ""}


def _fill_text(text: str, vals: dict) -> str:
    """Replace each {field} in ONE pass, left to right — a value that contains
    "{body}" stays as it is instead of being filled again."""
    out, i = [], 0
    while i < len(text):
        if text[i] == "{":
            end = text.find("}", i)
            name = text[i + 1:end] if end > 0 else ""
            if name in vals:
                out.append(vals[name])
                i = end + 1
                continue
        out.append(text[i])
        i += 1
    return "".join(out)


def fill(template, vals: dict):
    if isinstance(template, str):
        return _fill_text(template, vals)
    if isinstance(template, list):
        return [fill(x, vals) for x in template]
    if isinstance(template, dict):
        return {k: fill(v, vals) for k, v in template.items()}
    return template


# ── delivering ───────────────────────────────────────────────────────────────
def _owner_role(owner: str) -> str:
    """The owner's role NOW (a demotion takes effect at the next delivery)."""
    import authz
    if not authz.enabled():
        return "admin"
    return authz.session_role(owner) if owner else ""


def deliver(rule: dict, msg: dict) -> tuple:
    """Send one message through one rule → (ok, info). Runs as the rule's owner so
    the owner's vault entry is found — marked as a key call, never as the WebUI."""
    import authz
    import services
    owner = rule.get("owner", "")
    role = _owner_role(owner)
    if role != "admin":
        return False, "Refused: the rule's owner is no longer an admin."
    try:
        with authz.rest_identity(owner, role, via="key"):
            return services.post_json(rule["service"], rule.get("path", "/"),
                                      fill(rule.get("body", {}), values(msg, owner)))
    except Exception as exc:
        import errtext
        return False, f"Delivery failed: {errtext.safe(exc)}"


def _for_owner(rule: dict, msg: dict) -> bool:
    """A message of another person is not the rule owner's to receive."""
    import authz
    person = msg.get("person", "")
    return not authz.enabled() or not person or person == rule.get("owner", "")


def _within_rate(rule: dict) -> bool:
    key, now = (rule.get("recipient"), rule.get("owner")), time.monotonic()
    with _rate_lock:
        times = _sent.setdefault(key, deque())
        while times and now - times[0] > RATE_WINDOW:
            times.popleft()
        if len(times) >= RATE_MAX:
            return False
        times.append(now)
        return True


def _note_once(kind: str, msg: dict, rule: dict, info: str) -> None:
    """A "not sent" note at most once per rule, kind and window — a flood must not
    rewrite the log for every message or push real failures out of it."""
    key, now = (rule.get("recipient"), rule.get("owner"), kind), time.monotonic()
    with _rate_lock:
        if now < _noted.get(key, 0):
            return
        _noted[key] = now + RATE_WINDOW
    log.warning("inbox forward to '%s': %s", rule.get("service"), info)
    _note(msg.get("id", ""), rule, False, info)


def _exposed() -> bool:
    """Could the rules file be written with the file tools (fs_write)?"""
    coord = Path(coordination.COORD_DIR).resolve()
    return coord == WORK_DIR or WORK_DIR in coord.parents


def _revoke(rule: dict) -> None:
    try:
        with store.locked(LOCK):
            current = rules()
            for r in current:
                if r.get("recipient") == rule.get("recipient") and r.get("owner") == rule.get("owner"):
                    r["approved"] = False
            store.write_json(RULES_FILE, current)
    except Exception as exc:
        log.warning("inbox forward: could not revoke a rule (%s)", exc.__class__.__name__)


def _run(rule: dict, msg: dict) -> None:
    global _pending
    try:
        ok, info = deliver(rule, msg)
        if not ok:
            log.warning("inbox forward to '%s' failed: %s", rule.get("service"), info)
        _note(msg.get("id", ""), rule, ok, info)
    finally:
        if ASYNC:
            with _pending_cv:
                _pending -= 1
                _pending_cv.notify_all()


def _submit(rule: dict, msg: dict) -> None:
    global _pool, _pending
    with _pending_cv:
        if _pending >= MAX_PENDING:
            full = True
        else:
            full, _pending = False, _pending + 1
            if _pool is None:
                _pool = ThreadPoolExecutor(max_workers=WORKERS, thread_name_prefix="inbox-forward")
    if full:
        _note_once("queue", msg, rule, "Dropped: delivery queue full.")
        return
    try:
        _pool.submit(_run, rule, dict(msg))
    except Exception as exc:          # e.g. the interpreter is shutting down
        with _pending_cv:
            _pending -= 1
            _pending_cv.notify_all()
        log.warning("inbox forward: could not queue a delivery (%s)", exc.__class__.__name__)


def drain(timeout: float = 10) -> bool:
    """Wait until no delivery is waiting or running (tests, shutdown)."""
    deadline = time.monotonic() + timeout
    with _pending_cv:
        while _pending:
            left = deadline - time.monotonic()
            if left <= 0:
                return False
            _pending_cv.wait(left)
    return True


def dispatch(msg: dict) -> None:
    """Forward a just-stored message through every matching rule. Never raises."""
    if _exposed():
        log.warning("inbox forward is off: COORD_DIR lies inside WORK_DIR (file tools could write the rules)")
        return
    try:
        matching = [r for r in rules() if r.get("enabled", True) and r.get("approved")
                    and r.get("recipient") == msg.get("to") and _for_owner(r, msg)]
    except store.StoreCorrupt as exc:
        log.warning("inbox forward: rules unreadable, nothing sent (%s)", exc)
        return
    except Exception as exc:
        log.warning("inbox forward: %s", exc.__class__.__name__)
        return
    for rule in matching:
        if rule.get("target") != target(rule):
            _revoke(rule)
            _note(msg.get("id", ""), rule, False,
                  "Not sent: the service changed since the approval — approve it again in the WebUI.")
        elif not _within_rate(rule):
            _note_once("rate", msg, rule, f"Suppressed: rate limit ({RATE_MAX} per {RATE_WINDOW} s); "
                                          f"further messages in this minute are not sent.")
        elif ASYNC:
            _submit(rule, msg)
        else:
            _run(rule, msg)


# ── rules ────────────────────────────────────────────────────────────────────
def _caller() -> tuple:
    import authz
    ident, role = authz.effective_identity()
    return ("" if ident == "unknown" else ident), (role or "user")


def _problem(recipient: str, service: str, path: str, body, owner: str) -> str:
    import authz
    import services
    if authz.enabled() and not owner:
        return "Refused: AICortex can't tell who you are, so the rule would have no owner."
    if _owner_role(owner) != "admin":
        return ("Refused: forwarding needs an owner who is an admin in AICortex itself "
                "(operators or roles in its policy), not only through the sign-in provider.")
    if not recipient or len(recipient) > MAX_RECIPIENT or not recipient.isprintable():
        return f"Refused: recipient must be 1–{MAX_RECIPIENT} printable characters."
    if not services._load(service):
        return f"Refused: Unknown service '{service}'. Register it with service_add first."
    if not services.valid_path(path):
        return "Refused: path must be relative to the service base_url (e.g. \"/\" or \"/message\")."
    if not isinstance(body, (dict, list)):
        return "Refused: body must be a JSON object or array (the template)."
    if len(json.dumps(body, ensure_ascii=False)) > MAX_TEMPLATE:
        return f"Refused: body template is longer than {MAX_TEMPLATE} characters."
    return ""


def _same_target(a: dict, b: dict) -> bool:
    return all(a.get(k) == b.get(k) for k in ("service", "path", "body"))


def set_rule(recipient: str, service: str, body, path: str = "/", enabled: bool = True) -> str:
    import authz
    recipient, path = (recipient or "").strip(), (path or "/").strip() or "/"
    owner, _ = _caller()
    problem = _problem(recipient, service, path, body, owner)
    if problem:
        return problem
    rule = {"recipient": recipient, "service": service, "path": path, "body": body,
            "enabled": bool(enabled), "owner": owner, "updated": _now()}
    try:
        with store.locked(LOCK):
            current = rules()
            old = next((r for r in current
                        if r.get("recipient") == recipient and r.get("owner") == owner), None)
            kept_approval = bool(old and old.get("approved") and _same_target(old, rule))
            rule["approved"] = kept_approval or authz.from_webui()
            rule["target"] = old.get("target") if kept_approval else (
                target(rule) if rule["approved"] else "")
            kept = [r for r in current if r is not old]
            store.write_json(RULES_FILE, kept + [rule])
    except store.StoreCorrupt as exc:
        return f"Refused: {exc}"
    state = "on" if enabled else "off"
    head = f"Forwarding for '{recipient}' → service '{service}' (path {path}, {state}) saved."
    if rule["approved"]:
        return f"{head} Try it with inbox_forward_test(recipient=\"{recipient}\")."
    return (f"{head} It sends nothing until the person approves it in the AICortex WebUI "
            f"(tab Inbox) — it would send to {host_of(rule) or '?'}.")


def approve(recipient: str) -> str:
    """Switch a pending rule on — only the person, in the WebUI."""
    import authz
    if not authz.from_webui():
        return ("Refused: only the person approves a forwarding rule, in the AICortex WebUI "
                "(tab Inbox).")
    owner, _ = _caller()
    try:
        with store.locked(LOCK):
            current = rules()
            rule = next((r for r in current
                         if r.get("recipient") == recipient and r.get("owner") == owner), None)
            if rule is None:
                return f"No forwarding for '{recipient}'."
            rule["approved"], rule["updated"], rule["target"] = True, _now(), target(rule)
            store.write_json(RULES_FILE, current)
    except store.StoreCorrupt as exc:
        return f"Refused: {exc}"
    state = "on" if rule.get("enabled", True) else "off (switched off)"
    return f"Forwarding for '{recipient}' approved — it is {state}."


def delete_rule(recipient: str) -> str:
    owner, _ = _caller()
    try:
        with store.locked(LOCK):
            current = rules()
            kept = [r for r in current
                    if not (r.get("recipient") == recipient and r.get("owner") == owner)]
            if len(kept) == len(current):
                return f"No forwarding for '{recipient}'."
            store.write_json(RULES_FILE, kept)
    except store.StoreCorrupt as exc:
        return f"Refused: {exc}"
    return f"Deleted forwarding for '{recipient}'."


def _own_rule(recipient: str):
    owner, _ = _caller()
    return next((r for r in rules()
                 if r.get("recipient") == recipient and r.get("owner") == owner), None)


def _listing() -> str:
    """Own rules for everyone; all rules and their templates only for an admin."""
    import authz
    owner, role = _caller()
    admin = role == "admin" or not authz.enabled()
    try:
        visible = [r for r in rules() if admin or (owner and r.get("owner") == owner)]
    except store.StoreCorrupt as exc:
        return f"Forwarding rules unreadable: {exc}"
    if not visible:
        return "No forwarding rules. Create one with inbox_forward_set."
    lines = []
    for r in visible:
        state = ("on" if r.get("enabled", True) else "off") + (
            "" if r.get("approved") else ", waiting for approval in the WebUI")
        line = (f"- {r['recipient']} → {r['service']} {r.get('path', '/')} → {host_of(r) or '?'} "
                f"({state}, owner {r.get('owner') or '-'})")
        if admin:
            line += f" body {json.dumps(r.get('body'), ensure_ascii=False)}"
        lines.append(line)
    owners = {r.get("owner", "") for r in visible}
    last = [e for e in recent() if e.get("owner", "") in owners][-10:]
    if last:
        lines.append("Latest deliveries:")
        lines += [f"  {e['ts']} {e['recipient']} → {e['service']}: "
                  f"{'ok' if e['ok'] else 'FAILED'} {e['info']}" for e in last]
    return "\n".join(lines)


def register(mcp):
    @mcp.tool
    def inbox_forward_set(
        recipient: Annotated[str, Field(description='Inbox recipient whose new messages are forwarded, usually "user"')],
        service: Annotated[str, Field(description="A registered service (service_add) that receives the POST, e.g. ntfy or gotify")],
        body: Annotated[dict | list, Field(description="JSON body template; strings may use {title} {subject} {body} {from} {to} {id} {ts}")],
        path: Annotated[str, Field(description='Path under the service base_url, e.g. "/" (ntfy) or "/message" (Gotify)')] = "/",
        enabled: Annotated[bool, Field(description="False keeps the rule but sends nothing")] = True,
    ) -> str:
        """Forward every new inbox message for `recipient` as a JSON POST through a
        registered service — so job reports and webhook events reach the person as a push.
        One rule per recipient and person; setting it again replaces it. A new rule, or a
        change of service, path or body, sends nothing until the person approves it in
        the AICortex WebUI (tab Inbox) — tell them to.
        ntfy:   path "/", body {"topic": "aicortex", "title": "{title}", "message": "{body}", "markdown": true}
        Gotify: path "/message", body {"title": "{title}", "message": "{body}", "priority": 5}
                (service with auth_header "X-Gotify-Key", auth_scheme "").
        Returns what was saved, or why it was refused."""
        return set_rule(recipient, service, body, path, enabled)

    @mcp.tool
    def inbox_forward_list() -> str:
        """Your forwarding rules (an admin: everyone's, with templates), whether each is
        approved, and the latest deliveries (ok or the status code)."""
        return _listing()

    @mcp.tool
    def inbox_forward_delete(
        recipient: Annotated[str, Field(description="The recipient whose forwarding rule (yours) is removed")],
    ) -> str:
        """Remove your forwarding rule for `recipient`. Messages stay in the inbox."""
        return delete_rule(recipient)

    @mcp.tool
    def inbox_forward_test(
        recipient: Annotated[str, Field(description="The recipient whose forwarding rule (yours) is tested")],
    ) -> str:
        """Send one fixed test message through your forwarding rule for `recipient` now
        and report the answer — also before the rule is approved; no inbox content is
        sent. Outbound action → confirm with the user before sending."""
        try:
            rule = _own_rule(recipient)
        except store.StoreCorrupt as exc:
            return f"Forwarding rules unreadable: {exc}"
        if not rule:
            return f"No forwarding for '{recipient}'. Create one with inbox_forward_set."
        msg = dict(TEST_MESSAGE, to=recipient, ts=_now(), person=rule.get("owner", ""))
        ok, info = deliver(rule, msg)
        _note("test", rule, ok, info)
        return f"Sent: {info}" if ok else f"Failed: {info}"
