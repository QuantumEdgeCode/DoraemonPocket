#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
ng.ru (Независимая газета) 爬虫 — 双引擎架构
================================================
引擎 1 (列表/RSS, 主):  https://www.ng.ru/rss
    - NG 的 RSS 仅含摘要(description ~289字) + 完整元数据(title/link/pubDate+0300)
    - 时间用 RSS pubDate 解析为 ISO(+03:00 莫斯科时间)，比 HTML 的 ".date"(无时分)更精确
引擎 2 (详情页, 降级兜底): 抓每篇文章 HTML 取正文全文
    - NG 是 SSR + 未被反爬拦截(200 直出, 无 JS challenge / 无 IP 403)
    - 正文容器有两种模板:
        * 栏目页 /economics/2026-.../xxx.html  -> <article> 标签
        * /news/<id>.html                      -> .content.newsone
    - 图片在正文内 <img>(含 <p class="image_detail"> 图注块)；og:image 是站标，忽略
    - 过滤 <p class="image_detail"> (图注不是正文) 但保留其图片

合规: 内容版权归 Независимая газета，仅限个人学习/研究，禁止商用转发。
请求间隔 3s，尊重站点。

用法:
    python ng_crawler.py                                  # 全量(首页RSS, 100条)
    python ng_crawler.py --root https://www.ng.ru/rss      # 等价于默认
    python ng_crawler.py --limit 5                         # 调试前 N 条
    python ng_crawler.py --no-detail                       # 仅列表级(用 RSS 摘要当内容)
    python ng_crawler.py --root https://www.ng.ru/politics # 指定频道(需该频道有 /rss 或列表页)
"""

import argparse
import json
import os
import re
import sys
import time
from email.utils import parsedate_to_datetime
from urllib.parse import urljoin, urlparse

import requests
from bs4 import BeautifulSoup

# ---------- 常量 ----------
SITE = "https://www.ng.ru"
DEFAULT_FEED = "https://www.ng.ru/rss"
SOURCE_NAME = "Независимая газета"
UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36")
HEADERS = {
    "User-Agent": UA,
    "Accept-Language": "ru-RU,ru;q=0.9,en;q=0.8",
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
}
REQUEST_TIMEOUT = 20
SLEEP_SEC = 3  # 礼貌间隔

# 文章 URL 正则: /news/<id>.html 或 /<section>/<YYYY-MM-DD>/<slug>.html
ARTICLE_RE = re.compile(r"^https?://(?:www\.)?ng\.ru/(?:[a-z]+/)?(?:\d{4}-\d{2}-\d{2}/)?[\w\-]+\.html$")


# ---------- 工具函数 ----------
def log(msg):
    print(msg, flush=True)


def fetch(url, timeout=REQUEST_TIMEOUT):
    return requests.get(url, headers=HEADERS, timeout=timeout)


def is_article_url(u):
    return bool(ARTICLE_RE.match(u)) and "ng.ru" in u


# ---------- 引擎 1: RSS 列表 ----------
def parse_list_rss(feed_url):
    """解析 RSS 列表, 返回 [{title,url,summary,publish_time}]"""
    try:
        r = fetch(feed_url)
    except Exception as e:
        log(f"[列表] RSS 请求失败 {feed_url}: {e}")
        return []
    if r.status_code != 200 or "<item" not in r.text:
        log(f"[列表] RSS 不可用 (status={r.status_code}); 回退到首页列表抓取")
        return parse_list_html(feed_url)
    soup = BeautifulSoup(r.text, "xml")
    items = []
    for it in soup.find_all("item"):
        title = it.title.get_text(strip=True) if it.title else ""
        link = it.link.get_text(strip=True) if it.link else ""
        desc = it.description.get_text(strip=True) if it.description else ""
        pub = ""
        if it.pubDate:
            try:
                dt = parsedate_to_datetime(it.pubDate.get_text())
                pub = dt.isoformat()  # 已含 +03:00
            except Exception:
                pub = it.pubDate.get_text(strip=True)
        if link and is_article_url(link):
            items.append({
                "title": title,
                "url": link,
                "summary": desc,
                "publish_time": pub,
            })
    log(f"[列表] RSS 命中 {len(items)} 条")
    return items


def parse_list_html(page_url):
    """RSS 不可用时的兜底: 从栏目/首页 HTML 抽取文章链接"""
    try:
        r = fetch(page_url)
    except Exception as e:
        log(f"[列表] 页面请求失败 {page_url}: {e}")
        return []
    soup = BeautifulSoup(r.text, "lxml")
    seen, items = set(), []
    for a in soup.find_all("a", href=True):
        full = urljoin(page_url, a["href"].strip())
        if is_article_url(full) and full not in seen:
            seen.add(full)
            items.append({
                "title": a.get_text(strip=True),
                "url": full,
                "summary": "",
                "publish_time": "",
            })
    log(f"[列表] HTML 命中 {len(items)} 条")
    return items


# ---------- 引擎 2: 详情页 ----------
def find_body(soup):
    """返回正文容器(兼容两种模板), 否则 None"""
    art = soup.find("article")
    if art and len(art.get_text(strip=True)) > 200:
        return art
    for sel in [".content.newsone", ".newsone", ".news_detail_content", ".content"]:
        el = soup.select_one(sel)
        if el and len(el.get_text(strip=True)) > 200:
            return el
    return None


def parse_detail(url):
    """抓取详情页, 返回 {detail_title, content, images, publish_time}"""
    try:
        r = fetch(url)
    except Exception as e:
        log(f"  [详情] 请求失败 {url}: {e}")
        return {"detail_title": "", "content": "", "images": [], "publish_time": ""}

    soup = BeautifulSoup(r.text, "lxml")
    h1 = soup.find("h1")
    title = h1.get_text(strip=True) if h1 else ""

    body = find_body(soup)
    paras, imgs = [], []

    if body:
        # 图片: 正文内所有 <img>(含 <p class=image_detail> 图块)
        for i in body.find_all("img"):
            src = i.get("src") or i.get("data-src")
            if not src or src.startswith("data:"):
                continue
            abs_src = urljoin(url, src)
            if abs_src not in imgs:
                imgs.append(abs_src)
        # 正文: 跳过图注块 <p class=image_detail>, 其余 <p> 取文本
        for p in body.find_all("p"):
            if "image_detail" in " ".join(p.get("class") or []):
                continue
            t = p.get_text(" ", strip=True)
            if t:
                paras.append(t)

    content = "\n\n".join(paras)

    # 清理: 去掉正文开头的冗余电头 "HH:MM DD.MM.YYYY"(时间已在 publish_time)
    content = re.sub(r"^\s*\d{1,2}:\d{2}\s+\d{2}\.\d{2}\.\d{4}\b\s*", "", content)

    # 兜底: 无正文时用 meta description
    if not content:
        m = soup.find("meta", attrs={"name": "description"}) or soup.find(
            "meta", property="og:description")
        if m and m.get("content"):
            content = m.get("content").strip()

    # 时间兜底: 若列表未给, 从 HTML .date 解析 ("Вторник 28.07.2026")
    pub = ""
    d = soup.select_one(".date")
    if d:
        m = re.search(r"(\d{2})\.(\d{2})\.(\d{4})", d.get_text())
        if m:
            pub = f"{m.group(3)}-{m.group(2)}-{m.group(1)}"

    return {
        "detail_title": title,
        "content": content,
        "images": imgs,
        "publish_time": pub,
    }


# ---------- 主流程 ----------
def atomic_save(data, path="data/新闻/ng_collection.json"):
    """JSON 原子保护性写入：先写 .tmp → flush+fsync → os.replace 改名，杜绝半截 JSON。

    每抓完一篇即调用，进程被中断（Ctrl+C / 断电）也只丢当前这一篇，已落盘数据不丢。
    """
    d = os.path.dirname(path)
    if d:
        os.makedirs(d, exist_ok=True)
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)
        f.flush()
        os.fsync(f.fileno())
    os.replace(tmp, path)


def main():
    ap = argparse.ArgumentParser(description="ng.ru crawler")
    ap.add_argument("--root", default=DEFAULT_FEED,
                    help="RSS/列表页 URL (默认 https://www.ng.ru/rss)")
    ap.add_argument("--limit", type=int, default=0, help="仅抓取前 N 条(调试)")
    ap.add_argument("--no-detail", action="store_true",
                    help="不抓详情页, 直接用 RSS 摘要当内容")
    ap.add_argument("--out", default="data/新闻/ng_collection.json")
    args = ap.parse_args()

    t0 = time.time()
    root_is_rss = args.root.rstrip("/").endswith("/rss") or args.root.endswith(".xml")
    list_items = (parse_list_rss(args.root) if root_is_rss
                  else parse_list_html(args.root))

    if not list_items:
        log("未获取到任何文章链接, 退出。")
        sys.exit(1)

    if args.limit:
        list_items = list_items[:args.limit]
    log(f"待处理 {len(list_items)} 篇 (no_detail={args.no_detail})")

    collected = []
    empty_detail = 0
    try:
        for idx, it in enumerate(list_items, 1):
            url = it["url"]
            if args.no_detail:
                detail = {
                    "detail_title": it["title"],
                    "content": it["summary"],
                    "images": [],
                    "publish_time": it["publish_time"],
                }
                content_src = "rss_summary"
            else:
                detail = parse_detail(url)
                content_src = "fulltext" if detail["content"] else "empty"
                if not detail["content"]:
                    empty_detail += 1
                if idx < len(list_items):
                    time.sleep(SLEEP_SEC)

            # 合并: 列表级字段 + 详情级字段
            # 时间优先级: RSS(列表) 的精确时间 > HTML .date 兜底(仅日期)
            record = {
                "title": it["title"],
                "url": url,
                "publish_time": it["publish_time"] or detail["publish_time"],
                "source": SOURCE_NAME,
                "section": urlparse(url).path.split("/")[1] if len(urlparse(url).path.split("/")) > 1 else "",
                "summary": it["summary"],
                "detail_title": detail["detail_title"] or it["title"],
                "content": detail["content"],
                "content_source": content_src,
                "images": detail["images"],
            }
            collected.append(record)
            flag = f"[{content_src}]" if not args.no_detail else "[rss]"
            log(f"  ({idx}/{len(list_items)}) {flag} {len(detail['content'])}字 "
                f"{len(detail['images'])}图 | {detail['detail_title'][:46] or it['title'][:46]}")
            # 每篇实时原子落盘（与收尾同结构包裹）
            atomic_save({
                "source": SOURCE_NAME,
                "site": SITE,
                "feed": args.root,
                "crawled_at": time.strftime("%Y-%m-%dT%H:%M:%S+08:00"),
                "count": len(collected),
                "detail_html_accessible": True,
                "items": collected,
            }, args.out)
    except (KeyboardInterrupt, Exception) as e:
        log(f"\n[warn] 采集中断（{type(e).__name__}）：已实时保存至当前进度 -> {args.out}")
        raise

    out = {
        "source": SOURCE_NAME,
        "site": SITE,
        "feed": args.root,
        "crawled_at": time.strftime("%Y-%m-%dT%H:%M:%S+08:00"),
        "count": len(collected),
        "detail_html_accessible": True,  # SSR 直出, 无反爬
        "items": collected,
    }
    atomic_save(out, args.out)  # 收尾原子保存

    dt = time.time() - t0
    log("=" * 60)
    log(f"完成: {len(collected)} 篇 -> {args.out}")
    log(f"耗时 {dt:.1f}s | 空正文 {empty_detail} 篇")


if __name__ == "__main__":
    main()
