#! python
# -*- coding: UTF-8 -*-
'''
项目名称:pro 
文件名:数据压缩y3.py
生成时间:2025/3/30 18:40:07
创建用户:so-me	
AIEPN Inc
'''
import os
import tarfile
import json
import re
import shutil
import sys
import select
from tqdm import tqdm
import concurrent.futures

# 定义基本目录变量
BASE_DIR = "./data"
DATE_PATTERN = re.compile(r"\d{4}[-]?\d{2}[-]?\d{2}")  # 预编译正则表达式


def extract_date_from_dir_name(dir_name):
    """从目录名中提取日期部分"""
    match = DATE_PATTERN.search(dir_name)
    return match.group(0) if match else None


def create_unique_tarball_name(base_name, output_dir):
    """生成唯一压缩包名称"""
    counter = 1
    tarball_name = os.path.join(output_dir, f"{base_name}.tar.xz")
    while os.path.exists(tarball_name):
        tarball_name = os.path.join(output_dir, f"{base_name}_{counter:02d}.tar.xz")
        counter += 1
    return tarball_name


def compress_directory_to_tar_xz(directory, tarball_name):
    """优化后的压缩函数"""
    file_entries = []
    base_dir = os.path.abspath(directory)
    for root, _, files in os.walk(directory):
        for file in files:
            full_path = os.path.join(root, file)
            arcname = os.path.relpath(full_path, base_dir)
            file_entries.append((full_path, arcname))

    with tarfile.open(tarball_name, "w:xz", preset=1) as tar:
        with tqdm(file_entries, desc=f"压缩 {os.path.basename(directory)}",
                  unit="file", ncols=100, leave=False) as pbar:
            for full_path, arcname in pbar:
                tar.add(full_path, arcname=arcname)
    return tarball_name


def _format_size(size_bytes):
    """统一格式化文件大小"""
    units = ["B", "KB", "MB", "GB", "TB"]
    unit_index = 0
    while size_bytes >= 1024 and unit_index < 4:
        size_bytes /= 1024
        unit_index += 1
    return f"{size_bytes:.2f} {units[unit_index]}"


def print_empty_dirs(base_dir):
    """查找空目录"""
    empty_dirs = [root for root, dirs, files in os.walk(base_dir)
                  if not dirs and not files]
    if empty_dirs:
        print("空文件夹：")
        for d in empty_dirs:
            print(d)
    else:
        print("没有空文件夹。")


def save_dirs_to_json(dirs, file_name="file-name.json"):
    """保存目录列表"""
    with open(file_name, 'w', encoding='utf-8') as f:
        normalized = [os.path.normpath(d).replace("\\", "/") for d in dirs]
        json.dump(normalized, f, indent=4, ensure_ascii=False)


def load_dirs_from_json(file_name="file-name.json"):
    """加载目录列表"""
    try:
        with open(file_name, 'r', encoding='utf-8') as f:
            return [os.path.normpath(d) for d in json.load(f)]
    except (FileNotFoundError, json.JSONDecodeError):
        return []


def _delete_directory(dir_path):
    """删除单个目录"""
    try:
        if os.path.exists(dir_path):
            shutil.rmtree(dir_path)
            return (True, dir_path)
        return (False, f"目录不存在: {dir_path}")
    except Exception as e:
        return (False, f"删除失败 {dir_path}: {str(e)}")


import time  # 导入time模块，用于增加小延迟


def delete_directory_from_json(file_name="file-name.json"):
    """安全删除目录（已修复版本）"""
    try:
        dirs_to_delete = load_dirs_from_json(file_name)
        if not dirs_to_delete:
            print("没有需要删除的目录。")
            return

        # 预统计阶段
        total_files = 0
        total_size = 0
        valid_dirs = []

        for dir_path in dirs_to_delete:
            if os.path.isdir(dir_path):
                valid_dirs.append(dir_path)
                for root, _, files in os.walk(dir_path):
                    total_files += len(files)
                    total_size += sum(os.path.getsize(os.path.join(root, f)) for f in files)

        if not valid_dirs:
            print("没有有效的目录可删除。")
            return

        # 删除操作
        with concurrent.futures.ThreadPoolExecutor() as executor:
            futures = [executor.submit(_delete_directory, dir) for dir in valid_dirs]

            with tqdm(total=len(valid_dirs), desc="删除目录", unit="dir", ncols=100) as pbar:
                for future in concurrent.futures.as_completed(futures):
                    try:
                        # 获取每个任务的执行结果，这里需要确保解包的正确性
                        success, msg = future.result()  # 获取执行结果
                        if success:
                            pbar.set_postfix(status="删除成功")
                        else:
                            pbar.set_postfix(status="删除失败")
                        pbar.update(1)  # 更新进度条

                        # 增加小延迟，确保进度条显示
                        time.sleep(0.1)  # 延迟100ms，给进度条刷新时间
                    except Exception as e:
                        pbar.set_postfix(status=f"失败: {str(e)}")
                        pbar.update(1)

                pbar.refresh()  # 强制刷新进度条，确保显示

        # 输出删除统计
        success_count = sum(1 for future in futures if future.result()[0])  # 统计成功删除的目录
        print(f"\n删除统计:")
        print(f"- 成功删除: {success_count}/{len(valid_dirs)}")
        print(f"- 删除文件总数: {total_files}")
        print(f"- 释放空间: {_format_size(total_size)}")

    except Exception as e:
        print(f"操作异常: {str(e)}")


def main_menu():
    """显示主菜单"""
    print("\n" + "=" * 30)
    print("数据目录管理工具")
    print("1. 定位并保存符合日期格式的文件夹")
    print("2. 批量压缩目录")
    print("3. 显示空文件夹")
    print("4. 删除已保存的目录")
    print("0. 退出程序")


def main():
    if not os.path.exists(BASE_DIR):
        print(f"错误: 基础目录 {BASE_DIR} 不存在")
        return

    while True:
        main_menu()
        choice = input("请输入操作编号: ").strip()

        if choice == "1":
            # 定位目录逻辑
            target_dirs = []
            for root, dirs, _ in os.walk(BASE_DIR):
                for dir_name in dirs:
                    if DATE_PATTERN.search(dir_name):
                        full_path = os.path.normpath(os.path.join(root, dir_name))
                        target_dirs.append(full_path)

            if target_dirs:
                save_dirs_to_json(target_dirs)
                print(f"已保存 {len(target_dirs)} 个目录到 file-name.json")
            else:
                print("未找到符合要求的目录。")

        elif choice == "2":
            # 批量压缩逻辑
            try:
                dirs_to_compress = load_dirs_from_json()
                if not dirs_to_compress:
                    print("错误: 没有可压缩的目录")
                    continue

                print("\n正在执行压缩操作...")
                with concurrent.futures.ProcessPoolExecutor(max_workers=4) as executor:
                    futures = []
                    for dir_path in dirs_to_compress:
                        if not os.path.isdir(dir_path):
                            continue
                        base_name = extract_date_from_dir_name(os.path.basename(dir_path))
                        if not base_name:
                            continue
                        tarball_path = create_unique_tarball_name(
                            base_name,
                            os.path.dirname(dir_path)
                        )
                        futures.append(executor.submit(
                            compress_directory_to_tar_xz,
                            dir_path,
                            tarball_path
                        ))

                    # 处理进度
                    with tqdm(total=len(futures), desc="总体进度", unit="dir") as pbar:
                        for future in concurrent.futures.as_completed(futures):
                            try:
                                result = future.result()
                                print(f"\n压缩完成: {result}")
                            except Exception as e:
                                print(f"\n压缩失败: {str(e)}")
                            finally:
                                pbar.update(1)
                                # 清理输入缓冲区
                                while select.select([sys.stdin], [], [], 0)[0]:
                                    sys.stdin.read(1)

                input("\n操作完成，按回车返回主菜单...")

            except Exception as e:
                print(f"压缩操作异常: {str(e)}")

        elif choice == "3":
            print_empty_dirs(BASE_DIR)

        elif choice == "4":
            confirm = input("确定要删除所有保存的目录吗？(y/n): ").lower()
            if confirm == 'y':
                delete_directory_from_json()
            else:
                print("操作已取消")

        elif choice == "0":
            print("感谢使用，再见！")
            break

        else:
            print("无效输入，请重新选择")


if __name__ == "__main__":
    main()