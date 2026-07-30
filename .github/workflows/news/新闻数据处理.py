#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
新闻数据处理脚本（完整中文注释版）

【功能概述】
本脚本用于把多个新闻爬虫产出的 JSON 文件，处理成一份易于阅读的 Markdown 日报。
处理流程分为四步：
  1. 加载（load）：读取指定目录下所有 *_collection.json，归一化字段后合并成统一列表；
  2. 去重（dedup）：按新闻标题去掉重复条目（保留首次出现者）；
  3. 分类（classify）：根据标题中的关键词，把新闻归入「国际 / 国内」等若干板块；
  4. 生成（generate）：把分类结果渲染成 Markdown 文本，写出简报版与完整版两份。

【输入数据说明】
脚本兼容两种 JSON 结构（爬虫产出格式不统一，必须都能吃）：
  - 结构 A（纯列表）：文件内容直接是 list[article]，例如 aljazeera_collection.json；
  - 结构 B（字典包裹）：文件内容是 dict，正文列表放在 "articles" / "items" / "all_items" 键下，
    且字典顶层可能带 "source" / "language" 等元信息，例如 aa_collection.json。
每条 article 的真实字段为：
  id, url, title, summary, content, published_at, section, author, images, language
其中脚本需要的两个关键字段是：
  - title  ：新闻标题（去重与分类的依据）
  - content：新闻正文（日报正文内容；若为空则回退用 summary 兜底）
  - source ：来源媒体（实际数据中，列表结构没有该字段，需要从文件名推断；字典结构从顶层取）
  - content_chars：正文字数（实际数据没有该字段，由脚本自动计算）

【输出说明】
默认同时生成两份 Markdown：
  - 简报版  news_daily_{date}.md           ：正文截断前 300 字、每类最多 12 条、其他类最多 10 条；
  - 完整版  news_daily_{date}_full.md      ：正文全文不截断、所有分类全量输出（含「其他」类全部条目）。
文件若已存在，save_md_with_suffix 会自动追加 _01 / _02 等后缀，避免覆盖历史日报。

【用法】
  python 新闻数据处理.py
  python 新闻数据处理.py --input ./data/新闻 --output ./data --date 2026-07-30
"""

# ============================================================
# 标准库导入
# ============================================================
import os            # 文件路径、目录操作、判断文件/目录是否存在
import re            # 正则表达式：清洗正文空白、解析目录名中的日期
import glob          # 按通配符批量查找文件（如 *_collection.json）
import json          # 读写 JSON 数据
import argparse      # 解析命令行参数（--input / --output / --date）
from collections import OrderedDict  # 保序字典：确保分类板块顺序稳定（先定义先输出）
from datetime import datetime        # 日期解析与「生成时间」时间戳


# ============================================================
# 全局配置常量
# ============================================================

# 简报版正文的预览字数上限。超过该字数的正文会被截断并标注「（全文 X 字）」，
# 目的是避免简报体积过大、便于快速浏览。完整版传入 None 表示不截断。
CONTENT_PREVIEW = 300

# 来源别名映射表。
# 背景：列表结构的 JSON 文件没有顶层 source 字段，脚本退而用「文件名 stem」当来源名
# （例如 aljazeera_collection.json 的 stem 是 "aljazeera"），但裸文件名可读性差。
# 本表把这些裸名映射成可读的中文/英文名（如 "aljazeera" -> "Al Jazeera"）。
# 字典结构的 JSON 自带完整 source（如 "Anadolu Agency (aa.com.tr)"），会优先使用，不受本表影响。
SOURCE_ALIASES = {
    "aa": "Anadolu", "aljazeera": "Al Jazeera", "asahi": "朝日新闻",
    "belta": "白俄罗斯通讯社", "bernama": "马来西亚国家通讯社", "bloomberg": "彭博社",
    "cna": "中央通讯社", "donga": "东亚日报", "gnews": "Google News",
    "irna": "伊朗通讯社(中文)", "irna_fa": "伊朗通讯社(波斯语)", "mena": "埃及中东通讯社",
    "mk": "每日经济新闻", "nikkei": "日本经济新闻", "rg": "俄罗斯报",
    "ria": "俄新社",     "scmp": "南华早报", "sputnik": "俄罗斯卫星通讯社",
    "straitstimes": "海峡时报", "tass": "塔斯社", "hkcd": "香港商报", "hk01": "香港01", "hkcna": "香港中通社", "nhandan": "越南人民报", "leparisien": "巴黎人报", "lemonde": "世界报",
}


# ============================================================
# 步骤 1：加载并归一化数据
# ============================================================
def load_all_items(news_dir):
    """读取数据目录下所有 *_collection.json，归一化字段后合并为条目列表。

    返回 list[dict]，每条至少包含：title / url / content / source / content_chars。
    该函数兼容「纯列表」与「字典包裹」两种 JSON 结构，
    并对缺失字段做兜底处理（source 取文件级或别名，content 空时用 summary 兜底，
    content_chars 自动计算）。
    """
    items = []  # 最终合并后的所有新闻条目

    # 优先匹配 *_collection.json；如果该模式没有命中文件，则退化为匹配目录内任意 *.json
    files = sorted(glob.glob(os.path.join(news_dir, "*_collection.json")))
    if not files:
        # 兜底：直接处理目录内所有 json（防止命名不规范导致读不到数据）
        files = sorted(glob.glob(os.path.join(news_dir, "*.json")))

    for f in files:
        # 取文件名主干（去掉 .json 与 _collection 后缀），作为列表结构的来源兜底名
        # 例：aljazeera_collection.json -> 主干 "aljazeera"
        stem = os.path.splitext(os.path.basename(f))[0].replace("_collection", "")

        # 读取并解析 JSON，解析失败则跳过该文件并给出警告，不中断整体流程
        try:
            with open(f, encoding="utf-8") as fh:
                data = json.load(fh)
        except Exception as e:
            print(f"[WARN] 跳过无法解析的文件 {f}: {e}")
            continue

        # 根据 JSON 顶层类型分流处理：
        #   list  -> 正文就是它本身，来源名用上面算出的 stem
        #   dict  -> 正文在 articles/items/all_items 其中之一，来源名优先取顶层 source
        if isinstance(data, list):
            arts = data
            raw_src = stem
        elif isinstance(data, dict):
            arts = data.get("articles") or data.get("items") or data.get("all_items") or []
            raw_src = data.get("source") or stem
        else:
            # 既不是 list 也不是 dict（例如字符串/数字），无法处理，跳过
            print(f"[WARN] 未知结构，跳过 {f}")
            continue

        # 把原始来源名（stem 或顶层 source）映射成可读别名；未在表中则保持原样
        src = SOURCE_ALIASES.get(raw_src, raw_src)

        # 逐条文章做字段归一化
        for a in arts:
            if not isinstance(a, dict):
                # 防御性判断：数组中混入了非字典元素（如字符串）则跳过
                continue
            a = dict(a)  # 复制一份，避免直接修改原 JSON 对象（保持数据纯净）
            a.setdefault("source", src)  # 若该条没有 source，则补上本文件的来源

            content = a.get("content") or ""  # 取正文，缺失视为空字符串
            # 正文为空时，用 summary（摘要）兜底，保证日报里至少有可读内容
            if not content and a.get("summary"):
                content = a["summary"]
            a["content"] = content

            # 计算正文字数；若原始数据已带 content_chars 则尊重原值，否则按当前 content 长度算
            a["content_chars"] = a.get("content_chars", len(content))

            items.append(a)

    return items


# ============================================================
# 步骤 2：去重
# ============================================================
def deduplicate(items):
    """按标题去重，保留最先出现的条目，标题长度需 >= 6 才参与去重。

    去重策略说明：
      - 以「标题原文（去除首尾空白）」为唯一键；
      - 用集合 seen 记录已出现过的标题，实现 O(1) 查重；
      - 只保留首次出现的条目（后出现的重复标题直接丢弃）；
      - 要求标题长度 >= 6：过短标题（如 "US"）噪声大、易误判为重复，故不参与去重、直接保留。
    注意：这是「精确标题匹配」，跨源但措辞不同的英文标题不会被判定为重复（如仍保留）。
    """
    seen = set()       # 已见过的标题集合
    result = []        # 去重后的条目列表
    for it in items:
        t = (it.get("title") or "").strip()  # 取出标题并去首尾空白
        # 标题非空、长度达标、且此前未出现过 -> 保留
        if t and len(t) >= 6 and t not in seen:
            seen.add(t)
            result.append(it)
    return result


# ============================================================
# 步骤 3：分类
# ============================================================
def classify_news(items):
    """基于标题关键词的多维度自动分类（中英文关键词并存）。

    分类机制：
      - 预定义 11 个分类，用 OrderedDict 保证输出顺序固定；
      - 每个分类配一组关键词（中文 + 英文并存，因为实际数据标题多为英文）；
      - 遍历每篇新闻，按「分类定义顺序」逐类检查标题是否包含任一关键词，
        命中即归入该类并 break（先匹配者优先，避免一条新闻被重复归类）；
      - 所有分类都未命中 -> 归入「其他」兜底类。
    局限：关键词覆盖有限（体育/娱乐/纯地方新闻等不含地缘词），这类会被大量归入「其他」。
    """
    # 分类顺序即优先级：先定义先匹配。最后「其他」为兜底。
    cats = OrderedDict([
        ("中东局势", []), ("欧洲", []), ("美洲", []), ("亚洲", []),
        ("非洲/大洋洲", []), ("国际组织/法律", []),
        ("国内时政", []), ("台风/灾害", []), ("南海/外交", []),
        ("经济/科技/社会", []), ("其他", []),
    ])

    # 各分类对应的关键词表（中英文并存）。标题中出现任一关键词即判定属于该类。
    keywords = {
        "中东局势": ["伊朗", "Israel", "Iran", "巴勒斯坦", "加沙", "Gaza", "胡塞", "Houthi",
                    "也门", "Yemen", "红海", "Red Sea", "沙特", "Saudi", "伊拉克", "Iraq",
                    "叙利亚", "Syria", "黎巴嫩", "Lebanon", "中东", "Middle East", "Hormuz"],
        "欧洲": ["德国", "Germany", "法国", "France", "英国", "UK", "Britain", "乌克兰", "Ukraine",
                 "Russia", "俄罗斯", "北约", "NATO", "欧盟", "EU", "Europe"],
        "美洲": ["美国", "US", "USA", "America", "特朗普", "Trump", "巴西", "Brazil",
                 "白宫", "White House", "马斯克", "Musk"],
        "亚洲": ["日本", "Japan", "韩国", "South Korea", "Korea", "朝鲜", "North Korea",
                 "印度", "India", "菲律宾", "Philippines", "东盟", "ASEAN", "台湾", "Taiwan",
                 "中国", "China"],
        "非洲/大洋洲": ["非洲", "Africa", "澳大利亚", "Australia", "南非", "South Africa"],
        "国际组织/法律": ["联合国", "UN", "United Nations", "ICC", "世卫", "WHO", "court", "tribunal"],
        "国内时政": ["习近平", "王毅", "李强", "国务院", "中央", "Xi Jinping", "Wang Yi", "Li Qiang"],
        "台风/灾害": ["台风", "typhoon", "暴雨", "flood", "洪水", "earthquake", "地震",
                      "disaster", "hurricane"],
        "南海/外交": ["南海", "South China Sea", "黄岩岛", "Scarborough", "仁爱礁", "Spratly",
                      "海警", "coast guard"],
        "经济/科技/社会": ["经济", "economy", "科技", "technology", "AI",
                          "artificial intelligence", "股市", "stock", "低空经济", "GDP", "market"],
    }

    # 逐条新闻归类
    for item in items:
        title = item.get("title", "") or ""
        assigned = False  # 是否已归入某类
        # 按分类优先级顺序检查关键词
        for cat, kws in keywords.items():
            # 标题中只要包含该类的任意一个关键词，即归此类
            if any(kw in title for kw in kws):
                cats[cat].append(item)
                assigned = True
                break  # 命中即停止，保证每篇只进一个分类
        if not assigned:
            # 全部关键词都不命中 -> 放进「其他」兜底类
            cats["其他"].append(item)
    return cats


# ============================================================
# 步骤 4（辅助）：把单条新闻渲染成 Markdown 行
# ============================================================
def _fmt_item(item, preview_len=CONTENT_PREVIEW):
    """把单条新闻格式化为 Markdown 文本行。

    参数：
      item         : 单条新闻字典（含 title/url/source/content/content_chars/images/type）
      preview_len  : 正文预览字数上限；为 None 表示「完整版」——不截断、全文输出
    返回：
      (line, extra) 二元组：
        line  : 标题行，形如  "- 标题。[来源](链接)"；图表/组图类加 🖼️ 标记
        extra : 附加行列表（缩进引用），含正文预览与图片（完整版才铺图链，简报版仅标数量）
    说明：
      - 简报版传 preview_len=300 -> 超长正文截断并在末尾加「…」，再标注「（全文 X 字）」；
      - 完整版传 preview_len=None -> 正文原样输出，且不附加字数控告后缀；
      - 图片：完整版铺出全部图链（不截断、不省略），简报版仅标「共 N 张图」，避免日报过长；
      - type=="infographic"（越南人民报图表/组图类）标题加 🖼️ 标记，便于一眼区分。
    """
    src = item.get("source", "")         # 来源媒体
    url = item.get("url", "")            # 原文链接
    title = item.get("title", "")        # 标题
    content = item.get("content", "") or ""          # 正文（可能为空）
    chars = item.get("content_chars", len(content))  # 正文字数
    itype = item.get("type", "") or ""                # 条目类型（infographic=图表/组图）
    images = item.get("images", []) or []            # 图片列表（dict 含 url，或纯 url 字符串）

    # 标题行：图表/组图类加 🖼️ 标记，便于日报一眼区分
    prefix = "🖼️ " if itype == "infographic" else ""
    line = f"- {prefix}{title}。[{src}]({url})"

    extra = []  # 附加行：正文引用 + 图片信息
    if content:
        # 把正文里的连续空白（换行/多空格）压成单个空格，并去掉首尾空白，便于在日报中单行显示
        clean = re.sub(r"\s+", " ", content).strip()
        # 简报模式（preview_len 不是 None）下，超长则截断并加省略号
        if preview_len is not None and len(clean) > preview_len:
            clean = clean[:preview_len] + "…"
        # 完整模式不加「（全文 X 字）」后缀；简报模式附加该字数控告
        suffix = "" if preview_len is None else f"（全文 {chars} 字）"
        extra.append(f"  > {clean}{suffix}")  # 缩进两格 + Markdown 引用符号 >

    # 图片：完整版（preview_len=None）铺出全部图链，不截断不省略；简报版仅标数量
    if images:
        n = len(images)
        extra.append(f"  > 📷 共 {n} 张图")
        if preview_len is None:
            for im in images:
                u = im.get("url") if isinstance(im, dict) else im
                if u:
                    extra.append(f"  > ![]({u})")
    return line, extra


# ============================================================
# 步骤 4（主）：生成整份 Markdown 日报
# ============================================================
def generate_markdown(date_str, categories, preview_len=CONTENT_PREVIEW, max_per_cat=12, max_other=10):
    """把分类结果渲染成完整的 Markdown 日报文本并返回字符串。

    参数：
      date_str     : 日报日期字符串，如 "2026-07-30"
      categories   : classify_news 返回的 OrderedDict（分类 -> 条目列表）
      preview_len  : 传给 _fmt_item 的正文预览上限（None = 完整版不截断）
      max_per_cat  : 每个「国际/国内」分类最多输出的条目数（None = 不限制）
      max_other    : 「其他」类最多输出的条目数（None = 不限制）
    板块结构：
      - 国际新闻：中东局势 / 欧洲 / 美洲 / 亚洲 / 非洲·大洋洲 / 国际组织·法律
      - 国内新闻：国内时政 / 台风·灾害 / 南海·外交 / 经济·科技·社会
      - 其他要闻：未被上述关键词命中的兜底条目
    """
    # 解析日期并换算中文星期（用于日报大标题）
    dt = datetime.fromisoformat(date_str).date()
    weekday_cn = ["星期一", "星期二", "星期三", "星期四", "星期五", "星期六", "星期日"]
    wd = weekday_cn[dt.weekday()]

    # 日报开头：大标题 + 数据来源说明 + 分隔线
    lines = [f"# 📰 全球新闻日报 — {dt.year}年{dt.month}月{dt.day}日（{wd}）", ""]
    lines.append("> 数据来源：多源聚合（已去重、分类） | 整理日期：" + date_str)
    lines.append("---\n")

    # ---------- 国际新闻板块 ----------
    intl_cats = ["中东局势", "欧洲", "美洲", "亚洲", "非洲/大洋洲", "国际组织/法律"]
    # 仅当国际板块中至少有一个分类非空时才输出「## 国际新闻」标题
    if any(categories.get(c) for c in intl_cats):
        lines.append("## 🌐 国际新闻\n")
        for cat in intl_cats:
            items = categories.get(cat, [])
            if not items:
                continue  # 该分类无内容则跳过
            lines.append(f"### {cat}\n")  # 子分类标题
            # max_per_cat 为 None 时输出全部；否则只取前 max_per_cat 条
            for item in (items if max_per_cat is None else items[:max_per_cat]):
                line, extra = _fmt_item(item, preview_len)  # 渲染标题行 + 附加行（正文/图片）
                lines.append(line)
                lines.extend(extra)  # 追加正文引用与图片信息
            lines.append("")  # 分类之间空一行，提升可读性

    # ---------- 国内新闻板块 ----------
    dom_cats = ["国内时政", "台风/灾害", "南海/外交", "经济/科技/社会"]
    if any(categories.get(c) for c in dom_cats):
        lines.append("## 🇨🇳 国内新闻\n")
        # 国内各子分类的「美化标题」（带 emoji），找不到则用默认「### 分类名」
        labels = {
            "国内时政": "### 📜 重要时政",
            "台风/灾害": "### 🌪️ 台风/灾害",
            "南海/外交": "### 🏛️ 南海/外交",
            "经济/科技/社会": "### 💼 经济/科技/社会",
        }
        for cat in dom_cats:
            items = categories.get(cat, [])
            if not items:
                continue
            lines.append(labels.get(cat, f"### {cat}") + "\n")
            for item in (items if max_per_cat is None else items[:max_per_cat]):
                line, extra = _fmt_item(item, preview_len)
                lines.append(line)
                lines.extend(extra)
            lines.append("")

    # ---------- 其他要闻板块 ----------
    others = categories.get("其他", [])
    if others:
        lines.append("### 📌 其他要闻\n")
        for item in (others if max_other is None else others[:max_other]):
            line, quote = _fmt_item(item, preview_len)
            lines.append(line)
            if quote:
                lines.append(quote)
        # 若「其他」被截断（限制了条数且实际更多），追加一行提示真实总数
        if max_other is not None and len(others) > max_other:
            lines.append(f"- ……（其他要闻共 {len(others)} 条，仅显示前 {max_other} 条）")

    # ---------- 日报结尾：生成说明与时间戳 ----------
    lines.append("\n---\n")
    lines.append("> 📌 **说明**：本日报由新闻数据处理引擎自动生成。")
    lines.append(f"> 生成时间：{datetime.now().strftime('%Y-%m-%d %H:%M')}")
    return "\n".join(lines)


# ============================================================
# 工具函数：防覆盖保存
# ============================================================
def save_md_with_suffix(base_path, content):
    """把内容写到 base_path；若文件已存在，则自动追加 _01 / _02 … 后缀避免覆盖。

    例如 base_path 为 news_daily_2026-07-30.md 且已存在时，会依次尝试
    news_daily_2026-07-30_01.md、_02.md … 直到找到一个不存在的路径再写入。
    返回最终实际保存的文件路径。
    """
    # 目标文件不存在 -> 直接写入
    if not os.path.exists(base_path):
        with open(base_path, "w", encoding="utf-8") as f:
            f.write(content)
        return base_path

    # 已存在 -> 拆分目录 / 文件名 / 扩展名，循环找可用的带序号路径
    dir_name = os.path.dirname(base_path) or "."
    name, ext = os.path.splitext(os.path.basename(base_path))
    i = 1
    while True:
        new_path = os.path.join(dir_name, f"{name}_{i:02d}{ext}")  # 编号两位补零：_01, _02...
        if not os.path.exists(new_path):
            with open(new_path, "w", encoding="utf-8") as f:
                f.write(content)
            return new_path
        i += 1


# ============================================================
# 工具函数：从目录名推断日期
# ============================================================
def parse_date_from_dir(news_dir):
    """从目录名中推断日报日期。

    目录命名约定形如  .../2026-07-30-am/news  或  .../2026-07-30/news。
    优先匹配路径中「YYYY-MM-DD」或「YYYY-MM-DD-am/pm」形式的分段；
    若路径分段都不匹配（例如默认目录 ./data/新闻 不含日期），
    则回退取父目录名再解析；再不行就退化为「今天」。
    建议显式传 --date 以保证日报文件名日期准确。
    """
    d = os.path.normpath(news_dir)  # 规范化路径分隔符
    # 第一遍：检查路径的每一级目录名
    for p in d.split(os.sep):
        m = re.match(r"^(\d{4}-\d{2}-\d{2})(-am|-pm)?$", p)
        if m:
            return m.group(1)  # 命中：返回日期部分
    # 第二遍：回退取父目录名（如 .../2026-07-30-am/news 的父目录是 2026-07-30-am）
    parent = os.path.basename(os.path.dirname(d))
    m = re.match(r"^(\d{4}-\d{2}-\d{2})", parent)
    if m:
        return m.group(1)
    # 兜底：实在解析不出就用当前日期
    return datetime.now().strftime("%Y-%m-%d")


# ============================================================
# 主函数：串联以上所有步骤
# ============================================================
def main():
    # ---- 1. 解析命令行参数 ----
    ap = argparse.ArgumentParser(description="新闻 JSON 去重 / 分类 / 生成日报 MD")
    ap.add_argument("--input", default="./data/新闻",
                    help="包含 *_collection.json 的目录")
    ap.add_argument("--output", default=None,
                    help="输出目录，默认与 input 同级（去掉 /news）")
    ap.add_argument("--date", default=None,
                    help="日报日期 YYYY-MM-DD，默认从目录名推断")
    args = ap.parse_args()

    # ---- 2. 校验输入目录 ----
    news_dir = args.input
    if not os.path.isdir(news_dir):
        print(f"[ERROR] 输入目录不存在: {news_dir}")
        return 1  # 非零退出码表示失败

    # ---- 3. 确定输出目录（默认去掉输入目录末尾的 /news）----
    out_dir = args.output or os.path.dirname(os.path.normpath(news_dir)) or "."
    os.makedirs(out_dir, exist_ok=True)  # 不存在则创建，存在则不报错

    # ---- 4. 确定日报日期 ----
    date_str = args.date or parse_date_from_dir(news_dir)

    # ---- 5. 执行核心四步：加载 -> 去重 -> 分类 ----
    print(f"[INFO] 读取目录: {news_dir}")
    raw = load_all_items(news_dir)
    print(f"[INFO] 原始条目: {len(raw)}")

    dedup = deduplicate(raw)
    print(f"[INFO] 去重后: {len(dedup)}")

    cats = classify_news(dedup)
    dist = {k: len(v) for k, v in cats.items()}  # 统计每个分类的条目数，便于在日志里观察分布
    print(f"[INFO] 分类分布: {dist}")

    # ---- 6. 定义生成函数（闭包，复用 date_str / cats / out_dir）----
    def build(preview_len, max_per_cat, max_other, suffix, label):
        """根据参数生成一份日报并保存。

        preview_len/max_per_cat/max_other 控制「简报 or 完整版」的形态；
        suffix 用于区分文件名（"" 或 "_full"）；label 仅用于日志标识。
        """
        md = generate_markdown(date_str, cats, preview_len, max_per_cat, max_other)
        out_path = os.path.join(out_dir, f"news_daily_{date_str}{suffix}.md")
        saved = save_md_with_suffix(out_path, md)
        print(f"[INFO] 日报已生成({label}): {saved} ({len(md)} 字节)")
        return saved

    # ---- 7. 默认同时生成两份：简报（截断） + 完整版（不截断）----
    # 简报版：正文截断 300 字、每类最多 12 条、其他最多 10 条
    build(CONTENT_PREVIEW, 12, 10, "", "简报")
    # 完整版：正文不截断（preview_len=None）、所有分类全量输出（max 均为 None）
    build(None, None, None, "_full", "完整版")
    return 0  # 零退出码表示成功


# ============================================================
# 程序入口
# ============================================================
if __name__ == "__main__":
    # 用 raise SystemExit 把 main() 的返回值转为进程退出码（0=成功，非0=失败）
    raise SystemExit(main())
