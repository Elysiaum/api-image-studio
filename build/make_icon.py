# -*- coding: utf-8 -*-
"""生成程序图标 app.ico（多尺寸）。"""
import os

from PIL import Image, ImageDraw

SIZE = 512
ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
OUT = os.path.join(ROOT, "src", "api_image_studio", "assets", "app.ico")


def lerp(a, b, t):
    return tuple(int(a[i] + (b[i] - a[i]) * t) for i in range(3))


def make() -> Image.Image:
    # 渐变背景
    grad = Image.new("RGB", (SIZE, SIZE))
    px = grad.load()
    c1, c2 = (109, 93, 252), (177, 109, 255)      # 靛蓝 -> 紫罗兰
    for y in range(SIZE):
        for x in range(SIZE):
            t = (x / SIZE * 0.45 + y / SIZE * 0.55)
            px[x, y] = lerp(c1, c2, t)

    # 圆角遮罩
    mask = Image.new("L", (SIZE, SIZE), 0)
    ImageDraw.Draw(mask).rounded_rectangle(
        (0, 0, SIZE - 1, SIZE - 1), radius=int(SIZE * 0.23), fill=255
    )

    icon = Image.new("RGBA", (SIZE, SIZE), (0, 0, 0, 0))
    icon.paste(grad, (0, 0), mask)

    d = ImageDraw.Draw(icon)
    s = SIZE / 512.0

    # 白色相框（描边）
    pad, r = 118 * s, 34 * s
    d.rounded_rectangle(
        (pad, pad + 16 * s, SIZE - pad, SIZE - pad + 16 * s),
        radius=r, outline=(255, 255, 255, 245), width=int(26 * s),
    )

    # 太阳（小圆）
    d.ellipse((186 * s, 196 * s, 244 * s, 254 * s), fill=(255, 255, 255, 245))

    # 山形（折线填充）
    d.polygon(
        [
            (140 * s, 358 * s),
            (238 * s, 258 * s),
            (306 * s, 326 * s),
            (348 * s, 292 * s),
            (392 * s, 358 * s),
        ],
        fill=(255, 255, 255, 245),
    )
    # 补一块底，让山形与相框下沿衔接自然
    d.rectangle((140 * s, 352 * s, 392 * s, 372 * s), fill=(255, 255, 255, 245))

    return icon


if __name__ == "__main__":
    os.makedirs(os.path.dirname(OUT), exist_ok=True)
    img = make()
    img.save(
        OUT,
        format="ICO",
        sizes=[(256, 256), (128, 128), (64, 64), (48, 48), (32, 32), (16, 16)],
    )
    preview_dir = os.path.join(ROOT, "output")
    os.makedirs(preview_dir, exist_ok=True)
    png = os.path.join(preview_dir, "_icon_preview.png")
    img.resize((256, 256), Image.LANCZOS).save(png)
    print("saved", os.path.abspath(OUT))
    print("preview", os.path.abspath(png))
