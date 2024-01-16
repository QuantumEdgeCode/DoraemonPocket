#! python
# -*- coding: UTF-8 -*-
'''
项目名称:python 
文件名:xueqiuus-y2b.py
生成时间:2023/12/8 12:02:36
创建用户:musk	
AIEPN Inc
将获取的数据保存为1个csv文件
后续添加打印显示爬取页数
'''
import os
import requests
import pandas as pd
from datetime import datetime

# Corrected headers without leading whitespaces
header1 = """
Accept: */*
Accept-Encoding: gzip, deflate, br
Accept-Language: zh-CN,zh;q=0.9,en-US;q=0.8,en;q=0.7
cache-control: no-cache
Connection: keep-alive
Cookie: xq_a_token=4418c7deafa5b566e73966d73045c92752601c18; xqat=4418c7deafa5b566e73966d73045c92752601c18; xq_r_token=3400e25ce554d6c07b8d65bb450064b80039b8a7; xq_id_token=eyJ0eXAiOiJKV1QiLCJhbGciOiJSUzI1NiJ9.eyJ1aWQiOi0xLCJpc3MiOiJ1YyIsImV4cCI6MTcwNzE4MDMzNSwiY3RtIjoxNzA1MzU4ODkyMzAzLCJjaWQiOiJkOWQwbjRBWnVwIn0.mPqO1jN7VQdxYrds158ZrUZt4PCZLvLjilcMMXI6moivFyo49RzHjc8vwP8NjxTpxCctdccawbiNTNGkqYNMJXO5nfPf9tAYZKxwkaJABu2SMIdUU_1dgnUDsQ2r8EjwcoXlI-CxLyCNPsk4HAtzjCAY392tHxL2y6Di586Sq6rP5-X5wIASTJy3Hgiqiqv6YGgJoHlMeGxvbag6pvMH1ZMXvnrvJFDnEZrEO1twPz-Rqfxpl9Lcda9-pR7UOMx1KIYlBTf_JMrFNo3JixtCry-e4cbJrfGmjRk9K3hqrjSfV0HOoRMfJrZMylBPcq03xu8BL5hkPZbMC4O7CJneMA; cookiesu=651705358948986; u=651705358948986; device_id=9049210388405eff956787f83abdb0ac; s=b718wlkfa9
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

# Set the number of pages to fetch
total_pages = 56

# Request base URL
base_url = "https://xueqiu.com/service/v5/stock/screener/quote/list?page=2&size=90&order=desc&orderby=percent&order_by=percent&market=US&type=us&_"

# Create data directory if it doesn't exist
data_directory = "./data/us"
os.makedirs(data_directory, exist_ok=True)

# Generate the current date in the specified format
current_date = datetime.now().strftime("%Y-%m-%d")

# Initialize an empty list to store dataframes
dfs = []

for page_number in range(1, total_pages + 1):
    # Generate the URL for the current page
    url = base_url.format(page_number)

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
    # Append the dataframe to the list
    dfs.append(df)

# Concatenate all dataframes into one
final_df = pd.concat(dfs, ignore_index=True)

# Create the file name with the specified format and numbering
file_name = f"stock-{current_date}.csv"
file_path = os.path.join(data_directory, file_name)

# Check if the file already exists and rename if needed
count = 1
while os.path.exists(file_path):
    count += 1
    file_name = f"stock-{current_date}_{count:02d}.csv"
    file_path = os.path.join(data_directory, file_name)

# Save the combined dataframe to a CSV file
final_df.to_csv(file_path, index=False)

print(f'Data saved to {file_path}')
