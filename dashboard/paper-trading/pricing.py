"""实时盈亏计算工具。

为模拟交易仪表盘提供基于当前价/收盘价的持仓市值与盈亏估算。
价格来源优先级：

1. ``data/paper_trading/prices/current.json`` —— 盘中最新价
2. ``data/paper_trading/prices/YYYYMMDD.json`` —— 当日收盘价
3. 账户配置 ``static_prices`` —— 静态价格
4. 委托记录中最近成交价 —— 兜底
"""

from __future__ import annotations

import json
import logging
from datetime import datetime, time
from pathlib import Path
from typing import Any

import pandas as pd

from data_loader import derive_positions_with_cost

logger = logging.getLogger(__name__)


def _prices_dir(data_dir: Path) -> Path:
    """返回价格缓存目录。"""
    return data_dir / "prices"


def load_price_cache(data_dir: Path, date_str: str | None = None) -> dict[str, float]:
    """加载价格缓存文件。

    Args:
        data_dir: 模拟交易数据根目录。
        date_str: 日期字符串 ``YYYYMMDD``；为 ``None`` 时读取 ``current.json``。

    Returns:
        股票代码到价格的映射字典。
    """
    prices_dir = _prices_dir(data_dir)
    if date_str is None:
        path = prices_dir / "current.json"
    else:
        path = prices_dir / f"{date_str}.json"

    if not path.exists():
        return {}

    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        if not isinstance(data, dict):
            return {}
        prices = (
            data.get("prices", data)
            if isinstance(data.get("prices", {}), dict)
            else data
        )
        return {
            k: float(v)
            for k, v in prices.items()
            if isinstance(v, (int, float, str)) and v != ""
        }
    except Exception:
        logger.exception("读取价格缓存失败: %s", path)
        return {}


def save_price_cache(
    data_dir: Path, prices: dict[str, float], close: bool = False
) -> Path:
    """保存价格缓存到文件。

    Args:
        data_dir: 模拟交易数据根目录。
        prices: 股票代码到价格的映射。
        close: 是否为收盘价；为 ``True`` 时保存为 ``YYYYMMDD.json``，否则 ``current.json``。

    Returns:
        保存的文件路径。
    """
    prices_dir = _prices_dir(data_dir)
    prices_dir.mkdir(parents=True, exist_ok=True)

    if close:
        filename = f"{datetime.now().strftime('%Y%m%d')}.json"
    else:
        filename = "current.json"

    path = prices_dir / filename
    payload = {
        "timestamp": datetime.now().isoformat(),
        "type": "close" if close else "intraday",
        "prices": prices,
    }
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    logger.info("已保存 %d 条价格到 %s", len(prices), path)
    return path


def fetch_prices_from_server(
    host: str, port: int, api_key: str, stock_codes: list[str]
) -> dict[str, float]:
    """通过 qmt-server 的 ``/api/market/full_tick`` 接口获取最新价格。

    Returns:
        成功获取的股票代码到价格的映射。
    """
    import urllib.error
    import urllib.request

    if not stock_codes:
        return {}

    stocks_param = ",".join(stock_codes)
    encoded = urllib.request.quote(stocks_param)
    url = f"http://{host}:{port}/api/market/full_tick?stocks={encoded}"
    headers = {"Accept": "application/json"}
    if api_key:
        headers["X-API-Key"] = api_key

    req = urllib.request.Request(url, headers=headers, method="GET")
    try:
        with urllib.request.urlopen(req, timeout=30) as resp:
            data = json.loads(resp.read().decode("utf-8"))
    except urllib.error.HTTPError as exc:
        error_body = exc.read().decode("utf-8", errors="ignore")
        logger.error("获取行情 HTTP 错误 %s: %s", exc.code, error_body)
        raise RuntimeError(f"获取行情失败: HTTP {exc.code}") from exc
    except Exception as exc:
        logger.exception("获取行情失败")
        raise RuntimeError(f"获取行情失败: {exc}") from exc

    ticks = data.get("data", {}) if isinstance(data, dict) else {}
    prices: dict[str, float] = {}
    for code in stock_codes:
        tick = ticks.get(code)
        if not isinstance(tick, dict):
            continue
        for key in ("lastPrice", "close", "open", "lastprice"):
            price = tick.get(key)
            if isinstance(price, (int, float)) and price > 0:
                prices[code] = float(price)
                break

    return prices


def is_trading_hours(now: datetime | None = None) -> bool:
    """判断当前是否处于 A 股交易时段（简化版，仅按时间判断，不含节假日）。"""
    if now is None:
        now = datetime.now()
    if now.weekday() >= 5:  # 周六、周日
        return False
    t = now.time()
    morning = time(9, 30) <= t <= time(11, 30)
    afternoon = time(13, 0) <= t <= time(15, 0)
    return morning or afternoon


def resolve_prices(
    data_dir: Path,
    stock_codes: list[str],
    account_config: dict[str, Any] | None = None,
    date_str: str | None = None,
) -> dict[str, float]:
    """为给定股票列表解析最优可用价格。

    优先级：
    1. 盘中 ``current.json``（仅在交易时段）
    2. 当日收盘价 ``YYYYMMDD.json``
    3. 账户配置 ``static_prices``
    """
    if date_str is None:
        date_str = datetime.now().strftime("%Y%m%d")

    prices: dict[str, float] = {}

    # 交易时段优先使用盘中最新价
    if is_trading_hours():
        current = load_price_cache(data_dir, None)
        for code in stock_codes:
            if code in current:
                prices[code] = current[code]

    # 收盘价作为盘中补充及盘后主价格
    close = load_price_cache(data_dir, date_str)
    for code in stock_codes:
        if code not in prices and code in close:
            prices[code] = close[code]

    # 账户静态价格兜底
    if account_config:
        static = account_config.get("static_prices", {})
        for code in stock_codes:
            if code not in prices and code in static:
                prices[code] = float(static[code])

    return prices


def calculate_live_pnl(
    orders_df: pd.DataFrame,
    prices: dict[str, float],
    initial_cash: float = 100_000.0,
) -> dict[str, Any]:
    """根据委托记录和当前/收盘价格计算实时盈亏。

    Returns:
        包含 ``cash``、``market_value``、``total_asset``、``realized_pnl``、
        ``unrealized_pnl``、``total_pnl``、``total_return_rate``、``positions`` 的字典。
    """
    if orders_df.empty:
        return {
            "cash": float(initial_cash),
            "market_value": 0.0,
            "total_asset": float(initial_cash),
            "realized_pnl": 0.0,
            "unrealized_pnl": 0.0,
            "total_pnl": 0.0,
            "total_return_rate": 0.0,
            "positions": pd.DataFrame(),
        }

    # 可用资金取最近一条委托记录中的 account_cash
    sorted_orders = orders_df.sort_values(
        by=["trade_date", "order_time"], na_position="first"
    )
    last_cash = sorted_orders["account_cash"].dropna().iloc[-1]
    cash = float(last_cash)

    positions = derive_positions_with_cost(orders_df)
    if positions.empty:
        return {
            "cash": cash,
            "market_value": 0.0,
            "total_asset": cash,
            "realized_pnl": 0.0,
            "unrealized_pnl": 0.0,
            "total_pnl": cash - float(initial_cash),
            "total_return_rate": (cash - float(initial_cash)) / float(initial_cash)
            if initial_cash
            else 0.0,
            "positions": positions,
        }

    positions["current_price"] = positions["stock_code"].map(prices)
    # 无最新价时，用最近成交价兜底
    positions["current_price"] = positions["current_price"].fillna(
        positions["traded_price"]
    )

    positions["market_value"] = positions["volume"] * positions["current_price"]
    positions["unrealized_pnl"] = positions["market_value"] - positions["cost_basis"]

    market_value = float(positions["market_value"].sum())
    unrealized_pnl = float(positions["unrealized_pnl"].sum())
    total_asset = cash + market_value
    total_pnl = total_asset - float(initial_cash)
    realized_pnl = total_pnl - unrealized_pnl

    return {
        "cash": cash,
        "market_value": market_value,
        "total_asset": total_asset,
        "realized_pnl": round(realized_pnl, 4),
        "unrealized_pnl": round(unrealized_pnl, 4),
        "total_pnl": round(total_pnl, 4),
        "total_return_rate": round(total_pnl / float(initial_cash), 6)
        if initial_cash
        else 0.0,
        "positions": positions,
    }


def calculate_all_accounts_live_pnl(
    data_dir: Path,
    config: dict[str, Any],
    date_str: str | None = None,
) -> dict[str, dict[str, Any]]:
    """为所有账户计算实时盈亏。

    Returns:
        ``{account_id: live_pnl_dict}`` 的字典。
    """
    from data_loader import list_account_ids, load_all_orders

    account_ids = list_account_ids(data_dir)
    if not account_ids:
        return {}

    # 汇总所有出现过的股票代码
    all_stock_codes: set[str] = set()
    for aid in account_ids:
        orders = load_all_orders(data_dir, aid)
        if not orders.empty and "stock_code" in orders.columns:
            all_stock_codes.update(orders["stock_code"].dropna().unique())

    # 解析最优价格
    prices = resolve_prices(data_dir, list(all_stock_codes), date_str=date_str)

    results: dict[str, dict[str, Any]] = {}
    for aid in account_ids:
        orders = load_all_orders(data_dir, aid)
        account_config = config.get(aid, {})
        initial_cash = float(account_config.get("initial_cash", 100_000.0))
        results[aid] = calculate_live_pnl(orders, prices, initial_cash)

    return results


def build_live_summaries_df(
    data_dir: Path,
    config: dict[str, Any],
    base_summaries_df: pd.DataFrame | None = None,
) -> pd.DataFrame:
    """基于实时盈亏构建账户摘要 DataFrame，列名与 ``load_all_summaries`` 保持一致。

    Args:
        data_dir: 模拟交易数据根目录。
        config: 账户配置字典。
        base_summaries_df: 从 ``summary.json`` 加载的基础摘要 DataFrame，用于补充缺失字段。

    Returns:
        包含实时数据的账户摘要 DataFrame。
    """
    live_results = calculate_all_accounts_live_pnl(data_dir, config)
    if not live_results:
        return (
            base_summaries_df.copy()
            if base_summaries_df is not None
            else pd.DataFrame()
        )

    rows = []
    for account_id, live in live_results.items():
        rows.append(
            {
                "account_id": account_id,
                "initial_cash": float(
                    config.get(account_id, {}).get("initial_cash", 100_000.0)
                ),
                "cash": live["cash"],
                "market_value": live["market_value"],
                "total_asset": live["total_asset"],
                "total_pnl": live["total_pnl"],
                "total_return_rate": live["total_return_rate"],
                "realized_pnl": live["realized_pnl"],
                "unrealized_pnl": live["unrealized_pnl"],
                "total_trades": 0,
            }
        )

    live_df = pd.DataFrame(rows)

    # 如果提供了基础摘要，用其中的 total_trades 等字段补充
    if base_summaries_df is not None and not base_summaries_df.empty:
        base = base_summaries_df.set_index("account_id")
        live_df = live_df.set_index("account_id")
        for col in ["total_trades", "total_commission", "total_stamp_tax"]:
            if col in base.columns:
                live_df[col] = live_df.index.map(base[col].to_dict())
        live_df = live_df.reset_index()

    return live_df
