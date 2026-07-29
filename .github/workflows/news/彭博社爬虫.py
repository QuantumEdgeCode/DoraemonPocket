#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
bloomberg_crawler.py — 爬取 https://www.bloomberg.com/ (Bloomberg News, 美国)
================================================================================
方法（双引擎：静态优先 / Playwright 降级，沿用统一约定）
  ★ 重要现实（务必先读）：Bloomberg 使用 HUMAN / PerimeterX 企业级反爬，
    对**数据中心 IP**（本沙箱环境）在文章详情页直接返回 403 "Are you a robot?"，
    requests 与 Chromium 均被拦（cookie 复用也无效，属 IP 信誉级封锁）。
    因此本环境只能稳定拿到【sitemap 层的链接 + lastmod 时间】，详情正文普遍取不到。

  - 文章发现（robust 主路径）：sitemap
        robots.txt → Sitemap: /sitemaps/news/index.xml（索引，427 个月级子 sitemap，
          历史可回溯至 1991 年）+ /sitemaps/news/latest.xml（最新聚合 ~537 条）
        默认仅用 latest.xml；--months N 时再回溯最近 N 个月级子 sitemap（index.xml 顺序）
        URL 形如 /news/articles/<YYYY-MM-DD>/<slug>；过滤掉 /news/newsletters/ 等
  - 正文解析（详情引擎，仅当未被反爬拦截时生效）：
        Bloomberg 正文在 <script type="application/ld+json"> 的 NewsArticle 块中：
          · headline         → 标题
          · articleBody      → 全文（纯文本，付费文可能截断）
          · datePublished    → 时间（UTC，Z 结尾）
          · author           → 署名（可多名）
          · image            → 头图
        HTML 容器（兜底）：Next.js，静态 HTML 中含 JSON-LD，优先取它；
        若 JSON-LD 缺失再试 <h1> / og:title + 正文 DOM（本环境基本不会走到，因 403）
  - 时间：datePublished（UTC, Z）→ lastmod（sitemap, UTC）→ meta；
        用 datetime.fromisoformat 保留时区，**禁 datetime+小时 hack**（tz 正确）
  - 标题兜底：当详情被拦截（403）时，从 URL slug 生成可读标题
        （如 ".../ubs-starts-3-billion-buyback-..." → "Ubs Starts 3 Billion Buyback ..."）
  - 图片：JSON-LD image / og:image（详情引擎）；被拦截时无图

诚信标记：
  - 详情被反爬拦截（403 / "Are you a robot"）时，记录 content_source="detail_blocked"，
    仅保留 sitemap 提供的标题(由slug推断) + lastmod 时间，正文留空，绝不伪造内容。
  - 与 TASS 同理：sitemap 是可靠来源，详情降级是诚实回退。

绕过反爬（用户在本机/住宅 IP 执行）：
  - 用 --cookie 注入**本人真实浏览器 cookie**（Chrome DevTools → Application → Cookies，
    导出 name=value 对，如 "session_id=...; _pxhd=..."），可显著提高通过率。
  - 住宅/家庭 IP + 真实指纹（本机 Chrome）最可能过 PerimeterX。
  - 商业方案：住宅代理 + 反检测浏览器（Bright Data / Oxylabs）。

合规：Bloomberg L.P. 内容版权归其所有；本脚本仅用于个人学习/研究，禁止商用转发。
robots.txt 仅作提示（含 Crawl-delay），不阻断抓取；本环境详情页被反爬拦与 robots 无关。

用法：
  python bloomberg_crawler.py                  # 默认：latest.xml 全量（约 537 条）
  python bloomberg_crawler.py --limit 50       # 控量
  python bloomberg_crawler.py --months 3       # 回溯最近 3 个月级 sitemap
  python bloomberg_crawler.py --no-detail      # 仅采集链接，不抓详情
  python bloomberg_crawler.py --delay 3        # 请求间隔（秒）
  python bloomberg_crawler.py --cookie "k=v;k2=v2"   # 注入 cookie 绕过反爬（本机用）
  python bloomberg_crawler.py --cookie-file cookie.txt
"""

import argparse
import json
import os
import re
import time
import html as _html
from datetime import datetime, timezone, timedelta
from urllib.parse import urlparse

import requests
import urllib3
from bs4 import BeautifulSoup

# Bloomberg mirror intermittently fails TLS (UNEXPECTED_EOF); we fetch with
# verify=False, so silence the InsecureRequestWarning noise in the logs.
urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)

try:
    from playwright.sync_api import sync_playwright
    HAVE_PW = True
except Exception:
    HAVE_PW = False

BASE = "https://www.bloomberg.com"
SITEMAP_INDEX = BASE + "/sitemaps/news/index.xml"
SITEMAP_LATEST = BASE + "/sitemaps/news/latest.xml"
OUTPUT = "data/新闻/bloomberg_collection.json"
HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                  "(KHTML, like Gecko) Chrome/124.0 Safari/537.36",
    "Accept-Language": "en-US,en;q=0.9",
}

# 文章 URL：/news/articles/<YYYY-MM-DD>/<slug>
ARTICLE_RE = re.compile(
    r"https?://(?:www\.)?bloomberg\.com/news/articles/(\d{4}-\d{2}-\d{2})/([a-z0-9\-]+)",
    re.I,
)
TARGET_TYPES = {"NewsArticle", "ReportageNewsArticle", "Article", "NewsStory"}
NOISE_PREFIX = ("/news/newsletters/", "/news/latest", "/news/videos/",
                "/news/audio/", "/news/live/", "/markets/", "/podcasts/")

# sitemap 偶发 SSL EOF，需重试
SSL_RETRIES = 5
SSL_BACKOFF = 2.0


# ---------------------------------------------------------------------------
# 网络获取
# ---------------------------------------------------------------------------
def _session(cookies=None):
    s = requests.Session()
    s.headers.update(HEADERS)
    if cookies:
        for k, v in cookies.items():
            s.cookies.set(k, v)
    return s


def fetch_url(session, url, verify=False):
    """通用 GET，带 SSL 重试；返回 (text, status) 或 (None, None)。"""
    last = None
    for i in range(SSL_RETRIES):
        try:
            r = session.get(url, timeout=30, verify=verify)
            return r.text, r.status_code
        except Exception as e:
            last = e
            time.sleep(SSL_BACKOFF)
    return None, None


def is_robot_page(text):
    return text is not None and "Are you a robot" in text


def fetch_playwright(url, cookies=None):
    if not HAVE_PW:
        return None, None
    try:
        with sync_playwright() as p:
            b = p.chromium.launch(headless=True, args=["--no-sandbox"])
            ctx = b.new_context()
            if cookies:
                for k, v in cookies.items():
                    try:
                        ctx.add_cookies([{"name": k, "value": v,
                                          "url": BASE}])
                    except Exception:
                        pass
            pg = ctx.new_page()
            pg.set_extra_http_headers({"Accept-Language": "en-US,en;q=0.9"})
            resp = pg.goto(url, timeout=45000, wait_until="domcontentloaded")
            pg.wait_for_timeout(3000)
            html = pg.content()
            status = resp.status if resp else 0
            b.close()
            return html, status
    except Exception:
        return None, None


# ---------------------------------------------------------------------------
# 链接采集（sitemap）
# ---------------------------------------------------------------------------
def collect_links(months=0):
    """返回 {url: lastmod_iso} 。"""
    links = {}

    def parse_sitemap(text):
        for m in re.finditer(r"<url>\s*<loc>(.*?)</loc>\s*<lastmod>(.*?)</lastmod>",
                              text, re.S):
            loc = m.group(1).strip()
            lm = m.group(2).strip()
            if ARTICLE_RE.match(loc) and not any(loc.startswith(BASE + p)
                                                 for p in NOISE_PREFIX):
                links.setdefault(loc, lm)

    s = _session()
    # 1) 默认 latest.xml
    txt, st = fetch_url(s, SITEMAP_LATEST)
    if st == 200 and txt:
        parse_sitemap(txt)
        print("[list] latest.xml 命中 {0} 条".format(len(links)))
    else:
        print("[list] latest.xml 不可达（status={0}），尝试 index".format(st))

    # 2) 回溯月级 sitemap
    if months > 0:
        idx, ist = fetch_url(s, SITEMAP_INDEX)
        subs = re.findall(r"<loc>(.*?)</loc>", idx) if (ist == 200 and idx) else []
        # index 中顺序大致从近到远，取前 months 个
        chosen = subs[:months]
        for sub in chosen:
            if not sub.endswith(".xml"):
                continue
            stxt, sst = fetch_url(s, sub)
            if sst == 200 and stxt:
                before = len(links)
                parse_sitemap(stxt)
                print("[list] {0} 新增 {1} 条".format(
                    sub.split("/")[-1], len(links) - before))
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
            d = d.replace(tzinfo=timezone.utc)
        return d.isoformat()
    except Exception:
        pass
    try:
        from email.utils import parsedate_to_datetime
        dt = parsedate_to_datetime(s)
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
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


def slug_to_title(slug):
    """slug → 可读标题（兜底，仅在详情被拦截时使用）。"""
    words = slug.replace("_", "-").split("-")
    keep = [w for w in words if w and not w.isdigit()]
    if not keep:
        return slug
    t = " ".join(w[:1].upper() + w[1:] if w else w for w in keep)
    return t


def section_of(url):
    path = urlparse(url).path.strip("/")
    parts = [p for p in path.split("/") if p]
    # /news/articles/<date>/<slug> → "news/articles"
    if "articles" in parts:
        idx = parts.index("articles")
        return "/".join(parts[:idx + 1])
    return "/".join(parts[:1]) if parts else None


# ---------------------------------------------------------------------------
# 文章解析（详情引擎）
# ---------------------------------------------------------------------------
def parse_article(session, url, cookies, use_playwright=False):
    html, status = fetch_url(session, url)
    if html is None and use_playwright:
        html, status = fetch_playwright(url, cookies=cookies)
    if html is None:
        return None  # 网络失败
    if status != 200 or is_robot_page(html):
        return {"blocked": True, "status": status}

    blocks = extract_article_ldjson(html)
    if not blocks:
        # 兜底 DOM 解析（本环境 403 时不会走到）
        soup = BeautifulSoup(html, "html.parser")
        h1 = soup.find("h1")
        title = h1.get_text(strip=True) if h1 else None
        if not title:
            og = soup.find("meta", property="og:title")
            title = og.get("content") if og else None
        return {"blocked": False, "title": title, "body": None,
                "pub": None, "author": "Bloomberg", "images": [],
                "summary": None, "premium": False}

    d = blocks[0]
    title = d.get("headline") or d.get("alternativeHeadline")
    title = _html.unescape(title) if title else None
    if not title:
        og = re.search(r'<meta[^>]+property="og:title"[^>]+content="(.*?)"', html)
        title = _html.unescape(og.group(1)) if og else None

    body = d.get("articleBody")
    body = _html.unescape(body) if body else None

    pub = parse_time(d.get("datePublished") or d.get("dateModified"))
    author = normalize_authors(d.get("author")) or "Bloomberg"
    is_free = d.get("isAccessibleForFree")
    premium = (is_free is False)

    summary = d.get("description")
    summary = _html.unescape(summary) if summary else None
    if not summary:
        og = re.search(r'<meta[^>]+property="og:description"[^>]+content="(.*?)"', html)
        summary = _html.unescape(og.group(1)) if og else None

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

    if body and len(body) < 400 and not is_free:
        premium = True

    return {
        "blocked": False, "title": title, "body": body,
        "pub": pub, "author": author, "images": images,
        "summary": summary, "premium": premium,
    }


# ---------------------------------------------------------------------------
# 主流程
# ---------------------------------------------------------------------------

def load_cookies(args):
    cookies = {}
    if args.cookie:
        for pair in args.cookie.split(";"):
            if "=" in pair:
                k, v = pair.split("=", 1)
                cookies[k.strip()] = v.strip()
    if args.cookie_file and os.path.exists(args.cookie_file):
        with open(args.cookie_file, "r", encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if not line or line.startswith("#"):
                    continue
                if "=" in line:
                    k, v = line.split("=", 1)
                    cookies[k.strip()] = v.strip()
    if cookies:
        print("[cookie] 已注入 {0} 个 cookie".format(len(cookies)))
    return cookies


def atomic_save(records, path=OUTPUT, source="Bloomberg"):
    """原子写入：写 .tmp → flush → fsync → os.replace，防进程中断留半截 JSON。

    落盘裸数组（articles），每采集一篇实时调用，保证中断也能保留已抓数据。
    """
    os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(records, f, ensure_ascii=False, indent=2)
        f.flush()
        os.fsync(f.fileno())
    os.replace(tmp, path)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--limit", type=int, default=0,
                    help="最多解析 N 篇（0=全部）")
    ap.add_argument("--months", type=int, default=0,
                    help="回溯最近 N 个月级 sitemap（0=仅 latest.xml）")
    ap.add_argument("--no-detail", action="store_true")
    ap.add_argument("--delay", type=float, default=1.0,
                    help="请求间隔（sitemap 层较快；详情层尊重 Crawl-delay 可调大")
    ap.add_argument("--playwright", action="store_true",
                    help="强制用 Playwright 渲染详情（默认随请求失败降级）")
    ap.add_argument("--cookie", type=str, default=None,
                    help="注入 cookie 字符串 'k=v;k2=v2'（绕过反爬，本机用）")
    ap.add_argument("--cookie-file", type=str, default=None,
                    help="从文件读取 cookie（每行 k=v）")
    args = ap.parse_args()

    cookies = load_cookies(args)

    print("[list] 采集文章链接（sitemap: latest.xml"
          + ("" if args.months == 0 else " + 最近 {0} 个月".format(args.months)) + "）...")
    links = collect_links(months=args.months)
    print("[list] 发现唯一文章：{0} 篇".format(len(links)))

    items = list(links.items())
    if args.limit:
        items = items[: args.limit]

    session = _session(cookies=cookies)
    articles = []
    blocked = 0
    detailed = 0

    try:
        for i, (url, lastmod) in enumerate(items, 1):
            m = ARTICLE_RE.match(url)
            date_part = m.group(1) if m else None
            slug = m.group(2) if m else None
            # sitemap lastmod → published_at（UTC），优先于详情时间兜底
            pub_from_sitemap = parse_time(lastmod)

            print("[{0}/{1}] {2}".format(i, len(items), url))

            if args.no_detail:
                articles.append({
                    "url": url, "title": slug_to_title(slug) if slug else None,
                    "section": section_of(url),
                    "published_at": pub_from_sitemap,
                    "content_source": "sitemap_only",
                    "author": None, "content": "", "images": [], "premium": False,
                })
                atomic_save(articles)   # 实时落盘
                continue

            art = parse_article(session, url, cookies, use_playwright=args.playwright)
            if art is None:
                # 网络彻底失败
                articles.append(_meta_only(url, slug, section_of(url),
                                           pub_from_sitemap, "fetch_failed"))
                blocked += 1
                print("        ✗ 网络失败")
            elif art.get("blocked"):
                articles.append(_meta_only(url, slug, section_of(url),
                                           pub_from_sitemap, "detail_blocked"))
                blocked += 1
                print("        ⚠ 详情被反爬拦截（403，detail_blocked）")
            elif art.get("body"):
                art_full = {
                    "url": url,
                    "title": art["title"],
                    "section": section_of(url),
                    "author": art["author"],
                    "published_at": art["pub"] or pub_from_sitemap,
                    "summary": art["summary"],
                    "content": art["body"],
                    "images": art["images"],
                    "premium": art["premium"],
                    "content_source": "detail",
                }
                articles.append(art_full)
                detailed += 1
                print("        ✓ 标题={0} 正文={1}字 图={2} 署名={3} 付费={4}".format(
                    (art["title"][:42] if art["title"] else None),
                    len(art["body"]), len(art["images"]),
                    art["author"][:24], art["premium"]))
            else:
                # 详情可达但无正文（图集/视频/付费截断）
                articles.append(_meta_only(url, slug, section_of(url),
                                           pub_from_sitemap, "no_body",
                                           title=art.get("title")))
                blocked += 1
                print("        ⚠ 详情无正文（no_body）")

            atomic_save(articles)   # 实时落盘（每篇）
            time.sleep(args.delay)
    except (KeyboardInterrupt, Exception) as e:
        print("\n[中断] 捕获到异常/中断，已保存中断前进度（{0} 篇）→ {1}".format(
            len(articles), OUTPUT))
        atomic_save(articles)
        raise

    atomic_save(articles)
    print("\n[done] 已保存 {0} 篇 → {1}".format(len(articles), OUTPUT))
    print("       其中 详情成功 {0} 篇 / 仅元数据(被拦截或失败) {1} 篇".format(
        detailed, blocked))


def _meta_only(url, slug, section, pub_from_sitemap, source, title=None):
    return {
        "url": url,
        "title": title or (slug_to_title(slug) if slug else None),
        "section": section,
        "author": "Bloomberg" if source == "no_body" else None,
        "published_at": pub_from_sitemap,
        "content": "",
        "images": [],
        "premium": False,
        "content_source": source,
    }


if __name__ == "__main__":
    main()