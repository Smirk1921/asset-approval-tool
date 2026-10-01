# -*- coding: utf-8 -*-
"""图包素材审批工具 · 本地服务

启动：
    python app.py                 # 用同目录的 config.json
    python app.py --config 别的配置.json --port 9000

服务只监听本机，所有素材路径都来自配置文件，接口不接受任意路径参数。
"""
from __future__ import annotations

import argparse
import atexit
import mimetypes
import os
import signal
import subprocess
import threading
import time
import urllib.request
import webbrowser
from datetime import datetime
from pathlib import Path
from urllib.parse import quote

from flask import Flask, jsonify, request, send_file, send_from_directory

from core.agent_api import (brief_item, detail_item, filter_items, normalize_annotations,
                            normalize_status)
from core.agent_ext import (advice_payload, compare_sets as compare_sets_impl,
                            derive_set as derive_set_impl, drop_set as drop_set_impl,
                            export_crops as export_crops_impl, lookup as lookup_impl,
                            pair_image as pair_image_impl,
                            pair_images as pair_images_impl,
                            reload_config, review_batch as review_batch_impl)
from core.config import ConfigError, load_config
from core.materials import MaterialError, SetRegistry, read_text_file
from core.results import NO_HISTORY, STATUS_CN, ResultStore
from core.thumbs import clear_cache, thumbnail
from core.version import __version__

ROOT = Path(__file__).resolve().parent
STATIC_DIR = ROOT / "static"

app = Flask(__name__, static_folder=None)
app.config["JSON_AS_ASCII"] = False

_cfg: dict = {}
_registry: SetRegistry | None = None
_stores: dict[str, ResultStore] = {}
_stores_lock = threading.Lock()


# ---------------------------------------------------------------- 工具

def _set_of(set_id: str):
    material = _registry.get(set_id)
    if material.error:
        raise MaterialError(material.error)
    return material


def _store_of(material) -> ResultStore:
    """每个素材集一个结果文件；同一进程内只建一次。"""
    with _stores_lock:
        store = _stores.get(material.id)
        if store is None:
            store = ResultStore(material.id, material.name, Path(_cfg["data_dir"]) / "results")
            _stores[material.id] = store
        return store


def _json_error(message: str, code: int = 400):
    return jsonify({"ok": False, "error": message}), code


@app.errorhandler(MaterialError)
def _handle_material_error(exc):
    return _json_error(str(exc), 400)


@app.errorhandler(ConfigError)
def _handle_config_error(exc):
    return _json_error(str(exc), 400)


# ---------------------------------------------------------------- 页面

@app.get("/")
def index():
    return send_from_directory(STATIC_DIR, "index.html")


@app.get("/static/<path:filename>")
def static_files(filename: str):
    return send_from_directory(STATIC_DIR, filename)


@app.get("/favicon.ico")
def favicon():
    return ("", 204)


# ---------------------------------------------------------------- 配置

@app.get("/api/config")
def api_config():
    sets = []
    for spec in _cfg["sets"]:
        entry = {
            "id": spec["id"], "name": spec["name"], "type": spec["type"],
            "note": spec.get("note", ""),
            "sourceA": spec["a"]["label"] or "源A",
            "sourceB": spec["b"]["label"] or "源B",
            "count": 0, "error": None, "stats": {}, "facets": {}, "warnings": [],
        }
        try:
            material = _registry.get(spec["id"])
            entry.update(material.public_spec())
            if not material.error:
                entry["stats"] = _store_of(material).stats([i["id"] for i in material.items])
        except MaterialError as exc:
            entry["error"] = str(exc)
        sets.append(entry)
    return jsonify({
        "ok": True,
        "version": __version__,
        "configPath": _cfg["config_path"],
        "configMtime": _config_mtime(),
        "configHash": _config_hash(),
        "autoResetStale": bool(_cfg.get("auto_reset_stale")),
        "templates": _cfg.get("templates") or [],
        "dataDir": _cfg["data_dir"],
        "tags": _cfg["tags"],
        "shortcuts": _cfg["shortcuts"],
        "tagShortcuts": _cfg.get("tag_shortcuts") or [],
        "wallPageSize": _cfg["wall_page_size"],
        "autoAdvance": _cfg.get("auto_advance", True),
        "thumbSize": _cfg["thumb"]["max_size"],
        "statusLabels": STATUS_CN,
        "sets": sets,
    })


@app.get("/api/ping")
def api_ping():
    """轻量探针：只返回版本和配置指纹，前端用它判断"配置是不是变了"。

    刻意不返回素材集列表——那要重算源文件指纹，不适合几秒一次轮询。
    """
    return jsonify({"ok": True, "version": __version__,
                    "configHash": _config_hash(), "configMtime": _config_mtime()})


@app.get("/api/download")
def api_download():
    """把导出/裁剪产生的文件发给浏览器（只允许 data 目录下的文件）。"""
    raw = request.args.get("path")
    if not raw:
        return _json_error("缺少参数 path")
    target = Path(raw).resolve()
    data_dir = Path(_cfg["data_dir"]).resolve()
    try:
        target.relative_to(data_dir)
    except ValueError:
        return _json_error("只允许下载结果目录里的文件", 403)
    if not target.is_file():
        return _json_error(f"文件不存在：{target}", 404)
    return send_file(target, as_attachment=True, download_name=target.name)


def _config_mtime() -> float:
    try:
        return os.path.getmtime(_cfg["config_path"])
    except OSError:
        return 0.0


def _config_hash() -> str:
    """配置内容的短哈希，前端靠它判断"配置变了、要不要刷新"。"""
    import hashlib
    try:
        raw = Path(_cfg["config_path"]).read_bytes()
    except OSError:
        return ""
    digest = hashlib.sha1(raw)
    # sets.d 下的派生集合也算进配置指纹里
    for extra in sorted(Path(_cfg["config_path"]).parent.glob("sets.d/*.json")):
        try:
            digest.update(extra.read_bytes())
        except OSError:
            continue
    return digest.hexdigest()[:12]


def apply_auto_reset_stale() -> int:
    """配置里 auto_reset_stale=true 时，把「源文件已变」的结果清成未审。

    默认是关的：静默清掉别人已经下过的结论风险太大，宁可让界面提示、由人点。
    开了这个开关的人，就是明确要求"源一变就重审"。
    """
    if not _cfg.get("auto_reset_stale"):
        return 0
    cleared = 0
    for spec in _cfg["sets"]:
        material = _registry.get(spec["id"])
        if material.error:
            continue
        material.refresh_versions()
        store = _store_of(material)
        stale = store.stale_ids({i["id"]: i.get("v", "") for i in material.items})
        if stale:
            cleared += store.clear_ids(stale)
    return cleared


@app.post("/api/reload")
def api_reload():
    """重新读配置文件（素材集、标签、快捷键都会跟着更新），并重扫素材。"""
    global _cfg, _registry
    fresh = load_config(_cfg["config_path"])
    _cfg = fresh
    _registry = SetRegistry(_cfg)
    cleared = apply_auto_reset_stale()
    return jsonify({"ok": True, "sets": [s["id"] for s in _cfg["sets"]],
                    "autoResetCleared": cleared,
                    "tags": _cfg["tags"], "tagShortcuts": _cfg.get("tag_shortcuts") or []})


# ---------------------------------------------------------------- 素材清单

def _versions(material) -> dict[str, str]:
    """素材 id -> 源文件指纹。用于判断结果是否过期、以及给图片 URL 加版本号。"""
    return {item["id"]: item.get("v", "") for item in material.items}


def _set_overview(material, store) -> dict:
    versions = _versions(material)
    stats = store.stats([i["id"] for i in material.items])
    stale = store.stale_ids(versions)
    stats["stale"] = len(stale)
    stats["round"] = store.round
    return stats


@app.get("/api/set/<set_id>/items")
def api_items(set_id: str):
    material = _set_of(set_id)
    store = _store_of(material)
    # 每次打开/刷新都重算源文件指纹：素材重渲过的话，过期提示和图片版本号立刻就对了
    material.refresh_versions()
    records = store.all()
    versions = _versions(material)
    statuses = {}
    stale_count = 0
    for item in material.items:
        record = records.get(item["id"])
        if not record:
            continue
        recorded = record.get("src")
        is_stale = bool(recorded) and bool(item.get("v")) and recorded != item["v"]
        if is_stale:
            stale_count += 1
        statuses[item["key"]] = {
            "s": record.get("status") or "",
            "t": record.get("tags") or [],
            "n": 1 if record.get("note") else 0,
            "c": len(record.get("annotations") or []),
            "x": 1 if is_stale else 0,
        }
    stats = store.stats([i["id"] for i in material.items])
    stats["stale"] = stale_count
    stats["round"] = store.round
    return jsonify({
        "ok": True,
        "spec": material.public_spec(),
        "items": material.light_items(),
        "statuses": statuses,
        "stats": stats,
        "resultPath": str(store.path),
    })


@app.get("/api/set/<set_id>/item/<key>")
def api_item(set_id: str, key: str):
    material = _set_of(set_id)
    item = material.by_key(key)
    if item is None:
        return _json_error("找不到该素材（可能素材目录已变化，请刷新）", 404)
    store = _store_of(material)
    payload = dict(item)
    # 文本素材若是文件来源，取详情时才真正读盘
    if material.type == "text":
        for side in ("a", "b"):
            info = payload.get(side) or {}
            if info.get("lazy") and not info.get("text"):
                payload[side] = {**info, "text": read_text_file(info["path"])}
    payload["record"] = store.get(item["id"]) or {}
    return jsonify({"ok": True, "item": payload})


# ---------------------------------------------------------------- 文件与缩略图

def _resolve_file(material, key: str, side: str):
    item = material.by_key(key)
    if item is None:
        return None, "找不到该素材"
    if side not in ("a", "b"):
        return None, "side 只能是 a 或 b"
    info = item.get(side) or {}
    if material.type != "image":
        return None, "该素材集不是图片类型"
    if not info.get("exists"):
        return None, info.get("missing_reason") or "该素材没有这一侧的图片"
    path = Path(info["path"])
    if not path.exists():
        return None, "源文件不存在了"
    return path, None


@app.get("/api/set/<set_id>/file/<key>/<side>")
def api_file(set_id: str, key: str, side: str):
    material = _set_of(set_id)
    path, err = _resolve_file(material, key, side)
    if err:
        return _json_error(err, 404)
    mime = mimetypes.guess_type(str(path))[0] or "application/octet-stream"
    # 用 mtime 做条件请求：文件没变就 304，变了自动返回新图
    response = send_file(path, mimetype=mime, conditional=True)
    response.headers["Cache-Control"] = "no-cache, must-revalidate"
    return response


@app.get("/api/set/<set_id>/thumb/<key>/<side>")
def api_thumb(set_id: str, key: str, side: str):
    material = _set_of(set_id)
    path, err = _resolve_file(material, key, side)
    if err:
        return _json_error(err, 404)
    try:
        size = int(request.args.get("size") or _cfg["thumb"]["max_size"])
    except ValueError:
        size = _cfg["thumb"]["max_size"]
    size = max(48, min(1200, size))
    try:
        out = thumbnail(path, Path(_cfg["data_dir"]) / "thumbs" / material.id / side,
                        size, _cfg["thumb"]["quality"])
    except Exception as exc:  # 坏图/不支持格式：不要让整个墙崩掉
        app.logger.exception("缩略图生成失败：%s", path)
        return _json_error(f"缩略图生成失败：{exc}", 500)
    # 缓存文件名里带了源文件指纹，内容不会变，可长期缓存；
    # 前端 URL 上还会带 ?v=<源文件指纹>，源文件重渲后 URL 变化，浏览器必然重新拉。
    response = send_file(out, mimetype="image/jpeg", conditional=True)
    response.headers["Cache-Control"] = "public, max-age=31536000, immutable"
    return response


@app.post("/api/clear-thumb-cache")
def api_clear_thumbs():
    set_id = request.args.get("set") or (request.get_json(silent=True) or {}).get("set")
    root = Path(_cfg["data_dir"]) / "thumbs"
    removed = clear_cache(root / set_id if set_id else root)
    return jsonify({"ok": True, "removed": removed})


# ---------------------------------------------------------------- 审批写入

@app.post("/api/set/<set_id>/review")
def api_review(set_id: str):
    material = _set_of(set_id)
    body = request.get_json(silent=True) or {}
    item = material.by_key(body.get("key") or "")
    if item is None:
        return _json_error("找不到该素材", 404)
    store = _store_of(material)
    record = store.save(
        item["id"],
        status=body.get("status"),
        tags=body.get("tags"),
        note=body.get("note"),
        annotations=body.get("annotations"),
        reviewer=body.get("reviewer") or "",
    )
    # 记录审批那一刻的源文件指纹：以后源文件被重渲，就能识别出这条结果已过期
    if record:
        store.stamp_sources({item["id"]: item.get("v", "")})
        record = store.get(item["id"]) or record
    return jsonify({"ok": True, "record": record, "stats": _set_overview(material, store)})


@app.post("/api/set/<set_id>/batch")
def api_batch(set_id: str):
    material = _set_of(set_id)
    body = request.get_json(silent=True) or {}
    keys = body.get("keys") or []
    if not isinstance(keys, list) or not keys:
        return _json_error("keys 不能为空")
    ids = []
    for key in keys:
        item = material.by_key(key)
        if item is not None:
            ids.append(item["id"])
    if not ids:
        return _json_error("没有匹配到任何素材")
    status = body.get("status")
    if status is not None and status not in ("pass", "reject", "flag", ""):
        return _json_error("status 只能是 pass / reject / flag / 空")
    store = _store_of(material)
    changed = store.save_many(ids, status=status, tags=body.get("tags"))
    store.stamp_sources({item_id: (material.by_id(item_id) or {}).get("v", "") for item_id in ids})
    return jsonify({"ok": True, "changed": changed, "stats": _set_overview(material, store)})


@app.post("/api/set/<set_id>/undo")
def api_undo(set_id: str):
    """撤销：把某条素材回退到上一次的历史记录。"""
    material = _set_of(set_id)
    body = request.get_json(silent=True) or {}
    item = material.by_key(body.get("key") or "")
    if item is None:
        return _json_error("找不到该素材", 404)
    store = _store_of(material)
    previous = store.previous(item["id"])
    if previous is NO_HISTORY:
        return _json_error("没有可撤销的上一步")
    store.restore(item["id"], previous)
    return jsonify({"ok": True, "record": previous or {}, "stats": _set_overview(material, store)})


# ---------------------------------------------------------------- 过期结果 / 轮次

@app.post("/api/set/<set_id>/clear-stale")
def api_clear_stale(set_id: str):
    """把「审批之后源文件又被改过」的结果清掉，让它们回到未审。"""
    material = _set_of(set_id)
    store = _store_of(material)
    body = request.get_json(silent=True) or {}
    keys = body.get("keys")
    versions = _versions(material)
    if isinstance(keys, list) and keys:
        ids = [item["id"] for item in (material.by_key(k) for k in keys) if item]
    else:
        ids = store.stale_ids(versions)
    removed = store.clear_ids(ids)
    return jsonify({"ok": True, "removed": removed, "stats": _set_overview(material, store)})


@app.post("/api/set/<set_id>/new-round")
def api_new_round(set_id: str):
    """归档本轮结果，开始新一轮（同时清空该素材集的缩略图缓存）。"""
    material = _set_of(set_id)
    store = _store_of(material)
    info = store.start_new_round()
    removed = clear_cache(Path(_cfg["data_dir"]) / "thumbs" / material.id)
    info["clearedThumbs"] = removed
    info["stats"] = _set_overview(material, store)
    return jsonify({"ok": True, **info})


@app.get("/api/set/<set_id>/previous-round")
def api_previous_round(set_id: str):
    """上一轮被打回的素材 id，用来「优先复审上轮打回的」。"""
    material = _set_of(set_id)
    store = _store_of(material)
    records = store.previous_round_records()
    if not records:
        return jsonify({"ok": True, "round": None, "ids": [], "total": 0})
    ids = [item_id for item_id, record in records.items()
           if (record or {}).get("status") == "reject"]
    return jsonify({"ok": True, "round": max(1, store.round - 1), "ids": ids,
                    "total": len(records)})


# ---------------------------------------------------------------- 给 agent / 脚本用的接口
#
# 语义与 core/agent_api.py 的 AgentAPI 完全一致（同一套筛选与规范化函数），
# 区别只是这里跑在服务里、走 HTTP，能跟网页版共用同一份内存状态。

def _agent_set(set_id: str):
    if not set_id:
        raise MaterialError("缺少参数 set（素材集 id）")
    return _set_of(set_id)


def _agent_ids(raw) -> list[str] | None:
    if raw is None:
        return None
    if isinstance(raw, str):
        return [part for part in (p.strip() for p in raw.split(",")) if part]
    if isinstance(raw, list):
        return [str(x) for x in raw]
    return None


def _truthy(value) -> bool:
    if value is None:
        return False
    if isinstance(value, bool):
        return value
    return str(value).strip().lower() not in ("", "0", "false", "no", "off")


def _agent_filters(args, *, include_status: bool = True) -> dict:
    """从查询串/JSON 里取出筛选条件，字段名和 AgentAPI.items 对齐。

    include_status=False 用于批量操作：那里的 status 指的是「要改成什么状态」，
    不能再被当成筛选条件读走，否则语义打架（"把未审的都通过" 会变成 "把已通过的都通过"）。
    筛选条件也接受 filter_status / filter_tag 这种带前缀的写法。
    """
    def flag(*names):
        return any(_truthy(args.get(name)) for name in names)

    status = None
    if include_status:
        status = args.get("filter_status") or args.get("status")

    filters = {
        "status": status if status not in (None, "") else None,
        "tag": args.get("filter_tag") or args.get("tag") or None,
        "search": args.get("search") or None,
        "group": args.get("group") or None,
        "stale_only": flag("stale", "stale_only", "filter_stale"),
        "no_tags": flag("no_tags", "noTags"),
        "annotated": flag("annotated", "has_annotations"),
        "noted": flag("noted", "has_note"),
    }
    ids = _agent_ids(args.get("ids"))
    if ids:
        filters["ids"] = ids
    return filters


def _has_selection(args) -> bool:
    """批量操作必须给出明确的选择范围（ids，或至少一个筛选条件）。
    不给就直接报错，免得 agent 一句 "都通过" 把整个素材集刷了。"""
    if _agent_ids(args.get("ids")):
        return True
    # 故意不含裸的 status：批量里它是「要改成什么状态」，
    # 筛选状态必须写 filter_status，否则「把未审的都通过」会变成「把已通过的都通过」。
    keys = ("filter_status", "tag", "filter_tag", "search", "group",
            "stale", "stale_only", "filter_stale", "no_tags", "noTags",
            "annotated", "has_annotations", "noted", "has_note")
    return any(_truthy(args.get(k)) for k in keys)


def _agent_limit(args, default=None):
    raw = args.get("limit")
    if raw in (None, ""):
        return default
    try:
        return max(0, int(raw))
    except (TypeError, ValueError):
        raise MaterialError(f"limit 必须是整数：{raw!r}")


@app.get("/api/agent/status")
def agent_status():
    sets = []
    for spec in _cfg["sets"]:
        entry = {"id": spec["id"], "name": spec["name"], "type": spec["type"],
                 "note": spec.get("note", ""),
                 "source_a": spec["a"]["label"] or "源A",
                 "source_b": spec["b"]["label"] or "源B"}
        material = _registry.get(spec["id"])
        if material.error:
            entry.update({"count": 0, "error": material.error})
        else:
            material.refresh_versions()
            entry.update({"count": len(material.items), "error": None,
                          **_set_overview(material, _store_of(material))})
        sets.append(entry)
    return jsonify({
        "ok": True,
        "version": __version__,
        "config_path": _cfg["config_path"],
        "data_dir": _cfg["data_dir"],
        "server": f"http://{_cfg.get('host', '127.0.0.1')}:{_cfg.get('port', 8765)}/",
        "transport": "http",
        "tags": _cfg["tags"],
        "templates": _cfg.get("templates") or [],
        "sets": sets,
    })


@app.get("/api/agent/items")
def agent_items():
    material = _agent_set(request.args.get("set"))
    store = _store_of(material)
    material.refresh_versions()
    records = store.all()
    versions = _versions(material)
    matched = filter_items(material, records, **_agent_filters(request.args))
    offset = max(0, int(request.args.get("offset") or 0))
    limit = _agent_limit(request.args)
    window = matched[offset:offset + limit] if limit else matched[offset:]
    return jsonify({
        "ok": True,
        "set_id": material.id,
        "set_name": material.name,
        "total": len(material.items),
        "matched": len(matched),
        "offset": offset,
        "limit": limit,
        "items": [brief_item(item, records.get(item["id"]) or {},
                             is_stale=_is_item_stale(item, records.get(item["id"]) or {}, versions))
                  for item in window],
    })


def _is_item_stale(item: dict, record: dict, versions: dict) -> bool:
    recorded = record.get("src")
    return bool(recorded) and bool(versions.get(item["id"])) and recorded != versions[item["id"]]


def _agent_item_or_404(material, item_id: str) -> dict:
    item = material.by_id(item_id) or material.by_id(Path(str(item_id)).stem)
    if item is None:
        raise MaterialError(f"素材集 {material.id} 里没有 id 为 {item_id!r} 的素材")
    return item


@app.get("/api/agent/item")
def agent_item():
    material = _agent_set(request.args.get("set"))
    item = _agent_item_or_404(material, request.args.get("id") or "")
    store = _store_of(material)
    material.refresh_versions()
    record = store.get(item["id"]) or {}
    versions = _versions(material)
    return jsonify({"ok": True, "item": detail_item(material, item, record,
                                                    is_stale=_is_item_stale(item, record, versions))})


@app.post("/api/agent/review")
def agent_review():
    body = request.get_json(silent=True) or {}
    material = _agent_set(body.get("set"))
    item = _agent_item_or_404(material, body.get("id") or "")
    store = _store_of(material)
    record = store.save(
        item["id"],
        status=normalize_status(body.get("status")),
        tags=[str(t) for t in body["tags"]] if body.get("tags") is not None else None,
        note=body.get("note"),
        annotations=(normalize_annotations(body.get("annotations"))
                     if body.get("annotations") is not None else None),
    )
    if record:
        store.stamp_sources({item["id"]: item.get("v", "")})
        record = store.get(item["id"]) or record
    material.refresh_versions()
    versions = _versions(material)
    return jsonify({"ok": True, "changed": 1,
                    "item": detail_item(material, item, record,
                                        is_stale=_is_item_stale(item, record, versions)),
                    "stats": _set_overview(material, store)})


@app.post("/api/agent/batch")
def agent_batch():
    body = request.get_json(silent=True) or {}
    material = _agent_set(body.get("set"))
    store = _store_of(material)
    material.refresh_versions()
    records = store.all()
    versions = _versions(material)
    ids = _agent_ids(body.get("ids"))
    if ids:
        targets = filter_items(material, records, ids=ids)
        missing = [i for i in ids if not any(t["id"] == i for t in targets)]
        if missing:
            raise MaterialError(f"这些 id 不存在：{', '.join(missing[:5])}")
    else:
        if not _has_selection(body):
            raise MaterialError(
                "批量操作必须指定范围：给 ids，或给至少一个筛选条件"
                "（filter_status / filter_tag / search / group / stale / no_tags / annotated / noted）")
        targets = filter_items(material, records, **_agent_filters(body, include_status=False))
    if not targets:
        return jsonify({"ok": True, "changed": 0, "matched": 0,
                        "dry_run": bool(body.get("dry_run")), "ids": [],
                        "stats": _set_overview(material, store)})
    if body.get("dry_run"):
        return jsonify({"ok": True, "changed": 0, "matched": len(targets), "dry_run": True,
                        "ids": [t["id"] for t in targets],
                        "stats": _set_overview(material, store)})
    changed = store.save_many([t["id"] for t in targets],
                              status=normalize_status(body.get("status")),
                              tags=body.get("tags"))
    store.stamp_sources({t["id"]: t.get("v", "") for t in targets})
    return jsonify({"ok": True, "changed": changed, "matched": len(targets), "dry_run": False,
                    "ids": [t["id"] for t in targets],
                    "stats": _set_overview(material, store)})


@app.post("/api/agent/undo")
def agent_undo():
    body = request.get_json(silent=True) or {}
    material = _agent_set(body.get("set"))
    item = _agent_item_or_404(material, body.get("id") or "")
    store = _store_of(material)
    previous = store.previous(item["id"])
    if previous is NO_HISTORY:
        raise MaterialError("这条素材没有可撤销的上一步（撤销记录只存在于当前服务进程内）")
    store.restore(item["id"], previous)
    material.refresh_versions()
    record = store.get(item["id"]) or {}
    return jsonify({"ok": True,
                    "item": detail_item(material, item, record,
                                        is_stale=_is_item_stale(item, record, _versions(material)))})


@app.post("/api/agent/clear-stale")
def agent_clear_stale():
    body = request.get_json(silent=True) or {}
    material = _agent_set(body.get("set"))
    store = _store_of(material)
    material.refresh_versions()
    versions = _versions(material)
    ids = _agent_ids(body.get("ids"))
    if ids:
        targets = [material.by_id(i) or material.by_id(Path(str(i)).stem) for i in ids]
        targets = [t for t in targets if t]
    else:
        stale = set(store.stale_ids(versions))
        targets = [i for i in material.items if i["id"] in stale]
    if body.get("dry_run"):
        return jsonify({"ok": True, "removed": 0, "matched": len(targets), "dry_run": True,
                        "ids": [t["id"] for t in targets]})
    removed = store.clear_ids([t["id"] for t in targets])
    return jsonify({"ok": True, "removed": removed, "matched": len(targets), "dry_run": False,
                    "ids": [t["id"] for t in targets],
                    "stats": _set_overview(material, store)})


@app.post("/api/agent/new-round")
def agent_new_round():
    body = request.get_json(silent=True) or {}
    material = _agent_set(body.get("set"))
    store = _store_of(material)
    info = store.start_new_round()
    info["cleared_thumbs"] = clear_cache(Path(_cfg["data_dir"]) / "thumbs" / material.id)
    return jsonify({"ok": True, **info, "stats": _set_overview(material, store)})


@app.get("/api/agent/previous-round")
def agent_previous_round():
    material = _agent_set(request.args.get("set"))
    store = _store_of(material)
    records = store.previous_round_records()
    ids = [i for i, r in records.items() if (r or {}).get("status") == "reject"]
    return jsonify({"ok": True, "round": max(1, store.round - 1) if records else None,
                    "ids": ids, "total": len(records)})


@app.post("/api/agent/export")
def agent_export():
    body = request.get_json(silent=True) or {}
    material = _agent_set(body.get("set"))
    store = _store_of(material)
    material.refresh_versions()
    versions = _versions(material)
    records = store.all()
    only = (body.get("only") or "all").lower()
    fmt = (body.get("format") or "json").lower()

    rows = []
    for item in material.items:
        record = records.get(item["id"]) or {}
        if only == "reviewed" and not record:
            continue
        if only == "problem":
            status = record.get("status") or ""
            if status not in ("reject", "flag") and not record.get("annotations") \
                    and not record.get("tags") and not record.get("note"):
                continue
        rows.append({
            "id": item["id"], "name": item["name"], "group": item["group"], "meta": item["meta"],
            "sourceA": (item["a"].get("path") or "") if material.type == "image" else (item["a"].get("text") or ""),
            "sourceB": (item["b"].get("path") or "") if material.type == "image" else (item["b"].get("text") or ""),
            "sizes": _image_sizes(item) if (material.type == "image" and record.get("annotations")) else {},
            "record": {
                "status": record.get("status") or "",
                "status_cn": STATUS_CN.get(record.get("status") or "", "未审"),
                "tags": record.get("tags") or [],
                "note": record.get("note") or "",
                "annotations": record.get("annotations") or [],
                "reviewed_at": record.get("reviewed_at") or "",
                "round": record.get("round") or store.round,
                "stale": _is_item_stale(item, record, versions),
            },
        })

    if fmt == "csv":
        content = store.export_csv(rows)
    elif fmt in ("annotations", "csv-annotations"):
        content = store.export_annotations_csv(rows)
        fmt = "csv"
    elif fmt == "json":
        content = store.export_json(rows)
    else:
        raise MaterialError(f"不支持的导出格式：{fmt}（可用 json/csv/annotations）")

    result = {"ok": True, "set_id": material.id, "format": fmt, "only": only, "count": len(rows)}
    out = body.get("out")
    if out:
        path = Path(out)
        if path.is_dir():
            path = path / f"{material.id}.{'json' if fmt == 'json' else 'csv'}"
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(content, encoding="utf-8")
        result["path"] = str(path)
    else:
        result["content"] = content
    return jsonify(result)


@app.get("/api/agent/image")
def agent_image():
    material = _agent_set(request.args.get("set"))
    if material.type != "image":
        raise MaterialError(f"素材集 {material.id} 不是图片类型")
    item = _agent_item_or_404(material, request.args.get("id") or "")
    side = (request.args.get("side") or "b").lower()
    if side not in ("a", "b"):
        raise MaterialError("side 只能是 a 或 b")
    info = item[side]
    if not info.get("exists"):
        raise MaterialError(f"这条素材没有{'源A' if side == 'a' else '源B'}图片："
                            f"{info.get('missing_reason', '')}")
    path = Path(info["path"])
    result = {"ok": True, "id": item["id"], "side": side, "path": str(path),
              "bytes": path.stat().st_size if path.exists() else 0}
    max_size = request.args.get("max_size")
    if max_size:
        cache = Path(_cfg["data_dir"]) / "thumbs" / material.id / side
        thumb = thumbnail(path, cache, int(max_size), _cfg["thumb"]["quality"])
        result["thumb_path"] = str(thumb)
    return jsonify(result)



# ---------------------------------------------------------------- agent 扩展能力接口

def _agent_material_of(body) -> tuple:
    material = _agent_set(body.get("set"))
    return material, _store_of(material)


@app.post("/api/agent/pair-image")
def agent_pair_image():
    """把源A/源B 拼成一张对照图（视觉模型一次只吃一张图时用）。

    也支持直接给两张图的路径做自由对比（比如上一版渲染 vs 这一版渲染）。
    """
    body = request.get_json(silent=True) or {}
    if body.get("a") and body.get("b"):
        material = store = None
        set_id = None
    else:
        set_id = body.get("set")
        material, store = _agent_material_of(body)
    info = pair_image_impl(
        _cfg, material=material, store=store, item_id=body.get("id"),
        path_a=body.get("a"), path_b=body.get("b"),
        layout=body.get("layout") or "horizontal",
        max_width=body.get("max_width", 2048), max_height=body.get("max_height"),
        gap=int(body.get("gap") or 8), labels=body.get("labels"),
        out=body.get("out"), draw_annotations=body.get("draw_annotations", True),
        use_cache=body.get("use_cache", True))
    info["set_id"] = set_id
    return jsonify(info)


@app.post("/api/agent/pair-images")
def agent_pair_images():
    """批量拼接，一次拿很多张。"""
    body = request.get_json(silent=True) or {}
    material, store = _agent_material_of(body)
    ids = _agent_ids(body.get("ids")) or []
    out_dir = body.get("out_dir")
    if not out_dir:
        return _json_error("缺少参数 out_dir（拼好的图放哪个目录）")
    return jsonify(pair_images_impl(
        _cfg, material, store, ids, out_dir=out_dir,
        layout=body.get("layout") or "horizontal",
        max_width=body.get("max_width", 2048), max_height=body.get("max_height"),
        gap=int(body.get("gap") or 8), labels=body.get("labels"),
        draw_annotations=body.get("draw_annotations", True)))


@app.post("/api/agent/export-crops")
def agent_export_crops():
    """把圈注框裁成小图导出（抠图素材、单独发给子代理看）。"""
    body = request.get_json(silent=True) or {}
    material, store = _agent_material_of(body)
    zip_path = body.get("zip_path")
    out_dir = body.get("out_dir")
    if not zip_path and not out_dir and body.get("download"):
        # 网页端点「导出圈选裁剪」时走这条：打包到结果目录下，再走 /api/download 下载
        stamp = datetime.now().strftime("%Y%m%d_%H%M")
        zip_path = str(Path(_cfg["data_dir"]) / "exports" / f"{material.id}_圈选裁剪_{stamp}.zip")
    return jsonify(export_crops_impl(
        material, store, out_dir=out_dir, zip_path=zip_path,
        only=(body.get("only") or "problem"), side=body.get("side"),
        padding=int(body.get("padding") or 8), scale=float(body.get("scale") or 1.0),
        annotated_only=body.get("annotated_only", True)))


@app.post("/api/agent/review-batch")
def agent_review_batch():
    """一次写回多条审批结果：先全校验，有一条不合法就整批不写。"""
    body = request.get_json(silent=True) or {}
    material, store = _agent_material_of(body)
    if not body.get("dry_run"):
        material.refresh_versions()
    return jsonify(review_batch_impl(
        material, store, _cfg, body.get("reviews") or [],
        dry_run=bool(body.get("dry_run")), partial_ok=bool(body.get("partial_ok")),
        lenient_tags=bool(body.get("lenient_tags"))))


@app.post("/api/agent/derive-set")
def agent_derive_set():
    """从现有素材集派生一个新集合，写到 sets.d/（不动手写的 config.json）。"""
    global _cfg, _registry
    body = request.get_json(silent=True) or {}
    from_set = body.get("from_set")
    new_id = body.get("new_id")
    if not from_set or not new_id:
        return _json_error("需要 from_set 和 new_id")
    source = _agent_set(from_set)
    info = derive_set_impl(
        _cfg, source, _store_of(source), new_id=new_id, name=body.get("name"),
        a_root=body.get("a_root"), b_root=body.get("b_root"),
        only_ids=_agent_ids(body.get("only_ids")), only_from=body.get("only_from"),
        dry_run=bool(body.get("dry_run")))
    if not body.get("dry_run"):
        _cfg, _registry = reload_config(_cfg)
        material = _registry.get(new_id)
        info.update({"count": len(material.items), "error": material.error,
                     "warnings": material.warnings})
    return jsonify(info)


@app.post("/api/agent/drop-set")
def agent_drop_set():
    """删掉一个派生出来的集合。"""
    global _cfg, _registry
    body = request.get_json(silent=True) or {}
    set_id = body.get("set")
    if not set_id:
        return _json_error("缺少参数 set")
    info = drop_set_impl(_cfg, set_id, dry_run=bool(body.get("dry_run")))
    if not body.get("dry_run"):
        _cfg, _registry = reload_config(_cfg)
    return jsonify(info)


@app.get("/api/agent/lookup")
def agent_lookup():
    """同一个素材 id 在各个集合里的结论。"""
    item_id = request.args.get("id")
    if not item_id:
        return _json_error("缺少参数 id")
    sets = _agent_ids(request.args.get("sets"))
    # lookup 里按素材集 id 取结果，这里包一层（_store_of 收的是 material 对象）
    return jsonify(lookup_impl(_cfg, _registry,
                               lambda sid: _store_of(_registry.get(sid)),
                               item_id, sets=sets))


@app.get("/api/agent/compare")
def agent_compare():
    """两个集合按素材 id 对比结论。"""
    left = request.args.get("set")
    right = request.args.get("other")
    if not left or not right:
        return _json_error("需要 set 和 other 两个素材集 id")
    a, b = _agent_set(left), _agent_set(right)
    return jsonify(compare_sets_impl(a, b, _store_of(a), _store_of(b)))


@app.get("/api/agent/templates")
def agent_templates():
    return jsonify({"ok": True, "templates": _cfg.get("templates") or [],
                    "tags": _cfg["tags"]})


@app.get("/api/agent/pair-file")
def agent_pair_file():
    """把拼好的对照图直接当图片返回（前端/工具预览用）。"""
    path = request.args.get("path")
    if not path:
        return _json_error("缺少参数 path")
    target = Path(path)
    if not target.exists() or target.suffix.lower() != ".png":
        return _json_error(f"找不到这张对照图：{path}", 404)
    return send_file(target, mimetype="image/png", conditional=True)


# ---------------------------------------------------------------- 导出

@app.get("/api/set/<set_id>/export")
def api_export(set_id: str):
    material = _set_of(set_id)
    store = _store_of(material)
    fmt = (request.args.get("format") or "json").lower()
    only = (request.args.get("only") or "all").lower()
    part = (request.args.get("part") or "main").lower()
    records = store.all()

    rows = []
    for item in material.items:
        record = records.get(item["id"]) or {}
        if only == "reviewed" and not record:
            continue
        if only == "problem":
            # 「问题项」= 打回/存疑，或圈了图、挂了标签、写了备注（哪怕还没定状态）
            status = record.get("status") or ""
            if status not in ("reject", "flag") and not record.get("annotations") \
                    and not record.get("tags") and not record.get("note"):
                continue
        has_annotations = bool(record.get("annotations"))
        rows.append({
            "id": item["id"],
            "name": item["name"],
            "group": item["group"],
            "meta": item["meta"],
            "sourceA": (item["a"].get("path") or "") if material.type == "image" else (item["a"].get("text") or ""),
            "sourceB": (item["b"].get("path") or "") if material.type == "image" else (item["b"].get("text") or ""),
            "sizes": (_image_sizes(item) if material.type == "image" and has_annotations else {}),
            "record": {
                "status": record.get("status") or "",
                "status_cn": STATUS_CN.get(record.get("status") or "", "未审"),
                "tags": record.get("tags") or [],
                "note": record.get("note") or "",
                "annotations": record.get("annotations") or [],
                "reviewed_at": record.get("reviewed_at") or "",
                "round": record.get("round") or store.round,
                "stale": bool(record.get("src")) and record.get("src") != item.get("v"),
            },
        })

    stamp = datetime.now().strftime("%Y%m%d_%H%M")
    if fmt == "advice":
        body = advice_payload(material, rows, _cfg.get("tag_advice") or {})
        response = app.response_class(body, mimetype="application/json; charset=utf-8")
        response.headers["Content-Disposition"] = \
            f"attachment; filename=\"{material.id}_修正建议_{stamp}.json\""
        return response
    if fmt == "csv":
        if part == "annotations":
            body = store.export_annotations_csv(rows)
            filename = f"{material.id}_圈注明细_{stamp}.csv"
        else:
            body = store.export_csv(rows)
            filename = f"{material.id}_{stamp}.csv"
        response = app.response_class(body, mimetype="text/csv; charset=utf-8")
        response.headers["Content-Disposition"] = f"attachment; filename*=UTF-8''{quote(filename)}"
        return response
    body = store.export_json(rows)
    response = app.response_class(body, mimetype="application/json; charset=utf-8")
    response.headers["Content-Disposition"] = f"attachment; filename=\"{material.id}_{stamp}.json\""
    return response


def _image_sizes(item: dict) -> dict:
    """取素材两侧图片的像素尺寸，给圈注明细导出换算坐标用。图在读不到的尺寸时留空。"""
    from PIL import Image as _Image
    sizes = {}
    for side in ("a", "b"):
        path = (item.get(side) or {}).get("path")
        if not path:
            continue
        try:
            with _Image.open(path) as image:
                sizes[side] = {"w": image.width, "h": image.height}
        except Exception:
            continue
    return sizes


# ---------------------------------------------------------------- 启动

def build_app(config_path: str, reload_ok: bool = True):
    """（重新）加载配置。换配置时结果缓存要一起清掉，
    否则同一个进程里先开 A 配置再开 B 配置，会拿着 A 的结果文件继续读写。"""
    global _cfg, _registry
    _cfg = load_config(config_path)
    _registry = SetRegistry(_cfg)
    with _stores_lock:
        _stores.clear()
    return app


def port_owner(port: int) -> int | None:
    """谁在监听这个端口？拿不到就返回 None。

    Windows 的 netstat 中文输出是 GBK，而我们只关心其中的 ASCII 部分
    （TCP / 地址 / LISTENING / PID），所以用 errors="replace" 解出 ASCII 就够。
    """
    try:
        if os.name == "nt":
            out = subprocess.run(["netstat", "-ano", "-p", "TCP"],
                                 capture_output=True, text=True,
                                 encoding="utf-8", errors="replace", timeout=15).stdout or ""
            for line in out.splitlines():
                parts = line.split()
                if len(parts) >= 5 and parts[0].upper() == "TCP" \
                        and parts[3].upper() == "LISTENING" and parts[1].endswith(f":{port}"):
                    return int(parts[4])
        else:
            out = subprocess.run(["lsof", "-ti", f"tcp:{port}"],
                                 capture_output=True, text=True,
                                 encoding="utf-8", errors="replace", timeout=15).stdout or ""
            first = out.strip().splitlines()[0] if out.strip() else ""
            return int(first) if first.isdigit() else None
    except Exception:
        return None
    return None


def service_alive(host: str, port: int) -> bool:
    """这个端口上跑的是不是本工具（能正常应答 /api/config）。"""
    try:
        with urllib.request.urlopen(f"http://{host}:{port}/api/config", timeout=1.5) as response:
            return response.status == 200
    except Exception:
        return False


def kill_pid(pid: int) -> bool:
    try:
        if os.name == "nt":
            subprocess.run(["taskkill", "/F", "/PID", str(pid)],
                           capture_output=True, text=True,
                           encoding="utf-8", errors="replace", timeout=15)
        else:
            os.kill(pid, signal.SIGTERM)
        return True
    except Exception:
        return False


def write_pid_file(data_dir: str, port: int) -> Path:
    path = Path(data_dir) / "app.pid"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(f"{os.getpid()}\n{port}\n", encoding="utf-8")
    atexit.register(lambda: path.unlink(missing_ok=True))
    return path


def main() -> None:
    parser = argparse.ArgumentParser(description="图包素材审批工具")
    parser.add_argument("--config", default=str(ROOT / "config.json"),
                        help="配置文件路径（默认同目录 config.json）")
    parser.add_argument("--host", default=None, help="监听地址，默认取配置 host")
    parser.add_argument("--port", type=int, default=None, help="监听端口，默认取配置 port")
    parser.add_argument("--no-browser", action="store_true", help="启动后不自动打开浏览器")
    parser.add_argument("--restart", action="store_true",
                        help="先停掉占用该端口的旧实例，再启动")
    parser.add_argument("--probe", action="store_true",
                        help="只检查该端口上有没有本工具在跑：在跑退出码 3，没跑退出码 0")
    parser.add_argument("--open", action="store_true",
                        help="只打开浏览器指向已有实例，然后退出")
    args = parser.parse_args()

    build_app(args.config)
    host = args.host or _cfg.get("host") or "127.0.0.1"
    port = args.port or int(_cfg.get("port") or 8765)
    url = f"http://{host}:{port}/"

    if args.probe:
        raise SystemExit(3 if service_alive(host, port) else 0)
    if args.open:
        webbrowser.open(url)
        print(f"  已在浏览器打开： {url}")
        return

    # ---- 端口占用处理：避免「起第二个实例把自己数据搅乱」，也避免直接崩 ----
    owner = port_owner(port)
    if owner and owner != os.getpid():
        if service_alive(host, port):
            if args.restart:
                print(f"  端口 {port} 上已有本工具实例（PID {owner}），正在停掉它 …")
                kill_pid(owner)
                for _ in range(20):
                    time.sleep(0.3)
                    if not port_owner(port):
                        break
            else:
                print("=" * 62)
                print(f"  本工具已经在这个端口上运行了（PID {owner}）。")
                print(f"  直接打开： {url}")
                print("  如果想重启它：python app.py --restart")
                print("=" * 62)
                if not args.no_browser and _cfg.get("open_browser", True):
                    webbrowser.open(url)
                return
        else:
            print("=" * 62)
            print(f"  ✗ 端口 {port} 被别的程序占用了（PID {owner}）。")
            print(f"    换个端口：python app.py --port {port + 1}")
            print(f"    或强杀它：python app.py --restart")
            print("=" * 62)
            return
    elif owner == os.getpid():
        pass

    pid_file = write_pid_file(_cfg["data_dir"], port)

    cleared = apply_auto_reset_stale()

    print("=" * 62)
    print(f"  图包素材审批工具  v{__version__}")
    print("=" * 62)
    print(f"  进程 PID : {os.getpid()}   （记在 {pid_file}）")
    print(f"  配置文件 : {_cfg['config_path']}")
    print(f"  结果目录 : {_cfg['data_dir']}")
    for spec in _cfg["sets"]:
        try:
            material = _registry.get(spec["id"])
            if material.error:
                print(f"  [素材集] {material.name}  ⚠ {material.error}")
            else:
                store = _store_of(material)
                stats = _set_overview(material, store)
                extra = f"  已审 {stats['reviewed']}"
                if stats.get("stale"):
                    extra += f"  ⚠ 其中 {stats['stale']} 条源文件已变、结果可能过期"
                print(f"  [素材集] {material.name}  {len(material.items)} 条{extra}  第 {stats['round']} 轮")
                for warning in material.warnings:
                    print(f"            · {warning}")
        except MaterialError as exc:
            print(f"  [素材集] {spec['id']}  ⚠ {exc}")
    if cleared:
        print(f"  · auto_reset_stale 已生效：清掉 {cleared} 条源文件已变的结果")
    print("-" * 62)
    print(f"  浏览器打开： {url}")
    print("  按 Ctrl+C 停止")
    print("=" * 62)

    if not args.no_browser and _cfg.get("open_browser", True):
        threading.Timer(1.0, lambda: webbrowser.open(url)).start()
    app.run(host=host, port=port, debug=False, threaded=True)


if __name__ == "__main__":
    main()
