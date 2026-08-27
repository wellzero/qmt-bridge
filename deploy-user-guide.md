# bigqmt 后端部署与使用指南（QMT 侧 xtquant_big_convert）

> 适用范围：`big-qmt` 分支 §6 步骤 2 —— 把 `xtquant_big_convert` 服务端部署进
> 完整版 QMT（big QMT）内置 Python，**下单保持关闭**，与 mini 后端并行运行，
> 为步骤 3（双后端字段比对）做准备。
>
> 设计文档：`docs/big-qmt.md`；阅读伴侣：`docs/big-qmt-work-flow.md`。
> 本文以 `C:\QMT_Simulator`（模拟账户 `88002471`）为例，其它 QMT 客户端同理。

---

## 1. 架构与数据流

```
策略/Linux 侧                Windows qmt-server (py312)              完整版 QMT 客户端
────────────                ─────────────────────────              ──────────────────
REST/WS 调用  ──────────►  qmt-bridge (qmt-server)
                             │  --trader-backend bigqmt
                             ├─ MiniQmtBackend ──► miniQMT (原路径, 并行)
                             └─ BigQmtAdapter ──► xtquant_big_convert 客户端
                                                   │ Redis RPC (127.0.0.1:6379 db5)
                                                   ▼
                                              QMT 策略沙箱内的 RPC 服务端
                                              (bigqmt_signal_trader,
                                               rpc_allow_order_methods=False)
```

- 交易 RPC：资产/持仓/委托/成交查询走 Redis list queue；
  `submit_order`/`cancel_order` 被 `rpc_allow_order_methods=False` **门控拒绝**。
- 行情 shim：`--trader-backend bigqmt` 时 `xtquant.xtdata` 代理到同一 RPC
  （需 `QMT_BRIDGE_BIGQMT_SHIM_DIR`，见 §5.1）。

---

## 2. 前提条件

| 组件 | 要求 | 本机现状（2026-08-27 已验证） |
|---|---|---|
| 完整版 QMT 客户端 | 已登录、内置 Python 3.6 | `C:\QMT_Simulator`，账户 88002471，Py 3.6.8 x64 |
| qmt-server Python | ≥3.10，装 `qmt-bridge[bigqmt]` | `C:\...\Python312`，xtquant-big-convert 0.2.9 + redis-py 8.1 |
| Redis 服务端 | 任意 5.x/6.x/7.x，本机或可达 | `C:\Users\Docker\bigqmt\redis\`（tporadowski 5.0.14.1） |
| 网络 | qmt-server ↔ Redis ↔ QMT 同机或互通 | 全部 127.0.0.1 |

> 本机 pip 必须走 TUNA 镜像（PyPI CDN 被墙）：
> `py -m pip install --user <pkg> -i https://mirrors.tuna.tsinghua.edu.cn/pypi/web/simple`

---

## 3. 部署步骤

### 3.1 qmt-server 侧安装（部署机，一次性）

```powershell
# 在仓库根（或 Desktop\Shared\qmt-bridge-big-qmt 本地junction）
py -m pip install --user -e ".[bigqmt]" -i https://mirrors.tuna.tsinghua.edu.cn/pypi/web/simple
# bigqmt extra = xtquant-big-convert[redis]>=0.2.9（自带客户端 + xtquant import shim）
```

> 注意：该 wheel 会向用户 site-packages 顶层放一个 `xtquant\` import shim，
> 可能遮蔽真实 xtquant。测试套件已封闭化（commit `9fdcc50`），不受影响；
> qmt-server 启动时用 `QMT_BRIDGE_BIGQMT_SHIM_DIR` 显式指定专用 shim 目录（§5.1）。

### 3.2 Redis 服务端（部署机，一次性）

无 conda-forge win64 原生包；推荐 tporadowski/Redis（5.0.14.1，绿色免安装）：

```bash
# 走 api.github.com release 资产通道（本机 github.com 直连被墙）：
curl -sL -H "Accept: application/octet-stream" -o /tmp/Redis-x64-5.0.14.1.zip \
  "https://api.github.com/repos/tporadowski/redis/releases/assets/<asset_id>"
# （asset_id 从 releases/latest 的 JSON 里查；zip ≈12.6MB，约 14KB/s）
py -c "import zipfile; zipfile.ZipFile(r'<zip路径>').testzip(); zipfile.ZipFile(r'<zip路径>').extractall(r'C:\Users\Docker\bigqmt\redis')"
```

启动（ detached，开机需手动/计划任务再拉起）：

```powershell
Start-Process -FilePath 'C:\Users\Docker\bigqmt\redis\redis-server.exe' `
  -ArgumentList 'C:\Users\Docker\bigqmt\redis\redis.windows.conf','--dir','C:\Users\Docker\bigqmt\redis' `
  -WindowStyle Hidden `
  -RedirectStandardOutput 'C:\Users\Docker\bigqmt\redis\server-stdout.log' `
  -RedirectStandardError  'C:\Users\Docker\bigqmt\redis\server-stderr.log'

# 验证（配置默认 bind 127.0.0.1, protected-mode yes, port 6379）：
C:\Users\Docker\bigqmt\redis\redis-cli.exe ping   # → PONG
```

> Redis 5.0 只支持 RESP2。客户端兼容层默认 `protocol=2`（redis-py 8.x 才默认
> RESP3），**无需**额外配置。

### 3.3 部署 QMT 侧服务端文件

```powershell
py scripts\deploy_bigqmt_server.py `
  --qmt-python-dir "C:\QMT_Simulator\python" `
  --account-id 88002471 `
  --redis-host 127.0.0.1 --redis-port 6379 --redis-db 5 `
  --shim-out "C:\Users\Docker\bigqmt\shim"
```

脚本行为（先 `--dry-run` 可预览）：

- 复制 `bigqmt_signal_trader\` 包 + 6 个顶层入口（`BIGQMT_REDIS_DRYRUN.py`、
  `bigqmt_signal_trader_strategy.py`、`bigqmt_signal_trader_redis_rpc_runtime.py` 等）
  到 QMT `python\` 目录；
- 生成 `bigqmt_signal_trader_local_config.py`（**已存在则绝不覆盖**）：
  账户 ID + Redis 配置 + `rpc_allow_order_methods: False`（下单门控，默认关）；
- 导出 xtquant import shim 到 `--shim-out`（供 qmt-server 用，不进 QMT 目录）。

### 3.4 QMT 内置 Python 依赖（redis-py 3.5.3）

QMT 内置 Python 3.6 只需 Redis transport 的纯 Python 依赖：

```powershell
py -m pip download --no-deps redis==3.5.3 -d C:\temp\redis35 `
  -i https://mirrors.tuna.tsinghua.edu.cn/pypi/web/simple
py -m zipfile -e C:\temp\redis35\redis-3.5.3-py2.py3-none-any.whl C:\temp\redis35x\
Copy-Item -Recurse C:\temp\redis35x\redis C:\QMT_Simulator\python\
```

用 QMT 自带解释器验证（`pythonw.exe` 无控制台，写文件看结果）：

```powershell
& C:\QMT_Simulator\bin.x64\pythonw.exe C:\temp\py36_check.py   # 脚本内:
#   import sys; sys.path.insert(0, r'C:\QMT_Simulator\python')
#   import redis, bigqmt_signal_trader  → 写日志确认 OK
```

### 3.5 在 QMT 客户端里注册策略（**必须手动，UI 操作**）

> QMT 的策略列表来自内部索引：直接把 `.py` 丢进 `python\` 目录**不会**出现；
> 且既有策略文件被 ACL 锁定（Users 只读）并加密存储，不能手改。
> 唯一正规入口是策略编辑器保存（保存时自动加密并写索引）。

在 QMT 客户端（模拟端）操作：

1. 左侧导航 **模型研究** → 工具栏 **+新建策略** → **Python策略**，
   打开策略编辑器。
2. 编辑器名称框随意（如 `bigqmt_rpc_bootstrap`）；代码区全选后粘贴
   `C:\QMT_Simulator\python\bigqmt_rpc_bootstrap.py` 的内容（随 §3.3 部署）。
   该 bootstrap 只做三件事：导入 `bigqmt_signal_trader_redis_rpc_runtime`、
   绑定沙箱内置 `passorder`/`cancel`/`get_trade_detail_data`、
   暴露 `init/handlebar/adjust` 回调。
3. **保存**（软盘图标）。若弹出文件保存框：文件名填
   `..\python\bigqmt_rpc_bootstrap.py`；对话框有「如果文件存在,自动重命名」
   复选框 —— 保持勾选即可（生成 `*(1).py` 也能用，策略以编辑器名称注册）。
4. 依次点 **编译** → 确认无报错。

脚本内容要求（上游 runbook 约定）：保持 **ASCII**、首行 `#coding:gbk`、
不要出现中文注释。

### 3.6 创建模型交易实例并启动（**必须手动，UI 操作**）

1. 左侧导航 **模型交易** → 点击策略卡片（或 **新建策略交易**）。
2. 对话框里：
   - **策略类型** = `bigqmt_rpc_bootstrap`（§3.5 注册的）；
   - **主图代码** = `000300`（或任意流动性好的标的）；
   - **账号类型** = 股票；**资金账号** = `88002471`；
   - 参数表（网格类参数）会被 bootstrap 忽略，无需填写；
   - 可勾选 **终端启动后自动运行**（QMT 重启后实例自启）。
3. **确定** → 在实例列表上右键 → **运行模式：模拟信号**（非实盘！）→ 启动。

> ⚠️ 绝对不要勾选「启动本地 python」：那会把脚本当独立进程跑，
> `init/handlebar` 不会被 QMT 回调驱动，RPC 服务不会工作。

---

## 4. 部署验证

### 4.1 QMT 侧日志

实例启动后，检查 `C:\QMT_Simulator\python\logs\bigqmt.log`，应出现：

```
[bigqmt_rpc] transport=redis mode process_in_listener=... allow_order_methods=False ...
```

`allow_order_methods=False` 是**下单门控关闭**的确认行。没有该行 =
策略没被模型交易真正驱动（检查「启动本地 python」是否误勾、账户是否绑定）。

### 4.2 客户端连通性（qmt-server 侧）

```powershell
py C:\Users\Docker\bigqmt\verify_rpc.py
```

脚本逻辑（环境变量 `BIGQMT_ACCOUNT_ID=88002471` 等已内置默认）：

1. `compat.configure()` + `xt_trader.connect()` —— 内部即 RPC `ping`；
2. `query_stock_asset / positions / orders / trades` 四组查询；
3. 尝试 `order_stock(...)` —— **预期被拒绝**（门控关闭），报错即通过。

### 4.3 Redis 侧

```powershell
C:\Users\Docker\bigqmt\redis\redis-cli.exe -n 5 keys "bigqmt:*"
# 请求/响应队列形如 bigqmt:rpc:req:88002471 / bigqmt:rpc:resp:...
```

---

## 5. 使用

### 5.1 启动 bigqmt 后端的 qmt-server

```powershell
$env:QMT_BRIDGE_BIGQMT_SHIM_DIR = "C:\Users\Docker\bigqmt\shim"   # xtdata shim 专用目录
$env:BIGQMT_ACCOUNT_ID    = "88002471"
$env:BIGQMT_REDIS_HOST    = "127.0.0.1"; $env:BIGQMT_REDIS_PORT = "6379"; $env:BIGQMT_REDIS_DB = "5"
qmt-server --trader-backend bigqmt            # 或： just serve-bigqmt
```

- 端口/参数与原 `qmt-server` 完全一致（默认 18888）；策略侧 REST/WS API
  **零改动**（对外以 qmt-bridge API 为准）。
- 调度器同理：`QMT_BRIDGE_TRADER_BACKEND=bigqmt qmt-scheduler`。
- 客户端兼容层读 `bigqmt_signal_trader_local_config` 模块，或上述
  `BIGQMT_*` 环境变量（环境变量优先级足够，qmt-server 侧无需配置文件）。

### 5.2 双后端并行（§6 步骤 3 的用法）

mini 后端不受影响：`qmt-server`（默认 mini）与 `--trader-backend bigqmt`
**各起一个进程**（注意端口错开，如 18888 / 18889），策略侧对同一账户
分别请求资产/持仓/委托/成交，逐字段比对。qmt-bridge 的路由天然是比较工具。

### 5.3 能力位降级（哪些 API 会 503）

bigqmt 后端当前支持：委托（下单门控）、查询、信用、账户 四组。
以下组返回 503 + 「等待远端支持」错误信息（`docs/big-qmt.md` §4）：

`secu_account`（子账户）、`bank`（银证转账）、`transfer`（资金/证券划转）、
`ctp`、`smt`（约定式交易）、`ipo`（打新）、`com`、`export`。

---

## 6. 日常运维

| 操作 | 命令/入口 |
|---|---|
| Redis 状态 / 停止 | `redis-cli ping`；`Stop-Process -Name redis-server` |
| QMT 重启后恢复 | 勾选过「终端启动后自动运行」的实例会自启；RPC 随 `init` 拉起 |
| 看 QMT 侧日志 | `C:\QMT_Simulator\python\logs\bigqmt.log`（按天轮转，保留 7 天） |
| RPC 超时调大 | QMT 侧 `local_config` 里 `BIGQMT_RPC_TIMEOUT_SECONDS = 6.0`（下载大窗口数据时调大） |
| 全市场 tick 降载 | `local_config` 里 `full_tick_cache_enabled=True`（默认关） |

**开启下单灰度（§6 步骤 4，字段比对通过之后才做）**：

1. 编辑 `C:\QMT_Simulator\python\bigqmt_signal_trader_local_config.py`：
   `"rpc_allow_order_methods": True`；
2. 重启模型交易实例（QMT 内停止再启动）；
3. 小额验证一笔，观察 `bigqmt.log` 与 Redis `exec_events` 事件流。

---

## 7. 故障排查

| 症状 | 原因 | 处理 |
|---|---|---|
| `connect()` 超时/ping 无响应 | Redis 未起 / 策略实例没运行 | `redis-cli ping`；看 §4.1 日志行是否存在 |
| 日志无 `[bigqmt_rpc]` 行 | 勾了「启动本地 python」或实例未绑账户 | 按 §3.6 重挂实例 |
| RPC 通但查询报错 | 账户 ID 不一致 | QMT 侧 `local_config` 的 `BIGQMT_ACCOUNT_ID` 与客户端 env `BIGQMT_ACCOUNT_ID` 必须同为 `88002471` |
| 行情回落真实 xtquant | 未设 `QMT_BRIDGE_BIGQMT_SHIM_DIR` | §5.1 设置后重启 qmt-server（必须在任何 xtquant 导入前） |
| QMT 内置 Python 报 `No module named redis` | §3.4 未做/目录错 | 确认 `C:\QMT_Simulator\python\redis\__init__.py` 存在 |
| `Sensitive Data Detected`（QMT 日志） | 旧版 JSON 带股票代码触发沙箱过滤 | v0.2.9 已做安全编码；若出现请升级 wheel |
| 策略列表里看不到新文件 | 列表来自内部索引，非目录扫描 | 只能经编辑器保存注册（§3.5） |
| 手改 `python\` 下既有策略 `.py` 报拒绝访问 | QMT 安装文件 ACL=Users 只读且加密 | 不要手改；走编辑器 |

---

## 8. 卸载 / 回滚

1. QMT 客户端：模型交易里停止并删除实例；模型研究里删除注册的策略。
2. 删除部署文件（均为我们创建，可删）：
   `python\bigqmt_signal_trader\`、`python\redis\`、6 个顶层 `bigqmt*.py` /
   `BIGQMT_REDIS_DRYRUN.py`、`python\bigqmt_signal_trader_local_config.py`、
   `python\logs\`。**既有策略文件（网格策略.py 等）从未被修改，无需恢复。**
3. 停 Redis：`Stop-Process -Name redis-server`；删除 `C:\Users\Docker\bigqmt\redis\`。
4. qmt-server 侧：`py -m pip uninstall xtquant-big-convert`（可选）。
5. 仓库侧无需回滚（commit `9fdcc50` 只是测试封闭化，保留有益）。

---

*本文基于 2026-08-27 在 `C:\QMT_Simulator` 上的实际部署验证编写；
未验证项（客户端重启后目录新文件是否入索引等）已在文中标注。*
