#2024/05/31 21:52:19 GMT+08:00
import os
import time
import logging
import efinance as ef
import pandas as pd
from datetime import datetime

def save_deal_detail_to_excel(quote_id, max_count, file_name, log_file):
    try:
        # 获取期货最新交易日成交明细
        deal_detail = ef.futures.get_deal_detail(quote_id, max_count)

        # 如果数据不为空，则保存为 Excel 文件
        if not deal_detail.empty:
            # 处理重名文件，避免覆盖
            file_index = 1
            base_name, ext = os.path.splitext(file_name)
            while True:
                if not os.path.exists(file_name):
                    deal_detail.to_excel(file_name, index=None)
                    log_msg = f'数据已保存到 {file_name}'
                    print(log_msg)
                    write_to_log(log_file, log_msg)
                    return True
                file_name = f"{base_name}_{file_index:02d}{ext}"
                file_index += 1
        else:
            log_msg = f"期货代码 {quote_id} 的成交明细数据为空，未保存文件。"
            print(log_msg)
            write_to_log(log_file, log_msg)
            return False

    except Exception as e:
        log_msg = f"获取期货代码 {quote_id} 的成交明细数据失败：{str(e)}"
        print(log_msg)
        write_to_log(log_file, log_msg)
        return False

def write_to_log(log_file, message):
    with open(log_file, "a") as file:
        current_time = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        file.write(f"[{current_time}] {message}\n")

def main():
    # 记录程序开始时间
    start_time = time.time()

    # 获取当前本地日期
    local_date = datetime.now().strftime("%Y-%m-%d")

    # 生成保存数据和日志的目录
    data_dir = f"./data/a/期货成交明细/{local_date}"
    if not os.path.exists(data_dir):
        os.makedirs(data_dir)

    # 日志文件名
    log_file = os.path.join(data_dir, f"{local_date}.txt")

    # 从 code.txt 读取期货代码
    code_file_path = "code-list/期货行情id241226.txt"
    with open(code_file_path, "r") as file:
        futures_codes = [line.strip() for line in file]

    # 初始化成功和失败的计数器
    success_count = 0
    failure_count = 0

    # 处理每个期货代码
    for futures_code in futures_codes:
        file_name = os.path.join(data_dir, f"{futures_code}.xlsx")
        if save_deal_detail_to_excel(futures_code, 1000000, file_name, log_file):
            success_count += 1
        else:
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

if __name__ == "__main__":
    main()
