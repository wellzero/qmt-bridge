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

## 数据目录

默认读取项目根目录下的 ``data/paper_trading``。可通过以下方式修改：

- 侧边栏输入框
- 环境变量 ``PAPER_TRADING_DATA_DIR``

## 文件说明

- ``app.py``：Streamlit 入口页面
- ``data_loader.py``：读取 ``config.json``、``summary.json`` 和 ``order/*.csv``
- ``components.py``：页面展示组件
