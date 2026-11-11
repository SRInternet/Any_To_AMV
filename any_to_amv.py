#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
any_to_amv.py — 批量将任意媒体文件转换为 AMV 格式（Windows / Linux / macOS）

递归扫描输入目录，调用 FFmpeg 批量转码为 .amv。命令行参数可自定义目标分辨率
与帧率，不支持的取值会自动修正为 AMV 合规值并给出警告。

依赖
----
* Python 3.8+
* FFmpeg + FFprobe（在 PATH 中，或用 --ffmpeg / --ffprobe 指定路径）

用法示例
--------
    # 一键运行（当前目录 -> ./amv_output，320x240，18fps）
    python any_to_amv.py

    # 指定输入/输出目录与参数
    python any_to_amv.py -i "D:/videos" -o "D:/amv" --width 208 --height 160

    # 只探测并打印转码计划，不实际转码
    python any_to_amv.py --dry-run

    # 覆盖已存在的输出
    python any_to_amv.py --overwrite
"""

from __future__ import annotations

import argparse
import atexit
import json
import logging
import os
import re
import shutil
import signal
import subprocess
import threading
import sys
import time
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional, Tuple

# --------------------------------------------------------------------------- #
# 常量与 AMV 格式硬性限制（基于对 FFmpeg 9.0 amv muxer/encoder 的实测）
# --------------------------------------------------------------------------- #

#: AMV 容器默认（也是唯一可用）的视频编码器
AMV_VIDEO_CODEC = "amv"
#: AMV 容器默认（也是唯一可用）的音频编码器：IMA ADPCM
AMV_AUDIO_CODEC = "adpcm_ima_amv"
#: AMV 唯一支持的像素格式
AMV_PIX_FMT = "yuvj420p"

#: 实测：FFmpeg 的 amv 编码器只接受 block_size == 1225（35x35 宏块网格）
AMV_BLOCK_SIZE = 1225
#: 实测：amv 编码器只接受 18fps（其它帧率会直接失败）
AMV_FPS = 18
#: 实测：amv 编码器原生支持的分辨率集合（宽或高非 16 的倍数时需 -strict experimental）
AMV_NATIVE_RESOLUTIONS = {
    (16, 16), (32, 16), (96, 160), (128, 96), (176, 144), (192, 144),
    (208, 96), (208, 160), (210, 160), (224, 176), (256, 192), (320, 240),
    (352, 240), (352, 288), (384, 288), (640, 368), (640, 480),
}

#: AMV 音频参数上限（IMA ADPCM）
AMV_AUDIO_SAMPLE_RATE = 22050
AMV_AUDIO_CHANNELS = 1

#: 默认输出参数
DEFAULT_WIDTH = 320
DEFAULT_HEIGHT = 240
DEFAULT_FPS = 18

#: 需要扫描的媒体文件扩展名（小写）
VIDEO_EXTENSIONS = {
    ".mp4", ".m4v", ".mov", ".mkv", ".avi", ".wmv", ".asf", ".flv", ".f4v",
    ".webm", ".mpg", ".mpeg", ".mpe", ".m1v", ".m2v", ".mpv", ".ts", ".m2ts",
    ".mts", ".mxf", ".3gp", ".3g2", ".3gpp", ".ogv", ".ogg", ".vob", ".divx",
    ".rm", ".rmvb", ".amv", ".nsv", ".gif", ".gxf", ".y4m", ".dv",
}
AUDIO_EXTENSIONS = {
    ".mp3", ".wav", ".aac", ".flac", ".ogg", ".oga", ".opus", ".wma", ".m4a",
    ".aiff", ".aif", ".aifc", ".alac", ".ape", ".wv", ".amr", ".ac3", ".dts",
    ".mp2", ".mka", ".au", ".snd", ".caf", ".dsf", ".tta",
}
#: 明确应跳过的非媒体/元数据文件
SKIP_EXTENSIONS = {
    ".txt", ".log", ".nfo", ".md", ".json", ".xml", ".yml", ".yaml", ".ini",
    ".cfg", ".conf", ".bat", ".cmd", ".ps1", ".sh", ".py", ".js", ".tsx",
    ".jpg", ".jpeg", ".png", ".bmp", ".gif", ".webp", ".tiff", ".ico", ".svg",
    ".db", ".dat", ".part", ".crdownload", ".download", ".tmp", ".url", ".lnk",
    ".sub", ".srt", ".ass", ".ssa", ".vtt", ".idx",
}

# 注意：.gif 同时出现在 VIDEO 与 SKIP 中，判定时以 VIDEO 优先（可转成动画 AMV）

#: ffprobe 探测超时（秒）
PROBE_TIMEOUT = 60
#: ffprobe 判定"损坏文件"的最小可读流要求
MIN_STREAMS = 1

# ANSI 颜色：仅在交互式终端且未设置 NO_COLOR 时启用
_COLOR_ENABLED = sys.stdout.isatty() and os.environ.get("NO_COLOR") is None


def _force_utf8_output() -> None:
    """在 Windows 上将标准输出/错误切到 UTF-8，避免 GBK 控制台无法打印 Unicode 符号。

    这对跨平台健壮性很重要：脚本会输出 ✓ / ✗ / ↷ / █ 等符号。
    Python 3.7+ 支持 reconfigure()；失败时静默降级。
    注意：必须在创建 logging handler *之前*调用，否则 handler 会持有旧的
    (GBK) stream 对象，导致日志输出乱码。
    """
    for stream_name in ("stdout", "stderr"):
        stream = getattr(sys, stream_name, None)
        if stream is None:
            continue
        reconfigure = getattr(stream, "reconfigure", None)
        if callable(reconfigure):
            try:
                reconfigure(encoding="utf-8", errors="replace")
            except (ValueError, OSError, AttributeError):
                pass
    # 影响本进程后续派生的子进程（ffmpeg/ffprobe 的文本输出编码）
    for var, value in (("PYTHONIOENCODING", "utf-8"), ("PYTHONUTF8", "1")):
        os.environ.setdefault(var, value)


def _install_signal_handlers() -> None:
    """把 SIGTERM / SIGHUP 也转换为 KeyboardInterrupt。

    Python 只对 SIGINT（Ctrl+C）默认抛 KeyboardInterrupt；SIGTERM 的默认行为是
    立即终止进程，不会执行 finally。安装处理器使这些信号同样走中断清理路径，
    确保 ffmpeg 子进程被回收、半成品被删除。
    """
    def _to_keyboard_interrupt(signum, frame):      # noqa: ARG001
        # 用信号名（如 SIGTERM）而非数字，便于用户在日志中识别
        name = signal.Signals(signum).name if hasattr(signal, "Signals") else str(signum)
        raise KeyboardInterrupt(f"收到信号 {name}")

    for sig_name in ("SIGTERM", "SIGHUP"):
        sig = getattr(signal, sig_name, None)
        if sig is None:
            continue                      # Windows 无 SIGHUP
        try:
            signal.signal(sig, _to_keyboard_interrupt)
        except (OSError, ValueError, RuntimeError):
            # 非主线程 / 平台不支持时静默跳过，不影响主流程
            pass


def _c(code: str, text: str) -> str:
    """给文本添加 ANSI 颜色；颜色被禁用时原样返回。"""
    if not _COLOR_ENABLED:
        return text
    return f"\033[{code}m{text}\033[0m"


def red(t: str) -> str: return _c("31", t)
def green(t: str) -> str: return _c("32", t)
def yellow(t: str) -> str: return _c("33", t)
def blue(t: str) -> str: return _c("36", t)
def bold(t: str) -> str: return _c("1", t)
def dim(t: str) -> str: return _c("2", t)


# --------------------------------------------------------------------------- #
# 数据结构
# --------------------------------------------------------------------------- #

@dataclass
class MediaInfo:
    """ffprobe 探测得到的媒体元数据。"""

    path: str
    exists: bool = False
    size: int = 0
    format_name: str = ""
    duration: Optional[float] = None
    bit_rate: Optional[int] = None

    # 视频流
    has_video: bool = False
    v_codec: str = ""
    v_codec_long: str = ""
    width: int = 0
    height: int = 0
    sar_num: int = 1
    sar_den: int = 1
    dar_num: int = 0
    dar_den: int = 0
    fps: Optional[float] = None
    fps_rational: str = ""
    pix_fmt: str = ""
    rotation: int = 0            # 顺时针度数，已归一化到 0/90/180/270
    color_range: str = ""

    # 音频流（取第一条）
    has_audio: bool = False
    a_codec: str = ""
    a_codec_long: str = ""
    sample_rate: int = 0
    channels: int = 0
    channel_layout: str = ""
    audio_bit_rate: Optional[int] = None

    # 其它
    n_streams: int = 0
    attachments: int = 0
    probe_error: str = ""

    @property
    def display_duration(self) -> str:
        if self.duration is None:
            return "未知"
        return format_duration(self.duration)

    @property
    def display_fps(self) -> str:
        if self.fps is None:
            return "未知"
        return f"{self.fps:.3f}fps"

    @property
    def display_resolution(self) -> str:
        if not self.width or not self.height:
            return "未知"
        return f"{self.width}x{self.height}"

    @property
    def display_dar(self) -> str:
        if self.dar_num and self.dar_den:
            return f"{self.dar_num}:{self.dar_den}"
        if self.width and self.height:
            return f"{self.width}:{self.height}"
        return "未知"

    @property
    def display_sar(self) -> str:
        return f"{self.sar_num}:{self.sar_den}"


@dataclass
class EncodePlan:
    """单个文件的转码方案（含所有校验/修正记录）。"""

    src: str
    dst: str
    info: MediaInfo
    # 目标参数（校验/修正后的最终值）
    width: int = 0
    height: int = 0
    fps: int = AMV_FPS
    block_size: int = AMV_BLOCK_SIZE
    strict: Optional[str] = None          # None / "experimental"
    fit_mode: str = "pad"                 # pad(黑边) / crop(裁剪) / stretch(拉伸)
    scale_filter: str = ""                # 最终 -vf 中的缩放部分
    notes: List[str] = field(default_factory=list)   # 校验说明
    warnings: List[str] = field(default_factory=list)

    def add_note(self, msg: str) -> None:
        self.notes.append(msg)

    def add_warning(self, msg: str) -> None:
        self.warnings.append(msg)


@dataclass
class Stats:
    """批量转码统计。"""

    total: int = 0
    planned: int = 0          # dry-run 模式下"计划就绪"的数量
    success: int = 0
    skipped_exist: int = 0
    skipped_nonvideo: int = 0
    skipped_corrupt: int = 0
    failed: int = 0
    total_bytes_in: int = 0
    total_bytes_out: int = 0
    total_time: float = 0.0
    interrupted: bool = False      # 是否被用户 Ctrl+C 中断
    partial_removed: List[str] = field(default_factory=list)  # 中断时清理的半成品


# --------------------------------------------------------------------------- #
# 全局"活动转码"注册表：Windows 控制台 Ctrl+C 会走 STATUS_CONTROL_C_EXIT 硬终止，
# Python 的 except/finally 不执行；atexit 兜底负责回收子进程并删除半成品。
# --------------------------------------------------------------------------- #
_ACTIVE_LOCK = threading.Lock()
_ACTIVE: Dict[str, Tuple[Optional[subprocess.Popen], Optional[str]]] = {}


def _register_active(key: str, proc: Optional[subprocess.Popen],
                     dst: Optional[str]) -> None:
    with _ACTIVE_LOCK:
        _ACTIVE[key] = (proc, dst)


def _unregister_active(key: str) -> None:
    with _ACTIVE_LOCK:
        _ACTIVE.pop(key, None)


def _cleanup_active() -> None:
    """atexit 兜底：终止所有仍在运行的 ffmpeg 并删除其半成品输出。

    覆盖 Python 层 try/except/finally 无法处理的场景：
      * Windows 控制台 Ctrl+C 走 STATUS_CONTROL_C_EXIT 硬终止
      * os._exit() / 解释器崩溃
    """
    with _ACTIVE_LOCK:
        items = list(_ACTIVE.items())
        _ACTIVE.clear()
    if not items:
        return
    logger = logging.getLogger("any_to_amv")
    for key, (proc, dst) in items:
        if proc is not None and proc.poll() is None:
            try:
                proc.terminate()
            except (OSError, ProcessLookupError):
                pass
            try:
                proc.wait(timeout=1.5)
            except (subprocess.TimeoutExpired, OSError, ValueError):
                try:
                    proc.kill()
                except (OSError, ProcessLookupError):
                    pass
        if dst:
            try:
                if os.path.isfile(dst):
                    os.remove(dst)
                    logger.warning("atexit 兜底清理：已删除不完整输出 %s", dst)
            except OSError:
                pass


atexit.register(_cleanup_active)


def _terminate_proc(proc: Optional[subprocess.Popen], logger: logging.Logger,
                    grace: float = 2.0) -> None:
    """可靠终止 ffmpeg 子进程并释放其管道句柄。

    执行顺序：
      1. terminate()（SIGTERM / Windows TerminateProcess）请求优雅退出
      2. 等待 grace 秒；若超时则 kill()（SIGKILL）强杀
      3. 再次 wait() 回收子进程，避免僵尸进程
      4. 显式关闭 stdout/stderr/stdin 管道，释放 OS 文件句柄

    该函数可安全地重复调用（幂等），且不会因子进程已退出或句柄已关闭而抛异常。

    注意：grace 默认取较短值（2s）。过长的等待会让 Ctrl+C 后程序看起来"卡住"，
    而 ffmpeg 对 SIGTERM 的响应通常在毫秒级。
    """
    if proc is None:
        return
    # 1) 请求优雅终止
    if proc.poll() is None:                     # 仍在运行
        try:
            proc.terminate()
        except (OSError, ProcessLookupError):
            pass                                # 进程已消失，无需处理
        # 2) 短等待；超时则强杀
        try:
            proc.wait(timeout=grace)
        except subprocess.TimeoutExpired:
            logger.warning("ffmpeg 未在 %.1fs 内响应终止信号，强制结束进程", grace)
            try:
                proc.kill()
            except (OSError, ProcessLookupError):
                pass
            try:
                proc.wait(timeout=grace)
            except subprocess.TimeoutExpired:
                logger.error("ffmpeg 进程无法被回收，可能存在僵尸进程")
    else:
        # 进程已退出，仍需 wait() 一次以回收（避免僵尸）
        try:
            proc.wait(timeout=0.5)
        except (subprocess.TimeoutExpired, OSError, ValueError):
            pass
    # 3) 关闭管道，释放文件句柄（否则 Windows 上输出文件会被持续占用）
    for pipe_name in ("stdout", "stderr", "stdin"):
        pipe = getattr(proc, pipe_name, None)
        if pipe is None:
            continue
        try:
            pipe.close()
        except (OSError, ValueError):
            pass                                # 已关闭或已失效


def _remove_partial(path: Optional[str], logger: logging.Logger,
                    stats: Optional[Stats] = None) -> bool:
    """删除不完整的输出文件（半成品），返回是否成功删除。

    用于中断/失败路径：ffmpeg 被中途终止时会留下结构不完整（甚至很大）的
    .amv 文件，必须显式删除，否则输出目录残留脏数据。
    """
    if not path:
        return False
    try:
        if not os.path.isfile(path):
            return False
        size = os.path.getsize(path)
        os.remove(path)
    except OSError as exc:
        # Windows 上文件仍被占用时会删除失败（如句柄未释放）
        logger.error("无法删除不完整的输出文件 %s：%s", path, exc)
        return False
    logger.info("已删除不完整的输出文件：%s（%d 字节）", path, size)
    if stats is not None:
        stats.partial_removed.append(path)
    return True


# --------------------------------------------------------------------------- #
# 工具函数
# --------------------------------------------------------------------------- #

def format_duration(seconds: Optional[float]) -> str:
    """把秒数格式化为 HH:MM:SS.mmm。"""
    if seconds is None or seconds < 0:
        return "未知"
    h = int(seconds // 3600)
    m = int((seconds % 3600) // 60)
    s = seconds - h * 3600 - m * 60
    return f"{h:02d}:{m:02d}:{s:06.3f}"


def format_bytes(n: Optional[int]) -> str:
    if n is None:
        return "未知"
    size = float(n)
    for unit in ("B", "KiB", "MiB", "GiB", "TiB"):
        if size < 1024.0:
            return f"{size:.1f}{unit}" if unit != "B" else f"{int(size)}B"
        size /= 1024.0
    return f"{size:.1f}PiB"


def parse_fraction(text: Optional[str]) -> Optional[float]:
    """解析 '30000/1001' 或 '30' 形式的帧率为 float。"""
    if not text:
        return None
    text = text.strip()
    if not text or text in ("0/0", "N/A"):
        return None
    if "/" in text:
        num, _, den = text.partition("/")
        try:
            n, d = float(num), float(den)
        except ValueError:
            return None
        if d == 0:
            return None
        return n / d
    try:
        return float(text)
    except ValueError:
        return None


def gcd(a: int, b: int) -> int:
    while b:
        a, b = b, a % b
    return a


def resolve_binary(explicit: Optional[str], name: str) -> Optional[str]:
    """定位可执行文件：优先用户指定路径，其次 PATH，最后常见安装目录。"""
    found = shutil.which(explicit) if explicit else None
    if found:
        return found
    found = shutil.which(name)
    if found:
        return found
    found = shutil.which(f"{name}.exe")
    if found:
        return found
    # Windows 常见安装位置
    if os.name == "nt":
        for base in (
            os.environ.get("ProgramFiles", r"C:\Program Files"),
            os.environ.get("ProgramFiles(x86)", r"C:\Program Files (x86)"),
            os.environ.get("LOCALAPPDATA", ""),
            r"C:\ffmpeg",
            r"C:\ffmpeg\bin",
        ):
            if not base:
                continue
            for sub in ("", r"\ffmpeg\bin", r"\bin"):
                p = os.path.join(base, sub, f"{name}.exe")
                if os.path.isfile(p):
                    return p
    return None


# --------------------------------------------------------------------------- #
# FFmpeg / FFprobe 检查
# --------------------------------------------------------------------------- #

def check_tools(ffmpeg: str, ffprobe: str) -> Tuple[bool, List[str]]:
    """检查 ffmpeg/ffprobe 是否可用，并确认支持 amv 编码。返回 (ok, 错误列表)。"""
    errors: List[str] = []

    for exe, label in ((ffmpeg, "FFmpeg"), (ffprobe, "FFprobe")):
        if not exe:
            errors.append(f"未找到 {label} 可执行文件，请先安装并加入 PATH（或使用对应参数指定路径）。")
            continue
        try:
            p = subprocess.run([exe, "-version"], capture_output=True,
                               encoding="utf-8", errors="replace", timeout=30,
                               creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
        except FileNotFoundError:
            errors.append(f"{label} 路径无效：{exe}")
            continue
        except subprocess.TimeoutExpired:
            errors.append(f"{label} 执行超时：{exe}")
            continue
        except OSError as exc:
            errors.append(f"{label} 无法执行：{exe}（{exc}）")
            continue
        if p.returncode != 0:
            errors.append(f"{label} 返回非零退出码：{exe}")

    if ffmpeg:
        try:
            p = subprocess.run([ffmpeg, "-hide_banner", "-encoders"], capture_output=True,
                               encoding="utf-8", errors="replace", timeout=60,
                               creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
            if p.returncode == 0 and " amv " not in f" {p.stdout} ":
                errors.append(
                    "当前 FFmpeg 构建不包含 AMV 编码器（native FFmpeg 均内置；"
                    "若为第三方精简版请更换完整版）。"
                )
        except (OSError, subprocess.TimeoutExpired):
            pass

    return (not errors), errors


# --------------------------------------------------------------------------- #
# ffprobe 探测
# --------------------------------------------------------------------------- #

def _normalize_rotation(stream: Dict[str, Any]) -> int:
    """从 side_data / tags 中提取旋转角度并归一化。"""
    for sd in stream.get("side_data_list", []) or []:
        rot = sd.get("rotation")
        if rot is None:
            continue
        try:
            return int(round(float(rot))) % 360
        except (TypeError, ValueError):
            pass
    tags = stream.get("tags", {}) or {}
    for key in ("rotate", "ROTATE"):
        if key in tags:
            try:
                return int(round(float(tags[key]))) % 360
            except (TypeError, ValueError):
                pass
    return 0


def probe_media(path: str, ffprobe: str, timeout: int = PROBE_TIMEOUT) -> MediaInfo:
    """使用 ffprobe 探测单个文件的元数据。"""
    info = MediaInfo(path=path)
    try:
        info.exists = os.path.isfile(path)
        info.size = os.path.getsize(path) if info.exists else 0
    except OSError:
        pass

    cmd = [
        ffprobe, "-v", "error",
        "-print_format", "json",
        "-show_format", "-show_streams",
        "-i", path,
    ]
    try:
        p = subprocess.run(cmd, capture_output=True,
                           encoding="utf-8", errors="replace", timeout=timeout,
                           creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
    except subprocess.TimeoutExpired:
        info.probe_error = f"ffprobe 超时（>{timeout}s），文件可能极大或已损坏"
        return info
    except OSError as exc:
        info.probe_error = f"无法执行 ffprobe：{exc}"
        return info

    stderr = (p.stderr or "").strip()
    if p.returncode != 0:
        info.probe_error = stderr or f"ffprobe 退出码 {p.returncode}"
        return info

    try:
        data = json.loads(p.stdout or "{}")
    except json.JSONDecodeError as exc:
        info.probe_error = f"ffprobe 输出解析失败：{exc}"
        return info

    fmt = data.get("format", {}) or {}
    streams = data.get("streams", []) or []

    info.format_name = fmt.get("format_name", "")
    info.n_streams = len(streams)
    try:
        info.duration = float(fmt["duration"]) if fmt.get("duration") not in (None, "N/A") else None
    except (TypeError, ValueError):
        info.duration = None
    try:
        info.bit_rate = int(fmt["bit_rate"]) if fmt.get("bit_rate") not in (None, "N/A") else None
    except (TypeError, ValueError):
        info.bit_rate = None

    video_streams = [s for s in streams if s.get("codec_type") == "video"]
    audio_streams = [s for s in streams if s.get("codec_type") == "audio"]
    info.attachments = sum(1 for s in streams if s.get("codec_type") == "attachment")

    if video_streams:
        v = video_streams[0]
        info.has_video = True
        info.v_codec = v.get("codec_name", "")
        info.v_codec_long = v.get("codec_name", "")
        info.pix_fmt = v.get("pix_fmt", "")
        info.color_range = v.get("color_range", "")
        info.rotation = _normalize_rotation(v)
        try:
            info.width = int(v.get("width", 0) or 0)
            info.height = int(v.get("height", 0) or 0)
        except (TypeError, ValueError):
            info.width = info.height = 0

        # 采样宽高比
        sar = v.get("sample_aspect_ratio", "") or ""
        if sar and sar not in ("0:1", "N/A"):
            sn, _, sd = sar.partition(":")
            try:
                info.sar_num, info.sar_den = int(sn), int(sd)
            except ValueError:
                info.sar_num, info.sar_den = 1, 1
        else:
            info.sar_num, info.sar_den = 1, 1
        if info.sar_num <= 0 or info.sar_den <= 0:
            info.sar_num, info.sar_den = 1, 1

        # 显示宽高比
        dar = v.get("display_aspect_ratio", "") or ""
        if dar and dar not in ("0:1", "N/A"):
            dn, _, dd = dar.partition(":")
            try:
                info.dar_num, info.dar_den = int(dn), int(dd)
            except ValueError:
                info.dar_num, info.dar_den = 0, 0
        if not info.dar_num and info.width and info.height:
            dw = info.width * info.sar_num
            dh = info.height * info.sar_den
            g = gcd(dw, dh) or 1
            info.dar_num, info.dar_den = dw // g, dh // g

        # 帧率：优先 avg_frame_rate，其次 r_frame_rate
        fps = parse_fraction(v.get("avg_frame_rate"))
        info.fps_rational = v.get("avg_frame_rate", "") or ""
        if fps is None or fps <= 0 or fps > 1000:
            fps = parse_fraction(v.get("r_frame_rate"))
            info.fps_rational = v.get("r_frame_rate", "") or ""
        info.fps = fps if (fps and 0 < fps <= 1000) else None

    if audio_streams:
        a = audio_streams[0]
        info.has_audio = True
        info.a_codec = a.get("codec_name", "")
        info.a_codec_long = a.get("codec_name", "")
        info.channel_layout = a.get("channel_layout", "")
        try:
            info.sample_rate = int(a.get("sample_rate", 0) or 0)
        except (TypeError, ValueError):
            info.sample_rate = 0
        try:
            info.channels = int(a.get("channels", 0) or 0)
        except (TypeError, ValueError):
            info.channels = 0
        try:
            info.audio_bit_rate = int(a["bit_rate"]) if a.get("bit_rate") not in (None, "N/A") else None
        except (TypeError, ValueError):
            info.audio_bit_rate = None
        if not info.channel_layout and info.channels == 1:
            info.channel_layout = "mono"
        elif not info.channel_layout and info.channels == 2:
            info.channel_layout = "stereo"

    # 若无 duration 但有音频/视频流，尝试用流时长补全
    if info.duration is None:
        for s in streams:
            d = parse_fraction(s.get("duration")) if s.get("duration") not in (None, "N/A") else None
            if d and d > 0:
                info.duration = d
                break

    # 探测告警（未失败但可疑）
    if info.n_streams == 0:
        info.probe_error = "文件中不含任何媒体流"
    elif info.n_streams < MIN_STREAMS:
        info.probe_error = f"可读流数量不足（{info.n_streams}）"

    return info


# --------------------------------------------------------------------------- #
# AMV 参数校验与智能修正
# --------------------------------------------------------------------------- #

def validate_fps(requested: Optional[float], plan: "EncodePlan") -> int:
    """校验帧率。AMV 编码器仅支持 18fps，其它值一律就近回退到 18。"""
    req = DEFAULT_FPS if requested is None else int(round(requested))
    if req <= 0:
        plan.add_warning(f"帧率 {requested} 无效，已回退到 AMV 唯一支持值 {AMV_FPS}fps")
        return AMV_FPS
    if req != AMV_FPS:
        plan.add_warning(
            f"请求帧率 {req}fps 不被 AMV 支持（FFmpeg amv 编码器硬性限制），"
            f"已自动就近修正为 {AMV_FPS}fps"
        )
    return AMV_FPS


def validate_resolution(width: int, height: int, plan: "EncodePlan") -> Tuple[int, int]:
    """校验分辨率，返回修正后的 (width, height)。

    原生支持的分辨率直接采用；否则先就近取整到 16 的倍数，仍不在支持列表中时
    保持请求值并在 plan.strict 上标记 experimental。
    """
    if width <= 0 or height <= 0:
        plan.add_warning(f"分辨率 {width}x{height} 无效，已回退到 {DEFAULT_WIDTH}x{DEFAULT_HEIGHT}")
        return DEFAULT_WIDTH, DEFAULT_HEIGHT

    if (width, height) in AMV_NATIVE_RESOLUTIONS:
        plan.add_note(f"分辨率 {width}x{height} 属于 amv 编码器原生支持列表")
        return width, height

    # 非原生：尝试就近取整到 16 的倍数
    w16 = max(16, (width // 16) * 16)
    h16 = max(16, (height // 16) * 16)
    if (w16, h16) in AMV_NATIVE_RESOLUTIONS:
        plan.add_warning(
            f"分辨率 {width}x{height} 不在 amv 原生支持列表中，"
            f"已就近取整到 16 的倍数：{w16}x{h16}"
        )
        return w16, h16

    # 仍不在列表 -> 使用 -strict experimental 解锁（实测有效）
    plan.add_warning(
        f"分辨率 {width}x{height} 不属于 amv 编码器原生支持集合，"
        f"已自动附加 -strict experimental 以启用该分辨率"
    )
    plan.strict = "experimental"
    return width, height


def validate_block_size(requested: Optional[int], plan: "EncodePlan") -> int:
    """校验 block_size。AMV 编码器只接受 1225。"""
    if requested is None:
        return AMV_BLOCK_SIZE
    if requested != AMV_BLOCK_SIZE:
        plan.add_warning(
            f"请求 block_size={requested} 不被 AMV 支持"
            f"（FFmpeg amv 编码器仅接受 {AMV_BLOCK_SIZE}），已自动修正"
        )
    return AMV_BLOCK_SIZE


def compute_scale(info: MediaInfo, tw: int, th: int, mode: str, plan: "EncodePlan") -> str:
    """根据源媒体信息计算缩放 filter，保持宽高比。

    mode:
      pad    - 等比缩放后加黑边（letterbox/pillarbox），不裁剪不变形
      crop   - 等比放大后裁剪填满目标，不变形但会裁掉边缘
      stretch- 直接拉伸到目标尺寸（会变形，不推荐）
    """
    sw, sh = info.width, info.height
    rot = info.rotation

    # 旋转 90/270 时，输出的宽高与源的宽高互换
    if rot in (90, 270):
        sw, sh = sh, sw
        plan.add_note(f"源文件旋转 {rot}°，缩放计算已按旋转后画面尺寸 {sw}x{sh} 进行")

    if not sw or not sh:
        plan.add_warning("源分辨率未知，直接缩放到目标尺寸（无法保持宽高比）")
        return f"scale={tw}:{th}"

    src_dar = sw / sh
    dst_dar = tw / th

    if mode == "stretch":
        if abs(src_dar - dst_dar) > 0.01:
            plan.add_warning(
                f"stretch 模式：画面将由 {src_dar:.3f} 拉伸为 {dst_dar:.3f}，可能变形"
            )
        return f"scale={tw}:{th}"

    if mode == "crop":
        if abs(src_dar - dst_dar) <= 0.01:
            return f"scale={tw}:{th}"
        # 先按目标比例放大，再居中裁剪
        plan.add_note(
            f"源 DAR {src_dar:.3f} 与目标 DAR {dst_dar:.3f} 不同，"
            f"将等比放大后居中裁剪（会损失部分边缘画面）"
        )
        return (
            f"scale={tw}:{th}:force_original_aspect_ratio=increase,"
            f"crop={tw}:{th}"
        )

    # pad（默认）
    if abs(src_dar - dst_dar) <= 0.01:
        return f"scale={tw}:{th}"
    plan.add_note(
        f"源 DAR {src_dar:.3f} 与目标 DAR {dst_dar:.3f} 不同，"
        f"将等比缩放并填充黑边以保持宽高比"
    )
    return (
        f"scale={tw}:{th}:force_original_aspect_ratio=decrease,"
        f"pad={tw}:{th}:(ow-iw)/2:(oh-ih)/2:color=black"
    )


def build_filter_chain(info: MediaInfo, tw: int, th: int, mode: str, plan: "EncodePlan") -> str:
    """组装完整的视频滤镜链。"""
    parts: List[str] = []

    # 1. 旋转归一化
    if info.rotation in (90, 180, 270):
        rot_map = {90: "transpose=1", 180: "transpose=1,transpose=1", 270: "transpose=2"}
        parts.append(rot_map[info.rotation])
        plan.add_note(f"已加入旋转滤镜：{info.rotation}° -> 正向")

    # 2. 缩放（保持宽高比）
    parts.append(compute_scale(info, tw, th, mode, plan))

    # 3. 像素格式：AMV 只支持 yuvj420p（full-range YUV 4:2:0）。
    #    yuvj420p 被 swscale 标记为 deprecated，显式 setrange=full 可消除告警，
    #    并保证 full-range 语义正确传递，避免输出画面发灰/对比度异常。
    parts.append(f"format={AMV_PIX_FMT}")
    parts.append("setrange=full")
    # 4. 强制正方形像素，避免播放器按源文件的非方形 SAR 拉伸画面
    parts.append("setsar=1:1")

    # 帧率由 -r 控制（保证容器元数据与视频流一致）
    return ",".join(p for p in parts if p)


# --------------------------------------------------------------------------- #
# 文件扫描
# --------------------------------------------------------------------------- #

def is_candidate(path: str) -> bool:
    """根据扩展名判断是否可能是可转码媒体。"""
    ext = os.path.splitext(path)[1].lower()
    if ext in VIDEO_EXTENSIONS:
        return True
    if ext in AUDIO_EXTENSIONS:
        return True     # 纯音频可生成"黑屏/静态画面 + 声音"的 AMV
    return False


#: 扫描时要跳过的目录名（临时目录/版本控制/系统目录）
SKIP_DIRS = {
    ".tmp", ".git", ".svn", ".hg", ".venv", "venv", "env", "__pycache__",
    "node_modules", ".cache", "System Volume Information", "$RECYCLE.BIN",
    "lost+found", "amv_output", ".Trash", ".Trashes",
}


def scan_inputs(root: str, recursive: bool = True, exclude: Optional[List[str]] = None
                ) -> List[str]:
    """扫描目录，返回候选媒体文件列表（排序后）。"""
    exclude = [os.path.abspath(e) for e in (exclude or [])]
    found: List[str] = []

    if os.path.isfile(root):
        return [root] if is_candidate(root) else []

    for dirpath, dirnames, filenames in os.walk(root):
        dirnames.sort()
        # 跳过临时/版本控制/输出目录，避免重复转码与无谓扫描。
        # 注意：只按"目录名本身"判断，不要按"路径中包含 .tmp"判断，
        # 否则用户显式指定的输入目录若位于 .tmp 之下会被整体误跳过。
        dirnames[:] = [
            d for d in dirnames
            if d not in SKIP_DIRS
            and os.path.abspath(os.path.join(dirpath, d)) not in exclude
        ]
        for name in sorted(filenames):
            full = os.path.join(dirpath, name)
            if not is_candidate(full):
                continue
            if os.path.abspath(full) in exclude:
                continue
            found.append(full)

    if not recursive and os.path.isdir(root):
        found = [f for f in found if os.path.dirname(os.path.abspath(f)) == os.path.abspath(root)]

    return sorted(found)


# --------------------------------------------------------------------------- #
# 转码
# --------------------------------------------------------------------------- #

#: ffmpeg -progress 输出中的键
PROGRESS_KEY_RE = re.compile(r"^(?P<key>[a-z_]+)=(?P<value>.*)$", re.IGNORECASE)
TIME_RE = re.compile(r"^time=(?P<h>\d+):(?P<m>\d+):(?P<s>\d+\.\d+)")


def parse_progress_time(line: str) -> Optional[float]:
    """从 ffmpeg -progress 输出的一行中解析当前处理时间（秒）。

    优先使用 out_time_us / out_time_ms（微秒/毫秒级精度，结构化输出），
    退化时解析人类可读的 time=HH:MM:SS.xx 形式。
    """
    m = PROGRESS_KEY_RE.match(line.strip())
    if not m:
        return None
    key = m.group("key").lower()
    value = m.group("value").strip()
    if key == "out_time_us":
        try:
            return int(value) / 1_000_000.0
        except ValueError:
            return None
    if key == "out_time_ms":
        try:
            return int(value) / 1000.0
        except ValueError:
            return None
    if key == "out_time":
        t = TIME_RE.match(value)
        if t:
            try:
                return (int(t.group("h")) * 3600 + int(t.group("m")) * 60
                        + float(t.group("s")))
            except ValueError:
                return None
        # N/A 等情况
    return None


def run_ffmpeg(plan: "EncodePlan", ffmpeg: str, ffprobe: str,
               logger: logging.Logger,
               dry_run: bool = False, timeout: Optional[int] = None,
               quiet: bool = False) -> Tuple[bool, str]:
    """执行单条转码命令，返回 (成功?, 错误信息)。"""
    info = plan.info

    # ---- 输入侧 ----
    # 所有 -i 必须集中在输出选项之前；lavfi 补齐输入（静音/静态画面）也必须放在这里。
    # 用显式计数器记录每个输入的索引，避免手工推算出错。
    cmd: List[str] = [
        ffmpeg, "-hide_banner", "-nostdin", "-y",
        # 输入侧通用选项：容忍损坏/缺失的时间戳，提高对问题文件的兼容性
        "-fflags", "+genpts+igndts",
    ]

    # AMV muxer 强制要求"1 视频流 + 1 音频流"，任一侧缺失都要用 lavfi 补齐。
    next_idx = 0
    v_idx: Optional[int] = None
    a_idx: Optional[int] = None

    if not info.has_video:
        # 纯音频源：生成纯黑静态画面作为视频流
        v_idx = next_idx
        next_idx += 1
        cmd += ["-f", "lavfi", "-i",
                f"color=c=black:s={plan.width}x{plan.height}:r={plan.fps}"]
    if not info.has_audio:
        # 无音频源：合成静音作为音频流
        a_idx = next_idx
        next_idx += 1
        cmd += ["-f", "lavfi", "-i",
                "anullsrc=channel_layout=mono:sample_rate=22050"]

    src_idx = next_idx
    cmd += ["-i", plan.src]
    if v_idx is None:
        v_idx = src_idx
    if a_idx is None:
        a_idx = src_idx

    # ---- 视频输出 ----
    cmd += ["-map", f"{v_idx}:v:0", "-c:v", AMV_VIDEO_CODEC]
    if not info.has_video:
        # lavfi color 已按目标尺寸/帧率生成，无需再缩放；仅需像素格式/SAR 归一化
        cmd += ["-vf", f"format={AMV_PIX_FMT},setrange=full,setsar=1:1"]
    elif plan.scale_filter:
        cmd += ["-vf", plan.scale_filter]
    cmd += ["-s", f"{plan.width}x{plan.height}"]
    cmd += ["-r", str(plan.fps)]
    cmd += ["-block_size", str(plan.block_size)]
    cmd += ["-pix_fmt", AMV_PIX_FMT]
    # amv 是 mpegvideo 系编码器：关闭 B 帧与场景切换侦测，提升老旧硬件兼容性
    cmd += ["-bf", "0", "-sc_threshold", "0"]
    if plan.strict:
        cmd += ["-strict", plan.strict]

    # ---- 音频输出：强制 22050Hz 单声道 IMA ADPCM ----
    # a_idx 已在输入侧用计数器正确求出（源文件索引或静音流索引）
    cmd += ["-map", f"{a_idx}:a:0",
            "-c:a", AMV_AUDIO_CODEC,
            "-ar", str(AMV_AUDIO_SAMPLE_RATE),
            "-ac", str(AMV_AUDIO_CHANNELS)]

    # 任一侧使用 lavfi 补齐时，用 -shortest 让两条流等长，
    # 避免静音/黑屏无限生成导致输出文件爆炸。
    if not info.has_video or not info.has_audio:
        cmd += ["-shortest"]

    # ---- 容器 ----
    cmd += ["-f", "amv"]
    if plan.dst:
        cmd += ["-metadata", f"title={os.path.splitext(os.path.basename(plan.src))[0]}",
                plan.dst]

    # 结构化进度输出（out_time_us 提供微秒级精度，比解析 time= 文本更可靠）
    cmd += ["-progress", "pipe:1", "-nostats"]

    cmd_str = " ".join(_quote(c) for c in cmd)
    logger.info("FFmpeg 命令: %s", cmd_str)

    if dry_run:
        return True, ""

    if not quiet:
        print(dim("  $ " + _shorten_cmd(cmd_str)))

    total = info.duration or 0.0
    last_print = 0.0
    t0 = time.time()

    proc: Optional[subprocess.Popen] = None
    interrupted = False
    err_lines: List[str] = []
    # -progress 输出的已知键名，用于区分进度信息与真正的错误文本
    progress_keys = {
        "frame", "fps", "bitrate", "total_size", "out_time_us", "out_time_ms",
        "out_time", "dup_frames", "drop_frames", "speed", "progress",
        "stream_0_0_q", "media_type",
    }

    try:
        proc = subprocess.Popen(
            cmd, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
            stdin=subprocess.DEVNULL,
            encoding="utf-8", errors="replace", bufsize=1,
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
        )

        assert proc.stdout is not None
        # 注册到全局活动表：进程被硬终止（atexit 前不经 Python 层）时由兜底逻辑回收
        _register_active(id(proc), proc, plan.dst)
        # 用显式 readline() 循环而非迭代 proc.stdout：前者在中断时能立即跳出，
        # 后者会先尝试关闭管道，子进程仍在写入时该关闭操作可能阻塞。
        while True:
            try:
                raw = proc.stdout.readline()
            except (OSError, ValueError):
                break                       # 管道已关闭
            if not raw:
                break                       # EOF，子进程已结束
            line = raw.rstrip("\n")
            if not line.strip():
                continue
            t = parse_progress_time(line)
            if t is not None and total > 0:
                pct = min(100.0, t / total * 100.0)
                now = time.time()
                if not quiet and (now - last_print) >= 0.25:
                    last_print = now
                    bar = _progress_bar(pct)
                    print(f"\r  {bar} {pct:5.1f}%  "
                          f"[{format_duration(t)} / {format_duration(total)}]",
                          end="", flush=True)
                continue
            # 过滤掉 -progress 的其它键值对（frame=123 / fps=18 / progress=continue 等）
            m = PROGRESS_KEY_RE.match(line.strip())
            if m and m.group("key").lower() in progress_keys:
                continue
            # 其余输出（ffmpeg 的告警/错误）记录下来
            low = line.lower()
            if ("error" in low or "invalid" in low or "failed" in low
                    or "could not" in low or "unable" in low
                    or "no such" in low or "corrupt" in low
                    or "deprecated" in low or "experimental" in low):
                err_lines.append(line)
                logger.warning("ffmpeg: %s", line)

        rc = proc.wait(timeout=timeout)

    except KeyboardInterrupt:
        # 用户按 Ctrl+C：立刻标记中断，交由 finally 统一清理子进程与半成品
        interrupted = True
        logger.warning("收到用户中断信号，正在终止 ffmpeg 并清理不完整输出")
        return False, "用户中断（Ctrl+C）"
    except subprocess.TimeoutExpired:
        logger.error("ffmpeg 执行超时（>%ss）", timeout)
        return False, f"ffmpeg 执行超时（>{timeout}s）"
    except Exception as exc:              # 读取输出异常等未预期错误
        logger.exception("转码过程中发生未预期的异常")
        return False, f"读取 ffmpeg 输出失败：{exc}"
    finally:
        # 无论正常结束、失败还是被中断，都必须：
        #   1) 终止并回收子进程   2) 关闭管道句柄   3) 删掉半成品输出
        # 否则 Windows 上输出文件会被 ffmpeg.exe 持续占用而无法删除。
        # 顺序很重要：必须先终止进程再关管道，否则 readline 可能死锁。
        _terminate_proc(proc, logger)
        # 已正常回收则从 atexit 兜底表中移除，避免重复清理
        _unregister_active(id(proc) if proc is not None else 0)
        if not quiet and not interrupted:
            try:
                print("\r" + " " * 78 + "\r", end="", flush=True)
            except (OSError, ValueError):
                pass
        if interrupted:
            _remove_partial(plan.dst, logger)

    # ---- 以下仅在未被中断时执行 ----
    if rc != 0:
        tail = " | ".join(err_lines[-4:]) if err_lines else f"ffmpeg 退出码 {rc}"
        # ffmpeg 异常退出同样会留下不完整文件，一并清理
        _remove_partial(plan.dst, logger)
        return False, tail

    # 校验输出文件确实生成且非空
    if not plan.dst:
        return True, ""
    if not os.path.isfile(plan.dst) or os.path.getsize(plan.dst) == 0:
        _remove_partial(plan.dst, logger)
        return False, "ffmpeg 报告成功但输出文件缺失或为 0 字节"

    # 用 ffprobe 复核输出是否可读、编码是否正确
    ok, detail = verify_output(plan.dst, ffprobe)
    if not ok:
        _remove_partial(plan.dst, logger)
        return False, f"输出校验失败：{detail}"
    return True, ""


def verify_output(path: str, ffprobe: str) -> Tuple[bool, str]:
    """复核输出文件是否为有效的 AMV（可读取、含音视频双流）。"""
    cmd = [ffprobe, "-v", "error", "-print_format", "json",
           "-show_format", "-show_streams", path]
    try:
        p = subprocess.run(cmd, capture_output=True,
                           encoding="utf-8", errors="replace", timeout=60,
                           creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
    except (OSError, subprocess.TimeoutExpired) as exc:
        return False, f"ffprobe 复核失败：{exc}"
    if p.returncode != 0:
        return False, (p.stderr or "").strip() or "ffprobe 退出码非零"
    try:
        data = json.loads(p.stdout or "{}")
    except json.JSONDecodeError:
        return False, "输出文件无法解析"
    fmt = data.get("format", {}) or {}
    streams = data.get("streams", []) or []
    # 注意：AMV 容器底层是 AVI，ffprobe 的 format_name 会报 "avi"，
    # 因此不能凭 format_name 判断，必须以视频编码器是否为 amv 为准。
    video_streams = [s for s in streams if s.get("codec_type") == "video"]
    if not video_streams:
        return False, "输出中没有视频流"
    if video_streams[0].get("codec_name") != AMV_VIDEO_CODEC:
        return False, (f"视频编码不是 {AMV_VIDEO_CODEC}"
                       f"（实际：{video_streams[0].get('codec_name')}）")
    if not any(s.get("codec_type") == "audio" for s in streams):
        return False, "输出缺少音频流（AMV 要求音视频双流）"
    audio_streams = [s for s in streams if s.get("codec_type") == "audio"]
    if audio_streams[0].get("codec_name") != AMV_AUDIO_CODEC:
        return False, (f"音频编码不是 {AMV_AUDIO_CODEC}"
                       f"（实际：{audio_streams[0].get('codec_name')}）")
    return True, "ok"


def _quote(s: str) -> str:
    if " " in s and not s.startswith('"'):
        return f'"{s}"'
    return s


def _shorten_cmd(cmd: str, maxlen: int = 150) -> str:
    if len(cmd) <= maxlen:
        return cmd
    head = cmd[:maxlen // 2 - 3]
    tail = cmd[-maxlen // 2 + 3:]
    return head + " ... " + tail


def _progress_bar(pct: float, width: int = 28) -> str:
    filled = int(width * pct / 100.0)
    bar = "█" * filled + "░" * (width - filled)
    return f"[{bar}]"


# --------------------------------------------------------------------------- #
# 日志
# --------------------------------------------------------------------------- #

def setup_logger(log_path: str, verbose: bool) -> logging.Logger:
    logger = logging.getLogger("any_to_amv")
    logger.setLevel(logging.DEBUG if verbose else logging.INFO)
    logger.handlers.clear()
    logger.propagate = False

    fmt = logging.Formatter(
        "%(asctime)s [%(levelname)-7s] %(message)s", datefmt="%Y-%m-%d %H:%M:%S"
    )

    fh = logging.FileHandler(log_path, encoding="utf-8")
    fh.setLevel(logging.DEBUG)
    fh.setFormatter(fmt)
    logger.addHandler(fh)

    ch = logging.StreamHandler(sys.stderr)
    ch.setLevel(logging.WARNING)
    ch.setFormatter(logging.Formatter("[%(levelname)s] %(message)s"))
    logger.addHandler(ch)

    return logger


# --------------------------------------------------------------------------- #
# 主流程
# --------------------------------------------------------------------------- #

def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="any_to_amv.py",
        description="批量将任意媒体文件转换为 AMV 格式（FFmpeg / FFprobe）。",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=(
            "示例：\n"
            "  python any_to_amv.py                                  # 一键运行（全部使用默认值）\n"
            "  python any_to_amv.py -i D:/videos -o D:/amv           # 指定输入/输出目录\n"
            "  python any_to_amv.py --width 208 --height 160         # 自定义分辨率\n"
            "  python any_to_amv.py --fps 30                         # 自定义帧率（不被支持时自动就近修正）\n"
            "  python any_to_amv.py --dry-run                        # 只探测和预览，不实际转码\n"
            "  python any_to_amv.py --overwrite                      # 覆盖已存在的输出\n"
            "  python any_to_amv.py --fit crop                       # 裁剪填满而非加黑边\n"
        ),
    )

    g = p.add_argument_group("输入 / 输出")
    g.add_argument("-i", "--input", default=".",
                   help="输入目录或单个文件（默认：当前目录）")
    g.add_argument("-o", "--output", default=None,
                   help="输出目录（默认：<输入目录>/amv_output）")
    g.add_argument("--no-recursive", action="store_true",
                   help="不递归子目录（默认递归）")
    g.add_argument("--overwrite", action="store_true",
                   help="覆盖已存在的输出文件（默认跳过）")
    g.add_argument("--log", default=None,
                   help="日志文件路径（默认：<输出目录>/any_to_amv.log）")

    g = p.add_argument_group("AMV 目标参数")
    g.add_argument("--width", type=int, default=DEFAULT_WIDTH,
                   help=f"目标宽度（默认 {DEFAULT_WIDTH}）")
    g.add_argument("--height", type=int, default=DEFAULT_HEIGHT,
                   help=f"目标高度（默认 {DEFAULT_HEIGHT}）")
    g.add_argument("--fps", type=float, default=DEFAULT_FPS,
                   help=f"目标帧率（默认 {DEFAULT_FPS}；AMV 编码器仅支持 18fps，"
                        f"其它值将自动就近修正）")
    g.add_argument("--block-size", type=int, default=AMV_BLOCK_SIZE,
                   dest="block_size",
                   help=f"AMV block_size（默认 {AMV_BLOCK_SIZE}；编码器仅接受该值）")
    g.add_argument("--fit", choices=("pad", "crop", "stretch"), default="pad",
                   help="宽高比适配方式：pad=加黑边（默认），crop=裁剪填满，stretch=拉伸")

    g = p.add_argument_group("运行控制")
    g.add_argument("--dry-run", action="store_true",
                   help="只探测元数据并打印转码计划，不实际执行转码")
    g.add_argument("-v", "--verbose", action="store_true", help="更详细的日志输出")
    g.add_argument("--quiet", action="store_true", help="减少控制台输出（不显示进度条）")
    g.add_argument("--timeout", type=int, default=None,
                   help="单个文件转码超时秒数（默认不限制）")

    g = p.add_argument_group("FFmpeg 路径")
    g.add_argument("--ffmpeg", default=None, help="ffmpeg 可执行文件路径（默认从 PATH 查找）")
    g.add_argument("--ffprobe", default=None, help="ffprobe 可执行文件路径（默认从 PATH 查找）")

    return p


def main(argv: Optional[List[str]] = None) -> int:
    # Windows GBK/CP936 控制台无法输出 Unicode 符号（✓ ✗ ↷ █ 等），先切到 UTF-8
    _force_utf8_output()
    # 让 SIGTERM/SIGHUP 也触发中断清理，避免半成品文件被 ffmpeg 持续占用
    _install_signal_handlers()

    parser = build_parser()
    args = parser.parse_args(argv)

    # ---- 解析工具路径 ----
    ffmpeg = resolve_binary(args.ffmpeg, "ffmpeg")
    ffprobe = resolve_binary(args.ffprobe, "ffprobe")

    print(bold(blue("=" * 78)))
    print(bold(blue(" Any To AMV — 批量 AMV 转码工具")))
    print(bold(blue("=" * 78)))

    ok, errors = check_tools(ffmpeg, ffprobe)
    if not ok:
        for e in errors:
            print(red(f"[错误] {e}"))
        print(yellow(
            "\n请先安装 FFmpeg（需包含 native amv 编码器，标准发行版均已内置）：\n"
            "  Windows : https://www.gyan.dev/ffmpeg/builds/  （解压后将 bin 加入 PATH）\n"
            "  Linux   : sudo apt install ffmpeg\n"
            "  macOS   : brew install ffmpeg\n"
            "也可用 --ffmpeg / --ffprobe 显式指定可执行文件路径。"
        ))
        return 2

    print(f"  FFmpeg   : {green(ffmpeg)}")
    print(f"  FFprobe  : {green(ffprobe)}")

    # ---- 输入/输出路径 ----
    in_path = os.path.abspath(args.input)
    if not os.path.exists(in_path):
        print(red(f"[错误] 输入路径不存在：{in_path}"))
        return 2

    if args.output:
        out_dir = os.path.abspath(args.output)
    else:
        if os.path.isfile(in_path):
            out_dir = os.path.join(os.path.dirname(in_path), "amv_output")
        else:
            out_dir = os.path.join(in_path, "amv_output")

    try:
        os.makedirs(out_dir, exist_ok=True)
    except OSError as exc:
        print(red(f"[错误] 无法创建输出目录 {out_dir}：{exc}"))
        return 2

    log_path = os.path.abspath(args.log) if args.log else os.path.join(out_dir, "any_to_amv.log")
    logger = setup_logger(log_path, args.verbose)

    print(f"  输入目录 : {in_path}")
    print(f"  输出目录 : {out_dir}")
    print(f"  日志文件 : {log_path}")
    print(f"  目标参数 : {args.width}x{args.height} @ {args.fps:g}fps, "
          f"block_size={args.block_size}, fit={args.fit}")
    if args.dry_run:
        print(yellow("  模式     : DRY-RUN（只探测，不转码）"))

    # ---- 扫描 ----
    print()
    print(bold("扫描输入文件 ..."))
    files = scan_inputs(in_path, recursive=not args.no_recursive, exclude=[out_dir])

    if not files:
        print(yellow("未找到任何可转码的媒体文件。"))
        print(dim("  支持的视频扩展名：" + ", ".join(sorted(VIDEO_EXTENSIONS - SKIP_EXTENSIONS))))
        print(dim("  支持的音频扩展名：" + ", ".join(sorted(AUDIO_EXTENSIONS))))
        logger.info("扫描完成：未找到候选文件")
        return 0

    print(f"  发现 {green(str(len(files)))} 个候选媒体文件"
          f"（{'含子目录' if not args.no_recursive else '仅当前目录'}）")

    # ---- 全局参数校验（一次性给出警告）----
    global_plan = EncodePlan(src="<global>", dst="", info=MediaInfo(path="<global>"))
    validate_fps(args.fps, global_plan)
    validate_resolution(args.width, args.height, global_plan)
    validate_block_size(args.block_size, global_plan)

    if global_plan.warnings:
        print()
        print(yellow(bold("  AMV 参数校验警告（全局）：")))
        for w in global_plan.warnings:
            print(yellow(f"    ! {w}"))
        for w in global_plan.warnings:
            logger.warning("全局参数校验: %s", w)
    if global_plan.notes:
        for n in global_plan.notes:
            print(dim(f"    · {n}"))

    # ---- 逐文件处理 ----
    stats = Stats(total=len(files))
    print()
    print(bold("=" * 78))
    print(bold("开始处理"))
    print(bold("=" * 78))

    t_start = time.time()
    # 当前正在写入的输出文件；在 dst 计算成功前保持 None，
    # 这样即使中断发生在探测阶段，兜底清理也有明确的判断依据
    current_dst: Optional[str] = None
    try:
      for idx, src in enumerate(files, 1):
        # 单文件输入时 in_path 就是文件本身，relpath 会得到 "."，此时直接用文件名
        if os.path.isfile(in_path):
            rel = os.path.basename(in_path)
        else:
            rel = os.path.relpath(src, in_path)
        print()
        print(f"{bold(f'[{idx}/{len(files)}]')} {blue(rel)}")

        # 1) 扩展名过滤
        if not is_candidate(src):
            print(yellow("  ↷ 跳过：非媒体文件扩展名"))
            stats.skipped_nonvideo += 1
            logger.info("跳过（非媒体扩展名）: %s", src)
            continue

        # 2) ffprobe 探测
        info = probe_media(src, ffprobe)
        if info.probe_error or not info.exists:
            print(red(f"  ↷ 跳过：无法探测/文件损坏（{info.probe_error or '文件不存在'}）"))
            stats.skipped_corrupt += 1
            logger.error("跳过（探测失败）: %s | %s", src, info.probe_error)
            continue

        if not info.has_video and not info.has_audio:
            print(yellow("  ↷ 跳过：文件中没有可解码的音视频流"))
            stats.skipped_nonvideo += 1
            logger.info("跳过（无媒体流）: %s", src)
            continue

        if not info.has_video:
            # 纯音频：生成静态画面
            print(yellow("  ⚠ 纯音频文件，将生成黑屏静态画面的 AMV"))
        if not info.has_audio:
            # AMV muxer 强制要求音视频双流，此处必须合成静音
            print(yellow("  ⚠ 源文件无音频流，将合成静音音轨（AMV 容器强制要求音视频双流）"))

        print(dim(f"  探测：{info.display_resolution}"
                      + (f" (旋转{info.rotation}°)" if info.rotation else "")
                      + f" | DAR {info.display_dar} | SAR {info.display_sar}"
                      + f" | {info.display_fps} | {info.display_duration}"
                      + f" | 视频 {info.v_codec or '-'}"
                      + f" | 音频 {info.a_codec or '-'} {info.sample_rate or '-'}Hz "
                        f"{info.channels or '-'}ch"))

        # 3) 计算输出路径（保持相对结构，仅改扩展名）
        rel_dir = os.path.dirname(rel)
        base = os.path.splitext(os.path.basename(src))[0]
        dst_dir = os.path.join(out_dir, rel_dir) if rel_dir else out_dir
        dst = os.path.join(dst_dir, base + ".amv")
        # 记录当前输出文件，供外层中断兜底清理使用
        current_dst = dst

        # 4) 已存在检查
        if os.path.exists(dst) and not args.overwrite:
            print(yellow(f"  ↷ 跳过：输出已存在（{os.path.relpath(dst, out_dir)}）"
                         f"，使用 --overwrite 覆盖"))
            stats.skipped_exist += 1
            logger.info("跳过（输出已存在）: %s -> %s", src, dst)
            continue

        # 5) 构建转码计划
        #    先创建 plan，再逐个校验参数——所有校验函数都把警告/说明写入
        #    同一个 plan，保证用户能看到完整的参数修正记录。
        plan = EncodePlan(
            src=src, dst=dst, info=info,
            width=args.width, height=args.height,
            fit_mode=args.fit,
        )
        plan.fps = validate_fps(args.fps, plan)
        plan.block_size = validate_block_size(args.block_size, plan)
        plan.width, plan.height = validate_resolution(args.width, args.height, plan)
        if info.has_video:
            plan.scale_filter = build_filter_chain(info, plan.width, plan.height,
                                                   plan.fit_mode, plan)
        # 纯音频文件：视频流由 lavfi color 生成，无需缩放滤镜（见 run_ffmpeg）
        if info.has_audio and info.channels and info.channels > 2:
            plan.add_note(f"源音频 {info.channels} 声道，将混合为单声道（AMV 限制）")
        if info.has_audio and info.sample_rate and info.sample_rate != AMV_AUDIO_SAMPLE_RATE:
            plan.add_note(f"源采样率 {info.sample_rate}Hz，将重采样为 {AMV_AUDIO_SAMPLE_RATE}Hz")

        for n in plan.notes:
            print(dim(f"    · {n}"))
        for w in plan.warnings:
            print(yellow(f"    ! {w}"))
        logger.info("计划: %s -> %s | %dx%d@%dfps bs=%d strict=%s fit=%s vf=%s",
                    src, dst, plan.width, plan.height, plan.fps, plan.block_size,
                    plan.strict or "-", plan.fit_mode, plan.scale_filter)

        # 6) 执行
        try:
            os.makedirs(dst_dir, exist_ok=True)
        except OSError as exc:
            print(red(f"  ✗ 无法创建输出目录 {dst_dir}：{exc}"))
            stats.failed += 1
            logger.error("创建输出目录失败: %s | %s", dst_dir, exc)
            continue

        file_t0 = time.time()
        success, err = run_ffmpeg(plan, ffmpeg, ffprobe, logger,
                                  dry_run=args.dry_run, timeout=args.timeout,
                                  quiet=args.quiet)
        file_dt = time.time() - file_t0

        if success:
            if args.dry_run:
                # dry-run：仅验证了计划可构建，未实际生成文件
                print(green(f"  ✓ 计划就绪 → {os.path.relpath(dst, out_dir)}"))
                logger.info("计划就绪(dry-run): %s -> %s", src, dst)
                stats.planned += 1
                continue
            stats.success += 1
            in_sz = info.size
            out_sz = os.path.getsize(dst) if os.path.exists(dst) else 0
            stats.total_bytes_in += in_sz
            stats.total_bytes_out += out_sz
            ratio = (out_sz / in_sz * 100) if in_sz else 0
            print(green(f"  ✓ 完成（{file_dt:.1f}s）→ "
                        f"{os.path.relpath(dst, out_dir)} "
                        f"[{format_bytes(in_sz)} → {format_bytes(out_sz)}, "
                        f"{ratio:.1f}%]"))
            logger.info("成功: %s -> %s | %.1fs | %d -> %d bytes",
                        src, dst, file_dt, in_sz, out_sz)
        else:
            stats.failed += 1
            if "用户中断" in err:
                # 中断不是"转码失败"，单独归类，避免误导用户
                stats.interrupted = True
                print(yellow("  ↷ 已中断，正在清理 ..."))
                logger.warning("中断: %s -> %s", src, dst)
                break                       # 收到中断即停止处理后续文件
            print(red(f"  ✗ 失败：{err}"))
            logger.error("失败: %s -> %s | %s", src, dst, err)
            # 清理残留的不完整输出（可能是很大的半成品，不只是 0 字节）
            # run_ffmpeg 内部已尝试删除，这里兜底再删一次
            _remove_partial(dst, logger, stats)

    except KeyboardInterrupt:
        # 兜底：若中断发生在 run_ffmpeg 之外（如探测阶段），
        # 这里负责记录状态并清理可能已开始的输出文件
        stats.interrupted = True
        logger.warning("在处理循环中收到中断信号")
        print(yellow("\n↷ 收到中断信号，正在清理 ..."))
        _remove_partial(current_dst, logger, stats)
    except Exception:
        # 其它未预期异常：同样要清理半成品，避免留下脏数据
        logger.exception("处理循环中发生未预期的异常")
        _remove_partial(current_dst, logger, stats)
        raise

    stats.total_time = time.time() - t_start

    # ---- 汇总 ----
    print()
    print(bold("=" * 78))
    print(bold("处理完成 — 汇总"))
    print(bold("=" * 78))
    print(f"  候选文件总数   : {stats.total}")
    if args.dry_run:
        print(f"  {green('计划就绪')}       : {stats.planned}")
    else:
        print(f"  {green('成功转码')}       : {stats.success}")
    if stats.interrupted:
        print(f"  {yellow('被用户中断')}     : 是")
        if stats.partial_removed:
            print(f"  {yellow('已清理半成品')}   : {len(stats.partial_removed)} 个")
    print(f"  跳过（已存在） : {stats.skipped_exist}")
    print(f"  跳过（非视频） : {stats.skipped_nonvideo}")
    print(f"  跳过（损坏）   : {stats.skipped_corrupt}")
    print(f"  {red('失败')}           : {stats.failed}")
    print(f"  总耗时         : {stats.total_time:.1f}s")
    if stats.total_bytes_in:
        print(f"  总大小         : {format_bytes(stats.total_bytes_in)} → "
              f"{format_bytes(stats.total_bytes_out)}"
              f"（{stats.total_bytes_out / stats.total_bytes_in * 100:.1f}%）")
    print(f"  输出目录       : {out_dir}")
    print(f"  详细日志       : {log_path}")

    logger.info("汇总: total=%d planned=%d ok=%d exist=%d nonvideo=%d corrupt=%d "
                "failed=%d interrupted=%s partial_removed=%d time=%.1fs",
                stats.total, stats.planned, stats.success, stats.skipped_exist,
                stats.skipped_nonvideo, stats.skipped_corrupt, stats.failed,
                stats.interrupted, len(stats.partial_removed), stats.total_time)

    # 中断是用户主动行为，不算"失败"，返回专用退出码 130
    if stats.interrupted:
        return 130
    return 0 if stats.failed == 0 else 1


if __name__ == "__main__":
    exit_code = 1
    try:
        exit_code = main()
    except KeyboardInterrupt:
        # 正常流程下 main() 内部已处理中断；此处仅兜底极早期（如参数解析前）的中断
        print(red("\n\n用户中断（Ctrl+C），已停止处理。"))
        exit_code = 130
    finally:
        # 确保标准输出被 flush，避免终端上出现丢行/半行
        try:
            sys.stdout.flush()
            sys.stderr.flush()
        except (ValueError, OSError):
            pass
    sys.exit(exit_code)
