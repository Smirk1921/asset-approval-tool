# -*- coding: utf-8 -*-
"""生成演示素材（demo/）。

为什么要生成而不是直接用真实素材：仓库里不能放别人的项目图，
所以这里用 Pillow 画一批**合成的对照素材**，并且故意做出几种典型缺陷，
这样克隆下来就能立刻看到「审批工具到底在审什么」。

用法：
    python demo/generate_demo.py

生成内容：
    demo/original/*.png    源A：英文原图（无缺陷）
    demo/rendered/*.png    源B：中文成品（其中若干张有故意做出来的缺陷）
    demo/manifest.json     卡片清单（卡号/类型/扩展/英文标题）
    demo/texts/source.json 文本源A：英文 OCR 结果
    demo/texts/translated.json 文本源B：中文译文 + 校对状态
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

from PIL import Image, ImageDraw, ImageFont

ROOT = Path(__file__).resolve().parent
W, H = 420, 580

FONT_CANDIDATES = [
    "C:/Windows/Fonts/msyh.ttc",
    "C:/Windows/Fonts/msyhbd.ttc",
    "C:/Windows/Fonts/simsun.ttc",
    "/System/Library/Fonts/PingFang.ttc",
    "/usr/share/fonts/truetype/noto/NotoSansCJK-Regular.ttc",
    "/usr/share/fonts/opentype/noto/NotoSansCJK-Regular.ttc",
]
FONT_EN = ["C:/Windows/Fonts/arial.ttf", "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf"]


def load_font(size: int, bold: bool = False, chinese: bool = True):
    pool = (FONT_CANDIDATES if chinese else FONT_EN)
    if bold and chinese:
        pool = ["C:/Windows/Fonts/msyhbd.ttc"] + pool
    for path in pool:
        if Path(path).exists():
            try:
                return ImageFont.truetype(path, size)
            except OSError:
                continue
    return ImageFont.load_default()


# ---------------------------------------------------------------- 素材内容

# (卡号, 类型, 扩展, 英文标题, 英文正文, 中文标题, 中文正文, 缺陷)
CARDS = [
    ("101", "探索卡", "基础", "Frozen Lake",
     ["A frozen lake stretches before", "you. The ice groans underfoot,", "and something dark moves", "beneath the surface."],
     "冰封之湖", ["一片冰封的湖面展现在你面前。", "脚下的冰层发出呻吟，", "有什么漆黑的东西", "在水面下游动。"], None),

    ("102", "探索卡", "基础", "Ancient Ruins",
     ["Broken pillars rise from the", "sand. Whatever stood here was", "grand once, and is now", "only a warning."],
     "远古废墟", ["断裂的石柱从沙中升起。", "这里曾经矗立着宏伟的建筑，", "如今只剩下一声警告，", "提醒着后来者不要靠近这片", "被遗忘的土地与它沉睡的", "秘密。"], "text_overflow"),

    ("103", "探索卡", "基础", "Trading Post",
     ["A merchant waves you over.", "She has wares from every", "corner of the continent,", "and prices to match."],
     "贸易站", ["商人向你招手。", "她手上有来自大陆各个角落的", "货物，价格也与之相称。"], "missing_icon"),

    ("104", "探索卡", "基础", "Thunder Storm",
     ["Lightning splits the sky.", "The wind tears at your cloak.", "You must find shelter,", "and quickly."],
     "雷暴", ["闪电劈开天空。", "狂风撕扯着你的斗篷。", "你必须尽快找到避难所。"], "title_mixed"),

    ("105", "事件卡", "扩展·冰迷宫", "The Long Night",
     ["The sun does not rise.", "Lanterns burn low, and the", "cold seeps through every", "layer of clothing."],
     "漫漫长夜", ["太阳不再升起。", "灯笼快要燃尽，", "寒气渗透了每一层衣物。"], "text_overlap"),

    ("106", "事件卡", "扩展·冰迷宫", "Frozen Gate",
     ["A gate of blue ice bars", "your way. It is far too", "thick to break, and far", "too cold to climb."],
     "冰封之门", ["一扇蓝冰之门挡住了去路。", "它厚得无法击碎，", "也冷得无法攀爬。"], None),

    ("107", "道具卡", "扩展·禁殿", "Old Compass",
     ["The needle spins without", "rest. It points somewhere", "that is not north, and", "never has been."],
     "古老罗盘", ["指针不停地旋转。", "它指向的某个方向", "并不是北方，也从来不是。"], "black_on_black"),

    ("108", "道具卡", "扩展·禁殿", "Cracked Lantern",
     ["Light spills from the crack", "in its glass. It will not", "last the night, but it", "will last long enough."],
     "裂纹提灯", ["光从玻璃的裂缝中漏出。", "它撑不过整晚，", "但足够撑到你找到下一盏。"], None),
]

TEXT_SLOTS = [
    ("card01", "0", "0", "A frozen lake stretches before you.", "一片冰封的湖面展现在你面前。", "探索卡", "已校对"),
    ("card01", "0", "1", "The ice groans underfoot.", "脚下的冰层发出呻吟。", "探索卡", "已校对"),
    ("card02", "0", "0", "Broken pillars rise from the sand.", "断裂的石柱从沙中升起。", "探索卡", "已校对"),
    ("card02", "0", "1", "Whatever stood here was grand once.", "这里曾经矗立着宏伟的建筑。", "探索卡", "待校对"),
    ("card03", "0", "0", "A merchant waves you over.", "商人向你招手。", "探索卡", "已校对"),
    ("card03", "0", "1", "She has wares from every corner of the continent.", "她手上有来自大陆各个角落的货物。", "探索卡", "已校对"),
    ("card04", "0", "0", "Lightning splits the sky.", "闪电劈开天空。", "探索卡", "待校对"),
    ("card04", "0", "1", "The wind tears at your cloak.", "狂风撕扯着你的斗篷。", "探索卡", "已校对"),
    ("card05", "0", "0", "The sun does not rise.", "太阳不再升起。", "事件卡", "已校对"),
    ("card05", "0", "1", "Lanterns burn low.", "灯笼快要燃尽。", "事件卡", "已校对"),
    ("card06", "0", "0", "A gate of blue ice bars your way.", "一扇蓝冰之门挡住了去路。", "事件卡", "已校对"),
    ("card07", "0", "0", "The needle spins without rest.", "指针不停地旋转。", "道具卡", "待校对"),
]


# ---------------------------------------------------------------- 画图

def draw_card(canvas: Image.Image, title: str, body: list[str], number: str,
              *, chinese: bool, defect: str | None) -> None:
    d = ImageDraw.Draw(canvas)

    # 卡面底色
    d.rounded_rectangle([10, 10, W - 10, H - 10], radius=18, fill=(236, 222, 190), outline=(120, 92, 52), width=3)
    d.rounded_rectangle([22, 22, W - 22, H - 22], radius=12, outline=(170, 140, 96), width=2)

    # 顶部编号
    badge_w, badge_h = 96, 56
    bx = (W - badge_w) // 2
    d.rounded_rectangle([bx, 40, bx + badge_w, 40 + badge_h], radius=10,
                        fill=(214, 176, 92), outline=(110, 82, 40), width=3)
    f_num = load_font(34, bold=True, chinese=chinese)
    tw = d.textlength(number, font=f_num)
    d.text((bx + (badge_w - tw) / 2, 44), number, font=f_num, fill=(48, 34, 12))

    # 标题
    title_size = 30 if chinese else 28
    f_title = load_font(title_size, bold=True, chinese=chinese)
    tw = d.textlength(title, font=f_title)
    if defect == "title_mixed":
        # 缺陷：标题字体混排（一半大一半小、基线错位）
        half = len(title) // 2 or 1
        d.text((W / 2 - tw / 2, 122), title[:half], font=f_title, fill=(60, 40, 16))
        d.text((W / 2 - tw / 2 + d.textlength(title[:half], font=f_title), 130),
               title[half:], font=load_font(20, chinese=chinese), fill=(60, 40, 16))
    else:
        d.text((W / 2 - tw / 2, 122), title, font=f_title, fill=(60, 40, 16))

    # 正文框
    box = [40, 190, W - 40, 470]
    d.rectangle(box, fill=(248, 240, 220), outline=(150, 122, 82), width=2)

    f_body = load_font(19, chinese=chinese)
    line_h = 28
    y = box[1] + 16
    max_lines = int((box[3] - box[1] - 24) / line_h)
    for i, line in enumerate(body):
        if defect == "text_overflow":
            pass                      # 缺陷：不裁剪，直接画出框外
        elif i >= max_lines:
            break                     # 正常：超出就截断
        if defect == "black_on_black":
            fill = (24, 22, 20)       # 缺陷：黑底黑字（这里底色暗、字也黑）
            d.rectangle([box[0] + 2, box[1] + 2, box[2] - 2, box[3] - 2], fill=(38, 34, 32))
        else:
            fill = (52, 44, 34)
        d.text((box[0] + 18, y), line, font=f_body, fill=fill)
        y += line_h

    if defect == "text_overlap":
        # 缺陷：两层文字重叠在一起
        y2 = box[1] + 16 + line_h
        for line in body[1:3]:
            d.text((box[0] + 18, y2), line, font=f_body, fill=(52, 44, 34))
            y2 += line_h

    # 底部图标位
    icon_box = [W - 96, H - 108, W - 44, H - 56]
    d.rectangle(icon_box, outline=(150, 122, 82), width=2)
    if defect == "missing_icon":
        # 缺陷：图标没画上去（只留空框）
        d.text((icon_box[0] + 6, icon_box[1] + 8), "?", font=load_font(24, chinese=chinese), fill=(190, 60, 50))
    else:
        d.ellipse([icon_box[0] + 10, icon_box[1] + 10, icon_box[2] - 10, icon_box[3] - 10],
                  fill=(96, 140, 96), outline=(60, 96, 60), width=2)

    # 卡号小字
    f_small = load_font(15, chinese=chinese)
    d.text((40, H - 96), f"{'编号' if chinese else 'ID'} {number}", font=f_small, fill=(110, 92, 66))


def main() -> int:
    original = ROOT / "original"
    rendered = ROOT / "rendered"
    texts = ROOT / "texts"
    for directory in (original, rendered, texts):
        directory.mkdir(parents=True, exist_ok=True)

    manifest = []
    source_slots: dict[str, dict] = {}
    translated_slots: dict[str, dict] = {}

    for index, (number, kind, expansion, en_title, en_body, zh_title, zh_body, defect) in enumerate(CARDS, 1):
        name = f"{100 + index}_front.png"

        card = Image.new("RGB", (W, H), (86, 74, 58))
        draw_card(card, en_title, en_body, number, chinese=False, defect=None)
        card.save(original / name)

        render = Image.new("RGB", (W, H), (86, 74, 58))
        draw_card(render, zh_title, zh_body, number, chinese=True, defect=defect)
        render.save(rendered / name)

        manifest.append({
            "card_id": 100 + index,
            "卡号": number,
            "类型": kind,
            "扩展": expansion,
            "英文标题": en_title,
            "状态": "已校对(终稿)",
        })

    for card, row, col, en, zh, cat, status in TEXT_SLOTS:
        key = f"{card}|{row}|{col}"
        source_slots[key] = {
            "title": en.split(" ")[0],
            "lines": [{"t": part, "x": 100, "y": 200 + i * 30, "c": 0.98}
                      for i, part in enumerate(en.split(", "))],
        }
        translated_slots[key] = {"zh": zh, "cat": cat, "status": status, "source": "demo|终稿"}

    (ROOT / "manifest.json").write_text(
        json.dumps(manifest, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    (texts / "source.json").write_text(
        json.dumps(source_slots, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    (texts / "translated.json").write_text(
        json.dumps(translated_slots, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")

    defects = sum(1 for c in CARDS if c[7])
    print("生成完成：")
    print(f"  {original.relative_to(ROOT.parent)}/  {len(CARDS)} 张英文原图")
    print(f"  {rendered.relative_to(ROOT.parent)}/  {len(CARDS)} 张中文成品（其中 {defects} 张带故意缺陷）")
    print(f"  demo/manifest.json  {len(manifest)} 条卡片清单")
    print(f"  demo/texts/         {len(TEXT_SLOTS)} 条文本槽位")
    return 0


if __name__ == "__main__":
    sys.exit(main())
