#! python
# -*- coding: UTF-8 -*-
'''
项目名称:python 
集成开发环境:PyCharm
文件名:索引csv并链接html-y2.py
生成时间:2024-02-03 22:32:53
创建用户:musk	
AIEPN Inc
'''
import os

def generate_html(csv_filepath, stock_code, stock_name):
    # 读取HTML模板
    with open('template_file2.html', 'r', encoding='utf-8') as template_file:
        html_content = template_file.read()

    # 替换CSV文件路径变量
    html_content = html_content.replace('{filename}.csv', csv_filepath)

    # 替换股票代码和名称
    html_content = html_content.replace('{股票代码}', stock_code)
    html_content = html_content.replace('{股票名称}', stock_name)

    # 构造HTML文件名
    html_filename = os.path.splitext(os.path.basename(csv_filepath))[0] + '.html'
    counter = 1

    # 处理重复的HTML文件名
    while os.path.exists(html_filename):
        html_filename = f"{os.path.splitext(os.path.basename(csv_filepath))[0]}_{counter:02d}.html"
        counter += 1

    # 打印文件路径及文件名
    print(f"Generating HTML file: {os.path.join(os.path.dirname(csv_filepath), html_filename)}")

    # 写入生成的HTML文件
    with open(os.path.join(os.path.dirname(csv_filepath), html_filename), 'w', encoding='utf-8') as html_file:
        html_file.write(html_content)

if __name__ == "__main__":
    # 指定CSV文件所在目录
    csv_directory = './'

    # 读取股票列表
    stock_list_file = '股票列表.csv'
    stock_list = {}
    with open(stock_list_file, 'r', encoding='gbk') as stock_file:
        for line in stock_file.readlines()[1:]:
            code, name = line.strip().split(',')
            stock_list[code] = name

    # 遍历目录下的CSV文件并生成对应的HTML文件
    for csv_file in os.listdir(csv_directory):
        if csv_file.endswith('.csv'):
            stock_code = os.path.splitext(csv_file)[0]
            stock_name = stock_list.get(stock_code, 'Unknown')
            generate_html(os.path.join(csv_directory, csv_file), stock_code, stock_name)
