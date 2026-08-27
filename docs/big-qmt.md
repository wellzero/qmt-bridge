# 完整版 QMT（Big QMT）迁移设计

> 状态：设计已定稿（2026-08-26）；**§6 步骤 1（后端抽象 PR）已实施** ——
> `TraderBackend` 协议 + `MiniQmtBackend` / `BigQmtAdapter` 双后端 +
> 能力位降级 + CLI 开关 + xtdata shim 安装器 + 部署脚本，测试全 mock 通过。
> 背景：券商自 2026-07-06 起停用 miniQMT（独立交易 / xtquant 外部直连），存量客户 1~2 个月内清退。

## 1. 背景

自 2026-07-06 起，多家券商新开通的 QMT 策略交易权限**默认不再包含 miniQMT**
（独立交易 / 极简 / xtquant）功能；存量客户面临"后续专项清理"，
国金证券已于 2026-08-21 确认对存量客户关停。

官方理由：外部进程通过本地 xtquant 直连 QMT 属于"**外接系统**"，触碰合规红线。
完整版 QMT 的内置策略交易（模型交易）不受影响。

### 现有架构为何失效

qmt-bridge 当前在 QMT 客户端**外部**运行，直连 miniQMT 进程：

- 交易：`XtQuantTrader(path, session_id)` attach 到 miniQMT
  （`src/qmt_bridge/server/trading/manager.py`）—— miniQMT 关停后
  `connect()` 返回 `-1`
- 行情：外部 `xtquant.xtdata` attach —— 同为外部直连机制，预计一并失效

因此迁移方向是：**把"接触 QMT"的部分移入完整版 QMT 内置 Python 沙箱，
其余部分保持原位**。

## 2. 目标架构

已确认的分层（与 miniQMT 时代完全兼容的对外接口）：

```text
```text
┌─ Linux ──────────────────────────────────────────────────────────┐
│   live-trading 策略     paper-trading 策略     Streamlit 仪表盘    │
│          │                    │                      │           │
│          └────────────────┬───┴──────────────────────┘           │
│               qmt-bridge 客户端（本仓库 client/，纯 HTTP/WS 通道） │
└───────────────────────────┬──────────────────────────────────────┘
                            │ HTTP / WebSocket（局域网，接口零改动）
┌─ Windows ─────────────────▼──────────────────────────────────────┐
│  qmt-server（FastAPI，本仓库 server/）                             │
│    ├─ paper-trading → server/paper_trading/ 本地撮合引擎           │
│    │    价格源 = 行情（经 shim 的 xtdata.get_full_tick）           │
│    └─ live-trading → 交易适配层（bigqmt 模式，逐方法映射 §4）       │
│           │                                                       │
│           │  xtquant_big_convert 客户端（pip，运行在 qmt-server 内）│
│           │  Redis / ZMQ RPC ＋ xtquant import shim（行情）        │
│  ┌────────▼──────────────────────────────────────────────────┐   │
│  │ 完整版QMT（big QMT client，同机）                            │   │
│  │   └─ 内置Python：xtquant_big_convert 服务端                  │   │
│  │       必须支持 qmt-bridge 交易 API 所需 RPC 方法全集（§4）：   │   │
│  │       passorder / cancel / get_trade_detail_data / 行情      │   │
│  └────────────────────────────────────────────────────────────┘   │
└───────────────────────────────────────────────────────────────────┘
```

完整工作流（live-trading，四跳）：

1. **Linux**：live-trading 策略照常通过 **qmt-bridge 客户端**发 REST/WS 请求——
   客户端只是通道，不感知后端变化，策略零改动
2. **Windows qmt-server**（FastAPI）：按路由分发——paper 进本地撮合引擎；
   live 进交易适配层（bigqmt 模式），把 qmt-bridge API 逐方法翻译成 RPC 调用（§4）
3. **xtquant_big_convert 客户端**（qmt-server 进程内）：经 Redis/ZMQ RPC
   请求同机完整版 QMT 内置 Python 中的 xtquant_big_convert 服务端
4. **xtquant_big_convert 服务端** → `passorder` / `get_trade_detail_data`
   → 券商柜台

即：`Linux 策略 ↔ qmt-bridge 客户端 ↔ Windows qmt-server ↔ xtquant_big_convert ↔ 完整版QMT`。
xtquant_big_convert ＋ 完整版QMT **整体取代 miniQMT ＋ xtquant**；
qmt-server 仍部署在 Windows（与完整版 QMT 同机），Linux 侧只跑策略与客户端。
**远端必须实现 qmt-bridge 交易 API 所需方法全集**（§4），
缺失的方法按能力位降级或推动上游补充。

### 各层职责

**定位**：`xtquant_big_convert`（跑在完整版 QMT 内置 Python）**整体取代
miniQMT + xtquant 的角色**——对外提供与 miniQMT 同名的行情/交易方法，
qmt-server 及其上层策略不感知差别。

| 层 | 机器 / 位置 | 职责 |
|----|------|------|
| live/paper 策略、仪表盘 | Linux | 照常通过 qmt-bridge 客户端调 REST/WS API（零改动） |
| qmt-bridge 客户端 | Linux（本仓库 `client/`） | 纯通道：HTTP/WS 到 Windows qmt-server，不感知后端切换 |
| qmt-server | Windows（本仓库 `server/`，FastAPI） | 对外 API 不变；live 走交易适配层（bigqmt 模式逐方法映射，§4）；xtdata 经 shim 透明转发 |
| （qmt-server 内）paper-trading | Windows，qmt-server 进程内 | `PaperQuantTrader` 本地撮合，**不向 QMT 发任何委托**，仅消费行情价格 |
| xtquant_big_convert 客户端 | Windows（qmt-server 进程内，pip） | 把交易/行情调用翻译成 Redis/ZMQ RPC |
| xtquant_big_convert 服务端 | Windows，完整版 QMT 内置 Python 3.6 沙箱 | 策略（`init`/`handlebar`/`adjust` 驱动）封装 `passorder`、`cancel`、`get_trade_detail_data`、行情为 RPC 服务；**必须实现 qmt-bridge 交易 API 所需方法全集（§4）** |

### paper 路径几乎零改动

`server/paper_trading/engine.py` 已是完整本地引擎：本地账户状态、本地撮合、
回调与 `XtQuantTraderCallback` 对齐、模型与 xtquant 类型对齐。
唯一的 QMT 依赖是价格源 `XtdataPriceSource`（`get_full_tick`，
且自带静态价格回退）。shim 生效后该价格源继续工作。

额外收益：经 xtquant_big_convert，`get_full_tick` 走 **FormulaServer 快路径**
（QMT C++ 服务，端口 58600，p50 ≈ 0.07ms，不占 Python GIL），
每日 11:19–12:34 模拟盘拥塞窗口不会与 QMT 侧 Python GIL 争抢。

## 3. 集成设计

采用**依赖 + 适配器**方式，不 vendor / 不 fork（上游迭代快，v0.2.9 于
2026-08-25 发布；QMT 侧 Py3.6 沙箱代码混入本仓库无收益）。
许可证 MIT，兼容。

### 3.1 依赖引入

```toml
# pyproject.toml
[project.optional-dependencies]
bigqmt = ["xtquant-big-convert[redis]>=0.2.9"]
```

客户端要求 Python ≥ 3.8，满足本仓库 3.10+ 要求。

### 3.2 交易后端抽象

`XtTraderManager` 已是对 xttrader 的 1:1 门面，抽取协议后并列为双后端：

```text
TraderBackend (protocol, 生命周期 + 能力位；业务方法全集与 manager.py 一致，
               由 tests/test_trading_backends.py 的运行时方法全集断言保证)
  ├─ MiniQmtBackend  # 现有 XtQuantTrader 实现，原样保留
  └─ BigQmtAdapter   # 新增，基于 xtquant_compat；qmt-server（Windows）
                     # 进程内部的实现细节，Linux 策略完全不感知
```

```python
# BigQmtAdapter 核心初始化（运行于 Windows qmt-server 进程内；
# xtquant_compat 为模块级单例，configure() 原地更新）
from bigqmt_signal_trader.xtquant_compat import StockAccount, configure, xt_trader

configure()
acc = StockAccount(xt_trader.client.account_id, "STOCK")
# xt_trader.query_stock_asset(acc) / order_stock(...) / order_stock_async(...)
# 方法名与 miniQMT 一致；connect()/subscribe() 恒返回 0，lifespan 启动代码无需改动
```

CLI 增加 `--trader-backend mini|bigqmt`（`server/cli.py`）。

**映射方向（关键约定）**：对外以 **qmt-bridge REST API 为准**，策略零改动。
`BigQmtAdapter` 位于 Windows qmt-server 进程内，职责是把 qmt-bridge
每个交易接口翻译成 xtquant_big_convert 客户端的 RPC 调用；
**远端 xtquant_big_convert 必须实现这些方法**（§4 映射表即远端支持需求清单）。
上游尚未覆盖的方法有三条出路：

1. **能力位降级 503**（默认）：路由声明不支持，错误信息注明"等待远端支持"
2. **推动上游**：向 xtquant_big_convert 提 issue / PR 扩充 RPC 白名单
   （如银证转账、`cancel_order_stock_sysid` 变体等）
3. **自行扩展**：在自部署的 QMT 侧服务端副本中加白名单方法
   （MIT 允许；承担与上游分叉的维护成本）

### 3.3 行情 shim（零代码改动）

xtquant_big_convert 附带 `xtquant` import shim。本仓库 routers/ws/scheduler
中的 `from xtquant import xtdata` **均为模块顶层导入**（非惰性，共 23 处），
因此必须在 `cli.py` 构建 app（触发路由导入）之前、按后端类型插入 shim 路径：

```python
sys.path.insert(0, str(shim_src_dir))  # backend == "bigqmt" 时
```

已实现（`server/bigqmt_shim.py`）：`qmt-server` 在 `--trader-backend bigqmt`
时、`qmt-scheduler` 在 `QMT_BRIDGE_TRADER_BACKEND=bigqmt` 时自动安装；
shim 目录解析顺序为 `QMT_BRIDGE_BIGQMT_SHIM_DIR`（推荐：部署脚本
`--shim-out` 导出的专用目录，避免 wheel 顶层 `xtquant/` 覆盖 site-packages
里的真实 xtquant）→ 已安装包自带的顶层 `xtquant`（特征检测：其
`xttrader.py` 重导出 `bigqmt_signal_trader`）。

`server/helpers.py`、`scheduler.py`、整个下载管线无需改动。
注意：shim 路径必须位于真实 xtquant 之前。

### 3.4 能力开关与降级

后端声明 `SUPPORTS_*` 能力位，无对应能力的路由返回 503（带明确错误信息）
而非崩溃。覆盖映射见 §4。

### 3.5 部署工具

- `scripts/deploy_bigqmt_server.py`：从已安装 wheel 中提取
  `bigqmt_signal_trader/` + `BIGQMT_REDIS_DRYRUN.py` 入口，
  复制到 QMT 客户端 `python` 目录；生成 gitignored 的
  `bigqmt_signal_trader_local_config.py` 模板（账户 ID + Redis 配置）
- 同步更新 `justfile` 快捷命令（项目规范）
- QMT 侧 Python 3.6 只装所选 transport 的依赖（Redis 模式为纯 Python，
  可手动复制包目录规避旧 OpenSSL SSL 问题）

### 3.6 事件回调（live 路径，需验证）

现有 `trading/callbacks.py` 将 `XtQuantTraderCallback` 桥接到 asyncio 并推送 WS。
xtquant_big_convert 的委托/成交/错误事件经 Redis pubsub（`exec_events`）推送，
但 drop-in 的 `register_callback` 通道**可能不投递**——集成时需验证，
必要时将 pubsub 事件流直接接入同一 asyncio 桥接层。

## 4. qmt-bridge API ↔ 远端 RPC 映射（远端支持需求清单）

策略侧继续调用 qmt-bridge 现有 REST/WS API，**因此远端 xtquant_big_convert
必须实现下表各接口所需的 RPC 方法**。上游 v0.2.9 现状如下；
❌/⚠️ 项按 §3.2 三条出路处理（默认能力位降级 503）。

| qmt-bridge API 组 | 涉及方法 | 远端 RPC 对应 | 状态 |
|---|---|---|---|
| 委托 | `order_stock`(_async)、`cancel_order_stock`(_async)、`cancel_order_stock_sysid`(_async) | `submit_order` / `cancel_order`（受 `rpc_allow_order_methods` 门控，默认关） | ✅（sysid 变体需实测） |
| 查询 | `query_stock_asset` / `query_stock_positions` / `query_stock_orders` / `query_stock_trades`、单笔委托/成交/持仓查询 | `get_asset` / `get_positions` / `query_orders` / `query_trades`（单笔查询为服务端本地遍历，无需远端支持） | ✅ |
| 信用（两融） | `query_credit_detail`、`query_stk_compacts`、`query_credit_slo_code`、`query_credit_subjects`、`query_credit_assure` | margin 系列方法 | ⚠️ 需两融权限，否则返回空 |
| 账户 | `query_account_status`、`query_account_infos` | 单账户模型 | ⚠️ 单实例单账户，语义降级（已实现：`account` 能力位） |
| 账户（子账户） | `query_secu_account` | 无远端对应 | ❌ 降级 503（独立 `secu_account` 能力位，可推动上游） |
| 银证转账 | `bank_transfer_in` / `bank_transfer_out`(_async)、`query_bank_info` / `query_bank_amount` / `query_bank_transfer_stream` | 无 | ❌ 降级 503 / 推动上游 |
| 资金/证券划转 | `fund_transfer`、`secu_transfer` | 无 | ❌ 降级 503 / 推动上游 |
| CTP 划转 | `ctp_transfer_option_to_future`、`ctp_transfer_future_to_option` | 无 | ❌ 降级 503 |
| SMT 约定式交易 | `smt_query_quoter`、`smt_query_compact`、`smt_query_order`、`smt_negotiate_order_async`、`smt_appointment_order_async`、`smt_appointment_cancel_async`、`smt_compact_renewal_async`、`smt_compact_return_async` | 无 | ❌ 降级 503 |
| IPO 打新 | `query_ipo_data`、`query_new_purchase_limit` | 无（上游明示"不能直接视为无损替换"） | ❌ 降级 503 |
| COM 期权/期货 | `query_com_fund`、`query_com_position` | 无（完整版 QMT 为证券客户端） | ❌ 降级 503 |
| 数据导出 | `export_data`、`query_data`、`sync_transaction_from_external` | 无直接对应（`get_history_trade_detail_data` 仅可部分替代成交导出，需自行封装） | ❌ 降级 503 / 推动上游 |
| 行情（xtdata） | K线、板块、财务、下载、`get_full_tick`、`subscribe_whole_quote` 等 | 117 个只读白名单方法 + FormulaServer 快路径 | ✅/⚠️ 见下 |

### 行情侧细节

| 状态 | 接口 | 说明 |
|------|------|------|
| ✅ | `get_full_tick`、`subscribe_whole_quote` | `subscribe_whole_quote` 为服务端真实增量推送（心跳 + 自动恢复）；`get_full_tick` 走 FormulaServer（58600）快路径 |
| ⚠️ 部分 | `get_market_data_ex` / `get_local_data` | 回退到 `get_market_data` |
| ⚠️ 部分 | 复权 K 线 | 需服务端先下载原始数据（有自动下载自愈） |
| ⚠️ 部分 | `subscribe_quote`（按标的订阅） | 写意图到 Redis + 单次快照推送；连续推送以 `subscribe_whole_quote` 为准，`server/ws/` 需审计 |

## 5. 约束与风险

1. **实盘模式 only**：完整版 QMT 模拟模式下委托不进真实队列（下单返回 -1、查询为空）。
   模拟需求全部走 `server/paper_trading/` 本地引擎。
2. **单实例单账户**：一个完整版 QMT 客户端 = 一个 live 账户；
   多账户 = 多 QMT 实例 + 多 bridge 配置。
3. **下单默认关闭**：QMT 侧配置 `rpc_allow_order_methods: False`（默认），
   需显式开启——天然的灰度开关。
4. **GIL 抖动**：ZMQ transport 约 30% 请求出现 ~500ms GIL 尖峰，
   **默认用 Redis**（p50 ≈ 13ms）；只读行情走 FormulaServer（58600）不受影响。
5. **异步语义**：async 响应与事件走不同通道，事件先于响应属正常；
   compat 层用 `order_remark` 栅栏 + 10s 超时仲裁。
6. **回调推送**：见 §3.6，live WS 推送链路需实测验证。

## 6. 实施与灰度步骤

1. **后端抽象 PR**（可脱离 QMT 环境用 mock 测试）：
   `TraderBackend` 协议 + `BigQmtAdapter` + 能力位降级 + CLI 开关
   ✅ 已完成（2026-08-26）：`server/trading/{backend,mini_backend,bigqmt_backend,manager}.py`、
   `server/bigqmt_shim.py`、`scripts/deploy_bigqmt_server.py`、
   `pyproject.toml` `bigqmt` extra、`tests/test_trading_backends.py`
2. **QMT 侧部署**：安装 xtquant_big_convert 服务端，**下单保持关闭**；
   `MiniQmtBackend` 与 `BigQmtAdapter` 并行运行
3. **字段对比**：资产/持仓/委托/成交在双后端间逐字段比对
   （qmt-bridge 的路由天然是比较工具），
   并逐方法实测 §4 映射表中 ✅/⚠️ 项的远端行为
4. **开启下单灰度**：`rpc_allow_order_methods: True`，小额验证
5. **切换**：`--trader-backend bigqmt` + xtdata shim 上线，
   mini 后端保留至券商关停

## 7. 参考资料

- [xtquant_big_convert（GitHub，MIT）](https://github.com/litaolemo/xtquant_big_convert)
  — 官方 README、`docs/XTQUANT_COMPAT_REPLACEMENT.md`、`docs/RPC_API_REFERENCE.md`
- [miniQMT 停用事实核查与应对指南（wtsolutions）](https://invest.wtsolutions.cn/posts/miniqmt-termination)
- [QMT 常见问题 QA（miniqmt.com）](https://miniqmt.com/pages/qa/knowledge-qa.html)
- [2026 券商 miniQMT 支持清单](https://blog.cifangquant.com/post/111.html)
- PyPI: `xtquant-big-convert`（客户端 Py≥3.8；服务端 QMT 内置 Py3.6）
