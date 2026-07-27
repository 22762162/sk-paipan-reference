"""真实感语言:自 sk-zhengren 技能移植的人物真实感提示词层。

AIFOS 的人物提示词锁身份、人数、服装,但没有任何一层管"皮肤像不像
真人、眼睛有没有神"——塑料脸、死眼、瓷白眼白、绘画感头发正是
写实项目抽卡废片的高频来源。本模块把 sk-zhengren 的分层真实感
合同压缩成两行可注入条款(正向判据 + 负面控制)。

只对写实/半写实画风启用;Q版、二次元、水墨等画风自动跳过,
不污染非写实项目。零依赖叶子模块。
"""

REALISTIC_STYLE_TOKENS = (
    "写实", "真人", "实拍", "电影", "半写实", "3D", "photoreal",
    "realistic", "cinematic", "live-action",
)
NON_REALISTIC_TOKENS = (
    "Q版", "q版", "卡通", "二次元", "漫画风", "手绘", "水墨",
    "像素", "chibi", "赛璐璐", "扁平",
)

REALISM_CLAUSE = (
    "【真实感】皮肤保留毛孔、细小绒毛与轻微自然油光,肤色有冷暖过渡,"
    "禁止均匀磨皮的蜡面;眼神有明确注视目标与神采,瞳孔有虹膜纹理,"
    "眼白非瓷白,眼部高光位置与主光源一致;鼻翼-脸颊、法令区、"
    "下颌-颈部衔接有真实体积;发际线与头皮连接自然,允许少量碎发;"
    "暗部保留细节,光影方向全画面一致")

NEGATIVE_CONTROLS = (
    "【真实感负面】禁止:塑料或蜡质皮肤、过度磨皮、无神死眼、"
    "纯黑圆片瞳孔、瓷白眼白、绘画感成绺头发、耳部结构变形、"
    "手指增减或畸形、为凑质感堆砌的假纹理")


def realism_applicable(style):
    """写实/半写实画风才注入;显式非写实画风一律跳过。"""
    text = str(style or "")
    if any(token in text for token in NON_REALISTIC_TOKENS):
        return False
    return any(token in text for token in REALISTIC_STYLE_TOKENS)


def realism_lines(style):
    """返回应追加进人物类提示词的真实感条款(不适用时为空列表)。"""
    if not realism_applicable(style):
        return []
    return [REALISM_CLAUSE, NEGATIVE_CONTROLS]
