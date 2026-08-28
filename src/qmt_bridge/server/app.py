"""FastAPI 应用工厂与生命周期管理模块。

本模块负责：
1. 创建和配置 FastAPI 应用实例（应用工厂模式）
2. 管理应用生命周期（lifespan），包括：
   - 启动时初始化 xttrader 交易管理器（XtTraderManager）
   - 启动时初始化通知模块（飞书/Webhook 通知）
   - 关闭时清理所有资源连接
3. 注册所有 HTTP 路由和 WebSocket 端点

注：定时下载调度器已拆分为独立进程 ``qmt-scheduler``，
不再随 API 服务启动，避免 xtdata C 扩展并发调用崩溃。
"""

import logging
from contextlib import asynccontextmanager

from fastapi import FastAPI

from .config import Settings, get_settings
from .xtdata_lock import XtdataSerializerMiddleware

# 全局日志记录器，用于记录服务端运行状态
logger = logging.getLogger("qmt_bridge")

# ── xtdata 并发保护 ─────────────────────────────────────────────
# xtdata 的 C 扩展不是线程安全的。FastAPI 把同步路由处理函数分发到
# 线程池并发执行，多个请求同时调用 xtdata 会导致 BSON 断言崩溃。
#
# 通过 XtdataSerializerMiddleware（纯 ASGI 中间件）对 /api/* 请求加锁，
# 保证同一时刻只有一个 sync handler 在线程池里调用 xtdata。
# 详见 xtdata_lock.py。
# ────────────────────────────────────────────────────────────────


@asynccontextmanager
async def _lifespan(app: FastAPI):
    """管理 FastAPI 应用的生命周期（启动/关闭）。

    启动阶段（yield 之前）：
    1. 若启用交易功能，按 ``trader_backend`` 初始化 XtTraderManager
       （mini → XtQuantTrader 直连 miniQMT；bigqmt → xtquant_big_convert RPC）
    2. 若启用通知功能，启动 NotifierManager（飞书/Webhook 通知后端）

    关闭阶段（yield 之后）：
    1. 停止通知模块
    2. 断开交易管理器连接（底层调用 xttrader.disconnect()）

    Args:
        app: FastAPI 应用实例，通过 app.state 存储共享状态
    """
    settings: Settings = get_settings()

    # 如果配置中启用了交易模块，则初始化交易管理器
    if settings.trading_enabled:
        try:
            from .trading.manager import XtTraderManager

            # 按 --trader-backend 选定后端（mini / bigqmt，docs/big-qmt.md §3.2）
            manager = XtTraderManager.create(
                trader_backend=settings.trader_backend,
                mini_qmt_path=settings.mini_qmt_path,
                account_id=settings.trading_account_id,
            )
            # 连接交易后端（mini: xttrader.connect()；bigqmt: RPC ping）
            manager.connect()
            # 将管理器存储到 app.state，供各路由通过依赖注入获取
            app.state.trader_manager = manager
            logger.info("Trading module initialized (backend=%s)", manager.backend_name)
        except Exception:
            logger.exception("Failed to initialize trading module")
            app.state.trader_manager = None
    else:
        app.state.trader_manager = None

    # 如果配置中启用了模拟交易模块，则初始化 PaperTraderManager
    if settings.paper_trading_enabled:
        try:
            from .paper_trading import PaperTraderManager

            paper_manager = PaperTraderManager(
                data_dir=settings.paper_trading_data_dir or None,
                config_path=settings.paper_trading_config_path or None,
                default_account_id=settings.trading_account_id,
            )
            paper_manager.connect()
            app.state.paper_trader_manager = paper_manager
            logger.info("Paper trading module initialized")
        except Exception:
            logger.exception("Failed to initialize paper trading module")
            app.state.paper_trader_manager = None
    else:
        app.state.paper_trader_manager = None

    # 初始化通知模块（独立于交易模块，可单独启用）
    if settings.notify_enabled:
        try:
            from .notify import NotifierManager

            # 创建通知管理器并启动后台任务
            notifier = NotifierManager(settings)
            await notifier.start()
            app.state.notifier_manager = notifier
            logger.info("Notification module initialized")

            # 如果交易模块也已启用，将通知器注入到交易回调中
            # 这样当 xttrader 产生委托/成交回调时，可自动推送通知
            manager = getattr(app.state, "trader_manager", None)
            if manager is not None and hasattr(manager, "_callback"):
                manager._callback.set_notifier(notifier)
        except Exception:
            logger.exception("Failed to initialize notification module")
            app.state.notifier_manager = None
    else:
        app.state.notifier_manager = None

    yield  # --- 应用运行中，以下为关闭阶段 ---

    # 停止通知模块，释放后台资源
    notifier = getattr(app.state, "notifier_manager", None)
    if notifier is not None:
        try:
            await notifier.stop()
            logger.info("Notification module stopped")
        except Exception:
            logger.exception("Error stopping notification module")

    # 断开交易管理器连接（底层调用 xttrader.disconnect()）
    manager = getattr(app.state, "trader_manager", None)
    if manager is not None:
        try:
            manager.disconnect()
            logger.info("Trading module disconnected")
        except Exception:
            logger.exception("Error disconnecting trading module")

    # 断开模拟交易管理器连接
    paper_manager = getattr(app.state, "paper_trader_manager", None)
    if paper_manager is not None:
        try:
            paper_manager.disconnect()
            logger.info("Paper trading module disconnected")
        except Exception:
            logger.exception("Error disconnecting paper trading module")


def create_app(settings: Settings | None = None) -> FastAPI:
    """创建并配置 FastAPI 应用实例（应用工厂函数）。

    该函数完成以下工作：
    1. 创建 FastAPI 实例并绑定生命周期管理器
    2. 注册所有数据查询路由（行情、板块、财务等，始终可用）
    3. 注册 WebSocket 端点（实时行情推送、下载进度等）
    4. 根据配置条件注册通知路由和交易路由

    Args:
        settings: 应用配置对象。若为 None，则从环境变量自动加载。

    Returns:
        配置完成的 FastAPI 应用实例，可直接传给 uvicorn 运行。
    """
    if settings is None:
        settings = get_settings()

    app = FastAPI(
        title="QMT Bridge",
        description="miniQMT market data & trading API bridge",
        version="2.0.0",
        lifespan=_lifespan,
    )

    # 全局 xtdata 串行化中间件（仅拦截调用 xtdata 的 /api/* 端点）
    app.add_middleware(XtdataSerializerMiddleware)

    # 能力位降级：bigqmt 后端无远端 RPC 支持的交易接口 → 503
    # （docs/big-qmt.md §3.4；错误信息注明等待远端支持）
    from fastapi import Request
    from fastapi.responses import JSONResponse

    from .trading.backend import UnsupportedOperation

    @app.exception_handler(UnsupportedOperation)
    async def _unsupported_operation_handler(
        request: Request, exc: UnsupportedOperation
    ):
        return JSONResponse(
            status_code=503,
            content={
                "detail": str(exc),
                "backend": exc.backend,
                "method": exc.method,
            },
        )

    # bigqmt RPC 超时 → 504。RPC-only 端点（get_market_data /
    # get_market_last_trade_date 等未映射进 FormulaServer 白名单的调用）在
    # QMT 端策略未运行（休市/实例停止）时会等满超时再抛 TimeoutError，
    # 此前直接 500 且无任何线索；统一转成带提示的 504。
    # 不做重试：超时本身即"远端不在"，重试只会拖长响应。
    @app.exception_handler(TimeoutError)
    async def _rpc_timeout_handler(request: Request, exc: TimeoutError):
        return JSONResponse(
            status_code=504,
            content={
                "detail": "upstream RPC timeout: %s" % exc,
                "hint": (
                    "bigqmt RPC-only 接口需要 QMT 端策略实例运行（交易时段）；"
                    "行情 FormulaServer 直连部分不受影响"
                ),
            },
        )

    # ------------------------------------------------------------------
    # 注册数据查询路由（始终可用，无需启用交易模块）
    # 这些路由底层调用 xtquant.xtdata 的各类行情数据接口
    # 串行化由 XtdataSerializerMiddleware 统一处理
    # ------------------------------------------------------------------
    from .routers import (
        bond,
        calendar,
        cb,
        download,
        etf,
        financial,
        formula,
        futures,
        hk,
        instrument,
        legacy,
        market,
        meta,
        option,
        sector,
        tabular,
        tick,
        utility,
    )

    app.include_router(market.router)
    app.include_router(tick.router)
    app.include_router(sector.router)
    app.include_router(calendar.router)
    app.include_router(financial.router)
    app.include_router(instrument.router)
    app.include_router(option.router)
    app.include_router(etf.router)
    app.include_router(cb.router)
    app.include_router(bond.router)
    app.include_router(futures.router)
    app.include_router(meta.router)
    app.include_router(download.router)
    app.include_router(formula.router)
    app.include_router(hk.router)
    app.include_router(tabular.router)
    app.include_router(utility.router)
    app.include_router(legacy.router)

    # ------------------------------------------------------------------
    # 注册 WebSocket 端点（实时数据推送）
    # WebSocket 不加串行化依赖，避免长连接永久持锁
    # ------------------------------------------------------------------
    from .ws import download_progress, formula as formula_ws, realtime, whole_quote

    app.include_router(realtime.router)
    app.include_router(whole_quote.router)
    app.include_router(download_progress.router)
    app.include_router(formula_ws.router)

    # ------------------------------------------------------------------
    # 注册通知路由（仅在配置中启用通知时加载）
    # ------------------------------------------------------------------
    if settings.notify_enabled:
        from .notify.base import router as notify_router

        app.include_router(notify_router)  # 通知管理接口

    # ------------------------------------------------------------------
    # 注册交易路由（仅在配置中启用交易时加载）
    # 这些路由底层调用 xtquant.xttrader 的交易接口
    # ------------------------------------------------------------------
    if settings.trading_enabled:
        from .routers import bank, credit, fund, smt, trading

        app.include_router(trading.router)
        app.include_router(credit.router)
        app.include_router(fund.router)
        app.include_router(smt.router)
        app.include_router(bank.router)

        from .ws import trade_callback

        app.include_router(trade_callback.router)

    # ------------------------------------------------------------------
    # 注册模拟交易路由（仅在配置中启用模拟交易时加载）
    # ------------------------------------------------------------------
    if settings.paper_trading_enabled:
        from .paper_trading import router as paper_trading_router

        app.include_router(paper_trading_router)

    return app
