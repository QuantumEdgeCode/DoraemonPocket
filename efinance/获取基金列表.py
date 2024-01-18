#! python
# -*- coding: UTF-8 -*-
'''
项目名称:python 
集成开发环境:PyCharm
文件名:获取基金列表.py
生成时间:2023-08-04 上午 11:45
创建用户:moss
AIEPN Inc
'''
import efinance as ef
import pandas as pd
from datetime import datetime

# 获取全部基金信息(数据类型是 pandas.DataFrame)
fund_codes = ef.fund.get_fund_codes()
# 筛选场内基金的关键词(场内基金简称带 ETF)
key = 'ETF'
condition = fund_codes['基金简称'].apply(lambda x: key.lower() in str(x).lower())
# 选出基金简称含有 key 的基金(数据类型是 pandas.DataFrame)
targets = fund_codes[condition]

# 获取当前日期
date = datetime.now().strftime('%Y%m%d')

# 设置文件名
filename = f'基金列表-{date}.csv'

# 检查文件是否存在，如果存在则添加序号
i = 1
while pd.io.common.file_exists(filename):
    filename = f'1-{date}-{i:02d}.csv'
    i += 1

# 保存数据到 CSV 文件
targets.to_csv(filename, index=False)

# 输出结果
print(f'数据已保存到 {filename}')
