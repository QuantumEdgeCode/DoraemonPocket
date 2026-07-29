#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
scmp_crawler.py — 爬取 https://www.scmp.com/ (South China Morning Post, 中国香港)
================================================================================
方法（双引擎：静态优先 / Playwright 降级，沿用统一约定）
  - 文章 URL 形如  https://www.scmp.com/<栏目路径>/article/<ID>/<slug>
                   例：/news/world/americas/article/3362181/keiko-fujimori-...
                   ID 为 /article/ 后纯数字，唯一
  - 发现来源（去重合并）：
        sitemap/sitemap.xml（索引，67 个子 sitemap）→ 主文章池 sitemap/articles.xml
          （约 20000 条，最新在前）；再补 sitemap/google-news.xml（近期精选）
        ID 提取：/article/(数字)/
  - 正文解析：SCMP 为 Next.js SSR，但关键数据在 <script type="application/ld+json">
        的 NewsArticle/ReportageNewsArticle 块中，字段齐全且为权威来源：
          · headline         → 标题
          · articleBody      → 全文（纯文本，含换行；付费文可能截断）
          · datePublished    → 时间（已带 +08:00 香港时区）
          · author           → 署名（可能多名，合并为逗号分隔）
          · image.url        → 头图（cdn.i-scmp.com 真实图）
          · isAccessibleForFree → 是否免费（False 即订阅/付费内容，标记 premium）
        HTML 容器（兜底）：div.article__body / div.paywalled（一般无需）
  - 时间：datePublished（+08:00）→ dateModified → sitemap lastmod
  - 图片：JSON-LD image.url（主图）+ og:image，去重；SCMP 正文内联图在 JSON-LD
         articleBody 中不含，故仅取头图（如需内联图可后续扩展 HTML figure 解析）
  - 栏目：URL 路径在 /article/ 之前的部分（如 news/world/americas）

合规：South China Morning Post (SCMP) 内容版权归原社所有；本脚本仅用于个人学习/
研究，禁止商用转发。robots.txt 仅作提示（含 Crawl-delay: 10），不阻断抓取。

用法：
  python scmp_crawler.py                  # 全量（默认 limit=800）
  python scmp_crawler.py --limit 50       # 控量
  python scmp_crawler.py --no-detail      # 仅采集链接，不抓正文
  python scmp_crawler.py --delay 3        # 请求间隔（秒）
  python scmp_crawler.py --playwright     # 强制 Playwright 渲染（一般无需）
"""

import argparse
import html as _html
import json
import os
import re
import time
from datetime import datetime, timezone, timedelta
from urllib.parse import urlparse

import requests
from bs4 import BeautifulSoup

try:
    from playwright.sync_api import sync_playwright
    HAVE_PW = True
except Exception:
    HAVE_PW = False

BASE = "https://www.scmp.com"
SITEMAP_INDEX = BASE + "/sitemap/sitemap.xml"
OUTPUT = "data/新闻/scmp_collection.json"
HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                  "(KHTML, like Gecko) Chrome/124.0 Safari/537.36",
    "Accept-Language": "en-US,en;q=0.9",
}

ARTICLE_RE = re.compile(r"https?://(?:www\.)?scmp\.com/(?:[a-z0-9_\-/]+/)?article/(\d+)/", re.I)
TARGET_TYPES = {"NewsArticle", "ReportageNewsArticle", "Article"}


# ---------------------------------------------------------------------------
# 网络获取
# ---------------------------------------------------------------------------
def fetch(url):
    try:
        r = requests.get(url, headers=HEADERS, timeout=25)
        if r.status_code == 200:
            return r.text, True
        return None, False
    except Exception:
        return None, False


def fetch_playwright(url):
    if not HAVE_PW:
        return None, False
    try:
        with sync_playwright() as p:
            b = p.chromium.launch(headless=True)
            pg = b.new_page()
            pg.goto(url, timeout=35000, wait_until="networkidle")
            html = pg.content()
            b.close()
            return html, True
    except Exception:
        return None, False


# ---------------------------------------------------------------------------
# 链接采集
# ---------------------------------------------------------------------------
def collect_links():
    links = {}  # url -> aid

    # sitemap 索引 → 主文章池 articles.xml + google-news.xml
    idx, ok = fetch(SITEMAP_INDEX)
    subs = []
    if ok:
        subs = re.findall(r"<loc>(.*?)</loc>", idx)
    else:
        print("[list] 无法读取 sitemap 索引")
    for s in subs:
        if s.endswith("/articles.xml") or s.endswith("/google-news.xml"):
            sub, sok = fetch(s)
            if not sok:
                continue
            for u in re.findall(r"<loc>(.*?)</loc>", sub):
                m = ARTICLE_RE.match(u)
                if m:
                    u2 = u.split("?")[0].rstrip("/")
                    links.setdefault(u2, m.group(1))

    # 兜底：索引未覆盖时直接用已知主池
    if not links:
        for s in [BASE + "/sitemap/articles.xml", BASE + "/sitemap/google-news.xml"]:
            sub, sok = fetch(s)
            if not sok:
                continue
            for u in re.findall(r"<loc>(.*?)</loc>", sub):
                m = ARTICLE_RE.match(u)
                if m:
                    u2 = u.split("?")[0].rstrip("/")
                    links.setdefault(u2, m.group(1))

    return links


# ---------------------------------------------------------------------------
# JSON-LD 解析
# ---------------------------------------------------------------------------
def extract_article_ldjson(html):
    blocks = []
    for m in re.finditer(r'<script[^>]+type="application/ld\+json"[^>]*>(.*?)</script>',
                         html, re.S):
        raw = m.group(1).strip()
        if raw.startswith("<!--"):
            raw = raw[4:]
        if raw.endswith("-->"):
            raw = raw[:-3]
        try:
            d = json.loads(raw)
        except Exception:
            try:
                d = json.loads(raw.replace("&quot;", '"').replace("&amp;", "&"))
            except Exception:
                continue
        if not isinstance(d, dict):
            continue
        types = d.get("@type")
        if isinstance(types, str):
            types = [types]
        if isinstance(types, list) and TARGET_TYPES.intersection(types):
            blocks.append(d)
    return blocks


def parse_time(s):
    if not s:
        return None
    s = s.strip()
    try:
        d = datetime.fromisoformat(s.replace("Z", "+00:00"))
        if d.tzinfo is None:
            d = d.replace(tzinfo=timezone(timedelta(hours=8)))  # 香港 +08:00 兜底
        return d.isoformat()
    except Exception:
        pass
    try:
        from email.utils import parsedate_to_datetime
        dt = parsedate_to_datetime(s)
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone(timedelta(hours=8)))
        return dt.isoformat()
    except Exception:
        return s if s else None


def normalize_authors(author):
    names = []
    if isinstance(author, dict):
        author = [author]
    if isinstance(author, list):
        for a in author:
            if isinstance(a, dict):
                n = a.get("name")
                if n:
                    names.append(n)
            elif isinstance(a, str) and a:
                names.append(a)
    elif isinstance(author, str) and author:
        names.append(author)
    return ", ".join(names)


def section_of(url):
    path = urlparse(url).path.strip("/")
    parts = [p for p in path.split("/") if p]
    if "article" in parts:
        idx = parts.index("article")
        return "/".join(parts[:idx])
    return "/".join(parts[:1]) if parts else None


# ---------------------------------------------------------------------------
# 文章解析
# ---------------------------------------------------------------------------
def parse_article(url, aid, use_playwright=False):
    html, ok = fetch(url)
    if not ok and use_playwright:
        html, ok = fetch_playwright(url)
    if not ok:
        return None

    blocks = extract_article_ldjson(html)
    if not blocks:
        return None
    d = blocks[0]

    title = d.get("headline") or d.get("alternativeHeadline")
    title = _html.unescape(title) if title else None
    if not title:
        og = re.search(r'<meta[^>]+property="og:title"[^>]+content="(.*?)"', html)
        title = _html.unescape(og.group(1)) if og else None

    body = d.get("articleBody")
    if not body:
        return None  # 图集/视频类无正文
    body = _html.unescape(body)

    pub = parse_time(d.get("datePublished") or d.get("dateModified"))
    if not pub:
        m = re.search(r"<lastmod>(.*?)</lastmod>", html)
        if m:
            pub = parse_time(m.group(1))

    author = normalize_authors(d.get("author")) or "SCMP"
    is_free = d.get("isAccessibleForFree")
    premium = (is_free is False)  # 明确标记非免费（订阅内容）

    # 摘要
    summary = d.get("description")
    summary = _html.unescape(summary) if summary else None
    if not summary:
        og = re.search(r'<meta[^>]+property="og:description"[^>]+content="(.*?)"', html)
        summary = _html.unescape(og.group(1)) if og else None

    # 图片：优先 JSON-LD image.url（真实头图）；og:image 仅作兜底
    images = []
    img = d.get("image")
    if isinstance(img, dict) and img.get("url"):
        images.append({"url": img["url"], "caption": ""})
    elif isinstance(img, list):
        for it in img:
            if isinstance(it, dict) and it.get("url"):
                images.append({"url": it["url"], "caption": ""})
    if not images:
        og_img = re.search(r'<meta[^>]+property="og:image"[^>]+content="(.*?)"', html)
        if og_img and og_img.group(1):
            images.append({"url": og_img.group(1), "caption": ""})

    # 正文较短且非免费 → 可能截断，仍保留 premium 标记
    if len(body) < 400 and not is_free:
        premium = True

    return {
        "id": aid,
        "url": url,
        "title": title,
        "section": section_of(url),
        "author": author,
        "published_at": pub,
        "summary": summary,
        "content": body,
        "images": images,
        "premium": premium,
    }


# ---------------------------------------------------------------------------
# 主流程
# ---------------------------------------------------------------------------

def atomic_save(articles, path=OUTPUT):
    """原子写入 JSON：先写 .tmp -> flush+fsync -> os.replace 改名，防止进程中断导致半截文件。"""
    d = os.path.dirname(path)
    if d:
        os.makedirs(d, exist_ok=True)
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(articles, f, ensure_ascii=False, indent=2)
        f.flush()
        os.fsync(f.fileno())
    os.replace(tmp, path)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--limit", type=int, default=800)
    ap.add_argument("--no-detail", action="store_true")
    ap.add_argument("--delay", type=float, default=3.0)
    ap.add_argument("--playwright", action="store_true")
    args = ap.parse_args()


    print("[list] 采集文章链接（sitemap 索引 → articles.xml + google-news.xml）...")
    links = collect_links()
    print("[list] 发现唯一文章：{0} 篇".format(len(links)))

    items = list(links.items())
    if args.limit:
        items = items[: args.limit]

    articles = []
    try:
        for i, (url, aid) in enumerate(items, 1):
            print("[{0}/{1}] 解析：{2}".format(i, len(items), url))
            if args.no_detail:
                articles.append({"id": aid, "url": url, "title": None})
                atomic_save(articles, OUTPUT)  # 实时落盘
                continue
            art = parse_article(url, aid, use_playwright=args.playwright)
            if art and art.get("content"):
                articles.append(art)
                print("        ✓ 标题={0} 正文={1}字 图={2}张 栏目={3} 署名={4} 付费={5}".format(
                    (art["title"][:40] if art["title"] else None),
                    len(art["content"]),
                    len(art["images"]),
                    art["section"],
                    art["author"][:24],
                    art["premium"],
                ))
            else:
                print("        ✗ 无正文（可能为纯图集/视频/不可达）")
            atomic_save(articles, OUTPUT)  # 每篇实时落盘
            time.sleep(args.delay)

        print("\n[done] 已保存 {0} 篇 → {1}".format(len(articles), OUTPUT))
    except (KeyboardInterrupt, Exception) as exc:
        atomic_save(articles, OUTPUT)  # 中断前保存进度
        print("\n[interrupted] 已实时保存至当前进度（{0} 篇）-> {1}".format(len(articles), OUTPUT))
        raise


if __name__ == "__main__":
    main()
