#!/usr/bin/env python3
"""为模拟交易账户下载静态价格。

用法示例::

    # 从策略文件提取股票列表并下载到指定账户
    python scripts/download_paper_prices.py \
        --account-id blue_chip_paper \
        --strategy /home/claude/quant_free_trading/cn_strategy/blue_chip_multi_factor_rotation/backtest/blue_chip_multi_factor_rotation.py

    # 直接指定股票代码
    python scripts/download_paper_prices.py \
        --account-id xgb_etf_paper \
        --stock-codes 510300.SH 510050.SH 159915.SZ

    # 指定服务端地址
    python scripts/download_paper_prices.py \
        --host 192.168.1.100 --port 8083 --api-key your-secret-key \
        --account-id blue_chip_paper \
        --strategy /path/to/strategy.py

说明:
    - 本脚本调用 ``/api/paper_accounts/{account_id}/download_prices`` 接口。
    - 若 QMT 客户端未运行，xtquant 无法取到行情，则不会写入任何价格。
    - 下载成功的价格会自动保存到账户配置的 ``static_prices`` 中。
"""

from __future__ import annotations

import argparse
import json
import logging
import re
import sys
from pathlib import Path

# 将 qmt-bridge src 加入路径，确保能导入客户端
QMT_BRIDGE_SRC = Path(__file__).parent.parent / "src"
if str(QMT_BRIDGE_SRC) not in sys.path:
    sys.path.insert(0, str(QMT_BRIDGE_SRC))

from qmt_bridge import QMTClient  # noqa: E402

logger = logging.getLogger("download_paper_prices")


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="为模拟交易账户下载静态价格")
    parser.add_argument("--host", default="localhost", help="QMT Bridge 主机")
    parser.add_argument("--port", type=int, default=8083, help="QMT Bridge 端口")
    parser.add_argument("--api-key", default="test-key", help="API Key")
    parser.add_argument("--account-id", required=True, help="模拟账户 ID")
    parser.add_argument(
        "--strategy",
        default="",
        help="策略文件路径，自动提取 DEFAULT_STOCKS / FALLBACK_STOCKS",
    )
    parser.add_argument(
        "--stock-codes",
        nargs="+",
        default=[],
        help="直接指定股票代码列表，例如 000001.SZ 600519.SH",
    )
    parser.add_argument(
        "--output",
        default="",
        help="将下载结果额外保存到 JSON 文件路径（可选）",
    )
    return parser.parse_args()


def extract_symbols_from_strategy(strategy_path: Path) -> list[str]:
    """从策略源码中提取 DEFAULT_STOCKS 或 FALLBACK_STOCKS 列表。"""
    text = strategy_path.read_text(encoding="utf-8")
    symbols: list[str] = []
    start = text.find("DEFAULT_STOCKS = [")
    if start == -1:
        start = text.find("FALLBACK_STOCKS = [")
    if start != -1:
        end = text.find("]", start)
        block = text[start : end + 1]
        symbols = re.findall(r"['\"]([A-Z]{2}\d{6})['\"]", block)
    return symbols


def normalize_to_qmt(symbol: str) -> str:
    """将 SH600519 转换为 600519.SH。"""
    if symbol.startswith("SH"):
        return f"{symbol[2:]}.SH"
    if symbol.startswith("SZ"):
        return f"{symbol[2:]}.SZ"
    return symbol


def main() -> int:
    args = parse_args()
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s | %(levelname)s | %(message)s",
    )

    symbols: list[str] = []
    if args.strategy:
        strategy_path = Path(args.strategy).expanduser().resolve()
        if not strategy_path.exists():
            logger.error("策略文件不存在: %s", strategy_path)
            return 1
        symbols = extract_symbols_from_strategy(strategy_path)
        logger.info("从 %s 提取到 %d 只股票", strategy_path.name, len(symbols))

    qmt_symbols = [normalize_to_qmt(s) for s in symbols]
    qmt_symbols.extend(args.stock_codes)

    if not qmt_symbols:
        logger.error("未提供任何股票代码，请使用 --strategy 或 --stock-codes")
        return 1

    # 去重并保持顺序
    seen = set()
    unique_symbols = [s for s in qmt_symbols if not (s in seen or seen.add(s))]

    logger.info(
        "准备为账户 %s 下载 %d 只静态价格: %s",
        args.account_id,
        len(unique_symbols),
        ", ".join(unique_symbols[:10]) + (" ..." if len(unique_symbols) > 10 else ""),
    )

    client = QMTClient(
        host=args.host,
        port=args.port,
        api_key=args.api_key,
        paper=True,
    )

    try:
        resp = client._post(
            f"/api/paper_accounts/{args.account_id}/download_prices",
            {"stock_codes": unique_symbols},
        )
    except Exception as e:
        logger.error("下载静态价格失败: %s", e)
        return 1

    data = resp.get("data", {})
    logger.info(
        "账户 %s 成功下载 %d/%d 只静态价格",
        args.account_id,
        len(data),
        len(unique_symbols),
    )
    if data:
        logger.info("下载结果:\n%s", json.dumps(data, ensure_ascii=False, indent=2))

    missing = set(unique_symbols) - set(data.keys())
    if missing:
        logger.warning(
            "以下 %d 只股票未能下载到价格: %s", len(missing), ", ".join(sorted(missing))
        )

    if args.output:
        output_path = Path(args.output)
        output_path.write_text(
            json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8"
        )
        logger.info("结果已保存到: %s", output_path)

    return 0


if __name__ == "__main__":
    sys.exit(main())
