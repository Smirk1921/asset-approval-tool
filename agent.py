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
    common.add_argument("--force", action="store_true", default=argparse.SUPPRESS,
                        help="直连模式下强制写入。默认会拦住「服务在跑时的直连写」，避免改动被服务覆盖")

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
    p_review.add_argument("--template", help="用预设的标签组合（agent.py templates 可看清单）")
    p_review.add_argument("--lenient-tags", dest="lenient_tags", action="store_true",
                          help="标签不在预设里也只提醒不报错")

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
    p_batch.add_argument("--template", help="用预设的标签组合")
    p_batch.add_argument("--lenient-tags", dest="lenient_tags", action="store_true",
                         help="标签不在预设里也只提醒不报错")

    p_undo = sub.add_parser("undo", help="撤销某条素材的上一步", parents=[common])
    p_undo.add_argument("--set", required=True, dest="set_id")
    p_undo.add_argument("--id", required=True, dest="item_id")

    p_export = sub.add_parser("export", help="导出审批结果", parents=[common])
    p_export.add_argument("--set", required=True, dest="set_id")
    p_export.add_argument("--format", default="json",
                          choices=["json", "csv", "annotations", "advice"],
                          help="json=完整结构 csv=主表 annotations=圈注明细（一处一行）"
                               " advice=打回原因展开成修正建议（话术取自配置的 tag_advice）")
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

    # ---- v1.1 新增 ----
    p_pair = sub.add_parser("image-pair", parents=[common],
                            help="把源A/源B 拼成一张对照图（视觉模型一次只吃一张图时用）")
    p_pair.add_argument("--set", dest="set_id", help="素材集 id（配合 --id 使用）")
    p_pair.add_argument("--id", dest="item_id", help="素材 id")
    p_pair.add_argument("--a", help="自由模式：左图/上图路径（配 --b，可跨素材集对比两版渲染）")
    p_pair.add_argument("--b", help="自由模式：右图/下图路径")
    p_pair.add_argument("--layout", default="horizontal", choices=["horizontal", "vertical"],
                        help="horizontal=左右拼接（默认） vertical=上下拼接")
    p_pair.add_argument("--max-width", dest="max_width", type=int, default=2048,
                        help="拼好后总宽上限，超出整体等比缩小（默认 2048）")
    p_pair.add_argument("--max-height", dest="max_height", type=int)
    p_pair.add_argument("--gap", type=int, default=8, help="两图间隔像素（默认 8）")
    p_pair.add_argument("--labels", help="自定义标签，逗号分隔，如 英文原图,中文成品")
    p_pair.add_argument("--no-label", dest="no_label", action="store_true", help="不画标签")
    p_pair.add_argument("--no-annotations", dest="no_annotations", action="store_true",
                        help="不把已有圈注画上去")
    p_pair.add_argument("--out", help="输出文件；不给他就存到缓存目录并返回路径")
    p_pair.add_argument("--ids", help="批量模式：逗号分隔的素材 id")
    p_pair.add_argument("--ids-file", dest="ids_file", help="批量模式：每行一个 id 的文件")
    p_pair.add_argument("--out-dir", dest="out_dir", help="批量模式：输出目录")

    p_rb = sub.add_parser("review-batch", parents=[common],
                          help="一次写回多条审批结果（先全校验，有一条不合法就整批不写）")
    p_rb.add_argument("--set", required=True, dest="set_id")
    p_rb.add_argument("--file", help="JSON 文件，内容形如 [{id,status,tags,note}]")
    p_rb.add_argument("--stdin", action="store_true", help="从标准输入读 JSON")
    p_rb.add_argument("--dry-run", action="store_true", help="只校验、不写入")
    p_rb.add_argument("--partial-ok", dest="partial_ok", action="store_true",
                      help="只写合法的那些，不合法的列在 failures 里")
    p_rb.add_argument("--lenient-tags", dest="lenient_tags", action="store_true")

    p_crops = sub.add_parser("export-crops", parents=[common],
                             help="把圈注框裁成小图导出（抠图素材、单独发给子代理看）")
    p_crops.add_argument("--set", required=True, dest="set_id")
    p_crops.add_argument("--out", dest="out_dir", help="输出目录")
    p_crops.add_argument("--zip", dest="zip_path", help="打包成 zip（只给 zip 时不留散图）")
    p_crops.add_argument("--only", default="problem", choices=["all", "reviewed", "problem"])
    p_crops.add_argument("--side", choices=["a", "b"], help="强制只裁这一侧；不给就按圈注自己标的边")
    p_crops.add_argument("--padding", type=int, default=8, help="向外多留几像素（默认 8）")
    p_crops.add_argument("--scale", type=float, default=1.0, help="放大倍数，比如 2 放大两倍导出")

    p_ds = sub.add_parser("derive-set", parents=[common],
                          help="从现有素材集派生一个新集合（写到 sets.d/，不动手写的 config）")
    p_ds.add_argument("--from", required=True, dest="from_set")
    p_ds.add_argument("--id", required=True, dest="new_id", help="新集合 id")
    p_ds.add_argument("--name", help="新集合显示名")
    p_ds.add_argument("--a-root", dest="a_root", help="换源A目录")
    p_ds.add_argument("--b-root", dest="b_root", help="换源B目录（重渲后的新目录）")
    p_ds.add_argument("--only-ids", dest="only_ids", help="只含这些素材 id，逗号分隔")
    p_ds.add_argument("--only-from", dest="only_from",
                      help="按状态挑：unreviewed/reject/flag/pass/all")
    p_ds.add_argument("--dry-run", action="store_true")

    p_drop = sub.add_parser("drop-set", parents=[common], help="删掉一个派生出来的集合")
    p_drop.add_argument("--set", required=True, dest="set_id")
    p_drop.add_argument("--dry-run", action="store_true")

    p_lookup = sub.add_parser("lookup", parents=[common],
                              help="同一个素材 id 在各个集合里的结论")
    p_lookup.add_argument("--id", required=True, dest="item_id")
    p_lookup.add_argument("--sets", help="只看这几个集合，逗号分隔")

    p_cmp = sub.add_parser("compare", parents=[common],
                           help="两个集合按素材 id 对比结论（上轮打回的这轮怎样了）")
    p_cmp.add_argument("--set", required=True, dest="set_id")
    p_cmp.add_argument("--other", required=True)

    sub.add_parser("templates", parents=[common], help="列出预设的审批模板与标签", )

    return parser


def run(args) -> dict:
    backend = make_backend(getattr(args, "config", DEFAULT_CONFIG),
                           server=getattr(args, "server", None),
                           offline=getattr(args, "offline", False),
                           force_write=getattr(args, "force", False))

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
                              annotations=annotations, template=args.template,
                              lenient_tags=args.lenient_tags)

    if args.command == "batch":
        return backend.batch(
            args.set_id, ids=split_list(args.ids), status=args.status,
            tags=split_list(args.tags), dry_run=args.dry_run,
            template=args.template, lenient_tags=args.lenient_tags,
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

    # ---- v1.1 新增 ----
    if args.command == "image-pair":
        labels = None
        if args.labels:
            parts = [p.strip() for p in args.labels.replace("，", ",").split(",") if p.strip()]
            if len(parts) >= 2:
                labels = (parts[0], parts[1])
        elif args.no_label:
            labels = ("", "")
        options = dict(layout=args.layout, max_width=args.max_width,
                       max_height=args.max_height, gap=args.gap, labels=labels,
                       draw_annotations=not args.no_annotations)
        ids = split_list(args.ids)
        if args.ids_file:
            ids = [line.strip() for line in
                   Path(args.ids_file).read_text(encoding="utf-8").splitlines() if line.strip()]
        if ids:
            if not args.set_id or not args.out_dir:
                raise AgentError("批量模式需要 --set 和 --out-dir")
            return backend.pair_images(args.set_id, ids, out_dir=args.out_dir, **options)
        if args.a and args.b:
            return backend.pair_image(a=args.a, b=args.b, out=args.out, **options)
        if not args.set_id or not args.item_id:
            raise AgentError("要么给 --set + --id，要么给 --a + --b")
        return backend.pair_image(args.set_id, args.item_id, out=args.out, **options)

    if args.command == "review-batch":
        if args.stdin:
            raw = sys.stdin.read()
        elif args.file:
            raw = Path(args.file).read_text(encoding="utf-8")
        else:
            raise AgentError("需要 --file 或 --stdin")
        try:
            reviews = json.loads(raw)
        except json.JSONDecodeError as exc:
            raise AgentError(f"内容不是合法 JSON：{exc}") from exc
        return backend.review_batch(args.set_id, reviews, dry_run=args.dry_run,
                                    partial_ok=args.partial_ok,
                                    lenient_tags=args.lenient_tags)

    if args.command == "export-crops":
        return backend.export_crops(args.set_id, out_dir=args.out_dir,
                                    zip_path=args.zip_path, only=args.only,
                                    side=args.side, padding=args.padding,
                                    scale=args.scale)

    if args.command == "derive-set":
        if not args.dry_run:
            pass  # 写配置的保护在 backend 里做（HTTP 通路不需要）
        return backend.derive_set(
            from_set=args.from_set, new_id=args.new_id, name=args.name,
            a_root=args.a_root, b_root=args.b_root,
            only_ids=split_list(args.only_ids), only_from=args.only_from,
            dry_run=args.dry_run)

    if args.command == "drop-set":
        return backend.drop_set(args.set_id, dry_run=args.dry_run)

    if args.command == "lookup":
        return backend.lookup(args.item_id, sets=split_list(args.sets))

    if args.command == "compare":
        return backend.compare_sets(args.set_id, args.other)

    if args.command == "templates":
        return backend.templates()

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
