---
name: zhihu-scraping
description: |
  爬取知乎问题的高赞回答。两步法：先取问题页 initialData（5条预加载，含赞数，无成本），
  再用 API 列表 + 逐个 HTML 页面提取赞数和内容。checkpoint 增量保存支持中断续传。
trigger:
  - 用户要求爬取/分析某个知乎问题
  - 用户提供了知乎问题链接
  - 用户想了解某个知乎问题下大家都在怎么说
  - 毛选Skill调查阶段需要搜集知乎群众观点
  - 用户提到"zhihu-scraping"或"知乎skill"或"知乎爬虫"等关键词
  - 用户要求从知乎获取高赞回答
  - 任何包含 zhihu.com 链接的分析任务，在开始分析前先爬取该问题的高赞回答
---

## 爬取目标

三合一判定标准（优先级从高到低）：

1. **绝对高赞**：赞同数 ≥ 1000 的回答
2. **相对头部**：总回答数的前 1%
3. **最低保障**：最终输出不少于 10 个

## 核心发现：两源互补，仅此两源

经多轮实测，知乎获取回答赞数的渠道**仅有以下两个**：

| 渠道 | 途径 | 成本 | 返回 |
|------|------|------|------|
| **推荐流** | 问题页 HTML → `js-initialData` | 零（1次HTTP） | 固定5条预加载回答，**含赞数** |
| **默认流** | API v4 → 逐个回答 HTML | 每条约1秒 | 全量回答ID（列表无赞数），每个HTML含赞数+内容 |

### 已验证的结论

- **initialData 每次固定返回5条**：5次连续请求返回完全相同，URL参数不影响
- **API 所有 `sort_by` 值均等效**：`hot`/`vote`/`score`/`default`/`recommend`/`popular` 全部返回相同排序
- **API 排序非确定性**：不同时间调用同一问题，回答排序可能不同（实例：momo/7173赞，第一次排前30，第二次排100+）
- **无其他 endpoint**：`feeds` 与 initialData 相同；v3 API 返回HTML非JSON；无 `/top-answers`/`/recommend-answers` 等端点
- **两个渠道各有对方没有的高赞**：
  - Q1（1442回答）：initialData 有 4 个千赞回答被 API 扫描漏掉
  - Q2（462回答）：API 扫描有 5 个千赞回答不在 initialData 中（含最高赞 momo/7174）

### 策略建议

| 问题规模 | API 扫描深度 | 说明 |
|----------|-------------|------|
| < 200 回答 | 全量扫描 | 成本可控 |
| 200-500 回答 | 前 150-200 条 | 结合 initialData 的5条，通常可覆盖大部分高赞 |
| > 500 回答 | 前 200-300 条 | 幂律分布，尾部出现千赞概率极低 |

## 两步法工作流程

```
Phase 1 — 零成本发现（1次HTTP，秒级）:
  请求问题页 HTML → 解析 js-initialData → 提取5条预加载回答（含赞数、无完整内容）
  → 获得第一批高赞候选

Phase 2 — 渐进扫描（逐个HTML，含增量保存）:
  ① API v4 order_by=default → 分页获取全量 answer_id 列表（快，无赞数）
  ② 去重：跳过已在 Phase 1 获取的 answer_id
  ③ 逐个签名请求新增回答的 HTML 页面 → 提取 voteupCount + content
  ④ 每获取一条即写入 checkpoint（增量保存）
  ⑤ 合并 Phase 1 + Phase 2 结果，统计 ≥1000 赞数量

终止条件（满足任一即停止）:
  - 合并后 ≥1000 赞回答 ≥ 10 个 → 输出 top 10+
  - 全部 answer_id 已扫描 → 取赞数最高的 top 10
  - 总回答数 < 10 → 全量输出
  - 扫描至 top 20%（高赞集中在前部，无需全量扫描）
```

频率控制：每个 HTML 请求之间间隔 ≥1 秒。

## 去重规则

- 以 `answer_id` 为唯一标识，跨 Phase 去重
- Phase 1 的 5 个 ID 在 Phase 2 中自动跳过
- 多轮扩展时，checkpoint 中的已有 ID 自动跳过
- 最终输出按 `answer_id` 去重

## 增量保存与断点续传

脚本每获取一条回答即写入 checkpoint 文件：
- 路径：`data/zhihu/zhihu_q{id}_checkpoint.json`
- 内容：所有已获取的回答（含赞数和内容）+ 元信息
- 支持 `--resume` 参数，重跑时跳过已获取的 answer_id
- 随时 Ctrl+C 中断，下次 `--resume` 继续

# 知乎高赞回答爬取

## 概述

知乎有强反爬机制（ZSE），直接 HTTP 请求会被 403 拦截。可行方案需要：

1. **Chrome CDP 模式** — 连接用户已登录知乎的 Chrome 获取认证 cookie
2. **签名请求** — 通过 `libs/zhihu.js` + execjs 生成 `x-zse-96` / `x-zst-81` 头
3. **两源合并** — 问题页 initialData（5条预加载）+ API 列表 → HTML 提取
4. **客户端排序** — `order_by=vote` 已失效(403)，用 `default` 排序后按赞数客户端排序

## 工具脚本

本 Skill 配套脚本位于 `tools/crawl_zhihu_question.py`，是对 MediaCrawler 签名机制的薄封装。

### 使用方法

```bash
cd /Users/xsm/Documents/workspace/MediaCrawler && \
  uv run python /Users/xsm/Documents/workspace/maoxuan-skill/tools/crawl_zhihu_question.py \
  --url "https://www.zhihu.com/question/1920017559664714827" --count 15
```

参数：
- `--url` — 知乎问题链接（与 `--qid` 二选一）
- `--qid` — 知乎问题纯数字ID
- `--count` — 期望输出回答数（默认10）
- `--min-votes` — 赞数下限过滤（如 `--min-votes 1000`）
- `--resume` — 从 checkpoint 续传，跳过已获取的 answer_id
- `--delay` — HTML 请求间隔秒数（默认 1.0）
- `--json` — 仅输出JSON到stdout

输出：JSON 到 stdout + 文件保存到 `data/zhihu/zhihu_q{id}_top{count}.json`

输出结构：
```json
{
  "question_id": "xxx",
  "question_title": "xxx",
  "total_answers": 207,
  "total_fetched": 85,
  "fetched_count": 15,
  "high_vote_found": 6,
  "answers": [
    {
      "rank": 1,
      "answer_id": "xxx",
      "author_name": "xxx",
      "author_headline": "xxx",
      "voteup_count": 3151,
      "comment_count": 184,
      "content": "...",
      "url": "https://www.zhihu.com/question/xxx/answer/xxx",
      "source": "initialData"
    }
  ]
}
```

其中 `source` 字段标记来源（`initialData` 或 `api`），`high_vote_found` 为已发现千赞回答总数。

## 前置条件

### Chrome CDP 模式

用户需以调试模式启动 Chrome。macOS 上：

```bash
mkdir -p ~/.chrome-debug-profile
/Applications/Google\ Chrome.app/Contents/MacOS/Google\ Chrome \
  --remote-debugging-port=9222 \
  --user-data-dir="$HOME/.chrome-debug-profile"
```

首次启动后需在 Chrome 中登录知乎。后续登录状态会持久化在 profile 中。

### 依赖

脚本依赖 MediaCrawler 的 venv（含 `playwright`, `httpx`, `pyexecjs`, `parsel`），以及系统的 `node`（pyexecjs 运行时）。

验证 CDP 连通性：
```bash
curl -s http://localhost:9222/json/version | head -1
```

## 知乎 API 注意事项

### 1. `order_by=vote` 已失效

使用 `order_by=vote` 排序返回 `10003` 错误。只能用 `order_by=default`。

### 2. 列表 API 不返回内容和赞数

`/api/v4/questions/{qid}/answers` 返回的回答对象**不包含**：
- ~~`content`~~ — 缺失
- ~~`voteup_count`~~ — 缺失
- ~~`comment_count`~~ — 缺失

仅返回：`id`, `author`, `question`, `type`, `created_time`, `updated_time`, `url`。

### 3. 问题页 initialData 预加载 5 条回答（含赞数，无成本）

问题页 HTML 的 `<script id="js-initialData">` 中预加载了 5 条推荐回答，包含 `voteupCount`。可零成本获取第一批高赞候选。**注意：这 5 条和 API `order_by=default` 的排序不完全重合。**

### 4. HTML 页面请求也需要签名

直接 HTTP 请求回答页面也会触发 ZSE 检查。必须对每个 HTML URL 做签名。

### 5. feeds API 与 initialData 返回相同数据

`/api/v4/questions/{qid}/feeds` 返回的回答列表与问题页 initialData 完全一致，不需要单独调用。

## 常见问题

- **CDP 连接失败**：检查 Chrome 是否以 `--remote-debugging-port=9222` 启动，且使用非默认 `--user-data-dir`
- **API 返回 403**：cookie 可能已过期或未登录（检查 `z_c0` 是否存在）。在 Chrome 中重新登录知乎
- **内容为空**：检查 `js-initialData` 中 `entities.answers` 和 `entities.content` 两个路径
- **node 不可用**：pyexecjs 需要 node 运行时来编译 `libs/zhihu.js`
- **initialData 未提取到**：知乎可能调整页面结构，检查正则 `<script id="js-initialData" type="text/json">(.*?)</script>`
