# AIFOS V3.0 —— AI 精品漫剧工业化生产平台

> 定位:Production Platform。先跑通完整生产流程,再逐步升级到企业级 V4。

输入一句 **「开始制作《万妖图录》第15集」**,平台自动完成剧本、分镜、图片、
首尾帧、视频、配音、剪辑、质检、封面/标题/拆条和数据沉淀:

```bash
python3 -m aifos init                                    # 初始化工作区
python3 -m aifos produce "开始制作《万妖图录》第15集"      # 一句话开工
python3 -m aifos status                                  # 制作状态看板
python3 -m aifos serve                                   # Web 控制台 + 分镜画布
```

零第三方运行时依赖(仅 Python 标准库 + SQLite;测试用 pytest;
Web 控制台为纯标准库 http.server + 原生 JS,离线可用)。

## Web 控制台(`aifos serve` → http://127.0.0.1:8619)

- **仪表盘**:一句话开工输入框(后台异步制作、自动刷新)、剧集状态看板、
  成本按阶段/Provider 统计、订阅额度、IP 资产沉淀概览、实时日志;
- **分镜画布**(点击剧集进入):按场分行陈列全部镜头卡片——关键图、
  机位、时长、台词、视频/配音/质检徽章;支持**滚轮缩放、拖拽平移、
  卡片自由摆放(本地持久化)、适应视图、一键重排**;点击卡片右侧
  面板展示首尾帧、生成 Prompt、全部产物链接与该镜头质检问题;
  底部时间轴按场时长比例拆条,点击跳转对应场次;
- 产物文件(关键图 SVG、成片、封面、拆条、dreamina 日志)经
  `/artifacts/` 直接在线预览,含目录穿越防护。

## 八大核心模块 → 代码映射

| 模块 | 职责 | 代码 |
|---|---|---|
| 项目中心 | 项目、剧集、剧本、分镜、制作状态统一管理 | `project_center.py` |
| IP资产中心 | 角色/场景/动作/镜头/Prompt/首尾帧/图片/视频/配音沉淀,版本管理与复用 | `asset_center.py` |
| AI导演中心 | 总控:拆解任务、调度 Provider、控制流程与成本、质检自动重跑 | `director.py` |
| AI生产中心 | Codex(图片)、即梦 CLI(视频/配音,优先订阅额度)、API 备用、剪映剪辑 | `production/` |
| AI质检中心 | 角色一致性、镜头连续性、字幕、配音、敏感内容检测与评分 | `qc_center.py` |
| AI运营中心 | 自动封面、标题、拆条(后续扩展发布与数据分析) | `ops_center.py` |
| 数据中心 | Prompt、图片、视频、成功/失败案例沉淀,JSONL 导出 | `data_center.py` |
| 系统中心 | 权限、日志、Provider(CLI/API)管理、成本统计、配置管理 | `system_center.py` `config.py` |

## 生产流程

```
需求 → 剧本 → 分镜 → 资产调用 → 图片 → 首尾帧 → 视频 → 配音
     → 剪映 → AI质检 → 封面/标题 → 数据沉淀
```

由 AI 导演中心按上述十一个阶段顺序调度;每个阶段落任务表(`tasks`),
统一成本记账,超出单集预算(`budget.per_episode`)立即熔断。
质检发现可修复缺陷(镜头视频/配音缺失)时自动重跑对应产物并重新剪辑、
复检,最多 `retry.max_retries` 轮;不可修复问题(敏感词、角色不一致)
写入质检报告。

**增量生产(断点续产)**:默认复用已落盘完好的剧本/分镜/图片/首尾帧/
视频/配音,只补齐缺失部分——真实产线(即梦按镜头消耗额度)中断后
重跑 `produce` 不会重复烧钱;`--force`(或画布"强制重制")全量重新
生成。

## 模型协同与 Provider 路由

能力(script/storyboard/image/frames/video/voice/edit/cover)按
`routing` 配置的优先级链路由,前者不可用自动回退后者:

```
video: 即梦CLI(订阅额度内) → API 备用 → mock
image: Codex → API 备用 → mock
```

- **即梦 CLI 优先使用订阅额度**:`quota` 表本地计数 + `dreamina
  user_credit` 实时余额(`aifos stats` 展示;配 `min_credit` 可在余额不足
  时自动降级),额度耗尽自动回退 API;
- **外部 Provider 默认关闭**(`enabled: false`),未接入真实 CLI/API 时
  由内置 **Mock Provider** 确定性生成占位产物,保证全流程离线可跑、可测;
- 接入真实产线:`workspace/config.json` 中把对应 Provider `enabled` 置
  `true` 并配置 `command`(CLI)或 `endpoint`/`api_key`(API)。

通用 CLI Provider 协议:stdin 传入一行 JSON
`{"capability", "payload", "out_dir"}`,stdout 返回
`{"ok", "data", "uri", "cost"}`。

### 即梦官方 CLI(dreamina)原生接入

`jimeng` Provider(`production/dreamina.py`)直接调用即梦官方 CLI,
无需适配脚本。视频阶段自动执行(与即梦 CLI 规范对齐):

```bash
dreamina frames2video \
  --first=<首帧> --last=<尾帧> --prompt=<分镜提示词> \
  --duration=8 --video_resolution=720p \
  --model_version=seedance2.0fast_vip --poll=30
```

> ⚠️ `model_version` 平台默认钉死 **`seedance2.0fast_vip`**(Fast VIP),
> 不要使用旧脚本(submit-seedance2.py)中的 `seedance2.0_vip`;
> 测试项 `test_default_config_pins_fast_vip` 防止回归。
> `ark-seedance2.py` 属火山方舟 API 路线,对应本平台的 `api` 备用
> Provider,不走 dreamina 适配器。

macOS 实机 `workspace/config.json` 示例:

```json
{
  "providers": {
    "jimeng": {
      "enabled": true,
      "command": ["/Users/sk/.local/bin/dreamina"]
    },
    "codex": {
      "enabled": true,
      "command": ["python3", "-m", "aifos.adapters.codex_image",
                  "--codex", "/Users/sk/.local/node22/bin/codex"]
    }
  }
}
```

注:`codex` 经内置适配桥 `aifos.adapters.codex_image` 转为
`codex exec` 出图指令(image/frames/cover 三能力,产出后校验文件
落盘),按上例把 `--codex` 指向绝对路径即可;`dreamina` 无需包装,
开箱即用。每次外部调用的完整命令与原始输出都会落盘
(`shot_XXX.dreamina.log` / `codex_*.log`)便于排查。

## 账号矩阵(抖音等短视频平台)

一个项目 = 一个账号的内容线,支持两种内容类型,同一条流水线生产:

```bash
# 漫剧账号(连载剧情)
python3 -m aifos project create --title 万妖图录 --kind drama --account wyt_official
# AI 虚拟偶像账号(人设口播;项目名即偶像人设名,跨期人设一致)
python3 -m aifos project create --title 小澜同学 --kind idol --account xiaolan_ai
python3 -m aifos produce --title 小澜同学 --episode 1 --premise 新歌翻唱
python3 -m aifos publish --project 小澜同学 --episode 1   # 发布包
```

- **画幅**:全局默认 9:16(1080×1920 竖屏),项目可设 `--aspect 16:9`;
  贯通分镜 Prompt、出图尺寸、首尾帧与剪辑;
- **发布包**:每集自动产出 `publish/publish.json`——账号、成片、封面、
  3 个候选标题、话题标签(按类型:#漫剧/#AI虚拟偶像)、按场拆条与
  错峰发布建议,人工到创作者中心一键上传(开放平台 API 自动发布留待
  扩展);
- **偶像模板**:开场钩子 → 主体内容 → 引导关注的口播结构;
  Claude 编剧桥有对应的 IDOL_PROMPT,Mock 亦内置同构模板。

## 常用命令

```bash
python3 -m aifos produce --title 万妖图录 --episode 16 --premise "……"
python3 -m aifos asset list --project 万妖图录 --kind character
python3 -m aifos asset stats --project 万妖图录        # 资产复用统计
python3 -m aifos qc --project 万妖图录 --episode 15    # 质检报告
python3 -m aifos stats                                 # 成本 + 订阅额度
python3 -m aifos archive export --out dump.jsonl       # 数据沉淀导出
python3 -m aifos logs --limit 50
python3 -m aifos user add --name guest --role viewer   # 权限管理
```

## 测试

```bash
python3 -m pytest -q tests/test_aifos_*.py
```

覆盖:端到端流水线、资产跨集复用、确定性重跑、敏感词质检失败路径、
预算熔断、Provider 降级回退、订阅额度耗尽降级、质检各检查项、
质检自动重跑修复、CLI 一句话指令解析与权限控制。

## V3 暂不开发(放入 V4)

微服务、Kubernetes、知识图谱、MCP 生态、多租户、分布式调度。
