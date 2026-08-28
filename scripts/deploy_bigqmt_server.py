#!/usr/bin/env python3
"""部署 xtquant_big_convert 服务端到完整版 QMT（big QMT）内置 Python。

按 ``docs/big-qmt.md`` §3.5：从已安装的 ``xtquant-big-convert`` 包中提取
QMT 侧运行所需的文件，复制到 QMT 客户端 ``python`` 目录（内置 Python 3.6
沙箱），并生成 ``bigqmt_signal_trader_local_config.py`` 配置模板（账户 ID +
Redis 配置；含敏感信息，绝不覆盖、绝不入库）。

用法示例（在 Windows qmt-server 机器上，已 ``pip install -e ".[server,bigqmt]"``）::

    # 部署到完整版 QMT（默认路径可自动探测常见安装位置）
    python scripts/deploy_bigqmt_server.py --qmt-python-dir "D:\\国金QMT交易端\\userdata_mini\\..\\python"

    # 先看会复制哪些文件，不落盘
    python scripts/deploy_bigqmt_server.py --dry-run

    # 显式指定账户与 Redis（仅首次生成模板时生效）
    python scripts/deploy_bigqmt_server.py --account-id 12345678 \\
        --redis-host 127.0.0.1 --redis-port 6379 --redis-db 5

部署内容：

- ``bigqmt_signal_trader/``            RPC 服务端包（QMT 内置 Python 3.6）
- ``BIGQMT_REDIS_DRYRUN.py``           QMT 侧入口（策略沙箱加载器）
- ``bigqmt_signal_trader_strategy.py`` 策略包装（init/handlebar/adjust 驱动）
- ``bigqmt_signal_trader_redis_rpc_runtime.py`` / ``*_dryrun.py`` 等顶层模块

注意：
- **源解析**优先 ``import bigqmt_signal_trader`` 的实际落点（editable 安装是
  常态：PEP 660 下 ``dist.files`` 只有 finder 记录，locate_file 会指到
  site-packages 里并不存在的路径）；wheel 常规安装退回元数据定位。
  两者都不行时用 ``--source-dir`` 显式指定（如源码 checkout 的 ``src/``）。
- **覆盖前自动备份**已存在的受管文件（包 + 顶层入口，不含 local_config）
  到 ``%USERPROFILE%\\bigqmt\\backup\\bigqmt_server_pre_<时间戳>.zip``。
- 复制后清理目标包内的 ``__pycache__``；上游已删除而目标还留着的文件会打
  ``[遗留]`` 告警（只告警不删，删除动作留给人）。
- **不复制** wheel 附带的顶层 ``xtquant/`` 目录 —— QMT 目录里是真实 xtquant；
  qmt-bridge 的行情通道由 ``server/xtdata_source.py`` 直接解析
  xtquant-big-convert 包（editable 安装 → 源码 src/ 本体），用不到它。
- QMT 侧 Python 3.6 只需所选 transport 的依赖：Redis 模式为纯 Python
  （redis-py 3.5.x，可手动复制包目录规避旧 OpenSSL SSL 问题）。
- 下单默认关闭：QMT 侧配置 ``rpc_allow_order_methods: False``（默认），
  需显式开启（docs/big-qmt.md §5.3 灰度开关）。
"""

from __future__ import annotations

import argparse
import shutil
import sys
import zipfile
from datetime import datetime
from importlib import metadata
from pathlib import Path

# QMT 侧需要复制的顶层单文件模块（bigqmt_backtest 为客户端回测包，不部署）
TOP_LEVEL_FILES = [
    "BIGQMT_REDIS_DRYRUN.py",
    "bigqmt_signal_trader_strategy.py",
    "bigqmt_signal_trader_redis_rpc_runtime.py",
    "bigqmt_signal_trader_redis_dryrun.py",
    "bigqmt_signal_trader_dryrun.py",
    "bigqmt_signal_trader_diagnostic.py",
]
# QMT 侧需要复制的包目录
PACKAGES = ["bigqmt_signal_trader"]

LOCAL_CONFIG_TEMPLATE = '''#coding:utf-8
"""bigqmt_signal_trader 本地配置（bigqmt_signal_trader_local_config.py）。

由 scripts/deploy_bigqmt_server.py 生成，包含账户与 Redis 连接信息，
请勿提交到版本控制（本文件位于 QMT 安装目录，天然在仓库之外）。
"""

# 完整版 QMT 绑定的资金账号（单实例单账户）
BIGQMT_ACCOUNT_ID = "{account_id}"

BIGQMT_REDIS_CONFIG = {{
    "host": "{redis_host}",
    "port": {redis_port},
    "db": {redis_db},
    # "username": "",
    # "password": "",
    # "protocol": 2,  # redis-py 8.x 默认 RESP3，Redis 5.0 只支持 RESP2

    # 下单方法（submit_order / cancel_order）门控，默认关闭 ——
    # 灰度验证字段对比通过后再改 True 开启下单（docs/big-qmt.md §6.4）
    "rpc_allow_order_methods": False,
}}

# RPC 超时（秒），默认 6.0；下载大窗口数据时可调大
# BIGQMT_RPC_TIMEOUT_SECONDS = 6.0
'''


def _dist_files() -> Path:
    """定位已安装 xtquant-big-convert 的文件根目录（wheel 常规安装路径）。"""
    try:
        dist = metadata.distribution("xtquant-big-convert")
    except metadata.PackageNotFoundError:
        raise SystemExit(
            "未找到已安装的 xtquant-big-convert，请先：pip install 'qmt-bridge[bigqmt]'"
        )
    # 任取包内一个文件定位 site-packages 根
    for f in dist.files or []:
        if f.name == "xtquant_compat.py" and "bigqmt_signal_trader" in str(f):
            root = dist.locate_file(f).parent.parent
            return Path(root)
    raise SystemExit(
        "xtquant-big-convert 安装内容异常：找不到 bigqmt_signal_trader 包。\n"
        "editable 安装（dist.files 只有 finder 记录）会走到这里 ——"
        "请用 --source-dir 显式指定源码 src/ 目录"
    )


def _resolve_source() -> Path:
    """三级解析 xtquant-big-convert 源根目录。

    1. ``import bigqmt_signal_trader`` 的实际落点 —— editable 安装（常态）
       最可靠：包解析到哪就部署哪，客户端/服务端天然同版本。
    2. wheel 元数据（``_dist_files``）—— 常规安装。
    3. 都失败 → 报错（提示 --source-dir）。
    """
    try:
        import bigqmt_signal_trader

        pkg_file = Path(bigqmt_signal_trader.__file__).resolve()
        root = pkg_file.parent.parent
        if (root / "bigqmt_signal_trader").is_dir():
            return root
        print(f"[提示] import 解析到异常位置 {root}，退回 wheel 元数据定位")
    except ImportError:
        pass
    return _dist_files()


def _source_version() -> str:
    """best-effort 读已安装版本号，仅用于部署日志留痕。"""
    try:
        return metadata.version("xtquant-big-convert")
    except Exception:
        return "?"


def _backup_existing(qmt_dir: Path, backup_dir: Path) -> Path:
    """把已存在的受管文件打包备份，返回 zip 路径（无内容可备时返回 None）。"""
    targets = []
    pkg_dir = qmt_dir / "bigqmt_signal_trader"
    if pkg_dir.is_dir():
        targets.append(pkg_dir)
    for name in TOP_LEVEL_FILES:
        if (qmt_dir / name).is_file():
            targets.append(qmt_dir / name)
    if not targets:
        return None
    backup_dir.mkdir(parents=True, exist_ok=True)
    zip_path = backup_dir / (
        "bigqmt_server_pre_%s.zip" % datetime.now().strftime("%Y%m%d_%H%M%S")
    )
    with zipfile.ZipFile(zip_path, "w", zipfile.ZIP_DEFLATED) as zf:
        for t in targets:
            files = (
                [t] if t.is_file()
                else sorted(
                    p for p in t.rglob("*")
                    if p.is_file() and "__pycache__" not in p.parts
                )
            )
            for p in files:
                zf.write(p, p.relative_to(qmt_dir))
    return zip_path


def _report_leftovers(source: Path, qmt_dir: Path) -> list:
    """目标里存在、上游已没有的受管文件（上游改名/删除后残留）。"""
    left = []
    src_pkg = source / "bigqmt_signal_trader"
    dst_pkg = qmt_dir / "bigqmt_signal_trader"
    if src_pkg.is_dir() and dst_pkg.is_dir():
        src_files = {
            p.relative_to(src_pkg).as_posix()
            for p in src_pkg.rglob("*")
            if p.is_file() and "__pycache__" not in p.parts
        }
        for p in sorted(dst_pkg.rglob("*.py")):
            if "__pycache__" in p.parts:
                continue
            rel = p.relative_to(dst_pkg).as_posix()
            if rel not in src_files:
                left.append("bigqmt_signal_trader/" + rel)
    for name in TOP_LEVEL_FILES:
        if (qmt_dir / name).is_file() and not (source / name).is_file():
            left.append(name)
    return left


def _copy_tree(src: Path, dst: Path, dry_run: bool) -> int:
    """复制包目录（跳过 __pycache__），返回复制的文件数。"""
    count = 0
    for item in sorted(src.rglob("*")):
        if "__pycache__" in item.parts:
            continue
        if item.is_dir():
            continue
        rel = item.relative_to(src)
        target = dst / rel
        print(f"  {rel}")
        if not dry_run:
            target.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(item, target)
        count += 1
    return count


def main() -> int:
    parser = argparse.ArgumentParser(
        description="部署 xtquant_big_convert 服务端到完整版 QMT 内置 Python（docs/big-qmt.md §3.5）"
    )
    parser.add_argument(
        "--qmt-python-dir",
        default="",
        help="完整版 QMT 客户端的 python 目录（内置 Python 3.6 沙箱）",
    )
    parser.add_argument(
        "--source-dir",
        default="",
        help="xtquant-big-convert 源目录（默认：import 解析处 → pip 元数据）",
    )
    parser.add_argument(
        "--backup-dir",
        default=str(Path.home() / "bigqmt" / "backup"),
        help="覆盖前备份旧受管文件的目录（默认 %%USERPROFILE%%\\bigqmt\\backup）",
    )
    parser.add_argument(
        "--no-backup", action="store_true", help="跳过覆盖前备份（不推荐）"
    )
    parser.add_argument(
        "--account-id",
        default="",
        help="完整版 QMT 绑定的资金账号（写入 local_config 模板）",
    )
    parser.add_argument("--redis-host", default="127.0.0.1", help="Redis 地址")
    parser.add_argument("--redis-port", type=int, default=6379, help="Redis 端口")
    parser.add_argument("--redis-db", type=int, default=5, help="Redis db")
    parser.add_argument(
        "--dry-run", action="store_true", help="只列出将复制的文件，不落盘"
    )
    args = parser.parse_args()

    source = Path(args.source_dir) if args.source_dir else _resolve_source()
    if not (source / "bigqmt_signal_trader").is_dir():
        raise SystemExit(f"源目录异常，缺少 bigqmt_signal_trader 包：{source}")
    print(f"源（xtquant-big-convert {_source_version()}）：{source}")

    qmt_dir = Path(args.qmt_python_dir) if args.qmt_python_dir else None
    if args.dry_run and qmt_dir is None:
        qmt_dir = Path("(dry-run 未指定 --qmt-python-dir)")
    if qmt_dir is None:
        raise SystemExit("必须指定 --qmt-python-dir（完整版 QMT 客户端的 python 目录）")

    print(f"目标（完整版 QMT 内置 Python）：{qmt_dir}\n")

    if not args.dry_run and args.qmt_python_dir:
        qmt_dir.mkdir(parents=True, exist_ok=True)

    # 0. 覆盖前备份已存在的受管文件（包 + 顶层入口；local_config 不在
    #    受管清单里，本来就不覆盖，凭据不进备份 zip）
    if not args.dry_run and args.qmt_python_dir and not args.no_backup:
        zip_path = _backup_existing(qmt_dir, Path(args.backup_dir))
        if zip_path:
            print(f"[备份] 旧受管文件 -> {zip_path}")

    # 1. 复制 QMT 侧服务端包与顶层入口模块
    total = 0
    for pkg in PACKAGES:
        print(f"[包] {pkg}/")
        total += _copy_tree(source / pkg, qmt_dir / pkg, args.dry_run)
        if not args.dry_run and args.qmt_python_dir:
            # 清理目标包内 __pycache__：旧字节码不会被 py3 孤用（只认与
            # 源配套的 pyc），但留着会干扰人工 diff 与目录对比
            for cache in sorted((qmt_dir / pkg).rglob("__pycache__")):
                shutil.rmtree(cache, ignore_errors=True)
    for name in TOP_LEVEL_FILES:
        src_file = source / name
        if not src_file.is_file():
            print(f"[跳过] {name}（上游版本无此文件）")
            continue
        print(f"[文件] {name}")
        if not args.dry_run and args.qmt_python_dir:
            shutil.copy2(src_file, qmt_dir / name)
        total += 1

    # 1b. 遗留告警：上游已删除而目标目录还留着的受管文件（改名/精简后
    #     残留；旧模块仍可被 import，值得暴露出来 —— 只告警，删除留给人）
    if args.qmt_python_dir:
        for name in _report_leftovers(source, qmt_dir):
            print(f"[遗留] {name}（上游已无此文件，可手工删除）")

    # 2. 生成 local_config 模板（已存在则保留，绝不覆盖敏感配置）
    config_path = qmt_dir / "bigqmt_signal_trader_local_config.py"
    if config_path.is_file():
        print(f"\n[保留] {config_path} 已存在，不覆盖")
    else:
        print(f"\n[生成] {config_path}（账户 ID + Redis 配置模板）")
        if not args.dry_run and args.qmt_python_dir:
            config_path.write_text(
                LOCAL_CONFIG_TEMPLATE.format(
                    account_id=args.account_id or "YOUR_ACCOUNT_ID",
                    redis_host=args.redis_host,
                    redis_port=args.redis_port,
                    redis_db=args.redis_db,
                ),
                encoding="utf-8",
            )

    verb = "将复制" if args.dry_run else "已复制"
    print(f"\n完成：{verb} {total} 个文件")
    print(
        "后续步骤（docs/big-qmt.md §6）：\n"
        "  1. 在 QMT 内置 Python 中运行 BIGQMT_REDIS_DRYRUN.py（保持下单关闭）\n"
        "  2. qmt-server 侧并行运行 mini / bigqmt 双后端，逐字段比对\n"
        "  3. 比对通过后把 local_config 中 rpc_allow_order_methods 改 True 灰度下单"
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
