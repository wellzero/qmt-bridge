"""模拟交易仪表盘页面组件。"""

from __future__ import annotations

from typing import Any

import pandas as pd
import plotly.graph_objects as go
import streamlit as st

from data_loader import derive_positions, load_all_orders, load_summary


def render_account_cards(summaries_df: pd.DataFrame) -> None:
    """渲染账户概览卡片。"""
    if summaries_df.empty:
        st.info("暂无模拟交易账户数据。")
        return

    total_accounts = len(summaries_df)
    total_asset = summaries_df["total_asset"].sum()
    total_pnl = summaries_df["total_pnl"].sum()
    total_trades = int(summaries_df["total_trades"].sum())

    col1, col2, col3, col4 = st.columns(4)
    with col1:
        st.metric("账户数量", total_accounts)
    with col2:
        st.metric("总资产", f"{total_asset:,.2f}")
    with col3:
        st.metric("总盈亏", f"{total_pnl:,.2f}", delta=f"{total_pnl:,.2f}")
    with col4:
        st.metric("总成交笔数", total_trades)


def render_accounts_table(summaries_df: pd.DataFrame) -> None:
    """渲染所有账户摘要表格。"""
    if summaries_df.empty:
        return

    display_df = summaries_df.copy()
    rename_map = {
        "account_id": "账户 ID",
        "initial_cash": "初始资金",
        "cash": "可用资金",
        "market_value": "持仓市值",
        "total_asset": "总资产",
        "total_pnl": "总盈亏",
        "total_return_rate": "总收益率",
        "realized_pnl": "已实现盈亏",
        "unrealized_pnl": "未实现盈亏",
        "total_trades": "成交笔数",
        "total_commission": "累计手续费",
        "total_stamp_tax": "累计印花税",
    }
    display_df = display_df[[c for c in rename_map if c in display_df.columns]]
    display_df = display_df.rename(columns=rename_map)

    # 格式化收益率
    if "总收益率" in display_df.columns:
        display_df["总收益率"] = display_df["总收益率"].apply(
            lambda x: f"{x * 100:.2f}%" if pd.notna(x) else "-"
        )

    st.dataframe(display_df, use_container_width=True, hide_index=True)


def render_account_detail(
    data_dir: Any, account_id: str, account_config: dict[str, Any]
) -> None:
    """渲染单个账户的详细数据。"""
    summary = load_summary(data_dir, account_id)
    orders_df = load_all_orders(data_dir, account_id)

    st.subheader(f"账户：{account_id}")

    # ── 摘要指标 ────────────────────────────────────────────────────
    col1, col2, col3, col4, col5 = st.columns(5)
    with col1:
        st.metric("总资产", f"{float(summary.get('total_asset', 0)):,.2f}")
    with col2:
        st.metric("可用资金", f"{float(summary.get('cash', 0)):,.2f}")
    with col3:
        st.metric("持仓市值", f"{float(summary.get('market_value', 0)):,.2f}")
    with col4:
        st.metric("总盈亏", f"{float(summary.get('total_pnl', 0)):,.2f}")
    with col5:
        rate = float(summary.get("total_return_rate", 0)) * 100
        st.metric("总收益率", f"{rate:.2f}%")

    # ── 配置信息 ────────────────────────────────────────────────────
    with st.expander("账户配置"):
        cfg = account_config or {}
        cfg_cols = st.columns(3)
        cfg_items = [
            ("价格源", cfg.get("price_source", "-")),
            ("手续费率", f"{float(cfg.get('commission_rate', 0)):.4f}"),
            ("最低手续费", f"{float(cfg.get('min_commission', 0)):.2f}"),
            ("印花税率", f"{float(cfg.get('stamp_tax_rate', 0)):.4f}"),
            ("滑点", f"{float(cfg.get('slippage', 0)):.4f}"),
            ("初始资金", f"{float(cfg.get('initial_cash', 0)):,.2f}"),
        ]
        for (label, value), col in zip(cfg_items, cfg_cols * 2):
            col.markdown(f"**{label}：** {value}")

    # ── 资产走势 ────────────────────────────────────────────────────
    if not orders_df.empty and {
        "trade_date",
        "order_time",
        "account_cash",
        "account_market_value",
    }.issubset(set(orders_df.columns)):
        orders_df["datetime"] = pd.to_datetime(
            orders_df["trade_date"] + " " + orders_df["order_time"],
            errors="coerce",
        )
        chart_df = orders_df.dropna(subset=["datetime"]).sort_values("datetime")

        fig = go.Figure()
        fig.add_trace(
            go.Scatter(
                x=chart_df["datetime"],
                y=chart_df["account_cash"],
                mode="lines+markers",
                name="可用资金",
            )
        )
        fig.add_trace(
            go.Scatter(
                x=chart_df["datetime"],
                y=chart_df["account_market_value"],
                mode="lines+markers",
                name="持仓市值",
            )
        )
        fig.add_trace(
            go.Scatter(
                x=chart_df["datetime"],
                y=chart_df["account_cash"] + chart_df["account_market_value"],
                mode="lines+markers",
                name="总资产",
            )
        )
        fig.update_layout(
            title="资产变化趋势",
            xaxis_title="时间",
            yaxis_title="金额",
            legend=dict(
                orientation="h", yanchor="bottom", y=1.02, xanchor="right", x=1
            ),
            hovermode="x unified",
        )
        st.plotly_chart(fig, use_container_width=True)
    else:
        st.info(
            "当前委托记录缺少 ``account_cash`` / ``account_market_value`` 字段，无法绘制资产走势。"
        )

    # ── 委托记录 ────────────────────────────────────────────────────
    st.markdown("#### 委托记录")
    if orders_df.empty:
        st.info("暂无委托记录。")
    else:
        display_cols = [
            "trade_date",
            "order_time",
            "order_id",
            "stock_code",
            "order_type_label",
            "order_volume",
            "price",
            "traded_volume",
            "traded_price",
            "commission",
            "stamp_tax",
            "status",
        ]
        display_cols = [c for c in display_cols if c in orders_df.columns]
        rename = {
            "trade_date": "日期",
            "order_time": "时间",
            "order_id": "委托号",
            "stock_code": "股票代码",
            "order_type_label": "方向",
            "order_volume": "委托量",
            "price": "委托价",
            "traded_volume": "成交量",
            "traded_price": "成交价",
            "commission": "手续费",
            "stamp_tax": "印花税",
            "status": "状态",
        }
        st.dataframe(
            orders_df[display_cols].rename(columns=rename),
            use_container_width=True,
            hide_index=True,
        )

    # ── 推导持仓 ────────────────────────────────────────────────────
    st.markdown("#### 推导持仓（由委托记录计算，仅供参考）")
    positions_df = derive_positions(orders_df)
    if positions_df.empty:
        st.info("暂无持仓。")
    else:
        positions_df = positions_df.rename(
            columns={
                "stock_code": "股票代码",
                "volume": "持仓量",
                "traded_price": "参考成交价",
                "market_value": "参考市值",
                "trade_date": "最后交易日期",
                "order_time": "最后交易时间",
            }
        )
        st.dataframe(positions_df, use_container_width=True, hide_index=True)
