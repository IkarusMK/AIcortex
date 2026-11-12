"""Upgrade check: the old release writes data and hands out OAuth tokens the way
claude.ai gets them; then the new code starts on the SAME data and must keep
everything working — no re-login, nothing lost. See README.md for how to run it.

    python harness.py <old-app-dir> <old-python> <new-app-dir> <new-python>
"""
import asyncio
import hashlib
import json
import os
import re
import secrets
import shutil
import socket
import subprocess
import sys
import tempfile
import time
from pathlib import Path
from urllib.parse import parse_qs, urlparse

import httpx
from cryptography.fernet import Fernet
from fastmcp import Client

sys.path.insert(0, str(Path(__file__).parent))
from fake_idp import FakeIdP, b64u  # noqa: E402

DATA = Path(tempfile.mkdtemp(prefix="aicortex-upgrade-"))
CB = "http://127.0.0.1:9/callback"
DATA_ENV = ("APIKEY_DIR", "AUTH_STORE_DIR", "CALDAV_DIR", "COORD_DIR", "CRON_DIR", "FTP_DIR",
            "IMAP_DIR", "MAIL_DIR", "MCP_DIR", "MEMORY_DIR", "MQTT_DIR", "PRINT_DIR",
            "SCAN_DIR", "SERVICES_DIR", "SESSIONS_DIR", "SKILLS_DIR", "SSH_DIR", "VAULT_DIR",
            "WEBDAV_DIR", "WEBHOOK_DIR", "WORK_DIR", "PLAYBOOKS_DIR")
RUNNER = secrets.token_urlsafe(32)
VAULT_KEY = Fernet.generate_key().decode()
JWT_KEY = secrets.token_hex(32)
LOGS = DATA / "logs"

results = []
PLAYBOOKS = {"old": False}      # did the old release write a playbook and a run?
LEGACY = {"names": False}       # did the old release (1.x) name note files by the ASCII slug?


def check(name, ok, detail=""):
    results.append((name, bool(ok), detail))
    print(("  OK   " if ok else "  FAIL ") + name + (f"  — {detail}" if detail and not ok else ""))


def free_port():
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def env_for(port, idp):
    env = {k: v for k, v in os.environ.items() if not k.startswith(("OIDC_", "TOOL_PROFILE"))}
    for var in DATA_ENV:
        env[var] = str(DATA / var.lower())
    env.update({
        "DATA_ROOT": str(DATA), "CATALOG_CACHE_FILE": str(DATA / "catalog_cache.json"),
        "MCP_HOST": "127.0.0.1", "MCP_PORT": str(port), "BASE_URL": f"http://127.0.0.1:{port}",
        "OIDC_CONFIG_URL": f"{idp.base}/.well-known/openid-configuration",
        "OIDC_CLIENT_ID": "aicortex", "OIDC_CLIENT_SECRET": "upstream-secret",
        "JWT_SIGNING_KEY": JWT_KEY,
        "STORAGE_ENCRYPTION_KEY": VAULT_KEY,
        "RUNNER_TOKEN": RUNNER, "INTERNAL_ALLOW_CIDRS": "127.0.0.0/8",
        "PYTHONUNBUFFERED": "1",
    })
    return env


def start(app_dir, python, port, idp, log):
    proc = subprocess.Popen([python, "server.py"], cwd=app_dir, env=env_for(port, idp),
                            stdout=open(log, "w"), stderr=subprocess.STDOUT)
    for _ in range(150):
        try:
            if httpx.get(f"http://127.0.0.1:{port}/.well-known/oauth-authorization-server",
                         timeout=1).status_code == 200:
                return proc
        except httpx.HTTPError:
            pass
        if proc.poll() is not None:
            break
        time.sleep(0.2)
    proc.terminate()
    sys.exit(f"server did not start, see {log}:\n" + Path(log).read_text()[-2000:])


def login(base):
    meta = httpx.get(f"{base}/.well-known/oauth-authorization-server").json()
    reg = httpx.post(meta["registration_endpoint"], json={
        "client_name": "claude-like", "redirect_uris": [CB],
        "grant_types": ["authorization_code", "refresh_token"], "response_types": ["code"],
        "token_endpoint_auth_method": "none"})
    assert reg.status_code in (200, 201), reg.text
    client_id = reg.json()["client_id"]
    return client_id, authorize(base, meta, client_id)


def authorize(base, meta, client_id):
    verifier = b64u(os.urandom(32))
    challenge = b64u(hashlib.sha256(verifier.encode()).digest())
    # One browser (cookies kept) walks the redirects. Since 2.6.0 FastMCP shows its
    # consent page once per client before the IdP; approve it like a person would.
    # 2.5.0 has no consent step, so the same walk works for both versions.
    browser = httpx.Client(timeout=30)
    r = browser.get(meta["authorization_endpoint"], params={
        "response_type": "code", "client_id": client_id, "redirect_uri": CB,
        "code_challenge": challenge, "code_challenge_method": "S256", "state": "st1",
        "resource": f"{base}/mcp"})
    loc = ""
    for _ in range(10):
        assert r.status_code in (302, 303, 307), (r.status_code, r.text[:400])
        loc = r.headers["location"]
        if loc.startswith(CB):
            break
        if "/consent?" in loc:
            target = loc if loc.startswith("http") else f"{base}{loc}"
            page = browser.get(target)
            csrf = re.search(r'name="csrf_token" value="([^"]+)"', page.text).group(1)
            txn = parse_qs(urlparse(target).query)["txn_id"][0]
            r = browser.post(f"{base}/consent", data={
                "action": "approve", "txn_id": txn, "csrf_token": csrf, "submit": "true"})
            continue
        r = browser.get(loc if loc.startswith("http") else f"{base}{loc}")
    browser.close()
    code = parse_qs(urlparse(loc).query)["code"][0]
    tok = httpx.post(meta["token_endpoint"], data={
        "grant_type": "authorization_code", "code": code, "redirect_uri": CB,
        "client_id": client_id, "code_verifier": verifier, "resource": f"{base}/mcp"})
    assert tok.status_code == 200, tok.text
    return tok.json()


async def mcp(base, token, calls):
    out = []
    async with Client(f"{base}/mcp", auth=token) as c:
        for name, args in calls:
            if name == "__list__":
                out.append([t.name for t in await c.list_tools()])
                continue
            res = await c.call_tool(name, args, raise_on_error=False)
            text = "\n".join(getattr(x, "text", "") for x in res.content or [])
            out.append(("ERROR: " if res.is_error else "") + text)
    return out


def v1_phase(base, idp):
    client_id, tok = login(base)
    print(f"v1: OAuth login ok (client {client_id[:12]}…)")
    # Since 2.6 a secret is bound to the hosts it may go to, and an assistant binds a
    # new one only together with its value (secret_set(..., hosts=...)). Older
    # releases don't know the argument, so fall back to the plain call there.
    echo_host = urlparse(idp.base).hostname
    seeded = asyncio.run(mcp(base, tok["access_token"], [
        ("secret_set", {"name": "ECHO_TOKEN", "value": "s3cr3t-echo", "hosts": echo_host})]))
    if seeded[0].startswith("ERROR"):
        seeded = asyncio.run(mcp(base, tok["access_token"], [
            ("secret_set", {"name": "ECHO_TOKEN", "value": "s3cr3t-echo"})]))
    if seeded[0].startswith("ERROR"):
        sys.exit("v1 phase failed: " + seeded[0])
    r = asyncio.run(mcp(base, tok["access_token"], [
        ("memory_write", {"title": "Größe der Düse", "type": "reference", "content": "0.4 mm"}),
        ("memory_write", {"title": "Printer setup", "type": "project",
                          "content": "The printer uses the PLA+ profile.", "tags": "birdhouse"}),
        ("memory_write", {"title": "Answer style", "type": "feedback", "content": "Short answers."}),
        ("skill_write", {"name": "printer-slicing", "description": "Slice models for the 3D printer",
                         "instructions": "Use SliceTool.", "category": "Birdhouse"}),
        ("secret_set", {"name": "BOB_TOKEN", "value": "bob-only", "owner": "bob-sub"}),
        ("service_add", {"name": "echo", "base_url": idp.base, "token_env": "ECHO_TOKEN",
                         "category": "test", "description": "echo endpoint"}),
        ("call_service", {"service": "echo", "path": "/echo"}),
        ("task_add", {"title": "Old task from v1"}),
        ("session_save", {"summary": "Old session from v1", "session_id": "s-old",
                          "next_steps": "continue in v2"}),
        ("agent_register", {"name": "old-agent", "capabilities": "testing"}),
        ("cron_add", {"name": "daily-check", "schedule": "30 6 * * *", "prompt": "check things"}),
        ("apikey_create", {"identity": "alice-sub", "name": "n8n", "scopes": "all"}),
    ]))
    for line in r:
        if line.startswith("ERROR"):
            sys.exit("v1 phase failed: " + line)
    key = next((w for w in r[-1].replace("`", " ").split() if w.startswith("ak_")), "")
    assert key, r[-1]
    assert idp.echo_seen and "s3cr3t-echo" in json.dumps(idp.echo_seen[-1]), "v1 echo"
    LEGACY["names"] = (DATA / "memory_dir" / "shared" / "gr-e-der-d-se.md").exists()
    # A playbook and a started run (releases since 2.0 have them) must survive the update.
    try:
        pb = asyncio.run(mcp(base, tok["access_token"], [
            ("playbook_write", {"name": "upgrade-check", "title": "Upgrade check",
                                "description": "kept across updates", "stages": [
                                    {"id": "one", "name": "One", "goal": "a goal", "artifact": "one.md",
                                     "approval": "webui"}]}),
            ("playbook_start", {"playbook": "upgrade-check", "project": "UpgradeCheck",
                                "title": "Upgrade check run"})]))
        PLAYBOOKS["old"] = not any(line.startswith("ERROR") for line in pb)
    except Exception:
        PLAYBOOKS["old"] = False
    # The runner set up in the WebUI as 2.2 kept it (2.3 takes it over as the standard runner).
    (DATA / "cron_dir").mkdir(parents=True, exist_ok=True)
    (DATA / "cron_dir" / "runner_config.json").write_text(json.dumps({
        "backend": "openrouter", "model": "vendor/model-a", "builtin": True, "updated_by": "upgrade-check"}))
    print("v1: data written; REST key and v1 echo ok")
    return client_id, tok, key


def v2_phase(base, idp, client_id, tok, key):
    meta = httpx.get(f"{base}/.well-known/oauth-authorization-server").json()
    # 1. The token claude.ai already holds keeps working (no re-login).
    try:
        r = asyncio.run(mcp(base, tok["access_token"], [
            ("__list__", {}),
            ("memory_read", {"name": "Größe der Düse"}),
            ("memory_read", {"name": "gr-e-der-d-se"}),
            ("memory_write", {"title": "Größe der Düse", "type": "reference", "content": "0.6 mm"}),
            ("memory_list", {}),
            ("memory_search", {"query": "Düse"}),
            ("memory_search", {"query": "printer profile"}),
            ("skill_load", {"name": "printer-slicing"}),
            ("skill_search", {"query": "slicing"}),
            ("secret_list", {}),
            ("call_service", {"service": "echo", "path": "/echo"}),
            ("task_list", {}),
            ("session_load", {"session_id": "s-old"}),
            ("agent_list", {}),
            ("cron_list", {}),
            ("bootstrap", {}),
            ("cortex_start", {}),
            ("runner_list", {}),
            ("playbook_list", {}),
            ("playbook_runs", {"status": "all"}),
        ]))
        check("old OAuth access token still accepted", True)
    except Exception as exc:
        check("old OAuth access token still accepted", False, repr(exc)[:300])
        r = None
    if r:
        (tools, read_title, read_name, rewrite, mlist, s_duese, s_printer, skill, s_skill,
         secrets, echo, tasks, sess, agents, cron, boot, start, runners, pbs, pb_runs) = r
        check("tools/list works", len(tools) > 0, str(tools))
        check("bootstrap is offered (old project instructions say 'call bootstrap')",
              "bootstrap" in tools, str(tools))
        check("memory_read by old umlaut title", "0.4 mm" in read_title, read_title[:200])
        if LEGACY["names"]:
            check("memory_read by old file name", "0.4 mm" in read_name, read_name[:200])
        dupes = sorted(p.name for p in (DATA / "memory_dir" / "shared").glob("*.md")
                       if "Größe der Düse" in p.read_text(encoding="utf-8"))
        check("rewriting the umlaut title updates the old note (no duplicate)",
              len(dupes) == 1, f"{dupes} / {rewrite[:120]}")
        check("memory_list shows old notes", "Printer setup" in mlist or "printer-setup" in mlist,
              mlist[:300])
        check("memory_search finds old umlaut note", "0." in s_duese or "d-se" in s_duese
              or "düse" in s_duese.lower(), s_duese[:300])
        check("memory_search finds old note by words", "printer-setup" in s_printer, s_printer[:300])
        check("old skill loads", "SliceTool" in skill, skill[:200])
        check("old skill found by search", "printer-slicing" in s_skill, s_skill[:200])
        check("old vault secret listed", "ECHO_TOKEN" in secrets, secrets[:300])
        seen = json.dumps(idp.echo_seen[-1]) if idp.echo_seen else ""
        check("old vault secret decrypts and is injected", "s3cr3t-echo" in seen and
              len(idp.echo_seen) >= 2, echo[:200])
        check("old task visible", "Old task from v1" in tasks, tasks[:300])
        check("old session loads", "Old session from v1" in sess, sess[:300])
        check("old agent listed", "old-agent" in agents, agents[:300])
        check("old cron job listed", "daily-check" in cron, cron[:300])
        check("bootstrap still answers", not boot.startswith("ERROR") and len(boot) > 50, boot[:200])
        check("cortex_start works on old data", "Answer style" in start, start[:400])
        check("the old runner setup is the standard runner", "Standard — openrouter — on — standard"
              in runners, runners[:300])
        check("the old cron job stays unassigned (goes to the standard runner)",
              "daily-check" in cron and "runner r_" not in cron, cron[:300])
        if PLAYBOOKS["old"]:
            check("the old playbook is still in the library", "upgrade-check" in pbs, pbs[:300])
            check("the old playbook run is still there", "Upgrade check run" in pb_runs, pb_runs[:300])

    # 2. Refresh token from v1 yields a working v2 token.
    rr = httpx.post(meta["token_endpoint"], data={"grant_type": "refresh_token",
                                                  "refresh_token": tok.get("refresh_token", ""),
                                                  "client_id": client_id})
    ok = rr.status_code == 200 and "access_token" in rr.json()
    check("old refresh token works", ok, f"{rr.status_code} {rr.text[:200]}")
    if ok:
        try:
            asyncio.run(mcp(base, rr.json()["access_token"], [("memory_list", {})]))
            check("refreshed token works on /mcp", True)
        except Exception as exc:
            check("refreshed token works on /mcp", False, repr(exc)[:200])

    # 3. The client claude.ai registered in v1 is still known (fresh login works).
    try:
        fresh = authorize(base, meta, client_id)
        check("old registered client can log in again", "access_token" in fresh)
    except Exception as exc:
        check("old registered client can log in again", False, repr(exc)[:300])

    # 4. RUNNER_TOKEN and the v1 REST key.
    try:
        asyncio.run(mcp(base, RUNNER, [("memory_list", {})]))
        check("RUNNER_TOKEN still works", True)
    except Exception as exc:
        check("RUNNER_TOKEN still works", False, repr(exc)[:200])
    rest = httpx.post(f"{base}/api/v1/tools/memory_list", json={},
                      headers={"Authorization": f"Bearer {key}"})
    check("v1 REST key still works on /api/v1", rest.status_code == 200, f"{rest.status_code} {rest.text[:200]}")
    try:
        asyncio.run(mcp(base, key, [("memory_list", {})]))
        check("v1 REST key now also works on /mcp", True)
    except Exception as exc:
        check("v1 REST key now also works on /mcp", False, repr(exc)[:200])


def main():
    v1_app, v1_py, v2_app, v2_py = sys.argv[1:5]
    LOGS.mkdir(parents=True, exist_ok=True)
    idp = FakeIdP()
    port = free_port()
    base = f"http://127.0.0.1:{port}"

    p1 = start(v1_app, v1_py, port, idp, LOGS / "old.log")
    try:
        client_id, tok, key = v1_phase(base, idp)
    finally:
        p1.terminate()
        p1.wait(10)

    p2 = start(v2_app, v2_py, port, idp, LOGS / "new.log")
    try:
        v2_phase(base, idp, client_id, tok, key)
    finally:
        p2.terminate()
        p2.wait(10)
    failed = [n for n, ok, _ in results if not ok]
    print(f"\n{len(results) - len(failed)}/{len(results)} checks passed")
    if failed:
        print(f"data and server logs kept in {DATA}")
        sys.exit(1)
    shutil.rmtree(DATA, ignore_errors=True)


if __name__ == "__main__":
    main()
