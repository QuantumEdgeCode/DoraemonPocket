#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
thehindu_crawler.py — 爬取 https://www.thehindu.com/ (The Hindu, 印度)
============================================================================
方法（双引擎：静态优先 / Playwright 降级，沿用十站统一约定）
  - 文章 URL 形如  https://www.thehindu.com/<section>/<slug>/article<N>.ece
  - 发现来源（去重合并）：
      1) 各栏目 RSS：/feeder/{news,opinion,business,sport,science,entertainment,
                          world,books,education,life-and-style}.rss  （各 ~200 条）
      2) Google News sitemap：/sitemap/googlenews/all/all.xml  （~1000 条 .ece）
      3) 首页 + 主要栏目页 SSR 内嵌 .ece 链接
  - 正文容器：div.articlebodycontent（含页脚噪音，已在解析时剔除）
  - 时间：meta article:published_time（已是 +05:30 IST）→ RSS pubDate → sitemap lastmod
  - 图片：og:image（th-i.thgim.com 主图）+ 正文内 <figure> 配图（剔除 spacer/badge）
  - 作者：页脚 byline 元素（如 “The Hindu Bureau”）；缺失回退 “The Hindu”

合规：The Hindu 内容版权归 The Hindu (Kasturi & Sons Ltd.)；本脚本仅用于个人学习/研究，
禁止商用转发。robots.txt 仅作提示，不阻断抓取（用户既定偏好）。

用法：
  python thehindu_crawler.py                  # 全量（默认 limit=800）
  python thehindu_crawler.py --limit 50       # 控量
  python thehindu_crawler.py --no-detail      # 仅采集链接，不抓正文
  python thehindu_crawler.py --root <URL>     # 追加起始页
  python thehindu_crawler.py --delay 3        # 请求间隔（秒）
  python thehindu_crawler.py --playwright     # 强制 Playwright 渲染（一般无需）
"""

import argparse
import json
import os
import re
import sys
import time
from datetime import datetime, timezone, timedelta
from urllib.parse import urljoin, urlparse

import requests
from bs4 import BeautifulSoup

try:
    from playwright.sync_api import sync_playwright
    HAVE_PW = True
except Exception:
    HAVE_PW = False

BASE = "https://www.thehindu.com"
OUTPUT = "data/新闻/thehindu_collection.json"
HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                  "(KHTML, like Gecko) Chrome/124.0 Safari/537.36",
    "Accept-Language": "en-US,en;q=0.9",
}

# 各栏目 RSS（default=综合，其他为分类；部分子栏目无独立 RSS，已剔除 404 的）
DEFAULT_FEEDS = [
    BASE + "/feeder/default.rss",
    BASE + "/feeder/news.rss",
    BASE + "/feeder/opinion.rss",
    BASE + "/feeder/business.rss",
    BASE + "/feeder/sport.rss",
    BASE + "/feeder/science.rss",
    BASE + "/feeder/entertainment.rss",
    BASE + "/feeder/world.rss",
    BASE + "/feeder/books.rss",
    BASE + "/feeder/education.rss",
    BASE + "/feeder/life-and-style.rss",
]
# 更大的文章池（Google News sitemap）
GOOGLE_NEWS_SITEMAP = BASE + "/sitemap/googlenews/all/all.xml"
# 首页 + 主要栏目页（SSR 内嵌 .ece 链接，作补充）
DEFAULT_ROOTS = [
    BASE + "/",
    BASE + "/news/",
    BASE + "/opinion/",
    BASE + "/business/",
    BASE + "/sport/",
    BASE + "/science/",
    BASE + "/entertainment/",
    BASE + "/world/",
    BASE + "/books/",
    BASE + "/education/",
    BASE + "/life-and-style/",
]

ARTICLE_RE = re.compile(r"https?://(?:www\.)?thehindu\.com/[^\s\"'<>]*?article\d+\.ece", re.I)
ECE_ID_RE = re.compile(r"article(\d+)\.ece", re.I)

# 页脚/分享/标签噪音（命中即停止累计正文）
FOOTER_MARKERS = [
    "Published -", "Copy link", "Email", "Facebook", "Twitter", "Telegram",
    "LinkedIn", "WhatsApp", "Reddit", "Related Topics", "Share", "READ MORE",
]

NOISE_IMG = [
    "1x1_spacer", "google-preferred-badge", "th-online/", "/theme/images/",
    "scorecardresearch", "google-analytics", "doubleclick",
]


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
def is_article_url(u):
    u = u.split("?")[0].rstrip("/")
    return bool(ECE_ID_RE.search(u))


def collect_links(extra_roots=None):
    links = {}  # url -> article_id

    # 1) 栏目 RSS
    for feed in DEFAULT_FEEDS:
        try:
            r = requests.get(feed, headers=HEADERS, timeout=25)
            if r.status_code != 200 or "xml" not in r.headers.get("content-type", ""):
                continue
            for m in re.finditer(r"<link>(.*?)</link>", r.text, re.S):
                raw = m.group(1).strip()
                raw = re.sub(r"<!\[CDATA\[(.*?)\]\]>", r"\1", raw, flags=re.S)
                if is_article_url(raw):
                    u = raw.split("?")[0].rstrip("/")
                    links.setdefault(u, ECE_ID_RE.search(u).group(1))
        except Exception:
            pass

    # 2) Google News sitemap（~1000 条 .ece）
    try:
        r = requests.get(GOOGLE_NEWS_SITEMAP, headers=HEADERS, timeout=30)
        if r.status_code == 200:
            for loc in re.findall(r"<loc>(.*?)</loc>", r.text):
                if is_article_url(loc):
                    u = loc.split("?")[0].rstrip("/")
                    links.setdefault(u, ECE_ID_RE.search(u).group(1))
    except Exception:
        pass

    # 3) 首页 + 栏目页 SSR 内嵌 .ece
    roots = list(DEFAULT_ROOTS)
    if extra_roots:
        roots += extra_roots
    for root in roots:
        html, ok = fetch(root)
        if not ok:
            continue
        for m in ARTICLE_RE.finditer(html):
            u = m.group(0).split("?")[0].rstrip("/")
            if is_article_url(u):
                links.setdefault(u, ECE_ID_RE.search(u).group(1))

    return links


# ---------------------------------------------------------------------------
# 文章解析
# ---------------------------------------------------------------------------
def parse_time(s):
    if not s:
        return None
    s = s.strip()
    # 2026-07-29T00:15:00+05:30
    m = re.search(r"(\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2})([+-]\d{2}:?\d{2}|Z)?", s)
    if m:
        dt = m.group(1) + (m.group(2) or "")
        dt = dt.replace("Z", "+00:00")
        try:
            return datetime.fromisoformat(dt).isoformat()
        except Exception:
            pass
    # RSS: Wed, 29 Jul 2026 00:23:15 +0530
    try:
        from email.utils import parsedate_to_datetime
        dt = parsedate_to_datetime(s)
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone(timedelta(hours=5, minutes=30)))
        return dt.isoformat()
    except Exception:
        return None


def section_of(url):
    path = urlparse(url).path.strip("/")
    parts = [p for p in path.split("/") if p]
    if parts:
        return parts[0]
    return None


def parse_article(url, aid, use_playwright=False):
    html, ok = fetch(url)
    if not ok and use_playwright:
        html, ok = fetch_playwright(url)
    if not ok:
        return None

    soup = BeautifulSoup(html, "html.parser")

    # 标题
    h1 = soup.find("h1")
    title = h1.get_text(" ", strip=True) if h1 else None
    if not title:
        og = soup.find("meta", attrs={"property": "og:title"})
        title = og.get("content") if og else None

    # 时间
    pub = None
    m = soup.find("meta", attrs={"property": "article:published_time"})
    if m:
        pub = parse_time(m.get("content"))
    if not pub:
        m = soup.find("meta", attrs={"property": "article:modified_time"})
        if m:
            pub = parse_time(m.get("content"))

    # 摘要
    desc = soup.find("meta", attrs={"name": "description"}) or soup.find("meta", attrs={"property": "og:description"})
    summary = desc.get("content") if desc else None

    # 作者 / 署名
    author = "The Hindu"
    for el in soup.find_all(True):
        c = el.get("class")
        if not c:
            continue
        cs = " ".join(c).lower()
        if "byline" in cs or "author-name" in cs:
            t = el.get_text(" ", strip=True)
            if t and len(t) < 100:
                author = t
                break

    # 正文容器
    body = soup.select_one("div.articlebodycontent")
    if not body:
        body = soup.select_one("div.storyline")
    paragraphs = []
    if body:
        for tag in body.find_all(["script", "style"]):
            tag.decompose()
        for blk in body.find_all(["p", "h2", "h3", "blockquote", "li"]):
            txt = blk.get_text(" ", strip=True)
            if not txt:
                continue
            # 命中页脚/分享/标签噪音 → 停止
            stop = False
            for fm in FOOTER_MARKERS:
                if txt.startswith(fm) or fm in txt[:40]:
                    stop = True
                    break
            if stop:
                break
            paragraphs.append(txt)

    content = "\n\n".join(paragraphs)

    # 图片：og:image 主图 + 正文内 <figure> 配图
    images = []
    og_img = soup.find("meta", attrs={"property": "og:image"})
    if og_img and og_img.get("content"):
        images.append(og_img.get("content"))
    if body:
        for fig in body.find_all("figure"):
            im = fig.find("img")
            if not im:
                continue
            src = im.get("src") or im.get("data-src") or ""
            if not src:
                continue
            if any(n in src for n in NOISE_IMG):
                continue
            if src not in images:
                images.append(src)

    # 付费墙诚实标记
    premium = False
    if len(content) < 400:
        if "subscribe" in html.lower() or "continue reading" in html.lower() or "premium" in html.lower():
            premium = True

    return {
        "id": aid,
        "url": url,
        "title": title,
        "section": section_of(url),
        "author": author,
        "published_at": pub,
        "summary": summary,
        "content": content,
        "images": images,
        "premium": premium,
    }


# ---------------------------------------------------------------------------
# 主流程
# ---------------------------------------------------------------------------

def atomic_save(articles, path=OUTPUT):
    """原子写入：写 .tmp -> flush -> fsync -> os.replace，防止进程中断留半截 JSON。"""
    os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
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
    ap.add_argument("--root", action="append", default=[])
    ap.add_argument("--delay", type=float, default=3.0)
    ap.add_argument("--playwright", action="store_true")
    args = ap.parse_args()


    print("[list] 采集文章链接（RSS + Google News sitemap + 栏目页）...")
    links = collect_links(extra_roots=args.root)
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
                atomic_save(articles)  # 实时落盘
                continue
            art = parse_article(url, aid, use_playwright=args.playwright)
            if art and art.get("content"):
                articles.append(art)
                print("        ✓ 标题={0} 正文={1}字 图={2}张 栏目={3} 署名={4} 付费={5}".format(
                    (art["title"][:30] if art["title"] else None),
                    len(art["content"]),
                    len(art["images"]),
                    art["section"],
                    art["author"],
                    art["premium"],
                ))
                atomic_save(articles)  # 实时落盘，防中断丢数据
            else:
                print("        ✗ 无正文（可能为纯图集/视频/不可达）")
            time.sleep(args.delay)
    except KeyboardInterrupt:
        print("\n[interrupted] 已抓取的 {0} 篇已原子落盘".format(len(articles)), file=sys.stderr)
        return

    atomic_save(articles)  # 收尾再存（幂等）
    print("\n[done] 已保存 {0} 篇 → {1}".format(len(articles), OUTPUT))


if __name__ == "__main__":
    main()
