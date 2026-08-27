"""交易后端抽象测试 ── TraderBackend 协议 / 双后端 / 能力位降级。

对应 ``docs/big-qmt.md`` §3.2/§3.4/§4（后端抽象 PR，§6 步骤 1），
全部使用 mock，不依赖 QMT / xtquant / xtquant-big-convert 环境。

覆盖点：
- 能力位矩阵与 §4 映射表一致（mini 全量；bigqmt 委托/查询/信用/账户）
- 双后端实现 CAPABILITY_METHODS 方法全集（协议不漂移）
- bigqmt 后端未支持方法 → UnsupportedOperation → 路由 503
- BigQmtAdapter 逐方法映射（fake compat 单例）
- XtTraderManager 门面转发与后端工厂
- xtquant import shim 目录检测与 sys.path 安装
"""

from __future__ import annotations

import sys
from pathlib import Path
from types import SimpleNamespace

import pytest
from fastapi.testclient import TestClient

from qmt_bridge.server.config import Settings, reset_settings
from qmt_bridge.server.trading.backend import (
    CAPABILITY_METHODS,
    UnsupportedOperation,
    capability_of,
)
from qmt_bridge.server.trading.bigqmt_backend import BigQmtAdapter
from qmt_bridge.server.trading.manager import XtTraderManager
from qmt_bridge.server.trading.mini_backend import MiniQmtBackend

ALL_GROUPS = frozenset(CAPABILITY_METHODS)
LIFECYCLE_METHODS = ("connect", "disconnect", "supports")


# ------------------------------------------------------------------
# 能力位矩阵（对齐 docs/big-qmt.md §4）
# ------------------------------------------------------------------


def test_capability_groups_cover_expected_api_groups():
    """能力位分组齐全（§4 的 12 个 API 组）。"""
    assert ALL_GROUPS == {
        "order",
        "query",
        "credit",
        "account",
        "secu_account",
        "bank",
        "transfer",
        "ctp",
        "smt",
        "ipo",
        "com",
        "export",
    }


def test_mini_backend_supports_all_capabilities():
    """mini 后端：全部能力位支持（原行为不变）。"""
    backend = MiniQmtBackend()
    assert backend.name == "mini"
    assert backend.SUPPORTED_CAPABILITIES == ALL_GROUPS
    for group in ALL_GROUPS:
        assert backend.supports(group)


def test_bigqmt_backend_capability_matrix_matches_design_doc():
    """bigqmt 后端能力位 = §4 的 ✅/可用项：委托 / 查询 / 信用 / 账户。"""
    backend = BigQmtAdapter()
    assert backend.name == "bigqmt"
    assert backend.SUPPORTED_CAPABILITIES == frozenset(
        {"order", "query", "credit", "account"}
    )
    # ❌ 组全部降级
    for group in (
        "secu_account",
        "bank",
        "transfer",
        "ctp",
        "smt",
        "ipo",
        "com",
        "export",
    ):
        assert not backend.supports(group)


def test_capability_of_mapping():
    """方法名 → 能力位分组反查。"""
    assert capability_of("bank_transfer_in") == "bank"
    assert capability_of("order") == "order"
    assert capability_of("query_credit_detail") == "credit"
    assert capability_of("connect") is None


# ------------------------------------------------------------------
# 协议完整性：双后端实现方法全集，不会与 CAPABILITY_METHODS 漂移
# ------------------------------------------------------------------


@pytest.mark.parametrize("backend_name", ["mini", "bigqmt"])
def test_backends_implement_full_method_surface(backend_name):
    """双后端都实现受控方法全集 + 生命周期方法。"""
    backend = MiniQmtBackend() if backend_name == "mini" else BigQmtAdapter()
    for group, methods in CAPABILITY_METHODS.items():
        for name in methods:
            assert callable(
                getattr(backend, name)
            ), f"{backend_name} 后端缺少 {group} 组方法 {name}()"
    for name in LIFECYCLE_METHODS:
        assert callable(getattr(backend, name))


# ------------------------------------------------------------------
# bigqmt 能力位降级 → UnsupportedOperation
# ------------------------------------------------------------------


def test_bigqmt_unsupported_methods_raise():
    """无远端 RPC 支持的方法抛 UnsupportedOperation（§3.2 出路 1）。"""
    backend = BigQmtAdapter()
    with pytest.raises(UnsupportedOperation) as exc_info:
        backend.bank_transfer_in("ICBC", "6222", 1000.0)
    assert exc_info.value.backend == "bigqmt"
    assert exc_info.value.method == "bank_transfer_in"
    assert "等待远端" in str(exc_info.value)

    with pytest.raises(UnsupportedOperation):
        backend.query_ipo_data()
    with pytest.raises(UnsupportedOperation):
        backend.smt_query_quoter()
    with pytest.raises(UnsupportedOperation):
        backend.fund_transfer(1, 100.0)
    with pytest.raises(UnsupportedOperation):
        backend.query_secu_account()
    with pytest.raises(UnsupportedOperation):
        backend.export_data("C:/out", "deal")


def test_unsupported_operation_error_message_contains_backend_and_hint():
    """错误信息注明后端与提示，便于调用方理解 503 原因。"""
    err = UnsupportedOperation("query_bank_info", "bigqmt", "银证转账无远端 RPC")
    assert "bigqmt" in str(err)
    assert "query_bank_info" in str(err)
    assert "银证转账无远端 RPC" in str(err)


# ------------------------------------------------------------------
# BigQmtAdapter 逐方法映射（fake xtquant_compat 单例）
# ------------------------------------------------------------------


class _FakeXtTrader:
    """记录调用参数的 BigQmtXtTrader 替身。"""

    def __init__(self, account_id="88800001"):
        self.client = SimpleNamespace(account_id=account_id)
        self.calls = []
        self.registered_callback = None
        self.connected = False

    def register_callback(self, cb):
        self.registered_callback = cb
        self.calls.append(("register_callback", cb))
        return 0

    def start(self):
        self.calls.append(("start",))
        return 0

    def connect(self):
        self.connected = True
        self.calls.append(("connect",))
        return 0

    def subscribe(self, account):
        self.calls.append(("subscribe", account))
        return 0

    def stop(self):
        self.connected = False
        self.calls.append(("stop",))
        return 0

    # ---- 委托 ----
    def order_stock(self, account, code, otype, vol, ptype, price, strat, remark):
        self.calls.append(
            (
                "order_stock",
                account.account_id,
                code,
                otype,
                vol,
                ptype,
                price,
                strat,
                remark,
            )
        )
        return "20260826000001"

    def order_stock_async(self, account, code, otype, vol, ptype, price, strat, remark):
        self.calls.append(("order_stock_async", account.account_id, code, otype, vol))
        return 1

    def cancel_order_stock(self, account, order_id):
        self.calls.append(("cancel_order_stock", account.account_id, order_id))
        return True

    def cancel_order_stock_async(self, account, order_id):
        self.calls.append(("cancel_order_stock_async", account.account_id, order_id))
        return 2

    def cancel_order_stock_sysid(self, account, market, sysid):
        self.calls.append(
            ("cancel_order_stock_sysid", account.account_id, market, sysid)
        )
        return True

    def cancel_order_stock_sysid_async(self, account, market, sysid):
        self.calls.append(
            ("cancel_order_stock_sysid_async", account.account_id, market, sysid)
        )
        return 3

    # ---- 查询 ----
    def query_stock_asset(self, account):
        self.calls.append(("query_stock_asset", account.account_id))
        return SimpleNamespace(account_id=account.account_id, cash=10000.0)

    def query_stock_positions(self, account):
        self.calls.append(("query_stock_positions", account.account_id))
        return [
            SimpleNamespace(stock_code="510300.SH", volume=1000, can_use_volume=1000),
            SimpleNamespace(stock_code="600000.SH", volume=200, can_use_volume=200),
        ]

    def query_stock_orders(self, account, cancelable_only=False):
        self.calls.append(("query_stock_orders", account.account_id, cancelable_only))
        return [
            SimpleNamespace(order_id="111", order_sysid="111", stock_code="510300.SH"),
            SimpleNamespace(order_id="222", order_sysid="222", stock_code="600000.SH"),
        ]

    def query_stock_order(self, account, order_id):
        self.calls.append(("query_stock_order", account.account_id, order_id))
        for o in self.query_stock_orders(account):
            if o.order_id == str(order_id):
                return o
        return None

    def query_stock_trades(self, account):
        self.calls.append(("query_stock_trades", account.account_id))
        return [
            SimpleNamespace(trade_id="T1", traded_id="T1", stock_code="510300.SH"),
            SimpleNamespace(trade_id="T2", traded_id="T2", stock_code="600000.SH"),
        ]

    # ---- 信用 / 账户 ----
    def query_credit_detail(self, account):
        self.calls.append(("query_credit_detail", account.account_id))
        return []

    def query_stk_compacts(self, account):
        return []

    def query_credit_slo_code(self, account):
        return []

    def query_credit_subjects(self, account):
        return []

    def query_credit_assure(self, account):
        return []

    def query_account_status(self, account=None):
        return [SimpleNamespace(account_id=self.client.account_id, status=1)]

    def query_account_infos(self, account=None):
        return [SimpleNamespace(account_id=self.client.account_id, account_type=2)]


def _make_fake_compat(account_id="88800001"):
    """构造注入用的 xtquant_compat 替身（模块级单例语义）。"""
    trader = _FakeXtTrader(account_id)
    state = {"configured": []}

    class _StockAccount:
        def __init__(self, account_id, account_type="STOCK"):
            self.account_id = str(account_id)
            self.account_type = account_type

    compat = SimpleNamespace(
        xt_trader=trader,
        StockAccount=_StockAccount,
        configure=lambda account_id=None, **kw: state["configured"].append(account_id),
        _state=state,
    )
    return compat, trader


def _connected_adapter(account_id="88800001"):
    """返回已连接（fake compat）的 BigQmtAdapter。"""
    compat, trader = _make_fake_compat(account_id)
    adapter = BigQmtAdapter(account_id=account_id, compat=compat)
    adapter.connect()
    return adapter, trader


def test_bigqmt_connect_wires_compat_singletons():
    """connect() 走 configure() 单例 + 注册回调 + start/connect/subscribe。"""
    adapter, trader = _connected_adapter()
    assert trader.connected
    assert trader.registered_callback is not None  # BridgeTraderCallback 已注册
    call_names = [c[0] for c in trader.calls]
    assert "register_callback" in call_names
    assert "connect" in call_names
    assert "subscribe" in call_names
    assert adapter.account_id == "88800001"


def test_bigqmt_order_maps_to_order_stock_rpc():
    """同步下单映射（§4 委托行：submit_order RPC）。"""
    adapter, trader = _connected_adapter()
    result = adapter.order(
        stock_code="510300.SH",
        order_type=23,
        order_volume=100,
        price_type=11,
        price=4.01,
        strategy_name="s1",
        order_remark="r1",
    )
    assert result == "20260826000001"
    call = next(c for c in trader.calls if c[0] == "order_stock")
    assert call[1] == "88800001"  # account_id
    assert call[2:6] == ("510300.SH", 23, 100, 11)


def test_bigqmt_query_methods_map_to_rpc():
    """查询组映射：asset / positions / orders / trades / credit。"""
    adapter, trader = _connected_adapter()
    asset = adapter.query_asset()
    assert asset.cash == 10000.0
    positions = adapter.query_positions()
    assert [p.stock_code for p in positions] == ["510300.SH", "600000.SH"]
    orders = adapter.query_orders(cancelable_only=True)
    assert len(orders) == 2
    assert ("query_stock_orders", "88800001", True) in trader.calls
    assert adapter.query_trades()[0].traded_id == "T1"
    assert adapter.query_credit_detail() == []
    assert adapter.query_stk_compacts() == []


def test_bigqmt_single_item_queries_filter_locally():
    """单笔委托/成交/持仓查询为本地遍历（无需远端支持）。"""
    adapter, _ = _connected_adapter()
    assert adapter.query_single_order(222).stock_code == "600000.SH"
    assert adapter.query_single_order(999) is None
    assert adapter.query_single_trade("T2").stock_code == "600000.SH"
    assert adapter.query_single_position("510300.SH").volume == 1000
    assert adapter.query_single_position("000001.SZ") is None


def test_bigqmt_single_account_semantic_degradation():
    """单实例单账户：请求其他账户告警回落到已配置账户（§5.2）。"""
    adapter, trader = _connected_adapter("88800001")
    adapter.query_asset(account_id="99999999")
    call = next(c for c in trader.calls if c[0] == "query_stock_asset")
    assert call[1] == "88800001"  # 回落到已配置账户


def test_bigqmt_cancel_variants():
    """撤单变体映射（sysid 变体走 cancel_order RPC）。"""
    adapter, trader = _connected_adapter()
    assert adapter.cancel_order(111) is True
    assert ("cancel_order_stock", "88800001", 111) in trader.calls
    assert adapter.cancel_order_stock_sysid("SH", "999") is True
    assert ("cancel_order_stock_sysid", "88800001", "SH", "999") in trader.calls
    assert adapter.cancel_order_async(111) == 2


def test_bigqmt_account_status_local():
    """账户状态（本地判断 + 单账户语义降级）。"""
    adapter, _ = _connected_adapter()
    assert adapter.get_account_status() == {"connected": True}
    infos = adapter.query_account_infos()
    assert len(infos) == 1
    adapter.disconnect()
    assert adapter.get_account_status() == {"connected": False}


# ------------------------------------------------------------------
# XtTraderManager 门面与后端工厂
# ------------------------------------------------------------------


def test_manager_factory_selects_backend():
    """create() 按后端类型构建（bigqmt 无需安装 extra 即可构建，连接时才导入）。"""
    manager = XtTraderManager.create(trader_backend="bigqmt", account_id="1")
    assert manager.backend_name == "bigqmt"
    assert manager.supports("order")
    assert not manager.supports("bank")

    manager_mini = XtTraderManager.create(
        trader_backend="mini", mini_qmt_path="C:/mini", account_id="2"
    )
    assert manager_mini.backend_name == "mini"
    assert manager_mini.supports("bank")


def test_manager_factory_rejects_unknown_backend():
    with pytest.raises(ValueError, match="未知交易后端"):
        XtTraderManager.create(trader_backend="cloud")


def test_manager_forwards_business_methods():
    """门面透传业务方法与私有属性（_callback 供 app.py 注入通知器）。"""
    compat, trader = _make_fake_compat()
    adapter = BigQmtAdapter(account_id="88800001", compat=compat)
    adapter.connect()
    manager = XtTraderManager(adapter)

    assert manager.query_asset().cash == 10000.0
    assert manager._callback is adapter._callback
    assert manager.backend is adapter

    with pytest.raises(UnsupportedOperation):
        manager.query_bank_info()


def test_manager_getattr_missing_raises_attribute_error():
    """未知属性按标准协议抛 AttributeError（copy/pickle 探测安全）。"""
    manager = XtTraderManager.create(trader_backend="mini")
    with pytest.raises(AttributeError):
        _ = manager.no_such_method


# ------------------------------------------------------------------
# 路由层：能力位降级 → 503；支持的路由正常工作
# ------------------------------------------------------------------


@pytest.fixture()
def bigqmt_app(monkeypatch):
    """trading_enabled 的应用 + 手工注入已连接的 bigqmt 后端。

    行情路由在模块顶层 ``from xtquant import xtdata``（Linux 测试机无
    xtquant），注入空 fake xtquant 包使 create_app 可导入；
    不进入 TestClient 上下文（跳过 lifespan），保持手工注入的 manager；
    用例结束恢复全局配置单例，避免污染其他测试。
    """
    import types

    fake_pkg = types.ModuleType("xtquant")
    fake_xtdata = types.ModuleType("xtquant.xtdata")
    fake_pkg.xtdata = fake_xtdata
    # downloader.py 顶层 `from xtquant import xtbson`（失败则回退 `import bson`）：
    # 注入 fake，使路由测试不依赖 bson / 真实 xtquant 安装（hermetic）
    fake_xtbson = types.ModuleType("xtquant.xtbson")

    class _FakeBSON:
        @staticmethod
        def encode(param):
            return b""

    fake_xtbson.BSON = _FakeBSON
    fake_pkg.xtbson = fake_xtbson
    monkeypatch.setitem(sys.modules, "xtquant", fake_pkg)
    monkeypatch.setitem(sys.modules, "xtquant.xtdata", fake_xtdata)
    monkeypatch.setitem(sys.modules, "xtquant.xtbson", fake_xtbson)

    reset_settings(Settings(api_key="test-key", trading_enabled=True))
    from qmt_bridge.server.app import create_app

    app = create_app()
    adapter, _ = _connected_adapter()
    app.state.trader_manager = XtTraderManager(adapter)
    yield app
    reset_settings(None)


def test_bank_route_returns_503_on_bigqmt(bigqmt_app):
    """银证转账无远端 RPC → 503 + 明确错误信息（§3.4 能力位降级）。"""
    client = TestClient(bigqmt_app)
    resp = client.post(
        "/api/bank/transfer_in",
        json={
            "bank_no": "ICBC",
            "bank_account": "6222",
            "balance": 1000.0,
        },
        headers={"X-API-Key": "test-key"},
    )
    assert resp.status_code == 503
    body = resp.json()
    assert body["backend"] == "bigqmt"
    assert body["method"] == "bank_transfer_in"
    assert "等待远端" in body["detail"]


def test_trading_routes_work_on_bigqmt(bigqmt_app):
    """支持的路由（查询/委托组）经门面正常返回。"""
    client = TestClient(bigqmt_app)
    headers = {"X-API-Key": "test-key"}

    resp = client.get("/api/trading/positions", headers=headers)
    assert resp.status_code == 200
    data = resp.json()["data"]
    assert {p["stock_code"] for p in data} == {"510300.SH", "600000.SH"}

    resp = client.post(
        "/api/trading/order",
        json={
            "stock_code": "510300.SH",
            "order_type": 23,
            "order_volume": 100,
            "price_type": 11,
            "price": 4.01,
        },
        headers=headers,
    )
    assert resp.status_code == 200
    assert resp.json()["order_id"] == "20260826000001"


def test_unsupported_operation_handler_shape(bigqmt_app):
    """503 响应体结构统一（detail/backend/method）。"""
    client = TestClient(bigqmt_app)
    resp = client.get("/api/trading/ipo_data", headers={"X-API-Key": "test-key"})
    assert resp.status_code == 503
    assert resp.json()["method"] == "query_ipo_data"


# ------------------------------------------------------------------
# xtquant import shim 检测与安装（docs/big-qmt.md §3.3）
# ------------------------------------------------------------------


def _write_shim_dir(root: Path, real: bool = False) -> Path:
    xtquant = root / "xtquant"
    xtquant.mkdir(parents=True)
    (xtquant / "__init__.py").write_text("", encoding="utf-8")
    if real:
        (xtquant / "xttrader.py").write_text(
            "class XtQuantTraderClient: ...\n", encoding="utf-8"
        )
    else:
        (xtquant / "xttrader.py").write_text(
            "from bigqmt_signal_trader.xtquant_compat import XtQuantTrader\n",
            encoding="utf-8",
        )
    (xtquant / "xtdata.py").write_text(
        "import bigqmt_signal_trader.xtquant_compat as _compat\n",
        encoding="utf-8",
    )
    return xtquant


def test_shim_detection_distinguishes_real_xtquant(tmp_path):
    """特征检测：shim 的 xttrader.py 重导出 compat；真实 xtquant 不是。"""
    from qmt_bridge.server.bigqmt_shim import _looks_like_shim

    shim_dir = _write_shim_dir(tmp_path / "shim", real=False)
    assert _looks_like_shim(shim_dir) is True

    real_dir = _write_shim_dir(tmp_path / "real", real=True)
    assert _looks_like_shim(real_dir) is False
    assert _looks_like_shim(tmp_path / "nonexistent") is False


def test_install_shim_inserts_parent_at_sys_path_front(tmp_path, monkeypatch):
    """显式 shim 目录：父目录插到 sys.path[0]（必须先于真实 xtquant）。"""
    from qmt_bridge.server import bigqmt_shim

    _write_shim_dir(tmp_path, real=False)
    inserted = bigqmt_shim.install_bigqmt_xtdata_shim(str(tmp_path))
    assert inserted == tmp_path
    assert sys.path[0] == str(tmp_path)
    # 幂等
    assert bigqmt_shim.install_bigqmt_xtdata_shim(str(tmp_path)) == tmp_path
    assert sys.path.count(str(tmp_path)) == 1


def test_install_shim_rejects_real_xtquant_dir(tmp_path, monkeypatch):
    """非 shim 目录（真实 xtquant）不安装，返回 None。"""
    from qmt_bridge.server import bigqmt_shim

    monkeypatch.delenv("QMT_BRIDGE_BIGQMT_SHIM_DIR", raising=False)
    _write_shim_dir(tmp_path, real=True)
    assert bigqmt_shim.install_bigqmt_xtdata_shim(str(tmp_path)) is None


def test_install_shim_reads_env_var(tmp_path, monkeypatch):
    """QMT_BRIDGE_BIGQMT_SHIM_DIR 环境变量生效。"""
    from qmt_bridge.server import bigqmt_shim

    _write_shim_dir(tmp_path, real=False)
    monkeypatch.setenv("QMT_BRIDGE_BIGQMT_SHIM_DIR", str(tmp_path))
    assert bigqmt_shim.install_bigqmt_xtdata_shim() == tmp_path
