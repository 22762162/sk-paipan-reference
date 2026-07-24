"""Claude 编剧适配桥:剧本 / 分镜由 Claude CLI 实际生成。

把 AIFOS 通用 CLI Provider 协议转换为 `claude -p` 非交互调用,
要求 Claude 输出严格 JSON,解析并校验后回传平台。

配置示例(workspace/config.json):
  "claude": {
    "enabled": true,
    "command": ["python3", "-m", "aifos.adapters.claude_script",
                "--claude", "claude"]
  }
"""

import argparse
import json
import shutil
import subprocess
import sys
from pathlib import Path

from ..generation_diagnostics import normalize_generation_diagnostics
from ..inner_persona import normalize_inner_persona_policy
from ..story_analysis import STORY_ANALYSIS_SCHEMA, validate_story_analysis

SCRIPT_PROMPT = """你是漫剧编剧。为作品《{title}》第{episode}集创作一集完整剧本。
制作风格输入:{style};本集前提:{premise}。

要求:
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
- 人物重要度必须明确标为主角、重要配角、非重要配角或背景路人。
  非重要配角后续固定只生成 1 张候选图;场次中不得出现人物表未声明的新角色;
- `costume_direction` 必须给出可画的服装逻辑(款式、材质、层次、颜色、职业制服/时代服饰和剧情场合),禁止所有角色默认现代都市便服;
- `visual_variants` 至少给出 3 个剧情兼容的造型方案(例如日常、行动/冲突、仪式/舞台);它们是服装、道具和表演细节，不是画风选项；全剧候选图必须继承同一个项目画风;
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
 "scenes": [{{"scene_no": 1, "location": "地点",
   "characters": ["出场角色名"], "action": "本场动作描述",
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
  这些方案不改变全剧唯一画风，候选图只比较人物身份与剧情细节;
- 每个正式成员先写人物分析和视觉 DNA，再与全团其他成员做视觉去重；
  发型、服装结构、身体特征、视觉符号、核心配饰、气质关键词中两项以上
  重叠必须重设计，不能只靠换衣服颜色区分成员;
- 每个成员必须有 `introduction`,明确性别、年龄段、团内身份、性格、目标、
  成员关系与不可改变的身份事实;场次不得新增人物表之外的成员;
- 临时观众、工作人员、群演等背景路人只写 `name`、`role: "背景路人"` 和
  `crowd_function`,不建立独立人物设定或人物资产;非重要配角固定只生成 1 张候选图;
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
- 每段只承载一个主要动作或一次情绪转折；台词逐字照抄，禁止改写；
- 每场先给 1 个环境/肢体镜头，再为每句台词给 1 个对白镜头；
- 关键台词后的听者反应与情绪高潮留白由平台补齐，不要用空镜凑时长；
- 若剧本 `inner_persona_policy.enabled=true`：穿越前该人物可作为真实人物；
  穿越后其真人形态必须从 `characters` 删除。只有必要的内心独白、内心吐槽、
  喜剧反应或决策冲突，才可输出一个 `narrative_overlays` Q版叠层；不得每句
  台词后自动插入。Q版不计真实人数、不参与站位、不被他人感知，继承当前锁定
  衣服但无默认道具，表情动作可以夸张；内心发声时真人宿主闭口且不生成字幕；
- shot_no 从 1 连续编号；duration 单位秒，优先 5-8 秒，最长 15 秒；
- prompt 只含本镜头真正可见且会影响生成的世界状态、场景、准确人物名单、主体动作、
  光影、机位与结尾状态；不得复制整集前情、其他镜头、人物传记或无关道具；
- 每个镜头必须额外输出 `physical_logic`，只写本镜可见的物理/空间事实：人物、
  道具、桌面/地面、镜头的相对位置，使用方向、接触点、视线和动作可达性；
  涉及电脑/手机/屏幕时必须明确屏幕正面、键盘/手部、使用者和摄影机同侧关系，
  禁止人物坐在屏幕后方却看到屏幕正面等反向构图；
- prompt 中人物形态按人物设定描写(名字只是称呼,「小鹿」若设定为
  人类不能当动物写;设定为动物/精怪的按设定写,全片保持一致);
- 不生成对白字幕。手机屏、弹幕、合同等可读文字只描述载体与准确文字，
  后续由 ChatGPT 关键帧锁定，不能交给视频模型从零生成；如需可读文字，必须
  额外输出 `readable_text` 文字资产卡，只填写画面确实要出现的文字，不得把
  镜头合同、提示词标题、质检原因或系统字段当作屏幕内容；同时给出载体、版式、
  字体/颜色样式、屏幕透视和可读优先级；没有可读文字时输出 null；
- 只输出一个 JSON 对象,不要任何其他文字或 Markdown 代码块。

JSON 格式:
{{"episode_title": "...", "shots": [{{"shot_no": 1, "scene_no": 1,
  "kind": "environment", "description": "...", "camera": "镜头语言",
  "duration": 2.5, "characters": ["角色名"], "dialogue": null,
  "narrative_overlays": [{{"kind":"inner_persona_chibi",
    "function":"inner_monologue/inner_commentary/comic_reaction/decision_conflict",
    "name":"内心人格角色名","host_character":"宿主角色名",
    "expression":"夸张Q版表情","action":"夸张Q版动作",
    "dialogue":"必要时的内心台词；否则空字符串"}}],
  "physical_logic": "本镜物理/空间关系",
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
- 候选图数量固定:主角5、重要配角3、非重要配角1、背景路人0;
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
"texture_and_render":"","architecture_and_environment":"",
"wardrobe_and_styling":"","props_and_graphics":"","forbidden_visuals":[]}},
"scenes":[{{"scene_no":1,"location":"","story_function":"",
"environment":"","layout":"","materials_and_props":"","time_weather":"",
"lighting":"","sound":"","continuity_anchors":[],"prompt_prefix":""}}],
"characters":[{{"name":"","importance":"主角/重要配角/非重要配角/背景路人",
"candidate_count":5,"gender":"","age_range":"","identity_facts":"",
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


def extract_json(text):
    """从 Claude 输出中提取第一个合法 JSON 对象(容忍前后杂讯)。"""
    decoder = json.JSONDecoder()
    idx = text.find("{")
    while idx != -1:
        try:
            obj, _ = decoder.raw_decode(text[idx:])
            return obj
        except ValueError:
            idx = text.find("{", idx + 1)
    return None


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
    script["story_bible_version"] = 1
    script["declared_character_names"] = cast_names
    script["inner_persona_policy"] = normalize_inner_persona_policy(script)
    return script


def validate_script_bible(script):
    """返回世界观/前情/人物介绍硬门禁错误；通过时返回 ``None``。"""
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
    used = set()
    for scene in script.get("scenes") or []:
        used.update(scene.get("characters") or [])
        used.update(
            line.get("character") for line in scene.get("lines") or []
            if isinstance(line, dict) and line.get("character"))
    unknown = sorted(used - declared)
    if unknown:
        return "场次出现未在人物设定中介绍的角色: " + "、".join(unknown)
    return None


def validate_script(script, payload):
    if payload.get("character_design"):
        return validate_design(script, payload)
    if not isinstance(script, dict):
        return "输出不是 JSON 对象"
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
            {ln["character"] for ln in scene.get("lines", [])}))
        scene.setdefault("action", "")
    # 平台侧字段兜底,避免下游因缺字段中断
    script.setdefault("project_title", payload.get("project_title", ""))
    script.setdefault("episode_number", payload.get("episode_number", 0))
    script.setdefault("episode_title", "")
    script.setdefault("logline", "")
    normalize_script_bible(script, payload)
    return validate_script_bible(script)


def validate_storyboard(storyboard):
    if not isinstance(storyboard, dict) or not storyboard.get("shots"):
        return "缺少 shots"
    if not isinstance(storyboard["shots"], list):
        return "shots 需为数组"
    for shot in storyboard["shots"]:
        if not isinstance(shot, dict):
            return f"镜头需为对象,收到: {str(shot)[:80]}"
        for field in ("scene_no", "duration", "prompt"):
            if field not in shot:
                return f"镜头缺少字段 {field}: {shot}"
        try:
            shot["duration"] = float(shot["duration"])
        except (TypeError, ValueError):
            return f"镜头时长非法: {shot}"
        if shot["duration"] <= 0:
            return f"镜头时长非法: {shot}"
        shot.setdefault("characters", [])
        shot.setdefault("kind",
                        "dialogue" if shot.get("dialogue") else "environment")
        shot.setdefault("description", "")
        shot.setdefault("camera", "")
        shot.setdefault("dialogue", None)
        shot.setdefault("physical_logic", "")
        shot.setdefault("readable_text", None)
    # 编号强制连续,避免下游连续性质检失败
    for index, shot in enumerate(storyboard["shots"], start=1):
        shot["shot_no"] = index
    storyboard.setdefault("episode_title", "")
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
  配色、配饰/道具和气质变化；它们不改变本剧唯一画风，候选图不得把画风作为变量，
  也不是同一套衣服只换动作;
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
引用上述实际输入，不得虚构未提交的提示词或参考图。

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
  发现任何物理或空间关系不成立，pass 必须为 false，并在 issues 指出具体对象和位置。
- 不允许出现:与设定形态不符的角色、与剧情无关的杂物(悬挂的衣物/衣架)、
  字幕条、乱码文字、多余或缺失的人物{extra}
- 这是静态关键帧质检：只检查画面中能看见的最终状态。不得因为单张图无法证明
  运镜、眼神变化过程、呼吸或其他时间动作而判失败；景别裁掉且并非剧情要求必须
  出镜的裤子、腰间配饰等，也不得仅因不可见而判失败。

只输出一个 JSON 对象,不要任何其他文字:
{{"pass": true或false, "identity_checked": true或false,
"identity_match": true或false,
"identity_checks": [{{"character":"角色名",
"view":"front_or_three_quarter/profile/back/back_or_over_shoulder",
"basis":["实际核验项"],"checked":true或false,"match":true或false}}],
"gender_checked": true或false, "gender_match": true或false,
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


def build_qc_prompt(payload):
    characters = payload.get("characters") or []
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
    composition_rules = (
        f"类型={composition.get('composition_type')};"
        f"正面主体={composition.get('expected_primary_count')};"
        f"实际可见人形={composition.get('expected_visible_figure_count')};"
        f"{actor_rules};{composition.get('count_rule', '')}"
        if composition else "标准构图；按待检图实际可见视角逐人选择核验项")
    count_rule = (
        "本图为同一角色的多视角/局部设定图(四视图/特写/服装细节等):"
        "画面中出现的每个人形、头像或局部都必须是该角色同一人,"
        "人数不按出场人数核对"
        if payload.get("multi_view")
        else (
            f"严格共 {payload.get('count', len(characters))} 个已登记角色；"
            + (composition.get("count_rule") or "每个人物只计一次")))
    physical = payload.get("physical_contract") or {}
    physical_rules = "；".join(physical.get("rules") or [])
    physical_objects = "；".join(physical.get("objects") or [])
    physical_text = (
        ("必须执行硬检查；" if payload.get("physical_logic_required") else "仅作辅助检查；")
        + (physical_rules or "人物、镜头、道具关系按当前镜头实际构图核对")
        + (f"；对象关系：{physical_objects}" if physical_objects else ""))
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
            "Q版表情动作表现。若有内心声音，真人宿主必须闭口且不得出现旁白字幕。")
    else:
        overlay_rules = (
            "本镜不允许非现实内心Q版叠层；detected_overlay_count 必须为0，"
            "不得擅自新增Q版、分身、幽灵或意识小人。")
    return IMAGE_QC_PROMPT.format(
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
    data["pass"] = bool(data["pass"])
    issues = data.get("issues")
    if not isinstance(issues, list):
        issues = [str(issues)] if issues else []
    data["issues"] = [str(item) for item in issues][:8]
    if "identity_checked" in data:
        data["identity_checked"] = bool(data["identity_checked"])
    if "identity_match" in data:
        data["identity_match"] = bool(data["identity_match"])
    if "gender_checked" in data:
        data["gender_checked"] = bool(data["gender_checked"])
    if "gender_match" in data:
        data["gender_match"] = bool(data["gender_match"])
    if "count_checked" in data:
        data["count_checked"] = bool(data["count_checked"])
    if "count_match" in data:
        data["count_match"] = bool(data["count_match"])
    for key in (
            "physical_logic_checked", "physical_logic_match",
            "spatial_logic_checked", "spatial_logic_match",
            "overlay_count_checked", "overlay_count_match"):
        if key in data:
            data[key] = bool(data[key])
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
            row["checked"] = bool(row.get("checked"))
            row["match"] = bool(row.get("match"))
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


def build_prompt(capability, payload):
    """构造编剧/分镜/人物设定提示词(CLI 桥与 Claude API Provider 共用)。"""
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
            prompt = (
                f"这是上一版剧本:\n{previous}\n\n"
                f"用户的修改意见(必须逐条落实):{feedback}\n\n"
                f"请在保留可取之处的前提下按意见重写。{prompt}")
        return prompt
    if capability == "storyboard":
        return STORYBOARD_PROMPT.format(
            script=json.dumps(payload.get("script", {}), ensure_ascii=False))
    if capability == "image_qc":
        return build_qc_prompt(payload)
    raise ValueError(f"claude 编剧不支持能力: {capability}")


def run(request, claude, timeout):
    capability = request["capability"]
    payload = request.get("payload", {})
    if shutil.which(claude) is None and not Path(claude).exists():
        return {"ok": False, "error": f"claude 命令不存在: {claude}"}
    try:
        prompt = build_prompt(capability, payload)
    except ValueError as exc:
        return {"ok": False, "error": str(exc)}
    try:
        proc = subprocess.run(
            [claude, "-p", prompt], capture_output=True, text=True,
            timeout=timeout)
    except (OSError, subprocess.TimeoutExpired) as exc:
        return {"ok": False, "error": f"claude 调用失败: {exc}"}
    if proc.returncode != 0:
        return {"ok": False,
                "error": f"claude 退出码 {proc.returncode}: "
                         f"{proc.stderr.strip()[:300]}"}
    data = extract_json(proc.stdout)
    if data is None:
        return {"ok": False, "error": "claude 输出中未找到 JSON 对象"}
    if capability == "image_qc":
        error = validate_image_qc(data)
    elif capability == "script" and payload.get("story_analysis"):
        error = validate_story_analysis(data)
    elif capability == "script":
        error = validate_script(data, payload)
    else:
        error = validate_storyboard(data)
    if error:
        return {"ok": False, "error": f"claude 输出校验失败: {error}"}
    return {"ok": True, "data": data, "uri": ""}


def main(argv=None):
    parser = argparse.ArgumentParser(description="AIFOS Claude 编剧适配桥")
    parser.add_argument("--claude", default="claude",
                        help="claude 可执行文件路径")
    parser.add_argument("--timeout", type=int, default=600)
    args = parser.parse_args(argv)
    try:
        request = json.loads(sys.stdin.read())
        reply = run(request, args.claude, args.timeout)
    except Exception as exc:
        reply = {"ok": False, "error": str(exc)}
    print(json.dumps(reply, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    sys.exit(main())
