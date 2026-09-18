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
from time import sleep
from typing import Any

import pandas as pd

from data_loader import derive_positions_with_cost

logger = logging.getLogger(__name__)


def _prices_dir(data_dir: Path) -> Path:
    """返回价格缓存目录。"""
    return data_dir / "prices"


def load_price_cache_raw(data_dir: Path, date_str: str | None = None) -> dict[str, Any]:
    """加载价格缓存文件的原始内容（含 ``timestamp``、``type``、``prices``）。

    Args:
        data_dir: 模拟交易数据根目录。
        date_str: 日期字符串 ``YYYYMMDD``；为 ``None`` 时读取 ``current.json``。

    Returns:
        缓存文件的原始字典；读取失败或文件不存在时返回空字典。
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
        return data if isinstance(data, dict) else {}
    except Exception:
        logger.exception("读取价格缓存失败: %s", path)
        return {}


def load_price_cache(data_dir: Path, date_str: str | None = None) -> dict[str, float]:
    """加载价格缓存文件中的价格映射。

    Args:
        data_dir: 模拟交易数据根目录。
        date_str: 日期字符串 ``YYYYMMDD``；为 ``None`` 时读取 ``current.json``。

    Returns:
        股票代码到价格的映射字典。
    """
    data = load_price_cache_raw(data_dir, date_str)
    if not isinstance(data, dict):
        return {}
    prices = (
        data.get("prices", data) if isinstance(data.get("prices", {}), dict) else data
    )
    return {
        k: float(v)
        for k, v in prices.items()
        if isinstance(v, (int, float, str)) and v != ""
    }


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


def _server_headers(api_key: str) -> dict[str, str]:
    """构建访问 qmt-server 的请求头。"""
    headers = {"Accept": "application/json"}
    if api_key:
        headers["X-API-Key"] = api_key
    return headers


def _no_proxy_opener():
    """构建禁用系统代理的 URL opener。

    局域网请求禁用系统代理：进程若继承 http_proxy 且 no_proxy 通配符
    （如 ``*.zicp.fun``）不被 urllib 识别，请求会被转发到代理并超时。
    """
    import urllib.request

    return urllib.request.build_opener(urllib.request.ProxyHandler({}))


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
    headers = _server_headers(api_key)
    opener = _no_proxy_opener()

    data = None
    last_exc: Exception | None = None
    for attempt in range(2):  # 拥塞时偶发超时，重试一次
        req = urllib.request.Request(url, headers=headers, method="GET")
        try:
            with opener.open(req, timeout=30) as resp:
                data = json.loads(resp.read().decode("utf-8"))
            break
        except urllib.error.HTTPError as exc:
            error_body = exc.read().decode("utf-8", errors="ignore")
            logger.error("获取行情 HTTP 错误 %s: %s", exc.code, error_body)
            raise RuntimeError(f"获取行情失败: HTTP {exc.code}") from exc
        except Exception as exc:
            last_exc = exc
            if attempt == 0:
                logger.warning("获取行情超时，5 秒后重试: %s", exc)

    if data is None:
        logger.error("获取行情失败（已重试）: %s", last_exc)
        raise RuntimeError(
            "获取行情失败: "
            f"{last_exc}（qmt-server 无响应；每个交易日 11:19-12:34 "
            "策略集中迭代期间服务易拥塞，可稍后重试）"
        ) from last_exc

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


# ── 每日收盘价（用于无委托日的持仓重估）────────────────────────────


def fetch_daily_closes(
    host: str,
    port: int,
    api_key: str,
    stock_codes: list[str],
    start_time: str,
    end_time: str,
) -> dict[str, dict[str, float]]:
    """通过 qmt-server 的 ``/api/market/market_data_ex`` 拉取日收盘价。

    Args:
        stock_codes: 股票代码列表（内部按 50 只一批，规避 URL 长度限制）。
        start_time / end_time: ``YYYYMMDD``。

    Returns:
        ``{股票代码: {YYYYMMDD: close}}``；窗口内无数据的代码不出现在结果中。
    """
    import urllib.error
    import urllib.request

    closes: dict[str, dict[str, float]] = {}
    for i in range(0, len(stock_codes), 50):
        batch = stock_codes[i : i + 50]
        stocks_param = urllib.request.quote(",".join(batch))
        url = (
            f"http://{host}:{port}/api/market/market_data_ex"
            f"?stocks={stocks_param}&period=1d&fields=close"
            f"&start_time={start_time}&end_time={end_time}"
        )
        req = urllib.request.Request(
            url, headers=_server_headers(api_key), method="GET"
        )
        try:
            with _no_proxy_opener().open(req, timeout=60) as resp:
                data = json.loads(resp.read().decode("utf-8"))
        except urllib.error.HTTPError as exc:
            error_body = exc.read().decode("utf-8", errors="ignore")
            logger.error("拉取日收盘价 HTTP 错误 %s: %s", exc.code, error_body)
            raise RuntimeError(f"拉取日收盘价失败: HTTP {exc.code}") from exc
        bars = data.get("data", {}) if isinstance(data, dict) else {}
        for code, rows in bars.items():
            for row in rows or []:
                date = str(row.get("index", ""))
                close = row.get("close")
                if len(date) == 8 and isinstance(close, (int, float)) and close > 0:
                    closes.setdefault(code, {})[date] = float(close)
    return closes


def request_history_download(
    host: str,
    port: int,
    api_key: str,
    stock_codes: list[str],
    start_time: str,
    end_time: str,
    batch_size: int = 10,
) -> None:
    """触发 qmt-server 批量下载缺失的日 K 历史。

    下载接口对股票列表逐只串行执行、请求阻塞到整批完成，一次性发送
    全部代码会长时间阻塞甚至超时，因此分小批发送（每批等待完成）；
    单批超时重试一次——下载是幂等的增量操作，重复触发无副作用。
    """
    import urllib.error
    import urllib.request

    for i in range(0, len(stock_codes), batch_size):
        batch = stock_codes[i : i + batch_size]
        body = json.dumps(
            {
                "stock_list": batch,
                "period": "1d",
                "start_time": start_time,
                "end_time": end_time,
            }
        ).encode("utf-8")
        for attempt in range(2):
            req = urllib.request.Request(
                f"http://{host}:{port}/api/download/history_data2",
                data=body,
                headers={
                    **_server_headers(api_key),
                    "Content-Type": "application/json",
                },
                method="POST",
            )
            try:
                with _no_proxy_opener().open(req, timeout=300) as resp:
                    json.loads(resp.read().decode("utf-8"))
                break
            except urllib.error.HTTPError as exc:
                error_body = exc.read().decode("utf-8", errors="ignore")
                logger.error("触发日 K 下载 HTTP 错误 %s: %s", exc.code, error_body)
                raise RuntimeError(f"触发日 K 下载失败: HTTP {exc.code}") from exc
            except Exception as exc:
                if attempt == 0:
                    logger.warning(
                        "日 K 下载批次超时（%d 只，%s~%s），重试: %s",
                        len(batch),
                        batch[0],
                        batch[-1],
                        exc,
                    )
                else:
                    # 放弃该批：缺的代码由 ensure_daily_closes 的轮询兜底上报
                    logger.warning("日 K 下载批次放弃（%d 只）: %s", len(batch), exc)


def ensure_daily_closes(
    host: str,
    port: int,
    api_key: str,
    stock_codes: list[str],
    start_time: str,
    end_time: str,
    wait_secs: int = 240,
) -> tuple[dict[str, dict[str, float]], list[str]]:
    """确保日收盘价可用：触发下载后轮询读取，直至覆盖或超时。

    首轮下载后个别代码可能静默失败（下载返回 ok 但无数据），
    因此停滞时对缺失子集再补 1 轮下载重试。

    Returns:
        ``(closes, missing)``，missing 为重试后仍无任何有效收盘价的代码。
        个别代码长期无数据属正常（退市、ETF 行情缺失等），不阻塞整体。
    """
    if not stock_codes:
        return {}, []

    closes: dict[str, dict[str, float]] = {}
    pending = list(stock_codes)
    for round_no in range(2):  # 首轮 + 1 轮对缺失子集的补下载
        request_history_download(host, port, api_key, pending, start_time, end_time)
        deadline = datetime.now().timestamp() + wait_secs
        missing_prev, stall = -1, 0
        while True:
            sleep(5)
            try:
                round_closes = fetch_daily_closes(
                    host, port, api_key, pending, start_time, end_time
                )
            except Exception:
                logger.exception("拉取日收盘价失败，5 秒后重试")
                continue
            closes.update(round_closes)
            pending = [c for c in stock_codes if c not in closes]
            if not pending:
                return closes, []
            if len(pending) == missing_prev:
                stall += 1
                if stall >= 3:  # 连续 3 轮无进展，跳出本轮
                    break
            else:
                stall = 0
            missing_prev = len(pending)
            if datetime.now().timestamp() >= deadline:
                logger.warning("日收盘价下载超时，%d 个代码无数据", len(pending))
                break
        if round_no == 0 and pending:
            logger.info(
                "日收盘价首轮停滞，补下载 %d 个缺失代码: %s", len(pending), pending[:10]
            )
    if pending:
        logger.warning("日收盘价最终缺失 %d 个代码: %s", len(pending), pending[:10])
    return closes, pending


def load_daily_close_cache(data_dir: Path) -> dict[str, Any]:
    """加载日收盘价缓存 ``prices/daily_closes.json`` 的原始内容。"""
    path = _prices_dir(data_dir) / "daily_closes.json"
    if not path.exists():
        return {}
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        return data if isinstance(data, dict) else {}
    except Exception:
        logger.exception("读取日收盘价缓存失败: %s", path)
        return {}


def save_daily_close_cache(
    data_dir: Path,
    closes: dict[str, dict[str, float]],
    start_time: str,
    end_time: str,
) -> Path:
    """保存日收盘价缓存到 ``prices/daily_closes.json``。"""
    prices_dir = _prices_dir(data_dir)
    prices_dir.mkdir(parents=True, exist_ok=True)
    path = prices_dir / "daily_closes.json"
    payload = {
        "timestamp": datetime.now().isoformat(),
        "start_time": start_time,
        "end_time": end_time,
        "closes": closes,
    }
    path.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")
    logger.info("已保存 %d 个代码的日收盘价到 %s", len(closes), path)
    return path


def revalue_asset_history_tail(
    wide_df: pd.DataFrame,
    holdings: dict[str, dict[str, Any]],
    closes: dict[str, dict[str, float]],
) -> pd.DataFrame:
    """按 持仓 × 日收盘价 重估无委托日的期末总资产。

    委托日的值来自引擎快照（精确，含费用），仅重估每个账户最后一笔
    委托之后的交易日：``现金 + Σ 持仓量 × 当日价格``。无任何收盘价的
    持仓（如部分 ETF 行情缺失）按最近成交价冻结，与账户详情实时盈亏
    的兜底口径一致；某日其余持仓也缺价时沿用前值，避免总资产被低估。

    Args:
        wide_df: :func:`load_asset_history` 返回的快照宽表（已前向填充）。
        holdings: ``{account_id: {"cash", "volumes": {code: 量},
        "prices": {code: 最近成交价}, "last_order_date"}}``。
        closes: ``{股票代码: {YYYYMMDD: close}}``。

    Returns:
        重估后的宽表副本；``closes`` 为空时原样返回。
    """
    if wide_df.empty or not closes:
        return wide_df

    dates = wide_df.index
    revalued = wide_df.copy()
    for aid, holding in holdings.items():
        if aid not in revalued.columns:
            continue
        volumes: dict[str, Any] = holding.get("volumes") or {}
        if not volumes:
            continue
        last_order_date = str(holding.get("last_order_date", ""))
        tail_mask = dates > last_order_date  # YYYYMMDD 字典序即时间序
        if not tail_mask.any():
            continue

        fallback_prices: dict[str, Any] = holding.get("prices") or {}
        cash = float(holding.get("cash") or 0.0)
        total = pd.Series(0.0, index=dates)
        valid = pd.Series(True, index=dates)
        for code, volume in volumes.items():
            series = closes.get(code)
            if series:
                close_s = (
                    pd.Series(series)
                    .pipe(lambda s: s[~s.index.duplicated(keep="last")])
                    .sort_index()
                    .reindex(dates)
                    .ffill()  # 停牌/缺数日沿用最近收盘价
                )
            else:
                # 无收盘价序列的持仓（如部分 ETF）：按最近成交价冻结
                price = fallback_prices.get(code)
                close_s = pd.Series(
                    float(price)
                    if isinstance(price, (int, float)) and price > 0
                    else float("nan"),
                    index=dates,
                )
            total = total + close_s.fillna(0.0) * float(volume)
            valid = valid & close_s.notna()

        mask = tail_mask & valid
        revalued[aid] = revalued[aid].where(~mask, cash + total)
    return revalued


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


def _is_current_cache_fresh(data: dict[str, Any]) -> bool:
    """判断 ``current.json`` 的价格是否为当日的盘中/收盘缓存。"""
    timestamp = data.get("timestamp", "")
    if not isinstance(timestamp, str) or not timestamp:
        return True  # 旧格式无时间戳，直接视为可用
    try:
        ts_date = datetime.fromisoformat(timestamp).date()
        return ts_date == datetime.now().date()
    except ValueError:
        return True


def get_price_source_label(data_dir: Path) -> str:
    """返回当前正在使用的价格源标签，便于仪表盘展示。"""
    current_raw = load_price_cache_raw(data_dir, None)
    if current_raw and _is_current_cache_fresh(current_raw):
        cache_type = current_raw.get("type", "")
        if cache_type == "close":
            return "收盘价"
        return "最新价"

    date_str = datetime.now().strftime("%Y%m%d")
    if (_prices_dir(data_dir) / f"{date_str}.json").exists():
        return "收盘价"

    # 兜底：尝试找最近一日的收盘价缓存
    prices_dir = _prices_dir(data_dir)
    close_files = sorted(
        [p for p in prices_dir.glob("*.json") if p.stem != "current"],
        reverse=True,
    )
    if close_files:
        return "收盘价"

    return "最新可用价"


def resolve_prices(
    data_dir: Path,
    stock_codes: list[str],
    account_config: dict[str, Any] | None = None,
    date_str: str | None = None,
) -> dict[str, float]:
    """为给定股票列表解析最优可用价格。

    优先级：
    1. 当日 ``current.json``（盘中最新价，不限制交易时段，只要缓存是当日的）
    2. 当日收盘价 ``YYYYMMDD.json``
    3. 账户配置 ``static_prices``
    """
    if date_str is None:
        date_str = datetime.now().strftime("%Y%m%d")

    prices: dict[str, float] = {}

    # 优先使用当日的 current.json（含交易时段盘中价或收盘后刚获取的最新价）
    current_raw = load_price_cache_raw(data_dir, None)
    if current_raw and _is_current_cache_fresh(current_raw):
        current = current_raw.get("prices", current_raw)
        if isinstance(current, dict):
            for code in stock_codes:
                if code in current:
                    prices[code] = float(current[code])

    # 收盘价作为补充及盘后主价格
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

    # 可用资金取最近一条已成交委托的 account_cash 快照。
    # 不能用任意最后一条：废单/撤单行记录的是引擎当刻（可能已被重置清零的）
    # 状态快照，曾把重置后的 initial_cash 当作可用资金，总资产凭空翻倍
    filled_orders = orders_df[orders_df["traded_volume"].fillna(0) > 0].sort_values(
        by=["trade_date", "order_time"], na_position="first"
    )
    cash_series = (
        pd.to_numeric(filled_orders["account_cash"], errors="coerce").dropna()
        if "account_cash" in filled_orders.columns
        else pd.Series(dtype=float)
    )
    if not cash_series.empty:
        cash = float(cash_series.iloc[-1])
        last_filled = filled_orders.loc[cash_series.index[-1]]
    else:
        cash = float(initial_cash)
        last_filled = None

    # 用最近一条已成交委托的 account_market_value 作为持仓模型选择参考
    if last_filled is not None and pd.notna(last_filled.get("account_market_value")):
        reference_market_value = float(last_filled["account_market_value"])
    else:
        reference_market_value = 0.0

    positions = derive_positions_with_cost(
        orders_df,
        initial_cash=initial_cash,
        reference_market_value=float(reference_market_value),
    )
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

    # 如果提供了基础摘要，用其中的 total_trades 等字段补充；
    # 尚无 summary.json 的账户（仅 config 注册）补 0，避免显示 NaN
    if base_summaries_df is not None and not base_summaries_df.empty:
        base = base_summaries_df.set_index("account_id")
        live_df = live_df.set_index("account_id")
        for col in ["total_trades", "total_commission", "total_stamp_tax"]:
            if col in base.columns:
                live_df[col] = live_df.index.map(base[col].to_dict()).fillna(0)
        live_df = live_df.reset_index()

    return live_df
