"""模拟交易结果可视化仪表盘。

独立运行：

    streamlit run dashboard/paper-trading/app.py

或：

    just paper-dashboard

默认读取 ``data/paper_trading`` 目录；可通过侧边栏修改路径，
或设置环境变量 ``PAPER_TRADING_DATA_DIR``。
"""

from __future__ import annotations

import hashlib
import hmac
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

# ── 认证配置 ──────────────────────────────────────────────────────────

_AUTH_USERNAME = "admin"
# SHA256(admin:quant2024) 的密码哈希，避免明文存储
_AUTH_PASSWORD_HASH = (
    "8a050f9ba34664b9feb7735e0d6320944c9d725c772060406543e13fbb23f925"
)


def _hash_password(password: str) -> str:
    """计算密码的 SHA256 哈希。"""
    return hashlib.sha256(password.encode("utf-8"), usedforsecurity=True).hexdigest()


def _verify_password(password: str) -> bool:
    """恒定时间比较密码，降低时序攻击风险。"""
    return hmac.compare_digest(
        _hash_password(password),
        _AUTH_PASSWORD_HASH,
    )


def _render_login() -> None:
    """渲染登录表单；验证通过前会阻塞后续页面内容。"""
    st.title("🔒 模拟交易仪表盘")
    st.caption("请输入用户名和密码登录")

    with st.form("login_form", clear_on_submit=True):
        username = st.text_input("用户名", value="", placeholder="admin")
        password = st.text_input("密码", type="password", placeholder="请输入密码")
        submitted = st.form_submit_button("登录", use_container_width=True)

        if submitted:
            if username == _AUTH_USERNAME and _verify_password(password):
                st.session_state.authenticated = True
                st.rerun()
            else:
                st.error("用户名或密码错误")

    st.stop()


if "authenticated" not in st.session_state:
    st.session_state.authenticated = False

st.set_page_config(
    page_title="模拟交易仪表盘",
    page_icon="📈",
    layout="wide",
)

if not st.session_state.authenticated:
    _render_login()

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

    st.markdown("---")
    if st.button("退出登录", use_container_width=True):
        st.session_state.authenticated = False
        st.rerun()


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

st.markdown("### 账户列表（点击行可切换下方账户详情）")
selected_from_table = render_accounts_table(summaries_df, key="accounts_table")

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
