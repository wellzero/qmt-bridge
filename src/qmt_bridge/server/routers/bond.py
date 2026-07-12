"""债券路由模块 /api/bond/*。

提供债券（不含可转债）的列表查询和详情查询等端点。
底层调用 xtquant.xtdata 的相关接口：
- xtdata.get_stock_list_in_sector()  — 通过板块获取债券列表
- xtdata.get_instrument_detail()     — 获取债券合约详情
"""

import logging

from fastapi import APIRouter, Query
from xtquant import xtdata

from ..helpers import _numpy_to_python

logger = logging.getLogger("qmt_bridge")
router = APIRouter(prefix="/api/bond", tags=["bond"])

# 债券板块名称（按优先级尝试）
BOND_SECTORS = ["沪深债券", "沪市债券", "深市债券"]


@router.get("/list")
def get_bond_list():
    """获取沪深债券代码列表（不含可转债）。

    依次尝试多个板块名称，合并去重后返回。

    Returns:
        count: 债券数量。
        stocks: 债券代码列表。

    底层调用: xtdata.get_stock_list_in_sector()
    """
    stock_set = set()
    failures: list[tuple[str, str]] = []
    for sector in BOND_SECTORS:
        try:
            result = xtdata.get_stock_list_in_sector(sector)
            if result:
                stock_set.update(result)
        except Exception as exc:
            failures.append((sector, repr(exc)))
            logger.warning("get_stock_list_in_sector(%r) 失败: %r", sector, exc)
            continue

    if not stock_set:
        logger.warning(
            "所有债券板块均未返回数据，已尝试: %s；失败详情: %s",
            BOND_SECTORS,
            failures,
        )

    stock_list = sorted(stock_set)
    return {"count": len(stock_list), "stocks": stock_list}


@router.get("/detail")
def get_bond_detail(
    stock: str = Query(..., description="债券代码，如 019733.SH"),
):
    """获取债券合约详情。

    Args:
        stock: 债券代码。

    Returns:
        stock: 债券代码。
        data: 债券合约详情（含名称、上市日期、到期日等）。

    底层调用: xtdata.get_instrument_detail(stock)
    """
    raw = xtdata.get_instrument_detail(stock)
    return {"stock": stock, "data": _numpy_to_python(raw)}
