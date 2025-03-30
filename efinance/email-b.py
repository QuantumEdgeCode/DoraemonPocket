#! python
# -*- coding: UTF-8 -*-
'''
项目名称:pro 
文件名:email-b.py
生成时间:2025/3/2 13:30:51
创建用户:so-me	
AIEPN Inc
'''
import smtplib
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText
import logging
import json
from datetime import datetime
import sys

# 配置日志记录，避免乱码
logging.basicConfig(
    filename="email_log.txt",
    level=logging.INFO,
    format="%(asctime)s - %(levelname)s - %(message)s",
    encoding="utf-8",
)

# 读取 SMTP 和邮件配置信息
try:
    with open("config.json", encoding="utf-8") as config_file:
        config = json.load(config_file)
except Exception as e:
    logging.error("无法加载配置文件: %s", str(e))
    sys.exit("错误: 无法加载 config.json 文件")

def send_email(subject, message):
    """
    发送邮件
    :param subject: 邮件主题
    :param message: 邮件正文
    """
    sender_email = config.get("sender_email")
    sender_password = config.get("sender_password")
    sender_name = config.get("sender_name", sender_email)
    recipient_email = config.get("recipient_email")

    if not sender_email or not sender_password or not recipient_email:
        logging.error("邮件发送失败: 配置文件缺少必要字段")
        sys.exit("错误: 配置文件缺少必要字段")

    recipients = recipient_email.split(",")  # 支持多个收件人
    start_time = datetime.now()
    server = None

    try:
        # 创建邮件对象
        msg = MIMEMultipart()
        msg["From"] = f"{sender_name} <{sender_email}>"
        msg["To"] = ", ".join(recipients)
        msg["Subject"] = subject
        msg.attach(MIMEText(message, "plain", "utf-8"))

        # 连接 SMTP 服务器并发送邮件
        server = smtplib.SMTP(config["smtp_server"], config["smtp_port"])
        server.starttls()  # 启用 TLS 加密
        server.login(sender_email, sender_password)  # 登录邮箱

        server.sendmail(sender_email, recipients, msg.as_string())  # 发送邮件
        end_time = datetime.now()
        send_duration = (end_time - start_time).total_seconds()

        # 记录成功日志
        log_message = (
            f"邮件发送成功 (耗时 {send_duration:.2f} 秒):\n"
            f"SMTP 服务器: {config['smtp_server']}  端口: {config['smtp_port']}\n"
            f"收件人: {', '.join(recipients)}\n"
            f"主题: {subject}"
        )
        logging.info(log_message)
        print("✅ 邮件已成功发送:", ", ".join(recipients))
    except smtplib.SMTPException as smtp_error:
        logging.error("SMTP 错误: %s", str(smtp_error))
        print("❌ 邮件发送失败 (SMTP 错误):", str(smtp_error))
    except Exception as e:
        logging.error("邮件发送失败: %s", str(e))
        print("❌ 邮件发送失败:", str(e))
    finally:
        if server:
            server.quit()

def main():
    """
    主函数，从命令行参数接收邮件主题和正文并发送邮件
    """
    if len(sys.argv) != 3:
        print("用法: python email-b.py '<主题>' '<内容>'")
        sys.exit(1)

    subject = sys.argv[1]
    message = sys.argv[2]

    send_email(subject, message)

if __name__ == "__main__":
    main()
