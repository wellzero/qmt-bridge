"""模拟交易数据加载工具。

直接从 ``data/paper_trading`` 目录读取 ``config.json``、``summary.json`` 和
``order/orders_YYYYMMDD.csv``，无需启动 qmt-server。
"""

from __future__ import annotations

import json
import logging
from pathlib import Path
from typing import Any

import pandas as pd

logger = logging.getLogger(__name__)


def _default_data_dir() -> Path:
    """根据本文件位置推导默认模拟交易数据目录。"""
    # dashboard/paper-trading/data_loader.py -> project root
    return Path(__file__).resolve().parents[2] / "data" / "paper_trading"


def resolve_data_dir(override: str | None = None) -> Path:
    """解析要使用的模拟交易数据目录。"""
    if override:
        path = Path(override)
    else:
        env = __import__("os").getenv("PAPER_TRADING_DATA_DIR")
        path = Path(env) if env else _default_data_dir()
    return path.expanduser().resolve()


def load_config(data_dir: Path) -> dict[str, Any]:
    """加载全局账户配置 ``config.json``。"""
    config_path = data_dir / "config.json"
    if not config_path.exists():
        return {}
    try:
        return json.loads(config_path.read_text(encoding="utf-8"))
    except Exception:
        logger.exception("读取配置失败: %s", config_path)
        return {}


def list_account_ids(data_dir: Path) -> list[str]:
    """列出所有存在业绩文件或委托文件的账户 ID。"""
    if not data_dir.exists():
        return []
    accounts: list[str] = []
    for path in data_dir.iterdir():
        if not path.is_dir():
            continue
        summary = path / "summary" / "summary.json"
        orders_dir = path / "order"
        if summary.exists() or (orders_dir.exists() and any(orders_dir.iterdir())):
            accounts.append(path.name)
    return sorted(accounts)


def load_summary(data_dir: Path, account_id: str) -> dict[str, Any]:
    """加载单个账户的 ``summary.json``。"""
    summary_path = data_dir / account_id / "summary" / "summary.json"
    if not summary_path.exists():
        return {"account_id": account_id}
    try:
        data = json.loads(summary_path.read_text(encoding="utf-8"))
        data.setdefault("account_id", account_id)
        return data
    except Exception:
        logger.exception("读取摘要失败: %s", summary_path)
        return {"account_id": account_id}


def load_all_summaries(data_dir: Path) -> pd.DataFrame:
    """加载所有账户摘要并返回 DataFrame。"""
    rows = []
    for account_id in list_account_ids(data_dir):
        rows.append(load_summary(data_dir, account_id))
    if not rows:
        return pd.DataFrame()
    df = pd.DataFrame(rows)
    numeric_cols = [
        "initial_cash",
        "cash",
        "market_value",
        "total_asset",
        "total_pnl",
        "total_return_rate",
        "realized_pnl",
        "unrealized_pnl",
        "total_trades",
        "total_commission",
        "total_stamp_tax",
    ]
    for col in numeric_cols:
        if col in df.columns:
            df[col] = pd.to_numeric(df[col], errors="coerce")
    return df


def _read_single_csv(csv_path: Path) -> pd.DataFrame:
    """读取单个委托 CSV，并自动识别表头字段。"""
    try:
        df = pd.read_csv(csv_path, dtype=str, keep_default_na=False)
    except Exception:
        logger.exception("读取 CSV 失败: %s", csv_path)
        return pd.DataFrame()
    if df.empty:
        return df

    # 根据文件名推断日期
    date_str = csv_path.stem.replace("orders_", "")
    df["trade_date"] = date_str

    # 数值字段转换
    numeric_cols = [
        "price",
        "traded_price",
        "order_volume",
        "traded_volume",
        "commission",
        "stamp_tax",
        "account_cash",
        "account_market_value",
    ]
    for col in numeric_cols:
        if col in df.columns:
            df[col] = pd.to_numeric(df[col], errors="coerce")

    # 委托类型可读标签
    type_map = {"23": "买入", "24": "卖出"}
    if "order_type" in df.columns:
        df["order_type_label"] = (
            df["order_type"].astype(str).map(type_map).fillna("其他")
        )

    # 成交状态可读标签（以 status_msg 为准）
    if "status_msg" in df.columns:
        df["status"] = df["status_msg"]

    return df


def load_all_orders(data_dir: Path, account_id: str) -> pd.DataFrame:
    """加载某账户所有日期的委托记录。

    部分模拟交易引擎会将历史委托重复写入新的 ``orders_YYYYMMDD.csv``，
    因此按 ``order_id`` 去重，保留最早出现的那一行（其 ``trade_date`` 由最早的文件名推断，最为准确）。
    """
    orders_dir = data_dir / account_id / "order"
    if not orders_dir.exists():
        return pd.DataFrame()

    frames: list[pd.DataFrame] = []
    for csv_path in sorted(orders_dir.glob("orders_*.csv")):
        df = _read_single_csv(csv_path)
        if not df.empty:
            frames.append(df)

    if not frames:
        return pd.DataFrame()
    combined = pd.concat(frames, ignore_index=True)

    if "order_id" in combined.columns:
        # 保留首次出现，确保 trade_date 取最早的文件名日期
        combined = combined.drop_duplicates(subset=["order_id"], keep="first")

    return combined.reset_index(drop=True)


def derive_positions(orders_df: pd.DataFrame) -> pd.DataFrame:
    """根据委托记录推导当前持仓（仅用于展示，可能与实际持仓有偏差）。"""
    if orders_df.empty or "stock_code" not in orders_df.columns:
        return pd.DataFrame()

    positions = derive_positions_with_cost(orders_df)
    if positions.empty:
        return positions
    return positions[
        [
            "stock_code",
            "volume",
            "traded_price",
            "market_value",
            "trade_date",
            "order_time",
        ]
    ]


def _detect_daily_reset(orders_df: pd.DataFrame, initial_cash: float) -> bool:
    """检测账户是否在最新交易日重置为初始资金。

    部分策略（如按日独立回测/调仓）每天都会把前一日持仓清空、
    现金恢复到 ``initial_cash``，此时应仅使用最新交易日的委托推导持仓。
    """
    if orders_df.empty or "trade_date" not in orders_df.columns:
        return False

    dates = sorted(orders_df["trade_date"].unique())
    if len(dates) <= 1:
        return False

    latest = orders_df[orders_df["trade_date"] == dates[-1]].sort_values(
        by=["trade_date", "order_time"]
    )
    if latest.empty:
        return False

    first = latest.iloc[0]
    order_type = str(first.get("order_type", ""))
    cash = float(first.get("account_cash", 0) or 0)
    volume = float(first.get("traded_volume", 0) or 0)
    price = float(first.get("traded_price", 0) or 0)
    commission = float(first.get("commission", 0) or 0)
    stamp_tax = float(first.get("stamp_tax", 0) or 0)

    if order_type == "23":  # 买入
        pre_trade_cash = cash + volume * price + commission
    elif order_type == "24":  # 卖出
        pre_trade_cash = cash - volume * price + commission + stamp_tax
    else:
        return False

    threshold = max(initial_cash * 0.05, 1000.0)
    return abs(pre_trade_cash - initial_cash) < threshold


def derive_positions_with_cost(
    orders_df: pd.DataFrame, initial_cash: float | None = None
) -> pd.DataFrame:
    """根据委托记录推导当前持仓，并计算成本均价与成本基数。

    Args:
        orders_df: 委托记录 DataFrame。
        initial_cash: 初始资金；提供时用于检测每日重置型账户，
            若检测到重置则仅使用最新交易日的委托推导持仓。

    Returns:
        DataFrame 列：``stock_code``、``volume``、``avg_cost``、``cost_basis``、
        ``traded_price``、``market_value``、``trade_date``、``order_time``。
    """
    if orders_df.empty or "stock_code" not in orders_df.columns:
        return pd.DataFrame()

    df = orders_df.copy()
    if initial_cash is not None and _detect_daily_reset(df, float(initial_cash)):
        latest_date = sorted(df["trade_date"].unique())[-1]
        df = df[df["trade_date"] == latest_date]

    buy_mask = df["order_type"].astype(str) == "23"
    sell_mask = df["order_type"].astype(str) == "24"

    buy_rows = df[buy_mask].copy()
    sell_rows = df[sell_mask].copy()

    # 买入成本与数量
    buy_cost = (
        buy_rows.assign(
            cost=buy_rows["traded_volume"].fillna(0)
            * buy_rows["traded_price"].fillna(0)
        )
        .groupby("stock_code")
        .agg({"traded_volume": "sum", "cost": "sum"})
        .rename(columns={"traded_volume": "buy_volume", "cost": "buy_cost"})
    )

    sells = sell_rows.groupby("stock_code")["traded_volume"].sum().rename("sell_volume")

    positions = pd.concat([buy_cost, sells], axis=1).fillna(0)
    positions["volume"] = positions["buy_volume"] - positions["sell_volume"]
    positions = positions[positions["volume"] > 0].reset_index()

    positions["avg_cost"] = (positions["buy_cost"] / positions["buy_volume"]).round(4)
    positions["cost_basis"] = positions["avg_cost"] * positions["volume"]

    # 最近成交价作为参考市值
    latest = (
        df.sort_values(by=["trade_date", "order_time"])
        .groupby("stock_code")
        .last()[["traded_price", "trade_date", "order_time"]]
        .reset_index()
    )
    positions = positions.merge(latest, on="stock_code", how="left")
    positions["market_value"] = positions["volume"] * positions["traded_price"]
    return positions[
        [
            "stock_code",
            "volume",
            "avg_cost",
            "cost_basis",
            "traded_price",
            "market_value",
            "trade_date",
            "order_time",
        ]
    ]
