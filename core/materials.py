# -*- coding: utf-8 -*-
"""素材集扫描与配对：把配置里的「源A / 源B」变成一份可审批的素材清单。

支持三种配对方式：
  basename   按文件名配对（可递归扫描；渲染产物分目录存放时用它）
  manifest   按元数据清单配对（清单里每条记录对应一张素材，文件名由模板生成）
  json_key   按 JSON 顶层键配对（文本素材：英文原文 JSON ↔ 中文译文 JSON）

代码中不出现任何具体项目路径，全部来自 config.json。
"""
from __future__ import annotations

import base64
import fnmatch
import hashlib
import json
import os
import re
from pathlib import Path

_STATUS_SKIP = {"缺少源文件", "缺失"}
# 分面（筛选下拉框）允许的最大选项数，超过就只靠搜索框
_MAX_FACET_VALUES = 30
# 单个文本文件最多读多少字节，防止误配到大文件把内存吃光
_MAX_TEXT_BYTES = 4 * 1024 * 1024


def read_text_file(path: str) -> str:
    """读取文本素材文件（详情页按需调用）。"""
    file = Path(path)
    try:
        if file.stat().st_size > _MAX_TEXT_BYTES:
            with file.open("r", encoding="utf-8", errors="replace") as handle:
                return handle.read(_MAX_TEXT_BYTES) + "\n…（文件过大，已截断）"
        return file.read_text(encoding="utf-8", errors="replace")
    except OSError as exc:
        return f"（读取失败：{exc}）"


def file_stamp(path: str) -> str:
    """源文件指纹（mtime_ns + size），公开版本给 agent_ext 用。"""
    return _file_stamp(path)


def _file_stamp(path: str) -> str:
    try:
        stat = Path(path).stat()
        return f"{stat.st_mtime_ns:x}{stat.st_size:x}"
    except OSError:
        return ""


def _norm(path: str) -> str:
    return os.path.normcase(os.path.abspath(path))


def scan_stamps(roots, max_depth: int = 16) -> dict:
    """一次遍历目录，拿到 {规范化路径: mtime+size 指纹}。

    用 os.scandir：Windows 的目录枚举本身就带回文件的 mtime/size，
    所以 stat() 不用额外再问系统一次，比逐文件 os.stat() 快一个数量级
    （实测 1179 个文件：6ms vs 109ms）。
    """
    stamps: dict[str, str] = {}

    def walk(directory: str, depth: int) -> None:
        if depth > max_depth:
            return
        try:
            with os.scandir(directory) as entries:
                for entry in entries:
                    try:
                        if entry.is_dir(follow_symlinks=False):
                            walk(entry.path, depth + 1)
                        elif entry.is_file(follow_symlinks=False):
                            stat = entry.stat()
                            stamps[_norm(entry.path)] = f"{stat.st_mtime_ns:x}{stat.st_size:x}"
                    except OSError:
                        continue
        except OSError:
            return

    for root in roots:
        if root and os.path.isdir(root):
            walk(root, 0)
    return stamps


def file_stamps(paths) -> dict:
    """给若干个具体文件算指纹（文本素材集的源就是几个 JSON/文本文件，不是目录）。"""
    stamps: dict[str, str] = {}
    for path in paths:
        if not path or not os.path.isfile(path):
            continue
        try:
            stat = os.stat(path)
        except OSError:
            continue
        stamps[_norm(path)] = f"{stat.st_mtime_ns:x}{stat.st_size:x}"
    return stamps


def item_version(item: dict, set_type: str) -> str:
    """素材内容指纹：源文件一改，这个值就变。

    两个用途：
      1) 拼进图片 URL（?v=...），源文件更新后浏览器必然重新拉图，不会再用旧缓存；
      2) 存进审批结果，用来判断「这条结果是不是在我审批之后源文件又被改过」。
    图片以成品（源B）为准，成品缺失时退回原图。
    """
    if set_type == "image":
        for side in ("b", "a"):
            path = (item.get(side) or {}).get("path")
            if path:
                stamp = _file_stamp(path)
                if stamp:
                    return stamp
        return ""

    parts = []
    for side in ("b", "a"):
        info = item.get(side) or {}
        if info.get("text"):
            parts.append(info["text"])
        elif info.get("path"):
            parts.append(_file_stamp(info["path"]))
    parts = [p for p in parts if p]
    if not parts:
        return ""
    return hashlib.sha1("\x00".join(parts).encode("utf-8", "replace")).hexdigest()[:16]


class MaterialError(Exception):
    pass


def urlsafe_key(item_id: str) -> str:
    """把任意素材 id 编成 URL 安全、稳定的短键。"""
    return base64.urlsafe_b64encode(item_id.encode("utf-8")).decode("ascii").rstrip("=")


def _extract_text(record, spec: str) -> str:
    """从一条记录里取出文本。

    spec 支持：
      "zh"            普通字段
      "lines"         数组字段（元素为字符串/对象，直接拼接）
      "lines[].t"     对象数组的某个子字段
    """
    if not isinstance(record, dict):
        return ""
    if "[]" in spec:
        base, _, sub = spec.partition("[]")
        value = record.get(base.strip())
        if not isinstance(value, list):
            return ""
        parts = []
        for element in value:
            if sub.startswith("."):
                if isinstance(element, dict):
                    parts.append(str(element.get(sub[1:], "")))
                else:
                    parts.append(str(element))
            else:
                parts.append(str(element))
        return "\n".join(p for p in parts if p != "")
    value = record.get(spec)
    if value is None:
        return ""
    if isinstance(value, list):
        return "\n".join(str(v) for v in value)
    return str(value)


def _load_json(path: str):
    try:
        return json.loads(Path(path).read_text(encoding="utf-8"))
    except FileNotFoundError as exc:
        raise MaterialError(f"找不到文件：{path}") from exc
    except json.JSONDecodeError as exc:
        raise MaterialError(f"JSON 解析失败（{path}）：{exc}") from exc


def _matches(name: str, include: list[str], exclude: list[str]) -> bool:
    if include and not any(fnmatch.fnmatch(name, pat) for pat in include):
        return False
    if exclude and any(fnmatch.fnmatch(name, pat) for pat in exclude):
        return False
    return True


def _walk(root: str, include: list[str], exclude: list[str], recursive: bool,
          skip_dirs: list[str]) -> dict[str, dict]:
    """扫描目录，返回 {文件名: {"path": 绝对路径, "group": 子目录名}}。"""
    base = Path(root)
    if not base.exists():
        raise MaterialError(f"素材目录不存在：{root}")
    found: dict[str, dict] = {}
    if recursive:
        for path in base.rglob("*"):
            if not path.is_file():
                continue
            rel_parts = path.relative_to(base).parts
            if any(fnmatch.fnmatch(part, pat) for part in rel_parts[:-1] for pat in skip_dirs):
                continue
            if not _matches(path.name, include, exclude):
                continue
            group = rel_parts[0] if len(rel_parts) > 1 else ""
            if group in ("", "."):
                group = "根目录"
            found.setdefault(path.name, {"path": str(path), "group": group})
    else:
        for path in base.iterdir():
            if path.is_file() and _matches(path.name, include, exclude):
                found.setdefault(path.name, {"path": str(path), "group": "根目录"})
    return found


class MaterialSet:
    """一个素材集：扫描一次，得到素材清单 + 元数据 + 分面统计。"""

    def __init__(self, spec: dict):
        self.spec = spec
        self.id = spec["id"]
        self.name = spec["name"]
        self.type = spec["type"]
        self.items: list[dict] = []
        self._by_key: dict[str, dict] = {}
        self._by_id: dict[str, dict] = {}
        self.facets: dict[str, list] = {}
        self.warnings: list[str] = []
        self.error: str | None = None
        self._meta_lookup: dict[str, dict] = {}
        self._meta_records: list[dict] = []
        self._source_stamps: dict = {}
        self._build()
        self._source_stamps = file_stamps(self._text_source_files())

    # ---------------------------------------------------------------- 构建
    def _build(self) -> None:
        # 允许重复构建（源文件改了要重建），先清掉上一次的结果，
        # 否则 items 会一路 append 下去直接翻倍。
        self.items = []
        self._by_key = {}
        self._by_id = {}
        self.facets = {}
        self.warnings = []
        self.error = None
        self._meta_lookup = {}
        self._meta_records = []
        try:
            self._load_meta()
            if self.spec["pair"] == "json_key":
                self._build_json_key()
            elif self.spec["pair"] == "manifest":
                self._build_manifest()
            else:
                self._build_basename()
            self._apply_only_ids()
            self._finalize()
        except MaterialError as exc:
            self.error = str(exc)

    def _apply_only_ids(self) -> None:
        """只保留 only_ids 白名单里的素材（没配就是全部）。

        用途：从一个大素材集里「只看这批」，不必为了隔离而把文件拷到新目录。
        """
        only = self.spec.get("only_ids") or []
        if not only:
            return
        keep = {str(i) for i in only}
        matched = {item["id"] for item in self.items}
        self.items = [item for item in self.items if item["id"] in keep]
        missing = keep - matched
        if missing:
            preview = "、".join(sorted(missing)[:5])
            more = f" 等 {len(missing)} 个" if len(missing) > 5 else ""
            self.warnings.append(f"only_ids 里有没匹配到的 id：{preview}{more}")

    # -- 元数据清单 -------------------------------------------------------
    def _load_meta(self) -> None:
        meta = self.spec.get("meta")
        if not meta:
            return
        data = _load_json(meta["path"])
        if isinstance(data, list):
            self._meta_records = [r for r in data if isinstance(r, dict)]
            meta["kind"] = "list"
        elif isinstance(data, dict):
            self._meta_records = [{"__key__": k, **(v if isinstance(v, dict) else {"value": v})}
                                  for k, v in data.items()]
            meta["kind"] = "map"
        else:
            raise MaterialError(f"元数据文件格式不支持：{meta['path']}")

        key_template = meta.get("key_template") or ""
        if key_template:
            # 由模板反推候选文件名，建立「文件名 -> 记录」索引
            for record in self._meta_records:
                try:
                    filename = key_template.format(**{
                        k: v for k, v in record.items() if isinstance(v, (str, int, float))
                    })
                except (KeyError, IndexError, ValueError):
                    continue
                self._meta_lookup.setdefault(filename, record)
                self._meta_lookup.setdefault(Path(filename).stem, record)

    def _meta_for(self, item_id: str, name: str) -> dict:
        meta = self.spec.get("meta")
        if not meta:
            return {}
        record = None
        join = meta.get("join") or "filename"
        if join == "key":
            record = next((r for r in self._meta_records if r.get("__key__") == item_id), None)
        if record is None and self._meta_lookup:
            record = self._meta_lookup.get(name) or self._meta_lookup.get(Path(name).stem)
        if record is None:
            return {}
        out = {}
        for field in meta.get("fields") or []:
            if field in record:
                label = meta.get("rename", {}).get(field, field)
                value = record[field]
                if isinstance(value, (dict, list)):
                    value = json.dumps(value, ensure_ascii=False)
                out[label] = "" if value is None else str(value)
        return out

    def _name_from_meta(self, record: dict) -> str:
        meta = self.spec.get("meta") or {}
        field = meta.get("name_field")
        if field and record.get(field):
            return str(record[field])
        return ""

    # -- 三种配对方式 ------------------------------------------------------
    def _build_basename(self) -> None:
        spec = self.spec
        skip = list(spec["exclude"])
        found_a = {}
        found_b = {}
        if spec["a"]["kind"] == "dir":
            found_a = _walk(spec["a"]["root"], spec["include"], spec["exclude"],
                            spec["recursive"], skip)
        if spec["b"]["kind"] == "dir":
            found_b = _walk(spec["b"]["root"], spec["include"], spec["exclude"],
                            spec["recursive"], skip)

        if spec["items_from"] == "a":
            names = list(found_a)
        elif spec["items_from"] == "b":
            names = list(found_b)
        else:
            names = sorted(set(found_a) | set(found_b))

        for name in names:
            a_hit, b_hit = found_a.get(name), found_b.get(name)
            group = (b_hit or a_hit or {}).get("group", "")
            self.items.append(self._make_item(
                item_id=name,
                name=name,
                group=group,
                a_path=(a_hit or {}).get("path"),
                b_path=(b_hit or {}).get("path"),
            ))

    def _build_manifest(self) -> None:
        spec = self.spec
        meta = spec["meta"]
        id_field = meta.get("id_field")
        key_template = meta.get("key_template") or ""
        found_a = {}
        found_b = {}
        if spec["a"]["kind"] == "dir":
            found_a = _walk(spec["a"]["root"], spec["include"], spec["exclude"],
                            spec["recursive"], list(spec["exclude"]))
        if spec["b"]["kind"] == "dir":
            found_b = _walk(spec["b"]["root"], spec["include"], spec["exclude"],
                            spec["recursive"], list(spec["exclude"]))

        seen: set[str] = set()
        for record in self._meta_records:
            raw_id = record.get(id_field) if id_field else None
            if raw_id in (None, ""):
                continue
            item_id = str(raw_id)
            if item_id in seen:
                continue
            seen.add(item_id)
            try:
                filename = key_template.format(**{
                    k: v for k, v in record.items() if isinstance(v, (str, int, float))
                }) if key_template else ""
            except (KeyError, IndexError, ValueError):
                filename = ""
            a_hit = found_a.get(filename) if filename else None
            b_hit = found_b.get(filename) if filename else None
            self.items.append(self._make_item(
                item_id=item_id,
                name=self._name_from_meta(record) or filename or item_id,
                group=(b_hit or a_hit or {}).get("group", ""),
                a_path=(a_hit or {}).get("path"),
                b_path=(b_hit or {}).get("path"),
                extra_meta=self._meta_for(item_id, filename or item_id),
            ))
        if not self.items:
            raise MaterialError("按清单配对没有生成任何素材，请检查 key_template / id_field 配置")

    def _build_json_key(self) -> None:
        spec = self.spec
        data_a = _load_json(spec["a"]["path"]) if spec["a"]["kind"] == "json" else None
        data_b = _load_json(spec["b"]["path"]) if spec["b"]["kind"] == "json" else None
        if data_a is not None and not isinstance(data_a, dict):
            raise MaterialError(f"源A 需要是「键 -> 记录」的 JSON 对象：{spec['a']['path']}")
        if data_b is not None and not isinstance(data_b, dict):
            raise MaterialError(f"源B 需要是「键 -> 记录」的 JSON 对象：{spec['b']['path']}")

        keys_a = list(data_a) if data_a else []
        keys_b = list(data_b) if data_b else []
        if spec["items_from"] == "a":
            keys = keys_a
        elif spec["items_from"] == "b":
            keys = keys_b
        else:
            keys = sorted(set(keys_a) | set(keys_b))

        pattern = re.compile(spec["id_regex"]) if spec.get("id_regex") else None
        used: dict[str, int] = {}
        for key in keys:
            item_id = key
            if pattern:
                match = pattern.search(key)
                if match:
                    groups = match.groupdict()
                    if spec.get("id_template") and groups:
                        item_id = spec["id_template"].format(**groups)
                    elif groups:
                        item_id = "-".join(str(v) for v in groups.values())
            item_id = self._unique_id(item_id, key, used)

            text_a = self._text_of(spec["a"], data_a.get(key) if data_a else None)
            text_b = self._text_of(spec["b"], data_b.get(key) if data_b else None)
            record_meta = self._meta_for(key, Path(key).name)
            group = ""
            if spec.get("group_field"):
                group = record_meta.get(spec["group_field"], "")
            self.items.append(self._make_item(
                item_id=item_id, name=item_id, group=group,
                text_a=text_a, text_b=text_b,
                extra_meta=record_meta, raw_key=key,
            ))

    def _unique_id(self, item_id: str, key: str, used: dict[str, int]) -> str:
        if item_id not in used:
            used[item_id] = 0
            return item_id
        used[item_id] += 1
        self.warnings.append(f"素材 id 冲突：{item_id}（原始键 {key[:60]}…），已自动加后缀")
        return f"{item_id}#{used[item_id] + 1}"

    def _text_of(self, source: dict, record) -> str:
        if record is None:
            return ""
        if source["kind"] == "text_file":
            return Path(source["path"]).read_text(encoding="utf-8", errors="replace")
        fields = source.get("fields") or []
        if not fields:
            fields = [k for k, v in record.items() if isinstance(v, str)] if isinstance(record, dict) else []
        if not fields and not isinstance(record, dict):
            return str(record)
        return "\n".join(filter(None, (_extract_text(record, f) for f in fields)))

    # -- 收尾 --------------------------------------------------------------
    def _make_item(self, *, item_id, name, group, a_path=None, b_path=None,
                   text_a=None, text_b=None, extra_meta=None, raw_key=None) -> dict:
        item = {
            "id": item_id,
            "key": urlsafe_key(item_id),
            "name": name,
            "group": group,
            "meta": extra_meta or {},
        }
        if self.type == "image":
            item["a"] = {"exists": bool(a_path), "path": a_path or ""}
            item["b"] = {"exists": bool(b_path), "path": b_path or ""}
            if not a_path:
                item["a"]["missing_reason"] = "源A 无同名文件"
            if not b_path:
                item["b"]["missing_reason"] = "源B 无同名文件"
        else:
            # 文本素材：json 来源直接带正文；文件来源先记路径，取详情时再读，避免上千个文件全塞进内存
            for side, text, path in (("a", text_a, a_path), ("b", text_b, b_path)):
                if text is not None and text != "":
                    item[side] = {"exists": True, "text": text}
                elif path:
                    item[side] = {"exists": True, "text": "", "path": path, "lazy": True}
                else:
                    item[side] = {"exists": False, "text": "",
                                  "missing_reason": f"源{side.upper()} 无对应文本"}
        if raw_key:
            item["raw_key"] = raw_key
        if not item["meta"] and self.spec.get("meta"):
            item["meta"] = self._meta_for(raw_key or item_id, name)
        return item

    def _finalize(self) -> None:
        self.items.sort(key=_sort_key)
        for item in self.items:
            item["v"] = item_version(item, self.type)
            self._by_key[item["key"]] = item
            self._by_id[item["id"]] = item

        group_counts: dict[str, int] = {}
        facets: dict[str, dict[str, int]] = {}
        for item in self.items:
            if item["group"]:
                group_counts[item["group"]] = group_counts.get(item["group"], 0) + 1
            for field, value in (item.get("meta") or {}).items():
                if not value or len(str(value)) > 40:
                    continue
                facets.setdefault(field, {})
                facets[field][str(value)] = facets[field].get(str(value), 0) + 1

        # 只有分面数量够少才做成筛选下拉框；卡号/标题这类高基数项交给搜索框
        allowed = (self.spec.get("meta") or {}).get("facets")
        if group_counts:
            self.facets["分组"] = dict(sorted(group_counts.items(), key=lambda kv: -kv[1]))
        for field, values in facets.items():
            if allowed is not None and field not in allowed:
                continue
            if len(values) < 2 or len(values) > _MAX_FACET_VALUES:
                continue
            self.facets[field] = dict(sorted(values.items(), key=lambda kv: (-kv[1], kv[0])))

        missing_a = sum(1 for i in self.items if not i["a"]["exists"])
        missing_b = sum(1 for i in self.items if not i["b"]["exists"])
        if missing_a:
            self.warnings.append(f"{missing_a} 张素材缺源A")
        if missing_b:
            self.warnings.append(f"{missing_b} 张素材缺源B")

    # ---------------------------------------------------------------- 查询
    def by_key(self, key: str) -> dict | None:
        return self._by_key.get(key)

    def by_id(self, item_id: str) -> dict | None:
        return self._by_id.get(item_id)

    def refresh_versions(self) -> int:
        """重新算一遍每个素材的源文件指纹，返回有变化的条数。

        索引是启动时建的，指纹只在那时算一次；但素材随时可能被重新渲染。
        每次打开/刷新素材集时重算一次，就能做到「重渲完只要刷新浏览器」——
        过期提示会自己冒出来，图片 URL 的版本号也变了，浏览器必须重新拉图，
        不用重启工具、也不用 Ctrl+Shift+R。
        """
        if self.type != "image":
            # 文本素材：源是几个 JSON/文本文件，先看它们动没动，动了就整个重建
            stamps = file_stamps(self._text_source_files())
            if stamps == self._source_stamps:
                return 0
            self._source_stamps = stamps
            before = len(self.items)
            self._build()          # 里面已经做过 _finalize，不要再调一次
            if self.error:
                return 0
            return max(before, len(self.items))

        roots = []
        for side in ("b", "a"):
            info = self.spec.get(side) or {}
            if info.get("kind") == "dir" and info.get("root"):
                roots.append(info["root"])
        stamps = scan_stamps(roots)
        changed = 0
        for item in self.items:
            new_version = ""
            for side in ("b", "a"):
                path = (item.get(side) or {}).get("path")
                if path:
                    new_version = stamps.get(_norm(path), "")
                    if new_version:
                        break
            if new_version and new_version != item.get("v"):
                item["v"] = new_version
                changed += 1
        return changed

    def _text_source_files(self) -> list[str]:
        paths = []
        for side in ("a", "b"):
            info = self.spec.get(side) or {}
            if info.get("kind") in ("json", "text_file") and info.get("path"):
                paths.append(info["path"])
        return paths

    def light_items(self) -> list[dict]:
        """给前端的精简清单（不带文本正文，避免上千条时响应过大）。"""
        out = []
        for item in self.items:
            entry = {
                "key": item["key"],
                "id": item["id"],
                "name": item["name"],
                "group": item["group"],
                "meta": item["meta"],
                "hasA": item["a"]["exists"],
                "hasB": item["b"]["exists"],
                "v": item.get("v", ""),
            }
            if self.type == "text":
                # 文本素材集在缩略图墙里用一段摘录代替缩略图
                snippet = (item["b"].get("text") or item["a"].get("text") or "").replace("\n", " ")
                entry["preview"] = snippet[:120]
            out.append(entry)
        return out

    def public_spec(self) -> dict:
        return {
            "id": self.id,
            "name": self.name,
            "type": self.type,
            "pair": self.spec["pair"],
            "sourceA": self.spec["a"]["label"] or "源A",
            "sourceB": self.spec["b"]["label"] or "源B",
            "note": self.spec.get("note", ""),
            "count": len(self.items),
            "facets": self.facets,
            "metaFields": [f for f in (self.spec.get("meta") or {}).get("fields", [])],
            "warnings": self.warnings,
            "error": self.error,
        }


def _sort_key(item: dict):
    """按「卡号/数字 id」自然排序，纯文本 id 排后面。"""
    raw = str(item["id"])
    parts = re.split(r"(\d+)", raw)
    sortable = tuple((1, int(p)) if p.isdigit() else (0, p) for p in parts if p != "")
    return (sortable, raw)


class SetRegistry:
    """按需构建并缓存各素材集索引。"""

    def __init__(self, cfg: dict):
        self.cfg = cfg
        self._sets: dict[str, MaterialSet] = {}

    def get(self, set_id: str) -> MaterialSet:
        material = self._sets.get(set_id)
        if material is not None:
            return material
        spec = next((s for s in self.cfg["sets"] if s["id"] == set_id), None)
        if spec is None:
            raise MaterialError(f"没有这个素材集：{set_id}")
        material = MaterialSet(spec)
        self._sets[set_id] = material
        return material

    def reload(self, set_id: str | None = None) -> None:
        if set_id is None:
            self._sets.clear()
        else:
            self._sets.pop(set_id, None)
