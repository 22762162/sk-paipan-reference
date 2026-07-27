# AIFOS 提示词与参考图全链路审计

审计日期：2026-07-26

## 结论

本次错误并不主要是“模型不听话”，而是生产载荷里同时存在三类冲突：

1. 参考图没有单一职责，一张图同时被要求锁人物、服装、场景和画风；
2. 参考图筛选过宽，旧的全局图、仅有一人重叠的群像图会进入无关镜头；
3. 质检把模型漏答的身份、性别和人数检查字段当成通过。

这些问题会直接造成串脸、换性别、服装串用、人物变多、错误场景和首尾帧
修改不生效。v2.1 已正式实现并形成稳定基线，其原则是：**每张参考图只有一个用途；身份图默认
只继承身份；登记人物、功能人物和总可见人数分别计数；图片只描述一张已经
发生完毕的定格画面；视频才描述起点、单一主动作和终点；质检必须逐项显式
回答后才允许放行。** v2.2 hardening 在此基础上锁紧单人过肩、显式静态
目标、头饰/头发可见性、人物生命状态、道具物理实例和诊断严重度。

## 镜头提示词合同 v2.1 基线与 v2.2 hardening

`aifos.shot-prompt/v2.1` 的人数、图片/视频分流、空间关系、媒介和参考图
职责已经落地，不再列为待实现项。当前规范写入目标升级为
`aifos.shot-prompt/v2.2`。系统仍可读取 v2.1 和更早镜头，但任何重新编译、
自动修复、重试或付费生成前保存的合同必须输出 v2.2；需要从旧起止状态或
描述回退时，必须显式启用带名称的 legacy `frame_target_policy`，不得继续
写入新的 v2/v2.1 合同，也不得借隐式回退绕过硬门禁。

### 人数三元组

`population.counts` 是 v2.1 人数的规范来源：

| 字段 | 含义 | 计算方式 |
|---|---|---|
| `named_characters` | 有稳定身份、进入 `characters` 的登记角色数 | `len(characters)` |
| `functional_people` | 本镜只承担剧情/构图功能、不建立稳定身份的可见人形数 | 所有 `functional_figures[].count` 的精确整数和 |
| `real_people_total` | 真实世界中的总可见人形数 | `named_characters + functional_people` |
| `non_real_overlays` | 非现实 Q 版等可见叠层实例数 | 已归一化叠层实例数 |
| `visible_entity_instances_total` | 画面可见实体实例总数 | `real_people_total + non_real_overlays` |

为兼容旧调用方，`subject.count` 仍是登记角色数，并增加
`registered_count`、`functional_count`、`visible_count` 三个直观别名。
`composition.expected_visible_figure_count` 必须等于
`population.counts.real_people_total`。
`几名`、`数名`、
`若干`、范围值、空值或非正整数均不是可执行人数；解析可以保留原始稿供
人工修订，但在任何付费图片或视频生成前必须失败。尸体、背影、被遮挡但
仍可辨认的人形都计入总可见人数；非现实内心 Q 版叠层仍不计入真实人形。

林川示例：

```json
{
  "characters": ["林川"],
  "functional_figures": [
    {"label": "黑衣人", "count": 3},
    {"label": "书童尸体", "count": 1}
  ],
  "visible_figure_count": 5,
  "spatial_relations": [
    {"subject": "林川", "relation": "藏在", "object": "院门内侧门板后"},
    {"subject": "黑衣人3名", "relation": "围住", "object": "书童尸体"}
  ]
}
```

该镜头的合同结果必须是 `named_characters=1`、`functional_people=4`、
`real_people_total=5`，同时提供兼容别名 `registered_count=1`、
`functional_count=4`、`visible_count=5`。提示词必须明确“总可见人形
严格为 5”，不能只写“林川 1 人”。如果输入写总可见 6 人，或把黑衣人
写成“几名”，生成门禁必须失败。

### 静态图片与视频分流

图片提示词只渲染一个 `【定格状态】` 区段。首帧/尾帧相位只记录在结构化
合同中，提示词标签不得分叉成“首帧定格”或“终点定格”。不得同时渲染
`【起点】`、`【单一主动作】`、`【终点】` 时间线，也不能把“循声走到”
“放下包袱”“躲到”一类过程动作原样塞进图片模型。定格状态按以下优先级
取值：

1. 与 `frame_kind` 对应的 `frame_targets.keyframe` /
   `frame_targets.first_frame` / `frame_targets.last_frame`；
2. 显式 `frame_target`；
3. 显式 `freeze_state`；
4. 显式 `image_state`；
5. 只有显式启用 legacy fallback 时，才从 `start_state`、`end_state`、
   description 或 action 生成兼容目标；否则阻断。

合同同时记录
`output={media:image, frame_phase:start|end|freeze, temporal_policy:terminal_only}`、
结构化 `frame_target`、`frame_target_state` 和 `frame_target_source`，
让质检能够证明图片究竟继承了哪个显式状态；不能只凭最终提示词猜测。
视频对应 `output={media:video, frame_phase:timeline,
temporal_policy:timeline}`。

因此“林川循声走到院门，放下包袱，躲到门板后”的图片合同应描述为
“林川已经在院门内侧门板后屏息藏身，包袱已静置脚边”。图片模型只需画
这个结果，不能自行推演动作过程。

视频提示词继续保留 `【起点】→【单一主动作】→【终点】` 的时序合同，
并使用首帧和尾帧作为唯一动作边界。静态/视频使用同一个结构化事实来源，
但渲染器按媒介分流，不能把视频动作模板复用于图片。

### 空间关系

镜头输入中的 `spatial_relations` 必须原样归一化进合同，并以
`主体→关系→客体` 的可执行短句渲染。每条至少包含非空 `subject`、
`relation` 和 `object`；缺少主体或客体时尤其容易出现人物与道具错位，
必须在生成前失败。空间关系是当前镜头事实，不从旧镜头或身份参考图继承。

### 媒介一致性

合同必须把媒介判断结构化为 `medium`，而不是只依赖一串风格形容词。
同一镜头同时要求 2D 与 3D 时属于硬冲突，不能让 Provider 自行二选一。
“电影级半写实 3D 精品漫剧”必须明确为 3D 且“非真人摄影”；半写实指
材质与光影层次，不得被扩写成真人照片、摄影棚人像或 live action。

## v2.2 hardening

### 单人过肩是项目专用单主体构图

AIFOS 的 `single_subject_over_shoulder` 不等同于通用影视语义中的双人
过肩。它表示一个登记主体、一具连续身体和 `real_people_total=1`：近机位
肩背、后脑、侧脸与躯干都是同一个人，不能因为出现“肩”就自动补第二名
对话者、陌生前景人或镜中分身。

该构图必须在合同中明确 `composition_type=single_subject_over_shoulder`
及单连续身体约束。若剧情确实需要第二人，分镜必须升级为多人过肩，并将
第二人写入登记角色或带精确 `count` 的功能人物；Provider 不得自行猜测。

### 静态 frame_target 默认硬阻断

v2.2 的静态任务必须显式提供唯一 `frame_target`，并记录
`frame_target_state`、`frame_target_source` 和图片相位。缺失目标、目标
来源互相冲突，或只能从动作过程长文猜测冻结瞬间时，默认结果是 `BLOCK`。
不能再以“旧数据兼容”为由自动选一个看似合理的尾帧。

legacy 镜头已有结构化 `frame_target`、`freeze_state` 或 `image_state` 时，
编译器将该显式状态归一化为合同中的唯一 `frame_target`。只有必须从
`start_state`、`end_state`、description 或 action 回退时，镜头才需要显式
携带：

```json
{"frame_target_policy":{"name":"legacy_explicit","allow_legacy_fallback":true}}
```

使用该兼容策略的合同返回 `WARN`；未显式允许时返回 `BLOCK`。

### headwear 与 hair_visibility

人物状态必须分别记录结构化 `headwear` 和 `hair_visibility`。
`headwear.presence` 取 `none` / `worn` / `unknown`；
`headwear.kind` 取 `none` / `hair_only` / `soft_hat` /
`official_hat` / `crown` / `helmet` / `veil` / `hair_ornament` /
`other` / `unknown`，具体名称放在 `name`。`hair_visibility` 取
`fully_visible` / `partially_visible` / `covered` / `unknown`。身份图
锁定头发身份，并不意味着帽冠遮挡时仍要画出完整发髻。

无摘戴动作却跨帧换帽、声明 `covered` 却要求完整头发可见、声明无头饰却
出现帽冠，均为连续性 `BLOCK`。侧背镜中正面发际线不可见本身不是错误；
非关键发丝数量等纯视觉偏差不由结构化镜头合同 validator 判定。

### 人物 life / consciousness / embodiment / mobility

人物的生命与表演资格拆成四维：

| 字段 | 典型值 | 约束对象 |
|---|---|---|
| `life_state` | `alive` / `dead` / `nonliving` / `unknown` | 是否允许生命活动 |
| `consciousness_state` | `awake` / `asleep` / `unconscious` / `not_applicable` / `unknown` | 是否允许主动反应 |
| `embodiment` | `physical` / `statue` / `portrait` / `imagined` / `overlay` / `unknown` | 当前存在形态 |
| `mobility` | `active` / `limited` / `immobile` / `not_applicable` / `unknown` | 是否允许真实位移 |

尸体虽然计入可见真人，但应为
`dead + not_applicable + physical + immobile`，禁止呼吸、眨眼、主动视线
或自行移动。昏迷者仍是活人，但不能执行有意识反应；灵魂和内心 Q 版不能
获得真人重力、遮挡和站位。状态转变缺少可见事件或前后边界时必须 `BLOCK`。

### 道具注册、实例与转移

v2.2 将逻辑道具、当前物理实例和跨帧变化分开：

```json
{
  "prop_registry": [
    {
      "prop_id": "prop_bag_01",
      "name": "林川的包袱",
      "kind": "core",
      "instance_count": 1,
      "availability_start_event": {
        "event_id": "scene:1",
        "phase": "start"
      },
      "disclosure_policy": "explicit_frame_only"
    }
  ],
  "frame_props": [
    {
      "prop_id": "prop_bag_01",
      "phase": "start",
      "physical_state": "背在身上",
      "holder": "林川",
      "location": "none",
      "support": "林川肩背",
      "visibility": "visible",
      "representation": "physical"
    },
    {
      "prop_id": "prop_bag_01",
      "phase": "end",
      "physical_state": "完整",
      "holder": "none",
      "location": "林川脚边",
      "support": "地面",
      "visibility": "visible",
      "representation": "physical"
    }
  ],
  "prop_transitions": [
    {
      "prop_id": "prop_bag_01",
      "from_phase": "start",
      "to_phase": "end",
      "action": "放下"
    }
  ]
}
```

`prop_registry` 的每个 `prop_id` 代表一个受追踪物理实例，且
`instance_count` 固定为 1；同款多件必须拆成不同 `prop_id`。`frame_props`
记录该实例在 `start` / `end` / `freeze` 相位的状态；`visibility` 使用
`visible` / `occluded` / `hidden` / `absent`，`representation` 使用
`physical` / `reflection` / `screen` / `painting` / `overlay`。
`prop_transitions` 用 `from_phase` / `to_phase` 连接同一 `prop_id` 的相位。

剧本提及、角色知情、镜头外存在或审计披露不等于当前物理实例出现；反射、
屏幕、画中画和叠层只作披露，不建立第二个物理位置。同一 `prop_id` 在同一
相位出现两个物理主位置、转移缺少相位状态、持有人/位置与定格状态冲突时均
为 `BLOCK`。

### PASS / WARN / BLOCK severity

当前镜头合同 validator 使用聚合 `status`：

- `PASS`：合同没有阻断问题或兼容告警，可进入下一阶段；
- `WARN`：合同通过，但使用了显式允许的 legacy `frame_target` 回退；
- `BLOCK`：硬事实缺失或冲突，禁止付费生成、自动放行和下游消费。

兼容字段 `passed` 仅在 `BLOCK` 时为 false；`severity` 为对应的小写
`pass` / `warn` / `block`。具体阻断原因进入字符串数组 `issues`，legacy
兼容提示进入 `warnings`。该返回结构不承诺每条字符串问题都携带独立
`rule_id`、字段路径、证据或修复对象；视觉质检继续使用自身已有报告结构。

## 参考图 scope 与 binding

每张参考图都有唯一的用途 binding，以及可审计的
`inherit_scope.include` / `inherit_scope.exclude`。身份参考无论采用
默认 scope 还是显式 scope，都只允许：

```json
{
  "kind": "identity",
  "bindings": ["identity"],
  "inherit_scope": {
    "include": ["identity"],
    "exclude": [
      "wardrobe", "pose", "background", "props", "prop_position"
    ]
  }
}
```

这里的 `identity` 指脸型、五官比例、肤色与年龄感、发际线、发型轮廓和
发色，以及用户明确锁定的身份标志。服装、姿势、背景、普通道具及道具位置
服从本镜合同或各自专用参考图，不能从身份图偷渡。

`include` 和 `exclude` 有交集时合同自相矛盾，必须失败。同一张身份图
同时声明 `identity` 与 `wardrobe` binding 也必须失败；如果用户确实要
沿用身份图中的服装，应先建立独立服装资产或显式复制成只承担服装职责的
独立参考项，不能让一张上传图在同一请求中兼任两种职责。

迁移期可同时输出扁平 `inherits` / `excludes` 供旧参考图清单消费者读取，
但它们必须与 `inherit_scope.include` / `exclude` 完全一致；验证与新代码
以 `inherit_scope` 为规范来源，不能维护两套互相矛盾的范围。

## 从 v2/v2.1 迁移到 v2.2

迁移采用“宽读严写”：

1. 读取旧 v2 时，没有功能人物则视为 `functional_count=0`；
2. 没有总可见人数则以登记人物数作为 `visible_count`；
3. `subject.count` 继续表示登记人物数，避免破坏旧调用方；
4. 旧 identity reference 自动补齐 identity-only scope；
5. 旧静态合同若需从起止状态或描述回退，必须显式提供带名称且
   `allow_legacy_fallback:true` 的 `frame_target_policy`；否则阻断；
6. 缺少头饰/头发、人物四维状态或道具实例台账时，只能按证据确定性迁移，
   不能从人物传记或参考图服装猜测；
7. 旧视频合同保留起点—动作—终点时序，但仍需补齐 v2.2 状态与道具边界；
8. 保存、自动修复、重试和正式生成时一律输出 v2.2，并执行 v2.2 validator。

兼容只用于读取和确定性补缺，不能绕过新门禁。旧数据中一旦出现不精确
功能人数、人数和不一致、参考图职责冲突、无端点的空间关系或媒介冲突，
必须要求修订，不能因为来源是 v2 就继续付费生成。

## 实施状态

v2.1 的人数三元组、非现实叠层分计数、图片/视频渲染分流、空间关系、
媒介一致性及参考图 scope/binding 已正式实现并进入生产基线。本文不再将
这些能力描述为待实现。v2.2 hardening 的本文字段以当前合同构造器、
validator 和生成输入已经采用的结构为准；视觉 QC 继续使用自身已有报告
结构。

## 各环节问题与修复

| 环节 | 原因 | 优化后的合同 |
|---|---|---|
| 人物候选图 | 有参考图时把发型、服装、妆容全部锁死，5 张只剩动作不同；选中后续镜头又可能退回旧文字造型 | 锁脸型、骨相、年龄、性别、发际线和身份标志；明确允许 5 个方案改变发型梳法、妆容、服装、配色和气质；人工选中的发型/妆容/服装作为后续默认造型 |
| 人物母资产 | 四视图、妆容、服装图用途混在一起 | 最终立绘负责身份；四视图只补结构；妆容图只补局部；服装图只管服装和道具 |
| 场景图 | 主角立绘曾被当作全片画风锚，导致场景或其他人物继承主角脸和衣服 | 不再使用人物立绘做全局画风图；只有用户明确标注为“画风”的无关联图片可以全局使用 |
| 分镜关键帧 | 提示词过长、背景故事与动作过程混杂；人物重叠就引用旧成图 | 图片改为主体、场景、定格状态、空间关系、静态镜头、文字和硬约束分层，不再发送 START/ACTION/END；连续性旧图必须“人物集合完全一致 + 场景一致” |
| 空镜 | 场次有人时，旧逻辑会自动塞入第一名人物 | 环境空镜保持严格 0 人，并在提示词和人数质检中同时锁定 0 人 |
| 首尾帧 | `image_uri` 写进载荷但未进入参考图上传清单，模型可能根本看不到要修改的帧 | 当前关键图固定加入参考图对照表，作为构图/状态基底；人物身份仍由最终立绘锁定 |
| 局部修改 | 修改基底、身份图、场景图没有分工 | 待修改原图只负责“未提及部分保持不变”；身份、服装、场景分别按自己的参考图校正 |
| 图片质检 | 视觉模型不返回性别字段也可能被视为通过 | 身份、性别、人数都必须返回 `checked=true` 且 `match=true`；字段缺失、数量错误或性别错误均自动定向重画 |
| Seedance 视频 | 人工选择会替换人物立绘；参考图统一写成“锁身份/服装/场景/风格”；旧选择可能带入未出场角色 | 空间图和所有出场人物最终立绘不可取消；历史错角色/错场景选择自动过滤；每张视频参考图写明单一职责 |
| 视频动作提示 | 多动作、多运镜和泛化表演描述易引起漂移 | 明确首帧唯一动作起点、尾帧唯一终点、一个主动作、一次运镜、准确人数、逐人编号和空间路径 |

## 参考图选择规则

图片生产基础参考最多 8 张，为首尾帧修改基底和连续性图预留 2 个槽位：

1. 所有出场人物的最终立绘，缺一张即停止；
2. 多人走位或变机位镜头的空间调度图；
3. 角色不超过 2 人时，每人最多一张与本镜最相关的细节图；
4. 当前场景基准图；
5. 同人物集合、同场景的正式连续性图，最多一张；
6. 与当前人物或场景精确关联的用户参考图；
7. 用户明确声明的全局画风参考图。

3 人及以上群像默认不再上传四视图/妆容/服装套件，避免挤掉人物身份和
空间图，也减少角色间属性串用。超过参考上限时不截断人物立绘，而是阻止
生产并要求拆分群像。

Seedance 的顺序是：

1. 首帧；
2. 尾帧；
3. 必需的空间调度图；
4. 每位出场人物的最终立绘；
5. 本镜分镜示例图；
6. 当前场景图；
7. 精确关联的用户参考图。

人工调整只作用于第 5–7 类，不能删除空间图或人物最终立绘。

## 放行标准

正式图片必须同时满足：

- 画面实际人数等于分镜人数，空镜为 0；
- 每位人物都已逐人比对最终立绘；
- 性别/性别表达与人物设定和最终立绘一致；
- 人物身份、服装、场景、构图参考未跨用途传播；
- 可读文字只出现白名单内容；
- 首尾帧使用中或高质量，通过后才可交给 Seedance。

失败时保留失败图作为修改基底，只修正失败项，再带全部人物最终立绘复检；
不会仅根据原文字提示从零重画。
