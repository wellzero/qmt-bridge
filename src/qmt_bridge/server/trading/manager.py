"""XtTraderManager — 交易后端的门面与选择器。

按 ``docs/big-qmt.md`` §3.2，原 1:1 门面拆分为协议 + 双后端：

.. code-block:: text

    TraderBackend (protocol, backend.py)
      ├─ MiniQmtBackend  # 现有 XtQuantTrader 实现（mini_backend.py），原样保留
      └─ BigQmtAdapter   # xtquant_big_convert RPC（bigqmt_backend.py），新增

``XtTraderManager`` 保持原有公开方法集不变（路由层零改动），
内部把交易操作委托给按 ``--trader-backend`` 选定的后端；
不支持的操作由后端抛 ``UnsupportedOperation``（app 层转 503）。
"""

import logging

from .backend import TraderBackend

logger = logging.getLogger("qmt_bridge.trading")


class XtTraderManager:
    """交易管理器门面 ── 持有一个 TraderBackend 并转发全部交易操作。

    在 FastAPI lifespan 启动阶段、当交易功能启用时被创建。
    路由层通过 ``Depends(get_trader_manager)`` 拿到本类实例，
    方法调用经 ``__getattr__`` 透传给后端（mini / bigqmt 对上层无差别）。

    Attributes:
        backend: 当前交易后端实例。
    """

    def __init__(self, backend: TraderBackend):
        self._backend = backend

    # ------------------------------------------------------------------
    # 后端工厂
    # ------------------------------------------------------------------

    @classmethod
    def create(
        cls,
        trader_backend: str = "mini",
        mini_qmt_path: str = "",
        account_id: str = "",
    ) -> "XtTraderManager":
        """按后端类型创建管理器（app.py lifespan 调用）。

        Args:
            trader_backend: "mini"（XtQuantTrader 直连）或
                "bigqmt"（xtquant_big_convert RPC，需 ``qmt-bridge[bigqmt]``）。
            mini_qmt_path: miniQMT 安装路径（仅 mini 后端使用）。
            account_id: 交易资金账号。

        Returns:
            持有对应后端的 XtTraderManager 实例（尚未 connect）。
        """
        backend_name = (trader_backend or "mini").lower()
        if backend_name == "bigqmt":
            from .bigqmt_backend import BigQmtAdapter

            backend = BigQmtAdapter(account_id=account_id)
        elif backend_name == "mini":
            from .mini_backend import MiniQmtBackend

            backend = MiniQmtBackend(mini_qmt_path=mini_qmt_path, account_id=account_id)
        else:
            raise ValueError(f"未知交易后端: {trader_backend!r}（可选 mini / bigqmt）")
        return cls(backend)

    # ------------------------------------------------------------------
    # 生命周期与能力位
    # ------------------------------------------------------------------

    @property
    def backend_name(self) -> str:
        """当前后端标识（"mini" / "bigqmt"）。"""
        return getattr(self._backend, "name", "unknown")

    @property
    def backend(self) -> TraderBackend:
        """当前交易后端实例（双后端字段对比等工具使用）。"""
        return self._backend

    def connect(self):
        """初始化并连接交易后端。"""
        self._backend.connect()

    def disconnect(self):
        """断开连接并清理资源。"""
        self._backend.disconnect()

    def supports(self, capability: str) -> bool:
        """判断当前后端是否声明支持某能力位分组（§4）。"""
        return self._backend.supports(capability)

    # ------------------------------------------------------------------
    # 业务方法转发
    # ------------------------------------------------------------------

    def __getattr__(self, name: str):
        """把业务方法（order / query_orders / ...）透传给后端。

        后端自身对无远端支持的方法抛 ``UnsupportedOperation``，
        本层不做重复门控（单一事实来源）。私有属性（如 ``_callback``，
        供 app.py 注入通知器）同样透传，保持与旧版 XtTraderManager 兼容。
        """
        # __getattr__ 仅在常规查找失败时触发；_backend 未就绪时（如 copy/pickle
        # 探测属性）按标准协议抛 AttributeError，避免无限递归
        if name.startswith("__") and name.endswith("__"):
            raise AttributeError(name)
        backend = self.__dict__.get("_backend")
        if backend is None:
            raise AttributeError(name)
        return getattr(backend, name)
