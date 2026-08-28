#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""用完整版 QMT（big QMT）本体下载历史行情 —— 全脚本化。

原理（2026-08-28 实测验证）
--------------------------
下载这一步由 **QMT 安装自带的内置解释器 + 内置 xtquant** 执行
（``<QMT>\\bin.x64\\pythonw.exe`` + ``bin.x64\\Lib\\site-packages\\xtquant``），
调的也是同一套安装的数据服务 —— 全程不引入任何外部 Python / 外部 xtquant。
（实测推翻了上游"完整版终端 xtdata 下载不可达"的说法：本机可以下。）

文件落点的一个事实：xtquant 写入的是安装的数据服务仓
``<QMT>\\userdata_mini\\datadir``，而客户端 UI（图表/数据管理）写
``<QMT>\\datadir``，FormulaServer（bigqmt 行情读取，58600）**只认后者**。
两个仓同属一个安装、格式完全一致（同一 bin.x64 生成），所以脚本第二步
把文件从前者同步到后者 —— 纯文件复制，即刻生效，无需重启 QMT。

    ① bin.x64\\pythonw.exe（QMT 本体解释器）跑内置 xtquant 下载
       → userdata_mini\\datadir\\SH\\86400\\600519.DAT
    ② 复制到 datadir\\SH\\86400\\600519.DAT
    ③ bigqmt 后端 / FormulaServer 立即可读

用法示例
--------
    # 下载沪深300 日线（默认近一年）：
    py scripts/download_bigqmt_data.py --stocks 000300.SH

    # 多标的、多周期、指定区间：
    py scripts/download_bigqmt_data.py --stocks 000300.SH,600519.SH \\
        --periods 1d,1m --start 20250101 --end 20260828

注意
----
- 需要 QMT 客户端在运行（其数据服务可达；XtMiniQmt 进程由客户端自带）。
- 周期支持：1m/5m/15m/30m/1h/1d（tick 文件命名不同，暂不支持）。
- 下载是增量的（incrementally）：重复执行只补缺口；同步只复制新增/变化文件。
- 外部 Python 只是"跑本脚本的壳"：下载动作在 QMT 内置解释器里完成
  （找不到内置解释器时回退到外部 xtquant 并告警 —— 用的仍是同一数据服务）。
"""

from __future__ import annotations

import argparse
import json
import shutil
import subprocess
import sys
import tempfile
import time
from pathlib import Path

# K 线周期 → 数据仓目录名（秒）
PERIOD_DIRS = {
    "1m": "60",
    "5m": "300",
    "15m": "900",
    "30m": "1800",
    "1h": "3600",
    "60m": "3600",
    "1d": "86400",
}

# 代码后缀 → 市场目录名（BJ 未实测，仅放行）
SUFFIX_MARKETS = {"SH": "SH", "SZ": "SZ", "BJ": "BJ"}

# 在 QMT 内置解释器里执行的下载数据（写日志文件回传结果；PS5.1 传参
# 会撕裂多行代码，故整段落盘成 .py —— 与 deploy_bigqmt_windows.ps1 同法）
_INNER_CODE = r'''
# -*- coding: utf-8 -*-
import sys, time, json
log_path, site_dir, stocks_json, periods_json, start, end, wait_secs = sys.argv[1:8]
log = open(log_path, "w")
def w(obj):
    log.write(json.dumps(obj) + "\n"); log.flush()
try:
    sys.path.insert(0, site_dir)
    from xtquant import xtdata
    stocks = json.loads(stocks_json); periods = json.loads(periods_json)
    for period in periods:
        try:
            try:
                r = xtdata.download_history_data2(stocks, period, start, end, incrementally=True)
            except TypeError:
                # 内置 xtquant（Py3.6 老版本）无 incrementally 参数 —— 退化为全量
                r = xtdata.download_history_data2(stocks, period, start, end)
            w({"ev": "dl", "period": period, "ret": str(r)})
        except Exception as exc:
            w({"ev": "dl_err", "period": period, "err": repr(exc)})
    deadline = time.time() + float(wait_secs)
    while time.time() < deadline:
        time.sleep(2)
        done = {}
        for period in periods:
            try:
                data = xtdata.get_market_data_ex(["close"], stocks, period=period, count=1)
                done[period] = {s: (0 if data.get(s) is None else len(data[s])) for s in stocks}
            except Exception:
                done[period] = {}
        w({"ev": "rows", "counts": done})
        if all(n >= 1 for p in done.values() for n in p.values()):
            break
    w({"ev": "done"})
except Exception:
    import traceback
    w({"ev": "fatal", "err": traceback.format_exc()})
log.close()
'''


def _market_of(stock: str) -> str:
    suffix = stock.rsplit(".", 1)[-1].upper() if "." in stock else ""
    market = SUFFIX_MARKETS.get(suffix)
    if not market:
        raise SystemExit(f"不认识的市场后缀: {stock}（支持 {'/'.join(SUFFIX_MARKETS)}）")
    return market


def _files_for(qmt_root: Path, stock: str, period: str) -> tuple[Path, Path]:
    """返回 (数据服务仓源文件, 客户端仓目标文件)。"""
    code = stock.split(".")[0]
    subdir = PERIOD_DIRS[period]
    src = qmt_root / "userdata_mini" / "datadir" / _market_of(stock) / subdir / f"{code}.DAT"
    dst = qmt_root / "datadir" / _market_of(stock) / subdir / f"{code}.DAT"
    return src, dst


def download_via_qmt(
    qmt_root: Path, stocks: list[str], periods: list[str], start: str, end: str, wait: int
) -> None:
    """在 QMT 内置解释器里执行下载（pythonw 无控制台 → 结果写日志回读）。"""
    pythonw = qmt_root / "bin.x64" / "pythonw.exe"
    site_dir = str(qmt_root / "bin.x64" / "Lib" / "site-packages")
    log_path = Path(tempfile.gettempdir()) / "bigqmt_dl_inner.log"
    log_path.unlink(missing_ok=True)

    inner = Path(tempfile.gettempdir()) / "bigqmt_dl_inner.py"
    inner.write_text(_INNER_CODE, encoding="utf-8")

    args = [
        str(pythonw), str(inner), str(log_path), site_dir,
        json.dumps(stocks), json.dumps(periods), start, end, str(wait),
    ]
    if pythonw.is_file():
        print("  [dl] 经 QMT 内置解释器下载（bin.x64\\pythonw.exe + 内置 xtquant）")
        subprocess.Popen(args)  # GUI 子系统：不等待，靠轮询日志
    else:
        print("  [!!] 未找到 QMT 内置解释器，回退外部 xtquant（同一数据服务）")
        try:
            from xtquant import xtdata  # noqa: F401
        except ImportError:
            raise SystemExit("外部 xtquant 也不可用 —— 检查 QMT 安装路径 --qmt-root")
        from xtquant import xtdata

        def _external():
            for period in periods:
                xtdata.download_history_data2(stocks, period, start, end, incrementally=True)
        _external()
        return

    # 轮询内部日志：等 done 事件或超时（pythonw 起得慢，先等文件出现）
    deadline = time.time() + wait + 60
    last_line = ""
    while time.time() < deadline:
        time.sleep(2)
        if not log_path.is_file():
            continue
        lines = log_path.read_text(encoding="utf-8", errors="replace").strip().splitlines()
        if not lines:
            continue
        for line in lines:
            if line == last_line:
                continue
            last_line = line
            try:
                ev = json.loads(line)
            except ValueError:
                continue
            kind = ev.get("ev")
            if kind == "rows":
                counts = ev.get("counts", {})
                brief = {p: sum(1 for n in pc.values() if n >= 1) for p, pc in counts.items()}
                print(f"  [dl] 已就绪标的数(按周期): {brief}")
            elif kind == "dl_err":
                print(f"  [!!] 下载异常: {ev}")
            elif kind == "fatal":
                print(f"  [XX] 内置解释器失败: {ev.get('err', '')[:300]}")
                return
            elif kind == "done":
                print("  [dl] 内置下载流程结束")
                return


def sync_to_big(qmt_root: Path, stocks: list[str], periods: list[str]) -> int:
    """数据服务仓 → 客户端仓 同步：仅复制新增/尺寸变化的文件（幂等）。"""
    copied = 0
    for stock in stocks:
        for period in periods:
            src, dst = _files_for(qmt_root, stock, period)
            if not src.is_file() or src.stat().st_size == 0:
                print(f"  [skip] {stock} {period}: 无下载产物")
                continue
            if dst.is_file() and dst.stat().st_size == src.stat().st_size:
                print(f"  [keep] {stock} {period}: 已同步（{src.stat().st_size}B）")
                continue
            dst.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(src, dst)
            copied += 1
            print(f"  [copy] {stock} {period}: -> {dst}（{src.stat().st_size}B）")
    return copied


def main() -> int:
    parser = argparse.ArgumentParser(
        description="用 QMT 本体（内置解释器+内置 xtquant）下载历史行情，并同步到客户端数据仓",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=(
            "big 仓读回验证（bigqmt 后端，QMT 客户端在跑即可）：\n"
            "  curl \"http://127.0.0.1:18888/api/market/market_data_ex?stocks=000300.SH&period=1d&count=5\""
        ),
    )
    parser.add_argument("--stocks", required=True, help="逗号分隔，如 000300.SH,600519.SH")
    parser.add_argument("--periods", default="1d", help="逗号分隔: 1m/5m/15m/30m/1h/1d（默认 1d）")
    parser.add_argument("--start", default="", help="YYYYMMDD（默认由 xtdata 自定，通常近一年）")
    parser.add_argument("--end", default="", help="YYYYMMDD（默认至今）")
    parser.add_argument(
        "--qmt-root", default=r"C:\QMT_Simulator", help="完整版 QMT 安装根目录（含 bin.x64 与两个 datadir）"
    )
    parser.add_argument("--wait", type=int, default=180, help="等待下载完成的秒数（默认 180）")
    parser.add_argument("--sync-only", action="store_true", help="只做仓间同步，不触发下载")
    args = parser.parse_args()

    stocks = [s.strip() for s in args.stocks.split(",") if s.strip()]
    periods = [p.strip() for p in args.periods.split(",") if p.strip()]
    bad = [p for p in periods if p not in PERIOD_DIRS]
    if bad:
        raise SystemExit(f"不支持的周期: {bad}（K线周期 {sorted(PERIOD_DIRS)}；tick 不支持）")
    qmt_root = Path(args.qmt_root)

    if not args.sync_only:
        print(f"==> 下载 {len(stocks)} 只 × {periods}（QMT 本体通道）")
        download_via_qmt(qmt_root, stocks, periods, args.start, args.end, args.wait)

    print("==> 同步到客户端数据仓（bigqmt 后端的数据源）")
    copied = sync_to_big(qmt_root, stocks, periods)
    print(f"完成：新同步 {copied} 个文件。读回：见 --help 尾部 curl 示例。")
    return 0


if __name__ == "__main__":
    sys.exit(main())
