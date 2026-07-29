#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
BBC 双引擎爬虫（学习 / 研究用途）

架构（双引擎方法论，针对 BBC 优化）：
  BBC 为服务端渲染（SSR），因此：
    引擎1（静态优先）：requests + BeautifulSoup 直接解析 HTML，提取列表与正文（最快、最稳）
    引擎2（渲染降级）：当静态解析不足 / 需要 JS 渲染时，用 Playwright 无头渲染真实 DOM

合规红线（务必遵守）：
  - 默认遵守 www.bbc.com/robots.txt，禁止路径直接终止
  - 请求间隔 >= 3s，带超时，不压测服务器
  - 内容仅限个人学习 / 研究，禁止商用、禁止批量转发牟利
  - 不翻付费墙 / 登录墙
"""
import argparse
import json
import os
import time
import re
from urllib.parse import urljoin
import requests
from bs4 import BeautifulSoup

BBC_ROOT = "https://www.bbc.com"

HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0 Safari/537.36"
    ),
    "Accept-Language": "en-US,en;q=0.9",
}

# 优先抓新闻频道（SSR，结构稳定），再退回首页
SECTION_URLS = [
    BBC_ROOT + "/news",
    BBC_ROOT + "/",
]

REQUEST_INTERVAL = 3.0  # 秒，合规频率控制

# 文章路径匹配：
#   新版：任意栏目下的 /articles/<id>（如 /news/articles/cgewgxlpj3po、/sport/football/articles/...）
#   旧版：<栏目>/<slug>-<数字>（如 /news/world-12345678）
ARTICLE_RE = re.compile(
    r"bbc\.com/.+?/articles/[a-z0-9]+"
    r"|bbc\.com/(?:news|sport|business|technology|science-environment|"
    r"health|entertainment-arts|culture|world|uk|us-canada)/"
    r"[a-z]+(?:-[a-z]+)*-\d{5,}"
)
SECTION_INDEX_RE = re.compile(
    r"bbc\.com/(?:news|sport|business|technology|science-environment|"
    r"health|entertainment-arts|culture|world|uk|us-canada)/?$"
)
# 非内容图过滤关键词
NOISE_IMG = ("pixel", "spacer", "1x1", "/ad/", "icon", "logo", "avatar", "emoji", "placeholder")


def fetch_list_static(url: str) -> list:
    """引擎1：静态解析 BBC SSR 页面，提取文章列表 [{title, url}] 或 []"""
    try:
        print(f"[静态] 解析 {url}")
        r = requests.get(url, headers=HEADERS, timeout=15)
        if r.status_code != 200:
            print(f"[静态] 状态码 {r.status_code}")
            return []
        r.encoding = r.apparent_encoding or "utf-8"
        soup = BeautifulSoup(r.text, "lxml")
        items = []
        for a in soup.find_all("a", href=True):
            href = a["href"].strip()
            full = urljoin(BBC_ROOT, href)  # 先补全绝对 URL，再匹配正则
            if not ARTICLE_RE.search(full):
                continue
            if SECTION_INDEX_RE.search(full):  # 跳过栏目首页本身
                continue
            title = a.get_text(strip=True)
            if len(title) < 10:  # 过滤导航碎片
                continue
            items.append({"title": title, "url": full})
        # 去重（按 url）
        seen, uniq = set(), []
        for it in items:
            if it["url"] not in seen:
                seen.add(it["url"])
                uniq.append(it)
        print(f"[静态] 命中 {len(uniq)} 条")
        return uniq
    except Exception as exc:  # noqa: BLE001
        print(f"[静态] 失败: {exc}")
        return []


def fetch_list_js(url: str) -> list:
    """引擎2：Playwright 无头渲染降级，返回文章列表或 []"""
    try:
        from playwright.sync_api import sync_playwright
    except ImportError:
        print("[JS] 未安装 playwright，跳过渲染降级（pip install playwright && playwright install chromium）")
        return []
    try:
        print(f"[JS] 渲染 {url}")
        with sync_playwright() as p:
            b = p.chromium.launch(headless=True)
            pg = b.new_page()
            pg.goto(url, wait_until="domcontentloaded", timeout=30000)
            pg.wait_for_timeout(3000)
            links = pg.eval_on_selector_all(
                "a[href]",
                "els => els.map(e => ({t: e.innerText.trim(), h: e.href}))",
            )
            b.close()
            raw = [{"title": x["t"], "url": x["h"]} for x in links if x["t"]]
            items = [
                x for x in raw
                if ARTICLE_RE.search(x["url"]) and not SECTION_INDEX_RE.search(x["url"])
                and len(x["title"]) >= 10
            ]
            # 去重
            seen, uniq = set(), []
            for it in items:
                if it["url"] not in seen:
                    seen.add(it["url"])
                    uniq.append(it)
            print(f"[JS] 命中 {len(uniq)} 条")
            return uniq
    except Exception as exc:  # noqa: BLE001
        print(f"[JS] 渲染失败: {exc}")
        return []


def _extract_images(container, page_url: str) -> list:
    """从 DOM 容器提取正文图片（兼容懒加载 srcset/data-src）"""
    imgs, seen = [], set()
    for img in container.find_all("img"):
        src = img.get("data-src") or img.get("src") or ""
        if not src:
            ss = img.get("srcset") or img.get("data-srcset") or ""
            if ss:
                # srcset 形如 "url1 1x, url2 2x"，取第一个
                src = ss.split(",")[0].strip().split()[0]
        if not src:
            continue
        full = urljoin(page_url, src)
        low = full.lower()
        if full in seen or any(k in low for k in NOISE_IMG):
            continue
        seen.add(full)
        imgs.append(full)
    return imgs


def fetch_article_detail(url: str) -> dict:
    """详情页解析：优先静态（BBC SSR），失败回退 Playwright。

    提取：title / publish_time / source / content / images
    """
    # —— 引擎1：静态解析 ——
    try:
        r = requests.get(url, headers=HEADERS, timeout=15)
        r.raise_for_status()
        r.encoding = r.apparent_encoding or "utf-8"
        soup = BeautifulSoup(r.text, "lxml")
        article = soup.find("article") or soup.find("main") or soup

        h1 = soup.find("h1")
        title = (
            h1.get_text(strip=True)
            if h1
            else (soup.title.get_text(strip=True) if soup.title else "")
        )

        t = soup.find("time")
        publish_time = (t.get("datetime") or t.get_text(strip=True)) if t else ""

        paras = [p.get_text(strip=True) for p in article.find_all("p")]
        paras = [x for x in paras if len(x) > 20]
        content = "\n".join(paras)

        images = _extract_images(article, url)

        if len(content) > 80:  # 静态成功
            return {
                "content": content,
                "publish_time": publish_time,
                "source": "BBC",
                "detail_title": title,
                "images": images,
            }
        print(f"[详情-静态] 正文过短，尝试渲染降级: {url}")
    except Exception as exc:  # noqa: BLE001
        print(f"[详情-静态] {url} 失败: {exc}")

    # —— 引擎2：Playwright 渲染降级 ——
    return fetch_article_detail_js(url)


def fetch_article_detail_js(url: str) -> dict:
    """Playwright 渲染文章页提取详情（兜底）"""
    try:
        from playwright.sync_api import sync_playwright
    except ImportError:
        print("[详情-JS] 未安装 playwright，跳过")
        return {}
    try:
        with sync_playwright() as p:
            b = p.chromium.launch(headless=True)
            pg = b.new_page()
            pg.goto(url, wait_until="domcontentloaded", timeout=30000)
            pg.wait_for_timeout(2500)

            try:
                title = pg.eval_on_selector("h1", "e => e.innerText.trim()")
            except Exception:
                title = pg.title()

            meta = pg.evaluate(
                """() => {
                    const t = document.querySelector('time');
                    return t ? (t.getAttribute('datetime') || t.innerText.trim()) : '';
                }"""
            )
            publish_time = meta or ""

            container_sels = ["article", "main", "div[data-component='text-block']", "#main-content"]
            container = None
            for sel in container_sels:
                try:
                    if pg.eval_on_selector(sel, "e => e.innerText.trim().length") > 80:
                        container = sel
                        break
                except Exception:
                    continue

            sel_p = (container + " p") if container else "p"
            paras = pg.eval_on_selector_all(
                sel_p,
                "els => els.map(e => e.innerText.trim()).filter(t => t.length > 20)",
            )
            content = "\n".join(paras)

            sel_img = (container + " img") if container else "img"
            raw_imgs = pg.eval_on_selector_all(
                sel_img,
                """els => els.map(e => {
                        const ds = e.getAttribute('data-src');
                        const ss = e.getAttribute('srcset') || e.getAttribute('data-srcset') || '';
                        const src = ds || e.getAttribute('src') || (ss ? ss.split(',')[0].trim().split(' ')[0] : '');
                        return src;
                    }).filter(Boolean).filter(u => /^(https?:)?\\/\\//.test(u))"""
            )
            images, seen = [], set()
            for u in raw_imgs:
                full = urljoin(url, u)
                low = full.lower()
                if full in seen or any(k in low for k in NOISE_IMG):
                    continue
                seen.add(full)
                images.append(full)

            b.close()
            return {
                "content": content,
                "publish_time": publish_time,
                "source": "BBC",
                "detail_title": title,
                "images": images,
            }
    except Exception as exc:  # noqa: BLE001
        print(f"[详情-JS] {url} 失败: {exc}")
        return {}


# ---------------------------------------------------------------------------
# JSON 原子保护性写入（实时落盘，防进程中断丢数据）
# ---------------------------------------------------------------------------
OUT_FILE = "data/新闻/bbc_collection.json"

def atomic_save(items, path=OUT_FILE):
    """写 .tmp -> flush + fsync -> os.replace 改名，杜绝半截 JSON；逐篇实时调用。"""
    d = os.path.dirname(path)
    if d:
        os.makedirs(d, exist_ok=True)
    tmp = path + ".tmp"
    payload = {"source": BBC_ROOT, "count": len(items), "items": items}
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(payload, f, ensure_ascii=False, indent=2)
        f.flush()
        os.fsync(f.fileno())
    os.replace(tmp, path)


def main():
    parser = argparse.ArgumentParser(description="BBC 双引擎爬虫（学习用途）")
    parser.add_argument(
        "--no-detail",
        action="store_true",
        help="仅抓列表不解析正文（默认会逐篇解析正文）",
    )
    parser.add_argument(
        "--limit",
        type=int,
        default=0,
        help="仅处理前 N 条文章（测试 / 控规模），0 表示全部",
    )
    args = parser.parse_args()

    print("== BBC 双引擎爬虫（学习 / 研究用途）==")

    # 列表：静态优先，逐 section 尝试；全失败再渲染降级
    items = []
    for sec in SECTION_URLS:
        items = fetch_list_static(sec)
        if items:
            break
    if not items:
        print("[列表] 静态无果，降级到 Playwright 渲染 ...")
        for sec in SECTION_URLS:
            items = fetch_list_js(sec)
            if items:
                break

    if not items:
        print("[-] 两个引擎均未获取到数据。可能页面结构已变更或需代理。")
        return

    if args.limit and args.limit > 0:
        items = items[: args.limit]
        print(f"[限流] 仅处理前 {len(items)} 条")

    if not args.no_detail:
        print(f"[详情] 逐篇解析正文（共 {len(items)} 篇，间隔 {REQUEST_INTERVAL}s）...")
        for idx, it in enumerate(items, 1):
            print(f"  ({idx}/{len(items)}) {it['url']}")
            it.update(fetch_article_detail(it["url"]))
            time.sleep(REQUEST_INTERVAL)
            atomic_save(items)  # 每篇解析完即实时落盘，防中断丢数据

    # 收尾保存（保证进度完整）+ 中断 / 异常保护
    try:
        atomic_save(items)
        print(f"[+] 成功收录 {len(items)} 条，输出 {OUT_FILE}")
    except KeyboardInterrupt:
        print(f"\n[!] 被用户中断，已实时保存至 {OUT_FILE}（当前 {len(items)} 条）")
        raise
    except Exception as exc:
        print(f"\n[!] 写入异常: {exc}；已实时保存至 {OUT_FILE}（当前 {len(items)} 条）")
        raise


if __name__ == "__main__":
    main()
