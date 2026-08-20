# Binance 测试网自动量化交易系统

基于 **Binance U本位合约测试网**（testnet.binancefuture.com）的自动量化交易系统，采用 **Donchian 通道突破趋势跟踪**（海龟风格），带自动学习进化、交易所级止损、Web 仪表盘。

> ⚠️ **只用于测试网虚拟资金**，不碰真实资金。全部代码在本地运行，不消耗任何 API token。

> 📢 **开源声明 / 免责声明**：本项目 **开源，仅供学习与技术参考，不构成任何投资建议，也不构成任何要约或招揽**。量化交易涉及真实资金与杠杆风险，可能造成本金全部损失。下载、修改或使用本项目即表示你已充分理解并**自担一切风险**；**作者不对任何因使用本项目导致的盈亏、账户事故或资金损失负责**。请谨慎使用。

> 🔒 **请勿解除主网分级解锁限制**：主网开仓限额（测试网满 30 天 → 500U / 60 天 → 1000U / 90 天 → 自定义）是为保护你的**真实资金**而设的安全护栏。移除、篡改或绕过该限制（例如回填 `config.yaml` 的 `mainnet.since` 提前解锁）会让你直接暴露在全额真实资金风险下，**后果完全由你自己承担**。改动代码前请先读完本声明，保留该限制，谨慎使用。

---

## ✨ 功能总览

- 📈 **Donchian 通道突破趋势跟踪**：突破前 N 根 K 线最高/最低开仓，ATR 动态止损，反向突破通道出场（让利润奔跑）
- 🧬 **自动学习进化**：策略池 18 个组合（2h~1d 周期 × 参数），每天用最近 90 天**主网真实行情**回测评分，自动切换最优组合
- 🎚️ **动态杠杆 1x–5x**：突破越深（信号越强）杠杆越高、仓位越大；弱信号低倍小仓试错
- 🎯 **多策略模式可切换**：Donchian / 多策略 / 网格 / 均线 / RSI / 布林带，设置页勾选启用，各模式独立开仓统计与资金权重；同币种「先到先得」竞争制（⚠️ 短周期策略历史负期望，建议默认只开 Donchian）
- 🛡️ **交易所侧止损**：止损单挂在交易所（Algo Order API），本地断网/行情断流也能自动止损
- 🔒 **激进风控 + Web 实时调参**：单笔 ≤1000U、总持仓 ≤2000U、日亏损 30% 熔断、平仓冷静期；设置面板可**实时调整风控与敞口**并落盘 `config.yaml`，运行中热生效
- 🌐 **主网 / 测试网 一键热切换**：设置页切换交易网络，**无需重启**；默认测试网（虚拟资金），主网需独立 `BINANCE_API_KEY/SECRET`，切换前**二次确认 + 服务端校验**，主网模式下顶部显示红色警示横幅
- 📊 **本地 Web 仪表盘**：实时行情、持仓、权益曲线、信号强度/杠杆、下单/卖出成交流水、自动学习历史、操作日志
- 🕐 **24h 守护**：launchd 开机自启 + 崩溃自动重启（或 `deploy/manual.sh` 手动守护）

---

## 📁 在哪里配置什么文件

| 文件 | 作用 | 必须配置? |
|---|---|---|
| `.env` | API 密钥、模式、Telegram（从 `.env.example` 复制） | ✅ live 模式必须 |
| `config.yaml` | 交易对、策略参数、风控、杠杆、周期 | ✅ 按需修改 |
| `data/learner_state.json` | 自动学习结果（自动生成，勿手改） | ❌ 自动 |
| `data/state.json` | 运行状态（自动生成，勿手改） | ❌ 自动 |

### 1️⃣ 环境配置：`.env`（复制自 `.env.example`）

```bash
cp .env.example .env
```

```ini
# 交易模式: paper = 模拟下单(无需Key) / live = 真实测试网下单(需要Key)
TRADING_MODE=live

# 测试网 API Key / Secret (获取: https://testnet.binance.vision 用 GitHub 登录创建)
BINANCE_TESTNET_API_KEY=你的Key
BINANCE_TESTNET_API_SECRET=你的Secret

# 主网 API Key / Secret (仅主网模式使用, 真实资金; 获取: https://www.binance.com 账户→API管理)
# 与测试网 Key 完全独立; 也可在仪表盘 ⚙️ 设置 → 🌐 交易网络 中填写
BINANCE_API_KEY=你的主网Key
BINANCE_API_SECRET=你的主网Secret

# 交易网络: testnet(默认, 虚拟资金) / mainnet(真实资金); 通常留空由页面切换
TRADING_NETWORK=testnet

# Telegram 通知 (可选, 留空则不推送)
TELEGRAM_BOT_TOKEN=
TELEGRAM_CHAT_ID=
```

> 🔑 **注意**：`.env` 已在 `.gitignore` 中，永远不会提交到 git。

### 2️⃣ 策略配置：`config.yaml`（关键项）

```yaml
mode: live              # paper = 模拟 / live = 测试网真实下单
strategy_mode: donchian # 当前唯一策略: 通道突破趋势跟踪

# Donchian 参数 (自动学习器会按评分自动覆盖 entry/exit/sl_atr)
donchian:
  entry_n: 55           # 突破前55根K线最高/最低 → 入场
  exit_n: 20            # 反向突破前20根 → 出场
  sl_atr: 2.5           # 止损 = 入场 ∓ 2.5 × ATR
  leverage:             # 动态杠杆: 强信号高倍, 弱信号低倍试错
    min: 1
    max: 5

risk:
  max_single_order_notional: 1000   # 单笔名义价值上限 (U)
  max_total_position_notional: 2000 # 总持仓上限 (U)
  margin_per_position: 200          # 单笔保证金预算 (U) × 杠杆 = 实际名义仓位
  daily_loss_stop: 0.30             # 日亏损 30% 熔断
  cooldown_minutes: 60              # 平仓冷静期
```

### 3️⃣ 自动学习配置（`config.yaml` 内 `learner` 段）

```yaml
learner:
  enabled: true         # 启动时 + 每24小时自动学习一轮
  days: 90              # 用最近90天主网行情回测
  interval_hours: 24    # 学习间隔
```

---

## 🚀 怎么使用

### 1. 安装依赖

```bash
cd binance-quant
python3 -m venv .venv
.venv/bin/pip install -r requirements.txt
```

### 2. 配置 `.env`

按上文复制 `.env.example` 并填写。**先跑 paper 模式验证全流程**，再切 live：

```bash
TRADING_MODE=paper   # 先模拟
```

### 3. 启动

```bash
.venv/bin/python run.py
```

打开浏览器访问 **http://127.0.0.1:8090** 查看仪表盘。

> 🌐 **网络要求**：测试网 `testnet.binancefuture.com` 国内网络不可直连，需要代理（Clash 等）且代理节点可用。自动学习回测走主网 `fapi.binance.com`（公开数据，无需密钥）。

### 4. 切换到真实测试网下单（live）

1. 打开 https://testnet.binance.vision，GitHub 账号登录
2. 创建 API Key，保存 **API Key** 和 **Secret Key**
3. 页面底部点 **Request testnet funds** 领取测试资金（每次 10,000 USDT）
4. 编辑 `.env`：
   ```ini
   TRADING_MODE=live
   BINANCE_TESTNET_API_KEY=你的Key
   BINANCE_TESTNET_API_SECRET=你的Secret
   ```
5. 重启：`.venv/bin/python run.py`（日志显示 `模式: live`）

### 5. 24h 守护运行

**方式 A：手动守护脚本**（任何环境可用，推荐）：

```bash
cd binance-quant
./deploy/manual.sh start    # 启动 (nohup + PID 文件)
./deploy/manual.sh status   # 状态
./deploy/manual.sh logs     # 日志
./deploy/manual.sh stop     # 停止
```

**方式 B：launchd 开机自启**（macOS）：

```bash
cd binance-quant/deploy
./service.sh install    # 安装守护服务 (开机自启 + 崩溃自动重启)
./service.sh status     # 查看状态
./service.sh start|stop # 启停
./service.sh uninstall  # 卸载
```

### 6. 回测 / 工具

```bash
.venv/bin/python backtest.py                            # 默认 BTCUSDT 5m 最近3000根
.venv/bin/python backtest.py --symbol ETHUSDT --timeframe 4h --days 90
.venv/bin/python tune.py                                # 手动跑一轮参数优化 (multi模式)
.venv/bin/python scripts/income_query.py                # 查交易所资金流水(真实盈亏)
```

---

## 📊 仪表盘说明

| 区域 | 内容 |
|---|---|
| 顶部 | 心跳指示（主循环真实更新时间）、模式徽章、运行状态、暂停/恢复、重置今日 |
| 概览卡片 | 总权益、今日盈亏、未实现盈亏、持仓敞口、累计交易/胜率、手续费 |
| 权益曲线 | 实时权益走势 + 今日起始线 |
| 行情&持仓 | 各币种价格、24h涨跌、持仓方向/数量/杠杆、TP/SL |
| 策略信号 | 按启用模式分 **Tab** 展示（每 Tab 一个模式，带信号条数）；每个模式独立显示方向/信号强度/杠杆/距通道 |
| **设置面板**（⚙️） | **交易网络切换（主网/测试网）**、API 配置、策略模式勾选、交易对管理、**风控与敞口实时调参**（保存即热更新并落盘） |
| **自动学习历史** | 每轮学习的排名（最优组合/评分/第2/第3名），直观看到策略怎么进化 |
| **操作日志** | 系统每一步动作时间线：启动/开仓/平仓/熔断/学习/风控（保留200条） |
| **成交记录** | 下单/卖出逐笔流水（交易所真实成交，含手续费/盈亏） |

### ⚙️ Web 设置面板（⚙️ 右上角）

点击仪表盘右上角 **⚙️ 设置** 打开弹窗，分四块：

| 区块 | 作用 |
|---|---|
| 🌐 交易网络 | 切换 **测试网（虚拟资金）/ 主网（真实资金）**，热生效无需重启；主网需先填主网 API，切换前二次确认 |
| 🔑 测试网 API 配置 | 填写/保存 testnet API Key & Secret（live 模式需填，paper 免登录） |
| 🎯 策略模式 | 勾选启用哪些策略（卡片网格，显示各模式实时资金权重）；同币种竞争制，先到先得 |
| 📈 交易对管理 | 搜索/添加/移除交易对（有持仓的不可移除） |
| 🛡 风控与敞口 | **实时调整**风控参数，保存即热更新并落盘 `config.yaml`，无需重启 |

**交易网络切换（主网 / 测试网）**：

- 默认 **测试网**（`testnet.binancefuture.com`，虚拟资金，安全练手）。
- 切到 **主网**（`fapi.binance.com`，真实资金）前必须满足：
  1. 在「🌐 交易网络」区块填好 **主网 API Key / Secret**（独立存储于 `.env` 的 `BINANCE_API_KEY` / `BINANCE_API_SECRET`，与测试网 Key 完全隔离）；
  2. 点击「💰 主网 · 真实资金」会弹出**二次确认**框，明确提示真实资金风险；
  3. 服务端还会再校验一次「主网必须有 Key」，防止误触。
- 切换**热生效、无需重启**：交易所客户端按 `network` 重建 URL 与凭据，`config.yaml` 的 `network:` 同步落盘。
- 处于主网时，仪表盘**顶部出现红色闪烁横幅**「🔴 主网模式 · 真实资金交易…」，随时提醒。
- 若当前为 `live` 模式且切换到主网，本地持仓会被清空并交由下一轮账户同步重建，避免基于旧网络持仓误下单。

**主网分级解锁（保护真实资金）**：

为逐步放开真实资金风险，主网开仓受「测试网运行时长」分级限额约束（基准时间 `state.json` 的 `mainnet_baseline`，首次启动自动记录；也可用 `config.yaml` 的 `mainnet.since` 回填历史起点）：

| 测试网运行时长 | 档位 | 主网总持仓限额 | 说明 |
|---|---|---|---|
| < 30 天 | `locked` | 0（禁止切换） | 主网入口未开放，切换被服务端拒绝并提示剩余天数 |
| 30 ~ 60 天 | `t1` | **500 U** | 主网入口开放，限额 500U |
| 60 ~ 90 天 | `t2` | **1000 U** | 限额提升到 1000U |
| ≥ 90 天 | `t3` | **自定义** | 可在设置面板指定主网总持仓限额（默认用 `risk.max_total_position_notional`，不得超过该值） |

- 限额在**开仓预算计算时强制生效**：主网模式下每笔开仓的预算会被夹到 `cap − 当前敞口`，超出则拦截，总持仓绝不会超过当前档位上限。
- 设置面板「🌐 交易网络」区块实时显示解锁倒计时 / 当前档位 / 限额警告；未解锁时主网按钮置灰并点击弹窗提示。
- 跑满 90 天后，主网输入框内出现「自定义限额(U)」输入项，保存即写入 `config.yaml` 并热生效。
- ⚠️ **请勿解除该限制**：这是保护真实资金的安全护栏。任何移除 / 篡改 / 绕过（如为提前解锁而回填 `mainnet.since`）的行为都属于自行承担全额真实资金风险，作者不予负责。开源可改，但请保留这道护栏、谨慎使用。

**风控与敞口可调控项**（直接决定持仓敞口）：

| 项 | 含义 | 默认 |
|---|---|---|
| 总持仓上限 | 全部持仓名义价值上限（敞口天花板） | 2000 U |
| 单笔上限 | 单笔下单名义价值上限 | 1000 U |
| 单笔保证金 | 单笔保证金预算 × 杠杆 × 权重 = 实际名义仓位 | 200 U |
| 最大持仓数 | 同时持仓数上限 | 5 |
| 日亏熔断 | 当日回撤达到该百分比即熔断停机（面板填 %，如 30） | 30% |
| 冷静期 | 单币平仓后多久内不再开仓 | 60 分钟 |
| 杠杆 | auto 自动 / fixed 固定，及 min/max/fixed 倍率 | auto 1–5x |

> 这些参数与 `config.yaml` 的 `risk` / `leverage` 段一一对应；页面修改会**原地更新运行中的风控字典**，新开仓与 `check_open` 立即生效。

---

## 🧠 系统工作原理

### 策略：Donchian 通道突破

- **入场**：收盘价突破前 `entry_n` 根最高价 → 做多；跌破前 `entry_n` 根最低价 → 做空
- **止损**：入场价 ∓ `sl_atr × ATR`（波动自适应）
- **出场**：价格反向突破前 `exit_n` 根通道 → 平仓（无固定止盈，让利润奔跑）
- 每 5 分钟检测一次突破（避免延迟入场）

### 自动学习进化（每天一次）

```
策略池(18组合) → 每个组合用最近90天主网K线回测5币 → 评分 = 平均收益 - 0.3×最大回撤
→ 排序 → 自动切换最优组合实盘 → 历史轮次存入 data/learner_state.json
```

### 动态杠杆

`信号强度 = 突破深度 / (2×ATR)`，强度越高杠杆越高（1x–5x），名义仓位 = 保证金预算 × 杠杆：
强突破高倍重仓，弱突破低倍试探，同时限制杠杆保证止损距离 ≥ 50% 强平距离。

### 双保险止损

1. **交易所侧**：止损单挂交易所（STOP_MARKET），行情断流/本地崩溃也能触发
2. **本地侧**：每 5 秒轮询，触发后市价平仓（reduceOnly 防反向开仓）

---

## 📂 项目结构

```
binance-quant/
├── run.py                 # 入口: 启动交易引擎 + Web 服务
├── config.yaml            # 全部配置
├── .env                   # API 密钥 (git 忽略, 从 .env.example 复制)
├── backtest.py / tune.py  # 回测 / 参数优化工具
├── core/
│   ├── exchange.py        # 测试网 REST 客户端 (+ Algo Order 止损单)
│   ├── learner.py         # 自动学习进化 (策略池回测评分)
│   ├── orders.py          # 交易所成交记录重建
│   ├── state.py           # 状态持久化 (持仓/成交/权益/流水)
│   ├── risk.py            # 风控: 限额/熔断/冷静期
│   └── indicators.py      # ATR/EMA/RSI/布林带
├── strategies/
│   ├── donchian.py        # 主力策略: 通道突破 + 动态杠杆
│   ├── modes.py           # 多模式管理器 (6 模式统一接口 + 资金权重)
│   ├── grid.py / ma_cross.py / rsi.py / bollinger.py  # 可切换的短周期策略 (设置页启用)
│   └── engine.py          # 多策略自适应合成 (multi 模式)
├── engine/
│   ├── trader.py          # 主循环: 行情/止损/信号/学习 (含 _strategy_cycle 多模式竞争)
│   └── execution.py       # paper 模拟 / live 真实下单
├── deploy/                # launchd + 手动守护脚本
├── scripts/               # 工具脚本 (盈亏查询等)
└── web/                   # FastAPI + 仪表盘前端
    ├── app.py             # 路由 + 状态/设置 API (含 /api/settings/risk 热更新)
    └── static/            # 前端: index.html / app.js / style.css
```

---

## 📜 日志与数据

| 路径 | 内容 |
|---|---|
| `logs/quant.log` | 运行日志（滚动 5MB×3） |
| `logs/console.log` | 手动守护的 console 输出 |
| `data/state.json` | 状态持久化（git 忽略） |
| `data/learner_state.json` | 自动学习结果与历史轮次 |
| `data/backtest_result.json` | 最近一次回测结果 |

---

## ⚠️ 风险提示（诚实说明）

- **测试网的价值是流程验证，不是赚钱**。虚拟资金无真实风险，但也不产生真实收益
- Donchian 长周期趋势跟踪在 4 年主网回测中 5 币全正（+11.8% 平均），但**历史表现 ≠ 未来表现**
- 短周期（5m/15m/1h）策略已被回测证明负期望（手续费 0.05%×2 吃掉利润），已弃用
- 切换 live 模式前请确认风控参数符合预期；实盘前务必用主网长历史充分回测

---

## 🛠️ 常见问题

**Q: 仪表盘显示"行情获取失败"？**
A: 代理节点不可用。检查 Clash 等代理，切换可用节点后自动恢复（止损由交易所兜底，不受影响）。

**Q: 为什么不开仓？**
A: 当前是长周期趋势跟踪（2h~1d），需要价格突破通道才开仓，震荡行情会长时间等待，属正常。

**Q: 自动学习多久跑一次？**
A: 启动时立即跑一轮，之后每 24 小时一轮（`config.yaml` 的 `learner.interval_hours` 可调）。

**Q: 怎么确认系统真的在交易？**
A: 仪表盘"成交记录"展示交易所逐笔真实成交（下单/卖出），"操作日志"有开平仓时间线。
