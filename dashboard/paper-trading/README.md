# 模拟交易结果仪表盘

基于 Streamlit 的独立 HTTP 仪表盘，用于可视化展示 ``data/paper_trading`` 下的所有模拟交易账户结果。

## 功能

- 总览：账户数量、总资产、总盈亏、总成交笔数
- 账户列表：所有账户的资金、市值、收益率、成交笔数等
- 账户详情：
  - 账户配置（价格源、手续费率、最低手续费、印花税率等）
  - 资产变化趋势图（可用资金、持仓市值、总资产）
  - 全部委托记录
  - 由委托记录推导的当前持仓（仅供参考）

## 运行方式

### 1. 安装仪表盘依赖

```bash
just install-dashboard
```

或

```bash
pip install -e ".[dashboard]"
```

### 2. 启动仪表盘

```bash
streamlit run dashboard/paper-trading/app.py
```

或使用 just 快捷命令：

```bash
just paper-dashboard
```

默认访问：http://localhost:8501

## 认证

仪表盘默认启用登录，初始凭据：

- **用户名**: `admin`
- **密码**: `quant2024`

登录后可在侧边栏点击「退出登录」。

## 交互式交易图表

在「账户列表」中点击任意策略/账户行，即可在「账户详情」中查看该账户的交互式图表：

- **X 轴**：委托日期时间
- **Y 轴**：总资产（可用资金 + 持仓市值）
- **蓝色曲线**：总资产变化
- **橙色菱形标记**：每笔委托的时点
- **悬停提示**：将鼠标移到橙色标记上，可查看该笔委托的时间、股票代码、方向、委托量、成交量、成交价、手续费、印花税、状态及总资产

> 账户切换方式：在「账户列表」表格中点击目标行即可；默认展示第一个账户。

## 实时盈亏估算

仪表盘支持基于**当前价/收盘价**的实时盈亏估算：

- **交易时段**：读取 ``data/paper_trading/prices/current.json``，使用盘中最新价估算持仓市值与浮动盈亏。
- **收盘后**：读取 ``data/paper_trading/prices/YYYYMMDD.json``，使用当日收盘价估算。
- 价格源优先级：盘中最新价 → 当日收盘价 → 账户配置 ``static_prices`` → 最近成交价兜底。

更新价格缓存的方式：

```bash
# 交易时段更新盘中最新价
just update-paper-price-cache --host <qmt-server-host> --port 8083 --api-key <key>

# 收盘后更新当日收盘价
just update-paper-close-price --host <qmt-server-host> --port 8083 --api-key <key>
```

或直接用 Python：

```bash
python scripts/update_paper_price_cache.py --host <host> --port 8083 --api-key <key>
python scripts/update_paper_price_cache.py --close --host <host> --port 8083 --api-key <key>
```

## 数据目录

默认读取项目根目录下的 ``data/paper_trading``。可通过以下方式修改：

- 侧边栏输入框
- 环境变量 ``PAPER_TRADING_DATA_DIR``

## 文件说明

- ``app.py``：Streamlit 入口页面
- ``data_loader.py``：读取 ``config.json``、``summary.json`` 和 ``order/*.csv``
- ``components.py``：页面展示组件
