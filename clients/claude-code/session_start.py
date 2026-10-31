#!/usr/bin/env python3
"""Claude Code SessionStart hook: load the AICortex briefing into every session.

Claude Code keeps the SHORT-TERM memory of the work at hand (its own memory and
CLAUDE.md). AICortex holds the LONG-TERM memory every AI shares: who the user is,
how they want to work, their projects and decisions, and checkpoints of big
projects. This hook fetches that part when a session starts — and again after
/clear or a compaction — so it is there without anyone having to remember to ask.
If AICortex can't be reached, the hook says so instead of letting the session
quietly run without it.

Configuration (environment; see docs/clients.md):
  AICORTEX_URL      base URL, e.g. https://agent.example.com
  AICORTEX_KEY      an agent key (ak_...) — or, better, keep it out of files:
  AICORTEX_KEY_CMD  a command that prints the key, e.g.
                    security find-generic-password -s aicortex -w
  AICORTEX_PROJECT  optional project name. Default: the first line of a
                    .aicortex-project file in the repository, else the
                    repository's folder name; outside a repository, none.

Python 3.9+, standard library only. Always exits 0; never prints the key.
"""
import ipaddress
import json
import os
import shlex
import subprocess
import sys
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path

TIMEOUT = 8
MAX_CONTEXT = 9500          # Claude Code caps injected context at 10,000 characters

RULES = """How memory is split here:
- Short-term (this session, this machine): Claude Code's own memory and CLAUDE.md.
- Long-term, shared by every app and agent, in AICortex: who the user is, how they
  want to work, their projects and decisions. Save facts that should outlive this
  session with memory_write(..., project=...) and look things up with memory_search.
- Big projects: save a checkpoint with session_save(summary, next_steps, project=...)
  at milestones and before stopping, so the next session or another app continues.
AICortex is the "aicortex" MCP server (cortex_start, memory_search, memory_write,
session_save, integration_search, ...)."""


def _german() -> bool:
    return any(os.environ.get(v, "").lower().startswith("de") for v in ("LANG", "LC_ALL", "LC_MESSAGES"))


def _say(de: str, en: str) -> str:
    return de if _german() else en


def _emit(context: str, warning: str = "") -> None:
    out = {"hookSpecificOutput": {"hookEventName": "SessionStart",
                                  "additionalContext": context[:MAX_CONTEXT]}}
    if warning:
        out["systemMessage"] = warning
    sys.stdout.write(json.dumps(out))


def _fail(reason: str) -> None:
    _emit(f"AICortex (the user's long-term memory) could not be loaded at session start: "
          f"{reason}. Tell the user at the start of your first answer, and do not guess "
          f"their projects or preferences.",
          _say(f"AICortex nicht geladen: {reason}. Diese Sitzung läuft ohne Langzeitgedächtnis.",
               f"AICortex not loaded: {reason}. This session runs without the long-term memory."))


def _project(cwd: str) -> str:
    if os.environ.get("AICORTEX_PROJECT", "").strip():
        return os.environ["AICORTEX_PROJECT"].strip()[:80]
    start = Path(cwd or os.getcwd()).resolve()
    for d in [start, *start.parents]:
        marker = d / ".aicortex-project"
        if marker.is_file():
            first = marker.read_text(encoding="utf-8").strip().splitlines()
            return first[0].strip()[:80] if first else d.name
        if (d / ".git").exists():
            return d.name
    return ""          # outside a repository: no folder name (e.g. a home dir) leaves the machine


def _key() -> str:
    key = os.environ.get("AICORTEX_KEY", "").strip()
    cmd = os.environ.get("AICORTEX_KEY_CMD", "").strip()
    if not key and cmd:
        try:
            done = subprocess.run(shlex.split(cmd), capture_output=True, text=True, timeout=5)
            key = done.stdout.strip()
        except (OSError, subprocess.SubprocessError):
            key = ""
    return key


def _private_host(host: str) -> bool:
    if host in ("localhost",) or host.endswith((".local", ".lan", ".home.arpa", ".internal")) \
            or "." not in host:
        return True
    try:
        return ipaddress.ip_address(host).is_private
    except ValueError:
        return False


def main() -> None:
    try:
        event = json.loads(sys.stdin.read() or "{}")
    except ValueError:
        event = {}
    url = os.environ.get("AICORTEX_URL", "").strip().rstrip("/")
    parsed = urllib.parse.urlparse(url)
    if not url or parsed.scheme not in ("http", "https") or not parsed.hostname:
        return _fail("AICORTEX_URL is not set (e.g. https://agent.example.com)")
    if parsed.scheme == "http" and not _private_host(parsed.hostname):
        return _fail("AICORTEX_URL must use https for a public host — the key would travel unencrypted")
    key = _key()
    if not key:
        return _fail("no key — set AICORTEX_KEY_CMD (e.g. the macOS keychain) or AICORTEX_KEY")
    project = _project(str(event.get("cwd") or ""))
    req = urllib.request.Request(
        f"{url}/api/v1/tools/cortex_start", data=json.dumps({"project": project}).encode(),
        headers={"Authorization": f"Bearer {key}", "Content-Type": "application/json",
                 "Accept": "application/json"}, method="POST")
    try:
        with urllib.request.urlopen(req, timeout=TIMEOUT) as resp:
            body = json.loads(resp.read().decode("utf-8", "replace"))
    except urllib.error.HTTPError as exc:
        return _fail(f"the server answered HTTP {exc.code}"
                     + (" — check the key and its scopes" if exc.code in (401, 403) else ""))
    except (urllib.error.URLError, OSError, ValueError) as exc:
        reason = getattr(exc, "reason", exc)
        return _fail(f"no connection to {parsed.hostname} ({type(reason).__name__})")
    briefing = body.get("result") if isinstance(body, dict) else None
    if not isinstance(briefing, str) or not briefing.strip():
        return _fail("the server sent no briefing")
    _emit(f"AICortex briefing — the user's long-term memory, loaded at session start "
          f"({event.get('source') or 'startup'}):\n\n{briefing.strip()}\n\n{RULES}")


if __name__ == "__main__":
    try:
        main()
    except Exception as exc:   # a hook must never break the session
        _fail(f"the hook failed ({type(exc).__name__})")
    sys.exit(0)
