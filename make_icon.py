# -*- coding: utf-8 -*-
"""
程序化生成图标：赛博朋克垃圾桶。

Copyright (C) 2026 柯夜 (sickpoet). 保留所有权利。
源码：https://github.com/sickpoet/cdisk-cleaner

配色和界面共用一套：近黑底 + 霓虹青描边 + 品红点缀。
在 8 倍分辨率上绘制再 LANCZOS 缩小，边缘比直接画小图干净得多；
16/20px 会糊，所以单独给一套加粗的简化造型。

直接运行本脚本会生成 icon.ico；build_exe.py 也会调用它。
"""

import io
import struct

from PIL import Image, ImageDraw

SS = 8                        # 超采样倍数
SIZES = [16, 20, 24, 32, 48, 64, 128, 256]
OUT = "icon.ico"

BG = (6, 10, 17, 255)         # 近黑底，和界面 BG 一致
DEEP = (6, 16, 24, 255)       # 桶身内部
NEON = (0, 229, 255, 255)     # 主霓虹青
MAGENTA = (255, 45, 149, 255)  # 强调品红
DIM = (11, 109, 128, 255)     # 暗青
GLOW = (0, 120, 145, 110)     # 外发光（半透明）


def _cut_square(S, pad, cut):
    """切角方形的顶点，切左上与右下——和界面里的按钮同一套语言。"""
    x0, y0, x1, y1 = pad, pad, S - 1 - pad, S - 1 - pad
    return [(x0 + cut, y0), (x1, y0), (x1, y1 - cut),
            (x1 - cut, y1), (x0, y1), (x0, y0 + cut)]


def draw_icon(size):
    """返回指定边长的 RGBA 图标。"""
    S = size * SS
    img = Image.new("RGBA", (S, S), (0, 0, 0, 0))
    d = ImageDraw.Draw(img)

    pad = int(S * 0.02)
    cut = int(S * 0.20)

    if size <= 20:
        # 小尺寸：不画外发光（缩完只会糊成一坨），
        # 盖要明显比桶宽、中间留缝，否则整体会合并成一个方块
        d.polygon(_cut_square(S, pad, cut), fill=BG, outline=NEON,
                  width=max(1, int(S * 0.020)))
        d.rectangle([int(S * 0.18), int(S * 0.26),
                     int(S * 0.82), int(S * 0.375)], fill=NEON)
        d.polygon([(int(S * 0.29), int(S * 0.47)),
                   (int(S * 0.71), int(S * 0.47)),
                   (int(S * 0.62), int(S * 0.83)),
                   (int(S * 0.38), int(S * 0.83))], fill=NEON)
        return img.resize((size, size), Image.LANCZOS)

    # 外发光：比主体大一圈的半透明青，缩小时自然晕开
    d.polygon(_cut_square(S, -int(S * 0.03), cut + int(S * 0.03)), fill=GLOW)
    d.polygon(_cut_square(S, pad, cut), fill=BG, outline=NEON,
              width=max(1, int(S * 0.016)))

    stroke = max(1, int(S * 0.022))

    # 提手
    d.rectangle([int(S * 0.41), int(S * 0.15),
                 int(S * 0.59), int(S * 0.25)], outline=DIM, width=stroke)

    # 桶盖：实心青条，中间压一道暗缝
    d.rectangle([int(S * 0.17), int(S * 0.26),
                 int(S * 0.83), int(S * 0.35)], fill=NEON)
    d.rectangle([int(S * 0.17), int(S * 0.298),
                 int(S * 0.83), int(S * 0.316)], fill=DEEP)

    # 桶身：描边梯形
    d.polygon([(int(S * 0.24), int(S * 0.39)),
               (int(S * 0.76), int(S * 0.39)),
               (int(S * 0.68), int(S * 0.84)),
               (int(S * 0.32), int(S * 0.84))],
              fill=DEEP, outline=NEON, width=stroke)

    # 桶内三条数据线，中间那条用品红点一下
    for cx, col in ((0.40, NEON), (0.50, MAGENTA), (0.60, NEON)):
        c = S * cx
        hw = S * 0.017
        d.polygon([(c - hw, S * 0.47), (c + hw, S * 0.47),
                   (c + hw, S * 0.75), (c - hw, S * 0.75)], fill=col)

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
