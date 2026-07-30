# -*- coding: utf-8 -*-
"""
世界报爬虫（Le Monde / 法国《世界报》中文版-Le Monde.fr）
============================================================================
站点特点（实测 2026-07-30）：
  - 法国主流大报，Arc Publishing 架构，法文。
  - 首页 / sitemap 开放（200），但【单篇正文页被 PerimeterX 反爬 402 拦截】：
        * requests（含完整浏览器头 + 首页 cookie + Referer）→ 402「Accès restreint」
        * Playwright 无头 / stealth → 卡在「Client Challenge」JS 挑战页，过不了
        * AMP 变体 → 同样 402
        * 挑战页仅 4199 字节且【内嵌零正文】，无法从墙页提取任何内容
    结论：本环境下【文章全文正文不可达】。
  - 唯一可用的内容通道是官方 RSS（免费、稳定）：
        * 各栏目 `https://www.lemonde.fr/<栏目>/rss_full.xml` 返回 20 条最新（200）
        * 首页要闻 `https://www.lemonde.fr/rss/une.xml` 返回 14 条（200）
        * 每条含 <title> 标题 / <link> 文章URL / <pubDate> 巴黎+02:00 / <description> 导语段
        * 无 content:encoded、无作者、无图片（故 content=导语，images=[]）
  - 因此本爬虫【以 RSS 为唯一数据源】，产出「标题 + 栏目 + 时间 + 导语」级新闻，
    不抓取被墙的正文页（避免无效请求与封禁）。
  - 如日后在干净住宅 IP + 真实浏览器环境能过 PerimeterX 挑战，可在 parse_article()
    补全文抓取（届时 images/author 也能补上）。

发现策略：
  - 遍历 SECTIONS 中各栏目 RSS（默认全部），每条目解析为一条新闻；
  - 跨栏目按文章 URL 去重（同一篇可能同时出现在 Une 与所属栏目）；
  - 结果按发布时间倒序排列。

输出：data/新闻/lemonde_collection.json  （统一 schema，兼容 新闻数据处理.py）

用法：
  python 世界报爬虫.py                         # 全量（默认全部栏目，每栏目最新 20 条）
  python 世界报爬虫.py --limit 50              # 控量（取最新 N 条）
  python 世界报爬虫.py --sections international,economie   # 只抓指定栏目
  python 世界报爬虫.py --delay 0.5             # 请求间隔（秒）
"""
import argparse
import json
import os
import re
import sys
import time
from datetime import datetime, timezone, timedelta
from email.utils import parsedate_to_datetime
from urllib.parse import urlparse

import requests
import warnings
from bs4 import BeautifulSoup
from bs4 import MarkupResemblesLocatorWarning

# RSS 描述去除标签后可能是纯文本/类 URL，BS 会误报警告， suppress 之
warnings.filterwarnings("ignore", category=MarkupResemblesLocatorWarning)

BASE = "https://www.lemonde.fr"
# 输出路径以脚本所在目录为基准，避免依赖当前工作目录
_OUTPUT_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "data", "新闻")
OUTPUT = os.path.join(_OUTPUT_DIR, "lemonde_collection.json")

HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                  "(KHTML, like Gecko) Chrome/124.0 Safari/537.36",
    "Accept-Language": "fr-FR,fr;q=0.9,en;q=0.8",
}

# 栏目：feed 路径片段 -> 栏目法文名（URL 中体现）。une 走 /rss/une.xml 特例
SECTIONS = {
    "une": "Une",
    "politique": "Politique",
    "international": "International",
    "economie": "Economie",
    "culture": "Culture",
    "sport": "Sport",
    "sciences": "Sciences",
    "planete": "Planète",
    "societe": "Société",
    "idees": "Idées",
    "education": "Education",
    "les-decodeurs": "Les Décodeurs",
}

# 文章 ID 提取（URL 形如 ..._6736942_3210.html，首位为文章 ID）
ARTICLE_ID_RE = re.compile(r"_(\d{6,})_\d+\.html", re.I)

CDATA_RE = re.compile(r"<!\[CDATA\[(.*?)\]\]>", re.S)
TAG_RE = re.compile(r"<[^>]+>")


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
# RSS 解析
# ---------------------------------------------------------------------------
def _unwrap(cdata_text):
    """去 CDATA 包裹并反转义。"""
    m = CDATA_RE.search(cdata_text)
    if m:
        return m.group(1)
    return cdata_text


def _clean_text(s):
    """去 HTML 标签 + 实体反转义 + 折叠空白。"""
    if not s:
        return ""
    s = _unwrap(s)
    s = TAG_RE.sub("", s)
    s = BeautifulSoup(s, "html.parser").get_text(" ")
    return re.sub(r"\s+", " ", s).strip()


def _extract(block, tag):
    """从单个 <item> 块里取某个标签的文本（处理 CDATA / 多行）。"""
    m = re.search(rf"<{tag}[^>]*>(.*?)</{tag}>", block, re.S)
    return _clean_text(m.group(1)) if m else ""


def parse_feed(url, section):
    """解析一个 RSS feed，返回该栏目下的新闻条目列表（未去重）。"""
    xml, ok = fetch(url)
    if not ok or not xml:
        print("        ✗ feed 获取失败：{0}".format(url))
        return []
    items = re.findall(r"<item>(.*?)</item>", xml, re.S)
    out = []
    for blk in items:
        title = _extract(blk, "title")
        link = _extract(blk, "link")
        pub = _extract(blk, "pubDate")
        desc = _extract(blk, "description")
        if not link:
            continue
        link = link.split("?")[0]  # 去追踪参数
        # 发布时间：RFC822 -> ISO（保留 +02:00 巴黎时区）
        published_at = None
        if pub:
            try:
                dt = parsedate_to_datetime(pub)
                if dt.tzinfo is None:
                    dt = dt.replace(tzinfo=timezone(timedelta(hours=2)))
                published_at = dt.isoformat()
            except Exception:
                published_at = None
        # 文章 ID
        mid = ARTICLE_ID_RE.search(link)
        art_id = mid.group(1) if mid else "lm-" + str(abs(hash(link)) % 10 ** 8)
        out.append({
            "id": art_id,
            "url": link,
            "title": title,
            "section": section,
            "author": None,            # RSS 不提供作者
            "published_at": published_at,
            "summary": desc,           # 导语
            "content": desc,           # 正文被墙，RSS 仅给导语级内容
            "images": [],              # RSS 不提供图片
            "language": "fr",
        })
    return out


# ---------------------------------------------------------------------------
# 原子落盘（实时保存，Ctrl+C 不丢数据）
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


# ---------------------------------------------------------------------------
# 主流程
# ---------------------------------------------------------------------------
def main():
    ap = argparse.ArgumentParser(description="Le Monde (世界报) RSS 爬虫")
    ap.add_argument("--limit", type=int, default=None, help="最多保存 N 条（取最新）")
    ap.add_argument("--sections", type=str, default=None,
                    help="只抓指定栏目，逗号分隔（如 international,economie）；默认全部")
    ap.add_argument("--delay", type=float, default=0.5, help="每栏目请求间隔（秒）")
    args = ap.parse_args()

    if args.sections:
        keys = [s.strip() for s in args.sections.split(",") if s.strip() in SECTIONS]
        if not keys:
            print("[!] 指定栏目均无效，可选：{0}".format(", ".join(SECTIONS)))
            return
    else:
        keys = list(SECTIONS.keys())

    print("[list] 采集栏目 RSS（Le Monde / 官方免费源，正文被反爬墙拦截故走 RSS）：")
    collected = {}   # url -> item（跨栏目去重，后者不覆盖前者）
    articles = []    # 受保护：链接采集阶段也受 Ctrl+C 保护
    try:
        for key in keys:
            if key == "une":
                feed = BASE + "/rss/une.xml"
            else:
                feed = BASE + "/{0}/rss_full.xml".format(key)
            print("  - {0} ({1})".format(SECTIONS[key], feed))
            items = parse_feed(feed, SECTIONS[key])
            for it in items:
                if it["url"] not in collected:
                    collected[it["url"]] = it
                    articles.append(it)
            time.sleep(args.delay)

        # 按发布时间倒序（最新在前）；无时间者排末尾
        def _sort_key(x):
            pa = x.get("published_at") or ""
            return (pa == "", pa)
        articles.sort(key=_sort_key, reverse=True)

        if args.limit:
            articles = articles[: args.limit]

        atomic_save(articles, OUTPUT)
        print("\n[done] 已保存 {0} 条 → {1}".format(len(articles), OUTPUT))
        # 栏目分布速览
        from collections import Counter
        dist = Counter(a["section"] for a in articles)
        print("      栏目分布：", dict(dist))
    except KeyboardInterrupt:
        atomic_save(articles, OUTPUT)
        print("\n[interrupted] 已实时保存至当前进度（{0} 条）→ {1}".format(len(articles), OUTPUT))
        sys.exit(130)
    except Exception:
        atomic_save(articles, OUTPUT)
        print("\n[error] 出错，已保存当前进度（{0} 条）→ {1}".format(len(articles), OUTPUT))
        raise


if __name__ == "__main__":
    main()
