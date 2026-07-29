# -*- coding: utf-8 -*-
"""
donga_crawler.py — 东亚日报(동아일보/Donga Ilbo) 新闻双引擎爬虫
====================================================================
架构（双引擎方法论）：
  引擎1 静态优先：requests + BeautifulSoup 解析列表与正文（Donga 为 SSR，静态即可，实测 200）
  引擎2 渲染降级：Playwright 无头渲染（仅当静态被反爬拦截时触发）

合规：
  - robots 仅打印提示，不阻断（按用户要求）；3s 请求间隔；仅个人学习/研究用途。
  - 内容版权归 동아일보，禁止商用、禁止批量转发。

用法：
  python donga_crawler.py                       # 全量：列表 + 每篇正文/图片（默认）
  python donga_crawler.py --no-detail           # 仅抓全量列表（快）
  python donga_crawler.py --limit 3             # 前 3 篇含正文，调试
  python donga_crawler.py --root https://www.donga.com/news/Economy   # 指定频道
  python donga_crawler.py --cookie "k=v;k2=v2"  # 可选注入 cookie
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
DONGA_ROOT = "https://www.donga.com/"

USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
)
HEADERS = {
    "User-Agent": USER_AGENT,
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
    "Accept-Language": "ko-KR,ko;q=0.9,en;q=0.8",
    "Referer": "https://www.donga.com/",
}
REQUEST_INTERVAL = 3
REQUEST_TIMEOUT = 15
OUTPUT_JSON = "data/新闻/donga_collection.json"

# 文章 URL：/news/<栏目>/article/all/<YYYYMMDD>/<数字ID>[/<分页>]
# 排除 /Series/ /List/ /daily 等系列/列表页
ARTICLE_RE = re.compile(r"donga\.com/news/[A-Za-z]+/article/(?:all/)?\d{8}/\d+")
# 用于去重：提取 article 路径核心 ID（忽略结尾分页）
ARTICLE_KEY_RE = re.compile(r"/article/(?:all/)?(\d{8})/(\d+)")

# 韩国时区（mk/yna 教训：用 tzinfo，不要 datetime+9h）
KST = timezone(timedelta(hours=9), name="KST")

# 正文内 UI 噪音词
UI_NOISE = ("구독", "추천", "안녕하세요", "디지털뉴스팀", "무단 전재", "재배포 금지",
            "Copyright", "@donga.com")

# 非内容图过滤
NOISE_IMG = ("pixel", "spacer", "1x1", "/ad/", "icon", "logo", "emoji", "placeholder")


# ---------------- robots（仅提示，不阻断） ----------------

# ---------------- cookie ----------------
def load_cookies(cookie_arg: str) -> dict:
    import os
    raw = cookie_arg.strip()
    if not raw and os.path.exists("cookie.txt"):
        raw = open("cookie.txt", encoding="utf-8").read().strip()
        if raw:
            print("[cookie] 已从 cookie.txt 读取")
    if not raw:
        raw = os.environ.get("DONGA_COOKIE", "").strip()
        if raw:
            print("[cookie] 已从环境变量 DONGA_COOKIE 读取")
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
        full = urljoin(DONGA_ROOT, a["href"].strip())
        if not ARTICLE_RE.search(full):
            continue
        # 按文章核心 ID 去重（忽略结尾分页号）
        mk = ARTICLE_KEY_RE.search(full)
        key = mk.group(0) if mk else full.split("?")[0]
        if key in seen:
            continue
        seen.add(key)
        title = a.get_text(strip=True)
        # 纯图片锚点：从 img alt 兜底
        img = a.find("img")
        if len(title) < 8:
            if img and img.get("alt") and len(img["alt"].strip()) >= 8:
                title = img["alt"].strip()
            else:
                continue
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
            args=["--disable-blink-features=AutomationControlled", "--no-sandbox"],
        )
        ctx = b.new_context(user_agent=USER_AGENT, locale="ko-KR",
                            viewport={"width": 1366, "height": 900})
        ctx.add_init_script("""
            Object.defineProperty(navigator, 'webdriver', {get: () => undefined});
            Object.defineProperty(navigator, 'plugins', {get: () => [1, 2, 3, 4, 5]});
            window.chrome = {runtime: {}};
        """)
        if cookies:
            ctx.add_cookies([
                {"name": k, "value": v, "domain": ".donga.com", "path": "/"}
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
            "a[href*='/article/']",
            "els => els.map(e => ({t: (e.innerText||'').trim(), h: e.href, alt: (e.querySelector('img')||{}).alt||''}))",
        )
        b.close()

    items = []
    seen = set()
    for x in links:
        if not ARTICLE_RE.search(x["h"]):
            continue
        mk = ARTICLE_KEY_RE.search(x["h"])
        key = mk.group(0) if mk else x["h"].split("?")[0]
        if key in seen:
            continue
        seen.add(key)
        title = x["t"] if len(x["t"]) >= 8 else x.get("alt", "").strip()
        if len(title) < 8:
            continue
        items.append({"title": title, "url": x["h"]})
    print(f"[列表] Playwright 命中 {len(items)} 条")
    return items


# ---------------- 详情解析（双引擎复用） ----------------
def parse_detail_html(soup: BeautifulSoup, url: str) -> dict:
    # 标题：h1（无 class）
    title = ""
    h1 = soup.find("h1")
    if h1:
        title = h1.get_text(strip=True)
    if not title:
        og = soup.find("meta", property="og:title")
        if og and og.get("content"):
            title = og["content"].strip()

    # 时间：'입력2026-07-29 00:00...' 或 '2026년 7월 29일 00시 00분'
    publish_time = ""
    m = re.search(r"입력\s*(\d{4})-(\d{2})-(\d{2})\s+(\d{2}):(\d{2})", soup.get_text())
    if not m:
        m = re.search(r"(\d{4})년\s*(\d{1,2})월\s*(\d{1,2})일\s*(\d{1,2})시\s*(\d{2})분", soup.get_text())
        if m:
            y, mo, d, hh, mm = m.groups()
            publish_time = datetime(int(y), int(mo), int(d), int(hh), int(mm), tzinfo=KST).isoformat()
    if m and not publish_time:
        y, mo, d, hh, mm = m.groups()
        publish_time = datetime(int(y), int(mo), int(d), int(hh), int(mm), tzinfo=KST).isoformat()

    # 正文容器
    wrap = soup.select_one(".news_view") or soup.select_one("#articleBody") or soup.select_one(".article_txt")
    paras = []
    if wrap:
        p_tags = wrap.select(".article_txt p") or wrap.find_all("p")
        if p_tags:
            for p in p_tags:
                t = p.get_text(" ", strip=True)
                if len(t) > 15 and not any(noise in t for noise in UI_NOISE):
                    paras.append(t)
        else:
            for line in wrap.get_text("\n", strip=True).split("\n"):
                t = line.strip()
                if len(t) > 15 and not any(noise in t for noise in UI_NOISE):
                    paras.append(t)
    if not paras:
        for p in soup.find_all("p"):
            t = p.get_text(" ", strip=True)
            if len(t) > 20 and not any(noise in t for noise in UI_NOISE):
                paras.append(t)
    content = "\n".join(paras)

    # 来源/记者：'.byline' -> '김예슬 기자 seul56@donga.com'
    source = ""
    byline = soup.select_one(".byline")
    if byline:
        bm = re.search(r"([가-힣A-Za-z]+)\s*기자", byline.get_text())
        if bm:
            source = bm.group(1) + " 기자"
    if not source:
        sm = re.search(r"([가-힣A-Za-z]+)\s*기자", content[:300])
        source = (sm.group(1) + " 기자") if sm else "동아일보"

    # 图片：容器内 + 'dimg.donga.com' / 'image.donga.com'
    imgs = []
    seen_img = set()
    scope = wrap if wrap else soup
    for i in scope.find_all("img"):
        src = i.get("src") or i.get("data-src") or ""
        if not src:
            continue
        full = urljoin(url, src)
        low = full.lower()
        if any(k in low for k in NOISE_IMG):
            continue
        if "donga.com" not in full:
            continue
        if full in seen_img:
            continue
        seen_img.add(full)
        imgs.append(full)

    return {
        "detail_title": title,
        "publish_time": publish_time,
        "source": source,
        "content": content,
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
                args=["--disable-blink-features=AutomationControlled", "--no-sandbox"],
            )
            ctx = b.new_context(user_agent=USER_AGENT, locale="ko-KR",
                                viewport={"width": 1366, "height": 900})
            ctx.add_init_script("""
                Object.defineProperty(navigator, 'webdriver', {get: () => undefined});
                window.chrome = {runtime: {}};
            """)
            if cookies:
                ctx.add_cookies([
                    {"name": k, "value": v, "domain": ".donga.com", "path": "/"}
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

def _atomic_save(path, data):
    """原子写入 JSON：先写 .tmp，再 rename 覆盖，防止进程中断导致文件损坏。

    每抓完一篇即调用（实时落盘），进程被中断（Ctrl+C / 断电）也只丢当前这篇，
    已落盘数据不丢。写前自动创建目标目录。
    """
    d = os.path.dirname(path)
    if d:
        os.makedirs(d, exist_ok=True)
    tmp = path + ".tmp"
    try:
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False, indent=2)
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp, path)
    except Exception as exc:
        print(f"[atomic_save] 写入失败: {exc}")

def main():
    ap = argparse.ArgumentParser(description="东亚日报(Donga) 新闻双引擎爬虫")
    ap.add_argument("--no-detail", action="store_true", help="仅抓全量列表，不解析正文（快）")
    ap.add_argument("--limit", type=int, default=0, help="仅处理前 N 条（调试），0 表示全部")
    ap.add_argument("--root", default=DONGA_ROOT, help="列表页 URL（默认首页，可换频道页）")
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

    try:
        if not args.no_detail:
            print(f"[详情] 逐篇解析正文（共 {len(items)} 篇，间隔 {REQUEST_INTERVAL}s）...")
            for idx, it in enumerate(items, 1):
                print(f"  ({idx}/{len(items)}) {it['url']}")
                it.update(fetch_detail(it["url"], cookies))
                # 实时原子落盘（包裹结构，与收尾一致）
                partial = {
                    "source": "https://www.donga.com/",
                    "crawled_at": datetime.now(KST).isoformat(),
                    "count": idx,
                    "items": items[:idx],
                }
                _atomic_save(OUTPUT_JSON, partial)
                time.sleep(REQUEST_INTERVAL)
    except (KeyboardInterrupt, Exception) as e:
        print(f"\n[warn] 采集中断（{type(e).__name__}）：已实时保存至当前进度 → {OUTPUT_JSON}")
        raise

    out = {
        "source": "https://www.donga.com/",
        "crawled_at": datetime.now(KST).isoformat(),
        "count": len(items),
        "items": items,
    }
    _atomic_save(OUTPUT_JSON, out)  # 收尾原子保存（与实时写入同格式）
    print(f"[+] 已写入 {OUTPUT_JSON}（{len(items)} 条）")


if __name__ == "__main__":
    main()
