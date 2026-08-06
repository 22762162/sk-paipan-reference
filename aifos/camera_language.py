"""镜头语言具象化:把抽象摄影术语翻译成生图模型能核验的可见几何特征。

历史质检约 30 次「视角/构图不落实」(俯拍不俯、背面出正脸、过肩变
单人),根因是「俯拍」「back_view」这类行话对图像模型约束力弱——
模型不知道画面里到底该出现什么。本模块给每个景别/角度/机位术语补
一句「画面里应该看到什么」,随镜头合同进提示词;质检修图也按同一
标准核验,原始提示词与修订指令不再各说各话。

零依赖叶子模块:prompt_contract 与 qc_feedback 都可安全引用。
"""

import re

# 景别 → 画面可见特征(取景边界)
SCALE_GEOMETRY = {
    "大特写": "面部局部或指定细节占满画面,肩部以下全部出画",
    "特写": "头部与颈部占画面主导,肩线以下出画,背景只剩虚化色块",
    "近景": "胸口以上入画,双肩完整,手部只在抬起时入画",
    "中近景": "胸口以上、腰线以下出画,面部表情与手部动作同时可读,"
              "背景占比小于人物",
    "中景": "腰部或膝上以上入画,双臂动作完整可见",
    "膝上景": "取到膝盖上方、脚部完全出画,可见腰胯与站姿,"
              "适合展示服装形制",
    "七分身": "取到大腿中段,人物约占画面高度七成,姿态与服装主体完整",
    "中全景": "人物从头部至少取到小腿或完整全身,主要动作与支撑关系可读,"
              "同时保留足够环境边界与纵深来核验空间",
    "全景": "人物头顶到脚底完整入画,上下留呼吸空间,环境可辨认",
    "远景": "人物占画面高度不足三分之一,环境是画面主体",
    "大远景": "人物占画面高度不足四分之一,空间关系是画面主体,"
              "人物身份不作核验依据",
}

# 角度 → 摄影机与人物的垂直关系(俯仰透视特征)
ANGLE_GEOMETRY = {
    "顶拍": "摄影机在人物正上方垂直向下:只见头顶、双肩上表面,"
            "地面平面几乎充满背景",
    "俯拍": "摄影机高于人物视线向下看:可见头顶与双肩上表面,"
            "身后地面占据背景大部,天花板/天空不入画",
    "仰拍": "摄影机低于人物视线向上看:可见下颌底面与鼻底,"
            "躯干向上透视收缩,天花板或天空进入背景上部",
    "平视": "摄影机与人物眼睛等高:地平线过人物眼部高度,无俯仰透视变形",
    "低角度": "摄影机低于人物腰线向上拍:人物占据画面上方并显得高大,"
              "可见下颌底面,背景多为天空、屋顶或高处结构",
    "高角度": "摄影机高于人物头顶但非垂直:可见头顶与肩背上表面,"
              "人物在画面中显得渺小,地面占背景大部",
    "斜角": "画面竖直线与地平线整体倾斜约10到25度:门框、柱、"
            "地平线同向倾斜制造失衡感;人物本身不得歪斜或变形",
    "主观视角": "画面即某角色眼睛所见:该角色本人不入画(至多出现其手部"
                "或持物),其余人物朝镜头方向交流",
}

# 机位 → 面部/身体朝向的可见判据(身份核验安全:背面/侧面不见正脸)
POSITION_GEOMETRY = {
    "过肩": "前景近端是一名人物的后脑与肩背(虚化,不见五官),"
            "画面主体是远端另一人物",
    "背面": "只见人物后脑、背部与身体背面轮廓,"
            "完全不出现眉、眼、鼻、嘴任何面部器官",
    "侧面": "人物呈正侧轮廓:只见单侧眼睛与单侧耳朵,鼻梁构成面部外缘线",
    "正面": "人物面向摄影机,双眼可见且左右基本对称",
    "正侧面": "人物呈严格90度侧向:只见单侧眼睛与单侧耳朵,"
              "鼻梁构成面部外缘线,另半边面部完全不可见",
    "四分之三面": "人物面部转向约45度:双眼可见但远端眼略被鼻梁遮挡,"
                  "两侧脸颊面积不等,最利于识别身份",
    "反打": "与前一镜相反的机位:镜头越过对侧肩膀,屏幕方向与前镜"
            "严格互补,不得让对话双方同时朝向同一侧",
}

# 运镜 → 视频单元的可执行动作(静态图不适用,由 mode 决定是否下发)
MOVEMENT_GEOMETRY = {
    "推": "摄影机沿视线方向匀速靠近主体,取景由宽变窄,主体在画面中"
          "逐渐变大;背景透视关系保持不变",
    "拉": "摄影机沿视线方向匀速后退,取景由窄变宽,逐步交代主体所处环境",
    "摇": "机位不动,机身水平转动扫过空间:背景横向滑移,不产生视差位移",
    "移": "摄影机整体横向平移:前景与背景产生明显视差,近处移动快于远处",
    "跟": "摄影机与运动主体同速同向移动:主体在画面中位置基本不变,"
          "背景持续流动",
    "升降": "摄影机垂直升起或降下:视平线随之抬高或压低,俯仰关系连续变化",
    "环绕": "摄影机以主体为圆心横向绕行:主体朝向在画面中基本稳定,"
            "背景连续换面",
    "手持": "画面带轻微不规则晃动与呼吸感,晃幅不得大到看不清主体或"
            "破坏身份识别",
    "固定": "机位与焦距全程不变,画面内只有人物与环境自身在动",
    # ---- 表现性运镜(艺术运镜):按题材语法在情绪极点选用,一镜一种 ----
    "甩镜": "机身沿甩动方向极速转动:中段数帧呈方向性运动模糊、景物"
            "不可辨,起止画面清晰稳定;甩动方向与被甩向的目标方位一致",
    "螺旋环绕": "环绕与升降复合:绕主体横向绕行的同时持续上升或下降,"
                "背景连续换面且视平线渐变;主体始终是构图中心,"
                "情绪随高度变化推进",
    "穿越": "摄影机沿连续路径穿过门缝/窗洞/孔隙等前景开口:开口边缘"
            "放大至出画,穿过瞬间可短暂全遮挡或虚化,随后新空间连续展开;"
            "禁止瞬移换景",
    "俯冲": "摄影机自高处沿弧线加速下降逼近主体:视平线急速下移,"
            "地面透视迅速展开,主体由小变大,落点平稳收住不撞穿主体",
    "升格": "时间语言:全画面以明显慢于实时的速率呈现,发丝、衣料、"
            "水珠、尘埃呈悬浮般缓慢连贯运动,无跳帧;清晰度与人物"
            "身份特征不因慢速下降",
    "希区柯克变焦": "(实验级:视频模型失败率高,仅题材语法明确要求且"
                    "单镜情绪极点时限量使用,预期不稳时回退为「推」)"
                    "机位推近同时镜头反向变焦:主体在画面中的大小几乎"
                    "不变,背景透视被明显压缩或拉伸产生眩晕感;"
                    "主体轮廓与五官全程清晰稳定",
}

# 表现性运镜:3D 求解器没有它们的米制模型(求解只覆盖推/拉/摇/移/跟/
# 升降/环绕),这些词条以词典文字合同为准,不得被求解推导的「固定」覆盖。
EXPRESSIVE_MOVEMENTS = frozenset({
    "甩镜", "螺旋环绕", "穿越", "俯冲", "升格", "希区柯克变焦"})


def movement_geometry_for(declared_text, derived_term=""):
    """运镜条款取值,统一两条优先级规则:

    1. 长词优先匹配声明文本,避免「螺旋环绕」被「环绕」截胡;
    2. 声明命中表现性运镜时以词典文字合同为准——求解器对它们只会
       误报「固定」,把艺术运镜抹掉正是"画面全是静态"的病根之一;
       其余仍维持「三维调度为准」:分镜一个词约束力弱,推导自机位
       起终点的几何更可靠。
    """
    text = str(declared_text or "")
    declared = next(
        (word for word in sorted(MOVEMENT_GEOMETRY, key=len, reverse=True)
         if word and word in text), "")
    if declared in EXPRESSIVE_MOVEMENTS:
        return MOVEMENT_GEOMETRY[declared]
    return (MOVEMENT_GEOMETRY.get(str(derived_term or ""))
            or MOVEMENT_GEOMETRY.get(declared, ""))


# 运镜 → 首尾帧取景差异规则(AI 导演 2026-08-01 咨询产出,经校验收录)。
# 隔离测试实证:视频运镜由首尾帧差异硬约束——同取景写"推"也不推,
# 宽首帧+紧尾帧才真推。首尾帧生成阶段按此推导两帧各自的取景合同;
# no_delta 类(固定/手持/升格)两帧同取景,运动语义由提示词承担。
MOVEMENT_FRAME_DELTAS = {
    "推": {
        "no_delta": False,
        "framing_delta": (
            "尾帧比首帧紧3档；主体等比放大且居中，视平线差0，背景可"
            "见范围缩小。"),
        "first_frame": (
            "中景；摄影机距主体约6米，镜头焦段、机高和视线方向固定；"
            "主体中心位于画面x=50%、高度约45%；视平线距顶边5"
            "0%；背景左右边界与纵深层次完整可见。"),
        "last_frame": (
            "近景；摄影机沿首帧视线轴位于距主体约3.5米处，镜头焦段"
            "、机高和视线方向与首帧相同；主体中心仍为x=50%、高度"
            "约70%；视平线仍距顶边50%；背景消失外围约35%，消"
            "失点和层次顺序不变。"),
    },
    "拉": {
        "no_delta": False,
        "framing_delta": (
            "尾帧比首帧宽3档；主体等比缩小且居中，视平线差0，背景可"
            "见范围扩大。"),
        "first_frame": (
            "近景；摄影机距主体约3.5米，主体中心位于画面x=50%"
            "、高度约70%；视平线距顶边50%；背景仅见主体邻近区域"
            "。"),
        "last_frame": (
            "中景；摄影机沿首帧视线轴位于距主体约6米处，镜头焦段、机"
            "高和视线方向与首帧相同；主体中心仍为x=50%、高度约4"
            "5%；视平线仍距顶边50%；背景左右外围和纵深环境完整可"
            "见。"),
    },
    "摇": {
        "no_delta": False,
        "framing_delta": (
            "景别差0档、视平线差0；机位不变，水平方位角相差40°，"
            "主体由右侧移至左侧，背景横向换区但无机位视差。"),
        "first_frame": (
            "中景；摄影机位置、机高和焦段锁定，水平朝向为场景基准方位"
            "角-20°；视平线距顶边50%；主体大小约45%，位于x"
            "=75%；画面主要显示场景左侧背景。"),
        "last_frame": (
            "中景；摄影机位置、机高和焦段与首帧相同，水平朝向为场景基"
            "准方位角+20°；视平线仍距顶边50%；同一主体大小约4"
            "5%，位于x=25%；画面主要显示场景右侧背景。"),
    },
    "移": {
        "no_delta": False,
        "framing_delta": (
            "景别差0档、视平线差0、光轴方位差0；机位横移3米，近景"
            "横移70%画宽、远景横移10%画宽，形成可核验视差。"),
        "first_frame": (
            "中景；摄影机位于轨道横坐标x=-1.5米，机高1.6米，"
            "光轴与轨道垂直且保持平行；主体大小约45%，位于画面x="
            "55%；近景立柱位于x=85%，远处塔楼位于x=60%；"
            "视平线距顶边50%。"),
        "last_frame": (
            "中景；摄影机位于同一轨道横坐标x=+1.5米，机高、焦段"
            "和光轴方向与首帧相同；主体大小变化不超过5%，位于画面x"
            "=35%；同一近景立柱位于x=15%，远处塔楼位于x=5"
            "0%；视平线仍距顶边50%。"),
    },
    "跟": {
        "no_delta": False,
        "framing_delta": (
            "主体景别、大小、位置和视平线差均为0；摄影机与主体同向各"
            "位移5米，背景可见区改变，主体—摄影机相对几何不变。"),
        "first_frame": (
            "中景；主体位于路径坐标0米，摄影机位于其后方3米、机高1"
            ".6米；主体大小约45%，中心为x=50%；视平线距顶边"
            "50%；背景可见起点路标和近处左侧树干。"),
        "last_frame": (
            "中景；主体位于同一路径坐标5米，摄影机位于其后方2米位置"
            "，即路径坐标2米并保持3米跟拍距离、相同机高和焦段；主体"
            "大小约45%，中心仍为x=50%；视平线仍距顶边50%；"
            "背景改为终点路标，首帧树干已不在画内。"),
    },
    "升降": {
        "no_delta": False,
        "framing_delta": (
            "升起规则为尾帧宽2档、机高增加2.9米、视平线上移30%"
            "画高；剧本指定下降时交换两帧几何。"),
        "first_frame": (
            "膝上景；采用升起方向，摄影机高度1.6米、水平距离主体5"
            "米，俯角0°；主体中心位于x=50%、高度约55%；视平"
            "线距顶边52%；地面约占画面下部35%。"),
        "last_frame": (
            "全景；摄影机高度4.5米、水平距离主体仍为5米，俯角约-"
            "25°并指向主体；主体中心位于x=50%、高度约38%；"
            "视平线距顶边22%；地面约占画面75%，可见主体周边平面"
            "关系。"),
    },
    "环绕": {
        "no_delta": False,
        "framing_delta": (
            "景别差0档、视平线差0；机位绕主体相差90°，主体居中且"
            "大小稳定，背景换面。"),
        "first_frame": (
            "中景；摄影机位于以主体为圆心、半径4米、方位角-45°的"
            "位置，机高1.6米，光轴指向主体；主体大小约45%、中心"
            "为x=50%，面向镜头偏差不超过10°；视平线距顶边50"
            "%；背景为建筑正立面。"),
        "last_frame": (
            "中景；摄影机位于同一圆周半径4米、方位角+45°的位置，"
            "机高和焦段不变，光轴指向主体；主体大小约45%、中心仍为"
            "x=50%，面向镜头偏差不超过10°；视平线仍距顶边50"
            "%；背景为建筑侧面与相邻巷道。"),
    },
    "手持": {
        "no_delta": True,
        "framing_delta": (
            "no_delta=true；手持是帧间微幅不规则姿态扰动"
            "，不应由首尾取景差制造定向推、拉、摇、移。"),
        "first_frame": (
            "中景；摄影机位置、机高、焦段和光轴按本镜头基准构图锁定；"
            "主体大小约45%、中心为x=50%；视平线距顶边50%；"
            "背景边界按基准构图固定。"),
        "last_frame": (
            "中景；摄影机位置、机高、焦段、光轴、主体大小与位置、视平"
            "线及背景边界均与首帧基准构图相同。"),
    },
    "固定": {
        "no_delta": True,
        "framing_delta": (
            "no_delta=true；固定镜头没有摄影机位移、转角"
            "或焦距变化，首尾只能存在画面内容自身差异。"),
        "first_frame": (
            "按镜头指定景别生成基准静态构图；摄影机位置、机高、焦段和"
            "光轴锁定，记录主体中心坐标、主体画面高度、视平线高度及背"
            "景四边边界。"),
        "last_frame": (
            "摄影机位置、机高、焦段、光轴、景别、主体中心坐标、主体画"
            "面高度、视平线高度及背景四边边界与首帧完全相同；只允许人"
            "物姿态或环境内部状态不同。"),
    },
    "甩镜": {
        "no_delta": False,
        "framing_delta": (
            "景别差0档、视平线差0；同一机位的水平方位角相差90°，"
            "首尾分别锁定目标A与目标B，方向为向右甩；向左甩时角度符"
            "号取反。"),
        "first_frame": (
            "中景且画面清晰；摄影机位于固定机位P、机高1.6米，水平"
            "朝向方位角0°；目标A大小约45%、中心为x=50%；视"
            "平线距顶边50%；背景为区域A。"),
        "last_frame": (
            "中景且画面清晰；摄影机仍位于机位P、机高和焦段不变，水平"
            "朝向方位角+90°；被甩向的目标B大小约45%、中心为x"
            "=50%；视平线仍距顶边50%；背景为与区域A不重叠的区"
            "域B。"),
    },
    "螺旋环绕": {
        "no_delta": False,
        "framing_delta": (
            "尾帧比首帧宽3档；机位方位角相差90°、高度增加2.9米"
            "、视平线上移32%画高，主体保持构图中心且背景换面。"),
        "first_frame": (
            "七分身；摄影机位于主体圆心半径5米、方位角-45°、高度"
            "1.6米的位置，俯角0°；主体中心为x=50%、高度约5"
            "0%；视平线距顶边50%；背景为场景正面与低位遮挡物。"),
        "last_frame": (
            "全景；摄影机位于主体圆心半径5米、方位角+45°、高度4"
            ".5米的位置，俯角约-25°并指向主体；主体中心仍为x="
            "50%、高度约38%；视平线距顶边18%；背景为场景侧后"
            "方，地面布局和高位空间清晰可见。"),
    },
    "穿越": {
        "no_delta": False,
        "framing_delta": (
            "尾帧比首帧紧5档；摄影机跨越开口平面4米，开口边缘由四边"
            "可见变为全部出画，主体放大且新空间可见范围覆盖全画面。"),
        "first_frame": (
            "远景；摄影机位于门洞外侧2米、机高1.6米，光轴穿过开口"
            "中心；门框四边全部可见，外围墙面占画面约35%；开口占画"
            "面宽度约55%；新空间内主体高度约22%、中心为x=50"
            "%；视平线距顶边50%。"),
        "last_frame": (
            "中近景；摄影机位于同一光轴上、门洞内侧2米，机高和焦段不"
            "变；门框四边全部位于画外，新空间占满画面；同一主体高度约"
            "65%、中心仍为x=50%；视平线距顶边50%；可见新空"
            "间两侧背景。"),
    },
    "俯冲": {
        "no_delta": False,
        "framing_delta": (
            "尾帧比首帧紧6档；机高降低10.2米、水平距离缩短7米，"
            "视平线由画外上方落至顶边下42%，主体显著放大且落点不越"
            "过主体。"),
        "first_frame": (
            "大远景；摄影机高度12米、与主体水平距离10米、俯角-5"
            "5°，光轴指向主体；主体高度约10%、中心为x=50%、"
            "y=62%；视平线位于画外上方；地面占画面约85%，可见"
            "主体周边大范围地形。"),
        "last_frame": (
            "中近景；摄影机高度1.8米、与主体水平距离3米、俯角-5"
            "°，光轴仍指向主体且机位停在主体前方；主体高度约65%、"
            "中心为x=50%、y=52%；视平线距顶边42%；地面占"
            "画面下部约45%，主体后方环境仍可见。"),
    },
    "升格": {
        "no_delta": True,
        "framing_delta": (
            "no_delta=true；升格只改变时间采样与动作速度"
            "，不构成摄影机取景差，首尾允许动作相位差但禁止镜头几何差"
            "。"),
        "first_frame": (
            "按镜头指定景别生成动作起始关键帧；摄影机位置、机高、焦段"
            "和光轴锁定，主体中心坐标、画面高度、视平线及背景边界作为"
            "基准；发丝、衣料、水珠或尘埃可处于明确动作相位。"),
        "last_frame": (
            "摄影机位置、机高、焦段、光轴、景别、主体中心坐标、主体画"
            "面高度、视平线及背景边界与首帧相同；主体与细小物体可采用"
            "另一动作相位，人物身份特征保持一致。"),
    },
    "希区柯克变焦": {
        "no_delta": False,
        "framing_delta": (
            "景别差0档、主体大小与位置差0、视平线差0；机距由5米减"
            "至2米且焦段由85mm反向变为35mm，背景可见范围和透"
            "视尺度明显改变。"),
        "first_frame": (
            "近景；摄影机距主体5米，使用约85mm焦段，机高1.6米"
            "；主体高度约70%、中心为x=50%；视平线距顶边50%"
            "；背景视野较窄，远处物体显得较大且层次压缩。"),
        "last_frame": (
            "近景；摄影机沿视线轴位于距主体2米处，使用约35mm焦段"
            "，机高和光轴不变；主体高度仍约70%、中心仍为x=50%"
            "；视平线仍距顶边50%；背景视野更宽，远处物体显得更小，"
            "纵深间距明显拉伸。"),
    },
}


def movement_frame_delta(movement_text):
    """按运镜词取首尾帧取景差异规则(长词优先);无命中返回 None。"""
    text=str(movement_text or "")
    declared=next(
        (word for word in sorted(MOVEMENT_FRAME_DELTAS, key=len, reverse=True)
         if word and word in text), "")
    return MOVEMENT_FRAME_DELTAS.get(declared)


# 构图 → 主体在画面中的组织方式(图与视频通用)
COMPOSITION_GEOMETRY = {
    "三分法": "主体或其眼睛落在画面三等分线的交点附近,不居正中",
    "中心对称": "主体位于画面正中,左右元素基本对称,强调仪式感与压迫感",
    "框中框": "用门框、窗棂、帷幔、拱洞等前景结构在画面内再框住主体,"
              "形成画中画的包围感",
    "引导线": "画面内的道路、廊柱、屋脊、地砖缝等线条汇聚指向主体",
    "前景遮挡": "近处有虚化的前景物件局部遮挡画面边缘,制造纵深与偷窥感;"
                "遮挡不得盖住主体面部识别特征",
    "留白": "主体偏置一侧,另一侧留出大面积空白或虚化环境,承载情绪",
    "对角线": "主体或主要动势沿画面对角方向排布,产生不稳定与动感",
    "水平分割": "画面被地平线、桌面或水面明确横向分割为上下两块",
}


# 景别容量:在「画面可见真人严格共N人」的人数合同下,各景别最多能
# 完整容纳几名真人。与 SCALE_GEOMETRY 同源推导:特写肩线以下出画→
# 只装得下 1 人;近景胸口以上→ 2 人并肩已是极限;中景带双臂动作→
# 4 人;全景/远景不设限。景别与人数合同同级互斥时裁决体系只能熔断
# (rule_governance 条款(c)),所以必须在编译期就不让互斥合同诞生。
CAMERA_SCALE_CAPACITY = {
    "大特写": 1, "特写": 1, "近景": 2, "中近景": 2,
    "七分身": 3, "中景": 4, "膝上景": 4, "中全景": 6,
}
_CAPACITY_UPGRADE_ORDER = ("中景", "中全景", "全景", "远景")

# 「人物 + 不在其手上的道具」要同框表现空间关系(沈眉站在书案右侧、
# 银铃静止在书案上),取景必须同时装下人和那件道具。特写/近景只框
# 得住人物本身,这类合同必然执行不了——与人数容量同一病根:景别
# 分配不看本镜要表现什么。
_ANCHOR_SAFE_SCALES = frozenset(
    {"中景", "膝上景", "七分身", "中全景", "全景", "远景", "大远景"})


def enforce_spatial_anchor_scale(scale, anchor_count, allowed=None):
    """本镜要同框呈现的空间锚点(人物+离身道具)超过一个时放宽景别。

    anchor_count<=1 或未知时不动;人数容量走 enforce_scale_capacity,
    两者取更宽的那个由调用方按顺序应用即可。
    """
    try:
        count = int(anchor_count)
    except (TypeError, ValueError):
        return scale, ""
    scale_text = str(scale or "").strip()
    if count <= 1 or not scale_text or scale_text in _ANCHOR_SAFE_SCALES:
        return scale, ""
    candidates = [
        value for value in _CAPACITY_UPGRADE_ORDER
        if not allowed or value in allowed]
    if not candidates:
        return scale, ""
    upgraded = candidates[0]
    note = (
        f"空间锚点修正:本镜需同框呈现 {count} 个空间锚点"
        f"(人物与离身道具的位置关系),{scale_text}装不下,已升档为"
        f"{upgraded}")
    return upgraded, note


def scale_capacity(scale):
    """该景别在全员必见合同下的最大真人数;未知景别视为不设限。"""
    return CAMERA_SCALE_CAPACITY.get(str(scale or "").strip(), 10 ** 6)


def allows_partial_multi_subject_scale(framing_text, visible_count):
    """明确的局部/双人紧景别不套用“完整人物容量”。

    ``visible_figure_count`` 是画面里能辨认来源的真人实例数，不是从
    头到脚完整入画的人数。两个人各露一只手仍然必须登记为 2 人，供
    身份与人数质检使用；但不能因此把手腕大特写强制改成中景。

    这里只接受强导演证据，避免一个宽泛的“局部焦点”把真正的三人
    完整特写放过去：
    - 明确双人贴面/双人紧特写（最多两人）；或
    - 明确指定手、腕、眼等局部，且为紧景别；或
    - 明确说所有人物只以局部入画、完整人物/其余身体出画。
    """
    try:
        count = int(visible_count)
    except (TypeError, ValueError):
        return False
    if count <= 1:
        return False
    text = str(framing_text or "")
    if not text:
        return False

    def positive_requirement(pattern):
        """匹配正向取景要求，忽略“不要求/禁止/无需”等否定句。"""
        for match in re.finditer(pattern, text):
            prefix = text[max(0, match.start() - 12):match.start()]
            if re.search(
                    r"(?:不|无|未|禁止|不得|严禁|避免|无需)"
                    r"(?:要求|允许|呈现|显示)?[^，。；]{0,6}$",
                    prefix):
                continue
            return True
        return False

    # 明确要求完整人物/全身同框时绝不豁免。先判矛盾合同，保证后面的
    # “局部/双人特写”提示不能掩盖同时出现的全身要求。
    full_body_required = positive_requirement(
        r"(?:全部人物|所有人物|全员|每人|任何一人|两人|二人|两名人物|"
        r"三人|三名人物|多人).{0,10}"
        r"(?:全身|完整身体|完整人形|完整人物|从头到脚|头顶到脚底)"
        r".{0,10}(?:入画|入框|同框|可见|画面中)")
    complete_faces_required = bool(
        count >= 3 and positive_requirement(
            r"(?:三人|三名人物|多人|全员).{0,10}"
            r"(?:完整面孔|完整脸部|正脸清晰|清晰同框)"
            r".{0,10}(?:入画|入框|同框|可见|画面中)"))
    if full_body_required or complete_faces_required:
        return False

    tight_scale = any(token in text for token in (
        "大特写", "特写", "近景", "中近景"))
    if not tight_scale:
        return False

    explicit_all_partial = bool(re.search(
        r"(?:均|全部|所有|两名|三名|人物).*?(?:只|仅).*?"
        r"(?:局部|手|腕|肩臂|肢体).*?(?:入画|入框)", text))
    explicit_crop_out = (
        any(token in text for token in (
        "完整人形出画", "完整人物出画", "完整人物明确出画",
        "不出现任何完整人形", "不呈现任何完整人物",
        "不完整呈现任何人物",
        "不要求完整人形", "不允许任何一人完整入框",
        "其余身体明确出画", "其余身体始终出画",
        "头部、面部、躯干", "头部、面部、眼睛"))
        or bool(re.search(r"不要求.{0,12}完整入框", text)))
    enumerated_local_parts = bool(
        ("仅框入" in text or "只框入" in text)
        and len(re.findall(r"局部", text)) >= 2)
    if (explicit_all_partial or enumerated_local_parts) and explicit_crop_out:
        return True

    # 两张脸/过肩前景也是两名“可见真人”，但不是两具完整人物。
    # 只对明确写成双人紧景别的两人镜放行，三人及以上仍按容量闸门。
    explicit_two_subject_closeup = bool(
        count == 2
        and ("双人" in text or "两张脸" in text)
        and any(token in text for token in (
            "贴面", "面部", "过肩", "大特写", "特写", "近景")))
    if explicit_two_subject_closeup:
        return True

    # 细节插入镜通常只看两个人相互作用的手/腕。限制为两人且要求
    # 紧景别与明确的解剖局部，单独出现“局部锐利焦点”不会命中。
    explicit_detail_insert = bool(
        count == 2
        and any(token in text for token in (
            "手部大特写", "双手大特写", "手腕大特写", "腕部大特写",
            "手部特写", "腕部特写", "腕部插入特写", "掌纹特写",
            "眼部特写", "嘴部特写", "肩臂局部特写")))
    return explicit_detail_insert


def enforce_scale_capacity(scale, visible_count, allowed=None):
    """景别装不下必见人数时升到最近的可行档;返回 (执行景别, 修正说明)。

    只升不降:人数合同(严格共N人)不可被景别豁免,可行的唯一方向是
    放宽取景。visible_count 缺失/非法时不动——宁可漏修不可误改。
    """
    try:
        count = int(visible_count)
    except (TypeError, ValueError):
        return scale, ""
    if count <= 0 or scale_capacity(scale) >= count:
        return scale, ""
    candidates = [
        value for value in _CAPACITY_UPGRADE_ORDER
        if scale_capacity(value) >= count
        and (not allowed or value in allowed)]
    if not candidates:
        return scale, ""
    upgraded = candidates[0]
    note = (
        f"景别容量修正:{scale}最多完整容纳{scale_capacity(scale)}人,"
        f"本镜人数合同要求{count}人全部可见,已升档为{upgraded}")
    return upgraded, note


# 构图容量:这些构图按定义需要环境结构或画面纵深进入取景,肩线以下
# 全部出画的紧景别根本装不下。旧版把景别与构图各自轮换分配,编译出
# 「大特写 + 框中框」这种几何上不可能的合同:模型只能拉宽成中景去
# 满足框中框,再被质检判「景别严重不符」——《长夏记事》8/8 关键帧
# 全灭同一原因。构图是次要审美,景别是导演意图,冲突时让构图。
ENVIRONMENT_COMPOSITIONS = {
    "框中框": "需要门框/窗棂/帷幔等环境结构入画",
    "引导线": "需要画面内线条汇聚指向主体",
    "水平分割": "需要地平线/桌面/水面横向分割画面",
    "对角线": "需要画面对角方向的空间动势",
}
# 任何景别都成立的安全构图,按优先级回落。
_SAFE_COMPOSITIONS = ("三分法", "中心构图", "中心对称", "留白")
# 环境类构图至少需要这些景别之一。
_ENVIRONMENT_SAFE_SCALES = frozenset(
    {"中景", "膝上景", "七分身", "中全景", "全景", "远景", "大远景"})


# 机位容量:过肩/反打按定义需要「前景一个人的后脑肩背 + 远端另一个
# 人物」,单人镜头根本构不成这种关系。盲轮换把过肩配给独角戏时,模型
# 只能画成普通正面,再被质检判「过肩关系缺失」——与景别容量、构图
# 容量同一病根:镜头维度分配不看本镜有几个人。
MULTI_ACTOR_POSITIONS = {
    "过肩": "需要前景一名人物的后脑肩背与远端另一名人物",
    "反打": "需要对话双方分处两个互补机位",
}
_SINGLE_ACTOR_POSITIONS = ("斜侧", "侧面", "正面", "四分之三面")


def enforce_position_capacity(position, visible_count, allowed=None):
    """单人镜头拿到过肩/反打时换成单人成立的机位。

    visible_count 未知或已达 2 人时不动;宁可漏修不可误改。
    """
    pos_text = str(position or "").strip()
    if pos_text not in MULTI_ACTOR_POSITIONS:
        return position, ""
    try:
        count = int(visible_count)
    except (TypeError, ValueError):
        return position, ""
    if count >= 2:
        return position, ""
    fallback = next(
        (value for value in _SINGLE_ACTOR_POSITIONS
         if not allowed or value in allowed), "")
    if not fallback:
        return position, ""
    note = (
        f"机位容量修正:{pos_text}{MULTI_ACTOR_POSITIONS[pos_text]},"
        f"本镜可见真人{count}人构不成该关系,已改用{fallback}")
    return fallback, note


def enforce_composition_scale(scale, composition, allowed=None):
    """紧景别装不下环境类构图时换掉构图;返回 (执行构图, 修正说明)。

    只动构图不动景别:景别承载导演的情绪意图(要看清银铃的浅刻纹),
    构图是可替换的次要审美。allowed 给定时只在该集合里回落。
    """
    scale_text = str(scale or "").strip()
    comp_text = str(composition or "").strip()
    if comp_text not in ENVIRONMENT_COMPOSITIONS:
        return composition, ""
    if not scale_text or scale_text in _ENVIRONMENT_SAFE_SCALES:
        return composition, ""
    fallback = next(
        (value for value in _SAFE_COMPOSITIONS
         if not allowed or value in allowed), "")
    if not fallback:
        return composition, ""
    note = (
        f"构图容量修正:{comp_text}{ENVIRONMENT_COMPOSITIONS[comp_text]},"
        f"{scale_text}装不下,已改用{fallback}")
    return fallback, note


# 场景母版视角集:key → (中文名, 机位描述)。反打/侧向以主视角图为
# 参考链式生成,保证同一空间在不同机位下结构一致。
SCENE_VIEWS = {
    "main": ("主视角", "建立镜头的默认机位"),
    "reverse": ("反打视角", "从主视角正对面的机位回看同一空间"),
    "side": ("侧向视角", "与主视角成约90度的侧向机位"),
}


def scene_view_for_camera(camera):
    """镜头机位 → 最贴近的场景母版视角 key。

    接受结构化 camera dict(取「机位」字段)或原始镜头文本;
    未命中一律回主视角,绝不因视角判断阻断出图。
    场景做过四向扩展时左右侧向是两张不同的图,机位写明左/右就各归各位;
    没写明方向仍回通用 side(调用方按回退链找实际存在的那张)。
    """
    if isinstance(camera, dict):
        text = str(camera.get("机位") or "")
    else:
        text = str(camera or "")
    if any(token in text for token in ("背面", "背后", "过肩", "反打")):
        return "reverse"
    if any(token in text for token in ("侧面", "侧脸", "侧向")):
        return "side_left" if "左" in text else "side"
    return "main"


# 顶拍(垂直向下)语境:机位词描述的是躯干朝向,不是面部可见性——
# 「顶拍+正面」若同时断言"只见头顶"与"双眼可见",物理互斥必熔断。
TOP_DOWN_POSITION_GEOMETRY = {
    "正面": "顶拍语境:人物躯干腹面朝上、头在画面上方脚在下方的正躺/"
            "仰面朝向;眼部判据不适用",
    "背面": "顶拍语境:人物躯干背面朝上(俯卧/背对天空)的朝向;"
            "不出现面部",
    "侧面": "顶拍语境:人物侧躺或侧向站位,躯干长轴与画面一侧平行",
    "过肩": "顶拍语境:前景近端为一人头顶与双肩,主体在其下方画面中",
}


def camera_geometry_clause(camera):
    """结构化镜头(dict,含 景别/角度/机位)→ 可核验几何条款。

    只翻译词典命中的术语;「按分镜」「保持轴线」等默认占位不产出
    条款。无任何命中返回空串,调用方按无此行处理。
    角度=顶拍时,机位几何切换到躯干朝向语义,避免与"只见头顶"互斥。
    """
    camera = camera if isinstance(camera, dict) else {}
    angle_value = str(camera.get("角度") or "").strip()
    position_table = (TOP_DOWN_POSITION_GEOMETRY
                      if angle_value == "顶拍" else POSITION_GEOMETRY)
    parts = []
    for field, table in (("景别", SCALE_GEOMETRY),
                         ("角度", ANGLE_GEOMETRY),
                         ("机位", position_table),
                         ("运镜", MOVEMENT_GEOMETRY),
                         ("构图", COMPOSITION_GEOMETRY)):
        value = str(camera.get(field) or "").strip()
        rule = table.get(value)
        if rule:
            parts.append(f"{value}={rule}")
    if not parts:
        return ""
    return "按可见特征执行并核验:" + "；".join(parts)
