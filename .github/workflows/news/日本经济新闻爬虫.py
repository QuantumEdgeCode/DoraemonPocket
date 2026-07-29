#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
nikkei_crawler.py — 日本経済新聞 (www.nikkei.com) 新闻爬虫

架构：双引擎（requests + BeautifulSoup 静态优先 → Playwright 渲染降级兜底）。
      实测 nikkei.com 为 SSR，静态引擎直接 200 过，正文已在初始 HTML 中，
      Playwright 仅多解析约 300 字（意义不大），故默认静态优先。

关键选择器 / 踩坑（已修进脚本）：
1. 文章 URL：nikkei.com/article/<ID>/（ID 形如 DGXZQOUC281L20Y6A720C2000000）。
   须排除 /prime/（付费精选，路径为 /prime/ft/article/...）、ngc.nikkei.com（异域）。
2. 标题：H1 即干净标题（无 " - 日本経済新聞" 后缀）；回退 og:title 剥离后缀。
3. 时间：JSON-LD "datePublished"（已带 +09:00 JST）；
   回退 <meta article:published_time>。严禁 datetime+9h（沿用 mk/yna 教训，用 tzinfo）。
4. 正文：<article> 内 <p>。付费稿（会員限定）静态仅返回约 1000 字预览，
   免费稿为全文。须过滤页脚（日経の記事利用サービスについて）、会员 CTA
   （すべての記事が読み放題）、相关阅读（【関連記事】/こちらもおすすめ/関連企業・業界）等噪音段。
5. 作者/来源：JSON-LD "author" 多为机构（日本経済新聞社），个人署名罕见；
   source 固定 "日本経済新聞社"。付费标记：HTML 含 "会員限定" 时记 paywalled=True。
6. 图片：<article> 内 <figure><img>，真实配图域名 article-image-ix.nikkei.com
   （url-encoded imgix）；过滤 /.resources/ 横幅与 .svg 站标。回退 og:image。
7. robots：通用 UA 仅 Disallow 工具/股价数据类路径，/article/ 允许；仅打印状态+合规提醒，不阻断。

合规：日本経済新聞社内容版权归 株式会社日本経済新聞社，仅个人学习/研究，禁止商用转发。
      注意：多数记为会员限定，抓取到的为公开预览片段，非全文。
"""

import argparse
import json
import os
import re
import time
from datetime import datetime, timezone, timedelta
from urllib.parse import urljoin

import requests
from bs4 import BeautifulSoup

try:
    from playwright.sync_api import sync_playwright
    HAS_PLAYWRIGHT = True
except Exception:
    HAS_PLAYWRIGHT = False

BASE = "https://www.nikkei.com"
HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                  "(KHTML, like Gecko) Chrome/124.0 Safari/537.36",
    "Accept-Language": "ja-JP,ja;q=0.9,en;q=0.8",
}
JST = timezone(timedelta(hours=9))

DEFAULT_ROOTS = [
    "https://www.nikkei.com/",
    "https://www.nikkei.com/economy/",
    "https://www.nikkei.com/politics/",
    "https://www.nikkei.com/business/",
    "https://www.nikkei.com/markets/",
    "https://www.nikkei.com/sports/",
    "https://www.nikkei.com/culture/",
    "https://www.nikkei.com/opinion/",
]

ARTICLE_RE = re.compile(r"https?://(?:www\.)?nikkei\.com/article/([A-Za-z0-9]{15,28})/?", re.I)
TITLE_SUFFIXES = [" - 日本経済新聞", " - 日本経済新聞社", " ｜日本経済新聞", " ｜日本経済新聞社"]

# 段落级噪音（页脚 / 会员 CTA / 相关阅读区）
P_NOISE = [
    "日経の記事利用サービスについて",
    "企業での記事共有",
    "【関連記事】",
    "関連企業",
    "関連キーワード",
    "こちらもおすすめ",
    "すべての記事が読み放題",
    "この投稿は現在非表示",
    "※掲載される投稿",
    "有料会員が初回",
    "ログインして読む",
    "記事を印刷する",
    "メールで送る",
]

NOISE_IMG = [
    "/.resources/",
    ".svg",
    "banner",
    "paid-banner",
    "logo",
]

SESSION = requests.Session()
SESSION.headers.update(HEADERS)



def fetch(url, use_playwright=False):
    try:
        r = SESSION.get(url, timeout=25, allow_redirects=True)
        if r.status_code == 200 and len(r.text) > 1000:
            return r.text, True
    except Exception as e:
        print(f"  [warn] requests 失败 {url}: {e}")
    if use_playwright and HAS_PLAYWRIGHT:
        try:
            with sync_playwright() as p:
                b = p.chromium.launch(headless=True)
                pg = b.new_page()
                pg.goto(url, timeout=30000, wait_until="networkidle")
                html = pg.content()
                b.close()
                if html and len(html) > 1000:
                    return html, True
        except Exception as e:
            print(f"  [warn] Playwright 也失败 {url}: {e}")
    return "", False


def parse_iso(s):
    if not s:
        return None
    s = s.strip()
    if s.endswith("Z"):
        s = s[:-1] + "+00:00"
    try:
        dt = datetime.fromisoformat(s)
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=JST)
        return dt
    except Exception:
        return None


def clean_title(raw):
    if not raw:
        return None
    for suf in TITLE_SUFFIXES:
        if raw.endswith(suf):
            raw = raw[: -len(suf)]
    return raw.strip()


def extract_jsonld(soup):
    out = {}
    for tag in soup.find_all("script", attrs={"type": "application/ld+json"}):
        txt = tag.string or tag.get_text()
        if not txt:
            continue
        try:
            data = json.loads(txt)
        except Exception:
            continue
        items = data if isinstance(data, list) else [data]
        for d in items:
            if isinstance(d, dict) and d.get("@type") in ("NewsArticle", "Article") or (
                isinstance(d, dict) and d.get("headline")):
                out.update(d)
    return out


# 列表页为 JS 驱动，静态 HTML 中直接 /article/ 的 <a href> 极少（每页约 3 条）。
# 关键发现：首页/栏目页 HTML（含 JS state）内嵌大量文章 ID（DGXZQO...），
# 首页 123 个、/business/ 94 个、/markets/ 70 个、/economy/ 51 个… 故从 HTML 提取内嵌 ID 重建 URL。
# 注意：同一篇文章常有 26 位标准 ID 与 30 位「日期后缀」别名两种形式（内容完全相同），
# 故内嵌 ID 仅取 15~28 位标准形态，丢弃 30 位日期别名以避免重复抓取。
EMBED_ID_RE = re.compile(r"DGXZQO[A-Za-z0-9]{15,28}")


def collect_links(roots):
    links = {}
    for root in roots:
        print(f"[list] 抓取栏目页：{root}")
        html, ok = fetch(root)
        if not ok:
            print(f"  [warn] 栏目页不可达：{root}")
            continue
        soup = BeautifulSoup(html, "html.parser")
        # 1) 显式 /article/<id> 链接
        for a in soup.find_all("a", href=True):
            m = ARTICLE_RE.search(a["href"])
            if not m:
                continue
            full = urljoin(BASE, a["href"]).split("?")[0].rstrip("/")
            links.setdefault(full, m.group(1))
        # 2) 内嵌文章 ID（JS state / JSON），重建为 /article/<id>/
        for aid in EMBED_ID_RE.findall(html):
            full = f"{BASE}/article/{aid}/"
            links.setdefault(full, aid)
    return links


def parse_article(url, aid, use_playwright=False):
    html, ok = fetch(url, use_playwright=use_playwright)
    if not ok:
        return None
    soup = BeautifulSoup(html, "html.parser")
    ld = extract_jsonld(soup)

    # 标题
    title = None
    h1 = soup.find("h1")
    if h1 and h1.get_text(strip=True):
        title = h1.get_text(" ", strip=True)
    if not title:
        title = clean_title((soup.find("meta", attrs={"property": "og:title"}) or {}).get("content"))

    # 时间
    pub = parse_iso(ld.get("datePublished")) or parse_iso(
        (soup.find("meta", attrs={"property": "article:published_time"}) or {}).get("content"))
    mod = parse_iso(ld.get("dateModified")) or parse_iso(
        (soup.find("meta", attrs={"property": "article:modified_time"}) or {}).get("content"))

    # 作者 / 栏目
    author = ""
    ad = ld.get("author")
    if isinstance(ad, dict) and ad.get("name") and ad.get("@type") == "Person":
        author = ad["name"]
    section = ""
    sec = ld.get("articleSection")
    if isinstance(sec, list) and sec:
        section = sec[0]
    elif isinstance(sec, str):
        section = sec

    # 付费标记
    paywalled = ("会員限定" in html) or ("有料" in html and "読み放題" in html)

    # 正文：<article> 内 <p>，过滤噪音段
    content_parts = []
    art = soup.find("article")
    if art:
        for p in art.find_all("p"):
            txt = p.get_text(" ", strip=True)
            if not txt or len(txt) < 15:
                continue
            if any(n in txt for n in P_NOISE):
                continue
            content_parts.append(txt)
    content = "\n".join(content_parts).strip()

    # 图片：article-image-ix.nikkei.com 真实配图
    images = []
    if art:
        for fig in art.find_all("figure"):
            im = fig.find("img")
            if not im:
                continue
            src = im.get("src") or im.get("data-src") or im.get("srcset") or ""
            if src.startswith("//"):
                src = "https:" + src
            elif src.startswith("/"):
                src = BASE + src
            if not src or any(n in src for n in NOISE_IMG):
                continue
            if "article-image-ix.nikkei.com" not in src:
                continue
            if src not in images:
                images.append(src)
    if not images:
        og = soup.find("meta", attrs={"property": "og:image"})
        if og and og.get("content") and "article-image-ix.nikkei.com" in og["content"]:
            u = og["content"]
            if u.startswith("//"):
                u = "https:" + u
            if u not in images:
                images.append(u)

    return {
        "id": aid,
        "url": url,
        "title": title,
        "source": "日本経済新聞社",
        "author": author or None,
        "section": section or None,
        "language": "ja",
        "published_at": pub.isoformat() if pub else None,
        "updated_at": mod.isoformat() if mod else None,
        "paywalled": paywalled,
        "content": content,
        "images": images,
    }


def atomic_save(articles, path):
    """原子写入：写 .tmp -> flush -> fsync -> os.replace，防止进程中断留半截 JSON。"""
    d = os.path.dirname(path)
    if d:
        os.makedirs(d, exist_ok=True)
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(articles, f, ensure_ascii=False, indent=2)
        f.flush()
        os.fsync(f.fileno())
    os.replace(tmp, path)


def main():
    ap = argparse.ArgumentParser(description="日本経済新聞 (nikkei.com) 爬虫")
    ap.add_argument("--no-detail", action="store_true", help="仅抓取列表，不下载正文")
    ap.add_argument("--limit", type=int, default=0, help="限制处理文章数（0=全量）")
    ap.add_argument("--root", action="append", help="指定栏目/列表页 URL（可多次）")
    ap.add_argument("--cookie", default="", help="可选：注入 Cookie (name=value; ...) 或 cookie.txt 路径")
    ap.add_argument("--playwright", action="store_true", help="强制使用 Playwright 渲染引擎")
    ap.add_argument("--out", default="data/新闻/nikkei_collection.json", help="输出文件名")
    args = ap.parse_args()

    if args.cookie:
        cookie = args.cookie
        if cookie.endswith(".txt"):
            try:
                with open(cookie, "r", encoding="utf-8") as f:
                    cookie = f.read().strip()
            except Exception:
                pass
        SESSION.headers["Cookie"] = cookie


    roots = args.root if args.root else DEFAULT_ROOTS
    links = collect_links(roots)
    print(f"[list] 共发现 {len(links)} 篇不重复文章")

    if args.limit:
        items = list(links.items())[: args.limit]
    else:
        items = list(links.items())

    articles = []
    seen_titles = set()
    try:
        for i, (url, aid) in enumerate(items, 1):
            print(f"[{i}/{len(items)}] 解析：{url}")
            if args.no_detail:
                articles.append({"id": aid, "url": url, "title": None})
                atomic_save(articles, args.out)  # 实时落盘
                continue
            art = parse_article(url, aid, use_playwright=args.playwright)
            if art and art.get("content"):
                t = art.get("title")
                if t and t in seen_titles:  # 标题去重（捕捉残余别名重复）
                    print(f"        ↺ 跳过重复（标题已收录）：{t[:30]}")
                    continue
                if t:
                    seen_titles.add(t)
                articles.append(art)
                atomic_save(articles, args.out)  # 实时落盘，防中断丢数据
                print(f"        ✓ 标题={t[:30] if t else None} "
                      f"正文={len(art['content'])}字 图={len(art['images'])}张 "
                      f"付费={art['paywalled']} 栏目={art['section']}")
            else:
                print(f"        ✗ 无正文（可能为纯图集/视频/不可达）")
            time.sleep(3)
    except KeyboardInterrupt:
        print(f"\n[interrupted] 已抓取的 {len(articles)} 篇已原子落盘 -> {args.out}")
        return

    atomic_save(articles, args.out)  # 收尾再存（幂等）

    with_text = sum(1 for a in articles if a.get("content"))
    with_img = sum(1 for a in articles if a.get("images"))
    total_img = sum(len(a.get("images", [])) for a in articles)
    paid = sum(1 for a in articles if a.get("paywalled"))
    print(f"\n[完成] 输出 {len(articles)} 篇 → {args.out}")
    print(f"        有正文 {with_text} / 有图 {with_img} / 图片总数 {total_img} / 付费标记 {paid}")


if __name__ == "__main__":
    main()
