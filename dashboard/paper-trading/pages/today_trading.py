"""今日交易账户页面。

展示所有在当日有委托记录的模拟交易账户，并提供简要看盘信息。
"""

from __future__ import annotations

import logging
from datetime import datetime

import pandas as pd
import streamlit as st

from auth import logout_button, require_auth
from components import render_account_detail
from data_loader import list_account_ids, load_all_orders, load_config, resolve_data_dir
from pricing import build_live_summaries_df

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

st.set_page_config(
    page_title="Today's Trading Accounts - Trading Summary",
    page_icon="📅",
    layout="wide",
)

require_auth()


def _today_str() -> str:
    """返回当日 ``YYYYMMDD`` 字符串。"""
    return datetime.now().strftime("%Y%m%d")


st.title("📅 Today's Trading Accounts")
today = _today_str()
st.caption(f"日期：{today[:4]}-{today[4:6]}-{today[6:]}")

# ── 侧边栏 ──────────────────────────────────────────────────────────

with st.sidebar:
    st.header("数据目录")
    data_dir_input = st.text_input(
        "模拟交易数据目录",
        value=str(resolve_data_dir()),
        key="today_data_dir_input",
        help="默认指向项目 ``data/paper_trading`` 目录",
    )
    if st.button("刷新数据", use_container_width=True, key="today_refresh"):
        st.cache_data.clear()
        st.rerun()

    st.markdown("---")
    logout_button()


data_dir = resolve_data_dir(data_dir_input)

if not data_dir.exists():
    st.error(f"数据目录不存在：``{data_dir}``")
    st.stop()

# ── 加载数据 ─────────────────────────────────────────────────────────


@st.cache_data(ttl=30)
def _load_today_data(data_dir_str: str, date_str: str):
    """缓存加载当日交易数据。"""
    d = resolve_data_dir(data_dir_str)
    config = load_config(d)
    live_summaries = build_live_summaries_df(d, config)

    accounts_today: list[dict[str, object]] = []
    for account_id in list_account_ids(d):
        orders = load_all_orders(d, account_id)
        if orders.empty or "trade_date" not in orders.columns:
            continue
        today_orders = orders[orders["trade_date"] == date_str]
        if today_orders.empty:
            continue

        buy_count = int((today_orders["order_type"].astype(str) == "23").sum())
        sell_count = int((today_orders["order_type"].astype(str) == "24").sum())
        stock_codes = sorted(today_orders["stock_code"].dropna().unique())

        live = live_summaries[live_summaries["account_id"] == account_id]
        if not live.empty:
            total_asset = float(live.iloc[0]["total_asset"])
            total_pnl = float(live.iloc[0]["total_pnl"])
            total_return_rate = float(live.iloc[0]["total_return_rate"])
        else:
            total_asset = 0.0
            total_pnl = 0.0
            total_return_rate = 0.0

        accounts_today.append(
            {
                "account_id": account_id,
                "orders_today": len(today_orders),
                "buy_count": buy_count,
                "sell_count": sell_count,
                "stocks": ", ".join(stock_codes),
                "total_asset": total_asset,
                "total_pnl": total_pnl,
                "total_return_rate": total_return_rate,
            }
        )

    return config, pd.DataFrame(
        accounts_today,
        columns=[
            "account_id",
            "orders_today",
            "buy_count",
            "sell_count",
            "stocks",
            "total_asset",
            "total_pnl",
            "total_return_rate",
        ],
    )


config, today_df = _load_today_data(str(data_dir), today)

# ── 总览 ─────────────────────────────────────────────────────────────

if today_df.empty:
    st.info(f"今日（{today}）暂无任何账户交易。")
    st.stop()

st.markdown("### 今日交易总览")
col1, col2, col3, col4 = st.columns(4)
with col1:
    st.metric("交易账户数", len(today_df))
with col2:
    st.metric("今日总委托数", int(today_df["orders_today"].sum()))
with col3:
    st.metric("今日买入笔数", int(today_df["buy_count"].sum()))
with col4:
    st.metric("今日卖出笔数", int(today_df["sell_count"].sum()))

# ── 账户列表 ─────────────────────────────────────────────────────────

st.markdown("### 今日交易账户列表（点击行查看详情）")
display_df = today_df.copy()
display_df["total_return_rate"] = display_df["total_return_rate"].apply(
    lambda x: f"{x * 100:.2f}%" if pd.notna(x) else "-"
)
rename_map = {
    "account_id": "账户 ID",
    "orders_today": "今日委托数",
    "buy_count": "买入笔数",
    "sell_count": "卖出笔数",
    "stocks": "涉及股票",
    "total_asset": "总资产",
    "total_pnl": "总盈亏",
    "total_return_rate": "总收益率",
}
display_df = display_df[[c for c in rename_map if c in display_df.columns]]
display_df = display_df.rename(columns=rename_map)

event = st.dataframe(
    display_df,
    use_container_width=True,
    hide_index=True,
    on_select="rerun",
    selection_mode="single-row",
    key="today_accounts_table",
)

selected_account = None
selected = event.selection
if selected and selected.get("rows"):
    row_idx = selected["rows"][0]
    selected_account = str(today_df.iloc[row_idx]["account_id"])

# ── 选中账户详情 ─────────────────────────────────────────────────────

if selected_account:
    st.markdown("---")
    st.markdown("### 账户详情")
    account_config = config.get(selected_account, {})
    render_account_detail(data_dir, selected_account, account_config)

    # 额外展示今日委托明细
    st.markdown("#### 今日委托明细")
    orders = load_all_orders(data_dir, selected_account)
    today_orders = orders[orders["trade_date"] == today]
    display_cols = [
        "order_time",
        "order_id",
        "stock_code",
        "order_type_label",
        "order_volume",
        "traded_volume",
        "traded_price",
        "commission",
        "stamp_tax",
        "status",
    ]
    display_cols = [c for c in display_cols if c in today_orders.columns]
    rename = {
        "order_time": "时间",
        "order_id": "委托号",
        "stock_code": "股票代码",
        "order_type_label": "方向",
        "order_volume": "委托量",
        "traded_volume": "成交量",
        "traded_price": "成交价",
        "commission": "手续费",
        "stamp_tax": "印花税",
        "status": "状态",
    }
    st.dataframe(
        today_orders[display_cols].rename(columns=rename),
        use_container_width=True,
        hide_index=True,
    )
