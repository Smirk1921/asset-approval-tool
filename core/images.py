# -*- coding: utf-8 -*-
"""图像处理：把两图拼成一张对照图、按矩形裁剪导出。

为什么要拼图：不少视觉模型一次只吃一张图。源A/源B 分两次看，模型只能各自
「用文字描述」再对比，图标位置、分段间距、字体差异这类信息在描述里会丢。
拼成一张「左原图右成品」，模型一次调用就能直接对比。

代码里不含任何项目专属内容，标签文字由调用方传入。
"""
from __future__ import annotations

import hashlib
from pathlib import Path

from PIL import Image, ImageDraw, ImageFont

try:
    _RESAMPLE = Image.Resampling.LANCZOS
except AttributeError:  # pragma: no cover - 老版本 Pillow
    _RESAMPLE = Image.ANTIALIAS

# 找得到就用系统里能显示中文的字体，找不到就退回 Pillow 自带字体
_FONT_CANDIDATES = [
    "C:/Windows/Fonts/msyh.ttc",
    "C:/Windows/Fonts/msyhbd.ttc",
    "C:/Windows/Fonts/simsun.ttc",
    "/System/Library/Fonts/PingFang.ttc",
    "/usr/share/fonts/truetype/noto/NotoSansCJK-Regular.ttc",
    "/usr/share/fonts/opentype/noto/NotoSansCJK-Regular.ttc",
    "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf",
]
_font_cache: dict[int, ImageFont.ImageFont] = {}


def font(size: int):
    if size in _font_cache:
        return _font_cache[size]
    chosen = None
    for path in _FONT_CANDIDATES:
        if Path(path).exists():
            try:
                chosen = ImageFont.truetype(path, size)
                break
            except OSError:
                continue
    if chosen is None:
        chosen = ImageFont.load_default()
    _font_cache[size] = chosen
    return chosen


def open_rgb(path: str | Path) -> Image.Image:
    image = Image.open(path)
    image.load()
    if image.mode != "RGB":
        if image.mode in ("RGBA", "LA", "P"):
            rgba = image.convert("RGBA")
            background = Image.new("RGB", rgba.size, (255, 255, 255))
            background.paste(rgba, mask=rgba.split()[-1])
            image = background
        else:
            image = image.convert("RGB")
    return image


def compose_pair(
    path_a: str | Path,
    path_b: str | Path,
    *,
    layout: str = "horizontal",
    gap: int = 8,
    max_width: int | None = 2048,
    max_height: int | None = None,
    labels: tuple[str, str] | None = ("A", "B"),
    background: tuple[int, int, int] = (255, 255, 255),
    boxes: tuple[list[dict], list[dict]] | None = None,
) -> Image.Image:
    """把两张图拼成一张对照图。

    关键点是**尺寸归一**：原图和成品分辨率常常不一样（本项目就有 819 和 946 混用），
    不归一会一边大一边小，根本没法比。横向拼接时按较高的那张对齐高度，
    纵向拼接时按较宽的那张对齐宽度——只放大不缩小，尽量不丢细节。
    最后再按 max_width / max_height 整体等比缩一次。

    boxes 用来把已有圈注画上去（[[a侧框], [b侧框]]），
    这样视觉模型能看到「上一轮标过哪里」。
    """
    layout = (layout or "horizontal").lower()
    if layout not in ("horizontal", "vertical"):
        raise ValueError("layout 只能是 horizontal 或 vertical")

    image_a = open_rgb(path_a)
    image_b = open_rgb(path_b)

    if layout == "horizontal":
        target_h = max(image_a.height, image_b.height)
        panels = [_resize_to_height(image_a, target_h), _resize_to_height(image_b, target_h)]
    else:
        target_w = max(image_a.width, image_b.width)
        panels = [_resize_to_width(image_a, target_w), _resize_to_width(image_b, target_w)]

    box_lists = boxes or ([], [])
    if labels or any(box_lists):
        for panel, label, panel_boxes in zip(panels, labels or ("", ""), box_lists):
            if panel_boxes:
                _draw_boxes(panel, panel_boxes)
            if label:
                _draw_label(panel, label)

    if layout == "horizontal":
        width = panels[0].width + gap + panels[1].width
        height = max(panel.height for panel in panels)
    else:
        width = max(panel.width for panel in panels)
        height = panels[0].height + gap + panels[1].height

    canvas = Image.new("RGB", (width, height), background)
    if layout == "horizontal":
        canvas.paste(panels[0], (0, (height - panels[0].height) // 2))
        canvas.paste(panels[1], (panels[0].width + gap, (height - panels[1].height) // 2))
    else:
        canvas.paste(panels[0], ((width - panels[0].width) // 2, 0))
        canvas.paste(panels[1], ((width - panels[1].width) // 2, panels[0].height + gap))

    return _shrink_to_fit(canvas, max_width, max_height)


def _resize_to_height(image: Image.Image, height: int) -> Image.Image:
    if image.height == height:
        return image
    width = max(1, round(image.width * height / image.height))
    return image.resize((width, height), _RESAMPLE)


def _resize_to_width(image: Image.Image, width: int) -> Image.Image:
    if image.width == width:
        return image
    height = max(1, round(image.height * width / image.width))
    return image.resize((width, height), _RESAMPLE)


def _shrink_to_fit(image: Image.Image, max_width: int | None, max_height: int | None) -> Image.Image:
    scale = 1.0
    if max_width and image.width > max_width:
        scale = min(scale, max_width / image.width)
    if max_height and image.height > max_height:
        scale = min(scale, max_height / image.height)
    if scale >= 1.0:
        return image
    return image.resize((max(1, round(image.width * scale)), max(1, round(image.height * scale))),
                        _RESAMPLE)


def _draw_label(image: Image.Image, text: str) -> None:
    """在左上角画一个白底黑字的小标签，不遮挡主要内容。"""
    draw = ImageDraw.Draw(image, "RGBA")
    size = max(14, min(28, image.width // 28))
    fnt = font(size)
    padding = max(4, size // 4)
    try:
        left, top, right, bottom = draw.textbbox((0, 0), text, font=fnt)
        text_w, text_h = right - left, bottom - top
    except (AttributeError, ValueError):  # pragma: no cover
        text_w, text_h = draw.textlength(text, font=fnt), size
    box = [6, 6, 6 + text_w + padding * 2, 6 + text_h + padding * 2]
    draw.rectangle(box, fill=(255, 255, 255, 220), outline=(120, 120, 120, 220))
    draw.text((box[0] + padding, box[1] + padding - top), text, font=fnt, fill=(20, 20, 20))


def _draw_boxes(image: Image.Image, boxes: list[dict]) -> None:
    """把相对坐标（0~1）的矩形画到图上（只画，不改数据）。"""
    if not boxes:
        return
    width, height = image.size
    draw = ImageDraw.Draw(image, "RGBA")
    line = max(2, min(6, width // 200))
    for index, box in enumerate(boxes, 1):
        try:
            x, y = float(box.get("x", 0)), float(box.get("y", 0))
            w, h = float(box.get("w", 0)), float(box.get("h", 0))
        except (TypeError, ValueError):
            continue
        left, top = x * width, y * height
        right, bottom = (x + w) * width, (y + h) * height
        draw.rectangle([left, top, right, bottom], outline=(255, 196, 0, 255), width=line)
        note = str(box.get("note") or "").strip()
        if note:
            fnt = font(max(12, min(22, width // 40)))
            text = f"#{index} {note}"
            try:
                tb = draw.textbbox((0, 0), text, font=fnt)
                text_w, text_h = tb[2] - tb[0], tb[3] - tb[1]
            except (AttributeError, ValueError):  # pragma: no cover
                text_w, text_h = draw.textlength(text, font=fnt), 14
            # 给说明文字垫一层底，压在卡面文字上也能看清
            label_top = top - text_h - 8
            if label_top < 0:
                label_top = bottom + 2
            draw.rectangle([left, label_top, left + text_w + 8, label_top + text_h + 6],
                           fill=(255, 196, 0, 235))
            draw.text((left + 4, label_top + 3), text, font=fnt, fill=(30, 24, 0))
    return None


def crop_region(
    path: str | Path,
    box: dict,
    out_path: str | Path,
    *,
    padding: int = 0,
    scale: float = 1.0,
    side_label: str = "",
) -> tuple[Path, tuple[int, int, int, int]]:
    """按相对坐标裁一块出来（圈注框 → 独立小图，可直接当抠图素材）。

    返回 (输出路径, 实际像素矩形)。
    """
    image = open_rgb(path)
    width, height = image.size
    left = round(float(box.get("x", 0)) * width) - padding
    top = round(float(box.get("y", 0)) * height) - padding
    right = round((float(box.get("x", 0)) + float(box.get("w", 0))) * width) + padding
    bottom = round((float(box.get("y", 0)) + float(box.get("h", 0))) * height) + padding
    left, top = max(0, left), max(0, top)
    right, bottom = min(width, max(left + 1, right)), min(height, max(top + 1, bottom))

    crop = image.crop((left, top, right, bottom))
    if scale and scale != 1.0:
        crop = crop.resize((max(1, round(crop.width * scale)), max(1, round(crop.height * scale))),
                           _RESAMPLE)
    out = Path(out_path)
    out.parent.mkdir(parents=True, exist_ok=True)
    crop.save(out, "PNG")
    return out, (left, top, right, bottom)


def option_hash(**options) -> str:
    """把拼接参数压成短哈希，拼进缓存文件名（参数变了缓存自动失效）。"""
    raw = "|".join(f"{k}={options[k]}" for k in sorted(options))
    return hashlib.sha1(raw.encode("utf-8")).hexdigest()[:10]


def safe_name(text: str, limit: int = 24) -> str:
    """把说明文字变成能当文件名的片段。"""
    cleaned = "".join(ch for ch in str(text) if ch not in '\\/:*?"<>|\r\n\t').strip()
    cleaned = cleaned.replace(" ", "_")
    return cleaned[:limit] or "crop"
