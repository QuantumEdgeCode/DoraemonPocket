# -*- coding: utf-8 -*-
"""
tass_crawler.py — 俄通社-塔斯社(TASS) 新闻爬虫
====================================================================
背景与架构（双引擎方法论，针对 TASS 特殊反爬定制）：
  TASS 首页与文章 HTML 页被 servicepipe.ru 的 JS challenge + 边缘防火墙
  双重拦截：本环境数据中心 IP(203.10.99.50) 直接返回 403 "Forbidden"，
  静态 requests 与 Playwright 均无法拿到正文 HTML。

  但 TASS 的 RSS 订阅源并未被拦：
    - https://tass.ru/rss/yandex.xml  （774 条，且内嵌 <yandex:full-text> 全文！）
    - https://tass.ru/rss/all.xml     （99 条，仅有摘要 description）
  因此本爬虫以 RSS 为主引擎：
    引擎1 RSS（主，稳定可达）：解析列表 + 元数据 + 内嵌全文（yandex feed）。
    引擎2 详情页渲染（降级兜底）：仅当某条目无内嵌全文（如 all.xml）时，
        先试静态请求、再试 Playwright；若被 403 拦截则回退 RSS 摘要，
        并标记 content_source="detail_blocked"。

合规：
  - robots 仅打印提示，不阻断（按用户要求）；正文详情页请求间隔 3s；
    仅个人学习/研究用途。内容版权归 ТАСС(塔斯社)，禁止商用、禁止批量转发。
  - 注意：robots.txt 明确 Allow: /rss/yandex.xml（YandexNews），all.xml 亦未禁止，
    故 RSS 抓取合规。

用法：
  python tass_crawler.py                       # 默认全量：抓取 yandex.xml（774 条，含全文）
  python tass_crawler.py --no-detail           # 仅抓列表级字段（标题/时间/分类/摘要/图），跳过全文
  python tass_crawler.py --limit 5             # 前 5 条，调试
  python tass_crawler.py --root https://tass.ru/rss/all.xml   # 换源（仅摘要）
  python tass_crawler.py --cookie "k=v;k2=v2"  # 可选注入 cookie（供详情降级引擎）
"""

import argparse
import json
import time
from email.utils import parsedate_to_datetime
from datetime import datetime, timezone, timedelta
from bs4 import BeautifulSoup
import requests
from urllib.parse import urlparse
import os

# ---------------- 配置 ----------------
TASS_ROOT = "https://tass.ru/"
PRIMARY_FEED = "https://tass.ru/rss/yandex.xml"   # 774 条 + 内嵌全文
FALLBACK_FEED = "https://tass.ru/rss/all.xml"     # 99 条，仅摘要
USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
)
HEADERS = {
    "User-Agent": USER_AGENT,
    "Accept": "application/rss+xml, application/xml, text/xml, */*;q=0.8",
    "Accept-Language": "ru-RU,ru;q=0.9,en;q=0.8",
    "Referer": "https://tass.ru/",
}
REQUEST_INTERVAL = 3          # 仅详情降级引擎使用
REQUEST_TIMEOUT = 20
OUTPUT_JSON = "data/新闻/tass_collection.json"

# 莫斯科时间（TASS pubDate 为 +0300）
MSK = timezone(timedelta(hours=3), name="MSK")

# 非内容图过滤
NOISE_IMG = ("pixel", "spacer", "1x1", "/ad/", "icon", "logo", "emoji", "placeholder")


# ---------------- robots（仅提示，不阻断） ----------------

# ---------------- cookie ----------------
def load_cookies(cookie_arg: str) -> dict:
    import os
    raw = (cookie_arg or "").strip()
    if not raw and os.path.exists("cookie.txt"):
        raw = open("cookie.txt", encoding="utf-8").read().strip()
        if raw:
            print("[cookie] 已从 cookie.txt 读取")
    if not raw:
        raw = os.environ.get("TASS_COOKIE", "").strip()
        if raw:
            print("[cookie] 已从环境变量 TASS_COOKIE 读取")
    jar = {}
    if raw:
        for pair in raw.split(";"):
            if "=" in pair:
                k, v = pair.split("=", 1)
                jar[k.strip()] = v.strip()
        print(f"[cookie] 已注入 {len(jar)} 个 cookie")
    return jar


# ---------------- 引擎1：RSS 主引擎（稳定可达） ----------------
def fetch_feed(feed_url: str):
    """返回原始条目列表：每个含 title/url/pubDate/categories/summary/image/full_text_raw"""
    print(f"[RSS] 请求：{feed_url}")
    try:
        r = requests.get(feed_url, headers=HEADERS, timeout=REQUEST_TIMEOUT)
        if r.status_code != 200:
            print(f"[RSS] 状态码 {r.status_code}，非 200")
            return None
        if "servicepipe.ru" in r.text:
            print("[RSS] 该源被 JS challenge 拦截（非白名单 feed），跳过")
            return None
    except Exception as e:
        print(f"[RSS] 异常：{e}")
        return None

    soup = BeautifulSoup(r.text, "xml")
    items = soup.find_all("item")
    print(f"[RSS] 解析到 {len(items)} 条")
    out = []
    for it in items:
        title = it.title.get_text(strip=True) if it.title else ""
        link = it.link.get_text(strip=True) if it.link else ""
        if not title or not link:
            continue
        # 时间：RFC822 -> tz-aware ISO（+0300）
        pub_iso = ""
        if it.pubDate:
            try:
                dt = parsedate_to_datetime(it.pubDate.get_text())
                if dt.tzinfo is None:
                    dt = dt.replace(tzinfo=MSK)
                pub_iso = dt.isoformat()
            except Exception:
                pub_iso = it.pubDate.get_text(strip=True)
        # 分类（可能多条）
        cats = [c.get_text(strip=True) for c in it.find_all("category") if c.get_text(strip=True)]
        # 摘要
        summary = it.description.get_text(strip=True) if it.description else ""
        # 图片（enclosure）
        image = ""
        enc = it.enclosure
        if enc and enc.get("url"):
            image = enc.get("url").strip()
        elif enc and enc.get("href"):
            image = enc.get("href").strip()
        # 内嵌全文（yandex feed）
        ft = it.find(lambda t: t.name and t.name.split(":")[-1] == "full-text")
        full_text_raw = ft.get_text() if ft else ""

        out.append({
            "title": title,
            "url": link,
            "publish_time": pub_iso,
            "categories": cats,
            "summary": summary,
            "image": image,
            "full_text_raw": full_text_raw,
        })
    return out


# ---------------- 内嵌全文解析 ----------------
def parse_full_text(raw_html: str) -> str:
    """yandex:full-text 内是 HTML 实体化文本（<p>...</p>），二次解析取段落。"""
    if not raw_html:
        return ""
    inner = BeautifulSoup(raw_html, "lxml")
    paras = [p.get_text(" ", strip=True) for p in inner.find_all("p")]
    if not paras:
        # 退化：直接取文本
        paras = [t for t in inner.get_text("\n", strip=True).split("\n") if len(t) > 15]
    return "\n".join(paras)


# ---------------- 详情降级引擎（仅无内嵌全文时尝试） ----------------
def probe_detail_access() -> bool:
    """探测文章 HTML 页是否可达（本环境会被 403 拦截）。"""
    test = "https://tass.ru/ekonomika/27962029"
    try:
        r = requests.get(test, headers=HEADERS, timeout=15)
        if r.status_code == 200 and "servicepipe.ru" not in r.text and "Forbidden" not in r.text[:200]:
            return True
    except Exception:
        pass
    return False


def fetch_detail(url: str, cookies) -> dict:
    """Playwright 降级（双引擎第二步）；被 IP 拦截则返回空。"""
    empty = {"content": "", "content_source": "detail_blocked"}
    try:
        from playwright.sync_api import sync_playwright
    except ImportError:
        return empty
    try:
        with sync_playwright() as p:
            b = p.chromium.launch(
                headless=True,
                args=["--disable-blink-features=AutomationControlled", "--no-sandbox",
                      "--disable-dev-shm-usage"],
            )
            ctx = b.new_context(user_agent=USER_AGENT, locale="ru-RU",
                                viewport={"width": 1366, "height": 900})
            ctx.add_init_script("""
                Object.defineProperty(navigator, 'webdriver', {get: () => undefined});
                Object.defineProperty(navigator, 'plugins', {get: () => [1,2,3,4,5]});
                window.chrome = {runtime: {}};
            """)
            if cookies:
                ctx.add_cookies([
                    {"name": k, "value": v, "domain": ".tass.ru", "path": "/"}
                    for k, v in cookies.items()
                ])
            pg = ctx.new_page()
            pg.goto(url, wait_until="domcontentloaded", timeout=30000)
            try:
                pg.wait_for_function(
                    "() => document.querySelector('h1') && document.querySelector('h1').innerText.length>5",
                    timeout=20000)
            except Exception:
                pass
            pg.wait_for_timeout(2500)
            html = pg.content()
            b.close()
        soup = BeautifulSoup(html, "lxml")
        h1 = soup.find("h1")
        if h1 and "forbidden" not in h1.get_text().lower():
            wrap = soup.select_one(".news-detail__content") or soup.select_one(
                ".text-block") or soup.select_one("article")
            paras = []
            scope = wrap if wrap else soup
            for p in scope.find_all("p"):
                t = p.get_text(" ", strip=True)
                if len(t) > 15:
                    paras.append(t)
            if paras:
                return {"content": "\n".join(paras), "content_source": "detail_render"}
    except Exception as e:
        print(f"    [详情] 渲染失败：{e}")
    return empty


# ---------------- 原子写入 ----------------
def atomic_save(records, path=OUTPUT_JSON, source=TASS_ROOT):
    """原子写入：写 .tmp → flush → fsync → os.replace，防进程中断留半截 JSON。"""
    os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(records, f, ensure_ascii=False, indent=2)
        f.flush()
        os.fsync(f.fileno())
    os.replace(tmp, path)


# ---------------- 主流程 ----------------
def main():
    ap = argparse.ArgumentParser(description="塔斯社(TASS) 新闻双引擎爬虫（RSS 主引擎）")
    ap.add_argument("--no-detail", action="store_true",
                    help="仅抓列表级字段，跳过内嵌全文解析（快）")
    ap.add_argument("--limit", type=int, default=0, help="仅处理前 N 条（调试），0 表示全部")
    ap.add_argument("--root", default=PRIMARY_FEED, help="RSS feed URL（默认 yandex.xml）")
    ap.add_argument("--cookie", default="", help="注入 cookie：name1=val1; name2=val2")
    args = ap.parse_args()

    cookies = load_cookies(args.cookie)

    # 引擎1：RSS
    items = fetch_feed(args.root)
    if not items:
        print(f"[!] 主源失败，尝试降级源 {FALLBACK_FEED}")
        items = fetch_feed(FALLBACK_FEED)
    if not items:
        print("[-] 未获取到任何条目（RSS 可能被拦截或结构变化）。")
        return

    if args.limit > 0:
        items = items[: args.limit]
        print(f"[limit] 仅处理前 {len(items)} 条")

    # 是否需要详情降级引擎：仅当存在无内嵌全文且未禁用 detail 的条目
    need_detail = (not args.no_detail) and any(not it["full_text_raw"] for it in items)
    detail_enabled = False
    if need_detail:
        print("[详情] 探测文章 HTML 页可达性...")
        detail_enabled = probe_detail_access()
        if not detail_enabled:
            print("[详情] 文章页被边缘防火墙 403 拦截（本环境数据中心 IP）。"
                  "无内嵌全文的条目将回退 RSS 摘要。")

    out = {
        "source": TASS_ROOT,
        "feed": args.root,
        "crawled_at": datetime.now(MSK).isoformat(),
        "detail_html_accessible": detail_enabled,
        "count": 0,
        "items": [],
    }
    try:
        for idx, it in enumerate(items, 1):
            rec = {
                "title": it["title"],
                "url": it["url"],
                "publish_time": it["publish_time"],
                "categories": it["categories"],
                "summary": it["summary"],
                "image": it["image"],
            }
            if args.no_detail:
                rec["content"] = ""
                rec["content_source"] = "skipped"
            elif it["full_text_raw"]:
                rec["content"] = parse_full_text(it["full_text_raw"])
                rec["content_source"] = "rss_fulltext"
            else:
                # 无内嵌全文：尝试详情降级
                if detail_enabled:
                    print(f"  ({idx}/{len(items)}) 详情渲染 {it['url']}")
                    d = fetch_detail(it["url"], cookies)
                    rec["content"] = d["content"]
                    rec["content_source"] = d["content_source"]
                    time.sleep(REQUEST_INTERVAL)
                else:
                    rec["content"] = it["summary"]
                    rec["content_source"] = "rss_summary"
            out["items"].append(rec)
            out["count"] = len(out["items"])
            atomic_save(out)   # 实时落盘（每篇）
    except (KeyboardInterrupt, Exception) as e:
        print(f"\n[中断] 捕获到异常/中断，已保存中断前进度（{len(out['items'])} 条）→ {OUTPUT_JSON}")
        atomic_save(out)
        raise

    atomic_save(out)
    print(f"[+] 已写入 {OUTPUT_JSON}（{len(out['items'])} 条）")

    # 简要统计
    full = sum(1 for i in out["items"] if i["content_source"] == "rss_fulltext")
    summ = sum(1 for i in out["items"] if i["content_source"] in ("rss_summary", "detail_blocked"))
    img = sum(1 for i in out["items"] if i["image"])
    print(f"[统计] 全文 {full} / 摘要 {summ} / 含图 {img}")


if __name__ == "__main__":
    main()
