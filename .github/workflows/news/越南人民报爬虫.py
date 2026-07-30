#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
越南人民报网（中文版 / Nhan Dan，越南共产党中央机关报）爬虫
============================================================================
站点特点
----------------------------------------------------------------------------
- 域名：https://cn.nhandan.vn （中文版；越南语主站为 nhandan.vn，另有 en/fr/ru/es/kr 多语种版）
- 文章 URL 形如：https://cn.nhandan.vn/article-post<ID>.html （ID 唯一）
- 发现策略（主）：站点 sitemap 索引 /sitemap.xml → 各月份 news-YYYY-M.xml 子地图，
  里面全是 article-post<ID>.html 链接；按月份倒序取最近若干个月（默认 3）。
  发现策略（兜底）：首页 + 栏目页内链。
- 数据极干净：标题/摘要/正文/时间/栏目/图片基本都在结构化标签或 JSON-LD / OG 里：
  * 标题   : h1.article__title  （JSON-LD NewsArticle.headline 兜底，<title> 再兜底）
  * 摘要   : div.article__sapo.cms-desc  （og:description 兜底）
  * 正文   : div.article__body.zce-content-body.cms-body 内全部 <p>/<blockquote>
  * 时间   : <meta article:published_time> 形如 2026-07-30T16:47:54+07:00（越南 +07:00，已含时区）
  * 栏目   : <meta article:section> 中文（如「文化」）；BreadcrumbList.name 兜底
  * 作者   : div.article__source 文本（多数通讯社稿件为空 → None）
  * 图片   : 正文中 <img> 真实图源为 https://cn-cdn.nhandan.vn/images/...；
             注意 src 多为 data:image/gif;base64 懒加载占位，真实地址在 data-src/data-original，
             需回退取后者并过滤占位；og:image 作为首图。
             图表新闻/图集类正文区无 <p> 文本，但含多张图，仍按 infographic 类型保留（type 字段标记）。
             输出：data/新闻/nhandan_collection.json （字段与项目统一 schema 对齐，额外含 type）
- 输出：data/新闻/nhandan_collection.json （字段与项目统一 schema 对齐）
- 合规：robots.txt 允许抓取（仅 disallow 打印页/搜索页/标签页），频率可控。

用法
----------------------------------------------------------------------------
  python 越南人民报爬虫.py                  # 全量（默认 limit=400，近 3 个月 sitemap）
  python 越南人民报爬虫.py --limit 50       # 控量
  python 越南人民报爬虫.py --sitemap-months 6   # 发现源回溯月份数
  python 越南人民报爬虫.py --no-detail      # 只采链接不抓正文
  python 越南人民报爬虫.py --delay 0.5      # 请求间隔（秒）
  中途 Ctrl+C 会优雅退出：打印 [interrupted] 已实时保存至当前进度（N 篇）→ 路径
============================================================================
"""

import argparse
import html as _html
import json
import os
import re
import sys
import time
from datetime import datetime, timezone, timedelta

import requests
from bs4 import BeautifulSoup

BASE = "https://cn.nhandan.vn"
# 输出路径以脚本所在目录为基准，避免依赖当前工作目录
_OUTPUT_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "data", "新闻")
OUTPUT = os.path.join(_OUTPUT_DIR, "nhandan_collection.json")

VN = timezone(timedelta(hours=7))  # 越南 +07:00

HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                  "(KHTML, like Gecko) Chrome/124.0 Safari/537.36",
    "Accept-Language": "zh-CN,zh;q=0.9,en;q=0.8",
}

ARTICLE_RE = re.compile(r"/article-post(\d+)\.html$", re.I)
NEWS_SITEMAP_RE = re.compile(r"/sitemaps/news-(\d{4})-(\d{1,2})\.xml$", re.I)

# 过滤非文章链接（栏目页 / 专题 / 静态页 / 外链等）
NON_ARTICLE_RE = re.compile(
    r"(/topic/|/tag\.html|/search|/about-us|/sitemap|/print\.html|"
    r"/rss|/\.html$|/multimedia/|/mega-story/|/infographic/|/photo-news/)",
    re.I,
)


# ---------------------------------------------------------------------------
# 网络获取（带重试）
# ---------------------------------------------------------------------------
def fetch(url, n=3):
    last = None
    for attempt in range(1, n + 1):
        try:
            r = requests.get(url, headers=HEADERS, timeout=25)
            if r.status_code == 200 and r.text:
                r.encoding = "utf-8"
                return r.text, True
            last = r.status_code
        except Exception as e:
            last = e
            time.sleep(min(2 ** attempt, 6))
    return None, False


# ---------------------------------------------------------------------------
# 链接发现
# ---------------------------------------------------------------------------
def collect_links(months=3, max_pool=4000):
    """返回 {article_id: url}。主源：sitemap 的 news-*.xml；兜底：首页内链。"""
    links = {}

    # --- 主源：sitemap 索引 ---
    idx_text, ok = fetch(BASE + "/sitemap.xml")
    if ok and idx_text:
        subs = re.findall(r"<loc>(.*?)</loc>", idx_text)
        news = []
        for s in subs:
            m = NEWS_SITEMAP_RE.search(s)
            if m:
                news.append((int(m.group(1)), int(m.group(2)), s))
        # 按年月倒序（最新的月份在前）
        news.sort(key=lambda x: (x[0], x[1]), reverse=True)
        for y, mo, s in news[:months]:
            xml, ok2 = fetch(s)
            if not ok2 or not xml:
                continue
            for loc in re.findall(r"<loc>(.*?)</loc>", xml):
                if len(links) >= max_pool:
                    break
                m = ARTICLE_RE.search(loc)
                if m:
                    links.setdefault(m.group(1), loc)
            if len(links) >= max_pool:
                break

    # --- 兜底：首页内链（sitemap 失败时仍可发现部分文章）---
    if not links:
        home, ok3 = fetch(BASE + "/")
        if ok3 and home:
            for m in ARTICLE_RE.finditer(home):
                # ARTICLE_RE 只匹配后缀，需要从完整 URL 提取
                pass
            soup = BeautifulSoup(home, "html.parser")
            for a in soup.find_all("a", href=True):
                href = a["href"]
                if href.startswith("/"):
                    href = BASE + href
                m = ARTICLE_RE.search(href)
                if m:
                    links.setdefault(m.group(1), href)

    return links


# ---------------------------------------------------------------------------
# 时间规范化：2026-07-30T16:47:54+07:00 → 同格式字符串
# ---------------------------------------------------------------------------
def normalize_time(raw, visible_text=None):
    if raw:
        try:
            dt = datetime.fromisoformat(raw)
            if dt.tzinfo is None:
                dt = dt.replace(tzinfo=VN)
            return dt.isoformat()
        except Exception:
            pass
    # 兜底：可见文本 2026年07月30日星期四 16:47
    if visible_text:
        m = re.search(
            r"(\d{4})年(\d{1,2})月(\d{1,2})日\D*(\d{1,2}):(\d{2})", visible_text
        )
        if m:
            try:
                dt = datetime(
                    int(m.group(1)), int(m.group(2)), int(m.group(3)),
                    int(m.group(4)), int(m.group(5)), tzinfo=VN,
                )
                return dt.isoformat()
            except Exception:
                pass
    return None


# ---------------------------------------------------------------------------
# 文章解析
# ---------------------------------------------------------------------------
def parse_article(url):
    html, ok = fetch(url)
    if not ok or not html:
        return None
    soup = BeautifulSoup(html, "html.parser")

    # 标题
    title = None
    h1 = soup.find("h1", class_=re.compile(r"article__title", re.I))
    if h1:
        title = h1.get_text(strip=True)
    if not title:
        for s in soup.find_all("script", type="application/ld+json"):
            txt = s.get_text()
            m = re.search(r'"headline"\s*:\s*"([^"]+)"', txt)
            if m:
                title = _html.unescape(m.group(1))
                break
    if not title and soup.title:
        title = soup.title.get_text(strip=True)
    if not title:
        return None

    # 摘要
    summary = None
    sapo = soup.find("div", class_=re.compile(r"article__sapo", re.I))
    if sapo:
        summary = sapo.get_text(" ", strip=True)
    if not summary:
        og = soup.find("meta", attrs={"property": "og:description"})
        if og and og.get("content"):
            summary = og["content"].strip()

    # 栏目
    section = None
    sec_meta = soup.find("meta", attrs={"property": "article:section"})
    if sec_meta and sec_meta.get("content"):
        section = sec_meta["content"].strip()
    if not section:
        for s in soup.find_all("script", type="application/ld+json"):
            m = re.search(r'"@type"\s*:\s*"BreadcrumbList".*?"name"\s*:\s*"([^"]+)"',
                          s.get_text(), re.S)
            if m:
                section = _html.unescape(m.group(1))
                break

    # 时间
    pub_raw = None
    pt = soup.find("meta", attrs={"property": "article:published_time"})
    if pt and pt.get("content"):
        pub_raw = pt["content"].strip()
    meta_div = soup.find("div", class_=re.compile(r"article__meta", re.I))
    visible_time = meta_div.get_text(" ", strip=True) if meta_div else None
    published_at = normalize_time(pub_raw, visible_time)

    # 作者（多数通讯社稿件为空）
    author = None
    src = soup.find("div", class_=re.compile(r"article__source", re.I))
    if src:
        t = src.get_text(" ", strip=True)
        t = re.sub(r"^\s*(来源|责编|编辑)\s*[:：]?\s*", "", t).strip()
        if t and "人民报" not in t and "nhandan" not in t.lower():
            author = t

    # 正文
    content = ""
    body = soup.find("div", class_=re.compile(r"article__body", re.I))
    if body:
        parts = []
        for el in body.find_all(["p", "blockquote"]):
            txt = el.get_text(" ", strip=True)
            if txt:
                parts.append(txt)
        content = "\n".join(parts)

    # 图片
    images = []
    seen = set()
    lead = None
    og = soup.find("meta", attrs={"property": "og:image"})
    if og and og.get("content"):
        lead = og["content"].strip()
    if body:
        for im in body.find_all("img"):
            # 优先取真实图源；本站 src 常为 data: 懒加载占位，需回退 data-src/data-original
            src_url = im.get("src") or ""
            if src_url.startswith("data:"):
                src_url = im.get("data-src") or im.get("data-original") or ""
            low = src_url.lower()
            if not src_url or low.startswith("data:") or "base64" in low:
                continue
            if "cn-cdn.nhandan.vn/images/" not in low:
                continue
            if src_url not in seen:
                seen.add(src_url)
                images.append({"url": src_url, "caption": im.get("alt", "") or title or ""})
    if lead and lead not in seen:
        images.insert(0, {"url": lead, "caption": title or ""})

    if not content:
        if images:
            # 图表新闻 / 图集类：正文区无 <p> 文本，内容以图片呈现，仍保留
            content = "[图表新闻] 本文以图表/信息图形式呈现，共 {0} 张图".format(len(images))
            art_type = "infographic"
        else:
            # 既无正文又无图：纯跳转页或抓取失败，跳过
            return None
    else:
        art_type = "article"

    aid = None
    m = ARTICLE_RE.search(url)
    if m:
        aid = m.group(1)

    return {
        "id": aid,
        "url": url,
        "title": title,
        "section": section or "未分类",
        "author": author,
        "published_at": published_at,
        "summary": summary or "",
        "content": content,
        "images": images,
        "type": art_type,
        "language": "zh",
    }


# ---------------------------------------------------------------------------
# 原子落盘（先写 .tmp 再 replace，避免中断损坏）
# ---------------------------------------------------------------------------
def atomic_save(articles, path=OUTPUT):
    d = os.path.dirname(path)
    if d:
        os.makedirs(d, exist_ok=True)
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(articles, f, ensure_ascii=False, indent=2)
        f.flush()
        os.fsync(f.fileno())
    os.replace(tmp, path)


def backup_existing_output(path):
    """写盘前若目标已存在，按 _01/_02/_03 递增重命名旧文件，绝不静默覆盖。

    与海湾新闻爬虫一致：最新一份永远叫规范原名，历史全部留存。
    """
    if not os.path.exists(path):
        return
    base, ext = os.path.splitext(path)
    n = 1
    while True:
        cand = "{0}_{1:02d}{2}".format(base, n, ext)
        if not os.path.exists(cand):
            break
        n += 1
    os.replace(path, cand)
    print("[backup] 已备份旧文件 → {0}".format(cand))


# ---------------------------------------------------------------------------
# 入口
# ---------------------------------------------------------------------------
def main():
    ap = argparse.ArgumentParser(
        description="越南人民报网（中文版）爬虫",
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    ap.add_argument("--limit", type=int, default=400, help="最多解析文章数（默认 400）")
    ap.add_argument("--sitemap-months", type=int, default=3, help="发现源回溯月份数（默认 3）")
    ap.add_argument("--delay", type=float, default=0.5, help="请求间隔秒数（默认 0.5）")
    ap.add_argument("--no-detail", action="store_true", help="只采链接不抓正文")
    args = ap.parse_args()

    print("[list] 采集文章链接（sitemap news-*.xml → 首页兜底）...")
    links = collect_links(months=args.sitemap_months)
    print("[list] 唯一文章：{0} 篇".format(len(links)))

    items = list(links.items())
    if args.limit:
        items = items[: args.limit]

    backup_existing_output(OUTPUT)  # 写盘前备份旧文件，绝不覆盖

    articles = []
    try:
        for i, (aid, url) in enumerate(items, 1):
            print("[{0}/{1}] 解析：{2}".format(i, len(items), url))
            if args.no_detail:
                articles.append({"id": aid, "url": url, "title": None})
                atomic_save(articles, OUTPUT)
                continue
            art = parse_article(url)
            if art is None:
                # 瞬时抓取失败（不可达），重试一次
                time.sleep(1.0)
                art = parse_article(url)
            if art and art.get("content"):
                articles.append(art)
                tag = "〔图表〕" if art.get("type") == "infographic" else ""
                print("        ✓{0} 标题={1} 正文={2}字 图={3}张 栏目={4} 时间={5}".format(
                    tag,
                    (art["title"][:34] if art["title"] else None),
                    len(art["content"]),
                    len(art["images"]),
                    art["section"],
                    (art["published_at"][:16] if art["published_at"] else "无"),
                ))
            else:
                print("        ✗ 跳过（无正文且无图 / 抓取失败）")
            atomic_save(articles, OUTPUT)  # 每篇实时落盘
            time.sleep(args.delay)

        print("\n[done] 已保存 {0} 篇 → {1}".format(len(articles), OUTPUT))
    except KeyboardInterrupt:
        atomic_save(articles, OUTPUT)
        print("\n[interrupted] 已实时保存至当前进度（{0} 篇）→ {1}".format(len(articles), OUTPUT))
        sys.exit(130)
    except Exception:
        atomic_save(articles, OUTPUT)
        print("\n[error] 异常中断，已保存当前进度（{0} 篇）→ {1}".format(len(articles), OUTPUT))
        raise


if __name__ == "__main__":
    main()
