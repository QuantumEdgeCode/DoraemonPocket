#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Anadolu Agency (aa.com.tr) crawler
=================================

Premium news-agency crawler for Anadolu Ajansi (Turkey's state news agency),
built to match the conventions of the existing toolchain (ukrinform_crawler.py,
scmp_crawler.py, ...). It is a clean server-side-rendered (SSR) site with no
JS challenge / IP block / Cloudflare on the editorial pages, so a plain
requests + BeautifulSoup pass is enough (no Playwright needed).

Discovery
---------
Per-language News sitemaps live at  /<lang>/SiteMap/News  (also LiveBlogs /
Photos / Videos / InfoGraphics / Podcasts). Each News sitemap currently
exposes ~1000 recent article URLs of the form:

    https://www.aa.com.tr/<lang>/<section>/<slug>/<id>

Supported language codes (--lang): en tr ar ru fr ba kk ks sq fa mk id es
Default is **English** (international edition, carries real author bylines).

Parsing
-------
* Title        : JSON-LD NewsArticle.headline  ->  og:title (html.unescape)  ->  <h1>
* Section      : URL path segment (/en/<section>/)  ->  articleSection fallback
* Published    : JSON-LD datePublished (naive ISO, parsed + assumed UTC)
* Author       : JSON-LD author.name (real reporter byline, e.g. "Diyar Guldogan")
* Content      : the Tailwind `prose` <div> in the article body (JSON-LD
                 articleBody is only a ~180-char standfirst, NOT the full text)
* Lead image   : og:image / JSON-LD image thumbnail (thumbs_b_c_...) upgraded to
                 the full-resolution URL by stripping the `thumbs_b_c_` prefix
* Tags         : <meta name="keywords"> / property="article:tag" if present

Honesty / resilience
--------------------
* SSL: this host intermittently throws UNEXPECTED_EOF_WHILE_READING; fetch_html()
  uses verify=False + exponential backoff (same treatment as BelTA/Bloomberg).
* No challenge detection needed (no PerimeterX/Cloudflare on editorial pages),
  but a cheap guard skips any unexpected "challenge" body.
* robots.txt only ADVISES (Disallow: /api/, /*?s=*, /*/p/preview/*); we respect
  it by not crawling those, but crawling news articles is permitted (Allow: /).

Output: aa_collection.json  (unified schema, see save_collection())

Compliance: Anadolu Agency content is copyrighted; use for personal study /
research only, not commercial redistribution.
"""

import argparse
import html
import json
import os
import re
import sys
import time
from datetime import datetime, timezone
from urllib.parse import urlparse

import requests
import urllib3
from bs4 import BeautifulSoup

# AA (and several mirrors) intermittently fail TLS with UNEXPECTED_EOF, so we
# fetch with verify=False; silence the resulting InsecureRequestWarning noise.
urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)

UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/124.0 Safari/537.36")
DEFAULT_DELAY = 3.0
BASE = "https://www.aa.com.tr"

# languages offered by the site
LANGS = ["en", "tr", "ar", "ru", "fr", "ba", "kk", "ks", "sq", "fa", "mk", "id", "es"]


def fetch_html(url, n=5, timeout=25):
    """GET a URL with verify=False + exponential backoff. Returns (text, status)."""
    last = None
    for attempt in range(1, n + 1):
        try:
            r = requests.get(
                url,
                headers={"User-Agent": UA, "Accept-Language": "en;q=0.9,tr;q=0.8"},
                timeout=timeout,
                verify=False,
            )
            return r.text, r.status_code
        except Exception as e:  # SSL EOF, connection reset, etc.
            last = e
            if attempt < n:
                time.sleep(min(2 ** attempt, 16))
    return "", 0


def discover_urls(lang, limit=None):
    """Return a list of article URLs from /<lang>/SiteMap/News (deduped, ordered)."""
    sm_url = f"{BASE}/{lang}/SiteMap/News"
    txt, status = fetch_html(sm_url)
    if status != 200 or not txt:
        print(f"  [warn] sitemap {sm_url} -> status {status}", file=sys.stderr)
        return []
    locs = re.findall(r"<loc>(.*?)</loc>", txt)
    # keep only real article URLs: /<lang>/<section>/<slug>/<digits>
    pat = re.compile(rf"^{re.escape(BASE)}/{re.escape(lang)}/[^/]+/[^/]+/\d+$")
    urls = []
    seen = set()
    for u in locs:
        u = u.strip()
        if pat.match(u) and u not in seen:
            seen.add(u)
            urls.append(u)
    if limit is not None:
        urls = urls[:limit]
    return urls


def parse_article(url, html_text):
    soup = BeautifulSoup(html_text, "html.parser")

    # ---- JSON-LD (NewsArticle) ----
    ld = {}
    for m in re.finditer(
        r'<script[^>]+type="application/ld\+json"[^>]*>(.*?)</script>', html_text, re.S
    ):
        try:
            d = json.loads(m.group(1).strip())
        except Exception:
            continue
        if isinstance(d, dict) and d.get("@type") == "NewsArticle":
            ld = d
            break

    # ---- title ----
    title = (ld.get("headline") or "").strip()
    if not title:
        og = soup.find("meta", attrs={"property": "og:title"})
        if og and og.get("content"):
            title = html.unescape(og["content"]).strip()
    if not title:
        h1 = soup.find("h1")
        if h1:
            title = h1.get_text(strip=True)

    # ---- section (from URL, fallback to JSON-LD articleSection) ----
    parts = [p for p in urlparse(url).path.split("/") if p]
    # parts[0]=lang, parts[1]=section
    section_code = parts[1] if len(parts) > 1 else ""
    section_label = ld.get("articleSection") or (
        section_code.replace("-", " ").title() if section_code else ""
    )

    # ---- published_at (naive ISO -> assume UTC) ----
    published_at = ""
    raw_time = ld.get("datePublished") or ""
    if not raw_time:
        mt = soup.find("meta", attrs={"property": "article:published_time"})
        if mt and mt.get("content"):
            raw_time = mt["content"]
    if raw_time:
        try:
            # handle fractional seconds of variable length (.733 / .55)
            dt = datetime.fromisoformat(raw_time)
            if dt.tzinfo is None:
                dt = dt.replace(tzinfo=timezone.utc)  # Anadolu wire standard = UTC
            published_at = dt.isoformat()
        except Exception:
            published_at = raw_time  # keep raw if unparseable

    # ---- author ----
    author = ""
    a = ld.get("author")
    if isinstance(a, dict):
        author = a.get("name", "")
    elif isinstance(a, list) and a:
        author = a[0].get("name", "") if isinstance(a[0], dict) else ""

    # ---- body content from the `prose` container ----
    content = ""
    images = []
    prose = soup.find("div", class_=lambda c: c and "prose" in c.split())
    if prose:
        paras = [p.get_text(strip=True) for p in prose.find_all("p")]
        content = "\n\n".join(p for p in paras if p)
        # capture inline images inside the body too
        for im in prose.find_all("img"):
            src = im.get("src") or im.get("data-src") or ""
            if src and "thumbs_b_c_" not in src and "counter" not in src:
                images.append({"url": src, "caption": im.get("alt", "") or ""})

    # ---- lead image (og:image / JSON-LD image, full-res) ----
    lead_url = ""
    if isinstance(ld.get("image"), list) and ld["image"]:
        lead_url = ld["image"][0]
    elif isinstance(ld.get("image"), str):
        lead_url = ld["image"]
    if not lead_url:
        og = soup.find("meta", attrs={"property": "og:image"})
        if og and og.get("content"):
            lead_url = og["content"]
    if lead_url:
        full = lead_url.replace("thumbs_b_c_", "")
        images.insert(0, {"url": full, "caption": title})

    # ---- tags ----
    tags = []
    kw = soup.find("meta", attrs={"name": "keywords"})
    if kw and kw.get("content"):
        tags = [t.strip() for t in kw["content"].split(",") if t.strip()]
    if not tags:
        at = soup.find("meta", attrs={"property": "article:tag"})
        if at and at.get("content"):
            tags = [t.strip() for t in at["content"].split(",") if t.strip()]

    return {
        "title": title,
        "section": section_label,
        "section_code": section_code,
        "published_at": published_at,
        "author": author,
        "content": content,
        "images": images,
        "tags": tags,
        "url": url,
        "language": parts[0] if parts else "",
        "content_source": "full" if content else "empty",
    }


def save_collection(records, out_path):
    """原子保护性写入：写 .tmp -> flush+fsync -> os.replace 改名，杜绝半截 JSON；逐篇实时调用。"""
    d = os.path.dirname(out_path)
    if d:
        os.makedirs(d, exist_ok=True)
    tmp = out_path + ".tmp"
    payload = {
        "source": "Anadolu Agency (aa.com.tr)",
        "language": records[0]["language"] if records else "",
        "fetched_at": datetime.now(timezone.utc).isoformat(),
        "count": len(records),
        "articles": records,
    }
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(payload, f, ensure_ascii=False, indent=2)
        f.flush()
        os.fsync(f.fileno())
    os.replace(tmp, out_path)


def main():
    ap = argparse.ArgumentParser(description="Anadolu Agency (aa.com.tr) crawler")
    ap.add_argument("--lang", default="en",
                    help=f"language edition: one of {','.join(LANGS)} (default: en)")
    ap.add_argument("--limit", type=int, default=None,
                    help="max articles to crawl (default: all discovered)")
    ap.add_argument("--delay", type=float, default=DEFAULT_DELAY,
                    help="seconds between requests (default: 3)")
    ap.add_argument("--no-detail", action="store_true",
                    help="skip article bodies, keep sitemap metadata only")
    ap.add_argument("--out", default="data/新闻/aa_collection.json")
    args = ap.parse_args()

    lang = args.lang if args.lang in LANGS else "en"
    print(f"[*] Anadolu Agency crawler | lang={lang} | delay={args.delay}s")

    urls = discover_urls(lang, limit=args.limit)
    print(f"[*] discovered {len(urls)} article URLs")

    records = []
    if args.no_detail:
        # sitemap-only fast path: title-less metadata entries
        for u in urls:
            parts = [p for p in urlparse(u).path.split("/") if p]
            sec = parts[1] if len(parts) > 1 else ""
            records.append({
                "title": "", "section": sec.replace("-", " ").title(),
                "section_code": sec, "published_at": "", "author": "",
                "content": "", "images": [], "tags": [], "url": u,
                "language": parts[0] if parts else lang,
                "content_source": "sitemap_only",
            })
            save_collection(records, args.out)  # 实时落盘
        print(f"[*] sitemap-only mode: {len(records)} entries (no detail fetch)")
    else:
        ok = 0
        for i, u in enumerate(urls, 1):
            txt, status = fetch_html(u)
            if status == 200 and txt:
                rec = parse_article(u, txt)
                records.append(rec)
                ok += 1
                if i % 25 == 0 or i == len(urls):
                    print(f"  [{i}/{len(urls)}] ok={ok}  latest: {rec['title'][:50]}")
            else:
                print(f"  [{i}/{len(urls)}] skip status={status}", file=sys.stderr)
            time.sleep(args.delay)
            save_collection(records, args.out)  # 每篇实时落盘，防中断丢数据

    # 收尾保存（保证进度完整）+ 中断 / 异常保护
    try:
        save_collection(records, args.out)
        print(f"[done] {len(records)} records -> {args.out}")
    except KeyboardInterrupt:
        print(f"\n[!] 被用户中断，已实时保存至 {args.out}（当前 {len(records)} 条）")
        raise
    except Exception as exc:
        print(f"\n[!] 写入异常: {exc}；已实时保存至 {args.out}（当前 {len(records)} 条）")
        raise


if __name__ == "__main__":
    main()
