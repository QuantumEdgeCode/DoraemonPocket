#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
belta_crawler.py — 俄罗斯白俄罗斯通讯社（白通社）中文站 chn.belta.by 爬虫

站点特点（已逐个验证）：
  - 中文站，内容版权归 BelTA（白俄罗斯通讯社 / 白通社），仅限个人学习/研究，禁止商用转发。
  - 文章发现：栏目页 + 翻页
        新闻栏目（已验证）：politics(政治)/economics(经济)/society(社会)/president(总统)
        栏目页：/<section>/                    第一页
                /<section>/page/<N>/           后续页
        栏目页含文章链接 /<section>/view/<ID>-<YYYY>/，翻页到 maxpage 为止（实测政治 11 页）。
        republic-ch(共和国) 栏目页无 view 链接，已剔除。
  - 文章 URL 形如 https://chn.belta.by/<section>/view/<ID>-<YYYY>/
                   例：/politics/view/-45788-2026/
                   ID 为 view/ 后纯数字串（含前导负号），唯一；栏目名由 URL 路径权威取得。
  - 正文容器（易踩坑）：div.inner_content，段落是嵌套的小 <div>（不是 <p>），
        <p> 仅版权块一个；须在文本累积中遇到“订阅我们：”即停（尾部推广噪声）。
  - 配图：div.main_img > img（src 直取真实 jpg，cdn 同域 /images/storage/...）；
        另有一张 viber 推广图（/desimages/1.png）在“订阅我们”块内，已被“订阅我们”截断天然过滤。
  - 标题：<h1>（干净，无站点后缀）；og:title 同。
  - 时间：meta article:published_time = "2026-07-28 17:18:00"（无时区）；
        白俄罗斯/明斯克时区为 UTC+3，已用 tzinfo 显式附加，不靠 datetime+小时。
  - 栏目：已由 URL 路径权威取得（中文映射）；站点无署名栏 → author="白通社"。
  - SSL：服务端 TLS 偶发 UNEXPECTED_EOF（Python ssl 握手不稳），统一 verify=False + 重试。
  - 无付费墙、无 robots 硬阻断（robots.txt Allow:/ ；Sitemap 返回 403 故改走栏目翻页发现）。

输出：belta_collection.json（与同项目其它爬虫同构：
      id, url, title, section, section_code, author, published_at, summary, content, images, tags）

用法（与统一约定一致）：
  python belta_crawler.py                 # 默认 limit=800, delay=3
  python belta_crawler.py --limit N
  python belta_crawler.py --no-detail     # 只发现不抓正文（快速看规模）
  python belta_crawler.py --root <URL>    # 覆盖起始根
  python belta_crawler.py --delay 3
  python belta_crawler.py --playwright    # 预留（当前静态 SSR 已足够，默认不启用）
"""
import argparse
import json
import os
import re
import time
import urllib3
from datetime import datetime, timezone, timedelta
from bs4 import BeautifulSoup
import requests

urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)

BASE = "https://chn.belta.by"
SITE_NAME = "白俄罗斯通讯社(中文站)"

HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                  "(KHTML, like Gecko) Chrome/124.0 Safari/537.36"
}

SESSION = requests.Session()
SESSION.headers.update(HEADERS)

# 新闻栏目（已验证含 view 链接）；值为 (英文栏目, 中文栏目名)
SECTIONS = {
    "politics": "政治",
    "economics": "经济",
    "society": "社会",
    "president": "总统",
}

# 尾部噪声：订阅推广块（含 viber 图）
NOISE_MARKERS = ("订阅我们",)

# 白俄罗斯/明斯克时区 UTC+3
MINSK_TZ = timezone(timedelta(hours=3))

VIEW_RE = re.compile(r"/([a-z]+)/view/([^/]+)/?$")
PAGE_VIEW_RE = re.compile(r'href="(https?://chn\.belta\.by/([a-z]+)/view/([^"]+))"')


def fetch(url, tries=8, timeout=30, backoff=30):
    """带重试的 GET；服务端 TLS 偶发 EOF，且会按 IP 限流（429/503），需多试 + 退避。
    返回 response 或异常对象（无 .status_code）。"""
    last = None
    for i in range(tries):
        try:
            r = SESSION.get(url, timeout=timeout, verify=False)
            if r.status_code == 200:
                return r
            last = r
            # 限流/网关错：指数退避后重试
            if r.status_code in (429, 503, 502, 504):
                wait = backoff * (i + 1)
                print(f"  [ratelimit {r.status_code}] {url} -> sleep {wait}s", flush=True)
                time.sleep(wait)
                continue
            return r  # 其它 4xx 直接返回
        except Exception as e:  # SSL EOF / 连接异常
            last = e
            time.sleep(2 * (i + 1))
    return last


def parse_time(raw):
    if not raw:
        return None
    raw = raw.strip()
    for fmt in ("%Y-%m-%d %H:%M:%S",):
        try:
            return datetime.strptime(raw, fmt).replace(tzinfo=MINSK_TZ).isoformat()
        except ValueError:
            continue
    # 退路：直接解析后附加时区
    try:
        dt = datetime.fromisoformat(raw.replace("Z", "+00:00"))
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=MINSK_TZ)
        return dt.isoformat()
    except Exception:
        return None


def discover_articles(root=BASE, no_detail=False):
    """遍历各栏目 + 翻页，收集唯一文章 URL -> (url, section_code, section_cn)。"""
    seen = {}
    for code, cn in SECTIONS.items():
        page = 1
        while True:
            url = f"{BASE}/{code}/" if page == 1 else f"{BASE}/{code}/page/{page}/"
            r = fetch(url)
            if not r or r.status_code != 200:
                break
            new_this = 0
            for m in PAGE_VIEW_RE.finditer(r.text):
                aurl, acode, aid = m.group(1), m.group(2), m.group(3)
                if acode != code:
                    continue
                if aurl not in seen:
                    seen[aurl] = (aurl, acode, SECTIONS.get(acode, acode))
                    new_this += 1
            # 翻页终止：本页无新文章（已到末页或重复），或到达约定上限（防御）
            if new_this == 0:
                break
            page += 1
            if page > 60:  # 安全上限
                break
            time.sleep(0.5)
    return list(seen.values())


def extract(html, url):
    soup = BeautifulSoup(html, "html.parser")

    # 标题
    h1 = soup.find("h1")
    title = h1.get_text(strip=True) if h1 else None
    if not title:
        og = soup.find("meta", attrs={"property": "og:title"})
        title = og.get("content", "").strip() if og else None
    if not title:
        return None

    # 404 防御
    if "404" in title and "找不到" in html[:600]:
        return None

    # 正文容器
    ic = soup.find("div", class_="inner_content")
    if not ic:
        return None

    # 正文：inner_content 下所有直接/嵌套文本块，遇到“订阅我们”即停
    parts = []
    for div in ic.find_all("div", recursive=True):
        # 只取“叶子级”文本块（自身含文本、无文本子 div）
        if div.find("div"):
            continue
        t = div.get_text(strip=True)
        if not t:
            continue
        if any(mk in t for mk in NOISE_MARKERS):
            break
        parts.append(t)
    content = "\n\n".join(parts).strip()
    if not content:
        return None

    # 配图：div.main_img > img
    images = []
    mi = ic.select_one("div.main_img img")
    if mi:
        src = mi.get("src") or mi.get("data-src") or ""
        if src and src.lower().endswith((".jpg", ".jpeg", ".png", ".webp")):
            if "desimages/1.png" not in src:  # 推广图排除
                images.append({"url": src, "caption": ""})

    # 时间
    pub = None
    mt = soup.find("meta", attrs={"property": "article:published_time"})
    if mt and mt.get("content"):
        pub = parse_time(mt.get("content"))
    if not pub:
        md = soup.find("meta", attrs={"property": "article:modified_time"})
        if md and md.get("content"):
            pub = parse_time(md.get("content"))

    # 摘要：首段前 120 字
    summary = content[:120]

    # 栏目（URL 权威）
    m = VIEW_RE.search(url)
    section_code = m.group(1) if m else ""
    section = SECTIONS.get(section_code, section_code)

    # 文章 ID
    article_id = m.group(2) if m else url.rstrip("/").split("/")[-1]

    return {
        "id": article_id,
        "url": url,
        "title": title,
        "section": section,
        "section_code": section_code,
        "author": "白通社",
        "published_at": pub or "",
        "summary": summary,
        "content": content,
        "images": images,
        "tags": [],
    }


# ---------------------------------------------------------------------------
# JSON 原子保护性写入（实时落盘，防进程中断丢数据）
# ---------------------------------------------------------------------------
OUT_FILE = "data/新闻/belta_collection.json"

def atomic_save(items, path=OUT_FILE):
    """写 .tmp -> flush+fsync -> os.replace 改名，杜绝半截 JSON；逐篇实时调用。"""
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
    ap = argparse.ArgumentParser(description="BelTA 中文站(chn.belta.by) 爬虫")
    ap.add_argument("--limit", type=int, default=800)
    ap.add_argument("--no-detail", action="store_true", help="只发现不抓正文")
    ap.add_argument("--root", default=BASE)
    ap.add_argument("--delay", type=float, default=3.0)
    ap.add_argument("--playwright", action="store_true", help="预留（当前未用）")
    args = ap.parse_args()

    print(f"[*] 发现文章（{SITE_NAME}）...", flush=True)
    arts = discover_articles(args.root)
    print(f"[*] 发现 {len(arts)} 篇唯一文章", flush=True)

    out = []
    skipped = 0
    if not args.no_detail:
        for i, (url, code, cn) in enumerate(arts[: args.limit], 1):
            r = fetch(url)
            if not r or r.status_code != 200:
                skipped += 1
                if i % 50 == 0:
                    print(f"  [{i}/{min(len(arts),args.limit)}] {url} -> 跳过(HTTP {getattr(r,'status_code','ERR')})", flush=True)
                time.sleep(args.delay)
                continue
            item = extract(r.text, url)
            if item:
                out.append(item)
            else:
                skipped += 1
            if i % 25 == 0 or i == 1:
                print(f"  [{i}/{min(len(arts),args.limit)}] got={len(out)} skip={skipped}", flush=True)
            time.sleep(args.delay)
            atomic_save(out)  # 每篇实时落盘，防中断丢数据

    # 收尾保存（保证进度完整）+ 中断 / 异常保护
    try:
        atomic_save(out)
        print(f"[done] 抓取 {len(out)} 篇，跳过 {skipped} 篇 -> {OUT_FILE}", flush=True)
    except KeyboardInterrupt:
        print(f"\n[!] 被用户中断，已实时保存至 {OUT_FILE}（当前 {len(out)} 条）", flush=True)
        raise
    except Exception as exc:
        print(f"\n[!] 写入异常: {exc}；已实时保存至 {OUT_FILE}（当前 {len(out)} 条）", flush=True)
        raise


if __name__ == "__main__":
    main()
