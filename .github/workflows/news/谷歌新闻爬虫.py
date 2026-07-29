#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
gnews_crawler.py — Google News (news.google.com) crawler.

Google News is a *news aggregator*, not a publisher. Its content is exposed
cleanly through public RSS feeds (no Cloudflare / JS challenge on the feeds
themselves). This crawler:

  * DEFAULT  : Full-article mode. Discovers articles via the top-headlines feed
               + the 8 topic feeds for a chosen locale (rich metadata: title,
               source, published time, topic, snippet), THEN for each item opens
               the Google News link in a real browser (Playwright) which
               JS-redirects to the *original publisher* and generically extracts
               the article body. content_source = "full" | "unreachable".
               Slower / fragile (every publisher differs, some behind
               Cloudflare) — but this is the DEFAULT because the user wants
               real article text, not just headlines.

  * --metadata-only : OPT-OUT. Only collect the RSS metadata (no browser, no
               original fetch). Fast, reliable. content_source = "rss_metadata".

Design mirrors the other crawlers in this toolkit:
  - SSL hardened (verify=False + retry/backoff) — news.google.com is flaky.
  - Honest content_source tagging, never fabricates a section/source.
  - Unified collection schema (title/section/published_at/author/content/...).

Usage:
  python gnews_crawler.py                       # zh-CN, fetch original bodies (default)
  python gnews_crawler.py --metadata-only       # fast: RSS metadata only, no browser
  python gnews_crawler.py --lang en             # English locale
  python gnews_crawler.py --query "iran"        # add a search feed
  python gnews_crawler.py --limit 50
  python gnews_crawler.py --hl zh-CN --gl CN --ceid CN:zh-CN
"""

import argparse
import html
import json
import os
import re
import sys
import time
from collections import OrderedDict
from datetime import datetime, timezone
from email.utils import parsedate_to_datetime

import requests
from bs4 import BeautifulSoup
import urllib3

urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)

BASE = "https://news.google.com"
DEFAULT_HL, DEFAULT_GL, DEFAULT_CEID = "zh-CN", "CN", "CN:zh-CN"

TOPIC_CODES = [
    "WORLD", "NATION", "BUSINESS", "TECHNOLOGY",
    "ENTERTAINMENT", "SPORTS", "SCIENCE", "HEALTH",
]
# human label (zh) for topic codes — used to label the section field
TOPIC_LABEL_ZH = {
    "WORLD": "国际", "NATION": "国内", "BUSINESS": "财经", "TECHNOLOGY": "科技",
    "ENTERTAINMENT": "娱乐", "SPORTS": "体育", "SCIENCE": "科学", "HEALTH": "健康",
}

UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36")

_session = None


def get_session():
    global _session
    if _session is None:
        s = requests.Session()
        s.headers.update({"User-Agent": UA, "Accept-Language": "zh-CN,zh;q=0.9"})
        retry = urllib3.util.retry.Retry(
            total=4, backoff_factor=0.6,
            status_forcelist=[429, 500, 502, 503, 504],
            allowed_methods=frozenset(["GET"]),
        )
        adapter = requests.adapters.HTTPAdapter(max_retries=retry, pool_connections=8, pool_maxsize=8)
        s.mount("https://", adapter)
        s.mount("http://", adapter)
        _session = s
    return _session


def fetch_response(url, timeout=25):
    """SSL-hardened GET returning the requests.Response (or None). Never raises."""
    for attempt in range(4):
        try:
            r = get_session().get(url, timeout=timeout, verify=False)
            if r.status_code == 200:
                return r
            if r.status_code in (429, 500, 502, 503):
                time.sleep(1.0 * (attempt + 1))
                continue
            return r
        except Exception:
            if attempt < 3:
                time.sleep(0.8 * (attempt + 1))
                continue
            return None
    return None


def fetch_text(url, timeout=25):
    """SSL-hardened GET returning (decoded_text, status). Never raises."""
    r = fetch_response(url, timeout)
    if r is None:
        return "", 0
    try:
        return decode_body(r), r.status_code
    except Exception:
        return r.text, r.status_code


# ----------------------------------------------------------------------------
# Discovery: pull every <item> from the chosen feeds, dedupe by article URL.
# ----------------------------------------------------------------------------
def feed_url(kind, locale):
    hl, gl, ceid = locale
    if kind == "headlines":
        return f"{BASE}/rss?hl={hl}&gl={gl}&ceid={ceid}"
    if kind in TOPIC_CODES:
        return f"{BASE}/rss/headlines/section/topic/{kind}?hl={hl}&gl={gl}&ceid={ceid}"
    if kind.startswith("search:"):
        q = kind.split(":", 1)[1]
        return f"{BASE}/rss/search?q={requests.utils.quote(q)}&hl={hl}&gl={gl}&ceid={ceid}"
    return None


def parse_item(block, feed_section, lang):
    """Parse one <item>...</item> block into a normalized dict (metadata only)."""
    def grab(pat):
        m = re.search(pat, block, re.S)
        return m.group(1) if m else None

    raw_title = grab(r"<title>(.*?)</title>")
    link = grab(r"<link>(.*?)</link>")
    source = grab(r"<source[^>]*>(.*?)</source>")
    pub = grab(r"<pubDate>(.*?)</pubDate>")
    desc = grab(r"<description>(.*?)</description>")

    title = html.unescape(raw_title).strip() if raw_title else ""
    source = html.unescape(source).strip() if source else ""
    # title usually ends with " - SourceName"; strip it for a clean headline
    if source and title.endswith(f" - {source}"):
        title = title[: -(len(source) + 3)].strip()

    # snippet: decode description (HTML-escaped), strip tags, drop trailing source.
    # Google News <description> is a "more coverage" cluster:
    #   "Headline  Source  RelatedHeadline  Source2 ..."  (&nbsp; = \xa0).
    snippet = ""
    if desc:
        d = html.unescape(desc)
        d = d.replace("\xa0", " ").replace("&nbsp;", " ")
        d = re.sub(r"<[^>]+>", " ", d)
        d = re.sub(r"\s+", " ", d).strip()
        # drop the trailing "  Source" of the last item
        if source and d.endswith(source):
            d = d[: -len(source)].strip()
        # drop the leading main headline (== title) to avoid duplication
        if title and d.startswith(title):
            d = d[len(title):].strip(" ·•-")
        snippet = d

    published_at = ""
    if pub:
        try:
            dt = parsedate_to_datetime(pub.strip())
            if dt.tzinfo is None:
                dt = dt.replace(tzinfo=timezone.utc)
            published_at = dt.astimezone(timezone.utc).isoformat()
        except Exception:
            published_at = pub.strip()

    return {
        "title": title,
        "section": feed_section,
        "section_code": feed_section,
        "published_at": published_at,
        "author": source,          # publisher acts as the "author" (agency)
        "source": source,
        "content": snippet,
        "images": [],
        "tags": [],
        "url": link.strip() if link else "",
        "language": lang,
        "content_source": "rss_metadata",
    }


def discover(locale, queries=None, limit=None):
    """Return OrderedDict {url: item_meta} from headlines + topics + queries."""
    found = OrderedDict()
    feeds = [("headlines", "头条")] + [(t, TOPIC_LABEL_ZH.get(t, t)) for t in TOPIC_CODES]
    if queries:
        for q in queries:
            feeds.append((f"search:{q}", f"搜索:{q}"))

    for kind, label in feeds:
        url = feed_url(kind, locale)
        txt, st = fetch_text(url)
        if st != 200 or not txt:
            print(f"  ! feed {kind} status={st} (skipped)", file=sys.stderr)
            continue
        items = re.findall(r"<item>(.*?)</item>", txt, re.S)
        added = 0
        for block in items:
            it = parse_item(block, label, locale[0].split("-")[0])
            if not it["url"] or not it["title"]:
                continue
            if it["url"] not in found:
                found[it["url"]] = it
                added += 1
        print(f"  + feed {kind:<10} -> {added} new (total {len(found)})", file=sys.stderr)
        if limit and len(found) >= limit:
            break

    if limit:
        keys = list(found.keys())[:limit]
        found = OrderedDict((k, found[k]) for k in keys)
    return found


# ----------------------------------------------------------------------------
# OPT-IN: original-article resolution + generic extraction (Playwright).
# ----------------------------------------------------------------------------
def resolve_via_browser(url, browser):
    """Open the Google News link in a shared browser; it JS-redirects to the
    original publisher. Return the final publisher URL, or None if it never
    leaves news.google.com (so we honestly mark the item unreachable)."""
    if browser is None:
        return None
    try:
        pg = browser.new_page()
        final = None
        last_err = None
        for attempt in range(3):
            try:
                # 'load' (not domcontentloaded) so the redirect JS has executed
                pg.goto(url, timeout=30000, wait_until="load")
                # poll for a cross-origin redirect away from news.google.com
                for _ in range(50):
                    cur = pg.url
                    if cur and "news.google.com" not in cur:
                        final = cur
                        break
                    time.sleep(0.4)
                if final:
                    break
            except Exception as e:
                last_err = e
                time.sleep(1.0)
        if not final:
            try:
                cur = pg.url
                final = cur if cur and "news.google.com" not in cur else None
            except Exception:
                final = None
            if final is None and last_err:
                print(f"  ! resolve: {last_err}", file=sys.stderr)
        try:
            pg.close()
        except Exception:
            pass
        return final
    except Exception as e:
        print(f"  ! browser resolve failed: {e}", file=sys.stderr)
        return None


# Priority list of generic article-body selectors (publisher-agnostic).
ARTICLE_SELECTORS = [
    "article",
    "[itemprop='articleBody']",
    "[property='articleBody']",
    ".article-body", ".articleBody", ".post-content", ".entry-content",
    ".story-body", ".story-content", ".content-body", ".article__body",
    "main",
]


def decode_body(r):
    """Decode raw bytes honoring Content-Type charset, else UTF-8/GB18030/Big5.
    Chinese publishers often serve GBK without a BOM, which requests mis-reads."""
    raw = r.content
    m = re.search(r"charset=([\w-]+)", r.headers.get("Content-Type", ""), re.I)
    if m:
        enc = m.group(1).lower()
        try:
            return raw.decode(enc)
        except Exception:
            pass
    for enc in ("utf-8", "gb18030", "big5"):
        try:
            return raw.decode(enc)
        except Exception:
            continue
    return raw.decode("utf-8", errors="replace")


def extract_original(pub_url):
    """Generically extract article body text + lead image from a publisher page."""
    r = fetch_response(pub_url)
    if r is None or r.status_code != 200:
        return "", []
    txt = decode_body(r)
    soup = BeautifulSoup(txt, "html.parser")
    # drop noisy tags
    for tag in soup(["script", "style", "noscript", "header", "footer", "nav", "aside", "form"]):
        tag.decompose()

    root = None
    for sel in ARTICLE_SELECTORS:
        node = soup.select_one(sel)
        if node:
            root = node
            break
    if root is None:
        root = soup.body or soup

    paras = []
    for p in root.find_all("p"):
        t = p.get_text(" ", strip=True)
        if len(t) > 30:
            paras.append(t)
    content = "\n\n".join(paras)

    # lead image
    images = []
    og = soup.find("meta", property="og:image")
    if og and og.get("content"):
        images.append({"url": og["content"], "caption": ""})
    for img in root.find_all("img")[:6]:
        src = img.get("src") or img.get("data-src") or img.get("data-lazy-src")
        if src and src.startswith("http") and "logo" not in src.lower():
            if not any(i["url"] == src for i in images):
                images.append({"url": src, "caption": ""})

    return content, images


# ----------------------------------------------------------------------------
# Output
# ----------------------------------------------------------------------------
def atomic_save(data, path="data/新闻/gnews_collection.json"):
    """原子保护性写入 JSON：先写 path.tmp（flush+fsync），再 os.replace 改名覆盖，
    杜绝进程中断/断电导致半截 JSON 损坏。逐篇（线程池每返回一个结果）实时调用。"""
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
    ap = argparse.ArgumentParser(description="Google News RSS crawler")
    ap.add_argument("--lang", default="zh", choices=["zh", "en"],
                    help="locale shortcut: zh (zh-CN/中国) or en (en-US)")
    ap.add_argument("--hl", default=None)
    ap.add_argument("--gl", default=None)
    ap.add_argument("--ceid", default=None)
    ap.add_argument("--query", action="append", default=None,
                    help="add a search feed (repeatable), e.g. --query iran")
    ap.add_argument("--limit", type=int, default=None,
                    help="max articles to crawl (default: all discovered)")
    ap.add_argument("--metadata-only", action="store_true",
                    help="OPT-OUT: only collect RSS metadata, do NOT fetch original "
                         "article bodies (default is to fetch bodies via browser)")
    ap.add_argument("--concurrency", type=int, default=4)
    ap.add_argument("--delay", type=float, default=0.2)
    ap.add_argument("--out", default="data/新闻/gnews_collection.json")
    args = ap.parse_args()

    if args.lang == "zh":
        locale = (args.hl or DEFAULT_HL, args.gl or DEFAULT_GL, args.ceid or DEFAULT_CEID)
    else:
        locale = (args.hl or "en-US", args.gl or "US", args.ceid or "US:en")

    print(f"[*] Google News crawler | locale hl={locale[0]} gl={locale[1]} ceid={locale[2]}")
    fetch_bodies = not args.metadata_only
    print(f"[*] mode={'RSS metadata only' if args.metadata_only else 'FULL (fetch original bodies)'}")

    found = discover(locale, queries=args.query, limit=args.limit)
    print(f"[*] discovered {len(found)} unique articles")

    articles = list(found.values())

    if fetch_bodies:
        import concurrent.futures as cf
        import threading
        _tls = threading.local()

        def get_browser():
            if getattr(_tls, "browser", None) is None:
                from playwright.sync_api import sync_playwright
                _tls.pw = sync_playwright().start()
                _tls.browser = _tls.pw.chromium.launch(headless=True)
            return _tls.browser

        def do(it):
            pub = resolve_via_browser(it["url"], get_browser())
            if not pub:
                it["content_source"] = "unreachable"
                return it
            content, images = extract_original(pub)
            it["original_url"] = pub
            if len(content) > 200:
                it["content"] = content
                it["images"] = images
                it["content_source"] = "full"
            else:
                it["content_source"] = "unreachable"
            return it

        out = []
        try:
            with cf.ThreadPoolExecutor(max_workers=args.concurrency) as ex:
                for res in ex.map(do, articles):
                    out.append(res)
                    if args.delay:
                        time.sleep(args.delay)
                    # 实时原子落盘：线程池每返回一个结果就存一次
                    collection = {
                        "source_site": "news.google.com",
                        "crawler": "gnews_crawler.py",
                        "locale": {"hl": locale[0], "gl": locale[1], "ceid": locale[2]},
                        "crawled_at": datetime.now(timezone.utc).isoformat(),
                        "mode": "rss_metadata" if args.metadata_only else "full",
                        "count": len(out),
                        "articles": out,
                    }
                    atomic_save(collection, args.out)
        except (KeyboardInterrupt, Exception) as exc:
            collection = {
                "source_site": "news.google.com",
                "crawler": "gnews_crawler.py",
                "locale": {"hl": locale[0], "gl": locale[1], "ceid": locale[2]},
                "crawled_at": datetime.now(timezone.utc).isoformat(),
                "mode": "rss_metadata" if args.metadata_only else "full",
                "count": len(out),
                "articles": out,
            }
            atomic_save(collection, args.out)
            print(f"\n[!] 已实时保存至当前进度（{len(out)} 篇）→ {args.out}")
            raise
        articles = out

    collection = {
        "source_site": "news.google.com",
        "crawler": "gnews_crawler.py",
        "locale": {"hl": locale[0], "gl": locale[1], "ceid": locale[2]},
        "crawled_at": datetime.now(timezone.utc).isoformat(),
        "mode": "rss_metadata" if args.metadata_only else "full",
        "count": len(articles),
        "articles": articles,
    }
    atomic_save(collection, args.out)

    from collections import Counter
    cs = Counter(a["content_source"] for a in articles)
    print(f"[*] wrote {args.out} | {len(articles)} articles | content_source={dict(cs)}")


if __name__ == "__main__":
    main()
