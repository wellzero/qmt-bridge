"""模拟交易引擎重启回放恢复测试。

覆盖场景：服务重启 / 账户重建后，账户资金与持仓从历史委托 CSV 回放恢复，
隔夜持仓可正常卖出；历史中的多次引擎重置只保留最后一段（无幽灵持仓）。
"""

from __future__ import annotations

import tempfile
from pathlib import Path

import pytest

from qmt_bridge.server.paper_trading import PaperQuantTrader
from qmt_bridge.server.paper_trading.account import ORDER_JUNK, ORDER_SUCCEEDED
from qmt_bridge.server.paper_trading.config import PaperAccountConfig
from qmt_bridge.server.paper_trading.engine import FIX_PRICE, STOCK_BUY, STOCK_SELL
from qmt_bridge.server.paper_trading.papertrader import PaperAccount
from qmt_bridge.server.paper_trading.storage import PaperTradingStorage

# 与 test_paper_trading.py 一致的账户配置：静态价格源、零费用，
# 保证撮合不依赖 xtquant 环境
ACCOUNT_ID = "restore_acc"


def _account_config(account_id: str = ACCOUNT_ID) -> PaperAccountConfig:
    return PaperAccountConfig(
        account_id=account_id,
        initial_cash=100000.0,
        price_source="static",
        static_prices={"000001.SZ": 10.0, "000002.SZ": 20.0},
        commission_rate=0.0,
        min_commission=0.0,
        stamp_tax_rate=0.0,
    )


def _make_trader(data_dir: Path) -> PaperQuantTrader:
    """按生产路径构造 PaperQuantTrader（模拟重启后的新实例）。

    与 ``PaperTraderManager.connect`` 一致：__init__ 用的默认 storage
    指向 cwd/data，需重定向到测试目录后重新加载配置与账户。
    """
    t = PaperQuantTrader(path="", session_id=1)
    t._storage.data_dir = data_dir
    t._storage._ensure_dirs()
    # 注意：ConfigManager 的属性名是 storage，误写 _storage 不生效，
    # 曾导致测试覆盖生产 config.json（沿用 test_paper_trading.py 的教训）
    t._config_manager.storage = t._storage
    t._config_manager._configs = {}
    t._accounts = {}
    t._config_manager._load()
    t._load_accounts()
    t.connect()
    t.start()
    return t


def _make_storage(data_dir: Path) -> PaperTradingStorage:
    """直接构造指向测试目录的存储（不经 trader，避免多余线程）。"""
    storage = PaperTradingStorage(data_dir)
    return storage


@pytest.fixture
def temp_data_dir():
    """提供临时数据目录。"""
    with tempfile.TemporaryDirectory() as tmpdir:
        yield Path(tmpdir)


@pytest.fixture
def trader(temp_data_dir: Path) -> PaperQuantTrader:
    """第一个会话的 trader：注册账户并保持连接。"""
    t = _make_trader(temp_data_dir)
    t.create_account(_account_config())
    return t


def test_buy_then_restart_restores_state_and_sell_succeeds(
    trader: PaperQuantTrader, temp_data_dir: Path
):
    """核心场景：买入 → 重启 → 资金/持仓恢复，且卖出不再废单。"""
    account = PaperAccount(ACCOUNT_ID)
    trader.order_stock(
        account=account,
        stock_code="000001.SZ",
        order_type=STOCK_BUY,
        order_volume=1000,
        price_type=FIX_PRICE,
        price=10.0,
    )
    trader.stop()

    # 模拟重启：新实例从同一数据目录加载账户
    trader2 = _make_trader(temp_data_dir)
    asset = trader2.query_stock_asset(account)
    assert asset is not None
    assert asset.cash == pytest.approx(90000.0)
    positions = trader2.query_stock_positions(account)
    assert len(positions) == 1
    assert positions[0].stock_code == "000001.SZ"
    assert positions[0].volume == 1000

    # 重启后卖出必须成交（修复前因持仓丢失被拒为废单）
    order_id = trader2.order_stock(
        account=account,
        stock_code="000001.SZ",
        order_type=STOCK_SELL,
        order_volume=1000,
        price_type=FIX_PRICE,
        price=10.0,
    )
    order = trader2.query_stock_order(account, order_id)
    assert order is not None
    assert order.order_status == ORDER_SUCCEEDED
    assert order.traded_volume == 1000

    asset2 = trader2.query_stock_asset(account)
    assert asset2.cash == pytest.approx(100000.0)
    assert asset2.market_value == pytest.approx(0.0)
    trader2.stop()


def test_restore_picks_last_segment_after_reset(temp_data_dir: Path):
    """历史含引擎重置断点：只恢复最后一段，不产生幽灵持仓。"""
    storage = _make_storage(temp_data_dir)

    def _row(order_id, time, code, volume, price, cash):
        return {
            "order_time": time,
            "order_id": order_id,
            "stock_code": code,
            "order_type": STOCK_BUY,
            "order_volume": volume,
            "price_type": FIX_PRICE,
            "price": price,
            "traded_volume": volume,
            "traded_price": price,
            "commission": 0.0,
            "stamp_tax": 0.0,
            "account_cash": cash,
            "account_market_value": 0,
            "order_status": ORDER_SUCCEEDED,
            "status_msg": "已成",
            "strategy_name": "",
            "order_remark": "",
        }

    # 旧段（模拟 07-01 会话）：买入后现金 90000，段末实际现金 80000
    storage.write_orders(
        ACCOUNT_ID,
        [_row(1, "09:31:00", "000001.SZ", 1000, 10.0, 90000.0)],
        date_str="20260701",
    )
    storage.write_orders(
        ACCOUNT_ID,
        [_row(2, "09:32:00", "000002.SZ", 500, 20.0, 80000.0)],
        date_str="20260702",
    )
    # 新段（模拟 09-01 重置后会话）：现金回到 100000 后买入
    storage.write_orders(
        ACCOUNT_ID,
        [_row(3, "09:31:00", "000001.SZ", 200, 10.0, 98000.0)],
        date_str="20260901",
    )
    storage.write_config({ACCOUNT_ID: _account_config().to_storage_dict()})

    trader = _make_trader(temp_data_dir)
    state = trader._accounts[ACCOUNT_ID]
    assert state.cash == pytest.approx(98000.0)
    # 旧段持仓被丢弃：只有新段的 000001.SZ × 200
    assert set(state.positions) == {"000001.SZ"}
    assert state.positions["000001.SZ"].volume == 200
    assert len(state.trades) == 1
    trader.stop()


def test_junk_tail_rows_do_not_pollute_restore(
    trader: PaperQuantTrader, temp_data_dir: Path
):
    """尾部废单行携带重置后的错误快照，不应污染恢复结果。"""
    account = PaperAccount(ACCOUNT_ID)
    trader.order_stock(
        account=account,
        stock_code="000001.SZ",
        order_type=STOCK_BUY,
        order_volume=1000,
        price_type=FIX_PRICE,
        price=10.0,
    )
    trader.stop()

    # 追加一笔"废单"行：引擎状态被重置后快照为 initial_cash/0
    # （复现 2026-09-03 price_acceleration 账户的真实故障数据形态）
    storage = _make_storage(temp_data_dir)
    storage.append_order(
        ACCOUNT_ID,
        {
            "order_time": "14:00:00",
            "order_id": 999,
            "stock_code": "000001.SZ",
            "order_type": STOCK_SELL,
            "order_volume": 1000,
            "price_type": FIX_PRICE,
            "price": 0.0,
            "traded_volume": 0,
            "traded_price": 0.0,
            "commission": 0.0,
            "stamp_tax": 0.0,
            "account_cash": 100000.0,
            "account_market_value": 0,
            "order_status": ORDER_JUNK,
            "status_msg": "可用持仓不足",
            "strategy_name": "",
            "order_remark": "",
        },
    )

    trader2 = _make_trader(temp_data_dir)
    asset = trader2.query_stock_asset(account)
    assert asset.cash == pytest.approx(90000.0)
    positions = trader2.query_stock_positions(account)
    assert len(positions) == 1
    assert positions[0].volume == 1000
    trader2.stop()


def test_cross_day_duplicate_orders_deduped(temp_data_dir: Path):
    """跨日进程把同一委托重写进次日文件，回放只应用一次。"""
    storage = _make_storage(temp_data_dir)

    row = {
        "order_time": "14:50:00",
        "order_id": 7,
        "stock_code": "000001.SZ",
        "order_type": STOCK_BUY,
        "order_volume": 1000,
        "price_type": FIX_PRICE,
        "price": 10.0,
        "traded_volume": 1000,
        "traded_price": 10.0,
        "commission": 0.0,
        "stamp_tax": 0.0,
        "account_cash": 90000.0,
        "account_market_value": 10000.0,
        "order_status": ORDER_SUCCEEDED,
        "status_msg": "已成",
        "strategy_name": "",
        "order_remark": "",
    }
    # 同一笔委托同时出现在 09-01 与 09-02 文件（_persist_orders 重写行为）
    storage.write_orders(ACCOUNT_ID, [row], date_str="20260901")
    storage.write_orders(ACCOUNT_ID, [row], date_str="20260902")
    storage.write_config({ACCOUNT_ID: _account_config().to_storage_dict()})

    trader = _make_trader(temp_data_dir)
    state = trader._accounts[ACCOUNT_ID]
    assert state.cash == pytest.approx(90000.0)
    assert state.positions["000001.SZ"].volume == 1000
    assert len(state.trades) == 1
    trader.stop()


def test_reset_account_starts_clean(trader: PaperQuantTrader, temp_data_dir: Path):
    """reset_account 语义保持：清空状态并删除历史文件，不做回放恢复。"""
    account = PaperAccount(ACCOUNT_ID)
    trader.order_stock(
        account=account,
        stock_code="000001.SZ",
        order_type=STOCK_BUY,
        order_volume=1000,
        price_type=FIX_PRICE,
        price=10.0,
    )

    assert trader.reset_account(ACCOUNT_ID) is True
    state = trader._accounts[ACCOUNT_ID]
    assert state.cash == pytest.approx(100000.0)
    assert state.positions == {}
    assert state.trades == []
    assert not (temp_data_dir / "paper_trading" / ACCOUNT_ID / "order").exists()

    # 重置后再重启也不应恢复出旧持仓
    trader.stop()
    trader2 = _make_trader(temp_data_dir)
    asset = trader2.query_stock_asset(account)
    assert asset.cash == pytest.approx(100000.0)
    assert trader2.query_stock_positions(account) == []
    trader2.stop()


def test_summary_stats_include_history_after_restart(
    trader: PaperQuantTrader, temp_data_dir: Path
):
    """重启后 summary 统计（成交笔数/已实现盈亏）包含历史成交。"""
    account = PaperAccount(ACCOUNT_ID)
    trader.order_stock(
        account=account,
        stock_code="000001.SZ",
        order_type=STOCK_BUY,
        order_volume=1000,
        price_type=FIX_PRICE,
        price=10.0,
    )
    trader.order_stock(
        account=account,
        stock_code="000001.SZ",
        order_type=STOCK_SELL,
        order_volume=1000,
        price_type=FIX_PRICE,
        price=10.0,
    )
    trader.stop()

    trader2 = _make_trader(temp_data_dir)
    summary = trader2.get_summary(ACCOUNT_ID)
    assert summary.total_trades == 2
    assert summary.realized_pnl == pytest.approx(0.0)
    assert summary.cash == pytest.approx(100000.0)
    trader2.stop()


def test_create_account_reregistration_preserves_state(
    trader: PaperQuantTrader, temp_data_dir: Path
):
    """策略每次启动都会重注册账户（create_account），不得清空已有状态。"""
    account = PaperAccount(ACCOUNT_ID)
    trader.order_stock(
        account=account,
        stock_code="000001.SZ",
        order_type=STOCK_BUY,
        order_volume=1000,
        price_type=FIX_PRICE,
        price=10.0,
    )

    # 模拟编排脚本重注册同一账户
    trader.create_account(_account_config())
    asset = trader.query_stock_asset(account)
    assert asset.cash == pytest.approx(90000.0)
    assert asset.market_value == pytest.approx(10000.0)
    trader.stop()


def test_same_day_restart_preserves_morning_rows(
    trader: PaperQuantTrader, temp_data_dir: Path
):
    """同日重启后新会话写单不得抹掉当日早前会话的委托行。"""
    account = PaperAccount(ACCOUNT_ID)
    trader.order_stock(
        account=account,
        stock_code="000001.SZ",
        order_type=STOCK_BUY,
        order_volume=1000,
        price_type=FIX_PRICE,
        price=10.0,
    )
    trader.stop()

    trader2 = _make_trader(temp_data_dir)
    trader2.order_stock(
        account=PaperAccount(ACCOUNT_ID),
        stock_code="000002.SZ",
        order_type=STOCK_BUY,
        order_volume=500,
        price_type=FIX_PRICE,
        price=20.0,
    )

    storage = trader2._storage
    rows = storage.read_orders(ACCOUNT_ID)
    codes = [r["stock_code"] for r in rows]
    assert "000001.SZ" in codes  # 早前会话的买入行仍在
    assert "000002.SZ" in codes

    # 再次重启：两笔买入都应被回放恢复
    trader2.stop()
    trader3 = _make_trader(temp_data_dir)
    state = trader3._accounts[ACCOUNT_ID]
    assert state.cash == pytest.approx(80000.0)
    assert state.positions["000001.SZ"].volume == 1000
    assert state.positions["000002.SZ"].volume == 500
    trader3.stop()
