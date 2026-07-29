#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
The Straits Times 爬虫 (www.straitstimes.com)
=============================================
双引擎（静态 requests + BeautifulSoup 优先；Playwright 渲染降级兜底）。
与 tass/ria/ng/rg/asahi/nikkei/aljazeera 等爬虫同一套约定。

站点特点（已 recon 验证）：
  * 纯 SSR，静态 HTML 即可取到正文。 news/业务/体育等栏目为全文；opinion/分析类多为
    ST Premium 付费墙，静态仅能取到 ~2 段公开预览（teaser）。
  * 正文容器固定为 `div.storyline-wrapper`（内部 <p> 即正文，已剔除页眉「Sign up/
    Published/Updated/AI generated」等噪音；配图在同容器内）。
  * 时间优先取 meta `article:published_time`（已带 +08:00 新加坡时间），无需 datetime+Xh 时区 hack。
  * 列表发现两路：
      1) sitemap：robots 指向 /sitemap.xml（sitemap index），含 sections.xml + 按月
         /sitemap/YYYY/MM/feeds.xml（每月约 5000 条文章 URL）。默认取最近
         SITEMAP_MONTHS=2 个月。这是最全来源。
      2) 首页 / 栏目页 SSR 内嵌相对链接（首页约 81 条深链）。
  * 配图：真实配图在 cassette.sphdigital.com.sg；站标 /assets/ST-logo、/assets/subscribe-placeholder
    须剔除；og:image 兜底。
  * author：JSON-LD author.name（员工稿有，如 "Sue-Ann Tan"）；通讯社/电线稿多为空 → 回退 "The Straits Times"。
  * 付费标记：正文极短（<1000 字）或含「Continue reading / Subscribe to read」等提示 → 记 premium=True。

用法：
  python straitstimes_crawler.py                  # 默认发现+抓取（limit 200）
  python straitstimes_crawler.py --limit 800      # 抓取 800 篇
  python straitstimes_crawler.py --sitemap-months 1   # 仅最近 1 个月 sitemap
  python straitstimes_crawler.py --root <栏目URL>  # 额外起始页
  python straitstimes_crawler.py --no-detail      # 仅列出发现到的链接
  python straitstimes_crawler.py --delay 3        # 抓取间隔秒数

输出：straitstimes_collection.json
合规：内容版权归 SPH Media / The Straits Times；本脚本仅用于个人学习/研究，禁止商用转发。
注意：Premium 文章仅能取到公开预览片段，非全文。
"""

import argparse
import json
import os
import re
import time
from datetime import datetime, timezone
from urllib.parse import urljoin, urlparse

import requests
from bs4 import BeautifulSoup

try:
    from playwright.sync_api import sync_playwright
    PLAYWRIGHT_OK = True
except Exception:
    PLAYWRIGHT_OK = False

BASE = "https://www.straitstimes.com"
HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/124.0 Safari/537.36"
    ),
    "Accept-Language": "en-SG,en;q=0.9",
}
OUT_FILE = "data/新闻/straitstimes_collection.json"

# 默认抓取的 sitemap 月数（每月 feeds.xml 约 5000 条文章 URL）
SITEMAP_MONTHS = 2

# 非文章链接前缀（栏目首页/作者页/标签页等）
NON_ARTICLE_PREFIXES = (
    "authors/", "author/", "tag/", "tags/", "search/", "headstart",
    "topic/", "topics/", "live/", "newsletter", "subscribe", "account",
)

# 默认起始页（首页 + 主要栏目，SSR 内嵌文章链接；sitemap 才是主来源）
DEFAULT_ROOTS = [
    "https://www.straitstimes.com/",
    "https://www.straitstimes.com/singapore/",
    "https://www.straitstimes.com/asia/",
    "https://www.straitstimes.com/world/",
    "https://www.straitstimes.com/business/",
    "https://www.straitstimes.com/life/",
    "https://www.straitstimes.com/sport/",
    "https://www.straitstimes.com/opinion/",
]

# 噪声图片
NOISE_IMG = ["/assets/", "logo", "placeholder", "subscribe"]

# 付费墙提示词
PREMIUM_MARKERS = ["continue reading", "subscribe to read", "to continue reading", "this article is only available to"]


def fetch(url, use_playwright=False, timeout=25):
    try:
        r = requests.get(url, headers=HEADERS, timeout=timeout)
        if r.status_code == 200 and len(r.text) > 2000:
            return r.text, True
    except Exception:
        pass
    if use_playwright and PLAYWRIGHT_OK:
        try:
            with sync_playwright() as p:
                b = p.chromium.launch(headless=True)
                pg = b.new_page()
                pg.goto(url, timeout=timeout * 1000, wait_until="networkidle")
                html = pg.content()
                b.close()
                if html:
                    return html, True
        except Exception:
            pass
    return "", False



def _clean_url(u):
    u = u.split("?")[0].split("#")[0]
    if u.endswith("/"):
        u = u[:-1]
    return u


def is_article_url(u):
    path = urlparse(u).path.lstrip("/")
    if not path:
        return False
    if any(path.startswith(p) for p in NON_ARTICLE_PREFIXES):
        return False
    segs = path.split("/")
    # 文章至少 2 段且末段像 slug（含连字符、非文件扩展名）
    if len(segs) < 2:
        return False
    last = segs[-1]
    if "." in last:
        return False
    return True


def discover_from_sitemap(months=SITEMAP_MONTHS):
    out = {}
    try:
        idx_html, ok = fetch(BASE + "/sitemap.xml")
        if not ok:
            return out
        subs = re.findall(r"<loc>(.*?)</loc>", idx_html)
        # 仅取按月 feeds.xml，按出现顺序（降序）取最近 months 个
        feed_urls = [s for s in subs if re.search(r"/sitemap/\d{4}/\d{2}/feeds\.xml$", s)]
        feed_urls = feed_urls[:months]
        print(f"[sitemap] 取最近 {months} 个月 feeds.xml：{feed_urls}")
        for fu in feed_urls:
            try:
                fh, ok = fetch(fu)
                if not ok:
                    continue
                for loc in re.findall(r"<loc>(.*?)</loc>", fh):
                    u = _clean_url(loc)
                    if is_article_url(u):
                        out.setdefault(u, None)
            except Exception:
                continue
    except Exception as e:
        print(f"[sitemap] 错误: {e}")
    return out


def discover_from_roots(roots):
    out = {}
    for root in roots:
        try:
            html, ok = fetch(root)
            if not ok:
                print(f"  [warn] 起始页不可达：{root}")
                continue
            soup = BeautifulSoup(html, "html.parser")
            for a in soup.find_all("a", href=True):
                h = a["href"]
                if h.startswith("/"):
                    full = _clean_url(urljoin(BASE, h))
                elif BASE in h:
                    full = _clean_url(h)
                else:
                    continue
                if is_article_url(full):
                    out.setdefault(full, None)
        except Exception:
            continue
    return out


def collect_links(roots, months=SITEMAP_MONTHS):
    print("\n=== 发现文章链接 ===")
    links = {}
    sm = discover_from_sitemap(months)
    links.update(sm)
    print(f"  sitemap 发现：{len(sm)} 条")
    rs = discover_from_roots(roots)
    links.update(rs)
    print(f"  起始页发现：{len(rs)} 条")
    print(f"  去重合计：{len(links)} 条唯一文章 URL")
    return links


def parse_time(raw):
    if not raw:
        return None
    raw = raw.strip()
    try:
        if raw.endswith("Z"):
            dt = datetime.fromisoformat(raw.replace("Z", "+00:00"))
        else:
            dt = datetime.fromisoformat(raw)
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return dt.isoformat()
    except Exception:
        return None


def parse_article(url, use_playwright=False):
    html, ok = fetch(url, use_playwright=use_playwright)
    if not ok:
        return None
    soup = BeautifulSoup(html, "html.parser")

    # --- 正文容器 ---
    main = soup.select_one("div.storyline-wrapper")
    if not main:
        # 回退：整篇 <article> 去脚本样式后取 <p>
        art = soup.find("article")
        if not art:
            return None
        main = art
    for tag in main.find_all(["script", "style"]):
        tag.decompose()
    blocks = []
    for el in main.find_all(["p", "h2", "h3", "blockquote", "li"]):
        if el.find_parent("figure"):
            continue
        t = el.get_text(" ", strip=True)
        if t:
            blocks.append(t)
    content = "\n\n".join(blocks).strip()
    if len(content) < 80:
        return None

    # --- 标题 ---
    title = None
    h1 = soup.find("h1")
    if h1:
        title = h1.get_text(" ", strip=True)
    if not title:
        title = _ld_field(soup, "headline")
    if not title:
        og = soup.find("meta", attrs={"property": "og:title"})
        if og:
            title = og.get("content")

    # --- 时间 ---
    published = None
    mt = soup.find("meta", attrs={"property": "article:published_time"})
    if mt:
        published = mt.get("content")
    if not published:
        published = _ld_field(soup, "datePublished")
    published_iso = parse_time(published)

    # --- 栏目 ---
    seg = urlparse(url).path.strip("/").split("/")
    section = seg[0] if seg else None

    # --- 摘要 ---
    summary = None
    md = soup.find("meta", attrs={"name": "description"}) or soup.find("meta", attrs={"property": "description"})
    if md:
        summary = md.get("content")
    if not summary:
        summary = _ld_field(soup, "description")

    # --- 作者 ---
    author = _ld_field(soup, "author", sub="name")
    if not author:
        author = "The Straits Times"

    # --- 图片 ---
    images = []
    og_img = soup.find("meta", attrs={"property": "og:image"})
    if og_img and og_img.get("content"):
        images.append(og_img["content"])
    for im in main.find_all("img"):
        src = im.get("src") or im.get("data-src") or ""
        if src.startswith("//"):
            src = "https:" + src
        if not src:
            continue
        if any(n in src for n in NOISE_IMG):
            continue
        if "cassette.sphdigital.com.sg" in src and src not in images:
            images.append(src)

    # --- 付费墙标记 ---
    low = html.lower()
    premium = (len(content) < 1000) or any(m in low for m in PREMIUM_MARKERS)

    slug = seg[-1] if seg else url
    return {
        "id": slug,
        "url": url,
        "title": title,
        "summary": summary,
        "content": content,
        "published_at": published_iso,
        "section": section,
        "author": author,
        "images": images,
        "premium": premium,
        "language": "en",
    }


def _ld_field(soup, field, sub=None):
    for tag in soup.find_all("script", attrs={"type": "application/ld+json"}):
        txt = tag.string or tag.get_text()
        try:
            data = json.loads(txt)
        except Exception:
            continue
        found = []

        def walk(o):
            if isinstance(o, dict):
                if field in o:
                    v = o[field]
                    if sub and isinstance(v, dict):
                        v = v.get(sub)
                    found.append(v)
                for val in o.values():
                    walk(val)
            elif isinstance(o, list):
                for val in o:
                    walk(val)

        walk(data)
        if found:
            v = found[0]
            if isinstance(v, list):
                v = ", ".join(str(x) for x in v)
            return v if v else None
    return None


def atomic_save(items, path=OUT_FILE):
    """原子写入 JSON：先写 .tmp，flush+fsync 落盘，再 os.replace 改名，杜绝半截损坏文件。"""
    d = os.path.dirname(path)
    if d:
        os.makedirs(d, exist_ok=True)
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(items, f, ensure_ascii=False, indent=2)
        f.flush()
        os.fsync(f.fileno())
    os.replace(tmp, path)


def main():
    ap = argparse.ArgumentParser(description="The Straits Times 爬虫")
    ap.add_argument("--limit", type=int, default=200, help="抓取文章数量上限（默认 200）")
    ap.add_argument("--root", action="append", default=None, help="额外起始页 URL（可多次）")
    ap.add_argument("--sitemap-months", type=int, default=SITEMAP_MONTHS, help="sitemap 最近月数（默认 2）")
    ap.add_argument("--delay", type=float, default=3.0, help="每篇间隔秒数（默认 3）")
    ap.add_argument("--no-detail", action="store_true", help="仅列出发现的链接，不抓正文")
    ap.add_argument("--playwright", action="store_true", help="正文抓取启用 Playwright 渲染降级")
    args = ap.parse_args()


    roots = list(DEFAULT_ROOTS)
    if args.root:
        roots = args.root

    links = collect_links(roots, months=args.sitemap_months)

    if args.no_detail:
        print(f"\n[no-detail] 共发现 {len(links)} 条链接，写出链接清单。")
        atomic_save([{"url": u} for u in links], "data/新闻/straitstimes_links.json")
        print("已写出 straitstimes_links.json")
        return

    limit = args.limit
    articles = []
    seen_titles = set()
    total = len(links)
    try:
        for i, url in enumerate(links, 1):
            if limit and len(articles) >= limit:
                break
            print(f"[{i}/{total}] 解析：{url}")
            art = parse_article(url, use_playwright=args.playwright)
            if art and art.get("content") and art["title"] not in seen_titles:
                seen_titles.add(art["title"])
                articles.append(art)
                atomic_save(articles, OUT_FILE)
                print(f"        ✓ 标题={art['title'][:38]} 正文={len(art['content'])}字 "
                      f"图={len(art['images'])}张 栏目={art['section']} "
                      f"premium={art['premium']} 时间={art['published_at']}")
            else:
                print(f"        ✗ 跳过（无正文/重复/不可达）")
            time.sleep(args.delay)
    except (KeyboardInterrupt, Exception) as e:
        print(f"\n[!] 中断（{type(e).__name__}）：已实时保存至当前进度 → {OUT_FILE}")
        raise

    atomic_save(articles, OUT_FILE)
    print(f"\n=== 完成 ===")
    print(f"成功入库：{len(articles)} 篇 → {OUT_FILE}")
    with_img = sum(1 for a in articles if a["images"])
    premium_n = sum(1 for a in articles if a["premium"])
    print(f"含图：{with_img} 篇 | 无图：{len(articles) - with_img} 篇 | 标记为 Premium(预览)：{premium_n} 篇")


if __name__ == "__main__":
    main()
