"""模拟交易请求队列模块。

多账户可同时提交 QMT 相关操作（拉取行情、下单、撤单、查询资产等），
请求先进入本队列缓存，再由单一工作线程逐个取出串行执行，
确保任何时刻至多一个线程在通过 QMT 访问行情（xtquant 的 C 扩展非线程安全）。

设计要点：
- 所有账户的请求并发入队（不互相阻塞），处理严格串行、按 FIFO 顺序
- ``submit`` 同步等待执行结果，保持 ``PaperQuantTrader`` 公开 API 不变
- 工作线程内嵌套 submit 时直接内联执行，避免自锁死锁
- 支持超时、统计信息（队列长度、累计处理数、峰值等）供监控端点使用
"""

from __future__ import annotations

import logging
import queue as _stdlib_queue
import threading
import time
from dataclasses import dataclass, field
from typing import Any, Callable

logger = logging.getLogger("qmt_bridge.paper_trading")

# 单个请求的默认等待秒数：覆盖数百笔排队请求的常规积压
DEFAULT_SUBMIT_TIMEOUT = 60.0

# 排队等待超过该秒数时记录 WARNING，便于发现积压
SLOW_WAIT_THRESHOLD = 5.0

# 单个操作执行超过该秒数时记录 WARNING
SLOW_OP_THRESHOLD = 1.0


@dataclass
class PaperRequest:
    """队列中缓存的单个请求。"""

    op: str  # 操作名，如 "order_stock" / "query_stock_asset"
    account_id: str  # 发起请求的账户
    fn: Callable[[], Any]  # 待执行操作，仅由工作线程调用
    enqueued_at: float = field(default_factory=time.monotonic)  # 入队时间戳
    event: threading.Event = field(default_factory=threading.Event)
    result: Any = None
    error: BaseException | None = None

    @property
    def wait_seconds(self) -> float:
        """截至当前已在队列中等待的秒数。"""
        return time.monotonic() - self.enqueued_at


class PaperRequestQueue:
    """模拟交易请求队列：任意线程并发入队，单工作线程逐个执行。

    用法::

        q = PaperRequestQueue()
        q.start()
        result = q.submit("order_stock", "acc001", lambda: do_something())
        q.stop()
    """

    def __init__(
        self,
        worker_name: str = "paper-request-worker",
        timeout: float = DEFAULT_SUBMIT_TIMEOUT,
    ):
        self._queue: _stdlib_queue.Queue[PaperRequest | None] = _stdlib_queue.Queue()
        self._worker_name = worker_name
        self._timeout = timeout
        self._worker: threading.Thread | None = None
        self._worker_thread: threading.Thread | None = None
        self._stop_event = threading.Event()
        self._stats_lock = threading.Lock()
        self._stats = {
            "processed": 0,  # 累计成功处理数
            "failed": 0,  # 累计执行异常数
            "peak_size": 0,  # 队列长度峰值
            "last_op": "",  # 最近处理的操作名
            "last_error": "",  # 最近一次异常描述
        }

    # ------------------------------------------------------------------
    # 生命周期
    # ------------------------------------------------------------------

    def start(self) -> None:
        """启动工作线程（幂等）。"""
        if self._worker is not None and self._worker.is_alive():
            return
        self._stop_event.clear()
        self._worker = threading.Thread(
            target=self._run, name=self._worker_name, daemon=True
        )
        self._worker.start()
        logger.info("模拟交易请求队列工作线程已启动: %s", self._worker_name)

    def stop(self, join_timeout: float = 5.0) -> None:
        """停止工作线程（幂等）。已在队列中的请求会被尽快处理完。"""
        worker = self._worker
        if worker is None:
            return
        self._stop_event.set()
        # 唤醒可能在 get() 上阻塞的工作线程
        self._queue.put(None)
        if worker is not threading.current_thread():
            worker.join(timeout=join_timeout)
        self._worker = None
        self._worker_thread = None
        logger.info("模拟交易请求队列工作线程已停止")

    def _ensure_worker(self) -> None:
        """确保工作线程存活（懒启动，覆盖未显式 start 的用法）。"""
        if self._worker is None or not self._worker.is_alive():
            with self._stats_lock:
                # 双重检查，避免并发 submit 重复起线程
                if self._worker is None or not self._worker.is_alive():
                    logger.warning("请求队列工作线程未运行，自动重启")
                    self.start()

    # ------------------------------------------------------------------
    # 提交与执行
    # ------------------------------------------------------------------

    def submit(
        self,
        op: str,
        account_id: str,
        fn: Callable[[], Any],
        timeout: float | None = None,
    ) -> Any:
        """将请求放入队列并等待串行执行，返回执行结果。

        Args:
            op: 操作名，用于日志与统计。
            account_id: 发起请求的账户。
            fn: 待执行操作，在工作线程中调用。
            timeout: 等待结果的秒数，None 时使用构造时的默认值。

        Returns:
            ``fn()`` 的返回值。

        Raises:
            TimeoutError: 等待超时（请求仍可能稍后被工作线程执行）。
            BaseException: ``fn`` 抛出的异常原样向调用方重放。
        """
        # 工作线程内的嵌套提交直接内联执行，避免自锁死锁
        if (
            self._worker_thread is not None
            and threading.current_thread() is self._worker_thread
        ):
            return fn()

        self._ensure_worker()
        request = PaperRequest(op=op, account_id=account_id, fn=fn)
        self._queue.put(request)
        with self._stats_lock:
            size = self._queue.qsize()
            if size > self._stats["peak_size"]:
                self._stats["peak_size"] = size
        if size >= 20:
            logger.warning("请求队列积压 %d 个请求（当前 op=%s）", size, op)

        if not request.event.wait(timeout if timeout is not None else self._timeout):
            stats = self.stats()
            logger.error(
                "请求等待超时 op=%s account=%s timeout=%.1fs queue_size=%d",
                op,
                account_id,
                timeout if timeout is not None else self._timeout,
                stats["queue_size"],
            )
            raise TimeoutError(
                f"Paper request '{op}' for account '{account_id}' timed out "
                f"waiting in queue (queue_size={stats['queue_size']})"
            )

        if request.error is not None:
            raise request.error
        return request.result

    def _run(self) -> None:
        """工作线程主循环：逐个取出请求串行执行。"""
        self._worker_thread = threading.current_thread()
        while not self._stop_event.is_set():
            try:
                request = self._queue.get(timeout=0.5)
            except _stdlib_queue.Empty:
                continue
            if request is None:  # stop() 唤醒哨兵
                break
            wait = request.wait_seconds
            if wait > SLOW_WAIT_THRESHOLD:
                logger.warning(
                    "请求排队过久 op=%s account=%s wait=%.1fs",
                    request.op,
                    request.account_id,
                    wait,
                )
            started = time.monotonic()
            try:
                request.result = request.fn()
                with self._stats_lock:
                    self._stats["processed"] += 1
                    self._stats["last_op"] = request.op
            except BaseException as exc:
                request.error = exc
                with self._stats_lock:
                    self._stats["failed"] += 1
                    self._stats["last_error"] = f"{request.op}: {exc}"
                logger.exception(
                    "请求执行失败 op=%s account=%s", request.op, request.account_id
                )
            finally:
                request.event.set()
                self._queue.task_done()
                elapsed = time.monotonic() - started
                if elapsed > SLOW_OP_THRESHOLD:
                    logger.warning(
                        "操作执行缓慢 op=%s account=%s elapsed=%.1fs",
                        request.op,
                        request.account_id,
                        elapsed,
                    )
                logger.debug(
                    "请求处理完成 op=%s account=%s wait=%.3fs exec=%.3fs",
                    request.op,
                    request.account_id,
                    wait,
                    elapsed,
                )

    # ------------------------------------------------------------------
    # 统计
    # ------------------------------------------------------------------

    def stats(self) -> dict[str, Any]:
        """返回队列状态快照（供监控端点使用）。"""
        with self._stats_lock:
            snapshot = dict(self._stats)
        snapshot["queue_size"] = self._queue.qsize()
        snapshot["worker_alive"] = self._worker is not None and self._worker.is_alive()
        return snapshot
