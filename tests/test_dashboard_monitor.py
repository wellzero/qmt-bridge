"""账户状态监控单元测试。

目录名 ``paper-trading`` 含连字符无法作为包导入，
将目录加入 ``sys.path`` 后按模块名加载 ``monitor``。
"""

from __future__ import annotations

import json
import os
import sys
from datetime import datetime
from pathlib import Path

import pandas as pd

_DASHBOARD_DIR = Path(__file__).resolve().parents[1] / "dashboard" / "paper-trading"
if str(_DASHBOARD_DIR) not in sys.path:
    sys.path.insert(0, str(_DASHBOARD_DIR))

from monitor import (  # noqa: E402
    PROCESS_DEAD,
    PROCESS_RUNNING,
    STATUS_ACTIVE,
    STATUS_DISABLED,
    STATUS_NEVER_TRADED,
    STATUS_REJECTED_TODAY,
    STATUS_STALE,
    STATUS_TRADED_TODAY,
    build_account_status_df,
    build_log_status_df,
    is_error_line,
    match_log_to_account,
    read_all_log_lines,
    tail_log_lines,
)

_NOW = datetime(2026, 8, 26, 15, 0, 0)

# 与真实委托 CSV 一致的表头
_ORDER_HEADER = (
    "order_time,order_id,stock_code,order_type,order_volume,price_type,price,"
    "traded_volume,traded_price,commission,stamp_tax,account_cash,"
    "account_market_value,order_status,status_msg,strategy_name,order_remark"
)


def _write_config(data_dir: Path, accounts: dict) -> None:
    """写入 ``config.json``。"""
    (data_dir / "config.json").write_text(
        json.dumps(accounts, ensure_ascii=False), encoding="utf-8"
    )


def _order_row(
    time_str: str,
    order_id: int,
    code: str,
    traded_volume: int,
    price: float,
    status_msg: str,
    cash: float = 94_365,
    market_value: float = 5_630,
) -> str:
    """构造一行 17 字段的委托 CSV 记录（买入 200 股）。"""
    fields = [
        time_str,
        order_id,
        code,
        23,  # order_type: 买入
        200,  # order_volume
        5,  # price_type
        0.0,  # price
        traded_volume,
        price,
        5.0 if traded_volume else 0,  # commission
        0,  # stamp_tax
        cash,
        market_value,
        56,  # order_status
        status_msg,
        "TestStrategy",
        "",
    ]
    return ",".join(str(x) for x in fields)


def _write_orders(
    data_dir: Path, account_id: str, date_str: str, rows: list[str]
) -> None:
    """写入单日委托 CSV。"""
    order_dir = data_dir / account_id / "order"
    order_dir.mkdir(parents=True, exist_ok=True)
    content = "\n".join([_ORDER_HEADER, *rows]) + "\n"
    (order_dir / f"orders_{date_str}.csv").write_text(content, encoding="utf-8")


def _make_config(enabled: bool = True) -> dict:
    """构造最小账户配置。"""
    return {"initial_cash": 100_000.0, "enabled": enabled}


def test_account_status_classification(tmp_path):
    """各典型账户应被划分到正确状态，废单计入当日明细。"""
    data_dir = tmp_path / "paper_trading"
    data_dir.mkdir()

    filled = _order_row("10:00:00", 1, "600900.SH", 200, 28.15, "已成")
    rejected = _order_row("10:30:00", 2, "600887.SH", 0, 0.0, "可用资金不足")

    # 今日全部成交
    _write_orders(data_dir, "traded_today", "20260826", [filled])
    # 今日有一笔废单（可用资金不足）
    _write_orders(data_dir, "rejected_today", "20260826", [filled, rejected])
    # 30 天前交易过，仍有持仓
    _write_orders(data_dir, "stale_account", "20260727", [filled])
    # 3 天前交易过
    _write_orders(data_dir, "recent_account", "20260823", [filled])
    # 已停用但有历史数据
    _write_orders(data_dir, "disabled_account", "20260825", [filled])
    # 仅在 config 注册、从未交易
    _write_config(
        data_dir,
        {
            "traded_today": _make_config(),
            "rejected_today": _make_config(),
            "stale_account": _make_config(),
            "recent_account": _make_config(),
            "disabled_account": _make_config(enabled=False),
            "never_traded": _make_config(),
        },
    )

    status_df, rejected_df = build_account_status_df(
        data_dir, active_days=7, today=_NOW
    )
    status = dict(zip(status_df["account_id"], status_df["status"]))

    assert status["traded_today"] == STATUS_TRADED_TODAY
    assert status["rejected_today"] == STATUS_REJECTED_TODAY
    assert status["stale_account"] == STATUS_STALE
    assert status["recent_account"] == STATUS_ACTIVE
    assert status["never_traded"] == STATUS_NEVER_TRADED
    assert status["disabled_account"] == STATUS_DISABLED

    # 废单统计与明细
    row = status_df[status_df["account_id"] == "rejected_today"].iloc[0]
    assert row["rejected_today"] == 1
    assert row["rejected_total"] == 1
    assert row["filled_today"] == 1
    assert len(rejected_df) == 1
    assert rejected_df.iloc[0]["status_msg"] == "可用资金不足"

    # 无交易天数与最后交易日期
    stale_row = status_df[status_df["account_id"] == "stale_account"].iloc[0]
    assert stale_row["last_trade_date"] == "20260727"
    assert stale_row["days_since_trade"] == 30
    assert stale_row["position_count"] == 1

    # 排序：需要关注的（废单/无近期交易）排在运行正常的前面
    assert list(status_df["account_id"]).index("rejected_today") < list(
        status_df["account_id"]
    ).index("traded_today")


def test_match_log_to_account():
    """日志名应先精确、后按 token 唯一匹配关联账户。"""
    accounts = [
        "bluechip_quality_quarterly_paper",
        "alpha191_factor_papertrading_1",
        "alpha191_factor_papertrading_3",
    ]
    # 精确匹配
    assert (
        match_log_to_account("bluechip_quality_quarterly_paper_test", accounts)
        == "bluechip_quality_quarterly_paper"
    )
    # 模糊匹配：忽略 factor/live/papertrading 等通用词后 token 一致
    assert (
        match_log_to_account("factor_alpha191_live_1_paper_test", accounts)
        == "alpha191_factor_papertrading_1"
    )
    # token 无法唯一命中时返回 None
    assert match_log_to_account("factor_research_alpha191_paper_test", accounts) is None
    assert match_log_to_account("totally_unknown_paper_test", accounts) is None


def test_build_log_status_df(tmp_path):
    """日志扫描应判定进程存活、统计近期错误并解析最后日志时间。"""
    log_dir = tmp_path / "logs"
    log_dir.mkdir()
    accounts = ["alpha_strategy_paper"]

    # 运行中：1 分钟前有写入，日志含 1 条错误
    running_log = log_dir / "alpha_strategy_paper_test.log"
    running_log.write_text(
        "INFO:lumibot:heartbeat\n"
        "2026-08-26 15:39:10,000 | INFO | mod | ok\n"
        "2026-08-26 15:39:11,000 | ERROR | mod | boom\n"
        "INFO:lumibot:heartbeat\n",
        encoding="utf-8",
    )
    running_ts = _NOW.timestamp() - 60
    os.utime(running_log, (running_ts, running_ts))

    # 疑似停跑：60 分钟前写入
    dead_log = log_dir / "beta_strategy_paper_test.log"
    dead_log.write_text("2026-08-26 14:00:00,000 | INFO | mod | ok\n", encoding="utf-8")
    dead_ts = _NOW.timestamp() - 3600
    os.utime(dead_log, (dead_ts, dead_ts))

    log_df = build_log_status_df(log_dir, accounts, now=_NOW, alive_minutes=10)

    assert len(log_df) == 2
    # 疑似停跑排最前，且未关联账户
    first = log_df.iloc[0]
    assert first["log_file"] == "beta_strategy_paper_test.log"
    assert first["process_status"] == PROCESS_DEAD
    assert pd.isna(first["account_id"])

    running = log_df[log_df["log_file"] == "alpha_strategy_paper_test.log"].iloc[0]
    assert running["process_status"] == PROCESS_RUNNING
    assert running["account_id"] == "alpha_strategy_paper"
    assert running["recent_errors"] == 1
    assert "boom" in running["last_error"]
    assert running["last_log_time"] == datetime(2026, 8, 26, 15, 39, 11)
    assert abs(running["minutes_idle"] - 1.0) < 0.01


def test_tail_log_lines_and_error_filter(tmp_path):
    """日志详情应返回末尾行，错误行过滤可识别两种日志格式。"""
    log_path = tmp_path / "gamma_strategy_paper_test.log"
    lines = [f"INFO:lumibot:heartbeat {i}" for i in range(1, 11)]
    lines += [
        "2026-08-26 15:39:10,000 | INFO | mod | ok",
        "2026-08-26 15:39:11,000 | ERROR | mod | boom",
        "ERROR:lumibot.brokers.broker:[Gamma] failed",
        "INFO:lumibot:heartbeat 11",
    ]
    log_path.write_text("\n".join(lines) + "\n", encoding="utf-8")

    tail = tail_log_lines(log_path, max_lines=3)
    assert tail == [
        "2026-08-26 15:39:11,000 | ERROR | mod | boom",
        "ERROR:lumibot.brokers.broker:[Gamma] failed",
        "INFO:lumibot:heartbeat 11",
    ]

    # 请求行数超过文件行数时返回全部
    assert tail_log_lines(log_path, max_lines=100) == lines

    assert is_error_line("2026-08-26 15:39:11,000 | ERROR | mod | boom")
    assert is_error_line("ERROR:lumibot.brokers.broker:[Gamma] failed")
    assert not is_error_line("2026-08-26 15:39:10,000 | INFO | mod | ok")
    assert not is_error_line("INFO:lumibot:heartbeat")


def test_read_all_log_lines(tmp_path):
    """完整读取应返回文件全部行（供日志浏览器分页）。"""
    log_path = tmp_path / "delta_strategy_paper_test.log"
    lines = [f"line {i}" for i in range(1, 51)]
    log_path.write_text("\n".join(lines) + "\n", encoding="utf-8")

    assert read_all_log_lines(log_path) == lines
