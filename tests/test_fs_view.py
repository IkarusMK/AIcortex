"""Tests for fs_view — the workspace image/PDF viewer that hands vision content to the
model. The MCP content-block wrappers (imaging.image_block/text_block) are stubbed, so
these assertions cover fs_view's OWN logic — sandbox, size cap, dispatch, multi-page
note — independent of the FastMCP/MCP layer. WORK_DIR is isolated per test.
"""
import importlib
import io
import tempfile
from pathlib import Path

import pytest

pytest.importorskip("PIL")


class FakeMCP:
    def __init__(self):
        self.tools = {}

    def tool(self, fn):
        self.tools[fn.__name__] = fn
        return fn


def _png(w, h, color=(30, 120, 200)) -> bytes:
    from PIL import Image as PImage
    buf = io.BytesIO()
    PImage.new("RGB", (w, h), color).save(buf, format="PNG")
    return buf.getvalue()


def _two_page_pdf() -> bytes:
    from PIL import Image as PImage
    p1 = PImage.new("RGB", (500, 700), (255, 255, 255))
    p2 = PImage.new("RGB", (500, 700), (0, 0, 0))
    buf = io.BytesIO()
    p1.save(buf, format="PDF", save_all=True, append_images=[p2])
    return buf.getvalue()


def _setup(monkeypatch):
    """Isolate WORK_DIR per test; return (fs_view, fs_write, work_dir)."""
    work = Path(tempfile.mkdtemp()) / "work"
    monkeypatch.setenv("WORK_DIR", str(work))
    import fs_tools
    importlib.reload(fs_tools)                     # picks up the isolated WORK_DIR
    # Stub the content wrappers so the test needs neither fastmcp nor mcp installed.
    monkeypatch.setattr(fs_tools.imaging, "image_block", lambda j: {"image": len(j)})
    monkeypatch.setattr(fs_tools.imaging, "text_block", lambda t: {"note": t})
    mcp = FakeMCP()
    fs_tools.register(mcp)
    return mcp.tools["fs_view"], mcp.tools["fs_write"], work


def test_fs_view_returns_image_for_png(monkeypatch):
    fs_view, _, work = _setup(monkeypatch)
    work.mkdir(parents=True, exist_ok=True)
    (work / "pic.png").write_bytes(_png(400, 300))
    out = fs_view("pic.png")
    assert isinstance(out, dict) and "image" in out       # one stubbed image block


def test_fs_view_rejects_path_escape(monkeypatch):
    fs_view, _, _ = _setup(monkeypatch)
    assert "escapes" in fs_view("../../etc/passwd")


def test_fs_view_missing_file(monkeypatch):
    fs_view, _, _ = _setup(monkeypatch)
    assert "No file" in fs_view("nope.png")


def test_fs_view_text_file_points_to_fs_read(monkeypatch):
    fs_view, fs_write, _ = _setup(monkeypatch)
    fs_write("notes.txt", "plain text, not an image")
    assert "fs_read" in fs_view("notes.txt")


def test_fs_view_size_cap(monkeypatch):
    fs_view, _, work = _setup(monkeypatch)
    work.mkdir(parents=True, exist_ok=True)
    monkeypatch.setattr("fs_tools._VIEW_CEILING", 10)     # tiny cap → refuse
    (work / "big.png").write_bytes(_png(200, 200))
    assert "view cap" in fs_view("big.png")


@pytest.mark.skipif(importlib.import_module("imaging")._pdfium is None,
                    reason="pypdfium2 not installed")
def test_fs_view_pdf_multipage_note_and_blocks(monkeypatch):
    fs_view, _, work = _setup(monkeypatch)
    work.mkdir(parents=True, exist_ok=True)
    (work / "doc.pdf").write_bytes(_two_page_pdf())
    out = fs_view("doc.pdf", page=0, max_pages=1)          # 2-page doc, ask for 1
    assert isinstance(out, list) and len(out) == 2         # [note block, image block]
    assert "note" in out[0] and "of 2" in out[0]["note"]
    assert "image" in out[1]
