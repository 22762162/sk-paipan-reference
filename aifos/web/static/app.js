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

/* 页面内置提示(不用 alert/confirm——沙箱环境会静默拦截弹窗) */
function showToast(message, kind = "info") {
  document.querySelectorAll(".toast").forEach((t) => t.remove());
  const toast = document.createElement("div");
  toast.className = `toast ${kind}`;
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
  awaiting_confirm: "待确认", script: "剧本中", cast: "画人物场景",
  storyboard: "分镜中", images: "出图中",
  frames: "首尾帧", videos: "视频中", voices: "配音中", edit: "剪辑中",
  qc: "质检中", package: "包装中", archive: "沉淀中", running: "制作中",
};
function chip(status) {
  const cls = ["done", "failed", "qc_failed", "awaiting_confirm"].includes(status)
    ? status : "running";
  return `<span class="chip ${cls}">${esc(STATUS_CN[status] || status)}</span>`;
}

/* ---------- 路由 ---------- */
window.addEventListener("hashchange", route);
route();

function route() {
  if (pollTimer) { clearInterval(pollTimer); pollTimer = null; }
  const m = location.hash.match(/^#\/episode\/(\d+)$/);
  if (m) renderCanvasView(Number(m[1]));
  else if (location.hash === "#/settings") renderSettings();
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
      <div class="produce-hint">全自动完成:剧本 → 分镜 → 画面 → 配音 → 成片。做完后点下方剧集,再点「▶ 播放本集」观看。</div>
    </form>
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
  const btn = form.querySelector("button");
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
      <span class="hint">剧本 / 图片 / 视频每个环节都能选 CLI 或 API,按顺序自动回退;都没配好也有内置产线兜底,流程不会断</span>
    </div>
    <div class="panel">
      <h2>能力路由 · 每个环节谁来干(按顺序尝试)</h2>
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
    </div>
    <div class="settings-grid">
      ${data.providers.map(providerCard).join("")}
    </div>
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
  const isApi = ["api", "claude_api", "image_api", "ark_video"].includes(p.type);
  const isCli = ["cli", "dreamina"].includes(p.type);
  const state = p.ready ? ["done", "就绪"]
    : p.enabled ? ["qc_failed", "待配置"] : ["", "未启用"];
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
    ${isCli ? `<label class="set-row"><span>命令</span>
      <input data-field="command" value="${esc(p.command)}"
        placeholder="可执行文件或整串命令"></label>` : ""}
    ${p.type === "dreamina" ? `<label class="set-row"><span>模型版本</span>
      <input data-field="model_version" value="${esc(p.model_version)}"></label>` : ""}
    ${isApi ? `
      <label class="set-row"><span>接口地址</span>
        <input data-field="endpoint" value="${esc(p.endpoint)}"></label>
      <label class="set-row"><span>API Key</span>
        <input data-field="api_key" type="password"
          placeholder="${p.api_key_set
            ? `已保存(${esc(p.api_key_masked)}),留空保持不变`
            : "粘贴 API Key"}"></label>
      ${p.type !== "api" ? `<label class="set-row"><span>模型</span>
        <input data-field="model" value="${esc(p.model)}"></label>` : ""}` : ""}
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
        <div class="player-sub" id="pl-sub" hidden></div>
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
  const elSub = overlay.querySelector("#pl-sub");
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
    if (item.shot.dialogue) {
      elSub.innerHTML = `<b>${esc(item.shot.dialogue.character)}</b>${esc(item.shot.dialogue.dialogue)}`;
      elSub.hidden = false;
    } else { elSub.hidden = true; }
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
            line.dialogue === shot.dialogue.dialogue) return n;
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

const STAGE_CN = {
  script: "剧本", cast: "人物/场景图", storyboard: "分镜", images: "镜头画面",
  frames: "首尾帧", videos: "视频", voices: "配音", edit: "剪映剪辑",
  qc: "AI质检", package: "封面/标题", archive: "数据沉淀",
  assets: "资产调用",
};
const STAGE_ORDER = ["script", "cast", "storyboard", "images", "frames",
  "videos", "voices", "edit", "qc", "package", "archive"];
const STAGE_PLAIN = {
  script: "正在写剧本", cast: "正在画人物和场景", storyboard: "正在画分镜",
  images: "正在生成镜头画面", frames: "正在生成首尾帧", videos: "正在生成视频",
  voices: "正在配音", edit: "正在剪辑成片", qc: "正在质量检查",
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
        <span>剧本、人物、场景、分镜已就绪</span></div>
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
  app.innerHTML = `
  <div class="canvas-view">
    ${awaiting ? `
    <div class="confirm-banner">
      <div>
        <b>预生产完成,请过目 👀</b>
        <span>剧本、人物、场景、分镜都已生成(见画布与右侧面板)。满意就点确认,
        视频 → 配音 → 剪辑 → 质检会全自动完成;不满意可改剧本后重新制作。</span>
      </div>
      <button class="primary" id="btn-confirm">✅ 确认,开始生产</button>
    </div>` : ""}
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
      showToast("已确认!正在自动生产视频、配音与成片,完成后即可播放", "ok");
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
          ${data.episode.qc_score != null ? `<span class="chip">质检 ${fmt(data.episode.qc_score, 0)} 分</span>` : ""}
        </div>
        <p class="hero-logline">${esc(script.logline || "")}</p>
        <div class="hero-actions">
          <button class="primary" id="hero-play">▶ 播放本集</button>
          <button id="hero-script">📖 剧本</button>
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
        <button class="primary play-cta" id="panel-play">▶ 播放本集(${fmt(this.data.storyboard.shots.reduce((a, s) => a + s.duration, 0), 0)}秒)</button>
        <button class="play-cta" id="panel-export">⬇ 下载成品包(成片/封面/文案)</button>
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
        ${qc ? `<h4>质检 · ${qc.score}分(线${qc.pass_score})</h4>
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
    panel.innerHTML = `
      <h3>镜头 #${String(shotNo).padStart(2, "0")} · 场${shot.scene_no}</h3>
      ${art.images[shotNo] ? `<img class="preview" src="${esc(art.images[shotNo])}" alt="关键图">` : ""}
      <h4>首尾帧</h4>
      <div class="thumbs">
        <figure>${art.first[shotNo] ? `<img src="${esc(art.first[shotNo])}">` : ""}<figcaption>首帧</figcaption></figure>
        <figure>${art.last[shotNo] ? `<img src="${esc(art.last[shotNo])}">` : ""}<figcaption>尾帧</figcaption></figure>
      </div>
      ${mediaTag(art.videos[shotNo]) ? `<h4>镜头视频</h4>${mediaTag(art.videos[shotNo])}` : ""}
      ${shot.dialogue ? `<h4>台词</h4><div class="dialogue"><b>${esc(shot.dialogue.character)}</b>:${esc(shot.dialogue.dialogue)}</div>` : ""}
      ${lineNo != null && mediaTag(art.voices[lineNo]) ? mediaTag(art.voices[lineNo]) : ""}
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
