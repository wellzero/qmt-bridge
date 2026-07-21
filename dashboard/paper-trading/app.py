"""Trading Summary.

独立运行：

    streamlit run dashboard/paper-trading/app.py

或：

    just paper-dashboard

默认读取 ``data/paper_trading`` 目录；可通过侧边栏修改路径，
或设置环境变量 ``PAPER_TRADING_DATA_DIR``。
"""

from __future__ import annotations

import logging

import streamlit as st

from auth import logout_button, require_auth
from components import (
    render_account_cards,
    render_account_detail,
    render_accounts_table,
)
from data_loader import (
    list_account_ids,
    load_all_orders,
    load_all_summaries,
    load_config,
    resolve_data_dir,
)
from pricing import (
    build_live_summaries_df,
    fetch_prices_from_server,
    save_price_cache,
)

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

st.set_page_config(
    page_title="Trading Summary",
    page_icon="📈",
    layout="wide",
)

require_auth()


def _do_fetch_prices() -> None:
    """从 qmt-server 拉取最新价、保存缓存并重新计算所有账户实时统计。"""
    data_dir_input = st.session_state.get("data_dir_input", str(resolve_data_dir()))
    data_dir = resolve_data_dir(data_dir_input)

    if not data_dir.exists():
        st.error(f"数据目录不存在：``{data_dir}``")
        return

    server_host = st.session_state.get("server_host_input", "localhost")
    server_port = st.session_state.get("server_port_input", 8083)
    server_api_key = st.session_state.get("server_api_key_input", "")

    account_ids = list_account_ids(data_dir)
    all_stock_codes: set[str] = set()
    for aid in account_ids:
        orders = load_all_orders(data_dir, aid)
        if not orders.empty and "stock_code" in orders.columns:
            all_stock_codes.update(orders["stock_code"].dropna().unique())

    if not all_stock_codes:
        st.warning("未找到任何股票代码，无法更新价格")
        return

    try:
        prices = fetch_prices_from_server(
            server_host,
            int(server_port),
            server_api_key,
            sorted(all_stock_codes),
        )
    except Exception as exc:
        st.error(f"获取行情失败：{exc}")
        return

    if not prices:
        st.warning("未能获取到任何有效价格")
        return

    save_price_cache(data_dir, prices, close=False)

    # 重新计算所有账户实时统计
    config = load_config(data_dir)
    live_summaries = build_live_summaries_df(data_dir, config)
    st.session_state["last_price_update"] = len(prices)

    st.cache_data.clear()
    st.success(
        f"已更新 {len(prices)} 只股票最新价，并重新计算 {len(live_summaries)} 个账户实时盈亏"
    )
    st.rerun()


st.title("Trading Summary")
st.caption("直接读取本地 ``data/paper_trading`` 目录，无需连接 qmt-server")

if st.session_state.get("last_price_update"):
    st.info(
        f"已获取最新价：共更新 {st.session_state['last_price_update']} 只股票，账户统计已按最新价重新计算"
    )

# ── 侧边栏 ──────────────────────────────────────────────────────────

with st.sidebar:
    st.header("数据目录")
    data_dir_input = st.text_input(
        "模拟交易数据目录",
        value=str(resolve_data_dir()),
        key="data_dir_input",
        help="默认指向项目 ``data/paper_trading`` 目录",
    )
    if st.button("刷新数据", use_container_width=True):
        st.cache_data.clear()
        st.rerun()

    st.markdown("---")
    st.markdown("**运行方式**")
    st.code("streamlit run dashboard/paper-trading/app.py", language="bash")

    st.markdown("---")
    st.markdown("**行情更新**")
    st.text_input(
        "qmt-server 主机",
        value=st.session_state.get("server_host_input", "localhost"),
        key="server_host_input",
    )
    st.number_input(
        "qmt-server 端口",
        min_value=1,
        max_value=65535,
        value=int(st.session_state.get("server_port_input", 8083)),
        key="server_port_input",
    )
    st.text_input(
        "API Key",
        value=st.session_state.get("server_api_key_input", ""),
        type="password",
        key="server_api_key_input",
    )

    if st.button("获取最新价", use_container_width=True, type="primary"):
        _do_fetch_prices()

    st.markdown("---")
    logout_button()


data_dir = resolve_data_dir(data_dir_input)

if not data_dir.exists():
    st.error(f"数据目录不存在：``{data_dir}``")
    st.stop()

# ── 加载数据 ─────────────────────────────────────────────────────────


@st.cache_data(ttl=30)
def _load_all(data_dir_str: str):
    """缓存加载，减少磁盘 IO。"""
    d = resolve_data_dir(data_dir_str)
    config = load_config(d)
    summaries = load_all_summaries(d)
    accounts = list_account_ids(d)
    return config, summaries, accounts


config, summaries_df, account_ids = _load_all(str(data_dir))

# 基于最新价格缓存重新计算所有账户实时盈亏，用于总览与账户列表
live_summaries_df = build_live_summaries_df(data_dir, config, summaries_df)

# ── 总览 ─────────────────────────────────────────────────────────────

st.markdown("### 总览")
render_account_cards(live_summaries_df)

st.markdown("### 账户列表（点击行可切换下方账户详情）")
selected_from_table = render_accounts_table(live_summaries_df, key="accounts_table")

st.markdown("---")

# ── 单个账户详情 ─────────────────────────────────────────────────────

st.markdown("### 账户详情")
if not account_ids:
    st.info("未找到任何模拟交易账户。")
    st.stop()

# 用 session_state 记住表格选中的账户；无选择时默认第一个
if "selected_account" not in st.session_state:
    st.session_state.selected_account = account_ids[0]

if selected_from_table and selected_from_table in account_ids:
    st.session_state.selected_account = selected_from_table
elif st.session_state.selected_account not in account_ids:
    st.session_state.selected_account = account_ids[0]

selected_account = st.session_state.selected_account
st.caption(f"当前展示账户：{selected_account}（在上方账户列表点击行可切换）")

account_config = config.get(selected_account, {})
render_account_detail(data_dir, selected_account, account_config)
