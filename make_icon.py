# -*- coding: utf-8 -*-
"""
程序化生成图标：垃圾桶主题。

思路与 monitor-brightness 一致——在 8 倍分辨率上绘制，再用 LANCZOS 缩小，
边缘比直接在小画布上画干净得多。16/20px 会退化，所以单独用简化造型。

直接运行本脚本会生成 icon.ico；build_exe.py 也会调用它。
"""

import io
import struct

from PIL import Image, ImageDraw

SS = 8                        # 超采样倍数
SIZES = [16, 20, 24, 32, 48, 64, 128, 256]
OUT = "icon.ico"

BG = (30, 41, 59, 255)        # 深蓝灰底
BODY = (230, 237, 243, 255)   # 桶身近白
LID = (148, 163, 184, 255)    # 桶盖浅灰
ACCENT = (52, 211, 153, 255)  # 点缀青绿


def draw_icon(size):
    """返回指定边长的 RGBA 图标。"""
    S = size * SS
    img = Image.new("RGBA", (S, S), (0, 0, 0, 0))
    d = ImageDraw.Draw(img)

    d.rounded_rectangle([0, 0, S - 1, S - 1], radius=int(S * 0.22), fill=BG)

    if size <= 20:
        # 小尺寸：只留盖和桶身，并把比例放大，否则缩完糊成一个方块
        d.rectangle([int(S * 0.14), int(S * 0.28),
                     int(S * 0.86), int(S * 0.40)], fill=LID)
        d.polygon([(int(S * 0.19), int(S * 0.45)),
                   (int(S * 0.81), int(S * 0.45)),
                   (int(S * 0.72), int(S * 0.88)),
                   (int(S * 0.28), int(S * 0.88))], fill=BODY)
        return img.resize((size, size), Image.LANCZOS)

    # ---- 提手 ----
    d.rounded_rectangle([int(S * 0.43), int(S * 0.17),
                         int(S * 0.57), int(S * 0.26)],
                        radius=int(S * 0.02), fill=LID)

    # ---- 桶盖 ----
    d.rounded_rectangle([int(S * 0.20), int(S * 0.25),
                         int(S * 0.80), int(S * 0.33)],
                        radius=int(S * 0.025), fill=LID)

    # ---- 桶身（梯形，上宽下窄）----
    d.polygon([(int(S * 0.24), int(S * 0.37)),
               (int(S * 0.76), int(S * 0.37)),
               (int(S * 0.69), int(S * 0.80)),
               (int(S * 0.31), int(S * 0.80))], fill=BODY)

    # ---- 桶身竖纹（挖出底色，形成镂空感）----
    for cx in (0.40, 0.50, 0.60):
        half = int(S * 0.018)
        c = int(S * cx)
        d.polygon([(c - half, int(S * 0.45)), (c + half, int(S * 0.45)),
                   (c + half, int(S * 0.72)), (c - half, int(S * 0.72))],
                  fill=BG)

    # ---- 右下角一点青绿，避免整体太素 ----
    d.ellipse([int(S * 0.70), int(S * 0.70),
               int(S * 0.86), int(S * 0.86)], fill=ACCENT)

    return img.resize((size, size), Image.LANCZOS)


def build_ico(images, path):
    """
    手写 ICO 封装。

    不用 PIL 的 ICO 保存，是因为它只拿主图缩放出各尺寸，
    我们为 16/20px 单独画的简化造型会被丢掉。
    ICO 从 Vista 起允许直接内嵌 PNG，所以自己拼一遍，
    每个尺寸用各自画好的那张图。

    images: [(size, PIL.Image), ...]
    """
    blobs = []
    for size, im in images:
        buf = io.BytesIO()
        im.save(buf, format="PNG", optimize=True)
        blobs.append((size, buf.getvalue()))

    count = len(blobs)
    offset = 6 + 16 * count
    entries = b""
    payload = b""
    for size, blob in blobs:
        dim = 0 if size >= 256 else size   # ICO 里 256 记作 0
        entries += struct.pack("<BBBBHHII", dim, dim, 0, 0, 1, 32,
                               len(blob), offset)
        payload += blob
        offset += len(blob)

    with open(path, "wb") as fh:
        fh.write(struct.pack("<HHH", 0, 1, count) + entries + payload)


def main():
    images = [(s, draw_icon(s)) for s in SIZES]
    build_ico(images, OUT)
    print("已生成 %s，内嵌尺寸：%s" % (OUT, SIZES))


if __name__ == "__main__":
    main()
