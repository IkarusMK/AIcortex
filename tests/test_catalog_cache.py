"""Stat-signature catalog cache (v1.9.1).

Proves the bootstrap perf fix: a section is rendered once, then served from cache
WITHOUT re-running the (file-reading) render_fn while the source dir is unchanged;
adding or editing a file invalidates that section; the cache is byte-identical to a
live render; and it survives a process restart (disk-persisted). No MCP server needed.
"""
import os
import sys
import tempfile
import time
from pathlib import Path

_D = Path(tempfile.mkdtemp())
os.environ["CATALOG_CACHE_FILE"] = str(_D / "cache.json")
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "app"))

import catalog_cache as cc  # noqa: E402

failures = []


def check(name, cond):
    print(("PASS" if cond else "FAIL"), name)
    if not cond:
        failures.append(name)


SK = _D / "skills"
SK.mkdir()


def _mk(name, cat, body="body"):
    p = SK / name
    p.mkdir(exist_ok=True)
    (p / "SKILL.md").write_text(f"---\ncategory: {cat}\n---\n{body}", encoding="utf-8")


_mk("alpha", "X")
_mk("beta", "Y")

# render_fn counts invocations and actually reads the files (like _skill_list does).
calls = {"n": 0}


def render():
    calls["n"] += 1
    counts = {}
    for sk in sorted(SK.glob("*/SKILL.md")):
        text = sk.read_text(encoding="utf-8")
        cat = text.split("category:", 1)[1].split("\n", 1)[0].strip()
        counts[cat] = counts.get(cat, 0) + 1
    return [f"  {c}: {n}" for c, n in sorted(counts.items())]


def call():
    return cc.cached_lines("skills", [SK], "*/SKILL.md", render)


# 1) first call renders
l1 = call()
check("first call renders (render_fn ran once)", calls["n"] == 1)

# 2) warm call — unchanged dir → served from cache, render_fn NOT re-run
l2 = call()
check("warm call served from cache (no re-render)", calls["n"] == 1)
check("warm result identical to first", l1 == l2)

# 3) cached == a fresh live render (correctness)
live = render()          # bumps calls to 2, computed directly
check("cache equals a live render (byte-identical)", l2 == live)
calls["n"] = 1           # reset the counter so the invalidation checks are clean
cc.invalidate(); call()  # rebuild cache after the manual reset
calls["n"] = 1

# 4) add a skill → signature changes → re-render
time.sleep(0.01)
_mk("gamma", "X")
call()
check("adding a file invalidates → re-render", calls["n"] == 2)

# 5) edit a skill (content/size changes) → re-render
time.sleep(0.01)
_mk("alpha", "Z", body="body CHANGED AND LONGER")
call()
check("editing a file invalidates → re-render", calls["n"] == 3)

# 6) unchanged again → cached
call()
check("stable again after change (cached)", calls["n"] == 3)

# 7) persistence: flush, simulate a fresh process, reload from disk → no re-render
last = call()
cc.flush()
cc._mem = None           # simulate a container restart (fresh process, cold in-memory)
after = call()
check("persisted cache survives restart (no re-render)", calls["n"] == 3)
check("post-restart result identical", after == last)

# 8) signature is metadata-only and stable/º sensitive
sig_a = cc.signature([SK], "*/SKILL.md")
sig_b = cc.signature([SK], "*/SKILL.md")
check("signature stable when nothing changes", sig_a == sig_b and sig_a is not None)
time.sleep(0.01); _mk("delta", "Y")
check("signature changes on add", cc.signature([SK], "*/SKILL.md") != sig_a)

print()
if failures:
    print(f"{len(failures)} FAILED:", ", ".join(failures))
    sys.exit(1)
print("ALL TESTS PASSED")
