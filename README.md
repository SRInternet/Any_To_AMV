# <img src="convert.png" width="35" /> Any To AMV Converter

将任意目录（含子目录）下的媒体文件批量转换为 AMV 格式的跨平台脚本。基于 FFmpeg / FFprobe，Python 3.8+，支持 Windows / Linux / macOS。

AMV（Advanced Media Video）是 MP3/MP4 播放器时代流行的容器格式，视频编码 `amv`（mpegvideo 系，全 I 帧），音频编码 `adpcm_ima_amv`（IMA ADPCM，22050 Hz 单声道），像素格式固定 `yuvj420p`。它体积小、兼容老旧硬件，但画质与压缩率远低于现代格式。

## 环境要求

- Python 3.8+
- FFmpeg + FFprobe（需在 `PATH` 中，或用 `--ffmpeg` / `--ffprobe` 指定路径）

标准 FFmpeg 发行版均内置 `amv` 编码器。安装方式：

| 平台 | 安装方式 |
| --- | --- |
| Windows | 从 [gyan.dev](https://www.gyan.dev/ffmpeg/builds/) 下载 full build，解压后将 `bin` 目录加入 `PATH` |
| Linux | `sudo apt install ffmpeg` |
| macOS | `brew install ffmpeg` |

## 快速开始

```bash
# 一键运行（当前目录 -> ./amv_output，320x240 @ 18fps）
python any_to_amv.py

# 指定输入/输出目录
python any_to_amv.py -i "D:/videos" -o "D:/amv"

# 自定义分辨率（不合规时自动就近修正）
python any_to_amv.py -i "D:/videos" --width 208 --height 160

# 只探测并预览转码计划，不实际转码
python any_to_amv.py -i "D:/videos" --dry-run

# 覆盖已存在的输出
python any_to_amv.py -i "D:/videos" --overwrite
```

默认行为：递归扫描输入目录 → 输出到 `<输入目录>/amv_output/`（保留原有目录结构，仅将扩展名改为 `.amv`）→ 目标参数 `320x240 @ 18fps`。已存在的输出默认跳过，不覆盖。

## AMV 格式限制与自动修正策略

以下限制来自 FFmpeg `amv` 编码器本身，脚本会在不合规时自动修正并打印警告。

| 项目 | AMV 要求 | 修正策略 |
| --- | --- | --- |
| 帧率 | 仅 `18fps` | 任何其它值自动改为 18，并输出警告 |
| `block_size` | 仅 `1225`（35×35 宏块网格） | 任何其它值自动改为 1225 |
| 分辨率 | 有原生支持列表（见下） | 就近取整到 16 的倍数；仍不可行则附加 `-strict experimental` |
| 流数量 | 必须同时含视频流 + 音频流 | 源文件缺音频时合成静音；缺视频时生成纯黑静态画面 |
| 像素格式 | `yuvj420p` | 统一通过 `format=yuvj420p,setrange=full,setsar=1:1` 声明 full-range 语义 |
| 音频 | `adpcm_ima_amv`，22050 Hz 单声道 | 源音频任意采样率/声道数都会重采样与混合 |

amv 编码器原生支持的分辨率：

```
16x16  32x16  96x160  128x96  176x144  192x144  208x96  208x160  210x160
224x176 256x192 320x240 352x240 352x288 384x288 640x368 640x480
```

## 命令行参数

### 输入 / 输出

| 参数 | 默认值 | 说明 |
| --- | --- | --- |
| `-i`, `--input` | `.` | 输入目录或单个文件 |
| `-o`, `--output` | `<输入目录>/amv_output` | 输出目录 |
| `--no-recursive` | 关闭 | 不递归子目录（默认递归） |
| `--overwrite` | 关闭 | 覆盖已存在的输出文件（默认跳过） |
| `--log` | `<输出目录>/any_to_amv.log` | 日志文件路径 |

### AMV 目标参数

| 参数 | 默认值 | 说明 |
| --- | --- | --- |
| `--width` | `320` | 目标宽度 |
| `--height` | `240` | 目标高度 |
| `--fps` | `18` | 目标帧率（编码器仅支持 18，其它值自动修正） |
| `--block-size` | `1225` | AMV block_size（编码器仅接受该值） |
| `--fit` | `pad` | 宽高比适配方式：`pad` 等比缩放加黑边；`crop` 等比放大居中裁剪；`stretch` 直接拉伸 |

### 运行控制

| 参数 | 说明 |
| --- | --- |
| `--dry-run` | 只探测元数据并打印转码计划，不实际执行转码 |
| `-v`, `--verbose` | 更详细的日志输出（DEBUG 级） |
| `--quiet` | 减少控制台输出（不显示进度条与 FFmpeg 命令） |
| `--timeout` | 单个文件转码超时秒数（默认不限制） |

### FFmpeg 路径

| 参数 | 说明 |
| --- | --- |
| `--ffmpeg` | ffmpeg 可执行文件路径（默认从 `PATH` 及常见安装目录查找） |
| `--ffprobe` | ffprobe 可执行文件路径 |

## 工作原理

1. **环境自检** — ffmpeg / ffprobe 是否可用，是否包含 `amv` 编码器
2. **递归扫描** — 按扩展名过滤候选文件，跳过 `.tmp` / `.git` / `venv` / 输出目录自身
3. **ffprobe 探测** — 分辨率、DAR/SAR、帧率、时长、视频/音频编码、采样率、声道数、旋转角度、像素格式；探测失败或 0 字节的文件标记为损坏并跳过
4. **参数校验** — fps → 18，block_size → 1225，分辨率按上表修正；依据源 DAR 计算缩放滤镜
5. **执行转码** — 实时显示结构化进度条，同时记录完整日志
6. **输出校验** — 文件非空 + ffprobe 复核编码器与音频参数

单文件转码命令形如：

```bash
ffmpeg -hide_banner -nostdin -y -fflags +genpts+igndts \
  -i input.mp4 \
  -map 0:v:0 -c:v amv \
  -vf "scale=320:240:force_original_aspect_ratio=decrease,pad=320:240:(ow-iw)/2:(oh-ih)/2:color=black,format=yuvj420p,setrange=full,setsar=1:1" \
  -s 320x240 -r 18 -block_size 1225 -pix_fmt yuvj420p -bf 0 -sc_threshold 0 \
  -map 0:a:0 -c:a adpcm_ima_amv -ar 22050 -ac 1 \
  -f amv -metadata title=input \
  -progress pipe:1 -nostats \
  output.amv
```

`amv` 是 mpegvideo 系编码器，关闭 B 帧与场景切换侦测可提升老旧硬件兼容性。

## 中断处理

按 `Ctrl+C` 或发送 `SIGTERM` 即可中断。脚本会依次执行：

1. 终止当前 ffmpeg 子进程（`terminate()` 请求优雅退出，2 秒内无响应则 `kill()` 强杀，并 `wait()` 回收）
2. 关闭 stdout/stderr/stdin 管道句柄，释放对输出文件的占用
3. 删除当前文件的半成品 `.amv`
4. 停止处理后续文件并输出汇总统计

Python 只对 `SIGINT` 默认抛 `KeyboardInterrupt`，`SIGTERM` 的默认行为是立即终止进程、不执行 `finally`。因此脚本显式安装信号处理器，把 `SIGTERM` / `SIGHUP` 也转换为 `KeyboardInterrupt`，确保任何形式的中断都走同一套清理路径。

中断时退出码为 `130`，汇总中会显示 `被用户中断` 与 `已清理半成品` 数量。

## 输出示例

```
[2/3] movie.mp4
  探测：1920x1080 | DAR 16:9 | SAR 1:1 | 30.000fps | 00:03:00.100 | 视频 h264 | 音频 aac 48000Hz 2ch
    · 源 DAR 1.778 与目标 DAR 1.333 不同，将等比缩放并填充黑边以保持宽高比
    · 源采样率 48000Hz，将重采样为 22050Hz
  [████████████████████████░░░░░░░░]  71.2%  [00:02:08.333 / 00:03:00.100]
  ✓ 完成（12.4s）→ movie.amv [158.7MiB → 364.3KiB, 0.2%]

[3/3] broken.mp4
  ↷ 跳过：无法探测/文件损坏（moov atom not found）

==============================================================================
 处理完成 — 汇总
==============================================================================
  候选文件总数   : 3
  成功转码       : 2
  跳过（已存在） : 0
  跳过（非视频） : 0
  跳过（损坏）   : 1
  失败           : 0
  总耗时         : 14.1s
  输出目录       : D:\videos\amv_output
  详细日志       : D:\videos\amv_output\any_to_amv.log
```

## 常见问题

**输出文件比源文件还大？**
AMV 使用全 I 帧 mpegvideo 编码，几乎没有帧间压缩，压缩率远低于 H.264/H.265。对高分辨率或长时长源文件，输出体积可能反而更大，这是格式固有特性。可降低目标分辨率或只转换短视频片段。

**`ffprobe` 显示容器是 `avi` 而不是 `amv`？**
AMV 容器底层就是 AVI 变体，`format_name` 会报告 `avi`。判断是否为有效 AMV 应看视频编码器是否为 `amv`，脚本正是这样校验的。

**纯音频文件（如 mp3）能转换吗？**
能。AMV 容器强制要求音视频双流，脚本会为纯音频文件生成纯黑静态画面。若只需提取音频，不建议转成 AMV。

**含中文/日文/特殊字符的文件名？**
脚本内部统一使用 UTF-8，并在 Windows 上自动将标准输出切换为 UTF-8，可正确处理中文、日文、emoji 等文件名。

**`--dry-run` 模式下汇总显示"计划就绪"？**
`--dry-run` 不生成任何文件，该计数表示对应文件的探测与转码计划已成功构建。

## 文件说明

| 文件 | 说明 |
| --- | --- |
| `any_to_amv.py` | 主脚本（单文件，无第三方依赖，仅需 Python 标准库） |
| `README.md` | 本文档 |

日志文件默认写入 `<输出目录>/any_to_amv.log`，包含每条 FFmpeg 的完整原始命令，便于复现问题。

## 退出码

| 退出码 | 含义 |
| --- | --- |
| `0` | 全部成功（或无失败） |
| `1` | 存在转码失败的文件 |
| `2` | 环境/参数错误（FFmpeg 未安装、输入路径不存在等） |
| `130` | 用户中断（Ctrl+C / SIGTERM） |
