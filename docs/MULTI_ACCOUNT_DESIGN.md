# 多账户量化系统 · 详细设计文档（v2）

> **目标**（用户原话 + 本轮 refinement）：
> 1. 「通过绑定的 api key 切换对应账户（对应策略），且有一个主页面统计各测试网 api key
>    通过不同交易策略的盈亏对比分析。」
> 2. **（本轮新增）在页面最前面加一个「登录 API 的主要入口」页**：列出所有已绑定的 API，
>    点击对应 API → 跳转到该策略的面板（**单独开一个浏览器标签页，标签名 = 交易策略名称**）。
> 3. **（本轮新增）交易策略只能在绑定 API 的地方配置，不能随意变更**（运行中热切换会导致数据不稳定）。
>
> 架构取向：**单进程多账户**——一个进程内跑 N 个互相隔离的子引擎，每个子引擎绑定一个测试网
> API Key + 一个策略。不做多进程/多端口，而是「一份引擎 + 入口页（API 列表/绑定/总览）+
> 每账户独立面板（新标签页打开）」。

---

## 1. 当前架构回顾（已核实，作为改造基准）

| 组件 | 文件 / 签名 | 现状 |
|------|-------------|------|
| 引擎 | `engine/trader.py:51` `class TradingEngine.__init__(self, cfg)` | **单账户**：1 个 `TradingState` + 1 个 `BinanceFutures` + 1 个 `RiskManager` + 1 个 `ModeManager` + 1 个 `_loop` 异步任务 |
| 状态 | `core/state.py:26` `class TradingState.__init__(self, cfg)` | `STATE_FILE = DATA_DIR / "state.json"`（**全局单文件**，硬编码） |
| 交易所 | `core/exchange.py:27` `class BinanceFutures.__init__(self, cfg, base_url=None)` | 读 `cfg["api"]`（测试网）/`cfg["api_mainnet"]`（主网）+ `cfg["network"]`；有 `set_credentials(key, secret)` 热更新 |
| 运行模式 | `engine/trader.py:67` `self.run_mode` | `auto`=全部并行；否则只让指定策略 `run_mode` 开仓（信号仍全展示）。见 `config.yaml:26` |
| 主循环 | `engine/trader.py:525 _loop` → `_tick` → `for sym: _strategy_cycle(sym)` | 每账户独立跑；`run_mode` 决定哪些模式有开仓权 |
| 快照 | `engine/trader.py:191 get_snapshot()` → `self.state.snapshot()` | 返回 `equity / day_pnl / strategy_stats / positions / trades / events / signals` 等 |
| Web | `web/app.py:69 create_app(engine)` | **所有端点硬绑定单个 `engine`**；真实读状态端点是 `GET /api/state`（`/docs` 故意 404） |
| 入口 | `run.py` | `engine = TradingEngine(cfg)` → `await engine.start()` → `create_app(engine)` → `uvicorn` |
| 现有运行模式选择器 | `web/static/app.js` `saveRunMode` + `POST /api/settings/run_mode` | 面板内可热切换 `run_mode`（**多账户场景下将被移除/隐藏**，见第 8.4 节） |

**关键结论**：`TradingEngine` 天然就是「一个账户 = 一个引擎」单元。多账户 = 同时跑多个
`TradingEngine`，每个传**各自的 cfg**（`api`/`network`/`run_mode`/`symbols`）+ **各自的 state 文件**。
改动是「加一层编排 + 让状态可隔离 + Web 账户感知」，而非重写策略逻辑。

---

## 2. 总体设计

```
                         ┌─────────────────────────────────────────┐
   accounts.yaml  ──────▶│            AccountManager                │
   (.env 凭据引用)        │  (engine/accounts.py)                    │
                         │  - load_accounts()                       │
                         │  - build_account_cfg(global, spec)       │
   config.yaml ─────────▶│  - engines: {name: TradingEngine}        │
   (全局默认)             │  - start()/stop()/get_engine()/         │
                         │    list_accounts()/overview()/          │
                         │    bind()/unbind()  (热加载)             │
                         └───────────────┬─────────────────────────┘
                                         │  每个 name 一个独立子引擎
              ┌──────────────────────────┼──────────────────────────┐
              ▼                          ▼                          ▼
     TradingEngine("donchian")   TradingEngine("grid")    TradingEngine("rsi")
       state: accounts/             state: accounts/          state: accounts/
         don/state.json               grid/state.json           rsi/state.json
       api: ACCT1 .env              api: ACCT2 .env           api: ACCT3 .env
       run_mode: donchian          run_mode: grid            run_mode: rsi   ← 绑定即固定, 不可热改
                                         │
                                         ▼
                    Web 层: create_app(manager)  + 入口页(落地主页)
                    - 所有端点加 ?account=<name>
                    - GET /api/accounts          列表 + 默认账户
                    - GET /api/accounts/overview 多账户盈亏对比(入口页用)
                    - POST /api/accounts/bind     绑定新 API(写 yaml+.env+热加载)
                    - POST /api/accounts/unbind   解绑
                    - 入口页 = API 列表(即总览) + 绑定表单(在此选策略)
                    - 点击 API → window.open('/?account=<name>') 新标签页, 标题=策略名
```

设计要点：
1. `TradingEngine` 几乎不改（仅加可选 `state_file` / `name`）。
2. 新增 `AccountManager` 编排层：持有 N 个 `TradingEngine`，提供列表/聚合/**热绑定/解绑**接口。
3. **Web 账户感知**：读/写端点按 `?account=` 解析引擎；新增聚合与绑定端点。
4. **入口页 = 落地主页**：打开 `/` 先看「已绑定 API 列表 + 绑定表单」，它同时就是多账户盈亏对比总览。
5. **每账户面板在新标签页打开**：点 API → `window.open('/?account=<name>')`，该标签页 `document.title` = 策略中文名。
6. **策略绑定即固定**：策略(`run_mode`)只在绑定 API 时设定，面板内不可热切换（见 8.4）。
7. **凭据不进 `accounts.yaml`**：yaml 只存绑定关系 + 凭据环境变量名，密钥仍放 `.env`（已 gitignore）。
8. **状态完全隔离**：`data/accounts/<name>/state.json`，互不影响。

---

## 3. 数据模型：`accounts.yaml`

位置：`binance-quant/accounts.yaml`（**可提交**，不含明文密钥）。

```yaml
# 多账户绑定: 每个账户 = 一个测试网 API Key + 一个策略(run_mode)
# 策略在绑定处一次性设定, 之后不可在面板热改(避免数据不稳定)
accounts:
  - name: donchian-run          # 唯一标识
    enabled: true               # false = 不加载、不交易
    network: testnet            # testnet / mainnet
    mode: live                  # paper(模拟) / live(真实测试网)
    api_key_env: BINANCE_TESTNET_API_KEY_ACCT1
    api_secret_env: BINANCE_TESTNET_API_SECRET_ACCT1
    run_mode: donchian          # ★ 绑定时选定, 之后固定(不可热改)
    symbols: [BTCUSDT, ETHUSDT] # 可选, 缺省继承全局
    modes_enabled: [donchian]   # 可选, 缺省继承全局(仅信号展示)

  - name: grid-scalper
    enabled: true
    network: testnet
    mode: live
    api_key_env: BINANCE_TESTNET_API_KEY_ACCT2
    api_secret_env: BINANCE_TESTNET_API_SECRET_ACCT2
    run_mode: grid
    symbols: [SOLUSDT, XRPUSDT]

  - name: rsi-reversion
    enabled: false
    network: testnet
    mode: paper
    api_key_env: BINANCE_TESTNET_API_KEY_ACCT3
    api_secret_env: BINANCE_TESTNET_API_SECRET_ACCT3
    run_mode: rsi
```

字段说明：

| 字段 | 必填 | 说明 |
|------|------|------|
| `name` | ✅ | 唯一；`data/accounts/<name>/` 目录与切换键 |
| `enabled` | ✅ | 是否加载并交易 |
| `network` | ✅ | `testnet` / `mainnet` |
| `mode` | ✅ | `live` / `paper` |
| `api_key_env` / `api_secret_env` | ✅(live) | `.env` 变量名，**不直接写密钥** |
| `run_mode` | ✅ | **绑定策略，设定后固定**：`auto`/`donchian`/`multi`/`grid`/`ma_cross`/`rsi`/`bollinger` |
| `symbols` | ❌ | 缺省继承全局 |
| `modes_enabled` | ❌ | 缺省继承全局（仅影响信号展示，开仓权由 `run_mode` 决定） |

> 语义：`run_mode: donchian` 表示该账户只让 donchian 开仓，其余模式信号仍展示（与现有单账户行为一致）。
> 多账户下 `run_mode` 是账户身份，**不在面板暴露切换器**。

---

## 4. 配置隔离与状态隔离

### 4.1 每账户 cfg 构建（`core/accounts.py: build_account_cfg`）

```python
def build_account_cfg(global_cfg: dict, spec: AccountSpec) -> dict:
    cfg = copy.deepcopy(global_cfg)
    cfg["name"] = spec.name
    cfg["network"] = spec.network
    cfg["mode"] = spec.mode
    cfg["run_mode"] = spec.run_mode          # 绑定即固定, 全程不变
    cfg["api"] = {"key": os.getenv(spec.api_key_env, ""),
                  "secret": os.getenv(spec.api_secret_env, "")}
    if spec.symbols:
        cfg["symbols"] = spec.symbols
    if spec.modes_enabled:
        cfg["modes"]["enabled"] = spec.modes_enabled
    return cfg
```
- 每个 `TradingEngine(per_account_cfg)` 拿到「全局默认 + 账户覆盖」完整 cfg，内部逻辑零改动生效。
- 主网解锁：`engine.start()` 把 `mainnet_baseline` 写进**本账户** state，各账户各自累计运行天数，互不串扰。

### 4.2 状态隔离（`core/state.py` 小改）

```python
# 现状
STATE_FILE = DATA_DIR / "state.json"
class TradingState:
    def __init__(self, cfg: dict): ...

# 改为（默认行为不变）
class TradingState:
    def __init__(self, cfg: dict, state_file: Path | None = None):
        self.state_file = state_file or (DATA_DIR / "state.json")
        ...
```
`AccountManager` 传 `DATA_DIR / "accounts" / spec.name / "state.json"`。`.gitignore` 追加 `data/accounts/`。

---

## 5. 凭据安全

- 原则：`accounts.yaml` 只存绑定关系 + 环境变量名，明文密钥永不入 yaml。
- `.env`（已 gitignore）加每账户变量：
  ```dotenv
  BINANCE_TESTNET_API_KEY_ACCT1=xxxx
  BINANCE_TESTNET_API_SECRET_ACCT1=yyyy
  BINANCE_TESTNET_API_KEY_ACCT2=xxxx
  BINANCE_TESTNET_API_SECRET_ACCT2=yyyy
  ```
- `core/accounts.py` 用 `os.getenv(spec.api_key_env)` 注入到每账户 `api`。
- 绑定 API 时（见 8.3），后端把密钥写入 `.env` 对应变量名（复用 `core/config.py` 的 `update_env_api` 思路，按变量名写）。
- 可选加固（P4）：`accounts.enc.yaml` Fernet 加密 / macOS Keychain。

---

## 6. 引擎层改造

### 6.1 新增 `engine/accounts.py`

```python
@dataclass
class AccountSpec:
    name: str
    enabled: bool
    network: str
    mode: str
    api_key_env: str
    api_secret_env: str
    run_mode: str                  # 绑定即固定
    symbols: list | None = None
    modes_enabled: list | None = None

class AccountManager:
    def __init__(self, global_cfg: dict):
        self.global_cfg = global_cfg
        self.accounts: list[Account] = []
        self._load()

    def _load(self):
        specs = load_accounts()
        if not specs:              # 向后兼容: 无多账户配置 → 单一 "default" 账户
            specs = [AccountSpec(name="default", enabled=True,
                     network=global_cfg["network"], mode=global_cfg["mode"],
                     api_key_env="BINANCE_TESTNET_API_KEY",
                     api_secret_env="BINANCE_TESTNET_API_SECRET",
                     run_mode=global_cfg.get("run_mode", "auto"))]
        self.accounts = [self._build(s) for s in specs if s.enabled]

    def _build(self, spec) -> Account:
        cfg = build_account_cfg(self.global_cfg, spec)
        state_file = DATA_DIR / "accounts" / spec.name / "state.json"
        engine = TradingEngine(cfg, state_file=state_file, name=spec.name)
        return Account(spec=spec, engine=engine, state_file=state_file)

    async def start(self):
        await asyncio.gather(*[a.engine.start() for a in self.accounts])

    async def stop(self):
        await asyncio.gather(*[a.engine.stop() for a in self.accounts])

    def get_engine(self, name: str | None) -> TradingEngine | None:
        if name:
            for a in self.accounts:
                if a.spec.name == name:
                    return a.engine
        return self.accounts[0].engine if self.accounts else None

    def list_accounts(self) -> list[dict]:
        return [{"name": a.spec.name, "strategy": a.engine.run_mode,
                 "network": a.engine.cfg["network"], "enabled": a.spec.enabled}
                for a in self.accounts]

    # ---- 热绑定 / 解绑 (入口页"绑定新 API"用, 无需重启) ----
    async def bind(self, spec: AccountSpec) -> Account:
        acc = self._build(spec)
        self.accounts.append(acc)
        await acc.engine.start()
        save_accounts([a.spec for a in self.accounts])   # 写回 accounts.yaml
        return acc

    async def unbind(self, name: str) -> None:
        acc = next((a for a in self.accounts if a.spec.name == name), None)
        if not acc: return
        await acc.engine.stop()
        self.accounts.remove(acc)
        save_accounts([a.spec for a in self.accounts])

    def overview(self) -> dict:   # 见第 9 节
        ...
```

- `TradingEngine` 加可选参数 `__init__(self, cfg, state_file=None, name=None)`，`name` 用于日志前缀。
- `Account` 容器：`{spec, engine, state_file}`。
- `bind/unbind` 让入口页「绑定/解绑」**热生效**，不必重启进程。

### 6.2 `run.py` 改造

```python
from engine.accounts import AccountManager
manager = AccountManager(load_config())
await manager.start()
app = create_app(manager)
...
finally:
    await manager.stop()
```
向后兼容：无 `accounts.yaml` → `AccountManager` 退化为单一 `default` 账户，行为同现在。

---

## 7. Web / API 层改造（`web/app.py`）

`create_app(engine)` → `create_app(manager)`。读/写端点按 `?account=` 解析引擎：

```python
def _engine(request) -> TradingEngine:
    return manager.get_engine(request.query_params.get("account"))
```

| 端点 | 变化 |
|------|------|
| `GET /api/state` | `?account=` 决定返回哪个账户快照 |
| `GET /api/settings` `POST /api/settings/*` | 加 `?account=`，作用于该账户 |
| `POST /api/control` `POST /api/logs/clear` | 加 `?account=`，只影响该账户 |
| `GET /api/accounts` | **新增**：返回 `list_accounts()` + `default` |
| `GET /api/accounts/overview` | **新增**：多账户盈亏对比（入口页列表用，见第 9 节） |
| `POST /api/accounts/bind` | **新增**：绑定新 API（写 `accounts.yaml` + `.env` + `manager.bind()` 热加载） |
| `POST /api/accounts/unbind` | **新增**：解绑（停引擎 + 写回 yaml） |
| `POST /api/accounts/toggle` | 可选：启停某账户（`enabled` 切换） |

`api_status()` / `mainnet_cap_info()` 等本就是 `engine` 方法，账户感知后自动按账户返回。

> `bind` 端点需校验：`name` 唯一、`run_mode` ∈ 合法策略集、`network/mode` 合法；`live` 必须有
> 对应 `.env` 密钥（写盘后 `engine._verify_api()` 验证）。`run_mode` 在 bind 时一次性写定，
> 之后**不提供**修改该字段的接口（对应 8.4 不可变约束）。

---

## 8. 入口页（落地主页）+ 前端改造

### 8.1 入口页结构（`/` 默认渲染）

打开 `/`（无 `?account=`）→ 渲染入口页，它同时是「API 列表 / 绑定入口 / 盈亏总览」三合一：

- **顶部**：标题「API 账户入口」+ 系统权益合计概览。
- **已绑定 API 列表**（即多账户盈亏对比总览，可点表头排序）：

  | 账户 | 策略 | 网络 | 状态 | 权益(U) | 今日盈亏 | 累计已实现 | 胜率 | 交易数 | 持仓数 | 操作 |
  |------|------|------|------|---------|----------|------------|------|--------|--------|------|
  | donchian-run | 唐奇安通道 | testnet | 🟢 | 10,234.5 | +123.4 | +580.2 | 62.5% | 16 | 2 | 打开面板 / 解绑 |
  | grid-scalper | 网格 | testnet | 🟢 | 9,980.1 | -45.2 | -12.3 | 48.0% | 25 | 3 | 打开面板 / 解绑 |
  | **合计** | — | — | — | **30,214.8** | **+200.1** | **+980.5** | — | **41** | **5** | — |

  - 状态：运行🟢 / 暂停⏸ / 熔断🚨 / 未验证⚠️（取自每账户 `api_status`）。
  - 「策略」列显示**中文名**（唐奇安通道/多策略/网格/均线交叉/RSI反转/布林带），对应 `run_mode`。
- **「+ 绑定新 API」表单**（折叠，见 8.3）：在此**一次性选定该账户的交易策略**。
- 行「打开面板」→ 跳转到该账户常规面板（8.2）。

### 8.2 点击 API → 新标签页打开，标签名 = 策略名

```js
// 入口页: 点「打开面板」
window.open('/?account=' + encodeURIComponent(name), '_blank');
```
- 新标签页加载同一 `index.html`，但 URL 带 `?account=<name>`。
- 该标签页 `onload`：读 `?account=` → `activeAccount = name` → 渲染该账户常规面板（持仓/信号/交易/日志）→
  **`document.title = STRATEGY_LABELS[run_mode]`**（如「唐奇安通道」），于是**浏览器标签显示策略名**。
- 入口页留在原标签页，可同时开多个账户多个标签页对照。

### 8.3 「绑定新 API」表单（策略在此配置）

表单字段：
- 账户名（唯一）
- 网络 `network`：testnet / mainnet
- 模式 `mode`：paper / live
- API Key / API Secret（提交后写入 `.env` 对应变量名，不落 yaml）
- **绑定交易策略 `run_mode`**：下拉 `auto/donchian/multi/grid/ma_cross/rsi/bollinger`（**只在这里选**）
- 交易对 `symbols`（可选）

提交 → `POST /api/accounts/bind` → 后端写 `accounts.yaml`（绑定关系）+ `.env`（密钥）+ `manager.bind()` 热加载新引擎。
入口页列表即时刷新出新账户。

### 8.4 策略绑定不可变约束（★ 本轮核心）

**规则**：`run_mode` 是账户的不可变身份，**只在绑定 API 时设定，之后不能在面板热切换**。

**原因（用户指出）**：运行中热改 `run_mode` 会导致数据不稳定——
- 同一账户的历史 `mode_stats`（已实现盈亏/胜率按模式统计）归属混乱，破坏「账户 = 策略」的对比前提；
- 既有持仓是旧策略开的（持仓自带 `mode` 用于出场管理），新策略只影响未来开仓权，新旧策略混在一个账户里，复盘与对比失真。

**实现**：
- 绑定表单选策略 → `accounts.yaml` 存 `run_mode` → `AccountManager` 据此构建 cfg，全程不变。
- **每账户面板隐藏运行模式切换器**：移除现有 `#set-run-mode`（commit 314f61e 加的 `saveRunMode` UI）。
  改为只读徽章：`当前策略：唐奇安通道（绑定于 API，不可热改）`。
- `POST /api/settings/run_mode` 端点**保留但多账户 UI 不暴露**（仅供单账户 `default` 回退模式的高级用户）。
- **要换策略**：提供「重置账户」按钮——点击后弹**友好确认框**：「重置将清空该账户全部持仓 /
  成交 / 统计且不可恢复，确定要重置并更换策略吗？」确认后清掉该账户 `state.json`
  （`data/accounts/<name>/`）并允许重新绑定（选新策略）。同时保留「解绑」用于彻底移除账户。
  **不做运行中热切换**。

### 8.5 每账户面板（`?account=` 渲染，无 `?account=` 时显示入口页）

- `index.html` 顶部加逻辑：`const params = new URLSearchParams(location.search);`
  `activeAccount = params.get('account') || localStorage.getItem('acct') || 'default';`
  - 有 `?account=` → 隐藏入口页区块，显示常规面板，`document.title = 策略名`。
  - 无 `?account=` → 显示入口页。
- 所有 fetch 追加 `?account=activeAccount`（`/api/state`、`/api/settings`、`/api/settings/*`、
  `/api/control`、`/api/logs/clear`）。现有 `renderSignals/renderPositions/renderTrades/终端日志`
  渲染函数**无需改**（每账户 snapshot 结构一致）。
- 终端日志窗口标题带账户名：`root@quant:~# 系统日志 [donchian-run]`，清屏/未读徽标按账户独立。
- 策略在面板内显示为只读徽章（8.4），无切换下拉。

---

## 9. 多账户盈亏对比（入口页列表数据源）

`GET /api/accounts/overview` → 入口页列表用（结构同 v1，此处强调「列表即总览」）：

```json
{
  "accounts": [
    {"name":"donchian-run","strategy":"donchian","strategy_label":"唐奇安通道","network":"testnet",
     "running":true,"paused":false,"halted":false,
     "equity":10234.5,"day_pnl":123.4,"realized_pnl":580.2,
     "win_rate":62.5,"total_trades":16,"open_positions":2,
     "api":{"configured":true,"verified":true,"wallet":10234.5}},
    {"name":"grid-scalper","strategy":"grid","strategy_label":"网格",...}
  ],
  "by_strategy": {
    "donchian":{"accounts":["donchian-run"],"sum_realized":580.2,"sum_equity":10234.5},
    "grid":{"accounts":["grid-scalper"],"sum_realized":-12.3,"sum_equity":9980.1}
  },
  "totals":{"equity":30214.8,"day_pnl":200.1,"realized_pnl":980.5,"open_positions":5,"accounts":3}
}
```
- `overview()` 复用每账户 `get_snapshot()`，聚合 `equity/day_pnl/realized_pnl/win_rate/总交易/持仓数`。
- 前端在入口页列表里渲染对比表 + 横向柱状图（按账户 `realized_pnl` 与按策略 `sum_realized`，涨红跌绿）。
- 点击行「打开面板」→ 8.2 新标签页。

---

## 10. 向后兼容

- 无 `accounts.yaml` / `accounts: []` → `AccountManager` 退化为单一 `default` 账户，沿用 `config.yaml`
  的 `api/network/run_mode/symbols`，**老用户零感知**。
- `default` 单账户模式下：入口页显示一行 `default`；面板行为与原先一致（且此时运行模式切换器
  可保留给高级用户，因无「账户=策略」对比前提）。
- `data/state.json`（老单账户状态）保留；多账户状态放 `data/accounts/<name>/`。
  首次启用多账户时，`default` 账户建议新建 `accounts/default/state.json`，旧文件留作备份。

---

## 11. 分阶段实施计划

| 阶段 | 内容 | 改动文件 | 可独立验证 |
|------|------|----------|------------|
| **P0 隔离层** | `TradingState` 加 `state_file`；新增 `core/accounts.py`（`AccountSpec`/`load_accounts`/`save_accounts`/`build_account_cfg`/`AccountManager` 含 `bind/unbind`）；`run.py` 改用 `AccountManager`（含单账户回退） | `core/state.py`, 新增 `core/accounts.py`, `run.py` | 起进程，N 个引擎各跑各的，state 落 `data/accounts/<name>/state.json` |
| **P1 API 账户感知** | `create_app(manager)`；读/写端点加 `?account=`；新增 `GET /api/accounts`、`GET /api/accounts/overview`、`POST /api/accounts/bind`、`POST /api/accounts/unbind`、`POST /api/accounts/toggle`；`core/config.py` 加写 yaml + 写 .env 变量辅助 | `web/app.py`, `core/config.py` | `curl /api/accounts`、`/api/accounts/overview`、`/api/state?account=xxx`、`bind` 后热出现新账户 |
| **P2 入口页（落地主页）** | 入口页 = API 列表(含 P&L 对比) + 「绑定新 API」表单(在此选策略) + 点击「打开面板」`window.open('/?account=')` 新标签页且 `document.title`=策略名 + 解绑/启停 | `web/static/app.js`, `index.html`, `style.css` | 入口页列出账户并对比盈亏；绑新账户即时出现；点开新标签页标题=策略名 |
| **P3 每账户面板** | 现有面板按 `?account=` 渲染；**移除运行模式热切换器**，改只读策略徽章；终端日志带账户名 | `web/static/app.js`, `index.html`, `style.css` | 各面板按账户独立；策略徽章只读；日志独立 |
| **P4 安全加固（可选）** | `accounts.enc.yaml` Fernet 加密 / Keychain；`.gitignore` 补 `data/accounts/` | `core/accounts.py`, `.gitignore` | 加密/解密往返验证 |

建议顺序：**P0 → P1 → P2 → P3** 先交付核心（绑定多 Key + 入口页 + 新标签页面板 + 策略不可变），P4 视需要再上。

---

## 12. 风险与权衡

- **API 限频**：每账户独立 `BinanceFutures` 客户端，公开行情会重复拉取。测试网 3–10 账户 × 5 币种压力极小；账户多可后续抽共享只读行情客户端。
- **单进程可用性**：进程挂 = 所有账户停。测试网可接受，已有 launchd 自启。
- **内存**：每引擎持 `State/ModeManager/StrategyEngine`；10 账户内开销可忽略。
- **主网解锁**：每账户各自累计 `mainnet_baseline`，比现在更合理。
- **同币种多账户并行**：各账户独立 API Key/子账户，`max_total_position_notional` 每账户独立预算，互不干扰——正是「多 Key 各自跑各自策略」的效果。
- **策略不可变的代价**：换策略需解绑重绑（或重置账户）。换来的是数据稳定与对比可信，符合用户要求。若未来确需「同账户换策略且保留历史」，可加「策略归档」机制（把旧 `mode_stats` 冻结进 `archived_mode_stats`），本期不做。

---

## 13. 已确认决策（用户拍板，实现依据）

1. **入口页合一**：「API 列表 + 绑定 + 盈亏总览」**三合一**（不拆页）。
2. **站点密码**：**不需要**站点级密码（仅 API Key 绑定入口）。
3. **换策略方式**：提供「**重置账户**」按钮 + 友好确认提示（清空该账户持仓/成交/统计后重绑新策略）；
   同时保留「解绑」彻底移除。不做运行中热切换。
4. **单账户 default**：多账户场景**隐藏**运行模式切换器；单账户 `default` 回退模式**保留**切换器。
5. **`default` 状态迁移**：首次启用多账户，`default` 账户**新建 `accounts/default/state.json`**，
   旧 `data/state.json` **保留作备份**（首次创建时若旧文件存在则复制迁移历史，原文件不删）。
6. **开新标签页**：主入口 tab 保留；点击绑定列表中的任意 API → **新开标签页**并给标签命名
   （`document.title` = 策略名），方便分辨哪个是哪个。

---

*文档状态：v3 决策已确认（入口页三合一 / 无站点密码 / 重置账户+友好提示 / 单账户保留切换器 /
新建目录旧文件保留 / 开新标签页命名）。按 P0→P3 顺序实现中。*
