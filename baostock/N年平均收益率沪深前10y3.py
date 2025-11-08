#! python
# -*- coding: UTF-8 -*-
'''
项目名称:pro 
文件名:N年平均收益率沪深前10y3.py
生成时间:2025/11/8 14:28:59
创建用户:x	
AIEPN Inc
原作者: http://baostock.com/api/static/pdf/过去3年证券公司的年平均收益率.pdf
'''
import baostock as bs
import pandas as pd
import matplotlib.pyplot as plt
import math
import os


def get_closeprice(code, start_date='2015-01-05', end_date='2024-12-31'):
    """获取指定股票在指定区间的开盘价与收盘价"""
    rs = bs.query_history_k_data_plus(
        code,
        "date,open,close",
        start_date=start_date,
        end_date=end_date,
        frequency="d",
        adjustflag="3"  # 3: 不复权, 1: 前复权, 2: 后复权
    )

    data_list = []
    while (rs.error_code == '0') and rs.next():
        data_list.append(rs.get_row_data())

    if not data_list:
        return pd.DataFrame()

    df = pd.DataFrame(data_list, columns=rs.fields)
    df['code'] = code
    return df


def get_unique_filename(base_name, ext):
    """生成唯一文件名（禁止覆盖）"""
    file_path = f"{base_name}{ext}"
    counter = 1
    while os.path.exists(file_path):
        file_path = f"{base_name}_{counter:02d}{ext}"
        counter += 1
    return file_path


def save_csv_no_overwrite(df, base_name):
    """保存 CSV 文件，禁止覆盖"""
    csv_path = get_unique_filename(base_name, ".csv")
    df.to_csv(csv_path, index=False, encoding="utf-8-sig")
    print(f"✅ CSV 已保存: {csv_path}")
    return csv_path


def save_plot_no_overwrite(fig, base_name):
    """保存 PNG 图表，禁止覆盖"""
    png_path = get_unique_filename(base_name, ".png")
    fig.savefig(png_path, dpi=300, bbox_inches='tight')
    print(f"✅ 图表已保存: {png_path}")
    plt.close(fig)
    return png_path


def compute_Avg_EarningRate():
    """计算前10只股票的平均年化收益率并生成图表/CSV"""
    lg = bs.login()
    print('login respond error_code:' + lg.error_code)
    print('login respond  error_msg:' + lg.error_msg)

    # 获取股票列表
    rs = bs.query_stock_basic()
    stock_list = []
    while (rs.error_code == '0') and rs.next():
        stock_list.append(rs.get_row_data()[0])

    # 只取前10只测试
    stock_list = stock_list[:10]
    print(f"📊 将分析 {len(stock_list)} 只股票：{stock_list}")

    start_date = '2015-01-05'
    end_date = '2024-12-31'

    result = pd.DataFrame()

    for code in stock_list:
        df = get_closeprice(code, start_date, end_date)
        if df.empty:
            print(f"⚠️ {code} 无数据，跳过")
            continue

        df = df.dropna(subset=['open', 'close'])
        df['open'] = df['open'].astype(float)
        df['close'] = df['close'].astype(float)

        open_price = df.iloc[0]['open']
        close_price = df.iloc[-1]['close']
        years = (pd.to_datetime(end_date) - pd.to_datetime(start_date)).days / 365.25
        avg_rate = math.pow(close_price / open_price, 1 / years) - 1

        result = pd.concat([result, pd.DataFrame([[code, open_price, close_price, avg_rate]],
                                                 columns=['code', 'open', 'close', 'avgEarningRate'])],
                           ignore_index=True)

    if result.empty:
        print("⚠️ 未获取到任何有效股票数据。")
        bs.logout()
        return

    # 排序
    result = result.sort_values(by=['avgEarningRate'], ascending=False)

    # 保存 CSV（禁止覆盖）
    csv_path = save_csv_no_overwrite(result, "./Avg_Earning_Rate_data")

    # 绘图
    fig, ax = plt.subplots(figsize=(10, 5))
    bars = ax.bar(result['code'], result['avgEarningRate'])

    # 在柱子上显示百分比
    for bar in bars:
        height = bar.get_height()
        ax.text(bar.get_x() + bar.get_width()/2, height,
                f"{height*100:.2f}%", ha='center', va='bottom', fontsize=10)

    ax.set_title(f'Average Annualized Return ({start_date} - {end_date})')
    ax.set_xlabel('Stock Code')
    ax.set_ylabel('Annualized Return')
    plt.xticks(rotation=45)
    plt.tight_layout()

    # 保存图表 PNG（禁止覆盖）
    save_plot_no_overwrite(fig, "./Avg_Earning_Rate_chart")

    bs.logout()


if __name__ == '__main__':
    compute_Avg_EarningRate()
