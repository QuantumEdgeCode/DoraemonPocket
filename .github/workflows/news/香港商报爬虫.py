#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
hkcd_crawler.py — 爬取 https://www.hkcd.com.hk/ (香港商报 / Hong Kong Commercial Daily, 中国香港)
============================================================================================================
方法（静态优先 / Playwright 降级，沿用统一约定）
  - 文章 URL 形如  https://www.hkcd.com.hk/hkcdweb/content/2026/07/30/content_8767449.html
                   路径含发布日期（YYYY/MM/DD），content_ 后为纯数字 ID，唯一
  - 发现来源（去重合并）：
        * robots.txt 仅 Disallow: /wc，其余均允许
        * 官方 sitemap.xml 只有一个 node 入口，无用 → 改为「首页 + 栏目页 + 分页」遍历式发现
        * 首页直接暴露 ~200+ 篇最新文章链接（/hkcdweb/content/.../content_NNNNN.html）
        * 栏目入口来自首页 / 栏目页内的 node_N.html 与 aboutNewsTopic/topics/index.php?id=N
        * 栏目页内若含「下一页 / 数字分页」链接则顺延抓取（--max-pages 控制每栏目页数）
        文章 ID 由 URL 中的 content_(数字) 提取，跨页去重
  - 正文解析（BeautifulSoup）：
        * 标题：<title> 去掉 " - 香港商报" / " - 香港商報" 后缀（页面无稳定 <h1>）
        * 时间：正文文本中的 "2026-07-30 17:42" 或 "2026年07月30日 17:42"；
                缺失则用 URL 路径日期兜底（香港时区 +08:00）
        * 栏目：来自发现该文章的「栏目页」名称（<title> 去后缀）；首页发现记「要闻」
        * 作者：页面署名（如有），否则记「香港商报」
        * 正文：选取正文字数最多的 div/td/article/section 块（排除 nav/footer/side 等），
                取其下全部 <p> 文本拼接；正文过短则回退用整块纯文本
        * 图片：og:image 头图 + 正文块内 <img>（过滤 logo/图标/1x1 像素）
        * 摘要：og:description，否则取正文前 ~120 字
  - 合规：香港商报内容版权归原社所有；本脚本仅用于个人学习/研究，禁止商用转发。
          请求间隔默认 1s， polite crawling。

输出：data/新闻/hkcd_collection.json  （统一 schema 列表，兼容 新闻数据处理.py）

用法：
  python 香港商报爬虫.py                  # 全量发现（默认每栏目最多 3 页）
  python 香港商报爬虫.py --limit 50       # 控量（仅抓前 N 篇）
  python 香港商报爬虫.py --no-detail      # 仅采集链接，不抓正文
  python 香港商报爬虫.py --delay 2        # 请求间隔（秒）
  python 香港商报爬虫.py --max-pages 5    # 每栏目最多翻页数
  python 香港商报爬虫.py --playwright     # 强制 Playwright 渲染（一般无需）
"""

import argparse
import html as _html
import json
import os
import re
import time
import sys
from datetime import datetime, timezone, timedelta
from urllib.parse import urljoin, urlparse

import requests
from bs4 import BeautifulSoup

try:
    from playwright.sync_api import sync_playwright
    HAVE_PW = True
except Exception:
    HAVE_PW = False

BASE = "https://www.hkcd.com.hk"
# 输出路径以脚本所在目录为基准，避免依赖当前工作目录
_OUTPUT_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "data", "新闻")
OUTPUT = os.path.join(_OUTPUT_DIR, "hkcd_collection.json")
HK = timezone(timedelta(hours=8))  # 香港 +08:00

HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                  "(KHTML, like Gecko) Chrome/124.0 Safari/537.36",
    "Accept-Language": "zh-CN,zh;q=0.9,en;q=0.8",
}

ARTICLE_RE = re.compile(r"/hkcdweb/content/\d{4}/\d{2}/\d{2}/content_(\d+)\.html", re.I)
SECTION_TITLE_SUFFIX = re.compile(r"\s*[-–|]\s*(香港商報|香港商报|HKCD).*$", re.I)
TIME_RE = re.compile(r"(\d{4})[-/年](\d{1,2})[-/月](\d{1,2})[日]?\s*(\d{1,2}):(\d{2})")
URL_DATE_RE = re.compile(r"/hkcdweb/content/(\d{4})/(\d{2})/(\d{2})/")

# 栏目页链接特征（排除文章、静态资源、外链）
SECTION_RE = re.compile(r"(?:/hkcdweb/.*/node_\d+\.html$|aboutNewsTopic/topics/index\.php\?id=\d+)", re.I)

NAV_FILTER = re.compile(r"(首页|要闻|关于我们|版权|电话|邮箱|联系我们|copyright|all rights|关注我们|扫一扫)", re.I)


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


def fetch_playwright(url):
    if not HAVE_PW:
        return None, False
    try:
        with sync_playwright() as p:
            b = p.chromium.launch(headless=True, args=["--no-sandbox"])
            pg = b.new_page()
            pg.goto(url, timeout=35000, wait_until="domcontentloaded")
            h = pg.content()
            b.close()
            return h, True
    except Exception:
        return None, False


# ---------------------------------------------------------------------------
# 发现：首页 + 栏目页 + 分页
# ---------------------------------------------------------------------------
def section_name_from_html(html):
    """从栏目页 <title> 提取栏目名（去后缀）。"""
    if not html:
        return "要闻"
    m = re.search(r"<title>(.*?)</title>", html, re.S)
    if m:
        name = _html.unescape(m.group(1).strip())
        name = SECTION_TITLE_SUFFIX.sub("", name).strip()
        if name:
            return name
    return "要闻"


def collect_listing_links(html, base_url):
    """从栏目页 HTML 提取文章链接 + 可能的分页/子栏目链接。"""
    soup = BeautifulSoup(html, "html.parser")
    articles = {}      # url -> None（只收集，栏目名在外层补）
    next_pages = []    # 分页 / 子栏目链接
    for a in soup.find_all("a", href=True):
        href = a["href"].strip()
        if not href or href.startswith(("javascript", "#", "mailto:")):
            continue
        absurl = urljoin(base_url, href)
        if not absurl.startswith(BASE):
            continue
        low = absurl.lower()
        if ARTICLE_RE.search(absurl):
            articles[absurl.split("?")[0]] = None
        elif SECTION_RE.search(absurl) and "content_" not in low:
            # 栏目页或分页（如 node_49_1.html / ?page=2 / 数字分页）
            txt = a.get_text(strip=True)
            if re.fullmatch(r"\d+", txt) or txt in ("下一页", "下页", "Next", "更多"):
                next_pages.append(absurl.split("?")[0])
    return articles, next_pages


def discover(max_pages=3):
    """返回 {url: section_name}，去重。"""
    found = {}                  # url -> section
    seen_section_pages = set()  # 已抓的栏目/分页页，防环

    # 首页
    home, ok = fetch(BASE + "/")
    home_section = "要闻"
    if ok:
        arts, nxt = collect_listing_links(home, BASE + "/")
        for u in arts:
            found.setdefault(u, home_section)
        # 收集首页里的栏目入口（一次性，不视为分页）
        soup = BeautifulSoup(home, "html.parser")
        section_entries = []  # (url, section_name)
        for a in soup.find_all("a", href=True):
            href = a["href"].strip()
            if not href or href.startswith(("javascript", "#", "mailto:")):
                continue
            absurl = urljoin(BASE + "/", href)
            if SECTION_RE.search(absurl) and "content_" not in absurl.lower():
                nm = a.get_text(strip=True)
                nm = SECTION_TITLE_SUFFIX.sub("", nm).strip() if nm else ""
                section_entries.append((absurl.split("?")[0], nm or None))
    else:
        print("[list] 首页抓取失败")
        section_entries = []

    # 逐栏目页（含分页）
    for sec_url, sec_hint in section_entries:
        if sec_url in seen_section_pages:
            continue
        seen_section_pages.add(sec_url)
        html, ok = fetch(sec_url)
        if not ok:
            continue
        sec_name = sec_hint or section_name_from_html(html)
        arts, nxt = collect_listing_links(html, sec_url)
        for u in arts:
            found.setdefault(u, sec_name)
        # 分页顺延
        for pi, pnext in enumerate(nxt, 1):
            if pi > max_pages:
                break
            if pnext in seen_section_pages:
                continue
            seen_section_pages.add(pnext)
            ph, pok = fetch(pnext)
            if not pok:
                continue
            pa, _ = collect_listing_links(ph, pnext)
            for u in pa:
                found.setdefault(u, sec_name)

    return found


# ---------------------------------------------------------------------------
# 时间 / 栏目解析
# ---------------------------------------------------------------------------
def parse_time(html, url):
    m = TIME_RE.search(html or "")
    if m:
        try:
            dt = datetime(int(m.group(1)), int(m.group(2)), int(m.group(3)),
                          int(m.group(4)), int(m.group(5)), tzinfo=HK)
            return dt.isoformat()
        except Exception:
            pass
    # 兜底：URL 路径日期
    um = URL_DATE_RE.search(url or "")
    if um:
        try:
            dt = datetime(int(um.group(1)), int(um.group(2)), int(um.group(3)), tzinfo=HK)
            return dt.isoformat()
        except Exception:
            pass
    return None


# ---------------------------------------------------------------------------
# 正文解析
# ---------------------------------------------------------------------------
def extract_content(soup):
    """选取正文字数最多的块（排除导航/页脚/侧栏），返回拼接后的正文。"""
    best, best_len = soup, 0
    for el in soup.find_all(["div", "td", "article", "section"]):
        cls_id = " ".join(filter(None, [str(el.get("class")), str(el.get("id"))])).lower()
        if any(k in cls_id for k in ["nav", "foot", "header", "side", "banner",
                                     "menu", "comment", "ad", "related", "share",
                                     "crumb", "bread"]):
            continue
        t = el.get_text(strip=True)
        if len(t) > best_len:
            best_len, best = len(t), el
    ps = best.find_all("p")
    paras = [p.get_text(" ", strip=True) for p in ps]
    paras = [p for p in paras if len(p) >= 8 and not NAV_FILTER.search(p)]
    text = "\n\n".join(paras)
    if len(text) < 50:
        # 回退：整块纯文本，按行清理
        raw = best.get_text("\n", strip=True)
        lines = [ln.strip() for ln in raw.split("\n") if len(ln.strip()) >= 6]
        text = "\n\n".join(lines)
    return text


# 真实新闻图源：img.hkcd.com 图床 / content 路径下用户上传图
REAL_IMG = re.compile(r"img\.hkcd\.com|/hkcdweb/content/|/content_app/|/userfiles/", re.I)
# 页脚/侧栏/社交图标等站点装饰图（非正文内容）
CHROME_IMG = re.compile(
    r"(images2023/|placeholder|er\.php|/logo|icon|banner|wechat|slogan|rankTitle|"
    r"wap\.jpg|app\.jpg|meitijuzhen|shipinghao|kuaishou|jinritoutiao|\.gif$)", re.I)


def extract_images(soup, title):
    """仅保留真实新闻图（img.hkcd.com 图床 / content|userfiles 路径），过滤页脚社交图标。"""
    images = []
    lead = ""
    og = soup.find("meta", attrs={"property": "og:image"})
    if og and og.get("content"):
        lead = og["content"]
        if lead.startswith("//"):
            lead = "https:" + lead
    for im in soup.find_all("img"):
        src = im.get("src") or im.get("data-src") or im.get("data-original") or ""
        if not src:
            continue
        if src.startswith("//"):
            src = "https:" + src
        elif src.startswith("/"):
            src = BASE + src
        if not src.startswith("http"):
            continue
        if CHROME_IMG.search(src):
            continue
        if not REAL_IMG.search(src):
            continue
        images.append({"url": src, "caption": im.get("alt", "") or ""})
    if lead and not CHROME_IMG.search(lead) and REAL_IMG.search(lead) \
            and not any(i["url"] == lead for i in images):
        images.insert(0, {"url": lead, "caption": title or ""})
    # 去重
    seen, uniq = set(), []
    for im in images:
        if im["url"] not in seen:
            seen.add(im["url"])
            uniq.append(im)
    return uniq


def parse_article(url, section, use_playwright=False):
    html, ok = fetch(url)
    if not ok and use_playwright:
        html, ok = fetch_playwright(url)
    if not ok:
        return None
    soup = BeautifulSoup(html, "html.parser")

    # 标题
    title = ""
    if soup.title:
        title = _html.unescape(soup.title.get_text(strip=True))
        title = SECTION_TITLE_SUFFIX.sub("", title).strip()
    if not title:
        h = soup.find(["h1", "h2"])
        if h:
            title = h.get_text(strip=True)

    # 正文
    content = extract_content(soup)
    if not content or len(content) < 30:
        return None  # 图集/视频/不可达

    # 时间
    published_at = parse_time(html, url)

    # 作者（尽力而为）
    author = "香港商报"
    mt = re.search(r"(?:记者|编辑|作者)[：:]\s*([^\s/|<]{2,12})", html)
    if mt:
        author = mt.group(1)

    # 摘要
    summary = ""
    og = soup.find("meta", attrs={"property": "og:description"}) or soup.find("meta", attrs={"name": "description"})
    if og and og.get("content"):
        summary = _html.unescape(og["content"]).strip()
    if not summary and content:
        summary = content[:120]

    images = extract_images(soup, title)

    m = ARTICLE_RE.search(url)
    aid = m.group(1) if m else None

    return {
        "id": aid,
        "url": url,
        "title": title,
        "section": section or "",
        "author": author,
        "published_at": published_at,
        "summary": summary,
        "content": content,
        "images": images,
        "premium": False,
        "language": "zh",
    }


# ---------------------------------------------------------------------------
# 原子落盘
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


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--limit", type=int, default=None, help="最多抓多少篇（默认全部）")
    ap.add_argument("--no-detail", action="store_true", help="仅采集链接不抓正文")
    ap.add_argument("--delay", type=float, default=1.0, help="请求间隔秒")
    ap.add_argument("--max-pages", type=int, default=3, help="每栏目最多翻页数")
    ap.add_argument("--playwright", action="store_true", help="强制 Playwright 渲染")
    args = ap.parse_args()

    articles = []
    try:
        print("[list] 发现文章链接（首页 + 栏目页 + 分页）...")
        links = discover(max_pages=args.max_pages)
        print("[list] 发现唯一文章：{0} 篇".format(len(links)))

        items = list(links.items())
        if args.limit:
            items = items[:args.limit]

        for i, (url, section) in enumerate(items, 1):
            print("[{0}/{1}] 解析：{2}".format(i, len(items), url))
            if args.no_detail:
                articles.append({"id": (ARTICLE_RE.search(url).group(1) if ARTICLE_RE.search(url) else None),
                                 "url": url, "title": None, "section": section})
                atomic_save(articles, OUTPUT)
                continue
            art = parse_article(url, section, use_playwright=args.playwright)
            if art and art.get("content"):
                articles.append(art)
                print("        ✓ 标题={0} 正文={1}字 图={2}张 栏目={3} 时间={4}".format(
                    (art["title"][:36] if art["title"] else None),
                    len(art["content"]),
                    len(art["images"]),
                    art["section"],
                    (art["published_at"][:16] if art["published_at"] else "无"),
                ))
            else:
                print("        ✗ 无正文（图集/视频/不可达）")
            atomic_save(articles, OUTPUT)
            time.sleep(args.delay)

        print("\n[done] 已保存 {0} 篇 → {1}".format(len(articles), OUTPUT))
    except KeyboardInterrupt:
        atomic_save(articles, OUTPUT)
        print("\n[interrupted] 已实时保存至当前进度（{0} 篇）-> {1}".format(len(articles), OUTPUT))
        sys.exit(130)
    except Exception as exc:
        atomic_save(articles, OUTPUT)
        print("\n[error] 抓取异常，已保存当前进度（{0} 篇）-> {1}".format(len(articles), OUTPUT))
        raise


if __name__ == "__main__":
    main()
