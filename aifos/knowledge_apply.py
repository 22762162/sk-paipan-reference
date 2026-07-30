"""把知识大脑里已激活的条目真正送进生产。

2026-07-31 查证:知识大脑(`knowledge_brain.py`)建好后一直没接线——
`resolve` 的调用点只有 Web UI 那个手动搜索框和单元测试,`workflow.py` /
`director.py` / `prompt_contract.py` 一次都没调。条目激活了也进不了片子,
只是躺在库里等人去查。本模块补的就是这一段断线。

**两条送法,必须分清,否则就是提示词膨胀:**

- **写作类能力**(编剧 script、分镜 storyboard、画面质检 image_qc):接收方
  是会推理的 LLM,把知识的原则、执行与质检当工作说明送过去是对的。
- **图片/视频/配音能力**:接收方是生成模型,它不会读「先写清镜头目的」这种
  工作流指导,塞进去只会挤占本就紧张的提示词预算(参见提示词消融实测:
  零信息段落曾占整条提示词 36.5%)。这类知识必须由人改写进生产代码模块
  ——`motivated-lighting-contract` 之于 `lighting_language.py` 就是范例
  ——**不走本模块**。

所以下面的 `WRITING_CAPABILITIES` 是白名单而不是黑名单:新增能力默认不注入,
要注入得先想清楚接收方读不读得懂。
"""

# 只有这些能力的接收方是 LLM,才配收知识指令。
# capability -> 默认阶段(与 knowledge_brain 的 stages 枚举同口径)
WRITING_CAPABILITIES = {
    "script": "script",
    "storyboard": "storyboard",
    "image_qc": "review",
}

# 同为 script 能力,payload 上的开关决定它其实在干哪一阶段的活。
# 命中顺序即优先级,与 build_prompt 的分发顺序保持一致。
SCRIPT_STAGE_FLAGS = (
    ("prompt_refine", "cast"),
    ("asset_prompt", "text_assets"),
    ("prop_design", "text_assets"),
    ("shot_repair", "storyboard"),
    ("story_analysis", "script"),
    ("character_design", "cast"),
)

# 单条知识渲染上限与条数上限:知识是配料不是主菜,不能盖过本职提示词。
MAX_ENTRY_CHARS = 1200
MAX_ENTRIES = 4
MAX_TOTAL_CHARS = 3000
# 多要一些再自己裁:brain 的相关度算得很粗——阶段命中一律 +5、任务类型
# +4,查询词只有整串命中 triggers 才加分,于是同阶段的条目常常全部并列
# 5 分,谁进前 N 名基本由插入顺序决定。多取几条再按下面的规则重排,
# 免得阶段专属知识被 cross_stage 的通用条目挤掉。
RESOLVE_LIMIT = 8

# 拼检索词时只取这些字段:它们描述"这一镜/这个人物在干什么",
# 正好对得上知识条目的 triggers。整个 script JSON 不能进来。
_QUERY_FIELDS = (
    "style", "location", "creative_direction", "feedback",
    "blocking_reason", "asset_name", "character_name", "brief",
)


def stage_for(capability, payload=None):
    """能力(+payload 开关)→ 知识大脑的 stage;不该注入的返回空串。"""
    capability = str(capability or "").strip()
    if capability not in WRITING_CAPABILITIES:
        return ""
    payload = payload or {}
    if capability == "script":
        for flag, stage in SCRIPT_STAGE_FLAGS:
            if payload.get(flag):
                return stage
    return WRITING_CAPABILITIES[capability]


def query_for(payload):
    """从 payload 里凑一条短检索词,用于给知识条目算相关度。"""
    payload = payload or {}
    parts = []
    for field in _QUERY_FIELDS:
        value = str(payload.get(field) or "").strip()
        if value:
            parts.append(value)
    shot = payload.get("shot")
    if isinstance(shot, dict):
        for field in ("camera", "description", "prompt"):
            value = str(shot.get(field) or "").strip()
            if value:
                parts.append(value)
    return " ".join(parts)[:600]


def rank(matches, stage):
    """阶段专属知识排在通用知识之前,其余保持 brain 给的相关度序。

    brain 的打分里"本阶段专用"和"哪儿都能用"同样是 +5 分。真要拿去用时
    这两者价值不等:只声明了 cross_stage 的通用条目应该垫底,否则它会把
    真正对口的那条挤出名额。Python 的 sort 是稳定的,同组内原序不变。
    """
    def generic(item):
        stages = set(((item.get("applicability") or {}).get("stages")) or [])
        return 0 if stage in stages else 1
    return sorted(matches, key=generic)


def _entry_block(item):
    """单条知识 → 送给写作 LLM 的指令段(原则/执行/质检三段)。"""
    content = item.get("content") or {}
    title = str(item.get("title") or "").strip()
    version = item.get("version") or 1
    lines = [f"·《{title}》v{version}"]
    for label, key in (("原则", "principles"), ("执行", "workflow"),
                       ("质检", "quality_gates")):
        values = [str(v).strip() for v in (content.get(key) or [])
                  if str(v).strip()]
        if values:
            lines.append(f"  {label}:" + "；".join(values))
    block = "\n".join(lines)
    if len(block) > MAX_ENTRY_CHARS:
        block = block[:MAX_ENTRY_CHARS].rstrip() + "…"
    return block


def directives(brain, capability, payload, warn=None):
    """检索并渲染本次调用该带上的知识指令;没有可用知识时返回空串。

    任何异常都吞掉并返回空串:知识是增益,不能因为它把生产跑挂。

    warn 是可选的 (message) 回调。resolve 会把标准指纹对不上的条目直接
    跳过(`skipped_stale`),而制作标准一升版,**全部**知识都会一次性对不上,
    于是"已激活的知识"无声无息地不再进片子——这正是接线最容易白做的地方。
    有跳过就喊一声,让人知道该去 refresh 对齐,而不是等成片变差才发现。
    """
    stage = stage_for(capability, payload)
    if not stage or brain is None:
        return ""
    try:
        result = brain.resolve(stage=stage, query=query_for(payload),
                               limit=RESOLVE_LIMIT)
    except Exception:            # noqa: BLE001 - 知识失败不许影响生产
        return ""
    stale = (result or {}).get("skipped_stale") or []
    if stale and warn:
        warn("知识条目与当前制作标准指纹对不上,已跳过(需 refresh 对齐): "
             + "、".join(str(key) for key in stale[:5]))
    matches = rank(result.get("matches") or [], stage)
    if not matches:
        return ""
    blocks = []
    total = 0
    for item in matches[:MAX_ENTRIES]:
        block = _entry_block(item)
        if total + len(block) > MAX_TOTAL_CHARS:
            break
        blocks.append(block)
        total += len(block)
    if not blocks:
        return ""
    return (
        "【知识大脑·已验证方法】以下条目由研究室实测入库并经人工激活,"
        "适用于本阶段。按它执行,并让产出满足其中的质检项;"
        "与本项目制作标准冲突时以制作标准为准。\n"
        + "\n".join(blocks))


def attach(brain, capability, payload, warn=None):
    """把知识指令挂到 payload 上(原地改),返回是否挂上。

    走 payload 而不是让适配器自己查库:CLI 桥是子进程,payload 会被序列化
    成 JSON 传过去,子进程里没有 db 句柄。
    """
    if not isinstance(payload, dict):
        return False
    block = directives(brain, capability, payload, warn=warn)
    if not block:
        return False
    payload["knowledge_directives"] = block
    return True
