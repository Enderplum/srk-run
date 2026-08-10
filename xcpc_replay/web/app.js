/* ==========================================================================
 * XCPC 榜单回放 — 前端回放状态机与渲染
 *
 * 职责：
 *  1. 加载数据载荷（/api/contest）
 *  2. 逐事件推进回放（ICPC 计分、排名、封榜处理）
 *  3. 渲染榜单表格 / 事件流 / 计时 / 进度条 / 活动图
 *  4. 时间轴控制（播放 / 暂停 / 倍速 / 拖动 / 上一条 / 下一条）
 *
 * 计分规则与后端 xcpc_replay/replay.py 保持一致：
 *  - 结果不在 noPenalty 集合中的"失败"提交累计尝试次数；
 *  - 通过时：过题数 +1，罚时 += 通过分钟数 + 20 × 通过前尝试次数。
 * ========================================================================== */

"use strict";

/* ----------------------------- DOM 引用 ----------------------------- */
const $ = (id) => document.getElementById(id);

const el = {
  title: $("contest-title"),
  sub: $("contest-sub"),
  fileSelect: $("file-select"),
  boardFilter: $("board-filter"),
  freezeToggle: $("freeze-toggle"),
  freezeText: $("freeze-text"),
  validateBadge: $("validate-badge"),
  clockElapsed: $("clock-elapsed"),
  clockWall: $("clock-wall"),
  clockProgress: $("clock-progress"),
  timeline: $("timeline"),
  activityChart: $("activity-chart"),
  statEvent: $("stat-event"),
  statAc: $("stat-ac"),
  statLeader: $("stat-leader"),
  boardHeadRow: $("board-head-row"),
  boardBody: $("board-body"),
  boardEmpty: $("board-empty"),
  boardStat: $("board-stat"),
  feedList: $("feed-list"),
  feedEmpty: $("feed-empty"),
  feedTitle: document.querySelector(".feed-title"),
  feedPanel: $("feed-panel"),
  feedCollapse: $("feed-collapse"),
  btnPlay: $("btn-play"),
  btnStart: $("btn-start"),
  btnEnd: $("btn-end"),
  btnPrev: $("btn-prev"),
  btnNext: $("btn-next"),
  speedGroup: $("speed-group"),
};

/* ----------------------------- 全局状态 ----------------------------- */
let payload = null;        // 数据载荷
let events = [];           // payload.events 的快捷引用
let state = null;          // 回放状态（makeState 生成）
let rowInfos = [];         // 每行 DOM 引用
let boardMode = "all";     // 榜单视图：all=总榜（全部队伍） / official=正榜（仅正式队伍）

let playing = false;
let speed = 60;            // 比赛秒 / 真实秒
let nowMs = 0;             // 当前比赛时刻（毫秒）
let eventIndex = 0;        // 下一个待应用的事件下标
let freezeOn = false;
let freezeMs = 0;          // 封榜窗口时长（毫秒），由数据配置或默认值决定
let fullRender = true;     // 强制刷新全部行
let durationMs = 0;
let startDate = null;
let feedQueue = [];        // 本帧待渲染的动态
let suppressPop = false;   // 跳转时关闭单元格动画
let dragging = false;      // 是否正在拖动进度条
let lastTs = 0;
let chartBins = null;      // 活动图预计算数据
let firstBlood = [];

const SPEEDS = [0.5, 1, 2, 5, 15, 60, 300, 600, "MAX"];
//: 数据未配置封榜时长时使用的默认封榜窗口（最后 1 小时）
const DEFAULT_FREEZE_MS = 60 * 60 * 1000;

/** 判断某个比赛时刻是否处于封榜窗口内。 */
function isFrozenTime(t) {
  return freezeOn && freezeMs > 0 && t > Math.max(durationMs - freezeMs, 0);
}

/* ----------------------------- 工具函数 ----------------------------- */
function clamp(v, lo, hi) { return Math.min(hi, Math.max(lo, v)); }

function fmtHMS(ms) {
  ms = Math.max(0, Math.round(ms));
  const s = Math.floor(ms / 1000);
  const h = Math.floor(s / 3600);
  const m = Math.floor((s % 3600) / 60);
  const ss = s % 60;
  return String(h).padStart(2, "0") + ":" +
         String(m).padStart(2, "0") + ":" +
         String(ss).padStart(2, "0");
}

function fmtMin(m) {
  return String(Math.floor(m / 60)).padStart(2, "0") + ":" +
         String(m % 60).padStart(2, "0");
}

/* ----------------------------- 数据加载 ----------------------------- */
async function loadFiles() {
  const res = await fetch("/api/files");
  const data = await res.json();
  const names = data.files || [];
  el.fileSelect.innerHTML = "";
  if (!names.length) {
    const opt = document.createElement("option");
    opt.textContent = "未发现 .srk.json 文件";
    el.fileSelect.appendChild(opt);
    el.fileSelect.disabled = true;
    return;
  }
  names.forEach((n) => {
    const opt = document.createElement("option");
    opt.value = n;
    opt.textContent = n;
    el.fileSelect.appendChild(opt);
  });
  el.fileSelect.disabled = false;
  el.fileSelect.value = data.default || names[0];
  await loadContest(el.fileSelect.value);
}

async function loadContest(fileName) {
  const res = await fetch("/api/contest?file=" + encodeURIComponent(fileName));
  const data = await res.json();
  if (data.error) {
    el.title.textContent = "加载失败";
    el.sub.textContent = data.error;
    return;
  }
  payload = data;
  events = data.events || [];
  durationMs = data.durationMs ||
    (events.length ? events[events.length - 1].t + 60 * 60 * 1000 : 0);
  startDate = data.startAt ? new Date(data.startAt) : null;

  // 封榜窗口：优先使用数据中的 frozenDuration；无配置（为 0）时默认封榜最后一个小时，
  // 且不超过比赛时长的一半，避免整场被冻结。
  freezeMs = (data.frozenDurationMs > 0) ? data.frozenDurationMs : DEFAULT_FREEZE_MS;
  if (durationMs > 0) freezeMs = Math.min(freezeMs, Math.floor(durationMs / 2));
  el.freezeText.textContent = freezeMs > 0 ? "封榜（末 " + fmtHMS(freezeMs) + "）" : "封榜";
  el.freezeToggle.title = freezeMs > 0
    ? "封榜开始于 " + fmtHMS(Math.max(durationMs - freezeMs, 0)) + "，封榜内提交以『待判』呈现"
    : "封榜";

  el.title.textContent = data.title || "XCPC 榜单回放";
  el.sub.textContent = [
    data.source,
    (data.problems || []).length + " 题",
    (data.teams || []).length + " 队",
    events.length + " 条提交",
  ].join(" · ");

  freezeOn = el.freezeToggle.checked;
  buildProblemHeader();
  buildRows();
  buildSpeedButtons();
  buildChartBins();
  resetToStart();
  el.boardEmpty.classList.add("hidden");
  updateBoardStat();
  render();
}

/* ----------------------------- 构建 DOM ----------------------------- */
function buildProblemHeader() {
  el.boardHeadRow.innerHTML = "";
  const thRank = document.createElement("th");
  thRank.className = "col-rank";
  thRank.textContent = "#";
  el.boardHeadRow.appendChild(thRank);

  const thTeam = document.createElement("th");
  thTeam.className = "col-team";
  thTeam.textContent = "队伍";
  el.boardHeadRow.appendChild(thTeam);

  payload.problems.forEach((p, j) => {
    const th = document.createElement("th");
    th.className = "prob-head";
    th.innerHTML =
      '<span class="prob-alias">' + escapeHtml(p.alias) + "</span><br>" +
      '<span class="prob-count" id="prob-count-' + j + '">0</span>';
    el.boardHeadRow.appendChild(th);
  });

  const thSolved = document.createElement("th");
  thSolved.className = "col-solved";
  thSolved.textContent = "过题";
  el.boardHeadRow.appendChild(thSolved);

  const thPenalty = document.createElement("th");
  thPenalty.className = "col-penalty";
  thPenalty.textContent = "罚时";
  el.boardHeadRow.appendChild(thPenalty);
}

function escapeHtml(s) {
  return String(s).replace(/[&<>"']/g, (c) => ({
    "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;",
  }[c]));
}

/** 更新榜单底部的队伍统计（总榜 / 正榜）。 */
function updateBoardStat() {
  if (!payload) return;
  const total = payload.teams.length;
  const official = payload.teams.filter((t) => t.official).length;
  const ghost = total - official;
  el.boardStat.textContent = boardMode === "official"
    ? "正榜 · 正式队伍 " + official + " 支"
    : "总榜 · 共 " + total + " 队（正式 " + official + " / 非正式 " + ghost + "）";
}

function buildRows() {
  el.boardBody.innerHTML = "";
  const officialOnly = boardMode === "official";
  rowInfos = payload.teams.map((team, i) => {
    const tr = document.createElement("tr");
    if (!team.official) {
      tr.classList.add("unofficial");
      // 正榜模式下隐藏非正式（打星）队伍行
      if (officialOnly) tr.classList.add("hidden");
    }
    tr.dataset.team = i;

    // 名次
    const tdRank = document.createElement("td");
    tdRank.className = "col-rank";
    tdRank.innerHTML =
      '<div class="rank-cell"><span class="rank-num">-</span>' +
      '<span class="medal"></span></div>';
    tr.appendChild(tdRank);

    // 队伍
    const tdTeam = document.createElement("td");
    tdTeam.className = "col-team";
    const meta = [team.organization, team.id].filter(Boolean).join(" · ") ||
      (team.official ? "" : "非正式队伍");
    tdTeam.innerHTML =
      '<div class="team-cell">' +
      '<span class="team-name" title="' + escapeHtml(team.name) + '">' +
      escapeHtml(team.name) + "</span>" +
      '<span class="team-meta">' + escapeHtml(meta) + "</span></div>";
    tr.appendChild(tdTeam);

    // 题目单元格
    const probEls = payload.problems.map((_, j) => {
      const td = document.createElement("td");
      td.className = "prob-cell none";
      tr.appendChild(td);
      return td;
    });

    // 过题 / 罚时
    const tdSolved = document.createElement("td");
    tdSolved.className = "col-solved";
    tdSolved.textContent = "0";
    tr.appendChild(tdSolved);

    const tdPenalty = document.createElement("td");
    tdPenalty.className = "col-penalty";
    tdPenalty.textContent = "0";
    tr.appendChild(tdPenalty);

    el.boardBody.appendChild(tr);

    return {
      tr, probEls, solvedEl: tdSolved, penaltyEl: tdPenalty,
      rankNum: tdRank.querySelector(".rank-num"),
      medalEl: tdRank.querySelector(".medal"),
      prevRank: -1, dirty: false,
    };
  });
}

function buildSpeedButtons() {
  el.speedGroup.innerHTML = '<span class="ctrl-label">倍速</span>';
  SPEEDS.forEach((s) => {
    const btn = document.createElement("button");
    btn.className = "speed-btn";
    btn.dataset.speed = String(s);
    btn.textContent = (s === "MAX" ? "MAX" : s + "×");
    btn.title = s === "MAX" ? "每帧推进一条提交（最快）"
      : "每秒推进 " + s + " 秒比赛时间";
    btn.addEventListener("click", () => {
      speed = s;
      updateSpeedButtons();
    });
    el.speedGroup.appendChild(btn);
  });
  updateSpeedButtons();
}

function updateSpeedButtons() {
  el.speedGroup.querySelectorAll(".speed-btn").forEach((b) => {
    b.classList.toggle("active", String(b.dataset.speed) === String(speed));
  });
}

/* ----------------------------- 回放状态 ----------------------------- */
/** 构建全新的回放状态并返回（供模块级 state 使用）。 */
function makeState() {
  const nProb = payload.problems.length;
  const s = {
    teams: payload.teams.map(() => ({
      solved: 0,
      penalty: 0,
      probs: Array.from({ length: nProb }, () => ({
        attempts: 0, ac: false, acMin: 0, pendingCount: 0, fb: false,
      })),
    })),
    acCounts: new Array(nProb).fill(0),
  };
  firstBlood = new Array(nProb).fill(-1);
  state = s;
  return s;
}

function resetToStart() {
  state = makeState();
  nowMs = 0;
  eventIndex = 0;
  feedQueue = [];
  el.feedList.innerHTML = "";
  el.feedEmpty.style.display = "";
  fullRender = true;
  setValidateBadge(false, "");
  render();
}

/**
 * 应用一条提交事件。
 * @param {Object} ev 事件 {t, team, problem, result, ac}
 * @param {Array}  batch 收集本次应用的动态
 */
function applyEvent(ev, batch) {
  const frozen = isFrozenTime(ev.t);
  const st = state.teams[ev.team];
  const pr = st.probs[ev.problem];
  const noPenalty = payload.noPenalty || [];

  if (frozen) {
    // 封榜：所有提交统一标记为待判，并累计封榜内提交次数；不改变得分与排名
    pr.pendingCount += 1;
    if (batch) batch.push(ev);
    return;
  }
  if (ev.ac && !pr.ac) {
    // 通过：罚时 = 通过分钟数 + 20 × 通过前失败尝试数
    pr.ac = true;
    pr.acMin = Math.floor(ev.t / 60000);
    st.solved += 1;
    st.penalty += pr.acMin + pr.attempts * payload.penaltyMin;
    state.acCounts[ev.problem] += 1;
    if (firstBlood[ev.problem] === -1) {
      firstBlood[ev.problem] = ev.team;
      pr.fb = true;
    }
  } else if (!noPenalty.includes(ev.result)) {
    // 计罚时的失败提交才累计尝试次数（CE 等不计）
    pr.attempts += 1;
  }
  if (batch) batch.push(ev);
}

/** 将回放状态重建到指定时刻（用于拖动 / 跳转）。 */
function seekTo(ms, opts) {
  opts = opts || {};
  const target = clamp(ms, 0, durationMs);
  state = makeState();
  const batch = [];
  eventIndex = 0;
  const lastIdx = events.length;
  setValidateBadge(false, "");   // 离开/进入结束态前先隐藏校验徽章，避免残留
  while (eventIndex < lastIdx && events[eventIndex].t <= target) {
    applyEvent(events[eventIndex], batch);
    eventIndex += 1;
  }
  nowMs = target;
  fullRender = true;
  suppressPop = true;
  if (!opts.keepFeed) {
    feedQueue = [];
    el.feedList.innerHTML = "";
    el.feedEmpty.style.display = "";
  }
  if (eventIndex >= lastIdx && nowMs >= durationMs) {
    playing = false;
    updatePlayButton();
    runSelfCheck();
  }
  applyBatch(batch, true);
  render();
}

/** 排行榜：标准 ICPC 排序（过题数降序、罚时升序、并列同名次）。
 *  正榜（official）模式下仅对正式队伍排名。 */
function computeRanks() {
  const teams = state.teams;
  const order = teams.map((_, i) => i)
    .filter(i => boardMode !== "official" || payload.teams[i].official)
    .sort((a, b) =>
      teams[b].solved - teams[a].solved || teams[a].penalty - teams[b].penalty);
  const out = [];
  let prevKey = null;
  let prevRank = 0;
  for (let pos = 0; pos < order.length; pos++) {
    const ti = order[pos];
    const key = teams[ti].solved + ":" + teams[ti].penalty;
    const rank = key === prevKey ? prevRank : pos + 1;
    if (key !== prevKey) { prevKey = key; prevRank = rank; }
    out.push({ rank, team: ti, solved: teams[ti].solved, penalty: teams[ti].penalty });
  }
  return out;
}

/** 仅在正式队伍内的名次（用于奖牌划分）。 */
function computeOfficialRanks() {
  const teams = state.teams;
  const off = [];
  payload.teams.forEach((t, i) => { if (t.official) off.push(i); });
  off.sort((a, b) => teams[b].solved - teams[a].solved || teams[a].penalty - teams[b].penalty);
  const map = new Map();
  // noTied 时强制展开并列名次（顺序名次），保证各段恰好分到规定人数
  if (payload.noTied) {
    off.forEach((ti, pos) => { map.set(ti, pos + 1); });
    return map;
  }
  let prevKey = null, prevRank = 0;
  off.forEach((ti, pos) => {
    const key = teams[ti].solved + ":" + teams[ti].penalty;
    const rank = key === prevKey ? prevRank : pos + 1;
    if (key !== prevKey) { prevKey = key; prevRank = rank; }
    map.set(ti, rank);
  });
  return map;
}

/* ----------------------------- 渲染 ----------------------------- */
function render() {
  if (!payload) return;
  const ranked = computeRanks();
  const offRanks = computeOfficialRanks();
  const [gold, silver, bronze] = payload.medals || [0, 0, 0];

  // 榜单行
  for (const entry of ranked) {
    const info = rowInfos[entry.team];
    if (!info.dirty && !fullRender && info.prevRank === entry.rank) continue;
    updateRow(info, entry, offRanks, gold, silver, bronze);
  }
  fullRender = false;
  reorderRows(ranked);

  // 题目表头实时过题数
  state.acCounts.forEach((c, j) => {
    const node = $("prob-count-" + j);
    if (node && node.textContent !== String(c)) node.textContent = String(c);
  });

  // 计时 / 进度 / 统计
  const pct = durationMs ? (nowMs / durationMs) * 100 : 0;
  el.clockElapsed.textContent = fmtHMS(nowMs);
  el.clockProgress.textContent = pct.toFixed(1) + "%";
  if (startDate) {
    const wall = new Date(startDate.getTime() + nowMs);
    el.clockWall.textContent =
      wall.toLocaleTimeString("zh-CN", { hour12: false });
  }
  if (!dragging) {
    el.timeline.value = Math.round((pct / 100) * 1000);
  }
  el.timeline.style.setProperty("--pct", pct + "%");
  el.statEvent.textContent = eventIndex + " / " + events.length;
  el.statAc.textContent = ranked.length ? ranked.reduce((s, e) => s + e.solved, 0) : 0;
  const leader = ranked.length ? payload.teams[ranked[0].team].name : "—";
  if (el.statLeader.textContent !== leader) el.statLeader.textContent = leader;

  drawChart();
  // 跳转渲染结束后恢复单元格动画
  suppressPop = false;
}

function probCellView(pr) {
  // 封榜内提交优先显示：统一为「?」，并标定封榜后的提交次数
  if (pr.pendingCount > 0) {
    return ["?" + pr.pendingCount, "pending"];
  }
  if (pr.ac) {
    return [fmtMin(pr.acMin) + (pr.fb ? "★" : ""), "st" + (pr.fb ? " fb" : "")];
  }
  if (pr.attempts) return ["×" + pr.attempts, "wrong"];
  return ["", "none"];
}

function updateRow(info, entry, offRanks, gold, silver, bronze) {
  const moved = info.prevRank >= 0 && info.prevRank !== entry.rank;
  info.rankNum.textContent = entry.rank;
  info.rankNum.classList.toggle("rank-moved", moved);

  const oRank = offRanks.get(entry.team) || Infinity;
  let medal = "";
  if (gold && oRank <= gold) medal = "gold";
  else if (silver && oRank <= silver) medal = "silver";
  else if (bronze && oRank <= bronze) medal = "bronze";
  info.medalEl.className = "medal" + (medal ? " " + medal : "");

  const st = state.teams[entry.team];
  for (let j = 0; j < st.probs.length; j++) {
    const elProb = info.probEls[j];
    const [txt, cls] = probCellView(st.probs[j]);
    if (elProb.textContent !== txt || !elProb.className.includes(cls)) {
      elProb.textContent = txt;
      elProb.className = "prob-cell " + cls;
      if (!suppressPop) elProb.classList.add("pop");
    }
  }

  if (info.solvedEl.textContent !== String(entry.solved)) {
    info.solvedEl.textContent = String(entry.solved);
    if (!suppressPop) info.tr.classList.add("flash");
  }
  if (info.penaltyEl.textContent !== String(entry.penalty)) {
    info.penaltyEl.textContent = String(entry.penalty);
  }
  info.prevRank = entry.rank;
  info.dirty = false;
}

function reorderRows(ranked) {
  const tbody = el.boardBody;
  const children = tbody.children;
  for (let i = 0; i < ranked.length; i++) {
    const row = rowInfos[ranked[i].team].tr;
    if (children[i] !== row) tbody.insertBefore(row, children[i]);
  }
}

/* ----------------------------- 事件流 ----------------------------- */
function applyBatch(batch, silent) {
  if (!batch || !batch.length) return;
  for (const ev of batch) {
    rowInfos[ev.team].dirty = true;
    if (!silent) {
      const st = state.teams[ev.team].probs[ev.problem];
      if (ev.ac && st.ac && st.acMin === Math.floor(ev.t / 60000)) {
        rowInfos[ev.team].tr.classList.add("flash");
      }
    }
  }
  if (silent) return;
  for (const ev of batch) pushFeed(ev);
  el.feedTitle.classList.toggle("live", true);
}

function pushFeed(ev) {
  el.feedEmpty.style.display = "none";
  const li = document.createElement("li");
  li.className = "feed-item";

  const frozen = isFrozenTime(ev.t);
  const team = payload.teams[ev.team];
  const prob = payload.problems[ev.problem];

  let cls, text;
  if (frozen) {
    cls = "pending"; text = "?";
  } else if (ev.ac) {
    const isFb = firstBlood[ev.problem] === ev.team && state.teams[ev.team].probs[ev.problem].fb;
    cls = isFb ? "fb" : "ac";
    text = isFb ? "FB" : "AC";
  } else {
    cls = "wrong"; text = ev.result;
  }

  li.innerHTML =
    '<span class="feed-time">' + fmtHMS(ev.t) + "</span>" +
    '<span class="feed-team" title="' + escapeHtml(team.name) + '">' +
      escapeHtml(team.name) + "</span>" +
    '<span class="feed-prob">' + escapeHtml(prob.alias) + "</span>" +
    '<span class="feed-result ' + cls + '">' + escapeHtml(text) + "</span>";

  el.feedList.prepend(li);
  // 限制条数，避免 DOM 膨胀
  while (el.feedList.children.length > 60) el.feedList.removeChild(el.feedList.lastChild);
}

/* ----------------------------- 活动图 ----------------------------- */
function buildChartBins() {
  if (!events.length || !durationMs) { chartBins = null; return; }
  const binCount = clamp(Math.round(durationMs / 60000), 60, 360);
  const binMs = durationMs / binCount;
  const ac = new Array(binCount).fill(0);
  const other = new Array(binCount).fill(0);
  for (const ev of events) {
    const b = clamp(Math.floor(ev.t / binMs), 0, binCount - 1);
    if (ev.ac) ac[b] += 1; else other[b] += 1;
  }
  chartBins = { binCount, binMs, ac, other };
}

function drawChart() {
  const canvas = el.activityChart;
  const rect = canvas.getBoundingClientRect();
  if (!rect.width || !rect.height) return;
  const dpr = window.devicePixelRatio || 1;
  const W = Math.round(rect.width * dpr);
  const H = Math.round(rect.height * dpr);
  if (canvas.width !== W) canvas.width = W;
  if (canvas.height !== H) canvas.height = H;
  const ctx = canvas.getContext("2d");
  ctx.clearRect(0, 0, W, H);

  const x = (ms) => (ms / durationMs) * W;
  // 播放头
  ctx.strokeStyle = "rgba(255,255,255,0.85)";
  ctx.lineWidth = Math.max(1.5, dpr);
  ctx.shadowColor = "rgba(61,240,90,0.8)";
  ctx.shadowBlur = 6 * dpr;
  ctx.beginPath();
  ctx.moveTo(x(nowMs), 0);
  ctx.lineTo(x(nowMs), H);
  ctx.stroke();
  ctx.shadowBlur = 0;

  if (!chartBins) return;
  const { binCount, binMs, ac, other } = chartBins;
  let max = 1;
  for (let i = 0; i < binCount; i++) max = Math.max(max, ac[i] + other[i]);
  const bw = W / binCount;
  for (let i = 0; i < binCount; i++) {
    const hAc = (ac[i] / max) * H;
    const hOt = ((ac[i] + other[i]) / max) * H;
    if (hOt > 0.5) {
      ctx.fillStyle = "rgba(255,92,92,0.55)";
      ctx.fillRect(i * bw, H - hOt, Math.max(1, bw - 1), hOt);
    }
    if (hAc > 0.5) {
      ctx.fillStyle = "rgba(61,240,90,0.8)";
      ctx.fillRect(i * bw, H - hAc, Math.max(1, bw - 1), hAc);
    }
  }

  // 封榜窗口遮罩
  if (freezeOn && freezeMs > 0 && durationMs > 0) {
    const fx = x(Math.max(durationMs - freezeMs, 0));
    ctx.fillStyle = "rgba(247, 185, 85, 0.07)";
    ctx.fillRect(fx, 0, W - fx, H);
    ctx.fillStyle = "rgba(247, 185, 85, 0.55)";
    ctx.fillRect(fx, 0, 1, H);
  }
}

/* ----------------------------- 播放循环 ----------------------------- */
function tick(ts) {
  if (!lastTs) lastTs = ts;
  const dt = clamp((ts - lastTs) / 1000, 0, 0.1);
  lastTs = ts;

  if (playing && payload) {
    const batch = [];
    if (speed === "MAX") {
      if (eventIndex < events.length) {
        applyEvent(events[eventIndex], batch);
        eventIndex += 1;
        nowMs = events[eventIndex - 1].t;
      } else if (nowMs < durationMs) {
        nowMs = durationMs;
      }
    } else {
      nowMs = clamp(nowMs + dt * speed * 1000, 0, durationMs);
      while (eventIndex < events.length && events[eventIndex].t <= nowMs) {
        applyEvent(events[eventIndex], batch);
        eventIndex += 1;
      }
    }
    if (batch.length) {
      applyBatch(batch, false);
      el.feedTitle.classList.add("live");
    }
    if (eventIndex >= events.length && nowMs >= durationMs) {
      playing = false;
      updatePlayButton();
      el.feedTitle.classList.remove("live");
      runSelfCheck();
    }
    render();
  }
  requestAnimationFrame(tick);
}

/* ----------------------------- 控制 ----------------------------- */
function togglePlay() {
  if (!payload) return;
  if (playing) {
    playing = false;
    el.feedTitle.classList.remove("live");
  } else {
    if (eventIndex >= events.length && nowMs >= durationMs) resetToStart();
    playing = true;
    el.feedTitle.classList.add("live");
  }
  updatePlayButton();
}

function updatePlayButton() {
  el.btnPlay.textContent = playing ? "❚❚" : "▶";
  el.btnPlay.title = playing ? "暂停（空格）" : "播放（空格）";
}

function nextEvent() {
  if (!payload) return;
  if (eventIndex >= events.length) { seekTo(durationMs); return; }
  seekTo(events[eventIndex].t);
}

function prevEvent() {
  if (!payload) return;
  if (eventIndex <= 0) { seekTo(0); return; }
  seekTo(events[eventIndex - 1].t - 1);
}

function jumpStart() { if (payload) seekTo(0); }
function jumpEnd() { if (payload) seekTo(durationMs); }

/* ----------------------------- 结果校验 ----------------------------- */
function runSelfCheck() {
  if (freezeOn || !payload || !payload.finalBoard) return;
  const fb = payload.finalBoard;
  let mismatches = 0;
  for (let i = 0; i < payload.teams.length; i++) {
    if (state.teams[i].solved !== fb.solved[i] ||
        state.teams[i].penalty !== fb.penalty[i]) mismatches += 1;
  }
  const ranks = computeRanks();
  const ok = mismatches === 0 && ranks.length === fb.rank.length;
  setValidateBadge(ok, ok ? "结果校验通过" : mismatches + " 项与快照不一致");
}

function setValidateBadge(ok, text) {
  if (!text) {
    el.validateBadge.classList.add("hidden");
    el.validateBadge.classList.remove("warn");
    return;
  }
  el.validateBadge.textContent = text;
  el.validateBadge.classList.toggle("hidden", false);
  el.validateBadge.classList.toggle("warn", !ok);
}

/* ----------------------------- 事件绑定 ----------------------------- */
el.btnPlay.addEventListener("click", togglePlay);
el.btnStart.addEventListener("click", jumpStart);
el.btnEnd.addEventListener("click", jumpEnd);
el.btnPrev.addEventListener("click", prevEvent);
el.btnNext.addEventListener("click", nextEvent);

el.fileSelect.addEventListener("change", () => {
  if (el.fileSelect.value) loadContest(el.fileSelect.value);
});

// 总榜 / 正榜切换：重建行（正榜隐藏非正式队伍）并全量重绘
el.boardFilter.addEventListener("change", () => {
  boardMode = el.boardFilter.value;
  buildRows();
  updateBoardStat();
  fullRender = true;
  render();
});

el.freezeToggle.addEventListener("change", () => {
  freezeOn = el.freezeToggle.checked;
  // 封榜下最终榜单与快照必然不同，隐藏校验徽章避免误导
  if (freezeOn) setValidateBadge(false, "");
  if (payload) seekTo(nowMs);
});

el.feedCollapse.addEventListener("click", () => {
  el.feedPanel.classList.toggle("collapsed");
});

// 进度条：拖拽时暂停并实时跳转
el.timeline.addEventListener("pointerdown", () => {
  dragging = true;
  if (playing) { playing = false; updatePlayButton(); }
});
el.timeline.addEventListener("input", () => {
  if (payload) seekTo((el.timeline.value / 1000) * durationMs);
});
window.addEventListener("pointerup", () => { dragging = false; });

// 键盘快捷键
document.addEventListener("keydown", (e) => {
  if (e.target.tagName === "SELECT") return;
  if (e.code === "Space") {
    e.preventDefault();
    togglePlay();
  } else if (e.key === "ArrowRight") {
    nextEvent();
  } else if (e.key === "ArrowLeft") {
    prevEvent();
  } else if (e.key === "ArrowUp") {
    const idx = clamp(SPEEDS.indexOf(speed) + 1, 0, SPEEDS.length - 1);
    speed = SPEEDS[idx];
    updateSpeedButtons();
  } else if (e.key === "ArrowDown") {
    const idx = clamp(SPEEDS.indexOf(speed) - 1, 0, SPEEDS.length - 1);
    speed = SPEEDS[idx];
    updateSpeedButtons();
  }
});

/* ----------------------------- 启动 ----------------------------- */
requestAnimationFrame(tick);
loadFiles().catch((err) => {
  el.sub.textContent = "加载数据失败：" + err;
});
