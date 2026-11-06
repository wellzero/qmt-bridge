"""Streamlit 认证工具（已停用）。

应用户要求移除登录页（2026-10-24），本模块保留为空实现，
各页面的 `require_auth()` / `logout_button()` 调用无需改动。
原登录逻辑见 `.trash/auth.20261024_192827.py`，如需恢复直接还原该文件。
"""

from __future__ import annotations


def require_auth() -> None:
    """登录已移除，直接放行。"""
    return


def logout_button() -> None:
    """登录已移除，不渲染退出按钮。"""
    return
