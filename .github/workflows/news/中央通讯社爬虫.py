#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
cna_crawler.py — 爬取 https://www.cna.com.tw/ (中央通讯社 CNA, 中国台湾)
============================================================================
方法（双引擎：静态优先 / Playwright 降级，沿用十站统一约定）
  - 文章 URL 形如  https://www.cna.com.tw/news/<栏目代码>/<YYYYMMDD><NNNN>.aspx
                   例：/news/aopl/202607290007.aspx  （12 位数字 = 文章 ID）
  - 发现来源（去重合并）：
        sitemapindex.xml → 13 个分类子 sitemap（各 ~1000 条，合计 ~13k 条）
        子 sitemap 命名：sitemap_fromRemote_<code>.xml
          code: aipl(政治) aopl(國際) acn(兩岸) aie(產經) asc(社會) ait(科技)
                ahel(生活) asoc(運動) aloc(地方) acul(文化) aspt(娛樂) amov(影劇)
                newstopic_Topic(專題，非单篇，已剔除)
  - 正文容器：div.paragraph（内含 <p>，干净无页脚噪音）
  - 配图：figure.floatImg img（懒加载 data-src）
           · 头图：figure.floatImg.center > div.fullPic
           · 内文图：figure.floatImg.center > div.media > div.paragraph
           两者均位于文章主栏；侧栏“相关新闻/闲置新闻”配图（someBox/idleNews）
           已自动排除（那些 img 不在 figure.floatImg 内）
  - 时间：meta article:published_time（已是 +08:00 台湾时间）
           → article:modified_time → sitemap lastmod
  - 标题：<h1> 即干净标题；栏目中文名取 <title>/og:title 的「| 國際 |」段
  - 作者/署名：meta author 仅为出版方「中央通訊社」；真实署名内嵌于首段
           「（中央社…報導）」，用正则提取为 author

合规：中央通讯社（中国台湾）内容版权归原社所有；本脚本仅用于个人学习/研究，
禁止商用转发。robots.txt 仅作提示，不阻断抓取（用户既定偏好）。

用法：
  python cna_crawler.py                  # 全量（默认 limit=800）
  python cna_crawler.py --limit 50       # 控量
  python cna_crawler.py --no-detail      # 仅采集链接，不抓正文
  python cna_crawler.py --root <URL>     # 追加起始页
  python cna_crawler.py --delay 3        # 请求间隔（秒）
  python cna_crawler.py --playwright     # 强制 Playwright 渲染（一般无需）
"""

import argparse
import json
import os
import re
import time
from datetime import datetime, timezone, timedelta
from urllib.parse import urlparse

import requests
from bs4 import BeautifulSoup

try:
    from playwright.sync_api import sync_playwright
    HAVE_PW = True
except Exception:
    HAVE_PW = False

BASE = "https://www.cna.com.tw"
SITEMAP_INDEX = BASE + "/sitemapindex.xml"
OUTPUT = "data/新闻/cna_collection.json"
HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                  "(KHTML, like Gecko) Chrome/124.0 Safari/537.36",
    "Accept-Language": "zh-TW,zh;q=0.9,en;q=0.8",
}

# 文章 URL：/news/<code>/<12位数字>.aspx
ARTICLE_RE = re.compile(
    r"https?://(?:www\.)?cna\.com\.tw/news/([a-z]+)/(\d{12})\.aspx", re.I
)

# 栏目代码 → 中文名（仅作展示，真实栏目名同时取自 <title> 标签，二者取一即可）
SECTION_CN = {
    "aipl": "政治", "aopl": "國際", "acn": "兩岸", "aie": "產經",
    "asc": "社會", "ait": "科技", "ahel": "生活", "asoc": "運動",
    "aloc": "地方", "acul": "文化", "aspt": "娛樂", "amov": "影劇",
}

# 图片噪音（站务 UI / 广告 / 占位）
NOISE_IMG = [
    "/website/img/", "appstore", "googleplay", "support.svg",
    "pic_fb.jpg", "/ad/", "logo", "google-news.png",
]

# 真实文章配图域名特征
REAL_IMG = ("imgcdn.cna.com.tw/www/WebPhotos", "imgcdn.cna.com.tw/www/webphotos")

# 署名正则：（中央社…報導 / 專電 / 編譯 / 綜合外電報導）
BYLINE_RE = re.compile(r"（中央社[^）]*?）")


# ---------------------------------------------------------------------------
# 网络获取
# ---------------------------------------------------------------------------
def fetch(url):
    try:
        r = requests.get(url, headers=HEADERS, timeout=25)
        if r.status_code == 200:
            return r.text, True
        return None, False
    except Exception:
        return None, False


def fetch_playwright(url):
    if not HAVE_PW:
        return None, False
    try:
        with sync_playwright() as p:
            b = p.chromium.launch(headless=True)
            pg = b.new_page()
            pg.goto(url, timeout=35000, wait_until="networkidle")
            html = pg.content()
            b.close()
            return html, True
    except Exception:
        return None, False


# ---------------------------------------------------------------------------
# 链接采集
# ---------------------------------------------------------------------------
def collect_links(extra_roots=None):
    links = {}  # url -> (aid, section_code)

    # sitemapindex.xml → 13 个子 sitemap
    idx, ok = fetch(SITEMAP_INDEX)
    if ok:
        for loc in re.findall(r"<loc>(.*?)</loc>", idx):
            if "newstopic" in loc:
                continue  # 专题聚合页，非单篇
            sub, sok = fetch(loc)
            if not sok:
                continue
            for u in re.findall(r"<loc>(.*?)</loc>", sub):
                m = ARTICLE_RE.match(u)
                if m:
                    u2 = u.split("?")[0].rstrip("/")
                    links.setdefault(u2, (m.group(2), m.group(1)))
    else:
        print("[list] 无法读取 sitemapindex.xml")

    # 可选：追加栏目页 SSR 内嵌链接
    if extra_roots:
        for root in extra_roots:
            html, rok = fetch(root)
            if not rok:
                continue
            for m in ARTICLE_RE.finditer(html):
                u2 = m.group(0).split("?")[0].rstrip("/")
                links.setdefault(u2, (m.group(2), m.group(1)))

    return links


# ---------------------------------------------------------------------------
# 文章解析
# ---------------------------------------------------------------------------
def parse_time(s):
    if not s:
        return None
    s = s.strip()
    m = re.search(r"(\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2})([+-]\d{2}:?\d{2}|Z)?", s)
    if m:
        dt = m.group(1) + (m.group(2) or "")
        dt = dt.replace("Z", "+00:00")
        try:
            # 台湾时区 +08:00，字符串已自带，优先使用；缺失则补 +08:00
            d = datetime.fromisoformat(dt)
            if d.tzinfo is None:
                d = d.replace(tzinfo=timezone(timedelta(hours=8)))
            return d.isoformat()
        except Exception:
            pass
    try:
        from email.utils import parsedate_to_datetime
        dt = parsedate_to_datetime(s)
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone(timedelta(hours=8)))
        return dt.isoformat()
    except Exception:
        return None


def section_of(url, title_text):
    """返回 (section_cn, section_code)"""
    m = ARTICLE_RE.match(url)
    code = m.group(1) if m else None
    cn = SECTION_CN.get(code) if code else None
    # 从 <title>/og:title 的「| 國際 |」段提取权威中文栏目名
    if title_text:
        parts = [p.strip() for p in title_text.split("|")]
        if len(parts) >= 3:
            cand = parts[1]
            if cand and cand != "中央社 CNA":
                cn = cand
    return cn, code


def parse_article(url, aid, use_playwright=False):
    html, ok = fetch(url)
    if not ok and use_playwright:
        html, ok = fetch_playwright(url)
    if not ok:
        return None

    soup = BeautifulSoup(html, "html.parser")

    # 标题：<h1> 最干净；回退 og:title / <title>
    h1 = soup.find("h1")
    title = h1.get_text(" ", strip=True) if h1 else None
    og_title = soup.find("meta", attrs={"property": "og:title"})
    title_text = (og_title.get("content") if og_title else None) or (
        soup.title.get_text(strip=True) if soup.title else None
    )
    if not title:
        # og:title 形如「標題 | 國際 | 中央社 CNA」→ 取第一段
        if title_text:
            title = title_text.split("|")[0].strip()
    if not title:
        title = title_text

    sec_cn, sec_code = section_of(url, title_text)

    # 时间
    pub = None
    for prop in ("article:published_time", "article:modified_time"):
        m = soup.find("meta", attrs={"property": prop})
        if m:
            pub = parse_time(m.get("content"))
            if pub:
                break
    if not pub:
        lm = re.search(r"<lastmod>(.*?)</lastmod>", html)
        if lm:
            pub = parse_time(lm.group(1))

    # 摘要
    desc = soup.find("meta", attrs={"name": "description"}) or soup.find(
        "meta", attrs={"property": "og:description"}
    )
    summary = desc.get("content") if desc else None

    # 正文：div.paragraph 内 <p>/<h2>/<h3>/<blockquote>
    body = soup.select("div.paragraph")
    paragraphs = []
    if body:
        for para in body:
            for tag in para.find_all(["p", "h2", "h3", "blockquote"]):
                txt = tag.get_text(" ", strip=True)
                if txt:
                    paragraphs.append(txt)
    content = "\n\n".join(paragraphs)

    # 作者/署名：meta author 仅出版方；优先从首段提取「（中央社…報導）」
    author = "中央通訊社"
    if content:
        mb = BYLINE_RE.search(content)
        if mb:
            author = mb.group(0)

    # 图片：figure.floatImg（头图 div.fullPic + 内文 div.media），懒加载 data-src
    images = []
    for fig in soup.select("figure.floatImg"):
        im = fig.find("img")
        if not im:
            continue
        src = im.get("data-src") or im.get("src") or ""
        if not src or any(n in src for n in NOISE_IMG):
            continue
        if not any(k in src for k in REAL_IMG):
            continue
        if src in images:
            continue
        # 配图说明
        cap = ""
        fc = fig.find("figcaption")
        if fc:
            cap = fc.get_text(" ", strip=True)
        else:
            for el in fig.find_all(True):
                cs = " ".join(el.get("class") or []).lower()
                if ("photo" in cs or "caption" in cs or "pic" in cs) and el.name not in ("img", "picture"):
                    t = el.get_text(" ", strip=True)
                    if 0 < len(t) < 200:
                        cap = t
                        break
        images.append({"url": src, "caption": cap})

    # 付费墙诚实标记（CNA 基本免费，仅作兜底）
    premium = False
    if len(content) < 200:
        if any(k in html for k in ["登入", "會員", "訂閱"]):
            premium = True

    return {
        "id": aid,
        "url": url,
        "title": title,
        "section": sec_cn,
        "section_code": sec_code,
        "author": author,
        "published_at": pub,
        "summary": summary,
        "content": content,
        "images": images,
        "premium": premium,
    }


# ---------------------------------------------------------------------------
# 原子保护性写入
# ---------------------------------------------------------------------------
def atomic_save(items, path=OUTPUT):
    """原子保护性写入：先写 .tmp 临时文件，flush+fsync 落盘后 os.replace 改名，
    杜绝中途崩溃留下半截 JSON。每抓一篇即实时落盘，进程被杀也不丢已采集数据。"""
    d = os.path.dirname(path)
    if d:
        os.makedirs(d, exist_ok=True)
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(items, f, ensure_ascii=False, indent=2)
        f.flush()
        os.fsync(f.fileno())
    os.replace(tmp, path)


# ---------------------------------------------------------------------------
# 主流程
# ---------------------------------------------------------------------------

def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--limit", type=int, default=800)
    ap.add_argument("--no-detail", action="store_true")
    ap.add_argument("--root", action="append", default=[])
    ap.add_argument("--delay", type=float, default=3.0)
    ap.add_argument("--playwright", action="store_true")
    args = ap.parse_args()


    print("[list] 采集文章链接（sitemapindex → 13 个子 sitemap）...")
    links = collect_links(extra_roots=args.root)
    print("[list] 发现唯一文章：{0} 篇".format(len(links)))

    items = list(links.items())
    if args.limit:
        items = items[: args.limit]

    articles = []
    try:
        for i, (url, (aid, _code)) in enumerate(items, 1):
            print("[{0}/{1}] 解析：{2}".format(i, len(items), url))
            if args.no_detail:
                articles.append({"id": aid, "url": url, "title": None})
                atomic_save(articles, OUTPUT)
                continue
            art = parse_article(url, aid, use_playwright=args.playwright)
            if art and art.get("content"):
                articles.append(art)
                atomic_save(articles, OUTPUT)
                print("        ✓ 标题={0} 正文={1}字 图={2}张 栏目={3}({4}) 署名={5}".format(
                    (art["title"][:30] if art["title"] else None),
                    len(art["content"]),
                    len(art["images"]),
                    art["section"],
                    art["section_code"],
                    art["author"][:24],
                ))
            else:
                print("        ✗ 无正文（可能为纯图集/视频/不可达）")
            time.sleep(args.delay)
    except (KeyboardInterrupt, Exception) as exc:
        print("\n[!] 抓取中断（{0}），已保存至当前进度 -> {1}".format(type(exc).__name__, OUTPUT))
        atomic_save(articles, OUTPUT)
        raise

    atomic_save(articles, OUTPUT)
    print("\n[done] 已保存 {0} 篇 → {1}".format(len(articles), OUTPUT))


if __name__ == "__main__":
    main()
