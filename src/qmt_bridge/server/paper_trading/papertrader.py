"""模拟交易核心模块。

提供 ``PaperAccount`` 与 ``PaperQuantTrader`` 两个类，
公开 API 与 ``xtquant.xttrader.XtQuantTrader`` / ``xtquant.xttype.StockAccount`` 完全一致，
可作为真实 QMT 交易的无侵入替代，支持多账户并行模拟、CSV 委托记录与业绩汇总。
"""

from __future__ import annotations

import itertools
import logging
import threading
from datetime import datetime
from typing import Any

from .account import (
    ORDER_CANCELED,
    ORDER_PARTSUCC_CANCEL,
    ORDER_REPORTED,
    ORDER_REPORTED_CANCEL,
    AccountState,
    OrderState,
)
from .callback import PaperTraderCallback
from .config import PaperAccountConfig, PaperAccountConfigManager
from .engine import (
    FallbackPriceSource,
    MatchingEngine,
    StaticPriceSource,
    XtdataPriceSource,
)
from .models import (
    XtAccountStatus,
    XtAsset,
    XtCancelOrderResponse,
    XtOrder,
    XtOrderResponse,
    XtPosition,
    XtTrade,
)
from .storage import AccountSummary

logger = logging.getLogger("qmt_bridge.paper_trading")


# 委托类型常量
STOCK_BUY = 23
STOCK_SELL = 24


class PaperAccount:
    """模拟证券账号。

    对外属性与 ``xtquant.xttype.StockAccount`` 保持一致。
    """

    def __init__(self, account_id: str, account_type: int = 2):
        self.account_id = account_id
        self.account_type = account_type


class PaperQuantTrader:
    """模拟交易核心类。

    与 ``xtquant.xttrader.XtQuantTrader`` 公开 API 对齐，
    内部维护多个 ``AccountState``，按 ``account_id`` 隔离资金、持仓、委托与成交。
    """

    def __init__(self, path: str, session_id: int, callback: Any | None = None):
        self.path = path
        self.session_id = session_id
        self._callback = callback if callback is not None else PaperTraderCallback()
        self._connected = False
        self._started = False
        self._subscribed: set[str] = set()
        self._accounts: dict[str, AccountState] = {}
        self._config_manager = PaperAccountConfigManager()
        self._storage = self._config_manager.storage
        self._engine = MatchingEngine()
        self._seq = itertools.count(1)
        self._lock = threading.RLock()

        # 加载已配置的账户
        self._load_accounts()

    # ------------------------------------------------------------------
    # 内部辅助
    # ------------------------------------------------------------------

    def _load_accounts(self) -> None:
        """从配置管理器加载所有账户状态。"""
        loaded = 0
        for account_id in self._config_manager.list_accounts():
            config = self._config_manager.get_config(account_id)
            if config is not None and config.enabled:
                self._accounts[account_id] = self._config_manager.create_account_state(
                    config
                )
                loaded += 1
        if loaded:
            logger.info("已从配置加载 %d 个模拟账户", loaded)

    def _sync_accounts_from_storage(self) -> None:
        """扫描数据目录，为有业绩文件但配置缺失的账户自动重建配置。

        这种场景通常发生在 ``config.json`` 被覆盖/还原，而账户目录仍保留时。
        """
        if self._storage is None:
            return
        root = self._storage.paper_trading_dir
        if not root.exists():
            return

        synced = 0
        existing = set(self._config_manager.list_accounts())
        for path in root.iterdir():
            if not path.is_dir():
                continue
            account_id = path.name
            summary_path = path / "summary" / "summary.json"
            if not summary_path.exists():
                continue
            if account_id in existing:
                continue
            try:
                summary = self._storage.read_summary(account_id)
                config = PaperAccountConfig(
                    account_id=account_id,
                    initial_cash=summary.initial_cash,
                )
                self._config_manager.set_config(config)
                self._accounts[account_id] = self._config_manager.create_account_state(
                    config
                )
                self._update_summary(account_id)
                synced += 1
                logger.info("从磁盘自动恢复模拟账户配置: %s", account_id)
            except Exception:
                logger.exception("自动恢复模拟账户 %s 配置失败", account_id)
        if synced:
            logger.info("已从磁盘自动恢复 %d 个模拟账户配置", synced)

    def _resolve_account_id(self, account: PaperAccount | Any) -> str:
        """从账户对象中提取 account_id。"""
        return getattr(account, "account_id", "")

    def _get_account(self, account_id: str) -> AccountState:
        """获取账户状态，不存在则自动创建默认账户。"""
        with self._lock:
            if account_id not in self._accounts:
                logger.info("自动创建模拟账户状态: %s", account_id)
                config = self._config_manager.get_config(account_id)
                if config is None:
                    config = PaperAccountConfig(account_id=account_id)
                    self._config_manager.set_config(config)
                self._accounts[account_id] = self._config_manager.create_account_state(
                    config
                )
            return self._accounts[account_id]

    def _get_config(self, account_id: str) -> PaperAccountConfig:
        """获取账户配置，不存在则创建默认配置。"""
        config = self._config_manager.get_config(account_id)
        if config is None:
            logger.info("自动创建默认模拟账户配置: %s", account_id)
            config = PaperAccountConfig(account_id=account_id)
            self._config_manager.set_config(config)
        return config

    def _next_seq(self) -> int:
        return next(self._seq)

    def _now(self) -> str:
        return datetime.now().strftime("%H:%M:%S")

    def _set_price_source(self, config: PaperAccountConfig) -> None:
        """根据账户配置设置撮合引擎的价格源。"""
        if config.price_source == "xtdata":
            self._engine.price_source = XtdataPriceSource()
        elif config.price_source == "static":
            self._engine.price_source = StaticPriceSource(config.static_prices)
        else:  # fallback
            self._engine.price_source = FallbackPriceSource(
                static_source=StaticPriceSource(config.static_prices)
            )
        logger.debug(
            "账户 %s 使用价格源: %s, 静态价格数: %d",
            config.account_id,
            config.price_source,
            len(config.static_prices),
        )

    def _try_auto_download_price(
        self,
        account_id: str,
        stock_code: str,
        config: PaperAccountConfig,
    ) -> bool:
        """当配置允许且价格缺失时，尝试从 xtquant 下载单只股票静态价格。

        下载成功后会更新账户配置并持久化。

        Returns:
            是否成功下载到有效价格。
        """
        if not config.auto_download_prices:
            return False
        if config.price_source not in ("static", "fallback"):
            return False
        try:
            source = StaticPriceSource(config.static_prices)
            downloaded = source.download_prices([stock_code])
            price = downloaded.get(stock_code)
            if price is None:
                return False
            config.static_prices[stock_code] = price
            self._config_manager.set_config(config)
            logger.info(
                "账户 %s 自动下载 %s 静态价格成功: %.3f",
                account_id,
                stock_code,
                price,
            )
            return True
        except Exception:
            logger.exception("账户 %s 自动下载 %s 静态价格失败", account_id, stock_code)
            return False

    def _persist_orders(self, account_id: str) -> None:
        """将当前账户委托列表持久化到 CSV。"""
        state = self._accounts.get(account_id)
        if state is None:
            return
        rows = []
        with state._lock:
            for order in state.orders.values():
                xt = order.to_xt_order()
                rows.append(
                    {
                        "order_time": xt.order_time,
                        "order_id": xt.order_id,
                        "stock_code": xt.stock_code,
                        "order_type": xt.order_type,
                        "order_volume": xt.order_volume,
                        "price_type": xt.price_type,
                        "price": xt.price,
                        "traded_volume": xt.traded_volume,
                        "traded_price": xt.traded_price,
                        "commission": xt.commission,
                        "stamp_tax": xt.stamp_tax,
                        "order_status": xt.order_status,
                        "status_msg": xt.status_msg,
                        "strategy_name": xt.strategy_name,
                        "order_remark": xt.order_remark,
                    }
                )
        self._storage.write_orders(account_id, rows)

    def _update_summary(self, account_id: str) -> None:
        """更新并保存账户业绩摘要。"""
        state = self._accounts.get(account_id)
        if state is None:
            return
        self._engine.update_position_prices(state)
        summary = AccountSummary(account_id=account_id)
        with state._lock:
            summary.initial_cash = state.initial_cash
            summary.cash = state.cash
            summary.market_value = state.market_value
            summary.total_asset = state.total_asset
            summary.total_pnl = state.total_pnl
            if state.initial_cash != 0:
                summary.total_return_rate = round(
                    state.total_pnl / state.initial_cash, 6
                )
            summary.total_trades = len(state.trades)
            summary.total_commission = round(sum(t.commission for t in state.trades), 4)
            summary.total_stamp_tax = round(sum(t.stamp_tax for t in state.trades), 4)
            summary.realized_pnl = round(sum(t.realized_pnl for t in state.trades), 4)
            unrealized = 0.0
            for position in state.positions.values():
                unrealized += position.volume * (
                    position.last_price - position.avg_price
                )
            summary.unrealized_pnl = round(unrealized, 4)
        self._storage.write_summary(summary)
        logger.debug(
            "账户 %s 业绩摘要已更新: total_asset=%.2f, total_pnl=%.2f, trades=%d",
            account_id,
            summary.total_asset,
            summary.total_pnl,
            summary.total_trades,
        )

    def _dispatch_order_callback(self, order: OrderState) -> None:
        try:
            self._callback.on_stock_order(order.to_xt_order())
        except Exception:
            logger.exception("on_stock_order 回调异常")

    def _dispatch_trade_callback(self, trade: XtTrade) -> None:
        try:
            self._callback.on_stock_trade(trade)
        except Exception:
            logger.exception("on_stock_trade 回调异常")

    def _dispatch_asset_callback(self, account_id: str) -> None:
        try:
            state = self._accounts.get(account_id)
            if state is not None:
                self._callback.on_stock_asset(state.to_xt_asset())
        except Exception:
            logger.exception("on_stock_asset 回调异常")

    # ------------------------------------------------------------------
    # 生命周期与回调
    # ------------------------------------------------------------------

    def start(self) -> None:
        """启动模拟交易器。"""
        self._started = True
        logger.info("PaperQuantTrader started, session_id=%s", self.session_id)

    def stop(self) -> None:
        """停止模拟交易器。"""
        self._started = False
        self._connected = False
        logger.info("PaperQuantTrader stopped")

    def connect(self) -> int:
        """连接模拟交易器，始终返回 0 表示成功。"""
        self._connected = True
        try:
            self._callback.on_connected()
        except Exception:
            logger.exception("on_connected 回调异常")
        return 0

    def sleep(self, time: float) -> None:
        """模拟睡眠，实际立即返回。"""
        pass

    def run_forever(self) -> None:
        """模拟永久运行，实际立即返回。"""
        pass

    def set_timeout(self, timeout: int = 0) -> None:
        """设置超时，模拟交易忽略。"""
        pass

    def set_relaxed_response_order_enabled(self, enabled: bool) -> None:
        """设置响应顺序模式，模拟交易忽略。"""
        pass

    def register_callback(self, callback: Any) -> None:
        """注册回调对象。"""
        self._callback = callback if callback is not None else PaperTraderCallback()

    # ------------------------------------------------------------------
    # 订阅
    # ------------------------------------------------------------------

    def subscribe(self, account: PaperAccount | Any) -> int:
        """订阅账户。"""
        account_id = self._resolve_account_id(account)
        if account_id:
            self._subscribed.add(account_id)
            logger.info("Paper account subscribed: %s", account_id)
        return 0

    def unsubscribe(self, account: PaperAccount | Any) -> int:
        """取消订阅账户。"""
        account_id = self._resolve_account_id(account)
        self._subscribed.discard(account_id)
        return 0

    # ------------------------------------------------------------------
    # 委托操作
    # ------------------------------------------------------------------

    def order_stock(
        self,
        account: PaperAccount | Any,
        stock_code: str,
        order_type: int,
        order_volume: int,
        price_type: int,
        price: float,
        strategy_name: str = "",
        order_remark: str = "",
    ) -> int:
        """同步下单，返回委托编号。"""
        account_id = self._resolve_account_id(account)
        state = self._get_account(account_id)
        config = self._get_config(account_id)

        order_id = self._next_seq()
        order = OrderState(
            account_id=account_id,
            stock_code=stock_code,
            order_id=order_id,
            order_type=order_type,
            order_volume=order_volume,
            price_type=price_type,
            price=price,
            order_time=self._now(),
            strategy_name=strategy_name,
            order_remark=order_remark,
            order_status=ORDER_REPORTED,
            status_msg="已报",
            order_sysid=str(order_id),
        )

        side = (
            "买入"
            if order_type == STOCK_BUY
            else ("卖出" if order_type == STOCK_SELL else f"类型{order_type}")
        )
        logger.info(
            "[%s] 下单 account=%s %s %s volume=%d price_type=%s price=%.3f strategy=%s remark=%s",
            order.order_time,
            account_id,
            side,
            stock_code,
            order_volume,
            price_type,
            price,
            strategy_name,
            order_remark,
        )

        with state._lock:
            state.orders[order_id] = order

        self._persist_orders(account_id)
        self._dispatch_order_callback(order)

        self._set_price_source(config)
        if config.auto_download_prices and config.price_source in (
            "static",
            "fallback",
        ):
            current_price = self._engine.price_source.get_price(stock_code)
            if current_price is None:
                if self._try_auto_download_price(account_id, stock_code, config):
                    self._set_price_source(config)

        trade = self._engine.match(order, state, config)
        if trade is not None:
            trade.traded_id = self._next_seq()
            logger.info(
                "[%s] 成交 account=%s %s %s volume=%d price=%.3f amount=%.2f commission=%.2f status=%s",
                trade.traded_time,
                account_id,
                side,
                stock_code,
                trade.traded_volume,
                trade.traded_price,
                trade.traded_amount,
                trade.commission,
                order.status_msg,
            )
            self._dispatch_trade_callback(trade.to_xt_trade())
            self._update_summary(account_id)
        else:
            logger.warning(
                "[%s] 委托未成/废单 account=%s order_id=%d %s %s reason=%s",
                self._now(),
                account_id,
                order_id,
                side,
                stock_code,
                order.status_msg,
            )

        self._persist_orders(account_id)
        self._dispatch_order_callback(order)
        self._dispatch_asset_callback(account_id)

        return order_id

    def order_stock_async(
        self,
        account: PaperAccount | Any,
        stock_code: str,
        order_type: int,
        order_volume: int,
        price_type: int,
        price: float,
        strategy_name: str = "",
        order_remark: str = "",
    ) -> int:
        """异步下单，返回请求序号。"""
        seq = self._next_seq()
        order_id = self.order_stock(
            account,
            stock_code,
            order_type,
            order_volume,
            price_type,
            price,
            strategy_name,
            order_remark,
        )
        try:
            response = XtOrderResponse(
                account_id=self._resolve_account_id(account),
                order_id=order_id,
                strategy_name=strategy_name,
                order_remark=order_remark,
                error_msg="",
                seq=seq,
            )
            self._callback.on_order_stock_async_response(response)
        except Exception:
            logger.exception("on_order_stock_async_response 回调异常")
        return seq

    def cancel_order_stock(self, account: PaperAccount | Any, order_id: int) -> int:
        """同步撤单，返回 0 表示成功，-1 表示失败。"""
        account_id = self._resolve_account_id(account)
        state = self._accounts.get(account_id)
        if state is None:
            logger.warning("撤单失败: 账户 %s 不存在", account_id)
            return -1
        with state._lock:
            order = state.orders.get(order_id)
            if order is None:
                logger.warning("撤单失败: 账户 %s 委托 %d 不存在", account_id, order_id)
                return -1
            if order.order_status not in (
                ORDER_REPORTED,
                ORDER_PARTSUCC_CANCEL,
                ORDER_REPORTED_CANCEL,
            ):
                logger.warning(
                    "撤单失败: 账户 %s 委托 %d 状态为 %d (%s)，不可撤",
                    account_id,
                    order_id,
                    order.order_status,
                    order.status_msg,
                )
                return -1
            order.order_status = ORDER_CANCELED
            order.status_msg = "已撤"
            # 解冻买入冻结资金或卖出冻结持仓
            if order.order_type == STOCK_BUY:
                # 简化：买入同步成交为整单，撤单时无未成交部分需要解冻
                pass
            elif order.order_type == STOCK_SELL:
                position = state.positions.get(order.stock_code)
                if position is not None:
                    position.frozen_volume -= order.remain_volume
                    position.can_use_volume += order.remain_volume

        logger.info(
            "撤单成功: 账户 %s 委托 %d %s", account_id, order_id, order.stock_code
        )
        self._persist_orders(account_id)
        self._dispatch_order_callback(order)
        self._dispatch_asset_callback(account_id)
        return 0

    def cancel_order_stock_async(
        self, account: PaperAccount | Any, order_id: int
    ) -> int:
        """异步撤单，返回请求序号。"""
        seq = self._next_seq()
        result = self.cancel_order_stock(account, order_id)
        try:
            response = XtCancelOrderResponse(
                account_id=self._resolve_account_id(account),
                cancel_result=result,
                order_id=order_id,
                order_sysid=str(order_id),
                seq=seq,
                error_msg="" if result == 0 else "撤单失败",
            )
            self._callback.on_cancel_order_stock_async_response(response)
        except Exception:
            logger.exception("on_cancel_order_stock_async_response 回调异常")
        return seq

    def cancel_order_stock_sysid(
        self, account: PaperAccount | Any, market: Any, sysid: str
    ) -> int:
        """按系统编号同步撤单。"""
        account_id = self._resolve_account_id(account)
        state = self._accounts.get(account_id)
        if state is None:
            return -1
        with state._lock:
            for order in state.orders.values():
                if order.order_sysid == sysid:
                    return self.cancel_order_stock(account, order.order_id)
        return -1

    def cancel_order_stock_sysid_async(
        self, account: PaperAccount | Any, market: Any, sysid: str
    ) -> int:
        """按系统编号异步撤单。"""
        seq = self._next_seq()
        self.cancel_order_stock_sysid(account, market, sysid)
        return seq

    # ------------------------------------------------------------------
    # 查询操作
    # ------------------------------------------------------------------

    def query_stock_asset(self, account: PaperAccount | Any) -> XtAsset | None:
        account_id = self._resolve_account_id(account)
        state = self._accounts.get(account_id)
        if state is None:
            return None
        self._engine.update_position_prices(state)
        return state.to_xt_asset()

    def query_stock_order(
        self, account: PaperAccount | Any, order_id: int
    ) -> XtOrder | None:
        account_id = self._resolve_account_id(account)
        state = self._accounts.get(account_id)
        if state is None:
            return None
        with state._lock:
            order = state.orders.get(order_id)
            return order.to_xt_order() if order else None

    def query_stock_orders(
        self, account: PaperAccount | Any, cancelable_only: bool = False
    ) -> list[XtOrder]:
        account_id = self._resolve_account_id(account)
        state = self._accounts.get(account_id)
        if state is None:
            return []
        with state._lock:
            orders = list(state.orders.values())
            if cancelable_only:
                orders = [
                    o
                    for o in orders
                    if o.order_status
                    in (ORDER_REPORTED, ORDER_PARTSUCC_CANCEL, ORDER_REPORTED_CANCEL)
                ]
            return [o.to_xt_order() for o in orders]

    def query_stock_trades(self, account: PaperAccount | Any) -> list[XtTrade]:
        account_id = self._resolve_account_id(account)
        state = self._accounts.get(account_id)
        if state is None:
            return []
        with state._lock:
            return [t.to_xt_trade() for t in state.trades]

    def query_stock_position(
        self, account: PaperAccount | Any, stock_code: str
    ) -> XtPosition | None:
        account_id = self._resolve_account_id(account)
        state = self._accounts.get(account_id)
        if state is None:
            return None
        position = state.get_position(stock_code)
        if position is None:
            return None
        self._engine.update_position_prices(state)
        return position.to_xt_position()

    def query_stock_positions(self, account: PaperAccount | Any) -> list[XtPosition]:
        account_id = self._resolve_account_id(account)
        state = self._accounts.get(account_id)
        if state is None:
            return []
        self._engine.update_position_prices(state)
        with state._lock:
            return [p.to_xt_position() for p in state.positions.values()]

    # ------------------------------------------------------------------
    # 账户信息
    # ------------------------------------------------------------------

    def query_account_infos(self) -> list[PaperAccount]:
        """查询所有已注册账户信息。"""
        with self._lock:
            return [
                PaperAccount(a.account_id, a.account_type)
                for a in self._accounts.values()
            ]

    def query_account_status(self) -> list[XtAccountStatus]:
        """查询所有账户状态。"""
        with self._lock:
            return [
                XtAccountStatus(a.account_id, a.account_type, 0)
                for a in self._accounts.values()
            ]

    # ------------------------------------------------------------------
    # 异步查询占位
    # ------------------------------------------------------------------

    def query_stock_asset_async(
        self, account: PaperAccount | Any, callback: Any
    ) -> None:
        result = self.query_stock_asset(account)
        if callback is not None:
            try:
                callback(result)
            except Exception:
                logger.exception("异步查询资产回调异常")

    def query_stock_orders_async(
        self, account: PaperAccount | Any, callback: Any, cancelable_only: bool = False
    ) -> int:
        seq = self._next_seq()
        result = self.query_stock_orders(account, cancelable_only)
        if callback is not None:
            try:
                callback(result)
            except Exception:
                logger.exception("异步查询委托回调异常")
        return seq

    def query_stock_trades_async(
        self, account: PaperAccount | Any, callback: Any
    ) -> int:
        seq = self._next_seq()
        result = self.query_stock_trades(account)
        if callback is not None:
            try:
                callback(result)
            except Exception:
                logger.exception("异步查询成交回调异常")
        return seq

    def query_stock_positions_async(
        self, account: PaperAccount | Any, callback: Any
    ) -> int:
        seq = self._next_seq()
        result = self.query_stock_positions(account)
        if callback is not None:
            try:
                callback(result)
            except Exception:
                logger.exception("异步查询持仓回调异常")
        return seq

    def query_account_infos_async(self, callback: Any) -> int:
        seq = self._next_seq()
        if callback is not None:
            try:
                callback(self.query_account_infos())
            except Exception:
                logger.exception("异步查询账户信息回调异常")
        return seq

    def query_account_status_async(self, callback: Any) -> int:
        seq = self._next_seq()
        if callback is not None:
            try:
                callback(self.query_account_status())
            except Exception:
                logger.exception("异步查询账户状态回调异常")
        return seq

    # ------------------------------------------------------------------
    # 信用交易（占位）
    # ------------------------------------------------------------------

    def query_credit_detail(self, account: PaperAccount | Any) -> list[Any]:
        return []

    def query_credit_detail_async(
        self, account: PaperAccount | Any, callback: Any
    ) -> int:
        return self._next_seq()

    def query_stk_compacts(self, account: PaperAccount | Any) -> list[Any]:
        return []

    def query_stk_compacts_async(
        self, account: PaperAccount | Any, callback: Any
    ) -> int:
        return self._next_seq()

    def query_credit_subjects(self, account: PaperAccount | Any) -> list[Any]:
        return []

    def query_credit_subjects_async(
        self, account: PaperAccount | Any, callback: Any
    ) -> int:
        return self._next_seq()

    def query_credit_slo_code(self, account: PaperAccount | Any) -> list[Any]:
        return []

    def query_credit_slo_code_async(
        self, account: PaperAccount | Any, callback: Any
    ) -> int:
        return self._next_seq()

    def query_credit_assure(self, account: PaperAccount | Any) -> list[Any]:
        return []

    def query_credit_assure_async(
        self, account: PaperAccount | Any, callback: Any
    ) -> int:
        return self._next_seq()

    # ------------------------------------------------------------------
    # 新股申购（占位）
    # ------------------------------------------------------------------

    def query_new_purchase_limit(self, account: PaperAccount | Any) -> dict[str, int]:
        return {}

    def query_new_purchase_limit_async(
        self, account: PaperAccount | Any, callback: Any
    ) -> int:
        return self._next_seq()

    def query_ipo_data(self) -> dict[str, dict[str, Any]]:
        return {}

    def query_ipo_data_async(self, callback: Any) -> int:
        return self._next_seq()

    # ------------------------------------------------------------------
    # 资金/证券划转（占位）
    # ------------------------------------------------------------------

    def fund_transfer(
        self, account: PaperAccount | Any, transfer_direction: int, price: float
    ) -> tuple[bool, str]:
        return True, ""

    def secu_transfer(
        self,
        account: PaperAccount | Any,
        transfer_direction: int,
        stock_code: str,
        volume: int,
        transfer_type: int,
    ) -> tuple[bool, str]:
        return True, ""

    def query_com_fund(self, account: PaperAccount | Any) -> dict[str, Any]:
        return {}

    def query_com_position(self, account: PaperAccount | Any) -> list[dict[str, Any]]:
        return []

    # ------------------------------------------------------------------
    # 银证转账（占位）
    # ------------------------------------------------------------------

    def bank_transfer_in(
        self,
        account: PaperAccount | Any,
        bank_no: str,
        bank_account: str,
        balance: float,
        bank_pwd: str = "",
        fund_pwd: str = "",
    ) -> tuple[bool, str]:
        return True, ""

    def bank_transfer_out(
        self,
        account: PaperAccount | Any,
        bank_no: str,
        bank_account: str,
        balance: float,
        bank_pwd: str = "",
        fund_pwd: str = "",
    ) -> tuple[bool, str]:
        return True, ""

    def bank_transfer_in_async(
        self,
        account: PaperAccount | Any,
        bank_no: str,
        bank_account: str,
        balance: float,
        bank_pwd: str = "",
        fund_pwd: str = "",
    ) -> int:
        return self._next_seq()

    def bank_transfer_out_async(
        self,
        account: PaperAccount | Any,
        bank_no: str,
        bank_account: str,
        balance: float,
        bank_pwd: str = "",
        fund_pwd: str = "",
    ) -> int:
        return self._next_seq()

    def query_bank_info(self, account: PaperAccount | Any) -> list[Any]:
        return []

    def query_bank_amount(
        self,
        account: PaperAccount | Any,
        bank_no: str,
        bank_account: str,
        bank_pwd: str,
    ) -> list[Any]:
        return []

    def query_bank_transfer_stream(
        self,
        account: PaperAccount | Any,
        start_date: str,
        end_date: str,
        bank_no: str = "",
        bank_account: str = "",
    ) -> list[Any]:
        return []

    # ------------------------------------------------------------------
    # CTP 跨市场资金划转（占位）
    # ------------------------------------------------------------------

    def ctp_transfer_option_to_future(
        self, opt_account_id: str, ft_account_id: str, balance: float
    ) -> tuple[bool, str]:
        return True, ""

    def ctp_transfer_option_to_future_async(
        self, opt_account_id: str, ft_account_id: str, balance: float
    ) -> int:
        return self._next_seq()

    def ctp_transfer_future_to_option(
        self, opt_account_id: str, ft_account_id: str, balance: float
    ) -> tuple[bool, str]:
        return True, ""

    def ctp_transfer_future_to_option_async(
        self, opt_account_id: str, ft_account_id: str, balance: float
    ) -> int:
        return self._next_seq()

    # ------------------------------------------------------------------
    # SMT 约定式交易（占位）
    # ------------------------------------------------------------------

    def smt_query_quoter(self, account: PaperAccount | Any) -> list[Any]:
        return []

    def smt_query_compact(self, account: PaperAccount | Any) -> list[Any]:
        return []

    def smt_query_order(self, account: PaperAccount | Any) -> list[Any]:
        return []

    def smt_negotiate_order_async(
        self,
        account: PaperAccount | Any,
        src_group_id: str,
        order_code: str,
        date: str,
        amount: float,
        apply_rate: float,
        dict_param: dict | None = None,
    ) -> int:
        return self._next_seq()

    def smt_appointment_order_async(
        self,
        account: PaperAccount | Any,
        order_code: str,
        date: str,
        amount: float,
        apply_rate: float,
    ) -> int:
        return self._next_seq()

    def smt_appointment_cancel_async(
        self, account: PaperAccount | Any, apply_id: str
    ) -> int:
        return self._next_seq()

    def smt_compact_renewal_async(
        self,
        account: PaperAccount | Any,
        cash_compact_id: str,
        order_code: str,
        defer_days: int,
        defer_num: int,
        apply_rate: float,
    ) -> int:
        return self._next_seq()

    def smt_compact_return_async(
        self,
        account: PaperAccount | Any,
        src_group_id: str,
        cash_compact_id: str,
        order_code: str,
        occur_amount: float,
    ) -> int:
        return self._next_seq()

    # ------------------------------------------------------------------
    # 持仓统计/数据导出/外部同步（占位）
    # ------------------------------------------------------------------

    def query_position_statistics(self, account: PaperAccount | Any) -> list[Any]:
        return []

    def export_data(
        self,
        account: PaperAccount | Any,
        result_path: str,
        data_type: str,
        start_time: str | None = None,
        end_time: str | None = None,
        user_param: dict | None = None,
    ) -> dict[str, Any]:
        return {"success": True}

    def query_data(
        self,
        account: PaperAccount | Any,
        result_path: str,
        data_type: str,
        start_time: str | None = None,
        end_time: str | None = None,
        user_param: dict | None = None,
    ) -> dict[str, Any]:
        return {"success": True}

    def sync_transaction_from_external(
        self,
        operation: str,
        data_type: str,
        account: PaperAccount | Any,
        deal_list: list[Any],
    ) -> dict[str, Any]:
        return {"success": True}

    # ------------------------------------------------------------------
    # 智能算法（占位）
    # ------------------------------------------------------------------

    def get_smart_algo_param(self, algo_name_list: list[str]) -> dict[str, Any]:
        return {}

    def smart_algo_order_async(
        self,
        account: PaperAccount | Any,
        stock_code: str,
        order_type: int,
        order_volume: int,
        price_type: int,
        price: float,
        strategy_name: str,
        order_remark: str,
        algo_name: str,
        start_time: str,
        end_time: str,
        algo_param: dict[str, Any],
    ) -> int:
        return self._next_seq()

    def query_smart_algo_task(self, account: PaperAccount | Any) -> dict[str, Any]:
        return {}

    def cancel_smart_algo_task_async(
        self, account: PaperAccount | Any, task_id: str
    ) -> int:
        return self._next_seq()

    # ------------------------------------------------------------------
    # 其他查询（占位）
    # ------------------------------------------------------------------

    def query_secu_account(self, account: PaperAccount | Any) -> list[Any]:
        return []

    # ------------------------------------------------------------------
    # 账户管理扩展（非 xtquant 标准 API，供管理器使用）
    # ------------------------------------------------------------------

    def create_account(self, config: PaperAccountConfig) -> AccountState:
        """创建或更新模拟账户。"""
        self._config_manager.set_config(config)
        state = self._config_manager.create_account_state(config)
        with self._lock:
            self._accounts[config.account_id] = state
        self._update_summary(config.account_id)
        logger.info(
            "模拟账户已创建/更新: %s, initial_cash=%.2f, price_source=%s",
            config.account_id,
            config.initial_cash,
            config.price_source,
        )
        return state

    def reset_account(self, account_id: str) -> bool:
        """重置指定账户。"""
        config = self._config_manager.get_config(account_id)
        if config is None:
            logger.warning("重置失败: 模拟账户 %s 不存在", account_id)
            return False
        state = self._config_manager.create_account_state(config)
        with self._lock:
            self._accounts[account_id] = state
        self._storage.remove_account_files(account_id)
        self._update_summary(account_id)
        logger.info("模拟账户已重置: %s", account_id)
        return True

    def delete_account(self, account_id: str) -> bool:
        """删除指定账户及其数据。"""
        with self._lock:
            self._accounts.pop(account_id, None)
        self._config_manager.delete_config(account_id)
        self._storage.remove_account_files(account_id)
        logger.info("模拟账户已删除: %s", account_id)
        return True

    def get_summary(self, account_id: str) -> AccountSummary:
        """获取账户业绩摘要。"""
        self._update_summary(account_id)
        return self._storage.read_summary(account_id)

    def get_all_summaries(self) -> dict[str, AccountSummary]:
        """获取所有账户业绩摘要。"""
        with self._lock:
            account_ids = list(self._accounts.keys())
        return {aid: self.get_summary(aid) for aid in account_ids}
