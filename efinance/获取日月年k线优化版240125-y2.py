#! python
# -*- coding: UTF-8 -*-
'''
项目名称:python 
集成开发环境:PyCharm
文件名:获取日月年k线优化版240125-y2.py.py
生成时间:2024-01-25 08:20:34
创建用户:musk	
AIEPN Inc
'''
import os
import time
import shutil
import logging
import efinance as ef
from datetime import datetime

# 获取当前日期信息
current_date = datetime.now().strftime("%Y%m%d")

# 获取用户输入的文件名
file_name = input("请输入文件名（例如：shcode.txt 或 shcode）: ")

# 如果用户输入的文件名没有后缀'.txt'，则添加后缀
if not file_name.endswith('.txt'):
    file_name += '.txt'

# 尝试在当前目录查找文件
file_path = f"./{file_name}"

# 如果文件不存在，则在code-list目录中查找
if not os.path.exists(file_path):
    file_path_in_code_list = f"./code-list/{file_name}"
    if os.path.exists(file_path_in_code_list):
        file_path = file_path_in_code_list
    else:
        raise FileNotFoundError(f"找不到文件: {file_name}")

# 读取股票代码
with open(file_path, "r", encoding="utf-8") as file:
    stock_codes = [line.strip() for line in file]
'''
101: 日线
102: 周线
103: 月线
104：季度线(3月)
105：半年线(6月)
106：年度线
1：1分钟线(最近一个交易日(没开盘就是上一个交易日))
5：5分钟线(最近2个月左右)
30：30分钟线
60：60分钟线
'''
# 打印提示信息
print('''
101: 日线
102: 周线
103: 月线
104：季度线(3月)
105：半年线(6月)
106：年度线
1：1分钟线(最近一个交易日(没开盘就是上一个交易日))
5：5分钟线(最近2个月左右)
30：30分钟线
60：60分钟线
''')
# 获取用户输入的kit值
kit = input("请输入kit值（例如：101、102、103、104、105、106、1、5、30、60）: ")

# 获取用户输入的交易市场
trading_market = input("请输入交易市场（例如：a、hk、us、uk 等）: ")

# 根据kit值和交易市场设置不同的数据目录
if kit == "101":
    data_directory = f"./data/{trading_market}/day/{current_date}"
elif kit == "102":
    data_directory = f"./data/{trading_market}/zhou/{current_date}"
elif kit == "103":
    data_directory = f"./data/{trading_market}/yue/{current_date}"
elif kit == "104":
    data_directory = f"./data/{trading_market}/3mo/{current_date}"
elif kit == "105":
    data_directory = f"./data/{trading_market}/6mo/{current_date}"
elif kit == "106":
    data_directory = f"./data/{trading_market}/year/{current_date}"
elif kit == "1":
    data_directory = f"./data/{trading_market}/1分钟级别/{current_date}"
elif kit == "5":
    data_directory = f"./data/{trading_market}/5分钟级别/{current_date}"
elif kit == "30":
    data_directory = f"./data/{trading_market}/30分钟级别/{current_date}"
elif kit == "60":
    data_directory = f"./data/{trading_market}/60分钟级别/{current_date}"
else:
    raise ValueError("不支持的kit值，请输入101、102、103、104、105、106、1、5、30、60")

# 创建数据目录的函数
def create_data_directory(directory):
    if not os.path.exists(directory):
        os.makedirs(directory)

# 创建数据目录
create_data_directory(data_directory)

# 配置日志
log_filename = f"{data_directory}/{current_date}_log.txt"
log_format = '%(asctime)s - %(levelname)s - %(message)s'
logging.basicConfig(filename=log_filename, level=logging.INFO, format=log_format)

# 获取唯一文件名的函数
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

# 写入数据到CSV文件的函数
def write_data_to_csv(df, code, directory):
    create_data_directory(directory)
    filename = f"{directory}/{code}.csv"
    unique_filename = get_unique_filename(filename)
    df.to_csv(unique_filename, encoding='utf-8-sig', index=None)
    message = f"股票代码：{code} 的数据已保存到文件 {os.path.basename(unique_filename)} 中"
    logging.info(message)
    print(message)

# 获取股票数据的函数
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

# 处理每个股票代码
for stock_code in stock_codes:
    # df = get_stock_quote_with_retry(stock_code)
    df = get_stock_quote_with_retry(stock_code, klt=int(kit))
    if df is not None:
        write_data_to_csv(df, stock_code, data_directory)
    else:
        message = f"股票代码：{stock_code} 的数据获取失败，将跳过并自动获取下一个代码。"
        logging.warning(message)
        print(message)

# 等待5秒
time.sleep(5)

# 打包目录为tar.xz文件
tar_filename = f"{data_directory}.tar.xz"

# 使用序号重命名，不覆盖已存在的文件
index = 1
while os.path.exists(f"{tar_filename[:-7]}_{index:02d}.tar.xz"):
    index += 1

shutil.make_archive(f"{data_directory}_{index:02d}", 'xztar', data_directory)

# 打印日志和消息
logging.info(f"目录 {data_directory} 已打包为 {tar_filename}")
print(f"目录 {data_directory} 已打包为 {tar_filename}")