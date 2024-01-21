#! python
# -*- coding: UTF-8 -*-
'''
项目名称:python 
集成开发环境:PyCharm
文件名:批量获取行情数据y1.py
生成时间:2024-01-21 上午 11:13:52
创建用户:musk	
AIEPN Inc
'''
import baostock as bs
import pandas as pd
import os
import logging
from datetime import datetime

def configure_logging(log_file):
    # 配置日志记录
    os.makedirs(os.path.dirname(log_file), exist_ok=True)  # 创建日志文件所在目录
    logging.basicConfig(filename=log_file, level=logging.INFO, format='%(asctime)s - %(levelname)s - %(message)s')


def create_data_directory(data_dir):
    # 如果数据目录不存在，则递归创建
    os.makedirs(data_dir, exist_ok=True)

def get_stock_data(stock_code, start_date, end_date, data_dir):
    # 查询股票数据
    rs = bs.query_history_k_data_plus(stock_code,
                                       "date,time,code,open,high,low,close,volume,amount,adjustflag",
                                       start_date=start_date, end_date=end_date,
                                       frequency="5", adjustflag="3")

    # 记录查询结果
    logging.info(f'查询股票 {stock_code} 数据 - 响应错误代码: {rs.error_code}, 错误信息: {rs.error_msg}')

    data_list = []
    while (rs.error_code == '0') and rs.next():
        data_list.append(rs.get_row_data())

    if data_list:
        result = pd.DataFrame(data_list, columns=rs.fields)

        # 生成保存文件路径，检查是否已存在，若存在则添加序号
        count = 1
        save_path = os.path.join(data_dir, f'{stock_code}.csv')
        while os.path.exists(save_path):
            save_path = os.path.join(data_dir, f'{stock_code}_{count:02d}.csv')
            count += 1

        # 保存数据为 CSV 文件
        result.to_csv(save_path, index=False)

        # 打印成功信息
        print(f'成功获取股票 {stock_code} 的数据。保存在: {save_path}')
        logging.info(f'成功获取股票 {stock_code} 的数据。保存在: {save_path}')
    else:
        print(f'股票 {stock_code} 没有可用数据')
        logging.warning(f'股票 {stock_code} 没有可用数据')

def batch_get_stock_data(file_path, start_date, end_date):
    current_time = datetime.now()
    current_time_str = current_time.strftime('%Y-%m-%d')
    data_dir = f'./data/{current_time_str}'
    log_file = f'{data_dir}/{current_time_str}.txt'

    # 配置日志和数据目录
    configure_logging(log_file)
    create_data_directory(data_dir)

    with open(file_path, 'r') as file:
        stock_codes = file.read().splitlines()

    for stock_code in stock_codes:
        # 批量获取数据
        get_stock_data(stock_code, start_date, end_date, data_dir)

# 登陆系统
lg = bs.login()
print(f'登陆响应 错误代码: {lg.error_code}, 错误信息: {lg.error_msg}')

# 批量获取数据
batch_get_stock_data("a240118-baostock.txt", start_date='1999-07-26', end_date='2024-01-19')

# 登出系统
bs.logout()
