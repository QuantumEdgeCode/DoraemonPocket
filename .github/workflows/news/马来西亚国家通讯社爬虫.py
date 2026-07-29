#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Bernama (bernama.com) crawler
=============================

Premium news-agency crawler for BERNAMA - the Malaysian National News Agency.
Built to match the conventions of the existing toolchain (aa_crawler.py,
ukrinform_crawler.py, ...).

Site profile
------------
* Classic **PHP server-side-rendered (SSR)** site (NOT Next.js, NOT JS-driven,
  no PerimeterX/Cloudflare challenge on editorial pages). A plain
  requests + BeautifulSoup pass is enough (no Playwright needed).
* Article URL (canonical, the ONLY one that resolves):
      https://www.bernama.com/en/news.php?id=<digits>
  NOTE: section-prefixed variants (e.g. /sports/news.php?id=...,
  /general/news.php?id=...) all return **404**, so the working URL is always
  `/en/news.php?id=<id>` and the section must be taken from the *discovery
  context* (which section/listing page linked the article), NOT the URL.
* Multi-language editions exist (en / bm / ar / tam under /<lang>/index.php)
  but the English editorial feed is the documented, stable one, so this
  crawler defaults to **English** (international edition).

Discovery (combined, all working /en/news.php?id= URLs)
------------------------------------------------------
1. English homepage  https://www.bernama.com/en/
2. Section index pages (the ones that return 200 with article links):
     general, business, politics, sports, world, region, crime_courts
   (lifestyle/others 404 -> excluded). Each listing yields the section name,
   which is attached to every article id found there (resolves the "no section
   in URL" problem).
3. Archive page  https://www.bernama.com/en/archive.php
4. RSS feed  https://www.bernama.com/en/rssfeed.php  (~10 latest items; the
   <title> is "Section : Headline" -> gives a second, authoritative section
   label + a lead-paragraph <description> used as a fallback excerpt).

Parsing
-------
* Title        : <h1>  ->  og:title  ->  <title>
* Section      : from discovery context (which listing linked it); RSS title
                 prefix "Section : " is preferred when present; fallback "".
* Published    : meta article:published_time  "DD/MM/YYYY HH:MM AM/PM"
                 -> parsed and tagged **Malaysia time UTC+8 (MYT)**.
* Author       : BERNAMA is a wire agency; default "Bernama". If the dateline
                 carries a sourced tag like "(Bernama-Yonhap)" it is recorded
                 as "Bernama (Yonhap)".
* Content      : the main column  `div.col-lg-8`  -> its <p> paragraphs.
                 (The site is Bootstrap-grid based; the article body lives in
                 the col-lg-8 main column; nav/footer sit outside it.)
* Lead image   : og:image  (https://www.bernama.com/storage/photos/...)
* Tags         : meta keywords

Honesty / resilience
--------------------
* SSL: this host intermittently throws UNEXPECTED_EOF_WHILE_READING; fetch_html()
  uses verify=False + exponential backoff (same treatment as BelTA/Bloomberg).
* The discovery pool for a classic agency portal is modest (~50-90 unique
  recent articles across all listings) - this is the honest, real size of the
  freely-linkable recent set, not a limitation of the crawler.
* robots.txt does not exist (404) -> no crawl-delay constraint; we still pace
  politely with --delay (default 2s).

Output: bernama_collection.json  (unified schema, see save_collection())

Compliance: BERNAMA content is copyrighted; use for personal study / research
only, not commercial redistribution.
"""

import argparse
import html
import json
import os
import re
import sys
import time
from datetime import datetime, timezone, timedelta
from urllib.parse import urlparse

import requests
import urllib3
from bs4 import BeautifulSoup

# Bernama is fetched with verify=False (SSL hardening); silence the
# InsecureRequestWarning noise in the logs.
urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)

UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/124.0 Safari/537.36")
DEFAULT_DELAY = 2.0
BASE = "https://www.bernama.com"
EN_HOME = f"{BASE}/en"
RSS_URL = f"{BASE}/en/rssfeed.php"
ARCHIVE_URL = f"{BASE}/en/archive.php"

# section index pages that actually return 200 with article links
SECTIONS = ["general", "business", "politics", "sports", "world", "region", "crime_courts"]

MYT = timezone(timedelta(hours=8))  # Malaysia Time (UTC+8)

# Broad link matcher: catches /en/news.php?id= , <section>/news.php?id= and bare
# news.php?id= (relative) links, capturing an optional section prefix. The
# prefix (general/business/politics/sports/world/region/crime_courts) is used
# ONLY to label the section; the URL is always normalised to the canonical
# /en/news.php?id=<id> (the only variant that reliably returns 200).
KNOWN_SECTIONS = set(SECTIONS) | {"lifestyle", "bernamabiz"}
LINK_RE = re.compile(r"(?:https?://[^'\"]*)?(?:/(?:en/)?([a-z_]+)/)?news\.php\?id=(\d+)")


def fetch_html(url, n=5, timeout=25):
    """GET a URL with verify=False + exponential backoff. Returns (text, status)."""
    last = None
    for attempt in range(1, n + 1):
        try:
            r = requests.get(
                url,
                headers={"User-Agent": UA, "Accept-Language": "en;q=0.9"},
                timeout=timeout,
                verify=False,
            )
            return r.text, r.status_code
        except Exception as e:  # SSL EOF, connection reset, etc.
            last = e
            if attempt < n:
                time.sleep(min(2 ** attempt, 16))
    return "", 0


def discover():
    """
    Return dict {id(int): {"url", "section"}} by combining homepage, section
    pages, archive and RSS. Section is attached from the discovery source.
    """
    found = {}

    def add(uid, section):
        uid = int(uid)
        if uid not in found:
            found[uid] = {"url": f"{EN_HOME}/news.php?id={uid}", "section": section}

    def scan(txt, default_section=""):
        if not txt:
            return
        for m in LINK_RE.finditer(txt):
            prefix, uid = m.group(1), m.group(2)
            sec = default_section
            if prefix and prefix != "en" and prefix in KNOWN_SECTIONS:
                sec = prefix
            add(uid, sec)

    # 1) homepage (relative section-prefixed links -> section labels captured)
    txt, st = fetch_html(EN_HOME)
    scan(txt)
    # 2) section pages (each listing also re-labels its own articles)
    for sec in SECTIONS:
        txt, st = fetch_html(f"{EN_HOME}/{sec}/")
        scan(txt, default_section=sec)
    # 3) archive
    txt, st = fetch_html(ARCHIVE_URL)
    scan(txt)
    # 4) RSS (also gives an authoritative section + lead excerpt)
    rss, st = fetch_html(RSS_URL)
    rss_excerpts = {}
    if st == 200 and rss:
        for item in re.finditer(r"<item>(.*?)</item>", rss, re.S):
            block = item.group(1)
            link = re.search(r"<link>(.*?)</link>", block)
            title = re.search(r"<title>(.*?)</title>", block, re.S)
            desc = re.search(r"<description>(.*?)</description>", block, re.S)
            if not link:
                continue
            lm = LINK_RE.search(link.group(1))
            if not lm:
                continue
            uid = int(lm.group(2))
            sec = ""
            if title:
                tt = html.unescape(title.group(1).strip())
                if " : " in tt:
                    sec = tt.split(" : ", 1)[0].strip()
            add(uid, sec)
            if desc:
                # description is HTML-escaped; decode once
                d = html.unescape(desc.group(1))
                d = re.sub(r"<[^>]+>", "", d).strip()  # strip <font>/<p> wrappers
                rss_excerpts[uid] = d
    return found, rss_excerpts


def parse_article(url, html_text, fallback_section="", rss_excerpt=""):
    soup = BeautifulSoup(html_text, "html.parser")

    # ---- title ----
    title = ""
    h1 = soup.find("h1")
    if h1:
        title = h1.get_text(strip=True)
    if not title:
        og = soup.find("meta", attrs={"property": "og:title"})
        if og and og.get("content"):
            title = html.unescape(og["content"]).strip()
    if not title and soup.title:
        t = soup.title.get_text(strip=True)
        title = t.replace("BERNAMA - ", "", 1).strip()

    # ---- section (prefer passed-in context) ----
    section = fallback_section or ""

    # ---- published_at: meta article:published_time "DD/MM/YYYY HH:MM AM/PM" ----
    published_at = ""
    mt = soup.find("meta", attrs={"property": "article:published_time"})
    raw = mt.get("content", "").strip() if mt else ""
    if not raw:
        # fallback: scan body for date pattern near the title
        body = soup.get_text(" ", strip=True)
        m = re.search(r"\b(\d{1,2}/\d{1,2}/\d{4})[ ]+(\d{1,2}:\d{2})[ ]*(AM|PM)?\b", body)
        if m:
            raw = f"{m.group(1)} {m.group(2)} {m.group(3) or ''}".strip()
    if raw:
        try:
            # normalise "29/07/2026 03:46 PM"
            s = raw.replace("  ", " ").strip()
            if re.search(r"\b(AM|PM)\b", s, re.I):
                dt = datetime.strptime(s, "%d/%m/%Y %I:%M %p")
            else:
                dt = datetime.strptime(s, "%d/%m/%Y %H:%M")
            dt = dt.replace(tzinfo=MYT)  # Malaysia = UTC+8
            published_at = dt.isoformat()
        except Exception:
            published_at = raw  # keep raw if unparseable

    # ---- author (wire agency; prefer sourced dateline tag) ----
    author = "Bernama"
    body0 = soup.get_text(" ", strip=True)
    m = re.search(r"\(Bernama[-\s]?([A-Za-z]+)\)", body0)
    if m:
        author = f"Bernama ({m.group(1).title()})"

    # ---- content: main column div.col-lg-8 -> <p> ----
    body_content = ""
    images = []
    main = soup.select_one("div.col-lg-8") or soup
    paras = []
    for p in main.find_all("p"):
        t = p.get_text(" ", strip=True)
        if not t:
            continue
        # skip obvious nav/footer noise that sometimes leaks into the column
        if t.startswith("©") or t.lower().startswith("disclaimer"):
            continue
        paras.append(t)
    body_content = "\n\n".join(paras)
    # inline images inside the main column
    for im in main.find_all("img"):
        src = im.get("src") or im.get("data-src") or ""
        if not src:
            continue
        low = src.lower()
        if any(k in low for k in ["/assets/img/", "bernama.png", "logo", "popup", "kerjaya", "fotobernama"]):
            continue
        images.append({"url": src, "caption": im.get("alt", "") or ""})

    # ---- lead image (og:image) ----
    lead_url = ""
    og = soup.find("meta", attrs={"property": "og:image"})
    if og and og.get("content"):
        lead_url = og["content"]
    if lead_url and not any(i["url"] == lead_url for i in images):
        images.insert(0, {"url": lead_url, "caption": title})

    # ---- tags ----
    tags = []
    kw = soup.find("meta", attrs={"name": "keywords"})
    if kw and kw.get("content"):
        tags = [t.strip() for t in kw["content"].split(",") if t.strip()]

    # honesty: if body fetch returned nothing usable, fall back to RSS excerpt
    content = body_content
    if not content and rss_excerpt:
        content = rss_excerpt

    return {
        "title": title,
        "section": section,
        "section_code": section,
        "published_at": published_at,
        "author": author,
        "content": content,
        "images": images,
        "tags": tags,
        "url": url,
        "language": "en",
        "content_source": "full" if body_content else ("rss_excerpt" if rss_excerpt else "empty"),
    }


def save_collection(records, out_path):
    payload = {
        "source": "BERNAMA - Malaysian National News Agency (bernama.com)",
        "language": "en",
        "fetched_at": datetime.now(timezone.utc).isoformat(),
        "count": len(records),
        "articles": records,
    }
    d = os.path.dirname(out_path)
    if d:
        os.makedirs(d, exist_ok=True)
    tmp = out_path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(payload, f, ensure_ascii=False, indent=2)
        f.flush()
        os.fsync(f.fileno())
    os.replace(tmp, out_path)


def main():
    ap = argparse.ArgumentParser(description="BERNAMA (bernama.com) crawler")
    ap.add_argument("--limit", type=int, default=None,
                    help="max articles to crawl (default: all discovered)")
    ap.add_argument("--delay", type=float, default=DEFAULT_DELAY,
                    help="seconds between requests (default: 2)")
    ap.add_argument("--no-detail", action="store_true",
                    help="skip article bodies, keep listing metadata only")
    ap.add_argument("--out", default="data/新闻/bernama_collection.json")
    args = ap.parse_args()

    print(f"[*] BERNAMA crawler | delay={args.delay}s")
    found, rss_excerpts = discover()
    print(f"[*] discovered {len(found)} unique article IDs")

    # order by id descending (newest first) for a sane default crawl
    order = sorted(found.keys(), reverse=True)
    if args.limit is not None:
        order = order[:args.limit]

    records = []
    try:
        if args.no_detail:
            for uid in order:
                rec = found[uid]
                records.append({
                    "title": "", "section": rec["section"], "section_code": rec["section"],
                    "published_at": "", "author": "", "content": "", "images": [],
                    "tags": [], "url": rec["url"], "language": "en",
                    "content_source": "listing_only",
                })
                save_collection(records, args.out)  # 实时落盘
            print(f"[*] listing-only mode: {len(records)} entries (no detail fetch)")
        else:
            ok = 0
            for i, uid in enumerate(order, 1):
                rec = found[uid]
                txt, status = fetch_html(rec["url"])
                if status == 200 and txt:
                    art = parse_article(rec["url"], txt,
                                        fallback_section=rec["section"],
                                        rss_excerpt=rss_excerpts.get(uid, ""))
                    records.append(art)
                    ok += 1
                    if i % 10 == 0 or i == len(order):
                        print(f"  [{i}/{len(order)}] ok={ok}  {art['title'][:50]} | {art['section']}")
                else:
                    print(f"  [{i}/{len(order)}] skip status={status}", file=sys.stderr)
                save_collection(records, args.out)  # 每篇实时落盘
                time.sleep(args.delay)

        save_collection(records, args.out)  # 收尾再存
        print(f"[done] {len(records)} records -> {args.out}")
    except (KeyboardInterrupt, Exception) as exc:
        save_collection(records, args.out)  # 中断前保存进度
        print(f"\n[interrupted] 已实时保存至当前进度（{len(records)} records）-> {args.out}")
        raise


if __name__ == "__main__":
    main()
