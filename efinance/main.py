#! python
# -*- coding: UTF-8 -*-
'''
项目名称:pro 
文件名:main.py
生成时间:2025/3/23 23:08:52
创建用户:so-me	
AIEPN Inc
'''
import os
import subprocess
import socket
import time
import sys
import schedule
import platform
from datetime import datetime
import argparse

# 配置脚本列表
scripts = [
    "获取行情K2u修正x1.py",
    "当日股票单子流入数据分钟级yu.py",
    "获取交易所产品状况yux.py",
    "获取交易所期货成交明细yu.py",
    "批量获取行情快照y1k-jsonasc.py",
    "资金流yu.py"
]
notification_script = "email-b.py"


# 显示使用说明
def show_usage():
    usage_text = """
使用方法：
------------------------------------------------------
1. 立即执行任务（默认方式）
   python main.py

2. 立即执行任务（显式参数）
   python main.py --immediate

3. 以定时任务方式运行（每天 9:00 以及每小时执行）
   python main.py --schedule

4. 显示帮助信息
   python main.py --help
------------------------------------------------------
"""
    print(usage_text)


# 执行脚本并记录结果
def execute_script(script):
    start_time = time.time()
    status = "Success"
    exception = None

    try:
        print(f"执行脚本: {script}")
        result = subprocess.run(["python", script], stdout=sys.stdout, stderr=sys.stderr, text=True)

        if result.returncode != 0:
            status = "Failed"
    except Exception as e:
        status = "Failed"
        exception = str(e)

    elapsed_time = time.time() - start_time
    return {
        "script": script,
        "status": status,
        "exception": exception,
        "elapsed_time": elapsed_time
    }


# 获取系统信息
def get_system_info():
    ip_address = socket.gethostbyname(socket.gethostname())
    return {"ip_address": ip_address}


# 发送邮件
def send_email(subject, body):
    try:
        subprocess.run(["python", notification_script, subject, body], check=True)
    except Exception as e:
        print(f"邮件发送失败: {e}")


# 任务执行逻辑
def run_tasks():
    start_time = datetime.now()
    print(f"\n===== 任务开始: {start_time} =====")

    results = [execute_script(script) for script in scripts]
    end_time = datetime.now()

    # 收集系统信息
    system_info = get_system_info()

    # 生成邮件内容
    subject = "脚本执行报告"
    body = f"执行时间: {start_time}\n\n脚本执行结果:\n"

    for result in results:
        body += f"  - {result['script']}: {result['status']} ({result['elapsed_time']:.2f} 秒)\n"
        if result['exception']:
            body += f"    异常: {result['exception']}\n"

    body += f"\n系统信息:\n  - IP 地址: {system_info['ip_address']}\n"

    print(f"\n===== 任务结束: {end_time} =====")

    # 发送执行结果邮件
    send_email(subject, body)

    # 等待 60 秒，然后执行关机
    print("等待 60 秒后执行关机...")
    time.sleep(60)
    shutdown_system()


# 调度任务
def schedule_tasks():
    # 每天 9:00 运行任务
    schedule.every().day.at("09:00").do(run_tasks)
    # 每小时执行一次
    schedule.every().hour.do(run_tasks)

    print("任务调度已启动, 正在等待执行...")
    while True:
        schedule.run_pending()
        time.sleep(1)


# 关机命令执行，根据操作系统
def shutdown_system():
    system_name = platform.system()
    print(f"正在执行关机操作，操作系统: {system_name}")

    try:
        if system_name == "Windows":
            subprocess.run(["shutdown", "/s", "/f", "/t", "0"], check=True)
        elif system_name in ["Linux", "Darwin", "CentOS", "Ubuntu", "Debian"]:  # 所有 Linux 发行版统一使用此命令
            subprocess.run(["shutdown", "-h", "now"], check=True)
        else:
            print(f"不支持的操作系统: {system_name}")
    except Exception as e:
        print(f"关机命令执行失败: {e}")


# 立即运行或定时执行
if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="定时执行 Python 脚本，并发送执行结果通知邮件")
    parser.add_argument("--immediate", action="store_true", help="立即执行任务")
    parser.add_argument("--schedule", action="store_true", help="定时执行任务（每天 9:00 和每小时执行一次）")

    args = parser.parse_args()

    if args.schedule:
        print("程序已启动：定时执行模式")
        schedule_tasks()
    else:
        print("程序已启动：立即执行模式")
        run_tasks()
