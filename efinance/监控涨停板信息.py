
'''
- Python 版本 3.7 以及以上
- 安装依赖的库

pip install efinance>=0.4.2

有内存泄露，pycharm里运行，内存不断增长，初步定位在 get_snapshot_fast 部分

监控代码如下python
'''

import requests
import multitasking
import pandas as pd
from typing import List
from typing import Dict
import efinance as ef
from dataclasses import dataclass
from datetime import datetime
import rich
requests.packages.urllib3.util.ssl_.DEFAULT_CIPHERS = 'ALL:@SECLEVEL=1'

@dataclass()
class StockQuoteInfo:
    # * 股票代码
    stock_code: str
    # * 股票名称
    stock_name: str
    # * 行情时间
    dt: datetime
    # * 最新价
    price: float
    # * 涨停价
    top_price: float
    # * 跌停价
    bottom_price: float
    # * 最新涨停时间
    latest_zt_dt: datetime
    # * 最新非涨停时间
    latest_nzt_dt: datetime

    @property
    def zt_keep_seconds(self) -> int:
        """
        涨停保持秒数

        Returns
        -------
        int

        """
        return (self.latest_zt_dt - self.latest_nzt_dt).seconds


class Clock:
    def __init__(self) -> None:
        self.dt = datetime.now()

    def next(self) -> bool:
        """
        是否在 09:15:00 - 15:00:00

        Returns
        -------
        bool
        """
        dt = datetime.now()
        st = '09:15:00'
        et = '15:00:00'
        self.dt = dt
        return st <= dt.strftime('%H:%M:%S') <= et


def get_snapshot_fast(stock_codes: List[str]) -> Dict[str, pd.DataFrame]:
    """
    获取多只股票的最新行情快照

    Parameters
    ----------
    stock_codes : List[str]
        股票代码列表

    Returns
    -------
    Dict[str, DataFrame]
        股票代码为键，行情快照为值的字典
    """
    sns: Dict[str, pd.DataFrame] = {}

    @multitasking.task
    def start(stock_code: str) -> None:
        sns[stock_code] = ef.stock.get_quote_snapshot(stock_code)
    for stock_code in stock_codes:
        start(stock_code)
    multitasking.wait_for_tasks()
    return sns


@dataclass()
class Strategy:
    clock: Clock

    def __post_init__(self) -> None:
        self.stock_code_info: Dict[str, StockQuoteInfo] = {}

    def next(self) -> None:
        dt = self.clock.dt

        quotes = ef.stock.get_realtime_quotes()
        quotes.index = quotes['股票代码'].values
        quotes = quotes[quotes['涨跌幅'] != '-']
        # * 初步选出即将涨停的股票
        quotes = quotes[quotes['涨跌幅'] > 7]
        if len(quotes) == 0:
            return
        sns = get_snapshot_fast(quotes.index.values)
        for row in quotes.iloc:
            stock_code = row['股票代码']
            stock_name = row['股票名称']
            # * 最新行情快照
            sn = sns[stock_code]
            # * 涨停价
            top_price = sn['涨停价']
            # * 跌停价
            bottom_price = sn['跌停价']
            # * 最新价格
            current_price = sn['最新价']
            # * 上一次刷新时的行情
            pre_info = self.stock_code_info.get(stock_code)
            # * 该股是不是第一次被检测
            first = pre_info is None
            if first:
                pre_info = StockQuoteInfo(
                    stock_code=stock_code,
                    stock_name=stock_name,
                    dt=dt,
                    price=current_price,
                    top_price=top_price,
                    bottom_price=bottom_price,
                    latest_nzt_dt=dt,
                    latest_zt_dt=None)
                self.stock_code_info[stock_code] = pre_info
            buy_list = []
            for i in range(1, 6):
                buy_list.append(f'买 {i}: {sn[f"买{i}数量"]}')
            # * 买单情况
            buy_str = '\n'.join(buy_list)
            tip: str = None
            # * 检测是否刚涨停或者打开涨停
            if abs(top_price-current_price) <= 1e-2:
                # * 刚涨停则更新最新涨停时间
                if first or current_price > pre_info.price:
                    tip = ZT_TIP
                    pre_info.latest_zt_dt = dt
                # * 保持涨停则更新最新涨停时间
                elif current_price == pre_info.price:
                    tip = ZT_KEEP_TIP
                    pre_info.latest_zt_dt = dt
                # * 炸板后更新最新的不涨停时间
                else:
                    tip = ZT_BREAK_TIP
                    pre_info.latest_nzt_dt = dt

            # * 非涨停 更新价格
            else:
                pre_info.latest_nzt_dt = dt
            # * 不管有没有涨停均更新
            pre_info.price = current_price
            pre_info.dt = dt

            # * 在这里根据涨停状况做通知
            # * 如果需要推送到微信，可查看我写的 wechat_work 这个库
            # * 地址为 https://github.com/Micro-sheep/wechat_work
            if tip == ZT_TIP or (tip == ZT_KEEP_TIP and pre_info.zt_keep_seconds <= ZT_NOTICE_MAX_SECONDS):
                msg = f'股票代码: {stock_code}\n股票名称: {stock_name}\n🚀 封单情况 🚀\n{buy_str}\n🚀 {tip} 🚀\n🚀 涨停保持秒数: {pre_info.zt_keep_seconds} 🚀'
                rich.print(msg)


# * 是否为测试模式 如果是 True 则不管是否在 09:15:00 - 15:00:00 都会执行
# * 如果是 False 则只有在 09:15:00 - 15:00:00 才会执行
TEST_MODE = True

ZT_TIP = '刚涨停'
ZT_KEEP_TIP = '保持涨停'
ZT_BREAK_TIP = '涨停炸板'
# * 保持涨停通知超时时间 涨停保持秒数超过它则不做通知
ZT_NOTICE_MAX_SECONDS = 60

clock = Clock()
strategy = Strategy(clock)
while clock.next() or TEST_MODE:
    dt = clock.dt
    rich.print(f'[{dt.strftime("%m-%d %H:%M:%S")}] 刷新')
    strategy.next()
print('今日监控结束')
