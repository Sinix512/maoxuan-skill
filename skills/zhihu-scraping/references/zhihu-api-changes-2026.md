# Session: 2026-04-29 — Zhihu scraping debug session

## Question Scraped
- URL: `https://www.zhihu.com/question/8881520651/answer/2023169170347435380`
- Question ID: `8881520651`
- Title: "为什么好多大叔喜欢找年轻的呢？"
- Total answers: 3,594
- Top answer vote count: 4,733 (author: 归衡)

## What Worked

### CDP Connection
```python
browser = await p.chromium.connect_over_cdp("ws://localhost:9222/devtools/browser", timeout=30000)
```
Works after user enables `chrome://inspect/#remote-debugging`.

### API Endpoint (limited)
`/api/v4/questions/{qid}/answers?limit=20&offset=0&order_by=default`
- Works — returns answer list with IDs, author info, timestamps
- Does NOT return: content, voteup_count, comment_count
- `order_by=vote` returns 403 (broken)

### Content Extraction
Must fetch HTML pages and parse `js-initialData`:
```
//script[@id='js-initialData']/text()
→ json["initialState"]["entities"]["answers"][answer_id]["content"]
→ json["initialState"]["entities"]["answers"][answer_id]["voteupCount"]
```

## What Didn't Work

| Attempt | Result |
|---------|--------|
| curl with bot UA | ZSE challenge page (403) |
| Google cache | Google challenge page |
| browser_navigate (Browserbase) | ZSE challenge |
| Playwright standard mode (new browser) | Got guest cookies only — API call still 403 |
| v4 API `order_by=vote` | 403 — disabled by Zhihu |
| Direct `browser-cookie3` on macOS Chrome DB | Encrypted values not readable without OS keychain |

## Scripts Created (in MediaCrawler project)

- `get_zhihu_answers.py` — first attempt, Playwright standard mode
- `get_zhihu_answers_v2.py` — Chrome SQLite cookie extraction (failed, encrypted)
- `get_zhihu_answers_v3.py` — browser-cookie3 approach
- `get_zhihu_answers_v4.py` — CDP + sign + API (no content in response)
- `get_zhihu_answers_v5.py` — WORKING: CDP + sign + HTML page parsing
- `debug_zhihu_api.py` — endpoint format investigation
- `debug_answer_detail.py` — single answer detail API investigation

Final working script: `/Users/xsm/Documents/workspace/MediaCrawler/get_zhihu_answers_v5.py`
Output: `/Users/xsm/Documents/workspace/MediaCrawler/data/zhihu_q8881520651_full_answers.json`
