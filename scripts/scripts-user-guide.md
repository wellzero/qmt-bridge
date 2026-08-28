# bigqmt 启动脚本使用手册

> 适用脚本（均在本目录）：
> - `serve_qmt_server.ps1` —— qmt-server 启动器（mini / bigqmt 双后端一键切换）
> - `start_bigqmt_service.ps1` —— bigqmt 全链路一键拉起（Redis + QMT 客户端 + qmt-server）
> - `download_bigqmt_data.py` —— **下载历史行情到完整版 QMT 数据仓**（mini 通道下载 + 同步，bigqmt 后端立即可读）
> - 相关：`deploy_bigqmt_windows.ps1` —— 首次部署（装依赖/Redis/QMT 侧文件），见根目录
>   `deploy-user-guide.md`
>
> 实测环境：QMT_Simulator（C:\QMT_Simulator，账号 88002471）、Redis 5.0.14.1
> （~\bigqmt\redis）、Windows Server 2022 + PowerShell 5.1。2026-08-28 全流程验证。

---

## 0. 下载行情到 big QMT（`download_bigqmt_data.py`）

```powershell
just download-bigqmt --stocks 000300.SH,600519.SH --periods 1d,1m --start 20250101
# 或直接：
py scripts\download_bigqmt_data.py --stocks 000300.SH --periods 1d
```

原理（2026-08-28 实测打通）：下载由 **QMT 本体**完成 —— 脚本调用
`bin.x64\pythonw.exe`（QMT 内置解释器）+ 内置 xtquant 下载（实测推翻了
上游"完整版终端 xtdata 下载不可达"的说法）。xtquant 写入的是安装的数据
服务仓 `userdata_mini\datadir`，而 FormulaServer 只读客户端仓 `datadir`
（两仓同属一个安装、格式一致），脚本第二步把文件同步过去即即刻可读。

- 前提：QMT 客户端在运行；脚本本体可用任意 Python 跑（下载动作在 QMT
  内置解释器里执行，不依赖外部 xtquant）。
- 周期支持：`1m/5m/15m/30m/1h/1d`（tick 文件命名不同，暂不支持）。
- 兼容性：内置 xtquant 为 Py3.6 老版本，无 `incrementally` 参数 —— 脚本
  已自动退化处理。
- 读回验证：
  `curl "http://127.0.0.1:18888/api/market/market_data_ex?stocks=000300.SH&period=1d&count=5"`
- 常用变体：`--sync-only`（只同步不下载）、`--verify-only`（mini 读回验证）、
  `--qmt-root` 指向其它 QMT 安装。

---

## 1. 三个脚本的关系（一张图）

```
首次/重装（一次性）          日常启动（每次开机后）              日常使用
─────────────────          ─────────────────────           ──────────────
deploy_bigqmt_windows.ps1   start_bigqmt_service.ps1        策略/程序访问
  ├ 装客户端依赖              ① Redis 没起 → 自动拉起          http://<host>:18888
  ├ 装/启动 Redis             ② QMT 客户端没跑 → 自动启动         /api/market/*
  ├ 拷服务端文件进 QMT        ③ 调 serve_qmt_server.ps1          /api/sector/*
  │  （备份旧文件 + 版本留痕）  （bigqmt 后端 + 四项预检）         /api/trading/*
  └ QMT UI 挂策略（手工）                                        ... ...
```

只想换后端/调参数、链路环境已就绪时，可跳过编排直接用 `serve_qmt_server.ps1`。

---

## 2. 快速开始

### 2.1 bigqmt 全链路启动（推荐日常入口）

```powershell
cd C:\Users\Docker\Desktop\Shared\qmt-bridge-big-qmt

just start-bigqmt-service                          # 默认端口 18888
# 或直接：
powershell -NoProfile -ExecutionPolicy Bypass -File scripts\start_bigqmt_service.ps1

# 常用变体：
... -Port 18889 -Trading        # 指定端口 + 启用交易模块
... -CheckOnly                  # 整链健康检查（①②照常检查，③只跑预检不启动服务）
... -Help                       # 用法速查；完整文档 Get-Help .\start_bigqmt_service.ps1 -Full
```

成功标志（控制台逐段 `[OK]`）：

```
==> ① Redis（127.0.0.1:6379）          [OK] 已在监听 / 已拉起（detached）
==> ② QMT 客户端（XtItClient）         [OK] 已在运行 (PID xxxx)
==> ③ qmt-server（bigqmt 后端）        [OK] xtquant-big-convert / Redis / account 预检通过
    Uvicorn running on http://0.0.0.0:18888
```

前台运行，**Ctrl+C 停止 qmt-server**；Redis 与 QMT 客户端是独立进程不受影响。

### 2.2 只启动 qmt-server（环境已就绪 / mini 后端）

```powershell
just serve-backend mini                  # mini：XtQuantTrader 直连 miniQMT（默认 18888）
just serve-backend bigqmt                # bigqmt：设好环境变量 + 预检后启动
# 或：
powershell -NoProfile -ExecutionPolicy Bypass -File scripts\serve_qmt_server.ps1 -Backend bigqmt -Port 18889
```

### 2.3 双后端并行（灰度比对，docs/big-qmt.md §6 步骤 3）

两个窗口各起一个，端口错开：

```powershell
# 窗口 A：mini（原路径）            # 窗口 B：bigqmt（新路径）
just serve-backend mini -Port 18888    just serve-backend bigqmt -Port 18889
```

对同一账户分别请求资产/持仓/委托/成交，逐字段比对（策略侧 API 两个后端完全一致）。

---

## 3. `serve_qmt_server.ps1` 参数速查

| 参数 | 默认 | 说明 |
|---|---|---|
| `-Backend` | `mini` | `mini` = XtQuantTrader 直连 miniQMT；`bigqmt` = Redis RPC → 完整版 QMT |
| `-Port` | `18888` | 监听端口（cli 默认 8000，脚本按仓库约定统一 18888） |
| `-AccountId` | `88002471` | bigqmt 专用。**必须与 QMT 侧 local_config 一致**，否则请求写错队列（ping 超时）；不传时客户端会退到纸面账户 `blue_chip_paper` |
| `-QmtPythonDir` | `C:\QMT_Simulator\python` | bigqmt 专用。QMT 侧服务端文件部署处，一致性预检（见下）比对用 |
| `-RedisHost/-RedisPort/-RedisDb` | `127.0.0.1/6379/5` | bigqmt RPC 中介（db=5 为上游约定） |
| `-Trading` | 关 | 透传 `--trading` 启用交易模块 |
| `-LogLevel` | `info` | `info/debug/warning/error` |
| `-CheckOnly` | — | 只跑预检不启动服务（链路健康检查，如 `just serve-backend bigqmt -CheckOnly`） |
| `-Help` | — | 打印速查；`Get-Help` 可看完整文档 |

**bigqmt 分支的四项预检**（防"跑到一半才发现环境错"）：
1. `qmt-server` 命令存在（editable 安装是否指对本仓库）；
2. `xtquant-big-convert` 包可导入（输出带版本号；否则提示先 `just deploy-bigqmt-full`）；
3. **客户端包 ↔ QMT 侧部署一致性**：QMT 侧是文件拷贝、不随 pip 走，
   editable 切换/升级后两边会漂移（2026-08-28 本机实际发生过）。不一致
   只告警不拦截 —— 行情（FormulaServer）不受影响，重跑
   `just deploy-bigqmt-full -SkipServerInstall -SkipRedisInstall` 同步
   （deploy 脚本会先自动备份旧文件）；
4. Redis 端口可连（不通则 bigqmt 交易 RPC 全超时）。

**mini 分支**零额外环境，要求 miniQMT 客户端已登录；券商若关停 miniQMT，
`connect()` 返回 -1 —— 此时切换 bigqmt 后端（这就是双后端存在的意义）。

---

## 4. `start_bigqmt_service.ps1` 参数速查

| 参数 | 默认 | 说明 |
|---|---|---|
| `-Port` | `18888` | 透传给 serve 脚本 |
| `-AccountId` | `88002471` | 同上 |
| `-QmtExe` | `C:\QMT_Simulator\bin.x64\XtItClient.exe` | QMT 客户端 exe（部署到其它客户端时改这个；QMT python 目录由它自动推导，传给 serve 的一致性预检） |
| `-RedisDir` | `~\bigqmt\redis` | redis-server.exe / redis.windows.conf 所在目录 |
| `-Redis*` / `-Trading` / `-LogLevel` / `-CheckOnly` / `-Help` | — | 同 serve 脚本（`-CheckOnly` = 整链健康检查，不启动 qmt-server） |

**各段自动恢复逻辑**：

| 段 | 检测 | 缺失时的动作 |
|---|---|---|
| ① Redis | TCP 探活 6379 | 有 exe → **detached 拉起**（参数与 deploy 脚本一致，RDB/日志落 RedisDir）；无 exe → 终止，提示先 deploy |
| ② QMT 客户端 | 进程 XtItClient | 有 exe → 以 `reboot` 参数启动（**自动登录未 100% 证实，起来后请瞄一眼登录态**）；无 exe → 终止 |
| ③ qmt-server | — | 委托 serve_qmt_server.ps1 -Backend bigqmt（含四项预检） |

**唯一无法脚本化的一段**：QMT 里 `bigqmt_rpc_bootstrap` 的模型交易实例须
「运行中」（模型交易页）。休市时 QMT 引擎不驱动策略脚本 —— `bigqmt.log`
为空、交易 RPC ping 超时都是**正常现象**，开盘后自动恢复（详见 §6）。

---

## 5. 启动后验证

```powershell
# ① 行情 —— FormulaServer 直连（127.0.0.1:58600，QMT 客户端开着即可，休市可用）
curl "http://127.0.0.1:18888/api/sector/stocks?sector=沪深A股"
#   → {"sector":"沪深A股","count":5216,...}   （毫秒级返回）

curl "http://127.0.0.1:18888/api/market/market_data_ex?stocks=000001.SH&period=1d&count=5"
#   → {"data":{"000001.SH":[...]}}
#   K 线为空 = QMT 本地还没下载过历史数据（先用 QMT 终端 数据管理 补数据）

# ② 交易 RPC —— 仅交易时段（QMT 端策略被引擎驱动后）：
#    看 C:\QMT_Simulator\python\logs\bigqmt.log 出现
#    [bigqmt_rpc] ... allow_order_methods=False
#    再跑： py C:\Users\Docker\bigqmt\verify_rpc.py
#    （ping + 资产/持仓/委托/成交 + submit_order 预期被拒 —— 下单门控生效的证明）
```

---

## 6. 行情通道说明（bigqmt 模式下读数据走哪条路）

| 调用 | 通道 | 休市可用 |
|---|---|---|
| `get_instrument_detail` / `get_stock_list_in_sector` / `get_last_volume` 等 10 个方法 | FormulaServer 直连（58600） | ✅ |
| `get_market_data_ex`（**显式字段**，含默认 OHLCV 集） | 同上 | ✅ |
| 复权 K 线（dividend_type ≠ none）、`get_full_tick`、旧 `get_market_data` | Redis RPC（需 QMT 策略端运行） | ❌ |
| `download_history_data` 等下载 | QMT 端 xtdata 数据服务不可达（上游限制），用终端 数据管理 UI | — |

> 历史坑：`/api/market/market_data_ex` 曾固定传 `field_list=[]`（=全部字段），
> 被 FormulaServer 路由拒绝而回落 RPC → 休市全超时。现 `market.py` 已改为
> 空字段时填充 `DEFAULT_BAR_FIELDS`（time/open/high/low/close/volume/amount），
> 并新增 `&fields=` 查询参数可显式指定。旧端点 `/api/history` 仍走 RPC，勿用。

---

## 7. 故障排查

| 症状 | 原因 | 处置 |
|---|---|---|
| `qmt_bridge 解析到别的仓库` / 无 server/trading | editable 指向旧 checkout | `py -m pip uninstall qmt-bridge` 后重跑 deploy 脚本 |
| `[XX] xtquant-big-convert 未安装` | 没跑过部署 | `just deploy-bigqmt-full` |
| `[XX] Redis ... 不可连`（serve 脚本） | Redis 没起 | `just start-bigqmt-service`（会自动拉起）或 deploy 脚本 |
| ping 超时但队列名是 `blue_chip_paper` | 没传账号 | 加 `-AccountId 88002471`（或 BIGQMT_ACCOUNT_ID 环境变量） |
| ping 超时、`bigqmt.log` 空、无新 `__pycache__` | 休市 / 策略实例没「运行中」 | 交易时段再试；模型交易页右键实例 → 启动 |
| K 线返回空数组 | QMT 本地无历史数据 | QMT 终端 数据管理 下载，再读 |
| 误勾了「启动本地 python」 | 策略被当独立进程跑，回调不驱动 | 实例设置里去掉勾选，重启实例 |
| mini 后端 connect 返回 -1 | miniQMT 未登录 / 被券商关停 | 登录 miniQMT；或切 `-Backend bigqmt` |
| 中文输出乱码（经 bash 管道时） | GBK 控制台显示问题 | 仅显示层面，不影响数据；用 PowerShell 原生窗口看 |

---

## 8. 相关文档

- `deploy-user-guide.md`（仓库根）—— 部署手册：首次安装 §3、手工 UI 步骤 §3.5–3.6、故障排查 §7
- `docs/big-qmt.md` —— 设计文档：§3 架构（双后端；§3.3 行情通道由 `server/xtdata_source.py` 显式解析 xtquant-big-convert）、§6 灰度四步
- 脚本内置文档：`Get-Help .\serve_qmt_server.ps1 -Full` / `Get-Help .\start_bigqmt_service.ps1 -Full`
- just 入口：`just start-bigqmt-service` / `just serve-backend <mini|bigqmt>` / `just serve-bigqmt`
