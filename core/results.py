# -*- coding: utf-8 -*-
"""审批结果的读写。

存储：data/results/<素材集id>.json
  {
    "set_id": "...", "set_name": "...", "updated_at": "...",
    "items": { "<素材id>": {status, tags, note, annotations, reviewed_at} }
  }

每次标记立即写盘（原子替换），另留一份 append-only 的 .history.jsonl 便于追溯。
"""
from __future__ import annotations

import csv
import io
import json
import os
import threading
from datetime import datetime
from pathlib import Path

VALID_STATUS = ("pass", "reject", "flag", "")
STATUS_CN = {"pass": "通过", "reject": "打回", "flag": "存疑", "": "未审"}

# 「没有历史可回退」的哨兵值，区别于「历史记录是未审(None)」
NO_HISTORY = object()
_MAX_UNDO = 20


def _now() -> str:
    return datetime.now().strftime("%Y-%m-%d %H:%M:%S")


def _atomic_write_json(path: Path, payload) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(f".{path.name}.{os.getpid()}.tmp")
    tmp.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    os.replace(tmp, path)


class ResultStore:
    """一个素材集的审批结果存取。"""

    def __init__(self, set_id: str, set_name: str, result_dir: str | os.PathLike):
        self.set_id = set_id
        self.set_name = set_name
        self.dir = Path(result_dir)
        self.path = self.dir / f"{set_id}.json"
        self.history_path = self.dir / f"{set_id}.history.jsonl"
        self._lock = threading.RLock()
        self._items: dict[str, dict] = {}
        self._undo: dict[str, list] = {}
        self.round = 1
        self._dirty = True
        self.load()

    # ---------------------------------------------------------------- 读
    def load(self) -> None:
        with self._lock:
            if not self.path.exists():
                self._items = {}
                return
            try:
                data = json.loads(self.path.read_text(encoding="utf-8"))
                items = data.get("items") if isinstance(data, dict) else None
                self._items = items if isinstance(items, dict) else {}
                if isinstance(data, dict):
                    try:
                        self.round = max(1, int(data.get("round") or 1))
                    except (TypeError, ValueError):
                        self.round = 1
            except (json.JSONDecodeError, OSError):
                # 结果文件损坏时不要静默清空：备份后重新开始，避免用户丢数据
                backup = self.path.with_suffix(f".bad-{int(datetime.now().timestamp())}.json")
                try:
                    self.path.replace(backup)
                except OSError:
                    pass
                self._items = {}

    def get(self, item_id: str) -> dict | None:
        with self._lock:
            record = self._items.get(item_id)
            return json.loads(json.dumps(record)) if record else None

    def all(self) -> dict[str, dict]:
        with self._lock:
            return self._items

    def stats(self, item_ids) -> dict:
        counts = {"pass": 0, "reject": 0, "flag": 0, "unreviewed": 0}
        with self._lock:
            for item_id in item_ids:
                record = self._items.get(item_id)
                status = (record or {}).get("status") or ""
                if status in ("pass", "reject", "flag"):
                    counts[status] += 1
                else:
                    counts["unreviewed"] += 1
        counts["total"] = len(item_ids)
        counts["reviewed"] = counts["pass"] + counts["reject"] + counts["flag"]
        return counts

    # ---------------------------------------------------------------- 写
    def save(self, item_id: str, *, status=None, tags=None, note=None,
             annotations=None, reviewer: str = "") -> dict:
        """写入/更新一条素材的审批结果，立即落盘。"""
        with self._lock:
            before = json.loads(json.dumps(self._items.get(item_id) or {})) or None
            record = self._items.setdefault(item_id, {})
            if status is not None:
                status = status if status in VALID_STATUS else ""
                record["status"] = status
            if tags is not None:
                record["tags"] = [str(t) for t in tags if str(t).strip()]
            if note is not None:
                record["note"] = str(note)
            if annotations is not None:
                record["annotations"] = [_clean_annotation(a) for a in annotations]
            if reviewer:
                record["reviewer"] = reviewer

            record["reviewed_at"] = _now()
            record["round"] = self.round
            # 清空标记（回到未审）时删掉冗余字段，结果文件保持干净
            if not record.get("status") and not record.get("tags") and not record.get("note") \
                    and not record.get("annotations"):
                self._items.pop(item_id, None)
                snapshot = None
            else:
                record.setdefault("tags", [])
                record.setdefault("note", "")
                record.setdefault("annotations", [])
                snapshot = json.loads(json.dumps(record))

            self._flush()
            self._append_history(item_id, snapshot)
            if before != snapshot:
                self._push_undo(item_id, before)
            return snapshot or {}

    def save_many(self, item_ids, *, status=None, tags=None) -> int:
        """批量标记（只改状态/标签；status=="" 时视为「还原未审」，连备注和圈选一起清）。"""
        with self._lock:
            changed = 0
            now = _now()
            touched: list[str] = []
            for item_id in item_ids:
                before = json.loads(json.dumps(self._items.get(item_id) or {})) or None
                record = self._items.setdefault(item_id, {})
                if status is not None:
                    record["status"] = status if status in VALID_STATUS else ""
                if tags is not None:
                    existing = [t for t in record.get("tags", []) if t not in tags]
                    record["tags"] = existing + [str(t) for t in tags if str(t).strip()]
                # 批量「清除审批」（status==""）的语义是还原到未审，
                # 备注和圈选也一起清，否则前端再进来会看到旧框"复活"
                if status == "":
                    record["tags"] = []
                    record["note"] = ""
                    record["annotations"] = []
                record["reviewed_at"] = now
                record["round"] = self.round
                record.setdefault("tags", [])
                record.setdefault("note", "")
                record.setdefault("annotations", [])
                if before != record:
                    self._push_undo(item_id, before)
                    touched.append(item_id)
                changed += 1
            self._flush()
            try:
                with self.history_path.open("a", encoding="utf-8") as handle:
                    for item_id in item_ids:
                        handle.write(json.dumps(
                            {"at": now, "item": item_id, "batch": True,
                             "status": status, "tags": tags or []},
                            ensure_ascii=False) + "\n")
            except OSError:
                pass
            return changed

    # ---------------------------------------------------------------- 撤销
    def save_batch(self, entries) -> int:
        """一次写多条，只落盘一次。

        entries: [{id, status?, tags?, note?, annotations?, src?}]
        比起逐条 save，省掉 N-1 次整份写盘；中途也不会出现"写了一半"的状态。
        """
        with self._lock:
            now = _now()
            count = 0
            for entry in entries:
                item_id = str(entry.get("id") or "")
                if not item_id:
                    continue
                before = json.loads(json.dumps(self._items.get(item_id) or {})) or None
                record = self._items.setdefault(item_id, {})
                if entry.get("status") is not None:
                    record["status"] = entry["status"] if entry["status"] in VALID_STATUS else ""
                if entry.get("tags") is not None:
                    record["tags"] = [str(t) for t in entry["tags"] if str(t).strip()]
                if entry.get("note") is not None:
                    record["note"] = str(entry["note"])
                if entry.get("annotations") is not None:
                    record["annotations"] = [_clean_annotation(a) for a in entry["annotations"]]
                if entry.get("src"):
                    record["src"] = entry["src"]
                record["reviewed_at"] = now
                record["round"] = self.round

                if not record.get("status") and not record.get("tags") \
                        and not record.get("note") and not record.get("annotations"):
                    self._items.pop(item_id, None)
                    snapshot = None
                else:
                    record.setdefault("tags", [])
                    record.setdefault("note", "")
                    record.setdefault("annotations", [])
                    snapshot = json.loads(json.dumps(record))
                if before != snapshot:
                    self._push_undo(item_id, before)
                count += 1
            self._flush()
            self._append_history_bulk([(str(e.get("id") or ""), self._items.get(str(e.get("id") or "")))
                                       for e in entries if e.get("id")])
            return count

    def _append_history_bulk(self, pairs) -> None:
        try:
            with self.history_path.open("a", encoding="utf-8") as handle:
                for item_id, snapshot in pairs:
                    handle.write(json.dumps({"at": _now(), "item": item_id, "record": snapshot},
                                            ensure_ascii=False) + "\n")
        except OSError:
            pass

    def _push_undo(self, item_id: str, snapshot: dict | None) -> None:
        stack = self._undo.setdefault(item_id, [])
        stack.append(snapshot)
        if len(stack) > _MAX_UNDO:
            del stack[0]

    def previous(self, item_id: str):
        """返回上一次的状态快照；没有历史时返回 NO_HISTORY。"""
        with self._lock:
            stack = self._undo.get(item_id)
            if not stack:
                return NO_HISTORY
            return stack.pop()

    def restore(self, item_id: str, snapshot: dict | None) -> dict:
        """回退到快照（None 表示回到未审）。"""
        with self._lock:
            if snapshot:
                self._items[item_id] = json.loads(json.dumps(snapshot))
            else:
                self._items.pop(item_id, None)
            self._flush()
            self._append_history(item_id, snapshot)
            return snapshot or {}

    def _flush(self) -> None:
        payload = {
            "set_id": self.set_id,
            "set_name": self.set_name,
            "round": self.round,
            "updated_at": _now(),
            "items": self._items,
        }
        _atomic_write_json(self.path, payload)

    # ---------------------------------------------------------------- 源文件指纹 / 过期结果
    def stamp_sources(self, versions: dict[str, str]) -> None:
        """把素材当前的源文件指纹写进结果，用于日后判断结果是否过期。"""
        with self._lock:
            changed = False
            for item_id, version in versions.items():
                record = self._items.get(item_id)
                if record is not None and version and record.get("src") != version:
                    record["src"] = version
                    changed = True
            if changed:
                self._flush()

    def stale_ids(self, versions: dict[str, str]) -> list[str]:
        """返回「审批之后源文件又被改过」的素材 id。

        只有在结果里记过指纹（src）、且和当前指纹不一致时才算过期；
        老版本结果没有 src 字段，一律当作未知、不误报。
        """
        out = []
        with self._lock:
            for item_id, version in versions.items():
                record = self._items.get(item_id)
                if not record:
                    continue
                recorded = record.get("src")
                if recorded and version and recorded != version:
                    out.append(item_id)
        return out

    def clear_ids(self, item_ids) -> int:
        """删除指定素材的审批结果（保留 history 流水），用于清理过期结果。"""
        with self._lock:
            removed = 0
            for item_id in item_ids:
                if self._items.pop(item_id, None) is not None:
                    removed += 1
                    self._append_history(item_id, None)
            if removed:
                self._flush()
            return removed

    # ---------------------------------------------------------------- 分轮次
    def archive_path(self, round_no: int | None = None) -> Path:
        return self.dir / f"{self.set_id}.round{round_no or self.round}.json"

    def start_new_round(self) -> dict:
        """把当前结果归档成「第 N 轮」，清空当前结果，轮次 +1。"""
        with self._lock:
            archived_json = None
            archived_history = None
            if self._items or self.path.exists():
                archived_json = self.archive_path()
                if self.path.exists():
                    self.path.replace(archived_json)
                if self.history_path.exists():
                    archived_history = self.history_path.with_name(
                        f"{self.set_id}.round{self.round}.history.jsonl")
                    self.history_path.replace(archived_history)
            previous_round = self.round
            self.round = previous_round + 1
            self._items = {}
            self._undo = {}
            self._flush()
            return {
                "previousRound": previous_round,
                "round": self.round,
                "archivedJson": str(archived_json) if archived_json else "",
                "archivedHistory": str(archived_history) if archived_history else "",
            }

    def previous_round_records(self, round_no: int | None = None) -> dict:
        """读归档的上一轮结果，返回 {item_id: record}。"""
        target = round_no if round_no is not None else max(1, self.round - 1)
        path = self.archive_path(target)
        if not path.exists():
            return {}
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError):
            return {}
        items = data.get("items") if isinstance(data, dict) else None
        return items if isinstance(items, dict) else {}

    def _append_history(self, item_id: str, snapshot: dict | None) -> None:
        try:
            with self.history_path.open("a", encoding="utf-8") as handle:
                handle.write(json.dumps(
                    {"at": _now(), "item": item_id, "record": snapshot},
                    ensure_ascii=False) + "\n")
        except OSError:
            pass

    # ---------------------------------------------------------------- 导出
    def export_json(self, rows) -> str:
        payload = {
            "set_id": self.set_id,
            "set_name": self.set_name,
            "exported_at": _now(),
            "stats": self.stats([r["id"] for r in rows]),
            "count": len(rows),
            "items": rows,
        }
        return json.dumps(payload, ensure_ascii=False, indent=2)

    def export_csv(self, rows) -> str:
        buf = io.StringIO()
        writer = csv.writer(buf)
        writer.writerow([
            "素材id", "名称", "分组", "状态", "问题标签", "备注",
            "圈选数量", "元数据", "审批时间", "轮次",
        ])
        for row in rows:
            record = row.get("record") or {}
            anns = record.get("annotations") or []
            meta_text = "; ".join(f"{k}={v}" for k, v in (row.get("meta") or {}).items() if v not in ("", None))
            writer.writerow([
                row["id"], row.get("name", ""), row.get("group", ""),
                STATUS_CN.get(record.get("status") or "", "未审"),
                "; ".join(record.get("tags") or []),
                record.get("note") or "",
                len(anns), meta_text,
                record.get("reviewed_at") or "",
                record.get("round") or "",
            ])
        # 带 BOM，Windows Excel 直接双击打开不乱码
        return "\ufeff" + buf.getvalue()

    def export_annotations_csv(self, rows) -> str:
        """圈注明细单独出一张表：一处圈选一行，坐标带百分比和像素两套。

        像素列需要知道图片尺寸；拿不到尺寸（图缺失）时留空。
        """
        buf = io.StringIO()
        writer = csv.writer(buf)
        writer.writerow([
            "素材id", "名称", "分组", "圈选序号", "画在哪侧", "说明",
            "左侧占比", "上边占比", "宽占比", "高占比",
            "像素x", "像素y", "像素宽", "像素高", "源文件", "轮次",
        ])
        for row in rows:
            record = row.get("record") or {}
            anns = record.get("annotations") or []
            sizes = row.get("sizes") or {}
            for index, ann in enumerate(anns):
                side = ann.get("side", "b")
                size = sizes.get(side) or None
                px = [""] * 4
                if size and size.get("w") and size.get("h"):
                    px = [
                        round(ann.get("x", 0) * size["w"]),
                        round(ann.get("y", 0) * size["h"]),
                        round(ann.get("w", 0) * size["w"]),
                        round(ann.get("h", 0) * size["h"]),
                    ]
                writer.writerow([
                    row["id"], row.get("name", ""), row.get("group", ""),
                    index + 1,
                    "源A" if side == "a" else "源B",
                    ann.get("note") or "",
                    f"{ann.get('x', 0) * 100:.2f}%", f"{ann.get('y', 0) * 100:.2f}%",
                    f"{ann.get('w', 0) * 100:.2f}%", f"{ann.get('h', 0) * 100:.2f}%",
                    *px,
                    (row.get("sourceA" if side == "a" else "sourceB") or ""),
                    record.get("round") or "",
                ])
        return "\ufeff" + buf.getvalue()


def _clean_annotation(raw) -> dict:
    if not isinstance(raw, dict):
        return {}
    def num(key, default=0.0):
        try:
            return round(float(raw.get(key, default)), 6)
        except (TypeError, ValueError):
            return default
    side = str(raw.get("side") or "b")
    return {
        "side": side if side in ("a", "b") else "b",
        "x": num("x"), "y": num("y"), "w": num("w"), "h": num("h"),
        "note": str(raw.get("note") or ""),
        "tag": str(raw.get("tag") or ""),
    }
