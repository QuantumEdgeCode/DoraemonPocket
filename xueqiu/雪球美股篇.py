#! python
# -*- coding: UTF-8 -*-
'''
项目名称:python 
文件名:雪球美股篇.py
生成时间:2023/12/8 11:23:48
创建用户:musk	
AIEPN Inc
'''
import requests
import pandas as pd

# Corrected headers without leading whitespaces
header1 = """
Accept: */*
Accept-Encoding: gzip, deflate, br
Accept-Language: zh-CN,zh;q=0.9,en-US;q=0.8,en;q=0.7
cache-control: no-cache
Connection: keep-alive
Cookie: xq_a_token=a97fa15a5bb947c53ed434a6c0364dd03f36962c; xqat=a97fa15a5bb947c53ed434a6c0364dd03f36962c; xq_r_token=457987e3f3df9d22b53ad50b975087ff84ee9a79; xq_id_token=eyJ0eXAiOiJKV1QiLCJhbGciOiJSUzI1NiJ9.eyJ1aWQiOi0xLCJpc3MiOiJ1YyIsImV4cCI6MTcwNDU4NzkwNCwiY3RtIjoxNzAyMDAzOTk2MjQ3LCJjaWQiOiJkOWQwbjRBWnVwIn0.jOjP14HxnuCnyj3gTbUlKFfZg6t2IHgScCOsfd_vkyGaZbPJ3wapoLeYwRln0p8rfPe9OrJjfNXmVZDcYeax269DjnJOzyyZTXHgcnpZE-NvnviybFrqqGMQzEHLKm1MSp2q_71ZA3kExaEJ8YiGVmWA1GuC3JfHF7AOrSwcNAIP0T2B7FO120rnTo9pzeM1gBlGTkGK_GZydtWwePfl20AnVnRVLWT2UWWNXCZJm7ozsffVraLs_NNBDuVDFbTVslmkfbHWCiEcn706T7NwoNrxoBPl60ehGE14EJBNgT2eAy82-eB8EhHhOHZ1fbnY4WKPJLymhldSURlKXcLBCQ; cookiesu=761702004022937; u=761702004022937; device_id=9049210388405eff956787f83abdb0ac; s=b718wlkfa9
Host: xueqiu.com
Referer: https://xueqiu.com/hq
sec-ch-ua: "Google Chrome";v="105", "Not)A;Brand";v="8", "Chromium";v="105"
sec-ch-ua-mobile: ?0
sec-ch-ua-platform: "macOS"
Sec-Fetch-Dest: empty
Sec-Fetch-Mode: cors
Sec-Fetch-Site: same-origin
User-Agent: Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/105.0.0.0 Safari/537.36
X-Requested-With: XMLHttpRequest
"""

# Convert to dict format
header2 = {line.split(': ', 1)[0]: line.split(': ', 1)[1] for line in header1.strip().split('\n')}

# Request URL
url = "https://xueqiu.com/service/v5/stock/screener/quote/list?page=2&size=90&order=desc&orderby=percent&order_by=percent&market=US&type=us&_"

# Send the request
resp = requests.get(url, headers=header2)

# Parse JSON data
json_data = resp.json()
data_list = json_data['data']['list']

# Initialize empty lists for data storage
symbol_list = []  # 股票代码
name_list = []  # 股票名称
current_list = []  # 当前价
chg_list = []  # 涨跌额
percent_list = []  # 涨跌幅
current_year_percent_list = []  # 年初至今
volume_list = []  # 成交量
amount_list = []  # 成交额
turnover_rate_list = []  # 换手率
pe_ttm_list = []  # 市盈率
dividend_yield_list = []  # 股息率
market_capital_list = []  # 市值

# Append data to lists
for count, data in enumerate(data_list, start=1):
    symbol_list.append(data['symbol'])
    name_list.append(data['name'])
    current_list.append(data['current'])
    chg_list.append(data['chg'])
    percent_list.append(data['percent'])
    current_year_percent_list.append(data['current_year_percent'])
    volume_list.append(data['volume'])
    amount_list.append(data['amount'])
    turnover_rate_list.append(data['turnover_rate'])
    pe_ttm_list.append(data['pe_ttm'])
    dividend_yield_list.append(data['dividend_yield'])
    market_capital_list.append(data['market_capital'])
    print('已爬取第{}只股票，股票代码：{}，股票名称：{}'.format(count, data['symbol'], data['name']))

# Create a DataFrame
df = pd.DataFrame(
    {
        '股票代码': symbol_list,
        '股票名称': name_list,
        '当前价': current_list,
        '涨跌额': chg_list,
        '涨跌幅': percent_list,
        '年初至今': current_year_percent_list,
        '成交量': volume_list,
        '成交额': amount_list,
        '换手率': turnover_rate_list,
        '市盈率': pe_ttm_list,
        '股息率': dividend_yield_list,
        '市值': market_capital_list,
    }
)

# Save data to CSV
df.to_csv('stock_us_data.csv', index=False)

print('爬取并保存数据完成！')

