#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""给 README 画几张示意图（不搬游戏截图：游戏美术版权归厂商，开发机截图还带个人桌面信息）。

产出（默认写到 dist/<包>/docs/images/，由 tools/make_release.py 调用）：
  panel.png        控制面板长什么样（按钮 + 日志，日志内容照抄真实输出）
  flow.png         三步流程：停在关卡列表 → 自动读题填格 → 过关回列表
  list-modes.png   两种关卡列表（普通谜题卡片 / 特别谜题方块阵）与识别判据

用法：
  python tools/make_readme_images.py <输出目录>
"""
from __future__ import annotations

import sys
from pathlib import Path

from PIL import Image, ImageDraw, ImageFont

W = 1000
BG = (255, 255, 255)
INK = (36, 41, 47)
GRAY = (110, 118, 129)
LINE = (208, 215, 222)
BLUE = (9, 105, 218)
GREEN = (26, 127, 55)
FILL = (36, 216, 192)          # 脚本填色（游戏里的青）
CROSS = (146, 140, 153)        # 游戏/脚本的叉（灰紫）
PLATE = (187, 187, 187)        # 关卡列表上没解开的灰挡板
CARD = (255, 248, 197)         # 普通谜题列表的浅黄卡片
ACCENT = (255, 245, 214)

FONTS = [r"C:\Windows\Fonts\msyh.ttc", r"C:\Windows\Fonts\msyhbd.ttc",
         r"C:\Windows\Fonts\simhei.ttf", r"C:\Windows\Fonts\simsun.ttc"]


def font(size: int, bold: bool = False):
    for p in ([FONTS[1], FONTS[0]] if bold else FONTS):
        try:
            return ImageFont.truetype(p, size)
        except Exception:
            continue
    return ImageFont.load_default()


def text(d: ImageDraw.ImageDraw, xy, s, size=20, color=INK, bold=False, anchor=None):
    d.text(xy, s, font=font(size, bold), fill=color, anchor=anchor)


def rounded(d, box, r=10, fill=None, outline=None, width=1):
    d.rounded_rectangle(box, radius=r, fill=fill, outline=outline, width=width)


def panel_image() -> Image.Image:
    img = Image.new("RGB", (W, 630), BG)
    d = ImageDraw.Draw(img)
    text(d, (28, 22), "Miku 数织自动闯关 · 控制面板", 30, INK, bold=True)
    text(d, (28, 62), "本地面板：点按钮启停、实时看日志；也可以不用图形界面，直接跑命令行", 19, GRAY)

    y = 108
    rounded(d, (28, y, 972, y + 132), 12, fill=(246, 248, 250), outline=LINE)
    text(d, (48, y + 16), "模式", 19, GRAY, bold=True)
    labels = [("连续闯关", "primary"), ("跑一关", "plain"), ("停止", "danger")]
    x = 48
    for name, kind in labels:
        w = 132 if kind == "primary" else 116
        fill = {"primary": (23, 134, 74), "plain": (255, 255, 255), "danger": (255, 255, 255)}[kind]
        outline = {"primary": None, "plain": LINE, "danger": (207, 34, 46)}[kind]
        fg = {"primary": (255, 255, 255), "plain": INK, "danger": (207, 34, 46)}[kind]
        rounded(d, (x, y + 46, x + w, y + 90), 8, fill=fill, outline=outline)
        text(d, (x + w / 2, y + 68), name, 20, fg, bold=True, anchor="mm")
        x += w + 14
    # 点击间隔：画个真的输入框，别只写文字
    rounded(d, (x + 10, y + 52, x + 108, y + 84), 6, fill=(255, 255, 255), outline=LINE)
    text(d, (x + 22, y + 68), "80", 18, INK, anchor="lm")
    text(d, (x + 114, y + 68), "ms 点击间隔", 17, GRAY, anchor="lm")
    # 复选框（手绘方块 + 勾，避免字体缺 ☑ 变豆腐块）
    for i, (label, checked) in enumerate((("打叉空隙", True), ("隐藏控制台", False))):
        bx = 48 + i * 200
        d.rectangle([bx, y + 102, bx + 16, y + 118], outline=(140, 148, 158), width=2,
                    fill=(23, 134, 74) if checked else (255, 255, 255))
        if checked:
            d.line([bx + 4, y + 110, bx + 7, y + 114], fill=(255, 255, 255), width=2)
            d.line([bx + 7, y + 114, bx + 13, y + 105], fill=(255, 255, 255), width=2)
        text(d, (bx + 24, y + 110), label, 16, GRAY, anchor="lm")
    text(d, (48, y + 134), "自动一关接一关（含特别谜题）", 16, GRAY)

    y = 292
    rounded(d, (28, y, 972, y + 292), 12, fill=(13, 17, 23), outline=None)
    text(d, (48, y + 14), "日志", 18, (139, 148, 158), bold=True)
    log = [
        ("窗口：HatsuneMikuLogicPaintS", GRAY),
        ("关卡列表布局：自动识别（普通谜题黄卡片 / 特别谜题灰方块阵）", (139, 148, 158)),
        ("[布局] 当前画面：特别谜题（5×5 灰「?」方块阵）", BLUE),
        ("  已回到关卡列表（特别谜题方块阵），点开这一格…", (201, 209, 217)),
        ("> 识别提示数字…", (201, 209, 217)),
        ("  识别成功：15×15，行提示和=115，列提示和=115", GREEN),
        ("> 涂第 1/5 批（蛇形铺满 24 格）…", (201, 209, 217)),
        ("  自查通过（已涂 24 格，游戏那边的提示灰度对得上）", GREEN),
        ("  填格完成：点击 111 次，用时 8.25s", (201, 209, 217)),
        ("循环结束：成功 1 关，异常 0 关，共填 111 格，总用时 1.5 分钟", (201, 209, 217)),
    ]
    yy = y + 44
    for s, c in log:
        text(d, (48, yy), s, 17, c)
        yy += 26
    return img


def flow_image() -> Image.Image:
    img = Image.new("RGB", (W, 400), BG)
    d = ImageDraw.Draw(img)
    text(d, (28, 20), "它自己怎么跑完一关（全自动，不用你动手）", 26, INK, bold=True)

    # ① 关卡列表
    x0, y0 = 28, 92
    rounded(d, (x0, y0, x0 + 300, y0 + 240), 12, fill=(246, 248, 250), outline=LINE)
    text(d, (x0 + 16, y0 + 12), "① 停在关卡列表", 20, INK, bold=True)
    text(d, (x0 + 16, y0 + 40), "两种列表它都认得", 16, GRAY)
    for r in range(2):
        for c in range(4):
            bx, by = x0 + 20 + c * 68, y0 + 72 + r * 68
            if r == 0 and c == 0:
                rounded(d, (bx, by, bx + 56, by + 56), 6, fill=PLATE)
                text(d, (bx + 28, by + 28), "?", 26, (240, 240, 240), anchor="mm")
            elif r == 1 and c == 3:
                rounded(d, (bx, by, bx + 56, by + 56), 6, fill=CARD, outline=(214, 200, 130))
                text(d, (bx + 28, by + 28), "?", 22, (120, 110, 60), anchor="mm")
            else:
                rounded(d, (bx, by, bx + 56, by + 56), 6, fill=CARD, outline=(214, 200, 130))
    text(d, (x0 + 20, y0 + 208), "灰「?」方块 = 特别谜题", 15, GRAY)
    text(d, (x0 + 20, y0 + 226), "浅黄卡片 = 普通谜题", 15, GRAY)

    # ② 关卡内
    x1 = x0 + 340
    rounded(d, (x1, y0, x1 + 300, y0 + 240), 12, fill=(246, 248, 250), outline=LINE)
    text(d, (x1 + 16, y0 + 12), "② 读题 → 求解 → 填格", 20, INK, bold=True)
    text(d, (x1 + 16, y0 + 40), "分批填 + 每批自查", 16, GRAY)
    cell = 34
    ox, oy = x1 + 62, y0 + 74
    for c in range(5):
        text(d, (ox + c * cell + cell / 2, oy - 14), str([1, 3, 2, 4, 1][c]), 15, BLUE, anchor="mm")
    for r in range(4):
        text(d, (ox - 16, oy + r * cell + cell / 2), str([2, 1, 3, 2][r]), 15, BLUE, anchor="mm")
        for c in range(5):
            bx, by = ox + c * cell, oy + r * cell
            d.rectangle([bx, by, bx + cell, by + cell], fill=(230, 250, 255), outline=(190, 200, 210))
            if (r * 5 + c) % 3 == 0:
                d.rectangle([bx + 3, by + 3, bx + cell - 3, by + cell - 3], fill=FILL)
            elif (r + c) % 4 == 1:
                d.line([bx + 9, by + 9, bx + cell - 9, by + cell - 9], fill=CROSS, width=3)
                d.line([bx + cell - 9, by + 9, bx + 9, by + cell - 9], fill=CROSS, width=3)
    text(d, (x1 + 20, y0 + 208), "青色=该涂   灰紫叉=该空", 15, GRAY)
    text(d, (x1 + 20, y0 + 226), "提示数字变灰 = 这一行/列对了", 15, GRAY)

    # ③ 过关
    x2 = x1 + 340
    rounded(d, (x2, y0, x2 + 300, y0 + 240), 12, fill=(246, 248, 250), outline=LINE)
    text(d, (x2 + 16, y0 + 12), "③ 过关 → 自动下一关", 20, INK, bold=True)
    text(d, (x2 + 16, y0 + 40), "读不通的关会跳过", 16, GRAY)
    rounded(d, (x2 + 40, y0 + 96, x2 + 260, y0 + 168), 10, fill=(13, 17, 23))
    text(d, (x2 + 150, y0 + 132), "关卡完成", 26, (255, 214, 102), bold=True, anchor="mm")
    text(d, (x2 + 20, y0 + 188), "① 点「继续」/菜单退出", 15, GRAY)
    text(d, (x2 + 20, y0 + 206), "② 回到方块阵，换下一格", 15, GRAY)
    text(d, (x2 + 20, y0 + 224), "③ 读不通 → 记跳过表打别的", 15, GRAY)
    return img


def list_modes_image() -> Image.Image:
    img = Image.new("RGB", (W, 486), BG)
    d = ImageDraw.Draw(img)
    text(d, (28, 20), "两种关卡列表，脚本自己认（不用你切换）", 26, INK, bold=True)

    # 普通谜题
    rounded(d, (28, 84, 476, 372), 12, fill=(252, 251, 245), outline=LINE)
    text(d, (48, 98), "普通谜题：浅黄卡片 5×3", 21, INK, bold=True)
    for r in range(3):
        for c in range(5):
            bx, by = 52 + c * 78, 140 + r * 70
            rounded(d, (bx, by, bx + 66, by + 58), 6, fill=CARD, outline=(220, 205, 140))
            text(d, (bx + 33, by + 29), "Lv%d-%d" % (r + 1, c + 1), 13, (120, 110, 60), anchor="mm")
    text(d, (48, 348), "判据：整屏浅黄占比高（实测 0.35 以上）", 15, GRAY)

    # 特别谜题
    rounded(d, (524, 84, 972, 372), 12, fill=(252, 251, 245), outline=LINE)
    text(d, (544, 98), "特别谜题：5×5 灰「?」方块阵", 21, INK, bold=True)
    for r in range(5):
        for c in range(5):
            bx, by = 560 + c * 58, 132 + r * 58
            solved = (r == 0 and c >= 3) or (r == 1 and c >= 4)
            rounded(d, (bx, by, bx + 50, by + 50), 4,
                    fill=(214, 210, 232) if solved else PLATE,
                    outline=(160, 160, 170))
            if not solved:
                text(d, (bx + 25, by + 25), "?", 22, (245, 245, 245), anchor="mm")
    text(d, (544, 340), "判据：格线明显 + 最弱那条 >11 + 检不出棋盘 + 有灰挡板", 15, GRAY)
    text(d, (544, 360), "（四个条件一起看，认不出就不动手）", 14, GRAY)

    text(d, (28, 396), "两条判据都不满足时，它一个字都不点（宁可停着，也不把棋盘涂花）。", 17, INK)
    text(d, (28, 424), "日志里会出现 [布局] 当前画面：… 一行，布局一变就报，能直接看出它现在按哪种关卡在打。", 15, GRAY)
    return img


def main() -> int:
    out = Path(sys.argv[1]) if len(sys.argv) > 1 else Path("docs/images")
    out.mkdir(parents=True, exist_ok=True)
    for name, maker in (("panel.png", panel_image), ("flow.png", flow_image),
                        ("list-modes.png", list_modes_image)):
        p = out / name
        maker().save(p)
        print("  %s（%d 字节）" % (p, p.stat().st_size))
    return 0


if __name__ == "__main__":
    sys.exit(main())
