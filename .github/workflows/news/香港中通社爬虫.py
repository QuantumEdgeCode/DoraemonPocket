#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
hkcna_crawler.py — 爬取 https://www.hkcna.hk/ (香港中国通讯社 / 中通社, 中国香港)
================================================================================
方法（静态 HTML + JSP 列表遍历，沿用本仓库统一约定）
  - 站点实为 JSP 动态站，内容在 apex 域名 https://hkcna.hk （www 前缀被阿里云 WAF
    拦截返回 405，故 BASE 用无 www 的 apex 域名）。
  - 文章 URL 形如  https://hkcna.hk/docDetail.jsp?id=<9位数字>&channel=<4位数字>
                   ID 唯一（docDetail 的 id 参数）；channel 为栏目编号
  - 发现来源（去重合并）：
        首页 /  → 所有 docDetail.jsp?id= 链接（直接文章）
                + 所有 index_col*.jsp?channel= / docDetail 的 channel= 参数（栏目集合）
        每个栏目 index_col.jsp?channel=<ID>&page=1..N（默认 2 页）→ 收集文章链接
        ID 提取：docDetail.jsp 的 id=(\d+)；按 id 去重
  - 正文解析：
        · 标题：<title> 去掉「 | 栏目 - 香港中通社」后缀（取首个「 | 」前）
        · 正文：div.main 容器（含 <p> 段落），先 decomposes .noprint/.boxDiv/script/style
                等噪音块，再抽取所有 <p> 文本拼接；无正文则跳过（图集/视频类）
        · 时间：正文/页内匹配 YYYY-MM-DD HH:MM（香港 +08:00 兜底）
        · 栏目：<title> 中「 | 」第二段（如「頭條」）；缺失则用 URL 的 channel 编号
        · 图片：仅取 /picpath/photo/middle/（优先）或 /picpath/photo/ 真实新闻图，
                过滤 images/icon_*、tw-bg.png 等 UI 图标；按 URL 去重
        · 署名：中通社文章多无署名，默认「香港中通社」

合规：香港中国通讯社内容版权归原社所有；本脚本仅用于个人学习/研究，禁止商用转发。
站点无 robots.txt（返回空），文章页可正常抓取。

用法：
  python 香港中通社爬虫.py                 # 全量（默认 limit=400）
  python 香港中通社爬虫.py --limit 50      # 控量
  python 香港中通社爬虫.py --no-detail     # 仅采集链接，不抓正文
  python 香港中通社爬虫.py --delay 0.5     # 请求间隔（秒）
  python 香港中通社爬虫.py --max-pages 3   # 每个栏目翻页数（默认 2）
"""

import argparse
import json
import os
import re
import time
import sys
from datetime import datetime, timezone, timedelta

import requests
from bs4 import BeautifulSoup

BASE = "https://hkcna.hk"
HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                  "(KHTML, like Gecko) Chrome/124.0 Safari/537.36",
    "Accept-Language": "zh-HK,zh;q=0.9,zh-CN;q=0.8,en;q=0.7",
}
HK = timezone(timedelta(hours=8))  # 香港 +08:00
_OUTPUT_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "data", "新闻")
OUTPUT = os.path.join(_OUTPUT_DIR, "hkcna_collection.json")
IMG_RE = re.compile(r"/picpath/photo/")
ICON_RE = re.compile(r"(icon_|tw-bg|logo|banner|ad|wx|wechat|sina|fb)", re.I)
TIME_RE = re.compile(r"(\d{4})[-/年.](\d{1,2})[-/月.](\d{1,2})\D*(\d{1,2}):(\d{2})")


# ---------------------------------------------------------------------------
# 工具
# ---------------------------------------------------------------------------
def abs_url(href):
    if href.startswith("http"):
        return href
    if href.startswith("//"):
        return "https:" + href
    if href.startswith("./"):
        return BASE + "/" + href[2:]
    if href.startswith("/"):
        return BASE + href
    return BASE + "/" + href


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
def collect_links(max_pages=2):
    articles = {}   # id -> url
    channels = set()

    # 1) 首页：直接文章 + 栏目编号
    html = fetch(BASE + "/")
    if html:
        for href in re.findall(r'href="([^"]+)"', html):
            full = abs_url(href)
            if "docDetail.jsp" in full:
                m = re.search(r"[?&]id=(\d+)", full)
                if m:
                    articles.setdefault(m.group(1), full)
            cm = re.search(r"[?&]channel=(\d+)", full)
            if cm:
                channels.add(cm.group(1))
    print("[list] 首页发现文章 {0} 篇，栏目 {1} 个".format(len(articles), len(channels)))

    # 2) 逐栏目翻页补充
    for ch in channels:
        for pg in range(1, max_pages + 1):
            url = "{0}/index_col.jsp?channel={1}&page={2}".format(BASE, ch, pg)
            html = fetch(url)
            if not html:
                break
            found = 0
            for href in re.findall(r'href="([^"]+)"', html):
                full = abs_url(href)
                if "docDetail.jsp" in full:
                    m = re.search(r"[?&]id=(\d+)", full)
                    if m:
                        if m.group(1) not in articles:
                            articles[m.group(1)] = full
                        found += 1
            if found == 0:
                break  # 该栏目已无更多页
    print("[list] 合并后唯一文章：{0} 篇".format(len(articles)))
    return articles


# ---------------------------------------------------------------------------
# 正文 / 时间 / 图片 解析
# ---------------------------------------------------------------------------
def parse_time(html):
    m = TIME_RE.search(html)
    if not m:
        return None
    try:
        y, mo, d, h, mi = (int(x) for x in m.groups())
        return datetime(y, mo, d, h, mi, tzinfo=HK).isoformat()
    except Exception:
        return None


def extract_images(soup):
    imgs, seen = [], set()
    # 优先 middle（正文大图），其次任意 picpath 真实图
    cands = [im.get("src") for im in soup.find_all("img")
             if im.get("src") and "/picpath/photo/middle/" in im.get("src")]
    if not cands:
        cands = [im.get("src") for im in soup.find_all("img")
                 if im.get("src") and IMG_RE.search(im.get("src"))]
    for src in cands:
        if ICON_RE.search(src):
            continue
        full = abs_url(src)
        if full not in seen:
            seen.add(full)
            imgs.append({"url": full, "caption": ""})
    return imgs


def parse_article(url):
    html = fetch(url)
    if not html:
        return None
    soup = BeautifulSoup(html, "html.parser")

    # 标题
    raw = soup.title.string if soup.title else ""
    raw = raw.strip()
    title = raw.split("|")[0].strip() if raw else None
    if not title:
        return None

    # 栏目：<title> 第二段（再去掉「 - 香港中通社」来源后缀）
    section = None
    parts = [p.strip() for p in raw.split("|")]
    if len(parts) >= 2:
        section = parts[1].split(" - ")[0].strip()

    # 正文：div.main，清理噪音块后取 <p>
    main = soup.select_one("div.main") or soup
    for bad in main.select(".noprint, .boxDiv, script, style"):
        bad.decompose()
    paras = [p.get_text(strip=True) for p in main.find_all("p")]
    content = "\n\n".join(p for p in paras if p)
    if not content.strip():
        return None  # 纯图集/视频/不可达

    published_at = parse_time(html)

    images = extract_images(soup)

    # 署名：中通社文章无独立署名，正文末通常标「（香港中通社）」，统一记来源
    author = "香港中通社"

    # URL 内 channel 兜底栏目
    if not section:
        cm = re.search(r"[?&]channel=(\d+)", url)
        section = cm.group(1) if cm else ""

    return {
        "id": re.search(r"[?&]id=(\d+)", url).group(1) if re.search(r"[?&]id=(\d+)", url) else url,
        "url": url,
        "title": title,
        "section": section,
        "author": author,
        "published_at": published_at,
        "summary": "",
        "content": content,
        "images": images,
        "language": "zh",
    }


# ---------------------------------------------------------------------------
# 原子写入
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
    ap.add_argument("--limit", type=int, default=400)
    ap.add_argument("--no-detail", action="store_true")
    ap.add_argument("--delay", type=float, default=0.5)
    ap.add_argument("--max-pages", type=int, default=2)
    args = ap.parse_args()

    articles = []
    try:
        print("[list] 采集文章链接（首页 → 栏目翻页）...")
        links = collect_links(max_pages=args.max_pages)
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
                print("        ✓ 标题={0} 正文={1}字 图={2}张 栏目={3} 时间={4}".format(
                    (art["title"][:34] if art["title"] else None),
                    len(art["content"]),
                    len(art["images"]),
                    art["section"],
                    (art["published_at"][:16] if art["published_at"] else "无"),
                ))
            else:
                print("        ✗ 无正文（图集/视频/不可达）")
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
