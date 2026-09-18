"""资产走势数据口径单元测试。

目录名 ``paper-trading`` 含连字符无法作为包导入，
将目录加入 ``sys.path`` 后按模块名加载 ``data_loader``。
"""

from __future__ import annotations

import sys
from pathlib import Path

import pandas as pd

_DASHBOARD_DIR = Path(__file__).resolve().parents[1] / "dashboard" / "paper-trading"
if str(_DASHBOARD_DIR) not in sys.path:
    sys.path.insert(0, str(_DASHBOARD_DIR))

from data_loader import (  # noqa: E402
    eod_total_asset_series,
    load_asset_history,
)
from pricing import (  # noqa: E402
    load_daily_close_cache,
    revalue_asset_history_tail,
    save_daily_close_cache,
)

# 与真实委托 CSV 一致的表头
_COLUMNS = [
    "order_time",
    "order_id",
    "stock_code",
    "order_type",
    "order_volume",
    "price_type",
    "price",
    "traded_volume",
    "traded_price",
    "commission",
    "stamp_tax",
    "account_cash",
    "account_market_value",
    "order_status",
    "status_msg",
]


def _order(
    time: str,
    order_id: int,
    traded_volume: float,
    cash: float,
    mv: float,
    stock: str = "600000.SH",
) -> dict:
    return {
        "order_time": time,
        "order_id": order_id,
        "stock_code": stock,
        "order_type": "23",
        "order_volume": 100,
        "price_type": "5",
        "price": 0.0,
        "traded_volume": traded_volume,
        "traded_price": 10.0,
        "commission": 5.0,
        "stamp_tax": 0.0,
        "account_cash": cash,
        "account_market_value": mv,
        "order_status": "56",
        "status_msg": "已成",
    }


def _orders_df(rows: list[dict], dates: list[str]) -> pd.DataFrame:
    df = pd.DataFrame(rows, columns=_COLUMNS)
    df["trade_date"] = dates
    for col in (
        "price",
        "traded_volume",
        "traded_price",
        "commission",
        "stamp_tax",
        "account_cash",
        "account_market_value",
    ):
        df[col] = pd.to_numeric(df[col], errors="coerce")
    return df


def test_eod_series_takes_last_filled_of_day():
    """当日有多笔委托时期末值取最后一笔成交委托的现金+市值。"""
    df = _orders_df(
        [
            _order("09:35:00", 1, 100, 90_000, 10_000),
            _order("14:50:00", 2, 0, 95_000, 5_000),  # 废单（未成交）
            _order("10:30:00", 3, 200, 80_000, 20_000),
        ],
        ["20260901", "20260901", "20260901"],
    )
    series = eod_total_asset_series(df)
    assert series.index.tolist() == ["20260901"]
    assert series.iloc[0] == 100_000.0  # 10:30 的成交委托，而非 14:50 的废单


def test_eod_series_falls_back_to_last_row_without_fill():
    """当日全部未成交时退化为最后一行委托的资产快照。"""
    df = _orders_df(
        [
            _order("09:35:00", 1, 0, 90_000, 0),
            _order("10:30:00", 2, 0, 88_000, 0),
        ],
        ["20260901", "20260901"],
    )
    series = eod_total_asset_series(df)
    assert series.iloc[0] == 88_000.0


def test_eod_series_sorts_by_time_not_append_order():
    """行序按 (日期, 时间) 稳定排序，不依赖文件内追加顺序。"""
    df = _orders_df(
        [
            _order("14:00:00", 2, 100, 70_000, 25_000),
            _order("09:40:00", 1, 100, 85_000, 12_000),
        ],
        ["20260901", "20260901"],
    )
    series = eod_total_asset_series(df)
    # 时间排序后当日最后一笔成交是 14:00 的委托（95000），
    # 而非文件中最后追加的 09:40 委托（97000）
    assert series.iloc[0] == 95_000.0


def test_eod_series_skips_zero_asset_rows():
    """缺资金字段的脏行（资产为 0）不参与期末值选取。"""
    df = _orders_df(
        [
            _order("09:40:00", 1, 100, 0, 0),  # 脏行
            _order("10:00:00", 2, 100, 95_000, 5_000),
        ],
        ["20260901", "20260901"],
    )
    series = eod_total_asset_series(df)
    assert series.iloc[0] == 100_000.0


def test_eod_series_empty_for_missing_columns():
    """缺资金字段时返回空 Series。"""
    df = pd.DataFrame({"trade_date": ["20260901"], "traded_volume": [100]})
    assert eod_total_asset_series(df).empty


def _write_orders_csv(base: Path, account: str, date: str, rows: list[dict]) -> None:
    orders_dir = base / account / "order"
    orders_dir.mkdir(parents=True, exist_ok=True)
    df = _orders_df(rows, [date] * len(rows))
    df.drop(columns=["trade_date"]).to_csv(
        orders_dir / f"orders_{date}.csv", index=False
    )


def test_load_asset_history_alignment(tmp_path: Path):
    """宽表对齐全交易日轴：无委托日前值填充、首个交易日前按初始资金补齐。"""
    base = tmp_path / "paper_trading"
    base.mkdir()

    # 账户 a：9/1 与 9/3 有委托，9/2 无委托应沿用前值
    _write_orders_csv(
        base,
        "a_paper",
        "20260901",
        [_order("10:00:00", 1, 100, 99_000, 1_000)],
    )
    _write_orders_csv(
        base,
        "a_paper",
        "20260903",
        [_order("10:00:00", 2, 100, 101_000, 1_500)],
    )
    # 账户 b：仅 9/2 有委托，9/1 应为初始资金
    _write_orders_csv(
        base,
        "b_paper",
        "20260902",
        [_order("10:00:00", 1, 100, 60_000, 20_000)],
    )

    # 账户 c：仅在 config 注册、无委托记录
    (base / "config.json").write_text(
        '{"a_paper": {"initial_cash": 100000.0}, "b_paper": {"initial_cash": 80000.0},'
        ' "c_paper": {"initial_cash": 50000.0}}',
        encoding="utf-8",
    )

    wide, initial_map, stats_df, holdings = load_asset_history(base)

    assert wide.index.tolist() == ["20260901", "20260902", "20260903"]
    assert wide.columns.tolist() == ["a_paper", "b_paper"]
    # a：9/1 期末 100000，9/2 沿用前值，9/3 期末 102500
    assert wide["a_paper"].tolist() == [100_000.0, 100_000.0, 102_500.0]
    # b：9/1 按初始资金补齐，9/2 期末 80000
    assert wide["b_paper"].tolist() == [80_000.0, 80_000.0, 80_000.0]

    assert initial_map == {
        "a_paper": 100_000.0,
        "b_paper": 80_000.0,
        "c_paper": 50_000.0,
    }

    assert stats_df.loc["a_paper", "n_filled"] == 2
    assert stats_df.loc["a_paper", "first_date"] == "20260901"
    assert stats_df.loc["a_paper", "last_date"] == "20260903"
    assert stats_df.loc["c_paper", "n_filled"] == 0
    assert stats_df.loc["c_paper", "first_date"] == ""

    # 当前持仓：a 两日各买 100 股同一标的，现金取最后一笔成交快照
    assert holdings["a_paper"]["volumes"] == {"600000.SH": 200.0}
    assert holdings["a_paper"]["cash"] == 101_000.0
    assert holdings["a_paper"]["prices"] == {"600000.SH": 10.0}
    assert holdings["a_paper"]["last_order_date"] == "20260903"
    assert holdings["b_paper"]["volumes"] == {"600000.SH": 100.0}
    assert holdings["c_paper"]["volumes"] == {}


def test_load_asset_history_no_orders(tmp_path: Path):
    """无任何委托记录时宽表为空，但初始资金与统计表仍覆盖全部账户。"""
    base = tmp_path / "paper_trading"
    base.mkdir()
    (base / "config.json").write_text(
        '{"a_paper": {"initial_cash": 100000.0}}', encoding="utf-8"
    )
    wide, initial_map, stats_df, holdings = load_asset_history(base)
    assert wide.empty
    assert initial_map == {"a_paper": 100_000.0}
    assert stats_df.loc["a_paper", "n_filled"] == 0
    assert holdings["a_paper"]["volumes"] == {}


def test_revalue_asset_history_tail():
    """最后一笔委托后的交易日按 现金+持仓×收盘价 重估，缺价日沿用前值。"""
    wide = pd.DataFrame(
        {
            "a_paper": [100_000.0, 100_000.0, 100_500.0, 100_500.0, 100_500.0],
        },
        index=["20260901", "20260902", "20260903", "20260904", "20260905"],
    )
    holdings = {
        "a_paper": {
            "cash": 50_500.0,
            "volumes": {"600000.SH": 1000.0, "000001.SZ": 500.0, "510300.SH": 100.0},
            "prices": {"600000.SH": 9.0, "000001.SZ": 20.0, "510300.SH": 4.0},
            "last_order_date": "20260903",
        },
        # 未在 wide 列中的账户应被忽略
        "ghost_paper": {
            "cash": 1.0,
            "volumes": {"600000.SH": 1.0},
            "last_order_date": "20260901",
        },
    }
    # 600000.SH 仅有 9/5 收盘价：9/4 缺价沿用前值，9/5 重估；
    # 510300.SH 无任何收盘价（如 ETF 行情缺失）：按最近成交价 4.0 冻结
    closes = {
        "600000.SH": {"20260905": 11.0},
        "000001.SZ": {"20260903": 20.0, "20260904": 19.0, "20260905": 21.0},
    }
    out = revalue_asset_history_tail(wide, holdings, closes)
    assert out["a_paper"].tolist() == [
        100_000.0,  # 委托日之前
        100_000.0,
        100_500.0,  # 最后委托日：保留引擎快照
        100_500.0,  # 600000.SH 缺 9/4 收盘价：沿用前值
        50_500.0 + 1000 * 11.0 + 500 * 21.0 + 100 * 4.0,  # 9/5 重估
    ]
    # 原表不被修改
    assert wide["a_paper"].tolist() == [100_000.0] * 2 + [100_500.0] * 3


def test_revalue_asset_history_tail_no_closes():
    """无收盘价时原样返回；无持仓的账户不受影响。"""
    wide = pd.DataFrame(
        {"a_paper": [100_000.0, 100_100.0]}, index=["20260901", "20260902"]
    )
    holdings = {"a_paper": {"cash": 1.0, "volumes": {}, "last_order_date": "20260901"}}
    out = revalue_asset_history_tail(wide, holdings, {})
    assert out["a_paper"].tolist() == [100_000.0, 100_100.0]
    out2 = revalue_asset_history_tail(
        wide,
        {"a_paper": {"cash": 1.0, "volumes": {"X.SH": 1.0}, "last_order_date": ""}},
        {},
    )
    assert out2["a_paper"].tolist() == [100_000.0, 100_100.0]


def test_daily_close_cache_roundtrip(tmp_path: Path):
    """日收盘价缓存保存与读取往返一致。"""
    closes = {"600000.SH": {"20260901": 10.0, "20260902": 10.5}}
    save_daily_close_cache(tmp_path, closes, "20260901", "20260902")
    cached = load_daily_close_cache(tmp_path)
    assert cached["closes"] == closes
    assert cached["start_time"] == "20260901"
    assert cached["end_time"] == "20260902"
    assert cached["timestamp"]
    # 无缓存文件时返回空字典
    assert load_daily_close_cache(tmp_path / "nonexistent") == {}
