#2024/05/31 22:23:01 GMT+08:00
import os
import time
from datetime import datetime
import logging
import efinance as ef
import pandas as pd

# 设置日志配置
current_time = datetime.now()
current_time_str = current_time.strftime('%Y-%m-%d')
data_dir = f'./data/a/股票单子流入/分钟级/{current_time_str}'
log_file = f'{data_dir}/{current_time_str}.txt'

# 如果数据目录不存在，则创建
if not os.path.exists(data_dir):
    os.makedirs(data_dir)

# 配置日志记录
logging.basicConfig(filename=log_file, level=logging.INFO, format='%(asctime)s - %(levelname)s - %(message)s')

# 设置每秒不超过3个请求的限制
REQUEST_LIMIT = 3

# 重试次数和间隔
MAX_RETRIES = 1
RETRY_INTERVAL = 1

# 读取股票代码列表
with open('code-list/a25328.txt', 'r', encoding='utf-8') as file:
    stock_codes = [line.strip() for line in file]

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
            # 获取当日资金流向
            result = ef.stock.get_today_bill(code)

            # 转换为 pandas DataFrame
            df = pd.DataFrame(result)

            # 检查数据是否为空
            if df.empty:
                message = f'股票代码 {code} 数据为空。跳过保存。'
                logging.warning(message)
                print(message)
                break

            # 生成保存文件路径，检查是否已存在，若存在则添加序号
            count = 1
            save_path = os.path.join(data_dir, f'{code}.xlsx')
            while os.path.exists(save_path):
                save_path = os.path.join(data_dir, f'{code}_{count:02d}.xlsx')
                count += 1

            # 保存数据为 XLSX 文件
            df.to_excel(save_path, index=False)

            # 打印成功信息
            message = f'成功获取股票 {code} 的数据。保存在： {save_path}'
            logging.info(message)
            print(message)

            # 增加成功计数
            success_count += 1

            # 重置重试次数
            retries = 0
            break
        except Exception as e:
            # 打印错误信息
            message = f'获取股票 {code} 数据时发生错误： {str(e)}。重试中... (重试 {retries + 1}/{MAX_RETRIES})'
            logging.error(message)
            print(message)

            # 等待重试间隔
            time.sleep(RETRY_INTERVAL)

            # 增加重试次数
            retries += 1
    else:
        # 打印错误信息，超过最大重试次数
        message = f'在 {MAX_RETRIES} 次重试后无法获取股票 {code} 数据。继续下一个股票。'
        logging.error(message)
        print(message)

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
print(f'数据获取完成。查看日志 {log_file} 获取详细信息。')
