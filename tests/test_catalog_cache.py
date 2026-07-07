"""Stat-signature catalog cache (v1.9.1).

Proves the bootstrap perf fix: a section is rendered once, then served from cache
WITHOUT re-running the (file-reading) render_fn while the source dir is unchanged;
adding or editing a file invalidates that section; the cache is byte-identical to a
live render; and it survives a process restart (disk-persisted). No MCP server needed.
"""
import importlib
import tempfile
import time
from pathlib import Path


def test_catalog_cache(monkeypatch):
    _D = Path(tempfile.mkdtemp())
    monkeypatch.setenv("CATALOG_CACHE_FILE", str(_D / "cache.json"))
    import catalog_cache as cc
    importlib.reload(cc)

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
    assert calls["n"] == 1, "first call renders (render_fn ran once)"

    # 2) warm call — unchanged dir → served from cache, render_fn NOT re-run
    l2 = call()
    assert calls["n"] == 1, "warm call served from cache (no re-render)"
    assert l1 == l2, "warm result identical to first"

    # 3) cached == a fresh live render (correctness)
    live = render()          # bumps calls to 2, computed directly
    assert l2 == live, "cache equals a live render (byte-identical)"
    calls["n"] = 1           # reset the counter so the invalidation checks are clean
    cc.invalidate(); call()  # rebuild cache after the manual reset
    calls["n"] = 1

    # 4) add a skill → signature changes → re-render
    time.sleep(0.01)
    _mk("gamma", "X")
    call()
    assert calls["n"] == 2, "adding a file invalidates → re-render"

    # 5) edit a skill (content/size changes) → re-render
    time.sleep(0.01)
    _mk("alpha", "Z", body="body CHANGED AND LONGER")
    call()
    assert calls["n"] == 3, "editing a file invalidates → re-render"

    # 6) unchanged again → cached
    call()
    assert calls["n"] == 3, "stable again after change (cached)"

    # 7) persistence: flush, simulate a fresh process, reload from disk → no re-render
    last = call()
    cc.flush()
    cc._mem = None           # simulate a container restart (fresh process, cold in-memory)
    after = call()
    assert calls["n"] == 3, "persisted cache survives restart (no re-render)"
    assert after == last, "post-restart result identical"

    # 8) signature is metadata-only and stable / change-sensitive
    sig_a = cc.signature([SK], "*/SKILL.md")
    sig_b = cc.signature([SK], "*/SKILL.md")
    assert sig_a == sig_b and sig_a is not None, "signature stable when nothing changes"
    time.sleep(0.01); _mk("delta", "Y")
    assert cc.signature([SK], "*/SKILL.md") != sig_a, "signature changes on add"
