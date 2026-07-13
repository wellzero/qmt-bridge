"""模拟交易账户配置管理模块。

提供每个账户的独立配置（初始资金、手续费率、印花税、滑点、价格源等），
并通过 JSON 文件持久化，支持运行时增删改查。
"""

from __future__ import annotations

import logging
from typing import Any

from pydantic import BaseModel, Field

from .account import AccountState
from .storage import PaperTradingStorage

logger = logging.getLogger("qmt_bridge.paper_trading")


class PaperAccountConfig(BaseModel):
    """单个模拟账户的配置。"""

    account_id: str = Field(..., description="资金账号 ID")
    account_type: int = Field(default=2, description="账户类型：2 普通股票，3 信用")
    initial_cash: float = Field(default=1_000_000.0, description="初始资金")
    commission_rate: float = Field(default=0.0003, description="手续费率")
    stamp_tax_rate: float = Field(default=0.0005, description="印花税率，仅卖出收取")
    slippage: float = Field(default=0.0, description="滑点比例")
    price_source: str = Field(
        default="fallback", description="价格源：xtdata / static / fallback"
    )
    static_prices: dict[str, float] = Field(
        default_factory=dict, description="静态价格表"
    )
    auto_download_prices: bool = Field(
        default=True,
        description="价格缺失时是否自动尝试从 xtquant 下载静态价格",
    )
    partial_fill_enabled: bool = Field(
        default=False, description="是否启用部分成交模拟"
    )
    enabled: bool = Field(default=True, description="账户是否启用")

    def to_storage_dict(self) -> dict[str, Any]:
        return self.model_dump()

    @classmethod
    def from_storage_dict(cls, data: dict[str, Any]) -> "PaperAccountConfig":
        return cls(**data)


class PaperAccountConfigManager:
    """账户配置管理器。

    以 ``account_id`` 为键维护所有模拟账户配置，支持从磁盘加载、保存、
    按配置创建 ``AccountState``。
    """

    def __init__(self, storage: PaperTradingStorage | None = None):
        self.storage = storage or PaperTradingStorage()
        self._configs: dict[str, PaperAccountConfig] = {}
        self._load()

    def _load(self) -> None:
        raw = self.storage.read_config()
        self._configs = {}
        for account_id, cfg in raw.items():
            try:
                self._configs[account_id] = PaperAccountConfig.from_storage_dict(cfg)
            except Exception:
                logger.exception("加载账户 %s 配置失败", account_id)

    def _save(self) -> None:
        data = {
            account_id: cfg.to_storage_dict()
            for account_id, cfg in self._configs.items()
        }
        self.storage.write_config(data)

    def list_accounts(self) -> list[str]:
        """返回所有已配置账户 ID。"""
        return list(self._configs.keys())

    def get_config(self, account_id: str) -> PaperAccountConfig | None:
        """查询单个账户配置。"""
        return self._configs.get(account_id)

    def set_config(self, config: PaperAccountConfig) -> PaperAccountConfig:
        """创建或更新账户配置。"""
        self._configs[config.account_id] = config
        self._save()
        return config

    def delete_config(self, account_id: str) -> bool:
        """删除账户配置。"""
        if account_id in self._configs:
            del self._configs[account_id]
            self._save()
            return True
        return False

    def create_account_state(self, config: PaperAccountConfig) -> AccountState:
        """根据配置创建并初始化账户状态。"""
        state = AccountState(
            account_id=config.account_id,
            account_type=config.account_type,
            cash=config.initial_cash,
            initial_cash=config.initial_cash,
        )
        return state


class DownloadPricesRequest(BaseModel):
    """批量下载静态价格请求。"""

    stock_codes: list[str] = Field(
        ..., description='待下载的股票代码列表，例如 ["000001.SZ", "600519.SH"]'
    )
