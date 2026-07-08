"""Turn workspace bytes (images or PDFs) into display-ready JPEGs so the assistant
can SEE them with its vision — scanned pages, downloaded images, e-mail attachments.

Pillow normalizes + downscales raster images; pypdfium2 rasterizes PDF pages (Apache/
BSD, bundles PDFium, no system libs like poppler). Both ship manylinux cp314 wheels,
verified via uv against the runtime image's Python. Every third-party import is guarded
so a missing optional wheel degrades to a clear message instead of crashing the server.

The heavy lifting lives here (pure, unit-testable); fs_view / scan_document stay thin.
"""
import io

# Pillow is the one image dependency; pypdfium2 is only needed for PDFs. Both are
# import-guarded so the connector still starts (and the text tools keep working) if a
# wheel is ever missing on an exotic platform.
try:
    from PIL import Image, ImageOps
except Exception:  # pragma: no cover - Pillow is a pinned dep, guarded for safety
    Image = None
    ImageOps = None

try:
    import pypdfium2 as _pdfium
except Exception:  # pragma: no cover
    _pdfium = None

# FastMCP's Image helper is the sanctioned way to hand image bytes back from a tool;
# fall back to the raw MCP content block if the import path shifts between versions.
try:
    from fastmcp.utilities.types import Image as _FastMCPImage
except Exception:  # pragma: no cover
    try:
        from fastmcp import Image as _FastMCPImage
    except Exception:
        _FastMCPImage = None

MAX_DIM = 2000          # longest side after downscale — plenty for vision OCR, light payload
JPEG_QUALITY = 82
PDF_RENDER_SCALE = 2.0  # 72 dpi baseline x2 ≈ 144 dpi before the MAX_DIM cap applies
MAX_PAGES = 8           # hard cap on PDF pages rendered in one call


def available() -> bool:
    """True if raster-image viewing (Pillow) is usable."""
    return Image is not None


def _encode(img) -> bytes:
    """Downscale to MAX_DIM (longest side, aspect kept) and return JPEG bytes."""
    if img.mode not in ("RGB", "L"):
        img = img.convert("RGB")
    img.thumbnail((MAX_DIM, MAX_DIM))  # in-place; only ever shrinks
    buf = io.BytesIO()
    img.save(buf, format="JPEG", quality=JPEG_QUALITY, optimize=True)
    return buf.getvalue()


def image_to_jpeg(data: bytes) -> bytes:
    """Normalize any Pillow-readable image to a downscaled JPEG (honours EXIF rotation)."""
    if Image is None:
        raise RuntimeError("Pillow not available")
    img = Image.open(io.BytesIO(data))
    if ImageOps is not None:
        img = ImageOps.exif_transpose(img)  # respect phone/camera orientation
    return _encode(img)


def pdf_to_jpegs(data: bytes, first: int = 0, count: int = MAX_PAGES):
    """Render PDF pages [first, first+count) → (list[jpeg], total_pages, first_used)."""
    if _pdfium is None:
        raise RuntimeError("pypdfium2 not available")
    doc = _pdfium.PdfDocument(data)
    try:
        total = len(doc)
        first = max(0, min(int(first), max(0, total - 1)))
        count = max(1, min(int(count), MAX_PAGES))
        pages = [_encode(doc[i].render(scale=PDF_RENDER_SCALE).to_pil())
                 for i in range(first, min(first + count, total))]
        return pages, total, first
    finally:
        doc.close()


def _is_pdf(data: bytes, suffix: str) -> bool:
    return (suffix or "").lower() == ".pdf" or data[:5] == b"%PDF-"


def render(data: bytes, suffix: str = "", first: int = 0, count: int = 4):
    """Dispatch bytes → (list[jpeg_bytes], note). Empty list + note if not viewable."""
    if _is_pdf(data, suffix):
        if _pdfium is None:
            return [], "PDF viewing needs pypdfium2 (not installed on this server)."
        pages, total, first = pdf_to_jpegs(data, first=first, count=count)
        note = ""
        if total > len(pages):
            note = (f"PDF: page(s) {first + 1}–{first + len(pages)} of {total} "
                    "(call fs_view with page=N for the rest).")
        return pages, note
    if Image is None:
        return [], "Image viewing needs Pillow (not installed on this server)."
    try:
        return [image_to_jpeg(data)], ""
    except Exception as exc:
        return [], f"Not a viewable image/PDF ({type(exc).__name__}). For text use fs_read."


def image_block(jpeg: bytes):
    """Wrap JPEG bytes as an MCP image content block the assistant renders as vision."""
    if _FastMCPImage is not None:
        return _FastMCPImage(data=jpeg, format="jpeg")
    import base64
    from mcp.types import ImageContent
    return ImageContent(type="image",
                        data=base64.b64encode(jpeg).decode("ascii"),
                        mimeType="image/jpeg")


def text_block(text: str):
    """Wrap a short note as an MCP text content block (to accompany images)."""
    from mcp.types import TextContent
    return TextContent(type="text", text=text)
