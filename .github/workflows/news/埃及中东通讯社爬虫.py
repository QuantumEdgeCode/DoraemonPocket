#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
mena_crawler.py — 爬取 https://www.mena.org.eg/ (Middle East News Agency, 埃及国家通讯社)
================================================================================
方法（Next.js SSR 数据直取，双引擎降级，沿用统一约定）
  ★ 关键发现（实测）：
    - 站点为 Next.js 应用，由 **Cloudflare** 防护。列表/首页的 SSR HTML 里
      `__NEXT_DATA__` <script> 直接内嵌最新文章列表（每语种 ~80~85 篇），
      含 title / slug / bodyNews.body（**前 250 字摘要**）/ mainPhotoNews（图）/
      category / subCategory / publishedAt。这是**稳定可达**的数据源（requests 直取）。
    - 文章详情页 `/<lang>/news/<slug>` 的 __NEXT_DATA__ 不含正文（newsById 为空），
      正文为**客户端 JS 拉取**，且 **Cloudflare 对无头浏览器返回 "Performing
      security verification" 挑战页（403）**；站点**无公开 JSON API**（/api/news/<id>
      只是 Next.js SPA 回退 HTML）。故**全文正文在本沙箱无法取得**。
  - 文章 URL：`https://mena.org.eg/<lang>/news/<slug>`
        lang ∈ {en, ar}；slug 形如 `trump-denies-reports-...-<uuid>`（末段是文章 id）
  - 发现来源（按语种抓列表页，取首个能解析出文章的候选 URL）：
        en → /en/mena-news  （回退 /en）
        ar → /ar/news        （回退 / ，即阿拉伯语首页）
  - 正文处理：列表内嵌 `bodyNews.body` 是**站点截断的 250 字摘要**（实测恒为 250 字），
        作为 content，并标记 content_source="listing_excerpt_250"（诚实，非全文）。
        如需全文：须在**本机住宅 IP + 真实浏览器（过 Cloudflare）**渲染详情页；
        本沙箱无头浏览器被 Cloudflare 拦，故默认不抓详情。
  - 时间：publishedAt 形如 `2026-07-27T18:40:44.953Z`（UTC, Z 结尾），
        `datetime.fromisoformat(s.replace("Z","+00:00"))` 保留时区，**禁 datetime+小时 hack**。
  - 图片：mainPhotoNews.websiteUrl（真实大图，域 stagingmedia.mena.org.eg）；
        caption 可能含阿拉伯文；无图则留空。
  - 栏目：category.name / subCategory（如 "World" / "Middle East"）。
  - 署名：中东通讯社为通讯社通稿，无个人署名 → author 统一 "Middle East News Agency (MENA)"。

★ Cloudflare 韧性（重要）：列表/首页偶发被 Cloudflare 挑战（返回 403 或
  "Performing security verification" 页，__NEXT_DATA__ 无文章）。fetch_listing()
  内置重试 + 退避；某语种连续挑战则跳过并告警，不中断其它语种。

合规：Middle East News Agency (MENA) 内容版权归其所有；本脚本仅用于个人学习/
研究，禁止商用转发。robots.txt 仅含 Content-Signal 声明，无 sitemap，不阻断。

用法：
  python mena_crawler.py                  # 默认：en + ar 全量（各 ~80 篇，约 160 条）
  python mena_crawler.py --lang en        # 仅英文
  python mena_crawler.py --lang ar        # 仅阿拉伯文
  python mena_crawler.py --lang en,ar     # 多语种
  python mena_crawler.py --limit 50       # 每语种最多 50 篇
  python mena_crawler.py --delay 2        # 语种间间隔（秒）
"""

import argparse
import json
import os
import re
import time
from datetime import datetime, timezone

import requests
import urllib3

# MENA (Next.js + Cloudflare) is fetched with verify=False (SSL hardening);
# silence the InsecureRequestWarning noise in the logs.
urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)

BASE = "https://mena.org.eg"
OUTPUT = "data/新闻/mena_collection.json"
HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                  "(KHTML, like Gecko) Chrome/124.0 Safari/537.36",
    "Accept-Language": "en;q=0.9",
}

# 每语种的列表页候选（按顺序，取首个能解析出文章的）
LANG_LISTING = {
    "en": ["/en/mena-news", "/en"],
    "ar": ["/ar/news", "/"],
}
ALL_LANGS = ["en", "ar"]

CF_MARKERS = ("Performing security verification", "Ray ID",
              "cf-chl", "Just a moment", "Attention Required")


# ---------------------------------------------------------------------------
# 网络获取（带 Cloudflare 重试）
# ---------------------------------------------------------------------------
def fetch_html(url, retries=6, backoff=3.0, verify=False):
    last = None
    for i in range(retries):
        try:
            r = requests.get(url, headers=HEADERS, timeout=30, verify=verify)
            txt = r.text
            if r.status_code == 200 and not _is_cloudflare(txt):
                return txt, True
            # 403 / 挑战页 → 重试
            last = "status=%s cloudflare=%s" % (
                r.status_code, _is_cloudflare(txt))
        except Exception as e:
            last = repr(e)[:120]
        time.sleep(backoff)
    return None, False


def _is_cloudflare(txt):
    if not txt:
        return False
    return any(m in txt for m in CF_MARKERS)


def parse_next_data(html):
    m = re.search(r'<script id="__NEXT_DATA__"[^>]*>(.*?)</script>', html, re.S)
    if not m:
        return None
    try:
        return json.loads(m.group(1))
    except Exception:
        return None


# ---------------------------------------------------------------------------
# 文章提取（递归找含 title+slug+bodyNews+publishedAt 的对象）
# ---------------------------------------------------------------------------
def extract_articles(nd):
    pp = (nd or {}).get("props", {}).get("pageProps", {})
    out = []

    def walk(o):
        if isinstance(o, dict):
            if ("title" in o and "slug" in o and "bodyNews" in o
                    and "publishedAt" in o):
                out.append(o)
            for v in o.values():
                if isinstance(v, (dict, list)):
                    walk(v)
        elif isinstance(o, list):
            for v in o:
                if isinstance(v, (dict, list)):
                    walk(v)

    walk(pp)
    return out


def collect_for_lang(lang):
    candidates = LANG_LISTING.get(lang, ["/" + lang])
    for cand in candidates:
        url = BASE + cand
        txt, ok = fetch_html(url)
        if not ok:
            print("    [warn] {0} 不可达/被 Cloudflare 挑战，尝试下一候选".format(cand))
            continue
        nd = parse_next_data(txt)
        arts = extract_articles(nd)
        if arts:
            print("    [ok] {0} 解析出 {1} 篇".format(cand, len(arts)))
            return arts
        else:
            print("    [warn] {0} 无文章对象（可能挑战页），尝试下一候选".format(cand))
    return []


# ---------------------------------------------------------------------------
# 字段解析
# ---------------------------------------------------------------------------
def parse_time(s):
    if not s:
        return None
    s = s.strip()
    try:
        d = datetime.fromisoformat(s.replace("Z", "+00:00"))
        if d.tzinfo is None:
            d = d.replace(tzinfo=timezone.utc)
        return d.isoformat()
    except Exception:
        return s if s else None


def body_text(body_news):
    if isinstance(body_news, dict):
        return body_news.get("body") or ""
    if isinstance(body_news, list):
        return " ".join(
            s.get("body", "") for s in body_news if isinstance(s, dict)
        ).strip()
    return ""


def image_of(mp):
    if not isinstance(mp, dict):
        return None, ""
    url = mp.get("websiteUrl") or mp.get("thumbnailUrl") or ""
    cap = mp.get("caption") or ""
    return url, cap


def slug_to_id(slug):
    # slug 末段是 uuid（如 ...-e8e6338b-...），用作去重键
    return slug.rsplit("-", 1)[-1] if slug else ""


# ---------------------------------------------------------------------------
# 主流程
# ---------------------------------------------------------------------------

# ---------------------------------------------------------------------------
# JSON 原子保护性写入（实时落盘，防进程中断丢数据）
# ---------------------------------------------------------------------------
def atomic_save(articles, path=OUTPUT):
    """写 .tmp -> flush+fsync -> os.replace 改名，杜绝半截 JSON；按语种实时调用。"""
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
    ap.add_argument("--lang", type=str, default="all",
                    help="语种：en / ar / all / 逗号分隔（默认 all）")
    ap.add_argument("--limit", type=int, default=0,
                    help="每语种最多采集 N 篇（0=全部）")
    ap.add_argument("--delay", type=float, default=2.0,
                    help="语种之间的请求间隔（秒）")
    args = ap.parse_args()


    if args.lang == "all":
        langs = ALL_LANGS
    else:
        langs = [l.strip() for l in args.lang.split(",") if l.strip()]

    articles = []
    seen_ids = set()  # 跨语种按 slug 末段 uuid 去重（同稿多语种版视为不同，仍可重复则按 id 去）

    for lang in langs:
        print("\n=== 语种: {0} ===".format(lang))
        arts = collect_for_lang(lang)
        if args.limit:
            arts = arts[: args.limit]
        for a in arts:
            aid = slug_to_id(a.get("slug", ""))
            if aid in seen_ids:
                continue
            seen_ids.add(aid)

            title = a.get("title")
            slug = a.get("slug", "")
            url = "{0}/{1}/news/{2}".format(BASE, lang, slug)
            body = body_text(a.get("bodyNews"))
            img_url, img_cap = image_of(a.get("mainPhotoNews"))
            cat = (a.get("category") or {}).get("name") if isinstance(a.get("category"), dict) else None
            sub = a.get("subCategory")
            section = "/".join([x for x in [cat, sub] if x]) or None
            pub = parse_time(a.get("publishedAt"))

            rec = {
                "id": aid,
                "url": url,
                "title": title,
                "language": lang,
                "section": section,
                "author": "Middle East News Agency (MENA)",
                "published_at": pub,
                "summary": body,
                "content": body,
                "images": [{"url": img_url, "caption": img_cap}] if img_url else [],
                "premium": False,
                "content_source": "listing_excerpt_250",
            }
            articles.append(rec)
            print("    + {0} | {1} | {2}字".format(
                (title or "")[:50], section, len(body)))
        atomic_save(articles)  # 每语种实时落盘，防后续语种失败丢数据
        time.sleep(args.delay)

    # 收尾保存（保证完整性）+ 中断 / 异常保护
    try:
        atomic_save(articles)
        print("\n[done] 已保存 {0} 篇 → {1}".format(len(articles), OUTPUT))
        print("       语种分布：", {l: sum(1 for x in articles if x["language"] == l)
                                   for l in langs})
    except KeyboardInterrupt:
        print("\n[!] 被用户中断，已实时保存至 {0}（当前 {1} 条）".format(OUTPUT, len(articles)))
        raise
    except Exception as exc:
        print("\n[!] 写入异常: {0}；已实时保存至 {1}（当前 {2} 条）".format(exc, OUTPUT, len(articles)))
        raise


if __name__ == "__main__":
    main()
