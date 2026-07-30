#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
巴黎人报（Le Parisien，法国）爬虫
============================================================================
站点特点
----------------------------------------------------------------------------
- 域名：https://www.leparisien.fr （法国日报，隶属 LVMH 旗下 Les Échos-Le
  Parisien 集团；技术栈为 Arc Publishing，与华盛顿邮报同款）
- 反爬：全站 Akamai 边缘防护，普通 requests（含完整浏览器头）一律 403
  「Access Denied」；真实浏览器（Playwright/Chromium，TLS 指纹正常）可过。
  → 本爬虫 fetch 采用「requests 优先、失败(403/Access Denied)自动回退
    Playwright(带反自动化伪造)」策略，干净 IP 机器上两种都可能成功。
- 文章 URL 形如：https://www.leparisien.fr/<栏目>/<YYYY>/<MM>/<DD>/<slug>-<id>.php
  （旧式：.../<栏目>-<地区>/...-<id>.php）。栏目路径如 politique / international /
  economie / societe / sport / culture / paris / faits-divers 等。
- 发现策略：因 sitemap(arc/outboundfeeds) 同样在 Akamai 后，改用
  「首页 + 核心栏目页」经 Playwright 加载后提取内链（按文章 URL 特征过滤去重）。
- 解析（Arc 通用结构，JSON-LD 为主 + 多兜底，无需依赖固定 class）：
  * 标题   : JSON-LD NewsArticle.headline → <h1> → og:title → <title>
  * 摘要   : JSON-LD description → og:description → <meta name=description>
  * 正文   : JSON-LD articleBody（若有）→ 候选内容容器(article__content /
             data-cy=article-body / article-body / .content / <article>) 内
             <p>+<h2>+<h3>；再兜底到「含最多 <p> 的容器」启发式
  * 时间   : JSON-LD datePublished（ISO 已含时区 +02:00 夏令/ +01:00 冬令）
             → meta article:published_time
  * 栏目   : JSON-LD articleSection（法语，如 Politique/Paris）→ breadcrumb
  * 作者   : JSON-LD author[].name → meta article:author
  * 图片   : JSON-LD image + 正文 <img>(data-src/src) 去 data:/tracking 占位
- 输出：data/新闻/leparisien_collection.json （字段与项目统一 schema 对齐）
- 合规：robots.txt 未禁止文章抓取（仅限制 AI 训练类 bot 与 /guide-shopping 等）；
  抓取频率可控；Playwright 已带合理等待与超时。

用法
----------------------------------------------------------------------------
  python 巴黎人报爬虫.py                  # 全量（默认 limit=300）
  python 巴黎人报爬虫.py --limit 50       # 控量
  python 巴黎人报爬虫.py --sections politique international   # 指定栏目
  python 巴黎人报爬虫.py --no-detail      # 只采链接不抓正文
  python 巴黎人报爬虫.py --delay 1        # 请求间隔（秒，作用于 Playwright 翻页）
  中途 Ctrl+C 会优雅退出：打印 [interrupted] 已实时保存至当前进度（N 篇）→ 路径
============================================================================
"""

import argparse
import json
import os
import re
import sys
import time
from datetime import datetime

import requests
from bs4 import BeautifulSoup

try:
    from playwright.sync_api import sync_playwright
    HAVE_PW = True
except Exception:
    HAVE_PW = False

BASE = "https://www.leparisien.fr"
# 输出路径以脚本所在目录为基准，避免依赖当前工作目录
_OUTPUT_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "data", "新闻")
OUTPUT = os.path.join(_OUTPUT_DIR, "leparisien_collection.json")

HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                  "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36",
    "Accept-Language": "fr-FR,fr;q=0.9,en;q=0.8",
    "Accept-Encoding": "gzip, deflate, br",
}

# 文章 URL 识别：含 /YYYY/MM/DD/... .php，或结尾 -<数字>.php（旧式）
ARTICLE_RE = re.compile(
    r"leparisien\.fr/[^?\s]*?(?:\d{4}/\d{2}/\d{2}/[^?\s]+\.php|-\d{5,}\.php)$", re.I)
# 排除非文章页
NON_ARTICLE_RE = re.compile(
    r"(/rss|/tag/|/auteur|/video/|/photo|/abonnement|/recherche|/static|/ajax|"
    r"/widget|/plan-du-site|/mentions|/contact|/cgv|/newsletter|/amp/|/live/)",
    re.I)

# 核心栏目（用于发现）
DEFAULT_SECTIONS = [
    "politique", "international", "economie", "societe", "sport", "culture",
    "paris", "faits-divers", "sante", "high-tech", "immobilier",
    "environnement", "education",
]

# Playwright 全局句柄
_PW = None
_BROWSER = None
_CTX = None
_PAGE = None


def _ensure_page():
    """懒启动 Playwright（带反自动化伪造）。"""
    global _PW, _BROWSER, _CTX, _PAGE
    if _PAGE is not None:
        return _PAGE
    if not HAVE_PW:
        raise RuntimeError("Playwright 未安装，无法绕过 Akamai；请先 pip install playwright 并 playwright install chromium")
    _PW = sync_playwright().start()
    _BROWSER = _PW.chromium.launch(
        headless=True,
        args=["--no-sandbox", "--disable-blink-features=AutomationControlled",
              "--disable-infobars"],
    )
    _CTX = _BROWSER.new_context(
        user_agent=HEADERS["User-Agent"],
        locale="fr-FR",
        viewport={"width": 1366, "height": 768},
    )
    # 反自动化伪造
    _CTX.add_init_script(
        "() => {"
        " Object.defineProperty(navigator, 'webdriver', { get: () => undefined });"
        " window.chrome = { runtime: {}, loadTimes: () => {}, csi: () => {} };"
        " const o = navigator.__proto__;"
        " Object.defineProperty(o, 'languages', { get: () => ['fr-FR','fr'] });"
        " Object.defineProperty(o, 'plugins', { get: () => [1,2,3] });"
        "}"
    )
    _PAGE = _CTX.new_page()
    return _PAGE


# ---------------------------------------------------------------------------
# 网络获取：requests 优先，被 Akamai 拦则回退 Playwright
# ---------------------------------------------------------------------------
def fetch_html(url, n=2):
    last = None
    for attempt in range(1, n + 1):
        # 1) 先试 requests
        try:
            r = requests.get(url, headers=HEADERS, timeout=25)
            if r.status_code == 200 and r.text and "Access Denied" not in r.text:
                r.encoding = "utf-8"
                return r.text, True
            last = r.status_code
        except Exception as e:
            last = e
        # 2) 回退 Playwright
        if HAVE_PW:
            try:
                page = _ensure_page()
                page.goto(url, timeout=60000, wait_until="domcontentloaded")
                page.wait_for_timeout(3500)  # 等 Akamai JS 挑战/广告
                html = page.content()
                if html and "Access Denied" not in html:
                    return html, True
            except Exception as e:
                last = e
        time.sleep(min(2 ** attempt, 6))
    return None, False


# ---------------------------------------------------------------------------
# 链接发现（首页 + 栏目页，Playwright 取回 HTML 后抽内链）
# ---------------------------------------------------------------------------
def collect_links(sections):
    links = {}  # url -> True
    seed = [BASE + "/"] + [BASE + "/" + s + "/" for s in sections]
    for url in seed:
        html, ok = fetch_html(url)
        if not ok or not html:
            continue
        soup = BeautifulSoup(html, "html.parser")
        for a in soup.find_all("a", href=True):
            href = a["href"].strip()
            if not href:
                continue
            if href.startswith("//"):
                href = "https:" + href
            elif href.startswith("/"):
                href = BASE + href
            elif not href.startswith("http"):
                continue
            if "leparisien.fr" not in href:
                continue
            if NON_ARTICLE_RE.search(href):
                continue
            if ARTICLE_RE.search(href):
                # 归一化：去掉 tracking 参数与 #fragment
                clean = href.split("?")[0].split("#")[0]
                links[clean] = True
    return links


# ---------------------------------------------------------------------------
# 时间规范化
# ---------------------------------------------------------------------------
def normalize_time(raw):
    if not raw:
        return None
    raw = raw.strip()
    # 形如 2026-07-30T07:15:00+02:00
    m = re.search(r"(\d{4})-(\d{2})-(\d{2})[T ](\d{2}):(\d{2}):?(\d{2})?", raw)
    if m:
        try:
            dt = datetime(
                int(m.group(1)), int(m.group(2)), int(m.group(3)),
                int(m.group(4)), int(m.group(5)),
                int(m.group(6)) if m.group(6) else 0,
            )
            # 带时区偏移则保留
            off = re.search(r"([+-])(\d{2}):?(\d{2})", raw[m.end():])
            if off:
                sign = 1 if off.group(1) == "+" else -1
                from datetime import timezone, timedelta
                dt = dt.replace(tzinfo=timezone(
                    sign * timedelta(hours=int(off.group(2)), minutes=int(off.group(3)))))
            return dt.isoformat()
        except Exception:
            pass
    return raw  # 留原始串


# ---------------------------------------------------------------------------
# 解析 JSON-LD
# ---------------------------------------------------------------------------
def parse_ld(soup):
    data = {}
    for s in soup.find_all("script", type="application/ld+json"):
        txt = s.get_text()
        try:
            obj = json.loads(txt)
        except Exception:
            continue
        # 可能是列表或 @graph
        items = obj if isinstance(obj, list) else obj.get("@graph", [obj])
        if isinstance(items, dict):
            items = [items]
        for it in items:
            if not isinstance(it, dict):
                continue
            if it.get("@type") in ("NewsArticle", "Article", "Report", "NewsArticle"):
                data.update(it)
    return data


# ---------------------------------------------------------------------------
# 文章解析
# ---------------------------------------------------------------------------
def parse_article(url):
    html, ok = fetch_html(url)
    if not ok or not html:
        return None
    soup = BeautifulSoup(html, "html.parser")
    ld = parse_ld(soup)

    # 标题
    title = None
    if ld.get("headline"):
        title = ld["headline"].strip()
    if not title:
        h1 = soup.find("h1")
        if h1:
            title = h1.get_text(strip=True)
    if not title:
        og = soup.find("meta", attrs={"property": "og:title"})
        if og and og.get("content"):
            title = og["content"].strip()
    if not title and soup.title:
        title = soup.title.get_text(strip=True)
    if not title:
        return None

    # 摘要
    summary = None
    if ld.get("description"):
        summary = ld["description"].strip()
    if not summary:
        og = soup.find("meta", attrs={"property": "og:description"})
        if og and og.get("content"):
            summary = og["content"].strip()
    if not summary:
        md = soup.find("meta", attrs={"name": "description"})
        if md and md.get("content"):
            summary = md["content"].strip()

    # 栏目
    section = None
    if ld.get("articleSection"):
        sec = ld["articleSection"]
        section = sec[0] if isinstance(sec, list) else str(sec)
        section = section.strip()
    if not section:
        # breadcrumb
        for s in soup.find_all("script", type="application/ld+json"):
            m = re.search(r'"@type"\s*:\s*"BreadcrumbList".*?"name"\s*:\s*"([^"]+)"',
                          s.get_text(), re.S)
            if m:
                section = m.group(1).strip()
                break

    # 时间
    published_at = normalize_time(ld.get("datePublished"))
    if not published_at:
        pt = soup.find("meta", attrs={"property": "article:published_time"})
        if pt and pt.get("content"):
            published_at = normalize_time(pt["content"])

    # 作者
    author = None
    if ld.get("author"):
        au = ld["author"]
        if isinstance(au, list) and au:
            author = au[0].get("name") if isinstance(au[0], dict) else str(au[0])
        elif isinstance(au, dict):
            author = au.get("name")
        elif isinstance(au, str):
            author = au
        if author:
            author = author.strip()
    if not author:
        am = soup.find("meta", attrs={"property": "article:author"})
        if am and am.get("content"):
            author = am["content"].strip()

    # 正文
    content = ""
    if ld.get("articleBody") and len(ld["articleBody"]) > 200:
        content = ld["articleBody"].strip()
    if not content:
        body = None
        for sel in ["div.article__content", "div[data-cy='article-body']",
                    "section.article-body", "div.article-body",
                    "div.content", "article"]:
            el = soup.select_one(sel)
            if el:
                body = el
                break
        if body is None:
            # 启发式：含最多 <p> 的容器（排除导航/侧栏/页脚/广告/相关推荐块）
            SKIP = re.compile(r"nav|side|foot|header|comment|related|recommend|"
                              r"ad-|advert|pub|aside|breadcrumb|share|social", re.I)
            best, bestn = None, 0
            for div in soup.find_all(["div", "article", "section"]):
                cls_id = " ".join(filter(None, [str(div.get("class")),
                                                str(div.get("id"))])).lower()
                if SKIP.search(cls_id):
                    continue
                ps = div.find_all("p")
                if len(ps) > bestn:
                    bestn = len(ps)
                    best = div
            body = best
        if body:
            parts = []
            for el in body.find_all(["p", "h2", "h3"]):
                txt = el.get_text(" ", strip=True)
                if txt:
                    parts.append(txt)
            content = "\n".join(parts)

    # 图片
    images = []
    seen = set()
    # JSON-LD 图片
    ji = ld.get("image")
    if isinstance(ji, list):
        for x in ji:
            u = x.get("url") if isinstance(x, dict) else str(x)
            if u and u not in seen:
                seen.add(u)
                images.append({"url": u, "caption": title or ""})
    elif isinstance(ji, dict) and ji.get("url"):
        if ji["url"] not in seen:
            seen.add(ji["url"])
            images.append({"url": ji["url"], "caption": title or ""})
    elif isinstance(ji, str) and ji:
        if ji not in seen:
            seen.add(ji)
            images.append({"url": ji, "caption": title or ""})
    # og:image 作为首图
    og = soup.find("meta", attrs={"property": "og:image"})
    if og and og.get("content") and og["content"] not in seen:
        seen.add(og["content"])
        images.insert(0, {"url": og["content"], "caption": title or ""})
    # 正文 <img>
    scope = body if body else soup
    for im in scope.find_all("img"):
        src = (im.get("data-src") or im.get("src") or im.get("data-original") or "")
        if not src:
            continue
        if src.startswith("//"):
            src = "https:" + src
        elif src.startswith("/"):
            src = BASE + src
        low = src.lower()
        if low.startswith("data:") or "base64" in low:
            continue
        if any(k in low for k in ["pixel", "1x1", ".gif", "tracking", "cstatic",
                                   "favicon", "logo"]):
            continue
        if src not in seen:
            seen.add(src)
            images.append({"url": src, "caption": im.get("alt", "") or title or ""})

    if not content:
        return None

    aid = None
    m = re.search(r"-(\d{5,})\.php$", url)
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
        "language": "fr",
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


# ---------------------------------------------------------------------------
# 入口
# ---------------------------------------------------------------------------
def main():
    ap = argparse.ArgumentParser(
        description="巴黎人报（Le Parisien）爬虫",
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    ap.add_argument("--limit", type=int, default=300, help="最多解析文章数（默认 300）")
    ap.add_argument("--sections", nargs="*", default=None,
                    help="指定栏目（默认抓取核心栏目集合）")
    ap.add_argument("--delay", type=float, default=1.0, help="请求间隔秒数（默认 1.0）")
    ap.add_argument("--no-detail", action="store_true", help="只采链接不抓正文")
    args = ap.parse_args()

    sections = args.sections if args.sections else DEFAULT_SECTIONS

    print("[list] 采集文章链接（首页 + 栏目页，Playwright 绕过 Akamai）...")
    try:
        links = collect_links(sections)
    finally:
        # 关闭 Playwright
        global _PAGE, _CTX, _BROWSER, _PW
        try:
            if _PAGE:
                _PAGE = None
                if _CTX:
                    _CTX.close()
                if _BROWSER:
                    _BROWSER.close()
                if _PW:
                    _PW.stop()
        except Exception:
            pass
    print("[list] 唯一文章：{0} 篇".format(len(links)))

    items = list(links.keys())
    if args.limit:
        items = items[: args.limit]

    articles = []
    try:
        for i, url in enumerate(items, 1):
            print("[{0}/{1}] 解析：{2}".format(i, len(items), url))
            if args.no_detail:
                articles.append({"id": None, "url": url, "title": None})
                atomic_save(articles, OUTPUT)
                continue
            art = parse_article(url)
            if art and art.get("content"):
                articles.append(art)
                print("        ✓ 标题={0} 正文={1}字 图={2}张 栏目={3} 时间={4}".format(
                    (art["title"][:40] if art["title"] else None),
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
    except Exception:
        atomic_save(articles, OUTPUT)
        print("\n[error] 异常中断，已保存当前进度（{0} 篇）→ {1}".format(len(articles), OUTPUT))
        raise


if __name__ == "__main__":
    main()
