#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
ng.ru (Независимая газета) 爬虫 — 双引擎架构
================================================
引擎 1 (列表/RSS, 主):  https://www.ng.ru/rss/
    - NG 的 RSS 仅含摘要(description ~289字) + 完整元数据(title/link/pubDate+0300)
    - 时间用 RSS pubDate 解析为 ISO(+03:00 莫斯科时间)，比 HTML 的 ".date"(无时分)更精确
引擎 2 (详情页, 降级兜底): 抓每篇文章 HTML 取正文全文
    - NG 疑似前置 DDoS-Guard / 反爬: requests 被挂死; 已全面改用 Playwright 真实 Chromium 抓取
      (默认有头浏览器, 可过反爬); 仅 CI 无显示环境用 --headless 切回无头(可能被识别挂起)
    - 正文容器有两种模板:
        * 栏目页 /economics/2026-.../xxx.html  -> <article> 标签
        * /news/<id>.html                      -> .content.newsone
    - 图片在正文内 <img>(含 <p class="image_detail"> 图注块)；og:image 是站标，忽略
    - 过滤 <p class="image_detail"> (图注不是正文) 但保留其图片

合规: 内容版权归 Независимая газета，仅限个人学习/研究，禁止商用转发。
请求间隔 3s，尊重站点。

用法:
    python ng_crawler.py                                  # 全量(默认有头浏览器, 本地可直接爬)
    python ng_crawler.py --root https://www.ng.ru/rss/     # 等价于默认
    python ng_crawler.py --limit 5                         # 调试前 N 条
    python ng_crawler.py --no-detail                       # 仅列表级(用 RSS 摘要当内容)
    python ng_crawler.py --root https://www.ng.ru/politics # 指定频道(需该频道有 /rss 或列表页)
"""

import argparse
import json
import os
import re
import sys
import time
from email.utils import parsedate_to_datetime
from urllib.parse import urljoin, urlparse

import requests
from bs4 import BeautifulSoup

# ---------- 常量 ----------
SITE = "https://www.ng.ru"
DEFAULT_FEED = "https://www.ng.ru/rss/"
SOURCE_NAME = "Независимая газета"
UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36")
HEADERS = {
    "User-Agent": UA,
    "Accept-Language": "ru-RU,ru;q=0.9,en;q=0.8",
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
}
REQUEST_TIMEOUT = 20
SLEEP_SEC = 3  # 礼貌间隔
PW_HEADED = True  # 默认有头浏览器绕过 ng.ru 无头反爬; 仅 CI 无显示环境用 --headless 切回无头

# 文章 URL 正则: /news/<id>.html 或 /<section>/<YYYY-MM-DD>/<slug>.html
ARTICLE_RE = re.compile(r"^https?://(?:www\.)?ng\.ru/(?:[a-z]+/)?(?:\d{4}-\d{2}-\d{2}/)?[\w\-]+\.html$")


# ---------- 工具函数 ----------
def log(msg):
    print(msg, flush=True)


def fetch(url, timeout=REQUEST_TIMEOUT):
    return requests.get(url, headers=HEADERS, timeout=timeout)


def fetch_text_playwright(url, timeout=REQUEST_TIMEOUT):
    """用真实 Chromium 抓取页面文本，绕过 ng.ru 对 requests 的反爬挂死。

    ng.ru 疑似前置 DDoS-Guard / 反爬：对 Python requests（TLS 指纹/简陋 Header）
    会故意挂死连接（20s 超时不响应）；改用 Playwright 真实 Chromium 后，无头模式
    仍可能被指纹识别挂起——默认以有头浏览器运行（与人工浏览器一致，可过反爬）；
    CI 无显示环境加 --headless 切回无头，若也被挂起则需代理 / CDP 复用真实浏览器。
    返回解码后的页面文本；任何失败返回 None（交由上层回退）。
    """
    try:
        from playwright.sync_api import sync_playwright
    except ImportError:
        log(f"[列表] 未安装 playwright，无法启用浏览器引擎: {url}")
        return None
    try:
        with sync_playwright() as p:
            browser = p.chromium.launch(
                headless=not PW_HEADED,
                args=["--no-sandbox", "--disable-blink-features=AutomationControlled",
                      "--disable-infobars"],
            )
            ctx = browser.new_context(
                user_agent=UA,
                locale="ru-RU",
                extra_http_headers={"Accept-Language": "ru-RU,ru;q=0.9,en;q=0.8"},
            )
            page = ctx.new_page()
            resp = page.goto(url, wait_until="domcontentloaded", timeout=timeout * 1000)
            text = page.content()
            browser.close()
            if resp is None or resp.status >= 400:
                log(f"[列表] Playwright 状态异常: {url} status={getattr(resp, 'status', None)}")
                return None
            return text
    except Exception as e:
        log(f"[列表] Playwright 请求失败: {url}\n  -> {e}")
        return None


class BrowserSession:
    """复用单个 Playwright Chromium 实例抓取多页，绕过 ng.ru 对 requests 的反爬挂死。

    列表(RSS)与全部详情页共用同一个浏览器上下文，避免每篇重开浏览器(100 篇会极慢)。
    无 playwright 时 available()=False，上层回退 requests。
    """
    def __init__(self, headed=False, timeout=REQUEST_TIMEOUT):
        self.headed = headed
        self.timeout = timeout
        self._p = None
        self._browser = None
        self._ctx = None
        self._ok = None

    def available(self):
        if self._ok is None:
            try:
                from playwright.sync_api import sync_playwright
                self._ok = True
            except ImportError:
                self._ok = False
                log("[引擎] 未安装 playwright，详情页将回退 requests(可能被反爬挂死)")
        return self._ok

    def __enter__(self):
        if not self.available():
            return self
        from playwright.sync_api import sync_playwright
        self._p = sync_playwright().start()
        self._browser = self._p.chromium.launch(
            headless=not self.headed,
            args=["--no-sandbox", "--disable-blink-features=AutomationControlled",
                  "--disable-infobars"],
        )
        self._ctx = self._browser.new_context(
            user_agent=UA,
            locale="ru-RU",
            extra_http_headers={"Accept-Language": "ru-RU,ru;q=0.9,en;q=0.8"},
        )
        return self

    def fetch_text(self, url):
        """用当前浏览器上下文抓一页源码；失败返回 None。"""
        if self._ctx is None:
            return None
        try:
            page = self._ctx.new_page()
            resp = page.goto(url, wait_until="domcontentloaded", timeout=self.timeout * 1000)
            text = page.content()
            page.close()
            if resp is None or resp.status >= 400:
                log(f"[引擎] 浏览器状态异常: {url} status={getattr(resp, 'status', None)}")
                return None
            return text
        except Exception as e:
            log(f"[引擎] 浏览器请求失败: {url}\n  -> {e}")
            return None

    def __exit__(self, *exc):
        try:
            if self._browser:
                self._browser.close()
        except Exception:
            pass
        try:
            if self._p:
                self._p.stop()
        except Exception:
            pass


def is_article_url(u):
    return bool(ARTICLE_RE.match(u)) and "ng.ru" in u


# ---------- 引擎 1: RSS 列表 ----------
def parse_list_rss(feed_url, bs=None):
    """解析 RSS 列表, 返回 [{title,url,summary,publish_time}]
    注意: ng.ru 对 requests 反爬挂死(20s 超时不响应)且可能挂起无头 Chromium；
    优先用 Playwright 真实浏览器抓取 RSS（默认有头更稳），无浏览器引擎时回退单发抓取。
    """
    text = bs.fetch_text(feed_url) if (bs is not None and bs.available()) else fetch_text_playwright(feed_url)
    if not text or "<item" not in text:
        log(f"[列表] RSS(Playwright) 不可用; 回退到首页列表抓取")
        return parse_list_html(feed_url)
    soup = BeautifulSoup(text, "xml")
    items = []
    for it in soup.find_all("item"):
        title = it.title.get_text(strip=True) if it.title else ""
        link = it.link.get_text(strip=True) if it.link else ""
        desc = it.description.get_text(strip=True) if it.description else ""
        pub = ""
        if it.pubDate:
            try:
                dt = parsedate_to_datetime(it.pubDate.get_text())
                pub = dt.isoformat()  # 已含 +03:00
            except Exception:
                pub = it.pubDate.get_text(strip=True)
        if link and is_article_url(link):
            items.append({
                "title": title,
                "url": link,
                "summary": desc,
                "publish_time": pub,
            })
    log(f"[列表] RSS 命中 {len(items)} 条")
    return items


def parse_list_html(page_url):
    """RSS 不可用时的兜底: 从栏目/首页 HTML 抽取文章链接"""
    try:
        r = fetch(page_url)
    except Exception as e:
        log(f"[列表] 页面请求失败: {page_url}\n  -> {e}")
        return []
    soup = BeautifulSoup(r.text, "lxml")
    seen, items = set(), []
    for a in soup.find_all("a", href=True):
        full = urljoin(page_url, a["href"].strip())
        if is_article_url(full) and full not in seen:
            seen.add(full)
            items.append({
                "title": a.get_text(strip=True),
                "url": full,
                "summary": "",
                "publish_time": "",
            })
    log(f"[列表] HTML 命中 {len(items)} 条")
    return items


# ---------- 引擎 2: 详情页 ----------
def find_body(soup):
    """返回正文容器(兼容两种模板), 否则 None"""
    art = soup.find("article")
    if art and len(art.get_text(strip=True)) > 200:
        return art
    for sel in [".content.newsone", ".newsone", ".news_detail_content", ".content"]:
        el = soup.select_one(sel)
        if el and len(el.get_text(strip=True)) > 200:
            return el
    return None


def parse_detail(url, bs=None):
    """抓取详情页, 返回 {detail_title, content, images, publish_time}
    优先用 Playwright 真实浏览器(绕过 ng.ru 反爬); 浏览器已启用但本页失败则不回退 requests
    (ng.ru 对 requests 一律挂死, 回退纯属浪费 20s)。仅当完全无浏览器引擎时才回退 requests。
    """
    html = None
    use_browser = bs is not None and bs.available()
    if use_browser:
        html = bs.fetch_text(url)
    if not html:
        if use_browser:
            log(f"  [详情] 浏览器抓取失败 {url}")
            return {"detail_title": "", "content": "", "images": [], "publish_time": ""}
        try:
            r = fetch(url)
            html = r.text
        except Exception as e:
            log(f"  [详情] 请求失败 {url}: {e}")
            return {"detail_title": "", "content": "", "images": [], "publish_time": ""}

    soup = BeautifulSoup(html, "lxml")
    h1 = soup.find("h1")
    title = h1.get_text(strip=True) if h1 else ""

    body = find_body(soup)
    paras, imgs = [], []
    had_noise = False

    if body:
        # 剔除评论区/登录表单等 DOM 块, 避免正文混入噪声
        for bad in body.select("div.comments, div.comment, #comments, .comment-form, form.comment, .auth-block"):
            bad.decompose()
        # 图片: 正文内所有 <img>(含 <p class=image_detail> 图块)
        for i in body.find_all("img"):
            src = i.get("src") or i.get("data-src")
            if not src or src.startswith("data:"):
                continue
            abs_src = urljoin(url, src)
            if abs_src not in imgs:
                imgs.append(abs_src)
        # 正文: 跳过图注块 <p class=image_detail>, 其余 <p> 取文本
        for p in body.find_all("p"):
            if "image_detail" in " ".join(p.get("class") or []):
                continue
            t = p.get_text(" ", strip=True)
            if t:
                paras.append(t)

    # 文本级噪声清理: 评论提示/登录注册/相关阅读/广告 等行
    NOISE_RE = re.compile(
        r"Оставлять комментарии могут только авторизованные пользователи"
        r"|Вам необходимо Войти или Зарегистрироваться"
        r"|Комментировать|Читайте также|Подпишитесь на|Реклама",
        re.IGNORECASE)
    if NOISE_RE.search("\n".join(paras)):
        had_noise = True
    paras = [t for t in paras if not NOISE_RE.search(t)]
    content = "\n\n".join(paras)

    # 清理: 去掉正文开头的冗余电头 "HH:MM DD.MM.YYYY"(时间已在 publish_time)
    content = re.sub(r"^\s*\d{1,2}:\d{2}\s+\d{2}\.\d{2}\.\d{4}\b\s*", "", content)

    # 兜底: 无正文时用 meta description
    if not content:
        m = soup.find("meta", attrs={"name": "description"}) or soup.find(
            "meta", property="og:description")
        if m and m.get("content"):
            content = m.get("content").strip()

    # 时间兜底: 若列表未给, 从 HTML .date 解析 ("Вторник 28.07.2026")
    pub = ""
    d = soup.select_one(".date")
    if d:
        m = re.search(r"(\d{2})\.(\d{2})\.(\d{4})", d.get_text())
        if m:
            pub = f"{m.group(3)}-{m.group(2)}-{m.group(1)}"

    return {
        "detail_title": title,
        "content": content,
        "images": imgs,
        "publish_time": pub,
        "had_noise": had_noise,
    }


# ---------- 主流程 ----------
def atomic_save(data, path="data/新闻/ng_collection.json"):
    """JSON 原子保护性写入：先写 .tmp → flush+fsync → os.replace 改名，杜绝半截 JSON。

    每抓完一篇即调用，进程被中断（Ctrl+C / 断电）也只丢当前这一篇，已落盘数据不丢。
    """
    d = os.path.dirname(path)
    if d:
        os.makedirs(d, exist_ok=True)
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)
        f.flush()
        os.fsync(f.fileno())
    os.replace(tmp, path)


def run(args, bs):
    t0 = time.time()
    root_is_rss = args.root.rstrip("/").endswith("/rss") or args.root.endswith(".xml")
    list_items = (parse_list_rss(args.root, bs) if root_is_rss
                  else parse_list_html(args.root))

    if not list_items:
        log("未获取到任何文章链接, 退出。")
        sys.exit(1)

    if args.limit:
        list_items = list_items[:args.limit]
    log(f"待处理 {len(list_items)} 篇 (no_detail={args.no_detail})")

    collected = []
    empty_detail = 0
    noise_articles = 0
    try:
        for idx, it in enumerate(list_items, 1):
            url = it["url"]
            if args.no_detail:
                detail = {
                    "detail_title": it["title"],
                    "content": it["summary"],
                    "images": [],
                    "publish_time": it["publish_time"],
                }
                content_src = "rss_summary"
            else:
                detail = parse_detail(url, bs)
                content_src = "fulltext" if detail["content"] else "empty"
                if not detail["content"]:
                    empty_detail += 1
                if detail.get("had_noise"):
                    noise_articles += 1
                if idx < len(list_items):
                    time.sleep(SLEEP_SEC)

            # 合并: 列表级字段 + 详情级字段
            # 时间优先级: RSS(列表) 的精确时间 > HTML .date 兜底(仅日期)
            record = {
                "title": it["title"],
                "url": url,
                "publish_time": it["publish_time"] or detail["publish_time"],
                "source": SOURCE_NAME,
                "section": urlparse(url).path.split("/")[1] if len(urlparse(url).path.split("/")) > 1 else "",
                "summary": it["summary"],
                "detail_title": detail["detail_title"] or it["title"],
                "content": detail["content"],
                "content_source": content_src,
                "content_had_noise": detail.get("had_noise", False),
                "images": detail["images"],
            }
            collected.append(record)
            flag = f"[{content_src}]" if not args.no_detail else "[rss]"
            log(f"  ({idx}/{len(list_items)}) {flag} {len(detail['content'])}字 "
                f"{len(detail['images'])}图 | {detail['detail_title'][:46] or it['title'][:46]}")
            # 每篇实时原子落盘（与收尾同结构包裹）
            atomic_save({
                "source": SOURCE_NAME,
                "site": SITE,
                "feed": args.root,
                "crawled_at": time.strftime("%Y-%m-%dT%H:%M:%S+08:00"),
                "count": len(collected),
                "detail_html_accessible": True,  # 浏览器抓取绕过反爬
                "items": collected,
            }, args.out)
    except KeyboardInterrupt:
        log(f"\n[interrupted] 采集中断：已实时保存至当前进度（{len(collected)} 篇）-> {args.out}")
        sys.exit(130)

    out = {
        "source": SOURCE_NAME,
        "site": SITE,
        "feed": args.root,
        "crawled_at": time.strftime("%Y-%m-%dT%H:%M:%S+08:00"),
        "count": len(collected),
        "empty_detail": empty_detail,
        "noise_articles": noise_articles,
        "detail_html_accessible": True,  # 浏览器抓取绕过反爬
        "items": collected,
    }
    atomic_save(out, args.out)  # 收尾原子保存

    dt = time.time() - t0
    rate = (100 * (len(collected) - empty_detail) / len(collected)) if collected else 0
    log("=" * 60)
    log(f"完成: {len(collected)} 篇 -> {args.out}")
    log(f"耗时 {dt:.1f}s | 空正文 {empty_detail} 篇 | 噪声污染(已清理) {noise_articles} 篇 | 内容有效率 {rate:.0f}%")


def main():
    ap = argparse.ArgumentParser(description="ng.ru crawler")
    ap.add_argument("--root", default=DEFAULT_FEED,
                    help="RSS/列表页 URL (默认 https://www.ng.ru/rss/)")
    ap.add_argument("--headless", action="store_true",
                    help="CI 无显示环境用无头浏览器(默认有头, 本地直爬)")
    ap.add_argument("--limit", type=int, default=0, help="仅抓取前 N 条(调试)")
    ap.add_argument("--no-detail", action="store_true",
                    help="不抓详情页, 直接用 RSS 摘要当内容")
    ap.add_argument("--out", default="data/新闻/ng_collection.json")
    args = ap.parse_args()
    global PW_HEADED
    PW_HEADED = not args.headless
    with BrowserSession(headed=not args.headless) as bs:
        run(args, bs)


if __name__ == "__main__":
    main()
