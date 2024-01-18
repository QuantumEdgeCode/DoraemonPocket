#! python
# -*- coding: UTF-8 -*-
'''
项目名称:python 
文件名:获取日月年k线优化版231206.py
生成时间:2023/12/6 18:26:31
创建用户:musk	
AIEPN Inc
'''

import os
import time
import logging
import efinance as ef
from datetime import datetime

# 获取当前日期信息
current_date = datetime.now().strftime("%Y%m%d")
# 构建新的目录名称
data_directory = f"./data/hk/1分钟级别/{current_date}"

def create_data_directory(directory):
    if not os.path.exists(directory):
        os.makedirs(directory)

# 创建数据目录
create_data_directory(data_directory)

# 配置日志
log_filename = f"{data_directory}/{current_date}_log.txt"
log_format = '%(asctime)s - %(levelname)s - %(message)s'
logging.basicConfig(filename=log_filename, level=logging.INFO, format=log_format)

def get_unique_filename(file_path):
    if not os.path.exists(file_path):
        return file_path

    filename, extension = os.path.splitext(file_path)
    index = 1
    while True:
        new_file_path = f"{filename}_{index:02d}{extension}"
        if not os.path.exists(new_file_path):
            return new_file_path
        index += 1

'''
101: 日线
102: 周线
103: 月线
104：极度线(3月)
5：5分钟线(最近2个月左右)
1：1分钟线(最近一个交易日(没开盘就是上一个交易日))
'''
def get_stock_quote_with_retry(stock_code, klt=1, max_retries=3):
    retry_count = 0
    while retry_count < max_retries:
        try:
            df = ef.stock.get_quote_history(stock_code, klt=klt)
            return df
        except Exception as e:
            retry_count += 1
            message = f"股票代码 {stock_code} 数据获取失败，正在重试 ({retry_count}/{max_retries})..."
            logging.warning(message)
            print(message)
            time.sleep(5)

    message = f"获取股票代码 {stock_code} 的数据失败，达到最大重试次数"
    logging.error(message)
    with open(f"{data_directory}/error_log.txt", "a") as log_file:
        log_file.write(f"{message}\n")
    return None

def write_data_to_csv(df, code, directory):
    create_data_directory(directory)
    filename = f"{directory}/{code}.csv"
    unique_filename = get_unique_filename(filename)
    df.to_csv(unique_filename, encoding='utf-8-sig', index=None)
    message = f"股票代码：{code} 的数据已保存到文件 {os.path.basename(unique_filename)} 中"
    logging.info(message)
    print(message)

# 从 code.txt 读取股票代码
with open("./code-list/hk-full240115.txt", "r") as file:
    stock_codes = [line.strip() for line in file]

# 处理每个股票代码
for stock_code in stock_codes:
    df = get_stock_quote_with_retry(stock_code)
    if df is not None:
        write_data_to_csv(df, stock_code, data_directory)
    else:
        message = f"股票代码：{stock_code} 的数据获取失败，将跳过并自动获取下一个代码。"
        logging.warning(message)
        print(message)
