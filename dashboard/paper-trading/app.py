"""模拟交易结果可视化仪表盘。

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

from components import (
    render_account_cards,
    render_account_detail,
    render_accounts_table,
)
from data_loader import (
    list_account_ids,
    load_all_summaries,
    load_config,
    resolve_data_dir,
)

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

st.set_page_config(
    page_title="模拟交易仪表盘",
    page_icon="📈",
    layout="wide",
)

st.title("模拟交易结果仪表盘")
st.caption("直接读取本地 ``data/paper_trading`` 目录，无需连接 qmt-server")

# ── 侧边栏 ──────────────────────────────────────────────────────────

with st.sidebar:
    st.header("数据目录")
    data_dir_input = st.text_input(
        "模拟交易数据目录",
        value=str(resolve_data_dir()),
        help="默认指向项目 ``data/paper_trading`` 目录",
    )
    if st.button("刷新数据", use_container_width=True):
        st.cache_data.clear()
        st.rerun()

    st.markdown("---")
    st.markdown("**运行方式**")
    st.code("streamlit run dashboard/paper-trading/app.py", language="bash")


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

# ── 总览 ─────────────────────────────────────────────────────────────

st.markdown("### 总览")
render_account_cards(summaries_df)

st.markdown("### 账户列表")
render_accounts_table(summaries_df)

st.markdown("---")

# ── 单个账户详情 ─────────────────────────────────────────────────────

st.markdown("### 账户详情")
if not account_ids:
    st.info("未找到任何模拟交易账户。")
    st.stop()

selected_account = st.selectbox("选择账户", account_ids, index=0)
account_config = config.get(selected_account, {})
render_account_detail(data_dir, selected_account, account_config)
