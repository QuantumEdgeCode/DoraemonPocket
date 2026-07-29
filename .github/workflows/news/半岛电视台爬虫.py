#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Al Jazeera 爬虫  (www.aljazeera.com)
====================================
双引擎（静态 requests + BeautifulSoup 优先；Playwright 渲染降级兜底），
与之前 tass/ria/ng/rg/asahi/nikkei 等爬虫保持同一套约定。

站点特点（已 recon 验证）：
  * 纯 SSR，静态 HTML 即可取到全文，无 JS 挑战 / 无付费墙。
  * 正文容器固定为 `div.wysiwyg.wysiwyg--all-content`，内部 <p> 即正文段落。
  * 时间优先取 JSON-LD `datePublished`（已带 Z = UTC），无需 datetime+Xh 时区 hack。
  * 列表发现三路并行：
      1) sitemap：/sitemaps/article-new.xml 是 sitemap index，含 209 个按日期的
         子 sitemap（/sitemaps/article-new/28-07-2026.xml ...），每个约 38 条文章 URL
         + <lastmod>（UTC）。默认取最近 SITEMAP_DAYS 天。
      2) 首页 / 各栏目页 SSR 内嵌相对链接 /<section>/<YYYY>/<M>/<D>/<slug>。
      3) RSS：/xml/rss/all.xml（25 条，description 为空，仅作 URL+日期补充）。
  * 图片：正文真实配图在 /wp-content/uploads/... ；站脚 logo /static/media/*.svg 须剔除。
  * author：JSON-LD 多为机构 "Al Jazeera"，个人署名少见，直接取 JSON-LD author.name。

用法：
  python aljazeera_crawler.py                 # 默认发现+抓取（limit 200）
  python aljazeera_crawler.py --limit 600     # 抓取 600 篇
  python aljazeera_crawler.py --sitemap-days 7   # 仅最近 7 天 sitemap
  python aljazeera_crawler.py --root https://www.aljazeera.com/news/   # 指定起始页
  python aljazeera_crawler.py --no-detail     # 仅列出发现到的链接
  python aljazeera_crawler.py --delay 2       # 抓取间隔秒数

输出：aljazeera_collection.json（与现有俄语/日语/财经站爬虫同目录）
合规：Al Jazeera Media Network 内容版权归其所有；本脚本仅用于个人学习/研究，禁止商用转发。
"""

import argparse
import json
import os
import re
import sys
import time
from datetime import datetime, timezone
from email.utils import parsedate_to_datetime
from urllib.parse import urljoin, urlparse

import requests
from bs4 import BeautifulSoup

try:
    from playwright.sync_api import sync_playwright
    PLAYWRIGHT_OK = True
except Exception:
    PLAYWRIGHT_OK = False

BASE = "https://www.aljazeera.com"
HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/124.0 Safari/537.36"
    ),
    "Accept-Language": "en-US,en;q=0.9",
}
OUT_FILE = "data/新闻/aljazeera_collection.json"

# 默认抓取的最近 sitemap 天数（article-new 共约 209 天，按日期降序排列）
SITEMAP_DAYS = 30

# 默认起始页：首页 + 主要栏目（SSR 内嵌文章相对链接）
DEFAULT_ROOTS = [
    "https://www.aljazeera.com/",
    "https://www.aljazeera.com/news/",
    "https://www.aljazeera.com/opinion/",
    "https://www.aljazeera.com/features/",
    "https://www.aljazeera.com/sport/",
    "https://www.aljazeera.com/economy/",
    "https://www.aljazeera.com/world/",
    "https://www.aljazeera.com/middle-east/",
    "https://www.aljazeera.com/asia/",
    "https://www.aljazeera.com/europe/",
    "https://www.aljazeera.com/africa/",
    "https://www.aljazeera.com/us-canada/",
    "https://www.aljazeera.com/science/",
]

# 文章 URL：/<section>/<YYYY>/<M>/<D>/<slug>
ARTICLE_PATH_RE = re.compile(r"^/([a-z\-]+)/(\d{4})/(\d{1,2})/(\d{1,2})/([a-z0-9\-]+)/?$")
# 完整 URL 形式
ARTICLE_URL_RE = re.compile(
    r"https?://(?:www\.)?aljazeera\.com/([a-z\-]+)/\d{4}/\d{1,2}/\d{1,2}/[a-z0-9\-]+", re.I
)

# 噪声图片
NOISE_IMG = ["/static/media/", ".svg", "logo", "aj-footer", "aj-logo"]


def fetch(url, use_playwright=False, timeout=25):
    """返回 (html, ok)。静态优先，失败/空则 Playwright 渲染降级。"""
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
    """规范化：去 ?traffic_source=rss 等 query、去尾斜杠、转小写 host。"""
    u = u.split("?")[0].split("#")[0]
    if u.endswith("/"):
        u = u[:-1]
    return u


def discover_from_sitemap(days=SITEMAP_DAYS):
    """从 article-new.xml 取最近 days 天的子 sitemap，提取文章 URL + lastmod。"""
    out = {}  # url -> lastmod
    try:
        idx_html, ok = fetch(BASE + "/sitemaps/article-new.xml")
        if not ok:
            return out
        subs = re.findall(r"<loc>(.*?)</loc>", idx_html)
        # 按日期降序，取最近 days 个
        subs = subs[:days]
        print(f"[sitemap] article-new.xml 含 {len(subs)} 个子 sitemap（取最近 {days} 天）")
        for sub in subs:
            try:
                sh, ok = fetch(sub)
                if not ok:
                    continue
                # 逐条 <url><loc>..</loc><lastmod>..</lastmod>
                for m in re.finditer(r"<url>(.*?)</url>", sh, re.S):
                    block = m.group(1)
                    loc = re.search(r"<loc>(.*?)</loc>", block)
                    lm = re.search(r"<lastmod>(.*?)</lastmod>", block)
                    if not loc:
                        continue
                    u = _clean_url(loc.group(1))
                    if ARTICLE_URL_RE.search(u):
                        out.setdefault(u, lm.group(1) if lm else None)
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
                elif ARTICLE_URL_RE.search(h):
                    full = _clean_url(h)
                else:
                    continue
                if ARTICLE_URL_RE.search(full):
                    out.setdefault(full, None)
        except Exception:
            continue
    return out


def discover_from_rss():
    out = {}
    try:
        r = requests.get(BASE + "/xml/rss/all.xml", headers=HEADERS, timeout=20)
        if r.status_code == 200:
            for it in re.finditer(r"<item>(.*?)</item>", r.text, re.S):
                block = it.group(1)
                link = re.search(r"<link>(.*?)</link>", block)
                pub = re.search(r"<pubDate>(.*?)</pubDate>", block)
                if link:
                    u = _clean_url(link.group(1))
                    if ARTICLE_URL_RE.search(u):
                        out.setdefault(u, pub.group(1) if pub else None)
    except Exception:
        pass
    return out


def collect_links(roots, days=SITEMAP_DAYS):
    print("\n=== 发现文章链接 ===")
    links = {}
    sm = discover_from_sitemap(days)
    links.update(sm)
    print(f"  sitemap 发现：{len(sm)} 条")
    rs = discover_from_roots(roots)
    links.update(rs)
    print(f"  起始页发现：{len(rs)} 条")
    rss = discover_from_rss()
    links.update(rss)
    print(f"  RSS 发现：{len(rss)} 条")
    print(f"  去重合计：{len(links)} 条唯一文章 URL")
    return links


def parse_time(raw):
    """解析多种时间格式为带 UTC 时区的 ISO 字符串。失败返回 None。"""
    if not raw:
        return None
    raw = raw.strip()
    try:
        if raw.endswith("Z"):
            dt = datetime.fromisoformat(raw.replace("Z", "+00:00"))
            return dt.isoformat()
        if "+" in raw or "T" in raw:
            dt = datetime.fromisoformat(raw)
            if dt.tzinfo is None:
                dt = dt.replace(tzinfo=timezone.utc)
            return dt.isoformat()
        # RSS pubDate: 'Tue, 28 Jul 2026 18:25:13 +0000'
        dt = parsedate_to_datetime(raw)
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return dt.isoformat()
    except Exception:
        return None


def parse_article(url, fallback_time=None, use_playwright=False):
    html, ok = fetch(url, use_playwright=use_playwright)
    if not ok:
        return None
    soup = BeautifulSoup(html, "html.parser")

    # --- 正文容器 ---
    main = soup.select_one("div.wysiwyg--all-content")
    if not main:
        return None
    for tag in main.find_all(["script", "style"]):
        tag.decompose()
    blocks = []
    for el in main.find_all(["p", "h2", "h3", "blockquote", "li"]):
        # 跳过位于 figure 内的（图注单独处理）
        if el.find_parent("figure"):
            continue
        t = el.get_text(" ", strip=True)
        if t:
            blocks.append(t)
    content = "\n\n".join(blocks).strip()
    if len(content) < 80:
        return None  # 非文章页（图集/视频等）

    # --- 标题 ---
    title = None
    h1 = soup.find("h1")
    if h1:
        title = h1.get_text(" ", strip=True)
    if not title:
        og = soup.find("meta", attrs={"property": "og:title"})
        if og:
            title = og.get("content")
    if not title:
        title = _ld_field(soup, "headline")

    # --- 时间 ---
    published = _ld_field(soup, "datePublished")
    if not published:
        published = fallback_time
    if not published:
        mt = soup.find("meta", attrs={"property": "article:published_time"})
        if mt:
            published = mt.get("content")
    published_iso = parse_time(published)

    # --- 栏目（URL 第一段）---
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
    author = _ld_field(soup, "author", sub="name") or "Al Jazeera"

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
        if "/wp-content/uploads/" in src:
            if src not in images:
                images.append(src)

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
        "language": "en",
    }


def _ld_field(soup, field, sub=None):
    """从 JSON-LD 中提取字段。"""
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


# ---------------------------------------------------------------------------
# JSON 原子保护性写入（实时落盘，防进程中断丢数据）
# ---------------------------------------------------------------------------
def atomic_save(items, path=OUT_FILE):
    """写 .tmp -> flush+fsync -> os.replace 改名，杜绝半截 JSON；逐篇实时调用。"""
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
    ap = argparse.ArgumentParser(description="Al Jazeera 爬虫")
    ap.add_argument("--limit", type=int, default=200, help="抓取文章数量上限（默认 200）")
    ap.add_argument("--root", action="append", default=None, help="额外起始页 URL（可多次）")
    ap.add_argument("--sitemap-days", type=int, default=SITEMAP_DAYS, help="sitemap 最近天数（默认 30）")
    ap.add_argument("--delay", type=float, default=3.0, help="每篇间隔秒数（默认 3）")
    ap.add_argument("--no-detail", action="store_true", help="仅列出发现的链接，不抓正文")
    ap.add_argument("--playwright", action="store_true", help="正文抓取启用 Playwright 渲染降级")
    args = ap.parse_args()


    roots = list(DEFAULT_ROOTS)
    if args.root:
        roots = args.root

    links = collect_links(roots, days=args.sitemap_days)

    # 排序：有 lastmod 的优先（较新），其余随意
    items = sorted(links.items(), key=lambda kv: kv[1] or "")

    if args.no_detail:
        print(f"\n[no-detail] 共发现 {len(items)} 条链接，写出链接清单。")
        try:
            atomic_save([{"url": u, "lastmod": t} for u, t in items],
                        "data/新闻/aljazeera_links.json")
            print("已写出 aljazeera_links.json")
        except KeyboardInterrupt:
            print("\n[!] 被用户中断，链接清单未保存")
            raise
        except Exception as exc:
            print(f"\n[!] 写入异常: {exc}")
            raise
        return

    limit = args.limit
    articles = []
    seen_titles = set()
    for i, (url, lastmod) in enumerate(items, 1):
        if limit and len(articles) >= limit:
            break
        print(f"[{i}/{len(items)}] 解析：{url}")
        art = parse_article(url, fallback_time=lastmod, use_playwright=args.playwright)
        if art and art.get("content") and art["title"] not in seen_titles:
            seen_titles.add(art["title"])
            articles.append(art)
            print(f"        ✓ 标题={art['title'][:40]} 正文={len(art['content'])}字 "
                  f"图={len(art['images'])}张 栏目={art['section']} 时间={art['published_at']}")
        else:
            print(f"        ✗ 跳过（无正文/重复/不可达）")
        time.sleep(args.delay)
        atomic_save(articles)  # 每篇实时落盘，防中断丢数据

    # 收尾保存（保证进度完整）+ 中断 / 异常保护
    try:
        atomic_save(articles)
        print(f"\n=== 完成 ===")
        print(f"成功入库：{len(articles)} 篇 → {OUT_FILE}")
        with_img = sum(1 for a in articles if a["images"])
        print(f"含图：{with_img} 篇 | 无图：{len(articles) - with_img} 篇")
    except KeyboardInterrupt:
        print(f"\n[!] 被用户中断，已实时保存至 {OUT_FILE}（当前 {len(articles)} 条）")
        raise
    except Exception as exc:
        print(f"\n[!] 写入异常: {exc}；已实时保存至 {OUT_FILE}（当前 {len(articles)} 条）")
        raise


if __name__ == "__main__":
    main()
