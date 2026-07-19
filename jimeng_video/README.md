# jimeng_video · 《卡卡学姐的第一课》手机 Seedance 2 生成包 CLI

一个把「手机 Seedance 2 首尾帧逐段生成」的成果**校验 + 拼接成 102 秒成片**的命令行工具。
视频**生成本身仍在手机端 Seedance 2.0 完成**（见生成说明书）；本 CLI 负责生成前后的
本地工序：目录骨架、参考帧/衔接帧/提示词/规格校验，以及后期精确变速与拼接。

> 本包与仓库里的 `paipan_ref/`（排盘参考实现）互不相关，只是同处一个仓库。

## 依赖

- Python 3.11+（仅标准库）。
- 拼接/媒体探测需本机 **ffmpeg（含 ffprobe）**；macOS：`brew install ffmpeg`。
  没装 ffmpeg 时 `plan` / `init` / `check --no-probe` 仍可用。

## 素材放哪

把素材放在本机可访问的路径，例如 iCloud 里的
`4k高清/卡卡学姐的第一课_手机Seedance2生成包/`，目录结构：

```
卡卡学姐的第一课_手机Seedance2生成包/
├── 00_参考帧/          # 全部首帧/尾帧/共享衔接帧 PNG
├── 01_逐段提示词/       # U01.txt … U07.txt（从原始生成包整段粘贴）
├── 05_生成视频放这里/    # 手机下载的 U01.mp4 … U07.mp4
└── 06_成片/            # CLI 输出的 102 秒成片
```

## 用法

```bash
# 1) 打印分段 / 变速 / 102 秒计划（离线，随时看）
python3 -m jimeng_video plan

# 2) 在目标位置建目录骨架 + 提示词占位（幂等）
python3 -m jimeng_video init "~/…/4k高清/卡卡学姐的第一课_手机Seedance2生成包"

# 3) 校验参考帧齐全、共享衔接帧完全一致、提示词已填、视频 720p/9:16/≈15s
python3 -m jimeng_video check "<生成包目录>"
python3 -m jimeng_video check "<生成包目录>" --no-probe   # 无 ffmpeg 时只查文件存在

# 4) 逐段变速到 14.5s（U03 保留 15s）→ 拼成精确 102 秒成片
python3 -m jimeng_video assemble "<生成包目录>"
python3 -m jimeng_video assemble "<生成包目录>" --dry-run  # 只打印 ffmpeg 命令
python3 -m jimeng_video assemble "<生成包目录>" -o /path/out.mp4 --no-audio --fps 30
```

## 规格来源（全部来自生成说明书）

- 每段 Seedance 2.0 / 首尾帧 / 720p / 9:16 竖屏 / 15 秒 / 每段先 1 条；不要字幕贴纸水印，关闭自动配乐（环境音可留）。
- 后期 U01/U02/U04/U05/U06/U07 变速到 **14.5s**，U03 保留 **15s** → `6×14.5 + 15 = 102.0s`。
- U01/U02、U02/U03、U06/U07 使用**完全相同的共享衔接帧**（`check` 用 SHA-256 逐对比对）。

`assemble` 的变速比按每段**实测时长**计算（`目标/实测`），因此源片略偏 15s 也能精确落到目标；
成片完成后自检总时长是否落在 102.0s ± 0.2s。

## 人工目检 6 项（CLI 不自动判定）

分辨率、时长、衔接帧一致性可自动校验；下列画面/人物项仍需人工目检，`check` 只做提醒：
角色只有卡卡与小豆、发色制服稳定、不换脸换装、道具不畸形/不消失、无可读文字水印、尾帧姿势自然到位。

## 设计要点

- `spec.py` 只存事实常量；命令**构造**是纯函数（`ffmpeg.build_retime_cmd` / `build_concat_cmd`），
  与 subprocess **执行**分离，便于离线单测。
- 拼接分两步：逐段重定时并归一化编码（720×1280 / yuv420p / 指定 fps / aac48k；缺音轨自动补等长静音），
  再用 concat demuxer `-c copy` 无损拼接，避免各段流参数不一致导致拼接失败。
- 测试见 `tests/test_jimeng_video.py`（不依赖本机 ffmpeg）。
```bash
python3 -m pytest -q tests/test_jimeng_video.py
```
