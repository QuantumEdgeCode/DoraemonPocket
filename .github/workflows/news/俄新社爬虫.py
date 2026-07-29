# -*- coding: utf-8 -*-
"""
ria_crawler.py — 俄新社(RIA Novosti) 新闻双引擎爬虫
====================================================================
架构（双引擎方法论，针对 RIA 定制）：
  RIA 为 SSR 且**未被反爬拦截**（实测首页/文章页均 200 直出，无 JS challenge、
  无 IP 封锁），因此静态引擎（requests + BeautifulSoup）即可全程拿下，与 BBC/mk/yna/donga 同档。
  引擎1 静态优先：解析列表与正文（默认即可）。
  引擎2 Playwright 渲染：仅当静态被拦截时降级兜底（本环境基本不会触发）。

关键选择器 / 踩坑（已修进脚本）：
  - 文章 URL：`https://ria.ru/<YYYYMMDD>/<slug>-<id>.html`（带 .html 后缀，id 为 10 位 Number）。
    正则须锚定 `\\d{8}/[A-Za-z0-9_-]+\\.html`，避免误中栏目页 `/politics/`。
  - 标题：`h1`（干净头条）；`og:title` 会带引述前缀，仅作兜底。
  - 时间：`<meta property="article:published_time">` = `20260728T1528`（YYYYMMDDTHHMM，莫斯科时间 +03:00），
    解析后标注 `tzinfo=MSK`（沿用 mk/yna 教训：用 tzinfo，不要 datetime+3h）。
  - 正文：`.article__body` 内按 `.article__block` 分块——
        data-type="text"  → `.article__text` 段落
        data-type="quote" → `.article__quote-text` 引文
    须先 `extract()` 掉 `.article__summary`（“Краткий пересказ от РИА ИИ” AI 摘要，属 UI 噪音）。
  - 配图：`cdnn21.img.ria.ru`（或 cdnn*.img.ria.ru）；`img.lazyload` 用 `src` 全分辨率，过滤 `loader*.svg` 占位。
  - 来源：正文首段 dateline “МОСКВА, 28 июл — РИА Новости.” 提取通讯社名，兜底 “РИА Новости”。

合规：
  - robots 仅打印提示，不阻断（按用户要求）；3s 请求间隔；仅个人学习/研究用途。
  - robots.txt 未禁止文章页与首页（仅禁止 /search/、/services/ 等）；内容版权归 РИА Новости，
    禁止商用、禁止批量转发。

用法：
  python ria_crawler.py                       # 全量：首页列表 + 每篇正文/图片（默认）
  python ria_crawler.py --no-detail           # 仅抓全量列表（快）
  python ria_crawler.py --limit 3             # 前 3 篇含正文，调试
  python ria_crawler.py --root https://ria.ru/politics/   # 指定频道（政治）
  python ria_crawler.py --cookie "k=v;k2=v2"  # 可选注入 cookie
"""

import argparse
import json
import re
import time
import requests
from bs4 import BeautifulSoup
from datetime import datetime, timezone, timedelta
from urllib.parse import urljoin, urlparse
import os

# ---------------- 配置 ----------------
RIA_ROOT = "https://ria.ru/"
USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
)
HEADERS = {
    "User-Agent": USER_AGENT,
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
    "Accept-Language": "ru-RU,ru;q=0.9,en;q=0.8",
    "Referer": "https://ria.ru/",
}
REQUEST_INTERVAL = 3
REQUEST_TIMEOUT = 15
OUTPUT_JSON = "data/新闻/ria_collection.json"

# 文章 URL：/YYYYMMDD/<slug>-<id>.html
ARTICLE_RE = re.compile(r"ria\.ru/\d{8}/[A-Za-z0-9_-]+\.html")
# 用于去重：提取核心 id（/YYYYMMDD/<slug>-<id>.html）
ARTICLE_KEY_RE = re.compile(r"/(\d{8}/[A-Za-z0-9_-]+\.html)")

# 莫斯科时间（RIA published_time 为 +03:00）
MSK = timezone(timedelta(hours=3), name="MSK")

# 非内容图过滤
NOISE_IMG = ("pixel", "spacer", "1x1", "loader", "/ad/", "icon", "logo", "emoji", "placeholder", "svg")

# 正文内 UI 噪音词（RIA 基本没有，留作兜底）
UI_NOISE = ("подпишитесь", "©", "РИА Новости" )  # 仅剔除文末版权行，谨慎使用


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
        raw = os.environ.get("RIA_COOKIE", "").strip()
        if raw:
            print("[cookie] 已从环境变量 RIA_COOKIE 读取")
    jar = {}
    if raw:
        for pair in raw.split(";"):
            if "=" in pair:
                k, v = pair.split("=", 1)
                jar[k.strip()] = v.strip()
        print(f"[cookie] 已注入 {len(jar)} 个 cookie")
    return jar


# ---------------- 引擎1：静态列表 ----------------
def fetch_list_static(root_url, cookies):
    print(f"[列表] 静态引擎请求：{root_url}")
    try:
        r = requests.get(root_url, headers=HEADERS, cookies=cookies, timeout=REQUEST_TIMEOUT)
        if r.status_code in (403, 503, 406):
            print(f"[列表] 静态引擎返回 {r.status_code}（WAF 拦截），降级 Playwright")
            return None
        r.raise_for_status()
    except Exception as e:
        print(f"[列表] 静态引擎异常（{e}），降级 Playwright")
        return None

    soup = BeautifulSoup(r.text, "lxml")
    items = []
    seen = set()
    for a in soup.find_all("a", href=True):
        full = urljoin(RIA_ROOT, a["href"].strip())
        if not ARTICLE_RE.search(full):
            continue
        mk = ARTICLE_KEY_RE.search(full)
        key = mk.group(1) if mk else full.split("?")[0]
        if key in seen:
            continue
        seen.add(key)
        title = a.get_text(strip=True)
        # 纯图片锚点：从 img alt 兜底
        img = a.find("img")
        if len(title) < 6:
            if img and img.get("alt") and len(img["alt"].strip()) >= 6:
                title = img["alt"].strip()
            # 仍可能为空（列表模式会保留空，详情模式将覆盖）
        items.append({"title": title, "url": full})
    print(f"[列表] 静态引擎命中 {len(items)} 条")
    return items


# ---------------- 引擎2：Playwright 列表（降级兜底） ----------------
def fetch_list_render(root_url, cookies):
    try:
        from playwright.sync_api import sync_playwright
    except ImportError:
        print("[!] 未安装 playwright，无法降级。pip install playwright && playwright install chromium")
        return []

    print(f"[列表] Playwright 渲染：{root_url}")
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
            Object.defineProperty(navigator, 'plugins', {get: () => [1, 2, 3, 4, 5]});
            window.chrome = {runtime: {}};
        """)
        if cookies:
            ctx.add_cookies([
                {"name": k, "value": v, "domain": ".ria.ru", "path": "/"}
                for k, v in cookies.items()
            ])
        pg = ctx.new_page()
        pg.goto(root_url, wait_until="domcontentloaded", timeout=30000)
        pg.wait_for_timeout(3000)
        if "Access Denied" in pg.title():
            print("[列表] 页面被反爬拦截（Access Denied），注入 cookie 或换住宅 IP 重试")
            b.close()
            return []
        links = pg.eval_on_selector_all(
            "a[href*='/2026']",
            "els => els.map(e => ({t: (e.innerText||'').trim(), h: e.href, alt: (e.querySelector('img')||{}).alt||''}))",
        )
        b.close()

    items = []
    seen = set()
    for x in links:
        if not ARTICLE_RE.search(x["h"]):
            continue
        mk = ARTICLE_KEY_RE.search(x["h"])
        key = mk.group(1) if mk else x["h"].split("?")[0]
        if key in seen:
            continue
        seen.add(key)
        title = x["t"] if len(x["t"]) >= 6 else x.get("alt", "").strip()
        items.append({"title": title, "url": x["h"]})
    print(f"[列表] Playwright 命中 {len(items)} 条")
    return items


# ---------------- 详情解析（双引擎复用） ----------------
def parse_detail_html(soup: BeautifulSoup, url: str) -> dict:
    # 标题：h1
    title = ""
    h1 = soup.find("h1")
    if h1:
        title = h1.get_text(strip=True)
    if not title:
        og = soup.find("meta", property="og:title")
        if og and og.get("content"):
            title = og["content"].strip()

    # 时间：article:published_time = 20260728T1528 (+03:00 MSK)
    publish_time = ""
    pm = soup.find("meta", property="article:published_time")
    if pm:
        m = re.match(r"(\d{4})(\d{2})(\d{2})T(\d{2})(\d{2})", pm.get("content", ""))
        if m:
            y, mo, d, hh, mm = m.groups()
            publish_time = datetime(int(y), int(mo), int(d), int(hh), int(mm),
                                    tzinfo=MSK).isoformat()

    # 正文 + 配图
    body = soup.select_one(".article__body")
    paras = []
    imgs = []
    seen_img = set()
    source = ""
    if body:
        # 剔除 AI 摘要块（UI 噪音）
        summ = body.select_one(".article__summary")
        if summ:
            summ.extract()
        for blk in body.select(".article__block"):
            dt = blk.get("data-type")
            if dt == "text":
                t = blk.select_one(".article__text")
                if t:
                    txt = t.get_text(" ", strip=True)
                    if len(txt) > 10:
                        paras.append(txt)
                        if not source:
                            sm = re.search(r"—\s*([А-ЯЁ][А-Яа-яЁё\s]+?)\.", txt)
                            if sm:
                                source = sm.group(1).strip()
            elif dt == "quote":
                t = blk.select_one(".article__quote-text")
                if t:
                    txt = t.get_text(" ", strip=True)
                    if len(txt) > 5:
                        paras.append(txt)
            # 该块内图片
            for i in blk.find_all("img"):
                src = i.get("src") or i.get("data-src") or ""
                if not src:
                    ds = i.get("data-srcset") or ""
                    if ds:
                        src = ds.split()[0]
                if not src:
                    continue
                full = urljoin(url, src)
                low = full.lower()
                if "loader" in low or any(k in low for k in NOISE_IMG):
                    continue
                if "ria.ru" not in full and "cdnn" not in full:
                    continue
                if full in seen_img:
                    continue
                seen_img.add(full)
                imgs.append(full)
    # 主图（og:image）兜底/置顶
    og_img = soup.find("meta", property="og:image")
    if og_img and og_img.get("content"):
        lead = og_img["content"].strip()
        if lead not in seen_img:
            imgs.insert(0, lead)
    content = "\n\n".join(paras)
    content_source = "fulltext" if paras else "description_fallback"
    if not content:
        # 非文字型页面（图集 / 信息图 / 互动地图）：用 meta description 兜底摘要
        ogdesc = soup.find("meta", property="og:description") or soup.find(
            "meta", attrs={"name": "description"})
        if ogdesc and ogdesc.get("content"):
            content = ogdesc["content"].strip()
    if not source:
        source = "РИА Новости"

    return {
        "detail_title": title,
        "publish_time": publish_time,
        "source": source,
        "content": content,
        "content_source": content_source,
        "images": imgs,
    }


# ---------------- 详情：静态优先，Playwright 降级 ----------------
def fetch_detail(url: str, cookies) -> dict:
    empty = {"detail_title": "", "publish_time": "", "source": "", "content": "", "images": []}
    try:
        r = requests.get(url, headers=HEADERS, cookies=cookies, timeout=REQUEST_TIMEOUT)
        if r.status_code == 200:
            soup = BeautifulSoup(r.text, "lxml")
            d = parse_detail_html(soup, url)
            if d["content"]:
                return d
    except Exception:
        pass

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
                window.chrome = {runtime: {}};
            """)
            if cookies:
                ctx.add_cookies([
                    {"name": k, "value": v, "domain": ".ria.ru", "path": "/"}
                    for k, v in cookies.items()
                ])
            pg = ctx.new_page()
            pg.goto(url, wait_until="domcontentloaded", timeout=30000)
            pg.wait_for_timeout(2500)
            html = pg.content()
            b.close()
        return parse_detail_html(BeautifulSoup(html, "lxml"), url)
    except Exception as e:
        print(f"    [详情] 渲染失败：{e}")
        return empty


# ---------------- 主流程 ----------------
def atomic_save(data, path=OUTPUT_JSON):
    """原子保护性写入 JSON：先写 path.tmp（flush+fsync），再 os.replace 改名覆盖，
    杜绝进程中断/断电导致半截 JSON 损坏。逐篇实时调用，跑多少存多少。"""
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
    ap = argparse.ArgumentParser(description="俄新社(RIA Novosti) 新闻双引擎爬虫")
    ap.add_argument("--no-detail", action="store_true", help="仅抓全量列表，不解析正文（快）")
    ap.add_argument("--limit", type=int, default=0, help="仅处理前 N 条（调试），0 表示全部")
    ap.add_argument("--root", default=RIA_ROOT, help="列表页 URL（默认首页，可换频道页）")
    ap.add_argument("--cookie", default="", help="注入 cookie：name1=val1; name2=val2")
    args = ap.parse_args()

    cookies = load_cookies(args.cookie)

    items = fetch_list_static(args.root, cookies)
    if items is None:
        items = fetch_list_render(args.root, cookies)
    if not items:
        print("[-] 未获取到任何文章。可能页面结构变化或被反爬拦截。")
        return

    if args.limit > 0:
        items = items[: args.limit]
        print(f"[limit] 仅处理前 {len(items)} 条")

    if args.no_detail:
        print(f"[no-detail] 已获取列表 {len(items)} 条，写出链接清单。")
        out = {
            "source": RIA_ROOT,
            "crawled_at": datetime.now(MSK).isoformat(),
            "count": len(items),
            "items": items,
        }
        atomic_save(out, OUTPUT_JSON)
        print(f"[+] 已写入 {OUTPUT_JSON}（{len(items)} 条）")
        return

    print(f"[详情] 逐篇解析正文（共 {len(items)} 篇，间隔 {REQUEST_INTERVAL}s）...")
    try:
        for idx, it in enumerate(items, 1):
            print(f"  ({idx}/{len(items)}) {it['url']}")
            it.update(fetch_detail(it["url"], cookies))
            out = {
                "source": RIA_ROOT,
                "crawled_at": datetime.now(MSK).isoformat(),
                "count": len(items),
                "items": items,
            }
            atomic_save(out, OUTPUT_JSON)  # 抓一篇存一篇
            time.sleep(REQUEST_INTERVAL)
    except (KeyboardInterrupt, Exception) as exc:
        out = {
            "source": RIA_ROOT,
            "crawled_at": datetime.now(MSK).isoformat(),
            "count": len(items),
            "items": items,
        }
        atomic_save(out, OUTPUT_JSON)
        print(f"\n[!] 已实时保存至当前进度（{len(items)} 条）→ {OUTPUT_JSON}")
        raise

    # 收尾再存一次（确保最终一致）
    out = {
        "source": RIA_ROOT,
        "crawled_at": datetime.now(MSK).isoformat(),
        "count": len(items),
        "items": items,
    }
    atomic_save(out, OUTPUT_JSON)
    print(f"[+] 已写入 {OUTPUT_JSON}（{len(items)} 条）")


if __name__ == "__main__":
    main()
