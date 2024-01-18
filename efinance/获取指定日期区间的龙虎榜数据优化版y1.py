#! python
# -*- coding: UTF-8 -*-
'''
项目名称:python 
集成开发环境:PyCharm
文件名:获取指定日期区间的龙虎榜数据优化版y1.py
生成时间:2023-2024
创建用户:moss
AIEPN Inc
'''
import os
import efinance as ef

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
    # 获取指定日期区间的龙虎榜数据
    start_date = '2024-01-17'  # 开始日期
    end_date = '2024-01-17'    # 结束日期
    billboard_data = ef.stock.get_daily_billboard(start_date=start_date, end_date=end_date)

    # 保存数据到 CSV 文件
    file_name = f"{start_date}_{end_date}_龙虎榜.csv"
    save_to_csv(billboard_data, file_name)

if __name__ == "__main__":
    main()
