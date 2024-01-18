#! python
# -*- coding: UTF-8 -*-
'''
项目名称:python 
文件名:获取沪深市场A股最新状况优化版y1.py
生成时间:2023/07/28 上午01:52:00
创建用户:musk	
AIEPN Inc
'''
import efinance as ef
import os
from datetime import datetime

def save_to_csv(data, file_name):
    # 处理重名文件，避免覆盖
    file_index = 1
    base_name, ext = os.path.splitext(file_name)
    while True:
        if not os.path.exists(file_name):
            data.to_csv(file_name, encoding='utf-8-sig', index=None)
            print(f'数据已保存到 {file_name}')
            break
        file_name = f"{base_name}_{file_index:02d}{ext}"
        file_index += 1

def main():
    # 获取沪深市场 A 股最新状况
    realtime_quotes = ef.stock.get_realtime_quotes()

    # 获取当前本地日期
    local_date = datetime.now().strftime("%Y%m%d")

    # 生成保存数据的目录
    data_dir = "./data/A股每日状况/"
    if not os.path.exists(data_dir):
        os.makedirs(data_dir)

    # 保存数据到 CSV 文件
    file_name = os.path.join(data_dir, f"{local_date}_A股最新状况.csv")
    save_to_csv(realtime_quotes, file_name)

if __name__ == "__main__":
    main()
