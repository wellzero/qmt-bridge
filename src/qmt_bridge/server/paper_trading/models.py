"""模拟交易数据对象模块。

本模块提供与 ``xtquant.xttype`` 中数据对象属性对齐的轻量实现，
使 ``PaperQuantTrader`` 的查询与回调返回值可直接被现有路由/回调序列化代码处理。

所有类均为纯 Python 对象，不依赖 xtquant C 扩展，便于在非 Windows 环境测试。
"""

from __future__ import annotations


# 账户类型常量（与 xtconstant 对齐，避免模块级导入 xtquant）
SECURITY_ACCOUNT = 2
CREDIT_ACCOUNT = 3


class XtAsset:
    """模拟股票账户资金结构。"""

    def __init__(
        self,
        account_id: str,
        cash: float,
        frozen_cash: float,
        market_value: float,
        total_asset: float,
        fetch_balance: float,
    ):
        self.account_type = SECURITY_ACCOUNT
        self.account_id = account_id
        self.cash = cash
        self.frozen_cash = frozen_cash
        self.market_value = market_value
        self.total_asset = total_asset
        self.fetch_balance = fetch_balance


class XtOrder:
    """模拟股票委托结构。"""

    def __init__(
        self,
        account_id: str,
        stock_code: str,
        order_id: int,
        order_sysid: str,
        order_time: str,
        order_type: int,
        order_volume: int,
        price_type: int,
        price: float,
        traded_volume: int,
        traded_price: float,
        order_status: int,
        status_msg: str,
        strategy_name: str,
        order_remark: str,
        direction: int = 0,
        offset_flag: int = 0,
        secu_account: str = "",
        instrument_name: str = "",
    ):
        self.account_type = SECURITY_ACCOUNT
        self.account_id = account_id
        self.stock_code = stock_code
        self.order_id = order_id
        self.order_sysid = order_sysid
        self.order_time = order_time
        self.order_type = order_type
        self.order_volume = order_volume
        self.price_type = price_type
        self.price = price
        self.traded_volume = traded_volume
        self.traded_price = traded_price
        self.order_status = order_status
        self.status_msg = status_msg
        self.strategy_name = strategy_name
        self.order_remark = order_remark
        self.direction = direction
        self.offset_flag = offset_flag
        self.secu_account = secu_account
        self.instrument_name = instrument_name


class XtTrade:
    """模拟股票成交结构。"""

    def __init__(
        self,
        account_id: str,
        stock_code: str,
        order_type: int,
        traded_id: int,
        traded_time: str,
        traded_price: float,
        traded_volume: int,
        traded_amount: float,
        order_id: int,
        order_sysid: str,
        strategy_name: str,
        order_remark: str,
        direction: int = 0,
        offset_flag: int = 0,
        commission: float = 0.0,
        secu_account: str = "",
        instrument_name: str = "",
    ):
        self.account_type = SECURITY_ACCOUNT
        self.account_id = account_id
        self.order_type = order_type
        self.stock_code = stock_code
        self.traded_id = traded_id
        self.traded_time = traded_time
        self.traded_price = traded_price
        self.traded_volume = traded_volume
        self.traded_amount = traded_amount
        self.order_id = order_id
        self.order_sysid = order_sysid
        self.strategy_name = strategy_name
        self.order_remark = order_remark
        self.direction = direction
        self.offset_flag = offset_flag
        self.commission = commission
        self.secu_account = secu_account
        self.instrument_name = instrument_name


class XtPosition:
    """模拟股票持仓结构。"""

    def __init__(
        self,
        account_id: str,
        stock_code: str,
        volume: int,
        can_use_volume: int,
        open_price: float,
        market_value: float,
        frozen_volume: int,
        on_road_volume: int,
        yesterday_volume: int,
        avg_price: float,
        direction: int,
        last_price: float,
        profit_rate: float,
        secu_account: str = "",
        instrument_name: str = "",
    ):
        self.account_type = SECURITY_ACCOUNT
        self.account_id = account_id
        self.stock_code = stock_code
        self.volume = volume
        self.can_use_volume = can_use_volume
        self.open_price = open_price
        self.market_value = market_value
        self.frozen_volume = frozen_volume
        self.on_road_volume = on_road_volume
        self.yesterday_volume = yesterday_volume
        self.avg_price = avg_price
        self.direction = direction
        self.last_price = last_price
        self.profit_rate = profit_rate
        self.secu_account = secu_account
        self.instrument_name = instrument_name


class XtOrderError:
    """模拟委托失败结构。"""

    def __init__(
        self,
        account_id: str,
        order_id: int,
        error_id: int | None = None,
        error_msg: str | None = None,
        strategy_name: str | None = None,
        order_remark: str | None = None,
    ):
        self.account_type = SECURITY_ACCOUNT
        self.account_id = account_id
        self.order_id = order_id
        self.error_id = error_id
        self.error_msg = error_msg
        self.strategy_name = strategy_name
        self.order_remark = order_remark


class XtCancelError:
    """模拟撤单失败结构。"""

    def __init__(
        self,
        account_id: str,
        order_id: int,
        market: str,
        order_sysid: str,
        error_id: int | None = None,
        error_msg: str | None = None,
    ):
        self.account_type = SECURITY_ACCOUNT
        self.account_id = account_id
        self.order_id = order_id
        self.market = market
        self.order_sysid = order_sysid
        self.error_id = error_id
        self.error_msg = error_msg


class XtOrderResponse:
    """模拟异步下单反馈结构。"""

    def __init__(
        self,
        account_id: str,
        order_id: int,
        strategy_name: str,
        order_remark: str,
        error_msg: str,
        seq: int,
    ):
        self.account_type = SECURITY_ACCOUNT
        self.account_id = account_id
        self.order_id = order_id
        self.strategy_name = strategy_name
        self.order_remark = order_remark
        self.error_msg = error_msg
        self.seq = seq


class XtCancelOrderResponse:
    """模拟异步撤单反馈结构。"""

    def __init__(
        self,
        account_id: str,
        cancel_result: int,
        order_id: int,
        order_sysid: str,
        seq: int,
        error_msg: str,
    ):
        self.account_type = SECURITY_ACCOUNT
        self.account_id = account_id
        self.cancel_result = cancel_result
        self.order_id = order_id
        self.order_sysid = order_sysid
        self.seq = seq
        self.error_msg = error_msg


class XtAccountStatus:
    """模拟账号状态结构。"""

    def __init__(self, account_id: str, account_type: int, status: int):
        self.account_type = account_type
        self.account_id = account_id
        self.status = status


class XtSmtAppointmentResponse:
    """模拟约券异步反馈结构。"""

    def __init__(self, seq: int, success: bool, msg: str, apply_id: str):
        self.seq = seq
        self.success = success
        self.msg = msg
        self.apply_id = apply_id


class XtBankTransferResponse:
    """模拟银证转账异步反馈结构。"""

    def __init__(self, seq: int, success: bool, msg: str):
        self.seq = seq
        self.success = success
        self.msg = msg


class XtSmartAlgoOrderResponse:
    """模拟智能算法任务下单反馈结构。"""

    def __init__(
        self,
        account_id: str,
        task_id: str,
        strategy_name: str,
        order_remark: str,
        error_msg: str,
        seq: int,
    ):
        self.account_id = account_id
        self.task_id = task_id
        self.strategy_name = strategy_name
        self.order_remark = order_remark
        self.error_msg = error_msg
        self.seq = seq


class XtOperateSmartTaskResponse:
    """模拟智能算法任务操作反馈结构。"""

    def __init__(
        self,
        seq: int,
        success: bool,
        task_id: str,
        operate_reason: str,
        error_msg: str,
    ):
        self.seq = seq
        self.success = success
        self.task_id = task_id
        self.operate_reason = operate_reason
        self.error_msg = error_msg
