#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""图包审批工具 · 命令行接口（给 AI agent 和脚本用）。

设计约定，方便程序消费：
  - 每次调用**只往 stdout 打印一个 JSON 对象**，成功失败都是；失败时退出码非 0。
  - 参数既能用英文枚举（pass/reject/flag/unreviewed），也能用中文（通过/打回/存疑/未审）。
  - 服务在跑就走 HTTP（和网页版共用同一份状态），没跑就直接读写文件。

常用例子：
    python agent.py status
    python agent.py items --set demo_cards --status unreviewed --limit 5
    python agent.py item --set demo_cards --id 102_front.png
    python agent.py review --set demo_cards --id 102_front.png --status 打回 \\
        --tags 文字溢出 --note "正文跑出框外" --annotate "0.08,0.33,0.84,0.42,b,这里溢出"
    python agent.py batch --set demo_cards --filter-status unreviewed --status pass --dry-run
    python agent.py export --set demo_cards --format annotations --only problem --out out.csv

完整说明见 docs/agent-接口.md
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from core.agent_api import AgentError, make_backend  # noqa: E402

DEFAULT_CONFIG = str(Path(__file__).resolve().parent / "config.json")


def parse_annotate(raw: str) -> dict:
    """把 "x,y,w,h[,side][,说明]" 解析成圈选对象。说明放最后，所以里面可以有逗号。"""
    parts = raw.split(",", 5)
    if len(parts) < 4:
        raise AgentError(f"--annotate 至少要有 x,y,w,h 四个数：{raw!r}")
    try:
        x, y, w, h = (float(p) for p in parts[:4])
    except ValueError as exc:
        raise AgentError(f"--annotate 的 x,y,w,h 必须是数字：{raw!r}") from exc
    side = "b"
    note = ""
    if len(parts) >= 5 and parts[4].strip():
        side = parts[4].strip().lower()
    if len(parts) >= 6:
        note = parts[5]
    return {"x": x, "y": y, "w": w, "h": h, "side": side, "note": note}


def split_list(raw: str | None) -> list[str] | None:
    if not raw:
        return None
    return [part.strip() for part in raw.replace("，", ",").split(",") if part.strip()]


def build_parser() -> argparse.ArgumentParser:
    # 全局选项做成「父解析器」，子命令前后都能写：
    #   python agent.py --pretty items --set x    和    python agent.py items --set x --pretty
    # 两种都成立。用 SUPPRESS 当默认值，避免子解析器把父级已经解析到的值覆盖成空。
    common = argparse.ArgumentParser(add_help=False)
    common.add_argument("--config", default=argparse.SUPPRESS, help="配置文件路径")
    common.add_argument("--server", default=argparse.SUPPRESS,
                        help="服务地址；默认按配置里的 host/port 自动探测。填 auto 等价于默认")
    common.add_argument("--offline", action="store_true", default=argparse.SUPPRESS,
                        help="不走 HTTP，直接读写结果文件（服务没在跑时用）")
    common.add_argument("--pretty", action="store_true", default=argparse.SUPPRESS,
                        help="JSON 缩进输出，方便人看")

    parser = argparse.ArgumentParser(
        prog="agent.py",
        description="图包审批工具的命令行接口（每次输出一个 JSON）",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=__doc__.split("常用例子：")[-1],
        parents=[common],
    )
    parser.set_defaults(config=DEFAULT_CONFIG)

    sub = parser.add_subparsers(dest="command", required=True)

    sub.add_parser("status", help="总览：各素材集的条数与审批进度", parents=[common])

    def add_filter_args(p):
        p.add_argument("--status", help="按状态筛选：unreviewed/pass/reject/flag（也认中文）")
        p.add_argument("--tag", help="按问题标签筛选")
        p.add_argument("--search", help="按 id/名称/元数据模糊搜索")
        p.add_argument("--group", help="按分组筛选")
        p.add_argument("--stale", action="store_true", help="只看源文件已变（结果过期）的")
        p.add_argument("--no-tags", dest="no_tags", action="store_true", help="只看没打任何标签的")
        p.add_argument("--annotated", action="store_true", help="只看有圈选的")
        p.add_argument("--noted", action="store_true", help="只看有备注的")
        p.add_argument("--limit", type=int, help="最多返回多少条")
        p.add_argument("--offset", type=int, default=0, help="跳过前多少条")

    p_items = sub.add_parser("items", help="列出素材（含状态、标签、是否过期）", parents=[common])
    p_items.add_argument("--set", required=True, dest="set_id", help="素材集 id")
    add_filter_args(p_items)

    p_item = sub.add_parser("item", help="看单条素材详情（含图片路径或文本正文）", parents=[common])
    p_item.add_argument("--set", required=True, dest="set_id")
    p_item.add_argument("--id", required=True, dest="item_id")

    p_review = sub.add_parser("review", help="给单条素材写审批结果", parents=[common])
    p_review.add_argument("--set", required=True, dest="set_id")
    p_review.add_argument("--id", required=True, dest="item_id")
    p_review.add_argument("--status", help="pass/reject/flag/''（也认 通过/打回/存疑/未审）")
    p_review.add_argument("--tags", help="问题标签，逗号分隔")
    p_review.add_argument("--note", help="备注")
    p_review.add_argument("--annotations", help="圈选数组的 JSON 字符串")
    p_review.add_argument("--annotate", action="append", default=[], metavar="x,y,w,h[,side][,说明]",
                          help="圈一处问题；可重复。坐标是 0~1 的比例")

    p_batch = sub.add_parser("batch", help="批量改状态（必须指定范围）", parents=[common])
    p_batch.add_argument("--set", required=True, dest="set_id")
    p_batch.add_argument("--ids", help="指定素材 id，逗号分隔")
    p_batch.add_argument("--filter-status", dest="filter_status",
                         help="选择范围：按状态筛（unreviewed/pass/reject/flag/all）")
    p_batch.add_argument("--filter-tag", dest="filter_tag", help="选择范围：按标签筛")
    p_batch.add_argument("--search", help="选择范围：关键词")
    p_batch.add_argument("--group", help="选择范围：分组")
    p_batch.add_argument("--stale", action="store_true", help="选择范围：源文件已变的")
    p_batch.add_argument("--no-tags", dest="no_tags", action="store_true", help="选择范围：无标签的")
    p_batch.add_argument("--annotated", action="store_true", help="选择范围：有圈选的")
    p_batch.add_argument("--noted", action="store_true", help="选择范围：有备注的")
    p_batch.add_argument("--status", required=True, help="要改成什么状态")
    p_batch.add_argument("--tags", help="顺带贴上的问题标签，逗号分隔")
    p_batch.add_argument("--dry-run", action="store_true", help="只列出会命中哪些，不改动")

    p_undo = sub.add_parser("undo", help="撤销某条素材的上一步", parents=[common])
    p_undo.add_argument("--set", required=True, dest="set_id")
    p_undo.add_argument("--id", required=True, dest="item_id")

    p_export = sub.add_parser("export", help="导出审批结果", parents=[common])
    p_export.add_argument("--set", required=True, dest="set_id")
    p_export.add_argument("--format", default="json", choices=["json", "csv", "annotations"],
                          help="json=完整结构 csv=主表 annotations=圈注明细（一处一行）")
    p_export.add_argument("--only", default="all", choices=["all", "reviewed", "problem"])
    p_export.add_argument("--out", help="写到这个文件/目录；不给就直接把内容放进 JSON 返回")

    p_image = sub.add_parser("image", help="拿到素材图片的文件路径（给能自己读图的 agent）", parents=[common])
    p_image.add_argument("--set", required=True, dest="set_id")
    p_image.add_argument("--id", required=True, dest="item_id")
    p_image.add_argument("--side", default="b", choices=["a", "b"], help="a=源A b=源B")
    p_image.add_argument("--max-size", dest="max_size", type=int,
                         help="顺便生成一张这个尺寸的缩略图并返回路径")

    p_clear = sub.add_parser("clear-stale", help="清掉「源文件已变」的过期结果，让它们回到未审", parents=[common])
    p_clear.add_argument("--set", required=True, dest="set_id")
    p_clear.add_argument("--ids", help="只清这些 id；不给就清全部过期的")
    p_clear.add_argument("--dry-run", action="store_true")

    p_round = sub.add_parser("new-round", help="归档本轮结果并开始新一轮（会清空当前批次）", parents=[common])
    p_round.add_argument("--set", required=True, dest="set_id")
    p_round.add_argument("--yes", action="store_true", help="确认执行（必须显式给出）")

    p_prev = sub.add_parser("prev-round", help="上一轮被打回的素材 id", parents=[common])
    p_prev.add_argument("--set", required=True, dest="set_id")

    return parser


def run(args) -> dict:
    backend = make_backend(getattr(args, "config", DEFAULT_CONFIG),
                           server=getattr(args, "server", None),
                           offline=getattr(args, "offline", False))

    if args.command == "status":
        return backend.status()

    if args.command == "items":
        return backend.items(
            args.set_id, status=args.status, tag=args.tag, search=args.search,
            group=args.group, stale_only=args.stale, no_tags=args.no_tags,
            annotated=args.annotated, noted=args.noted,
            limit=args.limit, offset=args.offset)

    if args.command == "item":
        return backend.item(args.set_id, args.item_id)

    if args.command == "review":
        annotations = None
        if args.annotations or args.annotate:
            annotations = []
            if args.annotations:
                annotations.extend(json.loads(args.annotations))
            annotations.extend(parse_annotate(raw) for raw in args.annotate)
        return backend.review(args.set_id, args.item_id, status=args.status,
                              tags=split_list(args.tags), note=args.note,
                              annotations=annotations)

    if args.command == "batch":
        return backend.batch(
            args.set_id, ids=split_list(args.ids), status=args.status,
            tags=split_list(args.tags), dry_run=args.dry_run,
            filter_status=args.filter_status, filter_tag=args.filter_tag,
            search=args.search, group=args.group,
            stale_only=args.stale or None, no_tags=args.no_tags or None,
            annotated=args.annotated or None, noted=args.noted or None)

    if args.command == "undo":
        return backend.undo(args.set_id, args.item_id)

    if args.command == "export":
        return backend.export(args.set_id, fmt=args.format, only=args.only, out=args.out)

    if args.command == "image":
        return backend.image(args.set_id, args.item_id, side=args.side, max_size=args.max_size)

    if args.command == "clear-stale":
        return backend.clear_stale(args.set_id, ids=split_list(args.ids), dry_run=args.dry_run)

    if args.command == "new-round":
        if not args.yes:
            raise AgentError(
                "new-round 会把当前批次的结果归档并清空，必须加 --yes 明确确认；"
                "想先看看会归档什么，用 export 或 status。")
        return backend.new_round(args.set_id)

    if args.command == "prev-round":
        return backend.previous_round(args.set_id)

    raise AgentError(f"未知命令：{args.command}")


def main(argv=None) -> int:
    args = build_parser().parse_args(argv)
    try:
        result = run(args)
        status = 0
    except AgentError as exc:
        result = {"ok": False, "error": str(exc)}
        status = 1
    except json.JSONDecodeError as exc:
        result = {"ok": False, "error": f"--annotations 不是合法 JSON：{exc}"}
        status = 1
    except KeyboardInterrupt:
        result = {"ok": False, "error": "已中断"}
        status = 130

    print(json.dumps(result, ensure_ascii=False,
                     indent=2 if getattr(args, "pretty", False) else None))
    return status


if __name__ == "__main__":
    sys.exit(main())
