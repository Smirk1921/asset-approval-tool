# -*- coding: utf-8 -*-
"""agent 接口的共享原语：错误类型、状态/标签/圈选的规范化与校验。

agent_api（直连实现）和 agent_ext（拼接/裁剪/批量写回等扩展能力）都用这里的函数，
单独放一个文件是为了避免两者互相 import 形成循环。
"""
from __future__ import annotations


# 中文 / 英文 / 缩写都认，agent 传哪个都行
_STATUS_ALIASES = {
    "pass": "pass", "通过": "pass", "ok": "pass", "approved": "pass", "approve": "pass",
    "reject": "reject", "打回": "reject", "fail": "reject", "failed": "reject", "rejected": "reject",
    "flag": "flag", "存疑": "flag", "doubt": "flag", "unsure": "flag", "question": "flag",
    "": "", "none": "", "未审": "", "unreviewed": "", "pending": "", "todo": "", "clear": "",
}
_STATUS_FILTERS = {
    "all": None, "全部": None, "": None,
    "unreviewed": "", "未审": "", "pending": "",
    "pass": "pass", "通过": "pass",
    "reject": "reject", "打回": "reject",
    "flag": "flag", "存疑": "flag",
}


class AgentError(Exception):
    """给 agent 看的错误：消息直接可读，不该让它去猜。"""


def normalize_status(value, *, allow_filter: bool = False):
    """把各种写法的状态统一成 pass/reject/flag/''。"""
    if value is None:
        return None
    key = str(value).strip().lower()
    if allow_filter:
        if key in _STATUS_FILTERS:
            return _STATUS_FILTERS[key]
        raise AgentError(f"状态筛选值不认识：{value!r}（可用 all/unreviewed/pass/reject/flag）")
    if key in _STATUS_ALIASES:
        return _STATUS_ALIASES[key]
    raise AgentError(f"状态值不认识：{value!r}（可用 pass/reject/flag/'' 或 通过/打回/存疑/未审）")


def suggest_tag(tag: str, known: list[str]) -> str | None:
    """标签写错了给个最接近的建议。

    中文场景下 difflib 效果一般（"英文未抹除" vs "英文残留"），所以再加一层
    「最长公共连续子串」判断——共享"英文"两个字就足以提示出正确标签。
    """
    import difflib
    text = str(tag)
    known = list(known)
    if not known:
        return None
    matches = difflib.get_close_matches(text, known, n=1, cutoff=0.4)
    if matches:
        return matches[0]

    def longest_common_run(a: str, b: str) -> int:
        best = 0
        for i in range(len(a)):
            for j in range(i + 1, len(a) + 1):
                if a[i:j] in b:
                    best = max(best, j - i)
        return best

    scored = sorted(((longest_common_run(text, c), c) for c in known), key=lambda x: -x[0])
    top_run, top_tag = scored[0]
    return top_tag if top_run >= 2 else None


def validate_tags(tags, known: list[str], *, strict: bool = True) -> list[str]:
    """检查标签是不是配置里预设的那几个。

    agent 手写标签很容易写成同义词（"英文未抹除"/"分段错误"），
    那样写进去的标签在筛选和统计里就永远对不上，所以这里要拦一下。
    strict=True 直接报错，False 只返回提醒。
    """
    if tags is None:
        return []
    unknown = [str(t) for t in tags if str(t) not in set(known)]
    if not unknown:
        return []
    lines = []
    for tag in unknown:
        hint = suggest_tag(tag, known)
        lines.append(f"标签 {tag!r} 不在预设列表里" + (f"，是不是要写 {hint!r}？" if hint else ""))
    if strict:
        raise AgentError("；".join(lines) + f"（预设标签：{'、'.join(known)}）")
    return lines


def normalize_annotations(raw) -> list[dict]:
    """校验并规范化圈选区域。坐标一律是相对图片的 0~1 比例。"""
    if raw is None:
        return []
    if isinstance(raw, str):
        try:
            raw = json.loads(raw)
        except json.JSONDecodeError as exc:
            raise AgentError(f"annotations 不是合法 JSON：{exc}") from exc
    if not isinstance(raw, list):
        raise AgentError("annotations 必须是数组，每项形如 {x,y,w,h,side,note}")
    out = []
    for index, item in enumerate(raw, 1):
        if not isinstance(item, dict):
            raise AgentError(f"第 {index} 个圈选不是对象")
        try:
            box = {k: float(item.get(k, 0)) for k in ("x", "y", "w", "h")}
        except (TypeError, ValueError) as exc:
            raise AgentError(f"第 {index} 个圈选的 x/y/w/h 必须是数字") from exc
        for key, value in box.items():
            if not 0.0 <= value <= 1.0:
                raise AgentError(f"第 {index} 个圈选的 {key}={value} 越界，必须是 0~1 的比例")
        side = str(item.get("side") or "b").strip().lower()
        if side not in ("a", "b"):
            raise AgentError(f"第 {index} 个圈选的 side 只能是 a 或 b")
        out.append({**box, "side": side,
                    "note": str(item.get("note") or ""),
                    "tag": str(item.get("tag") or "")})
    return out
