"""xtdata import shim 安装器（bigqmt 模式行情通道）。

按 ``docs/big-qmt.md`` §3.3：``xtquant_big_convert`` 附带 ``xtquant`` import
shim（把 ``xtquant.xtdata`` / ``xtquant.xttrader`` 代理到 compat 单例）。
本仓库 routers / ws / downloader / scheduler 共有 23 处**模块顶层**
``from xtquant import xtdata``（非惰性，导入 app 即触发），因此必须在
``cli.py`` 构建 app（``from .app import create_app``）与导入 scheduler
**之前**把 shim 目录插到 ``sys.path`` 最前：

.. code-block:: python

    install_bigqmt_xtdata_shim()  # backend == "bigqmt" 时

shim 目录解析顺序：

1. 显式指定（``QMT_BRIDGE_BIGQMT_SHIM_DIR`` /
   ``scripts/deploy_bigqmt_server.py`` 提取出的专用目录）——
   用于真实 xtquant 与 shim 同处 site-packages 时避免互相覆盖；
2. 已安装 ``xtquant-big-convert`` 自带的顶层 ``xtquant`` 目录
   （经特征检测确认是 shim 而非真实 xtquant）。

注意：shim 路径必须位于真实 xtquant 之前。
"""

import logging
import sys
from pathlib import Path

logger = logging.getLogger("qmt_bridge.bigqmt_shim")


def _looks_like_shim(xtquant_dir: Path) -> bool:
    """检测目录是否为 xtquant_big_convert 附带的 import shim。

    shim 的 ``xttrader.py`` 仅是从 ``bigqmt_signal_trader.xtquant_compat``
    重导出；真实 xtquant 的同名文件含 XtQuantTraderClient 等实现。
    """
    try:
        xttrader = xtquant_dir / "xttrader.py"
        xtdata = xtquant_dir / "xtdata.py"
        if not (xttrader.is_file() and xtdata.is_file()):
            return False
        return "bigqmt_signal_trader" in xttrader.read_text(encoding="utf-8")
    except OSError:
        return False


def _shim_candidates(explicit_dir: str | None = None):
    """按优先级产出候选 shim 目录（含真实 xtquant 安装时排除自身的说明）。"""
    if explicit_dir:
        yield Path(explicit_dir)
    # 已安装 xtquant-big-convert 的顶层 xtquant 包（site-packages）
    try:
        from bigqmt_signal_trader import xtquant_compat  # noqa: F401

        pkg_dir = Path(xtquant_compat.__file__).resolve().parent
        yield pkg_dir.parent / "xtquant"
    except ImportError:
        pass


def install_bigqmt_xtdata_shim(explicit_dir: str | None = None) -> Path | None:
    """把 xtquant_big_convert 的 xtquant shim 插到 ``sys.path`` 最前。

    幂等：已在 sys.path 最前时直接返回。未找到可用 shim 时返回 None
    （交易仍可用 —— BigQmtAdapter 直接 import compat —— 但行情会落到
    真实 xtquant / miniQMT 路径，启动日志会给出告警）。

    Args:
        explicit_dir: 显式 shim 目录（含 ``xtquant/`` 子目录或即其本身），
            默认取 ``QMT_BRIDGE_BIGQMT_SHIM_DIR`` 环境变量。

    Returns:
        实际插入 ``sys.path`` 的目录（shim 包的父目录），未安装时为 None。
    """
    if explicit_dir is None:
        import os

        explicit_dir = os.environ.get("QMT_BRIDGE_BIGQMT_SHIM_DIR", "") or None

    for candidate in _shim_candidates(explicit_dir):
        # 允许传入 xtquant 目录本身或其父目录
        if candidate.name == "xtquant" and candidate.is_dir():
            shim_parent = candidate.parent
            shim_dir = candidate
        elif (candidate / "xtquant").is_dir():
            shim_parent = candidate
            shim_dir = candidate / "xtquant"
        else:
            continue
        if not _looks_like_shim(shim_dir):
            logger.warning(
                "候选 shim 目录 %s 未通过特征检测（不是 xtquant_big_convert 的 "
                "xtquant shim），跳过",
                shim_dir,
            )
            continue
        if sys.path and sys.path[0] == str(shim_parent):
            return shim_parent
        # 必须位于真实 xtquant 之前：插到 sys.path[0]。
        # 注意本函数必须在任何 xtquant 导入之前调用，否则同一进程内无法切换。
        if "xtquant" in sys.modules:
            logger.warning(
                "xtquant 已在本进程导入，shim 将不生效 —— 请确保在应用启动前安装"
            )
        sys.path.insert(0, str(shim_parent))
        logger.info("xtquant import shim 已安装: %s（置于 sys.path 最前）", shim_dir)
        return shim_parent

    logger.warning(
        "未找到 xtquant_big_convert 的 xtquant shim：行情将回落到真实 xtquant "
        "（miniQMT 路径）。可 pip install 'qmt-bridge[bigqmt]' 或设置 "
        "QMT_BRIDGE_BIGQMT_SHIM_DIR 指向 shim 目录"
    )
    return None
