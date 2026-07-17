#!/usr/bin/env python3
"""更新模拟交易账户的共享价格缓存。

用于为 ``dashboard/paper-trading`` 提供盘中最新价或当日收盘价，
供实时盈亏计算使用。

价格缓存文件：

- ``data/paper_trading/prices/current.json`` —— 盘中最新价
- ``data/paper_trading/prices/YYYYMMDD.json`` —— 当日收盘价

用法示例：

    # 更新盘中最新价（交易时段运行）
    python scripts/update_paper_price_cache.py --host 192.168.1.100 --port 8083 --api-key xxx

    # 更新收盘价（收盘后运行）
    python scripts/update_paper_price_cache.py --close --host 192.168.1.100 --port 8083 --api-key xxx

    # 指定数据目录
    python scripts/update_paper_price_cache.py --data-dir /path/to/paper_trading

说明:
    - 本脚本扫描 ``data/paper_trading`` 下所有账户的委托记录，汇总所有出现过的股票代码。
    - 通过 ``/api/market/full_tick`` 接口从 qmt-server 获取最新行情。
    - 若未提供 host/port，则默认连接 ``localhost:8083``。
"""

from __future__ import annotations

import argparse
import json
import logging
import sys
from datetime import datetime
from pathlib import Path

# 将 qmt-bridge src 加入路径
QMT_BRIDGE_SRC = Path(__file__).parent.parent / "src"
if str(QMT_BRIDGE_SRC) not in sys.path:
    sys.path.insert(0, str(QMT_BRIDGE_SRC))

from qmt_bridge import QMTClient  # noqa: E402

logger = logging.getLogger("update_paper_price_cache")


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="更新模拟交易共享价格缓存")
    parser.add_argument("--host", default="localhost", help="QMT Bridge 主机")
    parser.add_argument("--port", type=int, default=8083, help="QMT Bridge 端口")
    parser.add_argument("--api-key", default="test-key", help="API Key")
    parser.add_argument(
        "--data-dir",
        default="",
        help="模拟交易数据目录，默认使用项目 data/paper_trading",
    )
    parser.add_argument(
        "--close",
        action="store_true",
        help="保存为当日收盘价（文件名 YYYYMMDD.json），否则保存为 current.json",
    )
    return parser.parse_args()


def _resolve_data_dir(args_data_dir: str) -> Path:
    """解析模拟交易数据目录。"""
    if args_data_dir:
        return Path(args_data_dir).expanduser().resolve()
    # 默认：项目根目录 / data / paper_trading
    return Path(__file__).resolve().parents[2] / "data" / "paper_trading"


def _collect_stock_codes(data_dir: Path) -> list[str]:
    """扫描所有账户的委托 CSV，汇总出现过的股票代码。"""
    codes: set[str] = set()
    if not data_dir.exists():
        return []

    for account_dir in data_dir.iterdir():
        if not account_dir.is_dir():
            continue
        orders_dir = account_dir / "order"
        if not orders_dir.exists():
            continue
        for csv_path in orders_dir.glob("orders_*.csv"):
            try:
                import csv

                with open(csv_path, newline="", encoding="utf-8") as f:
                    reader = csv.DictReader(f)
                    for row in reader:
                        code = row.get("stock_code", "").strip()
                        if code:
                            codes.add(code)
            except Exception:
                logger.exception("读取 CSV 失败: %s", csv_path)

    return sorted(codes)


def _save_prices(data_dir: Path, prices: dict[str, float], close: bool) -> Path:
    """保存价格缓存到文件。"""
    prices_dir = data_dir / "prices"
    prices_dir.mkdir(parents=True, exist_ok=True)

    if close:
        filename = f"{datetime.now().strftime('%Y%m%d')}.json"
    else:
        filename = "current.json"

    path = prices_dir / filename
    payload = {
        "timestamp": datetime.now().isoformat(),
        "type": "close" if close else "intraday",
        "prices": prices,
    }
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    logger.info("已保存 %d 条价格到 %s", len(prices), path)
    return path


def main() -> int:
    args = parse_args()
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s | %(levelname)s | %(message)s",
    )

    data_dir = _resolve_data_dir(args.data_dir)
    logger.info("数据目录: %s", data_dir)

    stock_codes = _collect_stock_codes(data_dir)
    if not stock_codes:
        logger.warning("未找到任何股票代码，跳过更新")
        return 0

    logger.info("汇总到 %d 只股票，准备获取价格", len(stock_codes))

    client = QMTClient(
        host=args.host,
        port=args.port,
        api_key=args.api_key,
        paper=True,
    )

    try:
        resp = client._get("/api/market/full_tick", {"stocks": stock_codes})
        data = resp.get("data", {})
    except Exception as e:
        logger.error("获取行情失败: %s", e)
        return 1

    prices: dict[str, float] = {}
    for code in stock_codes:
        tick = data.get(code)
        if not isinstance(tick, dict):
            continue
        # 优先使用 lastPrice，否则 close/open
        for key in ("lastPrice", "close", "open", "lastprice"):
            price = tick.get(key)
            if isinstance(price, (int, float)) and price > 0:
                prices[code] = float(price)
                break

    if not prices:
        logger.warning("未能获取到任何有效价格")
        return 0

    logger.info("成功获取 %d/%d 只股票价格", len(prices), len(stock_codes))
    missing = set(stock_codes) - set(prices.keys())
    if missing:
        logger.warning("以下 %d 只股票缺少价格: %s", len(missing), ", ".join(sorted(missing)))

    _save_prices(data_dir, prices, args.close)
    return 0


if __name__ == "__main__":
    sys.exit(main())
