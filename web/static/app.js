/* Binance 测试网量化仪表盘 前端逻辑 */
"use strict";

const REFRESH_MS = 2000;
const MODE_LABELS = { donchian: "Donchian", multi: "多策略", grid: "网格", ma_cross: "均线", rsi: "RSI", bollinger: "布林带" };
let cfg = null;
let lastEquity = null;
let lastSnapshot = null;
let lastTickTs = 0;   // 后端主循环最近一次真实 tick 时间(秒)

const $ = (id) => document.getElementById(id);
const fmt = (v, d = 2) => (v == null || isNaN(v)) ? "--" : Number(v).toFixed(d);
const fmtPrice = (v) => (v == null || v <= 0) ? "--" : Number(v).toLocaleString("en-US", { maximumFractionDigits: 6 });
const fmtQty = (v) => (v == null || isNaN(v)) ? "--" : Number(Number(v).toFixed(8)).toString();
const cls = (v) => (v > 0 ? "pos" : v < 0 ? "neg" : "");
const sign = (v) => (v > 0 ? "+" : "");

function scoreBar(v) {
  if (v == null || isNaN(v)) return "";
  const pct = Math.max(-100, Math.min(100, v * 100));
  const color = v >= 0 ? "var(--green)" : "var(--red)";
  const left = v >= 0 ? 50 : 50 + pct;
  const width = Math.abs(pct);
  return `<span class="mono">${sign(v)}${fmt(v, 2)}</span><span class="score-bar"><span class="score-fill" style="left:${left}%;width:${width}%;background:${color}"></span></span>`;
}

function regimePill(regime) {
  const map = { ranging: ["pill-rang", "震荡"], trend_up: ["pill-up", "上涨"], trend_down: ["pill-down", "下跌"] };
  const [c, t] = map[regime] || ["pill-none", regime || "--"];
  return `<span class="pill ${c}">${t}</span>`;
}

function fmtTime(ts) {
  if (!ts) return "--";
  const d = new Date(ts * 1000);
  return d.toLocaleString("zh-CN", { hour12: false });
}

async function loadConfig() {
  try {
    const r = await fetch("/api/config");
    cfg = await r.json();
    $("mode-badge").textContent = cfg.mode === "live" ? "LIVE 测试网" : "PAPER 模拟";
    $("mode-badge").className = "badge " + (cfg.mode === "live" ? "badge-live" : "badge-paper");
  } catch (e) { /* ignore */ }
}

// ---------------- 自动学习历史 ----------------
let lastLearnerKey = null;
async function loadLearner() {
  try {
    const r = await fetch("/api/learner");
    const d = await r.json();
    if (!d.current) {
      $("learner-head").textContent = "尚无学习记录（系统启动后自动学习一轮）";
      return;
    }
    const cur = d.current;
    const curKey = `${cur.timeframe}-${cur.entry_n}/${cur.exit_n}/${cur.sl_atr}`;
    const head = $("learner-head");
    head.innerHTML = `当前生效: <b class="mono" style="color:var(--green)">${curKey}</b>` +
      ` &nbsp;|&nbsp; 评分 <b class="mono">${fmt(d.best_score, 2)}</b>` +
      ` &nbsp;|&nbsp; 上次学习: ${fmtTime(d.learned_at)}` +
      ` &nbsp;|&nbsp; 策略池 ${(d.rounds && d.rounds.length ? d.rounds[d.rounds.length-1].top3.length : 0)}+ 组合`;
    // 只在组合变化时打日志标记
    if (lastLearnerKey && lastLearnerKey !== curKey) {
      const el = document.createElement("div");
      el.className = "log-item tuning";
      el.innerHTML = `<span class="lt">${fmtTime(Date.now()/1000)}</span>` +
        `<span class="ltype" style="color:var(--muted);font-size:12px">[切换]</span>` +
        `<span class="lmsg">🤖 学习历史: 策略已切换 ${lastLearnerKey} → ${curKey}</span>`;
      const list = document.getElementById("log-list");
      if (list) list.prepend(el);
    }
    lastLearnerKey = curKey;

    const tb = document.querySelector("#learner-table tbody");
    const rounds = d.rounds || [];
    if (!rounds.length) {
      tb.innerHTML = `<tr><td colspan="5" class="empty">等待第一轮学习…</td></tr>`;
      return;
    }
    let html = "";
    for (const r of rounds.slice().reverse()) {
      const isCur = r.best_key === curKey;
      const top2 = r.top3[1] ? `${r.top3[1].key} (${fmt(r.top3[1].score, 2)})` : "--";
      const top3 = r.top3[2] ? `${r.top3[2].key} (${fmt(r.top3[2].score, 2)})` : "--";
      html += `<tr${isCur ? ' style="background:rgba(38,166,154,.06)"' : ""}>
        <td class="mono">${fmtTime(r.ts)}</td>
        <td class="mono">${isCur ? '🟢 ' : ''}${r.best_key}</td>
        <td class="mono pos">${fmt(r.best_score, 2)}</td>
        <td class="mono">${top2}</td>
        <td class="mono">${top3}</td>
      </tr>`;
    }
    tb.innerHTML = html;
  } catch (e) { /* ignore */ }
}

async function refresh() {
  let s;
  try {
    const r = await fetch("/api/state");
    s = await r.json();
  } catch (e) {
    $("status-badge").textContent = "连接断开";
    $("status-badge").className = "badge badge-halt";
    return;
  }

  // 状态
  let statusText = "运行中", statusCls = "badge-ok";
  if (s.halted) { statusText = "熔断: " + (s.halt_reason || "日亏损"); statusCls = "badge-halt"; }
  else if (s.paused) { statusText = "已暂停"; statusCls = "badge-pause"; }
  $("status-badge").textContent = statusText;
  $("status-badge").className = "badge " + statusCls;

  // 心跳: 记录后端主循环真实 tick 时间 (不是写死的)
  lastTickTs = s.last_tick_ts || lastTickTs;
  updateHeartbeat();

  // API 登录状态徽章
  renderLoginBadge(s.api);

  // 按钮
  const btn = $("btn-pause");
  btn.textContent = s.paused ? "恢复" : "暂停";
  btn.className = "btn " + (s.paused ? "btn-warn on" : "btn-warn");

  // 卡片
  setVal("c-equity", fmt(s.equity), cls(s.equity - s.day_start_equity));
  setVal("c-daypnl", `${sign(s.day_pnl)}${fmt(s.day_pnl)} (${sign(s.day_pnl_pct)}${fmt(s.day_pnl_pct)}%)`, cls(s.day_pnl));
  setVal("c-upnl", `${sign(s.unrealized)}${fmt(s.unrealized)}`, cls(s.unrealized));
  setVal("c-exposure", fmt(s.exposure) + " U");
  setVal("c-trades", `${s.strategy_stats.total_trades} 笔 / ${fmt(s.strategy_stats.win_rate, 1)}%`);
  setVal("c-realized", `${sign(s.strategy_stats.realized_pnl)}${fmt(s.strategy_stats.realized_pnl)} U`, cls(s.strategy_stats.realized_pnl));
  setVal("c-fees", fmt(s.strategy_stats.fees_paid) + " U");

  // 权益曲线
  drawChart(s.equity_history, s.day_start_equity);
  lastSnapshot = s;

  // 行情持仓表
  renderSymbols(s);
  renderSignals(s);
  renderTrades(s);
  renderModes(s);
  renderLogs(s);

  $("foot").textContent =
    `模式=${s.mode} | 权益=${fmt(s.equity)} | 起始=${fmt(s.day_start_equity)} | ` +
    `数据更新 ${s.last_tick_ts ? fmtTime(s.last_tick_ts) : "--"} | ` +
    `风控: 单笔≤${s.risk.max_single}U 总持仓≤${s.risk.max_total}U 日亏${(s.risk.daily_loss_stop * 100)}%熔断`;

  // 权益变化闪烁
  if (lastEquity != null && s.equity !== lastEquity) {
    const el = $("c-equity");
    el.style.transition = "color .3s";
    el.style.color = s.equity > lastEquity ? "var(--green)" : "var(--red)";
    setTimeout(() => { el.style.color = ""; }, 600);
  }
  lastEquity = s.equity;
}

function setVal(id, text, clsName) {
  const el = $(id);
  el.textContent = text;
  el.className = "value mono" + (clsName ? " " + clsName : "");
}

function renderSymbols(s) {
  const tb = document.querySelector("#symbols-table tbody");
  const posMap = {};
  s.positions.forEach((p) => { posMap[p.symbol] = p; });
  const order = cfg ? cfg.symbols : Object.keys(s.prices);
  let html = "";
  for (const sym of order) {
    const price = s.prices[sym];
    const chg = s.change_24h[sym];
    const pos = posMap[sym];
    // 该币种各模式信号: 优先取持仓模式的信号
    let sig = {};
    for (const k of Object.keys(s.signals)) {
      if (k.endsWith(":" + sym)) {
        sig = s.signals[k];
        if (pos && k.startsWith(pos.mode + ":")) break;
      }
    }
    const chgCls = chg >= 0 ? "pos" : "neg";
    html += `<tr>
      <td><b>${sym}</b></td>
      <td class="mono">${fmtPrice(price)}</td>
      <td class="mono ${chgCls}">${chg == null ? "--" : sign(chg) + fmt(chg, 2) + "%"}</td>
      <td>${regimePill(sig.regime)}</td>
      <td>${scoreBar(sig.combined != null ? sig.combined : sig.score)}</td>
      <td>${sig.mode ? (MODE_LABELS[sig.mode] || sig.mode) : "--"}</td>
      <td class="mono">${sig.leverage ? sig.leverage + "x" : "--"}</td>
      <td>${pos ? posPill(pos) : '<span class="pill pill-none">空仓</span>'}</td>
      <td class="mono">${pos ? fmtPrice(pos.entry) : "--"}</td>
      <td class="mono ${cls(pos ? pos.upnl : 0)}">${pos ? sign(pos.upnl) + fmt(pos.upnl) : "--"}</td>
      <td class="mono">${pos ? tpSlCell(pos) : "--"}</td>
    </tr>`;
  }
  tb.innerHTML = html || `<tr><td colspan="11" class="empty">暂无数据</td></tr>`;
}

function posPill(p) {
  return `<span class="pill ${p.side === "LONG" ? "pill-long" : "pill-short"}">${p.side === "LONG" ? "多" : "空"} ${p.qty} @${p.leverage}x</span>`;
}

// TP/SL 列: donchian 模式无固定TP, 显示"移动止损"替代 0.00
function tpSlCell(p) {
  if (p.tp && p.tp > 0) return fmtPrice(p.tp) + " / " + fmtPrice(p.sl);
  const lock = p.sl > 0 ? `移动止损 ${fmtPrice(p.sl)}` : "--";
  return `<span title="海龟策略: 不设固定止盈, 用移动止损锁利 + 通道反向出场">∞ / ${lock}</span>`;
}

function stratName(n) {
  const map = { grid: "网格", ma_cross: "均线", rsi: "RSI", bollinger: "布林带", none: "--" };
  return map[n] || n || "--";
}

// 策略信号模块 - 按启用模式动态生成 Tab
// 每个 Tab 独立显示该模式的所有交易对信号
let sigActiveTab = null;
const SIG_PANEL_HTML = `
  <div class="table-wrap">
    <table class="sig-tbl">
      <thead><tr>
        <th>交易对</th><th>状态</th><th>方向</th><th>评分</th><th>ATR%</th><th>强度</th><th>杠杆</th><th>距通道</th><th>最近分析</th>
      </tr></thead>
      <tbody></tbody>
    </table>
  </div>`;

function ensureSigPanel(mode) {
  let panel = document.getElementById("sig-panel-" + mode);
  if (!panel) {
    panel = document.createElement("div");
    panel.id = "sig-panel-" + mode;
    panel.className = "sig-tab-panel";
    panel.dataset.mode = mode;
    panel.innerHTML = SIG_PANEL_HTML;
    $("sig-tab-panels").appendChild(panel);
  }
  return panel;
}

function setActiveSigTab(mode) {
  sigActiveTab = mode;
  document.querySelectorAll(".sig-tab").forEach(el => {
    el.classList.toggle("active", el.dataset.tab === mode);
  });
  document.querySelectorAll(".sig-tab-panel").forEach(el => {
    el.classList.toggle("active", el.dataset.mode === mode);
  });
}

function renderSignalsTabs(usedModes) {
  const tabs = $("sig-tabs");
  if (!tabs) return;
  // 模式顺序: 已启用 > 已出现信号; 其余兜底
  const enabled = enabledModes.filter(m => MODE_LABELS[m]);
  const seen = usedModes.filter(m => MODE_LABELS[m] && !enabled.includes(m));
  const list = [...enabled, ...seen];
  if (!list.length) {
    tabs.innerHTML = `<span class="hint" style="padding:12px">尚未启用任何策略模式 · 打开 ⚙️ 设置 勾选</span>`;
    $("sig-tab-panels").innerHTML = `<div class="empty">暂无信号数据</div>`;
    return;
  }
  tabs.innerHTML = list.map((m, i) => {
    return `<div class="sig-tab ${(!sigActiveTab || sigActiveTab === m) && i === 0 ? "active" : ""}" data-tab="${m}">
      ${MODE_LABELS[m] || m}<span class="tab-count" data-count="${m}">0</span>
    </div>`;
  }).join("");
  // 为每个模式建面板
  list.forEach(m => ensureSigPanel(m));
  // 清理掉之前多余的面板
  document.querySelectorAll(".sig-tab-panel").forEach(el => {
    if (!list.includes(el.dataset.mode)) el.remove();
  });
  // 点击切换
  tabs.querySelectorAll(".sig-tab").forEach(el => {
    el.addEventListener("click", () => setActiveSigTab(el.dataset.tab));
  });
  // 默认激活
  if (!sigActiveTab || !list.includes(sigActiveTab)) setActiveSigTab(list[0]);
}

function renderSignals(s) {
  const used = new Set();
  // 收集每个模式下的信号 key
  const byMode = {};
  Object.keys(s.signals).sort().forEach(key => {
    const g = s.signals[key];
    if (!g) return;
    let mode, sym;
    if (key.includes(":")) {
      [mode, sym] = key.split(":");
    } else {
      mode = g.mode || "donchian";
      sym = key;
    }
    if (!MODE_LABELS[mode]) return;
    used.add(mode);
    if (!byMode[mode]) byMode[mode] = [];
    byMode[mode].push({ key, g, sym });
  });
  renderSignalsTabs([...used]);

  // 渲染每个模式面板
  const modesToRender = [...used].sort();
  modesToRender.forEach(mode => {
    const panel = document.getElementById("sig-panel-" + mode);
    if (!panel) return;
    const tb = panel.querySelector("tbody");
    const rows = byMode[mode] || [];
    // tab 计数
    const cntEl = document.querySelector(`.sig-tab[data-tab="${mode}"] .tab-count`);
    if (cntEl) cntEl.textContent = rows.length;
    if (!rows.length) {
      tb.innerHTML = `<tr><td colspan="9" class="empty">该模式暂无信号 · 等待 K 线分析…</td></tr>`;
      return;
    }
    let html = "";
    for (const { g, sym, key } of rows) {
      const waiting = g.action === "等待" || g.action == null;
      const price = s.prices[sym];
      let dist = "--";
      if (mode === "donchian" && price > 0 && g.up_level > 0 && g.dn_level > 0) {
        const toUp = (g.up_level - price) / price * 100;
        const toDn = (price - g.dn_level) / price * 100;
        dist = `上${sign(-toUp)}${fmt(Math.abs(toUp), 2)}% / 下${sign(-toDn)}${fmt(Math.abs(toDn), 2)}%`;
      } else if (price > 0) {
        const r = g.regime === "trend_up" ? "↑趋势" : g.regime === "trend_down" ? "↓趋势" : "震荡";
        dist = r;
      }
      html += `<tr>
        <td><b>${sym}</b><span class="mono" style="color:var(--muted);font-size:11px;margin-left:6px">${key}</span></td>
        <td>${regimePill(g.regime)}</td>
        <td class="mono">${waiting ? '<span class="pill pill-none">等待</span>' : (g.action === "LONG" ? '<span class="pill pill-long">▲做多</span>' : '<span class="pill pill-short">▼做空</span>')}</td>
        <td>${scoreBar(g.combined != null ? g.combined : g.score)}</td>
        <td class="mono">${fmt(g.atr_pct, 3)}%</td>
        <td class="mono">${fmt(g.strength, 2)}</td>
        <td class="mono ${g.leverage > 1 ? "pos" : ""}">${g.leverage}x</td>
        <td class="mono">${dist}</td>
        <td class="mono">${fmtTime(g.ts)}</td>
      </tr>`;
    }
    tb.innerHTML = html;
  });
}

function renderTrades(s) {
  const tb = document.querySelector("#trades-table tbody");
  const orders = s.orders || [];
  if (!orders.length) {
    tb.innerHTML = `<tr><td colspan="9" class="empty">暂无成交记录（系统运行后自动记录每一笔下单/卖出）</td></tr>`;
    return;
  }
  let html = "";
  for (const o of orders) {
    const isOpen = o.action === "OPEN";
    const pnlCls = cls(o.pnl);
    html += `<tr>
      <td class="mono">${fmtTime(o.ts)}</td>
      <td>${isOpen
        ? '<span class="pill pill-long">下单</span>'
        : '<span class="pill pill-short">卖出</span>'}</td>
      <td><b>${o.symbol}</b></td>
      <td><span class="pill ${o.side === "LONG" ? "pill-long" : "pill-short"}">${o.side === "LONG" ? "多" : "空"}</span></td>
      <td class="mono">${fmtQty(o.qty)}</td>
      <td class="mono">${fmtPrice(o.price)}</td>
      <td class="mono">${o.fees == null ? "--" : fmt(o.fees) + " U"}</td>
      <td class="mono ${pnlCls}">${o.pnl == null ? "--" : sign(o.pnl) + fmt(o.pnl) + " U"}</td>
      <td>${o.reason || "--"}</td>
    </tr>`;
  }
  tb.innerHTML = html;
}

// ---------------- 模式对比表 ----------------
let enabledModes = [];
let modeWeights = {};

function renderModes(s) {
  const tb = document.querySelector("#modes-table tbody");
  if (!tb) return;
  const stats = s.mode_stats || {};
  const all = Object.keys(MODE_LABELS);
  // 用已启用的模式 + 有统计的模式合并展示
  const shown = [...new Set([...enabledModes, ...Object.keys(stats)])].filter(m => all.includes(m));
  if (!shown.length) {
    tb.innerHTML = `<tr><td colspan="7" class="empty">暂无模式数据（系统运行后自动统计）</td></tr>`;
    return;
  }
  let html = "";
  for (const m of shown) {
    const st = stats[m] || { total_trades: 0, wins: 0, win_rate: 0, realized_pnl: 0, fees_paid: 0 };
    const enabled = enabledModes.includes(m);
    const w = modeWeights[m] != null ? modeWeights[m] : 1.0;
    html += `<tr${enabled ? "" : ' style="opacity:.45"'}>
      <td><b>${MODE_LABELS[m] || m}</b></td>
      <td>${enabled ? '<span class="pill pill-long">启用</span>' : '<span class="pill pill-none">停用</span>'}</td>
      <td class="mono ${w > 1.05 ? "pos" : w < 0.95 ? "neg" : ""}">${fmt(w, 2)}x</td>
      <td class="mono">${st.total_trades}</td>
      <td class="mono">${fmt(st.win_rate, 1)}%</td>
      <td class="mono ${cls(st.realized_pnl)}">${sign(st.realized_pnl)}${fmt(st.realized_pnl)} U</td>
      <td class="mono">${fmt(st.fees_paid)} U</td>
    </tr>`;
  }
  tb.innerHTML = html;
}

// ---------------- 操作日志时间线 ----------------
const LOG_TYPE_LABEL = {
  system: "系统", trade: "交易", risk: "风控", tuning: "调参", info: "信息", error: "异常",
};

function renderLogs(s) {
  const list = document.getElementById("log-list");
  const events = s.events || [];
  if (!events.length) {
    list.innerHTML = `<div class="empty">暂无日志（系统启动后自动记录每一步操作）</div>`;
    return;
  }
  let html = "";
  for (const e of events) {
    const clsName = "log-item " + e.type + (e.type === "trade" && e.msg.includes("🔻") ? " neg" : "");
    html += `<div class="${clsName}">
      <span class="lt">${fmtTime(e.ts)}</span>
      <span class="ltype" style="color:var(--muted);font-size:12px">[${LOG_TYPE_LABEL[e.type] || e.type}]</span>
      <span class="lmsg">${escapeHtml(e.msg)}</span>
    </div>`;
  }
  list.innerHTML = html;
  list.scrollTop = 0;
}

function escapeHtml(s) {
  return String(s).replace(/&/g, "&amp;").replace(/</g, "&lt;").replace(/>/g, "&gt;");
}

// ---------------- 心跳指示 ----------------
// 基于后端主循环的真实 tick 时间 (last_tick_ts), 每秒重算显示, 绝无写死
function updateHeartbeat() {
  const el = document.getElementById("heartbeat");
  if (!el) return;
  if (!lastTickTs) {
    el.textContent = "心跳 等待中…";
    el.className = "badge";
    return;
  }
  const age = Math.floor(Date.now() / 1000 - lastTickTs);
  const tickTime = new Date(lastTickTs * 1000).toLocaleTimeString("zh-CN", { hour12: false });
  if (age <= 10) {
    el.textContent = `心跳 ${tickTime} · ${age}s前`;
    el.className = "badge badge-ok pulse";
  } else if (age <= 60) {
    el.textContent = `心跳 ${tickTime} · ${age}s前 ⚠`;
    el.className = "badge warn pulse";
  } else {
    el.textContent = `心跳 ${tickTime} · ${Math.floor(age / 60)}分钟前 🚨`;
    el.className = "badge dead";
  }
}

function startHeartbeatTicker() {
  // 页面打开后: 立即刷新一次, 之后每 1 秒重算 (数据来自真实 tick 时间)
  updateHeartbeat();
  setInterval(updateHeartbeat, 1000);
}

// ---------------- API 登录状态徽章 ----------------
function renderLoginBadge(api) {
  const el = $("login-badge");
  if (!el) return;
  if (!api) { el.textContent = "登录 --"; el.className = "badge"; return; }
  if (api.mode !== "live") {
    el.textContent = "🟡 模拟模式(免登录)";
    el.className = "badge badge-pause";
    return;
  }
  if (api.verified === true) {
    el.textContent = `🔑 已登录 ${api.key_masked}` + (api.wallet != null ? ` · ${fmt(api.wallet)}U` : "");
    el.className = "badge badge-ok";
  } else if (api.verified === false) {
    el.textContent = api.configured ? "🔒 登录失败" : "🔒 未登录";
    el.className = "badge badge-halt";
    el.title = "API Key 无效或未配置, 点 ⚙️ 设置 配置测试网 API";
  } else {
    el.textContent = "⏳ 验证中…";
    el.className = "badge";
  }
}

function drawChart(history, dayStart) {
  const canvas = $("equity-chart");
  const dpr = window.devicePixelRatio || 1;
  const w = canvas.clientWidth, h = 220;
  canvas.width = w * dpr; canvas.height = h * dpr;
  const ctx = canvas.getContext("2d");
  ctx.scale(dpr, dpr);
  ctx.clearRect(0, 0, w, h);

  const pts = history || [];
  if (pts.length < 2) {
    ctx.fillStyle = "var(--muted)"; ctx.font = "13px sans-serif";
    ctx.fillText("等待数据…", 12, 20);
    return;
  }
  const values = pts.map((p) => p[1]);
  const min = Math.min(...values, dayStart || 0);
  const max = Math.max(...values, dayStart || 0);
  const pad = 10;
  const range = (max - min) || 1;
  const X = (i) => pad + (i / (pts.length - 1)) * (w - pad * 2);
  const Y = (v) => pad + (1 - (v - min) / range) * (h - pad * 2);

  // 网格线
  ctx.strokeStyle = "rgba(255,255,255,.05)"; ctx.lineWidth = 1;
  for (let i = 0; i < 4; i++) {
    const y = pad + (i / 3) * (h - pad * 2);
    ctx.beginPath(); ctx.moveTo(pad, y); ctx.lineTo(w - pad, y); ctx.stroke();
  }
  // 今日起始线
  if (dayStart) {
    ctx.strokeStyle = "rgba(240,185,11,.5)"; ctx.setLineDash([5, 5]);
    ctx.beginPath(); ctx.moveTo(pad, Y(dayStart)); ctx.lineTo(w - pad, Y(dayStart)); ctx.stroke();
    ctx.setLineDash([]);
    ctx.fillStyle = "rgba(240,185,11,.8)"; ctx.font = "11px sans-serif";
    ctx.fillText("今日起始 " + dayStart.toFixed(2), w - pad - 90, Y(dayStart) - 6);
  }
  // 权益线
  const up = values[values.length - 1] >= values[0];
  ctx.strokeStyle = up ? "var(--green)" : "var(--red)";
  ctx.lineWidth = 2; ctx.lineJoin = "round";
  ctx.beginPath();
  pts.forEach((p, i) => { const x = X(i), y = Y(p[1]); i === 0 ? ctx.moveTo(x, y) : ctx.lineTo(x, y); });
  ctx.stroke();
  // 渐变填充
  const grad = ctx.createLinearGradient(0, pad, 0, h - pad);
  grad.addColorStop(0, up ? "rgba(38,166,154,.18)" : "rgba(239,83,80,.18)");
  grad.addColorStop(1, "rgba(0,0,0,0)");
  ctx.lineTo(X(pts.length - 1), h - pad); ctx.lineTo(X(0), h - pad); ctx.closePath();
  ctx.fillStyle = grad; ctx.fill();

  $("chart-hint").textContent =
    `最新 ${fmt(values[values.length - 1])} | 最高 ${fmt(max)} | 最低 ${fmt(min)}`;
}

document.addEventListener("DOMContentLoaded", () => {
  loadConfig();
  refresh();
  setInterval(refresh, REFRESH_MS);
  startHeartbeatTicker();
  loadLearner();
  setInterval(loadLearner, 30000);
  $("btn-pause").addEventListener("click", async () => {
    const paused = $("btn-pause").textContent === "暂停";
    await fetch("/api/control", {
      method: "POST", headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ action: paused ? "pause" : "resume" }),
    });
  });
  $("btn-reset").addEventListener("click", async () => {
    if (confirm("重置今日: 以当前权益为新起点并解除熔断?")) {
      await fetch("/api/control", {
        method: "POST", headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ action: "reset_day" }),
      });
    }
  });

  // ---------------- 设置面板 ----------------
  let settingsData = { symbols: [], has_positions: [], candidates: [] };
  const setMsg = (text, isErr = false) => {
    const el = $("set-msg");
    el.textContent = text || "";
    el.style.color = isErr ? "var(--red)" : "var(--green)";
  };

  async function loadSettings() {
    try {
      const r = await fetch("/api/settings");
      settingsData = await r.json();
      const api = settingsData.api || {};
      let statusText;
      if (api.mode !== "live") {
        statusText = "🟡 模拟模式 (paper) - 无需登录";
      } else if (api.verified === true) {
        statusText = `🔑 已登录 (${api.key_masked})` + (api.wallet != null ? ` · 钱包 ${fmt(api.wallet)} U` : "");
      } else if (api.verified === false) {
        statusText = api.configured ? "🔒 已配置但登录失败 (Key 无效)" : "🔒 未登录 - 请填写 API Key/Secret";
      } else {
        statusText = "⏳ 验证中…";
      }
      $("set-api-status").textContent = statusText;
      if (api.verified === true) {
        $("set-api-key").value = "";
        $("set-api-secret").value = "";
        $("set-api-key").placeholder = api.key_masked;
      }
      // 模式信息 (全局, 供模式对比表)
      enabledModes = settingsData.enabled_modes || [];
      modeWeights = settingsData.mode_weights || {};
      renderModeSettings();
      renderSymbolList();
      renderRiskSettings();
    } catch (e) {
      setMsg("❌ 加载设置失败: " + e, true);
    }
  }

// 模式勾选 UI - 紧凑卡片网格
function renderModeSettings() {
  const box = $("set-modes");
  if (!box) return;
  const all = settingsData.all_modes || [];
  box.innerHTML = all.map(m => {
    const on = (settingsData.enabled_modes || []).includes(m);
    const w = (settingsData.mode_weights || {})[m];
    const wTxt = w != null ? fmt(w, 2) + "x" : "1.00x";
    return `<label class="mode-card ${on ? "on" : ""}" data-mode="${m}">
      <input type="checkbox" data-mode="${m}" ${on ? "checked" : ""}>
      <span class="mc-name">${MODE_LABELS[m] || m}</span>
      <span class="mc-weight">权重 ${wTxt}</span>
    </label>`;
  }).join("");
  // 用 label 上的 click 触发, 避免重复; checkbox 由 label 包装, change 仍有效
  box.querySelectorAll("input[type=checkbox]").forEach(cb => {
    cb.addEventListener("change", saveModes);
  });
}

  async function saveModes() {
    const modes = [...document.querySelectorAll("#set-modes input:checked")].map(x => x.dataset.mode);
    if (!modes.length) { setMsg("❌ 至少需要一个模式", true); renderModeSettings(); return; }
    setMsg("保存模式中…");
    const res = await fetch("/api/settings/modes", {
      method: "POST", headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ modes }),
    }).then(r => r.json());
    setMsg(res.msg || "保存失败", !res.ok);
    if (res.ok) {
      settingsData.enabled_modes = res.modes;
      enabledModes = res.modes;
      renderModeSettings();
    } else {
      renderModeSettings();
    }
  }

  function renderSymbolList() {
    const box = $("set-symbol-list");
    const positions = new Set(settingsData.has_positions || []);
    if (!settingsData.symbols.length) {
      box.innerHTML = `<span class="hint">暂无交易对</span>`;
      return;
    }
    box.innerHTML = settingsData.symbols.map(sym => {
      const locked = positions.has(sym);
      return `<span class="chip ${locked ? "chip-locked" : ""}" title="${locked ? "有持仓, 需先平仓" : "点击移除"}">
        <b>${sym}</b>${locked ? " 🔒" : `<span class="x" data-sym="${sym}">✕</span>`}
      </span>`;
    }).join("");
    box.querySelectorAll(".x").forEach(el => {
      el.addEventListener("click", async () => {
        const sym = el.dataset.sym;
        const next = settingsData.symbols.filter(s => s !== sym);
        const res = await fetch("/api/settings/symbols", {
          method: "POST", headers: { "Content-Type": "application/json" },
          body: JSON.stringify({ symbols: next }),
        }).then(r => r.json());
        setMsg(res.msg || (res.ok ? "✅ 已移除 " + sym : "操作失败"), !res.ok);
        if (res.ok) { settingsData.symbols = res.symbols; renderSymbolList(); }
      });
    });
  }

  async function doSearch() {
    const q = $("set-symbol-search").value.trim();
    const box = $("set-search-results");
    if (!q) { box.innerHTML = `<span class="hint">输入关键词搜索币种</span>`; return; }
    box.innerHTML = `<span class="hint">搜索中…</span>`;
    try {
      const r = await fetch("/api/symbols/search?q=" + encodeURIComponent(q) + "&limit=30");
      const d = await r.json();
      const results = (d.results || []).filter(x => !settingsData.symbols.includes(x.symbol));
      if (!results.length) {
        box.innerHTML = `<span class="hint">未找到匹配的币种 (或已在列表中)</span>`;
        return;
      }
      box.innerHTML = results.map(x => `<span class="chip chip-add" data-sym="${x.symbol}" title="点击添加">+ ${x.symbol}</span>`).join("");
      box.querySelectorAll(".chip-add").forEach(el => {
        el.addEventListener("click", async () => {
          const sym = el.dataset.sym;
          const next = [...settingsData.symbols, sym];
          const res = await fetch("/api/settings/symbols", {
            method: "POST", headers: { "Content-Type": "application/json" },
            body: JSON.stringify({ symbols: next }),
          }).then(r => r.json());
          setMsg(res.msg || (res.ok ? "✅ 已添加 " + sym : "操作失败"), !res.ok);
          if (res.ok) { settingsData.symbols = res.symbols; renderSymbolList(); doSearch(); }
        });
      });
    } catch (e) {
      box.innerHTML = `<span class="hint">搜索失败: ${e}</span>`;
    }
  }

  $("btn-settings").addEventListener("click", () => {
    $("settings-modal").style.display = "flex";
    setMsg("");
    loadSettings();
  });
  $("btn-settings-close").addEventListener("click", () => {
    $("settings-modal").style.display = "none";
  });
  $("settings-modal").addEventListener("click", (e) => {
    if (e.target.id === "settings-modal") $("settings-modal").style.display = "none";
  });
  $("btn-symbol-search").addEventListener("click", doSearch);
  $("set-symbol-search").addEventListener("keydown", (e) => { if (e.key === "Enter") doSearch(); });
  $("btn-save-api").addEventListener("click", async () => {
    const key = $("set-api-key").value.trim();
    const secret = $("set-api-secret").value.trim();
    if (!key && !secret) { setMsg("请填写 API Key 和 Secret", true); return; }
    setMsg("保存中…");
    const res = await fetch("/api/settings/api", {
      method: "POST", headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ api_key: key, api_secret: secret }),
    }).then(r => r.json());
    setMsg(res.msg || (res.ok ? "✅ 已保存" : "保存失败"), !res.ok);
    loadSettings();
  });

  // ---------------- 风控与敞口 ----------------
  function renderRiskSettings() {
    const r = settingsData.risk || (cfg && cfg.risk) || {};
    const l = settingsData.leverage || (cfg && cfg.leverage) || {};
    const setVal = (id, v) => { const el = $(id); if (el && v != null) el.value = v; };
    setVal("risk-total", r.max_total_position_notional);
    setVal("risk-single", r.max_single_order_notional);
    setVal("risk-margin", r.margin_per_position);
    setVal("risk-positions", r.max_positions);
    setVal("risk-dd", r.daily_loss_stop != null ? Math.round(r.daily_loss_stop * 100) : "");
    setVal("risk-cooldown", r.cooldown_minutes);
    setVal("risk-lev-mode", l.mode);
    setVal("risk-lev-min", l.min);
    setVal("risk-lev-max", l.max);
    setVal("risk-lev-fixed", l.fixed);
  }

  async function saveRisk() {
    const num = (id) => { const v = parseFloat($(id).value); return isNaN(v) ? null : v; };
    const risk = {
      max_total_position_notional: num("risk-total"),
      max_single_order_notional: num("risk-single"),
      margin_per_position: num("risk-margin"),
      max_positions: num("risk-positions"),
      daily_loss_stop: num("risk-dd") != null ? num("risk-dd") / 100 : null,
      cooldown_minutes: num("risk-cooldown"),
    };
    Object.keys(risk).forEach(k => { if (risk[k] == null) delete risk[k]; });
    const leverage = {
      mode: $("risk-lev-mode").value,
      min: num("risk-lev-min"),
      max: num("risk-lev-max"),
      fixed: num("risk-lev-fixed"),
    };
    Object.keys(leverage).forEach(k => { if (leverage[k] == null) delete leverage[k]; });
    if (!Object.keys(risk).length && !Object.keys(leverage).length) {
      setMsg("⚠ 没有可保存的值", true); return;
    }
    setMsg("保存风控中…");
    const res = await fetch("/api/settings/risk", {
      method: "POST", headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ risk, leverage }),
    }).then(r => r.json());
    setMsg(res.msg || (res.ok ? "✅ 已保存" : "保存失败"), !res.ok);
    if (res.ok) {
      settingsData.risk = res.risk;
      settingsData.leverage = res.leverage;
      renderRiskSettings();
    }
  }
  $("btn-save-risk").addEventListener("click", saveRisk);

  window.addEventListener("resize", () => {
    if (lastSnapshot) drawChart(lastSnapshot.equity_history, lastSnapshot.day_start_equity);
  });
});
