# AIFOS V3.0 —— AI 精品漫剧工业化生产平台

> 定位:Production Platform。先跑通完整生产流程,再逐步升级到企业级 V4。

输入一句 **「开始制作《万妖图录》第15集」**,平台自动完成剧本、分镜、图片、
首尾帧、视频、配音、剪辑、质检、封面/标题/拆条和数据沉淀:

```bash
python3 -m aifos init                                    # 初始化工作区
python3 -m aifos produce "开始制作《万妖图录》第15集"      # 一句话开工
python3 -m aifos status                                  # 制作状态看板
```

零第三方运行时依赖(仅 Python 标准库 + SQLite;测试用 pytest)。

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
      "command": ["/Users/sk/.local/node22/bin/codex", "exec", "--json"]
    }
  }
}
```

注:`codex` 走通用 CLI 协议,接真实 Codex 需一个把上述 JSON 协议转为
`codex exec` 调用的薄包装脚本;`dreamina` 无需包装,开箱即用。
每次 dreamina 调用的完整命令与原始输出都会落盘到
`artifacts/.../videos/shot_XXX.dreamina.log` 便于排查。

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
