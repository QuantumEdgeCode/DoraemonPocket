#! python
# -*- coding: UTF-8 -*-
'''
项目名称:python 
文件名:当日股票单子流入数据分钟级.py
生成时间:2023/12/7 19:36:26
创建用户:musk	
AIEPN Inc
'''
# import efinance as ef
# ef.stock.get_today_bill('300750')
import efinance as ef

# 获取当日资金流向
result = ef.stock.get_today_bill('300750')

# 打印结果
print(result)
