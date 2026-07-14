"""模拟交易模块单元测试与集成测试。"""

from __future__ import annotations

import tempfile
from pathlib import Path

import pytest

from qmt_bridge.server.paper_trading import PaperQuantTrader, PaperTraderManager
from qmt_bridge.server.paper_trading.account import ORDER_SUCCEEDED
from qmt_bridge.server.paper_trading.config import PaperAccountConfig
from qmt_bridge.server.paper_trading.engine import (
    FIX_PRICE,
    STOCK_BUY,
    STOCK_SELL,
    StaticPriceSource,
)
from qmt_bridge.server.paper_trading.papertrader import PaperAccount
from qmt_bridge.server.paper_trading.storage import AccountSummary, PaperTradingStorage


@pytest.fixture
def temp_data_dir():
    """提供临时数据目录。"""
    with tempfile.TemporaryDirectory() as tmpdir:
        yield Path(tmpdir)


@pytest.fixture
def trader(temp_data_dir: Path):
    """提供已连接的 PaperQuantTrader 实例。"""
    t = PaperQuantTrader(path="", session_id=1)
    t._storage.data_dir = temp_data_dir
    t._storage._ensure_dirs()
    t._config_manager._storage = t._storage
    t._config_manager._configs = {}
    t._accounts = {}
    t.connect()
    t.start()
    return t


def test_paper_account_attributes():
    """PaperAccount 对外属性与 StockAccount 对齐。"""
    account = PaperAccount("123456", account_type=2)
    assert account.account_id == "123456"
    assert account.account_type == 2


def test_create_account_and_trade(trader: PaperQuantTrader, temp_data_dir: Path):
    """创建账户、下单、查询资产与持仓。"""
    config = PaperAccountConfig(
        account_id="acc001",
        initial_cash=100000.0,
        price_source="static",
        static_prices={"000001.SZ": 10.0},
        commission_rate=0.0,
        min_commission=0.0,
        stamp_tax_rate=0.0,
    )
    trader.create_account(config)

    account = PaperAccount("acc001")
    order_id = trader.order_stock(
        account=account,
        stock_code="000001.SZ",
        order_type=STOCK_BUY,
        order_volume=1000,
        price_type=FIX_PRICE,
        price=10.0,
    )
    assert order_id > 0

    # 查询资产
    asset = trader.query_stock_asset(account)
    assert asset is not None
    assert asset.cash == pytest.approx(90000.0)
    assert asset.market_value == pytest.approx(10000.0)
    assert asset.total_asset == pytest.approx(100000.0)

    # 查询持仓
    positions = trader.query_stock_positions(account)
    assert len(positions) == 1
    assert positions[0].stock_code == "000001.SZ"
    assert positions[0].volume == 1000
    assert positions[0].avg_price == pytest.approx(10.0)

    # 查询委托
    orders = trader.query_stock_orders(account)
    assert len(orders) == 1
    assert orders[0].order_status == ORDER_SUCCEEDED

    # 查询成交
    trades = trader.query_stock_trades(account)
    assert len(trades) == 1
    assert trades[0].traded_volume == 1000


def test_sell_stock_and_summary(trader: PaperQuantTrader, temp_data_dir: Path):
    """卖出股票并验证业绩摘要。"""
    config = PaperAccountConfig(
        account_id="acc002",
        initial_cash=100000.0,
        price_source="static",
        static_prices={"000001.SZ": 10.0},
        commission_rate=0.0,
        min_commission=0.0,
        stamp_tax_rate=0.0,
    )
    trader.create_account(config)
    account = PaperAccount("acc002")

    # 买入
    trader.order_stock(
        account=account,
        stock_code="000001.SZ",
        order_type=STOCK_BUY,
        order_volume=1000,
        price_type=FIX_PRICE,
        price=10.0,
    )

    # 涨价后卖出
    config.static_prices = {"000001.SZ": 12.0}
    trader._config_manager.set_config(config)
    trader.order_stock(
        account=account,
        stock_code="000001.SZ",
        order_type=STOCK_SELL,
        order_volume=1000,
        price_type=FIX_PRICE,
        price=12.0,
    )

    summary = trader.get_summary("acc002")
    assert summary.total_pnl == pytest.approx(2000.0)
    assert summary.realized_pnl == pytest.approx(2000.0)
    assert summary.total_trades == 2


def test_multi_account_isolation(trader: PaperQuantTrader, temp_data_dir: Path):
    """多账户资金与持仓隔离。"""
    for aid in ("acc_a", "acc_b"):
        config = PaperAccountConfig(
            account_id=aid,
            initial_cash=100000.0,
            price_source="static",
            static_prices={"000001.SZ": 10.0},
            commission_rate=0.0,
            min_commission=0.0,
            stamp_tax_rate=0.0,
        )
        trader.create_account(config)

    trader.order_stock(
        account=PaperAccount("acc_a"),
        stock_code="000001.SZ",
        order_type=STOCK_BUY,
        order_volume=1000,
        price_type=FIX_PRICE,
        price=10.0,
    )

    asset_a = trader.query_stock_asset(PaperAccount("acc_a"))
    asset_b = trader.query_stock_asset(PaperAccount("acc_b"))
    assert asset_a.cash == pytest.approx(90000.0)
    assert asset_b.cash == pytest.approx(100000.0)


def test_csv_order_logging(trader: PaperQuantTrader, temp_data_dir: Path):
    """下单后 CSV 文件是否正确记录。"""
    config = PaperAccountConfig(
        account_id="acc003",
        initial_cash=100000.0,
        price_source="static",
        static_prices={"000001.SZ": 10.0},
        commission_rate=0.0,
        min_commission=0.0,
        stamp_tax_rate=0.0,
    )
    trader.create_account(config)
    trader.order_stock(
        account=PaperAccount("acc003"),
        stock_code="000001.SZ",
        order_type=STOCK_BUY,
        order_volume=500,
        price_type=FIX_PRICE,
        price=10.0,
    )

    # 直接通过 storage 接口读取当前日期的 CSV 记录
    rows = trader._storage.read_orders("acc003")
    assert len(rows) == 1
    assert rows[0]["stock_code"] == "000001.SZ"
    assert rows[0]["order_volume"] == "500"


def test_manager_lifecycle(temp_data_dir: Path):
    """PaperTraderManager 生命周期与配置管理。"""
    manager = PaperTraderManager(data_dir=temp_data_dir)
    manager.connect()

    config = PaperAccountConfig(
        account_id="acc_mgr",
        initial_cash=50000.0,
        price_source="static",
        static_prices={"600519.SH": 1000.0},
        commission_rate=0.0,
        min_commission=0.0,
        stamp_tax_rate=0.0,
    )
    manager.create_or_update_account(config)

    retrieved = manager.get_account_config("acc_mgr")
    assert retrieved is not None
    assert retrieved.initial_cash == pytest.approx(50000.0)

    order_id = manager.order(
        stock_code="600519.SH",
        order_type=STOCK_BUY,
        order_volume=10,
        price_type=FIX_PRICE,
        price=1000.0,
        account_id="acc_mgr",
    )
    assert order_id > 0

    asset = manager.query_asset("acc_mgr")
    assert asset is not None
    assert asset.cash == pytest.approx(40000.0)

    manager.disconnect()


def test_router_endpoints(temp_data_dir: Path):
    """FastAPI 路由集成测试（需要 xtquant 环境）。"""
    pytest.importorskip("xtquant")

    from fastapi.testclient import TestClient

    from qmt_bridge.server.app import create_app
    from qmt_bridge.server.config import Settings, reset_settings

    settings = Settings(
        host="0.0.0.0",
        port=8000,
        api_key="test-key",
        paper_trading_enabled=True,
        paper_trading_data_dir=str(temp_data_dir),
    )
    reset_settings(settings)

    app = create_app(settings)
    client = TestClient(app)
    headers = {"X-API-Key": "test-key"}

    # 创建账户
    resp = client.post(
        "/api/paper_accounts",
        headers=headers,
        json={
            "account_id": "api_acc",
            "initial_cash": 100000.0,
            "price_source": "static",
            "static_prices": {"000001.SZ": 10.0},
            "commission_rate": 0.0,
            "min_commission": 0.0,
            "stamp_tax_rate": 0.0,
        },
    )
    assert resp.status_code == 200

    # 下单
    resp = client.post(
        "/api/paper_trading/order",
        headers=headers,
        json={
            "account_id": "api_acc",
            "stock_code": "000001.SZ",
            "order_type": STOCK_BUY,
            "order_volume": 1000,
            "price_type": FIX_PRICE,
            "price": 10.0,
        },
    )
    assert resp.status_code == 200
    assert resp.json()["order_id"] > 0

    # 查询资产
    resp = client.get("/api/paper_trading/asset?account_id=api_acc", headers=headers)
    assert resp.status_code == 200
    assert resp.json()["data"]["cash"] == pytest.approx(90000.0)

    # 查询业绩
    resp = client.get("/api/paper_trading/summary?account_id=api_acc", headers=headers)
    assert resp.status_code == 200
    data = resp.json()["data"]
    assert data["total_asset"] == pytest.approx(100000.0)


def test_router_download_prices_with_mock(temp_data_dir: Path):
    """下载静态价格端点集成测试（mock xtquant）。"""
    pytest.importorskip("xtquant")

    from unittest.mock import MagicMock, patch

    from fastapi.testclient import TestClient

    from qmt_bridge.server.app import create_app
    from qmt_bridge.server.config import Settings, reset_settings

    settings = Settings(
        host="0.0.0.0",
        port=8000,
        api_key="test-key",
        paper_trading_enabled=True,
        paper_trading_data_dir=str(temp_data_dir),
    )
    reset_settings(settings)

    app = create_app(settings)
    client = TestClient(app)
    headers = {"X-API-Key": "test-key"}

    # 创建账户
    resp = client.post(
        "/api/paper_accounts",
        headers=headers,
        json={
            "account_id": "api_download",
            "initial_cash": 100000.0,
            "price_source": "static",
            "static_prices": {},
        },
    )
    assert resp.status_code == 200

    mock_xtdata = MagicMock()
    mock_xtdata.get_full_tick.return_value = {
        "000001.SZ": {"lastPrice": 12.34},
        "600519.SH": {"close": 567.89},
    }
    mock_xtquant_module = MagicMock()
    mock_xtquant_module.xtdata = mock_xtdata

    with patch.dict("sys.modules", {"xtquant": mock_xtquant_module}):
        resp = client.post(
            "/api/paper_accounts/api_download/download_prices",
            headers=headers,
            json={"stock_codes": ["000001.SZ", "600519.SH"]},
        )

    assert resp.status_code == 200
    data = resp.json()["data"]
    assert data["000001.SZ"] == pytest.approx(12.34)
    assert data["600519.SH"] == pytest.approx(567.89)

    # 验证配置已持久化
    resp = client.get("/api/paper_accounts/api_download", headers=headers)
    assert resp.status_code == 200
    assert resp.json()["data"]["static_prices"]["000001.SZ"] == pytest.approx(12.34)


def test_static_price_source_download_with_mock():
    """StaticPriceSource 从 xtquant 下载价格（mock）。"""
    from unittest.mock import MagicMock, patch

    source = StaticPriceSource(prices={"old.SZ": 5.0})

    mock_xtdata = MagicMock()
    mock_xtdata.get_full_tick.return_value = {
        "000001.SZ": {"lastPrice": 12.34},
        "600519.SH": {"close": 567.89},
        "bad.SZ": {},
    }
    mock_xtquant_module = MagicMock()
    mock_xtquant_module.xtdata = mock_xtdata

    with patch.dict("sys.modules", {"xtquant": mock_xtquant_module}):
        downloaded = source.download_prices(["000001.SZ", "600519.SH", "bad.SZ"])

    assert downloaded == {"000001.SZ": 12.34, "600519.SH": 567.89}
    assert source.prices["000001.SZ"] == pytest.approx(12.34)
    assert source.prices["600519.SH"] == pytest.approx(567.89)
    assert source.prices["old.SZ"] == pytest.approx(5.0)


def test_manager_download_prices_with_mock(temp_data_dir: Path):
    """PaperTraderManager 下载并保存静态价格（mock）。"""
    from unittest.mock import MagicMock, patch

    manager = PaperTraderManager(data_dir=temp_data_dir)
    manager.connect()

    config = PaperAccountConfig(
        account_id="acc_download",
        initial_cash=100000.0,
        price_source="static",
        static_prices={},
    )
    manager.create_or_update_account(config)

    mock_xtdata = MagicMock()
    mock_xtdata.get_full_tick.return_value = {
        "000001.SZ": {"lastPrice": 12.34},
    }
    mock_xtquant_module = MagicMock()
    mock_xtquant_module.xtdata = mock_xtdata

    with patch.dict("sys.modules", {"xtquant": mock_xtquant_module}):
        downloaded = manager.download_prices("acc_download", ["000001.SZ"])

    assert downloaded == {"000001.SZ": 12.34}

    updated_config = manager.get_account_config("acc_download")
    assert updated_config is not None
    assert updated_config.static_prices["000001.SZ"] == pytest.approx(12.34)

    manager.disconnect()


def test_storage_folder_structure(temp_data_dir: Path):
    """账户数据按 {account_id}/order/ 与 {account_id}/summary/ 分层存储。"""
    storage = PaperTradingStorage(data_dir=temp_data_dir)
    storage.append_order(
        "acc_folder",
        {
            "order_time": "10:00:00",
            "order_id": 1,
            "stock_code": "000001.SZ",
            "order_type": 23,
            "order_volume": 100,
            "price_type": 11,
            "price": 10.0,
            "traded_volume": 100,
            "traded_price": 10.0,
            "order_status": 50,
            "status_msg": "已成",
            "strategy_name": "test",
            "order_remark": "",
        },
    )
    summary = AccountSummary(
        account_id="acc_folder", cash=90000.0, total_asset=100000.0
    )
    storage.write_summary(summary)

    orders_path = storage.orders_path("acc_folder")
    summary_path = storage.summary_path("acc_folder")

    assert orders_path.relative_to(temp_data_dir).parts[:3] == (
        "paper_trading",
        "acc_folder",
        "order",
    )
    assert summary_path.relative_to(temp_data_dir).parts[:3] == (
        "paper_trading",
        "acc_folder",
        "summary",
    )
    assert orders_path.name.startswith("orders_")
    assert summary_path.name == "summary.json"


def datetime_now_str() -> str:
    """返回当前日期字符串（YYYYMMDD）。"""
    from datetime import datetime

    return datetime.now().strftime("%Y%m%d")
