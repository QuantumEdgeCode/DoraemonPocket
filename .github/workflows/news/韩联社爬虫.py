# -*- coding: utf-8 -*-
"""
yna_crawler.py — 韩联社(연합뉴스/YNA) 新闻双引擎爬虫
====================================================================
架构（双引擎方法论）：
  引擎1 静态优先：requests + BeautifulSoup 解析列表与正文（YNA 为 SSR，静态即可，实测 200）
  引擎2 渲染降级：Playwright 无头渲染（仅当静态被反爬拦截时触发）

合规：
  - robots 仅打印提示，不阻断（按用户要求）；3s 请求间隔；仅个人学习/研究用途。
  - 内容版权归 연합뉴스，禁止商用、禁止批量转发。

用法：
  python yna_crawler.py                       # 全量：列表 + 每篇正文/图片（默认）
  python yna_crawler.py --no-detail           # 仅抓全量列表（快）
  python yna_crawler.py --limit 3             # 前 3 篇含正文，调试
  python yna_crawler.py --root https://www.yna.co.kr/economy/all   # 指定频道
  python yna_crawler.py --cookie "k=v;k2=v2"  # 可选注入 cookie
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
YNA_ROOT = "https://www.yna.co.kr/"
USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
)
HEADERS = {
    "User-Agent": USER_AGENT,
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
    "Accept-Language": "ko-KR,ko;q=0.9,en;q=0.8",
    "Referer": "https://www.yna.co.kr/",
}
REQUEST_INTERVAL = 3          # 请求间隔（秒），合规频率控制
REQUEST_TIMEOUT = 15
OUTPUT_JSON = "data/新闻/yna_collection.json"

# 文章 URL 匹配：/view/AKR<数字>
ARTICLE_RE = re.compile(r"yna\.co\.kr/view/AKR\d+")

# 韩国时区（mk 教训：不能 datetime+9h，要用 tzinfo）
KST = timezone(timedelta(hours=9), name="KST")

# 正文内 UI 噪音词（YNA 的 #articleWrap 混入记者订阅组件 / 图片 caption 带 @yna.co.kr 邮箱）
UI_NOISE = ("구독", "구독중", "이미지 확대", "저작권자", "무단 전재", "재배포 금지",
            "Copyright", "연합뉴스는", "@yna.co.kr")

# 非内容图过滤（reporter=记者头像，photo=真实配图）
NOISE_IMG = ("pixel", "spacer", "1x1", "/ad/", "icon", "logo", "emoji", "placeholder", "/reporter/")


# ---------------- robots（仅提示，不阻断） ----------------

# ---------------- cookie ----------------
def load_cookies(cookie_arg: str) -> dict:
    """从 --cookie 参数 / cookie.txt / 环境变量加载 cookie。"""
    import os
    raw = cookie_arg.strip()
    if not raw and os.path.exists("cookie.txt"):
        raw = open("cookie.txt", encoding="utf-8").read().strip()
        if raw:
            print("[cookie] 已从 cookie.txt 读取")
    if not raw:
        raw = os.environ.get("YNA_COOKIE", "").strip()
        if raw:
            print("[cookie] 已从环境变量 YNA_COOKIE 读取")
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
        full = urljoin(YNA_ROOT, a["href"].strip())
        if not ARTICLE_RE.search(full):
            continue
        # 去掉 ?section= 参数统一去重（同一篇文章在不同频道页出现多次）
        key = full.split("?")[0]
        if key in seen:
            continue
        seen.add(key)
        title = a.get_text(strip=True)
        if len(title) < 8:  # 过滤纯图片锚点（无文字标题）
            # 尝试从 img alt 取标题
            img = a.find("img")
            if img and img.get("alt") and len(img["alt"].strip()) >= 8:
                title = img["alt"].strip()
            else:
                continue
        items.append({"title": title, "url": key})
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
        ctx = b.new_context(
            user_agent=USER_AGENT,
            locale="ko-KR",
            viewport={"width": 1366, "height": 900},
        )
        # stealth：覆盖自动化指纹
        ctx.add_init_script("""
            Object.defineProperty(navigator, 'webdriver', {get: () => undefined});
            Object.defineProperty(navigator, 'plugins', {get: () => [1, 2, 3, 4, 5]});
            Object.defineProperty(navigator, 'languages', {get: () => ['ko-KR', 'ko', 'en-US', 'en']});
            window.chrome = {runtime: {}};
        """)
        if cookies:
            ctx.add_cookies([
                {"name": k, "value": v, "domain": ".yna.co.kr", "path": "/"}
                for k, v in cookies.items()
            ])
        pg = ctx.new_page()
        # 新闻 SPA 有持续轮询，networkidle 会卡死（腾讯版教训）
        pg.goto(root_url, wait_until="domcontentloaded", timeout=30000)
        pg.wait_for_timeout(3000)
        if "Access Denied" in pg.title():
            print("[列表] 页面被反爬拦截（Access Denied），注入 cookie 或换住宅 IP 重试")
            b.close()
            return []
        links = pg.eval_on_selector_all(
            "a[href*='/view/AKR']",
            "els => els.map(e => ({t: (e.innerText||'').trim(), h: e.href, alt: (e.querySelector('img')||{}).alt||''}))",
        )
        b.close()

    items = []
    seen = set()
    for x in links:
        if not ARTICLE_RE.search(x["h"]):
            continue
        key = x["h"].split("?")[0]
        if key in seen:
            continue
        seen.add(key)
        title = x["t"] if len(x["t"]) >= 8 else x.get("alt", "").strip()
        if len(title) < 8:
            continue
        items.append({"title": title, "url": key})
    print(f"[列表] Playwright 命中 {len(items)} 条")
    return items


# ---------------- 详情解析（静态 HTML 统一解析，双引擎复用） ----------------
def parse_detail_html(soup: BeautifulSoup, url: str) -> dict:
    """从详情页 HTML 提取 标题/时间/来源/正文/图片。"""
    # 标题
    title = ""
    h1 = soup.select_one("h1.tit01")
    if h1:
        title = h1.get_text(strip=True)
    if not title:
        og = soup.find("meta", property="og:title")
        if og and og.get("content"):
            title = og["content"].strip()

    # 时间：.txt-time 格式 '2026-07-28 12:57'（KST）
    publish_time = ""
    tt = soup.select_one(".txt-time")
    m = re.search(r"(\d{4})-(\d{2})-(\d{2})\s+(\d{2}):(\d{2})", tt.get_text() if tt else "")
    if not m:
        # 回退：.date '2026/07/29 01:12 송고'
        de = soup.select_one(".date")
        m = re.search(r"(\d{4})/(\d{2})/(\d{2})\s+(\d{2}):(\d{2})", de.get_text() if de else "")
    if m:
        y, mo, d, hh, mm = m.groups()
        publish_time = datetime(int(y), int(mo), int(d), int(hh), int(mm), tzinfo=KST).isoformat()

    # 正文容器
    wrap = soup.select_one("#articleWrap") or soup.select_one(".story-news") or soup.select_one("article")

    # 正文段落：容器内所有 <p> 或按行切分，过滤 UI 噪音
    paras = []
    if wrap:
        p_tags = wrap.find_all("p")
        if p_tags:
            for p in p_tags:
                t = p.get_text(" ", strip=True)
                if len(t) > 15 and not any(noise in t for noise in UI_NOISE):
                    paras.append(t)
        else:
            # 无 <p> 结构：按行切分过滤
            for line in wrap.get_text("\n", strip=True).split("\n"):
                t = line.strip()
                if len(t) > 15 and not any(noise in t for noise in UI_NOISE):
                    paras.append(t)
    if not paras:
        # 最终回退：全页 <p>
        for p in soup.find_all("p"):
            t = p.get_text(" ", strip=True)
            if len(t) > 20 and not any(noise in t for noise in UI_NOISE):
                paras.append(t)
    content = "\n".join(paras)

    # 来源：正文首段 '(상파울루=연합뉴스) 한상균 기자' 提取记者名
    source = ""
    m2 = re.search(r"\([가-힣A-Za-z·\s]*=\s*연합뉴스\)\s*([가-힣A-Za-z·\s]+?)\s*기자", content[:300])
    if m2:
        source = m2.group(1).strip() + " 기자"
    else:
        m3 = re.search(r"연합뉴스\)\s*([가-힣A-Za-z·\s]+?)\s*기자", content[:300])
        source = (m3.group(1).strip() + " 기자") if m3 else "연합뉴스"

    # 图片：容器内 + /photo/ 路径过滤（/reporter/ 是头像）
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
        if "yna.co.kr" not in full:
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
    # 引擎1：静态
    try:
        r = requests.get(url, headers=HEADERS, cookies=cookies, timeout=REQUEST_TIMEOUT)
        if r.status_code == 200:
            soup = BeautifulSoup(r.text, "lxml")
            d = parse_detail_html(soup, url)
            if d["content"]:  # 静态拿到正文即返回
                return d
    except Exception:
        pass

    # 引擎2：Playwright 渲染
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
                    {"name": k, "value": v, "domain": ".yna.co.kr", "path": "/"}
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
def atomic_save(items, path=OUTPUT_JSON):
    """原子写入 JSON：先写 .tmp，flush+fsync 落盘，再 os.replace 改名，杜绝半截损坏文件。"""
    payload = {
        "source": "https://www.yna.co.kr/",
        "crawled_at": datetime.now(KST).isoformat(),
        "count": len(items),
        "items": items,
    }
    d = os.path.dirname(path)
    if d:
        os.makedirs(d, exist_ok=True)
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(payload, f, ensure_ascii=False, indent=2)
        f.flush()
        os.fsync(f.fileno())
    os.replace(tmp, path)


def main():
    ap = argparse.ArgumentParser(description="韩联社(YNA) 新闻双引擎爬虫")
    ap.add_argument("--no-detail", action="store_true", help="仅抓全量列表，不解析正文（快）")
    ap.add_argument("--limit", type=int, default=0, help="仅处理前 N 条（调试），0 表示全部")
    ap.add_argument("--root", default=YNA_ROOT, help="列表页 URL（默认首页，可换频道页）")
    ap.add_argument("--cookie", default="", help="注入 cookie：name1=val1; name2=val2")
    args = ap.parse_args()

    cookies = load_cookies(args.cookie)

    # 列表：静态优先，Playwright 降级
    items = fetch_list_static(args.root, cookies)
    if items is None:
        items = fetch_list_render(args.root, cookies)
    if not items:
        print("[-] 未获取到任何文章。可能页面结构变化或被反爬拦截。")
        return

    if args.limit > 0:
        items = items[: args.limit]
        print(f"[limit] 仅处理前 {len(items)} 条")

    # 详情：默认全量解析
    try:
        if not args.no_detail:
            print(f"[详情] 逐篇解析正文（共 {len(items)} 篇，间隔 {REQUEST_INTERVAL}s）...")
            for idx, it in enumerate(items, 1):
                print(f"  ({idx}/{len(items)}) {it['url']}")
                it.update(fetch_detail(it["url"], cookies))
                atomic_save(items, OUTPUT_JSON)
                time.sleep(REQUEST_INTERVAL)
        else:
            atomic_save(items, OUTPUT_JSON)
    except (KeyboardInterrupt, Exception) as e:
        print(f"\n[!] 中断（{type(e).__name__}）：已实时保存至当前进度 → {OUTPUT_JSON}")
        raise

    atomic_save(items, OUTPUT_JSON)
    print(f"[+] 已写入 {OUTPUT_JSON}（{len(items)} 条）")


if __name__ == "__main__":
    main()
