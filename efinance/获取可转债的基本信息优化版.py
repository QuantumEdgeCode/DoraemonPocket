'''
项目名称:python 
集成开发环境:PyCharm
文件名:获取可转债的基本信息优化版.py
生成时间:2023/08/03 下午10:29
创建用户:moss
AIEPN Inc
'''
import os
import efinance as ef
from datetime import datetime

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
    # 获取全部可转债的基本信息
    all_bonds_info = ef.bond.get_all_base_info()

    # 获取当前本地日期
    local_date = datetime.now().strftime("%Y%m%d")

    # 生成保存数据的目录
    data_dir = os.path.join(f"./data/可转债/")
    if not os.path.exists(data_dir):
        os.makedirs(data_dir)

    # 保存数据到 CSV 文件
    file_name = os.path.join(data_dir, f"{local_date}可转债信息.csv")
    save_to_csv(all_bonds_info, file_name)

if __name__ == "__main__":
    main()
