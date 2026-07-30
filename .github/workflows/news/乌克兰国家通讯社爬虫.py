#!/usr/bin/env python3
"""
Ukrinform (Ukrainian National News Agency) crawler
==================================================
SSR site (clean HTML + JSON-LD NewsArticle). No JS challenge / no IP block
observed from this environment. Dual-engine: static requests (primary) ->
Playwright (fallback, rarely triggered).

Discovery:
  - sitemap.xml is a sitemap INDEX of 135 weekly sub-sitemaps
    (currentweek.xml + /sitemap/2026/NN.xml by ISO week).
  - last.xml = curated recent pool (~171 articles).
  Both are reachable. We merge last.xml + currentweek.xml + the newest
  --weeks weekly files (default weeks=1) and dedupe by URL.

Article URL format:  /rubric-<code>/<id>-<slug>.html
  e.g. /rubric-ato/4149008-russian-forces-injure-six-residents.html
  rubric codes: ato, polytics, economy, sports, vidbudova, defense,
                society, crime, emergencies, ...

Body container:  div.newsText  (clean <p> paragraphs). The page appends a
  "Read also:" inline promo block at the end of the body -> trimmed.

Metadata:
  - title    : JSON-LD headline (cleanest)
  - time     : JSON-LD datePublished (already +03:00 Kyiv)
  - author   : JSON-LD author.name (agency -> "Ukrinform")
  - image    : og:image / JSON-LD image (static.ukrinform.com)
  - section  : rubric code from URL, mapped to friendly name

Usage:
  python ukrinform_crawler.py                 # default: last.xml + currentweek + 1 week
  python ukrinform_crawler.py --limit 200     # cap articles
  python ukrinform_crawler.py --no-detail     # sitemap-only metadata
  python ukrinform_crawler.py --weeks 4       # backfill recent N weeks
  python ukrinform_crawler.py --delay 3
  python ukrinform_crawler.py --cookie "k=v;..."   # optional
  python ukrinform_crawler.py --playwright    # force browser engine

Compliance: Ukrinform content is copyrighted; personal study/research only,
no commercial redistribution.
"""
import argparse
import html as _html
import json
import re
import sys
import time
import os
from bs4 import BeautifulSoup
from datetime import datetime, timezone, timedelta
from urllib.parse import urljoin, urlparse

import requests

# ---- suppress noisy TLS warnings (we use verify=False for flaky SSL) ----
import urllib3
urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)

BASE = "https://www.ukrinform.net"
UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/124.0 Safari/537.36")
HEADERS = {
    "User-Agent": UA,
    "Accept-Language": "en;q=0.9,uk;q=0.8,ru;q=0.7",
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
}

# rubric code -> friendly section name (Ukrainian agency taxonomy)
RUBRIC_MAP = {
    "ato": "War",
    "polytics": "Politics",
    "economy": "Economy",
    "sports": "Sports",
    "vidbudova": "Reconstruction",
    "defense": "Defense",
    "society": "Society",
    "crime": "Crime",
    "emergencies": "Emergencies",
}

# content noise markers appended to body
READ_ALSO_RE = re.compile(r"\s*read also[:\s]", re.IGNORECASE)

SSL_RETRIES = 5
BACKOFF = 2.0

# 熔断（circuit-breaker）：防止爬虫进入「持续获取失败」死循环 = 无效执行
CONSECUTIVE_FAIL_LIMIT = 20   # 连续 20 条获取失败（正文为空/被跳过/解析异常）→ 强制终止
FAIL_WINDOW_SEC = 600         # 持续 10 分钟（600s）连续失败（距上次成功）→ 强制终止


def fetch_html(url, session, cookie=None, timeout=25):
    """Fetch HTML with SSL-retry + exponential backoff. Returns (text, status)."""
    headers = dict(HEADERS)
    if cookie:
        headers["Cookie"] = cookie
    last_err = None
    for i in range(SSL_RETRIES):
        try:
            r = session.get(url, headers=headers, timeout=timeout, verify=False)
            if r.status_code == 200:
                return r.text, 200
            # 403/404/410 -> not worth retrying
            if r.status_code in (403, 404, 410, 401):
                return r.text, r.status_code
            last_err = f"HTTP {r.status_code}"
        except Exception as e:
            last_err = repr(e)
        time.sleep(BACKOFF * (i + 1))
    return "", -1



def discover_urls(session, weeks, cookie, verbose=True):
    """Return list of unique article URLs from sitemaps."""
    subs = []
    # 1) index
    idx_text, _ = fetch_html(urljoin(BASE, "/sitemap.xml"), session, cookie)
    if idx_text and "<sitemap>" in idx_text:
        subs += re.findall(r"<loc>(.*?)</loc>", idx_text)
    # 2) last.xml (curated recent)
    last_text, _ = fetch_html(urljoin(BASE, "/sitemap/last.xml"), session, cookie)
    if last_text:
        last_locs = re.findall(r"<loc>(.*?)</loc>", last_text)
        if verbose:
            print(f"[discover] last.xml -> {len(last_locs)} locs", file=sys.stderr)
    else:
        last_locs = []
    # pick currentweek + newest `weeks` weekly files from index
    weekly = [s for s in subs if re.search(r"/sitemap/\d{4}/\d+\.xml", s)]
    weekly_sorted = sorted(weekly, reverse=True)  # higher ISO-week first
    chosen = [s for s in subs if s.endswith("currentweek.xml")]
    chosen += weekly_sorted[: max(0, weeks)]
    if verbose:
        print(f"[discover] index subs={len(subs)} | weekly={len(weekly)} "
              f"| chosen weeks={len(chosen)}", file=sys.stderr)

    urls = []
    seen = set()
    pools = chosen + (["__last__"] if last_locs else [])
    for p in pools:
        if p == "__last__":
            locs = last_locs
        else:
            txt, _ = fetch_html(p, session, cookie)
            if not txt:
                continue
            locs = re.findall(r"<loc>(.*?)</loc>", txt)
        for loc in locs:
            loc = loc.strip()
            if not is_article_url(loc):
                continue
            if loc in seen:
                continue
            seen.add(loc)
            urls.append(loc)
    if verbose:
        print(f"[discover] total unique article URLs: {len(urls)}", file=sys.stderr)
    return urls


def is_article_url(url):
    """Ukrinform article: /rubric-<code>/<id>-<slug>.html"""
    if "ukrinform.net" not in url:
        return False
    return bool(re.search(r"/rubric-[a-z]+/\d+-[a-z0-9-]+\.html$", url))


def parse_detail(html, url):
    """Extract structured article data from detail HTML."""
    soup = BeautifulSoup(html, "html.parser")
    data = {
        "title": None, "section": None, "section_code": None,
        "published_at": None, "author": None, "content": "",
        "images": [], "tags": [], "url": url,
    }

    # ---- section from URL rubric code ----
    m = re.search(r"/rubric-([a-z]+)/", url)
    code = m.group(1) if m else None
    data["section_code"] = code
    data["section"] = RUBRIC_MAP.get(code, (code.capitalize() if code else None))

    # ---- JSON-LD ----
    ld = None
    m = re.search(r'<script[^>]+type="application/ld\+json"[^>]*>(.*?)</script>',
                  html, re.S)
    if m:
        try:
            ld = json.loads(m.group(1))
        except Exception:
            ld = None
    if ld:
        if not data["title"]:
            data["title"] = _html.unescape(ld.get("headline") or "")
        dp = ld.get("datePublished")
        if dp:
            data["published_at"] = normalize_time(dp)
        auth = ld.get("author")
        if isinstance(auth, dict):
            data["author"] = auth.get("name")
        elif isinstance(auth, list) and auth:
            data["author"] = auth[0].get("name") if isinstance(auth[0], dict) else None
        imgs = ld.get("image")
        if isinstance(imgs, list):
            data["images"] = [{"url": u, "caption": ""} for u in imgs if u]
        elif isinstance(imgs, str):
            data["images"] = [{"url": imgs, "caption": ""}]

    # ---- title fallback ----
    if not data["title"]:
        h1 = soup.find("h1")
        if h1:
            data["title"] = _html.unescape(h1.get_text(strip=True))
    if not data["title"]:
        og = soup.find("meta", attrs={"property": "og:title"})
        if og and og.get("content"):
            data["title"] = _html.unescape(og["content"].strip())

    # ---- author fallback ----
    if not data["author"]:
        data["author"] = "Ukrinform"

    # ---- time fallback (meta) ----
    if not data["published_at"]:
        for prop in ("article:published_time", "article:modified_time"):
            mt = soup.find("meta", attrs={"property": prop})
            if mt and mt.get("content"):
                data["published_at"] = normalize_time(mt["content"])
                break

    # ---- body: div.newsText ----
    body_text = ""
    nt = soup.find("div", class_="newsText")
    if nt:
        parts = []
        for p in nt.find_all("p"):
            t = p.get_text(" ", strip=True)
            if not t:
                continue
            if READ_ALSO_RE.search(t):
                break  # truncate at "Read also" promo
            parts.append(t)
        body_text = "\n\n".join(parts)
    # fallback: generic article container
    if not body_text:
        for cls in ("newsHolderContainer", "articleBody", "article-body", "content"):
            el = soup.find("div", class_=cls)
            if el:
                body_text = el.get_text("\n\n", strip=True)
                break
    # fallback: <article>
    if not body_text:
        art = soup.find("article")
        if art:
            body_text = art.get_text("\n\n", strip=True)
    data["content"] = body_text.strip()

    # ---- image fallback (og:image) ----
    if not data["images"]:
        og = soup.find("meta", attrs={"property": "og:image"})
        if og and og.get("content"):
            data["images"] = [{"url": og["content"], "caption": ""}]

    # ---- tags ----
    kw = soup.find("meta", attrs={"name": "keywords"})
    if kw and kw.get("content"):
        data["tags"] = [t.strip() for t in kw["content"].split(",") if t.strip()]
    if not data["tags"] and ld and ld.get("keywords"):
        data["tags"] = ld["keywords"] if isinstance(ld["keywords"], list) else [ld["keywords"]]

    return data


def normalize_time(s):
    """Parse ISO / RFC822 time to tz-aware ISO string. Ukrinform uses
    +03:00 (Kyiv). Never do datetime+Nh hacks."""
    s = s.strip()
    # try ISO
    try:
        dt = datetime.fromisoformat(s)
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone(timedelta(hours=3)))
        return dt.isoformat()
    except Exception:
        pass
    # try RFC822
    try:
        from email.utils import parsedate_to_datetime
        dt = parsedate_to_datetime(s)
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone(timedelta(hours=3)))
        return dt.isoformat()
    except Exception:
        pass
    return s  # leave as-is if unparseable


def atomic_save(records, path=None, source="Ukrinform"):
    """原子写入：写 .tmp → flush → fsync → os.replace，防进程中断留半截 JSON。"""
    if path is None:
        path = "data/新闻/ukrinform_collection.json"
    os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(records, f, ensure_ascii=False, indent=2)
        f.flush()
        os.fsync(f.fileno())
    os.replace(tmp, path)


def main():
    ap = argparse.ArgumentParser(description="Ukrinform crawler")
    ap.add_argument("--limit", type=int, default=0, help="max articles (0=all)")
    ap.add_argument("--no-detail", action="store_true",
                    help="sitemap-only metadata (skip detail fetch)")
    ap.add_argument("--weeks", type=int, default=1,
                    help="recent weekly sub-sitemaps to include (default 1)")
    ap.add_argument("--delay", type=float, default=2.0, help="delay between requests")
    ap.add_argument("--cookie", type=str, default=None, help="optional Cookie header")
    ap.add_argument("--playwright", action="store_true", help="force Playwright engine")
    ap.add_argument("--out", type=str, default="data/新闻/ukrinform_collection.json")
    ap.add_argument("--root", type=str, default=None,
                    help="override discovery root (single sitemap URL)")
    args = ap.parse_args()

    session = requests.Session()
    session.headers.update(HEADERS)

    # discovery
    if args.root:
        txt, _ = fetch_html(args.root, session, args.cookie)
        locs = re.findall(r"<loc>(.*?)</loc>", txt) if txt else []
        urls = [l.strip() for l in locs if is_article_url(l.strip())]
        print(f"[discover] root={args.root} -> {len(urls)} article URLs",
              file=sys.stderr)
    else:
        urls = discover_urls(session, args.weeks, args.cookie, verbose=True)

    if args.limit:
        urls = urls[: args.limit]

    out = {
        "source": "Ukrinform (Ukrainian National News Agency)",
        "collected_at": datetime.now(timezone.utc).isoformat(),
        "count": 0,
        "articles": [],
    }
    pw = None
    if args.playwright:
        try:
            from playwright.sync_api import sync_playwright
            pw = sync_playwright().start()
        except Exception as e:
            print(f"[warn] Playwright unavailable: {e}", file=sys.stderr)
            pw = None

    # 熔断状态
    fail_streak = 0
    last_success_ts = time.time()

    def maybe_break():
        """连续失败达上限，或持续 10 分钟连续失败 → 判定无效执行，强制终止。"""
        if fail_streak >= CONSECUTIVE_FAIL_LIMIT:
            print(f"\n[熔断] 连续 {fail_streak} 条获取失败（上限 {CONSECUTIVE_FAIL_LIMIT}），"
                  f"判定为无效执行，强制终止。", file=sys.stderr)
            atomic_save(out, args.out)
            sys.exit(3)
        if fail_streak > 0 and (time.time() - last_success_ts) >= FAIL_WINDOW_SEC:
            print(f"\n[熔断] 已持续 {int(time.time() - last_success_ts)}s 连续获取失败"
                  f"（上限 {FAIL_WINDOW_SEC}s），判定为无效执行，强制终止。",
                  file=sys.stderr)
            atomic_save(out, args.out)
            sys.exit(3)

    try:
        for i, url in enumerate(urls, 1):
            if args.no_detail:
                rec = {
                    "title": slug_title(url),
                    "section": None,
                    "section_code": (re.search(r"/rubric-([a-z]+)/", url) or [None, None])[1],
                    "published_at": None,
                    "author": None,
                    "content": "",
                    "content_source": "sitemap_only",
                    "images": [],
                    "tags": [],
                    "url": url,
                }
                out["articles"].append(rec)
                out["count"] = len(out["articles"])
                atomic_save(out, args.out)   # 实时落盘
                print(f"[{i}/{len(urls)}] (sitemap) {url}", file=sys.stderr)
                fail_streak = 0  # 元数据采集，非失败
                continue

            html, status = fetch_html(url, session, args.cookie)
            if status != 200 or not html:
                # try Playwright fallback
                if pw:
                    try:
                        b = pw.chromium.launch(headless=True, args=["--no-sandbox"])
                        pg = b.new_page()
                        if args.cookie:
                            pg.add_cookie({"name": "x", "value": "y",
                                           "url": BASE}) if False else None
                        pg.goto(url, timeout=30000, wait_until="domcontentloaded")
                        pg.wait_for_timeout(2000)
                        html = pg.content()
                        b.close()
                    except Exception as e:
                        print(f"  [pw fail] {url}: {e}", file=sys.stderr)
                        html = ""
                if not html:
                    print(f"  [skip] {url} (status {status})", file=sys.stderr)
                    fail_streak += 1
                    maybe_break()
                    if args.delay and i < len(urls):
                        time.sleep(args.delay)
                    continue
            try:
                rec = parse_detail(html, url)
                rec["content_source"] = "full"
                out["articles"].append(rec)
                out["count"] = len(out["articles"])
                atomic_save(out, args.out)   # 实时落盘（每篇）
                has_content = bool(rec["content"])
                print(f"[{i}/{len(urls)}] {rec['title'][:60] if rec['title'] else url} "
                      f"| {rec['section']} | {('Y' if has_content else 'N')}text "
                      f"| {len(rec['images'])}img", file=sys.stderr)
                if has_content or rec["images"]:
                    fail_streak = 0
                    last_success_ts = time.time()
                else:
                    fail_streak += 1
                    maybe_break()
            except Exception as e:
                print(f"  [parse err] {url}: {e}", file=sys.stderr)
                fail_streak += 1
                maybe_break()

            if args.delay and i < len(urls):
                time.sleep(args.delay)
    except (KeyboardInterrupt, Exception) as e:
        print(f"\n[中断] 已保存中断前进度（{len(out['articles'])} 篇）-> {args.out}",
              file=sys.stderr)
        atomic_save(out, args.out)
        raise

    if pw:
        try:
            pw.stop()
        except Exception:
            pass

    atomic_save(out, args.out)
    print(f"\n[done] wrote {len(out['articles'])} records -> {args.out}", file=sys.stderr)


def slug_title(url):
    """Fallback title from slug: /rubric-x/123-some-words.html -> Some Words"""
    m = re.search(r"/\d+-([a-z0-9-]+)\.html$", url)
    if not m:
        return url
    slug = m.group(1)
    return " ".join(w.capitalize() for w in slug.split("-") if not w.isdigit())


if __name__ == "__main__":
    main()
