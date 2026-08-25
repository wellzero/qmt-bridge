"""模拟交易数据加载工具。

直接从 ``data/paper_trading`` 目录读取 ``config.json``、``summary.json`` 和
``order/orders_YYYYMMDD.csv``，无需启动 qmt-server。
"""

from __future__ import annotations

import json
import logging
import re
import shutil
from datetime import datetime
from pathlib import Path
from typing import Any

import pandas as pd

logger = logging.getLogger(__name__)

# 账户 ID 仅允许字母、数字、下划线、连字符，防止路径穿越
_ACCOUNT_ID_PATTERN = re.compile(r"^[A-Za-z0-9_-]+$")

# 新建账户的默认交易参数，与现有账户配置保持一致
DEFAULT_ACCOUNT_CONFIG: dict[str, Any] = {
    "account_type": 2,
    "commission_rate": 0.0003,
    "min_commission": 5.0,
    "stamp_tax_rate": 0.0005,
    "slippage": 0.0,
    "price_source": "fallback",
    "static_prices": {},
    "auto_download_prices": True,
    "partial_fill_enabled": False,
    "enabled": True,
}


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


def _save_config(data_dir: Path, config: dict[str, Any]) -> None:
    """写回全局账户配置 ``config.json``。"""
    config_path = data_dir / "config.json"
    config_path.write_text(
        json.dumps(config, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )


def _validate_account_id(account_id: str) -> str:
    """校验账户 ID 合法性，返回去除首尾空白后的 ID。"""
    account_id = account_id.strip()
    if not account_id or not _ACCOUNT_ID_PATTERN.match(account_id):
        raise ValueError(
            f"无效的账户 ID：{account_id!r}（仅允许字母、数字、下划线、连字符）"
        )
    return account_id


def create_account(
    data_dir: Path, account_id: str, initial_cash: float = 100_000.0
) -> Path:
    """手动创建模拟交易账户。

    会创建 ``<account_id>/order``、``<account_id>/summary`` 目录和初始
    ``summary.json``，并在 ``config.json`` 中追加账户配置。

    Raises:
        ValueError: 账户 ID 无效或账户已存在。
    """
    account_id = _validate_account_id(account_id)
    account_dir = data_dir / account_id
    config = load_config(data_dir)
    if account_dir.exists() or account_id in config:
        raise ValueError(f"账户已存在：{account_id}")

    (account_dir / "order").mkdir(parents=True)
    (account_dir / "summary").mkdir(parents=True)

    summary = {
        "account_id": account_id,
        "initial_cash": float(initial_cash),
        "cash": float(initial_cash),
        "market_value": 0.0,
        "total_asset": float(initial_cash),
        "total_pnl": 0.0,
        "total_return_rate": 0.0,
        "realized_pnl": 0.0,
        "unrealized_pnl": 0.0,
        "total_trades": 0,
        "total_commission": 0.0,
        "total_stamp_tax": 0.0,
    }
    (account_dir / "summary" / "summary.json").write_text(
        json.dumps(summary, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )

    config[account_id] = {
        "account_id": account_id,
        "initial_cash": float(initial_cash),
        **DEFAULT_ACCOUNT_CONFIG,
    }
    _save_config(data_dir, config)
    logger.info("已创建模拟账户 %s，初始资金 %.2f", account_id, initial_cash)
    return account_dir


def remove_account(
    data_dir: Path, account_id: str, trash_dir: Path | None = None
) -> Path | None:
    """移除模拟交易账户。

    从 ``config.json`` 删除账户配置；账户数据目录不直接删除，
    按项目规范移入 ``.trash/``（文件名附加时间戳），可手动恢复。

    Returns:
        数据目录移入 ``.trash/`` 后的路径；若目录本就不存在则返回 None。
    """
    account_id = _validate_account_id(account_id)
    account_dir = data_dir / account_id

    config = load_config(data_dir)
    if account_id in config:
        del config[account_id]
        _save_config(data_dir, config)

    if not account_dir.exists():
        logger.info("账户 %s 无数据目录，仅删除配置", account_id)
        return None

    if trash_dir is None:
        trash_dir = Path(__file__).resolve().parents[2] / ".trash"
    trash_dir.mkdir(parents=True, exist_ok=True)
    timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    target = trash_dir / f"paper_trading_{account_id}.{timestamp}"
    shutil.move(str(account_dir), str(target))
    logger.info("已删除模拟账户 %s，数据移入 %s", account_id, target)
    return target


def list_account_ids(data_dir: Path) -> list[str]:
    """列出所有账户 ID。

    包括存在业绩文件或委托文件的账户，以及仅在 ``config.json`` 中注册、
    尚未产生任何委托的账户（如刚启动的实盘因子策略），
    使仪表盘在无交易时也能展示这些账户。
    """
    if not data_dir.exists():
        return []
    accounts: set[str] = set()
    for path in data_dir.iterdir():
        if not path.is_dir():
            continue
        summary = path / "summary" / "summary.json"
        orders_dir = path / "order"
        if summary.exists() or (orders_dir.exists() and any(orders_dir.iterdir())):
            accounts.add(path.name)

    # config.json 中注册但还没有数据目录的账户（ID 合法且未禁用）
    for account_id, account_config in load_config(data_dir).items():
        if isinstance(account_config, dict) and not account_config.get("enabled", True):
            continue
        if _ACCOUNT_ID_PATTERN.match(account_id):
            accounts.add(account_id)

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


def _derive_positions_core(orders_df: pd.DataFrame) -> pd.DataFrame:
    """根据委托记录计算持仓的核心逻辑（买入 - 卖出）。"""
    if orders_df.empty or "stock_code" not in orders_df.columns:
        return pd.DataFrame()

    buy_mask = orders_df["order_type"].astype(str) == "23"
    sell_mask = orders_df["order_type"].astype(str) == "24"

    buy_rows = orders_df[buy_mask].copy()
    sell_rows = orders_df[sell_mask].copy()

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

    if positions.empty:
        return pd.DataFrame()

    positions["avg_cost"] = (positions["buy_cost"] / positions["buy_volume"]).round(4)
    positions["cost_basis"] = positions["avg_cost"] * positions["volume"]

    # "最近成交"只统计已成交委托：废单（如可用资金不足）的 traded_price 为 0，
    # 若纳入会把持仓市值错误压为 0，进而干扰累积型/每日调仓型模型选择
    filled_df = orders_df[orders_df["traded_volume"].fillna(0) > 0]
    latest = (
        filled_df.sort_values(by=["trade_date", "order_time"])
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


def _market_value_sum(positions: pd.DataFrame) -> float:
    """汇总持仓 DataFrame 的市值，空表返回 0。"""
    return float(positions["market_value"].sum()) if not positions.empty else 0.0


def _select_positions_by_cash(
    filled: pd.DataFrame, initial_cash: float, tolerance: float = 1.0
) -> pd.DataFrame | None:
    """用资金流水精确反推截断点，返回模型；无法精确匹配时返回 None。

    引擎重置后资金回到 ``initial_cash``，因此真实窗口满足：
    ``initial_cash + 窗口内资金流出流入 = 最后一条委托的 account_cash``。
    资金流水只依赖成交价与费用（与估值价格无关），能精确到元级；
    若任何截断点都无法对上（如 CSV 缺失引擎已执行的委托），返回 None
    交由市值比较法兜底。
    """
    if "account_cash" not in filled.columns or filled.empty:
        return None

    volume = filled["traded_volume"].fillna(0)
    price = filled["traded_price"].fillna(0)
    fee = filled["commission"].fillna(0) + filled["stamp_tax"].fillna(0)
    is_buy = filled["order_type"].astype(str) == "23"

    flow = pd.Series(0.0, index=filled.index)
    flow[is_buy] = -(volume * price)[is_buy] - fee[is_buy]
    flow[~is_buy] = (volume * price)[~is_buy] - fee[~is_buy]

    last_cash = pd.to_numeric(filled["account_cash"], errors="coerce").dropna()
    if last_cash.empty:
        return None
    target = float(last_cash.iloc[-1])

    # 后缀和：suffix[i] = 第 i 行及之后的所有资金流
    suffix = flow.iloc[::-1].cumsum()[::-1]

    best_i: int | None = None
    best_diff = float("inf")
    for i in range(len(filled) + 1):
        model_cash = initial_cash + (float(suffix.iloc[i]) if i < len(filled) else 0.0)
        diff = abs(model_cash - target)
        if diff < best_diff:
            best_diff, best_i = diff, i

    if best_diff > tolerance:
        return None

    model = _derive_positions_core(filled.iloc[best_i:])
    if best_i and best_i > 0:
        boundary = filled.iloc[best_i - 1]
        logger.info(
            "资金流水反推：截断点在 %s %s 之后（第 %d/%d 笔），现金偏差 %.2f 元",
            boundary["trade_date"],
            boundary["order_time"],
            best_i,
            len(filled),
            best_diff,
        )
    return model


def derive_positions_with_cost(
    orders_df: pd.DataFrame,
    initial_cash: float | None = None,
    reference_market_value: float | None = None,
) -> pd.DataFrame:
    """根据委托记录推导当前持仓，并计算成本均价与成本基数。

    引擎状态重置（服务重启后按 config 重建账户、策略重注册账户、手工重置）
    不会回放历史委托，且重置边界常在盘中，CSV 中重置前的买入就成了
    "幽灵持仓"。选择模型的策略（按优先级）：

    1. 资金流水法：提供 ``initial_cash`` 且委托含 ``account_cash`` 时，
       在每笔已成交委托处尝试截断，找 ``initial_cash + 窗口资金流`` 与
       最后一条 ``account_cash`` 精确吻合（±1 元）的窗口。
    2. 市值比较法：退而求其次，取"截断点之后累计"模型中市值（按统一的
       最后成交价估值）与 ``reference_market_value`` 最接近的窗口。
    3. 两者都不可用时，退化为全量累积模型。

    Args:
        orders_df: 委托记录 DataFrame。
        initial_cash: 初始资金，用于资金流水反推。
        reference_market_value: 参考市值（引擎 summary 或最近委托的
            account_market_value），用于市值比较法选择截断点。

    Returns:
        DataFrame 列：``stock_code``、``volume``、``avg_cost``、``cost_basis``、
        ``traded_price``、``market_value``、``trade_date``、``order_time``。
    """
    if orders_df.empty or "stock_code" not in orders_df.columns:
        return pd.DataFrame()

    cumulative = _derive_positions_core(orders_df)

    if reference_market_value is None and initial_cash is None:
        return cumulative

    # 按时间排序的已成交委托；截断点 i 表示只保留第 i 行及之后的委托
    filled = (
        orders_df[orders_df["traded_volume"].fillna(0) > 0]
        .sort_values(["trade_date", "order_time"], kind="stable")
        .reset_index(drop=True)
    )
    if len(filled) <= 1:
        return cumulative

    # ── 优先：资金流水精确反推 ──
    if initial_cash is not None:
        cash_model = _select_positions_by_cash(filled, float(initial_cash))
        if cash_model is not None:
            return cash_model

    if reference_market_value is None:
        return cumulative
    ref = float(reference_market_value)

    # ── 兜底：统一价格下市值最接近 ──
    # 统一估值价格：各股票在全部已成交委托中的最后成交价。
    # 各候选模型的 market_value 使用各自窗口内的最后成交价，跨模型不可比，
    # 会把"恰好赶上高价委托的错模型"误判为最优；统一价格后比较只反映股数差异
    global_prices = filled.groupby("stock_code")["traded_price"].last()

    def _model_mv(model: pd.DataFrame) -> float:
        """按统一价格表汇总模型市值，空模型返回 0。"""
        if model.empty:
            return 0.0
        prices = model["stock_code"].map(global_prices).fillna(0)
        return float((model["volume"] * prices).sum())

    # 候选模型：全量累积（i=0）+ 各截断点后的累计（i=1..n，n 为清仓）
    candidates: list[tuple[float, int, str | None, pd.DataFrame]] = [
        (abs(_model_mv(cumulative) - ref), 0, None, cumulative)
    ]
    for i in range(1, len(filled) + 1):
        model = _derive_positions_core(filled.iloc[i:])
        boundary = filled.iloc[i - 1]
        label = f"{boundary['trade_date']} {boundary['order_time']}"
        candidates.append((abs(_model_mv(model) - ref), i, label, model))

    best_diff, best_i, best_label, best_df = min(candidates, key=lambda c: (c[0], c[1]))

    if best_i == 0:
        return cumulative

    logger.info(
        "检测到账户状态重置：全量累积模型市值 %.0f 与参考值 %.0f 偏差过大，"
        "改用 %s 之后的委托推导，市值 %.0f（偏差 %.0f）",
        _model_mv(cumulative),
        ref,
        best_label,
        _model_mv(best_df),
        best_diff,
    )
    return best_df
