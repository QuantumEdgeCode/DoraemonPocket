import struct
import datetime
import os
'''
项目名称:python 
文件名:单个通达信导出英文标签.py
生成时间:2023/08/22 14:06:00
创建用户:moss
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
    file_object_path = get_available_filename(file_object_path)  # Get an available filename
    
    output_filename = os.path.basename(file_object_path)  # Get just the filename

    with open(filepath, 'rb') as f:
        with open(file_object_path, 'w+') as file_object:
            file_object.write("stock_date,stock_open,stock_high,stock_low,stock_close,stock_amount,stock_vol,stock_reserved\n")  # Write the header line
            
            while True:
                stock_data = f.read(32)  # Read 32 bytes for each day's data

                if not stock_data:
                    break
                
                # Unpack the fields based on their respective formats
                stock_date = struct.unpack("i", stock_data[0:4])[0] # 4字节 如20091229
                stock_open = struct.unpack("i", stock_data[4:8])[0] #开盘价*100
                stock_high = struct.unpack("i", stock_data[8:12])[0] #最高价*100
                stock_low = struct.unpack("i", stock_data[12:16])[0] #最低价*100
                stock_close = struct.unpack("i", stock_data[16:20])[0] #收盘价*100
                stock_amount = struct.unpack("f", stock_data[20:24])[0] #成交额
                stock_vol = struct.unpack("i", stock_data[24:28])[0] #成交量
                stock_reserved = struct.unpack("i", stock_data[28:32])[0] #保留值

                date_format = datetime.datetime.strptime(str(stock_date), '%Y%m%d')  # Format the date
                formatted_data = f"{date_format.strftime('%Y-%m-%d')},{stock_open/100},{stock_high/100},{stock_low/100},{stock_close/100},{stock_amount},{stock_vol},{stock_reserved}\n"
                
                file_object.writelines(formatted_data)
                count += 1

    print(f"{output_filename} 导出成功，共导出 {count} 条数据")

# 打开sh600028.day股票数据文件
stock_csv('./sh/sh600026.day', '1')
