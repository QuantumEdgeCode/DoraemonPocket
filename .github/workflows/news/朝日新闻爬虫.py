#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
asahi_crawler.py — 朝日新聞 (www.asahi.com) 新闻爬虫

架构：双引擎（requests + BeautifulSoup 静态优先 → Playwright 渲染降级兜底）。
      实测 asahi.com 为 SSR，静态引擎直接 200 过，无需 Playwright。

关键选择器 / 踩坑（已修进脚本）：
1. 文章 URL：www.asahi.com/articles/<ID>.html （ID 形如 ASV7X2BVJV7XPTIL007M）。
   须排除 digital.asahi.com（付费站）与 ?iref= 跟踪参数（按 ? 去重）。
2. 标题：JSON-LD 的 "headline" 最干净（无 "：朝日新聞" 后缀）；
   回退 og:title 并剥离 "：朝日新聞" / "：朝日" 等后缀。
3. 时间：JSON-LD "datePublished"/"dateModified"（已带 +09:00 JST）；
   回退 <meta article:published_time>。严禁 datetime+9h（沿用 mk/yna 教训，用 tzinfo）。
4. 正文：div.l-main 内 <p>（无 figure 内 caption 干扰）。付费稿在正文末尾插入
   "有料会員になると…" 会员 CTA / "関連記事" 等，遇到这些标记即截断，保证正文纯净。
5. 作者/栏目：JSON-LD "creator"（数组，可多个记者）/ "articleSection"。
6. 图片：l-main 内 <figure><img srcset>，取 srcset 首个 URL（asahicom.jp/imgopt/img…），
   过滤 .svg 站标、profile-image.kraken.asahi.com 记者头像、ogp 栏目 logo；
   协议相对 // 补 https:。回退 og:image 作头图。
7. robots：仅打印状态 + 合规提醒，不阻断（与 Naver/mk/yna/donga 一致的用户偏好）。

合规：朝日新聞内容版权归 株式会社朝日新聞社，仅个人学习/研究，禁止商用转发。
"""

import argparse
import json
import os
import re
import sys
import time
from datetime import datetime, timezone, timedelta
from urllib.parse import urljoin, urlparse

import requests
from bs4 import BeautifulSoup

try:
    from playwright.sync_api import sync_playwright
    HAS_PLAYWRIGHT = True
except Exception:
    HAS_PLAYWRIGHT = False

BASE = "https://www.asahi.com"
HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                  "(KHTML, like Gecko) Chrome/124.0 Safari/537.36",
    "Accept-Language": "ja-JP,ja;q=0.9,en;q=0.8",
}
JST = timezone(timedelta(hours=9))

# 默认抓取的栏目页（实测静态 200 的 URL；/world/ 返回 403、/society/ 返回 404 故剔除）
DEFAULT_ROOTS = [
    "https://www.asahi.com/",
    "https://www.asahi.com/news/",
    "https://www.asahi.com/politics/",
    "https://www.asahi.com/national/",
    "https://www.asahi.com/business/",
    "https://www.asahi.com/sports/",
    "https://www.asahi.com/culture/",
    "https://www.asahi.com/life/",
    "https://www.asahi.com/opinion/",
]

ARTICLE_RE = re.compile(r"https?://(?:www\.)?asahi\.com/articles/([A-Za-z0-9]+)\.html", re.I)
TITLE_SUFFIXES = ["：朝日新聞", "：朝日", " - 朝日新聞", " - 朝日", "｜朝日新聞", "｜朝日"]

# 正文截断标记：遇到这些即停止（会员 CTA / 相关阅读区）
END_MARKERS = ["有料会員になると", "会員限定の", "無料期間中に解約", "関連記事", "あわせて読みたい",
               "※無料期間中", "今すぐ登録", "朝日新聞の", "有料記事を読む"]
# 段落级噪音（分享按钮等）
P_NOISE = ["印刷する", "メールでシェアする", "Facebookでシェアする", "Xでシェアする",
           "はてなブックマークでシェアする", "LINEでシェアする"]

NOISE_IMG = [
    ".svg",
    "profile-image.kraken.asahi.com",
    "koshien/virtualbaseball",
    "asahicom.jp/css/",
    "asahicom.jp/images/clear/",
    "asahicom.jp/images/icon_",
    "www.asahicom.jp/images/logo",
]

SESSION = requests.Session()
SESSION.headers.update(HEADERS)



def fetch(url, use_playwright=False):
    """双引擎：静态优先，失败降级 Playwright。返回 (html, ok)。"""
    try:
        r = SESSION.get(url, timeout=25, allow_redirects=True)
        if r.status_code == 200 and len(r.text) > 1000:
            return r.text, True
        if r.status_code in (301, 302, 403, 404, 410):
            # 尝试 Playwright 降级
            pass
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
    """解析 ISO 时间字符串为带时区的 datetime；失败返回 None。"""
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
    """提取 JSON-LD 结构化数据，返回 dict（取含 headline+datePublished 的那个）。"""
    candidates = []
    for tag in soup.find_all("script", attrs={"type": "application/ld+json"}):
        txt = tag.string or tag.get_text()
        if not txt:
            continue
        try:
            data = json.loads(txt)
        except Exception:
            continue
        if isinstance(data, list):
            candidates.extend(data)
        else:
            candidates.append(data)
    for d in candidates:
        if isinstance(d, dict) and d.get("headline") and (d.get("datePublished") or d.get("articleBody")):
            return d
    return {}


def collect_links(roots):
    """从栏目页收集文章 URL（去重，排除 digital 站 / 跟踪参数）。"""
    links = {}
    for root in roots:
        print(f"[list] 抓取栏目页：{root}")
        html, ok = fetch(root)
        if not ok:
            print(f"  [warn] 栏目页不可达：{root}")
            continue
        soup = BeautifulSoup(html, "html.parser")
        for a in soup.find_all("a", href=True):
            m = ARTICLE_RE.search(a["href"])
            if not m:
                continue
            full = urljoin(BASE, a["href"]).split("?")[0]
            if "digital.asahi.com" in full:
                continue
            links.setdefault(full, m.group(1))
    return links


def parse_article(url, aid, use_playwright=False):
    html, ok = fetch(url, use_playwright=use_playwright)
    if not ok:
        return None
    soup = BeautifulSoup(html, "html.parser")
    ld = extract_jsonld(soup)

    # 标题
    title = clean_title(ld.get("headline")) if ld.get("headline") else None
    if not title:
        og = soup.find("meta", attrs={"property": "og:title"})
        title = clean_title(og.get("content")) if og else None
    if not title:
        h1 = soup.find("h1")
        title = h1.get_text(" ", strip=True) if h1 else None

    # 时间（优先 JSON-LD，回退 meta）
    pub = parse_iso(ld.get("datePublished")) or parse_iso(
        (soup.find("meta", attrs={"property": "article:published_time"}) or {}).get("content"))
    mod = parse_iso(ld.get("dateModified")) or parse_iso(
        (soup.find("meta", attrs={"property": "article:modified_time"}) or {}).get("content"))

    # 作者 / 栏目
    creator = ld.get("creator")
    if isinstance(creator, list):
        author = "、".join([c for c in creator if isinstance(c, str)])
    elif isinstance(creator, str):
        author = creator
    else:
        author = ""
    section = ""
    sec = ld.get("articleSection")
    if isinstance(sec, list) and sec:
        section = sec[0]
    elif isinstance(sec, str):
        section = sec

    # 正文：div.l-main 内 <p>，遇截断标记停止
    content_parts = []
    main = soup.select_one("div.l-main")
    if main:
        for tag in main.find_all(["script", "style"]):
            tag.decompose()
        for p in main.find_all("p"):
            if p.find_parent("figure"):
                continue
            txt = p.get_text(" ", strip=True)
            if not txt:
                continue
            if any(n in txt for n in P_NOISE):
                continue
            if any(em in txt for em in END_MARKERS):
                break
            content_parts.append(txt)
    content = "\n".join(content_parts).strip()

    # 图片：仅保留带图注（figcaption 非空）的 <figure>，过滤相关阅读缩略图噪音
    images = []
    if main:
        for fig in main.find_all("figure"):
            cap = fig.find("figcaption")
            if not cap or not cap.get_text(strip=True):
                continue
            im = fig.find("img")
            if not im:
                continue
            src = im.get("srcset") or im.get("src") or ""
            # srcset: "url 1x, url2 2x" → 取首个 url
            if src:
                first = re.split(r"[\s,]+", src.strip())[0]
                if first:
                    src = first
            if not src:
                continue
            if src.startswith("//"):
                src = "https:" + src
            elif src.startswith("/"):
                src = BASE + src
            if any(n in src for n in NOISE_IMG):
                continue
            if src not in images:
                images.append(src)
    # 回退：og:image 作头图
    if not images:
        og_img = soup.find("meta", attrs={"property": "og:image"})
        if og_img and og_img.get("content"):
            u = og_img["content"]
            if u.startswith("//"):
                u = "https:" + u
            if u not in images and not any(n in u for n in NOISE_IMG):
                images.append(u)

    return {
        "id": aid,
        "url": url,
        "title": title,
        "source": "朝日新聞",
        "author": author or None,
        "section": section or None,
        "language": "ja",
        "published_at": pub.isoformat() if pub else None,
        "updated_at": mod.isoformat() if mod else None,
        "content": content,
        "images": images,
    }


def atomic_save(items, path="data/新闻/asahi_collection.json"):
    """JSON 原子保护性写入：先写 .tmp → flush+fsync → os.replace 改名，杜绝半截 JSON。

    每抓完一篇即调用，进程被中断（Ctrl+C / 断电）也只丢当前这一篇，已落盘数据不丢。
    """
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
    ap = argparse.ArgumentParser(description="朝日新聞 (asahi.com) 爬虫")
    ap.add_argument("--no-detail", action="store_true", help="仅抓取列表，不下载正文")
    ap.add_argument("--limit", type=int, default=0, help="限制处理文章数（0=全量）")
    ap.add_argument("--root", action="append", help="指定栏目/列表页 URL（可多次）")
    ap.add_argument("--cookie", default="", help="可选：注入 Cookie (name=value; ...) 或 cookie.txt 路径")
    ap.add_argument("--playwright", action="store_true", help="强制使用 Playwright 渲染引擎")
    ap.add_argument("--out", default="data/新闻/asahi_collection.json", help="输出文件名")
    args = ap.parse_args()

    if args.cookie:
        cookie = args.cookie
        if cookie.endswith(".txt") or "/" in cookie and not "=" in cookie:
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
    try:
        for i, (url, aid) in enumerate(items, 1):
            print(f"[{i}/{len(items)}] 解析：{url}")
            if args.no_detail:
                articles.append({"id": aid, "url": url, "title": None})
                atomic_save(articles, args.out)  # 实时落盘
                continue
            art = parse_article(url, aid, use_playwright=args.playwright)
            if art and art.get("content"):
                articles.append(art)
                print(f"        ✓ 标题={art['title'][:30] if art['title'] else None} "
                      f"正文={len(art['content'])}字 图={len(art['images'])}张 "
                      f"作者={art['author']} 栏目={art['section']}")
            else:
                print(f"        ✗ 无正文（可能为纯图集/视频/不可达）")
            atomic_save(articles, args.out)  # 每篇实时落盘
            time.sleep(3)  # 礼貌间隔
    except (KeyboardInterrupt, Exception) as e:
        print(f"\n[warn] 采集中断（{type(e).__name__}）：已实时保存至当前进度 → {args.out}")
        raise

    atomic_save(articles, args.out)  # 收尾保存

    with_text = sum(1 for a in articles if a.get("content"))
    with_img = sum(1 for a in articles if a.get("images"))
    total_img = sum(len(a.get("images", [])) for a in articles)
    print(f"\n[完成] 输出 {len(articles)} 篇 → {args.out}")
    print(f"        有正文 {with_text} / 有图 {with_img} / 图片总数 {total_img}")


if __name__ == "__main__":
    main()
