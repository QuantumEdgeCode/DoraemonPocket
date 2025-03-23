#! python
# -*- coding: UTF-8 -*-
'''
项目名称:pro 
文件名:屏幕二维码识别y2u.py
生成时间:2025/3/23 23:18:16
创建用户:so-me	
AIEPN Inc
'''
import os
import pyautogui
import cv2
import numpy as np
import keyboard
import json
from datetime import datetime
from urllib.parse import unquote
import hashlib

# 创建保存截图的目录，如果目录不存在
data_dir = './data'
if not os.path.exists(data_dir):
    os.makedirs(data_dir)

# 保存识别内容的 JSON 文件
json_file = os.path.join(data_dir, 'qr_codes.json')

# 如果 JSON 文件不存在，则创建并初始化
if not os.path.exists(json_file):
    with open(json_file, 'w', encoding='utf-8') as f:
        json.dump({}, f, ensure_ascii=False, indent=4)

# 截图函数
def capture_screen():
    # 捕获当前屏幕截图
    screenshot = pyautogui.screenshot()
    # 转换为 numpy 数组
    screenshot_np = np.array(screenshot)
    # 转换为 OpenCV 格式 (BGR)
    screenshot_bgr = cv2.cvtColor(screenshot_np, cv2.COLOR_RGB2BGR)
    return screenshot_bgr

# 识别二维码
def decode_qrcode(image):
    # 初始化二维码检测器
    detector = cv2.QRCodeDetector()
    # 使用 detectAndDecode 来解码二维码
    data, points, straight_qrcode = detector.detectAndDecode(image)
    if data:
        return data
    else:
        return None

# 对中文 URL 进行解码
def decode_url(url):
    try:
        # 解码 URL 中的中文字符
        decoded_url = unquote(url)
        return decoded_url
    except Exception as e:
        print("URL 解码错误:", e)
        return url

# 计算文件的 MD5 值
def calculate_md5(file_path):
    hash_md5 = hashlib.md5()
    with open(file_path, "rb") as f:
        for chunk in iter(lambda: f.read(4096), b""):
            hash_md5.update(chunk)
    return hash_md5.hexdigest()

# 保存截图和识别内容
def save_screenshot_and_data(screenshot, qr_data):
    # 使用时间戳生成文件名
    timestamp = datetime.now().strftime('%Y%m%d_%H%M%S')
    screenshot_filename = os.path.join(data_dir, f'{timestamp}.png')
    json_filename = os.path.join(data_dir, 'qr_codes.json')

    # 保存截图
    cv2.imwrite(screenshot_filename, screenshot)

    # 将路径中的反斜杠替换为正斜杠
    screenshot_filename = screenshot_filename.replace('\\', '/')

    # 解码 URL 中的中文字符
    decoded_qr_data = decode_url(qr_data)

    # 计算截图文件的 MD5 值
    screenshot_md5 = calculate_md5(screenshot_filename)

    # 获取当前时间作为入库时间
    entry_time = datetime.now().strftime('%Y-%m-%d %H:%M:%S')

    # 保存识别内容到 JSON 文件
    with open(json_filename, 'r+', encoding='utf-8') as f:
        data = json.load(f)
        data[timestamp] = {
            'screenshot': screenshot_filename,
            'qr_data': decoded_qr_data,
            'entry_time': entry_time,
            'screenshot_md5': screenshot_md5
        }
        # 将更新后的数据写回到文件
        f.seek(0)
        json.dump(data, f, ensure_ascii=False, indent=4)

# 监听快捷键并执行截屏和识别
def main():
    print("等待按下 Ctrl + F12 截取屏幕并识别二维码...")
    while True:
        if keyboard.is_pressed('ctrl+f12'):
            print("按下 Ctrl + F12，截取屏幕...")
            screenshot = capture_screen()
            qr_data = decode_qrcode(screenshot)
            if qr_data:
                # 解码 URL 如果识别到二维码
                print("识别到二维码内容:", decode_url(qr_data))
                save_screenshot_and_data(screenshot, qr_data)
            else:
                print("未识别到二维码。")
            # 防止重复触发
            keyboard.wait('ctrl+f12', suppress=True)

if __name__ == '__main__':
    main()

# import os
# import pyautogui
# import cv2
# import numpy as np
# import keyboard
# import json
# from datetime import datetime
# from urllib.parse import unquote
#
# # 创建保存截图的目录，如果目录不存在
# data_dir = './data'
# if not os.path.exists(data_dir):
#     os.makedirs(data_dir)
#
# # 保存识别内容的 JSON 文件
# json_file = os.path.join(data_dir, 'qr_codes.json')
#
# # 如果 JSON 文件不存在，则创建并初始化
# if not os.path.exists(json_file):
#     with open(json_file, 'w', encoding='utf-8') as f:
#         json.dump({}, f, ensure_ascii=False, indent=4)
#
# # 截图函数
# def capture_screen():
#     # 捕获当前屏幕截图
#     screenshot = pyautogui.screenshot()
#     # 转换为 numpy 数组
#     screenshot_np = np.array(screenshot)
#     # 转换为 OpenCV 格式 (BGR)
#     screenshot_bgr = cv2.cvtColor(screenshot_np, cv2.COLOR_RGB2BGR)
#     return screenshot_bgr
#
# # 识别二维码
# def decode_qrcode(image):
#     # 初始化二维码检测器
#     detector = cv2.QRCodeDetector()
#     # 使用 detectAndDecode 来解码二维码
#     data, points, straight_qrcode = detector.detectAndDecode(image)
#     if data:
#         return data
#     else:
#         return None
#
# # 对中文 URL 进行解码
# def decode_url(url):
#     try:
#         # 解码 URL 中的中文字符
#         decoded_url = unquote(url)
#         return decoded_url
#     except Exception as e:
#         print("URL 解码错误:", e)
#         return url
#
# # 保存截图和识别内容
# def save_screenshot_and_data(screenshot, qr_data):
#     # 使用时间戳生成文件名
#     timestamp = datetime.now().strftime('%Y%m%d_%H%M%S')
#     screenshot_filename = os.path.join(data_dir, f'{timestamp}.png')
#     json_filename = os.path.join(data_dir, 'qr_codes.json')
#
#     # 保存截图
#     cv2.imwrite(screenshot_filename, screenshot)
#
#     # 将路径中的反斜杠替换为正斜杠
#     screenshot_filename = screenshot_filename.replace('\\', '/')
#
#     # 解码 URL 中的中文字符
#     decoded_qr_data = decode_url(qr_data)
#
#     # 保存识别内容到 JSON 文件
#     with open(json_filename, 'r+', encoding='utf-8') as f:
#         data = json.load(f)
#         data[timestamp] = {
#             'screenshot': screenshot_filename,
#             'qr_data': decoded_qr_data
#         }
#         # 将更新后的数据写回到文件
#         f.seek(0)
#         json.dump(data, f, ensure_ascii=False, indent=4)
#
# # 监听快捷键并执行截屏和识别
# def main():
#     print("等待按下 Ctrl + F12 截取屏幕并识别二维码...")
#     while True:
#         if keyboard.is_pressed('ctrl+f12'):
#             print("按下 Ctrl + F12，截取屏幕...")
#             screenshot = capture_screen()
#             qr_data = decode_qrcode(screenshot)
#             if qr_data:
#                 # 解码 URL 如果识别到二维码
#                 print("识别到二维码内容:", decode_url(qr_data))
#                 save_screenshot_and_data(screenshot, qr_data)
#             else:
#                 print("未识别到二维码。")
#             # 防止重复触发
#             keyboard.wait('ctrl+f12', suppress=True)
#
# if __name__ == '__main__':
#     main()
