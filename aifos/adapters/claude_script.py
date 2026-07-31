"""Claude / Codex 编剧适配桥:剧本 / 分镜由真实编剧 CLI 生成。

把 AIFOS 通用 CLI Provider 协议转换为非交互调用,要求模型输出严格
JSON,解析并校验后回传平台。默认引擎是 `claude -p`;`--engine codex`
切换为 `codex exec`(只读沙箱),供 Claude CLI 不可用时兜底编剧,
两个引擎共用同一套提示词、校验器和分镜规范化逻辑。

配置示例(workspace/config.json):
  "claude": {
    "enabled": true,
    "command": ["python3", "-m", "aifos.adapters.claude_script",
                "--claude", "claude"]
  }
  "codex_writer": {
    "enabled": true,
    "command": ["python3", "-m", "aifos.adapters.claude_script",
                "--engine", "codex", "--codex", "codex"]
  }
"""

import argparse
import json
import os
import re
import shutil
import signal
import subprocess
import sys
import tempfile
import threading
import time
from pathlib import Path

from ..generation_diagnostics import normalize_generation_diagnostics
from ..identity_facts import unresolved_identity_fields
from ..speaker_labels import is_non_person_label
from ..inner_persona import normalize_inner_persona_policy
from ..story_logic import (
    PROP_CONTRACT_SCHEMA,
    audit_prop_contract,
    audit_storyboard_prop_contract,
    normalize_prop_contract,
    normalize_script_logic,
    normalize_storyboard_frame_phase_pairs,
)
from ..story_analysis import (
    STORY_ANALYSIS_SCHEMA,
    build_story_analysis,
    validate_story_analysis,
)
from ..script_import import sanitize_script_entities

SCRIPT_PROMPT = """你是兼具顶级类型片编剧、影视导演、场面调度和连续性经验的漫剧主创。
为作品《{title}》第{episode}集创作一集完整、能实际拍摄和生成的剧本。
制作风格输入:{style};本集前提:{premise}。

要求:
- 如果输入来自小说、故事梗概或泛化叙述，先做影视化改编：保留核心因果、人物
  动机和关键台词，把“他很震惊、局势紧张、两人交锋”等抽象概括改写成摄影机
  能直接拍到的动作、反应、道具变化和空间结果；不得照抄小说旁白充当画面;
- 写完后必须以编剧、导演、场面调度和连续性四个身份进行一次内部自审并修正：
  核对触发事件→选择→行动→结果的因果链，人物是否知道足够信息、动作是否可达、
  道具从哪里来又到哪里去、人物如何进出场、站位视线是否成立、伤势/服装/持物
  是否连续、设备使用方向和重力支撑是否真实；先修正再输出最终 JSON;
- 小说、梗概和导入文本只是可改编素材，不是已经锁定的正式剧本。不得为了逐字
  忠于原素材而保留明显的因果缺口、人物无来源知情、瞬移、凭空出现的道具或
  无法实际表演的动作；在不破坏核心人物、核心事件和关键结果的前提下，编剧
  必须主动补齐实际拍摄需要但原素材省略的细节;
- 对每件关键道具建立完整生命周期：出现来源→初始状态→当前持有人→拿取/使用/
  交接动作→状态变化→本场结束去向。服装、伤势、设备、门窗、座椅和其他支撑面
  也要写清使用方式，不能留给生图或视频模型自行猜测;
- 另建 `core_props`，只收录会跨镜复用、影响身份识别、承载关键剧情或出错会污染
  后续镜头的核心道具；每件写清剧情功能、外形结构、时代材质、持有人与状态变化，
  并固定 `candidate_count:4` 供人工四选一。一次性普通小物只写进场次道具台账，
  不得为了凑资产而列入 `core_props`;
- 同时建立 `aifos.prop-contract/v2.2` 的 `prop_registry`。只登记核心、剧情敏感或
  必须跨镜追踪的物理实例，不登记普通布景陈设；每件必须有稳定且全片唯一的
  `prop_id`，同名同款的两件实物也必须使用两个不同 ID。必须写 `kind`、
  `instance_count:1`、可与分镜 `event_id` 对齐的 `introduced_at` /
  `availability_start_event`、可选 `retired_at` / `availability_end_event`，
  以及明确 `disclosure_policy`。事件引用必须使用稳定 event_id，禁止只写会因
  重排而变化的 shot_no;
- 每件 `prop_registry` 条目必须写 `scale_reference`：用**画面内可见的参照物**
  描述它有多大，禁止只写厘米/毫米等绝对尺寸。绝对尺寸对出图模型无效，只有
  参照物有效——写「比一册线装书还薄、两指指尖即可捏住、五指合拢可藏进掌心」，
  不要写「直径1.5厘米」。参照物必须选本片场景里真实存在的物件或人体部位。
  没有这一条，出图时道具会继承母图(占满画面的棚拍特写)的画面占比而被画成
  数倍大;
- 每场必须明确人物信息状态：本场开始时各自知道什么、不知道什么、通过何种
  可见证据新获知什么；禁止角色突然知道未听见、未看见、未被告知的内容;
- 每场必须明确时间关系和动作耗时。上一场出口、本场入口、本场出口和下一场入口
  必须可连续继承，不允许人物、情绪、伤势、服装或道具无过渡跳变;
- 若后续发现确属剧本逻辑根因，只允许局部返编问题场：锁定前一场出口和后一场
  入口，保留人物身份、世界规则、既有伏笔和后续必达结果；若修改范围必须扩大，
  先列出受影响场次与资产，不得静默重写整集;
- 每场必须建立 `director_logic`：戏剧功能、入场状态、可见物理动作链、道具连续性、
  人物信息状态、空间逻辑、时间连续性、缺失细节补全、离场状态和导演意图，并建立
  `continuity_contract` 局部返编边界。禁止“推进剧情、发生冲突、自然表演”等占位语;
- 若制作风格输入为“未指定”,必须先从题材、时代、地域、人物阶层、物质文化、
  情绪和目标观众推导本剧专属视觉基准；不得默认套用现代乙女、国风、古装、
  2D、3D或半写实模板;
- 3 到 5 场戏,每场 2-4 句台词,适合 1-2 分钟短剧;
- 台词口语化,单句不超过 25 个字;
- 写场次前必须先建立 `story_world` 世界设定、`story_background` 故事前情和
  `characters` 人物设定介绍;三者是后续人物图、分镜、关键帧和视频的唯一剧情依据;
- 世界设定必须具体写清时代地域、社会/组织规则、能力或技术边界与视觉基准,
  禁止使用“按剧情决定”“自由发挥”等空泛占位;
- 故事前情必须说明本集开始前发生了什么、当前局势、核心冲突、本集目标及前后集衔接;
- 主角、重要配角和非重要配角都必须先写完整人物背景提示词,不能只写姓名和标签。
  背景要和本集剧情、时代/世界观、职业、人物性格、关系、目标与冲突绑定;
- 主角、重要配角和非重要配角必须有独立的 `introduction`,明确性别、年龄段、
  身份、性格、目标、人物关系和不可改变的身份事实;
- 跑龙套、群演、背景路人不得建立独立人物设定。只在 characters 中保留
  `name`、`role: "背景路人"`、`crowd_function` 和出现场次功能,不写外貌、性别、
  年龄、妆容、服装、经历或人物背景提示词,也不生成候选图/立绘/四视图;
- 旁白、画外音、独白、心声、解说、音效、声效、配乐/BGM、环境音、字幕、
  花字、提示音、广播这类叙事装置**不是人物**,一律不得写进 characters,
  也不得出现在任何场次的 `characters` 名单里。它们只是声音或文字来源,
  没有身体、不出现在画面中;台词内容照常保留在对应场次的叙述或对白里;
- 人物重要度必须明确标为主角、重要配角、非重要配角或背景路人。
  所有正式角色后续统一生成 4 张候选图;场次中不得出现人物表未声明的新角色;
- `costume_direction` 必须给出可画的服装逻辑(款式、材质、层次、颜色、职业制服/时代服饰和剧情场合),禁止所有角色默认现代都市便服;
- `visual_variants` 至少给出 3 个按剧情时间顺序排列的造型方案；
  第 1 项必须是人物首次登场前的基础定妆，只保留初始服装、发型、妆容与稳定身份特征，
  明确排除淋湿、泥污、伤情、死亡态和后续换装；后续项才写行动/冲突/仪式等剧情状态。
  `visual_variants` 供后续镜头连续性使用，不用于把4张人物定角候选做成4种状态；
  定角4张图必须复用第1项编译出的同一提示词，只靠图片模型随机采样;
- 每个正式角色必须先完成 `character_analysis`，再生成 `visual_dna`：
  从身份阶层、成长环境、当前处境、欲望、恐惧、关键经历、性格优缺点和
  行为习惯，推导脸部骨相、发型轮廓、身体/职业痕迹、服装结构与磨损、
  核心配饰、带故事来源的视觉符号及 3-8 个气质关键词；
- `cast_dedup` 必须把角色与全剧其他正式角色逐一比较；发型、服装结构、
  身体特征、视觉符号、核心配饰、气质关键词中有两项以上重叠就先重设计；
  禁止 AI 网红脸、模板帅哥美女、韩式偶像脸和只写“帅、美、冷峻、高级”;
- 只输出一个 JSON 对象,不要任何其他文字、解释或 Markdown 代码块。

JSON 格式(字段必须齐全):
{{"project_title": "{title}", "episode_number": {episode},
 "episode_title": "本集小标题", "logline": "一句话梗概",
 "story_world": {{"name": "故事世界名称", "overview": "世界概述",
   "era_and_location": "时代与主要地域", "social_order": "社会、组织与阵营秩序",
   "hard_rules": "能力、技术、身份与事件运行边界",
   "visual_baseline": "建筑、服装、道具、色彩与光影基准",
   "forbidden_drift": ["禁止乱入的时代/技术/物种/组织或身份"],
   "sanctioned_anachronisms": ["剧情明确允许跨时代出现的物品(如穿越者随身的手机);非穿越/无此类设定则给空数组"]}},
 "story_background": {{"prior_events": "本集之前的关键事件",
   "current_situation": "本集开场时局势", "core_conflict": "核心冲突",
   "episode_goal": "本集要完成或改变什么",
   "continuity_hooks": "承接前集并留给后集的连续性钩子"}},
 "characters": [{{"name": "角色名", "role": "主角/重要配角/非重要配角",
   "introduction": "人物设定介绍:性别、年龄段、身份、性格、目标、关系和身份硬事实",
   "gender": "明确性别", "age_range": "明确年龄段", "identity": "身份与阵营",
   "personality": "性格及外化方式",
   "background_prompt": "可直接用于出图的详细人物背景提示词:成长经历、当前处境、核心动机、心理矛盾和视觉象征",
   "era_setting": "时代/世界观/地域", "occupation": "职业/社会身份",
   "motivation": "核心目标与本集欲望", "backstory": "关键经历、秘密或创伤",
   "relationships": "与其他角色的关系及冲突", "costume_direction": "服装如何体现身份、性格和剧情阶段",
   "signature_props": "标志性道具或动作", "visual_variants": "日常/冲突/关键场合等剧情造型方案（不改变全剧画风）",
   "character_analysis": {{"identity_and_class":"身份与阶层","age_and_presentation":"年龄感",
     "upbringing":"成长环境","family_background":"家庭背景","education_background":"教育背景",
     "current_situation":"当前处境","core_desire":"核心欲望","greatest_fear":"最大恐惧",
     "formative_experiences":["关键经历"],"strengths":["性格优点"],
     "flaws":["性格缺陷"],"behavior_habits":["行为习惯"]}},
   "visual_dna": {{"face_structure":"脸型骨相","hair_silhouette":"发型轮廓",
     "body_or_occupation_marks":"身体或职业痕迹","clothing_structure":"服装结构",
     "clothing_wear_state":"服装状态","story_visual_symbol":"视觉符号",
     "story_visual_symbol_origin":"符号的故事来源","signature_accessory":"核心配饰",
     "temperament_keywords":["3-8个气质关键词"],"genre_system_mapping":{{}}}},
   "cast_dedup": {{"compared_with":["其他角色"],"dimensions":["发型","服装结构","身体特征","视觉符号","核心配饰","气质关键词"],"status":"passed","conflicts":[]}}}},
  {{"name": "路人功能名", "role": "背景路人",
   "crowd_function": "在哪一场以几人、什么剧情功能短暂出现;无独立人物资产"}}],
 "prop_contract_schema":"aifos.prop-contract/v2.2",
 "prop_registry":[{{"prop_id":"稳定且唯一的道具实例ID","name":"全剧唯一实例显示名称",
   "kind":"core/plot_sensitive/identity_prop","instance_count":1,
   "scale_reference":"用画面内参照物说明它多大(如:比一册线装书还薄、两指可捏住、掌心可藏住);禁止只写厘米",
   "introduced_at":{{"event_id":"稳定剧情事件ID","phase":"start"}},
   "availability_start_event":{{"event_id":"同一稳定剧情事件ID","phase":"start"}},
   "retired_at":null,"availability_end_event":null,
   "disclosure_policy":"explicit_frame_only/conceal_until_introduced/available_after_introduction/never_visualize"}}],
 "core_props": [{{"prop_id":"与prop_registry一致的稳定ID","name":"核心道具唯一名称",
   "story_function":"为什么会影响多镜头或核心剧情",
   "visual_design":"可直接画出的外形、结构、尺寸级别与识别点",
   "era_material":"符合世界观的材质、工艺和磨损",
   "owner":"初始与主要持有人",
   "continuity_states":"出现、使用/交接、状态变化和最终去向",
   "candidate_count":4}}],
 "adaptation_review": {{
   "source_to_screen_strategy": "如何把小说/梗概改成可见戏剧行动",
   "source_material_policy": "原素材哪些必须保留、哪些允许为影视逻辑改写；正式锁定点是什么",
   "causal_chain": "逐场触发→选择→行动→结果→钩子检查",
   "character_motivation": "关键行动与人物目标、信息和风险的对应",
   "information_continuity": "逐场人物已知/未知/新获知信息及其证据来源",
   "physical_reality": "动作可达、重力支撑、接触点、设备方向与道具来源去向检查",
   "spatial_continuity": "入口出口、站位、视线、路线和场景布局检查",
   "temporal_continuity": "时间顺序、动作耗时、场间过渡及状态继承检查",
   "prop_lifecycle": "关键道具逐件来源、持有人、使用交接、状态变化和去向",
   "missing_detail_completion": "原素材省略但拍摄必需的服化道、动作、空间和环境细节如何补齐",
   "story_density": "逐场新信息、选择、冲突、反应或结果检查；删除无效注水",
   "shootability": "核心事件如何由镜头直接拍出",
   "local_rewrite_policy": "逻辑返编时如何锁定前后边界、分析影响并只使受影响资产失效",
   "self_reviewed": true}},
 "scenes": [{{"scene_no": 1, "event_id":"稳定场次事件ID","location": "地点",
   "characters": ["出场角色名"], "action": "本场动作描述",
   "director_logic": {{
     "dramatic_function": "本场给观众带来的新信息/冲突/转折",
     "entry_state": "每名重要人物入场位置、姿态、情绪、伤势和持物",
     "information_state": "逐人写本场开始已知、未知及本场通过何种证据新获知的信息",
     "physical_actions": "按先后顺序写可见且可达的单一动作链",
     "prop_continuity": "关键道具来源、持有人、接触方式和离场去向；无则写无",
     "spatial_logic": "入口、相对站位、视线对象、动作路径、支撑面与出口",
     "time_continuity": "与上一场的时间关系、本场动作耗时及通向下一场的时间过渡",
     "missing_details_completed": "补齐原素材未写但实际拍摄必需的道具、服装、支撑、交接和环境反应",
     "exit_state": "每名重要人物离场位置、姿态、情绪、伤势和持物",
     "director_intent": "不用旁白，如何用表演、调度和画面结果拍出本场",
     "continuity_contract": {{
       "entry_boundary": "局部返编不可改变的本场入口事实",
       "exit_boundary": "局部返编必须交付给下一场的出口事实",
       "immutable_facts": "不可改变的人物身份、世界规则、既有事件、伏笔和后续必达结果",
       "prop_ledger": "本场关键道具进入、持有、交接、状态变化和离场台账",
       "knowledge_state": "本场各人物信息状态边界",
       "time_state": "本场前后时间状态边界",
       "local_rewrite_scope": "默认只改本场；扩大范围时必须先列出受影响场次与资产"}}}},
   "lines": [{{"character": "角色名", "dialogue": "台词"}}]}}]}}"""

IDOL_PROMPT = """你是 AI 虚拟偶像「{persona}」的内容策划。为第{episode}期短视频写口播脚本。
人设风格:{style};本期主题:{premise}。

要求:
- 竖屏短视频节奏:开场 3 秒钩子 → 主体内容 → 结尾引导关注;
- 写场次前先完整输出 `story_world`、`story_background` 和人物设定介绍,
  作为后续人物图、舞台分镜和视频的唯一事实源;
- 若主题涉及女团/男团/组合,设 2-4 名成员(队长/主唱/舞担等),
  characters 列出全部成员,台词在成员间分配;否则全部台词由
  「{persona}」一人口播;
- 每个正式成员都要写与团内定位、歌曲主题、成长经历和本期冲突绑定的人物背景提示词、
  职业/舞台身份、性格外化方式、服装逻辑和至少 3 套练习室/后台/舞台剧情造型方案；
  第1套必须是无淋湿、泥污、伤情或后续换装的首次登场基础定妆；
  其余方案只供后续镜头连续性使用。人物定角4张图复用第1套同一提示词，
  只靠图片模型随机采样比较人物形象;
- 每个正式成员先写人物分析和视觉 DNA，再与全团其他成员做视觉去重；
  发型、服装结构、身体特征、视觉符号、核心配饰、气质关键词中两项以上
  重叠必须重设计，不能只靠换衣服颜色区分成员;
- 每个成员必须有 `introduction`,明确性别、年龄段、团内身份、性格、目标、
  成员关系与不可改变的身份事实;场次不得新增人物表之外的成员;
- 临时观众、工作人员、群演等背景路人只写 `name`、`role: "背景路人"` 和
  `crowd_function`,不建立独立人物设定或人物资产;所有正式角色统一生成 4 张候选图;
- 台词口语化、有网感,单句不超过 22 个字;
- 只输出一个 JSON 对象,不要任何其他文字、解释或 Markdown 代码块。

JSON 格式(字段必须齐全,characters 只含「{persona}」一人):
{{"project_title": "{persona}", "episode_number": {episode},
 "episode_title": "本期小标题", "logline": "一句话内容概要",
 "story_world": {{"name": "偶像内容世界名称", "overview": "世界概述",
   "era_and_location": "当代及主要活动空间", "social_order": "团队/平台/粉丝规则",
   "hard_rules": "成员身份、舞台能力和活动边界",
   "visual_baseline": "练习室/后台/舞台的服装道具与光影基准",
   "forbidden_drift": ["禁止成员身份、人数、时代与舞台规则漂移"]}},
 "story_background": {{"prior_events": "本期之前的关键经历",
   "current_situation": "本期开场局势", "core_conflict": "本期核心矛盾",
   "episode_goal": "本期目标", "continuity_hooks": "与前后期的衔接"}},
 "characters": [{{"name": "{persona}", "role": "主角",
   "introduction": "人物设定介绍", "gender": "明确性别",
   "age_range": "明确年龄段", "identity": "职业/团内身份",
   "personality": "性格及舞台外化方式",
   "background_prompt": "与主题和成员定位绑定的人物背景提示词",
   "era_setting": "时代/舞台世界观", "occupation": "职业/团内身份",
   "motivation": "本期目标", "backstory": "成长经历",
   "relationships": "团内关系", "costume_direction": "练习室/后台/舞台服装逻辑",
  "signature_props": "标志道具", "visual_variants": "至少三套剧情造型方案（不改变全剧画风）",
   "character_analysis": {{"identity_and_class":"身份与团内位置","current_situation":"当前处境",
     "core_desire":"核心欲望","greatest_fear":"最大恐惧","formative_experiences":["关键经历"],
     "strengths":["优点"],"flaws":["缺点"],"behavior_habits":["行为习惯"]}},
   "visual_dna": {{"face_structure":"脸部骨相","hair_silhouette":"发型轮廓",
     "body_or_occupation_marks":"舞台/训练痕迹","clothing_structure":"服装结构",
     "story_visual_symbol":"视觉符号","story_visual_symbol_origin":"故事来源",
     "signature_accessory":"核心配饰","temperament_keywords":["3-8个气质关键词"]}},
   "cast_dedup": {{"compared_with":["其他成员"],"status":"passed","conflicts":[]}}}}],
 "scenes": [{{"scene_no": 1, "location": "场景",
   "characters": ["{persona}"], "action": "画面动作描述",
   "lines": [{{"character": "{persona}", "dialogue": "口播台词"}}]}}]}}"""

STORYBOARD_PROMPT = """你是漫剧分镜师。基于以下剧本 JSON 生成可交给工业流继续校验的原始分镜表。

剧本:
{script}

要求:
- `story_world` 故事世界观、`story_background`、非背景角色的
  `introduction` 与背景路人的
  `crowd_function` 是硬约束,必须先读取再分镜;不得新增人物表之外的角色,
  不得擅自改时代、组织、能力、技术、物种、性别、年龄段、身份与人物关系;
- 必须读取 `production_analysis` 制作圣经；逐场继承其中的环境、布局、材质、
  时段天气、主光方向和提示词前缀；继承全局画风、负面提示词与连续性规则;
- 若 `production_analysis.visual.director_knowledge` 中存在
  `firefire.director-style/v1`，它就是本风格唯一的高频镜头与视觉效果库：
  每镜必须依据叙事功能从 `shot_patterns` 选择一个主要镜头组合，并从
  `visual_effects` 只选本镜必要的效果；不得套用其他风格的惯用运镜，不得把
  整套粒子、滤镜和光效同时堆入一镜。`camera` 必须明确景别、角度、机位、
  焦段、一个主要运镜和构图；同时输出 `style_direction` 记录选择结果与动机;
- 每段只承载一个主要动作或一次情绪转折；台词逐字照抄，禁止改写；
- 每场先给 1 个环境/肢体镜头，再为每句台词给 1 个对白镜头；
- 关键台词后的听者反应与情绪高潮留白由平台补齐，不要用空镜凑时长；
- `characters` 只列有独立身份资产的登记角色。所有没有独立身份资产但在画面中
  可见的真人/人形（群众、路人、尸体、临时杀手、侍从等）必须逐类写入
  `functional_figures`；每项使用 `name` 或 `label` 标明功能，`count` 必须是
  精确正整数，可选写 `state` / `function`。禁止用“几名”“数名”“一群”
  “多人”“若干”等模糊数量，禁止把这些功能人物混入 `characters`；
- 平台按“登记角色数 + functional_figures 各项 count 之和”计算
  `visible_figure_count`（画面可见真人总数）；`narrative_overlays` 是非现实
  叙事叠层，不计入可见真人总数；
- 若剧本 `inner_persona_policy.enabled=true`：穿越前该人物可作为真实人物；
  穿越后其真人形态必须从 `characters` 删除。只有必要的内心独白、内心吐槽、
  喜剧反应或决策冲突，才可输出一个 `narrative_overlays` Q版叠层；不得每句
  台词后自动插入。Q版不计真实人数、不参与站位、不被他人感知，继承当前锁定
  衣服但无默认道具，表情动作可以夸张；比例固定为大头小身，约1.8头身，
  头占总高约58%，身体与四肢明显小于头；内心发声时真人宿主闭口且不生成字幕；
- shot_no 从 1 连续编号；duration 单位秒，优先 5-8 秒，最长 15 秒；
- 每镜必须输出 `frame_targets`，分别锁定 keyframe、first_frame、last_frame
  三种静态用途；每项只含一个 phase(start/end/freeze) 和一个可见、可拍、
  无时间过程的 state，并明确 fallback:false。人物镜可引用已经写清的起止状态，
  空镜也必须写场景、光线、陈设和环境变化完成后的唯一静态结果；禁止把“走过去、
  拿起来、从躺到坐”等动作过程直接当静态定格；
- prompt 只含本镜头真正可见且会影响生成的世界状态、场景、准确人物名单、主体动作、
  光影、机位与结尾状态；不得复制整集前情、其他镜头、人物传记或无关道具；
- 保留剧本 `prop_registry` 的 prop_id、名称、实例身份和场次生命周期边界；
  可把 availability_start/end 从源场次边界细化到同一场内的准确镜头 event_id，
  不得跨场移动、改名、删项或复制。每镜输出稳定且唯一的 `event_id`。仅对本镜
  实际相关的已登记剧情道具输出结构化 `frame_props`，不得
  通过全文搜索道具名猜测状态，也不得把未登记的普通布景陈设强行纳入该合同；
  每项必须写 prop_id、phase(start/end/freeze)、physical_state、holder、location、
  support、visibility(visible/occluded/hidden/absent) 和
  representation(physical/reflection/screen/painting/overlay)。反射、屏幕、画中画
  和叠层都算剧情披露，但不算第二件物理实例；hidden/absent 不得偷画成背景露角;
- 同一道具在 start/end 的持有人、位置、支撑、可见性、呈现方式或物理状态发生
  变化时，必须输出 `prop_transitions`，写明 prop_id、from_phase、to_phase 和
  唯一可见 action。静态 freeze 不写跨时间动作；同一 phase 的 physical 实体数
  不得重复；多件同款物品必须分别登记不同 prop_id;
- 每个镜头必须额外输出 `physical_logic`，只写本镜可见的物理/空间事实：人物、
  道具、桌面/地面、镜头的相对位置，使用方向、接触点、视线和动作可达性；
  涉及电脑/手机/屏幕时必须明确屏幕正面、键盘/手部、使用者和摄影机同侧关系，
  禁止人物坐在屏幕后方却看到屏幕正面等反向构图；
- 每个有角色的镜头必须输出 `start_state` 和 `end_state`，按角色名分别写
  pose、position、direction、prop、injury、emotion、wardrobe、结构化
  headwear(presence:none/worn、kind、name)、hair_visibility 和 hair_makeup；
  同时写 condition 的 life_state(alive/dead/nonliving)、
  consciousness_state(awake/asleep/unconscious/not_applicable)、
  embodiment(physical/statue/portrait/imagined/overlay)、
  mobility(active/limited/immobile/not_applicable)。服装、头饰、妆发和人物状态
  必须写当前镜头唯一可见状态，不能把多套剧情
  造型同时塞入同一镜。姿态必须说明站/坐/跪/躺/伏案及真实支撑面，终态必须
  被下一镜逐项继承；没有明确换装/摘戴/改妆动作时，下一镜不得改动 wardrobe、
  headwear、hair_visibility 或 hair_makeup；没有醒来、入睡、昏迷、死亡等
  明确过程时不得改变 condition。睡眠/昏迷/死亡/非生命形态不得出现与状态
  冲突的注视、眨眼、说话、主动动作或微表情。空镜输出空对象；
- prompt 中人物形态按人物设定描写(名字只是称呼,「小鹿」若设定为
  人类不能当动物写;设定为动物/精怪的按设定写,全片保持一致);
- 不生成对白字幕。手机屏、弹幕、合同等可读文字只描述载体与准确文字，
  后续由 ChatGPT 关键帧锁定，不能交给视频模型从零生成；如需可读文字，必须
  额外输出 `readable_text` 文字资产卡，只填写画面确实要出现的文字，不得把
  镜头合同、提示词标题、质检原因或系统字段当作屏幕内容；同时给出载体、版式、
  字体/颜色样式、屏幕透视和可读优先级；没有可读文字时输出 null；
- 只输出一个 JSON 对象,不要任何其他文字或 Markdown 代码块。

JSON 格式:
{{"episode_title": "...","prop_contract_schema":"aifos.prop-contract/v2.2",
 "prop_registry":[{{"prop_id":"稳定道具实例ID","name":"全剧唯一实例显示名称",
   "scale_reference":"用画面内参照物说明它多大;禁止只写厘米",
   "kind":"core/plot_sensitive/identity_prop","instance_count":1,
   "introduced_at":{{"event_id":"同一源场次内首次可披露的准确镜头event_id","phase":"start"}},
   "availability_start_event":{{"event_id":"同一准确镜头event_id","phase":"start"}},
   "retired_at":null,"availability_end_event":null,
   "disclosure_policy":"explicit_frame_only"}}],
 "shots": [{{"shot_no": 1, "scene_no": 1,
  "scene_event_id":"源剧本对应场次的稳定event_id",
  "event_id":"稳定且唯一的镜头事件ID",
  "kind": "environment", "description": "...", "camera": "镜头语言",
  "style_direction":{{"schema":"firefire.director-style/v1",
    "shot_pattern":"从本风格高频镜头组合中选中的一项",
    "visual_effects":["只列本镜实际使用的必要视觉效果"],
    "selection_reason":"说明该镜头与效果如何服务剧情功能"}},
  "duration": 2.5, "characters": ["角色名"], "dialogue": null,
  "functional_figures": [{{"name":"无独立身份资产的功能人物",
    "count":2,"state":"当前可见状态","function":"本镜剧情功能"}}],
  "narrative_overlays": [{{"kind":"inner_persona_chibi",
    "function":"inner_monologue/inner_commentary/comic_reaction/decision_conflict",
    "name":"内心人格角色名","host_character":"宿主角色名",
    "expression":"夸张Q版表情","action":"夸张Q版动作",
    "dialogue":"必要时的内心台词；否则空字符串"}}],
  "physical_logic": "本镜物理/空间关系",
  "frame_props":[
    {{"prop_id":"稳定道具实例ID","phase":"start",
      "physical_state":"起点物理状态","holder":"持有人或none",
      "location":"起点唯一主位置或none","support":"支撑/接触面或none",
      "visibility":"visible/occluded/hidden/absent","representation":"physical"}},
    {{"prop_id":"同一道具实例ID","phase":"end",
      "physical_state":"终点物理状态","holder":"持有人或none",
      "location":"终点唯一主位置或none","support":"支撑/接触面或none",
      "visibility":"visible/occluded/hidden/absent","representation":"physical"}}],
  "prop_transitions":[{{"prop_id":"稳定道具实例ID","from_phase":"start",
    "to_phase":"end","action":"单一可见拿取/交接/移动/状态变化"}}],
  "frame_targets":{{
    "keyframe":{{"phase":"end","state":"本镜代表性唯一静态结果","fallback":false}},
    "first_frame":{{"phase":"start","state":"动作发生前唯一可见状态","fallback":false}},
    "last_frame":{{"phase":"end","state":"动作完成后唯一可见状态","fallback":false}}}},
  "start_state": {{"角色名":{{"pose":"动作起点姿态及支撑面",
    "position":"相对场景和其他人物的位置","direction":"身体朝向和视线对象",
    "prop":"手中/身上道具","injury":"伤势","emotion":"可见情绪",
    "wardrobe":"当前唯一服装",
    "headwear":{{"presence":"none/worn","kind":"official_hat/crown/helmet/soft_hat/veil/hair_ornament/other/none","name":"具体名称或none"}},
    "hair_visibility":"fully_visible/partially_visible/covered",
    "condition":{{"life_state":"alive/dead/nonliving","consciousness_state":"awake/asleep/unconscious/not_applicable","embodiment":"physical/statue/portrait/imagined/overlay","mobility":"active/limited/immobile/not_applicable"}},
    "hair_makeup":"当前发型与妆容"}}}},
  "end_state": {{"角色名":{{"pose":"动作完成后的姿态及支撑面",
    "position":"终点位置","direction":"终点朝向和视线对象",
    "prop":"终点持物","injury":"终点伤势","emotion":"终点情绪",
    "wardrobe":"结尾唯一服装",
    "headwear":{{"presence":"none/worn","kind":"official_hat/crown/helmet/soft_hat/veil/hair_ornament/other/none","name":"具体名称或none"}},
    "hair_visibility":"fully_visible/partially_visible/covered",
    "condition":{{"life_state":"alive/dead/nonliving","consciousness_state":"awake/asleep/unconscious/not_applicable","embodiment":"physical/statue/portrait/imagined/overlay","mobility":"active/limited/immobile/not_applicable"}},
    "hair_makeup":"结尾发型与妆容"}}}},
  "readable_text": {{"carrier":"电脑屏幕", "whitelist":["逐字原文"],
    "layout":"版式/位置", "style":"字体/颜色/层级", "perspective":"透视/反光",
    "priority":"must_read"}},
  "prompt": "文生图提示词"}}]}}"""

STORY_ANALYSIS_PROMPT = """你是 AI 漫剧的编剧、美术指导、摄影指导和连续性导演。
请完整分析下面的剧本，建立一份在出人物图、场景图、五维分镜、关键帧和
Seedance 视频前必须锁定的“制作圣经”。

制作风格输入（明确填写时为覆盖项；未指定时必须依据剧本自动设计）:
{style}
用户补充方向:
{direction}
当前制作标准（必须遵守）:
{analysis_rules}
剧本 JSON:
{script}

分析规则:
- 先从全文提取时代、地域、社会秩序、人物身份与阶层、环境材料、服化道、
  题材、情绪弧和目标观众，再建立适合本剧的制作风格;
- 区分“故事时代/世界”与“成片渲染媒介/画风”。用户明确填写风格时保留其
  媒介诉求，但服装、建筑、器物和社会规则仍须符合故事世界;
- 用户未指定时，不得默认套用现代乙女、国风、古装、2D、3D、半写实或
  任何固定模板；历史题材要先核定朝代与地域，不得使用通用影楼古装或朝代混搭;
- 先重新核对人物实体。剧本人物表可能由小说规则解析而来，凡“冷声、温声、
  哑着嗓子、试探着、小心翼翼地、躬身回话”等语气、动作、状态短语，绝不是
  人物名；必须结合全文称谓、上下句、代词、关系和行动归并到真实人物，并在
  speaker_resolution 中逐项说明。无法可靠归并时保留待确认，禁止编造新人;
- 必须再逐场逐句判断说话人，在 `line_speakers` 中为每句对白输出一条记录；
  `line_index` 是该场 lines 数组从 1 开始的序号，`dialogue` 必须逐字复制原文。
  不得因为多句对白共享同一个旧标签就把它们归给同一人；要根据称谓（殿下/
  奴婢/本宫）、问答关系、上下句和动作分别判断。无法可靠判断时
  canonical_name 写“待确认说话人”，不得猜;
- 除纠正上述错误标签外，不得新增人物或擅自改变真实人物的姓名、性别、年龄、
  身份、阵营、物种与人物关系;
- 逐场分析空间功能、入口出口、前中后景、材质道具、时段天气、主光方向、
  环境声和连续性锚点;
- 候选图数量固定:所有正式角色4张、背景路人0张;
- 每名正式角色按“剧情证据→经历与处境→性格与行为→可见特征→视觉 DNA”
  分析；视觉 DNA 包含脸部骨相、发型轮廓、身体/职业痕迹、服装结构与状态、
  核心配饰、有故事来源的视觉符号及3-8个气质关键词;
- 每名正式角色必须根据全文明确或保守推断 gender、age_range，并生成一段
  `image_prompt`。它是交给图片模型的最终人物出图卡，必须自洽且可直接出图：
  只写单张图能够直接画出的事实，控制在约 300-650 个中文字符；包含可见身份、
  性别年龄呈现、脸部骨相、肤色、体态、发型、本张唯一一套服装及必要身份配饰、
  气质、时代约束、画面媒介、构图与纯净背景要求。人物身世、父母关系、知识储备、
  内心计划、倒计时、说服策略、道具来源、审计过程和备选服装只留在结构化人物分析，
  禁止塞入 `image_prompt`;
- 对全剧正式角色做视觉去重；发型、服装结构、身体特征、视觉符号、核心配饰、
  气质关键词中两项以上重叠时，先重设计再标记 passed;
- 人工定版后必须生成面部近景、正面、严格90度侧面、完整180度背面四张独立
  高清母资产；16:9三视图拼图只用于审核，不能代替独立参考图;
- 输出全局图片、人物、场景、关键帧、Seedance前缀及负面提示词;
- 台词逐字保护;每段最长15秒;关键台词后有反应镜;高潮有2-4秒留白镜;
  重要肢体动作独立成镜;不要字幕;可读文字先在关键帧锁定;
- 只输出一个 JSON 对象，不要解释或 Markdown。

JSON 结构:
{{"schema":"{schema}","source":"ai","locked":false,
"speaker_resolution":[{{"raw_label":"误识别标签",
"canonical_name":"真实人物名","classification":"performance_cue/action_cue/alias/misparsed_label",
"performance":"应保留的语气或动作","confidence":0.98,
"evidence":"用于判断的原文称谓、上下句或人物关系"}}],
"line_speakers":[{{"scene_no":1,"line_index":1,
"dialogue":"逐字复制该句对白","raw_label":"该句当前人物标签",
"canonical_name":"该句真实说话人","performance":"该句语气或动作",
"confidence":0.98,"evidence":"称谓、问答关系、上下句或动作证据"}}],
"narrative":{{"logline":"","genre":"","themes":[],"tone":"",
"target_audience":"","emotional_arc":"","core_conflict":"",
"continuity_hooks":""}},
"world":{{"name":"","overview":"","era_and_location":"",
"geography_and_climate":"","social_order":"","culture_and_lifestyle":"",
"technology_and_props":"","hard_rules":"","recurring_motifs":[],
"forbidden_drift":[]}},
"visual":{{"user_style_constraint":"明确风格则保留；未指定则写入根据剧本生成的本剧制作风格",
"style_source":"user_override或ai_inferred","analysis_basis":["时代/地域/题材等文本证据"],"medium":"",
"realism":"","palette":[],"lighting":"","camera_language":"",
"visual_effect_language":"按剧情功能选择的光效、氛围、材质与后期语言",
"texture_and_render":"","architecture_and_environment":"",
"wardrobe_and_styling":"","props_and_graphics":"","forbidden_visuals":[]}},
"scenes":[{{"scene_no":1,"location":"","story_function":"",
"environment":"","layout":"","materials_and_props":"","time_weather":"",
"lighting":"","sound":"","continuity_anchors":[],"prompt_prefix":""}}],
"characters":[{{"name":"","importance":"主角/重要配角/非重要配角/背景路人",
"candidate_count":4,"gender":"","age_range":"","identity_facts":"",
"visual_direction":"","image_prompt":"无矛盾、可直接交给图片模型的人物出图提示词",
"negative_prompt":"本角色专属负面提示词",
"continuity_anchors":[],"prompt_prefix":"",
"character_analysis":{{"identity_and_class":"","age_and_presentation":"",
"upbringing":"","family_background":"","education_background":"",
"current_situation":"","core_desire":"","greatest_fear":"",
"formative_experiences":[],"strengths":[],"flaws":[],"behavior_habits":[]}},
"visual_dna":{{"face_structure":"","hair_silhouette":"",
"body_or_occupation_marks":"","clothing_structure":"",
"clothing_wear_state":"","story_visual_symbol":"",
"story_visual_symbol_origin":"","signature_accessory":"",
"temperament_keywords":[],"genre_system_mapping":{{}}}},
"cast_dedup":{{"compared_with":[],"dimensions":[],
"overlap_threshold":2,"status":"passed","conflicts":[],
"redesign_if_overlap":true}}}}],
"prompt_bible":{{"global_image_prefix":"","negative_prompt":"",
"character_prefix":"","scene_prefix":"","keyframe_prefix":"",
"seedance_prefix":"","readable_text_policy":"","continuity_rules":[]}}}}"""


def _match_brace(text, start):
    """返回 text[start]=='{' 的配对 '}' 下标(串内字符不计),找不到为 None。"""
    depth = 0
    in_str = False
    escaped = False
    for index in range(start, len(text)):
        char = text[index]
        if in_str:
            if escaped:
                escaped = False
            elif char == "\\":
                escaped = True
            elif char == '"':
                in_str = False
            continue
        if char == '"':
            in_str = True
        elif char == "{":
            depth += 1
        elif char == "}":
            depth -= 1
            if depth == 0:
                return index
    return None


def _mend_json_syntax(block, prefer_unwrap=False):
    """错误制导的机械修补:每轮只动解析器报错的那一处,直到可解析。

    实测教训(凡人修仙传分镜):40KB 合法产出因逐镜重复的一类笔误全盘
    报废,extract_json 只捞回一个镜头碎片→「缺少 shots」→整路报废。
    全局正则会误伤数组里合法的 },{ ,必须按解析器报错位置定点修:
    - 对象成员前多出悬空 { ("甲":{...},{"乙":{...) → 删这一个字符;
    - 对象成员被完整错包一层 {"乙":{...}} → 解开包裹层(删首尾一对);
      两类肉眼难分——_match_brace 会把父对象的闭括号误当包裹层,
      因此按 prefer_unwrap 分两遍策略跑,谁能让整体通过谁算数;
    - 对象/数组尾随逗号 → 删除该逗号;
    - JSON 主体后有杂讯(Extra data) → 截断到主体结束。
    修不动或超轮数即放弃(返回 None),绝不猜测语义。
    """
    for _ in range(60):
        try:
            json.loads(block)
            return block
        except json.JSONDecodeError as exc:
            pos, msg = exc.pos, exc.msg
            if msg.startswith("Extra data"):
                block = block[:pos]
                continue
            if pos >= len(block):
                return None
            char = block[pos]
            if msg.startswith("Expecting property name") and char == "{":
                close = _match_brace(block, pos) if prefer_unwrap else None
                inner = (block[pos + 1:close].strip()
                         if close is not None else "")
                if close is not None and inner.startswith('"'):
                    block = (block[:pos] + block[pos + 1:close]
                             + block[close + 1:])
                else:
                    block = block[:pos] + block[pos + 1:]
                continue
            if (msg.startswith("Expecting property name")
                    and char == "}") or (
                    msg.startswith("Expecting value") and char == "]"):
                comma = block.rfind(",", 0, pos)
                if comma == -1:
                    return None
                block = block[:comma] + block[comma + 1:]
                continue
            return None
    return None


def extract_json(text):
    """从模型输出中提取最大的合法 JSON 对象(容忍前后杂讯)。

    推理型引擎的最终答复常带分析文字,里面可能夹着小的 {...} 片段
    (示例、引用)。取"第一个"会抓到这些碎片,把整份剧本/分镜误判成
    "缺少 scenes/shots";取"最大的"才是真正的产出。
    整体解析失败时,对最外层大括号块做错误制导机械修补
    (_mend_json_syntax);修补版能解析出更大的对象就用修补版
    (局部碎片只是最后兜底)。
    """
    decoder = json.JSONDecoder()
    best = None
    best_span = -1
    idx = text.find("{")
    while idx != -1:
        try:
            obj, end = decoder.raw_decode(text[idx:])
        except ValueError:
            idx = text.find("{", idx + 1)
            continue
        if isinstance(obj, dict) and end > best_span:
            best, best_span = obj, end
        # 跳过整个已解析片段,避免重复解析其内部子对象
        idx = text.find("{", idx + max(end, 1))
    last = text.rfind("}")
    starts = []
    first = text.find("{")
    if first != -1:
        starts.append(first)
    # 真正的 JSON 根多在行首;杂讯里的花括号会让"从第一个 { 修起"走偏
    starts.extend(m.end() - 1 for m in re.finditer(r"(?m)^\s*\{", text))
    seen = set()
    for start in starts[:9]:
        if start in seen or last <= start:
            continue
        seen.add(start)
        if (last - start) <= best_span:
            continue
        for prefer_unwrap in (False, True):
            mended = _mend_json_syntax(
                text[start:last + 1], prefer_unwrap=prefer_unwrap)
            if not mended:
                continue
            try:
                obj = json.loads(mended)
            except ValueError:
                continue
            if isinstance(obj, dict) and len(mended) > best_span:
                best, best_span = obj, len(mended)
            break
    return best


WORLD_FIELDS = (
    "name", "overview", "era_and_location", "social_order", "hard_rules",
    "visual_baseline",
)
BACKGROUND_FIELDS = (
    "prior_events", "current_situation", "core_conflict", "episode_goal",
    "continuity_hooks",
)
CHARACTER_INTRO_FIELDS = (
    "introduction", "gender", "age_range", "identity", "personality",
)
CHARACTER_PROFILE_FIELDS = (
    "background_prompt", "era_setting", "occupation", "motivation",
    "backstory", "relationships", "costume_direction", "signature_props",
    "visual_variants", "character_analysis", "visual_dna", "cast_dedup",
)
BACKGROUND_ROLE_TOKENS = (
    "背景路人", "背景人物", "背景群众", "群众演员", "群演", "跑龙套",
    "龙套", "临时路人", "路人角色", "路人",
)


def is_background_role(character):
    role = str((character or {}).get("role") or "").strip().lower()
    return any(token in role for token in BACKGROUND_ROLE_TOKENS)


def _missing(value):
    return value is None or (isinstance(value, str) and not value.strip())


def _fill_missing(target, key, value):
    if _missing(target.get(key)):
        target[key] = value


def _sentence(value):
    value = str(value).strip()
    return value if value.endswith(("。", "！", "？", ".", "!", "?")) else value + "。"


def normalize_script_bible(script, payload=None):
    """把新写、人工导入和旧版剧本统一成不会乱串的剧情事实源。

    真实编剧输出应当提供完整内容；这里的确定性补全主要服务旧数据和用户导入，
    且会显式写出“未指定时禁止猜测”，避免下游模型自行脑补身份。
    """
    if not isinstance(script, dict):
        return script
    payload = payload or {}
    title = (script.get("project_title")
             or payload.get("project_title") or "本剧")
    premise = payload.get("premise") or script.get("logline") or "本集剧情"
    style = (
        payload.get("style")
        or "待 AI 根据剧本全文生成的本剧专属制作风格")
    scenes = script.get("scenes") or []
    for position, scene in enumerate(scenes, 1):
        if not isinstance(scene, dict):
            continue
        scene.setdefault(
            "event_id",
            f"scene:{scene.get('scene_no', position)}")
    locations = "、".join(dict.fromkeys(
        scene.get("location") for scene in scenes
        if isinstance(scene, dict) and scene.get("location")
    )) or "剧本已声明场景"

    world = script.get("story_world")
    if not isinstance(world, dict):
        world = {}
        script["story_world"] = world
    _fill_missing(world, "name", f"《{title}》故事世界")
    _fill_missing(
        world, "overview",
        f"故事围绕“{premise}”展开，所有人物、事件与场景均服从同一世界设定。")
    _fill_missing(world, "era_and_location",
                  f"时代以剧本明示为准，主要地域为{locations}。")
    _fill_missing(
        world, "social_order",
        "社会身份、组织阵营、职业权限和人物关系以人物表与场次事实为准。")
    _fill_missing(
        world, "hard_rules",
        "不得新增未声明的能力、技术、组织、物种或身份；同一事实跨镜头保持一致。")
    _fill_missing(
        world, "visual_baseline",
        f"{style}；建筑、服装、发型、妆容、道具和光影必须符合时代地域与人物身份。")
    forbidden = world.get("forbidden_drift")
    if not isinstance(forbidden, list) or not any(
            str(item).strip() for item in forbidden):
        world["forbidden_drift"] = [
            "禁止时代、地域、能力/技术规则和组织阵营漂移",
            "禁止新增人物、改变人物性别年龄身份或混淆人物关系",
            "禁止建筑、服装、发型、妆容和道具脱离世界视觉基准",
        ]
    # 穿越/带入物白名单:剧情明确允许跨时代出现的物品(如穿越者的手机)。
    # 时代校验一律以剧本为准——名单内物品必须按剧情出现,不算时代错乱。
    sanctioned = world.get("sanctioned_anachronisms")
    if not isinstance(sanctioned, list):
        world["sanctioned_anachronisms"] = []
    else:
        world["sanctioned_anachronisms"] = [
            str(item).strip() for item in sanctioned if str(item).strip()]

    background = script.get("story_background")
    if not isinstance(background, dict):
        background = {}
        script["story_background"] = background
    logline = script.get("logline") or premise
    _fill_missing(background, "prior_events",
                  "本集开始前的已知前情：" + _sentence(premise))
    _fill_missing(background, "current_situation",
                  "本集开场局势：" + _sentence(logline))
    _fill_missing(background, "core_conflict", logline)
    _fill_missing(background, "episode_goal",
                  "让本集核心冲突发生明确推进，并在结尾留下可核验的状态变化。")
    _fill_missing(
        background, "continuity_hooks",
        "人物身份、关系、持有道具、伤势、服装、情绪和空间状态承接前后镜头与前后集。")

    appearances = {}
    for scene in scenes:
        if not isinstance(scene, dict):
            continue
        for name in scene.get("characters") or []:
            appearances.setdefault(name, []).append(
                scene.get("location") or "本集场景")
    cast = script.get("characters") or []
    cast_names = [
        character.get("name") for character in cast
        if isinstance(character, dict) and character.get("name")
    ]
    for character in cast:
        if not isinstance(character, dict) or not character.get("name"):
            continue
        name = character["name"]
        role = character.get("role") or "角色"
        places = "、".join(dict.fromkeys(
            appearances.get(name, []))) or locations
        if is_background_role(character):
            # 路人只承担场次中的人数/功能，不建立会触发候选图与套件的个人档案。
            for field in CHARACTER_INTRO_FIELDS + CHARACTER_PROFILE_FIELDS:
                character.pop(field, None)
            _fill_missing(
                character, "crowd_function",
                f"仅作为{places}中的短暂场景功能角色，按分镜声明的人数出现；"
                "无独立人物设定、候选图、立绘或四视图。")
            character["asset_policy"] = "scene_only_no_individual_asset"
            character["candidate_count"] = 0
            continue
        _fill_missing(
            character, "introduction",
            f"{name}是本剧{role}，活动于{places}；身份、性格、目标及与"
            "其他角色的关系以本人物表和台词行动为准，后续镜头不得换人或改身份。")
        _fill_missing(
            character, "gender",
            "未指定（人物定版前禁止下游模型自行猜测，定版后以参考图为准）")
        _fill_missing(
            character, "age_range",
            "未指定（人物定版前禁止下游模型自行猜测，定版后保持一致）")
        _fill_missing(character, "identity",
                      character.get("occupation") or role)
        _fill_missing(
            character, "personality",
            "由剧本台词、行动与人物背景外化，跨场景保持同一性格逻辑")
        character["candidate_count"] = 4
    props = script.get("core_props")
    if not isinstance(props, list):
        script["core_props"] = []
    else:
        normalized_props = []
        seen_props = set()
        for item in props:
            if not isinstance(item, dict):
                continue
            name = str(item.get("name") or "").strip()
            if not name or name in seen_props:
                continue
            seen_props.add(name)
            item["name"] = name
            item["candidate_count"] = 4
            for field in (
                    "story_function", "visual_design", "era_material",
                    "owner", "continuity_states"):
                item.setdefault(field, "")
            normalized_props.append(item)
        script["core_props"] = normalized_props
    script["story_bible_version"] = 1
    script["declared_character_names"] = cast_names
    script["inner_persona_policy"] = normalize_inner_persona_policy(script)
    normalize_script_logic(script)
    return script


def validate_script_bible(script, *, require_resolved_identity=True):
    """返回世界观/前情/人物介绍硬门禁错误；通过时返回 ``None``。"""
    strip_non_person_speakers(script)
    normalize_prop_contract(script)
    prop_report = audit_prop_contract(script)
    if not prop_report["passed"]:
        return "结构化道具合同无效: " + "；".join(prop_report["issues"])
    scene_event_ids = []
    for position, scene in enumerate(script.get("scenes") or [], 1):
        if not isinstance(scene, dict):
            continue
        scene.setdefault(
            "event_id",
            f"scene:{scene.get('scene_no', position)}")
        event_id = str(scene.get("event_id") or "").strip()
        if not event_id:
            return f"第{position}场缺少稳定 event_id"
        if event_id in scene_event_ids:
            return f"场次 event_id 重复: {event_id}"
        scene_event_ids.append(event_id)
    known_events = set(scene_event_ids) | {"episode-start", "episode-end"}
    for item in script.get("prop_registry") or []:
        if not isinstance(item, dict):
            continue
        for field, alias, fallback in (
                ("availability_start_event", "introduced_at",
                 "episode-start"),
                ("availability_end_event", "retired_at", "episode-end")):
            ref = item.get(field)
            if not ref:
                continue
            event_id = str(
                ref.get("event_id") if isinstance(ref, dict) else ref
            ).strip()
            if event_id not in known_events:
                # 模型偶发引用幻觉场次号(如 eval_test_scene),修复引擎也
                # 不知道合法号——本地回落到最宽可用窗口(episode 边界):
                # 语义安全,帧级 frame_props 照常管每镜可见性;原值留档。
                phase = "start" if fallback == "episode-start" else "end"
                repaired = {"event_id": fallback, "phase": phase,
                            "unresolved_event_id": event_id}
                item[field] = repaired
                item[alias] = dict(repaired)
    world = script.get("story_world")
    if not isinstance(world, dict):
        return "缺少 story_world 故事世界设定"
    for field in WORLD_FIELDS:
        if _missing(world.get(field)):
            return f"故事世界字段不全: story_world.{field}"
    if not isinstance(world.get("forbidden_drift"), list) or not any(
            str(item).strip() for item in world["forbidden_drift"]):
        return "故事世界字段不全: story_world.forbidden_drift"
    background = script.get("story_background")
    if not isinstance(background, dict):
        return "缺少 story_background 故事背景"
    for field in BACKGROUND_FIELDS:
        if _missing(background.get(field)):
            return f"故事背景字段不全: story_background.{field}"
    declared = set()
    for character in script.get("characters") or []:
        if not isinstance(character, dict) or not character.get("name"):
            continue
        declared.add(character["name"])
        if is_background_role(character):
            if _missing(character.get("crowd_function")):
                return f"{character['name']}背景路人缺少场次功能: crowd_function"
            if any(not _missing(character.get(field))
                   for field in CHARACTER_INTRO_FIELDS):
                return f"{character['name']}是背景路人，不应建立独立人物设定"
            continue
        for field in CHARACTER_INTRO_FIELDS:
            if _missing(character.get(field)):
                return f"{character['name']}人物设定字段不全: {field}"
        if require_resolved_identity:
            unresolved = unresolved_identity_fields(character)
            if unresolved:
                return (
                    f"{character['name']}人物{'、'.join(unresolved)}必须明确，"
                    "不能使用未指定、待确认或以参考图为准等占位表达")
    for prop in script.get("core_props") or []:
        if not isinstance(prop, dict) or _missing(prop.get("name")):
            return "核心道具字段不全: name"
        for field in (
                "story_function", "visual_design", "era_material",
                "owner", "continuity_states"):
            if _missing(prop.get(field)):
                return f"{prop['name']}核心道具字段不全: {field}"
        if prop.get("candidate_count") != 4:
            return f"{prop['name']}核心道具候选数量必须为4"
    used = set()
    for scene in script.get("scenes") or []:
        used.update(scene.get("characters") or [])
        used.update(
            line.get("character") for line in scene.get("lines") or []
            if isinstance(line, dict) and line.get("character"))
    # 旁白/音效是声音来源不是人物,不参与“角色必须已声明”的核对。
    unknown = sorted(
        name for name in (used - declared) if not is_non_person_label(name))
    if unknown:
        return "场次出现未在人物设定中介绍的角色: " + "、".join(unknown)
    logic = script.get("script_logic_audit") or {}
    if not logic.get("passed"):
        issues = logic.get("issues") or ["剧本缺少编剧/导演级逻辑审查"]
        return "剧本物理/空间/可拍摄性门禁失败: " + "；".join(issues[:6])
    return None


def strip_non_person_speakers(script):
    """旁白/音效/字幕不是人物:从人物表和场次名单剔除,台词保留并标记。

    必须跑在“场次角色是否都已声明”的校验之前。否则模型把旁白正确地当成
    声音来源写进台词、却(正确地)没写进人物表时,校验会判失败并要求重来
    ——模型下一轮多半就把旁白补进人物表来迎合校验。这个反向激励正是该
    问题被修好又反复出现的原因之一。
    """
    if not isinstance(script, dict):
        return []
    removed = []

    def note(label):
        label = str(label or "").strip()
        if label and label not in removed:
            removed.append(label)

    characters = script.get("characters")
    if isinstance(characters, list):
        kept = []
        for item in characters:
            if isinstance(item, dict) and is_non_person_label(
                    item.get("name")):
                note(item.get("name"))
                continue
            kept.append(item)
        script["characters"] = kept
    for scene in script.get("scenes") or []:
        if not isinstance(scene, dict):
            continue
        for line in scene.get("lines") or []:
            if isinstance(line, dict) and is_non_person_label(
                    line.get("character")):
                line["non_person_voice"] = True
        names = scene.get("characters")
        if isinstance(names, list):
            kept_names = []
            for name in names:
                if is_non_person_label(name):
                    note(name)
                    continue
                kept_names.append(name)
            scene["characters"] = kept_names
    # 留痕给导演层记入经验库(能穿过子进程 JSON 回到主进程)
    if removed:
        existing = script.get("non_person_labels_removed")
        merged = list(existing) if isinstance(existing, list) else []
        for label in removed:
            if label not in merged:
                merged.append(label)
        script["non_person_labels_removed"] = merged
    return removed


def validate_script(script, payload):
    if payload.get("character_design"):
        return validate_design(script, payload)
    if not isinstance(script, dict):
        return "输出不是 JSON 对象"
    strip_non_person_speakers(script)
    if not script.get("scenes"):
        return "缺少 scenes"
    if not script.get("characters"):
        return "缺少 characters"
    for character in script["characters"]:
        if not isinstance(character, dict) or not character.get("name"):
            return f"角色字段不全: {character}"
        if not is_background_role(character):
            for key in CHARACTER_PROFILE_FIELDS:
                default = (
                    [] if key == "visual_variants"
                    else {} if key in (
                        "character_analysis", "visual_dna", "cast_dedup")
                    else "")
                character.setdefault(key, default)
    for scene in script["scenes"]:
        if not scene.get("location") or "scene_no" not in scene:
            return f"场次字段不全: {scene}"
        for line in scene.get("lines", []):
            if not line.get("character") or not line.get("dialogue"):
                return f"台词字段不全: {line}"
        scene.setdefault("characters", sorted(
            {ln["character"] for ln in scene.get("lines", [])
             if not ln.get("non_person_voice")}))
        scene.setdefault("action", "")
    # 平台侧字段兜底,避免下游因缺字段中断
    script.setdefault("project_title", payload.get("project_title", ""))
    script.setdefault("episode_number", payload.get("episode_number", 0))
    script.setdefault("episode_title", "")
    script.setdefault("logline", "")
    normalize_script_bible(script, payload)
    # AI 初稿即使漏写性别/年龄也必须进入制作圣经供人工补录，不能在
    # Provider 层静默回退成另一份 mock 剧本。真正锁定人物、生成候选图
    # 时仍由 validate_script_bible 的默认严格模式阻断。
    return validate_script_bible(
        script, require_resolved_identity=False)


def validate_storyboard(storyboard):
    if not isinstance(storyboard, dict) or not storyboard.get("shots"):
        return "缺少 shots"
    if not isinstance(storyboard["shots"], list):
        return "shots 需为数组"
    normalize_prop_contract(storyboard)
    # 本地就地修:道具时间线缺失端(start/end 不成对、仅 freeze)按
    # "该镜内无状态变化"克隆回填,不为几行显式条目丢弃整份分镜。
    normalize_storyboard_frame_phase_pairs(storyboard)
    prop_registry_report = audit_prop_contract(storyboard)
    if not prop_registry_report["passed"]:
        return "分镜 prop_registry 无效: " + "；".join(
            prop_registry_report["issues"])
    for shot_position, shot in enumerate(storyboard["shots"], 1):
        if not isinstance(shot, dict):
            return f"镜头需为对象,收到: {str(shot)[:80]}"
        shot.setdefault(
            "event_id",
            str(shot.get("unit_id") or f"shot:{shot_position}"))
        shot.setdefault(
            "scene_event_id",
            f"scene:{shot.get('scene_no')}")
        for field in ("scene_no", "duration", "prompt"):
            if field not in shot:
                return f"镜头缺少字段 {field}: {shot}"
        try:
            shot["duration"] = float(shot["duration"])
        except (TypeError, ValueError):
            return f"镜头时长非法: {shot}"
        if shot["duration"] <= 0:
            return f"镜头时长非法: {shot}"
        characters = shot.setdefault("characters", [])
        if not isinstance(characters, list):
            return f"镜头 characters 需为数组: {shot}"
        shot["characters"] = list(dict.fromkeys(
            str(name).strip() for name in characters
            if str(name).strip()))
        functional_figures = shot.setdefault("functional_figures", [])
        if not isinstance(functional_figures, list):
            return f"镜头 functional_figures 需为数组: {shot}"
        normalized_figures = []
        fuzzy_counts = ("几名", "数名", "一群", "多人", "若干")
        for figure in functional_figures:
            if not isinstance(figure, dict):
                return f"功能人物需为对象: {figure}"
            label = str(
                figure.get("name") or figure.get("label") or "").strip()
            if not label:
                return f"功能人物缺少 name/label: {figure}"
            count = figure.get("count")
            if type(count) is not int or count <= 0:
                return f"功能人物 count 必须为精确正整数: {figure}"
            wording = " ".join(str(figure.get(key) or "") for key in (
                "name", "label", "state", "function"))
            if any(token in wording for token in fuzzy_counts):
                return f"功能人物禁止模糊数量词，必须使用精确 count: {figure}"
            normalized = dict(figure)
            normalized["count"] = count
            normalized_figures.append(normalized)
        shot["functional_figures"] = normalized_figures
        shot["visible_figure_count"] = (
            len(shot["characters"])
            + sum(item["count"] for item in normalized_figures))
        shot.setdefault("kind",
                        "dialogue" if shot.get("dialogue") else "environment")
        shot.setdefault("description", "")
        shot.setdefault("camera", "")
        style_direction = shot.setdefault("style_direction", {})
        if not isinstance(style_direction, dict):
            return f"镜头 style_direction 需为对象: {shot}"
        if style_direction:
            if style_direction.get("schema") not in (
                    None, "", "firefire.director-style/v1"):
                return f"镜头 style_direction.schema 非法: {shot}"
            style_direction["schema"] = "firefire.director-style/v1"
            style_direction["shot_pattern"] = str(
                style_direction.get("shot_pattern") or "").strip()
            effects = style_direction.get("visual_effects") or []
            if not isinstance(effects, list):
                return f"镜头 style_direction.visual_effects 需为数组: {shot}"
            style_direction["visual_effects"] = list(dict.fromkeys(
                str(item).strip() for item in effects if str(item).strip()))
            style_direction["selection_reason"] = str(
                style_direction.get("selection_reason") or "").strip()
        shot.setdefault("dialogue", None)
        shot.setdefault("physical_logic", "")
        shot.setdefault("start_state", {})
        shot.setdefault("end_state", {})
        shot.setdefault("readable_text", None)
        frame_targets = shot.get("frame_targets")
        if not isinstance(frame_targets, dict):
            return f"镜头缺少显式 frame_targets: {shot}"
        for target_name, expected_phase in (
                ("keyframe", {"start", "end", "freeze"}),
                ("first_frame", {"start", "freeze"}),
                ("last_frame", {"end", "freeze"})):
            target = frame_targets.get(target_name)
            if not isinstance(target, dict):
                return f"镜头 frame_targets.{target_name} 必须是对象: {shot}"
            phase = str(target.get("phase") or "").strip().lower()
            if phase not in expected_phase:
                return (
                    f"镜头 frame_targets.{target_name}.phase 非法: "
                    f"{phase or '空'}")
            target_state = target.get("state")
            if (_missing(target_state)
                    or (isinstance(target_state, (dict, list))
                        and not target_state)):
                return f"镜头 frame_targets.{target_name}.state 不能为空"
            if target.get("fallback") is not False:
                return (
                    f"镜头 frame_targets.{target_name}.fallback 必须明确为 false")
        frame_props = shot.setdefault("frame_props", [])
        if not isinstance(frame_props, list):
            return f"镜头 frame_props 需为数组: {shot}"
        for frame_prop in frame_props:
            if not isinstance(frame_prop, dict):
                continue
            frame_prop["prop_id"] = str(
                frame_prop.get("prop_id") or "").strip()
            for field in ("phase", "visibility", "representation"):
                frame_prop[field] = str(
                    frame_prop.get(field) or "").strip().lower()
        transitions = shot.setdefault("prop_transitions", [])
        if not isinstance(transitions, list):
            return f"镜头 prop_transitions 需为数组: {shot}"
        for transition in transitions:
            if not isinstance(transition, dict):
                continue
            transition["prop_id"] = str(
                transition.get("prop_id") or "").strip()
            for field in ("from_phase", "to_phase"):
                transition[field] = str(
                    transition.get(field) or "").strip().lower()
    # 编号强制连续,避免下游连续性质检失败
    for index, shot in enumerate(storyboard["shots"], start=1):
        shot["shot_no"] = index
    prop_report = audit_storyboard_prop_contract(storyboard)
    if not prop_report["passed"]:
        return "结构化镜头道具合同无效: " + "；".join(
            prop_report["issues"])
    storyboard.setdefault("episode_title", "")
    storyboard["prop_contract_schema"] = PROP_CONTRACT_SCHEMA
    return None


DESIGN_PROMPT = """你是漫剧人物设定师。为作品《{title}》的角色写生产级人物设定,
供 AI 出图使用(候选立绘/三视图审核板/独立正侧背母资产/妆容服装设定全部依据它)。
画风:{style}。剧情梗概:{logline}。本集前提:{premise}。

角色名单(全部要写,名字必须逐字一致):{names}
剧本人物背景(必须逐条吸收,不得改成模板化现代都市):{story_context}
本集场景锚点(用于决定每套服装/道具的剧情场合):{scene_context}
{references}
要求:
- 每个字段是一段具体、可画出来的描述(不要空话套话);
- 必须先理解角色的时代/世界观、地域、职业、成长经历、核心目标、关系和本集冲突,
  再决定服装和造型;人物背景与画风冲突时以剧情时代/世界观为准,不能把所有人都套成现代都市;
- `background_prompt` 要写成一段可直接拼进文生图的完整人物背景提示词,至少包含身份来源、
  当前处境、性格外化方式、动机、冲突、关系和视觉符号;
- `image_prompt` 是最终交给图片模型的单人物出图卡，必须先消解所有冲突，只保留一套
  明确的姓名、性别、年龄、身份、骨相、肤色、体态、发型、服装结构/材质/层次、
  必要身份配饰、气质、时代约束、渲染媒介、构图与纯净背景，控制在约 300-650 个
  中文字符；只写单张图可直接看见的事实。不得写人物身世、父母关系、知识储备、
  内心计划、倒计时、说服策略、道具来源，不得写多套备选服装，不得写“未知/自行
  猜测”，不得粘贴内部审计 JSON、候选比较过程或其他人物的设定；
  `negative_prompt` 写本角色专属禁止项;
- 必须先写 `character_analysis`：身份阶层、年龄感、成长与家庭/教育背景、当前处境、
  核心欲望、最大恐惧、关键经历、性格优缺点和行为习惯；原文未说明的内容只能保守推导;
- 再把人物分析转译为 `visual_dna`：脸部骨相、发型轮廓、身体/职业痕迹、服装结构与
  磨损状态、核心配饰、带明确故事来源的视觉符号和 3-8 个气质关键词；不得只写帅、美、
  冷峻、高级，也不得套 AI 网红脸、韩式偶像脸或模板男女主;
- `cast_dedup` 必须与剧本人物背景中的全部正式角色及已有角色设定逐一比较；
  发型、服装结构、身体特征、视觉符号、核心配饰、气质关键词中两项以上重叠必须主动
  更换结构方案，只有完成重设计后才能输出 `status:"passed"`;
- `era_setting`、`occupation`、`motivation`、`backstory`、`relationships`、
  `costume_direction`、`signature_props` 必须具体;职业身份要能从服装和装备一眼识别;
- `visual_variants` 必须给出 3-5 个与剧情兼容的造型方案,每项写清场合、服装、材质、
  配色、配饰/道具和气质变化，并按剧情时间顺序排列；第1项必须是首次登场前的
  干燥、洁净、无伤基础定妆，禁止官服等后续换装、淋湿、泥污、血迹、伤口或死亡态；
  后续项只供镜头连续性使用。人物定角4张候选必须复用第1项编译出的同一最终提示词，
  不得为4张图分别设计服装、妆容、动作或剧情状态，只靠图片模型随机采样;
- 传入的正式角色必须标注重要度:主角、重要配角或非重要配角;不得补画或扩写
  跑龙套/背景路人,这类角色只在剧本场次中保留人数与功能标签,不建立独立设定或人物资产;
- 性格要能从表情神态与站姿体现;外貌含脸型/肤色/身材比例;
- 服装要具体到款式、材质、层次;配色给出主色与点缀色;
- 如果角色有职业或工作身份(如外卖小哥、快递员、医生、护士、警察、消防员、
  保安、服务员、厨师、工人等),costume 必须写清该职业真实可辨认的工作服/制服
  和必要装备,禁止用普通便服代替;
- 有参考图时,参考图人物的脸和发型是最高标准;appearance/hair/eyes/makeup/
  signature/temperament 必须先看图再写,不得凭空换脸或换发型;
- 只输出一个 JSON 对象,不要任何其他文字或 Markdown 代码块。

JSON 格式:
{{"designs": [{{
  "name": "角色名",
  "species": "物种/形态(默认人类;若是动物/精怪/机器人等明确写出)",
  "personality": "性格(外化到神态)",
  "temperament": "气质", "appearance": "外貌(脸型/肤色/身材)",
  "hair": "发型发色", "eyes": "眼睛(形状/瞳色/眼神)",
  "makeup": "妆容细节", "costume": "服装(款式/材质/层次)",
  "costume_detail": "服装细节(纹样/扣饰/鞋履)",
  "accessories": "配饰", "palette": "主配色与点缀色",
  "signature": "标志性辨识特征",
  "background_prompt": "完整人物背景提示词(身份/经历/处境/动机/冲突/视觉象征)",
  "image_prompt": "最终无矛盾、可直接出图的单人物提示词",
  "negative_prompt": "本角色专属负面提示词",
  "era_setting": "时代/世界观/地域", "occupation": "职业/社会身份",
  "motivation": "核心目标", "backstory": "关键经历或秘密",
  "relationships": "关系与冲突", "costume_direction": "服装设计逻辑",
  "signature_props": "标志性道具", "visual_variants": [
    {{"label": "日常/身份", "costume": "...", "palette": "...", "props": "...", "temperament": "..."}},
    {{"label": "行动/冲突", "costume": "...", "palette": "...", "props": "...", "temperament": "..."}},
    {{"label": "关键场合", "costume": "...", "palette": "...", "props": "...", "temperament": "..."}}
  ],
  "character_analysis": {{"identity_and_class":"身份与社会阶层",
    "age_and_presentation":"年龄感与性别呈现","upbringing":"成长环境",
    "family_background":"家庭背景","education_background":"教育背景",
    "current_situation":"当前处境","core_desire":"核心欲望",
    "greatest_fear":"最大恐惧","formative_experiences":["关键人生经历"],
    "strengths":["性格优点"],"flaws":["性格缺陷"],"behavior_habits":["行为习惯"]}},
  "visual_dna": {{"face_structure":"脸型与骨相","hair_silhouette":"发型轮廓",
    "body_or_occupation_marks":"身体或职业痕迹","clothing_structure":"服装结构",
    "clothing_wear_state":"服装状态","story_visual_symbol":"视觉符号",
    "story_visual_symbol_origin":"符号的故事来源","signature_accessory":"核心配饰",
    "temperament_keywords":["3-8个气质关键词"],"genre_system_mapping":{{}}}},
  "cast_dedup": {{"compared_with":["其他正式角色"],"dimensions":[
    "hair_silhouette","clothing_structure","body_or_occupation_marks",
    "story_visual_symbol","signature_accessory","temperament_keywords"],
    "overlap_threshold":2,"status":"passed","conflicts":[],"redesign_if_overlap":true}}
}}]}}"""

# 人物设定必填字段;缺失时置空串,提示词侧自动跳过
DESIGN_FIELDS = ("species", "personality", "temperament",
                 "appearance", "hair",
                 "eyes", "makeup", "costume", "costume_detail",
                 "accessories", "palette", "signature", "background_prompt",
                 "image_prompt", "negative_prompt",
                 "era_setting", "occupation", "motivation", "backstory",
                 "relationships", "costume_direction", "signature_props",
                 "visual_variants", "character_analysis", "visual_dna",
                 "cast_dedup")


def validate_design(data, payload):
    designs = data.get("designs")
    if not isinstance(designs, list) or not designs:
        return "缺少 designs"
    wanted = [c.get("name") for c in payload.get("characters", [])]
    got = {d.get("name"): d for d in designs if d.get("name")}
    missing = [n for n in wanted if n not in got]
    if missing:
        return f"缺少角色设定: {'、'.join(missing)}"
    for design in designs:
        for key in DESIGN_FIELDS:
            design.setdefault(
                key,
                [] if key == "visual_variants"
                else {} if key in (
                    "character_analysis", "visual_dna", "cast_dedup")
                else "")
        if not design["species"]:
            design["species"] = "人类"
        if not (design.get("personality") and design.get("appearance")
                and design.get("costume")):
            return f"角色「{design.get('name')}」设定过于空泛" \
                   "(personality/appearance/costume 必填)"
        if not isinstance(design.get("character_analysis"), dict):
            return f"角色「{design.get('name')}」character_analysis 必须是对象"
        if not isinstance(design.get("visual_dna"), dict):
            return f"角色「{design.get('name')}」visual_dna 必须是对象"
        if not isinstance(design.get("cast_dedup"), dict):
            return f"角色「{design.get('name')}」cast_dedup 必须是对象"
    return None


IMAGE_QC_PROMPT = """你是漫剧图片质检员。查看图片文件 {image}(可直接读取),
逐项核对是否符合生产要求。

当前镜头本次真实生成输入(只能分析这一镜，禁止补写整集剧情或其他镜头):
- 范围:{generation_scope}
- 实际提交提示词:{generation_prompt}
- 实际提交参考图对照表:{generation_references}
除判断画面错误外，必须分别判断：提示词是否准确、简洁、无冲突、无无关剧情；
参考图是否属于本镜、人物/角色/用途是否绑定正确、是否有缺失或冲突。诊断只能
引用上述实际输入，不得虚构未提交的提示词或参考图。输入诊断是后续优化建议：
提示词重复、略长或参考图说明不够简洁，不得单独令 visual_pass 或 pass 为 false。

质检判定阈值（按手机竖屏正常播放观看，禁止放大像素挑刺）：
- 只有普通观众一眼可见、会影响身份识别、剧情理解或画面可信度的明显问题才失败：
  明显错人/错性别/错人数，人物严重跑脸，关键服装、道具、场景或时代明显错误，
  剧情必需文字错误，以及明显肢体畸形、穿模、悬浮、设备反向或空间关系不可能。
- 轻微肤质噪点、细小色差、衣物旧化程度、发丝/皱纹/妆效细节、轻微表情或视线
  偏差、非关键配饰细差、看不清且不影响剧情的背景小物，只记录为建议，必须通过。
- 人物身份只在“明显不是同一人”或锁定脸型/发型发生显著改变时失败；正常生成波动
  和不同角度造成的小差异不得失败。不确定时从宽通过，交由人工抽检。

最终立绘视觉基准(这些图片是身份事实来源，优先级高于早期文字描述):
{identity_references}
必须先逐人判断实际可见视角，再按视角核验，不得强迫所有人物露正脸:
- 正面/四分之三:核对脸型、五官比例、眼鼻嘴、发际线、年龄感和性别表达。
- 严格侧面:核对额头—鼻梁—唇—下颌侧廓、耳朵、发际线、发型轮廓、
  体型、服装、道具和站位；不得因只见一只眼或看不全正脸失败。
- 背面/半背影/过肩前景:核对后脑/帽冠和发型轮廓、肩背体型、服装背片/
  接缝/材质配色、身份配饰、道具、朝向和站位；正脸不可见本身不是错误。
  若这些可见身份点相符，identity_match 必须为 true。
当前镜头逐角色构图合同:{composition_rules}
必须单独核对每个人物的性别与性别表达；女性被画成男性、男性被画成女性，
一律是身份硬错误，不能因发色、服装或气质相似而通过。
当前镜头逐角色服装/头饰/妆发状态:{expected_wardrobe}
只要本镜声明了服装状态，就必须逐人核对实际画面；上一镜已经锁定官服而本镜
无换装动作却变成常服、漏掉帽冠、擅自改发型或妆容，一律是连续性硬错误。
场景连续性对照:{scene_continuity}
背面/侧面镜头按可见服装轮廓、背片、材质、颜色、头饰与发型核对，不要求正脸。
必须点数画面里实际可见的人物总数，并与要求人数逐一核对；多一个、少一个、
同一角色被复制两次或把两人合成一人都必须失败。
过肩构图中“1个正面主体 + 1个前景半背影对话者”是两个已登记角色槽位；
前景肩膀/半身只计作该对话者1人，不得另算成第三人、陌生人或人物复制。
{overlay_rules}
文字设定只补充剧情、动作、场景和当镜服装；与最终立绘冲突时以最终立绘为准。

画面要求:
- 出场角色:{characters}({count_rule};角色形态必须与人物设定
  一致——设定写明物种就按设定画,没写明的默认人类;名字不代表物种,
  「小鹿」若设定是人类就必须是人类,不能因为名字画成动物)
- 人物设定要点(脸型/五官/发型/发色/妆容/年龄感/标志特征必须一致；
  服装按本镜剧本和当集造型核对,允许与身份参考图不同):{designs}
- 角色性别硬事实:{expected_genders}
- 场景:{location};动作:{action};镜头:{camera}
  (景别需大致相符:要求全景/远景不能给成特写,反之亦然)
- 物理/空间逻辑硬检查:{physical_contract}
  必须核对人物、镜头、道具的前后左右关系、朝向、视线、接触点、重力支撑和动作可达性；
  电脑/手机/屏幕等设备必须按真实使用方向成立，屏幕正面、键盘/手部和使用者关系不能反向。
  只有明显且影响剧情理解或画面可信度的物理/空间错误才令 pass 为 false，并在 issues
  指出具体对象和位置；细微透视误差、遮挡造成的不确定关系和背景小物偏差不得失败。
- 不允许出现:与设定形态不符的角色、与剧情无关的杂物(悬挂的衣物/衣架)、
  字幕条、乱码文字、多余或缺失的人物{extra}
- 这是静态关键帧质检：只检查画面中能看见的最终状态。不得因为单张图无法证明
  运镜、眼神变化过程、呼吸或其他时间动作而判失败；景别裁掉且并非剧情要求必须
  出镜的裤子、腰间配饰等，也不得仅因不可见而判失败。

只输出一个 JSON 对象,不要任何其他文字。必须把“画面本身是否正确”和
“生成输入合同是否正确”分开判断；参考图编号/用途冲突不能伪装成画面人物错误:
{{"pass": true或false, "visual_pass": true或false,
"input_contract_pass": true或false,
"identity_checked": true或false,
"identity_match": true或false,
"identity_checks": [{{"character":"角色名",
"view":"front_or_three_quarter/profile/back/back_or_over_shoulder",
"basis":["实际核验项"],"checked":true或false,"match":true或false}}],
"gender_checked": true或false, "gender_match": true或false,
"wardrobe_checked": true或false, "wardrobe_match": true或false,
"count_checked": true或false, "count_match": true或false,
"overlay_count_checked": true或false, "overlay_count_match": true或false,
"detected_overlay_count": 画面实际内心Q版叠层数整数,
"physical_logic_checked": true或false, "physical_logic_match": true或false,
"spatial_logic_checked": true或false, "spatial_logic_match": true或false,
"detected_count": 画面实际人数整数,
"issues": ["不通过的具体原因,每条一句,指出在画面哪里"],
"image_error": {{"summary":"画面错误摘要","categories":["identity/count/camera等"],
"evidence":["画面中可见证据"]}},
"prompt_diagnosis": {{"status":"correct/needs_patch/conflicting/insufficient",
"issues":["提示词问题"],"irrelevant_or_conflicting_sections":["应删除或冲突片段"]}},
"reference_diagnosis": {{"status":"correct/needs_adjustment/conflicting/missing/uncertain",
"issues":["参考图问题"],"missing_roles":[{{"role":"identity/spatial/scene等",
"character":"角色名或空","reason":"缺失原因"}}]}},
"targeted_prompt_patch": {{"instructions":["只针对当前镜头的短修正指令"],
"preserve":["必须保持不变的内容"],"max_scope":"current_shot_only"}},
"reference_adjustments": [{{"action":"keep/remove/rebind/replace/add/drop_revision_base",
"target_index":参考图编号整数,"role":"用途","character":"角色名或空",
"replacement_selector":{{"asset_id":已有资产ID或null,"role":"用途","character":"角色名或空"}},
"reason":"调整原因"}}]}}"""


# 双路会诊的分工清单:两路都看全图、都给整体判定,但各自深查一半。
# 单判官扛二十项清单必然前详后略(台账 other 桶最大即是证据),拆成
# 两份重点清单,同样成本换更细的核验;任一路缺席自动降级单路。
REVIEW_LENSES = {
    "identity": {
        "index": 1,
        "label": "身份与人物",
        "focus": (
            "①人数:画面可见真人总数与合同是否严格一致(逐个点数并报出你数到"
            "的人,别漏背景里的人);②每个人物的脸型、五官骨相、发型轮廓"
            "是否与其最终立绘同一人,有无张冠李戴;③性别表达与年龄段;"
            "④物种形态(设定没写非人就必须是人);⑤生死/昏迷等硬状态在"
            "面部与身体上是否成立(尸体不得有注视方向与表情张力);"
            "⑥人物朝向与镜位是否自洽"),
    },
    "scene": {
        "index": 2,
        "label": "道具与场景",
        "focus": (
            "①核心道具是否在场、形制是否与母版同一件、持有人与佩戴"
            "位置对不对;②画面可读文字是否只出现白名单内容,有无乱码、"
            "字幕条、水印;③物理与空间关系(重力、支撑、接触、穿模、"
            "前后遮挡、站位与空间图是否一致);④服装妆发与本镜锁定造型"
            "是否一致;⑤景别、机位、构图是否落在合同规定档位;"
            "⑥场景陈设与光线方向是否与场景母版连续"),
    },
}


def build_qc_prompt(payload):
    characters = payload.get("characters") or []
    functional_figures = [
        dict(item) for item in (payload.get("functional_figures") or [])
        if isinstance(item, dict)
        and type(item.get("count")) is int and item.get("count") > 0
    ]
    functional_count = sum(
        item["count"] for item in functional_figures)
    try:
        expected_real_people = int(payload.get(
            "count", len(characters) + functional_count))
    except (TypeError, ValueError):
        expected_real_people = len(characters) + functional_count
    functional_line = "、".join(
        f"{item.get('name') or item.get('label') or '功能人物'}"
        f"{item['count']}人"
        + (f"({item.get('state') or item.get('function')})"
           if item.get("state") or item.get("function") else "")
        for item in functional_figures
    ) or "无"
    identity_refs = payload.get("identity_references") or []
    generation = payload.get("generation_input")
    generation = generation if isinstance(generation, dict) else {}
    generation_prompt = (
        generation.get("prompt")
        or payload.get("generation_prompt")
        or payload.get("prompt_used")
        or payload.get("prompt_compact")
        or "")
    generation_references = (
        generation.get("reference_manifest")
        or payload.get("reference_manifest")
        or [])
    generation_scope = generation.get("scope") or payload.get("scope") or {
        "item_id": payload.get("item_id", ""),
        "shot_no": payload.get("shot_no"),
        "frame_kind": payload.get("frame_kind", "keyframe"),
    }
    ref_lines = "\n".join(
        f"- {ref.get('character', '角色')}"
        f"({ref.get('reference_view') or 'final_portrait'}): "
        f"{ref.get('uri', '')}"
        for ref in identity_refs if isinstance(ref, dict))
    composition = payload.get("composition_contract") or {}
    actor_rules = "；".join(
        f"{item.get('character')}={item.get('role')}/"
        f"{item.get('expected_view')}/按{item.get('identity_basis')}核验"
        for item in composition.get("actors") or []
        if isinstance(item, dict))
    quality = composition.get("quality_requirements") or {}
    quality_line = (
        f"质检必须满足={'；'.join(quality.get('required') or [])};"
        f"质检禁止={'；'.join(quality.get('forbidden') or [])}"
        if isinstance(quality, dict) else "")
    composition_visible = composition.get("expected_visible_figure_count")
    if composition_visible is None:
        composition_visible = expected_real_people
    composition_rules = (
        f"类型={composition.get('composition_type')};"
        f"正面主体={composition.get('expected_primary_count')};"
        f"实际可见真人={composition_visible};实际可见人形={composition_visible};"
        f"{actor_rules};{composition.get('count_rule', '')};{quality_line}"
        if composition else "标准构图；按待检图实际可见视角逐人选择核验项")
    count_rule = (
        "本图为同一角色的多视角/局部设定图(四视图/特写/服装细节等):"
        "画面中出现的每个人形、头像或局部都必须是该角色同一人,"
        "人数不按出场人数核对"
        if payload.get("multi_view")
        else (
            f"画面可见真人严格共 {expected_real_people} 人，其中登记角色"
            f"共 {len(characters)} 个、即{len(characters)}人"
            f"（{'、'.join(characters) or '无'}），"
            f"功能人物{functional_count}人（{functional_line}）；"
            "身份逐人核验只覆盖登记角色，功能人物只核对数量、状态和剧情功能；"
            + (composition.get("count_rule") or "每个人物只计一次")))
    physical = payload.get("physical_contract") or {}
    physical_rules = "；".join(physical.get("rules") or [])
    physical_objects = "；".join(physical.get("objects") or [])
    physical_text = (
        ("必须执行硬检查；" if payload.get("physical_logic_required") else "仅作辅助检查；")
        + (physical_rules or "人物、镜头、道具关系按当前镜头实际构图核对")
        + (f"；对象关系：{physical_objects}" if physical_objects else ""))
    # 尺度判级:远景里物理上分辨不出刀刃缺口——按景别分级核验,
    # 低于可辨尺度的微细节不得作为 FAIL 理由(由近景镜头承担)。
    camera_text = str(
        (payload.get("camera") if not isinstance(
            payload.get("camera"), dict)
         else "·".join(str(v) for v in payload["camera"].values() if v))
        or generation.get("camera") or "")
    if any(t in camera_text for t in ("远景", "全景")):
        scale_tier = (
            "本镜为远景/全景:只核对人数、身份大轮廓(发型剪影/体型/"
            "性别)、服装大形制与色系、道具在场性与持有人;刃口缺口、"
            "指腹墨渍、织物纹理、饰品细纹、印文等微细节低于本景别"
            "可辨尺度,一律不得作为不合格理由,由近景镜头承担核验")
    elif any(t in camera_text for t in ("中景", "膝上", "半身")):
        scale_tier = (
            "本镜为中景:核对至服装结构、道具形态与佩戴位置;"
            "刃口缺口、指渍、织物纹理、印文等微细节不得作为"
            "不合格理由,由近景/特写镜头承担核验")
    else:
        scale_tier = (
            "本镜为近景/特写档:识别性微细节(如道具的刃口缺口、"
            "标志性磨损、印文)在可见面内必须核验一致")
    physical_text += f"；【尺度判级】{scale_tier}"
    staging = payload.get("spatial_staging")
    if isinstance(staging, dict) and staging:
        # 3D 空间调度的文字合同与出图提示词同源:屏幕定位、遮挡顺序、
        # 行动路线、屏幕左右关系都是可逐条比对的判据,比"看示意图"硬。
        physical_text += "；【空间调度核验(与提示词同源)】" + "；".join(
            f"{label}:{text}" for label, text in staging.items() if text)
    lens = str(payload.get("review_lens") or "").strip().lower()
    if lens in REVIEW_LENSES:
        # 双路会诊的分工:两路都看全图、都给整体判定,但各自深查一半。
        # 单个判官扛二十项清单必然前详后略(台账里 other 桶最大就是
        # 证据);拆成两份重点清单,同样成本换更细的核验。
        physical_text += (
            f"；【本次重点核查(第{REVIEW_LENSES[lens]['index']}路)】"
            + REVIEW_LENSES[lens]["focus"]
            + "。以上维度必须逐条深查并给出结论;其余维度仍要通扫,"
            "一旦看到明显问题照常写进 issues(由另一路负责深查,"
            "但你不得因此放过肉眼可见的错误)")
    overlays = [
        item for item in (payload.get("narrative_overlays") or [])
        if isinstance(item, dict)
    ][:1]
    if overlays:
        overlay = overlays[0]
        overlay_rules = (
            "本镜另有 1 个非现实内心Q版叠层："
            f"{overlay.get('name') or '内心Q版'}，宿主="
            f"{overlay.get('host_character') or '宿主'}。人数核验时 detected_count "
            "只统计真实人物，不把Q版计入；另用 detected_overlay_count 单独统计Q版，"
            "必须恰好1个。Q版不得被画成真人实体、不得进入真实站位或遮挡、不得被"
            "其他人物看见/回应/触碰；应继承当前锁定衣着、无默认道具，并以夸张"
            "Q版表情动作表现；比例约1.8头身，头占总高约58%，身体与四肢明显"
            "小于头。若有内心声音，真人宿主必须闭口且不得出现旁白字幕。")
    else:
        overlay_rules = (
            "本镜不允许非现实内心Q版叠层；detected_overlay_count 必须为0，"
            "不得擅自新增Q版、分身、幽灵或意识小人。")
    # 相邻帧对比判据。QC 早就随请求收到 role=continuity 的对照图
    # (同镜首帧/上一镜尾帧),但判据里从没要求对照它查场景——于是
    # 单帧重画后凭空多出灯笼、书架换款、纱幕变厚帘、色温整体偏橙
    # 这类漂移(2026-07-29 实测)全靠人眼兜底。有对照图就必须机器拦。
    continuity_labels = [
        str(entry.get("label") or "对照帧")
        for entry in (generation_references or [])
        if isinstance(entry, dict)
        and str(entry.get("role") or "") == "continuity"]
    if continuity_labels:
        scene_continuity = (
            "以参考图中的「" + "」「".join(continuity_labels[:3]) + "」为"
            "同场对照基准，逐项核对:陈设的种类与数量、家具款式、门窗/"
            "帷幔/墙面材质、光线方向与色温。画面出现对照图中不存在的"
            "新陈设(灯笼、额外家具、多出的器物)、既有陈设换款式、或"
            "色温明显偏移(变橙/变冷/变暗)，属于场景连续性错误，"
            "visual_pass 必须为 false 并在 issues 里写明是哪一件；"
            "人物动作与视线按本镜合同变化，不参与本项对比。")
    else:
        scene_continuity = "本镜无同场对照帧，跳过本项。"
    return IMAGE_QC_PROMPT.format(
        scene_continuity=scene_continuity,
        image=payload.get("image_uri", ""),
        generation_scope=json.dumps(
            generation_scope, ensure_ascii=False, separators=(",", ":")),
        generation_prompt=str(generation_prompt or "")[:12000],
        generation_references=json.dumps(
            generation_references, ensure_ascii=False,
            separators=(",", ":"))[:16000],
        identity_references=(ref_lines or "无人空镜，无需人物身份比对"),
        composition_rules=composition_rules,
        overlay_rules=overlay_rules,
        characters="、".join(characters) or "无人(空镜)",
        count_rule=count_rule,
        designs=payload.get("designs") or "见参考图",
        expected_genders=("；".join(
            f"{name}={gender}" for name, gender in
            (payload.get("expected_genders") or {}).items())
            or "以最终立绘与人物设定为准"),
        expected_wardrobe=("；".join(
            f"{name}={wardrobe}" for name, wardrobe in
            (payload.get("expected_wardrobe") or {}).items())
            or "本镜未声明服装状态，仅按可见剧情事实检查"),
        location=payload.get("location") or "按提示词",
        action=payload.get("action") or "按提示词",
        camera=payload.get("camera") or "按提示词",
        physical_contract=physical_text,
        extra=("、" + "、".join(payload.get("forbid", []))
               if payload.get("forbid") else "")
        + (("。剧情允许跨时代出现(穿越/带入物,时代判断以剧本为准,"
            "这些物品出现是正确的,禁止当成时代错乱判失败):"
            + "、".join(payload["era_exceptions"]))
           if payload.get("era_exceptions") else ""))


def validate_image_qc(data):
    if not isinstance(data, dict) or "pass" not in data:
        return "缺少 pass 字段"

    def strict_bool(key, *, required=False):
        if key not in data:
            return f"缺少 {key} 字段" if required else None
        if type(data[key]) is not bool:
            return f"{key} 必须是真正的 JSON 布尔值，不能是字符串或数字"
        return None

    boolean_fields = (
        "pass", "visual_pass", "input_contract_pass",
        "identity_checked", "identity_match",
        "gender_checked", "gender_match",
        "wardrobe_checked", "wardrobe_match",
        "count_checked", "count_match",
        "physical_logic_checked", "physical_logic_match",
        "spatial_logic_checked", "spatial_logic_match",
        "overlay_count_checked", "overlay_count_match",
    )
    for key in boolean_fields:
        error = strict_bool(key, required=(key == "pass"))
        if error:
            return error
    for key in ("visual_pass", "input_contract_pass"):
        if key in data and type(data[key]) is not bool:
            return f"{key} 必须是真正的 JSON 布尔值"
    issues = data.get("issues")
    if not isinstance(issues, list):
        issues = [str(issues)] if issues else []
    data["issues"] = [str(item) for item in issues][:8]
    if "detected_count" in data:
        try:
            data["detected_count"] = int(data["detected_count"])
        except (TypeError, ValueError):
            data.pop("detected_count", None)
    if "detected_overlay_count" in data:
        try:
            data["detected_overlay_count"] = int(
                data["detected_overlay_count"])
        except (TypeError, ValueError):
            data.pop("detected_overlay_count", None)
    checks = data.get("identity_checks")
    if isinstance(checks, list):
        normalized = []
        for item in checks[:16]:
            if not isinstance(item, dict) or not item.get("character"):
                continue
            row = dict(item)
            row["character"] = str(row["character"])
            row["view"] = str(row.get("view") or "")
            if (type(row.get("checked")) is not bool
                    or type(row.get("match")) is not bool):
                return (
                    "identity_checks 的 checked/match 必须是真正的 JSON "
                    "布尔值")
            basis = row.get("basis")
            row["basis"] = (
                [str(value) for value in basis[:12]]
                if isinstance(basis, list) else [])
            normalized.append(row)
        data["identity_checks"] = normalized
    elif checks is not None:
        data["identity_checks"] = []
    if not data["pass"] and not data["issues"]:
        data["issues"] = ["未给出具体原因"]
    data.update(normalize_generation_diagnostics(
        data, issues=data.get("issues")))
    return None


CHARACTER_REFINE_PROMPT = """你是本剧的人物造型总监。用户对角色「{name}」的当前形象提出修改意见,
你要深度理解意图,改写这个角色的人物形象提示词(设定卡)。

当前形象提示词:
{current}

角色背景与身份事实(不可违背):
{context}

项目画风(不可改变):{style}

用户意见(必须逐条落实):{feedback}

要求:
- 深度理解意见的真实意图;意见含糊时按最合理的造型逻辑具体化
  (如"头发长点"要落成明确的长度、层次与轮廓,"皮肤更白点"要落成
  具体肤色基调与质感,不能只是把原话抄进提示词);
- 未被意见涉及的部分(骨相、五官、年龄感、身份特征、画风)一字不改保留;
- 若某条意见与身份事实或项目画风冲突,不要硬改,在 conflict_notes
  里说明冲突并给出最接近用户意图的可行方案;
- image_prompt 必须是完整、独立、可直接生图的人物形象提示词,
  不是增量补丁;
- 只输出一个 JSON 对象,不要任何其他文字或 Markdown 代码块:
{{"image_prompt": "改写后的完整人物形象提示词",
 "changes": ["逐条说明改了什么,对应用户哪条意见"],
 "conflict_notes": ["与设定/画风冲突的意见及处理方式;没有则给空数组"]}}
"""


def validate_prompt_refine(data):
    """人物形象提示词改写输出的最小校验。"""
    if not isinstance(data, dict):
        return "输出必须是 JSON 对象"
    prompt = str(data.get("image_prompt") or "").strip()
    if len(prompt) < 20:
        return "缺少 image_prompt 或内容过短"
    for key in ("changes", "conflict_notes"):
        value = data.get(key)
        if value is None:
            data[key] = []
        elif not isinstance(value, list):
            return f"{key} 必须是数组"
    if not data["changes"]:
        return "changes 必须逐条说明改动,不能为空"
    return None


ASSET_PROMPT_PROMPT = """你是 AI 漫剧的美术资产总监。用户在资产工坊里要生产一张
自建资产图片,请你把用户的一句话需求写成一条完整、可直接交给图片模型执行的出图提示词。

资产类型:{asset_type_label}
资产名称:{name}
用户需求(必须逐条落实):{brief}
当前提示词(为空表示从零撰写):
{current}
用户补充意见(为空表示无):{feedback}
参考图(文件路径;为空表示无):
{references}
项目画风(为空表示由你按需求判断,不套用无关默认画风):{style}

要求:
- 只按用户需求写,不发明剧情、不硬套任何剧集设定;需求没提到的部分按该资产
  类型的常规美术逻辑补全到可执行程度,不留"自行发挥"式空话;
- 含糊表述必须具体化(如"帅一点"要落成明确的骨相、五官、气质与光影,
  "复古风"要落成明确的年代、媒介、色调与材质),不能把原话抄进提示词;
- 人物类:必须写清性别表达、年龄感、骨相五官、发型发色、妆容、体态气质、
  服装与配饰、背景处理;人物立绘默认纯净无场景背景;
- 风格类:必须写清媒介与笔触、色调与配色、光影、材质质感、构图特征,
  不要绑定具体人物身份;
- 场景类:必须写清空间类型、时代地域、结构与陈设、材质、光源与时间、
  视角与景别,画面中不出现人物;
- 物品类:必须写清形制、相对尺寸、结构、材质工艺、磨损与识别细节,
  单体展示,不画人物和场景故事;
- 有参考图时,写明每张参考图各自的职责(身份/服装/场景/构图/画风),
  不得让一张参考图越权覆盖其他维度;
- image_prompt 必须是完整独立的出图提示词,不是增量补丁,不含
  Markdown、编号列表标题或解释性文字;
- 只输出一个 JSON 对象,不要任何其他文字或 Markdown 代码块:
{{"image_prompt": "完整的出图提示词",
 "negative_prompt": "不希望出现的内容;没有则给空字符串",
 "notes": ["把用户哪条需求落成了什么具体画面事实"]}}
"""


def validate_asset_prompt(data):
    """资产工坊 AI 提示词输出的最小校验。"""
    if not isinstance(data, dict):
        return "输出必须是 JSON 对象"
    prompt = str(data.get("image_prompt") or "").strip()
    if len(prompt) < 20:
        return "缺少 image_prompt 或内容过短"
    if "```" in prompt:
        return "image_prompt 含 Markdown 代码围栏"
    if data.get("negative_prompt") is None:
        data["negative_prompt"] = ""
    elif not isinstance(data["negative_prompt"], str):
        return "negative_prompt 必须是字符串"
    notes = data.get("notes")
    if notes is None:
        data["notes"] = []
    elif not isinstance(notes, list):
        return "notes 必须是数组"
    return None


# AI 导演准则:唯一事实源——提示词与制作标准中心的展示共用这一份,
# 保证"标准里看到的"就是"运行时执行的"。
DIRECTOR_PRINCIPLES = (
    "景别必须装得下必须可见的人数(容量:大特写/特写=1人,近景/中近景=2人,"
    "七分身=3人,中景/膝上景=4人,全景以上不限)——绝不给多人镜头开特写",
    "与前后镜头形成节奏:连续三镜同景别是流水账,该切就切;对话正反打"
    "遵守轴线,两人左右关系不得翻转",
    "构图为叙事服务:压迫用俯拍/框中框,力量用低角度,窥视用前景遮挡,"
    "抒情用留白——不是为了花哨",
    "光影按题材基调选,画面内有实体光源(灯笼/烛火/窗光)时优先实用光源主导",
    "意见只写画面可核验的要求(亮度档位/视线方向/道具反光……),不写空话",
    "不改剧情:人物、动作、道具、生死状态一个都不许动",
    "现状已经很好时,允许少改甚至只给意见——改动越少越好,每一项改动都要"
    "在 rationale 里说出为什么",
)

AI_DIRECTOR_PROMPT = """你是本剧的 AI 导演。你精通镜头语言(景别/角度/机位/运镜/构图)、
布光语法与题材视听基调,现在要对单个镜头给出专业导演处理——目的只有一个:
让这一镜更好看、更会讲故事、更符合本剧基调,同时严格尊重剧情事实。

本剧题材与画风:{genre}
题材视听基调:{genre_grammar}

本镜事实(不可改变的剧情内容):
{shot_facts}

前后镜头(节奏与轴线参考):
{context_shots}

空间调度事实(人物站位/遮挡/朝向,若有):
{spatial}

质检指出的问题(若有,导演处理应能化解它们):
{qc_issues}

可用词汇(只能从这些词条里选,不得发明新词):
{vocabulary}

导演准则:
{principles}

只输出一个 JSON 对象,不要任何其他文字或 Markdown 代码块:
{{"schema": "aifos.ai_director.v1",
 "camera": {{"景别": "留空表示不改", "角度": "", "机位": "", "运镜": "",
             "构图": ""}},
 "lighting_style": "7种风格key之一/auto/留空不改",
 "note": "给生成模型的导演意见,可核验、≤200字;没有就留空",
 "rationale": "用人话向剧组解释这样处理的理由(≤200字)"}}
"""


def validate_ai_director(data, payload):
    """AI 导演输出校验:词条必须在词典内、景别必须装得下人数。"""
    from ..camera_language import (ANGLE_GEOMETRY, COMPOSITION_GEOMETRY,
                                   MOVEMENT_GEOMETRY, POSITION_GEOMETRY,
                                   SCALE_GEOMETRY, scale_capacity)
    from ..lighting_language import LIGHTING_STYLES
    if not isinstance(data, dict):
        return "输出必须是 JSON 对象"
    if data.get("schema") != "aifos.ai_director.v1":
        return "schema 必须是 aifos.ai_director.v1"
    tables = {"景别": SCALE_GEOMETRY, "角度": ANGLE_GEOMETRY,
              "机位": POSITION_GEOMETRY, "运镜": MOVEMENT_GEOMETRY,
              "构图": COMPOSITION_GEOMETRY}
    camera = data.get("camera")
    camera = camera if isinstance(camera, dict) else {}
    cleaned = {}
    for dim, value in camera.items():
        value = str(value or "").strip()
        if not value:
            continue
        table = tables.get(str(dim).strip())
        if table is None:
            return f"未知镜头维度「{dim}」"
        if value not in table:
            return (f"「{value}」不在{dim}词典里,只能从可用词汇中选")
        cleaned[str(dim).strip()] = value
    scale = cleaned.get("景别")
    if scale:
        try:
            count = int((payload or {}).get("visible_count") or 0)
        except (TypeError, ValueError):
            count = 0
        if count > 0 and scale_capacity(scale) < count:
            return (f"景别{scale}装不下本镜必须可见的{count}人——"
                    "导演不能违反容量物理")
    data["camera"] = cleaned
    lighting = str(data.get("lighting_style") or "").strip()
    if lighting and lighting != "auto" and lighting not in LIGHTING_STYLES:
        return f"未知光影风格「{lighting}」"
    data["lighting_style"] = lighting
    note = str(data.get("note") or "").strip()
    if len(note) > 220:
        return "导演意见过长(>200字),必须精炼"
    data["note"] = note
    rationale = str(data.get("rationale") or "").strip()
    if len(rationale) < 8:
        return "缺少 rationale:必须向剧组说明处理理由"
    data["rationale"] = rationale[:300]
    if not cleaned and not lighting and not note:
        return "镜头/光影/意见全为空:至少给一项处理,或在意见里说明现状已最优"
    return None


RULE_APPEAL_PROMPT = """你是本剧生产平台的规则仲裁官(终审法庭)。平台的内置校验规则做初审,
它是死的字面规则;你要判断这次判败到底是**真违规**,还是**规则字面化误杀 /
剧情本来就需要这样**。

被判败的规则:{rule_id}
规则给出的判败理由:
{rule_reason}

被检查的实际内容(判决对象):
{subject}

剧情与镜头上下文(事实源):
{context}

平台冲突裁决规则(优先级宪法,你的判决必须与它一致):
{adjudication}

判决准则:
- 只要事实**实质存在**,写法不同不算违规:数字区间「25-30」等同「25至30岁」;
  「共7名可见真人:3名登记角色加4名弓兵」等同「7人」;繁简异体同字;
  同义表述(单人/一名角色/同一角色)等同;
- 规则要求的东西在本镜**物理上不可能或不该出现**(景别太远看不见、被裁切、
  被背向遮挡、剧情要求该物不在场),不算违规;
- 剧本/镜头合同明确要求的剧情安排,不因与通用默认规则不符而判违规;
- 反过来:事实**真的被删改**(人数变了、角色被合并、道具消失、锁定文字被改),
  必须维持原判——这类放行会直接产出废图,后果比误杀更严重;
- 不确定时维持原判(宁可多修一次,不可放行错图)。

举证要求(硬性):撤销原判必须在 evidence 里**逐字引用**被检查内容中
证明事实存在的原文片段;引用不出来就必须维持原判。

只输出一个 JSON 对象,不要任何其他文字或 Markdown 代码块:
{{"schema": "aifos.rule_appeal.v1",
 "verdict": "upheld 或 overturned",
 "reason": "一句话说明判决依据",
 "evidence": "撤销时逐字引用的原文片段;维持原判时写空字符串",
 "suggested_rule_fix": "若判定为规则字面化误杀,用一句话说明规则该怎么改才不再误杀;否则空字符串"}}
"""


def validate_rule_appeal(data, payload=None):
    """规则仲裁输出的最小校验:撤销原判必须举证。"""
    if not isinstance(data, dict):
        return "输出必须是 JSON 对象"
    if data.get("schema") != "aifos.rule_appeal.v1":
        return "schema 必须是 aifos.rule_appeal.v1"
    verdict = str(data.get("verdict") or "").strip().lower()
    if verdict not in {"upheld", "overturned"}:
        return "verdict 只能是 upheld 或 overturned"
    data["verdict"] = verdict
    if not str(data.get("reason") or "").strip():
        return "缺少 reason"
    evidence = str(data.get("evidence") or "").strip()
    if verdict == "overturned":
        if len(evidence) < 4:
            return "撤销原判必须在 evidence 逐字引用证明事实存在的原文"
        subject = str((payload or {}).get("subject") or "")
        if subject and evidence not in subject:
            # 允许仲裁做最小裁剪(去引号/省略号),但主干必须真的在原文里
            core = evidence.strip("「」\"'．. …").strip()
            if not core or core not in subject:
                return "evidence 不是被检查内容中的原文,不能作为撤销依据"
    for key in ("reason", "evidence", "suggested_rule_fix"):
        data[key] = str(data.get(key) or "").strip()
    return None


LESSON_DISTILL_PROMPT = """你是本剧生产平台的质量总结官。下面是本项目真实质检中反复出现的
出错观察(原始记录,按出现次数排序)。你要把它们归纳成少量**可复用的通用规则**,
供后续所有镜头的提示词自查。

原始出错观察:
{observations}

归纳要求(铁律):
- 只归纳**跨镜头普遍适用**的规则;单个镜头的一次性事实(某人某处的伤口、
  某张纸上的字)不要写成规则;
- 每条规则必须是**可执行、可核验的正向要求或明确禁止**,不能是"注意质量"
  这类空话;
- **不得出现具体角色名、道具名、场次或镜头号**——规则要对新镜头同样成立;
- 同类观察合并成一条;最多输出 {max_rules} 条,宁缺毋滥;
- 每条不超过 40 字。

只输出一个 JSON 对象,不要任何其他文字或 Markdown 代码块:
{{"schema": "aifos.lesson_distill.v1",
 "rules": [{{"rule": "通用规则原文", "covers": ["被它覆盖的原始观察序号"]}}]}}
"""


def validate_lesson_distill(data, payload=None):
    """教训归纳输出校验:必须是通用规则,不能夹带镜头级专名。"""
    if not isinstance(data, dict):
        return "输出必须是 JSON 对象"
    if data.get("schema") != "aifos.lesson_distill.v1":
        return "schema 必须是 aifos.lesson_distill.v1"
    rules = data.get("rules")
    if not isinstance(rules, list):
        return "rules 必须是数组"
    forbidden = [str(name) for name in
                 ((payload or {}).get("proper_nouns") or []) if name]
    cleaned = []
    for item in rules:
        if isinstance(item, str):
            item = {"rule": item, "covers": []}
        if not isinstance(item, dict):
            return "rules 成员必须是对象或字符串"
        text = str(item.get("rule") or "").strip()
        if len(text) < 6:
            return "存在过短或空的规则"
        if len(text) > 60:
            return f"规则过长(>60字),必须精炼: {text[:30]}…"
        hit = next((name for name in forbidden if name and name in text), "")
        if hit:
            return f"规则里出现了镜头级专名「{hit}」,必须改写成通用表述"
        cleaned.append({"rule": text,
                        "covers": list(item.get("covers") or [])})
    if not cleaned:
        return "rules 为空:至少归纳出一条,或说明确无可复用规则"
    data["rules"] = cleaned
    return None


PROP_DESIGN_PROMPT = """你是本剧的道具设计总监。以下核心/身份识别道具已进入剧本的道具登记表,
但还没有可出图的设计卡。你要只依据正式剧本中的事实与合理工艺推导,为每件道具补全设计卡。

正式剧本(唯一事实源):
{script}

项目画风:{style}

待补卡道具(名称与登记信息):
{props}

铁律:
- 用途、持有人、出场方式只能取自剧本(如剧本写「握紧包裹绳结」,可推导出
  绳结封口、可单手提携;不得发明剧本没有的剧情或功能);
- visual_design 必须写全:整体形制、尺寸级别(相对人体,如可单手提/斜挎/
  怀揣)、可握持或可使用的具体结构、材质工艺、磨损与识别细节;
- era_material 必须符合剧本时代与持有人阶层,不得出现超时代工艺;
- 不确定的装饰细节按持有人身份取最朴素的合理设计,不添加剧情性文字或印记。

只输出一个 JSON 对象,不要任何其他文字或 Markdown 代码块:
{{"schema": "aifos.prop_design.v1",
 "props": [{{"name": "道具名(与登记表逐字一致)",
   "story_function": "剧情功能与用途(源自剧本)",
   "visual_design": "形制+尺寸级别+可握持/使用结构+材质+磨损识别细节",
   "era_material": "时代与材质工艺",
   "owner": "归属/持有人"}}]}}
"""


def validate_prop_design(data, payload):
    """道具设计卡补全输出的最小校验:每件待补卡道具都要有可执行事实。"""
    if not isinstance(data, dict):
        return "输出必须是 JSON 对象"
    if data.get("schema") != "aifos.prop_design.v1":
        return "schema 必须是 aifos.prop_design.v1"
    cards = data.get("props")
    if not isinstance(cards, list) or not cards:
        return "props 必须是非空数组"
    by_name = {}
    for card in cards:
        if not isinstance(card, dict):
            return "props 成员必须是对象"
        name = str(card.get("name") or "").strip()
        if not name:
            return "设计卡缺少 name"
        for field in ("story_function", "visual_design"):
            if len(str(card.get(field) or "").strip()) < 8:
                return f"道具「{name}」的 {field} 缺失或过于敷衍"
        by_name[name] = card
    requested = [
        str(item.get("name") if isinstance(item, dict) else item).strip()
        for item in (payload or {}).get("props") or []]
    missing = [name for name in requested if name and name not in by_name]
    if missing:
        return "以下待补卡道具没有输出设计卡: " + "、".join(missing)
    return None


SHOT_REPAIR_PROMPT = """你是本剧的分镜导演。镜头{shot_no}的提示词在进入图片生成前被审核熔断,
原因是镜头自身的几何/构图要求互斥(例如特写景别却要求多名相距很远的人物
与多个空间区域同时入框)。你要就地修复这个镜头,让它几何上自洽。

被熔断的镜头(分镜原文):
{shot}

场景地点:{location}
项目画风:{style}

审核熔断原因(必须逐条化解):
{blocking_reason}

修复边界(铁律):
- 只允许修改 camera(景别/机位/构图方式)和 description 中的取景表述;
- 景别必须能真实容纳镜头宣称的全部可见人物与画面区域;若剧情本意是
  只看局部,则在 description 里明确写出只框入哪些人物/道具、其余出画;
- 不得增删人物、道具,不得改变任何人物状态/朝向/位置等剧情事实,
  不得改动对白、时长、事件(event_id/phase)、prop_registry;
- camera 保持与原镜头相同的结构:原来是字符串就输出字符串,
  原来是对象就输出同结构的完整对象。

只输出一个 JSON 对象,不要任何其他文字或 Markdown 代码块:
{{"schema": "aifos.shot_repair.v1",
 "shot_no": {shot_no},
 "camera": "修复后的镜头(与原结构一致)",
 "description": "修复后的完整镜头描述;不需要改时省略此键",
 "repair_summary": "一句话说明改了什么、如何化解每条熔断原因"}}
"""


def validate_shot_repair(data, payload):
    """审核熔断镜头就地修复输出的最小校验。"""
    if not isinstance(data, dict):
        return "输出必须是 JSON 对象"
    if data.get("schema") != "aifos.shot_repair.v1":
        return "schema 必须是 aifos.shot_repair.v1"
    shot = (payload or {}).get("shot") or {}
    try:
        if int(data.get("shot_no")) != int(shot.get("shot_no")):
            return "shot_no 与被修复镜头不一致"
    except (TypeError, ValueError):
        return "shot_no 非法"
    camera = data.get("camera")
    original = shot.get("camera")
    if isinstance(original, dict):
        if not isinstance(camera, dict) or not camera:
            return "原镜头 camera 是对象,修复输出必须保持同结构对象"
    elif not str(camera or "").strip():
        return "camera 缺失或为空"
    description = data.get("description")
    if description is not None and not str(description).strip():
        return "description 若输出则不能为空"
    if not str(data.get("repair_summary") or "").strip():
        return "缺少 repair_summary"
    return None


SCENE_ANNOTATE_PROMPT = """你在读一张 720° 等距圆柱全景图(equirectangular),
它是场景「{location}」的几何真相。请标出画面里每一件**落在地面上的实体**。

关键:你**不需要估计距离**。平台会用「物体底部在图里的位置」做地面射线
求交,精确解出它的三维坐标——你只要把接触点标准。距离猜测反而会引入误差。

坐标约定:
- base_u ∈ [0,1):物体与地面接触处的**水平**位置,自图左缘起的比例。
  图的水平中心 u=0.5 是场景正前方。
- base_v ∈ [0,1]:同一接触点的**纵向**位置,自图顶起的比例。必须落在
  物体与地板真正相交的那条线上(不是物体中心,不是顶部)。
- top_v:该物体最高点的纵向比例(用于解高度)。看不清可省略。
- width_u:该物体在图里占的水平宽度比例(用于解宽度)。看不清可省略。
- depth_m:结合房间尺度判断的实体前后深度(米)。桌椅柜架、可移动道具
  必须给出；门窗、帷幔等薄面给真实厚度，禁止把宽度重复当深度。
- rotation_y_deg:物体绕竖直轴的朝向，0°表示宽边沿世界X轴，
  90°表示宽边沿世界-Z轴；按全景中的边线/墙线判断。看不清可省略，
  平台会记录为估计值而不会冒充测量值。

只标真实存在于画面里的东西。不要补画面上没有的家具,不要把墙面纹理、
光斑、影子当成物体。人物不要标(场景是空镜)。

category 取值:furniture(桌椅柜架床)、prop(炉瓶书卷等可移动物件)、
opening(门窗)、light(灯具)、decor(帷幔挂画盆栽)。

严格输出 JSON:
{{"objects":[{{"name":"物体中文名","category":"furniture",
  "base_u":0.5,"base_v":0.78,"top_v":0.55,"width_u":0.12,
  "depth_m":0.8,"rotation_y_deg":0,
  "note":"用于定位的接触点描述"}}]}}
只输出 JSON,不要额外说明。
"""


def validate_scene_annotation(data):
    """场景标注结构校验:只收能解出落地点的条目。"""
    if not isinstance(data, dict):
        return "场景标注必须是 JSON 对象"
    objects = data.get("objects")
    if not isinstance(objects, list) or not objects:
        return "场景标注缺少 objects 数组"
    for index, item in enumerate(objects, 1):
        if not isinstance(item, dict):
            return f"objects[{index}] 必须是对象"
        if not str(item.get("name") or "").strip():
            return f"objects[{index}] 缺少 name"
        for key in ("base_u", "base_v"):
            try:
                value = float(item[key])
            except (KeyError, TypeError, ValueError):
                return f"objects[{index}] 的 {key} 缺失或非数字"
            if not 0.0 <= value <= 1.0:
                return f"objects[{index}] 的 {key} 必须在 0~1 之间"
        for key in ("depth_m", "rotation_y_deg"):
            if item.get(key) is None:
                continue
            try:
                value = float(item[key])
            except (TypeError, ValueError):
                return f"objects[{index}] 的 {key} 必须是数字"
            if key == "depth_m" and not 0.02 <= value <= 20.0:
                return f"objects[{index}] 的 depth_m 必须在 0.02~20 米之间"
    return ""


def build_prompt(capability, payload):
    """构造编剧/分镜/人物设定提示词(CLI 桥与 Claude API Provider 共用)。

    尾部挂知识大脑指令:由 ProviderRouter 在派发前写进
    payload["knowledge_directives"](见 aifos/knowledge_apply.py)。适配器
    是子进程,自己没有 db 句柄,只负责渲染,不负责检索。
    """
    prompt = _build_prompt_body(capability, payload)
    directives = str((payload or {}).get("knowledge_directives") or "").strip()
    if directives:
        prompt = f"{prompt}\n\n{directives}"
    return prompt


def _build_prompt_body(capability, payload):
    if capability == "script" and payload.get("prompt_refine"):
        return CHARACTER_REFINE_PROMPT.format(
            name=payload.get("character_name", ""),
            current=(payload.get("current_prompt")
                     or "(尚无成形提示词,请依据角色背景从零撰写)"),
            context=json.dumps(
                payload.get("character_context") or {},
                ensure_ascii=False),
            style=(payload.get("style")
                   or "项目画风未指定;保持与已生成候选一致"),
            feedback=payload.get("feedback", ""))
    if capability == "script" and payload.get("asset_prompt"):
        references = "\n".join(
            f"- {item}" for item in (payload.get("references") or []) if item)
        return ASSET_PROMPT_PROMPT.format(
            asset_type_label=(payload.get("asset_type_label")
                              or payload.get("asset_type") or "自建资产"),
            name=payload.get("asset_name", "") or "(用户未命名)",
            brief=payload.get("brief", "") or "(未填写,请按资产名称与类型撰写)",
            current=(payload.get("current_prompt")
                     or "(尚无提示词,请从零撰写)"),
            feedback=payload.get("feedback", "") or "(无)",
            references=references or "(无)",
            style=payload.get("style", "") or "(未指定)")
    if capability == "script" and payload.get("ai_director"):
        return AI_DIRECTOR_PROMPT.format(
            principles="\n".join(f"- {p};" for p in DIRECTOR_PRINCIPLES),
            genre=payload.get("genre", "") or "未指定",
            genre_grammar=payload.get("genre_grammar", "")
            or "无题材专属基调,按通用叙事语法",
            shot_facts=json.dumps(payload.get("shot_facts") or {},
                                  ensure_ascii=False)[:4000],
            context_shots=json.dumps(payload.get("context_shots") or [],
                                     ensure_ascii=False)[:2000],
            spatial=str(payload.get("spatial") or "无")[:1500],
            qc_issues="；".join(
                str(i) for i in (payload.get("qc_issues") or [])[:6])
            or "无",
            vocabulary=json.dumps(payload.get("vocabulary") or {},
                                  ensure_ascii=False))
    if capability == "script" and payload.get("rule_appeal"):
        return RULE_APPEAL_PROMPT.format(
            rule_id=payload.get("rule_id", ""),
            rule_reason=payload.get("rule_reason", ""),
            subject=str(payload.get("subject") or "")[:8000],
            context=json.dumps(payload.get("context") or {},
                               ensure_ascii=False)[:8000],
            adjudication=json.dumps(payload.get("adjudication") or {},
                                    ensure_ascii=False))
    if capability == "script" and payload.get("lesson_distill"):
        return LESSON_DISTILL_PROMPT.format(
            observations="\n".join(
                f"{index}. {item}" for index, item in enumerate(
                    payload.get("observations") or [], 1)),
            max_rules=int(payload.get("max_rules") or 8))
    if capability == "script" and payload.get("prop_design"):
        return PROP_DESIGN_PROMPT.format(
            script=json.dumps(payload.get("script") or {},
                              ensure_ascii=False),
            style=(payload.get("style")
                   or "未指定;按剧本时代与题材自动匹配"),
            props=json.dumps(payload.get("props") or [],
                             ensure_ascii=False))
    if capability == "script" and payload.get("shot_repair"):
        shot = payload.get("shot") or {}
        return SHOT_REPAIR_PROMPT.format(
            shot_no=shot.get("shot_no", 0),
            shot=json.dumps(shot, ensure_ascii=False),
            location=payload.get("location", "") or "见镜头描述",
            style=(payload.get("style")
                   or "项目画风未指定;保持与已生成画面一致"),
            blocking_reason=payload.get("blocking_reason", ""))
    if capability == "script" and payload.get("story_analysis"):
        return STORY_ANALYSIS_PROMPT.format(
            style=(payload.get("style")
                   or "未指定；依据剧本分析，不得套用无关默认画风"),
            direction=(payload.get("creative_direction")
                       or "无额外补充，以剧本事实为准"),
            analysis_rules=json.dumps(
                payload.get("analysis_rules") or {}, ensure_ascii=False),
            script=json.dumps(payload.get("script", {}),
                              ensure_ascii=False),
            schema=STORY_ANALYSIS_SCHEMA)
    if capability == "script" and payload.get("character_design"):
        names = "、".join(
            f"{c.get('name')}({c.get('role') or '角色'})"
            for c in payload.get("characters", []))
        ref_lines = []
        for c in payload.get("characters", []):
            for uri in c.get("reference_images") or []:
                ref_lines.append(f"- {c.get('name')}: {uri}")
        references = ""
        if ref_lines:
            references = (
                "\n角色参考图(文件路径,可直接查看):\n"
                + "\n".join(ref_lines)
                + "\n有参考图的角色必须先查看参考图,以图中人物的脸部特征"
                "(脸型/五官比例/眼鼻嘴/年龄感)、发型发色、妆容、气质和"
                "整体风格为最高标准逐项撰写 appearance/hair/eyes/makeup/"
                "signature/temperament 等字段,与参考图冲突的描述一律"
                "以参考图为准,不得凭空想象;脸和发型不得漂移;服装可按剧情另行设计。\n")
        return DESIGN_PROMPT.format(
            title=payload.get("project_title", ""),
            style=(payload.get("style", "")
                   or "未指定；以本集制作圣经和剧本世界自动分析结果为准"),
            logline=payload.get("logline", "") or "见角色名单",
            premise=payload.get("premise", "") or "见本集剧本",
            names=names,
            story_context=json.dumps(
                {
                    "story_world": payload.get("story_world") or {},
                    "story_background": payload.get("story_background") or {},
                    "characters": (payload.get("character_context")
                                   or payload.get("characters", [])),
                    "existing_cast_designs": (
                        payload.get("existing_cast_designs") or {}),
                },
                ensure_ascii=False),
            scene_context=json.dumps(payload.get("scene_context") or [],
                                     ensure_ascii=False),
            references=references)
    if capability == "script":
        feedback = payload.get("feedback", "")
        if payload.get("template") == "idol":
            prompt = IDOL_PROMPT.format(
                persona=payload.get("persona")
                or payload.get("project_title", ""),
                episode=payload.get("episode_number", 0),
                style=(payload.get("style", "")
                       or "未指定；按人设、主题和舞台语境自动设计"),
                premise=payload.get("premise", "") or "自选一个日常主题")
        else:
            prompt = SCRIPT_PROMPT.format(
                title=payload.get("project_title", ""),
                episode=payload.get("episode_number", 0),
                style=(payload.get("style", "")
                       or "未指定；根据剧本自动分析，不套用固定画风"),
                premise=payload.get("premise", "") or "自由发挥")
        if feedback:
            previous = json.dumps(
                payload.get("previous_script", {}), ensure_ascii=False)
            source_policy = (
                "这是导入的小说/剧本文本素材，不是已锁定正式剧本。"
                "允许为因果、物理、时间、空间、人物信息与道具生命周期重写"
                "动作和台词；保留核心人物、核心事件和关键结果。"
                if payload.get("source_material_adaptation") else
                "这是上一版已结构化剧本；按用户意见修改并保持未涉及内容稳定。")
            prompt = (
                f"这是上一版剧本:\n{previous}\n\n"
                f"用户的修改意见(必须逐条落实):{feedback}\n\n"
                f"{source_policy}\n请在保留可取之处的前提下按意见重写。{prompt}")
        lessons = str(payload.get("lessons") or "").strip()
        if lessons:
            prompt = f"{prompt}\n\n【经验库·严禁再犯】{lessons}"
        return prompt
    if capability == "storyboard":
        return STORYBOARD_PROMPT.format(
            script=json.dumps(payload.get("script", {}), ensure_ascii=False))
    if capability == "image_qc":
        return build_qc_prompt(payload)
    if capability == "scene_annotate":
        return SCENE_ANNOTATE_PROMPT.format(
            location=str(payload.get("location") or "该场景"))
    raise ValueError(f"claude 编剧不支持能力: {capability}")


# codex exec 作为备选编剧引擎:只读沙箱(编剧只产 JSON 文本,不需要
# 写文件权限)、不落会话文件、无彩色控制符;最终答复经
# --output-last-message 落盘,避免从进度日志里抠 JSON。
CODEX_WRITER_ARGS = ("exec", "--sandbox", "read-only",
                     "--skip-git-repo-check", "--ephemeral",
                     "--color", "never")


# 在途引擎子进程登记簿:适配器被父进程 TERM 收割时转发终止到整个
# 进程组。子进程用 start_new_session 起在独立会话里,不转发的话
# TERM 只杀适配器本体,claude/codex 引擎会以孤儿身份继续跑并烧额度。
_LIVE_GROUPS = set()


def _register_term_forwarding():
    """SIGTERM → 终止全部在途引擎进程组后退出(幂等注册)。"""

    def _forward(_signum, _frame):
        for proc in list(_LIVE_GROUPS):
            try:
                _terminate_group(proc, grace=3)
            except Exception:
                pass
        raise SystemExit(143)

    try:
        signal.signal(signal.SIGTERM, _forward)
    except (ValueError, OSError):
        pass  # 非主线程/受限环境:保持默认行为


def _terminate_group(proc, grace=5):
    """TERM→KILL 整个进程组,不留幽灵子进程。"""
    if proc.poll() is not None:
        return
    try:
        os.killpg(proc.pid, signal.SIGTERM)
    except (ProcessLookupError, PermissionError):
        return
    try:
        proc.wait(timeout=grace)
    except subprocess.TimeoutExpired:
        try:
            os.killpg(proc.pid, signal.SIGKILL)
        except (ProcessLookupError, PermissionError):
            pass
        proc.wait()


def _process_cpu_seconds(pid):
    """进程组累计 CPU 时间(秒);取不到返回 None。仅标准库。"""
    try:
        out = subprocess.run(
            ["ps", "-o", "time=", "-p", str(pid)],
            capture_output=True, text=True, timeout=5).stdout.strip()
    except (OSError, subprocess.SubprocessError):
        return None
    if not out:
        return None
    parts = out.replace("-", ":").split(":")
    try:
        seconds = float(parts[-1])
        for index, value in enumerate(reversed(parts[:-1]), start=1):
            seconds += float(value) * (60 ** index)
    except ValueError:
        return None
    return seconds


def _run_with_stall_watchdog(cmd, env, timeout, stall_timeout):
    """跑子进程并监控存活迹象:输出活性 + CPU 时间推进,双信号任一即算
    活着;两者同时静止超过 stall_timeout 才判定卡住。

    codex 在长思考期几乎不产出任何 stdout/stderr(实测 100s 任务里
    静默 99.3s),只看输出必然误杀正常长任务;CPU 时间能反映它仍在
    本地解码/处理。真卡死(网络断流后挂起)两个信号会同时归零。

    timeout<=0 表示不设总时长上限——活性检测取代硬超时。
    返回 (returncode, stdout, stderr, stalled)。
    """
    proc = subprocess.Popen(
        cmd, stdin=subprocess.DEVNULL,
        stdout=subprocess.PIPE, stderr=subprocess.PIPE,
        text=True, env=env, start_new_session=True)
    _LIVE_GROUPS.add(proc)
    chunks = {"out": [], "err": []}
    activity = {"at": time.monotonic()}

    def pump(stream, key):
        for line in iter(stream.readline, ""):
            chunks[key].append(line)
            activity["at"] = time.monotonic()
        stream.close()

    threads = [
        threading.Thread(target=pump, args=(proc.stdout, "out"),
                         daemon=True),
        threading.Thread(target=pump, args=(proc.stderr, "err"),
                         daemon=True),
    ]
    for item in threads:
        item.start()
    deadline = (time.monotonic() + timeout
                if timeout and timeout > 0 else None)
    stalled = False
    cpu_seen = _process_cpu_seconds(proc.pid)
    cpu_checked_at = time.monotonic()
    # 采样间隔必须明显小于判定阈值,否则还没采到就先判卡住了。
    cpu_interval = max(1.0, min(10.0, stall_timeout / 3.0))
    while True:
        try:
            proc.wait(timeout=1)
            break
        except subprocess.TimeoutExpired:
            now = time.monotonic()
            # CPU 采样每 10s 一次:进程仍在消耗 CPU 说明它在干活,
            # 与输出一样算作存活迹象。
            if now - cpu_checked_at >= cpu_interval:
                cpu_checked_at = now
                cpu_now = _process_cpu_seconds(proc.pid)
                if (cpu_now is not None and cpu_seen is not None
                        and cpu_now > cpu_seen):
                    activity["at"] = now
                if cpu_now is not None:
                    cpu_seen = cpu_now
            if now - activity["at"] >= stall_timeout:
                stalled = True
                _terminate_group(proc)
                break
            if deadline is not None and now >= deadline:
                _terminate_group(proc)
                raise subprocess.TimeoutExpired(cmd, timeout)
    for item in threads:
        item.join(timeout=5)
    _LIVE_GROUPS.discard(proc)
    return (proc.returncode, "".join(chunks["out"]),
            "".join(chunks["err"]), stalled)


def _invoke_engine(engine, binary, prompt, timeout, codex_home="",
                   stall_timeout=0):
    """调用编剧引擎,返回 (ok, 文本或错误信息)。

    codex_home 指定 codex 登录态目录(CODEX_HOME),让编剧走独立
    账号通道(如 B 通道),不占默认账号的额度。
    stall_timeout>0 时启用停滞看门狗:连续无任何输出即判卡住并终止,
    配合 timeout=0(不设总时长)实现"长任务不掐、真卡住必停"。"""
    if engine == "codex":
        env = None
        if codex_home:
            home = Path(codex_home).expanduser()
            if not home.is_dir():
                return False, f"codex 编剧 CODEX_HOME 不存在: {home}"
            env = dict(os.environ, CODEX_HOME=str(home.resolve()))
        fd, last = tempfile.mkstemp(prefix="aifos-codex-writer-",
                                    suffix=".txt")
        os.close(fd)
        try:
            cmd = [binary, *CODEX_WRITER_ARGS,
                   "--output-last-message", last, prompt]
            if stall_timeout and stall_timeout > 0:
                returncode, out, err, stalled = _run_with_stall_watchdog(
                    cmd, env, timeout, stall_timeout)
                if stalled:
                    return False, (
                        f"codex 编剧卡住:连续 {int(stall_timeout)}s "
                        "无输出且无 CPU 活动,已终止本次调用;"
                        "请检查网络/账号状态后重试"
                        "(按约定不回退其他产线)")
            else:
                proc = subprocess.run(
                    cmd, capture_output=True, text=True,
                    timeout=(timeout if timeout and timeout > 0 else None),
                    env=env)
                returncode, out, err = (
                    proc.returncode, proc.stdout, proc.stderr)
            if returncode != 0:
                detail = (err.strip() or out.strip())[-300:]
                return False, f"codex 编剧退出码 {returncode}: {detail}"
            try:
                text = Path(last).read_text(encoding="utf-8")
            except OSError:
                text = ""
            # 最终答复文件为空的边角情况仍回退解析 stdout
            # (extract_json 能容忍进度杂讯)。
            return True, text.strip() or out
        finally:
            try:
                os.unlink(last)
            except OSError:
                pass
    proc = subprocess.run(
        [binary, "-p", prompt], capture_output=True, text=True,
        timeout=(timeout if timeout and timeout > 0 else None))
    if proc.returncode != 0:
        # claude CLI 的登录/鉴权错误(如 Not logged in)只写 stdout,
        # stderr 为空;两路都带上,避免日志里只剩"退出码 1:"无法定位。
        detail = proc.stderr.strip() or proc.stdout.strip()
        return False, f"claude 退出码 {proc.returncode}: {detail[:300]}"
    return True, proc.stdout


def _postprocess_and_validate(capability, payload, data):
    """产出后处理(实体清洗/分镜规范化)+ 校验;返回 (data, error)。

    初次生成与就地修复复检共用同一套逻辑,保证两条路径口径一致。
    """
    if (capability == "script" and isinstance(data, dict)
            and isinstance(data.get("scenes"), list)):
        sanitize_script_entities(data)
    if capability == "storyboard" and isinstance(data, dict):
        # The script registry is authoritative. The storyboard model may assign
        # frame-local states and refine a scene boundary to one exact shot in
        # that same scene, but it may not rename, delete, duplicate or move a
        # tracked prop lifecycle into another scene.
        source_script = json.loads(json.dumps(
            payload.get("script") or {}, ensure_ascii=False))
        normalize_prop_contract(source_script)
        scene_events = {
            str(scene.get("scene_no")): str(
                scene.get("event_id") or f"scene:{scene.get('scene_no')}")
            for scene in source_script.get("scenes") or []
            if isinstance(scene, dict) and scene.get("scene_no") is not None
        }
        for shot in data.get("shots") or []:
            if not isinstance(shot, dict):
                continue
            scene_no = str(shot.get("scene_no"))
            shot["scene_event_id"] = scene_events.get(
                scene_no, f"scene:{scene_no}")
        shot_event_scenes = {
            str(shot.get("event_id")): str(shot.get("scene_event_id"))
            for shot in data.get("shots") or []
            if isinstance(shot, dict) and shot.get("event_id")
            and shot.get("scene_event_id")
        }
        proposed_registry = {
            str(item.get("prop_id")): item
            for item in data.get("prop_registry") or []
            if isinstance(item, dict) and item.get("prop_id")
        }
        refined_registry = []
        for source_prop in source_script["prop_registry"]:
            item = json.loads(json.dumps(
                source_prop, ensure_ascii=False))
            proposed = proposed_registry.get(str(item.get("prop_id"))) or {}
            for field, alias in (
                    ("availability_start_event", "introduced_at"),
                    ("availability_end_event", "retired_at")):
                source_ref = item.get(field) or item.get(alias)
                proposed_ref = proposed.get(field) or proposed.get(alias)
                if not (isinstance(source_ref, dict)
                        and isinstance(proposed_ref, dict)):
                    continue
                source_event = str(
                    source_ref.get("event_id") or "").strip()
                proposed_event = str(
                    proposed_ref.get("event_id") or "").strip()
                # An exact shot refinement is valid only inside the source
                # scene boundary. episode-start/end remain immutable.
                if (source_event not in {"episode-start", "episode-end"}
                        and shot_event_scenes.get(proposed_event)
                        == source_event):
                    item[field] = json.loads(json.dumps(
                        proposed_ref, ensure_ascii=False))
                    item[alias] = json.loads(json.dumps(
                        proposed_ref, ensure_ascii=False))
            refined_registry.append(item)
        data["prop_contract_schema"] = PROP_CONTRACT_SCHEMA
        data["prop_registry"] = refined_registry
    if capability == "scene_annotate":
        error = validate_scene_annotation(data)
    elif capability == "image_qc":
        error = validate_image_qc(data)
    elif capability == "script" and payload.get("prompt_refine"):
        error = validate_prompt_refine(data)
    elif capability == "script" and payload.get("asset_prompt"):
        error = validate_asset_prompt(data)
    elif capability == "script" and payload.get("shot_repair"):
        error = validate_shot_repair(data, payload)
    elif capability == "script" and payload.get("prop_design"):
        error = validate_prop_design(data, payload)
    elif capability == "script" and payload.get("ai_director"):
        error = validate_ai_director(data, payload)
    elif capability == "script" and payload.get("rule_appeal"):
        error = validate_rule_appeal(data, payload)
    elif capability == "script" and payload.get("lesson_distill"):
        error = validate_lesson_distill(data, payload)
    elif capability == "script" and payload.get("story_analysis"):
        # 制作圣经的规范化器会补齐新增的派生字段（例如
        # visual_effect_language）并保留模型已经生成的内容。先规范化再
        # 校验，避免旧提示词或偶发漏字段触发一次“整份 JSON 重发”：
        # 这类产出通常很大，修复调用容易被截断并把整条 Provider 判死。
        data = build_story_analysis(
            payload.get("script") or {},
            payload.get("style") or "",
            raw=data,
            source=str(data.get("source") or "ai"),
        )
        error = validate_story_analysis(
            data, require_resolved_identity=False)
    elif capability == "script":
        error = validate_script(data, payload)
    else:
        error = validate_storyboard(data)
    return data, error


# 就地修复调用的耗时上限:只改几个字段,不该给整份重新生成的预算。
REPAIR_TIMEOUT_CAP = 900


def _storyboard_error_shot_positions(error, shot_count):
    """从分镜校验错误里提取 1-based 镜头位置，供增量修复使用。"""
    positions = []
    for value in re.findall(r"镜头\s*(\d+)", str(error or "")):
        position = int(value)
        if 1 <= position <= shot_count and position not in positions:
            positions.append(position)
    return positions


def _merge_storyboard_shot_repairs(data, repaired, positions):
    """把模型返回的少量坏镜头合并回原分镜，其余镜头保持不动。"""
    if isinstance(repaired, list):
        rows = repaired
    elif isinstance(repaired, dict) and isinstance(
            repaired.get("shots"), list):
        rows = repaired["shots"]
    elif isinstance(repaired, dict) and (
            repaired.get("shot_no") is not None
            or repaired.get("_position") is not None):
        rows = [repaired]
    else:
        return None
    rows = [row for row in rows if isinstance(row, dict)]
    if not rows:
        return None
    source_shots = data.get("shots") or []
    allowed = set(positions)
    assigned = {}
    for index, row in enumerate(rows):
        position = row.get("_position")
        if type(position) is not int:
            shot_no = row.get("shot_no")
            matches = [
                item_index + 1
                for item_index, source in enumerate(source_shots)
                if isinstance(source, dict)
                and source.get("shot_no") == shot_no
                and item_index + 1 in allowed
            ]
            if len(matches) == 1:
                position = matches[0]
            elif len(rows) == len(positions):
                position = positions[index]
        if type(position) is not int or position not in allowed:
            continue
        clean = json.loads(json.dumps(row, ensure_ascii=False))
        clean.pop("_position", None)
        assigned[position] = clean
    if not assigned:
        return None
    merged = json.loads(json.dumps(data, ensure_ascii=False))
    for position, row in assigned.items():
        merged["shots"][position - 1] = row
    return merged


def _repair_storyboard_shots_with_engine(
        engine, binary, payload, data, error, repair_timeout,
        codex_home="", stall_timeout=0):
    """只重发校验点名的坏镜头，避免整集分镜修复被输出上限截断。"""
    shots = data.get("shots") if isinstance(data, dict) else None
    if not isinstance(shots, list):
        return None, ""
    positions = _storyboard_error_shot_positions(error, len(shots))
    if not positions or len(positions) >= len(shots):
        return None, ""
    broken = []
    for position in positions:
        row = json.loads(json.dumps(shots[position - 1], ensure_ascii=False))
        row["_position"] = position
        broken.append(row)
    prompt = (
        "你刚为漫剧平台生成了一份分镜 JSON，机器只发现以下镜头字段错误：\n"
        f"{error}\n\n"
        "只修复下面列出的坏镜头。返回且只返回 JSON 对象 "
        "{\"shots\":[完整修复后的镜头对象]}。每个镜头必须保留 "
        "_position（原 shots 数组的 1-based 位置）；除校验点名字段外"
        "不得改写剧情、人物、台词、机位或其他字段。不要解释，不要"
        "Markdown 代码块。\n坏镜头：\n"
        + json.dumps({"shots": broken}, ensure_ascii=False))
    try:
        ok, text = _invoke_engine(
            engine, binary, prompt, repair_timeout,
            codex_home=codex_home, stall_timeout=stall_timeout)
    except (OSError, subprocess.TimeoutExpired) as exc:
        return None, f"(镜头级就地修复调用失败: {exc})"
    if not ok:
        return None, f"(镜头级就地修复失败: {str(text)[:200]})"
    repaired = extract_json(text)
    if repaired is None:
        return None, "(镜头级就地修复输出中未找到 JSON)"
    merged = _merge_storyboard_shot_repairs(data, repaired, positions)
    if merged is None:
        return None, "(镜头级就地修复未返回可合并的 shots)"
    fixed, fixed_error = _postprocess_and_validate(
        "storyboard", payload, merged)
    if fixed_error:
        return None, (
            "(镜头级就地修复复检仍未通过: "
            f"{str(fixed_error)[:300]})")
    return fixed, f"(镜头级就地修复合并 {len(positions)} 个镜头)"


def _repair_with_engine(engine, binary, capability, payload, data, error,
                        timeout, codex_home="", stall_timeout=0):
    """校验失败时的就地修复复检:同一引擎只修错误字段,不丢弃产出。

    原则:内容性校验失败(枚举/字段不合规)就地修,只有超时、连不上
    等系统性故障才交给路由换下一路。返回 (修复后的 data 或 None, 备注)。
    """
    try:
        source = json.dumps(data, ensure_ascii=False)
    except (TypeError, ValueError):
        return None, "(产出不可序列化,跳过就地修复)"
    prompt = (
        "你刚为漫剧平台生成了一份 JSON 产出,机器校验发现以下问题:\n"
        f"{error}\n\n"
        "只修复校验指出的字段,其余内容一字不动;修复后输出完整 JSON,"
        "不要任何解释或 Markdown 代码块。\n原 JSON:\n" + source)
    # timeout=0(不设上限)时修复调用仍用自身上限兜底
    repair_timeout = (min(int(timeout), REPAIR_TIMEOUT_CAP)
                      if timeout and timeout > 0 else REPAIR_TIMEOUT_CAP)
    if capability == "storyboard":
        fixed, note = _repair_storyboard_shots_with_engine(
            engine, binary, payload, data, error, repair_timeout,
            codex_home=codex_home, stall_timeout=stall_timeout)
        if fixed is not None:
            return fixed, note
    try:
        ok, text = _invoke_engine(engine, binary, prompt, repair_timeout,
                                  codex_home=codex_home,
                                  stall_timeout=stall_timeout)
    except (OSError, subprocess.TimeoutExpired) as exc:
        return None, f"(就地修复调用失败: {exc})"
    if not ok:
        return None, f"(就地修复失败: {str(text)[:200]})"
    fixed = extract_json(text)
    if fixed is None:
        return None, "(就地修复输出中未找到 JSON)"
    fixed, fixed_error = _postprocess_and_validate(capability, payload, fixed)
    if fixed_error:
        return None, f"(就地修复复检仍未通过: {str(fixed_error)[:300]})"
    return fixed, ""


def run(request, claude, timeout, engine="claude", codex="codex",
        codex_home="", stall_timeout=0):
    capability = request["capability"]
    payload = request.get("payload", {})
    binary = codex if engine == "codex" else claude
    if shutil.which(binary) is None and not Path(binary).exists():
        return {"ok": False, "error": f"{engine} 命令不存在: {binary}"}
    try:
        prompt = build_prompt(capability, payload)
    except ValueError as exc:
        return {"ok": False, "error": str(exc)}
    try:
        ok, text = _invoke_engine(engine, binary, prompt, timeout,
                                  codex_home=codex_home,
                                  stall_timeout=stall_timeout)
    except (OSError, subprocess.TimeoutExpired) as exc:
        return {"ok": False, "error": f"{engine} 调用失败: {exc}"}
    if not ok:
        return {"ok": False, "error": text}
    data = extract_json(text)
    if data is None:
        return {"ok": False, "error": f"{engine} 输出中未找到 JSON 对象"}
    data, error = _postprocess_and_validate(capability, payload, data)
    if error:
        # 产出已在手,只是字段不合规——丢弃整份重来是对已消耗时间的
        # 浪费;先让同一引擎局部修复并复检,复检仍不过才交回路由换路。
        fixed, note = _repair_with_engine(
            engine, binary, capability, payload, data, error, timeout,
            codex_home=codex_home, stall_timeout=stall_timeout)
        if fixed is not None:
            return {"ok": True, "data": fixed, "uri": "",
                    "repaired_fields": True}
        # 原始输出落盘:校验失败的现场证据,免得下次盲猜模型到底
        # 回了什么(路径回传在错误信息里)。
        dump_note = ""
        try:
            out_dir = Path(request.get("out_dir") or ".")
            out_dir.mkdir(parents=True, exist_ok=True)
            dump = out_dir / f"writer_failure_{capability}.txt"
            dump.write_text(
                f"engine={engine}\nerror={error}{note}\n"
                f"--- raw output ---\n{text}\n", encoding="utf-8")
            dump_note = f"(原始输出已存 {dump})"
        except OSError:
            pass
        return {"ok": False,
                "error": f"{engine} 输出校验失败: {error}{note}{dump_note}"}
    return {"ok": True, "data": data, "uri": ""}


def main(argv=None):
    parser = argparse.ArgumentParser(description="AIFOS Claude/Codex 编剧适配桥")
    parser.add_argument("--claude", default="claude",
                        help="claude 可执行文件路径")
    parser.add_argument("--engine", choices=("claude", "codex"),
                        default="claude",
                        help="编剧引擎;codex 供 Claude CLI 不可用时兜底")
    parser.add_argument("--codex", default="codex",
                        help="codex 可执行文件路径(--engine codex 时用)")
    parser.add_argument("--codex-home", default="",
                        help="codex 登录态目录(CODEX_HOME),"
                             "编剧走独立账号通道时指定")
    parser.add_argument("--timeout", type=int, default=600,
                        help="单次调用总时长上限;0=不设上限(配合停滞检测)")
    parser.add_argument("--stall-timeout", type=int, default=0,
                        help="连续无输出判定卡住的秒数;0=关闭活性检测")
    args = parser.parse_args(argv)
    _register_term_forwarding()
    try:
        request = json.loads(sys.stdin.read())
        reply = run(request, args.claude, args.timeout,
                    engine=args.engine, codex=args.codex,
                    codex_home=args.codex_home,
                    stall_timeout=args.stall_timeout)
    except Exception as exc:
        reply = {"ok": False, "error": str(exc)}
    print(json.dumps(reply, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    sys.exit(main())
