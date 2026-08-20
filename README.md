# Binance 测试网自动量化交易系统

基于 **Binance U本位合约测试网**（testnet.binancefuture.com）的多策略自动量化交易系统，支持：

- 🔄 **多策略引擎**：网格 / 均线交叉 / RSI / 布林带，按市场状态（震荡/上涨/下跌）自动分配权重
- 🎚️ **动态杠杆**：根据波动率自动调整 1x–5x（波动大降杠杆、波动小升杠杆）
- 🛡️ **激进风控**：单笔≤200U、总持仓≤2000U、日亏损 30% 熔断停机、ATR 止盈止损、开仓冷静期
- 📊 **本地 Web 仪表盘**：实时行情、持仓、权益曲线、策略信号、成交记录
- 💻 **双模式**：`paper` 模拟下单（无需 Key 即可全流程跑通）/ `live` 真实测试网下单

---

## 快速开始

### 1. 安装依赖

```bash
cd binance-quant
python3 -m venv .venv
.venv/bin/pip install -r requirements.txt
```

### 2. 启动（默认 paper 模拟模式，无需 API Key）

```bash
.venv/bin/python run.py
```

打开浏览器访问 **http://127.0.0.1:8090** 查看仪表盘。

> 当前环境注意：如果测试网连接失败（SSL 错误），检查本机代理（Clash 等）的节点是否可用，
> 本项目依赖代理访问 `testnet.binancefuture.com`（该域名国内网络不可直连）。

### 3. 切换到真实测试网下单（live）

1. 打开 https://testnet.binance.vision ，用 GitHub 账号登录
2. 创建 API Key，保存 **API Key** 和 **Secret Key**
3. 页面底部点 **Request testnet funds** 领取测试资金（每次 10,000 USDT）
4. 复制 `.env.example` 为 `.env` 并填写：

```bash
cp .env.example .env
# 编辑 .env:
TRADING_MODE=live
BINANCE_TESTNET_API_KEY=你的Key
BINANCE_TESTNET_API_SECRET=你的Secret
```

5. 重启系统：`.venv/bin/python run.py`（日志会显示 `模式: live`）

---

## 配置说明（config.yaml）

| 配置项 | 默认值 | 说明 |
|---|---|---|
| `mode` | paper | `paper`=模拟 / `live`=真实测试网 |
| `timeframe` | 5m | K线周期：1m/5m/15m/1h |
| `symbols` | BTC/ETH/BNB/SOL/XRP | 交易对列表 |
| `leverage.mode` | auto | `auto`=按波动率动态调整 / `fixed`=固定 |
| `risk.max_single_order_notional` | 200 | 单笔下单名义价值上限 (U) |
| `risk.max_total_position_notional` | 2000 | 全部持仓名义价值上限 (U) |
| `risk.daily_loss_stop` | 0.30 | 日亏损 30% 熔断停机 |
| `risk.cooldown_minutes` | 10 | 每个币种平仓后冷静期 |
| `signal.open_threshold` | 0.35 | 综合评分超过此值开仓 |
| `signal.close_threshold` | 0.15 | 综合评分低于此值平仓 |
| `position.tp_atr` / `sl_atr` | 2.0 / 1.5 | ATR 止盈止损倍数 |
| `web.port` | 8090 | 仪表盘端口 |

---

## 策略引擎工作原理

每个 K 线周期（默认 5 分钟），系统对每个交易对执行：

1. **指标计算**：EMA、RSI、布林带、ATR、趋势强度
2. **市场状态识别**：`|趋势强度| / ATR% ≥ 0.3` → 趋势市（上涨/下跌），否则震荡市
3. **策略评分**：四个策略各自输出 [-1, 1] 评分
4. **权重组合**：不同市场状态使用不同权重（震荡市网格为主，趋势市均线/布林突破为主）
5. **信号执行**：综合评分 ≥ +0.35 开多，≤ -0.35 开空；持仓中评分减弱平仓
6. **动态杠杆**：`杠杆 = 3 / 波动比`，限制在 1x–5x

### 止盈止损（ATR 动态跟踪）

- 止盈：入场价 ± 2.0 × ATR
- 止损：入场价 ∓ 1.5 × ATR
- 每 5 秒检查一次，实时监控价格触发

### 熔断机制

当日亏损达到 30% 时系统自动熔断：停止开新仓（live 模式额外平掉所有持仓），
次日 0 点（UTC）自动重置，或点击仪表盘"重置今日"手动解除。

---

## Web 仪表盘

| 区域 | 内容 |
|---|---|
| 顶部 | **心跳指示**（主循环真实更新时间，>10秒变黄、>60秒变红告警）、模式徽章（PAPER/LIVE）、运行状态、暂停/恢复、重置今日 |
| 概览卡片 | 总权益、今日盈亏、未实现盈亏、持仓敞口、累计交易/胜率、手续费 |
| 权益曲线 | 实时权益走势 + 今日起始线 |
| 行情&持仓 | 各币种价格、24h涨跌、市场状态、综合评分、持仓方向/数量/杠杆、TP/SL |
| 策略信号 | 每币种四策略评分、波动比、建议杠杆 |
| **操作日志** | **系统每一步动作的时间线：启动/开仓/平仓/熔断/风控拒绝/AI调参/暂停恢复（保留200条）** |
| 成交记录 | 最近 30 笔开平仓明细（含盈亏、手续费、原因） |

---

## 项目结构

```
binance-quant/
├── run.py                 # 入口：启动交易引擎 + Web 服务
├── config.yaml            # 全部配置
├── .env                   # API Key（live 模式需要）
├── core/
│   ├── exchange.py        # 测试网 REST 客户端（公开+签名）
│   ├── indicators.py      # EMA/RSI/布林带/ATR/趋势强度
│   ├── state.py           # 持仓/成交/权益状态 + 持久化
│   ├── risk.py            # 风控：限额/熔断/冷静期
│   └── logger.py          # 日志
├── strategies/
│   ├── engine.py          # 市场状态识别 + 权重组合 + 动态杠杆
│   ├── grid.py            # 网格策略
│   ├── ma_cross.py        # 均线交叉策略
│   ├── rsi.py             # RSI 超买超卖策略
│   └── bollinger.py       # 布林带策略
├── engine/
│   ├── trader.py          # 主循环：行情/止盈止损/信号执行
│   └── execution.py       # paper 模拟撮合 / live 真实下单
└── web/
    ├── app.py             # FastAPI
    └── static/            # 仪表盘前端
```

## 日志

- 控制台实时输出
- `logs/quant.log` 滚动文件（5MB × 3 份）
- `data/state.json` 状态持久化（重启自动恢复持仓和历史成交）

## 风险提示

- 本系统仅用于 **测试网**，资金为虚拟资金
- 量化交易存在模型/行情风险，实盘前请充分回测
- 切换 live 模式前请确认风控参数符合你的预期

---

## 🆕 三大扩展模块

### 1. 回测模块（验证策略参数）

用**主网真实历史K线**逐根模拟实盘（含盘中止盈止损、双边手续费、真实仓位精度），输出收益/胜率/盈亏比/Sharpe/最大回撤：

```bash
.venv/bin/python backtest.py                          # 默认 BTCUSDT 5m 最近3000根
.venv/bin/python backtest.py --symbol ETHUSDT --timeframe 15m --bars 5000
.venv/bin/python backtest.py --days 30                # 最近30天
```

结果保存到 `data/backtest_result.json`。

### 2. AI 自动调参（策略自学习）

以回测为评估函数，随机搜索 + 爬山优化 12 个参数（阈值/止盈止损/网格/RSI/均线/布林带），**只在评分显著提升时应用**：

```bash
.venv/bin/python tune.py                              # 手动跑一轮优化
.venv/bin/python tune.py --symbol ETHUSDT --trials 30 # 更多尝试
```

- 优化结果写入 `data/tuned_params.json`，系统启动时自动合并生效
- 系统内已内置**定时自学习**：每 6 小时自动跑一轮优化（`config.yaml` 的 `tuning` 段可调），显著更优时自动应用并通过 Telegram 通知

### 3. Telegram 手机通知

开仓/平仓/熔断/AI调参实时推送到手机：

1. Telegram 里找 **@BotFather** → `/newbot` 创建机器人 → 拿到 token
2. 找 **@userinfobot** 查询你的 chat_id
3. 填入 `.env`：
```bash
TELEGRAM_BOT_TOKEN=你的token
TELEGRAM_CHAT_ID=你的chat_id
```
4. 重启系统 `python run.py` 即生效（不配置则静默跳过）

---

## 🕐 24小时守护服务（launchd 定时任务）

交易系统已注册为 macOS 守护任务：**开机自动启动、崩溃 3 秒内自动重启、不依赖终端窗口**。

```bash
cd binance-quant/deploy
./service.sh status        # 查看状态
./service.sh logs          # 查看日志
./service.sh stop          # 停止服务（交易暂停）
./service.sh start         # 重新启动
./service.sh uninstall     # 卸载守护服务
```

- 配置文件：`deploy/com.binance-quant.trading.plist`（已安装到 `~/Library/LaunchAgents/`）
- 系统内部调度：每 5 秒行情监控 / 每 5 分钟策略决策 / 每 6 小时 AI 自动调参
- 重启电脑后服务会自动恢复，无需任何手动操作

---

## 🎓 关于盈亏的诚实说明（重要）

**当前参数组合在回测和实盘中都验证为负期望**，原因：单笔 200U 小仓位 × 0.05% 手续费 × 5m 高频交易，手续费（往返 0.2U/笔）超过平均毛利润，数学上必亏。

这不是系统故障——实盘亏损与回测预测完全吻合，说明系统执行是忠实的。

**测试网的价值是流程验证，不是赚钱**。本系统已真实验证：
- ✅ 自动下单（测试网真实成交，交易所可查）
- ✅ 三种平仓规则（止盈/止损/信号消失）均真实触发
- ✅ 风控（单笔/总持仓限额、日亏熔断、冷静期）
- ✅ 24h 守护（launchd 开机自启、崩溃 3 秒自动重启）
- ✅ 操作日志时间线 + 真实心跳（每 5 秒）
- ✅ AI 自动调参（能找到更优参数，但救不了负期望的数学结构）
- ✅ 回测模块（诚实地提前预言了亏损——这是它最大的价值）
- ✅ Web 仪表盘实时监控

**未来若要做真实盈利策略**：转向低频趋势跟踪（1d 级别、持仓数天、大幅降频）、放大单笔资金量让手续费占比下降、或使用 maker 限价单降费；所有改动必须先主网长历史回测验证再实盘。
