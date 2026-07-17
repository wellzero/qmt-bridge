"""模拟交易仪表盘页面组件。"""

from __future__ import annotations

from typing import Any

import pandas as pd
import plotly.graph_objects as go
import streamlit as st

from data_loader import load_all_orders, load_summary
from pricing import calculate_live_pnl, get_price_source_label, resolve_prices


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


def render_accounts_table(
    summaries_df: pd.DataFrame, key: str = "accounts_table"
) -> str | None:
    """渲染所有账户摘要表格，支持点击单行选择账户。

    返回被选中行的 ``account_id``；未选择时返回 ``None``。
    """
    if summaries_df.empty:
        return None

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

    event = st.dataframe(
        display_df,
        use_container_width=True,
        hide_index=True,
        on_select="rerun",
        selection_mode="single-row",
        key=key,
    )
    selected = event.selection
    if selected and selected.get("rows"):
        row_idx = selected["rows"][0]
        return str(summaries_df.iloc[row_idx]["account_id"])
    return None


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

    # ── 实时估算（当前价/收盘价）──────────────────────────────────────
    initial_cash = float(
        account_config.get("initial_cash", summary.get("initial_cash", 100_000))
    )
    live_positions_df = pd.DataFrame()
    live = {}
    if not orders_df.empty:
        stock_codes = orders_df["stock_code"].dropna().unique().tolist()
        prices = resolve_prices(data_dir, stock_codes, account_config)
        live = calculate_live_pnl(orders_df, prices, initial_cash)
        live_positions_df = live.get("positions", pd.DataFrame())

    if live:
        price_label = get_price_source_label(data_dir)
        st.markdown(f"#### 实时估算（基于 {price_label}）")
        lc1, lc2, lc3, lc4, lc5 = st.columns(5)
        with lc1:
            st.metric("估算总资产", f"{live['total_asset']:,.2f}")
        with lc2:
            st.metric("可用资金", f"{live['cash']:,.2f}")
        with lc3:
            st.metric("持仓市值", f"{live['market_value']:,.2f}")
        with lc4:
            st.metric("估算总盈亏", f"{live['total_pnl']:,.2f}")
        with lc5:
            st.metric("估算收益率", f"{live['total_return_rate'] * 100:.2f}%")

        if not live_positions_df.empty:
            display_positions = live_positions_df.copy()
            display_positions["current_price"] = display_positions[
                "current_price"
            ].fillna(display_positions["traded_price"])
            display_positions = display_positions.rename(
                columns={
                    "stock_code": "股票代码",
                    "volume": "持仓量",
                    "avg_cost": "成本均价",
                    "current_price": "当前价",
                    "market_value": "市值",
                    "unrealized_pnl": "浮动盈亏",
                    "trade_date": "最后交易日期",
                    "order_time": "最后交易时间",
                }
            )
            st.dataframe(display_positions, use_container_width=True, hide_index=True)
        else:
            st.info("当前无持仓。")
    else:
        st.info("暂无委托记录，无法估算实时盈亏。")

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
        chart_df["total_asset"] = (
            chart_df["account_cash"] + chart_df["account_market_value"]
        )

        def _order_hover_text(row: pd.Series) -> str:
            """为每个委托点生成悬浮提示文本。"""
            lines = [
                f"时间: {row['datetime']:%Y-%m-%d %H:%M:%S}",
                f"股票: {row.get('stock_code', '-')}",
                f"方向: {row.get('order_type_label', '-')}",
            ]
            if pd.notna(row.get("order_volume")):
                lines.append(f"委托量: {row['order_volume']}")
            if pd.notna(row.get("traded_volume")):
                lines.append(f"成交量: {row['traded_volume']}")
            if pd.notna(row.get("traded_price")):
                lines.append(f"成交价: {row['traded_price']:.3f}")
            if pd.notna(row.get("commission")):
                lines.append(f"手续费: {row['commission']:.2f}")
            if pd.notna(row.get("stamp_tax")):
                lines.append(f"印花税: {row['stamp_tax']:.2f}")
            if row.get("status"):
                lines.append(f"状态: {row['status']}")
            lines.append(f"总资产: {row['total_asset']:.2f}")
            return "<br>".join(lines)

        chart_df["hover_text"] = chart_df.apply(_order_hover_text, axis=1)

        fig = go.Figure()

        # 总资产曲线
        fig.add_trace(
            go.Scatter(
                x=chart_df["datetime"],
                y=chart_df["total_asset"],
                mode="lines",
                name="总资产",
                line=dict(color="#1f77b4", width=2),
                hovertemplate="总资产: %{y:,.2f}<br>时间: %{x}<extra>资产</extra>",
            )
        )

        # 委托标记点，悬浮显示委托详情
        fig.add_trace(
            go.Scatter(
                x=chart_df["datetime"],
                y=chart_df["total_asset"],
                mode="markers",
                name="委托",
                marker=dict(
                    size=10,
                    color="#ff7f0e",
                    symbol="diamond",
                    line=dict(width=1, color="#ffffff"),
                ),
                hovertemplate="%{text}<extra>委托详情</extra>",
                text=chart_df["hover_text"],
            )
        )

        fig.update_layout(
            title="总资产与委托时点（悬停查看委托详情）",
            xaxis_title="日期时间",
            yaxis_title="总资产",
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
        display_orders = orders_df.copy()
        # 若已解析到价格，为每笔委托补充最新价与按最新价估算的盈亏
        if live:
            display_orders["current_price"] = display_orders["stock_code"].map(prices)
            display_orders["current_price"] = display_orders["current_price"].fillna(
                display_orders["traded_price"]
            )

            def _order_pnl(row: pd.Series) -> float:
                """按最新价估算单笔委托盈亏：买入看多、卖出看空。"""
                price = row.get("current_price")
                trade_price = row.get("traded_price")
                volume = row.get("traded_volume")
                order_type = str(row.get("order_type", ""))
                if pd.isna(price) or pd.isna(trade_price) or pd.isna(volume):
                    return 0.0
                direction = 1.0 if order_type == "23" else -1.0
                return float((price - trade_price) * volume * direction)

            display_orders["pnl"] = display_orders.apply(_order_pnl, axis=1)

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
            "current_price",
            "pnl",
            "commission",
            "stamp_tax",
            "status",
        ]
        display_cols = [c for c in display_cols if c in display_orders.columns]
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
            "current_price": "最新价",
            "pnl": "估算盈亏",
            "commission": "手续费",
            "stamp_tax": "印花税",
            "status": "状态",
        }
        st.dataframe(
            display_orders[display_cols].rename(columns=rename),
            use_container_width=True,
            hide_index=True,
        )
