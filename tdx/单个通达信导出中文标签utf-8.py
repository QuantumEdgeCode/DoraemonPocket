import struct
import datetime
import os
'''
项目名称:python 
文件名:单个通达信导出中文标签utf-8.py
生成时间:2023/08/22 11:22:07
创建用户:musk	
AIEPN Inc
'''
def get_available_filename(filename):
    name, ext = os.path.splitext(filename)
    index = 1
    new_filename = filename
    while os.path.exists(new_filename):
        new_filename = f"{name}_{index:02d}{ext}"
        index += 1
    return new_filename

def stock_csv(filepath, name):
    data = []
    count = 0

    # 获取原day文件名的前缀
    filename = filepath.split('/')[-1]
    filename = filename.split('.')[0]
    
    # 构造导出的CSV文件名
    file_object_path = f'./data/{filename}.csv'
    
    original_filename = file_object_path
    file_object_path = get_available_filename(file_object_path)  # 获取可用的文件名
    
    output_filename = os.path.basename(file_object_path)  # 只获取文件名部分

    with open(filepath, 'rb') as f:
        with open(file_object_path, 'w+', encoding='utf-8') as file_object:  # 使用 utf-8 编码写入文件
            labels = "日期,开盘价,最高价,最低价,收盘价,成交额,成交量,保留值\n"  # 中文标签
            file_object.write(labels)  # 写入标题行
            
            while True:
                stock_data = f.read(32)  # 每天数据32字节

                if not stock_data:
                    break
                
                # 根据各字段的格式进行解包
                stock_date = struct.unpack("i", stock_data[0:4])[0] # 4字节 日期，如20091229
                stock_open = struct.unpack("i", stock_data[4:8])[0] #开盘价*100
                stock_high = struct.unpack("i", stock_data[8:12])[0] #最高价*100
                stock_low = struct.unpack("i", stock_data[12:16])[0] #最低价*100
                stock_close = struct.unpack("i", stock_data[16:20])[0] #收盘价*100
                stock_amount = struct.unpack("f", stock_data[20:24])[0] #成交额
                stock_vol = struct.unpack("i", stock_data[24:28])[0] #成交量
                stock_reserved = struct.unpack("i", stock_data[28:32])[0] #保留值

                date_format = datetime.datetime.strptime(str(stock_date), '%Y%m%d')  # 格式化日期
                formatted_data = f"{date_format.strftime('%Y-%m-%d')},{stock_open/100},{stock_high/100},{stock_low/100},{stock_close/100},{stock_amount},{stock_vol},{stock_reserved}\n"
                
                file_object.writelines(formatted_data)
                count += 1

    print(f"{output_filename} 导出成功，共导出 {count} 条数据")

# 打开sh600028.day股票数据文件
stock_csv('./sh/sh600026.day', '1')
