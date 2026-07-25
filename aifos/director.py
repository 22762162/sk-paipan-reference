"""AI 导演中心:总控。拆解任务、调度 Provider、控制流程与成本。

生产流程(SK 漫剧工业流):
  需求 → 剧本 → 连续性圣经 → 五维分镜 → 空间调度图 → 关键帧/文字锁定 → 首尾帧
       → 生产门禁 → Seedance2 视频(随视频声音/口型) → 剪辑
       → 抽帧检查板 + 内容复核 + 交付脚本 → 包装 → 数据沉淀
"""

import copy
import hashlib
import json
import re
import threading
import time
from pathlib import Path

from .adapters.claude_script import (is_background_role,
                                     normalize_script_bible,
                                     validate_script_bible)
from .db import now
from .errors import (AifosError, BudgetExceeded, ProduceCancelled,
                     ProviderError, ProviderUnavailable)
from .generation_diagnostics import (
    generation_input_hash,
    normalize_generation_diagnostics,
    prompt_hash,
    reference_actions,
    reference_hash,
    retry_input_decision,
    targeted_prompt_patch,
)
from .image_acceleration import ImageAccelerationStore
from .quality_policy import (
    default_quality_policy,
    formal_reference_allowed,
    image_task_class_for,
    normalize_quality,
    normalize_quality_policy,
    recommend_asset_quality,
    recommend_shot_image_quality,
    resolve_image_quality,
    resolve_video_quality,
    set_policy_choices,
)
from .prompt_contract import (
    build_physical_contract,
    build_composition_contract,
    compile_shot_prompt,
    readable_text_required,
    shot_local_scene,
    validate_shot_prompt_contract,
)
from .qc_feedback import optimize_qc_feedback
from .lessons import lessons_block, project_lessons, record_lessons
from .relations import relation_lines, write_relations
from .spatial_blocking import (
    build_spatial_plan,
    mark_spatial_reference_requirements,
    requires_spatial_reference,
    shot_blocking,
    write_spatial_reference_pngs,
    write_spatial_svgs,
)
from .story_analysis import (
    apply_story_analysis,
    build_story_analysis,
    reconcile_character_entities,
    unresolved_character_labels,
    validate_line_speaker_resolution,
    validate_story_analysis,
)
from .workflow import (
    PIPELINE_VERSION,
    build_content_review,
    build_continuity_bible,
    build_preflight,
    enrich_storyboard,
    lock_text_assets,
    production_profile,
    write_delivery_verifier,
    write_review_board,
)

# 画幅 → 像素尺寸(视频/图片);封面用竖版比例
ASPECT_DIMS = {
    "9:16": {"width": 1080, "height": 1920},
    "16:9": {"width": 1920, "height": 1080},
}

MODERN_OTOME_STYLE = (
    "现代都市乙女游戏CG，精致3D半写实角色渲染，亚洲当代青年，"
    "现代发型与时尚通勤服装，清透自然皮肤，细腻五官，柔和电影灯光，"
    "高级时尚杂志质感；禁止古装、汉服、发簪、长袍、水墨、国风、"
    "2D平涂、动漫线稿和历史建筑"
)
DEFAULT_VISUAL_STYLE = (
    "剧情自适应精品漫剧，电影级半写实人物与场景；服装、发型、道具、建筑和光影"
    "必须服从剧本的时代/世界观、地域、职业、人物性格与剧情阶段；自然材质，"
    "统一人物造型；禁止把未明确的故事默认套成现代都市便服"
)
LEGACY_DEFAULT_VISUAL_STYLE_PREFIX = "现代都市精品漫剧"


def infer_visual_style(premise="", project_title=""):
    """把明确时代/媒介要求提升为项目级视觉风格。

    剧情前提优先于标题；标题只在前提没有给出视觉方向时兜底，避免
    《万妖图录》这类明确仙侠标题被现代默认值改成都市题材。
    """
    text = (premise or "").lower()
    if any(word in text for word in ("乙女", "3d", "现代", "时尚", "都市")):
        return MODERN_OTOME_STYLE
    if any(word in text for word in ("古装", "仙侠", "武侠", "国风")):
        return ("国风漫剧，精致2D动画质感，服装与建筑严格符合剧情时代，"
                "高细节，统一人物造型")
    if any(word in text for word in ("民国", "年代", "旧上海", "旗袍")):
        return ("民国年代漫剧，电影级半写实质感；服装、发型、街景与道具严格"
                "符合民国时代和人物身份，统一角色造型")
    if any(word in text for word in ("校园", "高中", "大学", "学生")):
        return ("青春校园漫剧，清透电影光影；校服、社团服和生活造型严格按"
                "校园剧情与人物性格区分，统一角色造型")
    if any(word in text for word in ("赛博", "未来", "星际", "机甲", "末世")):
        return ("未来幻想漫剧，电影级科幻材质与光影；服装、装备和环境严格"
                "按世界观、阵营和人物功能设计，统一角色造型")
    if any(word in text for word in ("女团", "男团", "偶像", "打歌", "舞台")):
        return ("舞台偶像漫剧，精致半写实角色与舞台灯光；成员服装按定位、"
                "歌曲主题和剧情场合区分，禁止千篇一律通勤装")
    if any(word in text for word in ("西幻", "魔法", "骑士", "精灵", "吸血鬼")):
        return ("西幻漫剧，精致半写实幻想质感；服装、护具、法器和建筑严格"
                "按阵营、阶层与世界观设计，统一角色造型")
    title = (project_title or "").lower()
    if any(word in title for word in ("仙侠", "修仙", "妖", "灵", "剑", "宗门")):
        return ("国风漫剧，精致2D动画质感，服装与建筑严格符合剧情时代，"
                "高细节，统一人物造型")
    return DEFAULT_VISUAL_STYLE

STAGES = [
    ("script", "剧本 + AI制作圣经"),
    ("continuity", "连续性圣经"),
    ("cast", "人物/场景图"),
    ("storyboard", "五维分镜"),
    ("blocking", "空间调度图"),
    ("images", "关键帧"),
    ("text_assets", "文字资产锁定"),
    ("frames", "首尾帧"),
    ("preflight", "生产门禁"),
    ("videos", "Seedance视频"),
    ("voices", "Seedance2随视频声音/口型"),
    ("edit", "剪映剪辑"),
    ("qc", "三层质检"),
    ("package", "封面/标题/拆条"),
    ("archive", "数据沉淀"),
]

# 预生产检查点:此阶段完成后可暂停等待用户确认,
# 确认后才进入视频生产(真实产线从这里开始消耗即梦额度)
CONFIRM_AFTER = "preflight"
# 视频是按镜头计费的成品资产。生成后必须经过视频质检；质检失败只允许
# 自动返工一次，第二次仍失败时必须停在人工检查点，不能继续烧额度。
VIDEO_QC_AUTO_RETRIES = 1
VIDEO_QC_SCHEMA = "aifos.video-qc/v1"
CHARACTER_CANDIDATES = 5
IMPORTANT_CHARACTER_CANDIDATES = 3
NONIMPORTANT_CHARACTER_CANDIDATES = 1
CHARACTER_BACKGROUND_RULE = (
    "人物立绘必须是纯净、无文字的单人物资产背景;背景只允许纯色、柔和渐变"
    "或干净无辨识度的棚拍底,禁止任何场景、建筑、室内、街道、自然环境、"
    "道具、其他人物、品牌标识、Logo、水印、字幕和乱码文字。")

WORKWEAR_RULE = (
    "若角色身份或职业属于外卖小哥、快递员、配送员、医生、护士、警察、"
    "消防员、保安、服务员、厨师、工人等职业,必须穿该职业真实可辨认的"
    "工作服/制服并体现必要的职业装备,不得用普通便服替代。")

# 场景概念图是后续分镜共用的环境基准,不能把人物服装描述误当成场景内容。
# 这些关键词只补充空间功能和可见锚点,最终仍以剧本地点/时代/动作为准。
SCENE_ENVIRONMENT_PRESETS = (
    (("直播间", "直播室", "录播间"),
     "干净紧凑的直播工作区；主机位三脚架与镜头朝向主播位，桌面有麦克风、"
     "补光灯、耳机和调音设备，背景是吸音墙/灯带/收纳架，线缆整齐，留出人物站位"),
    (("练习室", "排练室"),
     "大面积镜面墙、木地板、音响与可移动把杆；墙边有水瓶和毛巾收纳，"
     "中心留出连续动作区域，镜面反射保持同一空间结构"),
    (("舞台", "演出厅", "剧场"),
     "明确的舞台台口、主灯位、侧灯位、地面走位区与观众方向；灯光设备只作环境"
     "结构，不出现观众面孔或无关人物"),
    (("后台", "化妆间", "休息室"),
     "演出后台的化妆台、镜前灯、服装挂架和设备箱；通道与座位分区清晰，"
     "道具按剧情需要摆放，不堆满无关杂物"),
    (("办公室", "会议室", "写字楼", "公司"),
     "功能明确、干净可用的办公空间；桌椅、显示器、文件收纳和玻璃/隔断形成"
     "前后层次，设备年代与故事阶段一致，桌面不出现可读文件文字"),
    (("教室", "学校", "图书馆"),
     "符合校园尺度的课桌、黑板/书架、窗户和过道；光线方向明确，书本只作无字"
     "道具，空间留出人物进出动线"),
    (("咖啡", "餐厅", "便利店", "厨房"),
     "服务空间的操作台、座位/货架、收银区域和出入口关系清楚；材质、器具和"
     "灯光体现营业时段，招牌与包装不得出现可读文字"),
    (("医院", "诊室", "病房"),
     "医疗空间的病床/诊疗台、器械车、隔帘、洗手区和门口关系清晰；设备整洁，"
     "屏幕与病历全部无字无标识"),
    (("宿舍", "卧室", "房间", "客厅"),
     "生活空间的床铺/沙发、收纳、桌面和门窗形成可连续拍摄的动线；物品有使用痕迹"
     "但不凌乱，私人照片和纸张不出现可读内容"),
    (("古镇", "客栈", "府邸", "宫殿", "宗门", "藏经阁"),
     "严格按古代/仙侠世界建造的空间；院落、木石结构、灯火、帘幕和传统器物"
     "符合阶层与地域，不混入现代电器、玻璃幕墙或现代服装陈设"),
    (("街道", "巷子", "广场", "车站"),
     "明确的道路走向、建筑界面、遮挡关系和远近层次；时代、地域和天气由剧本决定，"
     "空镜不添加随机路人、车辆或品牌广告"),
    (("山谷", "森林", "湖边", "海边", "荒野", "废墟"),
     "自然环境的地形层次、可通行路径、视线遮挡和远景边界清楚；天气与时间服从"
     "剧情，不用无关建筑或随机人物填充"),
)


def is_background_character(character):
    """背景路人可出现在镜头剧情中，但不创建单独人物母资产。"""
    return is_background_role(character)


def is_unresolved_character(character):
    name = str((character or {}).get("name") or "").strip()
    role = str((character or {}).get("role") or "").strip()
    if name == "待确认说话人" or "待确认" in role:
        return True
    from .script_import import is_likely_performance_label
    return is_likely_performance_label(name)


def character_candidate_target(character):
    """按人物重要度返回候选张数，防止非主要角色消耗五张额度。"""
    role = str((character or {}).get("role") or "").strip().lower()
    if is_background_character(character) or is_unresolved_character(character):
        return 0
    if any(token in role for token in ("主角", "主人公", "女主", "男主")):
        return CHARACTER_CANDIDATES
    if any(token in role for token in ("非重要", "非主要", "次要")):
        return NONIMPORTANT_CHARACTER_CANDIDATES
    if any(token in role for token in (
            "重要", "核心", "同伴", "反派", "对手",
            "队长", "主唱", "舞担", "成员", "男二", "女二")):
        return IMPORTANT_CHARACTER_CANDIDATES
    return NONIMPORTANT_CHARACTER_CANDIDATES


def character_candidate_policy_text():
    return ("主角5张；重要配角3张；非重要角色固定1张；"
            "跑龙套/背景路人不做独立设定、不生成候选图或立绘；"
            "所有候选继承本剧唯一画风，只比较人物身份与剧情造型细节")

def character_production_readiness_error(script, analysis=None):
    labels = unresolved_character_labels(script)
    if labels:
        return (
            "人物实体尚未确认：" + "、".join(labels)
            + "。这些更像语气、动作或状态，不会生成图片；"
            "请先点「AI 重新分析」把它们归并到真实人物，再锁定生产。")
    analysis_map = {
        item.get("name"): item
        for item in (analysis or {}).get("characters", [])
        if isinstance(item, dict) and item.get("name")
    }
    for character in (script or {}).get("characters", []):
        if (not isinstance(character, dict)
                or is_background_character(character)
                or is_unresolved_character(character)):
            continue
        item = analysis_map.get(character.get("name")) or {}
        if not str(
                item.get("image_prompt")
                or character.get("image_prompt") or "").strip():
            return (
                f"人物「{character.get('name')}」还没有无矛盾的最终出图提示词，"
                "请先重新运行 AI 人物分析。")
    return None

# 人物定版候选不承担“选画风”的职责。本剧画风由项目/制作圣经唯一锁定；
# 多张候选只用于比较同一画风下的人物身份、表情和剧情造型细节。候选被人工
# 锁定后，其完整脸、发型、妆容与服装才成为后续镜头不可漂移的身份锚点。
CHARACTER_LOOK_VARIANTS = (
    {
        "variant_id": "story_baseline",
        "variant_label": "候选 A · 基准身份",
        "look_variant": {
            "hair": "采用剧本人物设定中的基准发型",
            "makeup": "采用剧本人物设定中的基准妆容或面部修饰",
            "costume": "采用剧本人物设定中的基准服装与配色",
            "temperament": "准确呈现剧本设定的核心性格与气质",
        },
    },
    {
        "variant_id": "clean_minimal",
        "variant_label": "候选 B · 发型细节",
        "look_variant": {
            "hair": "同一发型体系内做轻微梳理和发丝整理差异，不改变身份轮廓",
            "makeup": "保持基准妆造和本剧统一媒介，只做不可改变身份的轻微强弱差异",
            "costume": "保持基准服装体系，只调整剧情允许的层次或细节",
            "temperament": "同一核心性格，表情略偏清爽自然，不能改变人物气质",
        },
    },
    {
        "variant_id": "sharp_professional",
        "variant_label": "候选 C · 表情细节",
        "look_variant": {
            "hair": "保持基准发型轮廓、发际线和发色家族，不更换发型体系",
            "makeup": "保持基准妆造，只让眉眼表情和面部明暗更清晰",
            "costume": "保持基准服装体系，不能以更换画风或时代服装制造差异",
            "temperament": "同一核心性格，表情略偏专注坚定，不能变成另一种人设",
        },
    },
    {
        "variant_id": "soft_relaxed",
        "variant_label": "候选 D · 职业细节",
        "look_variant": {
            "hair": "保持基准发型轮廓，只允许不改变身份的自然发丝差异",
            "makeup": "保持基准妆造体系，不能改成另一种媒介或人物年龄感",
            "costume": "保持职业、时代和剧情服装体系，突出一个可追溯的职业细节",
            "temperament": "同一核心性格，补充与职业/经历一致的自然状态",
        },
    },
    {
        "variant_id": "signature_statement",
        "variant_label": "候选 E · 标志特征",
        "look_variant": {
            "hair": "保持基准发型轮廓和身份标志，只强化一个可辨识的小细节",
            "makeup": "保持基准妆造和本剧统一画风，只强化人物已有的眉眼重点",
            "costume": "保持基准服装、时代和职业逻辑，只强化一个故事来源明确的细节",
            "temperament": "同一核心性格，呈现最清晰的角色标志性神态",
        },
    },
)

# 人物完整资产套件：16:9 合成板只用于审核；面部、正面、严格侧面和
# 完整背面必须分别生成高清单图，作为后续镜头真正使用的母资产。
CANONICAL_CHARACTER_SHEETS = [
    ("turnaround", "三视图审核板",
     "16:9横版审核板:左侧约1/3为同一角色清晰面部近景；右侧约2/3"
     "依次为同一角色完整全身正面、严格90度侧面、完整180度背面；"
     "纯白或中性浅灰背景,统一相机高度、焦段、站姿和比例；审核板仅作"
     "人工浏览与索引,不得替代独立高清母资产"),
    ("closeup", "面部特写",
     "独立高清面部近景母资产:中性表情,五官、发际线、瞳色、耳部、"
     "下颌与颈部结构清晰；肤面干净均匀,只保留极轻微自然微纹理"),
    ("front", "正面母资产",
     "独立高清全身正面母资产:严格正对镜头,自然中性站姿,从头到脚完整可见"),
    ("profile", "侧面母资产",
     "独立高清全身侧面母资产:严格90度真侧面,鼻唇下颌、耳朵、后脑、"
     "肩胯与服装侧缝结构真实,不得只做轻微转头或四分之三视角"),
    ("back", "背面母资产",
     "独立高清全身背面母资产:严格180度完整背面,后脑、发型背部、"
     "肩胛、服装背片、接缝、扣件和配饰位置真实,不得复制正面"),
]
DETAIL_CHARACTER_SHEETS = [
    ("features", "特征设定",
     "辨识特征拆解:发型、瞳色、体态、标志性配饰逐项放大标注"),
    ("makeup", "妆容设定",
     "妆容细节:底妆、眉眼妆、唇色、特殊纹样,正面半身"),
    ("costume", "服装设定",
     "全身服装设定:正面站姿,服装配色、材质与层次清晰完整"),
    ("costume_detail", "服装细节",
     "服装细节拆解:纹样、扣饰、腰带、鞋履、佩饰逐项放大展示"),
]
CHARACTER_SHEETS = CANONICAL_CHARACTER_SHEETS + DETAIL_CHARACTER_SHEETS
CANONICAL_CHARACTER_SHEET_KEYS = ("closeup", "front", "profile", "back")

CHARACTER_ASSET_POLICY_SCHEMA = "aifos.character-assets/v1"
CHARACTER_ASSET_MODES = ("auto", "simple", "full")
CHARACTER_ASSET_COMPLEXITY_TOKENS = (
    "近景", "特写", "微表情", "哭", "舞台", "舞蹈", "打斗", "追逐",
    "转身", "换装", "制服", "礼服", "群像", "直播", "口型", "演唱",
    "变身", "非人", "机器人", "妖", "兽",
)


def normalize_character_asset_mode(value):
    mode = str(value or "auto").strip().lower()
    if mode not in CHARACTER_ASSET_MODES:
        raise AifosError(
            "人物资产模式需为 auto(自动)、simple(仅人物形象图)或 full(完整套件)")
    return mode


def resolve_character_asset_policy(policy=None, script=None):
    """把用户选择解析为本集实际要不要生成四视图与细节图。"""
    mode = normalize_character_asset_mode((policy or {}).get("mode"))
    characters = [
        character for character in (script or {}).get("characters", [])
        if not is_background_character(character)
    ]
    scenes = list((script or {}).get("scenes", []))
    locations = {
        str(scene.get("location") or "").strip()
        for scene in scenes if str(scene.get("location") or "").strip()
    }
    reasons = []
    if mode == "auto":
        text = json.dumps(script or {}, ensure_ascii=False)
        risk_tokens = [token for token in CHARACTER_ASSET_COMPLEXITY_TOKENS
                       if token in text]
        if not script:
            resolved = "full"
            reasons.append("剧本信息不足，按完整模式保护后续一致性")
        elif len(characters) > 1:
            resolved = "full"
            reasons.append(f"有 {len(characters)} 名需锁定身份的人物")
        elif len(locations) > 1:
            resolved = "full"
            reasons.append(f"人物会跨 {len(locations)} 个场景复用")
        elif len(scenes) > 2:
            resolved = "full"
            reasons.append(f"剧情包含 {len(scenes)} 场，连续性要求较高")
        elif risk_tokens:
            resolved = "full"
            reasons.append("包含高一致性内容：" + "、".join(risk_tokens[:5]))
        else:
            resolved = "simple"
            reasons.append("单人、少场景且无高一致性内容，可只用最终人物形象图")
    else:
        resolved = mode
        if (policy or {}).get("source") == "legacy_migration":
            reasons.append("旧项目已有完整人物资产计划，保持原有生产行为")
        else:
            reasons.append(
                "用户手动选择仅使用最终人物形象图"
                if mode == "simple" else "用户手动选择完整人物资产套件")
    generate_sheets = resolved == "full"
    return {
        "schema": CHARACTER_ASSET_POLICY_SCHEMA,
        "mode": mode,
        "source": (policy or {}).get("source", "episode"),
        "resolved_mode": resolved,
        "generate_sheets": generate_sheets,
        "sheet_count_per_character": (
            len(CHARACTER_SHEETS) if generate_sheets else 0),
        "reasons": reasons,
    }

IMAGE_ASSET_KINDS = {
    "character_art", "character_sheet", "scene_art", "character_candidate",
    "image", "first_frame", "last_frame", "cover", "reference",
    "spatial_blocking", "inner_persona",
}

REFERENCE_ROLES = {
    "identity", "wardrobe", "scene", "composition", "style", "manual",
    "inner_persona",
}
# Seedream 5 Lite 当前最多接收 10 张参考图。导演层按最小公共上限组织
# 参考图，避免同一任务切换模型时图序、人物或构图语义发生漂移。
IMAGE_REFERENCE_LIMIT = 10
SHOT_BASE_REFERENCE_LIMIT = 8

# render_plan.json is shared by the parallel image workers.  Keep the
# read/write pair atomic inside one Python process; the actual image calls stay
# outside this lock so Codex A/B can run concurrently.
_PLAN_IO_LOCK = threading.RLock()


class Director:
    def __init__(self, db, config, logger, projects, assets, router, qc, ops,
                 data_center, artifacts_root, standards=None):
        self.db = db
        self.config = config
        self.log = logger
        self.projects = projects
        self.assets = assets
        self.router = router
        self.qc = qc
        self.ops = ops
        self.data = data_center
        self.artifacts_root = Path(artifacts_root)
        self.standards = standards
        self.image_acceleration = ImageAccelerationStore(db)

    def _resolve_standard_snapshot(self, episode_id, force=False):
        """为一集绑定不可漂移的制作标准。

        新集与强制重做读取当前生效标准；确认续产和普通断点续产始终恢复
        本集首次绑定的快照，避免出现旧分镜搭配新声画参数的混合产线。
        """
        if not force:
            existing, _ = self.projects.latest_document(
                episode_id, "production_standard")
            if existing is not None:
                return existing
        if self.standards is None:
            snapshot = {
                "profile_key": "sk-manju-v5",
                "version": 1,
                "version_id": 0,
                "name": "SK 五维漫剧标准",
                "fingerprint": "legacy-config",
                "content": {},
            }
        else:
            snapshot = self.standards.active()
        self.projects.save_document(
            episode_id, "production_standard", snapshot)
        return snapshot

    def _episode_quality_policy(self, episode_id, *, persist=False):
        policy, _ = self.projects.latest_document(episode_id, "quality_policy")
        normalized = normalize_quality_policy(policy)
        if persist and policy is None:
            self.projects.save_document(
                episode_id, "quality_policy", normalized)
        return normalized

    def update_quality_policy(self, episode_id, *, image_default=None,
                              video_default=None, image_overrides=None,
                              video_overrides=None):
        """保存本集自动/手动质量选择，供续产和逐镜审计复用。"""
        episode = self.projects.get_episode(int(episode_id))
        if episode is None:
            raise AifosError("剧集不存在")
        current = self._episode_quality_policy(episode["id"])
        policy = set_policy_choices(
            current, image_default=image_default,
            video_default=video_default,
            image_overrides=image_overrides,
            video_overrides=video_overrides)
        self.projects.save_document(episode["id"], "quality_policy", policy)
        return policy

    def _episode_character_asset_policy(self, episode_id, *, persist=False):
        policy, version = self.projects.latest_document(
            episode_id, "character_asset_policy")
        default_mode = "auto"
        source = "episode" if policy is not None else "default"
        if policy is None:
            episode = self.projects.get_episode(int(episode_id))
            project = (self.db.query_one(
                "SELECT * FROM projects WHERE id=?",
                (episode["project_id"],)) if episode is not None else None)
            if episode is not None and project is not None:
                ctx = {"project": dict(project), "episode": dict(episode),
                       "out_root": self._episode_dir(project, episode)}
                plan = self._plan_read(ctx)
                if any(item.get("category") == "character_sheet"
                       for item in plan.get("items", [])):
                    default_mode = "full"
                    source = "legacy_migration"
        normalized = {
            "schema": CHARACTER_ASSET_POLICY_SCHEMA,
            "mode": normalize_character_asset_mode(
                (policy or {}).get("mode", default_mode)),
            "source": (policy or {}).get("source", source),
        }
        if persist and policy is None:
            version = self.projects.save_document(
                episode_id, "character_asset_policy", normalized)
        return normalized, version

    def character_asset_policy(self, episode_id, *, script=None,
                               persist=False):
        """返回本集人物资产选择及自动判断后的实际执行模式。"""
        episode = self.projects.get_episode(int(episode_id))
        if episode is None:
            raise AifosError("剧集不存在")
        policy, version = self._episode_character_asset_policy(
            episode["id"], persist=persist)
        if script is None:
            script, _ = self.projects.latest_document(
                episode["id"], "script")
        resolved = resolve_character_asset_policy(policy, script)
        resolved["version"] = version
        return resolved

    def update_character_asset_policy(self, episode_id, mode,
                                      expected_version=None):
        """在人物定版检查点保存自动/简化/完整模式。"""
        episode = self.projects.get_episode(int(episode_id))
        if episode is None:
            raise AifosError("剧集不存在")
        if episode["status"] != "awaiting_cast":
            raise AifosError("只能在人物定版阶段调整四视图与细节图")
        _current, version = self._episode_character_asset_policy(
            episode["id"])
        if expected_version is not None:
            try:
                expected = int(expected_version)
            except (TypeError, ValueError):
                raise AifosError("人物资产设置版本无效")
            if expected != version:
                raise AifosError("人物资产设置已在其他页面更新，请刷新后重试")
        policy = {
            "schema": CHARACTER_ASSET_POLICY_SCHEMA,
            "mode": normalize_character_asset_mode(mode),
            "source": "user",
            "updated_at": now(),
        }
        version = self.projects.save_document_cas(
            episode["id"], "character_asset_policy", policy, version,
            allowed_status={"awaiting_cast"}, reject_running=True)
        if policy["mode"] == "simple":
            project = self.db.query_one(
                "SELECT * FROM projects WHERE id=?",
                (episode["project_id"],))
            ctx = {"project": dict(project), "episode": dict(episode),
                   "out_root": self._episode_dir(project, episode)}
            self._plan_seed(ctx, "character_sheet", [])
        resolved = self.character_asset_policy(episode["id"])
        resolved["version"] = version
        return resolved

    # ---- 入口:一句话开工 ----
    def produce(self, project_title, episode_number, premise="", style="",
                force=False, script=None, pause_for_confirm=False,
                kind=None, feedback="", run_id=None, style_pack_id=""):
        """force=False 时增量生产:已有且落盘完好的产物直接复用,
        只补齐缺失部分——真实产线(即梦按镜头计费)断点续产的关键。
        script:用户自带剧本(标准 JSON);提供时跳过 AI 编剧,
        人物/场次/分镜等全部从该剧本自动推导。
        pause_for_confirm=True:剧本确认后按角色重要度生成定妆候选并暂停，
        所有人物人工锁定后才继续五维分镜、关键帧、首尾帧和门禁；确认后再次调用
        produce(不带该参数)即从断点继续自动完成 Seedance 声画、无字幕剪辑
        与三层质检。"""
        if script is not None:
            force = True  # 剧本变了,旧镜头/配音不可复用
        existing_project = self.projects.get_project(project_title)
        requested_style = (style or "").strip()
        stored_style = (existing_project["style"] if existing_project else "")
        # 旧版本把未指定画风写成现代都市默认值;升级时按当前剧情重新推断,
        # 但用户明确选过的现代风格仍原样保留。
        if (not requested_style and stored_style.startswith(
                LEGACY_DEFAULT_VISUAL_STYLE_PREFIX)):
            stored_style = ""
        # 未指定风格时保持为空:剧本完成后由制作圣经根据全文证据推导。
        # 不能在看到正文前仅凭标题/一句梗概套入现代乙女或国风模板。
        visual_style = requested_style or stored_style
        project, created = self.projects.get_or_create_project(
            project_title, style=visual_style,
            kind=kind if kind in ("drama", "idol") else "drama",
            style_pack_id=str(style_pack_id or "").strip())
        updates = {}
        if (not created and kind in ("drama", "idol")
                and project["kind"] != kind):
            updates["kind"] = kind
        if (not created and visual_style
                and (requested_style or not project["style"])
                and project["style"] != visual_style):
            updates["style"] = visual_style
        if (not created and style_pack_id
                and project["style_pack_id"] != str(style_pack_id).strip()):
            updates["style_pack_id"] = str(style_pack_id).strip()
        if updates:
            # 用户明确改了内容类型/画风，或旧项目尚未锁定画风。
            project = self.projects.update_project(project_title, **updates)
        episode, _ = self.projects.get_or_create_episode(
            project["id"], episode_number, premise=premise)
        self.log.info(
            "director",
            f"开始制作《{project_title}》第{episode_number}集 "
            f"(episode_id={episode['id']},force={force})")

        standard_snapshot = self._resolve_standard_snapshot(
            episode["id"], force=force)
        profile = production_profile(self.config, standard_snapshot)
        aspect = (project["aspect"]
                  or self.config.get("defaults", "aspect", default="9:16"))
        ctx = {
            "project": dict(project),
            "episode": dict(episode),
            "out_root": self._episode_dir(project, episode),
            "force": force,
            "aspect": aspect,
            "dims": ASPECT_DIMS.get(aspect, ASPECT_DIMS["9:16"]),
            "provided_script": script,
            "feedback": feedback,
            "production_standard": standard_snapshot,
            "production_profile": profile,
            "voice_mode": profile.get("voice", "jimeng_builtin"),
            "lip_sync": bool(profile.get("lip_sync", True)),
            "quality_policy": self._episode_quality_policy(
                episode["id"], persist=True),
            "character_asset_policy": self.character_asset_policy(
                episode["id"], persist=True),
            "run_id": run_id,
        }
        stage_reports = []
        failed = False
        paused = ""
        for stage, stage_cn in STAGES:
            if self._cancel_requested(ctx):
                paused = "cancelled"
                break
            try:
                report = self._run_stage(stage, stage_cn, ctx)
            except ProduceCancelled:
                paused = "cancelled"
                break
            stage_reports.append(report)
            if report["status"] == "failed":
                failed = True
                break
            # 第一道确认:新生成的剧本先给用户过目,确认后才开始画图
            if (pause_for_confirm and stage == "script"
                    and ctx.get("script_is_new")):
                paused = "script"
                break
            # 第二道确认:候选数量按人物重要度分配,必须人工选定1张。没有最终立绘时，
            # 禁止继续生成资产套件、分镜画面和首尾帧。
            if (stage == "cast" and ctx.get("cast_selection_required")):
                paused = "cast"
                break
            if pause_for_confirm and stage == CONFIRM_AFTER:
                paused = "preflight"
                break

        episode = self.projects.get_episode(episode["id"])
        if failed:
            self.projects.set_episode_status(episode["id"], "failed")
        elif paused == "cancelled":
            # 手动停止:安全落回最近的可调整检查点
            gate_done = self.db.query_one(
                "SELECT COUNT(*) AS n FROM tasks WHERE episode_id=? "
                "AND stage=? AND status='done'",
                (episode["id"], CONFIRM_AFTER))
            script_doc, _ = self.projects.latest_document(
                episode["id"], "script")
            selection = self.character_selection_status(
                ctx["project"]["id"],
                (script_doc or {}).get("characters", []))
            candidates_started = any(
                item.get("candidate_count", 0)
                for item in selection.get("characters", []))
            landing = ("awaiting_confirm" if gate_done and gate_done["n"]
                       else "awaiting_cast" if (
                           selection.get("required") and candidates_started)
                       else "awaiting_script" if script_doc else "created")
            self.projects.set_episode_status(episode["id"], landing)
            self.log.info(
                "director",
                f"已手动停止生成,回到「{landing}」;调整后确认即可继续")
        elif paused == "script":
            self.projects.set_episode_status(
                episode["id"], "awaiting_script")
            self.log.info(
                "director",
                f"剧本已生成,等待确认后再画人物/场景/分镜"
                f"(episode_id={episode['id']})")
        elif paused == "cast":
            self.projects.set_episode_status(
                episode["id"], "awaiting_cast")
            self.log.info(
                "director",
                "人物候选已生成，等待逐个选定最终立绘；"
                "全部锁定前不生成后续图片"
                f"(episode_id={episode['id']})")
        elif paused:
            self.projects.set_episode_status(
                episode["id"], "awaiting_confirm")
            self.log.info(
                "director",
                f"预生产完成,等待确认后进入视频生产"
                f"(episode_id={episode['id']})")
        elif not ctx.get("qc_report", {}).get("passed", True):
            self.projects.set_episode_status(episode["id"], "qc_failed")
        else:
            self.projects.set_episode_status(episode["id"], "done")
        episode = self.projects.get_episode(episode["id"])

        summary = {
            "project": project_title,
            "episode": episode_number,
            "status": episode["status"],
            "qc_score": episode["qc_score"],
            "cost": round(episode["cost"], 2),
            "budget": self.config.get("budget", "per_episode", default=0),
            "artifacts_dir": str(ctx["out_root"]),
            "stages": stage_reports,
            "video_qc": ctx.get("video_qc_report", {}),
            "outputs": {
                "final": ctx.get("final_uri", ""),
                "cover": ctx.get("cover_uri", ""),
                "titles": ctx.get("titles", []),
                "clips": [c["uri"] for c in ctx.get("clips", [])],
                "publish": ctx.get("publish_kit", {}).get("uri", ""),
            },
            "aspect": ctx["aspect"],
            "production_standard": {
                "profile_key": standard_snapshot.get("profile_key", ""),
                "version": standard_snapshot.get("version"),
                "version_id": standard_snapshot.get("version_id"),
                "name": standard_snapshot.get("name", ""),
                "fingerprint": standard_snapshot.get("fingerprint", ""),
            },
        }
        (ctx["out_root"] / "summary.json").write_text(
            json.dumps(summary, ensure_ascii=False, indent=2),
            encoding="utf-8")
        self.log.info(
            "director",
            f"《{project_title}》第{episode_number}集 制作结束:"
            f"{episode['status']},质检 {episode['qc_score']},"
            f"成本 {episode['cost']:.2f}")
        return summary

    def _episode_dir(self, project, episode):
        path = (self.artifacts_root / f"p{project['id']:03d}"
                / f"e{episode['number']:03d}")
        path.mkdir(parents=True, exist_ok=True)
        return path

    # ---- 阶段调度:每个阶段落任务表,统一异常与成本记账 ----
    def _run_stage(self, stage, stage_cn, ctx):
        episode_id = ctx["episode"]["id"]
        ts = now()
        cur = self.db.execute(
            "INSERT INTO tasks(episode_id, run_id, stage, name, status, "
            "created_at, updated_at) VALUES(?,?,?,?,?,?,?)",
            (episode_id, ctx.get("run_id"), stage, stage_cn, "running", ts,
             ts))
        task_id = cur.lastrowid
        # 用户点了停止(状态=cancelling)时不要覆盖停止信号
        current = self.projects.get_episode(episode_id)
        if current is None or current["status"] != "cancelling":
            self.projects.set_episode_status(episode_id, stage)
        self._task_cost = 0.0
        self._task_providers = set()
        try:
            result = getattr(self, f"_stage_{stage}")(ctx)
            self.db.execute(
                "UPDATE tasks SET status='done', provider=?, cost=?, "
                "result=?, updated_at=? WHERE id=?",
                (",".join(sorted(self._task_providers)), self._task_cost,
                 json.dumps(result or {}, ensure_ascii=False)[:4000],
                 now(), task_id))
            return {"stage": stage, "name": stage_cn, "status": "done",
                    "cost": round(self._task_cost, 2),
                    "providers": sorted(self._task_providers),
                    "detail": result or {}}
        except ProduceCancelled:
            self.db.execute(
                "UPDATE tasks SET status='stopped', provider=?, cost=?, "
                "error=?, updated_at=? WHERE id=?",
                (",".join(sorted(self._task_providers)), self._task_cost,
                 "已手动停止", now(), task_id))
            raise
        except Exception as exc:
            self.db.execute(
                "UPDATE tasks SET status='failed', provider=?, cost=?, "
                "error=?, updated_at=? WHERE id=?",
                (",".join(sorted(self._task_providers)), self._task_cost,
                 str(exc)[:1000], now(), task_id))
            import traceback
            trace = traceback.format_exc(limit=3).strip()
            self.log.error(
                "director",
                f"阶段 {stage} 失败: {exc}\n{trace[-600:]}")
            return {"stage": stage, "name": stage_cn, "status": "failed",
                    "cost": round(self._task_cost, 2), "error": str(exc)}

    def _cancel_requested(self, ctx):
        """用户是否在 Web/CLI 点了「停止生成」(状态置为 cancelling)。"""
        row = self.projects.get_episode(ctx["episode"]["id"])
        return row is not None and row["status"] == "cancelling"

    def _call(self, ctx, capability, payload, sub_dir):
        """经由路由器调用 Provider,并做预算与成本记账。"""
        if self._cancel_requested(ctx):
            raise ProduceCancelled("已手动停止生成")
        if capability in ("image", "frames", "cover"):
            self._attach_reference_manifest(payload)
        episode = self.projects.get_episode(ctx["episode"]["id"])
        budget = self.config.get("budget", "per_episode", default=0)
        if budget and episode["cost"] >= budget:
            raise BudgetExceeded(
                f"单集成本 {episode['cost']:.2f} 已达预算 {budget},停止调度")
        result = self.router.call(
            capability, payload, ctx["out_root"] / sub_dir,
            cancel=lambda: self._cancel_requested(ctx))
        self._task_cost += result.cost
        self._task_providers.add(result.provider)
        self.projects.add_episode_cost(ctx["episode"]["id"], result.cost)
        return result

    # ---- 增量复用:已有资产落盘完好则直接使用 ----
    def _existing_asset_uri(self, ctx, kind, name):
        if ctx.get("force"):
            return None
        row = self.assets.latest(ctx["project"]["id"], kind, name)
        if row is None or not row["uri"]:
            return None
        uri = row["uri"]
        if uri.startswith("http://") or uri.startswith("https://"):
            return uri
        return uri if Path(uri).exists() else None

    def _shot_name(self, ctx, shot_no):
        return f"e{ctx['episode']['number']:03d}_shot{shot_no:03d}"

    def _line_name(self, ctx, line_no):
        return f"e{ctx['episode']['number']:03d}_line{line_no:03d}"

    # ---- 图片生产清单:每张图的分类/提示词/实时状态(Web 实时可见) ----
    # 每类资产套件重点采用的人物设定字段(全用会超长,按图取材)
    SHEET_DESIGN_KEYS = {
        "turnaround": ("species", "appearance", "hair", "costume",
                       "palette", "era_setting", "occupation",
                       "costume_direction", "visual_dna"),
        "closeup": ("species", "appearance", "hair", "eyes",
                    "temperament", "visual_dna"),
        "front": ("species", "appearance", "hair", "costume",
                  "costume_detail", "accessories", "palette",
                  "visual_dna"),
        "profile": ("species", "appearance", "hair", "costume",
                    "costume_detail", "accessories", "palette",
                    "visual_dna"),
        "back": ("species", "appearance", "hair", "costume",
                 "costume_detail", "accessories", "palette",
                 "visual_dna"),
        "features": ("signature", "appearance", "hair", "palette",
                      "signature_props"),
        "makeup": ("makeup", "eyes", "palette", "personality"),
        "costume": ("costume", "palette", "accessories",
                     "occupation", "costume_direction"),
        "costume_detail": ("costume", "costume_detail", "palette",
                            "accessories", "signature_props",
                            ),
    }
    VISIBLE_DNA_KEYS = (
        "face_structure", "hair_silhouette", "body_or_occupation_marks",
        "clothing_structure", "clothing_wear_state", "story_visual_symbol",
        "signature_accessory", "temperament_keywords",
    )
    DESIGN_LABELS = (
        ("species", "形态"),
        ("gender", "性别"), ("sex", "性别"),
        ("age_range", "年龄段"), ("identity", "身份"),
        ("appearance", "外貌"), ("hair", "发型"), ("eyes", "眼睛"),
        ("temperament", "气质"), ("personality", "性格"),
        ("makeup", "妆容"), ("costume", "服装"),
        ("costume_detail", "服装细节"), ("accessories", "配饰"),
        ("palette", "配色"), ("signature", "标志特征"),
        ("background_prompt", "人物背景提示词"),
        ("image_prompt", "最终人物出图卡"),
        ("negative_prompt", "人物负面提示词"),
        ("era_setting", "时代/世界观"), ("occupation", "职业身份"),
        ("motivation", "核心动机"), ("backstory", "人物经历"),
        ("relationships", "人物关系"),
        ("costume_direction", "服装设计逻辑"),
        ("signature_props", "标志道具"),
        ("visual_variants", "剧情造型方案"),
        ("visual_direction", "制作圣经视觉方向"),
        ("prompt_prefix", "人物提示词母版"),
        ("continuity_anchors", "人物连续性锚点"),
        ("character_analysis", "人物因果分析"),
        ("visual_dna", "人物视觉DNA"),
        ("cast_dedup", "全剧角色去重"))

    @staticmethod
    def _design_value(value):
        """把 Claude 返回的列表/对象稳定地拼进提示词,不丢失造型方案。"""
        if isinstance(value, (list, dict)):
            if not value:
                return ""
            return json.dumps(value, ensure_ascii=False, separators=(",", ":"))
        return str(value or "").strip()

    def _design_line(self, design, keys=None):
        """人物设定 → 提示词片段;keys 限定只取某几个字段。"""
        if not design:
            return ""
        parts = []
        for key, label in self.DESIGN_LABELS:
            if keys is not None and key not in keys:
                continue
            raw = design.get(key)
            if key == "visual_dna" and isinstance(raw, dict):
                # analysis_source / derivation are valuable planning evidence,
                # but they are biography rather than visible image controls.
                raw = {item: raw.get(item) for item in self.VISIBLE_DNA_KEYS
                       if raw.get(item)}
            value = self._design_value(raw)
            if value:
                parts.append(f"{label}:{value}")
        return ",".join(parts)

    def _anchor_character(self, project_id, characters=None):
        """风格锚角色:主角优先,否则名单第一位;记入 style_anchor 资产。"""
        row = self.assets.latest(project_id, "style_anchor", "default")
        if row is not None:
            meta = row["meta"]
            if isinstance(meta, str):
                meta = json.loads(meta or "{}")
            if meta.get("character"):
                return meta["character"]
        if not characters:
            return None
        anchor = next((c["name"] for c in characters
                       if "主" in (c.get("role") or "")),
                      characters[0]["name"])
        self.assets.register(project_id, "style_anchor", "default",
                             meta={"character": anchor})
        return anchor

    def _style_anchor_uri(self, project_id, exclude_name=None):
        """只返回用户明确标成 ``style`` 的无人物身份风格参考图。

        旧实现把主角立绘当成所有角色/场景的风格图，会把主角的脸、服装
        和姿态一并污染其他人物。项目画风默认由文字和场景基准维持；只有
        用户明确声明“仅参考画风”的图片才允许跨角色全局使用。
        """
        del exclude_name  # 保留旧调用签名，风格图不再由某个角色担任。
        for row in reversed(
                self.assets.active_list(project_id, kind="reference")):
            meta = self._asset_meta(row)
            if str(meta.get("attach_to") or "").strip():
                continue
            if self._reference_role(row) != "style":
                continue
            uri = str(row["uri"] or "")
            if uri and (uri.startswith(("http://", "https://"))
                        or Path(uri).exists()):
                return uri
        return None

    def _reference_role(self, row):
        """返回用户参考图的单一用途；旧数据采用保守推断。

        无关联、无用途的旧“全项目通用”图不再自动进入任何任务，避免一张
        人物图污染整部剧。用户仍可在 Seedance 资产选择器中手动选它。
        """
        meta = self._asset_meta(row)
        role = str(meta.get("reference_role")
                   or meta.get("role") or "").strip().lower()
        aliases = {
            "人物": "identity", "身份": "identity",
            "服装": "wardrobe", "道具": "wardrobe",
            "场景": "scene", "空间": "scene",
            "构图": "composition", "动作": "composition",
            "画风": "style", "风格": "style",
        }
        role = aliases.get(role, role)
        if role in REFERENCE_ROLES:
            return role
        text = " ".join((
            str(row["name"] or ""), str(meta.get("note") or ""))).lower()
        if any(token in text for token in (
                "画风", "风格", "style", "色板", "palette", "材质参考")):
            return "style"
        if any(token in text for token in (
                "场景", "空间", "环境", "location", "scene")):
            return "scene"
        if any(token in text for token in (
                "服装", "衣服", "造型", "道具", "costume", "wardrobe")):
            return "wardrobe"
        if any(token in text for token in (
                "构图", "机位", "动作", "姿势", "composition", "pose")):
            return "composition"
        # 有明确关联对象的历史图仍按“身份/对象参考”兼容；没有关联对象的
        # 旧图必须人工指定用途后才自动进入生产。
        return "identity" if str(meta.get("attach_to") or "").strip() \
            else "manual"

    def _reference_rows(self, project_id, attach_names, *,
                        allowed_roles=None, include_global_style=False):
        """筛出与当前角色/场景精确关联的用户参考图。

        全局旧图不再凭空进入所有镜头；只有明确 ``style`` 角色的全局图可按
        需加入。这个过滤同时供人物候选、套件、场景、关键帧和视频使用。
        """
        wanted = {str(name) for name in (attach_names or []) if name}
        allowed = set(allowed_roles or REFERENCE_ROLES)
        rows = []
        for row in self.assets.active_list(project_id, kind="reference"):
            meta = self._asset_meta(row)
            attach = str(meta.get("attach_to") or "").strip()
            role = self._reference_role(row)
            if role not in allowed:
                continue
            if attach:
                if attach not in wanted:
                    continue
            elif not (include_global_style and role == "style"):
                continue
            uri = str(row["uri"] or "")
            if uri and (uri.startswith(("http://", "https://"))
                        or Path(uri).exists()):
                rows.append(row)
        return rows

    def _reference_asset_match(self, row):
        meta = self._asset_meta(row)
        role = self._reference_role(row)
        labels = {
            "identity": "人物身份参考",
            "wardrobe": "服装/道具参考",
            "scene": "场景空间参考",
            "composition": "构图/动作参考",
            "style": "画风参考",
            "manual": "手动参考图",
        }
        return {
            "asset_id": row["id"], "kind": "reference",
            "name": row["name"],
            "label": f"{row['name']}·{labels[role]}",
            "uri": row["uri"], "reference_role": role,
            "attach_to": meta.get("attach_to", ""),
        }

    def _user_reference_payload(self, project_id, attach_names, *,
                                allowed_roles=None,
                                include_global_style=False):
        rows = self._reference_rows(
            project_id, attach_names, allowed_roles=allowed_roles,
            include_global_style=include_global_style)
        return {
            "reference_images": [row["uri"] for row in rows],
            "asset_matches": [
                self._reference_asset_match(row) for row in rows],
        }

    def _character_design(self, project_id, name):
        row = self.assets.latest(project_id, "character", name)
        if row is None:
            return None
        meta = row["meta"]
        if isinstance(meta, str):
            meta = json.loads(meta or "{}")
        return (meta or {}).get("design")

    def _locked_look_variant(self, project_id, name):
        """人工定版后取候选的服装/配饰锚点,供全部人物套件复用。"""
        row = self._locked_identity(project_id, name)
        if row is None:
            return {}
        meta = self._asset_meta(row)
        look = meta.get("look_variant")
        result = dict(look) if isinstance(look, dict) else {}
        # 旧候选的 look_variant 只有四个基础字段;配饰/职业道具仍从剧本
        # 人物设定补齐,否则不同套件会各自猜测“乔安到底有没有配饰”。
        design = self._character_design(project_id, name) or {}
        for key in ("costume", "costume_detail", "palette", "accessories",
                    "signature_props", "props", "temperament"):
            if not result.get(key) and design.get(key):
                result[key] = design[key]
        return result

    def _locked_look_line(self, look):
        if not look:
            return ""
        values = []
        for key, label in (("costume", "服装"), ("palette", "配色"),
                           ("accessories", "配饰"), ("props", "道具"),
                           ("temperament", "气质")):
            value = self._design_value(look.get(key))
            if value:
                values.append(f"{label}:{value}")
        if not any(key in look and self._design_value(look.get(key))
                   for key in ("accessories", "signature_props", "props")):
            values.append("配饰/道具:以锁定最终立绘可见内容为准,不可见不得新增")
        return ";".join(values)

    def _character_sheet_reference_uris(self, project_id, name,
                                        exclude_key=""):
        """套件重画只复用最相关的 1–2 张资产，避免参考图互相争权。"""
        uris = []
        priority = {
            "turnaround": ("front", "profile"),
            "closeup": ("front", "features"),
            "front": ("closeup", "turnaround"),
            "profile": ("front", "closeup"),
            "back": ("profile", "front"),
            "features": ("turnaround", "makeup"),
            "makeup": ("features", "turnaround"),
            "costume": ("turnaround", "costume_detail"),
            "costume_detail": ("costume", "turnaround"),
        }.get(exclude_key, ("turnaround", "costume"))
        for key in priority:
            if key == exclude_key:
                continue
            row = self.assets.latest(
                project_id, "character_sheet", f"{name}:{key}")
            if (row and formal_reference_allowed(self._asset_quality(row))
                    and row["uri"] and Path(row["uri"]).exists()):
                uris.append(row["uri"])
        return uris

    @staticmethod
    def _reference_safe_design(design):
        """最终立绘锁定后只保留剧情/服装语义，避免旧文字反向改脸改性别。

        appearance/hair/eyes/makeup/signature 等身份字段已经由人工锁定的
        最终立绘提供。候选生成前的旧文字可能与定版图冲突，不能再作为后续
        镜头的硬约束传给任何图片 API。
        """
        if not isinstance(design, dict):
            return {}
        keys = (
            "species", "gender", "sex", "age_range", "identity",
            "personality", "temperament", "costume",
            "costume_detail", "palette", "background_prompt",
            "image_prompt", "negative_prompt",
            "era_setting",
            "occupation", "motivation", "backstory", "relationships",
            "costume_direction", "signature_props",
            "character_analysis", "visual_dna", "cast_dedup",
        )
        return {key: design.get(key) for key in keys if design.get(key)}

    def _portrait_prompt(self, name, role, style, design=None):
        final_card = self._design_value(
            (design or {}).get("image_prompt"))
        if final_card:
            species = self._design_value(
                (design or {}).get("species") or "人类")
            acting = self._design_value(
                (design or {}).get("personality")
                or (design or {}).get("temperament"))
            detail = final_card
            if f"形态:{species}" not in final_card:
                detail = f"形态:{species}；{detail}"
            if acting and acting not in final_card:
                detail = f"{detail}；表情与站姿外化:{acting[:120]}"
        else:
            detail = self._design_line(
                design, keys=("species", "gender", "age_range", "identity",
                          "appearance", "hair", "eyes",
                          "makeup", "accessories", "signature",
                          "temperament", "costume", "costume_detail",
                          "palette", "era_setting", "occupation",
                          "signature_props"))
        return (f"角色立绘:{name}({role}),{style}"
                + (f",{detail},表情站姿体现其性格" if detail else "")
                + ";服装和造型必须从人物背景提示词、时代/世界观、职业、性格、"
                "本集剧情与当前场合推导，不得把不同角色模板化成同一种现代都市穿搭"
                + ",全身,正面;如有角色参考图,优先锁定该图的人脸骨相、五官比例、"
                "眼鼻嘴、肤色与年龄感、发际线、发型轮廓、发色、眉眼妆、眼线、"
                "睫毛、唇妆和身份配饰,必须是同一个人;服装、服装颜色/材质、动作、"
                "场景和光影按本剧本及当集造型生成,允许与参考图服装不同,除非明确"
                "要求保留参考图服装;"
                f"{WORKWEAR_RULE}{CHARACTER_BACKGROUND_RULE}")

    def _candidate_variant(self, index, design=None):
        """返回同一项目画风下的候选差异轴和资产元数据。"""
        template = CHARACTER_LOOK_VARIANTS[index - 1]
        look = dict(template["look_variant"])
        if design:
            base_costume = ";".join(filter(None, (
                str(design.get("costume") or "").strip(),
                str(design.get("costume_detail") or "").strip(),
            )))
            base = {
                "hair": str(design.get("hair") or "").strip(),
                "makeup": str(design.get("makeup") or "").strip(),
                "costume": base_costume,
                "temperament": str(design.get("temperament") or "").strip(),
            }
            for key, value in base.items():
                if not value:
                    continue
                if index == 1:
                    look[key] = value
                else:
                    look[key] = f"基准设定:{value}；本候选{look[key]}"
        story_variants = self._story_variants(design)
        story_variant = (story_variants[index - 1]
                         if index <= len(story_variants) else None)
        if story_variant:
            label = story_variant.get("label") or story_variant.get("name")
            if label:
                story_label = str(label)
            else:
                story_label = ""
            for key in ("hair", "makeup", "costume", "temperament"):
                if story_variant.get(key):
                    look[key] = self._design_value(story_variant[key])
        else:
            story_label = ""
        return {
            "variant_id": template["variant_id"],
            "variant_label": (f"{template['variant_label']} · "
                              f"{story_label}"
                              if story_label
                              else template["variant_label"]),
            "look_variant": look,
            "variant_source": "generated",
            "story_variant": story_variant or {},
        }

    @staticmethod
    def _story_variants(design):
        """读取剧本/人物设定给出的剧情造型方案。"""
        if not design:
            return []
        value = design.get("visual_variants") or design.get("outfit_variants")
        if isinstance(value, list):
            return [item for item in value if isinstance(item, dict)]
        if isinstance(value, dict):
            return [value]
        if not isinstance(value, str) or not value.strip():
            return []
        try:
            parsed = json.loads(value)
            if isinstance(parsed, list):
                return [item for item in parsed if isinstance(item, dict)]
        except (TypeError, ValueError):
            pass
        return [{"label": part.strip(), "costume": part.strip()}
                for part in value.replace("；", "|").split("|")
                if part.strip()]

    def _candidate_portrait_prompt(self, name, role, style, design, variant,
                                   has_reference=False):
        """把内部分析编译成同一项目画风下、单张图可执行的提示词。"""
        final_card = self._design_value(
            (design or {}).get("image_prompt"))
        if final_card:
            identity = final_card
        else:
            identity = self._design_line(
                design, keys=("species", "gender", "age_range", "identity",
                              "appearance", "eyes", "era_setting", "occupation",
                              "signature_props"))
            dna = (design or {}).get("visual_dna")
            if isinstance(dna, dict):
                compact_dna = "；".join(
                    f"{label}:{value}" for key, label in (
                        ("face_structure", "脸部骨相"),
                        ("hair_silhouette", "发型轮廓"),
                        ("body_or_occupation_marks", "体态/职业痕迹"),
                        ("clothing_structure", "服装结构"),
                        ("story_visual_symbol", "视觉符号"),
                        ("signature_accessory", "核心配饰"),
                        ("temperament_keywords", "气质"),
                    )
                    if (value := self._design_value(dna.get(key))))
                if compact_dna:
                    identity = f"{identity}；人物视觉DNA:{compact_dna}"
        look = variant["look_variant"]
        if has_reference:
            hair = (
                "严格保持参考图发型轮廓、发际线、发量和发色家族；"
                "候选不改变发型身份，只允许自然发丝状态差异")
            makeup = (
                "严格保持参考图妆造、眉眼、眼线、睫毛和唇妆身份；"
                "候选不改变妆造体系")
            variant_rule = (
                "有参考图时只锁人物脸型、五官骨相、年龄、性别表达、发际线"
                "、发型、妆造和身份标志；候选只允许剧情服装细节、表情和轻微"
                "姿态差异，禁止换脸、换发型或换画风")
        else:
            hair = look["hair"]
            makeup = look["makeup"]
            variant_rule = (
                "无参考图时,可在同一项目画风、时代、职业和核心人物气质内"
                "做有限的发型/妆容/表情细节差异,不得制作不同画风候选")
        return (
            f"【任务】{name}（{role}）单人定角候选 · {variant['variant_label']}。"
            "不同候选是互斥造型，不是同一套衣服只换动作。"
            f"【最终人物出图卡】{identity or '年龄、物种、性别表达与核心身份不变'}。"
            f"【本张造型覆盖项】仅以下字段覆盖出图卡中的对应字段："
            f"发型:{hair}；妆容:{makeup}；服装:{look['costume']}；"
            f"表情气质:{look['temperament']}。{variant_rule}。"
            + (f";剧情场合:{variant.get('story_variant', {}).get('occasion') or variant.get('story_variant', {}).get('scene')}"
               if variant.get("story_variant", {}).get("occasion")
               or variant.get("story_variant", {}).get("scene") else "")
            + (f";配饰/道具:{variant.get('story_variant', {}).get('props') or variant.get('story_variant', {}).get('accessories')}"
               if variant.get("story_variant", {}).get("props")
               or variant.get("story_variant", {}).get("accessories") else "")
            + f"【PROJECT STYLE LOCK】本项目唯一画风:{style}；"
            "所有候选必须完全一致，不得制作不同画风候选。"
            "职业人物必须穿真实可辨认的工作服/制服并保留必要装备。"
            "人物立绘必须是纯净、无文字的单人物资产背景。"
            "【构图】单人、全身正面自然站姿、从头到脚完整、纯净中性棚拍背景；"
            "五官、手指、关节与身体比例自然。"
            "【禁止】其他人物、身份/性别/年龄漂移、时代错置、模板网红脸、"
            "塑料皮肤、文字、字幕、Logo、水印、可辨认背景场景。")

    @staticmethod
    def _scene_style_line(style):
        """从项目画风中抽取适用于环境的媒介/材质/光影,去掉人物服装词。"""
        raw = str(style or "").strip()
        if not raw:
            return "剧情自适应半写实漫剧材质与电影光影"
        blocked = ("角色", "人物", "青年", "发型", "服装", "穿搭", "皮肤",
                   "五官", "妆", "脸", "通勤")
        chunks = [part.strip() for part in re.split(r"[,，;；。]", raw)
                  if part.strip()]
        kept = [part for part in chunks
                if not any(word in part for word in blocked)]
        # 兜底保留第一段媒介名,避免画风整句恰好只包含人物描述。
        if not kept:
            kept = chunks[:1]
        return "；".join(kept)

    @staticmethod
    def _scene_environment_line(location):
        place = str(location or "未命名地点")
        for keywords, detail in SCENE_ENVIRONMENT_PRESETS:
            if any(keyword in place for keyword in keywords):
                return detail
        return ("根据地点名称建立真实可用的空间功能、入口/出口、前中后景和人物动线；"
                "材质、设备、建筑与陈设严格服从剧本时代/世界观、地域和社会阶层，"
                "不凭空加入不属于故事的现代或古代元素")

    def _scene_prompt(self, location, style, scene=None, premise=""):
        """场景概念图提示词:只建立可复用环境,不把人物画风误当场景内容。"""
        scene = scene or {}
        place = str(location or scene.get("location") or "未命名地点")
        time_state = (scene.get("time_of_day") or scene.get("time") or "")
        if "·" in place and not time_state:
            place, suffix = place.split("·", 1)
            time_state = suffix.strip()
        time_state = time_state or "按本场剧情确定的时间与天气"
        production_design = (
            scene.get("production_design")
            if isinstance(scene.get("production_design"), dict) else {})
        purpose = (
            production_design.get("story_function")
            or scene.get("scene_purpose")
            or "建立本地点可复用的空间、出入口、表演区与摄影动线")
        analysis_line = "；".join(filter(None, (
            str(scene.get("prompt_prefix") or "").strip(),
            str(production_design.get("environment") or "").strip(),
            str(production_design.get("layout") or "").strip(),
            str(production_design.get("materials_and_props") or "").strip(),
            str(production_design.get("lighting") or "").strip(),
        )))
        negative = str(scene.get("negative_prompt") or "").strip()
        return ";".join(filter(None, (
            f"场景概念图/环境基准:{place.strip()}",
            f"项目视觉媒介与材质:{self._scene_style_line(style)}",
            (f"AI制作圣经:{analysis_line}" if analysis_line else ""),
            f"空间功能与布局:{self._scene_environment_line(place)}",
            f"时间与天气:{time_state}",
            f"当前场景图用途:{purpose}",
            "构图:适配项目画幅的环境建立镜头，前景/主体区/背景层次清楚，"
            "留出角色进出与表演动线，机位高度和光线方向稳定，后续镜头可复用",
            "空镜:画面中不出现人物、人体局部、剪影、倒影中的人或随机路人",
            "场景只保留与剧情有关的设备、道具和陈设；所有屏幕、纸张、招牌和包装"
            "均无可读文字、字幕、Logo、水印、乱码和品牌标识",
            (f"负面约束:{negative}" if negative else ""))))

    @staticmethod
    def _sheet_view_contract(key):
        return {
            "turnaround": (
                "【REVIEW BOARD】16:9横版；左侧约1/3为清晰面部近景，"
                "右侧约2/3依次为完整全身正面、严格90度侧面、完整180度"
                "背面。纯白或中性浅灰背景，统一相机高度、焦段、水平线、"
                "柔和中性光和自然站姿。该图仅用于审核与索引；不依赖模型"
                "生成Front/Profile/Back文字，标签留给静态排版。"),
            "closeup": (
                "【CANONICAL FACE】独立高清面部近景，正面中性表情；"
                "完整保留脸型、五官比例、眼鼻嘴、发际线、耳部、下颌与"
                "颈部连接，作为后续镜头面部身份母资产。"),
            "front": (
                "【CANONICAL FRONT】独立高清全身正面；严格正对镜头，"
                "身体无偏转，从头到脚完整可见，不裁切、不遮挡。"),
            "profile": (
                "【CANONICAL PROFILE】独立高清全身严格90度真侧面；"
                "双眼不可同时完整可见，鼻唇下颌、耳朵、后脑、肩胯、"
                "服装侧缝与配饰悬挂关系必须真实；禁止四分之三视角。"),
            "back": (
                "【CANONICAL BACK】独立高清全身严格180度完整背面；"
                "不露正脸，真实展示后脑发型、肩胛、服装背片、接缝、"
                "扣件、纹样与配饰背部位置；禁止复制正面。"),
        }.get(key, "")

    def _sheet_prompt(self, name, role, style, label, desc, key=None,
                      design=None, locked_look=None):
        detail = self._design_line(
            design, keys=self.SHEET_DESIGN_KEYS.get(key))
        anchor = self._locked_look_line(locked_look)
        return (
            f"【任务】角色{label}:{name}({role})；{desc}。"
            f"{self._sheet_view_contract(key)}"
            "【IDENTITY LOCK】只以人工锁定最终立绘锁定脸型、五官骨相、"
            "年龄、性别表达、发际线、发型轮廓和身份标志；必须是同一人。"
            + (f"【人物设定】{detail}。" if detail else "")
            + (f"【WARDROBE LOCK】{anchor}；配饰有则必须保留，"
               "不可见或未声明则不得新增。" if anchor else "")
            + "【WORLD / COSTUME】造型必须服从人物背景、时代/世界观、"
            "职业、性格和剧情场合，不得套用现代都市模板。"
            f"【STYLE】{style}。"
            "【CONSISTENCY】所有母资产必须是同一个人、同一年龄、同一套"
            "服装与配饰；身高、头身比、肩胯宽度、四肢长度、五官位置、"
            "发型轮廓、服装接缝、扣件、纹样、材质、颜色和配饰位置严格"
            "对应。除本资产明确展示的视角/细节外，不得改变结构。"
            "【SKIN / STRUCTURE】肤色干净均匀，仅保留极轻微真实微纹理；"
            "依靠骨相、体积和统一光线建立真实感，不增加可见脏点、斑驳"
            "泛红、灰褐颗粒或过强毛孔；五官、手指、关节和身体比例自然。"
            f"【NEGATIVE】禁止换脸、换性别、增减人物、乱码、Logo、水印；"
            f"{WORKWEAR_RULE}{CHARACTER_BACKGROUND_RULE}")

    def _plan_path(self, ctx):
        return ctx["out_root"] / "render_plan.json"

    def _plan_read(self, ctx):
        with _PLAN_IO_LOCK:
            path = self._plan_path(ctx)
            if path.exists():
                try:
                    return json.loads(path.read_text(encoding="utf-8"))
                except ValueError:
                    pass
            return {"items": []}

    def _plan_write(self, ctx, plan):
        with _PLAN_IO_LOCK:
            plan["updated_at"] = now()
            path = self._plan_path(ctx)
            path.parent.mkdir(parents=True, exist_ok=True)
            tmp = path.with_suffix(".json.tmp")
            tmp.write_text(json.dumps(plan, ensure_ascii=False, indent=1),
                           encoding="utf-8")
            tmp.replace(path)

    def _plan_seed(self, ctx, category, items):
        """登记(或刷新)某分类要生成的全部图片;同 id 条目保留状态。"""
        plan = self._plan_read(ctx)
        old = {i["id"]: i for i in plan["items"]
               if i.get("category") == category}
        rest = [i for i in plan["items"] if i.get("category") != category]
        for item in items:
            prev = old.get(item["id"])
            item.setdefault("status", "pending")
            item.setdefault("error", "")
            same_content = (
                prev is not None
                and (
                    not item.get("content_hash")
                    or (
                        bool(prev.get("content_hash"))
                        and prev.get("content_hash")
                        == item.get("content_hash"))))
            if same_content:
                item["status"] = prev.get("status", "pending")
                item["error"] = prev.get("error", "")
                for key in ("provider", "model", "real", "fallbacks",
                            "image_task_class", "image_quality", "unit_cost",
                            "qc", "started_at", "finished_at", "duration",
                            "reference_inputs", "revision", "prompt_used",
                            "prompt_used_hash", "generation_input",
                            "prompt_contract_validation", "output_uri"):
                    if key in prev:
                        item[key] = prev[key]
                if prev.get("custom_prompt"):
                    item["prompt"] = prev.get("prompt", item["prompt"])
                    item["custom_prompt"] = True
            elif prev is not None and item.get("content_hash"):
                item["invalidated_previous_output"] = True
                item["invalidation_reason"] = (
                    "镜头剧情/人物/Q版叠层/提示词或参考图合同已变化，"
                    "旧图片不得按镜头编号继续复用")
        plan["items"] = rest + items
        self._plan_write(ctx, plan)

    def _plan_mark(self, ctx, item_id, status, error="", prompt=None,
                   only_pending=False, extra=None):
        # _plan_mark is a read-modify-write operation.  Lock the whole
        # transaction, not just each individual file operation, otherwise two
        # workers can overwrite each other's status/prompt updates.
        with _PLAN_IO_LOCK:
            plan = self._plan_read(ctx)
            for item in plan["items"]:
                if item["id"] != item_id:
                    continue
                if only_pending and item.get("status") not in ("pending",
                                                               "failed"):
                    return
                item["status"] = status
                item["error"] = error
                # 计时:生成中记起点,完成/失败记单张耗时(供前端估算剩余时间)
                if status == "generating":
                    item["started_at"] = round(time.time(), 1)
                    item.pop("finished_at", None)
                elif status in ("done", "failed", "awaiting_human") \
                        and item.get("started_at"):
                    item["finished_at"] = round(time.time(), 1)
                    item["duration"] = round(
                        item["finished_at"] - item["started_at"], 1)
                if prompt is not None and prompt != item.get("prompt"):
                    item["prompt"] = prompt
                    item["custom_prompt"] = True
                if extra:
                    item.update(extra)
                self._plan_write(ctx, plan)
                # 出错经验库:质检发现过的问题(含重画后已通过的首轮问题)
                # 自动归档,之后所有出图/视频提示词带"严禁再犯"清单
                qc = (extra or {}).get("qc") or {}
                issues = qc.get("lesson_issues") or (
                    qc.get("issues") if qc.get("passed") is False else [])
                if issues:
                    try:
                        count = record_lessons(
                            self.assets, ctx["project"]["id"], issues,
                            category=item.get("category", ""))
                        if count:
                            self.log.info(
                                "director",
                                f"经验库已归档 {count} 条出错原因"
                                f"({item.get('label', item_id)});"
                                "后续出图/视频将自动规避")
                    except Exception:
                        pass   # 经验归档失败绝不能影响生产主流程
                return

    @staticmethod
    def _prompt_with_feedback(prompt, feedback):
        prompt = (prompt or "").strip()
        feedback = (feedback or "").strip()
        return (f"{prompt}。修改意见(必须落实):{feedback}"
                if feedback else prompt)

    @staticmethod
    def _reference_inputs(payload):
        """把本次真实传入产线的参考图做成人可读清单，供手机端核验。"""
        payload = payload or {}
        manifest = payload.get("reference_manifest")
        if isinstance(manifest, list):
            rows = []
            for position, entry in enumerate(manifest, 1):
                if not isinstance(entry, dict) or not entry.get("uri"):
                    continue
                rows.append({
                    "index": int(entry.get("index") or position),
                    "kind": str(entry.get("kind") or entry.get("role")
                                or "reference"),
                    "label": str(entry.get("label") or f"参考图{position}"),
                    "name": str(entry.get("label")
                                or Path(str(entry["uri"])).name),
                    "uri": str(entry["uri"]),
                    "asset_id": entry.get("asset_id"),
                    "source": (
                        "asset_center" if entry.get("asset_id") is not None
                        else "upload"),
                    "reference_role": str(entry.get("role") or ""),
                    "attach_to": str(entry.get("character") or ""),
                    "binding": str(entry.get("binding") or ""),
                })
            return {
                "attached": bool(rows),
                "count": len(rows),
                "required": bool(payload.get("require_reference_images")),
                "schema": "aifos.reference-inputs/v2",
                "items": rows,
            }
        rows = []

        asset_by_uri = {
            str(item.get("uri")): item
            for item in (payload.get("asset_matches") or [])
            if item.get("uri")
        }

        def add(kind, uri, label=""):
            if not uri:
                return
            value = str(uri)
            if any(row["uri"] == value for row in rows):
                return
            match = asset_by_uri.get(value) or {}
            rows.append({
                "kind": kind,
                "label": match.get("label") or label or kind,
                "name": match.get("name") or Path(value).name or value,
                "uri": value,
                "asset_id": match.get("asset_id"),
                "source": "asset_center" if match else "upload",
                "reference_role": match.get("reference_role", ""),
                "attach_to": match.get("attach_to", ""),
            })

        for ref in payload.get("identity_references") or []:
            if isinstance(ref, dict):
                add("identity", ref.get("uri"),
                    f"{ref.get('character', '角色')}最终立绘")
        add(
            "inner_persona", payload.get("inner_persona_ref"),
            "内心Q版母资产")
        for uri in payload.get("character_refs") or []:
            add("character", uri, "人物设定/资产图")
        add("spatial", payload.get("spatial_ref"), "本镜空间调度图")
        add("keyframe", payload.get("image_uri"), "本镜关键图")
        add("continuity", payload.get("chain_first_uri"), "上一镜尾帧")
        add("scene", payload.get("scene_ref"), "场景概念图")
        add("style", payload.get("style_ref"), "全项目风格基准图")
        for uri in payload.get("reference_images") or []:
            add("asset" if str(uri) in asset_by_uri else "user", uri,
                "资产中心匹配图" if str(uri) in asset_by_uri else "用户参考图")
        return {"attached": bool(rows), "count": len(rows),
                "required": bool(payload.get("require_reference_images")),
                "items": rows}

    ACCELERATABLE_IMAGE_CATEGORIES = frozenset({
        "character_candidate", "character_sheet", "scene_art",
        "shot_image", "frames",
    })
    ACCELERATION_IDENTITY_CATEGORIES = frozenset({
        "character_sheet", "shot_image", "frames",
    })

    @staticmethod
    def _stable_hash(value):
        raw = json.dumps(
            value, ensure_ascii=False, sort_keys=True,
            separators=(",", ":"), default=str).encode("utf-8")
        return hashlib.sha256(raw).hexdigest()

    def _shot_content_hash(self, shot, payload=None):
        """Hash the semantic shot contract; old art may reuse only on match."""
        payload = payload or {}
        return self._stable_hash({
            "shot_no": shot.get("shot_no"),
            "scene_no": shot.get("scene_no"),
            "characters": shot.get("characters") or [],
            "narrative_overlays": shot.get("narrative_overlays") or [],
            "description": shot.get("description") or "",
            "dialogue": shot.get("dialogue"),
            "start_state": shot.get("start_state") or {},
            "end_state": shot.get("end_state") or {},
            "prompt_contract": (
                payload.get("prompt_contract")
                or shot.get("prompt_contract") or {}),
            "prompt_compact": (
                payload.get("prompt_compact")
                or shot.get("seedance_prompt_compact") or ""),
            "reference_manifest": payload.get("reference_manifest") or [],
        })

    def _build_dispatch_contract(self, task, item):
        """从即将交给 worker 的真实 payload 构造不可变预检契约。"""
        payload = json.loads(json.dumps(
            task.get("payload") or {}, ensure_ascii=False, default=str))
        prompt_used = self._prompt_with_feedback(
            payload.get("prompt_compact") or payload.get("prompt", ""),
            payload.get("feedback", ""))
        prompt = prompt_used
        refs = self._reference_inputs(payload)
        category = item.get("category", "")
        characters = [str(value) for value in payload.get("characters") or []]
        identity_rows = [
            ref for ref in payload.get("identity_references") or []
            if isinstance(ref, dict)
        ]
        identity_map = {
            str(ref.get("character")): str(ref.get("uri"))
            for ref in identity_rows
            if ref.get("character") and ref.get("uri")
        }
        issues = []
        if not prompt_used.strip():
            issues.append("最终提示词为空")
        if payload.get("shot_no") is not None:
            validation = validate_shot_prompt_contract(
                payload.get("prompt_contract") or {})
            if not validation["passed"]:
                issues.extend(
                    f"镜头生成合同冲突：{issue}"
                    for issue in validation["issues"])
        subject = str(payload.get("art_name") or payload.get("location") or "")
        expected_names = characters or ([subject] if subject else [])
        for name in expected_names:
            if name and name not in prompt_used:
                issues.append(f"提示词没有明确写出对象「{name}」")
        # 用户已明确要求：调用其他 API 时不能只交文字，所有加速项至少一图。
        if not refs["items"]:
            issues.append("API 加速必须携带真实参考图，不能只使用文字描述")
        for ref in refs["items"]:
            uri = str(ref.get("uri") or "")
            if not uri:
                issues.append(f"参考图「{ref.get('label', '未命名')}」没有文件地址")
            elif not uri.startswith(("http://", "https://", "data:image/")) \
                    and not Path(uri).is_file():
                issues.append(f"参考图文件不存在: {uri}")
        if category in self.ACCELERATION_IDENTITY_CATEGORIES:
            required_identity_names = payload.get("identity_characters")
            if required_identity_names is None:
                required_identity_names = characters
            missing = [name for name in required_identity_names
                       if name not in identity_map]
            extra = [name for name in identity_map
                     if name not in required_identity_names]
            if missing:
                issues.append("缺少人物最终立绘映射: " + "、".join(missing))
            if extra:
                issues.append("参考图人物与提示词名单不一致: " + "、".join(extra))
        if category == "frames" and not (
                payload.get("image_uri") or payload.get("chain_first_uri")):
            issues.append("首尾帧缺少本镜关键帧/上一镜尾帧连续性参考")
        reference_facts = [{
            "kind": ref.get("kind", ""),
            "label": ref.get("label", ""),
            "name": ref.get("name", ""),
            "uri": ref.get("uri", ""),
            "asset_id": ref.get("asset_id"),
            "reference_role": ref.get("reference_role", ""),
            "attach_to": ref.get("attach_to", ""),
        } for ref in refs["items"]]
        contract = {
            "schema": "aifos.image-dispatch/v1",
            "item_id": task["item_id"],
            "label": item.get("label", task["item_id"]),
            "category": category,
            "capability": task["capability"],
            "prompt": prompt,
            "prompt_hash": self._stable_hash(prompt),
            "prompt_used": prompt_used,
            "prompt_used_hash": self._stable_hash(prompt_used),
            "references": {
                "required": True,
                "count": len(reference_facts),
                "items": reference_facts,
            },
            "reference_hash": self._stable_hash(reference_facts),
            "characters": characters,
            "identity_map": identity_map,
            "base_quality": payload.get("image_quality", "medium"),
            "payload": payload,
            "issues": list(dict.fromkeys(issues)),
        }
        contract["passed"] = not contract["issues"]
        token_basis = {key: contract[key] for key in (
            "item_id", "category", "capability", "prompt_hash",
            "prompt_used_hash",
            "reference_hash", "characters", "identity_map")}
        contract["token"] = self._stable_hash(token_basis)
        return contract

    def _prepare_dispatch_contracts(self, ctx, tasks):
        plan = self._plan_read(ctx)
        by_id = {item.get("id"): item for item in plan.get("items", [])}
        records = []
        for task in tasks:
            item = by_id.get(task.get("item_id"))
            if item is None or item.get("category") \
                    not in self.ACCELERATABLE_IMAGE_CATEGORIES:
                continue
            contract = self._build_dispatch_contract(task, item)
            task["_dispatch_contract"] = contract
            task["_dispatch_contract_token"] = contract["token"]
            records.append({
                "item_id": task["item_id"],
                "category": item["category"],
                "capability": task["capability"],
                "contract_token": contract["token"],
                "contract": contract,
                "never_started": (
                    item.get("status", "pending") == "pending"
                    and not item.get("started_at")
                    and not item.get("finished_at")
                    and not item.get("provider")),
            })
        if records:
            self.image_acceleration.register(ctx["episode"]["id"], records)

    def _claim_dispatch_task(self, ctx, task):
        token = task.get("_dispatch_contract_token")
        if not token:
            return task
        request = self.image_acceleration.claim(
            ctx["episode"]["id"], task["item_id"], token)
        if request is None:
            return task
        quality = normalize_quality(
            request.get("quality") or "medium", field="image_quality")
        payload = dict(task.get("payload") or {})
        decision = dict(payload.get("quality_decision") or {})
        decision.update({
            "level": quality,
            "source": "api_acceleration",
            "reasons": ["用户将尚未开工图片批量分流到指定 API"],
        })
        payload.update({
            "image_quality": quality,
            "quality_decision": decision,
            "image_task_class": image_task_class_for(quality),
            "strict_provider": request["provider"],
            "model_override": request["model"],
            "require_reference_images": True,
        })
        # claim 后、进入 worker 前再用本任务持有的 Router 做一次硬校验；
        # 服务运行期间配置变更时宁可失败，也不允许模型/参考图静默漂移。
        self.router.validate_image_selection(
            request["provider"], task["capability"], payload,
            request["model"])
        accelerated = dict(task)
        accelerated["payload"] = payload
        accelerated["_acceleration"] = {
            "status": "running", "gate": "passed",
            "provider": request["provider"], "model": request["model"],
            "quality": quality, "contract_token": token,
        }
        return accelerated

    def _finish_dispatch_task(self, ctx, task, result=None, error=""):
        if not task.get("_dispatch_contract_token"):
            return
        self.image_acceleration.finish(
            ctx["episode"]["id"], task["item_id"],
            result={
                "provider": getattr(result, "provider", "") if result else "",
                "model": getattr(result, "model", "") if result else "",
            }, error=error)

    def _plan_run(self, ctx, item_id, fn, prompt=None, payload=None,
                  revision_source="manual"):
        """包住一次出图调用:生成中 → 完成/失败;手动停止落回排队。
        完成时记录实际使用的产线(真实/占位)与回退原因,界面透明可见。"""
        payload = payload or {}
        feedback = payload.get("feedback", "")
        if payload.get("shot_no") is not None:
            if not isinstance(payload.get("reference_manifest"), list):
                self._attach_reference_manifest(payload)
            validation = validate_shot_prompt_contract(
                payload.get("prompt_contract") or {})
            payload["prompt_contract_validation"] = validation
            if not validation["passed"]:
                raise AifosError(
                    "镜头生成合同前置校验失败，未调用生图 API："
                    + "；".join(validation["issues"]))
        generation_input = self._image_generation_input(payload)
        self._plan_mark(ctx, item_id, "generating", prompt=prompt,
                        extra={
                            "qc": None,
                            "prompt_used": generation_input["prompt"],
                            "prompt_used_hash": generation_input[
                                "prompt_hash"],
                            "generation_input": generation_input,
                            "prompt_contract_validation": payload.get(
                                "prompt_contract_validation") or {},
                            "reference_inputs": self._reference_inputs(
                                payload),
                            "revision": {
                                "source": revision_source,
                                "prompt_modified": bool(feedback),
                                "feedback": feedback,
                            },
                        })
        try:
            result = fn()
        except ProduceCancelled:
            self._plan_mark(ctx, item_id, "pending")
            raise
        except Exception as exc:
            self._plan_mark(ctx, item_id, "failed", error=str(exc)[:300])
            raise
        extra = (self._plan_done_extra(result)
                 if getattr(result, "provider", None) else None)
        self._plan_mark(ctx, item_id, "done", extra=extra)
        return result

    # ---- 图片视觉质检:生成后核对剧本要求,不合格自动带意见重画 ----
    def _image_qc_enabled(self):
        return bool(self.config.get("defaults", "image_qc", default=True))

    def _qc_retries(self):
        try:
            return max(0, min(int(self.config.get(
                "defaults", "image_qc_retries", default=1)), 3))
        except (TypeError, ValueError):
            return 1

    def _identity_references(self, project_id, characters, required=True):
        refs, missing = [], []
        for name in characters or []:
            row = self._locked_identity(project_id, name)
            if row is None:
                missing.append(name)
                continue
            refs.append({
                "character": name,
                "asset_id": row["id"],
                "uri": row["uri"],
                "version": row["version"],
            })
        if required and missing:
            raise AifosError(
                "以下角色尚未锁定最终立绘，禁止出图/质检: " + "、".join(missing))
        return refs

    def _qc_identity_references(
            self, project_id, characters, composition_contract=None,
            required=True):
        """Attach the final portrait plus the matching canonical view for QC."""
        refs = self._identity_references(
            project_id, characters, required=required)
        by_name = {
            item.get("character"): item
            for item in (composition_contract or {}).get("actors") or []
            if isinstance(item, dict) and item.get("character")
        }
        seen = {item.get("uri") for item in refs}
        for name in characters or []:
            view = (by_name.get(name) or {}).get(
                "expected_view", "front_or_three_quarter")
            keys = {
                "front_or_three_quarter": ("closeup", "front"),
                "profile": ("profile",),
                "back": ("back",),
                "back_or_over_shoulder": ("back",),
            }.get(view, ())
            for key in keys:
                row = self.assets.latest(
                    project_id, "character_sheet", f"{name}:{key}")
                if (not row or not row["uri"] or row["uri"] in seen
                        or not formal_reference_allowed(
                            self._asset_quality(row))
                        or not Path(row["uri"]).exists()):
                    continue
                refs.append({
                    "character": name,
                    "asset_id": row["id"],
                    "uri": row["uri"],
                    "version": row["version"],
                    "reference_view": key,
                })
                seen.add(row["uri"])
                break
        return refs

    def _era_exceptions(self, ctx_or_episode_id):
        """剧本声明的穿越/带入物白名单(时代判断以剧本为准)。"""
        if isinstance(ctx_or_episode_id, dict):
            script = ctx_or_episode_id.get("script")
            if script is None:
                episode = ctx_or_episode_id.get("episode") or {}
                script, _ = self.projects.latest_document(
                    episode.get("id"), "script") if episode.get("id") \
                    else (None, None)
        else:
            script, _ = self.projects.latest_document(
                int(ctx_or_episode_id), "script")
        world = (script or {}).get("story_world") or {}
        return [str(item).strip()
                for item in (world.get("sanctioned_anachronisms") or [])
                if str(item).strip()]

    def _qc_spec(self, project_id, characters, location="", action="",
                 forbid=None, require_identity=True, expected_characters=None,
                 expected_count=None, character_background=None, camera="",
                 composition_contract=None, readable_text=None,
                 physical_contract=None, physical_logic_required=False,
                 era_exceptions=None, narrative_overlays=None):
        """视觉质检规格：按角色在本镜可见角度选择核验依据。"""
        identity_characters = list(characters or [])
        visible_characters = list(
            identity_characters if expected_characters is None
            else expected_characters)
        composition_contract = (
            composition_contract
            if isinstance(composition_contract, dict)
            else {"composition_type": "standard", "actors": []})
        identity_refs = self._qc_identity_references(
            project_id, identity_characters,
            composition_contract=composition_contract,
            required=bool(identity_characters and require_identity))
        backgrounds = character_background or {}
        designs = []
        genders = {}
        for name in visible_characters:
            # 性别、年龄、物种和身份是质检硬事实，不能经过用于“防止旧
            # 外貌文字反向改脸”的 safe_design 过滤。脸仍以最终立绘为准。
            design = self._character_design(project_id, name) or {}
            merged = {
                **(design or {}),
                **(backgrounds.get(name) or {}),
                # 人工选中的候选造型才是后续服装/发型/妆容默认值；
                # 不能又回退到候选前的文字设定。
                **self._locked_look_variant(project_id, name),
            }
            line = self._design_line(merged, keys=(
                "species", "gender", "age_range", "identity", "costume",
                "hair", "makeup", "palette", "accessories",
                "era_setting", "occupation", "costume_direction",
                "signature_props", "temperament"))
            designs.append(f"{name}({line})" if line else name)
            gender = str(merged.get("gender") or merged.get("sex") or "")
            if gender:
                genders[name] = gender
        identity_required = bool(identity_characters and require_identity)
        try:
            count = (len(visible_characters) if expected_count is None
                     else int(expected_count))
        except (TypeError, ValueError):
            count = len(visible_characters)
        overlays = [
            copy.deepcopy(item)
            for item in (narrative_overlays or [])
            if isinstance(item, dict)
        ][:1]
        return {
            # Internal retry guard: replacement selectors may only resolve
            # assets from this project.  The QC prompt does not render this
            # private field.
            "_project_id": int(project_id),
            "characters": visible_characters,
            "identity_characters": identity_characters,
            "count": count,
            "narrative_overlays": overlays,
            "expected_overlay_count": len(overlays),
            "overlay_count_required": bool(overlays),
            "designs": ";".join(designs),
            "expected_genders": genders,
            "location": location or "",
            "action": action or "",
            "camera": camera or "",
            # 穿越/带入物白名单:时代判断以剧本为准,名单内物品不算错
            "era_exceptions": [str(item).strip()
                               for item in (era_exceptions or [])
                               if str(item).strip()],
            "composition_contract": composition_contract,
            "physical_contract": (physical_contract
                                   if isinstance(physical_contract, dict)
                                   else build_physical_contract({
                                       "description": action,
                                       "action": action,
                                       "camera": camera,
                                       "composition_contract": composition_contract,
                                       "location": location,
                                       "readable_text": readable_text or {},
                                   })),
            "readable_text": (readable_text if isinstance(readable_text, dict)
                               else {}),
            "identity_view_policy": "adaptive_visible_angle_v2",
            "forbid": list(forbid or []),
            "identity_references": identity_refs,
            "identity_required": identity_required,
            "identity_match_required": identity_required,
            # 性别/性别表达是身份的一部分，但单独设硬门槛，避免模型只写
            # identity_checked=true 却漏掉女角被画成男性。
            "gender_required": identity_required,
            # 即使是无人空镜也要明确核对 0 人，禁止模型擅自新增路人。
            "count_required": True,
            "physical_logic_required": bool(physical_logic_required),
            "static_frame": True,
        }

    @staticmethod
    def _qc_issue_text(issues):
        return "；".join(str(item) for item in (issues or []))

    def _image_generation_input(self, payload, qc_spec=None):
        """Freeze the actual director-side prompt/reference input for QC.

        The provider may add transport-only wording, but every image route
        receives this compact prompt plus ``feedback`` and the manifest in this
        order.  Store hashes instead of the full prompt in attempt history; the
        full current-shot prompt is sent only to the QC request that needs it.
        """
        payload = payload or {}
        if not isinstance(payload.get("reference_manifest"), list):
            self._attach_reference_manifest(payload)
        prompt_sent = self._prompt_with_feedback(
            payload.get("prompt_compact") or payload.get("prompt") or "",
            payload.get("feedback") or "")
        frame_kind = str(
            payload.get("frame_kind")
            or ("frames" if payload.get("image_uri") else "keyframe"))
        scope = {
            "item_id": str(payload.get("item_id") or ""),
            "shot_no": payload.get("shot_no"),
            "frame_kind": frame_kind,
            "art_name": str(payload.get("art_name") or ""),
        }
        manifest = copy.deepcopy(payload.get("reference_manifest") or [])
        return {
            "schema": "aifos.image-generation-input/v1",
            "scope": scope,
            "prompt": prompt_sent,
            "prompt_hash": prompt_hash(prompt_sent),
            "reference_manifest": manifest,
            "reference_hash": reference_hash(manifest),
            "input_hash": generation_input_hash(prompt_sent, manifest),
            "prompt_contract": copy.deepcopy(
                payload.get("prompt_contract") or {}),
            "quality": str(payload.get("image_quality") or ""),
            "model": str(payload.get("model_override") or ""),
            "project_id": (qc_spec or {}).get("_project_id"),
        }

    @staticmethod
    def _replace_reference_uri(payload, old_uri, new_uri):
        """Replace one submitted reference without widening its scope."""
        old_uri, new_uri = str(old_uri or ""), str(new_uri or "")
        if not old_uri or not new_uri or old_uri == new_uri:
            return False
        changed = False
        for key in (
                "spatial_ref", "image_uri", "chain_first_uri", "scene_ref",
                "style_ref"):
            if str(payload.get(key) or "") == old_uri:
                payload[key] = new_uri
                changed = True
        for key in ("character_refs", "reference_images"):
            values = list(payload.get(key) or [])
            revised = [new_uri if str(uri) == old_uri else uri
                       for uri in values]
            if revised != values:
                payload[key] = list(dict.fromkeys(revised))
                changed = True
        for ref in payload.get("identity_references") or []:
            if (isinstance(ref, dict)
                    and str(ref.get("uri") or "") == old_uri):
                ref["uri"] = new_uri
                changed = True
        return changed

    def _resolve_reference_replacement(self, qc_spec, selector):
        """Resolve only an existing same-project asset selected by id."""
        selector = selector if isinstance(selector, dict) else {}
        asset_id = selector.get("asset_id")
        if asset_id in (None, ""):
            return None
        try:
            row = self.assets.get(int(asset_id))
        except (TypeError, ValueError):
            return None
        if row is None or not row["uri"] or not Path(row["uri"]).exists():
            return None
        project_id = (qc_spec or {}).get("_project_id")
        if project_id is not None and int(row["project_id"]) != int(project_id):
            return None
        if self.assets.is_deleted(row):
            return None
        return row

    def _apply_image_reference_adjustments(
            self, payload, qc_spec, diagnostics):
        """Apply model proposals through a narrow asset-safe allowlist.

        A visual model may identify a bad reference, but it may never invent a
        URI, silently replace a locked identity, or reach into another project.
        Unsupported proposals are recorded for the problem card and force the
        retry decision to use only changes that were actually applied.
        """
        actions = reference_actions(diagnostics)
        manifest = {
            int(item.get("index")): item
            for item in (payload.get("reference_manifest") or [])
            if isinstance(item, dict) and item.get("index") is not None
        }
        protected_roles = {
            "identity", "identity_detail", "structure", "wardrobe",
        }
        removable_roles = {
            "manual", "composition", "continuity", "revision_base",
            "qc_revision_base", "style",
        }
        applied, skipped = [], []

        def skip(action, reason):
            skipped.append({
                "action": action.get("action", ""),
                "target_index": action.get("target_index"),
                "reason": reason,
            })

        for action in actions:
            kind = str(action.get("action") or "")
            if kind == "keep":
                continue
            target = manifest.get(action.get("target_index"))
            target_role = str((target or {}).get("role") or "")
            target_uri = str((target or {}).get("uri") or "")
            if kind == "drop_revision_base":
                candidates = [
                    item for item in manifest.values()
                    if str(item.get("role") or "") in (
                        "revision_base", "qc_revision_base")]
                if target is not None:
                    candidates = [target]
                removed = []
                for item in candidates:
                    uri = str(item.get("uri") or "")
                    if not uri:
                        continue
                    payload["reference_images"] = [
                        value for value in (
                            payload.get("reference_images") or [])
                        if str(value) != uri]
                    payload["asset_matches"] = [
                        value for value in (payload.get("asset_matches") or [])
                        if str(value.get("uri") or "") != uri]
                    removed.append(uri)
                if removed:
                    applied.append({
                        "action": kind,
                        "target_indices": [
                            item.get("index") for item in candidates],
                        "reason": action.get("reason") or "",
                    })
                else:
                    skip(action, "当前输入没有可移除的失败稿基底")
                continue
            if kind in ("remove", "rebind", "replace") and target is None:
                skip(action, "目标参考图编号不存在")
                continue
            if target_role in protected_roles:
                skip(action, "锁定人物身份/人物母资产禁止自动移除、换绑或替换")
                continue
            if kind == "remove":
                if target_role not in removable_roles:
                    skip(action, f"{target_role or '未标注'}参考图不是可自动移除的弱引用")
                    continue
                payload["reference_images"] = [
                    value for value in (payload.get("reference_images") or [])
                    if str(value) != target_uri]
                payload["asset_matches"] = [
                    value for value in (payload.get("asset_matches") or [])
                    if str(value.get("uri") or "") != target_uri]
                for field in (
                        "image_uri", "chain_first_uri", "style_ref"):
                    if str(payload.get(field) or "") == target_uri:
                        payload[field] = ""
                applied.append({
                    "action": kind, "target_index": target.get("index"),
                    "role": target_role, "reason": action.get("reason") or "",
                })
                continue
            if kind == "rebind":
                matches = list(payload.get("asset_matches") or [])
                match = next((
                    item for item in matches
                    if str(item.get("uri") or "") == target_uri), None)
                if match is None:
                    match = {"asset_id": target.get("asset_id"),
                             "kind": target.get("kind"),
                             "name": target.get("label"),
                             "label": target.get("label"),
                             "uri": target_uri}
                    matches.append(match)
                match["reference_role"] = (
                    action.get("role") or target_role or "manual")
                match["attach_to"] = action.get("character") or ""
                payload["asset_matches"] = matches
                applied.append({
                    "action": kind, "target_index": target.get("index"),
                    "role": match["reference_role"],
                    "character": match["attach_to"],
                    "reason": action.get("reason") or "",
                })
                continue
            if kind in ("replace", "add"):
                row = self._resolve_reference_replacement(
                    qc_spec, action.get("replacement_selector"))
                if row is None:
                    skip(action, "替换资产未提供有效的本项目已有 asset_id")
                    continue
                new_uri = str(row["uri"])
                role = str(action.get("role") or target_role or "manual")
                character = str(action.get("character") or "")
                if kind == "replace":
                    if not self._replace_reference_uri(
                            payload, target_uri, new_uri):
                        skip(action, "目标参考图未在实际提交字段中找到")
                        continue
                    payload["asset_matches"] = [
                        item for item in (payload.get("asset_matches") or [])
                        if str(item.get("uri") or "") != target_uri]
                else:
                    refs = list(payload.get("reference_images") or [])
                    if new_uri not in [str(value) for value in refs]:
                        refs.append(new_uri)
                        payload["reference_images"] = refs
                matches = list(payload.get("asset_matches") or [])
                matches.append({
                    "asset_id": row["id"], "kind": row["kind"],
                    "name": row["name"], "label": row["name"],
                    "uri": new_uri, "reference_role": role,
                    "attach_to": character,
                })
                payload["asset_matches"] = matches
                applied.append({
                    "action": kind,
                    "target_index": (target or {}).get("index"),
                    "asset_id": row["id"], "role": role,
                    "character": character,
                    "reason": action.get("reason") or "",
                })
                continue
            skip(action, "不支持的参考图调整动作")
        return {"applied": applied, "skipped": skipped}

    @staticmethod
    def _use_failed_image_as_revision_base(diagnostics):
        """Do not let a bad identity/count output poison the next attempt."""
        reference_status = str(
            (diagnostics.get("reference_diagnosis") or {}).get(
                "status") or "").lower()
        if reference_status in {
                "conflicting", "needs_adjustment", "missing"}:
            return False
        categories = {
            str(value).strip().lower()
            for value in (
                (diagnostics.get("image_error") or {}).get("categories")
                or [])
        }
        if categories & {
                "identity", "gender", "count", "species", "identity_drift",
                "person_count"}:
            return False
        return not any(
            action.get("action") == "drop_revision_base"
            for action in reference_actions(diagnostics))

    def _assess_image_qc(self, qc_spec, verdict, attempts):
        """Separate rendered-image truth from prompt/reference contract truth."""
        verdict = verdict or {}
        input_diagnosis = normalize_generation_diagnostics(
            verdict, issues=verdict.get("issues"))
        identity_required = bool(qc_spec.get("identity_required"))
        raw_identity_checks = verdict.get("identity_checks")
        identity_checks = [
            dict(item) for item in (raw_identity_checks or [])
            if isinstance(item, dict) and item.get("character")
        ] if isinstance(raw_identity_checks, list) else []
        if identity_required and identity_checks:
            by_name = {str(item["character"]): item
                       for item in identity_checks}
            expected_names = [
                str(name) for name in (
                    qc_spec.get("identity_characters") or [])]
            identity_checked = all(
                name in by_name and bool(by_name[name].get("checked"))
                for name in expected_names)
            identity_match = all(
                name in by_name and bool(by_name[name].get("match"))
                for name in expected_names)
        else:
            identity_checked = (
                not identity_required
                or bool(verdict.get("identity_checked")))
            identity_match = (
                not identity_required
                or bool(verdict.get("identity_match")))
        gender_required = bool(qc_spec.get("gender_required"))
        gender_checked = (
            not gender_required or bool(verdict.get("gender_checked")))
        gender_match = (
            not gender_required or bool(verdict.get("gender_match")))
        count_required = bool(qc_spec.get("count_required"))
        count_checked = (
            not count_required or bool(verdict.get("count_checked")))
        count_match = (
            not count_required or bool(verdict.get("count_match")))
        overlay_required = bool(qc_spec.get("overlay_count_required"))
        overlay_checked = (
            not overlay_required
            or bool(verdict.get("overlay_count_checked")))
        overlay_match = (
            not overlay_required
            or bool(verdict.get("overlay_count_match")))
        physical_required = bool(qc_spec.get("physical_logic_required"))
        physical_checked = (
            not physical_required
            or bool(verdict.get("physical_logic_checked")))
        physical_match = (
            not physical_required
            or bool(verdict.get("physical_logic_match")))
        spatial_checked = (
            not physical_required
            or bool(verdict.get("spatial_logic_checked")))
        spatial_match = (
            not physical_required
            or bool(verdict.get("spatial_logic_match")))
        issues = [str(item) for item in (verdict.get("issues") or [])]
        if not identity_checked:
            issues.append("质检未确认已逐人比对最终立绘")
        elif not identity_match:
            issues.append("画面人物身份与锁定最终立绘不一致")
        if not gender_checked:
            issues.append("质检未单独核对人物性别/性别表达")
        elif not gender_match:
            issues.append("人物性别/性别表达与锁定最终立绘不一致")
        if not count_checked:
            issues.append("质检未核对画面实际人数")
        elif not count_match:
            detected = verdict.get("detected_count")
            suffix = f"，检测到{detected}人" if detected is not None else ""
            issues.append(
                f"画面人数与要求的{qc_spec.get('count', 0)}人不一致{suffix}")
        if not overlay_checked:
            issues.append("质检未单独核对非现实内心Q版叠层数量")
        elif not overlay_match:
            detected = verdict.get("detected_overlay_count")
            suffix = f"，检测到{detected}个" if detected is not None else ""
            issues.append(
                "内心Q版叠层与要求的"
                f"{qc_spec.get('expected_overlay_count', 0)}个不一致{suffix}")
        if not physical_checked:
            issues.append("质检未核对道具、人物与镜头的物理关系")
        elif not physical_match:
            issues.append("画面存在物理逻辑错误或道具使用关系不成立")
        if not spatial_checked:
            issues.append("质检未核对人物、道具、镜头的空间关系")
        elif not spatial_match:
            issues.append("人物、道具与镜头的相对位置/朝向不成立")
        issues = list(dict.fromkeys(issues))
        text = self._qc_issue_text(issues).lower()
        mismatch_words = (
            "人物不一致", "人物形象", "身份", "同一个人", "脸不一致",
            "性别", "男性", "女性", "人数", "数量", "多余人物",
            "缺失人物", "新增人物", "重复人物", "person count",
            "identity", "gender", "sex mismatch",
        )
        visual_hard_failure = (
            not identity_checked or not identity_match
            or not gender_checked or not gender_match
            or not count_checked or not count_match
            or not overlay_checked or not overlay_match
            or not physical_checked or not physical_match
            or not spatial_checked or not spatial_match
            or (not identity_checks
                and any(word in text for word in mismatch_words)))
        prompt_status = str(
            input_diagnosis["prompt_diagnosis"].get("status") or "").lower()
        reference_status = str(
            input_diagnosis["reference_diagnosis"].get("status")
            or "").lower()
        if "input_contract_pass" in verdict:
            input_contract_passed = bool(verdict["input_contract_pass"])
        elif prompt_status != "unknown" or reference_status != "unknown":
            input_contract_passed = (
                prompt_status == "correct" and reference_status == "correct")
        else:
            input_contract_passed = bool(verdict.get("pass"))
        visual_checks_passed = (
            identity_checked and identity_match
            and gender_checked and gender_match
            and count_checked and count_match
            and overlay_checked and overlay_match
            and physical_checked and physical_match
            and spatial_checked and spatial_match)
        if "visual_pass" in verdict:
            image_passed = (
                bool(verdict["visual_pass"]) and not visual_hard_failure)
        else:
            image_passed = (
                bool(verdict.get("pass")) and not visual_hard_failure)
        passed = (
            image_passed and input_contract_passed
            and visual_checks_passed)
        report = {
            "passed": passed,
            "issues": issues,
            "attempts": attempts,
            "identity_checked": identity_checked,
            "identity_match": identity_match,
            "gender_checked": gender_checked,
            "gender_match": gender_match,
            "count_checked": count_checked,
            "count_match": count_match,
            "overlay_count_checked": overlay_checked,
            "overlay_count_match": overlay_match,
            "physical_logic_checked": physical_checked,
            "physical_logic_match": physical_match,
            "spatial_logic_checked": spatial_checked,
            "spatial_logic_match": spatial_match,
            "detected_count": verdict.get("detected_count"),
            "detected_overlay_count": verdict.get(
                "detected_overlay_count"),
            "expected_overlay_count": qc_spec.get(
                "expected_overlay_count", 0),
            "narrative_overlays": qc_spec.get(
                "narrative_overlays") or [],
            "identity_references": len(
                qc_spec.get("identity_references") or []),
            "identity_checks": identity_checks,
            "composition_contract": qc_spec.get(
                "composition_contract") or {},
            "physical_contract": qc_spec.get("physical_contract") or {},
            "image_passed": image_passed,
            "visual_pass": image_passed,
            "input_contract_passed": input_contract_passed,
            "input_contract_pass": input_contract_passed,
            "redraw_required": not image_passed,
            "contract_repair_required": not input_contract_passed,
            "production_ready": passed,
            "hard_failure": bool(visual_hard_failure and not image_passed),
        }
        report.update({
            "input_diagnosis": input_diagnosis,
            "image_error": input_diagnosis["image_error"],
            "prompt_diagnosis": input_diagnosis["prompt_diagnosis"],
            "reference_diagnosis": input_diagnosis[
                "reference_diagnosis"],
            "targeted_prompt_patch": input_diagnosis[
                "targeted_prompt_patch"],
            "reference_adjustments": input_diagnosis[
                "reference_adjustments"],
            "diagnosis_complete": input_diagnosis[
                "diagnosis_complete"],
        })
        return report

    def _generate_image_with_qc(self, capability, payload, out_dir,
                                cancel, qc_spec):
        """出图 + 视觉质检 + 不合格自动重画(worker 线程安全:只调产线)。
        质检产线不可用/出错时失败关闭并保留图片待人工处理；结果附在
        result.qc，未通过的图片不得进入后续正式参考链。"""
        attempts = 0
        spent = 0.0
        attempt_history = []
        payload = copy.deepcopy(payload or {})
        if not isinstance(payload.get("reference_manifest"), list):
            self._attach_reference_manifest(payload)
        lesson_issues = []
        while True:
            generation_input = self._image_generation_input(
                payload, qc_spec=qc_spec)
            result = self.router.call(capability, payload, out_dir,
                                      cancel=cancel)
            generation_cost = float(result.cost or 0.0)
            result.cost += spent
            if not qc_spec or not self._image_qc_enabled():
                return result
            uri = result.uri
            if not uri or not Path(uri).exists():
                return result
            history_row = {
                "attempt": attempts + 1,
                "generated_at": now(),
                "provider": result.provider,
                "model": getattr(result, "model", "") or (
                    (getattr(result, "data", {}) or {}).get("model") or ""),
                "output_uri": uri,
                "prompt_hash": generation_input["prompt_hash"],
                "reference_hash": generation_input["reference_hash"],
                "input_hash": generation_input["input_hash"],
                "generation_cost": generation_cost,
                "applied_changes": copy.deepcopy(
                    payload.get("_applied_qc_changes") or []),
            }
            try:
                qc_payload = {
                    **qc_spec,
                    "image_uri": uri,
                    "generation_input": generation_input,
                    "generation_prompt": generation_input["prompt"],
                    # The multimodal QC provider uploads this exact full set,
                    # not only the final character portraits.
                    "reference_manifest": generation_input[
                        "reference_manifest"],
                }
                qc_result = self.router.call(
                    "image_qc", qc_payload, out_dir,
                    cancel=cancel)
            except (ProviderUnavailable, ProviderError) as exc:
                # 人物镜头不能在质检故障时静默放行。保留已生成图片供人工
                # 查看，但明确标成质检未过，后续不得当作正式参考图。
                diagnostics = normalize_generation_diagnostics({
                    "pass": False,
                    "issues": [f"质检产线不可用，图片未放行:{exc}"],
                })
                revision = optimize_qc_feedback(
                    [f"质检产线不可用，图片未放行:{exc}"], mode="image",
                    readable_text=qc_spec.get("readable_text"),
                    diagnostics=diagnostics)
                history_row.update({
                    "qc_passed": False,
                    "qc_cost": 0.0,
                    "image_error": diagnostics["image_error"],
                    "prompt_diagnosis": diagnostics["prompt_diagnosis"],
                    "reference_diagnosis": diagnostics[
                        "reference_diagnosis"],
                })
                attempt_history.append(history_row)
                result.qc = {
                    "passed": False,
                    "issues": [f"质检产线不可用，图片未放行:{exc}"],
                    "revision_feedback": revision["text"],
                    "revision_categories": revision["categories"],
                    "attempts": attempts + 1,
                    "identity_checked": False,
                    "gender_checked": False,
                    "gender_match": False,
                    "count_checked": False,
                    "count_match": False,
                    "physical_logic_checked": False,
                    "physical_logic_match": False,
                    "spatial_logic_checked": False,
                    "spatial_logic_match": False,
                    "hard_failure": True,
                    "image_passed": False,
                    "visual_pass": False,
                    "input_contract_passed": False,
                    "input_contract_pass": False,
                    "redraw_required": False,
                    "contract_repair_required": True,
                    "production_ready": False,
                    "identity_references": len(
                        qc_spec.get("identity_references") or []),
                    "input_diagnosis": diagnostics,
                    "image_error": diagnostics["image_error"],
                    "prompt_diagnosis": diagnostics["prompt_diagnosis"],
                    "reference_diagnosis": diagnostics[
                        "reference_diagnosis"],
                    "diagnosis_complete": False,
                    "retry_blocked": True,
                    "retry_blocked_reason": (
                        "质检产线未返回提示词与参考图诊断，禁止盲目重试"),
                    "attempt_history": attempt_history,
                    "generation_input": copy.deepcopy(generation_input),
                }
                return result
            result.cost += qc_result.cost
            report = self._assess_image_qc(
                qc_spec, qc_result.data or {}, attempts + 1)
            revision = optimize_qc_feedback(
                report["issues"], mode="image",
                readable_text=qc_spec.get("readable_text"),
                diagnostics=report["input_diagnosis"])
            report["revision_feedback"] = revision["text"]
            report["revision_categories"] = revision["categories"]
            history_row.update({
                "qc_passed": bool(report["passed"]),
                "qc_cost": float(qc_result.cost or 0.0),
                "image_error": copy.deepcopy(report["image_error"]),
                "prompt_diagnosis": copy.deepcopy(
                    report["prompt_diagnosis"]),
                "reference_diagnosis": copy.deepcopy(
                    report["reference_diagnosis"]),
            })
            attempt_history.append(history_row)
            report["attempt_history"] = copy.deepcopy(attempt_history)
            report["generation_input"] = copy.deepcopy(generation_input)
            # 经验库素材:重画后最终通过的图,第一次犯的错也要记住
            if not report["passed"]:
                lesson_issues.extend(report["issues"])
            report["lesson_issues"] = list(dict.fromkeys(lesson_issues))
            result.qc = report
            if (report["passed"] or not report["redraw_required"]
                    or attempts >= self._qc_retries()):
                return result
            diagnostics = report["input_diagnosis"]
            proposed = retry_input_decision(
                generation_input["prompt"],
                generation_input["reference_manifest"],
                diagnostics,
                previous_input_hash=(
                    attempt_history[-2]["input_hash"]
                    if len(attempt_history) > 1 else ""))
            next_payload = copy.deepcopy(payload)
            reference_changes = self._apply_image_reference_adjustments(
                next_payload, qc_spec, diagnostics)
            patch = targeted_prompt_patch(diagnostics)
            if patch:
                old_feedback = str(next_payload.get("feedback") or "").strip()
                next_payload["feedback"] = (
                    f"{old_feedback}\n{patch}" if old_feedback else patch
                )[:2400]
            if ((patch or reference_changes["applied"])
                    and self._use_failed_image_as_revision_base(diagnostics)):
                references = [uri]
                references.extend(next_payload.get("reference_images") or [])
                next_payload["reference_images"] = list(
                    dict.fromkeys(references))
                matches = [
                    match for match in (
                        next_payload.get("asset_matches") or [])
                    if match.get("uri") != uri]
                matches.append({
                    "asset_id": None, "kind": "qc_revision_base",
                    "name": f"qc_attempt_{attempts + 1}",
                    "label": "质检未过的待修改基底", "uri": uri,
                    "reference_role": "revision_base",
                })
                next_payload["asset_matches"] = matches
            next_payload["qc_revision"] = revision
            next_payload["qc_attempt"] = attempts + 1
            next_payload["revision_mode"] = "targeted_qc_fix"
            next_payload["source_qc_uri"] = uri
            self._attach_reference_manifest(next_payload)
            next_payload["require_reference_images"] = bool(
                next_payload.get("reference_manifest"))
            next_input = self._image_generation_input(
                next_payload, qc_spec=qc_spec)
            actual_prompt_changed = (
                next_input["prompt_hash"] != generation_input["prompt_hash"])
            actual_references_changed = (
                next_input["reference_hash"]
                != generation_input["reference_hash"])
            actual_changes = {
                "prompt_changed": actual_prompt_changed,
                "references_changed": actual_references_changed,
                "prompt_patch": patch,
                "reference_changes": reference_changes["applied"],
                "skipped_reference_changes": reference_changes["skipped"],
                "previous_input_hash": generation_input["input_hash"],
                "next_input_hash": next_input["input_hash"],
            }
            blocked_reason = ""
            if not report.get("diagnosis_complete"):
                blocked_reason = (
                    "质检未完整分析实际提示词和参考图，禁止盲目原样重试")
            elif not (actual_prompt_changed or actual_references_changed):
                blocked_reason = (
                    "定向修订后实际提示词与参考图均未变化，禁止原样重试")
            elif next_input["input_hash"] == generation_input["input_hash"]:
                blocked_reason = "修订后的生成输入与失败输入完全相同"
            proposed.update({
                "prompt_changed": actual_prompt_changed,
                "references_changed": actual_references_changed,
                "changes_input": bool(
                    actual_prompt_changed or actual_references_changed),
                "next_input_hash": next_input["input_hash"],
                "retry_blocked": bool(blocked_reason),
                "retry_blocked_reason": blocked_reason,
            })
            report["retry_decision"] = proposed
            report["applied_changes"] = actual_changes
            report["retry_blocked"] = bool(blocked_reason)
            report["retry_blocked_reason"] = blocked_reason
            history_row["applied_changes_for_next_attempt"] = copy.deepcopy(
                actual_changes)
            report["attempt_history"] = copy.deepcopy(attempt_history)
            result.qc = report
            if blocked_reason:
                return result
            spent = result.cost
            attempts += 1
            next_payload["_applied_qc_changes"] = [
                {
                    "source_attempt": attempts,
                    "prompt_changed": actual_prompt_changed,
                    "references_changed": actual_references_changed,
                    "prompt_patch": patch,
                    "reference_changes": reference_changes["applied"],
                }
            ]
            payload = next_payload

    def _plan_done_extra(self, result):
        extra = {"provider": result.provider,
                 "real": result.provider != "mock",
                 "fallbacks": getattr(result, "fallbacks", [])}
        uri = getattr(result, "uri", "")
        if uri:
            # 二次质检仍未通过的图片不会登记为正式资产，但必须保留
            # 最终失败稿，供问题清单预览和人工定向修改。
            extra["output_uri"] = uri
        data = getattr(result, "data", {}) or {}
        for key in ("first_source", "generation_calls", "model",
                    "image_task_class", "image_quality", "unit_cost",
                    "codex_profile"):
            if key in data:
                extra[key] = data[key]
        model = getattr(result, "model", "")
        if model and "model" not in extra:
            extra["model"] = model
        qc = getattr(result, "qc", None)
        if qc is not None:
            qc = dict(qc)
            if qc.get("passed") is False:
                attempts = int(qc.get("attempts") or 1)
                qc.update({
                    "awaiting_human": True,
                    "auto_repairs": max(0, attempts - 1),
                    "auto_repair_exhausted": True,
                })
            extra["qc"] = qc
        return extra

    @staticmethod
    def _critical_qc_error(result):
        qc = getattr(result, "qc", None) or {}
        if qc.get("passed") is False:
            if (qc.get("contract_repair_required")
                    and not qc.get("redraw_required")):
                return (
                    "图片画面未判错，但生成输入合同未通过；"
                    "已停止自动重画，请先修正提示词/参考图绑定后复检:"
                    + "；".join(qc.get("issues") or []))
            return (
                "图片质检未通过；已自动定向修图 "
                f"{qc.get('attempts', 1) - 1} 次后仍失败:"
                + "；".join(qc.get("issues") or []))
        return ""

    def _run_one_task(self, ctx, task, *, continue_on_qc_failure=False):
        """串行执行单个出图任务(含质检),记账并更新清单。"""
        if self._cancel_requested(ctx):
            raise ProduceCancelled("已手动停止生成")
        if task.get("capability") in ("image", "frames", "cover"):
            self._attach_reference_manifest(task["payload"])
        try:
            task = self._claim_dispatch_task(ctx, task)
        except Exception as exc:
            self._finish_dispatch_task(ctx, task, error=str(exc))
            self._plan_mark(ctx, task["item_id"], "failed",
                            error=str(exc)[:300])
            raise
        payload = task.get("payload") or {}
        generating_extra = {
            "image_task_class": payload.get("image_task_class"),
            "image_quality": payload.get("image_quality"),
            "reference_inputs": self._reference_inputs(payload),
        }
        if task.get("_codex_profile"):
            generating_extra["codex_profile"] = task["_codex_profile"]
        if task.get("_acceleration"):
            generating_extra["acceleration"] = task["_acceleration"]
        self._plan_mark(ctx, task["item_id"], "generating",
                        extra=generating_extra)
        try:
            result = self._generate_image_with_qc(
                task["capability"], task["payload"],
                ctx["out_root"] / task["sub_dir"],
                lambda: self._cancel_requested(ctx), task.get("qc_spec"))
        except ProduceCancelled:
            self._finish_dispatch_task(ctx, task, error="已手动停止生成")
            self._plan_mark(ctx, task["item_id"], "pending")
            raise
        except Exception as exc:
            self._finish_dispatch_task(ctx, task, error=str(exc))
            self._plan_mark(ctx, task["item_id"], "failed",
                            error=str(exc)[:300])
            raise
        if task.get("_codex_profile"):
            result.data.setdefault("codex_profile", task["_codex_profile"])
        self._task_cost += result.cost
        self._task_providers.add(result.provider)
        self.projects.add_episode_cost(ctx["episode"]["id"], result.cost)
        critical_error = self._critical_qc_error(result)
        if critical_error:
            self._finish_dispatch_task(ctx, task, error=critical_error)
            self._plan_mark(
                ctx, task["item_id"],
                ("awaiting_human" if continue_on_qc_failure else "failed"),
                error=critical_error[:300],
                extra=self._plan_done_extra(result))
            if continue_on_qc_failure:
                return None
            raise AifosError(critical_error)
        self._finish_dispatch_task(ctx, task, result=result)
        self._plan_mark(ctx, task["item_id"], "done",
                        extra=self._plan_done_extra(result))
        return result

    def _parallel_workers(self):
        try:
            workers = int(self.config.get(
                "defaults", "parallel_images", default=3))
        except (TypeError, ValueError):
            workers = 3
        return max(1, min(workers, 8))

    def _codex_parallel_profiles(self):
        """返回可安全用于本批图片的 Codex 登录态配置。"""
        profile_reader = getattr(self.config, "codex_profiles", None)
        if callable(profile_reader):
            profiles = profile_reader() or []
        else:
            profiles = (self.config.get("codex_profiles", default=None)
                        or self.config.get(
                            "codex_parallel", "profiles", default=[])
                        or [])
        ready = []
        for index, profile in enumerate(profiles):
            if not isinstance(profile, dict):
                continue
            if not bool(profile.get("enabled", False)):
                continue
            home_value = str(profile.get("codex_home") or "").strip()
            if not home_value or not Path(home_value).expanduser().is_dir():
                continue
            profile_id = str(profile.get("id") or (
                "codex_a" if index == 0 else f"codex_{index + 1}"))
            ready.append({
                "id": profile_id,
                "name": str(profile.get("name") or profile_id),
            })
        return ready

    def _assign_codex_profiles(self, tasks):
        """按轮询把独立图片任务分配到可用 Codex 登录态。"""
        profiles = self._codex_parallel_profiles()
        if not profiles:
            return tasks
        next_index = 0
        for task in tasks:
            if task.get("capability") not in ("image", "frames", "cover"):
                continue
            payload = task.get("payload")
            if not isinstance(payload, dict):
                continue
            strict = str(payload.get("strict_provider") or "").strip()
            if strict and strict != "codex":
                continue
            if payload.get("_codex_profile"):
                continue
            profile = profiles[next_index % len(profiles)]
            next_index += 1
            payload["_codex_profile"] = profile["id"]
            task["_codex_profile"] = profile["id"]
            task["worker"] = f"codex:{profile['id']}"
        return tasks

    def _video_parallel_workers(self):
        """Seedance 同时生成的镜头数；默认 4 路，避免串行等待。"""
        try:
            workers = int(self.config.get(
                "defaults", "parallel_videos", default=4))
        except (TypeError, ValueError):
            workers = 4
        return max(1, min(workers, 8))

    def _run_parallel(self, ctx, tasks, line="出图产线",
                      *, continue_on_qc_failure=False):
        """有界并行出图:只把 worker 数量的任务标为生成中。

        多人/文字/场首等高风险镜头可通过 priority 提前；尚未真正开工的
        条目保持 pending，因此计时与暂停后的恢复都反映真实状态。
        worker 线程只做产线调用;记账/资产登记/清单状态全在主线程。
        tasks: [{"item_id","capability","payload","sub_dir","tag","priority"}]
        默认返回 {tag: ProviderResult};暂停时未完成条目回到排队并保留
        已完成。continue_on_qc_failure=True 时，二次视觉质检失败的单项
        标为 awaiting_human 并继续补投剩余任务，返回
        (results, qc_failures)，真正的 Provider/预算错误仍会中止整批。"""
        if not tasks:
            return ({}, []) if continue_on_qc_failure else {}
        self._assign_codex_profiles(tasks)
        self._prepare_dispatch_contracts(ctx, tasks)
        tasks = sorted(tasks, key=lambda task: (
            -int(task.get("priority", 0)), str(task.get("item_id", ""))))
        workers = self._parallel_workers()
        # When two independent Codex logins are ready, they are the effective
        # image-production ceiling.  Do not create eight queued subprocess
        # workers just because the generic image slot setting is eight; that
        # would make the UI and the actual provider concurrency disagree.
        codex_profiles = self._codex_parallel_profiles()
        if codex_profiles:
            workers = min(workers, len(codex_profiles))
        if workers == 1 or len(tasks) == 1:
            out, qc_failures = {}, []
            for task in tasks:
                result = self._run_one_task(
                    ctx, task,
                    continue_on_qc_failure=continue_on_qc_failure)
                if result is None:
                    qc_failures.append((
                        task, AifosError(
                            "图片二次质检仍未通过，等待人工修改")))
                    continue
                out[task["tag"]] = result
            return ((out, qc_failures)
                    if continue_on_qc_failure else out)
        if self._cancel_requested(ctx):
            raise ProduceCancelled("已手动停止生成")
        episode = self.projects.get_episode(ctx["episode"]["id"])
        budget = self.config.get("budget", "per_episode", default=0)
        if budget and episode["cost"] >= budget:
            raise BudgetExceeded(
                f"单集成本 {episode['cost']:.2f} 已达预算 {budget},停止调度")
        from concurrent.futures import FIRST_COMPLETED, ThreadPoolExecutor, wait
        self.log.info(
            "director",
            f"{line}并行开工:共 {len(tasks)} 张,{workers} 路同时生成")
        cancel = lambda: self._cancel_requested(ctx)   # noqa: E731
        results, failures, qc_failures = {}, [], []
        cancelled = False
        started_at = time.monotonic()
        with ThreadPoolExecutor(max_workers=workers) as pool:
            iterator = iter(tasks)
            futures = {}

            def submit_next():
                while True:
                    try:
                        task = next(iterator)
                    except StopIteration:
                        return False
                    try:
                        task = self._claim_dispatch_task(ctx, task)
                    except Exception as exc:
                        failures.append((task, exc))
                        self._finish_dispatch_task(ctx, task, error=str(exc))
                        self._plan_mark(ctx, task["item_id"], "failed",
                                        error=str(exc)[:300])
                        return False
                    payload = task.get("payload") or {}
                    generating_extra = {
                        "image_task_class": payload.get("image_task_class"),
                        "image_quality": payload.get("image_quality"),
                        "reference_inputs": self._reference_inputs(payload),
                    }
                    if task.get("_codex_profile"):
                        generating_extra["codex_profile"] = task[
                            "_codex_profile"]
                    if task.get("_acceleration"):
                        generating_extra["acceleration"] = task[
                            "_acceleration"]
                    self._plan_mark(ctx, task["item_id"], "generating",
                                    extra=generating_extra)
                    future = pool.submit(
                        self._generate_image_with_qc, task["capability"],
                        task["payload"], ctx["out_root"] / task["sub_dir"],
                        cancel, task.get("qc_spec"))
                    futures[future] = task
                    return True

            for _ in range(min(workers, len(tasks))):
                submit_next()
            while futures:
                done_now, _ = wait(set(futures), timeout=2,
                                   return_when=FIRST_COMPLETED)
                for future in done_now:
                    task = futures.pop(future)
                    try:
                        result = future.result()
                    except ProduceCancelled:
                        cancelled = True
                        self._finish_dispatch_task(
                            ctx, task, error="已手动停止生成")
                        self._plan_mark(ctx, task["item_id"], "pending")
                        continue
                    except Exception as exc:
                        failures.append((task, exc))
                        self._finish_dispatch_task(ctx, task, error=str(exc))
                        self._plan_mark(ctx, task["item_id"], "failed",
                                        error=str(exc)[:300])
                        continue
                    if task.get("_codex_profile"):
                        result.data.setdefault(
                            "codex_profile", task["_codex_profile"])
                    self._task_cost += result.cost
                    self._task_providers.add(result.provider)
                    self.projects.add_episode_cost(
                        ctx["episode"]["id"], result.cost)
                    critical_error = self._critical_qc_error(result)
                    if critical_error:
                        error = AifosError(critical_error)
                        target = (qc_failures if continue_on_qc_failure
                                  else failures)
                        target.append((task, error))
                        self._finish_dispatch_task(
                            ctx, task, error=critical_error)
                        self._plan_mark(
                            ctx, task["item_id"],
                            ("awaiting_human"
                             if continue_on_qc_failure else "failed"),
                            error=critical_error[:300],
                            extra=self._plan_done_extra(result))
                        continue
                    self._finish_dispatch_task(ctx, task, result=result)
                    self._plan_mark(ctx, task["item_id"], "done",
                                    extra=self._plan_done_extra(result))
                    results[task["tag"]] = result
                if not cancelled and not failures \
                        and not self._cancel_requested(ctx):
                    while len(futures) < workers and submit_next():
                        pass
        elapsed = max(.001, time.monotonic() - started_at)
        self.log.info(
            "director", f"{line}本批完成 {len(results)}/{len(tasks)}，"
            f"墙钟 {elapsed:.1f}s，吞吐 {len(results) * 60 / elapsed:.2f} 张/分钟")
        if cancelled or self._cancel_requested(ctx):
            raise ProduceCancelled(
                "已手动暂停(本批已完成的图片全部保留)")
        if failures:
            raise failures[0][1]
        if continue_on_qc_failure:
            return results, qc_failures
        return results

    def image_acceleration_options(self, project_title, episode_number):
        """当前 stage 尚未进入 worker 的图片与可选 API/模型。"""
        project, episode = self._episode_ctx(project_title, episode_number)
        ctx = {"project": dict(project), "episode": dict(episode),
               "out_root": self._episode_dir(project, episode)}
        plan = self._plan_read(ctx)
        plan_by_id = {item.get("id"): item for item in plan.get("items", [])}
        asset_policy = self.character_asset_policy(episode["id"])
        rows = self.image_acceleration.list(episode["id"])
        items = []
        for row in rows:
            contract = row.get("contract") or {}
            if (row["category"] == "character_sheet"
                    and not asset_policy["generate_sheets"]):
                continue
            item = plan_by_id.get(row["item_id"])
            # 切换简化模式或刷新计划后，旧人物套件契约仍保留审计历史，
            # 但绝不能继续出现在可加速队列里。
            if item is None:
                continue
            issues = list(contract.get("issues") or [])
            plan_pending = item.get("status") == "pending"
            if row["acceleration_status"] in ("queued", "running"):
                status = row["acceleration_status"]
            elif row["acceleration_status"] in ("done", "failed"):
                status = row["acceleration_status"]
            elif row["production_state"] != "pending" or not plan_pending:
                status = ("completed" if row["production_state"] == "generated"
                          else "in_production")
                issues = []
            elif not row["never_started"]:
                status = "retry_only"
                issues.append("图片曾经进入过生产线，只能按重试流程处理")
            else:
                status = "blocked" if issues else "ready"
            items.append({
                "item_id": row["item_id"],
                "label": contract.get("label", item.get("label", row["item_id"])),
                "category": row["category"],
                "capability": row["capability"],
                "status": status,
                "production_state": row["production_state"],
                "contract_token": row["contract_token"],
                "prompt": contract.get("prompt", ""),
                "prompt_hash": contract.get("prompt_hash", ""),
                "prompt_used": contract.get("prompt_used", contract.get("prompt", "")),
                "prompt_used_hash": contract.get("prompt_used_hash", ""),
                "references": contract.get("references") or {
                    "required": True, "count": 0, "items": []},
                "characters": contract.get("characters") or [],
                "identity_map": contract.get("identity_map") or {},
                "base_quality": contract.get("base_quality", "medium"),
                "issues": list(dict.fromkeys(issues)),
                "requested_provider": row["requested_provider"],
                "requested_model": row["requested_model"],
                "requested_quality": row["requested_quality"],
                "actual_provider": row["actual_provider"],
                "actual_model": row["actual_model"],
                "error": row["error"],
            })
        providers = self.router.image_api_options()
        default = next((option for option in providers if option["ready"]), None)
        ready = [item for item in items if item["status"] == "ready"]
        return {
            "project": project_title,
            "episode": episode_number,
            "providers": providers,
            "default_provider": default["provider"] if default else "",
            "default_model": default["default_model"] if default else "",
            "default_quality": "medium",
            "items": items,
            "summary": {
                "total": len(items), "ready": len(ready),
                "queued": sum(item["status"] == "queued" for item in items),
                "running": sum(item["status"] == "running" for item in items),
                "blocked": sum(item["status"] == "blocked" for item in items),
                "completed": sum(item["status"] == "completed" for item in items),
            },
        }

    def preflight_image_acceleration(
            self, project_title, episode_number, item_ids, provider, model,
            quality="medium", contract_tokens=None):
        """无副作用逐张核对最终提示词、参考图、API、模型和质量。"""
        quality = normalize_quality(
            quality or "medium", field="image_quality")
        unique = list(dict.fromkeys(str(value) for value in (item_ids or [])
                                    if str(value).strip()))
        if not unique:
            raise AifosError("至少选择一张尚未开工的图片")
        if len(unique) > 200:
            raise AifosError("单次最多加速 200 张图片")
        project, episode = self._episode_ctx(project_title, episode_number)
        ctx = {"project": dict(project), "episode": dict(episode),
               "out_root": self._episode_dir(project, episode)}
        plan = self._plan_read(ctx)
        plan_by_id = {item.get("id"): item
                      for item in plan.get("items", [])}
        asset_policy = self.character_asset_policy(episode["id"])
        rows = {row["item_id"]: row
                for row in self.image_acceleration.list(episode["id"])}
        expected = contract_tokens or {}
        checked = []
        for item_id in unique:
            row = rows.get(item_id)
            issues = []
            if row is None:
                checked.append({"item_id": item_id, "label": item_id,
                                "status": "blocked",
                                "issues": ["图片尚未形成可派发契约"]})
                continue
            contract = row.get("contract") or {}
            issues.extend(contract.get("issues") or [])
            if (row["category"] == "character_sheet"
                    and not asset_policy["generate_sheets"]):
                issues.append("本集使用简化人物资产模式，不生成四视图或细节图")
            plan_item = plan_by_id.get(item_id)
            if plan_item is None:
                issues.append("图片已不在当前生产计划中")
            elif plan_item.get("status") != "pending":
                issues.append("图片已不处于当前待生产状态")
            if expected.get(item_id) \
                    and expected[item_id] != row["contract_token"]:
                issues.append("页面中的提示词/参考图预览已经过期")
            if row["production_state"] != "pending" \
                    or not row["never_started"]:
                issues.append("图片已进入生产线，不能再切换 API")
            if row["acceleration_status"] in ("queued", "running", "done"):
                issues.append("图片已经提交过 API 加速")
            payload = dict(contract.get("payload") or {})
            decision = dict(payload.get("quality_decision") or {})
            decision.update({"level": quality,
                             "source": "api_acceleration"})
            payload.update({
                "image_quality": quality,
                "quality_decision": decision,
                "image_task_class": image_task_class_for(quality),
                "require_reference_images": True,
                "strict_provider": provider,
                "model_override": model,
            })
            if not issues:
                try:
                    self.router.validate_image_selection(
                        provider, row["capability"], payload, model)
                except (ProviderUnavailable, ProviderError) as exc:
                    issues.append(str(exc))
            checked.append({
                "item_id": item_id,
                "label": contract.get("label", item_id),
                "category": row["category"],
                "capability": row["capability"],
                "status": "blocked" if issues else "ready",
                "issues": list(dict.fromkeys(issues)),
                "contract_token": row["contract_token"],
                "prompt": contract.get("prompt", ""),
                "prompt_hash": contract.get("prompt_hash", ""),
                "prompt_used": contract.get("prompt_used", contract.get("prompt", "")),
                "prompt_used_hash": contract.get("prompt_used_hash", ""),
                "references": contract.get("references") or {},
                "characters": contract.get("characters") or [],
                "provider": provider, "model": model,
                "quality": quality,
            })
        passed = bool(checked) and all(
            item["status"] == "ready" for item in checked)
        fingerprint_basis = {
            "episode_id": episode["id"], "provider": provider,
            "model": model, "quality": quality,
            "items": [{"item_id": item["item_id"],
                       "contract_token": item.get("contract_token", "")}
                      for item in checked],
        }
        return {
            "passed": passed,
            "fingerprint": self._stable_hash(fingerprint_basis),
            "provider": provider, "model": model, "quality": quality,
            "items": checked,
            "summary": {
                "total": len(checked),
                "ready": sum(item["status"] == "ready" for item in checked),
                "blocked": sum(item["status"] != "ready" for item in checked),
            },
        }

    def queue_image_acceleration(
            self, project_title, episode_number, item_ids, provider, model,
            quality="medium", fingerprint="", contract_tokens=None):
        report = self.preflight_image_acceleration(
            project_title, episode_number, item_ids, provider, model,
            quality=quality, contract_tokens=contract_tokens)
        if fingerprint and fingerprint != report["fingerprint"]:
            raise AifosError("预检结果已过期，请重新核对提示词和参考图")
        if not report["passed"]:
            first = next(item for item in report["items"]
                         if item["status"] != "ready")
            raise AifosError(
                f"{first['label']} 未通过放行: "
                + "；".join(first.get("issues") or ["未知原因"]))
        _project, episode = self._episode_ctx(project_title, episode_number)
        requests = [{
            "item_id": item["item_id"],
            "contract_token": item["contract_token"],
            "provider": provider, "model": model, "quality": report["quality"],
        } for item in report["items"]]
        self.image_acceleration.queue(episode["id"], requests)
        return {
            "queued": len(requests), "provider": provider,
            "model": model, "quality": report["quality"],
            "item_ids": [request["item_id"] for request in requests],
            "fingerprint": report["fingerprint"],
        }

    @staticmethod
    def _shot_priority(shot, scene_first=False):
        """失败代价最高的镜头先出，尽早暴露多人/文字/运动问题。"""
        people = int(shot.get("character_count", len(
            shot.get("characters", []))))
        text = shot.get("readable_text") or {}
        camera = str(shot.get("camera") or "")
        action = str(shot.get("description") or shot.get("prompt") or "")
        return (people * 30
                + (45 if readable_text_required(text) else 0)
                + (25 if scene_first else 0)
                + (15 if any(word in camera for word in
                             ("跟", "移", "摇", "环绕")) else 0)
                + (10 if any(word in action for word in
                             ("走", "跑", "进入", "离开", "追")) else 0))

    def _plan_seed_shots(self, ctx):
        """分镜确定后,把每个镜头的关键帧与首尾帧登记进清单。
        清单展示实际发送的当前镜头短合同，所见即所得。"""
        shots = (ctx.get("storyboard") or {}).get("shots") or []
        scene_counts = {}
        for shot in shots:
            scene_counts[shot.get("scene_no")] = (
                scene_counts.get(shot.get("scene_no"), 0) + 1)
        image_items, frame_items = [], []
        for shot in shots:
            shot_no = shot["shot_no"]
            shot_payload = self._shot_payload(
                ctx, shot, continuity_anchor=(
                    scene_counts.get(shot.get("scene_no"), 0) > 1))
            image_quality = resolve_image_quality(
                recommend_shot_image_quality(shot),
                ctx.get("quality_policy") or default_quality_policy(),
                f"shot:{shot_no}")
            frame_quality = resolve_image_quality(
                recommend_shot_image_quality(
                    shot, continuity_anchor=(
                        scene_counts.get(shot.get("scene_no"), 0) > 1)),
                ctx.get("quality_policy") or default_quality_policy(),
                f"frames:{shot_no}")
            image_items.append({
                "id": f"shot:{shot_no}", "category": "shot_image",
                "label": f"镜头 {shot_no:02d}"
                         + (f" · 第{shot['scene_no']}场"
                            if shot.get("scene_no") else ""),
                "shot_no": shot_no,
                # 清单展示与模型实际收到的是同一份当前镜头短合同。
                "prompt": shot_payload["prompt_compact"],
                "content_hash": self._shot_content_hash(shot),
                **self._quality_meta(image_quality),
            })
            frame_items.append({
                "id": f"frames:{shot_no}", "category": "frames",
                "label": f"镜头 {shot_no:02d} 首尾帧",
                "shot_no": shot_no,
                "prompt": shot_payload["prompt_compact"],
                "content_hash": self._shot_content_hash(shot),
                **self._quality_meta(frame_quality),
            })
        self._plan_seed(ctx, "shot_image", image_items)
        self._plan_seed(ctx, "frames", frame_items)

    # ---- 各阶段实现 ----
    @staticmethod
    def _normalize_script_character_profiles(script, premise="", *,
                                             project_title="", style=""):
        """为人工导入/旧版剧本补齐人物背景与剧情圣经,不覆盖已有设定。"""
        if not isinstance(script, dict):
            return script
        scenes = script.get("scenes") or []
        locations = {}
        for scene in scenes:
            for name in scene.get("characters", []) or []:
                locations.setdefault(name, []).append(
                    scene.get("location") or "本集场景")
        for character in script.get("characters", []) or []:
            if not isinstance(character, dict) or not character.get("name"):
                continue
            name = character["name"]
            role = character.get("role") or "角色"
            place = "、".join(dict.fromkeys(locations.get(name, []))) or "本集剧情场景"
            if is_background_character(character):
                character.setdefault(
                    "crowd_function",
                    f"仅作为{place}中的短暂场景功能角色，按分镜声明的人数出现；"
                    "无独立人物设定、候选图、立绘或四视图")
                continue
            character.setdefault(
                "background_prompt",
                f"{name}作为{role}出现在{place},其经历与本集前提{premise or '当前冲突'}"
                "相连;通过眼神、站姿、随身物件和服装层次外化性格,造型不得脱离故事")
            character.setdefault("era_setting", "由本集剧情和场景决定的时代/世界观")
            character.setdefault("occupation", role)
            character.setdefault("motivation", "推动本集目标并回应当前冲突")
            character.setdefault("backstory", "待编剧根据剧情补充的关键经历")
            character.setdefault("relationships", "与本集同场角色存在剧情关系,按台词和行动体现")
            character.setdefault(
                "costume_direction",
                "服装须符合时代/世界观、职业、性格和当前场合;至少区分日常、冲突、关键场合三套造型")
            character.setdefault("signature_props", "由职业、经历或本集关键事件决定的标志道具")
            character.setdefault("visual_variants", [])
            character.setdefault("character_analysis", {})
            character.setdefault("visual_dna", {})
            character.setdefault("cast_dedup", {})
        normalize_script_bible(script, {
            "project_title": project_title or script.get("project_title", ""),
            "premise": premise,
            "style": style,
        })
        error = validate_script_bible(script)
        if error:
            raise AifosError(f"剧本世界观/人物设定门禁失败: {error}")
        return script

    def _ensure_story_analysis(self, ctx, *, force=False,
                               creative_direction=""):
        """剧本 → AI 制作圣经；保存后注入剧本供所有下游提示词继承。"""
        episode_id = ctx["episode"]["id"]
        script = ctx["script"]
        analysis_rules = (
            (ctx.get("production_profile") or {}).get(
                "rules", {}).get("story_analysis", {}))
        # 空值代表“AI 根据完整剧本自动设计”,不要把制作标准中的通用兜底
        # 冒充用户锁定画风。分析完成后会把本剧专属结果写回项目。
        project_style = str(ctx["project"].get("style") or "").strip()
        current, version = self.projects.latest_document(
            episode_id, "story_analysis")
        current_visual = (
            current.get("visual") if isinstance(current, dict) else {}) or {}
        current_auto_style = (
            current_visual.get("style_source") == "ai_inferred"
            and project_style
            == str(current_visual.get("user_style_constraint") or "").strip())
        # 已保存的自动风格不是用户约束；重新分析时仍允许 AI 根据新剧本重建。
        style = "" if current_auto_style else project_style

        def persist_auto_style(analysis):
            nonlocal style
            visual = analysis.get("visual") or {}
            derived = str(
                visual.get("user_style_constraint") or "").strip()
            if not style and derived:
                visual["style_source"] = "ai_inferred"
                # 自动分析结果只在本集上下文和制作圣经中生效，不写成项目的
                # “人工锁定画风”。这样新集/重写仍会读完整剧本重新分析，
                # force 重制的编剧输入也保持确定性。
                project = dict(ctx["project"])
                project["style"] = derived
                ctx["project"] = project
                style = derived
            analysis["project_style"] = project_style
            return analysis

        if (not force and current is not None
                and current.get("script_version") == ctx.get(
                    "script_version")
                and current.get("project_style") == project_style
                and validate_story_analysis(current) is None):
            analysis = build_story_analysis(
                script, style, raw=current,
                source=current.get("source", "saved"))
            analysis["script_version"] = ctx.get("script_version")
            persist_auto_style(analysis)
            apply_story_analysis(script, analysis)
            ctx["story_analysis"] = analysis
            ctx["story_analysis_version"] = version
            return analysis, version, True
        result = self._call(
            ctx, "script", {
                "story_analysis": True,
                "project_title": ctx["project"]["title"],
                "episode_number": ctx["episode"]["number"],
                "script": script,
                "style": style,
                "creative_direction": creative_direction,
                "analysis_rules": analysis_rules,
                "production_profile": ctx.get("production_profile") or {},
            }, "story_analysis")
        imported = script.get("import_analysis")
        needs_line_resolution = (
            bool(unresolved_character_labels(script))
            or (isinstance(imported, dict)
                and bool(imported.get("misclassified_labels_removed")
                         or imported.get("entity_corrections"))))
        if needs_line_resolution:
            resolution_error = validate_line_speaker_resolution(
                script, result.data)
            if resolution_error:
                raise AifosError(f"剧本人物逐句校正失败: {resolution_error}")
        script_changed = reconcile_character_entities(script, result.data)
        if script_changed:
            # AI 已确认“温声/试探着”等只是表演提示：把它们合并回真实人物，
            # 再补齐真实人物的基础档案。台词原文不做任何改写。
            normalize_script_bible(script, {
                "project_title": ctx["project"]["title"],
                "episode_number": ctx["episode"]["number"],
                "style": style,
            })
        analysis = build_story_analysis(
            script, style, raw=result.data, source=result.provider)
        persist_auto_style(analysis)
        error = validate_story_analysis(analysis)
        if error:
            raise AifosError(f"剧本 AI 分析失败: {error}")
        apply_story_analysis(script, analysis)
        # 新写/上传/重写的剧本要等 AI 分析完成后一次性落成同一个版本，
        # 避免“原始剧本 v1 + 自动增强 v2”造成无意义的版本膨胀。
        if ctx.pop("script_pending_save", False) or script_changed:
            stored_script = copy.deepcopy(script)
            stored_script.pop("production_analysis", None)
            script_version = self.projects.save_document(
                episode_id, "script", stored_script)
        else:
            script_version = ctx.get("script_version") or 0
        ctx["script_version"] = script_version
        analysis["script_version"] = script_version
        # 内存中的下游上下文保留完整制作圣经，并带上最终剧本版本号。
        apply_story_analysis(script, analysis)
        version = self.projects.save_document(
            episode_id, "story_analysis", analysis)
        ctx["story_analysis"] = analysis
        ctx["story_analysis_version"] = version
        self.data.record(
            "prompt", "success",
            prompt=f"story-analysis:{ctx['project']['title']}"
            f":e{ctx['episode']['number']}",
            uri=result.uri,
            meta={"version": version,
                  "script_version": ctx.get("script_version"),
                  "style": style},
            episode_id=episode_id)
        return analysis, version, False

    def _stage_script(self, ctx):
        episode = ctx["episode"]
        provided = ctx.get("provided_script")
        if provided is not None:
            self._normalize_script_character_profiles(
                provided, ctx["episode"].get("premise", ""),
                project_title=ctx["project"]["title"],
                style=ctx["project"].get("style", ""))
            provided.setdefault("project_title", ctx["project"]["title"])
            provided.setdefault("episode_number", episode["number"])
            ctx["script"] = provided
            _, ctx["script_version"] = self.projects.latest_document(
                episode["id"], "script")
            ctx["script_pending_save"] = True
            analysis, analysis_version, _ = self._ensure_story_analysis(
                ctx, force=True)
            ctx["script_is_new"] = True
            version = ctx["script_version"]
            self.log.info(
                "director", f"使用用户自带剧本(v{version})，"
                f"AI 制作圣经(v{analysis_version})已完成，等待确认")
            return {"version": version, "provided": True,
                    "scenes": len(provided["scenes"]),
                    "story_analysis_version": analysis_version,
                    "world": analysis["world"]["name"]}
        if not ctx.get("force"):
            existing, version = self.projects.latest_document(
                episode["id"], "script")
            if existing is not None:
                before = json.dumps(
                    existing, ensure_ascii=False, sort_keys=True)
                self._normalize_script_character_profiles(
                    existing, ctx["episode"].get("premise", ""),
                    project_title=ctx["project"]["title"],
                    style=ctx["project"].get("style", ""))
                normalized_changed = json.dumps(
                    existing, ensure_ascii=False, sort_keys=True) != before
                ctx["script"] = existing
                ctx["script_version"] = version
                if normalized_changed:
                    ctx["script_pending_save"] = True
                analysis, analysis_version, reused = \
                    self._ensure_story_analysis(
                        ctx, force=normalized_changed)
                version = ctx["script_version"]
                if normalized_changed:
                    self.log.info(
                        "director",
                        f"已有剧本已补齐故事世界、前情与人物设定(v{version})")
                else:
                    self.log.info("director", f"复用已有剧本 v{version}")
                return {"version": version,
                        "reused": not normalized_changed,
                        "scenes": len(existing["scenes"]),
                        "story_analysis_version": analysis_version,
                        "story_analysis_reused": reused,
                        "world": analysis["world"]["name"]}
        payload = {
            "project_title": ctx["project"]["title"],
            "episode_number": episode["number"],
            "premise": episode["premise"],
            "style": ctx["project"]["style"],
            "template": ctx["project"]["kind"],       # drama / idol
            "persona": ctx["project"]["title"],       # 偶像人设名=项目名
        }
        if ctx.get("feedback"):
            # 修改意见:连同上一版剧本一起交给编剧重写
            payload["feedback"] = ctx["feedback"]
            previous, _ = self.projects.latest_document(
                episode["id"], "script")
            if previous is not None:
                payload["previous_script"] = previous
        result = self._call(ctx, "script", payload, "script")
        script = result.data
        self._normalize_script_character_profiles(
            script, ctx["episode"].get("premise", ""),
            project_title=ctx["project"]["title"],
            style=ctx["project"].get("style", ""))
        ctx["script"] = script
        _, ctx["script_version"] = self.projects.latest_document(
            episode["id"], "script")
        ctx["script_pending_save"] = True
        analysis, analysis_version, _ = self._ensure_story_analysis(
            ctx, force=True)
        version = ctx["script_version"]
        ctx["script_is_new"] = True     # 新写的剧本 → 触发剧本确认暂停
        self.data.record(
            "prompt", "success", prompt=f"script:{ctx['project']['title']}"
            f":e{episode['number']}", uri=result.uri,
            meta={"version": version}, episode_id=episode["id"])
        return {"version": version, "scenes": len(script["scenes"]),
                "story_analysis_version": analysis_version,
                "world": analysis["world"]["name"]}

    def _stage_continuity(self, ctx):
        """项目角色/场景/文字规则与生产配置的单集快照。"""
        if not ctx.get("force"):
            existing, version = self.projects.latest_document(
                ctx["episode"]["id"], "continuity")
            if (existing is not None
                    and existing.get("pipeline_version") == PIPELINE_VERSION
                    and existing.get("script_version") == ctx.get(
                        "script_version")
                    and existing.get("story_analysis_version") == ctx.get(
                        "story_analysis_version")
                    and (existing.get("production_profile")
                         if isinstance(existing.get("production_profile"),
                                       dict) else {}).get(
                        "standard_fingerprint") == ctx[
                            "production_profile"].get(
                                "standard_fingerprint")):
                ctx["continuity"] = existing
                return {"version": version, "reused": True,
                        "characters": len(existing.get("characters", [])),
                        "scenes": len(existing.get("scenes", []))}
        continuity = build_continuity_bible(
            ctx["project"], ctx["script"], ctx["production_profile"])
        continuity["script_version"] = ctx.get("script_version")
        continuity["story_analysis_version"] = ctx.get(
            "story_analysis_version")
        version = self.projects.save_document(
            ctx["episode"]["id"], "continuity", continuity)
        ctx["continuity"] = continuity
        return {"version": version,
                "characters": len(continuity["characters"]),
                "scenes": len(continuity["scenes"])}

    def _stage_storyboard(self, ctx):
        if not ctx.get("force"):
            existing, version = self.projects.latest_document(
                ctx["episode"]["id"], "storyboard")
            if (existing is not None
                    and existing.get("pipeline_version") == PIPELINE_VERSION
                    and existing.get("script_version") == ctx.get(
                        "script_version")
                    and existing.get("story_analysis_version") == ctx.get(
                        "story_analysis_version")
                    and (existing.get("profile")
                         if isinstance(existing.get("profile"), dict)
                         else {}).get(
                        "standard_fingerprint") == ctx[
                            "production_profile"].get(
                                "standard_fingerprint")):
                ctx["storyboard"] = existing
                self._plan_seed_shots(ctx)
                self.log.info("director", f"复用已有五维分镜 v{version}")
                return {"version": version, "reused": True,
                        "shots": len(existing["shots"])}
            if existing is not None:
                self.log.info(
                    "director", "剧本/标准已更新,重出分镜并重制后续画面")
        result = self._call(
            ctx, "storyboard", {
                "script": ctx["script"],
                "continuity": ctx["continuity"],
                "production_profile": ctx["production_profile"],
            }, "storyboard")
        # 原始分镜落盘:加工若出错,凭这份文件即可复现定位
        raw_path = ctx["out_root"] / "storyboard" / "raw_provider.json"
        raw_path.parent.mkdir(parents=True, exist_ok=True)
        raw_path.write_text(
            json.dumps(result.data, ensure_ascii=False, indent=1,
                       default=str),
            encoding="utf-8")
        try:
            storyboard = enrich_storyboard(
                ctx["script"], result.data, ctx["continuity"],
                ctx["production_profile"],
                style=ctx["project"].get("style", ""))
        except (AttributeError, TypeError, KeyError, ValueError) as exc:
            # 最后兜底:未知畸形结构给出可行动的错误与原始文件位置
            raise AifosError(
                f"分镜产出结构异常({exc});原始分镜已保存在 "
                f"{raw_path},把该文件发给开发助手即可定位") from exc
        storyboard["script_version"] = ctx.get("script_version")
        storyboard["story_analysis_version"] = ctx.get(
            "story_analysis_version")
        version = self.projects.save_document(
            ctx["episode"]["id"], "storyboard", storyboard)
        ctx["storyboard"] = storyboard
        # 分镜变了 → 旧的关键帧/首尾帧/视频全部作废重做
        ctx["force"] = True
        for shot in storyboard["shots"]:
            self.assets.register(
                ctx["project"]["id"], "prompt",
                f"e{ctx['episode']['number']:03d}_shot{shot['shot_no']:03d}",
                meta={"prompt": shot["prompt"],
                      "seedance_prompt": shot["seedance_prompt"],
                      "unit_id": shot["unit_id"]})
        self._plan_seed_shots(ctx)
        return {"version": version, "shots": len(storyboard["shots"]),
                "pipeline_version": storyboard["pipeline_version"]}

    def _stage_blocking(self, ctx):
        """五维分镜 → 确定性 3D 空间调度，不消耗任何出图额度。"""
        rules = ctx["production_profile"].get("rules", {}).get(
            "storyboard", {})
        threshold = int(rules.get(
            "spatial_blocking_required_for_group", 3))
        candidate = build_spatial_plan(
            ctx["script"], ctx["storyboard"], ctx["continuity"],
            group_threshold=threshold)
        existing, version = self.projects.latest_document(
            ctx["episode"]["id"], "blocking")
        reused = (not ctx.get("force") and existing is not None
                  and existing.get("source_fingerprint")
                  == candidate["source_fingerprint"]
                  and (existing.get("validation") or {}).get("passed"))
        blocking = existing if reused else candidate
        paths = write_spatial_svgs(
            blocking, ctx["out_root"] / "blocking")
        reference_paths = write_spatial_reference_pngs(
            blocking, ctx["out_root"] / "blocking" / "seedance")
        # PNG 路径属于生产合同的一部分；旧版 blocking 即使可复用，也要
        # 补写新字段，保证稍后刷新页面或重启后仍能找到必传空间图。
        version = self.projects.save_document(
            ctx["episode"]["id"], "blocking", blocking)
        for shot_no, uri in reference_paths.items():
            block = shot_blocking(blocking, shot_no) or {}
            self.assets.register(
                ctx["project"]["id"], "spatial_blocking",
                f"e{ctx['episode']['number']:03d}_shot{shot_no:03d}_space",
                uri=uri,
                meta={
                    "shot_no": shot_no,
                    "scene_no": block.get("scene_no"),
                    "image_quality": "high",
                    "mandatory_for_seedance": True,
                    "reason": block.get("spatial_reference_reason", ""),
                })
        ctx["blocking"] = blocking
        return {
            "version": version,
            "reused": reused,
            "scenes": len(blocking.get("scenes", [])),
            "required_scenes": blocking.get("summary", {}).get(
                "required_scenes", 0),
            "shots": blocking.get("summary", {}).get("shots", 0),
            "svgs": len(paths),
            "seedance_reference_pngs": len(reference_paths),
            "passed": blocking.get("validation", {}).get("passed", False),
        }

    @staticmethod
    def _upgrade_character_visual_dna(design, character):
        """把旧人物设定无损升级到视觉 DNA 结构，保留已有具体描述。"""
        upgraded = copy.deepcopy(design or {})
        analysis = upgraded.get("character_analysis")
        if not isinstance(analysis, dict) or not analysis:
            analysis = copy.deepcopy(
                character.get("character_analysis")
                if isinstance(character.get("character_analysis"), dict)
                else {})
            analysis.update({
                key: analysis.get(key) or value
                for key, value in {
                    "identity_and_class": (
                        character.get("identity")
                        or upgraded.get("occupation")
                        or character.get("role") or "角色"),
                    "age_and_presentation": (
                        character.get("age_range") or "以剧本为准"),
                    "upbringing": (
                        character.get("backstory")
                        or upgraded.get("backstory")
                        or "由关键经历塑造当前性格"),
                    "family_background": (
                        character.get("family_background")
                        or "剧本未明示，保守留白"),
                    "education_background": (
                        character.get("education_background")
                        or "剧本未明示，保守留白"),
                    "current_situation": (
                        character.get("current_situation")
                        or upgraded.get("background_prompt")
                        or "处于本集核心冲突中"),
                    "core_desire": (
                        character.get("motivation")
                        or upgraded.get("motivation")
                        or "完成本集目标"),
                    "greatest_fear": (
                        character.get("greatest_fear")
                        or "失去当前最重要的目标或关系"),
                    "formative_experiences": [
                        character.get("backstory")
                        or upgraded.get("backstory")
                        or "关键经历以剧本为准"],
                    "strengths": [
                        character.get("personality")
                        or upgraded.get("personality") or "目标明确"],
                    "flaws": ["由冲突和行为代价反推，禁止完美人设"],
                    "behavior_habits": [
                        character.get("signature_props")
                        or upgraded.get("signature_props")
                        or "通过眼神、站姿和随身物件外化性格"],
                }.items()
            })
            upgraded["character_analysis"] = analysis
        visual_dna = upgraded.get("visual_dna")
        if not isinstance(visual_dna, dict) or not visual_dna:
            visual_dna = copy.deepcopy(
                character.get("visual_dna")
                if isinstance(character.get("visual_dna"), dict)
                else {})
            visual_dna.update({
                key: visual_dna.get(key) or value
                for key, value in {
                    "face_structure": (
                        upgraded.get("appearance")
                        or "脸型骨相由年龄和经历推导"),
                    "hair_silhouette": (
                        upgraded.get("hair")
                        or "发型轮廓由职业与行为习惯推导"),
                    "body_or_occupation_marks": (
                        upgraded.get("occupation")
                        or "体态与职业痕迹服从人物经历"),
                    "clothing_structure": (
                        upgraded.get("costume")
                        or upgraded.get("costume_direction")
                        or "服装结构服从身份与剧情场合"),
                    "clothing_wear_state": (
                        "新旧、磨损与整洁程度服从社会阶层和当前处境"),
                    "story_visual_symbol": (
                        upgraded.get("signature")
                        or upgraded.get("signature_props")
                        or "从关键经历提炼专属视觉符号"),
                    "story_visual_symbol_origin": (
                        upgraded.get("backstory")
                        or "符号必须能追溯到经历、职业或关系"),
                    "signature_accessory": (
                        upgraded.get("signature_props")
                        or upgraded.get("accessories")
                        or "仅保留一个有剧情来源的核心配饰"),
                    "temperament_keywords": [
                        upgraded.get("temperament") or "克制",
                        upgraded.get("personality") or "有行动目标",
                        "非模板化"],
                    "genre_system_mapping": {},
                }.items()
            })
            upgraded["visual_dna"] = visual_dna
        keywords = upgraded["visual_dna"].get("temperament_keywords")
        if not isinstance(keywords, list):
            keywords = [str(keywords)] if str(keywords or "").strip() else []
        for fallback in ("有行动目标", "经历可见", "非模板化"):
            if len(keywords) >= 3:
                break
            if fallback not in keywords:
                keywords.append(fallback)
        upgraded["visual_dna"]["temperament_keywords"] = keywords[:8]
        return upgraded

    @staticmethod
    def _cast_visual_dna_audit(designs):
        """对明确相同的主要视觉维度做确定性预审，结果写回候选提示词。

        这里只拦截两项以上的明显文本重合；真实语义近似仍由人物设定 AI
        和人工选角复核，避免把不同措辞误判成已经去重。
        """
        dimensions = {
            "hair_silhouette": ("visual_dna", "hair_silhouette", "hair"),
            "clothing_structure": (
                "visual_dna", "clothing_structure", "costume"),
            "body_or_occupation_marks": (
                "visual_dna", "body_or_occupation_marks", "appearance"),
            "story_visual_symbol": (
                "visual_dna", "story_visual_symbol", "signature"),
            "signature_accessory": (
                "visual_dna", "signature_accessory", "signature_props"),
            "temperament_keywords": (
                "visual_dna", "temperament_keywords", "temperament"),
        }

        def value_for(design, spec):
            nested = design.get(spec[0])
            value = nested.get(spec[1]) if isinstance(nested, dict) else None
            if not value:
                value = design.get(spec[2])
            if isinstance(value, list):
                return tuple(sorted(
                    re.sub(r"\W+", "", str(item)).lower()
                    for item in value if str(item).strip()))
            return re.sub(r"\W+", "", str(value or "")).lower()

        names = list(designs)
        conflicts = {name: [] for name in names}
        for index, left in enumerate(names):
            for right in names[index + 1:]:
                overlap = []
                for dimension, spec in dimensions.items():
                    left_value = value_for(designs[left], spec)
                    right_value = value_for(designs[right], spec)
                    if (left_value and right_value
                            and left_value == right_value):
                        overlap.append(dimension)
                if len(overlap) >= 2:
                    item = {"with": right, "dimensions": overlap}
                    conflicts[left].append(item)
                    conflicts[right].append(
                        {"with": left, "dimensions": overlap})
        audit = {}
        for name in names:
            items = conflicts[name]
            audit[name] = {
                "compared_with": [
                    other for other in names if other != name],
                "dimensions": list(dimensions),
                "overlap_threshold": 2,
                "status": "redesign_required" if items else "passed",
                "conflicts": items,
                "redesign_if_overlap": True,
                "instruction": (
                    "与冲突角色更换至少两个主要视觉维度后再生成候选"
                    if items else "已通过确定性文本预审；仍需人工视觉复核"),
            }
        return audit

    def _ensure_character_designs(self, ctx, characters):
        """人物设定:编剧 AI 为每个角色写性格/外貌/妆容/服装细节。
        项目级一次生成(存 character 资产 meta),跨集复用保证形象一致;
        缺谁补谁,占位产线也会给出具体可画的设定。"""
        project_id = ctx["project"]["id"]
        designs, missing, dirty = {}, [], set()
        role_by_name = {
            character["name"]: character.get("role", "")
            for character in characters}
        for character in characters:
            name = character["name"]
            design = self._character_design(project_id, name)
            if design:
                upgraded = self._upgrade_character_visual_dna(
                    design, character)
                for key in (
                        "gender", "age_range", "identity", "image_prompt",
                        "negative_prompt", "visual_direction",
                        "character_analysis", "visual_dna",
                        "continuity_anchors"):
                    if character.get(key):
                        upgraded[key] = copy.deepcopy(character[key])
                dna = character.get("visual_dna")
                if character.get("image_prompt") and isinstance(dna, dict):
                    upgraded["appearance"] = (
                        dna.get("face_structure")
                        or upgraded.get("appearance"))
                    upgraded["hair"] = (
                        dna.get("hair_silhouette")
                        or upgraded.get("hair"))
                    upgraded["costume"] = (
                        dna.get("clothing_structure")
                        or upgraded.get("costume"))
                    upgraded["accessories"] = (
                        dna.get("signature_accessory")
                        or upgraded.get("accessories"))
                designs[name] = upgraded
                if upgraded != design:
                    dirty.add(name)
            else:
                missing.append(character)
        if not missing:
            audit = self._cast_visual_dna_audit(designs)
            for name, design in designs.items():
                if design.get("cast_dedup") != audit[name]:
                    dirty.add(name)
                design["cast_dedup"] = audit[name]
            for name in dirty:
                self.assets.register(
                    project_id, "character", name,
                    meta={"role": role_by_name.get(name, ""),
                          "design": designs[name]}, new_version=True)
            return designs
        result = self._call(ctx, "script", {
            "character_design": True,
            "project_title": ctx["project"]["title"],
            "style": ctx["project"]["style"] or "",
            "logline": (ctx.get("script") or {}).get("logline", ""),
            "premise": ctx["episode"].get("premise", ""),
            "episode_title": (ctx.get("script") or {}).get(
                "episode_title", ""),
            "story_world": (ctx.get("script") or {}).get(
                "story_world", {}),
            "story_background": (ctx.get("script") or {}).get(
                "story_background", {}),
            "scene_context": [
                {"scene_no": scene.get("scene_no"),
                 "location": scene.get("location", ""),
                 "action": scene.get("action", ""),
                 "characters": scene.get("characters", [])}
                for scene in (ctx.get("script") or {}).get("scenes", [])
            ],
            "character_context": [
                {key: value for key, value in c.items()
                 if key != "reference_images"}
                for c in missing
            ],
            "existing_cast_designs": designs,
            # 有参考图的角色:设定必须以参考图人物的脸部特征与风格
            # 为最高标准撰写(编剧 AI 可直接读取图片文件)
            "characters": [{"name": c["name"],
                            "role": c.get("role", ""),
                            "reference_images":
                                self._character_reference_uris(
                                    project_id, c["name"])}
                           for c in missing],
        }, "script")
        by_name = {d.get("name"): d
                   for d in result.data.get("designs", [])}
        for character in missing:
            name = character["name"]
            design = by_name.get(name)
            if not design:
                continue
            has_reference = bool(
                self._character_reference_uris(project_id, name))
            # 制作圣经先于人物设定，最终人物出图卡是身份事实源。Provider
            # 不得用另一套随机模板覆盖它；尤其 mock 仅用于跑通流程，绝不能
            # 把仙侠发色/道袍混进历史或现代人物。
            for key in (
                    "gender", "age_range", "identity", "image_prompt",
                    "negative_prompt", "visual_direction",
                    "character_analysis", "visual_dna",
                    "continuity_anchors"):
                if (character.get(key)
                        and not (has_reference
                                 and key in ("image_prompt", "visual_dna"))):
                    design[key] = copy.deepcopy(character[key])
            if (result.provider == "mock" and not has_reference
                    and isinstance(
                        character.get("visual_dna"), dict)):
                dna = character["visual_dna"]
                design["appearance"] = (
                    dna.get("face_structure") or design.get("appearance"))
                design["hair"] = (
                    dna.get("hair_silhouette") or design.get("hair"))
                design["costume"] = (
                    dna.get("clothing_structure") or design.get("costume"))
                design["accessories"] = (
                    dna.get("signature_accessory")
                    or design.get("accessories"))
                design["visual_variants"] = copy.deepcopy(
                    character.get("visual_variants") or [])
                keywords = dna.get("temperament_keywords")
                if isinstance(keywords, list) and keywords:
                    design["temperament"] = "、".join(map(str, keywords))
            # 即使编剧模型只回传基础视觉字段，也保留内部人物分析；
            # 真正出图时只编译其中对当前资产可见、可执行的字段。
            for key in (
                    "introduction", "gender", "age_range", "identity",
                    "personality",
                    "background_prompt", "era_setting", "occupation",
                    "motivation", "backstory", "relationships",
                    "costume_direction", "signature_props",
                    "visual_variants", "visual_direction",
                    "prompt_prefix", "continuity_anchors",
                    "image_prompt", "negative_prompt",
                    "character_analysis", "visual_dna", "cast_dedup"):
                if (not design.get(key) and character.get(key)
                        and not (has_reference and key == "image_prompt")):
                    design[key] = character[key]
            if has_reference:
                design["image_prompt"] = "；".join(filter(None, (
                    f"单人角色定妆母图：{name}",
                    f"形态：{design.get('species') or '人类'}",
                    f"性别年龄：{design.get('gender') or character.get('gender') or '以参考图与剧本为准'}；"
                    f"{design.get('age_range') or character.get('age_range') or '以参考图与剧本为准'}",
                    f"身份：{design.get('identity') or character.get('identity') or character.get('role')}",
                    f"参考图身份锁：{design.get('appearance')}；{design.get('hair')}；"
                    f"{design.get('eyes')}；{design.get('makeup')}；{design.get('signature')}",
                    f"服装：{design.get('costume')}；{design.get('costume_detail')}",
                    f"配饰：{design.get('accessories')}",
                    "参考图中的脸部骨相、五官比例、年龄感、发际线和发型为最高标准，"
                    "全身正面自然站姿，纯净无文字背景",
                )))
                dna = design.get("visual_dna")
                if isinstance(dna, dict):
                    dna["face_structure"] = design.get("appearance")
                    dna["hair_silhouette"] = design.get("hair")
            designs[name] = design
            dirty.add(name)
        audit = self._cast_visual_dna_audit(designs)
        for name, design in designs.items():
            if design.get("cast_dedup") != audit[name]:
                dirty.add(name)
            design["cast_dedup"] = audit[name]
            if name not in dirty:
                continue
            self.assets.register(
                project_id, "character", name,
                meta={"role": role_by_name.get(name, ""),
                      "design": design}, new_version=True)
        if designs:
            self.log.info(
                "director",
                "人物设定已就绪(性格/外貌/妆容/服装细节),"
                f"覆盖角色: {'、'.join(designs)}")
        return designs

    @staticmethod
    def _asset_meta(row):
        if row is None:
            return {}
        meta = row["meta"]
        if isinstance(meta, str):
            try:
                return json.loads(meta or "{}")
            except ValueError:
                return {}
        return meta or {}

    def _asset_quality(self, row, default="medium"):
        """旧资产没有质量元数据时按中档兼容；新资产必须显式记录。"""
        value = self._asset_meta(row).get("image_quality", default)
        try:
            return normalize_quality(value, field="asset.image_quality")
        except AifosError:
            return default

    def _quality_meta(self, decision):
        return {
            "image_quality": decision["level"],
            "recommended_quality": decision.get("recommended",
                                                decision["level"]),
            "quality_source": decision.get("source", "auto"),
            "quality_rule": decision.get("rule", ""),
            "quality_reasons": list(decision.get("reasons") or []),
        }

    def _shot_image_meta(self, ctx, shot, decision, extra=None):
        """镜头图写入可检索上下文，供跨集资产匹配和复用。"""
        location = self._scene_locations(ctx).get(shot.get("scene_no"), "")
        value = {
            **self._quality_meta(decision),
            "shot_content_hash": self._shot_content_hash(shot),
            "episode_number": ctx["episode"]["number"],
            "shot_no": shot.get("shot_no"),
            "scene_no": shot.get("scene_no"),
            "characters": list(shot.get("characters") or []),
            "location": location,
        }
        if extra:
            value.update(extra)
        return value

    @staticmethod
    def _quality_meets(actual, required):
        order = {"low": 0, "medium": 1, "high": 2}
        return order.get(actual, 1) >= order.get(required, 1)

    def _locked_identity(self, project_id, name):
        """返回人工选定的最终立绘；普通 character_art 不视为已定版。"""
        row = self.assets.latest(project_id, "character_identity", name)
        if row is None or not self._asset_meta(row).get("locked"):
            return None
        if not formal_reference_allowed(self._asset_quality(row)):
            return None
        uri = row["uri"]
        if not uri:
            return None
        if not uri.startswith(("http://", "https://")) and not Path(uri).exists():
            return None
        return row

    def _canonical_character_assets(self, project_id, name):
        """返回定版后四张独立正式母资产；三视图拼板不计入就绪状态。"""
        assets = {}
        for key in CANONICAL_CHARACTER_SHEET_KEYS:
            row = self.assets.latest(
                project_id, "character_sheet", f"{name}:{key}")
            if row is None or not formal_reference_allowed(
                    self._asset_quality(row)):
                continue
            uri = str(row["uri"] or "")
            if not uri or (
                    not uri.startswith(("http://", "https://"))
                    and not Path(uri).exists()):
                continue
            assets[key] = {
                "uri": uri, "version": row["version"],
                "label": self._asset_meta(row).get("label", key),
            }
        return {
            "required": list(CANONICAL_CHARACTER_SHEET_KEYS),
            "assets": assets,
            "ready": all(
                key in assets for key in CANONICAL_CHARACTER_SHEET_KEYS),
        }

    def character_selection_status(self, project_id, characters):
        """项目级人物定版状态：按重要度生成候选，最终只锁1张。"""
        result = []
        candidate_rows = {}
        for row in self.assets.list(project_id, "character_candidate"):
            candidate_rows[row["name"]] = row
        for character in characters or []:
            name = character["name"] if isinstance(character, dict) else str(character)
            target = character_candidate_target(
                character if isinstance(character, dict) else {"name": name})
            # 背景路人只留在剧本、连续性和镜头人数控制中，不进入人物定版清单，
            # 避免在 UI 与生产计划里形成一条“零候选”的伪人物资产。
            if target <= 0:
                continue
            locked = self._locked_identity(project_id, name)
            selected_meta = self._asset_meta(locked)
            canonical = self._canonical_character_assets(project_id, name)
            design = self._character_design(project_id, name) or {}
            candidates = []
            for row in candidate_rows.values():
                meta = self._asset_meta(row)
                if meta.get("character") != name:
                    continue
                uri = row["uri"]
                if not uri or (not uri.startswith(("http://", "https://"))
                               and not Path(uri).exists()):
                    continue
                index = int(meta.get("candidate_index") or 0)
                if index < 1 or index > target:
                    continue
                look_variant = meta.get("look_variant")
                variant_source = meta.get("variant_source")
                if not isinstance(look_variant, dict) or not meta.get(
                        "variant_label"):
                    look_variant = None
                    variant_source = "legacy"
                candidates.append({
                    "id": f"candidate:{name}:{index}",
                    "index": index,
                    "uri": uri,
                    "version": row["version"],
                    "variant_id": meta.get("variant_id", ""),
                    "variant_label": meta.get("variant_label", ""),
                    "look_variant": look_variant,
                    "variant_source": variant_source or "generated",
                    "selected": bool(
                        locked and selected_meta.get("candidate_asset_id") == row["id"]),
                })
            candidates.sort(key=lambda item: item["index"])
            result.append({
                "character": name,
                "role": character.get("role", "") if isinstance(character, dict) else "",
                "candidate_target": target,
                "selection_required": target > 0,
                "locked": locked is not None,
                "identity_uri": locked["uri"] if locked else "",
                "identity_version": locked["version"] if locked else None,
                "canonical_assets": canonical,
                "character_analysis": design.get(
                    "character_analysis") or {},
                "visual_dna": design.get("visual_dna") or {},
                "cast_dedup": design.get("cast_dedup") or {},
                "candidates": candidates,
                "candidate_count": len(candidates),
            })
        required_items = [item for item in result if item["candidate_target"] > 0]
        locked_count = sum(1 for item in required_items if item["locked"])
        return {
            "schema": "aifos.character-selection/v1",
            "candidate_target": max(
                (item["candidate_target"] for item in result), default=0),
            "candidate_policy": character_candidate_policy_text(),
            "characters": result,
            "locked": locked_count,
            "total": len(required_items),
            "passed": (not required_items
                       or locked_count == len(required_items)),
            "required": any(not item["locked"] for item in required_items),
            "canonical_assets_ready": bool(
                required_items and all(
                    item["canonical_assets"]["ready"]
                    for item in required_items)),
        }

    def _ensure_character_candidates(self, ctx, characters, designs, style):
        """按角色重要度补足候选；候选之间并行，后续等待人工选择。"""
        project_id = ctx["project"]["id"]
        seed = []
        tasks = []
        quality_by_candidate = {}
        variant_by_candidate = {}
        for character in characters:
            name = character["name"]
            role = character.get("role", "")
            target = character_candidate_target(character)
            locked = self._locked_identity(project_id, name)
            existing = {}
            for index in range(1, target + 1):
                row = self.assets.latest(
                    project_id, "character_candidate", f"{name}:{index:02d}")
                if row is None:
                    continue
                meta = self._asset_meta(row)
                idx = int(meta.get("candidate_index") or 0)
                uri = row["uri"]
                if idx and uri and (uri.startswith(("http://", "https://"))
                                    or Path(uri).exists()):
                    existing[idx] = row
            if locked and not existing:
                # 人工上传的最终立绘没有候选集，仍视为明确人工定版。
                continue
            reference_payload = self._user_reference_payload(
                project_id, [name],
                allowed_roles={"identity", "wardrobe", "composition"})
            refs = reference_payload["reference_images"]
            quality = resolve_image_quality(
                recommend_asset_quality("character_candidate"),
                ctx.get("quality_policy") or default_quality_policy(),
                f"candidate:{name}")
            for index in range(1, target + 1):
                item_id = f"candidate:{name}:{index}"
                if locked and index not in existing:
                    # 已人工定版:缺失的候选不会再生成,也绝不挂在清单里
                    # 占着"待生成"(否则永远显示还有几张没画完)
                    continue
                quality_by_candidate[(name, index)] = quality
                variant = self._candidate_variant(index, designs.get(name))
                if index in existing:
                    existing_meta = self._asset_meta(existing[index])
                    if (existing_meta.get("variant_label")
                            and isinstance(existing_meta.get("look_variant"), dict)):
                        variant = {
                            key: existing_meta[key] for key in (
                                "variant_id", "variant_label", "look_variant",
                                "variant_source", "story_variant")
                            if key in existing_meta
                        }
                        variant.setdefault("variant_source", "generated")
                    else:
                        variant = {
                            "variant_id": "",
                            "variant_label": "历史候选",
                            "look_variant": None,
                            "variant_source": "legacy",
                        }
                variant_by_candidate[(name, index)] = variant
                if variant["variant_source"] == "legacy":
                    prompt = "历史候选未记录候选差异轴，请按当前项目唯一画风和角色重要度规则重新生成"
                else:
                    prompt = self._candidate_portrait_prompt(
                        name, role, style, designs.get(name), variant,
                        has_reference=bool(refs))
                seed.append({
                    "id": item_id, "category": "character_candidate",
                    "label": (f"{name} · 候选 {index} · "
                              f"{variant['variant_label']}"), "name": name,
                    "candidate_index": index, "prompt": prompt,
                    **variant,
                    **self._quality_meta(quality),
                })
                if index in existing or locked:
                    continue
                tasks.append({
                    "item_id": item_id,
                    "capability": "image",
                    "payload": {
                        "portrait": True,
                        "portrait_candidate": True,
                        **variant,
                        "image_task_class": image_task_class_for(
                            quality["level"]),
                        "image_quality": quality["level"],
                        "quality_decision": quality,
                        "art_name": f"{name}_candidate_{index:02d}",
                        "role": role, "shot_no": 0,
                        "characters": [name], "location": "",
                        "prompt": prompt, "style": style,
                        # 候选提示词已编译为可直接出图的视觉合同。人物
                        # 剧情设定仍保留在 payload 供审计/QC 使用，但不再
                        # 由 API Provider 二次拼入模型提示词。
                        "prompt_contract_complete": True,
                        "character_background": designs.get(name) or {},
                        **reference_payload,
                        # 初次定妆尚不存在最终立绘；若用户上传过身份参考，
                        # API 也必须真实使用这些图，不能只读文字。
                        "require_reference_images": bool(refs),
                        "aspect": ctx["aspect"], **ctx["dims"],
                    },
                    "sub_dir": "cast/candidates",
                    "tag": (name, index, role),
                    # 初始人物母资产只按剧本性格、角色参考图和风格生成，
                    # 不在候选阶段做视觉 QC；人工选定后供后续镜头质检引用。
                })
        self._plan_seed(ctx, "character_candidate", seed)
        # 已存在的候选明确标成复用，避免重新排队。
        for item in seed:
            name, index = item["name"], item["candidate_index"]
            status = self.character_selection_status(project_id, [name])
            if any(c["index"] == index
                   for c in status["characters"][0]["candidates"]):
                self._plan_mark(ctx, item["id"], "reused", only_pending=True)
        for (name, index, role), result in self._run_parallel(
                ctx, tasks,
                line=f"人物定妆候选({character_candidate_policy_text()})").items():
            quality = quality_by_candidate[(name, index)]
            variant = variant_by_candidate[(name, index)]
            self.assets.register(
                project_id, "character_candidate", f"{name}:{index:02d}",
                uri=result.uri,
                meta={"character": name, "role": role,
                      "candidate_index": index,
                      **variant,
                      "prompt": next(
                          item["prompt"] for item in seed
                          if item["id"] == f"candidate:{name}:{index}"),
                      "reference_images": list(
                          next(task["payload"].get("reference_images", [])
                               for task in tasks
                               if task["item_id"] ==
                               f"candidate:{name}:{index}")),
                      "provider": result.provider,
                      "model": getattr(result, "model", ""),
                      **self._quality_meta(quality)})
        return self.character_selection_status(project_id, characters)

    def select_character_candidate(self, project_title, episode_number,
                                   character_name, candidate_index):
        """人工选择并锁定最终立绘；下游只能引用该不可变身份锚点。"""
        project, episode = self._episode_ctx(project_title, episode_number)
        script, _ = self.projects.latest_document(episode["id"], "script")
        characters = (script or {}).get("characters", [])
        character = next((c for c in characters
                          if c.get("name") == character_name), None)
        if character is None:
            raise AifosError(f"剧本中没有角色: {character_name}")
        if episode["status"] != "awaiting_cast":
            raise AifosError("只能在人物定版阶段选择候选；后续已生产时请先重开定版")
        target = character_candidate_target(character)
        if int(candidate_index) < 1 or int(candidate_index) > target:
            raise AifosError(
                f"{character_name}({character.get('role') or '角色'})最多允许"
                f"{target}张候选，不能选择第{int(candidate_index)}张")
        candidate = self.assets.latest(
            project["id"], "character_candidate",
            f"{character_name}:{int(candidate_index):02d}")
        if candidate is None or not candidate["uri"]:
            raise AifosError(f"人物候选不存在: {character_name}/{candidate_index}")
        if (not candidate["uri"].startswith(("http://", "https://"))
                and not Path(candidate["uri"]).exists()):
            raise AifosError("候选图片文件已丢失，请重新生成候选")
        candidate_quality = self._asset_quality(candidate, default="high")
        if not formal_reference_allowed(candidate_quality):
            raise AifosError(
                "低质量试错图不能锁为正式人物参考，请把选中形象以高质量重生后再定版")
        candidate_meta = self._asset_meta(candidate)
        variant_meta = {
            key: candidate_meta[key] for key in (
                "variant_id", "variant_label", "look_variant",
                "variant_source") if key in candidate_meta
        }
        meta = {
            "character": character_name,
            "role": character.get("role", ""),
            "locked": True,
            "candidate_index": int(candidate_index),
            "candidate_asset_id": candidate["id"],
            "candidate_version": candidate["version"],
            "locked_at": now(),
            "image_quality": candidate_quality,
            "recommended_quality": "high",
            "quality_source": "selected_mother_asset",
            **variant_meta,
        }
        identity = self.assets.register(
            project["id"], "character_identity", character_name,
            uri=candidate["uri"], meta=meta, new_version=True)
        # character_art 是旧代码/资产中心的兼容别名，但其来源明确指向
        # 人工锁定的 identity，不能再由文字直接生成。
        self.assets.register(
            project["id"], "character_art", character_name,
            uri=candidate["uri"], meta=meta, new_version=True)
        ctx = {"episode": dict(episode),
               "out_root": self._episode_dir(project, episode)}
        plan = self._plan_read(ctx)
        for item in plan.get("items", []):
            if item.get("category") == "character_candidate" \
                    and item.get("name") == character_name:
                self._plan_mark(
                    ctx, item["id"], item.get("status", "done"),
                    extra={"selected": int(item.get("candidate_index", 0))
                           == int(candidate_index)})
        self._plan_mark(
            ctx, f"char:{character_name}", "reused",
            extra={"selected": True, "identity_version": identity["version"]})
        status = self.character_selection_status(project["id"], characters)
        self.projects.save_document(episode["id"], "cast_selection", status)
        self.log.info(
            "director",
            f"人物定版: {character_name} 选中候选{int(candidate_index)}，"
            f"进度 {status['locked']}/{status['total']}")
        return status

    def _stage_cast(self, ctx):
        """人物立绘与场景概念图:项目级资产,跨集复用保证形象一致。"""
        readiness_error = character_production_readiness_error(
            ctx["script"], ctx.get("story_analysis"))
        if readiness_error:
            raise AifosError(readiness_error)
        project_id = ctx["project"]["id"]
        style = ctx["project"]["style"] or DEFAULT_VISUAL_STYLE
        all_characters = ctx["script"].get("characters", [])
        characters = [
            c for c in all_characters if character_candidate_target(c) > 0]
        locations = []
        scene_context_by_location = {}
        for scene in ctx["script"]["scenes"]:
            if scene["location"] not in locations:
                locations.append(scene["location"])
                scene_context_by_location[scene["location"]] = dict(scene)
        location_reuse = {
            location: sum(1 for scene in ctx["script"]["scenes"]
                          if scene.get("location") == location)
            for location in locations
        }
        scene_quality = {
            location: resolve_image_quality(
                recommend_asset_quality(
                    "scene_art", reuse_count=location_reuse[location]),
                ctx.get("quality_policy") or default_quality_policy(),
                f"scene:{location}")
            for location in locations
        }
        # 先由编剧 AI 写人物设定(性格/外貌/妆容/服装细节),
        # 立绘与全部资产套件的提示词据此丰富;项目级一次,跨集复用
        designs = self._ensure_character_designs(ctx, characters)
        # 风格锚:主角立绘最先画,成为全项目形象的风格基准图
        anchor_name = self._anchor_character(project_id, characters)
        characters = sorted(
            characters, key=lambda c: c["name"] != anchor_name)
        selection = self._ensure_character_candidates(
            ctx, characters, designs, style)
        self.projects.save_document(
            ctx["episode"]["id"], "cast_selection", selection)
        if selection["required"]:
            ctx["cast"] = [c["name"] for c in characters]
            ctx["cast_selection"] = selection
            ctx["cast_selection_required"] = True
            return {
                "characters": len(characters),
                "candidates": sum(item["candidate_count"]
                                  for item in selection["characters"]),
                "candidate_target": sum(
                    character_candidate_target(c) for c in characters),
                "locked": selection["locked"],
                "awaiting_selection": True,
                "created": 0, "reused": 0, "scenes": 0,
            }
        ctx["cast_selection"] = selection
        asset_policy = self.character_asset_policy(
            ctx["episode"]["id"], script=ctx["script"], persist=True)
        ctx["character_asset_policy"] = asset_policy
        sheet_definitions = (
            CHARACTER_SHEETS if asset_policy["generate_sheets"] else [])
        locked_looks = {
            c["name"]: self._locked_look_variant(project_id, c["name"])
            for c in characters
        }
        self._plan_seed(ctx, "character_art", [
            {"id": f"char:{c['name']}", "category": "character_art",
             "label": f"{c['name']}({c.get('role') or '角色'})",
             "name": c["name"],
             "image_quality": "high", "recommended_quality": "high",
             "quality_source": "auto", "quality_rule": "mother_asset",
             "quality_reasons": ["人物母资产会被后续全部镜头引用"],
             "prompt": self._portrait_prompt(
                 c["name"], c.get("role", ""), style,
                 design=designs.get(c["name"]))}
            for c in characters])
        self._plan_seed(ctx, "character_sheet", [
            {"id": f"sheet:{c['name']}:{key}",
             "category": "character_sheet",
             "label": f"{c['name']} · {label}",
             "name": c["name"], "sheet": key,
             "image_quality": "high", "recommended_quality": "high",
             "quality_source": "auto", "quality_rule": "mother_asset",
             "quality_reasons": ["人物母资产会被后续全部镜头引用"],
             "prompt": self._sheet_prompt(
                 c["name"], c.get("role", ""), style, label, desc,
                 key=key, design=designs.get(c["name"]),
                 locked_look=locked_looks.get(c["name"]))}
            for c in characters
            for key, label, desc in sheet_definitions])
        self._plan_seed(ctx, "scene_art", [
            {"id": f"scene:{loc}", "category": "scene_art",
             "label": loc, "name": loc,
             "prompt": self._scene_prompt(
                 loc, style, scene_context_by_location.get(loc),
                 premise=ctx["episode"].get("premise", "")),
             **self._quality_meta(scene_quality[loc])}
            for loc in locations])
        reused, created = 0, 0
        cast = []

        # 最终立绘只能来自人工锁定的候选；此处绝不再从文字直接生成。
        for character in characters:
            name = character["name"]
            self.assets.acquire(
                project_id, "character", name,
                meta={"role": character.get("role", "")})
            cast.append(name)
            locked = self._locked_identity(project_id, name)
            if locked:
                reused += 1
                self._plan_mark(ctx, f"char:{name}", "reused",
                                only_pending=True,
                                extra={"selected": True,
                                       "identity_version": locked["version"]})
                continue
            raise AifosError(f"角色{name}尚未锁定最终立绘")
        # 场景可与人物资产套件继续并行；人物出图全部引用最终身份锚点。
        tasks = []
        for scene in ctx["script"]["scenes"]:
            location = scene["location"]
            self.assets.acquire(project_id, "scene", location)
            existing_scene = self._existing_asset_uri(
                ctx, "scene_art", location)
            if existing_scene:
                row = self.assets.latest(project_id, "scene_art", location)
                if self._quality_meets(
                        self._asset_quality(row),
                        scene_quality[location]["level"]):
                    reused += 1
                    self._plan_mark(ctx, f"scene:{location}", "reused",
                                    only_pending=True)
                    continue
            if any(t["tag"] == ("scene", location, "") for t in tasks):
                continue
            scene_references = self._user_reference_payload(
                project_id, [location],
                allowed_roles={"scene", "composition"})
            tasks.append({
                "item_id": f"scene:{location}", "capability": "image",
                "payload": {
                    "scene_art": True, "art_name": location,
                    "image_task_class": image_task_class_for(
                        scene_quality[location]["level"]),
                    "image_quality": scene_quality[location]["level"],
                    "quality_decision": scene_quality[location],
                    "shot_no": 0, "characters": [], "location": location,
                    "action": scene.get("action", ""),
                    "prompt": self._scene_prompt(
                        location, style, scene,
                        premise=ctx["episode"].get("premise", "")),
                    "style": style,
                    "prompt_contract_complete": True,
                    **scene_references,
                    "style_ref": self._style_anchor_uri(project_id),
                    "require_reference_images": bool(
                        scene_references["reference_images"]
                        or self._style_anchor_uri(project_id)),
                    "aspect": ctx["aspect"], **ctx["dims"],
                }, "sub_dir": "cast", "tag": ("scene", location, "")})
        for tag, result in self._run_parallel(
                ctx, tasks, line="场景概念图").items():
            kind, name, role = tag
            self.assets.register(
                project_id, "scene_art", name, uri=result.uri,
                meta=self._quality_meta(scene_quality[name]))
            created += 1
        # 阶段3:人物资产套件产线 并行批量(引用各自立绘+风格基准图)
        tasks = []
        for character in characters:
            name = character["name"]
            role = character.get("role", "")
            portrait = self.assets.latest(project_id, "character_art", name)
            portrait_uri = (portrait["uri"]
                            if portrait and portrait["uri"]
                            and Path(portrait["uri"]).exists() else None)
            reference_payload = self._user_reference_payload(
                project_id, [name],
                allowed_roles={"identity", "wardrobe", "composition"})
            for key, label, desc in sheet_definitions:
                asset_name = f"{name}:{key}"
                sheet_aspect = (
                    "16:9" if key == "turnaround"
                    else "1:1" if key == "closeup"
                    else "9:16" if key in ("front", "profile", "back")
                    else ctx["aspect"])
                sheet_dims = ASPECT_DIMS.get(
                    sheet_aspect, ctx["dims"])
                existing_sheet = self._existing_asset_uri(
                    ctx, "character_sheet", asset_name)
                if existing_sheet:
                    row = self.assets.latest(
                        project_id, "character_sheet", asset_name)
                    if self._quality_meets(
                            self._asset_quality(row), "high"):
                        reused += 1
                        self._plan_mark(
                            ctx, f"sheet:{name}:{key}", "reused",
                            only_pending=True)
                        continue
                tasks.append({
                    "item_id": f"sheet:{name}:{key}",
                    "capability": "image",
                    "payload": self._with_reference_manifest({
                        "character_sheet": key, "sheet_label": label,
                        "image_task_class": "important",
                        "image_quality": "high",
                        "art_name": name, "role": role,
                        "shot_no": 0, "characters": [name], "location": "",
                        "prompt": self._sheet_prompt(
                            name, role, style, label, desc,
                            key=key, design=designs.get(name),
                            locked_look=locked_looks.get(name)),
                        "style": style,
                        "prompt_contract_complete": True,
                        "character_background": designs.get(name) or {},
                        "character_refs": (
                            [portrait_uri] if portrait_uri else []),
                        "identity_references": self._identity_references(
                            project_id, [name]),
                        "require_reference_images": True,
                        **reference_payload,
                        "style_ref": self._style_anchor_uri(project_id),
                        "aspect": sheet_aspect, **sheet_dims,
                    }), "sub_dir": "cast",
                    "tag": (name, key, label),
                    # 母资产同步质检:四视图等会被后续所有镜头当参考图,
                    # 一张跑偏就污染全部下游——生成后立即与锁定立绘逐一
                    # 比对身份,不合格自动重画。多视角图不按人数硬卡。
                    "qc_spec": {**self._qc_spec(
                        project_id, [name], forbid=self._FORBID,
                        action=f"人物资产设定图({label}):同一角色"
                               f"「{name}」的{label},每个视角/局部都必须"
                               "与最终立绘同一人、同一发型",),
                        "multi_view": True,
                        "count_required": False},
                })
        for (name, key, label), result in self._run_parallel(
                ctx, tasks, line="人物资产套件").items():
            self.assets.register(
                project_id, "character_sheet", f"{name}:{key}",
                uri=result.uri,
                meta={"character": name, "sheet": key, "label": label,
                      "canonical": key in CANONICAL_CHARACTER_SHEET_KEYS,
                      "review_only": key == "turnaround",
                      "aspect": (
                          "16:9" if key == "turnaround"
                          else "1:1" if key == "closeup"
                          else "9:16" if key in ("front", "profile", "back")
                          else ctx["aspect"]),
                      "image_quality": "high",
                      "recommended_quality": "high",
                      "quality_source": "auto",
                      "quality_rule": "mother_asset"})
            created += 1
        # 保存包含独立母资产就绪状态的最新人物定版文档，供 UI/API 和
        # 后续生产门禁读取；合成审核板不计入正式参考图。
        ctx["cast_selection"] = self.character_selection_status(
            project_id, characters)
        self.projects.save_document(
            ctx["episode"]["id"], "cast_selection",
            ctx["cast_selection"])
        ctx["cast"] = cast
        return {"reused": reused, "created": created,
                "characters": len(cast),
                "scenes": len(ctx["script"]["scenes"]),
                "character_asset_mode": asset_policy["mode"],
                "character_asset_resolved": asset_policy["resolved_mode"],
                "character_sheets_per_character": len(sheet_definitions)}

    def _scene_locations(self, ctx):
        return {s["scene_no"]: s["location"]
                for s in ctx["script"]["scenes"]}

    def _character_reference_uris(self, project_id, name):
        """只取明确关联到该角色的人物/服装参考图。"""
        return [
            row["uri"] for row in self._reference_rows(
                project_id, [name],
                allowed_roles={"identity", "wardrobe", "composition"})]

    def _reference_uris(self, project_id, attach_names):
        """兼容旧调用：只返回与当前对象精确关联的参考图。

        全局画风图通过 ``style_ref`` 单独传递并使用专用绑定，不能再混成
        “用户参考图”去覆盖所有人物、服装和场景。
        """
        return [
            row["uri"] for row in self._reference_rows(
                project_id, attach_names,
                allowed_roles={
                    "identity", "wardrobe", "scene", "composition"})]

    def _matching_produced_image_rows(self, project_id, characters,
                                      location, shot_no=None, limit=3):
        """从资产中心找同人物/同场景的正式成图，优先作为连续性参考。"""
        wanted = set(characters or [])
        ranked = []
        for row in self.assets.active_list(project_id):
            if row["kind"] not in ("image", "first_frame", "last_frame"):
                continue
            if not formal_reference_allowed(self._asset_quality(row)):
                continue
            meta = self._asset_meta(row)
            if shot_no is not None and meta.get("shot_no") == shot_no:
                continue
            uri = row["uri"]
            if (not uri.startswith(("http://", "https://"))
                    and not Path(uri).exists()):
                continue
            row_chars = set(meta.get("characters") or [])
            same_location = bool(location and meta.get("location") == location)
            # 连续性图必须人物集合完全一致。旧逻辑只要求“有重叠”，会把
            # 乔安+白芷的双人图塞进乔安单人镜，直接污染人数和身份。
            exact_people = row_chars == wanted
            if not same_location or not exact_people:
                continue
            score = 6 + len(wanted) * 4 + 3
            ranked.append((score, row["id"], row))
        ranked.sort(key=lambda item: (-item[0], -item[1]))
        return [item[2] for item in ranked[:limit]]

    @staticmethod
    def _shot_reference_sheet_keys(shot):
        """按镜头用途选择一张独立母资产，合成审核板不进入正式参考链。"""
        shot = shot or {}
        text = " ".join(str(shot.get(key) or "") for key in (
            "kind", "description", "prompt", "camera", "dialogue"))
        contract = shot.get("shot_contract") or {}
        design = (shot.get("five_dimensions") or {}).get(
            "camera_design") or {}
        text += " " + " ".join(str(value or "") for value in (
            contract.get("角度"), contract.get("机位"), contract.get("构图"),
            design.get("angle"), design.get("camera_position"),
            design.get("composition")))
        if any(word in text for word in (
                "背面", "背对", "离场", "转身离开", "back view")):
            return ("back",)
        if any(word in text for word in (
                "侧面", "侧脸", "侧身", "转头", "走位", "profile")):
            return ("profile",)
        if any(word in text for word in (
                "服装", "配饰", "袖口", "鞋", "细节", "costume", "detail")):
            return ("costume_detail",)
        if any(word in text for word in (
                "特写", "近景", "脸", "妆", "眼神", "表情", "closeup")):
            return ("closeup",)
        return ("front",)

    @classmethod
    def _shot_reference_sheet_keys_by_character(
            cls, shot, composition_contract=None):
        """Mixed views need per-actor references, not one sheet for the shot."""
        composition = (
            composition_contract
            if isinstance(composition_contract, dict)
            else build_composition_contract(shot))
        fallback = cls._shot_reference_sheet_keys(shot)
        close = any(word in str(shot.get("camera") or "")
                    for word in ("近景", "特写", "closeup"))
        result = {}
        for actor in composition.get("actors") or []:
            name = actor.get("character")
            view = actor.get("expected_view")
            if not name:
                continue
            if view in ("back", "back_or_over_shoulder"):
                result[name] = ("back",)
            elif view == "profile":
                result[name] = ("profile",)
            elif view == "front_or_three_quarter":
                result[name] = (("closeup", "front")
                                if close else ("front", "closeup"))
            else:
                result[name] = fallback
        return result

    def _art_refs(self, ctx, characters, location, shot_no=None,
                  sheet_keys=None, sheet_keys_by_character=None,
                  spatial_ref="", inner_persona_ref=""):
        """最终立绘/人物套件/场景图/用户参考 → 真实多图参考输入。

        含人物画面缺任何一个最终立绘都直接阻断；禁止静默退化为文字生图。
        基础参考最多 8 张，为首/尾帧阶段的本镜关键图与上一镜尾帧预留
        2 个槽位；任何人物最终立绘都不会被低优先级参考挤掉。
        """
        project_id = ctx["project"]["id"]
        refs = {"character_refs": [], "identity_references": [],
                "asset_matches": []}
        used_uris = set()

        def room():
            return len(used_uris) < SHOT_BASE_REFERENCE_LIMIT

        def remember(uri):
            value = str(uri or "")
            if not value or value in used_uris or not room():
                return False
            used_uris.add(value)
            return True

        identities = self._identity_references(
            project_id, characters, required=bool(characters))
        if len(identities) > SHOT_BASE_REFERENCE_LIMIT:
            raise AifosError(
                f"本镜有 {len(identities)} 位需锁定身份的人物，超过参考图"
                f"硬上限 {SHOT_BASE_REFERENCE_LIMIT}；请拆分群像镜头")
        for identity in identities:
            remember(identity["uri"])
            refs["character_refs"].append(identity["uri"])
            refs["identity_references"].append(identity)
            refs["asset_matches"].append({
                "asset_id": identity.get("asset_id"),
                "kind": "character_identity",
                "name": identity.get("character", ""),
                "label": f"{identity.get('character', '角色')}最终立绘",
                "uri": identity["uri"],
            })
        inner_uri = str(inner_persona_ref or "").strip()
        if (inner_uri
                and (inner_uri.startswith(("http://", "https://"))
                     or Path(inner_uri).exists())
                and remember(inner_uri)):
            refs["inner_persona_ref"] = inner_uri
            inner_row = next((
                row for row in self.assets.active_list(
                    project_id, kind="inner_persona")
                if row["uri"] == inner_uri), None)
            refs["asset_matches"].append({
                "asset_id": (
                    inner_row["id"] if inner_row is not None else None),
                "kind": "inner_persona",
                "name": (
                    inner_row["name"] if inner_row is not None
                    else "内心Q版"),
                "label": "内心Q版母资产",
                "uri": inner_uri,
                "reference_role": "inner_persona",
                "attach_to": f"shot:{shot_no}" if shot_no is not None else "",
            })
        # 多人走位/变机位镜头的 3D 空间图不仅要给 Seedance，也要在
        # 关键帧阶段先把人数、站位和屏幕方向锁准。它只承担空间职责，
        # 绝不能把编号、箭头或示意图样式画进成片。
        spatial_uri = str(spatial_ref or "").strip()
        if (spatial_uri
                and (spatial_uri.startswith(("http://", "https://"))
                     or Path(spatial_uri).exists())
                and remember(spatial_uri)):
            refs["spatial_ref"] = spatial_uri
            spatial_row = next((
                row for row in self.assets.active_list(
                    project_id, kind="spatial_blocking")
                if row["uri"] == spatial_uri), None)
            refs["asset_matches"].append({
                "asset_id": (
                    spatial_row["id"] if spatial_row is not None else None),
                "kind": "spatial_blocking",
                "name": (
                    spatial_row["name"] if spatial_row is not None
                    else f"shot_{shot_no or 0}_space"),
                "label": "本镜空间调度图",
                "uri": spatial_uri,
                "reference_role": "spatial",
                "attach_to": f"shot:{shot_no}" if shot_no is not None else "",
            })
        # 简化版即使项目历史里已有四视图，也只以人工锁定最终立绘为身份锚，
        # 避免旧扩展资产继续偷偷进入提示词与外部 API 参考图。
        asset_policy = ctx.get("character_asset_policy") or {}
        if not asset_policy and (ctx.get("episode") or {}).get("id"):
            asset_policy = self.character_asset_policy(
                ctx["episode"]["id"], script=ctx.get("script"))
        include_sheets = asset_policy.get("generate_sheets", True)
        # 3 人及以上群像只上传每人的最终立绘；人物套件图会挤占身份、
        # 场景和连续性槽位，也容易让模型把不同角色的服装/脸互相混合。
        if include_sheets and len(characters or []) <= 2:
            requested_keys = tuple(sheet_keys or ("front",))
            for name in characters or []:
                per_actor_keys = tuple(
                    (sheet_keys_by_character or {}).get(name)
                    or requested_keys)
                # 每人只使用第一张实际存在且质量合格的视角母资产。
                for key in per_actor_keys:
                    row = self.assets.latest(
                        project_id, "character_sheet", f"{name}:{key}")
                    if (not row
                            or not formal_reference_allowed(
                                self._asset_quality(row))
                            or not row["uri"]
                            or not Path(row["uri"]).exists()):
                        continue
                    if not remember(row["uri"]):
                        continue
                    refs["character_refs"].append(row["uri"])
                    label = {
                        "turnaround": "三视图审核板",
                        "closeup": "面部母资产",
                        "front": "正面母资产",
                        "profile": "侧面母资产",
                        "back": "背面母资产",
                        "features": "五官特征",
                        "makeup": "妆容",
                        "costume": "服装",
                        "costume_detail": "服装细节",
                    }.get(key, key)
                    refs["asset_matches"].append({
                        "asset_id": row["id"], "kind": row["kind"],
                        "name": row["name"], "label": f"{name}{label}",
                        "uri": row["uri"],
                    })
                    break
        if location:
            row = self.assets.latest(project_id, "scene_art", location)
            if (row and formal_reference_allowed(self._asset_quality(row))
                    and row["uri"] and Path(row["uri"]).exists()
                    and remember(row["uri"])):
                refs["scene_ref"] = row["uri"]
                refs["asset_matches"].append({
                    "asset_id": row["id"], "kind": row["kind"],
                    "name": row["name"], "label": f"场景:{location}",
                    "uri": row["uri"],
                })
        matched_rows = (self._matching_produced_image_rows(
            project_id, characters, location, shot_no=shot_no, limit=3)
            if shot_no is not None else [])
        # 只允许当前分镜合同下已经通过视觉质检的镜头图充当连续性参考。
        # 分镜重排后旧图片仍保存在资产历史中；如果仅按“同人物/同场景”
        # 选图，旧合同里画错的人物会重新污染本轮返工。
        plan = self._plan_read(ctx) if ctx.get("out_root") else {"items": []}
        current_shot_items = [
            item for item in plan.get("items", [])
            if item.get("category") == "shot_image"]
        if current_shot_items:
            passed_shots = {
                int(item["shot_no"])
                for item in current_shot_items
                if item.get("shot_no") is not None
                and item.get("status") in ("done", "reused")
                and (item.get("qc") or {}).get("passed") is True
                and not (item.get("qc") or {}).get("stale")
            }
            matched_rows = [
                row for row in matched_rows
                if int((self._asset_meta(row).get("shot_no") or -1))
                in passed_shots]
        matched_rows = matched_rows[:1]
        matched = []
        for row in matched_rows:
            if not remember(row["uri"]):
                continue
            matched.append(row["uri"])
            refs["asset_matches"].append({
                "asset_id": row["id"], "kind": row["kind"],
                "name": row["name"], "label": "同人物/同场景已生产图",
                "uri": row["uri"],
            })
        attach_names = list(characters or []) + ([location] if location else [])
        user_rows = self._reference_rows(
            project_id, attach_names,
            allowed_roles={
                "identity", "wardrobe", "scene", "composition"})
        reference = list(matched)
        for row in user_rows:
            if not remember(row["uri"]):
                continue
            reference.append(row["uri"])
            refs["asset_matches"].append(
                self._reference_asset_match(row))
        if reference:
            refs["reference_images"] = reference
        anchor = self._style_anchor_uri(project_id)
        if anchor and remember(anchor):
            refs["style_ref"] = anchor
            style_row = next((
                row for row in self.assets.active_list(
                    project_id, kind="reference")
                if row["uri"] == anchor), None)
            if style_row is not None:
                refs["asset_matches"].append(
                    self._reference_asset_match(style_row))
        # 只要本次已经有任何锚点，就必须路由到能真实接收图片的产线；
        # 空镜同样不能把场景/风格/用户参考静默丢掉。
        refs["require_reference_images"] = bool(
            characters or refs.get("scene_ref")
            or refs.get("reference_images") or refs.get("style_ref")
            or refs.get("inner_persona_ref"))
        return refs

    def _relations(self, ctx):
        """画布关系图:ctx 内缓存优先,单图重画等路径从落盘文件回读。"""
        if ctx.get("relations"):
            return ctx["relations"]
        out_root = ctx.get("out_root")
        if out_root is None:
            return None
        path = out_root / "relations.json"
        if path.exists():
            try:
                ctx["relations"] = json.loads(
                    path.read_text(encoding="utf-8"))
                return ctx["relations"]
            except ValueError:
                pass
        return None

    SHOT_CHARACTER_FACT_KEYS = (
        "species", "gender", "sex", "age_range", "identity", "occupation",
        "hair", "makeup", "costume", "costume_detail", "palette",
        "accessories", "signature_props", "temperament",
    )

    def _shot_character_facts(self, ctx, shot):
        """Compile only visible, current-shot actor facts.

        Backstory, motivation, relationships, analysis source and full episode
        context remain in planning documents; image providers never receive
        them as part of a shot prompt.
        """
        project_id = ctx["project"]["id"]
        script_profiles = {
            item.get("name"): item
            for item in (ctx.get("script") or {}).get("characters", [])
            if isinstance(item, dict) and item.get("name")
        }
        result = {}
        for name in shot.get("characters", []) or []:
            merged = {
                **(self._character_design(project_id, name) or {}),
                **script_profiles.get(name, {}),
                **self._locked_look_variant(project_id, name),
            }
            result[name] = {
                key: copy.deepcopy(merged.get(key))
                for key in self.SHOT_CHARACTER_FACT_KEYS
                if merged.get(key)
            }
        return result

    def _shot_character_visuals(self, ctx, shot, facts=None):
        facts = facts if isinstance(facts, dict) \
            else self._shot_character_facts(ctx, shot)
        return {
            name: self._design_line(
                design, keys=self.SHOT_CHARACTER_FACT_KEYS)
            for name, design in facts.items()
        }

    def _rich_shot_prompt(self, ctx, shot, location):
        """构造只含当前镜头事实的可审计静帧提示词。

        身份由最终立绘承担；不注入整集剧情、故事前情、人物经历、其他
        镜头对白或整场 action，避免审计提示词与实际短合同不一致。
        """
        title = ctx["project"].get("title", "")
        world = (ctx.get("script") or {}).get("story_world") or {}
        parts = [f"【TASK】漫剧《{title}》镜头 {shot.get('shot_no', '')} 静态关键帧"]
        shot_world = "；".join(filter(None, (
            str(shot.get("world_state") or "").strip(),
            str(shot.get("era") or "").strip(),
            str(shot.get("time") or "").strip(),
        )))
        if shot_world:
            parts.append(f"【本镜世界状态】{shot_world}")
        if location:
            parts.append(
                f"【SCENE】{location}；空间、陈设、光线与场景基准图一致")
        character_facts = self._shot_character_facts(ctx, shot)
        character_visuals = self._shot_character_visuals(
            ctx, shot, character_facts)
        who = []
        number_map = shot.get("character_number_map") or {}
        actor_by_name = {
            item.get("name"): actor_id
            for actor_id, item in number_map.items()
            if isinstance(item, dict) and item.get("name")
        }
        for pos, name in enumerate(shot.get("characters", []), 1):
            line = character_visuals.get(name, "")
            actor_id = actor_by_name.get(name) or f"P{pos:02d}"
            who.append(
                f"{actor_id}={name}（{line or '人物身份以最终立绘为准'}；"
                "脸型、五官骨相、年龄、性别表达、发际线与发型轮廓只以"
                "该角色最终立绘锁定；不得复制参考图的姿势、背景或未声明服装）")
        if who:
            parts.append(
                f"【IDENTITY LOCK】画面严格共{len(who)}人；"
                + "；".join(who)
                + "；每人只读取自己编号对应的最终立绘，禁止串脸、换性别、"
                "新增、缺失、合并或复制人物")
        else:
            parts.append(
                "【IDENTITY LOCK】严格 0 人空镜；禁止人物、人体局部、"
                "剪影、倒影中的人或随机路人")
        overlays = [
            item for item in (shot.get("narrative_overlays") or [])
            if isinstance(item, dict)
        ][:1]
        if overlays:
            overlay = overlays[0]
            inner_line = (
                f"；内心台词「{overlay.get('dialogue')}」"
                if overlay.get("dialogue") else "")
            parts.append(
                "【NON-DIEGETIC INNER CHIBI】"
                f"{overlay.get('name') or '内心Q版'}只代表"
                f"{overlay.get('host_character') or '宿主'}的内心人格；"
                f"{overlay.get('expression') or '夸张Q版表情和动作'}；"
                f"{overlay.get('action') or '在宿主肩旁完成内心反应'}"
                f"{inner_line}。不计真实人数，不进入物理/空间站位，不被"
                "其他人物看见、回应、触碰或对视；服装继承已锁定当前造型，"
                "不继承默认道具；内心声音出现时真人宿主闭口，不画旁白字幕")
        composition = (
            shot.get("composition_contract")
            if isinstance(shot.get("composition_contract"), dict)
            else build_composition_contract(shot))
        if composition.get("composition_type") == "over_shoulder_dialogue":
            duties = "；".join(
                f"{item.get('character')}={item.get('role')}/"
                f"{item.get('expected_view')}"
                for item in composition.get("actors") or [])
            parts.append(
                "【OVER-SHOULDER COMPOSITION】"
                f"正面主体{composition.get('expected_primary_count', 1)}人，"
                f"实际可见人形{composition.get('expected_visible_figure_count', len(who))}人；"
                f"{duties}；{composition.get('count_rule', '')}")
        start_state = shot.get("start_state") or {}
        if start_state:
            parts.append("【START STATE】" + "；".join(
                f"{name}:{state.get('position', '')},"
                f"{state.get('pose', '')},朝向{state.get('direction', '')}"
                for name, state in start_state.items()))
        # The audit prompt may be verbose, but its action line must remain
        # shot-local too; never inject the raw episode/story prompt as action.
        action = shot.get("description") or shot.get("action") or ""
        parts.append(f"【ACTION】{action or '环境状态保持稳定'}")
        dialogue = shot.get("dialogue")
        if (isinstance(dialogue, dict) and dialogue.get("dialogue")
                and dialogue.get("inner_voice")):
            parts.append(
                "【EYES / EXPRESSION】本镜台词是内心声音；由内心Q版表现"
                "夸张嘴形、表情和动作，真人宿主保持闭口，只用眼神和呼吸"
                "承接情绪；其他人物不得听见或回应")
        elif isinstance(dialogue, dict) and dialogue.get("dialogue"):
            speaker = dialogue.get("character", "")
            emo = (shot.get("speech_timing") or {}).get("emotion", "")
            parts.append(
                f"【EYES / EXPRESSION】{speaker}正在说话；"
                f"{'情绪' + emo + '；' if emo else ''}"
                "用视线、眉眼、下颌张力和呼吸体现，不把台词画成字幕")
        elif shot.get("kind") == "reaction":
            parts.append(
                "【EYES / EXPRESSION】表现听者即时反应；眼神先变，随后"
                "眉眼和呼吸产生微小变化，避免夸张表情包")
        else:
            performance = shot.get("performance") or {}
            parts.append(
                f"【EYES / EXPRESSION】{performance.get('gaze') or '视线服务主体动作'}；"
                f"{performance.get('micro_expression') or '自然微表情和呼吸'}")
        camera = shot.get("camera", "")
        contract = shot.get("shot_contract") or {}
        parts.append(
            "【COMPOSITION / CAMERA】"
            f"{contract.get('景别') or camera or '按分镜'}；"
            f"{contract.get('角度') or ''}；{contract.get('焦段') or ''}；"
            f"{contract.get('机位') or ''}；构图{contract.get('构图') or '主体清楚'}")
        physical = build_physical_contract({
            **shot, "spatial_blocking": shot_blocking(
                ctx.get("blocking"), shot.get("shot_no")) or {},
        })
        if physical.get("rules"):
            parts.append("【PHYSICAL / SPATIAL LOGIC】" + "；".join(
                physical["rules"]))
        lines = relation_lines(self._relations(ctx),
                               shot.get("characters", []))
        if lines:
            parts.append(
                "【SPATIAL RELATION·人物关系线】" + "；".join(lines))
        readable = shot.get("readable_text") or {}
        if readable_text_required(readable):
            whitelist = "、".join(readable.get("whitelist") or [])
            parts.append(
                f"【TEXT LOCK】文字载体={readable.get('carrier') or '指定载体'}；"
                f"只允许逐字出现:{whitelist or '白名单为空'}；禁止新增文字")
        else:
            parts.append("【TEXT LOCK】无画面文字；禁止字幕、乱码、Logo和水印")
        parts.append(
            "【SKIN / MATERIAL】肤色干净、均匀、通透，仅保留极轻微真实"
            "微纹理；衣料、金属、玻璃和环境材质可辨")
        parts.append(
            "【STRUCTURE】五官透视、手指、关节、肢体比例、遮挡关系与"
            "接触点自然，无粘连、断肢、穿模或漂浮道具")
        # 时代与物理硬约束:场景年代错乱与物体拉伸变形是高频真实错误。
        # 时代判断一律以剧本为准:穿越剧里剧本明写的跨时代物品是剧情
        # 事实,必须画出,绝不能被当成"时代错乱"卡掉。
        story_world = (ctx.get("script") or {}).get("story_world") or {}
        drift = [str(item).strip() for item in
                 (story_world.get("forbidden_drift") or [])
                 if str(item).strip()]
        sanctioned = [str(item).strip() for item in
                      (story_world.get("sanctioned_anachronisms") or [])
                      if str(item).strip()]
        parts.append(
            "【ERA LOCK】时代判断以本剧剧本为唯一标准:画面中每一件"
            "物品、服装、道具、建筑必须符合剧本声明的时代与世界观"
            + (f";剧情明确允许跨时代出现(必须按剧情画出,不算时代"
               f"错乱):{'、'.join(sanctioned)}" if sanctioned else "")
            + (f";严禁出现:{'、'.join(drift)}" if drift else
               ";除剧本明确写出的穿越/带入物品外,历史/古代场景不出现"
               "笔记本电脑、手机、屏幕、现代家具等现代物品")
            + ";道具形态按真实规格(笔记本电脑=合页翻盖薄板,不得拉长"
            "变形或塞进不合理容器)")
        parts.append(
            "【NEGATIVE】禁止身份漂移、性别错误、人数错误、脸部融合、"
            "重复人物、错误服装、无关杂物、脏污皮肤、塑料脸、字幕和标签")
        # 出错经验库:本项目此前真实犯过的错,逐条声明严禁再犯
        lessons = lessons_block(self.assets, ctx["project"]["id"])
        if lessons:
            parts.append("【LESSONS·严禁再犯】" + lessons)
        return "\n".join(p for p in parts if p)

    def _shot_payload(self, ctx, shot, *, continuity_anchor=False,
                      quality_override=None, item_id=None):
        locations = self._scene_locations(ctx)
        location = shot_local_scene(
            shot, locations.get(shot["scene_no"], ""))
        profile = (ctx.get("production_profile")
                   or (ctx.get("storyboard") or {}).get("profile")
                   or production_profile(
                       self.config, ctx.get("production_standard")))
        spatial = shot_blocking(ctx.get("blocking"), shot["shot_no"])
        readable_text = shot.get("readable_text", {}) or {}
        text_required = readable_text_required(readable_text)
        quality = resolve_image_quality(
            recommend_shot_image_quality(
                shot, continuity_anchor=continuity_anchor),
            ctx.get("quality_policy") or default_quality_policy(),
            item_id or f"shot:{shot['shot_no']}",
            explicit_override=quality_override)
        script_characters = {
            item.get("name"): item
            for item in (ctx.get("script") or {}).get("characters", [])
            if item.get("name")
        }
        # 身份参考图固定按剧本角色表顺序排列,不因场景生成器/字典排序而
        # 把第一位主角的参考图换成配角,同时保留镜头中的实际出场名单。
        script_order = [item.get("name") for item in (
            ctx.get("script") or {}).get("characters", [])
                        if item.get("name")]
        identity_characters = [
            name for name in script_order
            if name in shot["characters"]
            and not is_background_character(script_characters.get(name, {}))]
        identity_characters.extend(
            name for name in shot["characters"]
            if name not in identity_characters
            and not is_background_character(script_characters.get(name, {})))
        character_background = self._shot_character_facts(ctx, shot)
        character_visuals = self._shot_character_visuals(
            ctx, shot, character_background)
        composition_contract = build_composition_contract(shot)
        physical_contract = build_physical_contract({
            **shot, "spatial_blocking": spatial or {},
            "readable_text": readable_text,
        })
        narrative_overlays = [
            copy.deepcopy(item)
            for item in (shot.get("narrative_overlays") or [])
            if isinstance(item, dict)
        ][:1]
        inner_persona_ref = ""
        if narrative_overlays:
            overlay = narrative_overlays[0]
            inner_persona_ref = str(overlay.get("asset_uri") or "").strip()
            if not inner_persona_ref and overlay.get("asset_name"):
                row = self.assets.latest(
                    ctx["project"]["id"], "inner_persona",
                    str(overlay["asset_name"]))
                if row is not None:
                    inner_persona_ref = str(row["uri"] or "").strip()
                    overlay["asset_uri"] = inner_persona_ref
        payload = {
            "shot_no": shot["shot_no"],
            "unit_id": shot.get("unit_id"),
            "prompt": self._rich_shot_prompt(ctx, shot, location),
            "seedance_prompt": shot.get("seedance_prompt", shot["prompt"]),
            "characters": shot["characters"],
            "character_number_map": shot.get("character_number_map", {}),
            "identity_characters": identity_characters,
            "character_background": character_background,
            "character_visuals": character_visuals,
            "character_count": shot.get(
                "character_count", len(shot["characters"])),
            "visible_figure_count": (
                len(shot["characters"]) + len(narrative_overlays)),
            "narrative_overlays": narrative_overlays,
            "location": location,
            "dialogue": shot.get("dialogue"),
            "camera": shot.get("camera", ""),
            "action": shot.get("description", ""),
            "start_state": shot.get("start_state", {}),
            "end_state": shot.get("end_state", {}),
            "five_dimensions": shot.get("five_dimensions", {}),
            "readable_text": readable_text,
            # 正式关键帧默认中档；文字/群像/人脸情绪/连续性自动升高。
            "image_task_class": image_task_class_for(
                quality["level"], readable_text=text_required),
            "image_quality": quality["level"],
            "quality_decision": quality,
            "performance": shot.get("performance", {}),
            "shot_contract": shot.get("shot_contract", {}),
            "composition_contract": composition_contract,
            "physical_contract": physical_contract,
            "sound_design": shot.get("sound_design", {}),
            "spatial_blocking": spatial or {},
            "spatial_constraint": (spatial or {}).get("constraint", ""),
            "standard_fingerprint": profile.get("standard_fingerprint", ""),
            "forbid_subtitles": not profile["burn_subtitles"],
            "style": ctx["project"]["style"] or "",
            # The shot contract below is the complete provider instruction.
            # Providers must not append biography or episode-level context.
            "prompt_contract_complete": True,
            "aspect": ctx["aspect"], **ctx["dims"],
            **self._art_refs(
                ctx, identity_characters, location,
                shot_no=shot["shot_no"],
                sheet_keys=self._shot_reference_sheet_keys(shot),
                sheet_keys_by_character=(
                    self._shot_reference_sheet_keys_by_character(
                        shot, composition_contract)),
                spatial_ref=(
                    (spatial or {}).get("spatial_reference_uri", "")
                    if requires_spatial_reference(spatial or {}) else ""),
                inner_persona_ref=inner_persona_ref),
        }
        actor_ids = {
            actor.get("name"): actor.get("actor_id")
            for actor in (spatial or {}).get("actors", [])
            if actor.get("name") and actor.get("actor_id")
        }
        mapped_refs = []
        for reference in payload.get("identity_references", []):
            mapped = dict(reference)
            if actor_ids.get(mapped.get("character")):
                mapped["actor_id"] = actor_ids[mapped["character"]]
            mapped_refs.append(mapped)
        payload["identity_references"] = mapped_refs
        payload["character_reference_map"] = [{
            "actor_id": actor_ids.get(name, ""),
            "character": name,
            "uri": next((ref.get("uri") for ref in mapped_refs
                         if ref.get("character") == name), ""),
        } for name in shot.get("characters", [])]
        # 前置绑定:参考图对照表进提示词——每张图是谁的、参考什么,
        # 出图前就写死,而不是靠事后质检纠错
        self._attach_reference_manifest(payload)
        # 发送给图片模型的版本只保留本镜事实；完整 prompt 仍保留在
        # payload['prompt'] 供人工审计，避免故事背景、全局风格与参考图
        # 在模型输入里重复争权。
        contract, compact = compile_shot_prompt(
            {**shot,
             "character_visuals": character_visuals,
             "composition_contract": composition_contract,
             "physical_contract": physical_contract,
             "spatial_blocking": spatial or {},
             "readable_text": readable_text},
            location=location, style=payload.get("style", ""),
            references=payload.get("reference_manifest"), mode="image")
        payload["prompt_contract"] = contract
        payload["prompt_compact"] = compact
        payload["prompt_contract_validation"] = (
            validate_shot_prompt_contract(contract))
        return payload

    def _reference_manifest(self, payload):
        """按提交顺序给每张参考图编号并绑定用途(与产线上传顺序一致)。

        顺序必须与 API/CLI 产线的图片提交顺序完全相同:
        最终立绘 → 空间调度图 → 人物设定图 → 本镜关键图 →
        上一镜尾帧 → 场景图 → 风格基准 → 用户参考图。
        """
        matches = {
            match.get("uri"): match
            for match in payload.get("asset_matches", [])
            if isinstance(match, dict) and match.get("uri")
        }
        entries, seen = [], set()

        def add(uri, label, binding, character="", role="", asset_id=None,
                kind=""):
            value = str(uri or "").strip()
            if not value or value in seen:
                return
            seen.add(value)
            match = matches.get(value) or {}
            entries.append({
                "index": len(entries) + 1, "uri": value,
                "label": label, "binding": binding,
                "character": character, "role": role,
                "asset_id": (
                    asset_id if asset_id is not None
                    else match.get("asset_id")),
                "kind": kind or match.get("kind") or role,
            })

        for pos, ref in enumerate(
                payload.get("identity_references") or [], 1):
            if not isinstance(ref, dict) or not ref.get("uri"):
                continue
            who = str(ref.get("character") or "角色")
            actor = str(ref.get("actor_id") or f"P{pos:02d}")
            add(ref["uri"], f"{actor}·{who}最终立绘",
                f"只锁定{who}的脸型、五官骨相、年龄、性别表达、发际线、"
                "发型轮廓、体型与身份标志；不得复制此图的服装、姿势、"
                "构图、背景或光线，除非本镜另有明确要求；禁止参考他人图片",
                character=who, role="identity",
                asset_id=ref.get("asset_id"),
                kind="character_identity")
        add(
            payload.get("inner_persona_ref"), "内心Q版母资产",
            "只锁定非现实内心Q版的脸、发型、当前衣着、Q版比例和材质；"
            "允许本镜按剧情夸张表情与动作。它不是真实人物，不得覆盖真人"
            "身份、增加真实人数、进入空间站位、被其他人物看见或继承任何"
            "默认道具；透明背景不得画进成片",
            role="inner_persona", kind="inner_persona")
        add(payload.get("spatial_ref"), "本镜空间调度图",
            "只读取人物编号与对应站位、相对距离、前后遮挡、屏幕方向、"
            "行动箭头、摄影机起终点/高度、瞄准点和视锥；不得把3D示意视角、"
            "箭头、色块、网格或任何示意图元素画进最终画面",
            role="spatial")
        for uri in payload.get("character_refs") or []:
            match = matches.get(uri) or {}
            label = match.get("label") or "人物设定图"
            name = str(match.get("name") or "")
            who = name.split(":", 1)[0] if ":" in name else ""
            sheet = name.split(":", 1)[1] if ":" in name else ""
            if sheet in ("costume", "costume_detail"):
                binding = (
                    f"只读取{who or '对应人物'}的服装结构、材质、配色、"
                    "鞋履和配饰；不得用此图覆盖脸、年龄、性别、姿势或背景")
                role = "wardrobe"
            elif sheet in (
                    "features", "makeup", "closeup",
                    "front", "profile", "back"):
                if sheet == "back":
                    binding = (
                        f"只补充{who or '对应人物'}的后脑与发型背部轮廓、"
                        "肩背体型、服装背片/接缝、配饰和道具位置；背面不可见"
                        "五官时不得要求核脸；身份仍以最终立绘为准，不复制"
                        "姿势或背景")
                elif sheet == "profile":
                    binding = (
                        f"只补充{who or '对应人物'}的侧脸外轮廓、发型侧面"
                        "轮廓、服装、体型、配饰和道具位置；看不见的正面五官"
                        "不得作为失败理由；身份仍以最终立绘为准")
                else:
                    binding = (
                        f"只补充{who or '对应人物'}的正脸五官、脸型、年龄感、"
                        "发型和妆容细节；身份仍以最终立绘为准，不复制服装、"
                        "姿势或背景")
                role = "identity_detail"
            elif sheet == "turnaround":
                binding = (
                    f"只补充{who or '对应人物'}的体型比例、发型轮廓与"
                    "各角度结构；此合成板只作审核索引，不得取代独立"
                    "正面/侧面/背面母资产或覆盖最终立绘身份")
                role = "structure"
            else:
                binding = (
                    "只读取标签所指的单一人物属性；不得覆盖最终立绘身份，"
                    "不得把图片中的其他人物、姿势或背景带入本镜")
                role = "character_detail"
            add(uri, label,
                binding, character=who, role=role)
        add(payload.get("image_uri"), "本镜已通过的关键图",
            "只锁定本镜构图、人物站位、场景、道具、服装和已锁定文字；"
            "人物脸和性别仍以各自最终立绘为准，不得复制关键图中的错误",
            role="keyframe")
        add(payload.get("chain_first_uri"), "上一镜结尾画面",
            "只承接屏幕方向、构图、光线、人物站位、服装、道具和状态；"
            "不得用它覆盖各角色最终立绘身份", role="continuity")
        location = payload.get("location", "")
        add(payload.get("scene_ref"),
            f"场景「{location}」基准图" if location else "场景基准图",
            "只锁定空间结构、陈设、材质与主光方向；忽略图中任何人物、"
            "服装、姿势或可读文字", role="scene")
        add(payload.get("style_ref"), "全片风格基准图",
            "只读取媒介、线条/渲染、上色、材质与光影风格；忽略图中"
            "人物身份、服装、姿势、场景布局和文字", role="style")
        for uri in payload.get("reference_images") or []:
            match = matches.get(uri) or {}
            label = match.get("label") or "用户上传参考图"
            ref_role = str(match.get("reference_role") or "")
            attach = str(match.get("attach_to") or "")
            if "待修改基底" in label:
                binding = (
                    "这是当前待修改的原图；只修正修改意见指出的问题，"
                    "未提及的人物、构图、服装、道具、文字和光影保持不变")
                ref_role = "revision_base"
            elif "连续性约束" in label:
                binding = (
                    "这是同镜头另一端画面；人物身份、服装、场景、道具和"
                    "屏幕方向必须连续")
                ref_role = "continuity"
            elif "参考分镜" in label:
                binding = (
                    "只参考镜头构图、人物站位和场景关系；人物身份与性别"
                    "仍以最终立绘为准，忽略额外人物")
                ref_role = "composition"
            elif "已生产" in label:
                binding = (
                    "只承接同一人物集合、同一场景的服装/道具/光线状态；"
                    "人物身份仍以最终立绘为准")
                ref_role = "continuity"
            elif ref_role == "identity":
                binding = (
                    f"只锁定{attach or '所关联人物'}的脸、年龄、性别表达、"
                    + ("发际线/发量/发色家族和身份标志；候选阶段不改变发型和妆造，"
                       "只比较剧情服装细节、表情与轻微姿态；"
                       if payload.get("portrait_candidate") else
                       "发际线、发型轮廓和身份标志；")
                    + "不复制服装、姿势、背景或其他人物")
            elif ref_role == "wardrobe":
                binding = (
                    f"只参考{attach or '所关联对象'}的服装/道具结构、材质"
                    "与配色；不得覆盖人物脸、性别、姿势或背景")
            elif ref_role == "scene":
                binding = (
                    f"只参考{attach or '所关联场景'}的空间、陈设、材质和"
                    "光线；忽略图中人物与文字")
            elif ref_role == "composition":
                binding = (
                    "只参考构图、机位、动作路径或姿势关系；人物身份、"
                    "服装和场景分别服从对应锁定资产")
            elif ref_role == "style":
                binding = (
                    "只参考画风、渲染、材质、色彩与光影；忽略人物、服装、"
                    "姿势、场景布局和文字")
            else:
                binding = (
                    "用户手动参考图未声明用途；只作弱构图参考，不得覆盖"
                    "人物身份、性别、人数、服装、场景或已锁定文字")
                ref_role = "manual"
            add(uri, label, binding, character=attach, role=ref_role)
        if len(entries) > IMAGE_REFERENCE_LIMIT:
            labels = "、".join(entry["label"] for entry in entries)
            raise AifosError(
                f"本次需要 {len(entries)} 张参考图，超过图片模型公共上限 "
                f"{IMAGE_REFERENCE_LIMIT} 张；请拆分任务或删除低优先级参考:"
                f"{labels}")
        return entries

    def _with_reference_manifest(self, payload):
        """便捷包装:附上参考图对照表后原样返回 payload。"""
        self._attach_reference_manifest(payload)
        return payload

    def _attach_reference_manifest(self, payload):
        """把参考图对照表写进 payload 与提示词(编号=实际提交顺序)。"""
        # 同一 payload 可能在补入首/尾帧修图基底后再次绑定参考图。
        # 始终从未附表的原始提示词重建，避免“参考图对照表”重复叠加。
        base_prompt = payload.setdefault(
            "_reference_prompt_base", payload.get("prompt") or "")
        manifest = self._reference_manifest(payload)
        payload["reference_manifest"] = manifest
        payload["prompt"] = base_prompt
        if manifest:
            lines = [f"图{entry['index']}={entry['label']}:{entry['binding']}"
                     for entry in manifest]
            payload["prompt"] = (
                base_prompt.rstrip("。")
                + f"。参考图对照表(共{len(manifest)}张,按此顺序提交,"
                "每张图只允许执行其绑定用途；禁止跨用途传播、张冠李戴、"
                "把一个人的脸/服装/姿势/背景复制给另一个人):"
                + ";".join(lines))
        # QC 定向重画、人工换参考图会再次进入这里；同步刷新实际发送的
        # 短合同，避免新加入的“待修改基底”仍被旧提示词遮蔽。
        if ("shot_no" in payload and not payload.get("portrait")
                and not payload.get("character_sheet")
                and not payload.get("scene_art")):
            contract, compact = compile_shot_prompt(
                payload, location=payload.get("location", ""),
                style=payload.get("style", ""), references=manifest,
                mode="image")
            payload["prompt_contract"] = contract
            payload["prompt_compact"] = compact
            payload["prompt_contract_validation"] = (
                validate_shot_prompt_contract(contract))
        else:
            # 人物/场景资产也只把未附表的视觉合同交给 Provider。
            # 参考图及绑定职责由 manifest/真实上传通道单独传递；若继续把
            # 同一对照表塞进 prompt，Codex/Seedream 桥还会再展开一次，
            # 造成重复、超长并削弱真正的资产要求。
            payload.pop("prompt_contract", None)
            payload["prompt_compact"] = base_prompt

    def reconcile_completed_shot_images(self, ctx):
        """补登记旧批次已通过 QC、但因批次中途失败而遗留的关键帧。

        旧并行调度会先把每张图及 QC 状态写入 render_plan，随后才批量
        登记正式资产；若其中一张失败，已经通过的图片文件会留在磁盘，
        却没有进入资产中心。断点续跑前先按三重条件恢复：
        render_plan=done/reused、QC 明确通过、约定文件真实存在。失败稿
        只补 output_uri 并升级为 awaiting_human，绝不登记成正式资产。
        """
        plan = self._plan_read(ctx)
        by_id = {item.get("id"): item for item in plan.get("items", [])}
        recovered = 0
        exposed_failures = 0
        changed = False
        for shot in (ctx.get("storyboard") or {}).get("shots", []):
            shot_no = int(shot["shot_no"])
            item = by_id.get(f"shot:{shot_no}")
            if item is None:
                continue
            uri = str(item.get("output_uri") or "")
            if not uri:
                candidate = (
                    Path(ctx["out_root"]) / "images"
                    / f"shot_{shot_no:03d}.keyframe.png")
                if candidate.exists():
                    uri = str(candidate)
                    item["output_uri"] = uri
                    changed = True
            if not uri or (
                    not uri.startswith(("http://", "https://"))
                    and not Path(uri).exists()):
                continue
            qc = item.get("qc") or {}
            if qc.get("passed") is False:
                if item.get("status") != "awaiting_human":
                    item["status"] = "awaiting_human"
                    changed = True
                attempts = int(qc.get("attempts") or 1)
                qc.update({
                    "awaiting_human": True,
                    "auto_repairs": max(0, attempts - 1),
                    "auto_repair_exhausted": True,
                })
                item["qc"] = qc
                exposed_failures += 1
                changed = True
                continue
            if (item.get("status") not in ("done", "reused")
                    or qc.get("passed") is not True):
                continue
            asset_name = self._shot_name(ctx, shot_no)
            if self._existing_asset_uri(ctx, "image", asset_name):
                continue
            decision = {
                "level": item.get("image_quality") or "medium",
                "recommended": item.get("recommended_quality")
                or item.get("image_quality") or "medium",
                "source": item.get("quality_source") or "recovered",
                "rule": item.get("quality_rule") or "",
                "reasons": list(item.get("quality_reasons") or []),
            }
            recovered_meta = {
                "recovered_from_render_plan": True,
                "render_plan_item": item.get("id"),
            }
            for key in (
                    "provider", "model", "real", "fallbacks",
                    "image_task_class", "unit_cost", "qc",
                    "reference_inputs", "prompt_used", "prompt_used_hash",
                    "started_at", "finished_at", "duration"):
                if key in item:
                    recovered_meta[key] = item[key]
            self._register_shot_asset(
                ctx, "image", shot_no, uri,
                meta=self._shot_image_meta(
                    ctx, shot, decision, recovered_meta))
            recovered += 1
        if changed:
            plan["updated_at"] = now()
            self._plan_write(ctx, plan)
        if recovered or exposed_failures:
            self.log.info(
                "director",
                f"关键帧断点对账:补登记 {recovered} 张已通过图片，"
                f"保留 {exposed_failures} 张待人工失败稿")
        return {
            "recovered": recovered,
            "awaiting_human": exposed_failures,
        }

    def _preflight_plan_incomplete(self, ctx):
        """关键帧/首尾帧仍缺失或待人工时，不得显示为可确认 Seedance。"""
        for item in self._plan_read(ctx).get("items", []):
            category = item.get("category")
            if category not in ("shot_image", "frames"):
                continue
            if (item.get("status") not in ("done", "reused")
                    or (item.get("qc") or {}).get("passed") is False):
                return True
            shot_no = item.get("shot_no")
            if shot_no is None:
                continue
            asset_name = self._shot_name(ctx, int(shot_no))
            if category == "shot_image":
                if not self._existing_asset_uri(
                        ctx, "image", asset_name):
                    return True
            elif not (
                    self._existing_asset_uri(
                        ctx, "first_frame", asset_name)
                    and self._existing_asset_uri(
                        ctx, "last_frame", asset_name)):
                return True
        return False

    def _stage_images(self, ctx):
        self._plan_seed_shots(ctx)
        reconciliation = self.reconcile_completed_shot_images(ctx)
        # 生产画布:出图一开始就落盘人物/场景/镜头关系线,
        # 前端画布与出图/质检提示词共用,牵引人物关联性不漂移
        ctx["relations"] = write_relations(
            ctx["out_root"], ctx.get("script"), ctx.get("storyboard"))
        ctx["images"] = []
        reused = 0
        tasks = []
        quality_by_shot = {}
        seen_scenes = set()
        for shot in ctx["storyboard"]["shots"]:
            scene_first = shot.get("scene_no") not in seen_scenes
            seen_scenes.add(shot.get("scene_no"))
            payload = self._shot_payload(ctx, shot)
            required_quality = payload["quality_decision"]["level"]
            existing = self._existing_asset_uri(
                ctx, "image", self._shot_name(ctx, shot["shot_no"]))
            if existing:
                row = self.assets.latest(
                    ctx["project"]["id"], "image",
                    self._shot_name(ctx, shot["shot_no"]))
                actual_quality = self._asset_quality(row)
                same_content = (
                    self._asset_meta(row).get("shot_content_hash")
                    == self._shot_content_hash(shot))
                if (same_content
                        and self._quality_meets(
                            actual_quality, required_quality)):
                    ctx["images"].append(
                        {"shot_no": shot["shot_no"], "uri": existing,
                         "image_quality": actual_quality})
                    reused += 1
                    self._plan_mark(
                        ctx, f"shot:{shot['shot_no']}", "reused",
                        only_pending=True)
                    continue
            quality_by_shot[shot["shot_no"]] = payload["quality_decision"]
            tasks.append({
                "item_id": f"shot:{shot['shot_no']}",
                "capability": "image",
                "payload": payload,
                "sub_dir": "images", "tag": shot["shot_no"],
                "priority": self._shot_priority(shot, scene_first),
                "qc_spec": {**self._qc_spec(
                    ctx["project"]["id"],
                    payload.get("identity_characters", payload.get("characters", [])),
                    location=payload.get("location", ""),
                    action=payload.get("action", ""),
                    forbid=self._FORBID + ["字幕条"],
                    expected_characters=payload.get("characters", []),
                    expected_count=payload.get("character_count"),
                    character_background=payload.get(
                        "character_background", {}),
                    camera=payload.get("camera", ""),
                    composition_contract=payload.get(
                        "composition_contract"),
                    readable_text=payload.get("readable_text"),
                    physical_contract=payload.get("physical_contract"),
                    physical_logic_required=True,
                    era_exceptions=self._era_exceptions(ctx),
                    narrative_overlays=payload.get(
                        "narrative_overlays"))}})
        results, qc_failures = self._run_parallel(
            ctx, tasks, line="分镜画面",
            continue_on_qc_failure=True)
        for shot_no in sorted(results):
            result = results[shot_no]
            quality = quality_by_shot[shot_no]
            self._register_shot_asset(
                ctx, "image", shot_no, result.uri,
                meta=self._shot_image_meta(
                    ctx, next(s for s in ctx["storyboard"]["shots"]
                              if s["shot_no"] == shot_no), quality))
            ctx["images"].append({
                "shot_no": shot_no, "uri": result.uri,
                "image_quality": quality["level"]})
        ctx["images"].sort(key=lambda i: i["shot_no"])
        if qc_failures:
            failed_shots = sorted(
                int(task["tag"]) for task, _error in qc_failures)
            self.log.warn(
                "director",
                f"关键帧本批已完成其余 {len(results)} 张；"
                f"{len(failed_shots)} 张二次质检仍未通过，"
                "已隔离到待人工问题清单: "
                + "、".join(f"镜头{value}" for value in failed_shots))
            raise AifosError(
                f"{len(failed_shots)} 张关键帧自动修图 1 次后仍未通过；"
                "问题图已保留并列入待人工问题清单，其他关键帧已继续完成。"
                "请点击问题项定位并手动修改后，从断点继续。"
                "问题镜头: "
                + "、".join(str(value) for value in failed_shots))
        return {
            "count": len(ctx["images"]), "reused": reused,
            "recovered": reconciliation["recovered"],
        }

    def _stage_text_assets(self, ctx):
        """所有可读文字先由关键帧锁定；无文字单元自动通过。"""
        existing, version = self.projects.latest_document(
            ctx["episode"]["id"], "text_assets")
        if (not ctx.get("force") and existing is not None
                and existing.get("passed")):
            ctx["text_assets"] = existing
            return {"version": version, "reused": True,
                    "assets": len(existing.get("assets", [])),
                    "passed": True}
        images = {i["shot_no"]: i["uri"] for i in ctx["images"]}
        storyboard, manifest = lock_text_assets(
            ctx["storyboard"], images,
            ctx["production_profile"]["text_lock_provider"])
        if storyboard != ctx["storyboard"]:
            sb_version = self.projects.save_document(
                ctx["episode"]["id"], "storyboard", storyboard)
        else:
            _, sb_version = self.projects.latest_document(
                ctx["episode"]["id"], "storyboard")
        version = self.projects.save_document(
            ctx["episode"]["id"], "text_assets", manifest)
        ctx["storyboard"] = storyboard
        ctx["text_assets"] = manifest
        return {"version": version, "storyboard_version": sb_version,
                "assets": len(manifest["assets"]),
                "passed": manifest["passed"]}

    def _stage_frames(self, ctx):
        """首尾帧·帧链模式:同一场内「上一镜尾帧 = 下一镜首帧」,
        两段视频拼接处画面连贯;不同场之间是剪辑硬切,各自独立,
        因此按轮推进——每轮并行处理各场的第 N 镜,场内保持串行。"""
        self._plan_seed_shots(ctx)
        images = {i["shot_no"]: i for i in ctx["images"]}
        ctx["frames"] = []
        reused = 0
        chains = {}
        for shot in ctx["storyboard"]["shots"]:
            chains.setdefault(shot.get("scene_no"), []).append(shot)
        chain_list = list(chains.values())
        last_by_scene = {}
        max_len = max((len(c) for c in chain_list), default=0)
        for round_no in range(max_len):
            round_tasks = []
            for chain in chain_list:
                if round_no >= len(chain):
                    continue
                shot = chain[round_no]
                scene_no = shot.get("scene_no")
                name = self._shot_name(ctx, shot["shot_no"])
                payload = self._shot_payload(
                    ctx, shot, continuity_anchor=len(chain) > 1,
                    item_id=f"frames:{shot['shot_no']}")
                required_quality = payload["quality_decision"]["level"]
                first = self._existing_asset_uri(ctx, "first_frame", name)
                last = self._existing_asset_uri(ctx, "last_frame", name)
                if first and last:
                    first_row = self.assets.latest(
                        ctx["project"]["id"], "first_frame", name)
                    last_row = self.assets.latest(
                        ctx["project"]["id"], "last_frame", name)
                    levels = (self._asset_quality(first_row),
                              self._asset_quality(last_row))
                    frame_quality = min(
                        levels, key=("low", "medium", "high").index)
                    if self._quality_meets(
                            frame_quality, required_quality):
                        ctx["frames"].append({
                            "shot_no": shot["shot_no"], "first": first,
                            "last": last, "image_quality": frame_quality})
                        reused += 1
                        self._plan_mark(ctx, f"frames:{shot['shot_no']}",
                                        "reused", only_pending=True)
                        last_by_scene[scene_no] = {
                            "uri": last, "image_quality": frame_quality}
                        continue
                image = images[shot["shot_no"]]
                if formal_reference_allowed(
                        image.get("image_quality", "medium")):
                    payload["image_uri"] = image["uri"]
                else:
                    payload["draft_image_rejected"] = image["uri"]
                chain_first = last_by_scene.get(scene_no)
                if (round_no > 0 and chain_first
                        and formal_reference_allowed(
                            chain_first.get("image_quality", "medium"))
                        and Path(chain_first["uri"]).exists()):
                    payload["chain_first_uri"] = chain_first["uri"]
                round_tasks.append({
                    "item_id": f"frames:{shot['shot_no']}",
                    "capability": "frames",
                    "payload": payload,
                    "sub_dir": "frames", "tag": shot["shot_no"],
                    "priority": self._shot_priority(
                        shot, scene_first=round_no == 0),
                    "scene": scene_no})
            if not round_tasks:
                continue
            results = self._run_parallel(
                ctx, round_tasks,
                line=f"首尾帧帧链(第{round_no + 1}轮·各场并行)")
            for task in round_tasks:
                result = results.get(task["tag"])
                if result is None:
                    continue
                shot_no = task["tag"]
                decision = task["payload"]["quality_decision"]
                meta = self._quality_meta(decision)
                self._register_shot_asset(
                    ctx, "first_frame", shot_no, result.data["first"],
                    meta=meta)
                self._register_shot_asset(
                    ctx, "last_frame", shot_no, result.data["last"],
                    meta=meta)
                ctx["frames"].append({
                    "shot_no": shot_no,
                    "first": result.data["first"],
                    "last": result.data["last"],
                    "image_quality": decision["level"],
                })
                last_by_scene[task["scene"]] = {
                    "uri": result.data["last"],
                    "image_quality": decision["level"],
                }
        ctx["frames"].sort(key=lambda f: f["shot_no"])
        return {"count": len(ctx["frames"]), "reused": reused}

    def _stage_preflight(self, ctx):
        """确认前硬门禁：任一项未过都不能消耗 Seedance 额度。"""
        report = build_preflight(
            ctx["script"], ctx["storyboard"], ctx["continuity"],
            ctx["text_assets"], ctx["frames"], ctx["production_profile"],
            ctx.get("blocking"), ctx.get("quality_policy"))
        version = self.projects.save_document(
            ctx["episode"]["id"], "preflight", report)
        (ctx["out_root"] / "preflight_report.json").write_text(
            json.dumps(report, ensure_ascii=False, indent=2),
            encoding="utf-8")
        ctx["preflight"] = report
        if not report["passed"]:
            failed = [g["label"] for g in report["gates"]
                      if not g["passed"] and g.get("severity") != "warning"]
            raise AifosError("生产门禁未通过: " + "、".join(failed))
        return {"version": version, "passed": True,
                "gates": len(report["gates"]), "units": report["units"]}

    def _stage_videos(self, ctx):
        frames = {f["shot_no"]: f for f in ctx["frames"]}
        ctx["videos"] = []
        reused = 0
        pending = []
        for shot in ctx["storyboard"]["shots"]:
            name = self._shot_name(ctx, shot["shot_no"])
            existing = self._existing_asset_uri(ctx, "video", name)
            if existing:
                row = self.assets.latest(
                    ctx["project"]["id"], "video", name)
                meta = json.loads(row["meta"]) if row else {}
                ctx["videos"].append({
                    "shot_no": shot["shot_no"], "uri": existing,
                    "duration": shot["duration"],
                    "provider": meta.get("provider", ""),
                    "audio_in_video": meta.get("audio_in_video"),
                    "video_quality": meta.get("video_quality", "medium"),
                    "video_resolution": meta.get(
                        "video_resolution", "720p")})
                reused += 1
                continue
            pending.append(self._prepare_video_call(ctx, shot, frames))
        generated = self._run_videos_parallel(ctx, pending)
        ctx["videos"].extend(generated.values())
        ctx["videos"].sort(key=lambda item: int(item["shot_no"]))
        return {
            "count": len(ctx["videos"]),
            "reused": reused,
            "generated": len(generated),
            "parallel_workers": min(
                self._video_parallel_workers(), max(1, len(pending))),
        }

    def _ensure_spatial_reference_assets(self, ctx):
        """补齐并登记本集 Seedance 必传的逐镜空间 PNG（兼容旧项目）。"""
        blocking = ctx.get("blocking")
        if blocking is None:
            blocking, _ = self.projects.latest_document(
                ctx["episode"]["id"], "blocking")
        if not blocking:
            return {}
        mark_spatial_reference_requirements(blocking)
        required = [
            block for block in (blocking.get("shot_index") or {}).values()
            if requires_spatial_reference(block)]
        missing = [
            block for block in required
            if not block.get("spatial_reference_uri")
            or not Path(block["spatial_reference_uri"]).exists()]
        if missing:
            write_spatial_reference_pngs(
                blocking, ctx["out_root"] / "blocking" / "seedance")
            self.projects.save_document(
                ctx["episode"]["id"], "blocking", blocking)
        rows = {}
        for block in required:
            shot_no = int(block.get("shot_no") or 0)
            uri = block.get("spatial_reference_uri") or ""
            if not uri or not Path(uri).exists():
                continue
            name = (
                f"e{ctx['episode']['number']:03d}_shot{shot_no:03d}_space")
            row = self.assets.latest(
                ctx["project"]["id"], "spatial_blocking", name)
            if row is None or row["uri"] != uri:
                row = self.assets.register(
                    ctx["project"]["id"], "spatial_blocking", name,
                    uri=uri, meta={
                    "shot_no": shot_no,
                    "scene_no": block.get("scene_no"),
                    "image_quality": "high",
                    "mandatory_for_seedance": True,
                    "reason": block.get("spatial_reference_reason", ""),
                    })
            rows[shot_no] = row
        ctx["blocking"] = blocking
        return rows

    def _spatial_reference_requirement(self, ctx, shot_no):
        rows = self._ensure_spatial_reference_assets(ctx)
        block = shot_blocking(ctx.get("blocking"), shot_no) or {}
        return {
            "required": requires_spatial_reference(block),
            "ready": int(shot_no) in rows,
            "reason": block.get("spatial_reference_reason", ""),
            "row": rows.get(int(shot_no)),
            "error": block.get("spatial_reference_error", ""),
        }

    def set_video_references(self, episode_id, shot_no, asset_ids,
                             reset=False):
        """保存某镜头从资产中心人工选定的 Seedance 参考图。

        reset=True 撤销该镜头的人工选择,回落到自动选入必要参考图。
        """
        episode = self.projects.get_episode(int(episode_id))
        if episode is None:
            raise AifosError("剧集不存在")
        storyboard, _ = self.projects.latest_document(
            episode["id"], "storyboard")
        shots = (storyboard or {}).get("shots", [])
        target_shot = next((
            shot for shot in shots
            if int(shot.get("shot_no", -1)) == int(shot_no)), None)
        if target_shot is None:
            raise AifosError(f"镜头不存在: {shot_no}")
        if reset:
            document, _ = self.projects.latest_document(
                episode["id"], "video_references")
            document = document or {
                "schema": "aifos.video-references/v1", "shots": {}}
            document.setdefault("shots", {}).pop(str(int(shot_no)), None)
            document["updated_at"] = now()
            version = self.projects.save_document(
                episode["id"], "video_references", document)
            return {**document, "version": version}
        project = self.db.query_one(
            "SELECT * FROM projects WHERE id=?", (episode["project_id"],))
        script, _ = self.projects.latest_document(
            episode["id"], "script")
        ctx = {"project": dict(project), "episode": dict(episode),
               "out_root": self._episode_dir(project, episode),
               "script": script or {}}
        spatial = self._spatial_reference_requirement(ctx, shot_no)
        unique_ids = []
        for value in asset_ids or []:
            asset_id = int(value)
            if asset_id not in unique_ids:
                unique_ids.append(asset_id)
        # multimodal2video 最多 9 张；首尾帧占 2 张。空间图和每位出场
        # 人物最终立绘都是不可取消的硬参考，人工只能使用剩余槽位。
        mandatory_ids = set()
        if spatial["row"] is not None:
            mandatory_ids.add(spatial["row"]["id"])
        missing_identities = []
        for name in self._video_identity_names(ctx, target_shot):
            row = self._locked_identity(episode["project_id"], name)
            if row is None:
                missing_identities.append(name)
            else:
                mandatory_ids.add(row["id"])
        if missing_identities:
            raise AifosError(
                "以下出场角色缺少最终立绘，禁止选择 Seedance 参考图:"
                + "、".join(missing_identities))
        if len(mandatory_ids) > 7:
            raise AifosError(
                f"本镜空间图与人物最终立绘已占 {len(mandatory_ids)} 张，"
                "超过 Seedance 2 资产参考上限7张；请拆分群像镜头")
        manual_limit = max(0, 7 - len(mandatory_ids))
        unique_ids = [
            asset_id for asset_id in unique_ids
            if asset_id not in mandatory_ids]
        if len(unique_ids) > manual_limit:
            raise AifosError(
                f"本镜空间图与人物最终立绘已占 {len(mandatory_ids)} 张，"
                f"最多再选择 {manual_limit} 张额外参考图")
        selected = []
        location = self._shot_location(script, target_shot)
        for asset_id in unique_ids:
            row = self.assets.get(asset_id)
            if row is None or row["project_id"] != episode["project_id"]:
                raise AifosError(f"资产不存在或不属于本项目: {asset_id}")
            latest = self.assets.latest(
                row["project_id"], row["kind"], row["name"])
            if (latest is None or latest["id"] != row["id"]
                    or self.assets.is_deleted(row) or not row["uri"]):
                raise AifosError(f"资产已删除或不是最新版本: {asset_id}")
            if row["kind"] not in IMAGE_ASSET_KINDS:
                raise AifosError(f"资产不是可用图片: {row['kind']}")
            if row["kind"] == "spatial_blocking":
                raise AifosError("空间调度图由系统按镜头强制加入，无需人工选择")
            mismatch = self._video_reference_mismatch(
                row, target_shot, location)
            if mismatch:
                raise AifosError(mismatch)
            if not formal_reference_allowed(self._asset_quality(row)):
                raise AifosError(f"低质量候选图不能交给 Seedance: {row['name']}")
            uri = row["uri"]
            if (not uri.startswith(("http://", "https://"))
                    and not Path(uri).exists()):
                raise AifosError(f"资产文件不存在: {row['name']}")
            selected.append({
                "asset_id": row["id"], "kind": row["kind"],
                "name": row["name"], "version": row["version"],
            })
        document, _ = self.projects.latest_document(
            episode["id"], "video_references")
        document = document or {
            "schema": "aifos.video-references/v1", "shots": {}}
        document.setdefault("shots", {})[str(int(shot_no))] = selected
        document["updated_at"] = now()
        version = self.projects.save_document(
            episode["id"], "video_references", document)
        return {**document, "version": version}

    def effective_video_references(self, episode_id):
        """每个镜头实际交给 Seedance 的参考图与来源(auto=自动选入)。"""
        episode = self.projects.get_episode(int(episode_id))
        if episode is None:
            raise AifosError("剧集不存在")
        project = self.db.query_one(
            "SELECT * FROM projects WHERE id=?", (episode["project_id"],))
        ctx = {"project": dict(project), "episode": dict(episode),
               "out_root": self._episode_dir(project, episode)}
        self._ensure_spatial_reference_assets(ctx)
        storyboard, _ = self.projects.latest_document(
            episode["id"], "storyboard")
        document, _ = self.projects.latest_document(
            episode["id"], "video_references")
        manual = set(((document or {}).get("shots") or {}).keys())
        shots = {}
        for shot in (storyboard or {}).get("shots", []):
            no = shot.get("shot_no")
            if no is None:
                continue
            key = str(int(no))
            spatial = self._spatial_reference_requirement(ctx, no)
            shots[key] = {
                "mode": "manual" if key in manual else "auto",
                "spatial_reference_required": spatial["required"],
                "spatial_reference_ready": spatial["ready"],
                "spatial_reference_reason": spatial["reason"],
                "spatial_reference_error": spatial["error"],
                "items": [{
                    "asset_id": row["id"], "kind": row["kind"],
                    "name": row["name"], "uri": row["uri"],
                    "binding": self._video_reference_binding(row),
                } for row in self._video_reference_rows(ctx, no)],
            }
        return {"schema": "aifos.video-references-effective/v1",
                "shots": shots}

    def _auto_video_reference_rows(self, ctx, shot_no):
        """人工未选择时,按剧本/分镜自动集合本镜必要参考图。

        顺序:空间图 → 出场人物最终立绘 → 本镜分镜图 → 场景概念图
        → 与本镜对象精确关联的用户参考图;
        全部限中/高质量且文件在盘,最多 7 张(multimodal2video 上限内)。
        """
        project_id = ctx["project"]["id"]
        storyboard = ctx.get("storyboard")
        if storyboard is None:
            storyboard, _ = self.projects.latest_document(
                ctx["episode"]["id"], "storyboard")
        shot = next((s for s in (storyboard or {}).get("shots", [])
                     if int(s.get("shot_no", -1)) == int(shot_no)), None)
        if shot is None:
            return []
        script = ctx.get("script")
        if script is None:
            script, _ = self.projects.latest_document(
                ctx["episode"]["id"], "script")
        location = next(
            (scene.get("location", "")
             for scene in (script or {}).get("scenes", [])
             if scene.get("scene_no") == shot.get("scene_no")), "")
        rows, seen = [], set()

        def usable(row):
            if (row is None or self.assets.is_deleted(row)
                    or not row["uri"]):
                return False
            if not formal_reference_allowed(self._asset_quality(row)):
                return False
            uri = row["uri"]
            return (uri.startswith(("http://", "https://"))
                    or Path(uri).exists())

        def add(row):
            if row is not None and row["id"] not in seen and usable(row):
                seen.add(row["id"])
                rows.append(row)

        # 空间图是多人/变机位镜头的硬参考，优先占位且不可被人工选择覆盖。
        spatial = self._spatial_reference_requirement(ctx, shot_no)
        add(spatial["row"])
        missing = []
        for name in self._video_identity_names(ctx, shot):
            identity = self._locked_identity(project_id, name)
            if identity is None:
                missing.append(name)
            else:
                add(identity)
        inner_row = self._inner_persona_row_for_shot(ctx, shot)
        if (shot.get("narrative_overlays") and inner_row is None):
            raise AifosError(
                f"镜头{shot_no}声明了内心Q版，但对应母资产缺失或质量不足")
        add(inner_row)
        if missing:
            raise AifosError(
                "以下出场角色缺少最终立绘，禁止交给 Seedance:"
                + "、".join(missing))
        if len(rows) > 7:
            raise AifosError(
                f"镜头{shot_no}的空间图与人物最终立绘已占{len(rows)}张，"
                "超过 Seedance 2 资产参考上限7张；请拆分群像镜头")
        # 本镜分镜示例图承载构图事实；它即使是低档候选也可在首尾帧之外
        # 作为构图参考，但不能挤掉必传空间图与人物身份图。
        shot_image = self.assets.latest(
            project_id, "image",
            f"e{ctx['episode']['number']:03d}_shot{int(shot_no):03d}")
        if (len(rows) < 7 and shot_image is not None
                and not self.assets.is_deleted(shot_image)
                and shot_image["uri"]
                and (shot_image["uri"].startswith(("http://", "https://"))
                     or Path(shot_image["uri"]).exists())
                and shot_image["id"] not in seen):
            seen.add(shot_image["id"])
            rows.append(shot_image)
        if location and len(rows) < 7:
            add(self.assets.latest(project_id, "scene_art", location))
        wanted = list(shot.get("characters") or [])
        if location:
            wanted.append(location)
        for row in self._reference_rows(
                project_id, wanted,
                allowed_roles={
                    "identity", "wardrobe", "scene", "composition", "style"},
                include_global_style=True):
            if len(rows) >= 7:
                break
            add(row)
        return rows

    @staticmethod
    def _shot_location(script, shot):
        """按场号解析镜头场景名，兼容字符串/整数场号。"""
        scene_no = shot.get("scene_no")
        fallback = next((
            str(scene.get("location") or "")
            for scene in (script or {}).get("scenes", [])
            if str(scene.get("scene_no")) == str(scene_no)), "")
        return shot_local_scene(shot, fallback)

    def _video_identity_names(self, ctx, shot):
        """Seedance 必传最终立绘名单；背景路人不建立独立母资产。"""
        script = ctx.get("script")
        if script is None:
            script, _ = self.projects.latest_document(
                ctx["episode"]["id"], "script")
            ctx["script"] = script or {}
        profiles = {
            item.get("name"): item
            for item in (script or {}).get("characters", [])
            if isinstance(item, dict) and item.get("name")
        }
        return [
            name for name in (shot.get("characters") or [])
            if not is_background_character(profiles.get(name, {}))]

    def _inner_persona_row_for_shot(self, ctx, shot):
        overlays = [
            item for item in (shot.get("narrative_overlays") or [])
            if isinstance(item, dict)
        ][:1]
        if not overlays:
            return None
        name = str(overlays[0].get("asset_name") or "").strip()
        if not name:
            return None
        row = self.assets.latest(
            ctx["project"]["id"], "inner_persona", name)
        if (row is None or self.assets.is_deleted(row) or not row["uri"]
                or not formal_reference_allowed(self._asset_quality(row))):
            return None
        uri = str(row["uri"])
        if not (uri.startswith(("http://", "https://"))
                or Path(uri).exists()):
            return None
        return row

    def _video_reference_mismatch(self, row, shot, location=""):
        """返回 Seedance 资产与当前镜头不兼容的原因；空串表示可用。

        这个检查既用于新选择，也用于读取历史选择，避免旧版本保存的
        错角色/错场景参考图在升级后继续悄悄污染视频。
        """
        characters = set(shot.get("characters") or [])
        kind = row["kind"]
        if kind == "inner_persona":
            asset_name = str(row["name"] or "")
            expected = {
                str(item.get("asset_name") or "")
                for item in (shot.get("narrative_overlays") or [])
                if isinstance(item, dict)
            }
            if asset_name not in expected:
                return (
                    f"内心Q版资产「{asset_name}」未在本镜声明，"
                    "禁止作为额外人物参考")
        if kind in (
                "character_identity", "character_art",
                "character_candidate", "character_sheet"):
            character = (
                self._asset_meta(row).get("character")
                or str(row["name"]).split(":", 1)[0])
            if character not in characters:
                return (
                    f"资产「{row['name']}」属于未出场角色{character}，"
                    "禁止作为本镜人物参考")
        if kind == "reference":
            meta = self._asset_meta(row)
            attach = str(meta.get("attach_to") or "").strip()
            valid_targets = set(characters)
            if location:
                valid_targets.add(location)
            if attach and attach not in valid_targets:
                return (
                    f"参考图「{row['name']}」关联到{attach}，"
                    "与本镜人物/场景不匹配")
            role = self._reference_role(row)
            if role == "manual" and not attach:
                # 旧数据可保留手动选择能力，但只按弱构图使用。
                return ""
        return ""

    def _video_reference_rows(self, ctx, shot_no):
        document, _ = self.projects.latest_document(
            ctx["episode"]["id"], "video_references")
        shots_doc = (document or {}).get("shots", {})
        if str(int(shot_no)) not in shots_doc:
            # 人工没选过 = 自动选入必要参考图;人工选择(含清空)优先
            return self._auto_video_reference_rows(ctx, shot_no)
        selected = shots_doc.get(str(int(shot_no)), [])
        spatial = self._spatial_reference_requirement(ctx, shot_no)
        rows = [spatial["row"]] if spatial["row"] is not None else []
        seen = {row["id"] for row in rows}
        storyboard = ctx.get("storyboard")
        if storyboard is None:
            storyboard, _ = self.projects.latest_document(
                ctx["episode"]["id"], "storyboard")
        shot = next((
            item for item in (storyboard or {}).get("shots", [])
            if int(item.get("shot_no", -1)) == int(shot_no)), {})
        script = ctx.get("script")
        if script is None:
            script, _ = self.projects.latest_document(
                ctx["episode"]["id"], "script")
        location = self._shot_location(script, shot)
        # 人工选择只能调整“额外参考”，不能取消每位出场人物的最终立绘。
        missing = []
        for name in self._video_identity_names(ctx, shot):
            row = self._locked_identity(ctx["project"]["id"], name)
            if row is None:
                missing.append(name)
            elif row["id"] not in seen:
                seen.add(row["id"])
                rows.append(row)
        inner_row = self._inner_persona_row_for_shot(ctx, shot)
        if shot.get("narrative_overlays") and inner_row is None:
            raise AifosError(
                f"镜头{shot_no}声明了内心Q版，但对应母资产缺失或质量不足")
        if inner_row is not None and inner_row["id"] not in seen:
            seen.add(inner_row["id"])
            rows.append(inner_row)
        if missing:
            raise AifosError(
                "以下出场角色缺少最终立绘，禁止交给 Seedance:"
                + "、".join(missing))
        for item in selected:
            row = self.assets.get(item.get("asset_id"))
            if row is None or row["project_id"] != ctx["project"]["id"]:
                continue
            latest = self.assets.latest(
                row["project_id"], row["kind"], row["name"])
            if (latest is None or latest["id"] != row["id"]
                    or self.assets.is_deleted(row) or not row["uri"]
                    or not formal_reference_allowed(self._asset_quality(row))):
                continue
            if self._video_reference_mismatch(row, shot, location):
                continue
            uri = row["uri"]
            if (uri.startswith(("http://", "https://"))
                    or Path(uri).exists()) and row["id"] not in seen:
                seen.add(row["id"])
                rows.append(row)
        if len(rows) > 7:
            raise AifosError(
                f"镜头{shot_no}的空间图、人物最终立绘与人工参考共"
                f"{len(rows)}张，超过 Seedance 2 资产参考上限7张；"
                "请减少人工额外参考或拆分群像镜头")
        return rows

    def _video_reference_binding(self, row):
        """Seedance 每张资产的单一职责，禁止“身份/服装/场景全都锁”。"""
        kind = row["kind"]
        meta = self._asset_meta(row)
        name = str(row["name"] or "")
        if kind == "spatial_blocking":
            return (
                "只读取人物编号、起终站位、行动箭头、屏幕方向和摄影机"
                "起终点；不得把俯视图、文字、坐标、箭头或图形画进成片")
        if kind == "inner_persona":
            return (
                "只锁定非现实内心Q版的脸、发型、当前衣着、Q版比例和材质；"
                "允许按本镜做夸张表情动作。不得转化为真人、增加真实人数、"
                "进入物理站位、被其他人物感知或携带默认道具")
        if kind in (
                "character_identity", "character_art",
                "character_candidate"):
            character = str(meta.get("character") or name.split(":", 1)[0])
            return (
                f"只锁定{character}的脸型、五官骨相、年龄、性别表达、"
                "发际线、发型轮廓、体型与身份标志；服装、姿势、背景和"
                "光线服从首尾帧，不得复制给其他人")
        if kind == "character_sheet":
            character = str(meta.get("character") or name.split(":", 1)[0])
            sheet = str(meta.get("sheet") or (
                name.split(":", 1)[1] if ":" in name else ""))
            if sheet in ("costume", "costume_detail"):
                return (
                    f"只参考{character}的服装/道具结构、材质和配色；"
                    "不得覆盖脸、性别、动作、场景或其他人物")
            return (
                f"只补充{character}的体型/五官/发型或妆容细节；"
                "身份仍以最终立绘为准，不复制服装、姿势或背景")
        if kind == "scene_art":
            return (
                "只锁定场景空间、陈设、材质与主光方向；忽略图中人物、"
                "动作、服装和文字")
        if kind in ("image", "first_frame", "last_frame"):
            return (
                "只参考构图、人物站位、服装/道具状态、屏幕方向和光线"
                "连续性；人物身份和性别仍以最终立绘为准，忽略额外人物")
        if kind == "reference":
            role = self._reference_role(row)
            attach = str(meta.get("attach_to") or "所关联对象")
            return {
                "identity": (
                    f"只锁定{attach}的脸、年龄、性别表达、发型轮廓和"
                    "身份标志；不复制服装、姿势、背景或其他人物"),
                "wardrobe": (
                    f"只参考{attach}的服装/道具结构、材质与配色；"
                    "不得覆盖人物身份、动作或场景"),
                "scene": (
                    f"只参考{attach}的空间、陈设、材质和光线；"
                    "忽略人物、服装与文字"),
                "composition": (
                    "只参考构图、机位、动作路径和姿势关系；"
                    "人物身份、服装和场景服从各自锁定资产"),
                "style": (
                    "只参考画风、渲染、材质、色彩与光影；"
                    "忽略人物、服装、姿势、场景布局和文字"),
                "manual": (
                    "未声明用途，只作弱构图参考；不得覆盖人物身份、"
                    "性别、人数、服装、场景或已锁定文字"),
            }[role]
        return (
            "只按资产类别提供弱连续性参考；不得覆盖首尾帧、人物最终"
            "立绘、准确人数、性别或已锁定文字")

    def _seedance_video_prompt(self, ctx, shot, reference_manifest):
        """把旧/新分镜统一成短、硬、单动作的 Seedance 提示词。"""
        characters = list(shot.get("characters") or [])
        number_map = shot.get("character_number_map") or {}
        actor_by_name = {
            item.get("name"): actor_id
            for actor_id, item in number_map.items()
            if isinstance(item, dict) and item.get("name")
        }
        mapped = "、".join(
            f"{actor_by_name.get(name) or f'P{index:02d}'}={name}"
            for index, name in enumerate(characters, 1)) or "无人"

        def state_line(states):
            values = []
            for name, state in (states or {}).items():
                values.append(
                    f"{name}:{state.get('position', '')},"
                    f"{state.get('pose', '')},情绪{state.get('emotion', '')},"
                    f"朝向{state.get('direction', '')}")
            return "；".join(values)

        camera = ((shot.get("five_dimensions") or {}).get(
            "camera_design") or {})
        movement = str(camera.get("movement")
                       or (shot.get("shot_contract") or {}).get("运镜")
                       or "固定")
        performance = shot.get("performance") or {}
        spatial = shot_blocking(ctx.get("blocking"), shot["shot_no"]) or {}
        spatial_rule = str(spatial.get("constraint") or "").strip()
        readable = shot.get("readable_text") or {}
        if readable_text_required(readable):
            text_rule = (
                "首帧已有文字只做像素级保持，不新增、不改写、不重排；"
                "视频模型不得从零生成文字")
        else:
            text_rule = "画面无字幕、无新增文字、无Logo、无水印"
        dialogue = shot.get("dialogue") or {}
        if dialogue.get("dialogue") and dialogue.get("inner_voice"):
            dialogue_rule = (
                "；台词由非现实内心Q版发声并做夸张Q版口型，真人宿主"
                "全程闭口，只用眼神、呼吸和细小停顿承接")
        else:
            dialogue_rule = (
                f"；{dialogue.get('character')}按自然口型说出台词，"
                "说话时保留眼神、呼吸和细小停顿"
                if dialogue.get("dialogue") else "")
        lines = [
            "【输入边界】图1首帧是唯一动作起点；图2尾帧是唯一动作终点。",
            f"【人物硬锁】{mapped}；成片从头到尾严格共{len(characters)}人，"
            "不得新增、缺失、复制、合并、换脸或换性别；"
            "脸和性别按各自最终立绘，服装/道具按首尾帧。",
            f"【起点】{state_line(shot.get('start_state')) or '保持首帧可见状态'}。",
            f"【单一主动作】{shot.get('description') or shot.get('prompt') or '环境自然变化'}。",
            f"【表演】{performance.get('gaze') or '视线服务主体动作'}；"
            f"{performance.get('micro_expression') or '自然微表情、呼吸和重心变化'}"
            f"{dialogue_rule}。",
            f"【运镜】只执行一次{movement}；"
            f"{camera.get('shot_scale') or (shot.get('shot_contract') or {}).get('景别') or '保持既定景别'}，"
            f"{camera.get('angle') or (shot.get('shot_contract') or {}).get('角度') or '保持轴线'}；"
            f"动机={camera.get('movement_motivation') or '服务主体动作'}。",
        ]
        if spatial_rule:
            lines.append(f"【空间路径】{spatial_rule}")
        lines.extend([
            f"【终点】{state_line(shot.get('end_state')) or '准确到达尾帧状态'}。",
            f"【文字】{text_rule}。",
        ])
        if reference_manifest:
            lines.append(
                "【资产图单一职责】"
                + "；".join(
                    f"图{item['index']}={item['kind']}/{item['name']}："
                    f"{item['binding']}" for item in reference_manifest))
        lines.append(
            "【禁止】不得把空间示意图、编号、姓名标签、坐标、箭头、"
            "参考图边框、字幕或乱码画进成片；不得执行第二个主动作或"
            "第二种运镜；不得在首尾帧之间改变人物、服装、道具或场景。")
        # 与出图同一套时代/物理硬约束 + 出错经验库,视频端同步防错。
        # 时代判断以剧本为准:剧本明写的穿越/带入物品必须保留,不算错。
        world = (ctx.get("script") or {}).get("story_world") or {}
        drift = [str(item).strip() for item in
                 (world.get("forbidden_drift") or []) if str(item).strip()]
        sanctioned = [str(item).strip() for item in
                      (world.get("sanctioned_anachronisms") or [])
                      if str(item).strip()]
        lines.append(
            "【时代与物理】时代判断以本剧剧本为唯一标准；全程物品、服装、"
            "建筑必须符合剧本声明的时代与世界观"
            + (f"；剧情明确允许跨时代出现(必须保留,不算错):"
               f"{'、'.join(sanctioned)}" if sanctioned else "")
            + (f"，严禁出现:{'、'.join(drift)}" if drift else
               "，除剧本明确写出的穿越/带入物品外,历史/古代场景不出现"
               "笔记本电脑、手机等现代物品")
            + "；物体在运动中保持真实结构与比例,严禁拉长、扭曲、穿模。")
        lessons = lessons_block(self.assets, ctx["project"]["id"])
        if lessons:
            lines.append("【严禁再犯】" + lessons)
        return "\n".join(lines)

    @staticmethod
    def _video_input_snapshot(payload, result=None):
        """Freeze the exact director-side Seedance inputs for audit/retry.

        Providers may add transport-only suffixes (duration/audio flags), so a
        future adapter can return ``prompt_used``.  Until then the compact
        prompt is the exact prompt body handed to every video provider.
        """
        result_data = getattr(result, "data", {}) or {}
        prompt_sent = str(
            result_data.get("prompt_used")
            or payload.get("prompt_compact")
            or payload.get("prompt")
            or "")
        manifest = []
        for item in payload.get("reference_manifest") or []:
            if not isinstance(item, dict):
                continue
            manifest.append({
                key: item.get(key) for key in (
                    "index", "asset_id", "kind", "name", "version", "uri",
                    "binding")
            })
        snapshot = {
            "schema": "aifos.video-input-snapshot/v1",
            "shot_no": payload.get("shot_no"),
            "prompt_sent": prompt_sent,
            "prompt_sent_hash": hashlib.sha256(
                prompt_sent.encode("utf-8")).hexdigest(),
            "prompt_contract": copy.deepcopy(
                payload.get("prompt_contract") or {}),
            "keyframe": str(payload.get("keyframe") or ""),
            "first_frame": str(payload.get("first") or ""),
            "last_frame": str(payload.get("last") or ""),
            "reference_manifest": manifest,
            "reference_images": [
                str(uri) for uri in (payload.get("reference_images") or [])],
            "reference_images_used": [
                str(uri) for uri in (
                    result_data.get("reference_images_used")
                    or payload.get("reference_images") or [])],
            "duration": payload.get("duration"),
            "video_quality": payload.get("video_quality"),
            "video_resolution": payload.get("video_resolution"),
            "standard_fingerprint": payload.get("standard_fingerprint", ""),
        }
        signature_source = {
            key: snapshot[key] for key in (
                "prompt_sent", "keyframe", "first_frame", "last_frame",
                "reference_manifest", "reference_images", "duration",
                "video_quality", "video_resolution", "standard_fingerprint")
        }
        snapshot["input_signature"] = hashlib.sha256(json.dumps(
            signature_source, ensure_ascii=False, sort_keys=True,
            separators=(",", ":")).encode("utf-8")).hexdigest()
        return snapshot

    @staticmethod
    def _video_diagnosis_from_ctx(ctx, shot_no):
        """Read visual/input diagnosis without coupling Director to its engine."""
        candidates = (
            ctx.get("video_input_diagnoses"),
            ctx.get("video_visual_qc"),
        )
        for source in candidates:
            if isinstance(source, dict):
                raw = source.get(shot_no)
                if raw is None:
                    raw = source.get(str(shot_no))
                if raw is None and int(source.get("shot_no") or -1) == shot_no:
                    raw = source
                if isinstance(raw, dict):
                    return copy.deepcopy(
                        raw.get("input_diagnosis")
                        if isinstance(raw.get("input_diagnosis"), dict)
                        else raw)
            elif isinstance(source, list):
                raw = next((
                    item for item in source if isinstance(item, dict)
                    and int(item.get("shot_no") or -1) == shot_no), None)
                if raw:
                    return copy.deepcopy(
                        raw.get("input_diagnosis")
                        if isinstance(raw.get("input_diagnosis"), dict)
                        else raw)
        return {}

    @staticmethod
    def _video_frame_diagnosis_failed(diagnosis):
        frame = diagnosis.get("frame_audit") or {}
        if not isinstance(frame, dict):
            return False
        explicit = (
            frame.get("source_frames_valid"),
            frame.get("first_valid"),
            frame.get("last_valid"),
            frame.get("keyframe_valid"),
            frame.get("continuity_valid"),
        )
        return any(value is False for value in explicit)

    @staticmethod
    def _video_repair_feedback(item):
        """Render only the targeted repair, not the whole QC transcript."""
        decision = item.get("decision") or {}
        value = (
            decision.get("revised_prompt")
            or decision.get("prompt_patch")
            or item.get("prompt_patch"))
        if isinstance(value, dict):
            value = json.dumps(
                value, ensure_ascii=False, sort_keys=True,
                separators=(",", ":"))
        value = str(value or "").strip()
        if value:
            return "【结构化输入修订】" + value[:1200]
        return str(item.get("revision_feedback") or "")[:1200]

    def _latest_video_input_meta(self, ctx, shot_no):
        row = self.assets.latest(
            ctx["project"]["id"], "video",
            self._shot_name(ctx, int(shot_no)))
        return self._asset_meta(row) if row is not None else {}

    def _prepare_video_call(self, ctx, shot, frames):
        """在主线程锁定单镜首尾帧、人物/场景资产和空间图映射。"""
        shot_no = int(shot["shot_no"])
        diagnosis = self._video_diagnosis_from_ctx(ctx, shot_no)
        decision = diagnosis.get("decision") or {}
        if (decision.get("action") == "repair_frames_first"
                or self._video_frame_diagnosis_failed(diagnosis)):
            raise AifosError(
                f"镜头{shot_no}的源关键帧/首尾帧诊断未通过，"
                "必须先修复上游帧，禁止消耗 Seedance 额度")
        frame = frames[shot["shot_no"]]
        if not formal_reference_allowed(
                frame.get("image_quality", "medium")):
            raise AifosError(
                f"镜头{shot['shot_no']}首尾帧为低质量试错图，禁止交给 Seedance")
        quality = resolve_video_quality(
            ctx.get("quality_policy") or default_quality_policy(),
            shot_no=shot["shot_no"])
        reference_rows = self._video_reference_rows(ctx, shot["shot_no"])
        spatial = self._spatial_reference_requirement(
            ctx, shot["shot_no"])
        if spatial["required"] and not (
                spatial["ready"]
                and any(row["kind"] == "spatial_blocking"
                        for row in reference_rows)):
            detail = spatial["error"] or "空间 PNG 未生成或未上传"
            raise AifosError(
                f"镜头{shot['shot_no']}为多人/变机位镜头，"
                f"必须给 Seedance 2 上传空间调度示意图:{detail}")
        reference_assets = [{
            "asset_id": row["id"], "kind": row["kind"],
            "name": row["name"], "version": row["version"],
        } for row in reference_rows]
        reference_manifest = [{
            "index": index + 3,
            **asset,
            "uri": row["uri"],
            "binding": self._video_reference_binding(row),
        } for index, (row, asset) in enumerate(
            zip(reference_rows, reference_assets))]
        video_prompt = self._seedance_video_prompt(
            ctx, shot, reference_manifest)
        video_refs = [
            {"index": 1, "label": "首帧", "kind": "first_frame"},
            {"index": 2, "label": "尾帧", "kind": "last_frame"},
            *reference_manifest,
        ]
        contract, compact = compile_shot_prompt(
            shot, location=self._shot_location(ctx.get("script"), shot),
            style=ctx["project"].get("style", ""), references=video_refs,
            mode="video")
        manual_feedback = (ctx.get("video_feedback") or {}).get(
            int(shot["shot_no"]), "")
        if manual_feedback:
            video_prompt += (
                "\n【人工修订意见】这是人工检查后确认的本镜修订要求，"
                "只修正以下问题并保持其余首尾帧、人物、服装、场景和口型不变："
                + str(manual_feedback)[:1200])
            compact += (
                "\n【质检修订】只落实以下修订要求，其他首尾帧、人物、服装、"
                "场景、构图和口型保持不变：" + str(manual_feedback)[:1600])
        video_dialogue = copy.deepcopy(shot.get("dialogue"))
        if (isinstance(video_dialogue, dict)
                and video_dialogue.get("inner_voice")):
            video_dialogue["character"] = (
                video_dialogue.get("performed_by")
                or video_dialogue.get("character"))
        payload = {
            "shot_no": shot["shot_no"],
            "unit_id": shot.get("unit_id"),
            "prompt": video_prompt,
            "prompt_compact": compact,
            "prompt_contract": contract,
            "prompt_full": video_prompt,
            "duration": shot["duration"],
            "keyframe": next((
                item.get("uri", "") for item in (ctx.get("images") or [])
                if int(item.get("shot_no") or -1) == shot_no), ""),
            "first": frame["first"],
            "last": frame["last"],
            "reference_images": [row["uri"] for row in reference_rows],
            "reference_assets": reference_assets,
            "reference_manifest": reference_manifest,
            "dialogue": video_dialogue,
            "voice": ctx["production_profile"]["voice"],
            "lip_sync": ctx["production_profile"]["lip_sync"],
            "forbid_subtitles": not ctx["production_profile"]["burn_subtitles"],
            "video_quality": quality["level"],
            "video_resolution": quality["resolution"],
            "standard_fingerprint": ctx["production_profile"].get(
                "standard_fingerprint", ""),
            "aspect": ctx["aspect"], **ctx["dims"],
        }
        snapshot = self._video_input_snapshot(payload)
        payload["input_snapshot"] = snapshot
        payload["input_signature"] = snapshot["input_signature"]
        if diagnosis:
            payload["input_diagnosis"] = copy.deepcopy(diagnosis)
        previous_signature = str(
            self._latest_video_input_meta(ctx, shot_no).get(
                "input_signature") or "")
        if (decision.get("action") == "direct_video_retry"
                and previous_signature
                and previous_signature == snapshot["input_signature"]):
            raise AifosError(
                f"镜头{shot_no}的提示词、首尾帧与参考图均未改变，"
                "禁止原样重复调用 Seedance")
        return {
            "shot": shot,
            "payload": payload,
            "quality": quality,
            "reference_assets": reference_assets,
            "reference_manifest": reference_manifest,
        }

    def _finish_video_call(self, ctx, task, result):
        """在主线程登记单镜视频，避免并发 worker 争抢资产版本。"""
        shot = task["shot"]
        quality = task["quality"]
        reference_assets = task["reference_assets"]
        reference_manifest = task["reference_manifest"]
        provider = self.router.providers.get(result.provider)
        audio_in_video = bool(
            provider and provider.conf.get("audio_in_video"))
        # mock 是正式有声产线的离线契约模拟；它会把实际执行的
        # voice/lip_sync 回写结果，用结果而不是仅凭 production profile 判定。
        if (provider and "audio_in_video" not in provider.conf
                and provider.conf.get("type") == "mock"
                and result.data.get("voice") == "jimeng_builtin"
                and result.data.get("lip_sync")):
            audio_in_video = True
        input_snapshot = self._video_input_snapshot(task["payload"], result)
        previous_meta = self._latest_video_input_meta(
            ctx, int(shot["shot_no"]))
        attempt_history = list(previous_meta.get("attempt_history") or [])
        attempt_history.append({
            "attempt": len(attempt_history) + 1,
            "generated_at": now(),
            "provider": result.provider,
            "model": getattr(result, "model", "") or "",
            "input_signature": input_snapshot["input_signature"],
            "prompt_sent_hash": input_snapshot["prompt_sent_hash"],
            "reference_assets": copy.deepcopy(reference_assets),
            "first_frame": input_snapshot["first_frame"],
            "last_frame": input_snapshot["last_frame"],
        })
        attempt_history = attempt_history[-20:]
        self._register_shot_asset(ctx, "video", shot["shot_no"], result.uri,
                                  meta={"provider": result.provider,
                                        "model": getattr(result, "model", ""),
                                        "audio_in_video": audio_in_video,
                                        "video_quality": quality["level"],
                                        "video_resolution": quality["resolution"],
                                        "quality_source": quality["source"],
                                        "reference_assets": reference_assets,
                                        "reference_manifest":
                                            reference_manifest,
                                        "input_snapshot": input_snapshot,
                                        "input_signature":
                                            input_snapshot["input_signature"],
                                        "attempt_history": attempt_history})
        return {"shot_no": shot["shot_no"], "uri": result.uri,
                "duration": shot["duration"], "provider": result.provider,
                "audio_in_video": audio_in_video,
                "video_quality": quality["level"],
                "video_resolution": quality["resolution"],
                "reference_assets": reference_assets,
                "reference_manifest": reference_manifest,
                "input_snapshot": input_snapshot,
                "input_signature": input_snapshot["input_signature"],
                "attempt_history": attempt_history}

    def _run_videos_parallel(self, ctx, tasks):
        """有界并行生成 Seedance 视频，完成一镜就立即记账并沉淀资产。

        worker 只执行 Provider 调用；提示词、首尾帧、人物与空间参考图均在
        提交前按镜头冻结。暂停/失败后不再派发新镜头，已完成视频继续保留。
        """
        if not tasks:
            return {}
        workers = min(self._video_parallel_workers(), len(tasks))
        if workers == 1:
            output = {}
            for task in tasks:
                result = self._call(
                    ctx, "video", task["payload"], "videos")
                video = self._finish_video_call(ctx, task, result)
                output[int(video["shot_no"])] = video
            return output
        if self._cancel_requested(ctx):
            raise ProduceCancelled("已手动停止生成")
        episode = self.projects.get_episode(ctx["episode"]["id"])
        budget = self.config.get("budget", "per_episode", default=0)
        if budget and episode["cost"] >= budget:
            raise BudgetExceeded(
                f"单集成本 {episode['cost']:.2f} 已达预算 {budget},停止调度")

        from concurrent.futures import FIRST_COMPLETED, ThreadPoolExecutor, wait

        self.log.info(
            "director",
            f"Seedance 视频并行开工:共 {len(tasks)} 个镜头,"
            f"{workers} 路同时生成")
        cancel = lambda: self._cancel_requested(ctx)   # noqa: E731
        output, failures = {}, []
        cancelled = False
        started_at = time.monotonic()
        with ThreadPoolExecutor(max_workers=workers) as pool:
            iterator = iter(tasks)
            futures = {}

            def submit_next():
                try:
                    task = next(iterator)
                except StopIteration:
                    return False
                future = pool.submit(
                    self.router.call, "video", task["payload"],
                    ctx["out_root"] / "videos", cancel)
                futures[future] = task
                return True

            for _ in range(workers):
                submit_next()
            while futures:
                done_now, _ = wait(
                    set(futures), timeout=2,
                    return_when=FIRST_COMPLETED)
                for future in done_now:
                    task = futures.pop(future)
                    try:
                        result = future.result()
                    except ProduceCancelled:
                        cancelled = True
                        continue
                    except Exception as exc:
                        failures.append((task, exc))
                        continue
                    self._task_cost += result.cost
                    self._task_providers.add(result.provider)
                    self.projects.add_episode_cost(
                        ctx["episode"]["id"], result.cost)
                    video = self._finish_video_call(ctx, task, result)
                    output[int(video["shot_no"])] = video
                if (not cancelled and not failures
                        and not self._cancel_requested(ctx)):
                    while len(futures) < workers and submit_next():
                        pass

        elapsed = max(.001, time.monotonic() - started_at)
        self.log.info(
            "director",
            f"Seedance 视频本批完成 {len(output)}/{len(tasks)}，"
            f"墙钟 {elapsed:.1f}s，吞吐 "
            f"{len(output) * 60 / elapsed:.2f} 镜头/分钟")
        if cancelled or self._cancel_requested(ctx):
            raise ProduceCancelled(
                "已手动暂停(本批已完成的视频全部保留)")
        if failures:
            raise failures[0][1]
        return output

    def _make_video(self, ctx, shot, frames):
        """兼容单镜调用；正式视频阶段由 4 路并行调度器执行。"""
        task = self._prepare_video_call(ctx, shot, frames)
        result = self._call(ctx, "video", task["payload"], "videos")
        return self._finish_video_call(ctx, task, result)

    def _video_audio_states(self, ctx):
        """按实际视频资产/Provider 声明返回每镜是否内置配音。"""
        states = []
        for video in ctx.get("videos") or []:
            provider = self.router.providers.get(video.get("provider", ""))
            declared = (video.get("audio_in_video")
                        if video.get("audio_in_video") is not None
                        else (provider.conf.get("audio_in_video")
                              if provider is not None else False))
            states.append(bool(declared))
        return states

    def _videos_carry_audio(self, ctx):
        """视频是否全部自带配音(Seedance2 有声视频)。"""
        states = self._video_audio_states(ctx)
        if not states:
            return False
        return all(states)

    def _stage_voices(self, ctx):
        ctx["voices"] = []
        ctx["subtitles"] = []
        ctx["voice_mode"] = ctx["production_profile"]["voice"]
        ctx["lip_sync"] = ctx["production_profile"]["lip_sync"]
        lines = sum(len(scene.get("lines", []))
                    for scene in ctx["script"].get("scenes", []))
        audio_states = self._video_audio_states(ctx)
        all_videos_carry_audio = self._videos_carry_audio(ctx)
        if audio_states and any(audio_states) and not all(audio_states):
            raise AifosError(
                "同一集禁止混用有声与无声视频 Provider：会造成重复配音、"
                "口型错位或部分镜头无声")
        # 标准漫剧产线的声音与口型在即梦视频单元内完成，不再生成独立
        # 对白字幕或二次 TTS，避免音色、嘴型与镜头时长三者漂移。
        if all_videos_carry_audio:
            ctx["voice_carried"] = True
            self._task_providers.add("随视频配音(seedance2)")
            self.log.info(
                "director", "Seedance2 有声视频内置配音与口型，"
                "跳过独立 TTS 和对白字幕轨")
            return {"mode": "jimeng_builtin", "count": 0,
                    "reused": 0, "lines": lines,
                    "lip_sync": bool(ctx["lip_sync"]), "subtitles": 0,
                    "integrated_in_video": True,
                    "carried_by_video": True,
                    "provider_audio_confirmed": True}
        if (ctx["production_profile"].get("pipeline_version")
                == PIPELINE_VERSION
                and ctx["voice_mode"] == "jimeng_builtin"):
            raise AifosError(
                "SK V3.2 专业标准要求所有视频随 Seedance2 内置"
                "配音与对口型；当前视频 Provider 未声明 "
                "audio_in_video，已阻止错位的独立 TTS")
        line_no = 0
        reused = 0
        for scene in ctx["script"]["scenes"]:
            for line in scene["lines"]:
                line_no += 1
                name = self._line_name(ctx, line_no)
                existing = self._existing_asset_uri(ctx, "voice", name)
                if existing:
                    row = self.assets.latest(
                        ctx["project"]["id"], "voice", name)
                    meta = json.loads(row["meta"]) if row else {}
                    ctx["voices"].append({
                        "line_no": line_no, "uri": existing,
                        "duration": meta.get("duration") or round(
                            max(1.0, len(line["dialogue"]) * 0.18), 2)})
                    reused += 1
                else:
                    ctx["voices"].append(
                        self._make_voice(ctx, line_no, line))
                if ctx["production_profile"]["burn_subtitles"]:
                    ctx["subtitles"].append({
                        "line_no": line_no,
                        "character": line["character"],
                        "text": line["dialogue"],
                    })
        return {"mode": ctx["voice_mode"],
                "count": len(ctx["voices"]), "reused": reused,
                "lines": lines, "lip_sync": bool(ctx["lip_sync"]),
                "subtitles": len(ctx["subtitles"]),
                "integrated_in_video": False}

    def _make_voice(self, ctx, line_no, line):
        result = self._call(ctx, "voice", {
            "line_no": line_no,
            "character": line["character"],
            "text": line["dialogue"],
        }, "voices")
        self.assets.register(
            ctx["project"]["id"], "voice",
            f"e{ctx['episode']['number']:03d}_line{line_no:03d}",
            uri=result.uri,
            meta={"duration": result.data.get("duration", 0)})
        return {"line_no": line_no, "uri": result.uri,
                "duration": result.data.get("duration", 0)}

    def _stage_edit(self, ctx):
        result = self._call(ctx, "edit", {
            "shots": ctx["videos"],
            "voices": ctx["voices"],
            "subtitles": [] if not ctx["production_profile"]["burn_subtitles"]
            else ctx["subtitles"],
            "voice_mode": ctx.get("voice_mode", ""),
            "lip_sync": ctx.get("lip_sync", False),
            "forbid_subtitles": not ctx["production_profile"]["burn_subtitles"],
            "project_title": ctx["project"]["title"],
            "episode_number": ctx["episode"]["number"],
            "aspect": ctx["aspect"], **ctx["dims"],
        }, "edit")
        ctx["final_uri"] = result.uri
        ctx["edit_data"] = result.data
        self.assets.register(
            ctx["project"]["id"], "edit",
            f"e{ctx['episode']['number']:03d}_final", uri=result.uri,
            meta=result.data)
        return result.data

    def _video_qc_path(self, ctx):
        return ctx["out_root"] / "video_qc_report.json"

    def _load_video_qc_report(self, ctx):
        """读取跨次运行保留的视频质检状态。

        质检阶段可能因人工暂停或服务重启而重新进入；返工次数必须落盘，
        不能只放在本次 produce() 的内存上下文里，否则恢复生产会再次自动
        消耗一次视频额度。
        """
        # force=True 代表新剧本/新一轮全量生产；上一版视频的返工计数不应
        # 阻塞这次全新版本，但同一版本的断点续产(force=False)必须保留。
        if ctx.get("force"):
            return {}
        path = self._video_qc_path(ctx)
        if path.exists():
            try:
                data = json.loads(path.read_text(encoding="utf-8"))
                if isinstance(data, dict):
                    return data
            except (OSError, ValueError):
                pass
        document, _ = self.projects.latest_document(
            ctx["episode"]["id"], "video_qc_report")
        return document if isinstance(document, dict) else {}

    def _save_video_qc_report(self, ctx, report):
        report = dict(report or {})
        report.setdefault("schema", VIDEO_QC_SCHEMA)
        report["updated_at"] = now()
        ctx["video_qc_report"] = report
        path = self._video_qc_path(ctx)
        path.write_text(json.dumps(report, ensure_ascii=False, indent=2),
                        encoding="utf-8")
        self.projects.save_document(
            ctx["episode"]["id"], "video_qc_report", report)
        return report

    def _video_input_for_qc(self, ctx, shot_no):
        video = next((
            item for item in (ctx.get("videos") or [])
            if int(item.get("shot_no") or -1) == int(shot_no)), {})
        snapshot = copy.deepcopy(video.get("input_snapshot") or {})
        history = copy.deepcopy(video.get("attempt_history") or [])
        signature = str(video.get("input_signature") or "")
        if not snapshot or not history or not signature:
            meta = self._latest_video_input_meta(ctx, shot_no)
            snapshot = snapshot or copy.deepcopy(
                meta.get("input_snapshot") or {})
            history = history or copy.deepcopy(
                meta.get("attempt_history") or [])
            signature = signature or str(meta.get("input_signature") or "")
        if not snapshot:
            shot = next((
                item for item in (ctx.get("storyboard") or {}).get(
                    "shots", [])
                if int(item.get("shot_no") or -1) == int(shot_no)), {})
            frame = next((
                item for item in (ctx.get("frames") or [])
                if int(item.get("shot_no") or -1) == int(shot_no)), {})
            keyframe = next((
                item for item in (ctx.get("images") or [])
                if int(item.get("shot_no") or -1) == int(shot_no)), {})
            legacy_payload = {
                "shot_no": shot_no,
                "prompt_compact": (
                    shot.get("seedance_prompt_compact")
                    or shot.get("seedance_prompt")
                    or shot.get("prompt")
                    or ""),
                "prompt_contract": shot.get("prompt_contract") or {},
                "keyframe": keyframe.get("uri", ""),
                "first": frame.get("first", ""),
                "last": frame.get("last", ""),
                "reference_images": [
                    item.get("uri") for item in (
                        video.get("reference_manifest") or [])
                    if isinstance(item, dict) and item.get("uri")],
                "reference_manifest": copy.deepcopy(
                    video.get("reference_manifest") or []),
                "duration": shot.get("duration"),
                "video_quality": video.get("video_quality", "medium"),
                "video_resolution": video.get(
                    "video_resolution", "720p"),
                "standard_fingerprint": (
                    (ctx.get("production_profile") or {}).get(
                        "standard_fingerprint", "")),
            }
            snapshot = self._video_input_snapshot(legacy_payload)
            signature = snapshot["input_signature"]
        return snapshot, signature, history

    def _run_video_input_diagnosis(self, ctx, shot, snapshot, messages):
        """Inspect actual prompt, frames and reference manifest after failure.

        Video-capable visual QC is not yet a guaranteed provider capability.
        We therefore inspect both source frames with the existing multimodal
        image-QC route and fail closed when that route is unavailable.  A
        visual-video diagnosis supplied by a capable provider can override
        this through ``ctx.video_input_diagnoses``.
        """
        prompt = str(snapshot.get("prompt_sent") or "")
        contract = snapshot.get("prompt_contract") or {}
        technical_output_missing = bool(messages) and all(
            any(token in str(message) for token in (
                "视频缺失", "视频文件缺失", "产物缺失"))
            for message in messages)
        prompt_problems = []
        if not prompt:
            prompt_problems.append("缺少实际发送给 Seedance 的提示词")
        if len(prompt) > 2200:
            prompt_problems.append(
                f"实际视频提示词过长({len(prompt)}字)，主体动作可能被稀释")
        expected_count = len(shot.get("characters") or [])
        subject = contract.get("subject") or {}
        if (subject and subject.get("count") is not None
                and int(subject.get("count") or 0) != expected_count):
            prompt_problems.append("提示词合同人数与本镜人物名单不一致")

        manifest = [
            item for item in (snapshot.get("reference_manifest") or [])
            if isinstance(item, dict)]
        reference_problems = []
        indices = [item.get("index") for item in manifest]
        if len(indices) != len(set(indices)):
            reference_problems.append("参考图序号重复，图文对应关系不唯一")
        if len(manifest) > 7:
            reference_problems.append("首尾帧之外参考图超过 Seedance 上限7张")
        for item in manifest:
            uri = str(item.get("uri") or "")
            if not uri:
                reference_problems.append(
                    f"参考图{item.get('index')}缺少实际文件")
            elif (not uri.startswith(("http://", "https://"))
                  and not Path(uri).exists()):
                reference_problems.append(
                    f"参考图{item.get('index')}文件不存在:{item.get('name')}")
            if not str(item.get("binding") or "").strip():
                reference_problems.append(
                    f"参考图{item.get('index')}未声明单一职责")
        expected_reference_uris = [
            str(uri) for uri in (snapshot.get("reference_images") or [])]
        actual_reference_uris = [
            str(uri) for uri in (
                snapshot.get("reference_images_used") or [])]
        if actual_reference_uris != expected_reference_uris:
            reference_problems.append(
                "Provider实际使用的参考图顺序/数量与提交清单不一致")
        identity_names = self._video_identity_names(ctx, shot)
        identity_labels = {
            str(item.get("name") or "").split(":", 1)[0]
            for item in manifest
            if item.get("kind") in (
                "character_identity", "character_art",
                "character_candidate")}
        for name in identity_names:
            if name not in identity_labels:
                reference_problems.append(f"缺少{name}的最终立绘参考")

        frame_audit = {
            "source_frames_valid": True,
            "first_valid": True,
            "last_valid": True,
            "keyframe_valid": None,
            "continuity_valid": True,
            "visual_checked": False,
            "issues": [],
        }
        first = str(snapshot.get("first_frame") or "")
        last = str(snapshot.get("last_frame") or "")
        keyframe = str(snapshot.get("keyframe") or "")
        for label, uri, key in (
                ("首帧", first, "first_valid"),
                ("尾帧", last, "last_valid")):
            if not uri or (
                    not uri.startswith(("http://", "https://"))
                    and not Path(uri).exists()):
                frame_audit[key] = False
                frame_audit["issues"].append(f"{label}文件缺失")
        if keyframe:
            frame_audit["keyframe_valid"] = not (
                not keyframe.startswith(("http://", "https://"))
                and not Path(keyframe).exists())
            if not frame_audit["keyframe_valid"]:
                frame_audit["issues"].append("关键帧文件缺失")
        if frame_audit["first_valid"] and frame_audit["last_valid"]:
            try:
                composition = (
                    shot.get("composition_contract")
                    or build_composition_contract(shot))
                spec = self._qc_spec(
                    ctx["project"]["id"],
                    self._video_identity_names(ctx, shot),
                    location=self._shot_location(ctx.get("script"), shot),
                    action=shot.get("description") or shot.get("prompt") or "",
                    forbid=self._FORBID + ["字幕条"],
                    expected_characters=shot.get("characters") or [],
                    expected_count=shot.get(
                        "character_count", expected_count),
                    composition_contract=composition,
                    readable_text=shot.get("readable_text") or {},
                    narrative_overlays=shot.get(
                        "narrative_overlays"))
                samples = [
                    ("首帧", first, "first_valid"),
                    ("尾帧", last, "last_valid"),
                ]
                if keyframe and frame_audit["keyframe_valid"]:
                    samples.append(("关键帧", keyframe, "keyframe_valid"))
                qc_reference_manifest = [
                    {
                        "index": 1, "uri": first,
                        "label": "Seedance实际首帧",
                        "role": "first_frame",
                        "binding": "锁定视频动作起点、人物状态与构图",
                    },
                    {
                        "index": 2, "uri": last,
                        "label": "Seedance实际尾帧",
                        "role": "last_frame",
                        "binding": "锁定视频动作终点、人物状态与构图",
                    },
                    *[
                        {**copy.deepcopy(item), "index": index + 3}
                        for index, item in enumerate(manifest)
                    ],
                ]
                generation_input = {
                    "schema": "aifos.video-generation-input/v1",
                    "scope": {
                        "shot_no": shot.get("shot_no"),
                        "frame_kind": "video_source_frame",
                    },
                    "prompt": prompt,
                    "prompt_contract": copy.deepcopy(contract),
                    "reference_manifest": qc_reference_manifest,
                    "input_signature": snapshot.get(
                        "input_signature", ""),
                }
                for label, uri, key in samples:
                    result = self.router.call(
                        "image_qc", {
                            **spec,
                            "image_uri": uri,
                            "generation_input": generation_input,
                            "generation_prompt": prompt,
                            "reference_manifest": qc_reference_manifest,
                        },
                        ctx["out_root"],
                        cancel=lambda: self._cancel_requested(ctx))
                    if result.cost:
                        self._task_cost = (
                            getattr(self, "_task_cost", 0.0) + result.cost)
                        if not hasattr(self, "_task_providers"):
                            self._task_providers = set()
                        self._task_providers.add(result.provider)
                        self.projects.add_episode_cost(
                            ctx["episode"]["id"], result.cost)
                    verdict = self._assess_image_qc(
                        spec, result.data or {}, 1)
                    frame_audit["visual_checked"] = True
                    if not verdict["passed"]:
                        frame_audit[key] = False
                        frame_audit["issues"].extend(
                            f"{label}:{value}"
                            for value in verdict.get("issues") or [])
            except (AifosError, ProviderUnavailable, ProviderError) as exc:
                frame_audit["diagnostic_error"] = str(exc)
                frame_audit["visual_checked"] = False
        frame_audit["source_frames_valid"] = bool(
            frame_audit["first_valid"] and frame_audit["last_valid"]
            and frame_audit["keyframe_valid"] is not False)
        if not frame_audit["source_frames_valid"]:
            decision = {
                "action": "repair_frames_first",
                "safe_to_auto_retry": False,
                "input_changed": False,
                "reason": "源关键帧/首尾帧未通过视觉诊断",
            }
        elif reference_problems:
            decision = {
                "action": "awaiting_human",
                "safe_to_auto_retry": False,
                "input_changed": False,
                "reason": "参考图输入有误，需先更换或补齐参考图",
            }
        elif prompt_problems:
            decision = {
                "action": "direct_video_retry",
                "safe_to_auto_retry": True,
                "input_changed": True,
                "prompt_patch": {
                    "replace": "只保留本镜人物、单一动作、一次运镜和终点状态",
                    "remove": prompt_problems,
                },
                "reason": "提示词合同可定向收敛后重拍",
            }
        elif technical_output_missing:
            decision = {
                "action": "direct_video_retry",
                "safe_to_auto_retry": True,
                "input_changed": True,
                "prompt_patch": (
                    "技术恢复：严格沿用已核验的本镜首尾帧、人物与参考图，"
                    "重新生成并完整落盘视频文件"),
                "reason": "视频产物缺失，源输入已核验，可安全恢复生成",
            }
        else:
            # Source inputs passed, but no provider actually inspected the
            # produced video's temporal content.  Do not pretend that a text
            # diagnosis proved the cause and do not spend another video call.
            decision = {
                "action": "awaiting_human",
                "safe_to_auto_retry": False,
                "input_changed": False,
                "reason": (
                    "提示词、参考图与源帧未发现确定错误；"
                    "当前无视频时序视觉诊断证据，禁止盲目重拍"),
            }
        return {
            "schema": "aifos.generation-input-diagnosis/v1",
            "mode": "video",
            "issues": list(dict.fromkeys(
                [*messages, *prompt_problems, *reference_problems,
                 *frame_audit["issues"]])),
            "prompt_diagnosis": {
                "valid": not prompt_problems,
                "problems": prompt_problems,
                "prompt_sent_hash": snapshot.get("prompt_sent_hash", ""),
            },
            "reference_diagnosis": {
                "valid": not reference_problems,
                "problems": reference_problems,
                "items": copy.deepcopy(manifest),
            },
            "frame_audit": frame_audit,
            "decision": decision,
        }

    @staticmethod
    def _normalize_video_input_diagnosis(raw, related, revision):
        """Fail-safe retry routing from structured visual/input diagnosis."""
        raw = copy.deepcopy(raw) if isinstance(raw, dict) else {}
        prompt_audit = raw.get("prompt_diagnosis") or raw.get(
            "prompt_audit") or {}
        reference_audit = raw.get("reference_diagnosis") or raw.get(
            "reference_audit") or {}
        frame_audit = raw.get("frame_audit") or {}
        decision = copy.deepcopy(raw.get("decision") or {})
        generic_patch = raw.get("targeted_prompt_patch")
        if isinstance(generic_patch, dict):
            generic_patch = generic_patch.get("instructions") or generic_patch
        generic_reference_ops = raw.get("reference_adjustments") or []
        diagnosis_issues = [
            str(value) for value in (raw.get("issues") or [])
            if str(value).strip()]
        image_error = raw.get("image_error") or {}
        if isinstance(image_error, dict):
            diagnosis_issues.extend(
                str(value) for value in (
                    image_error.get("evidence") or [])
                if str(value).strip())
            if image_error.get("summary"):
                diagnosis_issues.append(str(image_error["summary"]))
        if not diagnosis_issues:
            for audit in (prompt_audit, reference_audit, frame_audit):
                if isinstance(audit, dict):
                    diagnosis_issues.extend(
                        str(value) for value in (audit.get("problems")
                                                 or audit.get("issues")
                                                 or [])
                        if str(value).strip())
        frame_failed = False
        if isinstance(frame_audit, dict):
            frame_failed = any(value is False for value in (
                frame_audit.get("source_frames_valid"),
                frame_audit.get("first_valid"),
                frame_audit.get("last_valid"),
                frame_audit.get("keyframe_valid"),
                frame_audit.get("continuity_valid"),
            ))
        if frame_failed:
            decision.update({
                "action": "repair_frames_first",
                "safe_to_auto_retry": False,
                "input_changed": False,
                "reason": (decision.get("reason")
                           or "源关键帧/首尾帧诊断未通过"),
            })
        if not decision:
            if generic_patch and raw.get("diagnosis_complete"):
                decision = {
                    "action": "direct_video_retry",
                    "safe_to_auto_retry": True,
                    "input_changed": True,
                    "prompt_patch": generic_patch,
                    "reason": "视觉诊断已生成本镜定向提示词修订",
                }
            elif generic_reference_ops:
                decision = {
                    "action": "awaiting_human",
                    "safe_to_auto_retry": False,
                    "input_changed": False,
                    "reference_ops": copy.deepcopy(generic_reference_ops),
                    "reason": "参考图修订建议尚未解析并实际应用",
                }
            # Backward compatibility: old QC only knew a shot-local
            # rerunnable video/media failure.  Compile a targeted prompt patch
            # so the retry input changes; global/config issues never enter here.
            rerunnable = any(bool(item.get("rerunnable"))
                             for item in related)
            if (not decision and related and rerunnable
                    and revision.get("instructions")):
                decision = {
                    "action": "direct_video_retry",
                    "safe_to_auto_retry": True,
                    "input_changed": True,
                    "prompt_patch": list(revision["instructions"]),
                    "reason": "镜头级视频输出未通过，使用定向修订重拍",
                }
            elif not decision and (related or diagnosis_issues):
                decision = {
                    "action": "awaiting_human",
                    "safe_to_auto_retry": False,
                    "input_changed": False,
                    "reason": "缺少可验证的提示词或参考图修订方案",
                }
            elif not decision:
                decision = {
                    "action": "none",
                    "safe_to_auto_retry": False,
                    "input_changed": False,
                }
        action = str(decision.get("action") or "awaiting_human")
        if action not in (
                "none", "direct_video_retry", "repair_frames_first",
                "awaiting_human"):
            action = "awaiting_human"
            decision["reason"] = (
                decision.get("reason") or "未知修复动作，已失败安全暂停")
        decision["action"] = action
        patch = decision.get("prompt_patch")
        ref_ops = decision.get("reference_ops") or []
        declared_change = bool(
            patch or decision.get("revised_prompt")
            or (ref_ops and decision.get("reference_ops_applied")))
        if action == "direct_video_retry" and not declared_change:
            decision.update({
                "action": "awaiting_human",
                "safe_to_auto_retry": False,
                "input_changed": False,
                "reason": (
                    "提示词未修订，或参考图修订尚未实际应用，"
                    "禁止原样重试"),
            })
        elif action == "direct_video_retry":
            decision["input_changed"] = True
            decision["safe_to_auto_retry"] = bool(
                decision.get("safe_to_auto_retry", True))
        else:
            decision["safe_to_auto_retry"] = False
        return {
            "schema": "aifos.generation-input-diagnosis/v1",
            "mode": "video",
            "issues": list(dict.fromkeys(diagnosis_issues)),
            "prompt_diagnosis": copy.deepcopy(prompt_audit),
            "reference_diagnosis": copy.deepcopy(reference_audit),
            "frame_audit": copy.deepcopy(frame_audit),
            "decision": decision,
        }

    def _build_video_qc_report(self, ctx, qc_report, previous=None):
        """把总质检中与视频直接相关的问题收敛为逐镜状态。

        这层是视频生成后的明确质检门：既包含视频文件/媒体健全性，也包含
        即梦有声视频与口型证据。内容复核、分镜合同等全局问题仍由总质检
        报告负责，不会被错误地当作视频返工理由。
        """
        previous = previous or {}
        previous_shots = {
            str(item.get("shot_no")): item
            for item in previous.get("shots", [])
            if isinstance(item, dict) and item.get("shot_no") is not None
        }
        direct_checks = {"video", "media_sanity", "integrated_audio"}
        all_issues = qc_report.get("issues") or []
        global_issues = [
            str(issue.get("message") or issue.get("check"))
            for issue in all_issues
            if issue.get("check") in direct_checks
            and issue.get("shot_no") is None
        ]
        review_units = {
            str(item.get("unit_id")): item
            for item in (ctx.get("content_review") or {}).get("units", [])
            if item.get("unit_id")
        }
        shots = []
        for shot in ctx.get("storyboard", {}).get("shots", []):
            shot_no = int(shot.get("shot_no"))
            related = [issue for issue in all_issues
                       if issue.get("check") in direct_checks
                       and issue.get("shot_no") == shot_no]
            messages = list(dict.fromkeys(
                str(issue.get("message") or issue.get("check"))
                for issue in related))
            review = review_units.get(str(shot.get("unit_id")))
            if review and review.get("verdict") == "FAIL":
                messages.append(str(review.get("drift_issue")
                                   or "逐段内容复核未通过"))
            old = previous_shots.get(str(shot_no), {})
            input_snapshot, input_signature, attempt_history = (
                self._video_input_for_qc(ctx, shot_no))
            raw_diagnosis = self._video_diagnosis_from_ctx(ctx, shot_no)
            if (raw_diagnosis.get("input_signature")
                    and raw_diagnosis.get("input_signature")
                    != input_signature):
                raw_diagnosis = {}
            for issue in related:
                embedded = issue.get("input_diagnosis")
                if isinstance(embedded, dict):
                    raw_diagnosis = {
                        **raw_diagnosis, **copy.deepcopy(embedded)}
            if (not raw_diagnosis and messages
                    and old.get("input_signature") == input_signature
                    and not old.get("passed")
                    and list(old.get("issues") or []) == messages
                    and isinstance(old.get("input_diagnosis"), dict)):
                raw_diagnosis = copy.deepcopy(old["input_diagnosis"])
            if not raw_diagnosis and messages:
                raw_diagnosis = self._run_video_input_diagnosis(
                    ctx, shot, input_snapshot, messages)
            messages.extend(
                str(value) for value in (raw_diagnosis.get("issues") or [])
                if str(value).strip())
            messages = list(dict.fromkeys(messages))
            revision = optimize_qc_feedback(
                messages, mode="video") if messages else {
                    "text": "", "categories": [], "issues": [],
                    "instructions": [], "mode": "video",
                }
            input_diagnosis = self._normalize_video_input_diagnosis(
                raw_diagnosis,
                related or ([{"rerunnable": False}] if messages else []),
                revision)
            if input_diagnosis.get("issues"):
                messages = list(dict.fromkeys(
                    messages + input_diagnosis["issues"]))
                revision = optimize_qc_feedback(messages, mode="video")
                input_diagnosis = self._normalize_video_input_diagnosis(
                    raw_diagnosis,
                    related or [{"rerunnable": False}],
                    revision)
            input_diagnosis["input_signature"] = input_signature
            decision = input_diagnosis["decision"]
            auto_retries_used = int(old.get("auto_retries_used") or 0)
            generation_attempts = int(old.get("generation_attempts") or 1)
            passed = not messages
            retry_available = (
                not passed
                and decision.get("action") == "direct_video_retry"
                and decision.get("safe_to_auto_retry")
                and decision.get("input_changed")
                and auto_retries_used < VIDEO_QC_AUTO_RETRIES)
            if passed:
                status = "passed"
            elif decision.get("action") == "repair_frames_first":
                status = "repair_frames_first"
            elif retry_available:
                status = "failed"
            else:
                status = "awaiting_human"
            shots.append({
                "shot_no": shot_no,
                "passed": passed,
                "status": status,
                "issues": messages,
                "generation_attempts": generation_attempts,
                "auto_retries_used": auto_retries_used,
                "auto_retry_limit": VIDEO_QC_AUTO_RETRIES,
                "revision_feedback": revision["text"],
                "revision_categories": revision["categories"],
                "input_diagnosis": input_diagnosis,
                "decision": decision,
                "input_snapshot": input_snapshot,
                "input_signature": input_signature,
                "attempt_history": attempt_history,
                "awaiting_human": bool(messages)
                and status in ("awaiting_human", "repair_frames_first"),
            })
        failed = [item for item in shots if not item["passed"]]
        waiting = [item for item in failed if item["awaiting_human"]]
        frame_blocked = [
            item for item in failed
            if item["status"] == "repair_frames_first"]
        return {
            "schema": VIDEO_QC_SCHEMA,
            "passed": not failed and not global_issues,
            "shots": shots,
            "failed_shots": [item["shot_no"] for item in failed],
            "awaiting_human": bool(waiting or global_issues),
            "awaiting_human_shots": [item["shot_no"] for item in waiting],
            "repair_frames_first_shots": [
                item["shot_no"] for item in frame_blocked],
            "global_issues": list(dict.fromkeys(global_issues)),
            "global_status": (
                "awaiting_human" if global_issues else "passed"),
            "auto_retry_limit": VIDEO_QC_AUTO_RETRIES,
            "last_total_qc_passed": bool(qc_report.get("passed")),
        }

    @staticmethod
    def _video_retry_candidates(video_qc_report):
        return [item["shot_no"] for item in video_qc_report.get("shots", [])
                if not item.get("passed")
                and (item.get("decision") or {}).get(
                    "action") == "direct_video_retry"
                and bool((item.get("decision") or {}).get(
                    "safe_to_auto_retry"))
                and bool((item.get("decision") or {}).get("input_changed"))
                and int(item.get("auto_retries_used") or 0)
                < VIDEO_QC_AUTO_RETRIES]

    def _stage_qc(self, ctx):
        """自动检查 + 图文检查板 + 逐段内容复核 + 交付脚本。"""
        content_review = build_content_review(
            ctx["script"], ctx["storyboard"], ctx["continuity"])
        content_path = ctx["out_root"] / "content_review.json"
        content_path.write_text(
            json.dumps(content_review, ensure_ascii=False, indent=2),
            encoding="utf-8")
        self.projects.save_document(
            ctx["episode"]["id"], "content_review", content_review)
        review_board = write_review_board(ctx, content_review)
        ctx["content_review"] = content_review
        ctx["review_board"] = review_board
        ep_name = f"e{ctx['episode']['number']:03d}"
        self.assets.register(
            ctx["project"]["id"], "review_board", ep_name,
            uri=review_board, meta={"passed": content_review["passed"]})
        max_retries = self.config.get("retry", "max_retries", default=2)
        try:
            max_retries = max(0, int(max_retries))
        except (TypeError, ValueError):
            max_retries = 2
        # 普通配音/缺失产物仍沿用通用重试配置；视频返工单独固定为一次。
        max_rounds = max(max_retries, VIDEO_QC_AUTO_RETRIES) + 1
        previous_video_qc = self._load_video_qc_report(ctx)
        report = None
        video_qc = previous_video_qc
        for attempt in range(max_rounds):
            report = self.qc.run(ctx["script"], ctx["storyboard"], ctx)
            video_qc = self._build_video_qc_report(
                ctx, report, previous=video_qc)
            self._save_video_qc_report(ctx, video_qc)
            self.log.info(
                "qc", f"质检第{attempt + 1}轮:得分 {report['score']}"
                f"(线 {report['pass_score']}),问题 {len(report['issues'])}")
            # 只要存在可自动重跑的缺失产物(即使总分达标)就先修复再定论。
            # 视频只能进入一次自动返工；第二次失败会在报告中标记待人工，
            # 绝不因为通用 retry.max_retries=2 而偷偷再拍第三版。
            video_shots = self._video_retry_candidates(video_qc)
            line_retry = list(report.get("rerun_lines") or [])
            if not video_shots and not line_retry:
                break
            if attempt >= max_rounds - 1:
                break
            if video_shots:
                by_shot = {
                    int(item["shot_no"]): item
                    for item in video_qc.get("shots", [])}
                ctx["video_feedback"] = {
                    int(shot_no): self._video_repair_feedback(
                        by_shot.get(int(shot_no), {}))
                    for shot_no in video_shots
                }
                ctx["video_input_diagnoses"] = {
                    int(shot_no): copy.deepcopy(
                        (by_shot.get(int(shot_no), {})
                         .get("input_diagnosis") or {}))
                    for shot_no in video_shots
                }
            self._rerun(ctx, report, video_shots=video_shots)
            if video_shots:
                by_shot = {
                    int(item["shot_no"]): item
                    for item in video_qc.get("shots", [])}
                for shot_no in video_shots:
                    item = by_shot.get(int(shot_no))
                    if item is not None:
                        item["auto_retries_used"] = min(
                            VIDEO_QC_AUTO_RETRIES,
                            int(item.get("auto_retries_used") or 0) + 1)
                        item["generation_attempts"] = int(
                            item.get("generation_attempts") or 1) + 1
                video_qc["auto_retry_count"] = sum(
                    int(item.get("auto_retries_used") or 0)
                    for item in video_qc.get("shots", []))
                self._save_video_qc_report(ctx, video_qc)
        delivery = write_delivery_verifier(
            ctx, review_board, content_review)
        if not delivery.get("passed"):
            report["issues"].append({
                "check": "delivery", "severity": "error",
                "shot_no": None, "line_no": None, "rerunnable": False,
                "message": "交付复核脚本未通过",
            })
            report["score"] = max(0, report["score"] - 15)
            report["passed"] = False
        report["content_review"] = content_review
        report["review_board"] = review_board
        report["delivery_check"] = delivery
        report["technical_passed"] = not any(
            i["severity"] == "error" and i["check"] not in ("content",)
            for i in report["issues"])
        report["content_passed"] = content_review["passed"]
        ctx["qc_report"] = report
        # 交付复核完成后再写一次，让前端拿到最终的总质检状态与“待人工”
        # 镜头，而不是停留在返工前的中间快照。
        video_qc["last_total_qc_passed"] = bool(report["passed"])
        video_qc["passed"] = (
            not bool(video_qc.get("failed_shots"))
            and not bool(video_qc.get("global_issues")))
        video_qc["awaiting_human"] = bool(
            video_qc.get("awaiting_human_shots")
            or video_qc.get("global_issues"))
        self._save_video_qc_report(ctx, video_qc)
        self.projects.set_qc_score(ctx["episode"]["id"], report["score"])
        report_path = ctx["out_root"] / "qc_report.json"
        report_path.write_text(
            json.dumps(report, ensure_ascii=False, indent=2),
            encoding="utf-8")
        # 新镜头已经进入本轮成片与质检，解除“镜头修订后待重拍”标记。
        self.projects.save_document(
            ctx["episode"]["id"], "shot_revision_state", {
                "schema": "aifos.shot-revision-state/v1",
                "active": False,
                "passed": bool(report["passed"]),
                "resolved_at": now(),
            })
        if not report["passed"]:
            self.log.warn(
                "qc", f"质检未通过(得分 {report['score']}),"
                "不可自动修复的问题已写入报告")
        return {"score": report["score"], "passed": report["passed"],
                "issues": len(report["issues"]),
                "content_passed": content_review["passed"],
                "delivery_passed": delivery.get("passed", False),
                "video_qc_passed": bool(video_qc.get("passed")),
                "video_qc_awaiting_human": bool(
                    video_qc.get("awaiting_human")),
                "video_qc_auto_retry_limit": VIDEO_QC_AUTO_RETRIES}

    def _rerun(self, ctx, report, video_shots=None):
        shots = {s["shot_no"]: s for s in ctx["storyboard"]["shots"]}
        frames = {f["shot_no"]: f for f in ctx["frames"]}
        # 调用方显式传入视频镜头集合时，只重拍该集合；这使“视频一次
        # 自动返工”的限制不会被其它重试来源绕过。
        shot_numbers = (list(video_shots)
                        if video_shots is not None
                        else list(report["rerun_shots"]))
        for shot_no in shot_numbers:
            self.log.info("director", f"自动重跑镜头 {shot_no} 的视频")
            new_video = self._make_video(ctx, shots[shot_no], frames)
            ctx["videos"] = [
                new_video if v["shot_no"] == shot_no else v
                for v in ctx["videos"]]
            self.data.record(
                "case", "failure", prompt=shots[shot_no]["prompt"],
                meta={
                    "reason": "qc_rerun", "shot_no": shot_no,
                    "revision_feedback": (ctx.get("video_feedback") or {})
                    .get(int(shot_no), ""),
                },
                episode_id=ctx["episode"]["id"])
        lines = {}
        line_no = 0
        for scene in ctx["script"]["scenes"]:
            for line in scene["lines"]:
                line_no += 1
                lines[line_no] = line
        for ln in report["rerun_lines"]:
            self.log.info("director", f"自动重跑台词 {ln} 的配音")
            new_voice = self._make_voice(ctx, ln, lines[ln])
            ctx["voices"] = [
                new_voice if v["line_no"] == ln else v
                for v in ctx["voices"]]
        self._stage_edit(ctx)

    def _stage_package(self, ctx):
        if not ctx.get("qc_report", {}).get("passed", False):
            self.log.warn("ops", "质检未通过,跳过封面/标题/拆条")
            return {"skipped": True}
        ep_name = f"e{ctx['episode']['number']:03d}"
        existing_cover = self._existing_asset_uri(ctx, "cover", ep_name)
        if existing_cover:
            ctx["cover_uri"] = existing_cover
        else:
            cover_characters = [c.get("name") for c in
                                ctx["script"].get("characters", [])
                                if c.get("name") and not is_background_character(c)]
            identity_refs = self._identity_references(
                ctx["project"]["id"], cover_characters,
                required=bool(cover_characters))
            cover = self.ops.make_cover(
                ctx["script"], ctx["out_root"] / "ops", aspect=ctx["aspect"],
                identity_references=identity_refs)
            self._task_cost += cover.cost
            self._task_providers.add(cover.provider)
            self.projects.add_episode_cost(ctx["episode"]["id"], cover.cost)
            ctx["cover_uri"] = cover.uri
        ctx["titles"] = self.ops.make_titles(
            ctx["script"], kind=ctx["project"]["kind"])
        ctx["clips"] = self.ops.make_clips(
            ctx["storyboard"], ctx["out_root"] / "ops")
        project_id = ctx["project"]["id"]
        self.assets.register(
            project_id, "cover", ep_name, uri=ctx["cover_uri"])
        self.assets.register(
            project_id, "title", ep_name, meta={"candidates": ctx["titles"]})
        for clip in ctx["clips"]:
            self.assets.register(
                project_id, "clip", f"{ep_name}_scene{clip['scene_no']:02d}",
                uri=clip["uri"])
        # 发布包:成片/封面/标题/话题标签一站式,供人工一键上传
        kit = self.ops.make_publish_kit(
            ctx["project"], ctx["episode"], ctx,
            ctx["out_root"] / "publish")
        ctx["publish_kit"] = kit
        return {"titles": len(ctx["titles"]), "clips": len(ctx["clips"]),
                "cover_reused": bool(existing_cover),
                "hashtags": len(kit["hashtags"])}

    def _stage_archive(self, ctx):
        """数据沉淀:Prompt、图片、视频、配音、成/败案例入库。"""
        episode_id = ctx["episode"]["id"]
        passed = ctx.get("qc_report", {}).get("passed", False)
        label = "success" if passed else "failure"
        for shot in ctx["storyboard"]["shots"]:
            self.data.record(
                "prompt", label, prompt=shot["prompt"],
                meta={"shot_no": shot["shot_no"]}, episode_id=episode_id)
        for image in ctx.get("images", []):
            self.data.record(
                "image", label, uri=image["uri"],
                meta={"shot_no": image["shot_no"]}, episode_id=episode_id)
        for video in ctx.get("videos", []):
            self.data.record(
                "video", label, uri=video["uri"],
                meta={"shot_no": video["shot_no"]}, episode_id=episode_id)
        for voice in ctx.get("voices", []):
            self.data.record(
                "voice", label, uri=voice["uri"],
                meta={"line_no": voice["line_no"]}, episode_id=episode_id)
        if not ctx.get("voices") and ctx.get("voice_mode") == "jimeng_builtin":
            self.data.record(
                "voice", label, uri=ctx.get("final_uri", ""),
                meta={"mode": "jimeng_builtin", "lip_sync": True,
                      "integrated_in_video": True}, episode_id=episode_id)
        self.data.record(
            "review", label, uri=ctx.get("review_board", ""),
            meta={"content_passed": ctx.get("content_review", {}).get("passed"),
                  "delivery_passed": ctx.get("qc_report", {}).get(
                      "delivery_check", {}).get("passed")},
            episode_id=episode_id)
        self.data.record(
            "case", label,
            prompt=f"《{ctx['project']['title']}》第{ctx['episode']['number']}集",
            uri=ctx.get("final_uri", ""),
            meta={
                "qc_score": ctx.get("qc_report", {}).get("score"),
                "cost": self.projects.get_episode(episode_id)["cost"],
                "standard_version": ctx.get("production_profile", {}).get(
                    "standard_version"),
                "standard_fingerprint": ctx.get(
                    "production_profile", {}).get("standard_fingerprint", ""),
            },
            episode_id=episode_id)
        return {"label": label}

    def _register_shot_asset(self, ctx, kind, shot_no, uri, meta=None):
        self.assets.register(
            ctx["project"]["id"], kind,
            f"e{ctx['episode']['number']:03d}_shot{shot_no:03d}", uri=uri,
            meta=meta)

    # ---- 剧本 AI 分析 / 制作圣经 ----
    def save_story_analysis(self, episode_id, analysis, *,
                            expected_version=None, locked=False):
        episode = self.projects.get_episode(int(episode_id))
        if episode is None:
            raise AifosError("剧集不存在")
        running = self.db.query_one(
            "SELECT id FROM production_runs WHERE episode_id=? "
            "AND status IN ('running', 'cancelling') LIMIT 1",
            (episode["id"],))
        if running is not None:
            raise AifosError("本集正在生产，请先停止并等待任务稳定后再修改制作圣经")
        script, script_version = self.projects.latest_document(
            episode["id"], "script")
        if script is None:
            raise AifosError("本集尚无剧本，先上传剧本或让 AI 写剧本")
        current, current_version = self.projects.latest_document(
            episode["id"], "story_analysis")
        if (expected_version is not None
                and int(expected_version) != int(current_version)):
            raise AifosError("制作圣经已在其他页面更新，请刷新后再保存")
        project = self.db.query_one(
            "SELECT * FROM projects WHERE id=?", (episode["project_id"],))
        normalized = build_story_analysis(
            script, project["style"], raw=analysis,
            source=("manual_adjusted" if current else "manual"))
        normalized["script_version"] = script_version
        normalized["project_style"] = project["style"]
        normalized["locked"] = bool(locked)
        normalized["updated_at"] = now()
        error = validate_story_analysis(normalized)
        if error:
            raise AifosError(f"制作圣经保存失败: {error}")
        if locked:
            error = character_production_readiness_error(script, normalized)
            if error:
                raise AifosError(error)
        version = self.projects.save_document(
            episode["id"], "story_analysis", normalized)
        self.projects.set_episode_status(episode["id"], "awaiting_script")
        self.log.info(
            "director", f"制作圣经已保存 v{version}"
            f"（{'已锁定' if locked else '待确认'}）")
        return {"analysis": normalized, "version": version}

    def lock_story_analysis(self, episode_id):
        analysis, version = self.projects.latest_document(
            int(episode_id), "story_analysis")
        if analysis is None:
            raise AifosError("AI 制作圣经尚未生成，请先完成剧本分析")
        script, _ = self.projects.latest_document(int(episode_id), "script")
        error = character_production_readiness_error(script or {}, analysis)
        if error:
            raise AifosError(error)
        if analysis.get("locked"):
            return {"analysis": analysis, "version": version}
        analysis = copy.deepcopy(analysis)
        analysis["locked"] = True
        analysis["locked_at"] = now()
        new_version = self.projects.save_document(
            int(episode_id), "story_analysis", analysis)
        return {"analysis": analysis, "version": new_version}

    def reanalyze_story(self, project_title, episode_number,
                        creative_direction="", run_id=None):
        project = self.projects.get_project(project_title)
        if project is None:
            raise AifosError(f"项目不存在: {project_title}")
        episode = self.db.query_one(
            "SELECT * FROM episodes WHERE project_id=? AND number=?",
            (project["id"], episode_number))
        if episode is None:
            raise AifosError(f"剧集不存在: 第{episode_number}集")
        script, script_version = self.projects.latest_document(
            episode["id"], "script")
        if script is None:
            raise AifosError("本集尚无剧本，无法分析")
        standard = self._resolve_standard_snapshot(
            episode["id"], force=False)
        ctx = {
            "project": dict(project), "episode": dict(episode),
            "script": script, "script_version": script_version,
            "out_root": self._episode_dir(project, episode),
            "production_standard": standard,
            "production_profile": production_profile(self.config, standard),
            "run_id": run_id,
        }
        self._task_cost = 0.0
        self._task_providers = set()
        analysis, version, _ = self._ensure_story_analysis(
            ctx, force=True, creative_direction=creative_direction)
        self.projects.set_episode_status(episode["id"], "awaiting_script")
        self.log.info(
            "director", f"剧本已重新分析，制作圣经 v{version} 等待确认")
        return {
            "project": project_title, "episode": episode_number,
            "episode_id": episode["id"], "status": "awaiting_script",
            "story_analysis_version": version,
            "world": analysis["world"]["name"],
            "cost": round(self._task_cost, 2),
        }

    # ---- 打磨:剧本意见重写 / 单张图片附意见重画 ----
    def revise_script(self, project_title, episode_number, feedback,
                      run_id=None):
        """按修改意见重写剧本并重跑预生产,回到待确认。"""
        project = self.projects.get_project(project_title)
        if project is not None:
            episode = self.db.query_one(
                "SELECT * FROM episodes WHERE project_id=? AND number=?",
                (project["id"], episode_number))
            if episode is not None:
                self.data.record(
                    "case", "failure", prompt=feedback,
                    meta={"reason": "script_revision"},
                    episode_id=episode["id"])
        return self.produce(
            project_title, episode_number, force=True,
            pause_for_confirm=True, feedback=feedback, run_id=run_id)

    def _sync_revised_video_references(self, episode_id, project_id,
                                       old_asset_id, new_row, usable=True):
        """把手选 Seedance 参考图中的旧关键帧原子迁移到新版本。

        人工选择文档保存的是 asset_id；只新增图片版本而不改文档会让
        `_video_reference_rows` 因“不是最新版”静默丢掉这张参考图。
        """
        if not old_asset_id:
            return []
        if new_row is not None and int(new_row["project_id"]) != int(project_id):
            raise AifosError("新关键帧不属于当前项目，拒绝同步 Seedance 参考")
        document, _ = self.projects.latest_document(
            episode_id, "video_references")
        if not document:
            return []
        changed = []
        shots = document.setdefault("shots", {})
        replacement = ({
            "asset_id": new_row["id"], "kind": new_row["kind"],
            "name": new_row["name"], "version": new_row["version"],
            "usable_for_video": bool(usable),
        } if new_row is not None else None)
        for key, selected in list(shots.items()):
            revised = []
            touched = False
            for item in selected or []:
                try:
                    matches = int(item.get("asset_id")) == int(old_asset_id)
                except (TypeError, ValueError):
                    matches = False
                if matches:
                    touched = True
                    if replacement is not None:
                        revised.append(dict(replacement))
                else:
                    revised.append(item)
            if touched:
                shots[key] = revised
                changed.append(int(key))
        if changed:
            document["updated_at"] = now()
            self.projects.save_document(
                episode_id, "video_references", document)
        return sorted(changed)

    def _regenerate_revised_frame_chain(
            self, ctx, storyboard, start_shot, feedback="",
            prompt_override="", quality_override="auto",
            revision_source="manual", codex_profile=""):
        """重做当前镜及同场已有的后续帧链，并作废对应旧视频。

        同场下一镜的首帧依赖上一镜尾帧。只重做当前镜会留下看似完整、
        实际已经断链的首尾帧，因此沿场景链同步；尚未生产的后续镜不抢跑。
        """
        scene_no = start_shot.get("scene_no")
        scene_shots = [
            shot for shot in storyboard.get("shots", [])
            if shot.get("scene_no") == scene_no]
        try:
            start_index = next(
                index for index, shot in enumerate(scene_shots)
                if int(shot["shot_no"]) == int(start_shot["shot_no"]))
        except StopIteration:
            raise AifosError(f"镜头不存在: {start_shot['shot_no']}")
        previous_last = None
        if start_index:
            previous = scene_shots[start_index - 1]
            row = self.assets.latest(
                ctx["project"]["id"], "last_frame",
                self._shot_name(ctx, previous["shot_no"]))
            if (row and formal_reference_allowed(self._asset_quality(row))
                    and row["uri"] and Path(row["uri"]).exists()):
                previous_last = row["uri"]

        affected, invalidated_videos = [], []
        for offset, shot in enumerate(scene_shots[start_index:]):
            asset_name = self._shot_name(ctx, shot["shot_no"])
            old_first = self.assets.latest(
                ctx["project"]["id"], "first_frame", asset_name)
            old_last = self.assets.latest(
                ctx["project"]["id"], "last_frame", asset_name)
            # 当前镜或后续镜尚未进入首尾帧生产线时都不抢跑；人工修好
            # 关键帧后由断点续产自然补齐，避免一次手动修图意外开始整条
            # 帧链并额外消耗额度。
            if not (old_first or old_last):
                break
            image_row = self.assets.latest(
                ctx["project"]["id"], "image", asset_name)
            if not (image_row and image_row["uri"]
                    and (image_row["uri"].startswith(("http://", "https://"))
                         or Path(image_row["uri"]).exists())):
                break
            choice = quality_override if offset == 0 else "auto"
            frames_payload = self._shot_payload(
                ctx, shot, continuity_anchor=len(scene_shots) > 1,
                quality_override=choice,
                item_id=f"frames:{shot['shot_no']}")
            if formal_reference_allowed(self._asset_quality(image_row)):
                frames_payload["image_uri"] = image_row["uri"]
            else:
                frames_payload["draft_image_rejected"] = image_row["uri"]
            frames_payload["feedback"] = feedback if offset == 0 else ""
            prior = self.assets.latest(
                ctx["project"]["id"], "first_frame", asset_name,
                include_deleted=True)
            frames_payload["revision"] = (
                (prior["version"] + 1) if prior else 1)
            if offset == 0 and prompt_override:
                frames_payload["prompt"] = prompt_override
                frames_payload["prompt_compact"] = prompt_override
                frames_payload["action"] = prompt_override
                frames_payload["seedance_prompt"] = prompt_override
            if previous_last:
                frames_payload["chain_first_uri"] = previous_last
            if codex_profile:
                frames_payload["_codex_profile"] = str(codex_profile)
            frame_result = self._plan_run(
                ctx, f"frames:{shot['shot_no']}",
                lambda payload=frames_payload: self._call(
                    ctx, "frames", payload, "frames"),
                prompt=self._prompt_with_feedback(
                    frames_payload["prompt"],
                    frames_payload["feedback"]),
                payload=frames_payload, revision_source=revision_source)
            meta = self._quality_meta(frames_payload["quality_decision"])
            new_first = self.assets.register(
                ctx["project"]["id"], "first_frame", asset_name,
                uri=frame_result.data["first"], meta=meta,
                new_version=True)
            new_last = self.assets.register(
                ctx["project"]["id"], "last_frame", asset_name,
                uri=frame_result.data["last"], meta=meta,
                new_version=True)
            if old_first is not None:
                self.assets.mark_superseded(
                    old_first["id"], new_first["id"],
                    reason="frame_chain_revision")
            if old_last is not None:
                self.assets.mark_superseded(
                    old_last["id"], new_last["id"],
                    reason="frame_chain_revision")
            previous_last = frame_result.data["last"]
            affected.append(int(shot["shot_no"]))
            video = self.assets.latest(
                ctx["project"]["id"], "video", asset_name)
            if video is not None:
                self.assets.soft_delete(
                    ctx["project"]["id"], "video", asset_name,
                    meta={"invalidated_by_shot_revision":
                          int(start_shot["shot_no"])})
                invalidated_videos.append(int(shot["shot_no"]))
        return {
            "frame_shots": affected,
            "invalidated_video_shots": invalidated_videos,
        }

    def _apply_revised_frame_assets(
            self, ctx, storyboard, shot, frame_uris, meta,
            formal_ready=True, sync_boundaries=None):
        """登记修改后的首/尾帧，并同步同场共享边界与所有旧引用。

        一段视频的尾帧就是同场下一段视频的首帧。人工只改其中一侧时，
        必须让相邻资产指向同一张新图；手选 Seedance 参考仍保存 asset_id，
        因而也要原子迁移并作废所有用旧图生成过的视频。
        """
        project_id = ctx["project"]["id"]
        episode_id = ctx["episode"]["id"]
        shot_no = int(shot["shot_no"])
        scene_no = shot.get("scene_no")
        scene_shots = [
            candidate for candidate in storyboard.get("shots", [])
            if candidate.get("scene_no") == scene_no]
        try:
            index = next(
                pos for pos, candidate in enumerate(scene_shots)
                if int(candidate["shot_no"]) == shot_no)
        except StopIteration:
            raise AifosError(f"镜头不存在: {shot_no}")

        allowed = {"first_frame", "last_frame"}
        updates = {
            kind: uri for kind, uri in (frame_uris or {}).items()
            if kind in allowed and uri}
        if not updates:
            raise AifosError("没有可登记的首帧或尾帧")
        sync_boundaries = (
            set(updates) if sync_boundaries is None
            else set(sync_boundaries))

        revised_rows = {}
        frame_shots = {shot_no}
        reference_shots = set()
        boundary_sync = []

        def register(kind, candidate, uri, extra=None):
            name = self._shot_name(ctx, candidate["shot_no"])
            old = self.assets.latest(project_id, kind, name)
            row = self.assets.register(
                project_id, kind, name, uri=uri,
                meta={**(meta or {}), **(extra or {})},
                new_version=True)
            reference_shots.update(self._sync_revised_video_references(
                episode_id, project_id, old["id"] if old else None,
                row, usable=formal_ready))
            if old is not None:
                self.assets.mark_superseded(
                    old["id"], row["id"], reason="frame_revision")
            return row

        for kind, uri in updates.items():
            revised_rows[kind] = register(
                kind, shot, uri,
                {"revised_frame": True, "source_shot": shot_no})

        if "first_frame" in updates and "first_frame" in sync_boundaries \
                and index > 0:
            previous = scene_shots[index - 1]
            previous_name = self._shot_name(ctx, previous["shot_no"])
            if self.assets.latest(project_id, "last_frame", previous_name):
                row = register(
                    "last_frame", previous, updates["first_frame"],
                    {"boundary_synced_from": shot_no,
                     "boundary_source_kind": "first_frame"})
                frame_shots.add(int(previous["shot_no"]))
                boundary_sync.append({
                    "source_shot": shot_no, "source_kind": "first_frame",
                    "synced_shot": int(previous["shot_no"]),
                    "synced_kind": "last_frame", "asset_id": row["id"],
                    "label": f"镜头{int(previous['shot_no'])}尾帧",
                })

        if "last_frame" in updates and "last_frame" in sync_boundaries \
                and index + 1 < len(scene_shots):
            following = scene_shots[index + 1]
            following_name = self._shot_name(ctx, following["shot_no"])
            if self.assets.latest(project_id, "first_frame", following_name):
                row = register(
                    "first_frame", following, updates["last_frame"],
                    {"boundary_synced_from": shot_no,
                     "boundary_source_kind": "last_frame"})
                frame_shots.add(int(following["shot_no"]))
                boundary_sync.append({
                    "source_shot": shot_no, "source_kind": "last_frame",
                    "synced_shot": int(following["shot_no"]),
                    "synced_kind": "first_frame", "asset_id": row["id"],
                    "label": f"镜头{int(following['shot_no'])}首帧",
                })

        invalidated_videos = []
        for affected_no in sorted(frame_shots | reference_shots):
            name = self._shot_name(ctx, affected_no)
            if self.assets.latest(project_id, "video", name) is None:
                continue
            self.assets.soft_delete(
                project_id, "video", name,
                meta={"invalidated_by_frame_revision": shot_no})
            invalidated_videos.append(affected_no)

        return {
            "frame_shots": sorted(frame_shots),
            "affected_shots": sorted(frame_shots | reference_shots),
            "invalidated_video_shots": invalidated_videos,
            "video_reference_shots": sorted(reference_shots),
            "boundary_sync": boundary_sync,
            "frame_asset_ids": {
                kind: row["id"] for kind, row in revised_rows.items()},
        }

    def _invalidate_revised_delivery(
            self, ctx, shot, formal_ready=True, affected_shots=None,
            source_kind="shot"):
        """镜头版本变化后隐藏旧成片/检查板，并登记待重拍状态。"""
        project_id = ctx["project"]["id"]
        episode_id = ctx["episode"]["id"]
        ep_name = f"e{ctx['episode']['number']:03d}"
        invalidated = []

        def invalidate(kind, name):
            row = self.assets.latest(project_id, kind, name)
            if row is None:
                return
            self.assets.soft_delete(
                project_id, kind, name,
                meta={"invalidated_by_shot_revision": int(shot["shot_no"])})
            invalidated.append(kind)

        invalidate("edit", f"{ep_name}_final")
        invalidate("review_board", ep_name)
        affected = sorted({
            int(value) for value in (affected_shots or [shot["shot_no"]])})
        storyboard = ctx.get("storyboard") or {}
        scene_by_shot = {
            int(candidate["shot_no"]): int(candidate["scene_no"])
            for candidate in storyboard.get("shots", [])
            if candidate.get("shot_no") is not None
            and candidate.get("scene_no") is not None}
        scene_nos = sorted({
            scene_by_shot.get(value, int(shot["scene_no"]))
            for value in affected})
        for scene_no in scene_nos:
            invalidate("clip", f"{ep_name}_scene{scene_no:02d}")
        self.projects.set_qc_score(episode_id, None)
        revision = {
            "schema": "aifos.shot-revision-state/v1",
            "active": True,
            "shot_no": int(shot["shot_no"]),
            "scene_no": int(shot["scene_no"]),
            "source_kind": source_kind,
            "affected_shots": affected,
            "affected_scenes": scene_nos,
            "formal_ready": bool(formal_ready),
            "invalidated": invalidated,
            "updated_at": now(),
        }
        self.projects.save_document(
            episode_id, "shot_revision_state", revision)
        self.projects.set_episode_status(episode_id, "awaiting_confirm")
        return {"invalidated_outputs": invalidated,
                "formal_ready": bool(formal_ready)}

    def regen_image(self, project_title, episode_number, target,
                    feedback="", prompt_override="", quality_override=None,
                    revision_source="manual", codex_profile=""):
        """重画单张图:target = {"kind": character_art|scene_art|shot|
        first_frame|last_frame, "name"|"shot_no"};附意见时按意见调整;
        prompt_override 非空则整句替换默认提示词(所见即所得)。
        镜头画面重画会连带重生成首尾帧并作废旧视频(补齐时重拍)。"""
        project = self.projects.get_project(project_title)
        if project is None:
            raise AifosError(f"项目不存在: {project_title}")
        episode = self.db.query_one(
            "SELECT * FROM episodes WHERE project_id=? AND number=?",
            (project["id"], episode_number))
        if episode is None:
            raise AifosError(f"剧集不存在: 第{episode_number}集")
        script, _ = self.projects.latest_document(episode["id"], "script")
        if script is None:
            raise AifosError("本集尚无剧本,先完成预生产")
        aspect = (project["aspect"]
                  or self.config.get("defaults", "aspect", default="9:16"))
        standard, _ = self.projects.latest_document(
            episode["id"], "production_standard")
        blocking, _ = self.projects.latest_document(
            episode["id"], "blocking")
        ctx = {
            "project": dict(project), "episode": dict(episode),
            "out_root": self._episode_dir(project, episode),
            "aspect": aspect,
            "dims": ASPECT_DIMS.get(aspect, ASPECT_DIMS["9:16"]),
            "script": script, "force": True,
            "production_standard": standard,
            "production_profile": production_profile(self.config, standard),
            "blocking": blocking,
            "character_asset_policy": self.character_asset_policy(
                episode["id"], script=script),
        }
        self._task_cost = 0.0
        self._task_providers = set()
        style = project["style"] or DEFAULT_VISUAL_STYLE
        kind = target.get("kind")
        prompt_override = (prompt_override or "").strip()
        quality_choice = (normalize_quality(
            quality_override, allow_auto=True, field="image_quality")
            if quality_override is not None else "auto")
        item_id = {
            "character_sheet": lambda: f"sheet:{target.get('name', '')}",
            "scene_art": lambda: f"scene:{target.get('name', '')}",
            "shot": lambda: f"shot:{int(target.get('shot_no', 0))}",
            "frames": lambda: f"frames:{int(target.get('shot_no', 0))}",
            "first_frame": lambda: (
                f"frames:{int(target.get('shot_no', 0))}"),
            "last_frame": lambda: (
                f"frames:{int(target.get('shot_no', 0))}"),
        }.get(kind, lambda: "")()
        plan_item = None
        plan_diagnostics = {}
        if item_id:
            plan_item = next(
                (entry for entry in self._plan_read(ctx)["items"]
                 if entry.get("id") == item_id), None)
            old_qc = (plan_item or {}).get("qc") or {}
            plan_diagnostics = normalize_generation_diagnostics(
                old_qc.get("input_diagnosis") or {},
                issues=old_qc.get("issues"))
            auto_revision = old_qc.get("revision_feedback") or ""
            # 兼容旧版计划:旧版本把“电脑屏幕空白”落成 generic。每次手动
            # 重画都依据当前质检原因重新编译，不能继续沿用旧的泛化提示。
            if old_qc.get("issues"):
                refreshed_revision = optimize_qc_feedback(
                    old_qc.get("issues") or [], mode="image",
                    diagnostics=(
                        plan_diagnostics
                        if plan_diagnostics.get("diagnosis_complete")
                        else None))
                auto_revision = refreshed_revision["text"]
            prompt_is_unchanged = (not prompt_override or prompt_override ==
                                   (plan_item or {}).get("prompt", ""))
            if (auto_revision and prompt_is_unchanged
                    and "【自动优化修订】" not in feedback):
                feedback = ((auto_revision + "\n【人工补充】" + feedback)
                            if feedback else auto_revision)[:2400]
        policy = self._episode_quality_policy(episode["id"])
        if quality_override is not None and item_id:
            policy = set_policy_choices(
                policy, image_overrides={item_id: quality_choice})
            self.projects.save_document(
                episode["id"], "quality_policy", policy)
        ctx["quality_policy"] = policy

        def next_revision(asset_kind, asset_name):
            """每次重画都进入提示词/占位种子,保证重画必然产生新画面。"""
            row = self.assets.latest(project["id"], asset_kind, asset_name)
            return (row["version"] + 1) if row else 1

        if kind == "character_art":
            raise AifosError(
                "最终立绘不能从文字直接重画。请按角色重要度重新生成候选并人工定版，"
                "或上传经过确认的最终立绘")
        elif kind == "character_sheet":
            if not ctx["character_asset_policy"]["generate_sheets"]:
                raise AifosError(
                    "本集使用简化人物资产模式，不生成四视图或细节图；"
                    "如确有需要，请在人物定版阶段切换为完整模式")
            raw = target["name"]
            if ":" not in raw:
                raise AifosError(f"资产名需为 角色:套件键,收到 {raw}")
            name, sheet_key = raw.split(":", 1)
            entry = next((s for s in CHARACTER_SHEETS
                          if s[0] == sheet_key), None)
            if entry is None:
                raise AifosError(f"未知人物资产套件: {sheet_key}")
            _, label, desc = entry
            role = next((c.get("role", "") for c in script["characters"]
                         if c["name"] == name), "")
            portrait = self.assets.latest(
                project["id"], "character_art", name)
            portrait_uri = (portrait["uri"]
                            if portrait and portrait["uri"]
                            and Path(portrait["uri"]).exists() else None)
            locked_look = self._locked_look_variant(project["id"], name)
            existing_sheet_refs = self._character_sheet_reference_uris(
                project["id"], name, exclude_key=sheet_key)
            user_references = self._user_reference_payload(
                project["id"], [name],
                allowed_roles={"identity", "wardrobe", "composition"})
            prompt = prompt_override or self._sheet_prompt(
                name, role, style, label, desc, key=sheet_key,
                design=self._character_design(project["id"], name),
                locked_look=locked_look)
            quality = resolve_image_quality(
                recommend_asset_quality("character_sheet"), policy,
                f"sheet:{raw}", explicit_override=quality_choice)
            payload = {
                    "character_sheet": sheet_key, "sheet_label": label,
                    "image_task_class": image_task_class_for(
                        quality["level"]),
                    "image_quality": quality["level"],
                    "quality_decision": quality,
                    "art_name": name, "role": role,
                    "shot_no": 0, "characters": [name], "location": "",
                    "prompt": prompt, "style": style,
                    "prompt_contract_complete": True,
                    "feedback": feedback,
                    "revision": next_revision("character_sheet", raw),
                    "character_refs": (
                        ([portrait_uri] if portrait_uri else [])
                        + existing_sheet_refs),
                    "identity_references": self._identity_references(
                        project["id"], [name]),
                    "require_reference_images": True,
                    **user_references,
                    "style_ref": self._style_anchor_uri(project["id"]),
                    "aspect": aspect, **ctx["dims"],
            }
            result = self._plan_run(
                ctx, f"sheet:{name}:{sheet_key}",
                lambda: self._call(ctx, "image", payload, "cast"),
                prompt=self._prompt_with_feedback(prompt, feedback),
                payload=payload, revision_source=revision_source)
            old_sheet = self.assets.latest(
                project["id"], "character_sheet", raw)
            new_sheet = self.assets.register(
                project["id"], "character_sheet", raw, uri=result.uri,
                meta={"character": name, "sheet": sheet_key,
                      "label": label, **self._quality_meta(quality)},
                new_version=True)
            if old_sheet is not None:
                self.assets.mark_superseded(
                    old_sheet["id"], new_sheet["id"],
                    reason="character_sheet_revision")
        elif kind == "scene_art":
            name = target["name"]
            scene = next((s for s in script["scenes"]
                          if s["location"] == name), {})
            prompt = prompt_override or self._scene_prompt(
                name, style, scene,
                premise=episode["premise"] if episode else "")
            reference_payload = self._user_reference_payload(
                project["id"], [name],
                allowed_roles={"scene", "composition"})
            references = reference_payload["reference_images"]
            style_ref = self._style_anchor_uri(project["id"])
            reuse_count = sum(1 for value in script["scenes"]
                              if value.get("location") == name)
            quality = resolve_image_quality(
                recommend_asset_quality("scene_art", reuse_count=reuse_count),
                policy, f"scene:{name}", explicit_override=quality_choice)
            payload = {
                    "scene_art": True, "art_name": name,
                    "image_task_class": image_task_class_for(
                        quality["level"]),
                    "image_quality": quality["level"],
                    "quality_decision": quality,
                    "shot_no": 0, "characters": [], "location": name,
                    "action": scene.get("action", ""),
                    "prompt": prompt,
                    "style": style, "feedback": feedback,
                    "prompt_contract_complete": True,
                    "revision": next_revision("scene_art", name),
                    **reference_payload,
                    "style_ref": style_ref,
                    "require_reference_images": bool(
                        references or style_ref),
                    "aspect": aspect, **ctx["dims"],
            }
            result = self._plan_run(
                ctx, f"scene:{name}",
                lambda: self._call(ctx, "image", payload, "cast"),
                prompt=self._prompt_with_feedback(prompt, feedback),
                payload=payload, revision_source=revision_source)
            old_scene = self.assets.latest(
                project["id"], "scene_art", name)
            new_scene = self.assets.register(
                project["id"], "scene_art", name, uri=result.uri,
                meta=self._quality_meta(quality), new_version=True)
            if old_scene is not None:
                self.assets.mark_superseded(
                    old_scene["id"], new_scene["id"],
                    reason="scene_revision")
        elif kind == "shot":
            shot_no = int(target["shot_no"])
            storyboard, _ = self.projects.latest_document(
                episode["id"], "storyboard")
            shot = next((s for s in (storyboard or {}).get("shots", [])
                         if s["shot_no"] == shot_no), None)
            if shot is None:
                raise AifosError(f"镜头不存在: {shot_no}")
            ctx["storyboard"] = storyboard
            payload = self._shot_payload(
                ctx, shot, quality_override=quality_choice,
                item_id=f"shot:{shot_no}")
            payload["feedback"] = feedback
            payload["revision"] = next_revision(
                "image", self._shot_name(ctx, shot_no))
            if prompt_override:
                payload["prompt"] = prompt_override
                payload["prompt_compact"] = prompt_override
                payload["action"] = prompt_override
                payload["seedance_prompt"] = prompt_override
                payload["_reference_prompt_base"] = prompt_override
            if plan_diagnostics.get("diagnosis_complete"):
                reference_changes = self._apply_image_reference_adjustments(
                    payload, {"_project_id": project["id"]},
                    plan_diagnostics)
                if reference_changes["applied"]:
                    payload["qc_reference_changes"] = reference_changes
                    self._attach_reference_manifest(payload)
            asset_name = self._shot_name(ctx, shot_no)
            old_image = self.assets.latest(
                project["id"], "image", asset_name)
            revision_base = old_image["uri"] if old_image else ""
            if not revision_base and plan_item \
                    and plan_item.get("status") in (
                        "awaiting_human", "failed"):
                candidate = str(plan_item.get("output_uri") or "")
                if candidate and (
                        candidate.startswith(("http://", "https://"))
                        or Path(candidate).exists()):
                    revision_base = candidate
            allow_revision_base = (
                not plan_diagnostics.get("diagnosis_complete")
                or self._use_failed_image_as_revision_base(
                    plan_diagnostics))
            if (revision_base and allow_revision_base
                    and (revision_base.startswith(("http://", "https://"))
                         or Path(revision_base).exists())):
                payload["reference_images"] = list(dict.fromkeys([
                    revision_base,
                    *(payload.get("reference_images") or []),
                ]))
                matches = [
                    match for match in (payload.get("asset_matches") or [])
                    if match.get("uri") != revision_base]
                matches.append({
                    "asset_id": old_image["id"] if old_image else None,
                    "kind": (old_image["kind"] if old_image
                             else "qc_revision_base"),
                    "name": (old_image["name"] if old_image
                             else f"shot_{shot_no:03d}_failed"),
                    "label": ("本镜当前关键帧（待修改基底）"
                              if old_image else
                              "本镜二次质检失败稿（待人工修改基底）"),
                    "uri": revision_base,
                })
                payload["asset_matches"] = matches
                payload["require_reference_images"] = True
                self._attach_reference_manifest(payload)
            if codex_profile:
                payload["_codex_profile"] = str(codex_profile)
            result = self._plan_run(
                ctx, f"shot:{shot_no}",
                lambda: self._call(ctx, "image", payload, "images"),
                prompt=self._prompt_with_feedback(
                    prompt_override or payload["prompt"], feedback),
                payload=payload, revision_source=revision_source)
            new_image = self.assets.register(
                project["id"], "image", asset_name, uri=result.uri,
                meta=self._shot_image_meta(
                    ctx, shot, payload["quality_decision"],
                    {"revision": payload["revision"]}),
                new_version=True)
            formal_ready = formal_reference_allowed(
                payload["image_quality"])
            reference_shots = self._sync_revised_video_references(
                episode["id"], project["id"],
                old_image["id"] if old_image else None,
                new_image, usable=formal_ready)
            if old_image is not None:
                self.assets.mark_superseded(
                    old_image["id"], new_image["id"],
                    reason="image_revision")
            sync = self._regenerate_revised_frame_chain(
                ctx, storyboard, shot, feedback=feedback,
                prompt_override=prompt_override,
                quality_override=quality_choice,
                revision_source=revision_source,
                codex_profile=codex_profile)
            sync.update(self._invalidate_revised_delivery(
                ctx, shot, formal_ready=formal_ready))
            sync["video_reference_shots"] = reference_shots
            sync["image_asset_id"] = new_image["id"]
        elif kind in ("first_frame", "last_frame"):
            shot_no = int(target["shot_no"])
            storyboard, _ = self.projects.latest_document(
                episode["id"], "storyboard")
            shot = next((s for s in (storyboard or {}).get("shots", [])
                         if int(s["shot_no"]) == shot_no), None)
            if shot is None:
                raise AifosError(f"镜头不存在: {shot_no}")
            ctx["storyboard"] = storyboard
            asset_name = self._shot_name(ctx, shot_no)
            current = self.assets.latest(project["id"], kind, asset_name)
            if not (current and current["uri"]
                    and (current["uri"].startswith(("http://", "https://"))
                         or Path(current["uri"]).exists())):
                label = "首帧" if kind == "first_frame" else "尾帧"
                raise AifosError(f"镜头{shot_no}尚无{label},请先生成首尾帧")
            counterpart_kind = (
                "last_frame" if kind == "first_frame" else "first_frame")
            counterpart = self.assets.latest(
                project["id"], counterpart_kind, asset_name)
            image_row = self.assets.latest(
                project["id"], "image", asset_name)
            scene_shots = [
                candidate for candidate in storyboard.get("shots", [])
                if candidate.get("scene_no") == shot.get("scene_no")]
            payload = self._shot_payload(
                ctx, shot, continuity_anchor=len(scene_shots) > 1,
                quality_override=quality_choice,
                item_id=f"frames:{shot_no}")
            if plan_diagnostics.get("diagnosis_complete"):
                reference_changes = self._apply_image_reference_adjustments(
                    payload, {"_project_id": project["id"]},
                    plan_diagnostics)
                if reference_changes["applied"]:
                    payload["qc_reference_changes"] = reference_changes
            label = "首帧" if kind == "first_frame" else "尾帧"
            state_label = "起始状态" if kind == "first_frame" else "结束状态"
            state = (shot.get("start_state", {}) if kind == "first_frame"
                     else shot.get("end_state", {}))
            use_current_revision_base = (
                not plan_diagnostics.get("diagnosis_complete")
                or self._use_failed_image_as_revision_base(
                    plan_diagnostics))
            base_prompt = prompt_override or payload.get(
                "_reference_prompt_base", payload["prompt"])
            payload["_reference_prompt_base"] = (
                f"只修改镜头{shot_no}的{label}这一张静帧，不生成另一张帧，"
                f"也不改关键分镜。{base_prompt}。{state_label}:{state}。"
                + ("严格以当前待修改原图为基底落实修改意见；"
                   if use_current_revision_base else
                   "失败稿不作为参考，按已锁定人物、关键分镜和另一端帧重新生成；")
                + "未被意见指出的"
                "人物身份、人数、构图、机位、服装、发型、妆容、道具、"
                "场景、文字和光影保持不变")
            payload["prompt"] = payload["_reference_prompt_base"]
            if prompt_override:
                payload["action"] = prompt_override
            payload["seedance_prompt"] = payload["prompt"]
            payload["frame_kind"] = kind
            payload["feedback"] = feedback
            payload["revision"] = next_revision(kind, asset_name)

            rows = [
                (current, f"本镜当前{label}（待修改基底）"),
                (image_row, "本镜参考分镜"),
                (counterpart,
                 f"本镜{'尾帧' if kind == 'first_frame' else '首帧'}"
                 "（连续性约束）"),
            ]
            if not use_current_revision_base:
                rows = rows[1:]
            references = []
            for row, _label in rows:
                if row and row["uri"] and row["uri"] not in references:
                    references.append(row["uri"])
            for uri in payload.get("reference_images") or []:
                if uri not in references:
                    references.append(uri)
            payload["reference_images"] = references
            matches = list(payload.get("asset_matches") or [])
            for row, ref_label in rows:
                if not row or not row["uri"]:
                    continue
                matches = [
                    match for match in matches
                    if match.get("uri") != row["uri"]]
                matches.append({
                    "asset_id": row["id"], "kind": row["kind"],
                    "name": row["name"], "label": ref_label,
                    "uri": row["uri"],
                })
            payload["asset_matches"] = matches
            payload["require_reference_images"] = True
            self._attach_reference_manifest(payload)
            if codex_profile:
                payload["_codex_profile"] = str(codex_profile)

            revision_dir = (
                f"frames/revisions/shot_{shot_no:03d}/"
                f"{kind}_v{payload['revision']}")
            result = self._plan_run(
                ctx, f"frames:{shot_no}",
                lambda: self._call(
                    ctx, "image", payload, revision_dir),
                prompt=self._prompt_with_feedback(
                    payload["prompt"], feedback),
                payload=payload, revision_source=revision_source)
            meta = {
                **self._quality_meta(payload["quality_decision"]),
                "frame_kind": kind, "revision": payload["revision"],
                "revision_source": revision_source,
            }
            formal_ready = formal_reference_allowed(
                payload["image_quality"])
            sync = self._apply_revised_frame_assets(
                ctx, storyboard, shot, {kind: result.uri}, meta,
                formal_ready=formal_ready)
            sync.update(self._invalidate_revised_delivery(
                ctx, shot, formal_ready=formal_ready,
                affected_shots=sync["affected_shots"],
                source_kind=kind))
        elif kind == "frames":
            shot_no = int(target["shot_no"])
            storyboard, _ = self.projects.latest_document(
                episode["id"], "storyboard")
            shot = next((s for s in (storyboard or {}).get("shots", [])
                         if s["shot_no"] == shot_no), None)
            if shot is None:
                raise AifosError(f"镜头不存在: {shot_no}")
            ctx["storyboard"] = storyboard
            asset_name = self._shot_name(ctx, shot_no)
            image_row = self.assets.latest(project["id"], "image",
                                           asset_name)
            if not (image_row and image_row["uri"]
                    and Path(image_row["uri"]).exists()):
                raise AifosError(f"镜头{shot_no}尚无关键图,请先重画镜头")
            scene_shots = [candidate for candidate in storyboard["shots"]
                           if candidate.get("scene_no") == shot.get("scene_no")]
            frames_payload = {
                **self._shot_payload(
                    ctx, shot, continuity_anchor=len(scene_shots) > 1,
                    quality_override=quality_choice,
                    item_id=f"frames:{shot_no}"),
                "feedback": feedback,
                "revision": next_revision("first_frame", asset_name),
            }
            if plan_diagnostics.get("diagnosis_complete"):
                reference_changes = self._apply_image_reference_adjustments(
                    frames_payload, {"_project_id": project["id"]},
                    plan_diagnostics)
                if reference_changes["applied"]:
                    frames_payload["qc_reference_changes"] = (
                        reference_changes)
                    self._attach_reference_manifest(frames_payload)
            if formal_reference_allowed(self._asset_quality(image_row)):
                frames_payload["image_uri"] = image_row["uri"]
            else:
                frames_payload["draft_image_rejected"] = image_row["uri"]
            if prompt_override:
                frames_payload["prompt"] = prompt_override
                frames_payload["prompt_compact"] = prompt_override
                frames_payload["action"] = prompt_override
                frames_payload["seedance_prompt"] = prompt_override
            prev = None
            for candidate in storyboard["shots"]:
                if candidate["shot_no"] >= shot_no:
                    break
                if candidate.get("scene_no") == shot.get("scene_no"):
                    prev = candidate
            if prev is not None:
                row = self.assets.latest(
                    project["id"], "last_frame",
                    self._shot_name(ctx, prev["shot_no"]))
                if (row and formal_reference_allowed(self._asset_quality(row))
                        and row["uri"] and Path(row["uri"]).exists()):
                    frames_payload["chain_first_uri"] = row["uri"]
            if codex_profile:
                frames_payload["_codex_profile"] = str(codex_profile)
            result = self._plan_run(
                ctx, f"frames:{shot_no}", lambda: self._call(
                    ctx, "frames", frames_payload, "frames"),
                prompt=self._prompt_with_feedback(
                    frames_payload["prompt"], feedback),
                payload=frames_payload, revision_source=revision_source)
            formal_ready = formal_reference_allowed(
                frames_payload["image_quality"])
            sync_boundaries = {"last_frame"}
            if (not frames_payload.get("chain_first_uri")
                    and result.data.get("first_source") != "previous_tail"):
                sync_boundaries.add("first_frame")
            sync = self._apply_revised_frame_assets(
                ctx, storyboard, shot,
                {"first_frame": result.data["first"],
                 "last_frame": result.data["last"]},
                self._quality_meta(frames_payload["quality_decision"]),
                formal_ready=formal_ready,
                sync_boundaries=sync_boundaries)
            sync.update(self._invalidate_revised_delivery(
                ctx, shot, formal_ready=formal_ready,
                affected_shots=sync["affected_shots"],
                source_kind="frames"))
        else:
            raise AifosError(f"不支持的重画目标: {kind}")
        if feedback:
            self.data.record(
                "case", "failure", prompt=feedback,
                meta={"reason": "image_revision", "target": target},
                episode_id=episode["id"])
        self.log.info(
            "director",
            f"重画完成: {target}(意见: {feedback or '无'})")
        response = {"target": target, "uri": result.uri,
                "quality": (result.data or {}).get(
                    "image_quality", quality_choice),
                "cost": round(self._task_cost, 2)}
        if kind in ("shot", "frames", "first_frame", "last_frame"):
            response["sync"] = sync
        return response

    # ---- 人工修改素材导入(下载 → 外部修图/剪辑 → 上传替换) ----
    IMAGE_MAGIC = {".png": b"\x89PNG", ".jpg": b"\xff\xd8\xff",
                   ".jpeg": b"\xff\xd8\xff", ".webp": b"RIFF",
                   ".svg": b"<"}

    def _episode_ctx(self, project_title, episode_number):
        project = self.projects.get_project(project_title)
        if project is None:
            raise AifosError(f"项目不存在: {project_title}")
        episode = self.db.query_one(
            "SELECT * FROM episodes WHERE project_id=? AND number=?",
            (project["id"], episode_number))
        if episode is None:
            raise AifosError(f"剧集不存在: 第{episode_number}集")
        return project, episode

    def _invalidate_cast_assets(self, project, script, reason):
        """让新一轮人物候选遮蔽旧版本,但保留旧文件和历史记录。"""
        for character in script.get("characters", []):
            name = character["name"]
            self.assets.register(
                project["id"], "character_identity", name, uri="",
                meta={"character": name, "locked": False,
                      "reason": reason}, new_version=True)
            for index in range(1, CHARACTER_CANDIDATES + 1):
                self.assets.register(
                    project["id"], "character_candidate",
                    f"{name}:{index:02d}", uri="",
                    meta={"character": name,
                          "role": character.get("role", ""),
                          "candidate_index": index,
                          "invalidated": reason}, new_version=True)
            for key, label, _desc in CHARACTER_SHEETS:
                self.assets.register(
                    project["id"], "character_sheet", f"{name}:{key}",
                    uri="", meta={"character": name, "sheet": key,
                                   "label": label,
                                   "invalidated": reason},
                    new_version=True)
        for location in dict.fromkeys(
                scene["location"] for scene in script.get("scenes", [])):
            self.assets.register(
                project["id"], "scene_art", location, uri="",
                meta={"invalidated": reason}, new_version=True)

    def regenerate_character_candidates(self, project_title, episode_number,
                                        run_id=None):
        """放弃当前人物选择并重新生成候选,不进入后续镜头生产。"""
        project, episode = self._episode_ctx(project_title, episode_number)
        if episode["status"] != "awaiting_cast":
            raise AifosError("只能在人物选择阶段返回重新生成")
        script, _ = self.projects.latest_document(episode["id"], "script")
        if script is None:
            raise AifosError("本集尚无剧本,先完成剧本确认")
        self._invalidate_cast_assets(
            project, script, reason="manual_regenerate_cast")
        self.projects.set_episode_status(episode["id"], "cast")
        aspect = (project["aspect"]
                  or self.config.get("defaults", "aspect", default="9:16"))
        ctx = {"project": dict(project), "episode": dict(episode),
               "out_root": self._episode_dir(project, episode),
               "script": script, "force": False, "aspect": aspect,
               "dims": ASPECT_DIMS.get(aspect, ASPECT_DIMS["9:16"]),
               "run_id": run_id,
               "quality_policy": self._episode_quality_policy(
                   episode["id"], persist=True)}
        self._task_cost = 0.0
        self._task_providers = set()
        try:
            report = self._stage_cast(ctx)
        except ProduceCancelled:
            self.projects.set_episode_status(episode["id"], "awaiting_cast")
            return {"status": "paused", "done": 0,
                    "note": "人物候选重新生成已暂停,已完成候选保留"}
        self.projects.set_episode_status(episode["id"], "awaiting_cast")
        self.log.info(
            "director", "已放弃当前人物选择,新候选已生成,等待重新定版"
            f"(episode_id={episode['id']})")
        return {"status": "awaiting_cast",
                "done": report.get("candidates", 0),
                "candidate_target": report.get("candidate_target", 0),
                "locked": 0}

    def import_image(self, project_title, episode_number, target,
                     file_bytes, ext):
        """上传替换图片:人物/场景/关键分镜/单独首帧或尾帧。

        关键分镜替换后重做同场帧链；单独首尾帧替换会同步相邻共享边界，
        两种操作都会迁移 Seedance 手选引用并作废受影响的旧视频/成片。
        """
        ext = ext.lower()
        magic = self.IMAGE_MAGIC.get(ext)
        if magic is None:
            raise AifosError(f"不支持的图片格式: {ext}(png/jpg/webp/svg)")
        if not file_bytes or not file_bytes.lstrip()[:8].startswith(magic) \
                and not file_bytes.startswith(magic):
            raise AifosError("文件内容与图片格式不符")
        project, episode = self._episode_ctx(project_title, episode_number)
        out_root = self._episode_dir(project, episode)
        kind = target.get("kind")
        if kind in ("character_art", "scene_art", "character_sheet"):
            name = target["name"]
            latest = self.assets.latest(project["id"], kind, name)
            if latest is None:
                if kind != "character_art":
                    raise AifosError(f"资产不存在: {kind}/{name}")
                script, _ = self.projects.latest_document(
                    episode["id"], "script")
                known = {c.get("name") for c in
                         (script or {}).get("characters", [])}
                if name not in known:
                    raise AifosError(f"剧本中没有角色: {name}")
            version = (latest["version"] + 1) if latest else 1
            safe = "".join(c if c.isalnum() else "_" for c in name)[:40]
            path = (out_root / "cast"
                    / f"upload_{kind}_{safe}_v{version}{ext}")
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_bytes(file_bytes)
            uploaded_meta = {"uploaded": True, "image_quality": "high",
                             "recommended_quality": "high",
                             "quality_source": "manual_upload"}
            new_asset = self.assets.register(
                project["id"], kind, name, uri=str(path),
                meta=uploaded_meta, new_version=True)
            if latest is not None:
                self.assets.mark_superseded(
                    latest["id"], new_asset["id"],
                    reason="manual_upload_revision")
            if kind == "character_art":
                # 人工上传等同于明确确认最终立绘，同时建立真正的身份锚点。
                old_identity = self.assets.latest(
                    project["id"], "character_identity", name)
                new_identity = self.assets.register(
                    project["id"], "character_identity", name,
                    uri=str(path),
                    meta={"character": name, "locked": True,
                          "uploaded": True, "locked_at": now(),
                          "image_quality": "high",
                          "recommended_quality": "high",
                          "quality_source": "manual_upload"},
                    new_version=True)
                if old_identity is not None:
                    self.assets.mark_superseded(
                        old_identity["id"], new_identity["id"],
                        reason="manual_upload_revision")
            self.log.info("director", f"已上传替换 {kind}/{name}")
            return {"uri": str(path)}
        if kind == "shot":
            shot_no = int(target["shot_no"])
            storyboard, _ = self.projects.latest_document(
                episode["id"], "storyboard")
            script, _ = self.projects.latest_document(episode["id"], "script")
            shot = next((s for s in (storyboard or {}).get("shots", [])
                         if s["shot_no"] == shot_no), None)
            if shot is None:
                raise AifosError(f"镜头不存在: {shot_no}")
            asset_name = f"e{episode['number']:03d}_shot{shot_no:03d}"
            path = (out_root / "images"
                    / f"shot_{shot_no:03d}.upload{ext}")
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_bytes(file_bytes)
            aspect = (project["aspect"] or self.config.get(
                "defaults", "aspect", default="9:16"))
            standard, _ = self.projects.latest_document(
                episode["id"], "production_standard")
            blocking, _ = self.projects.latest_document(
                episode["id"], "blocking")
            ctx = {"project": dict(project), "episode": dict(episode),
                   "out_root": out_root, "aspect": aspect,
                   "dims": ASPECT_DIMS.get(aspect, ASPECT_DIMS["9:16"]),
                   "script": script, "storyboard": storyboard,
                   "production_standard": standard,
                   "production_profile": production_profile(
                       self.config, standard),
                   "blocking": blocking,
                   "quality_policy": self._episode_quality_policy(
                       episode["id"], persist=True),
                   "character_asset_policy": self.character_asset_policy(
                       episode["id"], script=script),
                   "force": True}
            old_image = self.assets.latest(
                project["id"], "image", asset_name)
            new_image = self.assets.register(
                project["id"], "image", asset_name, uri=str(path),
                meta=self._shot_image_meta(
                    ctx, shot,
                    {"level": "high",
                     "recommended": "high",
                     "source": "manual_upload",
                     "rule": "manual_upload",
                     "reasons": ["人工上传正式图"]},
                    {"uploaded": True}),
                new_version=True)
            # 按新图同步同场既有帧链、手选 Seedance 参考和下游成片状态。
            self._task_cost = 0.0
            self._task_providers = set()
            reference_shots = self._sync_revised_video_references(
                episode["id"], project["id"],
                old_image["id"] if old_image else None, new_image)
            if old_image is not None:
                self.assets.mark_superseded(
                    old_image["id"], new_image["id"],
                    reason="manual_upload_revision")
            sync = self._regenerate_revised_frame_chain(
                ctx, storyboard, shot, feedback="人工上传替换关键帧",
                quality_override="high", revision_source="manual_upload")
            sync.update(self._invalidate_revised_delivery(
                ctx, shot, formal_ready=True))
            sync["video_reference_shots"] = reference_shots
            sync["image_asset_id"] = new_image["id"]
            self.log.info(
                "director", f"已上传替换镜头{shot_no}画面并同步下游版本")
            return {"uri": str(path), "sync": sync}
        if kind in ("first_frame", "last_frame"):
            shot_no = int(target["shot_no"])
            storyboard, _ = self.projects.latest_document(
                episode["id"], "storyboard")
            script, _ = self.projects.latest_document(
                episode["id"], "script")
            shot = next((candidate for candidate in
                         (storyboard or {}).get("shots", [])
                         if int(candidate["shot_no"]) == shot_no), None)
            if shot is None:
                raise AifosError(f"镜头不存在: {shot_no}")
            asset_name = f"e{episode['number']:03d}_shot{shot_no:03d}"
            old_frame = self.assets.latest(
                project["id"], kind, asset_name)
            label = "首帧" if kind == "first_frame" else "尾帧"
            if old_frame is None:
                raise AifosError(f"镜头{shot_no}尚无{label},请先生成首尾帧")
            version = int(old_frame["version"]) + 1
            suffix = "first" if kind == "first_frame" else "last"
            path = (out_root / "frames" / "revisions"
                    / f"shot_{shot_no:03d}.{suffix}.upload_v{version}{ext}")
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_bytes(file_bytes)
            aspect = (project["aspect"] or self.config.get(
                "defaults", "aspect", default="9:16"))
            standard, _ = self.projects.latest_document(
                episode["id"], "production_standard")
            blocking, _ = self.projects.latest_document(
                episode["id"], "blocking")
            ctx = {
                "project": dict(project), "episode": dict(episode),
                "out_root": out_root, "aspect": aspect,
                "dims": ASPECT_DIMS.get(aspect, ASPECT_DIMS["9:16"]),
                "script": script, "storyboard": storyboard,
                "production_standard": standard,
                "production_profile": production_profile(
                    self.config, standard),
                "blocking": blocking,
                "quality_policy": self._episode_quality_policy(
                    episode["id"], persist=True),
                "character_asset_policy": self.character_asset_policy(
                    episode["id"], script=script),
                "force": True,
            }
            meta = {
                "uploaded": True, "frame_kind": kind,
                "image_quality": "high",
                "recommended_quality": "high",
                "quality_source": "manual_upload",
            }
            sync = self._apply_revised_frame_assets(
                ctx, storyboard, shot, {kind: str(path)}, meta,
                formal_ready=True)
            sync.update(self._invalidate_revised_delivery(
                ctx, shot, formal_ready=True,
                affected_shots=sync["affected_shots"],
                source_kind=kind))
            self.log.info(
                "director",
                f"已上传替换镜头{shot_no}{label}并同步所有共享边界与引用")
            return {"uri": str(path), "sync": sync}
        raise AifosError(f"不支持的上传目标: {kind}")

    def restyle_project(self, project_title, episode_number, style=None):
        """一键换画风后按角色重要度重新生成候选，禁止直接覆盖最终立绘。"""
        project, episode = self._episode_ctx(project_title, episode_number)
        if style and style.strip():
            self.projects.update_project(project_title, style=style.strip())
            project = self.projects.get_project(project_title)
        script, _ = self.projects.latest_document(episode["id"], "script")
        if script is None:
            raise AifosError("本集尚无剧本,先完成剧本确认")
        # 新画风使旧身份锚点和下游人物资产失效，但保留历史版本/文件。
        # 用空的新版本遮蔽旧最新版，重新按重要度候选并人工定版，不做破坏性删除。
        self._invalidate_cast_assets(project, script, reason="restyle")

        self.projects.set_episode_status(episode["id"], "cast")
        self.log.info(
            "director",
            f"新画风已生效，按角色重要度重新生成定妆候选"
            f"({character_candidate_policy_text()}): {project['style']}")
        aspect = (project["aspect"]
                  or self.config.get("defaults", "aspect", default="9:16"))
        ctx = {"project": dict(project), "episode": dict(episode),
               "out_root": self._episode_dir(project, episode),
               "script": script, "force": False, "aspect": aspect,
               "dims": ASPECT_DIMS.get(aspect, ASPECT_DIMS["9:16"])}
        self._task_cost = 0.0
        self._task_providers = set()
        try:
            report = self._stage_cast(ctx)
        except ProduceCancelled:
            self.projects.set_episode_status(episode["id"], "awaiting_cast")
            self.log.info(
                "director",
                "换风格候选生成已暂停，已完成候选保留，可从断点继续")
            return {"status": "paused", "done": 0,
                    "style": project["style"]}
        self.projects.set_episode_status(episode["id"], "awaiting_cast")
        self.log.info(
            "director", "新画风人物候选已就绪，请逐个选定最终立绘；"
            "选定后才重做资产套件和场景")
        return {"status": "awaiting_cast",
                "done": report.get("candidates", 0),
                "style": project["style"]}

    PLAN_TARGETS = {
        "char": lambda parts: {"kind": "character_art", "name": parts[0]},
        "sheet": lambda parts: {"kind": "character_sheet",
                                "name": ":".join(parts)},
        "scene": lambda parts: {"kind": "scene_art", "name": parts[0]},
        "shot": lambda parts: {"kind": "shot", "shot_no": int(parts[0])},
        "frames": lambda parts: {"kind": "frames",
                                 "shot_no": int(parts[0])},
    }
    # 初始母资产只负责建立人物/场景基准；视觉 QC 只对后续镜头图执行。
    INITIAL_ASSET_CATEGORIES = frozenset({
        "character_candidate", "character_art", "character_sheet", "scene_art",
    })
    SHOT_QC_CATEGORIES = frozenset({"shot_image", "frames"})

    # ---- 单张/批量质检:核对已生成的图是否符合剧本要求 ----
    _FORBID = ["与设定形态不符的角色", "悬挂的衣物或衣架", "与设定不符的人",
               "剧本未提及却自行出现的时代错乱物品或建筑(时代判断以剧本"
               "为准:剧本明确写出的穿越/带入物品是剧情事实,必须出现,"
               "不算错误;只有剧本没写、画面凭空冒出的时代外物品才算错,"
               "如古代场景凭空出现笔记本电脑/现代家具)",
               "结构扭曲、比例失常或被拉长变形的物体(如被拉长的笔记本/"
               "盒子,不合物理的道具)"]

    def _plan_item_target(self, item_id):
        head, _, rest = item_id.partition(":")
        builder = self.PLAN_TARGETS.get(head)
        return builder(rest.split(":")) if builder else None

    def _plan_item_asset(self, project_id, ep_num, item):
        """清单条目 → (最新资产 uri, 质检要求 spec);无法解析返回 (None,None)。"""
        cat = item.get("category")
        shot_no = item.get("shot_no")
        name = item.get("name")
        prefix = f"e{ep_num:03d}"
        if cat == "character_art":
            row = self.assets.latest(project_id, "character_art", name)
            spec = self._qc_spec(project_id, [name], forbid=self._FORBID)
        elif cat == "character_sheet":
            asset_name = (name if ":" in str(name)
                          else f"{name}:{item.get('sheet', '')}")
            row = self.assets.latest(
                project_id, "character_sheet", asset_name)
            spec = self._qc_spec(project_id, [str(name).split(":")[0]],
                                 forbid=self._FORBID)
        elif cat == "scene_art":
            row = self.assets.latest(project_id, "scene_art", name)
            spec = self._qc_spec(project_id, [], location=name,
                                 forbid=self._FORBID)
        elif cat == "shot_image":
            row = self.assets.latest(
                project_id, "image", f"{prefix}_shot{shot_no:03d}")
            spec = None   # 分镜人物名单在下面按分镜补
        elif cat == "frames":
            # 首尾帧两张都要检:返回首帧,尾帧在 _qc_one 里另查
            row = self.assets.latest(
                project_id, "first_frame", f"{prefix}_shot{shot_no:03d}")
            spec = None
        else:
            return None, None
        uri = row["uri"] if row and row["uri"] else None
        if uri and not (uri.startswith("http") or Path(uri).exists()):
            uri = None
        return uri, spec

    def _plan_generation_input(self, item, payload, qc_spec):
        """Recover the exact saved prompt/reference pair for later re-QC."""
        previous_qc = item.get("qc") or {}
        saved = previous_qc.get("generation_input")
        if isinstance(saved, dict) and saved.get("input_hash"):
            snapshot = copy.deepcopy(saved)
            snapshot.setdefault("scope", {})
            snapshot["scope"]["item_id"] = str(item.get("id") or "")
            snapshot["scope"]["shot_no"] = item.get("shot_no")
            snapshot["scope"]["frame_kind"] = str(
                item.get("category")
                or snapshot["scope"].get("frame_kind") or "")
            return snapshot
        saved = item.get("generation_input")
        if isinstance(saved, dict) and saved.get("input_hash"):
            snapshot = copy.deepcopy(saved)
            snapshot.setdefault("scope", {})
            snapshot["scope"]["item_id"] = str(item.get("id") or "")
            snapshot["scope"]["shot_no"] = item.get("shot_no")
            snapshot["scope"]["frame_kind"] = str(
                item.get("category")
                or snapshot["scope"].get("frame_kind") or "")
            return snapshot
        payload = copy.deepcopy(payload or {})
        self._attach_reference_manifest(payload)
        snapshot = self._image_generation_input(payload, qc_spec=qc_spec)
        saved_prompt = str(item.get("prompt_used") or "")
        if saved_prompt:
            snapshot["prompt"] = saved_prompt
            snapshot["prompt_hash"] = prompt_hash(saved_prompt)
        saved_refs = (
            (item.get("reference_inputs") or {}).get("items") or [])
        if saved_refs:
            # Legacy plans saved the mobile display order
            # identity→character→spatial, while providers actually submitted
            # identity→spatial→character.  Recover the provider order and
            # reuse the current same-URI binding; otherwise old images would
            # reproduce the very numbering mismatch this migration fixes.
            role_priority = {
                "identity": 10, "character_identity": 10,
                "inner_persona": 20,
                "spatial": 30, "spatial_blocking": 30,
                "character": 40, "identity_detail": 40,
                "wardrobe": 40, "structure": 40,
                "keyframe": 50,
                "continuity": 60,
                "scene": 70,
                "style": 80,
                "asset": 90, "user": 90, "manual": 90,
            }
            indexed_refs = [
                (position, ref)
                for position, ref in enumerate(saved_refs)
                if isinstance(ref, dict) and ref.get("uri")
            ]
            indexed_refs.sort(key=lambda row: (
                role_priority.get(str(
                    row[1].get("reference_role")
                    or row[1].get("kind") or "manual"), 90),
                row[0]))
            current_by_uri = {
                str(ref.get("uri") or ""): ref
                for ref in snapshot.get("reference_manifest") or []
                if isinstance(ref, dict) and ref.get("uri")
            }
            snapshot["reference_manifest"] = [
                {
                    **copy.deepcopy(current_by_uri.get(
                        str(ref.get("uri") or ""), {})),
                    "index": index,
                    "asset_id": (
                        ref.get("asset_id")
                        if ref.get("asset_id") is not None
                        else current_by_uri.get(
                            str(ref.get("uri") or ""), {}).get("asset_id")),
                    "uri": str(ref.get("uri") or ""),
                    "label": str(ref.get("label") or ref.get("name")
                                 or f"参考图{index}"),
                    "role": str(
                        current_by_uri.get(
                            str(ref.get("uri") or ""), {}).get("role")
                        or ref.get("reference_role")
                        or ref.get("kind") or "reference"),
                    "character": str(
                        current_by_uri.get(
                            str(ref.get("uri") or ""), {}).get("character")
                        or ref.get("attach_to") or ""),
                    "binding": str(
                        current_by_uri.get(
                            str(ref.get("uri") or ""), {}).get("binding")
                        or (
                            "按生产清单中记录的"
                            f"{ref.get('label') or ref.get('kind') or '参考'}"
                            "单一职责核验")),
                }
                for index, (_position, ref) in enumerate(indexed_refs, 1)
            ]
            snapshot["reference_hash"] = reference_hash(
                snapshot["reference_manifest"])
        snapshot["input_hash"] = generation_input_hash(
            snapshot["prompt"], snapshot["reference_manifest"])
        snapshot["scope"]["item_id"] = str(item.get("id") or "")
        snapshot["scope"]["shot_no"] = item.get("shot_no")
        snapshot["scope"]["frame_kind"] = str(
            item.get("category") or snapshot["scope"].get("frame_kind") or "")
        return snapshot

    def _qc_signature(self, uris, spec, generation_input=None):
        """图片、质检规格及实际生成输入都没变时才复用质检结果。"""
        digest = hashlib.sha256()
        digest.update(json.dumps(spec, ensure_ascii=False, sort_keys=True,
                                 separators=(",", ":")).encode("utf-8"))
        if generation_input:
            digest.update(str(
                generation_input.get("input_hash") or "").encode("utf-8"))
        for label, uri in uris:
            digest.update(label.encode("utf-8"))
            digest.update(str(uri).encode("utf-8"))
            path = Path(uri)
            if path.exists() and path.is_file():
                with path.open("rb") as stream:
                    for chunk in iter(lambda: stream.read(1024 * 1024), b""):
                        digest.update(chunk)
        return digest.hexdigest()

    def qc_item(self, project_title, episode_number, item_id):
        """单张质检:对清单里某条目的最新图做视觉核对,写回 qc 结果。"""
        project, episode = self._episode_ctx(project_title, episode_number)
        ctx = {"project": dict(project), "episode": dict(episode),
               "out_root": self._episode_dir(project, episode)}
        plan = self._plan_read(ctx)
        item = next((i for i in plan["items"] if i["id"] == item_id), None)
        if item is None:
            raise AifosError(f"清单里没有该条目: {item_id}")
        category = item.get("category")
        if category in self.INITIAL_ASSET_CATEGORIES:
            raise AifosError(
                "初始人物/场景母资产不做视觉质检；请在分镜关键帧或首尾帧生成后质检")
        if category not in self.SHOT_QC_CATEGORIES:
            raise AifosError("该清单条目不支持镜头视觉质检")
        report = self._qc_one(project, episode, ctx, item)
        report, _ = self._auto_repair_qc_item(
            project, episode, ctx, item, report)
        return report

    def _qc_one(self, project, episode, ctx, item, codex_profile=""):
        project_id = project["id"]
        payload = {}
        uri, spec = self._plan_item_asset(
            project_id, episode["number"], item)
        if not uri:
            self._plan_mark(ctx, item["id"], item.get("status", "done"),
                            extra={"qc": {"passed": False, "attempts": 0,
                                          "issues": ["尚无可检的图片"]}})
            return {"passed": False, "issues": ["尚无可检的图片"]}
        if spec is None:
            # 分镜/首尾帧:按分镜取人物名单/场景/动作
            storyboard, _ = self.projects.latest_document(
                episode["id"], "storyboard")
            shot = next((s for s in (storyboard or {}).get("shots", [])
                         if s["shot_no"] == item.get("shot_no")), None)
            if shot is not None:
                ctx["storyboard"] = storyboard
                if "script" not in ctx:
                    script, _ = self.projects.latest_document(
                        episode["id"], "script")
                    ctx["script"] = script or {"scenes": []}
                ctx.setdefault("aspect", project["aspect"] or "9:16")
                ctx.setdefault("dims", ASPECT_DIMS.get(
                    ctx["aspect"], ASPECT_DIMS["9:16"]))
                payload = self._shot_payload(ctx, shot)
                spec = self._qc_spec(
                    project_id, payload.get(
                        "identity_characters", payload.get("characters", [])),
                    location=payload.get("location", ""),
                    action=payload.get("action", ""),
                    forbid=self._FORBID + ["字幕条"],
                    expected_characters=payload.get("characters", []),
                    expected_count=payload.get("character_count"),
                    character_background=payload.get(
                        "character_background", {}),
                    camera=payload.get("camera", ""),
                    composition_contract=payload.get(
                        "composition_contract"),
                    readable_text=payload.get("readable_text"),
                    physical_contract=payload.get("physical_contract"),
                    physical_logic_required=True,
                    narrative_overlays=payload.get(
                        "narrative_overlays"))
            else:
                spec = self._qc_spec(project_id, [], forbid=self._FORBID)
        # 首尾帧:首帧 + 尾帧两张都要检,任一不符即整组不合格
        uris = [("首帧", uri)]
        if item.get("category") == "frames":
            last = self.assets.latest(
                project_id, "last_frame",
                f"e{episode['number']:03d}_shot{item.get('shot_no'):03d}")
            if last and last["uri"] and (last["uri"].startswith("http")
                                         or Path(last["uri"]).exists()):
                uris.append(("尾帧", last["uri"]))
        generation_input = self._plan_generation_input(
            item, payload, spec)
        signature = self._qc_signature(
            uris, spec, generation_input=generation_input)
        previous = item.get("qc") or {}
        if previous.get("signature") == signature \
                and "passed" in previous:
            cached = dict(previous)
            revision = optimize_qc_feedback(
                cached.get("issues") or [], mode="image",
                readable_text=spec.get("readable_text"),
                diagnostics=cached.get("input_diagnosis"))
            cached["revision_feedback"] = revision["text"]
            cached["revision_categories"] = revision["categories"]
            cached["cached"] = True
            self._plan_mark(ctx, item["id"], item.get("status", "done"),
                            extra={"qc": cached})
            return cached
        passed_all, issues, cost = True, [], 0.0
        identity_checked_all = True
        identity_match_all = True
        gender_checked_all = True
        gender_match_all = True
        count_checked_all = True
        count_match_all = True
        physical_checked_all = True
        physical_match_all = True
        spatial_checked_all = True
        spatial_match_all = True
        hard_failure = False
        image_passed_all = True
        input_contract_passed_all = True
        input_diagnoses = []
        try:
            for label, one in uris:
                result = self.router.call(
                    "image_qc", {
                        **spec,
                        "image_uri": one,
                        "generation_input": generation_input,
                        "generation_prompt": generation_input["prompt"],
                        "reference_manifest": generation_input[
                            "reference_manifest"],
                        "_codex_profile": str(codex_profile or ""),
                    }, ctx["out_root"],
                    cancel=lambda: self._cancel_requested(ctx))
                cost += result.cost
                one_report = self._assess_image_qc(
                    spec, result.data or {}, 1)
                input_diagnoses.append({
                    "frame": label,
                    "passed": bool(one_report["passed"]),
                    "image_passed": bool(one_report["image_passed"]),
                    "input_contract_passed": bool(
                        one_report["input_contract_passed"]),
                    **copy.deepcopy(one_report["input_diagnosis"]),
                })
                passed_all = passed_all and one_report["passed"]
                image_passed_all = (
                    image_passed_all and one_report["image_passed"])
                input_contract_passed_all = (
                    input_contract_passed_all
                    and one_report["input_contract_passed"])
                identity_checked_all = (
                    identity_checked_all
                    and one_report["identity_checked"])
                identity_match_all = (
                    identity_match_all and one_report["identity_match"])
                gender_checked_all = (
                    gender_checked_all and one_report["gender_checked"])
                gender_match_all = (
                    gender_match_all and one_report["gender_match"])
                count_checked_all = (
                    count_checked_all and one_report["count_checked"])
                count_match_all = (
                    count_match_all and one_report["count_match"])
                physical_checked_all = (
                    physical_checked_all
                    and one_report["physical_logic_checked"])
                physical_match_all = (
                    physical_match_all
                    and one_report["physical_logic_match"])
                spatial_checked_all = (
                    spatial_checked_all
                    and one_report["spatial_logic_checked"])
                spatial_match_all = (
                    spatial_match_all
                    and one_report["spatial_logic_match"])
                hard_failure = hard_failure or one_report["hard_failure"]
                issues.extend(
                    f"{label}:{x}" for x in one_report["issues"])
        except (ProviderUnavailable, ProviderError) as exc:
            raise AifosError(f"质检产线不可用: {exc}") from exc
        report = {"passed": passed_all, "issues": issues,
                  "attempts": previous.get("attempts", 0),
                  "identity_checked": identity_checked_all,
                  "identity_match": identity_match_all,
                  "gender_checked": gender_checked_all,
                  "gender_match": gender_match_all,
                  "count_checked": count_checked_all,
                  "count_match": count_match_all,
                  "physical_logic_checked": physical_checked_all,
                  "physical_logic_match": physical_match_all,
                  "spatial_logic_checked": spatial_checked_all,
                  "spatial_logic_match": spatial_match_all,
                  "image_passed": image_passed_all,
                  "visual_pass": image_passed_all,
                  "input_contract_passed": input_contract_passed_all,
                  "input_contract_pass": input_contract_passed_all,
                  "redraw_required": not image_passed_all,
                  "contract_repair_required":
                      not input_contract_passed_all,
                  "production_ready": passed_all,
                  "hard_failure": hard_failure,
                  "identity_references": len(
                      spec.get("identity_references") or []),
                  "generation_input_hash": generation_input["input_hash"],
                  "generation_input": copy.deepcopy(generation_input),
                  "input_diagnoses": input_diagnoses,
                  "input_diagnosis": (
                      next((
                          value for value in input_diagnoses
                          if not value.get("passed")), None)
                      or (input_diagnoses[0] if input_diagnoses else {})),
                  "signature": signature, "cached": False}
        revision = optimize_qc_feedback(
            issues, mode="image", readable_text=spec.get("readable_text"),
            diagnostics=report.get("input_diagnosis"))
        report["revision_feedback"] = revision["text"]
        report["revision_categories"] = revision["categories"]
        self.projects.add_episode_cost(episode["id"], cost)
        self._plan_mark(ctx, item["id"], item.get("status", "done"),
                        extra={"qc": report})
        self.log.info(
            "director",
            f"质检 {item['id']}: "
            + ("通过" if report["passed"]
               else "未过 — " + "；".join(report["issues"])))
        return report

    def _auto_repair_qc_item(self, project, episode, ctx, item, report):
        """身份/性别/人数硬错误：以失败图为基底自动修图并立即复检。"""
        if report.get("passed") or not report.get("hard_failure"):
            return report, 0
        repaired = 0
        current = report
        for attempt in range(self._qc_retries()):
            diagnostics = normalize_generation_diagnostics(
                current.get("input_diagnosis") or {},
                issues=current.get("issues"))
            patch = targeted_prompt_patch(diagnostics)
            if not diagnostics.get("diagnosis_complete"):
                current = dict(current)
                current["retry_blocked"] = True
                current["retry_blocked_reason"] = (
                    "质检未完整分析实际提示词和参考图，禁止盲目自动修图")
                self._plan_mark(
                    ctx, item["id"], "awaiting_human",
                    extra={"qc": current})
                break
            if not patch:
                current = dict(current)
                current["retry_blocked"] = True
                current["retry_blocked_reason"] = (
                    "质检没有给出可实际应用的本镜提示词修订；"
                    "参考图调整需人工确认后再生成")
                self._plan_mark(
                    ctx, item["id"], "awaiting_human",
                    extra={"qc": current})
                break
            if self._cancel_requested(ctx):
                raise ProduceCancelled("已手动暂停质检自动修图")
            target = self._plan_item_target(item["id"])
            if target is None:
                break
            # 帧组能定位到首/尾时只修那一张，避免无关帧也被重画。
            if item.get("category") == "frames":
                issue_text = "；".join(current.get("issues") or [])
                has_first = "首帧:" in issue_text
                has_last = "尾帧:" in issue_text
                if has_first != has_last:
                    target = {
                        "kind": ("first_frame" if has_first
                                 else "last_frame"),
                        "shot_no": int(item.get("shot_no")),
                    }
            revision = optimize_qc_feedback(
                current.get("issues") or [], mode="image",
                diagnostics=diagnostics)
            feedback = revision["text"][:1600]
            self.log.warn(
                "director",
                f"{item['id']} 触发自动修图第{attempt + 1}次:"
                + "；".join(current.get("issues") or []))
            self.regen_image(
                project["title"], episode["number"], target,
                feedback=feedback, revision_source="auto_qc_identity")
            repaired += 1
            refreshed = next((
                candidate for candidate in self._plan_read(ctx)["items"]
                if candidate["id"] == item["id"]), item)
            current = self._qc_one(
                project, episode, ctx, refreshed)
            if current.get("passed") or not current.get("hard_failure"):
                break
        current = dict(current)
        current["auto_repaired"] = repaired
        current["auto_repair_exhausted"] = bool(
            repaired and current.get("hard_failure")
            and not current.get("passed"))
        return current, repaired

    def _qc_all_current_contract_parallel(
            self, project, episode, ctx, items, previous_status,
            progress=None):
        """用独立 Codex 登录态并行复检旧图，但不在检查阶段直接重画。"""
        from concurrent.futures import ThreadPoolExecutor, as_completed

        profiles = self._codex_parallel_profiles()
        workers = min(
            max(1, self._parallel_workers()),
            len(profiles) if profiles else 1,
            max(1, len(items)))
        prepared = []
        for item in items:
            uri, _ = self._plan_item_asset(
                project["id"], episode["number"], item)
            if not uri:
                continue
            old_qc = item.get("qc") or {}
            stale_qc = {
                "passed": False,
                "stale": True,
                "stale_reason": "current_storyboard_contract_recheck",
                "issues": ["分镜合同已更新，旧图正在按当前人物、人数和空间图重新质检"],
                "previous_passed": old_qc.get("passed"),
                "previous_signature": old_qc.get("signature", ""),
            }
            self._plan_mark(
                ctx, item["id"], "retrying",
                extra={"qc": stale_qc, "output_uri": uri,
                       "contract_recheck": True})
            prepared.append(item["id"])
        if progress:
            progress(
                phase="checking", total=len(prepared), completed=0,
                checked=0, passed=0, failed=0,
                parallelism=workers,
                note=f"按当前分镜合同复检；Codex {workers} 路并行")

        def run_one(index, item_id):
            from .app import App
            worker_app = App(self.artifacts_root.parent)
            director = worker_app.director
            worker_project, worker_episode = director._episode_ctx(
                project["title"], episode["number"])
            worker_ctx = {
                "project": dict(worker_project),
                "episode": dict(worker_episode),
                "out_root": director._episode_dir(
                    worker_project, worker_episode),
            }
            current = next((
                value for value in director._plan_read(worker_ctx)["items"]
                if value["id"] == item_id), None)
            if current is None:
                worker_app.close()
                return {"item_id": item_id, "error": "清单条目已不存在"}
            profile_id = (
                profiles[index % len(profiles)]["id"] if profiles else "")
            try:
                report = director._qc_one(
                    worker_project, worker_episode, worker_ctx, current,
                    codex_profile=profile_id)
                report = dict(report)
                report["stale"] = False
                report["contract_recheck"] = True
                report["codex_profile"] = profile_id
                director._plan_mark(
                    worker_ctx, item_id,
                    "done" if report.get("passed") else "awaiting_human",
                    extra={"qc": report, "contract_recheck": True})
                result = {
                    "item_id": item_id,
                    "passed": bool(report.get("passed")),
                    "report": report,
                    "codex_profile": profile_id,
                }
            except AifosError as exc:
                report = {
                    "passed": False, "stale": False,
                    "contract_recheck": True,
                    "codex_profile": profile_id,
                    "issues": [f"复检失败:{exc}"],
                }
                director._plan_mark(
                    worker_ctx, item_id, "awaiting_human",
                    error=str(exc)[:300], extra={"qc": report})
                result = {
                    "item_id": item_id, "passed": False,
                    "report": report, "error": str(exc),
                    "codex_profile": profile_id,
                }
            except Exception as exc:  # 单张异常不得中止其余镜头复检
                report = {
                    "passed": False, "stale": False,
                    "contract_recheck": True,
                    "codex_profile": profile_id,
                    "issues": [f"复检异常:{exc}"],
                }
                director._plan_mark(
                    worker_ctx, item_id, "awaiting_human",
                    error=str(exc)[:300], extra={"qc": report})
                result = {
                    "item_id": item_id, "passed": False,
                    "report": report, "error": str(exc),
                    "codex_profile": profile_id,
                }
            finally:
                worker_app.close()
            return result

        checked = passed = failed = 0
        failed_item_ids = []
        if workers == 1:
            results = [
                run_one(index, item_id)
                for index, item_id in enumerate(prepared)]
            iterator = results
        else:
            pool = ThreadPoolExecutor(
                max_workers=workers, thread_name_prefix="aifos-qc")
            futures = {
                pool.submit(run_one, index, item_id): item_id
                for index, item_id in enumerate(prepared)}
            iterator = (future.result()
                        for future in as_completed(futures))
        try:
            for result in iterator:
                checked += 1
                if result.get("passed"):
                    passed += 1
                else:
                    failed += 1
                    failed_item_ids.append(result["item_id"])
                if progress:
                    progress(
                        phase="checking", total=len(prepared),
                        completed=checked, checked=checked, passed=passed,
                        failed=failed, parallelism=workers,
                        current_item=result.get("item_id", ""),
                        codex_profile=result.get("codex_profile", ""))
        finally:
            if workers > 1:
                pool.shutdown(wait=True)
            row = self.projects.get_episode(episode["id"])
            if row and row["status"] in ("cast", "cancelling"):
                self.projects.set_episode_status(
                    episode["id"], previous_status)
        self.log.info(
            "director",
            f"当前分镜合同复检完成:{checked} 张,通过 {passed},"
            f"未过 {failed};Codex {workers} 路")
        return {
            "status": "done", "checked": checked, "passed": passed,
            "failed": failed, "failed_item_ids": sorted(
                failed_item_ids,
                key=lambda value: int(value.split(":")[-1])),
            "auto_repaired": 0, "parallelism": workers,
            "contract_recheck": True,
        }

    def qc_all(self, project_title, episode_number, *,
               include_existing=False, auto_repair=True, parallel=False,
               progress=None, categories=None):
        """批量质检。

        include_existing=True 时，当前清单虽为 pending、但磁盘已有旧图的
        镜头也必须重新检查；auto_repair=False 可先得到完整失败清单，再
        交给双 Codex 通道只重画失败项。
        """
        project, episode = self._episode_ctx(project_title, episode_number)
        ctx = {"project": dict(project), "episode": dict(episode),
               "out_root": self._episode_dir(project, episode)}
        plan = self._plan_read(ctx)
        allowed_categories = set(categories or self.SHOT_QC_CATEGORIES)
        allowed_categories &= self.SHOT_QC_CATEGORIES
        items = []
        for item in plan["items"]:
            if item.get("category") not in allowed_categories:
                continue
            if item.get("status") in ("done", "reused"):
                items.append(item)
                continue
            if include_existing:
                uri, _ = self._plan_item_asset(
                    project["id"], episode["number"], item)
                if uri:
                    items.append(item)
        previous_status = episode["status"]
        self.projects.set_episode_status(episode["id"], "cast")
        if include_existing and parallel and not auto_repair:
            return self._qc_all_current_contract_parallel(
                project, episode, ctx, items, previous_status,
                progress=progress)
        checked = passed = failed = 0
        auto_repaired = 0
        try:
            for item in items:
                if self._cancel_requested(ctx):
                    raise ProduceCancelled("已手动暂停质检")
                try:
                    report = self._qc_one(project, episode, ctx, item)
                    if auto_repair:
                        report, repaired = self._auto_repair_qc_item(
                            project, episode, ctx, item, report)
                        auto_repaired += repaired
                    self._plan_mark(
                        ctx, item["id"],
                        "done" if report.get("passed") else "awaiting_human",
                        extra={"qc": report,
                               "contract_recheck": bool(include_existing)})
                except AifosError as exc:
                    self.log.warn("director",
                                  f"质检 {item['id']} 跳过: {exc}")
                    continue
                checked += 1
                passed += 1 if report["passed"] else 0
                failed += 0 if report["passed"] else 1
        except ProduceCancelled:
            self.projects.set_episode_status(episode["id"], previous_status)
            return {"status": "paused", "checked": checked,
                    "passed": passed, "failed": failed,
                    "auto_repaired": auto_repaired}
        finally:
            row = self.projects.get_episode(episode["id"])
            if row and row["status"] in ("cast", "cancelling"):
                self.projects.set_episode_status(
                    episode["id"], previous_status)
            elif (row and previous_status == "failed"
                  and row["status"] == "awaiting_confirm"
                  and self._preflight_plan_incomplete(ctx)):
                # 自动/批量重画会暂时进入 awaiting_confirm；若本集仍有缺图、
                # 待生产或二次 QC 失败项，必须保持失败/待处理状态，不能
                # 让页面误报“预生产已完成，可以开始 Seedance”。
                self.projects.set_episode_status(episode["id"], "failed")
        self.log.info(
            "director",
            f"批量质检完成:{checked} 张,通过 {passed},未过 {failed},"
            f"自动修图 {auto_repaired} 次")
        return {"status": "done", "checked": checked,
                "passed": passed, "failed": failed,
                "auto_repaired": auto_repaired}

    def redo_items(self, project_title, episode_number, item_ids=None,
                   only_failed=False, quality_override=None, progress=None):
        """批量重画:按 item_ids 重画;only_failed=True 时重画所有质检
        未过的图。可暂停,重画后自动复检。"""
        project, episode = self._episode_ctx(project_title, episode_number)
        ctx = {"project": dict(project), "episode": dict(episode),
               "out_root": self._episode_dir(project, episode)}
        plan = self._plan_read(ctx)
        by_id = {i["id"]: i for i in plan["items"]}
        requested = [
            item for item in plan["items"]
            if item.get("category") in self.SHOT_QC_CATEGORIES
            and (
                (only_failed and (item.get("qc") or {}).get("passed") is False)
                or (not only_failed and item.get("id") in (item_ids or [])))
        ]
        contract_failures = [
            item for item in requested
            if (item.get("qc") or {}).get("contract_repair_required")
            or (item.get("qc") or {}).get(
                "input_contract_passed") is False
        ]
        if len(contract_failures) >= 3:
            note = (
                f"检测到 {len(contract_failures)} 张共享提示词/参考图合同异常；"
                "已在调用生图 API 前熔断。请先修复合同并重新质检，"
                "禁止把系统性输入错误当成逐张画面错误重画。")
            self.log.warn("director", "批量重画熔断:" + note)
            result = {
                "status": "blocked", "redone": 0,
                "reason": "systemic_input_contract_failure",
                "affected": len(contract_failures), "note": note,
            }
            if progress:
                progress(
                    phase="blocked", total=len(requested), completed=0,
                    redone=0, failed=0, note=note)
            return result
        if only_failed:
            targets = [i["id"] for i in plan["items"]
                       if (i.get("qc") or {}).get("passed") is False
                       and (i.get("qc") or {}).get(
                           "redraw_required", True)
                       and i.get("category") in self.SHOT_QC_CATEGORIES
                       and i.get("status") in (
                           "done", "reused", "awaiting_human", "failed")]
        else:
            targets = [tid for tid in (item_ids or []) if tid in by_id]
        if not targets:
            if progress:
                progress(phase="done", total=0, completed=0, redone=0,
                         failed=0, note="没有需要重画的图")
            return {"status": "done", "redone": 0,
                    "note": "没有需要重画的图"}
        if only_failed:
            identity_words = ("人物不一致", "人物形象", "身份", "同一个人",
                              "脸", "发型", "发色", "服装")
            systemic = []
            for item_id in targets:
                item = by_id[item_id]
                text = "；".join((item.get("qc") or {}).get("issues") or [])
                if any(word in text for word in identity_words):
                    systemic.append(item_id)
            if len(systemic) >= 3:
                self.log.warn(
                    "director",
                    f"批量重画熔断:{len(systemic)}张出现同类人物身份问题；"
                    "应先修复/重新选择最终立绘，禁止沿用错误锚点逐张重画")
                result = {
                    "status": "blocked", "redone": 0,
                    "reason": "systemic_identity_failure",
                    "affected": len(systemic),
                    "note": "检测到系统性人物身份/发型/服装漂移，请先回人物定版，避免无效批量重画",
                }
                if progress:
                    progress(phase="blocked", total=len(targets),
                             completed=0, redone=0, failed=0,
                             note=result["note"])
                return result

        previous_status = episode["status"]
        self.projects.set_episode_status(episode["id"], "cast")
        self.log.info("director", f"开始批量重画 {len(targets)} 张")
        total = len(targets)
        redone = failed = checked = qc_passed = qc_failed = processed = 0
        if progress:
            progress(phase="queued", total=total, completed=0, redone=0,
                     failed=0, checked=0, qc_passed=0, qc_failed=0,
                     prompt_policy="auto_revision",
                     reference_policy="auto_attach")
        try:
            # Each worker owns an App/Director so mutable per-task accounting
            # (cost, provider set, context and router state) cannot leak across
            # images.  Profiles are assigned round-robin; with Codex A+B this
            # gives two genuinely independent CLI processes.
            profiles = self._codex_parallel_profiles()
            parallel_workers = self._parallel_workers()
            if profiles:
                parallel_workers = min(parallel_workers, len(profiles))
            parallel_workers = max(1, min(parallel_workers, total))
            storyboard, _ = self.projects.latest_document(
                episode["id"], "storyboard")
            scene_by_shot = {
                int(shot.get("shot_no")): shot.get("scene_no")
                for shot in (storyboard or {}).get("shots", [])
                if shot.get("shot_no") is not None
            }
            scene_locks = {}

            def run_one(index, item_id):
                target = self._plan_item_target(item_id)
                item = by_id[item_id]
                label = item.get("label") or item_id
                if target is None:
                    return {"index": index, "item_id": item_id,
                            "label": label, "failed": True,
                            "error": "无法解析批量重画目标"}
                issues = list((item.get("qc") or {}).get("issues") or [])
                if issues:
                    revision = optimize_qc_feedback(issues, mode="image")
                    feedback = revision["text"][:1600]
                    revision_source = "batch_qc"
                else:
                    feedback = (
                        "批量重新画：生成与上一版明显不同的有效新版本；"
                        "严格保持已锁定的人物身份、服装、场景、文字白名单"
                        "和前后镜头连续性")
                    revision_source = "batch_redraw"
                prompt_override = (item.get("prompt", "")
                                   if item.get("custom_prompt") else "")
                profile_id = ""
                if profiles:
                    profile_id = profiles[(index - 1) % len(profiles)]["id"]
                shot_no = target.get("shot_no")
                scene_no = scene_by_shot.get(int(shot_no)) \
                    if shot_no is not None else None
                # 关键帧彼此是独立静态资产，均使用已锁定人物、当前空间图
                # 和已通过参考图，可以让 A/B 在同一场景真正并行。只有
                # 首尾帧存在“上一镜尾帧→下一镜首帧”的硬依赖，仍按场锁串行。
                lock_key = (
                    ("scene", scene_no)
                    if item.get("category") == "frames"
                    and scene_no is not None
                    else ("item", item_id))
                scene_lock = scene_locks.setdefault(
                    lock_key, threading.Lock())
                worker_app = None
                try:
                    if self._cancel_requested(ctx):
                        return {"index": index, "item_id": item_id,
                                "label": label, "cancelled": True}
                    # Local import avoids the Director ↔ App module cycle at
                    # import time.  All workers use the same workspace/config.
                    from .app import App
                    worker_app = App(self.artifacts_root.parent)
                    worker_director = worker_app.director
                    with scene_lock:
                        worker_director.regen_image(
                            project_title, episode_number, target,
                            feedback=feedback,
                            prompt_override=prompt_override,
                            quality_override=quality_override,
                            revision_source=revision_source,
                            codex_profile=profile_id)
                        worker_project, worker_episode = \
                            worker_director._episode_ctx(
                                project_title, episode_number)
                        worker_ctx = {
                            "project": dict(worker_project),
                            "episode": dict(worker_episode),
                            "out_root": worker_director._episode_dir(
                                worker_project, worker_episode),
                        }
                        refreshed = next(
                            (entry for entry in worker_director._plan_read(
                                worker_ctx)["items"]
                             if entry["id"] == item_id), item)
                        refs = refreshed.get("reference_inputs") or {}
                        report = refreshed.get("qc")
                        try:
                            if not isinstance(report, dict):
                                report = worker_director._qc_one(
                                    worker_project, worker_episode, worker_ctx,
                                    refreshed)
                            qc_error = ""
                        except AifosError as exc:
                            # Match the serial path: a successful redraw still
                            # counts as redone when the follow-up QC provider is
                            # unavailable; the item remains visible for manual
                            # checking instead of being miscounted as a redraw
                            # failure.
                            worker_director.log.warn(
                                "director", f"重画后复检 {item_id} 跳过: {exc}")
                            report = None
                            qc_error = str(exc)
                    return {
                        "index": index, "item_id": item_id, "label": label,
                        "redone": True, "report": report,
                        "qc_error": qc_error,
                        "references_attached": bool(refs.get("attached")),
                        "reference_count": int(refs.get("count") or 0),
                        "codex_profile": profile_id,
                        "feedback": feedback,
                    }
                except ProduceCancelled:
                    return {"index": index, "item_id": item_id,
                            "label": label, "cancelled": True,
                            "codex_profile": profile_id}
                except AifosError as exc:
                    return {"index": index, "item_id": item_id,
                            "label": label, "failed": True,
                            "error": str(exc),
                            "codex_profile": profile_id}
                except Exception as exc:  # defensive: one image must not stop the batch
                    self.log.warn("director", f"重画 {item_id} 失败: {exc}")
                    return {"index": index, "item_id": item_id,
                            "label": label, "failed": True,
                            "error": str(exc),
                            "codex_profile": profile_id}
                finally:
                    if worker_app is not None:
                        worker_app.close()

            if progress:
                progress(phase="redrawing", total=total, completed=0,
                         redone=0, failed=0, checked=0, qc_passed=0,
                         qc_failed=0, parallel_workers=parallel_workers,
                         codex_profiles=[p["id"] for p in profiles],
                         parallel_mode=("codex_profiles" if profiles
                                        else "configured_workers"))
            from concurrent.futures import ThreadPoolExecutor, as_completed
            futures = {}
            with ThreadPoolExecutor(max_workers=parallel_workers) as pool:
                for index, item_id in enumerate(targets, 1):
                    if self._cancel_requested(ctx):
                        raise ProduceCancelled("已手动暂停重画")
                    future = pool.submit(run_one, index, item_id)
                    futures[future] = (index, item_id)
                for future in as_completed(futures):
                    result = future.result()
                    index = result["index"]
                    item_id = result["item_id"]
                    label = result["label"]
                    if result.get("cancelled"):
                        raise ProduceCancelled("已手动暂停重画")
                    if result.get("failed"):
                        failed += 1
                        self.log.warn(
                            "director",
                            f"重画 {item_id} 跳过: {result.get('error', '')}")
                    else:
                        redone += 1
                        report = result.get("report")
                        if report is not None:
                            checked += 1
                            if report.get("passed"):
                                qc_passed += 1
                            else:
                                qc_failed += 1
                    processed += 1
                    if progress:
                        progress(
                            phase=("checking" if not result.get("failed")
                                   else "redrawing"),
                            total=total, completed=processed,
                            current_index=index, current_item=item_id,
                            current_label=label, redone=redone,
                            failed=failed, checked=checked,
                            qc_passed=qc_passed, qc_failed=qc_failed,
                            references_attached=result.get(
                                "references_attached", False),
                            reference_count=result.get("reference_count", 0),
                            codex_profile=result.get("codex_profile", ""),
                            qc_error=result.get("qc_error", ""),
                            prompt_modified=True,
                            revision_note=result.get("feedback", ""),
                            parallel_workers=parallel_workers)
        except ProduceCancelled:
            self.projects.set_episode_status(episode["id"], previous_status)
            if progress:
                progress(phase="paused", total=total, completed=processed,
                         redone=redone, failed=failed, checked=checked,
                         qc_passed=qc_passed, qc_failed=qc_failed)
            return {"status": "paused", "redone": redone,
                    "failed": failed, "checked": checked}
        finally:
            row = self.projects.get_episode(episode["id"])
            if row and row["status"] in ("cast", "cancelling"):
                self.projects.set_episode_status(
                    episode["id"], previous_status)
            elif (row and previous_status == "failed"
                  and row["status"] == "awaiting_confirm"
                  and self._preflight_plan_incomplete(ctx)):
                self.projects.set_episode_status(episode["id"], "failed")
        self.log.info("director", f"批量重画完成:{redone} 张")
        if progress:
            progress(phase="done", total=total, completed=processed,
                     redone=redone, failed=failed, checked=checked,
                     qc_passed=qc_passed, qc_failed=qc_failed,
                     current_item="", current_label="")
        return {"status": "done", "total": total, "redone": redone,
                "failed": failed, "checked": checked,
                "qc_passed": qc_passed, "qc_failed": qc_failed}

    def manual_qc_pass(self, project_title, episode_number, item_ids=None,
                       only_failed=False, note=""):
        """人工放行轻微问题图,保留原始质检问题和覆盖审计记录。

        人工通过不是删除质检结果: ``qc.issues`` 原样保留,同时写入
        ``manual_override``。二次质检失败稿若只有 ``output_uri``,这里会
        将该稿登记为正式镜头资产,这样放行后可以继续断点生产。
        """
        project, episode = self._episode_ctx(project_title, episode_number)
        ctx = {"project": dict(project), "episode": dict(episode),
               "out_root": self._episode_dir(project, episode)}
        plan = self._plan_read(ctx)
        by_id = {item.get("id"): item for item in plan.get("items", [])}
        if only_failed:
            targets = [item.get("id") for item in plan.get("items", [])
                       if item.get("category") in self.SHOT_QC_CATEGORIES
                       and (item.get("qc") or {}).get("passed") is False
                       and item.get("status") in (
                           "done", "reused", "awaiting_human", "failed")]
        else:
            targets = list(dict.fromkeys(
                str(value) for value in (item_ids or [])
                if str(value).strip()))
        targets = [item_id for item_id in targets if item_id in by_id]
        if len(targets) > 200:
            raise AifosError("单次最多人工放行 200 张图片")
        if not targets:
            return {"status": "done", "passed": 0, "skipped": 0,
                    "note": "没有需要人工放行的质检失败图"}

        storyboard, _ = self.projects.latest_document(
            episode["id"], "storyboard")
        ctx["storyboard"] = storyboard or {"shots": []}
        script, _ = self.projects.latest_document(episode["id"], "script")
        ctx["script"] = script or {"scenes": [], "characters": []}
        message = str(note or "人工复核通过：问题不影响本集观感，继续后续生产")
        passed, skipped = 0, 0
        skipped_items = []

        def valid_uri(value):
            value = str(value or "").strip()
            return bool(value) and (
                value.startswith(("http://", "https://"))
                or Path(value).is_file())

        for item_id in targets:
            item = by_id[item_id]
            category = item.get("category")
            if category not in self.SHOT_QC_CATEGORIES:
                skipped += 1
                skipped_items.append({"item_id": item_id,
                                      "reason": "该条目不是镜头图或首尾帧"})
                continue
            qc = dict(item.get("qc") or {})
            if qc.get("passed") is True:
                skipped += 1
                skipped_items.append({"item_id": item_id,
                                      "reason": "该图已经通过质检"})
                continue
            uri, _ = self._plan_item_asset(
                project["id"], episode["number"], item)
            # 二次失败稿没有正式资产时,允许用户明确放行 output_uri;
            # 没有真实文件则拒绝把“通过”写成空壳状态。
            candidate = item.get("output_uri")
            promote_candidate = (
                category == "shot_image"
                and item.get("status") in ("awaiting_human", "failed")
                and valid_uri(candidate))
            if promote_candidate or (not uri and valid_uri(candidate)):
                uri = str(candidate) if category == "shot_image" else uri
                if category == "shot_image":
                    shot = next((value for value in ctx["storyboard"].get(
                        "shots", []) if int(value.get("shot_no", -1))
                        == int(item.get("shot_no"))), None)
                    if shot is None:
                        uri = None
                    else:
                        decision = {
                            "level": item.get("image_quality") or "medium",
                            "recommended": item.get(
                                "recommended_quality")
                            or item.get("image_quality") or "medium",
                            "source": "manual_override",
                            "rule": item.get("quality_rule") or "",
                            "reasons": list(item.get("quality_reasons")
                                            or []),
                        }
                        asset_name = self._shot_name(
                            ctx, int(item["shot_no"]))
                        old_image = self.assets.latest(
                            project["id"], "image", asset_name)
                        new_image = self.assets.register(
                            project["id"], "image",
                            asset_name,
                            uri=uri,
                            meta=self._shot_image_meta(
                                ctx, shot, decision,
                                {"manual_qc_override": True,
                                 "manual_qc_note": message}),
                            new_version=True)
                        if old_image is not None:
                            self.assets.mark_superseded(
                                old_image["id"], new_image["id"],
                                reason="manual_qc_revision")
            if not uri:
                skipped += 1
                skipped_items.append({"item_id": item_id,
                                      "reason": "没有可用图片，不能人工放行"})
                continue
            if category == "frames":
                frame_name = self._shot_name(
                    ctx, int(item.get("shot_no")))
                last = self.assets.latest(
                    project["id"], "last_frame", frame_name)
                if last is None or not valid_uri(last["uri"]):
                    skipped += 1
                    skipped_items.append({
                        "item_id": item_id,
                        "reason": "首尾帧不完整，不能人工放行"})
                    continue
            issues = list(qc.get("issues") or [])
            qc.update({
                "passed": True,
                "manual_override": True,
                "manual_decision": "accepted",
                "manual_note": message[:1200],
                "manual_at": now(),
                "manual_original_issues": issues,
                "awaiting_human": False,
                "auto_repair_exhausted": False,
            })
            item["qc"] = qc
            item["status"] = "done"
            item["error"] = ""
            passed += 1

        plan["updated_at"] = now()
        self._plan_write(ctx, plan)
        self.log.info(
            "director",
            f"人工放行质检问题图:{passed} 张,跳过 {skipped} 张")
        return {"status": "done", "passed": passed, "skipped": skipped,
                "skipped_items": skipped_items,
                "note": message[:1200]}

    def redo_video(self, project_title, episode_number, shot_no, feedback):
        """人工确认问题后重生成指定视频镜头并重新质检。

        这是视频自动返工两次(初版 + 一次自动返工)后的唯一恢复入口。
        人工意见会进入本镜 Seedance 提示词；本方法只重拍指定镜头，不会
        把整集再次当成“自动返工”或重置自动返工计数。
        """
        feedback = str(feedback or "").strip()
        if not feedback:
            raise AifosError("请先填写视频质检问题与修改要求")
        project, episode = self._episode_ctx(project_title, episode_number)
        try:
            requested_shot_no = int(shot_no)
        except (TypeError, ValueError):
            raise AifosError("shot_no 必须是有效镜头编号")
        # API 调用方可能只提交原始质检现象；在进入生成链路前自动补上
        # 最近一次编译好的修订提示词，避免人工提交绕过自动修订规则。
        video_qc_doc, _ = self.projects.latest_document(
            episode["id"], "video_qc_report")
        previous_shot_qc = next(
            (item for item in (video_qc_doc or {}).get("shots", [])
             if int(item.get("shot_no", -1)) == requested_shot_no), None)
        auto_revision = (previous_shot_qc or {}).get("revision_feedback") or ""
        if auto_revision and "【自动优化修订】" not in feedback:
            feedback = (auto_revision + "\n【人工补充】" + feedback)[:2400]
        script, _ = self.projects.latest_document(episode["id"], "script")
        storyboard, _ = self.projects.latest_document(
            episode["id"], "storyboard")
        if not script or not storyboard:
            raise AifosError("本集尚未完成剧本和分镜，不能重生成视频")
        shot_no = requested_shot_no
        shot = next((item for item in storyboard.get("shots", [])
                     if int(item.get("shot_no", -1)) == shot_no), None)
        if shot is None:
            raise AifosError(f"镜头不存在: {shot_no}")
        standard, _ = self.projects.latest_document(
            episode["id"], "production_standard")
        continuity, _ = self.projects.latest_document(
            episode["id"], "continuity")
        blocking, _ = self.projects.latest_document(
            episode["id"], "blocking")
        preflight, _ = self.projects.latest_document(
            episode["id"], "preflight")
        content_review, _ = self.projects.latest_document(
            episode["id"], "content_review")
        profile = production_profile(self.config, standard)
        aspect = project["aspect"] or self.config.get(
            "defaults", "aspect", default="9:16")
        ctx = {
            "project": dict(project), "episode": dict(episode),
            "out_root": self._episode_dir(project, episode),
            "aspect": aspect, "dims": ASPECT_DIMS.get(
                aspect, ASPECT_DIMS["9:16"]), "script": script,
            "storyboard": storyboard, "continuity": continuity or {},
            "blocking": blocking or {}, "preflight": preflight or {},
            "content_review": content_review or {},
            "production_profile": profile,
            "voice_mode": profile.get("voice", "jimeng_builtin"),
            "lip_sync": bool(profile.get("lip_sync", True)),
            "quality_policy": self._episode_quality_policy(episode["id"]),
            "character_asset_policy": self.character_asset_policy(
                episode["id"], script=script),
            "images": [], "frames": [], "videos": [], "voices": [],
            "subtitles": [],
            "video_feedback": {shot_no: feedback}, "force": False,
        }
        for item in storyboard.get("shots", []):
            value = int(item["shot_no"])
            first = self.assets.latest(
                project["id"], "first_frame", self._shot_name(ctx, value))
            last = self.assets.latest(
                project["id"], "last_frame", self._shot_name(ctx, value))
            if first is None or last is None:
                raise AifosError(f"镜头{value}缺少首尾帧，不能重生成视频")
            ctx["frames"].append({"shot_no": value, "first": first["uri"],
                                  "last": last["uri"],
                                  "image_quality": "high"})
            image = self.assets.latest(
                project["id"], "image", self._shot_name(ctx, value))
            if image is not None and image["uri"]:
                ctx["images"].append({"shot_no": value, "uri": image["uri"]})
            row = self.assets.latest(
                project["id"], "video", self._shot_name(ctx, value))
            if row is not None and row["uri"]:
                meta = json.loads(row["meta"] or "{}")
                ctx["videos"].append({
                    "shot_no": value, "uri": row["uri"],
                    "duration": item.get("duration"),
                    "provider": meta.get("provider", ""),
                    "audio_in_video": meta.get("audio_in_video"),
                    "video_quality": meta.get("video_quality", "medium"),
                    "video_resolution": meta.get("video_resolution", "720p"),
                })
        self._task_cost = 0.0
        self._task_providers = set()
        frame_map = {item["shot_no"]: item for item in ctx["frames"]}
        generated = self._make_video(ctx, shot, frame_map)
        replaced = False
        updated_videos = []
        for item in ctx["videos"]:
            if item["shot_no"] == shot_no:
                updated_videos.append(generated)
                replaced = True
            else:
                updated_videos.append(item)
        if not replaced:
            updated_videos.append(generated)
        ctx["videos"] = sorted(
            updated_videos, key=lambda item: int(item["shot_no"]))
        self._stage_edit(ctx)
        ctx["voice_carried"] = self._videos_carry_audio(ctx)
        qc = self._stage_qc(ctx)
        self.projects.set_episode_status(
            episode["id"], "done" if qc.get("passed") else "qc_failed")
        return {"status": "done", "shot_no": shot_no,
                "qc": qc, "video_qc": ctx.get("video_qc_report")}

    def redo_placeholders(self, project_title, episode_number):
        """一键补真:把清单里落到占位产线的图,逐张用真实产线重画。
        可随时暂停,已补好的保留;真实产线仍不可用时保持占位并红标。"""
        project, episode = self._episode_ctx(project_title, episode_number)
        ctx = {"episode": dict(episode),
               "out_root": self._episode_dir(project, episode)}
        plan = self._plan_read(ctx)
        pending = [i for i in plan["items"]
                   if i.get("status") == "done" and i.get("real") is False]
        if not pending:
            return {"status": "done", "redone": 0,
                    "note": "清单里没有占位图"}
        previous_status = episode["status"]
        self.projects.set_episode_status(episode["id"], "cast")
        self.log.info(
            "director", f"开始补画 {len(pending)} 张占位图(真实产线)")
        redone = 0
        try:
            for item in pending:
                head, _, rest = item["id"].partition(":")
                builder = self.PLAN_TARGETS.get(head)
                if builder is None:
                    continue
                try:
                    self.regen_image(project_title, episode_number,
                                     builder(rest.split(":")))
                    redone += 1
                except AifosError as exc:
                    self.log.warn("director",
                                  f"补画 {item['id']} 跳过: {exc}")
        except ProduceCancelled:
            self.projects.set_episode_status(
                episode["id"], previous_status)
            return {"status": "paused", "redone": redone}
        finally:
            row = self.projects.get_episode(episode["id"])
            if row and row["status"] in ("cast", "cancelling"):
                self.projects.set_episode_status(
                    episode["id"], previous_status)
        self.log.info("director", f"占位图补画完成:{redone} 张")
        return {"status": "done", "redone": redone}

    # ---- 参考图管理:上传的参考图会自动进入出图提示(关联角色/场景) ----
    def add_reference(self, project_title, name, file_bytes, ext,
                      attach_to="", note="", reference_role=""):
        """上传带单一用途的参考图。

        attach_to 为空时只有 ``style`` 会自动全局使用；其他无关联图片
        作为手动参考保留，避免污染所有角色与镜头。
        """
        ext = ext.lower()
        magic = self.IMAGE_MAGIC.get(ext)
        if magic is None:
            raise AifosError(f"不支持的图片格式: {ext}(png/jpg/webp/svg)")
        if not file_bytes or not file_bytes.lstrip()[:8].startswith(magic) \
                and not file_bytes.startswith(magic):
            raise AifosError("文件内容与图片格式不符")
        project = self.projects.get_project(project_title)
        if project is None:
            raise AifosError(f"项目不存在: {project_title}")
        name = (name or "").strip() or f"参考图{ext}"
        role = str(reference_role or "").strip().lower()
        aliases = {
            "人物": "identity", "身份": "identity",
            "服装": "wardrobe", "道具": "wardrobe",
            "场景": "scene", "空间": "scene",
            "构图": "composition", "动作": "composition",
            "画风": "style", "风格": "style",
        }
        role = aliases.get(role, role)
        if not role:
            probe = {"name": name, "meta": json.dumps({
                "attach_to": attach_to or "", "note": note or ""})}
            role = self._reference_role(probe)
        if role not in REFERENCE_ROLES:
            raise AifosError(
                "参考图用途必须是 identity/wardrobe/scene/"
                "composition/style/manual")
        if role != "style" and not str(attach_to or "").strip():
            # 无关联人物/场景时无法建立硬映射，只能由用户在具体镜头手选。
            role = "manual"
        safe = "".join(c if c.isalnum() else "_" for c in name)[:40]
        existing = self.assets.latest(project["id"], "reference", name)
        version = (existing["version"] + 1) if existing else 1
        path = (self.artifacts_root / f"p{project['id']:03d}"
                / "references" / f"{safe}_v{version}{ext}")
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(file_bytes)
        self.assets.register(
            project["id"], "reference", name, uri=str(path),
            meta={"attach_to": attach_to or "", "note": note or "",
                  "reference_role": role},
            new_version=existing is not None)
        self.log.info(
            "director", f"已上传参考图「{name}」"
            f"(用途:{role};关联:{attach_to or '手动选择'});"
            "后续只按该用途参考")
        # 参考图为最高标准:关联到已有设定的角色时,立即按参考图的
        # 脸部特征与风格重写该角色的人物设定提示词。服装、构图等
        # 参考绝不能触发改脸，否则上传一张衣服图就会污染角色身份。
        design_refreshed = False
        if attach_to and role == "identity":
            design_refreshed = self._refresh_design_from_reference(
                project, attach_to)
        return {"name": name, "uri": str(path), "reference_role": role,
                "design_refreshed": design_refreshed}

    def _refresh_design_from_reference(self, project, name):
        """按参考图重写角色设定:脸部特征与风格以参考图为最高标准。

        找不到该角色/无编剧产线时静默跳过(不阻断参考图上传本身)。
        """
        row = self.assets.latest(project["id"], "character", name)
        if row is None:
            return False
        meta = self._asset_meta(row)
        references = [
            row["uri"] for row in self._reference_rows(
                project["id"], [name], allowed_roles={"identity"})]
        if not references:
            return False
        try:
            result = self.router.call("script", {
                "character_design": True,
                "project_title": project["title"],
                "style": project["style"] or "",
                "logline": "",
                "characters": [{"name": name,
                                "role": meta.get("role", ""),
                                "reference_images": references}],
            }, self.artifacts_root / f"p{project['id']:03d}" / "designs")
        except Exception as exc:
            self.log.warn(
                "director",
                f"按参考图重写「{name}」设定失败(参考图已保存,"
                f"出图仍会带参考图): {exc}")
            return False
        design = next((d for d in result.data.get("designs", [])
                       if d.get("name") == name), None)
        if not design:
            return False
        self.assets.register(
            project["id"], "character", name,
            meta={**meta, "design": design,
                  "design_from_reference": True}, new_version=True)
        self.log.info(
            "director",
            f"已按参考图重写「{name}」人物设定(脸部特征与风格以参考图"
            "为最高标准);已有形象可用「换风格重画/批量重画」按新设定重出")
        return True

    def delete_reference(self, project_title, name):
        project = self.projects.get_project(project_title)
        if project is None:
            raise AifosError(f"项目不存在: {project_title}")
        row = self.assets.latest(project["id"], "reference", name)
        if row is None or self.assets.is_deleted(row):
            raise AifosError(f"参考图不存在: {name}")
        self.assets.delete(project["id"], "reference", name)
        self.log.info("director", f"已删除参考图「{name}」")
        return {"deleted": name, "history_preserved": True}

    def delete_image_asset(self, project_title, asset_id):
        """资产中心删除已生产图：隐藏当前版本并安全作废下游产物。"""
        project = self.projects.get_project(project_title)
        if project is None:
            raise AifosError(f"项目不存在: {project_title}")
        row = self.assets.get(int(asset_id))
        if row is None or row["project_id"] != project["id"]:
            raise AifosError("图片资产不存在或不属于本项目")
        latest = self.assets.latest(project["id"], row["kind"], row["name"])
        if (latest is None or latest["id"] != row["id"]
                or self.assets.is_deleted(row)):
            raise AifosError("图片资产已删除或不是当前版本")
        if row["kind"] not in IMAGE_ASSET_KINDS - {"reference"}:
            raise AifosError(f"该资产不是可删除图片: {row['kind']}")
        self.assets.soft_delete(
            project["id"], row["kind"], row["name"],
            meta={"deleted_by": "asset_center"})
        invalidated = []

        def invalidate(kind, name):
            current = self.assets.latest(project["id"], kind, name)
            if current is not None and not self.assets.is_deleted(current):
                self.assets.soft_delete(
                    project["id"], kind, name,
                    meta={"invalidated_by_asset_id": row["id"]})
                invalidated.append(kind)

        if row["kind"] == "character_art":
            identity = self.assets.latest(
                project["id"], "character_identity", row["name"])
            if (identity is not None and identity["uri"] == row["uri"]
                    and not self.assets.is_deleted(identity)):
                invalidate("character_identity", row["name"])
        if row["kind"] == "image":
            for kind in ("first_frame", "last_frame", "video"):
                invalidate(kind, row["name"])
        elif row["kind"] in ("first_frame", "last_frame"):
            invalidate("video", row["name"])
        self.log.info(
            "director", f"资产中心已删除图片 {row['kind']}/{row['name']}"
            f"（历史保留；作废下游:{'、'.join(invalidated) or '无'}）")
        return {
            "deleted": {"asset_id": row["id"], "kind": row["kind"],
                        "name": row["name"]},
            "invalidated": invalidated,
            "history_preserved": True,
        }

    def import_video(self, project_title, episode_number, shot_no,
                     file_bytes, ext=".mp4"):
        """上传替换镜头视频(人工剪辑后的成片)。"""
        if ext.lower() != ".mp4":
            raise AifosError("视频仅支持 mp4")
        if b"ftyp" not in file_bytes[:32]:
            raise AifosError("文件内容不是合法 mp4")
        project, episode = self._episode_ctx(project_title, episode_number)
        out_root = self._episode_dir(project, episode)
        asset_name = f"e{episode['number']:03d}_shot{shot_no:03d}"
        path = out_root / "videos" / f"shot_{shot_no:03d}.upload.mp4"
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(file_bytes)
        self.assets.register(project["id"], "video", asset_name,
                             uri=str(path), meta={"uploaded": True},
                             new_version=True)
        self.log.info("director", f"已上传替换镜头{shot_no}视频")
        return {"uri": str(path)}
