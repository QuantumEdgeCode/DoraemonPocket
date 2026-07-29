#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
sputnik_crawler.py — 爬取 https://sputniknews.cn/ (俄罗斯卫星通讯社 中国站)
=================================================================================
方法（双引擎：静态优先 / Playwright 降级，沿用本项目统一约定）
  - 发现来源：
        robots.txt 指向 Sitemap: /sitemap_article_index.xml（索引）
          → 含 140 个月级子 sitemap（按月 date_start/date_end）
          → 每子 sitemap 约 2300+ 篇文章（URL 形如 /YYYYMMDD/<ID>.html）
        ID 提取：从 URL 末段 <ID>.html 的数字；同一月内 ID 唯一
        ★ 默认从最新月份往前抓；--months N 控制回溯月数（默认 1，全量可调大）
  - 文章 URL 形如  https://sputniknews.cn/20260729/1072541907.html
  - 正文容器：div.article__body
        段落 = div.article__block[data-type="text"] > div.article__text
        配图 = div.article__block[data-type="article"] > img（src，含 caption）
  - 标题：og:title（<title> 含日期与站名后缀，故取 og:title 更干净）
  - 时间：article:published_time → 格式 20260729T1319+0800（已含 +08:00 中国时区）
  - 摘要：og:description / meta description
  - 关键词：meta keywords → 作为 tags 字段
  - 栏目：sputniknews.cn 文章 URL 不编码栏目，breadcrumb 弱；统一记 section="俄罗斯卫星通讯社"
  - 署名：Sputnik 中文站无单独署名栏（meta author 缺失），统一记 "卫星社"

合规：俄罗斯卫星通讯社内容版权归原社，仅限个人学习/研究，禁止商用转发。
      robots.txt 仅 Disallow 打印页/搜索/服务等，文章页可爬；robots 提示仅作提示不阻断。
用法（同统一约定）：
  python sputnik_crawler.py [--limit 800] [--delay 3] [--months 1] [--no-detail] [--playwright]
"""
import argparse
import json
import os
import re
import time
from datetime import datetime

import requests
from bs4 import BeautifulSoup

try:
    from playwright.sync_api import sync_playwright  # 兜底引擎
    _HAVE_PW = True
except Exception:
    _HAVE_PW = False

BASE = "https://sputniknews.cn"
SITEMAP_INDEX = BASE + "/sitemap_article_index.xml"
OUT_FILE = "data/新闻/sputnik_collection.json"

HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                   "(KHTML, like Gecko) Chrome/124.0 Safari/537.36"
}
DETAIL_BAD = {"相关新闻", "阅读更多", "订阅我们", "版权", "转载", "纠错",
              "卫星通讯社", "俄罗斯卫星", "关注我们", "分享", "点击", "展开"}


def get_html(url, use_playwright=False):
    """静态优先；失败且已装 Playwright 时降级。"""
    try:
        r = requests.get(url, headers=HEADERS, timeout=20)
        if r.status_code == 200 and len(r.text) > 500:
            return r.text
    except Exception:
        pass
    if use_playwright and _HAVE_PW:
        try:
            with sync_playwright() as p:
                b = p.chromium.launch()
                pg = b.new_page()
                pg.goto(url, timeout=30000)
                html = pg.content()
                b.close()
                return html
        except Exception:
            return None
    return None


def parse_time(raw):
    """20260729T1319+0800 → datetime(+08:00)"""
    if not raw:
        return None
    try:
        return datetime.strptime(raw, "%Y%m%dT%H%M%z")
    except Exception:
        return None


def fetch_sitemap_urls(months=1):
    """返回 (url, article_id) 列表；从最新月份往前取 months 个月。"""
    html = get_html(SITEMAP_INDEX)
    if not html:
        print("[!] 无法获取 sitemap 索引，尝试直接最近月份")
        html = ""
    locs = re.findall(r"<loc>(.*?)</loc>", html)
    # 按月倒序：索引本身已是新→旧，取前 months 个
    sub = locs[:months] if months and months > 0 else locs
    out = []
    seen = set()
    for sm in sub:
        # sitemap 内的 URL 可能用 &amp; 实体，需还原
        sm_real = sm.replace("&amp;", "&")
        shtml = get_html(sm_real)
        if not shtml:
            continue
        for loc in re.findall(r"<loc>(.*?)</loc>", shtml):
            loc = loc.replace("&amp;", "&")
            m = re.search(r"/(\d+)\.html$", loc)
            if not m:
                continue
            aid = m.group(1)
            if aid in seen:
                continue
            seen.add(aid)
            out.append((loc, aid))
    return out


def parse_article(url, aid, verbose=False, max_retry=3):
    """解析单篇文章。

    当「HTML 已拿到但正文解析为空」时（多为页面动态内容/缓存未就绪，
    或个别文章结构异常），自动整页重抓 + 重解析，最多 max_retry 次，
    退避递增；避免偶发失败直接丢掉整篇文章。
    """
    for attempt in range(1, max_retry + 1):
        html = get_html(url)
        if not html:
            # 网络层失败：退避后重试整页
            if attempt < max_retry:
                if verbose:
                    print(f"    ↻ 抓取失败，重试 {attempt}/{max_retry}: {url}")
                time.sleep(1.5 * attempt)
                continue
            return None
        art = _parse_html(html, aid, url)
        if art:
            return art
        # HTML 到位但正文为空 → 可能是动态内容未就绪，重试
        if attempt < max_retry:
            if verbose:
                print(f"    ↻ 解析为空，重试 {attempt}/{max_retry}: {url}")
            time.sleep(1.5 * attempt)
            continue
    return None


def _parse_html(html, aid, url):
    """从已获取的 HTML 解析出文章记录；解析失败（无正文）返回 None。"""
    soup = BeautifulSoup(html, "html.parser")

    # 标题
    title = None
    og = soup.find("meta", property="og:title")
    if og and og.get("content"):
        title = og["content"].strip()
    if not title:
        h1 = soup.find("h1", class_="article__title")
        title = h1.get_text(strip=True) if h1 else None
    if not title:
        t = soup.find("title")
        title = t.get_text(strip=True) if t else None
    if not title:
        return None

    # 时间
    pub = None
    m = soup.find("meta", property="article:published_time")
    if m and m.get("content"):
        pub = parse_time(m["content"])

    # 摘要
    summary = None
    ogd = soup.find("meta", property="og:description")
    if ogd and ogd.get("content"):
        summary = ogd["content"].strip()
    if not summary:
        md = soup.find("meta", attrs={"name": "description"})
        if md and md.get("content"):
            summary = md["content"].strip()

    # 关键词 → tags
    tags = []
    mk = soup.find("meta", attrs={"name": "keywords"})
    if mk and mk.get("content"):
        tags = [t.strip() for t in mk["content"].split(",") if t.strip()]

    # 正文段落 + 配图
    body = soup.find("div", class_="article__body")
    if not body:
        return None
    content_parts = []
    images = []
    for block in body.find_all("div", class_="article__block"):
        dtype = block.get("data-type")
        if dtype == "text":
            txt = block.find("div", class_="article__text")
            if txt:
                content_parts.append(txt.get_text(strip=True))
        elif dtype in ("quote", "vrez"):
            # 改版/短讯：正文常被包进 quote / vrez 块的 <p> 内（而非标准 text 块）
            for p in block.find_all("p"):
                content_parts.append(p.get_text(strip=True))
        elif dtype == "article":
            im = block.find("img")
            if im:
                src = im.get("src") or im.get("data-src") or ""
                if src and "svg+xml" not in src and "data:" not in src:
                    cap = block.find(class_="article__article-desc")
                    images.append({
                        "url": src,
                        "caption": cap.get_text(strip=True) if cap else ""
                    })
    # 兜底：若上述仍无正文，直接从 body 内所有 <p> 提取
    # （兼容极少数结构异常、段落未包进标准 block 的文章）
    if not content_parts:
        for p in body.find_all("p"):
            t = p.get_text(strip=True)
            if t:
                content_parts.append(t)
    content = "\n\n".join(p for p in content_parts if p)
    if not content:
        return None

    # 截断页脚噪音
    for marker in DETAIL_BAD:
        idx = content.find(marker)
        if idx != -1 and idx > len(content) * 0.6:
            content = content[:idx].strip()
            break

    return {
        "id": aid,
        "url": url,
        "title": title,
        "section": "俄罗斯卫星通讯社",
        "section_code": "sputniknews.cn",
        "author": "卫星社",
        "published_at": pub.isoformat() if pub else None,
        "summary": summary,
        "content": content,
        "images": images,
        "tags": tags,
        "language": "zh",
    }


def atomic_save(items, path=OUT_FILE):
    """原子保护性写入 JSON。

    先写 path.tmp（flush + fsync 强制落盘），再用 os.replace 原子改名覆盖，
    杜绝进程中断/断电导致半截 JSON 损坏。逐篇实时调用，跑多少存多少。
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
    ap = argparse.ArgumentParser(description="Sputnik News CN crawler")
    ap.add_argument("--limit", type=int, default=800)
    ap.add_argument("--delay", type=float, default=3.0)
    ap.add_argument("--months", type=int, default=1,
                    help="回溯月数（从最新月份往前），默认 1")
    ap.add_argument("--no-detail", action="store_true")
    ap.add_argument("--playwright", action="store_true")
    ap.add_argument("--retry", type=int, default=3,
                    help="单篇解析失败时整页重抓重试次数（默认 3）")
    args = ap.parse_args()

    print(f"[*] 抓取 sitemap 索引（最近 {args.months} 个月）…")
    items_meta = fetch_sitemap_urls(months=args.months)
    print(f"[*] 发现文章 {len(items_meta)} 篇，开始解析（limit={args.limit}）")

    collected = []
    done = 0
    try:
        for url, aid in items_meta:
            if len(collected) >= args.limit:
                break
            art = parse_article(url, aid, verbose=not args.no_detail,
                                max_retry=args.retry)
            if not art:
                if not args.no_detail:
                    print(f"  ! 跳过（重试 {args.retry} 次仍无正文/解析失败）：{url}")
                continue
            collected.append(art)
            done += 1
            if not args.no_detail:
                print(f"[{len(collected)}] {art['title'][:30]} | {len(art['content'])}字 "
                      f"图={len(art['images'])} 时间={art['published_at']}")
            # 实时原子落盘：抓一篇存一篇，进程中断也不丢已抓数据
            atomic_save(collected, OUT_FILE)
            time.sleep(args.delay)
    except (KeyboardInterrupt, Exception) as exc:
        # 中断/异常前先把已抓的进度存盘
        atomic_save(collected, OUT_FILE)
        print(f"\n[!] 已实时保存至当前进度（{len(collected)} 篇）→ {OUT_FILE}")
        raise

    # 收尾再存一次（确保最终一致）
    atomic_save(collected, OUT_FILE)
    print(f"\n[done] 已保存 {len(collected)} 篇 → {OUT_FILE}")


if __name__ == "__main__":
    main()
