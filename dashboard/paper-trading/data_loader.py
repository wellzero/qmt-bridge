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
    """加载某账户所有日期的委托记录。"""
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
    return combined


def derive_positions(orders_df: pd.DataFrame) -> pd.DataFrame:
    """根据委托记录推导当前持仓（仅用于展示，可能与实际持仓有偏差）。"""
    if orders_df.empty or "stock_code" not in orders_df.columns:
        return pd.DataFrame()

    buy_mask = orders_df["order_type"].astype(str) == "23"
    sell_mask = orders_df["order_type"].astype(str) == "24"

    buys = (
        orders_df[buy_mask]
        .groupby("stock_code")["traded_volume"]
        .sum()
        .rename("buy_volume")
    )
    sells = (
        orders_df[sell_mask]
        .groupby("stock_code")["traded_volume"]
        .sum()
        .rename("sell_volume")
    )

    positions = pd.concat([buys, sells], axis=1).fillna(0)
    positions["volume"] = positions["buy_volume"] - positions["sell_volume"]
    positions = positions[positions["volume"] > 0].reset_index()

    # 最近成交价作为参考市值
    latest = (
        orders_df.sort_values(by=["trade_date", "order_time"])
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
            "traded_price",
            "market_value",
            "trade_date",
            "order_time",
        ]
    ]
