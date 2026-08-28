"""账户状态监控页面。

监控所有模拟交易账户与策略进程的运行状态：

- 账户：今日委托/废单、最后交易时间、持仓与实时盈亏
- 进程：策略日志（默认 ``PAPER_TEST_LOG_DIR``，可用侧边栏修改）的最后
  写入时间与近期错误，用于判断策略是否仍在运行
- 控制：每个策略可单独 启动 / 停止 / 重启（复用
  ``run_all_paper_tests.py`` 的 PID 文件管理，与命令行行为一致）

页面每分钟自动刷新（可在侧边栏关闭）。告警区固定高度、可滚动浏览全部条目；
点击策略进程行可翻页浏览该日志的完整历史（第 1 页为最新）。
"""

from __future__ import annotations

import logging
import os
from datetime import datetime
from pathlib import Path

import pandas as pd
import streamlit as st

from auth import logout_button, require_auth
from components import render_account_detail, render_big_title
from data_loader import load_config, resolve_data_dir
from monitor import (
    PROCESS_DEAD,
    STATUS_DISABLED,
    STATUS_NEVER_TRADED,
    build_account_status_df,
    build_log_status_df,
    is_error_line,
    read_all_log_lines,
    tail_log_lines,
)
from pricing import is_trading_hours, load_price_cache_raw
from process_control import (
    CTRL_RUNNING,
    CTRL_STOPPED,
    ORCHESTRATOR_FILE,
    list_strategy_controls,
    perform_action,
)

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

st.set_page_config(
    page_title="Account Status - Trading Summary",
    page_icon="🩺",
    layout="wide",
)

require_auth()

# 自动刷新间隔选项（秒）
_REFRESH_OPTIONS = {"30 秒": 30, "1 分钟": 60, "5 分钟": 300}

# 默认日志目录：环境变量优先
_DEFAULT_LOG_DIR = "/home/claude/quant_free_strategies/paper_test_logs"


def _default_log_dir() -> str:
    """返回策略日志目录默认值。"""
    return os.getenv("PAPER_TEST_LOG_DIR", _DEFAULT_LOG_DIR)


render_big_title("🩺 Account Status")
st.caption(
    "监控模拟交易账户与策略进程的运行状态，自动刷新；点击告警条目或表格行可展开详情"
)

# ── 侧边栏 ──────────────────────────────────────────────────────────

with st.sidebar:
    st.header("数据目录")
    data_dir_input = st.text_input(
        "模拟交易数据目录",
        value=str(resolve_data_dir()),
        key="monitor_data_dir_input",
        help="默认指向项目 ``data/paper_trading`` 目录",
    )

    st.markdown("---")
    st.header("策略日志目录")
    log_dir_input = st.text_input(
        "策略日志目录",
        value=_default_log_dir(),
        key="monitor_log_dir_input",
        help="策略 ``*_paper_test.log`` 所在目录，可用环境变量 ``PAPER_TEST_LOG_DIR`` 覆盖",
    )
    log_alive_minutes = st.number_input(
        "进程存活阈值（分钟）",
        min_value=1,
        max_value=1440,
        value=10,
        step=1,
        key="monitor_alive_minutes",
        help="日志最后写入距今超过该分钟数即视为「疑似停跑」",
    )

    st.markdown("---")
    st.header("判定阈值")
    active_days = st.number_input(
        "近期活跃天数",
        min_value=1,
        max_value=365,
        value=7,
        step=1,
        key="monitor_active_days",
        help="最后一次委托在多少天内视为「近期活跃」",
    )
    stale_days = st.number_input(
        "无交易提醒天数",
        min_value=1,
        max_value=365,
        value=14,
        step=1,
        key="monitor_stale_days",
        help="无交易超过该天数且有持仓时，在告警区提示",
    )

    st.markdown("---")
    st.header("自动刷新")
    auto_refresh = st.checkbox(
        "启用自动刷新",
        value=True,
        key="monitor_auto_refresh",
    )
    refresh_label = st.selectbox(
        "刷新间隔",
        list(_REFRESH_OPTIONS),
        index=1,
        key="monitor_refresh_interval",
        disabled=not auto_refresh,
    )

    st.markdown("---")
    if st.button("刷新数据", use_container_width=True, key="monitor_refresh"):
        st.cache_data.clear()

    logout_button()

data_dir = resolve_data_dir(data_dir_input)

if not data_dir.exists():
    st.error(f"数据目录不存在：``{data_dir}``")
    st.stop()

log_dir = Path(log_dir_input.strip()).expanduser()


# ── 数据加载（缓存）──────────────────────────────────────────────────


@st.cache_data(ttl=30)
def _load_status(
    data_dir_str: str, active_days: int
) -> tuple[dict, pd.DataFrame, pd.DataFrame]:
    """缓存加载账户状态与今日废单明细。"""
    d = resolve_data_dir(data_dir_str)
    config = load_config(d)
    status_df, rejected_df = build_account_status_df(
        d, config, active_days=int(active_days)
    )
    return config, status_df, rejected_df


@st.cache_data(ttl=30)
def _load_log_status(
    log_dir_str: str, account_ids: tuple[str, ...], alive_minutes: int
) -> pd.DataFrame:
    """缓存加载策略进程状态。"""
    return build_log_status_df(
        Path(log_dir_str),
        list(account_ids),
        alive_minutes=float(alive_minutes),
    )


@st.cache_data(ttl=30)
def _load_log_tail(log_path_str: str, mtime: float, max_lines: int) -> list[str]:
    """缓存读取日志尾部行；``mtime`` 变化时自动失效。"""
    return tail_log_lines(Path(log_path_str), max_lines=max_lines)


@st.cache_data(ttl=30, max_entries=3)
def _load_log_lines(log_path_str: str, mtime: float) -> list[str]:
    """缓存读取完整日志行；``mtime`` 变化时自动失效。

    单文件可达 20MB+，解码后内存约 2 倍体积，故限制缓存条数。
    """
    return read_all_log_lines(Path(log_path_str))


def _price_cache_alert() -> tuple[str, str] | None:
    """盘中检查最新价缓存是否过期，返回 (级别, 文案) 或 None。"""
    if not is_trading_hours():
        return None
    raw = load_price_cache_raw(data_dir)
    if not raw:
        return (
            "warning",
            "盘中但无最新价缓存，盈亏按成交价估算；可在主页点击「获取最新价」",
        )
    timestamp = raw.get("timestamp", "")
    try:
        cache_date = datetime.fromisoformat(str(timestamp)).date()
    except ValueError:
        return None
    if cache_date != datetime.now().date():
        return (
            "warning",
            f"最新价缓存为 {timestamp}（非当日），盘中盈亏可能失真；可在主页点击「获取最新价」",
        )
    return None


# 今日废单明细的列名映射（告警展开与汇总表共用）
_REJECTED_RENAME = {
    "account_id": "账户 ID",
    "trade_date": "日期",
    "order_time": "时间",
    "stock_code": "股票代码",
    "order_volume": "委托量",
    "status_msg": "原因",
}


def _render_log_tail(log_file: str, max_lines: int, errors_only: bool = False) -> None:
    """在当前位置渲染某个日志的尾部内容。"""
    log_path = log_dir / log_file
    if not log_path.exists():
        st.warning(f"日志文件不存在：``{log_path}``")
        return
    lines = _load_log_tail(str(log_path), log_path.stat().st_mtime, max(500, max_lines))
    if errors_only:
        lines = [line for line in lines if is_error_line(line)]
    lines = lines[-max_lines:]
    st.caption(f"``{log_file}`` 末尾 {len(lines)} 行")
    if lines:
        st.code("\n".join(lines), language="text", wrap_lines=True)
        st.caption("翻页浏览完整历史：在下方「策略进程」表点击对应日志行")
    else:
        st.info("没有匹配的日志行")


def _render_alert_detail(
    alert: dict,
    status_df: pd.DataFrame,
    rejected_df: pd.DataFrame,
    account_to_log: dict[str, pd.Series],
) -> None:
    """渲染单条告警展开后的详情。"""
    kind = alert["kind"]

    if kind == "rejected":
        sub = rejected_df[rejected_df["account_id"] == alert["account_id"]]
        st.dataframe(
            sub.rename(columns=_REJECTED_RENAME),
            use_container_width=True,
            hide_index=True,
        )
    elif kind == "stale":
        account_id = alert["account_id"]
        row = status_df[status_df["account_id"] == account_id].iloc[0]
        st.markdown(
            f"- 最后交易：**{row['last_trade_date']}**"
            f"（{int(row['days_since_trade'])} 天前）\n"
            f"- 持仓 **{int(row['position_count'])} 只**，"
            f"市值 {row['market_value']:,.0f}\n"
            f"- 总资产 {row['total_asset']:,.0f}，总盈亏 {row['total_pnl']:,.0f}"
        )
        log_row = account_to_log.get(account_id)
        if log_row is None:
            st.info("未找到该账户对应的策略日志，无法判定进程是否存活")
        else:
            st.markdown(
                f"策略进程：**{log_row['process_status']}**（日志 "
                f"``{log_row['log_file']}`` 最后写入 "
                f"{int(log_row['minutes_idle'])} 分钟前）"
            )
            _render_log_tail(log_row["log_file"], max_lines=30)
        st.caption("完整持仓与委托明细：在下方「账户状态」表点击该账户行查看")
    elif kind == "log_tail":
        _render_log_tail(alert["log_file"], max_lines=30)
    elif kind == "log_errors":
        _render_log_tail(alert["log_file"], max_lines=10, errors_only=True)
    # plain：告警标题即全部内容，无需展开详情


# ── 进程控制（启动 / 停止 / 重启）─────────────────────────────────────

# 控制面板排序：运行中优先，其次 PID 异常，最后已停止
_CTRL_ORDER = {CTRL_RUNNING: 0, "DEAD_PID_FILE": 1, "BAD_PID_FILE": 2, CTRL_STOPPED: 3}


def _control_status_label(control: dict) -> str:
    """把 PID 状态渲染为带图标的文案。"""
    status = control["status"]
    if status == CTRL_RUNNING:
        return f"🟢 运行中 (pid {control['pid']})"
    if status == "DEAD_PID_FILE":
        return "🟠 进程已退出（PID 残留）"
    if status == "BAD_PID_FILE":
        return "🟠 PID 文件损坏"
    if status == CTRL_STOPPED:
        return "⚪ 已停止"
    return status


def _render_action_buttons(
    cols: tuple,
    stem: str,
    control: dict | None,
    key_prefix: str,
) -> None:
    """在给定三列中渲染单个策略的 启动 / 停止 / 重启 按钮并处理点击。

    Args:
        cols: ``(start_col, stop_col, restart_col)`` 三个 st.columns 元素。
        stem: 策略基名（日志/PID 文件名去扩展名）。
        control: ``list_strategy_controls`` 中该策略的状态项；``None`` 表示
            不在编排脚本管理范围内（按钮全部禁用）。
        key_prefix: 按钮-key 前缀，同一 stem 在不同区块需不同前缀。
    """
    status = control["status"] if control else "NOT_MANAGED"
    is_running = status == CTRL_RUNNING
    # 无 PID 文件时 stop/restart 无意义（restart 本质是 stop+start）
    has_pid_file = status in (CTRL_RUNNING, "DEAD_PID_FILE", "BAD_PID_FILE")
    start_col, stop_col, restart_col = cols

    action: str | None = None
    if start_col.button(
        "▶ 启动",
        key=f"{key_prefix}start::{stem}",
        use_container_width=True,
        disabled=is_running or control is None,
    ):
        action = "start"
    if stop_col.button(
        "⏹ 停止",
        key=f"{key_prefix}stop::{stem}",
        use_container_width=True,
        disabled=not has_pid_file or control is None,
    ):
        action = "stop"
    if restart_col.button(
        "🔄 重启",
        key=f"{key_prefix}restart::{stem}",
        use_container_width=True,
        disabled=not has_pid_file or control is None,
    ):
        action = "restart"

    if action:
        result = perform_action(stem, action)
        ok = "ERROR" not in result["status"] and "ORCHESTRATOR" not in result["status"]
        st.toast(f"{stem}：{result['status']}", icon="✅" if ok else "❌")
        logger.info("进程控制 %s %s -> %s", action, stem, result["status"])
        # 立即失效状态缓存，让表格与本面板反映最新 PID 状态
        st.cache_data.clear()
        st.rerun(scope="fragment")


# ── 监控主体（自动刷新的 fragment）───────────────────────────────────

_refresh_secs = (
    _REFRESH_OPTIONS[st.session_state.monitor_refresh_interval]
    if st.session_state.get("monitor_auto_refresh", True)
    else None
)


@st.fragment(run_every=_refresh_secs)
def _render_monitor() -> None:
    """渲染监控主体：总览卡片、告警、账户状态表、策略进程表。"""
    config, status_df, rejected_df = _load_status(
        str(data_dir), int(st.session_state.monitor_active_days)
    )
    alive_minutes = int(st.session_state.monitor_alive_minutes)
    log_df = (
        _load_log_status(
            str(log_dir), tuple(status_df["account_id"].tolist()), alive_minutes
        )
        if log_dir.exists()
        else pd.DataFrame()
    )

    # 策略基名 ↔ 关联账户 双向映射 + 受管策略的 PID 状态（进程控制用）
    stem_to_account: dict[str, str] = {}
    if not log_df.empty:
        for _, log_row in log_df.iterrows():
            if log_row["account_id"]:
                stem = str(log_row["log_file"]).removesuffix(".log")
                stem_to_account.setdefault(stem, str(log_row["account_id"]))
    account_to_stem = {aid: stem for stem, aid in stem_to_account.items()}
    controls = list_strategy_controls()
    controls_by_stem = {c["stem"]: c for c in controls} if controls else {}

    # ── 总览卡片 ──
    today_traded = int((status_df["orders_today"] > 0).sum())
    today_rejected = int((status_df["rejected_today"] > 0).sum())
    never_traded = int((status_df["status"] == STATUS_NEVER_TRADED).sum())
    disabled = int((status_df["status"] == STATUS_DISABLED).sum())

    st.markdown("### 总览")
    col1, col2, col3, col4, col5, col6 = st.columns(6)
    with col1:
        st.metric("账户总数", len(status_df))
    with col2:
        st.metric("今日交易", today_traded)
    with col3:
        st.metric("今日废单", today_rejected)
    with col4:
        st.metric("未交易", never_traded)
    with col5:
        st.metric("已停用", disabled)
    with col6:
        if log_df.empty:
            st.metric("运行中进程", "—")
        else:
            running = int((log_df["process_status"] != PROCESS_DEAD).sum())
            st.metric("运行中进程", f"{running}/{len(log_df)}")

    # ── 告警（点击条目展开详情）──
    alerts: list[dict] = []
    for _, row in status_df[status_df["rejected_today"] > 0].iterrows():
        alerts.append(
            {
                "level": "error",
                "title": (
                    f"{row['account_id']} 今日 {int(row['rejected_today'])} 笔废单"
                    f"（累计 {int(row['rejected_total'])} 笔），策略可能想买但资金不足"
                ),
                "kind": "rejected",
                "account_id": row["account_id"],
            }
        )
    stale_mask = (
        status_df["days_since_trade"].notna()
        & (status_df["days_since_trade"] > int(st.session_state.monitor_stale_days))
        & (status_df["position_count"] > 0)
    )
    for _, row in status_df[stale_mask].iterrows():
        alerts.append(
            {
                "level": "warning",
                "title": (
                    f"{row['account_id']} 已 {int(row['days_since_trade'])} 天无交易，"
                    f"仍有 {int(row['position_count'])} 只持仓（低频策略属正常，也可能是引擎停跑）"
                ),
                "kind": "stale",
                "account_id": row["account_id"],
            }
        )
    if not log_df.empty:
        for _, row in log_df[log_df["process_status"] == PROCESS_DEAD].iterrows():
            alerts.append(
                {
                    "level": "error",
                    "title": (
                        f"策略进程疑似停跑：{row['log_file']}（最后写入 "
                        f"{int(row['minutes_idle'])} 分钟前）"
                    ),
                    "kind": "log_tail",
                    "log_file": row["log_file"],
                }
            )
        for _, row in log_df[log_df["recent_errors"] > 0].iterrows():
            account_label = f"（{row['account_id']}）" if row["account_id"] else ""
            alerts.append(
                {
                    "level": "warning",
                    "title": (
                        f"{row['log_file']}{account_label} 日志近期有 "
                        f"{int(row['recent_errors'])} 条错误"
                    ),
                    "kind": "log_errors",
                    "log_file": row["log_file"],
                }
            )
        orphan_mask = (log_df["process_status"] != PROCESS_DEAD) & (
            log_df["account_id"].fillna("") == ""
        )
        for _, row in log_df[orphan_mask].iterrows():
            alerts.append(
                {
                    "level": "info",
                    "title": (
                        f"{row['log_file']} 在运行但未关联任何模拟账户（尚未注册或未交易）"
                    ),
                    "kind": "plain",
                }
            )
    price_alert = _price_cache_alert()
    if price_alert:
        price_level, price_message = price_alert
        alerts.append({"level": price_level, "title": price_message, "kind": "plain"})

    st.markdown("### 告警（点击条目展开详情，区域可滚动浏览全部）")
    if not alerts:
        st.success("全部账户与策略进程运行正常 ✅")
    else:
        # 账户 → 关联日志反查表，供告警展开时展示进程状态
        account_to_log: dict[str, pd.Series] = {}
        if not log_df.empty:
            for _, log_row in log_df.iterrows():
                if log_row["account_id"]:
                    account_to_log[log_row["account_id"]] = log_row

        level_icons = {"error": "🔴", "warning": "🟡", "info": "🔵"}
        # 固定高度的可滚动容器：告警再多也不撑长页面
        with st.container(height=420):
            for alert in alerts:
                with st.expander(f"{level_icons[alert['level']]} {alert['title']}"):
                    _render_alert_detail(alert, status_df, rejected_df, account_to_log)

    # ── 今日废单明细 ──
    if not rejected_df.empty:
        with st.expander(f"🔴 今日废单明细（{len(rejected_df)} 笔）"):
            st.dataframe(
                rejected_df.rename(columns=_REJECTED_RENAME),
                use_container_width=True,
                hide_index=True,
            )

    # ── 账户状态表 ──
    all_statuses = status_df["status"].unique().tolist()
    # 会话内旧的选择可能不含新出现的状态，先消费「重置」标记再创建筛选器
    if st.session_state.pop("monitor_status_reset_pending", False):
        st.session_state.monitor_status_filter = all_statuses

    filter_col, reset_col = st.columns([5, 1])
    with filter_col:
        status_filter = st.multiselect(
            "状态筛选（表格内可滚动查看全部账户）",
            options=all_statuses,
            default=all_statuses,
            key="monitor_status_filter",
        )
    with reset_col:
        st.write("")
        if st.button("重置筛选", key="monitor_status_reset", use_container_width=True):
            st.session_state.monitor_status_reset_pending = True
            st.rerun(scope="fragment")

    display_df = status_df[status_df["status"].isin(status_filter)].copy()
    st.markdown(
        f"### 账户状态（显示 {len(display_df)}/{len(status_df)} 个账户，"
        f"点击行查看账户详情）"
    )

    # 关联策略进程状态
    if not log_df.empty:
        process_map = {
            row["account_id"]: row["process_status"]
            for _, row in log_df.iterrows()
            if row["account_id"]
        }
        display_df["process_status"] = (
            display_df["account_id"].map(process_map).fillna("— 无日志")
        )

    display_df["days_since_trade"] = display_df["days_since_trade"].astype("Int64")
    display_df["last_trade_date"] = display_df["last_trade_date"].replace("", pd.NA)
    display_df = display_df.rename(
        columns={
            "status": "状态",
            "account_id": "账户 ID",
            "process_status": "进程",
            "orders_today": "今日委托",
            "filled_today": "今日成交",
            "rejected_today": "今日废单",
            "rejected_total": "累计废单",
            "position_count": "持仓数",
            "last_trade_date": "最后交易",
            "days_since_trade": "无交易天数",
            "last_write": "数据更新",
            "cash": "可用资金",
            "market_value": "持仓市值",
            "total_asset": "总资产",
            "total_pnl": "总盈亏",
            "total_return_rate": "收益率",
        }
    )
    display_df["最后交易"] = display_df["最后交易"].map(
        lambda s: f"{s[:4]}-{s[4:6]}-{s[6:]}"
        if isinstance(s, str) and len(s) == 8
        else s
    )
    display_df["数据更新"] = display_df["数据更新"].map(
        lambda t: t.strftime("%m-%d %H:%M") if pd.notna(t) else "—"
    )
    display_df["收益率"] = display_df["收益率"] * 100

    table_columns = [
        "状态",
        "账户 ID",
        "进程",
        "今日委托",
        "今日成交",
        "今日废单",
        "累计废单",
        "持仓数",
        "最后交易",
        "无交易天数",
        "数据更新",
        "可用资金",
        "持仓市值",
        "总资产",
        "总盈亏",
        "收益率",
    ]
    event = st.dataframe(
        display_df[table_columns],
        use_container_width=True,
        hide_index=True,
        on_select="rerun",
        selection_mode="single-row",
        key="monitor_accounts_table",
        # 加高表格让 35 个账户尽量直接可见，超出的在表内滚动
        height=min(60 + 35 * (len(display_df) + 1), 900),
        column_config={
            "可用资金": st.column_config.NumberColumn(format="%,.2f"),
            "持仓市值": st.column_config.NumberColumn(format="%,.2f"),
            "总资产": st.column_config.NumberColumn(format="%,.2f"),
            "总盈亏": st.column_config.NumberColumn(format="%,.2f"),
            "收益率": st.column_config.NumberColumn(format="%.2f%%"),
        },
    )

    selected_account = None
    selected = event.selection
    if selected and selected.get("rows"):
        row_idx = selected["rows"][0]
        selected_account = str(display_df.iloc[row_idx]["账户 ID"])

    # ── 选中账户详情 ──
    if selected_account:
        st.markdown("---")
        st.markdown("### 账户详情")
        render_account_detail(
            data_dir, selected_account, config.get(selected_account, {})
        )
        # 该账户对应策略的 启动 / 停止 / 重启 按钮
        stem = account_to_stem.get(selected_account)
        if stem:
            st.markdown(f"#### ⚙️ 策略进程控制：`{stem}`")
            _render_action_buttons(
                st.columns(3), stem, controls_by_stem.get(stem), "acct_"
            )
            if stem not in controls_by_stem:
                st.caption("该策略不在编排脚本管理列表内，无法通过按钮控制")
        elif controls is not None:
            st.caption("该账户未关联策略日志，无法定位对应策略进程")

    # ── 进程控制 ──
    st.markdown("---")
    st.markdown("### ⚙️ 进程控制（逐个策略 启动 / 停止 / 重启）")
    if controls is None:
        st.warning(f"未找到编排脚本 ``{ORCHESTRATOR_FILE}``，进程控制不可用")
    else:
        running = sum(1 for c in controls if c["status"] == CTRL_RUNNING)
        st.caption(
            f"共 {len(controls)} 个策略，运行中 {running} 个；"
            "按 PID 文件操作，与 ``run_all_paper_tests.py`` 命令行行为一致"
        )
        ordered = sorted(
            controls, key=lambda c: (_CTRL_ORDER.get(c["status"], 4), c["stem"])
        )
        # 固定高度可滚动：策略再多也不撑长页面
        with st.container(height=430):
            header_cols = st.columns((4.6, 2.2, 1, 1, 1))
            header_cols[0].caption("策略（关联账户）")
            header_cols[1].caption("进程状态")
            for control in ordered:
                row = st.columns((4.6, 2.2, 1, 1, 1))
                account = stem_to_account.get(control["stem"])
                row[0].write(
                    f"``{control['stem']}``"
                    + (f" ｜ {account}" if account else "")
                )
                row[1].write(_control_status_label(control))
                _render_action_buttons(
                    (row[2], row[3], row[4]), control["stem"], control, "ctrl_"
                )

    # ── 策略进程表 ──
    st.markdown("---")
    st.markdown("### 策略进程（按日志判定）")
    selected_log = None
    if not log_dir.exists():
        st.info(f"日志目录不存在：``{log_dir}``，可在侧边栏配置")
    elif log_df.empty:
        st.info(f"目录 ``{log_dir}`` 下没有 ``*.log`` 文件")
    else:
        # 搜索框：按日志文件名或关联账户过滤，便于在 30+ 个日志中定位
        log_search = st.text_input(
            "搜索日志文件 / 关联账户",
            value="",
            key="monitor_log_search",
            placeholder="如 alpha191、factor、etf（留空显示全部）",
        )
        keyword = log_search.strip().lower()
        if keyword:
            log_view = log_df[
                log_df["log_file"].str.lower().str.contains(keyword, na=False)
                | log_df["account_id"]
                .fillna("")
                .str.lower()
                .str.contains(keyword, na=False)
            ]
        else:
            log_view = log_df

        st.markdown(
            f"显示 {len(log_view)}/{len(log_df)} 个日志，点击行翻页浏览完整日志"
        )
        if log_view.empty:
            st.info(f"没有匹配「{keyword}」的日志")
        else:
            log_display = log_view.copy()
            log_display["account_id"] = log_display["account_id"].fillna("—")
            log_display["last_write"] = log_display["last_write"].map(
                lambda t: t.strftime("%m-%d %H:%M:%S")
            )
            log_display["last_log_time"] = log_display["last_log_time"].map(
                lambda t: t.strftime("%m-%d %H:%M:%S") if pd.notna(t) else "—"
            )
            log_display["minutes_idle"] = log_display["minutes_idle"].round(1)
            log_display["size_mb"] = log_display["size_mb"].round(1)
            log_display = log_display.rename(
                columns={
                    "log_file": "日志文件",
                    "account_id": "关联账户",
                    "process_status": "进程状态",
                    "last_write": "最后写入",
                    "minutes_idle": "闲置分钟",
                    "last_log_time": "最后日志时间",
                    "recent_errors": "近期错误数",
                    "last_error": "最近错误",
                    "size_mb": "大小 (MB)",
                }
            )
            log_event = st.dataframe(
                log_display[
                    [
                        "进程状态",
                        "日志文件",
                        "关联账户",
                        "闲置分钟",
                        "最后写入",
                        "最后日志时间",
                        "近期错误数",
                        "大小 (MB)",
                        "最近错误",
                    ]
                ],
                use_container_width=True,
                hide_index=True,
                # 加高表格，尽量让全部日志直接可见，超出的在表内滚动
                height=min(60 + 35 * (len(log_view) + 1), 1100),
                on_select="rerun",
                selection_mode="single-row",
                key="monitor_logs_table",
            )
            log_selected = log_event.selection
            if log_selected and log_selected.get("rows"):
                selected_log = str(log_view.iloc[log_selected["rows"][0]]["log_file"])

    # ── 选中日志详情（分页浏览完整历史）──
    if selected_log:
        log_path = log_dir / selected_log
        st.markdown("---")
        st.markdown(f"### 📄 日志详情：``{selected_log}``")
        if not log_path.exists():
            st.error(f"日志文件不存在：``{log_path}``")
        else:
            mtime = log_path.stat().st_mtime
            size_mb = log_path.stat().st_size / 1024 / 1024
            all_lines = _load_log_lines(str(log_path), mtime)

            opt_col1, opt_col2 = st.columns(2)
            with opt_col1:
                page_size = int(
                    st.selectbox(
                        "每页行数",
                        [100, 500, 2000],
                        index=1,
                        key="monitor_log_page_size",
                    )
                )
            with opt_col2:
                errors_only = st.checkbox(
                    "仅显示错误行",
                    value=False,
                    key="monitor_log_errors_only",
                )

            shown = (
                [line for line in all_lines if is_error_line(line)]
                if errors_only
                else all_lines
            )
            total = len(shown)
            total_pages = max(1, -(-total // page_size))

            # 页码导航：1 = 最新一页，数字越大越早；按钮改写页码后
            # 再创建 number_input，使其读到新值
            current_page = int(st.session_state.get("monitor_log_page", 1))
            nav_older, nav_newer = st.columns(2)
            with nav_older:
                if st.button(
                    "⬅ 更早", key="monitor_log_older", use_container_width=True
                ):
                    st.session_state.monitor_log_page = min(
                        current_page + 1, total_pages
                    )
            with nav_newer:
                if st.button(
                    "更新 ➡", key="monitor_log_newer", use_container_width=True
                ):
                    st.session_state.monitor_log_page = max(current_page - 1, 1)

            page = int(
                st.number_input(
                    f"页码（1 = 最新一页，共 {total_pages} 页）",
                    min_value=1,
                    max_value=total_pages,
                    value=1,
                    key="monitor_log_page",
                )
            )
            # 切换日志或过滤后总页数可能变小，钳制到合法范围
            page = max(1, min(page, total_pages))

            end = total - (page - 1) * page_size
            start = max(0, end - page_size)
            page_lines = shown[start:end]
            st.caption(
                f"文件 {size_mb:.1f} MB / 共 {len(all_lines):,} 行"
                + (
                    f"，其中错误行 {total:,} 行"
                    if errors_only and total != len(all_lines)
                    else ""
                )
                + f"；第 {page}/{total_pages} 页，"
                + f"显示第 {start + 1:,}–{end:,} 行"
            )
            if page_lines:
                st.code("\n".join(page_lines), language="text", wrap_lines=True)
            else:
                st.info("没有匹配的日志行")

    st.caption(f"数据更新于 {datetime.now():%Y-%m-%d %H:%M:%S}")


_render_monitor()
