#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
hkcna_crawler.py — 爬取 https://www.hkcna.hk/ (香港中国通讯社 / 中通社, 中国香港)
================================================================================

★ 2026-10-16 站点改版适配（重要）
  站点 2026-09 前后改版，旧 CMS 路径全部下线：
    - https://hkcna.hk/docDetail.jsp?id=xxx      → 302 跳回首页（不可用）
    - https://hkcna.hk/picpath/photo/...（旧图床）→ 302 跳回首页（全部死链）
  新站资源位置（实测 200）：
    - 文章页：https://www.hkcna.hk/hk/content/s<mongoId>.html
    - 图  片：https://static.hkcna.hk/dams-res/editing/image/YYYYMM/DD/<hash>.jpg
  因此本爬虫改用**官方站内搜索 API + 稿件 JSON** 双通道（均为公开接口，无需鉴权），
  不再解析旧 JSP 列表页。

方法
  · 稿件发现（collect_links）：
        POST https://search.hkcna.hk/cass/common/search?page=N&size=100
        body {"channelId":"6a06cd19e4b05731cf1d1b70","keywordLogic":"and",
              "title":"","content":"","fulltext":"","columnId":[],"specialIds":[],
              "type":[],"author":"","editor":""}
        → 按发布时间倒序返回全站最新稿件（实测 totalElements 12w+，size 最大 100，
          page 任意翻页）。每条自带 title/url/jsonUrl/publishTime/columnName/images。
  · 正文获取（parse_article）：
        优先 GET <jsonUrl>（结构化 JSON，含 content 的 HTML 正文，远端压力最小）；
        失败则回退解析文章页 HTML（正文容器 div.global-content.story-content，
        标题取 og:title）。
        正文 HTML 内 <p> 取正文、<figure> 内 <img> + <figcaption> 取图片与图注。

合规：香港中国通讯社内容版权归原社所有；本脚本仅用于个人学习/研究，禁止商用转发。

★ 输出（落盘路径约定，2026-10-16 起按日期归档，与腾讯新闻/海湾新闻/俄罗斯独立报爬虫一致）：
  目录：data/新闻/香港中通社/
  文件名：news_collection_hkcna-YYYYMMDD.json（YYYYMMDD = 运行当天日期）
    - 最新一份永远写回此规范原名；
    - 同名文件已存在时，旧文件自动重命名 news_collection_hkcna-YYYYMMDD-01.json
      （已占用 -01 则 -02、-03 依次递增），旧内容零丢失，禁止静默覆盖。

用法：
  python 香港中通社爬虫.py                 # 默认 4 页 ×100 条，limit=400
  python 香港中通社爬虫.py --limit 50      # 控量
  python 香港中通社爬虫.py --no-detail     # 仅采集清单（不抓正文）
  python 香港中通社爬虫.py --delay 0.3     # 请求间隔（秒）
  python 香港中通社爬虫.py --max-pages 6   # API 翻页数（每页 100 条，默认 4）
"""

import argparse
import json
import os
import re
import time
import sys
from datetime import datetime, timezone, timedelta

import requests
from bs4 import BeautifulSoup

BASE = "https://www.hkcna.hk"
# 站内搜索 API（前端同款，公开无需鉴权）：cassProxy 取自页面 _base 配置
SEARCH_API = "https://search.hkcna.hk/cass/common/search"
CHANNEL_ID = "6a06cd19e4b05731cf1d1b70"
HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                  "(KHTML, like Gecko) Chrome/124.0 Safari/537.36",
    "Accept-Language": "zh-HK,zh;q=0.9,zh-CN;q=0.8,en;q=0.7",
}
API_HEADERS = {
    "Content-Type": "application/json",
    "Accept-Language": "zh-HK",
    "User-Agent": HEADERS["User-Agent"],
    "Origin": BASE,
    "Referer": BASE + "/",
}
HK = timezone(timedelta(hours=8))  # 香港 +08:00
# 输出路径约定（2026-10-16 用户指定「按日期归档」，与腾讯新闻/海湾新闻/俄罗斯独立报一致）：
#   目录    data/新闻/香港中通社/
#   文件名  news_collection_hkcna-YYYYMMDD.json（YYYYMMDD = 运行当天日期）
#   同名已存在时：旧文件重命名 -01/-02/-03 递增备份（见 backup_existing_output），
#   最新一份永远写回此规范原名，禁止静默覆盖。
# 以脚本所在目录为基准，避免依赖当前工作目录
_SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
_OUTPUT_DIR = os.path.join(_SCRIPT_DIR, "data", "新闻", "香港中通社")
OUTPUT = os.path.join(_OUTPUT_DIR, "news_collection_hkcna-{0}.json".format(
    datetime.now().strftime("%Y%m%d")))
PAGE_SIZE = 100                  # 搜索 API 单页最大条数（实测 size=100 可用）
# 旧 CMS 图床已下线，命中即在解析阶段过滤（只保留历史数据里的地址，不浪费请求）
DEAD_IMG_RE = re.compile(r"hkcna\.hk/picpath/", re.I)
IMG_EXT_RE = re.compile(r"\.(jpg|jpeg|png|webp|gif)$", re.I)


# ---------------------------------------------------------------------------
# 工具
# ---------------------------------------------------------------------------
def abs_url(href):
    """相对地址补全为绝对地址（新站文章页为 https://www.hkcna.hk/hk/content/s<id>.html）。"""
    if not href:
        return ""
    if href.startswith("http"):
        return href
    if href.startswith("//"):
        return "https:" + href
    if href.startswith("/"):
        return BASE + href
    return BASE + "/" + href.lstrip("./")


def _get(url, as_json=False, retries=2, timeout=25):
    """
    带重试的 GET（2026-10-16 新增：站点偶发超时/连接重置会直接吃掉一篇稿件，
    旧实现无重试、失败即判「无正文」）。
      - 状态码非 200、连接异常、JSON 解析失败 都算一次失败
      - 退避间隔 0.8s → 1.6s，重试 retries 次
    返回文本 / dict，全部失败返回 None。
    """
    for attempt in range(retries + 1):
        try:
            r = requests.get(url, headers=HEADERS, timeout=timeout)
            if r.status_code == 200:
                return r.json() if as_json else r.text
        except Exception:
            pass
        if attempt < retries:
            time.sleep(0.8 * (attempt + 1))
    return None


def get_text(url):
    """GET 文本（带重试），失败返回 None。"""
    return _get(url, as_json=False)


def get_json(url):
    """GET JSON（带重试），失败或内容不是 JSON 时返回 None。"""
    return _get(url, as_json=True)


def iso_hk(value):
    """
    把站点时间（2026-10-16T14:32:53.415+0000，UTC）统一为
    本仓库约定的 ISO 8601 格式：YYYY-MM-DDTHH:MM:SS+08:00（香港时间）。
    """
    if not value:
        return None
    raw = str(value).strip()
    dt = None
    try:
        dt = datetime.fromisoformat(raw.replace("Z", "+00:00"))
    except Exception:
        m = re.search(r"(\d{4})-(\d{2})-(\d{2})[T ](\d{2}):(\d{2})(?::(\d{2}))?", raw)
        if m:
            try:
                dt = datetime(int(m.group(1)), int(m.group(2)), int(m.group(3)),
                              int(m.group(4)), int(m.group(5)),
                              int(m.group(6) or 0), tzinfo=timezone.utc)
            except Exception:
                return None
        else:
            return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(HK).replace(microsecond=0).isoformat()


# ---------------------------------------------------------------------------
# 稿件清单采集（站内搜索 API 分页）
# ---------------------------------------------------------------------------
def collect_links(max_pages=4):
    """
    通过官方站内搜索 API 按发布时间倒序分页取最新稿件。
    返回 {稿件id: 稿件摘要dict}，摘要含 url / jsonUrl / title / publishTime /
    columnName / images，后续逐篇再取正文。
    """
    items = {}   # id -> 摘要
    payload = {
        "channelId": CHANNEL_ID,
        "keywordLogic": "and",
        "title": "", "content": "", "fulltext": "",
        "columnId": [], "specialIds": [], "type": [],
        "author": "", "editor": "",
    }
    for page in range(max_pages):
        try:
            r = requests.post(
                "{0}?page={1}&size={2}".format(SEARCH_API, page, PAGE_SIZE),
                data=json.dumps(payload, ensure_ascii=False),
                headers=API_HEADERS, timeout=25,
            )
            if r.status_code != 200:
                print("[list] API HTTP {0}，停止翻页".format(r.status_code))
                break
            body = r.json()
            if str(body.get("code")) != "0":
                break
            content = (body.get("data") or {}).get("content") or []
        except Exception as exc:
            print("[list] 第 {0} 页请求异常：{1}".format(page + 1, exc))
            break

        if not content:
            break
        for it in content:
            sid = it.get("id") or it.get("url")
            if sid and sid not in items:
                items[sid] = it
        print("[list] 第 {0}/{1} 页 → 累计 {2} 篇".format(page + 1, max_pages, len(items)))
        if len(content) < PAGE_SIZE:
            break
        time.sleep(0.2)

    print("[list] 唯一稿件：{0} 篇".format(len(items)))
    return items


# ---------------------------------------------------------------------------
# 正文 / 时间 / 图片 解析
# ---------------------------------------------------------------------------
UI_NOISE_RE = [
    re.compile(r"^\s*分享到[:：]?"),
    re.compile(r"^\s*稿件來源[:：]?"),
    re.compile(r"^\s*【\s*字號\s*[:：].*?】\s*$"),
    re.compile(r"^\s*\d{4}-\d{2}-\d{2}\s+\d{2}:\d{2}\s*"),
    re.compile(r"^\s*(大\s*中\s*小|字號)\s*$"),
]


def clean_paragraphs(paras):
    """剔除正文里的站点 UI 噪声行（分享/字號/來源/时间戳/原标题行），保留真实内容。"""
    out, seen = [], set()
    for p in paras:
        t = (p or "").strip()
        if not t:
            continue
        if any(rx.match(t) for rx in UI_NOISE_RE):
            continue
        if t in seen:               # 图集图注常重复，去重
            continue
        seen.add(t)
        out.append(t)
    return out


def parse_content_html(content_html):
    """
    把稿件正文 HTML 拆成 (正文纯文本, 图片列表)。
    正文：所有 <p> 文本按段落拼接（沿用仓库统一约定）。
    图片：<figure> 内的 <img> 取 src，<figcaption> 取图注；
          若正文内无图，则回退取正文容器内的独立 <img>（同样过滤 CDN 域）。
    """
    if not content_html:
        return "", []
    soup = BeautifulSoup(content_html, "html.parser")
    for bad in soup.select("script, style"):
        bad.decompose()

    paras = [p.get_text(strip=True) for p in soup.find_all("p")]
    content = "\n\n".join(p for p in paras if p)

    images, seen = [], set()
    figs = soup.find_all("figure")
    for fig in figs:
        img = fig.find("img")
        src = abs_url(img.get("src") or img.get("data-src") or "") if img else ""
        if not _is_news_image(src) or src in seen:
            continue
        cap_tag = fig.find("figcaption")
        seen.add(src)
        images.append({"url": src, "caption": (cap_tag.get_text(strip=True) if cap_tag else "")})
    if not images:
        for img in soup.find_all("img"):
            src = abs_url(img.get("src") or img.get("data-src") or "")
            if _is_news_image(src) and src not in seen:
                seen.add(src)
                images.append({"url": src, "caption": ""})
    return content, images


def _is_news_image(url):
    """只保留新 CDN 上的真实新闻图，排除 UI 图标/占位图。"""
    if not url or not IMG_EXT_RE.search(url):
        return False
    if DEAD_IMG_RE.search(url):        # 旧 CMS 图床（已下线）
        return False
    if "static.hkcna.hk" not in url:   # 其它外链（社交图标等）
        return False
    if re.search(r"(icon|logo|tw-bg|banner|pixel)", url, re.I):
        return False
    return True


def images_from_api(item):
    """搜索 API 记录里直接带的新 CDN 图片（图注字段为 ftCaption/comment）。"""
    out, seen = [], set()
    for im in (item.get("images") or []):
        url = abs_url(im.get("url") or im.get("previewUrl") or "")
        if not _is_news_image(url) or url in seen:
            continue
        seen.add(url)
        out.append({"url": url, "caption": (im.get("ftCaption") or im.get("comment") or "").strip()})
    return out


def parse_article_html(url):
    """
    兜底：直接解析文章页 HTML（正文容器 div.global-content.story-content）。
    返回 (标题, 正文, 图片列表, 发布时间原文)。
    """
    html = get_text(url)
    if not html:
        return None, "", [], None
    soup = BeautifulSoup(html, "html.parser")
    tag = soup.find("meta", property="og:title")
    title = (tag.get("content") or "").strip() if tag else ""
    if not title and soup.title:
        title = soup.title.get_text(strip=True)
    body = soup.select_one("div.global-content.story-content") or soup
    content, images = parse_content_html(str(body))
    if not content:
        # 图集(GALLERY)类：正文区没有 <p>，说明文字写在图注 .desc 里
        descs, seen = [], set()
        for d in body.select(".desc"):
            t = d.get_text(strip=True)
            if t and t not in seen:
                seen.add(t)
                descs.append(t)
        content = "\n\n".join(descs)
    m = re.search(r'"publishTime"\s*:\s*"([^"]+)"', html)
    return title or None, content, images, (m.group(1) if m else None)


def parse_article(item):
    """
    解析单篇稿件：优先走稿件 JSON 接口（远端压力最小、数据最完整），
    失败则回退解析文章页 HTML。返回统一 schema 的字典或 None。
    """
    url = item.get("url") or ""
    if not url:
        return None

    title = (item.get("title") or "").strip()
    published_at = iso_hk(item.get("publishTime"))
    content, images = "", []

    # 通道①：稿件 JSON（远端压力最小、数据最完整）
    data = get_json(item.get("jsonUrl")) if item.get("jsonUrl") else None
    if data:
        title = title or (data.get("title") or "").strip()
        content, images = parse_content_html(data.get("content") or "")
        if not published_at:
            published_at = iso_hk(data.get("updated"))

    # 通道②：JSON 通道没给出正文就回退 HTML 页面（2026-10-16 修复的关键 bug：
    # 部分稿件——如「香港新聞網」来源稿——jsonUrl 返回 200 但 content 为空，
    # 旧逻辑只要 JSON 请求成功就不再回退，导致有正文的稿件被误判为「无正文」）
    json_len = len(content.strip())
    if not content.strip():
        h_title, h_content, h_images, h_time = parse_article_html(url)
        h_len = len((h_content or "").strip())
        if h_content.strip():
            content = h_content
        if h_images:
            images = h_images
        title = title or h_title
        if not published_at:
            published_at = iso_hk(h_time)
        print("        · 诊断：JSON通道={0}字 → HTML兜底={1}字".format(json_len, h_len))

    if not images:                      # 正文无图时用 API 记录的封面/配图兜底
        images = images_from_api(item)

    # 图集类（GALLERY）：正文区没有段落，说明文字即图注 —— 用去重后的图注作为正文，
    # 不丢弃该稿件（沿用仓库对图集类「有图则保留」的约定，越南人民报同类处理）
    is_gallery = False
    if not content.strip():
        caps, seen_cap = [], set()
        for im in images:
            c = (im.get("caption") or "").strip()
            if c and c not in seen_cap:
                seen_cap.add(c)
                caps.append(c)
        if caps:
            content = "\n\n".join(caps)
            is_gallery = True
    if not title or not content.strip():
        return None                     # 真的什么都没有（视频/不可达）

    # 清洗站点 UI 噪声行（分享/字號/來源提示等，图集正文常混入）
    content = "\n\n".join(clean_paragraphs(content.split("\n\n")))

    m = re.search(r"/hk/content/s([a-z0-9]+)\.html", url)
    return {
        "id": m.group(1) if m else (item.get("id") or url),
        "url": url,
        "title": title,
        "section": (item.get("columnName") or "").strip(),
        "author": "香港中通社",
        "published_at": published_at,
        "summary": (item.get("summary") or "").strip(),
        "content": content,
        "images": images,
        "language": "zh",
        "type": "gallery" if is_gallery else "article",
    }


# ---------------------------------------------------------------------------
# 原子写入
# ---------------------------------------------------------------------------
def atomic_save(articles, path=OUTPUT):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(articles, f, ensure_ascii=False, indent=2)
        f.flush()
        os.fsync(f.fileno())
    os.replace(tmp, path)


def backup_existing_output(path):
    """写之前若目标已存在，将其重命名为 -01/-02/-03 递增编号保留历史，禁止覆盖本地文件。

    命名示例（同一天多轮运行）：
      news_collection_hkcna-20261016.json       ← 最新一份（规范原名）
      news_collection_hkcna-20261016-01.json    ← 上一轮
      news_collection_hkcna-20261016-02.json    ← 上上轮
    """
    if not os.path.exists(path):
        return
    base, ext = os.path.splitext(path)
    n = 1
    while True:
        cand = "{0}-{1:02d}{2}".format(base, n, ext)
        if not os.path.exists(cand):
            break
        n += 1
    os.replace(path, cand)  # 原子移动旧文件，绝不覆盖
    print("[backup] 已存在 → 重命名为 {0}（旧文件保留，不覆盖）".format(os.path.basename(cand)))


# ---------------------------------------------------------------------------
# 主流程
# ---------------------------------------------------------------------------
def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--limit", type=int, default=400)
    ap.add_argument("--no-detail", action="store_true", help="仅采集清单，不抓正文")
    ap.add_argument("--delay", type=float, default=0.3)
    ap.add_argument("--max-pages", type=int, default=4, help="API 翻页数（每页 100 条）")
    args = ap.parse_args()

    articles = []
    try:
        print("[list] 采集稿件清单（站内搜索 API 分页）...")
        links = collect_links(max_pages=args.max_pages)

        items = list(links.items())
        if args.limit:
            items = items[: args.limit]

        # 只在每轮开写前调用一次；循环内的每篇实时落盘（atomic_save）不会重复触发备份
        backup_existing_output(OUTPUT)

        for i, (aid, meta) in enumerate(items, 1):
            url = meta.get("url") if isinstance(meta, dict) else meta
            print("[{0}/{1}] 解析：{2}".format(i, len(items), url))
            if args.no_detail:
                articles.append({"id": aid, "url": url, "title": (meta or {}).get("title")})
                atomic_save(articles, OUTPUT)
                continue
            art = parse_article(meta)
            if art and art.get("content"):
                articles.append(art)
                print("        ✓ 标题={0} 正文={1}字 图={2}张 栏目={3} 时间={4}".format(
                    (art["title"][:34] if art["title"] else None),
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
        # 一条都没采到就中断时不要写空数组覆盖已有成果（历史文件零丢失）
        if articles:
            atomic_save(articles, OUTPUT)
            print("\n[interrupted] 已实时保存至当前进度（{0} 篇）→ {1}".format(len(articles), OUTPUT))
        else:
            print("\n[interrupted] 尚未采集到任何条目，未写入文件（原文件保持不变）→ {0}".format(OUTPUT))
        sys.exit(130)
    except Exception as exc:
        if articles:
            atomic_save(articles, OUTPUT)
            print("\n[error] 抓取异常，已保存当前进度（{0} 篇）→ {1}".format(len(articles), OUTPUT))
        else:
            print("\n[error] 抓取异常且无进度可存，未写入文件 → {0}".format(OUTPUT))
        raise


if __name__ == "__main__":
    main()
