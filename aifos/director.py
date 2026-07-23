"""AI 导演中心:总控。拆解任务、调度 Provider、控制流程与成本。

生产流程(SK 漫剧工业流):
  需求 → 剧本 → 连续性圣经 → 五维分镜 → 空间调度图 → 关键帧/文字锁定 → 首尾帧
       → 生产门禁 → Seedance2 视频(随视频声音/口型) → 剪辑
       → 抽帧检查板 + 内容复核 + 交付脚本 → 包装 → 数据沉淀
"""

import hashlib
import json
import re
import time
from pathlib import Path

from .adapters.claude_script import (is_background_role,
                                     normalize_script_bible,
                                     validate_script_bible)
from .db import now
from .errors import (AifosError, BudgetExceeded, ProduceCancelled,
                     ProviderError, ProviderUnavailable)
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
from .relations import relation_lines, write_relations
from .spatial_blocking import (build_spatial_plan, shot_blocking,
                               write_spatial_svgs)
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
    ("script", "剧本"),
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


def character_candidate_target(character):
    """按人物重要度返回候选张数，防止非主要角色消耗五张额度。"""
    role = str((character or {}).get("role") or "").strip().lower()
    if is_background_character(character):
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
            "跑龙套/背景路人不做独立设定、不生成候选图或立绘")

# 人物定版不是“同一套造型换几个动作”。候选分别承担不同的选角方向，
# 但都受角色年龄、职业、物种、时代和项目画风约束。候选被人工锁定后，
# 其完整脸、发型、妆容与服装才成为后续镜头不可漂移的身份锚点。
CHARACTER_LOOK_VARIANTS = (
    {
        "variant_id": "story_baseline",
        "variant_label": "角色本色",
        "look_variant": {
            "hair": "采用剧本人物设定中的基准发型",
            "makeup": "采用剧本人物设定中的基准妆容或面部修饰",
            "costume": "采用剧本人物设定中的基准服装与配色",
            "temperament": "准确呈现剧本设定的核心性格与气质",
        },
    },
    {
        "variant_id": "clean_minimal",
        "variant_label": "清爽极简",
        "look_variant": {
            "hair": "相对基准明显改变梳法和轮廓，整洁、轻盈、露出更多面部",
            "makeup": "低饱和清透妆或自然克制修饰，保留真实肤质",
            "costume": "简洁轻量、符合角色时代的层次与明亮中性色，轮廓不得复刻基准服装",
            "temperament": "清爽、亲近、可信赖，表演自然不刻意",
        },
    },
    {
        "variant_id": "sharp_professional",
        "variant_label": "干练正式",
        "look_variant": {
            "hair": "相对基准改为利落收束、侧分或更有结构的正式轮廓",
            "makeup": "眉眼与面部轮廓更清晰的正式妆或精致理容，克制不过浓",
            "costume": "挺括、有结构感的正式造型与更深配色，符合角色身份和时代",
            "temperament": "冷静、专业、有掌控力，站姿更坚定",
        },
    },
    {
        "variant_id": "soft_relaxed",
        "variant_label": "松弛亲和",
        "look_variant": {
            "hair": "相对基准改为自然松散、柔软纹理或轻微碎发的生活化轮廓",
            "makeup": "暖调轻透妆或柔和自然修饰，降低攻击性",
            "costume": "柔软面料、舒展层次和温暖配色的日常造型，不复刻其他候选",
            "temperament": "温暖、松弛、有生活气和亲近感",
        },
    },
    {
        "variant_id": "signature_statement",
        "variant_label": "高辨识造型",
        "look_variant": {
            "hair": "在剧情允许范围内采用最有记忆点的轮廓、编束或非对称梳法",
            "makeup": "强化一个清晰重点的镜头妆或面部修饰，精致但不舞台化过度",
            "costume": "用标志性剪裁、材质或强调色建立角色记忆点，仍符合身份与时代",
            "temperament": "更强的镜头存在感和角色辨识度，不改变核心性格",
        },
    },
)

# 人物完整资产套件:立绘之外每个角色补齐的生产级设定资产
# (项目级,跨集复用;全部以立绘和用户参考图为基准保证同一形象)
CHARACTER_SHEETS = [
    ("turnaround", "四视图",
     "标准四视图设定:正面/侧面/背面/四分之三视角并排一张图,"
     "全身等比例,发型服装配色完全一致"),
    ("closeup", "面部特写",
     "面部大特写:五官、发际线、瞳色细节清晰,中性表情"),
    ("features", "特征设定",
     "辨识特征拆解:发型、瞳色、体态、标志性配饰逐项放大标注"),
    ("makeup", "妆容设定",
     "妆容细节:底妆、眉眼妆、唇色、特殊纹样,正面半身"),
    ("costume", "服装设定",
     "全身服装设定:正面站姿,服装配色、材质与层次清晰完整"),
    ("costume_detail", "服装细节",
     "服装细节拆解:纹样、扣饰、腰带、鞋履、佩饰逐项放大展示"),
]

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
}


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
                kind=None, feedback="", run_id=None):
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
        visual_style = (requested_style
                        or stored_style
                        or infer_visual_style(premise, project_title))
        project, created = self.projects.get_or_create_project(
            project_title, style=visual_style,
            kind=kind if kind in ("drama", "idol") else "drama")
        updates = {}
        if (not created and kind in ("drama", "idol")
                and project["kind"] != kind):
            updates["kind"] = kind
        if (not created and visual_style
                and (requested_style or not project["style"])
                and project["style"] != visual_style):
            updates["style"] = visual_style
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
                       "costume_direction"),
        "closeup": ("species", "appearance", "hair", "eyes",
                    "temperament", "background_prompt"),
        "features": ("signature", "appearance", "hair", "palette",
                      "signature_props"),
        "makeup": ("makeup", "eyes", "palette", "personality"),
        "costume": ("costume", "palette", "accessories",
                     "occupation", "costume_direction"),
        "costume_detail": ("costume", "costume_detail", "palette",
                            "accessories", "signature_props",
                            "visual_variants"),
    }
    DESIGN_LABELS = (
        ("species", "形态"),
        ("appearance", "外貌"), ("hair", "发型"), ("eyes", "眼睛"),
        ("temperament", "气质"), ("personality", "性格"),
        ("makeup", "妆容"), ("costume", "服装"),
        ("costume_detail", "服装细节"), ("accessories", "配饰"),
        ("palette", "配色"), ("signature", "标志特征"),
        ("background_prompt", "人物背景提示词"),
        ("era_setting", "时代/世界观"), ("occupation", "职业身份"),
        ("motivation", "核心动机"), ("backstory", "人物经历"),
        ("relationships", "人物关系"),
        ("costume_direction", "服装设计逻辑"),
        ("signature_props", "标志道具"),
        ("visual_variants", "剧情造型方案"))

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
            value = self._design_value(design.get(key))
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
        """风格基准图:锚角色的最新立绘;全项目所有形象向它对齐。
        exclude_name=锚角色自己画立绘时不引用自己。"""
        anchor = self._anchor_character(project_id)
        if not anchor or anchor == exclude_name:
            return None
        row = self._locked_identity(project_id, anchor)
        if row and row["uri"] and Path(row["uri"]).exists():
            return row["uri"]
        return None

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
        """套件重画时复用已完成的其他套件,避免服装/配饰各自漂移。"""
        uris = []
        for key in ("turnaround", "costume", "costume_detail",
                    "features", "makeup"):
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
            "species", "personality", "temperament", "costume",
            "costume_detail", "palette", "background_prompt",
            "era_setting",
            "occupation", "motivation", "backstory", "relationships",
            "costume_direction", "signature_props",
        )
        return {key: design.get(key) for key in keys if design.get(key)}

    def _portrait_prompt(self, name, role, style, design=None):
        detail = self._design_line(
            design, keys=("species", "appearance", "hair", "eyes",
                          "makeup", "accessories", "signature",
                          "temperament", "personality", "costume",
                          "palette", "background_prompt", "era_setting",
                          "occupation", "motivation", "costume_direction",
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
        """返回真实写入提示词和资产元数据的候选造型方向。"""
        template = CHARACTER_LOOK_VARIANTS[index - 1]
        look = dict(template["look_variant"])
        if index == 1 and design:
            look.update({
                "hair": str(design.get("hair") or look["hair"]),
                "makeup": str(design.get("makeup") or look["makeup"]),
                "costume": ";".join(filter(None, (
                    str(design.get("costume") or "").strip(),
                    str(design.get("costume_detail") or "").strip(),
                ))) or look["costume"],
                "temperament": str(
                    design.get("temperament") or look["temperament"]),
            })
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
        """定角候选只锁角色边界，显式放开尚未定版的造型变量。"""
        identity = self._design_line(
            design, keys=("species", "appearance", "eyes", "personality",
                          "background_prompt", "era_setting", "occupation",
                          "motivation", "costume_direction",
                          "signature_props"))
        look = variant["look_variant"]
        if has_reference:
            hair = "严格保持参考图发型轮廓、发际线和发色,不得改发型"
            makeup = "严格保持参考图妆容与面部身份特征,不得改脸"
            variant_rule = (
                "有参考图时,人物脸和发型是最高标准;本套候选只允许在服装、"
                "服装配色、姿态和表情上做剧情化变化,不得改变脸和发型")
        else:
            hair = look["hair"]
            makeup = look["makeup"]
            variant_rule = (
                "无参考图时,可按本套造型方向变化脸部细节、发型和妆容,"
                "但不得改变年龄、物种和核心人物气质")
        return (
            f"角色定角候选:{name}({role}),{style}"
            + (f";角色不可越界特征:{identity}" if identity else "")
            + ";这是用于选择最终人物形象的互斥造型候选之一，不是同一套衣服只换动作"
            f";本套造型方向:{variant['variant_label']}"
            f";发型:{hair};妆容或面部修饰:{makeup}"
            f";服装:{look['costume']};气质:{look['temperament']}"
            + (f";剧情场合:{variant.get('story_variant', {}).get('occasion') or variant.get('story_variant', {}).get('scene')}"
               if variant.get("story_variant", {}).get("occasion")
               or variant.get("story_variant", {}).get("scene") else "")
            + (f";配饰/道具:{variant.get('story_variant', {}).get('props') or variant.get('story_variant', {}).get('accessories')}"
               if variant.get("story_variant", {}).get("props")
               or variant.get("story_variant", {}).get("accessories") else "")
            + ";服装轮廓、妆容强度和外显气质必须与其他候选明显不同,"
            + "但必须适配角色的性别表达、年龄、职业、物种、时代背景、人物背景和项目画风;"
            + "造型变化优先体现剧情场合与人物性格，不得套用现代都市默认模板"
            f";{variant_rule};{WORKWEAR_RULE}{CHARACTER_BACKGROUND_RULE}"
            ";全身正面自然站姿，动作只服务造型展示；干净均匀肤质，禁止塑料脸、"
            "脏污毛孔；单人，禁止新增人物")

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
        action = (scene.get("action") or premise or
                  "建立本场事件发生所需的环境关系")
        return ";".join((
            f"场景概念图/环境基准:{place.strip()}",
            f"项目视觉媒介与材质:{self._scene_style_line(style)}",
            f"空间功能与布局:{self._scene_environment_line(place)}",
            f"时间与天气:{time_state}",
            f"剧情用途:{action}",
            "构图:适配项目画幅的环境建立镜头，前景/主体区/背景层次清楚，"
            "留出角色进出与表演动线，机位高度和光线方向稳定，后续镜头可复用",
            "空镜:画面中不出现人物、人体局部、剪影、倒影中的人或随机路人",
            "场景只保留与剧情有关的设备、道具和陈设；所有屏幕、纸张、招牌和包装"
            "均无可读文字、字幕、Logo、水印、乱码和品牌标识"))

    def _sheet_prompt(self, name, role, style, label, desc, key=None,
                      design=None, locked_look=None):
        detail = self._design_line(
            design, keys=self.SHEET_DESIGN_KEYS.get(key))
        anchor = self._locked_look_line(locked_look)
        return (f"角色{label}:{name}({role}),{style},{desc}"
                + (f";人物设定:{detail}" if detail else "")
                + (f";已锁定服装锚点:{anchor};配饰有则必须保留,没有则不得新增"
                   if anchor else "")
                + ";本套资产的造型必须服从人物背景、时代/世界观、职业、性格和剧情场合，"
                "不得把服装统一成现代都市模板;"
                + f";与立绘同一人物、同一发型服装配色,严格保持形象一致;"
                f"{WORKWEAR_RULE}{CHARACTER_BACKGROUND_RULE}")

    def _plan_path(self, ctx):
        return ctx["out_root"] / "render_plan.json"

    def _plan_read(self, ctx):
        path = self._plan_path(ctx)
        if path.exists():
            try:
                return json.loads(path.read_text(encoding="utf-8"))
            except ValueError:
                pass
        return {"items": []}

    def _plan_write(self, ctx, plan):
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
            if prev is not None:
                item["status"] = prev.get("status", "pending")
                item["error"] = prev.get("error", "")
                for key in ("provider", "model", "real", "fallbacks",
                            "image_task_class", "image_quality", "unit_cost",
                            "qc", "started_at", "finished_at", "duration",
                            "reference_inputs", "revision"):
                    if key in prev:
                        item[key] = prev[key]
                if prev.get("custom_prompt"):
                    item["prompt"] = prev.get("prompt", item["prompt"])
                    item["custom_prompt"] = True
        plan["items"] = rest + items
        self._plan_write(ctx, plan)

    def _plan_mark(self, ctx, item_id, status, error="", prompt=None,
                   only_pending=False, extra=None):
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
            elif status in ("done", "failed") and item.get("started_at"):
                item["finished_at"] = round(time.time(), 1)
                item["duration"] = round(
                    item["finished_at"] - item["started_at"], 1)
            if prompt is not None and prompt != item.get("prompt"):
                item["prompt"] = prompt
                item["custom_prompt"] = True
            if extra:
                item.update(extra)
            self._plan_write(ctx, plan)
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
            })

        for ref in payload.get("identity_references") or []:
            if isinstance(ref, dict):
                add("identity", ref.get("uri"),
                    f"{ref.get('character', '角色')}最终立绘")
        for uri in payload.get("character_refs") or []:
            add("character", uri, "人物设定/资产图")
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

    def _build_dispatch_contract(self, task, item):
        """从即将交给 worker 的真实 payload 构造不可变预检契约。"""
        payload = json.loads(json.dumps(
            task.get("payload") or {}, ensure_ascii=False, default=str))
        prompt = self._prompt_with_feedback(
            payload.get("prompt", ""), payload.get("feedback", ""))
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
        if not prompt.strip():
            issues.append("最终提示词为空")
        subject = str(payload.get("art_name") or payload.get("location") or "")
        expected_names = characters or ([subject] if subject else [])
        for name in expected_names:
            if name and name not in prompt:
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
        } for ref in refs["items"]]
        contract = {
            "schema": "aifos.image-dispatch/v1",
            "item_id": task["item_id"],
            "label": item.get("label", task["item_id"]),
            "category": category,
            "capability": task["capability"],
            "prompt": prompt,
            "prompt_hash": self._stable_hash(prompt),
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
        feedback = (payload or {}).get("feedback", "")
        self._plan_mark(ctx, item_id, "generating", prompt=prompt,
                        extra={
                            "qc": None,
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

    def _qc_spec(self, project_id, characters, location="", action="",
                 forbid=None, require_identity=True):
        """视觉质检规格：待检图必须与人工锁定的最终立绘逐人比对。"""
        identity_refs = self._identity_references(
            project_id, characters,
            required=bool(characters and require_identity))
        designs = []
        for name in characters:
            design = self._reference_safe_design(
                self._character_design(project_id, name))
            line = self._design_line(design, keys=(
                "species", "costume", "era_setting", "occupation",
                "costume_direction", "signature_props")) if design else ""
            designs.append(f"{name}({line})" if line else name)
        identity_required = bool(characters and require_identity)
        return {
            "characters": list(characters),
            "count": len(characters),
            "designs": ";".join(designs),
            "location": location or "",
            "action": action or "",
            "forbid": list(forbid or []),
            "identity_references": identity_refs,
            "identity_required": identity_required,
            # 性别/性别表达是身份的一部分，但单独设硬门槛，避免模型只写
            # identity_checked=true 却漏掉女角被画成男性。
            "gender_required": identity_required,
            "static_frame": True,
        }

    def _generate_image_with_qc(self, capability, payload, out_dir,
                                cancel, qc_spec):
        """出图 + 视觉质检 + 不合格自动重画(worker 线程安全:只调产线)。
        质检产线不可用/出错时放行不阻塞;结果附在 result.qc。"""
        attempts = 0
        spent = 0.0
        while True:
            result = self.router.call(capability, payload, out_dir,
                                      cancel=cancel)
            result.cost += spent
            if not qc_spec or not self._image_qc_enabled():
                return result
            uri = result.uri
            if not uri or not Path(uri).exists():
                return result
            try:
                qc_result = self.router.call(
                    "image_qc", {**qc_spec, "image_uri": uri}, out_dir,
                    cancel=cancel)
            except (ProviderUnavailable, ProviderError) as exc:
                # 人物镜头不能在质检故障时静默放行。保留已生成图片供人工
                # 查看，但明确标成质检未过，后续不得当作正式参考图。
                result.qc = {
                    "passed": False,
                    "issues": [f"质检产线不可用，图片未放行:{exc}"],
                    "attempts": attempts + 1,
                    "identity_checked": False,
                    "gender_checked": False,
                    "gender_match": False,
                    "identity_references": len(
                        qc_spec.get("identity_references") or []),
                }
                return result
            result.cost += qc_result.cost
            verdict = qc_result.data or {}
            identity_checked = (not qc_spec.get("identity_required")
                                or bool(verdict.get("identity_checked")))
            # 兼容尚未升级的视觉质检 provider:只要它没有声明新字段,
            # 沿用旧的 identity_checked 合同;一旦声明性别字段,则严格执行
            # 两个性别门槛。这样不会把旧 provider 的历史结果误判为性别失败,
            # 但新 provider 不能漏报或绕过性别核对。
            gender_declared = bool({"gender_checked", "gender_match"}
                                   & set(verdict))
            gender_checked = (not qc_spec.get("gender_required")
                              or not gender_declared
                              or bool(verdict.get("gender_checked")))
            gender_match = (not qc_spec.get("gender_required")
                            or not gender_declared
                            or bool(verdict.get("gender_match")))
            issues = list(verdict.get("issues") or [])
            if not identity_checked:
                issues.append("质检未确认已逐人比对最终立绘")
            if not gender_checked:
                issues.append("质检未单独核对人物性别/性别表达")
            elif not gender_match:
                issues.append("人物性别/性别表达与锁定最终立绘不一致")
            report = {"passed": bool(verdict.get("pass"))
                      and identity_checked and gender_checked and gender_match,
                      "issues": issues,
                      "attempts": attempts + 1,
                      "identity_checked": identity_checked,
                      "gender_checked": gender_checked,
                      "gender_match": gender_match,
                      "identity_references": len(
                          qc_spec.get("identity_references") or [])}
            result.qc = report
            if report["passed"] or attempts >= self._qc_retries():
                return result
            spent = result.cost
            attempts += 1
            payload = dict(payload)
            payload["feedback"] = ((payload.get("feedback") or "")
                                   + ";图片质检不通过,必须修正:"
                                   + "；".join(report["issues"]))[:800]
            payload["qc_attempt"] = attempts

    def _plan_done_extra(self, result):
        extra = {"provider": result.provider,
                 "real": result.provider != "mock",
                 "fallbacks": getattr(result, "fallbacks", [])}
        data = getattr(result, "data", {}) or {}
        for key in ("first_source", "generation_calls", "model",
                    "image_task_class", "image_quality", "unit_cost"):
            if key in data:
                extra[key] = data[key]
        model = getattr(result, "model", "")
        if model and "model" not in extra:
            extra["model"] = model
        qc = getattr(result, "qc", None)
        if qc is not None:
            extra["qc"] = qc
        return extra

    def _run_one_task(self, ctx, task):
        """串行执行单个出图任务(含质检),记账并更新清单。"""
        if self._cancel_requested(ctx):
            raise ProduceCancelled("已手动停止生成")
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
        self._task_cost += result.cost
        self._task_providers.add(result.provider)
        self.projects.add_episode_cost(ctx["episode"]["id"], result.cost)
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

    def _run_parallel(self, ctx, tasks, line="出图产线"):
        """有界并行出图:只把 worker 数量的任务标为生成中。

        多人/文字/场首等高风险镜头可通过 priority 提前；尚未真正开工的
        条目保持 pending，因此计时与暂停后的恢复都反映真实状态。
        worker 线程只做产线调用;记账/资产登记/清单状态全在主线程。
        tasks: [{"item_id","capability","payload","sub_dir","tag","priority"}]
        返回 {tag: ProviderResult};暂停时未完成条目回到排队并保留已完成。"""
        if not tasks:
            return {}
        self._prepare_dispatch_contracts(ctx, tasks)
        tasks = sorted(tasks, key=lambda task: (
            -int(task.get("priority", 0)), str(task.get("item_id", ""))))
        workers = self._parallel_workers()
        if workers == 1 or len(tasks) == 1:
            out = {}
            for task in tasks:
                out[task["tag"]] = self._run_one_task(ctx, task)
            return out
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
        results, failures = {}, []
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
                if task.get("_acceleration"):
                    generating_extra["acceleration"] = task["_acceleration"]
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
                    self._task_cost += result.cost
                    self._task_providers.add(result.provider)
                    self.projects.add_episode_cost(
                        ctx["episode"]["id"], result.cost)
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
                + (45 if text.get("required") else 0)
                + (25 if scene_first else 0)
                + (15 if any(word in camera for word in
                             ("跟", "移", "摇", "环绕")) else 0)
                + (10 if any(word in action for word in
                             ("走", "跑", "进入", "离开", "追")) else 0))

    def _plan_seed_shots(self, ctx):
        """分镜确定后,把每个镜头的关键帧与首尾帧登记进清单。
        清单里展示的是详细提示词(含人物设定与故事情境),所见即所得。"""
        shots = (ctx.get("storyboard") or {}).get("shots") or []
        locations = self._scene_locations(ctx) if ctx.get("script") else {}
        scene_counts = {}
        for shot in shots:
            scene_counts[shot.get("scene_no")] = (
                scene_counts.get(shot.get("scene_no"), 0) + 1)
        image_items, frame_items = [], []
        for shot in shots:
            shot_no = shot["shot_no"]
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
                "prompt": self._rich_shot_prompt(
                    ctx, shot, locations.get(shot.get("scene_no"), "")),
                **self._quality_meta(image_quality),
            })
            frame_items.append({
                "id": f"frames:{shot_no}", "category": "frames",
                "label": f"镜头 {shot_no:02d} 首尾帧",
                "shot_no": shot_no,
                "prompt": shot.get("seedance_prompt", shot["prompt"]),
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
        normalize_script_bible(script, {
            "project_title": project_title or script.get("project_title", ""),
            "premise": premise,
            "style": style,
        })
        error = validate_script_bible(script)
        if error:
            raise AifosError(f"剧本世界观/人物设定门禁失败: {error}")
        return script

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
            version = self.projects.save_document(
                episode["id"], "script", provided)
            ctx["script"] = provided
            ctx["script_version"] = version
            # 用户自己写的剧本不需要再过目 → 不触发剧本确认暂停
            self.log.info("director", f"使用用户自带剧本(v{version}),"
                          "人物/分镜将自动推导")
            return {"version": version, "provided": True,
                    "scenes": len(provided["scenes"])}
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
                if json.dumps(
                        existing, ensure_ascii=False, sort_keys=True) != before:
                    version = self.projects.save_document(
                        episode["id"], "script", existing)
                    self.log.info(
                        "director",
                        f"已有剧本已补齐故事世界、前情与人物设定(v{version})")
                ctx["script"] = existing
                ctx["script_version"] = version
                self.log.info("director", f"复用已有剧本 v{version}")
                return {"version": version, "reused": True,
                        "scenes": len(existing["scenes"])}
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
        version = self.projects.save_document(episode["id"], "script", script)
        ctx["script"] = script
        ctx["script_version"] = version
        ctx["script_is_new"] = True     # 新写的剧本 → 触发剧本确认暂停
        self.data.record(
            "prompt", "success", prompt=f"script:{ctx['project']['title']}"
            f":e{episode['number']}", uri=result.uri,
            meta={"version": version}, episode_id=episode["id"])
        return {"version": version, "scenes": len(script["scenes"])}

    def _stage_continuity(self, ctx):
        """项目角色/场景/文字规则与生产配置的单集快照。"""
        if not ctx.get("force"):
            existing, version = self.projects.latest_document(
                ctx["episode"]["id"], "continuity")
            if (existing is not None
                    and existing.get("pipeline_version") == PIPELINE_VERSION
                    and existing.get("script_version") == ctx.get(
                        "script_version")
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
        """五维分镜 → 确定性俯视空间图，不消耗任何出图额度。"""
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
        if not reused:
            version = self.projects.save_document(
                ctx["episode"]["id"], "blocking", blocking)
        ctx["blocking"] = blocking
        return {
            "version": version,
            "reused": reused,
            "scenes": len(blocking.get("scenes", [])),
            "required_scenes": blocking.get("summary", {}).get(
                "required_scenes", 0),
            "shots": blocking.get("summary", {}).get("shots", 0),
            "svgs": len(paths),
            "passed": blocking.get("validation", {}).get("passed", False),
        }

    def _ensure_character_designs(self, ctx, characters):
        """人物设定:编剧 AI 为每个角色写性格/外貌/妆容/服装细节。
        项目级一次生成(存 character 资产 meta),跨集复用保证形象一致;
        缺谁补谁,占位产线也会给出具体可画的设定。"""
        project_id = ctx["project"]["id"]
        designs, missing = {}, []
        for character in characters:
            name = character["name"]
            design = self._character_design(project_id, name)
            if design:
                designs[name] = design
            else:
                missing.append(character)
        if not missing:
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
            # 即使编剧模型只回传了基础视觉字段,也不允许丢掉剧本中的
            # 时代/职业/动机/服装逻辑;这些字段会继续进入所有出图提示词。
            for key in (
                    "introduction", "gender", "age_range", "identity",
                    "personality",
                    "background_prompt", "era_setting", "occupation",
                    "motivation", "backstory", "relationships",
                    "costume_direction", "signature_props",
                    "visual_variants"):
                if not design.get(key) and character.get(key):
                    design[key] = character[key]
            self.assets.register(
                project_id, "character", name,
                meta={"role": character.get("role", ""),
                      "design": design}, new_version=True)
            designs[name] = design
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
            refs = self._reference_uris(project_id, [name])
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
                    prompt = "历史候选未记录独立造型方向，请按当前角色重要度规则重新生成"
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
                        "character_background": designs.get(name) or {},
                        "reference_images": refs,
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
        project_id = ctx["project"]["id"]
        style = ctx["project"]["style"] or DEFAULT_VISUAL_STYLE
        all_characters = ctx["script"].get("characters", [])
        characters = [c for c in all_characters
                      if not is_background_character(c)]
        locations = []
        scene_context_by_location = {}
        for scene in ctx["script"]["scenes"]:
            if scene["location"] not in locations:
                locations.append(scene["location"])
                scene_context_by_location[scene["location"]] = dict(scene)
            elif scene.get("action"):
                previous = scene_context_by_location[scene["location"]]
                previous["action"] = "；".join(filter(None, (
                    previous.get("action", ""), scene.get("action", ""))))
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
                    "reference_images": self._reference_uris(
                        project_id, [location]),
                    "style_ref": self._style_anchor_uri(project_id),
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
            reference = self._reference_uris(project_id, [name])
            for key, label, desc in sheet_definitions:
                asset_name = f"{name}:{key}"
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
                        "character_background": designs.get(name) or {},
                        "character_refs": (
                            [portrait_uri] if portrait_uri else []),
                        "identity_references": self._identity_references(
                            project_id, [name]),
                        "require_reference_images": True,
                        "reference_images": reference,
                        "style_ref": self._style_anchor_uri(project_id),
                        "aspect": ctx["aspect"], **ctx["dims"],
                    }), "sub_dir": "cast",
                    "tag": (name, key, label),
                    # 人物资产套件仍属于初始母资产阶段，不做视觉 QC；
                    # 分镜/首尾帧等后续镜头才使用已锁定人物立绘质检。
                })
        for (name, key, label), result in self._run_parallel(
                ctx, tasks, line="人物资产套件").items():
            self.assets.register(
                project_id, "character_sheet", f"{name}:{key}",
                uri=result.uri,
                meta={"character": name, "sheet": key, "label": label,
                      "image_quality": "high",
                      "recommended_quality": "high",
                      "quality_source": "auto",
                      "quality_rule": "mother_asset"})
            created += 1
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
        """只取明确关联到该角色的参考图(全局参考不用于定义单人长相)。"""
        uris = []
        for row in self.assets.active_list(project_id, kind="reference"):
            meta = self._asset_meta(row)
            if meta.get("attach_to") != name:
                continue
            if row["uri"] and Path(row["uri"]).exists():
                uris.append(row["uri"])
        return uris

    def _reference_uris(self, project_id, attach_names):
        """用户上传的参考图:关联到指定角色/场景的 + 全局的。"""
        uris = []
        for row in self.assets.active_list(project_id, kind="reference"):
            meta = json.loads(row["meta"] or "{}") if isinstance(
                row["meta"], str) else (row["meta"] or {})
            attach = meta.get("attach_to", "")
            if attach and attach not in (attach_names or []):
                continue
            if row["uri"] and Path(row["uri"]).exists():
                uris.append(row["uri"])
        return uris

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
            overlap = len(wanted & row_chars)
            if not same_location and not overlap:
                continue
            score = (6 if same_location else 0) + overlap * 4
            if wanted and row_chars == wanted:
                score += 3
            ranked.append((score, row["id"], row))
        ranked.sort(key=lambda item: (-item[0], -item[1]))
        return [item[2] for item in ranked[:limit]]

    @staticmethod
    def _shot_reference_sheet_keys(shot):
        """按镜头用途选择已生成的人物套件参考,避免所有镜头只喂四视图。"""
        shot = shot or {}
        text = " ".join(str(shot.get(key) or "") for key in (
            "kind", "description", "prompt", "camera", "dialogue"))
        if any(word in text for word in (
                "服装", "配饰", "袖口", "鞋", "细节", "costume", "detail")):
            return ("turnaround", "costume", "costume_detail")
        if any(word in text for word in (
                "特写", "近景", "脸", "妆", "眼神", "表情", "closeup")):
            return ("turnaround", "features", "makeup")
        return ("turnaround", "costume")

    def _art_refs(self, ctx, characters, location, shot_no=None,
                  sheet_keys=None):
        """最终立绘/人物套件/场景图/用户参考 → 真实多图参考输入。

        含人物画面缺任何一个最终立绘都直接阻断；禁止静默退化为文字生图。
        """
        project_id = ctx["project"]["id"]
        refs = {"character_refs": [], "identity_references": [],
                "asset_matches": []}
        identities = self._identity_references(
            project_id, characters, required=bool(characters))
        for identity in identities:
            refs["character_refs"].append(identity["uri"])
            refs["identity_references"].append(identity)
            refs["asset_matches"].append({
                "asset_id": identity.get("asset_id"),
                "kind": "character_identity",
                "name": identity.get("character", ""),
                "label": f"{identity.get('character', '角色')}最终立绘",
                "uri": identity["uri"],
            })
        # 简化版即使项目历史里已有四视图，也只以人工锁定最终立绘为身份锚，
        # 避免旧扩展资产继续偷偷进入提示词与外部 API 参考图。
        asset_policy = ctx.get("character_asset_policy") or {}
        if not asset_policy and (ctx.get("episode") or {}).get("id"):
            asset_policy = self.character_asset_policy(
                ctx["episode"]["id"], script=ctx.get("script"))
        include_sheets = asset_policy.get("generate_sheets", True)
        if include_sheets:
            requested_keys = tuple(sheet_keys or ("turnaround", "costume"))
            # 多人镜头优先保持参考图总量可控:每人喂本镜最相关的一张
            # 套件图;单人镜头可以同时喂四视图和服装/细节图。
            if len(characters or []) > 1:
                requested_keys = requested_keys[-1:]
            for name in characters or []:
                for key in requested_keys:
                    row = self.assets.latest(
                        project_id, "character_sheet", f"{name}:{key}")
                    if (not row
                            or not formal_reference_allowed(
                                self._asset_quality(row))
                            or not row["uri"]
                            or not Path(row["uri"]).exists()):
                        continue
                    refs["character_refs"].append(row["uri"])
                    label = {
                        "turnaround": "四视图",
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
        if location:
            row = self.assets.latest(project_id, "scene_art", location)
            if (row and formal_reference_allowed(self._asset_quality(row))
                    and row["uri"] and Path(row["uri"]).exists()):
                refs["scene_ref"] = row["uri"]
                refs["asset_matches"].append({
                    "asset_id": row["id"], "kind": row["kind"],
                    "name": row["name"], "label": f"场景:{location}",
                    "uri": row["uri"],
                })
        matched_rows = (self._matching_produced_image_rows(
            project_id, characters, location, shot_no=shot_no)
            if shot_no is not None else [])
        matched = []
        for row in matched_rows:
            matched.append(row["uri"])
            refs["asset_matches"].append({
                "asset_id": row["id"], "kind": row["kind"],
                "name": row["name"], "label": "同人物/同场景已生产图",
                "uri": row["uri"],
            })
        reference = matched + self._reference_uris(
            project_id, list(characters or []) + ([location] if location
                                                  else []))
        if reference:
            refs["reference_images"] = reference
        anchor = self._style_anchor_uri(project_id)
        if anchor:
            refs["style_ref"] = anchor
        # 只要本次已经有任何锚点，就必须路由到能真实接收图片的产线；
        # 空镜同样不能把场景/风格/用户参考静默丢掉。
        refs["require_reference_images"] = bool(
            characters or refs.get("scene_ref")
            or refs.get("reference_images") or refs.get("style_ref"))
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

    def _rich_shot_prompt(self, ctx, shot, location):
        """详细出图提示词:场景 + 出场人物完整设定 + 动作 + 台词情绪 + 镜头,
        让每张分镜画面都说清楚人物是谁、在做什么、什么故事情境。"""
        project_id = ctx["project"]["id"]
        title = ctx["project"].get("title", "")
        parts = [f"漫剧《{title}》分镜画面"]
        script = ctx.get("script") or {}
        world = script.get("story_world") or {}
        background = script.get("story_background") or {}
        world_line = "；".join(
            str(world.get(key, "")).strip()
            for key in ("overview", "era_and_location", "hard_rules",
                        "visual_baseline")
            if str(world.get(key, "")).strip())
        if world_line:
            parts.append(f"故事世界硬约束:{world_line}")
        situation = "；".join(
            str(background.get(key, "")).strip()
            for key in ("current_situation", "core_conflict", "episode_goal")
            if str(background.get(key, "")).strip())
        if situation:
            parts.append(f"本集故事背景:{situation}")
        if location:
            scene = next((s for s in script.get("scenes", [])
                          if s.get("scene_no") == shot.get("scene_no")), {})
            scene_detail = str(scene.get("action") or "").strip()
            parts.append(f"场景:{location}"
                         + (f"(本场情境与环境细节:{scene_detail})"
                            if scene_detail else ""))
        script_profiles = {
            item.get("name"): item
            for item in script.get("characters", [])
            if isinstance(item, dict) and item.get("name")
        }
        who = []
        for name in shot.get("characters", []):
            design = self._reference_safe_design(
                self._character_design(project_id, name))
            design = {
                **script_profiles.get(name, {}),
                **(design or {}),
            }
            line = self._design_line(design, keys=(
                "introduction", "gender", "age_range", "identity",
                "personality", "species", "costume", "costume_detail",
                "makeup", "accessories", "palette", "signature",
                "temperament",
                "background_prompt", "era_setting", "occupation",
                "costume_direction", "signature_props")) if design else ""
            identity_rule = (
                "身份外貌、性别、年龄、脸型、五官和发型只以所附人工锁定"
                "最终立绘为准，禁止被旧文字设定覆盖")
            who.append(f"{name}({identity_rule}"
                       + (f";{line}" if line else "") + ")")
        if who:
            parts.append(
                f"出场人物(严格共{len(who)}人,形态与设定一致):"
                + ";".join(who))
        else:
            parts.append("环境空镜,画面中无人物")
        action = shot.get("description") or shot.get("prompt", "")
        if action:
            parts.append(f"本镜动作/画面:{action}")
        dialogue = shot.get("dialogue")
        if isinstance(dialogue, dict) and dialogue.get("dialogue"):
            speaker = dialogue.get("character", "")
            emo = (shot.get("speech_timing") or {}).get("emotion", "")
            parts.append(
                f"此刻{speaker}正说「{dialogue['dialogue']}」"
                + (f",情绪{emo},神态需体现" if emo else ",神态需体现"))
        elif shot.get("kind") == "reaction":
            parts.append("表现听者听到上一句台词后的即时反应与微表情")
        camera = shot.get("camera", "")
        if camera:
            parts.append(f"镜头语言:{camera}")
        ref = (shot.get("script_reference") or "").strip()
        if ref and ref not in action:
            parts.append(f"剧情依据:{ref}")
        # 画布关系线:多人物镜头带上人物之间的关联,牵引跨镜一致
        lines = relation_lines(self._relations(ctx),
                               shot.get("characters", []))
        if lines:
            parts.append("人物关系线:" + ";".join(lines))
        return "。".join(p for p in parts if p)

    def _shot_payload(self, ctx, shot, *, continuity_anchor=False,
                      quality_override=None, item_id=None):
        locations = self._scene_locations(ctx)
        location = locations.get(shot["scene_no"], "")
        profile = (ctx.get("production_profile")
                   or (ctx.get("storyboard") or {}).get("profile")
                   or production_profile(
                       self.config, ctx.get("production_standard")))
        spatial = shot_blocking(ctx.get("blocking"), shot["shot_no"])
        readable_text = shot.get("readable_text", {}) or {}
        text_required = bool(readable_text.get("required"))
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
        character_background = {
            name: {
                **script_characters.get(name, {}),
                **(self._reference_safe_design(
                    self._character_design(
                        ctx["project"]["id"], name)) or {}),
            }
            for name in shot.get("characters", [])
        }
        payload = {
            "shot_no": shot["shot_no"],
            "unit_id": shot.get("unit_id"),
            "prompt": self._rich_shot_prompt(ctx, shot, location),
            "seedance_prompt": shot.get("seedance_prompt", shot["prompt"]),
            "characters": shot["characters"],
            "identity_characters": identity_characters,
            "character_background": character_background,
            "story_world": (ctx.get("script") or {}).get(
                "story_world", {}),
            "story_background": (ctx.get("script") or {}).get(
                "story_background", {}),
            "character_count": shot.get(
                "character_count", len(shot["characters"])),
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
            "sound_design": shot.get("sound_design", {}),
            "spatial_blocking": spatial or {},
            "spatial_constraint": (spatial or {}).get("constraint", ""),
            "standard_fingerprint": profile.get("standard_fingerprint", ""),
            "forbid_subtitles": not profile["burn_subtitles"],
            "style": ctx["project"]["style"] or "",
            "aspect": ctx["aspect"], **ctx["dims"],
            **self._art_refs(
                ctx, identity_characters, location,
                shot_no=shot["shot_no"],
                sheet_keys=self._shot_reference_sheet_keys(shot)),
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
        return payload

    def _reference_manifest(self, payload):
        """按提交顺序给每张参考图编号并绑定用途(与产线上传顺序一致)。

        顺序必须与 API/CLI 产线的图片提交顺序完全相同:
        最终立绘 → 人物设定图 → 上一镜尾帧 → 场景图 → 风格基准 → 用户参考图。
        """
        labels = {match.get("uri"): match.get("label", "")
                  for match in payload.get("asset_matches", [])
                  if isinstance(match, dict)}
        entries, seen = [], set()

        def add(uri, label, binding, character=""):
            value = str(uri or "").strip()
            if not value or value in seen:
                return
            seen.add(value)
            entries.append({
                "index": len(entries) + 1, "uri": value,
                "label": label, "binding": binding,
                "character": character,
            })

        for pos, ref in enumerate(
                payload.get("identity_references") or [], 1):
            if not isinstance(ref, dict) or not ref.get("uri"):
                continue
            who = str(ref.get("character") or "角色")
            actor = str(ref.get("actor_id") or f"P{pos:02d}")
            add(ref["uri"], f"{actor}·{who}最终立绘",
                f"{who}的脸型、五官、发型、体型、年龄感必须与此图同一人,"
                "禁止参考他人图片", character=who)
        for uri in payload.get("character_refs") or []:
            label = labels.get(uri) or "人物设定图"
            who = label.split("最终立绘")[0].split("四视图")[0] \
                if ("最终立绘" in label or "四视图" in label) else ""
            add(uri, label,
                (f"{who}的服装结构、材质与配色细节参考此图"
                 if who else "对应人物的服装结构与配色细节参考此图"),
                character=who)
        add(payload.get("chain_first_uri"), "上一镜结尾画面",
            "构图、光线、人物站位与状态需自然承接此图")
        location = payload.get("location", "")
        add(payload.get("scene_ref"),
            f"场景「{location}」基准图" if location else "场景基准图",
            "空间结构、陈设、材质与光线以此图为准")
        add(payload.get("style_ref"), "全片风格基准图",
            "绘画风格、线条、上色与光影必须与此图一致")
        for uri in payload.get("reference_images") or []:
            label = labels.get(uri) or "用户上传参考图"
            binding = ("同人物/同场景连续性参考,人物造型与环境延续此前画面"
                       if "已生产" in label
                       else "用户指定参考,涉及的人物/场景以此为优先标准")
            add(uri, label, binding)
        return entries

    def _with_reference_manifest(self, payload):
        """便捷包装:附上参考图对照表后原样返回 payload。"""
        self._attach_reference_manifest(payload)
        return payload

    def _attach_reference_manifest(self, payload):
        """把参考图对照表写进 payload 与提示词(编号=实际提交顺序)。"""
        manifest = self._reference_manifest(payload)
        payload["reference_manifest"] = manifest
        if not manifest:
            return
        lines = [f"图{entry['index']}={entry['label']}:{entry['binding']}"
                 for entry in manifest]
        payload["prompt"] = (
            (payload.get("prompt") or "").rstrip("。")
            + f"。参考图对照表(共{len(manifest)}张,按此顺序提交,"
            "必须严格按编号对应使用,禁止张冠李戴、禁止把一个人的脸画成"
            "另一张参考图中的人):" + ";".join(lines))

    def _stage_images(self, ctx):
        self._plan_seed_shots(ctx)
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
                if self._quality_meets(actual_quality, required_quality):
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
                    forbid=["与设定形态不符的角色", "悬挂的衣物或衣架", "与设定不符的人"] + ["字幕条"]),
                    "camera": payload.get("camera", "")}})
        results = self._run_parallel(ctx, tasks, line="分镜画面")
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
        return {"count": len(ctx["images"]), "reused": reused}

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
            ctx["videos"].append(self._make_video(ctx, shot, frames))
        return {"count": len(ctx["videos"]), "reused": reused}

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
        if not any(int(shot.get("shot_no", -1)) == int(shot_no)
                   for shot in shots):
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
        unique_ids = []
        for value in asset_ids or []:
            asset_id = int(value)
            if asset_id not in unique_ids:
                unique_ids.append(asset_id)
        # multimodal2video 最多 9 张；首尾帧占 2 张。
        if len(unique_ids) > 7:
            raise AifosError("每个镜头最多选择 7 张资产参考图")
        selected = []
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
            shots[key] = {
                "mode": "manual" if key in manual else "auto",
                "items": [{
                    "asset_id": row["id"], "kind": row["kind"],
                    "name": row["name"], "uri": row["uri"],
                } for row in self._video_reference_rows(ctx, no)],
            }
        return {"schema": "aifos.video-references-effective/v1",
                "shots": shots}

    def _auto_video_reference_rows(self, ctx, shot_no):
        """人工未选择时,按剧本/分镜自动集合本镜必要参考图。

        顺序:本镜分镜图 → 出场人物最终立绘 → 场景概念图 → 用户参考图;
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

        # 本镜分镜示例图:有就必交(首尾帧本就源于它,画了不交等于白做);
        # 它承载本镜构图/调度事实,不受"低质量禁入正式参考链"限制
        shot_image = self.assets.latest(
            project_id, "image",
            f"e{ctx['episode']['number']:03d}_shot{int(shot_no):03d}")
        if (shot_image is not None
                and not self.assets.is_deleted(shot_image)
                and shot_image["uri"]
                and (shot_image["uri"].startswith(("http://", "https://"))
                     or Path(shot_image["uri"]).exists())):
            seen.add(shot_image["id"])
            rows.append(shot_image)
        for name in shot.get("characters", []) or []:
            add(self._locked_identity(project_id, name))
        if location:
            add(self.assets.latest(project_id, "scene_art", location))
        wanted = set(shot.get("characters") or [])
        if location:
            wanted.add(location)
        for row in self.assets.active_list(project_id, kind="reference"):
            attach = self._asset_meta(row).get("attach_to", "")
            if attach and attach not in wanted:
                continue
            add(row)
        return rows[:7]

    def _video_reference_rows(self, ctx, shot_no):
        document, _ = self.projects.latest_document(
            ctx["episode"]["id"], "video_references")
        shots_doc = (document or {}).get("shots", {})
        if str(int(shot_no)) not in shots_doc:
            # 人工没选过 = 自动选入必要参考图;人工选择(含清空)优先
            return self._auto_video_reference_rows(ctx, shot_no)
        selected = shots_doc.get(str(int(shot_no)), [])
        rows = []
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
            uri = row["uri"]
            if (uri.startswith(("http://", "https://"))
                    or Path(uri).exists()):
                rows.append(row)
        return rows[:7]

    def _make_video(self, ctx, shot, frames):
        frame = frames[shot["shot_no"]]
        if not formal_reference_allowed(
                frame.get("image_quality", "medium")):
            raise AifosError(
                f"镜头{shot['shot_no']}首尾帧为低质量试错图，禁止交给 Seedance")
        quality = resolve_video_quality(
            ctx.get("quality_policy") or default_quality_policy(),
            shot_no=shot["shot_no"])
        reference_rows = self._video_reference_rows(ctx, shot["shot_no"])
        reference_assets = [{
            "asset_id": row["id"], "kind": row["kind"],
            "name": row["name"], "version": row["version"],
        } for row in reference_rows]
        result = self._call(ctx, "video", {
            "shot_no": shot["shot_no"],
            "unit_id": shot.get("unit_id"),
            "prompt": shot.get("seedance_prompt", shot["prompt"]),
            "duration": shot["duration"],
            "first": frame["first"],
            "last": frame["last"],
            "reference_images": [row["uri"] for row in reference_rows],
            "reference_assets": reference_assets,
            "dialogue": shot.get("dialogue"),
            "voice": ctx["production_profile"]["voice"],
            "lip_sync": ctx["production_profile"]["lip_sync"],
            "forbid_subtitles": not ctx["production_profile"]["burn_subtitles"],
            "video_quality": quality["level"],
            "video_resolution": quality["resolution"],
            "standard_fingerprint": ctx["production_profile"].get(
                "standard_fingerprint", ""),
            "aspect": ctx["aspect"], **ctx["dims"],
        }, "videos")
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
        self._register_shot_asset(ctx, "video", shot["shot_no"], result.uri,
                                  meta={"provider": result.provider,
                                        "audio_in_video": audio_in_video,
                                        "video_quality": quality["level"],
                                        "video_resolution": quality["resolution"],
                                        "quality_source": quality["source"],
                                        "reference_assets": reference_assets})
        return {"shot_no": shot["shot_no"], "uri": result.uri,
                "duration": shot["duration"], "provider": result.provider,
                "audio_in_video": audio_in_video,
                "video_quality": quality["level"],
                "video_resolution": quality["resolution"],
                "reference_assets": reference_assets}

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
        report = None
        for attempt in range(max_retries + 1):
            report = self.qc.run(ctx["script"], ctx["storyboard"], ctx)
            self.log.info(
                "qc", f"质检第{attempt + 1}轮:得分 {report['score']}"
                f"(线 {report['pass_score']}),问题 {len(report['issues'])}")
            # 只要存在可自动重跑的缺失产物(即使总分达标)就先修复再定论
            fixable = report["rerun_shots"] or report["rerun_lines"]
            if not fixable or attempt == max_retries:
                break
            self._rerun(ctx, report)
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
                "delivery_passed": delivery.get("passed", False)}

    def _rerun(self, ctx, report):
        shots = {s["shot_no"]: s for s in ctx["storyboard"]["shots"]}
        frames = {f["shot_no"]: f for f in ctx["frames"]}
        for shot_no in report["rerun_shots"]:
            self.log.info("director", f"自动重跑镜头 {shot_no} 的视频")
            new_video = self._make_video(ctx, shots[shot_no], frames)
            ctx["videos"] = [
                new_video if v["shot_no"] == shot_no else v
                for v in ctx["videos"]]
            self.data.record(
                "case", "failure", prompt=shots[shot_no]["prompt"],
                meta={"reason": "qc_rerun", "shot_no": shot_no},
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
            revision_source="manual"):
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
            # 后续镜尚未进入生产线时不提前生成；确认/续产时自然补齐。
            if offset and not (old_first or old_last):
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
                frames_payload["seedance_prompt"] = prompt_override
            if previous_last:
                frames_payload["chain_first_uri"] = previous_last
            frame_result = self._plan_run(
                ctx, f"frames:{shot['shot_no']}",
                lambda payload=frames_payload: self._call(
                    ctx, "frames", payload, "frames"),
                prompt=self._prompt_with_feedback(
                    frames_payload["prompt"],
                    frames_payload["feedback"]),
                payload=frames_payload, revision_source=revision_source)
            meta = self._quality_meta(frames_payload["quality_decision"])
            self.assets.register(
                ctx["project"]["id"], "first_frame", asset_name,
                uri=frame_result.data["first"], meta=meta,
                new_version=True)
            self.assets.register(
                ctx["project"]["id"], "last_frame", asset_name,
                uri=frame_result.data["last"], meta=meta,
                new_version=True)
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

    def _invalidate_revised_delivery(self, ctx, shot, formal_ready=True):
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
        invalidate("clip", f"{ep_name}_scene{int(shot['scene_no']):02d}")
        self.projects.set_qc_score(episode_id, None)
        revision = {
            "schema": "aifos.shot-revision-state/v1",
            "active": True,
            "shot_no": int(shot["shot_no"]),
            "scene_no": int(shot["scene_no"]),
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
                    revision_source="manual"):
        """重画单张图:target = {"kind": character_art|scene_art|shot,
        "name"|"shot_no"};附意见时新画面按意见调整;
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
        }.get(kind, lambda: "")()
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
                    "feedback": feedback,
                    "revision": next_revision("character_sheet", raw),
                    "character_refs": (
                        ([portrait_uri] if portrait_uri else [])
                        + existing_sheet_refs),
                    "identity_references": self._identity_references(
                        project["id"], [name]),
                    "require_reference_images": True,
                    "reference_images": self._reference_uris(
                        project["id"], [name]),
                    "style_ref": self._style_anchor_uri(project["id"]),
                    "aspect": aspect, **ctx["dims"],
            }
            result = self._plan_run(
                ctx, f"sheet:{name}:{sheet_key}",
                lambda: self._call(ctx, "image", payload, "cast"),
                prompt=self._prompt_with_feedback(prompt, feedback),
                payload=payload, revision_source=revision_source)
            self.assets.register(
                project["id"], "character_sheet", raw, uri=result.uri,
                meta={"character": name, "sheet": sheet_key,
                      "label": label, **self._quality_meta(quality)},
                new_version=True)
        elif kind == "scene_art":
            name = target["name"]
            scene = next((s for s in script["scenes"]
                          if s["location"] == name), {})
            prompt = prompt_override or self._scene_prompt(
                name, style, scene,
                premise=episode["premise"] if episode else "")
            references = self._reference_uris(project["id"], [name])
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
                    "revision": next_revision("scene_art", name),
                    "reference_images": references,
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
            self.assets.register(project["id"], "scene_art", name,
                                 uri=result.uri,
                                 meta=self._quality_meta(quality),
                                 new_version=True)
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
                payload["seedance_prompt"] = prompt_override
            asset_name = self._shot_name(ctx, shot_no)
            old_image = self.assets.latest(
                project["id"], "image", asset_name)
            result = self._plan_run(
                ctx, f"shot:{shot_no}",
                lambda: self._call(ctx, "image", payload, "images"),
                prompt=self._prompt_with_feedback(
                    payload["prompt"], feedback),
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
            sync = self._regenerate_revised_frame_chain(
                ctx, storyboard, shot, feedback=feedback,
                prompt_override=prompt_override,
                quality_override=quality_choice,
                revision_source=revision_source)
            sync.update(self._invalidate_revised_delivery(
                ctx, shot, formal_ready=formal_ready))
            sync["video_reference_shots"] = reference_shots
            sync["image_asset_id"] = new_image["id"]
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
            if formal_reference_allowed(self._asset_quality(image_row)):
                frames_payload["image_uri"] = image_row["uri"]
            else:
                frames_payload["draft_image_rejected"] = image_row["uri"]
            if prompt_override:
                frames_payload["prompt"] = prompt_override
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
            result = self._plan_run(
                ctx, f"frames:{shot_no}", lambda: self._call(
                    ctx, "frames", frames_payload, "frames"),
                prompt=self._prompt_with_feedback(
                    frames_payload["prompt"], feedback),
                payload=frames_payload, revision_source=revision_source)
            self.assets.register(project["id"], "first_frame", asset_name,
                                 uri=result.data["first"],
                                 meta=self._quality_meta(
                                     frames_payload["quality_decision"]),
                                 new_version=True)
            self.assets.register(project["id"], "last_frame", asset_name,
                                 uri=result.data["last"],
                                 meta=self._quality_meta(
                                     frames_payload["quality_decision"]),
                                 new_version=True)
            self.assets.delete(project["id"], "video", asset_name)
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
        if kind == "shot":
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
        """上传替换图片:character_art / scene_art / shot(镜头画面)。
        镜头画面替换后自动按新图重做首尾帧并作废旧视频。"""
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
            self.assets.register(project["id"], kind, name,
                                 uri=str(path), meta=uploaded_meta,
                                 new_version=True)
            if kind == "character_art":
                # 人工上传等同于明确确认最终立绘，同时建立真正的身份锚点。
                self.assets.register(
                    project["id"], "character_identity", name,
                    uri=str(path),
                    meta={"character": name, "locked": True,
                          "uploaded": True, "locked_at": now(),
                          "image_quality": "high",
                          "recommended_quality": "high",
                          "quality_source": "manual_upload"},
                    new_version=True)
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
    _FORBID = ["与设定形态不符的角色", "悬挂的衣物或衣架", "与设定不符的人"]

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

    def _qc_signature(self, uris, spec):
        """图片内容、最终立绘版本和质检规格都没变时复用质检结果。"""
        digest = hashlib.sha256()
        digest.update(json.dumps(spec, ensure_ascii=False, sort_keys=True,
                                 separators=(",", ":")).encode("utf-8"))
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
        return self._qc_one(project, episode, ctx, item)

    def _qc_one(self, project, episode, ctx, item):
        project_id = project["id"]
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
                    forbid=self._FORBID + ["字幕条"])
                spec["camera"] = payload.get("camera", "")
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
        signature = self._qc_signature(uris, spec)
        previous = item.get("qc") or {}
        if previous.get("signature") == signature \
                and "passed" in previous:
            cached = dict(previous)
            cached["cached"] = True
            return cached
        passed_all, issues, cost = True, [], 0.0
        identity_checked_all = True
        gender_checked_all = True
        gender_match_all = True
        try:
            for label, one in uris:
                result = self.router.call(
                    "image_qc", {**spec, "image_uri": one}, ctx["out_root"],
                    cancel=lambda: self._cancel_requested(ctx))
                cost += result.cost
                verdict = result.data or {}
                identity_checked = (not spec.get("identity_required")
                                    or bool(verdict.get("identity_checked")))
                if not identity_checked:
                    identity_checked_all = False
                    passed_all = False
                    issues.append(f"{label}:质检未确认已逐人比对最终立绘")
                gender_declared = bool({"gender_checked", "gender_match"}
                                       & set(verdict))
                gender_checked = (not spec.get("gender_required")
                                  or not gender_declared
                                  or bool(verdict.get("gender_checked")))
                gender_match = (not spec.get("gender_required")
                                or not gender_declared
                                or bool(verdict.get("gender_match")))
                if not gender_checked:
                    gender_checked_all = False
                    passed_all = False
                    issues.append(f"{label}:质检未单独核对人物性别/性别表达")
                elif not gender_match:
                    gender_match_all = False
                    passed_all = False
                    issues.append(
                        f"{label}:人物性别/性别表达与锁定最终立绘不一致")
                if not bool(verdict.get("pass")):
                    passed_all = False
                    issues.extend(f"{label}:{x}"
                                  for x in (verdict.get("issues") or []))
        except (ProviderUnavailable, ProviderError) as exc:
            raise AifosError(f"质检产线不可用: {exc}") from exc
        report = {"passed": passed_all, "issues": issues,
                  "attempts": previous.get("attempts", 0),
                  "identity_checked": identity_checked_all,
                  "gender_checked": gender_checked_all,
                  "gender_match": gender_match_all,
                  "identity_references": len(
                      spec.get("identity_references") or []),
                  "signature": signature, "cached": False}
        self.projects.add_episode_cost(episode["id"], cost)
        self._plan_mark(ctx, item["id"], item.get("status", "done"),
                        extra={"qc": report})
        self.log.info(
            "director",
            f"质检 {item['id']}: "
            + ("通过" if report["passed"]
               else "未过 — " + "；".join(report["issues"])))
        return report

    def qc_all(self, project_title, episode_number):
        """批量质检:对清单里所有已生成的图逐张核对,可暂停。"""
        project, episode = self._episode_ctx(project_title, episode_number)
        ctx = {"project": dict(project), "episode": dict(episode),
               "out_root": self._episode_dir(project, episode)}
        plan = self._plan_read(ctx)
        items = [i for i in plan["items"]
                 if i.get("status") in ("done", "reused")
                 and i.get("category") in self.SHOT_QC_CATEGORIES]
        previous_status = episode["status"]
        self.projects.set_episode_status(episode["id"], "cast")
        checked = passed = failed = 0
        try:
            for item in items:
                if self._cancel_requested(ctx):
                    raise ProduceCancelled("已手动暂停质检")
                try:
                    report = self._qc_one(project, episode, ctx, item)
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
                    "passed": passed, "failed": failed}
        finally:
            row = self.projects.get_episode(episode["id"])
            if row and row["status"] in ("cast", "cancelling"):
                self.projects.set_episode_status(
                    episode["id"], previous_status)
        self.log.info(
            "director",
            f"批量质检完成:{checked} 张,通过 {passed},未过 {failed}")
        return {"status": "done", "checked": checked,
                "passed": passed, "failed": failed}

    def redo_items(self, project_title, episode_number, item_ids=None,
                   only_failed=False, quality_override=None, progress=None):
        """批量重画:按 item_ids 重画;only_failed=True 时重画所有质检
        未过的图。可暂停,重画后自动复检。"""
        project, episode = self._episode_ctx(project_title, episode_number)
        ctx = {"project": dict(project), "episode": dict(episode),
               "out_root": self._episode_dir(project, episode)}
        plan = self._plan_read(ctx)
        by_id = {i["id"]: i for i in plan["items"]}
        if only_failed:
            targets = [i["id"] for i in plan["items"]
                       if (i.get("qc") or {}).get("passed") is False
                       and i.get("category") in self.SHOT_QC_CATEGORIES
                       and i.get("status") in ("done", "reused")]
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
            for index, item_id in enumerate(targets, 1):
                if self._cancel_requested(ctx):
                    raise ProduceCancelled("已手动暂停重画")
                target = self._plan_item_target(item_id)
                item = by_id[item_id]
                label = item.get("label") or item_id
                if target is None:
                    failed += 1
                    processed += 1
                    if progress:
                        progress(phase="redrawing", total=total,
                                 completed=processed, current_index=index,
                                 current_item=item_id, current_label=label,
                                 redone=redone, failed=failed)
                    continue
                issues = list((item.get("qc") or {}).get("issues") or [])
                if issues:
                    feedback = (
                        "批量重画自动修正：必须逐项解决上一版质检问题："
                        + "；".join(issues))[:800]
                    revision_source = "batch_qc"
                else:
                    feedback = (
                        "批量重新画：生成与上一版明显不同的有效新版本；"
                        "严格保持已锁定的人物身份、服装、场景、文字白名单"
                        "和前后镜头连续性")
                    revision_source = "batch_redraw"
                prompt_override = (item.get("prompt", "")
                                   if item.get("custom_prompt") else "")
                if progress:
                    progress(phase="redrawing", total=total,
                             completed=processed, current_index=index,
                             current_item=item_id, current_label=label,
                             redone=redone, failed=failed,
                             prompt_modified=True,
                             revision_note=feedback)
                try:
                    self.regen_image(
                        project_title, episode_number, target,
                        feedback=feedback, prompt_override=prompt_override,
                        quality_override=quality_override,
                        revision_source=revision_source)
                    redone += 1
                except AifosError as exc:
                    failed += 1
                    self.log.warn("director",
                                  f"重画 {item_id} 跳过: {exc}")
                    processed += 1
                    if progress:
                        progress(phase="redrawing", total=total,
                                 completed=processed, current_index=index,
                                 current_item=item_id, current_label=label,
                                 redone=redone, failed=failed,
                                 error=str(exc))
                    continue

                refreshed = next((entry for entry in self._plan_read(ctx)[
                    "items"] if entry["id"] == item_id), item)
                refs = refreshed.get("reference_inputs") or {}
                if progress:
                    progress(phase="checking", total=total,
                             completed=processed, current_index=index,
                             current_item=item_id, current_label=label,
                             redone=redone, failed=failed,
                             references_attached=bool(refs.get("attached")),
                             reference_count=int(refs.get("count") or 0))
                try:
                    report = self._qc_one(
                        project, episode, ctx, refreshed)
                    checked += 1
                    if report.get("passed"):
                        qc_passed += 1
                    else:
                        qc_failed += 1
                except AifosError as exc:
                    self.log.warn(
                        "director", f"重画后复检 {item_id} 跳过: {exc}")
                processed += 1
                if progress:
                    progress(phase="running", total=total,
                             completed=processed, current_index=index,
                             current_item=item_id, current_label=label,
                             redone=redone, failed=failed, checked=checked,
                             qc_passed=qc_passed, qc_failed=qc_failed,
                             references_attached=bool(refs.get("attached")),
                             reference_count=int(refs.get("count") or 0))
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
        self.log.info("director", f"批量重画完成:{redone} 张")
        if progress:
            progress(phase="done", total=total, completed=processed,
                     redone=redone, failed=failed, checked=checked,
                     qc_passed=qc_passed, qc_failed=qc_failed,
                     current_item="", current_label="")
        return {"status": "done", "total": total, "redone": redone,
                "failed": failed, "checked": checked,
                "qc_passed": qc_passed, "qc_failed": qc_failed}

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
                      attach_to="", note=""):
        """上传参考图:attach_to 为空=全项目通用,否则只用于该角色/场景。"""
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
        safe = "".join(c if c.isalnum() else "_" for c in name)[:40]
        existing = self.assets.latest(project["id"], "reference", name)
        version = (existing["version"] + 1) if existing else 1
        path = (self.artifacts_root / f"p{project['id']:03d}"
                / "references" / f"{safe}_v{version}{ext}")
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(file_bytes)
        self.assets.register(
            project["id"], "reference", name, uri=str(path),
            meta={"attach_to": attach_to or "", "note": note or ""},
            new_version=existing is not None)
        self.log.info(
            "director", f"已上传参考图「{name}」"
            f"(关联: {attach_to or '全项目'});后续出图将自动参考")
        # 参考图为最高标准:关联到已有设定的角色时,立即按参考图的
        # 脸部特征与风格重写该角色的人物设定提示词
        design_refreshed = False
        if attach_to:
            design_refreshed = self._refresh_design_from_reference(
                project, attach_to)
        return {"name": name, "uri": str(path),
                "design_refreshed": design_refreshed}

    def _refresh_design_from_reference(self, project, name):
        """按参考图重写角色设定:脸部特征与风格以参考图为最高标准。

        找不到该角色/无编剧产线时静默跳过(不阻断参考图上传本身)。
        """
        row = self.assets.latest(project["id"], "character", name)
        if row is None:
            return False
        meta = self._asset_meta(row)
        references = self._character_reference_uris(project["id"], name)
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
