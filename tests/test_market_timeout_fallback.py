"""行情 RPC 短超时与收盘价兜底测试。

背景（2026-10-07 国庆休市复现）：bigqmt 后端下 QMT 端策略实例不运行时
（休市/节假日），Redis RPC 无消费者，full_tick 等 RPC-only 接口按上游
默认要等 30~120s 才超时；行情路由全是 sync def（anyio 线程池默认 40
线程），这类请求叠加几次即占满线程池，连 /api/meta/health 都无响应。

两组防护的测试：
1. 服务端路由显式传短超时（bigqmt 兼容层支持 timeout_seconds 时才传，
   mini 后端签名不认则过滤掉）；
2. 仪表盘取价 full_tick 失败时回退 ``market_data_ex`` 日 K 收盘价
   （FormulaServer 直连，休市可用）。
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

from qmt_bridge.server.helpers import compat_only_kwargs
from qmt_bridge.server.routers import market as market_router

# dashboard/paper-trading 下的模块按脚本方式组织，测试需手动入路径
_DASHBOARD_DIR = Path(__file__).resolve().parents[1] / "dashboard" / "paper-trading"
if str(_DASHBOARD_DIR) not in sys.path:
    sys.path.insert(0, str(_DASHBOARD_DIR))

import pricing  # noqa: E402


# ── helpers.compat_only_kwargs ──────────────────────────────────


def test_compat_only_kwargs_keeps_supported():
    def fake(stocks, timeout_seconds=None):  # bigqmt 兼容层形态
        return stocks, timeout_seconds

    assert compat_only_kwargs(fake, timeout_seconds=10) == {"timeout_seconds": 10}


def test_compat_only_kwargs_filters_unsupported():
    def fake(stocks):  # 真实 xtdata 形态：不认 timeout_seconds
        return stocks

    assert compat_only_kwargs(fake, timeout_seconds=10) == {}


# ── market 路由：按后端签名传短超时 ─────────────────────────────


class _FakeXtdata:
    """记录调用参数的 xtdata 替身。

    bigqmt / mini 两形态的区别在**函数签名**（compat_only_kwargs 按签名
    过滤）：bigqmt 形态声明 timeout_seconds，mini 形态不声明——若过滤
    失效，mini 形态会直接 TypeError，测试报错而非静默通过。
    """

    def __init__(self, bigqmt: bool):
        self.bigqmt = bigqmt
        self.calls: list[dict] = []
        if bigqmt:
            self.get_full_tick = self._full_tick_timeout
            self.get_market_data_ex = self._mdx_timeout
        else:
            self.get_full_tick = self._full_tick_plain
            self.get_market_data_ex = self._mdx_plain

    # bigqmt 形态（bigqmt_signal_trader.xtquant_compat 的签名）
    def _full_tick_timeout(self, code_list, timeout_seconds=None):
        self.calls.append(
            {
                "func": "get_full_tick",
                "code_list": code_list,
                "timeout_seconds": timeout_seconds,
            }
        )
        return {code: {"lastPrice": 1.0} for code in code_list}

    def _mdx_timeout(
        self, field_list=None, stock_list=None, timeout_seconds=None, **kwargs
    ):
        self.calls.append(
            {
                "func": "get_market_data_ex",
                "stock_list": stock_list,
                "field_list": field_list,
                "timeout_seconds": timeout_seconds,
            }
        )
        return {}

    # mini 形态（真实 xtquant.xtdata 的签名：无 timeout_seconds）
    def _full_tick_plain(self, code_list):
        self.calls.append(
            {"func": "get_full_tick", "code_list": code_list, "timeout_seconds": None}
        )
        return {code: {"lastPrice": 1.0} for code in code_list}

    def _mdx_plain(self, field_list=None, stock_list=None, **kwargs):
        self.calls.append(
            {
                "func": "get_market_data_ex",
                "stock_list": stock_list,
                "field_list": field_list,
                # 过滤生效时该参数根本不会被传入
                "timeout_seconds": kwargs.get("timeout_seconds", "__absent__"),
            }
        )
        return {}


@pytest.fixture
def fake_xtdata(monkeypatch):
    def _make(bigqmt: bool) -> _FakeXtdata:
        fake = _FakeXtdata(bigqmt)
        monkeypatch.setattr(market_router, "xtdata", fake)
        return fake

    return _make


def test_full_tick_passes_timeout_on_bigqmt(fake_xtdata):
    fake = fake_xtdata(bigqmt=True)
    market_router.get_full_tick(stocks="000001.SZ,600000.SH")
    (call,) = fake.calls
    assert call["timeout_seconds"] == market_router.FULL_TICK_TIMEOUT_SECONDS


def test_full_tick_omits_timeout_on_mini(fake_xtdata):
    fake = fake_xtdata(bigqmt=False)
    market_router.get_full_tick(stocks="000001.SZ")
    (call,) = fake.calls
    # mini 后端不认 timeout_seconds，过滤后传 None（不触发 TypeError）
    assert call["timeout_seconds"] is None


def test_market_data_ex_passes_timeout_on_bigqmt(fake_xtdata):
    fake = fake_xtdata(bigqmt=True)
    market_router.get_market_data_ex(stocks="000001.SZ", fields="close")
    (call,) = fake.calls
    assert call["timeout_seconds"] == market_router.BAR_QUERY_TIMEOUT_SECONDS


def test_market_data_ex_default_fields_on_mini(fake_xtdata):
    """mini 后端：默认字段集 + timeout_seconds 未被传入。"""
    fake = fake_xtdata(bigqmt=False)
    market_router.get_market_data_ex(stocks="000001.SZ", fields="")
    (call,) = fake.calls
    assert call["timeout_seconds"] == "__absent__"
    assert "time" in call["field_list"] and "close" in call["field_list"]


# ── pricing.fetch_latest_prices：收盘价兜底 ─────────────────────


def test_fetch_latest_prices_prefers_tick(monkeypatch):
    monkeypatch.setattr(
        pricing,
        "fetch_prices_from_server",
        lambda *a, **k: {"000001.SZ": 11.6},
    )
    prices, source = pricing.fetch_latest_prices("h", 8083, "key", ["000001.SZ"])
    assert source == "tick"
    assert prices == {"000001.SZ": 11.6}


def test_fetch_latest_prices_falls_back_to_close(monkeypatch):
    def _raise(*args, **kwargs):
        raise RuntimeError("获取行情失败: timed out")

    monkeypatch.setattr(pricing, "fetch_prices_from_server", _raise)
    monkeypatch.setattr(
        pricing,
        "_fetch_recent_closes_per_code",
        lambda *a, **k: {
            "000001.SZ": {"20260929": 11.35, "20260930": 11.57},
            "600000.SH": {},
        },
    )
    prices, source = pricing.fetch_latest_prices(
        "h", 8083, "key", ["000001.SZ", "600000.SH"]
    )
    assert source == "close"
    # 取最近交易日（YYYYMMDD 字典序最大）收盘价；空序列代码被剔除
    assert prices == {"000001.SZ": 11.57}


def test_fetch_latest_prices_empty_tick_triggers_fallback(monkeypatch):
    """full_tick 成功但一只价都没拿到（如全部超时丢弃）也走兜底。"""
    monkeypatch.setattr(pricing, "fetch_prices_from_server", lambda *a, **k: {})
    called = {}

    def _closes(host, port, key, codes, start_time, end_time):
        called["codes"] = list(codes)
        return {"000001.SZ": {"20260930": 11.57}}

    monkeypatch.setattr(pricing, "_fetch_recent_closes_per_code", _closes)
    prices, source = pricing.fetch_latest_prices("h", 8083, "key", ["000001.SZ"])
    assert source == "close"
    assert prices == {"000001.SZ": 11.57}
    assert called["codes"] == ["000001.SZ"]
