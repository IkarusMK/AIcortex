"""The sandboxed workspace (fs_tools.py): list, read, view, write, move, delete, info.

Proves that every tool is confined to WORK_DIR — "..", symlinks that point outside,
a sibling directory sharing the workspace's name prefix and absolute paths never
reach outside files — that size and quota limits refuse before touching disk, that
destructive calls need confirmation, that failures come back as readable messages,
and that a read-only role can look but not change anything.
"""
import asyncio
import io
import json
import os
import sys

import pytest
from fastmcp import Client

ESCAPE = "Path escapes the workspace."


def _raw(server, tool, args=None):
    async def go():
        async with Client(server.mcp) as client:
            return await client.call_tool(tool, args or {}, raise_on_error=False)
    return asyncio.run(go())


def _call(server, tool, args=None):
    res = _raw(server, tool, args)
    return (res.content[0].text if res.content else ""), res.is_error


def _t(server, tool, args=None):
    return _call(server, tool, args)[0]


def _work():
    return sys.modules["fs_tools"].WORK_DIR


@pytest.fixture
def fs(fresh_server):
    _work().mkdir(parents=True, exist_ok=True)
    return fresh_server


@pytest.fixture
def outside(fs):
    """A directory next to the workspace holding a file nobody may reach."""
    sibling = _work().parent / (_work().name + "-evil")      # shares the name prefix
    sibling.mkdir()
    (sibling / "secret.txt").write_text("top secret", encoding="utf-8")
    return sibling


# ── sandbox ──────────────────────────────────────────────────────────
@pytest.mark.parametrize("tool,args", [
    ("fs_list", {"path": "../"}), ("fs_read", {"path": "../x"}),
    ("fs_write", {"path": "a/../../x", "content": "no"}),
    ("fs_move", {"source": "../x", "dest": "y"}), ("fs_move", {"source": "y", "dest": "../x"}),
    ("fs_view", {"path": "../x.png"}),
])
def test_dotdot_is_refused_by_every_tool(fs, tool, args):
    assert _t(fs, tool, args) == ESCAPE


def test_delete_refuses_escapes_and_the_workspace_root(fs):
    for path in ("../x", "", ".", "sub/.."):
        assert _t(fs, "fs_delete", {"path": path, "confirm": True}) == \
            "Refused (path escapes the workspace or is the root)."
    assert _work().is_dir()


def test_symlink_and_prefix_sibling_cannot_reach_outside_files(fs, outside):
    (_work() / "link").symlink_to(outside, target_is_directory=True)
    sibling_rel = "../" + outside.name
    for tool, args in (("fs_list", {"path": "link"}), ("fs_read", {"path": "link/secret.txt"}),
                       ("fs_read", {"path": sibling_rel + "/secret.txt"}),
                       ("fs_write", {"path": "link/new.txt", "content": "planted"}),
                       ("fs_move", {"source": "link/secret.txt", "dest": "stolen.txt"}),
                       ("fs_delete", {"path": "link/secret.txt", "confirm": True})):
        assert _t(fs, tool, args) in (ESCAPE, "Refused (path escapes the workspace or is the root).")
    assert sorted(p.name for p in outside.iterdir()) == ["secret.txt"]
    assert (outside / "secret.txt").read_text(encoding="utf-8") == "top secret"
    assert not (_work() / "stolen.txt").exists()


def test_absolute_paths_are_treated_as_workspace_relative(fs):
    assert _t(fs, "fs_read", {"path": "/etc/passwd"}) == "No file at '/etc/passwd'."
    assert _t(fs, "fs_write", {"path": "/tmp/aicortex-test.txt", "content": "x"}).startswith("Wrote")
    assert (_work() / "tmp" / "aicortex-test.txt").is_file()


def test_a_nul_byte_in_a_path_reads_and_writes_nothing(fs):
    for tool, args in (("fs_read", {"path": "a\x00b"}), ("fs_write", {"path": "a\x00b", "content": "x"})):
        text, err = _call(fs, tool, args)
        assert err or text in (ESCAPE, "No file at 'a\x00b'.") or text.startswith("Could not")
    assert list(_work().iterdir()) == []


# ── write / read ─────────────────────────────────────────────────────
def test_write_creates_parents_appends_and_reads_back_unicode(fs):
    assert _t(fs, "fs_write", {"path": "Ünï/日本.txt", "content": "héllo 🎉"}) == "Wrote 'Ünï/日本.txt' (11B)."
    assert _t(fs, "fs_write", {"path": "Ünï/日本.txt", "content": "!", "append": True}) == \
        "Appended to 'Ünï/日本.txt' (12B)."
    assert _t(fs, "fs_read", {"path": "Ünï/日本.txt"}) == "héllo 🎉!"
    _t(fs, "fs_write", {"path": "Ünï/日本.txt", "content": "new"})
    assert _t(fs, "fs_read", {"path": "Ünï/日本.txt"}) == "new"


def test_write_over_the_per_write_limit_is_refused_before_touching_disk(fs, monkeypatch):
    monkeypatch.setattr(sys.modules["fs_tools"], "_MAX_WRITE", 10)
    out = _t(fs, "fs_write", {"path": "big.txt", "content": "é" * 6})            # 12 bytes
    assert out.startswith("Refused: content is 12B, over the 10B per-write limit")
    assert not (_work() / "big.txt").exists()
    assert _t(fs, "fs_write", {"path": "ok.txt", "content": "é" * 5}).startswith("Wrote")


def test_write_over_the_workspace_quota_is_refused(fs, monkeypatch):
    monkeypatch.setattr(sys.modules["fs_tools"], "_WORKSPACE_QUOTA", 100)
    assert _t(fs, "fs_write", {"path": "a.txt", "content": "a" * 80}).startswith("Wrote")
    out = _t(fs, "fs_write", {"path": "b.txt", "content": "b" * 30})
    assert out.startswith("Refused: workspace is 80B of the 100B quota")
    assert not (_work() / "b.txt").exists()


def test_appending_cannot_push_the_workspace_over_its_quota(fs, monkeypatch):
    monkeypatch.setattr(sys.modules["fs_tools"], "_WORKSPACE_QUOTA", 100)
    _t(fs, "fs_write", {"path": "log.txt", "content": "a" * 60})
    out = _t(fs, "fs_write", {"path": "log.txt", "content": "b" * 60, "append": True})
    assert out.startswith("Refused: workspace") and (_work() / "log.txt").stat().st_size == 60


def test_overwriting_a_file_is_measured_by_its_new_size(fs, monkeypatch):
    monkeypatch.setattr(sys.modules["fs_tools"], "_WORKSPACE_QUOTA", 100)
    _t(fs, "fs_write", {"path": "a.txt", "content": "a" * 80})
    assert _t(fs, "fs_write", {"path": "a.txt", "content": "b" * 50}).startswith("Wrote")


def test_write_failures_come_back_as_messages(fs):
    (_work() / "folder").mkdir()
    (_work() / "plain.txt").write_text("x", encoding="utf-8")
    assert _t(fs, "fs_write", {"path": "folder", "content": "x"}).startswith("Could not write:")
    assert _t(fs, "fs_write", {"path": "plain.txt/child.txt", "content": "x"}).startswith("Could not write:")


def test_read_truncates_at_the_cap_and_reports_what_was_cut(fs, monkeypatch):
    (_work() / "big.txt").write_text("x" * 300_000, encoding="utf-8")
    text = _t(fs, "fs_read", {"path": "big.txt"})
    assert text.startswith("x" * 200_000) and text.endswith("…(truncated; returned 195.3KB of 293.0KB)")
    assert len(text.split("\n")[0]) == 200_000
    monkeypatch.setattr(sys.modules["fs_tools"], "_READ_CEILING", 50)
    assert _t(fs, "fs_read", {"path": "big.txt", "max_bytes": 10**9}).startswith("x" * 50 + "\n…(truncated")
    assert _t(fs, "fs_read", {"path": "big.txt", "max_bytes": 0}).startswith("x\n…(truncated")


def test_read_replaces_invalid_bytes_and_cuts_multibyte_text_safely(fs):
    (_work() / "latin.txt").write_bytes(b"caf\xe9")
    (_work() / "accents.txt").write_text("é" * 10, encoding="utf-8")
    assert _t(fs, "fs_read", {"path": "latin.txt"}) == "caf�"
    cut = _t(fs, "fs_read", {"path": "accents.txt", "max_bytes": 5})
    assert cut.startswith("éé�") and "truncated" in cut


def test_read_of_a_missing_file_or_folder_says_so(fs):
    (_work() / "folder").mkdir(parents=True)
    assert _t(fs, "fs_read", {"path": "nope.txt"}) == "No file at 'nope.txt'."
    assert _t(fs, "fs_read", {"path": "folder"}) == "No file at 'folder'."


@pytest.mark.skipif(os.geteuid() == 0, reason="root ignores file permissions")
def test_an_unreadable_file_is_reported_not_raised(fs):
    locked = _work() / "locked.txt"
    locked.write_text("x", encoding="utf-8")
    locked.chmod(0)
    try:
        assert _t(fs, "fs_read", {"path": "locked.txt"}).startswith("Could not read:")
    finally:
        locked.chmod(0o600)


# ── list / info ──────────────────────────────────────────────────────
def test_list_shows_newest_first_with_sizes_and_folders(fs):
    assert _t(fs, "fs_list") == "(empty)"
    (_work() / "sub").mkdir()
    (_work() / "old.txt").write_text("1234", encoding="utf-8")
    (_work() / "new.txt").write_text("12", encoding="utf-8")
    os.utime(_work() / "old.txt", (1_700_000_000, 1_700_000_000))
    os.utime(_work() / "new.txt", (1_700_000_900, 1_700_000_900))
    os.utime(_work() / "sub", (1_700_001_800, 1_700_001_800))
    assert _t(fs, "fs_list").splitlines() == [
        "- sub/ · — · 2023-11-14 22:43", "- new.txt · 2B · 2023-11-14 22:28", "- old.txt · 4B · 2023-11-14 22:13"]
    assert _t(fs, "fs_list", {"path": "old.txt"}) == "old.txt · 4B · file"
    assert _t(fs, "fs_list", {"path": "ghost"}) == "No such path 'ghost'."
    assert _t(fs, "fs_list", {"path": "sub"}) == "(empty)"


def test_info_summarizes_the_workspace_or_describes_one_path(fs):
    assert _t(fs, "fs_info") == "Workspace /data/work — 0 file(s), 0B total."
    _t(fs, "fs_write", {"path": "d/a.txt", "content": "x" * 2048})
    _t(fs, "fs_write", {"path": "b.txt", "content": "y" * 1024})
    (_work() / "dangling").symlink_to(_work() / "does-not-exist")           # unreadable entry is skipped
    assert _t(fs, "fs_info") == "Workspace /data/work — 2 file(s), 3.0KB total."
    assert _t(fs, "fs_info", {"path": "d/a.txt"}).startswith("d/a.txt · file · 2.0KB · modified 20")
    assert _t(fs, "fs_info", {"path": "d"}).startswith("d · dir · ")
    assert _t(fs, "fs_info", {"path": "ghost"}) == "No such path 'ghost'."
    assert _t(fs, "fs_info", {"path": "../x"}) == "No such path '../x'."
    assert _t(fs, "fs_write", {"path": "c.txt", "content": "z"}).startswith("Wrote")   # _usage skips it too


def test_size_formatting_across_units(fs):
    size = sys.modules["fs_tools"]._size
    assert [size(n) for n in (0, 1023, 1024, 1536, 5 * 1024**2, 3 * 1024**3, 2 * 1024**5)] == [
        "0B", "1023B", "1.0KB", "1.5KB", "5.0MB", "3.0GB", "2048.0TB"]


# ── move / delete ────────────────────────────────────────────────────
def test_move_renames_files_and_folders_and_creates_parents(fs):
    _t(fs, "fs_write", {"path": "a/one.txt", "content": "1"})
    assert _t(fs, "fs_move", {"source": "a/one.txt", "dest": "b/c/two.txt"}) == "Moved 'a/one.txt' → 'b/c/two.txt'."
    assert _t(fs, "fs_read", {"path": "b/c/two.txt"}) == "1" and not (_work() / "a/one.txt").exists()
    assert _t(fs, "fs_move", {"source": "b", "dest": "renamed"}).startswith("Moved")
    assert (_work() / "renamed" / "c" / "two.txt").is_file()
    assert _t(fs, "fs_move", {"source": "ghost", "dest": "x"}) == "No such source 'ghost'."


def test_move_failures_leave_the_workspace_intact(fs, monkeypatch):
    _t(fs, "fs_write", {"path": "keep/f.txt", "content": "1"})
    assert _t(fs, "fs_move", {"source": "keep", "dest": "keep/inside"}).startswith("Move failed:")
    assert _t(fs, "fs_move", {"source": "", "dest": "root-copy"}).startswith("Move failed:")
    assert (_work() / "keep" / "f.txt").is_file()
    monkeypatch.setattr(sys.modules["fs_tools"].shutil, "move",
                        lambda *a: (_ for _ in ()).throw(OSError("disk gone")))
    assert _t(fs, "fs_move", {"source": "keep", "dest": "other"}) == "Move failed: disk gone"


def test_delete_needs_confirmation_for_a_folder_with_content(fs, monkeypatch):
    _t(fs, "fs_write", {"path": "tree/leaf.txt", "content": "1"})
    (_work() / "empty").mkdir()
    refused = _t(fs, "fs_delete", {"path": "tree"})
    assert refused.startswith("Refused: 'tree' is a non-empty folder") and (_work() / "tree" / "leaf.txt").exists()
    assert _t(fs, "fs_delete", {"path": "empty"}) == "Deleted 'empty'."
    assert _t(fs, "fs_delete", {"path": "tree", "confirm": True}) == "Deleted 'tree'."
    assert _t(fs, "fs_delete", {"path": "tree"}) == "No such path 'tree'."
    _t(fs, "fs_write", {"path": "f.txt", "content": "1"})
    assert _t(fs, "fs_delete", {"path": "f.txt"}) == "Deleted 'f.txt'."
    _t(fs, "fs_write", {"path": "g/h.txt", "content": "1"})
    monkeypatch.setattr(sys.modules["fs_tools"].shutil, "rmtree",
                        lambda p: (_ for _ in ()).throw(OSError("busy")))
    assert _t(fs, "fs_delete", {"path": "g", "confirm": True}) == "Delete failed: busy"


# ── viewing ──────────────────────────────────────────────────────────
def test_view_returns_an_image_block_for_a_real_picture(fs):
    PImage = pytest.importorskip("PIL.Image")
    buf = io.BytesIO()
    PImage.new("RGB", (64, 48), (10, 120, 200)).save(buf, format="PNG")
    (_work() / "pic.png").write_bytes(buf.getvalue())
    content = _raw(fs, "fs_view", {"path": "pic.png"}).content
    mime = getattr(content[0], "mime_type", None) or content[0].mimeType     # SDK v1 / v2 field name
    assert len(content) == 1 and content[0].type == "image" and mime == "image/jpeg"


def test_view_reports_unavailable_rendering_and_render_failures(fs, monkeypatch):
    tools = sys.modules["fs_tools"]
    (_work() / "pic.png").write_bytes(b"not really a png")
    monkeypatch.setattr(tools.imaging, "available", lambda: False)
    assert _t(fs, "fs_view", {"path": "pic.png"}) == \
        "Image viewing is unavailable on this server (Pillow not installed)."
    monkeypatch.setattr(tools.imaging, "available", lambda: True)
    monkeypatch.setattr(tools.imaging, "render", lambda *a, **k: (_ for _ in ()).throw(ValueError("boom")))
    assert _t(fs, "fs_view", {"path": "pic.png"}) == "Could not render 'pic.png': ValueError: boom"
    monkeypatch.setattr(tools.imaging, "render", lambda *a, **k: ([], "Needs pypdfium2."))
    assert _t(fs, "fs_view", {"path": "pic.png"}) == "Needs pypdfium2."
    monkeypatch.setattr(tools.imaging, "render", lambda *a, **k: ([b"jpeg"], "page 1 of 2"))
    blocks = _raw(fs, "fs_view", {"path": "pic.png"}).content
    assert [b.type for b in blocks] == ["text", "image"] and blocks[0].text == "page 1 of 2"


# ── roles ────────────────────────────────────────────────────────────
def test_a_read_only_role_can_look_but_not_change_the_workspace(fs, monkeypatch):
    _t(fs, "fs_write", {"path": "shared.txt", "content": "hello"})
    policy = sys.modules["authz"].POLICY_FILE
    policy.parent.mkdir(parents=True, exist_ok=True)
    policy.write_text(json.dumps({"roles": {"vic": "viewer"}}), encoding="utf-8")
    monkeypatch.setattr(sys.modules["authz"], "_identity", lambda: ("vic", False, {}))
    assert _t(fs, "fs_read", {"path": "shared.txt"}) == "hello"
    assert "shared.txt" in _t(fs, "fs_list") and _t(fs, "fs_info").startswith("Workspace")
    for tool, args in (("fs_write", {"path": "x.txt", "content": "no"}), ("fs_delete", {"path": "shared.txt"}),
                       ("fs_move", {"source": "shared.txt", "dest": "y.txt"}), ("fs_view", {"path": "shared.txt"})):
        text, err = _call(fs, tool, args)
        assert err and text.startswith("Denied by policy: role 'viewer'"), tool
    assert (_work() / "shared.txt").read_text(encoding="utf-8") == "hello"
