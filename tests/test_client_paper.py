"""客户端 paper 路由切换单元测试。"""

from unittest.mock import MagicMock, patch

import pytest

from qmt_bridge import QMTClient


@pytest.fixture
def client():
    """提供非 paper 客户端实例。"""
    return QMTClient(host="localhost", port=8083, api_key="test-key")


@pytest.fixture
def paper_client():
    """提供 paper 客户端实例。"""
    return QMTClient(host="localhost", port=8083, api_key="test-key", paper=True)


def test_trading_prefix_default(client: QMTClient):
    """默认情况下使用真实交易前缀。"""
    assert client._trading_prefix == "/api/trading"


def test_trading_prefix_paper(paper_client: QMTClient):
    """paper=True 时使用模拟交易前缀。"""
    assert paper_client._trading_prefix == "/api/paper_trading"


def test_place_order_uses_paper_endpoint(paper_client: QMTClient):
    """paper 客户端下单应请求 /api/paper_trading/order。"""
    expected_url = "http://localhost:8083/api/paper_trading/order"
    mock_response = {"order_id": 123, "status": "submitted"}

    with patch.object(paper_client, "_opener") as mock_opener:
        mock_resp = MagicMock()
        mock_resp.read.return_value = (
            '{"order_id": 123, "status": "submitted"}'.encode()
        )
        mock_opener.open.return_value.__enter__ = MagicMock(
            return_value=mock_resp
        )
        mock_opener.open.return_value.__exit__ = MagicMock(
            return_value=False
        )

        result = paper_client.place_order(
            stock_code="000001.SZ",
            order_type=23,
            order_volume=100,
            price_type=11,
            price=10.0,
            account_id="paper_acc",
        )

        call_args = mock_opener.open.call_args
        assert call_args is not None
        req = call_args[0][0]
        assert req.full_url == expected_url
        assert result == mock_response


def test_query_orders_uses_paper_endpoint(paper_client: QMTClient):
    """paper 客户端查询委托应请求 /api/paper_trading/orders。"""
    expected_url = "http://localhost:8083/api/paper_trading/orders?account_id=paper_acc&cancelable_only=False"

    with patch.object(paper_client, "_opener") as mock_opener:
        mock_resp = MagicMock()
        mock_resp.read.return_value = '{"data": []}'.encode()
        mock_opener.open.return_value.__enter__ = MagicMock(
            return_value=mock_resp
        )
        mock_opener.open.return_value.__exit__ = MagicMock(
            return_value=False
        )

        paper_client.query_orders(account_id="paper_acc")

        call_args = mock_opener.open.call_args
        assert call_args is not None
        req = call_args[0][0]
        assert req.full_url == expected_url
