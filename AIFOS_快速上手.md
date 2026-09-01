# AIFOS 快速上手(真实可用版)

AIFOS 是本机运行的 AI 漫剧生产平台:一句话开工,自动完成
剧本 → 人物/场景图 → 分镜 → 首尾帧 →(确认)→ 视频 → 配音 → 剪辑 → 质检 → 成品包。
零第三方依赖,只需要 macOS 自带的 `python3` 和 `git`。

## 一条命令装好并启动

```bash
curl -fsSL https://raw.githubusercontent.com/22762162/sk-paipan-reference/claude/sk-manga-drama-platform-b8nbe3/install_aifos.sh | bash
```

脚本会:装到 `~/AIFOS`(已装过则更新)→ **自动检测本机的
claude / codex / dreamina / 剪映 CLI 并接线** → 体检打印每个环节由谁生产 →
启动控制台并打开浏览器(http://127.0.0.1:8619)。

以后每次启动:双击 `~/AIFOS/start_aifos.command`,或
`cd ~/AIFOS && python3 -m aifos serve`。

## 接入真实 AI(三种方式任选)

| 环节 | CLI 方式(推荐,用订阅额度) | API 方式(备用/扩容) |
|---|---|---|
| 剧本/分镜 | Claude CLI(自动检测) | Claude API:只填 Key |
| 图片/首尾帧/封面 | Codex CLI(自动检测) | 出图 API(OpenAI 兼容):只填 Key |
| 视频 | 即梦 CLI dreamina(自动检测,锁定 seedance2.0fast_vip) | 火山方舟 Ark:只填 Key |
| 配音 | **随 Seedance2 视频自动配音(默认,台词写进视频提示词)** | 豆包 TTS:填 APPID + Key |

- **API 只需要填 Key**:接口地址和模型都内置了官方默认(高级设置里可改),
  **粘贴 Key 保存即自动启用**;点 **测试连接** 会发真实请求验证,
  Key 错了直接报 HTTP 错误。
- 配音不再使用 macOS 本地 say(效果差,已移除):默认由 Seedance2
  生成视频时自动配音(有声视频);视频产线不带配音时才走豆包 TTS。
- 命令行配置:
  ```bash
  python3 -m aifos config detect --apply          # 自动接线本机 CLI
  python3 -m aifos config set --provider claude_api --api-key sk-ant-xxx  # 填 Key 即启用
  python3 -m aifos config set --provider doubao_tts --appid 12345 --api-key tok-xxx
  python3 -m aifos config test --provider claude_api   # 真实连通验证
  python3 -m aifos doctor --ping                  # 全面体检(含 API 真实验证)
  ```

## 怎么确认「是真的在生产」

- 仪表盘顶部有 **产线状态条**:每个环节标明 `✓ 真实产线` 还是 `○ 内置模拟`;
  全模拟时会显著提示去接入。
- 每集侧栏「制作阶段」逐项标注实际使用的产线(claude / codex / jimeng /
  claude_api… 或 内置模拟)。
- `python3 -m aifos doctor`:命令行体检,未接入的环节会给出接入命令。

> 内置模拟是兜底:没接任何 AI 时流程也能完整跑通,但画面是占位图。
> 接入 Codex/出图 API 后,人物立绘、场景图、分镜画面都是真实生成。

## 一句话开工

```bash
python3 -m aifos produce "万妖图录"        # 不用《》不用集数,自动接着做下一集
python3 -m aifos produce "苏念的一天 第3期" --kind idol
```

网页里直接在首页输入框输入作品名即可。流程是两道确认:

1. **剧本确认**:剧本写完先停(此刻还没画图、不花出图额度),
   进剧集页阅读剧本;不满意附意见重写/直接编辑/上传自己的剧本,
   满意点「✅ 剧本OK,开始画图」;
2. **开拍确认**:人物/场景/分镜/首尾帧画完再停,画布里逐张检查
   (可附意见重画/上传替换),点「✅ 确认,开始生产」→ 自动完成
   视频(自带配音)/剪辑/质检 → 「⬇ 下载成品包」。

剪辑接剪映:`pip3 install pyJianYingDraft`,设置里启用「剪映草稿」,
剪辑完成后草稿自动出现在剪映首页(镜头/字幕已铺好时间轴),
打开剪映点导出即可(剪映无官方 CLI,新版不支持自动导出)。

## 常见问题

- **点测试连接报错** → 信息里带 HTTP 状态码:401 是 Key 错,404 是接口地址错,
  超时是网络不通。改完再点(会自动保存表单再测)。
- **图片还是占位图** → 看仪表盘产线状态条:图片环节若是「内置模拟」,
  说明 Codex CLI 没检测到且出图 API 没配;任接一个即可。
- **视频没有生成 mp4** → 视频环节需要即梦 CLI(`dreamina`)或 Ark API;
  即梦额度耗尽会自动回退 Ark。
- 在线演示页(claude.ai)是浏览器沙箱,**禁止外联**,真实生产请用本机版。
