#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
hk01_crawler.py — 爬取 https://www.hk01.com/ (香港01, 中国香港)
================================================================================
方法（单引擎 + Next.js __NEXT_DATA__ 解析，沿用本仓库统一约定）
  - 文章 URL 形如  https://www.hk01.com/<中文栏目>/<ID>/<slug>
                   例：/地產樓市/60375282/高價接英皇火棒-...
                   ID 为路径中的 6+ 位数字，唯一；另有 /article/<ID> 短链等价
  - 发现来源（去重合并）：
        /sitemap.xml（约 825 条，最新在前，含所有栏目文章 loc）→ 主文章池
        兜底：首页 / 内链（href 中含 6+ 位数字 ID 即视为文章）
        ID 提取：路径中的 /(\d{6,})/
  - 正文解析：HK01 为 Next.js SSR，文章数据集中在
        <script id="__NEXT_DATA__"> 的 props.initialProps.pageProps.article 中：
          · articleId        → ID（也可用 URL 数字兜底）
          · title            → 标题
          · blocks[].htmlTokens[][].content → 正文（每个 run 为一段，token 含
                                type=text/h2，h2 即小标题；按段拼接）
          · publishTime      → 发布时间（**Unix 时间戳**，需 +08:00 转换）
          · authors[].publishName → 署名（可能多名，合并为逗号分隔）
          · zone.publishName / mainCategory / categories[].publishName → 栏目
          · description      → 摘要
          · mainImage.cdnUrl + thumbnails[].cdnUrl + blocks[].image.cdnUrl
                              → 图片（去重，均为 cdn.hk01.com 真实图）
        JSON-LD（ld+json，list 结构，首元素 NewsArticle）作标题/时间兜底校验。
  - 时间：publishTime 时间戳 → 香港 +08:00；缺失则用 lastModifyTime 兜底
  - 图片：mainImage + thumbnails + 正文 block 内嵌图，去重；均为 cdn.hk01.com
  - 栏目：zone.publishName（如「經濟」）优先，否则 mainCategory / categories[0]

合规：香港01 内容版权归原社所有；本脚本仅用于个人学习/研究，禁止商用转发。
robots.txt 仅 Disallow /api/、/assets/ 等，文章页可正常抓取。

用法：
  python 香港01爬虫.py                  # 全量（默认 limit=600）
  python 香港01爬虫.py --limit 50       # 控量
  python 香港01爬虫.py --no-detail      # 仅采集链接，不抓正文
  python 香港01爬虫.py --delay 1        # 请求间隔（秒）
  python 香港01爬虫.py --max-pages 2    # 首页补充发现时翻页数（一般无需）
"""

import argparse
import json
import os
import re
import time
import sys
from datetime import datetime, timezone, timedelta

import requests

BASE = "https://www.hk01.com"
SITEMAP = BASE + "/sitemap.xml"
HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                  "(KHTML, like Gecko) Chrome/124.0 Safari/537.36",
    "Accept-Language": "zh-HK,zh;q=0.9,en;q=0.8",
}
HK = timezone(timedelta(hours=8))  # 香港 +08:00
_OUTPUT_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "data", "新闻")
OUTPUT = os.path.join(_OUTPUT_DIR, "hk01_collection.json")
ART_ID_RE = re.compile(r"/(\d{6,})")


# ---------------------------------------------------------------------------
# 网络获取
# ---------------------------------------------------------------------------
def fetch(url):
    try:
        r = requests.get(url, headers=HEADERS, timeout=25)
        if r.status_code == 200:
            return r.text
    except Exception:
        pass
    return None


# ---------------------------------------------------------------------------
# 链接采集
# ---------------------------------------------------------------------------
def collect_links():
    links = {}  # aid -> url（去重，保留首个）

    # 1) sitemap.xml（主池，约 825 条）
    try:
        r = requests.get(SITEMAP, headers=HEADERS, timeout=30)
        if r.status_code == 200:
            for loc in re.findall(r"<loc>(.*?)</loc>", r.text):
                if "hk01.com" not in loc:
                    continue
                m = ART_ID_RE.search(loc)
                if m:
                    links.setdefault(m.group(1), loc.split("?")[0])
    except Exception:
        pass
    print("[list] sitemap 发现文章：{0} 篇".format(len(links)))

    # 2) 兜底：首页内链补充（sitemap 异常或数量偏少时）
    if len(links) < 50:
        html = fetch(BASE + "/")
        if html:
            for href in re.findall(r'href="([^"]+)"', html):
                if "hk01.com" in href:
                    full = href
                elif href.startswith("/"):
                    full = BASE + href
                else:
                    continue
                m = ART_ID_RE.search(full)
                if m:
                    links.setdefault(m.group(1), full.split("?")[0])
        print("[list] 补充首页内链后：{0} 篇".format(len(links)))

    return links


# ---------------------------------------------------------------------------
# 时间 / 字段工具
# ---------------------------------------------------------------------------
def ts_to_iso(ts):
    try:
        return datetime.fromtimestamp(int(ts), HK).isoformat()
    except Exception:
        return None


def build_content(blocks):
    """从 blocks 的 htmlTokens 还原正文：每个 run 为一段，token.content 拼接。"""
    parts = []
    for b in blocks or []:
        ht = b.get("htmlTokens")
        if not ht or not isinstance(ht, list):
            continue
        para = ""
        for run in ht:
            if isinstance(run, list):
                for tok in run:
                    if isinstance(tok, dict):
                        c = tok.get("content")
                        if c:
                            para += c
        if para.strip():
            parts.append(para.strip())
    return "\n\n".join(parts)


def collect_images(art):
    imgs, seen = [], set()

    def add(url, caption=""):
        if url and url not in seen:
            seen.add(url)
            imgs.append({"url": url, "caption": caption or ""})

    mi = art.get("mainImage") or {}
    if mi.get("cdnUrl"):
        add(mi["cdnUrl"], mi.get("caption", ""))
    for t in art.get("thumbnails") or []:
        if isinstance(t, dict) and t.get("cdnUrl"):
            add(t["cdnUrl"], t.get("caption", ""))
    for b in art.get("blocks") or []:
        im = b.get("image")
        if isinstance(im, dict) and im.get("cdnUrl"):
            add(im["cdnUrl"], im.get("caption", ""))
    return imgs


# ---------------------------------------------------------------------------
# 文章解析
# ---------------------------------------------------------------------------
def parse_article(url):
    html = fetch(url)
    if not html:
        return None

    m = re.search(r'<script id="__NEXT_DATA__"[^>]*>(.*?)</script>', html, re.S)
    if not m:
        return None
    try:
        nd = json.loads(m.group(1))
    except Exception:
        return None

    art = (nd.get("props") or {}).get("initialProps", {}).get("pageProps", {}).get("article")
    if not art:
        return None

    aid = str(art.get("articleId") or "")
    if not aid:
        m2 = ART_ID_RE.search(url)
        aid = m2.group(1) if m2 else url
    title = art.get("title")
    if not title:
        return None

    content = build_content(art.get("blocks", []))
    if not content.strip():
        return None  # 纯图集/视频类无正文

    ts = art.get("publishTime") or art.get("lastModifyTime")
    published_at = ts_to_iso(ts) if isinstance(ts, (int, float)) else None

    authors = [a.get("publishName") for a in art.get("authors", []) if a.get("publishName")]
    author = ", ".join(authors) if authors else "香港01"

    zone = art.get("zone")
    section = zone.get("publishName") if isinstance(zone, dict) else None
    if not section:
        section = art.get("mainCategory") or ""
    if not section and art.get("categories"):
        c0 = art["categories"][0]
        section = c0.get("publishName") if isinstance(c0, dict) else str(c0)

    summary = art.get("description") or ""

    images = collect_images(art)

    return {
        "id": aid,
        "url": url,
        "title": title,
        "section": section,
        "author": author,
        "published_at": published_at,
        "summary": summary,
        "content": content,
        "images": images,
        "language": "zh",
    }


# ---------------------------------------------------------------------------
# 原子写入（防中断半截文件）
# ---------------------------------------------------------------------------
def atomic_save(articles, path=OUTPUT):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(articles, f, ensure_ascii=False, indent=2)
        f.flush()
        os.fsync(f.fileno())
    os.replace(tmp, path)


# ---------------------------------------------------------------------------
# 主流程
# ---------------------------------------------------------------------------
def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--limit", type=int, default=600)
    ap.add_argument("--no-detail", action="store_true")
    ap.add_argument("--delay", type=float, default=0.8)
    ap.add_argument("--max-pages", type=int, default=1)
    args = ap.parse_args()

    articles = []
    try:
        print("[list] 采集文章链接（sitemap.xml → 首页兜底）...")
        links = collect_links()
        print("[list] 唯一文章：{0} 篇".format(len(links)))

        items = list(links.items())
        if args.limit:
            items = items[: args.limit]

        for i, (aid, url) in enumerate(items, 1):
            print("[{0}/{1}] 解析：{2}".format(i, len(items), url))
            if args.no_detail:
                articles.append({"id": aid, "url": url, "title": None})
                atomic_save(articles, OUTPUT)
                continue
            art = parse_article(url)
            if art and art.get("content"):
                articles.append(art)
                print("        ✓ 标题={0} 正文={1}字 图={2}张 栏目={3} 署名={4} 时间={5}".format(
                    (art["title"][:36] if art["title"] else None),
                    len(art["content"]),
                    len(art["images"]),
                    art["section"],
                    (art["author"][:20] if art["author"] else ""),
                    (art["published_at"][:16] if art["published_at"] else "无"),
                ))
            else:
                print("        ✗ 无正文（可能为纯图集/视频/不可达）")
            atomic_save(articles, OUTPUT)  # 每篇实时落盘
            time.sleep(args.delay)

        print("\n[done] 已保存 {0} 篇 → {1}".format(len(articles), OUTPUT))
    except KeyboardInterrupt:
        atomic_save(articles, OUTPUT)
        print("\n[interrupted] 已实时保存至当前进度（{0} 篇）→ {1}".format(len(articles), OUTPUT))
        sys.exit(130)
    except Exception as exc:
        atomic_save(articles, OUTPUT)
        print("\n[error] 抓取异常，已保存当前进度（{0} 篇）→ {1}".format(len(articles), OUTPUT))
        raise


if __name__ == "__main__":
    main()
