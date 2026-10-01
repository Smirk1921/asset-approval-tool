# -*- coding: utf-8 -*-
"""配置加载与校验。

配置文件 (config.json) 声明：
  - 全局项：服务端口、结果/缓存目录、问题标签、快捷键、缩略图尺寸
  - sets：若干「素材集」，每个素材集 = 名称 + 类型 + 源A + 源B + (可选)元数据来源

设计原则：路径一律来自配置，代码中不出现任何具体项目的路径。
"""
from __future__ import annotations

import copy
import json
import os
from pathlib import Path

# ---------------------------------------------------------------- 默认值

DEFAULT_TAGS = [
    "标题混排", "字体错", "黑底黑字", "图标缺失", "图标误抹",
    "分段错", "文字溢出", "术语错", "图标错位", "背景残留", "其它",
]

DEFAULT_SHORTCUTS = {
    "next": ["ArrowRight", "d"],
    "prev": ["ArrowLeft", "a"],
    "pass": ["1"],
    "reject": ["2"],
    "flag": ["3"],
    "clear": ["0"],
    "wall": ["g"],
    "detail": ["v"],
    "annotate": ["r"],
    "overlay": ["x"],
    "fit": ["f"],
    "zoom100": ["z"],
    "sync": ["s"],
    "focusSearch": ["/"],
}

# 问题标签的快捷键：按顺序对应 tags 数组的前几个标签。
# 默认用键盘上排，跳过 r（r 留给「圈图」），和上面的单键都不冲突。
DEFAULT_TAG_SHORTCUTS = ["q", "w", "e", "t", "y", "u", "i", "o", "p"]

DEFAULT_RUNTIME = {
    "host": "127.0.0.1",
    "port": 8765,
    "open_browser": True,
    "data_dir": "data",
    "wall_page_size": 60,
    "thumb": {"max_size": 360, "quality": 82},
    "auto_advance": True,   # 标记后自动跳到下一张
}

_STATUS_ALIASES = {
    "pass": "pass", "通过": "pass", "ok": "pass",
    "reject": "reject", "打回": "reject", "fail": "reject",
    "flag": "flag", "存疑": "flag", "doubt": "flag",
    "": "", "none": "", "unreviewed": "", "未审": "",
}


class ConfigError(Exception):
    """配置文件有问题时抛出，带可读的中文说明。"""


def _resolve(base: Path, value: str) -> str:
    """把配置里的路径解析成绝对路径（相对路径按配置文件所在目录解析）。"""
    if not value:
        return ""
    p = Path(value)
    if not p.is_absolute():
        p = base / p
    return str(p.resolve())


def _norm_status(value) -> str:
    if value is None:
        return ""
    return _STATUS_ALIASES.get(str(value).strip().lower(), "")


def load_config(path: str | os.PathLike) -> dict:
    """读取并规范化 config.json，返回可直接使用的配置字典。"""
    cfg_path = Path(path).resolve()
    if not cfg_path.exists():
        raise ConfigError(f"找不到配置文件：{cfg_path}")
    try:
        raw = json.loads(cfg_path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        raise ConfigError(f"配置文件 JSON 解析失败（{cfg_path}）：{exc}") from exc
    if not isinstance(raw, dict):
        raise ConfigError("配置文件根节点必须是对象 {}")

    base = cfg_path.parent
    cfg = copy.deepcopy(DEFAULT_RUNTIME)
    for key, value in raw.items():
        if key == "sets":
            continue
        cfg[key] = value
    cfg["data_dir"] = _resolve(base, cfg.get("data_dir") or "data")
    cfg["config_path"] = str(cfg_path)

    thumb = dict(DEFAULT_RUNTIME["thumb"])
    thumb.update(raw.get("thumb") or {})
    cfg["thumb"] = thumb

    tags = raw.get("tags")
    cfg["tags"] = [str(t) for t in tags] if isinstance(tags, list) and tags else list(DEFAULT_TAGS)

    shortcuts = copy.deepcopy(DEFAULT_SHORTCUTS)
    for action, keys in (raw.get("shortcuts") or {}).items():
        if isinstance(keys, str):
            keys = [keys]
        if isinstance(keys, list):
            shortcuts[action] = [str(k) for k in keys]
    cfg["shortcuts"] = shortcuts

    tag_keys = raw.get("tag_shortcuts")
    if isinstance(tag_keys, str):
        tag_keys = [tag_keys]
    cfg["tag_shortcuts"] = [str(k) for k in tag_keys] if isinstance(tag_keys, list) \
        else list(DEFAULT_TAG_SHORTCUTS)

    sets_raw = raw.get("sets")
    if not isinstance(sets_raw, list) or not sets_raw:
        raise ConfigError("配置里至少需要一个素材集（sets 数组）")

    seen_ids: set[str] = set()
    sets: list[dict] = []
    for index, item in enumerate(sets_raw):
        sets.append(_normalize_set(item, index, base, seen_ids))
    cfg["sets"] = sets
    return cfg


def _normalize_set(item: dict, index: int, base: Path, seen_ids: set[str]) -> dict:
    if not isinstance(item, dict):
        raise ConfigError(f"sets[{index}] 必须是对象")

    set_type = str(item.get("type") or "image").strip().lower()
    if set_type not in ("image", "text"):
        raise ConfigError(f"sets[{index}].type 只能是 image 或 text，实际是 {set_type!r}")

    set_id = str(item.get("id") or f"set{index + 1}").strip()
    if set_id in seen_ids:
        raise ConfigError(f"素材集 id 重复：{set_id!r}")
    seen_ids.add(set_id)

    name = str(item.get("name") or set_id)

    a = _normalize_source(item.get("a"), set_type, base, f"sets[{index}].a")
    b = _normalize_source(item.get("b"), set_type, base, f"sets[{index}].b")

    # pair 规则
    pair = str(item.get("pair") or ("basename" if set_type == "image" else "json_key")).strip()
    allowed = ("basename", "manifest", "json_key")
    if pair not in allowed:
        raise ConfigError(f"sets[{index}].pair 只能是 {'/'.join(allowed)}，实际是 {pair!r}")
    if set_type == "text" and pair == "basename":
        pair = "basename"  # 文本文件按同名配对，合法
    if pair == "json_key" and set_type != "text":
        raise ConfigError(f"sets[{index}]：pair=json_key 只用于 text 类型素材集")

    include = item.get("include") or (
        ["*.png", "*.jpg", "*.jpeg", "*.webp", "*.bmp", "*.gif"] if set_type == "image"
        else ["*.txt", "*.md", "*.csv", "*.json"]
    )
    if isinstance(include, str):
        include = [include]

    exclude = item.get("exclude") or (["_*", ".*", "~*"] if set_type == "image" else [])
    if isinstance(exclude, str):
        exclude = [exclude]

    items_from = str(item.get("items_from") or "b").strip().lower()
    if items_from not in ("a", "b", "union"):
        raise ConfigError(f"sets[{index}].items_from 只能是 a/b/union，实际是 {items_from!r}")

    meta = _normalize_meta(item.get("meta"), base, f"sets[{index}].meta")

    if pair == "manifest" and not meta:
        raise ConfigError(f"sets[{index}]：pair=manifest 时必须提供 meta 配置")

    return {
        "id": set_id,
        "name": name,
        "type": set_type,
        "pair": pair,
        "a": a,
        "b": b,
        "include": [str(p) for p in include],
        "exclude": [str(p) for p in exclude],
        "recursive": bool(item.get("recurse", True)),
        "items_from": items_from,
        "group_field": item.get("group_field") or "",
        "id_regex": item.get("id_regex") or "",
        "id_template": item.get("id_template") or "",
        "meta": meta,
        "note": item.get("note") or "",
        "default_tag": item.get("default_tag") or "",
    }


def _normalize_source(raw, set_type: str, base: Path, where: str) -> dict:
    if not isinstance(raw, dict):
        raise ConfigError(f"{where} 必须是对象，形如 {{\"root\": \"路径\", \"label\": \"源A\"}}")
    label = str(raw.get("label") or "")
    kind = str(raw.get("kind") or ("dir" if raw.get("root") else "json")).strip().lower()

    if kind == "dir":
        root = raw.get("root")
        if not root:
            raise ConfigError(f"{where}.root 不能为空")
        return {"kind": "dir", "root": _resolve(base, str(root)), "label": label}

    if kind in ("json", "text_file"):
        path = raw.get("path")
        if not path:
            raise ConfigError(f"{where}.path 不能为空")
        fields = raw.get("fields") or raw.get("text_fields") or []
        if isinstance(fields, str):
            fields = [fields]
        return {
            "kind": kind,
            "path": _resolve(base, str(path)),
            "label": label,
            "fields": [str(f) for f in fields],
        }

    raise ConfigError(f"{where}.kind 只能是 dir / json / text_file，实际是 {kind!r}")


def _normalize_meta(raw, base: Path, where: str) -> dict | None:
    if not raw:
        return None
    if not isinstance(raw, dict):
        raise ConfigError(f"{where} 必须是对象")
    path = raw.get("path")
    if not path:
        raise ConfigError(f"{where}.path 不能为空")
    fields = raw.get("fields") or []
    if isinstance(fields, str):
        fields = [fields]
    rename = raw.get("rename") or {}
    facets = raw.get("facets")
    if facets is not None and not isinstance(facets, list):
        raise ConfigError(f"{where}.facets 必须是数组（留空数组表示不做筛选下拉框）")
    return {
        "kind": str(raw.get("kind") or "auto").strip().lower(),
        "path": _resolve(base, str(path)),
        "join": str(raw.get("join") or "filename").strip().lower(),
        "key_template": raw.get("key_template") or "",
        "id_field": raw.get("id_field") or "",
        "name_field": raw.get("name_field") or "",
        "fields": [str(f) for f in fields],
        "facets": [str(f) for f in facets] if facets is not None else None,
        "rename": {str(k): str(v) for k, v in rename.items()} if isinstance(rename, dict) else {},
    }
