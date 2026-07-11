"""模拟交易 REST 路由模块。

提供两类端点：
1. ``/api/paper_accounts/*`` —— 账户配置管理（初始资金、费率、滑点等）
2. ``/api/paper_trading/*`` —— 与真实交易路由形态一致的模拟交易操作
"""

from __future__ import annotations

from fastapi import APIRouter, Depends, Query

from ..deps import get_paper_trader_manager
from ..helpers import _numpy_to_python, ok_response
from ..models import CancelRequest, OrderRequest
from ..security import require_api_key
from .config import DownloadPricesRequest, PaperAccountConfig
from .manager import PaperTraderManager

router = APIRouter(
    prefix="/api",
    tags=["paper_trading"],
    dependencies=[Depends(require_api_key)],
)


# ── 账户配置管理 ──


@router.get("/paper_accounts")
def list_paper_accounts(
    manager: PaperTraderManager = Depends(get_paper_trader_manager),
):
    """列出所有模拟账户配置。"""
    configs = manager.list_account_configs()
    return {"data": [_numpy_to_python(cfg.model_dump()) for cfg in configs]}


@router.get("/paper_accounts/{account_id}")
def get_paper_account(
    account_id: str,
    manager: PaperTraderManager = Depends(get_paper_trader_manager),
):
    """查询单个模拟账户配置。"""
    config = manager.get_account_config(account_id)
    if config is None:
        return ok_response(None)
    return ok_response(_numpy_to_python(config.model_dump()))


@router.post("/paper_accounts")
def create_or_update_paper_account(
    req: PaperAccountConfig,
    manager: PaperTraderManager = Depends(get_paper_trader_manager),
):
    """创建或更新模拟账户配置。"""
    config = manager.create_or_update_account(req)
    return ok_response(_numpy_to_python(config.model_dump()))


@router.delete("/paper_accounts/{account_id}")
def delete_paper_account(
    account_id: str,
    manager: PaperTraderManager = Depends(get_paper_trader_manager),
):
    """删除模拟账户及其数据。"""
    result = manager.delete_account(account_id)
    return ok_response(result)


@router.post("/paper_accounts/{account_id}/reset")
def reset_paper_account(
    account_id: str,
    manager: PaperTraderManager = Depends(get_paper_trader_manager),
):
    """重置模拟账户（清空持仓、委托、成交，资金恢复初始值）。"""
    result = manager.reset_account(account_id)
    return ok_response(result)


@router.post("/paper_accounts/{account_id}/download_prices")
def download_paper_account_prices(
    account_id: str,
    req: DownloadPricesRequest,
    manager: PaperTraderManager = Depends(get_paper_trader_manager),
):
    """从 xtquant 下载指定股票的最新价，并更新账户静态价格配置。"""
    downloaded = manager.download_prices(account_id, req.stock_codes)
    return ok_response(_numpy_to_python(downloaded))


# ── 模拟交易操作 ──


@router.post("/paper_trading/order")
def paper_place_order(
    req: OrderRequest,
    manager: PaperTraderManager = Depends(get_paper_trader_manager),
):
    """模拟同步下单。"""
    result = manager.order(
        stock_code=req.stock_code,
        order_type=req.order_type,
        order_volume=req.order_volume,
        price_type=req.price_type,
        price=req.price,
        strategy_name=req.strategy_name,
        order_remark=req.order_remark,
        account_id=req.account_id,
    )
    return {"order_id": result, "status": "submitted"}


@router.post("/paper_trading/cancel")
def paper_cancel_order(
    req: CancelRequest,
    manager: PaperTraderManager = Depends(get_paper_trader_manager),
):
    """模拟同步撤单。"""
    result = manager.cancel_order(order_id=req.order_id, account_id=req.account_id)
    return {"status": "ok", "data": _numpy_to_python(result)}


@router.get("/paper_trading/orders")
def paper_query_orders(
    account_id: str = Query("", description="模拟账户 ID"),
    cancelable_only: bool = Query(False, description="仅返回可撤委托"),
    manager: PaperTraderManager = Depends(get_paper_trader_manager),
):
    """查询模拟账户当日委托。"""
    result = manager.query_orders(
        account_id=account_id, cancelable_only=cancelable_only
    )
    return {"data": _numpy_to_python(result)}


@router.get("/paper_trading/positions")
def paper_query_positions(
    account_id: str = Query("", description="模拟账户 ID"),
    manager: PaperTraderManager = Depends(get_paper_trader_manager),
):
    """查询模拟账户当前持仓。"""
    result = manager.query_positions(account_id=account_id)
    return {"data": _numpy_to_python(result)}


@router.get("/paper_trading/asset")
def paper_query_asset(
    account_id: str = Query("", description="模拟账户 ID"),
    manager: PaperTraderManager = Depends(get_paper_trader_manager),
):
    """查询模拟账户资产。"""
    result = manager.query_asset(account_id=account_id)
    return {"data": _numpy_to_python(result)}


@router.get("/paper_trading/trades")
def paper_query_trades(
    account_id: str = Query("", description="模拟账户 ID"),
    manager: PaperTraderManager = Depends(get_paper_trader_manager),
):
    """查询模拟账户当日成交。"""
    result = manager.query_trades(account_id=account_id)
    return {"data": _numpy_to_python(result)}


@router.get("/paper_trading/order_detail")
def paper_query_order_detail(
    order_id: int = Query(0, description="委托编号"),
    account_id: str = Query("", description="模拟账户 ID"),
    manager: PaperTraderManager = Depends(get_paper_trader_manager),
):
    """查询模拟账户单笔委托详情。"""
    result = manager.query_order_detail(order_id=order_id, account_id=account_id)
    return {"data": _numpy_to_python(result)}


@router.post("/paper_trading/batch_order")
def paper_batch_order(
    orders: list[OrderRequest],
    manager: PaperTraderManager = Depends(get_paper_trader_manager),
):
    """模拟批量下单。"""
    results = []
    for req in orders:
        order_id = manager.order(
            stock_code=req.stock_code,
            order_type=req.order_type,
            order_volume=req.order_volume,
            price_type=req.price_type,
            price=req.price,
            strategy_name=req.strategy_name,
            order_remark=req.order_remark,
            account_id=req.account_id,
        )
        results.append({"stock_code": req.stock_code, "order_id": order_id})
    return {"data": results}


@router.post("/paper_trading/batch_cancel")
def paper_batch_cancel(
    cancel_requests: list[CancelRequest],
    manager: PaperTraderManager = Depends(get_paper_trader_manager),
):
    """模拟批量撤单。"""
    results = []
    for req in cancel_requests:
        result = manager.cancel_order(order_id=req.order_id, account_id=req.account_id)
        results.append({"order_id": req.order_id, "result": _numpy_to_python(result)})
    return {"data": results}


@router.get("/paper_trading/account_status")
def paper_get_account_status(
    account_id: str = Query("", description="模拟账户 ID"),
    manager: PaperTraderManager = Depends(get_paper_trader_manager),
):
    """获取模拟账户连接状态。"""
    result = manager.get_account_status(account_id=account_id)
    return {"data": _numpy_to_python(result)}


@router.get("/paper_trading/account_infos")
def paper_query_account_infos(
    manager: PaperTraderManager = Depends(get_paper_trader_manager),
):
    """查询所有已注册模拟账户信息。"""
    result = manager.query_account_infos()
    return {"data": _numpy_to_python(result)}


# ── 业绩汇总 ──


@router.get("/paper_trading/summary")
def paper_get_summary(
    account_id: str = Query("", description="模拟账户 ID"),
    manager: PaperTraderManager = Depends(get_paper_trader_manager),
):
    """查询单个模拟账户业绩摘要。"""
    result = manager.get_summary(account_id)
    return ok_response(_numpy_to_python(result.to_dict() if result else None))


@router.get("/paper_trading/summaries")
def paper_get_all_summaries(
    manager: PaperTraderManager = Depends(get_paper_trader_manager),
):
    """查询所有模拟账户业绩摘要。"""
    summaries = manager.get_all_summaries()
    return ok_response(
        {aid: _numpy_to_python(s.to_dict()) for aid, s in summaries.items()}
    )
