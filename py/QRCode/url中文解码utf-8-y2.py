import os
import cv2
from urllib.parse import unquote
import csv
from datetime import datetime

# 检查并创建目录和文件
data_dir = './data'
csv_file_path = os.path.join(data_dir, 'result.csv')

# 如果 CSV 文件已存在，尝试生成一个新的文件名
csv_counter = 1
while os.path.exists(csv_file_path):
    csv_counter += 1
    csv_file_path = os.path.join(data_dir, f'result_{csv_counter:02d}.csv')

with open(csv_file_path, 'w', newline='', encoding='utf-8') as csv_file:
    csv_writer = csv.writer(csv_file)
    csv_writer.writerow(['image', 'url', '解码内容'])

# 获取图片文件列表
image_files = [file for file in os.listdir(data_dir) if file.lower().endswith(('.png', '.jpg', '.PNG', '.JPG'))]

# 遍历图片文件
for image_file in image_files:
    image_path = os.path.join(data_dir, image_file)

    # 读取图像
    img = cv2.imread(image_path)

    # 初始化 QRCodeDetector
    detector = cv2.QRCodeDetector()

    # 检测和解码 QRCode
    retval, decoded_info, points, straight_qrcode = detector.detectAndDecodeMulti(img)

    # 输出调试信息
    print(f"Processing image: {image_file}")

    # 如果成功解码
    if retval:
        # 提取字符串
        decoded_info_str = decoded_info[0] if isinstance(decoded_info, tuple) else decoded_info

        # 解码 URL 编码的部分
        decoded_url = unquote(decoded_info_str, encoding='utf-8')

        # 打印到控制台
        print(f"image: {image_file} URL: {decoded_info_str} 解码内容: {decoded_url}")

        # 写入实时日志文件
        log_file_path = os.path.join(data_dir, f'{datetime.now().strftime("%Y%m%d%H%M%S")}.txt')
        with open(log_file_path, 'a', encoding='utf-8') as log_file:
            log_file.write(f"image: {image_file} URL: {decoded_info_str} 解码内容: {decoded_url}\n")

        # 写入 CSV 文件
        with open(csv_file_path, 'a', newline='', encoding='utf-8') as csv_file:
            csv_writer = csv.writer(csv_file)
            csv_writer.writerow([image_file, decoded_info_str, decoded_url])
    else:
        print(f"No QR Code detected in image: {image_file}")
