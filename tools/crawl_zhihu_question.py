#!/usr/bin/env python3
"""
知乎问题高赞回答爬取脚本 — 两步法

Phase 1: 请求问题页 HTML → 解析 js-initialData → 提取5条预加载回答（含赞数，零成本）
Phase 2: API v4 获取 answer_id 列表 → 逐个签名请求回答 HTML → 提取 voteupCount + content
         去重跳过 Phase 1 已获取的 ID，增量保存 checkpoint，支持断点续传

用法:
  cd /path/to/MediaCrawler && uv run python /path/to/this/script.py --url "https://www.zhihu.com/question/8881520651"
  cd /path/to/MediaCrawler && uv run python /path/to/this/script.py --qid 8881520651 --count 15
  cd /path/to/MediaCrawler && uv run python /path/to/this/script.py --qid 8881520651 --count 30 --resume
"""

import argparse
import asyncio
import json
import os
import re
import sys
import time

MEDIACRAWLER_ROOT = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "..", "MediaCrawler")
MEDIACRAWLER_ROOT = os.path.abspath(MEDIACRAWLER_ROOT)

OUTPUT_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "data", "zhihu")
OUTPUT_DIR = os.path.abspath(OUTPUT_DIR)


def extract_question_id(raw: str) -> str:
    if re.match(r"^\d+$", raw):
        return raw
    m = re.search(r"zhihu\.com/question/(\d+)", raw)
    if m:
        return m.group(1)
    raise ValueError(f"无法从 '{raw}' 中提取问题ID，请提供 zhihu.com/question/xxx 或纯数字ID")


def checkpoint_path(question_id: str) -> str:
    return os.path.join(OUTPUT_DIR, f"zhihu_q{question_id}_checkpoint.json")


def load_checkpoint(question_id: str) -> dict | None:
    cpath = checkpoint_path(question_id)
    if not os.path.exists(cpath):
        return None
    with open(cpath, "r", encoding="utf-8") as f:
        return json.load(f)


def save_checkpoint(checkpoint: dict):
    os.makedirs(OUTPUT_DIR, exist_ok=True)
    cpath = checkpoint_path(checkpoint["question_id"])
    checkpoint["_saved_at"] = time.strftime("%Y-%m-%d %H:%M:%S")
    with open(cpath, "w", encoding="utf-8") as f:
        json.dump(checkpoint, f, ensure_ascii=False, indent=2)


async def get_cookies_via_cdp():
    """通过 CDP 连接已运行的 Chrome 获取知乎 cookie"""
    from playwright.async_api import async_playwright

    async with async_playwright() as p:
        print("[CDP] Connecting to Chrome on port 9222...", file=sys.stderr)
        try:
            browser = await p.chromium.connect_over_cdp("http://localhost:9222")
        except Exception as e:
            raise RuntimeError(
                "无法通过 CDP 连接到 Chrome。请先启动 Chrome 调试模式：\n"
                '  /Applications/Google\\ Chrome.app/Contents/MacOS/Google\\ Chrome \\\n'
                '    --remote-debugging-port=9222 \\\n'
                '    --user-data-dir="$HOME/.chrome-debug-profile"\n'
                "启动后在 Chrome 中登录知乎，然后重新运行本脚本。"
            ) from e

        ctx = browser.contexts[0]
        page = await ctx.new_page()
        await page.goto("https://www.zhihu.com", wait_until="domcontentloaded")
        await asyncio.sleep(2)

        cookies = await ctx.cookies()
        zhihu_cookies = {c["name"]: c["value"] for c in cookies if "zhihu" in c.get("domain", "")}
        z_c0 = zhihu_cookies.get("z_c0", "")

        if not z_c0:
            print("[CDP] 未检测到登录状态，请在 Chrome 中登录知乎...", file=sys.stderr)
            await page.goto("https://www.zhihu.com/signin", wait_until="domcontentloaded")
            await asyncio.sleep(120)
            cookies = await ctx.cookies()
            zhihu_cookies = {c["name"]: c["value"] for c in cookies if "zhihu" in c.get("domain", "")}
            z_c0 = zhihu_cookies.get("z_c0", "")

        await page.close()

        if not z_c0:
            raise RuntimeError("登录超时或未检测到登录状态 (缺少 z_c0)")

        cookie_str = "; ".join([f"{k}={v}" for k, v in zhihu_cookies.items()])
        print(f"[CDP] Logged in, z_c0={z_c0[:20]}...", file=sys.stderr)
        return cookie_str


def parse_answer_from_entities(entities: dict, aid: str) -> dict:
    """从 initialData entities 中提取单个回答数据"""
    answers_data = entities.get("answers", {})
    content_data = entities.get("content", {})

    if aid in answers_data:
        ad = answers_data[aid]
        return {
            "content": ad.get("content", ""),
            "voteup_count": ad.get("voteupCount", 0),
            "comment_count": ad.get("commentCount", 0),
            "author_name": (ad.get("author", {}) or {}).get("name", "匿名用户"),
            "author_headline": (ad.get("author", {}) or {}).get("headline", ""),
        }
    elif aid in content_data:
        cd = content_data[aid]
        return {
            "content": cd.get("content", ""),
            "voteup_count": cd.get("voteupCount", 0),
            "comment_count": cd.get("commentCount", 0),
            "author_name": "匿名用户",
            "author_headline": "",
        }
    return None


async def extract_initialdata_answers(question_id: str, cookie_str: str, sign_func, httpx_module):
    """Phase 1: 从问题页 HTML 提取 initialData 预加载的回答（零 HTML 请求成本）"""
    question_url = f"https://www.zhihu.com/question/{question_id}"
    headers = {
        "accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
        "accept-language": "zh-CN,zh;q=0.9,en;q=0.8",
        "cookie": cookie_str,
        "referer": "https://www.zhihu.com",
        "user-agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36",
        "x-zse-93": "101_3_3.0",
    }

    async with httpx_module.AsyncClient() as client:
        signed = sign_func(question_url, cookie_str)
        resp = await client.get(question_url, headers={**headers, **signed}, timeout=15)
        if resp.status_code != 200:
            print(f"[Phase1] 问题页请求失败 HTTP {resp.status_code}", file=sys.stderr)
            return []

        match = re.search(r'<script id="js-initialData" type="text/json">(.*?)</script>', resp.text)
        if not match:
            print(f"[Phase1] 未找到 js-initialData", file=sys.stderr)
            return []

        jd = json.loads(match.group(1))
        entities = jd.get("initialState", {}).get("entities", {})
        answers_data = entities.get("answers", {})

        results = []
        for aid, ad in answers_data.items():
            parsed = parse_answer_from_entities(entities, str(aid))
            if parsed:
                plain_text = re.sub(r"<[^>]+>", "", parsed["content"])
                results.append({
                    "answer_id": str(aid),
                    "author_name": parsed["author_name"],
                    "author_headline": parsed["author_headline"],
                    "voteup_count": parsed["voteup_count"],
                    "comment_count": parsed["comment_count"],
                    "content": plain_text,
                    "url": f"https://www.zhihu.com/question/{question_id}/answer/{aid}",
                    "source": "initialData",
                })

        return results


async def crawl_question(question_id: str, count: int = 10, min_votes: int = 0,
                         resume: bool = False, delay: float = 1.0):
    """两步法爬取：Phase1 initialData + Phase2 API渐进扫描"""

    # ---------- 1. 获取 cookie ----------
    cookie_str = await get_cookies_via_cdp()

    original_cwd = os.getcwd()
    os.chdir(MEDIACRAWLER_ROOT)
    sys.path.insert(0, MEDIACRAWLER_ROOT)
    try:
        from media_platform.zhihu.help import sign
        import httpx
        from parsel import Selector

        # 加载 checkpoint
        checkpoint = load_checkpoint(question_id) if resume else None
        fetched_map: dict[str, dict] = {}

        if checkpoint:
            for a in checkpoint.get("answers", []):
                fetched_map[a["answer_id"]] = a
            print(f"[CHECKPOINT] 已恢复 {len(fetched_map)} 条已获取回答", file=sys.stderr)

        # ---------- Phase 1: initialData ----------
        if not checkpoint:  # 只在首次运行时执行
            print(f"[Phase1] 提取问题页预加载回答...", file=sys.stderr)
            init_answers = await extract_initialdata_answers(
                question_id, cookie_str, sign, httpx
            )
            for a in init_answers:
                fetched_map[a["answer_id"]] = a
            high_init = sum(1 for a in init_answers if a["voteup_count"] >= 1000)
            print(f"[Phase1] 获得 {len(init_answers)} 条预加载回答, 其中千赞以上 {high_init} 个", file=sys.stderr)
            # 首次也存 checkpoint
            save_checkpoint({
                "question_id": question_id,
                "question_title": "",
                "total_answers": 0,
                "fetched_count": len(fetched_map),
                "high_vote_count": high_init,
                "answers": list(fetched_map.values()),
            })

        # ---------- Phase 2: API 列表 + 渐进 HTML 扫描 ----------
        per_page = 20
        target_fetch = min(count * 5, 500)

        base_headers = {
            "accept": "*/*",
            "accept-language": "zh-CN,zh;q=0.9,en;q=0.8",
            "cookie": cookie_str,
            "referer": f"https://www.zhihu.com/question/{question_id}",
            "user-agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/148.0.0.0 Safari/537.36",
            "x-zse-93": "101_3_3.0",
        }

        answer_list = []
        question_title = checkpoint.get("question_title", "") if checkpoint else ""
        total = checkpoint.get("total_answers", 0) if checkpoint else 0
        pages_needed = (target_fetch + per_page - 1) // per_page

        async with httpx.AsyncClient() as client:
            for page in range(pages_needed):
                offset = page * per_page
                api_url = f"https://www.zhihu.com/api/v4/questions/{question_id}/answers?limit={per_page}&offset={offset}&order_by=default"
                signed_req = sign(api_url, cookie_str)
                resp = await client.get(api_url, headers={**base_headers, **signed_req}, timeout=15)

                if resp.status_code != 200:
                    if page == 0:
                        raise RuntimeError(f"API 返回 {resp.status_code}: {resp.text[:300]}")
                    break

                api_data = resp.json()
                page_answers = api_data.get("data", [])
                if not page_answers:
                    break

                if page == 0 and not checkpoint:
                    total = api_data.get("paging", {}).get("totals", len(page_answers))
                    q = page_answers[0].get("question", {})
                    question_title = q.get("title", "")

                answer_list.extend(page_answers)

                if len(page_answers) < per_page:
                    break

                await asyncio.sleep(0.5)

        total_ids = len(answer_list)
        new_ids = [a for a in answer_list if str(a.get("id", "")) not in fetched_map]
        skipped = total_ids - len(new_ids)

        print(f"[Phase2] 问题: {question_title}, 总回答: {total}, API获取ID: {total_ids}, "
              f"已去重跳过: {skipped}, 待获取: {len(new_ids)}", file=sys.stderr)

        # ---------- 逐个获取新增回答的 HTML ----------
        if new_ids:
            print(f"[Phase2] 开始获取 {len(new_ids)} 个新增回答页面...", file=sys.stderr)

        high_vote_seen = sum(1 for a in fetched_map.values() if a["voteup_count"] >= 1000)
        start_count = len(fetched_map)

        async with httpx.AsyncClient() as client:
            for i, ans in enumerate(answer_list):
                aid = str(ans.get("id", ""))
                if not aid or aid in fetched_map:
                    continue

                author = ans.get("author", {})
                author_name = author.get("name", "匿名用户") if isinstance(author, dict) else "匿名用户"
                author_headline = author.get("headline", "") if isinstance(author, dict) else ""
                answer_url = f"https://www.zhihu.com/question/{question_id}/answer/{aid}"

                signed_h = sign(answer_url, cookie_str)
                html_headers = {
                    "accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
                    "accept-language": "zh-CN,zh;q=0.9,en;q=0.8",
                    "cookie": cookie_str,
                    "referer": f"https://www.zhihu.com/question/{question_id}",
                    "user-agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/148.0.0.0 Safari/537.36",
                    "x-zse-93": "101_3_3.0",
                    **signed_h,
                }

                try:
                    resp2 = await client.get(answer_url, headers=html_headers, timeout=15, follow_redirects=True)

                    voteup = 0
                    comment_count = 0
                    content_text = ""

                    if resp2.status_code == 200:
                        sel = Selector(text=resp2.text)
                        init_text = sel.xpath("//script[@id='js-initialData']/text()").get()

                        if init_text:
                            jd = json.loads(init_text)
                            entities = jd.get("initialState", {}).get("entities", {})
                            parsed = parse_answer_from_entities(entities, aid)

                            if parsed:
                                content_text = parsed["content"]
                                voteup = parsed["voteup_count"]
                                comment_count = parsed["comment_count"]
                                # 如果 initialData 中已有更全的 author 名则使用
                                if parsed["author_name"] != "匿名用户":
                                    author_name = parsed["author_name"]
                                if parsed["author_headline"]:
                                    author_headline = parsed["author_headline"]

                            plain_text = re.sub(r"<[^>]+>", "", content_text) if content_text else "[内容未提取到]"
                        else:
                            plain_text = "[initialData未找到]"
                    else:
                        plain_text = f"[获取失败: HTTP {resp2.status_code}]"

                    entry = {
                        "answer_id": aid,
                        "author_name": author_name,
                        "author_headline": author_headline,
                        "voteup_count": voteup,
                        "comment_count": comment_count,
                        "content": plain_text,
                        "url": answer_url,
                        "source": "api",
                    }
                    fetched_map[aid] = entry

                    if voteup >= 1000:
                        high_vote_seen += 1

                    progress = len(fetched_map)
                    print(f"[Phase2] ({progress - start_count}/{len(new_ids)}) {author_name[:15]} | "
                          f"赞 {voteup:,} | 千赞已有: {high_vote_seen}", file=sys.stderr)

                except Exception as e:
                    print(f"[Phase2] ({len(fetched_map) - start_count + 1}/{len(new_ids)}) "
                          f"{author_name[:15]} | Failed: {e}", file=sys.stderr)
                    entry = {
                        "answer_id": aid,
                        "author_name": author_name,
                        "author_headline": author_headline,
                        "voteup_count": 0,
                        "comment_count": 0,
                        "content": f"[获取异常: {e}]",
                        "url": answer_url,
                        "source": "api",
                    }
                    fetched_map[aid] = entry

                # 增量保存
                save_checkpoint({
                    "question_id": question_id,
                    "question_title": question_title,
                    "total_answers": total,
                    "fetched_count": len(fetched_map),
                    "high_vote_count": high_vote_seen,
                    "answers": list(fetched_map.values()),
                })

                # 频率控制
                new_done = len(fetched_map) - start_count
                if new_done < len(new_ids):
                    await asyncio.sleep(delay)

    finally:
        os.chdir(original_cwd)

    # ---------- 排序 + 输出 ----------
    all_answers = list(fetched_map.values())
    all_answers.sort(key=lambda x: x["voteup_count"], reverse=True)

    if min_votes > 0:
        qualified = [a for a in all_answers if a["voteup_count"] >= min_votes]
        results = qualified[:count] if len(qualified) >= count else all_answers[:count]
    else:
        results = all_answers[:count]

    for i, item in enumerate(results):
        item["rank"] = i + 1

    return {
        "question_id": question_id,
        "question_title": question_title,
        "total_answers": total,
        "total_fetched": len(all_answers),
        "fetched_count": len(results),
        "high_vote_found": sum(1 for a in all_answers if a["voteup_count"] >= 1000),
        "answers": results,
    }


def main():
    parser = argparse.ArgumentParser(description="爬取知乎问题高赞回答 (两步法)")
    group = parser.add_mutually_exclusive_group(required=True)
    group.add_argument("--url", help="知乎问题链接")
    group.add_argument("--qid", help="知乎问题ID")
    parser.add_argument("--count", type=int, default=10, help="期望输出的回答数量 (默认10)")
    parser.add_argument("--min-votes", type=int, default=0, help="赞数下限过滤 (如 --min-votes 1000)")
    parser.add_argument("--resume", action="store_true", help="从断点续传，跳过已获取的 answer_id")
    parser.add_argument("--delay", type=float, default=1.0, help="HTML请求间隔秒数 (默认1.0)")
    parser.add_argument("--json", dest="json_output", action="store_true", help="仅输出JSON到stdout")
    args = parser.parse_args()

    raw = args.url or args.qid
    question_id = extract_question_id(raw)

    if not args.json_output:
        flag = " [续传]" if args.resume else ""
        print(f"[*] 问题ID: {question_id}, 期望 {args.count} 条, min_votes={args.min_votes}{flag}", file=sys.stderr)

    result = asyncio.run(crawl_question(
        question_id, count=args.count, min_votes=args.min_votes,
        resume=args.resume, delay=args.delay,
    ))

    os.makedirs(OUTPUT_DIR, exist_ok=True)
    output_path = os.path.join(OUTPUT_DIR, f"zhihu_q{question_id}_top{args.count}.json")
    with open(output_path, "w", encoding="utf-8") as f:
        json.dump(result, f, ensure_ascii=False, indent=2)

    if not args.json_output:
        print(f"\n{'='*60}", file=sys.stderr)
        print(f"[问题] {result['question_title']}", file=sys.stderr)
        print(f"[共计] {result['total_answers']} 个回答, 已扫描 {result['total_fetched']} 个, "
              f"其中千赞以上 {result['high_vote_found']} 个", file=sys.stderr)
        init_count = sum(1 for a in result["answers"] if a.get("source") == "initialData")
        if init_count:
            print(f"[来源] initialData={init_count}, api={result['fetched_count'] - init_count}", file=sys.stderr)
        print(f"[输出] 前 {result['fetched_count']} 条 → {output_path}", file=sys.stderr)
        print(f"{'='*60}", file=sys.stderr)
        for a in result["answers"]:
            source_tag = "[页]" if a.get("source") == "initialData" else "[A]"
            print(f"\n{'─'*60}", file=sys.stderr)
            print(f"  #{a['rank']} {source_tag} | 赞同 {a['voteup_count']:,} | 评论 {a['comment_count']} | {a['author_name']}", file=sys.stderr)
            if a['author_headline']:
                print(f"  {a['author_headline'][:80]}", file=sys.stderr)
            print(f"  ────", file=sys.stderr)
            content_display = a['content'][:400] if a['content'] else '(无内容)'
            print(f"  {content_display}", file=sys.stderr)

    print(json.dumps(result, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
