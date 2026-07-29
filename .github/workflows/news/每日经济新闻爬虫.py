#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Maekyung (매일경제 / mk.co.kr) 双引擎爬虫
=================================================
架构：接口/SSR 静态优先 (requests + BeautifulSoup) -> Playwright 无头渲染降级
说明：mk.co.kr 为服务端渲染 (SSR)，静态引擎即可直接解析列表与正文，Playwright 仅兜底。
合规：robots.txt 仅作提示，不阻断（依用户要求直接爬）；内容版权归 Maekyung，仅限个人学习/研究。

用法：
  python mk_crawler.py                 # 全量：列表 + 每篇正文/图片
  python mk_crawler.py --no-detail     # 仅抓全量列表（快，静态引擎）
  python mk_crawler.py --limit 3       # 前 3 篇含正文，调试用
  python mk_crawler.py --root https://www.mk.co.kr/news/economy   # 指定频道/栏目
  # 可选：注入 cookie（极端情况绕过限制）
  python mk_crawler.py --cookie "name1=val1; name2=val2"
"""
import argparse
import json
import os
import re
import time
from datetime import datetime, timedelta, timezone
from urllib.parse import urljoin

import requests
from bs4 import BeautifulSoup

try:
    from playwright.sync_api import sync_playwright
except Exception:
    sync_playwright = None

MK_ROOT = "https://www.mk.co.kr/"
USER_AGENT = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
              "(KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36")
REQUEST_INTERVAL = 3  # 请求间隔（秒），合规节流
OUTPUT_FILE = "data/新闻/mk_collection.json"

HEADERS = {
    "User-Agent": USER_AGENT,
    "Accept-Language": "ko-KR,ko;q=0.9,en;q=0.8",
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
}

# 文章路径：/news/<栏目>/<数字ID>，排除纯栏目首页（结尾无数字）
SECTION_RE = re.compile(r"/news/(?:economy|financial|stock|realestate|it|politics|world|culture|sports|life|opinion)/(\d{4,})$")
# 栏目首页（无数字ID）排除
SECTION_INDEX_RE = re.compile(r"/news/(?:economy|financial|stock|realestate|it|politics|world|culture|sports|life|opinion)/?$")

# 非内容图过滤关键词
NOISE_IMG = ("pixel", "spacer", "1x1", "/ad/", "icon", "logo", "avatar", "emoji",
             "placeholder", "btn", "banner", "share", "sns", "thumb_m")

KST = timedelta(hours=9)  # 韩国时区



# ---------------- 静态引擎：列表 ----------------
def fetch_list_static(root):
    try:
        r = requests.get(root, headers=HEADERS, timeout=15)
        if r.status_code != 200:
            print(f"[列表] 静态引擎返回 {r.status_code}（WAF 拦截），将降级 Playwright")
            return None
        return parse_list_html(r.text, root)
    except Exception as e:
        print(f"[列表] 静态引擎异常：{e}，将降级 Playwright")
        return None


def parse_list_html(html, root):
    soup = BeautifulSoup(html, "lxml")
    items = []
    for a in soup.find_all("a", href=True):
        href = a["href"].strip()
        full = urljoin(root, href)
        if not SECTION_RE.search(full):
            continue
        if SECTION_INDEX_RE.search(full):  # 跳过栏目首页本身
            continue
        title = a.get_text(strip=True)
        if len(title) < 8:  # 过滤导航碎片
            continue
        items.append({"title": title, "url": full})
    # 去重（保留首次出现）
    seen, uniq = set(), []
    for it in items:
        if it["url"] in seen:
            continue
        seen.add(it["url"])
        uniq.append(it)
    print(f"[列表] 静态引擎命中 {len(uniq)} 条")
    return uniq


# ---------------- Playwright 降级：列表 ----------------
def fetch_list_playwright(root):
    if not sync_playwright:
        print("[列表] Playwright 不可用，无法降级")
        return []
    items = []
    try:
        with sync_playwright() as p:
            b = p.chromium.launch(headless=True,
                                  args=["--disable-blink-features=AutomationControlled"])
            ctx = b.new_context(user_agent=USER_AGENT, viewport={"width": 1366, "height": 900},
                                locale="ko-KR")
            pg = ctx.new_page()
            pg.goto(root, wait_until="domcontentloaded", timeout=30000)
            pg.wait_for_timeout(2500)
            links = pg.eval_on_selector_all(
                "a[href*='/news/']",
                "els => els.map(e => e.href)"
            )
            for h in links:
                full = urljoin(root, h)
                if not SECTION_RE.search(full):
                    continue
                if SECTION_INDEX_RE.search(full):
                    continue
                title = ""
                try:
                    title = pg.evaluate(
                        "(url) => { const a=[...document.querySelectorAll('a[href]')].find(x=>x.href===url); return a?a.innerText.trim():''; }",
                        full)
                except Exception:
                    pass
                if len(title) < 8:
                    continue
                items.append({"title": title, "url": full})
            b.close()
    except Exception as e:
        print(f"[列表] Playwright 异常：{e}")
    seen, uniq = set(), []
    for it in items:
        if it["url"] in seen:
            continue
        seen.add(it["url"])
        uniq.append(it)
    print(f"[列表] Playwright 降级命中 {len(uniq)} 条")
    return uniq


# ---------------- 详情解析（静态 HTML / Playwright 渲染 HTML 统一复用） ----------------
def parse_detail_html(soup, url):
    # 标题
    title_el = soup.select_one(".view_head_title")
    title = title_el.get_text(strip=True) if title_el else (soup.find("h1").get_text(strip=True) if soup.find("h1") else "")

    # 时间 / 来源：在 .article 容器内
    article_el = soup.select_one(".article") or soup
    article_text = article_el.get_text(" ", strip=True) if article_el else soup.get_text(" ", strip=True)

    # 发布时间：입력 : 2026.07.28 18:08
    m = re.search(r"입력\s*:\s*(\d{4})\.(\d{2})\.(\d{2})\s+(\d{2}):(\d{2})", article_text)
    publish_time = ""
    if m:
        y, mo, d, hh, mm = m.groups()
        publish_time = datetime(int(y), int(mo), int(d), int(hh), int(mm),
                                tzinfo=timezone(KST)).isoformat()

    # 来源 / 作者：含 "기자" 的片段，如 "박창영 기자"
    source = ""
    sm = re.search(r"([가-힣A-Za-z]+)\s*기자", article_text)
    if sm:
        source = sm.group(1).strip() + " 기자"

    # 正文容器候选
    container = None
    for sel in ["#article_body", ".news_cnt", ".article_body", "#news_body", ".view_text", ".news_view"]:
        el = soup.select_one(sel)
        if el and len(el.get_text(strip=True)) > 80:
            container = el
            break

    paras = []
    if container:
        # 优先取 .content_section 分段
        sections = container.select(".content_section") or container.select("p")
        for s in sections:
            t = s.get_text(strip=True)
            if len(t) > 15:
                paras.append(t)
    else:
        # 回退：所有 .content_section
        for s in soup.select(".content_section"):
            t = s.get_text(strip=True)
            if len(t) > 15:
                paras.append(t)
        if not paras:
            for p in soup.select(".article p"):
                t = p.get_text(strip=True)
                if len(t) > 15:
                    paras.append(t)

    # 图片：统一从 .article 容器内取（覆盖常规文与解说文，content_section 之外也可能有配图）
    img_scope = soup.select_one(".article") or soup
    imgs = []
    for i in img_scope.select("img"):
        src = i.get("src") or i.get("data-src")
        if src:
            imgs.append(urljoin(url, src))

    content = "\n".join(paras)

    # 图片去噪 + 过滤 + 协议相对补全
    seen_img = set()
    clean_imgs = []
    for src in imgs:
        low = src.lower()
        if any(k in low for k in NOISE_IMG):
            continue
        if src in seen_img:
            continue
        seen_img.add(src)
        if low.startswith("//"):
            src = "https:" + src
        clean_imgs.append(src)

    return {
        "content": content,
        "publish_time": publish_time,
        "source": source,
        "detail_title": title,
        "images": clean_imgs,
    }


def fetch_detail_static(url):
    try:
        r = requests.get(url, headers=HEADERS, timeout=15)
        if r.status_code != 200:
            print(f"    [详情] 静态 {r.status_code}，降级 Playwright：{url}")
            return None
        return parse_detail_html(BeautifulSoup(r.text, "lxml"), url)
    except Exception as e:
        print(f"    [详情] 静态异常：{e}，降级 Playwright")
        return None


def fetch_detail_playwright(url):
    """Playwright 仅负责拿到渲染后的 HTML，解析逻辑复用 parse_detail_html。"""
    if not sync_playwright:
        return {"content": "", "publish_time": "", "source": "", "detail_title": "", "images": []}
    try:
        with sync_playwright() as p:
            b = p.chromium.launch(headless=True,
                                  args=["--disable-blink-features=AutomationControlled"])
            ctx = b.new_context(user_agent=USER_AGENT, viewport={"width": 1366, "height": 900},
                                locale="ko-KR")
            pg = ctx.new_page()
            pg.goto(url, wait_until="domcontentloaded", timeout=30000)
            pg.wait_for_timeout(3000)
            html = pg.content()
            b.close()
            return parse_detail_html(BeautifulSoup(html, "lxml"), url)
    except Exception as e:
        print(f"    [详情] Playwright 异常：{e}")
        return {"content": "", "publish_time": "", "source": "", "detail_title": "", "images": []}


def load_cookies(arg):
    cookies = {}
    raw = arg or os.environ.get("MK_COOKIE", "")
    if raw:
        for kv in raw.split(";"):
            kv = kv.strip()
            if "=" in kv:
                k, v = kv.split("=", 1)
                cookies[k.strip()] = v.strip()
    return cookies


def atomic_save(items, path=OUTPUT_FILE, source=MK_ROOT):
    """原子写入 JSON：先写 .tmp -> flush+fsync -> os.replace 改名，防止进程中断导致半截文件。"""
    payload = {
        "source": source,
        "fetched_at": datetime.now().isoformat(timespec="seconds"),
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
    ap = argparse.ArgumentParser(description="Maekyung (mk.co.kr) 双引擎爬虫")
    ap.add_argument("--root", default=MK_ROOT, help="起始页（首页或指定频道）")
    ap.add_argument("--no-detail", action="store_true", help="仅抓列表，不解析正文")
    ap.add_argument("--limit", type=int, default=0, help="仅处理前 N 条（测试/控规模），0 表示全部")
    ap.add_argument("--cookie", default="", help="可选 cookie：name1=val1; name2=val2")
    args = ap.parse_args()

    root = args.root.rstrip("/") + "/"

    # 列表
    items = fetch_list_static(root)
    if not items:
        items = fetch_list_playwright(root)

    if not items:
        print("[-] 两个引擎均未获取到数据。可能需代理/真实环境。")
        return

    if args.limit > 0:
        items = items[:args.limit]

    # 详情解析（静态优先 + Playwright 降级）
    try:
        if not args.no_detail:
            print(f"[详情] 逐篇解析正文（共 {len(items)} 篇，间隔 {REQUEST_INTERVAL}s）...")
            for idx, it in enumerate(items, 1):
                print(f"  ({idx}/{len(items)}) {it['url']}")
                detail = fetch_detail_static(it["url"])
                if detail is None:
                    detail = fetch_detail_playwright(it["url"])
                it.update(detail or {})
                atomic_save(items, OUTPUT_FILE, root)  # 每篇实时落盘
                time.sleep(REQUEST_INTERVAL)

        atomic_save(items, OUTPUT_FILE, root)  # 收尾再存
        print(f"[完成] 已写入 {OUTPUT_FILE}，共 {len(items)} 条")
    except (KeyboardInterrupt, Exception) as exc:
        atomic_save(items, OUTPUT_FILE, root)  # 中断前保存进度
        print(f"\n[interrupted] 已实时保存至当前进度（{len(items)} 条）-> {OUTPUT_FILE}")
        raise


if __name__ == "__main__":
    main()
