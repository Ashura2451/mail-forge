"""Bounded local content-asset helpers."""

from __future__ import annotations

import base64
import mimetypes
from pathlib import Path

MAX_INLINE_IMAGE_BYTES = 10 * 1024 * 1024


def image_to_base64_html(image_path: str | Path, *, max_bytes: int = MAX_INLINE_IMAGE_BYTES) -> str:
    """Return a data-URI image tag for a regular local image file."""

    path = Path(image_path)
    if not path.is_file():
        raise FileNotFoundError(path)
    if max_bytes < 1:
        raise ValueError("max_bytes must be positive")
    mime_type, _ = mimetypes.guess_type(path.name)
    if not mime_type or not mime_type.startswith("image/"):
        raise ValueError(f"could not determine an image MIME type: {path}")
    with path.open("rb") as handle:
        payload = handle.read(max_bytes + 1)
    if len(payload) > max_bytes:
        raise ValueError(f"image exceeds {max_bytes} bytes: {path}")
    encoded = base64.b64encode(payload).decode("ascii")
    return f'<img src="data:{mime_type};base64,{encoded}" alt="Image" />'
