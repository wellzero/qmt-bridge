"""模拟交易账户状态管理模块。

每个 ``account_id`` 对应一个独立的 ``AccountState``，
内部维护资金、持仓、委托、成交记录，并通过 ``threading.RLock`` 保证线程安全。
"""

from __future__ import annotations

import threading
from dataclasses import dataclass, field
from typing import TYPE_CHECKING

from .models import XtAsset, XtOrder, XtPosition, XtTrade

if TYPE_CHECKING:
    pass


# 委托状态码（与 xtconstant 对齐）
ORDER_UNREPORTED = 48
ORDER_WAIT_REPORTING = 49
ORDER_REPORTED = 50
ORDER_REPORTED_CANCEL = 51
ORDER_PARTSUCC_CANCEL = 52
ORDER_PART_CANCEL = 53
ORDER_CANCELED = 54
ORDER_PART_SUCC = 55
ORDER_SUCCEEDED = 56
ORDER_JUNK = 57
ORDER_UNKNOWN = 255


@dataclass
class PositionState:
    """单只股票持仓状态。"""

    account_id: str
    stock_code: str
    volume: int = 0  # 总持仓量
    can_use_volume: int = 0  # 可用数量
    frozen_volume: int = 0  # 冻结数量
    avg_price: float = 0.0  # 成本价
    last_price: float = 0.0  # 最新价

    @property
    def market_value(self) -> float:
        """持仓市值。"""
        return self.volume * self.last_price

    @property
    def profit_rate(self) -> float:
        """盈亏比例。"""
        if self.avg_price <= 0:
            return 0.0
        return (self.last_price - self.avg_price) / self.avg_price

    def to_xt_position(self) -> XtPosition:
        """转换为与 xtquant 对齐的持仓对象。"""
        return XtPosition(
            account_id=self.account_id,
            stock_code=self.stock_code,
            volume=self.volume,
            can_use_volume=self.can_use_volume,
            open_price=self.avg_price,
            market_value=self.market_value,
            frozen_volume=self.frozen_volume,
            on_road_volume=0,
            yesterday_volume=self.volume,
            avg_price=self.avg_price,
            direction=0,
            last_price=self.last_price,
            profit_rate=self.profit_rate,
        )


@dataclass
class OrderState:
    """单笔委托状态。"""

    account_id: str
    stock_code: str
    order_id: int
    order_type: int
    order_volume: int
    price_type: int
    price: float
    order_time: str
    strategy_name: str
    order_remark: str
    order_status: int = ORDER_WAIT_REPORTING
    status_msg: str = ""
    traded_volume: int = 0
    traded_price: float = 0.0  # 成交均价
    commission: float = 0.0  # 手续费
    stamp_tax: float = 0.0  # 印花税
    account_cash: float = 0.0  # 下单后账户可用资金
    account_market_value: float = 0.0  # 下单后账户持仓市值
    order_sysid: str = ""

    @property
    def remain_volume(self) -> int:
        """未成交数量。"""
        return self.order_volume - self.traded_volume

    def to_xt_order(self) -> XtOrder:
        """转换为与 xtquant 对齐的委托对象。"""
        return XtOrder(
            account_id=self.account_id,
            stock_code=self.stock_code,
            order_id=self.order_id,
            order_sysid=self.order_sysid,
            order_time=self.order_time,
            order_type=self.order_type,
            order_volume=self.order_volume,
            price_type=self.price_type,
            price=self.price,
            traded_volume=self.traded_volume,
            traded_price=self.traded_price,
            order_status=self.order_status,
            status_msg=self.status_msg,
            strategy_name=self.strategy_name,
            order_remark=self.order_remark,
            commission=self.commission,
            stamp_tax=self.stamp_tax,
            direction=0,
            offset_flag=0,
            secu_account="",
            instrument_name="",
        )


@dataclass
class TradeRecord:
    """单笔成交记录。"""

    account_id: str
    stock_code: str
    order_id: int
    order_type: int
    traded_id: int
    traded_time: str
    traded_price: float
    traded_volume: int
    traded_amount: float
    commission: float
    strategy_name: str = ""
    order_remark: str = ""
    stamp_tax: float = 0.0
    realized_pnl: float = 0.0

    def to_xt_trade(self) -> XtTrade:
        """转换为与 xtquant 对齐的成交对象。"""
        return XtTrade(
            account_id=self.account_id,
            stock_code=self.stock_code,
            order_type=self.order_type,
            traded_id=self.traded_id,
            traded_time=self.traded_time,
            traded_price=self.traded_price,
            traded_volume=self.traded_volume,
            traded_amount=self.traded_amount,
            order_id=self.order_id,
            order_sysid="",
            strategy_name=self.strategy_name,
            order_remark=self.order_remark,
            direction=0,
            offset_flag=0,
            commission=self.commission,
        )


@dataclass
class AccountState:
    """单个模拟账户的完整状态。"""

    account_id: str
    account_type: int
    cash: float = 0.0
    initial_cash: float = 0.0
    frozen_cash: float = 0.0
    positions: dict[str, PositionState] = field(default_factory=dict)
    orders: dict[int, OrderState] = field(default_factory=dict)
    trades: list[TradeRecord] = field(default_factory=list)
    _lock: threading.RLock = field(default_factory=threading.RLock)

    @property
    def market_value(self) -> float:
        """持仓总市值。"""
        with self._lock:
            return sum(p.market_value for p in self.positions.values())

    @property
    def total_asset(self) -> float:
        """总资产。"""
        return self.cash + self.frozen_cash + self.market_value

    @property
    def total_pnl(self) -> float:
        """总盈亏（总资产 - 初始资金）。"""
        return self.total_asset - self.initial_cash

    def get_position(self, stock_code: str) -> PositionState | None:
        """获取指定股票持仓。"""
        with self._lock:
            return self.positions.get(stock_code)

    def get_or_create_position(self, stock_code: str) -> PositionState:
        """获取或创建指定股票持仓。"""
        with self._lock:
            if stock_code not in self.positions:
                self.positions[stock_code] = PositionState(
                    account_id=self.account_id, stock_code=stock_code
                )
            return self.positions[stock_code]

    def to_xt_asset(self) -> XtAsset:
        """转换为与 xtquant 对齐的资金对象。"""
        return XtAsset(
            account_id=self.account_id,
            cash=self.cash,
            frozen_cash=self.frozen_cash,
            market_value=self.market_value,
            total_asset=self.total_asset,
            fetch_balance=self.cash,
        )

    def reset(self, initial_cash: float) -> None:
        """重置账户状态。"""
        with self._lock:
            self.cash = initial_cash
            self.initial_cash = initial_cash
            self.frozen_cash = 0.0
            self.positions.clear()
            self.orders.clear()
            self.trades.clear()
