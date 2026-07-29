#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
IRNA Persian (خبرگزاری جمهوری اسلامی ایران - www.irna.ir) crawler
=================================================================

Crawls the Persian (Farsi) edition of the Islamic Republic News Agency (IRNA):
    https://www.irna.ir/

Site profile
------------
* Same Cloudflare architecture as the other IRNA editions: the whole editorial
  site sits behind a **Cloudflare "Transferring to the website..." interstitial**
  (a JS-advancing holding page, NOT a cookie-based IUAM — no `cf_clearance`
  cookie is ever issued). A plain `requests` GET only ever receives the static
  transfer shell, so **every article page must be fetched with a real browser
  (Playwright/Chromium)** that executes the JS and advances past the transfer.
* The XML **sitemaps are whitelisted and bypass Cloudflare**, so discovery uses
  fast `requests` against:
      https://www.irna.ir/sitemap/news/sitemap.xml   (~30 latest, news+photo)
      https://www.irna.ir/sitemap/all/sitemap.xml    (index of daily sub-sitemaps)
  Article URLs (with their Persian slug) are de-duplicated across both.
* IMPORTANT (differs from the Chinese edition): the Persian site REQUIRES the
  Persian slug in the URL. A slugless `/news/<id>/` URL is rejected with
  ERR_CONNECTION_CLOSED, so we keep the full sitemap URL verbatim
  (https://www.irna.ir/news/<id>/<persian-slug>).

Parsing (Playwright HTML -> BeautifulSoup)
-----------------------------------------
* Title     : <h1>  (clean; no "- IRNA" suffix in the body markup).
* Section   : the **active** item(s) in the main nav <ul> (class="active").
              The deepest active <a> text is the specific category; the full
              active path is also recorded. "خانه" (home) is dropped.
* Published : meta article:published_time  "2026-07-29T08:28:50Z"  (UTC).
              Converted to **Iran time UTC+3:30** (Iran has no DST since 2022),
              which matches the on-page local display (Persian calendar).
* Author    : wire agency -> "IRNA" (ایرنا). The byline is only an agency tag.
* Content   : the <article> element (isolates the body; header / related-news
              widgets live outside it, so they are excluded).
* Images    : only real photos on the irna.ir image CDN (img9.irna.ir /
              img.irna.ir). Logo, barcode, avatar and /resources/theme assets
              are filtered out (they leak into the <article> subtree).
* Tags      : meta keywords (if present).

Performance
-----------
* Async Playwright with a concurrency pool (default 4) so articles finish in a
  few minutes. Each navigation re-triggers the transfer (~4s), which is the
  unavoidable cost of no cookie / no JSON API.
* Sitemaps fetched with plain requests (fast, whitelisted XML).

Honesty / resilience
--------------------
* SSL on this host intermittently throws UNEXPECTED_EOF / CONNECTION_CLOSED;
  sitemap fetch uses verify=False + retries; article fetch retries per-nav.
* If an article page cannot be cleared after retries, it is skipped (logged),
  not faked.

Output: irna_fa_collection.json  (unified schema, see save_collection())

Compliance: IRNA content is copyrighted; use for personal study / research only,
not commercial redistribution.
"""

import argparse
import asyncio
import html
import json
import re
import os
import sys
import time
from datetime import datetime, timezone, timedelta
from urllib.parse import urlparse

import requests
import urllib3
from bs4 import BeautifulSoup
from playwright.async_api import async_playwright

# IRNA Persian edition sits behind Cloudflare; sitemap/feed fetches use
# verify=False, so silence the InsecureRequestWarning noise in the logs.
urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)

UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/124.0 Safari/537.36")

SITEMAP_NEWS = "https://www.irna.ir/sitemap/news/sitemap.xml"
SITEMAP_ALL = "https://www.irna.ir/sitemap/all/sitemap.xml"

TEHRAN = timezone(timedelta(hours=3, minutes=30))  # Iran Standard Time (UTC+3:30), no DST

# Transfer-page junk markers; presence => page not yet cleared
JUNK = ["Transferring to the website", "error-section", "showPage", "chrome-error"]

# Image noise to drop (Persian edition leaks these inside <article>)
IMG_SKIP = ["logo", "barcode", "avatar", "icon", "/resources/theme/", "favicon"]

DEFAULT_CONCURRENCY = 4
DEFAULT_DELAY = 0.4


def fetch_xml(url, n=4):
    """GET an XML sitemap (whitelisted by Cloudflare) with retries."""
    last = None
    for attempt in range(1, n + 1):
        try:
            r = requests.get(url, headers={"User-Agent": UA}, timeout=25, verify=False)
            if r.status_code == 200 and r.text:
                return r.text, r.status_code
            last = r.status_code
        except Exception as e:
            last = e
            time.sleep(min(2 ** attempt, 8))
    return "", last if isinstance(last, int) else 0


def discover(days=0):
    """Return dict {id(int): url}, de-duplicated (full sitemap URL kept verbatim).

    Always includes the latest `news` sitemap. If `days>0`, also follows the
    `all` sitemap *index* and fetches the most recent `days` daily sub-sitemaps
    for historical depth.
    """
    found = {}
    # 1) latest news feed (always)
    txt, st = fetch_xml(SITEMAP_NEWS)
    if st == 200 and txt:
        for loc in re.findall(r"<loc>(.*?)</loc>", txt, re.S):
            loc = loc.strip()
            m = re.search(r"/news/(\d+)/", loc)
            if m:
                uid = int(m.group(1))
                found.setdefault(uid, loc)
    # 2) optional historical backfill via the daily sub-sitemap index
    if days and days > 0:
        idx, st = fetch_xml(SITEMAP_ALL)
        if st == 200 and idx:
            subs = [s.strip() for s in re.findall(r"<loc>(.*?)</loc>", idx, re.S)]
            for sub in subs[:min(days, len(subs))]:
                stxt, sst = fetch_xml(sub)
                if sst == 200 and stxt:
                    for loc in re.findall(r"<loc>(.*?)</loc>", stxt, re.S):
                        loc = loc.strip()
                        m = re.search(r"/news/(\d+)/", loc)
                        if m:
                            uid = int(m.group(1))
                            found.setdefault(uid, loc)
    return found


def extract_section(soup):
    """Section = deepest active nav item; also return the full active path."""
    acts = soup.select("li.active")
    texts = []
    seen = set()
    for li in acts:
        a = li.find("a")
        if a:
            t = a.get_text(strip=True)
            if t and t != "خانه" and t not in seen:  # drop home + dedupe
                seen.add(t)
                texts.append(t)
    if not texts:
        return "", ""
    return texts[-1], " / ".join(texts)


def extract_time(soup):
    """Return ISO string tagged +03:30 (Iran), or '' if unparseable."""
    raw = ""
    mt = soup.find("meta", attrs={"property": "article:published_time"})
    if mt and mt.get("content"):
        raw = mt.get("content").strip()
    if raw:
        try:
            s = raw.replace("Z", "+00:00")
            dt = datetime.fromisoformat(s)
            if dt.tzinfo is None:
                dt = dt.replace(tzinfo=timezone.utc)
            dt = dt.astimezone(TEHRAN)
            return dt.isoformat()
        except Exception:
            pass
    return ""


def parse_article(html_text, url):
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
        title = soup.title.get_text(strip=True).strip()

    # ---- section ----
    section, section_path = extract_section(soup)

    # ---- published_at ----
    published_at = extract_time(soup)

    # ---- author (wire agency) ----
    author = "IRNA"

    # ---- content: <article> body ----
    article_el = soup.find("article") or soup
    paras = []
    for p in article_el.find_all("p"):
        t = p.get_text(" ", strip=True)
        if t:
            paras.append(t)
    content = "\n\n".join(paras)

    # ---- images: real photos on irna.ir CDN only ----
    images = []
    for im in article_el.find_all("img"):
        src = im.get("src") or im.get("data-src") or ""
        if not src:
            continue
        if src.startswith("//"):
            src = "https:" + src
        low = src.lower()
        if any(k in low for k in IMG_SKIP):
            continue
        # keep only irna.ir image CDN (drop relative / unrelated assets)
        if "irna.ir" not in low:
            continue
        images.append({"url": src, "caption": im.get("alt", "") or ""})
    # og:image lead (img9.irna.ir etc.)
    lead = ""
    og = soup.find("meta", attrs={"property": "og:image"})
    if og and og.get("content"):
        lead = og["content"]
        if lead.startswith("//"):
            lead = "https:" + lead
    if lead and "irna.ir" in lead.lower() and not any(i["url"] == lead for i in images):
        images.insert(0, {"url": lead, "caption": title})

    # ---- tags ----
    tags = []
    kw = soup.find("meta", attrs={"name": "keywords"})
    if kw and kw.get("content"):
        tags = [t.strip() for t in kw["content"].split(",") if t.strip()]

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
        "language": "fa",
        "content_source": "full" if content else "empty",
    }


async def fetch_one(page, url, max_wait=45):
    """Navigate to url (goto retried), wait for the transfer page to clear."""
    html = ""
    for attempt in range(4):
        try:
            await page.goto(url, wait_until="domcontentloaded", timeout=60000)
            break
        except Exception:
            await asyncio.sleep(2)
            continue
    waited = 0
    while waited < max_wait:
        try:
            h = await page.content()
        except Exception:
            h = ""
        if h and not any(j in h for j in JUNK):
            return h
        await asyncio.sleep(1.5)
        waited += 1.5
    return ""


async def crawl(urls, out_path, concurrency=DEFAULT_CONCURRENCY, delay=DEFAULT_DELAY):
    records = []
    sem = asyncio.Semaphore(concurrency)
    skipped = 0
    save_lock = asyncio.Lock()

    async with async_playwright() as p:
        browser = await p.chromium.launch(
            headless=True,
            args=["--no-sandbox", "--disable-dev-shm-usage",
                  "--disable-blink-features=AutomationControlled"],
        )
        ctx = await browser.new_context(user_agent=UA, locale="fa")

        async def worker(uid, url):
            nonlocal skipped
            async with sem:
                page = await ctx.new_page()
                try:
                    html = await fetch_one(page, url)
                    if html:
                        art = parse_article(html, url)
                        records.append(art)
                        if len(records) % 20 == 0 or len(records) == 1:
                            print(f"  [ok {len(records)}] {art['section']} | {art['title'][:40]}")
                        # 实时原子落盘（每 20 条或首条），防中断丢数据
                        if len(records) % 20 == 0 or len(records) == 1:
                            async with save_lock:
                                save_collection(records, out_path)
                    else:
                        skipped += 1
                        print(f"  [skip id={uid}] transfer not cleared", file=sys.stderr)
                finally:
                    await page.close()
                await asyncio.sleep(delay)

        tasks = [asyncio.create_task(worker(uid, url)) for uid, url in urls]
        await asyncio.gather(*tasks)
        await browser.close()

    return records, skipped


def save_collection(records, out_path):
    payload = {
        "source": "IRNA Persian - Islamic Republic News Agency (خبرگزاری جمهوری اسلامی ایران / www.irna.ir)",
        "language": "fa",
        "fetched_at": datetime.now(timezone.utc).isoformat(),
        "count": len(records),
        "articles": records,
    }
    # 原子写入：写 .tmp -> flush -> fsync -> os.replace，防止进程中断留半截 JSON
    os.makedirs(os.path.dirname(out_path) or ".", exist_ok=True)
    tmp = out_path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(payload, f, ensure_ascii=False, indent=2)
        f.flush()
        os.fsync(f.fileno())
    os.replace(tmp, out_path)


def main():
    ap = argparse.ArgumentParser(description="IRNA Persian (www.irna.ir) crawler")
    ap.add_argument("--limit", type=int, default=None,
                    help="max articles to crawl (default: all discovered)")
    ap.add_argument("--days", type=int, default=0,
                    help="follow the last N daily sub-sitemaps for history (default: 0)")
    ap.add_argument("--concurrency", type=int, default=DEFAULT_CONCURRENCY,
                    help="parallel browser pages (default: 4)")
    ap.add_argument("--delay", type=float, default=DEFAULT_DELAY,
                    help="seconds between dispatches (default: 0.4)")
    ap.add_argument("--out", default="data/新闻/irna_fa_collection.json")
    args = ap.parse_args()

    print(f"[*] IRNA Persian crawler | concurrency={args.concurrency} delay={args.delay}s days={args.days}")
    found = discover(days=args.days)
    print(f"[*] discovered {len(found)} unique article IDs")

    # sort by id desc (newest first)
    items = sorted(found.items(), key=lambda kv: kv[0], reverse=True)
    if args.limit is not None:
        items = items[:args.limit]

    try:
        records, skipped = asyncio.run(crawl(items, args.out, args.concurrency, args.delay))
    except KeyboardInterrupt:
        print("[interrupted] partial results already saved atomically", file=sys.stderr)
        return
    # 收尾再存一次（幂等），确保最终一致
    save_collection(records, args.out)
    print(f"[done] {len(records)} records ({skipped} skipped) -> {args.out}")


if __name__ == "__main__":
    main()
