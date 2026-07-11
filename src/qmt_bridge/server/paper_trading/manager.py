"""模拟交易管理器模块。

提供 ``PaperTraderManager`` 类，作为 ``PaperQuantTrader`` 的生命周期包装器，
形态与 ``trading.manager.XtTraderManager`` 一致，便于在 FastAPI lifespan 和依赖注入中复用。
"""

from __future__ import annotations

import logging
from pathlib import Path
from typing import Any

from .config import PaperAccountConfig
from .papertrader import PaperAccount, PaperQuantTrader
from .storage import AccountSummary

logger = logging.getLogger("qmt_bridge.paper_trading")


class PaperTraderManager:
    """模拟交易管理器。

    在 FastAPI lifespan 启动阶段、当模拟交易功能启用时被创建。
    负责维护 ``PaperQuantTrader`` 实例，并提供账户配置、交易操作、业绩查询的统一入口。
    """

    def __init__(
        self,
        data_dir: Path | str | None = None,
        config_path: Path | str | None = None,
        default_account_id: str = "",
    ):
        self.data_dir = data_dir
        self.config_path = config_path
        self.default_account_id = default_account_id
        self._trader: PaperQuantTrader | None = None
        self._account: PaperAccount | None = None

    def connect(self) -> None:
        """初始化并连接 PaperQuantTrader 实例。"""
        from .storage import PaperTradingStorage

        storage = PaperTradingStorage(self.data_dir)
        # config_path 若指定，可覆盖默认 data_dir 下的 config.json
        if self.config_path:
            storage.config_path = Path(self.config_path)

        self._trader = PaperQuantTrader(path="", session_id=0)
        self._trader._storage = storage
        self._trader._config_manager._storage = storage
        self._trader._config_manager._load()
        self._trader._load_accounts()

        if self.default_account_id:
            self._account = PaperAccount(self.default_account_id)
            self._trader.subscribe(self._account)

        self._trader.start()
        self._trader.connect()
        logger.info(
            "PaperQuantTrader connected, default_account=%s", self.default_account_id
        )

    def disconnect(self) -> None:
        """断开连接并清理资源。"""
        if self._trader is not None:
            try:
                self._trader.stop()
            except Exception:
                logger.exception("Error stopping PaperQuantTrader")
            self._trader = None

    def _resolve_account(self, account_id: str = "") -> PaperAccount:
        """解析交易账户，返回 PaperAccount 实例。"""
        target = account_id or self.default_account_id
        return PaperAccount(target)

    # ------------------------------------------------------------------
    # 账户配置管理
    # ------------------------------------------------------------------

    def create_or_update_account(
        self, config: PaperAccountConfig
    ) -> PaperAccountConfig:
        """创建或更新模拟账户配置。"""
        if self._trader is None:
            raise RuntimeError("PaperQuantTrader is not connected")
        self._trader.create_account(config)
        return config

    def get_account_config(self, account_id: str) -> PaperAccountConfig | None:
        if self._trader is None:
            return None
        return self._trader._config_manager.get_config(account_id)

    def list_account_configs(self) -> list[PaperAccountConfig]:
        if self._trader is None:
            return []
        return [
            self._trader._config_manager.get_config(aid)
            for aid in self._trader._config_manager.list_accounts()
            if self._trader._config_manager.get_config(aid) is not None
        ]

    def reset_account(self, account_id: str) -> bool:
        if self._trader is None:
            return False
        return self._trader.reset_account(account_id)

    def delete_account(self, account_id: str) -> bool:
        if self._trader is None:
            return False
        return self._trader.delete_account(account_id)

    def get_summary(self, account_id: str = "") -> AccountSummary | None:
        if self._trader is None:
            return None
        target = account_id or self.default_account_id
        return self._trader.get_summary(target)

    def get_all_summaries(self) -> dict[str, AccountSummary]:
        if self._trader is None:
            return {}
        return self._trader.get_all_summaries()

    # ------------------------------------------------------------------
    # 委托操作
    # ------------------------------------------------------------------

    def order(
        self,
        stock_code: str,
        order_type: int,
        order_volume: int,
        price_type: int = 5,
        price: float = 0.0,
        strategy_name: str = "",
        order_remark: str = "",
        account_id: str = "",
    ) -> int:
        if self._trader is None:
            raise RuntimeError("PaperQuantTrader is not connected")
        account = self._resolve_account(account_id)
        return self._trader.order_stock(
            account,
            stock_code,
            order_type,
            order_volume,
            price_type,
            price,
            strategy_name,
            order_remark,
        )

    def order_async(
        self,
        stock_code: str,
        order_type: int,
        order_volume: int,
        price_type: int = 5,
        price: float = 0.0,
        strategy_name: str = "",
        order_remark: str = "",
        account_id: str = "",
    ) -> int:
        if self._trader is None:
            raise RuntimeError("PaperQuantTrader is not connected")
        account = self._resolve_account(account_id)
        return self._trader.order_stock_async(
            account,
            stock_code,
            order_type,
            order_volume,
            price_type,
            price,
            strategy_name,
            order_remark,
        )

    def cancel_order(self, order_id: int, account_id: str = "") -> int:
        if self._trader is None:
            raise RuntimeError("PaperQuantTrader is not connected")
        account = self._resolve_account(account_id)
        return self._trader.cancel_order_stock(account, order_id)

    def cancel_order_async(self, order_id: int, account_id: str = "") -> int:
        if self._trader is None:
            raise RuntimeError("PaperQuantTrader is not connected")
        account = self._resolve_account(account_id)
        return self._trader.cancel_order_stock_async(account, order_id)

    # ------------------------------------------------------------------
    # 查询操作
    # ------------------------------------------------------------------

    def query_orders(
        self, account_id: str = "", cancelable_only: bool = False
    ) -> list[Any]:
        if self._trader is None:
            return []
        account = self._resolve_account(account_id)
        return self._trader.query_stock_orders(account, cancelable_only)

    def query_positions(self, account_id: str = "") -> list[Any]:
        if self._trader is None:
            return []
        account = self._resolve_account(account_id)
        return self._trader.query_stock_positions(account)

    def query_asset(self, account_id: str = "") -> Any:
        if self._trader is None:
            return None
        account = self._resolve_account(account_id)
        return self._trader.query_stock_asset(account)

    def query_trades(self, account_id: str = "") -> list[Any]:
        if self._trader is None:
            return []
        account = self._resolve_account(account_id)
        return self._trader.query_stock_trades(account)

    def query_order_detail(self, order_id: int = 0, account_id: str = "") -> Any:
        if self._trader is None:
            return None
        account = self._resolve_account(account_id)
        return self._trader.query_stock_order(account, order_id)

    def query_single_position(self, stock_code: str, account_id: str = "") -> Any:
        if self._trader is None:
            return None
        account = self._resolve_account(account_id)
        return self._trader.query_stock_position(account, stock_code)

    def get_account_status(self, account_id: str = "") -> dict[str, bool]:
        return {"connected": self._trader is not None}

    def query_account_infos(self) -> list[Any]:
        if self._trader is None:
            return []
        return self._trader.query_account_infos()
