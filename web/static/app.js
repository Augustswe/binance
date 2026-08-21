/* Binance 测试网量化仪表盘 前端逻辑 */
"use strict";

const REFRESH_MS = 2000;
const MODE_LABELS = { auto: "自动并行", donchian: "Donchian", multi: "多策略", grid: "网格", ma_cross: "均线", rsi: "RSI", bollinger: "布林带" };
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

// ---------------- 多账户: ?account= 解析 ----------------
// 带 account 参数 → 单账户策略面板; 否则走入口页 (fetch 不带 account)。
const ACCOUNT = (() => { try { return new URLSearchParams(location.search).get("account"); } catch (e) { return null; } })();
// 给所有面板 API 调用追加 ?account=<name>, 让后端路由到对应子引擎。
// gSettingsAccount: 入口页打开「某账户设置抽屉」时临时覆盖 account, 使设置接口路由到该账户。
let gSettingsAccount = null;
function apiUrl(path) {
  const acct = gSettingsAccount || ACCOUNT;
  if (!acct) return path;
  return path + (path.includes("?") ? "&" : "?") + "account=" + encodeURIComponent(acct);
}

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
    const r = await fetch(apiUrl("/api/config"));
    cfg = await r.json();
    $("mode-badge").textContent = cfg.mode === "live" ? "LIVE 测试网" : "PAPER 模拟";
    $("mode-badge").className = "badge " + (cfg.mode === "live" ? "badge-live" : "badge-paper");
    updateNetBanner();
  } catch (e) { /* ignore */ }
}

// 主网红色横幅: 仅当 network=mainnet 时显示, 并动态带出当前限额与警告
function updateNetBanner() {
  const banner = $("net-banner");
  if (!banner) return;
  const net = (cfg && cfg.network) || lastSnapshot?.network || "testnet";
  if (net !== "mainnet") { banner.style.display = "none"; return; }
  banner.style.display = "block";
  const m = lastSnapshot?.mainnet;
  if (m) {
    banner.innerHTML = `🔴 主网模式 · 真实资金交易 · 总持仓限额 ${fmt(m.cap)}U · ${m.warning}`;
  } else {
    banner.innerHTML = "🔴 主网模式 · 真实资金交易 · 每一笔下单都会动用你的真实资产, 请确认风控参数";
  }
}

// ---------------- 自动学习历史 ----------------
let lastLearnerKey = null;
async function loadLearner() {
  try {
    const r = await fetch(apiUrl("/api/learner"));
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
    const r = await fetch(apiUrl("/api/state"));
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

  // 多账户面板: 标签页标题 = 策略名 (方便区分多个窗口), 顶部显示账户标识
  document.title = (s.strategy_label || "策略") + " · " + (s.account || "default") + " · Binance量化";
  const chip = $("account-chip");
  if (chip) {
    chip.textContent = "📁 " + (s.account || "default") + " · " + (s.strategy_label || "");
    chip.style.display = "";
  }

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

  // 主网横幅: 跟随实时快照 (切换网络后无需刷新页面即变)
  updateNetBanner();

  // 行情持仓表
  renderSymbols(s);
  renderSignals(s);
  renderTrades(s);
  renderLogs(s);

  // 风控文案: 主网模式显示主网限额, 否则显示 risk 配置
  const mn = s.mainnet;
  const riskTxt = (mn && s.network === "mainnet")
    ? `主网限额≤${fmt(mn.cap)}U`
    : `单笔≤${s.risk.max_single}U 总持仓≤${s.risk.max_total}U`;
  $("foot").textContent =
    `模式=${s.mode} | 权益=${fmt(s.equity)} | 起始=${fmt(s.day_start_equity)} | ` +
    `数据更新 ${s.last_tick_ts ? fmtTime(s.last_tick_ts) : "--"} | ` +
    `风控: ${riskTxt} 日亏${(s.risk.daily_loss_stop * 100)}%熔断`;

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

let tpslOpenSym = null;   // 当前展开止盈止损编辑的交易对
let tpslCache = {};       // 编辑中输入值缓存, 跨 5s 轮询保留, 避免丢内容/丢焦点

function renderSymbols(s) {
  const tb = document.querySelector("#symbols-table tbody");
  const posMap = {};
  s.positions.forEach((p) => { posMap[p.symbol] = p; });
  const order = cfg ? cfg.symbols : Object.keys(s.prices);
  if (tpslOpenSym && !tpslCache[tpslOpenSym]) tpslCache[tpslOpenSym] = {};

  // 编辑中: 先缓存输入值 + 记录焦点, 避免 5s 轮询重建时丢内容/丢焦点
  let activeField = null;
  if (tpslOpenSym) {
    const det = document.querySelector(".tpsl-detail[data-sym='" + tpslOpenSym + "']");
    if (det) {
      ["tp-type", "tp-val", "sl-type", "sl-val"].forEach((c) => {
        const el = det.querySelector("." + c);
        if (el) tpslCache[tpslOpenSym][c] = el.value;
        if (el && document.activeElement === el) activeField = c;
      });
    }
  }

  // 用户正在编辑止盈止损输入框时, 跳过本周期整表重建, 避免打断输入/吞掉点击
  if (tpslOpenSym && document.activeElement && document.activeElement.closest &&
      document.activeElement.closest(".tpsl-detail")) {
    return;
  }

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
    // 主导策略 (永远展示「具体策略名」, 不展示 auto/multi 等元模式):
    //   系统本策略开仓的持仓 → 持仓真实策略(pos.mode, 例如 donchian/网格/均线)
    //   交易所同步恢复的持仓(mode="交易所同步") → 交易所不记录原始开仓策略, 故展示:
    //       单一策略账户 → 该账户唯一绑定的具体策略(即其真实开仓策略)
    //       auto/multi  → 该币种当前各模式信号中评分最高/最优先触发的「具体策略」(数据驱动, 非原始开仓策略)
    //   无持仓 → 单一策略账户即它本身; auto/multi → 数据驱动主导策略
    let domLabel = "--";
    let domTitle = "";
    if (pos && pos.mode && pos.mode !== "交易所同步") {
      domLabel = MODE_LABELS[pos.mode] || pos.mode;           // 系统开仓: 真实策略
    } else {
      const rm = s.run_mode;
      const single = rm && rm !== "auto" && rm !== "multi";   // 单一策略账户
      if (single) {
        domLabel = MODE_LABELS[rm] || rm;
        if (pos && pos.mode === "交易所同步") {
          domTitle = "持仓由交易所同步恢复; 本账户仅绑定「" + domLabel + "」单一策略, 即为其开仓策略";
        }
      } else {
        // auto/multi: 数据驱动 — 该币种各模式信号中评分最高/最优先触发的「具体策略」
        let best = null, bestMetric = -1;
        for (const k of Object.keys(s.signals)) {
          if (!k.endsWith(":" + sym)) continue;
          const sg = s.signals[k];
          const sc = Math.abs((sg.score != null ? sg.score : (sg.combined != null ? sg.combined : 0)) || 0);
          const act = (sg.action && sg.action !== "等待") ? 1 : 0;   // 有开仓动作的信号优先
          const metric = act * 1000 + sc;
          if (metric > bestMetric) { bestMetric = metric; best = sg; }
        }
        if (best && best.mode) domLabel = MODE_LABELS[best.mode] || best.mode;
        if (pos && pos.mode === "交易所同步") {
          domTitle = "持仓由交易所同步恢复, 交易所不记录原始开仓策略; 此处显示当前该币种信号最强的具体策略(非原始开仓策略)";
        }
      }
    }
    const showLev = pos ? pos.leverage : sig.leverage;   // 有持仓显示真实杠杆, 无持仓显示计划杠杆
    const chgCls = chg >= 0 ? "pos" : "neg";
    const open = pos && tpslOpenSym === sym;
    html += `<tr data-sym="${sym}">
      <td><b>${sym}</b></td>
      <td class="mono">${fmtPrice(price)}</td>
      <td class="mono ${chgCls}">${chg == null ? "--" : sign(chg) + fmt(chg, 2) + "%"}</td>
      <td>${regimePill(sig.regime)}</td>
      <td>${scoreBar(sig.combined != null ? sig.combined : sig.score)}</td>
      <td title="${domTitle}">${domLabel}</td>
      <td class="mono" title="${pos ? '实际持仓杠杆 (开仓时定, 持仓期间不变)' : '计划开仓杠杆 (按信号强度动态)'}">${showLev ? showLev + "x" : "--"}</td>
      <td>${pos ? posPill(pos) : '<span class="pill pill-none">空仓</span>'}</td>
      <td class="mono">${pos ? fmtPrice(pos.entry) : "--"}</td>
      <td class="mono ${cls(pos ? pos.upnl : 0)}">${pos ? sign(pos.upnl) + fmt(pos.upnl) : "--"}</td>
      <td>${pos ? tpSlCell(pos) : '<span class="hint">--</span>'}</td>
    </tr>`;
    if (open) html += tpslDetailRow(pos);
  }
  tb.innerHTML = html || `<tr><td colspan="11" class="empty">暂无数据</td></tr>`;

  // 恢复焦点到正在编辑的输入框 (轮询重建后不打断输入)
  if (activeField && tpslOpenSym) {
    const det = document.querySelector(".tpsl-detail[data-sym='" + tpslOpenSym + "']");
    const el = det && det.querySelector("." + activeField);
    if (el) { el.focus(); const v = el.value; el.value = ""; el.value = v; }
  }
}

function posPill(p) {
  return `<span class="pill ${p.side === "LONG" ? "pill-long" : "pill-short"}">${p.side === "LONG" ? "多" : "空"} ${p.qty} @${p.leverage}x</span>`;
}

// TP/SL 列: 显眼可点击的「止盈止损」按钮 (展开就地编辑器)
// 点击 = 展开/收起该币种的 TP/SL 编辑器 (复用 .tpsl-btn 类, 走 tbody 事件委托)
function tpSlCell(p) {
  const open = tpslOpenSym === p.symbol;
  const active = p.manual_tp_active || p.manual_sl_active;
  const hasTP = p.tp > 0;
  const hasSL = p.sl > 0;
  // 移动止损模式: donchian 与 交易所同步 都随新高/新低锁浮盈, 不挂固定止盈
  const trailing = p.mode === "donchian" || p.mode === "交易所同步";
  // 始终显示当前止盈/止损状态 (自动策略值, 或手动覆盖后的值), 不随有无值而隐藏
  const autoTP = hasTP ? fmtPrice(p.tp) : "∞";
  let autoSL = "--";
  if (hasSL) {
    if (p.manual_sl_active) autoSL = `止损(手动) ${fmtPrice(p.sl)}`;
    else if (trailing) autoSL = `移动止损 ${fmtPrice(p.sl)}`;
    else autoSL = `止损 ${fmtPrice(p.sl)}`;
  }
  const cur = `<span class="tpsl-cur">${autoTP} / ${autoSL}${active ? " (手动)" : ""}</span>`;
  return `<div class="tpsl-cell">
    <div class="tpsl-actions">
      <button class="tpsl-btn ${open ? "open" : ""}" data-sym="${p.symbol}" title="${open ? "收起止盈止损编辑器" : "点击设置止盈止损 (触发即市价成交)"}">
        ${open ? "收起 ▲" : "止盈止损"}
      </button>
      <button class="tpsl-btn close-now" data-sym="${p.symbol}" title="按当前价立即市价平仓 (不可撤销)">立即平仓</button>
    </div>
    ${cur}
  </div>`;
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

// 持仓行内联止盈止损: 点击「止盈止损」按钮就地展开编辑, 市价单 (触发即市价成交)
function tpslDetailRow(p) {
  const sym = p.symbol;
  const mtp = p.manual_tp || null;
  const msl = p.manual_sl || null;
  const c = tpslCache[sym] || {};
  const tpType = c["tp-type"] || (mtp ? mtp.type : "price");
  const tpVal = (c["tp-val"] !== undefined && c["tp-val"] !== "") ? c["tp-val"] : (mtp ? mtp.value : "");
  const slType = c["sl-type"] || (msl ? msl.type : "price");
  const slVal = (c["sl-val"] !== undefined && c["sl-val"] !== "") ? c["sl-val"] : (msl ? msl.value : "");
  const active = p.manual_tp_active || p.manual_sl_active;
  return `<tr class="tpsl-detail" data-sym="${sym}"><td colspan="11">
    <div class="tpsl-edit">
      <span class="lbl">止盈 TP</span>
      <select class="tp-type input mini">${opt(tpType, "price", "价格")}${opt(tpType, "pct", "% 百分比")}</select>
      <input class="tp-val input mini" type="number" step="any" placeholder="价格或%" value="${tpVal}">
      <span class="lbl">止损 SL</span>
      <select class="sl-type input mini">${opt(slType, "price", "价格")}${opt(slType, "pct", "% 百分比")}</select>
      <input class="sl-val input mini" type="number" step="any" placeholder="价格或%" value="${slVal}">
      <button class="btn btn-ok btn-sm tpsl-apply">应用</button>
      <button class="btn btn-ghost btn-sm tpsl-clear">清除</button>
      <span class="hint">触发即市价成交 · 设了覆盖自动 ATR · 清空回退自动 · 价格或百分比均可</span>
      ${active ? '<span class="hint ok">● 已生效</span>' : ""}
    </div></td></tr>`;
}

function toggleTpsl(sym) {
  tpslOpenSym = (tpslOpenSym === sym) ? null : sym;
  tpslCache[sym] = {};   // 打开时以服务器值为准
  refresh();
}

async function onTpslApply(e) {
  const tr = e.target.closest("tr");
  const sym = tr.getAttribute("data-sym");
  const tpType = tr.querySelector(".tp-type").value;
  const tpVal = tr.querySelector(".tp-val").value;
  const slType = tr.querySelector(".sl-type").value;
  const slVal = tr.querySelector(".sl-val").value;
  const tp = tpVal !== "" ? {type: tpType, value: parseFloat(tpVal)} : null;
  const sl = slVal !== "" ? {type: slType, value: parseFloat(slVal)} : null;
  if (!tp && !sl) { alert("请至少填写一个 止盈(TP) 或 止损(SL)"); return; }
  if (tp && isNaN(tp.value)) { alert("TP 数值无效"); return; }
  if (sl && isNaN(sl.value)) { alert("SL 数值无效"); return; }
  tpslCache[sym] = {};
  await postManualTPSL(sym, tp, sl);
}

async function onTpslClear(e) {
  const tr = e.target.closest("tr");
  const sym = tr.getAttribute("data-sym");
  tpslCache[sym] = {};
  await postManualTPSL(sym, null, null);
}

function opt(sel, val, label) {
  return `<option value="${val}" ${sel === val ? "selected" : ""}>${label}</option>`;
}

async function onCloseNow(sym) {
  if (!confirm(`确认按当前价市价平仓 ${sym}？\n此操作不可撤销 (测试网)。`)) return;
  try {
    const res = await fetch(apiUrl("/api/close_position"), {
      method: "POST",
      headers: {"Content-Type": "application/json"},
      body: JSON.stringify({symbol: sym}),
    });
    const j = await res.json();
    if (!j.ok) { alert("平仓失败: " + (j.error || "未知错误")); }
    else { refresh(); }
  } catch (e) {
    alert("请求失败: " + e);
  }
}

async function postManualTPSL(sym, tp, sl) {
  try {
    const res = await fetch(apiUrl("/api/manual_tp_sl"), {
      method: "POST",
      headers: {"Content-Type": "application/json"},
      body: JSON.stringify({symbol: sym, tp, sl}),
    });
    const j = await res.json();
    if (!j.ok) { alert("设置失败: " + (j.error || "未知错误")); }
    else { refresh(); }
  } catch (e) {
    alert("请求失败: " + e);
  }
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
    const pinned = runMode !== "auto" && runMode === m;
    const active = (!sigActiveTab || sigActiveTab === m) && i === 0;
    return `<div class="sig-tab ${active ? "active" : ""} ${pinned ? "pinned" : ""}" data-tab="${m}" title="${pinned ? "当前运行模式: 仅此策略开仓" : ""}">
      ${MODE_LABELS[m] || m}${pinned ? '<span class="tab-pin">●运行中</span>' : ""}<span class="tab-count" data-count="${m}">0</span>
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
let runMode = "auto";  // 运行模式: auto = 全部并行, 否则只让指定策略开仓
let modeWeights = {};
// 设置弹窗共享状态: 入口页与面板共用同一份设置 DOM, 故提升到模块级
let settingsData = { symbols: [], has_positions: [], candidates: [] };
function setMsg(text, isErr = false) {
  const el = $("set-msg");
  if (el) { el.textContent = text || ""; el.style.color = isErr ? "var(--red)" : "var(--green)"; }
}

function renderConsoleModes(stats, weights) {
  const tb = document.querySelector("#modes-table tbody");
  if (!tb) return;
  const st = stats || {};
  const w = weights || {};
  // 仅展示有实盘统计(交易数>0)的模式; 按已实现盈亏降序, 让"更可靠/更赚"的策略排在前面
  const shown = Object.keys(st).filter(m => MODE_LABELS[m] && (st[m].total_trades || 0) > 0);
  shown.sort((a, b) => ((st[b].realized_pnl || 0) - (st[a].realized_pnl || 0)));
  if (!shown.length) {
    tb.innerHTML = `<tr><td colspan="6" class="empty">暂无模式数据（系统运行后自动统计）</td></tr>`;
    return;
  }
  let html = "";
  for (const m of shown) {
    const s = st[m] || { total_trades: 0, win_rate: 0, realized_pnl: 0, fees_paid: 0 };
    const wt = w[m] != null ? w[m] : 1.0;
    html += `<tr>
      <td><b>${MODE_LABELS[m]}</b></td>
      <td class="mono">${s.total_trades}</td>
      <td class="mono">${fmt(s.win_rate, 1)}%</td>
      <td class="mono ${cls(s.realized_pnl)}">${sign(s.realized_pnl)}${fmt(s.realized_pnl)} U</td>
      <td class="mono">${fmt(s.fees_paid)} U</td>
      <td class="mono ${wt > 1.05 ? "pos" : wt < 0.95 ? "neg" : ""}">${fmt(wt, 2)}x</td>
    </tr>`;
  }
  tb.innerHTML = html;
}

// ---------------- 系统日志终端 (浮动窗口) ----------------
const LOG_TYPE_LABEL = {
  system: "系统", trade: "交易", risk: "风控", tuning: "调参", info: "信息", error: "异常",
};

// 未读追踪: 按"最新一条日志时间戳"增量计数, 避免被 200/50 上限截断误算
let _logLastTopTs = null;
let _logUnread = 0;

function _logTerm() { return document.getElementById("log-term"); }
function _logTermOpen() {
  const t = _logTerm();
  return !!(t && t.classList.contains("open"));
}

// 展开 / 收起 / 切换
function openLogTerm() {
  const t = _logTerm();
  if (!t) return;
  t.classList.add("open");
  t.setAttribute("aria-hidden", "false");
  const btn = document.getElementById("log-term-btn");
  if (btn) btn.setAttribute("aria-expanded", "true");
  _logUnread = 0;            // 打开即已读
  _updateLogBadge();
  const list = document.getElementById("log-list");
  if (list) list.scrollTop = list.scrollHeight;   // 打开即贴底看最新
  try { localStorage.setItem("log_term_open", "1"); } catch (e) {}
}
function closeLogTerm() {
  const t = _logTerm();
  if (!t) return;
  t.classList.remove("open");
  t.setAttribute("aria-hidden", "true");
  const btn = document.getElementById("log-term-btn");
  if (btn) btn.setAttribute("aria-expanded", "false");
  try { localStorage.setItem("log_term_open", "0"); } catch (e) {}
}
function toggleLogTerm() {
  _logTermOpen() ? closeLogTerm() : openLogTerm();
}

function _updateLogBadge() {
  const b = document.getElementById("log-term-badge");
  if (!b) return;
  if (_logUnread > 0) {
    b.textContent = _logUnread > 99 ? "99+" : String(_logUnread);
    b.hidden = false;
  } else {
    b.hidden = true;
  }
}

function _updateLogStatus(n) {
  const el = document.getElementById("log-term-status");
  if (!el) return;
  if (n > 0) {
    el.textContent = `已记录 ${n} 条 · 实时`;
    el.className = "log-term-status-on";
  } else {
    el.textContent = "暂无日志";
    el.className = "";
  }
}

function renderLogs(s) {
  const list = document.getElementById("log-list");
  if (!list) return;
  const eventsRaw = s.events || [];   // API 返回: 最新在前 (state.py 已反转)

  // ---- 未读角标: 仅窗口收起时累计, 打开即清零 ----
  if (eventsRaw.length) {
    const topTs = eventsRaw[0].ts;
    if (_logLastTopTs !== null && topTs > _logLastTopTs) {
      _logUnread += eventsRaw.filter((e) => e.ts > _logLastTopTs).length;
    }
    _logLastTopTs = topTs;
  }
  if (_logTermOpen()) _logUnread = 0;
  _updateLogBadge();

  if (!eventsRaw.length) {
    list.innerHTML = `<div class="empty">暂无日志（系统启动后自动记录每一步操作）</div>`;
    _updateLogStatus(0);
    return;
  }

  // ---- 终端呈现: 最新在底部, 智能贴底 (不打扰向上翻看) ----
  const asEl = document.getElementById("log-term-autoscroll");
  const autoscrollOn = !asEl || asEl.checked;
  const nearBottom = list.scrollHeight - list.scrollTop - list.clientHeight < 80;
  const termOpen = _logTermOpen();
  const stick = !termOpen || (autoscrollOn && nearBottom);

  const events = eventsRaw.slice().reverse();  // 最新落底
  let html = "";
  for (const e of events) {
    const clsName = "log-item " + e.type + (e.type === "trade" && e.msg.includes("🔻") ? " neg" : "");
    html += `<div class="${clsName}">`
      + `<span class="lt">${fmtTime(e.ts)}</span>`
      + `<span class="ltype">[${LOG_TYPE_LABEL[e.type] || e.type}]</span>`
      + `<span class="lmsg">${escapeHtml(e.msg)}</span>`
      + `</div>`;
  }
  list.innerHTML = html;
  if (stick) list.scrollTop = list.scrollHeight;
  _updateLogStatus(eventsRaw.length);
}

async function clearLogTerm() {
  if (!confirm("确认清屏？此操作会清空全部系统日志且不可恢复。")) return;
  const list = document.getElementById("log-list");
  if (list) list.innerHTML = `<div class="empty">已清屏, 等待新日志…</div>`;
  _updateLogStatus(0);
  try {
    await fetch(apiUrl("/api/logs/clear"), { method: "POST" });
  } catch (e) { /* 离线不阻塞 UI */ }
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

function initPanel(account) {
  loadConfig();
  refresh();
  setInterval(refresh, REFRESH_MS);

  // 事件委托: 止盈止损按钮/应用/清除
  // 关键: 后台每 5s 会整表 rebuild (innerHTML), 若在按钮上逐个绑 onclick 会被销毁, 导致点击落空.
  // 改在稳定的 <tbody> 上委托, 无论表格何时重建, 点击都能命中.
  const symTb = document.querySelector("#symbols-table tbody");
  if (symTb) {
    symTb.addEventListener("click", (e) => {
      // 注意: .close-now 同时带有 .tpsl-btn, 必须在 .tpsl-btn 之前判定, 否则会被误判为展开编辑器
      const closeNow = e.target.closest(".close-now");
      if (closeNow) { onCloseNow(closeNow.dataset.sym); return; }
      const btn = e.target.closest(".tpsl-btn");
      if (btn) { toggleTpsl(btn.dataset.sym); return; }
      const apply = e.target.closest(".tpsl-apply");
      if (apply) { onTpslApply(e); return; }
      const clear = e.target.closest(".tpsl-clear");
      if (clear) { onTpslClear(e); return; }
    });
  }
  startHeartbeatTicker();
  loadLearner();
  setInterval(loadLearner, 30000);

  // ---------------- 系统日志终端 (浮动窗口) ----------------
  const logBtn = document.getElementById("log-term-btn");
  if (logBtn) logBtn.addEventListener("click", toggleLogTerm);
  const logClose = document.getElementById("log-term-close");
  if (logClose) logClose.addEventListener("click", closeLogTerm);
  const logClear = document.getElementById("log-term-clear");
  if (logClear) logClear.addEventListener("click", clearLogTerm);
  const logAs = document.getElementById("log-term-autoscroll");
  if (logAs) logAs.addEventListener("change", () => {
    // 重新贴底一次, 立即反馈勾选
    const list = document.getElementById("log-list");
    if (logAs.checked && list) list.scrollTop = list.scrollHeight;
  });
  // 记忆上次的展开/收起状态 (刷新页面不丢失)
  try {
    if (localStorage.getItem("log_term_open") === "1") {
      openLogTerm();
      if (typeof lastSnapshot !== "undefined" && lastSnapshot) renderLogs(lastSnapshot);
    }
  } catch (e) { /* localStorage 不可用时忽略 */ }
  $("btn-pause").addEventListener("click", async () => {
    const paused = $("btn-pause").textContent === "暂停";
    await fetch(apiUrl("/api/control"), {
      method: "POST", headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ action: paused ? "pause" : "resume" }),
    });
  });

}

// ---------------- 入口主页 (多账户落地页) ----------------
const STRATEGY_MAP = {};
function initEntry() {
  document.title = "Binance 测试网 · 多账户量化控制台";
  loadEntry();
  setInterval(loadEntry, 5000);
  loadEntryAutostart();
  const form = $("bind-form");
  if (form) form.addEventListener("submit", onBindSubmit);
  const rf = $("entry-refresh");
  if (rf) rf.addEventListener("click", loadEntry);
  const ab = $("entry-autostart-btn");
  if (ab) ab.addEventListener("click", toggleEntryAutostart);
  $("btn-pause") && ($("btn-pause").style.display = "none");
}

async function loadEntry() {
  try {
    const [a, ov] = await Promise.all([
      fetch("/api/accounts").then(r => r.json()),
      fetch("/api/accounts/overview").then(r => r.json()),
    ]);
    Object.assign(STRATEGY_MAP, a.strategies || {});
    renderEntryOverview(ov);
    renderEntryAccounts(ov, a);
    renderBindStrategies(a.strategies);
    const mb = $("entry-multi-badge");
    if (mb) mb.textContent = `${(ov.totals && ov.totals.accounts) || 0} 账户 · ${a.multi ? "多账户" : "单账户"}`;
  } catch (e) {
    const list = $("entry-accounts");
    if (list) list.innerHTML = `<div class="empty">加载失败: ${escapeHtml(String(e))}</div>`;
  }
}

function renderEntryOverview(ov) {
  const t = (ov.totals) || {};
  const cards = [
    { label: "总权益", val: fmt(t.equity) + " U" },
    { label: "今日盈亏", val: `${sign(t.day_pnl || 0)}${fmt(t.day_pnl || 0)} U`, cls: cls(t.day_pnl) },
    { label: "累计已实现盈亏", val: `${sign(t.realized_pnl || 0)}${fmt(t.realized_pnl || 0)} U`, cls: cls(t.realized_pnl) },
    { label: "持仓数", val: (t.open_positions || 0) + " 个" },
    { label: "账户数", val: (t.accounts || 0) + " 个" },
  ];
  const box = $("ov-cards");
  if (box) box.innerHTML = cards.map(c =>
    `<div class="card"><div class="label">${c.label}</div><div class="value mono ${c.cls || ""}">${c.val}</div></div>`
  ).join("");

  // 按账户盈亏明细表
  renderEntryAccountsTable(ov.accounts || []);
  // 策略模式对比表 (跨账户聚合)
  renderConsoleModes(ov.mode_stats, ov.mode_weights);
}

function renderEntryAccountsTable(accs) {
  const tb = document.querySelector("#ov-accounts-table tbody");
  if (!tb) return;
  if (!accs || !accs.length) {
    tb.innerHTML = `<tr><td colspan="10" class="empty">暂无账户</td></tr>`;
    return;
  }
  let html = "";
  for (const x of accs) {
    const [stTxt, stCls] = accStatus(x);
    html += `<tr>
      <td><b>${escapeHtml(x.name)}</b></td>
      <td>${escapeHtml(x.strategy_label)}</td>
      <td>${escapeHtml(x.network)}</td>
      <td><span class="badge ${stCls}">${stTxt}</span></td>
      <td class="mono">${fmt(x.equity)} U</td>
      <td class="mono ${cls(x.day_pnl)}">${sign(x.day_pnl)}${fmt(x.day_pnl)} U</td>
      <td class="mono ${cls(x.realized_pnl)}">${sign(x.realized_pnl)}${fmt(x.realized_pnl)} U</td>
      <td class="mono">${fmt(x.win_rate, 1)}%</td>
      <td class="mono">${x.total_trades}</td>
      <td class="mono">${x.open_positions}</td>
    </tr>`;
  }
  tb.innerHTML = html;
}

function accStatus(x) {
  if (x.halted) return ["已熔断", "badge-halt"];
  if (x.paused) return ["已暂停", "badge-pause"];
  if (x.running) return ["运行中", "badge-ok"];
  return ["离线", "badge"];
}

function renderEntryAccounts(ov, a) {
  const acc = (ov.accounts) || [];
  const list = $("entry-accounts");
  if (!list) return;
  if (!acc.length) { list.innerHTML = `<div class="empty">尚未绑定任何账户</div>`; return; }
  const defaultName = a.default || "default";
  list.innerHTML = acc.map(x => {
    const [stTxt, stCls] = accStatus(x);
    const isDefault = x.name === defaultName;
    return `<div class="acc-card" data-name="${escapeHtml(x.name)}">
      <div class="acc-head">
        <span class="acc-name">📁 ${escapeHtml(x.name)}</span>
        <span class="strategy-badge">${escapeHtml(x.strategy_label || x.strategy)}</span>
        <span class="net-pill ${x.network === "mainnet" ? "net-main" : "net-test"}">${x.network === "mainnet" ? "主网" : "测试网"}</span>
        <span class="badge ${stCls}">${stTxt}</span>
      </div>
      <div class="acc-metrics">
        <div><span class="m-label">权益</span><span class="mono">${fmt(x.equity)} U</span></div>
        <div><span class="m-label">今日</span><span class="mono ${cls(x.day_pnl)}">${sign(x.day_pnl)}${fmt(x.day_pnl)}</span></div>
        <div><span class="m-label">累计已实现</span><span class="mono ${cls(x.realized_pnl)}">${sign(x.realized_pnl)}${fmt(x.realized_pnl)}</span></div>
        <div><span class="m-label">胜率</span><span class="mono">${fmt(x.win_rate, 1)}%</span></div>
        <div><span class="m-label">交易数</span><span class="mono">${x.total_trades}</span></div>
        <div><span class="m-label">持仓</span><span class="mono">${x.open_positions}</span></div>
      </div>
      <div class="acc-actions">
        <button class="btn btn-ok btn-sm acc-open" data-name="${escapeHtml(x.name)}">打开面板</button>
        <button class="btn btn-ghost btn-sm acc-toggle" data-name="${escapeHtml(x.name)}" data-enabled="${x.enabled ? "0" : "1"}" title="启用 = 启动该账户的策略循环与自动交易; 停用 = 暂停自动交易 (保留现有持仓, 不再开新仓)">${x.enabled ? "停用" : "启用"}</button>
        <button class="btn btn-ghost btn-sm acc-unbind" data-name="${escapeHtml(x.name)}" ${isDefault ? "disabled title='默认账户不可解绑'" : "title='解绑 = 停止该引擎并从账户列表移除 (本地 state 文件保留备查); 操作不可恢复, 默认账户不可解绑'"}>解绑</button>
        <button class="btn btn-ghost btn-sm acc-settings" data-name="${escapeHtml(x.name)}" title="打开该账户的设置抽屉: API / 策略模式 / 交易对 / 风控 / 重置账户">⚙️ 设置</button>
      </div>
      <div class="acc-explain">启用/停用: 开关该账户自动交易 · 解绑: 移除账户(默认账户不可解绑) · 设置: 账户参数与重置</div>
    </div>`;
  }).join("");

  list.querySelectorAll(".acc-open").forEach(b => b.addEventListener("click", () => {
    window.open("/?account=" + encodeURIComponent(b.dataset.name), "_blank");
  }));
  list.querySelectorAll(".acc-settings").forEach(b => b.addEventListener("click", () => openEntrySettings(b.dataset.name)));
  list.querySelectorAll(".acc-toggle").forEach(b => b.addEventListener("click", () => onToggle(b.dataset.name, b.dataset.enabled === "1")));
  list.querySelectorAll(".acc-unbind").forEach(b => b.addEventListener("click", () => onUnbind(b.dataset.name)));
}

function renderBindStrategies(strategies) {
  const sel = $("bf-run-mode");
  if (!sel) return;
  sel.innerHTML = Object.keys(strategies || {}).map(k =>
    `<option value="${k}">${escapeHtml(strategies[k])}</option>`
  ).join("");
}

function bindMsg(text, isErr) {
  const el = $("bind-msg");
  if (!el) return;
  el.textContent = text || "";
  el.style.color = isErr ? "var(--red)" : "var(--green)";
}

async function onBindSubmit(e) {
  e.preventDefault();
  const name = ($("bf-name").value || "").trim();
  const network = $("bf-network").value;
  const mode = $("bf-mode").value;
  const run_mode = $("bf-run-mode").value;
  const key = ($("bf-key").value || "").trim();
  const secret = ($("bf-secret").value || "").trim();
  const symbolsRaw = ($("bf-symbols").value || "").trim();
  const symbols = symbolsRaw ? symbolsRaw.split(",").map(s => s.trim().toUpperCase()).filter(Boolean) : null;
  if (!name) { bindMsg("❌ 请填写账户名", true); return; }
  if (name.toLowerCase() === "default") { bindMsg("❌ 账户名不可为 default (默认账户已保留)", true); return; }
  if (mode === "live" && (!key || !secret)) { bindMsg("❌ LIVE 模式必须填写 API Key / Secret", true); return; }
  bindMsg("绑定中…");
  try {
    const res = await fetch("/api/accounts/bind", {
      method: "POST", headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ name, network, mode, run_mode, api_key: key, api_secret: secret, symbols }),
    }).then(r => r.json());
    if (!res.ok) { bindMsg(res.msg || "绑定失败", true); return; }
    bindMsg(res.msg || "✅ 已绑定", false);
    $("bf-name").value = ""; $("bf-key").value = ""; $("bf-secret").value = ""; $("bf-symbols").value = "";
    await loadEntry();
    // 绑定后自动打开该账户面板, 用户所见即所得
    window.open("/?account=" + encodeURIComponent(name), "_blank");
  } catch (err) {
    bindMsg("❌ 请求失败: " + err, true);
  }
}

async function onToggle(name, enabled) {
  try {
    const res = await fetch("/api/accounts/toggle", {
      method: "POST", headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ name, enabled: enabled === "1" || enabled === true }),
    }).then(r => r.json());
    if (!res.ok) alert(res.msg || "操作失败");
    loadEntry();
  } catch (e) { alert("操作失败: " + e); }
}

async function onReset(name) {
  if (!confirm(`重置账户「${name}」?\n\n将清空该账户的全部持仓 / 成交 / 统计数据, 并从交易所重新同步。\n此操作不可恢复, 请确认。`)) return;
  try {
    const res = await fetch("/api/accounts/reset", {
      method: "POST", headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ name }),
    }).then(r => r.json());
    alert(res.msg || (res.ok ? "✅ 已重置" : "重置失败"));
    loadEntry();
  } catch (e) { alert("重置失败: " + e); }
}

async function onUnbind(name) {
  if (!confirm(`解绑账户「${name}」?\n\n将停止其引擎并从绑定列表移除 (state 文件保留备查)。`)) return;
  try {
    const res = await fetch("/api/accounts/unbind", {
      method: "POST", headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ name }),
    }).then(r => r.json());
    alert(res.msg || (res.ok ? "✅ 已解绑" : "解绑失败"));
    loadEntry();
  } catch (e) { alert("解绑失败: " + e); }
}

// ==================== 设置弹窗 (入口页 / 面板共用同一份 DOM) ====================
let _loadSettings = null;   // 由 initSettings 注入, 供入口页「账户设置」抽屉调用

function openEntrySettings(name) {
  gSettingsAccount = name;
  const modal = $("settings-modal");
  if (!modal) return;
  const head = modal.querySelector(".modal-head span");
  if (head) head.textContent = `⚙️ ${name} · 账户设置`;
  // 账户级操作区(重置账户)仅在入口页「按账户」抽屉中显示
  const rs = modal.querySelector("#acc-reset-sec");
  if (rs) rs.style.display = "block";
  modal.style.display = "flex";
  document.body.classList.add("modal-open");
  setMsg("");
  if (_loadSettings) _loadSettings();
}

function closeEntrySettings() {
  gSettingsAccount = null;
  const modal = $("settings-modal");
  if (modal) {
    modal.style.display = "none";
    const rs = modal.querySelector("#acc-reset-sec");
    if (rs) rs.style.display = "none";
  }
  document.body.classList.remove("modal-open");
  const head = modal && modal.querySelector(".modal-head span");
  if (head) head.textContent = "⚙️ 系统设置";
}

// ---------------- 全局开机自启 (入口页单次, 不针对单账户) ----------------
let entryAutostartState = null;
async function loadEntryAutostart() {
  try {
    const d = await (await fetch("/api/settings")).json();
    entryAutostartState = d.autostart || null;
    renderEntryAutostart(entryAutostartState);
  } catch (e) { /* ignore */ }
}
function renderEntryAutostart(a) {
  const statusEl = $("entry-autostart-status");
  const btn = $("entry-autostart-btn");
  if (!statusEl || !btn) return;
  if (!a || !a.supported) {
    statusEl.textContent = "❌ 当前系统不支持 (仅 macOS 支持 launchd)";
    statusEl.style.color = "var(--muted)";
    btn.textContent = "不支持";
    btn.disabled = true;
    return;
  }
  btn.disabled = false;
  if (a.enabled) {
    statusEl.textContent = "✅ 已开启 (下次登录/开机自动启动)";
    statusEl.style.color = "var(--green)";
    btn.textContent = "关闭开机自启";
    btn.className = "btn btn-warn on";
  } else {
    statusEl.textContent = a.installed ? "⭘ 已安装但未加载" : "⭘ 未启用";
    statusEl.style.color = "var(--muted)";
    btn.textContent = "启用开机自启";
    btn.className = "btn btn-ok";
  }
}
async function toggleEntryAutostart() {
  if (!entryAutostartState || !entryAutostartState.supported) return;
  const enabled = !entryAutostartState.enabled;
  const btn = $("entry-autostart-btn");
  if (btn) btn.disabled = true;
  try {
    const res = await fetch("/api/settings/autostart", {
      method: "POST", headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ enabled }),
    }).then(r => r.json());
    if (!res.ok) alert(res.msg || "操作失败");
    await loadEntryAutostart();
  } catch (e) { alert("操作失败: " + e); }
  finally { const b = $("entry-autostart-btn"); if (b) b.disabled = false; }
}

function initSettings() {
  // 静态元素监听 (动态内容由各 render 函数内部绑定)
  $("btn-settings-close").addEventListener("click", closeEntrySettings);
  $("settings-modal").addEventListener("click", (e) => {
    if (e.target.id === "settings-modal") closeEntrySettings();
  });
  // 账户设置抽屉内的「重置账户」(仅入口页按账户上下文; gSettingsAccount 已在打开时设置)
  $("btn-acc-reset").addEventListener("click", async () => {
    if (!gSettingsAccount) return;
    await onReset(gSettingsAccount);
    closeEntrySettings();
  });
  $("btn-symbol-search").addEventListener("click", doSearch);
  $("set-symbol-search").addEventListener("keydown", (e) => { if (e.key === "Enter") doSearch(); });
  $("btn-save-api").addEventListener("click", async () => {
    const key = $("set-api-key").value.trim();
    const secret = $("set-api-secret").value.trim();
    if (!key && !secret) { setMsg("请填写 API Key 和 Secret", true); return; }
    setMsg("保存中…");
    const res = await fetch(apiUrl("/api/settings/api"), {
      method: "POST", headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ api_key: key, api_secret: secret }),
    }).then(r => r.json());
    setMsg(res.msg || (res.ok ? "✅ 已保存" : "保存失败"), !res.ok);
    loadSettings();
  });
  $("btn-save-risk").addEventListener("click", saveRisk);
  document.querySelectorAll(".net-opt").forEach(b => {
    b.addEventListener("click", () => {
      const target = b.dataset.net;
      const cur = settingsData.network || (cfg && cfg.network) || "testnet";
      if (target === cur) return;
      switchNetwork(target);
    });
  });
  $("btn-save-mainnet").addEventListener("click", async () => {
    const key = $("set-mainnet-key").value.trim();
    const secret = $("set-mainnet-secret").value.trim();
    if (!key && !secret) { setMsg("请填写主网 API Key 和 Secret", true); return; }
    setMsg("保存主网 API 中…");
    const res = await fetch(apiUrl("/api/settings/api"), {
      method: "POST", headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ mainnet_key: key, mainnet_secret: secret }),
    }).then(r => r.json());
    setMsg(res.msg || (res.ok ? "✅ 主网 API 已保存" : "保存失败"), !res.ok);
    if (res.ok) {
      settingsData.mainnet_configured = true;
      $("set-mainnet-key").value = "";
      $("set-mainnet-secret").value = "";
    }
  });
  $("btn-save-mainnet-quota").addEventListener("click", async () => {
    const limit = parseFloat($("set-mainnet-custom").value);
    if (!limit || limit <= 0) { setMsg("请填写有效的自定义额度 (USDT)", true); return; }
    setMsg("保存主网自定义额度中…");
    const res = await fetch(apiUrl("/api/settings/mainnet-quota"), {
      method: "POST", headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ custom_limit: limit }),
    }).then(r => r.json());
    setMsg(res.msg || (res.ok ? "✅ 已保存" : "保存失败"), !res.ok);
    if (res.ok) {
      if (settingsData.mainnet) settingsData.mainnet.custom_limit = limit;
      renderNetworkSettings();
    }
  });
  $("btn-save-pnl-init").addEventListener("click", savePnlInit);
  const rmSel = document.getElementById("set-run-mode");
  if (rmSel) rmSel.addEventListener("change", saveRunMode);
  window.addEventListener("resize", () => {
    if (lastSnapshot) drawChart(lastSnapshot.equity_history, lastSnapshot.day_start_equity);
  });

  // 暴露给入口页「账户设置」抽屉
  _loadSettings = loadSettings;

  // ---------- 以下函数原内联于面板, 现模块级共用 ----------
  async function loadSettings() {
    try {
      const r = await fetch(apiUrl("/api/settings"));
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
      enabledModes = settingsData.enabled_modes || [];
      modeWeights = settingsData.mode_weights || {};
      renderModeSettings();
      renderSymbolList();
      renderRiskSettings();
      renderNetworkSettings();
      renderPnl();
    } catch (e) {
      setMsg("❌ 加载设置失败: " + e, true);
    }
  }

  function renderModeSettings() {
    const box = $("set-modes");
    if (!box) return;
    if (settingsData.run_mode) runMode = settingsData.run_mode;
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
    box.querySelectorAll("input[type=checkbox]").forEach(cb => {
      cb.addEventListener("change", saveModes);
    });
    const sel = $("set-run-mode");
    if (sel) {
      sel.value = runMode;
      const tag = $("set-run-mode-tag");
      if (tag) tag.textContent = runMode === "auto" ? "自动并行" : (MODE_LABELS[runMode] || runMode);
    }
    const lockedNote = $("run-mode-locked");
    if (lockedNote) {
      if (settingsData.strategy_locked) {
        if (sel) sel.style.display = "none";
        const lbl = settingsData.strategy_label || (MODE_LABELS[runMode] || runMode);
        lockedNote.innerHTML = `🔒 该账户策略已固定为 <b>${escapeHtml(lbl)}</b>，不可在面板热改。如需更换策略，请到入口主页「重置账户」后重新绑定。`;
        lockedNote.style.display = "block";
      } else {
        if (sel) sel.style.display = "";
        lockedNote.style.display = "none";
      }
    }
  }

  async function saveRunMode() {
    const sel = $("set-run-mode");
    if (!sel) return;
    const mode = sel.value;
    setMsg("保存运行模式中…");
    const res = await fetch(apiUrl("/api/settings/run_mode"), {
      method: "POST", headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ mode }),
    }).then(r => r.json());
    setMsg(res.msg || "保存失败", !res.ok);
    if (res.ok) {
      runMode = res.run_mode;
      settingsData.run_mode = runMode;
      const tag = $("set-run-mode-tag");
      if (tag) tag.textContent = runMode === "auto" ? "自动并行" : (MODE_LABELS[runMode] || runMode);
      if (typeof lastSnapshot !== "undefined" && lastSnapshot) renderSignals(lastSnapshot);
      if (runMode !== "auto") setActiveSigTab(runMode);
      else if (sigActiveTab && sigActiveTab !== "auto") setActiveSigTab(enabledModes[0] || "donchian");
    }
  }

  async function saveModes() {
    const modes = [...document.querySelectorAll("#set-modes input:checked")].map(x => x.dataset.mode);
    if (!modes.length) { setMsg("❌ 至少需要一个模式", true); renderModeSettings(); return; }
    setMsg("保存模式中…");
    const res = await fetch(apiUrl("/api/settings/modes"), {
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
        const res = await fetch(apiUrl("/api/settings/symbols"), {
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
      const r = await fetch(apiUrl("/api/symbols/search?q=" + encodeURIComponent(q) + "&limit=30"));
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
          const res = await fetch(apiUrl("/api/settings/symbols"), {
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
    const res = await fetch(apiUrl("/api/settings/risk"), {
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

  function renderNetworkSettings() {
    const net = settingsData.network || (cfg && cfg.network) || "testnet";
    const m = settingsData.mainnet || (lastSnapshot && lastSnapshot.mainnet) || null;
    const statusEl = $("set-net-status");
    if (statusEl) {
      statusEl.textContent = net === "mainnet" ? "💰 主网 (真实资金)" : "🧪 测试网 (虚拟资金)";
      statusEl.style.color = net === "mainnet" ? "var(--red)" : "var(--green)";
    }
    document.querySelectorAll(".net-opt").forEach(b => {
      b.classList.toggle("active", b.dataset.net === net);
      if (b.dataset.net === "mainnet" && m && !m.unlocked) b.classList.add("disabled");
      else b.classList.remove("disabled");
    });
    const warnEl = $("set-net-unlock");
    if (warnEl) {
      if (m) {
        warnEl.textContent = m.warning;
        warnEl.style.display = "block";
        warnEl.className = "net-unlock-warn" + (net === "mainnet" ? " on-main" : (m.unlocked ? " ok" : " lock"));
      } else {
        warnEl.style.display = "none";
      }
    }
    const customRow = $("set-custom-row");
    if (customRow) customRow.style.display = (m && m.tier === "t3") ? "flex" : "none";
    const customInput = $("set-mainnet-custom");
    if (customInput && m && m.custom_limit > 0) customInput.value = m.custom_limit;
    const box = $("set-mainnet-box");
    if (box) box.style.display = net === "mainnet" ? "block" : "none";
    const cd = $("set-net-countdown");
    if (cd && m) {
      cd.style.display = "flex";
      $("cd-elapsed").textContent = (m.elapsed_days != null) ? m.elapsed_days : "--";
      const nextEl = $("cd-next");
      const nextLabel = $("cd-next-label");
      if (m.next_days != null) {
        nextEl.textContent = m.next_days;
        nextLabel.textContent = (m.tier === "locked") ? "距开放(天)" : "距下一档(天)";
        cd.classList.toggle("cd-locked", m.tier === "locked");
        cd.classList.remove("cd-done");
      } else {
        nextEl.textContent = "✓";
        nextLabel.textContent = "已全解锁";
        cd.classList.add("cd-done");
        cd.classList.remove("cd-locked");
      }
      $("cd-tier").textContent = m.tier || "--";
    } else if (cd) {
      cd.style.display = "none";
    }
  }

  function renderPnl() {
    const init = Number(settingsData.initial_capital || 0);
    const cur = Number(settingsData.equity || 0);
    const initEl = $("pnl-init");
    const curEl = $("pnl-cur");
    const pctEl = $("pnl-pct");
    const pctCard = pctEl && pctEl.parentElement;
    if (initEl) initEl.textContent = init > 0 ? fmt(init) + " U" : "--";
    if (curEl) curEl.textContent = fmt(cur) + " U";
    if (pctEl) {
      pctEl.textContent = "--";
      if (pctCard) pctCard.classList.remove("pnl-gain", "pnl-loss", "pnl-flat");
      if (init > 0) {
        const pnl = cur - init;
        const pct = (pnl / init) * 100;
        const psign = pnl > 0 ? "+" : "";
        pctEl.textContent = `${psign}${pct.toFixed(2)}%`;
        if (pctCard) pctCard.classList.add(pnl > 0 ? "pnl-gain" : (pnl < 0 ? "pnl-loss" : "pnl-flat"));
      }
    }
    const inp = $("set-pnl-init");
    if (inp && init > 0) inp.value = init;
  }

  async function savePnlInit() {
    const inp = $("set-pnl-init");
    const v = parseFloat(inp.value);
    if (!v || v <= 0) { setMsg("❌ 请输入大于 0 的起始资金", true); return; }
    try {
      const res = await fetch(apiUrl("/api/settings/initial_capital"), {
        method: "POST", headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ value: v }),
      });
      const data = await res.json();
      if (!data.ok) { setMsg("❌ " + (data.msg || "保存失败"), true); return; }
      settingsData.initial_capital = data.initial_capital;
      settingsData.equity = data.equity;
      renderPnl();
      setMsg(data.msg || "✅ 起始资金已保存");
    } catch (e) { setMsg("❌ 保存失败: " + e, true); }
  }

  async function switchNetwork(net) {
    const m = settingsData.mainnet || (lastSnapshot && lastSnapshot.mainnet);
    if (net === "mainnet" && m && !m.unlocked) { alert("🔒 " + m.warning); renderNetworkSettings(); return; }
    if (net === "mainnet") {
      const ok = confirm("⚠️ 即将切换到【主网 / 真实资金】模式！\n\n此模式下的每一笔开仓/平仓都会动用你的真实资产。\n请确保:\n  · 已在下方填好主网 API Key/Secret\n  · 已合理设置风控与敞口上限\n\n确认切换? (取消则停留在测试网)");
      if (!ok) { renderNetworkSettings(); return; }
    } else {
      const ok = confirm("切换回【测试网 / 虚拟资金】? (测试网使用虚拟资金, 不影响真实账户)");
      if (!ok) { renderNetworkSettings(); return; }
    }
    setMsg("切换网络中…");
    const res = await fetch(apiUrl("/api/settings/network"), {
      method: "POST", headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ network: net }),
    }).then(r => r.json());
    setMsg(res.msg || (res.ok ? "✅ 已切换" : "切换失败"), !res.ok);
    if (res.ok) {
      settingsData.network = res.network;
      if (cfg) cfg.network = res.network;
      updateNetBanner();
      renderNetworkSettings();
      loadSettings();
    } else {
      renderNetworkSettings();
    }
  }
}

// ---------------- 入口路由 ----------------
document.addEventListener("DOMContentLoaded", () => {
  initSettings();   // 始终装配设置弹窗监听 (面板与入口页共用)
  if (ACCOUNT) {
    initPanel(ACCOUNT);
  } else {
    initEntry();
  }
});
