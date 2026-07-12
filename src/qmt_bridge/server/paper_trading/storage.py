"""模拟交易持久化模块。

负责按 ``account_id`` 写入 CSV 委托流水、JSON 业绩摘要以及账户配置文件。
所有写操作使用临时文件 + ``os.replace`` 保证原子性，避免并发写损坏。
"""

from __future__ import annotations

import csv
import json
import logging
import os
import tempfile
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any

logger = logging.getLogger("qmt_bridge.paper_trading")

ORDERS_HEADER = [
    "order_time",
    "order_id",
    "stock_code",
    "order_type",
    "order_volume",
    "price_type",
    "price",
    "traded_volume",
    "traded_price",
    "order_status",
    "status_msg",
    "strategy_name",
    "order_remark",
]


@dataclass
class AccountSummary:
    """单账户业绩摘要。"""

    account_id: str
    initial_cash: float = 0.0
    cash: float = 0.0
    market_value: float = 0.0
    total_asset: float = 0.0
    total_pnl: float = 0.0
    total_return_rate: float = 0.0
    realized_pnl: float = 0.0
    unrealized_pnl: float = 0.0
    total_trades: int = 0
    total_commission: float = 0.0
    total_stamp_tax: float = 0.0

    def to_dict(self) -> dict[str, Any]:
        return {
            "account_id": self.account_id,
            "initial_cash": self.initial_cash,
            "cash": self.cash,
            "market_value": self.market_value,
            "total_asset": self.total_asset,
            "total_pnl": self.total_pnl,
            "total_return_rate": self.total_return_rate,
            "realized_pnl": self.realized_pnl,
            "unrealized_pnl": self.unrealized_pnl,
            "total_trades": self.total_trades,
            "total_commission": self.total_commission,
            "total_stamp_tax": self.total_stamp_tax,
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "AccountSummary":
        return cls(
            account_id=data.get("account_id", ""),
            initial_cash=data.get("initial_cash", 0.0),
            cash=data.get("cash", 0.0),
            market_value=data.get("market_value", 0.0),
            total_asset=data.get("total_asset", 0.0),
            total_pnl=data.get("total_pnl", 0.0),
            total_return_rate=data.get("total_return_rate", 0.0),
            realized_pnl=data.get("realized_pnl", 0.0),
            unrealized_pnl=data.get("unrealized_pnl", 0.0),
            total_trades=data.get("total_trades", 0),
            total_commission=data.get("total_commission", 0.0),
            total_stamp_tax=data.get("total_stamp_tax", 0.0),
        )


class PaperTradingStorage:
    """模拟交易持久化存储。

    每个账户的数据独立存放在 ``{data_dir}/paper_trading/{account_id}/`` 下：

    - ``order/orders_{YYYYMMDD}.csv`` —— 委托流水
    - ``summary/summary.json`` —— 业绩摘要

    全局账户配置文件 ``config.json`` 仍保留在 ``{data_dir}/paper_trading/`` 根目录。

    为兼容历史配置，若传入的 ``data_dir`` 路径本身以 ``paper_trading`` 结尾，
    则直接将其作为模拟交易根目录，不再追加一层 ``paper_trading``。
    """

    def __init__(self, data_dir: Path | str | None = None):
        self.data_dir = Path(data_dir or Path.cwd() / "data")
        self.paper_trading_dir = self._resolve_paper_trading_dir(self.data_dir)
        self._ensure_dirs()

    @staticmethod
    def _resolve_paper_trading_dir(data_dir: Path) -> Path:
        """解析模拟交易根目录。

        若 ``data_dir`` 已以 ``paper_trading`` 结尾，则直接返回；
        否则返回 ``data_dir / "paper_trading"``。
        """
        if data_dir.name.lower() == "paper_trading":
            return data_dir
        return data_dir / "paper_trading"

    def _ensure_dirs(self) -> None:
        self.paper_trading_dir.mkdir(parents=True, exist_ok=True)

    def _account_dir(self, account_id: str) -> Path:
        """返回单个账户的数据目录。"""
        safe_id = account_id.replace("/", "_").replace("\\", "_")
        return self.paper_trading_dir / safe_id

    # ── 配置文件 ──

    @property
    def config_path(self) -> Path:
        return self.paper_trading_dir / "config.json"

    def read_config(self) -> dict[str, Any]:
        if not self.config_path.exists():
            return {}
        try:
            with open(self.config_path, encoding="utf-8") as f:
                return json.load(f)
        except Exception:
            logger.exception("读取模拟交易配置文件失败: %s", self.config_path)
            return {}

    def write_config(self, config: dict[str, Any]) -> None:
        self._write_json(self.config_path, config)
        logger.debug("模拟交易配置已写入: %s", self.config_path)

    # ── 委托 CSV ──

    def orders_path(self, account_id: str, date_str: str | None = None) -> Path:
        if date_str is None:
            date_str = datetime.now().strftime("%Y%m%d")
        account_dir = self._account_dir(account_id)
        orders_dir = account_dir / "order"
        orders_dir.mkdir(parents=True, exist_ok=True)
        return orders_dir / f"orders_{date_str}.csv"

    def append_order(self, account_id: str, order_row: dict[str, Any]) -> None:
        path = self.orders_path(account_id)
        file_exists = path.exists()
        try:
            with open(path, "a", newline="", encoding="utf-8") as f:
                writer = csv.DictWriter(f, fieldnames=ORDERS_HEADER)
                if not file_exists:
                    writer.writeheader()
                writer.writerow({k: order_row.get(k, "") for k in ORDERS_HEADER})
        except Exception:
            logger.exception("追加委托 CSV 失败: %s", path)

    def read_orders(
        self, account_id: str, date_str: str | None = None
    ) -> list[dict[str, Any]]:
        path = self.orders_path(account_id, date_str)
        if not path.exists():
            return []
        try:
            with open(path, newline="", encoding="utf-8") as f:
                return list(csv.DictReader(f))
        except Exception:
            logger.exception("读取委托 CSV 失败: %s", path)
            return []

    def write_orders(
        self, account_id: str, orders: list[dict[str, Any]], date_str: str | None = None
    ) -> None:
        path = self.orders_path(account_id, date_str)
        orders_dir = path.parent
        try:
            with tempfile.NamedTemporaryFile(
                mode="w",
                newline="",
                encoding="utf-8",
                delete=False,
                dir=orders_dir,
            ) as tmp:
                writer = csv.DictWriter(tmp, fieldnames=ORDERS_HEADER)
                writer.writeheader()
                for order in orders:
                    writer.writerow({k: order.get(k, "") for k in ORDERS_HEADER})
                tmp_path = Path(tmp.name)
            os.replace(tmp_path, path)
            logger.debug(
                "账户 %s 委托 CSV 已写入: %s (rows=%d)", account_id, path, len(orders)
            )
        except Exception:
            logger.exception("写入委托 CSV 失败: %s", path)

    # ── 业绩摘要 JSON ──

    def summary_path(self, account_id: str) -> Path:
        account_dir = self._account_dir(account_id)
        summary_dir = account_dir / "summary"
        summary_dir.mkdir(parents=True, exist_ok=True)
        return summary_dir / "summary.json"

    def read_summary(self, account_id: str) -> AccountSummary:
        path = self.summary_path(account_id)
        if not path.exists():
            return AccountSummary(account_id=account_id)
        try:
            with open(path, encoding="utf-8") as f:
                return AccountSummary.from_dict(json.load(f))
        except Exception:
            logger.exception("读取业绩摘要失败: %s", path)
            return AccountSummary(account_id=account_id)

    def write_summary(self, summary: AccountSummary) -> None:
        self._write_json(self.summary_path(summary.account_id), summary.to_dict())
        logger.debug(
            "账户 %s 业绩摘要已写入: total_asset=%.2f total_pnl=%.2f",
            summary.account_id,
            summary.total_asset,
            summary.total_pnl,
        )

    # ── 通用 JSON 原子写 ──

    def _write_json(self, path: Path, data: dict[str, Any]) -> None:
        try:
            path.parent.mkdir(parents=True, exist_ok=True)
            with tempfile.NamedTemporaryFile(
                mode="w", encoding="utf-8", delete=False, dir=path.parent
            ) as tmp:
                json.dump(data, tmp, ensure_ascii=False, indent=2)
                tmp_path = Path(tmp.name)
            os.replace(tmp_path, path)
        except Exception:
            logger.exception("写入 JSON 失败: %s", path)

    # ── 账户数据清理 ──

    def remove_account_files(self, account_id: str) -> None:
        """删除某账户的 CSV、摘要文件及空账户目录。"""
        account_dir = self._account_dir(account_id)
        for path in [
            self.summary_path(account_id),
            self.orders_path(account_id),
        ]:
            try:
                path.unlink(missing_ok=True)
                logger.debug("已删除账户 %s 文件: %s", account_id, path)
            except Exception:
                logger.exception("删除文件失败: %s", path)
        # 尝试删除空账户目录
        try:
            if account_dir.exists() and not any(account_dir.iterdir()):
                account_dir.rmdir()
                logger.debug("已删除空账户目录: %s", account_dir)
        except Exception:
            logger.exception("删除账户目录失败: %s", account_dir)
