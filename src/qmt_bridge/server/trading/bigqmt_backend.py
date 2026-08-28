"""BigQmtAdapter ── 基于 xtquant_big_convert 的交易后端（bigqmt 模式）。

本类运行于 Windows qmt-server 进程内，把 qmt-bridge 每个交易接口翻译成
``xtquant_big_convert`` 客户端的 Redis/ZMQ RPC 调用，由完整版 QMT 内置
Python 沙箱中的服务端（``passorder`` / ``get_trade_detail_data``）执行。

与完整版 QMT 的通信全景（读走通道A直连，写走通道B排队；行前缀对齐仅在
等宽字体下成立）::

    qmt-server 进程                       完整版 QMT 客户端（XtItClient.exe）
    ──────────────                       ────────────────────────────────
    BigQmtAdapter                         ① C++ 行情服务 FormulaServer（TCP 58600）
      └ BigQmtRpcClient ──通道A(读)──►      白名单 10 方法毫秒级代答，不经沙箱、
        （与行情通道同一客户端）             无需策略实例运行，休市可用
                                          ② 策略沙箱（内置 Python 3.6）
    BigQmtRpcClient ──通道B(写)──┐          bigqmt_rpc_bootstrap 模型交易实例
      LPUSH 请求                  │          （须"运行中"——休市时 QMT 引擎不驱动）
      bigqmt:rpc:queue:<账号> ◄───┘          init 启动 RPC 服务端；adjust 每 500ms
        （Redis db5，RESP2）                  排空队列 → passorder / cancel /
                                               get_trade_detail_data 执行
                                            回写 bigqmt:rpc:resp:<账号>:<req_id>
    （阻塞等该键，默认 6s 超时）◄──────────    （TTL 60s）

    通道A/B 共用一个 BigQmtRpcClient：每次 call 先试 FormulaServer 能否代答
    （method 白名单 + 参数可翻译，如 get_market_data_ex 需显式字段集），
    不行才落通道B。账号两端必须一致，否则请求进 A 队列、服务端听 B 队列
    （表现为 ping 超时）；下单另受 QMT 侧 rpc_allow_order_methods 门控。

关键约定（``docs/big-qmt.md`` §3.2/§4）：

- **映射方向**：对外以 qmt-bridge REST API 为准，策略零改动；
  §4 映射表即远端支持需求清单。
- **能力位降级**：上游 v0.2.9 未覆盖的方法（银证转账 / 划转 / CTP / SMT /
  IPO / COM / 数据导出 / 证券子账户）抛 ``UnsupportedOperation`` → 路由 503，
  错误信息注明"等待远端支持"。
- **单实例单账户**：一个完整版 QMT 客户端 = 一个 live 账户；
  请求其他账户时告警并回落到已配置账户（语义降级）。
- **实盘模式 only**：完整版 QMT 模拟模式下委托不进真实队列，
  模拟需求走 ``server/paper_trading/`` 本地引擎。
"""

import logging

from .backend import CAPABILITY_METHODS, UnsupportedOperation

logger = logging.getLogger("qmt_bridge.trading.bigqmt")

# 上游未覆盖的能力位分组 → 503 降级时的提示（docs/big-qmt.md §4）
_UNSUPPORTED_HINTS = {
    "secu_account": "完整版 QMT 为单账户模型，无证券子账户查询",
    "bank": "银证转账无远端 RPC，可向 xtquant_big_convert 提 issue 扩充",
    "transfer": "资金/证券划转无远端 RPC，可向 xtquant_big_convert 提 issue 扩充",
    "ctp": "完整版 QMT 为证券客户端，无 CTP 跨市场划转",
    "smt": "SMT 约定式交易无远端 RPC",
    "ipo": "上游明示打新查询不能无损替换（query_new_purchase_limit 返回空）",
    "com": "完整版 QMT 为证券客户端，无 COM 期权/期货账户",
    "export": "数据导出无远端 RPC（get_history_trade_detail_data 仅部分替代成交导出）",
}


def _make_unsupported(method_name: str, hint: str):
    """为无远端 RPC 支持的方法生成能力位降级桩（抛 UnsupportedOperation）。"""

    def _raise(self, *args, **kwargs):
        raise UnsupportedOperation(method_name, self.name, hint)

    _raise.__name__ = method_name
    _raise.__doc__ = f"{method_name}() 在 bigqmt 后端不可用：{hint}"
    return _raise


class BigQmtAdapter:
    """基于 ``bigqmt_signal_trader.xtquant_compat`` 的交易后端。

    使用 compat 模块级单例（``configure()`` 原地更新 ``xt_trader`` / ``xtdata``），
    使交易适配层与行情通道（xtdata_source）共享同一个 RPC 客户端。

    Attributes:
        account_id: 已配置的 live 账户 ID（单实例单账户）。
        compat: xtquant_compat 模块（可注入 fake 以便脱离 QMT 环境测试）。
    """

    name = "bigqmt"
    # §4 映射表中 ✅/可用项：委托 / 查询 / 信用（需两融权限）/ 账户（语义降级）
    SUPPORTED_CAPABILITIES = frozenset({"order", "query", "credit", "account"})

    def __init__(self, account_id: str = "", compat=None):
        self.account_id = account_id
        self._compat = compat  # 惰性解析：None 时 connect() 中真实导入
        self._trader = None  # compat.xt_trader 单例（BigQmtXtTrader）
        self._account = None  # compat.StockAccount 实例
        self._callback = None  # BridgeTraderCallback 实例

    # ------------------------------------------------------------------
    # 生命周期
    # ------------------------------------------------------------------

    def _load_compat(self):
        """导入 xtquant_big_convert 的 compat 模块（允许测试注入 fake）。"""
        if self._compat is None:
            try:
                from bigqmt_signal_trader import xtquant_compat
            except ImportError as exc:
                raise ImportError(
                    "bigqmt 后端需要 xtquant-big-convert："
                    "pip install 'qmt-bridge[bigqmt]'"
                ) from exc
            self._compat = xtquant_compat
        return self._compat

    def connect(self):
        """初始化 compat 单例并连接完整版 QMT 的 RPC 服务端。

        ``configure()`` 读取 ``bigqmt_signal_trader_local_config`` 模块或
        ``BIGQMT_*`` 环境变量（Redis 地址 / 账户）；``connect()`` 内部会
        发 ``ping`` RPC 验证链路，Redis 或 QMT 侧服务端不可用时启动即失败。
        """
        compat = self._load_compat()

        # compat.configure() 原地更新 xt_trader / xtdata 单例，
        # 与行情通道（xtdata_source）共享同一 RPC 客户端。
        # account_id kwarg 已对照上游 v0.2.9 wheel 验证：
        # configure(account_id=None, redis_client=None, redis_config=None,
        #           timeout_seconds=None)，配置经 bigqmt_signal_trader_local_config
        # 模块或 BIGQMT_* 环境变量读取。
        compat.configure(account_id=self.account_id or None)
        self._trader = compat.xt_trader
        if not self.account_id:
            self.account_id = str(getattr(self._trader.client, "account_id", "") or "")

        self._account = compat.StockAccount(self.account_id, "STOCK")

        from .callbacks import BridgeTraderCallback

        self._callback = BridgeTraderCallback()
        logger.info(
            "BigQmtAdapter init: account_id=%s (xtquant_big_convert RPC)",
            self.account_id,
        )

        self._trader.register_callback(self._callback)
        self._trader.start()

        result = self._trader.connect()
        if result != 0:
            raise RuntimeError(f"BigQmtXtTrader connect failed: {result}")

        result = self._trader.subscribe(self._account)
        if result != 0:
            logger.warning("subscribe_account returned %s", result)

        logger.info("BigQmtAdapter connected, account=%s", self.account_id)

    def disconnect(self):
        """停止事件监听线程并清理资源。"""
        if self._trader is not None:
            try:
                self._trader.stop()
            except Exception:
                logger.exception("Error stopping BigQmtXtTrader")
            self._trader = None

    def supports(self, capability: str) -> bool:
        """判断是否声明支持某能力位分组。"""
        return capability in self.SUPPORTED_CAPABILITIES

    def _resolve_account(self, account_id: str = ""):
        """解析交易账户（单实例单账户，语义降级）。

        完整版 QMT 一个客户端只绑一个 live 账户；请求其他账户 ID 时
        告警并回落到已配置账户，多账户需多 QMT 实例 + 多 bridge 配置。
        """
        if account_id and account_id != self.account_id:
            logger.warning(
                "bigqmt 后端为单账户模型：请求账户 %s 回落到已配置账户 %s"
                "（多账户需多 QMT 实例）",
                account_id,
                self.account_id,
            )
        return self._account

    # ------------------------------------------------------------------
    # 委托操作 → submit_order / cancel_order RPC
    # ------------------------------------------------------------------

    def order(
        self,
        stock_code: str,
        order_type: int,
        order_volume: int,
        price_type: int = 5,
        price: float = 0.0,
        strategy_name: str = "",
        order_remark: str = "",
        account_id: str = "",
    ):
        """同步下单 → xt_trader.order_stock()（返回 order_sys_id，失败 -1）"""
        account = self._resolve_account(account_id)
        return self._trader.order_stock(
            account,
            stock_code,
            order_type,
            order_volume,
            price_type,
            price,
            strategy_name,
            order_remark,
        )

    def order_async(
        self,
        stock_code: str,
        order_type: int,
        order_volume: int,
        price_type: int = 5,
        price: float = 0.0,
        strategy_name: str = "",
        order_remark: str = "",
        account_id: str = "",
    ):
        """异步下单 → xt_trader.order_stock_async()（返回 seq，
        结果经 on_order_stock_async_response / on_order_error 回调推送）"""
        account = self._resolve_account(account_id)
        return self._trader.order_stock_async(
            account,
            stock_code,
            order_type,
            order_volume,
            price_type,
            price,
            strategy_name,
            order_remark,
        )

    def cancel_order(self, order_id: int, account_id: str = ""):
        """同步撤单 → xt_trader.cancel_order_stock()"""
        account = self._resolve_account(account_id)
        return self._trader.cancel_order_stock(account, order_id)

    def cancel_order_async(self, order_id: int, account_id: str = ""):
        """异步撤单 → xt_trader.cancel_order_stock_async()（结果经回调推送）"""
        account = self._resolve_account(account_id)
        return self._trader.cancel_order_stock_async(account, order_id)

    def cancel_order_stock_sysid(self, market: str, sysid: str, account_id: str = ""):
        """按系统编号同步撤单 → xt_trader.cancel_order_stock_sysid()"""
        account = self._resolve_account(account_id)
        return self._trader.cancel_order_stock_sysid(account, market, sysid)

    def cancel_order_stock_sysid_async(
        self, market: str, sysid: str, account_id: str = ""
    ):
        """按系统编号异步撤单 → xt_trader.cancel_order_stock_sysid_async()"""
        account = self._resolve_account(account_id)
        return self._trader.cancel_order_stock_sysid_async(account, market, sysid)

    # ------------------------------------------------------------------
    # 查询操作 → get_asset / get_positions / query_orders / query_trades
    # ------------------------------------------------------------------

    def query_orders(self, account_id: str = "", cancelable_only: bool = False):
        """查询当日委托列表 → xt_trader.query_stock_orders()"""
        account = self._resolve_account(account_id)
        return self._trader.query_stock_orders(account, cancelable_only)

    def query_positions(self, account_id: str = ""):
        """查询当前持仓列表 → xt_trader.query_stock_positions()"""
        account = self._resolve_account(account_id)
        return self._trader.query_stock_positions(account)

    def query_asset(self, account_id: str = ""):
        """查询账户资产信息 → xt_trader.query_stock_asset()"""
        account = self._resolve_account(account_id)
        return self._trader.query_stock_asset(account)

    def query_trades(self, account_id: str = ""):
        """查询当日成交列表 → xt_trader.query_stock_trades()"""
        account = self._resolve_account(account_id)
        return self._trader.query_stock_trades(account)

    def query_order_detail(self, order_id: int = 0, account_id: str = ""):
        """根据委托编号查询单笔委托详情（服务端本地遍历，无需远端支持）。"""
        account = self._resolve_account(account_id)
        orders = self._trader.query_stock_orders(account, False)
        if orders:
            for o in orders:
                if str(getattr(o, "order_id", "")) == str(order_id) or str(
                    getattr(o, "order_sysid", "")
                ) == str(order_id):
                    return o
        return None

    def query_single_order(self, order_id: int, account_id: str = ""):
        """按委托编号查询单笔委托 → xt_trader.query_stock_order()
        （compat 按 order_id / order_sysid 本地匹配）"""
        account = self._resolve_account(account_id)
        return self._trader.query_stock_order(account, order_id)

    def query_single_trade(self, trade_id: int, account_id: str = ""):
        """按成交编号查询单笔成交（服务端本地遍历，无需远端支持）。"""
        account = self._resolve_account(account_id)
        trades = self._trader.query_stock_trades(account)
        if trades:
            for t in trades:
                if str(getattr(t, "traded_id", "")) == str(trade_id) or str(
                    getattr(t, "trade_id", "")
                ) == str(trade_id):
                    return t
        return None

    def query_single_position(self, stock_code: str, account_id: str = ""):
        """查询单只股票持仓（服务端本地遍历，无需远端支持）。"""
        account = self._resolve_account(account_id)
        positions = self._trader.query_stock_positions(account)
        if positions:
            for p in positions:
                if getattr(p, "stock_code", None) == stock_code:
                    return p
        return None

    # ------------------------------------------------------------------
    # 信用交易操作（两融）→ margin 系列方法
    # 需两融权限，无权限/上下文未绑定时服务端降级返回空列表
    # ------------------------------------------------------------------

    def credit_order(
        self,
        stock_code: str,
        order_type: int,
        order_volume: int,
        price_type: int = 5,
        price: float = 0.0,
        strategy_name: str = "",
        order_remark: str = "",
        account_id: str = "",
    ):
        """信用交易下单（order_type 常量区分融资/融券）→ xt_trader.order_stock()"""
        account = self._resolve_account(account_id)
        return self._trader.order_stock(
            account,
            stock_code,
            order_type,
            order_volume,
            price_type,
            price,
            strategy_name,
            order_remark,
        )

    def query_credit_positions(self, account_id: str = ""):
        """查询信用账户持仓 → xt_trader.query_stock_positions()"""
        account = self._resolve_account(account_id)
        return self._trader.query_stock_positions(account)

    def query_credit_detail(self, account_id: str = ""):
        """查询信用账户资产详情 → xt_trader.query_credit_detail()"""
        account = self._resolve_account(account_id)
        return self._trader.query_credit_detail(account)

    def query_stk_compacts(self, account_id: str = ""):
        """查询信用负债合约 → xt_trader.query_stk_compacts()"""
        account = self._resolve_account(account_id)
        return self._trader.query_stk_compacts(account)

    def query_credit_slo_code(self, account_id: str = ""):
        """查询融券标的券列表 → xt_trader.query_credit_slo_code()"""
        account = self._resolve_account(account_id)
        return self._trader.query_credit_slo_code(account)

    def query_credit_subjects(self, account_id: str = ""):
        """查询信用标的券列表 → xt_trader.query_credit_subjects()"""
        account = self._resolve_account(account_id)
        return self._trader.query_credit_subjects(account)

    def query_credit_assure(self, account_id: str = ""):
        """查询信用担保品信息 → xt_trader.query_credit_assure()"""
        account = self._resolve_account(account_id)
        return self._trader.query_credit_assure(account)

    # ------------------------------------------------------------------
    # 账户信息（单账户模型，语义降级）
    # ------------------------------------------------------------------

    def get_account_status(self, account_id: str = ""):
        """获取账户连接状态（本地判断）。"""
        try:
            return {"connected": self._trader is not None}
        except Exception:
            return {"connected": False}

    def query_account_status(self):
        """查询账户状态 → xt_trader.query_account_status()
        （单账户模型：返回单元素列表或空）"""
        return self._trader.query_account_status()

    def query_account_infos(self):
        """查询账户信息 → xt_trader.query_account_infos()
        （单账户模型：返回单元素列表或空）"""
        return self._trader.query_account_infos()


# ------------------------------------------------------------------
# 能力位降级桩：为所有未支持分组的方法生成抛 UnsupportedOperation 的实现，
# 与 CAPABILITY_METHODS 联动，保证后端方法全集完整且不会与 §4 映射表漂移。
# ------------------------------------------------------------------

for _group, _methods in CAPABILITY_METHODS.items():
    if _group in BigQmtAdapter.SUPPORTED_CAPABILITIES:
        continue
    _hint = _UNSUPPORTED_HINTS.get(_group, "无远端 RPC 支持")
    for _name in _methods:
        setattr(BigQmtAdapter, _name, _make_unsupported(_name, _hint))
