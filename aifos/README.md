# AIFOS V3.2 —— AI 精品漫剧工业化生产平台

> 定位:Production Platform。先跑通完整生产流程,再逐步升级到企业级 V4。

输入一句 **「开始制作《万妖图录》第15集」**,平台按 SK 漫剧 V5 工业流完成
连续性档案、五维分镜、文字关键帧、首尾帧、Seedance 视频内同步人声与
口型、无字幕母版、三层质检、运营包装和数据沉淀:

```bash
python3 -m aifos init                                    # 初始化工作区
python3 -m aifos produce "开始制作《万妖图录》第15集"      # 一句话开工
python3 -m aifos status                                  # 制作状态看板
python3 -m aifos serve                                   # Web 控制台 + 分镜画布
```

零第三方运行时依赖(仅 Python 标准库 + SQLite;测试用 pytest;
Web 控制台为纯标准库 http.server + 原生 JS,离线可用)。

## 手机版 / 添加到主屏幕

双击仓库根目录的 `start_aifos.command` 会以局域网模式启动。终端会同时显示
本机地址和手机地址；让手机与电脑连接同一 Wi-Fi，在 Safari 或 Chrome 输入
该手机地址即可使用完整的响应式工作台。也可以手动启动：

```bash
python3 -m aifos serve --lan --port 8619
```

页面右上角的“手机打开”会列出当前可用地址，并支持复制或发送到手机，同时
生成一张**局域网地址二维码**——手机扫一扫即可直接打开，免去手输 IP。
iPhone/iPad 用 Safari 的“分享 → 添加到主屏幕”；Android 用 Chrome 的
“菜单 → 安装应用/添加到主屏幕”。安装后以独立窗口运行，底部提供生产总览、
历史、资产、标准和 AI 设置五栏导航。手机访问只面向可信的同一 Wi-Fi。

## 外网访问 / 隧道(`aifos tunnel`)

不在同一 Wi-Fi(如在外面用手机)时,用 `cloudflared` 把本机 AIFOS 暴露到
公网。免费的 trycloudflare 快速隧道**每次重启都会换新网址**,手机里存的旧
网址一连就 TLS 失败——`aifos tunnel` 把“当前公网地址”做成随时可见、可扫码,
不用再猜:

```bash
# 先在本机装好 cloudflared(brew install cloudflared),然后:
python3 -m aifos serve --lan            # 一个终端跑控制台
python3 -m aifos tunnel                 # 另一个终端起隧道
```

`aifos tunnel` 会:自动解析 cloudflared 分配的公网地址 → 写入
`workspace/public_url.json` → 在终端打印**二维码**(与终端配色无关,深色终端
也不反相)→ 运行中的 `serve` 进程据此在网页“手机打开 → 外网访问”里显示同一
二维码。手机扫码即得最新地址,地址换了重跑本命令刷新即可,**无需重装 App**。

想要**永不改变的稳定地址**(一次配置长期可用),用命名隧道:

```bash
# 先用 cloudflared 建好命名隧道并把 ingress 指向本机端口,拿到稳定域名后:
python3 -m aifos tunnel --name my-aifos --url https://aifos.example.com
```

若你已经用别的方式拿到了外网地址,只想让平台展示地址与二维码:

```bash
python3 -m aifos tunnel --record https://xxxx.trycloudflare.com/
```

安全提示:外网隧道会把控制台暴露到公网,当前版本没有身份认证,仅在需要时
开启,并优先使用命名隧道 + Cloudflare Access 等鉴权;不用时关掉隧道进程即可。

## Web 控制台(`aifos serve` → http://127.0.0.1:8619)

- **仪表盘**:一句话开工输入框(后台异步制作、自动刷新)、剧集状态看板、
  成本按阶段/Provider 统计、订阅额度、IP 资产沉淀概览、实时日志;
- **制作标准中心**(`#/standards`):把 `sk-manju-storyboard-skill`、五维分镜
  模板和交付规则变成可编辑的生产合同;按基础生产、分段节奏、台词、表演、
  镜头、连续性、声音交付和质量门禁分类编辑,支持即时校验、版本说明、历史
  激活、恢复厂标及 JSON 导入/导出;
- **分镜画布**(点击剧集进入):按场分行陈列全部镜头卡片——关键图、
  五维参数、起止状态、剧本映射、人物数量、时长、即梦声画与质检徽章;
  支持**滚轮缩放、拖拽平移、
  卡片自由摆放(本地持久化)、适应视图、一键重排**;点击卡片右侧
  面板展示首尾帧、Seedance Prompt、文字资产规则、全部产物链接与该镜头
  质检问题;默认开拍前必须通过 11 项门禁,质检页可打开逐段图文检查板;
  底部时间轴按场时长比例拆条,点击跳转对应场次;
- 产物文件(关键图 SVG、成片、封面、拆条、dreamina 日志)经
  `/artifacts/` 直接在线预览,含目录穿越防护。

## 九大核心模块 → 代码映射

| 模块 | 职责 | 代码 |
|---|---|---|
| 项目中心 | 项目、剧集、剧本、分镜、制作状态统一管理 | `project_center.py` |
| IP资产中心 | 角色/场景/动作/镜头/Prompt/首尾帧/图片/视频/配音沉淀,版本管理与复用 | `asset_center.py` |
| AI导演中心 | 总控:拆解任务、调度 Provider、控制流程与成本、质检自动重跑 | `director.py` |
| 制作标准中心 | SK Skill/制作规则结构化、校验、不可变版本、激活指针、逐集快照与交换包 | `standard_center.py` |
| AI生产中心 | ChatGPT/Codex 关键帧、Seedance 2.0 Fast VIP 720P、随视频配音/口型、豆包 TTS 备选、剪映剪辑 | `production/` |
| AI质检中心 | 结构/连续性/声画技术检查 + 抽帧检查板 + 逐段内容复核 + 交付脚本 | `qc_center.py` `workflow.py` |
| AI运营中心 | 自动封面、标题、拆条(后续扩展发布与数据分析) | `ops_center.py` |
| 数据中心 | Prompt、图片、视频、成功/失败案例沉淀,JSONL 导出 | `data_center.py` |
| 系统中心 | 权限、日志、Provider(CLI/API)管理、成本统计、配置管理 | `system_center.py` `config.py` |

## 生产流程

```
需求/剧本 → 连续性圣经 → 五维分镜 → 角色/场景资产 → 关键帧
         → 文字锁定 → 首尾帧 → 11项开拍门禁 → Seedance视频
         → Seedance2 随视频配音/口型 → 无字幕母版剪辑 → 三层质检
         → 封面/标题/拆条 → 数据沉淀
```

AI 导演中心按 14 个阶段顺序调度;每个阶段落任务表(`tasks`),
统一成本记账,超出单集预算(`budget.per_episode`)立即熔断。
默认视频生成前必须通过 **11 项门禁**:连续性、五维分镜、时长与时间码、
台词与语速、表演空间、镜头语言、人物数量、画面文字、首尾帧、声音设计和
生产规格。质检发现可修复的视频缺失时自动重跑并重新剪辑、复检,最多
`retry.max_retries` 轮;不可修复问题写入质检报告。

生产合同默认钉死 **Seedance 2.0 Fast VIP / 720P / 随视频配音与口型 /
无字幕母版**。台词逐字映射到视频单元,关键台词后自动补听者反应镜,
场尾补有内容的情绪留白;单元时长按 0.5 秒粒度且不超过 15 秒。画面中确需
出现的文字必须先由 ChatGPT 关键帧锁定,Seedance 仅保持原字。

**增量生产(断点续产)**:默认复用已落盘完好的剧本/分镜/图片/首尾帧/
视频与集成声画,只补齐缺失部分——真实产线(即梦按镜头消耗额度)中断后
重跑 `produce` 不会重复烧钱;`--force`(或画布"强制重制")全量重新
生成。

## 制作标准中心

制作标准不是说明文档,而是每次生产都会读取的结构化合同。默认标准来源为
`sk-manju-storyboard-skill` 和 `five-dimension-storyboard-template-v5.txt`,
内容保存在 SQLite。版本记录只追加、不覆盖;“当前生效标准”只是一个可切换
的指针,因此任何历史生产口径都可追溯。

### 硬规则与可调规则

以下硬规则用于防止模型、声画和交付规格在制作中静默漂移,保存时不可改:

- `Seedance 2.0 Fast VIP`、`720P`;
- Seedance2 在视频单元内同步生成角色人声与口型,豆包 TTS 仅作无声视频兼容备选;
- 不烧录字幕,交付字幕轨为空;
- Fast VIP 遇到真人脸限制时暂停确认,不得自动切换普通 VIP。

在硬边界内可按项目调整节奏和导演标准,常用项包括:

- 建议分段区间、单段最长时长与时间码精度;
- 单镜台词字数、四类情绪语速和缓冲时长;
- 听者反应镜占比、反应镜/留白镜时长、动作是否独立成镜;
- 每段纵向角度数量、相邻景别跳级、30° 机位原则和摄影机选项库;
- 人物/服装/道具/站位连续性、文字关键帧、环境声与交付复核;
- 11 项质量门的名称、启用状态和阻断/警告级别。门禁 id 与顺序固定,保证
  分镜、预检、UI 和导出报告能使用同一套语义。

表单会在保存前检查范围、必填字段、五维字段、17 列镜头合同、8 类场景词、
11 个门禁以及无字幕交付条件。非法标准不会产生半个版本;并发保存会使用
`expected_active_id` 检测旧页面覆盖新版本的冲突。

### 版本、激活、重置与交换

- **保存**:每次保存生成新的不可变版本;可立即激活,也可先保存为未激活版本。
- **激活**:历史版本可一键重新设为当前生效标准,不会删除较新的版本。
- **重置**:以 SK V5 厂标内容创建并激活一个新版本,不是清空历史。
- **导出/导入**:JSON 标准包不含密钥,带 schema 与 SHA-256 内容指纹;导入
  会验证指纹,损坏或被篡改的包不会入库。

### 每集快照、暂停确认与 `force`

新剧集第一次生产时会把当前生效标准完整保存为 `production_standard` 单集
快照,分镜、门禁、质检和交付均记录同一指纹。之后切换全局标准不会追改已经
开工的剧集:

- `pause_for_confirm=True` 在剧本、连续性、角色/场景、五维分镜、关键帧、
  文字资产、首尾帧和门禁完成后暂停;
- 用户确认时再次调用普通 `produce`,继续使用暂停时的旧快照;
- 普通断点续产也恢复该快照并复用已落盘产物,不会因标准中心刚刚切版而混用
  旧分镜和新声画参数;
- 只有显式 `--force`/“强制重制”才会为既有剧集绑定**当前**生效标准并全量
  重制。传入新剧本也等同于 `force`,因为旧镜头和旧声画已不可安全复用。

成品 ZIP 会归档 `制作合同/本集制作标准.json`,即使日后厂标多次升级,仍能
还原该集当时的规则、版本号和内容指纹。

### 标准中心 API

| 方法 | 路径 | 作用/主要参数 |
|---|---|---|
| `GET` | `/api/standards` | 当前生效版本、全部历史和前端能力声明 |
| `GET` | `/api/standards/export?version_id=2` | 导出指定版本;省略参数时导出当前版本 |
| `POST` | `/api/standards/save` | `content`、`change_note`、`activate`、`expected_active_id` |
| `POST` | `/api/standards/activate` | 以 `version_id` 激活历史版本 |
| `POST` | `/api/standards/reset` | 以 `change_note` 创建并激活新的厂标版本 |
| `POST` | `/api/standards/import` | `bundle`、`change_note`、`activate` |

保存示例:

```json
{
  "content": { "...": "GET /api/standards 返回的 active.content" },
  "change_note": "动作戏反应镜延长",
  "activate": true,
  "expected_active_id": 3
}
```

校验失败返回 HTTP 400 和可直接定位表单字段的
`issues: [{"path": "rules.…", "message": "…"}]`;并发版本冲突返回
HTTP 409 及实际生效版本 id。

## 模型协同与 Provider 路由

能力(script/storyboard/image/frames/video/voice/edit/cover)按
`routing` 配置的优先级链路由,前者不可用自动回退后者:

```
video: 即梦CLI(订阅额度内) → API 备用 → mock
voice: 随 Seedance2 视频完成；无声兼容模式才走豆包 TTS → API → mock
image: Codex → API 备用 → mock
```

- **即梦 CLI 优先使用订阅额度**:`quota` 表本地计数 + `dreamina
  user_credit` 实时余额(`aifos stats` 展示;配 `min_credit` 可在余额不足
  时自动降级),额度耗尽自动回退 API;
- **声音跟随实际视频 Provider**:全部镜头由声明 `audio_in_video` 的
  Seedance2 产线生成时跳过独立 TTS；无声兼容模式才使用豆包 TTS，
  专业标准禁止有声/无声视频混用，避免双声和口型错位;
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
  --duration=<单元时长,1-15秒> --video_resolution=720p \
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

覆盖:制作标准读取/校验/保存/激活/重置/导入导出、单集标准快照、暂停确认与
`force` 重新绑定、五维生产合同、11 项开拍门禁、无字幕与内置声画、交付
脚本实际执行、
端到端流水线、资产跨集复用、确定性重跑、敏感词质检失败路径、
预算熔断、Provider 降级回退、订阅额度耗尽降级、质检各检查项、
质检自动重跑修复、CLI 一句话指令解析与权限控制。

## V3 暂不开发(放入 V4)

微服务、Kubernetes、知识图谱、MCP 生态、多租户、分布式调度。
