"""Streamlit 认证工具。

多个页面共享的登录态与登录表单渲染。
"""

from __future__ import annotations

import hashlib
import hmac

import streamlit as st

_AUTH_USERNAME = "admin"
# SHA256(admin:quant2024) 的密码哈希，避免明文存储
_AUTH_PASSWORD_HASH = "8a050f9ba34664b9feb7735e0d6320944c9d725c772060406543e13fbb23f925"


def _hash_password(password: str) -> str:
    """计算密码的 SHA256 哈希。"""
    return hashlib.sha256(password.encode("utf-8"), usedforsecurity=True).hexdigest()


def _verify_password(password: str) -> bool:
    """恒定时间比较密码，降低时序攻击风险。"""
    return hmac.compare_digest(
        _hash_password(password),
        _AUTH_PASSWORD_HASH,
    )


def require_auth() -> None:
    """检查登录态；未登录时渲染登录表单并阻塞页面其余内容。"""
    if "authenticated" not in st.session_state:
        st.session_state.authenticated = False

    if st.session_state.authenticated:
        return

    st.set_page_config(
        page_title="模拟交易仪表盘",
        page_icon="📈",
        layout="wide",
    )

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


def logout_button() -> None:
    """在侧边栏渲染退出登录按钮。"""
    if st.sidebar.button("退出登录", use_container_width=True):
        st.session_state.authenticated = False
        st.rerun()
