"""
PDF → PNG rasterization for deck vision analysis.

Uses pymupdf (fitz) which is fast, pure-Python, and produces good-quality
images for Claude Vision at moderate DPI. 150 DPI is the sweet spot —
readable chart labels + manageable file sizes.

Outputs are base64-encoded PNG strings (ready for Anthropic API's image
content blocks). Each page is ~150-300KB.

Caches rasterized images alongside the cached PDFs so re-runs skip
rasterization entirely.
"""

from __future__ import annotations

import base64
import hashlib
from dataclasses import dataclass
from pathlib import Path

import fitz  # pymupdf


# --------------------------------------------------------------------------
# Rasterization
# --------------------------------------------------------------------------

@dataclass
class PageImage:
    """One rasterized page."""
    page_number: int                # 1-indexed
    data_base64: str                # base64-encoded bytes (no data: prefix)
    media_type: str = "image/jpeg"   # "image/jpeg" or "image/png"
    byte_size: int = 0               # approx size of underlying encoded bytes
    width_px: int = 0
    height_px: int = 0

    # Back-compat alias — earlier code referenced .png_base64
    @property
    def png_base64(self) -> str:
        return self.data_base64

    def to_anthropic_image_block(self) -> dict:
        """Return an Anthropic API image content block."""
        return {
            "type": "image",
            "source": {
                "type": "base64",
                "media_type": self.media_type,
                "data": self.data_base64,
            },
        }


def rasterize_pdf(pdf_bytes: bytes, *, dpi: int = 150,
                  image_format: str = "jpeg",
                  jpeg_quality: int = 85,
                  verbose: bool = False) -> list[PageImage]:
    """
    Convert PDF bytes to a list of rasterized page images.

    Default: JPEG @ 85% quality @ 150 DPI. This produces ~100-200KB/page
    and readable text + chart labels. At 56 pages that's ~8-12MB total
    for a full vision request — comfortably inside Anthropic's ~30MB
    request-body limit.

    PNG produces crisper text but is 5-6x larger — avoid unless you have
    a very short deck (<10 pages).

    Args:
        pdf_bytes: raw PDF content
        dpi: rendering resolution (150 = sweet spot)
        image_format: "jpeg" (default) or "png"
        jpeg_quality: 1-100 (85 is a good trade-off)
        verbose: log per-page stats
    """
    if not pdf_bytes or not pdf_bytes[:5].startswith(b"%PDF"):
        if verbose:
            print(f"  [RASTER] not a valid PDF")
        return []

    fmt = image_format.lower()
    if fmt not in ("jpeg", "jpg", "png"):
        raise ValueError(f"unsupported image_format: {image_format}")
    if fmt == "jpg":
        fmt = "jpeg"
    media_type = f"image/{fmt}"

    try:
        doc = fitz.open(stream=pdf_bytes, filetype="pdf")
    except Exception as e:
        if verbose:
            print(f"  [RASTER] open failed: {type(e).__name__}: {e}")
        return []

    # pymupdf uses zoom matrices — 72 DPI is 1.0; our target is dpi/72
    zoom = dpi / 72.0
    matrix = fitz.Matrix(zoom, zoom)

    pages: list[PageImage] = []
    for i, page in enumerate(doc):
        try:
            pix = page.get_pixmap(matrix=matrix, alpha=False)
            if fmt == "jpeg":
                encoded = pix.tobytes("jpeg", jpg_quality=jpeg_quality)
            else:
                encoded = pix.tobytes("png")
        except Exception as e:
            if verbose:
                print(f"  [RASTER] page {i+1} failed: {type(e).__name__}: {e}")
            continue
        b64 = base64.b64encode(encoded).decode("ascii")
        pages.append(PageImage(
            page_number=i + 1,
            data_base64=b64,
            media_type=media_type,
            byte_size=len(encoded),
            width_px=pix.width,
            height_px=pix.height,
        ))
        if verbose and (i == 0 or (i + 1) % 20 == 0):
            print(f"  [RASTER] page {i+1}/{len(doc)}: "
                  f"{pix.width}x{pix.height}, {len(encoded):,}B ({fmt})")

    doc.close()
    if verbose:
        total = sum(p.byte_size for p in pages)
        print(f"  [RASTER] {len(pages)} pages rasterized, "
              f"{total/1e6:.1f}MB total @ {dpi} DPI ({fmt})")
    return pages


# --------------------------------------------------------------------------
# Disk cache for rasterized pages (optional — pages are cheap to re-raster
# but caching saves ~10s per deck on repeat analysis)
# --------------------------------------------------------------------------

CACHE_DIR = Path("data/deck_images")


def _cache_dir(pdf_content_hash: str) -> Path:
    return CACHE_DIR / pdf_content_hash


def rasterize_pdf_cached(pdf_bytes: bytes, *, dpi: int = 150,
                         image_format: str = "jpeg",
                         jpeg_quality: int = 85,
                         verbose: bool = False) -> list[PageImage]:
    """
    Like rasterize_pdf but caches encoded images on disk keyed on PDF
    content hash + DPI + format. Cache extension matches the format so
    regenerating with a different format auto-invalidates.
    """
    fmt = image_format.lower()
    if fmt == "jpg":
        fmt = "jpeg"
    ext = "jpg" if fmt == "jpeg" else "png"
    media_type = f"image/{fmt}"

    h = hashlib.sha256(pdf_bytes).hexdigest()[:16] + f"_d{dpi}_{fmt}"
    cdir = _cache_dir(h)
    if cdir.exists():
        pages = []
        files = sorted(cdir.glob(f"page_*.{ext}"),
                       key=lambda p: int(p.stem.split("_")[1]))
        for f in files:
            try:
                data = f.read_bytes()
            except Exception:
                continue
            pages.append(PageImage(
                page_number=int(f.stem.split("_")[1]),
                data_base64=base64.b64encode(data).decode("ascii"),
                media_type=media_type,
                byte_size=len(data),
                width_px=0, height_px=0,
            ))
        if pages:
            if verbose:
                print(f"  [RASTER] CACHE HIT {h} ({len(pages)} pages)")
            return pages

    pages = rasterize_pdf(pdf_bytes, dpi=dpi, image_format=fmt,
                          jpeg_quality=jpeg_quality, verbose=verbose)
    if not pages:
        return pages

    cdir.mkdir(parents=True, exist_ok=True)
    for p in pages:
        raw = base64.b64decode(p.data_base64)
        (cdir / f"page_{p.page_number:03d}.{ext}").write_bytes(raw)
    if verbose:
        print(f"  [RASTER] cached to {cdir}")
    return pages
