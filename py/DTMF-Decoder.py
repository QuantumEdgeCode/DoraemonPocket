#! python
# -*- coding: UTF-8 -*-
'''
项目名称:pro
文件名:app.py
功能:DTMF 按键音解码器（UI 3.0）
说明:后端解码判定逻辑与原版保持一致，仅扩展片段元数据用于前端展示
'''
from flask import (Flask, request, render_template_string, url_for,
                   abort, send_from_directory, jsonify)
import os
import sqlite3
import uuid
from contextlib import contextmanager
from datetime import datetime

import numpy as np
import scipy.io.wavfile as wav
from scipy.signal import find_peaks

app = Flask(__name__)
BASE_DIR = os.path.dirname(os.path.abspath(__file__))
UPLOAD_FOLDER = os.path.join(BASE_DIR, 'uploads')
os.makedirs(UPLOAD_FOLDER, exist_ok=True)
app.config['MAX_CONTENT_LENGTH'] = 32 * 1024 * 1024  # 单文件上限 32MB
ALLOWED_EXT = {'.wav', '.wave'}
DB_PATH = os.path.join(BASE_DIR, 'dtmf_history.db')

# --- DTMF 频率表 ---
dtmf_freqs = {
    (697, 1209): '1', (697, 1336): '2', (697, 1477): '3',
    (770, 1209): '4', (770, 1336): '5', (770, 1477): '6',
    (852, 1209): '7', (852, 1336): '8', (852, 1477): '9',
    (941, 1209): '*', (941, 1336): '0', (941, 1477): '#'
}
low_freqs = [697, 770, 852, 941]
high_freqs = [1209, 1336, 1477]


def nearest_freq(freq, freq_list):
    return min(freq_list, key=lambda f: abs(f - freq))


def band_energy(mag, freqs, center, ratio=0.035):
    """中心频率 ±3.5% 频带内的能量，用于估算双音纯度"""
    mask = (freqs >= center * (1 - ratio)) & (freqs <= center * (1 + ratio))
    return float(np.sum(mag[mask] ** 2))


def detect_key_segment(segment, rate):
    """识别单个音频片段。

    判定条件与原版完全一致（find_peaks 高度 0.3、最近频率映射、查表），
    额外返回低/高频与置信度供 UI 展示。
    """
    if len(segment) < 64:
        return None
    fft_mag = np.abs(np.fft.rfft(segment))
    peak_max = float(np.max(fft_mag))
    if peak_max <= 0:
        return None
    freqs = np.fft.rfftfreq(len(segment), 1 / rate)
    peaks, _ = find_peaks(fft_mag, height=peak_max * 0.3)
    if len(peaks) < 2:
        return None

    order = np.argsort(fft_mag[peaks])[-2:]
    idx = np.sort(peaks[order])          # 按频率从低到高
    f_low, f_high = float(freqs[idx[0]]), float(freqs[idx[1]])
    low = nearest_freq(f_low, low_freqs)
    high = nearest_freq(f_high, high_freqs)
    key = dtmf_freqs.get((low, high))
    if key is None:
        return None

    # 置信度 = 两个 DTMF 频带能量 / 语音带(300-1800Hz)总能量
    voice = (freqs >= 300) & (freqs <= 1800)
    total = float(np.sum(fft_mag[voice] ** 2))
    signal = band_energy(fft_mag, freqs, low) + band_energy(fft_mag, freqs, high)
    conf = 0.0 if total <= 0 else min(1.0, signal / total)

    return {
        'key': key,
        'low': low,
        'high': high,
        'peak_low': round(f_low, 1),
        'peak_high': round(f_high, 1),
        'confidence': round(conf, 3),
    }


def decode_dtmf_segments(filename, frame_size=1024, energy_threshold=500):
    """解码整段音频，返回 (sequence, segments, meta)"""
    rate, data = wav.read(filename)
    channels = 1 if data.ndim == 1 else int(data.shape[1])
    if data.ndim > 1:
        data = data[:, 0]

    meta = {
        'rate': int(rate),
        'duration': round(len(data) / float(rate), 3) if rate else 0.0,
        'channels': channels,
    }

    data = data.astype(np.float32)
    peak = float(np.max(np.abs(data))) if data.size else 0.0
    if peak <= 0:
        return '', [], meta
    data /= peak

    # 分帧能量（向量化，等价于原版逐帧平方求和）
    n_frames = len(data) // frame_size
    if n_frames == 0:
        return '', [], meta
    energy = np.sum(data[:n_frames * frame_size].reshape(n_frames, frame_size) ** 2, axis=1)

    max_energy = float(np.max(energy))
    if max_energy <= 0:
        return '', [], meta
    threshold = energy_threshold / max_energy

    bounds = []
    in_segment = False
    seg_start = 0
    for i, e in enumerate(energy):
        if e > threshold:
            if not in_segment:
                in_segment = True
                seg_start = i * frame_size
        elif in_segment:
            in_segment = False
            bounds.append((seg_start, i * frame_size))
    if in_segment:
        bounds.append((seg_start, len(data)))

    sequence = []
    segments = []
    for start, end in bounds:
        info = detect_key_segment(data[start:end], rate)
        if info:
            sequence.append(info['key'])
            info['start'] = round(start / rate, 4)
            info['end'] = round(end / rate, 4)
            info['duration'] = round((end - start) / rate, 4)
            segments.append(info)

    meta['frames'] = int(n_frames)
    meta['threshold'] = round(threshold, 5)
    return ''.join(sequence), segments, meta


# --- 历史记录存储（SQLite） ---
SCHEMA = """
CREATE TABLE IF NOT EXISTS jobs (
    id             INTEGER PRIMARY KEY AUTOINCREMENT,
    filename       TEXT    NOT NULL,
    stored_name    TEXT    NOT NULL,
    size_bytes     INTEGER NOT NULL DEFAULT 0,
    rate           INTEGER,
    duration       REAL,
    channels       INTEGER,
    sequence       TEXT    NOT NULL DEFAULT '',
    key_count      INTEGER NOT NULL DEFAULT 0,
    avg_confidence REAL    NOT NULL DEFAULT 0,
    created_at     TEXT    NOT NULL
);
CREATE TABLE IF NOT EXISTS segments (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    job_id     INTEGER NOT NULL REFERENCES jobs(id) ON DELETE CASCADE,
    idx        INTEGER NOT NULL,
    key        TEXT    NOT NULL,
    start      REAL    NOT NULL,
    end        REAL    NOT NULL,
    low        INTEGER,
    high       INTEGER,
    confidence REAL
);
CREATE INDEX IF NOT EXISTS idx_jobs_created ON jobs(created_at DESC);
CREATE INDEX IF NOT EXISTS idx_segments_job ON segments(job_id);
"""


@contextmanager
def db():
    """每次请求独立连接，异常回滚，用完关闭"""
    conn = sqlite3.connect(DB_PATH, timeout=10)
    conn.row_factory = sqlite3.Row
    conn.execute('PRAGMA foreign_keys = ON')
    try:
        yield conn
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


def init_db():
    conn = sqlite3.connect(DB_PATH, timeout=10)
    try:
        conn.executescript(SCHEMA)
        conn.execute('PRAGMA journal_mode=WAL')
        conn.commit()
    finally:
        conn.close()


def save_history(result, stored, original, size_bytes):
    """写一条识别记录 + 明细片段，返回 (job_id, created_at)"""
    segs = result.get('segments') or []
    meta = result.get('meta') or {}
    avg = round(sum(float(s.get('confidence') or 0) for s in segs) / len(segs), 3) if segs else 0.0
    created = datetime.now().strftime('%Y-%m-%d %H:%M:%S')

    with db() as conn:
        cur = conn.execute(
            'INSERT INTO jobs (filename, stored_name, size_bytes, rate, duration, channels,'
            ' sequence, key_count, avg_confidence, created_at)'
            ' VALUES (?,?,?,?,?,?,?,?,?,?)',
            (original, stored, int(size_bytes), meta.get('rate'), meta.get('duration'),
             meta.get('channels'), result.get('sequence', ''), len(segs), avg, created))
        job_id = cur.lastrowid
        if segs:
            conn.executemany(
                'INSERT INTO segments (job_id, idx, key, start, end, low, high, confidence)'
                ' VALUES (?,?,?,?,?,?,?,?)',
                [(job_id, i, s['key'], s['start'], s['end'], s['low'], s['high'],
                  float(s.get('confidence') or 0)) for i, s in enumerate(segs)])
    return job_id, created


def list_history(limit=20, offset=0, keyword=''):
    """分页查询历史，keyword 同时匹配序列与文件名"""
    where, args = '', []
    if keyword:
        where = ' WHERE sequence LIKE ? OR filename LIKE ?'
        args = ['%' + keyword + '%', '%' + keyword + '%']

    with db() as conn:
        total = conn.execute('SELECT COUNT(*) FROM jobs' + where, args).fetchone()[0]
        rows = conn.execute(
            'SELECT id, filename, size_bytes, rate, duration, channels, sequence,'
            ' key_count, avg_confidence, created_at FROM jobs' + where +
            ' ORDER BY id DESC LIMIT ? OFFSET ?', args + [limit, offset]).fetchall()
    return [dict(r) for r in rows], int(total)


def history_stats():
    with db() as conn:
        jobs = conn.execute('SELECT COUNT(*) FROM jobs').fetchone()[0]
        keys = conn.execute('SELECT COALESCE(SUM(key_count), 0) FROM jobs').fetchone()[0]
        seconds = conn.execute('SELECT COALESCE(SUM(duration), 0) FROM jobs').fetchone()[0]
        top = conn.execute(
            'SELECT key, COUNT(*) AS c FROM segments GROUP BY key ORDER BY c DESC, key LIMIT 1'
        ).fetchone()
    return {
        'jobs': int(jobs),
        'keys': int(keys),
        'seconds': round(float(seconds or 0), 1),
        'top_key': top['key'] if top else None,
        'top_key_count': int(top['c']) if top else 0,
    }


def get_history(job_id):
    with db() as conn:
        job = conn.execute('SELECT * FROM jobs WHERE id = ?', (job_id,)).fetchone()
        if job is None:
            return None
        segs = conn.execute(
            'SELECT key, start, end, low, high, confidence FROM segments'
            ' WHERE job_id = ? ORDER BY idx', (job_id,)).fetchall()

    job = dict(job)
    return {
        'ok': True,
        'id': job['id'],
        'job_id': job['id'],
        'sequence': job['sequence'],
        'segments': [{
            'key': s['key'], 'start': s['start'], 'end': s['end'],
            'duration': round((s['end'] or 0) - (s['start'] or 0), 4),
            'low': s['low'], 'high': s['high'],
            'confidence': s['confidence'],
        } for s in segs],
        'meta': {
            'filename': job['filename'],
            'rate': job['rate'],
            'duration': job['duration'],
            'channels': job['channels'],
        },
        'stored_name': job['stored_name'],
        'size_bytes': job['size_bytes'],
        'created_at': job['created_at'],
        'avg_confidence': job['avg_confidence'],
    }


def _drop_audio(stored_name):
    """删除记录对应的音频文件，只允许操作 uploads 下的单层文件名"""
    name = os.path.basename(stored_name or '')
    if not name:
        return
    path = os.path.join(UPLOAD_FOLDER, name)
    if os.path.isfile(path):
        try:
            os.remove(path)
        except OSError:
            pass


def delete_history(job_id):
    with db() as conn:
        row = conn.execute('SELECT stored_name FROM jobs WHERE id = ?', (job_id,)).fetchone()
        if row is None:
            return False
        conn.execute('DELETE FROM jobs WHERE id = ?', (job_id,))
    _drop_audio(row['stored_name'])
    return True


def clear_history():
    with db() as conn:
        names = [r['stored_name'] for r in conn.execute('SELECT stored_name FROM jobs').fetchall()]
        conn.execute('DELETE FROM jobs')
        conn.execute("DELETE FROM sqlite_sequence WHERE name IN ('jobs', 'segments')")
    for name in names:
        _drop_audio(name)
    return len(names)


# --- 文件处理 ---
def save_upload(file_storage):
    """保存上传文件，返回 (存储名, 原始名, 路径)。存储名随机化，避免覆盖与路径穿越。"""
    original = (file_storage.filename or '').strip()
    ext = os.path.splitext(original)[1].lower()
    if ext not in ALLOWED_EXT:
        raise ValueError('仅支持 .wav 格式，其他格式请先转码')
    stored = uuid.uuid4().hex + ext
    path = os.path.join(UPLOAD_FOLDER, stored)
    file_storage.save(path)
    return stored, original, path


def analyze(path, original_name):
    sequence, segments, meta = decode_dtmf_segments(path)
    meta['filename'] = original_name
    return {'ok': True, 'sequence': sequence, 'segments': segments, 'meta': meta}


# --- HTML 页面 ---
HTML = r'''<!doctype html>
<html lang="zh-CN">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>DTMF 音频解码器</title>
<style>
:root{
  --panel:rgba(255,255,255,.04);
  --line:rgba(255,255,255,.075);
  --line-hi:rgba(255,255,255,.16);
  --text:#eaeef7;
  --dim:#8d96ab;
  --mute:#5a6275;
  --cyan:#22d3ee;
  --violet:#a78bfa;
  --green:#34d399;
  --red:#fb7185;
  --mono:ui-monospace,SFMono-Regular,"SF Mono",Menlo,Consolas,"Liberation Mono",monospace;
}
*{box-sizing:border-box}
[hidden]{display:none!important}
html{-webkit-text-size-adjust:100%}
body{
  margin:0;min-height:100vh;color:var(--text);
  font:15px/1.6 -apple-system,BlinkMacSystemFont,"Segoe UI",Roboto,"PingFang SC","Microsoft YaHei",sans-serif;
  -webkit-font-smoothing:antialiased;
  background:
    radial-gradient(900px 620px at 10% -10%,rgba(34,211,238,.13),transparent 62%),
    radial-gradient(820px 600px at 94% -4%,rgba(139,92,246,.17),transparent 60%),
    radial-gradient(760px 720px at 50% 116%,rgba(59,130,246,.11),transparent 62%),
    #05060a;
  background-attachment:fixed;
}
.shell{max-width:900px;margin:0 auto;padding:46px 20px 70px}
.topbar{display:flex;align-items:center;justify-content:space-between;gap:16px;margin-bottom:24px}
.brand{display:flex;align-items:center;gap:14px;min-width:0}
.mark{
  width:46px;height:46px;flex:none;border-radius:14px;display:grid;place-items:center;color:#a5f3fc;
  background:linear-gradient(150deg,rgba(34,211,238,.24),rgba(139,92,246,.24));
  border:1px solid rgba(255,255,255,.11);
  box-shadow:0 10px 28px -14px rgba(34,211,238,.85),inset 0 1px 0 rgba(255,255,255,.15);
}
.mark svg{width:23px;height:23px}
h1{margin:0;font-size:21px;font-weight:650;letter-spacing:-.02em;line-height:1.25}
h1 em{
  font-style:normal;
  background:linear-gradient(90deg,#67e8f9,#a78bfa);
  -webkit-background-clip:text;background-clip:text;color:transparent;
}
.brand p{margin:2px 0 0;font-size:12.5px;color:var(--dim);letter-spacing:.02em}
.badge{
  display:inline-flex;align-items:center;gap:7px;flex:none;white-space:nowrap;
  padding:7px 13px;border-radius:999px;font-size:12px;color:#9fb0c9;
  background:rgba(255,255,255,.04);border:1px solid var(--line);
}
.dot{width:6px;height:6px;border-radius:50%;background:var(--green);box-shadow:0 0 0 3px rgba(52,211,153,.16)}
.card{
  position:relative;padding:20px;margin-bottom:16px;border-radius:20px;
  background:linear-gradient(180deg,rgba(255,255,255,.046),rgba(255,255,255,.016));
  border:1px solid var(--line);
  box-shadow:0 26px 62px -34px rgba(0,0,0,.95),inset 0 1px 0 rgba(255,255,255,.05);
  backdrop-filter:blur(14px);-webkit-backdrop-filter:blur(14px);
  animation:rise .46s cubic-bezier(.2,.8,.2,1) both;
}
@keyframes rise{from{opacity:0;transform:translateY(10px)}to{opacity:1;transform:none}}

/* 上传区 */
.dropzone{
  border:1.5px dashed rgba(255,255,255,.14);border-radius:16px;
  padding:36px 20px 32px;text-align:center;cursor:pointer;outline:none;
  background:radial-gradient(120% 100% at 50% 0%,rgba(34,211,238,.055),transparent 62%);
  transition:border-color .22s,background .22s,transform .22s,box-shadow .22s;
}
.dropzone:hover,.dropzone:focus-visible{
  border-color:rgba(34,211,238,.42);
  background:radial-gradient(120% 100% at 50% 0%,rgba(34,211,238,.1),transparent 62%);
}
.dropzone.drag{
  border-color:var(--cyan);background:rgba(34,211,238,.09);transform:scale(1.006);
  box-shadow:0 0 0 4px rgba(34,211,238,.07),0 0 46px -12px rgba(34,211,238,.55);
}
.drop-icon{
  width:58px;height:58px;margin:0 auto 14px;border-radius:18px;display:grid;place-items:center;
  color:#7dd3fc;background:linear-gradient(160deg,rgba(34,211,238,.15),rgba(139,92,246,.13));
  border:1px solid rgba(125,211,252,.2);transition:transform .3s cubic-bezier(.2,.8,.2,1);
}
.drop-icon svg{width:26px;height:26px}
.dropzone:hover .drop-icon{transform:translateY(-3px)}
.dropzone h2{margin:0 0 6px;font-size:16.5px;font-weight:600;letter-spacing:-.01em}
.dropzone p{margin:0;font-size:13px;color:var(--dim)}
.dropzone b{color:#7dd3fc;font-weight:600}

.filebar{
  display:flex;align-items:center;justify-content:space-between;gap:12px;
  padding:12px 14px;margin-top:14px;border-radius:14px;
  background:rgba(255,255,255,.035);border:1px solid var(--line);
}
.filebar-main{display:flex;align-items:center;gap:12px;min-width:0}
.file-icon{
  width:38px;height:38px;flex:none;border-radius:10px;display:grid;place-items:center;
  font:600 10px/1 var(--mono);letter-spacing:.06em;color:#67e8f9;
  background:rgba(34,211,238,.1);border:1px solid rgba(34,211,238,.22);
}
.file-meta{min-width:0}
.file-meta strong{display:block;font-size:13.5px;font-weight:550;white-space:nowrap;overflow:hidden;text-overflow:ellipsis}
.file-meta span{font-size:11.5px;color:var(--mute);font-family:var(--mono)}
.progress{margin-top:10px;height:3px;border-radius:99px;background:rgba(255,255,255,.07);overflow:hidden}
.progress .bar{height:100%;width:0;border-radius:99px;background:linear-gradient(90deg,#22d3ee,#a78bfa);transition:width .2s ease}
.alert{
  display:flex;gap:9px;align-items:flex-start;margin-top:12px;padding:11px 14px;border-radius:12px;
  font-size:13px;color:#fecdd3;background:rgba(251,113,133,.09);border:1px solid rgba(251,113,133,.28);
}
.alert svg{width:15px;height:15px;flex:none;margin-top:2px}

.btn{
  display:inline-flex;align-items:center;gap:7px;flex:none;cursor:pointer;
  padding:9px 15px;border-radius:11px;font-size:13px;font-weight:550;color:#e8ecf5;
  background:rgba(255,255,255,.06);border:1px solid var(--line-hi);
  transition:background .18s,border-color .18s,transform .18s,box-shadow .18s;
}
.btn:hover{background:rgba(255,255,255,.1);border-color:rgba(255,255,255,.26);transform:translateY(-1px)}
.btn:active{transform:translateY(0)}
.btn svg{width:15px;height:15px}
.btn.ghost{background:transparent;border-color:var(--line);color:var(--dim);padding:7px 12px;font-size:12px}
.btn.ghost:hover{color:var(--text);background:rgba(255,255,255,.05)}

/* 播放器 */
.player-top{display:flex;align-items:center;gap:14px;margin-bottom:14px;flex-wrap:wrap}
.playbtn{
  width:47px;height:47px;flex:none;border-radius:50%;cursor:pointer;display:grid;place-items:center;
  color:#f0f9ff;border:1px solid rgba(255,255,255,.14);
  background:linear-gradient(150deg,rgba(34,211,238,.26),rgba(139,92,246,.26));
  box-shadow:0 12px 28px -14px rgba(34,211,238,.9),inset 0 1px 0 rgba(255,255,255,.18);
  transition:transform .16s,box-shadow .2s;
}
.playbtn:hover{transform:scale(1.05);box-shadow:0 14px 34px -12px rgba(34,211,238,1)}
.playbtn:active{transform:scale(.97)}
.playbtn svg{width:18px;height:18px;margin-left:1px}
.playbtn .ic-pause{margin-left:0;display:none}
.playbtn.playing .ic-play{display:none}
.playbtn.playing .ic-pause{display:block}
.timecode{font-family:var(--mono);font-size:13px;color:#c7d2e4;letter-spacing:.02em}
.timecode i{color:var(--mute);font-style:normal;margin:0 5px}
.wave-hint{margin-left:auto;font-size:11.5px;color:var(--mute)}
.wavewrap{
  position:relative;height:168px;border-radius:14px;overflow:hidden;
  background:linear-gradient(180deg,rgba(255,255,255,.028),rgba(255,255,255,.008));
  border:1px solid var(--line);
}
#wave{display:block;width:100%;height:100%;cursor:pointer}
.hoverline{
  position:absolute;top:0;bottom:0;width:1px;background:rgba(255,255,255,.28);
  pointer-events:none;opacity:0;transition:opacity .15s;
}
.hovertip{
  position:absolute;top:8px;transform:translateX(-50%);pointer-events:none;
  padding:3px 7px;border-radius:6px;font:500 10.5px/1.4 var(--mono);
  color:#dbe6f5;background:rgba(12,15,22,.9);border:1px solid rgba(255,255,255,.13);
  opacity:0;transition:opacity .15s;white-space:nowrap;
}
.wavewrap:hover .hoverline,.wavewrap:hover .hovertip{opacity:1}
.wavewrap.playing #wave{cursor:pointer}
.legend{display:flex;gap:18px;margin-top:11px;font-size:11.5px;color:var(--mute)}
.legend span{display:inline-flex;align-items:center;gap:6px}
.lg{width:9px;height:9px;border-radius:3px;display:inline-block}
.lg-on{background:linear-gradient(180deg,rgba(34,211,238,.9),rgba(139,92,246,.75))}
.lg-play{background:linear-gradient(90deg,#22d3ee,#a78bfa)}

/* 结果 */
.result-top{display:flex;align-items:flex-start;justify-content:space-between;gap:14px;margin-bottom:16px}
.result-top h3{margin:0;font-size:15px;font-weight:600;letter-spacing:-.01em}
.sub{margin:3px 0 0;font-size:12px;color:var(--mute);font-family:var(--mono)}
.seq{display:flex;flex-wrap:wrap;gap:9px;min-height:54px;align-items:center}
.seq-empty{font-size:13px;color:var(--mute)}
.keycap{
  width:54px;height:54px;border-radius:13px;cursor:pointer;padding:0;
  background:linear-gradient(180deg,#171c29,#0d1119);
  border:1px solid rgba(255,255,255,.1);
  box-shadow:inset 0 1px 0 rgba(255,255,255,.09),0 6px 16px -8px rgba(0,0,0,.95);
  color:#dde5f5;font:600 21px/1 var(--mono);display:grid;place-items:center;
  transition:transform .16s cubic-bezier(.2,.8,.2,1),border-color .16s,box-shadow .22s,background .2s,color .16s;
  animation:pop .34s cubic-bezier(.2,.9,.3,1.3) both;
}
@keyframes pop{from{opacity:0;transform:scale(.86) translateY(6px)}to{opacity:1;transform:none}}
.keycap:hover{
  transform:translateY(-4px);border-color:rgba(34,211,238,.5);color:#fff;
  box-shadow:0 12px 26px -12px rgba(34,211,238,.7),inset 0 1px 0 rgba(255,255,255,.15);
}
.keycap.active{
  transform:translateY(-4px) scale(1.05);color:#fff;
  background:linear-gradient(170deg,rgba(34,211,238,.34),rgba(139,92,246,.26));
  border-color:rgba(125,211,252,.85);
  box-shadow:0 0 0 1px rgba(34,211,238,.55),0 14px 34px -12px rgba(34,211,238,.9);
}
.keycap.sym{color:#c4b5fd}
.keycap.sym.active{color:#fff}
.stats{display:grid;grid-template-columns:repeat(4,1fr);gap:10px;margin-top:18px}
.stat{padding:12px 13px;border-radius:13px;background:rgba(255,255,255,.03);border:1px solid var(--line)}
.stat b{display:block;font:600 19px/1.3 var(--mono);letter-spacing:-.02em;color:#e9eefb}
.stat span{font-size:11px;color:var(--mute);letter-spacing:.03em}
.pad-title{margin-top:18px;font-size:11px;color:var(--mute);letter-spacing:.05em}
.pad{display:grid;grid-template-columns:repeat(3,1fr);gap:8px;margin-top:8px;max-width:258px}
.pad-cell{
  position:relative;height:38px;border-radius:10px;display:grid;place-items:center;
  background:rgba(255,255,255,.022);border:1px solid rgba(255,255,255,.055);
  color:#6f7890;font:600 13px/1 var(--mono);transition:color .25s,background .25s,border-color .25s,box-shadow .25s;
}
.pad-cell.hit{
  color:#e6fbff;background:linear-gradient(180deg,rgba(34,211,238,.16),rgba(139,92,246,.1));
  border-color:rgba(94,234,212,.36);box-shadow:0 0 22px -10px rgba(34,211,238,.95);
}
.pad-cell i{position:absolute;top:3px;right:6px;font:500 9px/1 var(--mono);font-style:normal;color:#67e8f9}
.pad-cell:not(.hit) i{display:none}

/* 历史记录 */
.hist-top{display:flex;align-items:flex-start;justify-content:space-between;gap:14px;flex-wrap:wrap}
.hist-top h3{margin:0;font-size:15px;font-weight:600;letter-spacing:-.01em}
.hist-actions{display:flex;align-items:center;gap:8px;flex-wrap:wrap}
.search{position:relative;display:flex;align-items:center}
.search svg{position:absolute;left:10px;width:14px;height:14px;color:var(--mute);pointer-events:none}
.search input{
  width:200px;padding:8px 10px 8px 30px;border-radius:10px;font-size:12.5px;color:var(--text);
  background:rgba(255,255,255,.04);border:1px solid var(--line);outline:none;
  transition:border-color .18s,background .18s;
}
.search input::placeholder{color:var(--mute)}
.search input:focus{border-color:rgba(34,211,238,.45);background:rgba(255,255,255,.06)}
.hist-stats{display:grid;grid-template-columns:repeat(4,1fr);gap:10px;margin:16px 0 12px}
.hist-list{display:flex;flex-direction:column;gap:2px}
.hist-row{
  display:flex;align-items:center;gap:12px;padding:10px 12px;border-radius:11px;cursor:pointer;
  border:1px solid transparent;transition:background .16s,border-color .16s;text-align:left;
}
.hist-row:hover{background:rgba(255,255,255,.04);border-color:var(--line)}
.hist-time{width:70px;flex:none;font:500 11px/1.4 var(--mono);color:var(--mute);white-space:nowrap}
.hist-main{flex:1;min-width:0;display:block}
.hist-seq{
  display:block;font:600 14px/1.35 var(--mono);color:#dfe8f8;letter-spacing:.06em;
  white-space:nowrap;overflow:hidden;text-overflow:ellipsis;
}
.hist-file{
  display:block;font-size:11px;color:var(--mute);
  white-space:nowrap;overflow:hidden;text-overflow:ellipsis;
}
.hist-count{
  flex:none;font:500 11px/1 var(--mono);color:#67e8f9;background:rgba(34,211,238,.1);
  border:1px solid rgba(34,211,238,.22);padding:5px 8px;border-radius:8px;white-space:nowrap;
}
.hist-del{
  width:26px;height:26px;flex:none;border-radius:8px;border:1px solid transparent;background:transparent;
  color:var(--mute);cursor:pointer;display:grid;place-items:center;padding:0;opacity:0;
  transition:opacity .16s,color .16s,background .16s,border-color .16s;
}
.hist-row:hover .hist-del,.hist-del:focus-visible{opacity:1}
.hist-del:hover{color:#fda4af;background:rgba(251,113,133,.12);border-color:rgba(251,113,133,.3)}
.hist-del svg{width:14px;height:14px}
.hist-empty{padding:28px 0;text-align:center;font-size:13px;color:var(--mute)}
.btn.more{width:100%;justify-content:center;margin-top:10px}
.btn.danger:hover{color:#fda4af;border-color:rgba(251,113,133,.4);background:rgba(251,113,133,.1)}

.foot{margin-top:26px;text-align:center;font-size:11.5px;color:#464e60;letter-spacing:.02em}
.toast{
  position:fixed;left:50%;bottom:32px;z-index:50;pointer-events:none;
  padding:11px 18px;border-radius:12px;font-size:13px;color:#e8ecf5;
  background:rgba(20,24,34,.94);border:1px solid rgba(255,255,255,.13);
  box-shadow:0 18px 40px -18px rgba(0,0,0,.95);backdrop-filter:blur(12px);
  opacity:0;transform:translate(-50%,20px);transition:opacity .25s,transform .25s;
}
.toast.show{opacity:1;transform:translate(-50%,0)}

@media(max-width:640px){
  .shell{padding:28px 14px 52px}
  .card{padding:16px;border-radius:17px}
  .stats{grid-template-columns:repeat(2,1fr)}
  .keycap{width:46px;height:46px;font-size:18px;border-radius:11px}
  .wave-hint,.badge{display:none}
  h1{font-size:18px}
  .wavewrap{height:140px}
  .hist-stats{grid-template-columns:repeat(2,1fr)}
  .hist-time{display:none}
  .hist-actions{width:100%}
  .search{flex:1}
  .search input{width:100%}
  .hist-del{opacity:1}
  .hist-count{padding:4px 6px;font-size:10px}
}
@media(prefers-reduced-motion:reduce){
  *{animation-duration:.001ms!important;transition-duration:.001ms!important}
}
</style>
</head>
<body>
<main class="shell">
  <header class="topbar">
    <div class="brand">
      <div class="mark" aria-hidden="true">
        <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round">
          <path d="M3 12h2M7 8v8M11 5v14M15 9v6M19 11v2M21 12h0" />
        </svg>
      </div>
      <div class="brand-txt">
        <h1>DTMF <em>Decoder</em></h1>
        <p>按键音 → 拨号序列</p>
      </div>
    </div>
    <div class="badge"><i class="dot"></i>本地解码</div>
  </header>

  <section class="card">
    <div class="dropzone" id="dropzone" tabindex="0" role="button" aria-label="选择或拖入 WAV 文件">
      <div class="drop-icon" aria-hidden="true">
        <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.7" stroke-linecap="round" stroke-linejoin="round">
          <path d="M12 16V4m0 0L7.5 8.5M12 4l4.5 4.5" />
          <path d="M4 15v3a2 2 0 0 0 2 2h12a2 2 0 0 0 2-2v-3" />
        </svg>
      </div>
      <h2>拖入 WAV 音频</h2>
      <p>或 <b>点击选择文件</b> · 上限 32 MB</p>
      <input type="file" id="fileInput" accept=".wav,audio/wav,audio/x-wav" hidden>
    </div>

    <div class="filebar" id="filebar" hidden>
      <div class="filebar-main">
        <div class="file-icon">WAV</div>
        <div class="file-meta">
          <strong id="fileName">-</strong>
          <span id="fileSize">-</span>
        </div>
      </div>
      <button type="button" class="btn ghost" id="resetBtn">重新选择</button>
    </div>

    <div class="progress" id="progress" hidden><div class="bar" id="bar"></div></div>

    <div class="alert" id="alert" hidden>
      <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round">
        <circle cx="12" cy="12" r="9" /><path d="M12 8v5M12 16.5v.5" />
      </svg>
      <span id="alertText"></span>
    </div>
  </section>

  <section class="card" id="playerCard" hidden>
    <div class="player-top">
      <button type="button" class="playbtn" id="playBtn" aria-label="播放/暂停">
        <svg class="ic-play" viewBox="0 0 24 24" fill="currentColor"><path d="M8 5.5v13l11-6.5z"/></svg>
        <svg class="ic-pause" viewBox="0 0 24 24" fill="currentColor"><path d="M7 5h3.5v14H7zM13.5 5H17v14h-3.5z"/></svg>
      </button>
      <div class="timecode"><span id="curTime">00:00.0</span><i>/</i><span id="durTime">00:00.0</span></div>
      <div class="wave-hint">点击波形跳转 · 点击色块单键回放 · 空格播放/暂停</div>
    </div>
    <div class="wavewrap" id="wavewrap">
      <canvas id="wave"></canvas>
      <div class="hoverline" id="hoverline"></div>
      <div class="hovertip" id="hovertip">00:00.0</div>
    </div>
    <div class="legend">
      <span><i class="lg lg-on"></i>识别到的按键区间</span>
      <span><i class="lg lg-play"></i>播放进度</span>
    </div>
  </section>

  <section class="card" id="resultCard" hidden>
    <div class="result-top">
      <div>
        <h3>识别结果</h3>
        <p class="sub" id="resultSub">-</p>
      </div>
      <button type="button" class="btn" id="copyBtn">
        <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.8" stroke-linecap="round" stroke-linejoin="round">
          <rect x="9" y="9" width="11" height="11" rx="2.5" />
          <path d="M15 5.5A2.5 2.5 0 0 0 12.5 3H6a2.5 2.5 0 0 0-2.5 2.5V13" />
        </svg>
        复制序列
      </button>
    </div>
    <div class="seq" id="seq"></div>
    <div class="stats" id="stats"></div>
    <div class="pad-title">按键分布</div>
    <div class="pad" id="pad"></div>
  </section>

  <section class="card" id="historyCard">
    <div class="hist-top">
      <div>
        <h3>历史记录</h3>
        <p class="sub" id="histSub">加载中…</p>
      </div>
      <div class="hist-actions">
        <div class="search">
          <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round">
            <circle cx="11" cy="11" r="6.5" /><path d="M16 16l4 4" />
          </svg>
          <input id="histSearch" type="search" placeholder="搜索序列 / 文件名" maxlength="64" autocomplete="off">
        </div>
        <button type="button" class="btn ghost danger" id="clearHistBtn">清空</button>
      </div>
    </div>
    <div class="hist-stats" id="histStats" hidden></div>
    <div class="hist-list" id="histList"></div>
    <button type="button" class="btn ghost more" id="histMore" hidden>加载更多</button>
  </section>

  <div class="foot">Flask · NumPy · SciPy · SQLite — 纯本地信号处理，不外传音频</div>
</main>

<div class="toast" id="toast"></div>
<audio id="audio" preload="auto" hidden></audio>

<script id="boot" type="application/json">{{ boot|tojson }}</script>
<script>
(function () {
  "use strict";

  var $ = function (id) { return document.getElementById(id); };
  var dropzone = $("dropzone"), fileInput = $("fileInput"), filebar = $("filebar"),
      fileName = $("fileName"), fileSize = $("fileSize"), resetBtn = $("resetBtn"),
      progress = $("progress"), bar = $("bar"), alertBox = $("alert"), alertText = $("alertText"),
      playerCard = $("playerCard"), resultCard = $("resultCard"),
      playBtn = $("playBtn"), audio = $("audio"),
      curTime = $("curTime"), durTime = $("durTime"),
      cv = $("wave"), ctx = cv.getContext("2d"),
      wavewrap = $("wavewrap"), hoverline = $("hoverline"), hovertip = $("hovertip"),
      seqEl = $("seq"), statsEl = $("stats"), padEl = $("pad"), resultSub = $("resultSub"),
      copyBtn = $("copyBtn"), toastEl = $("toast"),
      histSub = $("histSub"), histStats = $("histStats"), histList = $("histList"),
      histMore = $("histMore"), histSearch = $("histSearch"), clearHistBtn = $("clearHistBtn");

  var PAD = [["1","2","3"],["4","5","6"],["7","8","9"],["*","0","#"]];
  var MAX_SIZE = 32 * 1024 * 1024;

  var S = {
    sequence: "", segments: [], meta: {}, url: "",
    name: "", size: 0, duration: 0,
    channel: null, peaks: null, stopAt: null, busy: false
  };

  var cvW = 0, cvH = 0, rafId = 0, toastTimer = 0;

  /* ---------- 工具 ---------- */
  function fmtTime(t) {
    t = Math.max(0, Number(t) || 0);
    var m = Math.floor(t / 60);
    var s = t - m * 60;
    return String(m).padStart(2, "0") + ":" + s.toFixed(1).padStart(4, "0");
  }
  function fmtSize(b) {
    if (b < 1024) return b + " B";
    if (b < 1048576) return (b / 1024).toFixed(1) + " KB";
    return (b / 1048576).toFixed(2) + " MB";
  }
  function toast(msg) {
    toastEl.textContent = msg;
    toastEl.classList.add("show");
    clearTimeout(toastTimer);
    toastTimer = setTimeout(function () { toastEl.classList.remove("show"); }, 2100);
  }
  function showError(msg) {
    alertText.textContent = msg;
    alertBox.hidden = false;
  }
  function clearError() { alertBox.hidden = true; alertText.textContent = ""; }
  function setProgress(p) { bar.style.width = Math.max(0, Math.min(100, p)) + "%"; }

  /* ---------- Canvas 波形 ---------- */
  function fit() {
    var rect = cv.getBoundingClientRect();
    var dpr = Math.min(window.devicePixelRatio || 1, 2);
    cvW = Math.max(1, Math.round(rect.width));
    cvH = Math.max(1, Math.round(rect.height));
    cv.width = Math.round(cvW * dpr);
    cv.height = Math.round(cvH * dpr);
    ctx.setTransform(dpr, 0, 0, dpr, 0, 0);
    if (S.channel) {
      S.peaks = computePeaks(S.channel, Math.max(320, Math.floor(cvW * 1.2)));
    }
    draw();
  }

  function computePeaks(channel, bins) {
    var block = Math.max(1, Math.floor(channel.length / bins));
    var out = new Array(bins);
    for (var i = 0; i < bins; i++) {
      var mn = 0, mx = 0;
      var s = i * block, e = Math.min(channel.length, s + block);
      for (var j = s; j < e; j++) {
        var v = channel[j];
        if (v < mn) mn = v;
        if (v > mx) mx = v;
      }
      out[i] = [mn, mx];
    }
    return out;
  }

  function draw() {
    if (!cvW || !cvH) return;
    ctx.clearRect(0, 0, cvW, cvH);

    ctx.strokeStyle = "rgba(255,255,255,.05)";
    ctx.lineWidth = 1;
    for (var i = 1; i <= 3; i++) {
      var y = Math.round(cvH * i / 4) + 0.5;
      ctx.beginPath(); ctx.moveTo(0, y); ctx.lineTo(cvW, y); ctx.stroke();
    }

    if (!S.peaks || !S.peaks.length) return;

    var mid = cvH / 2;
    var amp = (cvH - 44) / 2;
    var dur = S.duration || 1;
    var prog = Math.min(1, (audio.currentTime || 0) / dur);

    // 片段区间
    var lastLabelRight = -1e9;   // 已绘制标签的右边界，用于防重叠
    for (var k = 0; k < S.segments.length; k++) {
      var seg = S.segments[k];
      var x1 = seg.start / dur * cvW;
      var w = Math.max(2, seg.end / dur * cvW - x1);

      // 底色压得很轻，避免密集时糊成一片
      var g = ctx.createLinearGradient(0, 0, 0, cvH);
      g.addColorStop(0, "rgba(34,211,238,.055)");
      g.addColorStop(1, "rgba(139,92,246,.018)");
      ctx.fillStyle = g;
      ctx.fillRect(x1, 0, w, cvH);

      // 顶部状态条 + 右侧分段线，密集时也能看清每次按键的边界
      ctx.fillStyle = "rgba(34,211,238,.62)";
      ctx.fillRect(x1, 0, w, 2.5);
      if (w >= 4) {
        ctx.fillStyle = "rgba(34,211,238,.22)";
        ctx.fillRect(x1 + w - 1, 0, 1, cvH);
      }

      // 片段够宽且整体不密集时才标按键号，宁愿不标也不能挤成一条
      if (S.segments.length <= 20 && w >= 24) {
        ctx.font = "600 11px ui-monospace, Menlo, Consolas, monospace";
        var tw = ctx.measureText(seg.key).width;
        var cx = x1 + w / 2;
        if (cx - tw / 2 >= lastLabelRight + 8) {
          ctx.fillStyle = "rgba(196,242,255,.92)";
          ctx.textAlign = "center";
          ctx.textBaseline = "top";
          ctx.fillText(seg.key, cx, 9);
          lastLabelRight = cx + tw / 2;
        }
      }
    }

    var bars = S.peaks, n = bars.length, step = cvW / Math.max(1, n - 1);

    // 用「上沿→下沿」的闭合轮廓填充，DTMF 方波密集时也不会裂成一排柱子
    var traceWave = function () {
      ctx.beginPath();
      ctx.moveTo(0, mid);
      for (var b = 0; b < n; b++) {
        ctx.lineTo(b * step, mid - bars[b][1] * amp);
      }
      ctx.lineTo(cvW, mid);
      for (var c = n - 1; c >= 0; c--) {
        ctx.lineTo(c * step, mid - bars[c][0] * amp);
      }
      ctx.closePath();
    };

    traceWave();
    ctx.fillStyle = "rgba(116,129,158,.26)";
    ctx.fill();
    ctx.strokeStyle = "rgba(190,205,230,.34)";
    ctx.lineWidth = 1;
    ctx.stroke();

    if (prog > 0) {
      ctx.save();
      ctx.beginPath();
      ctx.rect(0, 0, cvW * prog, cvH);
      ctx.clip();
      traceWave();
      var grad = ctx.createLinearGradient(0, 0, cvW, 0);
      grad.addColorStop(0, "rgba(34,211,238,.82)");
      grad.addColorStop(0.55, "rgba(96,165,250,.78)");
      grad.addColorStop(1, "rgba(167,139,250,.72)");
      ctx.fillStyle = grad;
      ctx.fill();
      ctx.strokeStyle = "rgba(186,230,253,.85)";
      ctx.lineWidth = 1;
      ctx.stroke();
      ctx.restore();
    }

    var px = cvW * prog;
    ctx.fillStyle = "rgba(255,255,255,.9)";
    ctx.fillRect(px - 0.5, 0, 1, cvH);
    ctx.beginPath();
    ctx.arc(px, 4, 3, 0, Math.PI * 2);
    ctx.fill();
  }

  /* ---------- 音频 ---------- */
  function loadWave(url) {
    var Ctor = window.AudioContext || window.webkitAudioContext;
    if (!Ctor) { draw(); return Promise.resolve(); }
    var ac = new Ctor();
    return fetch(url, { cache: "no-store" })
      .then(function (r) { return r.arrayBuffer(); })
      .then(function (buf) { return ac.decodeAudioData(buf); })
      .then(function (decoded) {
        S.duration = decoded.duration || S.duration;
        S.channel = decoded.getChannelData(0);
        S.peaks = computePeaks(S.channel, Math.max(320, Math.floor(cvW * 1.2)));
        draw();
      })
      .catch(function (err) {
        console.warn("波形解析失败，仅显示片段色块:", err);
        S.channel = null;
        S.peaks = null;
        draw();
      })
      .then(function () {
        try { ac.close(); } catch (e) {}
      });
  }

  function updateTime() {
    curTime.textContent = fmtTime(audio.currentTime);
    durTime.textContent = fmtTime(S.duration || audio.duration || 0);
  }

  function highlight() {
    var t = audio.currentTime;
    var idx = -1;
    for (var i = 0; i < S.segments.length; i++) {
      var s = S.segments[i];
      if (t >= s.start - 0.03 && t <= s.end + 0.03) { idx = i; break; }
    }
    var kids = seqEl.children;
    for (var j = 0; j < kids.length; j++) {
      kids[j].classList.toggle("active", j === idx);
    }
  }

  function loop() {
    draw();
    updateTime();
    highlight();
    if (!audio.paused && !audio.ended) {
      rafId = requestAnimationFrame(loop);
    } else {
      rafId = 0;
    }
  }

  function startLoop() { if (!rafId) rafId = requestAnimationFrame(loop); }

  function togglePlay() {
    if (!S.url) return;
    if (audio.paused) {
      S.stopAt = null;
      audio.play().catch(function () { toast("浏览器拦截了自动播放，请手动点击"); });
    } else {
      audio.pause();
    }
  }

  function playSegment(i) {
    var s = S.segments[i];
    if (!s) return;
    audio.currentTime = Math.max(0, s.start - 0.02);
    S.stopAt = s.end + 0.03;
    audio.play().catch(function () {});
  }

  function timeFromEvent(e) {
    var r = cv.getBoundingClientRect();
    var x = Math.min(Math.max(e.clientX - r.left, 0), r.width);
    return (x / Math.max(1, r.width)) * (S.duration || 0);
  }

  /* ---------- 渲染 ---------- */
  function renderSeq() {
    seqEl.innerHTML = "";
    if (!S.segments.length) {
      var d = document.createElement("div");
      d.className = "seq-empty";
      d.textContent = "未识别到有效按键，试试音量更清晰、噪声更低的 WAV";
      seqEl.appendChild(d);
      return;
    }
    S.segments.forEach(function (s, i) {
      var b = document.createElement("button");
      b.type = "button";
      b.className = "keycap" + (/^[*#]$/.test(s.key) ? " sym" : "");
      b.textContent = s.key;
      b.style.animationDelay = Math.min(i * 26, 420) + "ms";
      b.title = "第 " + (i + 1) + " 键 · " + s.start.toFixed(2) + "s–" + s.end.toFixed(2) +
                "s · " + s.low + "+" + s.high + " Hz · 置信度 " +
                Math.round((s.confidence || 0) * 100) + "%";
      b.addEventListener("click", function () { playSegment(i); });
      seqEl.appendChild(b);
    });
  }

  function renderStats() {
    var dur = S.duration || S.meta.duration || 0;
    var n = S.segments.length;
    var sum = 0;
    for (var i = 0; i < n; i++) sum += (S.segments[i].confidence || 0);
    var avg = n ? Math.round(sum / n * 100) : 0;
    statsEl.innerHTML =
      '<div class="stat"><b>' + S.sequence.length + '</b><span>识别按键</span></div>' +
      '<div class="stat"><b>' + dur.toFixed(1) + 's</b><span>音频时长</span></div>' +
      '<div class="stat"><b>' + (S.meta.rate || "-") + '</b><span>采样率 Hz</span></div>' +
      '<div class="stat"><b>' + avg + '%</b><span>平均置信度</span></div>';
  }

  function renderPad() {
    var count = {};
    for (var i = 0; i < S.sequence.length; i++) {
      var c = S.sequence[i];
      count[c] = (count[c] || 0) + 1;
    }
    var html = "";
    for (var r = 0; r < PAD.length; r++) {
      for (var k = 0; k < PAD[r].length; k++) {
        var key = PAD[r][k];
        var hit = count[key] || 0;
        html += '<div class="pad-cell' + (hit ? " hit" : "") + '">' + key +
                "<i>" + hit + "</i></div>";
      }
    }
    padEl.innerHTML = html;
  }

  function resetResult() {
    S.sequence = ""; S.segments = []; S.meta = {}; S.url = "";
    S.channel = null; S.peaks = null; S.stopAt = null;
    audio.pause();
    audio.removeAttribute("src");
    audio.load();
    seqEl.innerHTML = ""; statsEl.innerHTML = ""; padEl.innerHTML = "";
    playerCard.hidden = true; resultCard.hidden = true;
    curTime.textContent = "00:00.0"; durTime.textContent = "00:00.0";
    playBtn.classList.remove("playing");
    copyBtn.disabled = false;
  }

  function renderResult(d, autoplay) {
    S.sequence = d.sequence || "";
    S.segments = d.segments || [];
    S.meta = d.meta || {};
    S.url = d.audio_url || "";
    S.duration = S.meta.duration || 0;

    if (d.meta && d.meta.filename) {
      fileName.textContent = d.meta.filename;
      S.name = d.meta.filename;
    }
    filebar.hidden = false;
    dropzone.hidden = true;

    playerCard.hidden = false;
    resultCard.hidden = false;

    var jobId = d.job_id || d.id;
    resultSub.textContent = (S.sequence
      ? "共 " + S.segments.length + " 个按键片段"
      : "未识别到按键") + (jobId ? " · 记录 #" + jobId : "");

    renderSeq();
    renderStats();
    renderPad();

    fit();
    updateTime();

    if (!S.url) return;

    audio.src = S.url;
    audio.load();
    loadWave(S.url).then(function () {
      draw();
      startLoop();
      if (autoplay !== false && S.sequence) {
        audio.play().catch(function () {
          /* 浏览器自动播放策略拦截时静默处理，用户点播放即可 */
        });
      }    });
  }

  /* ---------- 上传 ---------- */
  function upload(file) {
    if (!file || S.busy) return;
    if (!/\.(wav|wave)$/i.test(file.name)) {
      showError("仅支持 .wav 文件，其他格式请先转码为 WAV");
      return;
    }
    if (file.size > MAX_SIZE) {
      showError("文件 " + fmtSize(file.size) + " 超过 32MB 上限");
      return;
    }
    if (file.size === 0) {
      showError("文件为空，换一个试试");
      return;
    }

    clearError();
    resetResult();
    S.busy = true;
    S.size = file.size;
    fileName.textContent = file.name;
    fileSize.textContent = fmtSize(file.size) + " · 解析中";
    filebar.hidden = false;
    dropzone.hidden = true;
    progress.hidden = false;
    setProgress(8);

    var fd = new FormData();
    fd.append("audio", file, file.name);

    var xhr = new XMLHttpRequest();
    xhr.open("POST", "/api/decode", true);
    xhr.responseType = "json";

    xhr.upload.onprogress = function (e) {
      if (e.lengthComputable) {
        setProgress(8 + (e.loaded / e.total) * 62);
      }
    };
    xhr.onload = function () {
      S.busy = false;
      setProgress(100);
      var d = xhr.response;
      if (xhr.status >= 200 && xhr.status < 300 && d && d.ok) {
        fileSize.textContent = fmtSize(file.size);
        setTimeout(function () { progress.hidden = true; }, 320);
        renderResult(d);
        loadHistory(true);
      } else {
        progress.hidden = true;
        fileSize.textContent = fmtSize(file.size);
        showError((d && d.error) || ("解码失败（HTTP " + xhr.status + "）"));
        dropzone.hidden = false;
      }
    };
    xhr.onerror = function () {
      S.busy = false;
      progress.hidden = true;
      showError("网络异常，无法连接解码服务");
      dropzone.hidden = false;
    };
    xhr.send(fd);
  }

  /* ---------- 事件绑定 ---------- */
  dropzone.addEventListener("click", function () { fileInput.click(); });
  dropzone.addEventListener("keydown", function (e) {
    if (e.key === "Enter" || e.key === " ") { e.preventDefault(); fileInput.click(); }
  });
  fileInput.addEventListener("change", function () {
    if (fileInput.files && fileInput.files[0]) upload(fileInput.files[0]);
    fileInput.value = "";
  });

  ["dragenter", "dragover"].forEach(function (ev) {
    dropzone.addEventListener(ev, function (e) {
      e.preventDefault();
      e.stopPropagation();
      dropzone.classList.add("drag");
    });
  });
  ["dragleave", "drop"].forEach(function (ev) {
    dropzone.addEventListener(ev, function (e) {
      e.preventDefault();
      e.stopPropagation();
      dropzone.classList.remove("drag");
    });
  });
  dropzone.addEventListener("drop", function (e) {
    var f = e.dataTransfer && e.dataTransfer.files && e.dataTransfer.files[0];
    if (f) upload(f);
  });
  window.addEventListener("dragover", function (e) { e.preventDefault(); });
  window.addEventListener("drop", function (e) { e.preventDefault(); });

  resetBtn.addEventListener("click", function () {
    resetResult();
    progress.hidden = true;
    filebar.hidden = true;
    dropzone.hidden = false;
    clearError();
    S.busy = false;
  });

  playBtn.addEventListener("click", togglePlay);
  audio.addEventListener("play", function () {
    playBtn.classList.add("playing");
    startLoop();
  });
  audio.addEventListener("pause", function () {
    playBtn.classList.remove("playing");
    draw();
    highlight();
  });
  audio.addEventListener("timeupdate", function () {
    if (S.stopAt !== null && audio.currentTime >= S.stopAt) {
      audio.pause();
      S.stopAt = null;
    }
    updateTime();
    highlight();
  });
  audio.addEventListener("loadedmetadata", updateTime);

  cv.addEventListener("click", function (e) {
    if (!S.url) return;
    var t = timeFromEvent(e);
    var hit = null;
    for (var i = 0; i < S.segments.length; i++) {
      if (t >= S.segments[i].start && t <= S.segments[i].end) { hit = S.segments[i]; break; }
    }
    if (hit) {
      audio.currentTime = Math.max(0, hit.start - 0.02);
      S.stopAt = hit.end + 0.03;
    } else {
      audio.currentTime = t;
      S.stopAt = null;
    }
    audio.play().catch(function () {});
    draw();
  });

  cv.addEventListener("mousemove", function (e) {
    if (!S.url) return;
    var r = cv.getBoundingClientRect();
    var x = e.clientX - r.left;
    var t = timeFromEvent(e);
    var hit = null;
    for (var i = 0; i < S.segments.length; i++) {
      if (t >= S.segments[i].start && t <= S.segments[i].end) { hit = S.segments[i]; break; }
    }
    hoverline.style.left = x + "px";
    hovertip.style.left = x + "px";
    hovertip.textContent = fmtTime(t) + (hit ? " · 键 " + hit.key : "");
  });
  cv.addEventListener("mouseleave", function () {
    hoverline.style.left = "-10px";
    hovertip.style.left = "-10px";
  });

  copyBtn.addEventListener("click", function () {
    if (!S.sequence) { toast("还没有可复制的序列"); return; }
    var done = function () { toast("已复制：" + S.sequence); };
    if (navigator.clipboard && navigator.clipboard.writeText) {
      navigator.clipboard.writeText(S.sequence).then(done).catch(function () { legacyCopy(done); });
    } else {
      legacyCopy(done);
    }
  });

  function legacyCopy(done) {
    var ta = document.createElement("textarea");
    ta.value = S.sequence;
    ta.style.position = "fixed";
    ta.style.opacity = "0";
    document.body.appendChild(ta);
    ta.select();
    try { document.execCommand("copy"); done(); }
    catch (e) { toast("复制失败，请手动选中序列"); }
    ta.remove();
  }

  document.addEventListener("keydown", function (e) {
    if (e.code !== "Space") return;
    var tag = (document.activeElement && document.activeElement.tagName) || "";
    if (/INPUT|TEXTAREA|BUTTON/.test(tag)) return;
    e.preventDefault();
    togglePlay();
  });

  /* ---------- 历史记录 ---------- */
  var HIST = { items: [], total: 0, offset: 0, limit: 20, q: "", loading: false, pending: null, seq: 0 };

  function escHtml(s) {
    return String(s == null ? "" : s).replace(/[&<>"']/g, function (c) {
      return { "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c];
    });
  }

  function relTime(text) {
    var t = Date.parse(String(text || "").replace(" ", "T"));
    if (!t) return text || "";
    var diff = (Date.now() - t) / 1000;
    if (diff < 60) return "刚刚";
    if (diff < 3600) return Math.floor(diff / 60) + " 分钟前";
    if (diff < 86400) return Math.floor(diff / 3600) + " 小时前";
    if (diff < 604800) return Math.floor(diff / 86400) + " 天前";
    return String(text).slice(0, 10);
  }

  function loadHistory(reset) {
    // 正在请求时排队，保证最后一次刷新不被丢弃
    if (HIST.loading) {
      HIST.pending = HIST.pending || { reset: false };
      HIST.pending.reset = HIST.pending.reset || !!reset;
      return;
    }
    HIST.loading = true;
    if (reset) { HIST.offset = 0; HIST.items = []; }

    var url = "/api/history?limit=" + HIST.limit + "&offset=" + HIST.offset +
              (HIST.q ? "&q=" + encodeURIComponent(HIST.q) : "");
    var mySeq = ++HIST.seq;

    fetch(url, { cache: "no-store" })
      .then(function (r) { return r.json(); })
      .then(function (d) {
        if (mySeq !== HIST.seq) return;      // 丢弃过期响应
        if (!d.ok) throw new Error(d.error || "加载失败");
        HIST.total = d.total || 0;
        HIST.items = reset ? d.items : HIST.items.concat(d.items);
        HIST.offset = HIST.items.length;
        renderHistory(d.stats);
      })
      .catch(function (err) {
        if (mySeq !== HIST.seq) return;
        histList.innerHTML = '<div class="hist-empty">历史加载失败：' + escHtml(err.message) + "</div>";
        histSub.textContent = "未能读取历史库";
      })
      .then(function () {
        HIST.loading = false;
        if (HIST.pending) {
          var p = HIST.pending;
          HIST.pending = null;
          loadHistory(p.reset);
        }
      });
  }

  function renderStatsCards(stats) {
    histStats.hidden = false;
    histStats.innerHTML =
      '<div class="stat"><b>' + stats.jobs + "</b><span>累计识别</span></div>" +
      '<div class="stat"><b>' + stats.keys + "</b><span>累计按键</span></div>" +
      '<div class="stat"><b>' + Number(stats.seconds || 0).toFixed(1) + "s</b><span>累计时长</span></div>" +
      '<div class="stat"><b>' + escHtml(stats.top_key || "—") + "</b><span>最常按键</span></div>";
  }

  function renderHistory(stats) {
    if (stats) {
      renderStatsCards(stats);
    } else if (HIST.total === 0) {
      // 本地已清空：立刻把统计归零，不等网络
      renderStatsCards({ jobs: 0, keys: 0, seconds: 0, top_key: null });
    }

    if (!HIST.items.length) {
      histList.innerHTML = '<div class="hist-empty">' +
        (HIST.q ? "没有匹配的记录" : "还没有识别记录，上传一个 WAV 试试") + "</div>";
    } else {
      histList.innerHTML = HIST.items.map(function (it) {
        return '<div class="hist-row" data-id="' + it.id + '">' +
          '<span class="hist-time">' + escHtml(relTime(it.created_at)) + "</span>" +
          '<span class="hist-main">' +
            '<span class="hist-seq">' + (escHtml(it.sequence) || "—") + "</span>" +
            '<span class="hist-file">' + escHtml(it.filename) + "</span>" +
          "</span>" +
          '<span class="hist-count">' + it.key_count + " 键</span>" +
          '<button type="button" class="hist-del" data-del="' + it.id + '" title="删除这条记录">' +
            '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.9" stroke-linecap="round">' +
            '<path d="M4 7h16M9 7V5h6v2M6.5 7l.8 12.2A1.8 1.8 0 0 0 9.1 21h5.8a1.8 1.8 0 0 0 1.8-1.8L17.5 7"/></svg>' +
          "</button>" +
        "</div>";
      }).join("");
    }

    histSub.textContent = HIST.q ? "匹配 " + HIST.total + " 条" : "共 " + HIST.total + " 条记录";
    histMore.hidden = HIST.items.length >= HIST.total;
  }

  function openHistory(id) {
    if (!id) return;
    fetch("/api/history/" + id, { cache: "no-store" })
      .then(function (r) { return r.json(); })
      .then(function (d) {
        if (!d.ok) throw new Error(d.error || "读取失败");
        filebar.hidden = false;
        dropzone.hidden = true;
        fileName.textContent = (d.meta && d.meta.filename) || "历史记录";
        fileSize.textContent = "历史记录 #" + d.id + " · " + relTime(d.created_at);
        renderResult(d, false);
        playerCard.scrollIntoView({ behavior: "smooth", block: "start" });
      })
      .catch(function (err) { toast("载入失败：" + err.message); });
  }

  function removeHistory(id) {
    if (!window.confirm("删除这条识别记录？对应音频文件也会一并删除，无法恢复。")) return;
    fetch("/api/history/" + id, { method: "DELETE" })
      .then(function (r) { return r.json(); })
      .then(function (d) {
        if (!d.ok) throw new Error(d.error || "删除失败");
        // 先本地摘除，UI 立即正确，再让服务端数据校准
        HIST.items = HIST.items.filter(function (it) { return it.id !== id; });
        HIST.total = Math.max(0, HIST.total - 1);
        renderHistory(null);
        toast("已删除记录 #" + id);
        loadHistory(true);
      })
      .catch(function (err) { toast("删除失败：" + err.message); });
  }

  histList.addEventListener("click", function (e) {
    var del = e.target.closest("[data-del]");
    if (del) {
      e.stopPropagation();
      removeHistory(parseInt(del.getAttribute("data-del"), 10));
      return;
    }
    var row = e.target.closest(".hist-row");
    if (row) openHistory(parseInt(row.getAttribute("data-id"), 10));
  });

  histMore.addEventListener("click", function () { loadHistory(false); });

  var searchTimer = 0;
  histSearch.addEventListener("input", function () {
    clearTimeout(searchTimer);
    searchTimer = setTimeout(function () {
      HIST.q = histSearch.value.trim();
      loadHistory(true);
    }, 280);
  });

  clearHistBtn.addEventListener("click", function () {
    if (!HIST.total) { toast("还没有历史记录"); return; }
    if (!window.confirm("清空全部 " + HIST.total + " 条历史记录？音频文件会一并删除，无法恢复。")) return;
    fetch("/api/history", { method: "DELETE" })
      .then(function (r) { return r.json(); })
      .then(function (d) {
        if (!d.ok) throw new Error(d.error || "清空失败");
        // 先本地清空，避免网络抖动导致列表残留
        HIST.items = [];
        HIST.total = 0;
        HIST.offset = 0;
        renderHistory(null);
        toast("已清空 " + d.removed + " 条记录");
        resetResult();
        progress.hidden = true;
        filebar.hidden = true;
        dropzone.hidden = false;
        loadHistory(true);
      })
      .catch(function (err) { toast("清空失败：" + err.message); });
  });

  /* ---------- 启动 ---------- */
  var boot = null;
  try { boot = JSON.parse($("boot").textContent || "null"); } catch (e) { boot = null; }

  window.addEventListener("load", function () { fit(); });
  if (window.ResizeObserver) {
    new ResizeObserver(function () { if (!playerCard.hidden) fit(); }).observe(wavewrap);
  } else {
    window.addEventListener("resize", function () { if (!playerCard.hidden) fit(); });
  }

  if (boot) {
    if (boot.ok) {
      filebar.hidden = false;
      dropzone.hidden = true;
      renderResult(boot);
    } else {
      showError(boot.error || "解码失败");
    }
  }

  loadHistory(true);
})();
</script>
</body>
</html>
'''


# --- 路由 ---
@app.route('/', methods=['GET', 'POST'])
def index():
    """页面入口。POST 分支保留为无 JS 降级路径。"""
    boot = None
    if request.method == 'POST':
        file = request.files.get('audio')
        if file and file.filename:
            try:
                stored, original, path = save_upload(file)
                result = analyze(path, original)
                result['audio_url'] = url_for('uploaded_file', filename=stored)
                try:
                    job_id, created = save_history(result, stored, original, os.path.getsize(path))
                    result['job_id'] = job_id
                    result['created_at'] = created
                except sqlite3.Error as exc:
                    result['db_error'] = str(exc)
                boot = result
            except Exception as exc:  # 解码失败也要把原因带回页面
                boot = {'ok': False, 'error': str(exc)}
    return render_template_string(HTML, boot=boot)


@app.route('/api/decode', methods=['POST'])
def api_decode():
    """前端 AJAX 上传入口，返回 JSON。"""
    file = request.files.get('audio')
    if not file or not file.filename:
        return jsonify(ok=False, error='没有收到音频文件'), 400
    try:
        stored, original, path = save_upload(file)
        result = analyze(path, original)
        result['audio_url'] = url_for('uploaded_file', filename=stored)
        # 入库失败不影响本次识别结果
        try:
            job_id, created = save_history(result, stored, original, os.path.getsize(path))
            result['job_id'] = job_id
            result['created_at'] = created
        except sqlite3.Error as exc:
            result['db_error'] = str(exc)
        return jsonify(result)
    except ValueError as exc:
        return jsonify(ok=False, error=str(exc)), 400
    except Exception as exc:
        return jsonify(ok=False, error='解码失败：%s' % exc), 400


@app.route('/api/history', methods=['GET', 'DELETE'])
def api_history():
    if request.method == 'DELETE':
        return jsonify(ok=True, removed=clear_history())

    try:
        limit = min(max(int(request.args.get('limit', 20)), 1), 100)
    except (TypeError, ValueError):
        limit = 20
    try:
        offset = max(int(request.args.get('offset', 0)), 0)
    except (TypeError, ValueError):
        offset = 0
    keyword = (request.args.get('q') or '').strip()[:64]

    items, total = list_history(limit, offset, keyword)
    return jsonify(ok=True, items=items, total=total, stats=history_stats(),
                   limit=limit, offset=offset)


@app.route('/api/history/<int:job_id>', methods=['GET', 'DELETE'])
def api_history_item(job_id):
    if request.method == 'DELETE':
        if not delete_history(job_id):
            return jsonify(ok=False, error='记录不存在'), 404
        return jsonify(ok=True, id=job_id)

    data = get_history(job_id)
    if data is None:
        return jsonify(ok=False, error='记录不存在'), 404
    data['audio_url'] = url_for('uploaded_file', filename=data.pop('stored_name'))
    return jsonify(data)


@app.errorhandler(413)
def payload_too_large(_):
    return jsonify(ok=False, error='文件超过 32MB 上限'), 413


@app.route('/uploads/<filename>')
def uploaded_file(filename):
    if not os.path.exists(os.path.join(UPLOAD_FOLDER, filename)):
        abort(404)
    return send_from_directory(UPLOAD_FOLDER, filename, mimetype='audio/wav')


init_db()


if __name__ == '__main__':
    app.run(debug=True, host='0.0.0.0', port=5000)
