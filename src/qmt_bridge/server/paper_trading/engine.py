"""模拟交易撮合引擎与价格源模块。

撮合引擎负责根据行情价格决定委托是否成交、成交价、手续费，
并更新账户资金与持仓。价格源支持真实行情（xtdata）、静态价格和自动回退三种模式。
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import TYPE_CHECKING, Protocol

from .account import (
    ORDER_JUNK,
    ORDER_SUCCEEDED,
    AccountState,
    OrderState,
    TradeRecord,
)

if TYPE_CHECKING:
    from .config import PaperAccountConfig

logger = logging.getLogger("qmt_bridge.paper_trading")


# 委托类型常量（与 xtconstant 对齐）
STOCK_BUY = 23
STOCK_SELL = 24

# 报价类型常量
LATEST_PRICE = 5
FIX_PRICE = 11


def _extract_price_from_tick(tick_data: dict) -> float | None:
    """从 ``get_full_tick`` 返回的单只股票 tick 字典中提取有效价格。"""
    for key in ("lastPrice", "close", "open", "lastprice"):
        price = tick_data.get(key)
        if isinstance(price, (int, float)) and price > 0:
            return float(price)
    return None


class PriceSource(Protocol):
    """价格源协议。"""

    def get_price(self, stock_code: str) -> float | None:
        """获取指定股票的最新价。"""
        ...


class XtdataPriceSource:
    """基于 ``xtquant.xtdata.get_full_tick`` 的真实行情价格源。"""

    def __init__(self):
        self._xtdata = None
        try:
            from xtquant import xtdata

            self._xtdata = xtdata
        except Exception:
            logger.warning(
                "xtquant.xtdata 未安装或不可用，XtdataPriceSource 将始终返回 None"
            )

    def get_price(self, stock_code: str) -> float | None:
        if self._xtdata is None:
            return None
        try:
            tick = self._xtdata.get_full_tick([stock_code])
            if isinstance(tick, dict):
                data = tick.get(stock_code)
                if isinstance(data, dict):
                    return _extract_price_from_tick(data)
        except Exception:
            logger.exception("XtdataPriceSource 获取 %s 价格失败", stock_code)
        return None


class StaticPriceSource:
    """静态价格源，用于测试或无行情环境。

    支持从 ``xtquant.xtdata.get_full_tick`` 下载行情数据并缓存为静态价格，
    便于在模拟交易中快速初始化或更新多只股票的参考价。
    """

    def __init__(self, prices: dict[str, float] | None = None):
        self.prices = dict(prices or {})

    def get_price(self, stock_code: str) -> float | None:
        return self.prices.get(stock_code)

    def set_price(self, stock_code: str, price: float) -> None:
        """设置或更新某只股票的静态价格。"""
        self.prices[stock_code] = price

    def download_prices(self, stock_codes: list[str]) -> dict[str, float]:
        """从 ``xtquant.xtdata.get_full_tick`` 下载指定股票最新价。

        下载成功的价格会合并到 ``self.prices`` 中；下载失败的股票会被跳过并记录日志。

        Args:
            stock_codes: 待下载的股票代码列表，例如 ``["000001.SZ", "600519.SH"]``。

        Returns:
            本次成功下载的价格字典 ``{stock_code: price}``。
        """
        downloaded: dict[str, float] = {}
        if not stock_codes:
            return downloaded

        try:
            from xtquant import xtdata
        except Exception:
            logger.warning("xtquant.xtdata 未安装或不可用，无法下载静态价格")
            return downloaded

        try:
            ticks = xtdata.get_full_tick(stock_codes)
            if not isinstance(ticks, dict):
                logger.warning("get_full_tick 返回格式异常: %s", type(ticks))
                return downloaded
        except Exception:
            logger.exception("StaticPriceSource 下载行情数据失败")
            return downloaded

        for stock_code in stock_codes:
            data = ticks.get(stock_code)
            if not isinstance(data, dict):
                logger.warning("未能获取 %s 的 tick 数据", stock_code)
                continue
            price = _extract_price_from_tick(data)
            if price is None:
                logger.warning("未能从 %s 的 tick 数据中提取有效价格", stock_code)
                continue
            self.prices[stock_code] = price
            downloaded[stock_code] = price

        return downloaded


class FallbackPriceSource:
    """回退价格源：优先 xtdata，失败时使用静态价格。

    对于限价单，若外部价格源均不可用，可进一步以委托价作为成交价。
    """

    def __init__(
        self,
        xtdata_source: PriceSource | None = None,
        static_source: StaticPriceSource | None = None,
    ):
        self.xtdata_source = xtdata_source or XtdataPriceSource()
        self.static_source = static_source or StaticPriceSource()

    def get_price(self, stock_code: str) -> float | None:
        price = self.xtdata_source.get_price(stock_code)
        if price is None:
            price = self.static_source.get_price(stock_code)
        return price


@dataclass
class FillResult:
    """单次撮合结果。"""

    traded_volume: int
    traded_price: float
    traded_amount: float
    commission: float
    stamp_tax: float


def _now_str() -> str:
    """当前时间字符串（HH:MM:SS 格式）。"""
    from datetime import datetime

    return datetime.now().strftime("%H:%M:%S")


class MatchingEngine:
    """模拟撮合引擎。

    根据价格源和账户配置，对单笔委托执行即时全量成交。
    买入校验可用资金，卖出校验可用持仓；成交后更新账户资金、持仓和委托状态。
    """

    def __init__(self, price_source: PriceSource | None = None):
        self.price_source = price_source or FallbackPriceSource()

    def resolve_fill_price(
        self,
        order: OrderState,
        config: PaperAccountConfig,
    ) -> float | None:
        """确定成交价。

        - 限价单：以委托价成交（若委托价 > 0）
        - 市价/最新价：从价格源获取，并叠加滑点
        """
        if order.price_type == FIX_PRICE and order.price > 0:
            base_price = order.price
            logger.debug(
                "%s 限价单使用委托价 %.3f 作为基准价", order.stock_code, base_price
            )
        else:
            base_price = self.price_source.get_price(order.stock_code)
            logger.debug("%s 从价格源获取基准价: %s", order.stock_code, base_price)
            if base_price is None:
                return None

        if order.order_type == STOCK_BUY:
            fill_price = round(base_price * (1 + config.slippage), 4)
        else:
            fill_price = round(base_price * (1 - config.slippage), 4)
        if config.slippage:
            logger.debug(
                "%s 应用滑点 %.4f 后成交价 %.3f",
                order.stock_code,
                config.slippage,
                fill_price,
            )
        return fill_price

    def match(
        self,
        order: OrderState,
        account: AccountState,
        config: PaperAccountConfig,
    ) -> TradeRecord | None:
        """对委托执行一次撮合。

        当前版本默认整单成交；若价格源不可用或资金/持仓不足，
        则将委托标记为废单并返回 None。

        Args:
            order: 待撮合委托。
            account: 委托所属账户状态。
            config: 账户级配置（手续费、滑点等）。

        Returns:
            若成交则返回 ``TradeRecord``，否则返回 None。
        """
        fill_price = self.resolve_fill_price(order, config)
        if fill_price is None or fill_price <= 0:
            order.order_status = ORDER_JUNK
            order.status_msg = "无法获取有效成交价格"
            logger.warning(
                "撮合失败 %s: 无法获取有效成交价格 (price_type=%s, price=%s)",
                order.stock_code,
                order.price_type,
                order.price,
            )
            return None

        volume = order.order_volume
        traded_amount = round(fill_price * volume, 4)
        commission = round(traded_amount * config.commission_rate, 4)
        stamp_tax = round(
            traded_amount * config.stamp_tax_rate
            if order.order_type == STOCK_SELL
            else 0,
            4,
        )

        with account._lock:
            if order.order_type == STOCK_BUY:
                total_cost = traded_amount + commission + stamp_tax
                if account.cash < total_cost:
                    order.order_status = ORDER_JUNK
                    order.status_msg = "可用资金不足"
                    logger.warning(
                        "撮合失败 %s: 资金不足 need=%.2f cash=%.2f",
                        order.stock_code,
                        total_cost,
                        account.cash,
                    )
                    return None
                account.cash -= total_cost
                logger.debug(
                    "买入扣减资金 %s: cost=%.2f commission=%.2f cash_left=%.2f",
                    order.stock_code,
                    traded_amount,
                    commission,
                    account.cash,
                )
            elif order.order_type == STOCK_SELL:
                position = account.get_position(order.stock_code)
                if position is None or position.can_use_volume < volume:
                    order.order_status = ORDER_JUNK
                    order.status_msg = "可用持仓不足"
                    logger.warning(
                        "撮合失败 %s: 持仓不足 need=%d have=%s",
                        order.stock_code,
                        volume,
                        position.can_use_volume if position else 0,
                    )
                    return None
                old_avg_price = position.avg_price
                position.can_use_volume -= volume
                position.volume -= volume
                if position.volume == 0:
                    account.positions.pop(order.stock_code, None)
                account.cash += traded_amount - commission - stamp_tax
                realized_pnl = (
                    traded_amount - volume * old_avg_price - commission - stamp_tax
                )
                logger.debug(
                    "卖出增加资金 %s: amount=%.2f commission=%.2f tax=%.2f pnl=%.2f cash=%.2f",
                    order.stock_code,
                    traded_amount,
                    commission,
                    stamp_tax,
                    realized_pnl,
                    account.cash,
                )
            else:
                order.order_status = ORDER_JUNK
                order.status_msg = "不支持的委托类型"
                logger.warning(
                    "撮合失败 %s: 不支持的委托类型 %s",
                    order.stock_code,
                    order.order_type,
                )
                return None

            # 更新买入后的持仓
            if order.order_type == STOCK_BUY:
                position = account.get_or_create_position(order.stock_code)
                total_cost_basis = position.avg_price * position.volume + traded_amount
                position.volume += volume
                position.can_use_volume += volume
                position.avg_price = (
                    round(total_cost_basis / position.volume, 4)
                    if position.volume > 0
                    else 0.0
                )
                position.last_price = fill_price

            # 更新委托状态为已成
            order.traded_volume = volume
            order.traded_price = fill_price
            order.order_status = ORDER_SUCCEEDED
            order.status_msg = "已成"

            trade = TradeRecord(
                account_id=account.account_id,
                stock_code=order.stock_code,
                order_id=order.order_id,
                order_type=order.order_type,
                traded_id=0,  # 由 PaperQuantTrader 统一生成
                traded_time=_now_str(),
                traded_price=fill_price,
                traded_volume=volume,
                traded_amount=traded_amount,
                commission=commission,
                stamp_tax=stamp_tax,
                realized_pnl=realized_pnl if order.order_type == STOCK_SELL else 0.0,
                strategy_name=order.strategy_name,
                order_remark=order.order_remark,
            )
            account.trades.append(trade)
            return trade

    def update_position_prices(self, account: AccountState) -> None:
        """刷新账户内所有持仓的最新价与市值。"""
        with account._lock:
            for position in account.positions.values():
                price = self.price_source.get_price(position.stock_code)
                if price is not None and price > 0:
                    position.last_price = round(price, 4)
                    logger.debug(
                        "刷新持仓价格 %s: last_price=%.3f market_value=%.2f",
                        position.stock_code,
                        position.last_price,
                        position.market_value,
                    )
