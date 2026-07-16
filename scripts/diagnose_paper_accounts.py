"""诊断模拟交易账户在仪表盘/服务中的可见性。

用法示例：

    # 检查本地默认数据目录
    python scripts/diagnose_paper_accounts.py

    # 检查指定数据目录
    python scripts/diagnose_paper_accounts.py --data-dir /path/to/paper_trading

    # 同时对比远程 qmt-server 的 paper_accounts 接口
    python scripts/diagnose_paper_accounts.py --host wellwell.zicp.fun --port 9003 --api-key xxx

输出说明：
    - 本地目录下的账户子目录
    - 每个子目录是否有 summary/summary.json 和 order/*.csv
    - 哪些账户会被 ``list_account_ids`` 识别并在仪表盘展示
    - 与远程 ``/api/paper_accounts`` 或 ``/api/paper_trading/summaries`` 的对比（若提供 host）
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

# 让脚本可以 import dashboard/paper-trading 下的模块
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "dashboard" / "paper-trading"))

from data_loader import load_all_orders, load_summary, resolve_data_dir


def _scan_accounts(data_dir: Path) -> list[dict]:
    """扫描数据目录，返回每个账户的可见性信息。"""
    rows = []
    if not data_dir.exists():
        return rows

    for path in sorted(data_dir.iterdir()):
        if not path.is_dir():
            continue
        account_id = path.name
        summary_path = path / "summary" / "summary.json"
        orders_dir = path / "order"
        has_summary = summary_path.exists()
        has_orders = orders_dir.exists() and any(orders_dir.iterdir())
        summary = load_summary(data_dir, account_id)
        orders = load_all_orders(data_dir, account_id)
        rows.append(
            {
                "account_id": account_id,
                "has_summary": has_summary,
                "has_orders": has_orders,
                "visible": has_summary or has_orders,
                "summary_total_asset": summary.get("total_asset"),
                "summary_total_trades": summary.get("total_trades"),
                "order_count": len(orders),
            }
        )
    return rows


def _fetch_remote_accounts(host: str, port: int, api_key: str | None = None) -> set[str]:
    """尝试从远程 qmt-server 拉取模拟账户列表。"""
    try:
        import urllib.request
    except ImportError:
        print("错误：无法导入 urllib，无法查询远程服务")
        return set()

    url = f"http://{host}:{port}/api/paper_accounts"
    headers = {}
    if api_key:
        headers["X-API-Key"] = api_key

    try:
        req = urllib.request.Request(url, headers=headers)
        with urllib.request.urlopen(req, timeout=10) as resp:
            data = json.loads(resp.read().decode("utf-8"))
    except Exception as exc:
        print(f"远程接口 {url} 查询失败: {exc}")
        return set()

    accounts = data.get("data", []) if isinstance(data, dict) else data
    return {a.get("account_id", "") for a in accounts if isinstance(a, dict)}


def main() -> int:
    parser = argparse.ArgumentParser(description="诊断模拟交易账户可见性")
    parser.add_argument("--data-dir", help="模拟交易数据目录，默认使用项目 data/paper_trading")
    parser.add_argument("--host", help="远程 qmt-server 主机名，用于对比")
    parser.add_argument("--port", type=int, default=8000, help="远程 qmt-server 端口")
    parser.add_argument("--api-key", help="远程服务 API Key（如需认证）")
    args = parser.parse_args()

    data_dir = resolve_data_dir(args.data_dir)
    print(f"数据目录: {data_dir}")
    print(f"目录存在: {data_dir.exists()}")
    print()

    rows = _scan_accounts(data_dir)
    total_dirs = len(rows)
    visible = [r for r in rows if r["visible"]]
    invisible = [r for r in rows if not r["visible"]]

    print(f"账户子目录总数: {total_dirs}")
    print(f"会在仪表盘展示: {len(visible)}")
    print(f"不会展示（缺少 summary 和 orders）: {len(invisible)}")
    print()

    print("-" * 100)
    print(f"{'账户 ID':<55s} {'summary':>8s} {'orders':>8s} {'展示':>6s} {'总资产':>12s} {'成交笔数':>8s} {'委托行数':>8s}")
    print("-" * 100)
    for r in rows:
        print(
            f"{r['account_id']:<55s} "
            f"{'Y' if r['has_summary'] else 'N':>8s} "
            f"{'Y' if r['has_orders'] else 'N':>8s} "
            f"{'Y' if r['visible'] else 'N':>6s} "
            f"{r['summary_total_asset'] if r['summary_total_asset'] is not None else '-':>12} "
            f"{r['summary_total_trades'] if r['summary_total_trades'] is not None else '-':>8} "
            f"{r['order_count']:>8d}"
        )

    if invisible:
        print()
        print("以下账户不会展示，因为它们既缺少 summary/summary.json，也缺少 order/*.csv：")
        for r in invisible:
            print(f"  - {r['account_id']}")

    if args.host:
        print()
        print(f"正在对比远程服务 http://{args.host}:{args.port} ...")
        remote_accounts = _fetch_remote_accounts(args.host, args.port, args.api_key)
        local_accounts = {r["account_id"] for r in rows}
        only_local = sorted(local_accounts - remote_accounts)
        only_remote = sorted(remote_accounts - local_accounts)

        print(f"本地账户数: {len(local_accounts)}")
        print(f"远程账户数: {len(remote_accounts)}")
        if only_local:
            print(f"仅在本地出现 ({len(only_local)} 个)：")
            for aid in only_local:
                print(f"  - {aid}")
        if only_remote:
            print(f"仅在远程出现 ({len(only_remote)} 个)：")
            for aid in only_remote:
                print(f"  - {aid}")
        if not only_local and not only_remote:
            print("本地与远程账户列表一致。")

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
