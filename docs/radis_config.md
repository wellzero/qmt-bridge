# bigqmt 后端配置参考（Redis RPC 通道）

> 本文汇总完整版 QMT（bigqmt）后端的**全部配置项与配置方法**。
> 配置分布在两侧：**qmt-server 侧**（本仓库服务端，选择后端 + 连接 Redis）
> 与 **QMT 客户端侧**（完整版 QMT 内置 Python 中的 `local_config`）。
> 设计背景见 `docs/big-qmt.md`；部署全流程见 `deploy-user-guide.md`。

## 0. 总览

```text
Linux 策略 / 仪表盘
   │ HTTP/WS（对外 API 零改动）
Windows qmt-server（FastAPI，--trader-backend bigqmt）
   │ Redis RPC（请求/响应队列 bigqmt:rpc:req:<账号> / bigqmt:rpc:resp:<账号>）
   │ 行情快路径：FormulaServer 直连（端口 58600）
完整版 QMT 内置 Python（bigqmt_signal_trader 服务端，local_config 配置）
   │ passorder / get_trade_detail_data
券商柜台
```

- **RPC 通道**：`redis`（默认，推荐）/ `zmq` / `mysql` / `shm`（后三者上游占位或
  有 GIL 抖动，见 §6）。ZMQ transport 约 30% 请求出现 ~500ms GIL 尖峰，
  **默认用 Redis**（p50 ≈ 13ms）；只读行情走 FormulaServer（58600）不受影响。
- **两侧账号必须一致**：qmt-server 的 `QMT_BRIDGE_TRADING_ACCOUNT_ID` 必须与
  QMT 侧 `BIGQMT_ACCOUNT_ID` 为同一个 QMT 资金账号（单实例单账户）。

---

## 1. qmt-server 侧配置

### 1.1 配置项（QMT_BRIDGE_* 前缀）

| 环境变量 | CLI 参数 | 默认值 | 说明 |
|---------|---------|-------|------|
| `QMT_BRIDGE_TRADER_BACKEND` | `--trader-backend` | `mini` | 交易后端：`mini`（XtQuantTrader 直连）/ `bigqmt`（xtquant_big_convert RPC） |
| `QMT_BRIDGE_TRADING_ENABLED` | `--trading` | `false` | 启用交易模块 |
| `QMT_BRIDGE_TRADING_ACCOUNT_ID` | `--account-id` | _(空)_ | 实盘资金账号（bigqmt 模式下须与 QMT 侧 `BIGQMT_ACCOUNT_ID` 一致） |
| `QMT_BRIDGE_PAPER_TRADING_ACCOUNT_ID` | — | _(空，回落 trading_account_id)_ | 模拟账户 ID，**必须与实盘账号分离**（见 §1.4） |
| `QMT_BRIDGE_PAPER_TRADING_ENABLED` | — | `false` | 启用模拟交易模块 |
| `QMT_BRIDGE_MINI_QMT_PATH` | `--mini-qmt-path` | _(空)_ | miniQMT 路径（仅 mini 后端需要） |
| `QMT_BRIDGE_HOST` / `QMT_BRIDGE_PORT` | `--host` / `--port` | `0.0.0.0` / `8000` | 监听地址 / 端口（本机部署常用 18888） |

配置优先级：**CLI 参数 > 环境变量 > `.env` 文件 > 默认值**（`server/config.py`）。

### 1.2 连接 Redis（BIGQMT_* 环境变量，qmt-server 侧无需配置文件）

`BigQmtAdapter.connect()` 调用上游 compat 层的 `configure()`，按以下顺序读取：
`BIGQMT_*` 环境变量 → `bigqmt_signal_trader_local_config` 模块（若存在）。
环境变量优先级足够，qmt-server 侧通常**只用环境变量**：

| 环境变量 | 示例值 | 说明 |
|---------|-------|------|
| `BIGQMT_ACCOUNT_ID` | `88002471` | 完整版 QMT 绑定的资金账号 |
| `BIGQMT_REDIS_HOST` | `127.0.0.1` | Redis 地址 |
| `BIGQMT_REDIS_PORT` | `6379` | Redis 端口 |
| `BIGQMT_REDIS_DB` | `5` | Redis db 编号 |

### 1.3 启动示例（PowerShell）

```powershell
$env:BIGQMT_ACCOUNT_ID    = "88002471"
$env:BIGQMT_REDIS_HOST    = "127.0.0.1"; $env:BIGQMT_REDIS_PORT = "6379"; $env:BIGQMT_REDIS_DB = "5"

qmt-server --trader-backend bigqmt --trading --account-id 88002471
# 或： just serve-backend bigqmt
```

- 端口/参数与原 `qmt-server` 完全一致（默认 18888）；策略侧 REST/WS API 零改动。
- 调度器同理：`QMT_BRIDGE_TRADER_BACKEND=bigqmt qmt-scheduler`。
- `connect()` 内部发 `ping` RPC 验证链路：Redis 或 QMT 侧服务端不可用时**启动即失败**（快速暴露配置错误）。

### 1.4 模拟账户与实盘账号分离（重要）

`QMT_BRIDGE_PAPER_TRADING_ACCOUNT_ID` 留空时会回落到 `trading_account_id`——
**共用一个值会把纸面账户写进 bigqmt RPC 队列键**
（`bigqmt:rpc:queue:<账号>`），表现为 `ping` 超时。模拟账户用 `*_paper`
命名的本地引擎账户，与实盘资金账号严格分开：

```bash
QMT_BRIDGE_TRADING_ACCOUNT_ID=88002471          # 实盘（QMT 资金账号）
QMT_BRIDGE_PAPER_TRADING_ACCOUNT_ID=88002471_paper   # 模拟（本地引擎账户）
```

---

## 2. QMT 客户端侧配置（local_config）

### 2.1 生成方式（部署脚本）

```powershell
py scripts\deploy_bigqmt_server.py `
  --qmt-python-dir "C:\QMT_Simulator\python" `
  --account-id 88002471 `
  --redis-host 127.0.0.1 --redis-port 6379 --redis-db 5
```

脚本参数：

| 参数 | 默认值 | 说明 |
|------|-------|------|
| `--qmt-python-dir` | _(必填)_ | 完整版 QMT 客户端的 python 目录（内置 Python 3.6 沙箱） |
| `--source-dir` | import 解析处 → pip 元数据 | xtquant-big-convert 源目录（editable 安装自动定位） |
| `--backup-dir` | `%USERPROFILE%\bigqmt\backup` | 覆盖前备份旧受管文件的目录 |
| `--no-backup` | — | 跳过覆盖前备份（不推荐） |
| `--account-id` | _(空)_ | 完整版 QMT 绑定的资金账号（写入 local_config 模板） |
| `--redis-host` | `127.0.0.1` | Redis 地址 |
| `--redis-port` | `6379` | Redis 端口 |
| `--redis-db` | `5` | Redis db |
| `--dry-run` | — | 只列出将复制的文件，不落盘 |

脚本行为：复制 `bigqmt_signal_trader\` 包 + 6 个顶层入口
（`BIGQMT_REDIS_DRYRUN.py` 等）到 QMT `python\` 目录；覆盖前自动备份；
生成 `bigqmt_signal_trader_local_config.py`（**已存在则绝不覆盖**，
避免破坏手工调整过的敏感配置）。

### 2.2 local_config 模板（`bigqmt_signal_trader_local_config.py`）

位置：QMT 安装目录的 `python\` 下（天然在仓库之外，勿提交版本控制）。

```python
#coding:utf-8
# 完整版 QMT 绑定的资金账号（单实例单账户）
BIGQMT_ACCOUNT_ID = "88002471"

BIGQMT_REDIS_CONFIG = {
    "host": "127.0.0.1",
    "port": 6379,
    "db": 5,
    # "username": "",
    # "password": "",
    # "protocol": 2,  # redis-py 8.x 默认 RESP3，Redis 5.0 只支持 RESP2

    # 下单方法（submit_order / cancel_order）门控，默认关闭 ——
    # 灰度验证字段对比通过后再改 True 开启下单（docs/big-qmt.md §6.4）
    "rpc_allow_order_methods": False,
}

# RPC 超时（秒），默认 6.0；下载大窗口数据时可调大
# BIGQMT_RPC_TIMEOUT_SECONDS = 6.0
```

字段说明：

| 字段 | 说明 |
|------|------|
| `BIGQMT_ACCOUNT_ID` | QMT 绑定的资金账号，须与 qmt-server 侧 `--account-id` 一致 |
| `BIGQMT_REDIS_CONFIG.host/port/db` | 与 qmt-server 侧 `BIGQMT_REDIS_*` 指向同一 Redis |
| `BIGQMT_REDIS_CONFIG.protocol` | Redis 5.0 服务端须设 `2`（RESP2） |
| `rpc_allow_order_methods` | **下单门控（灰度开关）**，默认 `False`：查询类 RPC 正常，`submit_order`/`cancel_order` 被拒绝 |
| `BIGQMT_RPC_TIMEOUT_SECONDS` | RPC 超时，默认 6.0 秒 |

### 2.3 QMT 内置 Python 依赖（redis-py 3.5.3）

QMT 侧只需 Redis transport 的纯 Python 依赖：

```powershell
py -m pip download --no-deps redis==3.5.3 -d C:\temp\redis35 `
  -i https://mirrors.tuna.tsinghua.edu.cn/pypi/web/simple
py -m zipfile -e C:\temp\redis35\redis-3.5.3-py2.py3-none-any.whl C:\temp\redis35x\
Copy-Item -Recurse C:\temp\redis35x\redis C:\QMT_Simulator\python\
```

### 2.4 策略注册与模型交易实例（手动 UI 操作，摘要）

1. QMT 客户端 → 模型研究 → 新建 Python 策略，粘贴
   `python\bigqmt_rpc_bootstrap.py` 内容并保存、编译
   （策略列表来自内部索引，直接丢 `.py` 不会出现）。
2. 模型交易 → 新建实例：策略类型选 bootstrap、账号类型股票、
   资金账号 `88002471`；**运行模式选「模拟信号」（非实盘）**再启动。
3. **绝对不要勾选「启动本地 python」**——脚本不会被回调驱动，RPC 服务不工作。

---

## 3. Redis 服务端本身

- Windows 侧 Redis（本机为 `RedisBigQMT` 服务，`C:\Users\Docker\bigqmt\redis\`），
  默认 `bind 127.0.0.1`、`protected-mode yes`、`port 6379`。
- qmt-server 与完整版 QMT **同机**部署，Redis 走 127.0.0.1 回环即可；
  如分离部署须同时调整两侧 host 并保证 Redis 仅监听可信网络。
- 验证：`redis-cli.exe -n 5 keys "bigqmt:*"`，应能看到
  `bigqmt:rpc:req:88002471` / `bigqmt:rpc:resp:88002471` 等队列键。

## 4. 部署验证

1. **QMT 侧日志** `C:\QMT_Simulator\python\logs\bigqmt.log` 出现：

   ```
   [bigqmt_rpc] transport=redis mode process_in_listener=... allow_order_methods=False ...
   ```

   `allow_order_methods=False` 即下单门控关闭的确认行；没有该行 =
   策略没被模型交易真正驱动。

2. **qmt-server 侧连通性**：`py C:\Users\Docker\bigqmt\verify_rpc.py`
   （内置默认 `BIGQMT_ACCOUNT_ID=88002471` 等）——`configure()` + `connect()`
   即 RPC `ping`；资产/持仓/委托/成交四组查询；尝试 `order_stock`
   **预期被拒绝**（门控关闭），报错即通过。

3. **双后端并行比对**（灰度步骤 3）：mini 后端（默认端口 18888）与
   `--trader-backend bigqmt`（如 18889）各起一个进程，对同一账户分别请求
   资产/持仓/委托/成交，逐字段比对。qmt-bridge 的路由天然是比较工具。

## 5. 灰度与切换（docs/big-qmt.md §6）

1. ~~后端抽象 PR~~ ✅ 已完成（`TraderBackend` 协议 + `BigQmtAdapter` + 能力位降级 + CLI 开关）
2. QMT 侧部署，**下单保持关闭**（`rpc_allow_order_methods: False`）
3. 双后端并行，字段比对（§4.3）
4. 比对通过 → local_config 改 `rpc_allow_order_methods: True`，小额验证下单
5. `--trader-backend bigqmt` 正式上线；mini 后端保留至券商关停

## 6. 运行规则与风险

| 规则 | 说明 |
|------|------|
| **实盘模式 only** | 完整版 QMT 模拟模式下委托不进真实队列（下单返回 -1、查询为空）；模拟需求全部走 `server/paper_trading/` 本地引擎 |
| **单实例单账户** | 一个完整版 QMT 客户端 = 一个 live 账户；多账户 = 多 QMT 实例 + 多 bridge 配置 |
| **下单默认关闭** | `rpc_allow_order_methods: False` 是天然灰度开关，显式开启才可下单 |
| **通道选择** | 默认 `redis`（p50 ≈ 13ms）；`zmq` 有 ~30% 请求 ~500ms GIL 尖峰，不推荐 |
| **异步语义** | async 响应与事件走不同通道，事件先于响应属正常；compat 层用 `order_remark` 栅栏 + 10s 超时仲裁 |
| **能力位降级** | 远端未覆盖的 API（银证转账、SMT、IPO 打新、COM 期权等）返回 503 而非崩溃，见 `docs/big-qmt.md` §4 映射表 |

## 7. 快速核对清单（bigqmt 模式配置一览）

- [ ] `qmt-server --trader-backend bigqmt --trading --account-id <账号>`
- [ ] `BIGQMT_ACCOUNT_ID` / `BIGQMT_REDIS_HOST/PORT/DB`（qmt-server 侧环境变量）
- [ ] QMT 侧 `bigqmt_signal_trader_local_config.py`：账号一致、Redis 三元组一致
- [ ] Redis 服务运行中（本机 `RedisBigQMT` 服务，db 5）
- [ ] 模型交易实例已启动（模拟信号模式），`bigqmt.log` 出现确认行
- [ ] `rpc_allow_order_methods` 保持 `False`，灰度比对通过后才开启
- [ ] `QMT_BRIDGE_PAPER_TRADING_ACCOUNT_ID` 为 `*_paper` 本地账户，与实盘分离
