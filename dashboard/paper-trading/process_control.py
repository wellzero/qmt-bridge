"""策略进程控制。

为「账户状态监控」页面提供 启动 / 停止 / 重启 能力，直接复用
``quant_free_strategies/run_all_paper_tests.py`` 的进程管理逻辑
（PID 文件 + 后台启动 + 优雅终止），保证与命令行脚本行为完全一致：

- ``status_strategy``：按 PID 文件判定 运行中 / 已停止 / PID 残留
- ``start_strategy`` / ``stop_strategy``：以 detached 进程启动、SIGTERM 后 SIGKILL

编排脚本位于另一仓库，故按需通过 importlib 加载；文件缺失时返回
``None``，页面降级为提示而非报错。
"""

from __future__ import annotations

import importlib.util
import os
import sys
import threading
from pathlib import Path
from typing import Any

# 编排脚本与默认 PID/日志目录（与 run_all_paper_tests.py 的默认值一致）
ORCHESTRATOR_FILE = Path(
    os.getenv(
        "PAPER_ORCHESTRATOR",
        "/home/claude/quant_free_strategies/run_all_paper_tests.py",
    )
)

# 进程状态（前端展示文案）
CTRL_RUNNING = "RUNNING"
CTRL_STOPPED = "STOPPED"

_module: Any = None
_module_lock = threading.Lock()


def load_orchestrator() -> Any | None:
    """按需加载编排脚本模块；不可用时返回 ``None``。

    结果缓存于模块级变量；streamlit 多线程 rerun 下用锁保证只加载一次。
    """
    global _module
    if _module is not None:
        return _module
    with _module_lock:
        if _module is not None:
            return _module
        if not ORCHESTRATOR_FILE.exists():
            return None
        spec = importlib.util.spec_from_file_location(
            "run_all_paper_tests", ORCHESTRATOR_FILE
        )
        if spec is None or spec.loader is None:
            return None
        module = importlib.util.module_from_spec(spec)
        sys.modules[spec.name] = module
        spec.loader.exec_module(module)
        _module = module
        return module


def _pid_dir(orch: Any) -> Path:
    """编排脚本默认的 PID 文件目录。"""
    return Path(os.getenv("PID_DIR", str(orch.BASE_DIR / "paper_test_pids")))


def _log_dir(orch: Any) -> Path:
    """编排脚本默认的策略日志目录。"""
    return Path(os.getenv("LOG_DIR", str(orch.BASE_DIR / "paper_test_logs")))


def list_strategy_controls() -> list[dict] | None:
    """列出全部受管策略及其 PID 状态。

    Returns:
        每项含 ``stem``（日志/PID 文件基名）、``status``、``pid``；
        编排脚本不可用时返回 ``None``。
    """
    orch = load_orchestrator()
    if orch is None:
        return None
    pid_dir = _pid_dir(orch)
    controls = []
    for target in orch.discover_strategies(orch.BASE_DIR):
        s = orch.status_strategy(target, pid_dir)
        controls.append(
            {"stem": target.stem, "status": s["status"], "pid": s.get("pid")}
        )
    return controls


def perform_action(stem: str, action: str) -> dict:
    """对单个策略执行 启动 / 停止 / 重启。

    Args:
        stem: 策略基名（``StrategyTarget.stem``，即日志/PID 文件名去扩展名）。
        action: ``start`` / ``stop`` / ``restart`` 之一。

    Returns:
        编排脚本的返回 dict（含 ``status``），异常或找不到策略时返回
        带 ``status`` 的错误 dict，不抛出。
    """
    action = action.lower()
    if action not in ("start", "stop", "restart"):
        return {"name": stem, "status": f"BAD_ACTION: {action}"}

    orch = load_orchestrator()
    if orch is None:
        return {"name": stem, "status": "ORCHESTRATOR_UNAVAILABLE"}

    try:
        target = next(
            (t for t in orch.discover_strategies(orch.BASE_DIR) if t.stem == stem),
            None,
        )
        if target is None:
            return {"name": stem, "status": "NOT_FOUND"}

        pid_dir = _pid_dir(orch)
        if action == "start":
            return orch.start_strategy(target, _log_dir(orch), pid_dir)
        if action == "stop":
            return orch.stop_strategy(target, pid_dir)
        # restart：先停（清理残留 PID）再启动
        stop_result = orch.stop_strategy(target, pid_dir)
        start_result = orch.start_strategy(target, _log_dir(orch), pid_dir)
        return {
            "name": stem,
            "status": f"{stop_result['status']} -> {start_result['status']}",
            "pid": start_result.get("pid"),
        }
    except Exception as e:  # 进程操作失败不应打断页面渲染
        return {"name": stem, "status": f"ERROR: {e}"}
