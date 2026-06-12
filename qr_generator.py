import qrcode
from PIL import Image


def generate_qr(url: str) -> Image.Image:
    qr = qrcode.QRCode(box_size=10, border=4)
    qr.add_data(url)
    qr.make(fit=True)
    return qr.make_image(fill_color="black", back_color="white").get_image()
