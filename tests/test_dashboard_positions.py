"""仪表盘数据加载单元测试。

目录名 ``paper-trading`` 含连字符无法作为包导入，
通过 importlib 按显式路径加载 ``data_loader`` 模块。
"""

from __future__ import annotations

import importlib.util
import json
from pathlib import Path

import pandas as pd

_DASHBOARD_DIR = Path(__file__).resolve().parents[1] / "dashboard" / "paper-trading"


def _load_data_loader():
    """按路径加载 dashboard 的 data_loader 模块。"""
    spec = importlib.util.spec_from_file_location(
        "dashboard_data_loader", _DASHBOARD_DIR / "data_loader.py"
    )
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


dl = _load_data_loader()


def _make_orders(rows: list[tuple]) -> pd.DataFrame:
    """构造委托 DataFrame：(stock_code, order_type, traded_volume, traded_price, trade_date, order_time)。"""
    return pd.DataFrame(
        rows,
        columns=[
            "stock_code",
            "order_type",
            "traded_volume",
            "traded_price",
            "trade_date",
            "order_time",
        ],
    )


def test_latest_price_ignores_failed_orders():
    """最近成交价应取自最后一条已成交委托，废单（traded_price=0）不参与。"""
    orders = _make_orders(
        [
            ("000001.SZ", "23", 1000.0, 10.0, "20260815", "10:00:00"),
            ("000001.SZ", "23", 500.0, 12.0, "20260819", "10:00:00"),
            ("000001.SZ", "23", 0.0, 0.0, "20260819", "10:50:00"),  # 可用资金不足
        ]
    )

    positions = dl._derive_positions_core(orders)
    assert len(positions) == 1
    row = positions.iloc[0]
    assert row["volume"] == 1500.0
    assert row["traded_price"] == 12.0
    assert row["market_value"] == 18000.0


def test_model_selection_prefers_latest_day_after_account_reset():
    """账户重置后（历史买入无卖出记录），参考市值应选中最新买入日模型。

    回归场景：2026-08-19 账户 csi500_earnings_reversal_biweekly_paper 重置后
    残留历史委托，废单 0 价把最新日模型市值压低，误选累积型模型，
    导致仪表盘显示 78% 虚增收益。
    """
    orders = _make_orders(
        [
            # 重置前的历史买入（重置未留下卖出记录）
            ("000001.SZ", "23", 1000.0, 10.0, "20260815", "10:00:00"),
            # 重置后的真实买入与失败的补买
            ("000001.SZ", "23", 500.0, 12.0, "20260819", "10:00:00"),
            ("000001.SZ", "23", 0.0, 0.0, "20260819", "10:50:00"),
            ("600000.SH", "23", 400.0, 25.0, "20260819", "10:01:00"),
            ("600000.SH", "23", 0.0, 0.0, "20260819", "10:51:00"),
        ]
    )
    # 引擎 summary/最近委托记录的真实市值：500×12 + 400×25
    reference = 500.0 * 12.0 + 400.0 * 25.0

    positions = dl.derive_positions_with_cost(orders, reference_market_value=reference)

    assert len(positions) == 2
    volumes = dict(zip(positions["stock_code"], positions["volume"]))
    assert volumes["000001.SZ"] == 500.0  # 不含重置前的幽灵持仓
    assert volumes["600000.SH"] == 400.0
    assert positions["market_value"].sum() == reference


def test_model_selection_finds_mid_history_reset_date():
    """重置日之后还有多个买入日时，应选中与参考市值吻合的中间基准日。

    回归场景：2026-08-25 账户 csi300_cpi_growth_swingtrading_paper 显示
    +51.66% 虚增收益——引擎 08-18 重置，但"最新买入日"模型基准是 08-25
    （当日买卖轧差后持仓为空），市值反而离参考值更远，旧逻辑只能
    在"全量累积"与"最新买入日"两个模型里二选一，误选了含幽灵持仓
    的全量累积模型。
    """
    orders = _make_orders(
        [
            # 08-15 重置前的幽灵买入
            ("000001.SZ", "23", 1000.0, 10.0, "20260815", "10:00:00"),
            ("600000.SH", "23", 400.0, 25.0, "20260815", "10:01:00"),
            # 08-18 重置后的真实建仓
            ("000001.SZ", "23", 500.0, 12.0, "20260818", "10:00:00"),
            ("600000.SH", "23", 400.0, 25.0, "20260818", "10:01:00"),
            # 08-22 调仓：卖旧买新（当日轧差后 000001 持仓为 0）
            ("000001.SZ", "24", 500.0, 13.0, "20260822", "10:00:00"),
            # 08-25 最新买入日：再买回 000001
            ("000001.SZ", "23", 300.0, 13.5, "20260825", "10:00:00"),
        ]
    )
    # 引擎真实持仓（08-18 起）：000001 300 股 @13.5 + 600000 400 股 @25
    reference = 300.0 * 13.5 + 400.0 * 25.0

    positions = dl.derive_positions_with_cost(orders, reference_market_value=reference)

    volumes = dict(zip(positions["stock_code"], positions["volume"]))
    assert volumes["000001.SZ"] == 300.0  # 不含 08-15 的 1000 股幽灵持仓
    assert volumes["600000.SH"] == 400.0
    assert positions["market_value"].sum() == reference


def test_model_selection_finds_intraday_reset_boundary():
    """重置发生在盘中时，截断点应能落在同一天的两笔委托之间。

    回归场景：2026-08-25 账户 dividend_bluechip_quarterly_paper 等显示
    +23% 虚增收益——重置边界在 2026-07-16 盘中，按"交易日"为基准的
    模型无法表达该边界（无论选哪天都会多算或少算）。
    """
    orders = _make_orders(
        [
            # 07-16 上午的幽灵买入（重置前）
            ("000001.SZ", "23", 1000.0, 10.0, "20260716", "09:35:00"),
            # 07-16 下午重置后的真实建仓（同一天！）
            ("000001.SZ", "23", 600.0, 11.0, "20260716", "13:05:00"),
            ("600000.SH", "23", 200.0, 20.0, "20260716", "13:06:00"),
        ]
    )
    # 引擎真实持仓：600 股 @11 + 200 股 @20
    reference = 600.0 * 11.0 + 200.0 * 20.0

    positions = dl.derive_positions_with_cost(orders, reference_market_value=reference)

    volumes = dict(zip(positions["stock_code"], positions["volume"]))
    assert volumes["000001.SZ"] == 600.0  # 不含同日上午的 1000 股幽灵持仓
    assert volumes["600000.SH"] == 200.0
    assert positions["market_value"].sum() == reference


def test_cash_reconciliation_beats_misleading_market_value():
    """资金流水精确反推应优先于市值比较（估值价差会误导市值法）。

    场景：引擎按实时价计参考市值（+10%），小市值幽灵持仓恰好把
    全量累积模型的市值"凑"得更接近参考值，市值法会误选含幽灵持仓
    的模型；资金流水与估值价格无关，能精确锁定真实窗口。
    """
    orders = pd.DataFrame(
        [
            # 重置前的幽灵买入：100 股 @10，手续费 5（旧引擎现金 89995）
            {
                "stock_code": "000001.SZ",
                "order_type": "23",
                "traded_volume": 100.0,
                "traded_price": 10.0,
                "trade_date": "20260815",
                "order_time": "10:00:00",
                "commission": 5.0,
                "stamp_tax": 0.0,
                "account_cash": 89995.0,
            },
            # 重置后的真实买入：300 股 @30，手续费 5（现金 100000-9005）
            {
                "stock_code": "600000.SH",
                "order_type": "23",
                "traded_volume": 300.0,
                "traded_price": 30.0,
                "trade_date": "20260818",
                "order_time": "10:00:00",
                "commission": 5.0,
                "stamp_tax": 0.0,
                "account_cash": 90995.0,
            },
        ]
    )
    # 引擎参考市值按实时价计（600000 实时 33 元）：300×33 = 9900。
    # 全量累积模型按成交价估 100×10 + 300×30 = 10000，反而更接近 9900，
    # 市值法会误选全量累积（含幽灵持仓）
    reference = 300.0 * 33.0

    positions = dl.derive_positions_with_cost(
        orders, initial_cash=100000.0, reference_market_value=reference
    )

    volumes = dict(zip(positions["stock_code"], positions["volume"]))
    assert "000001.SZ" not in volumes  # 幽灵持仓被资金流水法排除
    assert volumes["600000.SH"] == 300.0


def test_list_account_ids_includes_config_only_accounts(tmp_path):
    """仅在 config.json 注册、尚无任何委托的账户（如刚启动的实盘因子策略）也应列出。"""
    data_dir = tmp_path / "paper_trading"
    data_dir.mkdir()

    # 有委托数据的账户
    traded = data_dir / "traded_acc"
    (traded / "order").mkdir(parents=True)
    (traded / "order" / "orders_20260825.csv").write_text(
        "order_id\n1\n", encoding="utf-8"
    )
    # 与账户无关的目录（如价格缓存）不应被误认为账户
    (data_dir / "prices").mkdir()

    config = {
        "config_only_acc": {
            "account_id": "config_only_acc",
            "initial_cash": 100000.0,
            "enabled": True,
        },
        "disabled_acc": {"account_id": "disabled_acc", "enabled": False},
    }
    (data_dir / "config.json").write_text(json.dumps(config), encoding="utf-8")

    ids = dl.list_account_ids(data_dir)

    assert "traded_acc" in ids
    assert "config_only_acc" in ids
    assert "disabled_acc" not in ids  # 已禁用账户不展示
    assert "prices" not in ids
