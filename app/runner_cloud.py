"""Wake a Claude Code cloud task with the due jobs — a "claude-cloud" runner.

The person creates a cloud task on claude.ai/code with an API trigger and one fixed
sentence as its prompt (SETUP_SENTENCE): carry out what AICortex sends. When a job is
due, AICortex calls the trigger's /fire endpoint with the job in the wake-up text;
Claude runs it in Anthropic's cloud on the person's own plan and reaches AICortex
through their connector. Anthropic marks the wake-up text as untrusted data that
can't grant approval — so the frame (runner_cloud_frame.txt) keeps jobs to reading and
to what their prompt explicitly allows, as for every runner.

Anthropic's limits: 30 wake-ups an hour per task (so at most one every two minutes
here), 65,536 characters per wake-up, and no idempotency key — a retried wake-up
would start a second session, so a failed one is not retried.
"""
import json
import threading
import time
from datetime import datetime
from pathlib import Path

import httpx

import cron
import runner_state
import runners

ANTHROPIC_VERSION = "2023-06-01"
BETA = "experimental-cc-routine-2026-04-01"
TIMEOUT = 30
MIN_GAP = 120              # seconds between wake-ups of one task (≤ 30 an hour)
MAX_WAIT = 3600            # the longest Retry-After honoured
MAX_TEXT = 60_000          # characters per wake-up (Anthropic allows 65,536)
MAX_WAKES = 5              # wake-ups of one task in one minute, when jobs are long
SESSION_PREFIX = "https://claude.ai/"
FRAME = (Path(__file__).parent / "runner_cloud_frame.txt").read_text(encoding="utf-8")
SETUP_SENTENCE = {
    "en": "Carry out the job AICortex sends with this wake-up call (the <routine-fire-payload> block).",
    "de": "Führe den Auftrag aus, den AICortex im Weckruf mitschickt (Block <routine-fire-payload>).",
}
TEST_TEXT = ("AICortex connection test for runner {rid}. This is not a job: reply with one "
             "short sentence that you received it, and do nothing else.")

_next: dict = {}           # runner id → earliest time for its next wake-up
_lock = threading.Lock()


def _clock() -> float:
    return time.time()


def reset() -> None:
    with _lock:
        _next.clear()


def _may_wake(rid: str) -> bool:
    with _lock:
        return _clock() >= _next.get(rid, 0)


def _hold(rid: str, seconds: float) -> None:
    with _lock:
        _next[rid] = _clock() + max(MIN_GAP, min(seconds, MAX_WAIT))


def _text(rec: dict, jobs: list) -> str:
    listed = [{k: j[k] for k in ("id", "name", "prompt", "notify")} for j in jobs]
    return (f"{FRAME}\nRunner: {rec['id']} ({rec.get('name', '')})\n"
            f"Jobs (JSON):\n{json.dumps(listed, ensure_ascii=False)}")


def _texts(rec: dict, due: list) -> list:
    """[(text, jobs)] — as many jobs per wake-up as fit; a job too long on its own gets
    a wake-up of its own (and is then refused as too long)."""
    packs: list = []
    for job in due:
        if packs and len(_text(rec, packs[-1] + [job])) <= MAX_TEXT:
            packs[-1] = packs[-1] + [job]
        else:
            packs.append([job])
    return [(_text(rec, jobs), jobs) for jobs in packs]


def _scrub(text: str, token: str) -> str:
    return text.replace(token, "***") if token else text


def _retry_after(r) -> float:
    try:
        return float(r.headers.get("retry-after") or 0)
    except ValueError:
        return 0.0


def _fire(rec: dict, token: str, text: str):
    """(ok, session link or error, seconds to wait) for one wake-up."""
    headers = {"Authorization": f"Bearer {token}", "anthropic-version": ANTHROPIC_VERSION,
               "anthropic-beta": BETA}
    try:
        with httpx.Client(timeout=TIMEOUT) as http:
            r = http.post(runners.fire_url(rec), json={"text": text}, headers=headers)
    except httpx.HTTPError as exc:
        return False, f"Anthropic could not be reached ({exc.__class__.__name__})", 0.0
    if r.status_code != 200:
        return False, _scrub(f"Anthropic answered HTTP {r.status_code}: {r.text[:200]}", token), _retry_after(r)
    try:
        link = r.json().get("claude_code_session_url")
    except (ValueError, AttributeError):
        link = None
    return True, link if isinstance(link, str) and link.startswith(SESSION_PREFIX) else "", 0.0


def wake(rec: dict) -> int:
    """Wake the runner's cloud task when jobs are due → how many were handed over.
    Jobs are claimed only when a wake-up may go out now, so a waiting job is caught
    up later; a failed wake-up leaves its jobs claimed (that minute is skipped)."""
    rid = rec["id"]
    if not _may_wake(rid):
        return 0
    token = runners.resolve_key(rec)
    if not token or not runners.fire_url(rec):
        runner_state.put(rid, "no-key")
        return 0
    due = cron.claim_due(rid, tokens=False)
    if not due:
        runner_state.put(rid, "idle")
        return 0
    handed, packs = 0, _texts(rec, due)
    for _, jobs in packs[MAX_WAKES:]:                     # more than one minute's wake-ups
        for j in jobs:
            cron.mark_run(j["id"], "Not sent: too many long jobs at once — it runs at its next time.")
    for text, jobs in packs[:MAX_WAKES]:
        if len(text) > MAX_TEXT:
            for j in jobs:
                cron.mark_run(j["id"], "Not sent: the job is too long for a Claude cloud task.")
            continue
        ok, info, wait = _fire(rec, token, text)
        _hold(rid, wait)
        if not ok:
            runner_state.put(rid, "error", info)
            continue
        for j in jobs:
            cron.mark_run(j["id"], f"Handed to Claude — {info}" if info else "Handed to Claude.")
        handed += len(jobs)
        runner_state.put(rid, "ran", f"{len(jobs)} job(s): {', '.join(j['id'] for j in jobs)}", info)
    return handed


def test(rec: dict):
    """(ok, message) — a wake-up with a harmless check instead of a job."""
    token = runners.resolve_key(rec)
    if not token or not runners.fire_url(rec):
        return False, "No trigger or token stored yet."
    if not _may_wake(rec["id"]):
        return False, "The cloud task was woken less than two minutes ago — try again shortly."
    ok, info, wait = _fire(rec, token, TEST_TEXT.format(rid=rec["id"]))
    _hold(rec["id"], wait)
    if not ok:
        runner_state.put(rec["id"], "error", info)
        return False, info
    runner_state.put(rec["id"], "ran", f"test at {datetime.now().strftime('%H:%M')}", info)
    return True, f"Claude was woken — {info}" if info else "Claude was woken."
