# xtquant_big_convert 与 QMT 的协作方式（工作流）

> 本文是 `docs/big-qmt.md`（设计定稿）的阅读伴侣，聚焦回答一个问题：
> **`xtquant_big_convert` 是怎么跟完整版 QMT 配合工作的**。
> 设计细节、映射表、灰度步骤请看 `docs/big-qmt.md`。

## 1. xtquant_big_convert 是什么

第三方库（[GitHub](https://github.com/litaolemo/xtquant_big_convert)，MIT，PyPI 包名
`xtquant-big-convert`），用于**整体取代 miniQMT + xtquant**——因为券商自
2026-07 起逐步停用 miniQMT 外部直连（合规红线：外部进程直连 QMT 属"外接系统"）。

它的思路：把"接触 QMT"的代码移入完整版 QMT 的**内置 Python 3.6 沙箱**，
其余部分保持原位。于是它天然分成两个 halves：

- **服务端**：跑在完整版 QMT 内置 Python 沙箱里，把 `passorder` / `cancel` /
  `get_trade_detail_data` / 行情调用封装成 RPC 服务；
- **客户端**：pip 安装，跑在 qmt-server 进程内，把调用翻译成 Redis/ZMQ RPC。

## 2. 整体链路（四跳）

```text
Linux 策略 / paper 策略 / Streamlit 仪表盘
   │ HTTP / WS（接口零改动，不感知后端切换）
qmt-bridge 客户端（本仓库 client/，纯通道）
   │
Windows qmt-server（FastAPI，本仓库 server/）
   │  ├─ BigQmtAdapter（交易）→ Redis/ZMQ RPC
   │  └─ xtdata_source（行情）→ 同一 RPC 客户端
   ▼
完整版 QMT 客户端（同机 Windows）
   └─ 内置 Python 3.6 沙箱：xtquant_big_convert 服务端
        └─ passorder / get_trade_detail_data → 券商柜台
```

即：**`xtquant_big_convert` ＋ 完整版 QMT 取代 `miniQMT` ＋ `xtquant`**。
qmt-server 仍部署在 Windows（与完整版 QMT 同机），Linux 侧只跑策略与客户端。

## 3. 两个 halves 详解

### 3.1 服务端（QMT 内置 Python 沙箱）

由 `scripts/deploy_bigqmt_server.py` 部署：

1. 从 pip 安装的 wheel 中提取 `bigqmt_signal_trader/` 包 +
   `BIGQMT_REDIS_DRYRUN.py` 等顶层入口模块；
2. 复制到完整版 QMT 客户端的 `python` 目录（内置 Python 3.6 沙箱）；
3. 生成含账户 ID + Redis 配置的 `bigqmt_signal_trader_local_config.py`
   （位于 QMT 安装目录，天然在仓库之外；已存在则保留，绝不覆盖敏感配置）。

它在沙箱内以 QMT 策略形式运行（`init` / `handlebar` / `adjust` 驱动），
对外提供 RPC 服务。关键安全阀：

```python
# bigqmt_signal_trader_local_config.py
BIGQMT_REDIS_CONFIG = {
    ...
    "rpc_allow_order_methods": False,  # 下单默认关闭，灰度通过后才改 True
}
```

### 3.2 客户端（qmt-server 进程内）

pip 安装 `qmt-bridge[bigqmt]` 后，分两条通道接入本仓库：

#### 交易通道 —— BigQmtAdapter

`src/qmt_bridge/server/trading/bigqmt_backend.py`：

- 实现 `TraderBackend` 协议，与现有 `MiniQmtBackend` 并列双后端，
  由 CLI 开关 `--trader-backend mini|bigqmt` 切换；
- `connect()` 时 `compat.configure()` 初始化 `xt_trader` 单例
  （读取 local_config 或 `BIGQMT_*` 环境变量），内部发 `ping` RPC 验证链路，
  Redis 或 QMT 侧服务端不可用时启动即失败；
- 每个 qmt-bridge 交易接口**逐方法翻译**成 RPC：`order_stock()` →
  `submit_order` RPC、`query_stock_asset()` → `get_asset` RPC 等，
  方法名与 miniQMT 保持一致；
- 上游未覆盖的能力（银证转账、资金/证券划转、CTP、SMT、IPO、COM、
  数据导出、证券子账户）通过**能力位降级**处理：

  ```python
  SUPPORTED_CAPABILITIES = frozenset({"order", "query", "credit", "account"})
  ```

  缺的方法自动生成抛 `UnsupportedOperation` 的桩 → 路由返回 503
  "等待远端支持"（见文件末尾对 `CAPABILITY_METHODS` 的循环）。

#### 行情通道 —— xtdata 来源选择器

`src/qmt_bridge/server/xtdata_source.py`：

- 本仓库 routers / ws / downloader / scheduler / paper_trading 共 30+ 处
  `from xtquant import xtdata` 统一改为 `from .xtdata_source import xtdata`；
- 模块导入时按 `settings.trader_backend` 解析一次并绑定：
  `bigqmt` → `bigqmt_signal_trader.xtquant_compat.xtdata`（上游单例，
  editable 安装即直连源码 `src/` 本体），`mini`（默认）→ 真实
  `xtquant.xtdata`；
- 显式间接层，不依赖 sys.path 顺序 —— 本机存在多个同名 `xtquant` 包
  （Lib 手动拷贝 / site-packages / editable checkout），顺序法易被遮蔽；
- 时序要求：`cli.py` 先 `reset_settings` 再导入 app / scheduler；
- 额外收益：`get_full_tick` 走 FormulaServer 快路径（QMT C++ 服务，
  端口 58600，p50 ≈ 0.07ms，不占 Python GIL）。

## 4. 关键约束（big-qmt.md §5）

| 约束 | 说明 |
|---|---|
| 实盘 only | QMT 模拟模式下委托不进真实队列（下单返回 -1、查询为空）；模拟走 `server/paper_trading/` 本地引擎 |
| 单实例单账户 | 一个 QMT 客户端 = 一个 live 账户，多账户需多 QMT 实例 + 多 bridge 配置；请求别的账户 ID 时告警并回落到已配置账户 |
| 下单默认关 | `rpc_allow_order_methods` 是天然灰度开关 |
| 默认 Redis 传输 | ZMQ 约 30% 请求出现 ~500ms GIL 尖峰，Redis p50 ≈ 13ms |
| 事件回调需验证 | 委托/成交事件经 Redis pubsub（`exec_events`）推送，drop-in 的 `register_callback` 可能不投递（big-qmt.md §3.6 待验证项） |

## 5. 快速上手（big-qmt.md §6 步骤 2~5）

```bash
# 1. 安装客户端（qmt-server 机器）
pip install 'qmt-bridge[server,bigqmt]'

# 2. 部署服务端到 QMT 内置 Python（先 --dry-run 预览；源自动取
#    import bigqmt_signal_trader 的落点，覆盖前自动备份旧文件）
python scripts/deploy_bigqmt_server.py --dry-run
python scripts/deploy_bigqmt_server.py \
    --qmt-python-dir "D:\国金QMT交易端\..\python" \
    --account-id 12345678

# 3. 在 QMT 内置 Python 中运行 BIGQMT_REDIS_DRYRUN.py（下单保持关闭）

# 4. qmt-server 切到 bigqmt 后端（行情由 xtdata_source 自动解析）
qmt-server --trader-backend bigqmt
#   或环境变量：QMT_BRIDGE_TRADER_BACKEND=bigqmt
```

灰度顺序：双后端并行 → 资产/持仓/委托/成交逐字段比对 →
`rpc_allow_order_methods: True` 小额下单验证 → 正式切换
（mini 后端保留至券商关停）。

## 6. 相关文件索引

| 文件 | 职责 |
|---|---|
| `docs/big-qmt.md` | 设计定稿（映射表 §4、约束 §5、灰度步骤 §6） |
| `src/qmt_bridge/server/trading/bigqmt_backend.py` | BigQmtAdapter（交易后端，逐方法映射 + 能力位降级） |
| `src/qmt_bridge/server/trading/backend.py` | `TraderBackend` 协议 + `CAPABILITY_METHODS` 能力位分组 |
| `src/qmt_bridge/server/trading/mini_backend.py` | 现有 XtQuantTrader 实现（原样保留） |
| `src/qmt_bridge/server/xtdata_source.py` | xtdata 来源选择器（行情通道，按后端显式解析） |
| `src/qmt_bridge/server/cli.py` | `--trader-backend` 开关 |
| `scripts/deploy_bigqmt_server.py` | QMT 侧服务端部署（源解析 + 备份 + 遗留告警） |
| `tests/test_trading_backends.py` | 双后端测试（全 mock，脱离 QMT 环境） |
