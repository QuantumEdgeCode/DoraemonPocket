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
  · 图片本地化（download_photo，2026-10-16 新增，避免日后链接失效）：
        下载到 data/新闻/香港中通社/photo/，文件名沿用 CDN 的 hash 名（唯一），
        已存在则跳过（增量不重复下载），下载失败不中断、local_path 留空。
        JSON 中每图记录 {"url": 远端地址, "caption": 图注,
                        "local_path": "./data/新闻/香港中通社/photo/xxx.jpg"}
        **路径索引统一使用正斜杠 / 与 ./ 前缀**（不受 Windows 反斜杠影响）。

合规：香港中国通讯社内容版权归原社所有；本脚本仅用于个人学习/研究，禁止商用转发。

★ 输出（落盘路径约定，2026-10-16 起按日期归档，与腾讯新闻/海湾新闻/俄罗斯独立报爬虫一致）：
  目录：data/新闻/香港中通社/
  图片：data/新闻/香港中通社/photo/
  文件名：news_collection_hkcna-YYYYMMDD.json（YYYYMMDD = 运行当天日期）
    - 最新一份永远写回此规范原名；
    - 同名文件已存在时，旧文件自动重命名 news_collection_hkcna-YYYYMMDD-01.json
      （已占用 -01 则 -02、-03 依次递增），旧内容零丢失，禁止静默覆盖。

用法：
  python 香港中通社爬虫.py                 # 默认 4 页 ×100 条，limit=400
  python 香港中通社爬虫.py --limit 50      # 控量
  python 香港中通社爬虫.py --no-detail     # 仅采集清单（不抓正文/图片）
  python 香港中通社爬虫.py --no-images     # 抓正文但不下载图片
    python 香港中通社爬虫.py --delay 0.3     # 请求间隔（秒）
  python 香港中通社爬虫.py --max-pages 6   # API 翻页数（每页 100 条，默认 4）
  python 香港中通社爬虫.py --no-log        # 不写日志文件

★ 运行日志（2026-10-16 新增）：
  控制台输出**同时**实时写入 data/新闻/log/香港中通社爬虫YYYYMMDD.log（控制台照常显示）：
    - 每次写入立即 flush，Ctrl+C 中断也不丢日志；
    - **禁止覆盖**：同名文件以追加方式续写；单文件写满 10 MB 自动切换
      香港中通社爬虫YYYYMMDD-01.log、-02.log ……（重启后沿用同一规则）。
"""

import argparse
import hashlib
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
# 图片本地目录（2026-10-16 新增：爬取时同步下载，避免日后链接失效）
PHOTO_DIR = os.path.join(_OUTPUT_DIR, "photo")
OUTPUT = os.path.join(_OUTPUT_DIR, "news_collection_hkcna-{0}.json".format(
    datetime.now().strftime("%Y%m%d")))
PAGE_SIZE = 100                  # 搜索 API 单页最大条数（实测 size=100 可用）
# 日志（2026-10-16 用户要求：控制台输出实时落盘，同时保留控制台显示）
#   目录    data/新闻/log/
#   文件名  香港中通社爬虫-YYYYMMDD.log（YYYYMMDD = 运行当天日期）
#   禁止覆盖：已存在且未满 10MB 则追加；已满则自动切到 -01/-02/-03 …
#   单文件写满 LOG_MAX_BYTES 自动切割到下一个编号，避免文件过大
_LOG_DIR = os.path.join(_SCRIPT_DIR, "data", "新闻", "log")
LOG_MAX_BYTES = 10 * 1024 * 1024
_LOG_STEM = "香港中通社爬虫{0}".format(datetime.now().strftime("%Y%m%d"))
# 旧 CMS 图床已下线，命中即跳过下载（只保留历史数据里的地址，不浪费请求）
DEAD_IMG_RE = re.compile(r"hkcna\.hk/picpath/", re.I)
IMG_EXT_RE = re.compile(r"\.(jpg|jpeg|png|webp|gif)$", re.I)
TIME_RE = re.compile(r"(\d{4})[-/年.](\d{1,2})[-/月.](\d{1,2})\D*(\d{1,2}):(\d{2})")


# ---------------------------------------------------------------------------
# 控制台输出实时落盘（tee：保留控制台显示，同时写入日志）
# ---------------------------------------------------------------------------
class _Tee(object):
    """
    把 print / traceback 的输出**同时**写到控制台和日志文件：
      · 保留控制台：原样转发给原 sys.stdout
      · 实时落盘：每次 write 后立刻 flush，进程被 Ctrl+C 也不丢内容
      · 禁止覆盖：已存在的同名日志用追加模式；已写满的自动跳到下一个编号
      · 自动切割：当前文件超过 max_bytes 时切到 -01/-02/-03 …（重启后同样规则续写）
    """

    def __init__(self, stream, stem, log_dir=_LOG_DIR, max_bytes=LOG_MAX_BYTES):
        self.stream = stream          # 原控制台流
        self.stem = stem
        self.log_dir = log_dir
        self.max_bytes = max_bytes
        self.path = ""
        self._fh = None
        self._open_next()

    # ---- 文件句柄管理 ----
    def _candidates(self):
        """候选文件名生成器：原名 → -01 → -02 …"""
        yield os.path.join(self.log_dir, self.stem + ".log")
        n = 1
        while True:
            yield os.path.join(self.log_dir, "{0}-{1:02d}.log".format(self.stem, n))
            n += 1

    def _open_next(self):
        """打开下一个「不存在 或 未满」的日志文件（追加模式，绝不覆盖已有内容）。"""
        os.makedirs(self.log_dir, exist_ok=True)
        for path in self._candidates():
            if os.path.exists(path) and os.path.getsize(path) >= self.max_bytes:
                continue                      # 已写满 → 看下一个编号
            self._fh = open(path, "a", encoding="utf-8")
            self.path = path
            return
        raise RuntimeError("无法创建日志文件")

    def _rotate(self):
        """当前文件写满后切换到新文件，并在新文件里留一行说明。"""
        old = self.path
        self._fh.close()
        self._open_next()
        self._fh.write("\n[log] 上一份日志 {0} 已达 {1} MB，自动切换到本文件\n".format(
            os.path.basename(old), self.max_bytes // (1024 * 1024)))
        self._fh.flush()

    # ---- 流接口 ----
    def write(self, data):
        # 1) 控制台（编码异常不影响日志落盘）
        try:
            self.stream.write(data)
        except Exception:
            pass
        # 2) 日志文件
        try:
            if self._fh is None:
                self._open_next()
            self._fh.write(data)
            self._fh.flush()                  # 实时落盘
            if self.max_bytes and self._fh.tell() >= self.max_bytes:
                self._rotate()
        except Exception:
            pass
        return len(data)

    def flush(self):
        try:
            self.stream.flush()
        except Exception:
            pass
        try:
            if self._fh:
                self._fh.flush()
        except Exception:
            pass

    def isatty(self):
        try:
            return self.stream.isatty()
        except Exception:
            return False

    def close(self):
        try:
            if self._fh:
                self._fh.close()
        except Exception:
            pass
        self._fh = None


_TEE_STATE = {"tee": None, "out": None, "err": None}


def setup_logging(enabled=True):
    """安装 tee（sys.stdout / sys.stderr 共用同一个实例，保证写入顺序一致）。"""
    if not enabled:
        return None
    # Windows 控制台默认 GBK，中文标题可能编码失败，先切成 utf-8
    for s in (sys.stdout, sys.stderr):
        try:
            s.reconfigure(encoding="utf-8", errors="replace")
        except Exception:
            pass
    _TEE_STATE["out"], _TEE_STATE["err"] = sys.stdout, sys.stderr
    tee = _Tee(sys.stdout, _LOG_STEM)
    _TEE_STATE["tee"] = tee
    sys.stdout = tee
    sys.stderr = tee
    print("=" * 66)
    print("[log] 启动时间：{0}".format(datetime.now().strftime("%Y-%m-%d %H:%M:%S")))
    print("[log] 命令行  ：{0} {1}".format(
        os.path.basename(sys.argv[0]) or "香港中通社爬虫.py",
        " ".join(sys.argv[1:]) or "(默认参数)"))
    print("[log] 输出文件：{0}".format(OUTPUT))
    print("[log] 图片目录：{0}".format(to_posix_rel(PHOTO_DIR)))
    print("[log] 日志上限：单文件 {0} MB 后自动切割".format(LOG_MAX_BYTES // (1024 * 1024)))
    print("=" * 66)
    return tee.path


def restore_logging():
    """恢复原始 stdout/stderr 并关闭日志文件。"""
    tee = _TEE_STATE.get("tee")
    if not tee:
        return
    try:
        print("[log] 结束时间：{0}".format(datetime.now().strftime("%Y-%m-%d %H:%M:%S")))
    except Exception:
        pass
    if _TEE_STATE.get("out") is not None:
        sys.stdout = _TEE_STATE["out"]
    if _TEE_STATE.get("err") is not None:
        sys.stderr = _TEE_STATE["err"]
    tee.close()
    _TEE_STATE.update({"tee": None, "out": None, "err": None})


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
# 图片本地化（2026-10-16 新增：爬取时同步下载，避免日后链接失效）
# ---------------------------------------------------------------------------
def to_posix_rel(abs_path):
    """
    把绝对路径转成以脚本目录为基准的 POSIX 相对路径，形如：
        ./data/新闻/香港中通社/photo/6abf7560e4b048b73f3df3b4.jpg
    统一使用正斜杠 / （Windows 的 \\ 全部替换掉），确保跨系统一致。
    """
    rel = os.path.relpath(abs_path, _SCRIPT_DIR).replace(os.sep, "/")
    if not rel.startswith("./"):
        rel = "./" + rel
    return rel


def photo_filename(url):
    """由图片 URL 得到安全的本地文件名（沿用 CDN 的 hash 原名，保证唯一）。"""
    name = os.path.basename(url.split("?")[0]).strip()
    name = re.sub(r"[^0-9A-Za-z._-]", "_", name)
    if not name or not IMG_EXT_RE.search(name):
        h = hashlib.md5(url.encode("utf-8")).hexdigest()[:16]
        name = h + ".jpg"
    return name[:120]


def unique_path(abs_path):
    """同名文件已存在且内容不同时，追加 -01/-02 递增后缀，绝不覆盖。"""
    if not os.path.exists(abs_path):
        return abs_path
    base, ext = os.path.splitext(abs_path)
    n = 1
    while True:
        cand = "{0}-{1:02d}{2}".format(base, n, ext)
        if not os.path.exists(cand):
            return cand
        n += 1


def preview_url(url):
    """
    原图地址 → 同名中等预览图（原名后加 _m），例如：
        .../6abf7560e4b048b73f3df3b4.jpg → .../6abf7560e4b048b73f3df3b4_m.jpg
    已是预览图则原样返回。
    """
    return _swap_m(url, add=True)


def full_url(url):
    """
    预览图地址 → 同名原图（去掉 _m），例如：
        .../6abf7560e4b048b73f3df3b4_m.jpg → .../6abf7560e4b048b73f3df3b4.jpg
    已是原图则原样返回。
    """
    return _swap_m(url, add=False)


def _swap_m(url, add):
    """CDN 图片「原图 ⇄ 预览图(_m)」地址互转的内部实现。"""
    if not url:
        return url
    m = re.match(r"^(?P<base>.+?)(?P<mid>_m)?(?P<ext>\.(jpg|jpeg|png|webp|gif))$", url, re.I)
    if not m:
        return url
    has_m = bool(m.group("mid"))
    if add == has_m:                       # 已是目标形态
        return url
    mid = "_m" if add else ""
    return "{0}{1}{2}".format(m.group("base"), mid, m.group("ext"))


def _save_photo(url):
    """下载单张图片到 photo 目录（内部函数，不做地址变形）。失败返回 ""，不抛异常。"""
    if not url or DEAD_IMG_RE.search(url):
        return ""
    abs_path = os.path.join(PHOTO_DIR, photo_filename(url))
    # 先判断原始路径是否已下载过（增量复用），再考虑重名情况；
    # 顺序反了会导致每跑一轮都生成 -01/-02 重复副本
    if os.path.exists(abs_path):
        if os.path.getsize(abs_path) > 0:
            return to_posix_rel(abs_path)      # 已下载过，直接复用
        abs_path = unique_path(abs_path)       # 0 字节残留，另取名避免冲突
    # 图片下载同样带 1 次重试（网络抖动/连接被重置时不再直接丢图）
    for attempt in range(2):
        try:
            r = requests.get(url, headers=HEADERS, timeout=30, stream=True)
            if r.status_code != 200 or not (r.headers.get("Content-Type") or "").startswith("image/"):
                raise RuntimeError("HTTP {0}".format(r.status_code))
            tmp = abs_path + ".part"
            with open(tmp, "wb") as f:
                for chunk in r.iter_content(chunk_size=65536):
                    if chunk:
                        f.write(chunk)
                f.flush()
                os.fsync(f.fileno())
            os.replace(tmp, abs_path)          # 原子落盘，避免半截文件
            return to_posix_rel(abs_path)
        except Exception:
            if os.path.exists(abs_path + ".part"):
                try:
                    os.remove(abs_path + ".part")
                except OSError:
                    pass
            if attempt == 0:
                time.sleep(1.0)
    return ""


def download_photo(url, prefer_preview=False):
    """
    下载单张图片到 data/新闻/香港中通社/photo/，返回本地索引路径（POSIX 风格）。
    稿件正文里的图地址多数已是预览图（_m.jpg），故：
      默认（prefer_preview=False）→ 先尝试下载**原图**，失败再回退该地址本身；
      --img-preview            → 直接下载预览图（体积约为原图 1/5）。
    本地文件名与「实际下载的地址」保持一致（原图无 _m、预览图带 _m），名实相符。
    """
    if not url:
        return ""
    os.makedirs(PHOTO_DIR, exist_ok=True)
    if prefer_preview:
        return _save_photo(preview_url(url)) or _save_photo(url)
    target = full_url(url)
    if target != url:
        got = _save_photo(target)
        if got:
            return got
    return _save_photo(url)


def attach_local_paths(article, do_download=True, prefer_preview=False):
    """
    给文章的 images 补上 local_path 本地索引（并按需下载）。
    images 元素结构：{"url": 远端地址, "caption": 图注, "local_path": 本地索引}
    下载失败的图保留 url、local_path 留空，不中断采集。
    """
    if not do_download:
        for im in article.get("images") or []:
            im["local_path"] = ""
        return 0
    ok = 0
    for im in article.get("images") or []:
        path = download_photo(im.get("url") or "", prefer_preview=prefer_preview)
        if not path and prefer_preview:      # 预览图不可用时回退原图
            path = download_photo(im.get("url") or "")
        im["local_path"] = path
        if path:
            ok += 1
    return ok


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
    ap.add_argument("--no-detail", action="store_true", help="仅采集清单，不抓正文/图片")
    ap.add_argument("--no-images", action="store_true", help="抓正文但不下载图片（local_path 留空）")
    ap.add_argument("--img-preview", action="store_true",
                    help="下载 CDN 中等预览图（_m，体积约为原图 1/5）而非原图")
    ap.add_argument("--delay", type=float, default=0.3)
    ap.add_argument("--max-pages", type=int, default=4, help="API 翻页数（每页 100 条）")
    ap.add_argument("--no-log", action="store_true",
                    help="不写日志文件（默认控制台输出同步写入 data/新闻/log/）")
    args = ap.parse_args()

    # 控制台输出实时落盘（保留控制台显示）；Ctrl+C / 异常走 finally 恢复
    setup_logging(enabled=not args.no_log)

    do_images = not (args.no_images or args.no_detail)
    articles = []
    img_ok = img_total = 0
    try:
        print("[list] 采集稿件清单（站内搜索 API 分页）...")
        links = collect_links(max_pages=args.max_pages)

        items = list(links.items())
        if args.limit:
            items = items[: args.limit]

        # 只在每轮开写前调用一次；循环内的每篇实时落盘（atomic_save）不会重复触发备份
        backup_existing_output(OUTPUT)
        if do_images:
            os.makedirs(PHOTO_DIR, exist_ok=True)

        for i, (aid, meta) in enumerate(items, 1):
            url = meta.get("url") if isinstance(meta, dict) else meta
            print("[{0}/{1}] 解析：{2}".format(i, len(items), url))
            if args.no_detail:
                articles.append({"id": aid, "url": url, "title": (meta or {}).get("title")})
                atomic_save(articles, OUTPUT)
                continue
            art = parse_article(meta)
            if art and art.get("content"):
                n_img = len(art["images"])
                n_ok = attach_local_paths(art, do_download=do_images,
                                          prefer_preview=args.img_preview)
                img_total += n_img
                img_ok += n_ok
                articles.append(art)
                print("        ✓ 标题={0} 正文={1}字 图={2}张(已存{3}) 栏目={4} 时间={5}".format(
                    (art["title"][:34] if art["title"] else None),
                    len(art["content"]),
                    n_img,
                    n_ok,
                    art["section"],
                    (art["published_at"][:16] if art["published_at"] else "无"),
                ))
            else:
                print("        ✗ 无正文（图集/视频/不可达）")
            atomic_save(articles, OUTPUT)  # 每篇实时落盘
            time.sleep(args.delay)

        print("\n[done] 已保存 {0} 篇 → {1}".format(len(articles), OUTPUT))
        if do_images:
            print("[done] 图片 {0}/{1} 张已下载 → {2}".format(img_ok, img_total, to_posix_rel(PHOTO_DIR)))
        restore_logging()
    except KeyboardInterrupt:
        # 一条都没采到就中断时不要写空数组覆盖已有成果（历史文件零丢失）
        if articles:
            atomic_save(articles, OUTPUT)
            print("\n[interrupted] 已实时保存至当前进度（{0} 篇）→ {1}".format(len(articles), OUTPUT))
        else:
            print("\n[interrupted] 尚未采集到任何条目，未写入文件（原文件保持不变）→ {0}".format(OUTPUT))
        restore_logging()
        sys.exit(130)
    except Exception as exc:
        if articles:
            atomic_save(articles, OUTPUT)
            print("\n[error] 抓取异常，已保存当前进度（{0} 篇）→ {1}".format(len(articles), OUTPUT))
        else:
            print("\n[error] 抓取异常且无进度可存，未写入文件 → {0}".format(OUTPUT))
        restore_logging()
        raise


if __name__ == "__main__":
    main()
