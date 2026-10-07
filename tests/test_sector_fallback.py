"""板块列表端点 /api/sector/list 的后端差异测试。

bigqmt 兼容层的 ``get_sector_list(allow_fallback=False)`` 对枚举终端板块
直接抛 ``RpcServerRepliedError``（上游 issue #143），路由曾原样 500 ——
远程「列板块 → 取成分股 → 下载 K 线」流程第一步就断（2026-09-21 实测
192.168.0.107 三连 500）。路由按签名检测兼容层并显式取降级名单，
source 字段如实标注来源；mini 后端行为不变。
"""

from __future__ import annotations

import sys
from types import SimpleNamespace

import pytest
from fastapi.testclient import TestClient

from qmt_bridge.server.config import Settings, reset_settings

FALLBACK_SECTORS = ["沪深A股", "沪深ETF", "沪深指数"]


@pytest.fixture()
def app_no_lifespan(monkeypatch):
    """可导入的 app（fake xtquant 包）+ 不进 lifespan，用例后恢复单例。"""
    import types

    fake_pkg = types.ModuleType("xtquant")
    fake_xtdata = types.ModuleType("xtquant.xtdata")
    fake_pkg.xtdata = fake_xtdata
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

    reset_settings(Settings())
    from qmt_bridge.server.app import create_app

    yield create_app()
    reset_settings(None)


def _patch_sector_xtdata(monkeypatch, fake):
    from qmt_bridge.server.routers import sector

    monkeypatch.setattr(sector, "xtdata", fake)


def test_sector_list_mini_backend_uses_terminal(app_no_lifespan, monkeypatch):
    """mini 后端：无 allow_fallback 形参 → 原样调用，source=terminal。"""
    calls = []

    def _get_sector_list():
        calls.append("terminal")
        return ["我的自选板块", "沪深A股"]

    _patch_sector_xtdata(
        monkeypatch, SimpleNamespace(get_sector_list=_get_sector_list)
    )
    resp = TestClient(app_no_lifespan).get("/api/sector/list")
    assert resp.status_code == 200
    body = resp.json()
    assert body == {"sectors": ["我的自选板块", "沪深A股"], "source": "terminal"}
    assert calls == ["terminal"]


def test_sector_list_bigqmt_falls_back_instead_of_500(app_no_lifespan, monkeypatch):
    """bigqmt 后端：兼容层签名 → 显式 allow_fallback=True，不再 500。"""

    def _get_sector_list(allow_fallback=False):
        if not allow_fallback:
            # 复刻 RpcServerRepliedError(RuntimeError 子类) 的行为
            raise RuntimeError(
                "NotImplementedError: get_sector_list cannot enumerate ..."
            )
        return FALLBACK_SECTORS

    _patch_sector_xtdata(
        monkeypatch, SimpleNamespace(get_sector_list=_get_sector_list)
    )
    resp = TestClient(app_no_lifespan).get("/api/sector/list")
    assert resp.status_code == 200
    body = resp.json()
    assert body == {"sectors": FALLBACK_SECTORS, "source": "fallback"}
