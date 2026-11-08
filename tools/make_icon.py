"""Собирает значки приложения из assets/icon.svg (нужен cairosvg и Pillow)."""
from pathlib import Path

import cairosvg
from PIL import Image

ROOT = Path(__file__).resolve().parent.parent
svg = ROOT / "assets/icon.svg"
cairosvg.svg2png(url=str(svg), write_to=str(ROOT / "assets/icon-1024.png"), output_width=1024, output_height=1024)
src = Image.open(ROOT / "assets/icon-1024.png").convert("RGBA")
big = src.resize((256, 256), Image.LANCZOS)
big.save(ROOT / "assets/icon.png")
# мелкие размеры рисуем из вектора напрямую — так чище, чем уменьшать картинку
frames = []
for s in (16, 24, 32, 48, 64, 128, 256):
    tmp = ROOT / f"assets/_i{s}.png"
    cairosvg.svg2png(url=str(svg), write_to=str(tmp), output_width=s, output_height=s)
    frames.append(Image.open(tmp).convert("RGBA"))
frames[-1].save(ROOT / "assets/icon.ico", sizes=[(f.width, f.height) for f in frames], append_images=frames[:-1])
for s in (16, 24, 32, 48, 64, 128, 256):
    (ROOT / f"assets/_i{s}.png").unlink()
src.resize((64, 64), Image.LANCZOS).save(ROOT / "app/ui/assets/icon.png")
(ROOT / "app/ui/assets/mark.svg").write_text(svg.read_text(encoding="utf-8"), encoding="utf-8")
print("ok")
