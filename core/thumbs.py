# -*- coding: utf-8 -*-
"""缩略图生成与磁盘缓存。

上千张素材直接加载原图会卡，所以：
  - 缩略图按需生成，落盘缓存，下次直接命中；
  - 缓存文件用「源文件路径 + 修改时间 + 尺寸」做指纹，源图更新后自动失效。
"""
from __future__ import annotations

import hashlib
import os
import threading
import time
from pathlib import Path

from PIL import Image

try:  # Pillow 10+ 把 Image.ANTIALIAS 换成了 Image.Resampling.LANCZOS
    _RESAMPLE = Image.Resampling.LANCZOS
except AttributeError:  # pragma: no cover - 兼容老版本 Pillow
    _RESAMPLE = Image.ANTIALIAS

_locks: dict[str, threading.Lock] = {}
_locks_guard = threading.Lock()

# 输出格式 -> Pillow 认识的格式名
_PIL_FORMAT = {"jpg": "JPEG", "jpeg": "JPEG", "png": "PNG", "webp": "WEBP", "bmp": "BMP"}


def _lock_for(key: str) -> threading.Lock:
    with _locks_guard:
        lock = _locks.get(key)
        if lock is None:
            lock = threading.Lock()
            _locks[key] = lock
        return lock


def _fingerprint(src: Path, size: int, quality: int) -> str:
    try:
        stat = src.stat()
        stamp = f"{stat.st_mtime_ns}:{stat.st_size}"
    except OSError:
        stamp = "missing"
    raw = f"{src}|{stamp}|{size}|{quality}".encode("utf-8", "replace")
    return hashlib.sha1(raw).hexdigest()[:20]


def thumbnail(src: str | os.PathLike, cache_dir: str | os.PathLike,
              size: int, quality: int = 82, fmt: str = "jpg") -> Path:
    """返回缩略图路径（必要时生成）。失败时抛异常，由调用方决定 404/占位。"""
    src_path = Path(src)
    fmt = fmt.lower().lstrip(".")
    pil_format = _PIL_FORMAT.get(fmt, "JPEG")
    out_dir = Path(cache_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    out_path = out_dir / f"{_fingerprint(src_path, size, quality)}.{fmt}"
    if out_path.exists() and out_path.stat().st_size > 0:
        return out_path

    lock = _lock_for(str(out_path))
    with lock:
        if out_path.exists() and out_path.stat().st_size > 0:
            return out_path
        tmp = out_path.with_suffix(f".{os.getpid()}.{int(time.time() * 1000)}.tmp")
        with Image.open(src_path) as im:
            im.load()
            if pil_format == "JPEG":
                if im.mode not in ("RGB", "L"):
                    bg = Image.new("RGB", im.size, (255, 255, 255))
                    if im.mode in ("RGBA", "LA", "P"):
                        rgba = im.convert("RGBA")
                        bg.paste(rgba, mask=rgba.split()[-1])
                    else:
                        bg.paste(im.convert("RGB"))
                    im = bg
                elif im.mode == "L":
                    im = im.convert("RGB")
            im.thumbnail((size, size), _RESAMPLE)
            save_kwargs = {"quality": quality, "optimize": True} if pil_format == "JPEG" else {"optimize": True}
            im.save(tmp, pil_format, **save_kwargs)
        os.replace(tmp, out_path)
    return out_path


def clear_cache(cache_dir: str | os.PathLike) -> int:
    """清空缩略图缓存，返回删除的文件数。"""
    root = Path(cache_dir)
    if not root.exists():
        return 0
    removed = 0
    for path in root.rglob("*"):
        if path.is_file():
            try:
                path.unlink()
                removed += 1
            except OSError:
                pass
    return removed
