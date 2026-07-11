"""模拟交易回调基类模块。

与 ``xtquant.xttrader.XtQuantTraderCallback`` 对齐，
用户可继承 ``PaperTraderCallback`` 并在 ``PaperQuantTrader`` 中注册，
以接收模拟账户的连接、委托、成交、持仓、资产等推送事件。
"""

from .models import (
    XtAccountStatus,
    XtAsset,
    XtBankTransferResponse,
    XtCancelError,
    XtCancelOrderResponse,
    XtOperateSmartTaskResponse,
    XtOrder,
    XtOrderError,
    XtOrderResponse,
    XtPosition,
    XtSmtAppointmentResponse,
    XtSmartAlgoOrderResponse,
    XtTrade,
)


class PaperTraderCallback:
    """模拟交易回调基类。

    所有方法均为空实现，子类按需覆盖即可。
    """

    def on_connected(self):
        """连接建立回调。"""

    def on_disconnected(self):
        """连接断开回调。"""

    def on_account_status(self, status: XtAccountStatus):
        """账户状态变更回调。"""

    def on_stock_asset(self, asset: XtAsset):
        """账户资产推送回调。"""

    def on_stock_order(self, order: XtOrder):
        """委托状态推送回调。"""

    def on_stock_trade(self, trade: XtTrade):
        """成交推送回调。"""

    def on_stock_position(self, position: XtPosition):
        """持仓推送回调。"""

    def on_order_error(self, order_error: XtOrderError):
        """委托失败回调。"""

    def on_cancel_error(self, cancel_error: XtCancelError):
        """撤单失败回调。"""

    def on_order_stock_async_response(self, response: XtOrderResponse):
        """异步下单响应回调。"""

    def on_cancel_order_stock_async_response(self, response: XtCancelOrderResponse):
        """异步撤单响应回调。"""

    def on_smt_appointment_async_response(self, response: XtSmtAppointmentResponse):
        """约券预约异步响应回调。"""

    def on_bank_transfer_async_response(self, response: XtBankTransferResponse):
        """银证转账异步响应回调。"""

    def on_ctp_internal_transfer_async_response(self, response: XtBankTransferResponse):
        """CTP 内部划转异步响应回调。"""

    def on_smart_algo_order_async_response(self, response: XtSmartAlgoOrderResponse):
        """智能算法任务下单异步响应回调。"""

    def on_operate_smart_task_async_response(
        self, response: XtOperateSmartTaskResponse
    ):
        """智能算法任务操作异步响应回调。"""
