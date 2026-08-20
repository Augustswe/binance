/* Binance 测试网量化仪表盘 前端逻辑 */
"use strict";

const REFRESH_MS = 2000;
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
    const sig = s.signals[sym] || {};
    const pos = posMap[sym];
    const chgCls = chg >= 0 ? "pos" : "neg";
    html += `<tr>
      <td><b>${sym}</b></td>
      <td class="mono">${fmtPrice(price)}</td>
      <td class="mono ${chgCls}">${chg == null ? "--" : sign(chg) + fmt(chg, 2) + "%"}</td>
      <td>${regimePill(sig.regime)}</td>
      <td>${scoreBar(sig.combined)}</td>
      <td>${stratName(sig.dominant)}</td>
      <td class="mono">${sig.leverage ? sig.leverage + "x" : "--"}</td>
      <td>${pos ? posPill(pos) : '<span class="pill pill-none">空仓</span>'}</td>
      <td class="mono">${pos ? fmtPrice(pos.entry) : "--"}</td>
      <td class="mono ${cls(pos ? pos.upnl : 0)}">${pos ? sign(pos.upnl) + fmt(pos.upnl) : "--"}</td>
      <td class="mono">${pos ? fmtPrice(pos.tp) + " / " + fmtPrice(pos.sl) : "--"}</td>
    </tr>`;
  }
  tb.innerHTML = html || `<tr><td colspan="11" class="empty">暂无数据</td></tr>`;
}

function posPill(p) {
  return `<span class="pill ${p.side === "LONG" ? "pill-long" : "pill-short"}">${p.side === "LONG" ? "多" : "空"} ${p.qty} @${p.leverage}x</span>`;
}

function stratName(n) {
  const map = { grid: "网格", ma_cross: "均线", rsi: "RSI", bollinger: "布林带", none: "--" };
  return map[n] || n || "--";
}

function renderSignals(s) {
  const tb = document.querySelector("#signals-table tbody");
  const order = cfg ? cfg.symbols : Object.keys(s.signals);
  let html = "";
  for (const sym of order) {
    const g = s.signals[sym];
    if (!g) {
      html += `<tr><td><b>${sym}</b></td><td colspan="11" class="empty">等待首个K线分析…</td></tr>`;
      continue;
    }
    if (g.mode === "donchian") {
      html += `<tr>
        <td><b>${sym}</b></td>
        <td>${regimePill(g.regime)}</td>
        <td class="mono">${g.action === "等待" ? "--" : (g.action === "LONG" ? "▲做多" : "▼做空")}</td>
        <td>${scoreBar(g.combined)}</td>
        <td class="mono">${fmt(g.atr_pct, 3)}%</td>
        <td class="mono">${fmt(g.strength, 2)}</td>
        <td class="mono ${g.leverage > 1 ? "pos" : ""}">${g.leverage}x</td>
        <td class="mono">${fmtTime(g.ts)}</td>
        <td colspan="4"></td>
      </tr>`;
      continue;
    }
    html += `<tr>
      <td><b>${sym}</b></td>
      <td>${regimePill(g.regime)}</td>
      <td class="mono">${fmt(g.scores.grid, 2)}</td>
      <td class="mono">${fmt(g.scores.ma_cross, 2)}</td>
      <td class="mono">${fmt(g.scores.rsi, 2)}</td>
      <td class="mono">${fmt(g.scores.bollinger, 2)}</td>
      <td>${scoreBar(g.combined)}</td>
      <td class="mono">${fmt(g.atr_pct, 3)}%</td>
      <td class="mono">${fmt(g.vol_ratio, 2)}</td>
      <td class="mono">${g.leverage}x</td>
      <td class="mono">${fmtTime(g.ts)}</td>
    </tr>`;
  }
  tb.innerHTML = html || `<tr><td colspan="12" class="empty">暂无数据</td></tr>`;
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
      $("set-api-status").textContent = settingsData.api_configured
        ? `已配置 (${settingsData.api_key_masked})` : "未配置 (paper 模式无需)";
      if (settingsData.api_configured) {
        $("set-api-key").value = "";
        $("set-api-secret").value = "";
        $("set-api-key").placeholder = settingsData.api_key_masked;
      }
      renderSymbolList();
    } catch (e) {
      setMsg("❌ 加载设置失败: " + e, true);
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

  window.addEventListener("resize", () => {
    if (lastSnapshot) drawChart(lastSnapshot.equity_history, lastSnapshot.day_start_equity);
  });
});
