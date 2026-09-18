"""模拟交易仪表盘页面组件。"""

from __future__ import annotations

from typing import Any

import pandas as pd
import plotly.graph_objects as go
import streamlit as st
from plotly.subplots import make_subplots

from data_loader import load_all_orders, load_summary
from pricing import calculate_live_pnl, get_price_source_label, resolve_prices


def render_big_title(title: str) -> None:
    """渲染大号页面标题。"""
    st.markdown(
        f"""
        <style>
        .big-title {{
            font-size: 3rem !important;
            font-weight: 700;
            margin-bottom: 0.5rem;
        }}
        </style>
        <h1 class="big-title">{title}</h1>
        """,
        unsafe_allow_html=True,
    )


def render_account_cards(summaries_df: pd.DataFrame) -> None:
    """渲染账户概览卡片。"""
    if summaries_df.empty:
        st.info("暂无模拟交易账户数据。")
        return

    total_accounts = len(summaries_df)
    total_asset = summaries_df["total_asset"].sum()
    total_pnl = summaries_df["total_pnl"].sum()
    total_trades = int(summaries_df["total_trades"].sum())
    total_initial_cash = summaries_df["initial_cash"].sum()
    total_return_rate = (total_pnl / total_initial_cash) if total_initial_cash else 0.0

    col1, col2, col3, col4, col5 = st.columns(5)
    with col1:
        st.metric("账户数量", total_accounts)
    with col2:
        st.metric("总资产", f"{total_asset:,.2f}")
    with col3:
        st.metric("总盈亏", f"{total_pnl:,.2f}", delta=f"{total_pnl:,.2f}")
    with col4:
        st.metric("总收益率", f"{total_return_rate * 100:.2f}%")
    with col5:
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

    # 总收益率保持数值类型，由 column_config 格式化显示，确保可正确排序
    if "总收益率" in display_df.columns:
        display_df["总收益率"] = display_df["总收益率"] * 100

    event = st.dataframe(
        display_df,
        use_container_width=True,
        hide_index=True,
        on_select="rerun",
        selection_mode="single-row",
        key=key,
        column_config={
            "总收益率": st.column_config.NumberColumn(
                "总收益率", format="%.2f%%", help="按数值排序"
            )
        },
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

    # ── 实时盈亏（基于当前/收盘最新价）─────────────────────────────────
    initial_cash = float(
        account_config.get("initial_cash", summary.get("initial_cash", 100_000))
    )
    live_positions_df = pd.DataFrame()
    live: dict[str, Any] = {}
    prices: dict[str, float] = {}
    if not orders_df.empty:
        stock_codes = orders_df["stock_code"].dropna().unique().tolist()
        prices = resolve_prices(data_dir, stock_codes, account_config)
        live = calculate_live_pnl(orders_df, prices, initial_cash)
        live_positions_df = live.get("positions", pd.DataFrame())

    if live:
        price_label = get_price_source_label(data_dir)
        st.caption(f"价格来源：{price_label}")

        col1, col2, col3, col4, col5 = st.columns(5)
        with col1:
            st.metric("总资产", f"{live['total_asset']:,.2f}")
        with col2:
            st.metric("可用资金", f"{live['cash']:,.2f}")
        with col3:
            st.metric("持仓市值", f"{live['market_value']:,.2f}")
        with col4:
            st.metric("总盈亏", f"{live['total_pnl']:,.2f}")
        with col5:
            st.metric("总收益率", f"{live['total_return_rate'] * 100:.2f}%")

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
        # 无委托记录时，回退到 summary.json；仅有 config 注册的账户
        # （尚无 summary.json）用初始资金兜底，避免全部显示为 0
        col1, col2, col3, col4, col5 = st.columns(5)
        with col1:
            st.metric(
                "总资产", f"{float(summary.get('total_asset', initial_cash)):,.2f}"
            )
        with col2:
            st.metric("可用资金", f"{float(summary.get('cash', initial_cash)):,.2f}")
        with col3:
            st.metric("持仓市值", f"{float(summary.get('market_value', 0)):,.2f}")
        with col4:
            st.metric("总盈亏", f"{float(summary.get('total_pnl', 0)):,.2f}")
        with col5:
            rate = float(summary.get("total_return_rate", 0)) * 100
            st.metric("总收益率", f"{rate:.2f}%")
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
        # y/text 转纯列表：pandas Series 会被 plotly 6 的 orjson 路径编码为
        # bdata 二进制格式，Streamlit 内嵌 plotly.js 解码失败导致曲线不渲染
        chart_x = chart_df["datetime"].tolist()
        chart_y = _plain_list(chart_df["total_asset"])
        chart_text = chart_df["hover_text"].tolist()

        # 总资产曲线
        fig.add_trace(
            go.Scatter(
                x=chart_x,
                y=chart_y,
                mode="lines",
                name="总资产",
                line=dict(color="#1f77b4", width=2),
                hovertemplate="总资产: %{y:,.2f}<br>时间: %{x}<extra>资产</extra>",
            )
        )

        # 委托标记点，悬浮显示委托详情
        fig.add_trace(
            go.Scatter(
                x=chart_x,
                y=chart_y,
                mode="markers",
                name="委托",
                marker=dict(
                    size=10,
                    color="#ff7f0e",
                    symbol="diamond",
                    line=dict(width=1, color="#ffffff"),
                ),
                hovertemplate="%{text}<extra>委托详情</extra>",
                text=chart_text,
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


# ── 资产走势（全部账户 × 全交易日）──────────────────────────────────

# 走势指标口径
_ASSET_METRIC_ASSET = "总资产 (元)"
_ASSET_METRIC_RETURN = "累计收益率 (%)"

# 小倍数面板布局：每行面板数与单面板高度（像素）
_PANEL_COLS = 4
_PANEL_HEIGHT = 110


def _viz_colors() -> dict[str, str]:
    """按 Streamlit 主题基色返回图表颜色（已通过 CVD/对比度校验的红蓝发散对）。"""
    try:
        dark = (st.get_option("theme.base") or "light") == "dark"
    except Exception:
        dark = False
    return {
        # 蓝 = 期末收益 ≥ 0，红 = 期末收益 < 0；灰为初始资金参考线
        "pos": "#3987e5" if dark else "#2a78d6",
        "neg": "#e66767" if dark else "#e34948",
        "baseline": "#383835" if dark else "#c3c2b7",
    }


def _short_account_name(account_id: str) -> str:
    """面板标题与指标卡用的短账户名（去掉 ``_paper`` 后缀）。"""
    return account_id.removesuffix("_paper") or account_id


def _fmt_date(yyyymmdd: str) -> str:
    """YYYYMMDD -> YYYY-MM-DD，长度不符时原样返回。"""
    return (
        f"{yyyymmdd[:4]}-{yyyymmdd[4:6]}-{yyyymmdd[6:]}"
        if isinstance(yyyymmdd, str) and len(yyyymmdd) == 8
        else str(yyyymmdd)
    )


def _plain_list(values) -> list:
    """把 pandas Series / numpy 数组转为纯 Python 数值列表。

    plotly 6 的 orjson 序列化路径会把 numpy 数组编码为二进制 bdata 格式，
    Streamlit 内嵌的 plotly.js 无法解码、曲线整条不渲染，因此所有传入
    trace 的 y / customdata 必须先转为纯列表。NaN 转 None（plotly 缺口）。
    """
    if values is None:
        return []
    try:
        return [None if v != v else float(v) for v in values]
    except TypeError:
        return list(values)


def _portfolio_figure(
    dates, values: pd.Series, initial_total: float, metric: str, colors: dict
) -> go.Figure:
    """组合合计总资产单线图（单序列无需图例，悬浮同时展示两种口径）。"""
    is_return = metric == _ASSET_METRIC_RETURN
    if is_return:
        y = _plain_list((values / initial_total - 1) * 100)
        customdata = _plain_list(values)
        hover = (
            "组合收益率 %{y:.2f}%<br>总资产 ¥%{customdata:,.0f}"
            "<br>%{x|%Y-%m-%d}<extra>组合合计</extra>"
        )
        y_title, baseline = "累计收益率（%）", 0.0
    else:
        y = _plain_list(values)
        customdata = _plain_list((values / initial_total - 1) * 100)
        hover = (
            "组合总资产 ¥%{y:,.0f}<br>收益率 %{customdata:+.2f}%"
            "<br>%{x|%Y-%m-%d}<extra>组合合计</extra>"
        )
        y_title, baseline = "总资产（元）", initial_total

    fig = go.Figure()
    fig.add_trace(
        go.Scatter(
            x=dates,
            y=y,
            mode="lines",
            line=dict(color=colors["pos"], width=2),
            customdata=customdata,
            hovertemplate=hover,
        )
    )
    # layer="below"：参考线画在数据线下方，避免近水平收益曲线被参考线遮挡
    fig.add_hline(
        y=baseline, line=dict(color=colors["baseline"], width=1), layer="below"
    )
    fig.update_layout(
        yaxis_title=y_title,
        hovermode="x unified",
        margin=dict(l=10, r=10, t=10, b=10),
        xaxis=dict(tickformat="%m-%d"),
    )
    return fig


def _panels_figure(
    wide_df: pd.DataFrame, initial_map: dict[str, float], metric: str, colors: dict
) -> go.Figure:
    """每账户一个分面面板：期末收益极性着色 + 参考线 + 名称/收益率直接标注。

    账户数远超分类色板 8 色上限，按可视化规范用小倍数分面而非更多色相；
    每个面板只有一条序列，颜色编码"期末相对初始资金的极性"（红蓝发散对），
    序列身份由面板标题承载。收益率口径下全部面板共享纵轴便于横向比较，
    总资产口径下各面板独立缩放。
    """
    is_return = metric == _ASSET_METRIC_RETURN
    dates = pd.to_datetime(wide_df.index, format="%Y%m%d")
    initial_wide = pd.Series(initial_map).reindex(wide_df.columns)

    finals = wide_df.iloc[-1]
    rets = finals / initial_wide - 1
    order = rets.sort_values(ascending=False).index.tolist()

    n = len(order)
    rows = (n + _PANEL_COLS - 1) // _PANEL_COLS
    fig = make_subplots(
        rows=rows,
        cols=_PANEL_COLS,
        shared_xaxes=True,
        shared_yaxes=is_return,
        subplot_titles=[
            f"{_short_account_name(aid)}<br>{rets[aid] * 100:+.2f}%" for aid in order
        ],
        horizontal_spacing=0.03,
        vertical_spacing=0.085 if rows > 1 else 0.0,
    )

    for i, aid in enumerate(order):
        row, col = divmod(i, _PANEL_COLS)
        initial = float(initial_wide[aid])
        vals = wide_df[aid]
        if is_return:
            y = _plain_list((vals / initial - 1) * 100)
            customdata = _plain_list(vals)
            y_last, c_last = y[-1], customdata[-1]
            hover = (
                "收益率 %{y:.2f}%<br>总资产 ¥%{customdata:,.0f}"
                f"<br>%{{x|%Y-%m-%d}}<extra>{_short_account_name(aid)}</extra>"
            )
            baseline = 0.0
        else:
            y = _plain_list(vals)
            customdata = _plain_list((vals / initial - 1) * 100)
            y_last, c_last = y[-1], customdata[-1]
            hover = (
                "总资产 ¥%{y:,.0f}<br>较初始 %{customdata:+.2f}%"
                f"<br>%{{x|%Y-%m-%d}}<extra>{_short_account_name(aid)}</extra>"
            )
            baseline = initial
        color = colors["pos"] if rets[aid] >= 0 else colors["neg"]
        fig.add_trace(
            go.Scatter(
                x=dates,
                y=y,
                mode="lines",
                line=dict(color=color, width=1.6),
                customdata=customdata,
                hovertemplate=hover,
            ),
            row=row + 1,
            col=col + 1,
        )
        # 末交易日端点圆点：让"今天/最新值"在面板上一眼可见
        fig.add_trace(
            go.Scatter(
                x=[dates[-1]],
                y=[y_last],
                mode="markers",
                marker=dict(size=4, color=color),
                customdata=[c_last],
                hovertemplate=hover,
            ),
            row=row + 1,
            col=col + 1,
        )
        # layer="below"：参考线画在数据线下方，避免近水平收益曲线被参考线遮挡
        fig.add_hline(
            y=baseline,
            line=dict(color=colors["baseline"], width=1),
            layer="below",
            row=row + 1,
            col=col + 1,
        )

    # 账户数不足整行时隐藏尾部空面板
    for j in range(n, rows * _PANEL_COLS):
        row, col = divmod(j, _PANEL_COLS)
        fig.update_xaxes(visible=False, row=row + 1, col=col + 1)
        fig.update_yaxes(visible=False, row=row + 1, col=col + 1)

    fig.update_annotations(font=dict(size=9.5))
    fig.update_xaxes(tickformat="%m-%d", tickfont=dict(size=8))
    fig.update_yaxes(tickfont=dict(size=8), showgrid=False)
    if is_return:
        # 共享纵轴范围：全部面板统一，跨行比较不失真
        rets_wide = (wide_df / initial_wide - 1) * 100
        pad = (rets_wide.max().max() - rets_wide.min().min()) * 0.06 or 1.0
        fig.update_yaxes(
            range=[rets_wide.min().min() - pad, rets_wide.max().max() + pad]
        )
    else:
        # 总资产口径：各面板独立缩放，纵轴刻度没有可比性，隐藏以免误读
        fig.update_yaxes(showticklabels=False)
    fig.update_layout(
        height=max(240, rows * _PANEL_HEIGHT + 40),
        margin=dict(l=8, r=8, t=8, b=4),
        hovermode="closest",
    )
    return fig


def render_asset_history(
    wide_df: pd.DataFrame,
    initial_map: dict[str, float],
    stats_df: pd.DataFrame,
) -> None:
    """渲染全部账户 × 全交易日资产走势：指标卡、组合曲线、分面小图、明细表。"""
    colors = _viz_colors()

    finals = wide_df.iloc[-1]
    initial_wide = pd.Series(initial_map).reindex(wide_df.columns)
    rets = finals / initial_wide - 1
    total_final = float(finals.sum())
    total_initial = float(initial_wide.sum())
    best_id, worst_id = rets.idxmax(), rets.idxmin()

    # ── 指标卡 ──
    col1, col2, col3, col4, col5 = st.columns(5)
    with col1:
        st.metric(
            "交易账户",
            f"{len(wide_df.columns)}",
            delta=f"共 {len(initial_map)} 个注册账户",
            delta_color="off",
        )
    with col2:
        st.metric(
            "组合期末总资产",
            f"{total_final:,.0f}",
            delta=f"{total_final - total_initial:+,.0f}",
        )
    with col3:
        st.metric("平均累计收益率", f"{rets.mean() * 100:.2f}%")
    with col4:
        st.metric(
            "最佳账户",
            f"{rets[best_id] * 100:+.2f}%",
            delta=_short_account_name(best_id),
            delta_color="off",
        )
    with col5:
        st.metric(
            "最差账户",
            f"{rets[worst_id] * 100:+.2f}%",
            delta=_short_account_name(worst_id),
            delta_color="off",
        )

    # ── 走势图 ──
    metric = st.selectbox(
        "走势指标",
        [_ASSET_METRIC_ASSET, _ASSET_METRIC_RETURN],
        key="asset_history_metric",
        help="总资产口径各面板独立缩放；收益率口径全部面板共享纵轴，便于横向比较",
    )
    dates = pd.to_datetime(wide_df.index, format="%Y%m%d")
    portfolio = wide_df.sum(axis=1)

    st.markdown("#### 组合合计走势")
    st.plotly_chart(
        _portfolio_figure(dates, portfolio, total_initial, metric, colors),
        use_container_width=True,
    )

    st.markdown("#### 各账户走势（按期末收益排序）")
    st.plotly_chart(
        _panels_figure(wide_df, initial_map, metric, colors),
        use_container_width=True,
    )

    # ── 明细表（悬浮图的数值兜底通道）──
    st.markdown("#### 账户明细（按累计收益率排序）")
    traded_rows, idle_rows = [], []
    for account_id, stat in stats_df.iterrows():
        initial = float(initial_map.get(account_id, 100_000.0))
        if account_id in finals.index:
            final = float(finals[account_id])
            row = {
                "账户 ID": account_id,
                "成交笔数": int(stat["n_filled"]),
                "交易区间": (
                    f"{_fmt_date(stat['first_date'])} ~ {_fmt_date(stat['last_date'])}"
                    if stat["first_date"]
                    else "—"
                ),
                "期末总资产": final,
                "初始资金": initial,
                "累计收益率": (final / initial - 1) * 100,
            }
            traded_rows.append(row)
        else:
            idle_rows.append(
                {
                    "账户 ID": account_id,
                    "成交笔数": int(stat["n_filled"]),
                    "交易区间": "未交易",
                    "期末总资产": initial,
                    "初始资金": initial,
                    "累计收益率": 0.0,
                }
            )
    display = pd.DataFrame(
        sorted(traded_rows, key=lambda r: r["累计收益率"], reverse=True) + idle_rows
    )
    st.dataframe(
        display,
        use_container_width=True,
        hide_index=True,
        column_config={
            "期末总资产": st.column_config.NumberColumn(format="%,.0f"),
            "初始资金": st.column_config.NumberColumn(format="%,.0f"),
            "累计收益率": st.column_config.NumberColumn(format="%.2f%%"),
        },
    )
