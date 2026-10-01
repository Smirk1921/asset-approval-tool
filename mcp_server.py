#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""图包审批工具 · MCP 服务（stdio）。

把审批工具的能力暴露成 MCP 工具，任何支持 MCP 的 AI agent 都能直接调用，
不用去拼 HTTP 请求。用标准库实现，不依赖 mcp SDK。

在支持 MCP 的客户端里这样配（Claude Desktop / Cursor / ZCode 等）：

    {
      "mcpServers": {
        "asset-approval": {
          "command": "python",
          "args": ["D:\\\\path\\\\to\\\\图包审批工具\\\\mcp_server.py"]
        }
      }
    }

工具一览（`python mcp_server.py --list-tools` 也能看）：

    approval_status            各素材集的进度总览
    list_items                 按状态/标签/关键词列出素材
    get_item                   单条素材详情（图片路径 或 文本正文）
    get_item_images            直接返回图片内容，视觉模型可以"看"图（可同时给原图和成品）
    review_item                给一条素材写审批结果（状态/标签/备注/圈选）
    batch_review               批量改状态，支持 dry_run 预演
    export_results             导出 JSON / CSV / 圈注明细
    clear_stale_results        清掉「源文件已变」的过期结果
    start_new_round            归档本轮、开始新一轮
    list_previous_round_rejects 上一轮被打回的素材

传输层与 agent.py 一致：服务在跑就走 HTTP，没跑就直接读写文件。
"""
from __future__ import annotations

import argparse
import base64
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from core.agent_api import AgentError, make_backend  # noqa: E402
from core.thumbs import thumbnail  # noqa: E402

DEFAULT_CONFIG = str(Path(__file__).resolve().parent / "config.json")
PROTOCOL_VERSION = "2024-11-05"
SERVER_INFO = {"name": "asset-approval-tool", "version": "1.0.0"}

_SET_DESC = "素材集 id。不确定有哪些，先调 approval_status。"

TOOLS = [
    {
        "name": "approval_status",
        "description": "总览所有素材集的审批进度：总数、已审/通过/打回/存疑、源文件已变（结果过期）的条数、当前轮次。开工前先调它。",
        "inputSchema": {"type": "object", "properties": {}, "additionalProperties": False},
    },
    {
        "name": "list_items",
        "description": "列出素材（默认全部，可用参数筛）。返回每条的状态、标签、备注、圈选数、是否过期，以及是否有原图/成品。用来找出「还没审的」「打回的」等待处理素材。",
        "inputSchema": {
            "type": "object",
            "properties": {
                "set": {"type": "string", "description": _SET_DESC},
                "status": {"type": "string",
                           "description": "按状态筛：unreviewed / pass / reject / flag / all（也认 未审/通过/打回/存疑）"},
                "tag": {"type": "string", "description": "按问题标签筛，例如 文字溢出"},
                "search": {"type": "string", "description": "按 id / 名称 / 卡号等元数据模糊搜"},
                "group": {"type": "string", "description": "按分组筛（图片按成品子目录，文本按分类字段）"},
                "stale_only": {"type": "boolean", "description": "只看源文件已变、结果可能过期的"},
                "no_tags": {"type": "boolean", "description": "只看没打任何标签的"},
                "annotated": {"type": "boolean", "description": "只看圈过图的"},
                "noted": {"type": "boolean", "description": "只看写过备注的"},
                "limit": {"type": "integer", "description": "最多返回多少条，默认 50"},
                "offset": {"type": "integer", "description": "跳过前多少条，用于翻页"},
            },
            "required": ["set"],
            "additionalProperties": False,
        },
    },
    {
        "name": "get_item",
        "description": "单条素材的完整信息：图片素材给原图/成品的文件路径；文本素材直接给原文与译文正文。附带已有审批结果与圈选明细。",
        "inputSchema": {
            "type": "object",
            "properties": {
                "set": {"type": "string", "description": _SET_DESC},
                "id": {"type": "string", "description": "素材 id，例如 102_front.png 或 card01-槽0-0"},
            },
            "required": ["set", "id"],
            "additionalProperties": False,
        },
    },
    {
        "name": "get_item_images",
        "description": "把素材图片直接返回给你看（视觉模型可用）。side 默认 both，会一次给出英文原图和中文成品，便于对照找问题；返回的图是压缩过的预览，原图路径见 get_item。",
        "inputSchema": {
            "type": "object",
            "properties": {
                "set": {"type": "string", "description": _SET_DESC},
                "id": {"type": "string", "description": "素材 id"},
                "side": {"type": "string", "enum": ["a", "b", "both"],
                         "description": "a=源A原图，b=源B成品，both=两张都给（默认）"},
                "max_size": {"type": "integer", "description": "长边像素上限，默认 900，越大越清晰也越费 token"},
            },
            "required": ["set", "id"],
            "additionalProperties": False,
        },
    },
    {
        "name": "review_item",
        "description": "给一条素材写审批结果。status: pass=通过 / reject=打回 / flag=存疑 / 空字符串=清除标记。只传你要改的字段。坐标是相对图片的 0~1 比例（左上角为原点）。",
        "inputSchema": {
            "type": "object",
            "properties": {
                "set": {"type": "string", "description": _SET_DESC},
                "id": {"type": "string", "description": "素材 id"},
                "status": {"type": "string", "description": "pass / reject / flag / '' （也认 通过/打回/存疑/未审）"},
                "tags": {"type": "array", "items": {"type": "string"},
                         "description": "问题标签，例如 [\"文字溢出\",\"术语错\"]"},
                "note": {"type": "string", "description": "备注说明"},
                "annotations": {
                    "type": "array",
                    "description": "圈出的问题区域，每处一条",
                    "items": {
                        "type": "object",
                        "properties": {
                            "x": {"type": "number", "description": "左边界，0~1"},
                            "y": {"type": "number", "description": "上边界，0~1"},
                            "w": {"type": "number", "description": "宽，0~1"},
                            "h": {"type": "number", "description": "高，0~1"},
                            "side": {"type": "string", "enum": ["a", "b"],
                                     "description": "圈在原图(a)还是成品(b)上，默认 b"},
                            "note": {"type": "string", "description": "这一处的问题说明"},
                        },
                        "required": ["x", "y", "w", "h"],
                        "additionalProperties": False,
                    },
                },
            },
            "required": ["set", "id"],
            "additionalProperties": False,
        },
    },
    {
        "name": "batch_review",
        "description": "批量改状态。必须给明确范围：ids，或至少一个 filter_* 条件（想作用于全部要显式传 filter_status=\"all\"）。建议先 dry_run=true 看会命中哪些再执行。",
        "inputSchema": {
            "type": "object",
            "properties": {
                "set": {"type": "string", "description": _SET_DESC},
                "status": {"type": "string", "description": "要改成什么状态：pass / reject / flag / ''"},
                "tags": {"type": "array", "items": {"type": "string"}, "description": "顺带贴上的标签"},
                "ids": {"type": "array", "items": {"type": "string"}, "description": "指定素材 id 列表"},
                "filter_status": {"type": "string",
                                  "description": "范围：按状态筛（unreviewed/pass/reject/flag/all）"},
                "filter_tag": {"type": "string", "description": "范围：按标签筛"},
                "search": {"type": "string", "description": "范围：关键词"},
                "group": {"type": "string", "description": "范围：分组"},
                "stale_only": {"type": "boolean", "description": "范围：源文件已变的"},
                "no_tags": {"type": "boolean", "description": "范围：无标签的"},
                "annotated": {"type": "boolean", "description": "范围：有圈选的"},
                "noted": {"type": "boolean", "description": "范围：有备注的"},
                "dry_run": {"type": "boolean", "description": "true 时只返回会命中哪些，不改动"},
            },
            "required": ["set", "status"],
            "additionalProperties": False,
        },
    },
    {
        "name": "export_results",
        "description": "导出审批结果。format: json=完整结构 / csv=主表 / annotations=圈注明细（一处圈选一行，含百分比与像素坐标）。only: all / reviewed / problem（打回或存疑，或圈过图、挂过标签）。",
        "inputSchema": {
            "type": "object",
            "properties": {
                "set": {"type": "string", "description": _SET_DESC},
                "format": {"type": "string", "enum": ["json", "csv", "annotations"], "description": "默认 json"},
                "only": {"type": "string", "enum": ["all", "reviewed", "problem"], "description": "默认 all"},
                "out": {"type": "string", "description": "写到这个文件路径；不传则直接把内容返回"},
            },
            "required": ["set"],
            "additionalProperties": False,
        },
    },
    {
        "name": "clear_stale_results",
        "description": "把「源文件在我审批之后又被改过」的结果清掉，让它们回到未审（原记录仍留在 history 流水里）。建议先 dry_run=true。",
        "inputSchema": {
            "type": "object",
            "properties": {
                "set": {"type": "string", "description": _SET_DESC},
                "ids": {"type": "array", "items": {"type": "string"}, "description": "只清这些 id；不传则清全部过期的"},
                "dry_run": {"type": "boolean", "description": "true 时只列出会被清的"},
            },
            "required": ["set"],
            "additionalProperties": False,
        },
    },
    {
        "name": "start_new_round",
        "description": "归档本轮结果并开始新一轮：当前结果整体存成 <素材集>.roundN.json，当前批次清空、轮次 +1，缩略图缓存清掉。归档文件不会自动删除。",
        "inputSchema": {
            "type": "object",
            "properties": {"set": {"type": "string", "description": _SET_DESC}},
            "required": ["set"],
            "additionalProperties": False,
        },
    },
    {
        "name": "list_previous_round_rejects",
        "description": "上一轮被打回的素材 id 列表，用来「优先复审上轮打回的」。",
        "inputSchema": {
            "type": "object",
            "properties": {"set": {"type": "string", "description": _SET_DESC}},
            "required": ["set"],
            "additionalProperties": False,
        },
    },
]


class Server:
    def __init__(self, backend):
        self.backend = backend

    # ---------------------------------------------------------- 工具实现
    def call(self, name: str, args: dict):
        """返回 MCP 的 content 数组。"""
        if name == "approval_status":
            return text_result(self.backend.status())

        if name == "list_items":
            result = self.backend.items(
                args["set"],
                status=args.get("status"), tag=args.get("tag"), search=args.get("search"),
                group=args.get("group"),
                stale_only=bool(args.get("stale_only")), no_tags=bool(args.get("no_tags")),
                annotated=bool(args.get("annotated")), noted=bool(args.get("noted")),
                limit=args.get("limit", 50), offset=args.get("offset", 0))
            return text_result(result)

        if name == "get_item":
            return text_result(self.backend.item(args["set"], args["id"]))

        if name == "get_item_images":
            return self._images(args)

        if name == "review_item":
            result = self.backend.review(
                args["set"], args["id"],
                status=args.get("status"), tags=args.get("tags"),
                note=args.get("note"), annotations=args.get("annotations"))
            return text_result(result)

        if name == "batch_review":
            result = self.backend.batch(
                args["set"], ids=args.get("ids"), status=args.get("status"),
                tags=args.get("tags"), dry_run=bool(args.get("dry_run")),
                filter_status=args.get("filter_status"), filter_tag=args.get("filter_tag"),
                search=args.get("search"), group=args.get("group"),
                stale_only=args.get("stale_only"), no_tags=args.get("no_tags"),
                annotated=args.get("annotated"), noted=args.get("noted"))
            return text_result(result)

        if name == "export_results":
            result = self.backend.export(args["set"], fmt=args.get("format") or "json",
                                         only=args.get("only") or "all", out=args.get("out"))
            # 导出内容可能很长，但 agent 就是要它，原样给
            return text_result(result)

        if name == "clear_stale_results":
            return text_result(self.backend.clear_stale(
                args["set"], ids=args.get("ids"), dry_run=bool(args.get("dry_run"))))

        if name == "start_new_round":
            return text_result(self.backend.new_round(args["set"]))

        if name == "list_previous_round_rejects":
            return text_result(self.backend.previous_round(args["set"]))

        raise AgentError(f"没有这个工具：{name}")

    def _images(self, args: dict):
        """把图片本身作为 image content 返回，视觉模型能直接看。"""
        set_id, item_id = args["set"], args["id"]
        side = (args.get("side") or "both").lower()
        max_size = int(args.get("max_size") or 900)
        sides = ["a", "b"] if side == "both" else [side]
        if any(s not in ("a", "b") for s in sides):
            raise AgentError("side 只能是 a / b / both")

        content = []
        skipped = []
        for one in sides:
            try:
                info = self.backend.image(set_id, item_id, side=one, max_size=max_size)
            except AgentError as exc:
                skipped.append(f"{one}: {exc}")
                continue
            path = Path(info.get("thumb_path") or info["path"])
            if not path.exists():
                skipped.append(f"{one}: 文件不存在 {path}")
                continue
            content.append({
                "type": "image",
                "data": base64.b64encode(path.read_bytes()).decode("ascii"),
                "mimeType": "image/jpeg" if path.suffix.lower() in (".jpg", ".jpeg") else "image/png",
            })
            content.append({"type": "text", "text": f"{one}（{'源A' if one == 'a' else '源B'}）原图路径：{info['path']}"})

        if not content:
            raise AgentError("没取到任何图片：" + "；".join(skipped))
        summary = {"set": set_id, "id": item_id, "sides": sides, "preview_max_size": max_size}
        if skipped:
            summary["skipped"] = skipped
        content.append({"type": "text", "text": json.dumps(summary, ensure_ascii=False)})
        return content


def text_result(payload) -> list[dict]:
    if isinstance(payload, str):
        text = payload
    else:
        text = json.dumps(payload, ensure_ascii=False, indent=2)
    return [{"type": "text", "text": text}]


# ---------------------------------------------------------------- JSON-RPC

def handle(server: Server, message: dict):
    """返回要发回去的响应；通知类消息返回 None。"""
    method = message.get("method")
    msg_id = message.get("id")

    if method == "initialize":
        params = message.get("params") or {}
        return {
            "jsonrpc": "2.0", "id": msg_id,
            "result": {
                "protocolVersion": params.get("protocolVersion") or PROTOCOL_VERSION,
                "capabilities": {"tools": {"listChanged": False}},
                "serverInfo": SERVER_INFO,
                "instructions": (
                    "素材审批工具。先用 approval_status 看有哪些素材集和进度，"
                    "用 list_items 找待审素材，用 get_item / get_item_images 看内容，"
                    "再用 review_item 写结论。看图片请用 get_item_images（会返回图片本身）。"
                ),
            },
        }

    if method in ("notifications/initialized", "notifications/cancelled"):
        return None

    if method == "ping":
        return {"jsonrpc": "2.0", "id": msg_id, "result": {}}

    if method == "tools/list":
        return {"jsonrpc": "2.0", "id": msg_id, "result": {"tools": TOOLS}}

    if method == "tools/call":
        params = message.get("params") or {}
        name = params.get("name")
        args = params.get("arguments") or {}
        try:
            content = server.call(name, args)
            return {"jsonrpc": "2.0", "id": msg_id,
                    "result": {"content": content, "isError": False}}
        except AgentError as exc:
            # 参数/业务错误回给模型看，让它自己纠正，而不是抛协议层错误
            return {"jsonrpc": "2.0", "id": msg_id,
                    "result": {"content": [{"type": "text", "text": str(exc)}], "isError": True}}
        except Exception as exc:  # noqa: BLE001
            return {"jsonrpc": "2.0", "id": msg_id,
                    "result": {"content": [{"type": "text",
                                            "text": f"内部错误：{type(exc).__name__}: {exc}"}],
                               "isError": True}}

    if msg_id is None:
        return None
    return {"jsonrpc": "2.0", "id": msg_id,
            "error": {"code": -32601, "message": f"不支持的方法：{method}"}}


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description="图包审批工具 MCP 服务（stdio）")
    parser.add_argument("--config", default=DEFAULT_CONFIG)
    parser.add_argument("--server", default=None, help="服务地址；默认按配置自动探测")
    parser.add_argument("--offline", action="store_true", help="不走 HTTP，直接读写文件")
    parser.add_argument("--list-tools", action="store_true", help="打印工具清单后退出（自检用）")
    args = parser.parse_args(argv)

    if args.list_tools:
        for tool in TOOLS:
            required = tool["inputSchema"].get("required") or []
            print(f"{tool['name']}({', '.join(required)})")
            print(f"    {tool['description'][:100]}")
        return 0

    backend = make_backend(args.config, server=args.server, offline=args.offline)
    server = Server(backend)

    for line in sys.stdin:
        line = line.strip()
        if not line:
            continue
        try:
            message = json.loads(line)
        except json.JSONDecodeError:
            continue                      # 不是 JSON 就跳过，不要因为一行坏数据退出
        response = handle(server, message)
        if response is None:
            continue
        sys.stdout.write(json.dumps(response, ensure_ascii=False) + "\n")
        sys.stdout.flush()
    return 0


if __name__ == "__main__":
    sys.exit(main())
