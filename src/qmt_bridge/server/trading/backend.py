"""交易后端抽象 ── TraderBackend 协议与能力位定义。

按 ``docs/big-qmt.md`` §3.2/§3.4，交易层拆分为可并列的双后端：

.. code-block:: text

    TraderBackend (protocol, 覆盖 manager.py 全部方法签名)
      ├─ MiniQmtBackend  # 现有 XtQuantTrader 实现（mini_backend.py）
      └─ BigQmtAdapter   # 新增，基于 xtquant_big_convert（bigqmt_backend.py）

对外仍以 qmt-bridge REST API 为准（策略零改动）；后端声明 ``SUPPORTED_CAPABILITIES``
能力位，无对应能力的路由返回 503（带明确错误信息）而非崩溃。
能力位分组与 ``docs/big-qmt.md`` §4 映射表（远端支持需求清单）一一对应。
"""

from typing import Protocol, runtime_checkable


class UnsupportedOperation(Exception):
    """当前交易后端不支持该操作（能力位降级）。

    由 FastAPI 异常处理器转换为 503 响应，错误信息注明等待远端支持
    （见 ``docs/big-qmt.md`` §3.2 三条出路：能力位降级 / 推动上游 / 自行扩展）。
    """

    def __init__(self, method: str, backend: str, hint: str = ""):
        self.method = method
        self.backend = backend
        self.hint = hint
        super().__init__(
            f"交易后端 {backend!r} 不支持 {method}()（等待远端 xtquant_big_convert 支持）"
            + (f"：{hint}" if hint else "")
        )


# ------------------------------------------------------------------
# 能力位分组：组名 → XtTraderManager/后端的方法名集合。
# 与 docs/big-qmt.md §4 的「qmt-bridge API 组」行一一对应；
# 测试据此断言双后端的能力矩阵不回退。
# ------------------------------------------------------------------

CAPABILITY_METHODS: dict[str, frozenset[str]] = {
    # §4「委托」：submit_order / cancel_order RPC（sysid 变体需实测）
    "order": frozenset(
        {
            "order",
            "order_async",
            "cancel_order",
            "cancel_order_async",
            "cancel_order_stock_sysid",
            "cancel_order_stock_sysid_async",
            "credit_order",
        }
    ),
    # §4「查询」：get_asset / get_positions / query_orders / query_trades
    # （单笔委托/成交/持仓查询为服务端本地遍历，无需远端支持）
    "query": frozenset(
        {
            "query_orders",
            "query_positions",
            "query_asset",
            "query_trades",
            "query_order_detail",
            "query_single_order",
            "query_single_trade",
            "query_single_position",
            "query_credit_positions",
        }
    ),
    # §4「信用（两融）」：margin 系列方法，需两融权限，否则返回空
    "credit": frozenset(
        {
            "query_credit_detail",
            "query_stk_compacts",
            "query_credit_slo_code",
            "query_credit_subjects",
            "query_credit_assure",
        }
    ),
    # §4「账户」：单账户模型，语义降级（单实例单账户）
    "account": frozenset(
        {
            "get_account_status",
            "query_account_status",
            "query_account_infos",
        }
    ),
    # 证券子账户查询无远端对应（单账户模型），独立成组便于降级
    "secu_account": frozenset({"query_secu_account"}),
    # §4「银证转账」：无远端 RPC → 降级 503 / 推动上游
    "bank": frozenset(
        {
            "bank_transfer_in",
            "bank_transfer_out",
            "bank_transfer_in_async",
            "bank_transfer_out_async",
            "query_bank_info",
            "query_bank_amount",
            "query_bank_transfer_stream",
        }
    ),
    # §4「资金/证券划转」：无 → 降级 503
    "transfer": frozenset({"fund_transfer", "secu_transfer"}),
    # §4「CTP 划转」：无 → 降级 503
    "ctp": frozenset(
        {"ctp_transfer_option_to_future", "ctp_transfer_future_to_option"}
    ),
    # §4「SMT 约定式交易」：无 → 降级 503
    "smt": frozenset(
        {
            "smt_query_quoter",
            "smt_query_compact",
            "smt_query_order",
            "smt_negotiate_order_async",
            "smt_appointment_order_async",
            "smt_appointment_cancel_async",
            "smt_compact_renewal_async",
            "smt_compact_return_async",
        }
    ),
    # §4「IPO 打新」：上游明示不能无损替换 → 降级 503
    "ipo": frozenset({"query_ipo_data", "query_new_purchase_limit"}),
    # §4「COM 期权/期货」：完整版 QMT 为证券客户端 → 降级 503
    "com": frozenset({"query_com_fund", "query_com_position"}),
    # §4「数据导出」：get_history_trade_detail_data 仅可部分替代成交导出 → 降级 503
    "export": frozenset(
        {"export_data", "query_data", "sync_transaction_from_external"}
    ),
}


def capability_of(method_name: str) -> str | None:
    """返回方法名所属的能力位分组；不属于任何受控分组时返回 None。"""
    for group, methods in CAPABILITY_METHODS.items():
        if method_name in methods:
            return group
    return None


@runtime_checkable
class TraderBackend(Protocol):
    """交易后端协议 ── 覆盖 XtTraderManager 全部公开方法签名。

    两个实现（MiniQmtBackend / BigQmtAdapter）都必须实现本协议的完整方法集，
    不支持的方法直接抛 ``UnsupportedOperation`` —— 单一事实来源，
    路由层无需感知后端差异。

    Attributes:
        name: 后端标识（"mini" / "bigqmt"），用于日志与错误信息。
        SUPPORTED_CAPABILITIES: 声明支持的能力位分组集合。
    """

    name: str
    SUPPORTED_CAPABILITIES: frozenset[str]

    # ---- 生命周期 ----
    def connect(self) -> None: ...

    def disconnect(self) -> None: ...

    # ---- 能力位 ----
    def supports(self, capability: str) -> bool:
        """判断后端是否声明支持某能力位分组。"""
        ...

    # 其余方法签名（委托/查询/信用/账户/银证/划转/CTP/SMT/IPO/COM/导出）
    # 与 XtTraderManager 完全一致，见 mini_backend.py / bigqmt_backend.py
    # 的具体实现；协议层面以运行时方法全集断言（tests/test_trading_backends.py）
    # 保证双后端不漏方法，避免三处重复维护同一套签名。
