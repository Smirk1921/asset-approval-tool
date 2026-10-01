# -*- coding: utf-8 -*-
"""审批工具的扩展能力：拼接对照图、圈选裁剪导出、批量写回、派生集合、跨集合查询。

这些函数只依赖显式传进来的对象（cfg / material / store / registry），
所以「HTTP 服务」和「agent 直连文件」两条通路能共用同一份实现，行为不会走偏。
"""
from __future__ import annotations

import json
import shutil
import zipfile
from pathlib import Path

from .agent_common import AgentError, normalize_annotations, normalize_status, validate_tags
from .config import load_config, sets_dir_for
from .images import compose_pair, crop_region, open_rgb, option_hash, safe_name
from .materials import SetRegistry, file_stamp
from .results import STATUS_CN


# ---------------------------------------------------------------- 拼接对照图

def pair_image(cfg: dict, *, material=None, store=None, item_id: str | None = None,
               path_a=None, path_b=None, layout: str = "horizontal", max_width=2048,
               max_height=None, gap: int = 8, labels=None, out=None,
               draw_annotations: bool = True, use_cache: bool = True) -> dict:
    """把源A/源B 拼成一张对照图。

    两种用法：
        给 material+store+item_id   → 素材集里某一张
        给 path_a+path_b            → 任意两张图（比如对比上一版和这一版渲染）

    横向拼接时两侧按等高对齐（尺寸归一），原图和成品分辨率不同也不会一边大一边小。
    """
    free_mode = bool(path_a and path_b) and material is None
    if free_mode:
        file_a, file_b = Path(path_a), Path(path_b)
        if not file_a.exists() or not file_b.exists():
            raise AgentError("指定的图片不存在")
        label_a, label_b = file_a.stem, file_b.stem
        boxes = ([], [])
        cache_scope = "_free"
        cache_name = f"{safe_name(file_a.stem, 16)}_{safe_name(file_b.stem, 16)}"
        for path in (file_a, file_b):
            if not path.exists():
                raise AgentError(f"图片不存在：{path}")
    else:
        if material is None:
            raise AgentError("要么给素材集和素材 id，要么直接给两张图的路径")
        if material.type != "image":
            raise AgentError(f"素材集 {material.id} 不是图片类型，没法拼对照图")
        item = material.by_id(item_id or "")
        if item is None:
            raise AgentError(f"素材集 {material.id} 里没有 id 为 {item_id!r} 的素材")
        for side in ("a", "b"):
            if not item[side].get("exists"):
                raise AgentError(
                    f"{item['id']} 缺{'源A' if side == 'a' else '源B'}图片："
                    f"{item[side].get('missing_reason', '')}")
        file_a, file_b = Path(item["a"]["path"]), Path(item["b"]["path"])
        label_a = material.spec["a"]["label"] or "源A"
        label_b = material.spec["b"]["label"] or "源B"
        record = (store.get(item["id"]) if store else None) or {}
        annotations = (record.get("annotations") or []) if draw_annotations else []
        boxes = ([x for x in annotations if x.get("side") == "a"],
                 [x for x in annotations if x.get("side") == "b"])
        cache_scope = material.id
        cache_name = safe_name(item["id"], 60)

    if labels:
        label_a, label_b = labels

    options = dict(layout=layout, max_width=max_width, max_height=max_height, gap=gap,
                   label_a=label_a, label_b=label_b, boxes=sum(len(bx) for bx in boxes))
    fingerprint = "%s_%s_%s" % (file_stamp(file_a), file_stamp(file_b), option_hash(**options))
    cache_path = Path(cfg["data_dir"]) / "pair_cache" / cache_scope / f"{cache_name}_{fingerprint}.png"

    cached = bool(use_cache and cache_path.exists())
    if cached:
        image = open_rgb(cache_path)
    else:
        image = compose_pair(file_a, file_b, layout=layout, gap=gap, max_width=max_width,
                             max_height=max_height, labels=(label_a, label_b), boxes=boxes)
        cache_path.parent.mkdir(parents=True, exist_ok=True)
        image.save(cache_path, "PNG")

    result_path = cache_path
    if out:
        result_path = Path(out)
        if result_path.is_dir():
            result_path = result_path / cache_path.name
        result_path.parent.mkdir(parents=True, exist_ok=True)
        if result_path != cache_path:
            shutil.copyfile(cache_path, result_path)

    return {
        "ok": True,
        "id": None if free_mode else item_id,
        "path": str(result_path),
        "cache_path": str(cache_path),
        "layout": layout,
        "size": [image.width, image.height],
        "cached": cached,
        "bytes": result_path.stat().st_size,
        "labels": [label_a, label_b],
        "boxes_drawn": sum(len(bx) for bx in boxes),
    }


def pair_images(cfg: dict, material, store, ids, *, out_dir, **options) -> dict:
    """批量拼接（几百张一次拿完），省掉每张一次进程启动的开销。"""
    ids = [str(i) for i in ids]
    out_dir = Path(out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    pairs, failures = [], []
    for item_id in ids:
        target = out_dir / f"{Path(item_id).stem}_pair.png"
        try:
            info = pair_image(cfg, material=material, store=store, item_id=item_id,
                              out=target, **options)
            pairs.append({"id": item_id, "path": info["path"],
                          "size": info["size"], "cached": info["cached"]})
        except AgentError as exc:
            failures.append({"id": item_id, "error": str(exc)})
    return {"ok": True, "set_id": material.id, "out_dir": str(out_dir), "total": len(ids),
            "succeeded": len(pairs), "failed": len(failures),
            "failures": failures, "pairs": pairs}


# ---------------------------------------------------------------- 圈选裁剪导出

def export_crops(material, store, *, out_dir=None, zip_path=None, only: str = "problem",
                 side=None, padding: int = 8, scale: float = 1.0,
                 annotated_only: bool = True) -> dict:
    """把圈注框裁成小图导出——圈出缺失图标的位置，导出来就是现成的抠图素材。

    side 不给就按每条圈注自己标的边裁；给了就强制只裁这一边。
    """
    if not out_dir and not zip_path:
        raise AgentError("得给一个去处：--out 目录 或 --zip 压缩包")
    if material.type != "image":
        raise AgentError(f"素材集 {material.id} 不是图片类型")
    records = store.all()
    work_dir = Path(out_dir) if out_dir else Path(zip_path).with_suffix("")
    work_dir.mkdir(parents=True, exist_ok=True)

    produced, skipped = [], []
    for item in material.items:
        record = records.get(item["id"]) or {}
        annotations = record.get("annotations") or []
        if only == "problem":
            status = record.get("status") or ""
            if status not in ("reject", "flag") and not annotations and not record.get("tags"):
                continue
        if annotated_only and not annotations:
            continue
        for index, annotation in enumerate(annotations, 1):
            use_side = side or annotation.get("side") or "b"
            if use_side not in ("a", "b"):
                use_side = "b"
            info = item[use_side]
            if not info.get("exists"):
                skipped.append({"id": item["id"], "index": index,
                                "error": f"缺{'源A' if use_side == 'a' else '源B'}图片"})
                continue
            note = safe_name(annotation.get("note") or "", 16)
            name = f"{Path(item['id']).stem}_{index}_{use_side}_{note}.png"
            try:
                path, rect = crop_region(info["path"], annotation, work_dir / name,
                                         padding=padding, scale=scale)
            except Exception as exc:  # 单张坏图不要带崩整批
                skipped.append({"id": item["id"], "index": index, "error": str(exc)})
                continue
            produced.append({"id": item["id"], "index": index, "side": use_side,
                             "note": annotation.get("note") or "",
                             "path": str(path), "pixels": list(rect)})

    result = {"ok": True, "set_id": material.id, "count": len(produced),
              "out_dir": str(work_dir), "crops": produced, "skipped": skipped}
    if zip_path:
        archive = Path(zip_path)
        archive.parent.mkdir(parents=True, exist_ok=True)
        with zipfile.ZipFile(archive, "w", zipfile.ZIP_DEFLATED) as zf:
            for entry in produced:
                zf.write(entry["path"], Path(entry["path"]).name)
        result["zip"] = str(archive)
        if not out_dir:
            for entry in produced:
                try:
                    Path(entry["path"]).unlink()
                except OSError:
                    pass
            result["crops"] = [dict(e, path="(已打包)") for e in produced]
    return result


# ---------------------------------------------------------------- 批量写回

def review_batch(material, store, cfg: dict, reviews, *, dry_run: bool = False,
                 partial_ok: bool = False, lenient_tags: bool = False) -> dict:
    """一次写回多条审批结果（agent 跑完一批的典型用法）。

    先把所有条目校验完再动手：有一条不合法就整批不写，不会出现"写了一半"，
    同时省掉 N 次整份写盘。
    """
    if not isinstance(reviews, list):
        raise AgentError("reviews 必须是数组，每项形如 {id,status,tags,note,annotations}")
    known_ids = {item["id"] for item in material.items}
    versions = {item["id"]: item.get("v", "") for item in material.items}

    prepared, failures = [], []
    for index, entry in enumerate(reviews, 1):
        if not isinstance(entry, dict):
            failures.append({"index": index, "error": "不是对象"})
            continue
        item_id = str(entry.get("id") or "").strip()
        if not item_id:
            failures.append({"index": index, "error": "缺 id"})
            continue
        if item_id not in known_ids:
            failures.append({"id": item_id, "index": index, "error": "素材集里没有这个 id"})
            continue
        try:
            status = normalize_status(entry.get("status"))
            tags = entry.get("tags")
            if tags is not None:
                tags = [str(t) for t in tags]
                validate_tags(tags, cfg["tags"], strict=not lenient_tags)
            annotations = (normalize_annotations(entry.get("annotations"))
                           if entry.get("annotations") is not None else None)
        except AgentError as exc:
            failures.append({"id": item_id, "index": index, "error": str(exc)})
            continue
        prepared.append({"id": item_id, "status": status, "tags": tags,
                         "note": entry.get("note"), "annotations": annotations})

    if failures and not partial_ok:
        return {"ok": False, "set_id": material.id, "total": len(reviews), "succeeded": 0,
                "would_write": len(prepared), "failed": len(failures),
                "failures": failures, "applied": False, "dry_run": dry_run,
                "error": f"有 {len(failures)} 条不合法，整批没有写入"
                         f"（想只写合法的加 partial_ok）"}

    if dry_run:
        return {"ok": True, "set_id": material.id, "dry_run": True, "total": len(reviews),
                "would_write": len(prepared), "failed": len(failures),
                "failures": failures, "ids": [p["id"] for p in prepared]}

    entries = [{k: v for k, v in p.items() if v is not None} for p in prepared]
    changed = store.save_batch(entries)
    store.stamp_sources({p["id"]: versions.get(p["id"], "") for p in prepared})
    stats = store.stats([i["id"] for i in material.items])
    stats["stale"] = len(store.stale_ids(versions))
    stats["round"] = store.round
    return {"ok": True, "set_id": material.id, "total": len(reviews),
            "succeeded": changed, "failed": len(failures), "failures": failures,
            "applied": True, "stats": stats}


# ---------------------------------------------------------------- 派生集合

def derive_set(cfg: dict, source_material, source_store, *, new_id: str, name=None,
               a_root=None, b_root=None, only_ids=None, only_from=None,
               dry_run: bool = False) -> dict:
    """从一个素材集派生新集合，写到 sets.d/<new_id>.json。

    典型用法：大集合审完一轮、重渲后想「只看这批、单独一轮」，
    既不用把文件拷来拷去，也不用改手写的 config.json。
    """
    if any(s["id"] == new_id for s in cfg["sets"]):
        raise AgentError(f"素材集 {new_id!r} 已经存在了，换个 id")

    selected = None
    if only_ids:
        selected = [str(i) for i in only_ids]
    elif only_from:
        records = source_store.all()
        wanted = normalize_status(only_from, allow_filter=True)
        selected = [item["id"] for item in source_material.items
                    if wanted is None
                    or (records.get(item["id"], {}).get("status") or "") == wanted]
        if not selected:
            raise AgentError(f"按 {only_from!r} 挑不出任何素材")
    if selected is not None:
        unknown = [i for i in selected if source_material.by_id(i) is None]
        if unknown:
            raise AgentError(f"这些 id 在源集合里不存在：{'、'.join(unknown[:5])}")

    spec = json.loads(json.dumps(source_material.spec, ensure_ascii=False))
    spec.pop("from_file", None)
    spec["id"] = new_id
    spec["name"] = name or f"{source_material.name}（派生）"
    spec["note"] = (f"派生自 {source_material.id}"
                    + (f"，只含 {len(selected)} 张" if selected is not None else ""))
    if a_root:
        spec["a"] = dict(spec["a"], root=str(a_root))
    if b_root:
        spec["b"] = dict(spec["b"], root=str(b_root))
    if selected is not None:
        spec["only_ids"] = sorted(selected)
    else:
        spec.pop("only_ids", None)

    target = sets_dir_for(cfg["config_path"]) / f"{new_id}.json"
    if dry_run:
        return {"ok": True, "dry_run": True, "would_write": str(target), "set": spec,
                "item_count_hint": len(selected) if selected is not None else None}
    if target.exists():
        raise AgentError(f"文件已存在，先确认要不要覆盖：{target}")

    target.parent.mkdir(parents=True, exist_ok=True)
    payload = {"_说明": f"由工具派生（源集合 {source_material.id}）；"
                        f"改这个文件就等于改这个集合，删掉它就等于删掉这个集合。",
               **spec}
    target.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    return {"ok": True, "dry_run": False, "set_id": new_id, "config_file": str(target),
            "item_count_hint": len(selected) if selected is not None else None}


def drop_set(cfg: dict, set_id: str, *, dry_run: bool = False) -> dict:
    """删掉一个派生出来的集合（手写在 config.json 里的不在这里删）。"""
    spec = next((s for s in cfg["sets"] if s["id"] == set_id), None)
    if spec is None:
        raise AgentError(f"没有这个素材集：{set_id}")
    if not spec.get("from_file"):
        raise AgentError(f"{set_id} 是从 config.json 读到的（手写集合），"
                         f"工具不替你改手写配置；要删请在 config.json 里删")
    if not dry_run:
        Path(spec["from_file"]).unlink(missing_ok=True)
    return {"ok": True, "set_id": set_id, "dropped": True, "dry_run": dry_run,
            "config_file": spec["from_file"]}


# ---------------------------------------------------------------- 跨集合查询

def lookup(cfg: dict, registry, store_of, item_id: str, *, sets=None) -> dict:
    """同一个素材 id 在各个集合里的审批结论。

    迭代渲染时最常问的就是「这张卡上一轮是什么结论、是不是我打回的那张」。
    """
    needle = str(item_id)
    wanted = set(sets) if sets else None
    found = []
    for spec in cfg["sets"]:
        if wanted and spec["id"] not in wanted:
            continue
        material = registry.get(spec["id"])
        if material.error:
            continue
        item = material.by_id(needle) or material.by_id(Path(needle).stem)
        if item is None:
            continue
        record = store_of(material.id).get(item["id"]) or {}
        found.append({
            "set_id": material.id, "set_name": material.name,
            "id": item["id"], "name": item.get("name", ""),
            "status": record.get("status") or "",
            "status_cn": STATUS_CN.get(record.get("status") or "", "未审"),
            "tags": record.get("tags") or [], "note": record.get("note") or "",
            "annotations": len(record.get("annotations") or []),
            "reviewed_at": record.get("reviewed_at") or "",
            "round": record.get("round") or "",
        })
    return {"ok": True, "id": needle, "found": len(found), "sets": found}


def compare_sets(left_material, right_material, left_store, right_store) -> dict:
    """两个集合按素材 id 对比结论：「上轮打回的这轮怎样了」。"""
    left_records = left_store.all()
    right_records = right_store.all()
    left_ids = {item["id"] for item in left_material.items}
    right_ids = {item["id"] for item in right_material.items}

    rows, changed = [], 0
    for item_id in sorted(left_ids | right_ids):
        a = left_records.get(item_id) or {}
        b = right_records.get(item_id) or {}
        status_a, status_b = a.get("status") or "", b.get("status") or ""
        if item_id in right_ids and status_a != status_b:
            changed += 1
        rows.append({
            "id": item_id,
            "in_left": item_id in left_ids, "in_right": item_id in right_ids,
            "left_status": status_a, "left_status_cn": STATUS_CN.get(status_a, "未审"),
            "right_status": status_b, "right_status_cn": STATUS_CN.get(status_b, "未审"),
            "left_tags": a.get("tags") or [], "right_tags": b.get("tags") or [],
            "changed": status_a != status_b,
        })
    return {"ok": True, "left": left_material.id, "right": right_material.id,
            "left_name": left_material.name, "right_name": right_material.name,
            "total": len(rows), "changed": changed,
            "only_in_left": sorted(left_ids - right_ids),
            "only_in_right": sorted(right_ids - left_ids),
            "rows": rows}


# ---------------------------------------------------------------- 修正建议导出

def advice_payload(material, rows, advice_map: dict) -> str:
    """把标签展开成修正建议。

    工具本身不含任何项目专属话术——说什么由配置的 tag_advice 决定；
    没配到话术的标签，只把标签名和备注原样带出去。
    """
    advice_map = advice_map or {}
    out = []
    for row in rows:
        record = row.get("record") or {}
        tags = record.get("tags") or []
        if not tags and not record.get("note") and not record.get("annotations"):
            continue
        advices = [advice_map[t] for t in tags if advice_map.get(t)]
        unknown = [t for t in tags if not advice_map.get(t)]
        entry = {
            "id": row["id"],
            "名称": row.get("name", ""),
            "状态": record.get("status_cn") or STATUS_CN.get(record.get("status") or "", "未审"),
            "问题标签": tags,
            "修正建议": advices,
            "原审批备注": record.get("note") or "",
            "圈选": [{k: a.get(k) for k in ("side", "x", "y", "w", "h", "note")}
                     for a in (record.get("annotations") or [])],
            "元数据": row.get("meta") or {},
        }
        if unknown:
            entry["未配话术的标签"] = unknown
        out.append(entry)
    return json.dumps({"set_id": material.id, "set_name": material.name,
                       "count": len(out), "items": out}, ensure_ascii=False, indent=2)


def reload_config(cfg: dict):
    """配置变了之后重新加载（派生/删除集合后用）。"""
    fresh = load_config(cfg["config_path"])
    return fresh, SetRegistry(fresh)
