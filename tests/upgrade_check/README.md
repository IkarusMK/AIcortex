# Upgrade check

Proves that an existing installation keeps working after an update. The old release
runs first: it signs in a client the way claude.ai does (DCR, PKCE, a fake OIDC
provider) and writes memory, skills, vault secrets, a service, a task, a session, an
agent, a cron job and a REST key. Then the new code starts on the **same** data and
must accept the old tokens, refresh them, find everything and use the vault.

It needs two Python environments (the old and the new `fastmcp` differ), so it is not
part of the normal `pytest` run. Run it before every release:

```bash
git worktree add /tmp/aicortex-old v1.12.2          # the release users are on
python3 -m venv /tmp/old-venv && /tmp/old-venv/bin/pip install -r /tmp/aicortex-old/app/requirements.txt
python3 -m venv /tmp/new-venv && /tmp/new-venv/bin/pip install -r app/requirements.txt
cd tests/upgrade_check
/tmp/new-venv/bin/python harness.py /tmp/aicortex-old/app /tmp/old-venv/bin/python ../../app /tmp/new-venv/bin/python
```

Every check prints `OK` or `FAIL`; the exit code is 0 only when all pass. On a failure
the data folder and both server logs are kept and their path is printed.
