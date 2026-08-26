"""账户状态监控计算逻辑。

为「账户状态监控」页面提供纯数据计算，不依赖 streamlit，便于单元测试。
状态判定规则（按优先级从高到低）：

1. 🔴 今日废单：当日存在委托但无成交的委托（如可用资金不足）
2. 🟡 无近期交易：超过 ``active_days`` 天没有任何委托
3. ⚪ 未交易：仅在 ``config.json`` 注册、从未产生委托
4. 🚫 已停用：``config.json`` 中 ``enabled=false``
5. 🟢 今日已交易 / 🟢 近期活跃：其余账户
"""

from __future__ import annotations

import logging
import re
from datetime import datetime
from pathlib import Path
from typing import Any

import pandas as pd

from data_loader import list_account_ids, load_all_orders, load_config, load_summary
from pricing import calculate_live_pnl, resolve_prices

logger = logging.getLogger(__name__)

# 状态常量（前端展示文案）
STATUS_REJECTED_TODAY = "🔴 今日废单"
STATUS_TRADED_TODAY = "🟢 今日已交易"
STATUS_ACTIVE = "🟢 近期活跃"
STATUS_STALE = "🟡 无近期交易"
STATUS_NEVER_TRADED = "⚪ 未交易"
STATUS_DISABLED = "🚫 已停用"

# 展示排序：需要关注的排前面
_STATUS_PRIORITY: dict[str, int] = {
    STATUS_REJECTED_TODAY: 0,
    STATUS_STALE: 1,
    STATUS_NEVER_TRADED: 2,
    STATUS_DISABLED: 3,
    STATUS_TRADED_TODAY: 4,
    STATUS_ACTIVE: 5,
}

# 状态表与今日废单明细的列
STATUS_COLUMNS = [
    "account_id",
    "status",
    "enabled",
    "orders_today",
    "filled_today",
    "rejected_today",
    "rejected_total",
    "position_count",
    "last_trade_date",
    "days_since_trade",
    "last_write",
    "cash",
    "market_value",
    "total_asset",
    "total_pnl",
    "total_return_rate",
]

REJECTED_DETAIL_COLUMNS = [
    "account_id",
    "trade_date",
    "order_time",
    "stock_code",
    "order_volume",
    "status_msg",
]


def _numeric_col(df: pd.DataFrame, col: str) -> pd.Series:
    """取数值列；列缺失或解析失败时按 0 处理。"""
    if col not in df.columns:
        return pd.Series(0.0, index=df.index)
    return pd.to_numeric(df[col], errors="coerce").fillna(0)


def _rejected_mask(orders_df: pd.DataFrame) -> pd.Series:
    """识别废单：有委托量但无成交量（如「可用资金不足」被拒绝的委托）。"""
    if orders_df.empty:
        return pd.Series(False, index=orders_df.index)
    order_volume = _numeric_col(orders_df, "order_volume")
    traded_volume = _numeric_col(orders_df, "traded_volume")
    return (order_volume > 0) & (traded_volume <= 0)


def _last_write_time(account_dir: Path) -> datetime | None:
    """账户目录下委托 CSV 与 summary.json 的最近修改时间。"""
    candidates: list[Path] = []
    orders_dir = account_dir / "order"
    if orders_dir.exists():
        candidates.extend(orders_dir.glob("orders_*.csv"))
    summary_path = account_dir / "summary" / "summary.json"
    if summary_path.exists():
        candidates.append(summary_path)
    if not candidates:
        return None
    return datetime.fromtimestamp(max(p.stat().st_mtime for p in candidates))


def _days_since(date_str: str, today: datetime) -> int | None:
    """计算 ``YYYYMMDD`` 日期距今天数；解析失败返回 None。"""
    try:
        return (today.date() - datetime.strptime(date_str, "%Y%m%d").date()).days
    except (ValueError, TypeError):
        return None


def _classify(
    *,
    enabled: bool,
    rejected_today: int,
    orders_today: int,
    has_orders: bool,
    days_since_trade: int | None,
    active_days: int,
) -> str:
    """按优先级判定账户状态。"""
    if not enabled:
        return STATUS_DISABLED
    if rejected_today > 0:
        return STATUS_REJECTED_TODAY
    if orders_today > 0:
        return STATUS_TRADED_TODAY
    if not has_orders:
        return STATUS_NEVER_TRADED
    if days_since_trade is not None and days_since_trade <= active_days:
        return STATUS_ACTIVE
    return STATUS_STALE


def build_account_status_df(
    data_dir: Path,
    config: dict[str, Any] | None = None,
    *,
    today: datetime | None = None,
    active_days: int = 7,
) -> tuple[pd.DataFrame, pd.DataFrame]:
    """构建所有账户的状态表与今日废单明细。

    单账户异常（如委托缺少 ``account_cash`` 导致实时盈亏计算失败）不会
    中断整体构建，仅降级为 ``summary.json`` 数据并记录日志。

    Args:
        data_dir: 模拟交易数据根目录。
        config: 账户配置字典；为 ``None`` 时自动加载。
        today: 用于判定「今日」与无交易天数的时间基准，默认当前时间。
        active_days: 最近一次委托在多少天内视为「近期活跃」。

    Returns:
        ``(status_df, rejected_df)``。``status_df`` 列见 ``STATUS_COLUMNS``；
        ``rejected_df`` 为当日废单明细，列见 ``REJECTED_DETAIL_COLUMNS``。
    """
    if config is None:
        config = load_config(data_dir)
    if today is None:
        today = datetime.now()
    today_str = today.strftime("%Y%m%d")

    rows: list[dict[str, Any]] = []
    rejected_rows: list[dict[str, Any]] = []

    for account_id in list_account_ids(data_dir):
        account_config = config.get(account_id, {})
        enabled = bool(account_config.get("enabled", True))
        initial_cash = float(account_config.get("initial_cash", 100_000.0))
        account_dir = data_dir / account_id

        orders_df = load_all_orders(data_dir, account_id)
        has_orders = not orders_df.empty

        # ── 委托统计（不依赖实时盈亏计算，先行确定）──
        if has_orders and "trade_date" in orders_df.columns:
            today_orders = orders_df[orders_df["trade_date"] == today_str]
            rejected_mask_all = _rejected_mask(orders_df)
            last_trade_date = str(orders_df["trade_date"].dropna().max())
        else:
            today_orders = orders_df.iloc[0:0]
            rejected_mask_all = pd.Series(False, index=orders_df.index)
            last_trade_date = ""

        orders_today = len(today_orders)
        rejected_mask_today = _rejected_mask(today_orders)
        rejected_today = int(rejected_mask_today.sum())
        rejected_total = int(rejected_mask_all.sum())
        filled_today = int((~rejected_mask_today).sum())
        days_since_trade = (
            _days_since(last_trade_date, today) if last_trade_date else None
        )

        for _, order in today_orders[rejected_mask_today].iterrows():
            rejected_rows.append(
                {
                    "account_id": account_id,
                    "trade_date": order.get("trade_date", today_str),
                    "order_time": order.get("order_time", ""),
                    "stock_code": order.get("stock_code", ""),
                    "order_volume": order.get("order_volume", ""),
                    "status_msg": order.get("status_msg", ""),
                }
            )

        # ── 实时资产与持仓 ──
        live: dict[str, Any] = {}
        positions_df = pd.DataFrame()
        if has_orders and "stock_code" in orders_df.columns:
            try:
                stock_codes = orders_df["stock_code"].dropna().unique().tolist()
                prices = resolve_prices(data_dir, stock_codes, account_config)
                live = calculate_live_pnl(orders_df, prices, initial_cash)
                positions_df = live.get("positions", pd.DataFrame())
            except Exception:
                logger.exception(
                    "账户 %s 实时盈亏计算失败，降级为 summary 数据", account_id
                )

        if live:
            cash = float(live["cash"])
            market_value = float(live["market_value"])
            total_asset = float(live["total_asset"])
            total_pnl = float(live["total_pnl"])
            total_return_rate = float(live["total_return_rate"])
        else:
            summary = load_summary(data_dir, account_id)
            cash = float(summary.get("cash", initial_cash))
            market_value = float(summary.get("market_value", 0.0))
            total_asset = float(summary.get("total_asset", initial_cash))
            total_pnl = float(summary.get("total_pnl", 0.0))
            total_return_rate = float(summary.get("total_return_rate", 0.0))

        position_count = 0 if positions_df.empty else int(len(positions_df))

        rows.append(
            {
                "account_id": account_id,
                "status": _classify(
                    enabled=enabled,
                    rejected_today=rejected_today,
                    orders_today=orders_today,
                    has_orders=has_orders,
                    days_since_trade=days_since_trade,
                    active_days=active_days,
                ),
                "enabled": enabled,
                "orders_today": orders_today,
                "filled_today": filled_today,
                "rejected_today": rejected_today,
                "rejected_total": rejected_total,
                "position_count": position_count,
                "last_trade_date": last_trade_date,
                "days_since_trade": days_since_trade,
                "last_write": _last_write_time(account_dir),
                "cash": cash,
                "market_value": market_value,
                "total_asset": total_asset,
                "total_pnl": total_pnl,
                "total_return_rate": total_return_rate,
            }
        )

    status_df = pd.DataFrame(rows, columns=STATUS_COLUMNS)
    rejected_df = pd.DataFrame(rejected_rows, columns=REJECTED_DETAIL_COLUMNS)

    # 需要关注的排前面；同状态内按无交易天数降序
    status_df["_priority"] = status_df["status"].map(_STATUS_PRIORITY)
    status_df = (
        status_df.sort_values(
            ["_priority", "days_since_trade", "account_id"],
            ascending=[True, False, True],
            na_position="last",
        )
        .drop(columns="_priority")
        .reset_index(drop=True)
    )
    return status_df, rejected_df


# ── 策略进程日志监控 ──────────────────────────────────────────────────

# 匹配账户时忽略的通用词，使 ``factor_alpha191_live_1_paper_test.log`` 能
# 关联到账户 ``alpha191_factor_papertrading_1``
_GENERIC_NAME_TOKENS = {
    "factor",
    "live",
    "paper",
    "papertrading",
    "research",
    "strategy",
    "test",
    "trading",
}

# 日志行内的时间戳，如 ``2026-08-26 15:40:12,666 | INFO | ...``
_LOG_TIMESTAMP_RE = re.compile(r"^(\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2})")

# 判定错误行的标记：``... | ERROR | ...`` 或行首 ``ERROR:logger:``
_LOG_ERROR_MARKERS = (" | ERROR | ", "ERROR:")

# 进程状态
PROCESS_RUNNING = "🟢 运行中"
PROCESS_DEAD = "🔴 疑似停跑"

LOG_COLUMNS = [
    "log_file",
    "account_id",
    "process_status",
    "last_write",
    "minutes_idle",
    "last_log_time",
    "recent_errors",
    "last_error",
    "size_mb",
]


def _name_tokens(name: str) -> frozenset[str]:
    """把名称拆成小写 token 集合，忽略通用词。"""
    return frozenset(
        t
        for t in re.split(r"[_\-]+", name.lower())
        if t and t not in _GENERIC_NAME_TOKENS
    )


def match_log_to_account(log_stem: str, account_ids: list[str]) -> str | None:
    """把日志文件名（不含扩展名）关联到账户 ID。

    先尝试精确匹配 ``<account_id>_paper_test`` / ``<account_id>``；
    失败后按去掉通用词的 token 集合模糊匹配，仅接受唯一命中，
    避免把 ``factor_research_alpha191`` 错配到 ``alpha191_factor_papertrading_1``。
    """
    target = log_stem.lower().removesuffix("_paper_test").removesuffix("_test")
    lookup = {aid.lower(): aid for aid in account_ids}
    if target in lookup:
        return lookup[target]

    target_tokens = _name_tokens(target)
    if not target_tokens:
        return None
    matches = [aid for aid in account_ids if _name_tokens(aid) == target_tokens]
    return matches[0] if len(matches) == 1 else None


def is_error_line(line: str) -> bool:
    """判断一行日志是否为错误行（两种日志格式）。"""
    return any(marker in line for marker in _LOG_ERROR_MARKERS)


def read_all_log_lines(path: Path) -> list[str]:
    """读取整个日志文件的所有行（供日志浏览器分页浏览完整历史）。"""
    with path.open("rb") as f:
        text = f.read().decode("utf-8", errors="replace")
    return text.splitlines()


def tail_log_lines(
    path: Path, max_lines: int = 200, tail_bytes: int | None = None
) -> list[str]:
    """读取日志文件末尾的若干行。

    Args:
        path: 日志文件路径。
        max_lines: 返回末尾多少行。
        tail_bytes: 读取的尾部字节数；``None`` 时按 ``max_lines * 1KB``
            估算（日志单行约 500 字节，含余量），下限 256KB。
    """
    if tail_bytes is None:
        tail_bytes = max(256 * 1024, max_lines * 1024)
    lines = _read_log_tail(path, tail_bytes)
    return lines[-max_lines:]


def _read_log_tail(path: Path, tail_bytes: int) -> list[str]:
    """读取日志文件末尾的完整行（跳过被截断的首行）。"""
    size = path.stat().st_size
    with path.open("rb") as f:
        if size > tail_bytes:
            f.seek(size - tail_bytes)
            f.readline()  # 丢弃被截断的首行
        text = f.read().decode("utf-8", errors="replace")
    return text.splitlines()


def inspect_log_file(
    path: Path, *, now: datetime, tail_bytes: int = 256 * 1024
) -> dict[str, Any]:
    """提取单个策略日志的监控信息。

    只读文件末尾 ``tail_bytes`` 字节（日志单文件可达 20MB+），
    统计其中的错误行数与最后一条错误。
    """
    stat = path.stat()
    last_write = datetime.fromtimestamp(stat.st_mtime)
    minutes_idle = (now - last_write).total_seconds() / 60

    lines = _read_log_tail(path, tail_bytes)

    last_log_time: datetime | None = None
    error_lines: list[str] = []
    for line in reversed(lines):
        m = _LOG_TIMESTAMP_RE.match(line)
        if m and last_log_time is None:
            try:
                last_log_time = datetime.strptime(m.group(1), "%Y-%m-%d %H:%M:%S")
            except ValueError:
                pass
        if is_error_line(line):
            error_lines.append(line)

    return {
        "last_write": last_write,
        "minutes_idle": minutes_idle,
        "last_log_time": last_log_time,
        "recent_errors": len(error_lines),
        "last_error": error_lines[0][:300] if error_lines else "",
        "size_mb": stat.st_size / 1024 / 1024,
    }


def build_log_status_df(
    log_dir: Path,
    account_ids: list[str],
    *,
    now: datetime | None = None,
    alive_minutes: float = 10.0,
    tail_bytes: int = 256 * 1024,
) -> pd.DataFrame:
    """扫描策略日志目录，构建进程状态表。

    日志持续输出心跳（telemetry），因此最后写入时间即进程活跃信号：
    超过 ``alive_minutes`` 分钟未写入视为「疑似停跑」。

    Returns:
        列见 ``LOG_COLUMNS``；无法关联账户的日志 ``account_id`` 为空字符串。
        按疑似停跑优先、错误数降序排列。
    """
    if now is None:
        now = datetime.now()

    rows: list[dict[str, Any]] = []
    log_paths = sorted(p for p in log_dir.glob("*.log") if p.is_file())
    for path in log_paths:
        try:
            info = inspect_log_file(path, now=now, tail_bytes=tail_bytes)
        except Exception:
            logger.exception("读取日志失败: %s", path)
            continue

        process_status = (
            PROCESS_RUNNING if info["minutes_idle"] <= alive_minutes else PROCESS_DEAD
        )
        rows.append(
            {
                "log_file": path.name,
                "account_id": match_log_to_account(path.stem, account_ids),
                "process_status": process_status,
                **info,
            }
        )

    log_df = pd.DataFrame(rows, columns=LOG_COLUMNS)
    if log_df.empty:
        return log_df

    log_df["_dead"] = (log_df["process_status"] == PROCESS_DEAD).astype(int)
    log_df = (
        log_df.sort_values(
            ["_dead", "recent_errors", "log_file"],
            ascending=[False, False, True],
        )
        .drop(columns="_dead")
        .reset_index(drop=True)
    )
    return log_df
