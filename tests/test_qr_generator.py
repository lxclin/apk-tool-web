import pytest
from PIL import Image


def test_generate_qr_returns_image():
    from qr_generator import generate_qr
    result = generate_qr("https://play.google.com/store/apps/details?id=com.test.app")
    assert isinstance(result, Image.Image)


def test_generate_qr_non_empty():
    from qr_generator import generate_qr
    result = generate_qr("https://play.google.com/store/apps/details?id=com.test.app")
    assert result.size[0] > 0 and result.size[1] > 0


def test_generate_qr_different_urls_produce_different_images():
    from qr_generator import generate_qr
    qr1 = generate_qr("https://play.google.com/store/apps/details?id=com.app.a")
    qr2 = generate_qr("https://play.google.com/store/apps/details?id=com.app.b")
    assert qr1.tobytes() != qr2.tobytes()
