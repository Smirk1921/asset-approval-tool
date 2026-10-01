# AGENTS.md — 给 AI agent 的使用说明

这个仓库是一个**本地素材审批工具**：并排对照「原图 / 成品」，一键标记通过·打回·存疑，
支持问题标签、圈图标注、批量操作，结果实时写盘并可导出 JSON/CSV。

你（agent）可以直接驱动它。完整参数见 [`docs/agent-接口.md`](docs/agent-接口.md)。

## 三条通路，优先用 MCP

| 方式 | 什么时候用 |
|---|---|
| **MCP**（`mcp_server.py`） | 首选。工具直接出现在你的工具列表里，图片也能直接返回给你看 |
| **CLI**（`agent.py`） | 没有 MCP 时；每次调用输出一个 JSON，适合 shell 脚本 |
| **HTTP**（`app.py` 起的服务） | 用户正在网页上审批时，走这条和用户共用同一份状态 |

三者语义完全一致。**服务在跑就走 HTTP，没跑就直接读写文件** —— 这条规则自动生效，
你不用手动选。原因：网页版把结果缓存在内存里，你若在服务运行时直接改文件，改动会被覆盖。

MCP 配置：

```json
{
  "mcpServers": {
    "asset-approval": {
      "command": "python",
      "args": ["<仓库路径>/mcp_server.py"]
    }
  }
}
```

## 标准审批流程

```
approval_status                      # 1. 有哪些素材集、各自进度、有没有过期结果
list_items(status="unreviewed")      # 2. 找待审的
get_item_images(set, id, "both")     # 3. 看内容——原图和成品一起给你
review_item(set, id, status="reject", tags=["文字溢出"], annotations=[...])   # 4. 下结论
```

文本类素材集用 `get_item`（直接返回原文与译文正文），图片类用 `get_item_images`。

## 判断与写结论的规矩

- **`status`**：`pass` 通过 / `reject` 打回 / `flag` 存疑 / `""` 清除标记。
  **拿不准就 `flag`，不要猜着 `pass`**。通过意味着"可以发布"，代价比打回大。
- **`tags`** 用 `approval_status` 返回的 `tags` 列表里的词，不要自己造。
- **`annotations`** 坐标是**相对图片的 0~1 比例**（左上角原点），不是像素。
  `side: "b"` 指画在成品上，`"a"` 指原图。每处圈选都要写 `note` 说明问题是什么。
- 只传你要改的字段；没传的字段保持原值。

## 安全红线

1. **批量操作必须给范围**。`batch_review` 不给 `ids` 也不给 `filter_*` 会直接报错——
   这是故意的，防止一句"都通过"刷掉整个素材集。要作用于全部，显式传 `filter_status="all"`。
2. **先预演再执行**。`batch_review(dry_run=true)` 会告诉你将命中哪些、共几条，确认无误再真跑。
3. **批量里 `status` 是"改成什么"，筛选状态要用 `filter_status`**。写错会打错目标。
4. **不要动 `data/` 里的文件**。那是审批结果的存储目录，格式见
   [`docs/审批结果字段说明.md`](docs/审批结果字段说明.md)；要改结果请走工具接口。
5. **`start_new_round` 会归档并清空当前批次**，只在用户明确要求开始新一轮时调用。
6. **`clear_stale_results` 只清"源文件在我审批之后又被改过"的结果**（说明成品重渲过，
   旧结论可能不成立）。清之前先 `dry_run=true` 看清单。

## 你会遇到的几种"异常"，都不是 bug

- **`stale: true`** —— 这条的源文件在审批之后被改过（重渲了），旧结论可能过期。
  正确做法是重新看图再决定，而不是沿用旧结论。
- **`has_source_a/b: false`** —— 这一侧缺素材（原图没找到 / 还没渲染）。
  这类不要判 `pass`。
- **`transport: "direct"` + `warning`** —— 说明有服务跑在别的端口，
  按提示改用 `--server http://127.0.0.1:<port>`，别直接改文件。

## 仓库里有什么

```
app.py            网页版服务入口（用户界面）
agent.py          命令行接口（给 agent / 脚本）
mcp_server.py     MCP 服务（stdio，无额外依赖）
core/agent_api.py 操作层：HTTP 与直连两套实现，语义一致
core/            配置、素材扫描配对、结果读写、缩略图
demo/             合成演示素材，克隆即可跑（demo/generate_demo.py 可重新生成）
docs/             接口文档与结果字段说明
```

跑起来：`python app.py`（网页版）或 `python agent.py status`（命令行）。
依赖只有 Flask 和 Pillow。
