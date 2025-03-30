import os
import time
import logging
import json
import efinance as ef
from datetime import datetime
import pandas as pd

# 获取当前日期信息
current_date = datetime.now().strftime("%Y%m%d")

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

# 写入数据到文件的函数
def write_data(df, code, fqt, directory, format):
    directory = os.path.join(directory, str(fqt))
    os.makedirs(directory, exist_ok=True)
    filename = f"{directory}/{code}.{format}"
    unique_filename = get_unique_filename(filename)

    if format == 'csv':
        df.to_csv(unique_filename, index=False, encoding='utf-8-sig')
    elif format == 'xlsx':
        df.to_excel(unique_filename, index=False)
    elif format == 'json':
        df.to_json(unique_filename, orient='records', force_ascii=False, indent=4)

    message = f"股票代码:{code} 的数据(复权方式:{fqt})已保存到文件 {os.path.basename(unique_filename)} 中"
    logging.info(message)
    print(message)

# 获取股票数据的函数
def get_stock_quote_with_retry(stock_code, klt=1, fqt=1, max_retries=3):
    retry_count = 0
    while retry_count < max_retries:
        try:
            df = ef.stock.get_quote_history(stock_code, klt=klt, fqt=fqt)
            return df
        except Exception as e:
            retry_count += 1
            message = f"股票代码 {stock_code} 数据获取失败(复权方式: {fqt})，正在重试 ({retry_count}/{max_retries})..."
            logging.warning(message)
            print(message)
            time.sleep(0)

    message = f"获取股票代码 {stock_code} 的数据失败(复权方式: {fqt})，达到最大重试次数"
    logging.error(message)
    return None

# 读取配置文件
with open('code-list/config-1min.json', 'r', encoding='utf-8') as config_file:
    config_list = json.load(config_file)

# 处理每个配置
for config in config_list:
    market_id = config['id']
    file_name = config['file_name']
    kit = config['kit']
    trading_market = config['trading_market']
    fqt = config['fqt']
    save_format = config['save_format']

    file_path = f"./{file_name}"
    if not os.path.exists(file_path):
        file_path_in_code_list = f"./code-list/{file_name}"
        if os.path.exists(file_path_in_code_list):
            file_path = file_path_in_code_list
        else:
            message = f"找不到文件: {file_name}，跳过 ID: {market_id}"
            logging.warning(message)
            print(message)
            continue

    with open(file_path, "r", encoding="utf-8") as file:
        stock_codes = [line.strip() for line in file]

    kit_directory_map = {
        "101": "day", "102": "zhou", "103": "yue", "104": "3mo", "105": "6mo", "106": "year",
        "1": "1分钟级别", "5": "5分钟级别", "30": "30分钟级别", "60": "60分钟级别"
    }

    if kit not in kit_directory_map:
        raise ValueError("不支持的kit值，请输入101、102、103、104、105、106、1、5、30、60")

    base_data_directory = f"./data/{trading_market}/{kit_directory_map[kit]}/{current_date}"
    os.makedirs(base_data_directory, exist_ok=True)

    log_filename = f"{base_data_directory}/{current_date}_log.txt"
    log_format = '%(asctime)s - %(levelname)s - %(message)s'
    root_logger = logging.getLogger()
    if root_logger.hasHandlers():
        for handler in root_logger.handlers[:]:
            handler.close()
            root_logger.removeHandler(handler)

    logging.basicConfig(filename=log_filename, level=logging.INFO, format=log_format)

    start_time = time.time()
    success_count = 0
    failure_count = 0
    fqt_list = [0, 1, 2] if fqt == 'max' else [int(fqt)]

    for stock_code in stock_codes:
        try:
            for fqt_value in fqt_list:
                retries = 1 if fqt != 'max' else 3
                df = get_stock_quote_with_retry(stock_code, klt=int(kit), fqt=fqt_value, max_retries=retries)
                if df is not None:
                    write_data(df, stock_code, fqt_value, base_data_directory, save_format)
                    success_count += 1
                else:
                    message = f"股票代码 {stock_code} 的数据获取失败(复权方式: {fqt_value})，将跳过并自动获取下一个代码。"
                    logging.warning(message)
                    print(message)
                    failure_count += 1
        except Exception as e:
            message = f"处理股票代码 {stock_code} 时发生错误: {str(e)}"
            logging.error(message)
            print(message)
            failure_count += 1

    end_time = time.time()
    total_time = end_time - start_time

    print(f"市场ID: {market_id} 执行时间: {total_time:.2f} 秒")
    print(f"获取成功: {success_count}")
    print(f"获取失败: {failure_count}")

    logging.info(f"市场ID: {market_id} 执行时间: {total_time:.2f} 秒")
    logging.info(f"获取成功: {success_count}")
    logging.info(f"获取失败: {failure_count}")

    time.sleep(5)
