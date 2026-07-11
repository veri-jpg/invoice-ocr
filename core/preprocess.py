import io
from pathlib import Path

import fitz  # PyMuPDF
from PIL import Image

MAX_SIDE = 2000

_MEDIA_TYPES = {
    ".jpg": "image/jpeg",
    ".jpeg": "image/jpeg",
    ".png": "image/png",
    ".webp": "image/webp",
}


def preprocess_file(path: str | Path) -> tuple[bytes, str, int]:
    """Load a JPG/PNG/WEBP/PDF file and return (image_bytes, media_type, page_count)
    ready for the API. page_count is the PDF's total page count (only the first page
    is ever converted); it's always 1 for image files."""
    path = Path(path)
    if not path.exists():
        raise FileNotFoundError(f"File tidak ditemukan: {path}")

    suffix = path.suffix.lower()

    if suffix == ".pdf":
        image, page_count = _pdf_first_page_to_image(path)
    elif suffix in _MEDIA_TYPES:
        image = Image.open(path)
        image.load()
        page_count = 1
    else:
        raise ValueError(f"Format file tidak didukung: {suffix}")

    image = _resize_if_needed(image)
    return _image_to_jpeg_bytes(image), "image/jpeg", page_count


def _pdf_first_page_to_image(path: Path) -> tuple[Image.Image, int]:
    doc = fitz.open(path)
    try:
        if doc.page_count == 0:
            raise ValueError(f"PDF tidak memiliki halaman: {path}")
        page = doc.load_page(0)
        pix = page.get_pixmap(dpi=200)
        return Image.open(io.BytesIO(pix.tobytes("png"))), doc.page_count
    finally:
        doc.close()


def _resize_if_needed(image: Image.Image) -> Image.Image:
    longest_side = max(image.size)
    if longest_side <= MAX_SIDE:
        return image
    scale = MAX_SIDE / longest_side
    new_size = (round(image.width * scale), round(image.height * scale))
    return image.resize(new_size, Image.LANCZOS)


def _image_to_jpeg_bytes(image: Image.Image) -> bytes:
    if image.mode not in ("RGB", "L"):
        image = image.convert("RGB")
    buffer = io.BytesIO()
    image.save(buffer, format="JPEG", quality=90)
    return buffer.getvalue()
