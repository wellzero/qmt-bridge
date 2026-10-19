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

    # 显式指定账户与 Redis（仅首次生成模板时生效；--rpc-transport redis 时）
    python scripts/deploy_bigqmt_server.py --account-id 12345678 \\
        --rpc-transport redis --redis-host 127.0.0.1 --redis-port 6379 --redis-db 5

    # 沙箱终端（禁 open() 的 QMT 模拟端）：部署单文件扁平包
    python scripts/deploy_bigqmt_server.py --account-id 12345678 \\
        --entry singlefile --qmt-python-dir "C:\\QMT_Simulator\\python"

    # 沙箱禁 open() 但放行 CRT/Win32 读文件的终端：pipe 多文件形态
    # （BIGQMT_PIPE_DRYRUN.py + bigqmt_pipe_signal_trader/ 模块树，与扁平包同源）
    python scripts/deploy_bigqmt_server.py --account-id 12345678 \\
        --entry pipe_dryrun --qmt-python-dir "C:\\QMT_Simulator\\python"

    # 同时生成 qmt-server 侧的客户端配置（默认不生成，手动指定输出路径）
    python scripts/deploy_bigqmt_server.py --account-id 12345678 \\
        --client-config-out "C:\\Python312\\Lib\\site-packages\\bigqmt_signal_trader_client_config.py"

部署内容（--entry multifile，默认）：

- ``bigqmt_signal_trader/``            RPC 服务端包（QMT 内置 Python 3.6）
- ``BIGQMT_REDIS_DRYRUN.py``           QMT 侧入口（策略沙箱加载器）
- ``bigqmt_signal_trader_strategy.py`` 策略包装（init/handlebar/adjust 驱动）
- ``bigqmt_signal_trader_redis_rpc_runtime.py`` / ``*_dryrun.py`` 等顶层模块

``--entry singlefile`` 时只部署 ``BIGQMT_PIPE_ALL_IN_ONE.py`` 扁平单文件包
（内嵌 pipe 配置，沙箱禁 open() 的终端唯一可跑的形态；账号已烘焙进包内，
部署前校验与 --account-id 一致，见下）。``--client-config-out`` 额外生成
qmt-server 侧客户端配置（pipe 零 redis 键）。

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
- QMT 侧 Python 3.6 只需所选 transport 的依赖：**pipe 零依赖**（ctypes +
  kernel32，沙箱只需放行 ctypes，本部署默认，见 docs/IPC_USAGE.md）；
  Redis 模式为纯 Python（redis-py 3.5.x，可手动复制包目录规避旧
  OpenSSL SSL 问题）。
- 下单默认关闭：QMT 侧配置 ``rpc_allow_order_methods: False``（默认），
  需显式开启（docs/big-qmt.md §5.3 灰度开关）。
"""

from __future__ import annotations

import argparse
import re
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

# --entry pipe_dryrun 部署的模块树与入口（pipe 多文件形态）。
# 树由 BIGQMT_PIPE_ALL_IN_ONE.py 经 tools/extract_pipe_modules.py 抽取，
# 逐模块字节级往返校验，与扁平单文件包严格同码；入口 BIGQMT_PIPE_DRYRUN.py
# 自带 os.open / kernel32.CreateFileW 读文件回退，可在「沙箱禁 open() 但
# 放行 CRT/Win32」的终端（如 QMT 模拟端）直接跑多文件形态（FILEIO_PROBE.py
# 可先行探测）。树内 local_config 不随树复制 —— 凭据按模板生成（见 main）。
PIPE_DRYRUN_TREE = "bigqmt_pipe_signal_trader"
PIPE_DRYRUN_ENTRY = "BIGQMT_PIPE_DRYRUN.py"
LOCAL_CONFIG_NAME = "bigqmt_signal_trader_local_config.py"

LOCAL_CONFIG_TEMPLATE = '''#coding:utf-8
"""bigqmt_signal_trader 本地配置（bigqmt_signal_trader_local_config.py）。

由 scripts/deploy_bigqmt_server.py 生成，包含账户与 RPC 通道配置，
请勿提交到版本控制（本文件位于 QMT 安装目录，天然在仓库之外）。
"""

# 完整版 QMT 绑定的资金账号（单实例单账户）
BIGQMT_ACCOUNT_ID = "__ACCOUNT_ID__"

BIGQMT_REDIS_CONFIG = {
__REDIS_BLOCK__
    # 下单方法（submit_order / cancel_order）门控，默认关闭 ——
    # 灰度验证字段对比通过后再改 True 开启下单（docs/big-qmt.md §6.4）
    "rpc_allow_order_methods": False,
}

# RPC 超时（秒），默认 6.0；下载大窗口数据时可调大
# BIGQMT_RPC_TIMEOUT_SECONDS = 6.0
'''

# --rpc-transport pipe（默认）: 命名管道，QMT 沙箱安全。redis 连接键一个
# 都不写 —— 配置里出现 host/port 会让运行时把它当「显式配了 redis」，
# 多出一条拨号路径（docs/IPC_USAGE.md「Deliberately absent」节）。
REDIS_BLOCK_PIPE = '''    # 命名管道通道（本部署默认）：零依赖、无 socket、无 redis 包。
    # 沙箱只需放行 ctypes；管道路径由 pipe_name + 账号派生：
    #   \\\\.\\pipe\\bigqmt_rpc_<账号>           请求/响应
    #   \\\\.\\pipe\\bigqmt_rpc_events_<账号>    推送（行情/委托/成交）
    "transport": "pipe",
    "redis_enabled": False,
'''

# --rpc-transport redis: 原始 Redis 通道（LPUSH bigqmt:rpc:queue:<账号>）。
REDIS_BLOCK_REDIS = '''    "transport": "redis",
    "host": "{redis_host}",
    "port": {redis_port},
    "db": {redis_db},
    # "username": "",
    # "password": "",
    # "protocol": 2,  # redis-py 8.x 默认 RESP3，Redis 5.0 只支持 RESP2
'''

# --entry singlefile 部署的扁平单文件名（构建产物，见 _singlefile_build/）
BUNDLE_FILE = "BIGQMT_PIPE_ALL_IN_ONE.py"

# qmt-server 侧客户端配置模板（--client-config-out 输出；
# 已存在则保留不覆盖，与 local_config 同策略：凭据文件绝不覆盖）。
CLIENT_CONFIG_TEMPLATE = '''# coding: utf-8
"""Client-side private config (bigqmt_signal_trader_client_config.py).

由 scripts/deploy_bigqmt_server.py 生成；qmt-server / 外部脚本经
xtquant_compat.load_client_config() 按 sys.path 解析到本文件。
请勿提交到版本控制（含账户信息）。
"""

BIGQMT_ACCOUNT_ID = '__ACCOUNT_ID__'
BIGQMT_RPC_TIMEOUT_SECONDS = 30.0

BIGQMT_REDIS_CONFIG = {
__CLIENT_BLOCK__
}

# 引用/历史读直达 FormulaServer(58600)，未命中回退 RPC
BIGQMT_FORMULA_SERVER_CONFIG = {"enabled": True}
# MiniQMT 兼容本地缓存
BIGQMT_LOCAL_CACHE_CONFIG = {
    "enabled": True,
    "dir": None,
    "fallback_rpc": True,
    "format": "auto",
}
'''

# pipe 客户端块：与 REDIS_BLOCK_PIPE 同理 —— 一个 redis 连接键都不写。
# 写了 host/port 会被运行时判为「显式配了 redis」，多一条拨号路径，
# 且在禁连终端上那一 dial 是致命的（docs/IPC_USAGE.md「Deliberately absent」）。
CLIENT_BLOCK_PIPE = '''    # 命名管道通道：与 QMT 侧 pipe 服务端对接，零依赖、无 socket。
    # 不写 host/port/db/username/password（原因见 docs/IPC_USAGE.md）。
    "transport": 'pipe',
'''

CLIENT_BLOCK_REDIS = '''    "transport": "redis",
    "host": "{redis_host}",
    "port": {redis_port},
    "db": {redis_db},
    # "username": "",
    # "password": "",
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


def _resolve_bundle(args_bundle_path: str, source: Path) -> Path:
    """定位 BIGQMT_PIPE_ALL_IN_ONE.py：--bundle-path → 源目录根。

    源目录是 xtquant_big_convert 源码 checkout 时（如 \\\\host.lan\\Data
    \\xtquant_big_convert\\src），构建产物就在包旁边。
    """
    if args_bundle_path:
        p = Path(args_bundle_path)
        if not p.is_file():
            raise SystemExit(f"--bundle-path 指定的文件不存在：{p}")
        return p
    p = source / BUNDLE_FILE
    if p.is_file():
        return p
    raise SystemExit(
        f"找不到 {BUNDLE_FILE}（--entry singlefile 必需）。\n"
        "先构建：在装有 bigqmt_signal_trader 包的机器上运行\n"
        "  _singlefile_build\\build_pipe_single_file_flat.py（PYTHONPATH 指向 QMT\n"
        "  python 目录），产物拷到源目录根或用 --bundle-path 显式指定。"
    )


def _inspect_bundle(bundle: Path) -> tuple:
    """校验扁平包可解码且内嵌 pipe 配置，返回 (account_id, transport)。

    包是 GBK 编码（#coding:gbk）；账号在构建时烘焙进 _SHELL_CONFIG，
    部署到别的资金账号必须重新构建，不能靠改配置。
    """
    raw = bundle.read_bytes()
    try:
        text = raw.decode("gbk")
    except UnicodeDecodeError as exc:
        raise SystemExit(f"{bundle.name} 不是合法 GBK 编码（损坏或非本构建产物）：{exc}")
    m = re.search(r'^BIGQMT_ACCOUNT_ID\s*=\s*["\']([^"\']+)["\']', text, re.M)
    account = m.group(1) if m else ""
    t = re.search(r'["\']transport["\']\s*:\s*["\']([a-z]+)["\']', text)
    transport = t.group(1) if t else ""
    if not account or transport != "pipe":
        raise SystemExit(
            f"{bundle.name} 内嵌配置异常：account={account or '?'} "
            f"transport={transport or '?'}（期望 pipe）。请重新构建。"
        )
    return account, transport


def _backup_existing(qmt_dir: Path, backup_dir: Path, extra_files=(),
                     extra_dirs=()) -> Path:
    """把已存在的受管文件打包备份，返回 zip 路径（无内容可备时返回 None）。"""
    targets = []
    pkg_dir = qmt_dir / "bigqmt_signal_trader"
    if pkg_dir.is_dir():
        targets.append(pkg_dir)
    for name in extra_dirs:
        d = qmt_dir / name
        if d.is_dir():
            targets.append(d)
    for name in TOP_LEVEL_FILES:
        if (qmt_dir / name).is_file():
            targets.append(qmt_dir / name)
    for name in extra_files:
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


def _report_leftovers(source: Path, qmt_dir: Path,
                      extra_trees: tuple = ()) -> list:
    """目标里存在、上游已没有的受管文件（上游改名/删除后残留）。"""
    left = []
    for tree in ("bigqmt_signal_trader",) + tuple(extra_trees):
        src_pkg = source / tree
        dst_pkg = qmt_dir / tree
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
                    left.append(tree + "/" + rel)
    for name in TOP_LEVEL_FILES:
        if (qmt_dir / name).is_file() and not (source / name).is_file():
            left.append(name)
    return left


def _copy_tree(src: Path, dst: Path, dry_run: bool,
               skip_files: tuple = ()) -> int:
    """复制包目录（跳过 __pycache__ 与 skip_files 内的文件名），返回文件数。"""
    count = 0
    for item in sorted(src.rglob("*")):
        if "__pycache__" in item.parts:
            continue
        if item.is_dir():
            continue
        if item.name in skip_files:
            continue
        rel = item.relative_to(src)
        target = dst / rel
        print(f"  {rel}")
        if not dry_run:
            target.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(item, target)
        count += 1
    return count


def _emit_client_config(args) -> None:
    """--client-config-out：生成 qmt-server 侧客户端配置（pipe 零 redis 键）。

    qmt-server 进程靠 sys.path 解析 bigqmt_signal_trader_client_config，
    装在 exe 里的 qmt-server 不会自动带上 QMT python 目录 —— 这就是
    「504 redis rpc timeout」的根因（docs/IPC_USAGE.md Troubleshooting）。
    已存在的文件绝不覆盖（同 local_config 策略）。
    """
    if not args.client_config_out:
        return
    if args.rpc_transport not in ("pipe", "redis"):
        raise SystemExit(
            f"客户端配置模板仅支持 pipe/redis，当前 --rpc-transport "
            f"{args.rpc_transport}"
        )
    out = Path(args.client_config_out)
    if out.is_file():
        print(f"[保留] {out} 已存在，不覆盖")
        return
    if args.rpc_transport == "redis":
        block = CLIENT_BLOCK_REDIS.format(
            redis_host=args.redis_host,
            redis_port=args.redis_port,
            redis_db=args.redis_db,
        )
    else:
        block = CLIENT_BLOCK_PIPE
    text = (CLIENT_CONFIG_TEMPLATE
            .replace("__ACCOUNT_ID__", args.account_id or "YOUR_ACCOUNT_ID")
            .replace("__CLIENT_BLOCK__", block.rstrip("\n")))
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(text, encoding="utf-8")
    print(f"[生成] {out}（客户端配置，{args.rpc_transport} 通道）")
    print("       重启 qmt-server 后生效（load_client_config 在启动时解析一次）")


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
    parser.add_argument(
        "--rpc-transport",
        choices=("pipe", "redis", "zmq", "mysql"),
        default="pipe",
        help="RPC 通道（默认 pipe：命名管道，QMT 沙箱安全，零依赖；"
        "pipe 模式 --redis-* 参数不参与模板）",
    )
    parser.add_argument(
        "--entry",
        choices=("multifile", "singlefile", "pipe_dryrun"),
        default="multifile",
        help="QMT 侧部署形态：multifile=包+顶层入口（完整版 QMT，默认）；"
        "singlefile=只部署扁平单文件包（沙箱禁 open() 且 CRT/Win32 也被拦的终端，"
        "隐含 --rpc-transport pipe）；pipe_dryrun=部署 BIGQMT_PIPE_DRYRUN.py "
        "+ bigqmt_pipe_signal_trader/ 模块树（pipe 多文件形态，入口自带 "
        "os.open/kernel32 读文件回退，适合沙箱禁 open() 但放行底层 IO 的终端）",
    )
    parser.add_argument(
        "--bundle-path",
        default="",
        help=f"--entry singlefile 时 {BUNDLE_FILE} 的路径"
        "（默认：源目录根；找不到会给出构建指引）",
    )
    parser.add_argument(
        "--client-config-out",
        default="",
        help="额外生成 qmt-server 侧 bigqmt_signal_trader_client_config.py "
        "到该路径（如 site-packages 内）；已存在则不覆盖",
    )
    parser.add_argument("--redis-host", default="127.0.0.1", help="Redis 地址")
    parser.add_argument("--redis-port", type=int, default=6379, help="Redis 端口")
    parser.add_argument("--redis-db", type=int, default=5, help="Redis db")
    parser.add_argument(
        "--dry-run", action="store_true", help="只列出将复制的文件，不落盘"
    )
    args = parser.parse_args()

    if args.entry in ("singlefile", "pipe_dryrun") and args.rpc_transport != "pipe":
        raise SystemExit(
            f"--entry {args.entry} 仅支持 --rpc-transport pipe："
            f"{BUNDLE_FILE if args.entry == 'singlefile' else PIPE_DRYRUN_ENTRY} "
            "的 transport 为 pipe（pipe_dryrun 入口在加载配置后也会强制 "
            "transport=pipe 并剥掉 redis 连接键）。"
        )

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

    if args.entry == "singlefile":
        # ---- 单文件扁平包部署：沙箱禁 open() 的终端唯一可跑的形态 ----
        bundle = _resolve_bundle(args.bundle_path, source)
        embedded_account, embedded_transport = _inspect_bundle(bundle)
        print(f"[单文件包] {bundle}")
        print(f"  内嵌配置：account={embedded_account} transport={embedded_transport}")
        if args.account_id and args.account_id != embedded_account:
            raise SystemExit(
                f"账号不匹配：--account-id {args.account_id} ≠ 包内烘焙的 "
                f"{embedded_account}。\n账号在构建时写死，换账号必须用 "
                "_singlefile_build\\build_pipe_single_file_flat.py 重新构建，"
                "不能改配置。"
            )

        # 0. 覆盖前备份已存在的包文件
        if not args.dry_run and args.qmt_python_dir and not args.no_backup:
            zip_path = _backup_existing(qmt_dir, Path(args.backup_dir),
                                       extra_files=(BUNDLE_FILE,))
            if zip_path:
                print(f"[备份] 旧受管文件 -> {zip_path}")

        print(f"[文件] {BUNDLE_FILE}")
        if not args.dry_run and args.qmt_python_dir:
            shutil.copy2(bundle, qmt_dir / BUNDLE_FILE)

        # 扁平包自包含配置（内嵌 _SHELL_CONFIG），不写 local_config ——
        # 写了也会被忽略，反而造成「改了配置却不生效」的假象
        print("[跳过] bigqmt_signal_trader_local_config.py（扁平包内嵌配置，忽略外部配置）")

        verb = "将复制" if args.dry_run else "已复制"
        print(f"\n完成：{verb} 1 个文件（单文件入口）")
        print(
            "后续步骤：\n"
            f"  1. 在 QMT 策略编辑器/模型交易面板运行 {BUNDLE_FILE}\n"
            "     （沙箱终端只支持编辑器挂载运行，禁 open() 的多文件加载器不可用）\n"
            "  2. qmt-server 侧确认 bigqmt_signal_trader_client_config.py 为 pipe\n"
            "     （--client-config-out 可生成；重启 qmt-server 生效）"
        )
        _emit_client_config(args)
        return 0

    if args.entry == "pipe_dryrun":
        # ---- pipe 多文件部署：BIGQMT_PIPE_DRYRUN.py + bigqmt_pipe_signal_trader/ ----
        # 模块树与 BIGQMT_PIPE_ALL_IN_ONE.py 扁平包同源（extract_pipe_modules.py
        # 抽取、逐模块字节级往返校验）。入口自带 os.open / kernel32.CreateFileW
        # 读文件回退，因此可在「沙箱禁 builtins.open 但放行 CRT/Win32」的终端
        # 跑多文件形态；CRIO/Win32 也被拦的终端退回 --entry singlefile。
        tree_src = source / PIPE_DRYRUN_TREE
        if not (tree_src / "bigqmt_signal_trader").is_dir():
            raise SystemExit(
                f"源目录缺少 {PIPE_DRYRUN_TREE}/bigqmt_signal_trader：{source}\n"
                "先抽取（xtquant_big_convert 仓库）："
                "python tools/extract_pipe_modules.py，"
                "产物即源目录下的 bigqmt_pipe_signal_trader/。"
            )
        entry_src = source / PIPE_DRYRUN_ENTRY
        if not entry_src.is_file():
            raise SystemExit(f"源目录缺少 {PIPE_DRYRUN_ENTRY}：{source}")

        # 0. 覆盖前备份已存在的模块树与入口
        if not args.dry_run and args.qmt_python_dir and not args.no_backup:
            zip_path = _backup_existing(
                qmt_dir, Path(args.backup_dir),
                extra_files=(PIPE_DRYRUN_ENTRY,),
                extra_dirs=(PIPE_DRYRUN_TREE,))
            if zip_path:
                print(f"[备份] 旧受管文件 -> {zip_path}")

        # 1. 复制模块树；树内随带的 local_config 不复制（下一按模板生成，
        #    避免把抽取源里烘焙的账号当成部署账号静默落地）
        print(f"[树] {PIPE_DRYRUN_TREE}/")
        total = _copy_tree(tree_src, qmt_dir / PIPE_DRYRUN_TREE, args.dry_run,
                           skip_files=(LOCAL_CONFIG_NAME,))
        if not args.dry_run and args.qmt_python_dir:
            for cache in sorted((qmt_dir / PIPE_DRYRUN_TREE).rglob("__pycache__")):
                shutil.rmtree(cache, ignore_errors=True)

        # 2. 复制入口
        print(f"[文件] {PIPE_DRYRUN_ENTRY}")
        if not args.dry_run and args.qmt_python_dir:
            shutil.copy2(entry_src, qmt_dir / PIPE_DRYRUN_ENTRY)
        total += 1

        # 2b. 遗留告警（上游已删除而目标还留着的树内文件）
        if args.qmt_python_dir:
            for name in _report_leftovers(source, qmt_dir,
                                          extra_trees=(PIPE_DRYRUN_TREE,)):
                # 树内 local_config 受模板管理，不在上游树里，跳过误报
                if name == f"{PIPE_DRYRUN_TREE}/{LOCAL_CONFIG_NAME}":
                    continue
                print(f"[遗留] {name}（上游已无此文件，可手工删除）")

        # 3. 生成树内 local_config（已存在则保留，绝不覆盖敏感配置）
        config_path = qmt_dir / PIPE_DRYRUN_TREE / LOCAL_CONFIG_NAME
        if config_path.is_file():
            print(f"\n[保留] {config_path} 已存在，不覆盖")
        else:
            print(f"\n[生成] {config_path}（账户 ID + pipe 通道配置模板）")
            text = (LOCAL_CONFIG_TEMPLATE
                    .replace("__ACCOUNT_ID__", args.account_id or "YOUR_ACCOUNT_ID")
                    .replace("__REDIS_BLOCK__", REDIS_BLOCK_PIPE.rstrip("\n")))
            if not args.dry_run and args.qmt_python_dir:
                config_path.parent.mkdir(parents=True, exist_ok=True)
                config_path.write_text(text, encoding="utf-8")

        verb = "将复制" if args.dry_run else "已复制"
        print(f"\n完成：{verb} {total} 个文件（pipe 多文件入口）")
        print(
            "后续步骤：\n"
            f"  1. 在 QMT 策略编辑器/模型交易面板运行 {PIPE_DRYRUN_ENTRY}\n"
            "     （入口从 bigqmt_pipe_signal_trader/ 加载模块；沙箱若连\n"
            "      os.open/kernel32 都拦，日志会明确报错，改用 --entry singlefile）\n"
            "  2. qmt-server 侧确认 bigqmt_signal_trader_client_config.py 为 pipe\n"
            "     （--client-config-out 可生成；重启 qmt-server 生效）"
        )
        _emit_client_config(args)
        return 0

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
        print(f"\n[生成] {config_path}（账户 ID + {args.rpc_transport} 通道配置模板）")
        if args.rpc_transport == "redis":
            redis_block = REDIS_BLOCK_REDIS.format(
                redis_host=args.redis_host,
                redis_port=args.redis_port,
                redis_db=args.redis_db,
            )
        else:
            redis_block = REDIS_BLOCK_PIPE.replace(
                '"transport": "pipe"',
                '"transport": "%s"' % args.rpc_transport,
            )
        text = (LOCAL_CONFIG_TEMPLATE
                .replace("__ACCOUNT_ID__", args.account_id or "YOUR_ACCOUNT_ID")
                .replace("__REDIS_BLOCK__", redis_block.rstrip("\n")))
        if not args.dry_run and args.qmt_python_dir:
            config_path.write_text(text, encoding="utf-8")

    verb = "将复制" if args.dry_run else "已复制"
    print(f"\n完成：{verb} {total} 个文件")
    entry = ("BIGQMT_REDIS_DRYRUN.py（多文件入口）" if args.rpc_transport == "redis"
             else "单文件入口（沙箱禁 open() 的终端用 *_ALL_IN_ONE.py 扁平包，"
                  "见 docs/IPC_USAGE.md；pipe 通道下由该包内嵌配置决定 transport）")
    print(
        f"后续步骤（docs/big-qmt.md §6）：\n"
        f"  1. 在 QMT 内置 Python 中运行 {entry}（保持下单关闭）\n"
        "  2. qmt-server 侧并行运行 mini / bigqmt 双后端，逐字段比对\n"
        "  3. 比对通过后把 local_config 中 rpc_allow_order_methods 改 True 灰度下单"
    )
    _emit_client_config(args)
    return 0


if __name__ == "__main__":
    sys.exit(main())
