#! python
# -*- coding: UTF-8 -*-
'''
项目名称:python 
文件名:沪深A股季度表现time.py
生成时间:2023/12/7 19:18:44
创建用户:musk	
AIEPN Inc
'''
import efinance as ef
import pandas as pd
import os
import datetime
import warnings

def save_performance_data_to_csv():
    # Suppress FutureWarning for the duration of this function
    warnings.simplefilter(action='ignore', category=FutureWarning)

    # 获取所有公司绩效数据
    performance_data = ef.stock.get_all_company_performance()

    # Reset the warning filter
    warnings.resetwarnings()

    # 获取当前日期和时间
    current_time = datetime.datetime.now().strftime("%Y-%m-%d")

    # 生成保存路径
    base_path = "./data/沪深A股季度表现/"
    os.makedirs(base_path, exist_ok=True)

    # 初始文件名
    initial_file_name = "沪深A股季度表现.csv"
    file_name = f"{current_time}_{initial_file_name}"
    file_path = os.path.join(base_path, file_name)

    # 如果文件已存在，则修改文件名
    file_index = 1
    while os.path.exists(file_path):
        file_name = f"{current_time}_{initial_file_name}_{file_index:02d}.csv"
        file_path = os.path.join(base_path, file_name)
        file_index += 1

    # 保存数据为CSV文件
    performance_data.to_csv(file_path, index=False)

    # 打印文件名及保存路径
    print(f"文件名: {file_name}")
    print(f"路径: {os.path.abspath(file_path)}")

# 执行函数
save_performance_data_to_csv()

#以上为无警告版本
# import efinance as ef
# import pandas as pd
# import os
# import datetime
#
# def save_performance_data_to_csv():
#     # 获取所有公司绩效数据
#     performance_data = ef.stock.get_all_company_performance()
#
#     # 获取当前日期和时间
#     current_time = datetime.datetime.now().strftime("%Y-%m-%d")
#
#     # 生成保存路径
#     base_path = "./data/沪深A股季度表现/"
#     os.makedirs(base_path, exist_ok=True)
#
#     # 初始文件名
#     initial_file_name = "沪深A股季度表现.csv"
#     file_name = f"{current_time}_{initial_file_name}"
#     file_path = os.path.join(base_path, file_name)
#
#     # 如果文件已存在，则修改文件名
#     file_index = 1
#     while os.path.exists(file_path):
#         file_name = f"{current_time}_{initial_file_name}_{file_index:02d}.csv"
#         file_path = os.path.join(base_path, file_name)
#         file_index += 1
#
#     # 保存数据为CSV文件
#     performance_data.to_csv(file_path, index=False)
#
#     # 打印文件名及保存路径
#     print(f"File saved: {file_name}")
#     print(f"Path: {os.path.abspath(file_path)}")
#
# # 执行函数
# save_performance_data_to_csv()
