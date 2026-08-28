"""xtdata 来源选择器 —— 按交易后端返回行情模块。

背景（docs/big-qmt.md §3.3）
--------------------------------------
本仓库 routers / ws / downloader / scheduler / paper_trading 共 30+ 处
``from xtquant import xtdata``。bigqmt 后端下这些调用必须落到
``xtquant_big_convert`` 的 RPC 客户端（FormulaServer 直连 + Redis RPC），
mini 后端下则用真实 xtquant（miniQMT 数据服务）。

实现：显式间接层（不依赖 sys.path 顺序 —— 本机存在多个同名 ``xtquant``
包：Lib 手动拷贝、site-packages、editable checkout，顺序法容易被遮蔽）。
调用处统一写::

    from .xtdata_source import xtdata          # 顶层 server 模块
    from ..xtdata_source import xtdata         # routers/ ws/ paper_trading 等子包

模块导入时按 ``settings.trader_backend`` 解析一次并绑定；需要运行期重解析
（如测试切换后端）时用 :func:`get_xtdata`。解析规则：

- ``bigqmt`` → ``from bigqmt_signal_trader.xtquant_compat import xtdata``
  （上游单例，FormulaServer 白名单读走 127.0.0.1:58600 直连，其余走 RPC）
- 其它（默认 mini）→ 真实 ``xtquant.xtdata``

时序要求：cli.py 必须先 ``reset_settings`` 再导入 app / scheduler
（现状即如此），本模块首次导入时读到的就是最终后端。
"""

from __future__ import annotations

import logging
import os

logger = logging.getLogger("qmt_bridge.xtdata_source")

# 已解析的 xtdata 模块对象（首次 get_xtdata() 后缓存）
_resolved: object | None = None


def _current_backend() -> str:
    """读取当前交易后端：优先 settings 单例，退回环境变量，默认 mini。

    settings 未初始化（测试直接导入路由等场景）时 get_settings() 会
    from_env() 兜底，其 trader_backend 又取自 QMT_BRIDGE_TRADER_BACKEND，
    故两条路径最终一致。
    """
    try:
        from .config import get_settings

        return str(get_settings().trader_backend or "mini").lower()
    except Exception:  # 配置系统本身异常时不应拖垮行情导入
        return str(os.environ.get("QMT_BRIDGE_TRADER_BACKEND", "mini")).lower()


def get_xtdata():
    """按后端解析并返回 xtdata 模块对象。

    - bigqmt → ``bigqmt_signal_trader.xtquant_compat.xtdata`` 单例（缓存；
      单例本就稳定，缓存无副作用）。
    - mini → 真实 ``xtquant.xtdata``。**不缓存**：每次优先看
      ``sys.modules["xtquant"]``，使测试对 xtquant 的运行期替换
      （unittest.mock.patch.dict(sys.modules)）在调用时仍然生效 ——
      paper_trading 等惰性导入点依赖这一行为。

    Returns:
        当前后端的 xtdata 模块/单例对象。
    """
    global _resolved
    if _current_backend() == "bigqmt":
        if _resolved is None or type(_resolved).__name__ != "BigQmtXtData":
            from bigqmt_signal_trader.xtquant_compat import xtdata as _xt

            logger.info("xtdata -> bigqmt_signal_trader.xtquant_compat（RPC/FormulaServer 通道）")
            _resolved = _xt
        return _resolved
    import sys as _sys

    mod = _sys.modules.get("xtquant")
    if mod is not None and hasattr(mod, "xtdata"):
        return mod.xtdata  # 测试 mock / 已导入状态优先（与旧行为一致）
    from xtquant import xtdata as _xt

    logger.info("xtdata -> xtquant.xtdata（真实 miniQMT 通道）")
    return _xt


def reset_xtdata_cache() -> None:
    """清空 bigqmt 单例缓存（后端配置变化/测试切换时使用）。"""
    global _resolved
    _resolved = None


# 模块级绑定：调用方 `from .xtdata_source import xtdata` 即得当前后端的模块。
# 注意这是导入时快照（导入期生效，与调用方自己的顶层 import 语义一致）。
xtdata = get_xtdata()
