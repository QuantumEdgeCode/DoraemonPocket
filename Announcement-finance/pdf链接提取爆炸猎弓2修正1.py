#! python
# -*- coding: UTF-8 -*-
'''
项目名称:pro 
文件名:pdf链接提取爆炸猎弓2修正1.py
生成时间:2025/11/7 00:06:59
创建用户:x	
AIEPN Inc
'''
import csv
import os
import json
import time
from urllib.parse import urlparse, parse_qs  # 用于解析URL和查询参数
import sys
from collections import defaultdict  # 用于默认字典，便于分组累积数据

# ==================== 配置区（统一集中管理） ====================
# CSV 配置
CSV_FILENAME = './披露公告_merged.csv'  # 输入CSV文件名，包含公告数据
ROOT_DIR = './data'  # 保存目录，用于存放生成的JSON文件和日志
OUTPUT_SUFFIX = '_links.json'  # 输出文件后缀，如 000001_links.json
LOG_FILENAME = './data/extract_log.txt'  # 日志文件（假设移至 data 目录，根据错误路径调整）

# PDF 基础 URL
BASE_URL = "http://static.cninfo.com.cn/finalpage"  # PDF文件的基础URL路径

# 批量配置
BATCH_SIZE = 1000  # 每 N 条追加一次到文件，避免频繁I/O操作，提高效率
            # 注: 内存暴发户直接参数设置10000以上
# ==================== 配置区结束 ====================

# 自动创建数据目录（如果不存在）
# 这确保输出目录存在，避免后续文件写入失败
os.makedirs(ROOT_DIR, exist_ok=True)

# 配置日志：实时写入文件，控制台输出（简化，只 INFO 级）
# 使用logging模块记录程序运行信息，便于调试和监控
import logging

logging.basicConfig(
    level=logging.INFO,  # 只记录INFO及以上级别的信息
    format='%(asctime)s - %(levelname)s - %(message)s',  # 日志格式：时间-级别-消息
    handlers=[
        logging.FileHandler(LOG_FILENAME, mode='w', encoding='utf-8'),  # 实时写入文件，覆盖模式
        logging.StreamHandler(sys.stdout)  # 同时输出到控制台
    ]
)
logger = logging.getLogger(__name__)  # 获取当前模块的日志记录器


def print_progress(current, total):
    """简单内置进度条（无依赖）

    显示处理进度条，用于用户界面反馈。
    参数:
    - current: 当前处理条数
    - total: 总条数
    """
    if total == 0:
        return  # 避免除零错误
    percent = (current / total) * 100  # 计算百分比
    bar_length = 20  # 进度条长度
    filled = int(bar_length * current // total)  # 已填充部分
    bar = '=' * filled + ' ' * (bar_length - filled)  # 构建进度条字符
    sys.stdout.write(f'\rProgress: [{bar}] {percent:.1f}% ({current}/{total})')  # 覆盖式输出
    sys.stdout.flush()  # 立即刷新输出


def append_batch_to_json(stock_code, batch_links):
    """批量追加链接到对应 stock_code 的 JSON 文件

    将链接批次追加到指定股票代码的JSON文件中。
    如果文件存在，则加载后追加；否则创建新文件。
    参数:
    - stock_code: 股票代码，如 '000001'
    - batch_links: 待追加的链接列表（字典列表）
    """
    output_file = os.path.join(ROOT_DIR, f"{stock_code}{OUTPUT_SUFFIX}")  # 构建输出文件路径
    # 如果文件存在，加载并追加；否则创建新列表
    if os.path.exists(output_file):
        try:
            with open(output_file, 'r', encoding='utf-8') as f:  # 读取现有文件
                existing_links = json.load(f)  # 解析JSON
        except json.JSONDecodeError:
            existing_links = []  # 如果 JSON 损坏，重置为空列表
            logger.warning(f"JSON文件损坏，已重置: {output_file}")
    else:
        existing_links = []  # 新文件，从空列表开始

    existing_links.extend(batch_links)  # 追加新批次

    # 重写文件（紧凑格式）
    try:
        # os.makedirs(ROOT_DIR, exist_ok=True) # 已在上游创建，无需重复
        with open(output_file, 'w', encoding='utf-8') as f:  # 以UTF-8编码写入
            json.dump(existing_links, f, ensure_ascii=False, indent=None, separators=(',', ':'))  # 紧凑JSON，无缩进
        logger.info(f"批量追加到 {output_file}: {len(batch_links)} 条 (总计 {len(existing_links)} 条)")  # 记录追加信息
    except Exception as e:
        logger.error(f"批量追加到 {output_file} 失败: {e}")  # 记录错误


# 读取 CSV 行数（大文件计数，避免内存加载）
def count_csv_rows(filename):
    """高效计数 CSV 行数（跳过标题）

    不加载整个文件到内存，仅计数数据行（跳过首行标题）。
    参数:
    - filename: CSV文件名
    返回:
    - 整数：数据行数
    """
    try:
        if not os.path.exists(filename):
            raise FileNotFoundError(f"文件不存在: {filename}")
        with open(filename, 'r', encoding='utf-8') as file:  # 以UTF-8读取
            reader = csv.reader(file)  # 创建CSV阅读器
            next(reader, None)  # 跳过标题行
            return sum(1 for _ in reader)  # 计数剩余行
    except Exception as e:
        logger.error(f"计数 CSV 失败: {e}")
        return 0  # 出错返回0


# 检查输入文件并计数
# 验证CSV文件存在，并获取总记录数，用于进度显示
if not os.path.exists(CSV_FILENAME):
    logger.error(f"输入文件不存在: {CSV_FILENAME}")
    print(f"错误: 输入文件 {CSV_FILENAME} 不存在。请确保文件在当前目录，并重试。")
    sys.exit(1)  # 如果在脚本模式，退出；REPL 中可注释

total_records = count_csv_rows(CSV_FILENAME)  # 计算总行数
logger.info(f"成功读取 CSV: {total_records} 条记录")  # 记录成功信息
print(f"成功读取 CSV: {total_records} 条记录")  # 控制台输出

# 开始整体计时
overall_start = time.time()  # 记录程序开始时间
extract_start = time.time()  # 记录提取开始时间
logger.info("开始提取 PDF 链接，按 stock_code 批量实时写入文件...")  # 开始日志

# 内存中分组：{stock_code: [links]} - 累积到 BATCH_SIZE 后追加
# 使用defaultdict按股票代码分组链接列表，避免内存溢出
stock_batches = defaultdict(list)

try:
    with open(CSV_FILENAME, 'r', encoding='utf-8') as file:  # 打开CSV文件
        reader = csv.reader(file)  # 创建阅读器
        next(reader, None)  # 跳过标题行
        current = 0  # 当前处理行计数器
        for row in reader:  # 逐行处理
            current += 1
            print_progress(current, total_records)  # 显示进度
            if len(row) < 6:  # 检查列数是否足够
                logger.warning(f"行 {current} 列不足，跳过")  # 记录警告
                continue
            stock_code = row[0].strip()  # 股票代码（第1列，索引0）
            stock_short_name = row[2].strip() if len(row) > 2 and row[2] else ""  # 股票简称（修正为第3列，索引2）
            title_original = row[3].strip() if row[3] else "未知标题"  # 标题（第4列）
            announce_time = row[4].strip() if row[4] else ""  # 公告时间（第5列）
            announce_date = announce_time.split(' ')[
                0] if ' ' in announce_time else announce_time  # 提取日期 YYYY-MM-DD，安全处理
            url = row[5].strip()  # 公告链接（第6列）

            parsed_url = urlparse(url)  # 解析URL
            query_params = parse_qs(parsed_url.query)  # 解析查询参数
            announcement_id = query_params.get('announcementId', [''])[0]  # 提取announcementId

            if not announcement_id:  # 如果ID为空，跳过
                logger.warning(f"无法提取 announcementId: 第 {current} 行")
                continue

            pdf_url = f"{BASE_URL}/{announce_date}/{announcement_id}.PDF"  # 构建PDF完整URL

            # 追加到内存批次
            link_entry = {  # 创建链接条目字典
                "id": announcement_id,  # 公告ID
                "url": pdf_url,  # PDF URL
                "title": title_original,  # 标题
                "date": announce_date,  # 日期
                "stock_code": stock_code,  # 股票代码
                "stock_short_name": stock_short_name  # 股票简称
            }
            stock_batches[stock_code].append(link_entry)  # 添加到对应股票批次

            # 达到批量阈值时，追加到文件
            if len(stock_batches[stock_code]) >= BATCH_SIZE:
                append_batch_to_json(stock_code, stock_batches[stock_code])  # 批量写入
                stock_batches[stock_code] = []  # 清空批次，释放内存

except Exception as e:  # 捕获处理异常
    logger.error(f"提取过程失败: {e}")
    print(f"提取过程失败: {e}")
    sys.exit(1)  # 退出程序

# 处理剩余批次（每个 stock_code 清空内存）
# 循环处理所有剩余未写入的批次
for stock_code, remaining_links in stock_batches.items():
    if remaining_links:  # 如果有剩余链接
        append_batch_to_json(stock_code, remaining_links)  # 写入文件

extract_end = time.time()  # 记录提取结束时间
extract_time = extract_end - extract_start  # 计算提取耗时
logger.info(f"提取完成，耗时: {extract_time:.2f} 秒")  # 记录完成信息
print(f"\n提取完成，耗时: {extract_time:.2f} 秒")  # 控制台输出

overall_end = time.time()  # 记录程序结束时间
overall_time = overall_end - overall_start  # 计算总耗时
logger.info(f"程序总耗时: {overall_time:.2f} 秒")  # 记录总耗时
print(f"程序总耗时: {overall_time:.2f} 秒 (提取: {extract_time:.2f}s)")  # 控制台输出
print(f"日志文件: {LOG_FILENAME}")  # 输出日志路径
print(f"输出目录: {ROOT_DIR} (每个股票一个 JSON 文件)")  # 输出结果说明
