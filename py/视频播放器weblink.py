#! python
# -*- coding: UTF-8 -*-
'''
============================================================================
项目名称: 本地视频播放器 with H.265→H.264 GPU 转码
文件名: app8y12-h.265转码264.py
创建用户:x	
AIEPN Inc
============================================================================
功能概述:
    1. 基于 Flask 的本地视频播放器 Web 界面
    2. 支持 H.265/HEVC 视频的 GPU 硬件加速转码为 H.264（浏览器兼容）
    3. 支持视频缩略图、播放统计、评论系统（含图片评论）
    4. 支持键盘快捷键、触摸滑动手势等交互

技术栈:
    - Flask: Web 框架，处理路由和 API 接口
    - FFmpeg/FFprobe: 视频转码与元数据检测
    - PIL/Pillow: 图片验证
    - 原生 Python: subprocess（调用 FFmpeg）、threading（后台转码）

设计决策:
    - H.265 视频优先使用 GPU 转码缓存，未缓存时回退到实时 CPU 转码流
    - 使用 threading.Lock 防止同一视频被重复转码
    - 使用 lru_cache 缓存 JSON 读取和视频索引，减少磁盘 I/O
    - 使用 atomic_write_json 防止 JSON 文件写入损坏

生成时间: 2025/10/17
============================================================================
'''
# ========== 标准库导入 ==========
import os                         # 文件系统操作（路径、目录、文件存在检查）
import json                       # JSON 序列化/反序列化（存储播放数据）
import time                       # 时间戳（评论排序、文件名生成）
import secrets                    # 安全随机数（生成唯一文件名，防止碰撞）
import urllib.parse                # URL 编码/解码（视频文件名含中文时安全传递）
import logging                    # 日志记录（调试和监控用）
import subprocess                 # 调用 FFmpeg/FFprobe 外部进程
import threading                  # 后台转码线程
import hashlib                    # MD5 哈希（生成缓存文件名）

# ========== 第三方库导入 ==========
from flask import Flask, render_template_string, send_from_directory, request, jsonify, abort, Response
# Flask: Web 应用框架
# Flask.render_template_string: 直接渲染 HTML 字符串模板
# send_from_directory: 安全地发送静态文件（图片、视频）
# request: 获取 HTTP 请求数据（表单、文件、查询参数）
# jsonify: 生成 JSON 响应
# abort: 返回 HTTP 错误响应（404 等）
# Response: 自定义 HTTP 响应（用于流式传输视频数据）

from PIL import Image             # Pillow 库：验证上传的图片文件是否有效
import io                         # 字节流处理（内存中处理文件数据）
from functools import lru_cache   # LRU 缓存装饰器（减少重复 I/O）
import mimetypes                  # MIME 类型检测

# ========== 日志配置 ==========
# 设置日志级别为 INFO，记录应用运行状态、转码进度等信息
logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

# ========== Flask 应用初始化 ==========
# static_url_path="" 表示静态文件直接从 "/" 访问
# static_folder="." 表示静态文件目录为当前工作目录
app = Flask(__name__, static_url_path="", static_folder=".")
# 限制上传文件大小上限为 10MB（防止恶意大文件攻击）
app.config["MAX_CONTENT_LENGTH"] = 10 * 1024 * 1024

# ========== 文件扩展名配置 ==========
# 支持的视频格式列表（浏览器可直接播放 H.264，H.265 需转码）
VIDEO_EXTS = (".mp4", ".mkv", ".avi", ".mov", ".webm")
# 支持的图片格式列表（用于缩略图和评论图片）
IMAGE_EXTS = (".jpg", ".jpeg", ".png", ".gif", ".webp")

# ========== 文件路径配置 ==========
VIEWS_FILE = "./data/views.json"       # 视频浏览量存储文件
COMMENTS_FILE = "./data/comments.json" # 评论数据存储文件（JSON 格式）
UPLOAD_DIR = "uploads"                 # 用户上传的评论图片目录
CACHE_DIR = "./cache"                  # H.265 转码缓存目录

# ========== 确保必要目录存在 ==========
# 程序启动时自动创建所需目录，避免后续写入时因目录不存在而报错
for dir_path in ["./data", "./data/cover", UPLOAD_DIR, CACHE_DIR]:
    os.makedirs(dir_path, exist_ok=True)

# ========== GPU 编码器检测 ==========
# 全局变量，初始化后由 detect_gpu_encoder() 设置
GPU_ENCODER = None     # 选中的 GPU H.264 编码器名称（如 'h264_amf', 'h264_qsv' 等）
GPU_HWACCEL = None     # 对应的硬件加速解码器（如 'd3d11va', 'qsv', 'cuda'）

def detect_gpu_encoder():
    """
    自动检测系统可用的 GPU 硬件编码器。

    检测逻辑:
        1. 运行 `ffmpeg -encoders` 获取所有可用编码器列表
        2. 按优先级选择: AMD AMF > Intel QSV > NVIDIA NVENC > CPU(回退)

    优先级设计原因:
        - AMD AMF 在核显和独显上都能工作，兼容性最佳
        - Intel QSV 在 Intel 核显上效率高，功耗低
        - NVIDIA NVENC 在独显上性能最强，但需要 NVIDIA 显卡
        - 三者都不可用则回退到 CPU libx264 软件编码

    返回值:
        无，直接设置全局变量 GPU_ENCODER 和 GPU_HWACCEL
    """
    global GPU_ENCODER, GPU_HWACCEL
    try:
        # 调用 FFmpeg 获取所有可用编码器列表
        result = subprocess.run(
            ['ffmpeg', '-encoders'], capture_output=True, text=True, check=True
        )
        encoders = result.stdout

        # 优先级检测：AMD AMF（核显+独显均可用） > Intel QSV > NVIDIA NVENC
        if 'h264_amf' in encoders:
            GPU_ENCODER = 'h264_amf'
            GPU_HWACCEL = 'd3d11va'   # AMD 推荐搭配 d3d11va 硬解 H.265 输入
            logger.info("GPU encoder: AMD AMF (h264_amf + d3d11va)")
            return
        if 'h264_qsv' in encoders:
            GPU_ENCODER = 'h264_qsv'
            GPU_HWACCEL = 'qsv'       # Intel QSV 硬件加速解码器
            logger.info("GPU encoder: Intel QuickSync (h264_qsv)")
            return
        if 'h264_nvenc' in encoders:
            GPU_ENCODER = 'h264_nvenc'
            GPU_HWACCEL = 'cuda'      # NVIDIA CUDA 硬件加速解码器
            logger.info("GPU encoder: NVIDIA NVENC (h264_nvenc + cuda)")
            return
    except Exception as e:
        # FFmpeg 未安装或编码器列表获取失败时降级
        logger.warning(f"GPU encoder detection failed: {e}")
    # 所有 GPU 编码器都不可用，后续转码会使用 CPU libx264
    logger.info("GPU encoder: none, falling back to CPU libx264")

detect_gpu_encoder()

# ========== 转码缓存系统 ==========
# 设计理念:
#   H.265 视频无法直接在浏览器中播放，需要转码为 H.264。
#   为避免每次观看都重新转码，系统将转码结果缓存为 .mp4 文件。
#   缓存以原视频的绝对路径的 MD5 为文件名，确保唯一性。

_transcode_locks = {}       # 字典: filename -> True，记录正在转码中的视频
_transcode_lock_map = threading.Lock()  # 保护 _transcode_locks 字典的线程锁

def _cache_key(filename):
    """
    生成转码缓存文件的完整路径。

    使用原视频文件的绝对路径的 MD5 哈希作为缓存文件名，
    这样即使文件名相同但路径不同，也能区分为不同条目。

    Args:
        filename: 原始视频文件的路径

    Returns:
        str: 缓存文件的完整路径，如 "./cache/a1b2c3d4...mp4"
    """
    h = hashlib.md5(os.path.abspath(filename).encode()).hexdigest()
    return os.path.join(CACHE_DIR, h + '.mp4')

def _cache_exists(filename):
    """
    检查指定视频的转码缓存是否已存在。

    Args:
        filename: 原始视频文件的路径

    Returns:
        bool: True 如果缓存文件存在，否则 False
    """
    return os.path.exists(_cache_key(filename))

def _build_gpu_cmd(filename, output_path):
    """
    根据检测到的 GPU 编码器构建对应的 FFmpeg 转码命令。

    转码策略:
        - H.265/HEVC → H.264: 解决浏览器兼容性问题
        - 使用对应 GPU 的硬件编码器，显著加快转码速度（比 CPU 快 5-10x）
        - 音频统一重新编码为 AAC 128kbps（浏览器广泛支持）

    各编码器画质参数详解:
        - AMD AMF: -rc cqp（恒定 QP 模式），qp_i/qp_p=24 控制 I/P 帧量化参数，
                  数值越小画质越高、文件越大，22~26 为合理范围
        - Intel QSV: -global_quality 23，范围 1(最佳)~51(最差)，23 平衡画质与体积
        - NVIDIA NVENC: -cq 23，恒定质量模式，范围 0~51，23 接近视觉无损
        - CPU x264: -crf 22，恒定画质因子，范围 0(无损)~51(最差)，18~28 推荐

    Args:
        filename: 输入视频文件路径
        output_path: 输出 H.264 视频文件路径

    Returns:
        list: FFmpeg 命令行参数列表，可直接传入 subprocess.run()
    """
    if GPU_ENCODER == 'h264_amf':
        # AMD AMF 编码器配置
        # -rc cqp: 恒定 QP 模式，类似 x264 的 CRF，保持恒定画质
        # -qp_i 24 / -qp_p 24: I 帧和 P 帧的量化参数，值越大文件越小但画质越低
        # -usage transcoding: 转码专用模式，优化了速度和质量的平衡
        return [
            'ffmpeg', '-y',
            '-hwaccel', 'd3d11va',       # 使用 DirectX 11 硬件解码输入 H.265 流
            '-i', filename,
            '-c:v', 'h264_amf',          # AMD H.264 硬件编码器
            '-rc', 'cqp',                # 恒定 QP 模式（类似 CRF 恒定画质）
            '-qp_i', '24',               # I 帧（关键帧）QP 值
            '-qp_p', '24',               # P 帧（预测帧）QP 值
            '-usage', 'transcoding',     # 转码优化模式
            '-c:a', 'aac', '-b:a', '128k',  # 音频：AAC 编码，128kbps 比特率
            '-pix_fmt', 'yuv420p',       # 像素格式：YUV 4:2:0（最大兼容性）
            '-movflags', '+faststart',   # moov atom 前置，支持边下边播
            output_path
        ]
    elif GPU_ENCODER == 'h264_qsv':
        # Intel QuickSync Video 编码器配置
        # -global_quality 23: 画质控制参数，1(最高画质)~51(最低)
        # -hwaccel_output_format qsv: 输出保持在 GPU 内存中，避免 CPU-GPU 拷贝
        return [
            'ffmpeg', '-y',
            '-hwaccel', 'qsv',
            '-hwaccel_output_format', 'qsv',  # 输出保持在 GPU 内存中，零拷贝
            '-i', filename,
            '-c:v', 'h264_qsv',          # Intel QSV H.264 硬件编码器
            '-global_quality', '23',     # 画质控制（1 最佳 ~ 51 最差）
            '-c:a', 'aac', '-b:a', '128k',
            '-pix_fmt', 'yuv420p',
            '-movflags', '+faststart',
            output_path
        ]
    elif GPU_ENCODER == 'h264_nvenc':
        # NVIDIA NVENC 编码器配置
        # -preset p4: NVENC 预设级别，p1(最快)~p7(最慢/最佳压缩)
        #   p4 是中速预设，在速度和压缩率之间取得平衡
        # -cq 23: 恒定质量控制，类似 CRF
        return [
            'ffmpeg', '-y',
            '-hwaccel', 'cuda',          # 使用 CUDA 硬件解码
            '-i', filename,
            '-c:v', 'h264_nvenc',        # NVIDIA H.264 硬件编码器
            '-preset', 'p4',             # 中速预设：平衡速度与压缩率
            '-cq', '23',                 # 恒定质量控制（0 最佳 ~ 51 最差）
            '-c:a', 'aac', '-b:a', '128k',
            '-pix_fmt', 'yuv420p',
            '-movflags', '+faststart',
            output_path
        ]
    else:
        # CPU 回退方案：使用 libx264 软件编码
        # -preset medium: x264 编码速度预设，medium 在速度和压缩率之间平衡
        # -crf 22: 恒定画质因子，比默认 23 略高画质
        # -threads 0: 自动使用所有可用 CPU 核心
        return [
            'ffmpeg', '-y',
            '-i', filename,
            '-c:v', 'libx264',           # CPU 软件 H.264 编码器
            '-preset', 'medium',         # 编码速度预设（ultrafast → veryslow）
            '-crf', '22',                # 恒定画质因子（0 无损 ~ 51 最差，推荐 18~28）
            '-c:a', 'aac', '-b:a', '128k',
            '-pix_fmt', 'yuv420p',
            '-movflags', '+faststart',   # moov atom 前置，流式播放优化
            '-threads', '0',             # 自动全核心 CPU 编码
            output_path
        ]

def _do_transcode(filename):
    """
    后台同步执行转码任务。

    使用 subprocess.run() 同步调用 FFmpeg 进行转码。
    超时时间设置为 3600 秒（1 小时），防止长时间阻塞。

    encoding='utf-8, errors=replace' 的设计原因:
        FFmpeg 的 stderr 输出可能包含非 UTF-8 字符（如特殊文件名），
        errors='replace' 将无法解码的字节替换为 � 字符，避免 UnicodeDecodeError。

    错误处理策略:
        - 转码失败（returncode != 0）：记录 stderr 尾部 500 字符用于调试，清理失败文件
        - 超时异常（TimeoutExpired）：记录错误，清理不完整的输出文件
        - 其他异常：通用错误记录，清理文件

    Args:
        filename: 原始视频文件路径
    """
    output = _cache_key(filename)
    logger.info(f"[Transcode] Starting GPU transcode: {filename} -> {output}")

    cmd = _build_gpu_cmd(filename, output)
    logger.info(f"[Transcode] CMD: {' '.join(cmd)}")

    try:
        # 同步执行 FFmpeg，超时 3600 秒（1 小时）
        # encoding='utf-8' 确保 stderr 输出以文本形式捕获
        # errors='replace' 容忍非 UTF-8 字符，防止异常中断
        proc = subprocess.run(cmd, capture_output=True, timeout=3600,
                              encoding='utf-8', errors='replace')
        # 检查返回码为 0 且输出文件确实存在
        if proc.returncode == 0 and os.path.exists(output):
            size_mb = os.path.getsize(output) / (1024 * 1024)
            logger.info(f"[Transcode] Done: {output} ({size_mb:.1f} MB)")
        else:
            logger.error(f"[Transcode] Failed with code {proc.returncode}")
            logger.error(f"[Transcode] stderr: {proc.stderr[-500:]}")
            # 转码失败时清理可能不完整的输出文件
            if os.path.exists(output):
                os.remove(output)
    except subprocess.TimeoutExpired:
        logger.error(f"[Transcode] Timeout for {filename}")
        # 超时也需清理可能存在的半成品文件
        if os.path.exists(output):
            os.remove(output)
    except Exception as e:
        logger.error(f"[Transcode] Error: {e}")
        # 未知错误也需清理文件
        if os.path.exists(output):
            os.remove(output)

def start_background_transcode(filename):
    """
    启动后台转码线程，非阻塞方式执行转码。

    设计要点:
        - 去重保护: 通过 _transcode_locks 字典防止同一视频被多次提交转码
        - daemon=True: 守护线程，主进程退出时自动结束，不阻止程序退出
        - 非阻塞: 调用后立即返回，转码在后台线程中执行

    Args:
        filename: 原始视频文件路径
    """
    with _transcode_lock_map:
        if filename in _transcode_locks:
            logger.info(f"[Transcode] Already transcoding: {filename}")
            return
        _transcode_locks[filename] = True

    t = threading.Thread(target=_bg_transcode_wrapper, args=(filename,), daemon=True)
    t.start()

def _bg_transcode_wrapper(filename):
    """
    后台转码包装函数，确保转码完成后清理锁状态。

    使用 try/finally 结构保证即使转码异常退出，锁状态也会被正确释放，
    防止死锁导致后续对同一视频的转码请求被永久忽略。

    Args:
        filename: 原始视频文件路径
    """
    try:
        _do_transcode(filename)
    finally:
        # 无论成功还是失败，必须释放锁
        with _transcode_lock_map:
            _transcode_locks.pop(filename, None)

# ------------- 工具函数 -------------

def get_video_codec(filename):
    """
    使用 ffprobe 获取视频文件中第一条视频流的编解码器名称。

    工作原理:
        - 运行 `ffprobe -select_streams v:0 -show_entries stream=codec_name`
        - 从输出中提取编解码器字符串（如 'hevc', 'h264', 'vp9' 等）
        - 返回小写形式，便于后续字符串比较

    ffprobe 参数说明:
        -v quiet:     静默模式，只输出请求的数据
        -select_streams v:0: 只选择第一条视频流
        -show_entries stream=codec_name: 只显示编解码器名称
        -of csv=p=0:  输出格式为 CSV，无表头

    Args:
        filename: 视频文件路径

    Returns:
        str: 编解码器名称（小写），如 'hevc', 'h264' 等
        None: 检测失败时返回
    """
    try:
        cmd = [
            'ffprobe', '-v', 'quiet', '-select_streams', 'v:0',
            '-show_entries', 'stream=codec_name', '-of', 'csv=p=0', filename
        ]
        result = subprocess.run(cmd, capture_output=True, text=True, check=True)
        codec = result.stdout.strip()   # 去除首尾空白字符
        return codec.lower()            # 统一转为小写便于比较
    except (subprocess.CalledProcessError, FileNotFoundError) as e:
        logger.warning(f"Failed to probe codec for {filename}: {e}")
        return None

def get_video_duration(filename):
    """
    使用 ffprobe 获取视频文件的时长（秒）。

    工作原理:
        - 运行 `ffprobe -show_entries format=duration`
        - 提取 duration 字段并转换为 float 类型
        - 可用于播放进度计算和 UI 展示

    异常处理:
        - CalledProcessError: ffprobe 执行失败（如文件损坏）
        - FileNotFoundError: ffprobe 未安装
        - ValueError: 输出无法转换为浮点数

    Args:
        filename: 视频文件路径

    Returns:
        float: 视频时长（秒）
        None: 检测失败时返回
    """
    try:
        cmd = [
            'ffprobe', '-v', 'quiet', '-select_streams', 'v:0',
            '-show_entries', 'format=duration', '-of', 'csv=p=0', filename
        ]
        result = subprocess.run(cmd, capture_output=True, text=True, check=True)
        return float(result.stdout.strip())
    except (subprocess.CalledProcessError, FileNotFoundError, ValueError) as e:
        logger.warning(f"Failed to probe duration for {filename}: {e}")
        return None

def stream_transcoded_video(filename, seek_time=0):
    """
    使用 FFmpeg 实时将 H.265 视频转码为 H.264 并通过生成器流式传输。

    应用场景:
        当 H.265 视频尚未完成 GPU 后台转码缓存时，作为临时回退方案，
        让用户能立即开始观看，而不必等待完整转码完成。

    seek_time 参数的设计:
        用户拖动进度条时，需从指定时间点开始转码。实现方式：
        1. 将 -ss 参数放在 -i 前面，进行关键帧快速 seek（速度更快，但不精确到帧）
        2. 重新创建 FFmpeg 进程，从新位置开始转码到 stdout
        3. 因为实时转码流没有 Content-Length，浏览器无法原生 seek，
           所以前端使用 ?t= 参数重新请求视频 URL 来实现跳转

    编码参数设计原因:
        - preset ultrafast: 实时转码需要极低延迟，牺牲压缩率换取速度
        - tune zerolatency: 关闭编码器内部的 look-ahead 缓冲，减少输出延迟
        - frag_keyframe+empty_moov: 生成碎片化 MP4，每帧可独立解码，支持流式播放

    资源管理:
        - 使用 subprocess.Popen 非阻塞启动 FFmpeg
        - try/finally 确保生成器结束或客户端断开时进程被终止
        - 每次 4096 字节读取，平衡内存占用和 I/O 效率

    Args:
        filename: 原始 H.265 视频文件路径
        seek_time: 起始播放时间（秒），0 表示从头播放

    Returns:
        generator: 视频数据流生成器，每次 yield 4096 字节
        None: 文件不是 H.265 编码，无需转码
    """
    codec = get_video_codec(filename)
    if codec != 'hevc':
        logger.warning(f"File {filename} is not H.265, no transcoding needed")
        return None

    # 内部生成器函数：实际执行 FFmpeg 转码并逐块产出视频数据
    def generate():
        cmd = ['ffmpeg']
        if seek_time > 0:
            # -ss 放在 -i 前面进行输入前 seek（关键帧定位，快速但不精确）
            cmd += ['-ss', str(seek_time)]
            logger.info(f"FFmpeg seeking from {seek_time}s")
        cmd += [
            '-i', filename,
            '-c:v', 'libx264', '-preset', 'ultrafast', '-crf', '23', '-tune', 'zerolatency',
            '-c:a', 'aac', '-b:a', '128k',
            '-threads', '0',
            '-pix_fmt', 'yuv420p',
            '-f', 'mp4',                         # 输出格式：MP4
            '-movflags', 'frag_keyframe+empty_moov',  # 碎片化 MP4，支持流式传输
            '-'  # 输出到 stdout，由 Python 生成器读取
        ]

        # 使用 Popen 非阻塞启动 FFmpeg，便于后续流式读取
        proc = subprocess.Popen(
            cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE, bufsize=0
        )
        try:
            # 循环读取 stdout，每次 4096 字节，平衡内存占用与 I/O 次数
            while True:
                data = proc.stdout.read(4096)
                if not data:  # 读取到 EOF，转码完成
                    break
                yield data  # 将数据块交给 Flask Response 流式传输
        finally:
            # 无论正常结束还是客户端断开，都要终止 FFmpeg 进程
            proc.terminate()
            proc.wait()

    return generate

# @lru_cache(maxsize=1): 缓存最近 1 次调用的结果
# 每次 index_videos() 返回相同结果时直接返回缓存，避免重复扫描磁盘
# 注意：新增视频文件后需要重启应用才能被索引到
@lru_cache(maxsize=1)
def index_videos():
    """
    扫描当前目录下的所有视频文件并构建播放列表。

    工作流程:
        1. 使用 os.listdir("./") 列出当前目录所有文件
        2. 过滤出扩展名在 VIDEO_EXTS 中的文件
        3. 为每个视频构建包含 URL、名称、缩略图、浏览量的条目

    缩略图策略:
        使用文件名（不含扩展名）匹配 cover_<name>.jpg，
        匹配规则: 视频 "movie.mp4" → 缩略图 "data/cover/cover_movie.jpg"
        未匹配到时使用默认缩略图 default_thumbnail.jpg

    返回值:
        list[dict]: 播放列表，每个元素包含:
            - url: 视频播放 URL（经过 URL 编码处理中文文件名）
            - name: 视频文件名
            - thumbnails: 缩略图 URL
            - views: 浏览量（从 views.json 加载）
    """
    videos = [f for f in os.listdir("./") if f.lower().endswith(VIDEO_EXTS)]
    playlist = []
    for v in videos:
        name, _ = os.path.splitext(v)
        thumbnail = f"data/cover/cover_{name}.jpg"
        thumbnail_url = f"/{thumbnail}" if os.path.exists(thumbnail) else "/data/cover/default_thumbnail.jpg"
        if not os.path.exists(thumbnail):
            logger.warning(f"Thumbnail not found for {v}: {thumbnail}, using default")
        playlist.append({
            "url": f"/video/{urllib.parse.quote(v)}",
            "name": v,
            "thumbnails": thumbnail_url,
            "views": load_views().get(v, 0)
        })
    logger.info(f"Indexed {len(videos)} videos")
    return playlist

# @lru_cache(maxsize=1): 缓存最近 1 次文件加载结果
# 同一个文件被多次读取时直接返回缓存，减少磁盘 I/O
# 注意：外部修改 JSON 文件后需重启或手动清除缓存
@lru_cache(maxsize=1)
def load_json(filepath: str, default: dict = None):
    """
    加载 JSON 文件，如果文件不存在或解析失败返回默认值。

    设计决策:
        - 使用 @lru_cache 缓存最近一次读取结果，避免重复磁盘 I/O
        - encoding='utf-8' 确保中文字符正确读写
        - 解析失败时记录错误但返回默认值，不中断程序运行

    Args:
        filepath: JSON 文件路径
        default: 文件不存在或解析失败时返回的默认值，默认为空字典

    Returns:
        dict: JSON 数据的字典形式
    """
    if default is None:
        default = {}
    if os.path.exists(filepath):
        try:
            with open(filepath, "r", encoding="utf-8") as f:
                return json.load(f)
        except Exception as e:
            logger.error(f"Failed to load JSON {filepath}: {e}")
    return default

def load_views():
    """加载浏览量数据"""
    return load_json(VIEWS_FILE)

def save_views(data: dict):
    """保存浏览量数据"""
    atomic_write_json(VIEWS_FILE, data)

def load_comments():
    """加载评论数据"""
    return load_json(COMMENTS_FILE)

def save_comments(data: dict):
    """保存评论数据"""
    atomic_write_json(COMMENTS_FILE, data)

def atomic_write_json(filepath: str, data: dict):
    """
    原子写入 JSON 文件，防止写入过程中断导致文件损坏。

    实现原理:
        1. 先将数据写入临时文件 .tmp
        2. 写入完成后使用 os.replace() 原子替换目标文件
        3. os.replace() 在 Windows 和 Unix 上都是原子操作

    设计原因:
        - 防止程序崩溃时 JSON 文件被写出不完整数据
        - ensure_ascii=False: 保留中文字符不转义为 \\uXXXX
        - indent=2: 缩进格式化，便于人工查看和编辑

    Args:
        filepath: 目标 JSON 文件路径
        data: 要写入的数据字典
    """
    tmp = filepath + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)
    os.replace(tmp, filepath)

def secure_image_filename(filename: str) -> str:
    """
    生成安全的评论图片文件名，防止路径穿越和文件名冲突。

    安全策略:
        - 不保留用户原始文件名（防止路径穿越攻击如 ../etc/passwd）
        - 使用时间戳 + secrets.token_hex(8) 生成 16 位随机十六进制字符串
        - 只保留原始文件的扩展名，且限制在 IMAGE_EXTS 白名单内
        - 非白名单扩展名默认回退为 .jpg

    文件名格式:
        cmt_{Unix时间戳}_{8字节随机十六进制}.{扩展名}
        例如: cmt_1717200000_a1b2c3d4e5f6g7h8.jpg

    Args:
        filename: 用户上传的原始文件名

    Returns:
        str: 安全的图片文件名
    """
    name = filename or ""
    _, ext = os.path.splitext(name)
    ext = ext.lower() if ext.lower() in IMAGE_EXTS else ".jpg"
    return f"cmt_{int(time.time())}_{secrets.token_hex(8)}{ext}"

def is_valid_image(file):
    """
    验证上传的文件是否为有效图片格式。

    验证方式:
        使用 Pillow 的 Image.open() + verify() 方法。
        verify() 会检查文件的完整性，包括文件头、数据结构和校验码，
        但不会将整个图片加载到内存中，效率较高。

    注意:
        file 参数应为文件对象（如 Flask request.files 中的 FileStorage），
        验证后文件指针位置会改变，调用者需根据需要 seek(0) 重置。

    Args:
        file: 文件对象（Flask FileStorage 或普通 file-like 对象）

    Returns:
        bool: True 如果文件是有效图片，否则 False
    """
    try:
        img = Image.open(file)
        img.verify()
        return True
    except Exception as e:
        logger.error(f"Invalid image file: {e}")
        return False

# ------------- 页面 -------------

@app.route("/")
def index():
    """主页路由"""
    playlist = index_videos()
    playlist_js = json.dumps(playlist, ensure_ascii=False)
    html = f"""
    <!DOCTYPE html>
    <html lang="zh-CN">
    <head>
        <meta charset="utf-8">
        <meta name="viewport" content="width=device-width, initial-scale=1.0">
        <title>MusePlay - 本地视频</title>
        <script src="https://cdn.jsdelivr.net/npm/dplayer@1.27.1/dist/DPlayer.min.js"></script>
        <style>
            /* ============================================================
               CSS 主题变量（Design Tokens）
               使用 CSS 自定义属性定义全局设计变量，通过 data-theme 属性
               在深色/浅色主题间一键切换。动画使用自定义贝塞尔曲线，
               实现流畅的弹性动画和自然减速效果。
               ============================================================ */
            :root {{
                /* 圆角半径：xs(6px) → xl(32px) 五级梯度 */
                --radius-xs: 6px;
                --radius-sm: 10px;
                --radius-md: 16px;
                --radius-lg: 24px;
                --radius-xl: 32px;
                /* 缓动函数：expo 模拟自然减速，back 提供轻微回弹效果 */
                --ease-out-expo: cubic-bezier(0.16, 1, 0.3, 1);
                --ease-out-back: cubic-bezier(0.34, 1.56, 0.64, 1);
                --ease-in-out: cubic-bezier(0.65, 0, 0.35, 1);
                /* 动画时长：fast(150ms) / normal(300ms) / slow(500ms) */
                --transition-fast: 0.15s var(--ease-out-expo);
                --transition-normal: 0.3s var(--ease-out-expo);
                --transition-slow: 0.5s var(--ease-out-expo);
            }}

            /* ---- 深色主题（默认）----
               采用多层背景色叠加策略（base→layer→surface→elevated），
               配合玻璃拟态（glassmorphism）半透明背景和毛玻璃模糊效果。
               主色调使用靛蓝紫渐变（Indigo→Violet），阴影带彩色光晕。 */
            [data-theme="dark"] {{
                --bg-base: #09090b;
                --bg-layer: #131318;
                --bg-surface: #1a1a22;
                --bg-elevated: #242430;
                --bg-glass: rgba(26, 26, 34, 0.75);
                --text-primary: #f1f1f3;
                --text-secondary: #9898a6;
                --text-tertiary: #64647a;
                --accent: #6366f1;
                --accent-glow: rgba(99, 102, 241, 0.25);
                --accent-secondary: #8b5cf6;
                --border: rgba(255, 255, 255, 0.06);
                --border-active: rgba(99, 102, 241, 0.4);
                --shadow-sm: 0 1px 2px rgba(0,0,0,0.3);
                --shadow-md: 0 4px 16px rgba(0,0,0,0.35);
                --shadow-lg: 0 8px 40px rgba(0,0,0,0.5);
                --shadow-glow: 0 0 30px var(--accent-glow);
                --gradient-bg: linear-gradient(180deg, #0c0c14 0%, #09090b 100%);
                --gradient-card: linear-gradient(135deg, rgba(99,102,241,0.06) 0%, rgba(139,92,246,0.04) 100%);
                --gradient-header: linear-gradient(135deg, #1e1b4b 0%, #0f0d2e 50%, #09090b 100%);
            }}

            /* ---- 浅色主题 ----
               暗色背景映射到亮色背景，阴影强度降低，
               玻璃拟态半透明背景偏白，整体干净清爽。 */
            [data-theme="light"] {{
                --bg-base: #fafafa;
                --bg-layer: #f4f4f5;
                --bg-surface: #ffffff;
                --bg-elevated: #f8f8fc;
                --bg-glass: rgba(255, 255, 255, 0.8);
                --text-primary: #18181b;
                --text-secondary: #71717a;
                --text-tertiary: #a1a1aa;
                --accent: #6366f1;
                --accent-glow: rgba(99, 102, 241, 0.15);
                --accent-secondary: #8b5cf6;
                --border: rgba(0, 0, 0, 0.06);
                --border-active: rgba(99, 102, 241, 0.3);
                --shadow-sm: 0 1px 2px rgba(0,0,0,0.04);
                --shadow-md: 0 4px 16px rgba(0,0,0,0.06);
                --shadow-lg: 0 8px 40px rgba(0,0,0,0.08);
                --shadow-glow: 0 0 30px var(--accent-glow);
                --gradient-bg: linear-gradient(180deg, #fefefe 0%, #fafafa 100%);
                --gradient-card: linear-gradient(135deg, rgba(99,102,241,0.03) 0%, rgba(139,92,246,0.02) 100%);
                --gradient-header: linear-gradient(135deg, #ede9fe 0%, #e0e7ff 50%, #fafafa 100%);
            }}

            /* 全局重置 + antialiased 字体渲染，overflow-x 防止横向溢出 */
            * {{ margin: 0; padding: 0; box-sizing: border-box; }}

            /* body 随主题变量切换背景和文字颜色，过渡动画 500ms */
            body {{
                font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", Roboto, "Helvetica Neue", Arial, sans-serif;
                background: var(--bg-base);
                color: var(--text-primary);
                min-height: 100vh;
                transition: background var(--transition-slow), color var(--transition-slow);
                -webkit-font-smoothing: antialiased;
                -moz-osx-font-smoothing: grayscale;
                overflow-x: hidden;
            }}

            /* ---- 动态背景粒子（Ambient Orbs）----
               固定定位的模糊光球，用 accent 色和粉色营造氛围感。
               pointer-events: none 确保不干扰用户交互。
               随主题变量变化切换颜色，过渡动画 500ms。 */
            .bg-orb {{
                position: fixed;
                border-radius: 50%;
                filter: blur(120px);
                opacity: 0.3;
                pointer-events: none;
                z-index: 0;
                transition: all var(--transition-slow);
            }}
            .bg-orb-1 {{
                width: 500px; height: 500px;
                background: var(--accent);
                top: -200px; right: -150px;
                opacity: 0.12;
            }}
            .bg-orb-2 {{
                width: 400px; height: 400px;
                background: var(--accent-secondary);
                bottom: -100px; left: -100px;
                opacity: 0.08;
            }}
            .bg-orb-3 {{
                width: 300px; height: 300px;
                background: #ec4899;
                top: 50%; left: 50%;
                transform: translate(-50%, -50%);
                opacity: 0.05;
            }}

            /* ---- 顶部导航栏（Glassmorphism Sticky Navbar）----
               毛玻璃效果：backdrop-filter blur(20px) + saturate(180%)
               + 半透明背景，实现类似 macOS/iOS 的磨砂玻璃质感。
               sticky 定位 + z-index 1000 保证始终可见。 */
            .navbar {{
                position: sticky;
                top: 0;
                z-index: 1000;
                backdrop-filter: blur(20px) saturate(180%);
                -webkit-backdrop-filter: blur(20px) saturate(180%);
                background: var(--bg-glass);
                border-bottom: 1px solid var(--border);
                transition: all var(--transition-normal);
            }}

            .navbar-inner {{
                max-width: 1440px;
                margin: 0 auto;
                padding: 14px 24px;
                display: flex;
                align-items: center;
                justify-content: space-between;
                gap: 16px;
            }}

            .navbar-brand {{
                display: flex;
                align-items: center;
                gap: 12px;
                text-decoration: none;
            }}

            .navbar-logo {{
                width: 38px;
                height: 38px;
                border-radius: var(--radius-sm);
                background: linear-gradient(135deg, var(--accent), var(--accent-secondary));
                display: flex;
                align-items: center;
                justify-content: center;
                font-size: 18px;
                color: #fff;
                box-shadow: 0 4px 16px var(--accent-glow);
            }}

            .navbar-title {{
                font-size: 1.25rem;
                font-weight: 700;
                letter-spacing: -0.02em;
                color: var(--text-primary);
            }}
            .navbar-title span {{ color: var(--accent); }}

            .navbar-actions {{
                display: flex;
                align-items: center;
                gap: 10px;
            }}

            .btn {{
                display: inline-flex;
                align-items: center;
                gap: 6px;
                padding: 10px 18px;
                border: none;
                border-radius: var(--radius-sm);
                font-family: inherit;
                font-size: 0.875rem;
                font-weight: 500;
                cursor: pointer;
                transition: all var(--transition-fast);
                white-space: nowrap;
                position: relative;
                overflow: hidden;
            }}

            .btn-theme {{
                background: var(--bg-elevated);
                color: var(--text-primary);
                border: 1px solid var(--border);
                backdrop-filter: blur(8px);
            }}
            .btn-theme:hover {{
                background: var(--bg-surface);
                border-color: var(--border-active);
                transform: translateY(-1px);
            }}

            .btn-theme .icon-sun {{ display: none; }}
            .btn-theme .icon-moon {{ display: inline; }}
            [data-theme="light"] .btn-theme .icon-sun {{ display: inline; }}
            [data-theme="light"] .btn-theme .icon-moon {{ display: none; }}

            /* ---- 主布局（两栏 Flexbox）----
               left-panel: flex:1 自适应宽度，播放器+评论区
               right-panel: 固定 340px，sticky 定位跟随滚动，播放列表 */
            .main-content {{
                display: flex;
                gap: 24px;
                padding: 24px;
                max-width: 1440px;
                margin: 0 auto;
                position: relative;
                z-index: 1;
            }}

            .left-panel {{
                flex: 1;
                min-width: 0;
                display: flex;
                flex-direction: column;
                gap: 20px;
            }}

            .right-panel {{
                width: 340px;
                flex-shrink: 0;
                position: sticky;
                top: 90px;
                align-self: flex-start;
                max-height: calc(100vh - 120px);
                display: flex;
                flex-direction: column;
                gap: 16px;
            }}

            /* ---- 播放器区域（Player Card）----
               卡片式容器，hover 时边框高亮 + 阴影光晕增强，
               aspect-ratio 16:9 保持播放器宽高比。 */
            .player-wrapper {{
                background: var(--bg-layer);
                border: 1px solid var(--border);
                border-radius: var(--radius-md);
                overflow: hidden;
                box-shadow: var(--shadow-md);
                transition: all var(--transition-normal);
            }}
            .player-wrapper:hover {{
                box-shadow: var(--shadow-lg), var(--shadow-glow);
                border-color: var(--border-active);
            }}

            .player-title-bar {{
                padding: 14px 20px;
                background: var(--bg-elevated);
                border-bottom: 1px solid var(--border);
                display: flex;
                align-items: center;
                gap: 10px;
            }}
            .player-title-dot {{
                width: 8px;
                height: 8px;
                border-radius: 50%;
                background: #22c55e;
                box-shadow: 0 0 8px rgba(34, 197, 94, 0.5);
                flex-shrink: 0;
                animation: pulse-dot 2s var(--ease-in-out) infinite;
            }}
            @keyframes pulse-dot {{
                0%, 100% {{ opacity: 1; transform: scale(1); }}
                50% {{ opacity: 0.5; transform: scale(1.3); }}
            }}
            #video-title {{
                font-size: 0.95rem;
                font-weight: 600;
                color: var(--text-primary);
                white-space: nowrap;
                overflow: hidden;
                text-overflow: ellipsis;
                letter-spacing: -0.01em;
            }}
            .player-title-badge {{
                font-size: 0.7rem;
                font-weight: 600;
                text-transform: uppercase;
                letter-spacing: 0.05em;
                padding: 3px 10px;
                border-radius: 20px;
                background: var(--accent);
                color: #fff;
                opacity: 0;
                transform: translateX(8px);
                transition: all var(--transition-normal);
                white-space: nowrap;
            }}
            .player-title-badge.visible {{
                opacity: 1;
                transform: translateX(0);
            }}

            #player-container {{
                background: #000;
                position: relative;
                aspect-ratio: 16 / 9;
            }}

            #dplayer {{
                width: 100%;
                height: 100%;
            }}

            #dplayer video {{
                width: 100% !important;
                height: 100% !important;
                object-fit: contain;
            }}

            .player-controls {{
                display: flex;
                align-items: center;
                justify-content: center;
                gap: 6px;
                padding: 12px 20px;
                background: var(--bg-elevated);
                border-top: 1px solid var(--border);
            }}

            /* 控制器按钮：hover 上浮 2px + accent 色填充，
               active 缩放 94% 提供按压反馈，disabled 半透明禁用 */
            .ctrl-btn {{
                display: inline-flex;
                align-items: center;
                justify-content: center;
                width: 42px;
                height: 42px;
                border: 1px solid var(--border);
                border-radius: var(--radius-sm);
                background: var(--bg-surface);
                color: var(--text-primary);
                cursor: pointer;
                font-size: 1.1rem;
                transition: all var(--transition-fast);
                position: relative;
            }}
            .ctrl-btn:hover {{
                background: var(--accent);
                color: #fff;
                border-color: var(--accent);
                transform: translateY(-2px);
                box-shadow: 0 4px 12px var(--accent-glow);
            }}
            .ctrl-btn:active {{
                transform: scale(0.94);
                transition: transform 0.08s var(--ease-out-expo);
            }}
            .ctrl-btn:disabled {{
                opacity: 0.35;
                cursor: not-allowed;
                transform: none;
                box-shadow: none;
            }}
            .ctrl-btn:disabled:hover {{
                background: var(--bg-surface);
                color: var(--text-primary);
                border-color: var(--border);
            }}

            .ctrl-divider {{
                width: 1px;
                height: 24px;
                background: var(--border);
                margin: 0 6px;
            }}

            .ctrl-key-hint {{
                font-size: 0.65rem;
                color: var(--text-tertiary);
                margin-left: auto;
                opacity: 0.6;
            }}

            /* ---- 播放列表（Playlist Panel）----
               右侧面板主区域，flex:1 撑满剩余高度。
               内部 Grid 布局自动适配列数，自定义滚动条样式。
               视频卡片 hover 上浮 4px + accent 光晕阴影。 */
            .playlist-panel {{
                background: var(--bg-layer);
                border: 1px solid var(--border);
                border-radius: var(--radius-md);
                overflow: hidden;
                box-shadow: var(--shadow-sm);
                display: flex;
                flex-direction: column;
                flex: 1;
                min-height: 300px;
            }}

            .playlist-header {{
                padding: 16px;
                border-bottom: 1px solid var(--border);
            }}
            .playlist-header-title {{
                font-size: 0.8rem;
                font-weight: 600;
                text-transform: uppercase;
                letter-spacing: 0.06em;
                color: var(--text-secondary);
                margin-bottom: 12px;
            }}

            .search-box {{
                position: relative;
            }}
            .search-icon {{
                position: absolute;
                left: 14px;
                top: 50%;
                transform: translateY(-50%);
                color: var(--text-tertiary);
                font-size: 0.85rem;
                pointer-events: none;
                transition: color var(--transition-fast);
            }}
            #search-input {{
                width: 100%;
                padding: 11px 14px 11px 38px;
                border: 1px solid var(--border);
                border-radius: var(--radius-sm);
                background: var(--bg-surface);
                color: var(--text-primary);
                font-family: inherit;
                font-size: 0.875rem;
                transition: all var(--transition-fast);
                outline: none;
            }}
            #search-input:focus {{
                border-color: var(--accent);
                box-shadow: 0 0 0 3px var(--accent-glow);
            }}
            #search-input:focus + .search-icon,
            .search-box:focus-within .search-icon {{
                color: var(--accent);
            }}
            #search-input::placeholder {{
                color: var(--text-tertiary);
            }}

            #playlist {{
                flex: 1;
                overflow-y: auto;
                padding: 12px;
                display: grid;
                grid-template-columns: repeat(auto-fill, minmax(140px, 1fr));
                gap: 10px;
                align-content: start;
            }}

            #playlist::-webkit-scrollbar {{
                width: 4px;
            }}
            #playlist::-webkit-scrollbar-track {{
                background: transparent;
            }}
            #playlist::-webkit-scrollbar-thumb {{
                background: var(--border);
                border-radius: 10px;
            }}

            .video-card {{
                background: var(--bg-surface);
                border: 1px solid var(--border);
                border-radius: var(--radius-sm);
                overflow: hidden;
                cursor: pointer;
                transition: all var(--transition-normal);
                display: flex;
                flex-direction: column;
                position: relative;
            }}
            .video-card:hover {{
                transform: translateY(-4px);
                border-color: var(--border-active);
                box-shadow: var(--shadow-md), 0 8px 30px var(--accent-glow);
            }}
            .video-card.active {{
                border-color: var(--accent);
                box-shadow: 0 0 0 2px var(--accent-glow);
            }}
            .video-card.active::after {{
                content: '▶';
                position: absolute;
                top: 8px;
                right: 8px;
                width: 24px;
                height: 24px;
                border-radius: 50%;
                background: var(--accent);
                color: #fff;
                font-size: 10px;
                display: flex;
                align-items: center;
                justify-content: center;
                box-shadow: 0 2px 8px rgba(0,0,0,0.3);
                z-index: 2;
            }}

            .video-thumb-wrap {{
                position: relative;
                aspect-ratio: 16 / 10;
                overflow: hidden;
                background: var(--bg-elevated);
            }}
            .video-thumb {{
                width: 100%;
                height: 100%;
                object-fit: cover;
                transition: transform var(--transition-slow);
            }}
            .video-card:hover .video-thumb {{
                transform: scale(1.08);
            }}
            .video-thumb-overlay {{
                position: absolute;
                inset: 0;
                background: linear-gradient(to top, rgba(0,0,0,0.5) 0%, transparent 50%);
                opacity: 0;
                transition: opacity var(--transition-normal);
            }}
            .video-card:hover .video-thumb-overlay {{
                opacity: 1;
            }}
            .video-duration {{
                position: absolute;
                bottom: 6px;
                right: 6px;
                background: rgba(0,0,0,0.75);
                color: #fff;
                font-size: 0.68rem;
                font-weight: 500;
                padding: 2px 6px;
                border-radius: 4px;
                letter-spacing: 0.02em;
            }}

            .video-info {{
                padding: 10px 12px;
                display: flex;
                flex-direction: column;
                gap: 4px;
                flex: 1;
            }}
            .video-name {{
                font-size: 0.8rem;
                font-weight: 500;
                line-height: 1.35;
                color: var(--text-primary);
                display: -webkit-box;
                -webkit-line-clamp: 2;
                -webkit-box-orient: vertical;
                overflow: hidden;
                letter-spacing: -0.01em;
            }}
            .video-meta {{
                display: flex;
                align-items: center;
                justify-content: space-between;
                font-size: 0.7rem;
                color: var(--text-tertiary);
            }}
            .video-meta-views {{
                display: flex;
                align-items: center;
                gap: 3px;
            }}

            .playlist-empty {{
                grid-column: 1 / -1;
                text-align: center;
                padding: 40px 20px;
                color: var(--text-tertiary);
                font-size: 0.875rem;
            }}

            /* ---- 评论区（Comments Panel）----
               左侧面板下方，包含昵称输入、评论文本框、
               图片上传、提交按钮和评论列表。
               支持图片上传和图片点击放大预览。 */
            .comments-panel {{
                background: var(--bg-layer);
                border: 1px solid var(--border);
                border-radius: var(--radius-md);
                overflow: hidden;
                box-shadow: var(--shadow-sm);
            }}
            .comments-header {{
                padding: 16px 20px;
                border-bottom: 1px solid var(--border);
                display: flex;
                align-items: center;
                gap: 8px;
            }}
            .comments-header h3 {{
                font-size: 1rem;
                font-weight: 600;
                letter-spacing: -0.01em;
            }}
            .comments-count {{
                font-size: 0.75rem;
                color: var(--text-tertiary);
                background: var(--bg-elevated);
                padding: 2px 10px;
                border-radius: 20px;
            }}

            #comment-controls {{
                padding: 16px 20px;
                display: flex;
                flex-direction: column;
                gap: 10px;
                border-bottom: 1px solid var(--border);
            }}
            .comment-row {{
                display: flex;
                gap: 8px;
            }}
            #username {{
                flex: 0 0 140px;
                padding: 10px 14px;
                border: 1px solid var(--border);
                border-radius: var(--radius-sm);
                background: var(--bg-surface);
                color: var(--text-primary);
                font-family: inherit;
                font-size: 0.85rem;
                outline: none;
                transition: all var(--transition-fast);
            }}
            #comment-input {{
                flex: 1;
                padding: 10px 14px;
                border: 1px solid var(--border);
                border-radius: var(--radius-sm);
                background: var(--bg-surface);
                color: var(--text-primary);
                font-family: inherit;
                font-size: 0.85rem;
                outline: none;
                transition: all var(--transition-fast);
                resize: none;
            }}
            #username:focus, #comment-input:focus {{
                border-color: var(--accent);
                box-shadow: 0 0 0 3px var(--accent-glow);
            }}
            #username::placeholder, #comment-input::placeholder {{
                color: var(--text-tertiary);
            }}

            .comment-actions {{
                display: flex;
                align-items: center;
                gap: 8px;
            }}
            .file-upload-btn {{
                display: inline-flex;
                align-items: center;
                gap: 6px;
                padding: 8px 14px;
                border: 1px dashed var(--border);
                border-radius: var(--radius-sm);
                background: var(--bg-surface);
                color: var(--text-secondary);
                font-family: inherit;
                font-size: 0.8rem;
                cursor: pointer;
                transition: all var(--transition-fast);
            }}
            .file-upload-btn:hover {{
                border-color: var(--accent);
                color: var(--accent);
            }}
            .file-upload-btn.has-file {{
                border-style: solid;
                border-color: #22c55e;
                color: #22c55e;
                background: rgba(34, 197, 94, 0.06);
            }}
            #image-input {{ display: none; }}

            #submit-btn {{
                padding: 10px 24px;
                border: none;
                border-radius: var(--radius-sm);
                background: linear-gradient(135deg, var(--accent), var(--accent-secondary));
                color: #fff;
                font-family: inherit;
                font-size: 0.85rem;
                font-weight: 600;
                cursor: pointer;
                transition: all var(--transition-fast);
                box-shadow: 0 2px 8px var(--accent-glow);
            }}
            #submit-btn:hover {{
                transform: translateY(-1px);
                box-shadow: 0 6px 20px var(--accent-glow);
            }}
            #submit-btn:active {{
                transform: scale(0.96);
            }}

            #comment-list {{
                padding: 8px 20px;
                max-height: 320px;
                overflow-y: auto;
            }}
            #comment-list::-webkit-scrollbar {{
                width: 4px;
            }}
            #comment-list::-webkit-scrollbar-track {{
                background: transparent;
            }}
            #comment-list::-webkit-scrollbar-thumb {{
                background: var(--border);
                border-radius: 10px;
            }}

            .comment {{
                padding: 14px 0;
                border-bottom: 1px solid var(--border);
            }}
            .comment:last-child {{ border-bottom: none; }}
            .comment-user {{
                font-weight: 600;
                font-size: 0.85rem;
                color: var(--accent);
            }}
            .comment-meta {{
                font-size: 0.7rem;
                color: var(--text-tertiary);
                margin-left: 8px;
            }}
            .comment-text {{
                font-size: 0.875rem;
                color: var(--text-primary);
                line-height: 1.55;
                margin-top: 4px;
            }}
            .comment-thumb {{
                max-width: 160px;
                max-height: 120px;
                width: auto;
                height: auto;
                object-fit: cover;
                border-radius: var(--radius-xs);
                cursor: zoom-in;
                margin-top: 8px;
                border: 1px solid var(--border);
                transition: transform var(--transition-fast);
            }}
            .comment-thumb:hover {{
                transform: scale(1.03);
            }}
            .empty {{
                color: var(--text-tertiary);
                font-size: 0.85rem;
                padding: 30px 0;
                text-align: center;
            }}

            /* ---- 图片弹窗（Image Lightbox）----
               fixed 全屏覆盖 + backdrop-filter 毛玻璃背景，
               fadeIn 淡入 + scaleIn 缩放弹入双重动画，
               点击背景或 ESC 键关闭。 */
            #image-modal {{
                position: fixed;
                inset: 0;
                background: rgba(0,0,0,0.85);
                backdrop-filter: blur(12px);
                display: none;
                align-items: center;
                justify-content: center;
                z-index: 9999;
                animation: fadeIn 0.2s var(--ease-out-expo);
            }}
            @keyframes fadeIn {{
                from {{ opacity: 0; }}
                to {{ opacity: 1; }}
            }}
            #image-modal .modal-content {{
                max-width: 95vw;
                max-height: 90vh;
                background: var(--bg-surface);
                border-radius: var(--radius-md);
                position: relative;
                padding: 12px;
                box-shadow: var(--shadow-lg);
                animation: scaleIn 0.3s var(--ease-out-back);
            }}
            @keyframes scaleIn {{
                from {{ transform: scale(0.9); opacity: 0; }}
                to {{ transform: scale(1); opacity: 1; }}
            }}
            #image-modal img {{
                max-width: 90vw;
                max-height: 82vh;
                width: auto;
                height: auto;
                object-fit: contain;
                display: block;
                border-radius: var(--radius-xs);
            }}
            #image-modal .close-btn {{
                position: absolute;
                right: -12px;
                top: -12px;
                width: 32px;
                height: 32px;
                border-radius: 50%;
                background: var(--bg-elevated);
                border: 1px solid var(--border);
                color: var(--text-primary);
                cursor: pointer;
                font-weight: 600;
                font-size: 0.85rem;
                display: flex;
                align-items: center;
                justify-content: center;
                transition: all var(--transition-fast);
                z-index: 1;
            }}
            #image-modal .close-btn:hover {{
                background: #ef4444;
                color: #fff;
                border-color: #ef4444;
            }}

            /* ---- 音量条（Volume Indicator）----
               绝对定位于播放器右侧，默认隐藏透明，
               键盘 ↑↓ 或触摸手势触发显示，1.8s 后自动淡出。
               填充色为 accent 渐变 + 光晕效果。 */
            #volume-bar {{
                position: absolute;
                top: 50%;
                right: 24px;
                transform: translateY(-50%);
                width: 5px;
                height: 100px;
                background: rgba(255,255,255,0.08);
                border-radius: 10px;
                display: flex;
                flex-direction: column-reverse;
                align-items: center;
                z-index: 100;
                opacity: 0;
                transition: opacity var(--transition-normal);
                pointer-events: none;
                overflow: hidden;
            }}
            #volume-bar.show {{ opacity: 1; }}
            #volume-fill {{
                width: 100%;
                background: linear-gradient(to top, var(--accent), var(--accent-secondary));
                border-radius: 10px;
                transition: height 0.2s var(--ease-out-expo);
                box-shadow: 0 0 12px var(--accent-glow);
            }}
            #volume-percentage {{
                position: absolute;
                top: -28px;
                left: 50%;
                transform: translateX(-50%);
                background: var(--bg-elevated);
                backdrop-filter: blur(8px);
                color: var(--text-primary);
                font-size: 11px;
                font-weight: 600;
                padding: 3px 8px;
                border-radius: 6px;
                white-space: nowrap;
                border: 1px solid var(--border);
            }}

            /* ---- 响应式设计（Responsive Breakpoints）----
               1024px: 平板/小屏幕 → 主布局变为纵向排列，右侧面板全宽，
               播放列表 Grid 列宽缩小，最大高度限制 400px。
               640px: 手机 → 进一步缩小间距、字体、按钮尺寸，
               评论区输入框纵向排列，隐藏键盘快捷键提示。 */
            @media (max-width: 1024px) {{
                .main-content {{
                    flex-direction: column;
                    padding: 12px;
                    gap: 12px;
                }}
                .right-panel {{
                    width: 100%;
                    position: static;
                    max-height: none;
                }}
                #playlist {{
                    grid-template-columns: repeat(auto-fill, minmax(130px, 1fr));
                    gap: 8px;
                    max-height: 400px;
                }}
                .navbar-inner {{ padding: 10px 16px; }}
                .navbar-title {{ font-size: 1.1rem; }}
            }}

            @media (max-width: 640px) {{
                .main-content {{ padding: 8px; gap: 8px; }}
                .navbar-inner {{ padding: 8px 12px; }}
                .navbar-title {{ font-size: 1rem; }}
                .btn {{ padding: 8px 12px; font-size: 0.8rem; }}
                .navbar-logo {{ width: 30px; height: 30px; font-size: 14px; }}
                .player-controls {{ padding: 10px 14px; gap: 4px; }}
                .ctrl-btn {{ width: 38px; height: 38px; font-size: 1rem; }}
                .ctrl-key-hint {{ display: none; }}
                #playlist {{
                    grid-template-columns: repeat(auto-fill, minmax(110px, 1fr));
                    gap: 6px;
                    padding: 8px;
                }}
                .video-name {{ font-size: 0.75rem; }}
                .video-info {{ padding: 8px 10px; }}
                #comment-controls {{ padding: 12px 14px; }}
                .comment-row {{ flex-direction: column; }}
                #username {{ flex: none; }}
                #comment-list {{ padding: 8px 14px; max-height: 250px; }}
                .comment-thumb {{ max-width: 120px; max-height: 90px; }}
                #image-modal .modal-content {{ max-width: 98vw; max-height: 94vh; padding: 6px; }}
                #image-modal img {{ max-width: 94vw; max-height: 88vh; }}
            }}
        </style>
    </head>
    <body>
        <!-- 背景氛围光球：三个固定定位的模糊彩色光球，营造沉浸式视觉氛围 -->
        <div class="bg-orb bg-orb-1"></div>
        <div class="bg-orb bg-orb-2"></div>
        <div class="bg-orb bg-orb-3"></div>

        <!-- 导航栏：品牌 LOGO + 主题切换按钮 -->
        <nav class="navbar">
            <div class="navbar-inner">
                <a class="navbar-brand" href="/">
                    <div class="navbar-logo">▶</div>
                    <span class="navbar-title">Muse<span>Play</span></span>
                </a>
                <div class="navbar-actions">
                    <button class="btn btn-theme" id="theme-toggle">
                        <span class="icon-sun">☀️</span>
                        <span class="icon-moon">🌙</span>
                        <span id="theme-label">深色</span>
                    </button>
                </div>
            </div>
        </nav>

        <!-- 主布局容器：left-panel（播放器+评论）+ right-panel（播放列表） -->
        <div class="main-content">
            <div class="left-panel">
                <!-- 播放器卡片：标题栏 + DPlayer 播放器 + 控制按钮 -->
                <div class="player-wrapper">
                    <div class="player-title-bar">
                        <div class="player-title-dot"></div>
                        <span id="video-title">选择一个视频开始播放</span>
                        <span class="player-title-badge" id="now-playing-badge">正在播放</span>
                        <span class="player-title-badge" id="cache-badge" style="background:#22c55e;display:none;">✓ 已缓存</span>
                    </div>
                    <div id="player-container">
                        <div id="dplayer"></div>
                    </div>
                    <div class="player-controls">
                        <button id="prev-btn" class="ctrl-btn" title="上一个 (A)">⏮</button>
                        <button id="pip-btn" class="ctrl-btn" title="画中画">🖼</button>
                        <button id="next-btn" class="ctrl-btn" title="下一个 (D)">⏭</button>
                        <div class="ctrl-divider"></div>
                        <span class="ctrl-key-hint">Space 播放/暂停 · ← → 快退/进 · ↑ ↓ 音量</span>
                    </div>
                </div>

                <!-- 评论区 -->
                <div class="comments-panel">
                    <div class="comments-header">
                        <h3>💬 评论</h3>
                        <span class="comments-count" id="comments-count">0</span>
                    </div>
                    <div id="comment-controls">
                        <div class="comment-row">
                            <input id="username" placeholder="昵称（选填）">
                            <input id="comment-input" placeholder="写下你的评论...">
                        </div>
                        <div class="comment-actions">
                            <label class="file-upload-btn" id="file-upload-label" for="image-input">
                                🖼 添加图片
                            </label>
                            <input id="image-input" type="file" accept="image/*">
                            <button id="submit-btn">发布</button>
                        </div>
                    </div>
                    <div id="comment-list"></div>
                </div>
            </div>

            <div class="right-panel">
                <!-- 播放列表面板：搜索框 + 视频卡片网格 -->
                <div class="playlist-panel">
                    <div class="playlist-header">
                        <div class="playlist-header-title">📂 播放列表 · <span id="playlist-total">0</span> 个视频</div>
                        <div class="search-box">
                            <input type="text" id="search-input" placeholder="搜索视频...">
                            <span class="search-icon">🔍</span>
                        </div>
                    </div>
                    <div id="playlist"></div>
                </div>
            </div>
        </div>

        <!-- 图片弹窗：点击评论图片后全屏预览，点击背景或关闭按钮退出 -->
        <div id="image-modal" onclick="closeModal(event)">
            <div class="modal-content">
                <button class="close-btn" onclick="hideModal()">✕</button>
                <img id="modal-image" src="" alt="原图预览">
            </div>
        </div>

        <script>
            /* ============================================================
               JavaScript 主逻辑模块
               1. 主题管理       2. H.265 检测      3. DPlayer 初始化
               4. 视频切换       5. 画中画          6. 模糊搜索
               7. 播放列表渲染   8. 评论系统        9. Seek 拦截
               10. 键盘快捷键   11. 触摸手势
               ============================================================ */
            const playlist = {playlist_js};
            let current = 0;
            let allCards = [];
            let supportsH265 = false;

            // ---- 主题管理（Theme Toggle）----
            // 从 localStorage 读取已保存主题，默认深色。
            // 点击按钮切换 data-theme 属性，同时更新图标/标签并保存到 localStorage。
            const savedTheme = localStorage.getItem("theme") || "dark";
            document.body.setAttribute("data-theme", savedTheme);
            const themeLabel = document.getElementById('theme-label');
            const updateThemeLabel = (t) => {{ themeLabel.textContent = t === 'dark' ? '深色' : '浅色'; }};
            updateThemeLabel(savedTheme);

            document.getElementById('theme-toggle').addEventListener('click', () => {{
                const currentTheme = document.body.getAttribute('data-theme');
                const newTheme = currentTheme === 'dark' ? 'light' : 'dark';
                document.body.setAttribute('data-theme', newTheme);
                localStorage.setItem('theme', newTheme);
                updateThemeLabel(newTheme);
            }});

            // ---- H.265/HEVC 解码能力检测 ----
            // 使用 MSE API 检测浏览器是否原生支持 H.265 解码。
            // 不支持时自动请求服务器转码为 H.264 流。
            function detectH265Support() {{
                if (!MediaSource || !MediaSource.isTypeSupported) {{
                    supportsH265 = false;
                    return Promise.resolve(false);
                }}
                supportsH265 = MediaSource.isTypeSupported('video/mp4; codecs="hev1.1.6.L93.90"');
                return Promise.resolve(supportsH265);
            }}

            // ---- 恢复播放器状态 ----
            // 从 localStorage 读取上次观看的视频索引，实现记忆功能。
            const savedState = JSON.parse(localStorage.getItem("playerState") || "{{}}");
            if (savedState.index !== undefined) current = savedState.index;

            // ---- 播放列表总数 ----
            // 将视频总数写入页面头部，如 "📂 播放列表 · 7 个视频"
            document.getElementById('playlist-total').textContent = playlist.length;

            // ---- 初始化 DPlayer（视频播放器核心）----
            // 先检测 H.265 支持，再创建 DPlayer 实例。
            // 不支持 H.265 时自动给 URL 追加 ?transcode=1 参数，
            // 重写 switchVideo 方法确保所有视频切换都附带转码标记。
            detectH265Support().then(() => {{
                const transcode = supportsH265 ? '' : '?transcode=1';
                const initialUrl = playlist[current]?.url + transcode || '';
                const dp = new DPlayer({{
                    container: document.getElementById('dplayer'),
                    video: {{ url: initialUrl, type: 'auto' }},
                    screenshot: true,
                    thumbnails: playlist[current]?.thumbnails || '',
                    fit: 'cover',
                    notice: false
                }});

                const originalSwitchVideo = dp.switchVideo;
                dp.switchVideo = function(options) {{
                    if (!supportsH265 && !options.url.includes('transcode=1')) {{
                        options.url = options.url + (options.url.includes('?') ? '&' : '?') + 'transcode=1';
                    }}
                    originalSwitchVideo.call(this, options);
                }};

                // ---- 自定义通知（Custom Notice with Debounce）----
                // 防止短时间内重复弹通知（300ms 防抖），
                // 自动清除上一个通知以保证界面不重叠。
                let lastNotificationTime = 0;
                const DEBOUNCE_MS = 300;
                let currentNoticeTimeout = null;

                function showCustomNotice(text, duration = 1800) {{
                    const now = Date.now();
                    if (now - lastNotificationTime < DEBOUNCE_MS) return;
                    lastNotificationTime = now;
                    if (currentNoticeTimeout) clearTimeout(currentNoticeTimeout);
                    document.querySelectorAll('.dplayer-notice').forEach(n => n.style.display = 'none');
                    dp.notice(text, duration);
                    currentNoticeTimeout = setTimeout(() => {{
                        const el = document.querySelector('.dplayer-notice');
                        if (el) el.style.display = 'none';
                    }}, duration);
                }}

                // ---- 音量条（Dynamic Volume Bar）----
                // 动态创建 DOM 元素挂在播放器容器内，1.8s 后自动隐藏。
                // 由键盘 ↑↓ 键和触摸手势触发显示。
                let volumeBarTimeout = null;
                function showVolumeBar(volume) {{
                    let bar = document.getElementById('volume-bar');
                    if (!bar) {{
                        bar = document.createElement('div');
                        bar.id = 'volume-bar';
                        const fill = document.createElement('div');
                        fill.id = 'volume-fill';
                        const pct = document.createElement('div');
                        pct.id = 'volume-percentage';
                        bar.appendChild(fill);
                        bar.appendChild(pct);
                        document.getElementById('player-container').appendChild(bar);
                    }}
                    const fill = document.getElementById('volume-fill');
                    const pct = document.getElementById('volume-percentage');
                    const v = Math.round(volume * 100);
                    fill.style.height = v + '%';
                    pct.textContent = v + '%';
                    bar.classList.add('show');
                    if (volumeBarTimeout) clearTimeout(volumeBarTimeout);
                    volumeBarTimeout = setTimeout(() => bar.classList.remove('show'), 1800);
                }}

                // ---- 视频切换（Video Switching）----
                // 切换视频时同步更新标题、评论、浏览量、高亮卡片和播放状态。
                // 将当前索引和时间存入 localStorage 实现状态持久化。
                function switchToVideo(index) {{
                    if (index < 0 || index >= playlist.length) return;
                    current = index;
                    const transcode = supportsH265 ? '' : '?transcode=1';
                    dp.switchVideo({{ url: playlist[current].url + transcode, type: 'auto', thumbnails: playlist[current].thumbnails }});
                    dp.play();
                    loadComments();
                    updateViews(playlist[current].name);
                    updateVideoTitle();
                    highlightActiveCard();
                    localStorage.setItem("playerState", JSON.stringify({{ index: current, time: 0 }}));
                    updateButtons();
                }}

                function prevVideo() {{ switchToVideo(current - 1); }}
                function nextVideo() {{ switchToVideo((current + 1) % playlist.length); }}

                function updateButtons() {{
                    document.getElementById('prev-btn').disabled = current === 0;
                    document.getElementById('next-btn').disabled = current === playlist.length - 1;
                }}

                function updateVideoTitle() {{
                    const title = playlist[current]?.name || '选择一个视频开始播放';
                    document.getElementById('video-title').innerText = title;
                    const badge = document.getElementById('now-playing-badge');
                    if (playlist[current]) {{
                        badge.classList.add('visible');
                    }} else {{
                        badge.classList.remove('visible');
                    }}
                }}

                function highlightActiveCard() {{
                    allCards.forEach((card, i) => card.classList.toggle('active', i === current));
                }}

                // ---- 浏览量（View Counter）----
                // 每次切换视频时 POST 到 /click 接口更新计数，
                // 响应中的最新数字会实时更新卡片上的浏览量显示。
                async function updateViews(filename) {{
                    try {{
                        const res = await fetch('/click', {{
                            method: 'POST',
                            headers: {{'Content-Type': 'application/json'}},
                            body: JSON.stringify({{filename: filename}})
                        }});
                        const data = await res.json();
                        if (data.ok) {{
                            const viewsEl = allCards[current]?.querySelector('.video-meta-views');
                            if (viewsEl) viewsEl.innerHTML = '👁 ' + data.views;
                        }}
                    }} catch (err) {{ console.error('Error updating views:', err); }}
                }}

                // ---- 画中画（Picture-in-Picture）----
                // 利用浏览器原生 PiP API，点击按钮进入/退出画中画模式。
                // 通过事件监听更新 isPip 状态标志。
                let isPip = false;
                document.getElementById('pip-btn').addEventListener('click', async () => {{
                    if (!document.pictureInPictureEnabled) {{
                        alert('浏览器不支持画中画');
                        return;
                    }}
                    try {{
                        isPip ? await document.exitPictureInPicture() : await dp.video.requestPictureInPicture();
                    }} catch (e) {{ console.error('PiP error:', e); }}
                }});
                dp.video.addEventListener('enterpictureinpicture', () => {{ isPip = true; }});
                dp.video.addEventListener('leavepictureinpicture', () => {{ isPip = false; }});

                // ---- DPlayer 事件监听 ----
                // ended → 自动播放下一个视频。
                // timeupdate → 持续保存当前播放位置到 localStorage。
                // loadedmetadata → 恢复上次观看位置（seek），并加载评论。
                dp.on('ended', () => nextVideo());
                dp.on('timeupdate', () => {{
                    localStorage.setItem("playerState", JSON.stringify({{ index: current, time: dp.video.currentTime }}));
                }});
                dp.on('loadedmetadata', () => {{
                    const state = JSON.parse(localStorage.getItem("playerState") || "{{}}");
                    if (state.index === current && state.time) dp.seek(state.time);
                    loadComments();
                    updateButtons();
                }});

                // ---- 模糊搜索系统（Fuzzy Search）----
                // 使用编辑距离（Levenshtein）算法实现中英文模糊匹配。
                // 支持 AND 组合搜索和 OR 分组搜索（用 "or" 分隔）。
                // 匹配容差为关键词长度的 20% 或最多 2 个字符差异。
                function levenshteinDistance(str1, str2) {{
                    const m = [];
                    for (let i = 0; i <= str2.length; i++) {{ m[i] = [i]; }}
                    for (let j = 0; j <= str1.length; j++) {{ m[0][j] = j; }}
                    for (let i = 1; i <= str2.length; i++) {{
                        for (let j = 1; j <= str1.length; j++) {{
                            m[i][j] = str2.charAt(i-1) === str1.charAt(j-1)
                                ? m[i-1][j-1]
                                : Math.min(m[i-1][j-1]+1, m[i][j-1]+1, m[i-1][j]+1);
                        }}
                    }}
                    return m[str2.length][str1.length];
                }}

                function fuzzyMatch(query, name) {{
                    const nq = query.toLowerCase().replace(/[^a-z0-9\\u4e00-\\u9fff]/g, '');
                    const nn = name.toLowerCase().replace(/[^a-z0-9\\u4e00-\\u9fff]/g, '');
                    if (nn.includes(nq)) return true;
                    const dist = levenshteinDistance(nq, nn);
                    return dist <= Math.max(nq.length * 0.2, 2);
                }}

                function parseQuery(query) {{
                    const orGroups = query.toLowerCase().split(/\\s+or\\s+/i).map(g => g.split(/\\s+/).filter(t => t));
                    if (orGroups.length > 1) {{
                        return name => orGroups.some(g => g.every(t => fuzzyMatch(t, name)));
                    }}
                    const terms = orGroups[0].filter(t => t);
                    return name => terms.every(t => fuzzyMatch(t, name));
                }}

                // ---- 渲染播放列表（Playlist Rendering）----
                // 为每个视频动态创建卡片元素（缩略图 + 信息），
                // 缩略图采用懒加载 + 加载失败时显示 SVG 占位图。
                const listDiv = document.getElementById('playlist');
                playlist.forEach((item, index) => {{
                    const card = document.createElement('div');
                    card.className = 'video-card';
                    if (index === current) card.classList.add('active');
                    card.onclick = () => switchToVideo(index);
                    card.dataset.name = item.name.toLowerCase();

                    // 缩略图区域
                    const thumbWrap = document.createElement('div');
                    thumbWrap.className = 'video-thumb-wrap';
                    const img = document.createElement('img');
                    img.className = 'video-thumb';
                    img.src = item.thumbnails;
                    img.loading = 'lazy';
                    img.onerror = () => {{ img.src = 'data:image/svg+xml,<svg xmlns="http://www.w3.org/2000/svg" width="200" height="120" fill="%23333"><rect width="200" height="120"/><text x="100" y="65" text-anchor="middle" fill="%23666" font-size="14">无缩略图</text></svg>'; }};
                    const overlay = document.createElement('div');
                    overlay.className = 'video-thumb-overlay';
                    const duration = document.createElement('span');
                    duration.className = 'video-duration';
                    duration.textContent = '视频';
                    thumbWrap.appendChild(img);
                    thumbWrap.appendChild(overlay);
                    thumbWrap.appendChild(duration);

                    // 信息区域
                    const info = document.createElement('div');
                    info.className = 'video-info';
                    const nameEl = document.createElement('div');
                    nameEl.className = 'video-name';
                    nameEl.textContent = item.name;
                    const meta = document.createElement('div');
                    meta.className = 'video-meta';
                    const viewsEl = document.createElement('span');
                    viewsEl.className = 'video-meta-views';
                    viewsEl.innerHTML = '👁 ' + item.views;
                    meta.appendChild(viewsEl);
                    info.appendChild(nameEl);
                    info.appendChild(meta);

                    card.appendChild(thumbWrap);
                    card.appendChild(info);
                    listDiv.appendChild(card);
                    allCards.push(card);
                }});

                // ---- 搜索输入处理 ----
                // input 事件驱动，实时过滤播放列表卡片。
                // 空查询恢复显示所有卡片。
                document.getElementById('search-input').addEventListener('input', (e) => {{
                    const query = e.target.value.trim();
                    if (!query) {{
                        allCards.forEach(c => c.style.display = 'flex');
                        return;
                    }}
                    const matcher = parseQuery(query);
                    allCards.forEach(c => {{
                        c.style.display = matcher(c.dataset.name) ? 'flex' : 'none';
                    }});
                }});

                // ---- 控制按钮绑定 ----
                // 上一个/下一个按钮绑定 prevVideo/nextVideo 处理函数。
                document.getElementById('prev-btn').addEventListener('click', prevVideo);
                document.getElementById('next-btn').addEventListener('click', nextVideo);

                // ---- 转码视频 Seek 拦截逻辑 ----
                // 问题背景：实时转码流是 chunked transfer，浏览器无法获取
                // Content-Length，导致原生进度条拖动无效（无法计算字节偏移量）。
                // 解决方案：
                //   1. 如果转码已完成（缓存文件存在）→ 使用原生 seek，完美支持拖放
                //   2. 如果仍在转码中 → 拦截 dp.seek()，用 ?t= 参数重新请求流
                // 缓存轮询：每 3 秒查询 /cache_status 接口，转码完成后通知用户刷新。
                // 缓存文件：原生 seek 完美工作（静态 MP4，有 Content-Length）
                // 实时转码流：没有固定长度，拦截 dp.seek() 用 ?t= 参数重载
                if (!supportsH265) {{
                    let cachePollTimer = null;
                    const cacheBadge = document.getElementById('cache-badge');

                    dp.video.addEventListener('loadedmetadata', function() {{
                        dp.video._isCached = (dp.video.seekable.length > 0 && isFinite(dp.video.duration));
                        if (dp.video._isCached) {{
                            cacheBadge.style.display = 'inline-block';
                            if (cachePollTimer) {{ clearInterval(cachePollTimer); cachePollTimer = null; }}
                        }} else {{
                            cacheBadge.style.display = 'none';
                            // 轮询检查缓存是否完成
                            if (!cachePollTimer) {{
                                cachePollTimer = setInterval(() => {{
                                    const fname = encodeURIComponent(playlist[current]?.name || '');
                                    if (!fname) return;
                                    fetch('/cache_status/' + fname)
                                        .then(r => r.json())
                                        .then(data => {{
                                            if (data.cached) {{
                                                cacheBadge.style.display = 'inline-block';
                                                clearInterval(cachePollTimer);
                                                cachePollTimer = null;
                                                showCustomNotice('GPU 转码完成，刷新页面获得完整播放体验');
                                            }}
                                        }}).catch(() => {{}});
                                }}, 3000);
                            }}
                        }}
                    }});

                    let seeking = false;
                    const nativeSeek = dp.seek.bind(dp);
                    dp.seek = function(time) {{
                        if (dp.video._isCached) {{
                            nativeSeek(time);   // 缓存文件原生 seek
                            return;
                        }}
                        if (seeking) return;
                        seeking = true;
                        const url = playlist[current].url + '?transcode=1&t=' + Math.floor(time);
                        showCustomNotice('跳转中...', 2000);
                        dp.video.pause();
                        dp.switchVideo({{ url: url, type: 'auto', thumbnails: playlist[current].thumbnails }});
                        dp.video.addEventListener('canplay', function onCanPlay() {{
                            dp.video.removeEventListener('canplay', onCanPlay);
                            seeking = false;
                        }}, {{ once: true }});
                        dp.play();
                    }};
                }}

                updateVideoTitle();
                updateButtons();

                // ---- 评论系统（Comment System）----
                // loadComments(): 从 /comments/<filename> 获取评论列表并渲染 HTML。
                // 支持图片评论，点击缩略图可放大预览。
                // 提交: FormData POST 到 /comment，支持文本+图片。
                function loadComments() {{
                    const video = playlist[current]?.name || '';
                    const countEl = document.getElementById('comments-count');
                    if (!video) {{ countEl.textContent = '0'; return; }}
                    fetch('/comments/' + encodeURIComponent(video))
                        .then(r => r.ok ? r.json() : [])
                        .then(data => {{
                            const list = document.getElementById('comment-list');
                            list.innerHTML = '';
                            countEl.textContent = data.length;
                            if (!data || data.length === 0) {{
                                list.innerHTML = '<div class="empty">✨ 还没有评论，来占个楼吧～</div>';
                                return;
                            }}
                            data.forEach(c => {{
                                const div = document.createElement('div');
                                div.className = 'comment';
                                const user = (c.user || '匿名').slice(0, 32);
                                const text = (c.text || '').slice(0, 2000);
                                const tsStr = new Date((c.ts || 0) * 1000).toLocaleString('zh-CN');
                                div.innerHTML = '<span class="comment-user"></span><span class="comment-meta"></span><div class="comment-text"></div>';
                                div.querySelector('.comment-user').textContent = user;
                                div.querySelector('.comment-meta').textContent = tsStr;
                                div.querySelector('.comment-text').textContent = text;
                                if (c.image) {{
                                    const img = document.createElement('img');
                                    img.className = 'comment-thumb';
                                    img.src = c.image;
                                    img.alt = '评论图片';
                                    img.title = '点击查看原图';
                                    img.onclick = () => showModal(c.image);
                                    div.appendChild(img);
                                }}
                                list.appendChild(div);
                            }});
                        }})
                        .catch(() => {{
                            document.getElementById('comment-list').innerHTML = '<div class="empty">评论加载失败</div>';
                            countEl.textContent = '0';
                        }});
                }}

                document.getElementById('submit-btn').addEventListener('click', () => {{
                    const user = (document.getElementById('username').value || '匿名').trim();
                    const text = (document.getElementById('comment-input').value || '').trim();
                    const file = document.getElementById('image-input').files[0];
                    if (!text && !file) return;
                    const fd = new FormData();
                    fd.append('filename', playlist[current]?.name || '');
                    fd.append('user', user);
                    fd.append('text', text);
                    if (file) fd.append('image', file);
                    fetch('/comment', {{ method: 'POST', body: fd }})
                        .then(r => r.json())
                        .then(res => {{
                            if (res.ok) {{
                                document.getElementById('comment-input').value = '';
                                document.getElementById('image-input').value = '';
                                document.getElementById('file-upload-label').classList.remove('has-file');
                                loadComments();
                            }} else {{ alert(res.msg || '提交失败'); }}
                        }})
                        .catch(() => alert('网络错误，提交失败'));
                }});

                // ---- 文件上传状态指示 ----
                // 监听图片文件选择，更新上传按钮样式和文件名显示。
                document.getElementById('image-input').addEventListener('change', function() {{
                    const label = document.getElementById('file-upload-label');
                    if (this.files.length > 0) {{
                        label.classList.add('has-file');
                        label.innerHTML = '✓ ' + this.files[0].name;
                    }} else {{
                        label.classList.remove('has-file');
                        label.innerHTML = '🖼 添加图片';
                    }}
                }});

                // ---- 图片弹窗（Image Modal）----
                // showModal(src): 设置图片 src 并显示全屏弹窗。
                // hideModal(): 隐藏弹窗并清空 src。
                // closeModal(e): 点击背景遮罩关闭（事件委托判断 target.id）。
                function showModal(src) {{
                    const modal = document.getElementById('image-modal');
                    document.getElementById('modal-image').src = src;
                    modal.style.display = 'flex';
                }}
                function hideModal() {{
                    document.getElementById('image-modal').style.display = 'none';
                    document.getElementById('modal-image').src = '';
                }}
                function closeModal(e) {{ if (e.target.id === 'image-modal') hideModal(); }}
                // ---- ESC 关闭图片弹窗 ----
                // 监听全局 keydown 事件，按下 Escape 键时关闭图片弹窗。
                document.addEventListener('keydown', (e) => {{
                    if (e.key === 'Escape') hideModal();
                }});

                // ---- 键盘快捷键（Keyboard Shortcuts）----
                // Space:  播放/暂停（输入框中不触发）
                // ←/→:    快退/快进 5 秒
                // ↑/↓:    音量增减 10%
                // A/D:    上一个/下一个视频
                // ESC:    关闭图片弹窗
                const volumeStep = 0.1;
                document.addEventListener('keydown', function(e) {{
                    const ae = document.activeElement;
                    const isInput = ae.tagName === 'INPUT' || ae.tagName === 'TEXTAREA';
                    switch(e.code) {{
                        case 'Space':
                            if (isInput) return;
                            e.preventDefault();
                            dp.video.paused ? (dp.play(), showCustomNotice('▶ 播放')) : (dp.pause(), showCustomNotice('⏸ 暂停'));
                            break;
                        case 'ArrowRight':
                            if (isInput) return;
                            e.preventDefault();
                            const nt = dp.video.currentTime + 5;
                            dp.seek(nt);
                            showCustomNotice('⏩ +5s');
                            break;
                        case 'ArrowLeft':
                            if (isInput) return;
                            e.preventDefault();
                            const pt = Math.max(dp.video.currentTime - 5, 0);
                            dp.seek(pt);
                            showCustomNotice('⏪ -5s');
                            break;
                        case 'ArrowUp':
                            if (isInput) return;
                            e.preventDefault();
                            const vu = Math.min(dp.video.volume + volumeStep, 1);
                            dp.volume(vu, true, true);
                            showVolumeBar(vu);
                            break;
                        case 'ArrowDown':
                            if (isInput) return;
                            e.preventDefault();
                            const vd = Math.max(dp.video.volume - volumeStep, 0);
                            dp.volume(vd, true, true);
                            showVolumeBar(vd);
                            break;
                        case 'KeyA':
                            if (isInput) return;
                            e.preventDefault();
                            prevVideo();
                            break;
                        case 'KeyD':
                            if (isInput) return;
                            e.preventDefault();
                            nextVideo();
                            break;
                    }}
                }});

                // ---- 触摸手势（Touch Gestures）----
                // 垂直滑动: 调节音量（上下滑动，初始值为当前音量）
                // 水平滑动: 切换视频（左滑下一个，右滑上一个，需超过 50px 阈值）
                // 点按: 不触发任何操作（减少误触）
                let touchStartX = 0, touchStartY = 0, touchEndX = 0, touchEndY = 0;
                let isTouching = false, isSwiping = false, initialVolume = 0;
                const SWIPE_THRESHOLD = 50, VOLUME_SENSITIVITY = 0.005;
                const pc = document.getElementById('player-container');

                pc.addEventListener('touchstart', (e) => {{
                    isTouching = true; isSwiping = false;
                    touchStartX = e.changedTouches[0].screenX;
                    touchStartY = e.changedTouches[0].screenY;
                    initialVolume = dp.video.volume;
                }});

                pc.addEventListener('touchmove', (e) => {{
                    if (!isTouching) return;
                    touchEndX = e.changedTouches[0].screenX;
                    touchEndY = e.changedTouches[0].screenY;
                    const dx = Math.abs(touchEndX - touchStartX);
                    const dy = Math.abs(touchEndY - touchStartY);
                    if (!isSwiping && (dx > 5 || dy > 5)) {{ isSwiping = true; e.preventDefault(); }}
                    if (dy > dx && isSwiping) {{
                        const volChange = -(touchEndY - touchStartY) * VOLUME_SENSITIVITY;
                        const nv = Math.max(0, Math.min(1, initialVolume + volChange));
                        dp.volume(nv, true, true);
                        showVolumeBar(nv);
                    }}
                }});

                pc.addEventListener('touchend', (e) => {{
                    if (!isTouching) return;
                    isTouching = false;
                    touchEndX = e.changedTouches[0].screenX;
                    touchEndY = e.changedTouches[0].screenY;
                    const dx = Math.abs(touchEndX - touchStartX);
                    const dy = Math.abs(touchEndY - touchStartY);
                    if (!isSwiping && dx < 10 && dy < 10) {{ /* tap */ }}
                    else if (dx > SWIPE_THRESHOLD && dy < SWIPE_THRESHOLD / 2) {{
                        touchEndX > touchStartX ? prevVideo() : nextVideo();
                    }}
                    isSwiping = false;
                }});

                if (playlist.length > 0) loadComments();
            }});
        </script>
    </body>
    </html>
    """
    return render_template_string(html)

# ------------- 接口 -------------

@app.route("/video/<path:filename>")
def video(filename):
    """视频文件路由，H.265 优先使用缓存，否则实时转码"""
    if not filename.lower().endswith(VIDEO_EXTS):
        logger.error(f"Invalid video extension: {filename}")
        abort(404)
    if ".." in filename or not os.path.exists(filename):
        logger.error(f"Invalid path or file not found: {filename}")
        abort(404)

    need_transcode = request.args.get('transcode') == '1'

    # H.265 视频：优先走缓存
    if need_transcode:
        codec = get_video_codec(filename)
        if codec == 'hevc':
            cache_file = _cache_key(filename)
            if os.path.exists(cache_file):
                # 已有缓存，直接当静态文件播放 → 完美支持拖进度条！
                logger.info(f"Serving cached: {cache_file}")
                return send_from_directory(CACHE_DIR, os.path.basename(cache_file),
                                           mimetype='video/mp4',
                                           as_attachment=False,
                                           conditional=True)
            else:
                # 还没缓存 → 启动后台 GPU 转码 + 同时走实时流
                start_background_transcode(filename)
                logger.info(f"No cache yet, starting background transcode + realtime fallback")
                # 走实时流（seek 参数 t 用于跳转）
                seek_time = 0
                try:
                    seek_time = float(request.args.get('t', 0))
                except ValueError:
                    pass
                generator = stream_transcoded_video(filename, seek_time)
                if generator:
                    return Response(generator(), mimetype='video/mp4', headers={
                        'Content-Type': 'video/mp4',
                        'Accept-Ranges': 'bytes'
                    })

    return send_from_directory("./", filename, as_attachment=False, conditional=True)

@app.route("/data/cover/<path:filename>")
def serve_thumbnail(filename):
    """缩略图路由"""
    if not filename.lower().endswith(IMAGE_EXTS):
        logger.error(f"Invalid thumbnail extension: {filename}")
        abort(404)
    thumbnail_path = os.path.join("./data/cover", filename)
    if ".." in thumbnail_path or not os.path.exists(thumbnail_path):
        logger.error(f"Invalid path or thumbnail not found: {filename}")
        abort(404)
    return send_from_directory("./data/cover", filename, as_attachment=False)

@app.route("/uploads/<path:filename>")
def serve_uploaded_image(filename):
    """评论图片路由"""
    if not filename.lower().endswith(IMAGE_EXTS):
        logger.error(f"Invalid uploaded image extension: {filename}")
        abort(404)
    image_path = os.path.join(UPLOAD_DIR, filename)
    if ".." in image_path or not os.path.exists(image_path):
        logger.error(f"Invalid path or image not found: {filename}")
        abort(404)
    return send_from_directory(UPLOAD_DIR, filename, as_attachment=False)

@app.route("/click", methods=["POST"])
def click():
    """
    点击视频卡片时增加浏览量。

    端点: POST /click
    请求格式: Content-Type: application/json
        {
            "filename": "example.mp4"   // 视频文件名（相对于运行目录）
        }
    响应格式:
        成功 (200): {"ok": true, "filename": "...", "views": 123}
        参数缺失 (400): {"ok": false, "msg": "缺少或无效的 filename"}
        文件不存在 (404): {"ok": false, "msg": "文件不存在"}

    安全策略:
        - 验证 filename 不为空且不包含 ".."（防止路径穿越）
        - 验证文件确实存在于文件系统

    数据流:
        1. 解析 JSON 请求体获取 filename
        2. 验证 filename 的有效性和文件存在性
        3. 从 views.json 加载浏览量数据
        4. 对应视频的浏览量 +1
        5. 原子写入 views.json
        6. 返回更新后的浏览量
    """
    data = request.get_json(silent=True) or {}
    filename = data.get("filename")
    if not filename or ".." in filename:
        logger.error("Click request missing or invalid filename")
        return jsonify(ok=False, msg="缺少或无效的 filename"), 400
    if not os.path.exists(filename):
        logger.error(f"File not found for click: {filename}")
        return jsonify(ok=False, msg="文件不存在"), 404

    views = load_views()
    views[filename] = views.get(filename, 0) + 1
    save_views(views)
    logger.info(f"Updated views for {filename}: {views[filename]}")
    return jsonify(ok=True, filename=filename, views=views[filename])

@app.route("/cache_status/<path:filename>")
def cache_status(filename):
    """查询视频缓存状态"""
    if ".." in filename or not os.path.exists(filename):
        return jsonify({"cached": False, "reason": "file not found"})
    codec = get_video_codec(filename)
    if codec != 'hevc':
        return jsonify({"cached": False, "reason": "not H.265, no cache needed"})
    cache_file = _cache_key(filename)
    cached = os.path.exists(cache_file)
    size = os.path.getsize(cache_file) if cached else 0
    return jsonify({
        "cached": cached,
        "size": size,
        "cache_file": cache_file if cached else None
    })

@app.route("/comments/<path:filename>", methods=["GET"])
def get_comments(filename):
    """
    获取指定视频的所有评论。

    端点: GET /comments/<path:filename>
    请求格式: URL 路径参数
        /comments/example.mp4

    响应格式:
        成功 (200): JSON 数组，按时间戳升序排列
        [
            {
                "user": "小明",
                "text": "这个视频不错",
                "ts": 1717200000,
                "image": "/uploads/cmt_xxx.jpg"  // 可选，有图片评论时才存在
            }
        ]

    排序策略:
        按评论的时间戳 ts 升序排列，保证评论区按发布时间从早到晚显示。

    安全策略:
        - 检查 filename 是否包含 ".."（防止路径穿越）
    """
    if ".." in filename:
        logger.error(f"Invalid filename in comments request: {filename}")
        abort(404)
    comments = load_comments()
    lst = comments.get(filename, [])
    lst_sorted = sorted(lst, key=lambda x: x.get("ts", 0))
    return jsonify(lst_sorted)

@app.route("/comment", methods=["POST"])
def add_comment():
    """
    提交视频评论，支持可选图片附件。

    端点: POST /comment
    请求格式: Content-Type: multipart/form-data
        filename: "example.mp4"   // 必填，评论所属的视频文件名
        user: "小明"              // 选填，用户名，默认 "匿名"
        text: "这个视频不错"       // 选填，评论文本（与 image 至少填一个）
        image: <file>             // 选填，图片文件

    响应格式:
        成功 (200): {"ok": true}
        参数无效 (400): {"ok": false, "msg": "错误描述"}

    输入验证与安全策略:
        - 验证 filename 不为空且不包含 ".."（路径穿越防护）
        - 用户名字段截断至 64 字符（防止超长用户名）
        - 评论文本截断至 4000 字符（防止恶意超长内容）
        - 图片文件需通过 is_valid_image() 验证后才保存
        - 图片文件名使用 secure_image_filename() 生成（防止路径穿越和重名）

    数据处理流程:
        1. 从 form 数据中提取 filename, user, text, image
        2. 验证必填字段和文件名安全性
        3. 处理图片上传（验证、生成安全文件名、保存到 uploads/）
        4. 构建评论对象并追加到 comments.json
        5. 原子写入保存
    """
    filename = (request.form.get("filename") or "").strip()
    user = (request.form.get("user") or "匿名").strip()
    text = (request.form.get("text") or "").strip()
    file = request.files.get("image")

    if not filename or ".." in filename:
        logger.error("Comment request missing or invalid filename")
        return jsonify(ok=False, msg="缺少或无效的 filename"), 400
    if not text and not file:
        logger.error("Comment request empty (no text or image)")
        return jsonify(ok=False, msg="评论内容不能为空"), 400

    # 输入长度限制：防止恶意请求包含超长内容占用过多存储
    user = user[:64]      # 用户名最多 64 字符
    text = text[:4000]    # 评论文本最多 4000 字符
    image_url = None
    if file and file.filename:
        _, ext = os.path.splitext(file.filename)
        ext = ext.lower() if ext.lower() in IMAGE_EXTS else ".jpg"
        if not is_valid_image(file):
            logger.error(f"Invalid image file uploaded")
            return jsonify(ok=False, msg="无效的图片文件"), 400
        file.seek(0)
        safe_name = secure_image_filename(file.filename)
        save_path = os.path.join(UPLOAD_DIR, safe_name)
        file.save(save_path)
        image_url = f"/{UPLOAD_DIR}/{safe_name}"
        logger.info(f"Saved comment image: {save_path}")

    comments = load_comments()
    comments.setdefault(filename, []).append({
        "user": user,
        "text": text,
        "ts": int(time.time()),
        "image": image_url
    })
    save_comments(comments)
    logger.info(f"Added comment for {filename}")
    return jsonify(ok=True)

# ========== 应用启动 ==========

if __name__ == "__main__":
    """
    Flask 应用入口点。

    启动参数说明:
        host="0.0.0.0": 监听所有网络接口，允许局域网内其他设备访问
                        可通过 http://本机IP:5000 在同一网络下访问
                        如需仅本机访问，改为 "127.0.0.1"
        port=5000:      使用 5000 端口，浏览器访问地址为 http://localhost:5000
        debug=True:     启用调试模式，特性如下:
                        - 代码热重载：修改代码后自动重启
                        - 详细错误页面：在浏览器中显示完整的错误堆栈
                        - 注意：生产环境应设置 debug=False

    建议:
        - Windows 首次启动可能需要防火墙放行 5000 端口
        - 生产部署建议使用 gunicorn + nginx 而非 Flask 内置服务器
    """
    try:
        logger.info("Starting Flask application")
        app.run(host="0.0.0.0", port=5000, debug=True)
    except Exception as e:
        logger.error(f"Application crashed: {e}")