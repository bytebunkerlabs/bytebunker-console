#!/usr/bin/env python3
"""Draw the app icon (a dark rack with three green-lit units) and write
icon.png, icon.icns and icon.ico next to this file. Dev-time only: the
outputs are committed, so builds need no Pillow."""
import os
from PIL import Image, ImageDraw

HERE = os.path.dirname(os.path.abspath(__file__))
S = 1024
img = Image.new("RGBA", (S, S), (0, 0, 0, 0))
d = ImageDraw.Draw(img)
pad = 92
d.rounded_rectangle([pad, pad, S - pad, S - pad], radius=190, fill=(19, 18, 17, 255))
d.rounded_rectangle([pad + 6, pad + 6, S - pad - 6, S - pad - 6], radius=184, outline=(52, 49, 44, 255), width=6)
green, dim, slot = (95, 190, 131, 255), (54, 104, 74, 255), (35, 33, 32, 255)
left, right = 250, S - 250
top, h, gap = 286, 118, 54
for i in range(3):
    y = top + i * (h + gap)
    d.rounded_rectangle([left, y, right, y + h], radius=30, fill=slot)
    d.rounded_rectangle([left + 26, y + 40, left + 26 + 260, y + h - 40], radius=19, fill=green if i != 1 else dim)
    for k in range(3):
        cx = right - 58 - k * 52
        d.ellipse([cx - 15, y + h // 2 - 15, cx + 15, y + h // 2 + 15], fill=green if (i + k) % 2 == 0 else dim)
img.save(os.path.join(HERE, "icon.png"))
img.save(os.path.join(HERE, "icon.icns"))
img.save(os.path.join(HERE, "icon.ico"), sizes=[(16, 16), (24, 24), (32, 32), (48, 48), (64, 64), (128, 128), (256, 256)])
print("wrote icon.png, icon.icns, icon.ico")
