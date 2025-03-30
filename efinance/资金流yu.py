#2024/6/10 22:42:19 GMT+08:00
import os
import time
from datetime import datetime
import logging
import efinance as ef
import pandas as pd
import json

# 设置日志配置
current_time = datetime.now()
current_time_str = current_time.strftime('%Y%m%d')
data_dir = f'./data/a/资金流/{current_time_str}'
log_file = f'{data_dir}/{current_time_str}.txt'

# 如果数据目录不存在，则创建
if not os.path.exists(data_dir):
    os.makedirs(data_dir)

# 配置日志记录
logging.basicConfig(filename=log_file, level=logging.INFO, format='%(asctime)s - %(levelname)s - %(message)s')

# 在控制台输出日志
console_handler = logging.StreamHandler()
console_handler.setLevel(logging.INFO)
logging.getLogger().addHandler(console_handler)

# 设置重试次数和间隔
MAX_RETRIES = 1
RETRY_INTERVAL = 1

# 读取股票代码列表
with open('code-list/a25328.txt', 'r', encoding='utf-8') as file:
    stock_codes = [line.strip() for line in file]

def save_data_to_json(data, file_name):
    try:
        # 处理重名文件，避免覆盖
        file_index = 1
        base_name, ext = os.path.splitext(file_name)
        while True:
            if not os.path.exists(file_name):
                with open(file_name, 'w', encoding='utf-8') as json_file:
                    json.dump(data, json_file, ensure_ascii=False, indent=4)
                logging.info(f'数据已保存到 {file_name}')
                break
            file_name = f"{base_name}_{file_index:02d}{ext}"
            file_index += 1
    except Exception as e:
        logging.error(f"保存数据到 {file_name} 时发生错误：{str(e)}")

# 记录程序开始时间
start_time = time.time()

# 初始化成功和失败的计数器
success_count = 0
failure_count = 0

# 循环获取数据
for code in stock_codes:
    retries = 0
    while retries < MAX_RETRIES:
        try:
            # 获取五档行情
            quote_snapshot = ef.stock.get_quote_snapshot(code).to_dict()
            
            # 检查五档行情数据是否为空
            if not quote_snapshot:
                message = f'股票代码 {code} 的五档行情数据为空。跳过保存。'
                logging.warning(message)
                print(message)
                break
            
            # 获取资金流数据
            history_bill = ef.stock.get_history_bill(code)
            if history_bill.empty:
                message = f'股票代码 {code} 的资金流数据为空。跳过保存。'
                logging.warning(message)
                print(message)
                break

            # 获取最新一条资金流数据
            latest_bill = history_bill.iloc[-1].to_dict()

            # 合并数据
            combined_data = {
                "quote_snapshot": quote_snapshot,
                "history_bill": latest_bill
            }
            
            # 生成保存文件路径
            file_name = os.path.join(data_dir, f'{code}.json')
            save_data_to_json(combined_data, file_name)

            # 增加成功计数
            success_count += 1
            break
        except Exception as e:
            logging.error(f'获取股票 {code} 的数据时发生错误：{str(e)}。重试中... (重试 {retries + 1}/{MAX_RETRIES})')

            # 等待重试间隔
            time.sleep(RETRY_INTERVAL)

            # 增加重试次数
            retries += 1
    else:
        # 打印错误信息，超过最大重试次数
        logging.error(f'在 {MAX_RETRIES} 次重试后无法获取股票 {code} 数据。继续下一个股票。')

        # 增加失败计数
        failure_count += 1

# 记录程序结束时间
end_time = time.time()
total_time = end_time - start_time

# 打印执行时间和统计信息
print(f"程序执行时间: {total_time:.2f} 秒")
print(f"获取成功的代码数量: {success_count}")
print(f"获取失败的代码数量: {failure_count}")

logging.info(f"程序执行时间: {total_time:.2f} 秒")
logging.info(f"获取成功的代码数量: {success_count}")
logging.info(f"获取失败的代码数量: {failure_count}")

# 记录完成信息
logging.info(f'数据获取完成。查看日志 {log_file} 获取详细信息。')
