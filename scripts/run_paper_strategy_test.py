#!/usr/bin/env python3
"""模拟交易多策略并发测试运行器。

用法示例::

    # 单策略测试
    python scripts/run_paper_strategy_test.py \
        --strategy /home/claude/quant_free_trading/cn_strategy/blue_chip_multi_factor_rotation/backtest/blue_chip_multi_factor_rotation_backup.py \
        --account-id blue_chip_paper \
        --port 8083 \
        --duration 120

    # 双策略并发测试
    python scripts/run_paper_strategy_test.py \
        --strategy /path/to/blue_chip.py --account-id blue_chip_paper \
        --strategy /path/to/xgb_etf.py --account-id xgb_etf_paper \
        --port 8083 \
        --duration 180

环境要求:
    - 本脚本所在机器需能启动 QMT Bridge 服务端（即安装了 xtquant 的 Windows 环境）。
    - 策略通过 lumibot 的 QMTBridgeBroker 连接，启用 paper 模式后会自动走
      ``/api/paper_trading/*`` 端点。
    - 每个策略使用独立的 ``--account-id``，确保资金、持仓隔离。

注意:
    - 测试前会尝试为每个账户下载静态价格；若 QMT 客户端未运行，可改用
      ``--static-prices-file`` 传入 JSON 价格表。
    - 脚本会强制设置 ``QMT_BRIDGE_PAPER=true`` 和 ``IS_BACKTESTING=false``。
"""

from __future__ import annotations

import argparse
import json
import logging
import os
import subprocess
import sys
import tempfile
import time
from pathlib import Path

# 将 qmt-bridge src 加入路径，确保能导入客户端
QMT_BRIDGE_SRC = Path(__file__).parent.parent / "src"
if str(QMT_BRIDGE_SRC) not in sys.path:
    sys.path.insert(0, str(QMT_BRIDGE_SRC))

from qmt_bridge import QMTClient  # noqa: E402

logger = logging.getLogger("paper_strategy_test")


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="模拟交易多策略并发测试")
    parser.add_argument(
        "--strategy",
        action="append",
        required=True,
        help="策略文件路径（可多次传入，实现并发）",
    )
    parser.add_argument(
        "--account-id",
        action="append",
        required=True,
        help="与 --strategy 一一对应的模拟账户 ID",
    )
    parser.add_argument("--host", default="localhost", help="QMT Bridge 主机")
    parser.add_argument("--port", type=int, default=8083, help="QMT Bridge 端口")
    parser.add_argument("--api-key", default="test-key", help="API Key")
    parser.add_argument(
        "--initial-cash",
        type=float,
        default=1_000_000.0,
        help="每个模拟账户初始资金",
    )
    parser.add_argument(
        "--duration",
        type=int,
        default=120,
        help="每个策略运行时长（秒），超时后自动终止",
    )
    parser.add_argument(
        "--static-prices-file",
        default="",
        help="静态价格 JSON 文件路径（可选，用于无 QMT 行情时）",
    )
    parser.add_argument(
        "--server-cmd",
        default="python -m qmt_bridge.server.cli",
        help="启动 QMT Bridge 服务端的命令前缀",
    )
    parser.add_argument(
        "--no-server",
        action="store_true",
        help="不自动启动服务端（假定用户已在外部启动）",
    )
    parser.add_argument(
        "--server-data-dir",
        default="./data/paper_strategy_test",
        help="服务端数据目录",
    )
    return parser.parse_args()


def start_server(args: argparse.Namespace) -> subprocess.Popen:
    """在后台启动 QMT Bridge 服务端。"""
    cmd = [
        *args.server_cmd.split(),
        "--paper-trading",
        "--api-key",
        args.api_key,
        "--port",
        str(args.port),
        "--paper-trading-data-dir",
        args.server_data_dir,
    ]
    logger.info("启动 QMT Bridge 服务端: %s", " ".join(cmd))
    proc = subprocess.Popen(
        cmd,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
    )
    return proc


def wait_for_server(host: str, port: int, timeout: float = 30.0) -> bool:
    """轮询等待服务端可用。"""
    url = f"http://{host}:{port}/docs"
    deadline = time.time() + timeout
    while time.time() < deadline:
        try:
            import urllib.request

            with urllib.request.urlopen(url, timeout=2) as resp:
                if resp.status == 200:
                    return True
        except Exception:
            pass
        time.sleep(0.5)
    return False


def create_paper_account(
    client: QMTClient,
    account_id: str,
    initial_cash: float,
    static_prices: dict[str, float],
) -> bool:
    """通过 API 创建模拟账户。"""
    config = {
        "account_id": account_id,
        "initial_cash": initial_cash,
        "price_source": "static",
        "static_prices": static_prices,
        "commission_rate": 0.0003,
        "stamp_tax_rate": 0.0005,
    }
    try:
        resp = client._post("/api/paper_accounts", config)
        logger.info("创建账户 %s 结果: %s", account_id, resp)
        return True
    except Exception as e:
        logger.error("创建账户 %s 失败: %s", account_id, e)
        return False


def download_static_prices(
    client: QMTClient, account_id: str, stock_codes: list[str]
) -> dict[str, float]:
    """调用服务端接口下载静态价格。"""
    try:
        resp = client._post(
            f"/api/paper_accounts/{account_id}/download_prices",
            {"stock_codes": stock_codes},
        )
        data = resp.get("data", {})
        logger.info("账户 %s 下载到 %d 条静态价格", account_id, len(data))
        return data
    except Exception as e:
        logger.warning("账户 %s 下载静态价格失败: %s", account_id, e)
        return {}


def extract_symbols_from_strategy(strategy_path: Path) -> list[str]:
    """简单从策略源码中提取 DEFAULT_STOCKS 列表。

    仅用于演示；生产环境建议通过策略参数传入。
    """
    text = strategy_path.read_text(encoding="utf-8")
    symbols: list[str] = []
    # 查找 DEFAULT_STOCKS = [ ... ]
    start = text.find("DEFAULT_STOCKS = [")
    if start == -1:
        start = text.find("FALLBACK_STOCKS = [")
    if start != -1:
        end = text.find("]", start)
        block = text[start:end + 1]
        # 提取带引号的股票代码
        import re

        symbols = re.findall(r"['\"]([A-Z]{2}\d{6})['\"]", block)
    return symbols


def normalize_to_qmt(symbol: str) -> str:
    """将 SH600519 转换为 600519.SH。"""
    if symbol.startswith("SH"):
        return f"{symbol[2:]}.SH"
    if symbol.startswith("SZ"):
        return f"{symbol[2:]}.SZ"
    return symbol


def run_strategy(
    strategy_path: Path,
    account_id: str,
    args: argparse.Namespace,
    temp_dir: Path,
) -> subprocess.Popen:
    """在子进程中启动单个策略。

    为避免策略文件顶层的 ``load_dotenv(..., override=True)`` 覆盖我们为每个
    账户单独设置的环境变量，这里生成一个临时 wrapper，先设置环境变量并
    把 ``dotenv.load_dotenv`` 替换为空操作，再执行原始策略文件。
    """
    env = os.environ.copy()
    env["QMT_BRIDGE_HOST"] = args.host
    env["QMT_BRIDGE_PORT"] = str(args.port)
    env["QMT_BRIDGE_API_KEY"] = args.api_key
    env["QMT_BRIDGE_TRADING_ACCOUNT_ID"] = account_id
    env["QMT_BRIDGE_PAPER"] = "true"
    env["IS_BACKTESTING"] = "false"
    # 禁用 lumibot credentials 的递归 .env 扫描
    env["LUMIBOT_DISABLE_DOTENV"] = "1"

    wrapper_path = temp_dir / f"wrapper_{strategy_path.stem}_{account_id}.py"
    wrapper_content = f'''"""自动生成的策略 wrapper。"""
import os
import sys

# 环境变量已在子进程 env 中设置；此处再次显式设置以确保策略内可见
os.environ["QMT_BRIDGE_HOST"] = {args.host!r}
os.environ["QMT_BRIDGE_PORT"] = {str(args.port)!r}
os.environ["QMT_BRIDGE_API_KEY"] = {args.api_key!r}
os.environ["QMT_BRIDGE_TRADING_ACCOUNT_ID"] = {account_id!r}
os.environ["QMT_BRIDGE_PAPER"] = "true"
os.environ["IS_BACKTESTING"] = "false"
os.environ["LUMIBOT_DISABLE_DOTENV"] = "1"

# 屏蔽策略文件顶层的 load_dotenv，防止其覆盖上述变量
import dotenv
dotenv.load_dotenv = lambda *args, **kwargs: None

import runpy
runpy.run_path({str(strategy_path)!r}, run_name="__main__")
'''
    wrapper_path.write_text(wrapper_content, encoding="utf-8")

    cmd = [sys.executable, str(wrapper_path)]
    logger.info(
        "启动策略 %s (account=%s): %s", strategy_path.name, account_id, " ".join(cmd)
    )
    proc = subprocess.Popen(
        cmd,
        cwd=str(strategy_path.parent),
        env=env,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
    )
    return proc


def summarize_account(client: QMTClient, account_id: str) -> None:
    """打印账户资产与委托概览。"""
    try:
        asset = client._get("/api/paper_trading/asset", {"account_id": account_id})
        orders = client._get("/api/paper_trading/orders", {"account_id": account_id})
        summary = client._get("/api/paper_trading/summary", {"account_id": account_id})
        logger.info(
            "账户 %s 资产: %s | 委托数: %s | 业绩: %s",
            account_id,
            asset.get("data"),
            len(orders.get("data", [])),
            summary.get("data"),
        )
    except Exception as e:
        logger.error("汇总账户 %s 失败: %s", account_id, e)


def main() -> int:
    args = parse_args()
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s | %(levelname)s | %(message)s",
    )

    if len(args.strategy) != len(args.account_id):
        logger.error("--strategy 与 --account-id 数量必须一致")
        return 1

    server_proc = None
    if not args.no_server:
        server_proc = start_server(args)
        if not wait_for_server(args.host, args.port):
            logger.error("服务端未在预期时间内启动")
            if server_proc:
                server_proc.kill()
            return 1
        logger.info("服务端已就绪")

    client = QMTClient(
        host=args.host,
        port=args.port,
        api_key=args.api_key,
        paper=True,
    )

    # 加载静态价格（若提供）
    static_prices: dict[str, float] = {}
    if args.static_prices_file:
        static_prices = json.loads(Path(args.static_prices_file).read_text())

    # 为每个策略创建账户并补充价格
    strategy_runs = []
    for strategy_path_str, account_id in zip(args.strategy, args.account_id):
        strategy_path = Path(strategy_path_str).expanduser().resolve()
        symbols = extract_symbols_from_strategy(strategy_path)
        qmt_symbols = [normalize_to_qmt(s) for s in symbols]

        create_paper_account(client, account_id, args.initial_cash, static_prices)
        if not static_prices and qmt_symbols:
            download_static_prices(client, account_id, qmt_symbols[:50])

        strategy_runs.append(
            {
                "path": strategy_path,
                "account_id": account_id,
                "proc": None,
            }
        )

    # 启动策略子进程
    with tempfile.TemporaryDirectory() as tmpdir:
        processes = []
        for run in strategy_runs:
            proc = run_strategy(
                run["path"], run["account_id"], args, temp_dir=Path(tmpdir)
            )
            run["proc"] = proc
            processes.append(proc)

        logger.info("已启动 %d 个策略，运行 %d 秒后停止...", len(processes), args.duration)
        time.sleep(args.duration)

        # 终止策略进程
        for run in strategy_runs:
            proc = run["proc"]
            if proc.poll() is None:
                logger.info("终止策略 %s (pid=%s)", run["path"].name, proc.pid)
                proc.terminate()
                try:
                    proc.wait(timeout=10)
                except subprocess.TimeoutExpired:
                    proc.kill()

        # 打印最近日志与账户汇总
        for run in strategy_runs:
            proc = run["proc"]
            if proc.stdout:
                tail = "\n".join(proc.stdout.read().splitlines()[-30:])
                logger.info(
                    "策略 %s (account=%s) 最近日志:\n%s",
                    run["path"].name,
                    run["account_id"],
                    tail,
                )
            summarize_account(client, run["account_id"])

    if server_proc:
        server_proc.terminate()
        try:
            server_proc.wait(timeout=10)
        except subprocess.TimeoutExpired:
            server_proc.kill()

    return 0


if __name__ == "__main__":
    sys.exit(main())
