"""Unit tests for imaging.py — the raster/PDF → JPEG conversion behind fs_view.

Pillow synthesizes the inputs (a big PNG, a 2-page PDF) so the test needs no fixture
files, then asserts the outputs are valid, downscaled JPEGs — the exact bytes fs_view
hands to the model as vision. PDF cases skip cleanly if pypdfium2 is unavailable.
"""
import importlib
import io

import pytest

pytest.importorskip("PIL")            # skip the whole module if Pillow is missing
imaging = importlib.import_module("imaging")


def _png(w, h, color=(200, 40, 40)) -> bytes:
    from PIL import Image as PImage
    buf = io.BytesIO()
    PImage.new("RGB", (w, h), color).save(buf, format="PNG")
    return buf.getvalue()


def _dims(jpeg: bytes):
    from PIL import Image as PImage
    return PImage.open(io.BytesIO(jpeg)).size


def _two_page_pdf() -> bytes:
    from PIL import Image as PImage
    p1 = PImage.new("RGB", (600, 800), (255, 255, 255))
    p2 = PImage.new("RGB", (600, 800), (10, 10, 10))
    buf = io.BytesIO()
    p1.save(buf, format="PDF", save_all=True, append_images=[p2])
    return buf.getvalue()


def test_image_to_jpeg_downscales_large_image():
    out = imaging.image_to_jpeg(_png(5000, 3000))
    assert out[:2] == b"\xff\xd8"                       # JPEG SOI magic
    w, h = _dims(out)
    assert max(w, h) <= imaging.MAX_DIM                 # capped to MAX_DIM
    assert (w, h) == (imaging.MAX_DIM, int(imaging.MAX_DIM * 3000 / 5000))


def test_image_to_jpeg_keeps_small_image():
    assert _dims(imaging.image_to_jpeg(_png(300, 200))) == (300, 200)  # only shrinks


def test_render_dispatches_raster_image():
    jpegs, note = imaging.render(_png(400, 400), suffix=".png")
    assert note == "" and len(jpegs) == 1
    assert jpegs[0][:2] == b"\xff\xd8"


def test_render_rejects_text_file():
    jpegs, note = imaging.render(b"just text, definitely not an image", suffix=".txt")
    assert jpegs == [] and "fs_read" in note


def test_is_pdf_detects_by_magic_without_suffix():
    assert imaging._is_pdf(b"%PDF-1.7\n...", suffix="") is True
    assert imaging._is_pdf(b"\x89PNG\r\n", suffix="") is False


@pytest.mark.skipif(imaging._pdfium is None, reason="pypdfium2 not installed")
def test_render_pdf_all_pages():
    jpegs, note = imaging.render(_two_page_pdf(), suffix=".pdf", first=0, count=4)
    assert len(jpegs) == 2 and note == ""              # both pages, nothing truncated
    assert all(j[:2] == b"\xff\xd8" for j in jpegs)


@pytest.mark.skipif(imaging._pdfium is None, reason="pypdfium2 not installed")
def test_render_pdf_page_cap_notes_truncation():
    jpegs, note = imaging.render(_two_page_pdf(), suffix=".pdf", first=0, count=1)
    assert len(jpegs) == 1
    assert "of 2" in note                              # tells caller there is more
