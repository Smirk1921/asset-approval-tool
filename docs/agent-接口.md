# Agent 接口文档

给 AI agent 和脚本用的接口。三种通路，**语义完全一致**，选一个用就行。

- [一、选哪条通路](#一选哪条通路)
- [二、命令行 `agent.py`](#二命令行-agentpy)
- [三、MCP 服务 `mcp_server.py`](#三mcp-服务-mcp_serverpy)
- [四、HTTP 接口](#四http-接口)
- [五、返回的数据长什么样](#五返回的数据长什么样)
- [六、状态与标签词汇表](#六状态与标签词汇表)
- [七、错误与安全约束](#七错误与安全约束)

---

## 一、选哪条通路

| 通路 | 适用 | 特点 |
|---|---|---|
| MCP | 支持 MCP 的 agent | 工具直接可用，**图片能作为图片返回给视觉模型** |
| CLI | 没有 MCP 时、shell 脚本 | 每次调用输出**一个 JSON**，退出码非 0 表示失败 |
| HTTP | 用户正开着网页版 | 和网页版共用同一份内存状态 |

**自动选路**：CLI 与 MCP 默认先探测配置里的 `host:port`，服务在跑就走 HTTP，
没跑就直接读写文件。这样不会出现「agent 改了文件、被服务的下一次保存覆盖」。

如果服务是用 `--port` 起的非默认端口，探测会落空。工具靠 `data/app.pid` 兜底：
发现那个端口上真有服务在听，就在返回里给一条 `warning`，告诉你怎么改。手动指定用
`--server http://127.0.0.1:<port>`，强制直连用 `--offline`。

---

## 二、命令行 `agent.py`

```bash
python agent.py [--config 配置路径] [--server URL] [--offline] [--pretty] <命令> [选项]
```

全局选项写在子命令前后都行。**每次调用只往 stdout 打印一个 JSON 对象**，失败时退出码为 1。

### `status` — 总览

```bash
python agent.py status
```

返回配置路径、数据目录、当前走哪条通路、各素材集的条数与进度、过期条数、当前轮次。

### `items` — 列出素材

```bash
python agent.py items --set demo_cards --status unreviewed --limit 20
python agent.py items --set demo_cards --tag 文字溢出
python agent.py items --set demo_cards --search 102 --stale
```

| 选项 | 说明 |
|---|---|
| `--set` | 素材集 id（必填） |
| `--status` | `unreviewed` / `pass` / `reject` / `flag` / `all`（也认 未审/通过/打回/存疑） |
| `--tag` | 按问题标签筛 |
| `--search` | 按 id / 名称 / 元数据模糊搜 |
| `--group` | 按分组筛 |
| `--stale` | 只看源文件已变、结果可能过期的 |
| `--no-tags` | 只看没打任何标签的 |
| `--annotated` / `--noted` | 只看有圈选 / 有备注的 |
| `--limit` / `--offset` | 分页 |

返回里 `matched` 是命中总数，`items` 是本页内容。

### `item` — 单条详情

```bash
python agent.py item --set demo_cards --id 102_front.png
```

图片素材返回 `files.a` / `files.b` 两个文件路径；文本素材返回 `texts.a` / `texts.b` 正文。

### `review` — 写审批结果

```bash
python agent.py review --set demo_cards --id 102_front.png \
  --status 打回 --tags 文字溢出,分段错 --note "正文超出文本框" \
  --annotate "0.09,0.55,0.82,0.28,b,正文溢出到框外"
```

| 选项 | 说明 |
|---|---|
| `--status` | `pass`/`reject`/`flag`/`""`，也认 通过/打回/存疑/未审 |
| `--tags` | 逗号分隔，也认中文逗号 |
| `--note` | 备注 |
| `--annotate` | 圈一处问题，格式 `x,y,w,h[,side][,说明]`，**可重复**；说明放最后所以里面可以有逗号 |
| `--annotations` | 直接给圈选数组的 JSON（需要精细控制时用） |

### `batch` — 批量改状态

```bash
# 先预演：会命中哪些、共几条
python agent.py batch --set demo_cards --filter-status unreviewed --status pass --dry-run
# 确认后执行
python agent.py batch --set demo_cards --filter-status unreviewed --status pass
# 指定 id
python agent.py batch --set demo_cards --ids 101_front.png,102_front.png --status reject --tags 其它
```

**必须给范围**：`--ids`，或至少一个 `--filter-status/--filter-tag/--search/--group/--stale/--no-tags/--annotated/--noted`。
要作用于全部素材，显式传 `--filter-status all`。

注意 `--status` 是**要改成什么**，筛选状态要用 `--filter-status`。

### 其余命令

| 命令 | 说明 |
|---|---|
| `undo --set S --id I` | 撤销某条的上一步（撤销记录只存在于当前服务进程内，重启后失效） |
| `export --set S [--format json\|csv\|annotations] [--only all\|reviewed\|problem] [--out 路径]` | 不给 `--out` 就把内容放进 JSON 返回 |
| `image --set S --id I [--side a\|b] [--max-size N]` | 拿图片文件路径，`--max-size` 顺便生成缩略图 |
| `clear-stale --set S [--ids ...] [--dry-run]` | 清掉过期结果，让它们回到未审 |
| `new-round --set S --yes` | 归档本轮、开始新一轮（**必须加 `--yes`**） |
| `prev-round --set S` | 上一轮被打回的素材 id |

---

## 三、MCP 服务 `mcp_server.py`

标准库实现，不需要装 `mcp` SDK。配置：

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

可选参数：`--config 配置路径`、`--server URL`、`--offline`。
`python mcp_server.py --list-tools` 可以看工具清单（自检用）。

| 工具 | 必填参数 | 说明 |
|---|---|---|
| `approval_status` | — | 各素材集进度总览 |
| `list_items` | `set` | 列出素材，支持 `status/tag/search/group/stale_only/no_tags/annotated/noted/limit/offset` |
| `get_item` | `set`, `id` | 单条详情（图片给路径，文本给正文） |
| `get_item_images` | `set`, `id` | **把图片本身返回给你看**，`side` 可为 `a`/`b`/`both`（默认 both），`max_size` 默认 900 |
| `review_item` | `set`, `id` | 写结论：`status`/`tags`/`note`/`annotations` |
| `batch_review` | `set`, `status` | 批量改状态，范围用 `ids` 或 `filter_*`；支持 `dry_run` |
| `export_results` | `set` | `format`: json/csv/annotations；`only`: all/reviewed/problem |
| `clear_stale_results` | `set` | 清过期结果，支持 `dry_run` |
| `start_new_round` | `set` | 归档本轮、开始新一轮 |
| `list_previous_round_rejects` | `set` | 上轮打回的 id |

### 关于 `get_item_images`

返回的是 MCP 的 image content 块（JPEG 预览，base64）。`side: "both"` 会一次给
**英文原图和中文成品两张**，视觉模型可以直接对照找问题。预览长边默认 900 像素，
按需调 `max_size`（越大越清晰也越费 token）。原始分辨率图片的路径在同一次返回的
文本块里，需要精修时按路径取。

---

## 四、HTTP 接口

服务由 `python app.py` 启动（默认 <http://127.0.0.1:8765/>）。
`/api/agent/*` 是给程序用的，`/api/set/*` 是网页版自己用的，两者共用同一份状态。

| 方法 | 路径 | 说明 |
|---|---|---|
| GET | `/api/agent/status` | 总览 |
| GET | `/api/agent/items?set=&status=&tag=&search=&group=&stale=&no_tags=&annotated=&noted=&limit=&offset=` | 列出素材 |
| GET | `/api/agent/item?set=&id=` | 单条详情 |
| POST | `/api/agent/review` | `{set, id, status, tags, note, annotations}` |
| POST | `/api/agent/batch` | `{set, status, tags, ids?}` 或 `{set, status, tags, filter_status?/filter_tag?/search?/group?/stale?/no_tags?/annotated?/noted?, dry_run?}` |
| POST | `/api/agent/undo` | `{set, id}` |
| POST | `/api/agent/clear-stale` | `{set, ids?, dry_run?}` |
| POST | `/api/agent/new-round` | `{set}` |
| GET | `/api/agent/previous-round?set=` | 上轮打回的 id |
| POST | `/api/agent/export` | `{set, format, only, out?}`，不给 `out` 就把内容放在 `content` 里 |
| GET | `/api/agent/image?set=&id=&side=&max_size=` | 图片文件路径（`max_size` 时另存缩略图） |

例：

```bash
curl -s "http://127.0.0.1:8765/api/agent/items?set=demo_cards&status=unreviewed&limit=5"

curl -s -X POST http://127.0.0.1:8765/api/agent/review \
  -H "Content-Type: application/json" \
  -d '{"set":"demo_cards","id":"102_front.png","status":"reject","tags":["文字溢出"],
       "annotations":[{"x":0.09,"y":0.55,"w":0.82,"h":0.28,"side":"b","note":"正文溢出到框外"}]}'
```

---

## 五、返回的数据长什么样

### `status` / `approval_status`

```json
{
  "ok": true,
  "config_path": ".../config.json",
  "data_dir": ".../data",
  "server": "http://127.0.0.1:8765/",
  "transport": "http",
  "tags": ["标题混排", "字体错", "..."],
  "sets": [
    {
      "id": "demo_cards",
      "name": "演示·单卡对照（英文原图 ↔ 中文成品）",
      "type": "image",
      "note": "素材集说明",
      "source_a": "英文原图", "source_b": "中文成品",
      "count": 8, "reviewed": 3, "pass": 2, "reject": 1, "flag": 0,
      "unreviewed": 5, "stale": 0, "round": 1,
      "error": null
    }
  ]
}
```

### `list_items` 里每条

```json
{
  "id": "102_front.png",
  "name": "102_front.png",
  "group": "根目录",
  "meta": { "卡号": "102", "类型": "探索卡", "扩展": "基础", "英文标题": "Ancient Ruins" },
  "status": "reject",
  "status_cn": "打回",
  "tags": ["文字溢出", "分段错"],
  "note": "正文超出文本框",
  "annotations": 1,
  "stale": false,
  "has_source_a": true,
  "has_source_b": true
}
```

### `item` / `review_item` 里多出来的部分

```json
{
  "type": "image",
  "reviewed_at": "2026-10-01 15:20:31",
  "round": 1,
  "annotation_list": [
    { "x": 0.09, "y": 0.55, "w": 0.82, "h": 0.28, "side": "b", "note": "正文溢出到框外", "tag": "" }
  ],
  "source_labels": { "a": "英文原图", "b": "中文成品" },
  "files": { "a": ".../demo/original/102_front.png", "b": ".../demo/rendered/102_front.png" }
}
```

文本素材集则是 `"texts": { "a": "英文原文…", "b": "中文译文…" }` 而不是 `files`。

### `batch` 返回

```json
{
  "ok": true, "changed": 5, "matched": 5, "dry_run": false,
  "ids": ["101_front.png", "..."],
  "stats": { "total": 8, "pass": 5, "reject": 1, "flag": 0, "unreviewed": 2, "stale": 0, "round": 1 }
}
```

`dry_run: true` 时 `changed` 恒为 0，`ids` 是要命中的清单。

---

## 六、状态与标签词汇表

`status` 取值（中英文都认）：

| 值 | 中文 | 含义 |
|---|---|---|
| `pass` | 通过 | 可以发布 |
| `reject` | 打回 | 有问题，要修 |
| `flag` | 存疑 | 拿不准，留给人工定 |
| `""` | 未审 | 清除标记，回到未审 |

标签**不是固定的**，以 `approval_status` 返回的 `tags` 数组为准（由用户在配置里定义）。
默认那套是：标题混排 / 字体错 / 黑底黑字 / 图标缺失 / 图标误抹 / 分段错 / 文字溢出 /
术语错 / 图标错位 / 背景残留 / 其它。

---

## 七、错误与安全约束

### 错误怎么表示

- **CLI**：stdout 上仍是合法 JSON，形如 `{"ok": false, "error": "……"}`，退出码 1。
- **MCP**：工具结果里 `isError: true`，`content[0].text` 是可读的原因（不是协议层异常，
  模型能自己看懂并纠正）。
- **HTTP**：`{"ok": false, "error": "……"}`，HTTP 400。

错误信息都写成可读的中文，直接照做即可。

### 三道安全闸

1. **批量必须给范围**：`batch` 不给 `ids` 也不给筛选条件 → 直接报错。
   想作用于全部要显式 `filter_status="all"`。防的是"一句都通过刷掉整个素材集"。
2. **`dry_run` 预演**：`batch` 和 `clear-stale` 都支持，先看命中清单再执行。
3. **`new-round` 要显式确认**（CLI 的 `--yes`），因为它会清空当前批次。

### 不要直接改 `data/` 下的文件

那是审批结果的存储位置。格式说明见 [`审批结果字段说明.md`](审批结果字段说明.md)。
**服务运行时直接改文件会被服务的下一次保存覆盖** —— 用接口，不要用文件。
