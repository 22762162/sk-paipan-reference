/* AIFOS V3.2 工作台:生产总览 + 制作标准中心 + 分镜画布(原生 JS,零依赖) */
"use strict";

const app = document.getElementById("app");
const topbarRight = document.getElementById("topbar-right");
let pollTimer = null;
let standardsDraft = null;
let standardsBaseline = null;
let standardsMeta = null;
let standardsDirty = false;
let deferredInstallPrompt = null;
const MODERN_OTOME_STYLE = "现代都市乙女游戏CG，精致3D半写实角色渲染，亚洲当代青年，现代发型与时尚通勤服装，清透自然皮肤，细腻五官，柔和电影灯光，高级时尚杂志质感；禁止古装、汉服、发簪、长袍、水墨、国风、2D平涂、动漫线稿和历史建筑";

/* ---------- 工具 ---------- */
const esc = (s) => String(s ?? "").replace(/[&<>"']/g,
  (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));
const fmt = (n, d = 2) => n == null ? "-" : Number(n).toFixed(d);
const dateTime = (ts) => ts ? new Date(Number(ts) * 1000).toLocaleString("zh-CN", {
  month: "2-digit", day: "2-digit", hour: "2-digit", minute: "2-digit", second: "2-digit",
}) : "-";
const durationText = (seconds) => {
  const value = Math.max(0, Number(seconds) || 0);
  if (value < 60) return `${Math.round(value)} 秒`;
  if (value < 3600) return `${Math.floor(value / 60)} 分 ${Math.round(value % 60)} 秒`;
  return `${Math.floor(value / 3600)} 小时 ${Math.round(value % 3600 / 60)} 分`;
};

/* 服务自动更新后 build 变化 → 页面自动刷新,用户零操作 */
let appBuild = null;
function watchBuild(data) {
  if (!data || !data.build) return;
  if (appBuild === null) { appBuild = data.build; return; }
  if (appBuild !== data.build) {
    showToast("平台已自动更新到新版本,页面即将自动刷新…", "ok");
    setTimeout(() => location.reload(), 1500);
    appBuild = data.build;
  }
}

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

async function copyText(text) {
  try {
    await navigator.clipboard.writeText(text);
  } catch (_) {
    const input = document.createElement("textarea");
    input.value = text;
    input.setAttribute("readonly", "");
    input.style.position = "fixed";
    input.style.opacity = "0";
    document.body.appendChild(input);
    input.select();
    document.execCommand("copy");
    input.remove();
  }
}

function bindMobileAccessButtons(root = document) {
  root.querySelectorAll("[data-mobile-access], #mobile-access-global").forEach((button) => {
    if (button.dataset.mobileBound) return;
    button.dataset.mobileBound = "1";
    button.addEventListener("click", showMobileAccess);
  });
}

async function showMobileAccess() {
  document.getElementById("mobile-access-overlay")?.remove();
  let access;
  try {
    access = await api("/api/access");
  } catch (error) {
    showToast(`读取手机访问地址失败：${error.message}`, "error");
    return;
  }
  const browserIsLocal = ["127.0.0.1", "localhost", "::1"].includes(location.hostname);
  const urls = [...(access.lan_urls || [])];
  if (access.hostname_url) urls.push(access.hostname_url);
  if (!browserIsLocal && !urls.includes(`${location.origin}/`)) urls.unshift(`${location.origin}/`);
  const preferred = urls[0] || access.local_url;
  const standalone = matchMedia("(display-mode: standalone)").matches || navigator.standalone === true;
  const overlay = document.createElement("div");
  overlay.className = "script-overlay mobile-access-overlay";
  overlay.id = "mobile-access-overlay";
  overlay.setAttribute("role", "dialog");
  overlay.setAttribute("aria-modal", "true");
  overlay.setAttribute("aria-label", "手机访问与安装");
  overlay.innerHTML = `<div class="script-panel mobile-access-panel">
    <div class="script-head">
      <div><span class="mobile-kicker">AIFOS MOBILE</span><h3>手机打开与安装</h3></div>
      <button type="button" class="mobile-access-close" aria-label="关闭">✕</button>
    </div>
    <div class="mobile-access-hero">
      <img src="/static/assets/icon-192.png" alt="AIFOS 应用图标">
      <div><strong>把漫剧工作台装进口袋</strong>
        <p>手机和这台电脑连接同一 Wi-Fi，即可审片、看历史、选图和调整制作标准。</p></div>
    </div>
    ${access.lan_enabled ? `<div class="mobile-url-list">
      <span>手机浏览器输入</span>
      ${urls.length ? urls.map((url, index) => `<a href="${esc(url)}" data-mobile-url="${esc(url)}"${index ? "" : ' class="preferred"'}>${esc(url)}</a>`).join("")
        : `<p class="mobile-access-warning">暂未识别到 Wi-Fi 地址，请确认电脑已经连接 Wi-Fi 后重试。</p>`}
      ${access.lan_enabled && preferred ? `<div class="mobile-qr"><img src="/qr.svg?data=${encodeURIComponent(preferred)}" width="168" height="168" alt="局域网地址二维码" loading="lazy"><span>同一 Wi-Fi：手机扫码直接打开</span></div>` : ""}
    </div>` : `<p class="mobile-access-warning">当前服务仅允许本机访问。请用“启动 AIFOS”启动器重新打开，或使用 <code>aifos serve --lan</code>。</p>`}
    <div class="mobile-public-access${access.public_url ? "" : " muted"}">
      <span>外网访问（不在同一 Wi-Fi 也能连）</span>
      ${access.public_url ? `<a href="${esc(access.public_url)}" data-mobile-url="${esc(access.public_url)}" class="preferred">${esc(access.public_url)}</a>
      <div class="mobile-qr"><img src="/qr.svg?data=${encodeURIComponent(access.public_url)}" width="200" height="200" alt="外网地址二维码" loading="lazy"><span>手机扫一扫对准二维码即可打开</span></div>
      <p class="mobile-hint">地址变了就在电脑上重跑 <code>aifos tunnel</code> 刷新二维码，手机无需重装 App。</p>`
        : `<p class="mobile-hint">在电脑上开一个终端运行 <code>aifos tunnel</code>，这里会自动出现公网地址与二维码，手机扫码即连——外网地址换了也不用重装 App。命名隧道可获得永不改变的稳定地址。</p>`}
    </div>
    <div class="mobile-access-actions">
      <button type="button" class="primary" id="mobile-copy-url" ${urls.length ? "" : "disabled"}>复制手机网址</button>
      <button type="button" id="mobile-share-url" ${urls.length ? "" : "disabled"}>发送到手机</button>
      ${standalone ? `<button type="button" disabled>已在主屏幕运行</button>`
        : deferredInstallPrompt ? `<button type="button" id="mobile-pwa-install">安装到桌面</button>` : ""}
    </div>
    <div class="mobile-install-guide">
      <div><b>iPhone / iPad</b><span>${esc(access.install.ios)}</span></div>
      <div><b>Android</b><span>${esc(access.install.android)}</span></div>
    </div>
    <p class="mobile-access-warning">地址必须以 <code>http://</code> 开头。手机若提示
    “TLS 错误/安全连接失败”，是因为用了 <code>https://</code>——本地服务是普通 HTTP，
    把服务器地址改成上面的 http:// 开头地址即可。</p>
    <p class="mobile-security-note">仅在你信任的同一 Wi-Fi 内使用；不要把此地址公开到互联网。</p>
  </div>`;
  document.body.appendChild(overlay);
  const close = () => overlay.remove();
  overlay.querySelector(".mobile-access-close").addEventListener("click", close);
  overlay.addEventListener("click", (event) => { if (event.target === overlay) close(); });
  overlay.querySelector("#mobile-copy-url")?.addEventListener("click", async () => {
    await copyText(preferred);
    showToast("手机网址已复制", "ok");
  });
  overlay.querySelector("#mobile-share-url")?.addEventListener("click", async () => {
    if (navigator.share) {
      try { await navigator.share({ title: "AIFOS 手机工作台", text: "打开 AIFOS", url: preferred }); }
      catch (_) { /* 用户取消分享 */ }
    } else {
      await copyText(preferred);
      showToast("当前浏览器不支持直接分享，网址已复制", "ok");
    }
  });
  overlay.querySelector("#mobile-pwa-install")?.addEventListener("click", async () => {
    deferredInstallPrompt.prompt();
    await deferredInstallPrompt.userChoice;
    deferredInstallPrompt = null;
    close();
  });
  overlay.querySelector(".mobile-access-close").focus();
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
  queued_script: "已排队",
  awaiting_confirm: "待确认", awaiting_script: "剧本待确认",
  awaiting_cast: "人物待定版",
  cancelling: "正在暂停…",
  script: "剧本中", continuity: "锁连续性",
  cast: "画人物场景", storyboard: "五维分镜中", images: "关键帧中",
  text_assets: "锁文字", frames: "首尾帧", preflight: "门禁检查",
  videos: "视频中", voices: "声音/口型", edit: "剪辑中",
  qc: "质检中", package: "包装中", archive: "沉淀中", running: "制作中",
};
const RUN_STATUS_CN = {
  running: "运行中", cancelling: "暂停中", completed: "完成",
  paused: "阶段暂停", failed: "失败", stopped: "已停止",
  interrupted: "意外中断",
};
const ACTION_CN = {
  produce: "开始制作", script_import: "导入剧本制作",
  series_import: "多集文档导入", series_next: "串行准备下一集",
  confirm_script: "确认剧本后续产", confirm_cast: "确认人物定版后续产",
  confirm_preflight: "确认开拍后续产",
  force_rebuild: "全部重做", revise_script: "修改剧本",
  regen_image: "重画图片", adjustment: "制作调整",
  legacy_import: "历史记录回填",
};
function runChip(status) {
  return `<span class="run-status ${esc(status)}">${esc(RUN_STATUS_CN[status] || status)}</span>`;
}
function chip(status) {
  const cls = ["done", "failed", "qc_failed", "awaiting_confirm",
    "awaiting_script", "awaiting_cast"].includes(status)
    ? status : "running";
  return `<span class="chip ${cls}">${esc(STATUS_CN[status] || status)}</span>`;
}

/* ---------- 路由 ---------- */
function route() {
  if (pollTimer) { clearInterval(pollTimer); pollTimer = null; }
  if (typeof liveTicker !== "undefined" && liveTicker) {
    clearInterval(liveTicker); liveTicker = null;
  }
  const standards = location.hash.match(/^#\/standards(?:\/([a-z_]+))?$/);
  const history = location.hash.match(/^#\/history(?:\/(\d+))?$/);
  const m = location.hash.match(/^#\/episode\/(\d+)$/);
  const settings = location.hash === "#/settings";
  const assets = location.hash === "#/assets";
  const area = standards ? "standards" : history ? "history"
    : settings ? "settings" : assets ? "assets" : "dashboard";
  document.querySelectorAll(".main-nav a").forEach((link) => {
    const active = link.dataset.nav === area;
    link.classList.toggle("active", active);
    if (active) link.setAttribute("aria-current", "page");
    else link.removeAttribute("aria-current");
  });
  if (standards) renderStandards(standards[1] || "production");
  else if (history) renderHistory(history[1] ? Number(history[1]) : null);
  else if (m) renderCanvasView(Number(m[1]));
  else if (settings) renderSettings();
  else if (assets) renderAssetsCenter();
  else renderDashboard();
}

/* ================= 持久生产历史 ================= */
const HISTORY_STAGE_CN = {
  script: "剧本", continuity: "连续性圣经", cast: "人物/场景图",
  storyboard: "五维分镜", blocking: "空间调度图", images: "关键帧", text_assets: "文字锁定",
  frames: "首尾帧", preflight: "生产门禁", videos: "Seedance 视频",
  voices: "声音/口型", edit: "剪辑", qc: "三层质检",
  package: "封面/标题", archive: "数据沉淀",
};

function historyTable(items) {
  if (!items.length) return `<div class="history-empty">
    <strong>没有符合条件的记录</strong><span>调整筛选条件，或开始一次新的制作。</span></div>`;
  return `<div class="history-table-wrap"><table class="history-table">
    <thead><tr><th>开始时间</th><th>作品 / 剧集</th><th>生产操作</th><th>结果</th>
      <th>最后阶段</th><th class="num">耗时</th><th class="num">本次成本</th><th>管理</th></tr></thead>
    <tbody>${items.map((run) => `<tr>
      <td><a class="history-time" href="#/history/${run.id}">${dateTime(run.started_at)}</a>
        <small>#${run.id}${run.source === "migration" ? " · 旧记录" : ""}</small></td>
      <td><a class="history-project" href="#/history/${run.id}">${esc(run.current_project || run.project_title)}</a>
        <small>第 ${run.episode_number} 集${run.force ? " · 全部重做" : ""}</small></td>
      <td><span class="action-tag">${esc(ACTION_CN[run.action] || run.action)}</span></td>
      <td>${runChip(run.status)}${run.error ? `<small class="history-error-mini">${esc(run.error)}</small>` : ""}</td>
      <td>${esc(HISTORY_STAGE_CN[run.last_stage] || run.last_stage || "尚未进入阶段")}
        <small>${run.stage_count} 个阶段 · ${(run.providers || []).map(esc).join(" / ") || "无 Provider"}</small></td>
      <td class="num">${durationText(run.duration_seconds)}</td>
      <td class="num">${fmt(run.cost)}</td>
      <td><button class="danger history-delete-row" data-run-id="${run.id}"
        title="删除这集作品，可选择是否保留资产中心图片">删除作品</button></td>
    </tr>`).join("")}</tbody></table></div>`;
}

function bindHistoryDeleteButtons(items) {
  const runs = new Map(items.map((run) => [String(run.id), run]));
  document.querySelectorAll(".history-delete-row").forEach((button) => {
    button.addEventListener("click", (event) => {
      event.stopPropagation();
      const run = runs.get(button.dataset.runId);
      if (run) showHistoryDeleteDialog(
        { ...run, run_id: run.id }, () => renderHistory());
    });
  });
}

async function renderHistory(runId = null) {
  topbarRight.innerHTML = `<span class="history-live">SQLite 持久记录 · 重启不丢失</span>`;
  if (runId) return renderHistoryDetail(runId);
  app.innerHTML = `<div class="loading">正在读取生产历史…</div>`;
  let data;
  try { data = await api("/api/history?limit=500"); }
  catch (e) { app.innerHTML = `<div class="loading">历史加载失败：${esc(e.message)}</div>`; return; }
  const s = data.stats;
  app.innerHTML = `<div class="history-page">
    <div class="history-hero">
      <div><p class="eyebrow">PRODUCTION LEDGER</p><h1>生产历史</h1>
        <p>每次开工、续产、重做、修改和失败都永久记录。点击任意记录查看阶段、Provider、耗时与错误。</p></div>
      <div class="history-assurance"><b>持久化</b><span>记录保存在正式工作区数据库，不依赖浏览器或当前服务进程。</span></div>
    </div>
    <section class="history-kpis">
      <div><span>全部运行</span><strong>${s.total}</strong></div>
      <div><span>完成</span><strong>${s.completed}</strong></div>
      <div><span>暂停待确认</span><strong>${s.paused}</strong></div>
      <div><span>失败 / 中断</span><strong>${s.failed + s.interrupted}</strong></div>
      <div><span>累计运行成本</span><strong>${fmt(s.total_cost)}</strong></div>
    </section>
    <section class="history-ledger panel">
      <div class="history-toolbar">
        <div><h2>运行台账</h2><p>新记录实时写入，旧剧集已自动回填。</p></div>
        <label class="history-search"><span>搜索</span><input id="history-query" placeholder="作品名或集数"></label>
        <label><span>状态</span><select id="history-status"><option value="">全部状态</option>
          ${Object.entries(RUN_STATUS_CN).map(([value, label]) => `<option value="${value}">${label}</option>`).join("")}</select></label>
        <label><span>操作</span><select id="history-action"><option value="">全部操作</option>
          ${(data.filters.actions || []).map((value) => `<option value="${esc(value)}">${esc(ACTION_CN[value] || value)}</option>`).join("")}</select></label>
      </div>
      <div id="history-results">${historyTable(data.items)}</div>
    </section>
  </div>`;

  const applyFilters = () => {
    const query = document.getElementById("history-query").value.trim().toLowerCase();
    const status = document.getElementById("history-status").value;
    const action = document.getElementById("history-action").value;
    const filtered = data.items.filter((run) => {
      const haystack = `${run.current_project || run.project_title} ${run.episode_number}`.toLowerCase();
      return (!query || haystack.includes(query)) && (!status || run.status === status) &&
        (!action || run.action === action);
    });
    document.getElementById("history-results").innerHTML = historyTable(filtered);
    bindHistoryDeleteButtons(filtered);
  };
  bindHistoryDeleteButtons(data.items);
  document.getElementById("history-query").addEventListener("input", applyFilters);
  document.getElementById("history-status").addEventListener("change", applyFilters);
  document.getElementById("history-action").addEventListener("change", applyFilters);
}

async function renderHistoryDetail(runId) {
  app.innerHTML = `<div class="loading">正在读取运行 #${runId}…</div>`;
  let run;
  try { run = await api(`/api/history/${runId}`); }
  catch (e) { app.innerHTML = `<div class="loading">记录加载失败：${esc(e.message)}</div>`; return; }
  const stages = (run.summary?.stages?.length ? run.summary.stages : run.tasks) || [];
  const project = run.current_project || run.project_title;
  app.innerHTML = `<div class="history-page history-detail-page">
    <div class="history-detail-head">
      <a href="#/history" class="back-link">← 返回全部历史</a>
      <div class="history-detail-title"><div><p class="eyebrow">RUN #${run.id}</p>
        <h1>${esc(project)} · 第 ${run.episode_number} 集</h1>
        <p>${esc(ACTION_CN[run.action] || run.action)} · ${dateTime(run.started_at)}</p></div>${runChip(run.status)}</div>
      <div class="history-detail-actions">
        ${run.episode_id ? `<a class="button-link primary" href="#/episode/${run.episode_id}">打开本集</a>` : ""}
        <a class="button-link" href="#/history">查看全部记录</a>
        <button class="danger" id="history-delete-work">删除这集作品</button>
      </div>
    </div>
    <section class="history-kpis detail-kpis">
      <div><span>最终状态</span><strong>${esc(STATUS_CN[run.result_status] || RUN_STATUS_CN[run.status] || run.result_status || "-")}</strong></div>
      <div><span>总耗时</span><strong>${durationText(run.duration_seconds)}</strong></div>
      <div><span>本次成本</span><strong>${fmt(run.cost)}</strong></div>
      <div><span>阶段</span><strong>${run.stage_count || stages.length}</strong></div>
      <div><span>Provider</span><strong class="provider-value">${(run.providers || []).map(esc).join(" / ") || "-"}</strong></div>
    </section>
    ${run.error ? `<section class="history-error"><b>运行异常</b><p>${esc(run.error)}</p></section>` : ""}
    <section class="history-detail-grid">
      <div class="panel history-stage-panel"><div class="section-title"><div><h2>阶段时间线</h2><p>按实际执行顺序保留每一步结果。</p></div></div>
        <ol class="run-timeline">${stages.length ? stages.map((stage, index) => {
          const status = stage.status || "done";
          const providers = Array.isArray(stage.providers) ? stage.providers.join(" / ") : stage.provider;
          return `<li class="${esc(status)}"><span class="timeline-index">${String(index + 1).padStart(2, "0")}</span>
            <div><div class="timeline-title"><b>${esc(stage.name || HISTORY_STAGE_CN[stage.stage] || stage.stage || "运行阶段")}</b>${runChip(status === "done" ? "completed" : status === "stopped" ? "stopped" : status === "interrupted" ? "interrupted" : status === "failed" ? "failed" : "running")}</div>
            <p>${esc(providers || "无外部 Provider")} · 成本 ${fmt(stage.cost || 0)}${stage.error ? ` · ${esc(stage.error)}` : ""}</p></div></li>`;
        }).join("") : `<li class="empty-stage"><span class="timeline-index">–</span><div><b>尚无阶段记录</b><p>任务可能在进入生产阶段前结束。</p></div></li>`}</ol>
      </div>
      <aside class="panel history-meta-panel"><h2>运行信息</h2><dl>
        <div><dt>运行编号</dt><dd>#${run.id}</dd></div>
        <div><dt>操作类型</dt><dd>${esc(ACTION_CN[run.action] || run.action)}</dd></div>
        <div><dt>数据来源</dt><dd>${run.source === "migration" ? "旧记录自动回填" : "正式 Web 工作台"}</dd></div>
        <div><dt>开始</dt><dd>${dateTime(run.started_at)}</dd></div>
        <div><dt>结束</dt><dd>${dateTime(run.finished_at)}</dd></div>
        <div><dt>最后阶段</dt><dd>${esc(HISTORY_STAGE_CN[run.last_stage] || run.last_stage || "-")}</dd></div>
        <div><dt>强制重做</dt><dd>${run.force ? "是" : "否"}</dd></div>
      </dl></aside>
    </section>
  </div>`;
  document.getElementById("history-delete-work").onclick = () =>
    showHistoryDeleteDialog({ ...run, run_id: run.id });
}

function showHistoryDeleteDialog(target, onDeleted = null) {
  const project = target.current_project || target.project_title || target.project;
  const episodeNumber = target.episode_number ?? target.number;
  const overlay = document.createElement("div");
  overlay.className = "script-overlay history-delete-overlay";
  overlay.innerHTML = `<div class="history-delete-dialog">
    <p class="eyebrow">DELETE WORK</p>
    <h2>删除《${esc(project)}》第 ${episodeNumber} 集？</h2>
    <p class="history-delete-warning">这会删除本集、全部生产运行记录、任务和文档。
      项目本身保留，已生成的物理文件不会被直接抹掉。</p>
    <fieldset class="history-delete-options">
      <label><input type="radio" name="history-asset-choice" value="keep" checked>
        <span><b>保留资产中心图片（推荐）</b><small>删除作品后仍可在资产中心按原作品查看和复用。</small></span></label>
      <label><input type="radio" name="history-asset-choice" value="delete">
        <span><b>同时从资产中心移除关联图片</b><small>采用软删除，历史版本与原文件仍保留；多集共用母资产不会误删。</small></span></label>
    </fieldset>
    <div class="history-delete-actions">
      <button class="cancel">取消</button>
      <button class="danger confirm">确认删除本集作品</button>
    </div>
  </div>`;
  const close = () => overlay.remove();
  overlay.querySelector(".cancel").onclick = close;
  overlay.onclick = (ev) => { if (ev.target === overlay) close(); };
  overlay.querySelector(".confirm").onclick = async (ev) => {
    const button = ev.currentTarget;
    const deleteAssets = overlay.querySelector(
      'input[name="history-asset-choice"]:checked').value === "delete";
    button.disabled = true;
    button.textContent = "正在删除…";
    try {
      const request = target.run_id != null
        ? { run_id: target.run_id } : { episode_id: target.episode_id };
      request.delete_assets = deleteAssets;
      const result = await api("/api/history/delete", {
        method: "POST", headers: { "Content-Type": "application/json" },
        body: JSON.stringify(request),
      });
      close();
      showToast(deleteAssets
        ? `作品已删除，${result.assets_soft_deleted || 0} 张关联图片已从资产中心移除`
        : "作品已删除，资产中心图片已保留", "ok");
      if (onDeleted) await onDeleted(result);
      else location.hash = "#/history";
    } catch (e) {
      showToast(e.message, "error");
      button.disabled = false;
      button.textContent = "确认删除本集作品";
    }
  };
  document.body.appendChild(overlay);
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
      { path: "rules.production.resolution", label: "默认输出分辨率", locked: true,
        help: "自动/中档默认 720P；用户把单镜 Seedance 质量改为低或高时，分别明确使用 480P 或 1080P，并记录在该镜头合同中。" },
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
    id: "script_development", label: "剧本第一道总闸门", icon: "02A",
    blurb: "小说和梗概先由编剧完成影视化改编；因果、人物信息、物理、时间、空间和道具生命周期不完整时，禁止开始任何视觉资产。",
    fields: [
      { path: "rules.script_development.required_before_any_visual_asset", label: "任何出图前必须通过剧本门禁", type: "boolean" },
      { path: "rules.script_development.source_material_is_adaptable", label: "小说/梗概属于可改编素材", type: "boolean" },
      { path: "rules.script_development.auto_adapt_imported_source", label: "导入小说自动完成影视化改编", type: "boolean" },
      { path: "rules.script_development.writer_completes_missing_details", label: "编剧主动补齐拍摄细节", type: "boolean" },
      { path: "rules.script_development.single_integrated_review", label: "一次综合审查，不多角色反复质检", type: "boolean" },
      { path: "rules.script_development.scene_boundary_contract_required", label: "逐场锁定前后连续性边界", type: "boolean" },
      { path: "rules.script_development.local_rewrite_enabled", label: "发现剧本根因时允许局部返编", type: "boolean" },
      { path: "rules.script_development.impact_analysis_before_rewrite", label: "返编前先做影响分析", type: "boolean" },
      { path: "rules.script_development.preserve_unaffected_assets", label: "保留未受影响的分镜和资产", type: "boolean" },
      { path: "rules.script_development.human_approval_if_scope_expands", label: "返编越界时必须人工确认", type: "boolean" },
      { path: "rules.script_development.local_rewrite_default_scope", label: "默认返编范围" },
      { path: "rules.script_development.required_review_dimensions", label: "综合审查维度", type: "list",
        help: "一次覆盖因果、动机、信息、物理、空间、时间、道具、世界规则、可拍性和事件密度。" },
      { path: "rules.script_development.scene_boundary_fields", label: "局部返编边界字段", type: "list" },
    ],
  },
  {
    id: "story_analysis", label: "剧本 AI 分析", icon: "02A",
    blurb: "在生成人物和场景前，先把故事世界、环境、视觉媒介与提示词母版锁成可调整的制作圣经。",
    fields: [
      { path: "rules.story_analysis.required_before_images", label: "出图前必须有制作圣经", type: "boolean" },
      { path: "rules.story_analysis.auto_analyze_uploaded_script", label: "上传剧本也自动分析", type: "boolean" },
      { path: "rules.story_analysis.user_style_is_hard_constraint", label: "用户画风为最高硬约束", type: "boolean" },
      { path: "rules.story_analysis.distinguish_world_from_render_medium", label: "区分故事时代与渲染媒介", type: "boolean" },
      { path: "rules.story_analysis.editable_before_lock", label: "开画前允许编辑和重分析", type: "boolean" },
      { path: "rules.story_analysis.resolve_character_entities_before_images", label: "出图前归一真实人物实体", type: "boolean" },
      { path: "rules.story_analysis.performance_cues_are_not_characters", label: "语气与动作不得当作人物", type: "boolean" },
      { path: "rules.story_analysis.final_character_image_prompt_required", label: "人物最终出图卡必填", type: "boolean" },
      { path: "rules.story_analysis.compact_prompt_compilation", label: "出图提示词编译为简洁终稿", type: "boolean" },
      { path: "rules.story_analysis.required_sections", label: "制作圣经必备模块", type: "list",
        help: "故事、世界、视觉、场景、人物和提示词母版，缺一项不进入出图。" },
      { path: "rules.story_analysis.downstream_consumers", label: "必须继承制作圣经的环节", type: "list" },
      { path: "rules.story_analysis.visible_text_policy", label: "画面文字策略" },
      { path: "rules.story_analysis.default_visual_fallback", label: "未指定画风时的兜底媒介" },
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
  { id: "gates", label: "门禁质检", icon: "07", blurb: "系统永久硬门不可关闭；镜头语言和表演启发只提示、不阻断。", fields: [] },
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
      <div><span class="gate-index">${String(index + 1).padStart(2, "0")}</span><b>${esc(gate.label || gate.id)}</b>
        <span class="rule-badge ${gate.mandatory ? "hard" : "adjustable"}">${gate.mandatory ? "系统永久硬门" : "导演建议"}</span>
        <p>${esc(gate.description || "生产前自动检查，未通过则停止消耗视频额度。")}</p></div>
      <div class="gate-controls"><label><span>失败级别</span><select data-gate-severity="${index}" aria-label="${esc(gate.label || gate.id)}失败级别" ${gate.mandatory ? "disabled" : ""}><option value="block" ${gate.severity !== "warning" ? "selected" : ""}>阻断开拍</option><option value="warning" ${gate.severity === "warning" ? "selected" : ""}>只警告</option></select></label><label class="switch"><input type="checkbox" data-gate-index="${index}" ${gate.enabled !== false ? "checked" : ""} ${gate.mandatory ? "disabled" : ""}><span aria-hidden="true"></span><em>${gate.mandatory ? "永久启用" : gate.enabled !== false ? "启用" : "停用"}</em></label></div>
    </div>`).join("") || `<div class="empty">当前标准没有门禁定义</div>`}</div>`;
}

function ruleGovernanceHtml() {
  const governance = standardsDraft?.rules?.rule_governance || {};
  const scopes = governance.scopes || [];
  const precedence = governance.precedence || [];
  if (!scopes.length) return "";
  const scopeLabel = {
    system_permanent: "系统永久规则",
    project_or_episode: "项目 / 本集事实",
    shot_contract: "当前镜头规则",
    retry_patch: "一次性修复规则",
  };
  const scopeNote = {
    system_permanent: "只保留身份、性别、人数、物理、文字、参考图绑定和文件完整性等不可妥协门禁。",
    project_or_episode: "时代、画风、服装、道具、允许的跨时代物件；只作用于本项目或本集。",
    shot_contract: "机位、构图、动作、站位和首尾状态；只作用于当前镜头。",
    retry_patch: "针对本次质检问题修一次；通过或第二次失败后立即失效，不写入永久提示词。",
  };
  return `<article class="skill-manifest">
    <div><span>RULE GOVERNANCE</span><b>规则已分层，禁止临时修复污染永久规则</b></div>
    <dl>${scopes.map((scope) => `<div><dt>${scopeLabel[scope.id] || scope.id}</dt><dd>${esc(scopeNote[scope.id] || scope.expires || "")}</dd></div>`).join("")}</dl>
    <p>冲突优先级：${precedence.map((item) => esc(item.label || item.id)).join(" → ")}。质检观察默认“待审核”，不会自动注入其他镜头。</p>
  </article>`;
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
    ${ruleGovernanceHtml()}
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

/* 停止生成:流水线在当前产线调用后安全停下,落回可调整检查点 */
async function stopEpisode(episodeId) {
  try {
    await api("/api/stop", {
      method: "POST", headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ episode_id: episodeId }),
    });
    showToast("✓ 停止信号已发出:2 秒内终止当前产线调用,落回可调整的检查点", "ok");
    // 立即刷新,马上看到「正在停止…」状态,不等下一轮轮询
    if (location.hash === `#/episode/${episodeId}`)
      setTimeout(() => renderCanvasView(episodeId), 400);
    else if (location.hash === "" || location.hash === "#/")
      setTimeout(refreshIfIdle, 400);
  } catch (e) { showToast(e.message, "error"); }
}

/* 轮询防闪烁:数据没变一个字都不动;变了也只补丁更新变化区块 */
let dashSignature = "";

function dashSig(data) {
  return JSON.stringify([
    data.episodes.map((e) => [e.id, e.status, e.qc_score, e.cost]),
    data.jobs.map((j) => [j.id, j.status]),
    (data.series_batches || []).map((b) => [b.id, b.status, b.completed,
      b.auto_advance, (b.current || {}).episode_id, (b.next || {}).episode_id]),
    data.stats,
  ]);
}

function tilesHtml(data) {
  const s = data.stats;
  const running = data.jobs.filter((j) => j.status === "running").length;
  return `
      <div class="tile"><div class="label">剧集总数</div><div class="value">${s.episodes}</div></div>
      <div class="tile"><div class="label">已完成</div><div class="value">${s.done}<small> / ${s.episodes}</small></div></div>
      <div class="tile"><div class="label">总成本</div><div class="value">${fmt(s.total_cost)}<small>${Number(s.budget || 0) > 0 ? ` 单集预算 ${fmt(s.budget, 0)}` : " 单集不限额"}</small></div></div>
      <div class="tile"><div class="label">平均质检分</div><div class="value">${s.avg_qc == null ? "-" : fmt(s.avg_qc, 1)}</div></div>
      <div class="tile"><div class="label">制作任务</div><div class="value">${running}<small> 进行中</small></div></div>`;
}

function episodesPanelHtml(data) {
  const runningEpisodes = new Set(data.jobs
    .filter((job) => job.status === "running")
    .map((job) => `${job.title}\u0000${job.episode}`));
  return `
      <h2>剧集 · 点击进入分镜画布</h2>
      ${data.episodes.length ? `
      <table><thead><tr><th>项目</th><th>集</th><th>状态</th><th class="num">质检</th><th class="num">成本</th><th>管理</th></tr></thead>
      <tbody>${data.episodes.map((e) => {
        const running = runningEpisodes.has(`${e.project}\u0000${e.number}`);
        return `
        <tr class="clickable" data-ep="${e.id}">
          <td>${esc(e.project)}</td><td>第${e.number}集</td>
          <td>${chip(e.status)}</td>
          <td class="num">${e.qc_score == null ? "-" : fmt(e.qc_score, 0)}</td>
          <td class="num">${fmt(e.cost)}</td>
          <td>${["failed", "qc_failed"].includes(e.status) && !running
            ? `<button class="primary episode-resume" data-episode-id="${e.id}"
                data-title="${esc(e.project)}" data-number="${e.number}"
                title="从上次失败的断点接着做,已完成部分全部保留">▶ 继续</button> ` : ""}
          ${["failed", "qc_failed"].includes(e.status) && !running
            ? `<button class="danger episode-rebuild-all" data-episode-id="${e.id}"
                data-title="${esc(e.project)}" data-number="${e.number}"
                title="推翻原有设定,清理本轮复用并从头重新生成图片、首尾帧和视频">⚠ 全部重新生成</button> ` : ""}
          <button class="danger episode-delete-work" data-episode-id="${e.id}"
            ${running ? "disabled" : ""} title="${running
              ? "本集正在生成，请先安全停止" : "删除本集作品，可选择是否保留资产中心图片"}">删除</button></td>
        </tr>`;
      }).join("")}</tbody></table>`
      : `<div class="empty">暂无剧集,输入一句话开始制作。</div>`}`;
}

function bindEpisodeRows(data) {
  app.querySelectorAll("#episodes-panel tr.clickable").forEach((tr) =>
    tr.addEventListener("click", (event) => {
      if (event.target.closest("button")) return;
      location.hash = `#/episode/${tr.dataset.ep}`;
    }));
  const episodes = new Map(data.episodes.map((episode) =>
    [String(episode.id), episode]));
  app.querySelectorAll("#episodes-panel .episode-resume").forEach((button) =>
    button.addEventListener("click", async (event) => {
      event.stopPropagation();
      button.disabled = true;
      button.textContent = "续跑中…";
      try {
        await api("/api/produce", {
          method: "POST", headers: { "Content-Type": "application/json" },
          body: JSON.stringify({ title: button.dataset.title,
                                 episode: Number(button.dataset.number),
                                 review: true }),
        });
        showToast("已从断点继续:复用已完成部分,只重跑剩余步骤", "ok");
        location.hash = `#/episode/${button.dataset.episodeId}`;
      } catch (e) {
        showToast(e.message, "error");
        button.disabled = false;
        button.textContent = "▶ 继续";
      }
    }));
  app.querySelectorAll("#episodes-panel .episode-rebuild-all").forEach((button) =>
    button.addEventListener("click", (event) => {
      event.stopPropagation();
      armConfirm(button, "全部重新生成", async () => {
        button.disabled = true;
        button.textContent = "已确认,全部重新生成中…";
        try {
          await api("/api/produce", {
            method: "POST", headers: { "Content-Type": "application/json" },
            body: JSON.stringify({ title: button.dataset.title,
              episode: Number(button.dataset.number), force: true, review: true }),
          });
          showToast("已提交全部重新生成:图片、首尾帧、视频将按新设定从头生产", "ok");
          location.hash = `#/episode/${button.dataset.episodeId}`;
        } catch (e) {
          showToast(e.message, "error");
          button.disabled = false;
          button.textContent = "⚠ 全部重新生成";
        }
      });
    }));
  app.querySelectorAll("#episodes-panel .episode-delete-work").forEach((button) =>
    button.addEventListener("click", (event) => {
      event.stopPropagation();
      const episode = episodes.get(button.dataset.episodeId);
      if (episode) showHistoryDeleteDialog({
        episode_id: episode.id,
        project: episode.project,
        episode_number: episode.number,
      }, () => renderDashboard());
    }));
}

function updateTopbar(data) {
  const running = data.jobs.filter((j) => j.status === "running").length;
  const activeStandard = data.production_standard || {};
  topbarRight.innerHTML = `<a class="standard-live" href="#/standards/history">标准 v${esc(activeStandard.version || 1)}</a>` + (running
    ? `<span class="chip running">${running} 个制作任务进行中</span>` : "");
}

/* 局部刷新:只动进度条/数字/剧集表,不重画表单和图片区 → 不闪 */
function updateDashboard(data) {
  watchBuild(data);
  // 左上角显示当前运行版本(git 短哈希):一眼判断自动更新是否生效
  const sub = document.querySelector(".brand-sub");
  if (sub && data.build && !sub.textContent.includes(data.build))
    sub.textContent = `V3.2 · ${data.build} · AI 漫剧工业生产系统`;
  updateTopbar(data);
  renderProgressBanner(data);
  const series = document.getElementById("series-batches");
  if (series) {
    series.innerHTML = seriesBatchesHtml(data);
    bindSeriesBatches();
  }
  const tiles = document.getElementById("tiles");
  if (tiles) tiles.innerHTML = tilesHtml(data);
  const panel = document.getElementById("episodes-panel");
  if (panel) {
    panel.innerHTML = episodesPanelHtml(data);
    bindEpisodeRows(data);
  }
}

const SERIES_ITEM_CN = {
  queued: "等待前一集", active: "当前制作", done: "已完成",
  needs_attention: "需处理",
};

function seriesBatchesHtml(data) {
  const batches = (data.series_batches || []).filter((batch) => batch.status !== "done");
  if (!batches.length) return "";
  return `<section class="series-queues" aria-label="多集串行生产队列">
    ${batches.map((batch) => {
      const pct = Math.round((Number(batch.completed) || 0)
        / Math.max(1, Number(batch.total) || 1) * 100);
      const canNext = !batch.current && batch.next
        && !batch.items.some((item) => item.status === "needs_attention");
      return `<article class="series-queue" data-series-batch="${batch.id}">
        <div class="series-queue-head">
          <div><span class="eyebrow">SERIES QUEUE · #${batch.id}</span>
            <h2>《${esc(batch.project_title)}》· ${esc(batch.filename || "多集剧本")}</h2>
            <p>已完成 ${batch.completed}/${batch.total} 集；整批已入库，但始终只激活一集。</p></div>
          <label class="series-auto"><input type="checkbox" data-series-auto
            ${batch.auto_advance ? "checked" : ""}> 当前集完整通过后自动准备下一集</label>
        </div>
        <div class="series-progress"><i style="width:${pct}%"></i></div>
        <div class="series-episode-strip">${batch.items.map((item) => `
          <a href="#/episode/${item.episode_id}" class="series-episode ${item.status}">
            <b>第${item.episode_number}集</b><span>${esc(item.episode_title || "")}</span>
            <small>${esc(SERIES_ITEM_CN[item.status] || item.status)} · ${item.mode === "script" ? "已有剧本" : "剧情梗概待编剧"}</small>
          </a>`).join("")}</div>
        <div class="series-actions">
          ${batch.current ? `<a class="primary button-link" href="#/episode/${batch.current.episode_id}">继续当前第${batch.current.episode_number}集 →</a>` : ""}
          ${canNext ? `<button class="primary" data-series-next>开始第${batch.next.episode_number}集</button>` : ""}
          ${batch.items.some((item) => item.status === "needs_attention")
            ? `<span class="series-blocked">有剧集失败或质检未通过，修复后才会继续。</span>` : ""}
        </div>
      </article>`;
    }).join("")}
  </section>`;
}

function bindSeriesBatches() {
  document.querySelectorAll("[data-series-batch]").forEach((card) => {
    const batchId = Number(card.dataset.seriesBatch);
    card.querySelector("[data-series-next]")?.addEventListener("click", async (event) => {
      const button = event.currentTarget;
      button.disabled = true; button.textContent = "正在准备…";
      try {
        const reply = await api("/api/series/next", {
          method: "POST", headers: { "Content-Type": "application/json" },
          body: JSON.stringify({ batch_id: batchId }),
        });
        const step = reply.step;
        showToast(step.mode === "script"
          ? `第${step.number}集剧本已进入审阅`
          : `第${step.number}集正在按剧情梗概编剧`, "ok");
        location.hash = `#/episode/${step.episode_id}`;
      } catch (error) {
        showToast(error.message, "error");
        button.disabled = false; button.textContent = "开始下一集";
      }
    });
    card.querySelector("[data-series-auto]")?.addEventListener("change", async (event) => {
      try {
        await api("/api/series/settings", {
          method: "POST", headers: { "Content-Type": "application/json" },
          body: JSON.stringify({ batch_id: batchId,
            auto_advance: event.currentTarget.checked }),
        });
        showToast(event.currentTarget.checked
          ? "当前集通过后会自动准备下一集" : "已改为手动开始下一集", "ok");
      } catch (error) {
        event.currentTarget.checked = !event.currentTarget.checked;
        showToast(error.message, "error");
      }
    });
  });
}

/* ================= 仪表盘 ================= */
async function renderDashboard() {
  topbarRight.innerHTML = "";
  let data;
  try { data = await api("/api/overview"); }
  catch (e) { app.innerHTML = `<div class="loading">加载失败:${esc(e.message)}</div>`; return; }

  dashSignature = dashSig(data);
  const s = data.stats;
  const runningJobs = data.jobs.filter((j) => j.status === "running");
  const activeStandard = data.production_standard || {};
  const firefire = data.firefire || {};
  const approvedFireStyles = (firefire.styles || []).filter((style) =>
    style.status === "approved");
  updateTopbar(data);

  const maxStage = Math.max(1, ...data.cost_by_stage.map((r) => r.total || 0));
  app.innerHTML = `
  <div class="dash">
    <section class="mobile-access-card" aria-label="手机版入口">
      <img src="/static/assets/icon-192.png" alt="" aria-hidden="true">
      <div><b>手机版已就绪</b><span>同一 Wi-Fi 打开 · 可添加到主屏幕 · 审片选图更顺手</span></div>
      <button type="button" data-mobile-access>查看手机网址</button>
    </section>
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
      <input name="premise" placeholder="内容方向或补充要求（可不填，AI 会从剧本自动分析）">
      <label class="style-field">
        <span>视觉制作风格（可选）</span>
        <select name="style" aria-label="视觉风格">
          <option value="" selected>AI 根据剧本自动设计（推荐）</option>
          <option value="${esc(MODERN_OTOME_STYLE)}">现代乙女 · 3D半写实</option>
          <option value="现代都市电影感，写实人物，现代时装，自然皮肤，电影级灯光；禁止古装、汉服、水墨和2D线稿">现代都市 · 电影写实</option>
          <option value="国风漫剧，精致2D动画质感，服装与建筑严格符合剧情时代，高细节，统一人物造型">国风漫剧 · 2D</option>
        </select>
        <small>留空时先分析时代、地域、题材、人物阶层、环境和情绪，再生成本剧专属风格。</small>
      </label>
      <label class="style-field independent-style-field">
        <span>火火漫剧研究室 · 风格包（可选覆盖）</span>
        <select name="style_pack_id" aria-label="火火独立风格">
          <option value="">不使用独立风格</option>
          ${approvedFireStyles.map((style) =>
            `<option value="${esc(style.id)}">${esc(style.name)} · v${esc(style.version)}</option>`).join("")}
        </select>
        <small>只作用于本剧，不改写现有画风和硬规则</small>
      </label>
      <button class="primary" type="submit" data-produce-submit>生成剧本并 AI 分析</button>
      <textarea name="script" rows="7" hidden placeholder="可直接粘贴小说正文或标准剧本。支持人物在对白前/后，也支持独立引号对白。例如：
乾清宫内烛影摇曳。
朱慈烺咬牙道：“父皇，儿臣请战！”
“你可知此去凶险？”崇祯沉声问道。

标准剧本也可：
第1场 古镇长街
夜色渐深,妖气翻涌。
林昭:这股妖气不对劲。
小狐:小心,它就在附近!"></textarea>
      <div class="produce-hint" data-script-intelligence hidden>
        小说智能解析：识别引号对白、说话人、人物和动作；对白逐字保留。无法确定的说话人会在剧本审阅页明确标出，不会静默丢弃。
      </div>
      <div class="series-import-tools" data-series-tools hidden>
        <label class="series-file-picker">
          <span>📚 选择多集剧本文档</span>
          <input type="file" name="series_file"
            accept=".txt,.md,.markdown,.json,.docx,.pdf,text/plain,application/json,application/vnd.openxmlformats-officedocument.wordprocessingml.document,application/pdf">
        </label>
        <label class="series-start-field"><span>起始集</span>
          <input type="number" name="series_start" min="1" step="1"
            placeholder="自动接下一集"></label>
        <label class="series-auto-field"><input type="checkbox" name="series_auto" checked>
          当前集完整通过后，自动准备下一集</label>
        <small data-series-file-note>支持 TXT、Markdown、JSON、Word DOCX、PDF；先预览分集，不会直接开始烧图。</small>
      </div>
      <div class="produce-hint">SK 工业流:上传/AI 编剧 → AI 世界观、环境与风格分析 → 锁定制作圣经 → 连续性圣经 → 五维分镜 → 空间调度图 → 关键帧/文字锁定 → Seedance → 三层质检。</div>
    </form>
    <section class="panel firefire-summary-panel">
      <div class="panel-heading-row"><div><span class="eyebrow">RESEARCH BRAIN</span><h2>火火漫剧研究室</h2></div>
        <button type="button" class="mini-btn" data-firefire-open>打开研究室</button></div>
      <p class="muted">把公开短剧/平台案例拆成可追溯证据，生成验证任务；只有人工确认后，独立风格才会出现在上面的首步选择器。</p>
      <div class="firefire-stats">
        <span><b>${firefire.counts?.sessions || 0}</b>学习会话</span>
        <span><b>${firefire.counts?.evidence || 0}</b>证据</span>
        <span><b>${firefire.counts?.draft_styles || 0}</b>待确认风格</span>
        <span><b>${firefire.counts?.approved_styles || 0}</b>已发布风格</span>
      </div>
      ${approvedFireStyles.length ? `<div class="asset-chips firefire-style-chips">${approvedFireStyles.map((style) => `<span class="chip">${esc(style.name)} · v${esc(style.version)}</span>`).join("")}</div>` : `<div class="empty">还没有人工确认的独立风格</div>`}
    </section>
    <section class="workflow-map" aria-label="AIFOS 漫剧工业流">
      <div class="workflow-lead"><b>不把长剧本直接塞给视频模型</b><span>先锁定画面、人物与段间状态，再让 Seedance 只执行动作、镜头和情绪。</span><a href="#/standards/production">${esc(activeStandard.name || "SK 五维漫剧标准")} · v${esc(activeStandard.version || 1)} 正在驱动新剧集 →</a></div>
      <div class="workflow-steps">
        ${["剧本来源", "AI制作圣经", "连续性", "五维分镜", "空间调度", "文字关键帧", "首尾帧", "生产门禁", "视频/口型", "抽帧+复核", "交付"].map((name, i) =>
          `<div class="workflow-step"><em>${String(i + 1).padStart(2, "0")}</em><span>${name}</span></div>`).join("")}
      </div>
    </section>
    <div id="progress-banner"></div>
    <div id="series-batches">${seriesBatchesHtml(data)}</div>
    <div id="pipeline-strip"></div>

    <div class="tiles" id="tiles">${tilesHtml(data)}</div>

    <div class="panel" id="episodes-panel">${episodesPanelHtml(data)}</div>

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
  bindMobileAccessButtons(app);
  form.addEventListener("submit", onProduce);
  app.querySelector("[data-firefire-open]")?.addEventListener(
    "click", () => openFireFireLab(firefire));
  const syncProduceMode = () => {
    const scriptMode = !form.script.hidden;
    const file = form.elements.series_file.files[0];
    form.querySelector("[data-script-intelligence]").hidden = !scriptMode;
    form.querySelector("[data-produce-submit]").textContent = file
      ? "预览并批量导入"
      : scriptMode ? "智能解析小说 / 剧本并 AI 分析"
        : "生成剧本并 AI 分析";
  };
  form.querySelectorAll(".mode-tab").forEach((tab) =>
    tab.addEventListener("click", () => {
      form.querySelectorAll(".mode-tab").forEach((t) =>
        t.classList.toggle("active", t === tab));
      form.script.hidden = tab.dataset.mode !== "script";
      form.querySelector("[data-series-tools]").hidden = tab.dataset.mode !== "script";
      syncProduceMode();
      if (tab.dataset.mode === "script") form.script.focus();
    }));
  form.querySelectorAll(".kind-tab").forEach((tab) =>
    tab.addEventListener("click", () => {
      form.querySelectorAll(".kind-tab").forEach((t) =>
        t.classList.toggle("active", t === tab));
      form.dataset.kind = tab.dataset.kind;
    }));
  form.elements.series_file.addEventListener("change", () => {
    const file = form.elements.series_file.files[0];
    const note = form.querySelector("[data-series-file-note]");
    note.textContent = file
      ? `已选择 ${file.name} · ${Math.max(1, Math.round(file.size / 1024))}KB；点“预览并批量导入”检查分集。`
      : "支持 TXT、Markdown、JSON、Word DOCX、PDF；先预览分集，不会直接开始烧图。";
    syncProduceMode();
  });
  renderProgressBanner(data);
  bindSeriesBatches();
  bindEpisodeRows(data);
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

  const producing = data.episodes.some((e) =>
    !["done", "failed", "qc_failed", "created", "awaiting_script",
      "awaiting_cast", "awaiting_confirm", "queued_script"].includes(e.status));
  if (runningJobs.length || producing)
    pollTimer = setInterval(refreshIfIdle, 2500);
}

async function refreshIfIdle() {
  if (!(location.hash === "" || location.hash === "#/")) return;
  try {
    const data = await api("/api/overview");
    // 顺带静默刷新日志(纯文本,不引起闪烁)
    api("/api/logs?limit=30").then((rows) => {
      const el = document.getElementById("log-list");
      if (el && rows.length) el.innerHTML = rows.reverse().map((r) =>
        `<div class="lv-${esc(r.level)}">[${esc(r.level)}] ${esc(r.source)}: ${esc(r.message)}</div>`).join("");
    }).catch(() => {});
    const sig = dashSig(data);
    if (sig === dashSignature) return;   // 没变化:整页纹丝不动
    dashSignature = sig;
    updateDashboard(data);
  } catch (e) { /* 网络抖动下一轮再试 */ }
}

function fileBase64(file) {
  return new Promise((resolve, reject) => {
    const reader = new FileReader();
    reader.onload = () => resolve(String(reader.result || "").split(",")[1] || "");
    reader.onerror = () => reject(new Error("读取文档失败"));
    reader.readAsDataURL(file);
  });
}

async function previewSeriesImport(form, file) {
  const dataBase64 = await fileBase64(file);
  const request = {
    sentence: form.sentence.value.trim(),
    start_episode: form.elements.series_start.value || null,
    filename: file.name,
    data_base64: dataBase64,
    style: form.style.value,
    style_pack_id: form.style_pack_id.value,
    kind: form.dataset.kind || "",
    auto_advance: form.elements.series_auto.checked,
    start_first: true,
  };
  const preview = await api("/api/series/preview", {
    method: "POST", headers: { "Content-Type": "application/json" },
    body: JSON.stringify(request),
  });
  const conflicts = preview.conflicts || [];
  const overlay = document.createElement("div");
  overlay.className = "script-overlay series-preview-overlay";
  overlay.innerHTML = `<div class="script-panel series-preview-panel">
    <div class="script-head"><div><span class="eyebrow">DOCUMENT IMPORT</span>
      <h3>导入前确认分集 · 《${esc(preview.project_title)}》</h3></div>
      <button class="close">关闭 Esc</button></div>
    <div class="series-preview-summary">
      <div><b>${preview.total}</b><span>识别剧集</span></div>
      <div><b>第${preview.start_number}集</b><span>开始编号</span></div>
      <div><b>${esc((preview.source_format || "text").toUpperCase())}</b><span>${esc(preview.filename)}</span></div>
    </div>
    <p class="series-preview-rule">整批只负责导入和排队：当前集过完剧本、人物、开拍和质检门禁后，才准备下一集。不会同时批量烧图。</p>
    ${conflicts.length ? `<div class="series-import-error">第 ${conflicts.join("、")} 集已经存在。请关闭后修改“起始集”，现有内容不会被覆盖。</div>` : ""}
    <div class="series-preview-list">${preview.episodes.map((episode) => `
      <article class="series-preview-item ${episode.mode}">
        <div><b>第${episode.episode_number}集 · ${esc(episode.title)}</b>
          <span class="series-mode ${episode.mode}">${episode.mode === "script" ? "已识别完整剧本" : "剧情梗概 · 将由 AI 按集编剧"}</span></div>
        <p>${esc(episode.excerpt || "")}</p>
        <small>${episode.char_count} 字${episode.mode === "script"
          ? ` · ${episode.import_analysis?.dialogue_count || 0} 句对白 · ${episode.scene_count} 场 · 角色 ${episode.characters.map(esc).join("、") || "待识别"}`
          : " · 编剧完成后停在剧本审阅，不会直接出图"}</small>
      </article>`).join("")}</div>
    <div class="series-preview-actions">
      <button class="close-secondary">返回修改</button>
      <button class="primary import-confirm" ${conflicts.length ? "disabled" : ""}>确认导入 ${preview.total} 集并从第${preview.start_number}集开始</button>
    </div>
  </div>`;
  document.body.appendChild(overlay);
  const close = () => {
    overlay.remove();
    document.removeEventListener("keydown", onKey);
  };
  const onKey = (event) => { if (event.key === "Escape") close(); };
  document.addEventListener("keydown", onKey);
  overlay.addEventListener("click", (event) => { if (event.target === overlay) close(); });
  overlay.querySelector(".close").onclick = close;
  overlay.querySelector(".close-secondary").onclick = close;
  overlay.querySelector(".import-confirm")?.addEventListener("click", async (event) => {
    const button = event.currentTarget;
    button.disabled = true; button.textContent = "正在建立逐集队列…";
    try {
      const reply = await api("/api/series/import", {
        method: "POST", headers: { "Content-Type": "application/json" },
        body: JSON.stringify(request),
      });
      close();
      const first = reply.first;
      showToast(`已导入 ${reply.batch.total} 集；仅激活第${first.number}集`, "ok");
      location.hash = `#/episode/${first.episode_id}`;
    } catch (error) {
      showToast(error.message, "error");
      button.disabled = false;
      button.textContent = `确认导入 ${preview.total} 集并开始第一集`;
    }
  });
}

function openFireFireLab(initial) {
  const overlay = document.createElement("div");
  overlay.className = "script-overlay firefire-overlay";
  overlay.innerHTML = `<div class="script-panel firefire-panel">
    <div class="script-head"><div><span class="eyebrow">RESEARCH BRAIN</span><h3>火火漫剧研究室</h3></div><button class="close">关闭 Esc</button></div>
    <p class="logline">学习资料只进入研究空间；证据、分析草稿和验证任务可追溯，独立风格必须人工确认后才能用于新剧。</p>
    <div class="firefire-lab-body"></div>
  </div>`;
  document.body.appendChild(overlay);
  const panel = overlay.querySelector(".firefire-lab-body");
  const close = () => { overlay.remove(); document.removeEventListener("keydown", onKey); };
  const onKey = (event) => { if (event.key === "Escape") close(); };
  document.addEventListener("keydown", onKey);
  overlay.querySelector(".close").onclick = close;
  overlay.addEventListener("click", (event) => { if (event.target === overlay) close(); });

  const render = (data) => {
    const sessions = data.sessions || [];
    const styles = data.styles || [];
    panel.innerHTML = `
      <div class="firefire-grid">
        <form class="firefire-form" data-firefire-session-form>
          <h4>建立学习会话</h4>
          <input name="name" placeholder="案例名称，例如：红果爆款女团夜戏" required>
          <input name="source_url" type="url" placeholder="来源链接（可选，需有使用权）">
          <textarea name="notes" rows="3" placeholder="研究目标、平台、集数或补充说明"></textarea>
          <label class="checkline"><input type="checkbox" name="rights_confirmed"> 我确认有权使用该链接/素材，并会保留来源</label>
          <button class="primary" type="submit">保存学习会话</button>
        </form>
        <form class="firefire-form" data-firefire-evidence-form>
          <h4>补充证据</h4>
          <select name="session_id" required><option value="">选择学习会话</option>${sessions.map((s) => `<option value="${s.id}">${esc(s.name)} · #${s.id}</option>`).join("")}</select>
          <input name="label" placeholder="证据标签，如：女主近景妆造">
          <input name="uri" placeholder="截图/参考图路径或链接">
          <input name="timecode" placeholder="时间码，如 00:01:12">
          <textarea name="observation" rows="3" placeholder="观察到的画风、镜头、服装或节奏事实"></textarea>
          <button type="submit">保存证据</button>
        </form>
        <form class="firefire-form" data-firefire-style-form>
          <h4>建立独立风格草稿</h4>
          <input name="name" placeholder="风格名称，例如：夜色玻璃糖" required>
          <select name="session_id"><option value="">不关联会话</option>${sessions.map((s) => `<option value="${s.id}">${esc(s.name)} · #${s.id}</option>`).join("")}</select>
          <textarea name="summary" rows="2" placeholder="风格摘要与适用剧情"></textarea>
          <textarea name="compiled_style" rows="4" placeholder="可直接执行的完整风格提示词（含时代、人物、服装、灯光、镜头和禁用项）" required></textarea>
          <textarea name="positive_prompt" rows="2" placeholder="正向提示词（可选）"></textarea>
          <textarea name="negative_prompt" rows="2" placeholder="负向提示词（可选）"></textarea>
          <button type="submit">保存草稿，等待人工确认</button>
        </form>
      </div>
      <div class="firefire-records">
        <h4>学习会话与分析状态</h4>
        ${sessions.length ? sessions.map((s) => `<article class="firefire-record"><div><b>#${s.id} ${esc(s.name)}</b><span class="chip">${esc(s.status)}</span></div><small>${esc(s.source_url || s.notes || "未填写来源")}</small>${s.rights_confirmed && s.status !== "complete" ? `<button class="mini-btn firefire-analyse" data-id="${s.id}">建立分析工作单</button>` : `<small>${s.rights_confirmed ? "已完成" : "等待权利确认"}</small>`}</article>`).join("") : `<div class="empty">还没有学习会话</div>`}
        <h4>独立风格包</h4>
        ${styles.length ? styles.map((style) => `<article class="firefire-record"><div><b>${esc(style.name)} · v${esc(style.version)}</b><span class="chip">${esc(style.status)}</span></div><small>${esc(style.summary || style.compiled_style.slice(0, 120))}</small>${style.status === "draft" ? `<button class="mini-btn firefire-publish" data-id="${esc(style.id)}">人工确认并发布</button>` : ""}</article>`).join("") : `<div class="empty">还没有风格草稿</div>`}
      </div>`;
    panel.querySelector("[data-firefire-session-form]").onsubmit = async (event) => {
      event.preventDefault(); const form = event.currentTarget;
      try { await api("/api/firefire/session", { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify({ name: form.name.value, source_url: form.source_url.value, notes: form.notes.value, rights_confirmed: form.rights_confirmed.checked }) }); showToast("学习会话已保存", "ok"); render(await api("/api/firefire")); } catch (error) { showToast(error.message, "error"); }
    };
    panel.querySelector("[data-firefire-evidence-form]").onsubmit = async (event) => {
      event.preventDefault(); const form = event.currentTarget;
      try { await api("/api/firefire/evidence", { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify({ session_id: form.session_id.value, label: form.label.value, uri: form.uri.value, timecode: form.timecode.value, observation: form.observation.value }) }); showToast("证据已保存", "ok"); render(await api("/api/firefire")); } catch (error) { showToast(error.message, "error"); }
    };
    panel.querySelector("[data-firefire-style-form]").onsubmit = async (event) => {
      event.preventDefault(); const form = event.currentTarget;
      try { await api("/api/firefire/style", { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify({ name: form.name.value, session_id: form.session_id.value || null, summary: form.summary.value, compiled_style: form.compiled_style.value, positive_prompt: form.positive_prompt.value, negative_prompt: form.negative_prompt.value }) }); showToast("风格草稿已保存", "ok"); render(await api("/api/firefire")); } catch (error) { showToast(error.message, "error"); }
    };
    panel.querySelectorAll(".firefire-analyse").forEach((button) => button.onclick = async () => {
      try { await api("/api/firefire/analyse", { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify({ session_id: button.dataset.id }) }); showToast("已建立分析工作单，请继续绑定证据", "ok"); render(await api("/api/firefire")); } catch (error) { showToast(error.message, "error"); }
    });
    panel.querySelectorAll(".firefire-publish").forEach((button) => button.onclick = async () => {
      if (!window.confirm("确认已查看验证结果，并把这个风格发布到新剧首步选择器吗？")) return;
      try { await api("/api/firefire/style/publish", { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify({ style_id: button.dataset.id, approved_by: "human" }) }); showToast("独立风格已发布", "ok"); render(await api("/api/firefire")); } catch (error) { showToast(error.message, "error"); }
    });
  };
  render(initial || { sessions: [], styles: [] });
}

async function onProduce(ev) {
  ev.preventDefault();
  const form = ev.target;
  const btn = form.querySelector('button[type="submit"]');
  btn.disabled = true; btn.textContent = "提交中…";
  try {
    const seriesFile = form.elements.series_file?.files?.[0];
    if (!form.script.hidden && seriesFile) {
      btn.textContent = "正在解析文档…";
      await previewSeriesImport(form, seriesFile);
      btn.disabled = false; btn.textContent = "预览并批量导入";
      return;
    }
    const reply = await api("/api/produce", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({
        sentence: form.sentence.value,
        premise: form.premise.value,
        style: form.style.value,
        style_pack_id: form.style_pack_id.value,
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
    btn.disabled = false;
    btn.textContent = form.elements.series_file?.files?.[0]
      ? "预览并批量导入" : !form.script.hidden
        ? "智能解析小说 / 剧本并 AI 分析"
        : "生成剧本并 AI 分析";
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

const IMAGE_COST_GUIDE = [
  { kind: "subscription", name: "Codex 订阅", price: "订阅内",
    use: "重要高清图优先", note: "不增加图片 API 账单" },
  { kind: "batch", name: "Seedream 5.0 Lite", price: "¥0.22/张",
    use: "批量优先", note: "分镜关键帧、场景批量稿" },
  { kind: "standard", name: "GPT Image 2 medium", price: "约 ¥0.28/张",
    use: "常规精修", note: "质量与成本平衡" },
  { kind: "expensive", name: "GPT Image 2 high", price: "高成本",
    use: "核心/高风险图", note: "母资产、文字、群像、近脸、连续性" },
];

function imageCostGuideHtml(compact = false) {
  return `<section class="image-cost-guide${compact ? " compact" : ""}"
    aria-label="图片模型成本与推荐用途">
    <div class="image-cost-guide-head"><b>图片成本护栏</b>
      <span>先按用途选，避免把 high 用在整批候选图</span></div>
    <div class="image-cost-options">${IMAGE_COST_GUIDE.map((item) => `
      <div class="image-cost-option ${item.kind}">
        <strong>${esc(item.name)}</strong><b>${esc(item.price)}</b>
        <span>${esc(item.use)}</span><small>${esc(item.note)}</small>
      </div>`).join("")}</div>
    <div class="image-cost-rule"><b>默认分流</b>
      <span>批量 → Seedream 5.0 Lite</span>
      <span>重要高清 / 母资产 / 文字 / 群像 / 连续性 → Codex 订阅优先</span>
      <span class="danger">低质量图禁止进入 Seedance 正式参考链</span>
    </div>
  </section>`;
}

function qualityPolicyHtml() {
  return `<section class="quality-policy-card">
    <b>默认质量分级 · 按“出错会污染多少后续镜头”判断</b>
    <div><span>低：方向/构图/提示词试错（50%～60%）</span>
      <span>中：普通正式关键帧（30%～40%）</span>
      <span>高：人物母资产、复用场景、近脸情绪、文字、群像、连续性（10%～20%）</span></div>
    <small>自动判级是默认值；重画时可逐张覆盖。只有中、高质量审核通过的图片可交给 Seedance。</small>
  </section>`;
}

function icloudSyncHtml(sync = {}) {
  const states = {
    disabled: ["", "未启用"], unavailable: ["qc_failed", "iCloud 不可用"],
    partial: ["qc_failed", "部分失败"], ready: ["done", "同步正常"],
  };
  const state = states[sync.state] || ["", sync.state || "未知"];
  return `<section class="panel icloud-sync-card">
    <div class="icloud-sync-head">
      <div><h2>☁️ iCloud 图片同步</h2>
        <p>手机“文件”App → iCloud Drive → AIFOS；所有图片直接放在同一个文件夹。</p></div>
      <span class="chip ${state[0]}">${esc(state[1])}</span>
    </div>
    <code>${esc(sync.display_path || "~/Library/Mobile Documents/com~apple~CloudDocs/AIFOS")}</code>
    <div class="icloud-sync-stats">
      <span>已复制到 iCloud Drive <b>${Number(sync.synced || 0)}</b> 张</span>
      <span>失败 <b>${Number(sync.failed || 0)}</b> 张</span>
    </div>
    ${sync.last_error ? `<div class="miss">最近错误:${esc(sync.last_error)}</div>` : ""}
    <div class="pc-actions">
      <button class="primary" id="btn-icloud-toggle">${sync.enabled ? "停用同步" : "启用同步"}</button>
      <button id="btn-icloud-backfill" ${sync.enabled && sync.available ? "" : "disabled"}>补同步现有图片</button>
    </div>
    <small>已登记图片以只读镜像复制；项目素材标 E000，剧集图片标 E001…；修改或删除 iCloud 副本不会回写本地生产工作区。</small>
  </section>`;
}

function providerCostHint(provider) {
  const id = `${provider.name || ""} ${provider.type || ""} ${provider.model || ""}`.toLowerCase();
  if (id.includes("seedream"))
    return `<div class="pc-cost batch"><b>¥0.22/张</b> · Seedream 5.0 Lite 批量优先</div>`;
  if (provider.name === "codex")
    return `<div class="pc-cost subscription"><b>Codex 订阅内</b> · 重要高清图优先</div>`;
  if (provider.name === "image_api" || id.includes("gpt-image-2"))
    return `<div class="pc-cost standard"><b>medium 约 ¥0.28/张</b> · high 高成本，仅终稿/复杂文字</div>`;
  return "";
}

function codexProfileList(data) {
  const raw = Array.isArray(data?.codex_profiles) ? data.codex_profiles : [];
  const channels = [
    { channel: "A", defaultName: "Codex 通道 A" },
    { channel: "B", defaultName: "Codex 通道 B" },
    { channel: "C", defaultName: "Codex 通道 C" },
  ];
  return channels.map(({ channel, defaultName }, index) => {
    const exact = raw.find((item) =>
      String(item?.id || "").trim().toUpperCase() === channel ||
      String(item?.id || "").trim().toUpperCase().endsWith(`_${channel}`) ||
      String(item?.id || "").trim().toUpperCase().endsWith(`-${channel}`));
    const profile = exact || raw[index] || {};
    return {
      id: String(profile.id || channel),
      channel,
      name: profile.name || defaultName,
      enabled: Boolean(profile.enabled),
      codex_home: profile.codex_home || "",
      command: profile.command || "",
      status: String(profile.status || (profile.enabled ? "unknown" : "disabled")),
      reason: profile.reason || "",
      assigned: profile.assigned ?? 0,
      active_jobs: profile.active_jobs ?? [],
    };
  });
}

function codexCount(value) {
  if (Array.isArray(value)) return value.length;
  if (value && typeof value === "object")
    return Number(value.count ?? value.total ?? Object.keys(value).length) || 0;
  return Math.max(0, Number(value) || 0);
}

function codexHealth(profile) {
  const status = String(profile.status || "").toLowerCase();
  if (!profile.enabled) return { tone: "", label: "未启用" };
  if (["ready", "healthy", "ok", "idle", "done"].includes(status))
    return { tone: "done", label: "健康" };
  if (["running", "busy", "processing", "active"].includes(status))
    return { tone: "done", label: "运行中" };
  if (["error", "failed", "unhealthy", "offline", "missing"].includes(status))
    return { tone: "qc_failed", label: "异常" };
  return { tone: "", label: "待检测" };
}

function codexProfileCard(profile) {
  const health = codexHealth(profile);
  return `<article class="codex-channel-card" data-codex-profile="${esc(profile.id)}">
    <div class="codex-channel-head">
      <div><span class="codex-channel-badge">通道 ${esc(profile.channel)}</span>
        <strong>${esc(profile.name)}</strong></div>
      <span class="chip ${health.tone}">${health.label}</span>
    </div>
    <label class="set-row codex-set-row"><span>名称</span>
      <input data-codex-field="name" value="${esc(profile.name)}"
        placeholder="例如：Codex 图片通道 ${esc(profile.channel)}"></label>
    <label class="set-row codex-set-row"><span>CODEX_HOME / 配置路径</span>
      <input data-codex-field="codex_home" value="${esc(profile.codex_home)}"
        placeholder="例如：/Users/name/.codex-${esc(profile.channel.toLowerCase())}"></label>
    <label class="set-row codex-set-row"><span>Codex 命令</span>
      <input data-codex-field="command" value="${esc(profile.command)}"
        placeholder="留空使用系统 codex"></label>
    <label class="set-row toggle codex-toggle"><span>启用</span>
      <input type="checkbox" data-codex-field="enabled" ${profile.enabled ? "checked" : ""}>
      <em>${profile.enabled ? "参与图片任务并行分片" : "暂不分配图片任务"}</em></label>
    <div class="codex-channel-runtime">
      <span>已分配 <b>${codexCount(profile.assigned)}</b></span>
      <span>运行中 <b>${codexCount(profile.active_jobs)}/${
        Number(profile.parallel_limit) || 8}</b></span>
      <span>状态 <b>${health.label}</b></span>
    </div>
    ${profile.reason ? `<p class="${health.tone === "qc_failed" ? "codex-channel-error" : "dim"}">
      ${esc(profile.reason)}</p>` : ""}
  </article>`;
}

function codexJobList(value) {
  if (Array.isArray(value)) return value;
  if (value && Array.isArray(value.items)) return value.items;
  if (value && Array.isArray(value.jobs)) return value.jobs;
  return [];
}

function codexShardRows(data, shardPayload = null) {
  const profiles = codexProfileList(data);
  const raw = Array.isArray(shardPayload) ? shardPayload
    : shardPayload?.shards || shardPayload?.channels ||
      shardPayload?.items || shardPayload?.codex_profiles || [];
  const shards = Array.isArray(raw) ? raw : [];
  return profiles.map((profile, index) => {
    const extra = shards.find((item) =>
      [profile.id, profile.channel].includes(
        String(item?.id || item?.channel || item?.profile_id || "").trim())
      || String(item?.id || item?.channel || item?.profile_id || "")
        .trim().toUpperCase() === profile.channel) || shards[index] || {};
    const activeValue = extra.active_jobs ?? extra.running ?? profile.active_jobs;
    const jobs = codexJobList(activeValue);
    const assigned = codexCount(extra.assigned ?? extra.total ?? profile.assigned);
    const running = extra.running_count != null
      ? codexCount(extra.running_count)
      : jobs.length
        ? jobs.filter((job) =>
          ["running", "processing", "active", "retrying"].includes(
            String(job?.status || "").toLowerCase())).length
        : codexCount(activeValue);
    const completed = extra.completed != null
      ? codexCount(extra.completed)
      : jobs.filter((job) =>
        ["done", "completed", "success"].includes(
          String(job?.status || "").toLowerCase())).length;
    const failed = extra.failed != null
      ? codexCount(extra.failed)
      : jobs.filter((job) =>
        ["failed", "error"].includes(String(job?.status || "").toLowerCase())).length;
    let progress = Number(extra.progress);
    if (Number.isFinite(progress) && progress >= 0 && progress <= 1) progress *= 100;
    if (!Number.isFinite(progress))
      progress = assigned > 0 && (completed || failed)
        ? ((completed + failed) / assigned) * 100 : 0;
    const failedJob = jobs.find((job) => job?.error || job?.reason);
    const status = String(profile.status || "").toLowerCase();
    const error = extra.error || extra.last_error || failedJob?.error ||
      failedJob?.reason ||
      (["error", "failed", "unhealthy"].includes(status) ? profile.reason : "");
    return {
      id: profile.id, channel: profile.channel,
      name: profile.name, enabled: profile.enabled,
      assigned, running, completed, failed,
      progress: Math.max(0, Math.min(100, progress)),
      error,
    };
  });
}

function codexShardBoardHtml(data, shardPayload = null, optionalApiMissing = false) {
  const rows = codexShardRows(data, shardPayload);
  return `<div class="codex-shard-head">
      <div><h3>图片并行生产 · 任务分片</h3>
        <p>图片任务会按通道健康状态分配；单个通道报错时可由另一通道继续。</p></div>
      <button type="button" id="btn-codex-shards-refresh">刷新任务</button>
    </div>
    <div class="codex-shard-grid">
      ${rows.map((row) => `<article class="codex-shard-card">
        <div class="codex-shard-title">
          <span class="codex-channel-badge">通道 ${esc(row.channel)}</span>
          <strong>${esc(row.name)}</strong>
          <span class="chip ${row.failed || row.error ? "qc_failed" : row.enabled ? "done" : ""}">
            ${row.failed || row.error ? "有错误" : row.enabled ? "可分配" : "未启用"}</span>
        </div>
        <div class="codex-shard-stats">
          <span><b>${row.assigned}</b> 分配数</span>
          <span><b>${row.completed}</b> 已完成</span>
          <span><b>${row.running}</b> 进行中</span>
          <span><b>${row.failed}</b> 错误</span>
        </div>
        <div class="codex-progress" role="progressbar" aria-label="通道 ${esc(row.channel)} 图片任务进度"
          aria-valuemin="0" aria-valuemax="100" aria-valuenow="${Math.round(row.progress)}">
          <i style="width:${row.progress.toFixed(1)}%"></i></div>
        <small>${row.assigned
          ? `进度 ${Math.round(row.progress)}%`
          : "暂无已分配任务"}</small>
        ${row.error ? `<p class="codex-channel-error">${esc(row.error)}</p>` : ""}
      </article>`).join("")}
    </div>
    <p class="codex-shard-note ${optionalApiMissing ? "compat" : ""}">
      ${optionalApiMissing
        ? "实时分片接口待接入，当前按 /api/settings 的任务快照展示。"
        : "状态来自实时分片接口；刷新不会中断正在运行的任务。"}
    </p>`;
}

function codexExecutionSettingsHtml(data) {
  const profiles = codexProfileList(data);
  const supported = Array.isArray(data?.codex_profiles);
  const parallelValue = data?.codex_parallel;
  const enabledCount = Number(parallelValue?.enabled_count) ||
    profiles.filter((profile) => profile.enabled).length;
  const readyCount = profiles.filter((profile) => profile.enabled &&
    ["ready", "healthy", "ok", "idle"].includes(
      String(profile.status || "").toLowerCase())).length;
  const enabledProfiles = profiles.filter((profile) => profile.enabled);
  const statusUnknown = enabledProfiles.length > 0 && enabledProfiles.every(
    (profile) => !profile.status || profile.status === "unknown");
  const activeChannelCount = readyCount ||
    (statusUnknown ? enabledCount : 0);
  const parallel = activeChannelCount >= 2;
  const modeLabel = parallel && activeChannelCount === enabledCount
    ? `${activeChannelCount} 通道并行已启用`
    : activeChannelCount > 0
      ? `${activeChannelCount}/${enabledCount} 通道就绪`
    : enabledCount >= 2 ? `${enabledCount} 通道已配置，先修复异常通道`
      : enabledCount === 1 ? "单通道兼容模式" : "等待配置 Codex 通道";
  const perChannelCapacity = Math.max(
    1, Number(data?.defaults?.parallel_images) || 8);
  const totalCapacity = Math.max(0, activeChannelCount) * perChannelCapacity;
  return `<section class="panel codex-execution-panel">
    <div class="codex-execution-head">
      <div><span class="eyebrow">PARALLEL CODEX EXECUTION</span>
        <h2>Codex 多通道</h2>
        <p>通道 A/B/C 使用独立 CODEX_HOME；每通道当前 ${perChannelCapacity} 路，
          已就绪 ${activeChannelCount}/${enabledCount} 条，当前总容量 ${totalCapacity} 路。</p></div>
      <span class="chip ${parallel ? "done" : ""}">${modeLabel}</span>
    </div>
    <div class="codex-channel-grid">${profiles.map(codexProfileCard).join("")}</div>
    <div class="codex-profile-actions">
      <button type="button" class="primary" id="btn-codex-profiles-save">保存通道配置</button>
      <span id="codex-profile-save-status" class="dim">${supported
        ? "保存后新图片任务生效，运行中的任务不迁移。"
        : "接口待接入：当前服务未返回 codex_profiles；原 AI 设置仍可正常使用。"}</span>
    </div>
    <div class="codex-shard-board" id="codex-shard-board">
      ${codexShardBoardHtml(data, null, true)}
    </div>
  </section>`;
}

function bindCodexShardRefresh(data) {
  const button = document.getElementById("btn-codex-shards-refresh");
  if (!button) return;
  button.onclick = async () => {
    button.disabled = true;
    button.textContent = "刷新中…";
    const host = document.getElementById("codex-shard-board");
    try {
      const payload = await api("/api/image-production/shards");
      if (!host?.isConnected) return;
      host.innerHTML = codexShardBoardHtml(data, payload, false);
    } catch (_) {
      if (!host?.isConnected) return;
      host.innerHTML = codexShardBoardHtml(data, null, true);
    }
    bindCodexShardRefresh(data);
  };
}

async function hydrateCodexShardBoard(data) {
  const host = document.getElementById("codex-shard-board");
  if (!host) return;
  try {
    const payload = await api("/api/image-production/shards");
    if (!host.isConnected) return;
    host.innerHTML = codexShardBoardHtml(data, payload, false);
  } catch (_) {
    // 可选接口在旧服务上可能不存在；保留 /api/settings 快照，不弹错误打断设置。
  }
  bindCodexShardRefresh(data);
}

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
    ${imageCostGuideHtml()}
    ${icloudSyncHtml(data.icloud_sync)}
    ${codexExecutionSettingsHtml(data)}
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
  document.getElementById("btn-codex-profiles-save").onclick = async (ev) => {
    const button = ev.currentTarget;
    const status = document.getElementById("codex-profile-save-status");
    const profiles = [...app.querySelectorAll("[data-codex-profile]")].map((card) => {
      const field = (name) => card.querySelector(`[data-codex-field="${name}"]`);
      return {
        id: card.dataset.codexProfile,
        name: field("name").value.trim(),
        enabled: field("enabled").checked,
        codex_home: field("codex_home").value.trim(),
        command: field("command").value.trim(),
      };
    });
    button.disabled = true;
    button.textContent = "保存中…";
    if (status) status.textContent = "正在写入通道配置…";
    try {
      const fresh = await post({ codex_profiles: profiles });
      showToast(`Codex ${profiles.filter((profile) => profile.enabled).length} 通道配置已保存`, "ok");
      drawSettings(fresh);
    } catch (e) {
      if (status) {
        status.className = "codex-channel-error";
        status.textContent = `暂未保存：${e.message}（原 AI 设置不受影响）`;
      }
      showToast(`Codex 通道配置暂不可用：${e.message}`, "error");
      button.disabled = false;
      button.textContent = "保存通道配置";
    }
  };
  bindCodexShardRefresh(data);
  void hydrateCodexShardBoard(data);
  document.getElementById("btn-icloud-toggle").onclick = async () => {
    const current = Boolean((data.icloud_sync || {}).enabled);
    try {
      const fresh = await post({ icloud_sync: { enabled: !current } });
      showToast(!current ? "已启用 iCloud 图片同步" : "已停用 iCloud 图片同步", "ok");
      drawSettings(fresh);
    } catch (e) { showToast(e.message, "error"); }
  };
  document.getElementById("btn-icloud-backfill").onclick = async (ev) => {
    const btn = ev.currentTarget;
    btn.disabled = true; btn.textContent = "正在补同步…";
    try {
      const report = await api("/api/icloud-sync/backfill", { method: "POST" });
      showToast(`补同步完成:新增 ${report.synced} 张，失败 ${report.failed} 张`,
        report.failed ? "error" : "ok");
      const fresh = await api("/api/settings");
      drawSettings(fresh);
    } catch (e) {
      showToast(e.message, "error");
      btn.disabled = false; btn.textContent = "补同步现有图片";
    }
  };
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
        // 测通了但开关还没开 → 顺手替你打开,免得卡在"未启用"
        if (r.ok && r.disabled) {
          const fresh = await post({ provider: name, fields: { enabled: true } });
          showToast("✓ 测试通过,已自动打开「启用」,该产线已就绪", "ok");
          drawSettings(fresh);
          return;
        }
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
  const isApi = ["api", "claude_api", "image_api", "seedream_image", "seedream",
    "seedream_lite", "seedream5_lite", "ark_video", "doubao_tts"].includes(p.type)
    || ["seedream5_lite", "seedream_lite", "seedream"].includes(p.name);
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
    ${providerCostHint(p)}
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

function corePropsHtml(script) {
  const props = script.core_props || [];
  if (!props.length) return "";
  return `<section class="story-bible-section core-props">
    <h3>🧰 核心道具母资产 · 每件4选1</h3>
    <div class="script-character-profiles">${props.map((prop) => `
      <article class="character-profile">
        <h4>${esc(prop.name)} <span class="chip">4张候选</span></h4>
        ${prop.story_function ? `<p><b>剧情功能：</b>${esc(prop.story_function)}</p>` : ""}
        ${prop.visual_design ? `<p><b>视觉结构：</b>${esc(prop.visual_design)}</p>` : ""}
        ${prop.era_material ? `<p><b>时代材质：</b>${esc(prop.era_material)}</p>` : ""}
        ${prop.owner ? `<p><b>持有人：</b>${esc(prop.owner)}</p>` : ""}
        ${prop.continuity_states ? `<p><b>连续性：</b>${esc(prop.continuity_states)}</p>` : ""}
      </article>`).join("")}</div>
  </section>`;
}

async function pollJob(jobId, onDone, onProgress) {
  let timer = null;
  let finished = false;
  const tick = async () => {
    try {
      const job = await api(`/api/jobs/${jobId}`);
      if (onProgress) onProgress(job);
      if (job.status !== "running") {
        finished = true;
        if (timer) clearInterval(timer);
        if (job.status === "failed") showToast(job.error || "任务失败", "error");
        onDone(job);
      }
    } catch (e) {
      finished = true;
      if (timer) clearInterval(timer);
    }
  };
  await tick();
  if (!finished) timer = setInterval(tick, 1200);
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
      ${storyBibleHtml(script)}
      <div class="cast">${(script.characters || []).map((c) =>
        `<span class="chip">${esc(c.name)} · ${esc(c.role || "")}</span>`).join("")}</div>
      <div class="script-character-profiles">${(script.characters || [])
        .map(characterProfileHtml).join("")}</div>
      ${corePropsHtml(script)}
      ${script.scenes.map((s) => `
        <section class="scene">
          <div class="scene-head"><span class="scene-no">第 ${s.scene_no} 场</span>
            <span class="scene-loc">${esc(s.location)}</span></div>
          ${s.action ? `<p class="action">△ ${esc(s.action)}</p>` : ""}
          ${(s.lines || []).map((l) => `
            <div class="line-block">
              <div class="speaker">${esc(l.character)}</div>
              <div class="speech">${esc(l.dialogue)}
                ${l.performance ? `<small class="performance-cue">表演：${esc(l.performance)}</small>` : ""}
              </div>
            </div>`).join("")}
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
      pollCanvas(episodeId);
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
      pollCanvas(episodeId);
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
function qualitySelectHtml(cls = "quality-select", selected = "auto") {
  const values = [
    ["auto", "自动（按错误影响范围）"], ["low", "低 · 仅试错"],
    ["medium", "中 · 普通正式图"], ["high", "高 · 核心/高风险"],
  ];
  return `<select class="${cls}" title="可覆盖系统推荐质量">${values.map(([v, label]) =>
    `<option value="${v}" ${selected === v ? "selected" : ""}>${label}</option>`).join("")}</select>`;
}

function regenControls(target, label) {
  return `<div class="regen-box" data-target="${esc(JSON.stringify(target))}">
    <button class="regen-toggle">🔄 ${esc(label)}</button>
    <div class="regen-form" hidden>
      <input placeholder="修改意见,如:换成夜晚/表情更凶(可留空)">
      ${qualitySelectHtml("regen-quality")}
      <button class="regen-ref" title="上传参考图并自动挂到本对象,点重画立即生效">📎 参考图</button>
      <button class="primary regen-go">重画</button>
    </div></div>`;
}

/* 重画对象 → 参考图关联名(镜头类挂全项目) */
function refAttachOf(target) {
  if (target.kind === "character_art" || target.kind === "scene_art")
    return target.name;
  if (target.kind === "character_sheet")
    return String(target.name).split(":")[0];
  return "";
}

function uploadRegenReference(episodeId, target, btn) {
  const input = document.createElement("input");
  input.type = "file";
  input.accept = "image/*";
  input.onchange = () => {
    const file = input.files[0];
    if (!file) return;
    const reader = new FileReader();
    reader.onload = async () => {
      try {
        if (btn) btn.disabled = true;
        await api("/api/reference/upload", {
          method: "POST", headers: { "Content-Type": "application/json" },
          body: JSON.stringify({
            episode_id: episodeId,
            name: file.name.replace(/\.[^.]+$/, ""),
            attach_to: refAttachOf(target),
            filename: file.name,
            data_base64: String(reader.result).split(",")[1] || "",
          }),
        });
        const attach = refAttachOf(target);
        showToast(`参考图已挂到「${attach || "全项目"}」,点重画立即参考它`, "ok");
      } catch (e) { showToast(e.message, "error"); }
      if (btn) btn.disabled = false;
    };
    reader.readAsDataURL(file);
  };
  input.click();
}

function bindRegen(container, episodeId, getData, onDone) {
  container.querySelectorAll(".regen-box").forEach((box) => {
    const form = box.querySelector(".regen-form");
    box.querySelector(".regen-toggle").onclick = () => {
      form.hidden = !form.hidden;
      if (!form.hidden) form.querySelector("input").focus();
    };
    const refBtn = box.querySelector(".regen-ref");
    if (refBtn) refBtn.onclick = () => uploadRegenReference(
      episodeId, JSON.parse(box.dataset.target), refBtn);
    box.querySelector(".regen-go").onclick = async () => {
      const target = JSON.parse(box.dataset.target);
      const feedback = form.querySelector("input").value.trim();
      const quality = form.querySelector(".regen-quality").value;
      const btn = box.querySelector(".regen-go");
      btn.disabled = true; btn.textContent = "重画中…";
      try {
        const reply = await api("/api/regen_image", {
          method: "POST", headers: { "Content-Type": "application/json" },
          body: JSON.stringify({ episode_id: episodeId, target, feedback, quality }),
        });
        pollJob(reply.job_id, (job) => {
          if (job.status === "done") {
            showToast("已重画完成", "ok");
            if (onDone) onDone(); else renderCanvasView(episodeId);
          } else {
            showToast("重画失败:" + (job.error || "未知错误"), "error");
            btn.disabled = false; btn.textContent = "重画";
          }
        });
      } catch (e) {
        showToast(e.message, "error");
        btn.disabled = false; btn.textContent = "重画";
      }
    };
  });
}

/* 分镜生产表内直接修图：不再返回图片清单或画布侧栏找镜头。 */
function shotInlineRevisionHtml(shotNo, hasImage, productionActive = false) {
  if (!hasImage) return `<small class="shot-inline-revision-note">关键帧生成后可在此直接修改</small>`;
  const target = { kind: "shot", shot_no: Number(shotNo) };
  return `<details class="shot-inline-revision"
    data-target="${esc(JSON.stringify(target))}"
    data-production-active="${productionActive ? "1" : "0"}">
    <summary class="shot-revision-toggle">✏️ 直接修改此图</summary>
    <div class="shot-revision-form">
      <textarea rows="3" class="shot-revision-feedback"
        placeholder="必填：指出错误和正确画面，例如“程沐应为女性，脸和发型严格参考已锁定立绘；保持当前机位”"></textarea>
      ${qualitySelectHtml("shot-revision-quality")}
      <div class="shot-revision-actions">
        <button type="button" class="shot-revision-ref">📎 添加参考图</button>
        <button type="button" class="shot-revision-upload">⬆ 上传修好图片</button>
        <button type="button" class="primary shot-revision-go">${
          productionActive ? "暂停并修改" : "修改并同步后续"}</button>
      </div>
      <small>新图会自动同步本镜及同场后续首尾帧、Seedance 手选参考；
        旧镜头视频、旧成片和旧质检自动失效，历史版本仍保留。</small>
    </div>
  </details>`;
}

function frameInlineRevisionHtml(
  shotNo, kind, hasImage, productionActive = false) {
  const isFirst = kind === "first_frame";
  const label = isFirst ? "首帧" : "尾帧";
  if (!hasImage) {
    return `<small class="shot-inline-revision-note">${label}生成后可在此直接修改</small>`;
  }
  const target = { kind, shot_no: Number(shotNo) };
  const boundary = isFirst
    ? "同场上一镜的尾帧"
    : "同场下一镜的首帧";
  return `<details class="shot-inline-revision frame-inline-revision"
    data-target="${esc(JSON.stringify(target))}"
    data-production-active="${productionActive ? "1" : "0"}">
    <summary class="shot-revision-toggle">✏️ 修改${label}</summary>
    <div class="shot-revision-form">
      <textarea rows="3" class="shot-revision-feedback"
        placeholder="必填：只写这张${label}哪里错、要改成什么；未提及部分会保持不变"></textarea>
      ${qualitySelectHtml("shot-revision-quality")}
      <div class="shot-revision-actions">
        <button type="button" class="shot-revision-ref">📎 添加参考图</button>
        <button type="button" class="shot-revision-upload">⬆ 上传修好图片</button>
        <button type="button" class="primary shot-revision-go">${
          productionActive ? "暂停并修改" : `修改${label}并同步`}</button>
      </div>
      <small>修改后自动同步${boundary}、Seedance 手选引用及所有使用旧图的位置；
        受影响视频、旧成片和旧质检自动失效。</small>
    </div>
  </details>`;
}

async function waitForShotRevisionCheckpoint(
  episodeId, projectTitle, episodeNumber, timeoutMs = 120000) {
  const deadline = Date.now() + timeoutMs;
  const stable = new Set(["awaiting_script", "awaiting_cast", "awaiting_confirm",
    "done", "failed", "qc_failed", "created"]);
  while (Date.now() < deadline) {
    const [current, jobs] = await Promise.all([
      api(`/api/episode/${episodeId}`), api("/api/jobs"),
    ]);
    const busy = jobs.some((job) => job.status === "running"
      && job.title === projectTitle
      && Number(job.episode) === Number(episodeNumber));
    if (stable.has(current.episode.status) && !busy) return current;
    await new Promise((resolve) => setTimeout(resolve, 1000));
  }
  throw new Error("暂停尚未完成，请稍后在本镜继续修改");
}

function bindShotInlineRevisions(root, data) {
  root.querySelectorAll(".shot-inline-revision").forEach((box) => {
    const target = JSON.parse(box.dataset.target);
    const targetLabel = target.kind === "first_frame" ? "首帧"
      : target.kind === "last_frame" ? "尾帧" : "参考分镜";
    const form = box.querySelector(".shot-revision-form");
    const refBtn = box.querySelector(".shot-revision-ref");
    refBtn.onclick = (event) => {
      event.stopPropagation();
      uploadRegenReference(data.episode.id, target, refBtn);
    };
    box.querySelector(".shot-revision-upload").onclick = (event) => {
      event.stopPropagation();
      if (box.dataset.productionActive === "1") {
        showToast("正在生产时请使用“暂停并修改”，或先暂停后上传修好图片", "error");
        return;
      }
      uploadFile(data.episode.id, target, "image/*",
        () => renderCanvasView(data.episode.id));
    };
    const go = box.querySelector(".shot-revision-go");
    go.onclick = async (event) => {
      event.stopPropagation();
      const feedback = form.querySelector(".shot-revision-feedback").value.trim();
      if (!feedback) {
        showToast(`请先写清楚这张${targetLabel}哪里错、要改成什么`, "error");
        form.querySelector("textarea").focus();
        return;
      }
      const original = go.textContent;
      go.disabled = true;
      try {
        if (box.dataset.productionActive === "1") {
          go.textContent = "正在安全暂停…";
          try {
            await api("/api/stop", {
              method: "POST", headers: { "Content-Type": "application/json" },
              body: JSON.stringify({ episode_id: data.episode.id }),
            });
          } catch (error) {
            // 任务可能恰好在点击时结束；以随后读取到的稳定状态为准。
          }
          await waitForShotRevisionCheckpoint(
            data.episode.id, data.project.title, data.episode.number);
        }
        go.textContent = "正在重画并同步…";
        const reply = await api("/api/regen_image", {
          method: "POST", headers: { "Content-Type": "application/json" },
          body: JSON.stringify({
            episode_id: data.episode.id, target, feedback,
            quality: form.querySelector(".shot-revision-quality").value,
          }),
        });
        pollJob(reply.job_id, (job) => {
          if (job.status === "done") {
            const sync = (job.summary || {}).sync || {};
            const frameCount = (sync.frame_shots || []).length;
            const videoCount = (sync.invalidated_video_shots || []).length;
            const boundary = (sync.boundary_sync || [])
              .map((item) => item.label).filter(Boolean);
            const boundaryText = boundary.length
              ? `；已同步${boundary.join("、")}` : "";
            showToast(`镜头${target.shot_no}${targetLabel}已修改`
              + `${boundaryText}；共更新${frameCount}镜，`
              + `标记${videoCount}个视频待重拍`, "ok");
            renderCanvasView(data.episode.id);
          } else {
            showToast("修改失败：" + (job.error || "未知错误"), "error");
            go.disabled = false; go.textContent = original;
          }
        });
      } catch (error) {
        showToast(error.message, "error");
        go.disabled = false; go.textContent = original;
      }
    };
  });
}

/* ================= 图片生产清单 =================
   每张要生成的图:分类/名称/提示词/实时状态;可单张改提示词重画 */
const PLAN_CAT_CN = {
  character_candidate: "人物定妆候选(按角色重要度)",
  character_art: "人物立绘",
  character_sheet: "人物资产套件(三视图审核板/独立正侧背母资产/特写/服装)",
  scene_art: "场景概念图",
  shot_image: "分镜画面(关键帧)", frames: "首尾帧",
};
const PLAN_CATS = ["character_candidate", "character_art", "character_sheet", "scene_art",
  "shot_image", "frames"];
const PLAN_STATUS_CN = {
  pending: "排队中", generating: "生成中", done: "已完成",
  failed: "失败", reused: "复用已有", selected: "已选定",
  awaiting_human: "二次质检未过 · 待人工修改",
};
const PLAN_QC_CATS = new Set(["shot_image", "frames"]);
function planQcEnabled(item) { return PLAN_QC_CATS.has(item.category); }
const PLAN_REWORK_STATUSES = new Set([
  "done", "reused", "awaiting_human", "failed", "generating",
]);
function planNeedsRevision(item) {
  return planQcEnabled(item)
    && (item.qc || {}).passed === false
    && PLAN_REWORK_STATUSES.has(item.status || "pending");
}

/* 图片清单的筛选状态按集保存；批量修改只看当前需要返工的图片。 */
const planFailedOnlyByEpisode = new Map();
const planOverlaySignatures = new Map();
function planFailedOnly(episodeId) {
  return planFailedOnlyByEpisode.get(String(episodeId)) === true;
}
function planShowFailedOnly(episodeId) {
  planFailedOnlyByEpisode.set(String(episodeId), true);
  refreshOpenPlanOverlay(episodeId, true);
}
function planShowAll(episodeId) {
  planFailedOnlyByEpisode.delete(String(episodeId));
  refreshOpenPlanOverlay(episodeId, true);
}
const PROVIDER_LABEL = {
  codex: "Codex 订阅", image_api: "GPT Image 2 API", api: "通用API",
  seedream5_lite: "Seedream 5.0 Lite", seedream_lite: "Seedream 5.0 Lite",
  seedream: "Seedream 5.0 Lite",
  claude: "Claude CLI", claude_api: "Claude API", jimeng: "即梦CLI",
  ark: "火山Ark", mock: "占位产线",
};

/* 占位图判定:mock 产线画的是灰底示意图,不是真实 AI 图 */
function planIsMock(item) {
  return item.status === "done" && item.real === false;
}

function planQcBadge(item) {
  const qc = item.qc;
  if (!qc) return "";
  if (qc.passed && qc.manual_override)
    return `<span class="plan-st st-manual" title="人工确认通过，原质检问题仍保留在审计记录">人工通过✓</span>`;
  if (qc.passed)
    return `<span class="plan-st st-qc-ok" title="视觉质检通过${qc.attempts > 1 ? `(重画 ${qc.attempts - 1} 次后通过)` : ""}">质检✓</span>`;
  return `<span class="plan-st st-mock" title="${esc((qc.issues || []).join(";"))}">⚠ 质检未过</span>`;
}

function planQcIssuesHtml(item) {
  const qc = item.qc;
  if (!qc) return "";
  if (qc.passed && qc.manual_override) {
    const issues = qc.manual_original_issues || qc.issues || [];
    return `<div class="qc-manual-pass"><b>人工通过：</b>${esc(qc.manual_note || "问题不影响本集观感，继续后续生产")}
      ${issues.length ? `<span>原质检提示（已接受）：${esc(issues.join("；"))}</span>` : ""}</div>`;
  }
  if (qc.passed || !(qc.issues || []).length) return "";
  const revision = qc.revision_feedback
    ? `<div class="qc-revision"><b>自动优化修订：</b>${esc(qc.revision_feedback)}</div>` : "";
  return `<div class="plan-err qc-fail-reason"><b>质检没有通过的原因：</b>${esc(qc.issues.join("；"))}
    <span class="dim">(已自动重画 ${qc.attempts} 次仍未过,可改提示词手动重画)</span>${revision}</div>`;
}

function planQcReferenceGalleryHtml(item) {
  const qc = item.qc;
  if (!qc || qc.passed) return "";
  const refs = ((item.reference_inputs || {}).items || [])
    .filter((ref) => ref.url);
  if (!refs.length)
    return `<div class="plan-ref-gallery missing"><b>本次参考图：</b>未附可显示的参考图</div>`;
  return `<div class="plan-ref-gallery"><b>本次质检/重画实际附上的参考图：</b>
    <div class="plan-ref-grid">${refs.map((ref) => `<figure>
      <img class="zoomable" src="${esc(thumbUrl(ref.url, 220))}" loading="lazy"
        alt="${esc(ref.label || ref.name || "参考图")}">
      <figcaption>${esc(ref.label || ref.name || "参考图")}</figcaption>
    </figure>`).join("")}</div></div>`;
}

function planTraceBadges(item) {
  const revision = item.revision || {};
  const refs = item.reference_inputs || {};
  const auto = String(revision.source || "").startsWith("batch_");
  return `${auto && revision.prompt_modified
    ? `<span class="plan-st st-auto" title="批量重画已自动加入修正要求">自动改词✓</span>` : ""}
    ${refs.count
      ? `<span class="plan-st st-refs" title="本次实际交给出图产线的参考图">参考图 ×${refs.count}</span>`
      : (refs.required
        ? `<span class="plan-st st-mock">⚠ 缺参考图</span>` : "")}`;
}

function planTraceHtml(item) {
  const revision = item.revision || {};
  const refs = item.reference_inputs || {};
  const refItems = refs.items || [];
  if (!revision.prompt_modified && !refItems.length && !refs.required) return "";
  return `<details class="plan-trace" ${item.status === "generating" ? "open" : ""}>
    <summary>本次重画记录 · ${revision.prompt_modified ? "提示词已自动修正" : "提示词未改"}
      · ${refItems.length ? `已附 ${refItems.length} 张参考图` : (refs.required ? "缺少必需参考图" : "无需参考图")}</summary>
    ${revision.feedback ? `<div><b>自动修正：</b>${esc(revision.feedback)}</div>` : ""}
    ${refItems.length ? `<div><b>参考图：</b>${refItems.map((ref) =>
      `${esc(ref.label || ref.kind)}「${esc(ref.name || "未命名") }」`
      + `${ref.reference_role ? `〔${esc(ref.reference_role)}${ref.attach_to ? `→${esc(ref.attach_to)}` : ""}〕` : ""}`
    ).join("；")}</div>` : ""}
  </details>`;
}

function planMockReasonHtml(item) {
  const parts = (item.fallbacks || []).map((f) =>
    `${PROVIDER_LABEL[f.provider] || f.provider}:${f.reason}`);
  return parts.length
    ? `<div class="plan-fallback">真实出图产线没接通,逐个回退:${esc(parts.join(";"))}</div>`
    : "";
}

function planCostBadge(item) {
  const provider = String(item.provider || "").toLowerCase();
  const model = `${item.model || ""} ${item.quality || item.image_quality || ""}`.toLowerCase();
  if (provider === "codex")
    return `<span class="plan-st st-cost subscription" title="Codex 订阅内，不增加图片 API 账单">订阅内 · 高清优先</span>`;
  if (["seedream5_lite", "seedream_lite", "seedream"].includes(provider)
      || model.includes("seedream"))
    return `<span class="plan-st st-cost batch" title="Seedream 5.0 Lite 参考价">¥0.22 · 批量优先</span>`;
  if (provider === "image_api" || model.includes("gpt-image-2")) {
    if (model.includes("high"))
      return `<span class="plan-st st-cost expensive" title="高成本模式，不应用于批量候选">高成本 · 仅终稿/复杂文字</span>`;
    return `<span class="plan-st st-cost standard" title="GPT Image 2 medium 参考价">medium 约 ¥0.28</span>`;
  }
  return "";
}

function planQualityBadge(item) {
  const quality = item.image_quality || item.recommended_quality;
  if (!quality) return "";
  const labels = { low: "低", lite: "低", medium: "中", high: "高" };
  const reasons = (item.quality_reasons || []).join("；");
  const source = item.quality_source === "manual" ? "手动" : "自动";
  return `<span class="plan-st st-quality quality-${esc(quality)}"
    title="${esc(`${source}判级${reasons ? `：${reasons}` : ""}`)}">质量 ${labels[quality] || quality}${item.recommended_quality && item.image_quality !== item.recommended_quality ? ` · 建议${labels[item.recommended_quality] || item.recommended_quality}` : ""}</span>`;
}

function planStoryContextHtml(item, compact = false) {
  const story = item.story_context;
  if (!story || item.shot_no == null) return "";
  const eraClass = story.era === "现代" ? "modern"
    : story.era === "明代" ? "historical" : "unknown";
  const excerpt = story.script_excerpt || story.story || "剧本对应原句待补";
  return `<section class="plan-story-context ${eraClass}" aria-label="镜头剧本对应">
    <div class="plan-story-head"><b>本镜剧本对应</b>
      <span class="plan-story-era">${esc(story.era_label || story.era || "时代待确认")}</span></div>
    <div class="plan-story-meta"><span><b>场景：</b>${esc(story.location || "剧本未明确")}</span>
      <span><b>时间：</b>${esc(story.time || "剧本未明确")}</span></div>
    ${story.story && story.story !== excerpt ? `<p><b>镜头故事：</b>${esc(story.story)}</p>` : ""}
    <p><b>剧本原句：</b>${esc(excerpt)}</p>
    ${!compact && story.scene_story ? `<small><b>场次功能：</b>${esc(story.scene_story)}</small>` : ""}
  </section>`;
}

/* 列表用缩略图(服务端按需缩放缓存);灯箱/预览仍加载原图 */
function thumbUrl(url, w = 480) {
  if (!url || !url.startsWith("/artifacts/")) return url;
  const clean = url.split("?")[0].toLowerCase();
  if (!/[.](png|jpe?g|webp)$/.test(clean)) return url;
  return url + (url.includes("?") ? "&" : "?") + "w=" + w;
}

function planItemThumbs(data, item) {
  const art = data.artifacts || {};
  let urls = [];
  if (item.category === "character_art") {
    const row = (art.cast_art || []).find((c) => c.name === item.name);
    if (row && row.url) urls = [row.url];
  } else if (item.category === "character_candidate") {
    const group = ((data.cast_selection || {}).characters || [])
      .find((c) => c.character === item.name);
    const row = group && (group.candidates || [])
      .find((c) => c.index === item.candidate_index);
    if (row && row.url) urls = [row.url];
  } else if (item.category === "character_sheet") {
    const sheets = (art.character_sheets || {})[item.name] || [];
    const row = sheets.find((s) => s.sheet === item.sheet);
    if (row && row.url) urls = [row.url];
  } else if (item.category === "scene_art") {
    const row = (art.scene_art || []).find((s) => s.name === item.name);
    if (row && row.url) urls = [row.url];
  } else if (item.category === "shot_image") {
    if ((art.images || {})[item.shot_no]) urls = [art.images[item.shot_no]];
    else if (item.output_url) urls = [item.output_url];
  } else if (item.category === "frames") {
    if ((art.first || {})[item.shot_no]) urls.push(art.first[item.shot_no]);
    if ((art.last || {})[item.shot_no]) urls.push(art.last[item.shot_no]);
  }
  return urls.filter((u) => u && !u.split("?")[0].endsWith(".json"));
}

function planTargetOf(item) {
  if (item.category === "character_art")
    return { kind: "character_art", name: item.name };
  if (item.category === "character_sheet")
    return { kind: "character_sheet",
             name: `${item.name}:${item.sheet}` };
  if (item.category === "scene_art")
    return { kind: "scene_art", name: item.name };
  if (item.category === "frames")
    return { kind: "frames", shot_no: item.shot_no };
  return { kind: "shot", shot_no: item.shot_no };
}

function planItemHtml(data, item, editable) {
  const thumbs = planItemThumbs(data, item);
  const st = item.status || "pending";
  const canEdit = editable && item.category !== "character_candidate";
  const selectable = canEdit && ["done", "reused", "awaiting_human", "failed"].includes(st);
  const qcFailed = planNeedsRevision(item);
  return `<div class="plan-item plan-selectable st-${st}" data-plan-select="${esc(item.id)}"
    role="button" tabindex="0" aria-pressed="false"
    aria-label="选择查看 ${esc(item.label)}">
    <div class="plan-thumbs">${thumbs.length
      ? thumbs.map((u) => `<img src="${esc(thumbUrl(u, 240))}" loading="lazy" alt="">`).join("")
      : `<span class="plan-thumb-empty">${st === "generating" ? "⏳" : "🖼"}</span>`}</div>
    <div class="plan-main">
      <div class="plan-row">
        ${selectable ? `<label class="plan-pick"
          onclick="event.stopPropagation()"><input type="checkbox"
          class="plan-pick-box" data-pick="${esc(item.id)}"
          data-qc-failed="${qcFailed ? "1" : "0"}"> 选</label>` : ""}
        <b>${esc(item.label)}</b>
        <span class="plan-badges">
        ${planIsMock(item) ? `<span class="plan-st st-mock">⚠ 占位图</span>`
          : (item.provider && item.real
            ? `<span class="plan-st st-real">${esc(PROVIDER_LABEL[item.provider] || item.provider)}</span>` : "")}
        ${planQcBadge(item)}
        ${planTraceBadges(item)}
        ${item.model ? `<span class="plan-st st-model" title="实际记录的模型/托管通道">${esc(item.model)}</span>` : ""}
        ${planQualityBadge(item)}
        ${planCostBadge(item)}
        <span class="plan-st st-${st}">${PLAN_STATUS_CN[st] || st}${item.custom_prompt ? " · 已改词" : ""}</span>
        </span>
      </div>
      ${planIsMock(item) ? planMockReasonHtml(item) : ""}
      ${item.error ? `<div class="plan-err">${esc(item.error)}</div>` : ""}
      ${canEdit && planQcEnabled(item) && ["done", "reused"].includes(st) ? `<div class="plan-qc-row">
        <button class="plan-qc-one" data-plan-id="${esc(item.id)}"
          title="用 AI 视觉核对这张是否符合已锁定人物/场景设定">🔍 质检这张</button></div>` : ""}
      ${canEdit && qcFailed ? `<div class="plan-qc-row">
        <button class="plan-qc-accept" data-plan-id="${esc(item.id)}"
          title="保留原质检问题，人工确认该问题不影响本集观感">✅ 人工通过</button>
      </div>` : ""}
      ${planStoryContextHtml(item)}
      <details class="plan-prompt"><summary>实际发送提示词（镜头合同短版）</summary>
        <pre>${esc(item.prompt_used || item.prompt || "")}</pre></details>
      ${item.prompt_used && item.prompt_used !== item.prompt ? `<details class="plan-prompt"><summary>审计原文（完整提示词）</summary><pre>${esc(item.prompt || "")}</pre></details>` : ""}
      ${planQcIssuesHtml(item)}
      ${planQcReferenceGalleryHtml(item)}
      ${planTraceHtml(item)}
      ${canEdit ? `<div class="plan-edit" data-target="${esc(JSON.stringify(planTargetOf(item)))}">
        <button class="plan-edit-toggle">🔄 修改提示词/重画这张</button>
        <div class="plan-edit-form" hidden>
          <textarea class="plan-edit-prompt" rows="3">${esc(item.prompt_used || item.prompt || "")}</textarea>
          <input class="plan-edit-feedback" placeholder="补充意见(可留空),如:换成夜晚/表情更凶">
          ${qualitySelectHtml("plan-edit-quality")}
          <div class="plan-edit-actions">
            <button class="plan-edit-ref" title="上传参考图并自动挂到本对象,重画立即生效">📎 加参考图</button>
            <button class="primary plan-edit-go">按上面的提示词重画这张</button>
          </div>
        </div></div>`
      : ""}
    </div></div>`;
}

/* ================= 全流程生产表 =================
   用一张表把人物、场景、关键帧、首尾帧和视频串起来。
   图片清单仍保留卡片用于深度编辑；这里负责让用户一眼看出“谁/哪一镜/到哪一步/哪里出错”。 */
const PRODUCTION_LEDGER_STAGES = [
  { key: "character_candidate", label: "人物候选" },
  { key: "character_art", label: "人物立绘" },
  { key: "character_sheet", label: "人物辅助设定" },
  { key: "scene_art", label: "场景概念图" },
  { key: "shot_image", label: "分镜关键帧" },
  { key: "frames", label: "首尾帧" },
  { key: "video", label: "Seedance 视频" },
];
const PRODUCTION_LEDGER_STAGE_ORDER = Object.fromEntries(
  PRODUCTION_LEDGER_STAGES.map((stage, index) => [stage.key, index]));

function productionLedgerStage(item) {
  return PRODUCTION_LEDGER_STAGES.find((stage) => stage.key === item.category)
    || { key: item.category || "other", label: PLAN_CAT_CN[item.category] || item.category || "其他" };
}

function productionLedgerCharacter(data, name) {
  return ((data.cast_selection || {}).characters || [])
    .find((character) => character.character === name) || {};
}

function productionLedgerSelectedCandidate(data, item) {
  if (item.category !== "character_candidate") return false;
  const character = productionLedgerCharacter(data, item.name);
  return !!(character.candidates || []).find((candidate) =>
    Number(candidate.index) === Number(item.candidate_index) && candidate.selected);
}

function productionLedgerReferenceAssets(data, name) {
  const references = (data.artifacts || {}).references || [];
  return references.filter((reference) => {
    const target = String(reference.attach_to || "");
    return target === String(name || "") || target.includes(String(name || ""));
  }).map((reference) => ({
    ...reference, label: reference.note || reference.name || "上传参考图",
    actual: false,
  }));
}

function productionLedgerFallbackRefs(data, item) {
  const refs = [];
  const add = (ref) => {
    if (!ref || (!ref.url && !ref.name && !ref.label)) return;
    const key = `${ref.kind || ""}:${ref.url || ref.name || ref.label}`;
    if (refs.some((row) => row._key === key)) return;
    refs.push({ ...ref, _key: key, actual: false });
  };
  const category = item.category;
  if (["character_candidate", "character_art", "character_sheet"].includes(category)) {
    productionLedgerReferenceAssets(data, item.name).forEach(add);
    const character = productionLedgerCharacter(data, item.name);
    if (character.identity_url && category !== "character_candidate") {
      add({ kind: "identity", label: "最终人物立绘", name: item.name,
        url: character.identity_url });
    }
  }
  if (category === "scene_art") {
    productionLedgerReferenceAssets(data, item.name).forEach(add);
  }
  if (["shot_image", "frames"].includes(category)) {
    const shot = (data.storyboard?.shots || []).find((row) =>
      Number(row.shot_no) === Number(item.shot_no));
    (shot?.characters || []).forEach((name) => {
      const character = productionLedgerCharacter(data, name);
      if (character.identity_url) {
        add({ kind: "identity", label: `${name}·最终立绘`, name,
          url: character.identity_url });
      }
    });
    const scene = (data.artifacts || {}).scene_art?.find((row) =>
      row.name === (data.script?.scenes || []).find(
        (sceneRow) => sceneRow.scene_no === shot?.scene_no)?.location);
    if (scene?.url) add({ kind: "scene", label: "场景锚点", name: scene.name, url: scene.url });
    if (category === "frames" && (data.artifacts || {}).images?.[item.shot_no]) {
      add({ kind: "keyframe", label: "本镜关键帧", name: `镜头${item.shot_no}`,
        url: data.artifacts.images[item.shot_no] });
    }
  }
  return refs.map(({ _key, ...ref }) => ref);
}

function productionLedgerRefs(data, item) {
  const recorded = ((item.reference_inputs || {}).items || [])
    .filter((reference) => reference.url)
    .map((reference) => ({ ...reference, actual: true }));
  return {
    items: recorded.length ? recorded : productionLedgerFallbackRefs(data, item),
    actual: !!recorded.length,
    required: !!(item.reference_inputs || {}).required,
  };
}

function productionLedgerVideoRefs(data, shotNo) {
  const entry = ((data.video_references_effective || {}).shots || {})[String(shotNo)] || {};
  const artifacts = data.artifacts || {};
  const refs = [];
  const add = (kind, label, url, name = label) => {
    if (!url || refs.some((row) => row.url === url)) return;
    refs.push({ kind, label, name, url, actual: true });
  };
  add("first_frame", "首帧·必传", artifacts.first?.[shotNo], `镜头${shotNo}`);
  add("last_frame", "尾帧·必传", artifacts.last?.[shotNo], `镜头${shotNo}`);
  (entry.items || []).forEach((item) => add(
    item.kind, friendlyVideoReferenceName(item, shotNo), item.url, item.name));
  return { items: refs, actual: refs.length > 0, required: true };
}

function productionLedgerState(row) {
  if (row.issue && row.issueCritical) return "failed";
  if (row.selected) return "selected";
  return row.status || "pending";
}

function productionLedgerStateLabel(row) {
  if (row.status === "awaiting_human") return "二次失败·待人工"
  if (row.status === "retrying") return `自动返工 ${row.autoRetriesUsed || 0}/1`;
  if (row.issue && row.issueCritical) return "需要干预";
  if (row.selected) return "已定版";
  if (row.status === "done" && row.mock) return "占位图·需补画";
  return PLAN_STATUS_CN[row.status] || row.status || "待生成";
}

function productionLedgerPlanRows(data) {
  const items = ((data.render_plan || {}).items) || [];
  return items.map((item) => {
    const stage = productionLedgerStage(item);
    const qcIssues = item.qc && item.qc.passed === false
      ? (item.qc.issues || []) : [];
    const issue = item.error || (qcIssues.length ? qcIssues.join("；") : "");
    const refs = productionLedgerRefs(data, item);
    return {
      rowId: `plan:${item.id}`, planId: item.id, category: item.category,
      stageKey: stage.key, stageLabel: stage.label, item,
      objectLabel: item.category === "character_sheet"
        ? `${item.name} · ${item.label || item.sheet || "辅助设定"}`
        : item.category === "character_candidate"
          ? `${item.name} · 候选${item.candidate_index || ""}`
          : item.label || item.name || item.id,
      subLabel: item.category === "character_candidate"
        ? (item.variant_label || "造型候选")
        : item.category === "character_sheet"
          ? (item.sheet || "人物辅助设定") : (item.role || ""),
      status: item.status || "pending", selected: productionLedgerSelectedCandidate(data, item),
      mock: planIsMock(item), issue, issueCritical: !!issue,
      refs, outputUrls: planItemThumbs(data, item),
    };
  });
}

function productionLedgerVideoRows(data) {
  const shots = (data.storyboard || {}).shots || [];
  const artifacts = data.artifacts || {};
  const videoQc = ((data.video_qc_report || {}).shots || []).reduce((map, item) => {
    map[Number(item.shot_no)] = item;
    return map;
  }, {});
  const videoTask = (data.tasks || []).find((task) => task.stage === "videos"
    && ["running", "failed"].includes(task.status));
  return shots.map((shot) => {
    const shotNo = shot.shot_no;
    const videoUrl = artifacts.videos?.[shotNo] || "";
    const qc = videoQc[Number(shotNo)];
    const missingFrames = !artifacts.first?.[shotNo] || !artifacts.last?.[shotNo];
    const status = qc?.awaiting_human ? "awaiting_human"
      : qc && !qc.passed ? "retrying"
      : qc?.passed ? "done"
      : videoUrl ? "done" : videoTask?.status === "failed"
      ? "failed" : videoTask?.status === "running" ? "generating" : "pending";
    const issue = qc?.issues?.length ? qc.issues.join("；")
      : videoTask?.error || (missingFrames ? "缺少首帧或尾帧" : "等待视频生产");
    return {
      rowId: `video:${shotNo}`, shotNo, category: "video", stageKey: "video",
      stageLabel: "Seedance 视频", objectLabel: `镜头 ${String(shotNo).padStart(2, "0")}`,
      subLabel: shot.unit_id || `场${shot.scene_no} · ${shot.shot_function || "视频"}`,
      status, selected: false, mock: false, issue,
      autoRetriesUsed: Number(qc?.auto_retries_used || 0),
      videoQc: qc || null,
      issueCritical: status === "failed" || status === "awaiting_human"
        || (missingFrames && status !== "done"),
      refs: productionLedgerVideoRefs(data, shotNo),
      outputUrls: videoUrl ? [videoUrl] : (artifacts.first?.[shotNo] ? [artifacts.first[shotNo]] : []),
    };
  });
}

function productionLedgerRowIsUseful(row) {
  /* 全流程表是交付视图，不是素材回收站。
     候选/废片仍完整保留在人物定版、图片生产状况和历史记录中，方便回退与追溯。 */
  if (row.category === "character_candidate" && !row.selected) return false;
  if (row.mock) return false;
  if (row.item?.error || row.item?.qc?.passed === false) return false;
  if (["failed", "retrying", "awaiting_human"].includes(row.status)) return false;
  return true;
}

function productionLedgerRows(data) {
  const rows = [...productionLedgerPlanRows(data), ...productionLedgerVideoRows(data)]
    .filter(productionLedgerRowIsUseful);
  return rows.sort((a, b) => (PRODUCTION_LEDGER_STAGE_ORDER[a.stageKey] ?? 99)
    - (PRODUCTION_LEDGER_STAGE_ORDER[b.stageKey] ?? 99)
    || String(a.objectLabel).localeCompare(String(b.objectLabel), "zh"));
}

function productionLedgerReferenceHtml(refs) {
  const rows = refs.items || [];
  if (!rows.length) return refs.required
    ? `<span class="production-ledger-missing">⚠ 待挂载必需参考图</span>`
    : `<span class="production-ledger-no-ref">按规则自动调用</span>`;
  return `<div class="production-ledger-ref-list">
    ${rows.slice(0, 6).map((ref) => {
      const label = ref.label || ref.name || ref.kind || "参考图";
      if (!ref.url) return `<span class="production-ledger-ref${ref.actual ? " actual" : " expected"}"
        title="${esc(label)}">🖼 <b>${esc(label)}</b></span>`;
      return `<button type="button" class="production-ledger-ref production-ledger-ref-preview${ref.actual ? " actual" : " expected"}"
        data-ledger-ref-preview="${esc(ref.url)}" data-ledger-ref-label="${esc(label)}"
        title="点击放大：${esc(label)}" aria-label="点击放大查看${esc(label)}">
        <img src="${esc(thumbUrl(ref.url, 88))}" loading="lazy" alt="${esc(label)}">
        <b>${esc(label)}</b></button>`;
    }).join("")}
    ${rows.length > 6 ? `<small>+${rows.length - 6} 张</small>` : ""}
    ${!refs.actual ? `<small class="production-ledger-expected">预期自动挂载</small>` : ""}
  </div>`;
}

function productionLedgerStoryHtml(row) {
  const story = row.item?.story_context;
  if (!story || row.item?.shot_no == null) return "";
  return `<div class="production-ledger-story">
    <b>${esc(story.era_label || story.era || "时代待确认")} · ${esc(story.location || "场景待确认")}</b>
    <small>剧本：${esc(story.script_excerpt || story.story || "待补")}</small>
  </div>`;
}

function productionLedgerOutputHtml(row) {
  if (!row.outputUrls.length) return `<span class="production-ledger-output-empty">${
    row.status === "generating" ? "⏳ 正在生成" : "尚无产物"}</span>`;
  if (row.category === "video") return `<button class="production-ledger-video-output"
    data-ledger-play="${row.shotNo}" type="button">▶ ${row.status === "done" ? "视频已生成" : "查看当前帧"}</button>`;
  return `<div class="production-ledger-output-list">${row.outputUrls.slice(0, 2).map((url, index) =>
    `<button type="button" class="production-ledger-preview" data-ledger-preview="${esc(url)}"
      aria-label="预览${esc(row.objectLabel)}第${index + 1}张"><img src="${esc(thumbUrl(url, 120))}" loading="lazy" alt=""></button>`
  ).join("")}</div>`;
}

function productionLedgerActionHtml(row) {
  if (row.planId) return `<button type="button" class="production-ledger-action"
    data-ledger-plan="${esc(row.planId)}">${row.issue ? "立即处理" : "查看/干预"}</button>`;
  if (row.category === "video" && row.status === "awaiting_human")
    return `<button type="button" class="production-ledger-action"
      data-ledger-video-redo="${row.shotNo}">填写修改意见并重生成</button>`;
  if (row.category === "video") return `<button type="button" class="production-ledger-action"
    data-ledger-shot="${row.shotNo}">${row.issueCritical ? "查看问题" : "查看镜头"}</button>`;
  return `<span class="production-ledger-no-action">等待进入生产</span>`;
}

function productionProgressNumber(...values) {
  for (const value of values) {
    if (value == null || value === "") continue;
    const number = Number(value);
    if (Number.isFinite(number)) return number;
  }
  return 0;
}

function productionProgressModel(data) {
  const payload = data.production_progress || {};
  const overall = payload.overall || {};
  const plan = ((data.render_plan || {}).items) || [];
  const artifacts = data.artifacts || {};
  const shots = (data.storyboard || {}).shots || [];
  const tasks = data.tasks || [];
  const runningTask = tasks.find((task) => task.status === "running");
  const activeItemsRaw = payload.active_items || overall.active_items
    || plan.filter((item) => ["generating", "retrying"].includes(item.status));
  const activeItems = activeItemsRaw.map((item, index) => {
    const category = item.category || item.stage || overall.stage || runningTask?.stage || "";
    const shotNo = item.shot_no == null ? null : Number(item.shot_no);
    const status = item.status || (item.auto_repair || item.repairing
      ? "retrying" : "generating");
    return {
      id: item.item_id || item.id || `active-${index}`,
      planId: item.plan_id || item.item_id || item.id || "",
      category, shotNo, status,
      label: item.label || item.name || (shotNo == null
        ? (PLAN_CAT_CN[category] || STAGE_CN[category] || "生产任务")
        : `镜头 ${String(shotNo).padStart(2, "0")}`),
      detail: item.detail || item.message || item.phase_label || "",
      startedAt: item.started_at || item.startedAt || "",
      elapsed: productionProgressNumber(item.elapsed, item.elapsed_seconds),
    };
  });
  const episodeStatus = data.episode?.status || "created";
  const stableEpisode = ["done", "failed", "qc_failed", "created", "queued_script",
    "awaiting_script", "awaiting_cast", "awaiting_confirm"].includes(episodeStatus);
  const progressStatus = overall.status || payload.status || "";
  const explicitRunning = overall.running ?? payload.running ?? payload.active;
  const active = explicitRunning != null
    ? ![false, 0, "0", "false", "idle", "stopped"].includes(explicitRunning)
    : progressStatus === "running" || !!runningTask || !stableEpisode;
  const reportedStage = overall.current_stage || overall.stage || payload.current_stage
    || payload.stage || runningTask?.stage
    || (active ? episodeStatus : "");
  const activeStage = activeItems[0]?.category || "";
  const stage = active && activeStage
    && ["failed", "qc_failed", "awaiting_confirm", "production"].includes(reportedStage)
    ? activeStage : reportedStage;
  const reportedStageLabel = overall.current_stage_label || overall.stage_label
    || payload.current_stage_label || payload.stage_label || "";
  const stageLabel = active && activeStage
    && ["failed", "qc_failed", "awaiting_confirm", "production"].includes(reportedStage)
    ? `${PLAN_CAT_CN[activeStage] || STAGE_CN[activeStage] || activeStage}生产`
    : reportedStageLabel || STAGE_CN[stage] || STATUS_CN[stage] || stage || "准备中";
  const categoryRows = Array.isArray(payload.categories)
    ? payload.categories : Object.entries(payload.categories || {}).map(
      ([key, value]) => ({ key, ...(value || {}) }));
  const categories = categoryRows.map((category) => {
    const key = category.key || category.category || category.stage || "";
    return {
      key,
      label: category.label || PLAN_CAT_CN[key] || STAGE_CN[key] || key,
      total: productionProgressNumber(category.total, category.count),
      completed: productionProgressNumber(
        category.usable, category.completed,
        productionProgressNumber(category.done) + productionProgressNumber(category.reused),
        category.formal_assets),
      pending: productionProgressNumber(category.pending, category.queued),
      active: productionProgressNumber(category.active, category.running,
        productionProgressNumber(category.generating)
          + productionProgressNumber(category.retrying)),
      awaitingHuman: productionProgressNumber(category.awaiting_human),
      hardFailed: productionProgressNumber(category.failed),
      unverified: productionProgressNumber(category.unverified_done),
      failed: productionProgressNumber(
        productionProgressNumber(category.awaiting_human)
          + productionProgressNumber(category.failed)
          + productionProgressNumber(category.unverified_done),
        category.second_failures),
    };
  });
  const keyframeCategory = categories.find((row) => row.key === "shot_image")
    || categories.find((row) => row.key === "images");
  const keyframeTotal = productionProgressNumber(
    keyframeCategory?.total, overall.keyframe_total, payload.keyframe_total, shots.length);
  const keyframeDone = productionProgressNumber(
    keyframeCategory?.completed,
    overall.formal_assets, overall.keyframe_completed,
    payload.formal_assets, payload.keyframe_completed,
    Object.keys(artifacts.images || {}).length);
  const reportedImageFailures = (data.image_failures || []).length;
  const keyframeFailed = Math.max(
    productionProgressNumber(keyframeCategory?.awaitingHuman),
    productionProgressNumber(overall.second_failures, payload.second_failures),
    reportedImageFailures);
  const keyframePending = Math.max(0, productionProgressNumber(
    keyframeCategory
      ? keyframeCategory.pending + keyframeCategory.hardFailed
        + keyframeCategory.unverified
        - Math.min(keyframeCategory.hardFailed, reportedImageFailures)
      : null,
    overall.keyframe_pending, payload.keyframe_pending,
    keyframeTotal - keyframeDone - keyframeFailed));
  const categoryTotal = categories.reduce((sum, row) => sum + row.total, 0);
  const categoryDone = categories.reduce((sum, row) => sum + row.completed, 0);
  const total = productionProgressNumber(overall.total, payload.total, categoryTotal);
  const completed = productionProgressNumber(
    overall.usable, overall.completed,
    productionProgressNumber(overall.done) + productionProgressNumber(overall.reused),
    payload.usable, payload.completed, payload.done, categoryDone);
  const percentValue = productionProgressNumber(overall.percent, payload.percent,
    total ? completed / total * 100 : 0);
  const parallelSpec = overall.parallelism || payload.parallelism || {};
  const parallelLane = (stage === "videos" ? parallelSpec.video : parallelSpec.image)
    || parallelSpec;
  const parallelism = Math.max(0, productionProgressNumber(
    parallelLane.limit, parallelLane.parallelism, parallelLane.parallel,
    overall.parallel, overall.concurrency,
    payload.parallel, payload.concurrency,
    activeItems.length));
  const activeCount = Math.max(activeItems.length, productionProgressNumber(
    parallelLane.active, overall.active_count, payload.active_count));
  return {
    active: active && !["completed", "done", "idle", "stopped"].includes(progressStatus),
    stopping: episodeStatus === "cancelling" || progressStatus === "cancelling",
    stage,
    stageLabel: active ? stageLabel : "无生产阶段",
    parallelism, activeCount, activeItems, categories, total, completed,
    pending: productionProgressNumber(overall.pending, payload.pending,
      Math.max(0, total - completed)),
    failed: productionProgressNumber(overall.failed, payload.failed),
    percent: Math.max(0, Math.min(100, percentValue)),
    keyframeTotal, keyframeDone, keyframePending, keyframeFailed,
  };
}

function productionGuidanceModel(data) {
  const progress = productionProgressModel(data);
  const raw = data.production_guidance || {};
  const rawStages = raw.stages || {};
  const plan = ((data.render_plan || {}).items) || [];
  const shots = (data.storyboard || {}).shots || [];
  const artifacts = data.artifacts || {};
  const category = (key) => progress.categories.find((row) => row.key === key) || {};
  const matchingFrameShots = new Set(
    Object.keys(artifacts.first || {}).filter((shotNo) => artifacts.last?.[shotNo]));
  const fallbackCounts = {
    keyframes: {
      total: progress.keyframeTotal,
      usable: progress.keyframeDone,
      pending: progress.keyframePending,
      generating: productionProgressNumber(category("shot_image").active),
      awaiting_human: progress.keyframeFailed,
    },
    frames: {
      total: productionProgressNumber(category("frames").total, shots.length),
      usable: productionProgressNumber(category("frames").completed, matchingFrameShots.size),
      pending: productionProgressNumber(category("frames").pending,
        Math.max(0, shots.length - matchingFrameShots.size)),
      generating: productionProgressNumber(category("frames").active),
      awaiting_human: productionProgressNumber(category("frames").awaitingHuman),
    },
    videos: {
      total: productionProgressNumber(category("video").total, shots.length),
      usable: productionProgressNumber(category("video").completed,
        Object.keys(artifacts.videos || {}).length),
      pending: productionProgressNumber(category("video").pending,
        Math.max(0, shots.length - Object.keys(artifacts.videos || {}).length)),
      generating: productionProgressNumber(category("video").active),
      awaiting_human: productionProgressNumber(category("video").awaitingHuman),
    },
  };
  const statusClass = (status, fallback) => {
    const value = String(status || fallback || "pending").toLowerCase();
    if (["done", "complete", "completed", "ready"].includes(value)) return "complete";
    if (["active", "running", "generating", "retrying"].includes(value)) return "active";
    if (["blocked", "locked"].includes(value)) return "blocked";
    if (["paused", "attention", "awaiting_human", "failed"].includes(value)) return "attention";
    return "pending";
  };
  const buildStage = (key, label) => {
    const source = rawStages[key] || {};
    const fallback = fallbackCounts[key];
    const total = productionProgressNumber(source.total, fallback.total);
    const usable = productionProgressNumber(
      source.usable, source.completed, source.done, fallback.usable);
    const pending = productionProgressNumber(source.pending, fallback.pending);
    const generating = productionProgressNumber(
      source.generating, source.active, fallback.generating);
    const retrying = productionProgressNumber(source.retrying);
    const awaitingHuman = productionProgressNumber(
      source.awaiting_human, fallback.awaiting_human);
    const failed = productionProgressNumber(source.failed);
    const remaining = productionProgressNumber(source.remaining,
      Math.max(0, total - usable));
    const inferred = usable >= total && total > 0 ? "complete"
      : generating + retrying > 0 ? "active"
      : source.blocked_by ? "blocked"
      : awaitingHuman > 0 ? "attention" : "pending";
    return {
      key, label: source.label || label, total, usable, pending, generating,
      retrying, awaitingHuman, failed, remaining,
      status: statusClass(source.status, inferred),
      blockedBy: source.blocked_by || "",
      reason: source.reason || "",
      note: source.note || "",
      parallelCapacity: productionProgressNumber(
        source.parallel_capacity, source.parallelism),
      percent: productionProgressNumber(source.percent,
        total ? usable / total * 100 : 0),
    };
  };
  const keyframes = buildStage("keyframes", "关键帧");
  const frames = buildStage("frames", "首尾帧");
  const videos = buildStage("videos", "视频");
  if (!frames.note && shots.length
      && new Set(shots.map((shot) => shot.scene_no)).size === 1) {
    frames.note = "本集镜头属于同一连续帧链；进入首尾帧阶段后将按 1 路顺序生产，确保前后镜头衔接。";
    frames.parallelCapacity = 1;
  }
  const inferredPhase = keyframes.remaining > 0 ? "keyframes"
    : frames.remaining > 0 ? "frames"
    : videos.remaining > 0 ? "videos" : "review";
  const phase = raw.phase || raw.current_step || inferredPhase;
  const state = raw.state || (progress.active ? "active"
    : phase === "review" ? "ready" : "paused");
  const pendingSource = raw.next_actions?.resume_pending_images
    || raw.actions?.pending_images || {};
  const issueSource = raw.next_actions?.resolve_image_issues
    || raw.actions?.resolve_image_issues || {};
  const pendingShotNos = (pendingSource.shot_nos || plan
    .filter((item) => item.category === "shot_image" && item.status === "pending")
    .map((item) => Number(item.shot_no)).filter(Number.isFinite));
  const pendingItemIds = (pendingSource.item_ids || plan
    .filter((item) => item.category === "shot_image" && item.status === "pending")
    .map((item) => item.id).filter(Boolean));
  const issueShotNos = (issueSource.shot_nos
    || (data.image_failures || []).map((item) => Number(item.shot_no))
      .filter(Number.isFinite));
  const pendingCount = productionProgressNumber(
    pendingSource.count, keyframes.pending, pendingShotNos.length);
  const issueCount = productionProgressNumber(
    issueSource.count, keyframes.awaitingHuman,
    issueShotNos.length);
  const reason = phase === "keyframes"
    ? `${raw.reason ? `${raw.reason} ` : ""}`
      + `当前正式可用 ${keyframes.usable}/${keyframes.total}，`
      + `${pendingCount} 张待生产，${issueCount} 张待人工处理。`
      + "首尾帧必须基于全部合格关键帧，所以门禁尚未开放。"
    : raw.reason || (phase === "frames"
      ? `关键帧已齐，首尾帧为 ${frames.usable}/${frames.total}；补齐后才会进入视频。`
      : phase === "videos"
        ? `首尾帧已齐，视频为 ${videos.usable}/${videos.total}。`
        : "图片与视频资产已齐，等待最终审阅。");
  const currentLabel = raw.current_step_label || ({
    keyframes: "关键帧补齐", frames: "首尾帧生产", videos: "视频生产",
    review: "最终审阅",
  }[phase] || "生产准备");
  const headline = raw.headline || (state === "active"
    ? `正在${currentLabel}` : phase === "review" ? "已进入最终审阅"
      : `停在${currentLabel}`);
  const stages = [
    {
      key: "assets", label: "人物场景", total: 1, usable: 1, remaining: 0,
      status: "complete", reason: "人物与场景母资产已完成",
    },
    keyframes,
    frames,
    videos,
    {
      key: "delivery", label: "交付", total: 1,
      usable: data.episode?.status === "done" ? 1 : 0,
      remaining: data.episode?.status === "done" ? 0 : 1,
      status: data.episode?.status === "done" ? "complete"
        : phase === "review" ? "active" : "blocked",
      blockedBy: phase === "review" ? "" : phase,
      reason: phase === "review" ? "等待最终审阅" : "等待视频生产与质检",
    },
  ];
  return {
    state, phase, currentLabel, headline, reason,
    nextAction: raw.next_action || {},
    blockers: raw.blockers || [],
    canStartFrames: raw.can_start_frames ?? keyframes.remaining === 0,
    canStartVideos: raw.can_start_videos ?? (
      keyframes.remaining === 0 && frames.remaining === 0),
    canConfirmSeedance: raw.can_confirm_seedance ?? (
      keyframes.remaining === 0 && frames.remaining === 0),
    stages,
    actions: {
      pendingImages: {
        count: pendingCount,
        shotNos: pendingShotNos,
        itemIds: pendingItemIds,
        enabled: pendingSource.enabled ?? (pendingCount > 0 && !progress.active),
        label: pendingSource.label
          || `继续生产 ${pendingCount} 张未生成关键帧`,
      },
      resolveIssues: {
        count: issueCount,
        shotNos: issueShotNos,
        enabled: issueSource.enabled ?? issueCount > 0,
        label: issueSource.label
          || `处理 ${issueCount} 张问题图`,
      },
    },
    progress,
  };
}

function productionProgressPanelHtml(data) {
  const guidance = productionGuidanceModel(data);
  const progress = guidance.progress;
  const runtimeLabel = progress.stopping ? "正在安全暂停"
    : progress.active ? `正在运行 · ${progress.activeCount} 项`
      : "当前没有运行任务";
  const currentStage = guidance.stages.find((stage) => stage.key === guidance.phase);
  const stageCount = currentStage?.total
    ? `${currentStage.usable}/${currentStage.total}` : "";
  const pendingAction = guidance.actions.pendingImages;
  const issueAction = guidance.actions.resolveIssues;
  const stageIcon = (status) => status === "complete" ? "✓"
    : status === "active" ? "●" : status === "attention" ? "!"
      : status === "blocked" ? "🔒" : "○";
  return `<section class="production-progress-panel production-guidance ${progress.active ? "is-active" : "is-idle"}"
    aria-label="准确生产进度" data-production-progress>
    <div class="production-guidance-hero">
      <div class="production-progress-state">
        <span class="production-progress-dot${progress.active ? " active" : ""}"></span>
        <div><small>${esc(runtimeLabel)}</small>
          <b>${esc(guidance.headline)}${stageCount ? ` · ${stageCount}` : ""}</b></div>
      </div>
      <div class="production-guidance-channel">
        <small>生产通道</small><b>${progress.active
          ? `${progress.activeCount}/${progress.parallelism || progress.activeCount || 1}`
          : "0 正在占用"}</b>
      </div>
    </div>
    <div class="production-guidance-reason">
      <b>为什么停在这里</b><p>${esc(guidance.reason)}</p>
    </div>
    <ol class="production-stage-chain" aria-label="生产阶段链">
      ${guidance.stages.map((stage, index) => `<li class="stage-${stage.status}">
        <span class="production-stage-icon">${stageIcon(stage.status)}</span>
        <div><b>${esc(stage.label)}</b>
          <small>${stage.total > 1 ? `${stage.usable}/${stage.total}`
            : stage.status === "complete" ? "已完成"
              : stage.status === "blocked" ? "未解锁" : "待处理"}</small></div>
        ${index < guidance.stages.length - 1
          ? `<i class="production-stage-arrow" aria-hidden="true">→</i>` : ""}
      </li>`).join("")}
    </ol>
    ${guidance.stages.find((stage) => stage.key === "frames")?.note
      ? `<div class="production-stage-note"><b>首尾帧运行方式</b><span>${
        esc(guidance.stages.find((stage) => stage.key === "frames").note)}</span></div>`
      : ""}
    ${(pendingAction.enabled || issueAction.enabled) ? `<div class="production-guidance-next">
      <div><small>建议下一步</small><b>${esc(guidance.nextAction.label
        || (pendingAction.enabled ? pendingAction.label : issueAction.label))}</b></div>
      <div class="production-guidance-actions">
        ${pendingAction.enabled ? `<button type="button" class="primary"
          data-guidance-resume data-pending-count="${pendingAction.count}">
          ▶ ${esc(pendingAction.label)}</button>` : ""}
        ${issueAction.enabled ? `<button type="button" class="guidance-issue-action"
          data-guidance-issues data-first-shot="${issueAction.shotNos[0] || ""}">
          ⚠ ${esc(issueAction.label)}</button>` : ""}
      </div>
      <small class="production-guidance-safety">只补缺失项；不会误触 Seedance，也不会重做已通过图片。</small>
    </div>` : ""}
    <div class="production-progress-active">
      <b>${progress.active ? `正在处理 ${progress.activeCount} 项`
        : "当前活动项：无"}</b>
      ${progress.activeItems.length ? `<div class="production-progress-active-list">
        ${progress.activeItems.map((item) => {
          const retrying = item.status === "retrying";
          const actionAttrs = item.shotNo != null
            ? `data-progress-shot="${item.shotNo}"`
            : item.planId ? `data-progress-plan="${esc(item.planId)}"` : "";
          return `<button type="button" class="production-progress-active-item ${retrying ? "retrying" : ""}"
            ${actionAttrs}>
            <span>${retrying ? "↻ 自动修图" : "⏳ 生产中"}</span>
            <b>${esc(item.label)}</b>
            <small>${esc(item.detail || (item.shotNo != null
              ? `${PLAN_CAT_CN[item.category] || STAGE_CN[item.category] || "关键帧"} · 镜头 ${String(item.shotNo).padStart(2, "0")}`
              : PLAN_CAT_CN[item.category] || STAGE_CN[item.category] || item.category))}${
                item.elapsed ? ` · 已进行 ${fmtDur(item.elapsed)}` : ""}</small>
          </button>`;
        }).join("")}</div>`
        : `<span class="production-progress-idle-copy">${progress.active
          ? `当前处于“${esc(progress.stageLabel)}”，暂未拆分到具体图片或视频任务。`
          : "没有图片、首尾帧或视频正在调用 API；上方已说明停滞原因和可执行动作。"}</span>`}
    </div>
    ${progress.categories.length ? `<details class="production-progress-categories">
      <summary>查看各生产环节准确数量</summary>
      <div>${progress.categories.map((row) => `<span>
        <b>${esc(row.label)}</b><small>完成 ${row.completed}/${row.total}
        · 生产中 ${row.active} · 待生产 ${row.pending}
        · 未形成正式资产 ${row.unverified} · 问题 ${row.failed}</small>
      </span>`).join("")}</div></details>` : ""}
  </section>`;
}

const GENERATION_DIAG_STATUS_CN = {
  correct: "输入正确", passed: "通过", needs_patch: "需修正",
  needs_adjustment: "需调整", conflicting: "存在冲突",
  insufficient: "信息不足", missing: "缺失", uncertain: "待确认",
  unknown: "未判定", blocked: "已阻止重试", retry: "按调整重试",
  retry_with_changes: "按调整重试", awaiting_human: "等待人工",
  manual_review: "等待人工", stop: "停止重试", accept: "接受当前结果",
  direct_video_retry: "输入已调整，可重试", repair_frames_first: "先修复源帧",
  none: "无需重试", keep: "保持", remove: "移除", rebind: "重新绑定",
  replace: "替换", add: "补充", drop_revision_base: "移除失败稿基底",
};
const GENERATION_DIAG_FIELD_CN = {
  status: "判断", summary: "摘要", categories: "问题分类", evidence: "画面证据",
  issues: "发现的问题", irrelevant_or_conflicting_sections: "冲突或无关片段",
  missing_roles: "缺失的参考图职责", instructions: "调整指令",
  preserve: "保持不变", max_scope: "调整范围", role: "参考用途",
  character: "对应人物", reason: "原因", action: "动作",
  target_index: "参考图序号", replacement_selector: "替换目标",
  reference_adjustments: "参考图调整", targeted_prompt_patch: "提示词定向修订",
  revision_feedback: "自动优化修订", attempt: "尝试", attempt_no: "尝试",
  generation_attempts: "生成次数", auto_retries_used: "自动重试次数",
  result: "结果", passed: "是否通过", retry_blocked: "是否阻止重试",
  retry_blocked_reason: "阻止原因", attempt_history: "尝试记录",
  decision: "重试决策", prompt_changed: "提示词已变化",
  references_changed: "参考图已变化", changes_input: "生成输入已变化",
  same_as_previous_attempt: "与上次失败输入相同",
  valid: "是否有效", problems: "发现的问题", items: "参考图明细",
  safe_to_auto_retry: "可否安全自动重试", input_changed: "生成输入已变化",
  prompt_patch: "提示词修订", reference_ops: "参考图调整",
  reference_ops_applied: "参考图调整已应用", frame_audit: "源帧诊断",
  first_valid: "首帧有效", last_valid: "尾帧有效",
  source_frames_valid: "源帧有效", continuity_valid: "连续性有效",
  visual_checked: "已做视觉检查", provider: "生成通道", model: "模型",
  generated_at: "生成时间",
};
const GENERATION_DIAG_HIDDEN_FIELDS = new Set([
  "schema", "diagnosis_complete", "prompt_hash", "reference_hash",
  "input_hash", "revised_prompt_hash", "prompt", "prompt_full",
  "prompt_sent", "original_prompt", "revised_prompt", "uri", "path",
  "local_path", "absolute_path", "file_path",
]);

function generationDiagnosisHasValue(value) {
  if (value == null || value === "") return false;
  if (Array.isArray(value)) return value.some(generationDiagnosisHasValue);
  if (typeof value === "object")
    return Object.values(value).some(generationDiagnosisHasValue);
  return true;
}

function generationDiagnosisModel(record = {}) {
  const input = record.input_diagnosis && typeof record.input_diagnosis === "object"
    ? record.input_diagnosis : {};
  const first = (...values) => values.find(generationDiagnosisHasValue);
  const visual = first(input.image_error, input.visual_error, input.frame_audit,
    record.image_error, record.visual_error,
    (record.issues || []).length ? {
      summary: (record.issues || [])[0],
      evidence: record.issues || [],
    } : null);
  const prompt = first(input.prompt_diagnosis, input.prompt_audit,
    record.prompt_diagnosis, record.prompt_audit);
  const references = first(input.reference_diagnosis, input.reference_audit,
    record.reference_diagnosis, record.reference_audit);
  let applied = first(record.applied_changes, input.applied_changes);
  if (!generationDiagnosisHasValue(applied) && record.revision_feedback) {
    applied = { revision_feedback: record.revision_feedback };
  }
  const attempts = first(record.attempt_history, input.attempt_history);
  const decision = first(record.retry_decision, input.retry_decision,
    record.decision, input.decision);
  const decisionObject = decision && typeof decision === "object" ? decision : {};
  const nestedDecision = decisionObject.decision
    && typeof decisionObject.decision === "object" ? decisionObject.decision : {};
  const action = first(
    typeof decision === "string" ? decision : null,
    decisionObject.action, nestedDecision.action);
  const blockedReason = first(record.retry_blocked_reason,
    input.retry_blocked_reason, decisionObject.retry_blocked_reason,
    decisionObject.blocked_reason, nestedDecision.retry_blocked_reason,
    nestedDecision.blocked_reason);
  const retry = {};
  if (generationDiagnosisHasValue(attempts)) retry.attempt_history = attempts;
  if (generationDiagnosisHasValue(decision)) retry.decision = decision;
  if (action && !(decisionObject.action || nestedDecision.action))
    retry.action = action;
  if (blockedReason) retry.retry_blocked_reason = blockedReason;
  return { visual, prompt, references, applied, retry, action, blockedReason };
}

function generationDiagnosisFieldLabel(key) {
  return GENERATION_DIAG_FIELD_CN[key]
    || String(key || "").replaceAll("_", " ");
}

function generationDiagnosisScalarHtml(value, key = "") {
  if (typeof value === "boolean")
    return `<span class="generation-diagnosis-boolean ${value ? "yes" : "no"}">${value ? "是" : "否"}</span>`;
  const text = String(value ?? "");
  if (key === "status" || key === "action") {
    const label = GENERATION_DIAG_STATUS_CN[text] || text;
    return `<span class="generation-diagnosis-status state-${esc(text || "unknown")}">${esc(label)}</span>`;
  }
  return `<span>${esc(text)}</span>`;
}

function generationDiagnosisFieldIsHidden(field) {
  const key = String(field || "").toLowerCase();
  const safePromptField = [
    "diagnosis", "audit", "hash", "changed", "patch",
    "adjustment", "instruction", "delta", "status",
  ].some((token) => key.includes(token));
  return GENERATION_DIAG_HIDDEN_FIELDS.has(key)
    || key.endsWith("_hash") || key.endsWith("_signature")
    || key.endsWith("_uri") || key.endsWith("_path")
    || (key.includes("prompt") && !safePromptField);
}

function generationDiagnosisValueHtml(value, key = "", depth = 0) {
  if (!generationDiagnosisHasValue(value))
    return `<span class="generation-diagnosis-empty">未记录</span>`;
  if (depth > 5) return "";
  if (Array.isArray(value)) {
    return `<ul>${value.map((item) => `<li>${
      generationDiagnosisValueHtml(item, key, depth + 1)}</li>`).join("")}</ul>`;
  }
  if (typeof value !== "object")
    return generationDiagnosisScalarHtml(value, key);
  const entries = Object.entries(value)
    .filter(([field, item]) => !generationDiagnosisFieldIsHidden(field)
      && generationDiagnosisHasValue(item));
  if (!entries.length)
    return `<span class="generation-diagnosis-empty">未记录</span>`;
  return `<dl>${entries.map(([field, item]) => `<div>
    <dt>${esc(generationDiagnosisFieldLabel(field))}</dt>
    <dd>${generationDiagnosisValueHtml(item, field, depth + 1)}</dd>
  </div>`).join("")}</dl>`;
}

function generationDiagnosisSectionHtml(title, value, emptyText, className) {
  return `<section class="generation-diagnosis-section ${className}">
    <h4>${esc(title)}</h4>
    ${generationDiagnosisHasValue(value)
      ? generationDiagnosisValueHtml(value)
      : `<p class="generation-diagnosis-empty">${esc(emptyText)}</p>`}
  </section>`;
}

function generationDiagnosisHtml(record, kind = "image") {
  const diagnosis = generationDiagnosisModel(record);
  const issue = (record.issues || [])[0]
    || diagnosis.visual?.summary || "已保留问题记录";
  const action = diagnosis.action
    ? (GENERATION_DIAG_STATUS_CN[diagnosis.action] || diagnosis.action) : "";
  const summary = `${issue}${action ? ` · ${action}` : ""}`;
  return `<details class="generation-diagnosis generation-diagnosis-${esc(kind)}"
    data-generation-diagnosis>
    <summary><span>生成输入诊断</span>
      <small>${esc(summary.length > 90 ? `${summary.slice(0, 90)}…` : summary)}</small>
    </summary>
    <div class="generation-diagnosis-grid">
      ${generationDiagnosisSectionHtml("画面错在哪里", diagnosis.visual,
        "只有旧版质检原因，暂无结构化画面证据。", "visual")}
      ${generationDiagnosisSectionHtml("提示词诊断", diagnosis.prompt,
        "本次没有记录提示词输入诊断。", "prompt")}
      ${generationDiagnosisSectionHtml("参考图诊断", diagnosis.references,
        "本次没有记录参考图输入诊断。", "references")}
      ${generationDiagnosisSectionHtml("本次实际调整", diagnosis.applied,
        "未记录实际应用到本次生成输入的调整。", "changes")}
      ${generationDiagnosisSectionHtml("重试结果", diagnosis.retry,
        "未记录重试决策或尝试历史。", "retry")}
    </div>
  </details>`;
}

function videoFailurePanelHtml(failures) {
  if (!failures.length) return "";
  return `<section class="production-ledger-video-stop generation-failure-panel"
    role="alert" aria-label="视频待人工问题清单">
    <div class="generation-failure-heading">
      <div><b>⏸ 视频质检已暂停自动返工 · ${failures.length} 个镜头</b>
        <span>系统只自动返工 1 次；请核对本次生成输入，再填写人工修改意见。</span></div>
      <small>问题视频不会被当作已通过成片。</small>
    </div>
    <div class="generation-issue-list">${failures.map((failure) => `
      <article class="generation-issue-card video-failure-item">
        <div class="generation-issue-card-main">
          <span class="generation-issue-kind" aria-hidden="true">🎬</span>
          <div class="generation-issue-copy">
            <b>镜头 ${String(failure.shot_no).padStart(2, "0")}</b>
            <span>${esc((failure.issues || []).join("；") || "视频质检未通过")}</span>
            <small>已自动返工 ${Number(failure.auto_retries_used || 0)} / ${Number(failure.auto_retry_limit || 1)} 次</small>
          </div>
          <button type="button" class="primary generation-issue-action"
            data-ledger-video-redo="${failure.shot_no}">填写修改意见并重生成</button>
        </div>
        ${generationDiagnosisHtml(failure, "video")}
      </article>`).join("")}</div>
  </section>`;
}

function imageFailurePanelHtml(data) {
  const failures = data.image_failures || [];
  if (!failures.length) return "";
  return `<section class="image-failure-panel" role="alert"
    aria-label="待人工问题清单">
    <div class="image-failure-heading">
      <div><b>⚠ 待人工问题清单 · ${failures.length} 张关键帧</b>
        <span>系统已自动定向修图 1 次；这些图仍未通过，但其他关键帧会继续生产。</span></div>
      <div class="image-failure-actions">
        <button type="button" class="image-failure-batch"
          data-image-failure-batch>🛠 打开清单批量优化</button>
        <small>失败稿不会进入正式资产或 Seedance 参考链。</small>
      </div>
    </div>
    <div class="image-failure-list">${failures.map((failure) => `
      <article class="image-failure-item generation-issue-card"
        data-image-failure-item="${esc(failure.item_id || "")}">
        ${failure.failed_output_url ? `<button type="button"
          class="image-failure-preview" data-image-failure-preview="${esc(failure.failed_output_url)}"
          aria-label="查看镜头 ${failure.shot_no} 的失败关键帧">
          <img src="${esc(thumbUrl(failure.failed_output_url, 180))}" loading="lazy" alt=""></button>`
          : `<span class="image-failure-no-preview">暂无预览</span>`}
        <div class="image-failure-copy">
          <b>镜头 ${String(failure.shot_no).padStart(2, "0")}</b>
          <span>${esc((failure.issues || []).join("；") || "图片质检未通过")}</span>
          <small>已自动修图 ${Number(failure.auto_repairs || 1)} 次后仍未过</small>
        </div>
        <button type="button" class="primary image-failure-jump"
          data-image-failure-shot="${failure.shot_no}">
          跳到镜头并展开修改</button>
        <button type="button" class="image-failure-pass"
          data-image-failure-pass="${esc(failure.item_id || "")}">
          ✅ 人工通过</button>
        ${generationDiagnosisHtml(failure, "image")}
      </article>`).join("")}</div>
  </section>`;
}

function productionLedgerHtml(data, options = {}) {
  const rows = productionLedgerRows(data);
  const progress = productionProgressModel(data);
  const tasks = data.tasks || [];
  const runningTask = tasks.find((task) => task.status === "running");
  const currentStage = progress.active
    ? progress.stage || runningTask?.stage || data.episode?.status || "created" : "";
  const currentLabel = progress.active
    ? progress.stageLabel || STAGE_CN[currentStage] || STATUS_CN[currentStage] || currentStage
    : "当前没有生产任务";
  const humanVideoShots = (data.video_qc_report?.shots || [])
    .filter((item) => item.awaiting_human);
  const stageSummary = PRODUCTION_LEDGER_STAGES.map((stage) => {
    const list = rows.filter((row) => row.stageKey === stage.key);
    const done = list.filter((row) => ["done", "reused", "selected"].includes(row.status)
      || row.selected).length;
    const active = list.some((row) => row.status === "generating");
    return { ...stage, total: list.length, done, active };
  }).filter((stage) => stage.total || (stage.key === "video" && data.storyboard));
  const context = options.context || "live";
  return `<section class="production-ledger" data-ledger-context="${esc(context)}">
    <div class="production-ledger-heading">
      <div><h2>📊 全流程生产表</h2>
        <p>精选视图：只显示已选定候选和有效生产资料；未选候选、失败产物、质检废片与占位图已隐藏，原始记录仍可追溯。</p></div>
      <span class="production-ledger-current">${progress.active ? "当前阶段" : "生产状态"}：<b>${esc(currentLabel)}</b></span>
    </div>
    ${productionProgressPanelHtml(data)}
    ${videoFailurePanelHtml(humanVideoShots)}
    ${imageFailurePanelHtml(data)}
    <div class="production-ledger-summary">
      ${stageSummary.map((stage) => `<span class="production-ledger-stage ${stage.done === stage.total && stage.total ? "done" : stage.active ? "running" : "pending"}">
        ${stage.label} <b>${stage.done}/${stage.total}</b></span>`).join("")}
    </div>
    <div class="production-ledger-controls">
      <label>筛选生产项<select class="production-ledger-filter">
        <option value="all">全部有效生产项</option><option value="active">正在生产</option>
        <option value="pending">待补齐资料</option><option value="character">人物与场景</option>
        <option value="shot">关键帧与首尾帧</option><option value="video">视频</option>
      </select></label>
      <span class="production-ledger-filter-summary">显示 ${rows.length}/${rows.length} 项</span>
    </div>
    ${rows.length ? `<div class="production-ledger-table-wrap" role="region" aria-label="全流程生产表，可横向滚动">
      <table class="production-ledger-table"><caption>人物、场景、关键帧、首尾帧和视频生产状态</caption>
        <thead><tr><th>生产对象</th><th>生产环节</th><th>所需参考图</th><th>当前产物</th><th>状态标志</th><th>问题 / 干预</th></tr></thead>
        <tbody>${rows.map((row) => {
          const state = productionLedgerState(row);
          const filterKind = row.category === "video" ? "video"
            : ["character_candidate", "character_art", "character_sheet", "scene_art"].includes(row.category)
              ? "character" : "shot";
          return `<tr class="production-ledger-row state-${esc(state)}" data-ledger-row
            data-ledger-state="${esc(state)}" data-ledger-kind="${filterKind}">
            <th scope="row" class="production-ledger-object"><b>${esc(row.objectLabel)}</b>
              <small>${esc(row.subLabel || "")}</small>${productionLedgerStoryHtml(row)}</th>
            <td data-label="生产环节"><span class="production-ledger-stage-label">${esc(row.stageLabel)}</span>
              ${row.item?.model ? `<small>${esc(row.item.model)}</small>` : ""}</td>
            <td data-label="所需参考图">${productionLedgerReferenceHtml(row.refs)}</td>
            <td data-label="当前产物">${productionLedgerOutputHtml(row)}</td>
            <td data-label="状态标志"><span class="production-ledger-status status-${esc(state)}">${esc(productionLedgerStateLabel(row))}</span>
              ${row.mock ? `<small class="production-ledger-warning">占位产物</small>` : ""}</td>
            <td data-label="问题 / 干预" class="production-ledger-intervention">${row.issue
              ? `<div class="production-ledger-issue">⚠ ${esc(row.issue)}</div>`
              : `<span class="production-ledger-ok">暂无问题</span>`}${productionLedgerActionHtml(row)}</td>
          </tr>`;
        }).join("")}</tbody>
      </table></div>` : `<div class="production-ledger-empty">当前还没有已选定或可用的生产资料。</div>`}
  </section>`;
}

function focusImageFailureShot(root, data, shotNo) {
  document.getElementById("view-theater")?.click();
  const row = document.querySelector(
    `.storyboard-table-row[data-shot="${Number(shotNo)}"]`);
  if (!row) {
    showPlanOverlay(data.episode.id, `shot:${Number(shotNo)}`);
    return;
  }
  const section = row.closest(".shot-production-section");
  const filter = section?.querySelector(".shot-table-filter");
  if (filter) {
    filter.value = "all";
    filter.dispatchEvent(new Event("change"));
  }
  row.hidden = false;
  row.scrollIntoView({ behavior: "smooth", block: "center" });
  row.classList.add("selected", "qc-needs-human");
  setTimeout(() => row.classList.remove("selected"), 2200);
  const editor = row.querySelector(
    ".storyboard-reference .shot-inline-revision");
  if (!editor) {
    showPlanOverlay(data.episode.id, `shot:${Number(shotNo)}`);
    return;
  }
  editor.open = true;
  const failure = (data.image_failures || []).find(
    (item) => Number(item.shot_no) === Number(shotNo));
  const textarea = editor.querySelector(".shot-revision-feedback");
  if (textarea && !textarea.value.trim()) {
    textarea.value = failure?.revision_feedback
      || (failure?.issues || []).join("；");
  }
  textarea?.focus({ preventScroll: true });
  if (editor.dataset.productionActive === "1") {
    showToast("已定位问题图；其他关键帧仍在生产，批次完成后可直接提交修改", "info");
  }
}

async function submitPendingKeyframes(data, episodeId, button) {
  const guidance = productionGuidanceModel(data);
  const itemIds = guidance.actions.pendingImages.itemIds || [];
  if (!itemIds.length) {
    showPlanOverlay(episodeId);
    showToast("已打开图片清单，请筛选待生产关键帧", "info");
    return;
  }
  button.disabled = true;
  button.textContent = `正在提交 ${itemIds.length} 张…`;
  try {
    await api("/api/redo_items", {
      method: "POST", headers: { "Content-Type": "application/json" },
      body: JSON.stringify({
        episode_id: episodeId, item_ids: itemIds, quality: "auto",
      }),
    });
    showToast(`已开始补齐 ${itemIds.length} 张未生成关键帧；不会重做已通过图片`, "ok");
    pollCanvas(episodeId);
  } catch (error) {
    showToast(`提交补齐失败：${error.message}`, "error");
    button.disabled = false;
    button.textContent = `▶ ${guidance.actions.pendingImages.label}`;
  }
}

function bindProductionLedger(root, data, episodeId) {
  if (!root) return;
  root.querySelectorAll("[data-guidance-resume]").forEach((button) => {
    button.onclick = (event) => armConfirm(
      event.currentTarget,
      `补齐 ${Number(event.currentTarget.dataset.pendingCount || 0)} 张`,
      () => submitPendingKeyframes(data, episodeId, event.currentTarget));
  });
  root.querySelectorAll("[data-guidance-issues]").forEach((button) => {
    button.onclick = () => {
      const shotNo = Number(button.dataset.firstShot);
      if (Number.isFinite(shotNo) && shotNo > 0) {
        focusImageFailureShot(root, data, shotNo);
        return;
      }
      showPlanOverlay(episodeId);
    };
  });
  root.querySelectorAll("[data-progress-shot]").forEach((button) => {
    button.onclick = () => {
      const shotNo = Number(button.dataset.progressShot);
      const row = root.querySelector(`.storyboard-table-row[data-shot="${shotNo}"]`)
        || document.querySelector(`.storyboard-table-row[data-shot="${shotNo}"]`);
      if (!row) {
        showPlanOverlay(episodeId, `shot:${shotNo}`);
        return;
      }
      row.hidden = false;
      row.scrollIntoView({ behavior: "smooth", block: "center" });
      row.classList.add("selected");
      setTimeout(() => row.classList.remove("selected"), 1800);
    };
  });
  root.querySelectorAll("[data-progress-plan]").forEach((button) => {
    button.onclick = () => showPlanOverlay(episodeId, button.dataset.progressPlan);
  });
  root.querySelectorAll("[data-image-failure-preview]").forEach((button) => {
    button.onclick = () => showImageLightbox(
      button.dataset.imageFailurePreview, "二次质检未通过的关键帧");
  });
  root.querySelectorAll("[data-image-failure-shot]").forEach((button) => {
    button.onclick = () => focusImageFailureShot(
      root, data, Number(button.dataset.imageFailureShot));
  });
  root.querySelectorAll("[data-image-failure-batch]").forEach((button) => {
    button.onclick = () => showPlanOverlay(episodeId);
  });
  root.querySelectorAll("[data-image-failure-pass]").forEach((button) => {
    button.onclick = () => manualPassItems(
      episodeId, [button.dataset.imageFailurePass], button,
      () => renderCanvasView(episodeId));
  });
  root.querySelectorAll(".production-ledger-preview").forEach((button) => {
    button.onclick = () => showImageLightbox(button.dataset.ledgerPreview, button.getAttribute("aria-label") || "产物预览");
  });
  root.querySelectorAll(".production-ledger-ref-preview").forEach((button) => {
    button.onclick = () => showImageLightbox(
      button.dataset.ledgerRefPreview,
      button.dataset.ledgerRefLabel || "参考图预览");
  });
  root.querySelectorAll(".production-ledger-video-output").forEach((button) => {
    button.onclick = () => openPlayer(data, Number(button.dataset.ledgerPlay));
  });
  root.querySelectorAll("[data-ledger-plan]").forEach((button) => {
    button.onclick = () => showPlanOverlay(episodeId, button.dataset.ledgerPlan);
  });
  root.querySelectorAll("[data-ledger-shot]").forEach((button) => {
    button.onclick = () => {
      const row = root.querySelector(`.storyboard-table-row[data-shot="${button.dataset.ledgerShot}"]`)
        || document.querySelector(`.storyboard-table-row[data-shot="${button.dataset.ledgerShot}"]`);
      if (row) {
        row.scrollIntoView({ behavior: "smooth", block: "center" });
        row.classList.add("selected");
        setTimeout(() => row.classList.remove("selected"), 1800);
      } else showToast("本集分镜表尚未出现，稍后刷新即可查看该镜头", "info");
    };
  });
  root.querySelectorAll("[data-ledger-video-redo]").forEach((button) => {
    button.onclick = async () => {
      const shotNo = Number(button.dataset.ledgerVideoRedo);
      const qc = (data.video_qc_report?.shots || []).find((item) =>
        Number(item.shot_no) === shotNo);
      const initial = qc?.revision_feedback || (qc?.issues || []).join("；");
      const feedback = window.prompt(
        `镜头${shotNo}质检未通过。请填写确认后的修改要求：`, initial);
      if (!feedback || !feedback.trim()) return;
      button.disabled = true; button.textContent = "已提交，重生成中…";
      try {
        const reply = await api("/api/redo_video", {
          method: "POST", headers: { "Content-Type": "application/json" },
          body: JSON.stringify({ episode_id: episodeId, shot_no: shotNo,
            feedback: feedback.trim() }),
        });
        pollJob(reply.job_id, (job) => {
          if (job.status === "done") {
            showToast(`镜头${shotNo}已按人工意见重生成并复检`, "ok");
            renderCanvasView(episodeId);
          } else if (["failed", "stopped"].includes(job.status)) {
            showToast(`镜头${shotNo}重生成失败：${job.error || "请查看日志"}`, "error");
            button.disabled = false; button.textContent = "填写修改意见并重生成";
          }
        });
      } catch (error) {
        showToast(error.message, "error");
        button.disabled = false; button.textContent = "填写修改意见并重生成";
      }
    };
  });
  root.querySelectorAll(".production-ledger-filter").forEach((filter) => {
    const section = filter.closest(".production-ledger");
    const summary = section?.querySelector(".production-ledger-filter-summary");
    const rows = [...(section?.querySelectorAll("[data-ledger-row]") || [])];
    filter.onchange = () => {
      const key = filter.value;
      let visible = 0;
      rows.forEach((row) => {
        const state = row.dataset.ledgerState;
        const kind = row.dataset.ledgerKind;
        const show = key === "all"
          || (key === "active" && ["generating", "pending"].includes(state))
          || (key === "pending" && state === "pending")
          || (key === kind);
        row.hidden = !show;
        if (show) visible += 1;
      });
      if (summary) summary.textContent = `显示 ${visible}/${rows.length} 项`;
    };
  });
}

/* 一键补真:只重画占位图,不动其余 */
async function redoMock(episodeId, btn) {
  if (btn) { btn.disabled = true; btn.textContent = "已提交,补画中…"; }
  try {
    await api("/api/redo_mock", {
      method: "POST", headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ episode_id: episodeId }),
    });
    showToast("开始用真实产线补画占位图,进度看实况看板", "ok");
    pollCanvas(episodeId);
  } catch (e) {
    showToast(staleServerHint(e), "error");
    if (btn) { btn.disabled = false; btn.textContent = "🔁 重试补画"; }
  }
}

/* 占位图警示条:真实出图产线没接通时,大字告知原因与接入方法 */
function mockWarnHtml(data) {
  const items = ((data.render_plan || {}).items) || [];
  const mocks = items.filter(planIsMock);
  if (!mocks.length) return "";
  const reasons = [...new Set((mocks[0].fallbacks || []).map((f) =>
    `${PROVIDER_LABEL[f.provider] || f.provider}(${f.reason})`))];
  return `<div class="mock-warn">
    <b>⚠️ ${mocks.length} 张图是内置占位示意图,不是真实 AI 生成的画面
      <button class="primary mw-redo" onclick="redoMock(${data.episode.id}, this)"
        title="只重画这 ${mocks.length} 张占位图,其余不动;可随时暂停">🔁 用真实产线补画这 ${mocks.length} 张</button></b>
    <span>真实出图产线未接通:${esc(reasons.join(";") || "未检测到可用产线")}。
    接入方法:打开 <a href="#/settings">AI 设置</a> → 点「自动检测」接 Codex CLI,
    或在「图片生成 API」粘贴 Key 保存;接好后回到本集点「全部重做」,
    或在「🖼 图片清单」里单张重画。</span>
  </div>`;
}

function accelerationProviderLabel(name) {
  return PROVIDER_LABEL[name] || name;
}

function accelerationReferenceHtml(refs) {
  const items = (refs || {}).items || [];
  if (!items.length)
    return `<span class="accel-gate bad">✗ 没有真实参考图</span>`;
  return `<div class="accel-refs">${items.map((ref) => `
    <span class="accel-ref" title="${esc(ref.uri || "")}">
      ${ref.url ? `<img src="${esc(thumbUrl(ref.url, 96))}" alt="">` : "🖼"}
      ${esc(ref.label || ref.name || ref.kind || "参考图")}
    </span>`).join("")}</div>`;
}

function accelerationItemHtml(item) {
  const ready = item.status === "ready";
  const issues = item.issues || [];
  return `<article class="accel-item ${ready ? "ready" : "blocked"}">
    <label class="accel-pick"><input type="checkbox" data-accel-item="${esc(item.item_id)}"
      data-contract="${esc(item.contract_token || "")}" ${ready ? "checked" : "disabled"}>
      <b>${esc(item.label || item.item_id)}</b>
      <span>${esc(PLAN_CAT_CN[item.category] || item.category || "图片")}</span>
    </label>
    <div class="accel-gates">
      <span class="accel-gate ${item.prompt ? "ok" : "bad"}">${item.prompt ? "✓" : "✗"} 提示词</span>
      <span class="accel-gate ${(item.references || {}).count ? "ok" : "bad"}">
        ${(item.references || {}).count ? "✓" : "✗"} 参考图 ×${Number((item.references || {}).count || 0)}</span>
      <span class="accel-gate ${Object.keys(item.identity_map || {}).length || !(item.characters || []).length ? "ok" : "bad"}">
        人物映射 ${Object.keys(item.identity_map || {}).length}/${(item.characters || []).length}</span>
    </div>
    ${accelerationReferenceHtml(item.references)}
    ${issues.length ? `<div class="accel-issues" role="alert"><b>暂不放行：</b>${esc(issues.join("；"))}</div>` : ""}
    <details><summary>核对最终提交给 API 的提示词（镜头合同短版）</summary>
      <pre>${esc(item.prompt_used || item.prompt || "")}</pre></details>
    ${item.prompt_used && item.prompt_used !== item.prompt ? `<details><summary>审计原文（完整提示词）</summary><pre>${esc(item.prompt || "")}</pre></details>` : ""}
  </article>`;
}

async function showImageAcceleration(episodeId) {
  let options;
  try { options = await api(`/api/image_acceleration/options?episode_id=${episodeId}`); }
  catch (e) { showToast(staleServerHint(e), "error"); return; }
  const candidates = (options.items || []).filter(
    (item) => ["ready", "blocked", "queued", "running"].includes(item.status));
  const providers = (options.providers || []).filter((item) => item.ready);
  const overlay = document.createElement("div");
  overlay.className = "script-overlay acceleration-overlay";
  overlay.setAttribute("role", "dialog");
  overlay.setAttribute("aria-modal", "true");
  overlay.innerHTML = `<div class="script-panel acceleration-panel">
    <div class="script-head"><h3>⚡ 待生产图片批量 API 加速</h3>
      <button class="close">关闭 Esc</button></div>
    <p class="dim">只处理从未进入 worker 的排队图片。系统逐张锁定最终提示词、人物/场景参考图、API 和模型；
      任一项不一致，整批不放行，也不会回退到其他模型。</p>
    <div class="accel-controls">
      <label>调用 API<select class="accel-provider">${providers.length
        ? providers.map((provider) => `<option value="${esc(provider.provider)}"
          ${provider.provider === options.default_provider ? "selected" : ""}>
          ${esc(accelerationProviderLabel(provider.provider))}</option>`).join("")
        : `<option value="">没有已接通的参考图 API</option>`}</select></label>
      <label>模型<select class="accel-model"></select></label>
      <label>质量<select class="accel-quality">
        <option value="low">低 · 快速候选</option>
        <option value="medium" selected>中 · 默认生产档</option>
        <option value="high">高 · 核心资产/关键镜头</option>
      </select></label>
      <button class="accel-preflight primary" ${providers.length ? "" : "disabled"}>逐张预检所选图片</button>
    </div>
    <div class="accel-summary" role="status">已自动选中可放行的排队图片；先预检，再确认分流。</div>
    <div class="accel-items">${candidates.length
      ? candidates.map(accelerationItemHtml).join("")
      : `<div class="loading">当前没有“从未开工”的排队图片。生成中、失败重试和已完成图片不会被抢占。</div>`}</div>
    <div class="accel-actions">
      <button class="accel-queue primary" disabled>确认并批量调用所选 API</button>
    </div>
  </div>`;
  const close = () => {
    overlay.remove(); document.removeEventListener("keydown", onKey);
  };
  const onKey = (ev) => { if (ev.key === "Escape") close(); };
  overlay.querySelector(".close").onclick = close;
  overlay.addEventListener("click", (ev) => {
    if (ev.target === overlay) close();
  });
  document.addEventListener("keydown", onKey);
  document.body.appendChild(overlay);

  const providerSel = overlay.querySelector(".accel-provider");
  const modelSel = overlay.querySelector(".accel-model");
  const qualitySel = overlay.querySelector(".accel-quality");
  const preflightBtn = overlay.querySelector(".accel-preflight");
  const queueBtn = overlay.querySelector(".accel-queue");
  const summary = overlay.querySelector(".accel-summary");
  let preflight = null;
  const selectedProvider = () => providers.find(
    (provider) => provider.provider === providerSel.value);
  const loadModels = () => {
    const provider = selectedProvider();
    modelSel.innerHTML = (provider?.models || []).map((model) =>
      `<option value="${esc(model)}">${esc(model)}</option>`).join("");
    if (provider?.default_model) modelSel.value = provider.default_model;
  };
  const picked = () => [...overlay.querySelectorAll("[data-accel-item]:checked")];
  const invalidate = () => {
    preflight = null; queueBtn.disabled = true;
    summary.textContent = `已选 ${picked().length} 张；设置有变化，请重新逐张预检。`;
  };
  loadModels();
  providerSel.onchange = () => { loadModels(); invalidate(); };
  modelSel.onchange = invalidate;
  qualitySel.onchange = invalidate;
  overlay.querySelectorAll("[data-accel-item]").forEach(
    (box) => { box.onchange = invalidate; });

  const requestBody = () => {
    const boxes = picked();
    return {
      episode_id: episodeId,
      item_ids: boxes.map((box) => box.dataset.accelItem),
      contract_tokens: Object.fromEntries(boxes.map(
        (box) => [box.dataset.accelItem, box.dataset.contract])),
      provider: providerSel.value,
      model: modelSel.value,
      quality: qualitySel.value || "medium",
    };
  };
  preflightBtn.onclick = async () => {
    const body = requestBody();
    if (!body.item_ids.length) {
      showToast("至少选择一张可放行的排队图片", "error"); return;
    }
    preflightBtn.disabled = true; preflightBtn.textContent = "正在核对…";
    try {
      preflight = await api("/api/image_acceleration/preflight", {
        method: "POST", headers: { "Content-Type": "application/json" },
        body: JSON.stringify(body),
      });
      const failed = (preflight.items || []).filter((item) => item.status !== "ready");
      summary.innerHTML = preflight.passed
        ? `<b class="ok">✓ ${preflight.summary.ready} 张全部通过：</b>
          ${esc(accelerationProviderLabel(preflight.provider))} / ${esc(preflight.model)} /
          质量${preflight.quality === "medium" ? "中" : preflight.quality === "high" ? "高" : "低"}。`
        : `<b class="bad">✗ ${failed.length} 张未通过：</b>${esc(failed.map(
          (item) => `${item.label}:${(item.issues || []).join("；")}`).join("；"))}`;
      queueBtn.disabled = !preflight.passed;
    } catch (e) {
      preflight = null; queueBtn.disabled = true;
      summary.textContent = staleServerHint(e);
      showToast(staleServerHint(e), "error");
    } finally {
      preflightBtn.disabled = !providers.length;
      preflightBtn.textContent = "逐张预检所选图片";
    }
  };
  queueBtn.onclick = async () => {
    if (!preflight?.passed) return;
    queueBtn.disabled = true; queueBtn.textContent = "正在原子分流…";
    try {
      const reply = await api("/api/image_acceleration/queue", {
        method: "POST", headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ ...requestBody(), fingerprint: preflight.fingerprint }),
      });
      showToast(`已将 ${reply.queued} 张安全分流到 ${accelerationProviderLabel(reply.provider)} / ${reply.model}，下一空闲槽开始执行`, "ok");
      close(); pollCanvas(episodeId);
    } catch (e) {
      queueBtn.disabled = false; queueBtn.textContent = "确认并批量调用所选 API";
      showToast(staleServerHint(e), "error");
    }
  };
}

function renderPlanHtml(data, editable) {
  const allItems = ((data.render_plan || {}).items) || [];
  if (!allItems.length) return "";
  const failedOnly = planFailedOnly(data.episode.id);
  const items = failedOnly ? allItems.filter(planNeedsRevision) : allItems;
  const cats = PLAN_CATS
    .filter((c) => items.some((i) => i.category === c));
  const ready = items.filter(
    (i) => ["done", "reused"].includes(i.status)).length;
  const qcFailed = allItems.filter(planNeedsRevision).length;
  const qcItems = allItems.filter(planQcEnabled);
  const acceleration = (data.image_acceleration || {}).summary || {};
  const accelerationCount = Number(acceleration.ready || 0)
    + Number(acceleration.blocked || 0);
  return `<div class="plan-panel">
    <h2>🖼 图片生产清单 <span class="dim">${failedOnly
      ? `仅显示需修改 ${items.length}/${allItems.length} 张`
      : `共 ${items.length} 张 · 已就绪 ${ready}`}</span></h2>
    ${qualityPolicyHtml()}
    ${imageCostGuideHtml(true)}
    ${mockWarnHtml(data)}
    ${lessonsPanelHtml(data)}
    ${relationCanvasHtml(data)}
    ${editable ? `<div class="plan-batchbar">
      ${failedOnly ? `<button class="batch-show-all"
        onclick="planShowAll(${data.episode.id})"
        title="恢复显示本集全部图片">显示全部图片 (${allItems.length})</button>` : ""}
      ${accelerationCount ? `<button class="batch-accelerate primary"
        onclick="showImageAcceleration(${data.episode.id})"
        title="只分流从未进入 worker 的 pending 图片；开工前逐张核对提示词和参考图">
        ⚡ API 加速待生产图片${acceleration.ready ? ` (${acceleration.ready})` : ""}</button>` : ""}
      ${qcItems.length ? `<button class="batch-qc" onclick="qcAll(${data.episode.id}, this)"
        title="只核对后续分镜关键帧和首尾帧">🔍 批量质检镜头图</button>` : ""}
      ${qcItems.length ? `<button class="batch-recheck-current primary"
        onclick="recheckCurrentAndRedoFailed(${data.episode.id}, this)"
        title="把磁盘已有旧图按当前分镜、人物母图和空间图重新质检；仅将失败关键帧交给 Codex A/B 并行重画">
        🔄 当前分镜重检，只重做失败图</button>` : ""}
      <button class="batch-redo-failed" onclick="redoFailed(${data.episode.id}, this)"
        ${qcFailed ? "" : "disabled"}
        title="只显示质检未过的图片，并按每张图的原因自动优化提示词后批量重画">🛠 批量优化修改${qcFailed ? ` (${qcFailed})` : ""}</button>
      ${!failedOnly && qcFailed ? `<button class="batch-filter-failed"
        onclick="planShowFailedOnly(${data.episode.id})"
        title="先筛选出需要修改的图片，不显示已通过图片">只看需修改 (${qcFailed})</button>` : ""}
      <button class="batch-select-qc" onclick="planSelectQcFailed()"
        ${qcFailed ? "" : "disabled"} title="只勾选二次质检未通过的镜头图">选中未过图</button>
      <span class="batch-sep">|</span>
      <button class="batch-selall" onclick="planSelectAll(true)">全选</button>
      <button class="batch-selnone" onclick="planSelectAll(false)">清空</button>
      <button class="batch-sel-frames" onclick="planSelectCat('frames')"
        title="快速勾选全部首尾帧">选中全部首尾帧</button>
      ${qualitySelectHtml("batch-quality")}
      <button class="primary batch-redo-sel" onclick="redoSelected(${data.episode.id}, this)"
        disabled>🔁 重画选中的 <span class="sel-count">0</span> 张</button>
      <button class="batch-manual-pass" onclick="manualPassSelected(${data.episode.id}, this)"
        disabled title="保留质检问题记录，人工确认选中的轻微问题图可继续使用">✅ 人工通过选中的 <span class="sel-qc-count">0</span> 张</button>
    </div>` : ""}
    ${editable ? "" : `<div class="dim plan-hint">列表实时更新;要修改某张的提示词重画,等生成停下后点工具栏「🖼 图片清单」。</div>`}
    ${items.length ? cats.map((cat) => {
      const list = items.filter((i) => i.category === cat);
      const ok = list.filter(
        (i) => ["done", "reused"].includes(i.status)).length;
      return `<div class="plan-cat"><h3>${PLAN_CAT_CN[cat]} <span class="dim">${ok}/${list.length}</span></h3>
        <div class="plan-list">${list.map((i) => planItemHtml(data, i, editable)).join("")}</div></div>`;
    }).join("") : `<div class="plan-filter-empty">当前没有待修改图片；质检通过后会自动从此筛选中移除。</div>`}</div>`;
}

/* 单张质检 / 批量质检 / 批量重画未过 */
async function qcOne(episodeId, itemId, btn, onDone) {
  if (btn) { btn.disabled = true; btn.textContent = "质检中…"; }
  try {
    const r = await api("/api/qc_item", {
      method: "POST", headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ episode_id: episodeId, item_id: itemId }),
    });
    showToast(r.passed ? "质检通过 ✓"
      : "质检未过:" + (r.issues || []).join(";"), r.passed ? "ok" : "error");
    if (onDone) onDone(); else renderCanvasView(episodeId);
  } catch (e) {
    showToast(staleServerHint(e), "error");
    if (btn) { btn.disabled = false; btn.textContent = "🔍 质检这张"; }
  }
}

async function qcAll(episodeId, btn) {
  if (btn) { btn.disabled = true; btn.textContent = "批量质检中…"; }
  try {
    const reply = await api("/api/qc_all", {
      method: "POST", headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ episode_id: episodeId }),
    });
    pollJob(reply.job_id, (job) => {
      const s = job.summary || {};
      if (job.status === "done")
        showToast(`批量质检完成:核对 ${s.checked || 0} 张,`
          + `通过 ${s.passed || 0},未过 ${s.failed || 0}`, "ok");
      else showToast(job.error || "批量质检结束", "info");
      renderCanvasView(episodeId);
    });
    showToast("已开始批量质检,逐张核对中…", "ok");
  } catch (e) {
    showToast(staleServerHint(e), "error");
    if (btn) { btn.disabled = false; btn.textContent = "🔍 批量质检全部"; }
  }
}

async function recheckCurrentAndRedoFailed(episodeId, btn) {
  const original = btn?.textContent || "🔄 当前分镜重检，只重做失败图";
  if (btn) { btn.disabled = true; btn.textContent = "正在安全暂停…"; }
  try {
    await ensureBatchRevisionCheckpoint(episodeId);
    if (btn) btn.textContent = "Codex 多通道重新质检中…";
    const reply = await api("/api/qc_all", {
      method: "POST", headers: { "Content-Type": "application/json" },
      body: JSON.stringify({
        episode_id: episodeId,
        include_existing: true,
        auto_repair: false,
        parallel: true,
        categories: ["shot_image"],
      }),
    });
    showToast("已开始按当前分镜重新质检；旧图不会直接进入首尾帧或视频", "ok");
    pollJob(reply.job_id, async (job) => {
      if (job.status !== "done") {
        showToast(job.error || "当前分镜重检未完成", "error");
        if (btn) { btn.disabled = false; btn.textContent = original; }
        renderCanvasView(episodeId);
        return;
      }
      const summary = job.summary || {};
      const current = await api(`/api/episode/${episodeId}`);
      const failedIds = ((current.render_plan || {}).items || [])
        .filter((item) => item.category === "shot_image"
          && (item.qc || {}).passed === false
          && (item.qc || {}).contract_recheck)
        .map((item) => item.id);
      if (!failedIds.length) {
        showToast(`重检完成：${summary.passed || 0} 张全部通过，不需要重画`, "ok");
        if (btn) { btn.disabled = false; btn.textContent = original; }
        renderCanvasView(episodeId);
        return;
      }
      if (btn) btn.textContent = `A/B 并行重画 ${failedIds.length} 张…`;
      const redraw = await api("/api/redo_items", {
        method: "POST", headers: { "Content-Type": "application/json" },
        body: JSON.stringify({
          episode_id: episodeId, item_ids: failedIds, quality: "auto",
        }),
      });
      watchBatchRedraw(episodeId, redraw.job_id, failedIds.length, (redrawJob) => {
        const result = redrawJob.summary || {};
        if (redrawJob.status === "done")
          showToast(`选择性返工完成：仅重画 ${result.redone || 0} 张失败图`, "ok");
        else showToast(redrawJob.error || "选择性返工未完成", "error");
        if (btn) { btn.disabled = false; btn.textContent = original; }
        renderCanvasView(episodeId);
      });
      showToast(`重检发现 ${failedIds.length} 张失败图，已交给 Codex A/B 并行重画`, "ok");
      pollCanvas(episodeId);
    });
    pollCanvas(episodeId);
  } catch (e) {
    showToast(staleServerHint(e), "error");
    if (btn) { btn.disabled = false; btn.textContent = original; }
  }
}

/* 勾选批量重画:选中若干张 → 一次性重画 */
function planPickedIds() {
  return [...document.querySelectorAll(".plan-pick-box:checked")]
    .map((b) => b.dataset.pick);
}
function planPickedQcIds() {
  return [...document.querySelectorAll(
    '.plan-pick-box[data-qc-failed="1"]:checked')]
    .map((b) => b.dataset.pick);
}
function planUpdateSelCount() {
  const n = planPickedIds().length;
  const qcCount = planPickedQcIds().length;
  document.querySelectorAll(".sel-count").forEach((s) => {
    s.textContent = n;
  });
  document.querySelectorAll(".sel-qc-count").forEach((s) => {
    s.textContent = qcCount;
  });
  document.querySelectorAll(".batch-redo-sel").forEach((b) => {
    b.disabled = n === 0;
  });
  document.querySelectorAll(".batch-manual-pass").forEach((b) => {
    b.disabled = qcCount === 0;
  });
}
function planSelectAll(on) {
  document.querySelectorAll(".plan-pick-box").forEach((b) => {
    b.checked = on;
  });
  planUpdateSelCount();
}
function planSelectCat(cat) {
  document.querySelectorAll(".plan-pick-box").forEach((b) => {
    b.checked = b.dataset.pick.startsWith(cat + ":");
  });
  planUpdateSelCount();
}
function planSelectQcFailed() {
  document.querySelectorAll(".plan-pick-box").forEach((b) => {
    b.checked = b.dataset.qcFailed === "1";
  });
  planUpdateSelCount();
}

async function ensureBatchRevisionCheckpoint(episodeId) {
  const current = await api(`/api/episode/${episodeId}`);
  if (!current.production_progress?.overall?.running) return current;
  try {
    await api("/api/stop", {
      method: "POST", headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ episode_id: episodeId }),
    });
  } catch (error) {
    // 任务可能恰好结束；下面以稳定状态复核为准。
  }
  return waitForShotRevisionCheckpoint(
    episodeId, current.project.title, current.episode.number);
}

const batchRedrawSignatures = new Map();

function updateBatchRedrawProgress(episodeId, job, fallbackTotal = 0) {
  const box = document.querySelector(
    `.plan-overlay[data-episode="${episodeId}"] .batch-job-progress`);
  if (!box) return;
  const p = job.progress || {};
  const s = job.summary || {};
  const total = Number(p.total || s.total || fallbackTotal || 0);
  const completed = Number(p.completed || (job.status === "done" ? s.redone : 0) || 0);
  const pct = total ? Math.min(100, Math.round(completed / total * 100)) : 0;
  const phase = p.phase || (job.status === "done" ? "done" : "queued");
  const phaseCN = {
    queued: "排队中", redrawing: "正在重新画", checking: "正在自动复检",
    running: "继续下一张", done: "批量重画完成", paused: "已暂停",
    blocked: "已阻止无效重画",
  }[phase] || phase;
  const current = p.current_label
    ? ` · 当前：${esc(p.current_label)}` : "";
  const refText = p.references_attached
    ? `已自动附上 ${Number(p.reference_count || 0)} 张参考图`
    : "参考图将在每张开画前自动挂载并显示";
  const note = p.note || s.note || "";
  box.hidden = false;
  box.innerHTML = `<div class="batch-job-head"><b>${esc(phaseCN)}</b>
      <span>${completed}/${total || "?"} 张${current}</span></div>
    <div class="batch-job-track"><i style="width:${pct}%"></i></div>
    <div class="batch-job-meta">
      <span>提示词：${p.prompt_modified || p.prompt_policy === "auto_revision"
        ? "已自动加入本次修正要求 ✓" : "等待处理"}</span>
      <span>参考图：${esc(refText)}</span>
      ${p.checked != null ? `<span>复检：${p.qc_passed || 0} 通过 / ${p.qc_failed || 0} 未过</span>` : ""}
      ${note ? `<span class="plan-err">${esc(note)}</span>` : ""}
    </div>`;
}

async function refreshOpenPlanOverlay(episodeId, force = false) {
  const overlay = document.querySelector(
    `.plan-overlay[data-episode="${episodeId}"]`);
  const panel = overlay?.querySelector(".plan-overlay-content");
  if (!panel) return false;
  const active = document.activeElement;
  if (!force && (panel.querySelector(".plan-edit-form:not([hidden])")
      || active?.closest(".plan-edit-form"))) {
    // 用户正在改词时不替换 DOM；否则 textarea 会失焦、滚动位置也会跳走。
    return false;
  }
  const scrollState = [overlay, panel].map((el) => [el, el.scrollTop]);
  const checked = new Set([...panel.querySelectorAll(
    ".plan-pick-box:checked")].map((box) => box.dataset.pick));
  const openDetails = [...panel.querySelectorAll("details[open]")].map((details) => {
    const card = details.closest("[data-plan-select]");
    if (!card) return null;
    return [card.dataset.planSelect,
      [...card.querySelectorAll("details")].indexOf(details)];
  }).filter(Boolean);
  try {
    const data = await api(`/api/episode/${episodeId}`);
    planOverlaySignatures.set(String(episodeId), canvasSig(data));
    panel.innerHTML = renderPlanHtml(data, true)
      || `<div class="dim">本集还没有图片生产计划。</div>`;
    bindPlanSelection(panel, data, episodeId);
    bindPlanRegen(panel, episodeId, () => refreshOpenPlanOverlay(episodeId));
    panel.querySelectorAll(".plan-pick-box").forEach((box) => {
      box.checked = checked.has(box.dataset.pick);
    });
    openDetails.forEach(([id, index]) => {
      const card = [...panel.querySelectorAll("[data-plan-select]")]
        .find((candidate) => candidate.dataset.planSelect === id);
      const details = card?.querySelectorAll("details");
      if (details?.[index]) details[index].open = true;
    });
    planUpdateSelCount();
    // innerHTML 替换后在下一帧恢复两个可能的滚动容器，避免生成进度导致页面跳动。
    requestAnimationFrame(() => scrollState.forEach(([el, top]) => {
      if (el) el.scrollTop = top;
    }));
    return true;
  } catch (e) { /* 下一次任务进度更新再试 */ }
  return false;
}

function watchBatchRedraw(episodeId, jobId, total, onDone) {
  pollJob(jobId, (job) => {
    updateBatchRedrawProgress(episodeId, job, total);
    refreshOpenPlanOverlay(episodeId);
    onDone(job);
  }, (job) => {
    updateBatchRedrawProgress(episodeId, job, total);
    const p = job.progress || {};
    const signature = JSON.stringify([
      p.phase, p.completed, p.current_item, p.reference_count,
      p.qc_passed, p.qc_failed,
    ]);
    if (batchRedrawSignatures.get(jobId) !== signature) {
      batchRedrawSignatures.set(jobId, signature);
      refreshOpenPlanOverlay(episodeId);
    }
  });
}

async function redoSelected(episodeId, btn) {
  const ids = planPickedIds();
  if (!ids.length) return;
  const quality = btn?.closest(".plan-panel")?.querySelector(".batch-quality")?.value || "auto";
  if (btn) { btn.disabled = true; btn.textContent = "正在安全暂停…"; }
  try {
    await ensureBatchRevisionCheckpoint(episodeId);
    if (btn) btn.textContent = "已提交,重画中…";
    const reply = await api("/api/redo_items", {
      method: "POST", headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ episode_id: episodeId, item_ids: ids, quality }),
    });
    watchBatchRedraw(episodeId, reply.job_id, ids.length, (job) => {
      if (job.status === "done")
        showToast(`已重画选中的 ${(job.summary || {}).redone || 0} 张`, "ok");
      if (document.querySelector(`.plan-overlay[data-episode="${episodeId}"]`))
        refreshOpenPlanOverlay(episodeId, true);
      else renderCanvasView(episodeId);
    });
    showToast(`开始重画选中的 ${ids.length} 张,进度看实况看板`, "ok");
    pollCanvas(episodeId);
  } catch (e) {
    showToast(staleServerHint(e), "error");
    if (btn) { btn.disabled = false; btn.innerHTML =
      `🔁 重画选中的 <span class="sel-count">${ids.length}</span> 张`; }
  }
}

async function redoFailed(episodeId, btn) {
  // 先切换到“只看需修改”，避免批量操作把用户带回全部图片列表。
  planFailedOnlyByEpisode.set(String(episodeId), true);
  if (document.querySelector(`.plan-overlay[data-episode="${episodeId}"]`))
    refreshOpenPlanOverlay(episodeId, true);
  const quality = btn?.closest(".plan-panel")?.querySelector(".batch-quality")?.value || "auto";
  if (btn) { btn.disabled = true; btn.textContent = "正在安全暂停…"; }
  try {
    await ensureBatchRevisionCheckpoint(episodeId);
    if (btn) btn.textContent = "已提交,重画中…";
    const reply = await api("/api/redo_items", {
      method: "POST", headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ episode_id: episodeId, only_failed: true, quality }),
    });
    watchBatchRedraw(episodeId, reply.job_id, 0, (job) => {
      const summary = job.summary || {};
      if (summary.status === "blocked")
        showToast(summary.note || "检测到系统性人物漂移，已停止批量重画", "error");
      else if (job.status === "done")
        showToast(`已重画 ${summary.redone || 0} 张质检未过的图`, "ok");
      if (document.querySelector(`.plan-overlay[data-episode="${episodeId}"]`))
        refreshOpenPlanOverlay(episodeId, true);
      else renderCanvasView(episodeId);
    });
    showToast("开始重画质检未过的图,进度看实况看板", "ok");
    pollCanvas(episodeId);
  } catch (e) {
    showToast(staleServerHint(e), "error");
    if (btn) { btn.disabled = false; btn.textContent = "🔁 重画质检未过的"; }
  }
}

async function manualPassItems(episodeId, ids, btn, onDone) {
  if (!ids.length) return;
  const original = btn?.textContent || "✅ 人工通过";
  if (btn) { btn.disabled = true; btn.textContent = "正在安全暂停…"; }
  try {
    await ensureBatchRevisionCheckpoint(episodeId);
    if (btn) btn.textContent = "正在登记人工通过…";
    const reply = await api("/api/qc_override", {
      method: "POST", headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ episode_id: episodeId, item_ids: ids }),
    });
    showToast(`已人工通过 ${reply.passed || 0} 张，保留原质检问题记录`, "ok");
    if (reply.skipped) {
      showToast(`另有 ${reply.skipped} 张未能放行，请检查图片是否存在`, "info");
    }
    if (onDone) onDone(); else renderCanvasView(episodeId);
  } catch (e) {
    showToast(staleServerHint(e), "error");
    if (btn) { btn.disabled = false; btn.textContent = original; }
  }
}

function manualPassSelected(episodeId, btn) {
  const ids = planPickedQcIds();
  manualPassItems(episodeId, ids, btn, () => refreshOpenPlanOverlay(episodeId));
}

function bindPlanRegen(container, episodeId, onDone) {
  container.querySelectorAll(".plan-pick-box").forEach((box) => {
    box.onchange = planUpdateSelCount;
  });
  container.querySelectorAll(".plan-qc-one").forEach((btn) => {
    btn.onclick = (ev) => {
      ev.stopPropagation();
      qcOne(episodeId, btn.dataset.planId, btn, onDone);
    };
  });
  container.querySelectorAll(".plan-qc-accept").forEach((btn) => {
    btn.onclick = (ev) => {
      ev.stopPropagation();
      manualPassItems(episodeId, [btn.dataset.planId], btn, onDone);
    };
  });
  container.querySelectorAll(".plan-edit").forEach((box) => {
    const form = box.querySelector(".plan-edit-form");
    box.querySelector(".plan-edit-toggle").onclick = () => {
      form.hidden = !form.hidden;
      if (!form.hidden) form.querySelector("textarea").focus();
    };
    const planRefBtn = box.querySelector(".plan-edit-ref");
    if (planRefBtn) planRefBtn.onclick = () => uploadRegenReference(
      episodeId, JSON.parse(box.dataset.target), planRefBtn);
    box.querySelector(".plan-edit-go").onclick = async () => {
      const target = JSON.parse(box.dataset.target);
      const prompt = form.querySelector(".plan-edit-prompt").value.trim();
      const feedback = form.querySelector(".plan-edit-feedback").value.trim();
      const quality = form.querySelector(".plan-edit-quality").value;
      const btn = box.querySelector(".plan-edit-go");
      btn.disabled = true; btn.textContent = "正在安全暂停…";
      try {
        await ensureBatchRevisionCheckpoint(episodeId);
        btn.textContent = "已提交,重画中…";
        const reply = await api("/api/regen_image", {
          method: "POST", headers: { "Content-Type": "application/json" },
          body: JSON.stringify({ episode_id: episodeId, target,
                                 feedback, prompt, quality }),
        });
        pollJob(reply.job_id, (job) => {
          if (job.status === "done") {
            showToast("这张图已按新提示词重画完成", "ok");
            if (onDone) onDone(); else renderCanvasView(episodeId);
          } else {
            showToast(job.error || "重画失败", "error");
            btn.disabled = false;
            btn.textContent = "按上面的提示词重画这张";
          }
        });
      } catch (e) {
        showToast(e.message, "error");
        btn.disabled = false; btn.textContent = "按上面的提示词重画这张";
      }
    };
  });
}

/* 图片卡片选择/大图查看:直播看板和图片清单共用。
   选中项跨实时轮询保留；点击提示词、编辑控件时不误开预览。 */
const selectedPlanByEpisode = new Map();

function showPlanItemPreview(data, item, episodeId) {
  const thumbs = planItemThumbs(data, item);
  const st = item.status || "pending";
  const overlay = document.createElement("div");
  overlay.className = "script-overlay image-preview-overlay";
  const gallery = thumbs.length
    ? `<div class="plan-preview-gallery">
        <img class="plan-preview-main zoomable" src="${esc(thumbs[0])}"
          alt="${esc(item.label)}大图">
        ${thumbs.length > 1 ? `<div class="plan-preview-thumbs">${thumbs.map((url, index) =>
          `<button type="button" class="plan-preview-thumb${index === 0 ? " active" : ""}"
            data-preview-url="${esc(url)}" aria-label="查看第 ${index + 1} 张">
            <img src="${esc(url)}" alt=""></button>`).join("")}</div>` : ""}
      </div>`
    : `<div class="plan-preview-empty">${st === "generating" ? "⏳ 正在生成，完成后这里会自动出现大图"
      : st === "failed" ? `生成失败：${esc(item.error || "未提供原因")}`
      : "🖼 图片尚未生成"}</div>`;
  overlay.innerHTML = `<div class="script-panel image-preview-panel">
    <div class="script-head">
      <div><h3>${esc(item.label)}</h3>
        <span class="dim">${esc(PLAN_CAT_CN[item.category] || item.category || "图片")}</span></div>
      <button class="close">关闭 Esc</button>
    </div>
    ${gallery}
    <div class="plan-preview-meta">
      <span class="plan-st st-${st}">${esc(PLAN_STATUS_CN[st] || st)}</span>
      ${item.provider ? `<span>${esc(PROVIDER_LABEL[item.provider] || item.provider)}</span>` : ""}
      ${item.model ? `<span>${esc(item.model)}</span>` : ""}
      ${thumbs.length ? `<button type="button" class="preview-download">⬇ 下载当前图片</button>` : ""}
    </div>
    ${item.error ? `<div class="plan-err">${esc(item.error)}</div>` : ""}
    ${planStoryContextHtml(item)}
    <details class="plan-prompt" open><summary>实际发送提示词（镜头合同短版）</summary>
      <pre>${esc(item.prompt_used || item.prompt || "")}</pre></details>
    ${item.prompt_used && item.prompt_used !== item.prompt ? `<details class="plan-prompt"><summary>审计原文（完整提示词）</summary><pre>${esc(item.prompt || "")}</pre></details>` : ""}
  </div>`;
  const close = () => {
    overlay.remove();
    document.removeEventListener("keydown", onKey);
  };
  const onKey = (ev) => { if (ev.key === "Escape") close(); };
  overlay.addEventListener("click", (ev) => {
    if (ev.target === overlay) close();
  });
  overlay.querySelector(".close").onclick = close;
  overlay.querySelectorAll(".plan-preview-thumb").forEach((btn) => {
    btn.onclick = () => {
      overlay.querySelector(".plan-preview-main").src = btn.dataset.previewUrl;
      overlay.querySelectorAll(".plan-preview-thumb").forEach((other) =>
        other.classList.toggle("active", other === btn));
    };
  });
  const download = overlay.querySelector(".preview-download");
  if (download) download.onclick = () => {
    const current = overlay.querySelector(".plan-preview-main").src;
    downloadUrl(current, `${item.label || "AIFOS图片"}.png`);
  };
  document.addEventListener("keydown", onKey);
  document.body.appendChild(overlay);
  selectedPlanByEpisode.set(episodeId, item.id);
}

function bindPlanSelection(container, data, episodeId) {
  const items = ((data.render_plan || {}).items) || [];
  const byId = new Map(items.map((item) => [String(item.id), item]));
  const cards = [...container.querySelectorAll("[data-plan-select]")];
  const saved = selectedPlanByEpisode.get(episodeId);
  const select = (card, item, openPreview) => {
    cards.forEach((other) => {
      const active = other === card;
      other.classList.toggle("selected", active);
      other.setAttribute("aria-pressed", active ? "true" : "false");
    });
    selectedPlanByEpisode.set(episodeId, item.id);
    if (openPreview) showPlanItemPreview(data, item, episodeId);
  };
  cards.forEach((card) => {
    const item = byId.get(card.dataset.planSelect);
    if (!item) return;
    if (String(item.id) === String(saved)) select(card, item, false);
    card.onclick = (ev) => {
      if (ev.target.closest("button, a, input, textarea, select, details, summary, label")) return;
      select(card, item, true);
    };
    card.onkeydown = (ev) => {
      if (ev.key !== "Enter" && ev.key !== " ") return;
      if (ev.target.closest("button, a, input, textarea, select, details, summary, label")) return;
      ev.preventDefault();
      select(card, item, true);
    };
  });
}

/* ---- 生产直播大看板:每张图一张卡,秒表+预计耗时,画完立刻上图 ---- */
function fmtDur(sec) {
  sec = Math.max(0, Math.round(sec));
  if (sec < 60) return `${sec} 秒`;
  const m = Math.floor(sec / 60), s = sec % 60;
  return s ? `${m} 分 ${s} 秒` : `${m} 分钟`;
}

function planAvgDuration(items, category) {
  const durs = (list) => list.filter((i) => i.duration > 0)
    .map((i) => i.duration);
  let pool = category
    ? durs(items.filter((i) => i.category === category)) : [];
  if (!pool.length) pool = durs(items);
  if (!pool.length) return null;
  return pool.reduce((a, b) => a + b, 0) / pool.length;
}

function planCardHtml(data, item, avg) {
  const thumbs = planItemThumbs(data, item);
  const st = item.status || "pending";
  let media;
  if (["done", "reused"].includes(st) && thumbs.length)
    media = `<img src="${esc(thumbUrl(thumbs[0], 480))}" loading="lazy" alt="">`;
  else if (st === "generating") media = `<div class="pc-empty gen">⏳</div>`;
  else if (st === "failed") media = `<div class="pc-empty fail">✗</div>`;
  else media = `<div class="pc-empty">🖼</div>`;
  let state;
  if (st === "generating") {
    state = `<span class="pc-state pc-timer" data-started="${item.started_at || ""}"
      data-eta="${avg ? Math.round(avg) : ""}">正在画 0:00${avg ? ` · 预计 ~${fmtDur(avg)}` : ""}</span>`;
  } else if (["done", "reused"].includes(st)) {
    const qc = item.qc;
    const sourceNote = item.first_source === "keyframe" ? " · 首帧复用✓"
      : item.first_source === "previous_tail" ? " · 帧链复用✓" : "";
    const qcNote = !qc ? "" : (qc.passed ? " · 质检✓"
      : ` · <b class="pc-mock" title="${esc((qc.issues || []).join(";"))}">⚠质检未过</b>`);
    state = `<span class="pc-state pc-ok">✓ ${st === "reused" ? "复用已有" : "已完成"}${item.duration ? ` · 用时 ${fmtDur(item.duration)}` : ""}${sourceNote}${planIsMock(item) ? ` · <b class="pc-mock">占位图</b>` : ""}${qcNote}</span>`;
  } else if (st === "failed") {
    state = `<span class="pc-state pc-fail" title="${esc(item.error || "")}">失败:${esc((item.error || "").slice(0, 40))}</span>`;
  } else {
    state = `<span class="pc-state pc-wait">排队中</span>`;
  }
  return `<div class="plan-card plan-selectable st-${st}" data-plan-select="${esc(item.id)}"
    role="button" tabindex="0" aria-pressed="false"
    aria-label="选择查看 ${esc(item.label)}">
    <div class="pc-media">${media}</div>
    <div class="pc-label" title="${esc(item.label)}">${esc(item.label)}</div>
    ${state}
    ${item.model ? `<div class="pc-model" title="实际记录的模型/托管通道">${esc(item.model)}</div>` : ""}
    <details class="plan-prompt"><summary>提示词</summary>
      <pre>${esc(item.prompt || "")}</pre></details>
    ${planQcIssuesHtml(item)}
    ${planQcReferenceGalleryHtml(item)}
    ${planTraceHtml(item)}
  </div>`;
}

/* 生产画布:人物/场景/镜头节点 + 关系线(与出图提示词同源,牵引人物关联) */
/* 质检观察库:默认只保留证据，未经人工批准不进入其他镜头提示词。 */
function lessonsPanelHtml(data) {
  const lessons = data.lessons || [];
  if (!lessons.length) return "";
  return `<div class="lessons-panel">
    <h3>📋 质检观察库 <span class="dim">仅保留问题证据；默认不注入其他镜头，
      避免一次性修复累积成冲突的永久规则</span></h3>
    <ul>${lessons.slice(0, 8).map((item) =>
      `<li><b>×${item.count}</b> ${esc(item.issue)}
        <span class="rule-badge ${item.approved_for_prompt ? "live" : "adjustable"}">${item.approved_for_prompt ? "已人工批准为项目规则" : "待审核·不注入"}</span></li>`).join("")}</ul>
  </div>`;
}

function relationCanvasHtml(data) {
  const rel = data.relations;
  if (!rel || !(rel.nodes || []).length) return "";
  const items = ((data.render_plan || {}).items) || [];
  const shotStatus = {};
  items.forEach((i) => {
    const m = /^shot:(\d+)$/.exec(i.id || "");
    if (m) shotStatus[Number(m[1])] = i.status;
  });
  const chars = rel.nodes.filter((n) => n.type === "character");
  const scenes = rel.nodes.filter((n) => n.type === "scene");
  const shots = rel.nodes.filter((n) => n.type === "shot");
  const GAP = 46, TOP = 30;
  const colY = (list, idx) => TOP + idx * GAP
    + Math.max(0, (Math.max(chars.length, scenes.length, shots.length)
      - list.length) / 2) * GAP;
  const pos = {};
  chars.forEach((n, i) => { pos[n.id] = { x: 120, y: colY(chars, i) }; });
  scenes.forEach((n, i) => { pos[n.id] = { x: 360, y: colY(scenes, i) }; });
  shots.forEach((n, i) => { pos[n.id] = { x: 600, y: colY(shots, i) }; });
  const H = TOP + Math.max(chars.length, scenes.length, shots.length, 1) * GAP;
  const edgeEls = (rel.edges || []).map((e) => {
    const a = pos[e.from], b = pos[e.to];
    if (!a || !b) return "";
    const common = `class="rel-edge rel-${e.type}" data-from="${esc(e.from)}" data-to="${esc(e.to)}"`;
    if (e.type === "co_scene") {
      const bend = 70 + Math.abs(a.y - b.y) / 5;
      return `<g ${common}><path d="M ${a.x - 46} ${a.y} C ${a.x - bend} ${a.y}, ${b.x - bend} ${b.y}, ${b.x - 46} ${b.y}" fill="none"/>
        <text x="${a.x - bend + 6}" y="${(a.y + b.y) / 2 - 4}">${esc(e.label || "同场")}</text></g>`;
    }
    const x1 = a.x + 46, x2 = b.x - 46;
    return `<g ${common}><path d="M ${x1} ${a.y} C ${x1 + 60} ${a.y}, ${x2 - 60} ${b.y}, ${x2} ${b.y}" fill="none"/></g>`;
  }).join("");
  const nodeEl = (n) => {
    const p = pos[n.id];
    let cls = `rel-node rel-node-${n.type}`;
    let sub = "";
    if (n.type === "character") sub = n.role || "";
    if (n.type === "scene") sub = `第${n.scene_no}场`;
    if (n.type === "shot") {
      const st = shotStatus[n.shot_no] || "";
      cls += st ? ` st-${st}` : "";
      sub = { done: "✓完成", reused: "✓复用", generating: "生成中",
        failed: "✗失败" }[st] || (st ? st : "待产");
    }
    return `<g class="${cls}" data-node="${esc(n.id)}" transform="translate(${p.x},${p.y})">
      <rect x="-46" y="-16" width="92" height="32" rx="8"/>
      <text class="rel-label" y="${sub ? -2 : 4}">${esc(n.label)}</text>
      ${sub ? `<text class="rel-sub" y="12">${esc(sub)}</text>` : ""}</g>`;
  };
  return `<div class="relation-canvas">
    <h3>🧭 生产画布 <span class="dim">人物—场景—镜头关系线(点节点高亮它的线;
      出图与质检提示词自动携带同一份关系线,保持人物关联一致)</span></h3>
    <div class="rel-scroll"><svg viewBox="0 0 740 ${H}" width="740" height="${H}"
      class="rel-svg" role="img" aria-label="人物场景镜头关系画布">
      <text class="rel-col" x="120" y="14">人物</text>
      <text class="rel-col" x="360" y="14">场景</text>
      <text class="rel-col" x="600" y="14">镜头</text>
      ${edgeEls}
      ${rel.nodes.map(nodeEl).join("")}
    </svg></div></div>`;
}

/* 画布节点点击高亮(事件委托:看板轮询重绘也不丢绑定) */
document.addEventListener("click", (event) => {
  const node = event.target.closest(".rel-node");
  const svg = event.target.closest(".rel-svg");
  if (!svg) return;
  const id = node ? node.dataset.node : null;
  const active = node && svg.dataset.focus !== id;
  svg.dataset.focus = active ? id : "";
  svg.querySelectorAll(".rel-edge").forEach((edge) => {
    const hit = active
      && (edge.dataset.from === id || edge.dataset.to === id);
    edge.classList.toggle("hl", !!hit);
    edge.classList.toggle("dim-out", active && !hit);
  });
  svg.querySelectorAll(".rel-node").forEach((el) => {
    el.classList.toggle("hl", active && el.dataset.node === id);
  });
});

function renderPlanBoardHtml(data) {
  const items = ((data.render_plan || {}).items) || [];
  if (!items.length) return "";
  const ready = items.filter(
    (i) => ["done", "reused"].includes(i.status)).length;
  const pct = Math.round(ready / items.length * 100);
  const remainN = items.length - ready;
  const avgAll = planAvgDuration(items, null);
  const etaTotal = avgAll && remainN ? avgAll * remainN : null;
  const cats = PLAN_CATS
    .filter((c) => items.some((i) => i.category === c));
  return `<div class="plan-board">
    <div class="pb-head">
      <h2>🖼 图片生产实况</h2>
      <div class="pb-meta"><b>${ready}/${items.length}</b> 张完成 · ${pct}%
        ${etaTotal ? ` · 预计还需 ~${fmtDur(etaTotal)}`
          : (remainN ? " · 第一张完成后开始估算剩余时间" : "")}</div>
      <div class="pb-track"><div class="pb-fill" style="width:${pct}%"></div></div>
      <div class="pb-select-hint">点击任意图片卡片可选择并查看大图、状态和提示词</div>
    </div>
    ${mockWarnHtml(data)}
    ${lessonsPanelHtml(data)}
    ${relationCanvasHtml(data)}
    ${cats.map((cat) => {
      const list = items.filter((i) => i.category === cat);
      const ok = list.filter(
        (i) => ["done", "reused"].includes(i.status)).length;
      const avg = planAvgDuration(items, cat);
      return `<div class="pb-cat">
        <h3>${PLAN_CAT_CN[cat]} <span class="dim">${ok}/${list.length}</span></h3>
        <div class="pb-grid">${list.map(
          (i) => planCardHtml(data, i, avg)).join("")}</div></div>`;
    }).join("")}
  </div>`;
}

function imageAccelerationLivebarHtml(data) {
  const acceleration = (data.image_acceleration || {}).summary || {};
  const ready = Number(acceleration.ready || 0);
  const blocked = Number(acceleration.blocked || 0);
  const queued = Number(acceleration.queued || 0);
  const running = Number(acceleration.running || 0);
  if (ready + blocked + queued + running === 0) return "";
  return `<div class="image-accel-livebar ready">
    <div>
      <b>⚡ API 批量加速</b>
      <span>${ready
        ? `还有 ${ready} 张从未进入生产线；可自主选择 API 和模型，默认中等质量。`
        : `已排队 ${queued} 张 · 正在 API 加速 ${running} 张。`}</span>
      <small>放行前逐张核对实际提示词、参考图和人物身份；任何一项不一致都不会调用。</small>
    </div>
    <button id="btn-image-acceleration" class="primary">
      ${ready ? `选择 API/模型并加速 (${ready})` : "查看 API 加速状态"}
    </button>
  </div>`;
}

function bindImageAccelerationLivebar(episodeId) {
  const button = document.getElementById("btn-image-acceleration");
  if (button) button.onclick = () => showImageAcceleration(episodeId);
}

async function showPlanOverlay(episodeId, focusId = "") {
  let data;
  try { data = await api(`/api/episode/${episodeId}`); }
  catch (e) { showToast(e.message, "error"); return; }
  planOverlaySignatures.set(String(episodeId), canvasSig(data));
  const overlay = document.createElement("div");
  overlay.className = "script-overlay";
  overlay.innerHTML = `
    <div class="script-panel plan-overlay" data-episode="${episodeId}">
      <div class="script-head">
        <h3>🖼 图片生产清单 · 《${esc(data.project.title)}》第${data.episode.number}集</h3>
        <button class="close">关闭 Esc</button>
      </div>
      <div class="dim" style="margin:4px 0 10px">每张图的分类、状态与提示词都在这里;
        可勾选多张执行「批量优化修改」，系统会把每张质检原因自动编译进提示词；
        轻微问题也可单张或批量「人工通过」，原问题会保留在审计记录。
        镜头画面重画后会自动重做首尾帧并作废旧视频。</div>
      <div class="batch-job-progress" hidden></div>
      <div class="plan-overlay-content">${renderPlanHtml(data, true)
        || `<div class="dim">本集还没有图片生产计划(确认剧本、开始画图后就会出现)。</div>`}</div>
    </div>`;
  const close = () => {
    planOverlaySignatures.delete(String(episodeId));
    overlay.remove(); document.removeEventListener("keydown", onKey);
  };
  const onKey = (ev) => { if (ev.key === "Escape") close(); };
  overlay.addEventListener("click", (ev) => {
    if (ev.target === overlay) close();
  });
  overlay.querySelector(".close").onclick = close;
  document.addEventListener("keydown", onKey);
  document.body.appendChild(overlay);
  bindPlanSelection(overlay, data, episodeId);
  bindPlanRegen(overlay, episodeId, () => { close(); showPlanOverlay(episodeId); });
  if (focusId) {
    const target = [...overlay.querySelectorAll("[data-plan-select]")]
      .find((card) => card.dataset.planSelect === focusId);
    if (target) {
      target.scrollIntoView({ behavior: "smooth", block: "center" });
      target.classList.add("selected");
    }
  }
}

function blockingPoint(value) {
  const point = value && (value.point || value.position || value);
  if (!point || point.x == null || point.y == null) return null;
  return { x: point.x, y: point.y };
}

function blockingRoutePoints(entity) {
  const route = entity && entity.route;
  let candidates = [];
  if (Array.isArray(route)) candidates = route;
  else if (route && typeof route === "object") {
    candidates = [route.start, ...(route.points || []), route.end];
  }
  if (!candidates.filter(Boolean).length)
    candidates = [entity && entity.start, entity && entity.end];
  const result = candidates.map(blockingPoint).filter(Boolean);
  return result.filter((point, index) => !index
    || point.x !== result[index - 1].x || point.y !== result[index - 1].y);
}

function blockingPointText(point) {
  return point ? `(${point.x},${point.y})` : "未标注";
}

function blockingRouteText(entity, labels = ["起点", "终点"]) {
  const route3d = Array.isArray(entity && entity.route_3d)
    ? entity.route_3d.filter((point) => point && point.x != null
      && point.y != null && point.z != null) : [];
  if (route3d.length) {
    const format = (point) => `(${Number(point.x).toFixed(1)},${Number(point.y).toFixed(1)},${Number(point.z).toFixed(1)})m`;
    if (route3d.length === 1) return `原地 ${format(route3d[0])}`;
    return `${labels[0]} ${format(route3d[0])} → ${labels[1]} ${format(route3d[route3d.length - 1])}`;
  }
  const points = blockingRoutePoints(entity || {});
  if (!points.length) return "路线待生成";
  if (points.length === 1) return `原地 ${blockingPointText(points[0])}`;
  return `${labels[0]} ${blockingPointText(points[0])} → ${labels[1]} ${blockingPointText(points[points.length - 1])}`;
}

function blockingSafeColor(value) {
  return /^#[0-9a-f]{3,8}$/i.test(String(value || "")) ? value : "#94a3b8";
}

function blockingCharacterMapRows(blocking) {
  const map = blocking.character_number_map || {};
  if (Array.isArray(map)) return map;
  return Object.entries(map).map(([name, value]) => typeof value === "string"
    ? { name, actor_id: value }
    : { name, ...(value || {}) });
}

function blockingSceneActors(scene, blocking) {
  const declared = scene.actors || scene.actor_legend || scene.character_legend
    || (scene.legend && scene.legend.actors) || [];
  const fromShots = (scene.shots || []).flatMap((shot) => shot.actors || []);
  const mapped = blockingCharacterMapRows(blocking);
  const all = [...declared, ...fromShots, ...mapped];
  const sceneNames = new Set(fromShots.map((actor) => actor.name).filter(Boolean));
  const rows = [];
  const seen = new Set();
  all.forEach((raw) => {
    const actor = typeof raw === "string" ? { name: raw } : raw || {};
    if (sceneNames.size && !sceneNames.has(actor.name)
        && !fromShots.some((row) => row.actor_id && row.actor_id === actor.actor_id)) return;
    const key = actor.actor_id || actor.id || actor.name || actor.display_label;
    if (!key || seen.has(key)) return;
    seen.add(key);
    const shotActor = fromShots.find((row) => (actor.actor_id && row.actor_id === actor.actor_id)
      || (actor.name && row.name === actor.name)) || {};
    const mappedActor = mapped.find((row) => (actor.actor_id && row.actor_id === actor.actor_id)
      || (actor.name && row.name === actor.name)) || {};
    rows.push({ ...mappedActor, ...actor, ...shotActor,
      display_label: shotActor.display_label || actor.display_label || mappedActor.display_label });
  });
  return rows;
}

function blockingActorLabel(actor) {
  if (actor.display_label) return actor.display_label;
  const identity = actor.role && actor.name ? `${actor.role}·${actor.name}` : actor.name;
  return [actor.actor_id || actor.id, identity].filter(Boolean).join(" ") || "未编号人物";
}

function blockingLegendHtml(scene, blocking) {
  const actors = blockingSceneActors(scene, blocking);
  return `<div class="blocking-legend" aria-label="空间调度图例">
    <div class="blocking-route-key">
      <span><i class="route-symbol actor"></i>彩色实线：人物路线</span>
      <span><i class="route-symbol camera"></i>蓝色虚线：镜头路线</span>
      <span>○ / △ 起点</span><span>● / ▲ 终点</span>
    </div>
    ${actors.length ? `<div class="blocking-actor-legend" aria-label="人物编号图例">
      ${actors.map((actor) => `<span style="--actor-color:${blockingSafeColor(actor.color)}">
        <i></i><b>${esc(blockingActorLabel(actor))}</b></span>`).join("")}
    </div>` : `<div class="blocking-legend-empty">人物编号会在空间调度生成后显示</div>`}
  </div>`;
}

function blocking3dSceneHtml(scene, sceneIndex) {
  const shots = scene.shots || [];
  return `<div class="blocking-3d-stage" data-blocking-scene="${sceneIndex}">
    <div class="blocking-3d-toolbar">
      <div class="blocking-3d-shot-tabs" role="tablist" aria-label="选择镜头">
        ${shots.map((shot, index) => `<button type="button" role="tab"
          class="${index === 0 ? "active" : ""}" data-shot-index="${index}"
          aria-selected="${index === 0}">S${esc(shot.shot_no)}</button>`).join("")}
      </div>
      <div class="blocking-3d-views" aria-label="切换3D视角">
        <button type="button" class="active" data-view="orbit">透视</button>
        <button type="button" data-view="top">俯视</button>
        <button type="button" data-view="camera">机位方向</button>
        <button type="button" data-view="reset" aria-label="重置3D视角">重置</button>
      </div>
    </div>
    <canvas class="blocking-3d-canvas" tabindex="0" role="img"
      aria-label="第${esc(scene.scene_no)}场三维空间调度；可拖拽旋转、滚轮缩放"></canvas>
    <div class="blocking-3d-hint">
      拖拽旋转 · Shift/右键拖拽平移 · 滚轮缩放 · 彩色立柱=人物 · 蓝色棱锥=机位视锥
    </div>
  </div>`;
}

function blockingWorldPoint(point3d, point2d, height = 0) {
  if (point3d && ["x", "y", "z"].every((axis) => Number.isFinite(Number(point3d[axis])))) {
    return { x: Number(point3d.x), y: Number(point3d.y), z: Number(point3d.z) };
  }
  const point = point2d || { x: 500, y: 350 };
  const x = Number(point.x);
  const y = Number(point.y);
  return {
    x: ((Number.isFinite(x) ? x : 500) - 500) / 820 * 10,
    y: height,
    z: ((Number.isFinite(y) ? y : 350) - 350) / 470 * 7,
  };
}

function mountBlocking3d(stage, scene) {
  const canvas = stage.querySelector(".blocking-3d-canvas");
  const shots = scene.shots || [];
  if (!canvas || !shots.length) return () => {};
  const ctx = canvas.getContext("2d");
  if (!ctx) return () => {};
  const state = {
    shotIndex: 0, yaw: -.72, pitch: .58, zoom: 1,
    panX: 0, panY: 12, dragging: false, panning: false,
    pointerX: 0, pointerY: 0,
  };
  let logicalWidth = 0;
  let logicalHeight = 0;
  let frame = 0;

  const project = (point) => {
    const cosY = Math.cos(state.yaw);
    const sinY = Math.sin(state.yaw);
    const cosP = Math.cos(state.pitch);
    const sinP = Math.sin(state.pitch);
    const rx = point.x * cosY - point.z * sinY;
    const rz = point.x * sinY + point.z * cosY;
    const scale = Math.min(logicalWidth / 13, logicalHeight / 8.5) * state.zoom;
    return {
      x: logicalWidth / 2 + state.panX + rx * scale,
      y: logicalHeight * .57 + state.panY
        - (point.y * cosP - rz * sinP) * scale,
      depth: point.y * sinP + rz * cosP,
    };
  };
  const line = (from, to, color, width = 1, dash = []) => {
    const a = project(from);
    const b = project(to);
    ctx.beginPath();
    ctx.setLineDash(dash);
    ctx.moveTo(a.x, a.y);
    ctx.lineTo(b.x, b.y);
    ctx.strokeStyle = color;
    ctx.lineWidth = width;
    ctx.stroke();
    ctx.setLineDash([]);
    return { a, b };
  };
  const arrow = (from, to, color, width = 3, dash = []) => {
    const projected = line(from, to, color, width, dash);
    const angle = Math.atan2(
      projected.b.y - projected.a.y, projected.b.x - projected.a.x);
    ctx.beginPath();
    ctx.moveTo(projected.b.x, projected.b.y);
    ctx.lineTo(
      projected.b.x - 10 * Math.cos(angle - .45),
      projected.b.y - 10 * Math.sin(angle - .45));
    ctx.lineTo(
      projected.b.x - 10 * Math.cos(angle + .45),
      projected.b.y - 10 * Math.sin(angle + .45));
    ctx.closePath();
    ctx.fillStyle = color;
    ctx.fill();
  };
  const dot = (point, radius, fill, stroke = "", width = 1) => {
    const p = project(point);
    ctx.beginPath();
    ctx.arc(p.x, p.y, radius, 0, Math.PI * 2);
    ctx.fillStyle = fill;
    ctx.fill();
    if (stroke) {
      ctx.strokeStyle = stroke;
      ctx.lineWidth = width;
      ctx.stroke();
    }
    return p;
  };
  const label = (text, point, color = "#e2e8f0", offsetY = -10) => {
    const p = project(point);
    ctx.font = "600 12px system-ui, sans-serif";
    ctx.textAlign = "center";
    ctx.textBaseline = "bottom";
    const width = Math.min(190, ctx.measureText(text).width + 12);
    ctx.fillStyle = "rgba(10,16,28,.82)";
    ctx.fillRect(p.x - width / 2, p.y + offsetY - 17, width, 19);
    ctx.fillStyle = color;
    ctx.fillText(text, p.x, p.y + offsetY - 2, width - 8);
  };
  const polygon = (points, fill, stroke) => {
    const projected = points.map(project);
    if (!projected.length) return;
    ctx.beginPath();
    ctx.moveTo(projected[0].x, projected[0].y);
    projected.slice(1).forEach((point) => ctx.lineTo(point.x, point.y));
    ctx.closePath();
    ctx.fillStyle = fill;
    ctx.fill();
    ctx.strokeStyle = stroke;
    ctx.lineWidth = 1.2;
    ctx.stroke();
  };
  const render = () => {
    frame = 0;
    ctx.clearRect(0, 0, logicalWidth, logicalHeight);
    const background = ctx.createLinearGradient(0, 0, 0, logicalHeight);
    background.addColorStop(0, "#101b2c");
    background.addColorStop(1, "#080d16");
    ctx.fillStyle = background;
    ctx.fillRect(0, 0, logicalWidth, logicalHeight);

    const floor = [
      { x: -5, y: 0, z: -3.5 }, { x: 5, y: 0, z: -3.5 },
      { x: 5, y: 0, z: 3.5 }, { x: -5, y: 0, z: 3.5 },
    ];
    polygon(floor, "rgba(21,39,61,.86)", "#52627a");
    for (let x = -5; x <= 5; x += 1)
      line({ x, y: 0, z: -3.5 }, { x, y: 0, z: 3.5 }, "#2c405a", .7);
    for (let z = -3.5; z <= 3.5; z += .5)
      line({ x: -5, y: 0, z }, { x: 5, y: 0, z }, "#263b54", .7);
    line({ x: 0, y: 0, z: 0 }, { x: 1.3, y: 0, z: 0 }, "#f87171", 2);
    line({ x: 0, y: 0, z: 0 }, { x: 0, y: 1.3, z: 0 }, "#4ade80", 2);
    line({ x: 0, y: 0, z: 0 }, { x: 0, y: 0, z: 1.3 }, "#60a5fa", 2);

    const shot = shots[state.shotIndex] || shots[0];
    const camera = shot.camera || {};
    const cameraStart = blockingWorldPoint(
      camera.start_3d, camera.start, 1.55);
    const cameraEnd = blockingWorldPoint(
      camera.end_3d, camera.end, 1.55);
    const cameraTarget = blockingWorldPoint(
      camera.target_3d, camera.target, 1.25);
    polygon([
      cameraEnd,
      { x: cameraTarget.x - 1.15, y: 0, z: cameraTarget.z },
      { x: cameraTarget.x + 1.15, y: 0, z: cameraTarget.z },
      { x: cameraTarget.x + 1.15, y: 2.45, z: cameraTarget.z },
      { x: cameraTarget.x - 1.15, y: 2.45, z: cameraTarget.z },
    ], "rgba(56,189,248,.1)", "rgba(56,189,248,.72)");
    line(cameraEnd, cameraTarget, "#7dd3fc", 1.5, [6, 5]);
    if (camera.moving || cameraStart.x !== cameraEnd.x
        || cameraStart.y !== cameraEnd.y || cameraStart.z !== cameraEnd.z) {
      arrow(cameraStart, cameraEnd, "#38bdf8", 3);
      dot(cameraStart, 7, "#0f172a", "#38bdf8", 2);
    }
    const cameraDot = dot(cameraEnd, 9, "#38bdf8", "#e0f2fe", 2);
    ctx.save();
    ctx.translate(cameraDot.x, cameraDot.y);
    ctx.rotate(Math.PI / 4);
    ctx.strokeStyle = "#e0f2fe";
    ctx.lineWidth = 2;
    ctx.strokeRect(-7, -7, 14, 14);
    ctx.restore();
    label(`C${shot.shot_no} · ${camera.lens_mm || "-"}mm`, cameraEnd, "#bae6fd", -13);

    const actors = (shot.actors || []).map((actor) => {
      const start = blockingWorldPoint(actor.start_3d, actor.start);
      const end = blockingWorldPoint(actor.end_3d, actor.end);
      return { actor, start, end, depth: project(end).depth };
    }).sort((left, right) => left.depth - right.depth);
    actors.forEach(({ actor, start, end }) => {
      const color = blockingSafeColor(actor.color);
      const height = Number(actor.height_m) || 1.68;
      if (actor.moving) {
        arrow(start, end, color, 4);
        line(start, { ...start, y: height }, color, 3, [4, 4]);
        dot({ ...start, y: height }, 6, "#0b1220", color, 2);
      }
      line(end, { ...end, y: height }, color, 7);
      dot(end, 6, color, "#f8fafc", 1.5);
      dot({ ...end, y: height }, 8, color, "#f8fafc", 1.5);
      label(blockingActorLabel(actor), { ...end, y: height }, color, -12);
    });

    ctx.textAlign = "left";
    ctx.textBaseline = "top";
    ctx.font = "600 12px system-ui, sans-serif";
    ctx.fillStyle = "#cbd5e1";
    ctx.fillText(
      `S${shot.shot_no} · ${shot.character_count == null ? actors.length : shot.character_count}人 · ${camera.movement || "固定"} · XYZ 米制`,
      14, 12);
  };
  const requestRender = () => {
    if (frame) cancelAnimationFrame(frame);
    frame = requestAnimationFrame(render);
  };
  const resize = () => {
    const rect = canvas.getBoundingClientRect();
    logicalWidth = Math.max(320, Math.round(rect.width));
    logicalHeight = Math.max(300, Math.round(rect.height));
    const dpr = Math.min(2, window.devicePixelRatio || 1);
    canvas.width = Math.round(logicalWidth * dpr);
    canvas.height = Math.round(logicalHeight * dpr);
    ctx.setTransform(dpr, 0, 0, dpr, 0, 0);
    requestRender();
  };
  const applyView = (view) => {
    if (view === "top") {
      state.yaw = 0;
      state.pitch = 1.5;
      state.panX = 0;
      state.panY = 5;
    } else if (view === "camera") {
      const shot = shots[state.shotIndex] || shots[0];
      const camera = shot.camera || {};
      const from = blockingWorldPoint(camera.end_3d, camera.end, 1.55);
      const target = blockingWorldPoint(camera.target_3d, camera.target, 1.25);
      state.yaw = -Math.atan2(target.x - from.x, target.z - from.z);
      state.pitch = .32;
      state.panX = 0;
      state.panY = 12;
    } else {
      state.yaw = -.72;
      state.pitch = .58;
      state.panX = 0;
      state.panY = 12;
      state.zoom = 1;
    }
    stage.querySelectorAll("[data-view]").forEach((button) =>
      button.classList.toggle("active", button.dataset.view === view));
    requestRender();
  };
  stage.querySelectorAll("[data-shot-index]").forEach((button) => {
    button.addEventListener("click", () => {
      state.shotIndex = Number(button.dataset.shotIndex) || 0;
      stage.querySelectorAll("[data-shot-index]").forEach((candidate) => {
        const active = candidate === button;
        candidate.classList.toggle("active", active);
        candidate.setAttribute("aria-selected", String(active));
      });
      requestRender();
    });
  });
  stage.querySelectorAll("[data-view]").forEach((button) => {
    button.addEventListener("click", () =>
      applyView(button.dataset.view === "reset" ? "orbit" : button.dataset.view));
  });
  const onPointerDown = (event) => {
    state.dragging = true;
    state.panning = event.shiftKey || event.button === 2;
    state.pointerX = event.clientX;
    state.pointerY = event.clientY;
    canvas.setPointerCapture(event.pointerId);
  };
  const onPointerMove = (event) => {
    if (!state.dragging) return;
    const dx = event.clientX - state.pointerX;
    const dy = event.clientY - state.pointerY;
    state.pointerX = event.clientX;
    state.pointerY = event.clientY;
    if (state.panning) {
      state.panX += dx;
      state.panY += dy;
    } else {
      state.yaw += dx * .009;
      state.pitch = Math.max(.12, Math.min(1.52, state.pitch + dy * .007));
      stage.querySelectorAll("[data-view]").forEach((button) =>
        button.classList.toggle("active", button.dataset.view === "orbit"));
    }
    requestRender();
  };
  const onPointerUp = (event) => {
    state.dragging = false;
    if (canvas.hasPointerCapture(event.pointerId))
      canvas.releasePointerCapture(event.pointerId);
  };
  const onWheel = (event) => {
    event.preventDefault();
    state.zoom = Math.max(.45, Math.min(2.8, state.zoom * Math.exp(-event.deltaY * .001)));
    requestRender();
  };
  const onKey = (event) => {
    if (event.key === "ArrowLeft") state.yaw -= .12;
    else if (event.key === "ArrowRight") state.yaw += .12;
    else if (event.key === "ArrowUp") state.pitch = Math.max(.12, state.pitch - .1);
    else if (event.key === "ArrowDown") state.pitch = Math.min(1.52, state.pitch + .1);
    else if (event.key === "+" || event.key === "=") state.zoom = Math.min(2.8, state.zoom * 1.12);
    else if (event.key === "-") state.zoom = Math.max(.45, state.zoom / 1.12);
    else if (event.key === "0") applyView("orbit");
    else return;
    event.preventDefault();
    requestRender();
  };
  canvas.addEventListener("pointerdown", onPointerDown);
  canvas.addEventListener("pointermove", onPointerMove);
  canvas.addEventListener("pointerup", onPointerUp);
  canvas.addEventListener("pointercancel", onPointerUp);
  canvas.addEventListener("wheel", onWheel, { passive: false });
  canvas.addEventListener("keydown", onKey);
  canvas.addEventListener("contextmenu", (event) => event.preventDefault());
  canvas.addEventListener("dblclick", () => applyView("orbit"));
  window.addEventListener("resize", resize);
  resize();
  return () => {
    if (frame) cancelAnimationFrame(frame);
    window.removeEventListener("resize", resize);
  };
}

function blockingShotHtml(shot) {
  const actors = shot.actors || [];
  const actorRoutes = actors.length ? actors.map((actor) => `
    <div class="blocking-route actor" style="--actor-color:${blockingSafeColor(actor.color)}">
      <i></i><b>${esc(blockingActorLabel(actor))}</b>
      <span>${esc(blockingRouteText(actor))}${actor.route_label ? ` · ${esc(actor.route_label)}` : ""}</span>
    </div>`).join("") : `<div class="blocking-route empty">本镜为空镜</div>`;
  const camera = shot.camera || {};
  const cameraMove = camera.direction_label || camera.movement
    || (camera.moving ? "移动镜头" : "固定镜头");
  return `<article class="blocking-shot">
    <div class="blocking-shot-head"><b>S${esc(shot.shot_no)}</b>
      <span>${esc(shot.character_count == null ? actors.length : shot.character_count)} 人</span></div>
    <div class="blocking-routes">${actorRoutes}
      <div class="blocking-route camera"><i></i><b>🎥 镜头</b>
        <span>${esc(blockingRouteText(camera, ["机位起点", "机位终点"]))}</span>
        <em>${esc(camera.lens_mm || "-")}mm · ${esc(cameraMove)} · ${esc(camera.position || "-")}</em>
      </div>
    </div>
  </article>`;
}

async function showBlockingOverlay(episodeId) {
  let data;
  try { data = await api(`/api/episode/${episodeId}`); }
  catch (e) { showToast(e.message, "error"); return; }
  const blocking = data.blocking || {};
  const scenes = blocking.scenes || [];
  const overlay = document.createElement("div");
  overlay.className = "script-overlay";
  const sceneHtml = scenes.map((scene, sceneIndex) => {
    const svgUrl = scene.svg_url || scene.map_url || scene.svg || "";
    return `
    <section class="blocking-scene">
      <div class="blocking-scene-head">
        <div><h3>第 ${scene.scene_no} 场 · ${esc(scene.location || "未命名场景")}</h3>
          <span class="dim">${esc((scene.reasons || []).join(" · "))}</span></div>
        <span class="${scene.required ? "warn" : "pass"}">${scene.required ? "重点调度" : "连续性参考"}</span>
      </div>
      ${blockingLegendHtml(scene, blocking)}
      ${blocking3dSceneHtml(scene, sceneIndex)}
      ${svgUrl
        ? `<details class="blocking-fixed-reference"><summary>查看给关键帧 / Seedance 使用的固定 3D 参考图</summary>
          <div class="blocking-map-scroll"><button class="blocking-map-btn" data-map="${esc(svgUrl)}" data-label="第${scene.scene_no}场固定3D空间参考图"><img src="${esc(svgUrl)}" loading="lazy" alt="第${scene.scene_no}场固定3D空间参考图，点击放大"></button></div>
          <div class="blocking-map-mobile-hint">↔ 左右滑动查看全图 · 点图放大</div></details>`
        : `<div class="blocking-empty">固定 3D 参考图尚未生成</div>`}
      <div class="blocking-shot-list">${(scene.shots || []).map(blockingShotHtml).join("")}</div>
    </section>`;
  }).join("");
  overlay.innerHTML = `
    <div class="script-panel blocking-overlay">
      <div class="script-head">
        <h3>🧭 空间调度图 · 《${esc(data.project.title)}》第${data.episode.number}集</h3>
        <button class="close">关闭 Esc</button>
      </div>
      <div class="blocking-summary">
        <b>${blocking.summary?.scenes || 0}</b> 场 ·
        <b>${blocking.summary?.shots || 0}</b> 镜 ·
        <b>${blocking.summary?.required_scenes || 0}</b> 个重点调度场景
        <span class="${blocking.validation?.passed ? "pass" : "warn"}">${blocking.validation?.passed ? "空间门禁 PASS" : "空间门禁待生成"}</span>
      </div>
      <p class="dim">交互 3D 与模型参考图共用同一套米制坐标；人物站位、路线、机位高度、瞄准点和视锥只约束构图，不会画进最终画面。</p>
      ${sceneHtml || `<div class="blocking-empty">本集还没有空间调度图；确认剧本并完成五维分镜后会自动生成。</div>`}
    </div>`;
  const cleanups = [];
  const close = () => {
    cleanups.forEach((cleanup) => cleanup());
    overlay.remove();
    document.removeEventListener("keydown", onKey);
  };
  const onKey = (ev) => { if (ev.key === "Escape") close(); };
  overlay.addEventListener("click", (ev) => { if (ev.target === overlay) close(); });
  overlay.querySelector(".close").onclick = close;
  overlay.querySelectorAll(".blocking-map-btn").forEach((button) => {
    button.onclick = () => showImageLightbox(
      button.dataset.map, button.dataset.label, "blocking-map-lightbox");
  });
  document.addEventListener("keydown", onKey);
  document.body.appendChild(overlay);
  overlay.querySelectorAll("[data-blocking-scene]").forEach((stage) => {
    const scene = scenes[Number(stage.dataset.blockingScene)];
    if (scene) cleanups.push(mountBlocking3d(stage, scene));
  });
}

/* ================= 资产中心 =================
   人物资产套件/场景概念图/参考图:浏览、上传参考、替换、单张重画 */
const DESIGN_LABELS_JS = [
  ["personality", "性格"], ["temperament", "气质"], ["appearance", "外貌"],
  ["hair", "发型"], ["eyes", "眼睛"], ["makeup", "妆容"],
  ["costume", "服装"], ["costume_detail", "服装细节"],
  ["accessories", "配饰"], ["palette", "配色"], ["signature", "标志特征"],
  ["background_prompt", "人物背景提示词"], ["era_setting", "时代/世界观"],
  ["image_prompt", "最终人物出图提示词"],
  ["negative_prompt", "人物负面提示词"],
  ["occupation", "职业身份"], ["motivation", "核心动机"],
  ["backstory", "人物经历"], ["relationships", "人物关系"],
  ["costume_direction", "服装设计逻辑"], ["signature_props", "标志道具"],
  ["visual_variants", "剧情造型方案"]];

function designValueText(value) {
  if (value == null) return "";
  if (Array.isArray(value) && !value.length) return "";
  if (typeof value === "object" && !Array.isArray(value)
      && !Object.keys(value).length) return "";
  return typeof value === "string" ? value : JSON.stringify(value);
}

function designHtml(design) {
  if (!design) return "";
  const rows = DESIGN_LABELS_JS
    .map(([key, label]) => [key, label, designValueText(design[key])])
    .filter(([, , value]) => value.trim())
    .map(([key, label]) =>
      `<div class="design-row"><b>${label}</b><span>${esc(designValueText(design[key]))}</span></div>`);
  if (!rows.length) return "";
  const brief = [designValueText(design.personality),
    designValueText(design.temperament), designValueText(design.era_setting)]
    .filter(Boolean).join(" · ");
  return `<details class="design-box">
    <summary>🧬 人物设定${brief ? ` · ${esc(brief)}` : ""}(点开看全部,出图提示词按它生成)</summary>
    <div class="design-grid">${rows.join("")}</div></details>`;
}

function characterProfileHtml(character) {
  if (!character) return "";
  const role = String(character.role || "");
  const cueLikeName = /(地|着|嗓子|回话|垂手|好奇|冷声|温声|沉声|低声)$/
    .test(String(character.name || ""));
  if (role.includes("待确认") || character.name === "待确认说话人"
      || character.asset_policy === "unresolved_no_generation"
      || cueLikeName) {
    return `<div class="background-cast-note unresolved-cast-note">
      ⚠️ ${esc(character.name)} · 尚未确认真实人物。
      系统不会为它生成图片；请先用「AI 重新分析」或直接编辑剧本确认说话人。</div>`;
  }
  if (["背景路人", "背景人物", "背景群众", "群众演员", "群演",
       "跑龙套", "龙套", "临时路人", "路人角色", "路人"]
      .some((token) => role.includes(token))) {
    return `<div class="background-cast-note">🎭 ${esc(character.name)}
      · ${esc(role || "背景路人")} · 仅按场次人数与功能受控，
      不建立独立人物设定，不生成候选图、立绘或四视图</div>`;
  }
  const labels = [
    ["introduction", "人物介绍"], ["gender", "性别"],
    ["age_range", "年龄段"], ["identity", "身份/阵营"],
    ["personality", "性格"],
    ["background_prompt", "人物背景提示词"], ["era_setting", "时代/世界观"],
    ["image_prompt", "最终人物出图提示词"],
    ["negative_prompt", "人物负面提示词"],
    ["occupation", "职业身份"], ["motivation", "核心动机"],
    ["backstory", "人物经历"], ["relationships", "人物关系"],
    ["costume_direction", "服装设计逻辑"], ["signature_props", "标志道具"],
    ["visual_variants", "剧情造型方案"]];
  const rows = labels.map(([key, label]) =>
    `<div class="design-row"><b>${label}</b><span>${esc(designValueText(character[key]))}</span></div>`)
    .filter((row, index) => {
      const key = labels[index][0];
      return designValueText(character[key]).trim();
    });
  if (!rows.length) return "";
  const summary = [character.name, character.role, character.era_setting,
    character.occupation].map(designValueText).filter(Boolean).join(" · ");
  return `<details class="design-box script-profile-box">
    <summary>📚 ${esc(summary)} · 剧情人物背景与造型提示</summary>
    <div class="design-grid">${rows.join("")}</div></details>`;
}

function storyBibleHtml(script) {
  const world = script?.story_world || {};
  const background = script?.story_background || {};
  const worldLabels = [
    ["name", "世界名称"], ["overview", "世界概述"],
    ["era_and_location", "时代地域"], ["social_order", "社会/组织秩序"],
    ["hard_rules", "世界硬规则"], ["visual_baseline", "视觉基准"],
    ["forbidden_drift", "禁止漂移"]];
  const backgroundLabels = [
    ["prior_events", "前情"], ["current_situation", "当前局势"],
    ["core_conflict", "核心冲突"], ["episode_goal", "本集目标"],
    ["continuity_hooks", "前后集衔接"]];
  const rows = (source, labels) => labels
    .filter(([key]) => designValueText(source[key]).trim())
    .map(([key, label]) =>
      `<div class="story-bible-row"><b>${label}</b><span>${esc(designValueText(source[key]))}</span></div>`)
    .join("");
  const worldRows = rows(world, worldLabels);
  const backgroundRows = rows(background, backgroundLabels);
  if (!worldRows && !backgroundRows) return "";
  return `<section class="story-bible" aria-label="故事世界与背景设定">
    <div class="story-bible-title"><b>🌐 故事世界与背景圣经</b>
      <span>后续人物、分镜和镜头统一以此为准</span></div>
    <div class="story-bible-columns">
      <div><h3>故事世界</h3>${worldRows}</div>
      <div><h3>故事背景</h3>${backgroundRows}</div>
    </div>
  </section>`;
}

/* 图片点击放大(资产中心/参考图通用灯箱) */
function showImageLightbox(url, label, variant = "") {
  if (!url) return;
  const overlay = document.createElement("div");
  overlay.className = `script-overlay img-lightbox ${variant}`.trim();
  overlay.innerHTML = `
    <figure class="lightbox-box">
      <img src="${esc(url)}" alt="${esc(label || "")}">
      ${label ? `<figcaption>${esc(label)}</figcaption>` : ""}
      <button class="close">关闭 Esc</button>
    </figure>`;
  const close = () => {
    overlay.remove(); document.removeEventListener("keydown", onKey);
  };
  const onKey = (ev) => { if (ev.key === "Escape") close(); };
  overlay.addEventListener("click", (ev) => {
    if (ev.target === overlay) close();
  });
  overlay.querySelector(".close").onclick = close;
  document.addEventListener("keydown", onKey);
  document.body.appendChild(overlay);
}

function bindLightbox(container) {
  container.querySelectorAll(".plan-card .pc-media img").forEach((img) => {
    img.classList.add("zoomable");
  });
}

/* 全局委托:任何带 zoomable 的图片点击即放大(含生产预览大图) */
document.addEventListener("click", (ev) => {
  const img = ev.target.closest("img.zoomable");
  if (!img || img.closest(".img-lightbox")) return;
  const card = img.closest(".plan-card");
  const label = (card && (card.querySelector(".pc-label") || {}).textContent)
    || img.alt || "";
  // 灯箱看原图:去掉缩略图参数
  const full = img.src.replace(/([?&])w=\d+&?/, "$1").replace(/[?&]$/, "");
  showImageLightbox(full, String(label).trim());
});
function assetCardHtml(ep, target, url, label, mock, assetId = null) {
  const img = url && !url.split("?")[0].endsWith(".json")
    ? `<img src="${esc(thumbUrl(url, 480))}" loading="lazy" alt="">`
    : `<div class="pc-empty">🖼</div>`;
  const safe = `${target.kind}_${(target.name || "").replace(/[^\w:]/g, "_")}`;
  return `<div class="plan-card asset-card">
    <div class="pc-media">${img}</div>
    <div class="pc-label" title="${esc(label)}">${esc(label)}
      ${mock ? `<span class="plan-st st-mock" title="占位产线画的示意图,不理解修改意见;接入真实出图产线后重画才会按意见改样式">⚠ 占位图</span>` : ""}</div>
    ${ep ? `${regenControls(target, "重画")}
            ${ioControls(target, url || "", safe + ".png")}` : ""}
    ${assetId ? `<button class="asset-del danger" data-asset-id="${assetId}"
      title="从资产中心隐藏当前版本；历史版本和原文件仍会保留">🗑 删除</button>` : ""}
  </div>`;
}

const ASSET_BOARD_GROUPS = [
  { key: "production", label: "主生产资产", hint: "会直接进入镜头、首尾帧或视频" },
  { key: "character_support", label: "人物辅助设定", hint: "四视图、服装、妆容和细节参考" },
  { key: "candidate", label: "候选与历史", hint: "未定版候选只用于挑选，不进入镜头" },
  { key: "reference", label: "上传参考图", hint: "按角色或场景关联调用" },
  { key: "other", label: "其他资产", hint: "暂未归入生产链的资产" },
];

function assetBoardGroupKey(item) {
  if (item.board_group) return item.board_group;
  if (["character_art", "scene_art", "image", "first_frame",
    "last_frame", "cover"].includes(item.kind)) return "production";
  if (item.kind === "character_sheet") return "character_support";
  if (item.kind === "character_candidate") return "candidate";
  if (item.kind === "reference") return "reference";
  return "other";
}

function assetBoardGroupLabel(item) {
  return item.board_group_label
    || (ASSET_BOARD_GROUPS.find((g) => g.key === assetBoardGroupKey(item)) || {}).label
    || "其他资产";
}

function assetCatalogCardHtml(item) {
  const source = `《${item.source_project || "未命名作品"}》`
    + (item.source_episode ? ` · 第 ${item.source_episode} 集` : " · 项目公共资产");
  const group = assetBoardGroupKey(item);
  return `<article class="asset-catalog-card" data-category="${esc(item.category)}" data-group="${esc(group)}">
    <div class="asset-catalog-media"><img class="zoomable"
      src="${esc(thumbUrl(item.url, 480))}" loading="lazy" alt="${esc(item.label)}"></div>
    <div class="asset-catalog-head"><span class="asset-category-chip">${esc(item.category_label)}</span>
      <span class="asset-usage-chip ${item.selected ? "selected" : ""}">${esc(item.usage_label || assetBoardGroupLabel(item))}</span>
      <b>${esc(item.label)}</b></div>
    <dl class="asset-origin-meta">
      <div><dt>来源作品</dt><dd>${esc(source)}</dd></div>
      <div><dt>生成时间</dt><dd>${dateTime(item.generated_at)}</dd></div>
      <div><dt>质量 / 版本</dt><dd>${esc(item.quality || "medium")} · v${item.version}</dd></div>
    </dl>
    <details class="asset-prompt"><summary>查看提示词</summary>
      <p class="${item.prompt_status === "recorded" ? "" : "dim"}">${esc(item.prompt)}</p></details>
    <button class="asset-del danger" data-asset-id="${item.asset_id}"
      title="从资产中心隐藏当前版本；历史版本和原文件仍会保留">🗑 删除</button>
  </article>`;
}

function assetCatalogHtml(items, category = "all") {
  const visible = category === "all"
    ? items : items.filter((item) => item.category === category);
  return visible.length
    ? visible.map(assetCatalogCardHtml).join("")
    : `<div class="dim">当前分类还没有图片资产。</div>`;
}

function assetBoardHtml(items, category = "all", group = "all") {
  const visible = (items || []).filter((item) =>
    (category === "all" || item.category === category)
    && (group === "all" || assetBoardGroupKey(item) === group));
  if (!visible.length) return `<div class="dim asset-board-empty">当前筛选没有图片资产。</div>`;
  const groups = ASSET_BOARD_GROUPS.filter((g) =>
    visible.some((item) => assetBoardGroupKey(item) === g.key));
  return `<div class="asset-board-lanes">${groups.map((g) => {
    const lane = visible.filter((item) => assetBoardGroupKey(item) === g.key);
    return `<section class="asset-lane asset-lane-${g.key}" data-group="${g.key}">
      <div class="asset-lane-head"><div><h3>${g.label}
        <span>${lane.length}</span></h3><p>${g.hint}</p></div>
        <span class="asset-lane-count">${lane.filter((item) => item.usable_for_video).length} 可用于视频</span></div>
      <div class="asset-catalog-grid">${lane.map(assetCatalogCardHtml).join("")}</div>
    </section>`;
  }).join("")}</div>`;
}

async function renderAssetsCenter(selectedTitle) {
  topbarRight.innerHTML = "";
  let ov;
  try { ov = await api("/api/overview"); }
  catch (e) {
    app.innerHTML = `<div class="loading">加载失败:${esc(e.message)}</div>`;
    return;
  }
  const projects = ov.projects || [];
  if (!projects.length) {
    app.innerHTML = `<div class="loading">还没有项目。先在生产总览用一句话开始制作,
      剧本确认后可选择只用最终人物形象图，或生成完整人物资产套件。</div>`;
    return;
  }
  const stored = localStorage.getItem("aifos.assets.project");
  const title = selectedTitle
    || (projects.some((p) => p.title === stored) ? stored
        : projects[0].title);
  localStorage.setItem("aifos.assets.project", title);
  const eps = (ov.episodes || []).filter((e) => e.project === title);
  const ep = eps[0] || null;
  let epData = null;
  let art = { cast_art: [], scene_art: [], character_sheets: {},
              references: [] };
  if (ep) {
    try {
      epData = await api(`/api/episode/${ep.id}`);
      art = epData.artifacts || art;
    } catch (e) { /* 项目还没有产物 */ }
  }
  try {
    const catalog = await api(`/api/asset-images?project=${encodeURIComponent(title)}`);
    art.image_assets = catalog.items || [];
  } catch (e) { art.image_assets = art.image_assets || []; }
  // 占位图标注:该资产最后一次是占位产线画的 → 卡片红标提醒
  const mockIds = new Set(
    (((epData || {}).render_plan || {}).items || [])
      .filter(planIsMock).map((i) => i.id));
  const isMock = (id) => mockIds.has(id);
  const attachOptions = [
    ...(art.cast_art || []).map((c) => c.name),
    ...(art.scene_art || []).map((s) => s.name)];
  const storedCategory = localStorage.getItem("aifos.assets.category") || "all";
  const assetCategories = [...new Map((art.image_assets || []).map((item) =>
    [item.category, item.category_label])).entries()];
  const activeCategory = storedCategory === "all"
    || assetCategories.some(([value]) => value === storedCategory)
    ? storedCategory : "all";
  const storedBoardGroup = localStorage.getItem("aifos.assets.board") || "all";
  const activeBoardGroup = storedBoardGroup === "all"
    || ASSET_BOARD_GROUPS.some((group) => group.key === storedBoardGroup)
    ? storedBoardGroup : "all";
  const boardItems = art.image_assets || [];
  const boardCounts = Object.fromEntries(ASSET_BOARD_GROUPS.map((group) => [
    group.key, boardItems.filter((item) => assetBoardGroupKey(item) === group.key).length,
  ]));
  const characterAssetPolicy = (epData || {}).character_asset_policy || {};
  const characterAssetHint = characterAssetPolicy.resolved_mode === "simple"
    ? "当前为简化版：只使用人工锁定的最终人物形象图；这是人工明确选择的三视图门禁豁免"
    : "当前为完整版：视觉DNA + 三视图审核板 + 独立面部/正面/严格侧面/完整背面母资产 + 细节图";
  app.innerHTML = `
  <div class="assets-center">
    <div class="canvas-toolbar">
      <span class="title">🗂 资产中心</span>
      <select id="asset-project">${projects.map((p) =>
        `<option ${p.title === title ? "selected" : ""}>${esc(p.title)}</option>`).join("")}</select>
      <span class="hint">人物资产范围可按集选择;最终人物形象图始终进入出图与质检</span>
    </div>
    <section class="panel asset-panel asset-catalog-panel">
      <div class="asset-catalog-toolbar"><div><h2>🧭 资产画布</h2>
        <p class="dim">先看会进入生产的资产，再展开人物设定、候选和上传参考；避免所有图片混在一列。</p></div>
        <label>资产分类·细分 <select id="asset-category"><option value="all">全部图片</option>
          ${assetCategories.map(([value, label]) => `<option value="${esc(value)}"
            ${activeCategory === value ? "selected" : ""}>${esc(label)}</option>`).join("")}
        </select></label></div>
      <div class="asset-board-summary">${ASSET_BOARD_GROUPS.filter((group) => boardCounts[group.key]).map((group) =>
        `<button class="asset-board-filter ${activeBoardGroup === group.key ? "active" : ""}"
          data-asset-board-group="${group.key}"><b>${group.label}</b><span>${boardCounts[group.key]}</span></button>`).join("")}
        <button class="asset-board-filter ${activeBoardGroup === "all" ? "active" : ""}"
          data-asset-board-group="all"><b>全部资产</b><span>${boardItems.length}</span></button>
      </div>
      <div class="asset-board" id="asset-catalog-board">
        ${assetBoardHtml(boardItems, activeCategory, activeBoardGroup)}
      </div>
    </section>
    ${epData ? mockWarnHtml(epData) : ""}
    <section class="panel asset-panel">
      <h2>🎨 画风管理 <span class="dim">更换画风后一键重做全部形象:
        主角立绘最先重做并成为新的风格基准,其余全部对齐它,保证统一</span></h2>
      <div class="style-row">
        <label>本剧画风</label>
        <input id="restyle-input" list="style-presets"
          value="${esc((projects.find((pp) => pp.title === title) || {}).style || "")}"
          placeholder="如:水墨国风,淡彩,留白构图">
        <datalist id="style-presets">${STYLE_PRESETS.map((x) =>
          `<option>${esc(x)}</option>`).join("")}</datalist>
        <button class="primary" id="btn-restyle" ${ep ? "" : "disabled"}>🎨 按此画风重做全部形象</button>
      </div>
      ${imageLineControlsHtml()}
      <div class="dim">重做会消耗出图额度(每张一次);过程在本集页面实况可见,
        可随时暂停,已完成的保留。分镜画面如需同步新画风,重做完成后到本集点「全部重做」。</div>
    </section>
    <section class="panel asset-panel">
      <h2>📎 参考图 <span class="dim">上传后,生成人物/场景/分镜画面时自动作为参考
        (可全项目通用,或只关联某个角色/场景)</span></h2>
      <div class="ref-form">
        <input id="ref-name" placeholder="参考图名称,如:女主官方设定">
        <select id="ref-role" aria-label="参考图用途">
          <option value="identity">人物身份（只锁脸/年龄/性别）</option>
          <option value="wardrobe">服装/道具（不覆盖脸）</option>
          <option value="scene">场景空间（忽略人物）</option>
          <option value="composition">构图/动作（不覆盖身份）</option>
          <option value="style">画风（可全项目使用）</option>
        </select>
        <input id="ref-attach" list="ref-attach-list"
          placeholder="关联角色/场景；仅画风可留空全局使用">
        <datalist id="ref-attach-list">${attachOptions.map((n) =>
          `<option>${esc(n)}</option>`).join("")}</datalist>
        <button class="primary" id="ref-upload">⬆ 上传参考图</button>
      </div>
      <div class="pb-grid ref-grid">${(art.references || []).map((r) => `
        <div class="plan-card ref-card">
          <div class="pc-media">${r.url ? `<img src="${esc(thumbUrl(r.url, 480))}" loading="lazy" alt="">` : `<div class="pc-empty">🖼</div>`}</div>
          <div class="pc-label" title="${esc(r.name)}">${esc(r.name)}</div>
          <div class="dim">用途:${esc({
            identity: "人物身份", wardrobe: "服装/道具", scene: "场景空间",
            composition: "构图/动作", style: "画风", manual: "仅手动"
          }[r.reference_role] || r.reference_role || "旧图待指定")}
            · ${r.attach_to ? "关联:" + esc(r.attach_to)
              : (r.reference_role === "style" ? "全项目画风" : "仅手动选择")}</div>
          <button class="ref-del" data-name="${esc(r.name)}">删除</button>
        </div>`).join("")
        || `<div class="dim">还没有参考图。上传后所有出图自动参考,人物形象更稳定。</div>`}
      </div>
    </section>
    <section class="panel asset-panel">
      <h2>👤 人物资产 <span class="dim">${esc(characterAssetHint)}；现有资产均可查看和下载</span></h2>
      ${(art.cast_art || []).length ? art.cast_art.map((c) => {
        const sheets = (art.character_sheets || {})[c.name] || [];
        return `<div class="char-suite">
          <h3>${esc(c.name)} <span class="dim">${esc(c.role || "")}</span></h3>
          ${designHtml(c.design)}
          <div class="pb-grid">
            ${assetCardHtml(ep, { kind: "character_art", name: c.name },
                            c.url, "立绘", isMock(`char:${c.name}`), c.asset_id)}
            ${sheets.map((s) => assetCardHtml(
              ep, { kind: "character_sheet", name: s.name }, s.url,
              s.label || s.sheet,
              isMock(`sheet:${c.name}:${s.sheet}`), s.asset_id)).join("")}
          </div></div>`;
      }).join("")
      : `<div class="dim">本项目还没有人物资产。开始制作一集并确认剧本后,
         可选择只生成最终人物形象图，或生成每个角色的完整扩展套件。</div>`}
    </section>
    <section class="panel asset-panel">
      <h2>🏞 场景概念图</h2>
      <div class="pb-grid">${(art.scene_art || []).map((s) =>
        assetCardHtml(ep, { kind: "scene_art", name: s.name }, s.url,
                      s.name, isMock(`scene:${s.name}`), s.asset_id)).join("")
        || `<div class="dim">暂无场景概念图。</div>`}</div>
    </section>
    <section class="panel asset-panel">
      <h2>🎞 镜头图片 <span class="dim">关键图、首帧和尾帧均可删除；
        删除关键图会同时安全作废对应首尾帧和旧视频，避免误复用。</span></h2>
      <div class="pb-grid">${(art.image_assets || [])
        .filter((item) => ["image", "first_frame", "last_frame", "cover"].includes(item.kind))
        .map((item) => {
          const shotNo = Number((item.meta || {}).shot_no || 0);
          const target = item.kind === "image" && shotNo
            ? { kind: "shot", shot_no: shotNo }
            : (["first_frame", "last_frame"].includes(item.kind) && shotNo
              ? { kind: "frames", shot_no: shotNo } : null);
          return assetCardHtml(target ? ep : null, target || {}, item.url,
            item.label, false, item.asset_id);
        }).join("") || `<div class="dim">暂无镜头图片。</div>`}</div>
    </section>
  </div>`;
  document.getElementById("asset-project").onchange = (ev) =>
    renderAssetsCenter(ev.target.value);
  bindImageLineControls();
  const restyleBtn = document.getElementById("btn-restyle");
  if (restyleBtn && ep) restyleBtn.onclick = (ev) =>
    armConfirm(ev.target, "重做", async () => {
      try {
        await api("/api/restyle", {
          method: "POST", headers: { "Content-Type": "application/json" },
          body: JSON.stringify({
            episode_id: ep.id,
            style: document.getElementById("restyle-input").value.trim(),
          }),
        });
        showToast("已开始按新画风重做全部形象,跳转到实况页…", "ok");
        location.hash = `#/episode/${ep.id}`;
      } catch (e) { showToast(e.message, "error"); }
    });
  const reload = () => renderAssetsCenter(title);
  const bindAssetDeleteButtons = () => {
    app.querySelectorAll(".asset-del").forEach((btn) => {
      btn.onclick = (ev) => armConfirm(ev.target, "删除", async () => {
        try {
          await api("/api/asset/delete", {
            method: "POST", headers: { "Content-Type": "application/json" },
            body: JSON.stringify({ project: title,
              asset_id: Number(btn.dataset.assetId) }),
          });
          showToast("图片已从资产中心删除，历史版本仍保留", "ok");
          reload();
        } catch (e) { showToast(e.message, "error"); }
      });
    });
  };
  const renderAssetBoard = () => {
    const board = document.getElementById("asset-catalog-board");
    if (!board) return;
    board.innerHTML = assetBoardHtml(
      art.image_assets || [], document.getElementById("asset-category").value,
      localStorage.getItem("aifos.assets.board") || "all");
    bindAssetDeleteButtons();
    bindLightbox(board);
  };
  document.querySelectorAll("[data-asset-board-group]").forEach((button) => {
    button.onclick = () => {
      localStorage.setItem("aifos.assets.board", button.dataset.assetBoardGroup);
      document.querySelectorAll("[data-asset-board-group]").forEach((item) =>
        item.classList.toggle("active", item.dataset.assetBoardGroup === button.dataset.assetBoardGroup));
      renderAssetBoard();
    };
  });
  document.getElementById("asset-category").onchange = (ev) => {
    localStorage.setItem("aifos.assets.category", ev.target.value);
    renderAssetBoard();
  };
  bindLightbox(app);
  if (ep) {
    bindRegen(app, ep.id, () => null, reload);
    bindIo(app, ep.id, reload);
  }
  document.getElementById("ref-upload").onclick = () => {
    const input = document.createElement("input");
    input.type = "file";
    input.accept = "image/*";
    input.onchange = () => {
      const file = input.files[0];
      if (!file) return;
      const reader = new FileReader();
      reader.onload = async () => {
        try {
          await api("/api/reference/upload", {
            method: "POST",
            headers: { "Content-Type": "application/json" },
            body: JSON.stringify({
              project: title,
              name: document.getElementById("ref-name").value.trim()
                    || file.name.replace(/\.[^.]+$/, ""),
              attach_to: document.getElementById("ref-attach").value.trim(),
              reference_role: document.getElementById("ref-role").value,
              filename: file.name,
              data_base64: String(reader.result).split(",")[1] || "",
            }),
          });
          showToast("参考图已上传，只会按所选用途和关联对象使用", "ok");
          reload();
        } catch (e) { showToast(e.message, "error"); }
      };
      reader.readAsDataURL(file);
    };
    input.click();
  };
  app.querySelectorAll(".ref-del").forEach((btn) => {
    btn.onclick = (ev) => armConfirm(ev.target, "删除", async () => {
      try {
        await api("/api/reference/delete", {
          method: "POST", headers: { "Content-Type": "application/json" },
          body: JSON.stringify({ project: title, name: btn.dataset.name }),
        });
        showToast("参考图已删除", "ok");
        reload();
      } catch (e) { showToast(e.message, "error"); }
    });
  });
  bindAssetDeleteButtons();
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
  script: "剧本 + AI制作圣经", continuity: "连续性圣经", cast: "人物/场景图",
  storyboard: "五维分镜", blocking: "空间调度图", images: "关键帧", text_assets: "文字资产锁定",
  frames: "首尾帧", preflight: "生产门禁", videos: "Seedance 视频",
  voices: "随视频配音/口型", edit: "剪映剪辑",
  qc: "三层质检", package: "封面/标题", archive: "数据沉淀",
  assets: "资产调用",
};
const STAGE_ORDER = ["script", "continuity", "cast", "storyboard", "blocking", "images",
  "text_assets", "frames", "preflight", "videos", "voices", "edit", "qc",
  "package", "archive"];
const STAGE_PLAIN = {
  cancelling: "正在暂停,已完成的图片全部保留,随时可从断点继续",
  script: "正在写/读取剧本，并分析世界、环境和风格",
  continuity: "正在锁定角色、场景和文字规则",
  cast: "正在画人物和场景", storyboard: "正在生成五维分镜",
  blocking: "正在锁定人物走位、机位与屏幕轴线",
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
  const awaitingScript = data.episodes.filter(
    (e) => e.status === "awaiting_script");
  const awaitingCast = data.episodes.filter(
    (e) => e.status === "awaiting_cast");
  const awaiting = data.episodes.filter(
    (e) => e.status === "awaiting_confirm");
  const producing = data.episodes.filter(
    (e) => !["done", "failed", "qc_failed", "created", "awaiting_script",
             "awaiting_cast", "awaiting_confirm", "queued_script"].includes(e.status));
  if (!producing.length && !awaiting.length && !awaitingScript.length
      && !awaitingCast.length) {
    el.innerHTML = ""; return;
  }
  el.innerHTML = awaitingScript.map((e) => `
    <div class="progress-card confirm">
      <div class="progress-text">《${esc(e.project)}》第${e.number}集 剧本写好了,先过目 📖
        <span>看过剧本点确认才开始画图(还没花出图额度);不满意可附意见重写</span></div>
      <button class="primary" onclick="location.hash='#/episode/${e.id}'">去看剧本 →</button>
    </div>`).join("") + awaitingCast.map((e) => `
    <div class="progress-card confirm">
      <div class="progress-text">《${esc(e.project)}》第${e.number}集 人物/核心道具候选已就绪 👤
        <span>所有正式角色统一4张候选（主角、重要配角和普通配角都认真挑选；跑龙套/背景路人不做独立设定，也不生成候选图或立绘）；全部定版后才生成后续图片</span></div>
      <button class="primary" onclick="location.hash='#/episode/${e.id}'">去选人物/道具 →</button>
    </div>`).join("") + awaiting.map((e) => `
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
      <button class="stop-btn" onclick="stopEpisode(${e.id})" title="暂停生成:已完成的图片全部保留,可从断点继续">⏸ 暂停</button>
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

/* ---- 实时工作状态:当前步骤/张数/已运行时长/最新产线日志 ---- */
let liveState = { stage: "", since: 0 };
let liveTicker = null;

function liveCounts(data) {
  const art = data.artifacts || {};
  const total = data.storyboard ? data.storyboard.shots.length : 0;
  const parts = [];
  const progress = productionProgressModel(data);
  if (progress.active) {
    parts.push(`并行 ${progress.activeCount}/${progress.parallelism || progress.activeCount || 1}`);
    const activeLabels = progress.activeItems.slice(0, 4).map((item) =>
      `${item.status === "retrying" ? "自动修图" : "生产"}:${item.label}`);
    if (activeLabels.length) parts.push(activeLabels.join("、")
      + (progress.activeItems.length > 4 ? ` 等${progress.activeItems.length}项` : ""));
  }
  const plan = ((data.render_plan || {}).items) || [];
  if (plan.length) {
    parts.push(`图片正式资产 ${progress.completed}/${progress.total}`);
  }
  if (total) {
    parts.push(`关键帧 ${progress.keyframeDone}/${progress.keyframeTotal}`);
    parts.push(`视频 ${Object.keys(art.videos || {}).length}/${total}`);
  }
  if (!plan.length) {
    const cast = (art.cast_art || []).length;
    if (cast) parts.push(`人物图 ${cast}`);
  }
  return parts.join(" · ");
}

function updateLiveStrip(data) {
  const el = document.getElementById("live-strip");
  if (!el) return;
  const ep = data.episode;
  const progress = productionProgressModel(data);
  const runningTask = (data.tasks || []).find((t) => t.status === "running");
  const stage = ep.status === "cancelling" ? "cancelling"
    : progress.stage || (runningTask && runningTask.stage) || ep.status;
  if (stage !== liveState.stage) liveState = { stage, since: Date.now() };
  const secs = Math.floor((Date.now() - liveState.since) / 1000);
  const mm = Math.floor(secs / 60), ss = String(secs % 60).padStart(2, "0");
  const counts = liveCounts(data);
  const slow = secs > 120 && stage !== "cancelling"
    ? " · 外部产线(出图/视频)可能需要几分钟,只要本条时间在走就没卡住" : "";
  el.innerHTML = `
    <span class="live-dot${stage === "cancelling" ? " stopping" : ""}"></span>
    <b>${esc(progress.active
      ? STAGE_PLAIN[stage] || STAGE_CN[stage] || stage
      : "当前没有生产任务")}</b>
    <span class="dim">本步已进行 ${mm}:${ss}${counts ? " · " + counts : ""}${slow}</span>
    <span class="live-last dim" id="live-last"></span>`;
}

function startLiveTicker(episodeId) {
  if (liveTicker) clearInterval(liveTicker);
  liveTicker = setInterval(async () => {
    const strip = document.getElementById("live-strip");
    if (!strip) { clearInterval(liveTicker); liveTicker = null; return; }
    // 每秒本地跳秒;每 3 秒拿一条最新日志(轻量)
    const secs = Math.floor((Date.now() - liveState.since) / 1000);
    if (secs % 3 === 0) {
      api("/api/logs?limit=1").then((rows) => {
        const last = document.getElementById("live-last");
        if (last && rows.length)
          last.textContent = ` · 最新:${rows[0].message}`.slice(0, 120);
      }).catch(() => {});
      const logEl = document.getElementById("live-log");
      if (logEl) api("/api/logs?limit=14").then((rows) => {
        logEl.innerHTML = rows.reverse().map((r) =>
          `<div class="lv-${esc(r.level)}">[${esc(r.level)}] ${esc(r.source)}: ${esc(r.message)}</div>`).join("");
        logEl.scrollTop = logEl.scrollHeight;
      }).catch(() => {});
    }
    const bold = strip.querySelector("b");
    if (bold) {
      const dim = strip.querySelector(".dim");
      if (dim) {
        const mm = Math.floor(secs / 60), ss = String(secs % 60).padStart(2, "0");
        dim.textContent = dim.textContent.replace(/本步已进行 \d+:\d+/, `本步已进行 ${mm}:${ss}`);
      }
    }
    // 看板上"正在画"的卡片:秒表每秒走字
    document.querySelectorAll(".pc-timer[data-started]").forEach((el) => {
      const started = Number(el.dataset.started);
      if (!started) return;
      const s = Math.max(0, Math.floor(Date.now() / 1000 - started));
      const mm2 = Math.floor(s / 60), ss2 = String(s % 60).padStart(2, "0");
      const eta = Number(el.dataset.eta);
      el.textContent = `正在画 ${mm2}:${ss2}`
        + (eta ? ` · 预计 ~${fmtDur(eta)}` : "");
    });
  }, 1000);
}

/* 画布轮询防闪烁:内容签名没变就不重画 */
let canvasSignature = "";

function canvasSig(data) {
  return JSON.stringify([
    data.episode.status, data.episode.qc_score, data.episode.title,
    (data.tasks || []).map((t) => [t.stage, t.status]),
    data.script_version, data.storyboard_version,
    data.character_asset_policy_version,
    (data.character_asset_policy || {}).mode,
    (data.character_asset_policy || {}).resolved_mode,
    Object.values((data.artifacts || {}).images || {}),
    Object.values((data.artifacts || {}).first || {}),
    Object.values((data.artifacts || {}).last || {}),
    Object.entries((data.artifacts || {}).video_audio || {}),
    Object.values((data.artifacts || {}).videos || {}),
    ((data.artifacts || {}).cast_art || []).map((c) => c.url),
    ((data.artifacts || {}).scene_art || []).map((x) => x.url),
    (data.artifacts || {}).final, (data.artifacts || {}).cover,
    (((data.render_plan || {}).items) || []).map(
      (i) => [i.id, i.status, i.output_url || "",
        i.custom_prompt ? i.prompt : ""]),
    (data.image_failures || []).map((item) => [
      item.item_id, item.shot_no, item.failed_output_url,
      item.revision_feedback, item.issues,
    ]),
    data.production_progress,
    data.production_guidance,
  ]);
}

function pollCanvas(episodeId) {
  if (pollTimer) clearInterval(pollTimer);
  pollTimer = setInterval(async () => {
    try {
      const data = await api(`/api/episode/${episodeId}`);
      watchBuild(data);
      updateLiveStrip(data);          // 状态条每轮都刷新(不重画整页)
      if (canvasSig(data) !== canvasSignature) {
        const planOverlay = document.querySelector(
          `.plan-overlay[data-episode="${episodeId}"]`);
        if (planOverlay) {
          // 图片清单打开时只更新清单；不重建底层画布，避免遮罩层和滚动位置跳动。
          const signature = canvasSig(data);
          const key = String(episodeId);
          if (planOverlaySignatures.get(key) !== signature)
            refreshOpenPlanOverlay(episodeId);
        } else {
          const scrollHost = document.scrollingElement;
          const scrollTop = scrollHost ? scrollHost.scrollTop : 0;
          renderCanvasView(episodeId).finally(() => requestAnimationFrame(() => {
            if (scrollHost) scrollHost.scrollTop = scrollTop;
          }));
        }
      }
    } catch (e) { /* 下一轮再试 */ }
  }, 3000);
}

const CARD_W = 220, CARD_H = 218, GAP_X = 26, GAP_Y = 56, LANE_X = 150;
const CANVAS_STAGE_W = 324, CANVAS_STAGE_GAP = 30;
const CANVAS_STAGE_LEFT = 28, CANVAS_STAGE_TOP = 28;
const CANVAS_SHOTS_TOP = 2240;

function castLookHtml(candidate) {
  const look = candidate.look_variant;
  const valid = candidate.variant_source !== "legacy"
    && candidate.variant_label && look && typeof look === "object";
  if (!valid) return `<div class="cast-look legacy">
    <b>历史候选</b><span>未记录候选差异轴；建议按本剧唯一画风规则重新生成后再比较。</span>
  </div>`;
  const rows = [
    ["服装", look.costume], ["发型", look.hair],
    ["妆容", look.makeup], ["气质", look.temperament],
  ].filter(([, value]) => value);
  return `<div class="cast-look">
    <dl>${rows.map(([label, value]) => `<div><dt>${label}</dt><dd>${esc(value)}</dd></div>`).join("")}</dl>
  </div>`;
}

function castVisualDnaHtml(character) {
  const dna = character.visual_dna || {};
  const rows = [
    ["脸部骨相", dna.face_structure],
    ["发型轮廓", dna.hair_silhouette],
    ["职业/身体痕迹", dna.body_or_occupation_marks],
    ["服装结构", dna.clothing_structure],
    ["故事视觉符号", dna.story_visual_symbol],
    ["核心配饰", dna.signature_accessory],
    ["气质关键词", Array.isArray(dna.temperament_keywords)
      ? dna.temperament_keywords.join("、") : dna.temperament_keywords],
  ].filter(([, value]) => value);
  if (!rows.length) return "";
  const dedup = character.cast_dedup || {};
  const dedupStatus = dedup.status === "redesign_required"
    ? "⚠ 与其他角色存在重叠，候选提示词已要求重设计"
    : "✓ 已完成全剧角色去重预审";
  return `<details class="cast-look cast-visual-dna"><summary>查看剧情视觉 DNA</summary>
    <dl>${rows.map(([label, value]) => `<div><dt>${label}</dt><dd>${esc(value)}</dd></div>`).join("")}</dl>
    <p class="dim">${esc(dedupStatus)}</p></details>`;
}

function renderCastSelection(data, episodeId) {
  const selection = data.cast_selection || {};
  const characters = selection.characters || [];
  const props = selection.props || [];
  const assetPolicy = data.character_asset_policy || {
    mode: "auto", resolved_mode: "full", generate_sheets: true, reasons: [],
  };
  const assetMode = assetPolicy.mode || "auto";
  const resolvedLabel = assetPolicy.resolved_mode === "simple"
    ? "简化版（仅最终人物形象图）"
    : `完整版（每人增加 ${assetPolicy.sheet_count_per_character || 9} 张生产资产）`;
  const assetPolicyStatus = assetMode === "auto"
    ? `自动判断结果：${resolvedLabel}${(assetPolicy.reasons || []).length
      ? ` · ${assetPolicy.reasons.join("；")}` : ""}`
    : (assetMode === "simple"
      ? "已选择简化版：人工豁免三视图门禁，不生成独立正侧背和细节图"
      : "已选择完整版：生成视觉DNA、三视图审核板及独立高清正侧背母资产");
  const policy = selection.candidate_policy
    || "主角、重要配角和普通配角统一4张候选；跑龙套/背景路人不做独立设定、不生成候选图或立绘";
  app.innerHTML = `<div class="canvas-view cast-select-view">
    <div class="confirm-banner">
      <div><b>先定人物和核心道具，再生产后续图片 👤</b>
        <span>${esc(policy)}；${esc(selection.prop_candidate_policy || "核心道具统一4张候选并人工定版")}。有参考图时人物脸和发型是最高标准，职业角色必须穿工作服；人物候选统一使用纯背景，不得出现文字或场景。
        每名正式角色先从剧情推导视觉DNA并与全剧角色去重，再统一生成4张同一画风下的候选图；
        所有候选继承本剧唯一画风，不提供多个画风选项，只比较人物身份、表情、轻微姿态和剧情造型细节，
        请各选1张作为最终立绘。
        定版后完整版会生成面部、正面、严格90°侧面和完整180°背面独立母资产；
        16:9三视图拼板只用于审核，不作为正式镜头参考。
        后续关键帧、首尾帧和其他图片 API 都会真实携带这张参考图，
        视觉质检也会将成图与它逐人比对。</span></div>
        <div class="cast-asset-policy">
          <label for="cast-asset-mode"><b>人物扩展资产</b>
            <select id="cast-asset-mode">
              <option value="auto" ${assetMode === "auto" ? "selected" : ""}>自动判断（推荐）</option>
              <option value="simple" ${assetMode === "simple" ? "selected" : ""}>简化版 · 人工豁免三视图</option>
              <option value="full" ${assetMode === "full" ? "selected" : ""}>完整版 · 视觉DNA与独立三视图母资产</option>
            </select>
          </label>
          <span id="cast-asset-policy-status" class="dim" aria-live="polite">${esc(assetPolicyStatus)}</span>
        </div>
      <div class="cast-actions">
        <button id="cast-regenerate" title="保留旧版本，为全部人物和核心道具各重生成4张">↻ 全部重生成4张</button>
        <button class="primary" id="cast-continue" ${selection.passed ? "" : "disabled"}>
          ${selection.passed ? "✅ 全部定版，继续预生产" : `已定版 ${selection.asset_locked || 0}/${selection.asset_total || characters.length + props.length}`}
        </button>
      </div>
    </div>
    ${imageAccelerationLivebarHtml(data)}
    <div class="canvas-toolbar">
      <button id="cast-back">← 仪表盘</button>
      <span class="title">《${esc(data.project.title)}》第${data.episode.number}集 · 人物定版</span>
      ${chip(data.episode.status)}<span class="spacer"></span>
      <button id="cast-script">📖 看剧本</button>
    </div>
    ${productionLedgerHtml(data, { context: "cast" })}
    <div class="cast-selection-list">${characters.map((character) => `
      <section class="cast-choice panel">
        <div class="cast-choice-head"><div><h2>${esc(character.character)}</h2>
          <span class="dim">${esc(character.role || "角色")} · ${character.candidate_count || 0}/${character.candidate_target || selection.candidate_target || 4} 张候选</span></div>
          <button type="button" class="cast-regenerate-one" data-character="${esc(character.character)}">↻ 不满意，换4张</button>
          <strong class="${character.candidate_target === 0 ? "cast-locked" : (character.locked ? "cast-locked" : "cast-unlocked")}">
            ${character.candidate_target === 0 ? "无需单独立绘" : (character.locked ? "✓ 已锁定最终立绘" : "请选择1张")}</strong></div>
        ${castVisualDnaHtml(character)}
        <div class="cast-candidate-grid" role="list" aria-label="${esc(character.character)}的造型候选">${(character.candidates || []).map((candidate) => {
          const variant = candidate.variant_source === "legacy" || !candidate.variant_label
            ? "历史候选" : candidate.variant_label;
          const title = `${character.character} · 候选${candidate.index} · ${variant}`;
          return `<article class="cast-candidate${candidate.selected ? " selected" : ""}" role="listitem">
            <button type="button" class="cast-image" data-full="${esc(candidate.url || "")}" data-title="${esc(title)}" aria-label="查看${esc(title)}大图">
              ${candidate.url ? `<img src="${esc(thumbUrl(candidate.url, 520))}" loading="lazy" alt="${esc(title)}">`
                : `<span class="plan-thumb-empty">图片缺失</span>`}
            </button>
            <div class="cast-candidate-foot"><div class="cast-candidate-title"><span>候选 ${candidate.index}</span><b>${esc(variant)}</b></div>
              ${castLookHtml(candidate)}
              <button type="button" class="${candidate.selected ? "selected" : "primary"} cast-pick"
                data-character="${esc(character.character)}" data-index="${candidate.index}"
                aria-pressed="${candidate.selected ? "true" : "false"}" ${candidate.selected ? "disabled" : ""}>
                ${candidate.selected ? "✓ 当前最终立绘" : "选定这套造型"}</button></div>
          </article>`;
        }).join("")}</div>
      </section>`).join("")}</div>
    ${props.length ? `<h2 class="cast-section-title">核心道具四选一</h2>
    <div class="cast-selection-list">${props.map((prop) => `
      <section class="cast-choice panel">
        <div class="cast-choice-head"><div><h2>${esc(prop.prop)}</h2>
          <span class="dim">核心道具 · ${prop.candidate_count || 0}/${prop.candidate_target || 4} 张候选</span></div>
          <button type="button" class="prop-regenerate-one" data-prop="${esc(prop.prop)}">↻ 不满意，换4张</button>
          <strong class="${prop.locked ? "cast-locked" : "cast-unlocked"}">
            ${prop.locked ? "✓ 已锁定道具母资产" : "请选择1张"}</strong></div>
        ${(prop.story_function || prop.visual_design) ? `<details class="cast-look"><summary>查看道具设计依据</summary>
          ${prop.story_function ? `<p><b>剧情功能：</b>${esc(prop.story_function)}</p>` : ""}
          ${prop.visual_design ? `<p><b>视觉结构：</b>${esc(prop.visual_design)}</p>` : ""}
        </details>` : ""}
        <div class="cast-candidate-grid" role="list" aria-label="${esc(prop.prop)}的道具候选">${(prop.candidates || []).map((candidate) => {
          const variant = candidate.variant_label || `方案${candidate.index}`;
          const title = `${prop.prop} · 候选${candidate.index} · ${variant}`;
          return `<article class="cast-candidate${candidate.selected ? " selected" : ""}" role="listitem">
            <button type="button" class="cast-image" data-full="${esc(candidate.url || "")}" data-title="${esc(title)}" aria-label="查看${esc(title)}大图">
              ${candidate.url ? `<img src="${esc(thumbUrl(candidate.url, 520))}" loading="lazy" alt="${esc(title)}">`
                : `<span class="plan-thumb-empty">图片缺失</span>`}
            </button>
            <div class="cast-candidate-foot"><div class="cast-candidate-title"><span>候选 ${candidate.index}</span><b>${esc(variant)}</b></div>
              <button type="button" class="${candidate.selected ? "selected" : "primary"} prop-pick"
                data-prop="${esc(prop.prop)}" data-index="${candidate.index}"
                aria-pressed="${candidate.selected ? "true" : "false"}" ${candidate.selected ? "disabled" : ""}>
                ${candidate.selected ? "✓ 当前道具母资产" : "选定这套道具"}</button></div>
          </article>`;
        }).join("")}</div>
      </section>`).join("")}</div>` : ""}
  </div>`;
  bindImageAccelerationLivebar(episodeId);
  bindProductionLedger(app, data, episodeId);
  document.getElementById("cast-back").onclick = () => { location.hash = "#/"; };
  document.getElementById("cast-script").onclick = () => showScriptOverlay(data, episodeId);
  document.getElementById("cast-regenerate").onclick = (ev) => armConfirm(
    ev.currentTarget, "放弃当前选择并重新生成", async () => {
      ev.currentTarget.disabled = true;
      ev.currentTarget.textContent = "重新生成中…";
      try {
        await api("/api/character/regenerate", {
          method: "POST", headers: { "Content-Type": "application/json" },
          body: JSON.stringify({ episode_id: data.episode.id }),
        });
        showToast("已返回人物候选生成，旧候选保留在历史中", "ok");
        pollCanvas(episodeId);
      } catch (e) {
        showToast(e.message, "error");
        ev.currentTarget.disabled = false;
        ev.currentTarget.textContent = "↻ 全部重生成4张";
      }
    });
  app.querySelectorAll(".cast-image").forEach((button) => {
    button.onclick = () => {
      if (button.dataset.full) showImageLightbox(button.dataset.full, button.dataset.title || "人物候选大图");
    };
  });
  app.querySelectorAll(".cast-pick").forEach((button) => {
    button.onclick = async () => {
      const group = [...app.querySelectorAll(".cast-pick")]
        .filter((item) => item.dataset.character === button.dataset.character);
      group.forEach((item) => { item.disabled = true; });
      button.textContent = "锁定中…";
      try {
        await api("/api/character/select", {
          method: "POST", headers: { "Content-Type": "application/json" },
          body: JSON.stringify({ episode_id: data.episode.id,
            character: button.dataset.character,
            candidate_index: Number(button.dataset.index) }),
        });
        showToast(`${button.dataset.character} 已锁定候选 ${button.dataset.index}`, "ok");
        renderCanvasView(episodeId);
      } catch (e) {
        showToast(e.message, "error");
        group.forEach((item) => {
          item.disabled = item.getAttribute("aria-pressed") === "true";
        });
        button.textContent = "选定这套造型";
      }
    };
  });
  app.querySelectorAll(".prop-pick").forEach((button) => {
    button.onclick = async () => {
      const group = [...app.querySelectorAll(".prop-pick")]
        .filter((item) => item.dataset.prop === button.dataset.prop);
      group.forEach((item) => { item.disabled = true; });
      button.textContent = "锁定中…";
      try {
        await api("/api/prop/select", {
          method: "POST", headers: { "Content-Type": "application/json" },
          body: JSON.stringify({ episode_id: data.episode.id,
            prop: button.dataset.prop,
            candidate_index: Number(button.dataset.index) }),
        });
        showToast(`${button.dataset.prop} 已锁定候选 ${button.dataset.index}`, "ok");
        renderCanvasView(episodeId);
      } catch (e) {
        showToast(e.message, "error");
        group.forEach((item) => { item.disabled = false; });
        button.textContent = "选定这套道具";
      }
    };
  });
  const regenerateOne = async (button, payload) => {
    button.disabled = true;
    button.textContent = "重新生成中…";
    try {
      await api("/api/character/regenerate", {
        method: "POST", headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ episode_id: data.episode.id, ...payload }),
      });
      showToast("旧版本已保留，正在生成新一轮4张候选", "ok");
      pollCanvas(episodeId);
    } catch (e) {
      showToast(e.message, "error");
      button.disabled = false;
      button.textContent = "↻ 不满意，换4张";
    }
  };
  app.querySelectorAll(".cast-regenerate-one").forEach((button) => {
    button.onclick = (event) => armConfirm(
      event.currentTarget, "保留旧版并生成新4张", () => regenerateOne(
        button, { character: button.dataset.character }));
  });
  app.querySelectorAll(".prop-regenerate-one").forEach((button) => {
    button.onclick = (event) => armConfirm(
      event.currentTarget, "保留旧版并生成新4张", () => regenerateOne(
        button, { prop: button.dataset.prop }));
  });
  const next = document.getElementById("cast-continue");
  const assetModeSelect = document.getElementById("cast-asset-mode");
  const assetModeStatus = document.getElementById("cast-asset-policy-status");
  if (assetModeSelect) assetModeSelect.onchange = async () => {
    const previous = assetMode;
    assetModeSelect.disabled = true;
    if (next) next.disabled = true;
    assetModeStatus.textContent = "正在保存人物资产设置…";
    try {
      const result = await api("/api/character/assets-policy", {
        method: "POST", headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ episode_id: data.episode.id,
          mode: assetModeSelect.value,
          expected_version: Number(data.character_asset_policy_version || 0) }),
      });
      const resolved = (result.policy || {}).resolved_mode === "simple"
        ? "简化版" : "完整版";
      showToast(`人物资产模式已保存：${resolved}`, "ok");
      await renderCanvasView(episodeId);
    } catch (e) {
      assetModeSelect.value = previous;
      assetModeSelect.disabled = false;
      assetModeStatus.textContent = `保存失败：${e.message}`;
      if (next) next.disabled = !selection.passed;
      showToast(e.message, "error");
    }
  };
  if (next && selection.passed) next.onclick = async () => {
    next.disabled = true; next.textContent = "已确认，继续生产中…";
    try {
      await api("/api/confirm", {
        method: "POST", headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ episode_id: data.episode.id }),
      });
      showToast(assetPolicy.generate_sheets
        ? "人物已全部定版，开始生成三视图审核板与独立高清母资产"
        : "人物已全部定版；按人工选择豁免三视图，开始后续图片", "ok");
      pollCanvas(episodeId);
    } catch (e) {
      showToast(e.message, "error");
      next.disabled = false; next.textContent = "✅ 全部定版，继续预生产";
    }
  };
}

const VIDEO_REF_KIND_CN = {
  image: "分镜图", character_identity: "人物最终立绘",
  scene_art: "场景图", reference: "用户参考图",
  character_sheet: "人物资产图", character_art: "人物立绘",
  inner_persona: "内心Q版母资产",
  first_frame: "首帧", last_frame: "尾帧",
  spatial_blocking: "空间调度图",
};

const VIDEO_REF_USAGE_CN = {
  image: "锁定本镜构图、站位和机位",
  character_identity: "只锁定人物身份、脸型和发型轮廓，不复制姿势与背景",
  scene_art: "只锁场景布局、材质和主光方向",
  reference: "按上传时声明的单一用途使用",
  character_sheet: "只补充标签对应的人物局部属性",
  character_art: "只锁脸、年龄、性别表达与身份标志",
  inner_persona: "只锁Q版身份和当前服装；约1.8头身、头大身小，动作表情可夸张发挥",
  first_frame: "视频动作起点，必须传入",
  last_frame: "视频动作终点及衔接，必须传入",
  spatial_blocking: "锁定多人站位、行动路线和摄影机起终点，必须传入",
};

function friendlyVideoReferenceName(item, shotNo) {
  if (item.kind === "image") return `镜头 ${shotNo} 参考分镜`;
  let name = String(item.name || VIDEO_REF_KIND_CN[item.kind] || "参考图");
  if (item.kind !== "reference") return name;
  name = name.split("__").pop() || name;
  const suffixes = {
    features: "人物特征参考", identity: "人物身份参考",
    makeup: "妆容参考", outfit: "服装参考", detail: "细节参考",
    fullbody: "全身参考", portrait: "肖像参考",
  };
  const match = name.match(/^(.+?)_([a-zA-Z][a-zA-Z0-9_-]*)$/);
  if (!match) return name;
  const suffix = suffixes[match[2].toLowerCase()]
    || `${match[2].replaceAll("_", " ")} 参考`;
  return `${match[1]} · ${suffix}`;
}

function videoReferenceFigureHtml({
  url, kind, name, order, missing = false, binding = "",
}) {
  const type = VIDEO_REF_KIND_CN[kind] || kind || "参考图";
  const usage = binding || VIDEO_REF_USAGE_CN[kind] || "提供本镜视觉约束";
  if (!url || missing) return `<figure class="video-ref-card missing">
    <div class="video-ref-missing-image">待生成</div>
    <figcaption><b>${esc(type)}</b><span>${esc(name)}</span>
      <small>${esc(usage)}</small></figcaption>
  </figure>`;
  return `<figure class="video-ref-card ${kind === "first_frame"
    || kind === "last_frame" ? "required" : "asset"}">
    <button type="button" class="video-ref-preview"
      data-image-url="${esc(url)}" data-image-title="${esc(type)} · ${esc(name)}"
      aria-label="放大查看${esc(type)}：${esc(name)}">
      <img src="${esc(thumbUrl(url, 180))}" loading="lazy"
        alt="${esc(type)} ${esc(name)}">
      <span class="video-ref-order">${order}</span>
    </button>
    <figcaption><b>${esc(type)}</b><span title="${esc(name)}">${esc(name)}</span>
      <small>${esc(usage)}</small></figcaption>
  </figure>`;
}

function videoReferencePanelHtml(data) {
  const shots = (data.storyboard || {}).shots || [];
  const effective = (data.video_references_effective || {}).shots || {};
  const artifacts = data.artifacts || {};
  const firstFrames = artifacts.first || {};
  const lastFrames = artifacts.last || {};
  return `<section class="panel video-ref-panel">
    <h2>🧷 Seedance 参考图输入表
      <span class="dim">逐镜列出实际送入 Seedance 的首帧、尾帧和资产参考图。
      编号就是输入顺序；首尾帧固定占 2 张，资产参考图最多 7 张。</span></h2>
    <div class="video-ref-table-wrap" role="region"
      aria-label="Seedance 逐镜参考图输入表，可滚动查看">
      <table class="video-ref-table">
        <caption>每个镜头实际送入 Seedance 的全部图片与用途</caption>
        <thead><tr>
          <th scope="col">镜头</th>
          <th scope="col">首帧（必传）</th>
          <th scope="col">尾帧（必传）</th>
          <th scope="col">资产参考图（最多 7 张）</th>
          <th scope="col">输入状态</th>
          <th scope="col">操作</th>
        </tr></thead>
        <tbody>${shots.map((shot) => {
      const entry = effective[String(shot.shot_no)] || {};
      const rows = entry.items || [];
      const auto = entry.mode !== "manual";
      const first = firstFrames[String(shot.shot_no)]
        || firstFrames[shot.shot_no] || "";
      const last = lastFrames[String(shot.shot_no)]
        || lastFrames[shot.shot_no] || "";
      const frameCount = Number(Boolean(first)) + Number(Boolean(last));
      const total = frameCount + rows.length;
      const spatialRequired = Boolean(entry.spatial_reference_required);
      const spatialReady = !spatialRequired
        || Boolean(entry.spatial_reference_ready);
      return `<tr>
        <th scope="row" class="video-ref-shot">
          <b>#${String(shot.shot_no).padStart(2, "0")}</b>
          <span>${esc(shot.unit_id || `镜头 ${shot.shot_no}`)}</span>
        </th>
        <td data-label="首帧（必传）">${videoReferenceFigureHtml({
          url: first, kind: "first_frame",
          name: `镜头 ${shot.shot_no} 动作起点`, order: "①",
          missing: !first,
        })}</td>
        <td data-label="尾帧（必传）">${videoReferenceFigureHtml({
          url: last, kind: "last_frame",
          name: `镜头 ${shot.shot_no} 动作终点`, order: "②",
          missing: !last,
        })}</td>
        <td data-label="资产参考图" class="video-ref-assets">
          <div class="video-ref-asset-list">${rows.map((item, index) =>
            videoReferenceFigureHtml({
              url: item.url, kind: item.kind,
              name: friendlyVideoReferenceName(item, shot.shot_no),
              order: String(index + 3),
              binding: item.binding,
            })).join("")
            || `<span class="video-ref-empty">无额外资产参考图</span>`}</div>
        </td>
        <td data-label="输入状态" class="video-ref-status">
          <span class="video-ref-mode ${auto ? "auto" : "manual"}">${auto
            ? "按剧本自动选入" : "人工选定"}</span>
          <b>首尾帧 ${frameCount}/2 · 资产 ${rows.length}/7</b>
          ${spatialRequired ? `<small class="${spatialReady ? "pass" : "danger"}">
            空间图${spatialReady ? "已强制加入" : "缺失，禁止 Seedance"}
            ${entry.spatial_reference_reason
              ? ` · ${esc(entry.spatial_reference_reason)}` : ""}</small>` : ""}
          <small>${frameCount === 2 && spatialReady
            ? `实际输入 ${total} 张，可进入 Seedance`
            : (frameCount < 2
              ? `缺 ${2 - frameCount} 张必传帧，暂不可开拍`
              : "缺必传空间图，暂不可开拍")}</small>
        </td>
        <td data-label="操作">
          <button class="video-ref-edit" data-shot-no="${shot.shot_no}">
            调整资产参考图</button>
        </td>
      </tr>`;
    }).join("")}</tbody>
      </table>
    </div>
  </section>`;
}

function showVideoReferencePicker(data, episodeId, shotNo) {
  const library = (data.artifacts || {}).image_assets || [];
  // 从"实际生效"的参考图出发调整:自动模式下就是自动选入的那批
  const effectiveRows = (((data.video_references_effective || {}).shots
    || {})[String(shotNo)] || {}).items || [];
  const entry = (((data.video_references_effective || {}).shots
    || {})[String(shotNo)] || {});
  const manualLimit = entry.spatial_reference_required ? 6 : 7;
  const selected = new Set(effectiveRows
    .filter((row) => row.kind !== "spatial_blocking")
    .map((row) => Number(row.asset_id)));
  const overlay = document.createElement("div");
  overlay.className = "script-overlay video-ref-overlay";
  overlay.innerHTML = `<div class="script-box video-ref-box">
    <div class="script-head"><h2>镜头 ${shotNo} · 选择资产参考图</h2>
      <button class="close">关闭</button></div>
    <p class="dim">图片会与本镜首尾帧一起交给 Seedance 2.0 Fast VIP。
      低质量候选不能勾选；最多选 ${manualLimit} 张。${entry.spatial_reference_required
        ? "本镜的空间调度图由系统强制加入，不占人工选择。" : ""}</p>
    <div class="video-ref-picker-grid">${library.map((item) => `
      <label class="video-ref-choice${item.usable_for_video
        && item.kind !== "spatial_blocking" ? "" : " disabled"}">
        <input type="checkbox" value="${item.asset_id}"
          ${selected.has(Number(item.asset_id)) ? "checked" : ""}
          ${item.usable_for_video && item.kind !== "spatial_blocking"
            ? "" : "disabled"}>
        <img src="${esc(thumbUrl(item.url, 260))}" loading="lazy" alt="${esc(item.label)}">
        <span>${esc(item.label)}</span>
        <small>${esc(item.quality || "medium")}${item.kind === "spatial_blocking"
          ? " · 系统自动加入" : (item.usable_for_video ? "" : " · 低质量禁用")}</small>
      </label>`).join("") || `<div class="dim">资产中心还没有可选图片。</div>`}</div>
    <div class="script-actions">
      <button class="reset-auto" title="撤销人工选择,回到按剧本自动选入分镜图/人物立绘/场景图">↺ 恢复自动选入</button>
      <button class="primary save">保存参考图</button></div>
  </div>`;
  const close = () => overlay.remove();
  overlay.querySelector(".close").onclick = close;
  overlay.addEventListener("click", (ev) => { if (ev.target === overlay) close(); });
  overlay.querySelector(".reset-auto").onclick = async (ev) => {
    ev.target.disabled = true; ev.target.textContent = "恢复中…";
    try {
      await api("/api/video/references", {
        method: "POST", headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ episode_id: episodeId, shot_no: shotNo,
          reset: true }),
      });
      close();
      showToast(`镜头 ${shotNo} 已恢复自动选入必要参考图`, "ok");
      renderCanvasView(episodeId);
    } catch (e) {
      showToast(e.message, "error");
      ev.target.disabled = false; ev.target.textContent = "↺ 恢复自动选入";
    }
  };
  overlay.querySelector(".save").onclick = async (ev) => {
    const ids = [...overlay.querySelectorAll("input:checked")]
      .map((input) => Number(input.value));
    if (ids.length > manualLimit) {
      showToast(`每个镜头最多选择 ${manualLimit} 张资产参考图`, "error");
      return;
    }
    ev.target.disabled = true; ev.target.textContent = "保存中…";
    try {
      await api("/api/video/references", {
        method: "POST", headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ episode_id: episodeId, shot_no: shotNo,
          asset_ids: ids }),
      });
      close();
      showToast(`镜头 ${shotNo} 已保存 ${ids.length} 张资产参考图`, "ok");
      renderCanvasView(episodeId);
    } catch (e) {
      showToast(e.message, "error");
      ev.target.disabled = false; ev.target.textContent = "保存参考图";
    }
  };
  document.body.appendChild(overlay);
}

function bindVideoReferenceControls(data, episodeId) {
  document.querySelectorAll(".video-ref-preview").forEach((button) => {
    button.onclick = () => showImageLightbox(
      button.dataset.imageUrl, button.dataset.imageTitle);
  });
  document.querySelectorAll(".video-ref-edit").forEach((button) => {
    button.onclick = () => showVideoReferencePicker(
      data, episodeId, Number(button.dataset.shotNo));
  });
}

function renderQueuedSeriesView(data) {
  const batch = data.series_batch || {};
  const source = data.series_source || {};
  const current = batch.current;
  const isNext = batch.next && batch.next.episode_id === data.episode.id;
  app.innerHTML = `<div class="canvas-view queued-series-view">
    <div class="canvas-toolbar"><button id="queued-back">← 仪表盘</button>
      <span class="title">《${esc(data.project.title)}》第${data.episode.number}集</span>
      ${chip(data.episode.status)}</div>
    <section class="panel queued-series-card">
      <span class="eyebrow">SERIES QUEUE · ${source.position || "-"}/${source.total || "-"}</span>
      <h1>${esc(data.episode.title || `第${data.episode.number}集`)}</h1>
      <p>本集已经从 <b>${esc(source.filename || "多集文档")}</b> 导入，正在等待前一集完整通过。系统不会并行生图。</p>
      <div class="queued-source-mode">${source.mode === "script"
        ? "✓ 已识别完整剧本；轮到本集时直接进入剧本审阅"
        : "✦ 已识别剧情梗概；轮到本集时先由 AI 编写脚本，再进入审阅"}</div>
      <blockquote>${esc(String(source.source_text || "").replace(/\s+/g, " ").slice(0, 500))}</blockquote>
      <div class="series-actions">
        ${current ? `<a class="primary button-link" href="#/episode/${current.episode_id}">先完成当前第${current.episode_number}集 →</a>` : ""}
        ${!current && isNext ? `<button class="primary" id="queued-start">开始本集</button>` : ""}
      </div>
    </section>
  </div>`;
  document.getElementById("queued-back").onclick = () => { location.hash = "#/"; };
  document.getElementById("queued-start")?.addEventListener("click", async (event) => {
    const button = event.currentTarget;
    button.disabled = true; button.textContent = "正在准备…";
    try {
      await api("/api/series/next", {
        method: "POST", headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ batch_id: batch.id }),
      });
      renderCanvasView(data.episode.id);
    } catch (error) {
      showToast(error.message, "error");
      button.disabled = false; button.textContent = "开始本集";
    }
  });
}

function pausedProductionState(data) {
  const episode = data.episode || {};
  const items = ((data.render_plan || {}).items) || [];
  const ready = items.filter(
    (item) => ["done", "reused"].includes(item.status)).length;
  const tasks = data.tasks || [];
  const downstreamStages = new Set([
    "cast", "storyboard", "blocking", "images", "frames", "preflight",
    "videos", "voice", "edit", "qc",
  ]);
  const hasDownstreamTask = tasks.some((task) =>
    downstreamStages.has(task.stage)
    && ["done", "failed", "stopped", "cancelling"].includes(task.status));
  const active = episode.status === "awaiting_script" && !!data.script
    && (!!data.storyboard || ready > 0 || hasDownstreamTask);
  return {
    active,
    ready,
    total: items.length,
    shots: (data.storyboard?.shots || []).length,
    blockingScenes: (data.blocking?.scenes || []).length,
    images: Object.keys((data.artifacts || {}).images || {}).length,
  };
}

function pausedProductionAccessHtml(state) {
  if (!state.active) return "";
  return `<section class="paused-production-access" aria-label="暂停后的制作环节入口">
    <div class="paused-production-summary">
      <div><b>⏸ 制作已暂停，已有内容仍可查看</b>
        <span>暂停只停止继续生成，不会锁住剧本、人物、场景、分镜或图片。
        「继续补齐」只负责恢复剩余生产。</span></div>
      <strong>${state.ready}/${state.total || 0} 项已完成 ·
        ${state.images}/${state.shots || 0} 张分镜图</strong>
    </div>
    <nav class="paused-stage-links" aria-label="查看已完成的生产环节">
      <button type="button" data-paused-access="script"><b>01</b> 剧本与制作圣经</button>
      <button type="button" data-paused-access="assets"><b>02</b> 人物 / 场景</button>
      <button type="button" data-paused-access="storyboard"><b>03</b> 分镜列表
        <span>${state.shots}</span></button>
      <button type="button" data-paused-access="blocking"><b>04</b> 3D 空间调度
        <span>${state.blockingScenes}</span></button>
      <button type="button" data-paused-access="images"><b>05</b> 图片清单
        <span>${state.ready}/${state.total || 0}</span></button>
      <button type="button" data-paused-access="canvas"><b>06</b> 全流程画布</button>
    </nav>
  </section>`;
}

async function renderCanvasView(episodeId) {
  if (pollTimer) { clearInterval(pollTimer); pollTimer = null; }
  let data;
  try { data = await api(`/api/episode/${episodeId}`); }
  catch (e) { app.innerHTML = `<div class="loading">加载失败:${esc(e.message)}</div>`; return; }
  canvasSignature = canvasSig(data);

  const ep = data.episode, sb = data.storyboard, script = data.script;
  const pausedProduction = pausedProductionState(data);
  topbarRight.innerHTML = chip(ep.status);
  if (ep.status === "queued_script") {
    renderQueuedSeriesView(data);
    return;
  }
  if (ep.status === "awaiting_script" && script && !pausedProduction.active) {
    renderScriptReview(data, episodeId);
    return;
  }
  if (ep.status === "awaiting_cast" && script) {
    renderCastSelection(data, episodeId);
    return;
  }
  // 制作进行中一律进生产直播页(实况看板+日志+停止),画布留给审阅/成片
  const stable = ["done", "failed", "qc_failed", "created",
    "awaiting_script", "awaiting_cast", "awaiting_confirm", "queued_script"];
  if (!stable.includes(ep.status)) {
    renderProductionView(data, episodeId);
    return;
  }
  if (!sb) {
    // 只要有剧本、或制作已失败/中断,都必须给"从断点接着做"的入口,
    // 不能让用户面对一句"尚无分镜"无路可走
    if (script || ["failed", "qc_failed"].includes(ep.status)) {
      renderRecoveryView(data, episodeId);
      return;
    }
    app.innerHTML = `<div class="loading">本集尚无分镜(制作进行中或未开始)。<a href="#/">返回仪表盘</a></div>`;
    pollCanvas(episodeId);
    return;
  }

  const productionGuidance = productionGuidanceModel(data);
  const guidanceBlocksSeedance = !productionGuidance.canConfirmSeedance
    && ["keyframes", "frames"].includes(productionGuidance.phase);
  const guidanceStatusLabel = productionGuidance.phase === "keyframes"
    && guidanceBlocksSeedance ? "关键帧待补齐"
    : productionGuidance.currentLabel;
  if (guidanceBlocksSeedance) {
    topbarRight.innerHTML = `<span class="chip awaiting_confirm">${esc(
      guidanceStatusLabel)}</span>`;
  }

  // 质检问题按镜头/台词索引
  const shotIssues = storyboardShotIssues(data), lineIssues = {};
  (data.qc_report?.issues || []).forEach((i) => {
    if (i.line_no != null) (lineIssues[i.line_no] = lineIssues[i.line_no] || []).push(i);
  });

  const awaiting = ep.status === "awaiting_confirm"
    && productionGuidance.canConfirmSeedance;
  const revisionState = data.shot_revision_state || {};
  const revisedShot = revisionState.active ? revisionState.shot_no : null;
  const preflightReady = !!data.preflight?.passed
    && revisionState.formal_ready !== false;
  const profile = data.production_profile || {};
  const videoDefault = (data.quality_policy || {}).video_default || "auto";
  const gates = data.preflight?.gates || [];
  const lastFailed = ["failed", "qc_failed"].includes(ep.status)
    ? [...(data.tasks || [])].reverse().find((t) => t.status === "failed")
    : null;
  const firstImageFailure = (data.image_failures || [])[0] || null;
  app.innerHTML = `
  <div class="canvas-view">
    ${pausedProductionAccessHtml(pausedProduction)}
    ${["failed", "qc_failed"].includes(ep.status) ? `
    <div class="confirm-banner fail-banner">
      <div>
        <b>${lastFailed ? `上次制作在「${esc(STAGE_CN[lastFailed.stage] || lastFailed.stage)}」失败 ⚠️`
          : (ep.status === "qc_failed" ? "成片质检未通过 ⚠️" : "上次制作失败 ⚠️")}</b>
        <span>${firstImageFailure
          ? `${data.image_failures.length} 张关键帧二次质检未过，已隔离等待人工修改；其他关键帧已经继续完成，失败稿不会进入正式资产。`
          : `${lastFailed ? esc((lastFailed.error || "").slice(0, 200)) + ";" : ""}
        已完成的剧本/人物/图片/视频全部保留,点右侧按钮从断点接着做,只补缺失部分,不重复消耗额度。`}</span>
      </div>
      <button class="primary" id="btn-resume-canvas">${
        firstImageFailure
          ? `定位并处理 ${data.image_failures.length} 张问题图`
          : "▶ 从断点继续制作"}</button>
    </div>` : ""}
    ${awaiting ? `
    <div class="confirm-banner">
      <div>
        <b>${revisedShot != null
          ? revisionState.formal_ready === false
            ? `镜头 ${revisedShot} 当前是低质量试错版，不能交给 Seedance`
            : `镜头 ${revisedShot} 已更新，相关旧视频和旧成片已自动作废`
          : preflightReady
            ? `${gates.length} 项生产门禁通过，请做最终视觉确认`
            : "生产门禁未通过"}</b>
        <span>${revisedShot != null
          ? revisionState.formal_ready === false
            ? "请直接在下方分镜表把本镜改用中/高质量重画；低质量图只用于试错，确认按钮不会放行。"
            : "新关键帧、同场首尾帧链和 Seedance 手选参考已经同步；现在只需确认一次，系统只重拍受影响镜头，再重剪与复检。"
          : "角色/场景连续性、五维分镜、文字关键帧、首尾帧和即梦配置均已机检。确认后才会消耗 Seedance 额度；成片仍须通过检查板、内容复核与交付脚本。"}</span>
        <label class="video-quality-choice">Seedance 质量
          <select id="seedance-quality">
            <option value="auto" ${videoDefault === "auto" ? "selected" : ""}>自动 · 中档 720P（默认）</option>
            <option value="low" ${videoDefault === "low" ? "selected" : ""}>低档 · 480P</option>
            <option value="medium" ${videoDefault === "medium" ? "selected" : ""}>中档 · 720P</option>
            <option value="high" ${videoDefault === "high" ? "selected" : ""}>高档 · 1080P</option>
          </select>
        </label>
      </div>
      <button class="primary" id="btn-confirm" ${preflightReady ? "" : "disabled"}>${
        revisedShot != null ? "✅ 确认，只重拍受影响镜头" : "✅ 确认,开始 Seedance 生产"}</button>
    </div>
    ${videoReferencePanelHtml(data)}` : ""}
    <div class="profile-strip">
      <span><b>${esc(profile.standard_name || "SK 五维工业流")}</b> v${esc(profile.standard_version || 1)}</span>
      <span>Seedance 2.0 Fast VIP</span><span>${esc(profile.resolution || "720p")}</span>
      <span>Seedance2 随视频配音</span><span>口型同步</span><span>无字幕母版</span>
      <strong>${gates.filter((g) => g.passed).length}/${gates.length || 0} 门禁通过</strong>
      <a href="#/standards/history">查看制作标准</a>
    </div>
    ${mockWarnHtml(data)}
    <div class="canvas-toolbar">
      <button id="btn-back">← 仪表盘</button>
      <span class="title">《${esc(ep.project_title || data.project.title)}》第${ep.number}集</span>
      ${guidanceBlocksSeedance
        ? `<span class="chip awaiting_confirm">${esc(guidanceStatusLabel)}</span>`
        : chip(ep.status)}
      <span class="hint">质检 ${ep.qc_score == null ? "-" : fmt(ep.qc_score, 0)} 分 · 成本 ${fmt(ep.cost)}</span>
      <span class="spacer"></span>
      <div class="zoom-group view-toggle">
        <button id="view-theater">📋 分镜表</button>
        <button id="view-canvas">🗺 画布</button>
      </div>
      <button id="btn-play" class="primary">▶ 播放本集</button>
      <button id="btn-script">剧本</button>
      <button id="btn-blocking" title="人物走位、镜头位置、视锥和轴线">🧭 空间调度</button>
      <button id="btn-plan" title="每张图的状态与提示词;可单张改词重画">🖼 图片清单</button>
      <button id="btn-stop-live" class="stop-btn" hidden
        title="暂停生成:已完成的图片全部保留,可从断点继续">⏸ 暂停生成</button>
      <button id="btn-reproduce" title="复用已完成的部分,只补做缺失内容">继续补齐</button>
      <button id="btn-reproduce-force" class="danger"
        title="推翻原有设定,清理本轮复用并从头重新生成图片、首尾帧和视频">⚠ 全部重新生成</button>
      <div class="zoom-group">
        <button id="zoom-out">−</button>
        <span class="zoom-pct" id="zoom-pct">100%</span>
        <button id="zoom-in">＋</button>
        <button id="zoom-fit">适应</button>
        <button id="layout-reset">重排</button>
      </div>
    </div>
    <div class="canvas-stage-nav" id="canvas-stage-nav" hidden
      aria-label="全生产链画布导航"></div>
    <div id="live-strip" class="live-strip" hidden></div>
    <div class="canvas-body">
      <div id="theater"></div>
      <div id="viewport" hidden><div id="world"></div></div>
      <aside id="sidepanel"></aside>
    </div>
    <div class="timeline" id="timeline" hidden></div>
  </div>`;

  document.getElementById("btn-back").onclick = () => { location.hash = "#/"; };
  const stopLive = document.getElementById("btn-stop-live");
  if (stopLive) {
    const producingNow = !["done", "failed", "qc_failed", "created",
      "awaiting_script", "awaiting_cast", "awaiting_confirm"].includes(ep.status);
    const strip = document.getElementById("live-strip");
    if (strip) strip.hidden = !producingNow;
    if (producingNow) { updateLiveStrip(data); startLiveTicker(episodeId); }
    stopLive.hidden = !producingNow;
    stopLive.onclick = () => {
      stopLive.disabled = true;
      stopLive.textContent = "暂停中…";
      stopEpisode(ep.id);
    };
  }
  const reproduce = async (force) => {
    try {
      await api("/api/produce", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({
          title: data.project.title, episode: ep.number, force,
        }),
      });
      showToast(force ? "已提交全部重新生成,画布将自动刷新" : "已提交断点补齐,画布将自动刷新", "ok");
      pollCanvas(episodeId);
    } catch (e) { showToast(e.message, "error"); }
  };
  const reproduceButton = document.getElementById("btn-reproduce");
  if (productionGuidance.actions.pendingImages.enabled) {
    const pendingCount = productionGuidance.actions.pendingImages.count;
    reproduceButton.textContent = `继续补齐 ${pendingCount} 张`;
    reproduceButton.title = "只生产尚未生成的关键帧，不重做已通过图片，也不启动 Seedance";
    reproduceButton.onclick = (event) => armConfirm(
      event.currentTarget, `补齐 ${pendingCount} 张`,
      () => submitPendingKeyframes(data, episodeId, event.currentTarget));
  } else if (firstImageFailure) {
    reproduceButton.textContent = "先处理问题图";
    reproduceButton.title = "二次质检未过的关键帧人工修好后，再从断点补齐";
    reproduceButton.onclick = () => focusImageFailureShot(
      document, data, Number(firstImageFailure.shot_no));
  } else {
    reproduceButton.onclick = (ev) =>
      armConfirm(ev.target, "补齐", () => reproduce(false));
  }
  document.getElementById("btn-reproduce-force").onclick = (ev) =>
    armConfirm(ev.target, "全部重新生成", () => reproduce(true));
  const btnResumeCanvas = document.getElementById("btn-resume-canvas");
  if (btnResumeCanvas) {
    btnResumeCanvas.onclick = firstImageFailure
      ? () => focusImageFailureShot(
        document, data, Number(firstImageFailure.shot_no))
      : () => {
        btnResumeCanvas.disabled = true;
        btnResumeCanvas.textContent = "已提交,续跑中…";
        reproduce(false);
      };
  }
  document.getElementById("btn-script").onclick = () => showScriptOverlay(data, episodeId);
  document.getElementById("btn-blocking").onclick = () => showBlockingOverlay(episodeId);
  document.getElementById("btn-plan").onclick = () => showPlanOverlay(episodeId);
  document.getElementById("btn-play").onclick = () => openPlayer(data);
  if (awaiting) bindVideoReferenceControls(data, episodeId);
  const btnConfirm = document.getElementById("btn-confirm");
  if (btnConfirm) btnConfirm.onclick = async () => {
    btnConfirm.disabled = true;
    btnConfirm.textContent = "已确认,生产中…";
    try {
      await api("/api/confirm", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ episode_id: ep.id,
          video_quality: document.getElementById("seedance-quality")?.value || "auto" }),
      });
      showToast("已确认!正在生成 Seedance 视频、随视频配音/口型与无字幕母版", "ok");
      pollCanvas(episodeId);
    } catch (e) {
      showToast(e.message, "error");
      btnConfirm.disabled = false;
      btnConfirm.textContent = revisedShot != null
        ? "✅ 确认，只重拍受影响镜头"
        : "✅ 确认,开始 Seedance 生产";
    }
  };
  // 制作进行中自动刷新画布(待确认是稳定状态,不轮询)
  if (!["done", "failed", "qc_failed", "created",
        "awaiting_script", "awaiting_cast", "awaiting_confirm"].includes(ep.status))
    pollCanvas(episodeId);

  const canvas = new StoryboardCanvas(data, shotIssues, lineIssues);
  canvas.mount();
  renderTheater(data, canvas);

  // 分镜生产表(默认)/ 自由画布双视图；播放器仍从表头和逐镜按钮进入。
  const theaterEl = document.getElementById("theater");
  const viewportEl = document.getElementById("viewport");
  const timelineEl = document.getElementById("timeline");
  const btnTheater = document.getElementById("view-theater");
  const btnCanvas = document.getElementById("view-canvas");
  const sidepanelEl = document.getElementById("sidepanel");
  const stageNavEl = document.getElementById("canvas-stage-nav");
  const externalReviewPanels = [...document.querySelectorAll(".video-ref-panel")];
  const setView = (mode) => {
    localStorage.setItem("aifos.view", mode);
    const theaterMode = mode !== "canvas";
    theaterEl.hidden = !theaterMode;
    viewportEl.hidden = theaterMode;
    timelineEl.hidden = theaterMode;
    if (stageNavEl) stageNavEl.hidden = theaterMode;
    externalReviewPanels.forEach((panel) => { panel.hidden = !theaterMode; });
    btnTheater.classList.toggle("active", theaterMode);
    btnCanvas.classList.toggle("active", !theaterMode);
    if (window.matchMedia("(max-width: 780px)").matches)
      sidepanelEl.hidden = true;
    if (!theaterMode) canvas.fit();
  };
  btnTheater.onclick = () => setView("theater");
  btnCanvas.onclick = () => setView("canvas");
  setView(localStorage.getItem("aifos.view") || "theater");
  app.querySelectorAll("[data-paused-access]").forEach((button) => {
    button.onclick = () => {
      const target = button.dataset.pausedAccess;
      if (target === "script") {
        showScriptOverlay(data, episodeId);
      } else if (target === "blocking") {
        showBlockingOverlay(episodeId);
      } else if (target === "images") {
        showPlanOverlay(episodeId);
      } else if (target === "canvas") {
        setView("canvas");
        document.getElementById("canvas-stage-nav")?.scrollIntoView({
          behavior: "smooth", block: "nearest",
        });
      } else {
        setView("theater");
        const selector = target === "storyboard"
          ? ".shot-production-section" : ".production-ledger";
        requestAnimationFrame(() => document.querySelector(selector)?.scrollIntoView({
          behavior: "smooth", block: "start",
        }));
      }
    };
  });
}

/* ---- 剧本正文(审阅页与生产直播页共用) ---- */
function scriptBodyHtml(script) {
  const imported = script.import_analysis || {};
  const logic = script.script_logic_audit || {};
  const corrections = imported.entity_corrections || [];
  const logicIssues = Array.isArray(logic.issues) ? logic.issues : [];
  const logicSummary = logic.schema ? `
      <div class="${logic.passed ? "done" : "resume-banner"}">
        <b>${logic.passed ? "✓ 剧本第一道总闸门已通过" : "⚠ 剧本总闸门未通过"}</b>：
        ${esc(logic.summary || "等待编剧完成综合审查")}。
        <span class="dim">已一次检查因果、人物动机与信息、物理、时间、空间、
        道具生命周期、可拍性和局部返编边界。</span>
        ${logicIssues.length ? `<details><summary>查看 ${logicIssues.length} 项待修问题</summary>
          <ul>${logicIssues.slice(0, 12).map((item) => `<li>${esc(item)}</li>`).join("")}</ul>
          </details>` : ""}
      </div>` : "";
  const importSummary = imported.dialogue_count ? `
      <div class="resume-banner">
        <b>✓ 小说 / 剧本智能解析完成${imported.writer_adapted
          ? " · 已完成影视化改编" : ""}</b>：
        识别 ${Number(imported.dialogue_count || 0)} 句对白、
        ${Number(imported.character_count || 0)} 名人物、
        ${Number(imported.scene_count || 0)} 个场景。
        ${imported.unresolved_dialogue_count
          ? `<span class="warn">${Number(imported.unresolved_dialogue_count)} 句说话人待人工核对（已标为“待确认说话人”）。</span>`
          : "说话人均已识别。"}
        ${corrections.length
          ? `<span class="ok">已纠正 ${corrections.length} 个误识别标签：
            ${corrections.map((item) => `${esc(item.raw_label)} → ${esc(item.canonical_name)}`).join("、")}。</span>`
          : ""}
        ${imported.performance_cue_count
          ? `<span class="dim">另保留 ${Number(imported.performance_cue_count)} 条语气/动作供配音与表演使用。</span>`
          : ""}
        <span class="dim">${imported.writer_adapted
          ? "原素材已保留为导入依据；正式剧本允许为因果、物理、连续性和道具逻辑改写。"
          : "当前仍是解析稿；锁定前需通过剧本总闸门。"}</span>
      </div>` : "";
  return `
      <h1>${esc(script.episode_title || "本集剧本")}</h1>
      <p class="logline">${esc(script.logline || "")}</p>
      ${importSummary}
      ${logicSummary}
      ${storyBibleHtml(script)}
      <div class="cast">${(script.characters || []).map((c) =>
        `<span class="chip">${esc(c.name)} · ${esc(c.role || "")}</span>`).join("")}</div>
      <div class="script-character-profiles">${(script.characters || [])
        .map(characterProfileHtml).join("")}</div>
      ${corePropsHtml(script)}
      ${script.scenes.map((s) => `
        <section class="scene">
          <div class="scene-head"><span class="scene-no">第 ${s.scene_no} 场</span>
            <span class="scene-loc">${esc(s.location)}</span></div>
          ${s.action ? `<p class="action">△ ${esc(s.action)}</p>` : ""}
          ${(s.lines || []).map((l) => `
            <div class="line-block">
              <div class="speaker">${esc(l.character)}</div>
              <div class="speech">${esc(l.dialogue)}
                ${l.performance ? `<small class="performance-cue">表演：${esc(l.performance)}</small>` : ""}
              </div>
            </div>`).join("")}
        </section>`).join("")}`;
}

/* ---- 生产直播页:每一步实时可见,剧本一出即可阅读,可随时停止 ---- */
function renderProductionView(data, episodeId) {
  const ep = data.episode;
  const done = new Set((data.tasks || [])
    .filter((t) => t.status === "done").map((t) => t.stage));
  const runningTask = (data.tasks || []).find((t) => t.status === "running");
  const runningStage = ep.status === "cancelling" ? "cancelling"
    : (runningTask && runningTask.stage) || ep.status;
  const stopping = ep.status === "cancelling";
  app.innerHTML = `
  <div class="canvas-view">
    <div class="canvas-toolbar">
      <button id="btn-back">← 仪表盘</button>
      <span class="title">《${esc(data.project.title)}》第${ep.number}集</span>
      ${chip(ep.status)}
      <span class="spacer"></span>
      <button id="btn-plan-live"
        title="查看每张图片的状态、提示词和参考图">🖼 图片清单</button>
      <button id="btn-stop" class="stop-btn big" ${stopping ? "disabled" : ""}
        title="暂停生成:已完成的图片全部保留,可从断点继续">${stopping ? "暂停中…" : "⏸ 暂停生成"}</button>
    </div>
    <div id="live-strip" class="live-strip"></div>
    <div class="produce-live">
      <div class="panel">
        <h2>${stopping ? "⏸ 正在暂停,已画完的全部保留…" : "制作进行中"} · ${esc(STAGE_PLAIN[runningStage] || "准备中")}…</h2>
        <ol class="stage-steps">
          ${STAGE_ORDER.map((stage) => {
            const state = done.has(stage) ? "done"
              : stage === runningStage ? "run" : "todo";
            let extra = "";
            if (stage === "images" && data.storyboard)
              extra = `(${Object.keys((data.artifacts || {}).images || {}).length}/${data.storyboard.shots.length})`;
            if (stage === "videos" && data.storyboard)
              extra = `(${Object.keys((data.artifacts || {}).videos || {}).length}/${data.storyboard.shots.length})`;
            return `<li class="${state}">${state === "done" ? "✓"
              : state === "run" ? "⏳" : "○"} ${esc(STAGE_CN[stage] || stage)}${extra}</li>`;
          }).join("")}
        </ol>
        <div class="dim">每完成一步自动点亮;真实产线(出图/视频)单步可能要几分钟,
        看上方状态条的秒表在走就没卡住。</div>
        <h2 style="margin-top:14px">产线实时日志</h2>
        <div class="log-list" id="live-log"><div class="dim">加载中…</div></div>
      </div>
      <div class="produce-main">
        ${productionLedgerHtml(data, { context: "live" })}
        ${imageAccelerationLivebarHtml(data)}
        ${data.storyboard ? `${shotProductionTableHtml(data, {
          shotIssues: storyboardShotIssues(data), context: "live" })}
          <details class="asset-production-details">
            <summary>🖼 查看人物、场景及全部图片资产生产清单</summary>
            ${renderPlanBoardHtml(data)}
          </details>` : renderPlanBoardHtml(data)}
        ${data.script ? `<div class="script-review">
          <div class="dim" style="margin-bottom:6px">📖 剧本已就绪,可边生产边阅读:</div>
          ${scriptBodyHtml(data.script)}
        </div>` : `<div class="panel dim">剧本生成中,写好会第一时间显示在这里…</div>`}
      </div>
    </div>
  </div>`;
  document.getElementById("btn-back").onclick = () => { location.hash = "#/"; };
  document.getElementById("btn-plan-live").onclick = () => showPlanOverlay(episodeId);
  bindImageAccelerationLivebar(episodeId);
  bindProductionLedger(app, data, episodeId);
  document.getElementById("btn-stop").onclick = (ev) => {
    ev.target.disabled = true;
    ev.target.textContent = "停止中…";
    stopEpisode(ep.id);
  };
  updateLiveStrip(data);
  bindPlanSelection(app, data, episodeId);
  bindShotProductionTable(app, data);
  startLiveTicker(episodeId);
  pollCanvas(episodeId);
}

/* ---- 出图产线快速切换:Codex / Seedream / OpenAI 图片API + 并行路数 ---- */
function imageLineControlsHtml() {
  return `<div class="style-row image-line-row">
    <label>出图策略</label>
    <select id="image-line" disabled><option>加载中…</option></select>
    <label class="il-label">每通道图片并行</label>
    <select id="parallel-images" disabled><option>…</option></select>
    <label class="il-label">视频并行</label>
    <select id="parallel-videos" disabled><option>…</option></select>
    <label class="il-label">质检产线</label>
    <select id="qc-line" disabled><option>…</option></select>
    <span class="dim" id="image-line-hint"></span>
  </div>`;
}

/* 界面已更新但服务进程还是旧版时,接口会报旧错误 → 给出重启指引 */
function staleServerHint(e) {
  const msg = String((e && e.message) || e);
  if (msg.includes("缺少 provider") || msg.includes("未知能力")
      || msg.includes("不支持的默认项")) {
    return "服务还在运行旧版本(界面已是新版):关掉 AIFOS 窗口再双击"
      + " start_aifos.command 重启一次即可;此后更新全自动,无需再手动重启";
  }
  return msg;
}

async function bindImageLineControls() {
  const lineSel = document.getElementById("image-line");
  const parSel = document.getElementById("parallel-images");
  const videoParSel = document.getElementById("parallel-videos");
  if (!lineSel || !parSel || !videoParSel) return;
  let st;
  try { st = await api("/api/settings"); } catch (e) { return; }
  const byName = {};
  (st.providers || []).forEach((pv) => { byName[pv.name] = pv; });
  const ready = (n) => {
    const pv = byName[n] || (n === "seedream5_lite"
      ? byName.seedream_lite || byName.seedream : null);
    return pv && pv.enabled && (pv.checks || []).some(
      (c) => c.capability === "image" && c.ok) ? "已接通" : "未接通";
  };
  lineSel.innerHTML = `
    <option value="smart">智能分流 · 批量 Seedream / 重要高清 Codex</option>
    <option value="codex">全部 Codex 优先(${ready("codex")})</option>
    <option value="seedream5_lite">全部 Seedream 5.0 Lite 优先(${ready("seedream5_lite")})</option>
    <option value="image_api">全部 OpenAI 图片 API 优先(${ready("image_api")})</option>`;
  const currentStrategy = st.image_strategy || "smart";
  if (currentStrategy === "custom")
    lineSel.insertAdjacentHTML("afterbegin",
      `<option value="custom">当前：高级自定义路由</option>`);
  lineSel.value = currentStrategy;
  lineSel.disabled = false;
  lineSel.title = "选择后同步应用到人物、场景、镜头、首尾帧和封面；不可用时自动回退";
  const hint = document.getElementById("image-line-hint");
  let savedStrategy = currentStrategy;
  const updateImageLineHint = () => {
    if (!hint) return;
    const notes = {
      smart: "按用途自动选择；批量：Seedream 5.0 Lite → GPT Image 2 medium"
        + "；重要高清：Codex 订阅 → GPT Image 2 medium",
      codex: "人物、场景、镜头、首尾帧、封面全部 Codex 优先；失败自动回退",
      seedream5_lite: "全部图片 Seedream 5.0 Lite 优先；复杂文字/终稿也会按此选择",
      image_api: "全部图片 OpenAI 图片 API 优先；按 API 实际用量计费",
      custom: "当前使用设置中心保存的高级自定义图片路由",
    };
    hint.textContent = notes[lineSel.value] || notes.smart;
  };
  updateImageLineHint();
  lineSel.onchange = async () => {
    const pick = lineSel.value;
    if (pick === "custom") return;
    lineSel.disabled = true;
    try {
      await api("/api/settings", {
        method: "POST", headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ image_strategy: pick }),
      });
      showToast(`出图策略已切换：${lineSel.options[lineSel.selectedIndex].textContent}`
        + "，下一张图开始生效", "ok");
      savedStrategy = pick;
      updateImageLineHint();
    } catch (e) {
      lineSel.value = savedStrategy;
      updateImageLineHint();
      showToast(staleServerHint(e), "error");
    } finally { lineSel.disabled = false; }
  };
  parSel.innerHTML = [1, 2, 3, 4, 6, 8].map(
    (n) => `<option value="${n}">${n} 路/通道</option>`).join("");
  parSel.value = String((st.defaults || {}).parallel_images || 3);
  parSel.disabled = false;
  parSel.onchange = async () => {
    try {
      await api("/api/settings", {
        method: "POST", headers: { "Content-Type": "application/json" },
        body: JSON.stringify({
          defaults: { parallel_images: Number(parSel.value) } }),
      });
      const channels = Math.max(1,
        Number(st?.codex_parallel?.enabled_count) || 1);
      showToast(`每通道已设为 ${parSel.value} 路，当前 ${channels} 条通道`
        + `合计 ${Number(parSel.value) * channels} 路，下一批生效`, "ok");
    } catch (e) {
      showToast(staleServerHint(e), "error");
    }
  };
  videoParSel.innerHTML = [1, 2, 3, 4, 6, 8].map(
    (n) => `<option value="${n}">${n} 路并行</option>`).join("");
  videoParSel.value = String((st.defaults || {}).parallel_videos || 4);
  videoParSel.disabled = false;
  videoParSel.onchange = async () => {
    try {
      await api("/api/settings", {
        method: "POST", headers: { "Content-Type": "application/json" },
        body: JSON.stringify({
          defaults: { parallel_videos: Number(videoParSel.value) } }),
      });
      showToast(`视频生产已设为 ${videoParSel.value} 路并行，`
        + "下一批 Seedance 生效", "ok");
    } catch (e) {
      showToast(staleServerHint(e), "error");
    }
  };
  const qcSel = document.getElementById("qc-line");
  if (qcSel) {
    const qready = (n) => {
      const pv = byName[n];
      return pv && pv.enabled && (pv.checks || []).some(
        (c) => c.capability === "image_qc" && c.ok) ? "已接通" : "未接通";
    };
    const qchain = (st.routing || {}).image_qc || [];
    qcSel.innerHTML = `
      <option value="codex">Codex 视觉质检(${qready("codex")})</option>
      <option value="image_api">OpenAI 视觉(${qready("image_api")})</option>
      <option value="claude">Claude 视觉(${qready("claude")})</option>
      <option value="claude_api">Claude API 视觉(${qready("claude_api")})</option>`;
    qcSel.value = ["codex", "image_api", "claude", "claude_api"]
      .find((x) => qchain[0] === x) || "codex";
    qcSel.disabled = false;
    qcSel.onchange = async () => {
      const pick = qcSel.value;
      const rest = ["codex", "image_api", "claude", "claude_api"]
        .filter((x) => x !== pick);
      try {
        await api("/api/settings", {
          method: "POST", headers: { "Content-Type": "application/json" },
          body: JSON.stringify({ capability: "image_qc",
            chain: [pick, ...rest, "mock"] }),
        });
        showToast(`质检产线已切换为 ${pick === "codex" ? "Codex 视觉"
          : pick.includes("claude") ? "Claude 视觉" : "OpenAI 视觉"}`, "ok");
      } catch (e) { showToast(staleServerHint(e), "error"); }
    };
  }
}

/* ---- 剧本审阅页:剧本确认后才开始画图(第一道确认) ----
   开画前在这里确定画风、上传参考图,出图全程按它们执行 */
const STYLE_PRESETS = ["现代乙女 3D 半写实", "现代都市漫剧", "国风漫剧", "校园清新日系",
  "水墨国风", "赛博朋克霓虹", "日系少年漫", "欧美卡通",
  "3D 渲染动画", "复古港漫", "治愈系水彩"];

function analysisText(value) {
  return Array.isArray(value) ? value.join("、") : (value == null ? "" : String(value));
}

function storyAnalysisEditorHtml(analysis, version) {
  if (!analysis) return `<section class="analysis-studio missing">
    <div class="analysis-head"><div><span class="eyebrow">STEP 02</span>
      <h2>AI 世界观与风格分析尚未完成</h2>
      <p>未形成制作圣经前不会开始生成人物和场景。</p></div>
      <button class="primary" id="analysis-retry">AI 重新分析</button></div></section>`;
  const n = analysis.narrative || {}, w = analysis.world || {};
  const v = analysis.visual || {}, p = analysis.prompt_bible || {};
  const styleSource = v.style_source === "user_override"
    ? "人工指定" : "AI 根据剧本生成";
  const sceneCards = (analysis.scenes || []).map((scene) => `
    <details class="analysis-scene">
      <summary>场 ${esc(scene.scene_no || "—")} · ${esc(scene.location || "未命名")}</summary>
      <div><b>空间：</b>${esc(scene.environment || "")}</div>
      <div><b>布局：</b>${esc(scene.layout || "")}</div>
      <div><b>材质道具：</b>${esc(scene.materials_and_props || "")}</div>
      <div><b>时段/天气：</b>${esc(scene.time_weather || "")}</div>
      <div><b>光线：</b>${esc(scene.lighting || "")}</div>
    </details>`).join("");
  const productionCharacters = (analysis.characters || [])
    .filter((character) => character.importance !== "背景路人");
  const characterCards = productionCharacters.map((character) => `
    <details class="analysis-scene">
      <summary>${character.importance === "待确认" ? "⚠️" : "🧬"}
        ${esc(character.name || "未命名")} · ${esc(character.importance || "角色")}</summary>
      <div><b>性别 / 年龄：</b>${esc(character.gender || "待确认")} ·
        ${esc(character.age_range || "待确认")}</div>
      <div><b>身份：</b>${esc(character.identity_facts || "")}</div>
      <div><b>视觉方向：</b>${esc(character.visual_direction || "")}</div>
      <div><b>最终人物出图提示词：</b>${esc(character.image_prompt
        || "尚未形成；不会进入人物出图")}</div>
      ${character.negative_prompt
        ? `<div><b>负面提示词：</b>${esc(character.negative_prompt)}</div>` : ""}
      ${character.importance === "待确认"
        ? `<div class="warn">系统无法从原文可靠判断这句由谁说。
          <button type="button" class="analysis-confirm-speaker">
            去编辑剧本确认说话人</button></div>` : ""}
    </details>`).join("");
  return `<section class="analysis-studio" data-version="${Number(version || 0)}">
    <div class="analysis-head"><div><span class="eyebrow">STEP 02 · AI PRODUCTION BIBLE</span>
      <h2>世界观、环境与视觉制作圣经</h2>
      <p>AI 已读完整剧本。锁定后，人物、场景、分镜、关键帧和 Seedance
      都继承同一套提示词，不再各自猜风格。</p></div>
      <div class="analysis-state">${analysis.locked ? "🔒 已锁定" : "待确认"} · v${Number(version || 1)}</div>
    </div>
    <div class="analysis-summary">
      <div><span>类型</span><b>${esc(n.genre || "待分析")}</b></div>
      <div><span>世界</span><b>${esc(w.name || "待分析")}</b></div>
      <div><span>时代地域</span><b>${esc(w.era_and_location || "待分析")}</b></div>
      <div><span>视觉媒介</span><b>${esc(v.medium || "待分析")}</b></div>
    </div>
    <div class="analysis-grid">
      <label><span>一句话故事</span><textarea id="analysis-logline">${esc(n.logline || "")}</textarea></label>
      <label><span>类型与受众</span><textarea id="analysis-genre">${esc(n.genre || "")}</textarea></label>
      <label><span>时代、地域与世界</span><textarea id="analysis-era">${esc(w.era_and_location || "")}</textarea></label>
      <label><span>世界硬规则</span><textarea id="analysis-rules">${esc(w.hard_rules || "")}</textarea></label>
      <label><span>社会、组织与生活方式</span><textarea id="analysis-social">${esc([w.social_order, w.culture_and_lifestyle].filter(Boolean).join("；"))}</textarea></label>
      <label><span>技术等级与关键道具</span><textarea id="analysis-tech">${esc(w.technology_and_props || "")}</textarea></label>
      <label class="wide"><span>本剧制作风格 · ${esc(styleSource)}（可调整）</span>
        <textarea id="analysis-style">${esc(v.user_style_constraint || "")}</textarea></label>
      <label><span>色彩与渲染质感</span><textarea id="analysis-palette">${esc([analysisText(v.palette), v.texture_and_render].filter(Boolean).join("；"))}</textarea></label>
      <label><span>光线设计</span><textarea id="analysis-light">${esc(v.lighting || "")}</textarea></label>
      <label><span>镜头语言</span><textarea id="analysis-camera">${esc(v.camera_language || "")}</textarea></label>
      <label><span>建筑与环境设计</span><textarea id="analysis-environment">${esc(v.architecture_and_environment || "")}</textarea></label>
      <label><span>人物服装与造型边界</span><textarea id="analysis-wardrobe">${esc(v.wardrobe_and_styling || "")}</textarea></label>
      <label><span>禁止出现</span><textarea id="analysis-forbid">${esc(analysisText(v.forbidden_visuals))}</textarea></label>
    </div>
    <details class="prompt-master"><summary>查看 / 调整后续生成提示词母版</summary>
      <div class="analysis-grid">
        <label class="wide"><span>全局图片提示词</span><textarea id="analysis-global">${esc(p.global_image_prefix || "")}</textarea></label>
        <label class="wide"><span>负面提示词</span><textarea id="analysis-negative">${esc(p.negative_prompt || "")}</textarea></label>
        <label><span>场景图前缀</span><textarea id="analysis-scene-prefix">${esc(p.scene_prefix || "")}</textarea></label>
        <label><span>关键帧前缀</span><textarea id="analysis-keyframe-prefix">${esc(p.keyframe_prefix || "")}</textarea></label>
        <label class="wide"><span>Seedance 视频前缀</span><textarea id="analysis-seedance-prefix">${esc(p.seedance_prefix || "")}</textarea></label>
      </div></details>
    <details class="analysis-scenes"><summary>逐场环境分析 · ${(analysis.scenes || []).length} 场</summary>
      <div class="analysis-scene-grid">${sceneCards || "暂无场景分析"}</div></details>
    <details class="analysis-scenes" open><summary>真实人物与最终出图卡 ·
      ${productionCharacters.length} 人</summary>
      <div class="analysis-scene-grid">${characterCards || "暂无人物分析"}</div></details>
    <div class="analysis-actions">
      <input id="analysis-direction" placeholder="可选补充，如：更考据、更克制、雨夜冷调；留空则完全按剧本重建">
      <button id="analysis-rerun">↻ AI 重新分析</button>
      <button id="analysis-save">保存制作圣经</button>
      <button id="analysis-copy">复制全局提示词</button>
    </div>
  </section>`;
}

function collectStoryAnalysis(analysis) {
  const next = JSON.parse(JSON.stringify(analysis || {}));
  next.narrative ||= {}; next.world ||= {}; next.visual ||= {};
  next.prompt_bible ||= {};
  const value = (id) => document.getElementById(id)?.value.trim() || "";
  const list = (text) => text.split(/[、，,；;\n]+/).map((x) => x.trim()).filter(Boolean);
  next.narrative.logline = value("analysis-logline");
  next.narrative.genre = value("analysis-genre");
  next.world.era_and_location = value("analysis-era");
  next.world.hard_rules = value("analysis-rules");
  next.world.social_order = value("analysis-social");
  next.world.technology_and_props = value("analysis-tech");
  const previousStyle = analysis?.visual?.user_style_constraint || "";
  next.visual.user_style_constraint = value("analysis-style");
  if (next.visual.user_style_constraint !== previousStyle)
    next.visual.style_source = "user_override";
  next.visual.palette = list(value("analysis-palette"));
  next.visual.lighting = value("analysis-light");
  next.visual.camera_language = value("analysis-camera");
  next.visual.architecture_and_environment = value("analysis-environment");
  next.visual.wardrobe_and_styling = value("analysis-wardrobe");
  next.visual.forbidden_visuals = list(value("analysis-forbid"));
  next.prompt_bible.global_image_prefix = value("analysis-global");
  next.prompt_bible.negative_prompt = value("analysis-negative");
  next.prompt_bible.scene_prefix = value("analysis-scene-prefix");
  next.prompt_bible.keyframe_prefix = value("analysis-keyframe-prefix");
  next.prompt_bible.seedance_prefix = value("analysis-seedance-prefix");
  return next;
}

function renderScriptReview(data, episodeId) {
  const script = data.script;
  const storyAnalysis = data.story_analysis || script.production_analysis || null;
  const resolvedStyle = storyAnalysis?.visual?.user_style_constraint
    || data.project.style || "";
  let analysisDraft = storyAnalysis;
  let analysisVersion = Number(data.story_analysis_version || 0);
  const refs = (data.artifacts || {}).references || [];
  const planItems = ((data.render_plan || {}).items) || [];
  const planReady = planItems.filter(
    (i) => ["done", "reused"].includes(i.status)).length;
  const resuming = planItems.length > 0 && planReady > 0
    && planReady < planItems.length;
  const attachOptions = [
    ...(script.characters || []).map((c) => c.name),
    ...[...new Set((script.scenes || []).map((s) => s.location))]];
  app.innerHTML = `
  <div class="canvas-view">
    <div class="prepro-steps" aria-label="预生产步骤">
      <div class="done"><b>01</b><span>剧本总闸门<small>改编 · 因果 · 道具 · 连续性</small></span></div>
      <div class="active"><b>02</b><span>AI 制作圣经<small>世界 · 环境 · 风格</small></span></div>
      <div><b>03</b><span>人工锁定<small>可改、可重分析</small></span></div>
      <div><b>04</b><span>开始生产<small>人物 · 场景 · 分镜</small></span></div>
    </div>
    <div class="confirm-banner">
      <div>
        <b>剧本总闸门与 AI 制作圣经已就绪，先确认再出图 📖</b>
        <span>检查因果、人物信息、时间空间、道具生命周期、故事、世界、环境和画风
        → 可改或重新分析 → 锁定后才开始画。
        此刻还没有消耗生图额度。</span>
      </div>
      <button class="primary" id="btn-script-ok">${resuming
        ? "▶ 锁定并从断点继续" : "🔒 锁定制作圣经并开始人物图"}</button>
    </div>
    ${resuming ? `<div class="resume-banner">⏸ 上次生成已暂停:
      图片 <b>${planReady}/${planItems.length}</b> 已完成并全部保留。
      点「▶ 从断点继续画图」只画剩余的,不重复消耗额度。</div>` : ""}
    ${imageAccelerationLivebarHtml(data)}
    <div class="canvas-toolbar">
      <button id="btn-back">← 仪表盘</button>
      <span class="title">《${esc(data.project.title)}》第${data.episode.number}集</span>
      ${chip(data.episode.status)}
      <span class="spacer"></span>
      <button id="btn-polish">✏️ 打磨剧本(意见重写/直接编辑/上传下载)</button>
    </div>
    <div class="style-panel">
      <h2>🎨 制作风格与参考 <span class="dim">AI 已按剧本分析；开始画图前可调整</span></h2>
      <div class="style-row">
        <label>本剧制作风格</label>
        <input id="style-input" list="style-presets"
          value="${esc(resolvedStyle)}"
          placeholder="默认由 AI 根据剧本生成；也可在此人工覆盖">
        <datalist id="style-presets">${STYLE_PRESETS.map((s) =>
          `<option>${esc(s)}</option>`).join("")}</datalist>
        <button id="style-save">保存画风</button>
      </div>
      ${imageLineControlsHtml()}
      <div class="style-row">
        <label>参考图</label>
        <input id="ref-name" placeholder="名称,如:女主官方设定(可留空)">
        <input id="ref-attach" list="ref-attach-list"
          placeholder="关联对象(留空=全项目):角色或场景名">
        <datalist id="ref-attach-list">${attachOptions.map((n) =>
          `<option>${esc(n)}</option>`).join("")}</datalist>
        <button class="primary" id="ref-upload">⬆ 上传参考图</button>
      </div>
      ${refs.length ? `<div class="pb-grid ref-grid">${refs.map((r) => `
        <div class="plan-card ref-card">
          <div class="pc-media">${r.url ? `<img src="${esc(thumbUrl(r.url, 480))}" loading="lazy" alt="">` : `<div class="pc-empty">🖼</div>`}</div>
          <div class="pc-label" title="${esc(r.name)}">${esc(r.name)}</div>
          <div class="dim">${r.attach_to ? "关联:" + esc(r.attach_to) : "全项目通用"}</div>
          <button class="ref-del" data-name="${esc(r.name)}">删除</button>
        </div>`).join("")}</div>`
      : `<div class="dim" style="margin-top:6px">还没有参考图。有官方设定图/画风样例就传上来,
         人物形象和画风会稳定得多;之后在「资产中心」也能管理。</div>`}
    </div>
    ${storyAnalysisEditorHtml(storyAnalysis, data.story_analysis_version)}
    <div class="script-review">${scriptBodyHtml(script)}</div>
  </div>`;
  document.getElementById("btn-back").onclick = () => { location.hash = "#/"; };
  document.getElementById("btn-polish").onclick = () =>
    showScriptOverlay(data, episodeId);
  app.querySelectorAll(".analysis-confirm-speaker").forEach((button) => {
    button.onclick = () => showScriptOverlay(data, episodeId);
  });
  bindImageAccelerationLivebar(episodeId);
  bindLightbox(app);
  bindImageLineControls();
  const post = (path, body) => api(path, {
    method: "POST", headers: { "Content-Type": "application/json" },
    body: JSON.stringify(body) });
  const rerunAnalysis = async (button) => {
    button.disabled = true;
    const oldText = button.textContent;
    button.textContent = "AI 分析中…";
    try {
      const reply = await post("/api/story-analysis", {
        episode_id: data.episode.id, action: "reanalyze",
        creative_direction:
          document.getElementById("analysis-direction")?.value.trim() || "",
      });
      showToast("AI 正在重读剧本并重建制作圣经", "ok");
      if (reply.job_id) pollCanvas(episodeId);
    } catch (e) {
      showToast(e.message, "error");
      button.disabled = false; button.textContent = oldText;
    }
  };
  const saveAnalysis = async (locked = false) => {
    if (!analysisDraft) throw new Error("制作圣经尚未生成，请先点 AI 重新分析");
    const style = document.getElementById("style-input").value.trim();
    const styleField = document.getElementById("analysis-style");
    if (style && styleField && style !== styleField.value.trim()) {
      styleField.value = style;
      analysisDraft.visual ||= {};
      analysisDraft.visual.style_source = "user_override";
    }
    const reply = await post("/api/story-analysis", {
      episode_id: data.episode.id, action: "save",
      analysis: collectStoryAnalysis(analysisDraft),
      expected_version: analysisVersion, locked,
    });
    analysisDraft = reply.analysis;
    analysisVersion = Number(reply.version || analysisVersion);
    return reply;
  };
  document.getElementById("analysis-rerun")?.addEventListener(
    "click", (event) => rerunAnalysis(event.currentTarget));
  document.getElementById("analysis-retry")?.addEventListener(
    "click", (event) => rerunAnalysis(event.currentTarget));
  document.getElementById("analysis-save")?.addEventListener(
    "click", async (event) => {
      const button = event.currentTarget; button.disabled = true;
      try {
        await saveAnalysis(false);
        showToast(`制作圣经 v${analysisVersion} 已保存`, "ok");
        button.textContent = "✓ 已保存";
      } catch (e) { showToast(e.message, "error"); }
      setTimeout(() => {
        button.disabled = false; button.textContent = "保存制作圣经";
      }, 1200);
    });
  document.getElementById("analysis-copy")?.addEventListener(
    "click", async (event) => {
      try {
        await navigator.clipboard.writeText(
          document.getElementById("analysis-global")?.value || "");
        event.currentTarget.textContent = "✓ 已复制";
        setTimeout(() => {
          event.currentTarget.textContent = "复制全局提示词";
        }, 1200);
      } catch (e) { showToast("复制失败，请手动选择文本", "error"); }
    });
  const saveStyle = async () => {
    const style = document.getElementById("style-input").value.trim();
    if (!style || style === data.project.style) return style;
    await post("/api/project/style", {
      project: data.project.title, style });
    return style;
  };
  document.getElementById("style-save").onclick = async (ev) => {
    try {
      await saveStyle();
      showToast("画风已保存,出图会按这个风格执行", "ok");
      ev.target.textContent = "✓ 已保存";
      setTimeout(() => { ev.target.textContent = "保存画风"; }, 1500);
    } catch (e) { showToast(e.message, "error"); }
  };
  document.getElementById("ref-upload").onclick = () => {
    const input = document.createElement("input");
    input.type = "file";
    input.accept = "image/*";
    input.onchange = () => {
      const file = input.files[0];
      if (!file) return;
      const reader = new FileReader();
      reader.onload = async () => {
        try {
          await post("/api/reference/upload", {
            project: data.project.title,
            name: document.getElementById("ref-name").value.trim()
                  || file.name.replace(/\.[^.]+$/, ""),
            attach_to: document.getElementById("ref-attach").value.trim(),
            filename: file.name,
            data_base64: String(reader.result).split(",")[1] || "",
          });
          showToast("参考图已上传,画图时会自动参考", "ok");
          renderCanvasView(episodeId);
        } catch (e) { showToast(e.message, "error"); }
      };
      reader.readAsDataURL(file);
    };
    input.click();
  };
  app.querySelectorAll(".ref-del").forEach((btn) => {
    btn.onclick = (ev) => armConfirm(ev.target, "删除", async () => {
      try {
        await post("/api/reference/delete", {
          project: data.project.title, name: btn.dataset.name });
        showToast("参考图已删除", "ok");
        renderCanvasView(episodeId);
      } catch (e) { showToast(e.message, "error"); }
    });
  });
  document.getElementById("btn-script-ok").onclick = async (ev) => {
    const btn = ev.target;
    btn.disabled = true; btn.textContent = "已确认,画图中…";
    try {
      await saveStyle();   // 确认时顺手保存画风,忘点保存也不丢
      await saveAnalysis(true);
      await post("/api/confirm", { episode_id: data.episode.id });
      showToast("制作圣经已锁定！人物、场景和分镜将继承同一套世界与画风", "ok");
      pollCanvas(episodeId);
    } catch (e) {
      showToast(e.message, "error");
      btn.disabled = false; btn.textContent = "🔒 锁定制作圣经并开始人物图";
    }
  };
}

/* ---- 生产恢复页:阶段失败/中断后的一键续跑入口 ---- */
function renderRecoveryView(data, episodeId) {
  const ep = data.episode;
  const hasScript = !!data.script;
  const failedTask = [...(data.tasks || [])].reverse()
    .find((t) => t.status === "failed");
  app.innerHTML = `
  <div class="canvas-view">
    <div class="confirm-banner">
      <div>
        <b>${failedTask ? `上次生产在「${esc(STAGE_CN[failedTask.stage] || failedTask.stage)}」失败 ⚠️` : "生产已中断"}</b>
        <span>${failedTask ? esc((failedTask.error || "").slice(0, 200)) : "可从断点继续"};
        ${hasScript ? "剧本、人物设定和已画好的图全部保留,点继续只重跑未完成的部分,不重复消耗额度。"
          : "剧本还没写完;点继续会从剧本这一步接着做,写好后照常停下等你确认。"}</span>
      </div>
      <button class="primary" id="btn-resume">▶ 继续补齐(从断点重跑)</button>
    </div>
    <div class="canvas-toolbar">
      <button id="btn-back">← 仪表盘</button>
      <span class="title">《${esc(data.project.title)}》第${ep.number}集</span>
      ${chip(ep.status)}
      <span class="spacer"></span>
      ${hasScript ? `<button id="btn-script2">📖 看剧本</button>` : ""}
      <button id="btn-plan2">🖼 图片清单</button>
      <button id="btn-rebuild-all-recovery" class="danger"
        title="推翻原有设定,清理本轮复用并从头重新生成图片、首尾帧和视频">⚠ 全部重新生成</button>
    </div>
    <div class="produce-main" style="padding:0 18px 40px">
      ${productionLedgerHtml(data, { context: "recovery" })}
      ${imageAccelerationLivebarHtml(data)}
      ${renderPlanBoardHtml(data)}
    </div>
  </div>`;
  document.getElementById("btn-back").onclick = () => { location.hash = "#/"; };
  document.getElementById("btn-script2")?.addEventListener("click", () =>
    showScriptOverlay(data, episodeId));
  document.getElementById("btn-plan2").onclick = () =>
    showPlanOverlay(episodeId);
  document.getElementById("btn-rebuild-all-recovery").onclick = (ev) =>
    armConfirm(ev.target, "全部重新生成", async () => {
      ev.target.disabled = true;
      ev.target.textContent = "已确认,全部重新生成中…";
      try {
        await api("/api/produce", {
          method: "POST", headers: { "Content-Type": "application/json" },
          body: JSON.stringify({ title: data.project.title,
            episode: ep.number, force: true, review: true }),
        });
        showToast("已提交全部重新生成:图片、首尾帧、视频将按新设定从头生产", "ok");
        pollCanvas(episodeId);
      } catch (e) {
        showToast(e.message, "error");
        ev.target.disabled = false;
        ev.target.textContent = "⚠ 全部重新生成";
      }
    });
  bindImageAccelerationLivebar(episodeId);
  bindProductionLedger(app, data, episodeId);
  bindLightbox(app);
  document.getElementById("btn-resume").onclick = async (ev) => {
    const btn = ev.target;
    btn.disabled = true; btn.textContent = "已提交,续跑中…";
    try {
      await api("/api/produce", {
        method: "POST", headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ title: data.project.title,
                               episode: ep.number, review: true }),
      });
      showToast("已从断点继续:复用已完成部分,只重跑剩余步骤", "ok");
      pollCanvas(episodeId);
    } catch (e) {
      showToast(e.message, "error");
      btn.disabled = false; btn.textContent = "▶ 继续补齐(从断点重跑)";
    }
  };
}

function storyboardShotIssues(data) {
  const byShot = {};
  (data.qc_report?.issues || []).forEach((issue) => {
    if (issue.shot_no == null) return;
    (byShot[issue.shot_no] = byShot[issue.shot_no] || []).push(issue);
  });
  (data.image_failures || []).forEach((failure) => {
    if (failure.shot_no == null) return;
    const messages = failure.issues || [];
    (byShot[failure.shot_no] = byShot[failure.shot_no] || []).push({
      severity: "error",
      check: "关键帧二次质检",
      message: messages.join("；") || "自动定向修图后仍未通过，待人工修改",
      shot_no: failure.shot_no,
      plan_id: failure.item_id,
      revision_feedback: failure.revision_feedback || "",
      awaiting_human: true,
    });
  });
  return byShot;
}

function storyboardKeyframePlanItem(data, shotNo) {
  return (((data.render_plan || {}).items) || []).find((row) =>
    row.category === "shot_image" && Number(row.shot_no) === Number(shotNo));
}

function storyboardKeyframeFailure(data, shotNo) {
  return (data.image_failures || []).find((row) =>
    Number(row.shot_no) === Number(shotNo));
}

function storyboardKeyframeUrl(data, shotNo) {
  const failure = storyboardKeyframeFailure(data, shotNo);
  const item = storyboardKeyframePlanItem(data, shotNo);
  if (failure?.failed_output_url) return failure.failed_output_url;
  if (["awaiting_human", "failed"].includes(item?.status) && item?.output_url)
    return item.output_url;
  return (data.artifacts?.images || {})[shotNo] || "";
}

function storyboardLineNo(data, shot) {
  if (!shot.dialogue) return null;
  let lineNo = 0;
  for (const scene of (data.script?.scenes || [])) {
    for (const line of (scene.lines || [])) {
      lineNo += 1;
      if (scene.scene_no === shot.scene_no
          && line.character === shot.dialogue.character
          && line.dialogue === (shot.dialogue_source || shot.dialogue.dialogue))
        return lineNo;
    }
  }
  return null;
}

function storyboardPlanState(data, category, shotNo, complete) {
  const item = (((data.render_plan || {}).items) || []).find((row) =>
    row.category === category && Number(row.shot_no) === Number(shotNo));
  if (item?.status === "awaiting_human") {
    return {
      status: "awaiting_human",
      label: PLAN_STATUS_CN.awaiting_human,
    };
  }
  if (complete) return { status: "done", label: "已生成" };
  const status = item?.status || "pending";
  return { status, label: PLAN_STATUS_CN[status] || "待生成" };
}

function storyboardStateClass(status) {
  return ["done", "reused", "generating", "retrying", "awaiting_human", "pending", "failed"].includes(status)
    ? status : "pending";
}

function storyboardMediaThumb(url, label, shotNo, state) {
  const status = storyboardStateClass(state.status);
  if (!url) return `<div class="storyboard-media-empty state-${status}">
    <span>${esc(label)}</span><b>${esc(state.label)}</b></div>`;
  return `<button type="button" class="storyboard-media-button"
    data-image-url="${esc(url)}" data-image-title="镜头 ${shotNo} · ${esc(label)}"
    aria-label="放大镜头 ${shotNo} 的${esc(label)}">
    <img src="${esc(thumbUrl(url, 260))}" loading="lazy"
      alt="镜头 ${shotNo} ${esc(label)}">
    <span>${esc(label)}</span></button>`;
}

function storyboardCameraHtml(shot) {
  const design = shot.five_dimensions?.camera_design || {};
  const movement = design.movement || shot.shot_contract?.["运镜"] || "";
  const specs = [design.shot_scale, design.angle, design.lens,
    design.camera_position].filter(Boolean);
  if (!movement && !specs.length)
    return `<p>${esc(typeof shot.camera === "string" ? shot.camera : "未填写")}</p>`;
  return `<div class="storyboard-camera">
    <b>${esc(movement || "固定")}</b>
    ${specs.length ? `<span>${esc(specs.join(" · "))}</span>` : ""}
    ${design.movement_motivation
      ? `<small>动机：${esc(design.movement_motivation)}</small>` : ""}
    ${shot.camera && !specs.length ? `<small>${esc(String(shot.camera))}</small>` : ""}
  </div>`;
}

function storyboardCharacterHtml(shot) {
  const map = shot.character_number_map || {};
  const ids = shot.character_number_ids || Object.keys(map);
  const rows = ids.map((id) => map[id]).filter(Boolean);
  const names = shot.characters || [];
  const expected = Number(shot.character_count ?? names.length);
  const mismatch = expected !== names.length;
  const labels = rows.length
    ? rows.map((row) => row.display_label || `${row.actor_id || ""} ${row.name || ""}`)
    : names;
  const overlays = (shot.narrative_overlays || []).filter(
    (item) => item && item.kind === "inner_persona");
  return `<div class="storyboard-cast ${mismatch ? "mismatch" : ""}">
    ${labels.map((label) => `<span>${esc(label)}</span>`).join("")
      || `<span>无出场人物</span>`}
    <small>${expected} 人${mismatch ? ` · 名单实际 ${names.length} 人` : " · 人数已锁"}</small>
  </div>${overlays.length ? `<div class="storyboard-inner-persona">
    ${overlays.map((item) => `<span>🧠 ${esc(item.display_name
      || item.name || item.asset_name || "Q版内心")}</span>`).join("")}
    <small>非现实内心投影 · 夸张表演 · 不计入现场真人</small>
  </div>` : ""}`;
}

function storyboardSoundHtml(data, shot) {
  const art = data.artifacts || {};
  const sound = shot.sound_design || {};
  const lineNo = storyboardLineNo(data, shot);
  const hasVideo = !!(art.videos || {})[shot.shot_no];
  const videoAudio = art.video_audio || {};
  const hasAudioEvidence = Object.prototype.hasOwnProperty.call(
    videoAudio, shot.shot_no);
  const integratedVoice = hasAudioEvidence
    ? !!videoAudio[shot.shot_no]
    : data.production_profile?.voice === "jimeng_builtin";
  const voiceReady = !shot.dialogue || (integratedVoice
    ? hasVideo : (lineNo != null && !!(art.voices || {})[lineNo]));
  return `<div class="storyboard-sound">
    ${shot.dialogue ? `<div class="storyboard-dialogue"><b>${esc(shot.dialogue.character)}</b>
      <span>「${esc(shot.dialogue.dialogue)}」</span></div>`
      : `<span class="storyboard-no-dialogue">无对白</span>`}
    <dl>
      <div><dt>环境</dt><dd>${esc(sound.environment
        || shot.shot_contract?.["音效"] || "未填写")}</dd></div>
      ${sound.effects ? `<div><dt>拟音</dt><dd>${esc(sound.effects)}</dd></div>` : ""}
      ${sound.music ? `<div><dt>音乐</dt><dd>${esc(sound.music)}</dd></div>` : ""}
    </dl>
    ${shot.dialogue ? `<span class="storyboard-status state-${voiceReady ? "done" : "pending"}">
      ${voiceReady ? "✓ 配音/口型已就绪" : "○ 待随视频生成配音/口型"}</span>` : ""}
  </div>`;
}

function storyboardStatusHtml(data, shot, issues, context) {
  const art = data.artifacts || {};
  const no = shot.shot_no;
  const hasImage = !!(art.images || {})[no];
  const hasFirst = !!(art.first || {})[no];
  const hasLast = !!(art.last || {})[no];
  const hasVideo = !!(art.videos || {})[no];
  const imageState = storyboardPlanState(data, "shot_image", no, hasImage);
  const imageFailure = storyboardKeyframeFailure(data, no);
  const frameState = storyboardPlanState(data, "frames", no, hasFirst && hasLast);
  const readable = shot.readable_text || {};
  const videoQc = ((data.video_qc_report || {}).shots || []).find((item) =>
    Number(item.shot_no) === Number(no));
  const videoStatus = videoQc?.awaiting_human ? "awaiting_human"
    : videoQc && !videoQc.passed ? "retrying"
    : videoQc?.passed ? "done" : (hasVideo ? "done" : "pending");
  const videoLabel = videoQc?.awaiting_human
    ? "二次不合格·待人工修改"
    : videoQc && !videoQc.passed
      ? `质检失败·自动返工 ${videoQc.auto_retries_used || 0}/1`
      : videoQc?.passed ? "质检通过" : (hasVideo ? "已生成·待质检" : "待生成");
  return `<div class="storyboard-status-stack">
    <span class="storyboard-status state-${storyboardStateClass(imageState.status)}">
      参考分镜 · ${esc(imageState.label)}</span>
    ${imageFailure?.issues?.length ? `<span class="storyboard-status state-awaiting_human">
      原因 · ${esc(imageFailure.issues.join("；"))}</span>` : ""}
    <span class="storyboard-status state-${storyboardStateClass(frameState.status)}">
      首尾帧 · ${hasFirst && hasLast ? "已齐" : esc(frameState.label)}</span>
    <span class="storyboard-status state-${storyboardStateClass(videoStatus)}">
      视频 · ${videoLabel}</span>
    ${videoQc?.issues?.length ? `<span class="storyboard-status state-${videoQc.awaiting_human ? "awaiting_human" : "retrying"}">
      原因 · ${esc(videoQc.issues.join("；"))}</span>` : ""}
    ${videoQc?.revision_feedback ? `<span class="storyboard-status state-${videoQc.awaiting_human ? "awaiting_human" : "retrying"}">
      自动优化修订 · ${esc(videoQc.revision_feedback)}</span>` : ""}
    ${readable.required ? `<span class="storyboard-status state-${readable.keyframe_uri ? "done" : "pending"}">
      文字 · ${readable.keyframe_uri ? "已锁定" : "待锁定"}</span>` : ""}
    ${issues.length ? `<span class="storyboard-status state-failed">⚠ 质检问题 ${issues.length}</span>`
      : `<span class="storyboard-status state-done">质检 · 暂无问题</span>`}
    ${context === "review" ? `<button type="button" class="shot-table-detail"
      data-shot-detail="${no}">查看完整合同</button>` : ""}
    <button type="button" class="shot-table-play" data-shot-play="${no}">
      ▶ ${hasVideo ? "播放本镜" : "预览本镜"}</button>
  </div>`;
}

function shotProductionTableHtml(data, options = {}) {
  const shots = data.storyboard?.shots || [];
  if (!shots.length) return "";
  const art = data.artifacts || {};
  const issuesByShot = options.shotIssues || storyboardShotIssues(data);
  const context = options.context || "review";
  const aspectClass = data.project.aspect === "16:9" ? "landscape" : "portrait";
  const sceneOf = (no) => (data.script?.scenes || []).find(
    (scene) => scene.scene_no === no) || {};
  const sceneNos = [...new Set(shots.map((shot) => shot.scene_no))];
  const completeImages = shots.filter((shot) => !!(art.images || {})[shot.shot_no]).length;
  const completeFrames = shots.filter((shot) => (art.first || {})[shot.shot_no]
    && (art.last || {})[shot.shot_no]).length;
  const completeVideos = shots.filter((shot) => !!(art.videos || {})[shot.shot_no]).length;
  const bodies = sceneNos.map((sceneNo) => {
    const scene = sceneOf(sceneNo);
    const sceneShots = shots.filter((shot) => shot.scene_no === sceneNo);
    const duration = sceneShots.reduce((sum, shot) => sum + Number(shot.duration || 0), 0);
    return `<tbody data-storyboard-scene="${esc(sceneNo)}">
      <tr class="storyboard-scene-row" data-scene-heading="${esc(sceneNo)}">
        <th colspan="8" scope="rowgroup">场 ${esc(sceneNo)} · ${esc(scene.location || "")}
          <span>${fmt(duration, 1)}s · ${sceneShots.length} 镜</span></th></tr>
      ${sceneShots.map((shot) => {
        const no = shot.shot_no;
        const issues = issuesByShot[no] || [];
        const failedKeyframe = storyboardKeyframeFailure(data, no);
        const keyframeUrl = storyboardKeyframeUrl(data, no);
        const imageState = storyboardPlanState(data, "shot_image", no,
          !!(art.images || {})[no]);
        const frameState = storyboardPlanState(data, "frames", no,
          !!(art.first || {})[no] && !!(art.last || {})[no]);
        const description = shot.description || shot.shot_contract?.["画面内容描述"]
          || shot.five_dimensions?.subject_motion || shot.prompt || "未填写";
        return `<tr class="storyboard-table-row${failedKeyframe ? " qc-needs-human" : ""}"
          data-shot="${no}" data-scene="${esc(sceneNo)}"
          data-missing-keyframe="${(art.images || {})[no] ? "0" : "1"}"
          data-missing-frames="${(art.first || {})[no] && (art.last || {})[no] ? "0" : "1"}"
          data-missing-video="${(art.videos || {})[no] ? "0" : "1"}"
          data-has-issues="${issues.length ? "1" : "0"}">
          <th scope="row" class="storyboard-sequence" data-label="序号">
            <b>#${String(no).padStart(2, "0")}</b>
            <span>${esc(shot.unit_id || `S${no}`)}</span>
            <small>场 ${esc(sceneNo)} · ${esc(shot.shot_function || shot.kind || "镜头")}</small>
          </th>
          <td class="storyboard-duration" data-label="时长">
            <b>${fmt(shot.duration, 1)}s</b><span>${esc(shot.timecode || "时间码未填")}</span></td>
          <td class="storyboard-reference" data-label="参考分镜">
            ${storyboardMediaThumb(keyframeUrl,
              failedKeyframe ? "二次质检失败稿" : "参考分镜", no, imageState)}
            ${shotInlineRevisionHtml(
              no, !!keyframeUrl, context === "live")}</td>
          <td class="storyboard-frames" data-label="首尾帧"><div class="storyboard-frame-pair">
            <div class="storyboard-frame-item">
              ${storyboardMediaThumb((art.first || {})[no], "首帧", no, frameState)}
              ${frameInlineRevisionHtml(
                no, "first_frame", !!(art.first || {})[no], context === "live")}
            </div>
            <div class="storyboard-frame-item">
              ${storyboardMediaThumb((art.last || {})[no], "尾帧", no, frameState)}
              ${frameInlineRevisionHtml(
                no, "last_frame", !!(art.last || {})[no], context === "live")}
            </div>
          </div></td>
          <td class="storyboard-movement" data-label="运镜">${storyboardCameraHtml(shot)}</td>
          <td class="storyboard-description" data-label="画面描述">
            <p>${esc(description)}</p>${storyboardCharacterHtml(shot)}
            <details><summary>连续性与剧本依据</summary>
              <div><b>起：</b>${esc(stateInline(shot.start_state))}</div>
              <div><b>止：</b>${esc(stateInline(shot.end_state))}</div>
              <div><b>剧本：</b>${esc(shot.script_reference || "未填写")}</div>
              <div><b>视觉钩子：</b>${esc(shot.visual_hook || "未填写")}</div>
            </details></td>
          <td class="storyboard-sound-cell" data-label="声音">${storyboardSoundHtml(data, shot)}</td>
          <td class="storyboard-production-status" data-label="生产状态">
            ${storyboardStatusHtml(data, shot, issues, context)}</td>
        </tr>`;
      }).join("")}
    </tbody>`;
  }).join("");
  return `<section class="shot-production-section ${aspectClass}" data-shot-table-context="${esc(context)}">
    <div class="shot-production-heading">
      <div><h2>📋 分镜头生产表</h2>
        <p>逐镜核对；发现错误可在参考分镜下直接修改并同步后续，不用退回图片清单。</p></div>
      <div class="shot-production-progress">
        <span>参考分镜 ${completeImages}/${shots.length}</span>
        <span>首尾帧 ${completeFrames}/${shots.length}</span>
        <span>视频 ${completeVideos}/${shots.length}</span>
      </div>
      <label class="shot-table-filter-label">筛选
        <select class="shot-table-filter">
          <option value="all">全部镜头</option>
          <option value="missing-keyframe">缺参考分镜</option>
          <option value="missing-frames">缺首尾帧</option>
          <option value="missing-video">缺视频</option>
          <option value="issues">有质检问题</option>
        </select></label>
    </div>
    <div class="shot-table-filter-summary" aria-live="polite">显示全部 ${shots.length} 个镜头</div>
    <div class="shot-production-table-wrap" role="region"
      aria-label="分镜头生产表，可横向滚动" tabindex="0">
      <table class="shot-production-table">
        <caption>本集逐镜生产合同与产物状态</caption>
        <colgroup><col class="col-sequence"><col class="col-duration">
          <col class="col-reference"><col class="col-frames"><col class="col-movement">
          <col class="col-description"><col class="col-sound"><col class="col-status"></colgroup>
        <thead><tr><th scope="col">序号</th><th scope="col">时长</th>
          <th scope="col">参考分镜</th><th scope="col">首尾帧</th><th scope="col">运镜</th>
          <th scope="col">画面描述</th><th scope="col">声音</th><th scope="col">生产状态</th></tr></thead>
        ${bodies}
      </table>
    </div>
  </section>`;
}

function bindShotProductionTable(root, data, onSelect) {
  bindShotInlineRevisions(root, data);
  root.querySelectorAll(".storyboard-media-button").forEach((button) => {
    button.onclick = (event) => {
      event.stopPropagation();
      showImageLightbox(button.dataset.imageUrl, button.dataset.imageTitle);
    };
  });
  root.querySelectorAll(".shot-table-detail").forEach((button) => {
    button.onclick = (event) => {
      event.stopPropagation();
      if (onSelect) onSelect(Number(button.dataset.shotDetail));
    };
  });
  root.querySelectorAll(".shot-table-play").forEach((button) => {
    button.onclick = (event) => {
      event.stopPropagation();
      openPlayer(data, Number(button.dataset.shotPlay));
    };
  });
  if (onSelect) root.querySelectorAll(".storyboard-table-row").forEach((row) => {
    row.tabIndex = 0;
    row.onclick = (event) => {
      if (event.target.closest(
        "button, a, details, summary, input, textarea, select, label")) return;
      onSelect(Number(row.dataset.shot));
    };
    row.onkeydown = (event) => {
      if (["Enter", " "].includes(event.key)) {
        event.preventDefault(); onSelect(Number(row.dataset.shot));
      }
    };
  });
  root.querySelectorAll(".shot-production-section").forEach((section) => {
    const filter = section.querySelector(".shot-table-filter");
    const summary = section.querySelector(".shot-table-filter-summary");
    const rows = [...section.querySelectorAll(".storyboard-table-row")];
    if (!filter) return;
    filter.onchange = () => {
      const key = filter.value;
      let visible = 0;
      rows.forEach((row) => {
        const show = key === "all"
          || (key === "missing-keyframe" && row.dataset.missingKeyframe === "1")
          || (key === "missing-frames" && row.dataset.missingFrames === "1")
          || (key === "missing-video" && row.dataset.missingVideo === "1")
          || (key === "issues" && row.dataset.hasIssues === "1");
        row.hidden = !show;
        if (show) visible += 1;
      });
      section.querySelectorAll("[data-storyboard-scene]").forEach((body) => {
        const heading = body.querySelector(".storyboard-scene-row");
        if (heading) heading.hidden = ![...body.querySelectorAll(".storyboard-table-row")]
          .some((row) => !row.hidden);
      });
      summary.textContent = `显示 ${visible}/${rows.length} 个镜头`;
    };
  });
}

/* ---- 分镜生产表:Hero 横幅 + 人物条 + 逐镜生产合同 ---- */
function renderTheater(data, canvas) {
  const el = document.getElementById("theater");
  const art = data.artifacts;
  const script = data.script;
  const shots = data.storyboard.shots;
  const hero = art.cover || art.images[shots[0] && shots[0].shot_no] || "";
  const total = shots.reduce((a, s) => a + s.duration, 0);
  const kindCN = { drama: "剧情短剧", idol: "AI虚拟偶像" }[data.project.kind]
    || data.project.kind;
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
    ${productionLedgerHtml(data, { context: "review" })}
    ${shotProductionTableHtml(data, {
      shotIssues: canvas.shotIssues, context: "review" })}`;

  el.querySelector("#hero-play").onclick = () => openPlayer(data);
  el.querySelector("#hero-script").onclick = () =>
    document.getElementById("btn-script").click();
  el.querySelector("#hero-export").onclick = () => exportEpisode(data);
  el.querySelector("#hero-rename").onclick = () =>
    renameProject(data.project.title,
      () => renderCanvasView(data.episode.id));
  bindProductionLedger(el, data, data.episode.id);
  bindShotProductionTable(el, data, (shotNo) => {
    canvas.select(shotNo);
    if (window.matchMedia("(max-width: 780px)").matches) {
      const panel = document.getElementById("sidepanel");
      panel.hidden = false;
      const close = document.createElement("button");
      close.type = "button";
      close.className = "sidepanel-mobile-close";
      close.textContent = "× 关闭镜头详情";
      close.onclick = () => { panel.hidden = true; };
      panel.prepend(close);
    }
  });
}

const CANVAS_STAGE_STATUS_CN = {
  done: "已完成", reused: "已复用", selected: "已定版",
  generating: "生产中", running: "生产中", failed: "失败",
  stopped: "已停止", pending: "待生产",
};

function canvasLatestTask(data, stage) {
  return (data.tasks || []).filter((task) => task.stage === stage)
    .sort((a, b) => Number(b.created_at || 0) - Number(a.created_at || 0))[0] || null;
}

function canvasTaskRollup(data, stage) {
  const tasks = (data.tasks || []).filter((task) => task.stage === stage);
  const providers = [...new Set(tasks.flatMap((task) =>
    String(task.provider || "").split(",").map((name) => name.trim()).filter(Boolean)))];
  return {
    latest: canvasLatestTask(data, stage),
    cost: tasks.reduce((sum, task) => sum + Number(task.cost || 0), 0),
    providers,
  };
}

function canvasStageItemStatus(status) {
  if (["done", "reused", "selected", "completed"].includes(status)) return "done";
  if (["generating", "running"].includes(status)) return "generating";
  if (status === "failed") return "failed";
  if (status === "stopped") return "stopped";
  return "pending";
}

function canvasStageStatus(items, latestTask) {
  const taskStatus = canvasStageItemStatus(latestTask?.status);
  if (latestTask && ["generating", "failed", "stopped"].includes(taskStatus))
    return taskStatus;
  if (latestTask?.status === "done") return "done";
  const states = items.map((item) => canvasStageItemStatus(item.status));
  if (states.includes("generating")) return "generating";
  if (states.includes("failed")) return "failed";
  if (states.includes("stopped")) return "stopped";
  if (states.length && states.every((status) => status === "done")) return "done";
  return "pending";
}

function canvasLedgerItem(row) {
  const state = productionLedgerState(row);
  return {
    label: row.objectLabel,
    meta: [row.subLabel, row.issue].filter(Boolean).join(" · "),
    status: state,
    thumb: row.outputUrls?.[0] || "",
    planId: row.planId || "",
    shotNo: row.shotNo || null,
    refs: row.refs?.items?.length || 0,
  };
}

function productionCanvasStages(data) {
  const script = data.script || {};
  const continuity = data.continuity || {};
  const storyboard = data.storyboard || {};
  const blocking = data.blocking || {};
  const artifacts = data.artifacts || {};
  const ledgerRows = productionLedgerRows(data);
  const shots = storyboard.shots || [];
  const planGroups = {
    cast: ledgerRows.filter((row) => [
      "character_candidate", "character_art", "character_sheet", "scene_art",
    ].includes(row.category)),
    images: ledgerRows.filter((row) => row.category === "shot_image"),
    frames: ledgerRows.filter((row) => row.category === "frames"),
    videos: ledgerRows.filter((row) => row.category === "video"),
  };
  const qc = data.qc_report || {};
  const contentReview = data.content_review || qc.content_review || {};
  const delivery = qc.delivery_check || {};
  const latestVideoTask = canvasLatestTask(data, "videos");
  const stageItems = {
    script: (script.scenes || []).map((scene) => ({
      label: `第 ${scene.scene_no} 场 · ${scene.location || "未命名场景"}`,
      meta: `${(scene.lines || []).length} 句台词${scene.action ? " · 含场景动作" : ""}`,
      status: "done", action: "script",
    })),
    continuity: [
      ...(continuity.characters || []).map((character) => ({
        label: `人物 · ${character.name}`,
        meta: character.role || character.identity || "连续性身份锚点",
        status: "done", action: "overview",
      })),
      ...(continuity.scenes || []).map((scene) => ({
        label: `场景 · ${scene.location || scene.name || `第${scene.scene_no}场`}`,
        meta: "场景、道具与状态锚点", status: "done", action: "overview",
      })),
    ],
    cast: planGroups.cast.map(canvasLedgerItem),
    storyboard: shots.map((shot) => ({
      label: `${shot.unit_id || `镜头 ${String(shot.shot_no).padStart(2, "0")}`} · ${shot.shot_function || "分镜"}`,
      meta: `第 ${shot.scene_no} 场 · ${fmt(shot.duration, 1)}s · ${(shot.characters || []).length} 人`,
      status: "done", shotNo: shot.shot_no,
      thumb: artifacts.images?.[shot.shot_no] || "",
    })),
    blocking: (blocking.scenes || []).map((scene) => ({
      label: `第 ${scene.scene_no} 场 · ${scene.location || "未命名场景"}`,
      meta: `${(scene.shots || []).length} 镜 · ${scene.required ? "重点调度" : "连续性参考"}`,
      status: blocking.validation?.passed ? "done" : "pending",
      thumb: scene.svg_url || scene.map_url || scene.svg || "",
      action: "blocking",
    })),
    images: planGroups.images.map(canvasLedgerItem),
    text_assets: ((data.text_assets || {}).assets || []).map((asset) => ({
      label: `${asset.unit_id || `镜头 ${asset.shot_no}`} · ${asset.carrier || "画面文字"}`,
      meta: `白名单：${(asset.whitelist || []).join("、") || "无额外文字"}`,
      status: (data.text_assets || {}).passed ? "done" : "pending",
      shotNo: asset.shot_no,
      thumb: artifacts.images?.[asset.shot_no] || "",
    })),
    frames: planGroups.frames.map(canvasLedgerItem),
    preflight: ((data.preflight || {}).gates || []).map((gate) => ({
      label: gate.label || gate.id || "生产门禁",
      meta: gate.detail || gate.description || "",
      status: gate.passed ? "done" : "failed",
      action: "overview",
    })),
    videos: planGroups.videos.map((row) => ({
      ...canvasLedgerItem(row),
      meta: `${row.subLabel || ""}${row.refs?.items?.length
        ? ` · 实际输入 ${row.refs.items.length} 张参考图` : ""}`,
      videoReference: true,
    })),
    voices: shots.map((shot) => {
      const hasVideo = !!artifacts.videos?.[shot.shot_no];
      const hasIntegratedAudio = !!artifacts.video_audio?.[shot.shot_no];
      const dialogue = shot.dialogue?.dialogue || "";
      return {
        label: `${shot.unit_id || `镜头 ${shot.shot_no}`} · ${dialogue ? shot.dialogue.character : "无对白"}`,
        meta: dialogue || "保留环境声与动作拟音",
        status: hasVideo && (!dialogue || hasIntegratedAudio
          || data.production_profile?.voice === "jimeng_builtin") ? "done" : "pending",
        shotNo: shot.shot_no,
      };
    }),
    edit: artifacts.final ? [{
      label: "无字幕母版", meta: "最终剪辑成片", status: "done",
      thumb: artifacts.cover || "", action: "play",
    }] : [{
      label: "最终剪辑成片", meta: "等待所有镜头视频与声音完成",
      status: latestVideoTask?.status === "failed" ? "failed" : "pending",
    }],
    qc: [
      { label: "① 自动文件检查", meta: "分辨率、时长、音视频流与文件完整性",
        status: qc.technical_passed ? "done" : "pending", action: "overview" },
      { label: "② 抽帧图文检查板", meta: "人物、文字、服装与段间连续性",
        status: artifacts.review_board ? "done" : "pending", action: "overview" },
      { label: "③ 逐段内容复核", meta: "逐镜对照剧本核心事件",
        status: contentReview.passed ? "done" : "pending", action: "overview" },
      { label: "④ 交付脚本实跑", meta: "交付包自动检查",
        status: delivery.passed ? "done" : "pending", action: "overview" },
    ],
    package: [
      { label: "封面", meta: artifacts.cover ? "封面已生成" : "等待成片后生成",
        status: artifacts.cover ? "done" : "pending", thumb: artifacts.cover || "" },
      ...((artifacts.titles || []).map((title, index) => ({
        label: `标题 ${index + 1}`, meta: title, status: "done",
      }))),
      ...((artifacts.clips || []).map((clip) => ({
        label: `拆条 · 第 ${clip.scene_no || "-"} 场`,
        meta: "短视频拆条", status: clip.url ? "done" : "pending",
      }))),
    ],
    archive: [
      { label: "制作标准快照",
        meta: `${data.production_standard?.name || "SK 五维漫剧标准"} · v${data.production_standard?.version || 1}`,
        status: data.production_standard ? "done" : "pending" },
      { label: "连续性关系与资产来源",
        meta: data.relations ? "已记录人物、场景与镜头关系" : "等待归档",
        status: data.relations ? "done" : "pending" },
      { label: "最终交付包",
        meta: artifacts.final ? "成片与元数据已沉淀" : "等待成片完成",
        status: artifacts.final ? "done" : "pending" },
    ],
  };
  return STAGE_ORDER.map((key, index) => {
    const rollup = canvasTaskRollup(data, key);
    const items = stageItems[key]?.length ? stageItems[key] : [{
      label: STAGE_CN[key] || key,
      meta: "尚未进入此生产环节",
      status: "pending",
    }];
    return {
      key, index, label: STAGE_CN[key] || key, items,
      latestTask: rollup.latest, cost: rollup.cost, providers: rollup.providers,
      status: canvasStageStatus(items, rollup.latest),
      x: CANVAS_STAGE_LEFT + index * (CANVAS_STAGE_W + CANVAS_STAGE_GAP),
      y: CANVAS_STAGE_TOP,
    };
  });
}

function productionCanvasStageItemHtml(item) {
  const status = canvasStageItemStatus(item.status);
  const attrs = [
    item.planId ? `data-canvas-plan="${esc(item.planId)}"` : "",
    item.shotNo != null ? `data-canvas-shot="${item.shotNo}"` : "",
    item.action ? `data-canvas-action="${esc(item.action)}"` : "",
    item.videoReference ? `data-canvas-video-reference="${item.shotNo}"` : "",
  ].filter(Boolean).join(" ");
  const actionable = !!attrs;
  const tag = actionable ? "button" : "div";
  return `<${tag} ${actionable ? 'type="button"' : ""} class="canvas-stage-item" ${attrs}>
    ${item.thumb ? `<img src="${esc(thumbUrl(item.thumb, 96))}" loading="lazy" alt="">`
      : `<span class="canvas-stage-item-icon">${status === "done" ? "✓"
        : status === "generating" ? "⏳" : status === "failed" ? "!" : "○"}</span>`}
    <span class="canvas-stage-item-copy"><b>${esc(item.label)}</b>
      <small>${esc(item.meta || "")}</small></span>
    <span class="canvas-stage-item-status status-${status}">${
      esc(CANVAS_STAGE_STATUS_CN[status] || status)}</span>
  </${tag}>`;
}

function productionCanvasStageHtml(stage) {
  const provider = stage.providers.length
    ? stage.providers.join(" / ") : "等待产线";
  return `<section class="canvas-stage-board state-${stage.status}"
    data-canvas-node data-canvas-stage="${esc(stage.key)}"
    style="left:${stage.x}px;top:${stage.y}px">
    <header class="canvas-stage-board-head">
      <span class="canvas-stage-index">${String(stage.index + 1).padStart(2, "0")}</span>
      <div><h3>${esc(stage.label)}</h3>
        <p>${stage.items.length} 项 · ${esc(provider)}${
          stage.cost ? ` · 成本 ${fmt(stage.cost)}` : ""}</p></div>
      <span class="canvas-stage-state">${esc(
        CANVAS_STAGE_STATUS_CN[stage.status] || stage.status)}</span>
    </header>
    <div class="canvas-stage-list">${stage.items.map(
      productionCanvasStageItemHtml).join("")}</div>
  </section>`;
}

class StoryboardCanvas {
  constructor(data, shotIssues, lineIssues) {
    this.data = data;
    this.shotIssues = shotIssues;
    this.lineIssues = lineIssues;
    this.scale = 1; this.tx = 30; this.ty = 24;
    this.selected = null;
    this.layoutKey = `aifos.layout.pipeline.${data.episode.id}`;
    this.stages = productionCanvasStages(data);
    const tallestStage = Math.max(...this.stages.map((stage) =>
      70 + stage.items.length * 44), 0);
    this.shotZoneTop = Math.max(CANVAS_SHOTS_TOP,
      CANVAS_STAGE_TOP + tallestStage + 170);
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
        pos[s.shot_no] = {
          x: LANE_X + i * (CARD_W + GAP_X),
          y: this.shotZoneTop + lane * (CARD_H + GAP_Y),
        };
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
    this.renderStageNav();
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
    let html = `<div class="canvas-flow-line" data-canvas-node
      style="left:${CANVAS_STAGE_LEFT + CANVAS_STAGE_W}px;top:${CANVAS_STAGE_TOP + 42}px;
      width:${(this.stages.length - 1) * (CANVAS_STAGE_W + CANVAS_STAGE_GAP)
        - CANVAS_STAGE_W}px"></div>`;
    html += this.stages.map(productionCanvasStageHtml).join("");
    html += `<div class="canvas-shot-zone-label" data-canvas-node
      style="left:0;top:${this.shotZoneTop - 92}px">
      <b>逐镜生产区</b><span>按场次排列 · 可拖动镜头卡重新布局</span></div>`;
    for (const [sceneNo, y] of lanes) {
      const scene = this.sceneOf(sceneNo);
      html += `<div class="lane-label" data-canvas-node style="left:0;top:${y}px">
        场 ${sceneNo}<span class="loc">${esc(scene?.location || "")}</span></div>`;
    }
    for (const shot of storyboard.shots) {
      const p = this.positions[shot.shot_no];
      const failedKeyframe = storyboardKeyframeFailure(
        this.data, shot.shot_no);
      const img = storyboardKeyframeUrl(this.data, shot.shot_no);
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
      <div class="shot-card${this.selected === shot.shot_no ? " selected" : ""}${
        failedKeyframe ? " qc-needs-human" : ""}"
           data-canvas-node data-shot="${shot.shot_no}" style="left:${p.x}px;top:${p.y}px">
        ${img ? `<img src="${esc(img)}" alt="镜头${shot.shot_no}${
          failedKeyframe ? "二次质检失败稿" : "关键图"}" draggable="false">`
              : `<div class="no-img">暂无关键图</div>`}
        <div class="body">
          <div class="head"><span class="sn">#${String(shot.shot_no).padStart(2, "0")}</span>
            <span class="dur">${esc(shot.camera || "")} · ${fmt(shot.duration, 1)}s</span></div>
          <div class="desc">${esc(shot.dialogue ? `${shot.dialogue.character}:「${shot.dialogue.dialogue}」` : shot.description)}</div>
          <div class="badges">
            ${failedKeyframe ? `<span class="badge qc">⚠ 待人工修改</span>` : ""}
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

  renderStageNav() {
    const nav = document.getElementById("canvas-stage-nav");
    if (!nav) return;
    nav.innerHTML = `<span class="canvas-stage-nav-label">定位环节</span>${
      this.stages.map((stage) => `<button type="button"
        class="canvas-stage-jump state-${stage.status}"
        data-stage-jump="${esc(stage.key)}"><b>${String(stage.index + 1).padStart(2, "0")}</b>
        ${esc(stage.label)} <span>${stage.items.length}</span></button>`).join("")}`;
    nav.querySelectorAll("[data-stage-jump]").forEach((button) => {
      button.onclick = () => this.focusStage(button.dataset.stageJump);
    });
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

  focusElement(element, maxScale = 1.05) {
    if (!element) return;
    const rect = this.viewport.getBoundingClientRect();
    const x = Number.parseFloat(element.style.left || "0");
    const y = Number.parseFloat(element.style.top || "0");
    const width = element.offsetWidth || CANVAS_STAGE_W;
    const height = element.offsetHeight || 500;
    const next = Math.min(maxScale, Math.max(0.28, Math.min(
      (rect.width - 72) / width, (rect.height - 72) / height)));
    this.scale = next;
    this.tx = (rect.width - width * next) / 2 - x * next;
    this.ty = (rect.height - height * next) / 2 - y * next;
    this.applyTransform();
  }

  focusStage(stageKey) {
    const board = this.world.querySelector(
      `.canvas-stage-board[data-canvas-stage="${CSS.escape(stageKey)}"]`);
    if (!board) return;
    this.world.querySelectorAll(".canvas-stage-board").forEach((node) =>
      node.classList.toggle("focused", node === board));
    const viewport = this.viewport.getBoundingClientRect();
    const x = Number.parseFloat(board.style.left || "0");
    const y = Number.parseFloat(board.style.top || "0");
    const width = board.offsetWidth || CANVAS_STAGE_W;
    const next = Math.min(1.05, Math.max(0.65,
      (viewport.width - 56) / width));
    this.scale = next;
    this.tx = (viewport.width - width * next) / 2 - x * next;
    this.ty = 28 - y * next;
    this.applyTransform();
  }

  focusShot(shotNo) {
    this.select(shotNo);
    const card = this.world.querySelector(`.shot-card[data-shot="${shotNo}"]`);
    this.focusElement(card, 1.35);
  }

  showMobileSidePanel() {
    if (!window.matchMedia("(max-width: 780px)").matches
        || this.viewport.hidden) return;
    const panel = document.getElementById("sidepanel");
    panel.hidden = false;
    if (panel.querySelector(".sidepanel-mobile-close")) return;
    const close = document.createElement("button");
    close.type = "button";
    close.className = "sidepanel-mobile-close";
    close.textContent = "× 关闭详情";
    close.onclick = () => { panel.hidden = true; };
    panel.prepend(close);
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
    const failedKeyframe = storyboardKeyframeFailure(this.data, shotNo);
    const keyframeUrl = storyboardKeyframeUrl(this.data, shotNo);
    const lineNo = this.lineNoOf(shot);
    const dims = shot.five_dimensions || {};
    const cam = dims.camera_design || {};
    const textAsset = shot.readable_text || {};
    panel.innerHTML = `
      <h3>${esc(shot.unit_id || `镜头 #${String(shotNo).padStart(2, "0")}`)} · 场${shot.scene_no}</h3>
      ${keyframeUrl ? `<img class="preview" src="${esc(keyframeUrl)}" alt="${
        failedKeyframe ? "二次质检失败稿" : "关键图"}">` : ""}
      ${failedKeyframe ? `<div class="issue error">[关键帧二次质检]
        ${esc((failedKeyframe.issues || []).join("；") || "待人工修改")}</div>` : ""}
      ${shotInlineRevisionHtml(shotNo, !!keyframeUrl)}
      <h4>首尾帧</h4>
      <div class="thumbs editable-frame-thumbs">
        <figure>${art.first[shotNo] ? `<img src="${esc(art.first[shotNo])}">` : ""}
          <figcaption>首帧</figcaption>
          ${frameInlineRevisionHtml(
            shotNo, "first_frame", !!art.first[shotNo])}</figure>
        <figure>${art.last[shotNo] ? `<img src="${esc(art.last[shotNo])}">` : ""}
          <figcaption>尾帧</figcaption>
          ${frameInlineRevisionHtml(
            shotNo, "last_frame", !!art.last[shotNo])}</figure>
      </div>
      ${mediaTag(art.videos[shotNo]) ? `<h4>镜头视频</h4>${mediaTag(art.videos[shotNo])}` : ""}
      ${shot.dialogue ? `<h4>台词</h4><div class="dialogue"><b>${esc(shot.dialogue.character)}</b>:${esc(shot.dialogue.dialogue)}</div>` : ""}
      ${shot.dialogue_part?.total > 1 ? `<div class="dialogue-part">原句拆分 ${shot.dialogue_part.index}/${shot.dialogue_part.total} · ${esc(shot.dialogue_source || "")}</div>` : ""}
      ${lineNo != null && mediaTag(art.voices[lineNo]) ? mediaTag(art.voices[lineNo]) : ""}
      <h4>生产合同</h4>
      <ul class="links">
        <li><span>镜头功能</span><span>${esc(shot.shot_function || "-")}</span></li>
        <li><span>人物</span><span>${esc((shot.characters || []).join("、"))} · ${shot.character_count ?? 0}人</span></li>
        ${(shot.narrative_overlays || []).length ? `<li><span>内心Q版</span><span>${
          esc((shot.narrative_overlays || []).map((item) => item.display_name
            || item.name || item.asset_name || "Q版内心").join("、"))
        } · 非现实叠层，不计真人</span></li>` : ""}
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
      <div class="prompt">${esc(shot.seedance_prompt_compact || shot.seedance_prompt || shot.prompt)}</div>
      <h4>产物</h4>
      <ul class="links">
        ${this.link(failedKeyframe ? "二次质检失败稿" : "关键图", keyframeUrl)}
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
          keyframeUrl, `shot_${shotNo}.png`)}</div>
      <div class="io-row"><span>视频</span>
        ${ioControls({ kind: "shot_video", shot_no: shotNo },
          art.videos[shotNo], `shot_${shotNo}.mp4`)}</div>
      ${issues.length ? `<h4>质检问题</h4>${issues.map((i) => `
        <div class="issue ${esc(i.severity)}">[${esc(i.check)}] ${esc(i.message)}</div>`).join("")}` : ""}`;
    const epId = this.data.episode.id;
    bindShotInlineRevisions(panel, this.data);
    bindRegen(panel, epId, () => this.data);
    bindIo(panel, epId, () => renderCanvasView(epId));
  }

  link(label, url) {
    return `<li><span>${label}</span>${url ? `<a href="${esc(url)}" target="_blank">打开</a>` : `<span class="empty">无</span>`}</li>`;
  }

  lineNoOf(shot) {
    return storyboardLineNo(this.data, shot);
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
    const nodes = [...this.world.querySelectorAll("[data-canvas-node]")]
      .map((element) => {
        const x = Number.parseFloat(element.style.left || "0");
        const y = Number.parseFloat(element.style.top || "0");
        return {
          x, y,
          width: element.offsetWidth || 1,
          height: element.offsetHeight || 1,
        };
      });
    if (!nodes.length) return { x: 0, y: 0, w: 800, h: 600 };
    const minX = Math.min(...nodes.map((node) => node.x));
    const minY = Math.min(...nodes.map((node) => node.y));
    const maxX = Math.max(...nodes.map((node) => node.x + node.width));
    const maxY = Math.max(...nodes.map((node) => node.y + node.height));
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
      this.justDragged = drag.moved;
      if (drag.moved) setTimeout(() => { this.justDragged = false; }, 0);
      if (drag.kind === "card") {
        if (drag.moved) this.saveLayout();
        else this.select(drag.no);
      } else if (!drag.moved) {
        this.select(null);
      }
      drag = null;
    });
    this.world.addEventListener("click", (ev) => {
      if (this.justDragged) {
        ev.preventDefault();
        ev.stopPropagation();
        return;
      }
      const plan = ev.target.closest("[data-canvas-plan]");
      if (plan) {
        ev.stopPropagation();
        showPlanOverlay(this.data.episode.id, plan.dataset.canvasPlan);
        return;
      }
      const videoReference = ev.target.closest("[data-canvas-video-reference]");
      if (videoReference) {
        ev.stopPropagation();
        showVideoReferencePicker(this.data, this.data.episode.id,
          Number(videoReference.dataset.canvasVideoReference));
        return;
      }
      const shot = ev.target.closest("[data-canvas-shot]");
      if (shot && !shot.classList.contains("shot-card")) {
        ev.stopPropagation();
        this.focusShot(Number(shot.dataset.canvasShot));
        return;
      }
      const action = ev.target.closest("[data-canvas-action]");
      if (!action) return;
      ev.stopPropagation();
      if (action.dataset.canvasAction === "script")
        showScriptOverlay(this.data, this.data.episode.id);
      else if (action.dataset.canvasAction === "blocking")
        showBlockingOverlay(this.data.episode.id);
      else if (action.dataset.canvasAction === "play")
        openPlayer(this.data);
      else if (action.dataset.canvasAction === "overview") {
        this.select(null);
        this.showMobileSidePanel();
      }
    });
  }

  select(shotNo) {
    this.selected = shotNo;
    this.world.querySelectorAll(".shot-card").forEach((c) =>
      c.classList.toggle("selected", Number(c.dataset.shot) === shotNo));
    document.querySelectorAll(".storyboard-table-row").forEach((row) =>
      row.classList.toggle("selected", Number(row.dataset.shot) === shotNo));
    this.renderSidePanel(shotNo);
    if (shotNo != null) this.showMobileSidePanel();
  }
}

/* 所有路由依赖的常量、渲染函数与画布类均完成初始化后再启动应用。 */
window.addEventListener("beforeinstallprompt", (event) => {
  event.preventDefault();
  deferredInstallPrompt = event;
});
window.addEventListener("appinstalled", () => {
  deferredInstallPrompt = null;
  showToast("AIFOS 已添加到主屏幕", "ok");
});
if ("serviceWorker" in navigator) {
  window.addEventListener("load", () => {
    navigator.serviceWorker.register("/sw.js", { updateViaCache: "none" }).catch(() => {
      // 局域网 HTTP 在部分浏览器不是安全上下文；不影响在线使用。
    });
  });
}
bindMobileAccessButtons();
window.addEventListener("hashchange", route);
route();
