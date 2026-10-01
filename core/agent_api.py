# -*- coding: utf-8 -*-
"""给 AI agent / 脚本用的操作层。

同一个接口有两套实现，行为完全一致：

  AgentAPI    直接读写结果文件（不需要起服务）
  AgentClient 通过 HTTP 打给正在运行的网页版服务

为什么要有两套：网页版把审批结果缓存在内存里，每次标记会把整份结果写回磁盘。
如果服务在跑而 agent 直接改文件，服务下一次保存就会把 agent 的改动覆盖掉。
所以默认优先走 HTTP；只有服务没起来时才直连文件。

用 `make_backend()` 拿实现，它自己决定用哪套。
"""
from __future__ import annotations

import json
import os
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path

from .config import load_config
from .materials import MaterialError, SetRegistry, item_version, read_text_file
from .results import STATUS_CN, ResultStore
from .version import __version__
from .thumbs import thumbnail

from .agent_common import (AgentError, normalize_annotations, normalize_status,
                            suggest_tag, validate_tags)
from .agent_ext import (advice_payload, compare_sets as compare_sets_impl,
                        derive_set as derive_set_impl, drop_set as drop_set_impl,
                        export_crops as export_crops_impl, lookup as lookup_impl,
                        pair_image as pair_image_impl,
                        pair_images as pair_images_impl,
                        reload_config, review_batch as review_batch_impl)


def filter_items(material, records, *, status=None, tag=None, search=None,
                 stale_only=False, no_tags=False, annotated=False, noted=False,
                 group=None, ids=None) -> list[dict]:
    """按条件筛素材。status/tag/search 这些和网页版筛选栏是一套语义。"""
    want_status = normalize_status(status, allow_filter=True)
    id_set = set(ids) if ids else None
    query = (search or "").strip().lower()
    versions = {item["id"]: item.get("v", "") for item in material.items}

    matched = []
    for item in material.items:
        record = records.get(item["id"]) or {}
        if id_set is not None and item["id"] not in id_set:
            continue
        if want_status is not None and (record.get("status") or "") != want_status:
            continue
        if tag and tag not in (record.get("tags") or []):
            continue
        if no_tags and (record.get("tags") or []):
            continue
        if annotated and not (record.get("annotations") or []):
            continue
        if noted and not record.get("note"):
            continue
        if stale_only:
            recorded = record.get("src")
            if not (recorded and versions[item["id"]] and recorded != versions[item["id"]]):
                continue
        if group and item.get("group") != group:
            continue
        if query:
            haystack = " ".join([
                item["id"], item.get("name", ""), item.get("group", ""),
                " ".join(str(v) for v in (item.get("meta") or {}).values()),
            ]).lower()
            if query not in haystack:
                continue
        matched.append(item)
    return matched


def brief_item(item: dict, record: dict, *, is_stale: bool) -> dict:
    """列表里每条素材的简要信息（不带正文，避免上千条时响应过大）。"""
    return {
        "id": item["id"],
        "name": item.get("name", ""),
        "group": item.get("group", ""),
        "meta": item.get("meta") or {},
        "status": record.get("status") or "",
        "status_cn": STATUS_CN.get(record.get("status") or "", "未审"),
        "tags": record.get("tags") or [],
        "note": record.get("note") or "",
        "annotations": len(record.get("annotations") or []),
        "stale": is_stale,
        "has_source_a": item["a"]["exists"],
        "has_source_b": item["b"]["exists"],
    }


def detail_item(material, item: dict, record: dict, *, is_stale: bool,
                with_text: bool = True) -> dict:
    """单条素材的完整信息，含图片路径或文本正文，agent 拿它做判断。"""
    payload = brief_item(item, record, is_stale=is_stale)
    payload.update({
        "type": material.type,
        "reviewed_at": record.get("reviewed_at") or "",
        "round": record.get("round") or "",
        "annotation_list": record.get("annotations") or [],
        "source_labels": {"a": material.spec["a"]["label"] or "源A",
                          "b": material.spec["b"]["label"] or "源B"},
    })
    if material.type == "image":
        payload["files"] = {
            "a": (item["a"].get("path") or "") or None,
            "b": (item["b"].get("path") or "") or None,
        }
    elif with_text:
        texts = {}
        for side in ("a", "b"):
            info = item[side]
            if info.get("lazy") and not info.get("text"):
                texts[side] = read_text_file(info["path"])
            else:
                texts[side] = info.get("text") or ""
        payload["texts"] = texts
    return payload


class AgentAPI:
    """直接读写文件的操作层（不需要服务在跑）。"""

    def __init__(self, config_path: str, *, force_write: bool = False):
        self.cfg = load_config(config_path)
        self._registry = SetRegistry(self.cfg)
        self._stores: dict[str, ResultStore] = {}
        # 直连写入前的安全检查开关，见 _assert_can_write
        self.force_write = force_write

    def _assert_can_write(self, action: str) -> None:
        """直连改文件之前，确认没有服务正在跑。

        服务把结果缓存在内存里、每次标记整份写回磁盘，所以「服务在跑 + 直连改文件」
        = 改动会被服务下一次保存覆盖。这种静默丢数据比直接报错糟糕得多。
        """
        if self.force_write:
            return
        warning = pid_file_warning(self.cfg)
        if warning:
            raise AgentError(
                f"不能直接改文件（{action}）：{warning}")


    # -- 内部 ---------------------------------------------------------
    def _set(self, set_id: str):
        material = self._registry.get(set_id)
        if material.error:
            raise AgentError(f"素材集 {set_id} 配置有误：{material.error}")
        return material

    def _store(self, set_id: str) -> ResultStore:
        store = self._stores.get(set_id)
        if store is None:
            material = self._set(set_id)
            store = ResultStore(material.id, material.name,
                                Path(self.cfg["data_dir"]) / "results")
            self._stores[set_id] = store
        return store

    def _versions(self, material) -> dict:
        material.refresh_versions()
        return {item["id"]: item.get("v", "") for item in material.items}

    def _is_stale(self, item: dict, record: dict, versions: dict) -> bool:
        recorded = record.get("src")
        return bool(recorded) and bool(versions.get(item["id"])) and recorded != versions[item["id"]]

    # -- 读 -----------------------------------------------------------
    def status(self) -> dict:
        sets = []
        for spec in self.cfg["sets"]:
            material = self._registry.get(spec["id"])
            entry = {"id": spec["id"], "name": spec["name"], "type": spec["type"],
                     "note": spec.get("note", ""),
                     "source_a": spec["a"]["label"] or "源A",
                     "source_b": spec["b"]["label"] or "源B"}
            if material.error:
                entry.update({"count": 0, "error": material.error})
            else:
                store = self._store(spec["id"])
                versions = self._versions(material)
                stats = store.stats([i["id"] for i in material.items])
                stats["stale"] = len(store.stale_ids(versions))
                stats["round"] = store.round
                entry.update({"count": len(material.items), "error": None, **stats})
            sets.append(entry)
        payload = {
            "ok": True,
            "version": __version__,
            "config_path": self.cfg["config_path"],
            "data_dir": self.cfg["data_dir"],
            "server": f"http://{self.cfg.get('host', '127.0.0.1')}:{self.cfg.get('port', 8765)}/",
            "transport": "direct",
            "tags": self.cfg["tags"],
            "templates": self.cfg.get("templates") or [],
            "sets": sets,
        }
        warning = pid_file_warning(self.cfg)
        if warning:
            payload["warning"] = warning
        return payload

    def items(self, set_id: str, *, limit: int | None = None, offset: int = 0, **filters) -> dict:
        material = self._set(set_id)
        store = self._store(set_id)
        records = store.all()
        versions = self._versions(material)
        matched = filter_items(material, records, **filters)
        total = len(material.items)
        window = matched[offset:offset + limit] if limit else matched[offset:]
        return {
            "ok": True,
            "set_id": set_id,
            "set_name": material.name,
            "total": total,
            "matched": len(matched),
            "offset": offset,
            "limit": limit,
            "items": [brief_item(item, records.get(item["id"]) or {},
                                 is_stale=self._is_stale(item, records.get(item["id"]) or {}, versions))
                      for item in window],
        }

    def item(self, set_id: str, item_id: str) -> dict:
        material = self._set(set_id)
        item = material.by_id(item_id) or material.by_id(Path(str(item_id)).stem)
        if item is None:
            raise AgentError(f"素材集 {set_id} 里没有 id 为 {item_id!r} 的素材")
        store = self._store(set_id)
        record = store.get(item["id"]) or {}
        versions = self._versions(material)
        return {"ok": True,
                "item": detail_item(material, item, record,
                                    is_stale=self._is_stale(item, record, versions))}

    # -- 写 -----------------------------------------------------------
    def review(self, set_id: str, item_id: str, *, status=None, tags=None,
               note=None, annotations=None, template=None, lenient_tags: bool = False) -> dict:
        self._assert_can_write("写单条审批结果")
        tags = self._resolve_tags(tags, template, lenient=lenient_tags)
        material = self._set(set_id)
        item = material.by_id(item_id) or material.by_id(Path(str(item_id)).stem)
        if item is None:
            raise AgentError(f"素材集 {set_id} 里没有 id 为 {item_id!r} 的素材")
        store = self._store(set_id)
        record = store.save(
            item["id"],
            status=normalize_status(status),
            tags=[str(t) for t in tags] if tags is not None else None,
            note=note,
            annotations=normalize_annotations(annotations) if annotations is not None else None,
        )
        if record:
            store.stamp_sources({item["id"]: item.get("v", "")})
            record = store.get(item["id"]) or record
        versions = self._versions(material)
        return {"ok": True, "changed": 1,
                "item": detail_item(material, item, record, is_stale=self._is_stale(item, record, versions))}

    def batch(self, set_id: str, *, ids=None, status=None, tags=None, template=None,
              lenient_tags: bool = False, dry_run: bool = False, **filters) -> dict:
        """批量改状态。

        选择范围必须明确：给 ids，或给至少一个筛选条件（filter_status / filter_tag /
        search / group / stale_only / no_tags / annotated / noted）。
        不给就直接报错——免得一句「都通过」把整个素材集刷了。

        注意 status 是「要改成什么状态」，不是筛选条件；筛选状态请用 filter_status。
        """
        self._assert_can_write("批量改状态")
        tags = self._resolve_tags(tags, template, lenient=lenient_tags)
        material = self._set(set_id)
        store = self._store(set_id)
        records = store.all()
        versions = self._versions(material)

        # filter_status / filter_tag 只是更清楚的别名
        if "filter_status" in filters and "status" not in filters:
            filters["status"] = filters.pop("filter_status")
        if "filter_tag" in filters and "tag" not in filters:
            filters["tag"] = filters.pop("filter_tag")
        if filters.pop("filter_stale", None):
            filters["stale_only"] = True

        if ids:
            targets = filter_items(material, records, ids=ids)
            missing = [i for i in ids if not any(t["id"] == i for t in targets)]
            if missing:
                raise AgentError(f"这些 id 不存在：{', '.join(missing[:5])}")
        else:
            if not any(filters.get(k) for k in ("status", "tag", "search", "group",
                                                "stale_only", "no_tags", "annotated", "noted")):
                raise AgentError(
                    "批量操作必须指定范围：给 ids，或给至少一个筛选条件"
                    "（filter_status / filter_tag / search / group / stale_only / no_tags / annotated / noted）。"
                    "想作用于全部素材请显式传 filter_status='all'。")
            targets = filter_items(material, records, **filters)
        if not targets:
            return {"ok": True, "changed": 0, "matched": 0, "dry_run": dry_run,
                    "ids": [], "stats": self._stats(material, store)}
        if dry_run:
            return {"ok": True, "changed": 0, "matched": len(targets), "dry_run": True,
                    "ids": [t["id"] for t in targets], "stats": self._stats(material, store)}

        resolved = normalize_status(status)
        changed = store.save_many([t["id"] for t in targets], status=resolved, tags=tags)
        store.stamp_sources({t["id"]: t.get("v", "") for t in targets})
        return {"ok": True, "changed": changed, "matched": len(targets), "dry_run": False,
                "ids": [t["id"] for t in targets], "stats": self._stats(material, store)}

    def undo(self, set_id: str, item_id: str) -> dict:
        self._assert_can_write("撤销")
        material = self._set(set_id)
        item = material.by_id(item_id) or material.by_id(Path(str(item_id)).stem)
        if item is None:
            raise AgentError(f"素材集 {set_id} 里没有 id 为 {item_id!r} 的素材")
        store = self._store(set_id)
        from .results import NO_HISTORY
        previous = store.previous(item["id"])
        if previous is NO_HISTORY:
            raise AgentError("这条素材没有可撤销的上一步（撤销只在当前服务进程内有记录）")
        store.restore(item["id"], previous)
        record = store.get(item["id"]) or {}
        versions = self._versions(material)
        return {"ok": True, "item": detail_item(material, item, record,
                                                is_stale=self._is_stale(item, record, versions))}

    def clear_stale(self, set_id: str, *, ids=None, dry_run: bool = False) -> dict:
        if not dry_run:
            self._assert_can_write("清理过期结果")
        material = self._set(set_id)
        store = self._store(set_id)
        versions = self._versions(material)
        if ids:
            targets = [material.by_id(i) or material.by_id(Path(str(i)).stem) for i in ids]
            targets = [t for t in targets if t]
        else:
            stale_ids = set(store.stale_ids(versions))
            targets = [i for i in material.items if i["id"] in stale_ids]
        if dry_run:
            return {"ok": True, "removed": 0, "matched": len(targets), "dry_run": True,
                    "ids": [t["id"] for t in targets]}
        removed = store.clear_ids([t["id"] for t in targets])
        return {"ok": True, "removed": removed, "matched": len(targets), "dry_run": False,
                "ids": [t["id"] for t in targets]}

    def new_round(self, set_id: str) -> dict:
        self._assert_can_write("归档新一轮")
        material = self._set(set_id)
        store = self._store(set_id)
        info = store.start_new_round()
        from .thumbs import clear_cache
        info["cleared_thumbs"] = clear_cache(Path(self.cfg["data_dir"]) / "thumbs" / material.id)
        return {"ok": True, **info}

    def previous_round(self, set_id: str) -> dict:
        store = self._store(set_id)
        records = store.previous_round_records()
        ids = [i for i, r in records.items() if (r or {}).get("status") == "reject"]
        return {"ok": True, "round": max(1, store.round - 1) if records else None,
                "ids": ids, "total": len(records)}

    def export(self, set_id: str, *, fmt: str = "json", only: str = "all",
               out: str | None = None) -> dict:
        material = self._set(set_id)
        store = self._store(set_id)
        records = store.all()
        versions = self._versions(material)
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
                "id": item["id"], "name": item["name"], "group": item["group"],
                "meta": item["meta"],
                "sourceA": (item["a"].get("path") or "") if material.type == "image" else (item["a"].get("text") or ""),
                "sourceB": (item["b"].get("path") or "") if material.type == "image" else (item["b"].get("text") or ""),
                "sizes": {},
                "record": {
                    "status": record.get("status") or "",
                    "status_cn": STATUS_CN.get(record.get("status") or "", "未审"),
                    "tags": record.get("tags") or [],
                    "note": record.get("note") or "",
                    "annotations": record.get("annotations") or [],
                    "reviewed_at": record.get("reviewed_at") or "",
                    "round": record.get("round") or store.round,
                    "stale": self._is_stale(item, record, versions),
                },
            })
        fmt = (fmt or "json").lower()
        if fmt == "csv":
            content = store.export_csv(rows)
        elif fmt == "annotations":
            content = store.export_annotations_csv(rows)
            fmt = "csv"
        elif fmt == "advice":
            content = advice_payload(material, rows, self.cfg.get("tag_advice") or {})
        elif fmt == "json":
            content = store.export_json(rows)
        else:
            raise AgentError(
                f"不支持的导出格式：{fmt}（可用 json/csv/annotations/advice）")

        result = {"ok": True, "set_id": set_id, "format": fmt, "only": only, "count": len(rows)}
        if out:
            path = Path(out)
            if path.is_dir():
                path = path / f"{set_id}.{'json' if fmt == 'json' else 'csv'}"
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(content, encoding="utf-8")
            result["path"] = str(path)
        else:
            result["content"] = content
        return result

    def image(self, set_id: str, item_id: str, side: str = "b",
              max_size: int | None = None) -> dict:
        """拿到图片文件路径（给能自己读图的 agent）；max_size 时另存一张缩略图。"""
        material = self._set(set_id)
        if material.type != "image":
            raise AgentError(f"素材集 {set_id} 不是图片类型")
        item = material.by_id(item_id) or material.by_id(Path(str(item_id)).stem)
        if item is None:
            raise AgentError(f"素材集 {set_id} 里没有 id 为 {item_id!r} 的素材")
        side = str(side or "b").lower()
        if side not in ("a", "b"):
            raise AgentError("side 只能是 a 或 b")
        info = item[side]
        if not info.get("exists"):
            raise AgentError(f"这条素材没有{'源A' if side == 'a' else '源B'}图片："
                             f"{info.get('missing_reason', '')}")
        path = Path(info["path"])
        payload = {"ok": True, "id": item["id"], "side": side, "path": str(path),
                   "bytes": path.stat().st_size if path.exists() else 0}
        if max_size:
            cache = Path(self.cfg["data_dir"]) / "thumbs" / material.id / side
            thumb = thumbnail(path, cache, int(max_size), self.cfg["thumb"]["quality"])
            payload["thumb_path"] = str(thumb)
        return payload

    # -- 扩展能力（实现都在 core/agent_ext.py，两条通路共用）---------------
    def pair_image(self, set_id=None, item_id=None, *, a=None, b=None, **options) -> dict:
        material = store = None
        if not (a and b):
            material = self._set(set_id or "")
            store = self._store(material.id)
        return pair_image_impl(self.cfg, material=material, store=store, item_id=item_id,
                               path_a=a, path_b=b, **options)

    def pair_images(self, set_id: str, ids, **options) -> dict:
        material = self._set(set_id)
        return pair_images_impl(self.cfg, material, self._store(set_id), ids, **options)

    def export_crops(self, set_id: str, **options) -> dict:
        material = self._set(set_id)
        return export_crops_impl(material, self._store(set_id), **options)

    def review_batch(self, set_id: str, reviews, *, dry_run: bool = False,
                     partial_ok: bool = False, lenient_tags: bool = False) -> dict:
        if not dry_run:
            self._assert_can_write("批量写回审批结果")
        material = self._set(set_id)
        self._versions(material)
        return review_batch_impl(material, self._store(set_id), self.cfg, reviews,
                                 dry_run=dry_run, partial_ok=partial_ok,
                                 lenient_tags=lenient_tags)

    def derive_set(self, *, from_set: str, new_id: str, dry_run: bool = False,
                   **options) -> dict:
        if not dry_run:
            self._assert_can_write("派生新集合（要写配置）")
        source = self._set(from_set)
        info = derive_set_impl(self.cfg, source, self._store(from_set),
                               new_id=new_id, dry_run=dry_run, **options)
        if not dry_run:
            self.cfg, self._registry = reload_config(self.cfg)
            material = self._registry.get(new_id)
            info.update({"count": len(material.items), "error": material.error,
                         "warnings": material.warnings})
        return info

    def drop_set(self, set_id: str, *, dry_run: bool = False) -> dict:
        if not dry_run:
            self._assert_can_write("删除派生集合（要改配置）")
        info = drop_set_impl(self.cfg, set_id, dry_run=dry_run)
        if not dry_run:
            self.cfg, self._registry = reload_config(self.cfg)
        return info

    def lookup(self, item_id: str, *, sets=None) -> dict:
        return lookup_impl(self.cfg, self._registry, self._store, item_id, sets=sets)

    def compare_sets(self, set_id: str, other: str) -> dict:
        return compare_sets_impl(self._set(set_id), self._set(other),
                                 self._store(set_id), self._store(other))

    # -- 辅助 ---------------------------------------------------------
    def _resolve_tags(self, tags, template, *, lenient: bool = False) -> list[str] | None:
        """把 --template 或标签列表合起来，并校验是否都在预设里。"""
        resolved = None
        if template:
            found = next((t for t in self.cfg.get("templates", [])
                          if t["id"] == template or t["name"] == template), None)
            if found is None:
                known = "、".join(t["id"] for t in self.cfg.get("templates", [])) or "（没配模板）"
                raise AgentError(f"没有这个审批模板：{template!r}（可用：{known}）")
            resolved = list(found["tags"])
        if tags is not None:
            extra = [str(t) for t in tags]
            resolved = (resolved or []) + [t for t in extra if t not in (resolved or [])]
        if resolved is not None:
            validate_tags(resolved, self.cfg["tags"], strict=not lenient)
        return resolved

    def templates(self) -> dict:
        return {"ok": True, "templates": self.cfg.get("templates", []),
                "tags": self.cfg["tags"]}

    def _stats(self, material, store) -> dict:
        versions = self._versions(material)
        stats = store.stats([i["id"] for i in material.items])
        stats["stale"] = len(store.stale_ids(versions))
        stats["round"] = store.round
        return stats


class AgentClient:
    """通过 HTTP 打给正在运行的服务。接口和 AgentAPI 一致。"""

    def __init__(self, base_url: str, timeout: float = 30.0):
        self.base = base_url.rstrip("/")
        self.timeout = timeout

    def _get(self, path: str, **params) -> dict:
        url = f"{self.base}{path}"
        if params:
            url += "?" + urllib.parse.urlencode({k: v for k, v in params.items() if v is not None})
        try:
            with urllib.request.urlopen(url, timeout=self.timeout) as response:
                return json.loads(response.read().decode("utf-8"))
        except urllib.error.HTTPError as exc:
            raise self._explain(exc) from exc
        except (urllib.error.URLError, OSError) as exc:
            raise AgentError(f"连不上审批工具服务 {self.base}：{exc}") from exc

    def _post(self, path: str, payload: dict) -> dict:
        request = urllib.request.Request(
            f"{self.base}{path}", method="POST",
            data=json.dumps(payload, ensure_ascii=False).encode("utf-8"),
            headers={"Content-Type": "application/json"})
        try:
            with urllib.request.urlopen(request, timeout=self.timeout) as response:
                return json.loads(response.read().decode("utf-8"))
        except urllib.error.HTTPError as exc:
            raise self._explain(exc) from exc
        except (urllib.error.URLError, OSError) as exc:
            raise AgentError(f"连不上审批工具服务 {self.base}：{exc}") from exc

    @staticmethod
    def _explain(exc: urllib.error.HTTPError) -> AgentError:
        try:
            body = json.loads(exc.read().decode("utf-8"))
            return AgentError(body.get("error") or f"服务返回 {exc.code}")
        except Exception:
            return AgentError(f"服务返回 HTTP {exc.code}")

    # -- 接口 ---------------------------------------------------------
    def status(self) -> dict:
        return self._get("/api/agent/status")

    def items(self, set_id: str, *, limit=None, offset=0, **filters) -> dict:
        return self._get("/api/agent/items", set=set_id, limit=limit, offset=offset, **filters)

    def item(self, set_id: str, item_id: str) -> dict:
        return self._get("/api/agent/item", set=set_id, id=item_id)

    def review(self, set_id: str, item_id: str, **payload) -> dict:
        return self._post("/api/agent/review", {"set": set_id, "id": item_id, **payload})

    def batch(self, set_id: str, **payload) -> dict:
        return self._post("/api/agent/batch", {"set": set_id, **payload})

    def undo(self, set_id: str, item_id: str) -> dict:
        return self._post("/api/agent/undo", {"set": set_id, "id": item_id})

    def clear_stale(self, set_id: str, **payload) -> dict:
        return self._post("/api/agent/clear-stale", {"set": set_id, **payload})

    def new_round(self, set_id: str) -> dict:
        return self._post("/api/agent/new-round", {"set": set_id})

    def previous_round(self, set_id: str) -> dict:
        return self._get("/api/agent/previous-round", set=set_id)

    def export(self, set_id: str, **payload) -> dict:
        return self._post("/api/agent/export", {"set": set_id, **payload})

    def image(self, set_id: str, item_id: str, side: str = "b", max_size=None) -> dict:
        return self._get("/api/agent/image", set=set_id, id=item_id, side=side, max_size=max_size)


    def pair_image(self, set_id=None, item_id=None, *, a=None, b=None, **options) -> dict:
        return self._post("/api/agent/pair-image",
                          {"set": set_id, "id": item_id, "a": a, "b": b, **options})

    def pair_images(self, set_id: str, ids, **options) -> dict:
        return self._post("/api/agent/pair-images", {"set": set_id, "ids": list(ids), **options})

    def export_crops(self, set_id: str, **options) -> dict:
        return self._post("/api/agent/export-crops", {"set": set_id, **options})

    def review_batch(self, set_id: str, reviews, **options) -> dict:
        return self._post("/api/agent/review-batch", {"set": set_id, "reviews": reviews, **options})

    def derive_set(self, **options) -> dict:
        return self._post("/api/agent/derive-set", options)

    def drop_set(self, set_id: str, **options) -> dict:
        return self._post("/api/agent/drop-set", {"set": set_id, **options})

    def lookup(self, item_id: str, **options) -> dict:
        return self._get("/api/agent/lookup", id=item_id, **options)

    def compare_sets(self, set_id: str, other: str) -> dict:
        return self._get("/api/agent/compare", set=set_id, other=other)

    def templates(self) -> dict:
        return self._get("/api/agent/templates")


def server_alive(base_url: str, timeout: float = 1.5) -> bool:
    try:
        with urllib.request.urlopen(f"{base_url.rstrip('/')}/api/config", timeout=timeout) as response:
            return response.status == 200
    except Exception:
        return False


def port_open(port: int, host: str = "127.0.0.1", timeout: float = 0.4) -> bool:
    """这个端口上有东西在监听吗？（比查进程存活可靠：Windows 的 PID 会被复用）"""
    import socket
    try:
        with socket.socket() as sock:
            sock.settimeout(timeout)
            return sock.connect_ex((host, int(port))) == 0
    except OSError:
        return False


def pid_file_warning(cfg: dict) -> str | None:
    """直连写入前的安全检查。

    服务每次标记都会把整份结果写回磁盘，所以「服务在跑 + agent 直连改文件」=
    agent 的改动会被服务下一次保存覆盖。正常情况下 make_backend 会优先选 HTTP，
    但如果服务是用 --port 起的非默认端口，按配置探测就探不到。
    这里读 data/app.pid 兜底：里面记的端口上确实有服务在监听、而配置端口又不是它，
    就提醒 agent 换 --server，别直接改文件。
    """
    pid_path = Path(cfg["data_dir"]) / "app.pid"
    if not pid_path.exists():
        return None
    try:
        lines = pid_path.read_text(encoding="utf-8").split()
        pid = int(lines[0])
        port = int(lines[1])
    except (OSError, ValueError, IndexError):
        return None
    if port == int(cfg.get("port") or 0):
        return None                  # 就是配置里的端口，那探测失败说明它没跑
    if not port_open(port):
        return None                  # 端口没人听，pid 文件是残留，忽略
    return (f"审批工具服务正跑在端口 {port}（PID {pid}），而配置里写的是端口 "
            f"{cfg.get('port')}。直接改文件可能被那个服务覆盖，"
            f"请改用 --server http://127.0.0.1:{port}，或先停掉它。")


def make_backend(config_path: str, *, server: str | None = None, offline: bool = False,
                 force_write: bool = False):
    """选一个实现：服务在跑就走 HTTP，否则直连文件。

    server 传 "auto" 时按配置里的 host/port 探测。
    force_write 只在直连模式下有意义：跳过「服务在跑就别直连写」的保护。
    """
    cfg = load_config(config_path)
    base = server
    if base == "auto" or (base is None and not offline):
        base = f"http://{cfg.get('host', '127.0.0.1')}:{cfg.get('port', 8765)}"
    if offline or not base:
        return AgentAPI(config_path, force_write=force_write)
    if server_alive(base):
        return AgentClient(base)
    return AgentAPI(config_path, force_write=force_write)
