#!/usr/bin/env python3
"""模拟交易请求队列冒烟测试。

验证多账户并发提交（下单/撤单/查询）经请求队列缓存后由单一工作线程
逐个串行执行的完整链路::

    qmt-server --paper-trading ...   # 先启动服务端
    python scripts/smoke_paper_queue.py --host 127.0.0.1 --port 18099 \
        --api-key test-key

检查项：
1. queue_status 端点可用且工作线程存活
2. N 个账户并发下单全部成功，资金/委托/持仓数据正确
3. 并发资产查询不互相阻塞，结果正确
4. 撤单请求（对已成交委托返回 -1）同样经队列处理不报错
5. 队列统计 processed 计数与请求数吻合、failed 为 0

退出码 0 = 全部通过，1 = 存在失败项。
"""

from __future__ import annotations

import argparse
import json
import sys
import threading
import time
import urllib.error
import urllib.request
from datetime import datetime

# 允许在未安装包时直接从仓库源码运行
sys.path.insert(
    0, str(__import__("pathlib").Path(__file__).resolve().parents[1] / "src")
)

from qmt_bridge.client import QMTClient  # noqa: E402

STOCK_BUY = 23
STOCK_SELL = 24
FIX_PRICE = 11
STOCK_CODE = "000001.SZ"
PRICE = 10.0
VOLUME = 100
INITIAL_CASH = 1_000_000.0

results: list[tuple[str, bool, str]] = []


def check(name: str, ok: bool, detail: str = "") -> None:
    """记录并打印单项检查结果。"""
    results.append((name, ok, detail))
    mark = "✓" if ok else "✗"
    line = f"  [{mark}] {name}"
    if detail:
        line += f" — {detail}"
    print(line, flush=True)


def http_post(url: str, payload: dict, api_key: str) -> dict:
    """发送 JSON POST 请求（stdlib，绕过代理）。"""
    req = urllib.request.Request(
        url,
        data=json.dumps(payload).encode("utf-8"),
        headers={"Content-Type": "application/json", "X-API-Key": api_key},
        method="POST",
    )
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
    with opener.open(req, timeout=30) as resp:
        return json.loads(resp.read().decode("utf-8"))


def main() -> int:
    parser = argparse.ArgumentParser(description="模拟交易请求队列冒烟测试")
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8000)
    parser.add_argument("--api-key", default="")
    parser.add_argument("--accounts", type=int, default=4, help="并发账户数")
    parser.add_argument(
        "--orders-per-account", type=int, default=5, help="每账户下单数"
    )
    args = parser.parse_args()

    base = f"http://{args.host}:{args.port}/api"
    stamp = datetime.now().strftime("%H%M%S")
    account_ids = [f"smoke_{stamp}_{i}" for i in range(args.accounts)]
    total_orders = args.accounts * args.orders_per_account

    client = QMTClient(
        args.host, args.port, api_key=args.api_key, timeout=30, paper=True
    )

    print("== 模拟交易请求队列冒烟测试 ==")
    print(
        f"目标: {base}  账户数: {args.accounts}  每账户下单: {args.orders_per_account}"
    )
    print()

    # ── 1. 队列状态端点 ──
    print("[1] queue_status 端点与工作线程")
    try:
        status = client.get_paper_queue_status()["data"]
        check("queue_status 可访问", True, f"初始状态: {status}")
        check("工作线程存活", bool(status.get("worker_alive")))
    except Exception as exc:
        check("queue_status 可访问", False, repr(exc))
        return _summary()
    print()

    # ── 2. 并发创建账户 ──
    print("[2] 并发创建模拟账户（静态价格源）")
    create_errors: list[str] = []

    def create_account(aid: str) -> None:
        try:
            http_post(
                f"{base}/paper_accounts",
                {
                    "account_id": aid,
                    "initial_cash": INITIAL_CASH,
                    "price_source": "static",
                    "static_prices": {STOCK_CODE: PRICE},
                    "commission_rate": 0.0,
                    "min_commission": 0.0,
                    "stamp_tax_rate": 0.0,
                },
                args.api_key,
            )
        except Exception as exc:  # noqa: BLE001
            create_errors.append(f"{aid}: {exc!r}")

    _run_threads(
        [threading.Thread(target=create_account, args=(aid,)) for aid in account_ids]
    )
    check(
        f"创建 {len(account_ids)} 个账户全部成功",
        not create_errors,
        "; ".join(create_errors),
    )
    print()

    # ── 3. 并发下单（所有账户同时提交，队列逐个处理）──
    print(
        f"[3] {total_orders} 笔订单并发提交（{args.accounts} 账户 × {args.orders_per_account} 笔）"
    )
    order_latencies: list[float] = []
    order_errors: list[str] = []
    lat_lock = threading.Lock()

    def submit_order(idx: int) -> None:
        aid = account_ids[idx % args.accounts]
        started = time.monotonic()
        try:
            resp = client.place_order(
                stock_code=STOCK_CODE,
                order_type=STOCK_BUY,
                order_volume=VOLUME,
                price_type=FIX_PRICE,
                price=PRICE,
                account_id=aid,
            )
            if not resp.get("order_id", 0) > 0:
                order_errors.append(f"{aid}: 响应无 order_id: {resp}")
        except Exception as exc:  # noqa: BLE001
            order_errors.append(f"{aid}: {exc!r}")
        finally:
            with lat_lock:
                order_latencies.append(time.monotonic() - started)

    barrier = threading.Barrier(total_orders)

    def runner(idx: int) -> None:
        barrier.wait(timeout=30)  # 尽量同时开抢
        submit_order(idx)

    _run_threads(
        [threading.Thread(target=runner, args=(i,)) for i in range(total_orders)],
        join_timeout=60,
    )
    check(
        f"{total_orders} 笔订单全部提交成功",
        not order_errors,
        "; ".join(order_errors[:3]),
    )
    avg_ms = (
        sum(order_latencies) / len(order_latencies) * 1000 if order_latencies else 0
    )
    max_ms = max(order_latencies) * 1000 if order_latencies else 0
    check(
        "提交延迟统计",
        not order_errors,
        f"avg={avg_ms:.1f}ms max={max_ms:.1f}ms（含排队等待）",
    )
    print()

    # ── 4. 并发查询资产与委托 ──
    print("[4] 并发查询资产/委托/队列状态")
    query_errors: list[str] = []
    expected_cash = INITIAL_CASH - args.orders_per_account * VOLUME * PRICE

    def query_account(aid: str) -> None:
        try:
            asset = client.query_asset(account_id=aid)["data"]
            if asset is None or asset["cash"] != expected_cash:
                query_errors.append(
                    f"{aid}: cash={asset and asset['cash']} 期望 {expected_cash}"
                )
            orders = client.query_orders(account_id=aid)["data"]
            if len(orders) != args.orders_per_account:
                query_errors.append(
                    f"{aid}: 委托数 {len(orders)} 期望 {args.orders_per_account}"
                )
            positions = client.query_positions(account_id=aid)["data"]
            if (
                len(positions) != 1
                or positions[0]["volume"] != args.orders_per_account * VOLUME
            ):
                query_errors.append(f"{aid}: 持仓数据异常: {positions}")
        except Exception as exc:  # noqa: BLE001
            query_errors.append(f"{aid}: {exc!r}")

    _run_threads(
        [threading.Thread(target=query_account, args=(aid,)) for aid in account_ids]
    )
    check("各账户资金/委托/持仓正确", not query_errors, "; ".join(query_errors[:3]))
    print()

    # ── 5. 撤单路径（已成交委托返回 -1，验证经队列处理不异常）──
    print("[5] 撤单请求经队列处理")
    cancel_errors: list[str] = []

    def cancel_first(aid: str) -> None:
        try:
            orders = client.query_orders(account_id=aid)["data"]
            order_id = orders[0]["order_id"]
            resp = client.cancel_order(order_id=order_id, account_id=aid)
            if resp.get("data") not in (0, -1):
                cancel_errors.append(f"{aid}: 意外撤单结果 {resp}")
        except Exception as exc:  # noqa: BLE001
            cancel_errors.append(f"{aid}: {exc!r}")

    _run_threads(
        [threading.Thread(target=cancel_first, args=(aid,)) for aid in account_ids]
    )
    check("撤单并发执行无异常", not cancel_errors, "; ".join(cancel_errors[:3]))
    print()

    # ── 6. 队列统计 ──
    print("[6] 最终队列统计")
    final = client.get_paper_queue_status()["data"]
    expected_min = total_orders + len(account_ids) * 4  # 下单+资产+委托+持仓+撤单
    check(
        "processed 计数覆盖全部请求",
        final["processed"] >= expected_min,
        f"processed={final['processed']} (≥{expected_min}), "
        f"failed={final['failed']}, peak_size={final['peak_size']}",
    )
    check("无失败请求", final["failed"] == 0, f"failed={final['failed']}")
    check(
        "队列已清空",
        final["queue_size"] == 0,
        f"queue_size={final['queue_size']}",
    )
    if final["peak_size"] > 1:
        check(
            "请求确实并发入队缓存",
            True,
            f"峰值队列长度 {final['peak_size']} > 1（多请求同时排队）",
        )
    else:
        print(
            "  [i] 峰值队列长度为 1（处理快于提交，不构成失败；"
            "串行性已由单元测试保证）"
        )
    print()

    return _summary()


def _run_threads(threads: list[threading.Thread], join_timeout: float = 30) -> None:
    """启动全部线程并等待结束。"""
    for t in threads:
        t.start()
    for t in threads:
        t.join(join_timeout)


def _summary() -> int:
    """打印汇总并返回退出码。"""
    passed = sum(1 for _, ok, _ in results if ok)
    total = len(results)
    print(f"== 结果: {passed}/{total} 项通过 ==")
    for name, ok, detail in results:
        if not ok:
            print(f"  失败: {name} — {detail}")
    return 0 if passed == total else 1


if __name__ == "__main__":
    sys.exit(main())
