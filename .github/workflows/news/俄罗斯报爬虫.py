#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
rg.ru (Российская газета) 爬虫 — 静态 HTML 双引擎(列表 + 详情)
==============================================================
站点特征:
- SSR 直出, 无 JS challenge / 无 IP 封锁(首页/文章页均 200)
- **RSS 全部 401**(auth 保护) -> 无法用 RSS, 只能爬 HTML
- **Next.js 站点**: class 名是 CSS-module 哈希(如 PageArticleContent_textWrapper__qjCKN),
  哈希会变, 但模块前缀 `ArticleContent` 稳定 -> 用正则 `ArticleContent` 定位正文容器
- **列表不分页**: `news.html?page=N` / `news/?page=N` 每层都返回同一批 21 篇(前端 JS API 加载更多,
  未逆向)。故"全量"= 首页(~20) + news.html(~21) 去重 ≈ 35 篇
- 时间: `<meta property="article:published_time">` = `2026-07-28T19:13:00+00:00` (**UTC, +00:00**),
  直接作为 ISO 使用, **不要**再加 +3h(与 TASS/RIA/mk/yna 的 MSK 不同!)
- 配图: `cdnstatic.rg.ru`, 同一图有多档 `/cropWxH/` 裁剪 -> 规范化(去 /cropWxH/)后去重, 优先原图

合规: 内容版权归 Российская газета, 仅限个人学习/研究, 禁止商用转发。间隔 3s。

用法:
    python rg_crawler.py                                  # 全量(首页+news.html, ≈35篇)
    python rg_crawler.py --root https://rg.ru/news.html    # 指定列表页
    python rg_crawler.py --limit 5                         # 调试前 N 条
"""

import argparse
import json
import os
import re
import sys
import time
from urllib.parse import urljoin, urlparse

import requests
from bs4 import BeautifulSoup

# ---------- 常量 ----------
SITE = "https://rg.ru"
SOURCE_NAME = "Российская газета"
UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36")
HEADERS = {
    "User-Agent": UA,
    "Accept-Language": "ru-RU,ru;q=0.9,en;q=0.8",
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
}
REQUEST_TIMEOUT = 20
SLEEP_SEC = 3  # 礼貌间隔

# 文章 URL: rg.ru/<YYYY>/<MM>/<DD>/<slug>.html
ARTICLE_RE = re.compile(r"^https?://(?:www\.)?rg\.ru/\d{4}/\d{2}/\d{2}/[\w\-]+\.html$")
# 默认列表页(静态可抓的两处)
DEFAULT_LIST_PAGES = ["https://rg.ru/", "https://rg.ru/news.html"]


# ---------- 工具 ----------
def log(msg):
    print(msg, flush=True)


def fetch(url, timeout=REQUEST_TIMEOUT):
    return requests.get(url, headers=HEADERS, timeout=timeout)


def is_article_url(u):
    return bool(ARTICLE_RE.match(u))


def _section_of(url):
    """RG 文章 URL 形如 /<YYYY>/<MM>/<DD>/<slug>.html(无栏目段);
    仅 region 类(如 /reg-szfo/...) 才有栏目。这里取路径首段, 若为4位年份则返回空。"""
    seg = [s for s in urlparse(url).path.split("/") if s]
    if not seg:
        return ""
    return "" if re.match(r"\d{4}$", seg[0]) else seg[0]


# ---------- 列表 ----------
def parse_list(pages):
    """从若干列表页抽取去重后的文章链接"""
    seen, items = set(), []
    for page in pages:
        try:
            r = fetch(page)
        except Exception as e:
            log(f"[列表] 请求失败 {page}: {e}")
            continue
        if r.status_code != 200:
            log(f"[列表] {page} -> HTTP {r.status_code}, 跳过")
            continue
        soup = BeautifulSoup(r.text, "lxml")
        n = 0
        for a in soup.find_all("a", href=True):
            full = urljoin(page, a["href"].strip())
            if is_article_url(full) and full not in seen:
                seen.add(full)
                items.append({
                    "title": a.get_text(strip=True),
                    "url": full,
                    "summary": "",
                    "publish_time": "",
                })
                n += 1
        log(f"[列表] {page} -> 新增 {n} 条 (累计 {len(items)})")
    return items


# ---------- 详情 ----------
def find_body(soup):
    """定位正文容器: 优先用首个实质 <p> 的 ArticleContent 祖先, 回退到最大 ArticleContent div"""
    for p in soup.find_all("p"):
        if len(p.get_text(strip=True)) > 40:
            for anc in p.find_parents():
                cls = " ".join(anc.get("class") or [])
                if re.search(r"ArticleContent", cls, re.I):
                    return anc
            break
    cand = soup.find_all(class_=re.compile(r"ArticleContent", re.I))
    if cand:
        return max(cand, key=lambda d: len(d.get_text(strip=True)))
    return None


def extract_images(soup, base_url):
    """提取 cdnstatic 配图, 规范化(去 crop)后去重, 优先原图"""
    raw = set()
    for i in soup.find_all("img"):
        for attr in ("src", "data-src", "srcset"):
            for tok in _collect_img_tokens(i.get(attr)):
                if "cdnstatic.rg.ru" in tok:
                    raw.add(tok)
    for pic in soup.find_all("picture"):
        for src in pic.find_all("source"):
            for tok in _collect_img_tokens(src.get("srcset")):
                if "cdnstatic.rg.ru" in tok:
                    raw.add(tok)
    seen = {}
    for u in raw:
        if "dummy" in u or "logo" in u.lower():
            continue
        # 规范化: 去掉 /cropWxH/ 段 -> 得到原图全分辨率 URL(已验证 200 可达)
        canon = re.sub(r"/crop\d+x\d+/", "/", u)
        seen[canon] = True
    return list(seen.keys())


def _collect_img_tokens(value):
    """从 src / data-src(空格分隔) 或 srcset(空格+逗号分隔, 含 '620w' 描述符) 提取 URL"""
    if not value:
        return []
    toks = re.split(r"[\s,]+", value.strip())
    return [t for t in toks if t.startswith("http")]


def parse_detail(url):
    """抓取详情页, 返回 {detail_title, content, images, publish_time}"""
    try:
        r = fetch(url)
    except Exception as e:
        log(f"  [详情] 请求失败 {url}: {e}")
        return {"detail_title": "", "content": "", "images": [], "publish_time": ""}

    soup = BeautifulSoup(r.text, "lxml")
    h1 = soup.find("h1")
    title = h1.get_text(strip=True) if h1 else ""

    # 时间: article:published_time (UTC, 已是合法 ISO+tz)
    pub = ""
    mt = soup.find("meta", property="article:published_time")
    if mt and mt.get("content"):
        pub = mt.get("content").strip()
        if not re.match(r"^\d{4}-\d{2}-\d{2}T", pub):
            pub = ""  # 格式异常则清空, 走兜底

    body = find_body(soup)
    paras, imgs = [], []
    if body:
        for p in body.find_all("p"):
            t = p.get_text(" ", strip=True)
            if t:
                paras.append(t)
        imgs = extract_images(body, url) or extract_images(soup, url)

    content = "\n\n".join(paras)

    # 兜底: 无正文用 meta description
    if not content:
        m = soup.find("meta", attrs={"name": "description"}) or soup.find(
            "meta", property="og:description")
        if m and m.get("content"):
            content = m.get("content").strip()

    return {
        "detail_title": title,
        "content": content,
        "images": imgs,
        "publish_time": pub,
    }


# ---------- 主流程 ----------
def atomic_save(data, path="data/新闻/rg_collection.json"):
    """JSON 原子保护性写入：先写 .tmp → flush+fsync → os.replace 改名，杜绝半截 JSON。

    每抓完一篇即调用，进程被中断（Ctrl+C / 断电）也只丢当前这一篇，已落盘数据不丢。
    """
    d = os.path.dirname(path)
    if d:
        os.makedirs(d, exist_ok=True)
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)
        f.flush()
        os.fsync(f.fileno())
    os.replace(tmp, path)


def main():
    ap = argparse.ArgumentParser(description="rg.ru crawler")
    ap.add_argument("--root", default=None,
                    help="指定列表页 URL (默认首页+news.html)。注意: RSS 全部 401 不可用")
    ap.add_argument("--limit", type=int, default=0, help="仅抓取前 N 条(调试)")
    ap.add_argument("--no-detail", action="store_true",
                    help="不抓详情, 仅输出列表级(标题/URL, 无正文)")
    ap.add_argument("--out", default="data/新闻/rg_collection.json")
    args = ap.parse_args()

    t0 = time.time()
    pages = [args.root] if args.root else DEFAULT_LIST_PAGES
    list_items = parse_list(pages)
    if not list_items:
        log("未获取到任何文章链接, 退出。")
        sys.exit(1)

    if args.limit:
        list_items = list_items[:args.limit]
    log(f"待处理 {len(list_items)} 篇 (no_detail={args.no_detail})")

    collected = []
    empty_detail = 0
    try:
        for idx, it in enumerate(list_items, 1):
            url = it["url"]
            if args.no_detail:
                detail = {"detail_title": it["title"], "content": it["summary"],
                          "images": [], "publish_time": ""}
                content_src = "list_only"
            else:
                detail = parse_detail(url)
                content_src = "fulltext" if detail["content"] else "empty"
                if not detail["content"]:
                    empty_detail += 1
                if idx < len(list_items):
                    time.sleep(SLEEP_SEC)

            record = {
                "title": it["title"],
                "url": url,
                "publish_time": detail["publish_time"] or it["publish_time"],
                "source": SOURCE_NAME,
                "section": _section_of(url),
                "summary": it["summary"],
                "detail_title": detail["detail_title"] or it["title"],
                "content": detail["content"],
                "content_source": content_src,
                "images": detail["images"],
            }
            collected.append(record)
            flag = f"[{content_src}]" if not args.no_detail else "[list]"
            log(f"  ({idx}/{len(list_items)}) {flag} {len(detail['content'])}字 "
                f"{len(detail['images'])}图 | {detail['detail_title'][:44] or it['title'][:44]}")
            # 每篇实时原子落盘（与收尾同结构包裹）
            atomic_save({
                "source": SOURCE_NAME,
                "site": SITE,
                "list_pages": pages,
                "crawled_at": time.strftime("%Y-%m-%dT%H:%M:%S+08:00"),
                "count": len(collected),
                "detail_html_accessible": True,
                "note": "RSS 全部 401; 列表不分页(前端 JS API), 全量=首页+news.html 去重 ≈35篇",
                "items": collected,
            }, args.out)
    except (KeyboardInterrupt, Exception) as e:
        log(f"\n[warn] 采集中断（{type(e).__name__}）：已实时保存至当前进度 -> {args.out}")
        raise

    out = {
        "source": SOURCE_NAME,
        "site": SITE,
        "list_pages": pages,
        "crawled_at": time.strftime("%Y-%m-%dT%H:%M:%S+08:00"),
        "count": len(collected),
        "detail_html_accessible": True,
        "note": "RSS 全部 401; 列表不分页(前端 JS API), 全量=首页+news.html 去重 ≈35篇",
        "items": collected,
    }
    atomic_save(out, args.out)  # 收尾原子保存

    dt = time.time() - t0
    log("=" * 60)
    log(f"完成: {len(collected)} 篇 -> {args.out} (耗时 {dt:.1f}s, 空正文 {empty_detail})")


if __name__ == "__main__":
    main()
