#! python
# -*- coding: UTF-8 -*-
'''
项目名称:python 
集成开发环境:PyCharm
文件名:获取交易所产品状况y2-xls.py
生成时间:2024-02-01 22:00:57
创建用户:musk	
AIEPN Inc
'''
import os
import efinance as ef
from datetime import datetime

def save_realtime_quotes_to_excel(fs, data_dir):
    for market_scenario in fs:
        try:
            # 获取单个或多个市场行情的最新状况
            realtime_quotes = ef.stock.get_realtime_quotes(market_scenario)

            # 生成保存数据的文件路径
            if market_scenario is None:
                file_name = "沪深京A股市场行情.xlsx"
            else:
                file_name = f"{market_scenario}.xlsx"
            file_path = os.path.join(data_dir, file_name)

            # 处理重名文件，避免覆盖
            file_index = 1
            base_name, ext = os.path.splitext(file_path)
            while os.path.exists(file_path):
                file_path = f"{base_name}_{file_index:02d}{ext}"
                file_index += 1

            # 将数据保存为 Excel 文件，手动设置第一列列名为"股票代码"
            realtime_quotes.to_excel(file_path, index=False, sheet_name='Sheet1', columns=['股票代码'] + list(realtime_quotes.columns[1:]))
            print(f'数据已保存到 {file_path}')

        except KeyError as e:
            print(f"错误：{e}. 请确保参数 fs 中包含正确的行情类型。")

        except Exception as e:
            print(f"获取交易所产品状况数据失败：{str(e)}")

def main():
    # 获取当前本地日期
    local_date = datetime.now().strftime("%Y-%m-%d")

    # 生成保存数据的目录
    data_dir = f"./data/交易所产品状况/{local_date}"
    if not os.path.exists(data_dir):
        os.makedirs(data_dir)

    # 行情名称或者多个行情名列表
    # 这里示例为获取沪深A股市场行情和其他市场行情
    fs = [
        None, '沪深A股', '沪A', '深A', '北A', '可转债', '期货', '创业板', '美股',
        '港股', '中概股', '新股', '科创板', '沪股通', '深股通', '行业板块', '概念板块',
        '沪深系列指数', '上证系列指数', '深证系列指数', 'ETF', 'LOF', '英股'
    ]

    # 调用保存函数
    save_realtime_quotes_to_excel(fs, data_dir)

if __name__ == "__main__":
    main()


# import os
# import efinance as ef
# from datetime import datetime
#
# def save_realtime_quotes_to_excel(fs, data_dir):
#     for market_scenario in fs:
#         try:
#             # 获取单个或多个市场行情的最新状况
#             realtime_quotes = ef.stock.get_realtime_quotes(market_scenario)
#
#             # 生成保存数据的文件路径
#             if market_scenario is None:
#                 file_name = "沪深京A股市场行情.xls"
#             else:
#                 file_name = f"{market_scenario}.xls"
#             file_path = os.path.join(data_dir, file_name)
#
#             # 将数据保存为 Excel 文件，手动设置第一列列名为"股票代码"
#             realtime_quotes.to_excel(file_path, index=False, sheet_name='Sheet1', columns=['股票代码'] + list(realtime_quotes.columns[1:]))
#             print(f'数据已保存到 {file_path}')
#
#         except KeyError as e:
#             print(f"错误：{e}. 请确保参数 fs 中包含正确的行情类型。")
#
#         except Exception as e:
#             print(f"获取交易所产品状况数据失败：{str(e)}")
#
# def main():
#     # 获取当前本地日期
#     local_date = datetime.now().strftime("%Y-%m-%d")
#
#     # 生成保存数据的目录
#     data_dir = f"./data/交易所产品状况/{local_date}"
#     if not os.path.exists(data_dir):
#         os.makedirs(data_dir)
#
#     # 行情名称或者多个行情名列表
#     # 这里示例为获取沪深A股市场行情和其他市场行情
#     fs = [
#         None, '沪深A股', '沪A', '深A', '北A', '可转债', '期货', '创业板', '美股',
#         '港股', '中概股', '新股', '科创板', '沪股通', '深股通', '行业板块', '概念板块',
#         '沪深系列指数', '上证系列指数', '深证系列指数', 'ETF', 'LOF'
#     ]
#
#     # 调用保存函数
#     save_realtime_quotes_to_excel(fs, data_dir)
#
# if __name__ == "__main__":
#     main()
