# Agent 工作流示例

几个可以直接照抄的完整流程。所有命令都会输出 JSON，方便串进脚本或工作流引擎。

先看一遍有什么：

```bash
python agent.py status
```

---

## 一、批量预审闭环（agent 看素材 → 判断 → 写回）

最适合「几百张素材、用视觉模型跑一遍预审」的场景。

### 1. 拉出待审清单

```bash
python agent.py items --set demo_cards --status unreviewed --limit 200 > pending.json
```

`pending.json` 里的 `items[].id` 就是后面要用的素材 id。

### 2. 取「对照图」，一次只看一张

关键点：**把原图和成品拼成一张**再喂给模型。分两次调用、靠文字描述对齐，
图标位置/分段间距/字体差异这类信息会丢得很厉害。

```bash
# 单张
python agent.py image-pair --set demo_cards --id 102_front.png --out /tmp/pair.png

# 批量：一次把 200 张全拼好，省掉 200 次进程启动
python agent.py image-pair --set demo_cards --ids-file ids.txt --out-dir /tmp/pairs
```

返回里 `pairs[].path` 是每张拼好的图（左原图、右成品，两侧已等高对齐）。
每张图左上角有标签，如果这条已经圈过问题，圈注框也会画上去。

> 拼接结果会缓存到 `data/pair_cache/`，源文件没变就直接复用；
> 重渲之后再跑会自动重新拼。

### 3. 让模型看图，产出结论

给模型的要求建议这样写（结论必须落到这三种状态 + 短语标签）：

```
看这张左右对照图（左＝源A，右＝源B）。逐处对比：
1. 文字有没有超出框、压到边框、和别的文字重叠
2. 分段和原文是否一致（标题/正文/分支有没有混在一起）
3. 该有图标的地方有没有图标
输出 JSON：{"status":"pass|reject|flag","tags":["..."],"note":"一句话说明"}
说不准就用 flag，不要猜着 pass。
```

`tags` 只能用预设词表里的（`agent.py status` 的返回里有）。写错词会被工具挡下来并给出建议。

### 4. 一次性写回

```json
[
  {"id": "102_front.png", "status": "reject", "tags": ["文字溢出"], "note": "正文压到边框外"},
  {"id": "103_front.png", "status": "pass", "tags": [], "note": "正常"},
  {"id": "104_front.png", "status": "flag", "tags": [], "note": "标题字体存疑，看不准"}
]
```

```bash
# 先预演：只会告诉你"将写入几条、哪些不合法"，不改动任何东西
python agent.py review-batch --set demo_cards --file reviews.json --dry-run

# 确认没问题再真跑
python agent.py review-batch --set demo_cards --file reviews.json
```

**这一批要么全写、要么全不写**：有一条不合法就整批拒绝，不会出现"写了一半"。
想只写合法的那些加 `--partial-ok`，返回里的 `failures` 会列出每条的失败原因。

### 5. 把打回原因变成下一步的修正清单

```bash
python agent.py export --set demo_cards --format advice --only problem --out 修正清单.json
```

输出里每条带 `问题标签` / `修正建议` / `原审批备注` / `圈选坐标`。
具体说什么由你自己配（见 `config.json` 的 `tag_advice`），工具只负责贴出来。

---

## 二、派生一个隔离的集合（重渲后只看这一批）

场景：大集合已经审过一轮，重渲了其中一批，想**单独把这一批审一遍**，
又不想把文件拷到新目录、也不想手改 `config.json`。

```bash
# 预演：先看会写出什么配置
python agent.py derive-set --from demo_cards --id recheck --name "重渲复核" \
    --b-root render/新的批次 --only-ids 107604_front.png,110806_front.png --dry-run

# 真跑：写到 sets.d/recheck.json，写完立刻可用（不用重启服务）
python agent.py derive-set --from demo_cards --id recheck --name "重渲复核" \
    --b-root render/新的批次 --only-ids 107604_front.png,110806_front.png
```

也可以用状态来挑：

```bash
# 把上一轮打回的那些挑出来做新集合
python agent.py derive-set --from demo_cards --id recheck2 --only-from reject
```

`only_ids` 是素材级白名单：**不用往新目录拷文件**也能"只看这 N 张"。

- 派生出来的集合写在 `sets.d/<id>.json`，**不会改动你手写的 `config.json`**；
- 删掉那个文件就等于删掉这个集合（`python agent.py drop-set --set recheck`）；
- 网页上也可以用：右下「轮次」区 → **派生新集合…**。

审完想对比两轮：

```bash
python agent.py compare --set recheck --other demo_cards
```

返回里 `rows[]` 逐条给出两个集合的状态、`changed` 标记状态变了的那些。

---

## 三、归档并开新轮（同一批素材、第 N 轮）

场景：素材没变，但你想把当前结果留档、从头再审一遍。

```bash
python agent.py export --set demo_cards --only reviewed --out 第1轮结果.json   # 先留个底
python agent.py new-round --set demo_cards --yes                              # 归档并清空

# 新一轮开始时，先把上轮打回的挑出来优先复审
python agent.py prev-round --set demo_cards
#   → {"ok": true, "round": 1, "ids": ["102_front.png", "104_front.png"], "total": 8}
# 拿上面的 ids 直接批量标记（也可以只挑其中几个）
python agent.py batch --set demo_cards --ids 102_front.png,104_front.png --status pass
```

归档文件是 `<素材集>.roundN.json`，格式和主结果文件一致，随时能读。
网页上对应「轮次」区的 **归档本轮，开始新一轮…** 和 **筛出上轮打回的**。

---

## 四、把圈注的区域抠出来（缺图标/局部对比）

场景：审的时候圈出"这里缺个图标"，想把这一小块裁出来当抠图素材或单独放大看。

```bash
# 打包成 zip（只给 --zip 时不保留散图）
python agent.py export-crops --set demo_cards --zip 圈选裁剪.zip

# 或者导出到目录，顺便放大两倍、四周多留 12 像素
python agent.py export-crops --set demo_cards --out 抠图素材/ --scale 2 --padding 12
```

网页上：右侧「导出」区 → **导出圈选裁剪（ZIP）**。

文件名形如 `102_front_1_b_溢出区域.png`（素材 id_第几处_哪一侧_说明），
返回里 `crops[].pixels` 是实际像素矩形。

---

## 五、素材在多个集合里出现过，怎么查

```bash
# 这张卡在所有集合里的结论
python agent.py lookup --id 102_front.png

# 只看某几个集合
python agent.py lookup --id 102_front.png --sets demo_cards,recheck
```

返回 `sets[]` 逐条给出 `set_id / status / tags / note / reviewed_at`，
用来回答"这张卡上一轮是不是我打回的、当时什么原因"。

---

## 附：几条容易踩的坑

| 情况 | 说明 |
|---|---|
| 服务正在跑，还想用 `--offline` | 会被拦住。服务把结果缓存在内存里，直连改文件会被它下次保存覆盖。要么别加 `--offline`（自动走 HTTP），要么先停服务，确实要覆盖再加 `--force` |
| `image-pair` 提示缺一侧图片 | 那条素材本身源A或源B缺失，`list_items` 里的 `has_source_a/b` 会告诉你 |
| `review-batch` 报标签不认识 | 报错里会给出最接近的预设标签，改掉再提交 |
| `stale: true` | 这条的源文件在审批之后被改过（重渲了），结论可能过期。要重新看一眼再决定，别沿用旧结论 |
| 想一次改很多条状态 | 用 `batch`（同状态），要每条不同状态/标签用 `review-batch`。两者都支持先 `--dry-run` |
