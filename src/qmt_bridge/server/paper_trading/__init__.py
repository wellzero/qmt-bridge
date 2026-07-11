"""模拟交易子模块。

提供不依赖真实 QMT 客户端的纯本地模拟交易能力，
包括 ``PaperQuantTrader``、``PaperAccount``、账户配置管理与 REST API 路由。
"""

from .manager import PaperTraderManager
from .papertrader import PaperAccount, PaperQuantTrader
from .router import router

__all__ = ["PaperTraderManager", "PaperAccount", "PaperQuantTrader", "router"]
