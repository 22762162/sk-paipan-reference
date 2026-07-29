# 产线问题总账 · 《长夏记事》EP1 实拍

记录《长夏记事》EP1 走完 剧本 → 定妆 → 分镜 → 关键帧 → 视频 → 拼接交付 过程中暴露的
**平台/规则**问题：根因、修复位置、验证方式。不收录单张图的画面质量问题。

进本账的门槛：能让产线**卡住、空转、自相矛盾或产出不可执行合同**。

## 证据分级

本文档的每条声明按证据强度标注，未标注的默认为 A 级：

- **A｜代码可查**：能在仓库里直接读到对应代码或提交，任何人可复核。
- **B｜产物可查**：有落盘的产物或日志文件支撑。
- **C｜会话观察**：在交互式排查中亲眼见到，但未落盘，**无法事后复核**。

C 级声明保留是因为它们指向真实的排查线索，但不可作为决策依据引用。

---

## 一、导致「反复卡住」的根因（已修）

### 1. `codex exec` 继承管道 stdin 导致挂死　`A`
- **现象**：图片阶段长时间无输出，进程不退出；终端里手动跑同一条命令却正常。
- **根因**：`nohup`/`launchd` 启动时 stdin 是管道而非 tty，`codex exec` 在读到 EOF 前会
  一直等「Reading additional input from stdin...」。终端下 stdin 是 tty，所以复现不出来。
- **修复**：`aifos/adapters/codex_image.py` 的 `Popen` 加 `stdin=subprocess.DEVNULL`。
- **提交**：`2e4b2a3`
- **性质**：出图产线的单点故障——所有图片任务都过这里。

### 2. 僵尸认领：进程被杀后条目永久停在 `generating`　`A`
- **根因**：`generating` / `retrying` 状态没有生命周期收割，进程死亡不会释放认领。
- **修复**：`reconcile_completed_shot_images` 增加 `stale_reset`，把无主的
  `generating/retrying` 打回 `pending` 并记录原因；返回值新增 `stale_reset`、
  `awaiting_human_shots`。
- **提交**：`113b7ad`

### 3. 子进程不回收　`A`
- **修复**：`aifos/production/external.py` 加 `_LIVE_CHILDREN` WeakSet +
  `reap_live_children()` + `atexit`；`cli.py` 的 `serve` 增加 SIGTERM 处理。

### 4. 适配器读到写了一半的文件　`A`（代码修复）/ `C`（损失金额）
- **根因**：图片文件在写入过程中被下游读取。
- **修复**：按真实字节判定媒体类型 `sniff_image_media()`，后缀只作兜底，4 处声明点全部改用。
- **注**：当时估算的「10.20 元无效消耗」为会话内口算，无账单或日志留存，不可引用。

---

## 二、规则自相矛盾，产出「不可执行的合同」（已修）

这一类最隐蔽：产线不报错，但下游无论怎么画都过不了质检。

### 5. 景别容量门禁缺失——「大特写」画不出「人站在书案旁」　`A`
- **现象**：合同声明「大特写」，同时要求画出人物全身站位与书案环境，几何上不可能同时成立。
- **根因**：景别是盲轮换分配的，没有与「画面内需要可见的元素数量」做过校验。
- **修复**：`aifos/camera_language.py` 新增四道门禁——
  - `enforce_scale_capacity`：景别 vs 可见人数
  - `enforce_composition_scale`：框中框/引导线/水平分割/对角线 需要中景以上
  - `enforce_spatial_anchor_scale`：景别 vs 空间锚点数量
  - `enforce_position_capacity`：过肩/反打 需要 ≥2 个演员
- **提交**：`897794c`、`1298d8b`
- **效果**：`35cd36d` 的提交信息记录「景别与空间调度矛盾率 33%→4%」。
  该数字为提交作者当时的统计口径，**跨剧目**，不是 EP1 专属指标。

### 6. 「可披露窗口」把伏笔道具判成不可能　`A`
- **现象**：分镜阶段两路 writer 全部失败，报「没有可用 Provider」，实为都撞同一个校验。
- **根因**：`story_logic.py` 把「藏而未露」（银铃缝在衣襟内侧）当成了「已披露」，
  于是伏笔道具在披露前不允许出现在任何画面里——而伏笔的定义就是**在场但未被认出**。
- **修复**：区分 `is_disclosure` 语义，`freeze` 相位查 `end` 序位。
- **提交**：`c922c62`

### 7. 排除性约束被审词优化掉　`A`
- **现象**：提示词里的「严禁用实体遮蔽物遮脸」在下发时消失，模型于是拿黑纱布盖住人物头部。
- **根因**：提示词审查只保护正面 token，`严禁/绝不/不得/禁止` 引导的排除子句不在必留集合。
- **修复**：`router.py` 的 `_prompt_review_required_tokens` 把这四类子句逐字纳入必留。
- **提交**：`7a6775b`

### 8. 身份隐藏角色仍被要求出妆发图　`A`
- **修复**：`character_face_hidden` + `_sheet_suite_for` 剪掉
  turnaround/closeup/features/makeup 四张面部依赖设定图。
- **提交**：`e38945f`

### 9. 圣经生成自相矛盾（银/铜同级冲突）　`C`　**未修**
- 同级条款互斥时按治理规则只能熔断，但生成侧允许同级写入互斥描述，等于自造死锁。
- 无提交号，无落盘证据；仅作为待查线索保留。

---

## 三、质检与升级链的设计缺陷

### 10. 合同不一致被判成 `hard_failure`，堵死人工放行　`A`
- **现象**：画面本身达标（`visual_pass=True`，身份/人数/空间关系全部成立），
  仅因景别档位与合同对不上就被判 `hard_failure`，而 `hard_failure` **不允许人工放行**。
- **根因**：`hard_failure` 应当只覆盖身份、性别、人数这类硬伤。而合同本身可能是错的
  （见问题 5），**错合同不该拥有一票否决权**。
- **提交**：`5459efa`

### 11. 人工修订累积成互斥合同 → 熔断　`A`　**已修（第二次才修对）**

- **现象**：镜7 报「两组冲突均来自最高优先级的逐项人工修订，且没有显式条款裁定先后
  优先级」，此后该镜永久出不去。
- **根因**：`director.py` 两处跨次人工重画的拼接
  （图片侧约 13537 行、视频侧约 15943 行）都是
  `(auto_revision + "\n【人工补充】" + feedback)[:2400]`：
  1. 纯累加，无轮次可排序，几轮之后两条修订互斥，按治理条款 (c)「同级互斥且无显式
     裁决只能熔断」单镜永久卡死；
  2. `[:2400]` 保留的是**最旧**的自动修订、砍掉排在末尾的**最新**人工补充，
     与优先级正好相反。

- **第一次修复是无效的（记录在案）**：最初只改了质检自动重画那一处
  （`director.py` 约 5206 行）。多路复核用真实 `ProviderRouter` 实测证明该处是死路径——
  `router.py:626-628` 在提示词审核通过后**无条件**执行
  `payload["feedback"] = ""`，所以下一行拿到的历史恒为空串；
  加之 `_qc_retries()` 被硬钳为 `min(..., 1)`，轮次恒等于 1。
  叠加、丢最旧、预算上限全部是死代码，条款 (b2) 要求的「两条带轮次标记的事实共存」
  在该路径上结构性不可能出现。

- **实际修复**：
  - `rule_governance.py` 新增条款 **(b2)**：同级冲突的两条事实若都带「第N轮修订」
    标记，按轮次取最后一条执行，前轮视为已被本人替换，不构成冲突；条款 (c) 的熔断
    口子显式排除掉可排序的修订。
  - `rule_governance.stack_revision_feedback()`：每轮打
    `【第N轮修订·后条覆盖前条】`，超预算时从**最旧**一端丢起，最新一轮永不被截断。
  - `rule_governance.next_revision_round()`：轮次跨「人工重画」与「质检自动重画」
    全局单调。
  - 落到**真正会累积**的两处（13537 / 15943），轮次取
    `max(已有标记推出的下一轮, qc_failure_base + 1)`——`qc_failure_base` 是跨次
    持久化的连续失败计数，不依赖标记文本能否在审词环节存活。
- **测试**：`tests/test_aifos_revision_ordering.py`（9 项）。

### 12. 升级链改为首次失败即触发　`A`
- 阈值 `failures < 2` → `failures < 1`，并拆成两阶段：
  `executable = failures < CODEX_ESCALATION_FINAL_FAILURES`，
  只有 `targeted_redraw` 才会 `redraw_now`；其余四种动作
  （`repair_contract` / `split_shot` / `accept_current` / `manual_review`）
  给出中文说明后停手。

### 13. 帧链在首次质检失败处断裂并级联　`A`
- **缓解**：QC 阶梯复活 + `awaiting_human` 门禁持久化（跳过已判定需人工的镜头）。

---

## 四、道具尺度：「铃铛为什么这么大」的规则根因（已修）　`A`

### 14. `prop_registry` 没有任何尺度字段

EP1 的旧银铃在多张关键帧里被画成实际尺寸的 3–4 倍。追下去是一条完整的责任真空：

1. 道具参考图的 binding 写着「本镜中的尺寸、持有人、动作与状态**服从当前镜头合同**」
   （`director.py` 约 9880 行）——把尺寸甩给了合同；
2. 合同从 `prop_registry` 取道具事实；
3. 而 `prop_registry` 条目的全部字段是
   `prop_id / name / kind / instance_count / introduced_at /
   availability_start_event / retired_at / availability_end_event /
   disclosure_policy`——**没有任何一项描述尺度**；
4. 于是没有任何东西约束大小，模型退回去继承道具母图；
5. 而道具母图（`prop_顾长渊旧银铃_candidate_01.png`）是一张**占满画面的棚拍特写**，
   「这东西很大」就这样被当成道具事实继承了下来。

**实测结论**：写绝对尺寸（「直径1.5厘米」）无效，铃铛仍然过大；
改写成**画面内参照物**（「比一册线装书还薄、两指指尖即可捏住、五指合拢可藏进掌心」）
后，同一条产线画出的九态尺寸全部正确。

**修复（分三层，避免阻断存量项目）**：

第一次尝试是把 `scale_reference` 做成**阻断级**校验，全量回归打掉 4 个测试。
两个问题：(1) 它短路掉了 `validate_script` 下游既有的修复路径
（`test_hallucinated_availability_event_falls_back_to_episode_bounds` 要验的
「幻觉事件回落到 episode-start」根本没跑到）；(2) 存量剧本一律没有这个字段，
上线即全部不合格。改为分层：

1. **新剧本**：`prop_registry` schema 要求 `scale_reference`，规则明确要求用
   画面内参照物、禁止只写厘米（`adapters/claude_script.py`）；
2. **校验**：`story_logic.audit_prop_contract` 只出 `warnings`，不进 `issues`、
   不影响 `passed`；`_absolute_size_only()` 识别「只写了计量单位、没有任何
   参照物比较语」的写法并单独告警；
3. **兜底（存量项目的真正防护）**：`prompt_contract.py` 在道具
   `visibility == "visible"` 却查不到尺度时，自动注入
   【道具尺度】段——「严禁参照其母资产图在画面中的占比（母图是把道具放大
   数十倍的棚拍特写），必须按本镜的持有方式、与人手/身体/家具的真实比例
   关系推断它应有的大小，宁小勿大」。声明了尺度就不再挂这段。

- `prompt_contract.py` 把 `scale_reference` 渲染进【核心画面】的道具描述，
  让 binding 的「服从合同」终于有东西可服从；
- 道具参考图的 binding 补上明文禁令：「参考图是把该道具放大数十倍的棚拍特写，
  只用于认形制与材质，**严禁继承它在画面中的占比**」；
- 两张参考作用域表（`prompt_contract.REFERENCE_SCOPE_DEFAULTS["prop"]` 与
  `director._reference_manifest` 的 `reference_scopes["prop"]`）的 `exclude`
  补入 `frame_share`。

---

## 五、路由/配置修正　`A`

### 15. `routing/video` 把 Ark 排在即梦前面
- Ark 对写实人像的输入图会触发内容安全拒收，而本平台关键帧全部是写实人像——
  等于每个镜头都先浪费一次 Ark 往返才回落到即梦。
- **修复**：`workspace/config.json` 改为 `["jimeng", "ark", "mock"]`
  （备份 `.bak-videoroute`）。
- **注**：具体错误码在会话中观察到，但未落盘，属 `C` 级；
  **路由调整本身的收益不依赖该错误码**——Ark 与即梦对本片素材的适配差异已由
  实际生产结果确认（Ark 全部失败，即梦全部成功）。

### 16. codex_a 通道被产线占用　`A`
- `codex_a` 是用户自用通道，却被溢出调度消耗。产线池中禁用，只用 B/C 通道。

### 17. 图片阶段的 provider 边界（复核结论：平台侧本就正确）　`A`
- `routing` 中 `image/frames/cover` 均以 `codex` 打头，`jimeng` 声明
  `capabilities: ["video"]`，平台**从不**把图片任务派给即梦。
- 一次「图片跑到即梦生成」的偏差发生在临时脚本里，不是平台规则问题。

---

## 六、待决

### A. vFOV 计算　`C`　**待复核，原判断可能有误**
原记录称「100mm 镜头代码给出 35.5°，正确值 20.4°，差 1.78 倍」。
复核指出 20.4° 是**水平**视角而非垂直视角，且按代码自己声明的 9:16 画幅，
该公式可能是对的。**在重新验证前，不要按原结论改动代码。**

### B. 机位高度可能是死代码　`C`
观察到 33/33 镜头全部落在 1.55m，疑因「俯拍/仰拍」写在 `angle` 字段，
而高度函数只读 `position` + `movement`。数字为跨剧目观察，需重测确认。

### C. 提示词长度与通过率的关系　`C`
会话中观察到「718 字 → 8/8 通过；3751 字 → 0/8 通过」。**工程内无落盘支撑**，
不可作为结论引用，但值得设计一次可复现的对照实验——这条指向「是不是把简单的
事搞复杂了」这个更根本的问题。

### D. 空间图前置架构未落地
目标顺序：`剧本 → 空间图 → 分镜 → 关键帧`，分镜与关键帧都以空间图为参考。
方案在会话中给出过三步路线，但**未落盘成文件**，需要重新写。

---

## 七、回归验证　`A`

为避免与已前进的共享分支比较失真，在 `HEAD`（`a90d3bc`）建了一棵干净 worktree
做同树对照，两侧跑同一条命令，比较**失败用例集合**而非数字：

| | 失败 | 通过 |
|---|---|---|
| 基线（HEAD 干净树） | 83 | 903 |
| 本次改动后 | 83 | 918 |

失败集合逐条相同——**新增 0 条，修复 0 条，零回归**。多出的 15 项通过为新增测试。

`tests/test_cli.py`、`tests/test_four_pillars.py`、`tests/test_year_pillar.py`
三个文件在采集阶段即报 `TypeError: unsupported operand type(s) for |`
（`type | type` 联合语法需 Python 3.10+，当前环境 3.9），与 AIFOS 无关，
两侧一律排除。

本轮新增测试（共 15 项）：
- `tests/test_aifos_revision_ordering.py`（9 项）——人工修订时序裁决
- `tests/test_aifos_prop_scale_reference.py`（6 项）——道具画面内尺度与兜底禁令

后者在编写时逼出一个潜伏缺陷：`story_logic._absolute_size_only` 被引用但从未定义。
它只在 `scale_reference` 非空时才会执行，而既有测试数据里没有这个字段，
所以是一个「上线才炸」的 `NameError`。

早前几轮新增（与本轮改动无关，另计）：
- `tests/test_aifos_stuck_rootcause_fixes.py`
- `tests/test_aifos_prop_disclosure_window.py`
- `tests/test_aifos_prompt_review_page.py`
