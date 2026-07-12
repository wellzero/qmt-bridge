# 模拟交易使用指南

本指南介绍如何在 QMT Bridge 中启用、配置和使用模拟交易（Paper Trading）模块，
在不连接真实 QMT 客户端的情况下进行下单、持仓、业绩复盘，以及如何让策略对接模拟交易。

## 目录

- [适用场景](#适用场景)
- [启用模拟交易](#启用模拟交易)
- [创建与管理账户](#创建与管理账户)
- [下单与查询](#下单与查询)
- [批量下载行情价格](#批量下载行情价格)
- [对接策略（lumibot）](#对接策略-lumibot)
- [数据文件与目录结构](#数据文件与目录结构)
- [配置参考](#配置参考)
- [常见问题](#常见问题)

---

## 适用场景

- **策略回测与验证**：在真实交易前验证下单逻辑、风控与仓位管理。
- **无 QMT 环境开发**：在 Mac / Linux 开发机上调试交易代码（需额外 mock 行情数据）。
- **多账户模拟**：同时维护多个虚拟资金账号，互不干扰，适合多策略并行验证。
- **教学演示**：在不产生真实成交的情况下展示交易流程。

---

## 启用模拟交易

### 命令行启动

```bash
# 仅启用模拟交易（无需 QMT 客户端）
qmt-server --paper-trading --api-key your-secret-key

# 同时启用真实交易与模拟交易
qmt-server --trading --paper-trading --api-key your-secret-key \
  --mini-qmt-path "C:\国金QMT交易端\userdata_mini" \
  --account-id 12345678
```

### 环境变量

| 环境变量 | 默认值 | 说明 |
|---------|-------|------|
| `QMT_BRIDGE_PAPER_TRADING_ENABLED` | `false` | 是否启用模拟交易模块 |
| `QMT_BRIDGE_PAPER_TRADING_DATA_DIR` | `./data` | 数据根目录，账户数据会放在 `{data_dir}/paper_trading/{account_id}/` 下 |
| `QMT_BRIDGE_PAPER_TRADING_CONFIG_PATH` | _(空)_ | 配置文件路径（可选） |

> 注意：`paper_trading_data_dir` 是数据根目录。若你之前已配置为 `./data/paper_trading`，
> 系统会兼容该路径，不再额外追加一层 `paper_trading`。

### 验证服务

启动后访问 Swagger UI：

```bash
curl http://localhost:8083/docs
```

模拟交易相关路由前缀为 `/api/paper_accounts/*` 与 `/api/paper_trading/*`。

---

## 创建与管理账户

每个模拟账户拥有独立的资金、持仓、委托、成交与业绩摘要。

### 创建账户

```bash
curl -X POST http://localhost:8083/api/paper_accounts \
  -H "X-API-Key: your-secret-key" \
  -H "Content-Type: application/json" \
  -d '{
    "account_id": "acc001",
    "account_type": 2,
    "initial_cash": 1000000.0,
    "commission_rate": 0.0003,
    "stamp_tax_rate": 0.0005,
    "slippage": 0.0,
    "price_source": "static",
    "static_prices": {
      "000001.SZ": 12.34,
      "600519.SH": 1800.0
    },
    "partial_fill_enabled": false,
    "enabled": true
  }'
```

关键字段说明：

| 字段 | 说明 |
|-----|------|
| `account_id` | 账户唯一标识，后续所有交易操作都需要指定 |
| `initial_cash` | 初始资金 |
| `commission_rate` | 手续费率，默认万 3 |
| `stamp_tax_rate` | 印花税率，默认千 0.5，仅卖出收取 |
| `slippage` | 滑点比例，买入 `×(1+slippage)`，卖出 `×(1-slippage)` |
| `price_source` | `xtdata` / `static` / `fallback` |
| `static_prices` | `static` 或 `fallback` 失败时使用的价格表 |
| `partial_fill_enabled` | 是否启用部分成交模拟（当前版本默认整单成交） |
| `enabled` | 是否启用该账户 |

### 查询账户配置

```bash
# 单个账户
curl http://localhost:8083/api/paper_accounts/acc001 \
  -H "X-API-Key: your-secret-key"

# 全部账户
curl http://localhost:8083/api/paper_accounts \
  -H "X-API-Key: your-secret-key"
```

### 更新账户配置

`POST /api/paper_accounts` 即可创建或更新。例如调整初始资金或价格源：

```bash
curl -X POST http://localhost:8083/api/paper_accounts \
  -H "X-API-Key: your-secret-key" \
  -H "Content-Type: application/json" \
  -d '{
    "account_id": "acc001",
    "initial_cash": 500000.0,
    "price_source": "fallback",
    "static_prices": {"000001.SZ": 13.0}
  }'
```

> 更新配置不会重置已有持仓和资金；如需清空请调用重置接口。

### 重置账户

清空持仓、委托、成交，资金恢复为 `initial_cash`：

```bash
curl -X POST http://localhost:8083/api/paper_accounts/acc001/reset \
  -H "X-API-Key: your-secret-key"
```

### 删除账户

删除配置、持仓、委托、成交与摘要数据：

```bash
curl -X DELETE http://localhost:8083/api/paper_accounts/acc001 \
  -H "X-API-Key: your-secret-key"
```

---

## 下单与查询

### 价格源说明

| 价格源 | 说明 |
|-------|------|
| `xtdata` | 调用 `xtquant.xtdata.get_full_tick()` 获取最新价，需 QMT 客户端可用 |
| `static` | 使用账户配置中的 `static_prices` |
| `fallback` | 先尝试 `xtdata`，失败再回退到 `static` |

限价单（`price_type=11` 且 `price > 0`）始终以委托价成交。
市价/最新价单（`price_type=5`）从价格源取价，并叠加滑点。

### 下单

```bash
curl -X POST http://localhost:8083/api/paper_trading/order \
  -H "X-API-Key: your-secret-key" \
  -H "Content-Type: application/json" \
  -d '{
    "account_id": "acc001",
    "stock_code": "000001.SZ",
    "order_type": 23,
    "order_volume": 1000,
    "price_type": 11,
    "price": 12.34,
    "strategy_name": "manual_test",
    "order_remark": "demo"
  }'
```

响应示例：

```json
{"order_id": 1, "status": "submitted"}
```

### 查询资产

```bash
curl "http://localhost:8083/api/paper_trading/asset?account_id=acc001" \
  -H "X-API-Key: your-secret-key"
```

### 查询持仓

```bash
curl "http://localhost:8083/api/paper_trading/positions?account_id=acc001" \
  -H "X-API-Key: your-secret-key"
```

### 查询当日委托

```bash
curl "http://localhost:8083/api/paper_trading/orders?account_id=acc001" \
  -H "X-API-Key: your-secret-key"
```

### 查询当日成交

```bash
curl "http://localhost:8083/api/paper_trading/trades?account_id=acc001" \
  -H "X-API-Key: your-secret-key"
```

### 撤单

```bash
curl -X POST http://localhost:8083/api/paper_trading/cancel \
  -H "X-API-Key: your-secret-key" \
  -H "Content-Type: application/json" \
  -d '{"account_id": "acc001", "order_id": 1}'
```

### 查询业绩摘要

```bash
curl "http://localhost:8083/api/paper_trading/summary?account_id=acc001" \
  -H "X-API-Key: your-secret-key"
```

响应示例：

```json
{
  "data": {
    "account_id": "acc001",
    "initial_cash": 1000000.0,
    "cash": 987654.32,
    "market_value": 12345.68,
    "total_asset": 1000000.0,
    "total_pnl": 0.0,
    "total_return_rate": 0.0,
    "realized_pnl": 0.0,
    "unrealized_pnl": 0.0,
    "total_trades": 1,
    "total_commission": 3.7,
    "total_stamp_tax": 0.0
  }
}
```

---

## 批量下载行情价格

当使用 `static` 或 `fallback` 价格源时，可通过 xtquant 接口批量下载最新行情并写入账户的 `static_prices`：

```bash
curl -X POST http://localhost:8083/api/paper_accounts/acc001/download_prices \
  -H "X-API-Key: your-secret-key" \
  -H "Content-Type: application/json" \
  -d '{"stock_codes": ["000001.SZ", "600519.SH"]}'
```

响应示例：

```json
{
  "data": {
    "000001.SZ": 12.34,
    "600519.SH": 1800.0
  }
}
```

- 仅下载成功的股票会出现在响应中。
- 下载后的价格会自动保存到账户配置，后续撮合直接使用。
- 该接口依赖 xtquant，仅在 QMT 客户端可用时能成功取到行情。

---

## 对接策略（lumibot）

QMT Bridge 客户端与 lumibot 的 `QMTBridgeBroker` 已支持 `paper` 模式，
策略无需修改代码，只需通过环境变量切换即可走模拟交易端点。

### 环境变量

| 环境变量 | 说明 |
|---------|------|
| `QMT_BRIDGE_HOST` | QMT Bridge 服务地址，默认 `localhost` |
| `QMT_BRIDGE_PORT` | 端口，默认 `8083` |
| `QMT_BRIDGE_API_KEY` | API Key |
| `QMT_BRIDGE_TRADING_ACCOUNT_ID` | 模拟账户 ID |
| `QMT_BRIDGE_PAPER` | 设置为 `true` 启用模拟交易 |
| `IS_BACKTESTING` | 设置为 `false` 进入实盘/模拟交易模式 |

### 单策略运行示例

```bash
export QMT_BRIDGE_HOST=localhost
export QMT_BRIDGE_PORT=8083
export QMT_BRIDGE_API_KEY=your-secret-key
export QMT_BRIDGE_TRADING_ACCOUNT_ID=blue_chip_paper
export QMT_BRIDGE_PAPER=true
export IS_BACKTESTING=false

python /home/claude/quant_free_trading/cn_strategy/blue_chip_multi_factor_rotation/backtest/blue_chip_multi_factor_rotation.py
```

> **注意 `.env` 加载顺序**
> 部分策略文件顶部会显式调用 `load_dotenv(..., override=True)` 加载 lumibot 包目录下的 `.env`，
> 导致策略目录下自己的 `.env` 被覆盖。若发现 `QMT_BRIDGE_PAPER=true` 未生效，
> 请把策略顶部的 `load_dotenv` 改为优先加载策略目录自身的 `.env`，并不覆盖已存在的环境变量：
>
> ```python
> from pathlib import Path
> from dotenv import load_dotenv
>
> strategy_env_path = Path(__file__).resolve().parent / ".env"
> if strategy_env_path.exists():
>     load_dotenv(strategy_env_path)  # 不覆盖 shell 已设置变量
> ```

### 多策略并发运行

推荐使用项目内置的测试运行器 `scripts/run_paper_strategy_test.py`，
它会自动启动服务端、创建账户、并发运行策略、汇总业绩：

```bash
cd /home/quant_volumn/docker/data/qmt-bridge

# 双策略并发，不同 account_id
python scripts/run_paper_strategy_test.py \
  --strategy /home/claude/quant_free_trading/cn_strategy/blue_chip_multi_factor_rotation/backtest/blue_chip_multi_factor_rotation_backup.py \
  --account-id blue_chip_paper \
  --strategy /home/claude/quant_free_trading/cn_strategy/etf/xgb_etf_regime_rotation/backtest/xgb_etf_regime_rotation.py \
  --account-id xgb_etf_paper \
  --api-key your-secret-key \
  --duration 180
```

参数说明：

| 参数 | 说明 |
|-----|------|
| `--strategy` | 策略文件路径，可多次传入实现并发 |
| `--account-id` | 与 `--strategy` 一一对应的模拟账户 ID |
| `--duration` | 每个策略运行时长（秒），超时后自动终止 |
| `--static-prices-file` | 静态价格 JSON 文件路径（可选，用于无 QMT 行情时） |
| `--no-server` | 不自动启动服务端，使用外部已启动的服务端 |

---

## 数据文件与目录结构

模拟交易数据默认存放在 `./data/paper_trading/` 下：

```text
data/paper_trading/
├── config.json                       # 所有账户的全局配置
├── acc001/
│   ├── order/
│   │   └── orders_20260712.csv       # 当日委托流水
│   └── summary/
│       └── summary.json              # 账户业绩摘要
├── acc002/
│   ├── order/
│   │   └── orders_20260712.csv
│   └── summary/
│       └── summary.json
└── ...
```

### 委托 CSV 表头

```csv
order_time,order_id,stock_code,order_type,order_volume,price_type,price,traded_volume,traded_price,order_status,status_msg,strategy_name,order_remark
```

### 业绩摘要 JSON 字段

```json
{
  "account_id": "acc001",
  "initial_cash": 1000000.0,
  "cash": 987654.32,
  "market_value": 12345.68,
  "total_asset": 1000000.0,
  "total_pnl": 0.0,
  "total_return_rate": 0.0,
  "realized_pnl": 0.0,
  "unrealized_pnl": 0.0,
  "total_trades": 1,
  "total_commission": 3.7,
  "total_stamp_tax": 0.0
}
```

---

## 配置参考

### 服务端配置

```bash
qmt-server \
  --paper-trading \
  --api-key your-secret-key \
  --port 8083 \
  --paper-trading-data-dir ./data
```

或通过环境变量：

```bash
export QMT_BRIDGE_PAPER_TRADING_ENABLED=true
export QMT_BRIDGE_PAPER_TRADING_DATA_DIR=./data
export QMT_BRIDGE_API_KEY=your-secret-key
qmt-server
```

### 客户端配置（Python）

```python
from qmt_bridge import QMTClient

client = QMTClient(
    host="localhost",
    port=8083,
    api_key="your-secret-key",
    paper=True,  # 启用模拟交易端点
)

result = client.place_order(
    stock_code="000001.SZ",
    order_type=23,      # 买入
    order_volume=1000,
    price_type=11,      # 限价
    price=12.34,
    account_id="acc001",
)
print(result)
```

---

## 常见问题

### Q：模拟交易是否依赖 QMT 客户端？

A：模块本身不依赖 QMT，但若使用 `xtdata` 或 `fallback` 价格源，或调用 `/api/paper_accounts/{id}/download_prices` 下载行情，则需要 QMT 客户端提供实时价格。若使用 `static` 价格源并手动维护 `static_prices`，则完全不需要 QMT。

### Q：策略在模拟交易中为什么不成交？

A：常见原因：

1. `price_source` 为 `xtdata`/`fallback` 但无有效行情，导致无法获取成交价。
2. 账户资金不足或持仓不足。
3. 策略当前非调仓日，未产生交易信号。

建议先通过 `GET /api/paper_trading/asset` 和 `GET /api/paper_trading/orders` 排查。

### Q：一个服务端可以同时跑多个策略吗？

A：可以。每个策略使用不同的 `account_id` 即可，资金和持仓完全隔离。

### Q：模拟交易的撮合规则是什么？

A：当前版本默认整单成交；买入校验资金，卖出校验持仓；成交后即时更新资产、持仓、委托状态、CSV 流水和 JSON 摘要。手续费按 `commission_rate` 计算，印花税仅在卖出时按 `stamp_tax_rate` 计算。

### Q：Linux / Mac 上能跑完整策略测试吗？

A：策略历史数据接口依赖 xtquant，而 xtquant 是 Windows 二进制库，因此完整策略测试需要在 Windows + QMT 客户端环境运行。在 Linux / Mac 上可运行单元测试和纯静态价格模拟交易。
