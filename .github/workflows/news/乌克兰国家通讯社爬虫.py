#!/usr/bin/env python3
"""
Ukrinform (乌克兰国家通讯社) 爬虫
==================================
乌克兰官方通讯社，SSR 网站（干净 HTML + JSON-LD NewsArticle 元数据），
当前环境无 JS 挑战 / 无 IP 封禁。双引擎：静态 requests（主引擎）→
Playwright（回退引擎，极少触发）。

URL 发现：
  - sitemap.xml 是一个索引文件，包含约 135 个每周子 sitemap
    （currentweek.xml + /sitemap/2026/NN.xml 按 ISO 周编号）。
  - last.xml = 近期精选文章池（约 171 篇）。
  两者均可直接访问。合并 last.xml + currentweek.xml + 最新 --weeks 个
  周文件，按 URL 去重。

文章 URL 格式：  /rubric-<code>/<id>-<slug>.html
  例：/rubric-ato/4149008-russian-forces-injure-six-residents.html
  专栏代码：ato（战争）, polytics（政治）, economy（经济）, sports（体育）,
            vidbudova（重建）, defense（国防）, society（社会）,
            crime（犯罪）, emergencies（紧急事件）。

正文容器：  div.newsText（干净的 <p> 段落）。正文末尾追加了"Read also:"
           内联推广块，抓取时自动截断。

元数据来源：
  - title    : JSON-LD headline（最干净）
  - time     : JSON-LD datePublished（已含 +03:00 基辅时区）
  - author   : JSON-LD author.name（通讯社级 → "Ukrinform"）
  - image    : og:image / JSON-LD image（static.ukrinform.com）
  - section  : 从 URL 提取专栏代码，映射到友好名称

用法：
  python 乌克兰国家通讯社爬虫.py                 # 默认：last.xml + currentweek + 1 周
  python 乌克兰国家通讯社爬虫.py --limit 200     # 最多抓 200 篇
  python 乌克兰国家通讯社爬虫.py --no-detail     # 仅 sitemap 元数据（不抓正文）
  python 乌克兰国家通讯社爬虫.py --weeks 4       # 回填近 N 周
  python 乌克兰国家通讯社爬虫.py --delay 3
  python 乌克兰国家通讯社爬虫.py --cookie "k=v;..."   # 可选 Cookie
  python 乌克兰国家通讯社爬虫.py --playwright    # 强制使用浏览器引擎

合规说明：Ukrinform 内容受版权保护，仅限个人学习/研究使用，禁止商业再分发。
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

# ---- 抑制 SSL 警告（我们使用 verify=False 处理不稳定的 SSL） ----
import urllib3
urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)

# 站点根 URL
BASE = "https://www.ukrinform.net"

# 通用 User-Agent（伪装为 Chrome 浏览器）
UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/124.0 Safari/537.36")

# 通用请求头：UA + 语言偏好（英语为主，乌克兰语/俄语兜底）
HEADERS = {
    "User-Agent": UA,
    "Accept-Language": "en;q=0.9,uk;q=0.8,ru;q=0.7",
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
}

# 专栏代码 → 友好名称映射（乌克兰通讯社文章分类体系）
RUBRIC_MAP = {
    "ato": "War",          # 战争/军事行动
    "polytics": "Politics",    # 政治
    "economy": "Economy",      # 经济
    "sports": "Sports",        # 体育
    "vidbudova": "Reconstruction",  # 重建
    "defense": "Defense",      # 国防
    "society": "Society",      # 社会
    "crime": "Crime",          # 犯罪
    "emergencies": "Emergencies",   # 紧急事件
}

# 正文末尾推广块标记：匹配 "Read also:" 及其变体，从该处截断正文
READ_ALSO_RE = re.compile(r"\s*read also[:\s]", re.IGNORECASE)

# SSL 重试策略
SSL_RETRIES = 5      # 最大重试次数
BACKOFF = 2.0        # 退避基础秒数（每次重试 2×n 秒）


def fetch_html(url, session, cookie=None, timeout=25):
    """获取 HTML，含 SSL 重试 + 指数退避。

    参数：
        url     : 目标 URL
        session : requests.Session 实例（复用连接池）
        cookie  : 可选 Cookie 字符串（直接注入请求头）
        timeout : 单次请求超时（秒）

    返回：
        (html正文, HTTP状态码)  或  ("", -1) 表示全部重试失败

    策略：
        - HTTP 403/404/410/401 → 立即放弃（不值得重试）
        - 其他错误 → 退避重试（2s × 重试次数）
    """
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
    """从 sitemap 发现并去重文章 URL 列表。

    流程：
        1) 获取 sitemap.xml 索引，提取所有子 sitemap 地址
        2) 获取 last.xml（近期精选文章池）
        3) 从索引中筛选每周 sitemap，取最新 N 个（由 weeks 参数控制）
        4) 合并上述所有来源，按 URL 去重后返回

    参数：
        session : requests.Session
        weeks   : 包含最新 N 个周文件
        cookie  : 可选 Cookie
        verbose : 是否打印发现日志

    返回：
        去重后文章 URL 列表
    """
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
    """判断 URL 是否为有效文章链接。

    Ukrinform 文章标准格式：/rubric-<专栏代码>/<ID>-<slug>.html

    返回：
        True  = 是有效文章，False = 不是
    """
    if "ukrinform.net" not in url:
        return False
    return bool(re.search(r"/rubric-[a-z]+/\d+-[a-z0-9-]+\.html$", url))


def parse_detail(html, url):
    """从文章详情页 HTML 提取结构化数据。

    提取顺序（按优先级）：
        - 专栏：从 URL 路径中提取 rubric 代码，映射为友好名称
        - 标题：JSON-LD headline → h1 → og:title
        - 时间：JSON-LD datePublished → meta article:published_time
        - 作者：JSON-LD author.name → 默认 "Ukrinform"
        - 正文：div.newsText 内的 <p> 段落（遇 "Read also" 截断）→ 兜底 <article>
        - 图片：JSON-LD image → og:image
        - 标签：meta keywords → JSON-LD keywords

    参数：
        html : 文章页面 HTML 源码
        url  : 文章 URL（用于提取专栏代码）

    返回：
        结构化字典：{title, section, section_code, published_at, author,
                     content, images, tags, url}
    """
    soup = BeautifulSoup(html, "html.parser")
    data = {
        "title": None, "section": None, "section_code": None,
        "published_at": None, "author": None, "content": "",
        "images": [], "tags": [], "url": url,
    }

    # ---- 从 URL 提取专栏代码 ----
    m = re.search(r"/rubric-([a-z]+)/", url)
    code = m.group(1) if m else None
    data["section_code"] = code
    data["section"] = RUBRIC_MAP.get(code, (code.capitalize() if code else None))

    # ---- JSON-LD 结构化数据解析（最可靠） ----
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

    # ---- 标题兜底：h1 → og:title ----
    if not data["title"]:
        h1 = soup.find("h1")
        if h1:
            data["title"] = _html.unescape(h1.get_text(strip=True))
    if not data["title"]:
        og = soup.find("meta", attrs={"property": "og:title"})
        if og and og.get("content"):
            data["title"] = _html.unescape(og["content"].strip())

    # ---- 作者兜底（JSON-LD 无作者时默认署名）----
    if not data["author"]:
        data["author"] = "Ukrinform"

    # ---- 时间兜底：从 meta 标签提取 ----
    if not data["published_at"]:
        for prop in ("article:published_time", "article:modified_time"):
            mt = soup.find("meta", attrs={"property": prop})
            if mt and mt.get("content"):
                data["published_at"] = normalize_time(mt["content"])
                break

    # ---- 正文：div.newsText（主容器）----
    body_text = ""
    nt = soup.find("div", class_="newsText")
    if nt:
        parts = []
        for p in nt.find_all("p"):
            t = p.get_text(" ", strip=True)
            if not t:
                continue
            if READ_ALSO_RE.search(t):
                break  # 遇 "Read also" 推广块截断，去掉尾部无关内容
            parts.append(t)
        body_text = "\n\n".join(parts)
    # 兜底1：通用文章容器 class
    if not body_text:
        for cls in ("newsHolderContainer", "articleBody", "article-body", "content"):
            el = soup.find("div", class_=cls)
            if el:
                body_text = el.get_text("\n\n", strip=True)
                break
    # 兜底2：<article> 标签
    if not body_text:
        art = soup.find("article")
        if art:
            body_text = art.get_text("\n\n", strip=True)
    data["content"] = body_text.strip()

    # ---- 图片兜底：og:image ----
    if not data["images"]:
        og = soup.find("meta", attrs={"property": "og:image"})
        if og and og.get("content"):
            data["images"] = [{"url": og["content"], "caption": ""}]

    # ---- 标签：meta keywords → JSON-LD keywords ----
    kw = soup.find("meta", attrs={"name": "keywords"})
    if kw and kw.get("content"):
        data["tags"] = [t.strip() for t in kw["content"].split(",") if t.strip()]
    if not data["tags"] and ld and ld.get("keywords"):
        data["tags"] = ld["keywords"] if isinstance(ld["keywords"], list) else [ld["keywords"]]

    return data


def normalize_time(s):
    """解析 ISO / RFC822 时间字符串，返回含时区的 ISO 格式字符串。

    Ukrinform 使用 +03:00（基辅时区）。如果有歧义，不要用 datetime+Nh 的
    硬编码方式推算时区——尊重原文传递的信息。

    参数：
        s : 原始时间字符串

    返回：
        ISO 8601 格式时间字符串（含时区），无法解析时原样返回
    """
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
    """主入口：发现 URL → 逐篇获取/解析 → 实时原子落盘。

    流程：
        1) 解析命令行参数
        2) 从 sitemap 发现文章 URL 列表
        3) 逐篇 fetch + parse（或 --no-detail 仅 sitemap 元数据）
        4) 每篇实时 atomic_save（进程中断不丢进度）
        6) Playwright 回退（--playwright 参数启用）

    参数：（见 argparse 定义）
        --limit, --no-detail, --weeks, --delay, --cookie, --playwright, --out, --root
    """
    ap = argparse.ArgumentParser(description="Ukrinform（乌克兰国家通讯社）爬虫")
    ap.add_argument("--limit", type=int, default=0, help="最多获取文章数（0=不限）")
    ap.add_argument("--no-detail", action="store_true",
                    help="仅获取 sitemap 元数据（跳过抓取正文）")
    ap.add_argument("--weeks", type=int, default=1,
                    help="包含最新 N 个周的子 sitemap（默认 1 周）")
    ap.add_argument("--delay", type=float, default=2.0, help="请求间隔（秒）")
    ap.add_argument("--cookie", type=str, default=None, help="可选 Cookie 字符串")
    ap.add_argument("--playwright", action="store_true", help="强制使用 Playwright 浏览器引擎")
    ap.add_argument("--out", type=str, default="data/新闻/ukrinform_collection.json",
                    help="输出文件路径")
    ap.add_argument("--root", type=str, default=None,
                    help="替换发现入口：直接使用指定的 sitemap URL 作为唯一来源")
    args = ap.parse_args()

    session = requests.Session()
    session.headers.update(HEADERS)

    # 进度容器提前初始化：让「链接采集」阶段也受 Ctrl+C 保护（中断即可落盘已发现链接）
    out = {
        "source": "Ukrinform (Ukrainian National News Agency)",
        "collected_at": datetime.now(timezone.utc).isoformat(),
        "count": 0,
        "articles": [],
    }

    # ---- 发现 URL 列表 ----
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

    pw = None
    if args.playwright:
        try:
            from playwright.sync_api import sync_playwright
            pw = sync_playwright().start()
        except Exception as e:
            print(f"[warn] Playwright unavailable: {e}", file=sys.stderr)
            pw = None

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
                continue

            html, status = fetch_html(url, session, args.cookie)
            if status != 200 or not html:
                # Playwright 回退：静态获取失败时尝试浏览器渲染
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
            except Exception as e:
                print(f"  [parse err] {url}: {e}", file=sys.stderr)

            if args.delay and i < len(urls):
                time.sleep(args.delay)
    except KeyboardInterrupt:
        # 优雅退出：实时落盘当前进度，打印友好提示，退出码 130（不甩堆栈）
        atomic_save(out, args.out)
        print(f"[interrupted] 已实时保存至当前进度（{len(out['articles'])} 篇）→ {args.out}",
              file=sys.stderr)
        sys.exit(130)
    except Exception as e:
        # 真实异常：落盘后照常抛出，保留完整 traceback 供排查
        atomic_save(out, args.out)
        print(f"\n[error] 抓取异常，已保存当前进度（{len(out['articles'])} 篇）→ {args.out}",
              file=sys.stderr)
        raise

    if pw:
        try:
            pw.stop()
        except Exception:
            pass

    atomic_save(out, args.out)
    print(f"\n[done] wrote {len(out['articles'])} records -> {args.out}", file=sys.stderr)


def slug_title(url):
    """从 URL slug 提取标题兜底：/rubric-x/123-some-words.html → "Some Words"

    当 --no-detail 模式跳过正文获取时，用此函数生成一个基本可读的标题。
    """
    m = re.search(r"/\d+-([a-z0-9-]+)\.html$", url)
    if not m:
        return url
    slug = m.group(1)
    return " ".join(w.capitalize() for w in slug.split("-") if not w.isdigit())


if __name__ == "__main__":
    main()
