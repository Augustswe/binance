# Binance 测试网自动量化交易系统

基于 **Binance U本位合约测试网**（testnet.binancefuture.com）的自动量化交易系统，采用 **Donchian 通道突破趋势跟踪**（海龟风格），带自动学习进化、交易所级止损、Web 仪表盘。

> ⚠️ **只用于测试网虚拟资金**，不碰真实资金。全部代码在本地运行，不消耗任何 API token。

---

## ✨ 功能总览

- 📈 **Donchian 通道突破趋势跟踪**：突破前 N 根 K 线最高/最低开仓，ATR 动态止损，反向突破通道出场（让利润奔跑）
- 🧬 **自动学习进化**：策略池 18 个组合（2h~1d 周期 × 参数），每天用最近 90 天**主网真实行情**回测评分，自动切换最优组合
- 🎚️ **动态杠杆 1x–5x**：突破越深（信号越强）杠杆越高、仓位越大；弱信号低倍小仓试错
- 🛡️ **交易所侧止损**：止损单挂在交易所（Algo Order API），本地断网/行情断流也能自动止损
- 🔒 **激进风控**：单笔 ≤1000U、总持仓 ≤2000U、日亏损 30% 熔断、平仓冷静期
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
| 策略信号 | Donchian 方向、信号强度、动态杠杆（强信号高倍） |
| **自动学习历史** | 每轮学习的排名（最优组合/评分/第2/第3名），直观看到策略怎么进化 |
| **操作日志** | 系统每一步动作时间线：启动/开仓/平仓/熔断/学习/风控（保留200条） |
| **成交记录** | 下单/卖出逐笔流水（交易所真实成交，含手续费/盈亏） |

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
│   ├── donchian.py        # 当前主力策略: 通道突破 + 动态杠杆
│   └── (grid/ma_cross/rsi/bollinger)  # 旧多策略引擎 (保留可切回)
├── engine/
│   ├── trader.py          # 主循环: 行情/止损/信号/学习
│   └── execution.py       # paper 模拟 / live 真实下单
├── deploy/                # launchd + 手动守护脚本
├── scripts/               # 工具脚本 (盈亏查询等)
└── web/                   # FastAPI + 仪表盘前端
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
