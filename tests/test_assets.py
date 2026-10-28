import base64

import pytest

from mailforge.application.assets import image_to_base64_html


def test_image_asset_is_converted_to_bounded_inline_html(tmp_path):
    image = tmp_path / "pixel.png"
    payload = b"synthetic image bytes"
    image.write_bytes(payload)
    html = image_to_base64_html(image)
    encoded = base64.b64encode(payload).decode("ascii")
    assert html == f'<img src="data:image/png;base64,{encoded}" alt="Image" />'


def test_image_asset_rejects_missing_or_non_image_files(tmp_path):
    with pytest.raises(FileNotFoundError):
        image_to_base64_html(tmp_path / "missing.png")
    text = tmp_path / "notes.txt"
    text.write_text("not an image", encoding="utf-8")
    with pytest.raises(ValueError, match="image MIME"):
        image_to_base64_html(text)


def test_image_asset_enforces_explicit_size_bound(tmp_path):
    image = tmp_path / "large.png"
    image.write_bytes(b"1234")
    with pytest.raises(ValueError, match="exceeds"):
        image_to_base64_html(image, max_bytes=3)
