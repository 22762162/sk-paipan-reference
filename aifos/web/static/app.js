/* AIFOS 控制台单页应用:仪表盘 + 分镜画布(原生 JS,零依赖) */
"use strict";

const app = document.getElementById("app");
const topbarRight = document.getElementById("topbar-right");
let pollTimer = null;

/* ---------- 工具 ---------- */
const esc = (s) => String(s ?? "").replace(/[&<>"']/g,
  (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));
const fmt = (n, d = 2) => n == null ? "-" : Number(n).toFixed(d);

async function api(path, opts) {
  const res = await fetch(path, opts);
  const data = await res.json().catch(() => ({}));
  if (!res.ok) throw new Error(data.error || `HTTP ${res.status}`);
  return data;
}

const STATUS_CN = {
  done: "完成", failed: "失败", qc_failed: "质检未过", created: "已建",
  script: "剧本中", storyboard: "分镜中", assets: "调资产", images: "出图中",
  frames: "首尾帧", videos: "视频中", voices: "配音中", edit: "剪辑中",
  qc: "质检中", package: "包装中", archive: "沉淀中", running: "制作中",
};
function chip(status) {
  const cls = ["done", "failed", "qc_failed"].includes(status) ? status : "running";
  return `<span class="chip ${cls}">${esc(STATUS_CN[status] || status)}</span>`;
}

/* ---------- 路由 ---------- */
window.addEventListener("hashchange", route);
route();

function route() {
  if (pollTimer) { clearInterval(pollTimer); pollTimer = null; }
  const m = location.hash.match(/^#\/episode\/(\d+)$/);
  if (m) renderCanvasView(Number(m[1]));
  else renderDashboard();
}

/* ================= 仪表盘 ================= */
async function renderDashboard() {
  topbarRight.innerHTML = "";
  let data;
  try { data = await api("/api/overview"); }
  catch (e) { app.innerHTML = `<div class="loading">加载失败:${esc(e.message)}</div>`; return; }

  const s = data.stats;
  const runningJobs = data.jobs.filter((j) => j.status === "running");
  topbarRight.innerHTML = runningJobs.length
    ? `<span class="chip running">${runningJobs.length} 个制作任务进行中</span>` : "";

  const maxStage = Math.max(1, ...data.cost_by_stage.map((r) => r.total || 0));
  app.innerHTML = `
  <div class="dash">
    <form class="produce-bar" id="produce-form">
      <input name="sentence" placeholder='一句话开工:开始制作《万妖图录》第15集' required>
      <input name="premise" placeholder="本集前提/梗概(可选)">
      <button class="primary" type="submit">开始制作</button>
      <div class="produce-hint">平台将自动完成:剧本 → 分镜 → 资产 → 图片 → 首尾帧 → 视频 → 配音 → 剪辑 → 质检 → 封面/标题/拆条 → 数据沉淀</div>
    </form>

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
      <h2>IP 资产沉淀</h2>
      ${Object.keys(data.asset_stats).length ? Object.entries(data.asset_stats).map(([proj, rows]) => `
        <div style="margin-bottom:8px"><b>${esc(proj)}</b></div>
        <div class="asset-chips" style="margin-bottom:12px">
          ${rows.map((r) => `<span class="chip">${esc(KIND_CN[r.kind] || r.kind)} ×${r.total}${r.reused ? ` · 复用${r.reused}` : ""}</span>`).join("")}
        </div>`).join("") : `<div class="empty">暂无资产</div>`}
    </div>

    <div class="panel">
      <h2>最近日志</h2>
      <div class="log-list" id="log-list"><div class="empty">加载中…</div></div>
    </div>
  </div>`;

  document.getElementById("produce-form").addEventListener("submit", onProduce);
  app.querySelectorAll("tr.clickable").forEach((tr) =>
    tr.addEventListener("click", () => { location.hash = `#/episode/${tr.dataset.ep}`; }));

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
  const btn = form.querySelector("button");
  btn.disabled = true; btn.textContent = "提交中…";
  try {
    await api("/api/produce", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({
        sentence: form.sentence.value,
        premise: form.premise.value,
      }),
    });
    renderDashboard();
  } catch (e) {
    alert("提交失败:" + e.message);
    btn.disabled = false; btn.textContent = "开始制作";
  }
}

const STAGE_CN = {
  script: "剧本", storyboard: "分镜", assets: "资产调用", images: "图片",
  frames: "首尾帧", videos: "视频", voices: "配音", edit: "剪映剪辑",
  qc: "AI质检", package: "封面/标题", archive: "数据沉淀",
};
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

  app.innerHTML = `
  <div class="canvas-view">
    <div class="canvas-toolbar">
      <button id="btn-back">← 仪表盘</button>
      <span class="title">《${esc(ep.project_title || data.project.title)}》第${ep.number}集</span>
      ${chip(ep.status)}
      <span class="hint">质检 ${ep.qc_score == null ? "-" : fmt(ep.qc_score, 0)} 分 · 成本 ${fmt(ep.cost)}</span>
      <span class="spacer"></span>
      <span class="hint">滚轮缩放 · 拖拽空白平移 · 拖动卡片摆放 · 点击查看详情</span>
      <button id="btn-reproduce" title="增量:已有产物复用,只补齐缺失">增量重制</button>
      <button id="btn-reproduce-force" title="全部重新生成(消耗额度)">强制重制</button>
      <div class="zoom-group">
        <button id="zoom-out">−</button>
        <span class="zoom-pct" id="zoom-pct">100%</span>
        <button id="zoom-in">＋</button>
        <button id="zoom-fit">适应</button>
        <button id="layout-reset">重排</button>
      </div>
    </div>
    <div class="canvas-body">
      <div id="viewport"><div id="world"></div></div>
      <aside id="sidepanel"></aside>
    </div>
    <div class="timeline" id="timeline"></div>
  </div>`;

  document.getElementById("btn-back").onclick = () => { location.hash = "#/"; };
  const reproduce = async (force) => {
    const label = force ? "强制重制(全部重新生成,会消耗额度)" : "增量重制(只补齐缺失产物)";
    if (!confirm(`确认${label}本集?`)) return;
    try {
      await api("/api/produce", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({
          title: data.project.title, episode: ep.number, force,
        }),
      });
      pollTimer = setInterval(() => renderCanvasView(episodeId), 3000);
    } catch (e) { alert("提交失败:" + e.message); }
  };
  document.getElementById("btn-reproduce").onclick = () => reproduce(false);
  document.getElementById("btn-reproduce-force").onclick = () => reproduce(true);
  // 制作进行中自动刷新画布
  if (!["done", "failed", "qc_failed", "created"].includes(ep.status))
    pollTimer = setInterval(() => renderCanvasView(episodeId), 3000);

  const canvas = new StoryboardCanvas(data, shotIssues, lineIssues);
  canvas.mount();
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
      const voiceOk = lineNo != null && !!art.voices[lineNo];
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
            ${shot.dialogue ? `<span class="badge ${voiceOk ? "ok" : "miss"}">${voiceOk ? "✓ 配音" : "✗ 配音"}</span>` : ""}
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
      panel.innerHTML = `
        <h3>本集总览</h3>
        ${art.cover ? `<img class="preview" src="${esc(art.cover)}" alt="封面">` : ""}
        ${art.titles.length ? `<h4>候选标题</h4><ul class="titles-list">
          ${art.titles.map((t) => `<li>${esc(t)}</li>`).join("")}</ul>` : ""}
        <h4>制作阶段</h4>
        <ul class="stage-list">
          ${this.data.tasks.map((t) => `<li>
            <span>${esc(t.name)}</span>
            <span style="display:flex;gap:8px;align-items:center">
              <span class="cost">${fmt(t.cost)}</span>${chip(t.status === "done" ? "done" : t.status === "failed" ? "failed" : "running")}
            </span></li>`).join("")}
        </ul>
        ${qc ? `<h4>质检 · ${qc.score}分(线${qc.pass_score})</h4>
          ${qc.issues.length ? qc.issues.map((i) => `
            <div class="issue ${esc(i.severity)}">[${esc(i.check)}] ${esc(i.message)}</div>`).join("")
          : `<div class="empty">全部检查通过</div>`}` : ""}
        <h4>成片与产物</h4>
        <ul class="links">
          ${art.final ? `<li><span>成片</span><a href="${esc(art.final)}" target="_blank">打开</a></li>` : ""}
          ${art.clips.map((c) => `<li><span>拆条 · 场${c.scene_no}</span><a href="${esc(c.url)}" target="_blank">打开</a></li>`).join("")}
        </ul>`;
      return;
    }
    const shot = this.data.storyboard.shots.find((s) => s.shot_no === shotNo);
    const issues = this.shotIssues[shotNo] || [];
    const lineNo = this.lineNoOf(shot);
    panel.innerHTML = `
      <h3>镜头 #${String(shotNo).padStart(2, "0")} · 场${shot.scene_no}</h3>
      ${art.images[shotNo] ? `<img class="preview" src="${esc(art.images[shotNo])}" alt="关键图">` : ""}
      <h4>首尾帧</h4>
      <div class="thumbs">
        <figure>${art.first[shotNo] ? `<img src="${esc(art.first[shotNo])}">` : ""}<figcaption>首帧</figcaption></figure>
        <figure>${art.last[shotNo] ? `<img src="${esc(art.last[shotNo])}">` : ""}<figcaption>尾帧</figcaption></figure>
      </div>
      ${shot.dialogue ? `<h4>台词</h4><div class="dialogue"><b>${esc(shot.dialogue.character)}</b>:${esc(shot.dialogue.dialogue)}</div>` : ""}
      <h4>镜头信息</h4>
      <ul class="links">
        <li><span>机位</span><span>${esc(shot.camera || "-")}</span></li>
        <li><span>时长</span><span>${fmt(shot.duration, 1)}s</span></li>
        <li><span>类型</span><span>${shot.kind === "dialogue" ? "对白镜头" : "环境镜头"}</span></li>
      </ul>
      <h4>生成 Prompt</h4>
      <div class="prompt">${esc(shot.prompt)}</div>
      <h4>产物</h4>
      <ul class="links">
        ${this.link("关键图", art.images[shotNo])}
        ${this.link("首帧", art.first[shotNo])}
        ${this.link("尾帧", art.last[shotNo])}
        ${this.link("视频", art.videos[shotNo])}
        ${lineNo != null ? this.link("配音", art.voices[lineNo]) : ""}
      </ul>
      ${issues.length ? `<h4>质检问题</h4>${issues.map((i) => `
        <div class="issue ${esc(i.severity)}">[${esc(i.check)}] ${esc(i.message)}</div>`).join("")}` : ""}`;
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
            line.dialogue === shot.dialogue.dialogue) return n;
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
