# Binance 测试网自动量化交易系统

基于 **Binance U本位合约测试网**（testnet.binancefuture.com）的自动量化交易系统，采用 **单进程多账户**架构：一个进程同时跑 N 个互相隔离的子引擎，每个子引擎绑定一个测试网 API Key + 一个交易策略（`run_mode`）。

> ⚠️ **只用于测试网虚拟资金**，不碰真实资金。全部代码在本地运行，不消耗任何 API token。

> 📢 **开源声明 / 免责声明**：本项目 **开源，仅供学习与技术参考，不构成任何投资建议，也不构成任何要约或招揽**。量化交易涉及真实资金与杠杆风险，可能造成本金全部损失。下载、修改或使用本项目即表示你已充分理解并**自担一切风险**；**作者不对任何因使用本项目导致的盈亏、账户事故或资金损失负责**。请谨慎使用。

> 🔒 **请勿解除主网分级解锁限制**：主网开仓限额（测试网满 30 天 → 500U / 60 天 → 1000U / 90 天 → 自定义）是为保护你的**真实资金**而设的安全护栏。移除、篡改或绕过该限制（例如回填 `config.yaml` 的 `mainnet.since` 提前解锁）会让你直接暴露在全额真实资金风险下，**后果完全由你自己承担**。改动代码前请先读完本声明，保留该限制，谨慎使用。

> 📄 **许可证（License）**：本项目以 **MIT License** 开源，详见仓库根目录 [`LICENSE`](./LICENSE)。你可以在遵守许可证条款（保留版权与许可声明）的前提下自由使用、修改与再分发本项目的代码；本软件按「原样」提供，不作任何担保。许可证不覆盖、也不削弱上方的风险提示与免责声明——**量化交易的真实资金风险始终由使用者自行承担**。

---

## 📑 目录

- [✨ 功能总览](#功能总览)
- [🏗️ 架构：单进程多账户](#架构单进程多账户)
- [🎯 自动交易策略模式选择（重点）](#自动交易策略模式选择重点)
- [📁 配置什么文件](#配置什么文件)
- [🚀 怎么使用](#怎么使用)
- [🌐 仪表盘与入口页](#仪表盘与入口页)
- [🛡️ 风控与敞口](#风控与敞口)
- [🔒 主网分级解锁（保护真实资金）](#主网分级解锁保护真实资金)
- [🧠 系统工作原理](#系统工作原理)
- [📂 项目结构](#项目结构)
- [📜 日志与数据](#日志与数据)
- [⚠️ 风险提示](#风险提示)
- [🛠️ 常见问题](#常见问题)

---

## ✨ 功能总览

- 🏦 **单进程多账户**：一个进程跑 N 个隔离子引擎，每账户 = 一个测试网 API Key + 一个策略（`run_mode`），状态完全隔离（`data/accounts/<name>/state.json`）
- 📈 **Donchian 通道突破趋势跟踪**：突破前 N 根 K 线最高/最低开仓，ATR 动态止损 + 移动止损锁利，反向突破通道出场（让利润奔跑）
- 🧬 **自动学习进化**：策略池多组合回测评分，按实盘表现自动切换最优组合 / 分配各模式资金权重
- 🎚️ **动态杠杆 1x–5x**：突破越深（信号越强）杠杆越高、仓位越大；弱信号低倍小仓试错
- 🎯 **多策略模式可切换（6 种 + auto）**：`auto` 自动并行 / `donchian` 唐奇安通道 / `multi` 多策略自适应 / `grid` 网格 / `ma_cross` 均线交叉 / `rsi` RSI 反转 / `bollinger` 布林带；各模式独立开仓统计与资金权重；同币种「先到先得」竞争制
- 🛡️ **交易所侧止损**：止损单挂在交易所（Algo Order API），本地断网/行情断流也能自动止损
- 🔒 **激进风控 + Web 实时调参**：单笔 ≤1000U、总持仓 ≤2000U、**总敞口占权益可选 20/40/60/80% 上限**（>40% 实时分级警告 + 引擎拦截）、日亏损 30% 熔断、平仓冷静期；设置面板可**实时调整风控与敞口**并落盘 `config.yaml`，运行中热生效
- 🌐 **主网 / 测试网 一键热切换**：设置页切换交易网络，**无需重启**；默认测试网（虚拟资金），主网需独立 `BINANCE_API_KEY/SECRET`，切换前**二次确认 + 服务端校验**，主网模式下顶部显示红色警示横幅
- 📊 **本地 Web 仪表盘（入口页 + 每账户面板）**：入口页总览多账户盈亏对比 + 绑定 API；每账户独立面板展示实时行情、持仓、权益曲线、信号强度/杠杆、成交流水、自动学习历史、操作日志
- 🕐 **24h 守护**：macOS launchd / Windows 启动文件夹 开机自启 + 崩溃自动重启（或 `deploy/manual.sh` / 根目录 `start.sh` 手动守护）

---

## 🏗️ 架构：单进程多账户

```
accounts.yaml ──▶ AccountManager (engine/accounts.py)
config.yaml  ──▶   - 持有 N 个 TradingEngine, 各传各自 cfg + 各自 state 文件
.env 凭据引用 ─▶   - start()/stop()/get_engine()/bind()/unbind()/overview()

            ┌──────────────────┼──────────────────┐
            ▼                  ▼                  ▼
   TradingEngine(default)  TradingEngine(acct2)  TradingEngine(acct3)
    state: accounts/         state: accounts/       state: accounts/
      default/state.json       acct2/state.json       acct3/state.json
    api: .env 变量           api: .env 变量         api: .env 变量
    run_mode: auto           run_mode: grid         run_mode: rsi   ← 绑定即固定, 不可热改

Web 层 (web/app.py): 所有端点按 ?account=<name> 路由到对应子引擎
  打开 /            → 入口页 (API 列表 + 绑定 + 多账户盈亏对比总览)
  打开 /?account=x  → 该账户独立面板 (新标签页, 标签名 = 策略中文名)
```

设计要点：

1. **`TradingEngine` 几乎不改**，只是「一个账户 = 一个引擎」单元；多账户 = 同时跑多个 `TradingEngine`，各传各自 cfg + 各自 state 文件。
2. **`AccountManager` 编排层**：持有 N 个引擎，提供列表 / 聚合 / **热绑定 / 解绑** 接口。
3. **入账户感知**：读/写端点按 `?account=` 解析引擎；新增聚合与绑定端点。
4. **入口页 = 落地主页**：打开 `/` 先看「已绑定 API 列表 + 绑定表单」，它同时就是多账户盈亏对比总览。
5. **每账户面板在新标签页打开**：点 API → `window.open('/?account=<name>')`，标签名 = 策略中文名。
6. **策略绑定即固定**：`run_mode` 只在绑定 API 时设定，面板内不可热切换（换策略需重置账户或解绑重绑）。
7. **凭据不进 `accounts.yaml`**：yaml 只存绑定关系 + 凭据环境变量名，密钥仍放 `.env`（已 gitignore）。
8. **状态完全隔离**：`data/accounts/<name>/state.json`，互不影响。

---

## 🎯 自动交易策略模式选择（重点）

系统支持 **7 种运行模式**，由 `run_mode` 字段决定「哪个策略有开仓权」。信号面板始终展示所有启用模式的判断，但**只有 `run_mode` 指定的策略（或 auto 下全部启用模式）才有开仓权**。

### 1️⃣ run_mode 的两种语义

| run_mode | 含义 | 开仓权 |
|---|---|---|
| `auto` | **自动并行**（默认）：下方 `modes.enabled` 列出的全部模式同时跑 | 所有启用模式都有开仓权，同币种先到先得 |
| `<单一策略>` | `donchian` / `multi` / `grid` / `ma_cross` / `rsi` / `bollinger` | **只让指定策略开仓**；其余模式信号仍展示（仅无开仓权） |

> 例：`run_mode: donchian` 表示该账户只让 Donchian 开仓，但 `grid`/`rsi` 等模式的信号仍会在「策略信号」Tab 里展示，仅供观察。

### 2️⃣ 六种策略模式说明

| 模式 | 中文名 | 类型 | 思路 | 适用行情 |
|---|---|---|---|---|
| `donchian` | 唐奇安通道 | 趋势跟踪（海龟/CTA 风格） | 收盘价突破前 `entry_n` 根最高价→做多，跌破前 `entry_n` 根最低价→做空；反向突破 `exit_n` 根通道→出场（让利润奔跑）；入场价 ∓ `sl_atr×ATR` 止损，移动止损锁利 | 长周期（1d/4h）趋势市，**4 年主网回测 5 币全正** |
| `multi` | 多策略自适应 | 合成评分 | 综合 `grid`/`ma_cross`/`rsi`/`bollinger` 四个子策略，按**市场状态（regime）加权**：震荡市偏重 `grid`，趋势市偏重 `ma_cross`/`bollinger`；综合评分 = Σ(子策略评分 × 权重)；`dominant` = 评分绝对值最大的子策略 | 长周期，自动适配市场状态 |
| `grid` | 网格 | 均值回归（震荡市专用） | 以近期均价中枢为锚，低于中枢买入（看多评分），高于中枢卖出（看空评分） | 震荡市 |
| `ma_cross` | 均线交叉 | 趋势跟随 | `(EMA快 − EMA慢)` 以 ATR 归一化，快线斜率确认方向 | 趋势市 |
| `rsi` | RSI 反转 | 反转（均值回归） | RSI 超卖→买、超买→卖，中性区弱回归 | 震荡 / 反转 |
| `bollinger` | 布林带 | 自适应 | 震荡市做均值回归（触下轨买、触上轨卖），趋势市做突破顺势 | 震荡 + 趋势自适应 |

各子策略评分阈值：`|综合评分| > 0.15`（`OPEN_TH_DEFAULT`）才开仓。

**`multi` 模式的 regime 权重（`REGIME_WEIGHTS`）**：

| 市场状态 | grid | ma_cross | rsi | bollinger |
|---|---|---|---|---|
| 震荡 `ranging` | **0.50** | 0.10 | 0.25 | 0.15 |
| 上涨 `trend_up` | 0.05 | **0.50** | 0.10 | 0.35 |
| 下跌 `trend_down` | 0.05 | **0.50** | 0.10 | 0.35 |

### 3️⃣ 同币种竞争制 + 资金权重

- **同币种竞争**：同一币种同一时间只允许一个模式开仓（交易所仓位唯一）。某币种已有持仓时，**只让持有该仓的模式**做出场管理，其余模式仅展示信号、不开新仓。不同币种可被不同模式瓜分。
- **独立统计**：每个模式独立统计盈亏（`mode_stats`），系统按实盘表现**自动分配资金权重**（`data/mode_weights.json`）——赚钱的模式权重更高、分到更多预算。
- **auto 模式下的并行**：`run_mode: auto` 时 `modes.enabled` 全部并行，谁先触发信号谁先开仓；后续由资金权重调节各模式的实际仓位占比。

### 4️⃣ 策略绑定即固定（不可热改）

`run_mode` 是账户的**不可变身份**：只在**绑定 API 时**一次性设定，之后不能在面板热切换。

- **原因**：运行中热改 `run_mode` 会让同一账户的历史 `mode_stats`（按模式统计的盈亏/胜率）归属混乱，既有持仓是旧策略开的、新策略只影响未来开仓权，复盘与「账户=策略」对比失真。
- **换策略的方式**：
  - **重置账户**：清空该账户全部持仓/成交/统计（`data/accounts/<name>/` 下 state）后重绑新策略；
  - **解绑**：彻底移除该账户，再重新绑定（选新策略）。
  - 不做运行中热切换。
- **单账户 `default` 例外**：无 `accounts.yaml` 时退化为单一 `default` 账户，此时策略切换器**保留**给高级用户（`POST /api/settings/run_mode` 仍可用），因为没有「账户=策略」对比前提。

### 5️⃣ 行情/持仓表的「主导策略」列

行情与持仓表的「主导策略」列**永远显示具体策略名**，不显示 `auto` / `multi` 等元模式，展示每个币种当前真正主导的策略：

- **有持仓且为本系统开仓** → 显示该持仓的真实策略（`pos.mode`，如 `Donchian` / `网格` / `均线交叉` / `RSI反转` / `布林带`）。
- **有持仓但从交易所同步恢复**（`mode="交易所同步"`）→ 交易所不记录原始开仓策略，故展示：
  - **单一策略账户** → 该账户唯一绑定的具体策略（即其真实开仓策略，例如 `Donchian`）；
  - **`auto` / `multi` 账户** → 该币种当前各模式信号中**评分最高、有开仓动作优先**的具体策略（数据驱动，鼠标悬停会注明「非原始开仓策略」）。
- **无持仓** → 在该币种所有 `模式:币种` 信号里，取**评分最高、有开仓动作优先**的具体策略。不再千篇一律刷成同一值，也不再有 `--`。

### 6️⃣ 如何选择（建议）

- **新手 / 长周期练手**：`run_mode: donchian`（经典趋势跟踪，4 年回测全正），或 `auto`（全并行，系统自调权重）。
- **想看多策略协作**：`run_mode: multi`（按市场状态自动加权四个子策略）。
- **震荡市专吃**：`run_mode: grid` 或 `rsi` / `bollinger`（均值回归）。
- ⚠️ 短周期（5m/15m/1h）策略历史回测为负期望（手续费吃掉利润），建议默认只开 `donchian` / `multi` 长周期。

---

## 📁 配置什么文件

| 文件 | 作用 | 必须配置? |
|---|---|---|
| `.env` | API 密钥、模式、Telegram（从 `.env.example` 复制） | ✅ live 模式必须 |
| `config.yaml` | 交易对、策略参数、风控、杠杆、周期、全局 `run_mode` / `modes.enabled` | ✅ 按需修改 |
| `accounts.yaml` | 多账户绑定（每账户一个 API Key + 一个 `run_mode`），**可提交**，不含明文密钥 | ❌ 单账户可缺省 |
| `data/learner_state.json` | 自动学习结果（自动生成，勿手改） | ❌ 自动 |
| `data/state.json` | 单账户运行状态（自动生成，勿手改） | ❌ 自动 |
| `data/accounts/<name>/state.json` | 多账户每账户状态（自动生成，已 gitignore） | ❌ 自动 |

### 1️⃣ 环境配置：`.env`（复制自 `.env.example`）

```bash
cp .env.example .env
```

```ini
# 交易模式: paper = 模拟下单(无需Key) / live = 真实测试网下单(需要Key)
TRADING_MODE=live

# 测试网 API Key / Secret (获取: https://testnet.binancefuture.com 用 GitHub 登录创建; 注意是 U本位合约测试网, 非现货 testnet.binance.vision)
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

### 2️⃣ 全局策略配置：`config.yaml`（关键项）

```yaml
mode: live              # paper = 模拟 / live = 测试网真实下单
network: testnet        # testnet(默认) / mainnet(真实资金)

# 运行模式 (手动选择用哪个策略下单):
#   auto  = 自动: 下方 modes.enabled 全部并行, 同币种先到先得 (默认)
#   <策略> = 只让指定策略开仓, 例如 donchian / multi / grid / ma_cross / rsi / bollinger
# 信号面板始终展示所有启用模式的判断; 仅"开仓权"受此值约束
run_mode: auto

# 多模式并行 (可多选): donchian / multi / grid / ma_cross / rsi / bollinger
# 每个模式独立开仓独立统计, 同币种竞争制(先到先得), 系统按实盘表现分配资金权重
modes:
  enabled: [donchian, multi, grid, ma_cross, rsi, bollinger]

# Donchian 通道突破参数 (海龟风格)
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
  max_total_exposure_pct: 40       # 总敞口占权益上限 % (0=关闭; 可选 20/40/60/80) — 开仓前按权益比例核对, >40% 设置面板分级警告
```

### 3️⃣ 多账户绑定：`accounts.yaml`

位置：`binance-quant/accounts.yaml`（**可提交**，不含明文密钥）。每个账户 = 一个测试网 API Key + 一个策略（`run_mode`，绑定即固定）。

```yaml
accounts:
  - name: default             # 唯一标识 (data/accounts/<name>/ 目录)
    enabled: true             # false = 不加载、不交易
    network: testnet          # testnet / mainnet
    mode: live                # paper(模拟) / live(真实测试网)
    api_key_env: BINANCE_TESTNET_API_KEY       # .env 变量名, 不直接写密钥
    api_secret_env: BINANCE_TESTNET_API_SECRET
    run_mode: auto            # ★ 绑定时选定, 之后固定: auto/donchian/multi/grid/ma_cross/rsi/bollinger
    symbols: [BTCUSDT, ETHUSDT]   # 可选, 缺省继承全局 config.yaml
    modes_enabled: [donchian]     # 可选, 缺省继承全局(仅信号展示, 开仓权由 run_mode 决定)

  - name: grid-scalper
    enabled: true
    network: testnet
    mode: live
    api_key_env: BINANCE_TESTNET_API_KEY_ACCT2
    api_secret_env: BINANCE_TESTNET_API_SECRET_ACCT2
    run_mode: grid
```

**字段说明**：

| 字段 | 必填 | 说明 |
|---|---|---|
| `name` | ✅ | 唯一；`data/accounts/<name>/` 目录与切换键 |
| `enabled` | ✅ | 是否加载并交易 |
| `network` | ✅ | `testnet` / `mainnet` |
| `mode` | ✅ | `live` / `paper` |
| `api_key_env` / `api_secret_env` | ✅(live) | `.env` 变量名，**不直接写密钥** |
| `run_mode` | ✅ | **绑定策略，设定后固定**：`auto`/`donchian`/`multi`/`grid`/`ma_cross`/`rsi`/`bollinger` |
| `symbols` | ❌ | 缺省继承全局 |
| `modes_enabled` | ❌ | 缺省继承全局（仅影响信号展示，开仓权由 `run_mode` 决定） |

> **向后兼容**：无 `accounts.yaml` / `accounts: []` → `AccountManager` 退化为单一 `default` 账户，沿用 `config.yaml` 的 `api/network/run_mode/symbols`，老用户零感知。

### 4️⃣ 自动学习配置（`config.yaml` 内 `learner` 段）

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

打开浏览器访问 **http://127.0.0.1:8090** 查看仪表盘（默认进入**入口页**）。

### 🖱️ 一键启动 / 关闭脚本（Mac & Windows）

项目根目录提供跨平台的一键脚本，**不用记命令**：自动安装依赖（仅首次）、启动服务、并自动打开浏览器仪表盘；关闭脚本则停止后台服务。

| 系统 | 启动 | 关闭 |
|---|---|---|
| macOS / Linux | 终端运行 `./start.sh`，或双击 `start.command` | 终端运行 `./stop.sh`，或双击 `stop.command` |
| Windows | 双击 `start.bat` | 双击 `stop.bat` |

- **start**：若没有虚拟环境会自动建 `.venv` 并 `pip install -r requirements.txt`（仅首次较慢）；服务已在运行时只打开浏览器；启动后访问 http://127.0.0.1:8090
- **stop**：按端口 8090 找到进程并结束（Windows 只结束该端口的 python，不影响其它 python 程序）
- 日志输出：`logs/console.log`

> 💡 想让服务**开机 / 登录自动运行**，打开仪表盘入口页的 **⏻ 开机自启** 开关即可（macOS 用 launchd，Windows 用「启动」文件夹）。

> 🌐 **网络要求**：测试网 `testnet.binancefuture.com` 国内网络不可直连，需要代理（Clash 等）且代理节点可用。自动学习回测走主网 `fapi.binance.com`（公开数据，无需密钥）。

### 4. 切换到真实测试网下单（live）

1. 打开 https://testnet.binancefuture.com（U本位合约测试网；现货测试网 testnet.binance.vision 的 Key 与本系统不通用）
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
.venv/bin/python ml_train.py                            # 训练 ML 门禁/选择器模型 → data/ml_filter.pkl
.venv/bin/python scripts/income_query.py                # 查交易所资金流水(真实盈亏)
```

---

## 🌐 仪表盘与入口页

### 入口页（`/` 默认渲染）

打开根路径（无 `?account=`）→ 渲染**入口页**，它同时是「API 列表 / 绑定入口 / 盈亏总览」三合一：

- **📊 盈亏总览**（按账户细分）：
  - **顶部总览卡片**：总权益、今日盈亏、累计已实现盈亏、持仓数、账户数。
  - **按账户盈亏明细表**：逐账户展示 策略 / 网络 / 状态 / 权益 / 今日盈亏 / 累计已实现 / 胜率 / 交易数 / 持仓数（涨红跌绿）。
  - **🎯 策略模式对比表**（原面板模块，已移入控制台）：跨所有账户聚合各模式的 交易数 / 胜率 / 已实现盈亏 / 手续费 / 资金权重，按已实现盈亏降序，直观看「哪个策略更可靠」；已排除「交易所同步」等非策略记录。
- **⏻ 开机自启**（全局单次）：开启后登录/开机自动启动服务（仅 macOS / Windows 支持，全局生效）。
- **已绑定 API 列表**（账户卡片，即多账户盈亏对比总览）：

  | 账户 | 策略 | 网络 | 状态 | 权益 | 今日盈亏 | 累计已实现 | 胜率 | 交易数 | 持仓 | 操作 |
  |---|---|---|---|---|---|---|---|---|---|---|
  | default | 自动并行 | testnet | 🟢 | 10,234 | +123 | +580 | 62% | 16 | 2 | 打开面板 / 启用 / 解绑 / ⚙️设置 |
  | **合计** | — | — | — | **30,214** | **+200** | **+980** | — | **41** | **5** | — |

  - 状态：运行🟢 / 暂停⏸ / 熔断🚨 / 未验证⚠️。
  - **操作按钮顺序**：`打开面板` → `启用/停用` → `解绑` → `⚙️ 设置`（设置置末）。
    - **启用/停用**：启动或暂停该账户的策略循环与自动交易（暂停保留现有持仓，不再新开仓）。
    - **解绑**：停止该引擎并从账户列表移除（本地 state 文件保留备查），不可恢复；默认账户不可解绑。
    - **⚙️ 设置**：打开该账户的设置抽屉（见下）。
- **「+ 绑定新 API」表单**（折叠）：在此**一次性选定该账户的交易策略 `run_mode`**（`auto`/`donchian`/`multi`/`grid`/`ma_cross`/`rsi`/`bollinger`）+ 网络 / 模式 / API Key / 交易对；提交后写入 `accounts.yaml` + `.env` + 热加载新引擎，列表即时刷新。

### 每账户面板（`/?account=<name>`）

点击账户卡片「打开面板」→ 新标签页打开，标签名 = 策略中文名。该标签页是**纯仪表盘**，无设置入口、无「重置今日」：

| 区域 | 内容 |
|---|---|
| 顶栏 | 心跳指示（主循环真实更新时间，超时变色预警）、登录状态、模式徽章、运行状态、账户芯片、**暂停/恢复**；**主网模式下额外显示红色闪烁横幅**提醒真实资金 |
| 概览卡片 | 总权益、今日盈亏、未实现盈亏、持仓敞口、累计交易/胜率、手续费 |
| 权益曲线 | 实时权益走势 + 今日起始线 |
| 行情&持仓 | 各币种价格、24h涨跌、市场状态、**主导策略**、杠杆、持仓方向/数量、未实现盈亏、**止盈止损/立即平仓**（行内按钮，紧凑不挤压其它列） |
| 策略信号 | 按启用模式分 **Tab** 展示（每 Tab 一个模式，带信号条数）；每个模式独立显示方向/信号强度/杠杆/距通道 |
| 自动学习历史 | 每轮学习的排名（最优组合/评分/第2/第3名），直观看到策略怎么进化 |
| 操作日志 | 系统每一步动作时间线：启动/开仓/平仓/熔断/学习/风控（保留200条） |
| 成交记录 | 下单/卖出逐笔流水（交易所真实成交，含手续费/盈亏） |

### ⚙️ 设置抽屉（入口页「⚙️ 设置」打开，按账户路由）

点击账户卡片的 `⚙️ 设置` → 打开**设置抽屉**，标题为 `⚙️ <账户名> · 账户设置`，所有接口按该账户路由：

| 区块 | 作用 |
|---|---|
| 📊 盈亏模块 | 起始资金录入（盈亏比例据此计算） |
| 🌐 交易网络 | 切换 **测试网（虚拟资金）/ 主网（真实资金）**，热生效无需重启；主网需先填主网 API，切换前二次确认 |
| 🔑 测试网 API 配置 | 填写/保存 testnet API Key & Secret（live 模式需填，paper 免登录） |
| 🎯 策略模式 | **多账户场景为只读徽章**（策略绑定即固定，不可热改）；单账户 `default` 回退模式可在此热切换 `run_mode` |
| 📈 交易对管理 | 搜索/添加/移除交易对（有持仓的不可移除） |
| 🛡 风控与敞口 | **实时调整**风控参数，保存即热更新并落盘 `config.yaml`，无需重启 |
| 🗑 账户操作 | **重置账户**：清空该账户全部持仓/成交/统计并重新从交易所同步（不可恢复） |

> 多账户下「策略模式」是只读徽章（如「当前策略：自动并行（绑定于 API，不可热改）」）；要换策略请用「账户操作 → 重置账户」或「解绑」后重绑。

---

## 🛡️ 风控与敞口

**风控与敞口可调控项**（直接决定持仓敞口）：

| 项 | 含义 | 默认 |
|---|---|---|
| 总持仓上限 | 全部持仓名义价值上限（敞口天花板） | 2000 U |
| 单笔上限 | 单笔下单名义价值上限 | 1000 U |
| 单笔保证金 | 单笔保证金预算 × 杠杆 × 权重 = 实际名义仓位 | 200 U |
| 最大持仓数 | 同时持仓数上限 | 5 |
| 日亏熔断 | 当日回撤达到该百分比即熔断停机（面板填 %，如 30） | 30% |
| 冷静期 | 单币平仓后多久内不再开仓 | 60 分钟 |
| **总敞口占权益** | **总持仓名义价值 ÷ 权益 的上限**（开仓前引擎按权益比例核对；**>40% 实时分级警告** — 60% 琥珀激进 / 80% 红色极高风险；0=关闭） | **40%** |
| 杠杆 | auto 自动 / fixed 固定，及 min/max/fixed 倍率 | auto 1–5x |

> 这些参数与 `config.yaml` 的 `risk` / `leverage` 段一一对应；页面修改会**原地更新运行中的风控字典**，新开仓与 `check_open` 立即生效。`max_total_exposure_pct` 是**相对权益**的安全网：当账户权益缩水时，比例上限会比固定 USDT 限额更早收紧，防护穿仓；当前默认 40% 在权益 5k+ 时基本不改变既有行为（USDT 上限仍主导）。

**双保险止损**：

1. **交易所侧**：止损单挂交易所（STOP_MARKET / Algo Order），行情断流/本地崩溃也能触发
2. **本地侧**：每 5 秒轮询，触发后市价平仓（reduceOnly 防反向开仓）

---

## 🔒 主网分级解锁（保护真实资金）

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

---

## 🧠 系统工作原理

### 策略：Donchian 通道突破（主力，长周期）

- **入场**：收盘价突破前 `entry_n` 根最高价 → 做多；跌破前 `entry_n` 根最低价 → 做空
- **止损**：入场价 ∓ `sl_atr × ATR`（波动自适应）；开启移动止损后随价格创新高/新低上移/下移止损线锁利
- **出场**：价格反向突破前 `exit_n` 根通道 → 平仓（无固定止盈，让利润奔跑）
- 每 5 分钟检测一次突破（避免延迟入场）

> 其余模式（`grid`/`ma_cross`/`rsi`/`bollinger`/`multi`）见 [🎯 自动交易策略模式选择](#自动交易策略模式选择重点)。`run_mode: auto` 时所有启用模式并行竞争开仓。

### 自动学习进化（每天一次）

```
策略池(多组合) → 每个组合用最近90天主网K线回测 → 评分 = 平均收益 - 0.3×最大回撤
→ 排序 → 自动切换最优组合实盘 / 更新各模式资金权重 → 历史轮次存入 data/learner_state.json
```

### 动态杠杆

`信号强度 = 突破深度 / (2×ATR)`，强度越高杠杆越高（1x–5x），名义仓位 = 保证金预算 × 杠杆：
强突破高倍重仓，弱突破低倍试探，同时限制杠杆保证止损距离 ≥ 50% 强平距离。

---

## 📂 项目结构

```
binance-quant/
├── run.py                 # 入口: 启动 AccountManager(多账户) + Web 服务
├── start.sh / stop.sh     # 一键启动 / 关闭 (macOS / Linux, 终端运行)
├── start.command / stop.command  # 同上, macOS 双击即可运行
├── start.bat / stop.bat   # 一键启动 / 关闭 (Windows, 双击运行)
├── config.yaml            # 全局配置 (含 run_mode / modes.enabled / 风控 / 杠杆 / 周期)
├── accounts.yaml          # 多账户绑定 (每账户 API Key 变量名 + run_mode, 可提交)
├── .env                   # API 密钥 (git 忽略, 从 .env.example 复制)
├── backtest.py / tune.py / ml_train.py  # 回测 / 参数优化 / ML 训练
├── core/
│   ├── exchange.py        # 测试网 REST 客户端 (+ Algo Order 止损单)
│   ├── learner.py         # 自动学习进化 (策略池回测评分)
│   ├── orders.py          # 交易所成交记录重建
│   ├── state.py           # 状态持久化 (持仓/成交/权益/流水, 支持每账户 state_file)
│   ├── risk.py            # 风控: 限额/熔断/冷静期
│   ├── indicators.py      # ATR/EMA/RSI/布林带
│   ├── ml_filter.py        # ML 门禁/选择器 (纯 numpy)
│   └── config.py          # 配置读写 (含 run_mode / 主网解锁 / yaml+env 写入)
├── strategies/
│   ├── base.py            # 策略上下文与基类
│   ├── donchian.py        # 唐奇安通道突破 (主力, 长周期)
│   ├── modes.py           # 多模式管理器 (6 模式统一接口 + 同币种竞争制 + 资金权重)
│   ├── engine.py          # 多策略自适应合成 (multi 模式, regime 加权评分)
│   └── grid.py / ma_cross.py / rsi.py / bollinger.py  # 可切换的短/中周期策略
├── engine/
│   ├── accounts.py        # AccountManager + AccountSpec (多账户编排, bind/unbind/overview)
│   ├── trader.py          # 单账户 TradingEngine: 行情/止损/信号/学习 (含 _strategy_cycle 多模式竞争)
│   └── execution.py       # paper 模拟 / live 真实下单
├── deploy/                # launchd + 手动守护脚本
├── scripts/               # 工具脚本 (盈亏查询等)
└── web/                   # FastAPI + 仪表盘前端
    ├── app.py             # 路由 + 状态/设置 API (账户感知 ?account=, 含 /api/accounts/* 聚合与绑定)
    └── static/            # 前端: index.html / app.js / style.css
```

---

## 📜 日志与数据

| 路径 | 内容 |
|---|---|
| `logs/quant.log` | 运行日志（滚动 5MB×3） |
| `logs/console.log` | 手动守护的 console 输出 |
| `logs/engine_multi.log` | 多账户引擎日志 |
| `data/state.json` | 单账户状态持久化（git 忽略） |
| `data/accounts/<name>/state.json` | 多账户每账户状态（git 忽略） |
| `data/learner_state.json` | 自动学习结果与历史轮次 |
| `data/mode_weights.json` | 各模式资金权重（按实盘表现自动更新） |
| `data/backtest_result.json` | 最近一次回测结果 |
| `data/ml_filter.pkl` | ML 门禁/选择器模型（`ml_train.py` 生成） |

---

## ⚠️ 风险提示（诚实说明）

- **测试网的价值是流程验证，不是赚钱**。虚拟资金无真实风险，但也不产生真实收益
- Donchian 长周期趋势跟踪在 4 年主网回测中 5 币全正（+11.8% 平均），但**历史表现 ≠ 未来表现**
- 短周期（5m/15m/1h）策略已被回测证明负期望（手续费 0.05%×2 吃掉利润），已弃用
- 切换 live 模式前请确认风控参数符合预期；实盘前务必用主网长历史充分回测

---

## 🛠️ 常见问题

**Q: 入口页显示"行情获取失败"？**
A: 代理节点不可用。检查 Clash 等代理，切换可用节点后自动恢复（止损由交易所兜底，不受影响）。

**Q: 为什么不开仓？**
A: 当前是长周期趋势跟踪（2h~1d），需要价格突破通道才开仓，震荡行情会长时间等待，属正常。若 `run_mode: auto` 且多个模式启用，任一模式触发信号即开仓。

**Q: 自动学习多久跑一次？**
A: 启动时立即跑一轮，之后每 24 小时一轮（`config.yaml` 的 `learner.interval_hours` 可调）。各模式资金权重由学习器/实盘表现持续更新到 `data/mode_weights.json`。

**Q: 怎么确认系统真的在交易？**
A: 仪表盘"成交记录"展示交易所逐笔真实成交（下单/卖出），"操作日志"有开平仓时间线。

**Q: 想换个策略怎么办？**
A: 策略（`run_mode`）绑定即固定，面板内不可热改。请用账户卡片「⚙️ 设置 → 账户操作 → 重置账户」清空该账户统计后重绑新策略，或直接「解绑」后重新绑定（选新策略）。单账户 `default` 可在设置抽屉内热切换 `run_mode`。

**Q: 行情/持仓表「主导策略」列显示的是什么？**
A: 永远显示具体策略名（不显示 auto/multi 元模式）：本系统开仓的持仓显示其真实策略（`pos.mode`）；从交易所同步恢复、原始策略不可知的持仓，单一策略账户显示其唯一绑定策略，`auto`/`multi` 账户显示该币种当前信号最强的具体策略（悬停注明「非原始开仓策略」）；无持仓显示该币种信号中评分最高/最优先触发的模式，不再是固定值或 `--`。

**Q: 面板顶部还有「重置今日」按钮吗？**
A: 已移除（保留后端 `reset_day` 端点）。多账户下重置某账户请走入口页「⚙️ 设置 → 账户操作 → 重置账户」。
