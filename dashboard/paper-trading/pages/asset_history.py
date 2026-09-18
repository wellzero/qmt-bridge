"""资产走势页面。

展示全部模拟交易账户在所有交易日上的总资产走势：

- 组合合计总资产曲线（单线 + 初始资金参考线）
- 每账户小倍数分面图（按期末收益正负着色，悬浮查看任意交易日总资产）
- 全账户期末指标明细表

数据口径：委托日的期末值取当日最后一笔成交委托的 账户现金 + 市值
（引擎快照，精确含费用）；无委托日在有日收盘价缓存时按
现金 + 持仓 × 当日收盘价 重估，否则沿用前值；
账户首个交易日前按初始资金补齐。收盘价缓存可通过侧边栏
「更新每日收盘价」从 qmt-server 拉取（首次会触发历史补下载）。
"""

from __future__ import annotations

import logging
from datetime import datetime
from pathlib import Path

import streamlit as st

from auth import logout_button, require_auth
from components import render_asset_history, render_big_title
from data_loader import load_asset_history, resolve_data_dir
from pricing import (
    ensure_daily_closes,
    load_daily_close_cache,
    revalue_asset_history_tail,
    save_daily_close_cache,
)

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

st.set_page_config(
    page_title="Asset History - Trading Summary",
    page_icon="📊",
    layout="wide",
)

require_auth()

render_big_title("📊 Asset History")


def _do_update_closes(data_dir: Path, wide_df, holdings) -> None:
    """从 qmt-server 拉取全部持仓的日收盘价（含历史补下载）并保存缓存。"""
    codes = sorted({c for h in holdings.values() for c in (h.get("volumes") or {})})
    if not codes:
        st.warning("当前没有任何账户持仓，无需拉取收盘价")
        return

    host = st.session_state.get("server_host_input", "localhost")
    port = int(st.session_state.get("server_port_input", 8083))
    api_key = st.session_state.get("server_api_key_input", "")
    start = str(wide_df.index[0])
    end = datetime.now().strftime("%Y%m%d")

    try:
        with st.spinner(
            f"正在从 qmt-server 拉取 {len(codes)} 只持仓的日收盘价"
            "（首次会先补下载历史 K 线，可能需要几分钟）…"
        ):
            closes, missing = ensure_daily_closes(
                host, port, api_key, codes, start, end
            )
    except Exception as exc:
        st.error(f"拉取日收盘价失败：{exc}")
        return

    if not closes:
        st.warning("未能获取到任何日收盘价")
        return

    save_daily_close_cache(data_dir, closes, start, end)
    st.session_state["asset_closes_update"] = {
        "n_codes": len(closes),
        "n_missing": len(missing),
    }
    st.rerun()


# ── 侧边栏（第一部分：数据目录 + server 配置）──────────────────────

with st.sidebar:
    st.header("数据目录")
    data_dir_input = st.text_input(
        "模拟交易数据目录",
        value=str(resolve_data_dir()),
        key="data_dir_input",
        help="默认指向项目 ``data/paper_trading`` 目录",
    )
    if st.button("刷新数据", use_container_width=True, key="asset_history_refresh"):
        st.cache_data.clear()
        st.rerun()

    st.markdown("---")
    st.markdown("**收盘价重估**")
    st.caption(
        "无委托日的总资产按 持仓 × 日收盘价 重估；缓存缺失或过期时"
        "点击下方「更新每日收盘价」"
    )

    # 与主页「行情更新」共享同一组 server 配置（key 相同，值互通）
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

data_dir = resolve_data_dir(data_dir_input)

if not data_dir.exists():
    st.error(f"数据目录不存在：``{data_dir}``")
    st.stop()

# ── 加载数据 ─────────────────────────────────────────────────────────


@st.cache_data(ttl=60)
def _load_history(data_dir_str: str):
    """缓存加载全部账户 × 全交易日的期末总资产宽表与当前持仓。"""
    return load_asset_history(resolve_data_dir(data_dir_str))


wide_df, initial_map, stats_df, holdings = _load_history(str(data_dir))

if wide_df.empty:
    st.info("暂无任何委托记录，无法绘制资产走势。")
    st.stop()

# 无委托日按收盘价重估（有缓存时）
close_cache = load_daily_close_cache(data_dir)
closes = close_cache.get("closes") or {}
if closes:
    wide_df = revalue_asset_history_tail(wide_df, holdings, closes)

if close_update := st.session_state.pop("asset_closes_update", None):
    st.success(
        f"已更新 {close_update['n_codes']} 只股票的日收盘价并重估无委托日资产"
        + (
            f"；{close_update['n_missing']} 个代码无数据（退市/长期停牌属正常）"
            if close_update["n_missing"]
            else ""
        )
    )

if closes:
    st.caption(
        "期末值口径：委托日取引擎快照（现金+市值，精确含费用），无委托日按"
        f" 持仓 × 日收盘价 重估（收盘价截至 {close_cache.get('end_time', '')}，"
        f"缓存于 {str(close_cache.get('timestamp', ''))[:16]}），"
        "首个交易日前为初始资金"
    )
else:
    st.warning(
        "暂无日收盘价缓存：无委托日的总资产沿用前值（持仓不重新估值）。"
        "可在侧边栏填写 qmt-server 地址后点击「更新每日收盘价」"
    )

render_asset_history(wide_df, initial_map, stats_df)

# ── 侧边栏（第二部分：更新按钮 + 退出登录）─────────────────────────

with st.sidebar:
    if st.button(
        "更新每日收盘价",
        use_container_width=True,
        type="primary",
        key="asset_update_closes",
    ):
        _do_update_closes(data_dir, wide_df, holdings)

    st.markdown("---")
    logout_button()
