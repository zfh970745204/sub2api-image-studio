"""Prepare compressed website artwork from the reviewed ImageGen outputs."""
from pathlib import Path
import sys
from io import BytesIO

from PIL import Image, ImageOps

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "backend"))
from app.image_ops import finalize_print_extraction

SOURCE = ROOT / "output/imagegen/showcase-v3"
DEST = ROOT / "frontend/public/brand"
DEST.mkdir(exist_ok=True)


def web(image, name, width=1440, quality=85):
    image = ImageOps.exif_transpose(image).copy()
    image.thumbnail((width, width * 2), Image.Resampling.LANCZOS)
    image.save(DEST / f"{name}.webp", quality=quality, method=6)
    preview = image.copy()
    preview.thumbnail((640, 800), Image.Resampling.LANCZOS)
    preview.save(DEST / f"{name}-small.webp", quality=80, method=6)


for place in ("login", "register", "home"):
    with Image.open(SOURCE / f"{place}-studio.png") as img:
        web(img, f"{place}-studio-v3")

shirt_path = SOURCE / "shirt-source.webp"
with Image.open(shirt_path) as img:
    web(img, "shirt-source-v3", 1000)
extracted, metadata = finalize_print_extraction((SOURCE / "shirt-extracted-key.png").read_bytes())
(DEST / "shirt-print-v3.png").write_bytes(extracted)
with Image.open(BytesIO(extracted)) as img:
    web(img, "shirt-print-v3", 1200, 92)
    print("Print alpha:", img.mode, img.getextrema()[-1])
for filename, name in (("mug-source.jpg", "mug-source-v3"), ("mug-commerce.png", "mug-commerce-v3")):
    with Image.open(SOURCE / filename) as img:
        web(img, name, 1200)
print("Website media ready:", DEST)
