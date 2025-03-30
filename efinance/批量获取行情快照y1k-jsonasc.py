import os
from datetime import datetime
import efinance as ef
import json

def save_snapshot_to_json(quote_id, snapshot, data_dir):
    try:
        # 生成保存文件的目录
        if not os.path.exists(data_dir):
            os.makedirs(data_dir)

        # 生成文件名
        file_name = os.path.join(data_dir, f"{quote_id}.json")

        # 处理重名文件，避免覆盖
        file_index = 1
        base_name, ext = os.path.splitext(file_name)
        while True:
            if not os.path.exists(file_name):
                with open(file_name, 'w', encoding='utf-8') as json_file:
                    json.dump(snapshot, json_file, ensure_ascii=False, indent=4)
                print(f'数据已保存到 {file_name}')
                break
            file_name = f"{base_name}_{file_index:02d}{ext}"
            file_index += 1

    except Exception as e:
        print(f"获取行情快照代码 {quote_id} 的行情快照数据失败：{str(e)}")

def main():
    # 获取当前本地日期
    local_date = datetime.now().strftime("%Y%m%d")

    # 生成保存数据的目录
    data_dir = f"./data/a/行情快照/{local_date}"

    # 从 code.txt 读取行情快照代码
    code_file_path = "code-list/a25328.txt"
    with open(code_file_path, "r") as file:
        futures_codes = [line.strip() for line in file]

    # 处理每个行情快照代码
    for futures_code in futures_codes:
        snapshot = ef.stock.get_quote_snapshot(futures_code)
        # 添加索引名称
        snapshot.index.name = '字段名'
        save_snapshot_to_json(futures_code, snapshot.to_dict(), data_dir)

if __name__ == "__main__":
    main()
