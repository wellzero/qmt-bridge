"""xtdata 请求级串行化模块。

xtquant 的 C 扩展不是线程安全的。FastAPI 的同步路由处理函数在线程池中
并发执行，多个请求同时调用 xtdata.* 会导致内部 BSON 序列化出现数据竞争，
触发 ``Assertion failed: u < 1000000`` 崩溃。

本模块通过 HTTP 中间件 + asyncio.Lock 实现请求级串行化：
- 同一时刻只允许一个 HTTP 请求进入路由处理函数
- 不修改 xtdata 模块本身，避免内部互调死锁风险
- 后台调度器的基础下载任务也通过同一把锁串行化

asyncio.Lock 在事件循环层面工作，持锁期间线程池中的 xtdata 调用正常执行，
释锁后下一个请求才进入处理函数，从而保证 xtdata 不被并发调用。

使用纯 ASGI 中间件（而非 BaseHTTPMiddleware），在服务关闭时排队请求
能立即取消退出，不会产生大量 CancelledError。
"""

import asyncio
import logging

logger = logging.getLogger("qmt_bridge")

# 全局异步锁，确保同一时刻只有一个请求/任务调用 xtdata
xtdata_lock = asyncio.Lock()

# /api/* 中无需串行化的前缀。
# - /api/notify：不调用 xtdata 的端点
# - /api/paper_trading、/api/paper_accounts：模拟交易端点，其 QMT 访问
#   （get_full_tick 等）已由 PaperRequestQueue 单工作线程串行化，
#   不参与全局锁可让多账户请求并发入队缓存，再逐个处理
NO_LOCK_PREFIXES: tuple[str, ...] = (
    "/api/notify",
    "/api/paper_trading",
    "/api/paper_accounts",
)


class XtdataSerializerMiddleware:
    """纯 ASGI 中间件：串行化调用 xtdata 的 HTTP 请求。

    通过 asyncio.Lock 保证同一时刻只有一个请求的同步处理函数在线程池中执行。
    - 仅拦截 HTTP 请求，WebSocket 不受影响。
    - 仅锁 /api/* 路径；/docs、/openapi.json 等静态端点直通。
    - NO_LOCK_PREFIXES 中的路径（如 /api/notify）直通，不参与串行化。
    """

    def __init__(self, app):
        self.app = app

    async def __call__(self, scope, receive, send):
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return
        path = scope.get("path", "")
        if path.startswith("/api/") and not path.startswith(NO_LOCK_PREFIXES):
            async with xtdata_lock:
                await self.app(scope, receive, send)
            return
        await self.app(scope, receive, send)
