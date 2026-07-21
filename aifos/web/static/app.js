/* AIFOS V3.2 工作台:生产总览 + 制作标准中心 + 分镜画布(原生 JS,零依赖) */
"use strict";

const app = document.getElementById("app");
const topbarRight = document.getElementById("topbar-right");
let pollTimer = null;
let standardsDraft = null;
let standardsBaseline = null;
let standardsMeta = null;
let standardsDirty = false;

/* ---------- 工具 ---------- */
const esc = (s) => String(s ?? "").replace(/[&<>"']/g,
  (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));
const fmt = (n, d = 2) => n == null ? "-" : Number(n).toFixed(d);

async function api(path, opts) {
  const res = await fetch(path, opts);
  const data = await res.json().catch(() => ({}));
  if (!res.ok) {
    const error = new Error(data.message || data.error || `HTTP ${res.status}`);
    error.details = data;
    throw error;
  }
  return data;
}

/* 页面内置提示(不用 alert/confirm——沙箱环境会静默拦截弹窗) */
function showToast(message, kind = "info") {
  document.querySelectorAll(".toast").forEach((t) => t.remove());
  const toast = document.createElement("div");
  toast.className = `toast ${kind}`;
  toast.setAttribute("role", kind === "error" ? "alert" : "status");
  toast.textContent = message;
  document.body.appendChild(toast);
  setTimeout(() => toast.remove(), 5000);
}

/* 二次点击确认:第一次点击进入待确认态,3 秒内再点执行 */
function armConfirm(btn, label, action) {
  if (btn.dataset.armed === "1") {
    btn.dataset.armed = "";
    btn.textContent = btn.dataset.original;
    action();
    return;
  }
  btn.dataset.armed = "1";
  btn.dataset.original = btn.textContent;
  btn.textContent = `再点一次确认${label}`;
  setTimeout(() => {
    if (btn.dataset.armed === "1") {
      btn.dataset.armed = "";
      btn.textContent = btn.dataset.original;
    }
  }, 3000);
}

const STATUS_CN = {
  done: "完成", failed: "失败", qc_failed: "质检未过", created: "已建",
  awaiting_confirm: "待确认", script: "剧本中", continuity: "锁连续性",
  cast: "画人物场景", storyboard: "五维分镜中", images: "关键帧中",
  text_assets: "锁文字", frames: "首尾帧", preflight: "门禁检查",
  videos: "视频中", voices: "声音/口型", edit: "剪辑中",
  qc: "质检中", package: "包装中", archive: "沉淀中", running: "制作中",
};
function chip(status) {
  const cls = ["done", "failed", "qc_failed", "awaiting_confirm"].includes(status)
    ? status : "running";
  return `<span class="chip ${cls}">${esc(STATUS_CN[status] || status)}</span>`;
}

/* ---------- 路由 ---------- */
function route() {
  if (pollTimer) { clearInterval(pollTimer); pollTimer = null; }
  const standards = location.hash.match(/^#\/standards(?:\/([a-z_]+))?$/);
  const m = location.hash.match(/^#\/episode\/(\d+)$/);
  const settings = location.hash === "#/settings";
  const area = standards ? "standards" : settings ? "settings" : "dashboard";
  document.querySelectorAll(".main-nav a").forEach((link) => {
    const active = link.dataset.nav === area;
    link.classList.toggle("active", active);
    if (active) link.setAttribute("aria-current", "page");
    else link.removeAttribute("aria-current");
  });
  if (standards) renderStandards(standards[1] || "production");
  else if (m) renderCanvasView(Number(m[1]));
  else if (settings) renderSettings();
  else renderDashboard();
}

/* ================= 制作标准中心 ================= */
const STANDARD_SECTIONS = [
  {
    id: "production", label: "基础生产", icon: "01",
    blurb: "决定视频模型、画质、声音、字幕与每个生成单元的基本边界。",
    fields: [
      { path: "name", label: "标准名称", help: "用于新剧集快照、版本历史和交付包标识。" },
      { path: "description", label: "标准说明", help: "写清适用题材、画面目标和团队共识。" },
      { path: "profile_key", label: "标准标识", locked: true, help: "版本链的稳定标识，保存后不可更换。" },
      { path: "rules.production.video_model", label: "视频模型", locked: true,
        help: "锁定 Seedance 2.0 Fast VIP；遇到真人脸限制时暂停，不静默切普通 VIP。" },
      { path: "rules.production.resolution", label: "输出分辨率", locked: true,
        help: "当前标准锁定 720P，避免不同阶段分辨率漂移。" },
      { path: "rules.production.voice", label: "对白声音", locked: true,
        help: "Seedance2 在视频单元内同步生成角色人声与口型；豆包 TTS 仅作为无声视频兼容模式的备选。" },
      { path: "rules.production.lip_sync", label: "即梦对口型", type: "boolean", locked: true,
        help: "硬规则：必须开启。" },
      { path: "rules.production.burn_subtitles", label: "烧录对白字幕", type: "boolean", locked: true,
        help: "硬规则：保持关闭，交付无字幕母版。" },
      { path: "rules.production.text_lock_provider", label: "文字关键帧 Provider",
        help: "画面出现手机屏、合同、门牌等可读文字时，先用关键帧逐字锁定。" },
      { path: "rules.production.fast_vip_real_face_conflict", label: "真人脸受限策略", locked: true,
        help: "固定为 pause_for_confirmation，需要人工决定，不自动降级模型。" },
    ],
  },
  {
    id: "timing", label: "分段节奏", icon: "02",
    blurb: "控制镜头时长、生成粒度和长台词拆分，直接影响节奏与生成成本。",
    fields: [
      { path: "rules.production.preferred_segment_seconds", label: "建议单元时长", type: "range", min: 1, max: 15, step: .5, unit: "秒",
        help: "优先把一个动作或一句台词控制在这个区间。" },
      { path: "rules.production.max_segment_seconds", label: "单元最长时长", type: "number", min: 5, max: 15, step: .5, unit: "秒",
        help: "任何 Seedance 单元不得超过此上限。" },
      { path: "rules.production.time_precision_seconds", label: "时间码精度", type: "number", min: .5, max: 2, step: .5, unit: "秒",
        help: "镜头时长会向上对齐到该粒度。" },
      { path: "rules.dialogue.max_chars_per_shot", label: "单镜台词上限", type: "number", min: 8, max: 25, step: 1, unit: "字",
        help: "超过上限时优先在逗号、句号、问号等自然停顿处拆镜。" },
      { path: "rules.dialogue.preserve_verbatim", label: "台词逐字保真", type: "boolean",
        help: "禁止改写、缩写、意译或同义替换。" },
      { path: "rules.dialogue.split_at_natural_pause", label: "只在自然停顿拆分", type: "boolean",
        help: "避免把词组从中间切断。" },
    ],
  },
  {
    id: "dialogue", label: "台词与表演", icon: "03",
    blurb: "语速跟随情绪，台词后给听者反应，高点给演员留白。",
    fields: [
      { path: "rules.dialogue.speech_profiles.tense_angry.chars_per_second", label: "紧张/愤怒语速", type: "range", min: 1, max: 10, step: .5, unit: "字/秒" },
      { path: "rules.dialogue.speech_profiles.tense_angry.buffer_seconds", label: "紧张/愤怒缓冲", type: "range", min: 0, max: 3, step: .1, unit: "秒" },
      { path: "rules.dialogue.speech_profiles.daily.chars_per_second", label: "日常语速", type: "range", min: 1, max: 10, step: .5, unit: "字/秒" },
      { path: "rules.dialogue.speech_profiles.daily.buffer_seconds", label: "日常缓冲", type: "range", min: 0, max: 3, step: .1, unit: "秒" },
      { path: "rules.dialogue.speech_profiles.sad_gentle.chars_per_second", label: "悲伤/温柔语速", type: "range", min: 1, max: 10, step: .5, unit: "字/秒" },
      { path: "rules.dialogue.speech_profiles.sad_gentle.buffer_seconds", label: "悲伤/温柔缓冲", type: "range", min: 0, max: 3, step: .1, unit: "秒" },
      { path: "rules.dialogue.speech_profiles.trembling.chars_per_second", label: "颤抖/哽咽语速", type: "range", min: 1, max: 10, step: .5, unit: "字/秒" },
      { path: "rules.dialogue.speech_profiles.trembling.buffer_seconds", label: "颤抖/哽咽缓冲", type: "range", min: 0, max: 3, step: .1, unit: "秒" },
      { path: "rules.performance.reaction_after_key_dialogue", label: "关键台词后补反应镜", type: "boolean",
        help: "有听者时，不能说完就切走。" },
      { path: "rules.performance.listener_duration_ratio", label: "听者反应镜最短比例", type: "number", min: .6667, max: 1, step: .01, unit: "×说话镜",
        help: "默认不少于说话者镜头的 2/3。" },
      { path: "rules.performance.reaction_seconds", label: "反应镜建议范围", type: "range", min: .5, max: 10, step: .5, unit: "秒", help: "作为常规范围；若与听者时长比例冲突，2/3 比例优先。" },
      { path: "rules.performance.beat_seconds", label: "情绪留白时长", type: "range", min: 2, max: 4, step: .5, unit: "秒" },
      { path: "rules.performance.physical_action_separate_shot", label: "重要肢体动作独立成镜", type: "boolean" },
      { path: "rules.performance.performance_goal_required", label: "逐镜表演目标必填", type: "boolean" },
    ],
  },
  {
    id: "continuity", label: "人物连续性", icon: "04",
    blurb: "锁定角色、服装、道具、空间和每段起止状态，避免人物与场景漂移。",
    fields: [
      { path: "rules.continuity.on_stage_characters_only", label: "禁止新增路人与复制人物", type: "boolean" },
      { path: "rules.continuity.character_count_lock", label: "逐镜人物数量锁定", type: "boolean" },
      { path: "rules.continuity.end_state_to_next_start", label: "首尾状态必须完整继承", type: "boolean" },
      { path: "rules.continuity.canonical_entity_names", label: "同一实体全程同名", type: "boolean" },
      { path: "rules.continuity.costume_and_prop_lock", label: "服装与关键道具锁定", type: "boolean" },
      { path: "rules.continuity.scene_change_starts_new_segment", label: "换场景强制新段", type: "boolean" },
      { path: "rules.continuity.position_uses_frame_geometry", label: "站位使用画面几何", type: "boolean" },
      { path: "rules.continuity.state_labels", label: "角色状态字段", type: "list", locked: true,
        help: "每段节头和节尾都必须记录这些字段。" },
    ],
  },
  {
    id: "storyboard", label: "五维镜头", icon: "05",
    blurb: "把轻量分镜升级为可执行的摄影合同，而不是一段泛化提示词。",
    fields: [
      { path: "rules.storyboard.minimum_vertical_angles_per_segment", label: "每段纵向角度数量", type: "number", min: 2, max: 4, step: 1 },
      { path: "rules.storyboard.adjacent_shot_scale_jump_levels", label: "相邻景别最小跳级", type: "number", min: 1, max: 4, step: 1 },
      { path: "rules.storyboard.adjacent_camera_axis_change_degrees", label: "替代机位偏转", type: "number", min: 15, max: 180, step: 5, unit: "°" },
      { path: "rules.storyboard.environment_sound_required", label: "逐镜环境声必填", type: "boolean" },
      { path: "rules.storyboard.forbid_repeated_scale_and_angle", label: "禁止连续同景别同角度", type: "boolean" },
      { path: "rules.storyboard.visual_hook_required", label: "逐镜视觉钩子必填", type: "boolean" },
      { path: "rules.storyboard.eye_line_required", label: "逐镜视线关系必填", type: "boolean" },
      { path: "rules.storyboard.required_columns", label: "镜头合同字段", type: "list",
        help: "标准模板为 17 列：时间码、摄影、站位、视线、表演、音效与镜头功能。" },
      { path: "rules.storyboard.scene_type_words", label: "段落类型词", type: "list",
        help: "每段必须从类型词库中选择一个。" },
      { path: "rules.storyboard.shot_functions", label: "镜头功能词库", type: "list" },
    ],
  },
  {
    id: "text_audio", label: "文字与声音", icon: "06",
    blurb: "可读文字先锁关键帧；对白不做字幕条；环境声是氛围的核心层。",
    fields: [
      { path: "rules.production.text_lock_provider", label: "可读文字关键帧锁定方", locked: true },
      { path: "rules.delivery.no_burned_subtitles", label: "禁止画面对白字幕条", type: "boolean" },
      { path: "rules.delivery.subtitle_track_must_be_empty", label: "字幕轨必须为空", type: "boolean" },
      { path: "rules.delivery.external_voice_track_must_be_empty", label: "外部对白轨必须为空", type: "boolean" },
      { path: "rules.delivery.no_bgm", label: "母版不加 BGM", type: "boolean" },
      { path: "rules.delivery.environment_sound_required", label: "逐镜环境声必填", type: "boolean" },
      { path: "rules.delivery.html_review_board_required", label: "生成图文检查板", type: "boolean" },
      { path: "rules.delivery.content_review_required", label: "逐段内容复核", type: "boolean" },
      { path: "rules.delivery.delivery_verifier_required", label: "实跑交付检查脚本", type: "boolean" },
      { path: "rules.delivery.archive_standard_snapshot", label: "成品包归档标准快照", type: "boolean" },
    ],
  },
  { id: "gates", label: "门禁质检", icon: "07", blurb: "生产前逐项拦截；关闭门禁会降低交付可靠性。", fields: [] },
  { id: "camera", label: "镜头词库", icon: "08", blurb: "统一摄影词汇，避免模型收到互相冲突或不存在的运镜指令。", fields: [
    { path: "rules.camera_library.shot_scales", label: "景别词库", type: "list" },
    { path: "rules.camera_library.angles", label: "角度词库", type: "list" },
    { path: "rules.camera_library.positions", label: "机位词库", type: "list" },
    { path: "rules.camera_library.movements", label: "运镜词库", type: "list" },
    { path: "rules.camera_library.compositions", label: "构图词库", type: "list" },
    { path: "rules.camera_library.focal_lengths_mm", label: "焦段词库", type: "numberList" },
    { path: "rules.camera_library.speeds", label: "拍摄速度词库", type: "list" },
  ] },
  { id: "history", label: "版本历史", icon: "09", blurb: "每次保存创建不可变版本；历史版本可以一键重新激活。", fields: [] },
];

function getByPath(root, path) {
  return path.split(".").reduce((node, key) => node == null ? undefined : node[key], root);
}

function setByPath(root, path, value) {
  const keys = path.split(".");
  let node = root;
  keys.slice(0, -1).forEach((key) => {
    if (!node[key] || typeof node[key] !== "object") node[key] = {};
    node = node[key];
  });
  node[keys[keys.length - 1]] = value;
}

function cloneJson(value) { return JSON.parse(JSON.stringify(value)); }

function diffCount(a, b) {
  if (JSON.stringify(a) === JSON.stringify(b)) return 0;
  if (!a || !b || typeof a !== "object" || typeof b !== "object") return 1;
  const keys = new Set([...Object.keys(a), ...Object.keys(b)]);
  return [...keys].reduce((count, key) => count + diffCount(a[key], b[key]), 0);
}

function validateStandardDraft(draft) {
  const errors = [];
  const add = (path, message) => errors.push({ path, message });
  const p = draft?.rules?.production || {};
  const d = draft?.rules?.dialogue || {};
  const perf = draft?.rules?.performance || {};
  const preferred = p.preferred_segment_seconds || [];
  if (preferred.length !== 2 || Number(preferred[0]) > Number(preferred[1]))
    add("rules.production.preferred_segment_seconds", "建议时长下限不能大于上限");
  if (Number(p.max_segment_seconds) < Number(preferred[1] || 0) || Number(p.max_segment_seconds) > 15)
    add("rules.production.max_segment_seconds", "最长时长需覆盖建议区间且不能超过 15 秒");
  if (Number(p.time_precision_seconds) <= 0)
    add("rules.production.time_precision_seconds", "时间精度必须大于 0");
  if (Number(d.max_chars_per_shot) < 8 || Number(d.max_chars_per_shot) > 25)
    add("rules.dialogue.max_chars_per_shot", "单镜台词上限应在 8–25 字之间");
  if (Number(perf.listener_duration_ratio) < (2 / 3) || Number(perf.listener_duration_ratio) > 1)
    add("rules.performance.listener_duration_ratio", "反应镜比例应在 2/3–1 之间");
  const beat = perf.beat_seconds || [];
  if (beat.length !== 2 || Number(beat[0]) > Number(beat[1]))
    add("rules.performance.beat_seconds", "留白时长下限不能大于上限");
  for (const [key, band] of Object.entries(d.speech_profiles || {})) {
    const speed = band.chars_per_second || [];
    if (Number(speed[0]) <= 0 || Number(speed[0]) > Number(speed[1]))
      add(`rules.dialogue.speech_profiles.${key}`, "语速最小值必须大于 0 且不高于最大值");
  }
  const hard = [
    [p.video_model === "seedance2.0fast_vip", "视频模型必须为 Seedance 2.0 Fast VIP"],
    [String(p.resolution).toLowerCase() === "720p", "分辨率必须为 720P"],
    [p.voice === "jimeng_builtin", "声音模式必须为 Seedance2 随视频配音"],
    [p.lip_sync === true, "即梦对口型必须开启"],
    [p.burn_subtitles === false, "无字幕母版不能烧录对白字幕"],
  ];
  hard.forEach(([ok, message]) => { if (!ok) add("rules.production", message); });
  return errors;
}

function renderStandardField(field) {
  const value = getByPath(standardsDraft, field.path);
  const id = `sf-${field.path.replace(/[^a-z0-9]+/gi, "-")}`;
  const helpId = `${id}-help`;
  const disabled = field.locked ? "disabled" : "";
  let control;
  if (field.type === "boolean") {
    control = `<label class="switch"><input id="${id}" type="checkbox" data-standard-path="${esc(field.path)}" ${value ? "checked" : ""} ${disabled}><span aria-hidden="true"></span><em>${value ? "开启" : "关闭"}</em></label>`;
  } else if (field.type === "range") {
    const range = Array.isArray(value) ? value : ["", ""];
    control = `<div class="range-control"><input id="${id}" type="number" data-standard-path="${esc(field.path)}" data-range-index="0" value="${esc(range[0])}" min="${field.min}" max="${field.max}" step="${field.step}" ${disabled} aria-describedby="${helpId}"><span>至</span><input type="number" data-standard-path="${esc(field.path)}" data-range-index="1" value="${esc(range[1])}" min="${field.min}" max="${field.max}" step="${field.step}" ${disabled} aria-label="${esc(field.label)}上限"><small>${esc(field.unit || "")}</small></div>`;
  } else if (field.type === "list" || field.type === "numberList") {
    control = `<textarea id="${id}" rows="3" data-standard-path="${esc(field.path)}" data-standard-type="${field.type}" ${disabled} aria-describedby="${helpId}">${esc(Array.isArray(value) ? value.join("、") : value || "")}</textarea>`;
  } else {
    const type = field.type === "number" ? "number" : "text";
    control = `<div class="unit-control"><input id="${id}" type="${type}" data-standard-path="${esc(field.path)}" value="${esc(value ?? "")}" ${field.min != null ? `min="${field.min}"` : ""} ${field.max != null ? `max="${field.max}"` : ""} ${field.step != null ? `step="${field.step}"` : ""} ${disabled} aria-describedby="${helpId}"><small>${esc(field.unit || "")}</small></div>`;
  }
  return `<div class="standard-field ${field.locked ? "locked" : ""}">
    <div class="field-copy"><label for="${id}">${esc(field.label)}</label>${field.locked ? `<span class="rule-badge hard">硬规则</span>` : `<span class="rule-badge adjustable">可调整</span>`}<p id="${helpId}">${esc(field.help || "保存后从下一集开始生效；已绑定剧集继续使用原快照。")}</p></div>
    <div class="field-control">${control}</div>
  </div>`;
}

function renderGatesEditor() {
  const gates = standardsDraft?.rules?.quality_gates || [];
  return `<div class="gate-editor">${gates.map((gate, index) => `
    <div class="gate-rule">
      <div><span class="gate-index">${String(index + 1).padStart(2, "0")}</span><b>${esc(gate.label || gate.id)}</b><p>${esc(gate.description || "生产前自动检查，未通过则停止消耗视频额度。")}</p></div>
      <div class="gate-controls"><label><span>失败级别</span><select data-gate-severity="${index}" aria-label="${esc(gate.label || gate.id)}失败级别"><option value="block" ${gate.severity !== "warning" ? "selected" : ""}>阻断开拍</option><option value="warning" ${gate.severity === "warning" ? "selected" : ""}>只警告</option></select></label><label class="switch"><input type="checkbox" data-gate-index="${index}" ${gate.enabled !== false ? "checked" : ""}><span aria-hidden="true"></span><em>${gate.enabled !== false ? "启用" : "停用"}</em></label></div>
    </div>`).join("") || `<div class="empty">当前标准没有门禁定义</div>`}</div>`;
}

function renderVersionHistory() {
  const activeId = standardsMeta?.active?.version_id;
  const history = standardsMeta?.history || [];
  return `<div class="version-list">${history.map((item) => `
    <article class="version-item ${item.version_id === activeId ? "active" : ""}">
      <div class="version-line"><b>v${esc(item.version)}</b>${item.version_id === activeId ? `<span class="rule-badge live">当前生效</span>` : ""}<time>${item.created_at ? new Date(item.created_at * 1000).toLocaleString("zh-CN") : ""}</time></div>
      <p>${esc(item.change_note || "创建制作标准版本")}</p>
      <code>${esc((item.fingerprint || "").slice(0, 12))}</code>
      ${item.version_id !== activeId ? `<button type="button" data-activate-version="${item.version_id}">恢复并激活此版本</button>` : ""}
    </article>`).join("")}</div>`;
}

function standardImpactHtml() {
  const rules = standardsDraft?.rules || {};
  const p = rules.production || {};
  const d = rules.dialogue || {};
  const perf = rules.performance || {};
  const columns = rules.storyboard?.required_columns || [];
  const enabledGates = (rules.quality_gates || []).filter((g) => g.enabled !== false).length;
  const preferred = p.preferred_segment_seconds || [5, 8];
  const averageSegment = (Number(preferred[0]) + Number(preferred[1])) / 2 || 6.5;
  const estimatedUnits = Math.ceil(60 / averageSegment);
  const changes = diffCount(standardsBaseline, standardsDraft);
  const errors = validateStandardDraft(standardsDraft);
  return `<div class="impact-card ${errors.length ? "has-errors" : ""}">
    <div class="impact-status"><span class="status-dot"></span><b>${errors.length ? `${errors.length} 项需要修正` : "规则结构有效"}</b></div>
    <dl>
      <div><dt>视频单元</dt><dd>${esc((p.preferred_segment_seconds || []).join("–"))}s · 最长 ${esc(p.max_segment_seconds)}s</dd></div>
      <div><dt>60秒基准量</dt><dd>约 ${estimatedUnits} 个叙事单元 + 反应/留白</dd></div>
      <div><dt>时间精度</dt><dd>${esc(p.time_precision_seconds)}s</dd></div>
      <div><dt>台词拆分</dt><dd>≤ ${esc(d.max_chars_per_shot)} 字/镜</dd></div>
      <div><dt>反应镜</dt><dd>≥ ${fmt((perf.listener_duration_ratio || 0) * 100, 0)}% 说话镜</dd></div>
      <div><dt>情绪留白</dt><dd>${esc((perf.beat_seconds || []).join("–"))}s</dd></div>
      <div><dt>镜头合同</dt><dd>${columns.length} 列字段</dd></div>
      <div><dt>开拍门禁</dt><dd>${enabledGates} 项启用</dd></div>
      <div><dt>交付声音</dt><dd>Seedance2 随视频配音/口型 · 无字幕母版</dd></div>
    </dl>
    <div class="change-summary">${changes ? `已修改 ${changes} 个值，尚未保存` : "与当前生效版本一致"}</div>
    ${errors.length ? `<ul class="validation-list">${errors.map((e) => `<li>${esc(e.message)}</li>`).join("")}</ul>` : ""}
  </div>`;
}

async function renderStandards(sectionId) {
  const section = STANDARD_SECTIONS.find((item) => item.id === sectionId) || STANDARD_SECTIONS[0];
  if (!standardsMeta || !standardsDirty) {
    try {
      standardsMeta = await api("/api/standards");
      standardsBaseline = cloneJson(standardsMeta.active.content);
      standardsDraft = cloneJson(standardsMeta.active.content);
      standardsDirty = false;
    } catch (e) {
      app.innerHTML = `<div class="loading">制作标准加载失败：${esc(e.message)}</div>`;
      return;
    }
  }
  const active = standardsMeta.active;
  topbarRight.innerHTML = `<span class="standard-live">标准 v${esc(active.version)} 生效中</span>`;
  const source = standardsDraft.source_skill || {};
  const skillManifest = section.id === "production" ? `
    <article class="skill-manifest">
      <div><span>SKILL SOURCE</span><b>${esc(source.name || "SK 漫剧五维分镜制作 Skill")}</b></div>
      <dl><div><dt>技能 ID</dt><dd>${esc(source.id || "sk-manju-storyboard-skill")}</dd></div><div><dt>模板</dt><dd>${esc(source.reference || "five-dimension-storyboard-template-v5.txt")}</dd></div></dl>
      <p>${esc(source.principle || "先完成五维分镜和硬门校验，再进入关键帧与 Seedance 生产。")}</p>
    </article>` : "";
  const content = section.id === "gates" ? renderGatesEditor()
    : section.id === "history" ? renderVersionHistory()
    : `${skillManifest}<div class="standard-fields">${section.fields.map(renderStandardField).join("")}</div>`;
  app.innerHTML = `<div class="standards-page">
    <header class="standards-hero">
      <div><div class="eyebrow">PRODUCTION STANDARD CENTER</div><h1 tabindex="-1">制作标准中心</h1><p>把漫剧 Skill、分镜模板和质检规则变成真正驱动生产的版本化合同。</p></div>
      <div class="standard-actions">
        <button type="button" id="standard-import">导入</button>
        <button type="button" id="standard-export">导出</button>
        <button type="button" id="standard-reset">恢复官方标准</button>
        <input type="file" id="standard-import-file" accept="application/json,.json" hidden>
      </div>
    </header>
    <section class="standard-identity">
      <div class="identity-main"><span class="live-pulse"></span><div><b>${esc(active.name)}</b><small>${esc(active.profile_key)} · v${esc(active.version)} · ${esc((active.fingerprint || "").slice(0, 12))}</small></div></div>
      <div class="skill-source"><span>规则来源</span><b>${esc(source.name || "sk-manju-storyboard-skill")}</b><small>${esc(source.version || source.spec_version || "五维分镜 V5")}</small></div>
      <div class="identity-stat"><span>保存策略</span><b>新版本 + 每集快照</b><small>历史剧集不随标准变化</small></div>
    </section>
    <div class="standards-layout">
      <nav class="standard-nav" aria-label="制作标准分类">${STANDARD_SECTIONS.map((item) => `<a href="#/standards/${item.id}" class="${item.id === section.id ? "active" : ""}" ${item.id === section.id ? `aria-current="page"` : ""}><em>${item.icon}</em><span>${item.label}</span></a>`).join("")}</nav>
      <section class="standard-editor" aria-labelledby="standard-section-title">
        <div class="section-heading"><div><span>${section.icon}</span><h2 id="standard-section-title">${esc(section.label)}</h2></div><p>${esc(section.blurb)}</p></div>
        ${content}
      </section>
      <aside class="impact-panel"><h2>生产影响预览</h2><p>这些值会进入新剧集的制作快照，并驱动分镜、门禁和交付。</p><div id="impact-preview">${standardImpactHtml()}</div></aside>
    </div>
    <footer class="standard-savebar">
      <div><span id="dirty-status">${standardsDirty ? "有未保存修改" : "当前无修改"}</span><input id="change-note" placeholder="版本说明，例如：动作戏反应镜延长"></div>
      <button type="button" class="primary" id="standard-save" ${validateStandardDraft(standardsDraft).length || !standardsDirty ? "disabled" : ""}>保存并生效</button>
    </footer>
  </div>`;
  bindStandards(section.id);
  requestAnimationFrame(() => app.querySelector("h1")?.focus());
}

function refreshStandardDraftUi() {
  standardsDirty = diffCount(standardsBaseline, standardsDraft) > 0;
  const preview = document.getElementById("impact-preview");
  if (preview) preview.innerHTML = standardImpactHtml();
  const status = document.getElementById("dirty-status");
  if (status) status.textContent = standardsDirty ? "有未保存修改" : "当前无修改";
  const save = document.getElementById("standard-save");
  if (save) save.disabled = validateStandardDraft(standardsDraft).length > 0 || !standardsDirty;
}

function downloadJson(data, filename) {
  const url = URL.createObjectURL(new Blob(
    [JSON.stringify(data, null, 2)], { type: "application/json" }));
  downloadUrl(url, filename);
  setTimeout(() => URL.revokeObjectURL(url), 1000);
}

function bindStandards(sectionId) {
  app.querySelectorAll("[data-standard-path]").forEach((input) => {
    input.addEventListener("input", () => {
      const path = input.dataset.standardPath;
      let value;
      if (input.type === "checkbox") value = input.checked;
      else if (input.dataset.standardType === "list")
        value = input.value.split(/[、,，\n]/).map((item) => item.trim()).filter(Boolean);
      else if (input.dataset.standardType === "numberList")
        value = input.value.split(/[、,，\n]/).map((item) => Number(item.trim())).filter((item) => Number.isFinite(item));
      else if (input.type === "number") value = Number(input.value);
      else value = input.value;
      if (input.dataset.rangeIndex != null) {
        const range = [...(getByPath(standardsDraft, path) || [0, 0])];
        range[Number(input.dataset.rangeIndex)] = value;
        setByPath(standardsDraft, path, range);
      } else setByPath(standardsDraft, path, value);
      const switchLabel = input.closest(".switch")?.querySelector("em");
      if (switchLabel) switchLabel.textContent = input.checked ? "开启" : "关闭";
      refreshStandardDraftUi();
    });
  });
  app.querySelectorAll("[data-gate-index]").forEach((input) => {
    input.addEventListener("change", () => {
      standardsDraft.rules.quality_gates[Number(input.dataset.gateIndex)].enabled = input.checked;
      input.closest(".switch").querySelector("em").textContent = input.checked ? "启用" : "停用";
      refreshStandardDraftUi();
    });
  });
  app.querySelectorAll("[data-gate-severity]").forEach((select) => {
    select.addEventListener("change", () => {
      standardsDraft.rules.quality_gates[Number(select.dataset.gateSeverity)].severity = select.value;
      refreshStandardDraftUi();
    });
  });
  document.getElementById("standard-save")?.addEventListener("click", async (ev) => {
    const btn = ev.currentTarget;
    btn.disabled = true; btn.textContent = "保存中…";
    try {
      const reply = await api("/api/standards/save", {
        method: "POST", headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ content: standardsDraft,
          change_note: document.getElementById("change-note").value.trim() || "调整制作标准",
          activate: true, expected_active_id: standardsMeta.active.version_id }),
      });
      standardsMeta = null; standardsDirty = false;
      showToast(`制作标准 v${reply.standard.version} 已保存并生效`, "ok");
      renderStandards(sectionId);
    } catch (e) {
      showToast(e.message, "error");
      btn.disabled = false; btn.textContent = "保存并生效";
    }
  });
  document.getElementById("standard-export")?.addEventListener("click", async () => {
    try {
      const bundle = await api(`/api/standards/export?version_id=${standardsMeta.active.version_id}`);
      downloadJson(bundle, `AIFOS_${standardsMeta.active.profile_key}_v${standardsMeta.active.version}.json`);
      showToast("制作标准已导出", "ok");
    } catch (e) { showToast(e.message, "error"); }
  });
  const fileInput = document.getElementById("standard-import-file");
  document.getElementById("standard-import")?.addEventListener("click", () => fileInput.click());
  fileInput?.addEventListener("change", async () => {
    try {
      const bundle = JSON.parse(await fileInput.files[0].text());
      const reply = await api("/api/standards/import", {
        method: "POST", headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ bundle, change_note: `导入 ${fileInput.files[0].name}`, activate: true }),
      });
      standardsMeta = null; standardsDirty = false;
      showToast(`已导入并激活 v${reply.standard.version}`, "ok");
      renderStandards(sectionId);
    } catch (e) { showToast(`导入失败：${e.message}`, "error"); }
    fileInput.value = "";
  });
  document.getElementById("standard-reset")?.addEventListener("click", (ev) => {
    armConfirm(ev.currentTarget, "恢复", async () => {
      try {
        const reply = await api("/api/standards/reset", {
          method: "POST", headers: { "Content-Type": "application/json" },
          body: JSON.stringify({ change_note: "恢复 SK 五维漫剧 V5 官方标准" }),
        });
        standardsMeta = null; standardsDirty = false;
        showToast(`已恢复并激活官方标准 v${reply.standard.version}`, "ok");
        renderStandards(sectionId);
      } catch (e) { showToast(e.message, "error"); }
    });
  });
  app.querySelectorAll("[data-activate-version]").forEach((btn) => {
    btn.addEventListener("click", async () => {
      try {
        const reply = await api("/api/standards/activate", {
          method: "POST", headers: { "Content-Type": "application/json" },
          body: JSON.stringify({ version_id: Number(btn.dataset.activateVersion) }),
        });
        standardsMeta = null; standardsDirty = false;
        showToast(`已激活制作标准 v${reply.standard.version}`, "ok");
        renderStandards("history");
      } catch (e) { showToast(e.message, "error"); }
    });
  });
}

/* ================= 仪表盘 ================= */
async function renderDashboard() {
  topbarRight.innerHTML = "";
  let data;
  try { data = await api("/api/overview"); }
  catch (e) { app.innerHTML = `<div class="loading">加载失败:${esc(e.message)}</div>`; return; }

  const s = data.stats;
  const runningJobs = data.jobs.filter((j) => j.status === "running");
  const activeStandard = data.production_standard || {};
  topbarRight.innerHTML = `<a class="standard-live" href="#/standards/history">标准 v${esc(activeStandard.version || 1)}</a>` + (runningJobs.length
    ? `<span class="chip running">${runningJobs.length} 个制作任务进行中</span>` : "");

  const maxStage = Math.max(1, ...data.cost_by_stage.map((r) => r.total || 0));
  app.innerHTML = `
  <div class="dash">
    <form class="produce-bar" id="produce-form">
      <div class="mode-tabs">
        <button type="button" class="mode-tab active" data-mode="ai">✨ AI 自动编剧</button>
        <button type="button" class="mode-tab" data-mode="script">📄 我有剧本</button>
      </div>
      <div class="kind-tabs">
        <span class="kind-label">内容类型</span>
        <button type="button" class="kind-tab active" data-kind="">自动识别</button>
        <button type="button" class="kind-tab" data-kind="drama">🎬 剧情短剧</button>
        <button type="button" class="kind-tab" data-kind="idol">💫 虚拟偶像/女团</button>
      </div>
      <input name="sentence" placeholder='写作品名就行,例如:苏念的一天(集数可不写,自动接着做下一集)' required>
      <input name="premise" placeholder="内容方向,例如:偶像女团 / 都市职场 / 仙侠(可不填)">
      <button class="primary" type="submit">开始制作</button>
      <textarea name="script" rows="5" hidden placeholder="把你的剧本粘贴到这里,人物、场次、分镜会自动识别。写法示例:
第1场 古镇长街
夜色渐深,妖气翻涌。
林昭:这股妖气不对劲。
小狐:小心,它就在附近!"></textarea>
      <div class="produce-hint">SK 工业流:连续性圣经 → 五维分镜 → 关键帧/文字锁定 → 生产门禁 → Seedance → 三层质检 → 交付脚本。</div>
    </form>
    <section class="workflow-map" aria-label="AIFOS 漫剧工业流">
      <div class="workflow-lead"><b>不把长剧本直接塞给视频模型</b><span>先锁定画面、人物与段间状态，再让 Seedance 只执行动作、镜头和情绪。</span><a href="#/standards/production">${esc(activeStandard.name || "SK 五维漫剧标准")} · v${esc(activeStandard.version || 1)} 正在驱动新剧集 →</a></div>
      <div class="workflow-steps">
        ${["连续性", "五维分镜", "文字关键帧", "首尾帧", "生产门禁", "视频/口型", "抽帧+内容复核", "交付脚本"].map((name, i) =>
          `<div class="workflow-step"><em>${String(i + 1).padStart(2, "0")}</em><span>${name}</span></div>`).join("")}
      </div>
    </section>
    <div id="progress-banner"></div>
    <div id="pipeline-strip"></div>

    <div class="tiles">
      <div class="tile"><div class="label">剧集总数</div><div class="value">${s.episodes}</div></div>
      <div class="tile"><div class="label">已完成</div><div class="value">${s.done}<small> / ${s.episodes}</small></div></div>
      <div class="tile"><div class="label">总成本</div><div class="value">${fmt(s.total_cost)}<small> 单集预算 ${fmt(s.budget, 0)}</small></div></div>
      <div class="tile"><div class="label">平均质检分</div><div class="value">${s.avg_qc == null ? "-" : fmt(s.avg_qc, 1)}</div></div>
      <div class="tile"><div class="label">制作任务</div><div class="value">${runningJobs.length}<small> 进行中</small></div></div>
    </div>

    <div class="panel">
      <h2>剧集 · 点击进入分镜画布</h2>
      ${data.episodes.length ? `
      <table><thead><tr><th>项目</th><th>集</th><th>状态</th><th class="num">质检</th><th class="num">成本</th></tr></thead>
      <tbody>${data.episodes.map((e) => `
        <tr class="clickable" data-ep="${e.id}">
          <td>${esc(e.project)}</td><td>第${e.number}集</td>
          <td>${chip(e.status)}</td>
          <td class="num">${e.qc_score == null ? "-" : fmt(e.qc_score, 0)}</td>
          <td class="num">${fmt(e.cost)}</td>
        </tr>`).join("")}</tbody></table>`
      : `<div class="empty">暂无剧集,输入一句话开始制作。</div>`}
    </div>

    <div class="grid-2">
      <div class="panel">
        <h2>成本 · 按阶段</h2>
        ${data.cost_by_stage.filter((r) => r.total > 0).map((r) => `
          <div class="bar-row">
            <span class="name">${esc(STAGE_CN[r.stage] || r.stage)}</span>
            <div class="bar-track"><div class="bar-fill" style="width:${(r.total / maxStage * 100).toFixed(1)}%"></div></div>
            <span class="val">${fmt(r.total)}</span>
          </div>`).join("") || `<div class="empty">暂无成本数据</div>`}
      </div>
      <div class="panel">
        <h2>Provider · 调用与额度</h2>
        ${data.cost_by_provider.length ? `
        <table><thead><tr><th>Provider</th><th class="num">调用</th><th class="num">成本</th></tr></thead>
        <tbody>${data.cost_by_provider.map((r) => `
          <tr><td>${esc(r.provider)}</td><td class="num">${r.calls}</td><td class="num">${fmt(r.total)}</td></tr>`).join("")}
        </tbody></table>` : `<div class="empty">暂无调用</div>`}
        ${data.quota.map((q) => `
          <div class="bar-row" style="margin-top:8px">
            <span class="name">${esc(q.provider)} 额度</span>
            <div class="bar-track"><div class="bar-fill" style="width:${(q.used / Math.max(1, q.quota_limit) * 100).toFixed(1)}%"></div></div>
            <span class="val">${q.used}/${q.quota_limit}</span>
          </div>`).join("")}
      </div>
    </div>

    <div class="panel">
      <h2>账号矩阵 · IP 资产沉淀</h2>
      ${Object.keys(data.asset_stats).length ? Object.entries(data.asset_stats).map(([proj, rows]) => {
        const p = data.projects.find((x) => x.title === proj) || {};
        const kindCN = { drama: "漫剧", idol: "AI虚拟偶像" }[p.kind] || p.kind || "";
        return `
        <div style="margin-bottom:8px"><b>${esc(proj)}</b>
          <span class="chip" style="margin-left:8px">${esc(kindCN)}</span>
          ${p.account ? `<span class="chip">@${esc(p.account)}</span>` : ""}
          <span class="chip">${esc(p.aspect || "9:16")}</span>
          <button class="mini-btn proj-rename" data-title="${esc(proj)}" title="修改项目名">✎ 改名</button>
        </div>
        <div class="asset-chips" style="margin-bottom:12px">
          ${rows.map((r) => `<span class="chip">${esc(KIND_CN[r.kind] || r.kind)} ×${r.total}${r.reused ? ` · 复用${r.reused}` : ""}</span>`).join("")}
        </div>`;
      }).join("") : `<div class="empty">暂无资产</div>`}
    </div>

    <div class="panel">
      <h2>最近日志</h2>
      <div class="log-list" id="log-list"><div class="empty">加载中…</div></div>
    </div>
  </div>`;

  const form = document.getElementById("produce-form");
  form.addEventListener("submit", onProduce);
  form.querySelectorAll(".mode-tab").forEach((tab) =>
    tab.addEventListener("click", () => {
      form.querySelectorAll(".mode-tab").forEach((t) =>
        t.classList.toggle("active", t === tab));
      form.script.hidden = tab.dataset.mode !== "script";
      if (tab.dataset.mode === "script") form.script.focus();
    }));
  form.querySelectorAll(".kind-tab").forEach((tab) =>
    tab.addEventListener("click", () => {
      form.querySelectorAll(".kind-tab").forEach((t) =>
        t.classList.toggle("active", t === tab));
      form.dataset.kind = tab.dataset.kind;
    }));
  renderProgressBanner(data);
  app.querySelectorAll("tr.clickable").forEach((tr) =>
    tr.addEventListener("click", () => { location.hash = `#/episode/${tr.dataset.ep}`; }));
  app.querySelectorAll(".proj-rename").forEach((btn) =>
    btn.addEventListener("click", () =>
      renameProject(btn.dataset.title, renderDashboard)));

  api("/api/doctor").then((doc) => {
    const el = document.getElementById("pipeline-strip");
    if (!el) return;
    el.innerHTML = `
      <div class="pipeline-row">
        <span class="pipeline-label">产线状态</span>
        ${doc.capabilities.map((c) => `
          <span class="pipe-chip ${c.real ? "real" : "mockp"}"
            title="${esc(c.detail)}${c.hint ? " · " + esc(c.hint) : ""}">
            ${c.real ? "✓" : "○"} ${esc(c.label)}·${c.real
              ? esc(c.provider_label.split(" ")[0].split("·")[0]) : "内置模拟"}</span>`).join("")}
        <a class="pipe-link" href="#/settings">${doc.real_count
          ? `${doc.real_count}/${doc.total} 环节已接真实 AI · 去设置`
          : "全部为内置模拟(占位画面)· 去接入真实 AI →"}</a>
      </div>`;
  }).catch(() => {});

  api("/api/logs?limit=30").then((rows) => {
    const el = document.getElementById("log-list");
    if (el) el.innerHTML = rows.length
      ? rows.reverse().map((r) => `<div class="lv-${esc(r.level)}">[${esc(r.level)}] ${esc(r.source)}: ${esc(r.message)}</div>`).join("")
      : `<div class="empty">暂无日志</div>`;
  }).catch(() => {});

  if (runningJobs.length) pollTimer = setInterval(refreshIfIdle, 2500);
}

function refreshIfIdle() {
  if (location.hash === "" || location.hash === "#/") renderDashboard();
}

async function onProduce(ev) {
  ev.preventDefault();
  const form = ev.target;
  const btn = form.querySelector('button[type="submit"]');
  btn.disabled = true; btn.textContent = "提交中…";
  try {
    const reply = await api("/api/produce", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({
        sentence: form.sentence.value,
        premise: form.premise.value,
        script_text: form.script.hidden ? "" : form.script.value,
        kind: form.dataset.kind || "",
      }),
    });
    showToast(reply.note
      ? `${reply.note},任务已提交`
      : `《${reply.title}》第${reply.episode}集 制作任务已提交`, "ok");
    renderDashboard();
  } catch (e) {
    showToast(e.message, "error");
    btn.disabled = false; btn.textContent = "开始制作";
  }
}

/* ================= 项目改名 ================= */
function renameProject(title, onDone) {
  const overlay = document.createElement("div");
  overlay.className = "script-overlay";
  overlay.innerHTML = `
    <div class="script-panel rename-panel">
      <div class="script-head"><h3>项目改名</h3>
        <button class="close">关闭 Esc</button></div>
      <p class="logline">剧集、素材、成片都按项目关联,改名不影响已有内容。</p>
      <div class="rename-row">
        <input id="rename-input" value="${esc(title)}" maxlength="40">
        <button class="primary" id="rename-go">保存</button>
      </div>
    </div>`;
  const close = () => {
    overlay.remove();
    document.removeEventListener("keydown", onKey);
  };
  const onKey = (ev) => { if (ev.key === "Escape") close(); };
  document.addEventListener("keydown", onKey);
  overlay.addEventListener("click", (ev) => { if (ev.target === overlay) close(); });
  overlay.querySelector(".close").onclick = close;
  document.body.appendChild(overlay);
  const input = overlay.querySelector("#rename-input");
  input.focus(); input.select();
  const go = async () => {
    const value = input.value.trim();
    if (!value || value === title) { close(); return; }
    try {
      await api("/api/project/rename", {
        method: "POST", headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ title, new_title: value }),
      });
      showToast(`已改名:《${title}》 → 《${value}》`, "ok");
      close();
      if (onDone) onDone(value);
    } catch (e) { showToast(e.message, "error"); }
  };
  overlay.querySelector("#rename-go").onclick = go;
  input.addEventListener("keydown", (ev) => {
    if (ev.key === "Enter") go();
  });
}

/* ================= 设置中心:每个环节 CLI / API 自由配置 ================= */
const CAP_LABEL = {
  script: "剧本", storyboard: "分镜", image: "图片", frames: "首尾帧",
  video: "视频", voice: "配音", edit: "剪辑", cover: "封面",
};

async function renderSettings() {
  topbarRight.innerHTML = "";
  let data;
  try { data = await api("/api/settings"); }
  catch (e) { app.innerHTML = `<div class="loading">加载失败:${esc(e.message)}</div>`; return; }
  drawSettings(data);
}

function drawSettings(data) {
  app.innerHTML = `
  <div class="settings">
    <div class="settings-head">
      <button id="btn-back">← 仪表盘</button>
      <h1>⚙️ AI 产线设置</h1>
      <button class="primary" id="btn-detect"
        title="扫描本机的 claude / codex / dreamina / 剪映 CLI,找到即自动填好并启用">🔍 自动检测本机 CLI</button>
      <span class="hint">CLI 点「自动检测」,API 只要粘贴 Key(接口地址已内置官方默认),保存即启用;没配好的环节由内置产线兜底</span>
    </div>
    <div class="settings-grid">
      ${data.providers.map(providerCard).join("")}
    </div>
    <details class="panel route-panel">
      <summary>高级 · 能力路由(每个环节谁来干,按顺序自动回退;一般不用改)</summary>
      <table class="route-table">
        <thead><tr><th>环节</th><th>调用顺序(逗号分隔,前面不可用自动用后面)</th><th></th></tr></thead>
        <tbody>
        ${Object.entries(data.routing).map(([cap, chain]) => `
          <tr data-cap="${esc(cap)}">
            <td>${esc(CAP_LABEL[cap] || cap)} <small class="dim">${esc(cap)}</small></td>
            <td><input class="route-input" value="${esc(chain.join(", "))}"></td>
            <td><button class="route-save">保存</button></td>
          </tr>`).join("")}
        </tbody>
      </table>
    </details>
    <div class="panel dim" style="font-size:12px">
      配置文件:${esc(data.config_path)}(改动即存,下一次制作生效;API Key 只存本机,不回显)
    </div>
  </div>`;
  document.getElementById("btn-back").onclick = () => { location.hash = "#/"; };
  const post = (body) => api("/api/settings", {
    method: "POST", headers: { "Content-Type": "application/json" },
    body: JSON.stringify(body),
  });
  document.getElementById("btn-detect").onclick = async (ev) => {
    const btn = ev.target;
    btn.disabled = true; btn.textContent = "检测中…";
    try {
      const fresh = await api("/api/settings/detect", { method: "POST" });
      const names = (fresh.applied || []).map((a) => a.provider);
      showToast(names.length
        ? `✓ 找到并已启用:${names.join("、")}`
        : "本机没找到 claude / codex / dreamina;装好后重试,或改用 API 模式",
        names.length ? "ok" : "error");
      drawSettings(fresh);
    } catch (e) {
      showToast(e.message, "error");
      btn.disabled = false; btn.textContent = "🔍 自动检测本机 CLI";
    }
  };
  app.querySelectorAll("tr[data-cap]").forEach((tr) => {
    tr.querySelector(".route-save").onclick = async () => {
      try {
        const fresh = await post({
          capability: tr.dataset.cap,
          chain: tr.querySelector(".route-input").value,
        });
        showToast("路由已保存", "ok");
        drawSettings(fresh);
      } catch (e) { showToast(e.message, "error"); }
    };
  });
  const collectFields = (card) => {
    const fields = {};
    card.querySelectorAll("[data-field]").forEach((input) => {
      if (input.type === "checkbox") fields[input.dataset.field] = input.checked;
      else if (input.value.trim() !== "") fields[input.dataset.field] = input.value.trim();
    });
    // 粘贴了 Key = 就是要用:自动启用,省去再点开关
    if (fields.api_key && !fields.enabled) fields.enabled = true;
    return fields;
  };
  app.querySelectorAll(".provider-card").forEach((card) => {
    const name = card.dataset.provider;
    card.querySelector(".pc-save").onclick = async () => {
      try {
        const fresh = await post({ provider: name, fields: collectFields(card) });
        showToast("已保存,下一次制作生效", "ok");
        drawSettings(fresh);
      } catch (e) { showToast(e.message, "error"); }
    };
    card.querySelector(".pc-test").onclick = async () => {
      const el = card.querySelector(".pc-status");
      const btn = card.querySelector(".pc-test");
      btn.disabled = true; btn.textContent = "测试中…";
      el.innerHTML = `<div class="dim">正在保存配置并真实请求接口…</div>`;
      try {
        // 先把表单里填的保存下来再测,避免「填了没保存」白测一场
        await post({ provider: name, fields: collectFields(card) });
        const r = await api("/api/settings/test", {
          method: "POST", headers: { "Content-Type": "application/json" },
          body: JSON.stringify({ provider: name }),
        });
        el.innerHTML = checksHtml(r.results) + (r.extra
          ? `<div class="${r.ok ? "ok" : "miss"}">${esc(r.extra)}</div>` : "");
        const bad = (r.results || []).find((x) => !x.ok);
        showToast(r.ok ? "✓ 测试通过,该产线已就绪"
          : `✗ 测试未通过:${r.extra || (bad && bad.reason) || "未知原因"}`,
          r.ok ? "ok" : "error");
      } catch (e) {
        el.innerHTML = `<div class="miss">✗ ${esc(e.message)}</div>`;
        showToast(`✗ 测试失败:${e.message}`, "error");
      } finally {
        btn.disabled = false; btn.textContent = "测试连接";
      }
    };
  });
}

function checksHtml(checks) {
  return (checks || []).map((c) => `
    <div class="${c.ok ? "ok" : "miss"}">${c.ok ? "✓" : "✗"}
      ${esc(CAP_LABEL[c.capability] || c.capability)}:${esc(c.reason)}</div>`).join("");
}

function providerCard(p) {
  const isApi = ["api", "claude_api", "image_api", "ark_video",
    "doubao_tts"].includes(p.type);
  const isCli = ["cli", "dreamina"].includes(p.type);
  const state = p.ready ? ["done", "就绪"]
    : p.enabled ? ["qc_failed", "待配置"] : ["", "未启用"];
  // 高级设置(接口地址/模型/命令都有内置默认,平时不用碰)
  const advanced = [];
  if (isCli) advanced.push(`<label class="set-row"><span>命令</span>
      <input data-field="command" value="${esc(p.command)}"
        placeholder="可执行文件或整串命令(「自动检测」会自动填)"></label>`);
  if (p.type === "dreamina") advanced.push(`<label class="set-row"><span>模型版本</span>
      <input data-field="model_version" value="${esc(p.model_version)}"></label>`);
  if (isApi) advanced.push(`<label class="set-row"><span>接口地址</span>
      <input data-field="endpoint" value="${esc(p.endpoint)}"
        placeholder="官方地址已内置"></label>`);
  if (isApi && !["api", "doubao_tts"].includes(p.type))
    advanced.push(`<label class="set-row"><span>模型</span>
      <input data-field="model" value="${esc(p.model)}"></label>`);
  if (p.type === "doubao_tts") advanced.push(`<label class="set-row"><span>音色</span>
      <input data-field="voice_type" value="${esc(p.voice_type || "")}"
        placeholder="如 BV700_streaming"></label>`);
  return `
  <div class="panel provider-card" data-provider="${esc(p.name)}">
    <div class="pc-head">
      <h2>${esc(p.label)}</h2>
      <span class="chip">${esc(p.mode)}</span>
      <span class="chip ${state[0]}">${state[1]}</span>
    </div>
    <div class="pc-caps">${p.capabilities.map((c) =>
      `<span class="chip">${esc(CAP_LABEL[c] || c)}</span>`).join("")}</div>
    <label class="set-row toggle"><span>启用</span>
      <input type="checkbox" data-field="enabled" ${p.enabled ? "checked" : ""}>
      <em>${p.enabled ? "参与自动路由" : "跳过,用下一个"}</em></label>
    ${isApi ? `
      ${p.type === "doubao_tts" ? `<label class="set-row"><span>APPID</span>
        <input data-field="appid" value="${esc(p.appid || "")}"
          placeholder="火山引擎语音的 appid"></label>` : ""}
      <label class="set-row"><span>API Key</span>
        <input data-field="api_key" type="password"
          placeholder="${p.api_key_set
            ? `已保存(${esc(p.api_key_masked)}),留空保持不变`
            : "粘贴 Key,保存即自动启用"}"></label>
      <div class="pc-note">只需要填 Key:接口地址与模型已内置官方默认。</div>` : ""}
    ${advanced.length ? `<details class="pc-advanced">
      <summary>高级设置(一般不用改)</summary>
      ${advanced.join("")}</details>` : ""}
    <div class="pc-actions">
      <button class="primary pc-save">保存</button>
      <button class="pc-test">测试连接</button>
    </div>
    <div class="pc-status">${checksHtml(p.checks)}</div>
  </div>`;
}

/* ================= 整集播放器(动态分镜连播) ================= */
function openPlayer(data, startShotNo) {
  const art = data.artifacts;
  const lineNoIndex = buildLineIndex(data.script);
  const shots = data.storyboard.shots.map((s) => {
    const video = art.videos[s.shot_no] || "";
    const lineNo = s.dialogue ? lineNoIndex(s) : null;
    return {
      shot: s,
      mp4: /\.mp4($|\?)/.test((video || "").split("#")[0]) ? video : null,
      first: art.first[s.shot_no] || art.images[s.shot_no] || "",
      last: art.last[s.shot_no] || art.images[s.shot_no] || "",
      audio: lineNo != null &&
        /\.(wav|mp3|m4a|aiff)($|\?)/.test((art.voices[lineNo] || ""))
        ? art.voices[lineNo] : null,
    };
  }).filter((x) => x.first || x.mp4);
  if (!shots.length) return;
  const total = shots.reduce((a, x) => a + x.shot.duration, 0);

  const overlay = document.createElement("div");
  overlay.className = "player-overlay";
  overlay.innerHTML = `
    <div class="player-box">
      <div class="player-head">
        <span>《${esc(data.project.title)}》第${data.episode.number}集</span>
        <span class="player-pos" id="pl-pos"></span>
        <button class="close" id="pl-close">关闭 Esc</button>
      </div>
      <div class="player-stage" id="pl-stage">
        <img id="pl-a" alt=""><img id="pl-b" alt="">
        <video id="pl-video" playsinline hidden></video>
        <button class="player-big" id="pl-big">▶</button>
      </div>
      <div class="player-bar">
        <button id="pl-toggle">▶ 播放</button>
        <div class="player-track" id="pl-track"><div class="player-fill" id="pl-fill"></div></div>
        <span class="player-time" id="pl-time"></span>
      </div>
    </div>`;
  document.body.appendChild(overlay);

  const elA = overlay.querySelector("#pl-a");
  const elB = overlay.querySelector("#pl-b");
  const elVideo = overlay.querySelector("#pl-video");
  const elFill = overlay.querySelector("#pl-fill");
  const elPos = overlay.querySelector("#pl-pos");
  const elTime = overlay.querySelector("#pl-time");
  const btnToggle = overlay.querySelector("#pl-toggle");
  const btnBig = overlay.querySelector("#pl-big");

  let index = -1, playing = false, timer = null, elapsedBefore = 0;
  let currentAudio = null;

  function fmtTime(sec) {
    return `${Math.floor(sec / 60)}:${String(Math.round(sec % 60)).padStart(2, "0")}`;
  }
  function stopMedia() {
    if (timer) { clearTimeout(timer); timer = null; }
    if (currentAudio) { currentAudio.pause(); currentAudio = null; }
    elVideo.pause();
  }
  function showShot(i, autoplay) {
    index = i;
    const item = shots[i];
    elapsedBefore = shots.slice(0, i).reduce((a, x) => a + x.shot.duration, 0);
    elPos.textContent = `镜头 ${i + 1}/${shots.length} · 场${item.shot.scene_no}`;
    elTime.textContent = `${fmtTime(elapsedBefore)} / ${fmtTime(total)}`;
    elFill.style.width = `${(elapsedBefore / total) * 100}%`;
    if (item.mp4) {
      elA.hidden = elB.hidden = true;
      elVideo.hidden = false;
      elVideo.src = item.mp4;
      elVideo.muted = !item.audio ? false : true;
      if (autoplay) elVideo.play().catch(() => {});
      elVideo.onended = () => { if (playing) next(); };
    } else {
      elVideo.hidden = true;
      elA.hidden = elB.hidden = false;
      elA.src = item.first;
      elB.src = item.last;
      elB.classList.remove("fade-in");
      void elB.offsetWidth;  // 重置过渡
      if (autoplay) {
        elB.style.transitionDuration = `${item.shot.duration}s`;
        elB.classList.add("fade-in");
        timer = setTimeout(next, item.shot.duration * 1000);
      }
    }
    if (autoplay && item.audio) {
      currentAudio = new Audio(item.audio);
      currentAudio.play().catch(() => {});
    }
  }
  function next() {
    stopMedia();
    if (index + 1 >= shots.length) { setPlaying(false); showShot(0, false); return; }
    showShot(index + 1, true);
  }
  function setPlaying(value) {
    playing = value;
    btnToggle.textContent = playing ? "⏸ 暂停" : "▶ 播放";
    btnBig.hidden = playing;
    if (!playing) stopMedia();
  }
  function start() {
    setPlaying(true);
    showShot(index < 0 ? 0 : index, true);
  }
  btnToggle.onclick = () => (playing ? setPlaying(false) : start());
  btnBig.onclick = start;
  overlay.querySelector("#pl-track").onclick = (ev) => {
    const rect = ev.currentTarget.getBoundingClientRect();
    const target = ((ev.clientX - rect.left) / rect.width) * total;
    let acc = 0;
    for (let i = 0; i < shots.length; i += 1) {
      acc += shots[i].shot.duration;
      if (target < acc) { stopMedia(); showShot(i, playing); return; }
    }
  };
  const close = () => {
    stopMedia();
    overlay.remove();
    document.removeEventListener("keydown", onKey);
  };
  const onKey = (ev) => {
    if (ev.key === "Escape") close();
    if (ev.key === " ") { ev.preventDefault(); btnToggle.click(); }
  };
  document.addEventListener("keydown", onKey);
  overlay.querySelector("#pl-close").onclick = close;
  overlay.addEventListener("click", (ev) => { if (ev.target === overlay) close(); });
  const aspect = data.project.aspect || "9:16";
  overlay.querySelector(".player-stage").style.aspectRatio =
    aspect === "16:9" ? "16 / 9" : "9 / 16";
  if (aspect === "16:9")
    overlay.querySelector(".player-box").style.width = "min(960px, 96vw)";
  const startIndex = startShotNo
    ? Math.max(0, shots.findIndex((x) => x.shot.shot_no === startShotNo))
    : 0;
  if (startShotNo) {
    setPlaying(true);
    showShot(startIndex, true);
  } else {
    showShot(0, false);
  }
}

function buildLineIndex(script) {
  return (shot) => {
    let n = 0;
    for (const scene of script.scenes) {
      for (const line of scene.lines) {
        n += 1;
        if (scene.scene_no === shot.scene_no &&
            line.character === shot.dialogue.character &&
            line.dialogue === (shot.dialogue_source || shot.dialogue.dialogue)) return n;
      }
    }
    return null;
  };
}

/* 剧本 → 可编辑文本(与导入格式一致) */
function scriptToText(script) {
  return script.scenes.map((s) => {
    const lines = [`第${s.scene_no}场 ${s.location}`];
    if (s.action) lines.push(s.action);
    (s.lines || []).forEach((l) => lines.push(`${l.character}:${l.dialogue}`));
    return lines.join("\n");
  }).join("\n\n");
}

async function pollJob(jobId, onDone) {
  const timer = setInterval(async () => {
    try {
      const job = await api(`/api/jobs/${jobId}`);
      if (job.status !== "running") {
        clearInterval(timer);
        if (job.status === "failed") showToast(job.error || "任务失败", "error");
        onDone(job);
      }
    } catch (e) { clearInterval(timer); }
  }, 1200);
}

/* 剧本阅读 + 打磨(意见重写 / 直接编辑) */
function showScriptOverlay(data, episodeId) {
  const script = data.script;
  if (!script) return;
  const overlay = document.createElement("div");
  overlay.className = "script-overlay";
  overlay.innerHTML = `
    <div class="script-panel">
      <div class="script-head">
        <h3>《${esc(script.project_title)}》第${script.episode_number}集
            ${script.episode_title ? " · " + esc(script.episode_title) : ""}</h3>
        <button class="close">关闭 Esc</button>
      </div>
      <div class="revise-bar">
        <textarea id="revise-feedback" rows="2"
          placeholder="对剧本的修改意见,例如:节奏太慢,第2场加冲突;台词更口语化"></textarea>
        <div class="revise-actions">
          <button class="primary" id="btn-revise">✏️ 按意见重写剧本</button>
          <button id="btn-edit-toggle">📝 直接编辑文字</button>
          <button id="btn-script-down">⬇ 下载剧本</button>
          <button id="btn-script-up">⬆ 上传剧本文件</button>
        </div>
      </div>
      <div id="edit-area" hidden>
        <textarea id="edit-text" rows="14"></textarea>
        <button class="primary" id="btn-edit-submit">用这版剧本重做</button>
      </div>
      <p class="logline">${esc(script.logline || "")}</p>
      <div class="cast">${(script.characters || []).map((c) =>
        `<span class="chip">${esc(c.name)} · ${esc(c.role || "")}</span>`).join("")}</div>
      ${script.scenes.map((s) => `
        <section class="scene">
          <h4>场 ${s.scene_no} · ${esc(s.location)}</h4>
          ${s.action ? `<p class="action">${esc(s.action)}</p>` : ""}
          ${(s.lines || []).map((l) => `
            <p class="line"><b>${esc(l.character)}</b>${esc(l.dialogue)}</p>`).join("")}
        </section>`).join("")}
    </div>`;
  const close = () => { overlay.remove(); document.removeEventListener("keydown", onKey); };
  const onKey = (ev) => { if (ev.key === "Escape") close(); };
  overlay.addEventListener("click", (ev) => { if (ev.target === overlay) close(); });
  overlay.querySelector(".close").onclick = close;
  document.addEventListener("keydown", onKey);
  document.body.appendChild(overlay);

  const post = (path, body) => api(path, {
    method: "POST", headers: { "Content-Type": "application/json" },
    body: JSON.stringify(body) });
  overlay.querySelector("#btn-revise").onclick = async () => {
    const feedback = overlay.querySelector("#revise-feedback").value.trim();
    if (!feedback) { showToast("先写一句修改意见", "error"); return; }
    try {
      await post("/api/revise", { episode_id: data.episode.id, feedback });
      close();
      showToast("正在按你的意见重写剧本并重出人物/分镜…", "ok");
      pollTimer = setInterval(() => renderCanvasView(episodeId), 3000);
    } catch (e) { showToast(e.message, "error"); }
  };
  const editArea = overlay.querySelector("#edit-area");
  overlay.querySelector("#btn-edit-toggle").onclick = () => {
    editArea.hidden = !editArea.hidden;
    if (!editArea.hidden) {
      overlay.querySelector("#edit-text").value = scriptToText(script);
    }
  };
  const submitScriptText = async (text, source) => {
    try {
      await post("/api/produce", {
        title: data.project.title,
        episode: data.episode.number,
        script_text: text,
        review: true,
      });
      close();
      showToast(`已按${source}重做,人物/分镜将自动更新…`, "ok");
      pollTimer = setInterval(() => renderCanvasView(episodeId), 3000);
    } catch (e) { showToast(e.message, "error"); }
  };
  overlay.querySelector("#btn-edit-submit").onclick = () =>
    submitScriptText(overlay.querySelector("#edit-text").value, "你的文字");
  overlay.querySelector("#btn-script-down").onclick = () => {
    const blob = new Blob([scriptToText(script)],
      { type: "text/plain;charset=utf-8" });
    const url = URL.createObjectURL(blob);
    downloadUrl(url,
      `${script.project_title}_第${script.episode_number}集_剧本.txt`);
    setTimeout(() => URL.revokeObjectURL(url), 5000);
  };
  overlay.querySelector("#btn-script-up").onclick = () => {
    const input = document.createElement("input");
    input.type = "file";
    input.accept = ".txt,.json,text/plain,application/json";
    input.onchange = () => {
      const file = input.files[0];
      if (!file) return;
      const reader = new FileReader();
      reader.onload = () => submitScriptText(
        String(reader.result), `文件「${file.name}」`);
      reader.readAsText(file, "utf-8");
    };
    input.click();
  };
}

/* 成品包一键下载 */
async function exportEpisode(data) {
  try {
    showToast("正在打包成品…", "ok");
    const res = await fetch(`/api/export/${data.episode.id}`);
    if (!res.ok) {
      const err = await res.json().catch(() => ({}));
      throw new Error(err.error || "导出失败");
    }
    const blob = await res.blob();
    const url = URL.createObjectURL(blob);
    downloadUrl(url,
      `${data.project.title}_第${data.episode.number}集_成品包.zip`);
    setTimeout(() => URL.revokeObjectURL(url), 10000);
    showToast("成品包已开始下载", "ok");
  } catch (e) { showToast(e.message, "error"); }
}

/* 素材下载 / 人工修改后上传 */
function downloadUrl(url, filename) {
  const a = document.createElement("a");
  a.href = url;
  a.download = filename || "";
  document.body.appendChild(a);
  a.click();
  a.remove();
}

function uploadFile(episodeId, target, accept, onDone) {
  const input = document.createElement("input");
  input.type = "file";
  input.accept = accept;
  input.onchange = () => {
    const file = input.files[0];
    if (!file) return;
    const reader = new FileReader();
    reader.onload = async () => {
      try {
        showToast("上传中…", "ok");
        await api("/api/upload", {
          method: "POST", headers: { "Content-Type": "application/json" },
          body: JSON.stringify({
            episode_id: episodeId, target, filename: file.name,
            data_base64: String(reader.result).split(",")[1] || "",
          }),
        });
        showToast("已替换为你的版本", "ok");
        onDone();
      } catch (e) { showToast(e.message, "error"); }
    };
    reader.readAsDataURL(file);
  };
  input.click();
}

function ioControls(target, url, filename) {
  return `<div class="io-box" data-target="${esc(JSON.stringify(target))}"
    data-url="${esc(url || "")}" data-name="${esc(filename)}">
    ${url ? `<button class="io-down">⬇ 下载</button>` : ""}
    <button class="io-up">⬆ 上传替换</button>
  </div>`;
}

function bindIo(container, episodeId, reload) {
  container.querySelectorAll(".io-box").forEach((box) => {
    const target = JSON.parse(box.dataset.target);
    const down = box.querySelector(".io-down");
    if (down) down.onclick = () =>
      downloadUrl(box.dataset.url, box.dataset.name);
    box.querySelector(".io-up").onclick = () => uploadFile(
      episodeId, target,
      target.kind === "shot_video" ? "video/mp4" : "image/*",
      reload);
  });
}

/* 单张图片附意见重画 */
function regenControls(target, label) {
  return `<div class="regen-box" data-target="${esc(JSON.stringify(target))}">
    <button class="regen-toggle">🔄 ${esc(label)}</button>
    <div class="regen-form" hidden>
      <input placeholder="修改意见,如:换成夜晚/表情更凶(可留空)">
      <button class="primary regen-go">重画</button>
    </div></div>`;
}

function bindRegen(container, episodeId, getData) {
  container.querySelectorAll(".regen-box").forEach((box) => {
    const form = box.querySelector(".regen-form");
    box.querySelector(".regen-toggle").onclick = () => {
      form.hidden = !form.hidden;
      if (!form.hidden) form.querySelector("input").focus();
    };
    box.querySelector(".regen-go").onclick = async () => {
      const target = JSON.parse(box.dataset.target);
      const feedback = form.querySelector("input").value.trim();
      const btn = box.querySelector(".regen-go");
      btn.disabled = true; btn.textContent = "重画中…";
      try {
        const reply = await api("/api/regen_image", {
          method: "POST", headers: { "Content-Type": "application/json" },
          body: JSON.stringify({ episode_id: episodeId, target, feedback }),
        });
        pollJob(reply.job_id, (job) => {
          if (job.status === "done") {
            showToast("已重画完成", "ok");
            renderCanvasView(episodeId);
          } else { btn.disabled = false; btn.textContent = "重画"; }
        });
      } catch (e) {
        showToast(e.message, "error");
        btn.disabled = false; btn.textContent = "重画";
      }
    };
  });
}

/* 可内联播放的媒体标签(真实产线 mp4/wav;mock 的 json 描述文件回退为链接) */
function mediaTag(url) {
  if (!url) return "";
  const clean = url.split("?")[0].toLowerCase();
  if (clean.endsWith(".mp4"))
    return `<video class="player" controls preload="metadata" src="${esc(url)}"></video>`;
  if ([".wav", ".mp3", ".m4a", ".aiff"].some((ext) => clean.endsWith(ext)))
    return `<audio class="player" controls preload="metadata" src="${esc(url)}"></audio>`;
  return "";
}

function stateInline(states) {
  return Object.entries(states || {}).map(([name, state]) =>
    `${name}:${state.position || ""}·${state.pose || ""}·${state.emotion || ""}`
  ).join("；") || "-";
}

const STAGE_CN = {
  script: "剧本", continuity: "连续性圣经", cast: "人物/场景图",
  storyboard: "五维分镜", images: "关键帧", text_assets: "文字资产锁定",
  frames: "首尾帧", preflight: "生产门禁", videos: "Seedance 视频",
  voices: "随视频配音/口型", edit: "剪映剪辑",
  qc: "三层质检", package: "封面/标题", archive: "数据沉淀",
  assets: "资产调用",
};
const STAGE_ORDER = ["script", "continuity", "cast", "storyboard", "images",
  "text_assets", "frames", "preflight", "videos", "voices", "edit", "qc",
  "package", "archive"];
const STAGE_PLAIN = {
  script: "正在写剧本", continuity: "正在锁定角色、场景和文字规则",
  cast: "正在画人物和场景", storyboard: "正在生成五维分镜",
  images: "正在生成关键帧", text_assets: "正在锁定可读文字",
  frames: "正在生成首尾帧", preflight: "正在执行生产硬门禁",
  videos: "正在生成 Seedance 视频", voices: "正在确认随视频声音与口型",
  edit: "正在剪辑无字幕成片", qc: "正在做自动检查、检查板和内容复核",
  package: "正在做封面和标题", archive: "正在归档素材",
};

/* 制作中的醒目进度条 */
function renderProgressBanner(data) {
  const el = document.getElementById("progress-banner");
  if (!el) return;
  const awaiting = data.episodes.filter(
    (e) => e.status === "awaiting_confirm");
  const producing = data.episodes.filter(
    (e) => !["done", "failed", "qc_failed", "created",
             "awaiting_confirm"].includes(e.status));
  if (!producing.length && !awaiting.length) { el.innerHTML = ""; return; }
  el.innerHTML = awaiting.map((e) => `
    <div class="progress-card confirm">
      <div class="progress-text">《${esc(e.project)}》第${e.number}集 预生产完成,等你过目
        <span>连续性、五维分镜、关键帧与生产门禁均已通过</span></div>
      <button class="primary" onclick="location.hash='#/episode/${e.id}'">去确认 →</button>
    </div>`).join("") + producing.map((e) => {
    const idx = STAGE_ORDER.indexOf(e.status);
    const step = idx >= 0 ? idx + 1 : 1;
    const pct = Math.round(step / STAGE_ORDER.length * 100);
    return `
    <div class="progress-card">
      <div class="progress-text">正在制作《${esc(e.project)}》第${e.number}集
        <span>第 ${step} 步 / 共 ${STAGE_ORDER.length} 步 · ${esc(STAGE_PLAIN[e.status] || e.status)}…</span></div>
      <div class="progress-track"><div class="progress-fill" style="width:${pct}%"></div></div>
    </div>`;
  }).join("");
}
const KIND_CN = {
  character: "角色", scene: "场景", action: "动作", shot: "镜头",
  prompt: "Prompt", first_frame: "首帧", last_frame: "尾帧", image: "图片",
  video: "视频", voice: "配音", cover: "封面", title: "标题",
  clip: "拆条", edit: "成片",
};

/* ================= 分镜画布 ================= */
const CARD_W = 220, CARD_H = 218, GAP_X = 26, GAP_Y = 56, LANE_X = 150;

async function renderCanvasView(episodeId) {
  if (pollTimer) { clearInterval(pollTimer); pollTimer = null; }
  let data;
  try { data = await api(`/api/episode/${episodeId}`); }
  catch (e) { app.innerHTML = `<div class="loading">加载失败:${esc(e.message)}</div>`; return; }

  const ep = data.episode, sb = data.storyboard, script = data.script;
  topbarRight.innerHTML = chip(ep.status);
  if (!sb) {
    app.innerHTML = `<div class="loading">本集尚无分镜(制作进行中或未开始)。<a href="#/">返回仪表盘</a></div>`;
    if (!["done", "failed", "qc_failed"].includes(ep.status))
      pollTimer = setInterval(() => renderCanvasView(episodeId), 3000);
    return;
  }

  // 质检问题按镜头/台词索引
  const shotIssues = {}, lineIssues = {};
  (data.qc_report?.issues || []).forEach((i) => {
    if (i.shot_no != null) (shotIssues[i.shot_no] = shotIssues[i.shot_no] || []).push(i);
    if (i.line_no != null) (lineIssues[i.line_no] = lineIssues[i.line_no] || []).push(i);
  });

  const awaiting = ep.status === "awaiting_confirm";
  const profile = data.production_profile || {};
  const gates = data.preflight?.gates || [];
  app.innerHTML = `
  <div class="canvas-view">
    ${awaiting ? `
    <div class="confirm-banner">
      <div>
        <b>${data.preflight?.passed ? `${gates.length} 项生产门禁通过，请做最终视觉确认` : "生产门禁未通过"}</b>
        <span>角色/场景连续性、五维分镜、文字关键帧、首尾帧和即梦配置均已机检。
        确认后才会消耗 Seedance 额度；成片仍须通过检查板、内容复核与交付脚本。</span>
      </div>
      <button class="primary" id="btn-confirm" ${data.preflight?.passed ? "" : "disabled"}>✅ 确认,开始 Seedance 生产</button>
    </div>` : ""}
    <div class="profile-strip">
      <span><b>${esc(profile.standard_name || "SK 五维工业流")}</b> v${esc(profile.standard_version || 1)}</span>
      <span>Seedance 2.0 Fast VIP</span><span>${esc(profile.resolution || "720p")}</span>
      <span>Seedance2 随视频配音</span><span>口型同步</span><span>无字幕母版</span>
      <strong>${gates.filter((g) => g.passed).length}/${gates.length || 0} 门禁通过</strong>
      <a href="#/standards/history">查看制作标准</a>
    </div>
    <div class="canvas-toolbar">
      <button id="btn-back">← 仪表盘</button>
      <span class="title">《${esc(ep.project_title || data.project.title)}》第${ep.number}集</span>
      ${chip(ep.status)}
      <span class="hint">质检 ${ep.qc_score == null ? "-" : fmt(ep.qc_score, 0)} 分 · 成本 ${fmt(ep.cost)}</span>
      <span class="spacer"></span>
      <div class="zoom-group view-toggle">
        <button id="view-theater">🎬 剧场</button>
        <button id="view-canvas">🗺 画布</button>
      </div>
      <button id="btn-play" class="primary">▶ 播放本集</button>
      <button id="btn-script">剧本</button>
      <button id="btn-reproduce" title="复用已完成的部分,只补做缺失内容">继续补齐</button>
      <button id="btn-reproduce-force" title="从头全部重新制作(真实产线会消耗额度)">全部重做</button>
      <div class="zoom-group">
        <button id="zoom-out">−</button>
        <span class="zoom-pct" id="zoom-pct">100%</span>
        <button id="zoom-in">＋</button>
        <button id="zoom-fit">适应</button>
        <button id="layout-reset">重排</button>
      </div>
    </div>
    <div class="canvas-body">
      <div id="theater"></div>
      <div id="viewport" hidden><div id="world"></div></div>
      <aside id="sidepanel"></aside>
    </div>
    <div class="timeline" id="timeline" hidden></div>
  </div>`;

  document.getElementById("btn-back").onclick = () => { location.hash = "#/"; };
  const reproduce = async (force) => {
    try {
      await api("/api/produce", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({
          title: data.project.title, episode: ep.number, force,
        }),
      });
      showToast(force ? "已提交强制重制,画布将自动刷新" : "已提交增量重制,画布将自动刷新", "ok");
      pollTimer = setInterval(() => renderCanvasView(episodeId), 3000);
    } catch (e) { showToast(e.message, "error"); }
  };
  document.getElementById("btn-reproduce").onclick = (ev) =>
    armConfirm(ev.target, "补齐", () => reproduce(false));
  document.getElementById("btn-reproduce-force").onclick = (ev) =>
    armConfirm(ev.target, "重做", () => reproduce(true));
  document.getElementById("btn-script").onclick = () => showScriptOverlay(data, episodeId);
  document.getElementById("btn-play").onclick = () => openPlayer(data);
  const btnConfirm = document.getElementById("btn-confirm");
  if (btnConfirm) btnConfirm.onclick = async () => {
    btnConfirm.disabled = true;
    btnConfirm.textContent = "已确认,生产中…";
    try {
      await api("/api/confirm", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ episode_id: ep.id }),
      });
      showToast("已确认!正在生成 Seedance 视频、随视频配音/口型与无字幕母版", "ok");
      pollTimer = setInterval(() => renderCanvasView(episodeId), 3000);
    } catch (e) {
      showToast(e.message, "error");
      btnConfirm.disabled = false;
      btnConfirm.textContent = "✅ 确认,开始生产";
    }
  };
  // 制作进行中自动刷新画布(待确认是稳定状态,不轮询)
  if (!["done", "failed", "qc_failed", "created",
        "awaiting_confirm"].includes(ep.status))
    pollTimer = setInterval(() => renderCanvasView(episodeId), 3000);

  const canvas = new StoryboardCanvas(data, shotIssues, lineIssues);
  canvas.mount();
  renderTheater(data, canvas);

  // 剧场(影视站观感,默认)/ 自由画布 双视图
  const theaterEl = document.getElementById("theater");
  const viewportEl = document.getElementById("viewport");
  const timelineEl = document.getElementById("timeline");
  const btnTheater = document.getElementById("view-theater");
  const btnCanvas = document.getElementById("view-canvas");
  const setView = (mode) => {
    localStorage.setItem("aifos.view", mode);
    const theaterMode = mode !== "canvas";
    theaterEl.hidden = !theaterMode;
    viewportEl.hidden = theaterMode;
    timelineEl.hidden = theaterMode;
    btnTheater.classList.toggle("active", theaterMode);
    btnCanvas.classList.toggle("active", !theaterMode);
    if (!theaterMode) canvas.fit();
  };
  btnTheater.onclick = () => setView("theater");
  btnCanvas.onclick = () => setView("canvas");
  setView(localStorage.getItem("aifos.view") || "theater");
}

/* ---- 剧场模式:Hero 横幅 + 人物条 + 每场一条横向镜头海报行 ---- */
function renderTheater(data, canvas) {
  const el = document.getElementById("theater");
  const art = data.artifacts;
  const script = data.script;
  const shots = data.storyboard.shots;
  const aspect = data.project.aspect || "9:16";
  const hero = art.cover || art.images[shots[0] && shots[0].shot_no] || "";
  const total = shots.reduce((a, s) => a + s.duration, 0);
  const kindCN = { drama: "剧情短剧", idol: "AI虚拟偶像" }[data.project.kind]
    || data.project.kind;
  const sceneOf = (no) => script.scenes.find((s) => s.scene_no === no) || {};
  const rows = [...new Set(shots.map((s) => s.scene_no))].map((no) => ({
    scene: sceneOf(no),
    shots: shots.filter((s) => s.scene_no === no),
  }));
  el.innerHTML = `
    <div class="hero" style="background-image:url('${hero.replace(/'/g, "%27")}')">
      <div class="hero-content">
        <h1>${esc(data.project.title)}
          <button class="mini-btn" id="hero-rename" title="修改项目名">✎</button></h1>
        <div class="hero-meta">
          <span class="chip">${esc(kindCN)}</span>
          <span class="chip">第${data.episode.number}集${data.episode.title ? " · " + esc(data.episode.title) : ""}</span>
          <span class="chip">${fmt(total, 0)} 秒 · ${shots.length} 镜</span>
          <span class="chip">SK 五维分镜</span><span class="chip">无字幕</span>
          ${data.episode.qc_score != null ? `<span class="chip">质检 ${fmt(data.episode.qc_score, 0)} 分</span>` : ""}
        </div>
        <p class="hero-logline">${esc(script.logline || "")}</p>
        <div class="hero-actions">
          <button class="primary" id="hero-play">▶ 播放本集</button>
          <button id="hero-script">📖 剧本</button>
          ${art.review_board ? `<a class="button-link" href="${esc(art.review_board)}" target="_blank">🧪 图文检查板</a>` : ""}
          <button id="hero-export" title="成片/封面/文案/配音/草稿一键打包">⬇ 下载成品包</button>
        </div>
      </div>
    </div>
    ${(art.cast_art || []).length ? `
    <div class="cast-strip">
      ${art.cast_art.map((c) => `
        <figure title="${esc(c.name)}">
          <img src="${esc(c.url)}" alt="${esc(c.name)}">
          <figcaption>${esc(c.name)}</figcaption>
        </figure>`).join("")}
    </div>` : ""}
    ${rows.map(({ scene, shots: ss }) => `
    <section class="scene-row">
      <h3>场 ${scene.scene_no} · ${esc(scene.location || "")}
        <span>${fmt(ss.reduce((a, s) => a + s.duration, 0), 1)}s · ${ss.length} 镜</span></h3>
      <div class="row-scroll">
        ${ss.map((s) => `
        <div class="t-card ${aspect === "16:9" ? "landscape" : ""}" data-shot="${s.shot_no}">
          ${art.images[s.shot_no]
            ? `<img src="${esc(art.images[s.shot_no])}" alt="镜头${s.shot_no}" loading="lazy">`
            : `<div class="t-empty">画面生成中</div>`}
          <button class="t-detail" data-shot="${s.shot_no}" title="镜头详情">ⓘ</button>
          <div class="t-play">▶</div>
          <div class="t-info">
            <b>#${String(s.shot_no).padStart(2, "0")}</b> ${esc(s.camera || "")} · ${fmt(s.duration, 1)}s
            ${s.dialogue ? `<div class="t-line">${esc(s.dialogue.character)}:${esc(s.dialogue.dialogue)}</div>` : ""}
          </div>
        </div>`).join("")}
      </div>
    </section>`).join("")}`;

  el.querySelector("#hero-play").onclick = () => openPlayer(data);
  el.querySelector("#hero-script").onclick = () =>
    document.getElementById("btn-script").click();
  el.querySelector("#hero-export").onclick = () => exportEpisode(data);
  el.querySelector("#hero-rename").onclick = () =>
    renameProject(data.project.title,
      () => renderCanvasView(data.episode.id));
  el.querySelectorAll(".t-card").forEach((card) => {
    card.addEventListener("click", () =>
      openPlayer(data, Number(card.dataset.shot)));
  });
  el.querySelectorAll(".t-detail").forEach((btn) => {
    btn.addEventListener("click", (ev) => {
      ev.stopPropagation();
      canvas.select(Number(btn.dataset.shot));
    });
  });
}

class StoryboardCanvas {
  constructor(data, shotIssues, lineIssues) {
    this.data = data;
    this.shotIssues = shotIssues;
    this.lineIssues = lineIssues;
    this.scale = 1; this.tx = 30; this.ty = 24;
    this.selected = null;
    this.layoutKey = `aifos.layout.${data.episode.id}`;
    this.positions = this.loadLayout();
    this.viewport = document.getElementById("viewport");
    this.world = document.getElementById("world");
  }

  /* ---- 布局:按场分行,支持拖拽微调并本地持久化 ---- */
  defaultPositions() {
    const pos = {};
    const scenes = new Map();
    this.data.storyboard.shots.forEach((s) => {
      if (!scenes.has(s.scene_no)) scenes.set(s.scene_no, []);
      scenes.get(s.scene_no).push(s);
    });
    let lane = 0;
    for (const [, shots] of [...scenes.entries()].sort((a, b) => a[0] - b[0])) {
      shots.forEach((s, i) => {
        pos[s.shot_no] = { x: LANE_X + i * (CARD_W + GAP_X), y: lane * (CARD_H + GAP_Y) };
      });
      lane += 1;
    }
    return pos;
  }
  loadLayout() {
    try {
      const saved = JSON.parse(localStorage.getItem(this.layoutKey) || "{}");
      return { ...this.defaultPositions(), ...saved };
    } catch { return this.defaultPositions(); }
  }
  saveLayout() { localStorage.setItem(this.layoutKey, JSON.stringify(this.positions)); }

  mount() {
    this.renderWorld();
    this.renderTimeline();
    this.renderSidePanel(null);
    this.bind();
    this.fit();
  }

  sceneOf(no) { return this.data.script.scenes.find((s) => s.scene_no === no); }

  renderWorld() {
    const { storyboard } = this.data;
    const art = this.data.artifacts;
    const lanes = new Map();
    storyboard.shots.forEach((s) => {
      const p = this.positions[s.shot_no];
      if (!lanes.has(s.scene_no) || p.y < lanes.get(s.scene_no)) lanes.set(s.scene_no, p.y);
    });
    let html = "";
    for (const [sceneNo, y] of lanes) {
      const scene = this.sceneOf(sceneNo);
      html += `<div class="lane-label" style="left:0;top:${y}px">
        场 ${sceneNo}<span class="loc">${esc(scene?.location || "")}</span></div>`;
    }
    for (const shot of storyboard.shots) {
      const p = this.positions[shot.shot_no];
      const img = art.images[shot.shot_no];
      const issues = this.shotIssues[shot.shot_no] || [];
      const hasVideo = !!art.videos[shot.shot_no];
      const lineNo = this.lineNoOf(shot);
      const videoAudio = art.video_audio || {};
      const hasAudioEvidence = Object.prototype.hasOwnProperty.call(
        videoAudio, shot.shot_no);
      const integratedVoice = hasAudioEvidence
        ? !!videoAudio[shot.shot_no]
        : this.data.production_profile?.voice === "jimeng_builtin";
      const voiceOk = integratedVoice ? hasVideo : lineNo != null && !!art.voices[lineNo];
      html += `
      <div class="shot-card${this.selected === shot.shot_no ? " selected" : ""}"
           data-shot="${shot.shot_no}" style="left:${p.x}px;top:${p.y}px">
        ${img ? `<img src="${esc(img)}" alt="镜头${shot.shot_no}关键图" draggable="false">`
              : `<div class="no-img">暂无关键图</div>`}
        <div class="body">
          <div class="head"><span class="sn">#${String(shot.shot_no).padStart(2, "0")}</span>
            <span class="dur">${esc(shot.camera || "")} · ${fmt(shot.duration, 1)}s</span></div>
          <div class="desc">${esc(shot.dialogue ? `${shot.dialogue.character}:「${shot.dialogue.dialogue}」` : shot.description)}</div>
          <div class="badges">
            <span class="badge ${hasVideo ? "ok" : "miss"}">${hasVideo ? "✓ 视频" : "✗ 视频"}</span>
            ${shot.dialogue ? `<span class="badge ${voiceOk ? "ok" : "miss"}">${voiceOk ? "✓ 配音/口型" : "✗ 配音/口型"}</span>` : ""}
            ${issues.length ? `<span class="badge qc">⚠ 质检${issues.length}</span>` : ""}
          </div>
        </div>
      </div>`;
    }
    this.world.innerHTML = html;
    this.applyTransform();
  }

  renderTimeline() {
    const el = document.getElementById("timeline");
    const scenes = new Map();
    this.data.storyboard.shots.forEach((s) => {
      scenes.set(s.scene_no, (scenes.get(s.scene_no) || 0) + s.duration);
    });
    const total = [...scenes.values()].reduce((a, b) => a + b, 0) || 1;
    el.innerHTML = [...scenes.entries()].map(([no, dur]) => `
      <div class="tl-seg" data-scene="${no}" style="flex:${(dur / total).toFixed(4)}"
           title="场${no} · ${fmt(dur, 1)}s">场${no} · ${fmt(dur, 1)}s</div>`).join("");
    el.querySelectorAll(".tl-seg").forEach((seg) =>
      seg.addEventListener("click", () => this.focusScene(Number(seg.dataset.scene))));
  }

  focusScene(sceneNo) {
    const shots = this.data.storyboard.shots.filter((s) => s.scene_no === sceneNo);
    if (!shots.length) return;
    const first = this.positions[shots[0].shot_no];
    this.tx = 40 - (first.x - LANE_X) * this.scale;
    this.ty = 40 - first.y * this.scale;
    this.applyTransform();
  }

  /* ---- 侧栏 ---- */
  renderSidePanel(shotNo) {
    const panel = document.getElementById("sidepanel");
    const art = this.data.artifacts;
    if (shotNo == null) {
      const ep = this.data.episode;
      const qc = this.data.qc_report;
      const preflight = this.data.preflight;
      const continuity = this.data.continuity;
      const contentReview = this.data.content_review || qc?.content_review;
      const delivery = qc?.delivery_check;
      const standard = this.data.production_standard || {};
      panel.innerHTML = `
        <h3>本集总览</h3>
        <button class="primary play-cta" id="panel-play">▶ 播放本集(${fmt(this.data.storyboard.shots.reduce((a, s) => a + s.duration, 0), 0)}秒)</button>
        <button class="play-cta" id="panel-export">⬇ 下载成品包(成片/封面/文案)</button>
        <h4>本集制作标准</h4>
        <div class="standard-snapshot">
          <div><b>${esc(standard.name || "SK 五维漫剧标准")}</b><span>v${esc(standard.version || 1)}</span></div>
          <code>${esc((standard.fingerprint || "").slice(0, 12))}</code>
          <small>本集已锁快照，后续修改厂标不会改变当前制作合同。</small>
        </div>
        <h4>生产门禁 · ${preflight?.passed ? "PASS" : "待检查"}</h4>
        <div class="gate-list">${(preflight?.gates || []).map((gate) => `
          <div class="gate-item ${gate.passed ? "pass" : "fail"}">
            <span>${gate.passed ? "✓" : "×"} ${esc(gate.label)}</span>
            <small>${esc(gate.detail)}</small>
          </div>`).join("") || `<div class="empty">预生产后显示门禁结果</div>`}</div>
        ${continuity ? `<div class="contract-summary">
          <span><b>${continuity.characters?.length || 0}</b> 角色锚点</span>
          <span><b>${continuity.scenes?.length || 0}</b> 场景锚点</span>
          <span><b>${this.data.storyboard.shots.length}</b> 五维单元</span>
          <span><b>${this.data.text_assets?.assets?.length || 0}</b> 文字资产</span>
        </div>` : ""}
        <h4>三层质检</h4>
        <div class="qc-layers">
          <span class="${qc?.technical_passed ? "pass" : "pending"}">① 自动文件检查 ${qc?.technical_passed ? "PASS" : "待完成"}</span>
          <span class="${art.review_board ? "pass" : "pending"}">② 抽帧图文检查板 ${art.review_board ? "READY" : "待生成"}</span>
          <span class="${contentReview?.passed ? "pass" : "pending"}">③ 逐段内容复核 ${contentReview?.passed ? "PASS" : "待完成"}</span>
          <span class="${delivery?.passed ? "pass" : "pending"}">④ 交付脚本实跑 ${delivery?.passed ? "PASS" : "待完成"}</span>
        </div>
        ${art.review_board ? `<a class="review-link" href="${esc(art.review_board)}" target="_blank">打开图文检查板 →</a>` : ""}
        ${(art.cast_art || []).length ? `<h4>人物 · 不满意可附意见重画</h4>
        <div class="art-grid">${art.cast_art.map((c) => `
          <figure><img src="${esc(c.url)}" alt="${esc(c.name)}">
          <figcaption>${esc(c.name)}${c.role ? " · " + esc(c.role) : ""}</figcaption>
          ${regenControls({ kind: "character_art", name: c.name }, "重画")}
          ${ioControls({ kind: "character_art", name: c.name }, c.url,
            `${c.name}.png`)}</figure>`).join("")}
        </div>` : ""}
        ${(art.scene_art || []).length ? `<h4>场景</h4>
        <div class="art-grid">${art.scene_art.map((s) => `
          <figure><img src="${esc(s.url)}" alt="${esc(s.name)}">
          <figcaption>${esc(s.name)}</figcaption>
          ${regenControls({ kind: "scene_art", name: s.name }, "重画")}
          ${ioControls({ kind: "scene_art", name: s.name }, s.url,
            `${s.name}.png`)}</figure>`).join("")}
        </div>` : ""}
        ${art.cover ? `<img class="preview" src="${esc(art.cover)}" alt="封面">` : ""}
        ${art.titles.length ? `<h4>候选标题</h4><ul class="titles-list">
          ${art.titles.map((t) => `<li>${esc(t)}</li>`).join("")}</ul>` : ""}
        <h4>制作阶段 · 括号内为实际产线</h4>
        <ul class="stage-list">
          ${this.data.tasks.map((t) => `<li>
            <span>${esc(t.name)}${t.provider
              ? ` <small class="prov ${t.provider.includes("mock") ? "" : "real"}">${t.provider.includes("mock") ? "内置模拟" : esc(t.provider)}</small>` : ""}</span>
            <span style="display:flex;gap:8px;align-items:center">
              <span class="cost">${fmt(t.cost)}</span>${chip(t.status === "done" ? "done" : t.status === "failed" ? "failed" : "running")}
            </span></li>`).join("")}
        </ul>
        ${qc ? `<h4>问题清单 · ${qc.score}分(线${qc.pass_score})</h4>
          ${qc.issues.length ? qc.issues.map((i) => `
            <div class="issue ${esc(i.severity)}">[${esc(i.check)}] ${esc(i.message)}</div>`).join("")
          : `<div class="empty">全部检查通过</div>`}` : ""}
        <h4>成片与产物</h4>
        ${mediaTag(art.final)}
        <ul class="links">
          ${art.final ? `<li><span>成片</span><a href="${esc(art.final)}" target="_blank">打开</a></li>` : ""}
          ${art.clips.map((c) => `<li><span>拆条 · 场${c.scene_no}</span><a href="${esc(c.url)}" target="_blank">打开</a></li>`).join("")}
        </ul>`;
      const playBtn = panel.querySelector("#panel-play");
      if (playBtn) playBtn.onclick = () => openPlayer(this.data);
      const exportBtn = panel.querySelector("#panel-export");
      if (exportBtn) exportBtn.onclick = () => exportEpisode(this.data);
      bindRegen(panel, this.data.episode.id, () => this.data);
      bindIo(panel, this.data.episode.id,
             () => renderCanvasView(this.data.episode.id));
      return;
    }
    const shot = this.data.storyboard.shots.find((s) => s.shot_no === shotNo);
    const issues = this.shotIssues[shotNo] || [];
    const lineNo = this.lineNoOf(shot);
    const dims = shot.five_dimensions || {};
    const cam = dims.camera_design || {};
    const textAsset = shot.readable_text || {};
    panel.innerHTML = `
      <h3>${esc(shot.unit_id || `镜头 #${String(shotNo).padStart(2, "0")}`)} · 场${shot.scene_no}</h3>
      ${art.images[shotNo] ? `<img class="preview" src="${esc(art.images[shotNo])}" alt="关键图">` : ""}
      <h4>首尾帧</h4>
      <div class="thumbs">
        <figure>${art.first[shotNo] ? `<img src="${esc(art.first[shotNo])}">` : ""}<figcaption>首帧</figcaption></figure>
        <figure>${art.last[shotNo] ? `<img src="${esc(art.last[shotNo])}">` : ""}<figcaption>尾帧</figcaption></figure>
      </div>
      ${mediaTag(art.videos[shotNo]) ? `<h4>镜头视频</h4>${mediaTag(art.videos[shotNo])}` : ""}
      ${shot.dialogue ? `<h4>台词</h4><div class="dialogue"><b>${esc(shot.dialogue.character)}</b>:${esc(shot.dialogue.dialogue)}</div>` : ""}
      ${shot.dialogue_part?.total > 1 ? `<div class="dialogue-part">原句拆分 ${shot.dialogue_part.index}/${shot.dialogue_part.total} · ${esc(shot.dialogue_source || "")}</div>` : ""}
      ${lineNo != null && mediaTag(art.voices[lineNo]) ? mediaTag(art.voices[lineNo]) : ""}
      <h4>生产合同</h4>
      <ul class="links">
        <li><span>镜头功能</span><span>${esc(shot.shot_function || "-")}</span></li>
        <li><span>人物</span><span>${esc((shot.characters || []).join("、"))} · ${shot.character_count ?? 0}人</span></li>
        <li><span>时间码</span><span>${esc(shot.timecode || "-")} · ${fmt(shot.duration, 1)}s</span></li>
        <li><span>类型词</span><span>${esc(shot.type_word || shot.kind || "-")}</span></li>
        <li><span>剧本对应</span><span>${esc(shot.script_reference || "-")}</span></li>
      </ul>
      <h4>段间状态</h4>
      <div class="state-box"><b>起</b>${esc(stateInline(shot.start_state))}</div>
      <div class="state-box"><b>止</b>${esc(stateInline(shot.end_state))}</div>
      <h4>五维分镜</h4>
      <div class="dimension-list">
        <div><b>主体动势</b><span>${esc(dims.subject_motion || "-")}</span></div>
        <div><b>环境光影</b><span>${esc(dims.environment_light || "-")}</span></div>
        <div><b>摄影调度</b><span>${esc([cam.shot_scale, cam.angle, cam.lens, cam.camera_position, cam.movement].filter(Boolean).join(" · "))}</span></div>
        <div><b>时间状态</b><span>${esc(dims.time_state?.evolution || "-")}</span></div>
        <div><b>美学参数</b><span>${esc(dims.aesthetics?.render || "-")}</span></div>
      </div>
      <h4>表演与声音</h4>
      <div class="dimension-list">
        <div><b>表演目标</b><span>${esc(shot.performance?.goal || "-")}</span></div>
        <div><b>视线</b><span>${esc(shot.performance?.gaze || "-")}</span></div>
        <div><b>微表情</b><span>${esc(shot.performance?.micro_expression || "-")}</span></div>
        <div><b>环境声</b><span>${esc(shot.sound_design?.environment || "-")}</span></div>
        <div><b>音乐策略</b><span>${esc(shot.sound_design?.music || "-")}</span></div>
      </div>
      <h4>文字资产</h4>
      <div class="text-contract ${textAsset.required ? "required" : "clear"}">
        ${textAsset.required
          ? `需关键帧锁定 · ${esc(textAsset.carrier)} · 白名单:${esc((textAsset.whitelist || []).join("、") || "空")}`
          : "无可读文字 · 禁止字幕条与乱码"}
      </div>
      <h4>Seedance 执行提示词</h4>
      <div class="prompt">${esc(shot.seedance_prompt || shot.prompt)}</div>
      <h4>产物</h4>
      <ul class="links">
        ${this.link("关键图", art.images[shotNo])}
        ${this.link("首帧", art.first[shotNo])}
        ${this.link("尾帧", art.last[shotNo])}
        ${this.link("视频", art.videos[shotNo])}
        ${lineNo != null ? this.link("配音", art.voices[lineNo]) : ""}
      </ul>
      <h4>画面不满意?</h4>
      ${regenControls({ kind: "shot", shot_no: shotNo }, "附意见重画本镜头")}
      <h4>下载修改后上传</h4>
      <div class="io-row"><span>画面</span>
        ${ioControls({ kind: "shot", shot_no: shotNo },
          art.images[shotNo], `shot_${shotNo}.png`)}</div>
      <div class="io-row"><span>视频</span>
        ${ioControls({ kind: "shot_video", shot_no: shotNo },
          art.videos[shotNo], `shot_${shotNo}.mp4`)}</div>
      ${issues.length ? `<h4>质检问题</h4>${issues.map((i) => `
        <div class="issue ${esc(i.severity)}">[${esc(i.check)}] ${esc(i.message)}</div>`).join("")}` : ""}`;
    const epId = this.data.episode.id;
    bindRegen(panel, epId, () => this.data);
    bindIo(panel, epId, () => renderCanvasView(epId));
  }

  link(label, url) {
    return `<li><span>${label}</span>${url ? `<a href="${esc(url)}" target="_blank">打开</a>` : `<span class="empty">无</span>`}</li>`;
  }

  lineNoOf(shot) {
    if (!shot.dialogue) return null;
    let n = 0;
    for (const scene of this.data.script.scenes) {
      for (const line of scene.lines) {
        n += 1;
        if (scene.scene_no === shot.scene_no &&
            line.character === shot.dialogue.character &&
            line.dialogue === (shot.dialogue_source || shot.dialogue.dialogue)) return n;
      }
    }
    return null;
  }

  /* ---- 交互:平移 / 缩放 / 拖拽卡片 / 选中 ---- */
  applyTransform() {
    this.world.style.transform =
      `translate(${this.tx}px, ${this.ty}px) scale(${this.scale})`;
    const pct = document.getElementById("zoom-pct");
    if (pct) pct.textContent = `${Math.round(this.scale * 100)}%`;
  }

  setScale(next, cx, cy) {
    next = Math.min(2.5, Math.max(0.2, next));
    const rect = this.viewport.getBoundingClientRect();
    const px = (cx ?? rect.width / 2), py = (cy ?? rect.height / 2);
    // 以光标为锚点缩放
    this.tx = px - (px - this.tx) * (next / this.scale);
    this.ty = py - (py - this.ty) * (next / this.scale);
    this.scale = next;
    this.applyTransform();
  }

  bounds() {
    const xs = Object.values(this.positions);
    if (!xs.length) return { x: 0, y: 0, w: 800, h: 600 };
    const minX = Math.min(...xs.map((p) => p.x)) - LANE_X;
    const minY = Math.min(...xs.map((p) => p.y));
    const maxX = Math.max(...xs.map((p) => p.x)) + CARD_W;
    const maxY = Math.max(...xs.map((p) => p.y)) + CARD_H;
    return { x: minX, y: minY, w: maxX - minX, h: maxY - minY };
  }

  fit() {
    const b = this.bounds();
    const rect = this.viewport.getBoundingClientRect();
    const scale = Math.min(2.5, Math.max(0.2, Math.min(
      (rect.width - 80) / b.w, (rect.height - 80) / b.h)));
    this.scale = scale;
    this.tx = (rect.width - b.w * scale) / 2 - b.x * scale;
    this.ty = (rect.height - b.h * scale) / 2 - b.y * scale;
    this.applyTransform();
  }

  bind() {
    const vp = this.viewport;
    document.getElementById("zoom-in").onclick = () => this.setScale(this.scale * 1.25);
    document.getElementById("zoom-out").onclick = () => this.setScale(this.scale / 1.25);
    document.getElementById("zoom-fit").onclick = () => this.fit();
    document.getElementById("layout-reset").onclick = () => {
      localStorage.removeItem(this.layoutKey);
      this.positions = this.defaultPositions();
      this.renderWorld(); this.fit();
    };

    vp.addEventListener("wheel", (ev) => {
      ev.preventDefault();
      const rect = vp.getBoundingClientRect();
      const factor = ev.deltaY < 0 ? 1.12 : 1 / 1.12;
      this.setScale(this.scale * factor,
        ev.clientX - rect.left, ev.clientY - rect.top);
    }, { passive: false });

    let drag = null;
    vp.addEventListener("pointerdown", (ev) => {
      const card = ev.target.closest(".shot-card");
      const rect = vp.getBoundingClientRect();
      if (card) {
        const no = Number(card.dataset.shot);
        drag = {
          kind: "card", no, card, moved: false,
          startX: ev.clientX, startY: ev.clientY,
          origin: { ...this.positions[no] },
        };
      } else {
        drag = { kind: "pan", startX: ev.clientX, startY: ev.clientY,
                 origin: { tx: this.tx, ty: this.ty }, moved: false };
        vp.classList.add("panning");
      }
      vp.setPointerCapture(ev.pointerId);
    });
    vp.addEventListener("pointermove", (ev) => {
      if (!drag) return;
      const dx = ev.clientX - drag.startX, dy = ev.clientY - drag.startY;
      if (Math.abs(dx) + Math.abs(dy) > 3) drag.moved = true;
      if (drag.kind === "pan") {
        this.tx = drag.origin.tx + dx; this.ty = drag.origin.ty + dy;
        this.applyTransform();
      } else {
        const p = this.positions[drag.no];
        p.x = drag.origin.x + dx / this.scale;
        p.y = drag.origin.y + dy / this.scale;
        drag.card.style.left = `${p.x}px`;
        drag.card.style.top = `${p.y}px`;
      }
    });
    vp.addEventListener("pointerup", (ev) => {
      if (!drag) return;
      vp.classList.remove("panning");
      if (drag.kind === "card") {
        if (drag.moved) this.saveLayout();
        else this.select(drag.no);
      } else if (!drag.moved) {
        this.select(null);
      }
      drag = null;
    });
  }

  select(shotNo) {
    this.selected = shotNo;
    this.world.querySelectorAll(".shot-card").forEach((c) =>
      c.classList.toggle("selected", Number(c.dataset.shot) === shotNo));
    this.renderSidePanel(shotNo);
  }
}

/* 所有路由依赖的常量、渲染函数与画布类均完成初始化后再启动应用。 */
window.addEventListener("hashchange", route);
route();
